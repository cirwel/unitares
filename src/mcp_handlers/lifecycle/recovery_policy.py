"""Shared recovery policy helpers.

Recovery is an actuator decision.  The public ``coherence`` scalar is retained
for compatibility and audit context, but its legacy producer is ``C(V)``: ODE
control feedback derived from the signed void coordinate.  It is therefore not
health evidence and must not authorize or deny a recovery action.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from src.coherence_provenance import (
    LEGACY_COHERENCE_SOURCE,
    coherence_role_for_source,
)


RECOVERY_POLICY_SCHEMA = "recovery.authority.v2"


NO_RISK_AUTHORITY_EDGE = "no_risk_authority"

# Three states a recovery gate must tell apart.  Only the first carries a number
# a gate may compare against; the other two are different kinds of absence, and
# collapsing them into one scalar is the defect this module exists to prevent.
RISK_AUTHORITY_RESOLVED = "resolved"  # a risk some verdict was actually made from
RISK_AUTHORITY_UNMEASURED = "unmeasured"  # no check-in has ever landed
RISK_AUTHORITY_LOST = "lost"  # measured, but the decision pair is gone


@dataclass(frozen=True)
class RiskAuthority:
    """A resolved risk reading together with why it is or is not usable."""

    risk: float | None
    state: str

    @property
    def is_resolved(self) -> bool:
        return self.state == RISK_AUTHORITY_RESOLVED

    @property
    def is_unmeasured(self) -> bool:
        return self.state == RISK_AUTHORITY_UNMEASURED

    @property
    def is_lost(self) -> bool:
        return self.state == RISK_AUTHORITY_LOST


def read_risk_authority(metrics: Mapping[str, Any]) -> RiskAuthority:
    """Read decision risk without promoting Φ trend telemetry.

    ``risk`` is populated only in the ``resolved`` state: the pair that produced
    the last verdict.  ``phi_history`` is an honest read fallback but must not
    gate recovery, and nothing here ever falls through to ``current_risk`` or
    ``mean_risk``.

    The two absent states are deliberately NOT the same, and neither is a low
    reading:

    ``unmeasured``
        No check-in has ever landed, so there is no risk to compare against.
        Recovery paths already decline to risk-gate this case on purpose — a
        paused identity cannot author a state row, so gating it on a number it
        never produced is a trap it can never climb out of.

    ``lost``
        The agent HAS measured; the risk paired with its last verdict did not
        survive a restart and could not be restored from the durable record.
        Substituting a scalar here is the defect this module exists to prevent:
        at the recovery thresholds every plausible midpoint (0.5 against a 0.65
        self-recovery limit and a 0.60 auto-resume gate) reads as "safe enough
        to resume", so the agent would be resumed — unattended, by the stuck
        sweep — on a number no verdict was ever made from.  Gates must refuse.
    """
    source = metrics.get("risk_score_source")
    value = metrics.get("risk_score")

    if source not in (None, RISK_AUTHORITY_RESOLVED):
        # A named non-resolved source (today: `phi_history`) means the monitor
        # has Φ history, so it has measured, and only the authority is missing.
        return RiskAuthority(None, RISK_AUTHORITY_LOST)

    if value is None:
        # No source and no headline risk: an uninitialized monitor, or a durable
        # row that carries no risk at all.  Nothing was ever measured.
        return RiskAuthority(None, RISK_AUTHORITY_UNMEASURED)

    try:
        risk = float(value)
    except (TypeError, ValueError):
        return RiskAuthority(None, RISK_AUTHORITY_LOST)
    if not math.isfinite(risk):
        return RiskAuthority(None, RISK_AUTHORITY_LOST)

    return RiskAuthority(min(1.0, max(0.0, risk)), RISK_AUTHORITY_RESOLVED)


def render_risk(risk_score: float | None, places: int = 3) -> str:
    """Format a risk reading for a human-readable line.

    A missing reading renders as ``unavailable`` rather than a number, so an
    audit trail never records a risk the agent never had.
    """
    if risk_score is None:
        return "unavailable"
    return f"{risk_score:.{places}f}"


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
    risk_score: float | None,
    void_active: bool,
    max_risk: float,
    tight_width: float = 0.15,
) -> dict[str, Any]:
    """Return recovery headroom without importing legacy ``C(V)`` semantics.

    The shape intentionally resembles the historical proprioceptive-margin
    response so clients can continue displaying ``margin`` and ``nearest_edge``.
    Only inputs that are valid recovery evidence participate in the result.

    ``risk_score=None`` (no resolved reading) yields ``margin="unknown"`` rather
    than a headroom number.  "Unknown" is deliberately neither ``comfortable``
    nor ``critical``: absence of a reading is not evidence that an agent is
    safe, and it is not evidence that it is stuck either.  Detection rules keyed
    on a specific margin therefore do not fire on it, and authorization rules
    must refuse it explicitly.
    """
    if risk_score is None:
        return {
            "schema": "recovery.margin.v2",
            "margin": "critical" if void_active else "unknown",
            "nearest_edge": "void_active" if void_active else NO_RISK_AUTHORITY_EDGE,
            "distance": None,
            "inputs": {
                "risk_score": None,
                "max_risk": max_risk,
                "void_active": void_active,
            },
            "excluded_inputs": {
                "coherence": {
                    "reason": "legacy_tanh_v_is_control_feedback_not_health_evidence"
                },
                "risk_score": {"reason": "no_resolved_risk_paired_with_a_verdict"},
            },
        }

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
