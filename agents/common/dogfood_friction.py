"""Dogfood friction finding envelopes for CI/CD issue surfacing.

Fresh dogfood episodes are high-sensitivity probes. This module turns their
observations into a canonical `/api/findings` event payload while keeping the
layers separate: the event carries route hints for issue surfacing, KG sediment,
dialectic review, and CI gates, but does not perform those downstream actions.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agents.common.findings import compute_change_token, compute_fingerprint, post_finding

REQUIRED_FIELDS = ("surface", "attempted_action", "expected", "observed")
VALID_SEVERITIES = ("low", "medium", "high", "critical")
CI_GATE_ACTIONS = frozenset({"block", "cooldown", "rollback"})
DEFAULT_AGENT_ID = "dogfood-friction"
DEFAULT_AGENT_NAME = "Dogfood Friction Probe"
EVENT_TYPE = "dogfood_friction_finding"


class DogfoodFrictionValidationError(ValueError):
    """Raised when a dogfood friction envelope is missing required evidence."""


def _clean_text(value: Any) -> str:
    """Return a stripped string for JSON-ish input values."""
    if value is None:
        return ""
    return str(value).strip()


def _clean_bool(value: Any, *, default: bool = False) -> bool:
    """Coerce common JSON/string boolean shapes without treating text as truthy."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _clean_int(value: Any, *, default: int = 0) -> int:
    """Coerce a non-negative integer, falling back to ``default`` on bad input."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(parsed, 0)


def normalize_dogfood_friction(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a dogfood friction observation.

    Required fields describe the measured friction itself. Optional fields add
    evidence, recurrence, and routing context. The result is deterministic so
    CI jobs can compare, hash, and dedupe repeated observations.
    """
    normalized: dict[str, Any] = {}
    missing: list[str] = []

    for field in REQUIRED_FIELDS:
        text = _clean_text(payload.get(field))
        if not text:
            missing.append(field)
        normalized[field] = text

    if missing:
        joined = ", ".join(missing)
        raise DogfoodFrictionValidationError(f"dogfood friction missing required field(s): {joined}")

    severity = _clean_text(payload.get("severity") or "low").lower()
    if severity not in VALID_SEVERITIES:
        allowed = ", ".join(VALID_SEVERITIES)
        raise DogfoodFrictionValidationError(
            f"dogfood friction severity {severity!r} must be one of: {allowed}"
        )

    normalized.update(
        {
            "kind": "dogfood_friction",
            "fresh_agent_context": _clean_text(payload.get("fresh_agent_context")),
            "evidence_uri": _clean_text(payload.get("evidence_uri")),
            "repro_command": _clean_text(payload.get("repro_command")),
            "workaround_used": _clean_text(payload.get("workaround_used")),
            "severity": severity,
            "reproducible": _clean_bool(payload.get("reproducible"), default=False),
            "recurrence_count": _clean_int(payload.get("recurrence_count"), default=0),
            "ambiguous": _clean_bool(payload.get("ambiguous"), default=False),
            "policy_question": _clean_bool(payload.get("policy_question"), default=False),
            "proposed_action": _clean_text(payload.get("proposed_action")).lower(),
            "source": _clean_text(payload.get("source") or "dogfood"),
        }
    )
    normalized["routes"] = route_dogfood_friction(normalized)
    normalized["boundary_note"] = (
        "Issue surfacing is measurement/instrumentation; KG is durable sediment; "
        "dialectic is contested-meaning review; CI/CD is the actuator layer."
    )
    return normalized


def route_dogfood_friction(friction: Mapping[str, Any]) -> list[str]:
    """Return ordered route hints without performing downstream writes.

    Order is intentionally policy-shaped: possible CI gate first, then ordinary
    issue surfacing, then durable KG sediment, then dialectic for ambiguity.
    """
    severity = _clean_text(friction.get("severity")).lower()
    proposed_action = _clean_text(friction.get("proposed_action")).lower()
    routes: list[str] = []

    if severity == "critical" or proposed_action in CI_GATE_ACTIONS:
        routes.append("ci_gate")

    if _clean_bool(friction.get("reproducible"), default=False):
        routes.append("issue_surface")

    if _clean_int(friction.get("recurrence_count"), default=0) >= 2:
        routes.append("kg_note")

    if (
        _clean_bool(friction.get("ambiguous"), default=False)
        or _clean_bool(friction.get("policy_question"), default=False)
    ):
        routes.append("dialectic_request")

    if not routes:
        routes.append("local_observation")
    return routes


def build_dogfood_friction_event(
    payload: Mapping[str, Any],
    *,
    agent_id: str = DEFAULT_AGENT_ID,
    agent_name: str = DEFAULT_AGENT_NAME,
) -> dict[str, Any]:
    """Build kwargs suitable for ``agents.common.findings.post_finding``.

    ``fingerprint`` is the durable dedup identity: same surface + attempted
    action + observed friction should collapse across CI runs even when the
    evidence artifact URL changes. ``change_token`` includes the full normalized
    evidence shape, so the event stream can emit when the condition changes.
    """
    friction = normalize_dogfood_friction(payload)
    fingerprint = compute_fingerprint(
        [
            EVENT_TYPE,
            friction["surface"],
            friction["attempted_action"],
            friction["observed"],
        ]
    )
    message = (
        f"[{friction['kind']}] {friction['surface']}: "
        f"{friction['attempted_action']} -> {friction['observed']}"
    )
    change_token = compute_change_token(
        {
            "type": EVENT_TYPE,
            "severity": friction["severity"],
            "message": message,
            "extra": friction,
        }
    )
    return {
        "event_type": EVENT_TYPE,
        "severity": friction["severity"],
        "message": message,
        "agent_id": _clean_text(agent_id) or DEFAULT_AGENT_ID,
        "agent_name": _clean_text(agent_name) or DEFAULT_AGENT_NAME,
        "fingerprint": fingerprint,
        "change_token": change_token,
        "extra": friction,
    }


def post_dogfood_friction(
    payload: Mapping[str, Any],
    *,
    agent_id: str = DEFAULT_AGENT_ID,
    agent_name: str = DEFAULT_AGENT_NAME,
) -> bool:
    """Post a dogfood friction finding through the shared findings helper."""
    event = build_dogfood_friction_event(
        payload,
        agent_id=agent_id,
        agent_name=agent_name,
    )
    return post_finding(**event)
