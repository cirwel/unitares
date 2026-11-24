"""R2 Phase 1 lineage telemetry gate.

R2 Phase 1 shipped as a shadow/telemetry stage. Phase 2 consumers should not
open until there is enough observed lineage traffic to evaluate the promotion
and demotion behavior. This module keeps that gate read-only and explicit.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from src.db import get_db
from src.db.mixins.audit import R2_PHASE1_AUDIT_TELEMETRY_SQL as AUDIT_TELEMETRY_SQL


DEFAULT_PHASE1_START = datetime(2026, 5, 5, tzinfo=timezone.utc)
DEFAULT_MIN_TELEMETRY_DAYS = 28
DEFAULT_MIN_CONFIRMED_PAIRS = 50
DEFAULT_MIN_DEMOTED_PAIRS = 10
DEFAULT_MIN_CROSS_ROLE_REJECTIONS = 1

LINEAGE_AUDIT_EVENT_TYPES = (
    "lineage_declared",
    "lineage_promoted",
    "lineage_demoted",
    "lineage_cross_role_rejected",
    "lineage_grace_expired",
)

IDENTITY_TELEMETRY_SQL = """
SELECT
    COUNT(*) FILTER (WHERE parent_agent_id IS NOT NULL)::BIGINT
        AS lineage_total,
    COUNT(*) FILTER (
        WHERE parent_agent_id IS NOT NULL
          AND lineage_archived_at IS NULL
          AND lineage_demoted_at IS NULL
          AND provisional_lineage = TRUE
    )::BIGINT AS active_provisional,
    COUNT(*) FILTER (
        WHERE parent_agent_id IS NOT NULL
          AND lineage_archived_at IS NULL
          AND lineage_demoted_at IS NULL
          AND provisional_lineage = FALSE
          AND confirmed_at IS NOT NULL
    )::BIGINT AS active_confirmed,
    COUNT(*) FILTER (WHERE lineage_demoted_at IS NOT NULL)::BIGINT
        AS demoted_total,
    COUNT(*) FILTER (WHERE lineage_archived_at IS NOT NULL)::BIGINT
        AS archived_total,
    COUNT(*) FILTER (
        WHERE parent_agent_id IS NOT NULL
          AND lineage_declared_at >= $1
    )::BIGINT AS declared_since,
    COUNT(*) FILTER (WHERE confirmed_at >= $1)::BIGINT
        AS confirmed_since,
    COUNT(*) FILTER (WHERE lineage_demoted_at >= $1)::BIGINT
        AS demoted_since,
    COUNT(*) FILTER (WHERE lineage_archived_at >= $1)::BIGINT
        AS archived_since,
    MIN(lineage_declared_at) FILTER (
        WHERE parent_agent_id IS NOT NULL
          AND lineage_declared_at >= $1
    ) AS first_declared_since,
    MAX(lineage_last_eval_at) FILTER (
        WHERE lineage_last_eval_at >= $1
    ) AS last_eval_since
