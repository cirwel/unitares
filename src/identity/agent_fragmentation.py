"""Read-only diagnostics for low-check-in identity fragmentation.

Fresh UUIDs are the intended process-instance identity boundary. This module
measures the failure mode around that boundary: identities that exist but have
no measured trajectory, or only a very small number of real check-ins.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.db import get_db


IDENTITY_FRAGMENTATION_SQL = """
WITH per_identity AS (
    SELECT
        i.identity_id,
        i.agent_id,
        i.status,
        i.created_at,
        i.updated_at,
        i.last_activity_at,
        i.parent_agent_id,
        i.spawn_reason,
        i.provisional_lineage,
        i.chain_obs_count,
        COALESCE(
            NULLIF(i.metadata->>'label', ''),
            NULLIF(i.metadata->>'display_name', ''),
            NULLIF(a.label, ''),
            ''
        ) AS label,
        COALESCE(NULLIF(i.metadata->>'model_type', ''), '') AS model_type,
        COALESCE(NULLIF(i.metadata->>'purpose', ''), '') AS purpose,
        COALESCE(NULLIF(i.metadata->>'thread_id', ''), NULLIF(a.thread_id, ''), '') AS thread_id,
        NULLIF(i.metadata->>'active_session_key', '') AS active_session_key,
        CASE
            WHEN (i.metadata->>'total_updates') ~ '^[0-9]+$'
            THEN (i.metadata->>'total_updates')::INT
            ELSE NULL
        END AS metadata_total_updates,
        COUNT(s.*) FILTER (WHERE s.synthetic = false) AS real_checkins,
        COUNT(s.*) FILTER (WHERE s.synthetic = true) AS bootstrap_rows,
        MIN(s.recorded_at) FILTER (WHERE s.synthetic = false) AS first_real_checkin_at,
        MAX(s.recorded_at) FILTER (WHERE s.synthetic = false) AS last_real_checkin_at,
        MAX(s.recorded_at) AS last_any_state_at,
        latest.session_resolution_source,
        latest.transport,
        latest.context_source
    FROM core.identities i
    LEFT JOIN core.agents a ON a.id = i.agent_id
    LEFT JOIN core.agent_state s ON s.identity_id = i.identity_id
    LEFT JOIN LATERAL (
        SELECT
            s2.state_json#>>'{provenance_context,session_resolution_source}'
                AS session_resolution_source,
            s2.state_json#>>'{provenance_context,transport}' AS transport,
            s2.state_json#>>'{provenance_context,context_source}' AS context_source
        FROM core.agent_state s2
        WHERE s2.identity_id = i.identity_id
        ORDER BY s2.recorded_at DESC
        LIMIT 1
    ) latest ON TRUE
    GROUP BY
        i.identity_id,
        i.agent_id,
        i.status,
        i.created_at,
        i.updated_at,
        i.last_activity_at,
        i.parent_agent_id,
        i.spawn_reason,
        i.provisional_lineage,
        i.chain_obs_count,
        label,
        model_type,
        purpose,
        thread_id,
        active_session_key,
        metadata_total_updates,
        latest.session_resolution_source,
        latest.transport,
        latest.context_source
)
SELECT *
FROM per_identity
WHERE ($1::TIMESTAMPTZ IS NULL OR created_at >= $1::TIMESTAMPTZ)
ORDER BY created_at DESC
"""


SYNTHETIC_STATE_SQL = """
SELECT
    synthetic,
    COUNT(*) AS rows,
    COUNT(DISTINCT identity_id) AS identities,
    MIN(recorded_at) AS first_seen,
    MAX(recorded_at) AS last_seen
