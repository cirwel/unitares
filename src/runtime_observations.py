"""Identity-bound host observations (legacy ``runtime`` API namespace).

This module stores factual host evidence such as a completed-tool rollup or a
hook-parent-process heartbeat.  These observations are deliberately written to
the audit plane, never ``core.agent_state``: neither a hook receipt nor a live
shared host PID proves that a Codex agent is continuously running.  They must
not synthesize EISV, progress, intent, or an agent-authored check-in.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any


RUNTIME_EVENT_PREFIX = "runtime_observation."
VALID_KINDS = {"activity_rollup", "heartbeat"}
RUNTIME_EVENT_TYPES = tuple(RUNTIME_EVENT_PREFIX + kind for kind in sorted(VALID_KINDS))
RUNTIME_RECENT_SECONDS = 3600.0
EXECUTION_MODES = {"interactive", "automation", "ephemeral", "unknown"}
EXECUTION_MODE_SOURCES = {
    "explicit_env",
    "hook_payload",
    "session_metadata",
    "unspecified",
}
RESTORATION_CONTEXT_KEYS = (
    "task_label",
    "task_outcome",
    "comparison_key",
    "memory_context",
    "harness_type",
    "model_provider",
    "model",
    "transport",
    "tool_surface",
)
_SLOT_HASH_RE = re.compile(r"^[0-9a-f]{12,64}$")
_HOST_RE = re.compile(r"^[a-z0-9_.-]{1,32}$")
_EVENT_NAMESPACE = uuid.UUID("d4bb38de-7a45-4f5a-82df-4864eac82e9d")


class RuntimeObservationError(ValueError):
    """A caller-visible runtime observation validation error."""

    def __init__(
        self, message: str, *, status_code: int = 400, code: str = "invalid_observation"
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _bounded_reflection_context(value: Any) -> dict[str, Any]:
    """Return only compact, explicitly persisted restoration context."""
    state = _as_mapping(value)
    context = _as_mapping(state.get("provenance_context"))
    bounded: dict[str, Any] = {}
    for key in RESTORATION_CONTEXT_KEYS:
        item = context.get(key)
        if isinstance(item, str) and item.strip():
            bounded[key] = item.strip()[:240]
        elif isinstance(item, (list, tuple)):
            values = [str(part).strip()[:80] for part in item if str(part).strip()]
            if values:
                bounded[key] = values[:10]
    action = state.get("action")
    if isinstance(action, str) and action.strip():
        bounded["governance_action"] = action.strip()[:40]
    return bounded


def _restoration_capsule(
    process: dict[str, Any],
    reflection: dict[str, Any],
    *,
    last_host_observation: datetime,
    last_tool_activity: datetime | None,
    last_heartbeat: datetime | None,
) -> dict[str, Any]:
    """Cross-link bounded facts without collapsing their provenance."""
    last_reflection = reflection.get("last_reflection")
    reflection_context = _bounded_reflection_context(
        reflection.get("last_reflection_state")
    )
    missing: list[str] = []
    if last_reflection is None:
        missing.append("agent_authored_checkin")
    if not reflection_context:
        missing.append("authored_task_context")
    if not process.get("latest_event_id"):
        missing.append("host_observation_reference")

    if last_reflection is None:
        relationship = "host_observation_only"
    elif last_tool_activity and last_tool_activity > last_reflection:
        relationship = "tool_events_after_agent_report"
    elif last_host_observation > last_reflection:
        relationship = "host_observation_after_agent_report"
    else:
        relationship = "agent_report_current"

    return {
        "schema": "unitares.restoration_capsule.v2",
        "process_id": f"{process['agent_id']}:{process['slot_hash']}",
        "generated_from": "bounded_host_observation_and_state_update_evidence",
        "execution": {
            "mode": process["execution_mode"],
            "mode_source": process["execution_mode_source"],
            "host_family": process["host_family"],
            "model": process["model"],
            "slot_hash": process["slot_hash"],
            "plugin_version": process["plugin_version"],
        },
        "host_observation": {
            "last_observed_at": _iso(last_host_observation),
            "last_tool_activity_at": _iso(last_tool_activity),
            "last_heartbeat_at": _iso(last_heartbeat),
            "latest_kind": process["latest_kind"],
            "event_id": process["latest_event_id"],
            "observation_count": process["observation_count"],
            "tool_count": process["tool_count"],
            "tools_in_window": process["tools_in_window"],
            "hook_parent_process_observed_alive": process[
                "hook_parent_process_observed_alive"
            ],
            "host_process_scope": "hook_parent",
        },
        "reflection": {
            "last_authored_at": _iso(last_reflection),
            "count": reflection.get("reflection_count", 0),
            "context": reflection_context,
        },
        "interpretation": {
            "last_at": _iso(reflection.get("last_interpretation")),
            "count": reflection.get("interpretation_count", 0),
            "agent_authored": False,
        },
        "initialization": {
            "last_at": _iso(reflection.get("last_bootstrap")),
            "count": reflection.get("bootstrap_count", 0),
            "agent_authored": False,
        },
        "continuity": {
            "relationship": relationship,
            "missing": missing,
            "restore_basis": (
                "host_observation_and_authored_context"
                if last_reflection and reflection_context
                else "host_observation_only"
            ),
        },
    }


def summarize_runtime_activity(
    events: list[dict[str, Any]],
    reflection_rows: list[Any],
    *,
    now: datetime | None = None,
    window_hours: float = 24.0,
) -> dict[str, Any]:
    """Build the dashboard's host-evidence/check-in read model honestly.

    Host observations remain audit evidence.  ``agent_report`` rows are the
    only agent-authored check-ins; substrate interpretations, initialization
    rows, and historical unclassified check-ins remain separately exposed.
    In particular, a heartbeat can prove only that the hook's parent PID was
    alive.  Codex desktop may share that PID across chats, so it never promotes
    a slot into "active agent" state.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    reflections: dict[str, dict[str, Any]] = {}
    for row in reflection_rows:
        agent_id = str(_row_value(row, "agent_id") or "").strip()
        if not agent_id:
            continue
        reflections[agent_id] = {
            "label": _row_value(row, "label"),
            "last_reflection": _as_utc(_row_value(row, "last_reflection_at")),
            "reflection_count": int(_row_value(row, "reflection_count", 0) or 0),
            "last_interpretation": _as_utc(_row_value(row, "last_interpretation_at")),
            "interpretation_count": int(
                _row_value(row, "interpretation_count", 0) or 0
            ),
            "last_bootstrap": _as_utc(_row_value(row, "last_bootstrap_at")),
            "bootstrap_count": int(_row_value(row, "bootstrap_count", 0) or 0),
            "last_unclassified": _as_utc(_row_value(row, "last_unclassified_at")),
            "last_reflection_state": _row_value(row, "last_reflection_state"),
        }

    processes: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        agent_id = str(event.get("agent_id") or "").strip()
        slot_hash = str(details.get("slot_hash") or "unknown").strip()
        if not agent_id:
            continue
        observed = _as_utc(details.get("observed_at") or event.get("timestamp"))
        if observed is None:
            continue
        key = (agent_id, slot_hash)
        process = processes.setdefault(
            key,
            {
                "agent_id": agent_id,
                "slot_hash": slot_hash,
                "host_family": str(details.get("host_family") or "unknown"),
                "plugin_version": str(details.get("plugin_version") or ""),
                "execution_mode": "unknown",
                "execution_mode_source": "unspecified",
                "model": "",
                "observation_count": 0,
                "tool_count": 0,
                "tools_in_window": 0,
                "last_host_observation": None,
                "last_tool_activity": None,
                "last_heartbeat": None,
                "latest_kind": None,
                "latest_event_id": None,
                "hook_parent_process_observed_alive": False,
                "seconds_since_last_tool": None,
            },
        )
        process["observation_count"] += 1
        process["tool_count"] = max(
            process["tool_count"],
            _bounded_int(details.get("tool_count"), maximum=10_000_000),
        )
        kind = str(details.get("observation_kind") or "")
        if kind == "activity_rollup":
            process["tools_in_window"] += _bounded_int(
                details.get("tool_delta"), maximum=1_000_000
            )
            if (
                process["last_tool_activity"] is None
                or observed > process["last_tool_activity"]
            ):
                process["last_tool_activity"] = observed
        if (
            process["last_host_observation"] is None
            or observed > process["last_host_observation"]
        ):
            process["last_host_observation"] = observed
            process["latest_kind"] = kind or None
            process["host_family"] = str(details.get("host_family") or "unknown")
            process["plugin_version"] = str(details.get("plugin_version") or "")
            mode = str(details.get("execution_mode") or "unknown").strip().lower()
            process["execution_mode"] = mode if mode in EXECUTION_MODES else "unknown"
            source = (
                str(details.get("execution_mode_source") or "unspecified")
                .strip()
                .lower()
            )
            process["execution_mode_source"] = (
                source if source in EXECUTION_MODE_SOURCES else "unspecified"
            )
            process["model"] = str(details.get("model") or "")[:80]
            process["latest_event_id"] = (
                str(event.get("event_id")) if event.get("event_id") else None
            )
            process["seconds_since_last_tool"] = _optional_bounded_float(
                details.get("seconds_since_last_tool"), maximum=31_536_000.0
            )
        if kind == "heartbeat" and (
            process["last_heartbeat"] is None or observed > process["last_heartbeat"]
        ):
            process["last_heartbeat"] = observed
            process["hook_parent_process_observed_alive"] = (
                details.get("host_process_alive") is True
            )

    rows: list[dict[str, Any]] = []
    for process in processes.values():
        reflection = reflections.get(process["agent_id"], {})
        last_host_observation = process.pop("last_host_observation")
        last_tool_activity = process.pop("last_tool_activity")
        last_heartbeat = process.pop("last_heartbeat")
        last_reflection = reflection.get("last_reflection")
        host_observation_age = max(
            0.0, (current - last_host_observation).total_seconds()
        )
        tool_activity_age = (
            max(0.0, (current - last_tool_activity).total_seconds())
            if last_tool_activity
            else None
        )
        heartbeat_age = (
            max(0.0, (current - last_heartbeat).total_seconds())
            if last_heartbeat
            else None
        )
        reflection_age = (
            max(0.0, (current - last_reflection).total_seconds())
            if last_reflection
            else None
        )
        capsule = _restoration_capsule(
            process,
            reflection,
            last_host_observation=last_host_observation,
            last_tool_activity=last_tool_activity,
            last_heartbeat=last_heartbeat,
        )
        reflection_count = int(reflection.get("reflection_count", 0) or 0)
        interpretation_count = int(reflection.get("interpretation_count", 0) or 0)
        bootstrap_count = int(reflection.get("bootstrap_count", 0) or 0)
        if reflection_count:
            state_update_profile = "agent_report_present"
        elif interpretation_count:
            state_update_profile = "substrate_only"
        elif bootstrap_count:
            state_update_profile = "initialization_only"
        else:
            state_update_profile = "no_state_updates"
        tool_activity_after_report = bool(
            last_tool_activity
            and last_reflection
            and last_tool_activity > last_reflection
        )
        host_observation_after_report = bool(
            last_reflection and last_host_observation > last_reflection
        )
        heartbeat_recent = bool(
            heartbeat_age is not None and heartbeat_age <= RUNTIME_RECENT_SECONDS
        )
        rows.append(
            {
                **process,
                "process_id": f"{process['agent_id']}:{process['slot_hash']}",
                "agent_label": reflection.get("label"),
                "last_host_observation_at": _iso(last_host_observation),
                "last_tool_activity_at": _iso(last_tool_activity),
                "last_heartbeat_at": _iso(last_heartbeat),
                "host_observation_age_seconds": host_observation_age,
                "tool_activity_age_seconds": tool_activity_age,
                "heartbeat_age_seconds": heartbeat_age,
                "tool_activity_recent": bool(
                    tool_activity_age is not None
                    and tool_activity_age <= RUNTIME_RECENT_SECONDS
                ),
                "host_heartbeat_recent": heartbeat_recent,
                "host_process_scope": "hook_parent" if last_heartbeat else None,
                # Backward-compatible keys now use the conservative meaning:
                # only completed-tool evidence can mark a slot operational.
                "last_operational_at": _iso(last_tool_activity),
                "operational_age_seconds": tool_activity_age,
                "operational_recent": bool(
                    tool_activity_age is not None
                    and tool_activity_age <= RUNTIME_RECENT_SECONDS
                ),
                # Deprecated, intentionally conservative: the observer only
                # knows about a hook-parent PID, never a per-agent process.
                "host_process_alive": False,
                "last_reflection_at": _iso(last_reflection),
                "last_agent_report_at": _iso(last_reflection),
                "reflection_age_seconds": reflection_age,
                "reflection_count": reflection_count,
                "agent_report_count": reflection_count,
                "last_interpretation_at": _iso(reflection.get("last_interpretation")),
                "substrate_interpretation_count": interpretation_count,
                "last_bootstrap_at": _iso(reflection.get("last_bootstrap")),
                "bootstrap_count": bootstrap_count,
                "state_update_profile": state_update_profile,
                "last_unclassified_at": _iso(reflection.get("last_unclassified")),
                "restoration_capsule": capsule,
                "tool_activity_after_agent_report": tool_activity_after_report,
                "host_observation_after_agent_report": host_observation_after_report,
                "operational_after_reflection": tool_activity_after_report,
            }
        )

    rows.sort(key=lambda row: row["last_host_observation_at"] or "", reverse=True)
    host_observation_times = [
        parsed
        for row in rows
        if (parsed := _as_utc(row["last_host_observation_at"])) is not None
    ]
    tool_activity_times = [
        parsed
        for row in rows
        if (parsed := _as_utc(row["last_tool_activity_at"])) is not None
    ]
    last_host_observation = max(host_observation_times, default=None)
    last_tool_activity = max(tool_activity_times, default=None)
    last_reflection = max(
        (r["last_reflection"] for r in reflections.values() if r["last_reflection"]),
        default=None,
    )
    return {
        "success": True,
        "window_hours": window_hours,
        "generated_at": current.isoformat(),
        "summary": {
            "processes": len(rows),
            "observed_slots": len(rows),
            "agents": len({row["agent_id"] for row in rows}),
            "recent_processes": sum(1 for row in rows if row["operational_recent"]),
            "recent_tool_activity_slots": sum(
                1 for row in rows if row["tool_activity_recent"]
            ),
            "recent_host_heartbeat_slots": sum(
                1 for row in rows if row["host_heartbeat_recent"]
            ),
            "observations": sum(row["observation_count"] for row in rows),
            "processes_after_reflection": sum(
                1 for row in rows if row["operational_after_reflection"]
            ),
            "host_observations_after_agent_report": sum(
                1 for row in rows if row["host_observation_after_agent_report"]
            ),
            "slots_without_agent_report": sum(
                1 for row in rows if row["agent_report_count"] == 0
            ),
            "last_operational_at": _iso(last_tool_activity),
            "last_host_observation_at": _iso(last_host_observation),
            "last_reflection_at": _iso(last_reflection),
            "last_agent_report_at": _iso(last_reflection),
            "execution_modes": {
                mode: sum(1 for row in rows if row["execution_mode"] == mode)
                for mode in sorted(EXECUTION_MODES)
                if any(row["execution_mode"] == mode for row in rows)
            },
        },
        "processes": rows,
        "semantics": {
            "host_observation": (
                "identity-bound hook receipts or hook-parent PID heartbeats; "
                "never proof of continuous agent runtime and never EISV"
            ),
            "agent_checkin": "agent_state rows explicitly labeled agent_report",
            "turn_summary": (
                "substrate_interpretation rows, commonly Codex Stop summaries, "
                "remain non-agent-authored"
            ),
            "initialization": "synthetic bootstrap rows are not real check-ins",
            "compatibility": (
                "the /v1/runtime namespace and process/operational keys are legacy; "
                "host_process_alive is deliberately false; prefer scoped "
                "hook-parent-observed and tool-activity fields"
            ),
        },
    }


