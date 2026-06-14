"""Calibration: ultra-stable agents must not be falsely flagged high-risk for
small, absolutely-safe EISV wobbles.

Regression for the 2026-06-13 Sentinel pause: a long-running monitor with a
very tight behavioral baseline (std ~0.007-0.024) had a perfectly healthy
fluctuation (E 0.77->0.66, I 0.68->0.66) scored as a many-sigma "severe
deviation" -> risk 0.94 -> high-risk -> cirs_block -> paused ~18h. The fix is
to gate self-relative EISV risk by absolute EISV health: while raw E/I/S/V are
safe, movement from the agent's own tight baseline is evidence, not danger.
"""

import pytest

from src.agent_behavioral_baseline import WelfordStats
from src.behavioral_state import BehavioralEISV, eisv_min_std_for_dimension
from src.behavioral_assessment import assess_behavioral_state, RISK_CAUTION_THRESHOLD


# Real captured Sentinel baseline (1239 updates) at the moment of the false pause.
_SENTINEL_BASELINE = {
    "E": (0.7729981068548344, 0.7055579515066273),
    "I": (0.6811142748149669, 0.057464469736054416),
    "S": (0.23501023019317843, 2.5585618595179382),
    "V": (0.09186701608785974, 0.3958598283268047),
}
_SENTINEL_PAUSE_STATE = {"E": 0.6608, "I": 0.6572, "S": 0.379, "V": 0.046}


def _baselined(mean_m2, current, count=1239):
    st = BehavioralEISV()
    for dim, (mean, m2) in mean_m2.items():
        bl = getattr(st, f"_baseline_{dim}")
        bl.count, bl.mean, bl.m2 = count, mean, m2
    for dim, val in current.items():
        setattr(st, dim, val)
    st.update_count = count
    return st


class TestWelfordMinStd:
    def test_min_std_caps_sensitivity_for_tight_variance(self):
        s = WelfordStats()
        for v in (0.500, 0.501, 0.499, 0.500, 0.501, 0.499):  # std ~0.0009
            s.update(v)
        raw = s.z_score(0.60)
        floored = s.z_score(0.60, min_std=0.05)
        assert abs(floored) < abs(raw)
        assert abs(floored) == pytest.approx(abs(0.60 - s.mean) / 0.05, rel=1e-3)

    def test_default_call_unchanged(self):
        s = WelfordStats()
        for v in (0.1, 0.5, 0.9, 0.3, 0.7):
            s.update(v)
        assert s.z_score(0.5) == pytest.approx((0.5 - s.mean) / s.std)

    def test_below_min_count_returns_zero(self):
        s = WelfordStats()
        for v in (0.5, 0.5, 0.5):  # count < 5
            s.update(v)
        assert s.z_score(0.9, min_std=0.05) == 0.0

    def test_zero_variance_with_floor_scores_at_floor(self):
        # Exactly-zero variance: with min_std=0 the 1e-9 guard returns 0.0
        # (unchanged); with min_std=0.05 a move now scores at the floor scale
        # rather than being invisible. Intended regime change — pinned here.
        s = WelfordStats()
        for _ in range(10):
            s.update(0.5)
        assert s.std == 0.0
        assert s.z_score(0.6) == 0.0  # default min_std=0.0 → unchanged
        assert s.z_score(0.6, min_std=0.05) == pytest.approx((0.6 - 0.5) / 0.05)
        assert s.z_score(0.5, min_std=0.05) == 0.0  # no move → no deviation

    def test_large_move_still_scores_with_floor(self):
        # A genuine, large deviation must still register even with the floor.
        s = WelfordStats()
        for v in (0.500, 0.501, 0.499, 0.500, 0.501):
            s.update(v)
        z = s.z_score(0.95, min_std=0.05)  # +0.45 from mean
        assert abs(z) >= 3.0


class TestStableSentinelNotFalselyPaused:
    def test_small_healthy_wobble_is_not_high_risk(self):
        state = _baselined(_SENTINEL_BASELINE, _SENTINEL_PAUSE_STATE)
        result = assess_behavioral_state(state, rho=0.0)
        # With absolute EISV-safe gating this small wobble is no longer
        # high-risk (it scored 0.94 / high-risk before the gate).
        assert result.verdict != "high-risk"
        assert result.risk < RISK_CAUTION_THRESHOLD
        assert result.components["low_E"] == 0.0
        assert result.components["low_I"] == 0.0
        assert result.components["high_S"] == 0.0
        assert result.components["high_V"] == 0.0

    def test_rho_and_continuity_energy_still_score_inside_safe_gate(self):
        state = _baselined(_SENTINEL_BASELINE, _SENTINEL_PAUSE_STATE)
        result = assess_behavioral_state(state, rho=-0.5, continuity_energy=1.0)
        assert result.components["low_E"] == 0.0
        assert result.components["low_I"] == 0.0
        assert result.components["high_S"] == 0.0
        assert result.components["adversarial_rho"] > 0.0
        assert result.components["high_CE"] > 0.0

    def test_genuine_entropy_spike_still_flags(self):
        # The same tight baseline, but a real large entropy excursion must
        # still produce a non-zero high_S risk component once EISV leaves the
        # absolute safe gate.
        state = _baselined(_SENTINEL_BASELINE, {"E": 0.773, "I": 0.681, "S": 0.70, "V": 0.092})
        result = assess_behavioral_state(state, rho=0.0)
        assert result.components.get("high_S", 0.0) > 0.0

    def test_combined_basin_exit_still_flags(self):
        state = _baselined(_SENTINEL_BASELINE, {"E": 0.35, "I": 0.34, "S": 0.65, "V": 0.20})
        result = assess_behavioral_state(state, rho=0.0)
        assert result.components["low_E"] > 0.0
        assert result.components["low_I"] > 0.0
        assert result.components["high_S"] > 0.0
        assert result.components["high_V"] > 0.0
        assert result.risk >= RISK_CAUTION_THRESHOLD

    def test_custom_slow_alpha_floor_preserves_outside_gate_signal(self):
        state = _baselined(
            {
                "E": (0.62, 0.0),
                "I": (0.62, 0.0),
                "S": (0.20, 0.0),
                "V": (0.00, 0.0),
            },
            {"E": 0.59, "I": 0.59, "S": 0.20, "V": 0.0},
            count=50,
        )
        state.alphas["E"] = 0.01
        state.alphas["I"] = 0.01

        result = assess_behavioral_state(state, rho=0.0)

        assert result.components["low_E"] > 0.0
        assert result.components["low_I"] > 0.0
        assert result.risk >= RISK_CAUTION_THRESHOLD


class TestDeviation:
    def test_default_deviation_uses_raw_baseline_std(self):
        state = _baselined(_SENTINEL_BASELINE, _SENTINEL_PAUSE_STATE)
        z_E = state.deviation("E")
        assert z_E == pytest.approx(
            (0.6608 - 0.7729981068548344) / state._baseline_E.std,
            rel=1e-2,
        )

    def test_deviation_accepts_ema_derived_min_std(self):
        state = _baselined(_SENTINEL_BASELINE, _SENTINEL_PAUSE_STATE)
        z_E = state.deviation("E", min_std=eisv_min_std_for_dimension("E"))
        assert z_E == pytest.approx(
            (0.6608 - 0.7729981068548344) / eisv_min_std_for_dimension("E"),
            rel=1e-2,
        )
