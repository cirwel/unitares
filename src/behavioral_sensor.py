"""Compatibility behavioral sensor for non-embodied agents.

Pure function — no imports from governance modules. Takes extracted history lists
and returns an EISV dict suitable for spring coupling in the ODE.

Despite the historical name, this is not fully behavior-only: the deployed
``coherence_history`` contains legacy ``C(V_ODE)`` directional control feedback.
It contributes 25–30% of E and 30–40% of I. Keep that causal dependency visible
in telemetry and do not replace/reweight it without shadowing the resulting E/I
distribution and downstream baselines first.
"""

from collections.abc import Mapping
import math
import os


NEUTRALIZE_SENSOR_SELF_LOOPS_ENV = "UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS"
NEUTRALIZE_SENSOR_SELF_LOOPS_MODE = "renormalized_v2"
PER_AGENT_I_CALIBRATION_ENV = "UNITARES_PER_AGENT_I_CALIBRATION"

SELF_REFERENTIAL_OUTCOME_TYPES = frozenset({"trajectory_validated", "cirs_resonance"})

AGENT_CALIBRATION_POOLING_K = 10.0
MIN_UNPOOLED_AGENT_CALIBRATION_PAIRS = 5


def is_self_referential_outcome(outcome: object) -> bool:
    """Return True when an outcome row is a server-generated trajectory/resonance echo.

    ``_post_update_trajectory`` emits ``trajectory_validated`` rows (scored as
    ``1 / (1 + exp(-10 * (prev_drift_norm - current_drift_norm)))``) and
    ``_post_update_cirs`` emits ``cirs_resonance`` rows from the monitor's own
    internal state, both with ``verification_source = "server_observation"``.
    ``trajectory_validated`` alone accounts for 80.3% of unfiltered outcome rows
    (``docs/evaluations/eisv-ei-referent/result-v0.md``), feeding the observer's
    own drift and oscillation state back into ``E`` (``outcome_success``), ``I``
    (``outcome_score_consistency``), and ``outcomes_in_e`` (#2610, Invariant 4).

    On the live ``get_recent_outcomes`` path (where ``verification_source`` is
    not selected when ``UNITARES_OUTCOME_PROVENANCE_FILTER=off``), rows matching
    ``SELF_REFERENTIAL_OUTCOME_TYPES`` are excluded by ``outcome_type``. When
    ``verification_source`` is present on a caller-supplied row (e.g. in replay
    or direct unit calls), only ``"server_observation"`` rows are excluded.

    In the behavioral sensor, filtering these rows is coupled to
    ``neutralize_self_loops`` (``UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS``) so
    removing echo rows cannot shift weight onto active loop-derived
    ``decision_e`` (0.35 -> 0.40) and ``legacy_coherence_level`` (0.25 -> 0.30)
    when an agent has fewer than 3 non-echo outcomes (see
    ``src/db/mixins/tool_usage.py:get_recent_outcomes``).
    """
    if isinstance(outcome, Mapping):
        outcome_type = outcome.get("outcome_type")
        verification_source = outcome.get("verification_source")
    else:
        try:
            outcome_type = outcome["outcome_type"]
        except (KeyError, TypeError, IndexError):
            outcome_type = None
        try:
            verification_source = outcome["verification_source"]
        except (KeyError, TypeError, IndexError):
            verification_source = None
    if str(outcome_type or "").strip().lower() not in SELF_REFERENTIAL_OUTCOME_TYPES:
        return False
    if verification_source is not None:
        return str(verification_source).strip().lower() == "server_observation"
    return True


def filter_behavioral_outcomes(outcome_history: list | None) -> list | None:
    """Return ``outcome_history`` with self-referential trajectory/resonance rows removed."""
    if outcome_history is None:
        return None
    return [row for row in outcome_history if not is_self_referential_outcome(row)]


