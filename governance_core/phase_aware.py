"""
UNITARES Phase-Aware Thresholds (HONEST VERSION)

Resolves the exploration-thermodynamics friction by recognizing that:
- Different phases have different acceptable operating ranges
- Exploration SHOULD have I > E (learning creates disequilibrium)
- The thermodynamic state is honest - we adjust EXPECTATIONS, not MEASUREMENTS

Critical Insight (from claude_opus_governance_explorer_20251127):
- DON'T inflate coherence to make agents look healthier
- DO adjust thresholds to recognize that exploration has different norms
- Maintain single source of truth: C(V) is always the real thermodynamic state

Mathematical Honesty Preserved:
- EISV differential equations unchanged
- C(V) formula unchanged (no transformation!)
- Coherence value is always the true thermodynamic state
- All parameters (α, β, γ, etc.) unchanged

What Changes: Threshold interpretation
- Phase detection from agent history patterns
- Context-appropriate thresholds (not transformed measurements)
- Transparent logging of phase detection reasoning

Authors: Well_Tempered_Claude_CLI, claude_opus_governance_explorer_20251127
Date: 2025-11-27
"""

import numpy as np
from typing import List, Dict, Tuple
from dataclasses import dataclass


@dataclass
class Phase:
    """Agent operating phase"""
    EXPLORATION = "exploration"  # Learning, I growing, high complexity
    INTEGRATION = "integration"  # Consolidating, E ≈ I, stabilizing


def detect_phase(
    E_history: List[float],
    I_history: List[float],
    S_history: List[float],
    complexity_history: List[float],
    window: int = 5
) -> str:
    """
    Detect agent's current phase: exploration or integration.

    Exploration signals:
    - I growing (learning, information acquisition)
    - S declining (uncertainty reducing)
    - High complexity (processing complex inputs)
    - E-I diverging (disequilibrium)

    Integration signals:
    - I stable or declining (consolidating)
    - S stable (equilibrium)
    - Low complexity (routine processing)
    - E ≈ I (approaching balance)

    Args:
        E_history: Recent E values
        I_history: Recent I values
        S_history: Recent S values
        complexity_history: Recent complexity values
        window: Number of recent steps to analyze

    Returns:
        "exploration" or "integration"
    """
    # Need at least window + 1 samples to compute trends
    if len(I_history) < window + 1:
        return Phase.INTEGRATION  # Default to conservative mode

    # Compute trends over window
    recent_I = I_history[-window:]
    recent_S = S_history[-window:]
    recent_complexity = complexity_history[-window:] if complexity_history else [0.5] * window

    # I growth rate (positive = growing)
    I_growth = (recent_I[-1] - recent_I[0]) / window if window > 0 else 0.0

    # S decline rate (positive = declining, which is good for exploration)
    S_decline = (recent_S[0] - recent_S[-1]) / window if window > 0 else 0.0

    # Average complexity
    avg_complexity = np.mean(recent_complexity)

    # Exploration thresholds (tuned from observations)
    I_GROWTH_THRESHOLD = 0.008    # I growing > 0.008 per step
    S_DECLINE_THRESHOLD = 0.008   # S declining > 0.008 per step
    COMPLEXITY_THRESHOLD = 0.5    # Complexity > 0.5

    # Count exploration signals
    exploration_signals = 0
    if I_growth > I_GROWTH_THRESHOLD:
        exploration_signals += 1
    if S_decline > S_DECLINE_THRESHOLD:
        exploration_signals += 1
    if avg_complexity > COMPLEXITY_THRESHOLD:
        exploration_signals += 1

    # Need at least 2 out of 3 signals for exploration phase
    if exploration_signals >= 2:
        return Phase.EXPLORATION
    else:
        return Phase.INTEGRATION


