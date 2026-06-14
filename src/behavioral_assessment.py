"""Behavioral assessment: transparent, component-based risk from behavioral EISV.

No sigmoid/phi black box. Each risk component has a clear source and weight.
Assessment is auditable — you can trace exactly why a verdict was issued.

After warmup, scoring switches from fixed universal thresholds to
self-relative z-score deviations from the agent's own behavioral baseline.
Absolute safety floors always apply regardless of baseline.

Self-relative deviation risk is gated by absolute basin health (issue #689):
inside the healthy basin a deviation from your own norm is information, not
danger, so it raises no risk; outside the basin the gate opens and the absolute
floors fire. This replaces the flat MIN_MEANINGFUL_EISV_STD σ-floor as the
principled fix for the 2026-06-13 ultra-stable-agent false-pause (#686).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from src.behavioral_state import BehavioralEISV


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

# Absolute safety floors — always active, override baseline.
# These catch states that are genuinely dangerous regardless of an agent's
# characteristic operating point. Set between "extreme" and the fixed-
# threshold triggers (0.4 for E/I, 0.5 for S) to provide meaningful
# backstop for agents whose baseline normalizes persistently bad states.
ABSOLUTE_E_FLOOR = 0.30
ABSOLUTE_I_FLOOR = 0.30
ABSOLUTE_S_CEILING = 0.70
ABSOLUTE_V_CEILING = 0.50

# --- Absolute-basin-health gate edges (issue #689) ----------------------------
# Self-relative z-deviation risk is gated by how far the ABSOLUTE EISV value sits
# between the healthy-basin edge and the absolute danger edge. Inside the healthy
# basin "you moved from your own norm" is information, not danger, so the gate is
# 0 and self-relative deviations contribute no risk; as a dimension leaves the
# basin toward its absolute floor the gate ramps 0→1, restoring full self-relative
# sensitivity exactly where it matters (and where σ resolution is meaningful).
#
# Healthy edge = BASIN_HIGH per-dimension EISV bounds. These MIRROR
# config.governance_config.BASIN_HIGH; they are duplicated here (rather than
# imported) to keep this scoring module free of the numpy/config import chain on
# the hot path. Parity is drift-guarded by
# tests/test_stable_agent_risk_calibration.py::test_basin_gate_edges_match_config.
# Danger edge = the ABSOLUTE_* floors/ceilings above.
#
# This is the principled replacement for the flat MIN_MEANINGFUL_EISV_STD floor:
# it does not touch σ at all, so it never blunts the meaningful variance of a
# genuinely unstable agent, and it sidesteps the EMA double-smoothing artifact
# (a tight σ inside the basin no longer matters because the gate is 0 there).
BASIN_E_HEALTHY = 0.60   # == BASIN_HIGH.E_min
BASIN_I_HEALTHY = 0.70   # == BASIN_HIGH.I_min
BASIN_S_HEALTHY = 0.25   # == BASIN_HIGH.S_max
BASIN_V_HEALTHY = 0.15   # == BASIN_HIGH.V_abs_max

# Sigma thresholds for self-relative scoring
SIGMA_MILD = 1.5      # noticeably different from self
SIGMA_MODERATE = 2.0   # concerning
SIGMA_SEVERE = 3.0     # severe deviation


def _basin_health_gate(state: BehavioralEISV) -> Dict[str, float]:
    """Per-dimension absolute-health gate in [0, 1] for self-relative risk.

    0.0 == dimension is inside the healthy basin (deviation is information, not
    danger); 1.0 == dimension has reached its absolute danger edge (full
    self-relative sensitivity). Linear ramp between the basin edge and the
    absolute floor. Keyed by the risk-component name the gate multiplies.

    Uses ONLY the absolute EISV value — never risk or coherence — so there is no
    circularity with the risk score being computed.
    """

    def _floor_gate(value: float, healthy: float, danger: float) -> float:
        # lower-is-worse dimensions (E, I): gate opens as value falls below healthy
        if value >= healthy:
            return 0.0
        if value <= danger:
            return 1.0
        return (healthy - value) / (healthy - danger)

    def _ceiling_gate(value: float, healthy: float, danger: float) -> float:
        # higher-is-worse dimensions (S, |V|): gate opens as value rises above healthy
        if value <= healthy:
            return 0.0
        if value >= danger:
            return 1.0
        return (value - healthy) / (danger - healthy)

    return {
        "low_E": _floor_gate(state.E, BASIN_E_HEALTHY, ABSOLUTE_E_FLOOR),
        "low_I": _floor_gate(state.I, BASIN_I_HEALTHY, ABSOLUTE_I_FLOOR),
        "high_S": _ceiling_gate(state.S, BASIN_S_HEALTHY, ABSOLUTE_S_CEILING),
        "high_V": _ceiling_gate(abs(state.V), BASIN_V_HEALTHY, ABSOLUTE_V_CEILING),
    }


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
    if state.E < 0.4:
        components["low_E"] = 0.30 * (0.4 - state.E) / 0.4
    else:
        components["low_E"] = 0.0

    # --- Component 2: Low Integrity (weight: 0.30) ---
    if state.I < 0.4:
        components["low_I"] = 0.30 * (0.4 - state.I) / 0.4
    else:
        components["low_I"] = 0.0

    # --- Component 3: High Entropy (weight: 0.20) ---
    s_threshold = 0.5
    task_type = ctx.get("task_type", "mixed")
    if task_type == "convergent":
        s_threshold = 0.6
    if state.S > s_threshold:
        components["high_S"] = 0.20 * min(1.0, (state.S - s_threshold) / (1.0 - s_threshold))
    else:
        components["high_S"] = 0.0

    # --- Component 4: High |V| imbalance (weight: 0.20) ---
    abs_v = abs(state.V)
    if abs_v > 0.15:
        components["high_V"] = 0.20 * min(1.0, (abs_v - 0.15) / 0.85)
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

    Each EISV-derived component is then multiplied by an absolute-basin-health
    gate (issue #689): inside the healthy basin the gate is 0, so a deviation
    from your own norm raises no risk; as a dimension leaves the basin toward its
    absolute floor the gate ramps 0→1, restoring full self-relative sensitivity.
    The rho and continuity-energy components are absolute signals (not baseline-
    relative) and are intentionally NOT gated.

    NOTE: a gated-to-0 component here does NOT mean the dimension cannot raise
    risk — ``assess_behavioral_state`` takes ``max()`` of this result with
    ``_score_absolute_floors`` per component, so the absolute floors remain a
    hard backstop regardless of the gate.
    """
    components: Dict[str, float] = {}
    gate = _basin_health_gate(state)

    # --- Component 1: E deviation below baseline (weight: 0.30) ---
    z_E = state.deviation("E")
    if z_E < -SIGMA_MILD:
        severity = min(1.0, (-z_E - SIGMA_MILD) / (SIGMA_SEVERE - SIGMA_MILD))
        components["low_E"] = 0.30 * severity * gate["low_E"]
    else:
        components["low_E"] = 0.0

    # --- Component 2: I deviation below baseline (weight: 0.30) ---
    z_I = state.deviation("I")
    if z_I < -SIGMA_MILD:
        severity = min(1.0, (-z_I - SIGMA_MILD) / (SIGMA_SEVERE - SIGMA_MILD))
        components["low_I"] = 0.30 * severity * gate["low_I"]
    else:
        components["low_I"] = 0.0

    # --- Component 3: S deviation above baseline (weight: 0.20) ---
    z_S = state.deviation("S")
    task_type = ctx.get("task_type", "mixed")
    sigma_threshold = SIGMA_MILD
    if task_type == "convergent":
        sigma_threshold = SIGMA_MODERATE
    if z_S > sigma_threshold:
        severity = min(1.0, (z_S - sigma_threshold) / (SIGMA_SEVERE - sigma_threshold))
        components["high_S"] = 0.20 * severity * gate["high_S"]
    else:
        components["high_S"] = 0.0

    # --- Component 4: |V| deviation above baseline (weight: 0.20) ---
    z_V = state.deviation("V")
    if abs(z_V) > SIGMA_MILD:
        severity = min(1.0, (abs(z_V) - SIGMA_MILD) / (SIGMA_SEVERE - SIGMA_MILD))
        components["high_V"] = 0.20 * severity * gate["high_V"]
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
