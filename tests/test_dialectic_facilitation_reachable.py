"""
A facilitation request must survive long enough to be answered, and answering
it must actually work.

The dead-end, as measured on the live DB 2026-08-10: all 38 sessions carrying
`awaiting_facilitation` were in phase `failed`. Every one had asked for a human
and been swept there by the 2h stuck-session timer, after which `reassign` —
the one operation that answers the request — refused the phase. The request was
visible and unanswerable at the same time.

Two defects, fixed together:

  1. `STUCK_SESSION_THRESHOLD` (2h) measures "this process is wedged". A
     session waiting on a person runs on a person's clock. `FACILITATION_TIMEOUT`
     (4h) already existed for this and was only reachable inside the ANTITHESIS
     branch — but every real facilitation event originates at THESIS, which fell
     straight through to FAILED.
  2. Reassigning a swept session left it `status='failed'`, so the new reviewer
     had no phase to act in and the sweeper re-terminated it on the next cycle.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.dialectic_protocol import DialecticMessage, DialecticPhase, DialecticSession
from src.mcp_handlers.dialectic import auto_resolve

AUTO = "src.mcp_handlers.dialectic.auto_resolve"


def _stuck_session(*, awaiting: bool, age_hours: float, phase: str = "thesis"):
    ts = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    return {
        "session_id": f"sess-{'await' if awaiting else 'plain'}-{age_hours}",
        "paused_agent_id": "agent-paused",
        "reviewer_agent_id": None,
        "phase": phase,
        "status": "active",
        "awaiting_facilitation": awaiting,
        "updated_at": ts,
        "created_at": ts,
    }


async def _sweep(sessions):
    """Run the sweeper over `sessions`, capturing terminal writes."""
    failed: list[str] = []

    async def _fake_status(session_id, status):
        failed.append(session_id)
        return True

    with patch(f"{AUTO}.get_active_sessions_async", new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO}.update_session_status_async", new=_fake_status), \
         patch(f"{AUTO}.add_message_async", new_callable=AsyncMock), \
         patch(f"{AUTO}.has_inflight_saga_async", new_callable=AsyncMock, return_value=False):
        await auto_resolve.auto_resolve_stuck_sessions()
    return failed


@pytest.mark.asyncio
async def test_facilitation_request_is_not_swept_on_the_stuck_timer():
    """3h old and waiting on a human: past the 2h stuck timer, inside the 4h one."""
    s = _stuck_session(awaiting=True, age_hours=3)
    failed = await _sweep([s])
    assert s["session_id"] not in failed, (
        "a session waiting on a person was failed by the stuck-process timer — "
        "this is what made all 38 requests unanswerable"
    )


@pytest.mark.asyncio
async def test_facilitation_request_still_fails_eventually():
    """The hold is a longer deadline, not an exemption — 5h is past FACILITATION_TIMEOUT."""
    s = _stuck_session(awaiting=True, age_hours=5)
    failed = await _sweep([s])
    assert s["session_id"] in failed, "the extended deadline must still be a deadline"


@pytest.mark.asyncio
async def test_ordinary_stuck_session_is_unaffected():
    """No facilitation request: the 2h stuck timer applies exactly as before."""
    s = _stuck_session(awaiting=False, age_hours=3)
    failed = await _sweep([s])
    assert s["session_id"] in failed


@pytest.mark.asyncio
async def test_reassign_revives_a_swept_facilitation_session():
    """
    Assigning a reviewer to a swept session must reopen it. Otherwise the
    request is answered on paper and dead in fact.
    """
    from src.mcp_handlers.dialectic.handlers import _apply_reviewer_reassignment

    session = DialecticSession(
        paused_agent_id="agent-paused",
        reviewer_agent_id=None,
        dispute_type="verification",
    )
    session.phase = DialecticPhase.FAILED
    session.awaiting_facilitation = True
    session.transcript.append(DialecticMessage(
        phase="thesis", agent_id="agent-paused",
        timestamp=datetime.now(timezone.utc).isoformat(),
        reasoning="my case", root_cause="rc",
    ))

    reopened: list[tuple[str, str]] = []

    async def _fake_reopen(session_id, phase):
        reopened.append((session_id, phase))
        return True

    D = "src.mcp_handlers.dialectic.handlers"
    with patch(f"{D}.beam_update_reviewer", new_callable=AsyncMock, return_value=None), \
         patch(f"{D}.pg_update_reviewer", new_callable=AsyncMock), \
         patch(f"{D}.pg_update_awaiting_facilitation", new_callable=AsyncMock), \
         patch(f"{D}.pg_reopen_session", new=_fake_reopen), \
         patch(f"{D}.pg_add_message", new_callable=AsyncMock):
        await _apply_reviewer_reassignment(
            "sess-revive", session, "new-reviewer", reason="operator facilitation",
        )

    # A thesis exists, so the session reopens where the reviewer can act.
    assert session.phase == DialecticPhase.ANTITHESIS
    assert session.awaiting_facilitation is False
    assert reopened == [("sess-revive", "antithesis")], (
        "the row must be reopened too — an in-memory phase change leaves "
        "status='failed' and the sweeper re-terminates it"
    )


@pytest.mark.asyncio
async def test_reassign_does_not_resurrect_an_ordinary_failed_session():
    """Revival is scoped to a standing request, not to failure in general."""
    from src.mcp_handlers.dialectic.handlers import _apply_reviewer_reassignment

    session = DialecticSession(
        paused_agent_id="agent-paused",
        reviewer_agent_id="old-reviewer",
        dispute_type="verification",
    )
    session.phase = DialecticPhase.FAILED
    session.awaiting_facilitation = False

    reopened = []

    async def _fake_reopen(session_id, phase):
        reopened.append(session_id)
        return True

    D = "src.mcp_handlers.dialectic.handlers"
    with patch(f"{D}.beam_update_reviewer", new_callable=AsyncMock, return_value=None), \
         patch(f"{D}.pg_update_reviewer", new_callable=AsyncMock), \
         patch(f"{D}.pg_update_awaiting_facilitation", new_callable=AsyncMock), \
         patch(f"{D}.pg_reopen_session", new=_fake_reopen), \
         patch(f"{D}.pg_add_message", new_callable=AsyncMock):
        await _apply_reviewer_reassignment(
            "sess-plain", session, "new-reviewer", reason="routine",
        )

    assert session.phase == DialecticPhase.FAILED
    assert reopened == []
