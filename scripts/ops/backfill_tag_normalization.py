#!/usr/bin/env python3
"""
One-time backfill: canonicalize tags on existing knowledge-graph rows.

The write path now routes every new write through the extended
``normalize_tags`` (formatting + the postgresql→postgres spelling-variant map),
but historical rows were written before that and still fragment search
(``Postgres`` / ``postgres`` / ``PostgreSQL`` filed three ways). This replays
the canonical normalizer over existing discoveries so old entries stop
fragmenting search.

Two layers, mirroring the runtime split:
  * Always applies the formatting layer (``normalize_tags``) — safe, lossless
    spelling/format folding.
  * With ``--include-semantic`` also applies the curated semantic synonym map
    (``db``→``database``, ``auth``→``identity``) — the same rewrite the
    lifecycle pass performs. Off by default so the backfill stays purely
    formatting unless explicitly asked for the semantic merge.

DRY RUN BY DEFAULT. Nothing is written without ``--apply``. Run against the
live governance DB at deploy time; this script does not create or migrate
schema.

Usage:
    python3 scripts/ops/backfill_tag_normalization.py                      # dry run, formatting only
    python3 scripts/ops/backfill_tag_normalization.py --include-semantic   # dry run, + semantic map
    python3 scripts/ops/backfill_tag_normalization.py --apply              # apply, formatting only
    python3 scripts/ops/backfill_tag_normalization.py --apply --include-semantic -v
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))


# Statuses that make up the queryable corpus. Archived/cold rows are included
# so historical search over those tiers also de-fragments; this is a one-time
# pass, not the recurring lifecycle sweep (which scopes to the active corpus).
CORPUS_STATUSES = ("open", "resolved", "archived", "cold", "superseded", "disputed")


def _canonical_tags(tags, include_semantic: bool):
    """Return the canonical tag list for ``tags`` under the chosen layers."""
    from src.knowledge_graph import normalize_tags

    canonical = normalize_tags(tags)
    if include_semantic:
        from src.knowledge_ontology import apply_semantic_synonyms

        canonical = apply_semantic_synonyms(canonical)
    return canonical


async def backfill(apply: bool, include_semantic: bool, verbose: bool, limit: int) -> dict:
    from src.knowledge_graph import get_knowledge_graph

    graph = await get_knowledge_graph()

    seen_ids: set[str] = set()
    scanned = 0
    changed = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for status in CORPUS_STATUSES:
        rows = await graph.query(status=status, limit=limit)
        for discovery in rows:
            if discovery.id in seen_ids:
                continue
            seen_ids.add(discovery.id)
            scanned += 1

            current = list(discovery.tags or [])
            if not current:
                continue
            canonical = _canonical_tags(current, include_semantic)
            if canonical == current:
                continue

            changed += 1
            if verbose or not apply:
                print(f"  {discovery.id}: {current} -> {canonical}")
            if apply:
                await graph.update_discovery(discovery.id, {
                    "tags": canonical,
                    "updated_at": now_iso,
                })

    return {"scanned": scanned, "changed": changed, "applied": apply}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Write the canonicalized tags (default: dry run)")
    parser.add_argument("--include-semantic", action="store_true",
                        help="Also apply the curated semantic synonym map")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print every rewrite even when applying")
    parser.add_argument("--limit", type=int, default=10000,
                        help="Max rows per status tier to scan (default: 10000)")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY RUN"
    layers = "formatting + semantic" if args.include_semantic else "formatting only"
    print(f"[{mode}] tag-normalization backfill ({layers})")

    result = asyncio.run(
        backfill(args.apply, args.include_semantic, args.verbose, args.limit)
    )

    print(
        f"\nScanned {result['scanned']} discoveries; "
        f"{result['changed']} need(ed) canonicalization."
    )
    if not args.apply and result["changed"]:
        print("Re-run with --apply to write these changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
