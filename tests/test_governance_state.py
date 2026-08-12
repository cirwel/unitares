"""
Tests for src/governance_state.py — GovernanceState wrapper.

Tests properties, serialization, validation, interpretation layer, and quick helpers.
"""

import pytest
import sys
import numpy as np
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.governance_state import GovernanceState
from governance_core import State, Theta, DEFAULT_STATE, DEFAULT_THETA


# ============================================================================
# GovernanceState — construction and properties
# ============================================================================

class TestGovernanceStateConstruction:

    def test_default(self):
        state = GovernanceState()
        assert 0 <= state.E <= 1
        assert 0 <= state.I <= 1
        assert 0 <= state.S <= 1

    def test_custom_eisv(self):
        state = GovernanceState()
        state.unitaires_state = State(E=0.7, I=0.8, S=0.1, V=0.0)
        assert state.E == 0.7
        assert state.I == 0.8
        assert state.S == 0.1
        assert state.V == 0.0

    def test_lambda1_property(self):
        state = GovernanceState()
        l1 = state.lambda1
        assert isinstance(l1, float)
        assert 0 <= l1 <= 1


# ============================================================================
# to_dict / from_dict roundtrip
# ============================================================================

class TestGovernanceStateSerialization:

    def test_to_dict(self):
        state = GovernanceState()
        state.unitaires_state = State(E=0.7, I=0.8, S=0.1, V=0.05)
        state.coherence = 0.9
        d = state.to_dict()
        assert d["E"] == 0.7
        assert d["I"] == 0.8
        assert d["S"] == 0.1
        assert d["V"] == 0.05
        assert d["coherence"] == 0.9
        assert "lambda1" in d
        assert "regime" in d

    def test_to_dict_with_history(self):
        state = GovernanceState()
        state.E_history = [0.5, 0.6, 0.7]
        state.I_history = [0.5, 0.6, 0.7]
        state.S_history = [0.1, 0.1, 0.1]
        state.V_history = [0.0, 0.0, 0.0]
        state.coherence_history = [0.8, 0.9, 0.9]
        state.risk_history = [0.1, 0.1, 0.1]
        state.decision_history = ["continue", "continue"]
        state.timestamp_history = ["2025-01-01", "2025-01-02"]
        d = state.to_dict_with_history()
        assert len(d["E_history"]) == 3
        assert "unitaires_state" in d
        assert "unitaires_theta" in d

    def test_history_capping(self):
        state = GovernanceState()
        state.E_history = list(range(200))
        state.I_history = list(range(200))
        state.S_history = list(range(200))
        state.V_history = list(range(200))
        state.coherence_history = list(range(200))
        state.risk_history = list(range(200))
        state.decision_history = list(range(200))
        state.timestamp_history = list(range(200))
        d = state.to_dict_with_history(max_history=50)
        assert len(d["E_history"]) == 50
        assert len(d["decision_history"]) == 50

    def test_from_dict_basic(self):
        data = {
            "E": 0.7, "I": 0.8, "S": 0.1, "V": 0.0,
            "coherence": 0.9, "void_active": False,
            "time": 10.0, "update_count": 5,
        }
        state = GovernanceState.from_dict(data)
        assert state.E == 0.7
        assert state.I == 0.8
        assert state.update_count == 5

    def test_from_dict_with_unitaires_state(self):
        data = {
            "unitaires_state": {"E": 0.6, "I": 0.7, "S": 0.2, "V": 0.05},
            "unitaires_theta": {"C1": 0.5, "eta1": 0.1},
            "coherence": 0.8,
        }
        state = GovernanceState.from_dict(data)
        assert state.E == 0.6
        assert state.I == 0.7

    def test_from_dict_with_history(self):
        data = {
            "E": 0.7, "I": 0.8, "S": 0.1, "V": 0.0,
            "E_history": [0.5, 0.6, 0.7],
            "I_history": [0.5, 0.6, 0.8],
            "S_history": [0.1, 0.1, 0.1],
            "V_history": [0.0, 0.0, 0.0],
            "coherence_history": [0.7, 0.8, 0.9],
            "risk_history": [0.2, 0.1, 0.1],
            "decision_history": ["continue", "approve"],
            "regime": "convergence",
            "regime_history": ["divergence", "convergence"],
        }
        state = GovernanceState.from_dict(data)
        assert len(state.E_history) == 3
        assert state.regime == "convergence"
        assert len(state.regime_history) == 2

    def test_from_dict_empty(self):
        state = GovernanceState.from_dict({})
        # Should use defaults
        assert state.E == DEFAULT_STATE.E
        assert state.update_count == 0

    def test_roundtrip(self):
        state = GovernanceState()
        state.unitaires_state = State(E=0.7, I=0.8, S=0.15, V=0.03)
        state.update_count = 10
        state.regime = "convergence"
        d = state.to_dict_with_history()
        loaded = GovernanceState.from_dict(d)
        assert loaded.E == pytest.approx(0.7, abs=0.01)
        assert loaded.I == pytest.approx(0.8, abs=0.01)
        assert loaded.update_count == 10
        assert loaded.regime == "convergence"


