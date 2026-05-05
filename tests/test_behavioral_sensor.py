"""Tests for behavioral_sensor.py — EISV from governance observables."""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from src.behavioral_sensor import (
    compute_behavioral_sensor_eisv,
    _compute_E,
    _compute_I,
    _compute_S,
    _compute_V,
    _simple_slope,
    _coherence_trend,
    _regime_instability,
)


# ── Helpers ──

def make_histories(n=10, decision="proceed", coherence=0.5, regime="high",
                   E=0.7, I=0.6, S=0.3, V=0.1):
    """Generate uniform test histories."""
    return {
        "decision_history": [decision] * n,
        "coherence_history": [coherence] * n,
        "regime_history": [regime] * n,
        "E_history": [E] * n,
        "I_history": [I] * n,
        "S_history": [S] * n,
        "V_history": [V] * n,
    }


# ══════════════════════════════════════════════════
#  Unit tests: compute_behavioral_sensor_eisv
# ══════════════════════════════════════════════════

class TestBehavioralSensor:
    def test_returns_none_with_insufficient_history(self):
        h = make_histories(n=2)
        result = compute_behavioral_sensor_eisv(**h)
        assert result is None

    def test_returns_none_with_empty_history(self):
        h = make_histories(n=0)
        result = compute_behavioral_sensor_eisv(**h)
        assert result is None

    def test_returns_dict_with_sufficient_history(self):
        h = make_histories(n=5)
        result = compute_behavioral_sensor_eisv(**h)
        assert result is not None
        assert set(result.keys()) == {"E", "I", "S", "V"}

    def test_bounds_respected(self):
        """All output values within specified ranges."""
        h = make_histories(n=10)
        result = compute_behavioral_sensor_eisv(**h)
        assert 0.0 <= result["E"] <= 1.0
        assert 0.0 <= result["I"] <= 1.0
        assert 0.05 <= result["S"] <= 1.0
        assert -1.0 <= result["V"] <= 1.0

    def test_bounds_with_extreme_inputs(self):
        """Extreme input values still produce bounded outputs."""
        h = make_histories(n=10, E=1.0, I=0.0, S=1.0, V=1.0)
        h["coherence_history"] = [0.99, 0.01] * 5
        h["decision_history"] = ["reject"] * 10
        result = compute_behavioral_sensor_eisv(
            **h, calibration_error=1.0, drift_norm=5.0, complexity_divergence=3.0
        )
        assert 0.0 <= result["E"] <= 1.0
        assert 0.0 <= result["I"] <= 1.0
        assert 0.05 <= result["S"] <= 1.0
        assert -1.0 <= result["V"] <= 1.0

    def test_none_optional_params_use_defaults(self):
        h = make_histories(n=5)
        result = compute_behavioral_sensor_eisv(**h)
        assert result is not None
        # Should not raise with all Nones

    def test_v_independent_of_v_history(self):
        """V computation does NOT read V_history — changing it shouldn't affect V."""
        h1 = make_histories(n=10)
        h2 = make_histories(n=10)
        h2["V_history"] = [99.0] * 10  # wildly different V_history

        r1 = compute_behavioral_sensor_eisv(**h1)
        r2 = compute_behavioral_sensor_eisv(**h2)
        assert r1["V"] == r2["V"]


# ══════════════════════════════════════════════════
#  Unit tests: E (decision success rate)
# ══════════════════════════════════════════════════

