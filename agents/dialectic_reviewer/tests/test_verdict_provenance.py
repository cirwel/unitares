"""Reviewer verdicts must record which model produced them.

The gap this closes: provenance was computed on every review and written only
into the check-in's ``response_text``, which is not persisted (3 of 30,063
agent_state rows in 30 days; 0 of 4.18M audit events carried the reviewer's
audit line). So 194 live antithesis/synthesis rows over 90 days recorded
neither the model nor whether a fallback fired.

That distinction is not cosmetic: replaying 14 real theses (2026-08-18) put
local-model verdicts 36-50% apart from the deployed codex reviewer's, so a
selected-host verdict and a degraded-fallback verdict are materially different
objects.

``signature`` is NOT an available slot — it is the protocol's HMAC attestation.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType
from unittest.mock import AsyncMock, patch

from agents.dialectic_reviewer import reviewer as r

FULL_PROVENANCE = {
    "backend": "external",
    "host_id": "external:generativelanguage.googleapis.com",
    "model_requested": "some-model-id",
    "model_used": "served-002",
    "models_used": ["served-002"],
    "tokens_used": 321,
    "cost_usd": 0.004,
    "latency_ms": 1234,
    "finish_reason": "stop",
    "fallback_from": None,
    "warnings": [],
}


# --------------------------------------------------------------------------- #
# The payload itself
# --------------------------------------------------------------------------- #
def test_persisted_provenance_keeps_the_attribution_fields():
    stored = r._provenance_for_message(FULL_PROVENANCE, degraded=False)
    assert stored["backend"] == "external"
    assert stored["host_id"] == "external:generativelanguage.googleapis.com"
    assert stored["model_used"] == "served-002"
    assert stored["models_used"] == ["served-002"]
    assert stored["tokens_used"] == 321
    assert stored["degraded"] is False


def test_fallback_is_recorded_when_one_fired():
    provenance = {
        "backend": "ollama",
        "host_id": "ollama:local",
        "models_used": ["gemma4:latest"],
        "fallback_from": "codex:host-adapter",
        "warnings": ["Codex backend unavailable or returned no verdict"],
    }
    stored = r._provenance_for_message(provenance, degraded=True)
    assert stored["fallback_from"] == "codex:host-adapter"
    assert stored["degraded"] is True
    # A degraded verdict must be distinguishable from a selected-host one.
    assert stored != r._provenance_for_message(FULL_PROVENANCE, degraded=False)


def test_unknown_provenance_fields_are_not_persisted():
    """Allowlist, not denylist — a future backend must not be able to leak a
    field into the governance ledger by adding it to its provenance dict."""
    stored = r._provenance_for_message(
        {**FULL_PROVENANCE, "api_key": "sk-secret", "raw_response": "..."},
        degraded=False,
    )
    assert "api_key" not in stored
    assert "raw_response" not in stored
    assert "sk-secret" not in json.dumps(stored)


def test_none_valued_fields_are_dropped_not_stored_as_null():
    stored = r._provenance_for_message(FULL_PROVENANCE, degraded=False)
    assert "fallback_from" not in stored  # it was None on this path


def test_payload_is_json_serializable():
    """It lands in a jsonb column; a non-serializable value would fail the write
    at submit time, i.e. lose the verdict rather than just the provenance."""
    json.dumps(r._provenance_for_message(FULL_PROVENANCE, degraded=False))


# --------------------------------------------------------------------------- #
# The submission actually carries it
# --------------------------------------------------------------------------- #
def _run_reviewer_capturing_calls(provenance, verdict_text, *, prompts=None):
    """Drive run() far enough to capture the antithesis submission.

    ``verdict_text`` may be a single reply (returned for every call) or a list
    of replies served in order, which is how the one repair attempt is
    exercised. Pass ``prompts`` to capture the prompts run() actually sent.
    """
    calls = []
    replies = list(verdict_text) if isinstance(verdict_text, list) else None

    class FakeClient:
        agent_uuid = "reviewer-uuid"

        def __init__(self, *a, **kw):
            pass

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def onboard(self, *a, **kw):
            return {"uuid": self.agent_uuid}

        async def call_tool(self, name, args):
            calls.append((name, args))
            return {"success": True}

        async def checkin(self, **kw):
            return {"success": True}

    thesis = r.Thesis(session_id="s1", root_cause="rc", reasoning="why")

    async def fake_obtain(prompt):
        r._record_reviewer_provenance(provenance)
        if prompts is not None:
            prompts.append(prompt)
        if replies is None:
            return verdict_text
        return replies.pop(0) if replies else ""

    # unitares_sdk is imported lazily inside run(); stub it so the test needs
    # no SDK install and no network.
    sdk = ModuleType("unitares_sdk")
    sdk_client = ModuleType("unitares_sdk.client")
    sdk_client.GovernanceClient = FakeClient

    with patch.object(r, "obtain_reviewer_text", side_effect=fake_obtain):
        with patch.object(r, "continue_after_disagreement", new=AsyncMock()):
            with patch.dict(
                sys.modules,
                {"unitares_sdk": sdk, "unitares_sdk.client": sdk_client},
            ):
                asyncio.run(r.run(thesis, "http://localhost:8767", None))
    return calls


def test_antithesis_submission_carries_reviewer_backend():
    calls = _run_reviewer_capturing_calls(
        FULL_PROVENANCE, '{"agrees": false, "reasoning": "no"}'
    )
    antithesis = [args for name, args in calls if args.get("action") == "antithesis"]
    assert antithesis, "reviewer submitted no antithesis"
    stored = antithesis[0]["observed_metrics"]["reviewer_backend"]
    assert stored["backend"] == "external"
    assert stored["model_used"] == "served-002"


DEGRADED_PROVENANCE = {
    "backend": "ollama",
    "host_id": "ollama:local",
    "models_used": ["gemma4:latest"],
    "fallback_from": "codex:host-adapter",
}


def test_antithesis_provenance_survives_a_degraded_fallback():
    """A fallback backend that STILL judges must record that it was a fallback.

    This is the half of the original contract that survives the abstention
    rule: the verdict is real, so it is filed, and the record has to show it
    came from the free local model rather than the selected host — replaying 14
    real theses put those 36-50% apart.

    ``degraded`` is False here on purpose. It describes the VERDICT (could we
    extract a judgment), not the BACKEND (which host answered). A fallback that
    produced a parseable judgment is an intact verdict from a weaker model, and
    ``fallback_from`` is what says so.
    """
    calls = _run_reviewer_capturing_calls(
        DEGRADED_PROVENANCE, '{"agrees": false, "root_cause": "shallow", "reasoning": "no"}'
    )
    antithesis = [args for name, args in calls if args.get("action") == "antithesis"]
    assert antithesis, "a fallback backend that judged must still file its verdict"
    stored = antithesis[0]["observed_metrics"]["reviewer_backend"]
    assert stored["fallback_from"] == "codex:host-adapter"
    assert stored["degraded"] is False


def test_no_parseable_judgment_files_nothing_at_all():
    """THE LIVE INCIDENT, pinned: 2026-09-19, session 99ff6f25a310d23e, PR #2316.

    codex was unavailable, the fallback gemma4 returned nothing parseable, and
    the old code filed that non-answer as a BINDING rejection with empty
    reasoning — claiming the reviewer slot, blocking the paused agent, and
    locking out an independent reviewer that arrived four minutes later holding
    a reproduced counterexample.

    A reviewer that could not judge has not reviewed. It must file NOTHING, so
    the slot stays open for one that can. Fail-closed means "no approval", not
    "silent rejection".
    """
    calls = _run_reviewer_capturing_calls(
        DEGRADED_PROVENANCE, "not json at all"  # exactly what gemma4 returned
    )
    assert calls == [], (
        "a reviewer with no judgment filed something anyway: "
        f"{[args.get('action') for _, args in calls]}"
    )


def test_submission_does_not_touch_signature():
    """signature is the protocol's HMAC attestation, not a provenance slot."""
    calls = _run_reviewer_capturing_calls(
        FULL_PROVENANCE, '{"agrees": true, "reasoning": "ok", "proposed_conditions": ["c"]}'
    )
    for _, args in calls:
        assert "signature" not in args


