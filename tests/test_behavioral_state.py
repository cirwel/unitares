"""Tests for BehavioralEISV: EMA convergence, bounds, history, bootstrap."""

import pytest
from src.behavioral_state import (
    BehavioralEISV,
    BOOTSTRAP_E,
    BOOTSTRAP_I,
    BOOTSTRAP_S,
    BOOTSTRAP_V,
    BOOTSTRAP_UPDATES,
    DEFAULT_ALPHAS,
    EMA_STEP_STD_FLOOR_FRACTION,
    MAX_HISTORY,
    eisv_min_std_for_dimension,
)


class TestBehavioralEISVBasics:
    """Basic construction and defaults."""

    def test_default_values(self):
        state = BehavioralEISV()
        assert state.E == BOOTSTRAP_E
        assert state.I == BOOTSTRAP_I
        assert state.S == BOOTSTRAP_S
        assert state.V == BOOTSTRAP_V
        assert state.update_count == 0
        assert state.confidence == 0.0

    def test_single_update(self):
        state = BehavioralEISV()
        state.update(0.8, 0.7, 0.1)
        assert state.update_count == 1
        assert state.E != BOOTSTRAP_E  # should have moved toward 0.8
        assert state.I != BOOTSTRAP_I  # should have moved toward 0.7
        assert len(state.E_history) == 1

    def test_min_std_is_derived_from_dimension_alpha(self):
        assert eisv_min_std_for_dimension("E") == pytest.approx(
            DEFAULT_ALPHAS["E"] * EMA_STEP_STD_FLOOR_FRACTION
        )
        assert eisv_min_std_for_dimension("I") == pytest.approx(
            DEFAULT_ALPHAS["I"] * EMA_STEP_STD_FLOOR_FRACTION
        )
        assert eisv_min_std_for_dimension("unknown") == 0.0

    def test_v_ema_converges_toward_gap(self):
        """V should converge toward E-I gap after many constant updates."""
        state = BehavioralEISV()
        for _ in range(200):
            state.update(0.9, 0.3, 0.2)
        assert state.V > 0  # E > I → positive V
        # After many updates, V should be close to (E - I) but not exactly equal
        assert abs(state.V - (state.E - state.I)) < 0.05

    def test_v_ema_lag_on_reversal(self):
        """V should lag behind sudden E-I reversals (EMA smoothing)."""
        state = BehavioralEISV()
        # Settle with E > I
        for _ in range(50):
            state.update(0.9, 0.3, 0.2)
        v_before = state.V
        assert v_before > 0
        # Reverse: I > E
        state.update(0.3, 0.9, 0.2)
        # V should still be positive (lag), not instantly flip
        assert state.V > 0
        # But slightly smaller than before
        assert state.V < v_before

    def test_alpha_v_affects_convergence(self):
        """Different alpha_V should produce different convergence rates."""
        fast = BehavioralEISV()
        fast.alphas["V"] = 0.50
        slow = BehavioralEISV()
        slow.alphas["V"] = 0.05
        for _ in range(20):
            fast.update(0.9, 0.3, 0.2)
            slow.update(0.9, 0.3, 0.2)
        # Fast should be closer to the E-I gap
        fast_gap = abs(fast.V - (fast.E - fast.I))
        slow_gap = abs(slow.V - (slow.E - slow.I))
        assert fast_gap < slow_gap


class TestEMAConvergence:
    """EMA should converge to the observation value over time."""

    def test_converges_to_constant_input(self):
        state = BehavioralEISV()
        target_E, target_I, target_S = 0.8, 0.6, 0.3
        for _ in range(200):
            state.update(target_E, target_I, target_S)
        # After many updates, should be very close to targets
        assert abs(state.E - target_E) < 0.01
        assert abs(state.I - target_I) < 0.01
        assert abs(state.S - target_S) < 0.01

    def test_responds_to_change(self):
        state = BehavioralEISV()
        # Settle at one value
        for _ in range(50):
            state.update(0.8, 0.8, 0.1)
        old_E = state.E
        # Switch to new value
        for _ in range(50):
            state.update(0.3, 0.3, 0.8)
        # Should have moved significantly toward new value
        assert state.E < old_E - 0.2
        assert state.S > 0.3


class TestBounds:
    """All values should stay within valid ranges."""

    def test_clamps_inputs(self):
        state = BehavioralEISV()
        state.update(1.5, -0.5, 2.0)
        assert 0.0 <= state.E <= 1.0
        assert 0.0 <= state.I <= 1.0
        assert 0.0 <= state.S <= 1.0
        assert -1.0 <= state.V <= 1.0

    def test_extreme_values_stay_bounded(self):
        state = BehavioralEISV()
        for _ in range(100):
            state.update(1.0, 0.0, 1.0)
        assert 0.0 <= state.E <= 1.0
        assert 0.0 <= state.I <= 1.0
        assert 0.0 <= state.S <= 1.0
        assert -1.0 <= state.V <= 1.0


