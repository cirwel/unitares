"""
Tests for src/mcp_handlers/dialectic/auto_resolve.py

Tests auto-resolution of stuck dialectic sessions, including
reviewer re-assignment and awaiting_facilitation behavior.
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

AUTO_RESOLVE = "src.mcp_handlers.dialectic.auto_resolve"


@pytest.fixture(autouse=True)
def _no_inflight_saga():
    """Default the C1 saga-inflight guard to False (no BEAM saga in flight).

    The sweeper now calls has_inflight_saga_async before touching a session;
    these tests exercise the no-saga path. The guard behavior itself is covered
    in test_dialectic_sweeper_saga_guard.py.
    """
    with patch(f"{AUTO_RESOLVE}.has_inflight_saga_async",
               new_callable=AsyncMock, return_value=False), \
         patch(f"{AUTO_RESOLVE}.emit_sweep_cycle", new_callable=AsyncMock):
        yield


def _make_mock_server(agents=None):
    mock = MagicMock()
    mock.agent_metadata = agents or {}
    mock.load_metadata_async = AsyncMock()
    return mock


def _make_agent_meta(status="active"):
    return SimpleNamespace(status=status, tags=[], last_update=datetime.now().isoformat())


def _old_time(hours=3):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _recent_time(minutes=5):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


# --- Basic auto_resolve Tests ---


@pytest.mark.asyncio
async def test_no_active_sessions():
    """Should return 0 resolved when no active sessions."""
    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=[]):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    assert result["resolved_count"] == 0
    assert "No active sessions" in result["message"]


@pytest.mark.asyncio
async def test_no_stuck_sessions():
    """Active sessions that are recent should not be resolved."""
    sessions = [
        {"session_id": "s1", "updated_at": _recent_time(), "paused_agent_id": "a1", "phase": "thesis"}
    ]

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    assert result["resolved_count"] == 0
    assert "No stuck sessions" in result["message"]


@pytest.mark.asyncio
async def test_sweep_prioritizes_oldest_rows_and_reports_a_full_batch():
    """A 100-row cap must not silently select the newest active sessions.

    The 101st row is an overflow sentinel. It proves the scanned count is a
    bounded batch rather than the complete active-session denominator.
    """
    sessions = [
        {
            "session_id": f"recent-{index}",
            "updated_at": _recent_time(),
            "paused_agent_id": f"agent-{index}",
            "phase": "thesis",
        }
        for index in range(101)
    ]
    fetch = AsyncMock(return_value=sessions)
    emitted = AsyncMock()

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async", fetch), \
         patch(f"{AUTO_RESOLVE}.emit_sweep_cycle", emitted):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions(trigger_source="periodic")

    fetch.assert_awaited_once_with(
        limit=101,
        least_recently_updated_first=True,
    )
    assert result["active_session_count"] == 100
    assert result["active_session_batch_truncated"] is True
    assert result["stuck_session_count"] == 0
    assert emitted.await_args.kwargs["active_session_batch_truncated"] is True


@pytest.mark.asyncio
async def test_resolves_stuck_thesis_session():
    """Sessions in thesis phase inactive for >2h should be marked FAILED (no reviewer to reassign)."""
    sessions = [
        {"session_id": "stuck-1", "updated_at": _old_time(5), "paused_agent_id": "a1",
         "phase": "thesis", "reviewer_agent_id": None}
    ]

    mock_update = AsyncMock()
    mock_add_msg = AsyncMock()

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.update_session_status_async", mock_update), \
         patch(f"{AUTO_RESOLVE}.add_message_async", mock_add_msg):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    assert result["resolved_count"] == 1
    mock_update.assert_called_once_with("stuck-1", "failed")


@pytest.mark.asyncio
async def test_antithesis_reassigns_reviewer_when_gone():
    """Stuck antithesis session with gone reviewer should try reassignment."""
    sessions = [
        {"session_id": "s1", "updated_at": _old_time(3), "paused_agent_id": "a1",
         "phase": "antithesis", "reviewer_agent_id": "gone-reviewer"}
    ]

    server = _make_mock_server({
        "a1": _make_agent_meta(status="paused"),
        "new-reviewer": _make_agent_meta(status="active"),
        # "gone-reviewer" NOT in metadata
    })

    mock_update_reviewer = AsyncMock()
    mock_add_msg = AsyncMock()
    mock_select = AsyncMock(return_value="new-reviewer")
    mock_emit = AsyncMock()

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.mcp_server", server), \
         patch(f"{AUTO_RESOLVE}.update_session_reviewer_async", mock_update_reviewer), \
         patch(f"{AUTO_RESOLVE}.add_message_async", mock_add_msg), \
         patch(f"{AUTO_RESOLVE}.emit_reviewer_reassigned", mock_emit), \
         patch("src.mcp_handlers.dialectic.reviewer.select_reviewer", mock_select):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    assert result["reassigned_count"] == 1
    assert result["resolved_count"] == 0  # Not failed
    mock_update_reviewer.assert_called_once_with("s1", "new-reviewer")

    # ⛔The (F) reassignment-rate baseline is computed from this event. The
    # sweeper emitted NOTHING until 2026-08-22 while handlers.py called the
    # other producer "the single chokepoint". Assert behaviour, not file text.
    mock_emit.assert_awaited_once()
    assert mock_emit.await_args.kwargs == {
        "session_id": "s1",
        "old_reviewer_id": "gone-reviewer",
        "new_reviewer_id": "new-reviewer",
        "reason": "reviewer_unresponsive",
        "source": "sweeper",
    }


@pytest.mark.asyncio
async def test_reassignment_recorded_even_if_transcript_append_fails():
    """A committed reassignment must reach the (F) stream even if narration fails.

    Regression for a 2026-08-22 review finding: the emit sat after
    ``add_message_async`` inside one try whose except has no ``continue``, so a
    transcript failure on an already-committed write fell through to the
    facilitation branch — no event, no count, and a facilitation message naming
    the stale reviewer.
    """
    sessions = [
        {"session_id": "s1", "updated_at": _old_time(3), "paused_agent_id": "a1",
         "phase": "antithesis", "reviewer_agent_id": "gone-reviewer"}
    ]
    server = _make_mock_server({
        "a1": _make_agent_meta(status="paused"),
        "new-reviewer": _make_agent_meta(status="active"),
    })
    mock_emit = AsyncMock()

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.mcp_server", server), \
         patch(f"{AUTO_RESOLVE}.update_session_reviewer_async", AsyncMock(return_value=True)), \
         patch(f"{AUTO_RESOLVE}.add_message_async",
               AsyncMock(side_effect=RuntimeError("transcript down"))), \
         patch(f"{AUTO_RESOLVE}.emit_reviewer_reassigned", mock_emit), \
         patch("src.mcp_handlers.dialectic.reviewer.select_reviewer",
               AsyncMock(return_value="new-reviewer")):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    mock_emit.assert_awaited_once()
    assert result["reassigned_count"] == 1
    assert result["facilitation_count"] == 0, (
        "a committed reassignment must not fall through to the facilitation branch"
    )


@pytest.mark.asyncio
async def test_antithesis_awaits_facilitation_when_no_candidates():
    """Stuck antithesis with no replacement should await facilitation (not fail immediately).

    The request must be PERSISTED, not only narrated. Until 2026-08-26 this
    branch appended the transcript message and counted a facilitation while
    `awaiting_facilitation` stayed false in the row — and every reader that
    answers a facilitation request (`reopen_session`,
    `_apply_reviewer_reassignment`) keys on that flag, so the request was
    visible in the transcript and unanswerable in the database.
    """
    # Session is 2.5 hours old (past threshold but under facilitation timeout of 4h)
    sessions = [
        {"session_id": "s1", "updated_at": _old_time(2.5), "paused_agent_id": "a1",
         "phase": "antithesis", "reviewer_agent_id": "gone-reviewer"}
    ]

    server = _make_mock_server({
        "a1": _make_agent_meta(status="paused"),
        # No other agents available
    })

    mock_update_status = AsyncMock()
    mock_add_msg = AsyncMock()
    mock_mark = AsyncMock(return_value=True)
    mock_emit = AsyncMock()
    mock_select = AsyncMock(return_value=None)

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.mcp_server", server), \
         patch(f"{AUTO_RESOLVE}.update_session_status_async", mock_update_status), \
         patch(f"{AUTO_RESOLVE}.mark_awaiting_facilitation_async", mock_mark), \
         patch(f"{AUTO_RESOLVE}.emit_facilitation_needed", mock_emit), \
         patch(f"{AUTO_RESOLVE}.add_message_async", mock_add_msg), \
         patch("src.mcp_handlers.dialectic.reviewer.select_reviewer", mock_select):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    assert result["facilitation_count"] == 1
    assert result["resolved_count"] == 0  # NOT failed yet
    mock_update_status.assert_not_called()  # Should not mark as failed
    mock_mark.assert_awaited_once_with("s1")
    # Every other writer of this flag announces it; a request nobody is told
    # about is one the operator has to go looking for.
    assert mock_emit.await_args.kwargs["session_id"] == "s1"
    assert mock_emit.await_args.kwargs["reason"] == "reviewer_unresponsive"


@pytest.mark.asyncio
async def test_facilitation_request_is_recorded_once_not_once_per_cycle():
    """A session already awaiting facilitation is held, not re-narrated.

    `add_message` writes to dialectic_messages and leaves
    dialectic_sessions.updated_at alone, so a row that asked for a human keeps
    looking stuck — deliberately, since that is what keeps `select_reviewer`
    retrying while a human is waited on. Re-entering this branch must therefore
    cost nothing: no second message, no second count, and no second event.
    """
    sessions = [
        {"session_id": "s1", "updated_at": _old_time(2.5), "paused_agent_id": "a1",
         "phase": "antithesis", "reviewer_agent_id": "gone-reviewer",
         "awaiting_facilitation": True}
    ]

    server = _make_mock_server({"a1": _make_agent_meta(status="paused")})

    mock_update_status = AsyncMock()
    mock_add_msg = AsyncMock()
    mock_mark = AsyncMock(return_value=True)

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.mcp_server", server), \
         patch(f"{AUTO_RESOLVE}.update_session_status_async", mock_update_status), \
         patch(f"{AUTO_RESOLVE}.mark_awaiting_facilitation_async", mock_mark), \
         patch(f"{AUTO_RESOLVE}.emit_facilitation_needed", new_callable=AsyncMock), \
         patch(f"{AUTO_RESOLVE}.add_message_async", mock_add_msg), \
         patch("src.mcp_handlers.dialectic.reviewer.select_reviewer",
               new_callable=AsyncMock, return_value=None):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    assert result["facilitation_count"] == 0, "the request was already recorded"
    assert result["resolved_count"] == 0, "the operator's 4h clock is still running"
    mock_mark.assert_not_awaited()
    mock_add_msg.assert_not_awaited()
    mock_update_status.assert_not_called()


@pytest.mark.asyncio
async def test_refused_facilitation_write_is_not_narrated():
    """A refused flag write means another writer finished the session.

    Same posture as the refused reviewer write: no transcript message, no
    facilitation count — narrating a request the database rejected would put a
    standing ask on a session nobody can answer.
    """
    sessions = [
        {"session_id": "s1", "updated_at": _old_time(2.5), "paused_agent_id": "a1",
         "phase": "antithesis", "reviewer_agent_id": "gone-reviewer"}
    ]

    server = _make_mock_server({"a1": _make_agent_meta(status="paused")})

    mock_add_msg = AsyncMock()
    mock_mark = AsyncMock(return_value=False)
    mock_emit_refused = AsyncMock()

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.mcp_server", server), \
         patch(f"{AUTO_RESOLVE}.update_session_status_async", AsyncMock()), \
         patch(f"{AUTO_RESOLVE}.mark_awaiting_facilitation_async", mock_mark), \
         patch(f"{AUTO_RESOLVE}.emit_facilitation_needed", new_callable=AsyncMock), \
         patch(f"{AUTO_RESOLVE}.emit_write_refused", mock_emit_refused), \
         patch(f"{AUTO_RESOLVE}.add_message_async", mock_add_msg), \
         patch("src.mcp_handlers.dialectic.reviewer.select_reviewer",
               new_callable=AsyncMock, return_value=None):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    assert result["facilitation_count"] == 0
    assert result["skipped_count"] == 1
    mock_add_msg.assert_not_awaited()
    assert result["details"] == [{
        "session_id": "s1",
        "action": "write_refused",
        "attempted": "awaiting_facilitation",
    }]
    # The refusal must also reach the durable stream, not only the counter --
    # a skipped_count nobody can query cannot distinguish "no collision" from
    # "no instrument".
    mock_emit_refused.assert_awaited_once()
    assert mock_emit_refused.await_args.kwargs == {
        "session_id": "s1",
        "attempted": "awaiting_facilitation",
        "paused_agent_id": "a1",
        "source": "sweeper",
    }


@pytest.mark.asyncio
async def test_antithesis_fails_after_facilitation_timeout():
    """Session past facilitation timeout (4h) should be marked FAILED."""
    # Session is 5 hours old — past the 4h facilitation timeout
    sessions = [
        {"session_id": "s1", "updated_at": _old_time(5), "paused_agent_id": "a1",
         "phase": "antithesis", "reviewer_agent_id": "gone-reviewer"}
    ]

    server = _make_mock_server({
        "a1": _make_agent_meta(status="paused"),
    })

    mock_update_status = AsyncMock()
    mock_add_msg = AsyncMock()
    mock_select = AsyncMock(return_value=None)

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.mcp_server", server), \
         patch(f"{AUTO_RESOLVE}.update_session_status_async", mock_update_status), \
         patch(f"{AUTO_RESOLVE}.update_session_reviewer_async", AsyncMock()), \
         patch(f"{AUTO_RESOLVE}.add_message_async", mock_add_msg), \
         patch("src.mcp_handlers.dialectic.reviewer.select_reviewer", mock_select):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    assert result["resolved_count"] == 1  # Should be FAILED now
    mock_update_status.assert_called_once_with("s1", "failed")


@pytest.mark.asyncio
async def test_antithesis_with_active_reviewer_not_reassigned():
    """Stuck antithesis where reviewer is still active should be failed (timeout, not gone)."""
    sessions = [
        {"session_id": "s1", "updated_at": _old_time(5), "paused_agent_id": "a1",
         "phase": "antithesis", "reviewer_agent_id": "slow-reviewer"}
    ]

    server = _make_mock_server({
        "a1": _make_agent_meta(status="paused"),
        "slow-reviewer": _make_agent_meta(status="active"),  # Still there, just slow
    })

    mock_update_status = AsyncMock()
    mock_add_msg = AsyncMock()

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.mcp_server", server), \
         patch(f"{AUTO_RESOLVE}.update_session_status_async", mock_update_status), \
         patch(f"{AUTO_RESOLVE}.add_message_async", mock_add_msg):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    # Reviewer is present — this is a normal timeout, not a missing reviewer
    assert result["resolved_count"] == 1
    mock_update_status.assert_called_once_with("s1", "failed")


@pytest.mark.asyncio
async def test_handles_session_without_id():
    """Sessions without session_id should be skipped."""
    sessions = [
        {"updated_at": _old_time(5), "paused_agent_id": "a1", "phase": "thesis"}
    ]

    mock_update = AsyncMock()

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.update_session_status_async", mock_update), \
         patch(f"{AUTO_RESOLVE}.add_message_async", AsyncMock()):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    assert result["resolved_count"] == 0
    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_handles_z_suffix_timestamps():
    """Should handle 'Z' suffix in ISO timestamps."""
    old_time = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sessions = [
        {"session_id": "s1", "updated_at": old_time, "paused_agent_id": "a1", "phase": "thesis"}
    ]

    mock_update = AsyncMock()

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.update_session_status_async", mock_update), \
         patch(f"{AUTO_RESOLVE}.add_message_async", AsyncMock()):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    assert result["resolved_count"] == 1


@pytest.mark.asyncio
async def test_handles_get_sessions_error():
    """Should handle errors from get_active_sessions_async gracefully."""
    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, side_effect=Exception("DB error")):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    assert result["resolved_count"] == 0
    assert "error" in result


@pytest.mark.asyncio
async def test_late_cycle_error_preserves_earlier_committed_counts():
    """A later row failure must not turn an already-committed reap into zero."""
    sessions = [
        {
            "session_id": "reaped-before-error",
            "updated_at": _old_time(5),
            "paused_agent_id": "a1",
            "phase": "thesis",
            "reviewer_agent_id": None,
        },
        {
            "session_id": "saga-check-errors",
            "updated_at": _old_time(5),
            "paused_agent_id": "a2",
            "phase": "thesis",
            "reviewer_agent_id": None,
        },
    ]
    emitted = AsyncMock()

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.has_inflight_saga_async",
               new_callable=AsyncMock,
               side_effect=[False, RuntimeError("saga backend down")]), \
         patch(f"{AUTO_RESOLVE}.update_session_status_async",
               new_callable=AsyncMock, return_value=True), \
         patch(f"{AUTO_RESOLVE}.add_message_async", new_callable=AsyncMock), \
         patch(f"{AUTO_RESOLVE}.emit_sweep_cycle", emitted):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions(trigger_source="periodic")

    assert result["resolved_count"] == 1
    assert result["write_attempt_count"] == 1
    assert result["details"][0]["session_id"] == "reaped-before-error"
    assert result["error"] == "saga backend down"
    assert emitted.await_args.kwargs["resolved_count"] == 1
    assert emitted.await_args.kwargs["error"] == "saga backend down"


# --- check_and_resolve_stuck_sessions Tests ---


@pytest.mark.asyncio
async def test_check_and_resolve_delegates():
    """check_and_resolve_stuck_sessions should delegate to auto_resolve."""
    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=[]):
        from src.mcp_handlers.dialectic.auto_resolve import check_and_resolve_stuck_sessions
        result = await check_and_resolve_stuck_sessions()

    assert result["resolved_count"] == 0


@pytest.mark.asyncio
async def test_check_and_resolve_handles_error():
    """check_and_resolve should catch errors from auto_resolve."""
    with patch(f"{AUTO_RESOLVE}.auto_resolve_stuck_sessions",
               new_callable=AsyncMock, side_effect=Exception("unexpected")):
        from src.mcp_handlers.dialectic.auto_resolve import check_and_resolve_stuck_sessions
        result = await check_and_resolve_stuck_sessions()

    assert result["resolved_count"] == 0
    assert "error" in result


# --- Threshold Tests ---


def test_stuck_threshold_is_2_hours():
    """Threshold should match DialecticProtocol.MAX_ANTITHESIS_WAIT."""
    from src.mcp_handlers.dialectic.auto_resolve import STUCK_SESSION_THRESHOLD
    assert STUCK_SESSION_THRESHOLD == timedelta(hours=2)


def test_facilitation_timeout_is_4_hours():
    """Facilitation timeout should be 4 hours (2h stuck + 2h grace)."""
    from src.mcp_handlers.dialectic.auto_resolve import FACILITATION_TIMEOUT
    assert FACILITATION_TIMEOUT == timedelta(hours=4)

# --- In-process cache coherence -------------------------------------------
#
# The sweeper writes straight to PostgreSQL while ACTIVE_SESSIONS is never
# evicted, so anything it commits and does not mirror is invisible to the
# handlers in the same process — which are the ones that answer the request.


def _cached(session_id, *, phase, reviewer="gone-reviewer", awaiting=False):
    """Put a live session object in ACTIVE_SESSIONS, as a real process would."""
    from src.dialectic_protocol import DialecticSession, DialecticPhase
    from src.mcp_handlers.dialectic.session import ACTIVE_SESSIONS

    session = DialecticSession(
        paused_agent_id="a1", reviewer_agent_id=reviewer, dispute_type="verification",
    )
    session.phase = DialecticPhase(phase)
    session.awaiting_facilitation = awaiting
    ACTIVE_SESSIONS[session_id] = session
    return session


@pytest.fixture
def clean_active_sessions():
    from src.mcp_handlers.dialectic.session import ACTIVE_SESSIONS
    ACTIVE_SESSIONS.clear()
    yield ACTIVE_SESSIONS
    ACTIVE_SESSIONS.clear()


@pytest.mark.asyncio
async def test_facilitation_request_reaches_the_in_process_cache(clean_active_sessions):
    """A DB-only flag write is answerable everywhere except where it was raised.

    `handle_reassign_reviewer` reads ACTIVE_SESSIONS before the database and
    `_apply_reviewer_reassignment` decides revival from the in-memory
    `awaiting_facilitation`, so without the mirror the operator's reassign in
    this process reopens nothing and the guarded reviewer write is refused.
    """
    sessions = [
        {"session_id": "s1", "updated_at": _old_time(2.5), "paused_agent_id": "a1",
         "phase": "antithesis", "reviewer_agent_id": "gone-reviewer"}
    ]
    cached = _cached("s1", phase="antithesis")
    server = _make_mock_server({"a1": _make_agent_meta(status="paused")})

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.mcp_server", server), \
         patch(f"{AUTO_RESOLVE}.update_session_status_async", AsyncMock()), \
         patch(f"{AUTO_RESOLVE}.mark_awaiting_facilitation_async",
               new_callable=AsyncMock, return_value=True), \
         patch(f"{AUTO_RESOLVE}.emit_facilitation_needed", new_callable=AsyncMock), \
         patch(f"{AUTO_RESOLVE}.add_message_async", new_callable=AsyncMock), \
         patch("src.mcp_handlers.dialectic.reviewer.select_reviewer",
               new_callable=AsyncMock, return_value=None):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        await auto_resolve_stuck_sessions()

    assert cached.awaiting_facilitation is True


@pytest.mark.asyncio
async def test_reap_reaches_the_in_process_cache(clean_active_sessions):
    """A cached session must not go on reading as live after its row is reaped."""
    from src.dialectic_protocol import DialecticPhase

    sessions = [
        {"session_id": "s1", "updated_at": _old_time(5), "paused_agent_id": "a1",
         "phase": "antithesis", "reviewer_agent_id": None,
         "awaiting_facilitation": True}
    ]
    cached = _cached("s1", phase="antithesis", reviewer=None, awaiting=True)
    server = _make_mock_server({"a1": _make_agent_meta(status="paused")})

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.mcp_server", server), \
         patch(f"{AUTO_RESOLVE}.update_session_status_async",
               new_callable=AsyncMock, return_value=True), \
         patch(f"{AUTO_RESOLVE}.add_message_async", new_callable=AsyncMock):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    assert result["resolved_count"] == 1
    assert cached.phase == DialecticPhase.FAILED


@pytest.mark.asyncio
async def test_reassignment_clears_a_standing_request(clean_active_sessions):
    """Reassigning ANSWERS the request, so the request must not outlive it.

    A stale flag on a session that later fails ordinarily would make it
    revivable by `reassign` — the hazard the guarded writer exists to avoid.
    """
    sessions = [
        {"session_id": "s1", "updated_at": _old_time(2.5), "paused_agent_id": "a1",
         "phase": "antithesis", "reviewer_agent_id": "gone-reviewer",
         "awaiting_facilitation": True}
    ]
    cached = _cached("s1", phase="antithesis", awaiting=True)
    server = _make_mock_server({
        "a1": _make_agent_meta(status="paused"),
        "a2": _make_agent_meta(status="active"),
    })
    mock_clear = AsyncMock(return_value=True)

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.mcp_server", server), \
         patch(f"{AUTO_RESOLVE}.update_session_reviewer_async",
               new_callable=AsyncMock, return_value=True), \
         patch(f"{AUTO_RESOLVE}.update_session_awaiting_facilitation_async", mock_clear), \
         patch(f"{AUTO_RESOLVE}.emit_reviewer_reassigned", new_callable=AsyncMock), \
         patch(f"{AUTO_RESOLVE}.update_session_status_async", AsyncMock()), \
         patch(f"{AUTO_RESOLVE}.add_message_async", new_callable=AsyncMock), \
         patch("src.mcp_handlers.dialectic.reviewer.select_reviewer",
               new_callable=AsyncMock, return_value="a2"):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    assert result["reassigned_count"] == 1
    mock_clear.assert_awaited_once_with("s1", False)
    assert cached.awaiting_facilitation is False
    assert cached.reviewer_agent_id == "a2"


@pytest.mark.asyncio
async def test_a_failed_flag_write_does_not_discard_the_cycle():
    """One session's DB error must not throw away reaps already committed.

    The write sits in the per-session loop of a sweep whose earlier iterations
    have already marked sessions failed; letting it reach the outer handler
    would report the whole cycle as an error and lose their counts.
    """
    sessions = [
        # Reaped first (5h old, no reviewer), then the one that errors.
        {"session_id": "reaped", "updated_at": _old_time(5), "paused_agent_id": "a0",
         "phase": "thesis", "reviewer_agent_id": None},
        {"session_id": "s1", "updated_at": _old_time(2.5), "paused_agent_id": "a1",
         "phase": "antithesis", "reviewer_agent_id": "gone-reviewer"},
    ]
    server = _make_mock_server({
        "a0": _make_agent_meta(status="paused"),
        "a1": _make_agent_meta(status="paused"),
    })

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.mcp_server", server), \
         patch(f"{AUTO_RESOLVE}.update_session_status_async",
               new_callable=AsyncMock, return_value=True), \
         patch(f"{AUTO_RESOLVE}.mark_awaiting_facilitation_async",
               new_callable=AsyncMock, side_effect=RuntimeError("connection lost")), \
         patch(f"{AUTO_RESOLVE}.add_message_async", new_callable=AsyncMock), \
         patch("src.mcp_handlers.dialectic.reviewer.select_reviewer",
               new_callable=AsyncMock, return_value=None):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    assert "error" not in result, "the cycle must not be reported as a failure"
    assert result["resolved_count"] == 1, "the committed reap keeps its count"
    assert result["facilitation_count"] == 0, "the request was never recorded"
