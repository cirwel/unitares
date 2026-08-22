"""Observational maturity gate for fallback-owned cold-start risk pauses.

The deployed monitor can express the same risk-driven pause as ``risk_pause``
or as a CIRS ``cirs_block``.  A selected ``nearest_edge`` cannot establish that
risk was the sole effective cause: the priority stack can mask simultaneous
resonance, coherence, void, or basin hard stops.  Eligibility therefore
requires the decision producer's complete versioned hard-stop provenance.

The two-observation confirmation policy in this module does not actuate: it
evaluates, in shadow, whether a pause would be the first or second adjacent
fallback-owned observation and returns a fully serializable provenance record.
A separate stateless epistemic-authority guard can turn one exact non-authored
cold-start pause into guidance.  That transition is fail-closed and does not
promote the dormant confirmation policy.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from numbers import Real
from typing import Any

from config.governance_config import (
    BASIN_LOW_COHERENCE_CEIL,
    BASIN_LOW_I_CEIL,
    BASIN_LOW_RISK_FLOOR,
    BASIN_LOW_V_ABS_FLOOR,
    classify_basin,
)
from src.cirs import CIRS_DEFAULTS
from src.monitor_decision import HARD_STOP_PROVENANCE_SCHEMA


BEHAVIORAL_AUTHORITY_THRESHOLD = 0.3
COLD_START_CONFIRMATIONS_REQUIRED = 2
NON_AUTHORING_EPISTEMIC_CLASSES = frozenset({
    "substrate_observation",
    "substrate_interpretation",
    "prediction",
    "synthetic",
})
_KNOWN_EPISTEMIC_CLASSES = NON_AUTHORING_EPISTEMIC_CLASSES | {"agent_report"}

NON_AUTHORED_COLD_START_GUARD_SCHEMA = "eisv.cold-start-epistemic-guard.v1"
NON_AUTHORED_COLD_START_RECOVERY_SCHEMA = "eisv.cold-start-recovery.v1"
NON_AUTHORED_COLD_START_ENFORCEMENT_BASIS = (
    "non_authored_phi_cold_start_deferred"
)
NON_AUTHORED_COLD_START_RECOVERY_BASIS = (
    "non_authored_phi_cold_start_trap"
)
COLD_START_CONFIRMATION_ACTUATION_SCOPE = "fallback_risk_pause_deferral"


def _is_risk_routed_pause(decision: Mapping[str, Any]) -> bool:
    """Match the two policy routes that may represent fallback-owned risk."""
    if (
        decision.get("action") != "pause"
        or decision.get("nearest_edge") != "risk"
    ):
        return False
    sub_action = decision.get("sub_action")
    return sub_action in {"risk_pause", "cirs_block"}


def _validated_risk_only_hard_stop_provenance(
    decision: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return exact, internally consistent risk-only provenance or ``None``.

    The aggregate ``risk_only`` bit is not trusted by itself.  This validator
    recomputes every condition from the recorded decision-time inputs and
    thresholds, checks the risk-neutral basin counterfactual, and verifies the
    complete trigger lists.  Missing or contradictory fields fail closed.
    """
    provenance = decision.get("hard_stop_provenance")
    if not isinstance(provenance, Mapping):
        return None
    selected = provenance.get("selected_decision")
    selected = selected if isinstance(selected, Mapping) else {}
    cirs = provenance.get("cirs")
    cirs = cirs if isinstance(cirs, Mapping) else {}
    cirs_observed = cirs.get("observed")
    cirs_observed = cirs_observed if isinstance(cirs_observed, Mapping) else {}
    cirs_thresholds = cirs.get("thresholds")
    cirs_thresholds = (
        cirs_thresholds if isinstance(cirs_thresholds, Mapping) else {}
    )
    cirs_conditions = cirs.get("conditions")
    cirs_conditions = (
        cirs_conditions if isinstance(cirs_conditions, Mapping) else {}
    )
    policy = provenance.get("policy")
    policy = policy if isinstance(policy, Mapping) else {}
    observed = policy.get("observed")
    observed = observed if isinstance(observed, Mapping) else {}
    policy_thresholds = policy.get("thresholds")
    policy_thresholds = (
        policy_thresholds if isinstance(policy_thresholds, Mapping) else {}
    )
    policy_conditions = policy.get("conditions")
    policy_conditions = (
        policy_conditions if isinstance(policy_conditions, Mapping) else {}
    )
    counterfactual = policy.get("risk_neutral_counterfactual")
    counterfactual = (
        counterfactual if isinstance(counterfactual, Mapping) else {}
    )

    observed_values = {
        name: _finite_number(observed.get(name))
        for name in ("E", "I", "S", "V", "coherence", "risk_score")
    }
    policy_threshold_values = {
        name: _finite_number(policy_thresholds.get(name))
        for name in (
            "coherence_critical",
            "basin_low_I",
            "basin_low_coherence",
            "basin_low_abs_V",
            "basin_low_risk",
        )
    }
    required_cirs_conditions = {
        name: cirs_conditions.get(name)
        for name in (
            "coherence_floor",
            "risk_ceiling",
            "resonance",
            "unclassified_hard_block",
        )
    }
    required_policy_conditions = {
        name: policy_conditions.get(name)
        for name in (
            "void_active",
            "coherence_floor",
            "high_risk_verdict",
            "low_basin",
            "basin_low_integrity",
            "basin_low_coherence",
            "basin_high_abs_valence",
            "basin_risk_floor",
            "independent_low_basin",
        )
    }
    if (
        provenance.get("schema") != HARD_STOP_PROVENANCE_SCHEMA
        or provenance.get("complete") is not True
        or provenance.get("risk_only") is not True
        or not all(value is not None for value in observed_values.values())
        or not all(value is not None for value in policy_threshold_values.values())
        or not all(
            isinstance(value, bool)
            for value in required_cirs_conditions.values()
        )
        or not all(
            isinstance(value, bool)
            for value in required_policy_conditions.values()
        )
        or not isinstance(observed.get("void_active"), bool)
        or not isinstance(observed.get("verdict"), str)
        or observed.get("basin") not in {"high", "boundary", "low"}
        or cirs.get("provenance_complete") is not True
        or cirs.get("mode") not in {
            "not_supplied",
            "legacy_v0_1",
            "adaptive_v2",
        }
        or selected.get("action") != decision.get("action")
        or selected.get("sub_action") != decision.get("sub_action")
        or selected.get("nearest_edge") != decision.get("nearest_edge")
    ):
        return None

    E = observed_values["E"]
    I = observed_values["I"]
    S = observed_values["S"]
    V = observed_values["V"]
    coherence = observed_values["coherence"]
    risk = observed_values["risk_score"]
    coherence_critical = policy_threshold_values["coherence_critical"]
    basin_risk_floor = policy_threshold_values["basin_low_risk"]
    counterfactual_risk = _finite_number(counterfactual.get("risk_score"))
    if counterfactual_risk != 0.0:
        return None
    if (
        policy_threshold_values["basin_low_I"] != BASIN_LOW_I_CEIL
        or policy_threshold_values["basin_low_coherence"]
        != BASIN_LOW_COHERENCE_CEIL
        or policy_threshold_values["basin_low_abs_V"]
        != BASIN_LOW_V_ABS_FLOOR
        or policy_threshold_values["basin_low_risk"]
        != BASIN_LOW_RISK_FLOOR
    ):
        return None
    recomputed_basin = classify_basin(
        E=E,
        I=I,
        S=S,
        V=V,
        coherence=coherence,
        risk_score=risk,
    )
    recomputed_counterfactual_basin = classify_basin(
        E=E,
        I=I,
        S=S,
        V=V,
        coherence=coherence,
        risk_score=counterfactual_risk,
    )
    expected_policy_conditions = {
        "void_active": observed.get("void_active") is True,
        "coherence_floor": coherence < coherence_critical,
        "high_risk_verdict": observed.get("verdict") == "high-risk",
        "low_basin": recomputed_basin == "low",
        "basin_low_integrity": I < BASIN_LOW_I_CEIL,
        "basin_low_coherence": coherence < BASIN_LOW_COHERENCE_CEIL,
        "basin_high_abs_valence": abs(V) > BASIN_LOW_V_ABS_FLOOR,
        "basin_risk_floor": risk >= basin_risk_floor,
        "independent_low_basin": recomputed_counterfactual_basin == "low",
    }
    if (
        required_policy_conditions != expected_policy_conditions
        or observed.get("basin") != recomputed_basin
        or counterfactual.get("basin") != recomputed_counterfactual_basin
    ):
        return None

    cirs_floor = _finite_number(cirs_thresholds.get("coherence_floor"))
    cirs_ceiling = _finite_number(cirs_thresholds.get("risk_ceiling"))
    cirs_oi_threshold = _finite_number(
        cirs_thresholds.get("oscillation_index")
    )
    cirs_flip_threshold = cirs_thresholds.get("flips")
    cirs_observed_coherence = _finite_number(cirs_observed.get("coherence"))
    cirs_observed_risk = _finite_number(cirs_observed.get("risk_score"))
    cirs_observed_oi = _finite_number(cirs_observed.get("oscillation_index"))
    cirs_observed_flips = cirs_observed.get("flips")
    response_tier = cirs.get("response_tier")
    cirs_mode = cirs.get("mode")
    # Match the producer's priority semantics, not merely a record that agrees
    # with itself.  A CIRS hard block always selects ``cirs_block`` before the
    # direct verdict route; conversely ``cirs_block`` cannot exist without that
    # tier.  Coherently relabeling both the decision and its embedded selected
    # route must therefore still fail closed.
    if (
        decision.get("sub_action") == "cirs_block"
        and response_tier != "hard_block"
    ) or (
        decision.get("sub_action") == "risk_pause"
        and response_tier == "hard_block"
    ):
        return None
    if cirs_mode == "not_supplied":
        if (
            response_tier is not None
            or cirs_observed_coherence != coherence
            or cirs_observed_risk != risk
            or cirs_observed_oi is not None
            or cirs_observed_flips is not None
            or cirs_floor is not None
            or cirs_ceiling is not None
            or cirs_oi_threshold is not None
            or cirs_flip_threshold is not None
            or any(required_cirs_conditions.values())
        ):
            return None
    else:
        allowed_tiers = (
            {"hard_block", "soft_dampen", "proceed"}
            if cirs_mode == "legacy_v0_1"
            else {"hard_block", "safe", "caution", "high-risk"}
        )
        if (
            cirs_mode not in {"legacy_v0_1", "adaptive_v2"}
            or response_tier not in allowed_tiers
            or cirs_floor is None
            or cirs_ceiling is None
            or cirs_oi_threshold is None
            or not isinstance(cirs_flip_threshold, int)
            or isinstance(cirs_flip_threshold, bool)
            or cirs_observed_coherence != coherence
            or cirs_observed_risk != risk
            or cirs_observed_oi is None
            or not isinstance(cirs_observed_flips, int)
            or isinstance(cirs_observed_flips, bool)
            or required_cirs_conditions["coherence_floor"]
            != (coherence < cirs_floor)
            or required_cirs_conditions["risk_ceiling"]
            != (risk > cirs_ceiling)
            or required_cirs_conditions["resonance"]
            != (
                abs(cirs_observed_oi) >= cirs_oi_threshold
                or cirs_observed_flips >= cirs_flip_threshold
            )
        ):
            return None
        if cirs_mode == "legacy_v0_1" and (
            cirs_floor != CIRS_DEFAULTS["tau_low"]
            or cirs_ceiling != CIRS_DEFAULTS["beta_high"]
            or cirs_oi_threshold != CIRS_DEFAULTS["oi_threshold"]
            or cirs_flip_threshold != CIRS_DEFAULTS["flip_threshold"]
        ):
            return None
        absolute_stop = (
            required_cirs_conditions["coherence_floor"]
            or required_cirs_conditions["risk_ceiling"]
        )
        if (
            response_tier == "hard_block"
            and not (
                absolute_stop
                or (
                    cirs_mode == "legacy_v0_1"
                    and required_cirs_conditions["resonance"]
                )
            )
        ) or (response_tier != "hard_block" and absolute_stop):
            return None

    expected_risk_hard_stops = [
        name
        for name, active in (
            ("cirs_risk_ceiling", required_cirs_conditions["risk_ceiling"]),
            ("high_risk_verdict", required_policy_conditions["high_risk_verdict"]),
            ("basin_risk_floor", required_policy_conditions["basin_risk_floor"]),
        )
        if active
    ]
    expected_independent_hard_stops = [
        name
        for name, active in (
            ("cirs_resonance", required_cirs_conditions["resonance"]),
            ("cirs_coherence_floor", required_cirs_conditions["coherence_floor"]),
            (
                "cirs_unclassified_hard_block",
                required_cirs_conditions["unclassified_hard_block"],
            ),
            ("void_active", required_policy_conditions["void_active"]),
            (
                "policy_coherence_floor",
                required_policy_conditions["coherence_floor"],
            ),
            (
                "independent_low_basin",
                required_policy_conditions["independent_low_basin"],
            ),
        )
        if active
    ]
    if (
        not expected_risk_hard_stops
        or expected_independent_hard_stops
        or provenance.get("risk_hard_stops") != expected_risk_hard_stops
        or provenance.get("independent_hard_stops") != []
    ):
        return None
    return provenance


