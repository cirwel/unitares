#!/usr/bin/env python3
"""Backfill historical task_* outcomes into per-channel tactical calibration state.

After bumping epoch to 3, the new `tasks` channel starts empty. This script
replays eligible rows from audit.outcome_events.task_* through the tracker so
the channel begins with real history instead of a cold start.

test_* is NOT backfilled: production rows do not carry detail->>'reported_confidence'
for tests (verified 2026-04-26 against live DB — 0/2300 coverage). The tests
channel accumulates forward from this PR's deployment.

Usage:
    python3 scripts/dev/backfill_tactical_calibration.py [--dry-run] [--days 30]
"""

import argparse
import asyncio
import json
import sys
from typing import Dict, List

from src.sequential_calibration import sequential_calibration_tracker
from src.calibration import calibration_checker
from src.mcp_handlers.observability.outcome_events import (
    _HARD_EXOGENOUS_TYPE_TO_CHANNEL,
)


# task_* are the only types this script backfills — see module docstring.
BACKFILL_TYPES = ("task_completed", "task_failed")


async def fetch_eligible_rows(days: int) -> List[Dict]:
    """Read task_* rows with reconstructable confidence within the look-back window.

    No epoch filter: task outcome correctness (did the task succeed?) is
    epoch-invariant for calibration purposes. Scoping by `MAX(epoch)` after
    the 2→3 bump returned zero rows because all historical task_* data lives
    at epoch 2; the truth signal is still valid for calibration regardless of
    which governance epoch it was stamped under.
    """
    from src.db import get_db
    db = get_db()
    # Two confidence keys exist in production: `reported_confidence` (newer code
    # path, ~639 rows) and `confidence` (older / input passthrough, ~951 rows).
    # COALESCE both to maximize backfill coverage; either is the same semantic
    # signal (agent's stated confidence at decision time).
    sql = """
        SELECT
            ts,
            outcome_type,
            agent_id,
            is_bad,
            COALESCE(
                (detail->>'reported_confidence')::float,
                (detail->>'confidence')::float
            ) AS confidence
        FROM audit.outcome_events
        WHERE outcome_type = ANY($1)
          AND ts > NOW() - ($2 || ' days')::interval
          AND (
              detail->>'reported_confidence' IS NOT NULL
              OR detail->>'confidence' IS NOT NULL
          )
        ORDER BY ts ASC
    """
    async with db.acquire() as conn:
        rows = await conn.fetch(sql, list(BACKFILL_TYPES), str(days))
    return [dict(r) for r in rows]


async def backfill(days: int, dry_run: bool) -> Dict[str, int]:
    """Replay eligible historical rows into the tracker.

    On any exception during fetch or replay, exit non-zero before save_state();
    state file is not partially mutated.
    """
    summary = {
        "candidates": 0,
        "replayed": 0,
        "skipped_no_confidence": 0,
        "skipped_unknown_channel": 0,
    }

    # Verify epoch alignment: tracker init handles migration, but if someone
    # ran backfill before letting the server restart once, the tracker may
    # still hold pre-migration state. Refuse rather than silently corrupt.
    from src.sequential_calibration import GovernanceConfig
    if sequential_calibration_tracker.state_file.exists():
        with open(sequential_calibration_tracker.state_file, "r") as f:
            on_disk = json.load(f)
        on_disk_epoch = int(on_disk.get("epoch", 1))
        if on_disk_epoch != GovernanceConfig.CURRENT_EPOCH:
            print(
                f"State file is at epoch {on_disk_epoch}; current is "
                f"{GovernanceConfig.CURRENT_EPOCH}. Restart governance-mcp "
                f"once first to trigger the migration, then re-run.",
                file=sys.stderr,
            )
            raise SystemExit(2)

    rows = await fetch_eligible_rows(days)
    summary["candidates"] = len(rows)

    for row in rows:
        confidence = row.get("confidence")
        if confidence is None:
            summary["skipped_no_confidence"] += 1
            continue
        channel = _HARD_EXOGENOUS_TYPE_TO_CHANNEL.get(row["outcome_type"])
        if not channel:
            summary["skipped_unknown_channel"] += 1
            continue

        if not dry_run:
            sequential_calibration_tracker.record_exogenous_tactical_outcome(
                confidence=float(confidence),
                outcome_correct=not bool(row["is_bad"]),
                agent_id=row.get("agent_id"),
                signal_source=channel,
                outcome_type=row["outcome_type"],
                persist=False,  # critical: no per-row writes
            )
            # Also feed the bin-level CalibrationChecker so per_channel_calibration
            # populates. The runtime handler at outcome_events.py:282 calls both;
            # the backfill must too, or `tactical_bin_stats_by_channel` stays empty
            # and the dashboard's per-channel chips never render.
            calibration_checker.record_tactical_decision(
                confidence=float(confidence),
                decision='proceed',
                immediate_outcome=not bool(row["is_bad"]),
                signal_source=channel,
            )
            summary["replayed"] += 1

    if not dry_run and summary["replayed"] > 0:
        # Single atomic save for the SequentialCalibrationTracker (json file).
        sequential_calibration_tracker.save_state()

        # CalibrationChecker writes to postgres via fire-and-forget _run_async.
        # In a short-lived script those tasks get cancelled when asyncio.run
        # exits, so the per-channel bin stats never persist. Force a synchronous
        # flush by awaiting the postgres write directly.
        try:
            from src.db import get_db
            db = get_db()
            state_data = {
                'bins': {k: dict(v) for k, v in calibration_checker.bin_stats.items()},
                'complexity_bins': {k: dict(v) for k, v in calibration_checker.complexity_stats.items()},
                'tactical_bins': {k: dict(v) for k, v in calibration_checker.tactical_bin_stats.items()},
                'tactical_bins_by_channel': {
                    channel: {k: dict(v) for k, v in bins.items()}
                    for channel, bins in calibration_checker.tactical_bin_stats_by_channel.items()
                },
            }
            await db.update_calibration(state_data)
        except Exception as e:
            print(f"Warning: failed to flush CalibrationChecker to postgres: {e}", file=sys.stderr)
            print("  SequentialCalibrationTracker state was saved successfully.", file=sys.stderr)
            print("  per_channel_calibration may be empty until the running mcp organically populates it.", file=sys.stderr)

    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Report counts without mutating state.")
    parser.add_argument("--days", type=int, default=30,
                        help="Look-back window in days (default 30).")
    args = parser.parse_args()

    summary = asyncio.run(backfill(days=args.days, dry_run=args.dry_run))
    print("Backfill summary:")
    for key, value in summary.items():
        print(f"  {key}: {value}")
    print("\nNext: restart governance-mcp; check_calibration should now report"
          " per_channel_calibration with a populated 'tasks' entry.")


if __name__ == "__main__":
    main()
