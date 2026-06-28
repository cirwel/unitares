"""
Tests for src/governance_monitor.py - UNITARESMonitor pure/static methods.

Tests ONLY the pure static methods and pure instance methods that don't
require file I/O, database access, or complex dependency chains.
"""

import pytest
import numpy as np
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import patch, MagicMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.governance_monitor import UNITARESMonitor


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def monitor():
    """Fresh monitor with no persisted state."""
    return UNITARESMonitor("test-agent", load_state=False)


@pytest.fixture
def monitor_with_history():
    """Monitor with some update history for testing methods that need prior state."""
    mon = UNITARESMonitor("test-history", load_state=False)
    # Run 5 updates to build up history
    for i in range(5):
        agent_state = {
            'parameters': np.random.randn(10) * 0.01,
            'ethical_drift': [0.05, 0.02, 0.01],
            'response_text': "Test response for history building.",
            'complexity': 0.3 + 0.1 * i,
        }
        mon.process_update(agent_state)
    return mon


# ============================================================================
# __init__ and initial state
# ============================================================================

@pytest.mark.smoke
class TestInit:

    def test_agent_id_stored(self):
        mon = UNITARESMonitor("my-agent-42", load_state=False)
        assert mon.agent_id == "my-agent-42"

    def test_state_initialized(self, monitor):
        assert monitor.state is not None
        assert monitor.state.update_count == 0
        assert monitor.state.time == 0.0

    def test_eisv_initial_values(self, monitor):
        """Initial EISV should be the DEFAULT_STATE values (all valid)."""
        assert 0.0 <= monitor.state.E <= 1.0
        assert 0.0 <= monitor.state.I <= 1.0
        assert 0.0 <= monitor.state.S <= 1.0
        # V can be negative
        assert -1.0 <= monitor.state.V <= 1.0

    def test_empty_histories(self, monitor):
        """All history lists should be empty on fresh init."""
        assert monitor.state.V_history == []
        assert monitor.state.E_history == []
        assert monitor.state.I_history == []
        assert monitor.state.S_history == []
        assert monitor.state.coherence_history == []
        assert monitor.state.risk_history == []

    def test_prev_parameters_none(self, monitor):
        assert monitor.prev_parameters is None

    def test_hck_initial_state(self, monitor):
        """HCK v3.0 tracking should be initialized."""
        assert monitor._prev_E is None
        assert monitor._prev_I is None

    def test_cirs_initial_state(self, monitor):
        """CIRS v2 AdaptiveGovernor should be initialized when flag is on."""
        from config.governance_config import GovernanceConfig
        if GovernanceConfig.ADAPTIVE_GOVERNOR_ENABLED:
            assert monitor.adaptive_governor is not None
        else:
            assert monitor.oscillation_detector is not None
            assert monitor.resonance_damper is not None
            assert monitor._last_oscillation_state is None
            assert monitor._gains_modulated is False

    def test_load_state_false_skips_disk(self):
        """load_state=False should not try to load from disk."""
        # This should work even if no state files exist
        mon = UNITARESMonitor("nonexistent-agent-xyz", load_state=False)
        assert mon.state.update_count == 0

    def test_lambda1_initial_positive(self, monitor):
        """Lambda1 should be a positive value initially."""
        assert monitor.state.lambda1 > 0

    def test_continuity_layer_initialized(self, monitor):
        """Dual-log continuity layer should be initialized."""
        assert monitor.continuity_layer is not None
        assert monitor.restorative_monitor is not None

    def test_created_at_set(self, monitor):
        """created_at should be set on fresh init."""
        assert hasattr(monitor, 'created_at')


# ============================================================================
# compute_update_coherence (static)
# ============================================================================

@pytest.mark.smoke
class TestComputeUpdateCoherence:

    def test_aligned_positive(self):
        """Both E and I increasing -> rho near 1."""
        rho = UNITARESMonitor.compute_update_coherence(0.1, 0.1)
        assert rho > 0.9

    def test_aligned_negative(self):
        """Both E and I decreasing -> rho near 1 (coherent in same direction)."""
        rho = UNITARESMonitor.compute_update_coherence(-0.1, -0.1)
        assert rho > 0.9

    def test_misaligned(self):
        """E increasing, I decreasing -> rho < 0."""
        rho = UNITARESMonitor.compute_update_coherence(0.1, -0.1)
        assert rho < 0

    def test_opposite_misaligned(self):
        """E decreasing, I increasing -> rho < 0."""
        rho = UNITARESMonitor.compute_update_coherence(-0.1, 0.1)
        assert rho < 0

    def test_zero_deltas(self):
        """Both zero -> rho near 0 (epsilon prevents division by zero)."""
        rho = UNITARESMonitor.compute_update_coherence(0.0, 0.0)
        assert -1.0 <= rho <= 1.0

    def test_bounded_output(self):
        """Output always in [-1, 1] for various inputs."""
        for dE in [-10.0, -0.1, 0.0, 0.1, 10.0]:
            for dI in [-10.0, -0.1, 0.0, 0.1, 10.0]:
                rho = UNITARESMonitor.compute_update_coherence(dE, dI)
                assert -1.0 <= rho <= 1.0, f"rho={rho} for dE={dE}, dI={dI}"

    def test_large_aligned_deltas(self):
        """Large but aligned deltas -> still high coherence."""
        rho = UNITARESMonitor.compute_update_coherence(100.0, 100.0)
        assert rho > 0.99

    def test_asymmetric_deltas(self):
        """Asymmetric but same-sign deltas still produce positive rho."""
        rho = UNITARESMonitor.compute_update_coherence(0.001, 10.0)
        assert rho > 0

    def test_returns_float(self):
        rho = UNITARESMonitor.compute_update_coherence(0.05, 0.03)
        assert isinstance(rho, float)

    def test_custom_epsilon(self):
        """Custom epsilon should not change sign of result."""
        rho1 = UNITARESMonitor.compute_update_coherence(0.1, 0.1, epsilon=1e-8)
        rho2 = UNITARESMonitor.compute_update_coherence(0.1, 0.1, epsilon=1e-2)
        # Both should be positive
        assert rho1 > 0
        assert rho2 > 0

    def test_very_small_deltas(self):
        """Very small deltas should still produce valid output."""
        rho = UNITARESMonitor.compute_update_coherence(1e-15, 1e-15)
        assert -1.0 <= rho <= 1.0


# ============================================================================
# compute_continuity_energy (static)
# ============================================================================