FROM core.agent_state
GROUP BY synthetic
ORDER BY synthetic
"""


async def collect_agent_fragmentation(
    *,
    db: Optional[Any] = None,
    since: Optional[datetime] = None,
    stale_hours: float = 24.0,
    low_checkin_max: int = 3,
    sample_limit: int = 30,
    observed_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """Collect and assess identity/check-in fragmentation from PostgreSQL."""
    backend = db or get_db()
    async with backend.acquire() as conn:
        rows = await conn.fetch(IDENTITY_FRAGMENTATION_SQL, since)
        synthetic_rows = await conn.fetch(SYNTHETIC_STATE_SQL)

    return build_agent_fragmentation_snapshot(
        rows,
        synthetic_rows,
        stale_hours=stale_hours,
        low_checkin_max=low_checkin_max,
        sample_limit=sample_limit,
        observed_at=observed_at,
        since=since,
    )


def build_agent_fragmentation_snapshot(
    identity_rows: Sequence[Mapping[str, Any]],
    synthetic_rows: Sequence[Mapping[str, Any]] = (),
    *,
    stale_hours: float = 24.0,
    low_checkin_max: int = 3,
    sample_limit: int = 30,
    observed_at: Optional[datetime] = None,
    since: Optional[datetime] = None,
) -> dict[str, Any]:
    """Build the report payload from already-fetched rows."""
    observed = _aware(observed_at or datetime.now(timezone.utc))
    stale_cutoff = observed - timedelta(hours=stale_hours)
    recent_24h_cutoff = observed - timedelta(hours=24)
    recent_7d_cutoff = observed - timedelta(days=7)
    low_checkin_max = max(int(low_checkin_max), 1)
    sample_limit = max(int(sample_limit), 0)

    rows = [_normalize_identity_row(row) for row in identity_rows]
    synthetic = _normalize_synthetic_rows(synthetic_rows)

    total = len(rows)
    active = [row for row in rows if row["status"] == "active"]
    zero_real = [row for row in rows if row["real_checkins"] == 0]
    one_real = [row for row in rows if row["real_checkins"] == 1]
    low_real = [
        row for row in rows if 1 <= row["real_checkins"] <= low_checkin_max
    ]
    more_than_low = [row for row in rows if row["real_checkins"] > low_checkin_max]
    active_zero = [row for row in active if row["real_checkins"] == 0]
    active_low = [
        row for row in active if 1 <= row["real_checkins"] <= low_checkin_max
    ]
    active_zero_stale = [
        row for row in active_zero if _older_than(row["created_at"], stale_cutoff)
    ]
    active_low_stale = [
        row for row in active_low if _older_than(row["created_at"], stale_cutoff)
    ]
    recent_24h = [row for row in rows if _at_or_after(row["created_at"], recent_24h_cutoff)]
    recent_7d = [row for row in rows if _at_or_after(row["created_at"], recent_7d_cutoff)]
    no_state = [
        row for row in rows
        if row["real_checkins"] == 0 and row["bootstrap_rows"] == 0
    ]
    bootstrap_only = [
        row for row in rows
        if row["real_checkins"] == 0 and row["bootstrap_rows"] > 0
    ]

    payload = {
        "decision": _decision(active_zero_stale, active_low_stale, synthetic),
        "reason": _reason(active_zero_stale, active_low_stale, synthetic),
        "observed_at": observed.isoformat(),
        "since": since.isoformat() if since else None,
        "thresholds": {
            "stale_hours": stale_hours,
            "low_checkin_max": low_checkin_max,
        },
        "totals": {
            "identities": total,
            "active": len(active),
            "zero_real_checkins": len(zero_real),
            "one_real_checkin": len(one_real),
            "one_to_low_real_checkins": len(low_real),
            "more_than_low_real_checkins": len(more_than_low),
            "active_zero_real_checkins": len(active_zero),
            "active_one_to_low_real_checkins": len(active_low),
            "active_zero_real_stale": len(active_zero_stale),
            "active_one_to_low_real_stale": len(active_low_stale),
            "no_state_at_all": len(no_state),
            "bootstrap_only": len(bootstrap_only),
        },
        "recent": {
            "created_24h": _count_block(recent_24h, low_checkin_max),
            "created_7d": _count_block(recent_7d, low_checkin_max),
        },
        "state_rows": synthetic,
        "status_breakdown": _status_breakdown(rows, low_checkin_max),
        "active_zero_by_model": _group_active_zero_by_model(active_zero),
        "recent_7d_by_session_source": _group_recent_by_session(recent_7d, low_checkin_max),
        "thread_clusters": _thread_clusters(active, low_checkin_max),
        "samples": {
            "active_zero_real": _sample(active_zero, sample_limit),
            "active_zero_real_stale": _sample(active_zero_stale, sample_limit),
        },
        "recommendations": _recommendations(
            active_zero_stale,
            active_low_stale,
            synthetic,
        ),
    }
    return payload


def _decision(
    active_zero_stale: Sequence[Mapping[str, Any]],
    active_low_stale: Sequence[Mapping[str, Any]],
    synthetic: Mapping[str, Any],
) -> str:
    if active_zero_stale:
        return "attention"
    if active_low_stale:
        return "watch"
    if synthetic["synthetic_rows"] <= 1:
        return "watch"
    return "ok"


def _reason(
    active_zero_stale: Sequence[Mapping[str, Any]],
    active_low_stale: Sequence[Mapping[str, Any]],
    synthetic: Mapping[str, Any],
) -> str:
    if active_zero_stale:
        return "active_identities_without_measured_trajectory"
    if active_low_stale:
        return "active_identities_with_sparse_measured_trajectory"
    if synthetic["synthetic_rows"] <= 1:
        return "bootstrap_rows_absent_or_nearly_absent"
    return "measured_trajectory_coverage_acceptable"


def _recommendations(
    active_zero_stale: Sequence[Mapping[str, Any]],
    active_low_stale: Sequence[Mapping[str, Any]],
    synthetic: Mapping[str, Any],
) -> list[str]:
    items: list[str] = []
    if active_zero_stale:
        items.append(
            "Fix managed startup paths that mint identities without initial_state "
            "or an immediate first process_agent_update."
        )
        items.append(
            "Group operator views by thread_id/role in addition to UUID so fresh "
            "process identity does not look like task fragmentation."
        )
    if active_low_stale:
        items.append(
            "Add adapter-side check-in prompts or automatic bounded-task check-ins "
            "for long sessions with only 1-3 measured updates."
        )
    if synthetic["synthetic_rows"] <= 1:
        items.append(
            "Verify hook/client adapters are actually sending onboard.initial_state; "
            "the bootstrap path is present but not materially populated."
        )
    if not items:
        items.append("No immediate fragmentation action required.")
    return items


def _count_block(rows: Sequence[Mapping[str, Any]], low_checkin_max: int) -> dict[str, Any]:
    return {
        "identities": len(rows),
        "zero_real_checkins": sum(row["real_checkins"] == 0 for row in rows),
        "one_real_checkin": sum(row["real_checkins"] == 1 for row in rows),
        "one_to_low_real_checkins": sum(
            1 <= row["real_checkins"] <= low_checkin_max for row in rows
        ),
        "more_than_low_real_checkins": sum(
            row["real_checkins"] > low_checkin_max for row in rows
        ),
    }


def _status_breakdown(
    rows: Sequence[Mapping[str, Any]],
    low_checkin_max: int,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, bool], list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["status"], bool(row["label"])), []).append(row)

    out: list[dict[str, Any]] = []
    for (status, has_label), items in sorted(grouped.items()):
        block = _count_block(items, low_checkin_max)
        out.append({"status": status, "has_label": has_label, **block})
    return out


def _group_active_zero_by_model(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, bool], int] = {}
    for row in rows:
        key = (_display(row["model_type"]), bool(row["label"]))
        grouped[key] = grouped.get(key, 0) + 1
    return [
        {"model_type": model_type, "has_label": has_label, "identities": count}
        for (model_type, has_label), count in sorted(
            grouped.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1]),
        )
    ]


def _group_recent_by_session(
    rows: Sequence[Mapping[str, Any]],
    low_checkin_max: int,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        key = (_display(row["session_resolution_source"]), _display(row["transport"]))
        grouped.setdefault(key, []).append(row)

    out: list[dict[str, Any]] = []
    for (session_source, transport), items in grouped.items():
        block = _count_block(items, low_checkin_max)
        out.append({
            "session_resolution_source": session_source,
            "transport": transport,
            **block,
        })
    return sorted(out, key=lambda item: (-item["identities"], item["session_resolution_source"]))


def _thread_clusters(
    rows: Sequence[Mapping[str, Any]],
    low_checkin_max: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if not row["thread_id"] or row["real_checkins"] > low_checkin_max:
            continue
        grouped.setdefault(row["thread_id"], []).append(row)

    out: list[dict[str, Any]] = []
    for thread_id, items in grouped.items():
        if len(items) <= 1:
            continue
        created = [item["created_at"] for item in items if item["created_at"]]
        out.append({
            "thread_id": thread_id,
            "active_low_identities": len(items),
            "zero_real_checkins": sum(item["real_checkins"] == 0 for item in items),
            "one_to_low_real_checkins": sum(
                1 <= item["real_checkins"] <= low_checkin_max for item in items
            ),
            "first_created_at": min(created).isoformat() if created else None,
            "last_created_at": max(created).isoformat() if created else None,
            "sample_labels": [
                item["label"] or item["agent_id"] for item in sorted(
                    items,
                    key=lambda item: item["created_at"] or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True,
                )[:5]
            ],
        })
    return sorted(
        out,
        key=lambda item: (
            -item["active_low_identities"],
            -item["zero_real_checkins"],
            item["thread_id"],
        ),
    )


def _sample(
    rows: Sequence[Mapping[str, Any]],
    sample_limit: int,
) -> list[dict[str, Any]]:
    if sample_limit <= 0:
        return []
    sorted_rows = sorted(
        rows,
        key=lambda row: row["created_at"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return [
        {
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "agent_id": row["agent_id"],
            "label": row["label"] or None,
            "model_type": row["model_type"] or None,
            "purpose": row["purpose"] or None,
            "spawn_reason": row["spawn_reason"] or None,
            "thread_id": row["thread_id"] or None,
            "parent_agent_id": row["parent_agent_id"] or None,
        }
        for row in sorted_rows[:sample_limit]
    ]


def _normalize_identity_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "identity_id": row.get("identity_id"),
        "agent_id": str(row.get("agent_id") or ""),
        "status": str(row.get("status") or "unknown"),
        "created_at": _aware_or_none(row.get("created_at")),
        "updated_at": _aware_or_none(row.get("updated_at")),
        "last_activity_at": _aware_or_none(row.get("last_activity_at")),
        "parent_agent_id": _text(row.get("parent_agent_id")),
        "spawn_reason": _text(row.get("spawn_reason")),
        "provisional_lineage": bool(row.get("provisional_lineage") or False),
        "chain_obs_count": _int(row.get("chain_obs_count")),
        "label": _text(row.get("label")),
        "model_type": _text(row.get("model_type")),
        "purpose": _text(row.get("purpose")),
        "thread_id": _text(row.get("thread_id")),
        "active_session_key": _text(row.get("active_session_key")),
        "metadata_total_updates": _int(row.get("metadata_total_updates")),
        "real_checkins": _int(row.get("real_checkins")),
        "bootstrap_rows": _int(row.get("bootstrap_rows")),
        "first_real_checkin_at": _aware_or_none(row.get("first_real_checkin_at")),
        "last_real_checkin_at": _aware_or_none(row.get("last_real_checkin_at")),
        "last_any_state_at": _aware_or_none(row.get("last_any_state_at")),
        "session_resolution_source": _text(row.get("session_resolution_source")),
        "transport": _text(row.get("transport")),
        "context_source": _text(row.get("context_source")),
    }


def _normalize_synthetic_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_flag = {bool(row.get("synthetic")): row for row in rows}
    measured = by_flag.get(False, {})
    synthetic = by_flag.get(True, {})
    return {
        "measured_rows": _int(measured.get("rows")),
        "measured_identities": _int(measured.get("identities")),
        "synthetic_rows": _int(synthetic.get("rows")),
        "synthetic_identities": _int(synthetic.get("identities")),
        "synthetic_first_seen": (
            _aware_or_none(synthetic.get("first_seen")).isoformat()
            if _aware_or_none(synthetic.get("first_seen")) else None
        ),
        "synthetic_last_seen": (
            _aware_or_none(synthetic.get("last_seen")).isoformat()
            if _aware_or_none(synthetic.get("last_seen")) else None
        ),
    }


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _aware_or_none(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            return _aware(datetime.fromisoformat(text))
        except ValueError:
            return None
    return None


def _at_or_after(value: Optional[datetime], cutoff: datetime) -> bool:
    return bool(value and value >= cutoff)


def _older_than(value: Optional[datetime], cutoff: datetime) -> bool:
    return bool(value and value < cutoff)


def _int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _display(value: str) -> str:
    return value or "<none>"
