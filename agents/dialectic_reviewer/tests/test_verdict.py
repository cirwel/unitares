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
    build_continuation_prompt,
    build_review_prompt,
    find_pending_paused_response,
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


def test_prompt_separates_server_captured_governance_evidence_from_agent_claims():
    prompt = build_review_prompt(
        Thesis(
            session_id="s1",
            root_cause="claimed cause",
            paused_agent_state={
                "risk_score": 0.7859,
                "coherence": 0.4986,
                "verdict": "high-risk",
            },
        )
    )
    assert "SERVER-CAPTURED GOVERNANCE EVIDENCE AT SESSION OPEN" in prompt
    assert "not authored by the paused agent" in prompt
    assert "policy_evaluation.action/enforcement" in prompt
    assert '"risk_score": 0.7859' in prompt
    assert '"coherence": 0.4986' in prompt
    assert '"verdict": "high-risk"' in prompt


def test_continuation_prompt_requires_independent_ratification():
    prompt = build_continuation_prompt(
        Thesis(session_id="s1", root_cause="old", proposed_conditions=["old term"]),
        Verdict(False, "deeper cause", ["fix it"], "not enough"),
        {
            "agent_id": "paused",
            "agrees": True,
            "root_cause": "deeper cause",
            "proposed_conditions": ["fix it"],
            "reasoning": "new evidence",
        },
        synthesis_round=3,
    )
    assert "SAME independent reviewer" in prompt
    assert "independently ratify" in prompt
    assert "new evidence" in prompt
    assert "synthesis round 3" in prompt


def test_pending_response_must_follow_reviewers_latest_synthesis():
    session = {
        "paused_agent": "paused",
        "reviewer": "reviewer",
        "transcript": [
            {"phase": "synthesis", "agent_id": "paused", "reasoning": "old"},
            {"phase": "synthesis", "agent_id": "reviewer", "agrees": False},
            {"phase": "synthesis", "agent_id": "paused", "reasoning": "new"},
        ],
    }
    pending = find_pending_paused_response(session)
    assert pending is not None
    assert pending["reasoning"] == "new"

    session["transcript"].append(
        {"phase": "synthesis", "agent_id": "reviewer", "agrees": False}
    )
    assert find_pending_paused_response(session) is None


# --------------------------- Thesis.from_env --------------------------- #
def test_thesis_from_env_parses_json_conditions():
    env = {
        "DIALECTIC_SESSION_ID": "sess-123",
        "DIALECTIC_THESIS_ROOT_CAUSE": "rc",
        "DIALECTIC_THESIS_CONDITIONS": json.dumps(["x", "y"]),
        "DIALECTIC_THESIS_REASONING": "because",
        "DIALECTIC_PAUSED_AGENT_STATE": json.dumps({"coherence": 0.42}),
    }
    t = Thesis.from_env(env)
    assert t.session_id == "sess-123"
    assert t.proposed_conditions == ["x", "y"]
    assert t.paused_agent_state == {"coherence": 0.42}


def test_thesis_from_env_tolerates_newline_conditions():
    env = {"DIALECTIC_SESSION_ID": "s", "DIALECTIC_THESIS_CONDITIONS": "one\ntwo\n"}
    assert Thesis.from_env(env).proposed_conditions == ["one", "two"]


@pytest.mark.parametrize("raw_state", ["not-json", "[]", '"agent-authored prose"'])
def test_thesis_from_env_discards_malformed_or_non_object_pause_evidence(raw_state):
    env = {
        "DIALECTIC_SESSION_ID": "s",
        "DIALECTIC_PAUSED_AGENT_STATE": raw_state,
    }
    assert Thesis.from_env(env).paused_agent_state == {}


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

    # This test isolates the initial judgment; continuation behavior has its own
    # wiring test below.
    monkeypatch.setenv("UNITARES_DIALECTIC_CONTINUATION_WAIT_S", "0")

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
    # The reviewer submits via the `dialectic` umbrella tool (action=...), not the
    # bare submit_* names (which are register=False on the MCP surface).
    dialectic_calls = [a for n, a in calls if n == "dialectic"]
    anti = [a for a in dialectic_calls if a.get("action") == "antithesis"]
    synth = [a for a in dialectic_calls if a.get("action") == "synthesis"]
    assert anti and anti[0]["session_id"] == "sess-9"
    # synthesis carried agrees=False — the reviewer actually blocked
    assert synth and synth[0]["agrees"] is False
    assert synth[0]["session_id"] == "sess-9"

    # THE REPLAY GUARD. Passing verdict.reasoning to both calls made the
    # synthesis a byte-identical copy of the antithesis in 60 of 60 orchestrated
    # sessions from 2026-06-23 onward, so every transcript printed the same
    # paragraph under both headings. The argument belongs to the antithesis; the
    # synthesis carries the verdict. finalize_resolution recovers the rationale
    # from this agent's own antithesis, so nothing is lost downstream.
    assert "reasoning" not in synth[0], (
        "synthesis must not restate the antithesis — that is the replay bug"
    )
    assert anti[0].get("reasoning"), "the antithesis is where the argument goes"
    assert synth[0]["root_cause"] == "shallow"
    assert synth[0]["proposed_conditions"] == ["real fix"]


