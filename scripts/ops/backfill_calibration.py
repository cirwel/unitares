#!/usr/bin/env python3
"""
Backfill calibration from historical outcome_events.

Replays test_passed/test_failed outcomes that were recorded without confidence
(eprocess_eligible=false) by pairing each with the nearest prior audit trail
confidence. Feeds the results into the sequential calibration tracker.

Usage:
    python3 scripts/ops/backfill_calibration.py              # dry run
    python3 scripts/ops/backfill_calibration.py --apply       # apply
    python3 scripts/ops/backfill_calibration.py --apply -v    # verbose
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "t"}


def _parse_confidence(value) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_detail(detail):
    if isinstance(detail, dict):
        return detail
    if isinstance(detail, str):
        try:
            return json.loads(detail)
        except json.JSONDecodeError:
            return {}
    return {}


def _normalize_timestamp(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


async def backfill(apply: bool = False, verbose: bool = False) -> dict:
    from src.db import get_db
    from src.sequential_calibration import SequentialCalibrationTracker

    db = get_db()
    await db.init()

    # Rebuild from the database history in timestamp order. This keeps the
    # e-process chronology intact and makes the script idempotent: re-running
    # --apply rewrites the tracker from source-of-truth outcomes instead of
    # replaying the same rows into already-mutated state.
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT agent_id, outcome_type, is_bad, detail, ts
            FROM audit.outcome_events
            ORDER BY ts ASC
            """
        )

    total = len(rows)
    included_existing = 0
    paired_missing_conf = 0
    skipped_no_match = 0
    rebuilt_samples: list[dict] = []

    for row in rows:
        outcome_ts = row["ts"]
        outcome_type = row["outcome_type"]
        detail = _coerce_detail(row.get("detail"))
        signal_source = detail.get("hard_exogenous_signal")
        if not signal_source and outcome_type in ("test_passed", "test_failed"):
            signal_source = "tests"
        if not signal_source:
            continue

        reported_conf = _parse_confidence(detail.get("reported_confidence"))
        if reported_conf is not None and _truthy(detail.get("eprocess_eligible")):
            included_existing += 1
            rebuilt_samples.append(
                {
                    "agent_id": row["agent_id"],
                    "confidence": reported_conf,
                    "outcome_correct": not row["is_bad"],
                    "signal_source": signal_source,
                    "decision_action": detail.get("decision_action"),
                    "outcome_type": outcome_type,
                    "timestamp": _normalize_timestamp(outcome_ts),
                    "prediction_id": detail.get("prediction_id"),
                }
            )
            continue

        # Historical gap we can repair safely: test outcomes with no recorded confidence.
        if outcome_type not in ("test_passed", "test_failed"):
            continue

        confidence = await db.get_latest_confidence_before(
            before_ts=outcome_ts,
            agent_id=row["agent_id"],
        )

        if confidence is None:
            skipped_no_match += 1
            if verbose:
                print(f"  SKIP {outcome_type} at {outcome_ts} — no prior confidence found")
            continue

        outcome_correct = outcome_type == "test_passed"
        paired_missing_conf += 1

        if verbose:
            print(
                f"  PAIR {outcome_type} at {outcome_ts} "
                f"← confidence={confidence:.3f} → correct={outcome_correct}"
            )

        rebuilt_samples.append(
            {
                "agent_id": row["agent_id"],
                "confidence": confidence,
                "outcome_correct": outcome_correct,
                "signal_source": signal_source,
                "decision_action": detail.get("decision_action"),
                "outcome_type": outcome_type,
                "timestamp": _normalize_timestamp(outcome_ts),
                "prediction_id": detail.get("prediction_id"),
            }
        )

    if apply:
        tracker = SequentialCalibrationTracker()
        tracker.reset()
        for sample in rebuilt_samples:
            tracker.record_exogenous_tactical_outcome(
                confidence=sample["confidence"],
                outcome_correct=sample["outcome_correct"],
                agent_id=sample["agent_id"],
                signal_source=sample["signal_source"],
                decision_action=sample["decision_action"],
                outcome_type=sample["outcome_type"],
                timestamp=sample["timestamp"],
                prediction_id=sample["prediction_id"],
                persist=False,
            )
        tracker.save_state()


    result = {
        "total_outcomes": total,
        "rebuilt_samples": len(rebuilt_samples),
        "included_existing_eligible": included_existing,
        "paired_missing_confidence": paired_missing_conf,
        "skipped_no_match": skipped_no_match,
        "applied": apply,
    }

    if apply:
        metrics = tracker.compute_metrics()
        result["tracker_state"] = {
            "status": metrics.get("status", "no_data"),
            "eligible_samples": metrics.get("eligible_samples", 0),
        }
        for field in ("log_evidence", "capped_alarm"):
            if field in metrics:
                result["tracker_state"][field] = metrics[field]

    return result


def main():
    parser = argparse.ArgumentParser(description="Backfill calibration from outcome_events")
    parser.add_argument("--apply", action="store_true", help="Actually record to tracker (default: dry run)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print each pairing")
    args = parser.parse_args()

    if not args.apply:
        print("DRY RUN — pass --apply to record to sequential calibration tracker\n")

    result = asyncio.run(backfill(apply=args.apply, verbose=args.verbose))

    print(f"\nResults:")
    print(f"  Total outcome rows:        {result['total_outcomes']}")
    print(f"  Rebuilt samples:           {result['rebuilt_samples']}")
    print(f"  Included existing:         {result['included_existing_eligible']}")
    print(f"  Paired missing confidence: {result['paired_missing_confidence']}")
    print(f"  Skipped (no match):        {result['skipped_no_match']}")
    print(f"  Applied:                   {result['applied']}")

    if "tracker_state" in result:
        ts = result["tracker_state"]
        print(f"\n  Tracker state after backfill:")
        print(f"    status:           {ts['status']}")
        print(f"    eligible_samples: {ts['eligible_samples']}")
        if "log_evidence" in ts:
            print(f"    log_evidence:     {ts['log_evidence']:.4f}")
        if "capped_alarm" in ts:
            print(f"    capped_alarm:     {ts['capped_alarm']:.4f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
