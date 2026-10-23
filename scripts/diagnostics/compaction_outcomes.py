#!/usr/bin/env python3
"""Report recorded context compactions and whether each identity was kept.

Read-only. Each compaction a host reported with a usable session binding is
paired with its outcome: ``minted`` (a ``spawn_reason="compaction"`` child was
created), ``kept`` (the identity wrote again), or ``no_later_activity``.
Where an adapter asked the agent whether it was the same process, its answer
is shown beside the outcome. Definitions and limits live in
``src/identity/compaction_outcomes.py``.

Usage:
    python3 scripts/diagnostics/compaction_outcomes.py
    python3 scripts/diagnostics/compaction_outcomes.py --since 2026-10-22T00:00:00Z
    python3 scripts/diagnostics/compaction_outcomes.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone


sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

from src.db import close_db
from src.identity.compaction_outcomes import OUTCOMES, collect_compaction_outcomes
from src.runtime_observations import PROBE_ANSWERS


def _parse_timestamp(value: str) -> datetime:
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("timestamp must not be empty")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "timestamp must be ISO-8601, e.g. 2026-10-22T00:00:00Z"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        type=_parse_timestamp,
        default=datetime.now(timezone.utc) - timedelta(days=30),
        help="Read compactions recorded at or after this time (default: 30 days ago).",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args()

    try:
        report = await collect_compaction_outcomes(since=args.since)
    finally:
        await close_db()

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    print(f"since: {report['since']}")
    print(f"scope: {report['scope']}")
    if not report["complete"]:
        print("coverage: INCOMPLETE, the compaction read stopped at its page cap")
    outcomes = report["outcomes"]
    print(
        f"compactions: {report['compactions']} "
        + " ".join(f"{name}={outcomes[name]}" for name in OUTCOMES)
    )
    for host, counts in report["by_host"].items():
        print(f"  {host:<12} " + " ".join(f"{name}={counts[name]}" for name in OUTCOMES))
    probe = report["probe"]
    if probe["asked"]:
        print(f"\nprobe: {probe['asked']} answered or recorded unanswered")
        print(f"  scope: {probe['scope']}")
        if not probe.get("complete", True):
            print("  coverage: INCOMPLETE, the probe read stopped at its page cap")
        for answer in PROBE_ANSWERS:
            counts = probe["by_answer_and_outcome"][answer]
            print(f"  {answer:<13} " + " ".join(f"{name}={counts[name]}" for name in OUTCOMES))
        print(
            f"  consistent={probe['consistent']} inconsistent={probe['inconsistent']} "
            f"of {probe['decisive_pairs']} decisive; "
            f"answered by a successor={probe['answered_by_successor']} "
            f"unrelated ignored={probe['unrelated_ignored']}"
        )
    if report["rows"]:
        print("\nrecent:")
        for row in report["rows"][-20:]:
            children = ",".join(child[:8] for child in row["minted_children"])
            print(
                f"  {row['compacted_at']}  {row['agent_id'][:8]}  "
                f"{row['host_family']:<8} {row['outcome']}"
                + (f" -> {children}" if children else "")
                + (f"  said {row['probe']['answer']}" if row["probe"] else "")
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
