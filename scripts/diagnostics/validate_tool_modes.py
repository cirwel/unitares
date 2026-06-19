#!/usr/bin/env python3
"""
Validate TOOL_MODE configuration against the canonical tool schema.

Checks:
- TOOL_CATEGORIES contains only tools that exist in the schema
- Every schema tool is either categorized OR at least reachable via TOOL_MODE=full
- Full mode includes all schema tools (source of truth)
- Minimal/Lite contain required discovery tools (list_tools, describe_tool)

Run:
  python3 scripts/diagnostics/validate_tool_modes.py
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo_root))

    from src.tool_schemas import get_tool_definitions
    from src import tool_modes

    schema_tools = sorted({t.name for t in get_tool_definitions()})
    schema_set = set(schema_tools)

    categories_union = set()
    for _, tools in tool_modes.TOOL_CATEGORIES.items():
        categories_union |= set(tools)

    # Category drift: names in categories but not in schema
    extra_in_categories = sorted(categories_union - schema_set)

    # Uncategorized tools (should be 0 for ergonomics, but correctness is via full mode)
    uncategorized = sorted(schema_set - categories_union)

    # Full mode should match schema
    full_mode_set = tool_modes.get_tools_for_mode("full")
    full_missing = sorted(schema_set - full_mode_set)
    full_extra = sorted(full_mode_set - schema_set)

    # Minimal/lite should include discovery tools
    required_discovery = {"list_tools", "describe_tool"}
    minimal_set = tool_modes.get_tools_for_mode("minimal")
    lite_set = tool_modes.get_tools_for_mode("lite")
    minimal_missing = sorted(required_discovery - minimal_set)
    lite_missing = sorted(required_discovery - lite_set)

    ok = True

    if extra_in_categories:
        ok = False
        print("FAIL: TOOL_CATEGORIES contains tools not present in schema:")
        for n in extra_in_categories:
            print(f"  - {n}")

    if uncategorized:
        # Not fatal for correctness, but a UX problem.
        print("WARN: schema tools not present in any TOOL_CATEGORIES (categorization gap):")
        for n in uncategorized:
            print(f"  - {n}")

    if full_missing or full_extra:
        ok = False
        print("FAIL: TOOL_MODE=full does not match schema tool list.")
        if full_missing:
            print("  Missing from full:")
            for n in full_missing:
                print(f"    - {n}")
        if full_extra:
            print("  Extra in full (not in schema):")
            for n in full_extra:
                print(f"    - {n}")

    if minimal_missing:
        ok = False
        print("FAIL: minimal mode missing discovery tools:")
        for n in minimal_missing:
            print(f"  - {n}")

    if lite_missing:
        ok = False
        print("FAIL: lite mode missing discovery tools:")
        for n in lite_missing:
            print(f"  - {n}")

    if ok:
        print(f"OK: tool_modes validated (schema_tools={len(schema_tools)}, categorized={len(categories_union)})")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())