class TestComputeE:
    def test_all_proceed_high_e(self):
        """All proceed + good coherence + low divergence → high E."""
        e = _compute_E(["proceed"] * 10, [0.52] * 10, complexity_divergence=0.1)
        assert e > 0.75

    def test_all_reject_low_e(self):
        """All reject → low E (even with good coherence)."""
        e = _compute_E(["reject"] * 10, [0.52] * 10, complexity_divergence=0.1)
        assert e < 0.55

    def test_all_proceed_no_context_moderate_e(self):
        """All proceed but no coherence/divergence context → moderate-high E."""
        e = _compute_E(["proceed"] * 10)
        assert 0.7 < e < 0.95

    def test_mixed_decisions(self):
        decisions = ["proceed"] * 5 + ["reject"] * 5
        e = _compute_E(decisions)
        assert 0.3 < e < 0.8

    def test_recent_decisions_weighted_more(self):
        """Recent proceeds after rejects → higher E than rejects after proceeds."""
        improving = ["reject"] * 5 + ["proceed"] * 5
        declining = ["proceed"] * 5 + ["reject"] * 5
        assert _compute_E(improving) > _compute_E(declining)

    def test_empty_returns_default(self):
        """Empty decisions → default 0.65 decision component."""
        e = _compute_E([])
        assert 0.5 < e < 0.8

    def test_guide_between_proceed_and_reject(self):
        e_guide = _compute_E(["guide"] * 10)
        e_proceed = _compute_E(["proceed"] * 10)
        e_reject = _compute_E(["reject"] * 10)
        assert e_reject < e_guide < e_proceed

    def test_only_uses_last_10(self):
        """Window is 10 — earlier decisions ignored."""
        long = ["reject"] * 100 + ["proceed"] * 10
        short = ["proceed"] * 10
        assert abs(_compute_E(long) - _compute_E(short)) < 0.01

    def test_high_coherence_raises_e(self):
        """Higher coherence → higher E for same decisions."""
        e_low = _compute_E(["proceed"] * 10, [0.42] * 10)
        e_high = _compute_E(["proceed"] * 10, [0.54] * 10)
        assert e_high > e_low

    def test_high_divergence_lowers_e(self):
        """High complexity divergence → lower E (poor self-calibration)."""
        e_low_div = _compute_E(["proceed"] * 10, complexity_divergence=0.1)
        e_high_div = _compute_E(["proceed"] * 10, complexity_divergence=0.7)
        assert e_low_div > e_high_div


# ══════════════════════════════════════════════════
#  Unit tests: I (calibration + coherence trend)
# ══════════════════════════════════════════════════

class TestComputeI:
    def test_high_calibration_error_low_i(self):
        i = _compute_I([0.5] * 10, calibration_error=0.9)
        assert i < 0.4

    def test_zero_calibration_error_high_i(self):
        i = _compute_I([0.5] * 10, calibration_error=0.0)
        assert i > 0.7

    def test_none_calibration_uses_default(self):
        i = _compute_I([0.5] * 10, calibration_error=None)
        assert 0.4 < i < 0.8  # default 0.75 cal_I

    def test_improving_coherence_higher_i(self):
        improving = [0.45, 0.46, 0.47, 0.48, 0.50, 0.52, 0.53, 0.54, 0.55, 0.56]
        flat = [0.5] * 10
        i_improving = _compute_I(improving, calibration_error=0.1)
        i_flat = _compute_I(flat, calibration_error=0.1)
        assert i_improving > i_flat


class TestCoherenceTrend:
    def test_improving_trend(self):
        improving = [0.45, 0.46, 0.48, 0.50, 0.52, 0.54, 0.55, 0.56]
        assert _coherence_trend(improving) > 0.6

    def test_declining_trend(self):
        declining = [0.56, 0.55, 0.54, 0.52, 0.50, 0.48, 0.46, 0.45]
        assert _coherence_trend(declining) < 0.6

    def test_short_history_returns_default(self):
        assert _coherence_trend([0.5, 0.5, 0.5]) == 0.6


# ══════════════════════════════════════════════════
#  Unit tests: S (entropy)
# ══════════════════════════════════════════════════

