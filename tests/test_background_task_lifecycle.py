"""Regression tests for background task ownership and broadcaster cleanup."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from src import background_tasks
from src.broadcaster import EISVBroadcaster


@pytest.mark.asyncio
async def test_stop_all_background_tasks_cancels_supervised_tasks():
    background_tasks._supervised_tasks.clear()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def sleeper():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = background_tasks._supervised_create_task(sleeper(), name="test_supervised")
    await started.wait()

    await background_tasks.stop_all_background_tasks()

    assert cancelled.is_set()
    assert task.cancelled()
    assert background_tasks._supervised_tasks == []


@pytest.mark.asyncio
async def test_startup_auto_calibration_supervises_ground_truth_collector(monkeypatch):
    background_tasks._supervised_tasks.clear()
    created: list[tuple[str | None, asyncio.Task]] = []

    async def fake_sleep(_seconds):
        return None

    async def fake_collect_ground_truth_automatically(**_kwargs):
        return {"updated": 0}

    async def fake_collector_task(interval_hours: float = 6.0):
        await asyncio.Event().wait()

    def fake_supervised_create_task(coro, *, name: str | None = None):
        task = asyncio.create_task(coro, name=name)
        created.append((name, task))
        task.cancel()
        return task

    import src.auto_ground_truth as auto_ground_truth

    monkeypatch.setattr(background_tasks.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        auto_ground_truth,
        "collect_ground_truth_automatically",
        fake_collect_ground_truth_automatically,
    )
    monkeypatch.setattr(
        auto_ground_truth,
        "auto_ground_truth_collector_task",
        fake_collector_task,
    )
    monkeypatch.setattr(
        background_tasks,
        "_supervised_create_task",
        fake_supervised_create_task,
    )

    await background_tasks.startup_auto_calibration()
    await asyncio.gather(*(task for _, task in created), return_exceptions=True)

    assert [name for name, _ in created] == ["auto_ground_truth_collector"]


def test_mcp_server_bootstrap_stops_background_tasks_before_closing_db():
    from src.services.mcp_server_bootstrap import ServerBootstrap

    source = inspect.getsource(ServerBootstrap.shutdown)
    assert "await stop_all_background_tasks()" in source
    assert source.index("await stop_all_background_tasks()") < source.index("await close_db()")


class _HealthySocket:
    def __init__(self):
        self.sent = []
        self.close_calls = 0

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self):
        self.close_calls += 1


class _SlowSocket:
    def __init__(self):
        self.close_calls = 0

    async def send_json(self, data):
        await asyncio.sleep(10)

    async def close(self):
        self.close_calls += 1


@pytest.mark.asyncio
async def test_broadcaster_closes_dead_sockets_after_timeout():
    broadcaster = EISVBroadcaster()
    broadcaster._SEND_TIMEOUT_SECONDS = 0.01

    healthy = _HealthySocket()
    slow = _SlowSocket()
    broadcaster.connections = [healthy, slow]

    payload = {"type": "eisv_update"}
    await broadcaster._send_to_clients(payload)

    assert healthy.sent == [payload]
    assert slow.close_calls == 1
    assert slow not in broadcaster.connections
    assert healthy in broadcaster.connections


# ---------------------------------------------------------------------------
# Restartable named tasks — make-the-unstick-button-real regression tests
# ---------------------------------------------------------------------------
#
# Background: until 2026-04-14 the dashboard's "unstick" button only flipped
# meta.status = "active" + a Postgres write. It never touched the asyncio
# Task object behind the wedged background task. These tests pin the new
# cancel_and_respawn primitive that makes unstick actually unstick.


@pytest.mark.asyncio
async def test_cancel_and_respawn_replaces_running_task():
    """The real unstick: cancel_and_respawn_task must cancel the live task
    AND register a fresh one spawned from the same factory."""
    background_tasks._supervised_tasks.clear()
    background_tasks._RESTARTABLE_TASK_FACTORIES.clear()
    background_tasks._RESTARTABLE_TASKS.clear()

    cancelled = asyncio.Event()
    second_started = asyncio.Event()
    spawn_count = [0]

    def factory():
        spawn_count[0] += 1
        is_first = spawn_count[0] == 1

        async def runner():
            try:
                if is_first:
                    await asyncio.Event().wait()  # park forever (the wedge)
                else:
                    second_started.set()
                    await asyncio.Event().wait()  # park forever too — we
                    # just need to confirm the new task started
            except asyncio.CancelledError:
                if is_first:
                    cancelled.set()
                raise

        return runner()

    first = background_tasks._spawn_restartable_task("unit_test_task", factory)
    # Yield so the first task actually starts
    await asyncio.sleep(0)

    info = background_tasks.cancel_and_respawn_task("unit_test_task")

    assert info["restarted"] is True
    assert info["previous_state"] == "running"

    # The new task is registered under the same name and is not the same
    # object as the original.
    new_task = background_tasks._RESTARTABLE_TASKS["unit_test_task"]
    assert new_task is not first

    # Wait for cancellation of the first task and start of the second.
    await asyncio.wait_for(cancelled.wait(), timeout=1.0)
    await asyncio.wait_for(second_started.wait(), timeout=1.0)

    # Cleanup so other tests don't see this task.
    new_task.cancel()
    try:
        await new_task
    except (asyncio.CancelledError, BaseException):
        pass


@pytest.mark.asyncio
async def test_cancel_and_respawn_unknown_name_returns_failure():
    """An unknown task name must not raise — it should return a structured
    failure so the resume handler can continue gracefully and report the
    fact to the operator instead of 500-ing."""
    background_tasks._RESTARTABLE_TASK_FACTORIES.clear()
    background_tasks._RESTARTABLE_TASKS.clear()

    info = background_tasks.cancel_and_respawn_task("does_not_exist")

    assert info["restarted"] is False
    assert info["previous_state"] == "unknown"
    assert info["reason"] is not None
    assert "does_not_exist" in info["reason"]
