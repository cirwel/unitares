#!/usr/bin/env python3
"""Find durable label fields that cannot discriminate what their name promises.

## What this checks, and what it deliberately does NOT

It does NOT detect wrong causes. Nothing can, from outside: a row saying
`reason='liveness_timeout'` is indistinguishable from a correct one until
someone reads the transcript it summarises.

What IS checkable is weaker and still useful: whether a field CAN carry the
distinction its name implies. A discriminator with one distinct value over a
real window is not discriminating anything, and every reader who partitions on
it gets a silently empty answer. That is a property of the data, not a
judgment about intent, so it can be asserted without a narrative.

## Why this exists

2026-08-18. A session (mine) read `{"action":"failed","reason":"liveness_timeout"}`
off swept dialectic sessions and reported a 63% failure rate as evidence the
review channel was broken. The rows were accurate. The story was invented: 25
of 26 carried a standing reviewer rejection and the protocol had run to
completion. Three more claims from the same session failed the same way, and
in each case the check that would have caught it was one query against a
database that was open the whole time.

The generalisation drawn from that — "the fleet's records assert causes they
cannot support" — was then audited, and it is FALSE. Worth recording, because
the wrong lesson is more expensive than the original error:

  - `audit.events` reason values overwhelmingly carry their evidence inline
    ("Boundary basin — near state-space edge (risk=0.00, I=0.69)",
    "confidence 0.206 < threshold 0.55"). That is the good pattern.
  - Of the bare enum-like labels, nearly all are direct OBSERVATIONS the
    writer made (`suppressed_event_type`, `pg_session_missing`).
  - `verification_source` and `spawn_reason` discriminate properly.

The real defect class is narrower and different: **names that promise a
discrimination the value does not deliver.** `status='failed'` for a standing
rejection. `paused_agent_id` for an agent at status='active'. A metric called
`agent_kg_retrieval` that counted audit/cleanup sweeps. Those do not read as
wrong — they read as correct, to everyone including their author, which is
exactly why a human reviewing the output cannot catch them.

Degenerate discrimination is the mechanically detectable corner of that class.
This script finds it. The rest still needs a reader.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Columns whose NAME promises to separate cases. A single distinct value here
# means every downstream partition, filter, or GROUP BY silently collapses.
LABEL_COLUMNS: list[tuple[str, str, str]] = [
    ("core.dialectic_sessions", "trigger_source", "created_at"),
    ("core.dialectic_sessions", "status", "created_at"),
    ("core.dialectic_sessions", "session_type", "created_at"),
    ("core.agents", "spawn_reason", "created_at"),
    ("core.identities", "spawn_reason", "created_at"),
    ("audit.outcome_events", "verification_source", "ts"),
    ("audit.outcome_events", "eisv_verdict", "ts"),
    ("audit.r1_score_audit", "verdict", "recorded_at"),
]

# jsonb keys inside audit.events that name a cause or a judgment.
PAYLOAD_KEY_PATTERN = r"reason|cause|verdict|basis|explanation"

# A label is THIN when one value covers this share — still technically
# discriminating, but any analysis resting on it is resting on the tail.
THIN_SHARE = 0.99


def connect():
    import psycopg2  # type: ignore

    dsn = os.environ.get(
        "GOVERNANCE_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/governance",
    )
    return psycopg2.connect(dsn)


def _verdict(rows: int, distinct: int, top_share: float | None) -> str:
    if rows == 0:
        return "NO_DATA"
    if distinct <= 1:
        return "DEGENERATE"
    if top_share is not None and top_share >= THIN_SHARE:
        return "THIN"
    return "ok"


def audit_columns(cur, days: int) -> list[dict]:
    out = []
    for table, column, ts_col in LABEL_COLUMNS:
        # Table may not exist in every deployment; a missing table is not a
        # finding, so it is reported as such rather than raising.
        cur.execute(
            "SELECT to_regclass(%s) IS NOT NULL",
            (table,),
        )
        if not cur.fetchone()[0]:
            out.append({"field": f"{table}.{column}", "verdict": "ABSENT"})
            continue

        cur.execute(
            f"""
            SELECT count(*) AS rows,
                   count(DISTINCT {column}) AS distinct_values,
                   (SELECT count(*) FROM {table}
                     WHERE {ts_col} > now() - make_interval(days => %(days)s)
                     GROUP BY {column} ORDER BY count(*) DESC LIMIT 1) AS top_n
            FROM {table}
            WHERE {ts_col} > now() - make_interval(days => %(days)s)
            """,
            {"days": days},
        )
        rows, distinct, top_n = cur.fetchone()
        top_share = (top_n / rows) if rows and top_n else None

        cur.execute(
            f"""
            SELECT coalesce({column}::text, '(null)'), count(*)
            FROM {table}
            WHERE {ts_col} > now() - make_interval(days => %(days)s)
            GROUP BY 1 ORDER BY 2 DESC LIMIT 4
            """,
            {"days": days},
        )
        out.append(
            {
                "field": f"{table}.{column}",
                "rows": rows,
                "distinct_values": distinct,
                "top_share_pct": round(100 * top_share, 1) if top_share else None,
                "top_values": [{"value": v, "n": n} for v, n in cur.fetchall()],
                "verdict": _verdict(rows, distinct, top_share),
            }
        )
    return out


def audit_payload_keys(cur, days: int) -> list[dict]:
    cur.execute(
        """
        SELECT k,
               count(*) AS rows,
               count(DISTINCT e.payload->>k) AS distinct_values,
               count(DISTINCT e.event_type) AS event_types
        FROM audit.events e, LATERAL jsonb_object_keys(e.payload) k
        WHERE e.ts > now() - make_interval(days => %(days)s)
          AND k ~ %(pattern)s
        GROUP BY 1 ORDER BY 2 DESC
        """,
        {"days": days, "pattern": PAYLOAD_KEY_PATTERN},
    )
    out = []
    for key, rows, distinct, event_types in cur.fetchall():
        out.append(
            {
                "field": f"audit.events.payload->>{key}",
                "rows": rows,
                "distinct_values": distinct,
                "event_types": event_types,
                # A key spanning many event types with one value is the worst
                # case: it reads as a shared vocabulary and carries nothing.
                "verdict": _verdict(rows, distinct, None),
            }
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 if any label is DEGENERATE (for CI)",
    )
    args = ap.parse_args()
    if args.days <= 0:
        ap.error("--days must be positive")

    with connect() as conn:
        with conn.cursor() as cur:
            findings = audit_columns(cur, args.days) + audit_payload_keys(cur, args.days)

    if args.json:
        print(json.dumps(findings, indent=2))
    else:
        print(f"Label discrimination audit — last {args.days}d")
        print("  DEGENERATE = one distinct value; the field cannot separate anything.")
        print(f"  THIN = one value covers >={THIN_SHARE:.0%}; analysis rests on the tail.\n")
        for f in sorted(findings, key=lambda x: (x["verdict"] != "DEGENERATE", x["field"])):
            head = f"  [{f['verdict']:10s}] {f['field']}"
            if f["verdict"] in ("ABSENT", "NO_DATA"):
                print(head)
                continue
            print(
                f"{head}  rows={f['rows']} distinct={f['distinct_values']}"
                + (f" top={f['top_share_pct']}%" if f.get("top_share_pct") else "")
            )
            for v in f.get("top_values", [])[:3]:
                print(f"                 {v['n']:>7}  {v['value']}")

    degenerate = [f for f in findings if f["verdict"] == "DEGENERATE"]
    if degenerate and not args.json:
        print(
            f"\n{len(degenerate)} degenerate label(s). Each is either unused or "
            "misnamed — a reader partitioning on it gets one bucket and no warning."
        )
    return 1 if (args.strict and degenerate) else 0


if __name__ == "__main__":
    sys.exit(main())