# ----------------- one repair attempt before abstaining (Q5) ---------------- #
def test_a_formatting_slip_is_repaired_rather_than_abstained_on():
    """A model that judged but botched the envelope must not cost a review.

    Raised by independent review on 2026-09-19: abstaining on a single
    unparseable reply treats a transient formatting failure as proof that no
    judgment could be formed. It is not.
    """
    prompts: list[str] = []
    calls = _run_reviewer_capturing_calls(
        DEGRADED_PROVENANCE,
        [
            "Sure! Here is my review: the conditions look shallow.",  # no JSON
            '{"agrees": false, "root_cause": "shallow", "reasoning": "no"}',
        ],
        prompts=prompts,
    )
    antithesis = [args for name, args in calls if args.get("action") == "antithesis"]
    assert antithesis, "the repaired verdict was thrown away"
    synthesis = [args for name, args in calls if args.get("action") == "synthesis"]
    assert synthesis and synthesis[0]["agrees"] is False
    assert synthesis[0]["root_cause"] == "shallow"

    # The repair must RE-ASK for the same judgment, not invite a fresh one —
    # re-running the original prompt would be quiet reviewer-shopping.
    assert len(prompts) == 2
    assert "COULD NOT BE PARSED" in prompts[1]
    assert "do NOT change your position" in prompts[1]