def per_agent_i_calibration_enabled() -> bool:
    """Return True when per-agent tactical calibration in behavioral I is enabled.

    Controlled by ``UNITARES_PER_AGENT_I_CALIBRATION`` (default ON; set to
    ``0``/``false``/``no``/``off``/``""`` to restore the legacy shared fleet
    calibration scalar). When enabled (#2818 §3.1 & §3.2):
    - Requires ``agent_calibration_channel`` with ``evidence_status == "available"``
      and ``freshness_status == "recent"`` (rejecting ``"stale"`` and ``"unknown"``).
    - If a frozen reference-cohort ``prior_error`` in ``(0, 1)`` is supplied on
      the channel (§3.2(3)) and ``n_verified_pairs >= 1``, ``calibration_accuracy``
      uses partial-pooled error ``e_pool = (1 - w) * prior_error + w * e_agent``
      with ``w = n / (n + 10)`` (``source="agent_calibration_pooled"``).
    - If no reference-cohort ``prior_error`` is supplied (per §3.2(3)-(4),
      never substituting zero error or the legacy 0.1474 fleet scalar) and
      ``n_verified_pairs >= 5``, uses the agent's own sample-weighted binned ECE
      ``1.0 - e_agent`` (``source="agent_tactical_calibration"``).
    - Otherwise (``n_verified_pairs == 0``, stale/unknown freshness, or ``n < 5``
      without a reference prior), no shared prior enters ``I`` (§3.2 Rule 1);
      base ``I`` uses the calibration-free renormalization (Arm F, §6.4).
    """
    raw = os.environ.get("UNITARES_PER_AGENT_I_CALIBRATION", "1").strip().lower()
    return raw in ("1", "true", "yes", "on")


def neutralize_sensor_self_loops_enabled() -> bool:
    """Return True when controller self-loops in the behavioral sensor are neutralized.

    Controlled by ``UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS`` (default ON; set to
    ``0``/``false``/``no``/``off``/``""`` to restore the legacy self-loop
    terms). When enabled, the self-referential controller terms inside the
    behavioral sensor are excluded from the base formulas and the remaining
    observed base components are renormalized to sum to 1.0 (while the one-step
    shadow ablations ``compute_*_shadow`` continue to use constant substitution
    for single-term level attribution; if no non-loop base input is observed on
    a dimension, its documented default fallback carries 1.0 of the base before
    optional post-base blends):
    - ``decision_history`` and ``legacy_coherence_level`` excluded from base E,
      renormalizing onto ``complexity_calibration`` (0.50) + ``outcome_success``
      (0.50) when both are observed, 1.00 on whichever single term is observed,
      or ``complexity_calibration`` default (0.85, ``observed=False``) when
      neither is observed
    - ``legacy_coherence_trend`` excluded from base I, renormalizing onto
      ``calibration_accuracy`` (5/7) + ``outcome_score_consistency`` (2/7) when
      both are observed, 1.00 on whichever single term is observed, or explicit
      ``uncalibrated_base_fallback`` (0.75, ``observed=False``) when neither
      calibration nor >= 3 outcome scores is observed
    - ``regime_instability`` excluded from base S, renormalizing onto
      ``drift_norm`` (8/13) + ``complexity_divergence`` (5/13) when both are
      observed (or both defaulted), or 1.00 on whichever single term is observed
    - server-generated ``trajectory_validated`` / ``cirs_resonance`` outcome
      echoes excluded from E and I
    """
    raw = os.environ.get("UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS", "1").strip().lower()
    return raw in ("1", "true", "yes", "on")


LEGACY_COHERENCE_ABLATION_SCHEMA = "legacy_coherence_dependency_ablation.v1"
LEGACY_COHERENCE_NEUTRAL_VALUE = 0.5
LEGACY_COHERENCE_NEUTRAL_COMPONENT = 0.6

DECISION_SELF_LOOP_ABLATION_SCHEMA = "decision_self_loop_ablation.v1"
BEHAVIORAL_SENSOR_COMPONENTS_SCHEMA = "behavioral_sensor.components.v1"
CALIBRATION_FREE_I_ABLATION_SCHEMA = "calibration_free_i_ablation.v1"
# The neutral stand-in is the OBSERVED level, not the score table's midpoint.
# Measured 2026-08-20 over 30d (n=33,379 non-synthetic): guide 52.7% -> 0.7,
# approve 47.2% -> 1.0, and the 0.0-scored pause vocabulary is 0.12% of rows.
# So the fleet's realised decision_e sits near 0.84. Using the table midpoint
# instead would inject a level shift of its own and make the ablation's delta
# unreadable -- the question here is whether decision_e carries INFORMATION,
# not what happens if you move everyone's level.
DECISION_NEUTRAL_SCORE = 0.84

REGIME_SELF_LOOP_ABLATION_SCHEMA = "regime_self_loop_ablation.v1"
# The neutral stand-in for the regime-instability term is its OBSERVED fleet
# level, for the same reason as DECISION_NEUTRAL_SCORE: the shadow asks whether
# the term carries information, not what happens if every agent's level moves.
# Measured 2026-10-12 over 30 days of persisted S components (n=18,158 observed
# rows): mean 0.120, p50 0.000, p90 0.346.
REGIME_NEUTRAL_INSTABILITY = 0.12

