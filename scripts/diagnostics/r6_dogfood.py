#!/usr/bin/env python3
"""Generate or assess R6 H1/H3 dogfood payloads.

By default this script is offline and only prints payload templates. Pass
``--assess`` to read existing S22 write-context rows for the comparison key.
It never writes governance state or KG rows.
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
from src.identity.r6_dogfood import (
    assess_r6_dogfood_entries,
    build_r6_dogfood_payloads,
    default_r6_comparison_key,
)
from src.identity.s22_h5_comparison import collect_s22_h5_entries


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiment",
        choices=["h1", "h3"],
        required=True,
        help="R6 experiment to prepare or assess.",
    )
    parser.add_argument(
        "--comparison-key",
        help="Shared comparison key. Defaults to r6-<experiment>-<today>.",
    )
    parser.add_argument(
        "--model",
        default="gpt-5.5",
        help="Hermes model label for baseline entries.",
    )
    parser.add_argument(
        "--variant-model",
        help="H1 second Hermes model label. Defaults to a replacement placeholder.",
    )
    parser.add_argument(
        "--memory-context",
        default="same-hermes-memory",
        help="Stable label for the Hermes memory/profile context.",
    )
    parser.add_argument(
        "--parent-agent-id",
        help="Optional predecessor UUID to include in the H3 force_new onboard step.",
    )
    parser.add_argument(
        "--assess",
        action="store_true",
        help="Read existing S22 rows for the key and assess experiment coverage.",
    )
    parser.add_argument(
        "--show-payloads",
        action="store_true",
        help="Include payload templates. Defaults to true when --assess is omitted.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    args = parser.parse_args()

    comparison_key = (
        args.comparison_key
        or default_r6_comparison_key(args.experiment)
    )
    include_payloads = args.show_payloads or not args.assess

    payload: dict = {
        "experiment": args.experiment,
        "comparison_key": comparison_key,
        "read_only": True,
    }
    if include_payloads:
        payload["payloads"] = build_r6_dogfood_payloads(
            args.experiment,
            comparison_key=comparison_key,
            model=args.model,
            variant_model=args.variant_model,
            memory_context=args.memory_context,
            parent_agent_id=args.parent_agent_id,
        )

    if args.assess:
        try:
            entries = await collect_s22_h5_entries(comparison_key=comparison_key)
            payload["assessment"] = assess_r6_dogfood_entries(
                entries,
                experiment_id=args.experiment,
                comparison_key=comparison_key,
            )
        finally:
            await close_db()

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_text(payload)
    return 0


def _print_text(payload: dict) -> None:
    print(f"experiment: {payload['experiment']}")
    print(f"comparison key: {payload['comparison_key']}")
    print("read-only: true")
    assessment = payload.get("assessment")
    if assessment:
        print(f"decision: {assessment['decision']}")
        print(f"reason: {assessment['reason']}")
        print(f"hermes entries: {assessment['hermes_comparable_entry_count']}")
        print(f"models: {', '.join(assessment['distinct_models']) or 'none'}")
        print(f"agent ids: {', '.join(assessment['distinct_agent_ids']) or 'none'}")
        print(
            "memory contexts: "
            f"{', '.join(assessment['distinct_memory_contexts']) or 'none'}"
        )
        for recommendation in assessment.get("recommendations", []):
            print(f"recommendation: {recommendation}")

    for idx, item in enumerate(payload.get("payloads", []), start=1):
        print(f"payload {idx}: {json.dumps(item, sort_keys=True)}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
