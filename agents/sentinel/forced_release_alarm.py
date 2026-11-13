"""
Sentinel forced-release alarm rule (RFC v0.8 §7.10 + §7.11.5).

Polls Core's read-only `GET /v1/lease-plane/alarm-events` endpoint for three
distinct event classes:

  - Ad-hoc forced releases (`event_type='forced'`): one alarm per event.
    Per RFC §7.10 alarm-on-every-event semantic — operator-typed force-release
    is rare enough that per-event auditing is the right signal.

  - Deprecation sweeps (`event_type='lease.deprecation_swept'`): batched
    alarms grouped by `deprecation_id`, one summary per completed batch.
    Per RFC §7.11.5 batch suppression — bulk sweeps could otherwise fire
    hundreds of alarms in minutes, drowning the channel.

  - Held-by-other conflicts (`event_type='conflict_held_by_other'`): batched
    per cycle by `surface_id`, one summary per surface.

Cursor state: callers persist `last_event_ts` so successive polls don't
re-emit alarms for already-seen events.

Boundary contract: this client module never imports `asyncpg` or `src.*` and
never opens a direct PostgreSQL connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

import httpx

from agents.common.http_client import (
    build_auth_headers,
    resolve_governance_base_url,
)

# Surfaces minted under this namespace are reserved for destructive integration
# tests — e.g. tests/test_lease_plane_force_release_contract.py, which performs
# a REAL force-release against the live router to exercise the §9 auth contract.
RESERVED_TEST_SURFACE_PREFIX = "td:/test/"

_SUPPRESSED_TEST_SURFACE_PREFIXES = (
    RESERVED_TEST_SURFACE_PREFIX,
    "td:/force-release-contract-test-",
    "dialectic:/test_elixir_",
    "resident:/test_elixir_",
)

ALARM_POLL_HTTP_TIMEOUT_S = 15.0


def _is_reserved_test_surface(surface_id: Any) -> bool:
    """True if `surface_id` is a test fixture surface (reserved or legacy)."""
    return isinstance(surface_id, str) and surface_id.startswith(
        _SUPPRESSED_TEST_SURFACE_PREFIXES
    )


def _as_utc_datetime(value: Any) -> datetime | None:
    """Coerce a `datetime` or ISO-8601 string into a UTC `datetime` (`+00:00`)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.endswith("Z") or cleaned.endswith("z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    raise TypeError(f"Expected datetime or ISO string, got {type(value).__name__}")


@dataclass
class ForcedReleaseAlarm:
    """One alarm produced by `poll_forced_release_alarms`."""

    kind: str  # "ad_hoc" | "deprecation_batch" | "conflict_batch"
    severity: str  # "high" | "medium"
    summary: str
    fingerprint: str
    extra: dict[str, Any] = field(default_factory=dict)


def classify_alarm_events(
    payload: Mapping[str, Any],
    *,
    last_event_ts: datetime | None = None,
) -> tuple[list[ForcedReleaseAlarm], datetime | None]:
    """Classify JSON event rows from `/v1/lease-plane/alarm-events` into alarms."""
    alarms: list[ForcedReleaseAlarm] = []
    max_ts = _as_utc_datetime(last_event_ts)

    for raw_row in payload.get("ad_hoc") or []:
        row = dict(raw_row)
        row["ts"] = _as_utc_datetime(row.get("ts"))
        row_ts = row["ts"]
        if _is_reserved_test_surface(row.get("surface_id")):
            if row_ts is not None and (max_ts is None or row_ts > max_ts):
                max_ts = row_ts
            continue
        alarms.append(_ad_hoc_alarm(row))
        if row_ts is not None and (max_ts is None or row_ts > max_ts):
            max_ts = row_ts

    for raw_row in payload.get("deprecation_batches") or []:
        row = dict(raw_row)
        row["first_ts"] = _as_utc_datetime(row.get("first_ts"))
        row["last_ts"] = _as_utc_datetime(row.get("last_ts"))
        row["sweep_completed_at"] = _as_utc_datetime(row.get("sweep_completed_at"))
        alarms.append(_batch_alarm(row))
        row_last = row["last_ts"]
        if row_last is not None and (max_ts is None or row_last > max_ts):
            max_ts = row_last

    for raw_row in payload.get("conflict_batches") or []:
        row = dict(raw_row)
        row["first_ts"] = _as_utc_datetime(row.get("first_ts"))
        row["last_ts"] = _as_utc_datetime(row.get("last_ts"))
        row_last = row["last_ts"]
        if _is_reserved_test_surface(row.get("surface_id")):
            if row_last is not None and (max_ts is None or row_last > max_ts):
                max_ts = row_last
            continue
        alarms.append(_conflict_alarm(row))
        if row_last is not None and (max_ts is None or row_last > max_ts):
            max_ts = row_last

    return alarms, max_ts


async def poll_forced_release_alarms(
    *,
    last_event_ts: datetime | None,
    api_url: str | None = None,
    api_token: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[list[ForcedReleaseAlarm], datetime | None]:
    """Poll Core's `/v1/lease-plane/alarm-events` endpoint and classify alarms."""
    base_url = resolve_governance_base_url(api_url)
    headers = build_auth_headers(api_token)
    params: dict[str, str] = {}
    if last_event_ts is not None:
        normalized_since = _as_utc_datetime(last_event_ts)
        assert normalized_since is not None
        params["since"] = normalized_since.isoformat()

    url = f"{base_url}/v1/lease-plane/alarm-events"
    if http_client is not None:
        resp = await http_client.get(
            url, headers=headers, params=params, timeout=ALARM_POLL_HTTP_TIMEOUT_S,
        )
    else:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url, headers=headers, params=params, timeout=ALARM_POLL_HTTP_TIMEOUT_S,
            )

    if resp.status_code == 404:
        raise RuntimeError(
            f"GET /v1/lease-plane/alarm-events returned 404 at {base_url} "
            "(Core server predates the lease-plane telemetry route)"
        )
    if resp.status_code == 401:
        raise RuntimeError(
            "GET /v1/lease-plane/alarm-events returned 401 Unauthorized "
            "(check UNITARES_MCP_BEARER_TOKEN / UNITARES_HTTP_API_TOKEN)"
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"GET /v1/lease-plane/alarm-events failed: HTTP {resp.status_code} "
            f"{resp.text[:200]}"
        )

    body = resp.json()
    if not isinstance(body, dict) or not body.get("success"):
        raise RuntimeError(f"GET /v1/lease-plane/alarm-events rejected: {body!r}")

    return classify_alarm_events(body, last_event_ts=last_event_ts)


def _ad_hoc_alarm(row: Mapping[str, Any]) -> ForcedReleaseAlarm:
    """Build a per-event ad-hoc forced-release alarm."""
    surface_id = row["surface_id"]
    ts = _as_utc_datetime(row.get("ts"))
    return ForcedReleaseAlarm(
        kind="ad_hoc",
        severity="high",
        summary=f"forced release: {surface_id} (lease {row.get('lease_id')})",
        fingerprint=f"forced_release:ad_hoc:{row['event_id']}",
        extra={
            "event_id": str(row["event_id"]),
            "ts": ts.isoformat() if ts else None,
            "lease_id": str(row["lease_id"]) if row.get("lease_id") else None,
            "surface_id": surface_id,
            "surface_kind": row["surface_kind"],
        },
    )


def _conflict_alarm(row: Mapping[str, Any]) -> ForcedReleaseAlarm:
    """Build a per-surface batched alarm for held-by-other conflicts."""
    surface_id = row["surface_id"]
    count = int(row["event_count"])
    first_ts = _as_utc_datetime(row.get("first_ts"))
    last_ts = _as_utc_datetime(row.get("last_ts"))
    if last_ts is None:
        raise ValueError("conflict_batch row requires non-null last_ts")
    return ForcedReleaseAlarm(
        kind="conflict_batch",
        severity="medium",
        summary=f"held-by-other conflicts: {surface_id} (count={count})",
        fingerprint=f"forced_release:conflict_batch:{surface_id}:{last_ts.isoformat()}",
        extra={
            "surface_id": surface_id,
            "surface_kind": row["surface_kind"],
            "count": count,
            "first_ts": first_ts.isoformat() if first_ts else None,
            "last_ts": last_ts.isoformat(),
        },
    )


def _batch_alarm(row: Mapping[str, Any]) -> ForcedReleaseAlarm:
    """Build a single summary alarm for a completed deprecation batch."""
    depr_id = row["deprecation_id"]
    kind = row["surface_kind"]
    count = int(row["event_count"])
    first_ts = _as_utc_datetime(row.get("first_ts"))
    last_ts = _as_utc_datetime(row.get("last_ts"))
    swept_ts = _as_utc_datetime(row.get("sweep_completed_at"))
    return ForcedReleaseAlarm(
        kind="deprecation_batch",
        severity="medium",
        summary=f"deprecation sweep complete: kind={kind} count={count}",
        fingerprint=f"forced_release:deprecation_batch:{depr_id}",
        extra={
            "deprecation_id": str(depr_id),
            "kind": kind,
            "count": count,
            "first_ts": first_ts.isoformat() if first_ts else None,
            "last_ts": last_ts.isoformat() if last_ts else None,
            "sweep_completed_at": swept_ts.isoformat() if swept_ts else None,
        },
    )