# outcome_success maps the non-adverse share of recent outcomes onto
# [FLOOR, FLOOR + SPAN] = [0.3, 0.9]. Named so the agent-facing account in
# src/outcomes_in_e.py states the same map instead of a copy of it.
OUTCOME_E_FLOOR = 0.3
OUTCOME_E_SPAN = 0.6


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
    *,
    regime_instability_override: float | None = None,
    neutralize_self_loops: bool | None = None,
    calibration_free_i: bool = False,
    per_agent_i_calibration: bool | None = None,
    agent_calibration_channel: Mapping | None = None,
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
        regime_instability_override=regime_instability_override,
        neutralize_self_loops=neutralize_self_loops,
        calibration_free_i=calibration_free_i,
        per_agent_i_calibration=per_agent_i_calibration,
        agent_calibration_channel=agent_calibration_channel,
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
    *,
    regime_instability_override: float | None = None,
    neutralize_self_loops: bool | None = None,
    calibration_free_i: bool = False,
    per_agent_i_calibration: bool | None = None,
    agent_calibration_channel: Mapping | None = None,
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

    active_neutralize = (
        neutralize_sensor_self_loops_enabled()
        if neutralize_self_loops is None
        else bool(neutralize_self_loops)
    )
    active_per_agent_i = (
        per_agent_i_calibration_enabled()
        if per_agent_i_calibration is None
        else bool(per_agent_i_calibration)
    )

    dimensions = {
        "E": _compute_E_components(
            decision_history,
            coherence_history,
            complexity_divergence,
            outcome_history,
            neutralize_self_loops=active_neutralize,
        ),
        "I": _compute_I_components(
            coherence_history,
            calibration_error,
            outcome_history,
            neutralize_self_loops=active_neutralize,
            calibration_free_i=calibration_free_i,
            per_agent_i_calibration=active_per_agent_i,
            agent_calibration_channel=agent_calibration_channel,
        ),
        "S": _compute_S_components(
            drift_norm,
            regime_history,
            complexity_divergence,
            regime_instability_override=regime_instability_override,
            neutralize_self_loops=active_neutralize,
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


UNOBSERVED_SHARE_SCHEMA = "behavioral_sensor.unobserved_share.v1"
_UNOBSERVED_SHARE_DIMENSIONS = ("E", "I", "S")


def compute_unobserved_share(components: Mapping | None) -> dict | None:
    """Share of each published E/I/S value that rests on a default, not a reading.

    Reads a ``compute_behavioral_sensor_components`` record and, for each of E,
    I and S, returns the fraction of the final value's convex weight carried by
    terms the record itself flags ``observed: False``. A base term's final
    weight is its own weight times every later blend's retained weight; a blend
    adjustment's final weight is its input weight times every later blend's
    retained weight. Additive adjustments (retained weight 1.0) are outside the
    convex combination and are not counted.

    0.0 means every weighted term was supplied; 1.0 means the value is entirely
    defaults. ``mean`` averages the three dimensions. V is omitted: it is
    derived from E and I and has no inputs of its own.

    This is a description of the record, measurement-only. It is a lower bound
    on emptiness: a term flagged observed can still be pinned at a clip bound or
    arrive as the same constant on every check-in, and this function does not
    look across check-ins to see that. It changes no E/I/S/V value and nothing
    in policy reads it.
    """
    if not isinstance(components, Mapping):
        return None
    dimensions = components.get("dimensions")
    if not isinstance(dimensions, Mapping):
        return None

    def _weight(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        return number if math.isfinite(number) and number >= 0.0 else None

    shares: dict[str, float] = {}
    defaulted_terms: dict[str, list[str]] = {}
    for dimension in _UNOBSERVED_SHARE_DIMENSIONS:
        record = dimensions.get(dimension)
        if not isinstance(record, Mapping):
            return None
        base_terms = record.get("components")
        adjustments = record.get("adjustments")
        if not isinstance(base_terms, list) or not isinstance(adjustments, list):
            return None
        blends = []
        for adjustment in adjustments:
            if not isinstance(adjustment, Mapping):
                return None
            retained = _weight(adjustment.get("retained_weight"))
            input_weight = _weight(adjustment.get("input_weight"))
            if retained is None or input_weight is None:
                return None
            if retained < 1.0:
                blends.append((adjustment, input_weight, retained))

        retained_after_base = 1.0
        for _, _, retained in blends:
            retained_after_base *= retained

        total = 0.0
        unobserved = 0.0
        names: list[str] = []
        for term in base_terms:
            if not isinstance(term, Mapping):
                return None
            weight = _weight(term.get("weight"))
            if weight is None:
                return None
            final_weight = weight * retained_after_base
            total += final_weight
            if term.get("observed") is not True:
                unobserved += final_weight
                names.append(str(term.get("name")))
        for index, (adjustment, input_weight, _) in enumerate(blends):
            final_weight = input_weight
            for _, _, later_retained in blends[index + 1:]:
                final_weight *= later_retained
            total += final_weight
            if adjustment.get("observed") is not True:
                unobserved += final_weight
                names.append(str(adjustment.get("name")))
        if total <= 0.0:
            return None
        shares[dimension] = max(0.0, min(1.0, unobserved / total))
        defaulted_terms[dimension] = names

    return {
        "schema": UNOBSERVED_SHARE_SCHEMA,
        "mode": "measurement_only",
        "policy_applied": False,
        "by_dimension": shares,
        "mean": sum(shares.values()) / len(shares),
        "defaulted_terms": defaulted_terms,
        "basis": "observed_flags_within_one_checkin",
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
    *,
    agent_calibration_channel: Mapping | None = None,
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
    if neutralize_sensor_self_loops_enabled():
        return {
            **base,
            "eligible": False,
            "eligibility_reason": "self_loop_already_neutralized",
            "deployed": None,
            "candidate": None,
            "candidate_minus_deployed": None,
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
        agent_calibration_channel=agent_calibration_channel,
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
        agent_calibration_channel=agent_calibration_channel,
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
    *,
    agent_calibration_channel: Mapping | None = None,
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
    if neutralize_sensor_self_loops_enabled():
        return {
            **base,
            "eligible": False,
            "eligibility_reason": "self_loop_already_neutralized",
            "deployed": None,
            "candidate": None,
            "candidate_minus_deployed": None,
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
        agent_calibration_channel=agent_calibration_channel,
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



def compute_regime_self_loop_shadow(
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
    *,
    agent_calibration_channel: Mapping | None = None,
) -> dict:
    """Shadow the behavioral S reading with the regime-instability term neutralized.

    ``detect_regime`` (``src/monitor_regime.py``) labels the regime from
    behavioral S's own step-to-step change once behavioral confidence reaches
    0.3, and ``_regime_instability`` counts those label changes at 35% of raw
    S. So part of S reads S's own jitter. In synthetic streams of the live build
    (``docs/evaluations/eisv-s-faithfulness/result-v0.md``, ledger row 46),
    holding the term constant cut resting S step SD by 94%.

    This replaces the term with ``REGIME_NEUTRAL_INSTABILITY``, its observed
    fleet level, and reports the one-step S delta. Everything else, including
    every deployed weight and adjustment, is identical. Measurement-only: the
    live observation is never modified and no policy reads this. Like its
    siblings it stops at the raw behavioral observation; replaying recursive S
    history, the EMA, or the regime labels themselves (which ``detect_regime``
    derives from S) needs a longitudinal simulator this is not.
    """
    base = {
        "schema": REGIME_SELF_LOOP_ABLATION_SCHEMA,
        "mode": "measurement_only",
        "policy_applied": False,
        "intervention": {
            "field": "regime_history",
            "source": "governance_monitor.regime_history",
            "role": "labels_derived_from_behavioral_S",
            "operation": "replace_term_with_observed_fleet_level",
            "replacement_value": REGIME_NEUTRAL_INSTABILITY,
            "deployed_weight": 0.35,
        },
        "not_modeled": [
            "recursive_S_history_replay",
            "behavioral_ema_replay",
            "regime_label_counterfactual",
            "policy_effect",
            "future_outcomes",
        ],
        "interpretation": (
            "one-step S delta. Spread of the delta across an agent's check-ins "
            "is the jitter the term injects; its mean is a level offset against "
            "the fleet stand-in"
        ),
    }
    if neutralize_sensor_self_loops_enabled():
        return {
            **base,
            "eligible": False,
            "eligibility_reason": "self_loop_already_neutralized",
            "deployed": None,
            "candidate": None,
            "candidate_minus_deployed": None,
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
    inputs = dict(
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
        agent_calibration_channel=agent_calibration_channel,
    )
    deployed = deployed_observation or compute_behavioral_sensor_eisv(**inputs)
    candidate = compute_behavioral_sensor_eisv(
        **inputs,
        regime_instability_override=REGIME_NEUTRAL_INSTABILITY,
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
    deployed_s = float(deployed["S"])
    candidate_s = float(candidate["S"])
    return {
        **base,
        "eligible": True,
        "eligibility_reason": None,
        "deployed": {"S": deployed_s},
        "candidate": {"S": candidate_s},
        "candidate_minus_deployed": {"S": candidate_s - deployed_s},
        "deployed_regime_instability": _regime_instability(regime_history),
    }


def compute_calibration_free_i_shadow(
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
    *,
    agent_calibration_channel: Mapping | None = None,
) -> dict:
    """Shadow the behavioral I reading with the global calibration slot removed (Arm F).

    Implements Arm F of ``docs/proposals/active/eisv-per-agent-i-calibration-v0.md``
    §6.4 (#2818): removes ``calibration_accuracy`` from the base ``I`` formula
    and renormalizes the remaining base terms to sum to 1.0 before the standard
    post-base adjustments (``continuity_I_input`` at 0.20, ``unique_tools_ratio``
    at 0.10):
    - Default (``UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1``, era ``SN1``):
      - With >=3 non-echo scored outcomes: ``outcome_score_consistency`` 1.00
      - Without >=3 non-echo scored outcomes: ineligible (``insufficient_outcomes_under_neutralized_self_loops``),
        because neither ``calibration_accuracy`` nor ``legacy_coherence_trend`` nor
        ``outcome_score_consistency`` is present in the candidate.
    - Legacy (``UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=0``):
      - With >=3 scored outcomes: ``legacy_coherence_trend`` 0.60 + ``outcome_score_consistency`` 0.40
      - Without >=3 scored outcomes: ``legacy_coherence_trend`` 1.00

    Measurement-only: the live observation is never modified and no policy reads
    this shadow.
    """
    active_neutralize = neutralize_sensor_self_loops_enabled()
    if active_neutralize:
        weights_with_outcomes = {
            "outcome_score_consistency": 1.00,
        }
        weights_without_outcomes = None
    else:
        weights_with_outcomes = {
            "legacy_coherence_trend": 0.60,
            "outcome_score_consistency": 0.40,
        }
        weights_without_outcomes = {
            "legacy_coherence_trend": 1.00,
        }
    base = {
        "schema": CALIBRATION_FREE_I_ABLATION_SCHEMA,
        "mode": "measurement_only",
        "policy_applied": False,
        "intervention": {
            "field": "calibration_error",
            "source": "fleet_calibration",
            "role": "global_calibration_term_in_I",
            "operation": "remove_slot_and_renormalize_base_weights",
            "weights_with_outcomes": weights_with_outcomes,
            "weights_without_outcomes": weights_without_outcomes,
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
    if active_neutralize:
        filtered_outcomes = filter_behavioral_outcomes(outcome_history)
        valid_scores = [
            s for o in (filtered_outcomes or ())
            if (s := o.get("outcome_score")) is not None
            and isinstance(s, (int, float))
            and not isinstance(s, bool)
            and math.isfinite(s)
        ]
        if len(valid_scores) < 3:
            return {
                **base,
                "eligible": False,
                "eligibility_reason": "insufficient_outcomes_under_neutralized_self_loops",
                "deployed": None,
                "candidate": None,
                "candidate_minus_deployed": None,
            }
    inputs = dict(
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
        agent_calibration_channel=agent_calibration_channel,
    )
    deployed = deployed_observation or compute_behavioral_sensor_eisv(**inputs)
    candidate = compute_behavioral_sensor_eisv(**inputs, calibration_free_i=True)
    if deployed is None or candidate is None:
        return {
            **base,
            "eligible": False,
            "eligibility_reason": "behavioral_sensor_unavailable",
            "deployed": None,
            "candidate": None,
            "candidate_minus_deployed": None,
        }
    deployed_i = float(deployed["I"])
    candidate_i = float(candidate["I"])
    return {
        **base,
        "eligible": True,
        "eligibility_reason": None,
        "deployed": {"I": deployed_i},
        "candidate": {"I": candidate_i},
        "candidate_minus_deployed": {"I": candidate_i - deployed_i},
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
    *,
    neutralize_self_loops: bool = False,
) -> dict:
    # Complexity calibration — low divergence = high capacity awareness
    cd = complexity_divergence if complexity_divergence is not None else 0.15
    cal_e = max(0.3, min(1.0, 1.0 - cd))

    # Outcome success rate — successful outcomes indicate productive energy
    filtered_outcomes = (
        filter_behavioral_outcomes(outcome_history)
        if neutralize_self_loops
        else outcome_history
    )
    has_outcomes = bool(filtered_outcomes and len(filtered_outcomes) >= 3)
    outcome_e = 0.0
    if has_outcomes and filtered_outcomes is not None:
        good_count = sum(1 for o in filtered_outcomes if not o.get('is_bad', False))
        success_rate = good_count / len(filtered_outcomes)
        outcome_e = OUTCOME_E_FLOOR + success_rate * OUTCOME_E_SPAN  # [0,1] -> [0.3, 0.9]

    if neutralize_self_loops:
        # Exclude controller self-loops (decision_history, legacy_coherence_level)
        # and renormalize remaining observed base terms to sum to 1.0 (avoiding
        # diluting an observed term with a defaulted constant when only one of
        # the two non-loop base terms is observed).
        cal_observed = complexity_divergence is not None
        if has_outcomes and cal_observed:
            components = [
                _component(
                    "complexity_calibration", "continuity_metrics", cal_e, 0.50,
                    observed=True,
                ),
                _component(
                    "outcome_success", "recent_outcomes", outcome_e, 0.50,
                    observed=True,
                ),
            ]
            raw = (
                components[0]["weighted_contribution"]
                + components[1]["weighted_contribution"]
            )
        elif has_outcomes:
            components = [
                _component(
                    "outcome_success", "recent_outcomes", outcome_e, 1.00,
                    observed=True,
                ),
            ]
            raw = components[0]["weighted_contribution"]
        else:
            components = [
                _component(
                    "complexity_calibration", "continuity_metrics", cal_e, 1.00,
                    observed=cal_observed,
                    default_reason=(
                        None if cal_observed
                        else "default_complexity_divergence_0.15"
                    ),
                ),
            ]
            raw = components[0]["weighted_contribution"]
        value = max(0.0, min(1.0, raw))
        return {
            "value": value,
            "base_value": value,
            "components": components,
            "adjustments": [],
        }

    # Decision success — exponentially weighted
    window = decision_history[-10:]
    if not window:
        decision_e = 0.65
        decision_observed = False
        decision_default_reason = "no_decision_history"
    else:
        n = len(window)
        alpha = 0.3
        weights = [math.exp(alpha * (i - n + 1)) for i in range(n)]
        total_w = sum(weights)
        decision_e = sum(
            w * _DECISION_SCORES.get(str(d).lower(), 0.5)
            for w, d in zip(weights, window)
        ) / total_w
        decision_observed = True
        decision_default_reason = None

    # Legacy C(V_ODE) controller level — map [0.35, 0.65] → [0.3, 0.9].
    # ``coherence_history`` is retained as the compatibility parameter name.
    if coherence_history and len(coherence_history) >= 3:
        recent_coh = coherence_history[-10:]
        mean_coh = sum(recent_coh) / len(recent_coh)
        coh_e = 0.3 + (mean_coh - 0.35) * 2.0  # 0.35→0.3, 0.65→0.9
        coh_e = max(0.3, min(0.9, coh_e))
        coh_observed = True
        coh_default_reason = None
    else:
        coh_e = 0.6
        coh_observed = False
        coh_default_reason = "insufficient_coherence_history"

    if has_outcomes:
        # Weights: 35% decision, 25% coherence, 20% calibration, 20% outcomes
        components = [
            _component(
                "decision_history", "governance_decisions", decision_e, 0.35,
                observed=decision_observed,
                default_reason=decision_default_reason,
                health_evidence=False,
            ),
            _component(
                "legacy_coherence_level", "legacy_tanh_v", coh_e, 0.25,
                observed=coh_observed,
                default_reason=coh_default_reason,
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
                observed=decision_observed,
                default_reason=decision_default_reason,
                health_evidence=False,
            ),
            _component(
                "legacy_coherence_level", "legacy_tanh_v", coh_e, 0.30,
                observed=coh_observed,
                default_reason=coh_default_reason,
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
    *,
    neutralize_self_loops: bool = False,
    calibration_free_i: bool = False,
    per_agent_i_calibration: bool = False,
    agent_calibration_channel: Mapping | None = None,
) -> dict:
    effective_calibration_free_i = bool(calibration_free_i)
    cal_source = "fleet_calibration"
    cal_observed = calibration_error is not None
    cal_default_reason = (
        None if calibration_error is not None
        else "default_calibration_accuracy_0.75"
    )
    cal_I = 1.0 - calibration_error if calibration_error is not None else 0.75
    cal_I = max(0.0, min(1.0, cal_I))

    if per_agent_i_calibration and not effective_calibration_free_i:
        agent_cal_I: float | None = None
        agent_cal_source: str | None = None
        if isinstance(agent_calibration_channel, Mapping):
            n_pairs = agent_calibration_channel.get("n_verified_pairs")
            agent_err = agent_calibration_channel.get("calibration_error")
            status = agent_calibration_channel.get("evidence_status")
            freshness = agent_calibration_channel.get("freshness_status")
            prior_err = agent_calibration_channel.get("prior_error")
            if (
                status == "available"
                and freshness == "recent"
                and isinstance(n_pairs, int)
                and not isinstance(n_pairs, bool)
                and n_pairs >= 1
                and isinstance(agent_err, (int, float))
                and not isinstance(agent_err, bool)
                and math.isfinite(float(agent_err))
            ):
                clamped_err = max(0.0, min(1.0, float(agent_err)))
                if (
                    isinstance(prior_err, (int, float))
                    and not isinstance(prior_err, bool)
                    and math.isfinite(float(prior_err))
                    and 0.0 < float(prior_err) < 1.0
                ):
                    w = float(n_pairs) / (float(n_pairs) + AGENT_CALIBRATION_POOLING_K)
                    e_pool = (1.0 - w) * float(prior_err) + w * clamped_err
                    agent_cal_I = max(0.0, min(1.0, 1.0 - e_pool))
                    agent_cal_source = "agent_calibration_pooled"
                elif n_pairs >= MIN_UNPOOLED_AGENT_CALIBRATION_PAIRS:
                    agent_cal_I = max(0.0, min(1.0, 1.0 - clamped_err))
                    agent_cal_source = "agent_tactical_calibration"
        if agent_cal_I is not None and agent_cal_source is not None:
            cal_I = agent_cal_I
            cal_source = agent_cal_source
            cal_observed = True
            cal_default_reason = None
        else:
            # #2818 §3.2 Rule 1 & Rule 3: with no admissible per-agent evidence
            # (or n < 5 without a reference-cohort prior, or stale pairs), no
            # shared fleet scalar or invented prior enters I; fall back to
            # calibration-free I (Arm F).
            effective_calibration_free_i = True

    # Outcome consistency — consistent scores indicate information integrity
    filtered_outcomes = (
        filter_behavioral_outcomes(outcome_history)
        if neutralize_self_loops
        else outcome_history
    )
    consistency_I: float | None = None
    if filtered_outcomes and len(filtered_outcomes) >= 3:
        scores = [s for o in filtered_outcomes
                  if (s := o.get('outcome_score')) is not None
                  and isinstance(s, (int, float)) and not isinstance(s, bool)
                  and math.isfinite(s)]
        if len(scores) >= 3:
            mean_s = sum(scores) / len(scores)
            score_var = sum((s - mean_s) ** 2 for s in scores) / len(scores)
            consistency_I = max(0.3, 1.0 - score_var * 4)  # Low variance = high consistency

    if neutralize_self_loops:
        # Exclude legacy_coherence_trend and renormalize remaining observed base terms to sum to 1.0.
        cal_active = (not effective_calibration_free_i) and cal_observed
        if consistency_I is not None and cal_active:
            components = [
                _component(
                    "calibration_accuracy", cal_source, cal_I, 0.50 / 0.70,
                    observed=True,
                ),
                _component(
                    "outcome_score_consistency", "recent_outcomes", consistency_I, 0.20 / 0.70,
                    observed=True,
                    health_evidence=False,
                ),
            ]
            raw = (
                components[0]["weighted_contribution"]
                + components[1]["weighted_contribution"]
            )
        elif consistency_I is not None:
            components = [
                _component(
                    "outcome_score_consistency", "recent_outcomes", consistency_I, 1.00,
                    observed=True,
                    health_evidence=False,
                ),
            ]
            raw = components[0]["weighted_contribution"]
        elif cal_active:
            components = [
                _component(
                    "calibration_accuracy", cal_source, cal_I, 1.00,
                    observed=True,
                ),
            ]
            raw = components[0]["weighted_contribution"]
        else:
            fallback_reason = (
                "no_admissible_calibration_or_outcomes"
                if effective_calibration_free_i
                else "default_calibration_accuracy_0.75"
            )
            components = [
                _component(
                    "uncalibrated_base_fallback", "behavioral_sensor_default", 0.75, 1.00,
                    observed=False,
                    default_reason=fallback_reason,
                    health_evidence=False,
                ),
            ]
            raw = components[0]["weighted_contribution"]
        value = max(0.0, min(1.0, raw))
        return {
            "value": value,
            "base_value": value,
            "components": components,
            "adjustments": [],
        }

    coh_I = _coherence_trend(coherence_history)
    coh_observed = len(coherence_history) >= 4
    coh_default_reason = (
        None if len(coherence_history) >= 4
        else "insufficient_coherence_history"
    )

    if consistency_I is not None:
        if effective_calibration_free_i:
            components = [
                _component(
                    "legacy_coherence_trend", "legacy_tanh_v", coh_I, 0.60,
                    observed=coh_observed,
                    default_reason=coh_default_reason,
                    health_evidence=False,
                ),
                _component(
                    "outcome_score_consistency", "recent_outcomes", consistency_I, 0.40,
                    observed=True,
                    health_evidence=False,
                ),
            ]
            raw = (
                components[0]["weighted_contribution"]
                + components[1]["weighted_contribution"]
            )
        else:
            # Weights: 50% calibration, 30% coherence trend, 20% outcome consistency
            components = [
                _component(
                    "calibration_accuracy", cal_source, cal_I, 0.50,
                    observed=cal_observed,
                    default_reason=cal_default_reason,
                ),
                _component(
                    "legacy_coherence_trend", "legacy_tanh_v", coh_I, 0.30,
                    observed=coh_observed,
                    default_reason=coh_default_reason,
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

    if effective_calibration_free_i:
        components = [
            _component(
                "legacy_coherence_trend", "legacy_tanh_v", coh_I, 1.00,
                observed=coh_observed,
                default_reason=coh_default_reason,
                health_evidence=False,
            ),
        ]
        raw = components[0]["weighted_contribution"]
    else:
        components = [
            _component(
                "calibration_accuracy", cal_source, cal_I, 0.60,
                observed=cal_observed,
                default_reason=cal_default_reason,
            ),
            _component(
                "legacy_coherence_trend", "legacy_tanh_v", coh_I, 0.40,
                observed=coh_observed,
                default_reason=coh_default_reason,
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
    *,
    regime_instability_override: float | None = None,
    neutralize_self_loops: bool = False,
) -> dict:
    # Drift component (40%)
    dn = drift_norm if drift_norm is not None else 0.2
    drift_s = min(1.0, dn * 1.5)

    # Complexity divergence (25%)
    cd = complexity_divergence if complexity_divergence is not None else 0.1
    cd_s = min(1.0, cd)

    # Regime instability (35%): count transitions / window. The override exists
    # only for compute_regime_self_loop_shadow; the deployed path never sets it.
    regime_overridden = regime_instability_override is not None
    if neutralize_self_loops and not regime_overridden:
        drift_observed = drift_norm is not None
        cd_observed = complexity_divergence is not None
        if drift_observed and not cd_observed:
            components = [
                _component(
                    "drift_norm", "governance_drift", drift_s, 1.00,
                    observed=True,
                ),
            ]
            raw = components[0]["weighted_contribution"]
        elif cd_observed and not drift_observed:
            components = [
                _component(
                    "complexity_divergence", "continuity_metrics", cd_s, 1.00,
                    observed=True,
                ),
            ]
            raw = components[0]["weighted_contribution"]
        else:
            components = [
                _component(
                    "drift_norm", "governance_drift", drift_s, 0.40 / 0.65,
                    observed=drift_observed,
                    default_reason=None if drift_observed else "default_drift_norm_0.2",
                ),
                _component(
                    "complexity_divergence", "continuity_metrics", cd_s, 0.25 / 0.65,
                    observed=cd_observed,
                    default_reason=(
                        None if cd_observed
                        else "default_complexity_divergence_0.1"
                    ),
                ),
            ]
            raw = (
                components[0]["weighted_contribution"]
                + components[1]["weighted_contribution"]
            )
        value = max(0.05, min(1.0, raw))
        return {
            "value": value,
            "base_value": value,
            "components": components,
            "adjustments": [],
        }

    if regime_overridden:
        regime_s = float(regime_instability_override)
        regime_observed = False
        regime_default_reason = "ablation_override"
    else:
        regime_s = _regime_instability(regime_history)
        regime_observed = len(regime_history) >= 2
        regime_default_reason = (
            None if len(regime_history) >= 2
            else "insufficient_regime_history"
        )

    components = [
        _component(
            "drift_norm", "governance_drift", drift_s, 0.40,
            observed=drift_norm is not None,
            default_reason=None if drift_norm is not None else "default_drift_norm_0.2",
        ),
        _component(
            "regime_instability", "regime_history", regime_s, 0.35,
            observed=regime_observed,
            default_reason=regime_default_reason,
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
