"""Server-owned read-only lease-plane alarm event queries (`GET /v1/lease-plane/alarm-events`).

Exposes the three RFC v0.8 §7.10 / §7.11.5 event classes (`forced`,
`lease.deprecation_swept` completed batches, and `conflict_held_by_other`
per-surface batches) from Core's PostgreSQL pool inside a single
`repeatable_read` read-only transaction so remote monitoring clients never open
direct PostgreSQL connections.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

STATEMENT_TIMEOUT_SQL = "SET LOCAL statement_timeout = '15s'"

AD_HOC_FORCED_SQL = """
    SELECT event_id, ts, lease_id, surface_id, surface_kind, payload
    FROM lease_plane.lease_plane_events
    WHERE event_type = 'forced'
    {ts_filter}
    ORDER BY ts
"""

DEPRECATION_BATCH_SQL = """
    SELECT
        ds.deprecation_id, ds.surface_kind, ds.sweep_completed_at,
        count(e.event_id) AS event_count,
        min(e.ts) AS first_ts, max(e.ts) AS last_ts
    FROM lease_plane.lease_plane_events e
    JOIN lease_plane.deprecated_schemes ds
      ON ds.deprecation_id::text = e.payload->>'deprecation_id'
    WHERE e.event_type = 'lease.deprecation_swept'
      AND ds.sweep_completed_at IS NOT NULL
      {ts_filter}
    GROUP BY ds.deprecation_id, ds.surface_kind, ds.sweep_completed_at
"""

CONFLICT_BATCH_SQL = """
    SELECT
        surface_id, surface_kind,
        count(event_id) AS event_count,
        min(ts) AS first_ts, max(ts) AS last_ts
    FROM lease_plane.lease_plane_events
    WHERE event_type = 'conflict_held_by_other'
    {ts_filter}
    GROUP BY surface_id, surface_kind
"""


def parse_since_param(raw: str | None) -> datetime | None:
    """Parse an optional ISO-8601 `since` query parameter into a tz-aware `datetime`.

    Raises `ValueError` on malformed strings or naive timestamps (missing UTC
    offset) so callers never pass ambiguous local timestamps to PostgreSQL.
    """
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z") or cleaned.endswith("z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError("'since' must include an explicit timezone offset (e.g. 'Z' or '+00:00')")
    # Guard against wild offsets too
    offset = dt.tzinfo.utcoffset(dt)
    if offset is not None and abs(offset) > timedelta(hours=24):
        raise ValueError("'since' timezone offset out of range")
    return dt


def _normalize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


async def fetch_lease_alarm_events_from_conn(
    conn: Any,
    *,
    since: datetime | None = None,
) -> dict[str, Any]:
    """Query all three lease-plane alarm event classes inside one snapshot."""
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute(STATEMENT_TIMEOUT_SQL)
        db_now = await conn.fetchval("SELECT now()")
        polled_at = _iso_or_none(db_now)

        if since is None:
            ad_hoc_rows = await conn.fetch(AD_HOC_FORCED_SQL.format(ts_filter=""))
            batch_rows = await conn.fetch(DEPRECATION_BATCH_SQL.format(ts_filter=""))
            conflict_rows = await conn.fetch(CONFLICT_BATCH_SQL.format(ts_filter=""))
        else:
            ad_hoc_rows = await conn.fetch(
                AD_HOC_FORCED_SQL.format(ts_filter="AND ts > $1"),
                since,
            )
            batch_rows = await conn.fetch(
                DEPRECATION_BATCH_SQL.format(ts_filter="AND ds.sweep_completed_at > $1"),
                since,
            )
            conflict_rows = await conn.fetch(
                CONFLICT_BATCH_SQL.format(ts_filter="AND ts > $1"),
                since,
            )

    ad_hoc = [
        {
            "event_id": str(row["event_id"]),
            "ts": _iso_or_none(row["ts"]),
            "lease_id": str(row["lease_id"]) if row["lease_id"] else None,
            "surface_id": row["surface_id"],
            "surface_kind": row["surface_kind"],
            "payload": _normalize_json_value(row["payload"]) if row["payload"] is not None else {},
        }
        for row in ad_hoc_rows
    ]

    deprecation_batches = [
        {
            "deprecation_id": str(row["deprecation_id"]),
            "surface_kind": row["surface_kind"],
            "sweep_completed_at": _iso_or_none(row["sweep_completed_at"]),
            "event_count": int(row["event_count"]),
            "first_ts": _iso_or_none(row["first_ts"]),
            "last_ts": _iso_or_none(row["last_ts"]),
        }
        for row in batch_rows
    ]

    conflict_batches = [
        {
            "surface_id": row["surface_id"],
            "surface_kind": row["surface_kind"],
            "event_count": int(row["event_count"]),
            "first_ts": _iso_or_none(row["first_ts"]),
            "last_ts": _iso_or_none(row["last_ts"]),
        }
        for row in conflict_rows
    ]

    return {
        "polled_at": polled_at,
        "since": _iso_or_none(since),
        "ad_hoc": ad_hoc,
        "deprecation_batches": deprecation_batches,
        "conflict_batches": conflict_batches,
    }


async def fetch_lease_alarm_events(*, since: datetime | None = None) -> dict[str, Any]:
    """Acquire a connection from Core's pool and fetch lease-plane alarm events."""
    from src import agent_storage

    db = agent_storage.get_db()
    async with db.acquire() as conn:
        return await fetch_lease_alarm_events_from_conn(conn, since=since)
