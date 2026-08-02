"""Identity-bound host runtime observations.

This module stores factual host evidence such as a completed-tool rollup or a
process-alive heartbeat.  Runtime observations are deliberately written to the
audit plane, never ``core.agent_state``: process liveness is not agent-authored
proprioception and must not synthesize EISV, progress, or intent.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any


RUNTIME_EVENT_PREFIX = "runtime_observation."
VALID_KINDS = {"activity_rollup", "heartbeat"}
_SLOT_HASH_RE = re.compile(r"^[0-9a-f]{12,64}$")
_HOST_RE = re.compile(r"^[a-z0-9_.-]{1,32}$")
_EVENT_NAMESPACE = uuid.UUID("d4bb38de-7a45-4f5a-82df-4864eac82e9d")


class RuntimeObservationError(ValueError):
    """A caller-visible runtime observation validation error."""

    def __init__(self, message: str, *, status_code: int = 400, code: str = "invalid_observation"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _bounded_int(value: Any, *, default: int = 0, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0, min(maximum, parsed))


def _bounded_float(value: Any, *, default: float = 0.0, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(maximum, parsed))


def _optional_bounded_float(value: Any, *, maximum: float) -> float | None:
    if value in (None, ""):
        return None
    return _bounded_float(value, maximum=maximum)


def _parse_observed_at(value: Any) -> datetime:
    if value in (None, ""):
        return datetime.now(timezone.utc)
    if not isinstance(value, str):
        raise RuntimeObservationError("'observed_at' must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeObservationError("'observed_at' must be an ISO 8601 string") from exc
    if parsed.tzinfo is None:
        raise RuntimeObservationError("'observed_at' must include a timezone")
    return parsed.astimezone(timezone.utc)


def _session_is_live(session: Any, now: datetime) -> bool:
    if not bool(getattr(session, "is_active", False)):
        return False
    expires_at = getattr(session, "expires_at", None)
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > now


def _normalize(payload: dict[str, Any]) -> tuple[str, str, str, datetime, dict[str, Any]]:
    raw_agent = str(payload.get("agent_uuid") or "").strip()
    try:
        agent_uuid = str(uuid.UUID(raw_agent))
    except (ValueError, AttributeError) as exc:
        raise RuntimeObservationError("'agent_uuid' must be a UUID") from exc

    client_session_id = str(payload.get("client_session_id") or "").strip()
    if not client_session_id or len(client_session_id) > 256:
        raise RuntimeObservationError("missing or invalid 'client_session_id'")

    kind = str(payload.get("observation_kind") or "").strip().lower()
    if kind not in VALID_KINDS:
        raise RuntimeObservationError(
            f"'observation_kind' must be one of {sorted(VALID_KINDS)}"
        )

    host_family = str(payload.get("host_family") or "codex").strip().lower()
    if not _HOST_RE.fullmatch(host_family):
        raise RuntimeObservationError("missing or invalid 'host_family'")

    slot_hash = str(payload.get("slot_hash") or "").strip().lower()
    if not _SLOT_HASH_RE.fullmatch(slot_hash):
        raise RuntimeObservationError("missing or invalid 'slot_hash'")

    observed_at = _parse_observed_at(payload.get("observed_at"))
    normalized: dict[str, Any] = {
        "schema_version": 1,
        "observation_kind": kind,
        "host_family": host_family,
        "slot_hash": slot_hash,
        "observed_at": observed_at.isoformat(),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "tool_count": _bounded_int(payload.get("tool_count"), maximum=10_000_000),
        "tool_delta": _bounded_int(payload.get("tool_delta"), maximum=1_000_000),
        "window_seconds": _bounded_float(
            payload.get("window_seconds"), maximum=31_536_000.0
        ),
        "seconds_since_last_tool": _optional_bounded_float(
            payload.get("seconds_since_last_tool"), maximum=31_536_000.0
        ),
        "plugin_version": str(payload.get("plugin_version") or "")[:64],
        "epistemic_class": "substrate_observation",
        "measurement_scope": (
            "host_process_liveness" if kind == "heartbeat" else "host_event_receipt"
        ),
        "agent_authored": False,
        "eisv_written": False,
    }
    if kind == "heartbeat":
        if payload.get("host_process_alive") is not True:
            raise RuntimeObservationError(
                "heartbeat requires 'host_process_alive': true"
            )
        normalized["host_process_alive"] = True

    raw_event_id = str(payload.get("event_id") or "").strip()
    if raw_event_id:
        try:
            event_id = str(uuid.UUID(raw_event_id))
        except ValueError as exc:
            raise RuntimeObservationError("'event_id' must be a UUID") from exc
    else:
        stable_key_parts = [
            agent_uuid,
            client_session_id,
            kind,
            normalized["observed_at"],
        ]
        if kind == "activity_rollup":
            stable_key_parts.append(str(normalized["tool_count"]))
        stable_key = "|".join(stable_key_parts)
        event_id = str(uuid.uuid5(_EVENT_NAMESPACE, stable_key))
    return agent_uuid, client_session_id, event_id, observed_at, normalized


async def record_runtime_observation(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate, identity-bind, and persist one host runtime observation."""
    if not isinstance(payload, dict):
        raise RuntimeObservationError("body must be a JSON object")

    agent_uuid, session_id, event_id, observed_at, normalized = _normalize(payload)

    from src.db import get_db

    db = get_db()
    session = await db.get_session(session_id)
    if session is None:
        raise RuntimeObservationError(
            "client session is not bound",
            status_code=409,
            code="session_unbound",
        )
    if getattr(session, "agent_id", None) != agent_uuid:
        raise RuntimeObservationError(
            "client session is bound to a different identity",
            status_code=409,
            code="identity_session_mismatch",
        )
    if not _session_is_live(session, datetime.now(timezone.utc)):
        raise RuntimeObservationError(
            "client session is inactive or expired",
            status_code=409,
            code="session_inactive",
        )

    canonical = json.dumps(
        {
            "agent_uuid": agent_uuid,
            "client_session_id": session_id,
            **normalized,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    raw_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    from src.audit_db import append_audit_event_async

    persisted = await append_audit_event_async(
        {
            "timestamp": observed_at,
            "event_id": event_id,
            "event_type": RUNTIME_EVENT_PREFIX + normalized["observation_kind"],
            "agent_id": agent_uuid,
            "session_id": session_id,
            # Keep host evidence out of audit confidence fallbacks used by
            # outcome calibration. Measurement certainty is encoded by the
            # explicit event type/scope, not the confidence column.
            "confidence": 0.0,
            "details": normalized,
        },
        raw_hash=raw_hash,
    )
    if not persisted:
        raise RuntimeObservationError(
            "runtime observation could not be persisted",
            status_code=503,
            code="persistence_failed",
        )

    # A verified host heartbeat is legitimate session activity. Keep the
    # identity binding alive without creating an EISV state row.
    try:
        await db.update_session_activity(session_id)
    except Exception:
        pass

    return {
        "success": True,
        "event_id": event_id,
        "event_type": RUNTIME_EVENT_PREFIX + normalized["observation_kind"],
        "agent_uuid": agent_uuid,
        "client_session_id": session_id,
        "epistemic_class": "substrate_observation",
        "eisv_written": False,
    }
