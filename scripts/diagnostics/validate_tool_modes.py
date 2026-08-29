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
    from src.mcp_handlers.tool_stability import (
        list_all_aliases,
        resolve_tool_alias,
    )

    schema_tools = sorted({t.name for t in get_tool_definitions()})
    schema_set = set(schema_tools)

    # An alias is a live, callable tool name that get_tool_definitions() does not
    # return, because it only reports the canonical targets. Such a name
    # legitimately appears in TOOL_CATEGORIES. Treat an alias as valid iff it
    # resolves to a real schema tool; an alias pointing at nothing IS real drift.
    #
    # Scoped to AGENT_WORKFLOW_ALIASES (8 names) until 2026-08-29, which happened
    # to be sufficient only because every consolidated alias was ALSO a
    # registered schema tool and so passed via schema_set. Once those duplicate
    # registrations were retired -- the alias is now the only home for names like
    # submit_thesis and get_server_info -- the narrow scope reported nine
    # perfectly callable names as category drift. Widening to the whole table is
    # a strengthening, not a loosening: it checks 70 aliases for danglement
    # instead of 8, and a dangling alias in either set still fails.
    alias_names = set(list_all_aliases())
    valid_aliases = {a for a in alias_names if resolve_tool_alias(a)[0] in schema_set}
    dangling_aliases = sorted(alias_names - valid_aliases)

    categories_union = set()
    for _, tools in tool_modes.TOOL_CATEGORIES.items():
        categories_union |= set(tools)

    # Category drift: names in categories that are neither a schema tool nor a
    # valid alias of one.
    extra_in_categories = sorted(categories_union - schema_set - valid_aliases)

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
        print("FAIL: TOOL_CATEGORIES contains tools that are neither a schema tool nor a valid alias:")
        for n in extra_in_categories:
            print(f"  - {n}")

    if dangling_aliases:
        ok = False
        print("FAIL: tool_stability alias entries that do not resolve to a schema tool:")
        for n in dangling_aliases:
            print(f"  - {n}")

    if uncategorized:
        # Not fatal: uncategorized tools are still reachable via full mode (the
        # default); they only fall out of minimal/lite, which is often deliberate.
        print(
            f"WARN: {len(uncategorized)} schema tool(s) not in any TOOL_CATEGORIES "
            f"(absent from minimal/lite only): {', '.join(uncategorized)}"
        )

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


