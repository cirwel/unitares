"""Checkpoint capture follows admission and precedes every monitor effect."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.db.mixins.identity import CheckpointIdentityRefused, IdentityMixin
from src.services.checkpoint_service import CheckpointBoundaryStop


@pytest.fixture
def update_edges(monkeypatch):
    from src import agent_lifecycle, agent_loop_detection as ald, agent_monitor_state
    from src.eisv import adapter

    events = []
    meta = SimpleNamespace(
        status="active",
        loop_cooldown_until=None,
        loop_detected_at=None,
        loop_incidents=[],
        recent_update_timestamps=[],
        add_lifecycle_event=Mock(),
    )
    monitor = SimpleNamespace(
        process_update=Mock(side_effect=lambda *args, **kwargs: (
            events.append("update") or {
                "decision": {"action": "pause", "reason": "measured risk"},
            }
        )),
    )
    ownership = Mock(side_effect=lambda *args: events.append("auth") or (True, None))
    loop_check = Mock(side_effect=lambda *args: events.append("loop") or (False, ""))
    get_monitor = Mock(side_effect=lambda *args: events.append("monitor") or monitor)
    hydrate = AsyncMock(side_effect=lambda *args: events.append("hydrate"))
    save = AsyncMock()
    monkeypatch.setenv("UNITARES_POLICY_ACTUATION", "observe")
    monkeypatch.setattr(ald, "agent_metadata", {"checkpoint-agent": meta})
    monkeypatch.setattr(ald, "verify_agent_ownership", ownership)
    monkeypatch.setattr(ald, "detect_loop_pattern", loop_check)
    monkeypatch.setattr(ald, "save_monitor_state_async", save)
    monkeypatch.setattr(agent_lifecycle, "get_or_create_monitor", get_monitor)
    monkeypatch.setattr(agent_monitor_state, "hydrate_from_db_if_fresh", hydrate)
    # Observe delegation while retaining the real adapter and its executor work.
    assess = AsyncMock(wraps=adapter.assess_eisv)
    monkeypatch.setattr(adapter, "assess_eisv", assess)
    return SimpleNamespace(
        ald=ald, meta=meta, monitor=monitor, events=events,
        ownership=ownership, loop_check=loop_check,
        get_monitor=get_monitor, hydrate=hydrate, save=save, assess=assess,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("split", [False, True])
async def test_wrapper_and_split_preserve_assessment_behavior(update_edges, split):
    edge = update_edges
    state = {"task_type": "convergent", "complexity": 0.4}
    if split:
        admission = await edge.ald.validate_update_admission_async(
            "checkpoint-agent", "test-key", session_bound=True,
        )
        assert edge.events == ["auth", "loop"]
        edge.get_monitor.assert_not_called()
        edge.hydrate.assert_not_awaited()
        edge.monitor.process_update.assert_not_called()
        result = await edge.ald.execute_admitted_update_async(
            admission, state, auto_save=False, confidence=0.7, task_type="divergent",
        )
    else:
        result = await edge.ald.process_update_authenticated_async(
            "checkpoint-agent", "test-key", state,
            auto_save=False, confidence=0.7, task_type="divergent", session_bound=True,
        )

    edge.ownership.assert_called_once_with("checkpoint-agent", "test-key", True)
    assert edge.events == ["auth", "loop", "monitor", "hydrate", "update"]
    # Preserve the legacy source: the state field overrides the separate argument.
    edge.monitor.process_update.assert_called_once_with(
        state, confidence=0.7, task_type="convergent",
    )
    edge.assess.assert_awaited_once()
    request = edge.assess.await_args.args[0]
    assert request.agent_id == "checkpoint-agent"
    assert request.agent_state is state
    assert request.confidence == 0.7
    assert result["decision"]["action"] == "proceed"
    assert result["decision"]["recommended_action"] == "pause"
    assert result["enforcement"]["posture"] == "observe"
    edge.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_follows_admission_and_captured_posture(update_edges, monkeypatch):
    edge = update_edges

    async def commit_checkpoint():
        edge.events.append("checkpoint")
        # The operator posture is captured once, before the new commit await.
        monkeypatch.setenv("UNITARES_POLICY_ACTUATION", "enforce")

    result = await edge.ald.process_update_authenticated_async(
        "checkpoint-agent", "test-key", {}, auto_save=False,
        before_assessment=commit_checkpoint,
    )

    assert edge.events == ["auth", "loop", "checkpoint", "monitor", "hydrate", "update"]
    assert result["enforcement"]["posture"] == "observe"
    assert result["decision"]["action"] == "proceed"


@pytest.mark.asyncio
async def test_request_is_constructed_after_checkpoint_and_passed_to_real_adapter(update_edges, monkeypatch):
    from src.eisv import adapter

    edge = update_edges
    request_class = adapter.EisvRequest
    state = {"_outcome_history": []}
    raw_result = {"decision": {"action": "pause", "reason": "measured risk"}}

    def request(*args, **kwargs):
        edge.events.append("request")
        assert kwargs["agent_state"]["_checkpoint_id"] == "accepted-checkpoint"
        return request_class(*args, **kwargs)

    def update(*args, **kwargs):
        edge.events.append("update")
        assert args[0] is state
        return raw_result

    async def accept():
        edge.events.append("checkpoint")
        state["_checkpoint_id"] = "accepted-checkpoint"

    monkeypatch.setattr(adapter, "EisvRequest", request)
    edge.monitor.process_update.side_effect = update
    result = await edge.ald.process_update_authenticated_async(
        "checkpoint-agent", "test-key", state,
        auto_save=False, before_assessment=accept,
    )

    assert edge.events == ["auth", "loop", "checkpoint", "request", "monitor", "hydrate", "update"]
    assert result is raw_result
    assert result["decision"]["action"] == "proceed"
    assert result["decision"]["recommended_action"] == "pause"
    edge.save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refusal", "exception", "message"),
    [
        ("auth", PermissionError, "Authentication failed"),
        ("posture", ValueError, "must be one of enforce, observe"),
        ("archive", ValueError, "archived and cannot be updated"),
        ("cooldown", ValueError, "Cooldown expires in 2.5 seconds"),
    ],
)
async def test_admission_refusal_never_calls_checkpoint_or_monitor(
    update_edges, monkeypatch, refusal, exception, message,
):
    edge = update_edges
    if refusal == "auth":
        edge.ownership.side_effect = None
        edge.ownership.return_value = (False, "invalid key")
    elif refusal == "posture":
        monkeypatch.setenv("UNITARES_POLICY_ACTUATION", "invalid")
    elif refusal == "archive":
        edge.meta.status = "archived"
    else:
        edge.loop_check.side_effect = None
        edge.loop_check.return_value = (True, "Loop cooldown active. Wait 2.5s before retrying.")
    commit_checkpoint = AsyncMock()

    with pytest.raises(exception, match=message):
        await edge.ald.process_update_authenticated_async(
            "checkpoint-agent", "test-key", {},
            before_assessment=commit_checkpoint,
        )

    commit_checkpoint.assert_not_awaited()
    edge.get_monitor.assert_not_called()
    edge.hydrate.assert_not_awaited()
    edge.monitor.process_update.assert_not_called()
    edge.save.assert_not_awaited()
    edge.assess.assert_not_awaited()
    if refusal != "cooldown":
        edge.loop_check.assert_not_called()


@pytest.mark.asyncio
async def test_new_loop_refusal_keeps_existing_cooldown_effects(update_edges):
    edge = update_edges
    edge.loop_check.side_effect = None
    edge.loop_check.return_value = (True, "Rapid-fire updates detected")
    commit_checkpoint = AsyncMock()

    with pytest.raises(ValueError, match="Updates blocked for 5 seconds"):
        await edge.ald.process_update_authenticated_async(
            "checkpoint-agent", "test-key", {},
            before_assessment=commit_checkpoint,
        )

    assert edge.meta.loop_cooldown_until is not None
    assert edge.meta.loop_detected_at is not None
    assert len(edge.meta.loop_incidents) == 1
    assert edge.meta.loop_incidents[0]["cooldown_seconds"] == 5
    edge.meta.add_lifecycle_event.assert_called_once_with(
        "loop_detected", "Rapid-fire updates detected",
    )
    commit_checkpoint.assert_not_awaited()
    edge.get_monitor.assert_not_called()
    edge.assess.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", [
    RuntimeError("checkpoint write unconfirmed"),
    CheckpointBoundaryStop("CHECKPOINT_REPLAY", "existing checkpoint", replay=True),
    CheckpointBoundaryStop("CHECKPOINT_KEY_CONFLICT", "conflicting content"),
    asyncio.CancelledError(),
])
async def test_checkpoint_exception_stops_before_monitor_setup(update_edges, rejection):
    edge = update_edges
    commit_checkpoint = AsyncMock(side_effect=rejection)

    with pytest.raises(type(rejection)) as caught:
        await edge.ald.process_update_authenticated_async(
            "checkpoint-agent", "test-key", {},
            before_assessment=commit_checkpoint,
        )

    assert caught.value is rejection
    commit_checkpoint.assert_awaited_once_with()
    assert edge.events == ["auth", "loop"]
    edge.get_monitor.assert_not_called()
    edge.monitor.process_update.assert_not_called()
    edge.assess.assert_not_awaited()


@pytest.mark.asyncio
async def test_admission_is_frozen_and_mapping_cannot_bypass_it(update_edges):
    edge = update_edges
    admission = await edge.ald.validate_update_admission_async(
        "checkpoint-agent", "test-key",
    )
    with pytest.raises(FrozenInstanceError):
        admission.posture = "enforce"
    with pytest.raises(TypeError, match="internal update admission"):
        await edge.ald.execute_admitted_update_async(
            {"agent_id": "checkpoint-agent", "posture": "observe"}, {},
        )
    edge.get_monitor.assert_not_called()
    edge.assess.assert_not_awaited()


@pytest.mark.asyncio
async def test_checkpoint_identity_uses_callers_transaction_connection():
    conn = Mock()
    conn.fetchrow = AsyncMock(return_value={
        "identity_id": 41,
        "agent_status": "active",
        "identity_status": "active",
    })
    identity_id = await IdentityMixin().lock_active_checkpoint_identity(
        conn, "checkpoint-agent",
    )
    assert identity_id == 41
    sql, agent_id = conn.fetchrow.call_args.args
    assert "FOR UPDATE OF a, i" in sql
    assert agent_id == "checkpoint-agent"
    conn.execute.assert_not_called()
    conn.transaction.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("statuses", "reason"),
    [
        (None, "durable_identity_missing"),
        (("active", "paused"), "durable_status_mismatch"),
        (("archived", "active"), "durable_status_mismatch"),
        (("paused", "paused"), "identity_not_active"),
        (("archived", "archived"), "identity_not_active"),
        (("deleted", "deleted"), "identity_not_active"),
        (("unknown", "unknown"), "identity_not_active"),
        ((None, None), "identity_not_active"),
    ],
)
async def test_checkpoint_identity_refuses_missing_inactive_or_split_state(statuses, reason):
    row = None if statuses is None else {
        "identity_id": 41,
        "agent_status": statuses[0],
        "identity_status": statuses[1],
    }
    conn = Mock()
    conn.fetchrow = AsyncMock(return_value=row)

    with pytest.raises(CheckpointIdentityRefused) as caught:
        await IdentityMixin().lock_active_checkpoint_identity(conn, "checkpoint-agent")

    assert caught.value.reason == reason
    assert caught.value.agent_status == (statuses[0] if statuses else None)
    assert caught.value.identity_status == (statuses[1] if statuses else None)
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_checkpoint_receipt_scope_verification_has_no_lifecycle_gate():
    conn = Mock()
    conn.fetchval = AsyncMock(return_value=41)

    await IdentityMixin().verify_checkpoint_identity_scope(conn, "checkpoint-agent", 41)

    sql, agent_id = conn.fetchval.call_args.args
    assert agent_id == "checkpoint-agent"
    assert "status" not in sql
    assert "FOR UPDATE" not in sql
    conn.execute.assert_not_called()
    conn.transaction.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "stored", "reason"),
    [
        (41, None, "durable_identity_missing"),
        (41, 42, "identity_scope_mismatch"),
        (True, 1, "identity_scope_mismatch"),
        ("41", 41, "identity_scope_mismatch"),
        (0, 0, "identity_scope_mismatch"),
        (-1, -1, "identity_scope_mismatch"),
    ],
)
async def test_checkpoint_scope_refuses_missing_or_another_identity(requested, stored, reason):
    conn = Mock()
    conn.fetchval = AsyncMock(return_value=stored)

    with pytest.raises(CheckpointIdentityRefused) as caught:
        await IdentityMixin().verify_checkpoint_identity_scope(conn, "checkpoint-agent", requested)

    assert caught.value.reason == reason
    conn.execute.assert_not_called()
