#!/usr/bin/env python3
"""S8a Phase-2 backfill: stamp untagged identities and promote eligible ones.

Two operations live behind one CLI:

1. ``--stamp-untagged`` — fix Phase-1 stamp gap on existing untagged rows.
   Runs ``default_tags_for_onboard(label)`` against ``core.identities``
   rows with empty/missing tags. Without this, untagged identities (the
   day-7 audit found 72 in-window) can never become promotion candidates
   because the sweep's WHERE clause requires the ``ephemeral`` tag. The
   441-update ``claude_desktop-claude`` row is the canonical example.

2. ``--promote`` (default) — run the ephemeral → engaged_ephemeral
   promotion sweep. Same logic as the in-server background task, but
   from a CLI so operators can run it on demand or against the archived
   backlog (~3180 rows for **decision (d)** in
 ````).

The two operations are typically run in sequence:

```bash
# Phase A: stamp the untagged backlog (stamp gap)
python scripts/migration/s8a_phase2_backfill.py --stamp-untagged --dry-run
python scripts/migration/s8a_phase2_backfill.py --stamp-untagged

# Phase B: promote the ephemeral cohort (active + archived)
python scripts/migration/s8a_phase2_backfill.py --promote --include-archived --dry-run
python scripts/migration/s8a_phase2_backfill.py --promote --include-archived
```

Custom thresholds (e.g. ``--threshold 5`` to compare partition shapes)
and ``--limit N`` chunking are supported.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.grounding.class_promotion import (
    DEFAULT_PROMOTION_THRESHOLD,
    promote_engaged_ephemeral,
    stamp_untagged_identities,
)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    op = parser.add_mutually_exclusive_group()
    op.add_argument(
        "--stamp-untagged",
        action="store_true",
        help="Stamp default class tags on identities with empty/missing tags.",
    )
    op.add_argument(
        "--promote",
        action="store_true",
        help="Run ephemeral → engaged_ephemeral promotion sweep (default).",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_PROMOTION_THRESHOLD,
        help=f"Promotion threshold (only with --promote; default: {DEFAULT_PROMOTION_THRESHOLD})",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Also operate on status='archived' rows (decision-d backfill).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap rows touched per call (for chunking large backfills).",
    )
    args = parser.parse_args()

    if args.stamp_untagged:
        result = await stamp_untagged_identities(
            include_archived=args.include_archived,
            dry_run=args.dry_run,
            limit=args.limit,
        )
        print(json.dumps(result, indent=2, default=str))
        if args.dry_run:
            return 0
        if result.get("stamped", 0) == 0:
            print("\n[INFO] No untagged identities matched.", file=sys.stderr)
        else:
            print(
                f"\n[OK] Stamped {result['stamped']} untagged identities "
                f"(include_archived={result['include_archived']})",
                file=sys.stderr,
            )
        return 0

    # Default operation: promote.
    result = await promote_engaged_ephemeral(
        threshold=args.threshold,
        include_archived=args.include_archived,
        dry_run=args.dry_run,
        limit=args.limit,
    )
    print(json.dumps(result, indent=2, default=str))
    if args.dry_run:
        return 0
    if result.get("promoted", 0) == 0:
        print("\n[INFO] No identities matched promotion criteria.", file=sys.stderr)
    else:
        print(
            f"\n[OK] Promoted {result['promoted']} identities at threshold "
            f"{result['threshold']} (include_archived={result['include_archived']})",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