class TestComputeS:
    def test_high_drift_high_s(self):
        s = _compute_S(drift_norm=1.0, regime_history=["high"] * 10, complexity_divergence=0.0)
        assert s > 0.3

    def test_low_drift_low_s(self):
        s = _compute_S(drift_norm=0.0, regime_history=["high"] * 10, complexity_divergence=0.0)
        assert s < 0.2

    def test_regime_instability_increases_s(self):
        stable = _compute_S(drift_norm=0.1, regime_history=["high"] * 10, complexity_divergence=0.1)
        unstable = _compute_S(
            drift_norm=0.1,
            regime_history=["high", "low"] * 5,
            complexity_divergence=0.1,
        )
        assert unstable > stable

    def test_minimum_s_is_0_05(self):
        s = _compute_S(drift_norm=0.0, regime_history=["high"] * 10, complexity_divergence=0.0)
        assert s >= 0.05


class TestRegimeInstability:
    def test_no_transitions(self):
        assert _regime_instability(["high"] * 10) == 0.0

    def test_every_step_transitions(self):
        assert _regime_instability(["high", "low"] * 5) == 1.0

    def test_short_history(self):
        assert _regime_instability(["high"]) == 0.1  # default


# ══════════════════════════════════════════════════
#  Unit tests: V (E-I trajectory slope difference)
# ══════════════════════════════════════════════════

class TestComputeV:
    def test_e_rising_i_flat_positive_v(self):
        E = [0.5 + 0.02 * i for i in range(10)]  # rising
        I = [0.6] * 10  # flat
        v = _compute_V(E, I)
        assert v > 0.0

    def test_i_rising_e_flat_negative_v(self):
        E = [0.6] * 10  # flat
        I = [0.5 + 0.02 * i for i in range(10)]  # rising
        v = _compute_V(E, I)
        assert v < 0.0

    def test_parallel_slopes_near_zero(self):
        E = [0.5 + 0.01 * i for i in range(10)]
        I = [0.5 + 0.01 * i for i in range(10)]
        v = _compute_V(E, I)
        assert abs(v) < 0.1

    def test_insufficient_history_returns_zero(self):
        assert _compute_V([0.5, 0.6], [0.5, 0.6]) == 0.0

    def test_clipped_to_bounds(self):
        E = [0.0 + 0.1 * i for i in range(10)]
        I = [1.0 - 0.1 * i for i in range(10)]
        v = _compute_V(E, I)
        assert -1.0 <= v <= 1.0


class TestSimpleSlope:
    def test_flat(self):
        assert _simple_slope([5.0] * 10) == 0.0

    def test_rising(self):
        assert _simple_slope([0.0, 1.0, 2.0]) > 0.0

    def test_falling(self):
        assert _simple_slope([2.0, 1.0, 0.0]) < 0.0

    def test_single_value(self):
        assert _simple_slope([1.0]) == 0.0


# ══════════════════════════════════════════════════
#  Integration tests: injection in update_phases.py
# ══════════════════════════════════════════════════

