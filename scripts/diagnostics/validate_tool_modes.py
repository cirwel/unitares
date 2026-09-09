#!/usr/bin/env python3
"""
Validate TOOL_MODE configuration against the advertised tool roster.

The roster (``tool_modes.advertised_tool_names_full``) is every register=True
dispatch tool plus the workflow aliases. It is what a full-mode server
advertises; the schema-definition list (``get_tool_definitions``) is its
registered half, and the registrar adds the aliases. Both derive from the one
record per tool in src/tool_meta.py.

Checks:
- TOOL_CATEGORIES partitions the roster exactly: every advertised name in one
  category, and nothing else in any category
- Every tool_stability alias resolves to a schema tool (a dangling alias is
  real drift)
- Full mode equals the roster
- Every legacy mode and category setting resolves to that same complete roster

tests/test_tool_registry_bookkeeping.py checks the same rule for the tier,
operation, stability, and catalog maps; this script is the CI smoke gate for
the mode-facing maps.

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

    schema_set = {t.name for t in get_tool_definitions()}
    advertised = tool_modes.advertised_tool_names_full()

    # An alias is a live, callable name that get_tool_definitions() does not
    # return, because it only reports the canonical targets. Treat an alias as
    # valid iff it resolves to a real schema tool; one pointing at nothing IS
    # real drift. Widened from the 8 workflow aliases to the whole table on
    # 2026-08-29: it checks 70 aliases for danglement instead of 8.
    alias_names = set(list_all_aliases())
    dangling_aliases = sorted(
        a for a in alias_names if resolve_tool_alias(a)[0] not in schema_set
    )

    categories_union: set[str] = set()
    for _, tools in tool_modes.TOOL_CATEGORIES.items():
        categories_union |= set(tools)

    # Categories are keyed by the advertised roster and nothing else. Until
    # 2026-09-07 legacy alias names were tolerated here and 23 advertised
    # tools sat in no category at all (a WARN nobody read).
    extra_in_categories = sorted(categories_union - advertised)
    uncategorized = sorted(advertised - categories_union)

    full_mode_set = tool_modes.get_tools_for_mode("full")
    full_missing = sorted(advertised - full_mode_set)
    full_extra = sorted(full_mode_set - advertised)

    # Legacy configuration cannot partition the public federation catalog.
    legacy_mismatches = {
        mode: tool_modes.get_tools_for_mode(mode) ^ advertised
        for mode in ("minimal", "standard", "lite", "operator_readonly", "operator_recovery", *tool_modes.TOOL_CATEGORIES)
        if tool_modes.get_tools_for_mode(mode) != advertised
    }

    ok = True

    if extra_in_categories:
        ok = False
        print("FAIL: TOOL_CATEGORIES names tools that are not on the advertised roster:")
        for n in extra_in_categories:
            print(f"  - {n}")

    if uncategorized:
        ok = False
        print("FAIL: advertised tool(s) in no TOOL_CATEGORIES category:")
        for n in uncategorized:
            print(f"  - {n}")

    if dangling_aliases:
        ok = False
        print("FAIL: tool_stability alias entries that do not resolve to a schema tool:")
        for n in dangling_aliases:
            print(f"  - {n}")

    if full_missing or full_extra:
        ok = False
        print("FAIL: TOOL_MODE=full does not equal the advertised roster.")
        if full_missing:
            print("  Missing from full:")
            for n in full_missing:
                print(f"    - {n}")
        if full_extra:
            print("  Extra in full (not advertised):")
            for n in full_extra:
                print(f"    - {n}")

    for mode, names in legacy_mismatches.items():
        ok = False
        print(f"FAIL: legacy mode {mode!r} differs from the complete catalog: {sorted(names)}")

    if not ok:
        return 1

    print(
        f"OK: tool_modes validated (advertised={len(advertised)}, "
        f"categorized={len(categories_union)}, schema_definitions={len(schema_set)}, "
        f"aliases={len(alias_names)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
