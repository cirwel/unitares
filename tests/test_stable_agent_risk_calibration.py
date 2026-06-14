"""Calibration: ultra-stable agents must not be falsely flagged high-risk for
small, absolutely-healthy EISV wobbles.

Regression for the 2026-06-13 Sentinel pause: a long-running monitor with a
very tight behavioral baseline (std ~0.007-0.024) had a perfectly healthy
fluctuation (E 0.77->0.66, I 0.68->0.66) scored as a many-sigma "severe
deviation" -> risk 0.94 -> high-risk -> cirs_block -> paused ~18h. The
MIN_MEANINGFUL_EISV_STD floor caps z-score sensitivity at a meaningful
resolution while preserving genuine multi-tenth moves and absolute floors.
"""

import pytest

from src.agent_behavioral_baseline import WelfordStats
from src.behavioral_state import BehavioralEISV, MIN_MEANINGFUL_EISV_STD
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


class TestWelfordSigmaFloor:
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
        # With the floor this small, absolutely-healthy wobble is no longer
        # high-risk (it scored 0.94 / high-risk before the floor).
        assert result.verdict != "high-risk"
        assert result.risk < RISK_CAUTION_THRESHOLD

    def test_genuine_entropy_spike_still_flags(self):
        # The same tight baseline, but a real large entropy excursion must
        # still produce a non-zero high_S risk component (floor preserves signal).
        state = _baselined(_SENTINEL_BASELINE, {"E": 0.773, "I": 0.681, "S": 0.70, "V": 0.092})
        result = assess_behavioral_state(state, rho=0.0)
        assert result.components.get("high_S", 0.0) > 0.0


class TestDeviationUsesFloor:
    def test_deviation_applies_min_std(self):
        state = _baselined(_SENTINEL_BASELINE, _SENTINEL_PAUSE_STATE)
        # E moved 0.112 from a baseline whose true std is ~0.024; without the
        # floor this is ~-4.7 sigma, with the 0.05 floor it is ~-2.24.
        z_E = state.deviation("E")
        assert z_E == pytest.approx((0.6608 - 0.7729981068548344) / MIN_MEANINGFUL_EISV_STD, rel=1e-2)