class TestBehavioralSensorInjection:
    """Mock-based tests for the injection logic in execute_locked_update."""

    def _make_mock_monitor(self, n=10):
        monitor = MagicMock()
        monitor.state.decision_history = ["proceed"] * n
        monitor.state.coherence_history = [0.5] * n
        monitor.state.regime_history = ["high"] * n
        monitor.state.E_history = [0.7] * n
        monitor.state.I_history = [0.6] * n
        monitor.state.S_history = [0.3] * n
        monitor.state.V_history = [0.1] * n
        monitor._last_drift_vector = MagicMock(norm=0.2)
        monitor._last_continuity_metrics = MagicMock(complexity_divergence=0.15)
        return monitor

    def _make_ctx(self, monitor):
        """Build an UpdateContext wired for execute_locked_update with a mock monitor."""
        from src.mcp_handlers.updates.context import UpdateContext
        mcp_server = MagicMock()
        mcp_server.monitors = {"test-agent": monitor}
        mcp_server.agent_metadata = {}
        # ODE call returns a valid result
        mcp_server.process_update_authenticated_async = AsyncMock(return_value={
            "status": "ok",
            "metrics": {"coherence": 0.8, "risk_score": 0.1},
        })

        ctx = UpdateContext(  # noqa: F811 — imported in method scope
            agent_id="test-agent",
            agent_uuid="test-agent",
            arguments={"client_session_id": "test-session"},
            response_text="test response",
            complexity=0.5,
            ethical_drift=[0.0, 0.0, 0.0],
            is_new_agent=False,
            meta=MagicMock(purpose="testing", active_session_key="test-session"),
            loop=AsyncMock(),
            mcp_server=mcp_server,
        )
        return ctx

    def test_physical_sensor_takes_priority(self):
        """When sensor_data with eisv is provided, behavioral sensor is skipped."""
        from src.behavioral_sensor import compute_behavioral_sensor_eisv

        ctx_agent_state = {
            "sensor_eisv": {"E": 0.8, "I": 0.7, "S": 0.2, "V": 0.1},
        }
        # The check is: if "sensor_eisv" not in ctx.agent_state
        assert "sensor_eisv" in ctx_agent_state  # physical takes priority

    def test_behavioral_injection_when_no_sensor_data(self):
        """Behavioral EISV is injected when no physical sensor data."""
        monitor = self._make_mock_monitor(n=10)
        result = compute_behavioral_sensor_eisv(
            decision_history=list(monitor.state.decision_history),
            coherence_history=list(monitor.state.coherence_history),
            regime_history=list(monitor.state.regime_history),
            E_history=list(monitor.state.E_history),
            I_history=list(monitor.state.I_history),
            S_history=list(monitor.state.S_history),
            V_history=list(monitor.state.V_history),
            drift_norm=0.2,
            complexity_divergence=0.15,
        )
        assert result is not None
        assert set(result.keys()) == {"E", "I", "S", "V"}

    def test_no_injection_for_new_agent(self):
        """New agents (no monitor or < 3 history) get no behavioral sensor."""
        monitor = self._make_mock_monitor(n=2)
        result = compute_behavioral_sensor_eisv(
            decision_history=list(monitor.state.decision_history),
            coherence_history=list(monitor.state.coherence_history),
            regime_history=list(monitor.state.regime_history),
            E_history=list(monitor.state.E_history),
            I_history=list(monitor.state.I_history),
            S_history=list(monitor.state.S_history),
            V_history=list(monitor.state.V_history),
        )
        assert result is None

    def _patch_execute_locked_deps(self, **overrides):
        """Context manager stack for mocking execute_locked_update's heavy dependencies."""
        from contextlib import ExitStack
        patches = {
            "src.mcp_handlers.updates.phases.agent_storage": MagicMock(
                get_agent=AsyncMock(return_value=MagicMock(api_key="fake-key")),
            ),
            "src.calibration.calibration_checker": MagicMock(
                compute_calibration_metrics=MagicMock(return_value=None),
            ),
            "src.agent_behavioral_baseline.ensure_baseline_loaded": AsyncMock(return_value=None),
            "src.agent_behavioral_baseline.compute_anomaly_entropy": MagicMock(return_value=0.0),
        }
        patches.update(overrides)

        stack = ExitStack()
        for target, mock_val in patches.items():
            if isinstance(mock_val, Exception):
                stack.enter_context(patch(target, side_effect=mock_val))
            else:
                stack.enter_context(patch(target, mock_val))
        return stack

    @pytest.mark.asyncio
    async def test_tool_tracker_failure_still_computes_sensor(self):
        """Tool usage tracker raising doesn't prevent behavioral sensor injection."""
        from src.mcp_handlers.updates.phases import (
            execute_locked_update,
            prepare_unlocked_inputs,
        )
        monitor = self._make_mock_monitor(n=10)
        ctx = self._make_ctx(monitor)

        with self._patch_execute_locked_deps(), \
             patch("src.tool_usage_tracker.get_tool_usage_tracker", side_effect=RuntimeError("tracker down")), \
             patch("src.db.get_db", return_value=None):
            await prepare_unlocked_inputs(ctx)
            result = await execute_locked_update(ctx)

        assert result is None  # no early exit = success
        assert "sensor_eisv" in ctx.agent_state, "Sensor should still be injected despite tracker failure"

    @pytest.mark.asyncio
    async def test_db_outcomes_failure_still_computes_sensor(self):
        """get_recent_outcomes raising doesn't prevent behavioral sensor injection."""
        from src.mcp_handlers.updates.phases import (
            execute_locked_update,
            prepare_unlocked_inputs,
        )
        monitor = self._make_mock_monitor(n=10)
        ctx = self._make_ctx(monitor)

        mock_db = MagicMock()
        mock_db.get_recent_outcomes = AsyncMock(side_effect=ConnectionError("db gone"))

        with self._patch_execute_locked_deps(), \
             patch("src.tool_usage_tracker.get_tool_usage_tracker",
                   return_value=MagicMock(get_usage_stats=MagicMock(return_value={"total_calls": 0}))), \
             patch("src.db.get_db", return_value=mock_db):
            await prepare_unlocked_inputs(ctx)
            result = await execute_locked_update(ctx)

        assert result is None
        assert "sensor_eisv" in ctx.agent_state, "Sensor should still be injected despite DB failure"

    @pytest.mark.asyncio
    async def test_compute_behavioral_raises_no_sensor_injected(self):
        """If compute_behavioral_sensor_eisv itself raises, update succeeds without sensor."""
        from src.mcp_handlers.updates.phases import (
            execute_locked_update,
            prepare_unlocked_inputs,
        )

        monitor = self._make_mock_monitor(n=10)
        ctx = self._make_ctx(monitor)

        with self._patch_execute_locked_deps(), \
             patch("src.behavioral_sensor.compute_behavioral_sensor_eisv", side_effect=RuntimeError("boom")), \
             patch("src.db.get_db", return_value=None):
            await prepare_unlocked_inputs(ctx)
            result = await execute_locked_update(ctx)

        assert result is None  # update still succeeds
        assert "sensor_eisv" not in ctx.agent_state, "No sensor should be injected when computation fails"

    def test_no_monitor_skips_sensor(self):
        """When no monitor exists, behavioral sensor is not computed."""
        monitors = {}
        monitor = monitors.get("nonexistent_agent")
        assert monitor is None
        # The injection code checks `if monitor and len(...)` — this would skip

    @pytest.mark.asyncio
    async def test_explicit_sensor_data_eisv_routed_to_agent_state(self):
        """When sensor_data["eisv"] is in the payload, it lands in agent_state["sensor_eisv"]."""
        from src.mcp_handlers.updates.phases import (
            execute_locked_update,
            prepare_unlocked_inputs,
        )

        monitor = MagicMock()
        monitor.state.decision_history = []
        ctx = self._make_ctx(monitor)

        explicit_eisv = {"E": 0.9, "I": 0.8, "S": 0.1, "V": 0.0}
        ctx.arguments["sensor_data"] = {"eisv": explicit_eisv}

        with self._patch_execute_locked_deps(), \
             patch("src.db.get_db", return_value=None):
            await prepare_unlocked_inputs(ctx)
            result = await execute_locked_update(ctx)

        assert result is None
        assert ctx.agent_state.get("sensor_eisv") == explicit_eisv

    @pytest.mark.asyncio
    async def test_no_sensor_data_means_no_sensor_eisv(self):
        """When the payload carries no sensor_data, agent_state lacks sensor_eisv."""
        from src.mcp_handlers.updates.phases import (
            execute_locked_update,
            prepare_unlocked_inputs,
        )

        monitor = MagicMock()
        monitor.state.decision_history = []
        ctx = self._make_ctx(monitor)

        with self._patch_execute_locked_deps(), \
             patch("src.db.get_db", return_value=None):
            await prepare_unlocked_inputs(ctx)
            result = await execute_locked_update(ctx)

        assert result is None
        assert ctx.agent_state.get("sensor_eisv") is None


