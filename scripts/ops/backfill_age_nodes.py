#!/usr/bin/env python3
"""
Repair AGE graph nodes and TAGGED edges from canonical knowledge rows.

``knowledge.discoveries`` (PostgreSQL) is the source of truth; the AGE graph is
a derived index over it. Rows written while ``UNITARES_KNOWLEDGE_BACKEND`` was
``postgres`` — or whose AGE node write silently no-opped — exist in SQL but have
no ``Discovery`` vertex. Tag edits could also update the node's ``tags``
property and SQL rows without replacing derived ``TAGGED`` relationships. The
#949 SQL fallback keeps canonical rows retrievable; this pass repairs missing
vertices and reconciles both missing and stale tag assignments straight from
SQL.

Idempotent: missing structures use MERGE, and each drifted tag set is replaced
with the same canonical SQL projection on every run.

DRY RUN BY DEFAULT. Nothing is written without ``--apply``. Run against the live
governance DB; this script does not create or migrate schema. Requires the AGE
backend (``UNITARES_KNOWLEDGE_BACKEND=age``); it exits early on any other backend.

Usage:
    python3 scripts/ops/backfill_age_nodes.py                 # dry run, full scan
    python3 scripts/ops/backfill_age_nodes.py --limit 500     # dry run, recent 500
    python3 scripts/ops/backfill_age_nodes.py --apply         # repair projection
    python3 scripts/ops/backfill_age_nodes.py --apply --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))


async def _run(apply: bool, limit: int | None) -> dict:
    from src.knowledge_graph import get_knowledge_graph, selected_backend_name

    backend = selected_backend_name()
    if backend != "age":
        raise SystemExit(
            f"Active knowledge backend is '{backend}', not 'age'. The AGE node "
            "backfill only applies to the AGE graph backend. Set "
            "UNITARES_KNOWLEDGE_BACKEND=age to run it."
        )

    graph = await get_knowledge_graph()
    if not hasattr(graph, "backfill_missing_age_nodes"):
        raise SystemExit(
            f"Backend {type(graph).__name__} has no backfill_missing_age_nodes()."
        )

    return await graph.backfill_missing_age_nodes(dry_run=not apply, limit=limit)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair AGE nodes and TAGGED edges from canonical SQL.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Repair missing nodes and tag edges. Without this, dry-run only.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only scan the most recent N SQL discoveries (default: all).",
    )
    args = parser.parse_args()

    summary = asyncio.run(_run(apply=args.apply, limit=args.limit))

    mode = "APPLIED" if not summary.get("dry_run") else "DRY RUN"
    print(f"=== AGE projection backfill ({mode}) ===")
    print(f"  scanned (SQL rows):     {summary['scanned']}")
    print(f"  present in AGE graph:   {summary['age_present']}")
    print(f"  missing from AGE graph: {summary['missing']}")
    if summary.get("sample_missing"):
        print(f"  sample missing ids:     {summary['sample_missing']}")
    if not summary.get("dry_run"):
        print(f"  nodes created:          {summary['created']}")
        print(f"  node failures:          {summary['failed']}")

    print(f"  expected TAGGED edges:  {summary['tag_assignments_expected']}")
    print(f"  present TAGGED edges:   {summary['tag_assignments_present']}")
    print(f"  missing TAGGED edges:   {summary['tag_assignments_missing']}")
    print(f"  stale TAGGED edges:     {summary['tag_assignments_stale']}")
    print(f"  discoveries drifted:    {summary['tag_discoveries_drifted']}")
    if summary.get("sample_missing_tag_assignments"):
        print(
            "  sample missing tags:    "
            f"{summary['sample_missing_tag_assignments']}"
        )
    if summary.get("sample_stale_tag_assignments"):
        print(
            "  sample stale tags:      "
            f"{summary['sample_stale_tag_assignments']}"
        )

    projection_drift = (
        summary["missing"]
        or summary["tag_assignments_missing"]
        or summary["tag_assignments_stale"]
    )
    if not summary.get("dry_run"):
        print(f"  tag sets reconciled:    {summary['tags_reconciled']}")
        print(f"  tag repair failures:    {summary['tag_repair_failed']}")
        print(f"  orphan tags removed:    {summary['orphan_tags_removed']}")
    elif projection_drift:
        print("  (dry run — re-run with --apply to repair the AGE projection)")
    else:
        print("  AGE projection is in sync with the SQL source of truth.")


if __name__ == "__main__":
    main()
