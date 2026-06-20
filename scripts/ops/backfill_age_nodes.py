#!/usr/bin/env python3
"""
One-time backfill: rebuild AGE graph nodes for SQL-only knowledge discoveries.

``knowledge.discoveries`` (PostgreSQL) is the source of truth; the AGE graph is
a derived index over it. Rows written while ``UNITARES_KNOWLEDGE_BACKEND`` was
``postgres`` — or whose AGE node write silently no-opped — exist in SQL but have
no ``Discovery`` vertex. The #949 SQL fallback makes those rows retrievable via
``knowledge(action='get'|'search'|'list')``, but graph traversal (response
chains, related-edge expansion, tag rollups, hybrid graph search) can only see
rows that have an AGE node. This pass recreates the missing vertices and their
edges (AUTHORED / RESPONDS_TO / RELATED_TO / TAGGED) straight from SQL.

Idempotent: every Cypher builder uses MERGE, so re-running only fills gaps.

DRY RUN BY DEFAULT. Nothing is written without ``--apply``. Run against the live
governance DB; this script does not create or migrate schema. Requires the AGE
backend (``UNITARES_KNOWLEDGE_BACKEND=age``); it exits early on any other backend.

Usage:
    python3 scripts/ops/backfill_age_nodes.py                 # dry run, full scan
    python3 scripts/ops/backfill_age_nodes.py --limit 500     # dry run, recent 500
    python3 scripts/ops/backfill_age_nodes.py --apply         # rebuild missing nodes
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
        description="Rebuild AGE graph nodes for SQL-only knowledge discoveries.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the missing AGE nodes. Without this, runs as a dry run.",
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
    print(f"=== AGE node backfill ({mode}) ===")
    print(f"  scanned (SQL rows):     {summary['scanned']}")
    print(f"  present in AGE graph:   {summary['age_present']}")
    print(f"  missing from AGE graph: {summary['missing']}")
    if summary.get("sample_missing"):
        print(f"  sample missing ids:     {summary['sample_missing']}")
    if not summary.get("dry_run"):
        print(f"  nodes created:          {summary['created']}")
        print(f"  failed:                 {summary['failed']}")
    elif summary["missing"]:
        print("  (dry run — re-run with --apply to rebuild these nodes)")
    else:
        print("  AGE graph is in sync with the SQL source of truth.")


if __name__ == "__main__":
    main()
