"""Tests for behavioral sensor signal-hygiene and clean actor work-tool switches."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src import eisv_instrument
from src.audit_db import (
    BEHAVIORAL_EXCLUDED_META_TOOL_SET,
    BEHAVIORAL_NON_AGENT_ERROR_TYPES,
    _behavioral_excluded_tool_names,
    get_behavioral_work_tool_stats_async,
)
from src.behavioral_sensor import (
    actor_work_tool_stats_enabled,
    compute_behavioral_sensor_components,
    compute_behavioral_sensor_eisv,
    drop_continuity_e_blend_enabled,
    drop_unique_tools_i_blend_enabled,
    extract_tool_usage_counts,
    populate_behavioral_sensor_state,
)
from src.tool_usage_tracker import ToolUsageTracker


def _histories(n: int = 10) -> dict:
    return {
        "decision_history": ["proceed"] * n,
        "coherence_history": [0.5] * n,
        "regime_history": ["high"] * n,
        "E_history": [0.7] * n,
        "I_history": [0.6] * n,
        "S_history": [0.3] * n,
        "V_history": [0.1] * n,
    }


def _mock_monitor(n: int = 10, *, e_clipped: bool = True) -> MagicMock:
    monitor = MagicMock()
    monitor._needs_hydration = False
    monitor.state.decision_history = ["proceed"] * n
    monitor.state.coherence_history = [0.5] * n
    monitor.state.regime_history = ["high"] * n
    monitor.state.E_history = [0.7] * n
    monitor.state.I_history = [0.6] * n
    monitor.state.S_history = [0.3] * n
    monitor.state.V_history = [0.1] * n
    monitor._last_drift_vector = MagicMock(norm=0.2)
    monitor._last_continuity_metrics = MagicMock(
        E_input=0.30 if e_clipped else 0.80,
        I_input=0.65,
        S_input=0.25,
        complexity_divergence=0.15,
        self_complexity_defaulted=False,
        E_input_clipped=e_clipped,
        continuity_degenerate=e_clipped,
    )
    return monitor


def _mock_ctx() -> object:
    from src.mcp_handlers.updates.context import UpdateContext

    return UpdateContext(
        agent_id="actor-agent-1",
        agent_uuid="actor-agent-1",
        arguments={"client_session_id": "sess-1"},
        response_text="real work turn",
        complexity=0.45,
        ethical_drift=[0.0, 0.0, 0.0],
        _calibration_signal={"agent_channel": {"evidence_status": "insufficient"}},
    )


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class TestDefaultFlagsPreservation:
    def test_new_flags_default_off(self, monkeypatch):
        for var in (
            "UNITARES_DROP_CONTINUITY_E_BLEND",
            "UNITARES_DROP_UNIQUE_TOOLS_I_BLEND",
            "UNITARES_ACTOR_WORK_TOOL_STATS",
        ):
            monkeypatch.delenv(var, raising=False)
        assert drop_continuity_e_blend_enabled() is False
        assert drop_unique_tools_i_blend_enabled() is False
        assert actor_work_tool_stats_enabled() is False

    def test_defaults_preserve_existing_blends(self, monkeypatch):
        for var in (
            "UNITARES_DROP_CONTINUITY_E_BLEND",
            "UNITARES_DROP_UNIQUE_TOOLS_I_BLEND",
            "UNITARES_ACTOR_WORK_TOOL_STATS",
        ):
            monkeypatch.delenv(var, raising=False)

        comp = compute_behavioral_sensor_components(
            **_histories(),
            continuity_E_input=0.30,
            continuity_I_input=0.60,
            continuity_S_input=0.20,
            tool_error_rate=0.20,
            tool_call_velocity=7.0,
            unique_tools_ratio=0.05,
        )
        e_adj_names = [a["name"] for a in comp["dimensions"]["E"]["adjustments"]]
        i_adj_names = [a["name"] for a in comp["dimensions"]["I"]["adjustments"]]
        assert "continuity_E_input" in e_adj_names
        assert "tool_success_rate" in e_adj_names
        assert "unique_tools_ratio" in i_adj_names


class TestDropContinuityEBlend:
    @pytest.mark.parametrize("cont_e", [0.30, 0.80, 0.99])
    def test_unconditional_omission_eliminates_clip_cliff(self, monkeypatch, cont_e):
        """Omitting continuity_E_input must be unconditional, never creating a cliff at 0.30."""
        monkeypatch.setenv("UNITARES_DROP_CONTINUITY_E_BLEND", "1")
        with_cont = compute_behavioral_sensor_components(
            **_histories(),
            continuity_E_input=cont_e,
            continuity_I_input=0.60,
            continuity_S_input=0.25,
            tool_error_rate=0.10,
        )
        without_cont = compute_behavioral_sensor_components(
            **_histories(),
            continuity_E_input=None,
            continuity_I_input=0.60,
            continuity_S_input=0.25,
            tool_error_rate=0.10,
        )
        assert with_cont["dimensions"]["E"]["value"] == pytest.approx(
            without_cont["dimensions"]["E"]["value"]
        )
        e_names = [a["name"] for a in with_cont["dimensions"]["E"]["adjustments"]]
        assert "continuity_E_input" not in e_names
        assert "tool_success_rate" in e_names
        # Other continuity dimensions stay active
        assert "continuity_I_input" in [
            a["name"] for a in with_cont["dimensions"]["I"]["adjustments"]
        ]
        assert "continuity_S_input" in [
            a["name"] for a in with_cont["dimensions"]["S"]["adjustments"]
        ]

    def test_diagnostics_and_canaries_retained_when_blend_dropped(self, monkeypatch):
        monkeypatch.setenv("UNITARES_DROP_CONTINUITY_E_BLEND", "1")
        state = {}
        monitor = _mock_monitor(e_clipped=True)
        obs = populate_behavioral_sensor_state(
            state,
            monitor_state=monitor.state,
            drift_vector=monitor._last_drift_vector,
            continuity_metrics=monitor._last_continuity_metrics,
        )
        assert obs is not None
        deriv = state["_eisv_derivation"]
        assert deriv["inputs"]["features"]["continuity_E_input"] == pytest.approx(0.30)
        assert deriv["substrate_canaries"]["E_input_clipped"] is True
        e_names = [
            a["name"] for a in deriv["components"]["dimensions"]["E"]["adjustments"]
        ]
        assert "continuity_E_input" not in e_names
        assert len(state["_eisv_shadow_ablations"]) == 4


class TestDropUniqueToolsIBlend:
    @pytest.mark.parametrize("ratio", [0.01, 0.25, 1.0])
    def test_unconditional_omission_removes_volume_and_specialist_penalty(
        self, monkeypatch, ratio
    ):
        monkeypatch.setenv("UNITARES_DROP_UNIQUE_TOOLS_I_BLEND", "1")
        res = compute_behavioral_sensor_components(
            **_histories(),
            tool_error_rate=0.20,
            tool_call_velocity=10.0,
            unique_tools_ratio=ratio,
        )
        baseline = compute_behavioral_sensor_components(
            **_histories(),
            tool_error_rate=0.20,
            tool_call_velocity=10.0,
            unique_tools_ratio=None,
        )
        assert res["dimensions"]["I"]["value"] == pytest.approx(
            baseline["dimensions"]["I"]["value"]
        )
        assert "unique_tools_ratio" not in [
            a["name"] for a in res["dimensions"]["I"]["adjustments"]
        ]
        # Sibling tool signals remain intact
        assert "tool_success_rate" in [
            a["name"] for a in res["dimensions"]["E"]["adjustments"]
        ]
        assert "tool_velocity_pressure" in [
            a["name"] for a in res["dimensions"]["S"]["adjustments"]
        ]

    def test_derivation_retains_unique_tools_ratio_diagnostic(self, monkeypatch):
        monkeypatch.setenv("UNITARES_DROP_UNIQUE_TOOLS_I_BLEND", "1")
        state = {}
        monitor = _mock_monitor(e_clipped=False)
        populate_behavioral_sensor_state(
            state,
            monitor_state=monitor.state,
            drift_vector=monitor._last_drift_vector,
            continuity_metrics=monitor._last_continuity_metrics,
            unique_tools_ratio=0.125,
        )
        deriv = state["_eisv_derivation"]
        assert deriv["inputs"]["features"]["unique_tools_ratio"] == pytest.approx(0.125)
        i_names = [
            a["name"] for a in deriv["components"]["dimensions"]["I"]["adjustments"]
        ]
        assert "unique_tools_ratio" not in i_names


class TestBehavioralWorkToolStatsDb:
    def test_excluded_sets_cover_pollers_lifecycle_and_non_actor_errors(self):
        assert {
            "get_governance_metrics",
            "process_agent_update",
            "onboard",
            "identity",
            "list_tools",
            "describe_tool",
            "health_check",
            "bind_session",
            "mark_response_complete",
            "simulate_update",
            "get_workspace_health",
        }.issubset(BEHAVIORAL_EXCLUDED_META_TOOL_SET.tools)
        expanded = _behavioral_excluded_tool_names()
        assert {
            "check_working_state",
            "sync_state",
            "start_session",
        }.issubset(expanded)
        assert ToolUsageTracker.REMOVED_TOOLS.issubset(expanded)
        assert {"system_error", "governance_refusal", "state_error"}.issubset(
            BEHAVIORAL_NON_AGENT_ERROR_TYPES
        )

    @pytest.mark.asyncio
    async def test_get_behavioral_work_tool_stats_async_aggregates_cleanly(self):
        conn = MagicMock()
        conn.fetch = AsyncMock(
            return_value=[
                {"eff_tool": "knowledge", "total_calls": 10, "success_count": 8},
                {"eff_tool": "dialectic", "total_calls": 2, "success_count": 2},
            ]
        )
        db = MagicMock()
        db._pool = object()
        db.acquire.return_value = _AcquireCtx(conn)

        with patch("src.db.get_db", return_value=db):
            stats = await get_behavioral_work_tool_stats_async(
                agent_id="actor-agent-1", window_hours=1.0
            )

        assert stats["source"] == "db_actor_work_tools"
        assert stats["total_calls"] == 12
        assert stats["unique_tools"] == 2
        assert stats["error_count"] == 2
        assert stats["error_rate"] == pytest.approx(2 / 12)
        assert stats["calls_per_minute"] == pytest.approx(12 / 60.0)
        assert stats["unique_tools_ratio"] == pytest.approx(2 / 12)
        assert stats["tools"]["knowledge"]["error_count"] == 2

        sql, agent_arg, cutoff_arg, excluded_tools_arg, excluded_errs_arg = conn.fetch.call_args[0]
        assert "COALESCE(NULLIF(payload->>'canonical_tool', ''), tool_name)" in sql
        assert "tool_name NOT LIKE 'lease.%'" in sql
        assert agent_arg == "actor-agent-1"
        assert isinstance(cutoff_arg, datetime) and cutoff_arg.tzinfo == timezone.utc
        assert _behavioral_excluded_tool_names().issubset(set(excluded_tools_arg))
        assert ToolUsageTracker.REMOVED_TOOLS.issubset(set(excluded_tools_arg))
        assert BEHAVIORAL_NON_AGENT_ERROR_TYPES.issubset(set(excluded_errs_arg))

    @pytest.mark.asyncio
    async def test_get_behavioral_work_tool_stats_async_empty_or_invalid(self):
        assert await get_behavioral_work_tool_stats_async(agent_id="") is None
        assert await get_behavioral_work_tool_stats_async(agent_id="a", window_hours=0) is None
        assert await get_behavioral_work_tool_stats_async(agent_id="a", window_hours=float("nan")) is None
        assert await get_behavioral_work_tool_stats_async(agent_id="a", window_hours=float("inf")) is None
        assert await get_behavioral_work_tool_stats_async(agent_id="a", window_hours=True) is None

        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[])
        db = MagicMock()
        db._pool = object()
        db.acquire.return_value = _AcquireCtx(conn)

        with patch("src.db.get_db", return_value=db):
            empty = await get_behavioral_work_tool_stats_async(agent_id="quiet-agent")
        assert empty["total_calls"] == 0
        assert empty["tools"] == {}

        with patch("src.db.get_db", return_value=None):
            assert await get_behavioral_work_tool_stats_async(agent_id="a") is None


class TestPrepareBehavioralSensorInputsToolStats:
    @pytest.mark.asyncio
    async def test_actor_work_tool_stats_flag_uses_db_and_skips_jsonl(self, monkeypatch):
        from src.mcp_handlers.updates.phases import _prepare_behavioral_sensor_inputs

        monkeypatch.setenv("UNITARES_ACTOR_WORK_TOOL_STATS", "1")
        monkeypatch.setenv("UNITARES_DROP_UNIQUE_TOOLS_I_BLEND", "1")
        monkeypatch.setenv("UNITARES_DROP_CONTINUITY_E_BLEND", "1")

        ctx = _mock_ctx()
        monitor = _mock_monitor(e_clipped=True)
        mcp_server = MagicMock(monitors={"actor-agent-1": monitor})
        tracker_mock = MagicMock()

        db_stats = {
            "total_calls": 20,
            "unique_tools": 2,
            "error_count": 4,
            "tools": {
                "knowledge": {"total_calls": 16, "error_count": 4},
                "dialectic": {"total_calls": 4, "error_count": 0},
            },
            "source": "db_actor_work_tools",
        }
        with patch("src.db.get_db", return_value=None), \
             patch("src.tool_usage_tracker.get_tool_usage_tracker", tracker_mock), \
             patch(
                 "src.audit_db.get_behavioral_work_tool_stats_async",
                 AsyncMock(return_value=db_stats),
             ) as db_fn:
            await _prepare_behavioral_sensor_inputs(ctx, mcp_server)

        db_fn.assert_awaited_once_with(agent_id="actor-agent-1", window_hours=1.0)
        tracker_mock.assert_not_called()

        features = ctx.agent_state["_eisv_derivation"]["inputs"]["features"]
        assert features["tool_error_rate"] == pytest.approx(0.20)
        assert features["tool_call_velocity"] == pytest.approx(20 / 60.0)
        assert features["unique_tools_ratio"] == pytest.approx(0.10)
        assert ctx.agent_state["_actor_work_tool_stats"]["total_calls"] == 20

        dims = ctx.agent_state["_eisv_derivation"]["components"]["dimensions"]
        assert "tool_success_rate" in [a["name"] for a in dims["E"]["adjustments"]]
        assert "continuity_E_input" not in [a["name"] for a in dims["E"]["adjustments"]]
        assert "unique_tools_ratio" not in [a["name"] for a in dims["I"]["adjustments"]]

    @pytest.mark.asyncio
    async def test_embodied_early_return_still_caches_actor_work_tool_stats(self, monkeypatch):
        from src.mcp_handlers.updates.phases import _prepare_behavioral_sensor_inputs

        monkeypatch.setenv("UNITARES_ACTOR_WORK_TOOL_STATS", "1")
        ctx = _mock_ctx()
        ctx.agent_state["sensor_eisv"] = {"E": 0.9, "I": 0.8, "S": 0.1, "V": 0.1}
        mcp_server = MagicMock(monitors={})
        tracker_mock = MagicMock()
        db_stats = {
            "total_calls": 5,
            "unique_tools": 1,
            "error_count": 1,
            "tools": {"knowledge": {"total_calls": 5, "error_count": 1}},
            "source": "db_actor_work_tools",
        }
        with patch("src.tool_usage_tracker.get_tool_usage_tracker", tracker_mock), \
             patch(
                 "src.audit_db.get_behavioral_work_tool_stats_async",
                 AsyncMock(return_value=db_stats),
             ):
            await _prepare_behavioral_sensor_inputs(ctx, mcp_server)

        tracker_mock.assert_not_called()
        assert ctx.agent_state["_actor_work_tool_stats"] == db_stats

    def test_derive_confidence_respects_actor_work_tool_stats_without_jsonl(self, monkeypatch):
        from src.confidence import derive_confidence

        monkeypatch.setenv("UNITARES_ACTOR_WORK_TOOL_STATS", "1")
        state = MagicMock(coherence=0.5, I=0.6, S=0.2, V=0.05)
        tracker_mock = MagicMock()
        db_stats = {
            "total_calls": 10,
            "unique_tools": 2,
            "error_count": 2,
            "tools": {"knowledge": {"total_calls": 10, "error_count": 2}},
            "source": "db_actor_work_tools",
        }
        with patch("src.tool_usage_tracker.get_tool_usage_tracker", tracker_mock):
            conf_with_stats, meta_with_stats = derive_confidence(
                state,
                agent_id="actor-agent-1",
                tool_usage_stats=db_stats,
            )
            conf_no_stats, meta_no_stats = derive_confidence(
                state,
                agent_id="actor-agent-1",
            )

        tracker_mock.assert_not_called()
        assert meta_with_stats["tool_stats"]["total_calls"] == 10
        assert meta_with_stats["tool_stats"]["success_rate"] == pytest.approx(0.80)
        assert "tool_stats" not in meta_no_stats
        assert isinstance(conf_with_stats, float) and isinstance(conf_no_stats, float)

    @pytest.mark.asyncio
    async def test_no_fallback_to_jsonl_when_db_empty_or_errors(self, monkeypatch):
        from src.mcp_handlers.updates.phases import _prepare_behavioral_sensor_inputs

        monkeypatch.setenv("UNITARES_ACTOR_WORK_TOOL_STATS", "1")
        tracker_mock = MagicMock()

        for side_effect in ({"total_calls": 0, "unique_tools": 0, "tools": {}}, RuntimeError("db down")):
            ctx = _mock_ctx()
            monitor = _mock_monitor(e_clipped=False)
            mcp_server = MagicMock(monitors={"actor-agent-1": monitor})
            db_mock = (
                AsyncMock(side_effect=side_effect)
                if isinstance(side_effect, Exception)
                else AsyncMock(return_value=side_effect)
            )
            with patch("src.db.get_db", return_value=None), \
                 patch("src.tool_usage_tracker.get_tool_usage_tracker", tracker_mock), \
                 patch("src.audit_db.get_behavioral_work_tool_stats_async", db_mock):
                await _prepare_behavioral_sensor_inputs(ctx, mcp_server)

            tracker_mock.assert_not_called()
            features = ctx.agent_state["_eisv_derivation"]["inputs"]["features"]
            assert features["tool_error_rate"] is None
            assert features["tool_call_velocity"] is None
            assert features["unique_tools_ratio"] is None


class TestInstrumentConfigHashIsolation:
    def test_all_three_flags_produce_distinct_config_hashes(self, monkeypatch):
        for var in (
            "UNITARES_DROP_CONTINUITY_E_BLEND",
            "UNITARES_DROP_UNIQUE_TOOLS_I_BLEND",
            "UNITARES_ACTOR_WORK_TOOL_STATS",
        ):
            monkeypatch.delenv(var, raising=False)
        eisv_instrument.reset_caches()
        base = eisv_instrument.config_hash()

        hashes = {base}
        for var in (
            "UNITARES_DROP_CONTINUITY_E_BLEND",
            "UNITARES_DROP_UNIQUE_TOOLS_I_BLEND",
            "UNITARES_ACTOR_WORK_TOOL_STATS",
        ):
            monkeypatch.setenv(var, "1")
            eisv_instrument.reset_caches()
            hashes.add(eisv_instrument.config_hash())
            monkeypatch.delenv(var, raising=False)

        assert len(hashes) == 4


class TestToolUsageExtractionAndDownstreamConsumers:
    def test_extract_tool_usage_counts_handles_malformed_and_fallback_shapes(self):
        assert extract_tool_usage_counts(None) == (0, 0)
        assert extract_tool_usage_counts({"total_calls": True, "error_count": 1}) == (0, 0)
        assert extract_tool_usage_counts({"total_calls": "bad"}) == (0, 0)
        assert extract_tool_usage_counts({
            "total_calls": 10,
            "error_count": "not-an-int",
            "tools": {"knowledge": {"total_calls": 10, "success_count": 7}},
        }) == (10, 3)
        assert extract_tool_usage_counts({
            "total_calls": 8,
            "error_count": 2,
            "tools": {"knowledge": {"other": 1}},
        }) == (8, 2)
        assert extract_tool_usage_counts({
            "total_calls": 5,
            "tools": {"knowledge": {"error_count": True}, "dialectic": {"error_count": "2"}},
        }) == (5, 2)

    @pytest.mark.asyncio
    async def test_prepare_handles_none_unique_tools_cleanly(self, monkeypatch):
        from src.mcp_handlers.updates.phases import _prepare_behavioral_sensor_inputs

        monkeypatch.setenv("UNITARES_ACTOR_WORK_TOOL_STATS", "1")
        ctx = _mock_ctx()
        monitor = _mock_monitor(e_clipped=False)
        mcp_server = MagicMock(monitors={"actor-agent-1": monitor})
        db_stats = {
            "total_calls": 10,
            "unique_tools": None,
            "error_count": 2,
            "tools": {},
        }
        with patch("src.db.get_db", return_value=None), \
             patch("src.tool_usage_tracker.get_tool_usage_tracker", MagicMock()), \
             patch(
                 "src.audit_db.get_behavioral_work_tool_stats_async",
                 AsyncMock(return_value=db_stats),
             ):
            await _prepare_behavioral_sensor_inputs(ctx, mcp_server)

        features = ctx.agent_state["_eisv_derivation"]["inputs"]["features"]
        assert features["tool_error_rate"] == pytest.approx(0.20)
        assert features["tool_call_velocity"] == pytest.approx(10 / 60.0)
        assert features["unique_tools_ratio"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_post_update_baselines_consume_cached_actor_stats_without_jsonl(self, monkeypatch):
        from src.mcp_handlers.updates.post_update_helpers import _post_update_health_and_baselines

        monkeypatch.setenv("UNITARES_ACTOR_WORK_TOOL_STATS", "1")
        ctx = _mock_ctx()
        ctx.mcp_server = MagicMock(monitors={})
        ctx.result = {"metrics": {"risk_score": 0.1, "coherence": 0.5, "void_active": False}}
        ctx.agent_state["_actor_work_tool_stats"] = {
            "total_calls": 15,
            "unique_tools": 3,
            "error_count": 3,
            "tools": {"knowledge": {"total_calls": 15, "error_count": 3}},
        }
        tracker_mock = MagicMock()
        baseline_mock = MagicMock()

        with patch("src.tool_usage_tracker.get_tool_usage_tracker", tracker_mock), \
             patch("src.agent_behavioral_baseline.get_agent_behavioral_baseline", return_value=baseline_mock), \
             patch("src.agent_behavioral_baseline.schedule_baseline_save"):
            await _post_update_health_and_baselines(ctx)

        tracker_mock.assert_not_called()
        calls = [(c.args[0], c.args[1]) for c in baseline_mock.update.call_args_list]
        assert ("tool_error_rate", pytest.approx(0.20)) in calls
        assert ("tool_call_velocity", pytest.approx(0.25)) in calls
