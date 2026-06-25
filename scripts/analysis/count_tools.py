"""
Count registered MCP tools for CI reporting.

Usage:
    python scripts/analysis/count_tools.py              # Print total count
    python scripts/analysis/count_tools.py --json       # Print as JSON
    python scripts/analysis/count_tools.py --by-module  # Breakdown by module
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _registry_accessors():
    import src.mcp_handlers  # noqa: F401 - populates decorator registry
    from src.mcp_handlers.decorators import get_tool_definition, list_registered_tools

    return get_tool_definition, list_registered_tools


def _module_bucket(module_name: str) -> str:
    prefix = "src.mcp_handlers."
    if module_name.startswith(prefix):
        module_name = module_name[len(prefix):]
    return module_name or "(unknown)"


def count_tools(*, include_hidden: bool = False, include_deprecated: bool = True) -> tuple[dict[str, list[str]], int]:
    """Count tools from the runtime registry that dispatch actually uses."""
    get_tool_definition, list_registered_tools = _registry_accessors()
    by_module: dict[str, list[str]] = defaultdict(list)

    for tool_name in list_registered_tools(
        include_hidden=include_hidden,
        include_deprecated=include_deprecated,
    ):
        td = get_tool_definition(tool_name)
        if td is None:
            continue
        module_name = _module_bucket(getattr(td.handler, "__module__", ""))
        by_module[module_name].append(tool_name)

    sorted_modules = {
        module_name: sorted(tool_names)
        for module_name, tool_names in sorted(by_module.items())
    }
    total = sum(len(tool_names) for tool_names in sorted_modules.values())
    return sorted_modules, total


def main():
    parser = argparse.ArgumentParser(description="Count MCP tools")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--by-module", action="store_true", help="Show breakdown by module")
    parser.add_argument("--include-hidden", action="store_true", help="Include hidden/internal tools")
    parser.add_argument(
        "--exclude-deprecated",
        action="store_true",
        help="Exclude deprecated-but-callable tools",
    )
    args = parser.parse_args()

    include_deprecated = not args.exclude_deprecated
    try:
        by_module, total = count_tools(
            include_hidden=args.include_hidden,
            include_deprecated=include_deprecated,
        )
    except ModuleNotFoundError as exc:
        print(f"WARNING: Tool count unavailable ({exc})", file=sys.stderr)
        by_module = {}
        total = 0

    if args.json:
        output = {
            "total": total,
            "include_hidden": args.include_hidden,
            "include_deprecated": include_deprecated,
            "by_module": by_module,
        }
        print(json.dumps(output, indent=2))
    elif args.by_module:
        for module, tools in by_module.items():
            print(f"\n{module} ({len(tools)} tools):")
            for tool_name in tools:
                print(f"  - {tool_name}")
        print(f"\nTotal: {total} tools")
    else:
        print(f"{total} tools")


if __name__ == "__main__":
    main()