async def read_runtime_activity(
    *, window_hours: float = 24.0, limit: int = 1000
) -> dict[str, Any]:
    """Read recent runtime evidence plus authored-reflection clocks."""
    now = datetime.now(timezone.utc)
    start_time = (now - timedelta(hours=window_hours)).isoformat()
    from src.audit_db import query_audit_events_async

    events = await query_audit_events_async(
        event_types=list(RUNTIME_EVENT_TYPES),
        start_time=start_time,
        limit=limit,
        order="desc",
    )
    agent_ids = sorted(
        {str(event.get("agent_id")) for event in events if event.get("agent_id")}
    )
    reflection_rows: list[Any] = []
    if agent_ids:
        from src.db import get_db

        db = get_db()
        async with db.acquire() as conn:
            reflection_rows = await conn.fetch(
                """
                SELECT a.id AS agent_id,
                       a.label,
                       max(s.recorded_at) FILTER (
                           WHERE s.synthetic = false
                             AND s.epistemic_class = 'agent_report'
                       ) AS last_reflection_at,
                       count(s.state_id) FILTER (
                           WHERE s.synthetic = false
                             AND s.epistemic_class = 'agent_report'
                       ) AS reflection_count,
                       max(s.recorded_at) FILTER (
                           WHERE s.synthetic = false
                             AND s.epistemic_class = 'substrate_interpretation'
                       ) AS last_interpretation_at,
                       count(s.state_id) FILTER (
                           WHERE s.synthetic = false
                             AND s.epistemic_class = 'substrate_interpretation'
                       ) AS interpretation_count,
                       max(s.recorded_at) FILTER (
                           WHERE s.synthetic = true
                       ) AS last_bootstrap_at,
                       count(s.state_id) FILTER (
                           WHERE s.synthetic = true
                       ) AS bootstrap_count,
                       max(s.recorded_at) FILTER (
                           WHERE s.synthetic = false
                             AND s.epistemic_class IS NULL
                       ) AS last_unclassified_at,
                       (array_agg(s.state_json ORDER BY s.recorded_at DESC) FILTER (
                           WHERE s.synthetic = false
                             AND s.epistemic_class = 'agent_report'
                       ))[1] AS last_reflection_state
                FROM core.agents a
                LEFT JOIN core.identities i ON i.agent_id = a.id
                LEFT JOIN core.agent_state s ON s.identity_id = i.identity_id
                WHERE a.id = ANY($1::text[])
                GROUP BY a.id, a.label
                """,
                agent_ids,
            )
    return summarize_runtime_activity(
        events, reflection_rows, now=now, window_hours=window_hours
    )


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
        raise RuntimeObservationError(
            "'observed_at' must be an ISO 8601 string"
        ) from exc
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


