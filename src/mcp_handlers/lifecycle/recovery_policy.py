"""Shared recovery policy helpers.

Recovery is an actuator decision.  The public ``coherence`` scalar is retained
for compatibility and audit context, but its legacy producer is ``C(V)``: ODE
control feedback derived from the signed void coordinate.  It is therefore not
health evidence and must not authorize or deny a recovery action.
"""

from __future__ import annotations

from typing import Any, Iterable

from src.coherence_provenance import (
    LEGACY_COHERENCE_SOURCE,
    coherence_role_for_source,
)


RECOVERY_POLICY_SCHEMA = "recovery.authority.v2"


def recovery_policy_context(
    *,
    coherence: float,
    authoritative_inputs: Iterable[str],
    coherence_source: str | None = None,
    coherence_role: str | None = None,
) -> dict[str, Any]:
    """Describe which readings may influence the recovery decision."""
    source = coherence_source or LEGACY_COHERENCE_SOURCE
    role = coherence_role or coherence_role_for_source(source)
    return {
        "schema": RECOVERY_POLICY_SCHEMA,
        "authoritative_inputs": list(authoritative_inputs),
        "diagnostic_inputs": {
            "coherence": {
                "value": coherence,
                "source": source,
                "role": role,
                "authoritative": False,
                "health_evidence": False,
            }
        },
    }


def compute_recovery_margin(
    *,
    risk_score: float,
    void_active: bool,
    max_risk: float,
    tight_width: float = 0.15,
) -> dict[str, Any]:
    """Return recovery headroom without importing legacy ``C(V)`` semantics.

    The shape intentionally resembles the historical proprioceptive-margin
    response so clients can continue displaying ``margin`` and ``nearest_edge``.
    Only inputs that are valid recovery evidence participate in the result.
    """
    risk_distance = max_risk - risk_score
    if void_active:
        margin = "critical"
        nearest_edge = "void_active"
        distance = 0.0
    elif risk_distance < 0.0:
        margin = "critical"
        nearest_edge = "risk_score"
        distance = risk_distance
    elif risk_distance <= tight_width:
        margin = "tight"
        nearest_edge = "risk_score"
        distance = risk_distance
    else:
        margin = "comfortable"
        nearest_edge = "risk_score"
        distance = risk_distance

    return {
        "schema": "recovery.margin.v2",
        "margin": margin,
        "nearest_edge": nearest_edge,
        "distance": round(distance, 6),
        "inputs": {
            "risk_score": risk_score,
            "max_risk": max_risk,
            "void_active": void_active,
        },
        "excluded_inputs": {
            "coherence": {
                "reason": "legacy_tanh_v_is_control_feedback_not_health_evidence"
            }
        },
    }
