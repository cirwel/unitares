#!/usr/bin/env python3
"""Lint the advertised tool descriptions against the routing-line contract.

Implements the deterministic checker from docs/proposals/tool-surface-legibility-v0.md
section 1. Each advertised description's first line must be a routing line:

  * at most ROUTING_LINE_MAX characters,
  * starting with a verb-like word (not a bare noun phrase or a conjunction),
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

# A routing line opens with an imperative verb. Rather than a verb lexicon, reject
# the openers that mark a description written for the ontology: articles,
# conjunctions, and ``This``/``Use``-less framing.
_NON_VERB_OPENERS = frozenset({
    "a", "an", "the", "this", "these", "it", "its", "and", "or", "but", "if",
    "when", "while", "for", "to", "in", "on", "of", "with", "no", "not", "all",
    "canonical", "weak/fast", "weak",
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
    if not opener or opener in _NON_VERB_OPENERS:
        problems.append(f"first line does not open with a verb ({opener or 'empty'!r})")
    dated = _DATED_POLICY.findall(description)
    if dated:
        problems.append(f"dated policy id in advertised text: {sorted(set(dated))}")
    return problems


def advertised_descriptions() -> dict[str, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.tool_schemas import get_tool_definitions

    return {tool.name: tool.description or "" for tool in get_tool_definitions()}


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
