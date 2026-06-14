#!/usr/bin/env python3
"""Run one supervised resident-validation canary invocation.

This CLI is the safe cron/launchd/BEAM handoff surface for resident validation:
it acquires a local invocation lock, appends bounded canary ticks to JSONL,
records a local audit event, and prints only a constant non-sensitive status.
It does not submit UNITARES writes, open issues, request dialectic, deploy,
merge, or roll back anything.
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
from src.resident_validation_invocation import (  # noqa: E402
    INVOCATION_EVENT_TYPE,
    InvocationLockHeld,
    SupervisedInvocationPlan,
    run_supervised_canary_invocation,
)


def _parse_observed_at(value: str | None) -> datetime | None:
    """Parse an ISO timestamp, accepting a trailing Z for UTC."""
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build_parser() -> argparse.ArgumentParser:
    """Create the supervised invocation CLI parser."""
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
    parser.add_argument("--max-ticks-per-run", type=int, default=1)
    parser.add_argument("--lock-ttl-seconds", type=int, default=300)
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path("data/resident_validation/canary.jsonl"),
    )
    parser.add_argument(
        "--lock-path",
        type=Path,
        default=Path("data/resident_validation/supervised.lock.json"),
    )
    parser.add_argument(
        "--audit-path",
        type=Path,
        default=Path("data/resident_validation/supervised_invocations.jsonl"),
    )
    return parser


def _print_summary(status: str) -> None:
    """Print the public non-sensitive summary shape."""
    print(
        json.dumps(
            {"event_type": INVOCATION_EVENT_TYPE, "status": status},
            sort_keys=True,
        )
    )


def main(argv: list[str] | None = None) -> int:
    """Run one supervised resident-validation canary invocation."""
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
    plan = SupervisedInvocationPlan(
        profile=profile,
        state_path=args.state_path,
        lock_path=args.lock_path,
        audit_path=args.audit_path,
        max_ticks_per_run=args.max_ticks_per_run,
        lock_ttl_seconds=args.lock_ttl_seconds,
    )
    try:
        summary = run_supervised_canary_invocation(
            plan,
            count=args.count,
            observation=args.observation,
            prediction=args.prediction,
            confidence=args.confidence,
            now=_parse_observed_at(args.observed_at),
        )
    except InvocationLockHeld:
        _print_summary("lock_held")
        return 75
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
