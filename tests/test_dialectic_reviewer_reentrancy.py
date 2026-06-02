"""Reentrancy guard for the auto-resolve pre-check in is_agent_in_active_session.

select_reviewer() calls is_agent_in_active_session() once per candidate, and
auto_resolve_stuck_sessions() calls select_reviewer(). Without a guard, one
top-level is_agent_in_active_session() call fans the stuck-session sweep out to
O(fleet_size) PG scans once UNITARES_AUTOSELECT_REVIEWER is enabled. The guard
runs the sweep at most once per asyncio task-tree.
"""

import pytest
from unittest.mock import AsyncMock, patch

REVIEWER = "src.mcp_handlers.dialectic.reviewer"
AUTO_RESOLVE = "src.mcp_handlers.dialectic.auto_resolve"


@pytest.mark.asyncio
async def test_auto_resolve_runs_once_at_top_level():
    """A plain call runs the stuck-session sweep exactly once."""
    from src.mcp_handlers.dialectic.reviewer import is_agent_in_active_session

    sweep = AsyncMock(return_value={"resolved_count": 0})
    with patch(f"{AUTO_RESOLVE}.check_and_resolve_stuck_sessions", sweep), \
         patch(f"{REVIEWER}.pg_is_agent_in_active_session",
               new_callable=AsyncMock, return_value=False):
        result = await is_agent_in_active_session("agent-x")

    assert result is False
    sweep.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_resolve_skipped_when_already_in_progress():
    """When the guard is already set (i.e. we are nested inside an auto-resolve
    -> select_reviewer chain), the inner call must NOT re-trigger the sweep."""
    from src.mcp_handlers.dialectic.reviewer import (
        is_agent_in_active_session,
        _AUTO_RESOLVE_IN_PROGRESS,
    )

    sweep = AsyncMock(return_value={"resolved_count": 0})
    token = _AUTO_RESOLVE_IN_PROGRESS.set(True)
    try:
        with patch(f"{AUTO_RESOLVE}.check_and_resolve_stuck_sessions", sweep), \
             patch(f"{REVIEWER}.pg_is_agent_in_active_session",
                   new_callable=AsyncMock, return_value=False):
            result = await is_agent_in_active_session("agent-x")
    finally:
        _AUTO_RESOLVE_IN_PROGRESS.reset(token)

    assert result is False
    sweep.assert_not_awaited()


@pytest.mark.asyncio
async def test_guard_resets_after_call():
    """The guard must not leak across sequential top-level calls."""
    from src.mcp_handlers.dialectic.reviewer import (
        is_agent_in_active_session,
        _AUTO_RESOLVE_IN_PROGRESS,
    )

    sweep = AsyncMock(return_value={"resolved_count": 0})
    with patch(f"{AUTO_RESOLVE}.check_and_resolve_stuck_sessions", sweep), \
         patch(f"{REVIEWER}.pg_is_agent_in_active_session",
               new_callable=AsyncMock, return_value=False):
        await is_agent_in_active_session("agent-x")
        await is_agent_in_active_session("agent-y")

    assert _AUTO_RESOLVE_IN_PROGRESS.get() is False
    assert sweep.await_count == 2
