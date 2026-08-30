"""Reentrancy guard for the auto-resolve pre-check in is_agent_in_active_session.

select_reviewer() calls is_agent_in_active_session() once per candidate, and
auto_resolve_stuck_sessions() calls select_reviewer(). Without a guard, one
top-level is_agent_in_active_session() call fans the stuck-session sweep out to
O(fleet_size) PG scans once UNITARES_AUTOSELECT_REVIEWER is enabled. The guard
runs the sweep at most once per asyncio task-tree.
"""

import asyncio

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


@pytest.mark.asyncio
async def test_direct_resolver_owns_guard_during_reviewer_selection():
    """A background/direct cycle must not fan out through its candidates.

    The old guard was set only by ``is_agent_in_active_session`` before its
    lazy call. A direct/background resolver entered with the flag false, then
    ``select_reviewer`` called the lazy path once per candidate and recursively
    launched another complete sweep each time.
    """
    from datetime import datetime, timedelta, timezone

    from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
    from src.mcp_handlers.dialectic.reviewer import is_agent_in_active_session

    session = {
        "session_id": "stuck-1",
        "updated_at": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),
        "paused_agent_id": "paused",
        "reviewer_agent_id": "gone",
        "phase": "antithesis",
    }
    nested_sweep = AsyncMock(return_value={"resolved_count": 0})

    async def select_two_candidates(**_kwargs):
        await is_agent_in_active_session("candidate-1")
        await is_agent_in_active_session("candidate-2")
        return None

    server = type("Server", (), {
        "agent_metadata": {},
        "load_metadata_async": AsyncMock(),
    })()
    with patch(f"{AUTO_RESOLVE}.get_active_sessions_async",
               new_callable=AsyncMock, return_value=[session]), \
         patch(f"{AUTO_RESOLVE}.has_inflight_saga_async",
               new_callable=AsyncMock, return_value=False), \
         patch(f"{AUTO_RESOLVE}.mcp_server", server), \
         patch(f"{AUTO_RESOLVE}.update_session_status_async",
               new_callable=AsyncMock, return_value=True), \
         patch(f"{AUTO_RESOLVE}.add_message_async", new_callable=AsyncMock), \
         patch(f"{AUTO_RESOLVE}.emit_sweep_cycle", new_callable=AsyncMock), \
         patch(f"{AUTO_RESOLVE}.check_and_resolve_stuck_sessions", nested_sweep), \
         patch(f"{REVIEWER}.pg_is_agent_in_active_session",
               new_callable=AsyncMock, return_value=False), \
         patch(f"{REVIEWER}.select_reviewer", side_effect=select_two_candidates):
        result = await auto_resolve_stuck_sessions(trigger_source="periodic")

    assert result["resolved_count"] == 1
    nested_sweep.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_top_level_tasks_do_not_suppress_each_other():
    """ContextVar ownership is isolated across independent asyncio tasks."""
    from src.mcp_handlers.dialectic import auto_resolve

    entered = 0
    both_entered = asyncio.Event()
    cycle = {
        "resolved_count": 0,
        "reassigned_count": 0,
        "facilitation_count": 0,
        "skipped_count": 0,
        "active_session_count": 0,
        "active_session_batch_truncated": False,
        "stuck_session_count": 0,
        "invalid_session_count": 0,
        "saga_inflight_skip_count": 0,
        "write_attempt_count": 0,
    }

    async def overlapping_cycle():
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await both_entered.wait()
        return cycle.copy()

    emitted = AsyncMock()
    with patch.object(auto_resolve, "_auto_resolve_stuck_sessions",
                      side_effect=overlapping_cycle), \
         patch.object(auto_resolve, "emit_sweep_cycle", emitted):
        results = await asyncio.wait_for(
            asyncio.gather(
                auto_resolve.auto_resolve_stuck_sessions(trigger_source="periodic"),
                auto_resolve.auto_resolve_stuck_sessions(
                    trigger_source="active_session_check"
                ),
            ),
            timeout=1,
        )

    assert entered == 2
    assert emitted.await_count == 2
    assert all("reentrant_suppressed" not in result for result in results)
