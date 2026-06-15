"""Regression tests for the governance-monitor math modules.

src/monitor_regime.py and src/monitor_risk.py are pure(-ish) calculation
surfaces with no direct test coverage. Both carry *calibrated* thresholds —
the regime cutoffs were re-tuned 2026-03-16 against observed agent
distributions, and the phi->risk band mapping keys off PHI_SAFE_THRESHOLD /
PHI_CAUTION_THRESHOLD. Untested calibration math is the classic place a
regression hides silently (a number quietly goes wrong, nothing fails), so
these tests pin the current input->output contract.

The state objects these functions consume are large GovernanceState instances;
the tests use minimal fakes carrying only the attributes each function reads,
which also documents that surface explicitly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from config.governance_config import GovernanceConfig as GovConfig
from src.monitor_regime import detect_regime
from src.monitor_risk import estimate_risk


# --------------------------------------------------------------------------- #
# detect_regime
# --------------------------------------------------------------------------- #

def _regime_state(I, S, V, S_history, I_history, locked=0):
    """Minimal stand-in for GovernanceState as read by detect_regime."""
    return SimpleNamespace(
        I=I,
        S=S,
        V=V,
        S_history=list(S_history),
        I_history=list(I_history),
        locked_persistence_count=locked,
    )


class TestDetectRegime:
    def test_insufficient_history_is_exploration(self):
        # Fewer than 2 history points → cannot compute deltas → EXPLORATION.
        state = _regime_state(0.80, 0.17, 0.01, [0.17], [0.80])
        assert detect_regime(state) == "EXPLORATION"

    def test_stable_requires_three_consecutive(self):
        # I>=0.85 and S<=0.10 increments the persistence counter; STABLE only
        # latches on the 3rd consecutive qualifying step.
        state = _regime_state(0.90, 0.05, 0.01, [0.05, 0.05], [0.90, 0.90], locked=0)
        assert detect_regime(state) != "STABLE"   # count → 1
        assert detect_regime(state) != "STABLE"   # count → 2
        assert detect_regime(state) == "STABLE"    # count → 3, latches
        assert state.locked_persistence_count == 3

    def test_non_stable_step_resets_persistence(self):
        state = _regime_state(0.90, 0.05, 0.01, [0.05, 0.05], [0.90, 0.90], locked=2)
        # A qualifying step would latch, but a low-I step must reset the counter.
        state.I = 0.50
        detect_regime(state)
        assert state.locked_persistence_count == 0

    def test_divergence_rising_s_with_elevated_v(self):
        # S rising (dS > eps) AND V elevated (> 0.05) → DIVERGENCE.
        state = _regime_state(0.50, 0.30, 0.10, [0.20, 0.20], [0.50, 0.50])
        assert detect_regime(state) == "DIVERGENCE"

    def test_rising_s_without_v_is_not_divergence(self):
        # Same S rise but V below the 0.05 floor → not flagged as divergence.
        state = _regime_state(0.50, 0.30, 0.01, [0.20, 0.20], [0.50, 0.50])
        assert detect_regime(state) != "DIVERGENCE"

    def test_transition_s_falling_i_rising(self):
        # S falling while I rising → recovering → TRANSITION.
        state = _regime_state(0.80, 0.20, 0.01, [0.30, 0.30], [0.70, 0.70])
        assert detect_regime(state) == "TRANSITION"

    def test_convergence_moderate_stable_s_healthy_i(self):
        # S < 0.25, not rising, I healthy (> 0.70) → CONVERGENCE.
        state = _regime_state(0.80, 0.20, 0.01, [0.20, 0.20], [0.80, 0.80])
        assert detect_regime(state) == "CONVERGENCE"

    def test_behavioral_values_override_state_when_confident(self):
        # Calm ODE state (would read CONVERGENCE) but a confident behavioral
        # signal shows divergence → behavioral wins.
        state = _regime_state(0.80, 0.20, 0.01, [0.20, 0.20], [0.80, 0.80])
        behavioral = SimpleNamespace(
            confidence=0.5, I=0.50, S=0.30, V=0.10,
            S_history=[0.20, 0.20], I_history=[0.50, 0.50],
        )
        assert detect_regime(state, behavioral) == "DIVERGENCE"

    def test_low_confidence_behavioral_is_ignored(self):
        # confidence < 0.3 → fall back to ODE state (CONVERGENCE here).
        state = _regime_state(0.80, 0.20, 0.01, [0.20, 0.20], [0.80, 0.80])
        behavioral = SimpleNamespace(
            confidence=0.1, I=0.50, S=0.30, V=0.10,
            S_history=[0.20, 0.20], I_history=[0.50, 0.50],
        )
        assert detect_regime(state, behavioral) == "CONVERGENCE"


# --------------------------------------------------------------------------- #
# estimate_risk — phi -> risk band mapping
# --------------------------------------------------------------------------- #

def _risk_state():
    """Minimal stand-in for GovernanceState as read by estimate_risk.

    Empty *_history lists keep the velocity-risk term at zero so the phi-band
    mapping is isolated.
    """
    return SimpleNamespace(
        coherence=0.5,
        E_history=[],
        I_history=[],
        S_history=[],
        V_history=[],
        risk_history=[],
    )


# Expected band values derived from PHI_SAFE_THRESHOLD=0.08, PHI_CAUTION=0.0
# with RISK_PHI_WEIGHT=1.0 / RISK_TRADITIONAL_WEIGHT=0.0 and zero velocity:
#   phi >= 0.08:        risk = 0.3 - (phi - 0.08) * 0.5
#   0.0 <= phi < 0.08:  risk = 0.3 + (0.08 - phi) / 0.08 * 0.4
#   phi < 0.0:          risk = min(1.0, 0.7 + |phi| * 2.0)
@pytest.mark.parametrize(
    "phi, expected",
    [
        (0.58, 0.05),   # comfortably safe → low risk
        (0.08, 0.30),   # exactly at the safe threshold
        (0.04, 0.50),   # caution band midpoint
        (0.00, 0.70),   # bottom of caution band
        (-0.05, 0.80),  # unsafe → high risk
        (-0.20, 1.00),  # deeply unsafe → clamped to 1.0
    ],
)
def test_phi_band_mapping(phi, expected):
    state = _risk_state()
    score_result = {"phi": phi, "verdict": "n/a"}
    risk = estimate_risk(state, {"response_text": ""},
                         score_result=score_result, behavioral_risk=None)
    assert risk == pytest.approx(expected, abs=1e-6)


def test_risk_is_monotonic_in_phi():
    # Lower phi (worse) must never produce lower risk than higher phi.
    state = _risk_state
    phis = [0.58, 0.08, 0.04, 0.0, -0.05, -0.20]
    risks = [
        estimate_risk(state(), {"response_text": ""},
                      score_result={"phi": p, "verdict": "n/a"},
                      behavioral_risk=None)
        for p in phis
    ]
    # phis are descending → risks must be non-decreasing
    assert risks == sorted(risks)


def test_risk_clamped_to_unit_interval():
    state = _risk_state()
    risk = estimate_risk(state, {"response_text": ""},
                         score_result={"phi": -10.0, "verdict": "n/a"},
                         behavioral_risk=None)
    assert 0.0 <= risk <= 1.0
    assert risk == pytest.approx(1.0)


def test_risk_appends_to_history():
    state = _risk_state()
    estimate_risk(state, {"response_text": ""},
                  score_result={"phi": 0.5, "verdict": "n/a"},
                  behavioral_risk=None)
    assert len(state.risk_history) == 1


class TestBehavioralRiskPath:
    def test_behavioral_risk_is_primary_when_enabled(self, monkeypatch):
        # When the behavioral verdict is enabled and a behavioral_risk is
        # supplied, it becomes the primary signal (plus zero velocity here).
        monkeypatch.setattr(GovConfig, "BEHAVIORAL_VERDICT_ENABLED", True)
        state = _risk_state()
        risk = estimate_risk(state, {"response_text": ""},
                             score_result=None, behavioral_risk=0.42)
        assert risk == pytest.approx(0.42)

    def test_behavioral_risk_clamped(self, monkeypatch):
        monkeypatch.setattr(GovConfig, "BEHAVIORAL_VERDICT_ENABLED", True)
        state = _risk_state()
        risk = estimate_risk(state, {"response_text": ""},
                             score_result=None, behavioral_risk=5.0)
        assert risk == pytest.approx(1.0)