def _is_fallback_risk_policy_candidate(decision: Mapping[str, Any]) -> bool:
    """Match only a risk route with complete risk-only trigger provenance."""
    return (
        _is_risk_routed_pause(decision)
        and _validated_risk_only_hard_stop_provenance(decision) is not None
    )


def classify_verdict_driver(
    *,
    behavioral_confidence: Any,
    behavioral_verdict: Any,
    behavioral_enabled: bool,
    phi_telemetry: bool,
) -> str:
    """Name the source that actually owns the final verdict."""
    confidence = _finite_number(behavioral_confidence)
    behavioral_warm = (
        behavioral_enabled
        and isinstance(behavioral_verdict, str)
        and bool(behavioral_verdict)
        and confidence is not None
        and confidence >= BEHAVIORAL_AUTHORITY_THRESHOLD
    )
    if behavioral_warm and phi_telemetry:
        return "behavioral_assessment"
    if behavioral_warm:
        return "phi_floor"
    return "phi_cold_start"


def evaluate_cold_start_risk_confirmation(
    decision: Mapping[str, Any],
    *,
    behavioral_confidence: Any,
    is_baselined: bool,
    primary_driver: Any,
    primary_eisv_source: Any,
    process_cycle: int,
    monitor_lineage: str,
    lineage_status: str,
    previous_evaluation: Mapping[str, Any] | None,
    history_gap: bool,
    independent_override: str | None,
    shadow_enabled: bool,
    actuation_enabled: bool,
) -> dict[str, Any]:
    """Evaluate a two-observation confirmation rule without changing policy.

    Eligibility is deliberately fail-closed.  Missing provenance, a history
    discontinuity, a restored/unknown monitor lineage, or an independent safety
    signal all leave the original pause untouched.  The returned record is
    suitable for policy, enforcement, persistence, dashboard, and audit use.
    """
    confidence = _finite_number(behavioral_confidence)
    action = decision.get("action")
    sub_action = decision.get("sub_action")
    reason = decision.get("reason")
    nearest_edge = decision.get("nearest_edge")
    risk_routed_pause = _is_risk_routed_pause(decision)
    hard_stop_provenance = _validated_risk_only_hard_stop_provenance(decision)
    policy_candidate = risk_routed_pause and hard_stop_provenance is not None
    measurement_ready = (
        confidence is not None
        and confidence >= BEHAVIORAL_AUTHORITY_THRESHOLD
    )
    enabled = bool(shadow_enabled or actuation_enabled)
    provenance_complete = (
        confidence is not None
        and isinstance(primary_driver, str)
        and bool(primary_driver)
        and isinstance(primary_eisv_source, str)
        and bool(primary_eisv_source)
        and isinstance(action, str)
        and bool(action.strip())
        and isinstance(sub_action, str)
        and bool(sub_action.strip())
        and isinstance(reason, str)
        and bool(reason.strip())
        and isinstance(is_baselined, bool)
        and (
            independent_override is None
            or (
                isinstance(independent_override, str)
                and bool(independent_override.strip())
            )
        )
        and isinstance(history_gap, bool)
        and isinstance(monitor_lineage, str)
        and bool(monitor_lineage.strip())
        and isinstance(lineage_status, str)
        and bool(lineage_status.strip())
        and isinstance(process_cycle, int)
        and not isinstance(process_cycle, bool)
        and process_cycle > 0
    )

    ineligibility_reason = None
    if not enabled:
        ineligibility_reason = "gate_disabled"
    elif not risk_routed_pause:
        ineligibility_reason = "policy_not_risk_pause"
    elif hard_stop_provenance is None:
        ineligibility_reason = "hard_stop_provenance_missing_or_not_risk_only"
    elif not provenance_complete:
        ineligibility_reason = "provenance_incomplete"
    elif independent_override:
        ineligibility_reason = "independent_override"
    elif measurement_ready:
        ineligibility_reason = "behavioral_measurement_ready"
    elif is_baselined:
        ineligibility_reason = "behavioral_baseline_present"
    elif primary_driver != "phi_cold_start":
        ineligibility_reason = "verdict_source_not_phi_cold_start"
    elif primary_eisv_source != "ode_fallback":
        ineligibility_reason = "eisv_source_not_ode_fallback"
    elif history_gap:
        ineligibility_reason = "history_gap"
    elif lineage_status != "identity_genesis":
        ineligibility_reason = "restart_or_lineage_uncertainty"

    eligible = ineligibility_reason is None
    previous = previous_evaluation if isinstance(previous_evaluation, Mapping) else {}
    adjacent_confirmation = (
        eligible
        and previous.get("eligible") is True
        and previous.get("monitor_lineage") == monitor_lineage
        and previous.get("process_cycle") == process_cycle - 1
        and previous.get("primary_driver") == "phi_cold_start"
        and previous.get("primary_eisv_source") == "ode_fallback"
        and previous.get("policy_candidate") is True
    )
    confirmation_count = (
        min(
            COLD_START_CONFIRMATIONS_REQUIRED,
            int(previous.get("confirmation_count") or 0) + 1,
        )
        if adjacent_confirmation
        else (1 if eligible else 0)
    )
    would_defer = eligible and confirmation_count < COLD_START_CONFIRMATIONS_REQUIRED
    confirmed = eligible and confirmation_count >= COLD_START_CONFIRMATIONS_REQUIRED

    if not enabled:
        outcome = "disabled"
    elif not eligible:
        outcome = "ineligible"
    elif would_defer:
        outcome = "shadow_would_defer"
    else:
        outcome = "shadow_confirmed"

    if eligible and confirmation_count == 1:
        reset_reason = (
            "first_identity_observation"
            if process_cycle == 1
            else "intervening_or_discontinuous_observation"
        )
    else:
        reset_reason = ineligibility_reason

    # The operator flag is intentionally insufficient by itself.  A future
    # promotion must first add an atomic durable confirmation record; until
    # then even an accidentally enabled flag cannot suppress a pause.
    actuation_ready = False
    actuation_applied = False
    if actuation_enabled:
        actuation_blocker = "durable_confirmation_state_not_implemented"
    else:
        actuation_blocker = "operator_flag_disabled"

    if policy_candidate and independent_override:
        enforcement_basis = "independent_override"
    elif policy_candidate and would_defer:
        enforcement_basis = "phi_cold_start_unconfirmed_shadow"
    elif policy_candidate and confirmed:
        enforcement_basis = "phi_cold_start_confirmed"
    elif policy_candidate and primary_driver == "phi_cold_start":
        enforcement_basis = "phi_cold_start_fail_closed"
    elif policy_candidate:
        enforcement_basis = "risk_policy"
    elif action in {"pause", "reject"}:
        enforcement_basis = "non_cold_start_policy"
    else:
        enforcement_basis = "advisory_policy"

    return {
        "schema": "eisv.cold-start-confirmation.v1",
        "mode": "shadow" if enabled else "disabled",
        "shadow_enabled": bool(shadow_enabled),
        # These actuation fields describe only the dormant confirmation policy:
        # whether the *first* fallback-owned risk pause would be deferred.  They
        # do not describe the underlying risk policy's runtime circuit breaker;
        # that effect lives in the sibling ``enforcement`` envelope.
        "actuation_scope": COLD_START_CONFIRMATION_ACTUATION_SCOPE,
        "actuation_enabled": bool(actuation_enabled),
        "actuation_ready": actuation_ready,
        "actuation_applied": actuation_applied,
        "actuation_blocker": actuation_blocker,
        "measurement_phase": (
            "behavioral_ready" if measurement_ready else "fallback_cold_start"
        ),
        "measurement_ready": measurement_ready,
        "behavioral_confidence": confidence,
        "behavioral_authority_threshold": BEHAVIORAL_AUTHORITY_THRESHOLD,
        "is_baselined": bool(is_baselined),
        "primary_driver": primary_driver,
        "primary_eisv_source": primary_eisv_source,
        "policy_candidate": policy_candidate,
        "hard_stop_provenance": (
            dict(hard_stop_provenance)
            if isinstance(hard_stop_provenance, Mapping)
            else None
        ),
        "provenance_complete": provenance_complete,
        "eligible": eligible,
        "ineligibility_reason": ineligibility_reason,
        "reset_reason": reset_reason,
        "confirmation_count": confirmation_count,
        "confirmations_required": COLD_START_CONFIRMATIONS_REQUIRED,
        "would_defer": would_defer,
        "confirmed": confirmed,
        "outcome": outcome,
        "independent_override": independent_override,
        "enforcement_basis": enforcement_basis,
        "monitor_lineage": monitor_lineage,
        "lineage_status": lineage_status,
        "process_cycle": process_cycle,
        "original_decision": {
            "action": action,
            "sub_action": sub_action,
            "reason": reason,
            "guidance": decision.get("guidance"),
            **(
                {"nearest_edge": nearest_edge}
                if "nearest_edge" in decision
                else {}
            ),
        },
        "note": (
            "Shadow evaluation only: the original policy decision is unchanged. "
            "actuation_* describes only fallback-risk-pause deferral, not the "
            "runtime circuit breaker recorded in envelope.enforcement. Confirmation "
            "deferral fails closed until its state is durable and atomic."
        ),
    }