def test_abstention_survives_a_failed_repair_and_is_bounded_to_one():
    """Still abstains when the repair also fails — and re-asks exactly once."""
    prompts: list[str] = []
    calls = _run_reviewer_capturing_calls(
        DEGRADED_PROVENANCE, ["prose", "still prose"], prompts=prompts
    )
    assert calls == [], f"filed anyway: {[a.get('action') for _, a in calls]}"
    assert len(prompts) == 2, (
        f"expected exactly one repair attempt, got {len(prompts) - 1}"
    )


def test_a_repair_may_not_flip_a_rejection_into_an_approval():
    """The repair prompt ASKS for the same position; it cannot enforce it.

    Found by independent review of this PR (codex, 2026-09-19). The reply being
    restated is unparseable by construction, so a first reply that rejected in
    prose followed by a parseable ``agrees: true`` would file an approval no one
    can verify was ever the model's judgment — and an approval can resolve the
    session and release the paused agent.

    Approval is the one direction that must never rest on an unverifiable
    restatement, so this abstains instead. Losing a genuine approval that merely
    botched its format is the correct direction to fail.
    """
    prompts: list[str] = []
    calls = _run_reviewer_capturing_calls(
        DEGRADED_PROVENANCE,
        [
            "I reject this: the conditions are shallow and miss the root cause.",
            '{"agrees": true, "root_cause": "fine", '
            '"proposed_conditions": ["ship it"], "reasoning": "looks ok"}',
        ],
        prompts=prompts,
    )
    assert calls == [], (
        "a repair manufactured an approval: "
        f"{[args.get('action') for _, args in calls]}"
    )
    assert len(prompts) == 2, "the repair attempt did not run"


def test_a_repair_that_restates_an_objection_is_still_accepted():
    """The safe direction must keep working — this is not a ban on repairs."""
    calls = _run_reviewer_capturing_calls(
        DEGRADED_PROVENANCE,
        [
            "I reject this, the root cause is shallow.",
            '{"agrees": false, "root_cause": "shallow", "reasoning": "no"}',
        ],
    )
    synthesis = [args for name, args in calls if args.get("action") == "synthesis"]
    assert synthesis and synthesis[0]["agrees"] is False
    assert synthesis[0]["root_cause"] == "shallow"
