"""A fallback may object; it may never approve (#2379).

``fallback_from`` means the operator selected a reviewer host, that host
returned no verdict, and the local model answered instead. Session
17ca66285f91e61e is the live instance: gemini-3.8-flash was truncated at the
1024-token budget, gemma4 restated the thesis's own conditions, and the session
resolved ``agrees: true`` in 34 seconds.

Properties under test:
  - a fallback approval becomes an abstention (no verdict filed, slot left open);
  - a fallback objection is still filed, with ``fallback_from`` on the record
    (``degraded`` keeps its verdict-level meaning; see test_verdict_provenance);
  - a verdict from the selected host itself is untouched;
  - in continuation, a fallback approval cannot overwrite a standing rejection.
"""

from __future__ import annotations

import sys
import types

import pytest

import agents.dialectic_reviewer.reviewer as r
from agents.dialectic_reviewer.host_backends import HostReviewResult
from agents.dialectic_reviewer.reviewer import Thesis, Verdict, withhold_fallback_approval

EXTERNAL_HOST = "external:generativelanguage.googleapis.com"
APPROVE = (
    '{"agrees": true, "root_cause": "rc", '
    '"proposed_conditions": ["the thesis condition"], "reasoning": "fine"}'
)
OBJECT = (
    '{"agrees": false, "root_cause": "shallow", '
    '"proposed_conditions": ["supply evidence"], "reasoning": "missing"}'
)


def _truncated_external() -> HostReviewResult:
    return HostReviewResult(
        text=None,
        host_id=EXTERNAL_HOST,
        model_requested="gemini-test",
        finish_reason="length",
        backend="external",
        error="External reviewer was truncated at 8192 tokens",
    )


def _approval() -> Verdict:
    return Verdict(
        agrees=True, root_cause="rc", proposed_conditions=["c"], reasoning="fine"
    )


# ------------------------------ pure rule ---------------------------------- #
def test_selected_host_verdict_passes_through_untouched():
    verdict = _approval()
    assert withhold_fallback_approval(verdict, {"fallback_from": None}) is verdict


def test_fallback_approval_is_withheld_as_an_abstention():
    out = withhold_fallback_approval(_approval(), {"fallback_from": EXTERNAL_HOST})
    assert out.agrees is False
    assert out.judgment_formed is False
    assert out.proposed_conditions == []
    assert EXTERNAL_HOST in out.reasoning


def test_fallback_objection_is_kept_as_filed():
    verdict = Verdict(
        agrees=False, root_cause="shallow", proposed_conditions=["x"], reasoning="no"
    )
    assert withhold_fallback_approval(verdict, {"fallback_from": EXTERNAL_HOST}) is verdict


def test_fallback_non_judgment_is_left_to_the_existing_abstention():
    verdict = r.parse_reviewer_verdict("prose, no json")
    out = withhold_fallback_approval(verdict, {"fallback_from": EXTERNAL_HOST})
    assert out is verdict


# ------------------------------ run() wiring -------------------------------- #
def _install_fake_client(monkeypatch, calls, *, on_call=None):
    class FakeClient:
        def __init__(self, url):
            self.agent_uuid = "reviewer-uuid"

        async def connect(self):
            return None

        async def onboard(self, **kw):
            calls.append(("onboard", kw))
            return None

        async def call_tool(self, name, args, **kw):
            calls.append((name, args))
            if on_call is not None:
                return on_call(self, args)
            if args.get("action") == "antithesis" and args.get("judgment_formed") is False:
                return {"abstained": True, "reviewer_slot_open": True}
            return {"ok": True}

        async def checkin(self, response_text, complexity=0.3, confidence=0.7, **kw):
            calls.append(("checkin", {"response_text": response_text, "confidence": confidence}))
            return None

        async def disconnect(self):
            return None

    fake_mod = types.ModuleType("unitares_sdk.client")
    fake_mod.GovernanceClient = FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "unitares_sdk", types.ModuleType("unitares_sdk"))
    monkeypatch.setitem(sys.modules, "unitares_sdk.client", fake_mod)


