#!/usr/bin/env python3
"""R1 lineage maintenance CLI.

Subcommands:

1. ``promote-provisional`` — re-score provisional lineage claims. Plausible
   scores call ``confirm_lineage`` only when ``--apply`` is passed. Unsupported
   scores call ``demote_lineage`` only when ``--apply-orphans`` is passed. The
   score evaluation itself writes the normal R1 audit/KG records.

2. ``archive-public-kg`` — archive public R1 KG score nodes. Defaults to
   dry-run; pass ``--apply`` to update statuses. ``--ttl-days 0`` archives
   ALL open nodes regardless of age (one-shot backlog cleanup, since R1
   scores are no longer emitted to the public KG).
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
    promote.add_argument(
        "--apply-orphans",
        action="store_true",
        help=(
            "Call demote_lineage(reason='r1_unsupported') for unsupported scores. "
            "Without this flag unsupported rows are reported only."
        ),
    )
    promote.add_argument("--limit", type=int, default=None, help="Limit candidates evaluated.")

    archive = sub.add_parser(
        "archive-public-kg",
        help="Archive stale public R1 KG score nodes after the TTL.",
    )
    archive.add_argument(
        "--ttl-days",
        type=int,
        default=30,
        help="TTL in days (default: 30). Use 0 to archive ALL open nodes regardless of age "
             "(one-shot backlog cleanup now that R1 scores are no longer emitted to the KG).",
    )
    archive.add_argument("--limit", type=int, default=None, help="Limit rows archived.")
    archive.add_argument("--apply", action="store_true", help="Archive rows instead of dry-run.")

    args = parser.parse_args()

    if args.command == "promote-provisional":
        result = await sweep_provisional_lineage(
            apply=args.apply,
            apply_orphans=args.apply_orphans,
            limit=args.limit,
        )
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
