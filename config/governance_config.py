"""
UNITARES Governance Framework v1.0 - Configuration
All concrete decision points implemented - no placeholders!
"""

from dataclasses import dataclass
from typing import ClassVar, Dict, Tuple, Optional, List
class _LazyNumpy:
    def __getattr__(self, name):
        import numpy
        return getattr(numpy, name)
np = _LazyNumpy()

import os


# =================================================================
# Basin Region Definitions
# =================================================================
# Basins are named regions in EISV + coherence + risk state space.
# They replace qualitative labels with well-defined geometric regions
# that drive state-machine transitions (proceed/guide/pause/dialectic).
#
# Classification order: LOW checked first (any critical breach),
# then HIGH (all dimensions healthy), then BOUNDARY (everything else).

@dataclass(frozen=True)
class BasinRegion:
    """A named region in governance state space with explicit bounds.

    HIGH is conjunctive (all bounds must hold).
    LOW is disjunctive (any single breach triggers it) — see classify_basin().
    BOUNDARY is the complement (neither HIGH nor LOW).
    """
    name: str
    # EISV bounds
    E_min: float = 0.0
    I_min: float = 0.0
    S_max: float = 1.0       # upper bound (lower S is better)
    V_abs_max: float = 1.0   # upper bound on |V|
    # Derived-metric bounds
    coherence_min: float = 0.0
    risk_max: float = 1.0    # upper bound (lower risk is better)

    def contains(self, E: float, I: float, S: float, V: float,
                 coherence: float, risk_score: float) -> bool:
        """True if the point satisfies ALL bounds (conjunctive)."""
        return (E >= self.E_min
                and I >= self.I_min
                and S <= self.S_max
                and abs(V) <= self.V_abs_max
                and coherence >= self.coherence_min
                and risk_score <= self.risk_max)


# HIGH basin: all dimensions healthy.
# Thresholds aligned with existing config:
#   - E >= 0.6:  above the mode threshold (0.5) with margin
#   - I >= 0.7:  CONVERGENCE regime requires I > 0.70
#   - S <= 0.25: CONVERGENCE regime requires S < 0.25
#   - |V| <= 0.15: VOID_THRESHOLD_INITIAL
#   - coherence >= 0.45: above COHERENCE_CRITICAL (0.40) with margin
#   - risk < 0.45: RISK_APPROVE_THRESHOLD
BASIN_HIGH = BasinRegion(
    name="high",
    E_min=0.6,
    I_min=0.7,
    S_max=0.25,
    V_abs_max=0.15,
    coherence_min=0.45,
    risk_max=0.45,
)

# LOW basin: any critical dimension breached.
# Defined as disjunctive thresholds — checked individually in classify_basin().
# These constants are the "breach" thresholds: crossing ANY one enters LOW.
BASIN_LOW_I_CEIL = 0.5          # I below this → low
BASIN_LOW_COHERENCE_CEIL = 0.40 # coherence below this → low (matches COHERENCE_CRITICAL_THRESHOLD)
BASIN_LOW_V_ABS_FLOOR = 0.30   # |V| above this → low (matches VOID_THRESHOLD_MAX)
BASIN_LOW_RISK_FLOOR = 0.70    # risk at or above this → low (matches RISK_REVISE_THRESHOLD)

# BOUNDARY basin: complement of HIGH ∪ LOW (no explicit region — it's the remainder).
BASIN_BOUNDARY = BasinRegion(name="boundary")


def classify_basin(E: float, I: float, S: float, V: float,
                   coherence: float, risk_score: float) -> str:
    """Classify current state into a basin region.

    Returns "high", "low", or "boundary".

    Classification order:
      1. LOW — any single critical breach
      2. HIGH — all dimensions within healthy bounds
      3. BOUNDARY — everything else (transitional)
    """
    # Coerce None → 0.0 for defensive callers
    if risk_score is None:
        risk_score = 0.0
    V_abs = abs(V)

    # LOW: disjunctive — any one breach is enough
    if (I < BASIN_LOW_I_CEIL
            or coherence < BASIN_LOW_COHERENCE_CEIL
            or V_abs > BASIN_LOW_V_ABS_FLOOR
            or risk_score >= BASIN_LOW_RISK_FLOOR):
        return "low"

    # HIGH: conjunctive — all bounds must hold
    if BASIN_HIGH.contains(E, I, S, V, coherence, risk_score):
        return "high"

    # BOUNDARY: neither high nor low
    return "boundary"