def _select_external_that_truncates(monkeypatch, local_reply):
    monkeypatch.setenv("UNITARES_DIALECTIC_REVIEWER_HOST", "gemini")
    monkeypatch.setenv("UNITARES_DIALECTIC_CONTINUATION_WAIT_S", "0")

    async def fake_external(prompt):
        return _truncated_external()

    async def fake_local(prompt, model=r.DEFAULT_MODEL):
        return local_reply

    monkeypatch.setattr(r, "call_external_reviewer", fake_external)
    monkeypatch.setattr(r, "call_reviewer_model", fake_local)


@pytest.mark.asyncio
async def test_run_abstains_when_only_the_fallback_approved(monkeypatch):
    """The #2379 shape: no synthesis may be filed, so nothing can resolve."""
    _select_external_that_truncates(monkeypatch, APPROVE)
    calls: list[tuple[str, dict]] = []
    _install_fake_client(monkeypatch, calls)

    verdict = await r.run(
        Thesis(session_id="sess-2379", root_cause="rc", proposed_conditions=["c"]),
        governance_url="http://localhost:8767",
        parent_agent_id="paused-uuid",
    )

    assert verdict.judgment_formed is False
    assert verdict.agrees is False
    dialectic = [a for n, a in calls if n == "dialectic"]
    assert [a["action"] for a in dialectic] == ["antithesis"], dialectic
    anti = dialectic[0]
    assert anti["judgment_formed"] is False
    stored = anti["observed_metrics"]["reviewer_backend"]
    assert stored["fallback_from"] == EXTERNAL_HOST
    assert stored["backend"] == "ollama"


@pytest.mark.asyncio
async def test_run_files_a_fallback_objection_with_its_provenance(monkeypatch):
    _select_external_that_truncates(monkeypatch, OBJECT)
    calls: list[tuple[str, dict]] = []
    _install_fake_client(monkeypatch, calls)

    verdict = await r.run(
        Thesis(session_id="sess-obj", root_cause="rc", proposed_conditions=["c"]),
        governance_url="http://localhost:8767",
        parent_agent_id="paused-uuid",
    )

    assert verdict.agrees is False
    assert verdict.judgment_formed is True
    dialectic = [a for n, a in calls if n == "dialectic"]
    anti = next(a for a in dialectic if a["action"] == "antithesis")
    synth = next(a for a in dialectic if a["action"] == "synthesis")
    assert anti["judgment_formed"] is True
    assert anti["observed_metrics"]["reviewer_backend"]["fallback_from"] == EXTERNAL_HOST
    assert synth["agrees"] is False
    checkin = next(c[1] for c in calls if c[0] == "checkin")
    assert f"fallback_from={EXTERNAL_HOST}" in checkin["response_text"]


@pytest.mark.asyncio
async def test_run_keeps_an_approval_from_the_selected_host(monkeypatch):
    """The rule must not touch the path it exists to protect."""
    monkeypatch.setenv("UNITARES_DIALECTIC_REVIEWER_HOST", "gemini")
    monkeypatch.setenv("UNITARES_DIALECTIC_CONTINUATION_WAIT_S", "0")

    async def fake_external(prompt):
        return HostReviewResult(
            text=APPROVE, host_id=EXTERNAL_HOST, backend="external",
            models_used=["gemini-test"],
        )

    async def local_must_not_run(prompt, model=r.DEFAULT_MODEL):
        raise AssertionError("local fallback ran although the selected host answered")

    monkeypatch.setattr(r, "call_external_reviewer", fake_external)
    monkeypatch.setattr(r, "call_reviewer_model", local_must_not_run)
    calls: list[tuple[str, dict]] = []
    _install_fake_client(monkeypatch, calls)

    verdict = await r.run(
        Thesis(session_id="sess-ok", root_cause="rc", proposed_conditions=["c"]),
        governance_url="http://localhost:8767",
        parent_agent_id="paused-uuid",
    )

    assert verdict.agrees is True
    dialectic = [a for n, a in calls if n == "dialectic"]
    anti = next(a for a in dialectic if a["action"] == "antithesis")
    assert "fallback_from" not in anti["observed_metrics"]["reviewer_backend"]
    assert any(a["action"] == "synthesis" and a["agrees"] is True for a in dialectic)


