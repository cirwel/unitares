"""Discord bridge delivery and attention receipts.

These events describe what a presentation surface did with governance facts.
They are written to audit storage, not to the dashboard event ring, so the
Discord bridge does not re-ingest and re-post its own delivery receipts.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List
from uuid import uuid4

BRIDGE_EVENT_TYPES: dict[str, str] = {
    "bridge.delivery": "delivery",
    "bridge.delivery_failed": "delivery_failed",
    "bridge.suppressed": "suppressed",
    "bridge.rate_limited": "rate_limited",
    "bridge.ack": "ack",
    "bridge.command": "command",
}

BRIDGE_SURFACE = "discord"

_VALID_SEVERITIES = {
    "info",
    "low",
    "medium",
    "warning",
    "high",
    "critical",
}
_HIGH_ATTENTION_SEVERITIES = {"high", "critical"}
_MAX_STRING = 500
_MAX_EVENTS_LIMIT = 500


class BridgeEventError(ValueError):
    """Raised when a bridge receipt payload is invalid."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_str(value: Any, *, max_len: int = _MAX_STRING) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


def _clean_str_list(value: Any, *, max_items: int = 12) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    out: list[str] = []
    for item in value:
        cleaned = _clean_str(item, max_len=80)
        if cleaned and cleaned not in out:
            out.append(cleaned)
        if len(out) >= max_items:
            break
    return out


def _parse_datetime(value: Any, *, default: datetime | None = None) -> datetime:
    if value is None:
        return default or _now()
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    text = str(value).strip()
    if not text:
        return default or _now()
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise BridgeEventError(f"invalid timestamp: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_window(value: Any, *, default_hours: float) -> datetime:
    if value is None:
        return _now() - timedelta(hours=default_hours)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    text = str(value).strip()
    if not text:
        return _now() - timedelta(hours=default_hours)

    if len(text) >= 2 and text[-1] in {"d", "h", "m", "s"} and text[:-1].isdigit():
        amount = int(text[:-1])
        delta = {
            "d": timedelta(days=amount),
            "h": timedelta(hours=amount),
            "m": timedelta(minutes=amount),
            "s": timedelta(seconds=amount),
        }[text[-1]]
        return _now() - delta

    return _parse_datetime(text)


def _coerce_limit(value: Any, *, default: int = 100) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, _MAX_EVENTS_LIMIT))


def normalize_bridge_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize a bridge receipt payload."""
    if not isinstance(payload, dict):
        raise BridgeEventError("body must be a JSON object")

    event_type = _clean_str(payload.get("event_type") or payload.get("type"), max_len=80)
    if event_type not in BRIDGE_EVENT_TYPES:
        raise BridgeEventError(
            f"event_type must be one of {sorted(BRIDGE_EVENT_TYPES)}"
        )

    kind = BRIDGE_EVENT_TYPES[event_type]
    severity = _clean_str(
        payload.get("source_severity") or payload.get("severity") or "info",
        max_len=40,
    )
    severity = (severity or "info").lower()
    if severity not in _VALID_SEVERITIES:
        raise BridgeEventError(
            f"severity must be one of {sorted(_VALID_SEVERITIES)}"
        )

    source_event_id = _clean_str(payload.get("source_event_id"), max_len=160)
    discord_message_id = _clean_str(payload.get("discord_message_id"), max_len=80)
    command_name = _clean_str(payload.get("command_name"), max_len=120)

    if event_type == "bridge.ack" and not (source_event_id or discord_message_id):
        raise BridgeEventError(
            "bridge.ack requires source_event_id or discord_message_id"
        )
    if event_type == "bridge.command" and not command_name:
        raise BridgeEventError("bridge.command requires command_name")

    status = _clean_str(payload.get("status"), max_len=60)
    if not status:
        status = {
            "bridge.delivery": "delivered",
            "bridge.delivery_failed": "failed",
            "bridge.suppressed": "suppressed",
            "bridge.rate_limited": "rate_limited",
            "bridge.ack": "acked",
            "bridge.command": "commanded",
        }[event_type]

    event_id = _clean_str(payload.get("event_id"), max_len=160) or str(uuid4())
    timestamp = _parse_datetime(payload.get("timestamp"))
    bridge_id = _clean_str(payload.get("bridge_id"), max_len=160) or "discord-bridge"

    normalized: Dict[str, Any] = {
        "schema": "unitares.bridge_event.v1",
        "event_id": event_id,
        "event_type": event_type,
        "type": event_type,
        "kind": kind,
        "status": status,
        "surface": BRIDGE_SURFACE,
        "bridge_id": bridge_id,
        "timestamp": timestamp.isoformat(),
        "severity": severity,
    }

    optional_fields = {
        "source_event_id": source_event_id,
        "source_event_type": _clean_str(payload.get("source_event_type"), max_len=160),
        "source_agent_id": _clean_str(payload.get("source_agent_id"), max_len=160),
        "channel_key": _clean_str(payload.get("channel_key"), max_len=120),
        "discord_guild_id": _clean_str(payload.get("discord_guild_id"), max_len=80),
        "discord_channel_id": _clean_str(payload.get("discord_channel_id"), max_len=80),
        "discord_message_id": discord_message_id,
        "operator_id_hash": _clean_str(payload.get("operator_id_hash"), max_len=160),
        "command_name": command_name,
        "reason": _clean_str(payload.get("reason")),
        "message": _clean_str(payload.get("message")),
        "error": _clean_str(payload.get("error")),
    }
    for key, value in optional_fields.items():
        if value is not None:
            normalized[key] = value

    operator_roles = _clean_str_list(payload.get("operator_roles"))
    if operator_roles:
        normalized["operator_roles"] = operator_roles

    return normalized


async def record_bridge_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a bridge receipt to audit.events."""
    from src.audit_db import append_audit_event_async

    event = normalize_bridge_event(payload)
    persisted = await append_audit_event_async(
        {
            "timestamp": event["timestamp"],
            "event_id": event["event_id"],
            "event_type": event["event_type"],
            "agent_id": event["bridge_id"],
            "confidence": 1.0,
            "details": event,
        }
    )
    return {
        "success": bool(persisted),
        "persisted": bool(persisted),
        "event": event,
    }


