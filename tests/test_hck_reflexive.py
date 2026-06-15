"""Regression tests for src/hck_reflexive.py (HCK v3.0 reflexive control math).

These are pure, stateless functions feeding the PI controller and stability
tracking: update coherence rho(t), continuity energy CE(t), and PI-gain
modulation. They had no direct coverage. Pin the input->output contract so a
change to the coherence/energy formulas is caught.

NOTE — modulate_gains doc/behavior mismatch (surfaced by this test):
the docstring's worked example claims `rho=0 -> factor=0.75`, but the
implementation computes `max(min_factor, (rho + 1) / 2)`, which yields 0.5 at
rho=0 and a *flat* 0.5 floor for every rho <= 0. The tests below pin the actual
implemented behavior (0.5), not the docstring's number. Flagged for a human to
decide whether the doc or the formula is wrong.
"""

from __future__ import annotations

import pytest

from src.hck_reflexive import (
    compute_update_coherence,
    compute_continuity_energy,
    modulate_gains,
)


# --------------------------------------------------------------------------- #
# compute_update_coherence — directional alignment of E and I updates
# --------------------------------------------------------------------------- #

class TestUpdateCoherence:
    def test_aligned_updates_approach_plus_one(self):
        assert compute_update_coherence(0.2, 0.3) == pytest.approx(1.0, abs=1e-6)
        assert compute_update_coherence(-0.2, -0.3) == pytest.approx(1.0, abs=1e-6)

    def test_opposed_updates_approach_minus_one(self):
        assert compute_update_coherence(0.2, -0.3) == pytest.approx(-1.0, abs=1e-6)
        assert compute_update_coherence(-0.2, 0.3) == pytest.approx(-1.0, abs=1e-6)

    def test_zero_delta_is_neutral(self):
        assert compute_update_coherence(0.0, 0.5) == pytest.approx(0.0)
        assert compute_update_coherence(0.5, 0.0) == pytest.approx(0.0)
        assert compute_update_coherence(0.0, 0.0) == pytest.approx(0.0)

    def test_result_always_within_unit_range(self):
        for dE, dI in [(1e6, 1e6), (-1e6, 1e6), (1e-9, -1e-9), (5.0, -0.001)]:
            rho = compute_update_coherence(dE, dI)
            assert -1.0 <= rho <= 1.0

    def test_magnitude_independent_only_direction_matters(self):
        small = compute_update_coherence(0.01, 0.02)
        large = compute_update_coherence(100.0, 200.0)
        assert small == pytest.approx(large, abs=1e-4)


# --------------------------------------------------------------------------- #
# compute_continuity_energy — work to maintain consistency as state evolves
# --------------------------------------------------------------------------- #

class TestContinuityEnergy:
    def test_insufficient_history_is_zero(self):
        assert compute_continuity_energy([]) == 0.0
        assert compute_continuity_energy([{"E": 0.5}]) == 0.0

    def test_pure_state_delta_weighted_by_alpha_state(self):
        # Single EISV change of 0.1 (in E), no route changes.
        # CE = alpha_state * avg_state_delta = 0.6 * 0.1
        history = [
            {"E": 0.5, "I": 0.5, "S": 0.5, "V": 0.0},
            {"E": 0.6, "I": 0.5, "S": 0.5, "V": 0.0},
        ]
        assert compute_continuity_energy(history) == pytest.approx(0.06)

    def test_route_flips_weighted_by_alpha_decision(self):
        # Stable EISV, route flips a->b->a over 3 states => 2 changes / 2 = 1.0
        # CE = alpha_decision * 1.0 = 0.4
        history = [
            {"E": 0.5, "I": 0.5, "S": 0.5, "V": 0.0, "route": "a"},
            {"E": 0.5, "I": 0.5, "S": 0.5, "V": 0.0, "route": "b"},
            {"E": 0.5, "I": 0.5, "S": 0.5, "V": 0.0, "route": "a"},
        ]
        assert compute_continuity_energy(history) == pytest.approx(0.4)

    def test_window_limits_states_considered(self):
        # 50 flat states then a big jump only in the last pair; with window=2
        # only the last two states are considered → the jump dominates.
        history = [{"E": 0.5, "I": 0.5, "S": 0.5, "V": 0.0} for _ in range(50)]
        history.append({"E": 1.0, "I": 0.5, "S": 0.5, "V": 0.0})  # +0.5 in E
        ce = compute_continuity_energy(history, window=2)
        assert ce == pytest.approx(0.6 * 0.5)

    def test_energy_is_non_negative(self):
        history = [
            {"E": 0.9, "I": 0.1, "S": 0.2, "V": -0.3, "route": "x"},
            {"E": 0.1, "I": 0.9, "S": 0.8, "V": 0.3, "route": "y"},
        ]
        assert compute_continuity_energy(history) >= 0.0


# --------------------------------------------------------------------------- #
# modulate_gains — reduce PI aggressiveness when updates are incoherent
# --------------------------------------------------------------------------- #

class TestModulateGains:
    def test_full_coherence_leaves_gains_unchanged(self):
        kp, ki = modulate_gains(1.0, 2.0, rho=1.0)
        assert kp == pytest.approx(1.0)
        assert ki == pytest.approx(2.0)

    def test_positive_rho_scales_linearly(self):
        # rho=0.5 -> factor = max(0.5, 0.75) = 0.75
        kp, ki = modulate_gains(1.0, 2.0, rho=0.5)
        assert kp == pytest.approx(0.75)
        assert ki == pytest.approx(1.5)

    def test_zero_rho_hits_floor_not_doc_example(self):
        # Implementation: max(0.5, (0+1)/2) = 0.5  (docstring claims 0.75)
        kp, ki = modulate_gains(1.0, 1.0, rho=0.0)
        assert kp == pytest.approx(0.5)
        assert ki == pytest.approx(0.5)

    @pytest.mark.parametrize("rho", [0.0, -0.25, -0.5, -1.0])
    def test_nonpositive_rho_clamped_to_min_factor(self, rho):
        # Every rho <= 0 collapses to the 0.5 floor (flat, not a smooth curve).
        kp, ki = modulate_gains(1.0, 1.0, rho=rho)
        assert kp == pytest.approx(0.5)
        assert ki == pytest.approx(0.5)

    def test_custom_min_factor_respected(self):
        kp, ki = modulate_gains(1.0, 1.0, rho=-1.0, min_factor=0.2)
        assert kp == pytest.approx(0.2)
        assert ki == pytest.approx(0.2)
