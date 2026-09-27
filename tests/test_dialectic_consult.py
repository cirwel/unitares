"""dialectic(action='consult'): an outside verdict filed as a record with no authority.

The route documented for an outside consult was an antithesis stamped
reviewer_kind='external_consult'. That needs the reviewer slot, which the
orchestrated reviewer takes within about a minute, so 0 of 136 sessions ever
carried one. A consult needs no slot, and the properties pinned here are the
ones that make it safe to allow anyone: it is never a verdict, never a phase
move, and never activity on the session's liveness clock.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.dialectic_protocol import DialecticMessage, DialecticPhase, DialecticSession
from src.mcp_handlers.dialectic import handlers as h
from tests.helpers import parse_result

DIALECTIC = "src.mcp_handlers.dialectic.handlers"
PROVENANCE = {"backend": "codex-cli", "model_used": "gpt-x", "consult_source": "pipeline review"}


def _session(phase=DialecticPhase.SYNTHESIS):
    session = DialecticSession(paused_agent_id="agent-paused", reviewer_agent_id="agent-reviewer")
    session.phase = phase
    return session


async def _consult(session, caller="agent-outside", insert_returns=77, **arguments):
    add_message = AsyncMock(return_value=insert_returns)
    h.ACTIVE_SESSIONS[session.session_id] = session
    try:
        with patch(f"{DIALECTIC}._resolve_dialectic_agent_id",
                   new=AsyncMock(return_value=(caller, None))), \
             patch(f"{DIALECTIC}.load_session", new=AsyncMock(return_value=None)), \
             patch(f"{DIALECTIC}.pg_add_bounded_message", add_message), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None):
            result = await h.handle_submit_consult({"session_id": session.session_id, **arguments})
    finally:
        h.ACTIVE_SESSIONS.pop(session.session_id, None)
    return parse_result(result), add_message


@pytest.mark.asyncio
async def test_a_third_party_files_a_consult_without_the_reviewer_slot():
    session = _session()
    data, add_message = await _consult(
        session, reasoning="The migration drops a column still read by the sweeper.",
        agrees=False, proposed_conditions=["keep the column one release"],
        reviewer_provenance=PROVENANCE,
    )

    assert data["success"] is True
    assert data["filer_role"] == "third_party"
    kwargs = add_message.await_args.kwargs
    assert kwargs["message_type"] == "consult"
    assert kwargs["max_of_type"] == h.MAX_CONSULTS_PER_SESSION
    # The bounded insert writes agrees NULL and never touches the session row.
    assert "agrees" not in kwargs
    stamp = kwargs["observed_metrics"]["reviewer_backend"]
    assert stamp["reviewer_kind"] == "external_consult"
    assert stamp["backend"] == "codex-cli"
    assert kwargs["observed_metrics"]["consult"] == {
        "position": "disagrees",
        "filer_role": "third_party",
        "session_phase_at_filing": "synthesis",
    }
    # Nothing about the review moved.
    assert session.phase == DialecticPhase.SYNTHESIS
    assert session.reviewer_agent_id == "agent-reviewer"


@pytest.mark.asyncio
async def test_the_kind_is_forced_whatever_the_caller_claims():
    data, add_message = await _consult(
        _session(), reasoning="r",
        reviewer_provenance={**PROVENANCE, "reviewer_kind": "orchestrated"},
    )
    assert data["success"] is True
    stamp = add_message.await_args.kwargs["observed_metrics"]["reviewer_backend"]
    assert stamp["reviewer_kind"] == "external_consult"


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments, missing", [
    ({"reviewer_provenance": PROVENANCE}, "reasoning"),
    ({"reasoning": "r"}, "reviewer_provenance"),
    ({"reasoning": "r", "reviewer_provenance": {}}, "reviewer_provenance"),
    # Review round 1 on #2540: keys the stamp drops would file a sourceless row.
    ({"reasoning": "r", "reviewer_provenance": {"reviewer_kind": "external_consult"}},
     "reviewer_provenance"),
    ({"reasoning": "r", "reviewer_provenance": {"bakend": "codex"}}, "reviewer_provenance"),
])
async def test_reasoning_and_provenance_are_required(arguments, missing):
    data, add_message = await _consult(_session(), **arguments)
    assert data["success"] is False
    assert missing in data["error"]
    add_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_closed_session_still_takes_a_consult():
    """Stranded objections are where an outside view is most useful."""
    data, _ = await _consult(_session(DialecticPhase.FAILED), reasoning="r",
                             reviewer_provenance=PROVENANCE)
    assert data["success"] is True


@pytest.mark.asyncio
async def test_the_paused_agent_may_file_one_and_is_labelled():
    data, _ = await _consult(_session(), caller="agent-paused", reasoning="council seat 2",
                             reviewer_provenance=PROVENANCE)
    assert data["filer_role"] == "paused_agent"


@pytest.mark.asyncio
async def test_a_full_session_refuses_and_records_nothing():
    """The bound is enforced in PostgreSQL; a None insert means full."""
    data, add_message = await _consult(_session(), insert_returns=None,
                                       reasoning="r", reviewer_provenance=PROVENANCE)
    assert data["success"] is False
    assert data.get("error_code") == "CONSULT_LIMIT"
    assert "Nothing was recorded" in data["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["approve", "agree", "ture", "1", 1])
async def test_an_unrecognised_position_is_refused_not_recorded_as_a_rejection(value):
    """Review round 1 on #2540."""
    data, add_message = await _consult(_session(), reasoning="r", agrees=value,
                                       reviewer_provenance=PROVENANCE)
    assert data["success"] is False
    assert data.get("error_code") == "INVALID_PARAM"
    add_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("value, position", [(True, "agrees"), ("False", "disagrees")])