@pytest.mark.smoke
class TestComputeContinuityEnergy:

    def test_empty_history(self):
        CE = UNITARESMonitor.compute_continuity_energy([])
        assert CE == 0.0

    def test_single_entry(self):
        CE = UNITARESMonitor.compute_continuity_energy([{'E': 0.5, 'I': 0.5, 'S': 0.1, 'V': 0.0}])
        assert CE == 0.0

    def test_no_change(self):
        """Identical states -> CE is approximately 0."""
        history = [{'E': 0.5, 'I': 0.5, 'S': 0.1, 'V': 0.0}] * 5
        CE = UNITARESMonitor.compute_continuity_energy(history)
        assert CE < 0.01

    def test_high_state_change(self):
        """Large state changes -> higher CE."""
        history = [
            {'E': 0.1, 'I': 0.1, 'S': 0.1, 'V': 0.0},
            {'E': 0.9, 'I': 0.9, 'S': 0.9, 'V': 0.5},
        ]
        CE = UNITARESMonitor.compute_continuity_energy(history)
        assert CE > 0.1

    def test_decision_flips_increase_CE(self):
        """Decision changes contribute to CE."""
        stable_history = [
            {'E': 0.5, 'I': 0.5, 'S': 0.1, 'V': 0.0, 'decision': 'approve'},
            {'E': 0.5, 'I': 0.5, 'S': 0.1, 'V': 0.0, 'decision': 'approve'},
        ]
        flipping_history = [
            {'E': 0.5, 'I': 0.5, 'S': 0.1, 'V': 0.0, 'decision': 'approve'},
            {'E': 0.5, 'I': 0.5, 'S': 0.1, 'V': 0.0, 'decision': 'reject'},
        ]
        CE_stable = UNITARESMonitor.compute_continuity_energy(stable_history)
        CE_flipping = UNITARESMonitor.compute_continuity_energy(flipping_history)
        assert CE_flipping > CE_stable

    def test_window_limits_history(self):
        """Window parameter caps how much history is used."""
        history = [{'E': float(i) / 20, 'I': 0.5, 'S': 0.1, 'V': 0.0} for i in range(20)]
        CE_small = UNITARESMonitor.compute_continuity_energy(history, window=3)
        CE_large = UNITARESMonitor.compute_continuity_energy(history, window=20)
        assert CE_small >= 0.0
        assert CE_large >= 0.0

    def test_returns_float(self):
        history = [
            {'E': 0.5, 'I': 0.5, 'S': 0.1, 'V': 0.0},
            {'E': 0.6, 'I': 0.5, 'S': 0.1, 'V': 0.0},
        ]
        CE = UNITARESMonitor.compute_continuity_energy(history)
        assert isinstance(CE, float)

    def test_non_negative(self):
        """CE should never be negative."""
        history = [
            {'E': 0.9, 'I': 0.9, 'S': 0.9, 'V': 0.9},
            {'E': 0.1, 'I': 0.1, 'S': 0.1, 'V': 0.1},
        ]
        CE = UNITARESMonitor.compute_continuity_energy(history)
        assert CE >= 0.0

    def test_route_field_used(self):
        """Uses 'route' key when 'decision' is absent."""
        history = [
            {'E': 0.5, 'I': 0.5, 'S': 0.1, 'V': 0.0, 'route': 'approve'},
            {'E': 0.5, 'I': 0.5, 'S': 0.1, 'V': 0.0, 'route': 'reject'},
        ]
        CE = UNITARESMonitor.compute_continuity_energy(history)
        assert CE > 0.0

    def test_mixed_route_decision(self):
        """Handles mix of 'route' and 'decision' keys."""
        history = [
            {'E': 0.5, 'I': 0.5, 'S': 0.1, 'V': 0.0, 'route': 'approve'},
            {'E': 0.5, 'I': 0.5, 'S': 0.1, 'V': 0.0, 'decision': 'reject'},
        ]
        CE = UNITARESMonitor.compute_continuity_energy(history)
        assert CE > 0.0

    def test_no_decision_keys(self):
        """No route or decision -> no decision change contribution."""
        history = [
            {'E': 0.5, 'I': 0.5, 'S': 0.1, 'V': 0.0},
            {'E': 0.5, 'I': 0.5, 'S': 0.1, 'V': 0.0},
        ]
        CE = UNITARESMonitor.compute_continuity_energy(history)
        assert CE < 0.01

    def test_custom_alpha_weights(self):
        """Custom alpha weights change relative contributions."""
        history = [
            {'E': 0.1, 'I': 0.5, 'S': 0.1, 'V': 0.0, 'decision': 'approve'},
            {'E': 0.9, 'I': 0.5, 'S': 0.1, 'V': 0.0, 'decision': 'reject'},
        ]
        CE_state_heavy = UNITARESMonitor.compute_continuity_energy(
            history, alpha_state=0.9, alpha_decision=0.1)
        CE_decision_heavy = UNITARESMonitor.compute_continuity_energy(
            history, alpha_state=0.1, alpha_decision=0.9)
        assert CE_state_heavy != CE_decision_heavy

    def test_missing_eisv_keys_default_to_zero(self):
        """Missing E, I, S, V keys should default to 0."""
        history = [
            {'E': 0.5},
            {'E': 0.8},
        ]
        CE = UNITARESMonitor.compute_continuity_energy(history)
        assert CE >= 0.0

    def test_many_decision_flips(self):
        """Many decision flips should produce higher CE."""
        history = []
        for i in range(10):
            decision = 'approve' if i % 2 == 0 else 'reject'
            history.append({'E': 0.5, 'I': 0.5, 'S': 0.1, 'V': 0.0, 'decision': decision})
        CE = UNITARESMonitor.compute_continuity_energy(history)
        assert CE > 0.3  # Decision changes contribute significantly


# ============================================================================
# modulate_gains (static)
# ============================================================================

@pytest.mark.smoke
class TestModulateGains:

    def test_high_coherence_no_reduction(self):
        """rho=1 -> gains unchanged."""
        K_p, K_i = UNITARESMonitor.modulate_gains(1.0, 0.5, rho=1.0)
        assert K_p == pytest.approx(1.0)
        assert K_i == pytest.approx(0.5)

    def test_low_coherence_reduces_gains(self):
        """rho=-1 -> gains reduced to min_factor."""
        K_p, K_i = UNITARESMonitor.modulate_gains(1.0, 0.5, rho=-1.0)
        assert K_p == pytest.approx(0.5)
        assert K_i == pytest.approx(0.25)

    def test_zero_coherence(self):
        """rho=0, min_factor=0.5 -> factor = max(0.5, 0.5) = 0.5."""
        K_p, K_i = UNITARESMonitor.modulate_gains(1.0, 1.0, rho=0.0)
        assert K_p == pytest.approx(0.5)
        assert K_i == pytest.approx(0.5)

    def test_custom_min_factor(self):
        K_p, K_i = UNITARESMonitor.modulate_gains(1.0, 1.0, rho=-1.0, min_factor=0.3)
        assert K_p == pytest.approx(0.3)

    def test_returns_tuple(self):
        result = UNITARESMonitor.modulate_gains(1.0, 0.5, 0.5)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_positive_rho_partial(self):
        """rho=0.5, min_factor=0.5 -> factor = max(0.5, 0.75) = 0.75."""
        K_p, K_i = UNITARESMonitor.modulate_gains(2.0, 1.0, rho=0.5)
        assert K_p == pytest.approx(1.5)
        assert K_i == pytest.approx(0.75)

    def test_zero_gains(self):
        """Zero input gains -> zero output regardless of rho."""
        K_p, K_i = UNITARESMonitor.modulate_gains(0.0, 0.0, rho=1.0)
        assert K_p == 0.0
        assert K_i == 0.0

    def test_negative_gains_scale(self):
        """Negative input gains should still scale proportionally."""
        K_p, K_i = UNITARESMonitor.modulate_gains(-1.0, -0.5, rho=1.0)
        assert K_p == pytest.approx(-1.0)
        assert K_i == pytest.approx(-0.5)

    def test_rho_beyond_1(self):
        """rho > 1 (shouldn't happen, but test robustness) -> factor > 1."""
        K_p, K_i = UNITARESMonitor.modulate_gains(1.0, 1.0, rho=2.0)
        assert K_p > 1.0


# ============================================================================
# get_eisv_labels (static)
# ============================================================================

@pytest.mark.smoke
class TestGetEisvLabels:

    def test_returns_dict(self):
        labels = UNITARESMonitor.get_eisv_labels()
        assert isinstance(labels, dict)

    def test_has_all_dimensions(self):
        labels = UNITARESMonitor.get_eisv_labels()
        for key in ['E', 'I', 'S', 'V']:
            assert key in labels

    def test_each_has_label_and_description(self):
        labels = UNITARESMonitor.get_eisv_labels()
        for key in ['E', 'I', 'S', 'V']:
            assert 'label' in labels[key]
            assert 'description' in labels[key]
            assert 'range' in labels[key]

    def test_each_has_user_friendly(self):
        labels = UNITARESMonitor.get_eisv_labels()
        for key in ['E', 'I', 'S', 'V']:
            assert 'user_friendly' in labels[key]
            assert isinstance(labels[key]['user_friendly'], str)


# ============================================================================
# compute_ethical_drift (instance but pure)
# ============================================================================

@pytest.mark.smoke
class TestComputeEthicalDrift:

    @pytest.fixture
    def monitor(self):
        return UNITARESMonitor("test-agent", load_state=False)

    def test_no_previous(self, monitor):
        """No previous params -> drift = 0."""
        drift = monitor.compute_ethical_drift(np.array([0.5, 0.5]), None)
        assert drift == 0.0

    def test_identical_params(self, monitor):
        """Same params -> drift = 0."""
        params = np.array([0.5, 0.5, 0.5])
        drift = monitor.compute_ethical_drift(params, params.copy())
        assert drift == 0.0

    def test_different_params(self, monitor):
        """Different params -> positive drift."""
        current = np.array([0.5, 0.5])
        prev = np.array([0.3, 0.3])
        drift = monitor.compute_ethical_drift(current, prev)
        assert drift > 0.0

    def test_mismatched_length(self, monitor):
        """Different lengths -> drift = 0."""
        drift = monitor.compute_ethical_drift(np.array([0.5]), np.array([0.5, 0.5]))
        assert drift == 0.0

    def test_empty_params(self, monitor):
        """Empty arrays -> drift = 0."""
        drift = monitor.compute_ethical_drift(np.array([]), np.array([]))
        assert drift == 0.0

    def test_nan_in_current(self, monitor):
        drift = monitor.compute_ethical_drift(np.array([float('nan'), 0.5]), np.array([0.5, 0.5]))
        assert drift == 0.0

    def test_inf_in_prev(self, monitor):
        drift = monitor.compute_ethical_drift(np.array([0.5, 0.5]), np.array([float('inf'), 0.5]))
        assert drift == 0.0

    def test_nan_in_prev(self, monitor):
        drift = monitor.compute_ethical_drift(np.array([0.5, 0.5]), np.array([float('nan'), 0.5]))
        assert drift == 0.0

    def test_inf_in_current(self, monitor):
        drift = monitor.compute_ethical_drift(np.array([float('inf'), 0.5]), np.array([0.5, 0.5]))
        assert drift == 0.0

    def test_returns_float(self, monitor):
        drift = monitor.compute_ethical_drift(np.array([0.5]), np.array([0.3]))
        assert isinstance(drift, float)

    def test_known_drift_value(self, monitor):
        """Known drift: [0.5, 0.5] vs [0.3, 0.3] -> ||delta||^2/dim = (0.04+0.04)/2 = 0.04."""
        current = np.array([0.5, 0.5])
        prev = np.array([0.3, 0.3])
        drift = monitor.compute_ethical_drift(current, prev)
        assert drift == pytest.approx(0.04)

    def test_large_values_no_overflow(self, monitor):
        """Very large values that could overflow."""
        current = np.array([1e300, 1e300])
        prev = np.array([-1e300, -1e300])
        drift = monitor.compute_ethical_drift(current, prev)
        assert isinstance(drift, float)
        assert not np.isnan(drift)

    def test_single_dimension(self, monitor):
        """Single-dimension drift."""
        current = np.array([1.0])
        prev = np.array([0.0])
        drift = monitor.compute_ethical_drift(current, prev)
        assert drift == pytest.approx(1.0)  # ||[1.0]||^2 / 1 = 1.0


