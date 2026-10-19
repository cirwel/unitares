"""A routine request_review ack omits its canonical payload (#2443).

#2437 set the rule for lifecycle responses: a routine response carries what
the agent acts on, and anything abnormal keeps its full explanation. The
write acks (store_finding, update_finding, record_result) took it in #2439;
request_review still repeated its whole payload, including the agent
signature, under raw_governance on every call. These pin which request_review
acks are routine (an open review waiting on a known move), that each abnormal
one keeps the payload and says why, and what a routine ack still carries.

Kept apart from test_agent_experience_envelope.py so concurrent envelope work
appending to that file does not collide with this one.
"""

from __future__ import annotations

import json

import pytest

from src.mcp_handlers.middleware.envelope_step import (
    _ROUTINE_REVIEW_KEYS,
    build_experience_envelope,
)

_UUID = "660ba42a-baca-501c-ab2a-110f348bab92"
_SESSION = "8f1c2d3e4a5b6c7d"


def _signature(
    session_resolution_source: str = "explicit_client_session_id",
    proof_origin: str = "caller_asserted",
) -> dict:
    """agent_signature as success_response attaches it to the ack."""
    from src.services.identity_payloads import build_identity_signature_payload

    return build_identity_signature_payload(
        agent_uuid=_UUID,
        agent_id="Claude_Opus_20261019",
        display_name="probe",
        label_source="claimed",
        session_resolution_source=session_resolution_source,
        proof_origin=proof_origin,
    )


def _one_call_open_slot() -> dict:
    """The default one-call ack (use_brief_as_thesis=true): thesis recorded,
    no reviewer yet, slot open. The keys handle_submit_thesis and
    handle_request_dialectic_review set on that path, wrapped the way
    success_response wraps them."""
    return {
        "success": True,
        "server_time": "2026-10-19T00:00:00+00:00",
        "phase": "antithesis",
        "session_id": _SESSION,
        "next_step": (
            "An eligible reviewer should claim the session by submitting antithesis"
        ),
        "reviewed_work_authorship": {"declared": False, "author_agent_ids": []},
        "one_call_review": True,
        "thesis_source": "issue_description",
        "whose_move": (
            "a reviewer's — thesis recorded, awaiting a reviewer to claim the "
            "open slot"
        ),
        "agent_signature": _signature(),
    }


def _one_call_dispatched() -> dict:
    payload = _one_call_open_slot()
    payload.update({
        "orchestrated_review": True,
        "reviewer_dispatch": {
            "execution_id": "exec-1",
            "agent_id": "exec-1",
            "via": "agent-orchestrator",
        },
        "note": (
            "Independent reviewer spawned via the agent-orchestrator; it will "
            "claim the reviewer slot and submit its verdict. Poll dialectic "
            "action='get' for the antithesis/synthesis."
        ),
        "whose_move": (
            "the reviewer's — an independent reviewer was spawned; poll "
            f"dialectic(action='get', session_id='{_SESSION}') for its "
            "antithesis/synthesis"
        ),
    })
    return payload


def _two_call() -> dict:
    """use_brief_as_thesis=false: the session opens, the thesis is owed."""
    return {
        "success": True,
        "server_time": "2026-10-19T00:00:00+00:00",
        "message": "Dialectic session created",
        "session_id": _SESSION,
        "paused_agent_id": _UUID,
        "reviewer_agent_id": None,
        "awaiting_reviewer": True,
        "phase": "thesis",
        "session_type": "review",
        "reason": "Pressure-test the migration plan",
        "trigger_source": "agent_requested",
        "reviewed_work_authorship": {"declared": False, "author_agent_ids": []},
        "note": "Session created. Awaiting reviewer assignment.",
        "whose_move": "YOURS — your thesis is owed; the saved brief can be reused",
        "next_call": (
            f"dialectic(action='thesis', session_id='{_SESSION}', "
            "use_brief_as_thesis=true)"
        ),
        "agent_signature": _signature(),
    }


