"""Post-restart behavioral sensor integration with live drift and continuity."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_restored_monitor(
    agent_id: str = "restored-agent",
    n: int = 8,
    *,
    void_active: bool = True,
):
    """Build a real UNITARESMonitor in post-restart state (_last_* are None)."""
    from src.governance_monitor import UNITARESMonitor

    monitor = UNITARESMonitor(agent_id=agent_id, load_state=False)
    monitor.state.decision_history = ["proceed"] * n
    monitor.state.coherence_history = [0.5] * n
    monitor.state.regime_history = ["CONVERGENCE"] * n
    monitor.state.E_history = [0.75] * n
    monitor.state.I_history = [0.70] * n
    monitor.state.S_history = [0.25] * n
    monitor.state.V_history = [0.05] * n
    monitor.state.risk_history = [0.1] * n
    monitor.state.void_active = void_active
    monitor.state.update_count = n
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
        arguments={"client_session_id": "sess-1", "confidence": 0.8},
        response_text=(
            "Completed the EISV post-restart continuity and drift fix, updated "
            "phases.py and governance_monitor.py, and verified regression tests."
        ),
        complexity=0.4,
        confidence=0.8,
        ethical_drift=[0.05, 0.0, 0.0],
        is_new_agent=False,
        meta=mcp_server.agent_metadata[agent_id],
        loop=asyncio.get_running_loop(),
        mcp_server=mcp_server,
    )


def _agent_channel():
    return {
        "evidence_status": "available",
        "freshness_status": "recent",
        "n_verified_pairs": 6,
        "calibration_error": 0.08,
        "value": 0.92,
    }


def _mock_calibration_checker():
    return MagicMock(
        compute_calibration_metrics=MagicMock(return_value={}),
        compute_agent_calibration_candidate=MagicMock(
            return_value={"evidence_status": "unavailable"}
        ),
        compute_agent_tactical_calibration_channel=MagicMock(
            return_value=_agent_channel()
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("hold_unread", ["0", "1"])
async def test_post_restart_first_checkin_uses_live_drift_continuity_and_db_inputs(
    tmp_path, monkeypatch, hold_unread
):
    from src import (
        agent_lifecycle,
        agent_loop_detection as ald,
        agent_monitor_state,
        audit_log,
        drift_telemetry,
        governance_monitor as gm,
    )
    from src.mcp_handlers.updates.phases import execute_locked_update, prepare_unlocked_inputs

    monkeypatch.setenv("UNITARES_HOLD_UNREAD_AXES", hold_unread)
    restored_monitor = _make_restored_monitor(n=8, void_active=True)
    # Real restarts never persist _last_drift_vector or _last_continuity_metrics.
    assert restored_monitor._last_drift_vector is None
    assert restored_monitor._last_continuity_metrics is None

    ctx = _make_ctx()
    outcomes = [{"outcome_type": "task_completed", "is_bad": False, "outcome_score": 0.9}] * 3
    mock_db = MagicMock()
    mock_db.get_recent_outcomes = AsyncMock(return_value=outcomes)
    mock_db.load_agent_baseline = AsyncMock(return_value=None)
    cal_checker = _mock_calibration_checker()

    with (
        patch("src.mcp_handlers.updates.phases.agent_storage", MagicMock(
            get_agent=AsyncMock(return_value=SimpleNamespace(api_key="fake-key"))
        )),
        patch("src.calibration.calibration_checker", cal_checker),
        patch.object(gm, "calibration_checker", cal_checker),
        patch.object(audit_log.audit_logger, "log_file", tmp_path / "audit.jsonl"),
        patch.object(
            drift_telemetry,
            "_telemetry",
            drift_telemetry.DriftTelemetry(data_dir=tmp_path / "telemetry"),
        ),
        patch("src.agent_behavioral_baseline.ensure_baseline_loaded", AsyncMock(return_value=None)),
        patch("src.agent_behavioral_baseline.compute_anomaly_entropy", MagicMock(return_value=0.0)),
        patch(
            "src.tool_usage_tracker.get_tool_usage_tracker",
            return_value=MagicMock(get_usage_stats=MagicMock(return_value={"total_calls": 0})),
        ),
        patch("src.db.get_db", return_value=mock_db),
        patch("src.services.checkpoint_service.accept_before_assessment", AsyncMock()),
        patch.object(ald, "agent_metadata", ctx.mcp_server.agent_metadata),
        patch.object(ald, "verify_agent_ownership", MagicMock(return_value=(True, None))),
        patch.object(ald, "detect_loop_pattern", MagicMock(return_value=(False, ""))),
        patch.object(ald, "save_monitor_state_async", AsyncMock()),
        patch.object(agent_lifecycle, "get_or_create_monitor", MagicMock(return_value=restored_monitor)),
        patch.object(agent_monitor_state, "hydrate_from_db_if_fresh", AsyncMock(return_value=True)),
    ):
        await prepare_unlocked_inputs(ctx)
        assert callable(ctx.agent_state.get("_prepare_restored_monitor"))
        assert isinstance(ctx.agent_state.get("_behavioral_sensor_context"), dict)
        assert "sensor_eisv" not in ctx.agent_state

        early_exit = await execute_locked_update(ctx)

    assert early_exit is None
    assert ctx.previous_void_active is True
    assert ctx.agent_state["sensor_eisv_source"] == "behavioral"
    assert restored_monitor._behavioral_obs_source == "behavioral"
    assert "_prepare_restored_monitor" not in ctx.agent_state
    assert "_behavioral_sensor_context" not in ctx.agent_state

    derivation = ctx.agent_state["_eisv_derivation"]
    assert derivation["kind"] == "behavioral_sensor"
    features = derivation["inputs"]["features"]
    assert features["drift_norm"] is not None
    assert features["complexity_divergence"] is not None
    assert len(derivation["inputs"]["outcomes"]) == 3
    i_comps = {
        c["name"]: c for c in derivation["components"]["dimensions"]["I"]["components"]
    }
    assert i_comps["calibration_accuracy"]["source"] == "agent_tactical_calibration"
    # With live drift and continuity supplied, S must not be blanked under HU1=1.
    assert ctx.agent_state["_behavioral_reading"]["S"] is not None
    mock_db.get_recent_outcomes.assert_awaited_once()


@pytest.mark.asyncio
async def test_unhydrated_cached_monitor_restores_void_and_uses_live_drift(tmp_path):
    from src import (
        agent_lifecycle,
        agent_loop_detection as ald,
        agent_monitor_state,
        audit_log,
        drift_telemetry,
        governance_monitor as gm,
    )
    from src.mcp_handlers.updates.phases import execute_locked_update, prepare_unlocked_inputs

    cached_monitor = _make_restored_monitor(n=0, void_active=False)
    cached_monitor._needs_hydration = True

    ctx = _make_ctx()
    ctx.mcp_server.monitors[ctx.agent_id] = cached_monitor
    outcomes = [{"outcome_type": "task_completed", "is_bad": False, "outcome_score": 0.9}] * 3
    mock_db = MagicMock()
    mock_db.get_recent_outcomes = AsyncMock(return_value=outcomes)
    mock_db.load_agent_baseline = AsyncMock(return_value=None)
    cal_checker = _mock_calibration_checker()

    async def _hydrate(monitor, agent_id):
        monitor.state.decision_history = ["proceed"] * 8
        monitor.state.coherence_history = [0.5] * 8
        monitor.state.regime_history = ["CONVERGENCE"] * 8
        monitor.state.E_history = [0.75] * 8
        monitor.state.I_history = [0.70] * 8
        monitor.state.S_history = [0.25] * 8
        monitor.state.V_history = [0.05] * 8
        monitor.state.risk_history = [0.1] * 8
        monitor.state.void_active = True
        monitor.state.update_count = 8
        monitor._needs_hydration = False
        return True

    with (
        patch("src.mcp_handlers.updates.phases.agent_storage", MagicMock(
            get_agent=AsyncMock(return_value=SimpleNamespace(api_key="fake-key"))
        )),
        patch("src.calibration.calibration_checker", cal_checker),
        patch.object(gm, "calibration_checker", cal_checker),
        patch.object(audit_log.audit_logger, "log_file", tmp_path / "audit.jsonl"),
        patch.object(
            drift_telemetry,
            "_telemetry",
            drift_telemetry.DriftTelemetry(data_dir=tmp_path / "telemetry"),
        ),
        patch("src.agent_behavioral_baseline.ensure_baseline_loaded", AsyncMock(return_value=None)),
        patch("src.agent_behavioral_baseline.compute_anomaly_entropy", MagicMock(return_value=0.0)),
        patch(
            "src.tool_usage_tracker.get_tool_usage_tracker",
            return_value=MagicMock(get_usage_stats=MagicMock(return_value={"total_calls": 0})),
        ),
        patch("src.db.get_db", return_value=mock_db),
        patch("src.services.checkpoint_service.accept_before_assessment", AsyncMock()),
        patch.object(ald, "agent_metadata", ctx.mcp_server.agent_metadata),
        patch.object(ald, "verify_agent_ownership", MagicMock(return_value=(True, None))),
        patch.object(ald, "detect_loop_pattern", MagicMock(return_value=(False, ""))),
        patch.object(ald, "save_monitor_state_async", AsyncMock()),
        patch.object(agent_lifecycle, "get_or_create_monitor", MagicMock(return_value=cached_monitor)),
        patch.object(agent_monitor_state, "hydrate_from_db_if_fresh", AsyncMock(side_effect=_hydrate)),
    ):
        await prepare_unlocked_inputs(ctx)
        assert callable(ctx.agent_state.get("_prepare_restored_monitor"))
        early_exit = await execute_locked_update(ctx)

    assert early_exit is None
    assert ctx.previous_void_active is True
    assert ctx.agent_state["_eisv_derivation"]["inputs"]["features"]["drift_norm"] is not None
    assert ctx.agent_state["_eisv_derivation"]["inputs"]["features"]["complexity_divergence"] is not None
    assert "_prepare_restored_monitor" not in ctx.agent_state
    assert "_behavioral_sensor_context" not in ctx.agent_state


@pytest.mark.asyncio
async def test_checkpoint_rejection_skips_monitor_creation_and_cleans_up_state():
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
        assert "_behavioral_sensor_context" in ctx.agent_state
        with pytest.raises(CheckpointBoundaryStop):
            await execute_locked_update(ctx)

    get_monitor.assert_not_called()
    assert "_prepare_restored_monitor" not in ctx.agent_state
    assert "_behavioral_sensor_context" not in ctx.agent_state