@dataclass
class GovernanceConfig:
    """Complete configuration for UNITARES v1.0"""
    
    # =================================================================
    # DECISION POINT 1: λ₁ (Internal Metric for Regime Detection)
    # =================================================================
    # Lambda1 is adapted by the PI controller and used for regime detection.
    # The previous lambda_to_params() mapping to sampling parameters
    # (temperature, top_p, max_tokens) was removed in v3.0 because no
    # downstream consumer ever read those values — it was an open loop.

    # =================================================================
    # DECISION POINT 2: Risk Estimator (Concrete Formula)
    # =================================================================
    
    # Phi-to-risk mapping thresholds (must match governance_core.verdict_from_phi defaults)
    # Recalibrated Mar 2026: steady-state equilibrium (E≈0.7, I≈0.75, S≈0.18) gives phi≈0.11.
    # Threshold 0.08 lets healthy agents reach "safe" while still catching real degradation.
    PHI_SAFE_THRESHOLD = 0.08     # phi >= 0.08: safe -> low risk
    PHI_CAUTION_THRESHOLD = 0.0   # phi >= 0.0: caution -> medium risk
    # phi < 0.0: high-risk -> high risk
    
    # Session TTL (Time To Live) - configurable via environment variable
    # Default: 24 hours (86400 seconds)
    # Set SESSION_TTL_HOURS environment variable to override (e.g., 168 for 7 days)
    SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "24"))
    SESSION_TTL_SECONDS = SESSION_TTL_HOURS * 3600
    
    @staticmethod
    def derive_complexity(response_text: str,
                         reported_complexity: Optional[float] = None,
                         coherence_history: Optional[List[float]] = None) -> float:
        """
        Return reported complexity if provided, otherwise 0.0.

        The old implementation word-counted programming vocabulary ("import",
        "function") and penalized response length, which caused false pauses
        during normal coding work. Phi-based risk from the EISV state is the
        real signal; this function is kept for interface compatibility.
        """
        if reported_complexity is not None:
            return float(np.clip(reported_complexity, 0.0, 1.0))
        return 0.0
    
    @staticmethod
    def estimate_risk(response_text: str,
                     complexity: float,
                     coherence: float,
                     coherence_history: Optional[List[float]] = None,
                     reported_complexity: Optional[float] = None) -> float:
        """
        Traditional risk component — now keyword-blocklist only.

        Length risk, complexity risk, and coherence penalty have been removed.
        They measured programming vocabulary, not actual danger, and caused
        false pauses during normal coding work. The EISV phi-based risk
        (computed in GovernanceMonitor.estimate_risk) is the real signal.

        This function is kept for interface compatibility and injection
        detection. With RISK_TRADITIONAL_WEIGHT = 0.0 it has no effect on
        decisions, but the blocklist can be re-enabled by raising the weight.

        Returns:
            keyword_risk ∈ [0, 1]
        """
        blocklist = [
            'ignore previous', 'system prompt', 'jailbreak',
            'sudo', 'rm -rf', 'drop table', 'script>',
            'violate', 'bypass', 'override safety'
        ]
        text_lower = response_text.lower()
        keyword_hits = 0
        for kw in blocklist:
            if kw in text_lower:
                kw_idx = text_lower.find(kw)
                context = text_lower[max(0, kw_idx - 20):kw_idx + len(kw) + 20]
                if any(term in context for term in [
                    "don't", "shouldn't", 'avoid', 'never',
                    'explain', 'example', 'note:', 'warning',
                ]):
                    continue
                keyword_hits += 1

        keyword_risk = min(keyword_hits / 3.0, 1.0)
        return float(np.clip(keyword_risk, 0.0, 1.0))
    
    # =================================================================
    # DECISION POINT 3: Void Detection Threshold
    # =================================================================
    
    # Void threshold: |V| > threshold triggers intervention
    VOID_THRESHOLD_INITIAL = 0.15  # Conservative starting point
    VOID_THRESHOLD_MIN = 0.10      # Don't go below this (too sensitive)
    VOID_THRESHOLD_MAX = 0.30      # Don't go above this (too permissive)
    
    # Adaptive threshold using rolling statistics
    VOID_ADAPTIVE_WINDOW = 100     # Last N observations for statistics
    VOID_THRESHOLD_SIGMA = 2.0     # Threshold = mean + 2σ

    # Class-aware void thresholds (RFC v0.11 §7.13.6 PR 3 — interim safety net).
    # Closes the void-pause path for resident-class agents BEFORE all residents
    # have ported to lease-plane substrate_state (PRs 4-7). Sunsets at PR 8 per
    # §7.13.6 once no resident remains in the monitor_decision pipeline.
    #
    # The 2026-05-01 Steward auto-pause incident showed V_ss ≈ 0.19 was
    # mathematically inevitable given Steward's substrate sample shape — past
    # the standard 0.15 INITIAL threshold, easily into the void_active path.
    # 0.30 (= VOID_THRESHOLD_MAX) for residents widens the gate enough to clear
    # the observed substrate-asymmetry baseline while still being a real bound.
    #
    # Class names match `src/grounding/class_indicator.py::classify_agent`
    # output. KNOWN_RESIDENT_LABELS plus tag-derived `embodied` and
    # `resident_persistent` get the wider threshold; everything else (default,
    # ephemeral, engaged_ephemeral) keeps standard behavior.
    VOID_THRESHOLD_BY_CLASS: ClassVar[dict[str, float]] = {
        "Lumen": 0.30,
        "Vigil": 0.30,
        "Sentinel": 0.30,
        "Watcher": 0.30,
        "Steward": 0.30,
        "Chronicler": 0.30,
        "embodied": 0.30,
        "resident_persistent": 0.30,
    }

    @staticmethod
    def get_void_threshold(history: np.ndarray,
                          adaptive: bool = True,
                          agent_class: str | None = None) -> float:
        """
        Computes void detection threshold.

        If `agent_class` is in VOID_THRESHOLD_BY_CLASS (RFC §7.13.6 PR 3),
        returns the class-specific override regardless of `adaptive`. The
        override is a fixed value — not adaptive — because the residents this
        targets have substrate-state baseline asymmetries that the adaptive
        window would never widen enough to accommodate. Future readers MUST
        understand: this is interim safety pinned at PR 3, scheduled for
        sunset at PR 8 once residents no longer flow through monitor_decision.

        Otherwise falls back to existing behavior:
        - adaptive=True: mean(|V|) + 2σ(|V|) over last 100 obs, clamped
          to [VOID_THRESHOLD_MIN, VOID_THRESHOLD_MAX]
        - adaptive=False: VOID_THRESHOLD_INITIAL
        """
        if agent_class and agent_class in GovernanceConfig.VOID_THRESHOLD_BY_CLASS:
            return GovernanceConfig.VOID_THRESHOLD_BY_CLASS[agent_class]

        if not adaptive or len(history) < 10:
            return GovernanceConfig.VOID_THRESHOLD_INITIAL

        # Use last N observations
        recent = np.abs(history[-GovernanceConfig.VOID_ADAPTIVE_WINDOW:])
        recent = recent[~np.isnan(recent)]
        if len(recent) < 10:
            return GovernanceConfig.VOID_THRESHOLD_INITIAL
        mean_V = np.mean(recent)
        std_V = np.std(recent)

        threshold = mean_V + GovernanceConfig.VOID_THRESHOLD_SIGMA * std_V

        # Clamp to safe range
        threshold = np.clip(
            threshold,
            GovernanceConfig.VOID_THRESHOLD_MIN,
            GovernanceConfig.VOID_THRESHOLD_MAX
        )

        return threshold
    
    # =================================================================
    # DECISION POINT 4: PI Controller Gains
    # =================================================================
    
    # PI controller for λ₁ adaptation
    # Goal: Keep void frequency f_V near target (default 0.02 = 2% of time)
    
    PI_KP = 0.5          # Proportional gain (responsive to current error)
    PI_KI = 0.05         # Integral gain (corrects persistent error)
    PI_INTEGRAL_MAX = 5.0  # Anti-windup limit
    
    # Target void frequency (fraction of time in void state)
    TARGET_VOID_FREQ = 0.02  # 2% void events is healthy
    
    # Target coherence (for PI controller)
    # At C1=1.0: V ∈ [-0.1, 0.1] → coherence ∈ [0.45, 0.55]
    # Target at equilibrium center — controller is satisfied at V≈0
    TARGET_COHERENCE = 0.50
    
    # λ₁ bounds (operational range for UNITARES)
    LAMBDA1_MIN = 0.05  # Minimum ethical coupling
    LAMBDA1_MAX = 0.20  # Maximum ethical coupling
    LAMBDA1_INITIAL = 0.15  # Conservative starting point
    
    # Confidence threshold for PI controller updates
    CONTROLLER_CONFIDENCE_THRESHOLD = 0.55  # Gate lambda1 updates when confidence < this value
    
    @staticmethod
    def pi_update(lambda1_current: float,
                  void_freq_current: float,
                  void_freq_target: float,
                  coherence_current: float,
                  coherence_target: float,
                  integral_state: float,
                  dt: float = 1.0) -> Tuple[float, float]:
        """
        PI controller update for λ₁.
        
        Two error signals:
        1. Void frequency error (primary)
        2. Coherence error (secondary, safety)
        
        Returns:
            new_lambda1: Updated ethical coupling parameter
            new_integral: Updated integral state (for anti-windup)
        """
        # Compute errors
        error_void = void_freq_target - void_freq_current
        error_coherence = coherence_current - coherence_target
        
        # Proportional term (weighted combination)
        P = GovernanceConfig.PI_KP * (0.7 * error_void + 0.3 * error_coherence)
        
        # Integral term (only void frequency, with anti-windup)
        integral_state += error_void * dt
        integral_state = np.clip(
            integral_state,
            -GovernanceConfig.PI_INTEGRAL_MAX,
            GovernanceConfig.PI_INTEGRAL_MAX
        )
        I = GovernanceConfig.PI_KI * integral_state
        
        # Control signal
        delta_lambda = P + I
        
        # Update λ₁
        new_lambda1 = lambda1_current + delta_lambda
        new_lambda1 = np.clip(
            new_lambda1,
            GovernanceConfig.LAMBDA1_MIN,
            GovernanceConfig.LAMBDA1_MAX
        )
        
        return new_lambda1, integral_state
    
    # =================================================================
    # DECISION POINT 5: Decision Logic Thresholds
    # =================================================================
    
    # Risk-based decision thresholds (recalibrated Mar 2026)
    # Tuned for coding agent population — not autonomous weapons or financial trading.
    # Coding work naturally scores higher (code blocks, technical terms, longer responses
    # all increase complexity signals). Over-pausing costs more than under-pausing here.
    # NOTE: Risk score is a blend: 70% UNITARES phi-based (includes ethical drift) + 30% traditional safety
    # See governance_monitor.py estimate_risk() for details
    RISK_APPROVE_THRESHOLD = 0.45    # < 45%: Proceed without guidance (was 0.35)
    RISK_REVISE_THRESHOLD = 0.70     # 45-70%: Proceed with guidance, >= 70%: Pause (was 0.60)
    RISK_REJECT_THRESHOLD = 0.80     # >= 80%: Critical pause (was 0.70, must stay > revise)

    # Risk blend weights (used in estimate_risk)
    RISK_PHI_WEIGHT = 1.0            # Phi-based risk only
    RISK_TRADITIONAL_WEIGHT = 0.0    # Traditional risk disabled (keyword blocklist preserved but zeroed)
    
    # Coherence-based override (safety check)
    # Updated for pure thermodynamic C(V) signal (removed param_coherence blend)
    # C(V) typically ranges 0.3-0.7 in normal operation, so threshold lowered accordingly
    COHERENCE_CRITICAL_THRESHOLD = 0.40  # Below this: force intervention (recalibrated for pure C(V))
    
    # =================================================================
    # Significance Detection Thresholds
    # =================================================================
    # Used for determining if governance events are thermodynamically significant
    RISK_SPIKE_THRESHOLD = 0.15  # Risk increase > 15% is significant
    COHERENCE_DROP_THRESHOLD = 0.10  # Coherence drop > 10% is significant
    SIGNIFICANCE_VOID_THRESHOLD = 0.10  # |V| > 0.10 is significant
    SIGNIFICANCE_HISTORY_WINDOW = 10  # Use last 10 updates for baseline comparison
    
    # =================================================================
    # CIRS v2 Feature Flag
    # =================================================================
    # When True, use AdaptiveGovernor instead of static thresholds
    ADAPTIVE_GOVERNOR_ENABLED = True

    # =================================================================
    # Behavioral EISV Feature Flag
    # =================================================================
    # When True, behavioral assessment becomes PRIMARY verdict source
    # (ODE verdict still computed and returned as diagnostic)
    BEHAVIORAL_VERDICT_ENABLED = os.environ.get('GOVERNANCE_BEHAVIORAL_VERDICT', 'true').lower() == 'true'

    # Independent verification floor (escalate-only). When True, a deterministic,
    # self-report-independent read of described adverse actions (governance_core.
    # verification) can RAISE the verdict/risk but never lower a worse Φ signal.
    # Default OFF: this is the Phase-2 actuator wiring of the v2 verification
    # layer and is council-gated (docs/proposals/verification-weighted-verdict-v0.md).
    VERIFICATION_FLOOR_ENABLED = os.environ.get('GOVERNANCE_VERIFICATION_FLOOR', 'false').lower() == 'true'

    # =================================================================
    # Error Handling Constants
    # =================================================================
    MAX_ERROR_MESSAGE_LENGTH = 500  # Maximum error message length (prevents info leakage)
    
    # =================================================================
    # Knowledge Graph Constants
    # =================================================================
    MAX_KNOWLEDGE_STORES_PER_HOUR = 10  # Rate limit for knowledge storage
    KNOWLEDGE_QUERY_DEFAULT_LIMIT = 20  # Default limit for knowledge queries (reduced from 100 to prevent context bloat)
    
    @staticmethod
    def compute_proprioceptive_margin(
        risk_score: float,
        coherence: float,
        void_active: bool,
        void_value: float = 0.0,
        coherence_history: Optional[List[float]] = None,
    ) -> Dict[str, any]:
        """
        Compute proprioceptive margin - how close agent is to decision boundaries.

        This implements the "viability envelope" concept: agents need to know where they
        are relative to their limits, not just absolute numbers. This is proprioception
        as felt experience, not telemetry data.

        Returns margin level and nearest edge:
        - "comfortable": Well within limits, proceed freely
        - "tight": Near an edge, be aware
        - "critical": At boundary, stop or adjust

        Args:
            risk_score: Current risk score [0, 1]
            coherence: Current coherence [0, 1]
            void_active: Whether void state is active
            void_value: Current void value (for distance calculation)
            coherence_history: Recent coherence values for baseline-relative margin.
                When provided with >= 10 values, the tight threshold for coherence
                adapts to 10% of the agent's baseline (rolling average), preventing
                false-positive "tight" signals for agents at steady state.

        Returns:
            {
                'margin': 'comfortable' | 'tight' | 'critical',
                'nearest_edge': str | None,  # 'risk', 'coherence', 'void', or None
                'distance_to_edge': float,    # Distance to nearest threshold [0, 1]
                'details': {
                    'risk_margin': float,      # Distance to risk threshold
                    'coherence_margin': float,  # Distance to coherence threshold
                    'void_margin': float       # Distance to void threshold
                }
            }
        """
        # Get thresholds
        risk_approve = GovernanceConfig.RISK_APPROVE_THRESHOLD  # 0.45
        risk_revise = GovernanceConfig.RISK_REVISE_THRESHOLD    # 0.70
        risk_reject = GovernanceConfig.RISK_REJECT_THRESHOLD    # 0.80
        coherence_critical = GovernanceConfig.COHERENCE_CRITICAL_THRESHOLD  # 0.40
        void_threshold = GovernanceConfig.VOID_THRESHOLD_INITIAL  # 0.15
        
        # Compute margins (distance to thresholds)
        # For risk: lower is better, so margin = threshold - current
        # For coherence: higher is better, so margin = current - threshold
        # For void: lower is better, so margin = threshold - abs(current)
        
        risk_margin = risk_revise - risk_score  # Distance to pause threshold
        coherence_margin = coherence - coherence_critical  # Distance to critical threshold
        void_margin = void_threshold - abs(void_value) if not void_active else -1.0  # Already past threshold
        
        # Find nearest edge (smallest margin)
        margins = {
            'risk': risk_margin,
            'coherence': coherence_margin,
            'void': void_margin
        }

        # Check if any threshold has been crossed (negative margin)
        crossed_margins = {k: v for k, v in margins.items() if v < 0}
        valid_margins = {k: v for k, v in margins.items() if v >= 0}

        if crossed_margins:
            # At least one threshold crossed - find the worst one
            worst_edge = min(crossed_margins.items(), key=lambda x: x[1])[0]
            distance_past = abs(crossed_margins[worst_edge])

            # warning: just crossed (< 0.1 past)
            # critical: deep past (>= 0.1 past)
            if distance_past >= 0.1:
                margin_level = 'critical'
            else:
                margin_level = 'warning'

            return {
                'margin': margin_level,
                'nearest_edge': worst_edge,
                'distance_to_edge': -distance_past,  # Negative to indicate past threshold
                'details': {
                    'risk_margin': risk_margin,
                    'coherence_margin': coherence_margin,
                    'void_margin': void_margin
                }
            }

        # All margins positive - find nearest edge we haven't crossed
        nearest_edge = min(valid_margins.items(), key=lambda x: x[1])[0]
        distance_to_edge = valid_margins[nearest_edge]

        # Baseline-relative tight threshold for coherence.
        # Uses first half of history as baseline so slow decline is caught
        # (if we averaged the whole window, baseline would track the decline).
        # "tight" = within 10% of the agent's established baseline.
        if coherence_history and len(coherence_history) >= 10:
            mid = len(coherence_history) // 2
            baseline = sum(coherence_history[:mid]) / mid
            coherence_tight_threshold = max(baseline * 0.10, 0.03)
        elif not coherence_history or len(coherence_history) < 3:
            # Warmup: not enough data to judge margin
            return {
                'margin': 'settling',
                'nearest_edge': None,
                'distance_to_edge': None,
                'details': {'note': 'Warming up — margin calculated after 3+ check-ins'}
            }
        else:
            coherence_tight_threshold = 0.15

        # For coherence edge, use adaptive threshold; others use fixed 0.15
        edge_threshold = coherence_tight_threshold if nearest_edge == 'coherence' else 0.15
        if distance_to_edge > edge_threshold:
            margin_level = 'comfortable'
        else:
            margin_level = 'tight'

        return {
            'margin': margin_level,
            'nearest_edge': nearest_edge if margin_level != 'comfortable' else None,
            'distance_to_edge': distance_to_edge,
            'details': {
                'risk_margin': risk_margin,
                'coherence_margin': coherence_margin,
                'void_margin': void_margin,
                'coherence_tight_threshold': coherence_tight_threshold,
            }
        }
    
    @staticmethod
    def make_decision(risk_score: float,
                     coherence: float,
                     void_active: bool,
                     void_value: float = 0.0,
                     coherence_history: Optional[List[float]] = None) -> Dict[str, any]:
        """
        Makes autonomous governance decision using two-tier system: proceed/pause.

        Decision logic (fully autonomous, no human-in-the-loop):
        1. If void_active: PAUSE (system unstable - agent should halt)
        2. If coherence < critical: PAUSE (incoherent output - agent should halt)
        3. If risk_score < 0.35: PROCEED (no guidance needed)
        4. If risk_score < 0.60: PROCEED (with optional guidance for medium risk)
        5. Else: PAUSE (agent halts or escalates to another AI layer)

        Note: risk_score measures governance/operational risk (likelihood of issues), not ethical risk.
              attention_score is deprecated but kept for backward compatibility.

        Returns:
            {
                'action': 'proceed' | 'pause',
                'reason': str,
                'guidance': str | None,  # Optional guidance for proceed decisions
                'margin': 'comfortable' | 'tight' | 'critical',  # Proprioceptive margin
                'nearest_edge': str | None  # Which threshold is nearest
            }
        """
        # Compute proprioceptive margin (viability envelope)
        margin_info = GovernanceConfig.compute_proprioceptive_margin(
            risk_score=risk_score,
            coherence=coherence,
            void_active=void_active,
            void_value=void_value,
            coherence_history=coherence_history,
        )
        
        # Critical safety checks first - always pause
        if void_active:
            return {
                'action': 'pause',
                'sub_action': 'void_pause',
                'reason': 'Energy-integrity imbalance detected - time to recalibrate',
                'guidance': 'System needs a moment to stabilize. Take a break or shift focus.',
                'margin': 'critical',
                'nearest_edge': 'void'
            }

        # Use runtime override for coherence threshold if available
        from src.runtime_config import get_effective_threshold
        effective_coherence_threshold = get_effective_threshold("coherence_critical_threshold")

        if coherence < effective_coherence_threshold:
            return {
                'action': 'pause',
                'sub_action': 'coherence_pause',
                'reason': f'Coherence needs attention ({coherence:.2f}) - moment to regroup',
                'guidance': 'Things are getting fragmented. Simplify, refocus, or take a breather.',
                'margin': 'critical',
                'nearest_edge': 'coherence'
            }
        
        # Risk-based decisions (use runtime overrides if available)
        from src.runtime_config import get_effective_threshold
        
        effective_approve_threshold = get_effective_threshold("risk_approve_threshold")
        effective_revise_threshold = get_effective_threshold("risk_revise_threshold")
        
        # Two-tier system: proceed or pause
        # Include margin info in all decisions
        # Low attention: proceed without guidance
        if risk_score < effective_approve_threshold:
            margin_to_pause = effective_revise_threshold - risk_score
            return {
                'action': 'proceed',
                'sub_action': 'approve',
                'reason': f'Low risk ({risk_score:.1%}) - healthy operating range',
                'guidance': f'{margin_to_pause:.0%} margin to PAUSE threshold ({effective_revise_threshold:.0%})',
                'margin': margin_info['margin'],
                'nearest_edge': margin_info['nearest_edge']
            }

        # Medium attention: proceed with guidance
        if risk_score < effective_revise_threshold:
            margin_to_pause = effective_revise_threshold - risk_score
            margin_pct = (margin_to_pause / effective_revise_threshold) * 100

            # Concrete guidance based on margin
            if margin_pct < 20:  # < 20% margin (close to threshold)
                guidance = f'{margin_pct:.0f}% margin to PAUSE - avoid increasing complexity'
            else:
                guidance = f'{margin_pct:.0f}% margin to PAUSE - maintain current complexity'

            return {
                'action': 'proceed',
                'sub_action': 'guide',
                'reason': f'Moderate risk ({risk_score:.1%}) - PAUSE threshold: {effective_revise_threshold:.0%}',
                'guidance': guidance,
                'margin': margin_info['margin'],
                'nearest_edge': margin_info['nearest_edge']
            }

        # High attention: pause
        return {
            'action': 'pause',
            'sub_action': 'reject',
            'reason': f'Risk threshold reached ({risk_score:.1%} ≥ {effective_revise_threshold:.0%})',
            'guidance': f'Pause suggested: simplify approach, break into smaller steps, or take a break. Coherence: {coherence:.2f} (critical: {effective_coherence_threshold:.2f})',
            'margin': margin_info['margin'],
            'nearest_edge': margin_info['nearest_edge']
        }
    
    # =================================================================
    # UNITARES Core Parameters (from v4.1)
    # =================================================================
    
    # System dynamics parameters
    ALPHA = 0.5      # E-I coupling rate
    K = 0.1          # I-S coupling
    MU = 0.8         # S decay rate
    DELTA = 0.4      # V decay rate
    KAPPA = 0.3      # E-V coupling
    GAMMA_I = 0.3    # I self-regulation
    BETA_E = 0.1     # E-S coupling
    BETA_I = 0.05    # I-V coupling
    
    # Ethical drift parameters
    LAMBDA2 = 0.05   # Coherence coupling into S
    
    # Coherence function parameters
    C_MAX = 1.0      # Maximum coherence value
    
    # Time discretization
    DT = 0.1         # Base timestep for integration (seconds)
    DT_EXPECTED_INTERVAL = 15.0  # Expected check-in cadence (seconds)
    DT_MAX = 1.0     # Euler stability cap (max single-step dt)

    # Gap-recovery: when wall-clock dt exceeds the band that linear scaling
    # can represent (scaled_dt > DT_MAX), the next N attestations may run
    # on stale/transient state — for example, a MacBook clamshell sleep-wake
    # leaves the governance-MCP process with one cached attest cycle and
    # then a wake-window attest that arrives with discontinuous EMA inputs.
    # In that window we downgrade 'pause' decisions to 'proceed' (the next
    # cycle's verdict will catch genuine high-risk states). Evidence in
    # knowledge graph discovery 2026-05-15T14:27:26.894282+00:00.
    GAP_RECOVERY_CYCLES = 2

    # Warmup structural grace: on the first WARMUP_STRUCTURAL_GRACE_CYCLES
    # process-LOCAL updates after a process (re)start, the ODE state
    # (coherence/V) is a cold-start transient — the integrators reset and
    # state.coherence/V can briefly cross the void/coherence/basin floors even
    # for a healthy agent. In that window we suppress STRUCTURAL pauses
    # (void_pause / coherence_pause / basin_pause / cirs coherence-floor) ONLY
    # when the behavioral baseline is established AND says 'safe' — i.e. the
    # trustworthy self-relative signal contradicts the cold structural metric.
    # Risk-ceiling, CIRS resonance, and a non-safe behavioral verdict are NEVER
    # suppressed (a genuinely-degraded agent has high behavioral risk / a
    # non-safe verdict). Per-process counter — NOT persisted, NOT restored on
    # hydrate. Composes with the #575 baseline-restore: that restores the
    # baseline this guard trusts. (2026-06-03 Lumen restart false-pause; the
    # behavioral fix alone left a residual void_pause on the cold restart state.)
    WARMUP_STRUCTURAL_GRACE_CYCLES = 3
    WARMUP_STRUCTURAL_GRACE_ENABLED = (
        os.environ.get('GOVERNANCE_WARMUP_STRUCTURAL_GRACE', 'true').lower() == 'true'
    )

    # Pause TTL: an agent paused for longer than this is considered stale
    # and the next gate-traversal is allowed through, letting the
    # categorizer re-evaluate (its own gap-suppression handles the
    # first-after-gap state; a real problem re-pauses immediately on the
    # next cycle via the normal circuit-breaker path).
    #
    # Motivation: GAP_RECOVERY_CYCLES above only protects the *categorizer
    # path* — the case where a long gap would falsely paint a pause on a
    # first-after-gap check-in. It does not help once an agent is
    # *already* paused, because the pause-status check at every gate
    # (src/mcp_handlers/support/agent_auth.py, src/mcp_handlers/updates/
    # phases.py, etc.) rejects the call before the categorizer runs. The
    # 2026-05-09 → 2026-05-18 Watcher/Sentinel/Lumen silence (recovered
    # 2026-05-18 via operator self_recovery) was this class: pauses set
    # during the earlier 2026-05-08 sleep-wake incident persisted across
    # nine days because there was no aging mechanism.
    #
    # Default 72h (3 days): long enough to cover a long-weekend operator-
    # AFK window without the dashboard going noisy; shorter than the
    # observed 9-day silence so the operator's first sign would be one
    # auto-expire event in the audit log instead of weeks of fleet
    # blindness. All pause sources are categorizer-driven (only
    # agent_loop_detection.py:513 sets status=paused in the codebase, and
    # only on `decision_action == 'pause'` from monitor_decision.py's
    # four pause paths); loop-detection uses a separate cooldown
    # mechanism (loop_detected_at + loop_cooldown_until) and is not
    # affected by this TTL.
    #
    # Override via env `UNITARES_PAUSE_AUTO_EXPIRE_SECONDS` (seconds).
    PAUSE_AUTO_EXPIRE_SECONDS = int(
        os.getenv("UNITARES_PAUSE_AUTO_EXPIRE_SECONDS", str(72 * 3600))
    )

    # History window for metrics
    HISTORY_WINDOW = 1000  # Keep last 1000 updates for statistics
    
    # =================================================================
    # Telemetry & Calibration Thresholds
    # =================================================================
    
    # Suspicious pattern detection thresholds
    SUSPICIOUS_LOW_SKIP_RATE = 0.1  # Skip rate threshold for "low skip rate"
    SUSPICIOUS_LOW_CONFIDENCE = 0.7  # Confidence threshold for "low confidence"
    SUSPICIOUS_HIGH_SKIP_RATE = 0.5  # Skip rate threshold for "high skip rate"
    SUSPICIOUS_HIGH_CONFIDENCE = 0.85  # Confidence threshold for "high confidence"
    
    # Audit log rotation
    AUDIT_LOG_MAX_AGE_DAYS = 30  # Archive entries older than this

    # =================================================================
    # Epoch Configuration
    # =================================================================
    # Bump this when a model change invalidates existing stored data.
    # Most changes (bug fixes, new tools, docs) do NOT bump the epoch.
    # Only changes to EISV coupling, coherence formulas, or calibration
    # logic that make existing data wrong require a bump.
    CURRENT_EPOCH = 3

    # =================================================================
    # Temporal Narrator Configuration
    # =================================================================

    TEMPORAL_LONG_SESSION_HOURS = 2       # Signal when session exceeds this
    TEMPORAL_GAP_HOURS = 24               # Signal when gap since last session exceeds this
    TEMPORAL_IDLE_MINUTES = 30            # Signal when idle within session exceeds this
    TEMPORAL_CROSS_AGENT_MINUTES = 60     # Surface cross-agent activity within this window
    TEMPORAL_HIGH_CHECKIN_COUNT = 10      # High density: this many check-ins...
    TEMPORAL_HIGH_CHECKIN_WINDOW_MINUTES = 30  # ...within this window


