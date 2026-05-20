#!/usr/bin/env python3
"""Lease-plane Phase A latency audit.

Mines the existing audit trail for substrate-tax indicators at the lease
boundary. Same pattern as `surface-lease-plane-v0.md` §7.5 v0.9 (Steward
audit-mining): use what's already instrumented rather than waiting for
purpose-built telemetry.

Inputs:
  - `lease_plane.lease_plane_events` (BEAM-side event log; 13+ event_types)
  - `audit.tool_usage` (forwarded `lease.*` rows; `latency_ms` currently NULL)

Outputs (text or JSON via --json):
  1. Hold-time distribution by surface_kind — `acquire` → `release` deltas
     from `lease_plane_events`. Not RPC latency, but the closest proxy
     available without client-side timing.
  2. Conflict / TTL-reap / down-local rates as fraction of acquires.
  3. Forward-attempt distribution — non-zero forward_attempts indicate
     coordination_failure-class retries between BEAM and Postgres.
  4. `audit.tool_usage.latency_ms` NULL-rate audit — explicit gap statement.

Usage:
    python3 scripts/dev/lease_plane_latency_audit.py
    python3 scripts/dev/lease_plane_latency_audit.py --days 14 --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import psycopg2  # type: ignore


def connect():
    dsn = os.environ.get(
        "GOVERNANCE_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/governance",
    )
    return psycopg2.connect(dsn)


HOLD_TIME_SQL = """
WITH pairs AS (
  SELECT
    a.lease_id,
    a.surface_kind,
    a.surface_id,
    a.ts AS acquired_ts,
    r.ts AS released_ts,
    EXTRACT(EPOCH FROM (r.ts - a.ts)) * 1000 AS hold_ms
  FROM lease_plane.lease_plane_events a
  JOIN lease_plane.lease_plane_events r
    ON r.lease_id = a.lease_id AND r.event_type = 'release'
  WHERE a.event_type = 'acquire'
    AND a.ts > now() - make_interval(days := %s)
)
SELECT
  surface_kind,
  count(*) AS n,
  percentile_cont(0.50) WITHIN GROUP (ORDER BY hold_ms) AS p50,
  percentile_cont(0.95) WITHIN GROUP (ORDER BY hold_ms) AS p95,
  percentile_cont(0.99) WITHIN GROUP (ORDER BY hold_ms) AS p99,
  max(hold_ms) AS p100
FROM pairs
GROUP BY surface_kind
ORDER BY n DESC
"""

EVENT_TYPE_DISTRIBUTION_SQL = """
SELECT event_type, count(*)
FROM lease_plane.lease_plane_events
WHERE ts > now() - make_interval(days := %s)
GROUP BY event_type
ORDER BY count(*) DESC
"""

FORWARD_ATTEMPTS_SQL = """
SELECT
  forward_attempts,
  count(*)
FROM lease_plane.lease_plane_events
WHERE ts > now() - make_interval(days := %s)
GROUP BY forward_attempts
ORDER BY forward_attempts
"""

TOOL_USAGE_LATENCY_NULL_SQL = """
SELECT
  tool_name,
  count(*) AS total,
  count(*) FILTER (WHERE latency_ms IS NULL) AS null_latency
FROM audit.tool_usage
WHERE tool_name LIKE 'lease.%%'
  AND ts > now() - make_interval(days := %s)
GROUP BY tool_name
ORDER BY total DESC
"""


def fetchall_dict(cur, sql: str, days: int) -> list[dict[str, Any]]:
    cur.execute(sql, (days,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def run(days: int) -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            hold = fetchall_dict(cur, HOLD_TIME_SQL, days)
            etype = fetchall_dict(cur, EVENT_TYPE_DISTRIBUTION_SQL, days)
            fwd = fetchall_dict(cur, FORWARD_ATTEMPTS_SQL, days)
            null_lat = fetchall_dict(cur, TOOL_USAGE_LATENCY_NULL_SQL, days)
    total_acquires = sum(r["count"] for r in etype if r["event_type"] == "acquire")
    conflict_rate = (
        sum(r["count"] for r in etype if r["event_type"] == "conflict_held_by_other")
        / total_acquires if total_acquires else 0.0
    )
    reaped_rate = (
        sum(r["count"] for r in etype if r["event_type"] in ("reaped_local_ttl", "reaped_remote_ttl"))
        / total_acquires if total_acquires else 0.0
    )
    return {
        "window_days": days,
        "hold_time_by_surface_kind": [
            {
                "surface_kind": r["surface_kind"],
                "n": int(r["n"]),
                "p50_ms": float(r["p50"]) if r["p50"] is not None else None,
                "p95_ms": float(r["p95"]) if r["p95"] is not None else None,
                "p99_ms": float(r["p99"]) if r["p99"] is not None else None,
                "p100_ms": float(r["p100"]) if r["p100"] is not None else None,
            }
            for r in hold
        ],
        "event_type_distribution": [
            {"event_type": r["event_type"], "count": int(r["count"])}
            for r in etype
        ],
        "conflict_rate": round(conflict_rate, 6),
        "reaped_rate": round(reaped_rate, 6),
        "forward_attempts_distribution": [
            {"forward_attempts": int(r["forward_attempts"]), "count": int(r["count"])}
            for r in fwd
        ],
        "tool_usage_latency_null_audit": [
            {
                "tool_name": r["tool_name"],
                "total": int(r["total"]),
                "null_latency": int(r["null_latency"]),
                "null_pct": round(100.0 * r["null_latency"] / r["total"], 2) if r["total"] else 0.0,
            }
            for r in null_lat
        ],
    }


def render_text(result: dict[str, Any]) -> str:
    lines = [
        f"Lease-plane Phase A latency audit (window: {result['window_days']}d)",
        "",
        "Hold-time distribution (acquire -> release, ms):",
    ]
    for r in result["hold_time_by_surface_kind"]:
        lines.append(
            f"  [{r['surface_kind']:<10}] n={r['n']:>6}  "
            f"p50={r['p50_ms']:>8.1f}  p95={r['p95_ms']:>10.1f}  "
            f"p99={r['p99_ms']:>10.1f}  p100={r['p100_ms']:>10.1f}"
        )

    lines.extend(["", "Event-type distribution:"])
    for r in result["event_type_distribution"]:
        lines.append(f"  {r['event_type']:<35} {r['count']:>8}")

    lines.extend(
        [
            "",
            f"Conflict rate (held_by_other / acquires):  {result['conflict_rate']*100:.4f}%",
            f"Reaped rate    (TTL reap   / acquires):  {result['reaped_rate']*100:.4f}%",
        ]
    )

    lines.extend(["", "Forward-attempt distribution (BEAM->Postgres retry signal):"])
    for r in result["forward_attempts_distribution"]:
        lines.append(f"  attempts={r['forward_attempts']:>2}  count={r['count']:>8}")

    lines.extend(["", "audit.tool_usage.latency_ms NULL audit:"])
    for r in result["tool_usage_latency_null_audit"]:
        lines.append(
            f"  {r['tool_name']:<28} total={r['total']:>6}  "
            f"null={r['null_latency']:>6}  null_pct={r['null_pct']:.1f}%"
        )

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14, help="window in days (default 14)")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()
    result = run(args.days)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(render_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