def _row_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    details = row.get("details") if isinstance(row, dict) else None
    payload = details if isinstance(details, dict) else {}
    event_type = row.get("event_type") or payload.get("event_type")
    return {
        "event_id": row.get("event_id") or payload.get("event_id"),
        "event_type": event_type,
        "timestamp": row.get("timestamp") or payload.get("timestamp"),
        "bridge_id": row.get("agent_id") or payload.get("bridge_id"),
        "kind": payload.get("kind") or BRIDGE_EVENT_TYPES.get(str(event_type), "unknown"),
        "status": payload.get("status"),
        "severity": payload.get("severity") or "info",
        "source_event_id": payload.get("source_event_id"),
        "source_event_type": payload.get("source_event_type"),
        "source_agent_id": payload.get("source_agent_id"),
        "channel_key": payload.get("channel_key"),
        "discord_message_id": payload.get("discord_message_id"),
        "operator_id_hash": payload.get("operator_id_hash"),
        "operator_roles": payload.get("operator_roles") or [],
        "command_name": payload.get("command_name"),
        "reason": payload.get("reason"),
        "message": payload.get("message"),
        "error": payload.get("error"),
    }


def _event_match_keys(event: Dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for field in ("source_event_id", "discord_message_id"):
        value = event.get(field)
        if value:
            keys.add(f"{field}:{value}")
    return keys


def _sort_events_desc(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(events, key=lambda e: e.get("timestamp") or "", reverse=True)


async def build_bridge_summary(arguments: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Build an agent/operator-facing Discord bridge attention summary."""
    from src.audit_db import query_audit_events_async

    args = arguments or {}
    since_arg = args.get("since")
    until_arg = args.get("until")
    start_dt = _parse_window(since_arg, default_hours=24.0)
    end_dt = _parse_datetime(until_arg) if until_arg is not None else None
    limit = _coerce_limit(args.get("limit"), default=100)
    include_events = bool(args.get("include_events", True))

    rows = await query_audit_events_async(
        event_types=list(BRIDGE_EVENT_TYPES),
        start_time=start_dt.isoformat(),
        end_time=end_dt.isoformat() if end_dt else None,
        limit=limit,
        order="desc",
    )
    events = [_row_payload(row) for row in rows]

    by_event_type = Counter(str(e.get("event_type") or "<unknown>") for e in events)
    by_status = Counter(str(e.get("status") or "<unknown>") for e in events)
    by_channel = Counter(str(e.get("channel_key") or "<unknown>") for e in events)

    acked_keys: set[str] = set()
    for event in events:
        if event.get("event_type") == "bridge.ack":
            acked_keys.update(_event_match_keys(event))

    deliveries = [
        e for e in events
        if e.get("event_type") == "bridge.delivery"
    ]
    unacked_critical = []
    for event in deliveries:
        severity = str(event.get("severity") or "").lower()
        if severity not in _HIGH_ATTENTION_SEVERITIES:
            continue
        keys = _event_match_keys(event)
        ack_state = "unmatchable" if not keys else "acked" if keys & acked_keys else "unacked"
        if ack_state != "acked":
            item = dict(event)
            item["ack_state"] = ack_state
            unacked_critical.append(item)

    recent_failures = [
        e for e in events
        if e.get("event_type") in {"bridge.delivery_failed", "bridge.rate_limited"}
    ][:10]
    recent_suppressions = [
        e for e in events
        if e.get("event_type") == "bridge.suppressed"
    ][:10]
    recent_commands = [
        e for e in events
        if e.get("event_type") == "bridge.command"
    ][:10]

    summary: Dict[str, Any] = {
        "success": True,
        "surface": BRIDGE_SURFACE,
        "schema": "unitares.bridge_summary.v1",
        "window": {
            "since": start_dt.isoformat(),
            "until": end_dt.isoformat() if end_dt else None,
            "defaulted": since_arg is None,
        },
        "total_events": len(events),
        "by_event_type": dict(by_event_type),
        "by_status": dict(by_status),
        "by_channel": dict(by_channel),
        "unacked_critical_count": len(unacked_critical),
        "unacked_critical": unacked_critical[:20],
        "recent_failures": recent_failures,
        "recent_suppressions": recent_suppressions,
        "recent_commands": recent_commands,
        "limit_reached": len(rows) >= limit,
        "generated_at": _now().isoformat(),
    }
    if include_events:
        summary["events"] = _sort_events_desc(events)
    return summary
