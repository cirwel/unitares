#!/usr/bin/env python3
"""Report S22 H5 comparable task-entry coverage.

This is a read-only diagnostic. It reads durable S22 write context from
``core.agent_state.state_json.provenance_context`` and
``knowledge.discoveries.provenance.s22_context``. It does not create task
entries; it only reports whether one shared comparison key covers Hermes,
Claude Code, and Codex CLI.
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
from src.identity.s22_h5_comparison import (
    DEFAULT_REQUIRED_HARNESSES,
    assess_s22_h5_coverage,
    build_s22_h5_missing_payloads,
    collect_s22_h5_entries,
)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit-per-source",
        type=int,
        default=200,
        help="Maximum S22 rows to read from agent_state and KG independently.",
    )
    parser.add_argument(
        "--required-harness",
        action="append",
        dest="required_harnesses",
        help=(
            "Required harness for the H5 gate. Repeat to override the default "
            f"set: {', '.join(DEFAULT_REQUIRED_HARNESSES)}."
        ),
    )
    parser.add_argument(
        "--comparison-key",
        help=(
            "Restrict the diagnostic to one H5 comparison key. Useful when "
            "verifying the bounded task shared across harnesses."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full assessment payload as JSON.",
    )
    parser.add_argument(
        "--show-missing-payloads",
        action="store_true",
        help=(
            "Include process_agent_update payload templates for missing "
            "required harness entries. Read-only; does not call the tool."
        ),
    )
    parser.add_argument(
        "--task-label",
        default="Run S22 H5 coverage diagnostic",
        help="Task label to include in missing-entry payload templates.",
    )
    parser.add_argument(
        "--task-outcome",
        default="diagnostic-complete",
        help="Task outcome to include in missing-entry payload templates.",
    )
    args = parser.parse_args()

    try:
        entries = await collect_s22_h5_entries(
            limit_per_source=args.limit_per_source,
            comparison_key=args.comparison_key,
        )
        assessment = assess_s22_h5_coverage(
            entries,
            required_harnesses=(
                tuple(args.required_harnesses)
                if args.required_harnesses
                else DEFAULT_REQUIRED_HARNESSES
            ),
        )
        if args.comparison_key:
            assessment["target_comparison_key"] = args.comparison_key
        if args.show_missing_payloads:
            assessment["missing_payloads"] = build_s22_h5_missing_payloads(
                assessment,
                comparison_key=args.comparison_key,
                task_label=args.task_label,
                task_outcome=args.task_outcome,
            )
    finally:
        await close_db()

    if args.json:
        print(json.dumps(assessment, indent=2, sort_keys=True))
        return 0

    _print_text_report(assessment)
    return 0


def _print_text_report(assessment: dict) -> None:
    print(f"decision: {assessment['decision']}")
    print(f"reason: {assessment['reason']}")
    if assessment.get("target_comparison_key"):
        print(f"target comparison key: {assessment['target_comparison_key']}")
    print(f"entries: {assessment['entry_count']}")
    print(f"comparable entries: {assessment['comparable_entry_count']}")
    print(f"required harnesses: {', '.join(assessment['required_harnesses'])}")
    print(
        "present harnesses: "
        f"{', '.join(assessment['present_harnesses']) or 'none'}"
    )
    print(
        "comparable harnesses: "
        f"{', '.join(assessment['comparable_harnesses']) or 'none'}"
    )
    missing = assessment["missing_comparable_harnesses"]
    print(f"missing comparable harnesses: {', '.join(missing) if missing else 'none'}")
    complete = assessment["complete_comparison_keys"]
    print(f"complete comparison keys: {', '.join(complete) if complete else 'none'}")

    for comparison in assessment["comparison_sets"]:
        print(
            "comparison: "
            f"{comparison['comparison_key']} "
            f"entries={comparison['entry_count']} "
            f"harnesses={','.join(comparison['harnesses']) or 'none'} "
            f"missing={','.join(comparison['missing_harnesses']) or 'none'}"
        )

    for recommendation in assessment.get("recommendations", []):
        print(f"recommendation: {recommendation}")

    for payload in assessment.get("missing_payloads", []):
        harness = payload.get("arguments", {}).get("harness_type", "unknown")
        print(f"missing payload for {harness}: {json.dumps(payload, sort_keys=True)}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