def _normalize(
    payload: dict[str, Any],
) -> tuple[str, str, str, datetime, dict[str, Any]]:
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

    execution_mode = str(payload.get("execution_mode") or "unknown").strip().lower()
    if execution_mode not in EXECUTION_MODES:
        raise RuntimeObservationError(
            f"'execution_mode' must be one of {sorted(EXECUTION_MODES)}"
        )
    execution_mode_source = (
        str(payload.get("execution_mode_source") or "unspecified").strip().lower()
    )
    if execution_mode_source not in EXECUTION_MODE_SOURCES:
        raise RuntimeObservationError(
            f"'execution_mode_source' must be one of {sorted(EXECUTION_MODE_SOURCES)}"
        )
    if (execution_mode == "unknown") != (execution_mode_source == "unspecified"):
        raise RuntimeObservationError(
            "'execution_mode' and 'execution_mode_source' must either both be "
            "unknown/unspecified or both describe explicit provenance"
        )

    slot_hash = str(payload.get("slot_hash") or "").strip().lower()
    if not _SLOT_HASH_RE.fullmatch(slot_hash):
        raise RuntimeObservationError("missing or invalid 'slot_hash'")

    observed_at = _parse_observed_at(payload.get("observed_at"))
    normalized: dict[str, Any] = {
        "schema_version": 1,
        "observation_kind": kind,
        "host_family": host_family,
        "execution_mode": execution_mode,
        "execution_mode_source": execution_mode_source,
        "model": str(payload.get("model") or "").strip()[:80],
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
            "hook_parent_process_liveness"
            if kind == "heartbeat"
            else "completed_tool_event_receipts"
        ),
        "host_process_scope": "hook_parent" if kind == "heartbeat" else None,
        "session_activity_evidence": False,
        "agent_runtime_evidence": False,
        "agent_authored": False,
        "eisv_written": False,
    }
    if kind == "heartbeat":
        if payload.get("host_process_alive") is not True:
            raise RuntimeObservationError(
                "heartbeat requires 'host_process_alive': true"
            )
        normalized["host_process_alive"] = True
    else:
        normalized["session_activity_evidence"] = normalized["tool_delta"] > 0

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
    """Validate, identity-bind, and persist one host observation."""
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

    # Only a non-empty completed-tool rollup is evidence that this Codex slot
    # did work. A heartbeat may observe a shared app-server parent long after a
    # chat/worktree ended, so it must never prolong the identity session.
    session_activity_refreshed = False
    if normalized["session_activity_evidence"]:
        try:
            session_activity_refreshed = bool(
                await db.update_session_activity(session_id)
            )
        except Exception:
            pass

    return {
        "success": True,
        "event_id": event_id,
        "event_type": RUNTIME_EVENT_PREFIX + normalized["observation_kind"],
        "agent_uuid": agent_uuid,
        "client_session_id": session_id,
        "epistemic_class": "substrate_observation",
        "measurement_scope": normalized["measurement_scope"],
        "session_activity_refreshed": session_activity_refreshed,
        "agent_runtime_evidence": False,
        "eisv_written": False,
    }