class TestHistory:
    """History arrays should be capped and correct."""

    def test_history_grows_with_updates(self):
        state = BehavioralEISV()
        for _ in range(10):
            state.update(0.5, 0.5, 0.2)
        assert len(state.E_history) == 10
        assert len(state.I_history) == 10
        assert len(state.S_history) == 10
        assert len(state.V_history) == 10

    def test_history_capped_at_max(self):
        state = BehavioralEISV()
        for _ in range(MAX_HISTORY + 50):
            state.update(0.5, 0.5, 0.2)
        assert len(state.E_history) == MAX_HISTORY
        assert len(state.I_history) == MAX_HISTORY

    def test_history_keeps_recent(self):
        state = BehavioralEISV()
        for i in range(MAX_HISTORY + 10):
            state.update(i / (MAX_HISTORY + 10), 0.5, 0.2)
        # Last entry should be the most recent E value
        assert state.E_history[-1] == state.E


class TestBootstrap:
    """Bootstrap behavior: confidence ramp and faster alpha."""

    def test_confidence_ramp(self):
        state = BehavioralEISV()
        assert state.confidence == 0.0
        for _ in range(5):
            state.update(0.5, 0.5, 0.2)
        assert state.confidence == pytest.approx(0.5)
        for _ in range(5):
            state.update(0.5, 0.5, 0.2)
        assert state.confidence == pytest.approx(1.0)

    def test_bootstrap_moves_faster(self):
        """During bootstrap, alpha is higher → state moves faster toward obs."""
        bootstrap = BehavioralEISV()
        settled = BehavioralEISV()
        # Pre-settle the "settled" one
        for _ in range(BOOTSTRAP_UPDATES + 10):
            settled.update(0.5, 0.5, 0.2)
        # Now give both the same observation
        bootstrap.update(0.9, 0.9, 0.1)
        old_settled_E = settled.E
        settled.update(0.9, 0.9, 0.1)
        # Bootstrap should have moved MORE toward 0.9
        bootstrap_delta = abs(bootstrap.E - BOOTSTRAP_E)
        settled_delta = abs(settled.E - old_settled_E)
        assert bootstrap_delta > settled_delta

    def test_full_confidence_after_bootstrap(self):
        state = BehavioralEISV()
        for _ in range(BOOTSTRAP_UPDATES):
            state.update(0.5, 0.5, 0.2)
        assert state.confidence == 1.0


class TestTrend:
    """Trend detection from history."""

    def test_positive_trend(self):
        state = BehavioralEISV()
        for i in range(10):
            state.update(0.3 + i * 0.05, 0.5, 0.2)
        assert state.trend("E") > 0

    def test_negative_trend(self):
        state = BehavioralEISV()
        for i in range(10):
            state.update(0.8 - i * 0.05, 0.5, 0.2)
        assert state.trend("E") < 0

    def test_stable_trend(self):
        state = BehavioralEISV()
        for _ in range(10):
            state.update(0.5, 0.5, 0.2)
        assert abs(state.trend("E")) < 0.01

    def test_trend_with_no_history(self):
        state = BehavioralEISV()
        assert state.trend("E") == 0.0


class TestSerialization:
    """to_dict / from_dict round-trip."""

    def test_round_trip(self):
        state = BehavioralEISV()
        for _ in range(15):
            state.update(0.7, 0.6, 0.3)
        d = state.to_dict_with_history()
        restored = BehavioralEISV.from_dict(d)
        assert restored.E == pytest.approx(state.E, abs=1e-4)
        assert restored.I == pytest.approx(state.I, abs=1e-4)
        assert restored.S == pytest.approx(state.S, abs=1e-4)
        assert restored.V == pytest.approx(state.V, abs=1e-4)
        assert restored.update_count == state.update_count
        assert len(restored.E_history) == len(state.E_history)

    def test_from_empty_dict(self):
        state = BehavioralEISV.from_dict({})
        assert state.E == BOOTSTRAP_E
        assert state.update_count == 0

    def test_to_dict_basic(self):
        state = BehavioralEISV()
        state.update(0.8, 0.7, 0.3)
        d = state.to_dict()
        assert "E" in d
        assert "I" in d
        assert "S" in d
        assert "V" in d
        assert "confidence" in d
        assert "updates" in d
        # Basic dict should NOT have history
        assert "E_history" not in d


def test_to_dict_for_persistence_round_trips_baseline_without_histories():
    """Lean DB-persistence snapshot: restores baseline maturity (Welford stats +
    update_count) but omits the bulky history arrays. (Fleet starvation fix.)"""
    from src.behavioral_state import BehavioralEISV

    src = BehavioralEISV()
    for _ in range(30):
        src.update(0.31, 0.81, 0.24)
    assert src.is_baselined is True

    blob = src.to_dict_for_persistence()
    # History arrays omitted (the whole point — avoid per-row DB bloat)
    assert "E_history" not in blob
    assert "obs_history" not in blob
    # But baseline_stats + counts present
    assert "baseline_stats" in blob
    assert blob["updates"] == 30

    restored = BehavioralEISV.from_dict(blob)
    assert restored.update_count == 30
    assert restored.is_baselined is True
    # Welford baseline survived → z-scoring works post-restore
    assert restored._baseline_E.count == src._baseline_E.count