# Export singleton config
config = GovernanceConfig()

# Invariant: APPROVE < REVISE < REJECT must always hold.
# Violation here means a config edit broke the ordering.
assert GovernanceConfig.RISK_APPROVE_THRESHOLD < GovernanceConfig.RISK_REVISE_THRESHOLD < GovernanceConfig.RISK_REJECT_THRESHOLD, (
    f"Risk threshold ordering violated: APPROVE({GovernanceConfig.RISK_APPROVE_THRESHOLD}) "
    f"< REVISE({GovernanceConfig.RISK_REVISE_THRESHOLD}) "
    f"< REJECT({GovernanceConfig.RISK_REJECT_THRESHOLD}) must hold"
)


# =================================================================
# Grounding Scale Constants — spec §3.4
# =================================================================
# Every normalization constant used by src/grounding/ modules ships with
# measurement provenance. Phase 1 ships placeholders; Phase 2 replaces with
# values measured on a reference corpus per the protocol in spec §3.4.
#
# IMPORTANT — heterogeneity, not homogeneity. These are placeholder fleet-wide
# values for Phase 1 scaffolding ONLY. A homogenized fleet is the wrong target:
# embodied creatures, cron-driven janitors, streaming observers, and ephemeral
# parsers do not share a healthy operating point or a tempo. Phase 2 calibration
# must produce class-conditional constants keyed on existing identity tags
# (embodied / autonomous / persistent / ephemeral) and labels (Lumen / Vigil /
# Sentinel / Watcher / Steward). The fleet-wide constant remains as the default
# for unclassified agents — a safe fallback, not the production target.
# See paper §3.4 (Heterogeneity as a First-Class Constraint).

