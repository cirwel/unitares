"""Behavioral assessment: transparent, component-based risk from behavioral EISV.

No sigmoid/phi black box. Each risk component has a clear source and weight.
Assessment is auditable — you can trace exactly why a verdict was issued.

After warmup, scoring switches from fixed universal thresholds to
self-relative z-score deviations from the agent's own behavioral baseline.
Absolute safety floors always apply regardless of baseline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from src.behavioral_state import BehavioralEISV, eisv_min_std_for_dimension


@dataclass
class AssessmentResult:
    """Result of behavioral state assessment."""

    # Overall
    health: str          # "healthy", "moderate", "at_risk", "critical"
    verdict: str         # "safe", "caution", "high-risk"
    risk: float          # [0, 1] composite risk score

    # Coherence from rho (update coherence)
    coherence: float     # [0, 1] mapped from rho [-1, 1]

    # Component breakdown (for transparency/debugging)
    components: Dict[str, float]

    # Optional guidance text
    guidance: Optional[str] = None


# Verdict thresholds
RISK_SAFE_THRESHOLD = 0.35
RISK_CAUTION_THRESHOLD = 0.60

# Component-level fixed-threshold risk boundaries.
FIXED_E_RISK_FLOOR = 0.40
FIXED_I_RISK_FLOOR = 0.40
FIXED_S_RISK_CEILING = 0.50
FIXED_CONVERGENT_S_RISK_CEILING = 0.60
FIXED_V_RISK_CEILING = 0.15

# Non-circular absolute EISV-safe gate for self-relative scoring.
#
# This is deliberately not the stricter BASIN_HIGH target shape from
# governance_config because that shape includes convergence targets
# (I>=0.70, S<=0.25) that healthy boundary residents can miss without being
# dangerous; using it here would not fix the 2026-06-13 Sentinel false pause.
# It is also not classify_basin(), because that would be circular: this scorer
# is creating the risk input classify_basin needs.
EISV_SAFE_E_MIN = 0.60
EISV_SAFE_I_MIN = 0.60
EISV_SAFE_S_MAX = 0.50
EISV_SAFE_CONVERGENT_S_MAX = 0.60
EISV_SAFE_V_ABS_MAX = 0.15

# Absolute safety floors — always active, override baseline.
# These catch states that are genuinely dangerous regardless of an agent's
# characteristic operating point. Set between "extreme" and the fixed-
# threshold triggers (0.4 for E/I, 0.5 for S) to provide meaningful
# backstop for agents whose baseline normalizes persistently bad states.
ABSOLUTE_E_FLOOR = 0.30
ABSOLUTE_I_FLOOR = 0.30
ABSOLUTE_S_CEILING = 0.70
ABSOLUTE_V_CEILING = 0.50

# Sigma thresholds for self-relative scoring
SIGMA_MILD = 1.5      # noticeably different from self
SIGMA_MODERATE = 2.0   # concerning
SIGMA_SEVERE = 3.0     # severe deviation


def assess_behavioral_state(
    state: BehavioralEISV,
    rho: float = 0.0,
    continuity_energy: float = 0.0,
    agent_context: Optional[Dict] = None,
) -> AssessmentResult:
    """Assess agent health from behavioral EISV + auxiliary signals.

    Uses self-relative scoring after warmup, fixed thresholds before.
    Absolute safety floors always apply.

    Args:
        state: Current behavioral EISV state
        rho: Update coherence from HCK [-1, 1]
        continuity_energy: CE from continuity layer [0, inf)
        agent_context: Optional dict with task_type, update_count, etc.

    Returns:
        AssessmentResult with health, verdict, risk breakdown
    """
    ctx = agent_context or {}

    # Score components based on baseline status
    if state.is_baselined:
        components = _score_self_relative(state, rho, continuity_energy, ctx)
    else:
        components = _score_fixed_threshold(state, rho, continuity_energy, ctx)

    # Absolute safety floors — always active, take max per component
    floor_components = _score_absolute_floors(state)
    for key, value in floor_components.items():
        components[key] = max(components.get(key, 0.0), value)

    # --- Trend bonus: improving E+I reduces risk slightly ---
    trend_bonus = 0.0
    if state.update_count >= 5:
        e_trend = state.trend("E")
        i_trend = state.trend("I")
        if e_trend > 0.005 and i_trend > 0.005:
            trend_bonus = -0.05  # small risk reduction for improving trajectory

    # --- Composite risk ---
    risk = sum(components.values()) + trend_bonus
    risk = max(0.0, min(1.0, risk))

    # --- Coherence from rho ---
    # Map rho [-1, 1] to coherence [0, 1]
    coherence = (rho + 1.0) / 2.0
    coherence = max(0.0, min(1.0, coherence))

    # --- Verdict ---
    if risk < RISK_SAFE_THRESHOLD:
        verdict = "safe"
    elif risk < RISK_CAUTION_THRESHOLD:
        verdict = "caution"
    else:
        verdict = "high-risk"

    # --- Health ---
    if risk < 0.20:
        health = "healthy"
    elif risk < RISK_SAFE_THRESHOLD:
        health = "moderate"
    elif risk < RISK_CAUTION_THRESHOLD:
        health = "at_risk"
    else:
        health = "critical"

    # --- Guidance ---
    task_type = ctx.get("task_type", "mixed")
    guidance = _generate_guidance(state, components, health, verdict, task_type)

    return AssessmentResult(
        health=health,
        verdict=verdict,
        risk=round(risk, 4),
        coherence=round(coherence, 4),
        components={k: round(v, 4) for k, v in components.items()},
        guidance=guidance,
    )


def _score_fixed_threshold(
    state: BehavioralEISV,
    rho: float,
    continuity_energy: float,
    ctx: Dict,
) -> Dict[str, float]:
    """Fixed-threshold assessment — used during warmup phase.

    Identical to the original assessment logic.
    """
    components: Dict[str, float] = {}

    # --- Component 1: Low Energy (weight: 0.30) ---
    if state.E < FIXED_E_RISK_FLOOR:
        components["low_E"] = 0.30 * (FIXED_E_RISK_FLOOR - state.E) / FIXED_E_RISK_FLOOR
    else:
        components["low_E"] = 0.0

    # --- Component 2: Low Integrity (weight: 0.30) ---
    if state.I < FIXED_I_RISK_FLOOR:
        components["low_I"] = 0.30 * (FIXED_I_RISK_FLOOR - state.I) / FIXED_I_RISK_FLOOR
    else:
        components["low_I"] = 0.0

    # --- Component 3: High Entropy (weight: 0.20) ---
    s_threshold = _fixed_s_risk_ceiling(ctx)
    if state.S > s_threshold:
        components["high_S"] = 0.20 * min(1.0, (state.S - s_threshold) / (1.0 - s_threshold))
    else:
        components["high_S"] = 0.0

    # --- Component 4: High |V| imbalance (weight: 0.20) ---
    abs_v = abs(state.V)
    if abs_v > FIXED_V_RISK_CEILING:
        components["high_V"] = 0.20 * min(1.0, (abs_v - FIXED_V_RISK_CEILING) / (1.0 - FIXED_V_RISK_CEILING))
    else:
        components["high_V"] = 0.0

    # --- Component 5: Adversarial rho (weight: 0.15) ---
    if rho < -0.2:
        components["adversarial_rho"] = 0.15 * min(1.0, (-0.2 - rho) / 0.8)
    else:
        components["adversarial_rho"] = 0.0

    # --- Component 6: High continuity energy (weight: 0.10) ---
    if continuity_energy > 0.5:
        components["high_CE"] = 0.10 * min(1.0, (continuity_energy - 0.5) / 1.5)
    else:
        components["high_CE"] = 0.0

    return components


def _score_self_relative(
    state: BehavioralEISV,
    rho: float,
    continuity_energy: float,
    ctx: Dict,
) -> Dict[str, float]:
    """Self-relative assessment — deviation from agent's own behavioral baseline.

    Same components and weights as fixed-threshold mode, but triggers are based
    on sigma-deviations from the agent's characteristic operating point.
    """
    components: Dict[str, float] = {}
    absolute_eisv_safe = _is_absolute_eisv_safe(state, ctx)

    # --- Component 1: E deviation below baseline (weight: 0.30) ---
    # Inside the absolute EISV-safe region, movement from self-baseline is
    # information, not risk. Outside it, relative deviations are allowed to
    # contribute again, with a small per-dimension EMA-derived denominator
    # guard so exact-zero baselines do not become invisible.
    z_E = state.deviation("E", min_std=eisv_min_std_for_dimension("E"))
    if not absolute_eisv_safe and z_E < -SIGMA_MILD:
        severity = min(1.0, (-z_E - SIGMA_MILD) / (SIGMA_SEVERE - SIGMA_MILD))
        components["low_E"] = 0.30 * severity
    else:
        components["low_E"] = 0.0

    # --- Component 2: I deviation below baseline (weight: 0.30) ---
    z_I = state.deviation("I", min_std=eisv_min_std_for_dimension("I"))
    if not absolute_eisv_safe and z_I < -SIGMA_MILD:
        severity = min(1.0, (-z_I - SIGMA_MILD) / (SIGMA_SEVERE - SIGMA_MILD))
        components["low_I"] = 0.30 * severity
    else:
        components["low_I"] = 0.0

    # --- Component 3: S deviation above baseline (weight: 0.20) ---
    z_S = state.deviation("S", min_std=eisv_min_std_for_dimension("S"))
    task_type = ctx.get("task_type", "mixed")
    sigma_threshold = SIGMA_MILD
    if task_type == "convergent":
        sigma_threshold = SIGMA_MODERATE
    if not absolute_eisv_safe and z_S > sigma_threshold:
        severity = min(1.0, (z_S - sigma_threshold) / (SIGMA_SEVERE - sigma_threshold))
        components["high_S"] = 0.20 * severity
    else:
        components["high_S"] = 0.0

    # --- Component 4: |V| deviation above baseline (weight: 0.20) ---
    z_V = state.deviation("V", min_std=eisv_min_std_for_dimension("V"))
    if not absolute_eisv_safe and abs(z_V) > SIGMA_MILD:
        severity = min(1.0, (abs(z_V) - SIGMA_MILD) / (SIGMA_SEVERE - SIGMA_MILD))
        components["high_V"] = 0.20 * severity
    else:
        components["high_V"] = 0.0

    # --- Component 5: Adversarial rho (weight: 0.15) --- (not baseline-relative)
    if rho < -0.2:
        components["adversarial_rho"] = 0.15 * min(1.0, (-0.2 - rho) / 0.8)
    else:
        components["adversarial_rho"] = 0.0

    # --- Component 6: High continuity energy (weight: 0.10) --- (not baseline-relative)
    if continuity_energy > 0.5:
        components["high_CE"] = 0.10 * min(1.0, (continuity_energy - 0.5) / 1.5)
    else:
        components["high_CE"] = 0.0

    return components


def _fixed_s_risk_ceiling(ctx: Dict) -> float:
    """Absolute S boundary used by fixed-threshold scoring."""
    if ctx.get("task_type", "mixed") == "convergent":
        return FIXED_CONVERGENT_S_RISK_CEILING
    return FIXED_S_RISK_CEILING


def _eisv_safe_s_max(ctx: Dict) -> float:
    """Task-aware S boundary for the non-circular EISV-safe gate."""
    if ctx.get("task_type", "mixed") == "convergent":
        return EISV_SAFE_CONVERGENT_S_MAX
    return EISV_SAFE_S_MAX


def _is_absolute_eisv_safe(state: BehavioralEISV, ctx: Dict) -> bool:
    """True when raw EISV is safe enough that self-relative movement is not risk."""
    return (
        state.E >= EISV_SAFE_E_MIN
        and state.I >= EISV_SAFE_I_MIN
        and state.S <= _eisv_safe_s_max(ctx)
        and abs(state.V) <= EISV_SAFE_V_ABS_MAX
    )


def _score_absolute_floors(state: BehavioralEISV) -> Dict[str, float]:
    """Absolute safety floors — always active, override baseline.

    These catch genuinely dangerous states that no amount of baseline
    normalization should mask.
    """
    components: Dict[str, float] = {}

    if state.E < ABSOLUTE_E_FLOOR:
        components["low_E"] = 0.30 * (ABSOLUTE_E_FLOOR - state.E) / ABSOLUTE_E_FLOOR
    if state.I < ABSOLUTE_I_FLOOR:
        components["low_I"] = 0.30 * (ABSOLUTE_I_FLOOR - state.I) / ABSOLUTE_I_FLOOR
    if state.S > ABSOLUTE_S_CEILING:
        components["high_S"] = 0.20 * min(1.0, (state.S - ABSOLUTE_S_CEILING) / (1.0 - ABSOLUTE_S_CEILING))
    if abs(state.V) > ABSOLUTE_V_CEILING:
        components["high_V"] = 0.20 * min(1.0, (abs(state.V) - ABSOLUTE_V_CEILING) / (1.0 - ABSOLUTE_V_CEILING))

    return components


def _generate_guidance(
    state: BehavioralEISV,
    components: Dict[str, float],
    health: str,
    verdict: str,
    task_type: str,
) -> Optional[str]:
    """Generate actionable guidance from assessment components."""
    if health == "healthy":
        return None

    # Find the dominant risk component
    if not components:
        return None
    top_component = max(components, key=components.get)
    top_value = components[top_component]

    if top_value < 0.01:
        return None

    if state.is_baselined:
        guidance_map = {
            "low_E": (
                f"Energy below your baseline (E={state.E:.2f}, typical={state._baseline_E.mean:.2f}). "
                f"Something may be reducing your capacity."
            ),
            "low_I": (
                f"Integrity below your baseline (I={state.I:.2f}, typical={state._baseline_I.mean:.2f}). "
                f"Check calibration and recent outcomes."
            ),
            "high_S": (
                f"Entropy above your baseline (S={state.S:.2f}, typical={state._baseline_S.mean:.2f}). "
                f"You may be in an unstable regime."
            ),
            "high_V": (
                f"E-I imbalance beyond your norm (V={state.V:.2f}, typical={state._baseline_V.mean:.2f}). "
                f"{'Slow down.' if state.V > 0 else 'Try more exploration.'}"
            ),
            "adversarial_rho": "Updates are incoherent. E and I moving in opposite directions.",
            "high_CE": "High state volatility. Agent state is changing rapidly.",
        }
    else:
        guidance_map = {
            "low_E": f"Low energy (E={state.E:.2f}). Consider simplifying tasks or checking tool reliability.",
            "low_I": f"Low integrity (I={state.I:.2f}). Calibration may be off — check recent outcomes.",
            "high_S": f"High entropy (S={state.S:.2f}). Regime may be unstable — consider consolidating.",
            "high_V": f"E-I imbalance (V={state.V:.2f}). {'Running hot — slow down.' if state.V > 0 else 'Running careful — increase exploration.'}",
            "adversarial_rho": "Updates are incoherent. E and I moving in opposite directions.",
            "high_CE": "High state volatility. Agent state is changing rapidly.",
        }

    return guidance_map.get(top_component)