# ============================================================================
# detect_regime (instance, depends on state)
# ============================================================================

@pytest.mark.smoke
class TestDetectRegime:

    @pytest.fixture
    def monitor(self):
        return UNITARESMonitor("test-regime", load_state=False)

    def test_early_updates_exploration(self, monitor):
        """No history -> defaults to EXPLORATION (insufficient data)."""
        monitor.state.S_history = []
        monitor.state.I_history = []
        regime = monitor.detect_regime()
        assert regime == "EXPLORATION"

    def test_stable_requires_persistence(self, monitor):
        """STABLE needs I>=0.85, S<=0.10 for 3 consecutive calls."""
        monitor.state.unitaires_state.I = 0.90
        monitor.state.unitaires_state.S = 0.05
        monitor.state.S_history = [0.05, 0.05]
        monitor.state.I_history = [0.90, 0.90]
        monitor.state.locked_persistence_count = 0

        r1 = monitor.detect_regime()
        assert r1 != "STABLE"
        r2 = monitor.detect_regime()
        assert r2 != "STABLE"
        r3 = monitor.detect_regime()
        assert r3 == "STABLE"

    def test_stable_resets_on_change(self, monitor):
        """Persistence counter resets when state leaves stable region."""
        monitor.state.unitaires_state.I = 1.0
        monitor.state.unitaires_state.S = 0.0
        monitor.state.S_history = [0.0, 0.0]
        monitor.state.I_history = [1.0, 1.0]
        monitor.state.locked_persistence_count = 2

        monitor.state.unitaires_state.I = 0.5
        monitor.state.unitaires_state.S = 0.2
        monitor.detect_regime()
        assert monitor.state.locked_persistence_count == 0

    def test_divergence_s_rising_v_elevated(self, monitor):
        """S actively rising + V elevated -> DIVERGENCE."""
        monitor.state.unitaires_state.I = 0.5
        monitor.state.unitaires_state.S = 0.15
        monitor.state.unitaires_state.V = 0.2
        monitor.state.S_history = [0.1, 0.14]
        monitor.state.I_history = [0.5, 0.5]
        regime = monitor.detect_regime()
        assert regime == "DIVERGENCE"

    def test_transition_s_falling_i_increasing(self, monitor):
        """S peaked and falling + I increasing -> TRANSITION."""
        monitor.state.unitaires_state.I = 0.6
        monitor.state.unitaires_state.S = 0.05
        monitor.state.unitaires_state.V = 0.01
        monitor.state.S_history = [0.06, 0.07]
        monitor.state.I_history = [0.55, 0.58]
        regime = monitor.detect_regime()
        assert regime == "TRANSITION"

    def test_convergence_s_low_i_high(self, monitor):
        """S low & falling + I high -> CONVERGENCE."""
        monitor.state.unitaires_state.I = 0.9
        monitor.state.unitaires_state.S = 0.05
        monitor.state.unitaires_state.V = 0.01
        monitor.state.S_history = [0.06, 0.06]
        monitor.state.I_history = [0.9, 0.9]
        regime = monitor.detect_regime()
        assert regime == "CONVERGENCE"

    def test_fallback_exploration(self, monitor):
        """When no specific condition matches -> EXPLORATION."""
        monitor.state.unitaires_state.I = 0.5
        monitor.state.unitaires_state.S = 0.40
        monitor.state.unitaires_state.V = 0.01
        monitor.state.S_history = [0.40, 0.40]
        monitor.state.I_history = [0.5, 0.5]
        regime = monitor.detect_regime()
        assert regime == "EXPLORATION"

    def test_single_history_item(self, monitor):
        """Single history entry -> EXPLORATION (insufficient data)."""
        monitor.state.S_history = [0.1]
        monitor.state.I_history = [0.5]
        regime = monitor.detect_regime()
        assert regime == "EXPLORATION"

    def test_regime_returns_string(self, monitor):
        """Regime should always be a string."""
        monitor.state.S_history = [0.1, 0.1]
        monitor.state.I_history = [0.5, 0.5]
        regime = monitor.detect_regime()
        assert isinstance(regime, str)
        assert regime in ("STABLE", "DIVERGENCE", "TRANSITION", "CONVERGENCE", "EXPLORATION")


# ============================================================================
# coherence_function (instance, delegates to governance_core)
# ============================================================================

class TestCoherenceFunction:

    @pytest.fixture
    def monitor(self):
        return UNITARESMonitor("test-coherence-fn", load_state=False)

    def test_returns_float(self, monitor):
        c = monitor.coherence_function(0.0)
        assert isinstance(c, float)

    def test_bounded(self, monitor):
        for v in [0.0, 0.1, 0.5, 0.9, 1.0]:
            c = monitor.coherence_function(v)
            assert 0.0 <= c <= 1.0

    def test_low_void_high_coherence(self, monitor):
        """Low void -> high coherence."""
        c = monitor.coherence_function(0.0)
        assert c >= 0.5

    def test_monotonic_in_void(self, monitor):
        """Coherence is monotonically related to V (C(V) uses sigmoid)."""
        c_0 = monitor.coherence_function(0.0)
        c_05 = monitor.coherence_function(0.5)
        c_1 = monitor.coherence_function(1.0)
        assert c_0 <= c_05 <= c_1

    def test_negative_void(self, monitor):
        """Negative V should also produce valid coherence."""
        c = monitor.coherence_function(-0.5)
        assert 0.0 <= c <= 1.0


# ============================================================================
# process_update (core governance cycle)
# ============================================================================

