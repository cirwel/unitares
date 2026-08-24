"""Shared recovery policy helpers.

Recovery is an actuator decision.  The public ``coherence`` scalar is retained
for compatibility and audit context, but its legacy producer is ``C(V)``: ODE
control feedback derived from the signed void coordinate.  It is therefore not
health evidence and must not authorize or deny a recovery action.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

from src.coherence_provenance import (
    LEGACY_COHERENCE_SOURCE,
    coherence_role_for_source,
)


RECOVERY_POLICY_SCHEMA = "recovery.authority.v2"


def authoritative_risk_score(
    metrics: Mapping[str, Any],
    *,
    default: float = 0.5,
) -> float:
    """Read decision risk without promoting Φ trend telemetry.

    Current monitor metrics always expose ``risk_score_source``.  ``resolved``
    is the pair that produced the last verdict; ``phi_history`` is an honest
    read fallback but must not gate recovery.  Missing source is accepted for
    backward-compatible fixtures/older callers that already provide a headline
    ``risk_score``.  Invalid or non-authoritative readings use the caller's
    explicit default and never fall through to ``current_risk``/``mean_risk``.
    """
    source = metrics.get("risk_score_source")
    if source not in (None, "resolved"):
        return float(default)

    value = metrics.get("risk_score")
    if value is None:
        return float(default)
    try:
        risk = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(risk):
        return float(default)
    return min(1.0, max(0.0, risk))


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
