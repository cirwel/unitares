"""Regression guards for P001 fire-and-forget task references.

Bare `loop.create_task(coro)` returns a Task that CPython GC can collect
mid-await if no caller holds a reference. For tasks that await asyncpg
(audit-log PG tail, thread-identity persist, inferred-purpose persist),
collection between yields silently drops the write.

These tests pin the "store ref in module-local set, clear on done"
pattern at each of the three fixed sites (Watcher P001 fingerprints
#69f2ccbc, #0a0616c2, #acfc7012 — 2026-05-18).
"""

import asyncio

import pytest


# ─── audit_log._spawn_pg_audit_task ────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_log_spawn_pins_task_until_done():
    """`_spawn_pg_audit_task` adds the Task to the inflight set on create
    and removes it via done callback when the coro finishes."""
    from src.audit_log import _inflight_pg_audit_tasks, _spawn_pg_audit_task

    completed = asyncio.Event()

    async def _stub():
        await asyncio.sleep(0)
        completed.set()

    loop = asyncio.get_running_loop()
    before = len(_inflight_pg_audit_tasks)
    _spawn_pg_audit_task(loop, _stub())
    # Pinned immediately on spawn
    assert len(_inflight_pg_audit_tasks) == before + 1

    # Let the coroutine run + done callback fire
    await asyncio.wait_for(completed.wait(), timeout=1.0)
    # done callback runs on the same loop; one more yield gives it the slot
    await asyncio.sleep(0)
    assert len(_inflight_pg_audit_tasks) == before


# ─── phases._spawn_persist_task ────────────────────────────────────────


@pytest.mark.asyncio
async def test_phases_spawn_pins_task_until_done():
    """`_spawn_persist_task` does the same for thread-identity and
    inferred-purpose persisters."""
    from src.mcp_handlers.updates.phases import (
        _inflight_persist_tasks,
        _spawn_persist_task,
    )

    completed = asyncio.Event()

    async def _stub():
        await asyncio.sleep(0)
        completed.set()

    before = len(_inflight_persist_tasks)
    _spawn_persist_task(_stub(), name="test_persist")
    assert len(_inflight_persist_tasks) == before + 1

    await asyncio.wait_for(completed.wait(), timeout=1.0)
    await asyncio.sleep(0)
    assert len(_inflight_persist_tasks) == before


@pytest.mark.asyncio
async def test_phases_spawn_task_carries_name():
    """The Task name is preserved so a stray crash log identifies the
    call site. Mirrors the same property tested for
    coordination_failure_emit._spawn_dedicated_write_task."""
    from src.mcp_handlers.updates.phases import (
        _inflight_persist_tasks,
        _spawn_persist_task,
    )

    async def _stub():
        await asyncio.sleep(0)

    _spawn_persist_task(_stub(), name="my_distinctive_name")
    pinned = [t for t in _inflight_persist_tasks if t.get_name() == "my_distinctive_name"]
    assert len(pinned) == 1

    # Let the task complete to keep the set clean for sibling tests
    await asyncio.sleep(0)
    await asyncio.sleep(0)
