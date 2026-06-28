#!/usr/bin/env python3
"""Redis-retirement Phase 1B: session-mirror parity checker.

Measures how faithfully the PostgreSQL mirror (core.session_bindings) reflects
the authoritative Redis session: keys, so the operator can decide whether to
flip UNITARES_SESSION_MIRROR_APPLY (the read flip). READ-ONLY.

Why birth-cohort, not a point-in-time snapshot
----------------------------------------------
The implementation council flagged that a naive "iterate all Redis keys, are
they in PG?" ratio conflates writer-correctness with expiry-race noise: with the
keyspace churning and Redis/PG expiry clocks independent, a key can be live in
Redis but already reaped in PG, or written to Redis microseconds before the PG
dual-write commits — both count as spurious "divergence" that never converges.

So we sample a BIRTH COHORT: Redis session: bindings whose bound_at is old
enough to have committed to PG (>= --min-age-min) but young enough not to have
expired out of either store (<= --max-age-min). Within that window a faithful
writer should show ~100% match; misses are real dropped writes, not races.

Direction: Redis -> PG (does the mirror reflect what Redis holds?) — this is the
one that catches dropped/under-mirrored writes, the failure that would cause a
cold-mint after the read flip.

Output: a single JSON summary to stdout (cron/dashboard friendly). We do NOT
emit per-row audit events — at ~900 keys per scan that would flood the audit
partition (council note); the summary is the signal.

Exit codes: 0 = ran successfully (parity is informational — the operator picks
the flip threshold); 1 = runner error (store unreachable, etc.).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

SESSION_PREFIX = "session:"
DEFAULT_DSN = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)
DEFAULT_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


def _parse_bound_at(payload: Dict[str, Any]) -> Optional[datetime]:
    """Parse the ISO bound_at from a Redis session payload, tz-aware, or None."""
    raw = payload.get("bound_at")
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def compare_birth_cohort(
    redis_items: Dict[str, Dict[str, Any]],
    pg_uuid_by_key: Dict[str, str],
    *,
    now: datetime,
    min_age: timedelta,
    max_age: timedelta,
) -> Dict[str, Any]:
    """Pure comparison core (no I/O) — unit-testable.

    redis_items:    session_key -> parsed Redis payload (must carry agent_id, bound_at)
    pg_uuid_by_key: session_key -> agent_uuid present in core.session_bindings
    Returns a summary dict with the cohort size, match/miss/mismatch counts, the
    parity ratio, and a small sample of divergences.
    """
    lo, hi = now - max_age, now - min_age  # cohort: bound_at in [lo, hi]
    cohort = 0
    matched = 0
    missing_in_pg: List[str] = []
    uuid_mismatch: List[Dict[str, str]] = []

    for session_key, payload in redis_items.items():
        bound_at = _parse_bound_at(payload)
        if bound_at is None or not (lo <= bound_at <= hi):
            continue  # outside the birth cohort (too young/old or unparseable)
        cohort += 1
        redis_uuid = payload.get("agent_id")  # Redis JSON key is agent_id (= the UUID)
        pg_uuid = pg_uuid_by_key.get(session_key)
        if pg_uuid is None:
            missing_in_pg.append(session_key)
        elif pg_uuid != redis_uuid:
            uuid_mismatch.append(
                {"session_key": session_key, "redis": redis_uuid, "pg": pg_uuid}
            )
        else:
            matched += 1

    ratio = (matched / cohort) if cohort else None
    return {
        "cohort_size": cohort,
        "matched": matched,
        "missing_in_pg": len(missing_in_pg),
        "uuid_mismatch": len(uuid_mismatch),
        "parity_ratio": ratio,
        "window_min": [int(min_age.total_seconds() // 60), int(max_age.total_seconds() // 60)],
        "sample_missing": missing_in_pg[:10],
        "sample_mismatch": uuid_mismatch[:10],
    }


async def _gather_redis_items(redis_url: str) -> Dict[str, Dict[str, Any]]:
    import redis.asyncio as aioredis  # local import so the module loads without redis
    client = aioredis.from_url(redis_url, decode_responses=True)
    items: Dict[str, Dict[str, Any]] = {}
    try:
        async for key in client.scan_iter(match=f"{SESSION_PREFIX}*", count=500):
            raw = await client.get(key)
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            items[key[len(SESSION_PREFIX):]] = payload
    finally:
        await client.aclose()
    return items


async def _gather_pg_uuids(dsn: str, session_keys: List[str]) -> Dict[str, str]:
    import asyncpg  # local import
    if not session_keys:
        return {}
    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        rows = await conn.fetch(
            """
            SELECT session_key, agent_uuid
            FROM core.session_bindings
            WHERE session_key = ANY($1::text[])
              AND (expires_at IS NULL OR expires_at > now())
            """,
            session_keys,
        )
        return {r["session_key"]: r["agent_uuid"] for r in rows}
    finally:
        await conn.close()


async def _pg_binding_count(dsn: str) -> int:
    import asyncpg
    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        return await conn.fetchval("SELECT count(*) FROM core.session_bindings") or 0
    finally:
        await conn.close()


async def run(dsn: str, redis_url: str, min_age_min: int, max_age_min: int) -> int:
    # Inert gate: an empty mirror means the shadow writer hasn't run
    # (UNITARES_SESSION_MIRROR_SHADOW off) — every key would report
    # missing_in_pg, which is non-signal. Report and exit 0.
    mirror_rows = await _pg_binding_count(dsn)
    if mirror_rows == 0:
        print(json.dumps({
            "status": "inert",
            "reason": "core.session_bindings is empty — enable UNITARES_SESSION_MIRROR_SHADOW first",
            "mirror_rows": 0,
        }))
        return 0

    redis_items = await _gather_redis_items(redis_url)
    pg_uuids = await _gather_pg_uuids(dsn, list(redis_items.keys()))
    now = datetime.now(timezone.utc)
    summary = compare_birth_cohort(
        redis_items, pg_uuids, now=now,
        min_age=timedelta(minutes=min_age_min),
        max_age=timedelta(minutes=max_age_min),
    )
    summary["status"] = "ran"
    summary["mirror_rows"] = mirror_rows
    summary["redis_session_keys"] = len(redis_items)
    print(json.dumps(summary, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Session-mirror birth-cohort parity checker")
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    ap.add_argument("--min-age-min", type=int, default=5,
                    help="cohort lower bound: bindings at least this old (committed)")
    ap.add_argument("--max-age-min", type=int, default=60,
                    help="cohort upper bound: bindings at most this old (not yet expired)")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args.dsn, args.redis_url, args.min_age_min, args.max_age_min))
    except Exception as e:  # runner error
        print(json.dumps({"status": "error", "error": str(e)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