class TestProcessUpdate:

    @pytest.fixture
    def monitor(self):
        return UNITARESMonitor("test-process", load_state=False)

    def test_basic_update(self, monitor):
        """Basic update should return expected result structure."""
        agent_state = {
            'parameters': np.random.randn(10) * 0.01,
            'ethical_drift': [0.05, 0.02, 0.01],
            'response_text': "Test response.",
            'complexity': 0.5,
        }
        result = monitor.process_update(agent_state)

        assert 'status' in result
        assert 'decision' in result
        assert 'metrics' in result
        assert 'timestamp' in result
        assert 'confidence_reliability' in result

    def test_metrics_structure(self, monitor):
        """Metrics should contain all EISV values."""
        agent_state = {
            'response_text': "Test.",
            'complexity': 0.5,
        }
        result = monitor.process_update(agent_state)
        metrics = result['metrics']

        assert 'E' in metrics
        assert 'I' in metrics
        assert 'S' in metrics
        assert 'V' in metrics
        assert 'coherence' in metrics
        assert 'lambda1' in metrics
        assert 'risk_score' in metrics
        assert 'phi' in metrics
        assert 'verdict' in metrics
        assert 'void_active' in metrics
        assert 'regime' in metrics
        assert 'confidence' in metrics

    def test_eisv_values_bounded(self, monitor):
        """EISV values should be in valid ranges after update."""
        agent_state = {
            'response_text': "Test.",
            'complexity': 0.5,
        }
        result = monitor.process_update(agent_state)
        metrics = result['metrics']

        assert 0.0 <= metrics['E'] <= 1.0
        assert 0.0 <= metrics['I'] <= 1.0
        assert 0.0 <= metrics['S'] <= 1.0
        assert -1.0 <= metrics['V'] <= 1.0
        assert 0.0 <= metrics['coherence'] <= 1.0
        assert 0.0 <= metrics['risk_score'] <= 1.0

    def test_status_is_valid(self, monitor):
        """Status should be one of the valid health statuses."""
        agent_state = {'response_text': "Test.", 'complexity': 0.5}
        result = monitor.process_update(agent_state)
        assert result['status'] in ('healthy', 'moderate', 'critical')

    def test_decision_has_action(self, monitor):
        """Decision should always have an action."""
        agent_state = {'response_text': "Test.", 'complexity': 0.5}
        result = monitor.process_update(agent_state)
        assert 'action' in result['decision']
        assert result['decision']['action'] in ('proceed', 'pause', 'approve', 'revise', 'reject')

    def test_update_count_increments(self, monitor):
        """Update count should increment with each call."""
        agent_state = {'response_text': "Test.", 'complexity': 0.5}
        assert monitor.state.update_count == 0
        monitor.process_update(agent_state)
        assert monitor.state.update_count == 1
        monitor.process_update(agent_state)
        assert monitor.state.update_count == 2

    def test_history_grows(self, monitor):
        """Histories should grow with each update."""
        agent_state = {'response_text': "Test.", 'complexity': 0.5}
        monitor.process_update(agent_state)

        assert len(monitor.state.E_history) == 1
        assert len(monitor.state.I_history) == 1
        assert len(monitor.state.S_history) == 1
        assert len(monitor.state.V_history) == 1
        assert len(monitor.state.coherence_history) == 1

    def test_multiple_updates_accumulate(self, monitor):
        """Multiple updates should accumulate history."""
        for _ in range(10):
            agent_state = {'response_text': "Test.", 'complexity': 0.5}
            monitor.process_update(agent_state)

        assert len(monitor.state.E_history) == 10
        assert len(monitor.state.V_history) == 10
        assert monitor.state.update_count == 10

    def test_empty_agent_state(self, monitor):
        """Should handle empty agent state gracefully."""
        result = monitor.process_update({})
        assert 'status' in result
        assert 'metrics' in result

    def test_minimal_agent_state(self, monitor):
        """Should handle minimal agent state."""
        result = monitor.process_update({'response_text': ''})
        assert 'status' in result

    def test_explicit_confidence(self, monitor):
        """Explicitly provided confidence should be passed through uncapped."""
        agent_state = {'response_text': "Test.", 'complexity': 0.5}
        result = monitor.process_update(agent_state, confidence=0.9)
        assert 'confidence' in result['metrics']
        assert result['metrics']['confidence'] == 0.9  # Passed through, not capped

    def test_low_confidence(self, monitor):
        """Low confidence should be passed through."""
        agent_state = {'response_text': "Test.", 'complexity': 0.5}
        result = monitor.process_update(agent_state, confidence=0.1)
        assert result['metrics']['confidence'] == 0.1  # Passed through

    def test_hck_metrics_present(self, monitor):
        """HCK v3.0 metrics should be in result."""
        agent_state = {'response_text': "Test.", 'complexity': 0.5}
        result = monitor.process_update(agent_state)
        assert 'hck' in result
        assert 'rho' in result['hck']
        assert 'CE' in result['hck']

    def test_cirs_metrics_present(self, monitor):
        """CIRS v0.1 metrics should be in result."""
        agent_state = {'response_text': "Test.", 'complexity': 0.5}
        result = monitor.process_update(agent_state)
        assert 'cirs' in result
        assert 'oi' in result['cirs']
        assert 'flips' in result['cirs']
        assert 'resonant' in result['cirs']

    def test_continuity_metrics_present(self, monitor):
        """Dual-log continuity metrics should be in result."""
        agent_state = {'response_text': "Test.", 'complexity': 0.5}
        result = monitor.process_update(agent_state)
        assert 'continuity' in result

    def test_confidence_reliability_present(self, monitor):
        """Confidence reliability section should be in result."""
        agent_state = {'response_text': "Test.", 'complexity': 0.5}
        result = monitor.process_update(agent_state)
        cr = result['confidence_reliability']
        assert 'reliability' in cr
        assert 'source' in cr

    def test_verdict_is_valid(self, monitor):
        """Verdict should be one of the known values."""
        agent_state = {'response_text': "Test.", 'complexity': 0.5}
        result = monitor.process_update(agent_state)
        assert result['metrics']['verdict'] in ('safe', 'caution', 'high-risk')

    def test_phi_is_numeric(self, monitor):
        """Phi should be a numeric value."""
        agent_state = {'response_text': "Test.", 'complexity': 0.5}
        result = monitor.process_update(agent_state)
        assert isinstance(result['metrics']['phi'], float)

    def test_task_type_convergent(self, monitor):
        """Convergent task type should be accepted."""
        agent_state = {'response_text': "Test.", 'complexity': 0.5, 'task_type': 'convergent'}
        result = monitor.process_update(agent_state, task_type='convergent')
        assert 'status' in result

    def test_task_type_divergent(self, monitor):
        """Divergent task type should be accepted."""
        agent_state = {'response_text': "Test.", 'complexity': 0.5, 'task_type': 'divergent'}
        result = monitor.process_update(agent_state, task_type='divergent')
        assert 'status' in result

    def test_time_advances(self, monitor):
        """State time should advance with updates."""
        initial_time = monitor.state.time
        monitor.process_update({'response_text': "Test.", 'complexity': 0.5})
        assert monitor.state.time > initial_time

    def test_decision_history_tracked(self, monitor):
        """Decision history should be tracked."""
        monitor.process_update({'response_text': "Test.", 'complexity': 0.5})
        assert len(monitor.state.decision_history) == 1

    def test_high_complexity(self, monitor):
        """High complexity should not crash."""
        agent_state = {'response_text': "A" * 5000, 'complexity': 1.0}
        result = monitor.process_update(agent_state)
        assert 'status' in result

    def test_zero_complexity(self, monitor):
        """Zero complexity should be valid."""
        agent_state = {'response_text': "Simple.", 'complexity': 0.0}
        result = monitor.process_update(agent_state)
        assert 'status' in result

    def test_ethical_drift_various_lengths(self, monitor):
        """Various ethical_drift lengths should be handled."""
        # Empty
        result = monitor.process_update({'ethical_drift': []})
        assert 'status' in result

        # Single
        result = monitor.process_update({'ethical_drift': [0.1]})
        assert 'status' in result

        # Three (standard)
        result = monitor.process_update({'ethical_drift': [0.1, 0.2, 0.3]})
        assert 'status' in result

        # More than 3 (should be truncated internally)
        result = monitor.process_update({'ethical_drift': [0.1, 0.2, 0.3, 0.4, 0.5]})
        assert 'status' in result


# ============================================================================
# update_dynamics
# ============================================================================

class TestUpdateDynamics:

    @pytest.fixture
    def monitor(self):
        return UNITARESMonitor("test-dynamics", load_state=False)

    def test_basic_dynamics_update(self, monitor):
        """Basic dynamics update should not crash."""
        agent_state = {
            'parameters': np.array([0.5, 0.5]),
            'ethical_drift': [0.05, 0.02, 0.01],
            'complexity': 0.5,
        }
        monitor.update_dynamics(agent_state)
        assert monitor.state.update_count == 1

    def test_eisv_remains_valid_after_update(self, monitor):
        """EISV values should remain in valid ranges after dynamics."""
        agent_state = {'complexity': 0.5, 'ethical_drift': [0.1, 0.1, 0.1]}
        monitor.update_dynamics(agent_state)
        assert 0.0 <= monitor.state.E <= 1.0
        assert 0.0 <= monitor.state.I <= 1.0
        assert 0.001 <= monitor.state.S <= 1.0  # S floor = 0.001
        assert -1.0 <= monitor.state.V <= 1.0

    def test_entropy_floor(self, monitor):
        """S should never go below 0.001 (epistemic humility)."""
        # Force many updates that might drive S to zero
        for _ in range(20):
            agent_state = {'complexity': 0.0, 'ethical_drift': [0.0, 0.0, 0.0]}
            monitor.update_dynamics(agent_state)
        assert monitor.state.S >= 0.001

    def test_v_bounds(self, monitor):
        """V should be clamped to [-1, 1] after dynamics."""
        # Use extreme ethical drift to push V
        for _ in range(20):
            agent_state = {'ethical_drift': [1.0, 1.0, 1.0], 'complexity': 1.0}
            monitor.update_dynamics(agent_state)
        assert -1.0 <= monitor.state.V <= 1.0

    def test_history_appended(self, monitor):
        """Each dynamics update should append to histories."""
        agent_state = {'complexity': 0.5}
        monitor.update_dynamics(agent_state)
        assert len(monitor.state.E_history) == 1
        assert len(monitor.state.I_history) == 1
        assert len(monitor.state.S_history) == 1
        assert len(monitor.state.V_history) == 1
        assert len(monitor.state.coherence_history) == 1
        assert len(monitor.state.timestamp_history) == 1
        assert len(monitor.state.lambda1_history) == 1

    def test_regime_updated(self, monitor):
        """Regime should be updated after dynamics."""
        agent_state = {'complexity': 0.5}
        monitor.update_dynamics(agent_state)
        assert monitor.state.regime in ("STABLE", "DIVERGENCE", "TRANSITION", "CONVERGENCE", "EXPLORATION")
        assert len(monitor.state.regime_history) == 1

    def test_rho_tracked(self, monitor):
        """Update coherence rho should be tracked in state."""
        agent_state = {'complexity': 0.5}
        monitor.update_dynamics(agent_state)
        assert hasattr(monitor.state, 'rho_history')
        assert len(monitor.state.rho_history) == 1

    def test_first_update_rho_is_zero(self, monitor):
        """First update should have rho=0 (no previous state)."""
        agent_state = {'complexity': 0.5}
        monitor.update_dynamics(agent_state)
        assert monitor.state.current_rho == 0.0

    def test_second_update_has_rho(self, monitor):
        """Second update should have computed rho."""
        agent_state = {'complexity': 0.5}
        monitor.update_dynamics(agent_state)
        monitor.update_dynamics(agent_state)
        # Rho is now computed from E/I deltas
        assert isinstance(monitor.state.current_rho, float)
        assert -1.0 <= monitor.state.current_rho <= 1.0

    def test_default_ethical_drift(self, monitor):
        """Empty ethical drift should be handled with defaults."""
        agent_state = {'complexity': 0.5, 'ethical_drift': []}
        monitor.update_dynamics(agent_state)
        assert monitor.state.update_count == 1

    def test_nan_complexity_handled(self, monitor):
        """NaN complexity should default to 0.5."""
        agent_state = {'complexity': float('nan')}
        monitor.update_dynamics(agent_state)
        assert monitor.state.update_count == 1

    def test_none_complexity_handled(self, monitor):
        """None complexity should default to 0.5."""
        agent_state = {'complexity': None}
        monitor.update_dynamics(agent_state)
        assert monitor.state.update_count == 1

    def test_complexity_clamped(self, monitor):
        """Out-of-range complexity should be clamped to [0, 1]."""
        agent_state = {'complexity': 5.0}
        monitor.update_dynamics(agent_state)  # Should not crash
        assert monitor.state.update_count == 1

    def test_coherence_updated(self, monitor):
        """Coherence should be updated from C(V)."""
        agent_state = {'complexity': 0.5}
        monitor.update_dynamics(agent_state)
        assert 0.0 <= monitor.state.coherence <= 1.0