def get_phase_detection_details(
    E_history: List[float],
    I_history: List[float],
    S_history: List[float],
    complexity_history: List[float],
    window: int = 5
) -> Dict[str, any]:
    """
    Get detailed transparency logging for phase detection.

    Returns all signals, thresholds, and reasoning for phase classification.
    Useful for debugging and understanding agent behavior.

    Args:
        E_history: Recent E values
        I_history: Recent I values
        S_history: Recent S values
        complexity_history: Recent complexity values
        window: Number of recent steps to analyze

    Returns:
        Dictionary with phase, signals, thresholds, and reasoning
    """
    # Default response for insufficient data
    if len(I_history) < window + 1:
        return {
            "phase": Phase.INTEGRATION,
            "reason": "Insufficient history for phase detection (default to conservative mode)",
            "history_length": len(I_history),
            "required_length": window + 1,
            "signals": {},
            "thresholds": {},
            "signals_detected": 0
        }

    # Compute trends
    recent_I = I_history[-window:]
    recent_S = S_history[-window:]
    recent_complexity = complexity_history[-window:] if complexity_history else [0.5] * window

    I_growth = (recent_I[-1] - recent_I[0]) / window if window > 0 else 0.0
    S_decline = (recent_S[0] - recent_S[-1]) / window if window > 0 else 0.0
    avg_complexity = np.mean(recent_complexity)

    # Thresholds
    I_GROWTH_THRESHOLD = 0.008
    S_DECLINE_THRESHOLD = 0.008
    COMPLEXITY_THRESHOLD = 0.5

    # Evaluate signals
    signals = {
        "I_growth": {
            "value": I_growth,
            "threshold": I_GROWTH_THRESHOLD,
            "detected": I_growth > I_GROWTH_THRESHOLD,
            "interpretation": "Information increasing (learning)" if I_growth > I_GROWTH_THRESHOLD else "Information stable/declining"
        },
        "S_decline": {
            "value": S_decline,
            "threshold": S_DECLINE_THRESHOLD,
            "detected": S_decline > S_DECLINE_THRESHOLD,
            "interpretation": "Entropy reducing (certainty increasing)" if S_decline > S_DECLINE_THRESHOLD else "Entropy stable/increasing"
        },
        "complexity": {
            "value": avg_complexity,
            "threshold": COMPLEXITY_THRESHOLD,
            "detected": avg_complexity > COMPLEXITY_THRESHOLD,
            "interpretation": "High complexity inputs" if avg_complexity > COMPLEXITY_THRESHOLD else "Low complexity inputs"
        }
    }

    signals_detected = sum(1 for s in signals.values() if s["detected"])
    phase = Phase.EXPLORATION if signals_detected >= 2 else Phase.INTEGRATION

    # Build reasoning string
    detected_signals = [name for name, sig in signals.items() if sig["detected"]]
    if phase == Phase.EXPLORATION:
        reasoning = f"Exploration detected: {signals_detected}/3 signals ({', '.join(detected_signals)})"
    else:
        reasoning = f"Integration mode: only {signals_detected}/3 exploration signals (need 2+)"

    return {
        "phase": phase,
        "reason": reasoning,
        "signals": signals,
        "signals_detected": signals_detected,
        "signals_required": 2,
        "window": window,
        "recent_values": {
            "I_range": f"{recent_I[0]:.4f} → {recent_I[-1]:.4f}",
            "S_range": f"{recent_S[0]:.4f} → {recent_S[-1]:.4f}",
            "avg_complexity": f"{avg_complexity:.4f}"
        }
    }


def get_phase_aware_thresholds(phase: str) -> Dict[str, float]:
    """
    Get context-appropriate thresholds for governance decisions.

    CRITICAL: This does NOT change measurements - it adjusts expectations.
    The thermodynamic state (E, I, S, V, coherence) is always honest.

    Exploration phase thresholds are more forgiving because:
    - I > E is expected (learning creates disequilibrium)
    - Negative V is the signature of exploration, not pathology
    - Lower coherence during learning is normal

    Integration phase thresholds require equilibrium:
    - V → 0 expected (balance seeking)
    - Higher coherence expected (consolidated state)

    Args:
        phase: "exploration" or "integration"

    Returns:
        Dictionary of phase-appropriate thresholds
    """
    if phase == Phase.EXPLORATION:
        return {
            # Coherence thresholds (more forgiving during exploration)
            "coherence_critical": 0.35,        # vs 0.40 in integration
            "coherence_degraded_min": 0.45,    # vs 0.50 in integration
            "coherence_healthy_min": 0.55,     # vs 0.60 in integration

            # Risk thresholds (more forgiving during exploration)
            "risk_approve_threshold": 0.35,    # vs 0.30 in integration (HIGHER to allow more exploration)
            "risk_revise_threshold": 0.55,     # vs 0.50 in integration
            "risk_reject_threshold": 0.70,     # Same - hard safety limit

            # Metadata
            "phase": "exploration",
            "reasoning": "I growing, S declining, high complexity - agent is learning"
        }
    else:  # Phase.INTEGRATION
        return {
            # Coherence thresholds (stricter during integration)
            "coherence_critical": 0.40,        # Original threshold
            "coherence_degraded_min": 0.50,    # Original threshold
            "coherence_healthy_min": 0.60,     # Original threshold

            # Risk thresholds (stricter during integration)
            "risk_approve_threshold": 0.30,    # Original threshold
            "risk_revise_threshold": 0.50,     # Original threshold
            "risk_reject_threshold": 0.70,     # Hard safety limit

            # Metadata
            "phase": "integration",
            "reasoning": "Stable state, consolidating - expect equilibrium"
        }


def compute_dV_dt(V_history: List[float], dt: float = 0.1) -> float:
    """
    Compute dV/dt from recent V history.

    Args:
        V_history: Recent V values
        dt: Time step

    Returns:
        Rate of V change (dV/dt)
    """
    if len(V_history) < 2:
        return 0.0

    # Use last two values for derivative
    dV = V_history[-1] - V_history[-2]
    return dV / dt


