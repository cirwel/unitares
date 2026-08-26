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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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


@pytest.mark.asyncio
async def test_revival_persistence_order_reopen_first():
    """Council 2026-08-21: the revival writes must run reopen -> reviewer ->
    clear-awaiting. The old order (reviewer, clear, reopen) both welded the
    reopen shut (its SQL requires awaiting_facilitation=true, already
    cleared) and hit the reviewer terminal-guard on the still-failed row."""
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

    calls: list[str] = []

    async def _reopen(session_id, phase):
        calls.append("reopen")
        return True

    async def _reviewer(session_id, reviewer_id):
        calls.append("reviewer")
        return True

    async def _awaiting(session_id, awaiting):
        calls.append("awaiting")
        return True

    D = "src.mcp_handlers.dialectic.handlers"
    with patch(f"{D}.beam_update_reviewer", new_callable=AsyncMock, return_value=None), \
         patch(f"{D}.pg_update_reviewer", new=_reviewer), \
         patch(f"{D}.pg_update_awaiting_facilitation", new=_awaiting), \
         patch(f"{D}.pg_reopen_session", new=_reopen), \
         patch(f"{D}.pg_add_message", new_callable=AsyncMock):
        await _apply_reviewer_reassignment(
            "sess-order", session, "new-reviewer", reason="operator facilitation",
        )

    assert calls == ["reopen", "reviewer", "awaiting"], (
        f"revival persistence order regressed: {calls}"
    )


@pytest.mark.asyncio
async def test_revival_refused_reviewer_write_raises_under_strict():
    """A refused reviewer write (row terminal/missing) must surface as a
    failure under strict_persistence, never as a silent 'Reviewer
    reassigned' success."""
    from src.mcp_handlers.dialectic.handlers import _apply_reviewer_reassignment

    session = DialecticSession(
        paused_agent_id="agent-paused",
        reviewer_agent_id="old-reviewer",
        dispute_type="verification",
    )

    D = "src.mcp_handlers.dialectic.handlers"
    with patch(f"{D}.beam_update_reviewer", new_callable=AsyncMock, return_value=None), \
         patch(f"{D}.pg_update_reviewer", new_callable=AsyncMock, return_value=False), \
         patch(f"{D}.pg_update_awaiting_facilitation", new_callable=AsyncMock), \
         patch(f"{D}.pg_reopen_session", new_callable=AsyncMock, return_value=True), \
         patch(f"{D}.pg_add_message", new_callable=AsyncMock):
        with pytest.raises(RuntimeError):
            await _apply_reviewer_reassignment(
                "sess-refused", session, "new-reviewer",
                reason="race", strict_persistence=True,
            )


# --- Self-review authority (#1585 item 1) ---------------------------------
#
# Conditions set by the governed review of the guard itself (dialectic session
# cfb3f0085a4d5c06, reviewer chatgpt-codex_fb8d5918, 2026-08-26).


@pytest.mark.asyncio
async def test_the_transition_owner_refuses_a_self_authorized_resume():
    """The last authoritative check, not the handler, is the safety boundary.

    `execute_resolution` owns the paused→active mutation and reads the status
    it is about to act on, so the handler's earlier read cannot go stale
    underneath it.
    """
    from src.mcp_handlers.dialectic import resolution as res

    session = DialecticSession(
        paused_agent_id="agent-a", reviewer_agent_id="agent-a",
        dispute_type="verification",
    )
    meta = SimpleNamespace(status="paused", api_key="k")
    server = MagicMock()
    server.load_metadata_async = AsyncMock()
    server.agent_metadata = {"agent-a": meta}

    with patch.object(res, "mcp_server", server):
        out = await res.execute_resolution(session, SimpleNamespace(conditions=[], root_cause="rc"))

    assert out["success"] is False
    assert out["refused"] == "self_review_not_authorizing"
    assert meta.status == "paused", "the agent must not have been resumed"


@pytest.mark.asyncio
async def test_the_transition_owner_leaves_independent_resumes_alone():
    """The refusal keys on the conflicted identity, not on resuming as such."""
    from src.mcp_handlers.dialectic import resolution as res

    session = DialecticSession(
        paused_agent_id="agent-a", reviewer_agent_id="agent-b",
        dispute_type="verification",
    )
    server = MagicMock()
    server.load_metadata_async = AsyncMock()
    # MagicMock, not SimpleNamespace: this path runs past the authority check
    # into the real resume, which calls metadata methods this test does not care
    # about. The assertion is only that the authority check let it through.
    live_meta = MagicMock()
    live_meta.status = "paused"
    server.agent_metadata = {"agent-a": live_meta}

    with patch.object(res, "mcp_server", server), \
         patch.object(res, "parse_condition", side_effect=lambda c: c), \
         patch.object(res, "apply_condition", new_callable=AsyncMock,
                      return_value={"status": "applied"}), \
         patch.object(res, "_record_outcome_event_inline", new_callable=AsyncMock):
        out = await res.execute_resolution(
            session,
            SimpleNamespace(conditions=[], root_cause="rc", hash=lambda: "sha-test"),
        )

    assert out.get("refused") is None


@pytest.mark.asyncio
async def test_reassignment_rewinds_a_refused_self_review_to_antithesis():
    """The incoming independent reviewer must actually get a turn.

    Left at SYNTHESIS, the only antithesis on the record is the conflicted
    identity's, so `_reviewer_verdict_pending` never holds the paused agent and
    it could resolve with the new reviewer never having spoken.
    """
    from src.mcp_handlers.dialectic.handlers import _apply_reviewer_reassignment

    session = DialecticSession(
        paused_agent_id="agent-a", reviewer_agent_id="agent-a",
        dispute_type="verification",
    )
    session.phase = DialecticPhase.SYNTHESIS
    session.awaiting_facilitation = True
    session.transcript.append(DialecticMessage(
        phase="thesis", agent_id="agent-a",
        timestamp=datetime.now(timezone.utc).isoformat(),
        reasoning="my case", root_cause="rc",
    ))

    D = "src.mcp_handlers.dialectic.handlers"
    with patch(f"{D}.beam_update_reviewer", new_callable=AsyncMock, return_value=None), \
         patch(f"{D}.pg_update_reviewer", new_callable=AsyncMock), \
         patch(f"{D}.pg_update_awaiting_facilitation", new_callable=AsyncMock), \
         patch(f"{D}.pg_reopen_session", new_callable=AsyncMock, return_value=True), \
         patch(f"{D}.pg_add_message", new_callable=AsyncMock):
        await _apply_reviewer_reassignment(
            "sess-self", session, "agent-b", reason="operator facilitation",
        )

    assert session.phase == DialecticPhase.ANTITHESIS
    assert session.reviewer_agent_id == "agent-b"
    assert session.awaiting_facilitation is False
    # The conflicted identity's messages stay as attributed audit evidence.
    assert any(m.agent_id == "agent-a" for m in session.transcript)
