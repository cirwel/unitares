#!/usr/bin/env python3
"""Report R2 Phase 1 lineage telemetry readiness.

This is a read-only diagnostic. It counts lineage state in ``core.identities``
and lineage lifecycle events in ``audit.events``, then evaluates the Phase 2
gate from ````. It does not write
audit rows, KG rows, or lineage state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys


sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

from src.db import close_db
from src.identity.r2_phase1_telemetry import (
    DEFAULT_MIN_CONFIRMED_PAIRS,
    DEFAULT_MIN_CROSS_ROLE_REJECTIONS,
    DEFAULT_MIN_DEMOTED_PAIRS,
    DEFAULT_MIN_TELEMETRY_DAYS,
    R2Phase1Thresholds,
    collect_r2_phase1_telemetry,
    parse_since,
)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        help=(
            "Start timestamp for Phase 1 telemetry. Defaults to the R2 Phase 1 "
            "ship date, 2026-05-05T00:00:00Z."
        ),
    )
    parser.add_argument(
        "--min-telemetry-days",
        type=int,
        default=DEFAULT_MIN_TELEMETRY_DAYS,
        help="Minimum elapsed Phase 1 observation window before Phase 2 can open.",
    )
    parser.add_argument(
        "--min-confirmed-pairs",
        type=int,
        default=DEFAULT_MIN_CONFIRMED_PAIRS,
        help="Minimum confirmed lineage pairs required before Phase 2 can open.",
    )
    parser.add_argument(
        "--min-demoted-pairs",
        type=int,
        default=DEFAULT_MIN_DEMOTED_PAIRS,
        help="Minimum demoted lineage pairs required before Phase 2 can open.",
    )
    parser.add_argument(
        "--min-cross-role-rejections",
        type=int,
        default=DEFAULT_MIN_CROSS_ROLE_REJECTIONS,
        help="Minimum cross-role rejection events required before Phase 2 can open.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args()

    thresholds = R2Phase1Thresholds(
        min_telemetry_days=args.min_telemetry_days,
        min_confirmed_pairs=args.min_confirmed_pairs,
        min_demoted_pairs=args.min_demoted_pairs,
        min_cross_role_rejections=args.min_cross_role_rejections,
    )

    try:
        assessment = await collect_r2_phase1_telemetry(
            since=parse_since(args.since),
            thresholds=thresholds,
        )
    finally:
        await close_db()

    if args.json:
        print(json.dumps(assessment, indent=2, sort_keys=True))
        return 0

    _print_text_report(assessment)
    return 0


def _print_text_report(assessment: dict) -> None:
    snapshot = assessment["snapshot"]
    checks = assessment["checks"]
    identity = snapshot["identity_counts"]
    events = snapshot["audit_event_counts"]

    print(f"decision: {assessment['decision']}")
    print(f"reason: {assessment['reason']}")
    print(f"since: {snapshot['since']}")
    print(f"observed at: {snapshot['observed_at']}")
    print(f"telemetry age days: {snapshot['telemetry_age_days']}")
    print(
        "identity counts: "
        f"lineage_total={identity['lineage_total']} "
        f"active_provisional={identity['active_provisional']} "
        f"active_confirmed={identity['active_confirmed']} "
        f"demoted_total={identity['demoted_total']} "
        f"archived_total={identity['archived_total']}"
    )
    print(
        "since counts: "
        f"declared={identity['declared_since']} "
        f"confirmed={identity['confirmed_since']} "
        f"demoted={identity['demoted_since']} "
        f"archived={identity['archived_since']}"
    )
    print(
        "audit events: "
        + " ".join(f"{key}={value}" for key, value in sorted(events.items()))
    )
    for name, check in checks.items():
        status = "pass" if check["passed"] else "fail"
        print(
            "check: "
            f"{name} observed={check['observed']} "
            f"required={check['required']} {status}"
        )
    for recommendation in assessment.get("recommendations", []):
        print(f"recommendation: {recommendation}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
