"""
Sentinel forced-release alarm rule (RFC v0.8 §7.10 + §7.11.5).

Polls `lease_plane.lease_plane_events` for three distinct event classes:

  - Ad-hoc forced releases (`event_type='forced'`): one alarm per event.
    Per RFC §7.10 alarm-on-every-event semantic — operator-typed force-release
    is rare enough that per-event auditing is the right signal.

  - Deprecation sweeps (`event_type='lease.deprecation_swept'`): batched
    alarms grouped by `deprecation_id`, one summary per completed batch.
    Per RFC §7.11.5 batch suppression — bulk sweeps could otherwise fire
    hundreds of alarms in minutes, drowning the channel.

  - Held-by-other conflicts (`event_type='conflict_held_by_other'`): batched
    per cycle by `surface_id`, one summary per surface. Held-by-other is the
    advisory-mode normal-operation outcome (dispatch preflight + worker
    acquire on a busy surface), not a fault — but a sustained burst on one
    surface still warrants an operator signal. Per RFC §7.11.5 framing:
    higher-frequency events get batched per cycle, not per event.

Cursor state: callers persist `last_event_ts` so successive polls don't
re-emit alarms for already-seen events.

Authority: this module is read-only on `lease_plane.lease_plane_events`
and `lease_plane.deprecated_schemes`. It does NOT write to either table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

try:
    import asyncpg
except ImportError:  # pragma: no cover — surfaces at first call site
    asyncpg = None  # type: ignore


@dataclass
class ForcedReleaseAlarm:
    """One alarm produced by `poll_forced_release_alarms`.

    `kind` is `"ad_hoc"` (per-event for `event_type='forced'`),
    `"deprecation_batch"` (one per completed deprecation_id), or
    `"conflict_batch"` (one per surface_id per cycle for held-by-other bursts).
    `extra` carries the discriminator fields used for fingerprinting and
    downstream display.
    """

    kind: str  # "ad_hoc" | "deprecation_batch" | "conflict_batch"
    severity: str  # "high" | "medium"
    summary: str
    fingerprint: str
    extra: dict[str, Any] = field(default_factory=dict)


async def poll_forced_release_alarms(
    *,
    db_url: str,
    last_event_ts: datetime | None,
) -> tuple[list[ForcedReleaseAlarm], datetime | None]:
    """Return (alarms, new_cursor) since the previous poll.

    Args:
        db_url: PostgreSQL connection URL for the governance DB.
        last_event_ts: timestamp of the last event seen in the prior poll;
                       `None` on first run (poll everything).

    Returns:
        (alarms, new_cursor): list of alarms to emit; new cursor for state.
        new_cursor is the max(ts) across all events seen this poll, or
        last_event_ts unchanged if no new events.
    """
    if asyncpg is None:
        raise RuntimeError("asyncpg required for poll_forced_release_alarms")

    conn = await asyncpg.connect(db_url)
    try:
        return await _poll_inner(conn, last_event_ts)
    finally:
        await conn.close()


async def _poll_inner(
    conn, last_event_ts: datetime | None,
) -> tuple[list[ForcedReleaseAlarm], datetime | None]:
    alarms: list[ForcedReleaseAlarm] = []
    max_ts = last_event_ts

    # 1. Ad-hoc forced events: one alarm per event.
    ad_hoc_query = """
        SELECT event_id, ts, lease_id, surface_id, surface_kind, payload
        FROM lease_plane.lease_plane_events
        WHERE event_type = 'forced'
        {ts_filter}
        ORDER BY ts
    """
    if last_event_ts is None:
        rows = await conn.fetch(ad_hoc_query.format(ts_filter=""))
    else:
        rows = await conn.fetch(
            ad_hoc_query.format(ts_filter="AND ts > $1"), last_event_ts,
        )
    for row in rows:
        alarms.append(_ad_hoc_alarm(row))
        if max_ts is None or row["ts"] > max_ts:
            max_ts = row["ts"]

    # 2. Deprecation sweep batches: group by deprecation_id, one alarm per
    #    completed batch (sweep_completed_at IS NOT NULL).
    batch_query = """
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
    if last_event_ts is None:
        rows = await conn.fetch(batch_query.format(ts_filter=""))
    else:
        # Only emit the batch alarm for batches whose sweep COMPLETED after the
        # last poll — avoids re-emitting already-seen completed sweeps.
        rows = await conn.fetch(
            batch_query.format(ts_filter="AND ds.sweep_completed_at > $1"), last_event_ts,
        )
    for row in rows:
        alarms.append(_batch_alarm(row))
        # Cursor advances ONLY based on event ts, not sweep_completed_at.
        # PR 5 council fix: pre-PR-5 advanced cursor to MAX(last_ts, sweep_completed_at),
        # which mixes event-stream and table-metadata timestamps — causes
        # clock-skew/order-of-events fragility.
        if max_ts is None or row["last_ts"] > max_ts:
            max_ts = row["last_ts"]

    # 3. Held-by-other conflicts: group by surface_id within this poll cycle,
    #    one alarm per surface. Per RFC §7.11.5: higher-frequency events get
    #    batched per cycle (not per deprecation_id, since there's no batch
    #    identity to span cycles for). Cursor advances on event ts only.
    conflict_query = """
        SELECT
            surface_id, surface_kind,
            count(event_id) AS event_count,
            min(ts) AS first_ts, max(ts) AS last_ts
        FROM lease_plane.lease_plane_events
        WHERE event_type = 'conflict_held_by_other'
        {ts_filter}
        GROUP BY surface_id, surface_kind
    """
    if last_event_ts is None:
        rows = await conn.fetch(conflict_query.format(ts_filter=""))
    else:
        rows = await conn.fetch(
            conflict_query.format(ts_filter="AND ts > $1"), last_event_ts,
        )
    for row in rows:
        alarms.append(_conflict_alarm(row))
        if max_ts is None or row["last_ts"] > max_ts:
            max_ts = row["last_ts"]

    return alarms, max_ts


