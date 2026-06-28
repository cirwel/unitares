"""
Regression tests for the auto-resolve sweeper saga-inflight guard (C1,
council 2026-06-28).

The periodic stuck-session sweeper writes status='failed' (and reassigns
reviewers) on sessions inactive for >2h. Once the BEAM session owner drives a
SYNTHESIS->RESOLVED resolution it holds a non-terminal row in
coordination.session_resolution_sagas, possibly for >2h. The sweeper must skip
such a session, or it races the saga and corrupts the outcome.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

AUTO_RESOLVE = "src.mcp_handlers.dialectic.auto_resolve"


def _old_time(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


@pytest.mark.asyncio
async def test_sweeper_skips_session_with_inflight_saga():
    """A stuck session with an in-flight resolution saga is NOT touched."""
    sessions = [
        {
            "session_id": "saga-inflight-1",
            "updated_at": _old_time(5),
            "paused_agent_id": "a1",
            "reviewer_agent_id": "r1",
            "phase": "synthesis",
        }
    ]
    mock_update = AsyncMock()
    mock_reviewer = AsyncMock()
    mock_add_msg = AsyncMock()

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.has_inflight_saga_async",
               new_callable=AsyncMock, return_value=True), \
         patch(f"{AUTO_RESOLVE}.update_session_status_async", mock_update), \
         patch(f"{AUTO_RESOLVE}.update_session_reviewer_async", mock_reviewer), \
         patch(f"{AUTO_RESOLVE}.add_message_async", mock_add_msg):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    # The guard must prevent every write to the saga-owned session.
    mock_update.assert_not_called()
    mock_reviewer.assert_not_called()
    mock_add_msg.assert_not_called()
    assert result["resolved_count"] == 0
    assert result["reassigned_count"] == 0


@pytest.mark.asyncio
async def test_sweeper_proceeds_when_no_inflight_saga():
    """With no saga in flight, the sweeper still fails a long-stuck session."""
    sessions = [
        {
            "session_id": "no-saga-1",
            "updated_at": _old_time(5),
            "paused_agent_id": "a1",
            "reviewer_agent_id": None,
            "phase": "thesis",
        }
    ]
    mock_update = AsyncMock()
    mock_add_msg = AsyncMock()

    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=sessions), \
         patch(f"{AUTO_RESOLVE}.has_inflight_saga_async",
               new_callable=AsyncMock, return_value=False), \
         patch(f"{AUTO_RESOLVE}.update_session_status_async", mock_update), \
         patch(f"{AUTO_RESOLVE}.add_message_async", mock_add_msg):
        from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
        result = await auto_resolve_stuck_sessions()

    mock_update.assert_called_once_with("no-saga-1", "failed")
    assert result["resolved_count"] == 1