@pytest.mark.asyncio
async def test_continuation_fallback_approval_does_not_overwrite_the_rejection(
    monkeypatch,
):
    """Round 1 comes from the selected host and objects; round 2's host call
    fails and the fallback approves. The standing rejection must survive."""
    from src.dialectic_protocol import DialecticMessage, DialecticSession

    monkeypatch.setenv("UNITARES_DIALECTIC_REVIEWER_HOST", "gemini")
    monkeypatch.setenv("UNITARES_DIALECTIC_CONTINUATION_WAIT_S", "1")
    monkeypatch.setenv("UNITARES_DIALECTIC_CONTINUATION_POLL_S", "0.01")

    external_replies = iter(
        [
            HostReviewResult(
                text=OBJECT, host_id=EXTERNAL_HOST, backend="external",
                models_used=["gemini-test"],
            ),
            _truncated_external(),
        ]
    )

    async def fake_external(prompt):
        return next(external_replies)

    async def fake_local(prompt, model=r.DEFAULT_MODEL):
        return APPROVE

    monkeypatch.setattr(r, "call_external_reviewer", fake_external)
    monkeypatch.setattr(r, "call_reviewer_model", fake_local)

    session = DialecticSession(paused_agent_id="paused-uuid")
    session.session_id = "sess-cont"
    assert session.submit_thesis(
        DialecticMessage(
            phase="thesis", agent_id="paused-uuid",
            timestamp="2026-09-24T00:00:00+00:00", root_cause="claimed",
            proposed_conditions=["initial"], reasoning="initial claim",
        )
    )["success"] is True
    state = {"paused_response_submitted": False}

    def on_call(client, args):
        action = args.get("action")
        if action == "antithesis":
            return session.submit_antithesis(
                DialecticMessage(
                    phase="antithesis", agent_id=client.agent_uuid,
                    timestamp="2026-09-24T00:01:00+00:00", reasoning=args["reasoning"],
                )
            )
        if action == "synthesis":
            return session.submit_synthesis(
                DialecticMessage(
                    phase="synthesis", agent_id=client.agent_uuid,
                    timestamp="2026-09-24T00:02:00+00:00", agrees=args["agrees"],
                    root_cause=args.get("root_cause"),
                    proposed_conditions=args.get("proposed_conditions"),
                    reasoning=args.get("reasoning"),
                )
            )
        if action == "get":
            if not state["paused_response_submitted"]:
                session.submit_synthesis(
                    DialecticMessage(
                        phase="synthesis", agent_id="paused-uuid",
                        timestamp="2026-09-24T00:03:00+00:00", agrees=True,
                        root_cause="verified", proposed_conditions=["ship evidence"],
                        reasoning="here is the evidence",
                    )
                )
                state["paused_response_submitted"] = True
            return {"success": True, **session.to_dict()}
        raise AssertionError(f"unexpected action: {args}")

    calls: list[tuple[str, dict]] = []
    _install_fake_client(monkeypatch, calls, on_call=on_call)

    verdict = await r.run(
        Thesis(session_id="sess-cont", root_cause="claimed", proposed_conditions=["initial"]),
        governance_url="http://localhost:8767",
        parent_agent_id="paused-uuid",
    )

    syntheses = [a for n, a in calls if n == "dialectic" and a["action"] == "synthesis"]
    assert len(syntheses) == 1, "the fallback approval was filed over the rejection"
    assert verdict.agrees is False
    assert verdict.proposed_conditions == ["supply evidence"]