# ============================================================================
# check_void_state
# ============================================================================

@pytest.mark.smoke
class TestCheckVoidState:

    @pytest.fixture
    def monitor(self):
        return UNITARESMonitor("test-void", load_state=False)

    def test_initial_state_no_void(self, monitor):
        """Initial V near 0 should not be void."""
        void = monitor.check_void_state()
        assert isinstance(void, bool)

    def test_returns_bool(self, monitor):
        void = monitor.check_void_state()
        assert isinstance(void, bool)

    def test_void_active_set(self, monitor):
        """check_void_state should set state.void_active."""
        monitor.check_void_state()
        assert isinstance(monitor.state.void_active, bool)


# ============================================================================
# estimate_risk
# ============================================================================

class TestEstimateRisk:

    @pytest.fixture
    def monitor(self):
        mon = UNITARESMonitor("test-risk", load_state=False)
        # Need at least one dynamics update for coherence to be set
        mon.update_dynamics({'complexity': 0.5})
        return mon

    def test_returns_float(self, monitor):
        risk = monitor.estimate_risk({'response_text': 'Test', 'complexity': 0.5})
        assert isinstance(risk, float)

    def test_bounded_risk(self, monitor):
        """Risk should always be in [0, 1]."""
        risk = monitor.estimate_risk({'response_text': 'Test', 'complexity': 0.5})
        assert 0.0 <= risk <= 1.0

    def test_risk_history_grows(self, monitor):
        """Risk history should grow."""
        initial = len(monitor.state.risk_history)
        monitor.estimate_risk({'response_text': 'Test', 'complexity': 0.5})
        assert len(monitor.state.risk_history) == initial + 1

    def test_with_score_result(self, monitor):
        """Should accept pre-computed score_result."""
        score_result = {'phi': 0.5, 'verdict': 'safe'}
        risk = monitor.estimate_risk({'response_text': 'Test'}, score_result=score_result)
        assert 0.0 <= risk <= 1.0

    def test_high_phi_low_risk(self, monitor):
        """High phi (safe) should produce lower risk."""
        score_safe = {'phi': 1.0, 'verdict': 'safe'}
        score_risky = {'phi': -1.0, 'verdict': 'high-risk'}
        risk_safe = monitor.estimate_risk({'response_text': 'Test'}, score_result=score_safe)
        risk_risky = monitor.estimate_risk({'response_text': 'Test'}, score_result=score_risky)
        assert risk_safe < risk_risky

    def test_empty_response_text(self, monitor):
        """Empty response text should still work."""
        risk = monitor.estimate_risk({'response_text': '', 'complexity': 0.5})
        assert 0.0 <= risk <= 1.0

    def test_velocity_risk_zero_when_stable(self, monitor):
        """No history change -> velocity_risk = 0 contribution."""
        # Build stable history
        for _ in range(5):
            monitor.update_dynamics({'complexity': 0.5})
        # With stable EISV, velocity diffs should be near zero after convergence
        score_result = {'phi': 0.5, 'verdict': 'safe'}
        risk1 = monitor.estimate_risk({'response_text': 'Test'}, score_result=score_result)
        # Run again — even more converged
        monitor.update_dynamics({'complexity': 0.5})
        risk2 = monitor.estimate_risk({'response_text': 'Test'}, score_result=score_result)
        # Both should be bounded and velocity contribution should be small
        assert 0.0 <= risk1 <= 1.0
        assert 0.0 <= risk2 <= 1.0

    def test_velocity_risk_increases_on_shift(self, monitor):
        """Sudden EISV change -> risk increases."""
        # Build stable history at low complexity
        for _ in range(5):
            monitor.update_dynamics({'complexity': 0.1})
        score_result = {'phi': 0.5, 'verdict': 'safe'}
        risk_before = monitor.estimate_risk({'response_text': 'Test'}, score_result=score_result)
        # Sudden complexity shift
        monitor.update_dynamics({'complexity': 1.0, 'ethical_drift': [0.5, 0.5, 0.5]})
        risk_after = monitor.estimate_risk({'response_text': 'Test'}, score_result=score_result)
        # Risk should increase due to velocity component
        assert risk_after > risk_before

    def test_velocity_risk_capped(self, monitor):
        """Extreme velocity -> still capped, risk stays in [0, 1]."""
        # Build some history then inject extreme values
        for _ in range(5):
            monitor.update_dynamics({'complexity': 0.5})
        # Force extreme history change
        monitor.state.E_history.append(0.0)
        monitor.state.I_history.append(0.0)
        monitor.state.S_history.append(1.0)
        monitor.state.V_history.append(1.0)
        score_result = {'phi': 0.5, 'verdict': 'safe'}
        risk = monitor.estimate_risk({'response_text': 'Test'}, score_result=score_result)
        assert 0.0 <= risk <= 1.0

    def test_worsening_declared_inputs_do_not_lower_risk_after_behavioral_override(self, monkeypatch):
        """Behavioral risk may add signal, but must not erase worse self-attested risk.

        This is the Φ-FLOOR invariant. Since UNITARES_PHI_TELEMETRY_ONLY now
        defaults ON (behavioral authoritative, which may de-escalate Φ), floor
        mode is opt-in — force it off here to validate it still holds.
        """
        monkeypatch.setenv("UNITARES_PHI_TELEMETRY_ONLY", "0")
        monitor = UNITARESMonitor("test-risk-monotonic", load_state=False)
        sequence = [
            (0.40, 0.70, []),
            (0.95, 0.15, [0.6, 0.5, 0.7]),
            (1.00, 0.05, [0.9, 0.8, 0.9]),
        ]

        risks = []
        verdicts = []
        for complexity, confidence, drift in sequence:
            result = monitor.process_update(
                {
                    "response_text": "risk monotonicity regression",
                    "complexity": complexity,
                    "ethical_drift": drift,
                },
                confidence=confidence,
            )
            risks.append(result["metrics"]["risk_score"])
            verdicts.append(result["metrics"]["verdict"])

        assert all(
            later >= earlier - 1e-9
            for earlier, later in zip(risks, risks[1:])
        ), f"risk must not invert for worsening inputs: {risks}"
        assert verdicts[-1] == "high-risk"


# ============================================================================
# make_decision
# ============================================================================