# ============================================================================
# validate
# ============================================================================

class TestGovernanceStateValidate:

    def test_valid_state(self):
        state = GovernanceState()
        is_valid, errors = state.validate()
        assert is_valid is True
        assert len(errors) == 0

    def test_e_out_of_bounds(self):
        state = GovernanceState()
        state.unitaires_state = State(E=1.5, I=0.5, S=0.1, V=0.0)
        is_valid, errors = state.validate()
        assert is_valid is False
        assert any("E out of bounds" in e for e in errors)

    def test_negative_i(self):
        state = GovernanceState()
        state.unitaires_state = State(E=0.5, I=-0.1, S=0.1, V=0.0)
        is_valid, errors = state.validate()
        assert is_valid is False
        assert any("I out of bounds" in e for e in errors)

    def test_nan_detection(self):
        state = GovernanceState()
        state.unitaires_state = State(E=float('nan'), I=0.5, S=0.1, V=0.0)
        is_valid, errors = state.validate()
        assert is_valid is False
        assert any("NaN" in e for e in errors)

    def test_inf_detection(self):
        state = GovernanceState()
        state.unitaires_state = State(E=float('inf'), I=0.5, S=0.1, V=0.0)
        is_valid, errors = state.validate()
        assert is_valid is False

    def test_history_consistency(self):
        state = GovernanceState()
        state.E_history = [0.5, 0.6, 0.7]
        state.I_history = [0.5, 0.6]  # one shorter — allowed (diff <= 1)
        state.S_history = [0.1, 0.1, 0.1]
        state.V_history = [0.0, 0.0, 0.0]
        state.coherence_history = [0.8, 0.8, 0.8]
        state.risk_history = [0.1, 0.1, 0.1]
        is_valid, errors = state.validate()
        assert is_valid is True

    def test_history_large_mismatch(self):
        state = GovernanceState()
        state.E_history = [0.5, 0.6, 0.7, 0.8, 0.9]
        state.I_history = [0.5]  # 4 entries shorter
        state.S_history = [0.1, 0.1, 0.1, 0.1, 0.1]
        state.V_history = [0.0, 0.0, 0.0, 0.0, 0.0]
        state.coherence_history = [0.8, 0.8, 0.8, 0.8, 0.8]
        state.risk_history = [0.1, 0.1, 0.1, 0.1, 0.1]
        is_valid, errors = state.validate()
        assert is_valid is False
        assert any("History length mismatch" in e for e in errors)


# ============================================================================
# interpret_state
# ============================================================================

class TestInterpretState:

    def test_healthy_state(self):
        state = GovernanceState()
        state.unitaires_state = State(E=0.7, I=0.8, S=0.1, V=0.01)
        state.coherence = 0.9
        result = state.interpret_state(risk_score=0.1)
        assert result["health"] == "healthy"
        assert result["basin"] == "high"

    def test_critical_state(self):
        state = GovernanceState()
        state.unitaires_state = State(E=0.3, I=0.2, S=0.8, V=0.5)
        state.coherence = 0.2
        result = state.interpret_state(risk_score=0.8)
        assert result["health"] == "critical"

    def test_at_risk_state(self):
        state = GovernanceState()
        state.coherence = 0.4
        result = state.interpret_state(risk_score=0.6)
        assert result["health"] == "at_risk"

    def test_result_structure(self):
        state = GovernanceState()
        result = state.interpret_state()
        assert "health" in result
        assert "basin" in result
        assert "mode" in result
        assert "trajectory" in result
        assert "guidance" in result
        assert "borderline" in result

    def test_auto_risk_estimation(self):
        state = GovernanceState()
        state.unitaires_state = State(E=0.7, I=0.8, S=0.1, V=0.0)
        state.coherence = 0.9
        # Don't provide risk_score — should auto-estimate
        result = state.interpret_state()
        assert result["health"] in ["healthy", "moderate", "at_risk", "critical"]


