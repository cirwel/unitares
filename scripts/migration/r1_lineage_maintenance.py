#!/usr/bin/env python3
"""R1 lineage maintenance CLI.

Subcommands:

1. ``promote-provisional`` — re-score provisional lineage claims. Plausible
   scores call ``confirm_lineage`` only when ``--apply`` is passed. The score
   evaluation itself writes the normal R1 audit/KG records.

2. ``archive-public-kg`` — archive stale public R1 KG score nodes after the
   v3.2-D TTL. Defaults to dry-run; pass ``--apply`` to update statuses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.identity.r1_maintenance import (
    archive_stale_public_r1_scores,
    sweep_provisional_lineage,
)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    promote = sub.add_parser(
        "promote-provisional",
        help="Re-score provisional lineage and optionally confirm plausible claims.",
    )
    promote.add_argument(
        "--apply",
        action="store_true",
        help="Call confirm_lineage for plausible scores. Scoring always writes R1 audit/KG records.",
    )
    promote.add_argument("--limit", type=int, default=None, help="Limit candidates evaluated.")

    archive = sub.add_parser(
        "archive-public-kg",
        help="Archive stale public R1 KG score nodes after the TTL.",
    )
    archive.add_argument("--ttl-days", type=int, default=30, help="TTL in days (default: 30).")
    archive.add_argument("--limit", type=int, default=None, help="Limit rows archived.")
    archive.add_argument("--apply", action="store_true", help="Archive rows instead of dry-run.")

    args = parser.parse_args()

    if args.command == "promote-provisional":
        result = await sweep_provisional_lineage(apply=args.apply, limit=args.limit)
    else:
        result = await archive_stale_public_r1_scores(
            ttl_days=args.ttl_days,
            dry_run=not args.apply,
            limit=args.limit,
        )

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
