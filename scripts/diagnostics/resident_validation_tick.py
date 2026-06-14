#!/usr/bin/env python3
"""Emit one bounded resident-validation tick envelope.

This CLI is intentionally a one-tick primitive. Supervisors, launchd jobs, BEAM
processes, or Hermes cron can call it repeatedly to create a long-running
resident validation stream without giving the resident direct deploy/merge
authority.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.resident_validation import (  # noqa: E402
    ResidentProfile,
    build_process_update_kwargs,
    build_tick_envelope,
)


def _parse_observed_at(value: str | None) -> datetime | None:
    """Parse an ISO timestamp, accepting a trailing Z for UTC."""
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _append_state(path: Path, payload: dict) -> None:
    """Append the sorted JSON envelope to a local JSONL state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
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
    parser.add_argument("--tick-index", required=True, type=int)
    parser.add_argument("--observation", required=True)
    parser.add_argument("--prediction", required=True)
    parser.add_argument("--confidence", required=True, type=float)
    parser.add_argument("--observed-at", default=None)
    parser.add_argument("--state-path", type=Path, default=None)
    parser.add_argument(
        "--process-update-kwargs",
        action="store_true",
        help="Output process_agent_update kwargs instead of the raw tick envelope.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
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
    envelope = build_tick_envelope(
        profile,
        tick_index=args.tick_index,
        observation=args.observation,
        prediction=args.prediction,
        confidence=args.confidence,
        now=_parse_observed_at(args.observed_at),
    )
    payload = build_process_update_kwargs(envelope) if args.process_update_kwargs else envelope
    if args.state_path:
        _append_state(args.state_path, payload)
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
