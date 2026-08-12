"""
Tests for pure/near-pure functions in src/mcp_handlers/core.py.

Tests _assess_thermodynamic_significance with mock monitor and config.
"""

import pytest
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import List
from unittest.mock import patch, MagicMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.core import _assess_thermodynamic_significance


@dataclass
class MockState:
    """Minimal mock of GovernanceState for significance testing."""
    risk_history: List[float] = field(default_factory=list)
    coherence_history: List[float] = field(default_factory=list)
    V: float = 0.0


class MockMonitor:
    """Minimal mock of UNITARESMonitor."""
    def __init__(self, state=None):
        self.state = state or MockState()


@pytest.fixture
def mock_config():
    """Mock governance config with default significance thresholds."""
    config = MagicMock()
    config.RISK_SPIKE_THRESHOLD = 0.15
    config.COHERENCE_DROP_THRESHOLD = 0.10
    config.SIGNIFICANCE_VOID_THRESHOLD = 0.10
    config.SIGNIFICANCE_HISTORY_WINDOW = 5
    return config


class TestAssessThermodynamicSignificance:

    def _call(self, monitor, result, mock_config):
        with patch('config.governance_config.config', mock_config):
            return _assess_thermodynamic_significance(monitor, result)

    def test_no_monitor_not_significant(self, mock_config):
        result = self._call(None, {}, mock_config)
        assert result['is_significant'] is False
        assert 'No monitor available' in result['reasons']
        assert 'timestamp' in result

    def test_empty_result_not_significant(self, mock_config):
        monitor = MockMonitor()
        result = self._call(monitor, {}, mock_config)
        assert result['is_significant'] is False
        assert result['reasons'] == []

    def test_risk_spike_detected(self, mock_config):
        state = MockState(risk_history=[0.2, 0.2, 0.2, 0.6])
        monitor = MockMonitor(state)
        result = self._call(monitor, {}, mock_config)
        assert result['is_significant'] is True
        assert any('risk_spike' in r for r in result['reasons'])

    def test_no_risk_spike_small_delta(self, mock_config):
        state = MockState(risk_history=[0.2, 0.2, 0.25])
        monitor = MockMonitor(state)
        result = self._call(monitor, {}, mock_config)
        assert not any('risk_spike' in r for r in result['reasons'])

    def test_behavioral_consistency_drop_detected(self, mock_config):
        state = MockState(coherence_history=[0.8, 0.8, 0.8, 0.5])
        monitor = MockMonitor(state)
        result = self._call(
            monitor,
            {"metrics": {"coherence_role": "behavioral_update_consistency"}},
            mock_config,
        )
        assert result['is_significant'] is True
        assert any('coherence_drop' in r for r in result['reasons'])

    def test_legacy_control_feedback_drop_not_significant(self, mock_config):
        state = MockState(coherence_history=[0.8, 0.8, 0.8, 0.5])
        monitor = MockMonitor(state)
        result = self._call(
            monitor,
            {"metrics": {"coherence_role": "ode_control_feedback"}},
            mock_config,
        )
        assert result['is_significant'] is False
        assert not any('coherence_drop' in r for r in result['reasons'])

    def test_no_coherence_drop_small_delta(self, mock_config):
        state = MockState(coherence_history=[0.8, 0.8, 0.75])
        monitor = MockMonitor(state)
        result = self._call(monitor, {}, mock_config)
        assert not any('coherence_drop' in r for r in result['reasons'])

    def test_void_significant(self, mock_config):
        state = MockState(V=0.15)
        monitor = MockMonitor(state)
        result = self._call(monitor, {}, mock_config)
        assert result['is_significant'] is True
        assert any('void_significant' in r for r in result['reasons'])

    def test_void_below_threshold(self, mock_config):
        state = MockState(V=0.05)
        monitor = MockMonitor(state)
        result = self._call(monitor, {}, mock_config)
        assert not any('void_significant' in r for r in result['reasons'])

    def test_negative_void_significant(self, mock_config):
        """Negative V should also trigger (uses abs())"""
        state = MockState(V=-0.15)
        monitor = MockMonitor(state)
        result = self._call(monitor, {}, mock_config)
        assert result['is_significant'] is True
        assert any('void_significant' in r for r in result['reasons'])

    def test_circuit_breaker_triggered(self, mock_config):
        monitor = MockMonitor()
        result_input = {'circuit_breaker': {'triggered': True}}
        result = self._call(monitor, result_input, mock_config)
        assert result['is_significant'] is True
        assert 'circuit_breaker_triggered' in result['reasons']

    def test_circuit_breaker_not_triggered(self, mock_config):
        monitor = MockMonitor()
        result_input = {'circuit_breaker': {'triggered': False}}
        result = self._call(monitor, result_input, mock_config)
        assert 'circuit_breaker_triggered' not in result['reasons']

    def test_decision_pause(self, mock_config):
        monitor = MockMonitor()
        result_input = {'decision': {'action': 'pause'}}
        result = self._call(monitor, result_input, mock_config)
        assert result['is_significant'] is True
        assert 'decision_pause' in result['reasons']

    def test_decision_reject(self, mock_config):
        monitor = MockMonitor()
        result_input = {'decision': {'action': 'reject'}}
        result = self._call(monitor, result_input, mock_config)
        assert result['is_significant'] is True
        assert 'decision_reject' in result['reasons']

    def test_decision_proceed_not_significant(self, mock_config):
        monitor = MockMonitor()
        result_input = {'decision': {'action': 'proceed'}}
        result = self._call(monitor, result_input, mock_config)
        assert not any('decision_' in r for r in result['reasons'])

    def test_multiple_reasons(self, mock_config):
        """Multiple significance triggers at once."""
        state = MockState(
            risk_history=[0.2, 0.2, 0.2, 0.7],
            coherence_history=[0.8, 0.8, 0.8, 0.4],
            V=0.2,
        )
        monitor = MockMonitor(state)
        result_input = {'circuit_breaker': {'triggered': True}}
        result = self._call(monitor, result_input, mock_config)
        assert result['is_significant'] is True
        assert len(result['reasons']) >= 3

    def test_single_history_entry_no_crash(self, mock_config):
        """Single entry in history shouldn't crash."""
        state = MockState(risk_history=[0.5], coherence_history=[0.5])
        monitor = MockMonitor(state)
        result = self._call(monitor, {}, mock_config)
        assert isinstance(result, dict)

    def test_result_has_timestamp(self, mock_config):
        monitor = MockMonitor()
        result = self._call(monitor, {}, mock_config)
        assert 'timestamp' in result
        assert isinstance(result['timestamp'], str)
