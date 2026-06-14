"""Resident validation cohort envelopes for long-running UNITARES probes.

The v0 cohort is deliberately narrow: residents can observe, emit findings,
leave KG sediment, or request dialectic review, but they cannot deploy, merge,
or force-push. This module creates deterministic tick envelopes that can be
written locally, sent through process_agent_update, or consumed by a future
supervisor without giving the resident direct actuator authority.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

VALID_ROLES = frozenset({"dogfood_probe", "steward", "builder", "reviewer"})
DEFAULT_ALLOWED_EFFECTS = ("emit_finding", "leave_kg_note", "request_dialectic")
FORBIDDEN_EFFECTS = frozenset({"deploy", "merge", "force_push", "rollback"})


@dataclass(frozen=True)
class ResidentProfile:
    """Static identity and authority boundary for one validation resident."""

    cohort_id: str
    resident_id: str
    resident_name: str
    role: str
    cadence_seconds: int
    observation_scope: tuple[str, ...]
    allowed_effects: tuple[str, ...] = field(default=DEFAULT_ALLOWED_EFFECTS)
    deploy_authority: bool = False

    def __post_init__(self) -> None:
        """Validate v0 resident bounds before any envelope is emitted."""
        if not self.cohort_id.strip():
            raise ValueError("cohort_id is required")
        if not self.resident_id.strip():
            raise ValueError("resident_id is required")
        if not self.resident_name.strip():
            raise ValueError("resident_name is required")
        if self.role not in VALID_ROLES:
            raise ValueError(f"role must be one of {sorted(VALID_ROLES)}")
        if self.cadence_seconds <= 0:
            raise ValueError("cadence_seconds must be positive")
        if not self.observation_scope:
            raise ValueError("observation_scope must not be empty")
        forbidden = sorted(set(self.allowed_effects) & FORBIDDEN_EFFECTS)
        if self.deploy_authority or forbidden:
            detail = ", ".join(forbidden) if forbidden else "deploy_authority=True"
            raise ValueError(f"v0 validation residents have no deploy authority: {detail}")


def stable_tick_id(cohort_id: str, resident_id: str, tick_index: int) -> str:
    """Return a stable 16-hex tick id for cohort/resident/index identity."""
    if tick_index < 0:
        raise ValueError("tick_index must be non-negative")
    raw = f"{cohort_id}|{resident_id}|{tick_index}"
    return "rv_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _ensure_utc(dt: datetime) -> datetime:
    """Return a timezone-aware UTC datetime without changing absolute time."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_tick_envelope(
    profile: ResidentProfile,
    *,
    tick_index: int,
    observation: str,
    prediction: str,
    confidence: float,
    now: datetime | None = None,
    horizon_seconds: int | None = None,
    decision_action: str = "observe",
    outcome: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one resident-validation tick envelope.

    The envelope is a measurement artifact: it names the resident, heartbeat,
    prediction, bounded authority, and optional outcome, but it does not perform
    downstream KG/dialectic/CI/GitHub actions by itself.
    """
    if not observation.strip():
        raise ValueError("observation is required")
    if not prediction.strip():
        raise ValueError("prediction is required")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")

    observed_at = _ensure_utc(now or datetime.now(timezone.utc))
    horizon = horizon_seconds or profile.cadence_seconds
    next_due = observed_at + timedelta(seconds=profile.cadence_seconds)
    envelope: dict[str, Any] = {
        "event_type": "resident_validation_tick",
        "cohort_id": profile.cohort_id,
        "tick_id": stable_tick_id(profile.cohort_id, profile.resident_id, tick_index),
        "tick_index": tick_index,
        "resident": {
            "id": profile.resident_id,
            "name": profile.resident_name,
            "role": profile.role,
            "observation_scope": list(profile.observation_scope),
        },
        "heartbeat": {
            "cadence_seconds": profile.cadence_seconds,
            "observed_at": observed_at.isoformat(),
            "next_due_at": next_due.isoformat(),
        },
        "prediction": {
            "claim": prediction,
            "confidence": float(confidence),
            "horizon_seconds": horizon,
            "decision_action": decision_action,
        },
        "observation": observation,
        "authority": {
            "allowed_effects": list(profile.allowed_effects),
            "deploy_authority": profile.deploy_authority,
            "forbidden_effects": sorted(FORBIDDEN_EFFECTS),
        },
    }
    if outcome is not None:
        envelope["outcome"] = outcome
    return envelope


def build_process_update_kwargs(envelope: dict[str, Any]) -> dict[str, Any]:
    """Build process_agent_update kwargs for a resident-validation tick."""
    resident = envelope["resident"]
    prediction = envelope["prediction"]
    tick_index = envelope["tick_index"]
    summary = (
        f"{resident['name']} tick {tick_index} observed without governed mutation"
    )
    return {
        "response_text": (
            f"Resident validation tick {envelope['tick_id']}: "
            f"{envelope['observation']} Prediction: {prediction['claim']}"
        ),
        "task_type": "exploration",
        "complexity": 0.2,
        "confidence": prediction["confidence"],
        "epistemic_class": "prediction",
        "provenance_context": {
            "harness_type": "resident_validation_cohort",
            "cohort_id": envelope["cohort_id"],
            "resident_role": resident["role"],
            "tick_id": envelope["tick_id"],
            "governance_mode": "bounded_observation",
        },
        "recent_tool_results": [
            {
                "kind": "tool_call",
                "tool": "resident_validation_tick",
                "summary": summary,
            }
        ],
    }
