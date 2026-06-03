"""Wave 0 step 2C-1 — coordination_failure.anyio_cancellation.background_task wire.

Pins:
  - The supervised-task done-callback (`_on_background_task_done`) emits
    `coordination_failure.anyio_cancellation.background_task` exactly when
    the task ended via cancellation (not when it completed normally and not
    when it crashed with a non-cancellation exception — those keep the
    existing logger.error path).
  - Payload carries `task_name` for per-task attribution and `incident_id`.
  - Emit failure (mocked to raise) does NOT break the callback's existing
    bookkeeping — the task is still removed from the supervised list.

§2.executor_loop is deferred; see PR description for the design note.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


EVENT_TYPE = "coordination_failure.anyio_cancellation.background_task"


@pytest.fixture(autouse=True)
def _reset_shutdown_latch():
    """The shutdown latch (`_background_tasks_shutting_down`) is a one-way
    module global; other test files call stop_all_background_tasks() which
    latches it True for the whole pytest process, which would suppress the
    emits these tests assert on. Reset to the runtime default around each test
    so ordering can't contaminate the result."""
    import src.background_tasks as bt

    bt._background_tasks_shutting_down = False
    yield
    bt._background_tasks_shutting_down = False


@pytest.mark.asyncio
async def test_cancelled_task_emits_event():
    """A supervised task ending via cancellation MUST trigger an emit
    with event_type=`...anyio_cancellation.background_task` and the task's
    name in the payload."""
    from src.background_tasks import _supervised_create_task, _supervised_tasks

    async def long_runner():
        await asyncio.sleep(60)

    with patch(
        "src.coordination_failure_emit.emit_coordination_failure_sync"
    ) as mock_emit:
        task = _supervised_create_task(long_runner(), name="test_cancellation_emit")
        await asyncio.sleep(0)  # let the task actually start
        task.cancel()
        # Wait for cancellation + done-callback to drain
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Done callbacks run on the next event-loop tick
        await asyncio.sleep(0)

    mock_emit.assert_called_once()
    kwargs = mock_emit.call_args.kwargs
    assert kwargs["service"] == "governance_mcp"
    assert kwargs["event_type"] == EVENT_TYPE
    assert kwargs["payload"]["task_name"] == "test_cancellation_emit"
    assert "incident_id" in kwargs["payload"]
    assert len(kwargs["payload"]["incident_id"]) == 36

    # Bookkeeping invariant — task is removed from the supervised list
    assert task not in _supervised_tasks


@pytest.mark.asyncio
async def test_completed_task_does_not_emit():
    """Normal completion of a supervised task MUST NOT emit a cancellation
    event."""
    from src.background_tasks import _supervised_create_task

    async def quick_runner():
        return "done"

    with patch(
        "src.coordination_failure_emit.emit_coordination_failure_sync"
    ) as mock_emit:
        task = _supervised_create_task(quick_runner(), name="test_no_emit_on_complete")
        await task
        await asyncio.sleep(0)

    mock_emit.assert_not_called()


@pytest.mark.asyncio
async def test_crashed_task_does_not_emit_cancellation_event():
    """A task that raises a non-cancellation exception keeps the existing
    logger.error path; it MUST NOT emit a cancellation-specific event
    (the family belongs to a different sub-namespace)."""
    from src.background_tasks import _supervised_create_task

    async def crasher():
        raise RuntimeError("intentional")

    with patch(
        "src.coordination_failure_emit.emit_coordination_failure_sync"
    ) as mock_emit:
        task = _supervised_create_task(crasher(), name="test_crash_no_cancel_emit")
        # Drain the exception so pytest doesn't surface it as an unhandled error
        try:
            await task
        except RuntimeError:
            pass
        await asyncio.sleep(0)

    mock_emit.assert_not_called()


@pytest.mark.asyncio
async def test_emit_failure_does_not_break_supervised_list_bookkeeping():
    """If the emit raises (defense-in-depth — the inner function is already
    failure-safe by contract), the done-callback MUST still remove the
    cancelled task from `_supervised_tasks`. Observability MUST NOT break
    supervisor invariants."""
    from src.background_tasks import _supervised_create_task, _supervised_tasks

    async def long_runner():
        await asyncio.sleep(60)

    with patch(
        "src.coordination_failure_emit.emit_coordination_failure_sync",
        side_effect=RuntimeError("emit blew up"),
    ):
        task = _supervised_create_task(long_runner(), name="test_emit_raise_safety")
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)

    assert task not in _supervised_tasks


@pytest.mark.asyncio
async def test_graceful_shutdown_cancellation_does_not_emit(monkeypatch):
    """When the graceful-shutdown latch is set, supervised-task cancellations
    are benign restart teardown and MUST NOT emit a coordination_failure event
    — they were inflating the §129 substrate-tax incident count with restart
    noise (2026-06-03 fix). monkeypatch restores the one-way flag after the
    test so it cannot contaminate the runtime-cancel tests."""
    import src.background_tasks as bt
    from src.background_tasks import _supervised_create_task

    async def long_runner():
        await asyncio.sleep(60)

    monkeypatch.setattr(bt, "_background_tasks_shutting_down", True)

    with patch(
        "src.coordination_failure_emit.emit_coordination_failure_sync"
    ) as mock_emit:
        task = _supervised_create_task(long_runner(), name="test_shutdown_suppressed")
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)

    # Suppressed during shutdown — no substrate-tax incident emitted.
    mock_emit.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_cancellation_still_emits_when_not_shutting_down():
    """Defense for reviewer Finding 3: a runtime cancellation (flag still
    False — e.g. cancel_and_respawn_task while the server is up) MUST still
    emit; the shutdown latch must not over-suppress the genuine signal."""
    import src.background_tasks as bt
    from src.background_tasks import _supervised_create_task

    assert bt._background_tasks_shutting_down is False  # default runtime state

    async def long_runner():
        await asyncio.sleep(60)

    with patch(
        "src.coordination_failure_emit.emit_coordination_failure_sync"
    ) as mock_emit:
        task = _supervised_create_task(long_runner(), name="test_runtime_cancel_emits")
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)

    mock_emit.assert_called_once()
    assert mock_emit.call_args.kwargs["event_type"] == EVENT_TYPE