def _ad_hoc_alarm(row) -> ForcedReleaseAlarm:
    """Build a per-event ad-hoc forced-release alarm."""
    surface_id = row["surface_id"]
    return ForcedReleaseAlarm(
        kind="ad_hoc",
        severity="high",
        summary=f"forced release: {surface_id} (lease {row['lease_id']})",
        fingerprint=f"forced_release:ad_hoc:{row['event_id']}",
        extra={
            "event_id": str(row["event_id"]),
            "ts": row["ts"].isoformat() if row["ts"] else None,
            "lease_id": str(row["lease_id"]) if row["lease_id"] else None,
            "surface_id": surface_id,
            "surface_kind": row["surface_kind"],
        },
    )


def _conflict_alarm(row) -> ForcedReleaseAlarm:
    """Build a per-surface batched alarm for held-by-other conflicts.

    Fingerprint includes max_ts so a later cycle producing more conflicts on
    the same surface yields a distinct alarm — without that, downstream
    dedup would suppress the second burst.
    """
    surface_id = row["surface_id"]
    count = row["event_count"]
    last_ts = row["last_ts"]
    return ForcedReleaseAlarm(
        kind="conflict_batch",
        severity="medium",
        summary=f"held-by-other conflicts: {surface_id} (count={count})",
        fingerprint=f"forced_release:conflict_batch:{surface_id}:{last_ts.isoformat()}",
        extra={
            "surface_id": surface_id,
            "surface_kind": row["surface_kind"],
            "count": count,
            "first_ts": row["first_ts"].isoformat() if row["first_ts"] else None,
            "last_ts": last_ts.isoformat() if last_ts else None,
        },
    )


def _batch_alarm(row) -> ForcedReleaseAlarm:
    """Build a single summary alarm for a completed deprecation batch."""
    depr_id = row["deprecation_id"]
    kind = row["surface_kind"]
    count = row["event_count"]
    return ForcedReleaseAlarm(
        kind="deprecation_batch",
        severity="medium",
        summary=f"deprecation sweep complete: kind={kind} count={count}",
        fingerprint=f"forced_release:deprecation_batch:{depr_id}",
        extra={
            "deprecation_id": str(depr_id),
            "kind": kind,
            "count": count,
            "first_ts": row["first_ts"].isoformat() if row["first_ts"] else None,
            "last_ts": row["last_ts"].isoformat() if row["last_ts"] else None,
            "sweep_completed_at": row["sweep_completed_at"].isoformat()
                if row["sweep_completed_at"] else None,
        },
    )
