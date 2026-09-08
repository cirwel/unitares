#!/usr/bin/env python3
"""Tools named in caller-facing hints that the active profile does not advertise.

A response that says "poll `dialectic(action='get', ...)`" is an instruction.
A schema-driven MCP client (Claude Code, Codex, Cursor) can only call names
`tools/list` returned, so when the named tool is not advertised the instruction
is a dead end -- the server told the agent to do something the agent has no
way to do. `src/tool_modes.py` is right that an unadvertised name still
DISPATCHES; that is a property no schema-driven client can use.

`tests/test_lite_wire_surface.py` already holds this invariant, but against two
hand-written lists (`call_model`'s inference hints, and the pause/auth-refusal
`self_recovery` hint). A hand-written list catches what somebody remembered.
This script derives the set instead, by reading the handler tree.

VERIFIED THE HARD WAY, 2026-09-08: a Claude Code session on the default
`standard` profile called `request_review`, and the response told it to poll
`dialectic(action='get', session_id=...)`. `dialectic` is registered and
callable on every transport, and the session could not call it -- the name was
never advertised, so it was never offered to the model. This script exists
because that is not a one-off.

WHAT COUNTS AS A HINT. Only string literals that reach a caller: values under
the response keys in `HINT_KEYS`, whether written as dict entries, keyword
arguments, or assignments. Docstrings, comments and the internal action tables
in `consolidated.py` are deliberately NOT scanned -- an unfiltered scan reports
19 unadvertised names in `standard` where the caller-facing count is 10, and a
number that overstates the problem is not usable evidence.

TWO CLASSES OF FINDING, and they need different fixes:

  1. The hint names the RAW IMPLEMENTATION of an advertised alias --
     `onboard` for `start_session`, `process_agent_update` for `sync_state`,
     `get_governance_metrics` for `check_working_state`. The capability is
     advertised; the hint just says a name the client was not shown. Fix the
     hint text. Costs nothing on the wire.
  2. The hint names a capability the profile genuinely does not advertise --
     `dialectic`, `observe`, `agent`, `bind_session`. Either advertise it, or
     route the hint through something that is advertised.

`--classify` splits the output that way.

Usage:
    python3 scripts/diagnostics/hint_target_advertisement.py
    python3 scripts/diagnostics/hint_target_advertisement.py --mode lite
    python3 scripts/diagnostics/hint_target_advertisement.py --classify
    python3 scripts/diagnostics/hint_target_advertisement.py --json
"""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

HANDLER_ROOT = PROJECT_ROOT / "src" / "mcp_handlers"

#: Exit status when the handler tree could not be read.
EXIT_REGISTRY_UNAVAILABLE = 2

#: Exit status for `--fail-on-finding` when a dead-end hint exists.
EXIT_DEAD_END_HINT = 3

#: Response keys whose string values are serialized into an envelope the caller
#: reads. Add a key here when a new response field starts carrying prose that
#: names tools; leaving one out under-reports, which is the safer direction but
#: still a gap.
HINT_KEYS: Set[str] = {
    "action_required", "all_inline", "fix", "guidance", "hint", "hints",
    "how_to_strengthen", "next_action", "next_step", "next_steps", "open_one",
    "raw_governance_hint", "recovery", "recovery_hint", "related_tools",
    "remediation", "resolution", "suggestion", "suggestions", "whose_move",
}

#: A hint reads as a call: the tool name followed by an open paren. Bare
#: mentions in prose ("the knowledge graph") are not instructions and are not
#: matched -- requiring the paren is what keeps this from flagging English.
#: The optional second group is an explicit ``action='...'`` argument, which
#: decides whether an advertised alias actually covers the hinted call: an
#: alias pins ONE action of its router, so `request_review` (action='request')
#: does not cover a hint that says `dialectic(action='get', ...)`.
CALL_PATTERN = re.compile(
    r"\b([a-z_][a-z0-9_]{2,})\s*\(\s*(?:action\s*=\s*['\"]([a-z_]+)['\"])?"
)


@dataclass(frozen=True)
class DeadEndHint:
    """A tool named in a caller-facing hint but absent from the profile."""

    tool: str
    sites: List[str] = field(default_factory=list)
    #: Router actions the hints ask for, when stated. Empty when no hint named
    #: one (a flat tool, or a router call written without ``action=``).
    actions: List[str] = field(default_factory=list)
    #: The advertised workflow alias that dispatches to this tool AND pins the
    #: action the hints ask for -- class 1 above, fixable by editing the hint
    #: text. None when no advertised alias covers the hinted call.
    advertised_alias: Optional[str] = None

    @property
    def kind(self) -> str:
        return "names_unadvertised_twin" if self.advertised_alias else "unadvertised_capability"


def _hint_value_nodes(tree: ast.AST) -> Iterator[ast.AST]:
    """Value nodes that land under a caller-facing response key."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in HINT_KEYS:
                    yield value
        elif isinstance(node, ast.keyword) and node.arg in HINT_KEYS:
            yield node.value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                name = getattr(target, "attr", None) or getattr(target, "id", None)
                if name in HINT_KEYS:
                    yield node.value
                elif (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value in HINT_KEYS
                ):
                    yield node.value


def _string_constants(node: ast.AST) -> Iterator[ast.Constant]:
    """Every string constant reachable from a value node.

    Walks into f-strings, lists and nested dicts, because hints are written all
    three ways.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            yield sub