async def test_true_and_false_are_the_positions(value, position):
    data, _ = await _consult(_session(), reasoning="r", agrees=value,
                             reviewer_provenance=PROVENANCE)
    assert data["position"] == position


@pytest.mark.asyncio
async def test_an_abstention_is_not_filed():
    """Review round 1 on #2540: no judgment, no consult."""
    data, add_message = await _consult(_session(), reasoning="model returned nothing",
                                       judgment_formed=False, reviewer_provenance=PROVENANCE)
    assert data["success"] is False
    assert data.get("error_code") == "NO_JUDGMENT"
    add_message.assert_not_awaited()


def test_a_consult_does_not_reset_the_protocol_clock():
    session = _session()
    session.transcript.append(DialecticMessage(
        phase="synthesis", agent_id="agent-reviewer",
        timestamp="2026-09-27T01:00:00+00:00", agrees=False))
    session.transcript.append(DialecticMessage(
        phase="consult", agent_id="agent-outside",
        timestamp="2026-09-27T05:00:00+00:00", reasoning="r"))
    assert session.get_last_update_timestamp().isoformat() == "2026-09-27T01:00:00+00:00"
    age_with = h._last_activity_age_s({"transcript": [m.to_dict() for m in session.transcript]})
    age_without = h._last_activity_age_s({"transcript": [session.transcript[0].to_dict()]})
    assert age_with is not None and age_without is not None
    assert abs(age_with - age_without) < 5


def test_a_consult_is_not_a_verdict_the_guard_reads():
    """A reviewer rejection still stands after an approving consult."""
    session = _session()
    session.transcript.append(DialecticMessage(
        phase="synthesis", agent_id="agent-reviewer",
        timestamp="2026-09-27T01:00:00+00:00", agrees=False))
    session.transcript.append(DialecticMessage(
        phase="consult", agent_id="agent-outside",
        timestamp="2026-09-27T02:00:00+00:00", reasoning="looks fine", agrees=None))
    assert session._reviewer_objection_stands() is True


@pytest.mark.parametrize("value", [1, 0, "1", "approve"])
def test_the_routed_schema_refuses_an_ambiguous_position_before_coercion(value):
    """Review on #2540: lax validation turned a JSON 1 into True before the
    handler saw it, so the handler-level check alone was bypassed."""
    from pydantic import ValidationError

    from src.mcp_handlers.schemas.dialectic import DialecticParams

    with pytest.raises(ValidationError, match="true or false"):
        DialecticParams.model_validate({"action": "consult", "session_id": "s", "agrees": value})


@pytest.mark.parametrize("value", [True, False, "TRUE", "false", None])
def test_the_routed_schema_accepts_the_two_positions(value):
    from src.mcp_handlers.schemas.dialectic import DialecticParams

    DialecticParams.model_validate({"action": "consult", "session_id": "s", "agrees": value})


def test_synthesis_keeps_its_existing_agrees_coercion():
    from src.mcp_handlers.schemas.dialectic import DialecticParams

    DialecticParams.model_validate({"action": "synthesis", "session_id": "s", "agrees": 1})
