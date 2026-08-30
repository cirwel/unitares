#!/usr/bin/env python3
"""
Tool Count Automation - Single Source of Truth

This script counts MCP tools from the runtime decorator registry. Static
source scans drifted once handlers moved into subpackages and consolidated
action routers, while the registry is exactly what dispatch uses.

Counting requires the runtime dependency tree, because the registry is
populated by importing the handler package. Where those dependencies are
absent the count is *unavailable* — which is a different fact from "this repo
has zero tools", and every output mode here keeps the two distinguishable:
`--json` carries an explicit `available` flag, and the plain and `--by-module`
modes say "unavailable" rather than printing a number. Callers must render
them differently. A sentinel published as a measurement is instrumentation
failing toward "healthy" instead of toward "unknown", which is exactly what
this module exists to prevent.

Usage:
    python3 scripts/diagnostics/count_tools.py              # Display count
    python3 scripts/diagnostics/count_tools.py --json       # JSON output
    python3 scripts/diagnostics/count_tools.py --by-module  # Breakdown by module
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

#: Printed on stdout in plain mode when the registry could not be read. Chosen
#: to be non-numeric so a caller that captures stdout and interpolates it into
#: a report cannot render it as an inventory.
UNAVAILABLE_SENTINEL = "unavailable"

#: Exit status for `--require-registry` when the registry is unavailable.
EXIT_REGISTRY_UNAVAILABLE = 2


@dataclass(frozen=True)
class ToolCount:
    """A tool count, or an explicit statement that counting was impossible.

    `available=False` means the count could not be taken here; `total` is then
    None rather than 0, so no caller can read the absence as an inventory.
    """

    available: bool
    total: Optional[int] = None
    breakdown: Dict[str, int] = field(default_factory=dict)
    reason: Optional[str] = None


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


def resolve_tool_count(
    *, include_hidden: bool = False, include_deprecated: bool = True
) -> ToolCount:
    """Count tools, reporting unavailability as a state rather than a zero.

    Counting imports the full handler tree. In dependency-less environments
    (the doc-validation CI runner) that import fails; this returns
    `available=False` with the reason instead of crashing the step, and
    crucially without inventing a number for the caller to publish.
    """
    try:
        breakdown = get_tool_breakdown(
            include_hidden=include_hidden,
            include_deprecated=include_deprecated,
        )
    except ModuleNotFoundError as exc:
        return ToolCount(available=False, reason=str(exc))

    return ToolCount(available=True, total=sum(breakdown.values()), breakdown=breakdown)


def main() -> int:
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
    parser.add_argument(
        '--require-registry',
        action='store_true',
        help=(
            'Exit non-zero when the runtime registry is unavailable. Use where '
            'dependencies ARE installed, so an unavailable count is a real '
            'breakage rather than an accepted degradation.'
        ),
    )
    args = parser.parse_args()

    include_deprecated = not args.exclude_deprecated
    result = resolve_tool_count(
        include_hidden=args.include_hidden,
        include_deprecated=include_deprecated,
    )

    if not result.available:
        print(f"WARNING: Tool count unavailable ({result.reason})", file=sys.stderr)

    if args.json:
        output = {
            "available": result.available,
            "total": result.total,
            "reason": result.reason,
            "include_hidden": args.include_hidden,
            "include_deprecated": include_deprecated,
            "breakdown": result.breakdown,
        }
        print(json.dumps(output, indent=2))
    elif args.by_module:
        if not result.available:
            print(f"Tool count unavailable ({result.reason})")
        else:
            print("Tool count by module:")
            for module, count in sorted(result.breakdown.items()):
                print(f"  {module:20} {count:2} tools")
            print(f"  {'─' * 30}")
            print(f"  {'Total:':20} {result.total:2} tools")
    else:
        print(result.total if result.available else UNAVAILABLE_SENTINEL)

    if not result.available and args.require_registry:
        return EXIT_REGISTRY_UNAVAILABLE
    return 0


if __name__ == "__main__":
    sys.exit(main())
