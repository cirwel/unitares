"""Tests for the periodic dialectic stuck-session sweep.

auto_resolve_stuck_sessions() was previously only invoked lazily (from
is_agent_in_active_session), so a session that went stale with no further
dialectic traffic was never swept. dialectic_auto_resolve_sweeper_task gives it
a real timer. These tests pin the cycle's key-mapping (the auto_resolve return
key `resolved_count` confusingly counts FAILED sessions) and registration.
"""

import pytest
from unittest.mock import AsyncMock, patch

AR = "src.mcp_handlers.dialectic.auto_resolve.auto_resolve_stuck_sessions"


@pytest.mark.asyncio
async def test_cycle_maps_autoresolve_keys():
    """resolved_count -> failed, reassigned_count -> reassigned, facilitation."""
    from src.background_tasks import _run_dialectic_auto_resolve_cycle

    fake = {"resolved_count": 2, "reassigned_count": 1, "facilitation_count": 3}
    with patch(AR, new=AsyncMock(return_value=fake)):
        summary = await _run_dialectic_auto_resolve_cycle()

    assert summary == {"failed": 2, "reassigned": 1, "facilitation": 3}


@pytest.mark.asyncio
async def test_cycle_handles_missing_keys():
    """A sparse / error return must not raise; missing counts read as 0."""
    from src.background_tasks import _run_dialectic_auto_resolve_cycle

    with patch(AR, new=AsyncMock(return_value={"error": "boom"})):
        summary = await _run_dialectic_auto_resolve_cycle()

    assert summary == {"failed": 0, "reassigned": 0, "facilitation": 0}


@pytest.mark.asyncio
async def test_cycle_handles_none_values():
    from src.background_tasks import _run_dialectic_auto_resolve_cycle

    with patch(AR, new=AsyncMock(return_value={"resolved_count": None})):
        summary = await _run_dialectic_auto_resolve_cycle()

    assert summary["failed"] == 0


def test_sweeper_task_is_registered():
    """The sweeper must actually be wired into startup, not just defined."""
    import inspect
    import src.background_tasks as bt

    # the task function exists and is a coroutine function
    assert inspect.iscoroutinefunction(bt.dialectic_auto_resolve_sweeper_task)
    # and is registered in the startup wiring
    src_text = inspect.getsource(bt)
    assert 'dialectic_auto_resolve_sweeper_task()' in src_text
    assert 'name="dialectic_auto_resolve_sweeper"' in src_text
