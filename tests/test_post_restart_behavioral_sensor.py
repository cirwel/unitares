"""Post-restart behavioral sensor preparation via assess_eisv."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_restored_monitor(n: int = 8, *, void_active: bool = True) -> MagicMock:
    monitor = MagicMock()
    monitor.state.decision_history = ["proceed"] * n
    monitor.state.coherence_history = [0.5] * n
    monitor.state.regime_history = ["CONVERGENCE"] * n
    monitor.state.E_history = [0.75] * n
    monitor.state.I_history = [0.70] * n
    monitor.state.S_history = [0.25] * n
    monitor.state.V_history = [0.05] * n
    monitor.state.void_active = void_active
    monitor._last_drift_vector = MagicMock(
        norm=0.2,
        calibration_deviation=0.1,
        complexity_divergence=0.05,
        coherence_deviation=0.0,
        stability_deviation=0.2,
    )
    monitor._last_continuity_metrics = MagicMock(
        complexity_divergence=0.05,
        E_input=0.8,
        I_input=0.7,
        S_input=0.2,
        self_complexity_defaulted=False,
        E_input_clipped=False,
        continuity_degenerate=False,
    )
    monitor._last_continuity_metrics.response_text = "Completed work"
    monitor._last_continuity_metrics.epistemic_class = "agent_report"
    monitor.process_update.return_value = {
        "status": "healthy",
        "decision": {"action": "proceed", "sub_action": "approve"},
        "metrics": {"E": 0.75, "I": 0.70, "S": 0.25, "V": 0.05, "risk_score": 0.1},
    }
    return monitor


def _make_ctx(agent_id: str = "restored-agent"):
    from src import agent_loop_detection as ald
    from src.mcp_handlers.updates.context import UpdateContext

    mcp_server = MagicMock()
    mcp_server.monitors = {}  # Empty cache right after server restart
    mcp_server.agent_metadata = {
        agent_id: SimpleNamespace(
            status="active",
            api_key="fake-key",
            purpose="testing",
            active_session_key="sess-1",
            thread_id="thread-1",
            node_index=1,
            loop_cooldown_until=None,
            loop_detected_at=None,
            loop_incidents=[],
            recent_update_timestamps=[],
            recent_decisions=[],
            total_updates=8,
            last_update=None,
            add_recent_update=MagicMock(),
        )
    }
    mcp_server.process_update_authenticated_async = ald.process_update_authenticated_async
    return UpdateContext(
        agent_id=agent_id,
        agent_uuid=agent_id,
        arguments={"client_session_id": "sess-1"},
        response_text="Completed the implementation and ran tests.",
        complexity=0.4,
        ethical_drift=[0.0, 0.0, 0.0],
        is_new_agent=False,
        meta=mcp_server.agent_metadata[agent_id],
        loop=asyncio.get_running_loop(),
        mcp_server=mcp_server,
    )


@pytest.mark.asyncio
async def test_post_restart_first_checkin_runs_behavioral_sensor_and_captures_void():
    from src import agent_lifecycle, agent_loop_detection as ald, agent_monitor_state
    from src.mcp_handlers.updates.phases import execute_locked_update, prepare_unlocked_inputs
    from src.services.checkpoint_service import CheckpointBoundaryStop  # noqa: F401

    restored_monitor = _make_restored_monitor(n=8, void_active=True)
    ctx = _make_ctx()
    outcomes = [{"outcome_type": "task_completed", "is_bad": False, "outcome_score": 0.9}] * 3
    mock_db = MagicMock()
    mock_db.get_recent_outcomes = AsyncMock(return_value=outcomes)
    mock_db.load_agent_baseline = AsyncMock(return_value=None)

    seen_in_process_update = {}

    def _capture_update(agent_state, **kwargs):
        seen_in_process_update["has_callback"] = "_prepare_restored_monitor" in agent_state
        seen_in_process_update["sensor_eisv_source"] = agent_state.get("sensor_eisv_source")
        seen_in_process_update["has_sensor_eisv"] = "sensor_eisv" in agent_state
        return restored_monitor.process_update.return_value

    restored_monitor.process_update.side_effect = _capture_update

    with (
        patch("src.mcp_handlers.updates.phases.agent_storage", MagicMock(
            get_agent=AsyncMock(return_value=SimpleNamespace(api_key="fake-key"))
        )),
        patch("src.calibration.calibration_checker", MagicMock(
            compute_calibration_metrics=MagicMock(return_value={}),
            compute_agent_calibration_candidate=MagicMock(return_value={"evidence_status": "unavailable"}),
            compute_agent_tactical_calibration_channel=MagicMock(return_value={"evidence_status": "unavailable"}),
        )),
        patch("src.agent_behavioral_baseline.ensure_baseline_loaded", AsyncMock(return_value=None)),
        patch("src.agent_behavioral_baseline.compute_anomaly_entropy", MagicMock(return_value=0.0)),
        patch("src.tool_usage_tracker.get_tool_usage_tracker",
              return_value=MagicMock(get_usage_stats=MagicMock(return_value={"total_calls": 0}))),
        patch("src.db.get_db", return_value=mock_db),
        patch("src.services.checkpoint_service.accept_before_assessment", AsyncMock()),
        patch.object(ald, "agent_metadata", ctx.mcp_server.agent_metadata),
        patch.object(ald, "verify_agent_ownership", MagicMock(return_value=(True, None))),
        patch.object(ald, "detect_loop_pattern", MagicMock(return_value=(False, ""))),
        patch.object(agent_lifecycle, "get_or_create_monitor", MagicMock(return_value=restored_monitor)),
        patch.object(agent_monitor_state, "hydrate_from_db_if_fresh", AsyncMock(return_value=True)),
    ):
        await prepare_unlocked_inputs(ctx)
        assert callable(ctx.agent_state.get("_prepare_restored_monitor"))
        assert "sensor_eisv" not in ctx.agent_state

        early_exit = await execute_locked_update(ctx)

    assert early_exit is None
    assert seen_in_process_update == {
        "has_callback": False,
        "sensor_eisv_source": "behavioral",
        "has_sensor_eisv": True,
    }
    assert "_prepare_restored_monitor" not in ctx.agent_state
    assert ctx.agent_state["sensor_eisv_source"] == "behavioral"
    assert ctx.agent_state["_eisv_derivation"]["kind"] == "behavioral_sensor"
    assert ctx.previous_void_active is True
    mock_db.get_recent_outcomes.assert_awaited_once()


@pytest.mark.asyncio
async def test_checkpoint_rejection_skips_monitor_creation_and_cleans_up_callback():
    from src import agent_lifecycle, agent_loop_detection as ald
    from src.mcp_handlers.updates.phases import execute_locked_update, prepare_unlocked_inputs
    from src.services.checkpoint_service import CheckpointBoundaryStop

    ctx = _make_ctx()
    get_monitor = MagicMock()
    mock_db = MagicMock()
    mock_db.load_agent_baseline = AsyncMock(return_value=None)

    with (
        patch("src.mcp_handlers.updates.phases.agent_storage", MagicMock(
            get_agent=AsyncMock(return_value=SimpleNamespace(api_key="fake-key"))
        )),
        patch("src.agent_behavioral_baseline.ensure_baseline_loaded", AsyncMock(return_value=None)),
        patch("src.agent_behavioral_baseline.compute_anomaly_entropy", MagicMock(return_value=0.0)),
        patch("src.db.get_db", return_value=mock_db),
        patch(
            "src.services.checkpoint_service.accept_before_assessment",
            AsyncMock(side_effect=CheckpointBoundaryStop("CHECKPOINT_REPLAY", "replay", replay=True)),
        ),
        patch.object(ald, "agent_metadata", ctx.mcp_server.agent_metadata),
        patch.object(ald, "verify_agent_ownership", MagicMock(return_value=(True, None))),
        patch.object(ald, "detect_loop_pattern", MagicMock(return_value=(False, ""))),
        patch.object(agent_lifecycle, "get_or_create_monitor", get_monitor),
    ):
        await prepare_unlocked_inputs(ctx)
        assert "_prepare_restored_monitor" in ctx.agent_state
        with pytest.raises(CheckpointBoundaryStop):
            await execute_locked_update(ctx)

    get_monitor.assert_not_called()
    assert "_prepare_restored_monitor" not in ctx.agent_state