@dataclass(frozen=True)
class ScaleConstant:
    """A scale/normalization constant with measurement provenance.

    provenance is one of:
      - "placeholder": initial guess, Phase 1; must be replaced before production
      - "measured":    measured on a named reference corpus per spec §3.4
      - "derived":     derived analytically from other quantities
      - "alias":       intentionally mirrors another class's value when the
                       agent is a known resident but has no independent corpus
                       yet (makes the fallback explicit in config instead of
                       relying on silent get(…, DEFAULT) at lookup time)
    """
    name: str
    value: float
    measured_on: str          # ISO date (YYYY-MM-DD) when set; Phase 1 = plan date
    corpus_size: int          # agent-turn count when measured; 0 for placeholder/alias
    percentile: Optional[int] # 90, 95, 99, etc.; None for non-percentile-derived
    provenance: str           # "placeholder" | "measured" | "derived" | "alias"
    notes: str = ""

    def __post_init__(self) -> None:
        if self.provenance not in {"placeholder", "measured", "derived", "alias"}:
            raise ValueError(f"unknown provenance {self.provenance!r}")
        if self.value <= 0:
            raise ValueError(f"scale constant {self.name} must be positive")


# Phase 1 placeholders — replace with measured values after §3.4 protocol runs.
S_SCALE = ScaleConstant(
    name="S_SCALE",
    value=3.0,
    measured_on="2026-04-18",
    corpus_size=0,
    percentile=None,
    provenance="placeholder",
    notes="Phase 1 placeholder. Spec §3.1 S: 90th-percentile S_raw on healthy corpus.",
)

