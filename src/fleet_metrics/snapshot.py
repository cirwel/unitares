"""Server-owned fleet-metrics snapshot queries (`GET /v1/metrics/snapshot`).

Computes the product-neutral trailing-7d and cumulative scalar aggregates from
Core's PostgreSQL pool so external metric scrapers can collect a consistent
snapshot over authenticated HTTP without opening direct PostgreSQL connections.
"""

from __future__ import annotations

import copy
import time
from typing import Any

from src.db.mixins.audit import (
    FINDING_COUNTS_7D_SQL as FINDING_COUNTS_7D_SQL,
    fetch_finding_counts_7d,
)
from src.db.mixins.knowledge_graph import KG_DISCOVERIES_COUNT_SQL
from src.db.mixins.state import AGENT_STATE_SNAPSHOT_SCALAR_SQL
from src.fleet_metrics.catalog import require
from src.logging_utils import get_logger

logger = get_logger(__name__)

SNAPSHOT_WINDOW = "7d"
SNAPSHOT_CACHE_TTL_S = 5.0
STATEMENT_TIMEOUT_SQL = "SET LOCAL statement_timeout = '10s'"

CHECKINS_7D_SQL = (
    "SELECT count(*) FROM audit.tool_usage "
    "WHERE ts > now() - interval '7 days' "
    "AND tool_name = 'process_agent_update'"
)

SNAPSHOT_SCALAR_QUERIES: dict[str, str] = {
    "agents.active.7d": AGENT_STATE_SNAPSHOT_SCALAR_SQL["agents.active.7d"],
    "kg.entries.count": KG_DISCOVERIES_COUNT_SQL,
    "checkins.7d": CHECKINS_7D_SQL,
    "governance.coherence.mean.7d": AGENT_STATE_SNAPSHOT_SCALAR_SQL[
        "governance.coherence.mean.7d"
    ],
    "governance.risk.mean.7d": AGENT_STATE_SNAPSHOT_SCALAR_SQL[
        "governance.risk.mean.7d"
    ],
    "governance.guide.7d": AGENT_STATE_SNAPSHOT_SCALAR_SQL["governance.guide.7d"],
    "governance.pause.7d": AGENT_STATE_SNAPSHOT_SCALAR_SQL["governance.pause.7d"],
}

# Fail at import if a snapshot scalar drifts out of the core catalog.
for _metric_name in SNAPSHOT_SCALAR_QUERIES:
    require(_metric_name)

_cache_entry: tuple[float, dict[str, Any]] | None = None


def clear_snapshot_cache() -> None:
    """Clear the short-lived server snapshot cache (used in tests)."""
    global _cache_entry
    _cache_entry = None


async def collect_snapshot_from_conn(conn: Any) -> dict[str, Any]:
    """Run all snapshot queries over an acquired asyncpg connection.

    Uses a repeatable-read read-only transaction so all scalars observe one DB
    snapshot, with per-query savepoints so a failure in one table/query returns
    `null` + an `errors` entry for that key instead of blanking the whole run.
    """
    metrics: dict[str, float | None] = {}
    finding_counts_7d: dict[str, int] = {}
    errors: dict[str, str] = {}

    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute(STATEMENT_TIMEOUT_SQL)
        db_now = await conn.fetchval("SELECT now()")
        generated_at = db_now.isoformat() if hasattr(db_now, "isoformat") else str(db_now)

        for name, sql in SNAPSHOT_SCALAR_QUERIES.items():
            try:
                async with conn.transaction():
                    raw = await conn.fetchval(sql)
                metrics[name] = float(raw or 0.0)
            except Exception as exc:
                logger.warning("metrics snapshot query failed for %s: %s", name, exc)
                metrics[name] = None
                errors[name] = f"{type(exc).__name__}: {exc}"

        try:
            async with conn.transaction():
                finding_counts_7d = await fetch_finding_counts_7d(conn)
        except Exception as exc:
            logger.warning("metrics snapshot finding_counts_7d failed: %s", exc)
            errors["finding_counts_7d"] = f"{type(exc).__name__}: {exc}"

    return {
        "generated_at": generated_at,
        "window": SNAPSHOT_WINDOW,
        "metrics": metrics,
        "finding_counts_7d": finding_counts_7d,
        "errors": errors,
    }


async def collect_metrics_snapshot(
    *,
    ttl_seconds: float = SNAPSHOT_CACHE_TTL_S,
) -> dict[str, Any]:
    """Acquire a DB connection from Core's pool and return the snapshot dict."""
    global _cache_entry
    now_mono = time.monotonic()
    if ttl_seconds > 0 and _cache_entry is not None:
        expires_at, cached = _cache_entry
        if now_mono < expires_at:
            return copy.deepcopy(cached)

    from src import agent_storage

    db = agent_storage.get_db()
    async with db.acquire() as conn:
        snapshot = await collect_snapshot_from_conn(conn)

    if ttl_seconds > 0 and not snapshot["errors"]:
        _cache_entry = (time.monotonic() + ttl_seconds, copy.deepcopy(snapshot))
    return snapshot