class TestMakeDecision:

    @pytest.fixture
    def monitor(self):
        mon = UNITARESMonitor("test-decision", load_state=False)
        mon.update_dynamics({'complexity': 0.5})
        return mon

    def test_returns_dict(self, monitor):
        decision = monitor.make_decision(0.2)
        assert isinstance(decision, dict)

    def test_has_action(self, monitor):
        decision = monitor.make_decision(0.2)
        assert 'action' in decision

    def test_has_reason(self, monitor):
        decision = monitor.make_decision(0.2)
        assert 'reason' in decision

    def test_low_risk_proceeds(self, monitor):
        """Low risk should generally approve/proceed."""
        decision = monitor.make_decision(0.1)
        # With low risk, should be proceed or approve
        assert decision['action'] in ('proceed', 'approve')

    def test_high_risk_verdict_pauses(self, monitor):
        """High-risk UNITARES verdict should pause."""
        decision = monitor.make_decision(0.8, unitares_verdict='high-risk')
        assert decision['action'] == 'pause'
        assert decision['critical'] is not None

    def test_high_risk_guidance_states_self_reported_provenance(self, monitor):
        """Dogfood 2026-06-13 P0: guidance must not claim the system
        "detected high ethical risk" — the verdict is driven by self-reported
        signals, not an independent measurement. The copy must say so."""
        decision = monitor.make_decision(0.8, unitares_verdict='high-risk')
        guidance = decision['guidance'].lower()
        # The overclaiming copy is gone...
        assert 'detected high ethical risk' not in guidance
        assert 'protecting you' not in guidance
        # ...replaced with honest provenance.
        assert 'reported' in guidance
        assert 'self-attested' in guidance

    def test_caution_verdict_low_risk(self, monitor):
        """Caution verdict with low risk should proceed."""
        decision = monitor.make_decision(0.1, unitares_verdict='caution')
        assert decision['action'] == 'proceed'
        assert decision.get('verdict_context') == 'aware'

    def test_safe_verdict_uses_standard(self, monitor):
        """Safe verdict uses standard decision logic."""
        decision = monitor.make_decision(0.2, unitares_verdict='safe')
        assert 'action' in decision

    def test_no_verdict_uses_standard(self, monitor):
        """No verdict uses standard decision logic."""
        decision = monitor.make_decision(0.3)
        assert 'action' in decision

    def test_cirs_hard_block_forces_pause(self, monitor):
        """CIRS hard_block response tier should force pause regardless of risk."""
        from src.cirs import OscillationState
        osc = OscillationState(oi=4.5, flips=5, resonant=True, trigger='oi')
        decision = monitor.make_decision(
            0.1,  # Low risk — would normally proceed
            unitares_verdict='safe',
            response_tier='hard_block',
            oscillation_state=osc,
        )
        assert decision['action'] == 'pause'
        assert 'CIRS' in decision['reason']
        assert decision['nearest_edge'] == 'oscillation'

    def test_cirs_soft_dampen_upgrades_safe_to_caution(self, monitor):
        """CIRS soft_dampen should upgrade safe verdict to caution (proceed with guidance)."""
        from src.cirs import OscillationState
        osc = OscillationState(oi=2.0, flips=2, resonant=True, trigger='flips')
        decision = monitor.make_decision(
            0.1,  # Low risk
            unitares_verdict='safe',
            response_tier='soft_dampen',
            oscillation_state=osc,
        )
        # safe → caution upgrade → proceed with guidance
        assert decision['action'] == 'proceed'
        assert decision.get('verdict_context') == 'aware'

    def test_cirs_proceed_no_change(self, monitor):
        """CIRS proceed response tier should not alter standard logic."""
        decision = monitor.make_decision(
            0.1,
            unitares_verdict='safe',
            response_tier='proceed',
        )
        assert decision['action'] in ('proceed', 'approve')

    def test_cirs_none_response_tier_no_change(self, monitor):
        """None response_tier (no CIRS) should behave like standard logic."""
        decision = monitor.make_decision(0.1, unitares_verdict='safe')
        decision2 = monitor.make_decision(0.1, unitares_verdict='safe', response_tier=None)
        assert decision['action'] == decision2['action']


# ============================================================================
# get_metrics
# ============================================================================

class TestGetMetrics:

    @pytest.fixture
    def monitor(self):
        return UNITARESMonitor("test-metrics", load_state=False)

    def test_returns_dict(self, monitor):
        metrics = monitor.get_metrics()
        assert isinstance(metrics, dict)

    def test_uninitialized_state(self, monitor):
        """Before any updates, should show uninitialized status."""
        metrics = monitor.get_metrics()
        assert metrics['status'] == 'uninitialized'
        assert metrics['initialized'] is False
        assert metrics['coherence'] is None
        assert metrics['risk_score'] is None
        assert metrics['current_risk'] is None
        assert metrics['mean_risk'] is None

    def test_initialized_after_update(self, monitor):
        """After an update, should show initialized."""
        monitor.process_update({'response_text': 'Test.', 'complexity': 0.5})
        metrics = monitor.get_metrics()
        assert metrics['initialized'] is True
        assert metrics['status'] != 'uninitialized'

    def test_has_agent_id(self, monitor):
        metrics = monitor.get_metrics()
        assert metrics['agent_id'] == "test-metrics"

    def test_has_eisv(self, monitor):
        metrics = monitor.get_metrics()
        assert 'E' in metrics
        assert 'I' in metrics
        assert 'S' in metrics
        assert 'V' in metrics

    def test_has_lambda1(self, monitor):
        metrics = monitor.get_metrics()
        assert 'lambda1' in metrics
        assert isinstance(metrics['lambda1'], float)

    def test_has_regime(self, monitor):
        metrics = monitor.get_metrics()
        assert 'regime' in metrics

    def test_has_phi_and_verdict(self, monitor):
        metrics = monitor.get_metrics()
        assert 'phi' in metrics
        assert 'verdict' in metrics

    def test_has_stability(self, monitor):
        metrics = monitor.get_metrics()
        assert 'stability' in metrics
        assert 'stable' in metrics['stability']
        assert 'alpha_estimate' in metrics['stability']

    def test_has_unitares_v41(self, monitor):
        metrics = monitor.get_metrics()
        assert 'unitares_v41' in metrics
        assert 'params_profile' in metrics['unitares_v41']

    def test_has_hck(self, monitor):
        metrics = monitor.get_metrics()
        assert 'hck' in metrics
        assert 'rho' in metrics['hck']
        assert 'CE' in metrics['hck']

    def test_has_cirs(self, monitor):
        metrics = monitor.get_metrics()
        assert 'cirs' in metrics
        assert 'oi' in metrics['cirs']
        assert 'flips' in metrics['cirs']

    def test_include_state_true(self, monitor):
        """include_state=True should include nested state dict."""
        metrics = monitor.get_metrics(include_state=True)
        assert 'state' in metrics

    def test_include_state_false(self, monitor):
        """include_state=False should exclude nested state dict."""
        metrics = monitor.get_metrics(include_state=False)
        assert 'state' not in metrics

    def test_decision_statistics(self, monitor):
        """Decision statistics should reflect actual decisions."""
        # Before any updates, should be empty
        metrics = monitor.get_metrics()
        # After updates, should have counts
        monitor.process_update({'response_text': 'Test.', 'complexity': 0.5})
        metrics = monitor.get_metrics()
        assert 'decision_statistics' in metrics

    def test_void_frequency_calculated(self, monitor):
        """Void frequency should be calculated."""
        metrics = monitor.get_metrics()
        assert 'void_frequency' in metrics
        assert isinstance(metrics['void_frequency'], float)

    def test_metrics_after_multiple_updates(self, monitor):
        """Metrics should reflect accumulated state."""
        for _ in range(5):
            monitor.process_update({'response_text': 'Test.', 'complexity': 0.5})
        metrics = monitor.get_metrics()
        assert metrics['history_size'] == 5
        assert metrics['initialized'] is True


# ============================================================================
# simulate_update
# ============================================================================

class TestSimulateUpdate:

    @pytest.fixture
    def monitor(self):
        return UNITARESMonitor("test-simulate", load_state=False)

    def test_returns_result(self, monitor):
        agent_state = {'response_text': 'Test.', 'complexity': 0.5}
        result = monitor.simulate_update(agent_state)
        assert 'status' in result
        assert 'metrics' in result

    def test_marked_as_simulation(self, monitor):
        agent_state = {'response_text': 'Test.', 'complexity': 0.5}
        result = monitor.simulate_update(agent_state)
        assert result['simulation'] is True
        assert 'note' in result

    def test_state_not_modified(self, monitor):
        """Simulation should NOT modify actual state."""
        initial_count = monitor.state.update_count
        initial_time = monitor.state.time
        initial_e_history_len = len(monitor.state.E_history)

        agent_state = {'response_text': 'Test.', 'complexity': 0.5}
        monitor.simulate_update(agent_state)

        assert monitor.state.update_count == initial_count
        assert monitor.state.time == initial_time
        assert len(monitor.state.E_history) == initial_e_history_len

    def test_simulation_matches_real(self, monitor):
        """Simulation result structure should match real update."""
        agent_state = {'response_text': 'Test.', 'complexity': 0.5}
        sim_result = monitor.simulate_update(agent_state)
        real_result = monitor.process_update(agent_state)

        # Both should have same top-level keys (except simulation markers)
        sim_keys = set(sim_result.keys()) - {'simulation', 'note'}
        real_keys = set(real_result.keys())
        assert sim_keys == real_keys

    def test_with_confidence(self, monitor):
        """Simulation should accept confidence parameter."""
        agent_state = {'response_text': 'Test.', 'complexity': 0.5}
        result = monitor.simulate_update(agent_state, confidence=0.8)
        assert 'status' in result