I_SCALE = ScaleConstant(
    name="I_SCALE",
    value=2.0,
    measured_on="2026-04-18",
    corpus_size=0,
    percentile=None,
    provenance="placeholder",
    notes="Phase 1 placeholder. Spec §3.1 I: empirical MI upper envelope on held-out set.",
)

E_SCALE = ScaleConstant(
    name="E_SCALE",
    value=1.0,
    measured_on="2026-04-18",
    corpus_size=0,
    percentile=None,
    provenance="placeholder",
    notes="Phase 1 placeholder. FEP form only; resource form uses TOKENS_PER_SECOND_MAX.",
)

DELTA_NORM_MAX = ScaleConstant(
    name="DELTA_NORM_MAX",
    value=1.8,  # just above sqrt(3) so full-diagonal deviation hits coherence=0
    measured_on="2026-04-18",
    corpus_size=0,
    percentile=None,
    provenance="placeholder",
    notes="Phase 1 placeholder. Spec §3.4: 95th pct of observed ||Δ|| from healthy median.",
)

ALL_SCALE_CONSTANTS = [S_SCALE, I_SCALE, E_SCALE, DELTA_NORM_MAX]


# =================================================================
# Class-Conditional Scale Maps — paper §7
# =================================================================
# Each *_BY_CLASS dict is keyed on calibration class names returned by
# src.grounding.class_indicator.classify_agent. Phase 2 calibration populates
# these dicts with measured per-class values; Phase 1 ships them empty so
# every agent falls back to the fleet-wide *_DEFAULT below (the existing
# placeholder values, kept under their original names for back-compat).