FROM core.identities
"""


@dataclass(frozen=True)
class R2Phase1Thresholds:
    min_telemetry_days: int = DEFAULT_MIN_TELEMETRY_DAYS
    min_confirmed_pairs: int = DEFAULT_MIN_CONFIRMED_PAIRS
    min_demoted_pairs: int = DEFAULT_MIN_DEMOTED_PAIRS
    min_cross_role_rejections: int = DEFAULT_MIN_CROSS_ROLE_REJECTIONS

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


async def collect_r2_phase1_telemetry(
    *,
    db: Optional[Any] = None,
    since: datetime = DEFAULT_PHASE1_START,
    now: Optional[datetime] = None,
    thresholds: R2Phase1Thresholds = R2Phase1Thresholds(),
) -> dict[str, Any]:
    """Collect and assess read-only R2 Phase 1 telemetry."""
    since = _ensure_aware_utc(since)
    observed_at = _ensure_aware_utc(now or datetime.now(timezone.utc))

    backend = db or get_db()
    async with backend.acquire() as conn:
        identity_row = await conn.fetchrow(IDENTITY_TELEMETRY_SQL, since)
        event_rows = await conn.fetch(
            AUDIT_TELEMETRY_SQL,
            since,
            list(LINEAGE_AUDIT_EVENT_TYPES),
        )

    snapshot = build_r2_phase1_snapshot(
        identity_row,
        event_rows,
        since=since,
        observed_at=observed_at,
    )
    return assess_r2_phase1_telemetry(snapshot, thresholds=thresholds)


def build_r2_phase1_snapshot(
    identity_row: Any,
    event_rows: list[Any] | tuple[Any, ...],
    *,
    since: datetime,
    observed_at: datetime,
) -> dict[str, Any]:
    """Build a normalized telemetry snapshot from DB rows."""
    identity_counts = {
        "lineage_total": _row_int(identity_row, "lineage_total"),
        "active_provisional": _row_int(identity_row, "active_provisional"),
        "active_confirmed": _row_int(identity_row, "active_confirmed"),
        "demoted_total": _row_int(identity_row, "demoted_total"),
        "archived_total": _row_int(identity_row, "archived_total"),
        "declared_since": _row_int(identity_row, "declared_since"),
        "confirmed_since": _row_int(identity_row, "confirmed_since"),
        "demoted_since": _row_int(identity_row, "demoted_since"),
        "archived_since": _row_int(identity_row, "archived_since"),
    }
    event_counts = {event_type: 0 for event_type in LINEAGE_AUDIT_EVENT_TYPES}
    event_first_seen = {event_type: None for event_type in LINEAGE_AUDIT_EVENT_TYPES}
    event_last_seen = {event_type: None for event_type in LINEAGE_AUDIT_EVENT_TYPES}

    for row in event_rows:
        event_type = _row_text(row, "event_type")
        if event_type not in event_counts:
            continue
        event_counts[event_type] = _row_int(row, "event_count")
        event_first_seen[event_type] = _format_time(_row_value(row, "first_seen_at"))
        event_last_seen[event_type] = _format_time(_row_value(row, "last_seen_at"))

    return {
        "since": _format_time(_ensure_aware_utc(since)),
        "observed_at": _format_time(_ensure_aware_utc(observed_at)),
        "telemetry_age_days": _age_days(since, observed_at),
        "identity_counts": identity_counts,
        "audit_event_counts": event_counts,
        "audit_event_first_seen": event_first_seen,
        "audit_event_last_seen": event_last_seen,
        "first_declared_since": _format_time(
            _row_value(identity_row, "first_declared_since")
        ),
        "last_eval_since": _format_time(_row_value(identity_row, "last_eval_since")),
    }


def assess_r2_phase1_telemetry(
    snapshot: dict[str, Any],
    *,
    thresholds: R2Phase1Thresholds = R2Phase1Thresholds(),
) -> dict[str, Any]:
    """Assess whether R2 Phase 2 consumer work has enough Phase 1 telemetry."""
    identity_counts = snapshot.get("identity_counts") or {}
    event_counts = snapshot.get("audit_event_counts") or {}
    demoted_pairs = max(
        int(identity_counts.get("demoted_since") or 0),
        int(event_counts.get("lineage_demoted") or 0),
    )
    checks = {
        "telemetry_window_days": _check(
            snapshot.get("telemetry_age_days") or 0,
            thresholds.min_telemetry_days,
        ),
        "confirmed_pairs": _check(
            identity_counts.get("confirmed_since") or 0,
            thresholds.min_confirmed_pairs,
        ),
        "demoted_pairs": _check(demoted_pairs, thresholds.min_demoted_pairs),
        "cross_role_rejections": _check(
            event_counts.get("lineage_cross_role_rejected") or 0,
            thresholds.min_cross_role_rejections,
        ),
    }
    passed = all(check["passed"] for check in checks.values())
    failed_checks = _failed_checks(checks)
    decision = "candidate" if passed else "defer"
    reason = (
        "phase2_telemetry_thresholds_satisfied"
        if passed
        else "phase2_telemetry_thresholds_unmet"
    )
    recommendations = (
        [
            "Open the R2 Phase 2 design gate before wiring trust-tier, KG, "
            "baseline, or dashboard consumers."
        ]
        if passed
        else [
            "Keep R2 Phase 2 consumers deferred until all telemetry thresholds pass.",
            _deficit_summary(failed_checks),
        ]
    )

    return {
        "decision": decision,
        "reason": reason,
        "thresholds": thresholds.to_dict(),
        "checks": checks,
        "failed_checks": failed_checks,
        "snapshot": snapshot,
        "recommendations": recommendations,
    }


def parse_since(value: Optional[str]) -> datetime:
    if not value:
        return DEFAULT_PHASE1_START
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    return _ensure_aware_utc(parsed)


def _check(observed: Any, required: int) -> dict[str, Any]:
    observed_value = float(observed)
    return {
        "observed": observed_value,
        "required": required,
        "passed": observed_value >= required,
    }


def _failed_checks(checks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    failed = []
    for name, check in checks.items():
        if check.get("passed"):
            continue
        observed = float(check.get("observed") or 0)
        required = float(check.get("required") or 0)
        failed.append({
            "name": name,
            "observed": observed,
            "required": required,
            "remaining": max(0.0, round(required - observed, 2)),
        })
    return failed


def _deficit_summary(failed_checks: list[dict[str, Any]]) -> str:
    if not failed_checks:
        return "All telemetry thresholds are satisfied."
    parts = [
        f"{item['name']} needs {item['remaining']} more"
        for item in failed_checks
    ]
    return "Remaining R2 Phase 2 deficits: " + "; ".join(parts) + "."


def _row_int(row: Any, key: str) -> int:
    value = _row_value(row, key)
    return int(value or 0)


def _row_text(row: Any, key: str) -> Optional[str]:
    value = _row_value(row, key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _row_value(row: Any, key: str) -> Any:
    if row is None:
        return None
    if hasattr(row, "get"):
        return row.get(key)
    return row[key]


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_time(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_aware_utc(value).isoformat()
    return str(value)


def _age_days(since: datetime, observed_at: datetime) -> float:
    seconds = max(0.0, (_ensure_aware_utc(observed_at) - _ensure_aware_utc(since)).total_seconds())
    return round(seconds / 86400.0, 2)
