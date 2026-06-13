"""
CIRS v2: Adaptive Governor

Unified adaptive governance -- replaces OscillationDetector + ResonanceDamper +
classify_response with a single PID-inspired controller that owns threshold
management. Thresholds are living per-agent state, not config constants.

The D-term IS the damping. Oscillation produces large derivatives, which
produce large corrections. The system self-stabilizes.

Design: the original design note (docs/plans/2026-02-19-cirs-v2-adaptive-governor-
design.md) is not present in this repo. Production thresholds intentionally
deviate from the paper's Table 5 (see the `# Paper:` annotations on each
GovernorConfig constant below); reconciling those deltas — ratify as permanent
vs. recalibrate toward the paper — is tracked in unitares#661.

Update cycle (called from process_agent_update):
  1. Detect phase from EISV histories (calls governance_core.phase_aware.detect_phase)
  2. Set reference point based on phase (exploration vs integration refs from config)
  3. Compute error: e = ref - current
  4. PID: P = K_p * e, I = K_i * integral(e), D = K_d * d_factor * (e - prev_e)
     - Integral wind-up protection: clamped to [-integral_max, integral_max]
     - Zero-crossing reset: integral resets when error crosses zero
     - D-factor: modulated by phase (exploration=0.5, integration=1.0)
  5. Apply bounded adjustment: tau/beta clamped to [floor, ceiling]
  6. Update oscillation metrics (OI via incremental EMA, flips, resonance)
  7. Threshold decay when stable (OI < threshold and flips == 0)
  8. Store controller output for observability
  9. Make verdict and return result dict
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class GovernorConfig:
    """Configuration for AdaptiveGovernor.

    Static defaults that become initial values. Hard bounds cannot be
    overridden by adaptation.
    """

    # Default thresholds (starting point -- will adapt)
    # Paper Table 5: tau=0.40, beta=0.60. Production values raised for operational stability.
    tau_default: float = 0.44        # Paper: 0.40. Raised to sit just below typical coherence ~0.458
    beta_default: float = 0.70       # Paper: 0.60. Raised to reduce false high-risk verdicts

    # Hard safety bounds (cannot be overridden by adaptation)
    tau_floor: float = 0.25
    tau_ceiling: float = 0.75
    beta_floor: float = 0.20
    beta_ceiling: float = 0.80       # Paper: 0.70. Raised to match RISK_REJECT_THRESHOLD

    # PID gains
    K_p: float = 0.05               # Proportional -- gentle
    K_i: float = 0.005              # Integral -- very slow
    K_d: float = 0.10               # Derivative -- strongest (IS the damping)

    # Integral wind-up protection
    integral_max: float = 0.10

    # Phase reference points
    # Paper Table 5: exploration τ=0.35/β=0.55, integration τ=0.40/β=0.60
    exploration_tau_ref: float = 0.38   # Paper: 0.35
    exploration_beta_ref: float = 0.55  # Matches paper
    integration_tau_ref: float = 0.44   # Paper: 0.40
    integration_beta_ref: float = 0.70  # Paper: 0.60. Raised to match beta_default

    # Phase modulation of D-term
    exploration_d_factor: float = 0.5   # Gentler damping during exploration
    integration_d_factor: float = 1.0   # Full damping during integration

    # Threshold decay (return to defaults when stable)
    decay_rate: float = 0.01
    decay_oi_threshold: float = 0.5    # OI must be below this for decay

    # Oscillation detection (kept for observability)
    window: int = 10
    ema_lambda: float = 0.35
    oi_threshold: float = 2.5
    flip_threshold: int = 4

    # V damping adaptation
    delta_default: float = 0.25          # V damping (matches parameters.py)
    delta_floor: float = 0.15            # Min damping (more sensitive)
    delta_ceiling: float = 0.50          # Max damping (more stable)
    delta_ref_variance: float = 0.005    # Target V variance for coherence spread

    # Verdict thresholds (relative to adaptive tau/beta)
    # Paper specifies two-tier (proceed/pause). Offset=0 means safe and caution
    # collapse: C>=tau AND R<beta → safe, otherwise → high-risk. The paper
    # (Remark 5.1) removed the middle tier because it caused oscillation.
    beta_approve_offset: float = 0.0


@dataclass
class GovernorState:
    """Per-agent adaptive state. Mutable, updated each cycle."""

    # Adaptive thresholds
    tau: float = 0.44
    beta: float = 0.70
    phase: str = "integration"

    # PID accumulators
    error_integral_tau: float = 0.0
    error_integral_beta: float = 0.0
    prev_error_tau: float = 0.0
    prev_error_beta: float = 0.0

    # Oscillation tracking
    oi: float = 0.0
    flips: int = 0
    resonant: bool = False
    trigger: Optional[str] = None
    was_resonant: bool = False
    ema_coherence: float = 0.0
    ema_risk: float = 0.0
    history: List[Dict] = field(default_factory=list)

    # Per-agent V damping
    delta: float = 0.25
    error_integral_delta: float = 0.0
    prev_error_delta: float = 0.0
    v_variance_ema: float = 0.0

    # Controller output (for observability)
    last_p_tau: float = 0.0
    last_i_tau: float = 0.0
    last_d_tau: float = 0.0
    last_p_beta: float = 0.0
    last_i_beta: float = 0.0
    last_d_beta: float = 0.0

    def to_dict(self) -> Dict:
        """Serialize for persistence. Omits transient observability fields."""
        return {
            "tau": self.tau,
            "beta": self.beta,
            "phase": self.phase,
            "error_integral_tau": self.error_integral_tau,
            "error_integral_beta": self.error_integral_beta,
            "prev_error_tau": self.prev_error_tau,
            "prev_error_beta": self.prev_error_beta,
            "oi": self.oi,
            "flips": self.flips,
            "resonant": self.resonant,
            "was_resonant": self.was_resonant,
            "ema_coherence": self.ema_coherence,
            "ema_risk": self.ema_risk,
            "history": self.history[-10:] if self.history else [],
            "delta": self.delta,
            "error_integral_delta": self.error_integral_delta,
            "prev_error_delta": self.prev_error_delta,
            "v_variance_ema": self.v_variance_ema,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "GovernorState":
        """Restore from persisted dict. Unknown keys ignored for forward compat."""
        state = cls()
        for key in ("tau", "beta", "phase", "error_integral_tau", "error_integral_beta",
                     "prev_error_tau", "prev_error_beta", "oi", "ema_coherence", "ema_risk",
                     "delta", "error_integral_delta",
                     "prev_error_delta", "v_variance_ema"):
            if key in data:
                setattr(state, key, data[key])
        state.flips = int(data.get("flips", 0))
        state.resonant = bool(data.get("resonant", False))
        state.was_resonant = bool(data.get("was_resonant", False))
        state.history = list(data.get("history", []))
        return state


class Verdict:
    """Governance verdict constants.

    Note: string values match existing codebase conventions:
    - "high-risk" uses hyphen (matches governance_monitor.py, tool_schemas.py)
    - "hard_block" uses underscore (matches src/cirs.py response tiers)
    """

    SAFE = "safe"
    CAUTION = "caution"
    HIGH_RISK = "high-risk"
    HARD_BLOCK = "hard_block"


class AdaptiveGovernor:
    """
    CIRS v2 Adaptive Governor.

    Owns threshold management for a single agent. Thresholds start at config
    defaults and adapt using PID control toward phase-appropriate reference
    points. The D-term provides oscillation damping.
    """

    def __init__(self, config: Optional[GovernorConfig] = None):
        self.config = config or GovernorConfig()
        self.state = GovernorState(
            tau=self.config.tau_default,
            beta=self.config.beta_default,
        )

    def update(
        self,
        coherence: float,
        risk: float,
        verdict: str,
        E_history: List[float],
        I_history: List[float],
        S_history: List[float],
        complexity_history: List[float],
        V_history: Optional[List[float]] = None,
    ) -> Dict:
        """
        Core PID update cycle. Called once per process_agent_update.

        Args:
            coherence: Current thermodynamic coherence C(V).
            risk: Current risk score.
            verdict: Previous verdict string (used for oscillation tracking).
            E_history: Recent E values for phase detection.
            I_history: Recent I values for phase detection.
            S_history: Recent S values for phase detection.
            complexity_history: Recent complexity values for phase detection.
            V_history: Recent V values for delta adaptation.

        Returns:
            Dict with verdict and full state for observability.
        """
        from .phase_aware import detect_phase, Phase

        # 1. Detect phase from EISV trajectory
        self.state.phase = detect_phase(
            E_history, I_history, S_history, complexity_history
        )

        # 2. Set reference point based on phase
        if self.state.phase == Phase.EXPLORATION:
            tau_ref = self.config.exploration_tau_ref
            beta_ref = self.config.exploration_beta_ref
            d_factor = self.config.exploration_d_factor
        else:
            tau_ref = self.config.integration_tau_ref
            beta_ref = self.config.integration_beta_ref
            d_factor = self.config.integration_d_factor

        # 3. Compute error signals against live governance state, not the
        # controller's own thresholds. The references define the desired
        # operating point for coherence/risk in the current phase.
        e_tau = tau_ref - coherence
        e_beta = beta_ref - risk

        # 4a. PID update -- tau
        p_tau = self.config.K_p * e_tau

        # Reset integral on zero-crossing (error changed sign)
        if self.state.prev_error_tau * e_tau < 0:
            self.state.error_integral_tau = 0.0
        self.state.error_integral_tau = _clamp(
            self.state.error_integral_tau + e_tau,
            -self.config.integral_max,
            self.config.integral_max,
        )
        i_tau = self.config.K_i * self.state.error_integral_tau

        d_tau = self.config.K_d * d_factor * (e_tau - self.state.prev_error_tau)
        self.state.prev_error_tau = e_tau

        # 4b. PID update -- beta
        p_beta = self.config.K_p * e_beta

        if self.state.prev_error_beta * e_beta < 0:
            self.state.error_integral_beta = 0.0
        self.state.error_integral_beta = _clamp(
            self.state.error_integral_beta + e_beta,
            -self.config.integral_max,
            self.config.integral_max,
        )
        i_beta = self.config.K_i * self.state.error_integral_beta

        d_beta = self.config.K_d * d_factor * (e_beta - self.state.prev_error_beta)
        self.state.prev_error_beta = e_beta

        # 5. Apply bounded adjustment
        adjustment_tau = p_tau + i_tau + d_tau
        adjustment_beta = p_beta + i_beta + d_beta

        self.state.tau = _clamp(
            self.state.tau + adjustment_tau,
            self.config.tau_floor,
            self.config.tau_ceiling,
        )
        self.state.beta = _clamp(
            self.state.beta + adjustment_beta,
            self.config.beta_floor,
            self.config.beta_ceiling,
        )

        # 6. Update oscillation metrics (OI, flips, resonance)
        self._update_oscillation(coherence, risk, verdict)

        # 7. Threshold decay when stable
        if (
            abs(self.state.oi) < self.config.decay_oi_threshold
            and self.state.flips == 0
        ):
            self.state.tau += self.config.decay_rate * (
                self.config.tau_default - self.state.tau
            )
            self.state.beta += self.config.decay_rate * (
                self.config.beta_default - self.state.beta
            )
            # Re-clamp after decay
            self.state.tau = _clamp(
                self.state.tau, self.config.tau_floor, self.config.tau_ceiling
            )
            self.state.beta = _clamp(
                self.state.beta, self.config.beta_floor, self.config.beta_ceiling
            )

        # 8. Delta adaptation: tune V damping per-agent based on V variance
        if V_history and len(V_history) >= 5:
            recent_V = V_history[-10:]
            v_mean = sum(recent_V) / len(recent_V)
            v_var = sum((v - v_mean) ** 2 for v in recent_V) / len(recent_V)
            self.state.v_variance_ema = (
                0.3 * v_var + 0.7 * self.state.v_variance_ema
            )

            # Error: positive when variance is below target (need less damping)
            e_delta = self.config.delta_ref_variance - self.state.v_variance_ema

            # PID for delta (inverted: low variance -> reduce delta -> more sensitivity)
            p_delta = self.config.K_p * e_delta * (-1.0)

            if self.state.prev_error_delta * e_delta < 0:
                self.state.error_integral_delta = 0.0
            self.state.error_integral_delta = _clamp(
                self.state.error_integral_delta + e_delta,
                -self.config.integral_max,
                self.config.integral_max,
            )
            i_delta = self.config.K_i * self.state.error_integral_delta * (-1.0)

            d_delta = self.config.K_d * d_factor * (e_delta - self.state.prev_error_delta) * (-1.0)
            self.state.prev_error_delta = e_delta

            adjustment_delta = p_delta + i_delta + d_delta
            self.state.delta = _clamp(
                self.state.delta + adjustment_delta,
                self.config.delta_floor,
                self.config.delta_ceiling,
            )

            # Delta decay when stable (return to default)
            if (
                abs(self.state.oi) < self.config.decay_oi_threshold
                and self.state.flips == 0
            ):
                self.state.delta += self.config.decay_rate * (
                    self.config.delta_default - self.state.delta
                )
                self.state.delta = _clamp(
                    self.state.delta,
                    self.config.delta_floor,
                    self.config.delta_ceiling,
                )

        # 9. Store controller output for observability
        self.state.last_p_tau = p_tau
        self.state.last_i_tau = i_tau
        self.state.last_d_tau = d_tau
        self.state.last_p_beta = p_beta
        self.state.last_i_beta = i_beta
        self.state.last_d_beta = d_beta

        # 10. Make verdict using adaptive thresholds
        verdict_result = self.make_verdict(coherence, risk)

        return self._build_result(verdict_result)

    def make_verdict(self, coherence: float, risk: float) -> str:
        """Make governance verdict using ADAPTIVE thresholds.

        Priority order:
        1. Hard block: coherence < tau_floor OR risk > beta_ceiling
        2. Safe: coherence >= tau AND risk < (beta + beta_approve_offset)
        3. Caution: coherence >= tau AND risk < beta
        4. High-risk: otherwise

        Args:
            coherence: Current coherence value.
            risk: Current risk value.

        Returns:
            Verdict string constant.
        """
        # Hard block -- absolute safety boundaries
        if coherence < self.config.tau_floor:
            return Verdict.HARD_BLOCK
        if risk > self.config.beta_ceiling:
            return Verdict.HARD_BLOCK

        beta_approve = self.state.beta + self.config.beta_approve_offset

        # Use adaptive thresholds
        if coherence >= self.state.tau and risk < beta_approve:
            return Verdict.SAFE
        if coherence >= self.state.tau and risk < self.state.beta:
            return Verdict.CAUTION
        return Verdict.HIGH_RISK

    def _update_oscillation(
        self, coherence: float, risk: float, verdict: str
    ):
        """Update oscillation metrics (OI, flips) for observability.

        Uses incremental EMA on sign transitions and counts verdict flips
        within the rolling window.
        """
        # Track previous resonant state for transition detection
        self.state.was_resonant = self.state.resonant

        delta_coh = coherence - self.state.tau
        delta_risk = risk - self.state.beta

        self.state.history.append(
            {
                "verdict": verdict,
                "sign_coh": 1 if delta_coh >= 0 else -1,
                "sign_risk": 1 if delta_risk >= 0 else -1,
            }
        )

        # Trim to window size
        if len(self.state.history) > self.config.window:
            self.state.history.pop(0)

        # Incremental EMA on sign transitions
        if len(self.state.history) >= 2:
            coh_t = (
                self.state.history[-1]["sign_coh"]
                - self.state.history[-2]["sign_coh"]
            )
            risk_t = (
                self.state.history[-1]["sign_risk"]
                - self.state.history[-2]["sign_risk"]
            )
            self.state.ema_coherence = (
                self.config.ema_lambda * coh_t
                + (1 - self.config.ema_lambda) * self.state.ema_coherence
            )
            self.state.ema_risk = (
                self.config.ema_lambda * risk_t
                + (1 - self.config.ema_lambda) * self.state.ema_risk
            )

        self.state.oi = self.state.ema_coherence + self.state.ema_risk

        # Count verdict flips within window
        self.state.flips = sum(
            1
            for i in range(1, len(self.state.history))
            if self.state.history[i]["verdict"]
            != self.state.history[i - 1]["verdict"]
        )

        # Resonance detection
        self.state.resonant = False
        self.state.trigger = None
        if abs(self.state.oi) >= self.config.oi_threshold:
            self.state.resonant = True
            self.state.trigger = "oi"
        elif self.state.flips >= self.config.flip_threshold:
            self.state.resonant = True
            self.state.trigger = "flips"

    def _build_result(self, verdict: str) -> Dict:
        """Build observability result dict."""
        return {
            "verdict": verdict,
            "tau": self.state.tau,
            "beta": self.state.beta,
            "tau_default": self.config.tau_default,
            "beta_default": self.config.beta_default,
            "phase": self.state.phase,
            "controller": {
                "p_tau": self.state.last_p_tau,
                "i_tau": self.state.last_i_tau,
                "d_tau": self.state.last_d_tau,
                "p_beta": self.state.last_p_beta,
                "i_beta": self.state.last_i_beta,
                "d_beta": self.state.last_d_beta,
            },
            "oi": self.state.oi,
            "flips": self.state.flips,
            "resonant": self.state.resonant,
            "trigger": self.state.trigger,
            "response_tier": verdict,  # Backward compat key
            "delta": self.state.delta,
            "v_variance_ema": self.state.v_variance_ema,
        }


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp value to [lo, hi] range."""
    return max(lo, min(hi, value))