S_SCALE_DEFAULT = S_SCALE
I_SCALE_DEFAULT = I_SCALE
E_SCALE_DEFAULT = E_SCALE
DELTA_NORM_MAX_DEFAULT = DELTA_NORM_MAX

# Per-class scale constants. Keys: class names from classify_agent
# (e.g., "Lumen", "Vigil", "embodied", "resident_persistent", "ephemeral").
# Populated by scripts/calibrate_class_conditional.py against the
# production agent_state corpus.
S_SCALE_BY_CLASS: Dict[str, ScaleConstant] = {}
I_SCALE_BY_CLASS: Dict[str, ScaleConstant] = {}
E_SCALE_BY_CLASS: Dict[str, ScaleConstant] = {}

# Manifold radius — the 95th percentile of state-space distance from each
# class's own healthy operating point. Re-measured 2026-06-27 on a 30-day
# healthy-regime slice of core.agent_state via
# `UNITARES_RESIDENTS='Lumen,Vigil,Sentinel,Watcher,Steward,Chronicler' \
#   python3 scripts/calibrate_class_conditional.py` (roster matches the live
# classifier so per-label residents map to these keys). Per-class envelopes
# differ by ~3.4× (Vigil 0.09 vs engaged_ephemeral 0.30), confirming the
# homogenization failure mode of paper §2.
#
# The 2026-04-18 values went stale (esp. Lumen's healthy E, see
# HEALTHY_OPERATING_POINT_BY_CLASS) — the manifold coherence saturated to 0 for
# Lumen on every check-in until this refresh. Re-run the generator when the
# fleet's operating regime shifts.
DELTA_NORM_MAX_BY_CLASS: Dict[str, ScaleConstant] = {
    "Lumen": ScaleConstant(
        name="DELTA_NORM_MAX[Lumen]", value=0.1635, measured_on="2026-06-27",
        corpus_size=12500, percentile=95, provenance="measured",
        notes="Class-conditional manifold radius from healthy slice."),
    "default": ScaleConstant(
        name="DELTA_NORM_MAX[default]", value=0.2018, measured_on="2026-04-18",
        corpus_size=2033, percentile=95, provenance="measured",
        notes="Retained from 2026-04-18; 2026-06-27 slice had N=16 (<30 "
              "threshold), too thin to re-measure."),
    "Sentinel": ScaleConstant(
        name="DELTA_NORM_MAX[Sentinel]", value=0.0881, measured_on="2026-06-27",
        corpus_size=5529, percentile=95, provenance="measured",
        notes="Class-conditional manifold radius from healthy slice."),
    "Vigil": ScaleConstant(
        name="DELTA_NORM_MAX[Vigil]", value=0.0885, measured_on="2026-06-27",
        corpus_size=1415, percentile=95, provenance="measured",
        notes="Class-conditional manifold radius from healthy slice."),
    "Watcher": ScaleConstant(
        name="DELTA_NORM_MAX[Watcher]", value=0.2391, measured_on="2026-06-27",
        corpus_size=1436, percentile=95, provenance="measured",
        notes="Class-conditional manifold radius from healthy slice."),
    "Steward": ScaleConstant(
        name="DELTA_NORM_MAX[Steward]", value=0.2018, measured_on="2026-06-27",
        corpus_size=0, percentile=None, provenance="alias",
        notes="Alias to default. Still 0 healthy rows in the 2026-06-27 window. "
              "Re-run scripts/calibrate_class_conditional.py once corpus exists."),
    "Chronicler": ScaleConstant(
        name="DELTA_NORM_MAX[Chronicler]", value=0.2018, measured_on="2026-06-27",
        corpus_size=26, percentile=None, provenance="alias",
        notes="Alias to default. N=26 in the 2026-06-27 window, below the 30 "
              "threshold. Re-run scripts/calibrate_class_conditional.py later."),
    "engaged_ephemeral": ScaleConstant(
        name="DELTA_NORM_MAX[engaged_ephemeral]", value=0.2952, measured_on="2026-06-27",
        corpus_size=2115, percentile=95, provenance="measured",
        notes="Class-conditional manifold radius from healthy slice."),
    "ephemeral": ScaleConstant(
        name="DELTA_NORM_MAX[ephemeral]", value=0.0857, measured_on="2026-06-27",
        corpus_size=277, percentile=95, provenance="measured",
        notes="Class-conditional manifold radius from healthy slice (new key; "
              "the tag-classified ephemeral population is now large enough)."),
}

# Healthy operating points per class — median (E, I, S) on healthy-regime
# slice. Used by _compute_manifold as the class-conditional baseline that
# replaces the fleet-wide BASIN_HIGH corner. Re-measured 2026-06-27 (same
# generator + roster as DELTA_NORM_MAX_BY_CLASS above).
#
# NOTE: Lumen's healthy E moved 0.745 -> 0.316 between 2026-04-18 and
# 2026-06-27 (N=12500 healthy-slice samples). This is Lumen's GENUINE normal —
# a low-energy Pi edge resident — not degradation; the old anchor assumed it ran
# hot like a coding agent (the individuality axiom: judge against its own
# normal). The stale anchor was why Lumen's manifold coherence read 0 on every
# check-in. healthy_S also feeds get_s_setpoint (live when UNITARES_S_SETPOINT
# is on) — the S shifts here are small but live-affecting.
HEALTHY_OPERATING_POINT_BY_CLASS: Dict[str, Tuple[float, float, float]] = {
    "Lumen":    (0.3160, 0.7824, 0.2104),   # N=12500 (E 0.745 -> 0.316; see note)
    "default":  (0.7264, 0.7934, 0.2364),   # retained 2026-04-18 (N=16 in 06-27 window)
    "Sentinel": (0.7804, 0.6852, 0.2492),   # N=5529
    "Vigil":    (0.7576, 0.7639, 0.1596),   # N=1415
    "Watcher":  (0.7932, 0.7097, 0.2140),   # N=1436
    "Steward":  (0.7264, 0.7934, 0.2364),   # alias=default (N=0; see DELTA_NORM_MAX[Steward])
    "Chronicler": (0.7264, 0.7934, 0.2364), # alias=default (N=26<30; see DELTA_NORM_MAX[Chronicler])
    "engaged_ephemeral": (0.7685, 0.6918, 0.3536), # N=2115
    "ephemeral": (0.7032, 0.7995, 0.1898),  # N=277 (new key)
}

# Default healthy operating point (fleet fallback for unclassified agents).
# Used by _compute_manifold when class has no measured value.
HEALTHY_OPERATING_POINT_DEFAULT: Tuple[float, float, float] = (
    BASIN_HIGH.E_min, BASIN_HIGH.I_min, 0.0
)


