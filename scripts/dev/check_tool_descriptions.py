#!/usr/bin/env python3
"""Lint the advertised tool descriptions against the routing-line contract.

Implements the deterministic checker from docs/proposals/tool-surface-legibility-v0.md
section 1. Each advertised description's first line must be a routing line:

  * at most ROUTING_LINE_MAX characters,
  * starting with a recognised imperative verb (a closed list, so unknown openers are reported),
  * free of dated policy IDs (``2026-05-23``, ``S1-c``, ``Part C``), which belong
    in the ``describe_tool`` detail payload and docs/.

Report-only by default: the catalog predates the contract, so exit status stays 0
unless ``--strict`` is passed. Offline, no DB or network.

    python3 scripts/dev/check_tool_descriptions.py            # report
    python3 scripts/dev/check_tool_descriptions.py --strict   # exit 1 on any finding
    python3 scripts/dev/check_tool_descriptions.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROUTING_LINE_MAX = 140

# A routing line opens with an imperative verb. Python cannot test "is a verb", so
# the check is positive: the opener must be in this closed set. An unknown opener
# is reported as unrecognised rather than waved through; a legitimate new verb is
# added here, which is the review point for what a routing line may start with.
_ROUTING_VERBS = frozenset({
    "add", "append", "ask", "archive", "ask", "bind", "call", "check", "clear",
    "close", "compare", "create", "declare", "delegate", "describe", "detect",
    "disable", "discover", "end", "enumerate", "export", "fetch", "file", "find",
    "get", "grade", "hand", "inspect", "leave", "list", "load", "log", "look",
    "manage", "mark", "mint", "open", "pause", "query", "read", "record",
    "register", "report", "request", "resolve", "restore", "resume", "return",
    "review", "revise", "run", "save", "search", "send", "set", "show", "simulate", "start",
    "store", "submit", "summarize", "sync", "test", "update", "use", "verify",
    "view", "write",
})
_DATED_POLICY = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b|\bS\d+-[a-z]\b|\bPart [A-Z]\b")


def first_line(description: str) -> str:
    return description.strip().split("\n", 1)[0].strip()


def check_description(name: str, description: str) -> list[str]:
    """Return the contract violations for one advertised description."""
    problems: list[str] = []
    line = first_line(description)
    if len(line) > ROUTING_LINE_MAX:
        problems.append(f"first line is {len(line)} chars (max {ROUTING_LINE_MAX})")
    opener = re.split(r"[\s:,;]", line, maxsplit=1)[0].lower() if line else ""
    if opener not in _ROUTING_VERBS:
        problems.append(
            f"first line does not open with a recognised verb ({opener or 'empty'!r}); "
            "add a legitimate verb to _ROUTING_VERBS"
        )
    dated = _DATED_POLICY.findall(description)
    if dated:
        problems.append(f"dated policy id in advertised text: {sorted(set(dated))}")
    return problems


def advertised_descriptions() -> dict[str, str]:
    """Canonical tool descriptions plus the workflow aliases the wire advertises.

    Aliases (start_session, sync_state, request_review, ...) are not in
    ``get_tool_definitions()``; their advertised text is the alias
    ``migration_note`` built by ``build_alias_tool_definition``.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.interface_contract import build_alias_tool_definition, workflow_alias_names_for_mode
    from src.tool_schemas import get_tool_definitions

    definitions = list(get_tool_definitions())
    descriptions = {tool.name: tool.description or "" for tool in definitions}
    for alias in workflow_alias_names_for_mode("full"):
        tool = build_alias_tool_definition(alias, definitions=definitions)
        descriptions[tool.name] = tool.description or ""
    return descriptions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--strict", action="store_true", help="exit 1 on any finding")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    descriptions = advertised_descriptions()
    findings = {
        name: problems
        for name, desc in sorted(descriptions.items())
        if (problems := check_description(name, desc))
    }

    if args.json:
        print(json.dumps({"checked": len(descriptions), "findings": findings}, indent=2))
    else:
        for name, problems in findings.items():
            for problem in problems:
                print(f"{name}: {problem}")
        print(f"\n{len(findings)}/{len(descriptions)} advertised descriptions violate the contract")
    return 1 if (args.strict and findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
