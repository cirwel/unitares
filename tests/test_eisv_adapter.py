"""The synchronous adapter preserves the existing monitor invocation boundary."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.eisv import adapter


@pytest.fixture
def edges(monkeypatch):
    from src import agent_lifecycle, agent_monitor_state

    events = []
    result = {"decision": {"action": "pause"}, "metrics": {"E": 0.6}}
    monitor = SimpleNamespace(process_update=Mock(
        side_effect=lambda *a, **kw: events.append("update") or result,
    ))
    get_monitor = Mock(side_effect=lambda *a: events.append("monitor") or monitor)
    hydrate = AsyncMock(side_effect=lambda *a: events.append("hydrate"))
    metric = Mock()
    monkeypatch.setattr(agent_lifecycle, "get_or_create_monitor", get_monitor)
    monkeypatch.setattr(agent_monitor_state, "hydrate_from_db_if_fresh", hydrate)
    monkeypatch.setattr(adapter, "_perf_record_ms", metric)
    return SimpleNamespace(
        events=events, result=result, monitor=monitor, get_monitor=get_monitor,
        hydrate=hydrate, metric=metric,
    )


def metric_names(edges):
    return [call.args[0] for call in edges.metric.call_args_list]


def test_request_is_shallowly_frozen_and_keeps_call_local_mapping():
    state = {}
    request = adapter.EisvRequest("agent", state)
    with pytest.raises(FrozenInstanceError):
        request.agent_state = {}
    state["_checkpoint_id"] = "accepted-checkpoint"
    assert request.agent_state is state
    assert request.agent_state["_checkpoint_id"] == "accepted-checkpoint"
    assert request.confidence is None


@pytest.mark.asyncio
@pytest.mark.parametrize("history_kind", ["absent", "none", "list"])
async def test_original_mapping_result_monitor_and_history_semantics(edges, history_kind):
    state = {"_checkpoint_id": "accepted-checkpoint"}
    if history_kind != "absent":
        state["_outcome_history"] = None if history_kind == "none" else [{"is_bad": False}]
    history = state.get("_outcome_history")

    assessment = await adapter.assess_eisv(adapter.EisvRequest("agent", state, 0.7))

    assert assessment.result is edges.result
    assert assessment.monitor is edges.monitor
    assert edges.events == ["monitor", "hydrate", "update"]
    edges.get_monitor.assert_called_once_with("agent")
    edges.hydrate.assert_awaited_once_with(edges.monitor, "agent")
    args, kwargs = edges.monitor.process_update.call_args
    assert args[0] is state
    assert kwargs == {"confidence": 0.7, "task_type": "mixed"}
    assert ("_outcome_history" in args[0]) == (history_kind != "absent")
    assert args[0].get("_outcome_history") is history
    assert args[0]["_checkpoint_id"] == "accepted-checkpoint"
    # The adapter does not apply the default observe posture to this pause.
    assert assessment.result["decision"]["action"] == "pause"
    assert metric_names(edges) == ["ode.monitor_setup_ms", "ode.numpy_step_ms"]
    assert all(call.args[1] >= 0 for call in edges.metric.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("hydrated_type", [None, "convergent"])
async def test_task_type_is_read_after_hydration_without_normalizing_none(edges, hydrated_type):
    state = {"task_type": "before-hydration"}

    async def hydrate(*args):
        edges.events.append("hydrate")
        state["task_type"] = hydrated_type

    edges.hydrate.side_effect = hydrate
    await adapter.assess_eisv(adapter.EisvRequest("agent", state))

    assert edges.monitor.process_update.call_args.kwargs["task_type"] is hydrated_type


@pytest.mark.asyncio
async def test_estimator_mutation_is_visible_to_original_caller(edges):
    state = {"_outcome_history": []}

    def update(received, **kwargs):
        received["estimator_annotation"] = "retained"
        received["_outcome_history"].append({"is_bad": True})
        return edges.result

    edges.monitor.process_update.side_effect = update
    await adapter.assess_eisv(adapter.EisvRequest("agent", state))

    assert state == {
        "_outcome_history": [{"is_bad": True}],
        "estimator_annotation": "retained",
    }


@pytest.mark.asyncio
async def test_default_executor_does_not_propagate_caller_context(edges, monkeypatch):
    marker = contextvars.ContextVar("eisv-adapter-test", default="worker-default")
    marker_token = marker.set("caller-context")
    loop = asyncio.get_running_loop()
    original_executor = loop.run_in_executor
    executor_calls = []
    observations = []
    caller_thread = threading.get_ident()

    def dispatch(executor, func, *args):
        executor_calls.append(executor)
        return original_executor(executor, func, *args)

    def get_monitor(*args):
        observations.append(("monitor", threading.get_ident(), marker.get()))
        return edges.monitor

    async def hydrate(*args):
        observations.append(("hydrate", threading.get_ident(), marker.get()))

    def update(*args, **kwargs):
        observations.append(("update", threading.get_ident(), marker.get()))
        return edges.result

    monkeypatch.setattr(loop, "run_in_executor", dispatch)
    edges.get_monitor.side_effect = get_monitor
    edges.hydrate.side_effect = hydrate
    edges.monitor.process_update.side_effect = update
    try:
        await adapter.assess_eisv(adapter.EisvRequest("agent", {}))
    finally:
        marker.reset(marker_token)

    assert executor_calls == [None, None]
    assert [row[0] for row in observations] == ["monitor", "hydrate", "update"]
    assert observations[1] == ("hydrate", caller_thread, "caller-context")
    for _, thread_id, context_value in (observations[0], observations[2]):
        assert thread_id != caller_thread
        assert context_value == "worker-default"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["monitor", "hydrate", "update"])
async def test_errors_propagate_unchanged_and_failed_spans_emit_no_success_metric(edges, failure_stage):
    error = RuntimeError(f"{failure_stage} failed")
    target = {
        "monitor": edges.get_monitor,
        "hydrate": edges.hydrate,
        "update": edges.monitor.process_update,
    }[failure_stage]
    target.side_effect = error

    with pytest.raises(RuntimeError) as caught:
        await adapter.assess_eisv(adapter.EisvRequest("agent", {}))

    assert caught.value is error
    assert metric_names(edges) == ([] if failure_stage != "update" else ["ode.monitor_setup_ms"])
    if failure_stage == "monitor":
        edges.hydrate.assert_not_awaited()
    if failure_stage != "update":
        edges.monitor.process_update.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["monitor", "update"])
async def test_cancelling_executor_await_does_not_cancel_running_worker(edges, stage):
    loop = asyncio.get_running_loop()
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    returned = []

    def worker(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        try:
            if not release.wait(timeout=5):
                raise AssertionError("test failed to release executor worker")
            returned.append(stage)
            return edges.monitor if stage == "monitor" else edges.result
        finally:
            loop.call_soon_threadsafe(finished.set)

    target = edges.get_monitor if stage == "monitor" else edges.monitor.process_update
    target.side_effect = worker
    task = asyncio.create_task(adapter.assess_eisv(adapter.EisvRequest("agent", {})))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert not finished.is_set()
        assert returned == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(finished.wait(), timeout=5)

    assert returned == [stage]
    assert metric_names(edges) == ([] if stage == "monitor" else ["ode.monitor_setup_ms"])
    if stage == "monitor":
        edges.hydrate.assert_not_awaited()
        edges.monitor.process_update.assert_not_called()


@pytest.mark.asyncio
async def test_cancelling_hydration_propagates_without_starting_estimator(edges):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def hydrate(*args):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    edges.hydrate.side_effect = hydrate
    task = asyncio.create_task(adapter.assess_eisv(adapter.EisvRequest("agent", {})))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert cancelled.is_set()
    edges.monitor.process_update.assert_not_called()
    edges.metric.assert_not_called()