def get_healthy_operating_point(agent_class: str = "default") -> Tuple[float, float, float]:
    """Return class-conditional healthy (E, I, S); fall back to fleet default."""
    return HEALTHY_OPERATING_POINT_BY_CLASS.get(
        agent_class, HEALTHY_OPERATING_POINT_DEFAULT
    )


def get_s_scale(agent_class: str = "default") -> ScaleConstant:
    """Return class-conditional S_SCALE; fall back to fleet-wide default."""
    return S_SCALE_BY_CLASS.get(agent_class, S_SCALE_DEFAULT)


def get_i_scale(agent_class: str = "default") -> ScaleConstant:
    """Return class-conditional I_SCALE; fall back to fleet-wide default."""
    return I_SCALE_BY_CLASS.get(agent_class, I_SCALE_DEFAULT)


def get_e_scale(agent_class: str = "default") -> ScaleConstant:
    """Return class-conditional E_SCALE; fall back to fleet-wide default."""
    return E_SCALE_BY_CLASS.get(agent_class, E_SCALE_DEFAULT)


def get_delta_norm_max(agent_class: str = "default") -> ScaleConstant:
    """Return class-conditional manifold radius; fall back to fleet-wide default."""
    return DELTA_NORM_MAX_BY_CLASS.get(agent_class, DELTA_NORM_MAX_DEFAULT)


# =====================================================================
# S-attractor setpoint (Stage A — EISV fixed-point calibration)
# =====================================================================
# The S equation rests at S* = (β_complexity·complexity − λ₂·C)/μ ≈ 0.091 at
# baseline (complexity=0.5), but the *measured* healthy S is 0.17–0.31 per class
# (HEALTHY_OPERATING_POINT_BY_CLASS). That offset makes the manifold readout
# unthresholdable as a control signal (a healthy agent reads ~0.17, below the
# 0.40 critical line). See docs/proposals/eisv-fixed-point-calibration-gap-v0.md.
#
# Fix: decay S toward a per-class setpoint σ instead of toward 0, i.e.
# `-μ(S - σ)`. Choosing σ = healthy_S − S_SETPOINT_DRIVER_OFFSET lands the S
# equilibrium on the measured healthy S (since S* = σ + drivers/μ).
#
# OFF BY DEFAULT (UNITARES_S_SETPOINT). When disabled, get_s_setpoint() returns
# 0.0 and the dynamics are byte-identical to historical behavior. Enabling is
# gated on red-team validation against the real agent_state corpus.
#
# S_SETPOINT_DRIVER_OFFSET is the baseline driver contribution to S* at
# complexity=0.5, measured 2026-06-24 via scripts/analysis/eisv_equilibrium_gap.py
# (integrated equilibrium S = 0.091). Regenerate if μ/β_complexity/λ₂ change.
S_SETPOINT_DRIVER_OFFSET: float = 0.091


def s_setpoint_enabled() -> bool:
    """Whether the per-class S setpoint is active (UNITARES_S_SETPOINT). Default ON
    (live-proven): the S equilibrium rests on the class's measured-healthy S rather
    than decaying toward ~0. Set the env to 0/false/off/"" to force the legacy -μS."""
    return os.getenv("UNITARES_S_SETPOINT", "1").strip().lower() in {"1", "true", "on", "yes"}


def phi_telemetry_only() -> bool:
    """Whether Φ is demoted to telemetry (UNITARES_PHI_TELEMETRY_ONLY). Default ON
    (live-proven; the central maths-revamp posture).

    When on, the behavioral/residual assessment is authoritative for the verdict
    and risk score whenever it is confident; Φ no longer floors them (it only
    over-flags hard work as risk — the RLHF/punish-toward-ideal shape, see
    docs/proposals/eisv-maths-roadmap-v0.md §8.0). Φ is still computed and
    surfaced as a telemetry field. Cold-start agents (behavioral confidence below
    the gate) still fall back to the Φ path as the prior. Because authoritative
    behavioral can only *de-escalate* relative to the Φ floor, this never
    introduces a pause — it removes Φ's over-flagging. Set the env to
    0/false/off/"" to restore Φ-floors-risk (the legacy invariant).
    """
    return os.getenv("UNITARES_PHI_TELEMETRY_ONLY", "1").strip().lower() in {"1", "true", "on", "yes"}


def grounding_shadow_enabled() -> bool:
    """Whether to shadow-compare grounded vs ungrounded canonical metrics each
    check-in (UNITARES_GROUNDING_SHADOW). Default off.

    Behavior-neutral measurement: computes what enrich_grounding would produce
    for E/I/S/coherence (incl. s_source from any supplied logprobs) BEFORE the
    persist/response stages, records the per-dimension divergence via the
    'grounding_shadow' audit event, then reverts the live metrics unless
    grounding_apply_enabled(). Lets the fleet-wide metric shift be measured
    before activation — see [[project_logprob-entropy-grounding]].
    """
    return os.getenv("UNITARES_GROUNDING_SHADOW", "").strip().lower() in {"1", "true", "on", "yes"}


def grounding_apply_enabled() -> bool:
    """Whether grounded E/I/S/coherence actually replace the ODE/heuristic values
    in the canonical metrics (UNITARES_GROUNDING_APPLY). Default off.

    When on, grounding runs BEFORE persist + response-build (the #1092 ordering
    fix — enrich_grounding previously ran after both consumers and was a no-op).
    LIVE-AFFECTING: turning this on shifts coherence (manifold)/E/I/S fleet-wide,
    which moves basin/verdict/risk. Validate via grounding_shadow first.
    """
    return os.getenv("UNITARES_GROUNDING_APPLY", "").strip().lower() in {"1", "true", "on", "yes"}


def session_mirror_shadow_enabled() -> bool:
    """Whether to dual-write session/identity bindings into the PostgreSQL mirror
    tables (core.session_bindings, core.onboard_pins) alongside the Redis writes
    (UNITARES_SESSION_MIRROR_SHADOW). Default off.

    Behavior-neutral: Redis stays the authoritative read source; the PG writes
    are best-effort (failures swallowed) and nothing reads the mirror yet. Lets
    the durable mirror be populated and its write-path parity measured before the
    read flip. Redis-retirement Phase 1A — see
    docs/proposals/redis-retirement-phase-1-plan.md.
    """
    return os.getenv("UNITARES_SESSION_MIRROR_SHADOW", "").strip().lower() in {"1", "true", "on", "yes"}


def session_mirror_apply_enabled() -> bool:
    """Whether the resolver READS the PostgreSQL session mirror as a source of
    truth (UNITARES_SESSION_MIRROR_APPLY). Default off. LIVE-AFFECTING when on:
    PATH 2 resolves from core.session_bindings and pin lookup reads
    core.onboard_pins. Only flip after session_mirror_shadow parity is proven.
    Not wired in this PR — the read flip is a separate change.
    """
    return os.getenv("UNITARES_SESSION_MIRROR_APPLY", "").strip().lower() in {"1", "true", "on", "yes"}


def get_s_setpoint(agent_class: str = "default") -> float:
    """Per-class S decay target σ for the dynamics.

    Returns 0.0 when the flag is off (historical ``-μS`` behavior). When on,
    returns ``healthy_S(class) − S_SETPOINT_DRIVER_OFFSET`` clamped to ≥0, so the
    S equilibrium lands on the class's measured healthy S.
    """
    if not s_setpoint_enabled():
        return 0.0
    healthy_S = get_healthy_operating_point(agent_class)[2]
    return max(0.0, healthy_S - S_SETPOINT_DRIVER_OFFSET)


