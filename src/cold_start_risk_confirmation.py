"""Observational maturity gate for fallback-owned cold-start risk pauses.

The deployed monitor can express the same risk-driven pause as ``risk_pause``
or as a CIRS ``cirs_block`` whose ``nearest_edge`` is ``risk``.  Either can
occur while behavioral confidence is below its authority threshold.  In that
window the verdict is owned by the Phi cold-start prior, which the result
envelope already labels non-discriminative.

This module does not actuate.  It evaluates, in shadow, whether a pause would be
the first or second adjacent fallback-owned observation and returns a fully
serializable provenance record.  Actuation remains fail-closed until the
confirmation state can be durably and atomically persisted across the policy to
runtime boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from numbers import Real
from typing import Any


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


def _is_fallback_risk_policy_candidate(decision: Mapping[str, Any]) -> bool:
    """Match only policy decisions whose authority comes from fallback risk.

    ``risk_pause`` is the direct verdict path.  CIRS can route the same risk
    score through ``cirs_block``; ``nearest_edge='risk'`` is the decision
    producer's exact trigger attribution.  Every other CIRS edge stays outside
    this authority guard and therefore fails closed.
    """
    if decision.get("action") != "pause":
        return False
    sub_action = decision.get("sub_action")
    return sub_action == "risk_pause" or (
        sub_action == "cirs_block" and decision.get("nearest_edge") == "risk"
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
    policy_candidate = _is_fallback_risk_policy_candidate(decision)
    measurement_ready = (
        confidence is not None
        and confidence >= BEHAVIORAL_AUTHORITY_THRESHOLD
    )
    enabled = bool(shadow_enabled or actuation_enabled)
    provenance_complete = (
        confidence is not None
        and isinstance(primary_driver, str)
        and bool(primary_driver)
        and isinstance(action, str)
        and bool(action.strip())
        and isinstance(sub_action, str)
        and bool(sub_action.strip())
        and isinstance(reason, str)
        and bool(reason.strip())
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
    elif not policy_candidate:
        ineligibility_reason = "policy_not_risk_pause"
    elif not provenance_complete:
        ineligibility_reason = "provenance_incomplete"
    elif independent_override:
        ineligibility_reason = "independent_override"
    elif measurement_ready:
        ineligibility_reason = "behavioral_measurement_ready"
    elif primary_driver != "phi_cold_start":
        ineligibility_reason = "verdict_source_not_phi_cold_start"
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
        "policy_candidate": policy_candidate,
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
    and every policy path other than direct ``risk_pause`` or risk-attributed
    CIRS ``cirs_block`` are also untouched.
    """
    guarded = dict(decision)
    action = guarded.get("action")
    sub_action = guarded.get("sub_action")
    if not _is_fallback_risk_policy_candidate(guarded):
        return guarded

    maturity_gate = guarded.get("cold_start_confirmation")
    maturity_gate = maturity_gate if isinstance(maturity_gate, Mapping) else {}
    confidence = _finite_number(maturity_gate.get("behavioral_confidence"))
    primary_driver = maturity_gate.get("primary_driver")
    measurement_ready = maturity_gate.get("measurement_ready")
    independent_override = maturity_gate.get("independent_override")
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
    elif independent_override:
        ineligibility_reason = "independent_override"
    elif primary_driver != "phi_cold_start":
        ineligibility_reason = "verdict_source_not_phi_cold_start"
    elif confidence is None:
        ineligibility_reason = "behavioral_confidence_missing"
    elif measurement_ready is not False:
        ineligibility_reason = (
            "behavioral_measurement_ready"
            if measurement_ready is True
            else "measurement_readiness_missing"
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
        "measurement_ready": measurement_ready,
        "behavioral_confidence": confidence,
        "behavioral_authority_threshold": BEHAVIORAL_AUTHORITY_THRESHOLD,
        "independent_override": independent_override,
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
        "maturity_gate_exact": (
            maturity_gate.get("schema") == "eisv.cold-start-confirmation.v1"
            and maturity_gate.get("outcome") == "shadow_would_defer"
            and maturity_gate.get("eligible") is True
            and maturity_gate.get("would_defer") is True
            and maturity_gate.get("confirmed") is False
            and maturity_gate.get("confirmation_count") == 1
            and maturity_gate.get("confirmations_required") == 2
            and maturity_gate.get("primary_driver") == "phi_cold_start"
            and maturity_gate.get("measurement_ready") is False
            and maturity_gate.get("independent_override") in (None, "")
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