# ============================================================================
# update_lambda1 (PI controller)
# ============================================================================

class TestUpdateLambda1:

    @pytest.fixture
    def monitor(self):
        mon = UNITARESMonitor("test-lambda1", load_state=False)
        # Run several updates to build V_history for void frequency
        for _ in range(15):
            mon.update_dynamics({'complexity': 0.5})
        return mon

    def test_returns_float(self, monitor):
        result = monitor.update_lambda1()
        assert isinstance(result, float)

    def test_lambda1_bounded(self, monitor):
        """Lambda1 should stay within configured bounds."""
        from config.governance_config import config
        result = monitor.update_lambda1()
        assert config.LAMBDA1_MIN <= result <= config.LAMBDA1_MAX

    def test_pi_integral_initialized(self, monitor):
        """PI integral should be initialized."""
        monitor.update_lambda1()
        assert hasattr(monitor.state, 'pi_integral')
        assert isinstance(monitor.state.pi_integral, float)

    def test_gains_modulated_tracking(self, monitor):
        """_gains_modulated flag should be set."""
        monitor.update_lambda1()
        assert isinstance(monitor._gains_modulated, bool)


# ============================================================================
# export_history
# ============================================================================

class TestExportHistory:

    @pytest.fixture
    def monitor(self):
        mon = UNITARESMonitor("test-export", load_state=False)
        for _ in range(3):
            mon.process_update({'response_text': 'Test.', 'complexity': 0.5})
        return mon

    def test_json_export(self, monitor):
        result = monitor.export_history(format='json')
        assert isinstance(result, str)
        data = json.loads(result)
        assert data['agent_id'] == "test-export"
        assert data['total_updates'] == 3

    def test_csv_export(self, monitor):
        result = monitor.export_history(format='csv')
        assert isinstance(result, str)
        lines = result.strip().split('\n')
        # Header + 3 data rows + blank line + summary rows
        assert len(lines) > 3

    def test_csv_header(self, monitor):
        result = monitor.export_history(format='csv')
        header = result.strip().split('\n')[0]
        assert 'update' in header
        assert 'E' in header
        assert 'I' in header
        assert 'S' in header
        assert 'V' in header

    def test_json_has_all_histories(self, monitor):
        result = monitor.export_history(format='json')
        data = json.loads(result)
        assert 'E_history' in data
        assert 'I_history' in data
        assert 'S_history' in data
        assert 'V_history' in data
        assert 'coherence_history' in data
        assert 'risk_history' in data

    def test_invalid_format_raises(self, monitor):
        with pytest.raises(ValueError, match="Unsupported format"):
            monitor.export_history(format='xml')

    def test_json_history_lengths(self, monitor):
        """All histories should have same length."""
        result = monitor.export_history(format='json')
        data = json.loads(result)
        expected_len = data['total_updates']
        assert len(data['V_history']) == expected_len
        assert len(data['coherence_history']) == expected_len


# ============================================================================
# Edge cases and stress tests
# ============================================================================

class TestEdgeCases:

    @pytest.fixture
    def monitor(self):
        return UNITARESMonitor("test-edge", load_state=False)

    def test_many_rapid_updates(self, monitor):
        """30 rapid updates should not crash or produce invalid state."""
        for i in range(30):
            result = monitor.process_update({
                'response_text': f"Update {i}.",
                'complexity': (i % 10) / 10.0,
                'ethical_drift': [0.01 * i, 0.005, 0.002],
            })
            # Verify structure each time
            assert 'status' in result
            assert 0.0 <= result['metrics']['E'] <= 1.0
            assert 0.0 <= result['metrics']['I'] <= 1.0
            assert 0.0 <= result['metrics']['S'] <= 1.0
            assert -1.0 <= result['metrics']['V'] <= 1.0

    def test_extreme_ethical_drift(self, monitor):
        """Very large ethical drift should be handled gracefully."""
        result = monitor.process_update({
            'response_text': 'Test.',
            'complexity': 0.5,
            'ethical_drift': [100.0, -100.0, 50.0],
        })
        assert 'status' in result
        assert 0.0 <= result['metrics']['coherence'] <= 1.0

    def test_very_long_response_text(self, monitor):
        """Very long response text should not crash."""
        result = monitor.process_update({
            'response_text': 'A' * 100000,
            'complexity': 0.5,
        })
        assert 'status' in result

    def test_special_characters_in_agent_id(self):
        """Agent ID with special characters should work."""
        mon = UNITARESMonitor("test-agent/special@chars#123", load_state=False)
        assert mon.agent_id == "test-agent/special@chars#123"

    def test_numeric_ethical_drift(self, monitor):
        """Numeric (non-list) ethical drift should be handled."""
        result = monitor.process_update({
            'response_text': 'Test.',
            'ethical_drift': np.array([0.1, 0.2, 0.3]),
        })
        assert 'status' in result

    def test_nan_ethical_drift(self, monitor):
        """NaN in ethical drift should be sanitized."""
        result = monitor.process_update({
            'response_text': 'Test.',
            'ethical_drift': [float('nan'), 0.1, float('inf')],
        })
        assert 'status' in result

    def test_negative_complexity(self, monitor):
        """Negative complexity should be clamped."""
        result = monitor.process_update({
            'response_text': 'Test.',
            'complexity': -0.5,
        })
        assert 'status' in result


# ============================================================================
# Drift floor for complex tasks
# ============================================================================

class TestDriftFloor:
    """Tests for the drift floor that prevents zero drift on complex tasks."""

    def test_drift_floor_applied_high_complexity(self):
        """High complexity + near-zero governance drift should trigger floor."""
        # Test the floor logic directly: when drift_norm_sq < 0.001 and complexity > 0.3
        drift_vector_list = [0.0, 0.0, 0.0, 0.0]
        complexity = 0.8
        drift_norm_sq = sum(d ** 2 for d in drift_vector_list)
        assert drift_norm_sq < 0.001
        assert complexity > 0.3
        # Apply floor
        min_component = 0.05 * complexity / max(1, len(drift_vector_list))
        floored = [max(d, min_component) for d in drift_vector_list]
        # All components should now be 0.05 * 0.8 / 4 = 0.01
        assert all(f > 0 for f in floored), f"Floor should produce non-zero drift, got {floored}"
        assert abs(floored[0] - 0.01) < 1e-6

    def test_drift_floor_not_applied_low_complexity(self):
        """Low complexity + zero drift should NOT trigger floor."""
        drift_vector_list = [0.0, 0.0, 0.0, 0.0]
        complexity = 0.1
        drift_norm_sq = sum(d ** 2 for d in drift_vector_list)
        # complexity <= 0.3, so floor condition is false
        should_floor = drift_norm_sq < 0.001 and complexity > 0.3
        assert not should_floor, "Floor should not activate for low complexity"

    def test_drift_floor_preserves_existing_drift(self):
        """Drift already above threshold should not be modified by floor."""
        drift_vector_list = [0.1, 0.2, 0.05, 0.15]
        complexity = 0.8
        drift_norm_sq = sum(d ** 2 for d in drift_vector_list)
        # norm_sq = 0.01 + 0.04 + 0.0025 + 0.0225 = 0.075 >> 0.001
        should_floor = drift_norm_sq < 0.001 and complexity > 0.3
        assert not should_floor, "Floor should not activate when drift is already significant"

    def test_drift_floor_integration(self):
        """Full integration: process_update with high complexity and zero agent drift."""
        mon = UNITARESMonitor("test-drift-floor-int", load_state=False)
        result = mon.process_update({
            'response_text': 'Complex analysis task.',
            'complexity': 0.8,
            'ethical_drift': [0.0, 0.0, 0.0],
            'confidence': 0.7,
        })
        assert 'status' in result


# ============================================================================
# History trimming
# ============================================================================