def apply_non_authored_cold_start_guard(
    decision: Mapping[str, Any],
    *,
    epistemic_class: Any,
    enabled: bool,
) -> dict[str, Any]:
    """Downgrade a non-authoritative Phi cold-start pause to guidance.

    This is deliberately separate from the two-observation confirmation shadow.
    It has no counter and promotes none of that shadow's dormant actuation.  The
    guard asks a narrower authority question: may a non-agent-authored row, whose
    verdict is still owned by the non-discriminative Phi cold-start fallback,
    hard-pause the identity before it has authored a report?  When provenance is
    exact, the answer is no; the raw pause remains in audit/history while runtime
    enforcement receives ``proceed/guide``.

    Unknown or incomplete provenance fails closed and leaves the pause intact.
    Agent-authored reports, behaviorally ready rows, independent verification,
    incomplete hard-stop provenance, and any simultaneous non-risk hard stop are
    also untouched.
    """
    guarded = dict(decision)
    action = guarded.get("action")
    sub_action = guarded.get("sub_action")
    if not _is_risk_routed_pause(guarded):
        return guarded

    hard_stop_provenance = _validated_risk_only_hard_stop_provenance(guarded)
    maturity_gate = guarded.get("cold_start_confirmation")
    maturity_gate = maturity_gate if isinstance(maturity_gate, Mapping) else {}
    confidence = _finite_number(maturity_gate.get("behavioral_confidence"))
    primary_driver = maturity_gate.get("primary_driver")
    primary_eisv_source = maturity_gate.get("primary_eisv_source")
    measurement_ready = maturity_gate.get("measurement_ready")
    is_baselined = maturity_gate.get("is_baselined")
    independent_override = maturity_gate.get("independent_override")
    required_maturity_fields = {
        "schema",
        "policy_candidate",
        "provenance_complete",
        "hard_stop_provenance",
        "primary_driver",
        "primary_eisv_source",
        "measurement_ready",
        "is_baselined",
        "behavioral_confidence",
        "independent_override",
    }
    epistemic_class_known = (
        isinstance(epistemic_class, str)
        and epistemic_class in _KNOWN_EPISTEMIC_CLASSES
    )
    non_authoring = epistemic_class in NON_AUTHORING_EPISTEMIC_CLASSES

    if not enabled:
        ineligibility_reason = "guard_disabled"
    elif not epistemic_class_known:
        ineligibility_reason = "epistemic_class_missing_or_unknown"
    elif not non_authoring:
        ineligibility_reason = "agent_authored_report"
    elif not maturity_gate:
        ineligibility_reason = "maturity_provenance_missing"
    elif not required_maturity_fields.issubset(maturity_gate):
        ineligibility_reason = "maturity_provenance_incomplete"
    elif maturity_gate.get("schema") != "eisv.cold-start-confirmation.v1":
        ineligibility_reason = "maturity_schema_unknown"
    elif hard_stop_provenance is None:
        ineligibility_reason = "hard_stop_provenance_missing_or_not_risk_only"
    elif maturity_gate.get("policy_candidate") is not True:
        ineligibility_reason = "maturity_policy_not_risk_only"
    elif maturity_gate.get("hard_stop_provenance") != hard_stop_provenance:
        ineligibility_reason = "hard_stop_provenance_mismatch"
    elif maturity_gate.get("provenance_complete") is not True:
        ineligibility_reason = "maturity_provenance_incomplete"
    elif independent_override is not None:
        ineligibility_reason = "independent_override"
    elif primary_driver != "phi_cold_start":
        ineligibility_reason = "verdict_source_not_phi_cold_start"
    elif primary_eisv_source != "ode_fallback":
        ineligibility_reason = "eisv_source_not_ode_fallback"
    elif confidence is None:
        ineligibility_reason = "behavioral_confidence_missing"
    elif measurement_ready is not False:
        ineligibility_reason = (
            "behavioral_measurement_ready"
            if measurement_ready is True
            else "measurement_readiness_missing"
        )
    elif is_baselined is not False:
        ineligibility_reason = (
            "behavioral_baseline_present"
            if is_baselined is True
            else "baseline_status_missing"
        )
    elif confidence >= BEHAVIORAL_AUTHORITY_THRESHOLD:
        ineligibility_reason = "behavioral_measurement_ready"
    else:
        ineligibility_reason = None

    applied = ineligibility_reason is None
    original_reason = guarded.get("reason")
    epistemic_gate = {
        "schema": NON_AUTHORED_COLD_START_GUARD_SCHEMA,
        "enabled": bool(enabled),
        "applied": applied,
        "ineligibility_reason": ineligibility_reason,
        "epistemic_class": epistemic_class,
        "agent_authored": epistemic_class == "agent_report",
        "non_authoring": non_authoring,
        "primary_driver": primary_driver,
        "primary_eisv_source": primary_eisv_source,
        "measurement_ready": measurement_ready,
        "is_baselined": is_baselined,
        "behavioral_confidence": confidence,
        "behavioral_authority_threshold": BEHAVIORAL_AUTHORITY_THRESHOLD,
        "independent_override": independent_override,
        "hard_stop_provenance": (
            dict(hard_stop_provenance)
            if isinstance(hard_stop_provenance, Mapping)
            else None
        ),
        "enforcement_basis": (
            NON_AUTHORED_COLD_START_ENFORCEMENT_BASIS if applied else None
        ),
        "original_decision": {
            "action": action,
            "sub_action": sub_action,
            "reason": original_reason,
            "guidance": guarded.get("guidance"),
            **(
                {"nearest_edge": guarded.get("nearest_edge")}
                if "nearest_edge" in guarded
                else {}
            ),
        },
        "note": (
            "A non-agent-authored Phi cold-start fallback is advisory until "
            "agent-authored or behaviorally authoritative evidence exists."
            if applied else
            "Guard did not apply; the original policy decision remains intact."
        ),
    }
    guarded["cold_start_epistemic_gate"] = epistemic_gate
    if not applied:
        return guarded

    guarded["original_action"] = action
    guarded["original_sub_action"] = sub_action
    guarded["cold_start_epistemic_deferred"] = True
    guarded["action"] = "proceed"
    guarded["sub_action"] = "guide"
    guarded["reason"] = (
        "non-authored Phi cold-start pause deferred to guidance "
        f"(epistemic_class={epistemic_class}, "
        f"behavioral_confidence={confidence:.3f}; was: {original_reason})"
    )
    guarded["guidance"] = (
        "Treat this fallback estimate as advisory. Hard-pause authority remains "
        "available to agent-authored, behaviorally ready, independently verified, "
        "structural, and runtime-safety evidence."
    )
    return guarded


