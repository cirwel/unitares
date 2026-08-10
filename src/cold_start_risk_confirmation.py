"""Observational maturity gate for fallback-owned cold-start risk pauses.

The deployed monitor can produce ``risk_pause`` while behavioral confidence is
below its authority threshold.  In that window the verdict is owned by the Phi
cold-start prior, which the result envelope already labels non-discriminative.

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
    policy_candidate = action == "pause" and sub_action == "risk_pause"
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
        },
        "note": (
            "Shadow evaluation only: the original policy decision is unchanged. "
            "Actuation fails closed until confirmation state is durable and atomic."
        ),
    }


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
