"""Compatibility behavioral sensor for non-embodied agents.

Pure function — no imports from governance modules. Takes extracted history lists
and returns an EISV dict suitable for spring coupling in the ODE.

Despite the historical name, this is not fully behavior-only: the deployed
``coherence_history`` contains legacy ``C(V_ODE)`` directional control feedback.
It contributes 25–30% of E and 30–40% of I. Keep that causal dependency visible
in telemetry and do not replace/reweight it without shadowing the resulting E/I
distribution and downstream baselines first.
"""

import math


LEGACY_COHERENCE_ABLATION_SCHEMA = "legacy_coherence_dependency_ablation.v1"
LEGACY_COHERENCE_NEUTRAL_VALUE = 0.5
LEGACY_COHERENCE_NEUTRAL_COMPONENT = 0.6


def compute_behavioral_sensor_eisv(
    decision_history: list,
    coherence_history: list,
    regime_history: list,
    E_history: list,
    I_history: list,
    S_history: list,
    V_history: list,
    calibration_error: float | None = None,
    drift_norm: float | None = None,
    complexity_divergence: float | None = None,
    continuity_E_input: float | None = None,
    continuity_I_input: float | None = None,
    continuity_S_input: float | None = None,
    outcome_history: list | None = None,
    tool_error_rate: float | None = None,
    tool_call_velocity: float | None = None,
    unique_tools_ratio: float | None = None,
) -> dict | None:
    """Compute the deployed mixed-provenance EISV observation.

    Returns {"E", "I", "S", "V"} dict or None if insufficient history (< 3 entries).
    """
    if len(decision_history) < 3 or len(coherence_history) < 3:
        return None

    E = _compute_E(decision_history, coherence_history, complexity_divergence, outcome_history)
    I = _compute_I(coherence_history, calibration_error, outcome_history)
    S = _compute_S(drift_norm, regime_history, complexity_divergence)
    V = _compute_V(E_history, I_history)

    # Blend continuity-derived signals (20% weight) when available.
    # These are grounded in operational log analysis (token rates, divergence).
    if continuity_E_input is not None:
        E = 0.80 * E + 0.20 * continuity_E_input
    if continuity_I_input is not None:
        I = 0.80 * I + 0.20 * continuity_I_input
    if continuity_S_input is not None:
        S = 0.80 * S + 0.20 * continuity_S_input

    # Blend tool usage signals (10-15% weight) when available.
    # These are grounded in actual tool call outcomes, not self-reports.
    if tool_error_rate is not None:
        E = 0.85 * E + 0.15 * (1.0 - tool_error_rate)
    if tool_call_velocity is not None:
        # Velocity > 5 calls/min adds mild entropy (capped at 0.10 contribution)
        S = S + 0.10 * min(1.0, max(0.0, tool_call_velocity - 5.0) / 10.0)
    if unique_tools_ratio is not None:
        I = 0.90 * I + 0.10 * unique_tools_ratio

    return {"E": E, "I": I, "S": S, "V": V}