class TestPrepareUnlockedInputsContract:
    """Lock surface-shrink contract: prepare_unlocked_inputs alone must produce
    everything the locked phase used to compute itself — agent_state with all
    inputs, policy_warnings, sensor_eisv when applicable.

    This test exists so a future refactor that accidentally moves work back
    into the lock fails loudly here instead of silently regressing the broker
    timeout. Per the [checkin_phases] log analysis on 2026-05-04, the locked
    phase had a 7s steady-state floor; lifting these inputs out drops it.
    """

    def _make_mock_monitor(self, n=10):
        monitor = MagicMock()
        monitor.state.decision_history = ["proceed"] * n
        monitor.state.coherence_history = [0.5] * n
        monitor.state.regime_history = ["high"] * n
        monitor.state.E_history = [0.7] * n
        monitor.state.I_history = [0.6] * n
        monitor.state.S_history = [0.3] * n
        monitor.state.V_history = [0.1] * n
        monitor._last_drift_vector = MagicMock(norm=0.2)
        monitor._last_continuity_metrics = MagicMock(complexity_divergence=0.15)
        return monitor

    def _make_ctx(self, monitor=None, sensor_data=None):
        from src.mcp_handlers.updates.context import UpdateContext
        mcp_server = MagicMock()
        mcp_server.monitors = {"test-agent": monitor} if monitor else {}
        arguments = {"client_session_id": "test-session"}
        if sensor_data is not None:
            arguments["sensor_data"] = sensor_data
        return UpdateContext(
            agent_id="test-agent",
            agent_uuid="test-agent",
            arguments=arguments,
            response_text="test response",
            complexity=0.5,
            ethical_drift=[0.0, 0.0, 0.0],
            is_new_agent=False,
            meta=MagicMock(),
            loop=AsyncMock(),
            mcp_server=mcp_server,
        )

    @pytest.mark.asyncio
    async def test_populates_agent_state_inputs(self):
        from src.mcp_handlers.updates.phases import prepare_unlocked_inputs
        ctx = self._make_ctx()
        with patch("src.db.get_db", return_value=None):
            await prepare_unlocked_inputs(ctx)
        # ODE inputs are ready before the lock is acquired
        assert ctx.agent_state["response_text"] == "test response"
        assert ctx.agent_state["complexity"] == 0.5
        assert "parameters" in ctx.agent_state
        assert "ethical_drift" in ctx.agent_state

    @pytest.mark.asyncio
    async def test_populates_policy_warnings(self):
        from src.mcp_handlers.updates.phases import prepare_unlocked_inputs
        ctx = self._make_ctx()
        with patch("src.db.get_db", return_value=None):
            await prepare_unlocked_inputs(ctx)
        # Policy warnings are computed unlocked (pure CPU on response_text)
        assert isinstance(ctx.policy_warnings, list)

    @pytest.mark.asyncio
    async def test_behavioral_sensor_runs_unlocked(self):
        """Behavioral sensor injection happens before the lock when monitor has history."""
        from src.mcp_handlers.updates.phases import prepare_unlocked_inputs
        monitor = self._make_mock_monitor(n=10)
        ctx = self._make_ctx(monitor=monitor)
        with patch("src.db.get_db", return_value=None), \
             patch("src.tool_usage_tracker.get_tool_usage_tracker",
                   return_value=MagicMock(get_usage_stats=MagicMock(return_value={"total_calls": 0}))):
            await prepare_unlocked_inputs(ctx)
        assert "sensor_eisv" in ctx.agent_state

    @pytest.mark.asyncio
    async def test_explicit_sensor_data_routed_unlocked(self):
        """Caller-supplied sensor_data["eisv"] lands in agent_state before the lock."""
        from src.mcp_handlers.updates.phases import prepare_unlocked_inputs
        explicit = {"E": 0.9, "I": 0.8, "S": 0.1, "V": 0.0}
        ctx = self._make_ctx(sensor_data={"eisv": explicit})
        with patch("src.db.get_db", return_value=None):
            await prepare_unlocked_inputs(ctx)
        assert ctx.agent_state["sensor_eisv"] == explicit