@pytest.mark.asyncio
async def test_run_reconsiders_paused_response_with_same_reviewer(monkeypatch):
    """A rejection stays live long enough for the same identity to ratify a fix."""
    import sys
    import types

    import agents.dialectic_reviewer.reviewer as r
    from src.dialectic_protocol import (
        DialecticMessage,
        DialecticPhase,
        DialecticSession,
    )

    prompts: list[str] = []
    outputs = iter(
        [
            '{"agrees": false, "root_cause": "shallow", '
            '"proposed_conditions": ["supply evidence"], "reasoning": "missing"}',
            '{"agrees": true, "root_cause": "verified", '
            '"proposed_conditions": ["ship the evidence"], "reasoning": "addressed"}',
        ]
    )

    async def fake_obtain(prompt):
        prompts.append(prompt)
        return next(outputs)

    monkeypatch.setattr(r, "obtain_reviewer_text", fake_obtain)
    monkeypatch.setenv("UNITARES_DIALECTIC_CONTINUATION_WAIT_S", "1")
    monkeypatch.setenv("UNITARES_DIALECTIC_CONTINUATION_POLL_S", "0.01")

    calls: list[tuple[str, dict]] = []
    session = DialecticSession(paused_agent_id="paused-uuid")
    session.session_id = "sess-rounds"
    thesis_result = session.submit_thesis(
        DialecticMessage(
            phase="thesis",
            agent_id="paused-uuid",
            timestamp="2026-08-16T00:00:00+00:00",
            root_cause="claimed",
            proposed_conditions=["initial"],
            reasoning="initial claim",
        )
    )
    assert thesis_result["success"] is True

    class FakeClient:
        def __init__(self, url):
            self.url = url
            self.agent_uuid = "reviewer-uuid"
            self.paused_response_submitted = False

        async def connect(self):
            return None

        async def onboard(self, **kw):
            calls.append(("onboard", kw))
            return None

        async def call_tool(self, name, args, **kw):
            calls.append((name, args))
            if name != "dialectic":
                return {"success": True}
            if args.get("action") == "antithesis":
                return session.submit_antithesis(
                    DialecticMessage(
                        phase="antithesis",
                        agent_id=self.agent_uuid,
                        timestamp="2026-08-16T00:01:00+00:00",
                        reasoning=args["reasoning"],
                    )
                )
            if args.get("action") == "synthesis":
                return session.submit_synthesis(
                    DialecticMessage(
                        phase="synthesis",
                        agent_id=self.agent_uuid,
                        timestamp="2026-08-16T00:02:00+00:00",
                        agrees=args["agrees"],
                        root_cause=args.get("root_cause"),
                        proposed_conditions=args.get("proposed_conditions"),
                        reasoning=args.get("reasoning"),
                    )
                )
            if args.get("action") == "get":
                if not self.paused_response_submitted:
                    paused_result = session.submit_synthesis(
                        DialecticMessage(
                            phase="synthesis",
                            agent_id="paused-uuid",
                            timestamp="2026-08-16T00:03:00+00:00",
                            agrees=True,
                            root_cause="verified",
                            proposed_conditions=["ship the evidence"],
                            reasoning="here is the missing evidence",
                        )
                    )
                    assert paused_result["blocked"] == "reviewer_objection_stands"
                    self.paused_response_submitted = True
                return {"success": True, **session.to_dict()}
            raise AssertionError(f"unexpected action: {args}")

        async def checkin(self, response_text, complexity=0.3, confidence=0.7, **kw):
            calls.append(("checkin", {"response_text": response_text, **kw}))
            return None

        async def disconnect(self):
            return None

    fake_mod = types.ModuleType("unitares_sdk.client")
    fake_mod.GovernanceClient = FakeClient  # type: ignore[attr-defined]
    pkg = types.ModuleType("unitares_sdk")
    monkeypatch.setitem(sys.modules, "unitares_sdk", pkg)
    monkeypatch.setitem(sys.modules, "unitares_sdk.client", fake_mod)

    verdict = await r.run(
        Thesis(
            session_id="sess-rounds",
            root_cause="claimed",
            proposed_conditions=["initial"],
        ),
        governance_url="http://localhost:8767",
        parent_agent_id="paused-uuid",
    )

    assert verdict.agrees is True
    assert len(prompts) == 2
    assert "here is the missing evidence" in prompts[1]
    dialectic_calls = [args for name, args in calls if name == "dialectic"]
    assert len([call for call in dialectic_calls if call["action"] == "antithesis"]) == 1
    syntheses = [call for call in dialectic_calls if call["action"] == "synthesis"]
    assert [call["agrees"] for call in syntheses] == [False, True]
    assert syntheses[1]["reasoning"] == "addressed"
    assert session.phase == DialecticPhase.RESOLVED
    assert session.synthesis_round == 3
    assert [message.agent_id for message in session.transcript[-3:]] == [
        "reviewer-uuid",
        "paused-uuid",
        "reviewer-uuid",
    ]