def collect_hint_sites(roster: Set[str]) -> Dict[str, Set[tuple]]:
    """Map each rostered tool name to (site, action) pairs instructing a call.

    ``action`` is the router action the hint stated, or None when it stated
    none.
    """
    sites: Dict[str, Set[tuple]] = {}
    for path in sorted(HANDLER_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        relative = path.relative_to(PROJECT_ROOT)
        for value in _hint_value_nodes(tree):
            for constant in _string_constants(value):
                for match in CALL_PATTERN.finditer(constant.value):
                    name = match.group(1)
                    if name in roster:
                        sites.setdefault(name, set()).add(
                            (f"{relative}:{constant.lineno}", match.group(2))
                        )
    return sites


def find_dead_end_hints(mode: str) -> List[DeadEndHint]:
    """Hints in `mode` that name a tool `mode` does not advertise."""
    import src.mcp_handlers  # noqa: F401  -- settles every @mcp_tool decorator

    from src.mcp_handlers.decorators import get_tool_registry
    from src.mcp_handlers.tool_stability import (
        AGENT_WORKFLOW_ALIASES,
        resolve_tool_alias,
    )
    from src.tool_modes import get_tools_for_mode

    roster = set(get_tool_registry()) | set(AGENT_WORKFLOW_ALIASES)
    advertised = get_tools_for_mode(mode)

    # Which advertised alias, if any, covers a call to (implementation, action).
    # An alias pins at most one action of its router, so the action is part of
    # the key: `request_review` covers dialectic(action='request') and nothing
    # else on that router.
    alias_for_call: Dict[tuple, str] = {}
    for alias in AGENT_WORKFLOW_ALIASES:
        if alias not in advertised:
            continue
        impl, info = resolve_tool_alias(alias)
        alias_for_call.setdefault(
            (impl, info.inject_action if info else None), alias
        )

    findings = []
    for tool, pairs in collect_hint_sites(roster).items():
        if tool in advertised:
            continue
        sites = sorted(site for site, _ in pairs)
        actions = sorted({action for _, action in pairs if action})
        # Only a text fix when EVERY hinted call is covered by an advertised
        # alias. One uncovered action makes the whole finding a real dead end.
        covering = {
            alias_for_call.get((tool, action)) for _, action in pairs
        }
        alias = (
            next(iter(covering))
            if len(covering) == 1 and None not in covering
            else None
        )
        findings.append(
            DeadEndHint(
                tool=tool, sites=sites, actions=actions, advertised_alias=alias
            )
        )
    return sorted(findings, key=lambda f: (-len(f.sites), f.tool))


def _render(findings: List[DeadEndHint], mode: str, classify: bool) -> None:
    if not findings:
        print(f"{mode}: no caller-facing hint names an unadvertised tool.")
        return

    total_sites = sum(len(f.sites) for f in findings)
    print(
        f"{mode}: {len(findings)} tools named in caller-facing hints are NOT "
        f"advertised ({total_sites} sites)."
    )
    groups = (
        [
            ("names the unadvertised raw twin of an advertised alias "
             "(fix the hint text)",
             [f for f in findings if f.advertised_alias]),
            ("capability not advertised on this profile "
             "(advertise it, or route the hint through one that is)",
             [f for f in findings if not f.advertised_alias]),
        ]
        if classify
        else [("", findings)]
    )
    for heading, group in groups:
        if not group:
            continue
        if heading:
            print()
            print(f"-- {heading}")
        for finding in group:
            suffix = (
                f"  [advertised as {finding.advertised_alias}]"
                if finding.advertised_alias
                else ""
            )
            actions = (
                f"  actions: {', '.join(finding.actions)}"
                if finding.actions
                else ""
            )
            print(f"  {finding.tool} ({len(finding.sites)} sites){suffix}{actions}")
            for site in finding.sites[:3]:
                print(f"      {site}")
            if len(finding.sites) > 3:
                print(f"      ... +{len(finding.sites) - 3} more")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Find tools named in caller-facing hints that a profile does not "
            "advertise"
        )
    )
    parser.add_argument(
        "--mode",
        default="standard",
        help="GOVERNANCE_TOOL_MODE profile to check (default: standard)",
    )
    parser.add_argument(
        "--classify",
        action="store_true",
        help="Split findings by whether an advertised alias already covers them",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--fail-on-finding",
        action="store_true",
        help="Exit non-zero when any dead-end hint exists (for use as a guard)",
    )
    args = parser.parse_args()

    try:
        findings = find_dead_end_hints(args.mode)
    except ModuleNotFoundError as exc:
        print(
            f"WARNING: hint scan unavailable ({exc}); the handler tree could "
            "not be imported, so this is 'unknown', not 'clean'",
            file=sys.stderr,
        )
        return EXIT_REGISTRY_UNAVAILABLE

    if args.json:
        print(
            json.dumps(
                {
                    "mode": args.mode,
                    "findings": [
                        {
                            "tool": f.tool,
                            "kind": f.kind,
                            "advertised_alias": f.advertised_alias,
                            "actions": f.actions,
                            "site_count": len(f.sites),
                            "sites": f.sites,
                        }
                        for f in findings
                    ],
                },
                indent=2,
            )
        )
    else:
        _render(findings, args.mode, args.classify)

    if findings and args.fail_on_finding:
        return EXIT_DEAD_END_HINT
    return 0


if __name__ == "__main__":
    sys.exit(main())
