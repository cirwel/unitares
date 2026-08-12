"""
Tests for src/health_thresholds.py - Health status calculation from risk/coherence.

All pure logic, no mocking needed.
"""

import pytest
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.health_thresholds import HealthStatus, HealthThresholds


class TestHealthStatus:

    def test_enum_values(self):
        assert HealthStatus.HEALTHY.value == "healthy"
        assert HealthStatus.MODERATE.value == "moderate"
        assert HealthStatus.CRITICAL.value == "critical"
        assert HealthStatus.UNKNOWN.value == "unknown"

    def test_all_members(self):
        assert set(HealthStatus) == {
            HealthStatus.HEALTHY,
            HealthStatus.MODERATE,
            HealthStatus.CRITICAL,
            HealthStatus.UNKNOWN,
        }


class TestHealthThresholdsDefaults:

    def test_default_risk_thresholds(self):
        ht = HealthThresholds()
        assert ht.risk_healthy_max == 0.45
        assert ht.risk_moderate_max == 0.70

    def test_default_coherence_thresholds(self):
        ht = HealthThresholds()
        assert ht.coherence_uninitialized == 0.60
        assert ht.coherence_healthy_min == 0.52
        assert ht.coherence_moderate_min == 0.48
        assert ht.coherence_critical_threshold == 0.40

    def test_custom_thresholds(self):
        ht = HealthThresholds(risk_healthy_max=0.20, risk_moderate_max=0.50)
        assert ht.risk_healthy_max == 0.20
        assert ht.risk_moderate_max == 0.50


class TestGetHealthStatus:
    """Test the priority-based health status decision tree."""

    def setup_method(self):
        self.ht = HealthThresholds()

    # --- Void state (highest priority) ---

    def test_void_active_always_critical(self):
        status, msg = self.ht.get_health_status(risk_score=0.1, coherence=0.9, void_active=True)
        assert status == HealthStatus.CRITICAL
        assert "Void" in msg

    def test_void_active_overrides_good_metrics(self):
        status, _ = self.ht.get_health_status(risk_score=0.0, coherence=1.0, void_active=True)
        assert status == HealthStatus.CRITICAL

    # --- Coherence is not health evidence ---

    def test_coherence_below_critical_threshold(self):
        status, msg = self.ht.get_health_status(coherence=0.35)
        assert status == HealthStatus.UNKNOWN
        assert "not health-rated" in msg

    def test_coherence_at_critical_threshold_not_critical(self):
        """Coherence exactly at threshold is NOT below it."""
        status, _ = self.ht.get_health_status(coherence=0.40)
        assert status == HealthStatus.UNKNOWN

    # --- Risk-based assessment ---

    def test_risk_healthy(self):
        status, msg = self.ht.get_health_status(risk_score=0.20)
        assert status == HealthStatus.HEALTHY
        assert "Low risk" in msg

    def test_risk_at_healthy_boundary(self):
        """Risk exactly at healthy max is NOT healthy."""
        status, _ = self.ht.get_health_status(risk_score=0.45)
        assert status == HealthStatus.MODERATE

    def test_risk_moderate(self):
        status, msg = self.ht.get_health_status(risk_score=0.45)
        assert status == HealthStatus.MODERATE
        assert "Typical risk" in msg

    def test_risk_at_moderate_boundary(self):
        """Risk exactly at moderate max is critical."""
        status, _ = self.ht.get_health_status(risk_score=0.70)
        assert status == HealthStatus.CRITICAL

    def test_risk_critical(self):
        status, msg = self.ht.get_health_status(risk_score=0.85)
        assert status == HealthStatus.CRITICAL
        assert "High risk" in msg

    def test_risk_zero(self):
        status, _ = self.ht.get_health_status(risk_score=0.0)
        assert status == HealthStatus.HEALTHY

    # --- Risk takes priority over coherence ---

    def test_risk_overrides_coherence(self):
        """When both risk and coherence provided, risk wins (after void/critical checks)."""
        status, _ = self.ht.get_health_status(risk_score=0.10, coherence=0.49)
        assert status == HealthStatus.HEALTHY  # Risk says healthy

    # --- Coherence-only calls remain compatible but unclassified ---

    def test_coherence_uninitialized(self):
        """A placeholder-looking scalar still cannot establish health."""
        status, msg = self.ht.get_health_status(coherence=1.0)
        assert status == HealthStatus.UNKNOWN
        assert "not health-rated" in msg

    def test_coherence_healthy(self):
        status, msg = self.ht.get_health_status(coherence=0.55)
        assert status == HealthStatus.UNKNOWN
        assert "not health-rated" in msg

    def test_coherence_moderate(self):
        status, msg = self.ht.get_health_status(coherence=0.49)
        assert status == HealthStatus.UNKNOWN
        assert "not health-rated" in msg

    def test_coherence_low(self):
        """Coherence below moderate_min but above critical."""
        status, msg = self.ht.get_health_status(coherence=0.42)
        assert status == HealthStatus.UNKNOWN
        assert "not health-rated" in msg

    # --- No metrics ---

    def test_no_metrics_unknown(self):
        status, msg = self.ht.get_health_status()
        assert status == HealthStatus.UNKNOWN
        assert "unknown" in msg.lower()


class TestShouldAlert:

    def setup_method(self):
        self.ht = HealthThresholds()

    def test_high_risk_alerts(self):
        assert self.ht.should_alert(risk_score=0.75) is True

    def test_low_risk_no_alert(self):
        assert self.ht.should_alert(risk_score=0.30) is False

    def test_risk_at_boundary_alerts(self):
        assert self.ht.should_alert(risk_score=0.70) is True

    def test_low_coherence_does_not_create_health_alert(self):
        assert self.ht.should_alert(coherence=0.45) is False

    def test_high_coherence_no_alert(self):
        assert self.ht.should_alert(coherence=0.55) is False

    def test_coherence_at_boundary_alerts(self):
        assert self.ht.should_alert(coherence=0.48) is False  # >= moderate_min

    def test_coherence_below_boundary_does_not_alert(self):
        assert self.ht.should_alert(coherence=0.47) is False

    def test_no_metrics_no_alert(self):
        assert self.ht.should_alert() is False

    def test_risk_takes_priority(self):
        """Risk is checked first; if present, coherence ignored."""
        assert self.ht.should_alert(risk_score=0.30, coherence=0.40) is False