def compute_legacy_coherence_dependency_shadow(
    decision_history: list,
    coherence_history: list,
    regime_history: list,
    E_history: list,
    I_history: list,
    S_history: list,
    V_history: list,
    calibration_error: float | None = None,
    drift_norm: float | None = None,
    complexity_divergence: float | None = None,
    continuity_E_input: float | None = None,
    continuity_I_input: float | None = None,
    continuity_S_input: float | None = None,
    outcome_history: list | None = None,
    tool_error_rate: float | None = None,
    tool_call_velocity: float | None = None,
    unique_tools_ratio: float | None = None,
    deployed_observation: dict | None = None,
) -> dict:
    """Shadow the behavioral E/I reading with legacy coherence neutralized.

    This is a measurement-only intervention.  It replaces the bounded history
    of legacy ``C(V_ODE)`` values with the transfer function's midpoint (0.5),
    which maps to the existing neutral E-level and I-trend component (0.6).
    All other inputs and all deployed weights remain identical.  The live
    observation is never changed.

    The candidate intentionally stops at the raw behavioral E/I observation.
    Replaying recursive E/I history, the behavioral EMA, V, policy, or later
    outcomes would require a separate longitudinal simulator and is not implied
    by this per-check-in shadow.
    """
    base = {
        "schema": LEGACY_COHERENCE_ABLATION_SCHEMA,
        "mode": "measurement_only",
        "policy_applied": False,
        "intervention": {
            "field": "coherence_history",
            "source": "legacy_tanh_v",
            "role": "ode_control_feedback",
            "operation": "replace_with_transfer_midpoint",
            "replacement_value": LEGACY_COHERENCE_NEUTRAL_VALUE,
            "mapped_E_level_component": LEGACY_COHERENCE_NEUTRAL_COMPONENT,
            "mapped_I_trend_component": LEGACY_COHERENCE_NEUTRAL_COMPONENT,
        },
        "not_modeled": [
            "recursive_E_I_history_replay",
            "behavioral_ema_replay",
            "V_counterfactual",
            "policy_effect",
            "future_outcomes",
        ],
    }
    if len(decision_history) < 3 or len(coherence_history) < 3:
        return {
            **base,
            "eligible": False,
            "eligibility_reason": "insufficient_behavioral_history",
            "deployed": None,
            "candidate": None,
            "candidate_minus_deployed": None,
        }

    deployed = deployed_observation or compute_behavioral_sensor_eisv(
        decision_history=decision_history,
        coherence_history=coherence_history,
        regime_history=regime_history,
        E_history=E_history,
        I_history=I_history,
        S_history=S_history,
        V_history=V_history,
        calibration_error=calibration_error,
        drift_norm=drift_norm,
        complexity_divergence=complexity_divergence,
        continuity_E_input=continuity_E_input,
        continuity_I_input=continuity_I_input,
        continuity_S_input=continuity_S_input,
        outcome_history=outcome_history,
        tool_error_rate=tool_error_rate,
        tool_call_velocity=tool_call_velocity,
        unique_tools_ratio=unique_tools_ratio,
    )
    neutral_history = [LEGACY_COHERENCE_NEUTRAL_VALUE] * min(
        len(coherence_history), 10
    )
    candidate = compute_behavioral_sensor_eisv(
        decision_history=decision_history,
        coherence_history=neutral_history,
        regime_history=regime_history,
        E_history=E_history,
        I_history=I_history,
        S_history=S_history,
        V_history=V_history,
        calibration_error=calibration_error,
        drift_norm=drift_norm,
        complexity_divergence=complexity_divergence,
        continuity_E_input=continuity_E_input,
        continuity_I_input=continuity_I_input,
        continuity_S_input=continuity_S_input,
        outcome_history=outcome_history,
        tool_error_rate=tool_error_rate,
        tool_call_velocity=tool_call_velocity,
        unique_tools_ratio=unique_tools_ratio,
    )
    if deployed is None or candidate is None:
        return {
            **base,
            "eligible": False,
            "eligibility_reason": "behavioral_sensor_unavailable",
            "deployed": None,
            "candidate": None,
            "candidate_minus_deployed": None,
        }

    deployed_ei = {key: float(deployed[key]) for key in ("E", "I")}
    candidate_ei = {key: float(candidate[key]) for key in ("E", "I")}
    return {
        **base,
        "eligible": True,
        "eligibility_reason": None,
        "deployed": deployed_ei,
        "candidate": candidate_ei,
        "candidate_minus_deployed": {
            key: candidate_ei[key] - deployed_ei[key] for key in ("E", "I")
        },
    }


# --- E: Decision success rate, exponentially weighted ---

# What actually lands in ``decision_history`` is the SUB-action when one exists:
# ``governance_monitor.py`` appends ``decision.get('sub_action', decision['action'])``.
# So the strings arriving here are the sub_action vocabulary from
# ``monitor_decision.py``, not the coarse action names — and every pause/block
# variant was missing from this table, falling through to the 0.5 default.
#
# That is the wrong direction for exactly the wrong event: a pause is the rare,
# high-signal state, and scoring it 0.5 told _compute_E that nothing notable
# happened. The bare "pause"/"reject" keys were unreachable through this path.
#
# Keep this table in sync with the ``'sub_action':`` literals in
# monitor_decision.py — tests/test_behavioral_sensor_decision_coverage.py fails
# if a new one is added without a score here.
_DECISION_SCORES = {
    # coarse actions (still used when a decision carries no sub_action)
    "proceed": 1.0, "approve": 1.0,
    "guide": 0.7,
    "revise": 0.5, "reflect": 0.5,
    "pause": 0.0, "reject": 0.0,
    # sub_actions actually emitted by monitor_decision.py
    "risk_pause": 0.0,
    "basin_pause": 0.0,
    "coherence_pause": 0.0,
    "void_pause": 0.0,
    "cirs_block": 0.0,
}


