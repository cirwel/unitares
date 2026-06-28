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
               new_callable=AsyncMock, return_value=False):
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

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.mcp_server", server), \
         patch(f"{AUTO_RESOLVE}.update_session_reviewer_async", mock_update_reviewer), \
         patch(f"{AUTO_RESOLVE}.add_message_async", mock_add_msg), \
         patch("src.mcp_handlers.dialectic.reviewer.select_reviewer", mock_select):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    assert result["reassigned_count"] == 1
    assert result["resolved_count"] == 0  # Not failed
    mock_update_reviewer.assert_called_once_with("s1", "new-reviewer")


@pytest.mark.asyncio
async def test_antithesis_awaits_facilitation_when_no_candidates():
    """Stuck antithesis with no replacement should await facilitation (not fail immediately)."""
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
    mock_select = AsyncMock(return_value=None)

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.mcp_server", server), \
         patch(f"{AUTO_RESOLVE}.update_session_status_async", mock_update_status), \
         patch(f"{AUTO_RESOLVE}.add_message_async", mock_add_msg), \
         patch("src.mcp_handlers.dialectic.reviewer.select_reviewer", mock_select):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    assert result["facilitation_count"] == 1
    assert result["resolved_count"] == 0  # NOT failed yet
    mock_update_status.assert_not_called()  # Should not mark as failed


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
