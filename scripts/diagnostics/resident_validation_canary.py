#!/usr/bin/env python3
"""Run one bounded resident-validation canary tick batch.

The canary is intentionally stateful but non-actuating: it appends raw
``resident_validation_tick`` envelopes to JSONL and prints a compact JSON
summary. A future launchd/BEAM supervisor can call this repeatedly for
long-running validation without giving the canary merge/deploy authority.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.resident_validation import ResidentProfile  # noqa: E402
from src.resident_validation_runner import build_canary_ticks  # noqa: E402


def _parse_observed_at(value: str | None) -> datetime | None:
    """Parse an ISO timestamp, accepting a trailing Z for UTC."""
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build_parser() -> argparse.ArgumentParser:
    """Create the canary CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort-id", required=True)
    parser.add_argument("--resident-id", required=True)
    parser.add_argument("--resident-name", required=True)
    parser.add_argument(
        "--role",
        required=True,
        choices=("dogfood_probe", "steward", "builder", "reviewer"),
    )
    parser.add_argument("--cadence-seconds", required=True, type=int)
    parser.add_argument(
        "--observation-scope",
        default="repo,ci,kg,dialectic",
        help="Comma-separated observation scopes for this resident.",
    )
    parser.add_argument("--observation", required=True)
    parser.add_argument("--prediction", required=True)
    parser.add_argument("--confidence", required=True, type=float)
    parser.add_argument("--observed-at", default=None)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path("data/resident_validation/canary.jsonl"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the canary tick batch."""
    args = build_parser().parse_args(argv)
    scope = tuple(
        part.strip() for part in args.observation_scope.split(",") if part.strip()
    )
    profile = ResidentProfile(
        cohort_id=args.cohort_id,
        resident_id=args.resident_id,
        resident_name=args.resident_name,
        role=args.role,
        cadence_seconds=args.cadence_seconds,
        observation_scope=scope,
    )
    ticks = build_canary_ticks(
        profile,
        state_path=args.state_path,
        count=args.count,
        observation=args.observation,
        prediction=args.prediction,
        confidence=args.confidence,
        now=_parse_observed_at(args.observed_at),
    )
    print(
        json.dumps(
            {
                "event_type": "resident_validation_canary_batch",
                "cohort_id": args.cohort_id,
                "resident_id": args.resident_id,
                "state_path": str(args.state_path),
                "ticks": ticks,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
