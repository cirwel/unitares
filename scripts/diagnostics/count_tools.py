#!/usr/bin/env python3
"""
Tool Count Automation - Single Source of Truth

This script counts MCP tools by scanning @mcp_tool decorators.
Use this as the authoritative tool count instead of hardcoding numbers.

Usage:
    python3 scripts/diagnostics/count_tools.py              # Display count
    python3 scripts/diagnostics/count_tools.py --json       # JSON output
    python3 scripts/diagnostics/count_tools.py --by-module  # Breakdown by module
"""

import re
import json
from pathlib import Path
from typing import Dict

PROJECT_ROOT = Path(__file__).parent.parent.parent


def count_tools_in_file(filepath: Path) -> int:
    """Count @mcp_tool decorators in a file."""
    if not filepath.exists():
        return 0

    with open(filepath) as f:
        content = f.read()

    # Match @mcp_tool( decorators
    return len(re.findall(r'@mcp_tool\(', content))


def get_tool_breakdown() -> Dict[str, int]:
    """Get tool count breakdown by module."""
    handler_dir = PROJECT_ROOT / "src" / "mcp_handlers"

    # Exclude these files (not real handlers)
    EXCLUDE = {"__init__", "decorators", "utils", "validators"}

    breakdown = {}
    for filepath in handler_dir.glob("*.py"):
        if filepath.name.startswith("__"):
            continue

        module_name = filepath.stem
        if module_name in EXCLUDE:
            continue

        count = count_tools_in_file(filepath)
        if count > 0:
            breakdown[module_name] = count

    return breakdown


def get_total_count() -> int:
    """Get total tool count."""
    return sum(get_tool_breakdown().values())


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Count MCP tools")
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    parser.add_argument('--by-module', action='store_true', help='Show breakdown by module')
    args = parser.parse_args()

    total = get_total_count()
    breakdown = get_tool_breakdown()

    if args.json:
        output = {
            "total": total,
            "breakdown": breakdown
        }
        print(json.dumps(output, indent=2))
    elif args.by_module:
        print(f"Tool count by module:")
        for module, count in sorted(breakdown.items()):
            print(f"  {module:20} {count:2} tools")
        print(f"  {'─' * 30}")
        print(f"  {'Total:':20} {total:2} tools")
    else:
        print(total)


if __name__ == "__main__":
    main()