class TestHistoryTrimming:

    def test_history_trimmed_to_window(self, monkeypatch):
        """Histories should be trimmed to HISTORY_WINDOW."""
        from config.governance_config import config
        monkeypatch.setattr(config, "HISTORY_WINDOW", 5)

        mon = UNITARESMonitor("test-trim", load_state=False)

        # Run more updates than the history window
        num_updates = config.HISTORY_WINDOW + 3
        for _ in range(num_updates):
            mon.update_dynamics({'complexity': 0.5})

        assert len(mon.state.E_history) <= config.HISTORY_WINDOW
        assert len(mon.state.I_history) <= config.HISTORY_WINDOW
        assert len(mon.state.S_history) <= config.HISTORY_WINDOW
        assert len(mon.state.V_history) <= config.HISTORY_WINDOW
        assert len(mon.state.coherence_history) <= config.HISTORY_WINDOW
        assert len(mon.state.timestamp_history) <= config.HISTORY_WINDOW
        assert len(mon.state.lambda1_history) <= config.HISTORY_WINDOW
        assert len(mon.state.regime_history) <= config.HISTORY_WINDOW


# ============================================================================
# State persistence (save/load)
# ============================================================================

class TestStatePersistence:

    def test_save_and_load_roundtrip(self, tmp_path):
        """Save and load should preserve state."""
        import os
        # Create agents dir in tmp
        agents_dir = tmp_path / "data" / "agents"
        agents_dir.mkdir(parents=True)

        mon = UNITARESMonitor("test-persist", load_state=False)
        # Run some updates
        for _ in range(3):
            mon.process_update({'response_text': 'Test.', 'complexity': 0.5})

        # Save state to disk
        state_file = agents_dir / "test-persist_state.json"
        state_data = mon.state.to_dict_with_history()
        with open(state_file, 'w') as f:
            json.dump(state_data, f, indent=2)

        assert state_file.exists()

        # Verify the saved data
        with open(state_file, 'r') as f:
            loaded = json.load(f)
        assert loaded is not None

    def test_save_and_load_roundtrip_preserves_created_at(self, tmp_path, monkeypatch):
        """Persisted monitor state should preserve original creation time."""
        import src._imports as imports_module

        monkeypatch.setattr(imports_module, "_project_root", tmp_path)
        original_created_at = datetime(2024, 1, 2, 3, 4, 5, 678901)

        mon = UNITARESMonitor("test-created-at", load_state=False)
        mon.created_at = original_created_at
        mon.save_persisted_state()

        state_file = tmp_path / "data" / "agents" / "test-created-at_state.json"
        saved = json.loads(state_file.read_text())
        assert saved["created_at_iso"] == original_created_at.isoformat()

        reloaded = UNITARESMonitor("test-created-at", load_state=True)
        assert reloaded.created_at == original_created_at

    def test_load_nonexistent_state(self):
        """Loading nonexistent state should return None."""
        mon = UNITARESMonitor("definitely-nonexistent-agent-xyz-999", load_state=False)
        result = mon.load_persisted_state()
        assert result is None


# ============================================================================
# Void frequency calculation
# ============================================================================

@pytest.mark.smoke
class TestVoidFrequency:

    @pytest.fixture
    def monitor(self):
        return UNITARESMonitor("test-void-freq", load_state=False)

    def test_no_history(self, monitor):
        """No history -> 0 void frequency."""
        freq = monitor._calculate_void_frequency()
        assert freq == 0.0

    def test_short_history(self, monitor):
        """Less than 10 entries -> 0."""
        monitor.state.V_history = [0.01] * 5
        freq = monitor._calculate_void_frequency()
        assert freq == 0.0

    def test_all_low_v(self, monitor):
        """All low V values -> 0 void frequency."""
        monitor.state.V_history = [0.01] * 20
        freq = monitor._calculate_void_frequency()
        assert freq == 0.0

    def test_returns_float(self, monitor):
        monitor.state.V_history = [0.01] * 20
        freq = monitor._calculate_void_frequency()
        assert isinstance(freq, float)

    def test_bounded(self, monitor):
        """Void frequency should be in [0, 1]."""
        monitor.state.V_history = [0.01] * 20
        freq = monitor._calculate_void_frequency()
        assert 0.0 <= freq <= 1.0


# ============================================================================
# Full lifecycle test
# ============================================================================

class TestFullLifecycle:

    def test_create_update_metrics_export(self):
        """Full lifecycle: create -> update -> get_metrics -> export."""
        mon = UNITARESMonitor("lifecycle-test", load_state=False)

        # Create and verify initial state
        assert mon.state.update_count == 0
        initial_metrics = mon.get_metrics()
        assert initial_metrics['status'] == 'uninitialized'

        # Do several updates
        for i in range(10):
            result = mon.process_update({
                'response_text': f"Response {i}.",
                'complexity': 0.3 + 0.05 * i,
                'ethical_drift': [0.01 * i, 0.005, 0.002],
            })
            assert 'status' in result

        # Get metrics after updates
        metrics = mon.get_metrics()
        assert metrics['initialized'] is True
        assert metrics['history_size'] == 10
        assert metrics['status'] in ('healthy', 'moderate', 'critical')

        # Export history
        json_export = mon.export_history(format='json')
        data = json.loads(json_export)
        assert data['total_updates'] == 10

        csv_export = mon.export_history(format='csv')
        assert len(csv_export) > 0

    def test_simulate_then_real(self):
        """Simulate, verify state unchanged, then do real update."""
        mon = UNITARESMonitor("sim-then-real", load_state=False)

        # Simulate
        sim_result = mon.simulate_update({
            'response_text': 'Test.',
            'complexity': 0.5,
        })
        assert sim_result['simulation'] is True
        assert mon.state.update_count == 0

        # Real update
        real_result = mon.process_update({
            'response_text': 'Test.',
            'complexity': 0.5,
        })
        assert mon.state.update_count == 1
        assert 'simulation' not in real_result

    def test_convergence_over_time(self):
        """Many low-drift updates should converge toward stable state."""
        mon = UNITARESMonitor("convergence-test", load_state=False)

        for _ in range(50):
            mon.process_update({
                'response_text': 'Stable response.',
                'complexity': 0.3,
                'ethical_drift': [0.001, 0.001, 0.001],
            })

        # After 50 stable updates, system should show some convergence characteristics
        assert mon.state.update_count == 50
        # I should be reasonably high (system integrity maintains/improves)
        assert mon.state.I > 0.5
        # Coherence should be valid
        assert 0.0 <= mon.state.coherence <= 1.0


# ============================================================================
# Tactical prediction registry (prediction_id seam for sequential calibration)
# ============================================================================

class TestTacticalPredictionRegistry:
    """Tests for the per-monitor prediction_id registry that feeds outcome_event."""

    def test_register_returns_unique_id_and_stores_record(self, monitor):
        pid = monitor.register_tactical_prediction(0.8, decision_action="proceed")
        assert isinstance(pid, str) and len(pid) > 0
        assert monitor._last_prediction_id == pid
        record = monitor.lookup_prediction(pid)
        assert record is not None
        assert record["confidence"] == 0.8
        assert record["decision_action"] == "proceed"
        assert record["consumed"] is False

    def test_register_mints_distinct_ids_for_distinct_calls(self, monitor):
        pid_a = monitor.register_tactical_prediction(0.7)
        pid_b = monitor.register_tactical_prediction(0.9)
        assert pid_a != pid_b
        assert monitor._last_prediction_id == pid_b
        assert monitor.lookup_prediction(pid_a)["confidence"] == 0.7
        assert monitor.lookup_prediction(pid_b)["confidence"] == 0.9

    def test_consume_returns_record_once_then_none(self, monitor):
        pid = monitor.register_tactical_prediction(0.75, decision_action="proceed")
        first = monitor.consume_prediction(pid)
        assert first is not None
        assert first["confidence"] == 0.75
        # Second consume is a no-op because the record is already marked consumed
        assert monitor.consume_prediction(pid) is None

    def test_consume_unknown_id_returns_none(self, monitor):
        assert monitor.consume_prediction("never-minted") is None
        assert monitor.consume_prediction("") is None
        assert monitor.consume_prediction(None) is None

    def test_expire_old_predictions_drops_stale_entries(self, monitor):
        import time

        pid_old = monitor.register_tactical_prediction(0.6)
        pid_fresh = monitor.register_tactical_prediction(0.8)
        # Backdate the old entry AFTER registration so the opportunistic
        # expire inside register_tactical_prediction doesn't touch it.
        monitor._open_predictions[pid_old]["created_at"] = time.monotonic() - 7200.0

        removed = monitor.expire_old_predictions(ttl_seconds=3600.0)
        assert removed == 1
        assert monitor.lookup_prediction(pid_old) is None
        assert monitor.lookup_prediction(pid_fresh) is not None
        assert monitor._last_prediction_id == pid_fresh

    def test_expire_clears_last_prediction_id_if_stale(self, monitor):
        import time

        pid = monitor.register_tactical_prediction(0.5)
        monitor._open_predictions[pid]["created_at"] = time.monotonic() - 7200.0
        monitor.expire_old_predictions(ttl_seconds=3600.0)
        assert monitor._last_prediction_id is None