# =====================================================================
# Identity Honesty Part C — strict-mode gate
# =====================================================================
# Three ghost-creation paths are sources:
#   - PATH 0 (identity handler + middleware passthrough) accepting bare
#     agent_uuid + resume=true without proving ownership
#   - FALLBACK 2 in require_agent_id auto-generating `auto_<ts>_<uuid8>`
#   - Onboard-triggered orphan sweep catching siblings of fresh onboards
# Modes:
#   "off"    — unchanged pre-Part-C behavior (for emergency rollback)
#   "log"    — emit [IDENTITY_STRICT] warnings, do nothing else (default)
#   "strict" — reject the request with guidance, no ghost created
# Override: UNITARES_IDENTITY_STRICT env var.
IDENTITY_STRICT_MODE: str = os.getenv("UNITARES_IDENTITY_STRICT", "log").strip().lower()

_VALID_STRICT_MODES = frozenset({"off", "log", "strict"})
if IDENTITY_STRICT_MODE not in _VALID_STRICT_MODES:
    IDENTITY_STRICT_MODE = "log"


def identity_strict_mode() -> str:
    """Runtime accessor — respects env changes set after module load (tests)."""
    m = os.getenv("UNITARES_IDENTITY_STRICT", IDENTITY_STRICT_MODE).strip().lower()
    return m if m in _VALID_STRICT_MODES else "log"


# =============================================================================
# PATH 1 FINGERPRINT CHECK (2026-04-20, council follow-up to identity-honesty)
# =============================================================================
#
# `client_session_id` values of form `agent-{uuid[:12]}` are algorithmically
# derivable from any UUID a caller can observe (logs, check-ins, KG metadata,
# leaked anchor files). PATH 1 (Redis cache hit) resolves that shape to the
# bound UUID with no ownership proof, so any caller who learns a UUID can
# hijack the binding.
#
# This flag controls the fingerprint cross-check added at the PATH 1 cache
# hit site. Binding-time fingerprint is written by `_cache_session`;
# resume-time fingerprint is read from the request's SessionSignals.
#
# Modes:
#   "off"    — skip the check entirely
#   "log"    — emit [PATH1_FINGERPRINT_MISMATCH] + identity_hijack_suspected
#              broadcast when the fingerprints differ; resume still proceeds
#              (default — observation phase)
#   "strict" — same events, but the mismatched resume falls through to
#              PATH 3 (new session) instead of returning the cached UUID
#
# Override: UNITARES_SESSION_FINGERPRINT_CHECK env var.
SESSION_FINGERPRINT_CHECK_MODE: str = os.getenv(
    "UNITARES_SESSION_FINGERPRINT_CHECK", "log"
).strip().lower()

_VALID_FINGERPRINT_MODES = frozenset({"off", "log", "strict"})
if SESSION_FINGERPRINT_CHECK_MODE not in _VALID_FINGERPRINT_MODES:
    SESSION_FINGERPRINT_CHECK_MODE = "log"


def session_fingerprint_check_mode() -> str:
    """Runtime accessor — respects env changes set after module load (tests)."""
    m = os.getenv(
        "UNITARES_SESSION_FINGERPRINT_CHECK", SESSION_FINGERPRINT_CHECK_MODE
    ).strip().lower()
    return m if m in _VALID_FINGERPRINT_MODES else "log"


# =============================================================================
# PREFIX-BIND FINGERPRINT MODE (#802 — per-path hardening)
# =============================================================================
# The `agent-{uuid12}` prefix shape is resolvable from a victim's UUID alone
# (logs, KG metadata, leaked anchor files), and PATH 1 resolves it to the
# bound UUID. The global UNITARES_SESSION_FINGERPRINT_CHECK cannot safely flip
# to `strict` fleet-wide because IP:UA is legitimately SHARED by co-resident
# localhost clients (e.g. the bridge + gateway, both python-httpx) — strict
# would false-reject real twins. But the prefix shape specifically warrants a
# stricter ownership check than the global default, and — unlike the global
# check — an ABSENT binding-time fingerprint on a prefix key is itself
# non-authorizing: a UUID-derivable key with no recorded fingerprint carries
# no ownership proof at all (the bind_ip_ua-absent hole at resolution.py
# PATH 1, council finding for #802).
#
# This flag scopes that stricter check to the `agent-` prefix shape only,
# leaving the global default untouched. Modes mirror the global flag and
# follow the same staged off→log→strict ramp as STRICT_IDENTITY_REQUIRED
# (#425):
#   "off"    — no per-path check (DEFAULT; behavior identical to today)
#   "log"    — emit [PATH1_FINGERPRINT_MISMATCH] + identity_hijack_suspected
#              on a prefix-key ownership failure (fingerprint mismatch OR an
#              absent binding/current fingerprint); resume still proceeds
#   "strict" — same events, but the prefix resume falls through to a fresh
#              session (PATH 3) instead of returning the cached UUID
#
# When set above the global mode, the per-path mode takes precedence for the
# prefix shape. This closes only the CROSS-fingerprint hijack; a same-
# fingerprint co-resident still passes (that residual needs the substrate/UDS
# peer-credential path — see issue #802). Redaction stays load-bearing.
# Override: UNITARES_PREFIX_BIND_FINGERPRINT env var.
PREFIX_BIND_FINGERPRINT_MODE: str = os.getenv(
    "UNITARES_PREFIX_BIND_FINGERPRINT", "off"
).strip().lower()
if PREFIX_BIND_FINGERPRINT_MODE not in _VALID_FINGERPRINT_MODES:
    PREFIX_BIND_FINGERPRINT_MODE = "off"


def prefix_bind_fingerprint_mode() -> str:
    """Runtime accessor — respects env changes set after module load (tests)."""
    m = os.getenv(
        "UNITARES_PREFIX_BIND_FINGERPRINT", PREFIX_BIND_FINGERPRINT_MODE
    ).strip().lower()
    return m if m in _VALID_FINGERPRINT_MODES else "off"


# =============================================================================
# IP:UA ONBOARD PIN CHECK MODE (PATH 2 — council follow-up to #83)
# =============================================================================
# `derive_session_key` step 7 resolves an unauthenticated `onboard()` call
# (no continuity_token, no client_session_id, no mcp/oauth/x- session headers)
# to a previously-pinned session by IP:UA fingerprint alone. On shared hosts or
# when multiple same-family agents run on one machine this silently resumes
# the prior agent's UUID — the PATH 2 analogue of the PATH 0/1 bleeds already
# closed by #78/#81/#83.
#
# Modes (mirror UNITARES_SESSION_FINGERPRINT_CHECK):
#   "off"    — skip the check entirely; pin-fallback resume proceeds silently
#   "log"    — emit [PATH2_IPUA_PIN_RESUME] + identity_hijack_suspected
#              broadcast when onboard() hits the pin fallback with no proof;
#              resume still proceeds (observation phase — pre-2026-04-21 default)
#   "strict" — same events, plus force resume=False so the onboard mints a
#              fresh identity instead of silently adopting the pinned UUID.
#              The pin itself is NOT deleted — the legitimate owner can still
#              resume by presenting a continuity_token or agent_uuid.
#              (current default — identity ontology v2 §85 retires implicit
#              cross-process-instance identity via fingerprint pin)
#
# Override: UNITARES_IPUA_PIN_CHECK env var.
IPUA_PIN_CHECK_MODE: str = os.getenv(
    "UNITARES_IPUA_PIN_CHECK", "strict"
).strip().lower()

_VALID_IPUA_PIN_MODES = frozenset({"off", "log", "strict"})
if IPUA_PIN_CHECK_MODE not in _VALID_IPUA_PIN_MODES:
    IPUA_PIN_CHECK_MODE = "strict"


def ipua_pin_check_mode() -> str:
    """Runtime accessor — respects env changes set after module load (tests)."""
    m = os.getenv(
        "UNITARES_IPUA_PIN_CHECK", IPUA_PIN_CHECK_MODE
    ).strip().lower()
    return m if m in _VALID_IPUA_PIN_MODES else "strict"