def _wire(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


@pytest.mark.parametrize("make", [_one_call_open_slot, _one_call_dispatched, _two_call])
def test_a_routine_review_ack_omits_the_canonical_payload(make):
    payload = make()
    env = build_experience_envelope("request_review", "dialectic", payload, {})

    assert "raw_governance" not in env
    assert env["response_shape"] == "routine"
    assert "response_shape_reason" not in env
    # The id the caller passes to every later dialectic call survives.
    assert env["state_summary"]["session_id"] == _SESSION
    assert env["state_summary"]["phase"] == payload["phase"]
    assert env["state_summary"]["whose_move"] == payload["whose_move"]
    assert env["next_action"]
    # The read the hint names returns the session, not this ack, so it is
    # not advertised as a re-fetch of the omitted payload.
    assert "raw_governance_available" not in env
    assert f"session_id='{_SESSION}'" in env["raw_governance_hint"]
    assert "action='get'" in env["raw_governance_hint"]
    # The signature was the bulk of the old ack; who opened the review stays.
    assert env["agent_uuid"] == _UUID
    assert env["written_as"]["caller_proven"] is True
    assert "identity_context" not in json.dumps(env)
    assert _wire(env) < _wire(payload)


def test_a_dispatched_reviewer_stays_visible_on_a_routine_ack():
    env = build_experience_envelope(
        "request_review", "dialectic", _one_call_dispatched(), {}
    )
    assert env["state_summary"]["orchestrated_review"] is True
    assert env["state_summary"]["reviewer_dispatch"]["via"] == "agent-orchestrator"


def test_a_two_call_ack_names_the_thesis_call():
    env = build_experience_envelope("request_review", "dialectic", _two_call(), {})
    assert "use_brief_as_thesis=true" in env["next_action"]
    assert _SESSION in env["next_action"]


def test_a_routine_review_ack_fits_its_measured_size():
    """Measured on the fixture above (2026-10-19): the envelope that kept
    raw_governance was 1,562 B with a caller-proven signature and 2,888 B
    with a server-inferred one (the live #2443 reading was about 3.0 KB);
    the routine envelope is 896 B either way, 93 B of it the size receipt.
    A ceiling, not a target."""
    payload = _one_call_open_slot()
    env = build_experience_envelope("request_review", "dialectic", payload, {})
    assert _wire(env) <= 1_100, (_wire(env), env)


_RESOLVED_SYNTHETIC = {
    "phase": "resolved",
    "synthetic_review": True,
    "reviewer_agent_id": "synthetic-reviewer",
    "antithesis": {"concerns": ["c"], "counter_reasoning": "r",
                   "grounding_cited": "", "position": "p"},
    "synthesis": {"agreed_root_cause": "x", "merged_conditions": ["m"],
                  "reasoning": "y"},
    "recommendation": "RESUME",
    "resolved": True,
    "review_verdict": "resolved",
    "next_steps": ["proceed"],
    "whose_move": "nobody — review resolved in this call",
}


@pytest.mark.parametrize("change,reason_part", [
    # The review itself came back in this call.
    (_RESOLVED_SYNTHETIC, "synthesis"),
    # Recused: the thesis is about the review system; a human decides.
    ({"recusal": {"reason": "subject_matter"}, "awaiting_facilitation": True},
     "recusal"),
    # Every automated reviewer path failed.
    ({"awaiting_facilitation": True}, "awaiting_facilitation"),
    # Only listed hosts may approve, and the list could not be resolved.
    ({"reviewer_hosts_error": "unresolved"}, "reviewer_hosts_error"),
    # The caller's arguments were auto-corrected.
    ({"_param_coercions": {"applied": {}, "note": "n"}}, "_param_coercions"),
    # A key nobody has classified yet keeps the payload by default.
    ({"some_future_signal": 1}, "some_future_signal"),
    # A terminal phase is the review's own content.
    ({"phase": "failed"}, "phase:failed"),
    ({"phase": None}, "phase:missing"),
    # A producer regression that loses the session id is not trimmed.
    ({"session_id": None}, "no_session_id"),
])
def test_an_abnormal_review_ack_keeps_the_payload_and_says_why(change, reason_part):
    payload = _one_call_open_slot()
    payload.update(change)
    env = build_experience_envelope("request_review", "dialectic", payload, {})
    assert env["raw_governance"] is payload
    assert env["response_shape"] == "full"
    assert reason_part in env["response_shape_reason"]
    assert "raw_governance_hint" not in env
    assert "written_as" not in env


def test_a_skipped_review_keeps_the_payload():
    """waiting_input: no session was opened, so there is nothing routine."""
    payload = {
        "success": True,
        "skipped": True,
        "reason": "Agent is waiting_input; not stuck",
        "agent_id": _UUID,
        "status": "waiting_input",
        "recommendation": "No dialectic needed.",
    }
    env = build_experience_envelope("request_review", "dialectic", payload, {})
    assert env["raw_governance"] is payload
    assert env["response_shape"] == "full"


@pytest.mark.asyncio
async def test_a_failed_thesis_passes_through_whole():
    """#1414: the session row committed but the thesis was not recorded. The
    handler returns success=false, so the envelope never sees it."""
    from mcp.types import TextContent

    from src.mcp_handlers.middleware import DispatchContext
    from src.mcp_handlers.middleware.envelope_step import apply_experience_envelope

    payload = {
        "success": False,
        "error": "Thesis requires root_cause",
        "session_id": _SESSION,
        "thesis_recorded": False,
        "session_created": True,
    }
    result = [TextContent(type="text", text=json.dumps(payload))]
    out = await apply_experience_envelope(
        "dialectic", {}, DispatchContext(original_name="request_review"), result
    )
    assert json.loads(out[0].text) == payload


def test_the_routine_allowlist_names_no_review_content():
    """The keys that carry the review itself must never be routine."""
    for key in (
        "antithesis", "synthesis", "recommendation", "resolution", "resolved",
        "review_verdict", "recusal", "awaiting_facilitation", "synthetic_review",
        "thesis_recorded", "session_created", "effect_pending",
        "terminal_committed", "skipped",
    ):
        assert key not in _ROUTINE_REVIEW_KEYS, key