# ============================================================================
# _interpret_health
# ============================================================================

class TestInterpretHealth:

    def test_critical(self):
        state = GovernanceState()
        assert state._interpret_health(0.5, 0.8) == "critical"

    def test_at_risk(self):
        state = GovernanceState()
        assert state._interpret_health(0.5, 0.6) == "at_risk"

    def test_low_controller_feedback_does_not_mean_unstable(self):
        state = GovernanceState()
        assert state._interpret_health(0.2, 0.1) == "healthy"

    def test_controller_direction_does_not_change_health(self):
        state = GovernanceState()
        assert state._interpret_health(0.1, 0.4) == state._interpret_health(0.9, 0.4)

    def test_healthy(self):
        state = GovernanceState()
        assert state._interpret_health(0.8, 0.1) == "healthy"

    def test_moderate(self):
        state = GovernanceState()
        assert state._interpret_health(0.5, 0.4) == "moderate"


# ============================================================================
# _interpret_basin
# ============================================================================

class TestInterpretBasin:
    """Basin classification uses multi-dimensional bounds via classify_basin().

    HIGH requires: E>=0.6, I>=0.7, S<=0.25, |V|<=0.15, coh>=0.45, risk<=0.45.
    LOW triggers on: I<0.5, coh<0.40, |V|>0.30, or risk>=0.70.
    BOUNDARY is the complement.
    """

    def test_high(self):
        state = GovernanceState()
        # Set all dimensions to healthy values
        state.unitaires_state = State(E=0.7, I=0.8, S=0.1, V=0.01)
        state.coherence = 0.6
        assert state._interpret_basin(0.7, 0.8, risk_score=0.1) == "high"

    def test_low_due_to_I(self):
        state = GovernanceState()
        state.unitaires_state = State(E=0.5, I=0.3, S=0.1, V=0.0)
        state.coherence = 0.5
        assert state._interpret_basin(0.5, 0.3) == "low"

    def test_low_due_to_coherence(self):
        state = GovernanceState()
        state.unitaires_state = State(E=0.7, I=0.8, S=0.1, V=0.0)
        state.coherence = 0.35  # Below COHERENCE_CRITICAL_THRESHOLD
        assert state._interpret_basin(0.7, 0.8, risk_score=0.1) == "low"

    def test_boundary_moderate_state(self):
        """State that's neither fully healthy nor critically degraded."""
        state = GovernanceState()
        # E too low for HIGH, but I above LOW threshold
        state.unitaires_state = State(E=0.5, I=0.6, S=0.1, V=0.0)
        state.coherence = 0.5
        assert state._interpret_basin(0.5, 0.6, risk_score=0.2) == "boundary"

    def test_boundary_high_entropy(self):
        """High S pushes out of HIGH but doesn't breach LOW thresholds."""
        state = GovernanceState()
        state.unitaires_state = State(E=0.7, I=0.8, S=0.4, V=0.0)
        state.coherence = 0.6
        assert state._interpret_basin(0.7, 0.8, risk_score=0.1) == "boundary"


# ============================================================================
# _interpret_mode
# ============================================================================

class TestInterpretMode:

    def test_collaborating(self):
        state = GovernanceState()
        mode, _ = state._interpret_mode(0.7, 0.7, 0.5)
        assert mode == "collaborating"

    def test_building_alone(self):
        state = GovernanceState()
        mode, _ = state._interpret_mode(0.7, 0.7, 0.1)
        assert mode == "building_alone"

    def test_stalled(self):
        state = GovernanceState()
        mode, _ = state._interpret_mode(0.2, 0.2, 0.1)
        assert mode == "stalled"

    def test_exploring_alone(self):
        state = GovernanceState()
        mode, _ = state._interpret_mode(0.7, 0.2, 0.1)
        assert mode == "exploring_alone"

    def test_borderline_detection(self):
        state = GovernanceState()
        mode, borderline = state._interpret_mode(0.52, 0.8, 0.1)
        # E=0.52 is near threshold 0.5 ± 0.1
        assert "E" in borderline

    def test_hysteresis_with_prev_mode(self):
        state = GovernanceState()
        # Slightly below threshold — without hysteresis would be low
        mode1, _ = state._interpret_mode(0.48, 0.7, 0.1, prev_mode=None)
        mode2, _ = state._interpret_mode(0.48, 0.7, 0.1, prev_mode="exploring_alone")
        # With exploring in prev_mode, E threshold shifts down → still counts as high
        assert mode2 in ["building_alone", "exploring_alone", "collaborating", "exploring_together"]


