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

DECISION_SELF_LOOP_ABLATION_SCHEMA = "decision_self_loop_ablation.v1"
BEHAVIORAL_SENSOR_COMPONENTS_SCHEMA = "behavioral_sensor.components.v1"
# The neutral stand-in is the OBSERVED level, not the score table's midpoint.
# Measured 2026-08-20 over 30d (n=33,379 non-synthetic): guide 52.7% -> 0.7,
# approve 47.2% -> 1.0, and the 0.0-scored pause vocabulary is 0.12% of rows.
# So the fleet's realised decision_e sits near 0.84. Using the table midpoint
# instead would inject a level shift of its own and make the ablation's delta
# unreadable -- the question here is whether decision_e carries INFORMATION,
# not what happens if you move everyone's level.
DECISION_NEUTRAL_SCORE = 0.84


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
    components = compute_behavioral_sensor_components(
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
    if components is None:
        return None
    return {
        dimension: components["dimensions"][dimension]["value"]
        for dimension in ("E", "I", "S", "V")
    }


def compute_behavioral_sensor_components(
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
    """Explain the exact deployed behavioral observation without changing it.

    The returned values are the same values ``compute_behavioral_sensor_eisv``
    publishes.  Components and ordered adjustments make mixed provenance,
    defaults, and weights inspectable.  This record is measurement-only and is
    never consumed by policy.
    """
    del S_history, V_history  # accepted for signature parity; formulas do not read them
    if len(decision_history) < 3 or len(coherence_history) < 3:
        return None

    dimensions = {
        "E": _compute_E_components(
            decision_history,
            coherence_history,
            complexity_divergence,
            outcome_history,
        ),
        "I": _compute_I_components(
            coherence_history,
            calibration_error,
            outcome_history,
        ),
        "S": _compute_S_components(
            drift_norm,
            regime_history,
            complexity_divergence,
        ),
        "V": _compute_V_components(E_history, I_history),
    }
    for dimension in ("E", "I", "S"):
        dimensions[dimension]["measurement_role"] = "behavioral_sensor_input"
        dimensions[dimension]["behavioral_state_consumed"] = True
    # GovernanceMonitor passes only E/I/S into BehavioralState.update. This V
    # remains part of the full submitted sensor (diagnostic/divergence and,
    # when configured, ODE coupling) but does not produce behavioral-state V;
    # that live value is an EMA of the raw E-I imbalance.
    dimensions["V"]["measurement_role"] = "sensor_diagnostic"
    dimensions["V"]["behavioral_state_consumed"] = False

    def blend(dimension: str, name: str, source: str, value: float, weight: float) -> None:
        record = dimensions[dimension]
        before = record["value"]
        after = (1.0 - weight) * before + weight * value
        record["adjustments"].append({
            "name": name,
            "source": source,
            "input_value": value,
            "input_weight": weight,
            "retained_weight": 1.0 - weight,
            "output_value": after,
            "observed": True,
        })
        record["value"] = after

    # Preserve the deployed adjustment order exactly.
    if continuity_E_input is not None:
        blend("E", "continuity_E_input", "response_structure", continuity_E_input, 0.20)
    if continuity_I_input is not None:
        blend("I", "continuity_I_input", "response_structure", continuity_I_input, 0.20)
    if continuity_S_input is not None:
        blend("S", "continuity_S_input", "response_structure", continuity_S_input, 0.20)
    if tool_error_rate is not None:
        blend("E", "tool_success_rate", "tool_audit", 1.0 - tool_error_rate, 0.15)
    if tool_call_velocity is not None:
        before = dimensions["S"]["value"]
        velocity_pressure = min(1.0, max(0.0, tool_call_velocity - 5.0) / 10.0)
        after = before + 0.10 * velocity_pressure
        dimensions["S"]["adjustments"].append({
            "name": "tool_velocity_pressure",
            "source": "tool_audit",
            "input_value": velocity_pressure,
            "input_weight": 0.10,
            "retained_weight": 1.0,
            "output_value": after,
            "observed": True,
        })
        dimensions["S"]["value"] = after
    if unique_tools_ratio is not None:
        blend("I", "unique_tools_ratio", "tool_audit", unique_tools_ratio, 0.10)

    return {
        "schema": BEHAVIORAL_SENSOR_COMPONENTS_SCHEMA,
        "mode": "measurement_only",
        "policy_applied": False,
        "dimensions": dimensions,
    }


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


def compute_decision_self_loop_shadow(
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
    """Shadow the behavioral E/I reading with ``decision_e`` neutralized.

    ``decision_history`` is populated solely from ``governance_monitor``'s own
    verdicts, and ``decision_e`` is the largest single term in E (0.35 with
    outcomes, 0.40 without). The source-level cycle is real:
    E -> basin -> guide -> decision_e -> E.

    ⛔WHAT THIS CANNOT ANSWER, stated first because the original version of this
    docstring claimed otherwise. It does NOT measure closed-loop gain.
    0.35/0.40 are MIXTURE WEIGHTS, not a gain. This function deliberately
    excludes EMA replay, V, policy effects and future outcomes (see
    ``not_modeled``), so by construction it produces a ONE-STEP LEVEL DELTA.
    And because the decision vocabulary is categorical, local gain is zero away
    from the score thresholds and discontinuous at them, so a single scalar
    "gain" is the wrong object regardless of how it is estimated. ⛔Do not quote
    any number derived from this as a loop gain, and note that a sub-unity gain
    would describe ordinary stable recurrence rather than a defect anyway.

    WHAT IT DOES ANSWER: whether the channel is EXCITED. Measured over 30d, the
    per-agent decision vocabulary has p50 of 1 distinct value and p95 of 2 (408
    of 613 agents carry exactly one), so for most agents the term is an
    intercept -- topologically present, empirically unexcited. A near-constant
    ``candidate_minus_deployed`` with small residual spread confirms that
    reading and points at calibration; a delta that moves with the agent's own
    trajectory shows the channel is at least transmitting, which is a
    precondition for any structural claim but not evidence of one. That is why
    this emits per-check-in deltas rather than a summary.

    Measurement-only, exactly like the legacy-coherence sibling: the live
    observation is never modified and no policy reads this. Stops at the raw
    behavioral E/I observation for the same reason -- replaying recursive E/I
    history, the EMA, V, or policy needs a longitudinal simulator this is not.

    ``DECISION_NEUTRAL_SCORE`` is the observed fleet level rather than the score
    table's midpoint, so the delta reads as "what does removing the information
    do" and not "what does moving everyone's level do".
    """
    base = {
        "schema": DECISION_SELF_LOOP_ABLATION_SCHEMA,
        "mode": "measurement_only",
        "policy_applied": False,
        "intervention": {
            "field": "decision_history",
            "source": "governance_monitor.decision_history",
            "role": "observer_own_prior_verdicts",
            "operation": "replace_with_observed_fleet_level",
            "replacement_value": DECISION_NEUTRAL_SCORE,
            "deployed_weight_with_outcomes": 0.35,
            "deployed_weight_without_outcomes": 0.40,
        },
        "not_modeled": [
            "recursive_E_I_history_replay",
            "behavioral_ema_replay",
            "V_counterfactual",
            "policy_effect",
            "future_outcomes",
        ],
        "interpretation": (
            "one-step level delta, NOT loop gain. near-constant with small "
            "residual spread => the channel is an unexcited intercept (points "
            "at _DECISION_SCORES calibration); delta moving with the agent's "
            "trajectory => the channel transmits, which is a precondition for "
            "a structural claim and not evidence of one"
        ),
        "cannot_measure": [
            "closed_loop_gain",
            "cross_checkin_propagation",
            "basin_boundary_flip_counterfactual",
        ],
    }
    if len(decision_history) < 3:
        return {
            **base,
            "eligible": False,
            "eligibility_reason": "insufficient_decision_history",
            "deployed": None,
            "candidate": None,
            "candidate_minus_deployed": None,
        }

    shared = dict(
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
    deployed = deployed_observation or compute_behavioral_sensor_eisv(
        decision_history=decision_history, **shared
    )
    # A sentinel the score table maps to DECISION_NEUTRAL_SCORE, so the EWA sees
    # a flat series. Substituting the numeric score directly would not work:
    # _compute_E looks decisions up by string.
    neutral_history = [_DECISION_NEUTRAL_KEY] * min(len(decision_history), 10)
    candidate = compute_behavioral_sensor_eisv(
        decision_history=neutral_history, **shared
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
# Ablation-only sentinel. Never emitted by monitor_decision.py; it exists so
# compute_decision_self_loop_shadow can feed _compute_E a flat decision series
# through the same string-lookup path the real vocabulary uses.
_DECISION_NEUTRAL_KEY = "__ablation_neutral__"

_DECISION_SCORES = {
    _DECISION_NEUTRAL_KEY: DECISION_NEUTRAL_SCORE,
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
    return _compute_E_components(
        decision_history,
        coherence_history,
        complexity_divergence,
        outcome_history,
    )["value"]


def _component(
    name: str,
    source: str,
    value: float,
    weight: float,
    *,
    observed: bool,
    default_reason: str | None = None,
    health_evidence: bool | None = None,
) -> dict:
    record = {
        "name": name,
        "source": source,
        "value": value,
        "weight": weight,
        "weighted_contribution": weight * value,
        "observed": observed,
    }
    if default_reason is not None:
        record["default_reason"] = default_reason
    if health_evidence is not None:
        record["health_evidence"] = health_evidence
    return record


def _compute_E_components(
    decision_history: list,
    coherence_history: list | None = None,
    complexity_divergence: float | None = None,
    outcome_history: list | None = None,
) -> dict:
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
        components = [
            _component(
                "decision_history", "governance_decisions", decision_e, 0.35,
                observed=bool(window),
                default_reason=None if window else "no_decision_history",
                health_evidence=False,
            ),
            _component(
                "legacy_coherence_level", "legacy_tanh_v", coh_e, 0.25,
                observed=bool(coherence_history and len(coherence_history) >= 3),
                default_reason=(
                    None if coherence_history and len(coherence_history) >= 3
                    else "insufficient_coherence_history"
                ),
                health_evidence=False,
            ),
            _component(
                "complexity_calibration", "continuity_metrics", cal_e, 0.20,
                observed=complexity_divergence is not None,
                default_reason=(
                    None if complexity_divergence is not None
                    else "default_complexity_divergence_0.15"
                ),
            ),
            _component(
                "outcome_success", "recent_outcomes", outcome_e, 0.20,
                observed=True,
            ),
        ]
    else:
        # Without outcomes: 40% decision, 30% coherence, 30% calibration (original)
        components = [
            _component(
                "decision_history", "governance_decisions", decision_e, 0.40,
                observed=bool(window),
                default_reason=None if window else "no_decision_history",
                health_evidence=False,
            ),
            _component(
                "legacy_coherence_level", "legacy_tanh_v", coh_e, 0.30,
                observed=bool(coherence_history and len(coherence_history) >= 3),
                default_reason=(
                    None if coherence_history and len(coherence_history) >= 3
                    else "insufficient_coherence_history"
                ),
                health_evidence=False,
            ),
            _component(
                "complexity_calibration", "continuity_metrics", cal_e, 0.30,
                observed=complexity_divergence is not None,
                default_reason=(
                    None if complexity_divergence is not None
                    else "default_complexity_divergence_0.15"
                ),
            ),
        ]

    # Keep the deployed left-to-right arithmetic order bit-for-bit. ``sum``
    # may use compensated summation on newer Python runtimes.
    if len(components) == 4:
        raw = (
            components[0]["weighted_contribution"]
            + components[1]["weighted_contribution"]
            + components[2]["weighted_contribution"]
            + components[3]["weighted_contribution"]
        )
    else:
        raw = (
            components[0]["weighted_contribution"]
            + components[1]["weighted_contribution"]
            + components[2]["weighted_contribution"]
        )
    value = max(0.0, min(1.0, raw))
    return {
        "value": value,
        "base_value": value,
        "components": components,
        "adjustments": [],
    }


# --- I: Calibration accuracy + legacy control-feedback trend ---

def _compute_I(
    coherence_history: list,
    calibration_error: float | None,
    outcome_history: list | None = None,
) -> float:
    return _compute_I_components(
        coherence_history,
        calibration_error,
        outcome_history,
    )["value"]


def _compute_I_components(
    coherence_history: list,
    calibration_error: float | None,
    outcome_history: list | None = None,
) -> dict:
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
            components = [
                _component(
                    "calibration_accuracy", "fleet_calibration", cal_I, 0.50,
                    observed=calibration_error is not None,
                    default_reason=(
                        None if calibration_error is not None
                        else "default_calibration_accuracy_0.75"
                    ),
                ),
                _component(
                    "legacy_coherence_trend", "legacy_tanh_v", coh_I, 0.30,
                    observed=len(coherence_history) >= 4,
                    default_reason=(
                        None if len(coherence_history) >= 4
                        else "insufficient_coherence_history"
                    ),
                    health_evidence=False,
                ),
                _component(
                    "outcome_score_consistency", "recent_outcomes", consistency_I, 0.20,
                    observed=True,
                    health_evidence=False,
                ),
            ]
            raw = (
                components[0]["weighted_contribution"]
                + components[1]["weighted_contribution"]
                + components[2]["weighted_contribution"]
            )
            value = max(0.0, min(1.0, raw))
            return {
                "value": value,
                "base_value": value,
                "components": components,
                "adjustments": [],
            }

    components = [
        _component(
            "calibration_accuracy", "fleet_calibration", cal_I, 0.60,
            observed=calibration_error is not None,
            default_reason=(
                None if calibration_error is not None
                else "default_calibration_accuracy_0.75"
            ),
        ),
        _component(
            "legacy_coherence_trend", "legacy_tanh_v", coh_I, 0.40,
            observed=len(coherence_history) >= 4,
            default_reason=(
                None if len(coherence_history) >= 4
                else "insufficient_coherence_history"
            ),
            health_evidence=False,
        ),
    ]
    raw = (
        components[0]["weighted_contribution"]
        + components[1]["weighted_contribution"]
    )
    value = max(0.0, min(1.0, raw))
    return {
        "value": value,
        "base_value": value,
        "components": components,
        "adjustments": [],
    }


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
    return _compute_S_components(
        drift_norm,
        regime_history,
        complexity_divergence,
    )["value"]


def _compute_S_components(
    drift_norm: float | None,
    regime_history: list,
    complexity_divergence: float | None,
) -> dict:
    # Drift component (40%)
    dn = drift_norm if drift_norm is not None else 0.2
    drift_s = min(1.0, dn * 1.5)

    # Regime instability (35%): count transitions / window
    regime_s = _regime_instability(regime_history)

    # Complexity divergence (25%)
    cd = complexity_divergence if complexity_divergence is not None else 0.1
    cd_s = min(1.0, cd)

    components = [
        _component(
            "drift_norm", "governance_drift", drift_s, 0.40,
            observed=drift_norm is not None,
            default_reason=None if drift_norm is not None else "default_drift_norm_0.2",
        ),
        _component(
            "regime_instability", "regime_history", regime_s, 0.35,
            observed=len(regime_history) >= 2,
            default_reason=(
                None if len(regime_history) >= 2
                else "insufficient_regime_history"
            ),
        ),
        _component(
            "complexity_divergence", "continuity_metrics", cd_s, 0.25,
            observed=complexity_divergence is not None,
            default_reason=(
                None if complexity_divergence is not None
                else "default_complexity_divergence_0.1"
            ),
        ),
    ]
    raw = (
        components[0]["weighted_contribution"]
        + components[1]["weighted_contribution"]
        + components[2]["weighted_contribution"]
    )
    value = max(0.05, min(1.0, raw))
    return {
        "value": value,
        "base_value": value,
        "components": components,
        "adjustments": [],
    }


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
    return _compute_V_components(E_history, I_history)["value"]


def _compute_V_components(E_history: list, I_history: list) -> dict:
    window = 10
    e_win = E_history[-window:]
    i_win = I_history[-window:]

    if len(e_win) < 3 or len(i_win) < 3:
        components = [
            _component(
                "E_minus_I_slope", "per_checkin_history", 0.0, 0.60,
                observed=False,
                default_reason="insufficient_E_I_history",
            ),
            _component(
                "E_minus_I_level", "per_checkin_history", 0.0, 0.40,
                observed=False,
                default_reason="insufficient_E_I_history",
            ),
        ]
        return {
            "value": 0.0,
            "base_value": 0.0,
            "components": components,
            "adjustments": [],
        }

    e_slope = _simple_slope(e_win)
    i_slope = _simple_slope(i_win)
    trend = e_slope - i_slope

    # Instantaneous E-I gap
    level = e_win[-1] - i_win[-1]

    # 60% trend + 40% level
    components = [
        _component(
            "E_minus_I_slope", "per_checkin_history", trend, 0.60,
            observed=True,
        ),
        _component(
            "E_minus_I_level", "per_checkin_history", level, 0.40,
            observed=True,
        ),
    ]
    raw = (
        components[0]["weighted_contribution"]
        + components[1]["weighted_contribution"]
    )
    value = max(-1.0, min(1.0, raw))
    return {
        "value": value,
        "base_value": value,
        "components": components,
        "adjustments": [],
        "details": {
            "E_slope_per_checkin": e_slope,
            "I_slope_per_checkin": i_slope,
            "cadence_normalized": False,
        },
    }


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
