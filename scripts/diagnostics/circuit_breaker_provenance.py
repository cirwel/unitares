#!/usr/bin/env python3
"""Report when the runtime circuit breaker actually delivered, and how often.

Read-only. Written to answer one operator question that the repo record cannot
answer on its own: *when did enforcement start delivering, and was that a
deliberate enablement or a posture that had been live all along?*

The documentation history is genuinely confusing on this point. The 2026-08-06
audit snapshot concluded there was no recent governed delivery; that conclusion
was falsified on 2026-08-09 by a `lifecycle_paused` event at behavioral
confidence 0.1 (`docs/ontology/eisv-proprioception-contract.md`, "Deployed
posture"). So the deployed posture changed in the *documentation* without any
switch being flipped, and the only way to recover the real timeline is from the
audit rows.

Reads from:
  - audit.events (event_type='lifecycle_paused')   -- delivered enforcement
  - audit.events (event_type='circuit_breaker_trip') -- fires 1:1 alongside
  - audit.events (event_type='auto_attest')        -- produced verdicts

The produced-vs-delivered distinction is the one this script exists to keep
straight, per the contract: a produced pause verdict is not an enforced action,
and a pause *count* must never be read as an enforcement count.

Payload keys on `auto_attest` are reported as a census rather than assumed, so a
key rename shows up as a visible zero with its own explanation instead of a
silently wrong number.

Usage:
    python3 scripts/diagnostics/circuit_breaker_provenance.py
    python3 scripts/diagnostics/circuit_breaker_provenance.py --since 2026-06-01T00:00:00Z
    python3 scripts/diagnostics/circuit_breaker_provenance.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Any, Optional


sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

from src.db import close_db, get_db


DELIVERED_EVENT = "lifecycle_paused"
TRIP_EVENT = "circuit_breaker_trip"
PRODUCED_EVENT = "auto_attest"


async def _event_timeline(pool, event_type: str, since: Optional[datetime]) -> dict:
    """First/last occurrence, total, distinct agents, and a monthly histogram."""
    where = "event_type = $1"
    params: list[Any] = [event_type]
    if since is not None:
        where += " AND ts >= $2"
        params.append(since)

    async with pool.acquire() as conn:
        summary = await conn.fetchrow(
            f"""
            SELECT
                min(ts) AS first_ts,
                max(ts) AS last_ts,
                count(*)::int AS total,
                count(DISTINCT agent_id)::int AS distinct_agents
            FROM audit.events
            WHERE {where}
            """,
            *params,
        )
        monthly = await conn.fetch(
            f"""
            SELECT to_char(date_trunc('month', ts), 'YYYY-MM') AS month,
                   count(*)::int AS n
            FROM audit.events
            WHERE {where}
            GROUP BY 1
            ORDER BY 1
            """,
            *params,
        )

    return {
        "event_type": event_type,
        "first_ts": summary["first_ts"].isoformat() if summary["first_ts"] else None,
        "last_ts": summary["last_ts"].isoformat() if summary["last_ts"] else None,
        "total": summary["total"] or 0,
        "distinct_agents": summary["distinct_agents"] or 0,
        "monthly": [{"month": r["month"], "n": r["n"]} for r in monthly],
    }


async def _produced_verdicts(pool, since: Optional[datetime]) -> dict:
    """Census of auto_attest decisions plus which payload keys are actually present.

    Reporting key presence matters more than it looks: if `decision` or
    `gap_suppressed` is renamed upstream, a hardcoded filter returns zero and
    reads as "no produced pauses" rather than "this script is looking in the
    wrong place."
    """
    where = "event_type = $1"
    params: list[Any] = [PRODUCED_EVENT]
    if since is not None:
        where += " AND ts >= $2"
        params.append(since)

    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT count(*)::int FROM audit.events WHERE {where}", *params
        )
        by_decision = await conn.fetch(
            f"""
            SELECT coalesce(payload->>'decision', '(absent)') AS decision,
                   count(*)::int AS n
            FROM audit.events
            WHERE {where}
            GROUP BY 1
            ORDER BY 2 DESC
            """,
            *params,
        )
        suppressed = await conn.fetch(
            f"""
            SELECT coalesce(payload->>'gap_suppressed', '(absent)') AS gap_suppressed,
                   count(*)::int AS n
            FROM audit.events
            WHERE {where}
            GROUP BY 1
            ORDER BY 2 DESC
            """,
            *params,
        )
        key_census = await conn.fetch(
            f"""
            SELECT k AS key, count(*)::int AS n
            FROM audit.events, jsonb_object_keys(payload) AS k
            WHERE {where}
            GROUP BY 1
            ORDER BY 2 DESC
            LIMIT 40
            """,
            *params,
        )

    return {
        "total": total or 0,
        "by_decision": [{"decision": r["decision"], "n": r["n"]} for r in by_decision],
        "by_gap_suppressed": [
            {"gap_suppressed": r["gap_suppressed"], "n": r["n"]} for r in suppressed
        ],
        "payload_keys": [{"key": r["key"], "n": r["n"]} for r in key_census],
    }


def _iso(value: str) -> datetime:
    if not value.strip():
        raise argparse.ArgumentTypeError("--since must not be empty")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--since must be an ISO 8601 timestamp: {exc}"
        ) from exc


def _print_text(payload: dict) -> None:
    delivered = payload["delivered"]
    trips = payload["circuit_breaker_trips"]
    produced = payload["produced"]

    print("Circuit-breaker provenance")
    print("=" * 60)
    if payload["since"]:
        print(f"window: since {payload['since']}")
    else:
        print("window: all retained audit history")
    print()

    print(f"DELIVERED enforcement ({DELIVERED_EVENT})")
    print(f"  first        : {delivered['first_ts'] or '(none)'}")
    print(f"  last         : {delivered['last_ts'] or '(none)'}")
    print(f"  total        : {delivered['total']}")
    print(f"  agents       : {delivered['distinct_agents']}")
    for row in delivered["monthly"]:
        print(f"    {row['month']}  {row['n']:>6}")
    print()

    print(f"cross-check ({TRIP_EVENT}, expected ~1:1 with delivered)")
    print(f"  first        : {trips['first_ts'] or '(none)'}")
    print(f"  total        : {trips['total']}")
    delta = trips["total"] - delivered["total"]
    if delta:
        print(f"  NOTE         : differs from delivered by {delta:+d} — investigate")
    print()

    print(f"PRODUCED verdicts ({PRODUCED_EVENT})")
    print(f"  total        : {produced['total']}")
    for row in produced["by_decision"]:
        print(f"    decision={row['decision']:<20} {row['n']:>6}")
    for row in produced["by_gap_suppressed"]:
        print(f"    gap_suppressed={row['gap_suppressed']:<13} {row['n']:>6}")
    if any(r["decision"] == "(absent)" for r in produced["by_decision"]):
        print("  payload keys actually present (top 40):")
        for row in produced["payload_keys"]:
            print(f"    {row['key']:<34} {row['n']:>6}")
    print()

    print("What this does NOT show")
    print("-" * 60)
    print("  A delivered pause proves the breaker CAN actuate. It does not show")
    print("  prevention, benefit, or correctness — those are counterfactual and")
    print("  need a shadow/prospective record, not a count. Do not read the")
    print("  delivered total as a harm-prevention count.")
    print("  A produced verdict that was gap-suppressed did NOT enforce.")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        type=_iso,
        default=None,
        help="Only consider audit rows at or after this ISO 8601 timestamp.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args()

    pool = await get_db()
    try:
        delivered = await _event_timeline(pool, DELIVERED_EVENT, args.since)
        trips = await _event_timeline(pool, TRIP_EVENT, args.since)
        produced = await _produced_verdicts(pool, args.since)
    finally:
        await close_db()

    payload = {
        "since": args.since.isoformat() if args.since else None,
        "delivered": delivered,
        "circuit_breaker_trips": trips,
        "produced": produced,
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_text(payload)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