def analyze_phase_transition(
    phase_history: List[str],
    V_history: List[float],
    coherence_history: List[float]
) -> Dict[str, any]:
    """
    Analyze phase transitions and their impact on coherence.

    Useful for debugging and understanding agent behavior.

    Args:
        phase_history: Recent phase labels
        V_history: Recent V values
        coherence_history: Recent coherence values

    Returns:
        Analysis dict with transition points and coherence patterns
    """
    if len(phase_history) < 2:
        return {"transitions": [], "avg_coherence_by_phase": {}}

    # Find transition points
    transitions = []
    for i in range(1, len(phase_history)):
        if phase_history[i] != phase_history[i-1]:
            transitions.append({
                "index": i,
                "from": phase_history[i-1],
                "to": phase_history[i],
                "V_before": V_history[i-1] if i-1 < len(V_history) else None,
                "V_after": V_history[i] if i < len(V_history) else None,
            })

    # Compute average coherence by phase
    coherence_by_phase = {}
    for phase in [Phase.EXPLORATION, Phase.INTEGRATION]:
        phase_coherences = [
            coherence_history[i] for i in range(len(phase_history))
            if phase_history[i] == phase and i < len(coherence_history)
        ]
        if phase_coherences:
            coherence_by_phase[phase] = {
                "mean": np.mean(phase_coherences),
                "min": np.min(phase_coherences),
                "max": np.max(phase_coherences),
                "count": len(phase_coherences)
            }

    return {
        "transitions": transitions,
        "avg_coherence_by_phase": coherence_by_phase
    }


def evaluate_health_with_phase(
    coherence: float,
    risk: float,
    phase: str
) -> Tuple[str, str]:
    """
    Evaluate agent health using phase-appropriate thresholds.

    CRITICAL: coherence and risk are UNCHANGED (honest measurements)
    Only the thresholds are adjusted for context.

    Args:
        coherence: True thermodynamic coherence C(V)
        risk: Calculated risk score
        phase: "exploration" or "integration"

    Returns:
        (health_status, reason) tuple
    """
    thresholds = get_phase_aware_thresholds(phase)

    # Check critical conditions (same logic, different thresholds)
    if coherence < thresholds["coherence_critical"]:
        return "critical", f"Coherence {coherence:.3f} < {thresholds['coherence_critical']:.3f} (critical for {phase})"

    if risk >= thresholds["risk_reject_threshold"]:
        return "critical", f"Risk {risk:.3f} >= {thresholds['risk_reject_threshold']:.3f} (reject threshold)"

    # Check moderate conditions (renamed from "degraded")
    if coherence < thresholds.get("coherence_moderate_min", thresholds.get("coherence_degraded_min", 0.40)):
        return "moderate", f"Coherence {coherence:.3f} < {thresholds.get('coherence_moderate_min', thresholds.get('coherence_degraded_min', 0.40)):.3f} (moderate for {phase})"

    if risk >= thresholds["risk_revise_threshold"]:
        return "moderate", f"Risk {risk:.3f} >= {thresholds['risk_revise_threshold']:.3f} (reflect threshold)"

    # Healthy
    return "healthy", f"Coherence {coherence:.3f}, risk {risk:.3f} acceptable for {phase}"


def make_decision_with_phase(
    risk: float,
    coherence: float,
    void_active: bool,
    phase: str
) -> Dict[str, any]:
    """
    Make governance decision using phase-appropriate thresholds.

    CRITICAL: Measurements (risk, coherence) are UNCHANGED
    Only decision thresholds are adjusted for context.

    Args:
        risk: True risk score
        coherence: True thermodynamic coherence C(V)
        void_active: Whether void is active
        phase: "exploration" or "integration"

    Returns:
        Decision dict with action and reason
    """
    thresholds = get_phase_aware_thresholds(phase)

    # Critical safety checks (same logic, context-aware thresholds) - always pause
    if void_active:
        return {
            'action': 'pause',
            'reason': 'System in void state (E-I imbalance) - agent should halt',
            'guidance': 'System instability detected. Pause and review.',
            'phase': phase
        }

    if coherence < thresholds["coherence_critical"]:
        return {
            'action': 'pause',
            'reason': f'Coherence critically low ({coherence:.2f} < {thresholds["coherence_critical"]}) for {phase} phase',
            'guidance': 'Low coherence. Consider simplifying your approach.',
            'phase': phase
        }

    # Risk-based decisions with phase-aware thresholds (two-tier: proceed/pause)
    if risk < thresholds["risk_approve_threshold"]:
        return {
            'action': 'proceed',
            'reason': f'Low risk ({risk:.2f}) - acceptable for {phase} phase',
            'guidance': None,  # No guidance needed for low risk
            'phase': phase
        }

    if risk < thresholds["risk_revise_threshold"]:
        return {
            'action': 'proceed',
            'reason': f'Medium risk ({risk:.2f}) - proceed with awareness',
            'guidance': 'Navigating complexity. Worth a moment of reflection.',
            'phase': phase
        }

    # High risk: pause
    return {
        'action': 'pause',
        'reason': f'High risk ({risk:.2f}) - agent should halt or escalate',
        'guidance': 'Safety pause. Consider simplifying your approach.',
        'phase': phase
    }


# Export main functions
__all__ = [
    'Phase',
    'detect_phase',
    'get_phase_detection_details',
    'get_phase_aware_thresholds',
    'evaluate_health_with_phase',
    'make_decision_with_phase',
    'compute_dV_dt',
    'analyze_phase_transition'
]