# ============================================================================
# _interpret_trajectory
# ============================================================================

class TestInterpretTrajectory:

    def test_improving(self):
        state = GovernanceState()
        state.unitaires_state = State(E=0.5, I=0.5, S=0.1, V=0.15)
        assert state._interpret_trajectory() == "improving"

    def test_declining(self):
        state = GovernanceState()
        state.unitaires_state = State(E=0.5, I=0.5, S=0.1, V=-0.15)
        assert state._interpret_trajectory() == "declining"

    def test_stable(self):
        state = GovernanceState()
        state.unitaires_state = State(E=0.5, I=0.5, S=0.1, V=0.05)
        assert state._interpret_trajectory() == "stable"

    def test_stuck(self):
        state = GovernanceState()
        state.unitaires_state = State(E=0.5, I=0.5, S=0.1, V=0.05)
        state.decision_history = ["continue", "pause", "pause", "pause", "pause"]
        assert state._interpret_trajectory() == "stuck"


# ============================================================================
# _generate_guidance
# ============================================================================

class TestGenerateGuidance:

    def test_critical_guidance(self):
        state = GovernanceState()
        g = state._generate_guidance("critical", "low", "stalled", "declining", "mixed", {})
        assert g is not None
        assert "Circuit breaker" in g

    def test_declining_guidance(self):
        state = GovernanceState()
        g = state._generate_guidance("moderate", "high", "building_alone", "declining", "mixed", {})
        assert g is not None
        assert "negative" in g.lower()

    def test_stuck_guidance(self):
        state = GovernanceState()
        g = state._generate_guidance("moderate", "high", "building_alone", "stuck", "mixed", {})
        assert g is not None
        assert "different approach" in g.lower()

    def test_stalled_guidance(self):
        state = GovernanceState()
        g = state._generate_guidance("moderate", "low", "stalled", "stable", "mixed", {})
        assert g is not None
        assert "Low activity" in g

    def test_healthy_building_no_guidance(self):
        state = GovernanceState()
        g = state._generate_guidance("healthy", "high", "building_alone", "stable", "mixed", {})
        # Healthy + building_alone → priority 5 returns None (no action needed)
        assert g is None

    def test_moderate_building_suggests_dialectic(self):
        state = GovernanceState()
        g = state._generate_guidance("moderate", "high", "building_alone", "stable", "mixed", {})
        # Non-healthy + building_alone + stable → suggests dialectic
        assert g is not None
        assert "dialectic" in g.lower()

    def test_healthy_collaborating_no_guidance(self):
        state = GovernanceState()
        g = state._generate_guidance("healthy", "high", "collaborating", "stable", "mixed", {})
        assert g is None  # Best state — no guidance needed


# ============================================================================
# _estimate_risk_simple
# ============================================================================

class TestEstimateRiskSimple:

    def test_low_risk(self):
        state = GovernanceState()
        state.unitaires_state = State(E=0.7, I=0.8, S=0.1, V=0.0)
        state.coherence = 0.9
        risk = state._estimate_risk_simple()
        assert risk < 0.3

    def test_high_risk(self):
        state = GovernanceState()
        state.unitaires_state = State(E=0.3, I=0.2, S=0.9, V=0.5)
        state.coherence = 0.2
        risk = state._estimate_risk_simple()
        assert risk > 0.5

    def test_bounded(self):
        state = GovernanceState()
        state.unitaires_state = State(E=0.0, I=0.0, S=1.0, V=1.0)
        state.coherence = 0.0
        risk = state._estimate_risk_simple()
        assert 0 <= risk <= 1

    def test_fallback_risk_is_symmetric_in_signed_v(self):
        state = GovernanceState()
        state.unitaires_state = State(E=0.5, I=0.5, S=0.2, V=-0.4)
        negative_v = state._estimate_risk_simple()
        state.unitaires_state = State(E=0.5, I=0.5, S=0.2, V=0.4)
        assert state._estimate_risk_simple() == negative_v
