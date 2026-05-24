#!/usr/bin/env python3
"""Report identities with no or sparse measured check-ins.

Read-only. This diagnostic is for the operational gap where fresh process UUIDs
are minted correctly, but clients do not send ``initial_state`` or real
``process_agent_update`` calls often enough to establish measured trajectories.

Usage:
    python3 scripts/diagnostics/agent_fragmentation.py
    python3 scripts/diagnostics/agent_fragmentation.py --json
    python3 scripts/diagnostics/agent_fragmentation.py --since 2026-05-20T00:00:00Z
    python3 scripts/diagnostics/agent_fragmentation.py --stale-hours 24
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone


sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

from src.db import close_db
from src.identity.agent_fragmentation import collect_agent_fragmentation


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
            "timestamp must be ISO-8601, e.g. 2026-05-20T00:00:00Z"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        type=_parse_timestamp,
        help="Restrict identities to created_at >= this timestamp.",
    )
    parser.add_argument(
        "--stale-hours",
        type=float,
        default=24.0,
        help="Age threshold for active zero/low-check-in identities.",
    )
    parser.add_argument(
        "--low-checkin-max",
        type=int,
        default=3,
        help="Maximum real check-ins counted as sparse trajectory.",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=30,
        help="Maximum sample identities to include per sample section.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args()

    try:
        report = await collect_agent_fragmentation(
            since=args.since,
            stale_hours=args.stale_hours,
            low_checkin_max=args.low_checkin_max,
            sample_limit=args.sample_limit,
        )
    finally:
        await close_db()

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    _print_text_report(report)
    return 0


def _print_text_report(report: dict) -> None:
    totals = report["totals"]
    state_rows = report["state_rows"]
    print(f"decision: {report['decision']}")
    print(f"reason: {report['reason']}")
    print(f"observed at: {report['observed_at']}")
    print(f"since: {report.get('since') or '<all>'}")
    print(
        "totals: "
        f"identities={totals['identities']} "
        f"active={totals['active']} "
        f"zero_real={totals['zero_real_checkins']} "
        f"one_real={totals['one_real_checkin']} "
        f"one_to_low={totals['one_to_low_real_checkins']} "
        f"active_zero={totals['active_zero_real_checkins']} "
        f"active_zero_stale={totals['active_zero_real_stale']} "
        f"active_one_to_low_stale={totals['active_one_to_low_real_stale']}"
    )
    print(
        "state rows: "
        f"measured={state_rows['measured_rows']} "
        f"measured_identities={state_rows['measured_identities']} "
        f"synthetic={state_rows['synthetic_rows']} "
        f"synthetic_identities={state_rows['synthetic_identities']}"
    )
    for window, block in report["recent"].items():
        print(
            f"{window}: identities={block['identities']} "
            f"zero_real={block['zero_real_checkins']} "
            f"one_to_low={block['one_to_low_real_checkins']} "
            f"gt_low={block['more_than_low_real_checkins']}"
        )

    print("\nactive zero-real by model:")
    for row in report["active_zero_by_model"][:10]:
        label = "labeled" if row["has_label"] else "unlabeled"
        print(f"  {row['model_type']:<24} {label:<9} {row['identities']}")

    print("\nrecent 7d by session source:")
    for row in report["recent_7d_by_session_source"][:10]:
        print(
            f"  {row['session_resolution_source']:<28} "
            f"{row['transport']:<14} "
            f"identities={row['identities']} "
            f"zero_real={row['zero_real_checkins']} "
            f"one_to_low={row['one_to_low_real_checkins']}"
        )

    print("\nthread clusters (active <= low-checkin-max):")
    clusters = report["thread_clusters"][:10]
    if not clusters:
        print("  none")
    for row in clusters:
        labels = ", ".join(row["sample_labels"])
        print(
            f"  {row['thread_id']}: "
            f"active_low={row['active_low_identities']} "
            f"zero_real={row['zero_real_checkins']} "
            f"one_to_low={row['one_to_low_real_checkins']} "
            f"labels={labels}"
        )

    print("\nrecommendations:")
    for item in report["recommendations"]:
        print(f"  - {item}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
