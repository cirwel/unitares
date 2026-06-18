#!/usr/bin/env python3
"""
Tool Count Automation - Single Source of Truth

This script counts MCP tools from the runtime decorator registry. Static
source scans drifted once handlers moved into subpackages and consolidated
action routers, while the registry is exactly what dispatch uses.

Usage:
    python3 scripts/diagnostics/count_tools.py              # Display count
    python3 scripts/diagnostics/count_tools.py --json       # JSON output
    python3 scripts/diagnostics/count_tools.py --by-module  # Breakdown by module
"""

import json
import sys
from pathlib import Path
from typing import Dict

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _registry_accessors():
    # Importing src.mcp_handlers runs first-party handler imports, which
    # populates decorators._TOOL_DEFINITIONS.
    import src.mcp_handlers  # noqa: F401
    from src.mcp_handlers.decorators import get_tool_definition, list_registered_tools

    return get_tool_definition, list_registered_tools


def _module_bucket(module_name: str) -> str:
    prefix = "src.mcp_handlers."
    if module_name.startswith(prefix):
        module_name = module_name[len(prefix):]
    return module_name or "(unknown)"


def get_tool_breakdown(*, include_hidden: bool = False, include_deprecated: bool = True) -> Dict[str, int]:
    """Get tool count breakdown by module."""
    get_tool_definition, list_registered_tools = _registry_accessors()
    breakdown = {}
    for tool_name in list_registered_tools(
        include_hidden=include_hidden,
        include_deprecated=include_deprecated,
    ):
        td = get_tool_definition(tool_name)
        if td is None:
            continue
        module_name = _module_bucket(getattr(td.handler, "__module__", ""))
        breakdown[module_name] = breakdown.get(module_name, 0) + 1

    return breakdown


def get_total_count(*, include_hidden: bool = False, include_deprecated: bool = True) -> int:
    """Get total tool count."""
    return sum(
        get_tool_breakdown(
            include_hidden=include_hidden,
            include_deprecated=include_deprecated,
        ).values()
    )


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Count MCP tools")
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    parser.add_argument('--by-module', action='store_true', help='Show breakdown by module')
    parser.add_argument('--include-hidden', action='store_true', help='Include hidden/internal tools')
    parser.add_argument(
        '--exclude-deprecated',
        action='store_true',
        help='Exclude deprecated-but-callable tools',
    )
    args = parser.parse_args()

    include_deprecated = not args.exclude_deprecated
    total = get_total_count(
        include_hidden=args.include_hidden,
        include_deprecated=include_deprecated,
    )
    breakdown = get_tool_breakdown(
        include_hidden=args.include_hidden,
        include_deprecated=include_deprecated,
    )

    if args.json:
        output = {
            "total": total,
            "include_hidden": args.include_hidden,
            "include_deprecated": include_deprecated,
            "breakdown": breakdown,
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