def _compute_E(
    decision_history: list,
    coherence_history: list | None = None,
    complexity_divergence: float | None = None,
    outcome_history: list | None = None,
) -> float:
    """E from decisions, legacy control-feedback level, calibration, and outcomes.

    Pure decision-based E saturates at 1.0 for healthy agents (all "proceed").
    Blending with coherence, calibration, and outcomes makes E reflect actual capacity.
    """
    # Decision success — exponentially weighted
    window = decision_history[-10:]
    if not window:
        decision_e = 0.65
    else:
        n = len(window)
        alpha = 0.3
        weights = [math.exp(alpha * (i - n + 1)) for i in range(n)]
        total_w = sum(weights)
        decision_e = sum(
            w * _DECISION_SCORES.get(str(d).lower(), 0.5)
            for w, d in zip(weights, window)
        ) / total_w

    # Legacy C(V_ODE) controller level — map [0.35, 0.65] → [0.3, 0.9].
    # ``coherence_history`` is retained as the compatibility parameter name.
    if coherence_history and len(coherence_history) >= 3:
        recent_coh = coherence_history[-10:]
        mean_coh = sum(recent_coh) / len(recent_coh)
        coh_e = 0.3 + (mean_coh - 0.35) * 2.0  # 0.35→0.3, 0.65→0.9
        coh_e = max(0.3, min(0.9, coh_e))
    else:
        coh_e = 0.6

    # Complexity calibration — low divergence = high capacity awareness
    cd = complexity_divergence if complexity_divergence is not None else 0.15
    cal_e = max(0.3, min(1.0, 1.0 - cd))

    # Outcome success rate — successful outcomes indicate productive energy
    if outcome_history and len(outcome_history) >= 3:
        good_count = sum(1 for o in outcome_history if not o.get('is_bad', False))
        success_rate = good_count / len(outcome_history)
        outcome_e = 0.3 + success_rate * 0.6  # Map [0,1] -> [0.3, 0.9]
        # Weights: 35% decision, 25% coherence, 20% calibration, 20% outcomes
        raw = 0.35 * decision_e + 0.25 * coh_e + 0.20 * cal_e + 0.20 * outcome_e
    else:
        # Without outcomes: 40% decision, 30% coherence, 30% calibration (original)
        raw = 0.40 * decision_e + 0.30 * coh_e + 0.30 * cal_e

    return max(0.0, min(1.0, raw))


# --- I: Calibration accuracy + legacy control-feedback trend ---

def _compute_I(
    coherence_history: list,
    calibration_error: float | None,
    outcome_history: list | None = None,
) -> float:
    cal_I = 1.0 - calibration_error if calibration_error is not None else 0.75
    cal_I = max(0.0, min(1.0, cal_I))

    coh_I = _coherence_trend(coherence_history)

    # Outcome consistency — consistent scores indicate information integrity
    if outcome_history and len(outcome_history) >= 3:
        scores = [s for o in outcome_history
                  if (s := o.get('outcome_score')) is not None
                  and isinstance(s, (int, float)) and math.isfinite(s)]
        if len(scores) >= 3:
            mean_s = sum(scores) / len(scores)
            score_var = sum((s - mean_s) ** 2 for s in scores) / len(scores)
            consistency_I = max(0.3, 1.0 - score_var * 4)  # Low variance = high consistency
            # Weights: 50% calibration, 30% coherence trend, 20% outcome consistency
            return max(0.0, min(1.0, 0.50 * cal_I + 0.30 * coh_I + 0.20 * consistency_I))

    return max(0.0, min(1.0, 0.6 * cal_I + 0.4 * coh_I))


def _coherence_trend(coherence_history: list) -> float:
    """Split-half legacy C(V_ODE) trend mapped to [0.3, 0.9]."""
    window = coherence_history[-10:]
    if len(window) < 4:
        return 0.6  # neutral default

    mid = len(window) // 2
    first_half = sum(window[:mid]) / mid
    second_half = sum(window[mid:]) / (len(window) - mid)

    # Positive diff = improving, negative = declining
    diff = second_half - first_half
    # Map diff from [-0.1, 0.1] to [0.3, 0.9]
    mapped = 0.6 + diff * 3.0
    return max(0.3, min(0.9, mapped))


# --- S: Entropy from drift, regime instability, complexity divergence ---

def _compute_S(
    drift_norm: float | None,
    regime_history: list,
    complexity_divergence: float | None,
) -> float:
    # Drift component (40%)
    dn = drift_norm if drift_norm is not None else 0.2
    drift_s = min(1.0, dn * 1.5)

    # Regime instability (35%): count transitions / window
    regime_s = _regime_instability(regime_history)

    # Complexity divergence (25%)
    cd = complexity_divergence if complexity_divergence is not None else 0.1
    cd_s = min(1.0, cd)

    raw = 0.40 * drift_s + 0.35 * regime_s + 0.25 * cd_s
    return max(0.05, min(1.0, raw))


def _regime_instability(regime_history: list) -> float:
    """Count regime transitions normalized by window size."""
    window = regime_history[-10:]
    if len(window) < 2:
        return 0.1  # default low instability

    transitions = sum(
        1 for i in range(1, len(window)) if window[i] != window[i - 1]
    )
    return min(1.0, transitions / (len(window) - 1))


# --- V: E-I trajectory slope difference ---

def _compute_V(E_history: list, I_history: list) -> float:
    """V from E-I slope difference. Does NOT read V_history."""
    window = 10
    e_win = E_history[-window:]
    i_win = I_history[-window:]

    if len(e_win) < 3 or len(i_win) < 3:
        return 0.0

    e_slope = _simple_slope(e_win)
    i_slope = _simple_slope(i_win)
    trend = e_slope - i_slope

    # Instantaneous E-I gap
    level = e_win[-1] - i_win[-1]

    # 60% trend + 40% level
    v = 0.6 * trend + 0.4 * level
    return max(-1.0, min(1.0, v))


def _simple_slope(values: list) -> float:
    """Least-squares slope over an index sequence."""
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den == 0:
        return 0.0
    return num / den
