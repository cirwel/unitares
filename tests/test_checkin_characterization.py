"""Compatibility observations before separating checkpoints from EISV.

These tests run the real workflow, phase bodies, grounding, enrichments and
response serializer. The existing workflow harness replaces the estimator,
storage, baseline/profile and context edges. Thus these assertions characterize
the orchestration contract, not database atomicity or estimator mathematics.
Top-level success still does not prove persistence. A separate call-local
receipt records only a positive acknowledgement from the state writer.
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest

from src.services.update_workflow_service import run_process_update_workflow
from src.mcp_handlers.updates.context import UpdateContext
from src.state_persistence import (
    STATE_PERSISTENCE_UNCONFIRMED_WARNING,
    StatePersistenceReceipt,
)
from tests.helpers import parse_result
from tests.test_update_workflow_service import (
    _make_real_spine_harness,
    _patch_real_spine_edges,
)


@pytest.fixture
def checkin(monkeypatch):
    """Use an existing identity with explicit caller proof and fixed IO results."""
    monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
    monkeypatch.setenv("UNITARES_PHASE5_EVIDENCE_WRITE", "off")
    harness = _make_real_spine_harness(response_text="Implemented the isolated change.")
    harness.events = []
    harness.lock_held = False

    @asynccontextmanager
    async def lock(*args, **kwargs):
        harness.events.append("lock_acquired")
        harness.lock_held = True
        try:
            yield
        finally:
            harness.lock_held = False
            harness.events.append("lock_released")

    harness.server.lock_manager.acquire_agent_lock_async.side_effect = lock
    estimator_result = harness.server.process_update_authenticated_async.return_value

    async def estimate(**kwargs):
        assert harness.lock_held
        harness.events.append("estimate")
        return estimator_result

    async def persist(**kwargs):
        assert not harness.lock_held
        harness.events.append("state_write")
        return 123

    async def outcome(**kwargs):
        assert not harness.lock_held
        harness.events.append("outcome_write")
        return "outcome-123"

    harness.server.process_update_authenticated_async.side_effect = estimate
    harness.storage.record_agent_state.side_effect = persist
    harness.db.record_outcome_event.side_effect = outcome
    with _patch_real_spine_edges(harness), patch(
        "src.mcp_handlers.context.get_session_resolution_source",
        return_value="explicit_client_session_id",
    ), patch(
        "src.mcp_handlers.context.get_session_proof_origin", return_value="caller_asserted",
    ):
        yield harness


@pytest.mark.asyncio
async def test_checkin_record_and_outcome_follow_lock_release(checkin):
    data = parse_result(await run_process_update_workflow(checkin.ctx))

    assert checkin.events == [
        "lock_acquired", "estimate", "lock_released", "state_write", "outcome_write",
    ]
    # Pin the stable part of the public response; timestamps and optional
    # enrichment diagnostics are intentionally outside this compatibility pin.
    assert {
        "success": data["success"],
        "agent_id": data["agent_id"],
        "prediction_id": data["prediction_id"],
        "outcome_id": data["outcome_event"]["outcome_id"],
        "identity_tier": data["identity_assurance"]["tier"],
    } == {
        "success": True,
        "agent_id": checkin.agent_id,
        "prediction_id": "pred-123",
        "outcome_id": "outcome-123",
        "identity_tier": "strong",
    }
    state = checkin.storage.record_agent_state.await_args.kwargs
    outcome = checkin.db.record_outcome_event.await_args.kwargs
    estimate = checkin.server.process_update_authenticated_async.await_args.kwargs
    assert estimate["agent_state"]["sensor_eisv"] == {
        "E": 0.81, "I": 0.62, "S": 0.18, "V": 0.04,
    }
    assert estimate["confidence"] == pytest.approx(0.93)
    assert estimate["session_bound"] is True
    assert state["report_excerpt"] == checkin.ctx.response_text
    assert state["epistemic_class"] == "agent_report"
    assert state["provenance_context"] == checkin.ctx.agent_state["provenance_context"]
    assert state["sensor_eisv_source"] == checkin.ctx.agent_state.get("sensor_eisv_source")
    assert state["coherence_form"] == checkin.ctx.coherence_form
    assert state["eisv_telemetry"] == checkin.ctx.result["eisv_telemetry"]
    for field in ("E", "I", "S", "V"):
        assert state[field] == outcome[f"eisv_{field.lower()}"] == data["metrics"][field]
    assert outcome["verification_source"] == "agent_reported_tool_result"
    assert outcome["detail"]["source"] == "auto_checkin"
    assert outcome["session_id"] == checkin.session_key
    # This automatic result is not bound to the returned prediction ID.
    assert outcome.get("prediction_id") is None
    assert checkin.ctx.state_persistence == StatePersistenceReceipt(123)
    assert data["state_persistence"] == StatePersistenceReceipt(123).to_dict()
    assert STATE_PERSISTENCE_UNCONFIRMED_WARNING not in checkin.ctx.warnings


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [PermissionError, ValueError, RuntimeError])
async def test_estimator_failure_releases_lock_without_post_update_writes(checkin, failure_type):
    failure = failure_type("injected estimator failure")
    checkin.server.process_update_authenticated_async.side_effect = failure

    with pytest.raises(Exception, match="injected estimator failure") as excinfo:
        await run_process_update_workflow(checkin.ctx)

    if failure_type in (PermissionError, ValueError):
        assert excinfo.value is failure
    else:
        assert str(excinfo.value) == "Error processing update: injected estimator failure"
        assert excinfo.value.__cause__ is failure
    assert checkin.events == ["lock_acquired", "lock_released"]
    checkin.storage.record_agent_state.assert_not_awaited()
    checkin.db.record_outcome_event.assert_not_awaited()
    assert checkin.ctx.state_persistence is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,outcome_expected",
    [
        pytest.param(ValueError("Agent not found in database"), False, id="strict-missing-agent"),
        pytest.param(RuntimeError("database unavailable"), True, id="generic-write-failure"),
    ],
)
async def test_state_write_failure_has_asymmetric_outcome_behavior(checkin, failure, outcome_expected):
    """Pin a known gap: generic storage failure does not stop later outcomes."""
    checkin.storage.record_agent_state.side_effect = failure

    data = parse_result(await run_process_update_workflow(checkin.ctx))

    checkin.server.process_update_authenticated_async.assert_awaited_once()
    checkin.storage.record_agent_state.assert_awaited_once()
    checkin.storage.create_agent.assert_not_awaited()
    assert data["success"] is True
    assert data["prediction_id"] == "pred-123"
    assert not checkin.lock_held
    assert checkin.ctx.state_persistence == StatePersistenceReceipt()
    assert data["state_persistence"] == StatePersistenceReceipt().to_dict()
    assert checkin.ctx.warnings.count(STATE_PERSISTENCE_UNCONFIRMED_WARNING) == 1
    if outcome_expected:
        checkin.db.record_outcome_event.assert_awaited_once()
        assert data["outcome_event"]["outcome_id"] == "outcome-123"
    else:
        checkin.db.record_outcome_event.assert_not_awaited()
        assert "outcome_event" not in data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "acknowledgement",
    [None, False, True, 0, -1, 1.5, "123", {"state_id": 123}, object()],
    ids=["absent", "false", "true", "zero", "negative", "float", "text", "mapping", "object"],
)
async def test_unknown_writer_acknowledgement_is_unconfirmed(checkin, acknowledgement):
    checkin.storage.record_agent_state.side_effect = None
    checkin.storage.record_agent_state.return_value = acknowledgement

    data = parse_result(await run_process_update_workflow(checkin.ctx))

    assert data["success"] is True
    checkin.storage.record_agent_state.assert_awaited_once()
    checkin.db.record_outcome_event.assert_awaited_once()
    assert checkin.ctx.state_persistence == StatePersistenceReceipt()
    assert data["state_persistence"] == StatePersistenceReceipt().to_dict()
    assert checkin.ctx.warnings.count(STATE_PERSISTENCE_UNCONFIRMED_WARNING) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [RuntimeError, ValueError])
async def test_lost_state_acknowledgement_does_not_claim_no_row(checkin, failure_type):
    committed_rows = []

    async def persist_then_lose_acknowledgement(**kwargs):
        committed_rows.append(kwargs)
        raise failure_type("connection lost after commit")

    checkin.storage.record_agent_state.side_effect = persist_then_lose_acknowledgement

    data = parse_result(await run_process_update_workflow(checkin.ctx))

    assert data["success"] is True
    assert len(committed_rows) == 1
    checkin.storage.record_agent_state.assert_awaited_once()
    checkin.storage.create_agent.assert_not_awaited()
    assert checkin.ctx.state_persistence == StatePersistenceReceipt()
    assert data["state_persistence"] == StatePersistenceReceipt().to_dict()
    assert checkin.ctx.warnings.count(STATE_PERSISTENCE_UNCONFIRMED_WARNING) == 1
    # Keep the legacy strict-ValueError short circuit, even if a row committed.
    assert checkin.db.record_outcome_event.await_count == int(failure_type is RuntimeError)


@pytest.mark.asyncio
async def test_state_write_cancellation_propagates_with_unconfirmed_receipt(checkin):
    write_entered = asyncio.Event()

    async def pending_write(**kwargs):
        write_entered.set()
        await asyncio.Future()

    checkin.storage.record_agent_state.side_effect = pending_write
    task = asyncio.create_task(run_process_update_workflow(checkin.ctx))
    try:
        await asyncio.wait_for(write_entered.wait(), timeout=5)
        assert checkin.ctx.state_persistence == StatePersistenceReceipt()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert checkin.ctx.state_persistence == StatePersistenceReceipt()
    assert checkin.ctx.warnings.count(STATE_PERSISTENCE_UNCONFIRMED_WARNING) == 1
    checkin.storage.create_agent.assert_not_awaited()
    checkin.db.record_outcome_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_outcome_failure_preserves_response_and_prior_state_write(checkin):
    checkin.db.record_outcome_event.side_effect = RuntimeError("outcome writer unavailable")

    data = parse_result(await run_process_update_workflow(checkin.ctx))

    checkin.storage.record_agent_state.assert_awaited_once()
    checkin.db.record_outcome_event.assert_awaited_once()
    assert data["success"] is True
    assert data["agent_id"] == checkin.agent_id
    assert data["prediction_id"] == "pred-123"
    assert "outcome_event" not in data
    assert checkin.ctx.state_persistence == StatePersistenceReceipt(123)
    assert data["state_persistence"] == StatePersistenceReceipt(123).to_dict()
    assert STATE_PERSISTENCE_UNCONFIRMED_WARNING not in checkin.ctx.warnings


@pytest.mark.asyncio
async def test_unexpired_pause_refuses_before_lazy_persistence_or_lock(checkin):
    checkin.meta.status = "paused"
    with patch(
        "src.mcp_handlers.support.pause_ttl.maybe_auto_expire_pause_async",
        new=AsyncMock(return_value=False),
    ), patch(
        "src.mcp_handlers.identity.handlers.ensure_agent_persisted", new=AsyncMock(),
    ) as lazy_persist:
        data = parse_result(await run_process_update_workflow(checkin.ctx))

    assert data["success"] is False
    assert data["error_code"] == "AGENT_PAUSED"
    assert checkin.meta.status == "paused"
    assert checkin.events == []
    lazy_persist.assert_not_awaited()
    checkin.server.lock_manager.acquire_agent_lock_async.assert_not_called()
    checkin.server.process_update_authenticated_async.assert_not_awaited()
    checkin.storage.record_agent_state.assert_not_awaited()
    checkin.db.record_outcome_event.assert_not_awaited()
    assert checkin.ctx.state_persistence is None
    assert "state_persistence" not in data


@pytest.mark.asyncio
async def test_second_checkin_can_finish_while_first_state_write_is_pending(checkin):
    """The per-agent estimator lock does not order post-update persistence."""
    mutex = asyncio.Lock()
    first_write_entered = asyncio.Event()
    release_first_write = asyncio.Event()
    completion_order = []
    estimation_count = 0
    write_count = 0
    estimator_result = checkin.server.process_update_authenticated_async.return_value

    @asynccontextmanager
    async def lock(*args, **kwargs):
        async with mutex:
            yield

    async def estimate(**kwargs):
        nonlocal estimation_count
        assert mutex.locked()
        estimation_count += 1
        # Each call owns its result, as the production estimator does. Both
        # contexts still reference the same monitor, which this test stubs.
        return deepcopy(estimator_result)

    async def persist(**kwargs):
        nonlocal write_count
        assert not mutex.locked()
        write_count += 1
        this_write = write_count
        if this_write == 1:
            first_write_entered.set()
            await release_first_write.wait()
        completion_order.append(this_write)
        return 100 + this_write

    checkin.server.lock_manager.acquire_agent_lock_async.side_effect = lock
    checkin.server.process_update_authenticated_async.side_effect = estimate
    checkin.storage.record_agent_state.side_effect = persist
    second_ctx = UpdateContext(
        arguments=dict(checkin.ctx.arguments), mcp_server=checkin.server,
    )
    tasks = [asyncio.create_task(run_process_update_workflow(checkin.ctx))]
    try:
        await asyncio.wait_for(first_write_entered.wait(), timeout=5)
        assert checkin.ctx.state_persistence == StatePersistenceReceipt()
        assert second_ctx.state_persistence is None
        tasks.append(asyncio.create_task(run_process_update_workflow(second_ctx)))
        second_result = await asyncio.wait_for(tasks[1], timeout=5)
        assert parse_result(second_result)["success"] is True
        assert parse_result(second_result)["state_persistence"] == StatePersistenceReceipt(102).to_dict()
        assert estimation_count == 2
        assert completion_order == [2]
        assert not tasks[0].done()
        assert second_ctx.state_persistence == StatePersistenceReceipt(102)
        assert checkin.ctx.state_persistence == StatePersistenceReceipt()
    finally:
        release_first_write.set()
        # Bound cleanup too: a failing assertion must not leave a test task
        # blocked on a barrier after patches have been removed.
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=5,
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    assert parse_result(results[0])["success"] is True
    assert parse_result(results[0])["state_persistence"] == StatePersistenceReceipt(101).to_dict()
    assert completion_order == [2, 1]
    assert checkin.db.record_outcome_event.await_count == 2
    assert checkin.ctx.state_persistence == StatePersistenceReceipt(101)
    assert second_ctx.state_persistence == StatePersistenceReceipt(102)
    assert STATE_PERSISTENCE_UNCONFIRMED_WARNING not in checkin.ctx.warnings
    assert STATE_PERSISTENCE_UNCONFIRMED_WARNING not in second_ctx.warnings


@pytest.mark.asyncio
@pytest.mark.parametrize("lose_acknowledgement", [False, True])
async def test_receipt_matches_live_postgres_acknowledgement(
    checkin, live_postgres_backend, lose_acknowledgement,
):
    """A real state row may exist with either a received or lost acknowledgement."""
    from src import agent_storage

    backend = live_postgres_backend
    await backend.upsert_agent(checkin.agent_id, "test-state-receipt-key")
    await backend.upsert_identity(checkin.agent_id, "test-state-receipt-key-hash")
    state_ids = []

    async def persist(**kwargs):
        state_id = await agent_storage.record_agent_state(**kwargs)
        state_ids.append(state_id)
        if lose_acknowledgement:
            raise RuntimeError("acknowledgement lost after real PostgreSQL write")
        return state_id

    checkin.storage.record_agent_state.side_effect = persist
    with patch("src.agent_storage.get_db", return_value=backend):
        data = parse_result(await run_process_update_workflow(checkin.ctx))

    assert data["success"] is True
    assert len(state_ids) == 1
    # A separate acquisition observes the committed row, after the writer exited.
    async with backend.acquire() as conn:
        state_id = await conn.fetchval(
            "SELECT state_id FROM core.agent_state WHERE state_id = $1",
            state_ids[0],
        )
    assert state_id == state_ids[0]
    expected = StatePersistenceReceipt(None if lose_acknowledgement else state_id)
    assert checkin.ctx.state_persistence == expected
    assert data["state_persistence"] == expected.to_dict()
    assert checkin.ctx.warnings.count(STATE_PERSISTENCE_UNCONFIRMED_WARNING) == int(
        lose_acknowledgement
    )
