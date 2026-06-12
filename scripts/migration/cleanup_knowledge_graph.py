#!/usr/bin/env python3
"""
Manual Knowledge Graph Cleanup Script

Run this to clean up old discoveries and keep the graph performant.

Usage:
    # Dry run (see what would be cleaned up)
    python3 scripts/cleanup_knowledge_graph.py --dry-run

    # Actually clean up
    python3 scripts/cleanup_knowledge_graph.py

    # Get stats only
    python3 scripts/cleanup_knowledge_graph.py --stats
"""

import sys
import asyncio
import argparse
import json
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.knowledge_graph import get_knowledge_graph
from src.knowledge_graph_lifecycle import KnowledgeGraphLifecycle


async def main():
    parser = argparse.ArgumentParser(description="Clean up knowledge graph")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be cleaned without making changes")
    parser.add_argument("--stats", action="store_true", help="Show lifecycle statistics only")
    args = parser.parse_args()

    # Get knowledge graph
    graph = await get_knowledge_graph()
    archive_dir = project_root / "data" / "knowledge_graph_archives"

    # Create lifecycle manager
    lifecycle = KnowledgeGraphLifecycle(graph, archive_dir)

    if args.stats:
        # Just show stats
        stats = await lifecycle.get_lifecycle_stats()
        print("\n=== Knowledge Graph Lifecycle Statistics ===\n")
        print(json.dumps(stats, indent=2))
        return

    # Run cleanup
    print(f"\n=== Knowledge Graph Cleanup {'(DRY RUN)' if args.dry_run else ''} ===\n")

    summary = await lifecycle.run_cleanup(dry_run=args.dry_run)

    print(json.dumps(summary, indent=2))

    if args.dry_run:
        print("\n💡 Run without --dry-run to actually perform cleanup")
    else:
        print("\n✅ Cleanup complete!")
        print(f"   Archived: {summary['discoveries_archived']} discoveries")
        print(f"   Exported: {summary['discoveries_exported']} discoveries")
        print(f"   Deleted: {summary['discoveries_deleted']} discoveries")


if __name__ == "__main__":
    asyncio.run(main())
