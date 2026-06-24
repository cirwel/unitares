"""Unit tests for the orchestrated dialectic reviewer's pure logic.

The independence-critical invariant: a disagreeing (or unparseable) model yields
``agrees=False``. The current in-process synthetic path returns ``agrees=False``
exactly zero times by construction — these tests prove the orchestrated reviewer
can actually block.
"""
import json

import pytest

from agents.dialectic_reviewer.reviewer import (
    Thesis,
    Verdict,
    build_review_prompt,
    parse_reviewer_verdict,
)


# --------------------------- parse_reviewer_verdict --------------------------- #
def test_disagreement_yields_agrees_false():
    out = json.dumps(
        {
            "agrees": False,
            "root_cause": "The agent rationalized; the real cause is unaddressed.",
            "proposed_conditions": ["Fix the actual lock contention first"],
            "reasoning": "Conditions don't touch the root cause.",
        }
    )
    v = parse_reviewer_verdict(out)
    assert v.agrees is False
    assert v.degraded is False
    assert v.proposed_conditions == ["Fix the actual lock contention first"]


def test_agreement_yields_agrees_true_with_conditions():
    out = '```json\n{"agrees": true, "root_cause": "transient", "proposed_conditions": ["retry once"], "reasoning": "ok"}\n```'
    v = parse_reviewer_verdict(out)
    assert v.agrees is True
    assert v.proposed_conditions == ["retry once"]


def test_string_bool_is_coerced_like_the_server():
    assert parse_reviewer_verdict('{"agrees": "true"}').agrees is True
    assert parse_reviewer_verdict('{"agrees": "false"}').agrees is False
    assert parse_reviewer_verdict('{"agrees": "maybe"}').agrees is False


def test_think_block_is_stripped_before_json():
    out = '<think>I should be skeptical here.</think>\n{"agrees": false, "reasoning": "r"}'
    v = parse_reviewer_verdict(out)
    assert v.agrees is False
    assert v.degraded is False


def test_conditions_alias_accepted():
    v = parse_reviewer_verdict('{"agrees": false, "conditions": ["a", "b"]}')
    assert v.proposed_conditions == ["a", "b"]


def test_unparseable_output_degrades_to_disagree_not_approve():
    for junk in ["", "I think it's fine, sure.", "not json at all", "{broken json"]:
        v = parse_reviewer_verdict(junk)
        assert v.agrees is False, f"unparseable {junk!r} must NOT rubber-stamp"
        assert v.degraded is True


def test_missing_agrees_key_defaults_to_disagree():
    v = parse_reviewer_verdict('{"reasoning": "no verdict field"}')
    assert v.agrees is False
    assert v.degraded is False  # JSON parsed; just no approval token


# --------------------------- build_review_prompt --------------------------- #
def test_prompt_frames_disagreement_as_valid():
    prompt = build_review_prompt(
        Thesis(session_id="s1", root_cause="rc", proposed_conditions=["c1"], reasoning="r")
    )
    assert "INDEPENDENT" in prompt
    assert "rubber-stamp" in prompt.lower()
    assert "c1" in prompt  # the proposed condition is surfaced for review
    assert "STRICT JSON" in prompt


# --------------------------- Thesis.from_env --------------------------- #
def test_thesis_from_env_parses_json_conditions():
    env = {
        "DIALECTIC_SESSION_ID": "sess-123",
        "DIALECTIC_THESIS_ROOT_CAUSE": "rc",
        "DIALECTIC_THESIS_CONDITIONS": json.dumps(["x", "y"]),
        "DIALECTIC_THESIS_REASONING": "because",
    }
    t = Thesis.from_env(env)
    assert t.session_id == "sess-123"
    assert t.proposed_conditions == ["x", "y"]


def test_thesis_from_env_tolerates_newline_conditions():
    env = {"DIALECTIC_SESSION_ID": "s", "DIALECTIC_THESIS_CONDITIONS": "one\ntwo\n"}
    assert Thesis.from_env(env).proposed_conditions == ["one", "two"]


# --------------------------- SDK interface conformance --------------------------- #
def test_runner_only_calls_real_governance_client_methods():
    """Guard against the mock lying: every GovernanceClient method run() invokes
    must actually exist on the real SDK class. (This catches close-vs-disconnect /
    sync_state-vs-checkin drift that mocked wiring tests cannot.)"""
    client_mod = pytest.importorskip("unitares_sdk.client")
    gc = client_mod.GovernanceClient
    for method in ("connect", "onboard", "call_tool", "checkin", "disconnect"):
        assert hasattr(gc, method), f"GovernanceClient is missing {method!r} — runner would crash live"


# --------------------------- run() wiring (mocked) --------------------------- #
@pytest.mark.asyncio
async def test_run_submits_disagreement_through_protocol(monkeypatch):
    """A disagreeing model must reach submit_synthesis with agrees=False."""
    import agents.dialectic_reviewer.reviewer as r

    async def fake_model(prompt, model=r.DEFAULT_MODEL):
        return '{"agrees": false, "root_cause": "shallow", "proposed_conditions": ["real fix"], "reasoning": "no"}'

    monkeypatch.setattr(r, "call_reviewer_model", fake_model)

    calls: list[tuple[str, dict]] = []

    class FakeClient:
        def __init__(self, url):
            self.url = url

        async def connect(self):
            return None

        async def onboard(self, **kw):
            calls.append(("onboard", kw))
            return None

        async def call_tool(self, name, args, **kw):
            calls.append((name, args))
            return {"ok": True}

        # Method names MUST mirror the real GovernanceClient — a mock that
        # invents names hides runtime AttributeErrors (it did, once).
        async def checkin(self, response_text, complexity=0.3, confidence=0.7, **kw):
            calls.append(("checkin", {"response_text": response_text, **kw}))
            return None

        async def disconnect(self):
            return None

    # Inject the fake SDK client module so `from unitares_sdk.client import GovernanceClient` resolves.
    import sys
    import types

    fake_mod = types.ModuleType("unitares_sdk.client")
    fake_mod.GovernanceClient = FakeClient  # type: ignore[attr-defined]
    pkg = types.ModuleType("unitares_sdk")
    monkeypatch.setitem(sys.modules, "unitares_sdk", pkg)
    monkeypatch.setitem(sys.modules, "unitares_sdk.client", fake_mod)

    verdict = await r.run(
        Thesis(session_id="sess-9", root_cause="rc", proposed_conditions=["c"], reasoning="x"),
        governance_url="http://localhost:8767",
        parent_agent_id="parent-uuid",
    )

    assert verdict.agrees is False
    # onboarded with lineage + the dedicated spawn_reason
    onboard_kw = next(c[1] for c in calls if c[0] == "onboard")
    # `name` is the REQUIRED first arg of GovernanceClient.onboard — without it the
    # runner TypeErrors on every spawn (the blocker that made the runner inert).
    assert onboard_kw["name"] == r.REVIEWER_NAME
    assert onboard_kw["force_new"] is True
    assert onboard_kw["spawn_reason"] == r.SPAWN_REASON
    assert onboard_kw["parent_agent_id"] == "parent-uuid"
    # submit_synthesis carried agrees=False — the reviewer actually blocked
    synth = [a for n, a in calls if n == "submit_synthesis"]
    assert synth and synth[0]["agrees"] is False
    assert synth[0]["session_id"] == "sess-9"
