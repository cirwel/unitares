"""
Health Thresholds for Risk-Based Status Calculation

Defines health status based on risk scores and void state.
Provides consistent health assessment across the governance system.

The historical coherence bands remain as configuration/schema compatibility,
but the overloaded scalar is no longer converted into a health label or alert.
Its deployed producer is directional controller feedback, not symmetric health
evidence; future producers must earn their own validated interpretation.
"""

from enum import Enum
from dataclasses import dataclass
from typing import Tuple, Optional


class HealthStatus(Enum):
    HEALTHY = "healthy"
    MODERATE = "moderate"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


@dataclass
class HealthThresholds:
    """Define health status based on risk score and other metrics"""
    
    # Risk-based thresholds (recalibrated Mar 2026 for coding agent population)
    # Aligned with RISK_APPROVE_THRESHOLD (0.45) and RISK_REVISE_THRESHOLD (0.70)
    risk_healthy_max: float = 0.45    # < 45%: Healthy (aligned with RISK_APPROVE_THRESHOLD)
    risk_moderate_max: float = 0.70   # 45-70%: Moderate, 70%+: Critical
    
    # Legacy coherence-compatibility thresholds (retained for public config and
    # policy compatibility; deliberately not used for health classification).
    # Updated for pure thermodynamic C(V) signal (removed param_coherence blend)
    # Physics: V ∈ [-0.1, 0.1] → coherence ∈ [0.45, 0.55]
    # Mean V ≈ -0.016 → coherence ≈ 0.49 (conservative operation)
    coherence_uninitialized: float = 0.60  # Placeholder state (coherence=1.0 before first update)
    coherence_healthy_min: float = 0.52   # Legacy field name; not used for health
    coherence_moderate_min: float = 0.48  # Below mean but acceptable (V ≈ -0.02)
    
    # Coherence critical threshold (aligned with governance_monitor.py)
    coherence_critical_threshold: float = 0.40

    def get_health_status(
        self,
        risk_score: Optional[float] = None,
        coherence: Optional[float] = None,
        void_active: bool = False
    ) -> Tuple[HealthStatus, str]:
        """
        Determine health status from metrics.

        Priority:
        1. void_active -> CRITICAL
        2. risk_score -> HEALTHY/MODERATE/CRITICAL
        3. no risk score -> UNKNOWN

        ``coherence`` remains accepted so existing callers do not break. It is
        reported in the message when it is the only available scalar, but it
        cannot establish health because its producers have different roles.
        """
        # Void state always critical
        if void_active:
            return HealthStatus.CRITICAL, "Void state active - system instability detected"

        # Use attention_score (renamed from risk_score) if available
        if risk_score is not None:
            if risk_score < self.risk_healthy_max:
                return HealthStatus.HEALTHY, f"Low risk ({risk_score:.2%})"
            elif risk_score < self.risk_moderate_max:
                return HealthStatus.MODERATE, f"Typical risk ({risk_score:.2%}) - normal for development work"
            else:
                return HealthStatus.CRITICAL, f"High risk ({risk_score:.2%}) - consider simplifying approach"
        
        # Do not manufacture a health label from the overloaded compatibility
        # scalar. The live decision still exposes any configured policy gate;
        # this class answers the narrower question "what health evidence do we
        # have?" and therefore returns UNKNOWN without a risk measurement.
        if coherence is not None:
            return HealthStatus.UNKNOWN, (
                f"Health not assessed: coherence compatibility signal "
                f"({coherence:.2f}) is not health-rated"
            )

        return HealthStatus.UNKNOWN, "Health status unknown - risk measurement unavailable"
    
    def should_alert(self, risk_score: Optional[float] = None, coherence: Optional[float] = None) -> bool:
        """Determine if risk level warrants an alert"""
        if risk_score is not None:
            return risk_score >= self.risk_moderate_max
        # Coherence-only alerts repeated the same invalid health inference.
        # Decision/policy alerts remain separate and continue to expose their
        # configured compatibility gates.
        return False