def evaluate_non_authored_cold_start_trap(
    state_record: Any,
    *,
    enabled: bool,
) -> dict[str, Any]:
    """Recognize the exact persisted provenance of the legacy recovery trap.

    A paused identity cannot write a new state row, so its frozen cold-start risk
    can make reviewed recovery impossible.  This evaluator permits the review
    handler to discount that one risk check only when the latest persisted row
    proves that a non-authored, first-observation Phi fallback was actually
    enforced.  Every field is matched explicitly; missing or contradictory
    evidence fails closed.
    """
    epistemic_class = _record_field(state_record, "epistemic_class")
    state_json = _record_field(state_record, "state_json")
    state_json = state_json if isinstance(state_json, Mapping) else {}
    state_epistemic_class = state_json.get("epistemic_class")
    telemetry = state_json.get("eisv_telemetry")
    telemetry = telemetry if isinstance(telemetry, Mapping) else {}
    policy = telemetry.get("policy_evaluation")
    policy = policy if isinstance(policy, Mapping) else {}
    policy_inputs = policy.get("inputs")
    policy_inputs = policy_inputs if isinstance(policy_inputs, Mapping) else {}
    maturity_gate = policy.get("maturity_gate")
    maturity_gate = maturity_gate if isinstance(maturity_gate, Mapping) else {}
    policy_hard_stop_provenance = policy.get("hard_stop_provenance")
    persisted_decision = {
        "action": policy.get("action"),
        "sub_action": policy.get("sub_action"),
        "nearest_edge": policy_inputs.get("nearest_edge"),
        "hard_stop_provenance": policy_hard_stop_provenance,
    }
    validated_hard_stop_provenance = (
        _validated_risk_only_hard_stop_provenance(persisted_decision)
    )
    enforcement = telemetry.get("enforcement")
    enforcement = enforcement if isinstance(enforcement, Mapping) else {}
    confidence = _finite_number(maturity_gate.get("behavioral_confidence"))

    circuit_breaker_applied = (
        enforcement.get("requested") is True
        and enforcement.get("applied") is True
        and enforcement.get("mode") == "circuit_breaker"
        and enforcement.get("actor") == "agent_loop_detection"
        and enforcement.get("effect") == "agent_metadata.status=paused"
    )
    legacy_enforcement_basis_exact = (
        enforcement.get("basis") == "phi_cold_start_unconfirmed_shadow"
    )

    requirements = {
        "guard_enabled": bool(enabled),
        "state_record_present": state_record is not None,
        "epistemic_class_non_authoring": (
            epistemic_class in NON_AUTHORING_EPISTEMIC_CLASSES
        ),
        "epistemic_class_consistent": (
            isinstance(state_epistemic_class, str)
            and state_epistemic_class == epistemic_class
        ),
        "telemetry_schema_exact": telemetry.get("schema") == "eisv.telemetry.v1",
        "policy_was_risk_pause": (
            policy.get("action") == "pause"
            and policy.get("sub_action") == "risk_pause"
            and not policy.get("suppression")
        ),
        "policy_source_was_phi_fallback": (
            policy_inputs.get("verdict_source") == "phi_cold_start"
            and policy_inputs.get("primary_eisv_source") == "ode_fallback"
        ),
        "risk_only_hard_stop_provenance_exact": (
            validated_hard_stop_provenance is not None
            and maturity_gate.get("hard_stop_provenance")
            == validated_hard_stop_provenance
        ),
        "maturity_gate_exact": (
            maturity_gate.get("schema") == "eisv.cold-start-confirmation.v1"
            and maturity_gate.get("outcome") == "shadow_would_defer"
            and maturity_gate.get("eligible") is True
            and maturity_gate.get("would_defer") is True
            and maturity_gate.get("confirmed") is False
            and maturity_gate.get("confirmation_count") == 1
            and maturity_gate.get("confirmations_required") == 2
            and maturity_gate.get("primary_driver") == "phi_cold_start"
            and maturity_gate.get("primary_eisv_source") == "ode_fallback"
            and maturity_gate.get("measurement_ready") is False
            and maturity_gate.get("is_baselined") is False
            and maturity_gate.get("independent_override") is None
            and maturity_gate.get("lineage_status") == "identity_genesis"
            and confidence is not None
            and confidence < BEHAVIORAL_AUTHORITY_THRESHOLD
        ),
        # Keep the factual actuation observation separate from the narrow
        # legacy-exception basis.  Previously this predicate folded both
        # together, so a real circuit breaker with a different basis appeared
        # as ``circuit_breaker_applied=false`` in recovery diagnostics.
        "circuit_breaker_applied": circuit_breaker_applied,
        "legacy_enforcement_basis_exact": legacy_enforcement_basis_exact,
    }
    failed_requirements = [
        name for name, satisfied in requirements.items() if not satisfied
    ]
    eligible = not failed_requirements
    return {
        "schema": NON_AUTHORED_COLD_START_RECOVERY_SCHEMA,
        "eligible": eligible,
        "recovery_basis": (
            NON_AUTHORED_COLD_START_RECOVERY_BASIS if eligible else None
        ),
        "epistemic_class": epistemic_class,
        "behavioral_confidence": confidence,
        "observed_enforcement": {
            "requested": enforcement.get("requested"),
            "applied": enforcement.get("applied"),
            "mode": enforcement.get("mode"),
            "basis": enforcement.get("basis"),
            "scope": enforcement.get("scope"),
            "actor": enforcement.get("actor"),
            "effect": enforcement.get("effect"),
            "actuation_id": enforcement.get("actuation_id"),
            "applied_at": enforcement.get("applied_at"),
            "circuit_breaker_applied": circuit_breaker_applied,
        },
        "failed_requirements": failed_requirements,
        "requirements": requirements,
        "note": (
            "Latest persisted row proves the legacy non-authored Phi cold-start "
            "circuit-breaker trap; reviewed recovery may discount frozen risk only."
            if eligible else
            (
                "Persisted provenance records an applied circuit breaker, but does "
                "not match the narrow legacy non-authored recovery exception."
                if circuit_breaker_applied else
                "Persisted provenance does not record an applied circuit breaker or "
                "authorize a cold-start recovery exception."
            )
        ),
    }


def _record_field(record: Any, field: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(field)
    return getattr(record, field, None)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