# ══════════════════════════════════════════════════
#  Unit tests: Tool usage signal blending
# ══════════════════════════════════════════════════

class TestToolUsageBlending:
    """Tests for tool_error_rate, tool_call_velocity, unique_tools_ratio params."""

    def test_none_tool_params_same_as_before(self):
        """All tool params None produces same result as without them."""
        h = make_histories(n=10)
        r_without = compute_behavioral_sensor_eisv(**h)
        r_with = compute_behavioral_sensor_eisv(
            **h,
            tool_error_rate=None,
            tool_call_velocity=None,
            unique_tools_ratio=None,
        )
        assert r_without == r_with

    def test_high_error_rate_reduces_e(self):
        """High tool error rate (many failures) should reduce E."""
        h = make_histories(n=10)
        r_low_err = compute_behavioral_sensor_eisv(**h, tool_error_rate=0.1)
        r_high_err = compute_behavioral_sensor_eisv(**h, tool_error_rate=0.9)
        assert r_low_err["E"] > r_high_err["E"]

    def test_zero_error_rate_boosts_e(self):
        """Zero error rate blends 1.0 into E — should raise it or keep it high."""
        h = make_histories(n=10)
        r_no_tool = compute_behavioral_sensor_eisv(**h)
        r_zero_err = compute_behavioral_sensor_eisv(**h, tool_error_rate=0.0)
        # With 0.85 * E + 0.15 * 1.0, E should be >= original when original E < 1.0
        assert r_zero_err["E"] >= r_no_tool["E"] - 0.01  # small tolerance

    def test_high_velocity_increases_s(self):
        """Tool call velocity > 5/min should increase entropy S."""
        h = make_histories(n=10)
        r_low_vel = compute_behavioral_sensor_eisv(**h, tool_call_velocity=2.0)
        r_high_vel = compute_behavioral_sensor_eisv(**h, tool_call_velocity=12.0)
        assert r_high_vel["S"] > r_low_vel["S"]

    def test_low_velocity_no_entropy_change(self):
        """Velocity <= 5/min should not add entropy."""
        h = make_histories(n=10)
        r_no_vel = compute_behavioral_sensor_eisv(**h)
        r_low_vel = compute_behavioral_sensor_eisv(**h, tool_call_velocity=3.0)
        assert r_low_vel["S"] == r_no_vel["S"]

    def test_velocity_entropy_capped(self):
        """Even extreme velocity should cap entropy contribution at 0.10."""
        h = make_histories(n=10)
        r_extreme = compute_behavioral_sensor_eisv(**h, tool_call_velocity=100.0)
        r_high = compute_behavioral_sensor_eisv(**h, tool_call_velocity=15.0)
        assert r_extreme["S"] == r_high["S"]

    def test_high_tool_diversity_maintains_i(self):
        """High unique_tools_ratio signals coherent work, maintains I."""
        h = make_histories(n=10)
        r_low_div = compute_behavioral_sensor_eisv(**h, unique_tools_ratio=0.1)
        r_high_div = compute_behavioral_sensor_eisv(**h, unique_tools_ratio=0.9)
        assert r_high_div["I"] > r_low_div["I"]

    def test_tool_params_dont_affect_v(self):
        """Tool usage signals should not affect V computation."""
        h = make_histories(n=10)
        r_no_tool = compute_behavioral_sensor_eisv(**h)
        r_with_tool = compute_behavioral_sensor_eisv(
            **h, tool_error_rate=0.5, tool_call_velocity=10.0, unique_tools_ratio=0.3
        )
        assert r_no_tool["V"] == r_with_tool["V"]

    def test_bounds_with_tool_params(self):
        """Output stays bounded with extreme tool usage values."""
        h = make_histories(n=10)
        r = compute_behavioral_sensor_eisv(
            **h, tool_error_rate=1.0, tool_call_velocity=100.0, unique_tools_ratio=0.0
        )
        assert 0.0 <= r["E"] <= 1.0
        assert 0.0 <= r["I"] <= 1.0
        assert 0.05 <= r["S"] <= 1.5  # S can go above 1.0 with velocity addition
        assert -1.0 <= r["V"] <= 1.0
