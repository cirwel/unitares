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

WHOSE PROBLEM IT IS. A hint only strands a caller who can receive it. Each site
is resolved to the `@mcp_tool` handler that emits it, and kept only when that
handler is reachable on the profile being checked -- the profile advertises it,
advertises the router it dispatches through, or advertises some other name
resolving to the same (router, action) pair. Handlers under `middleware/` run on
every dispatch and so always count. Without this the `standard` run blamed it
for `observe`, `agent` and `operator_resume_agent`, whose hints are emitted only
by operator tools and which ARE advertised on the operator profiles: advertised
where its callers are is not dormant. An unresolvable emitter counts as
reachable, because over-reporting a live hint is recoverable and missing one is
not.

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

#: A hint reads as a call: the tool name immediately followed by an open paren.
#: Bare mentions in prose ("the knowledge graph") are not instructions and are
#: not matched -- requiring the paren is what keeps this from flagging English.
#:
#: The paren must be ADJACENT. Allowing whitespace (as this did until
#: 2026-09-08) makes an ordinary English parenthetical parse as a call: the
#: sentence "this guard only blocks ACTING AS another agent (writes/mutations)"
#: in src/mcp_handlers/middleware/params_step.py was reported as a dead-end
#: hint naming `agent`. One false finding is one too many for an instrument
#: whose whole job is to say which hints strand a caller.
#:
#: The optional second group is an explicit ``action='...'`` argument, which
#: decides whether an advertised alias actually covers the hinted call: an
#: alias pins ONE action of its router, so `request_review` (action='request')
#: does not cover a hint that says `dialectic(action='get', ...)`.
CALL_PATTERN = re.compile(
    r"\b([a-z_][a-z0-9_]{2,})\(\s*(?:action\s*=\s*['\"]([a-z_]+)['\"])?"
)

#: Handlers under this package run on EVERY dispatch regardless of which tool
#: was called, so a hint emitted there reaches a caller on any profile.
MIDDLEWARE_MARKER = "/middleware/"

#: Dead ends that are known, understood, and deliberately left. The guard
#: (--fail-on-finding) fails on anything NOT listed here, so this can only
#: shrink: it is a ledger of accepted findings, not a mute button. Each entry
#: is (tool, "path:line") and must carry its reason.
#:
#: An entry that stops matching is also reported, so a fixed site cannot sit
#: here forever pretending to be outstanding.
#:
#: Scoped to the DEFAULT profile. `--fail-on-finding --mode <other>` will report
#: that profile's own unlisted findings, which is intended: a wider profile has
#: different reachability and its own ledger question, not this one's.
KNOWN_DEAD_ENDS: Dict[tuple, str] = {
    ("observe", "src/mcp_handlers/consolidated.py:121"):
        "observe's own identity refusal, naming observe. Only a caller who "
        "already invoked observe can receive it, so it strands nobody. The "
        "emitter is unresolvable here because consolidated.py builds its "
        "routers through a factory rather than @mcp_tool, so the call-graph "
        "hop bottoms out at a closure.",
    ("observe", "src/mcp_handlers/consolidated.py:126"):
        "Same refusal payload as :121 (its next_step half).",
    ("agent", "src/mcp_handlers/support/agent_auth.py:211"):
        "The archived-identity refusal offers the agent path first "
        "(start_session(resume=true), advertised) and names "
        "agent(action='update') explicitly as 'Operator restore:'. Naming "
        "another party's remedy is not a dead end for the caller.",
    ("get_governance_metrics", "src/mcp_handlers/core.py:218"):
        "OPEN, needs a decision rather than a rename. The example is "
        "get_governance_metrics(agent_id='<uuid>'); rewriting it to "
        "check_working_state(agent_id=...) would name a parameter that alias "
        "HIDES on the wire (_HIDE_IDENTITY_PARAMS_TOOLS), trading one dead end "
        "for another. Reading another agent's metrics is an observability "
        "operation and `standard` advertises no path to it.",
}

#: Emitter value recorded for a middleware hint. Not a tool name -- deliberately
#: unusable as one, so it cannot collide with the roster.
MIDDLEWARE_SENTINEL = "*middleware*"


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


def _decorated_tool_ranges(tree: ast.AST) -> List[tuple]:
    """(start, end, tool_name) for every ``@mcp_tool("name")`` handler in a file.

    Includes ``register=False`` delegates deliberately: a delegate is exactly
    what a router action dispatches to, so it is the unit that answers "can a
    caller on this profile see this response at all".
    """
    ranges = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            name = getattr(getattr(call, "func", None), "id", None) or getattr(
                getattr(call, "func", None), "attr", None
            )
            if name != "mcp_tool" or not call.args:
                continue
            first = call.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                ranges.append(
                    (node.lineno, getattr(node, "end_lineno", node.lineno), first.value)
                )
    return sorted(ranges)


def _emitting_tool(ranges: List[tuple], lineno: int) -> Optional[str]:
    """The tool whose handler body contains ``lineno``, innermost first."""
    best = None
    for start, end, tool in ranges:
        if start <= lineno <= end and (best is None or start > best[0]):
            best = (start, tool)
    return best[1] if best else None


#: How far to chase a helper back toward a decorated handler before giving up.
#: Three hops covers every current path (refusal helper -> router -> tool); the
#: cap exists so a cycle or a deep utility chain degrades to "unknown", which
#: reports the hint rather than hiding it.
_EMITTER_MAX_DEPTH = 3


def _function_spans(tree: ast.AST) -> List[tuple]:
    """(start, end, name) for every function in a file, decorated or not."""
    return sorted(
        (node.lineno, getattr(node, "end_lineno", node.lineno), node.name)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def _called_names(tree: ast.AST) -> Dict[str, Set[str]]:
    """Map each function to the plain names it calls.

    Attribute calls (``obj.method()``) are ignored: this only needs to chase
    module-local helpers back to the handler that emits through them, and a
    name is enough for that.
    """
    spans = _function_spans(tree)

    def enclosing(lineno: int) -> Optional[str]:
        best = None
        for start, end, name in spans:
            if start <= lineno <= end and (best is None or start > best[0]):
                best = (start, name)
        return best[1] if best else None

    out: Dict[str, Set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = getattr(node.func, "id", None)
        if not callee:
            continue
        caller = enclosing(node.lineno)
        if caller:
            out.setdefault(caller, set()).add(callee)
    return out


class _EmitterIndex:
    """Resolves "which tool's response carries this line" across the tree.

    A hint often sits in an undecorated helper -- a refusal builder, a shared
    payload -- and the tool that matters is whichever handler CALLS it. Without
    this hop every such hint resolved to "unknown" and was reported against
    every profile: `observe`'s own identity refusal, reachable only by someone
    who already called `observe`, was reported as stranding agents on
    `standard`.
    """

    def __init__(self) -> None:
        self._decorated: Dict[str, List[tuple]] = {}
        self._spans: Dict[str, List[tuple]] = {}
        self._callers: Dict[str, Set[tuple]] = {}
        self._middleware: Set[str] = set()

    def add(self, relative: str, tree: ast.AST) -> None:
        if MIDDLEWARE_MARKER in f"/{relative}":
            self._middleware.add(relative)
            return
        self._decorated[relative] = _decorated_tool_ranges(tree)
        self._spans[relative] = _function_spans(tree)
        for caller, callees in _called_names(tree).items():
            for callee in callees:
                self._callers.setdefault(callee, set()).add((relative, caller))

    def _enclosing_function(self, relative: str, lineno: int) -> Optional[str]:
        best = None
        for start, end, name in self._spans.get(relative, []):
            if start <= lineno <= end and (best is None or start > best[0]):
                best = (start, name)
        return best[1] if best else None

    def _tools_for_function(
        self, relative: str, func: Optional[str], depth: int, seen: Set[tuple]
    ) -> Optional[Set[str]]:
        if func is None or depth > _EMITTER_MAX_DEPTH or (relative, func) in seen:
            return None
        seen.add((relative, func))
        for start, end, tool in self._decorated.get(relative, []):
            span = self._enclosing_function(relative, start + 1)
            if span == func or tool == func:
                return {tool}
        tools: Set[str] = set()
        for caller_file, caller_func in self._callers.get(func, ()):  # one hop up
            found = self._tools_for_function(caller_file, caller_func, depth + 1, seen)
            if found is None:
                return None  # an unresolvable caller makes the whole answer unknown
            tools |= found
        return tools or None

    def emitters(self, relative: str, lineno: int) -> Optional[Set[str]]:
        """Tools whose responses can carry this line; None when undetermined."""
        if relative in self._middleware:
            return {MIDDLEWARE_SENTINEL}
        direct = _emitting_tool(self._decorated.get(relative, []), lineno)
        if direct:
            return {direct}
        func = self._enclosing_function(relative, lineno)
        return self._tools_for_function(relative, func, 0, set())


def _reachable_predicate(mode: str):
    """A test for "can a caller on ``mode`` receive a response from this tool".

    A tool is reachable when the profile advertises it, when the profile
    advertises the router it dispatches through (a router reaches every one of
    its actions), or when some advertised name resolves to the same
    (router, action) pair it does -- which is how `request_review` makes
    `request_dialectic_review`'s responses reachable without either name
    matching the other.
    """
    from src.mcp_handlers.tool_stability import list_all_aliases
    from src.tool_modes import get_tools_for_mode

    advertised = get_tools_for_mode(mode)
    aliases = list_all_aliases()

    def resolve(name):
        alias = aliases.get(name)
        if alias is None:
            return (name, None)
        return (alias.new_name, alias.inject_action)

    advertised_calls = set()
    for name in advertised:
        canonical, action = resolve(name)
        advertised_calls.add((canonical, action))
        if name == canonical:
            # An advertised router reaches every action it routes.
            advertised_calls.add((canonical, "*"))

    def one(tool: str) -> bool:
        if tool == MIDDLEWARE_SENTINEL:
            # Runs on every dispatch, so its hints reach every profile.
            return True
        if tool in advertised:
            return True
        canonical, action = resolve(tool)
        return (
            (canonical, action) in advertised_calls
            or (canonical, "*") in advertised_calls
        )

    def reachable(emitters) -> bool:
        if not emitters:
            # Undetermined (an unresolvable helper chain, or a module-level
            # string). Report it: over-reporting a live hint is recoverable,
            # missing one is not.
            return True
        return any(one(tool) for tool in emitters)

    return reachable


def collect_hint_sites(roster: Set[str]) -> Dict[str, Set[tuple]]:
    """Map each rostered tool name to (site, action, emitter) triples.

    ``action`` is the router action the hint stated, or None when it stated
    none. ``emitter`` is the tool whose handler emits the hint -- the thing
    that decides whether a caller on a given profile can receive it at all --
    or None when the hint sits outside any decorated handler.
    """
    sites: Dict[str, Set[tuple]] = {}

    # Two passes: the index must see every file's call graph before any hint is
    # resolved, because a helper's emitter is usually in another module.
    trees: Dict[str, ast.AST] = {}
    index = _EmitterIndex()
    for path in sorted(HANDLER_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        trees[relative] = tree
        index.add(relative, tree)

    for relative, tree in trees.items():
        for value in _hint_value_nodes(tree):
            for constant in _string_constants(value):
                for match in CALL_PATTERN.finditer(constant.value):
                    name = match.group(1)
                    if name in roster:
                        emitters = index.emitters(relative, constant.lineno)
                        sites.setdefault(name, set()).add(
                            (
                                f"{relative}:{constant.lineno}",
                                match.group(2),
                                frozenset(emitters) if emitters else None,
                            )
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

    reachable = _reachable_predicate(mode)

    findings = []
    for tool, triples in collect_hint_sites(roster).items():
        if tool in advertised:
            continue
        # Only sites a caller on THIS profile can actually receive. A hint
        # inside `handle_operator_resume_agent` is not `standard`'s problem:
        # an agent on `standard` cannot call that tool, so it never sees the
        # string. Judging every hint against one profile reported three
        # operator-path tools as stranding agents when they are advertised
        # exactly where their callers are.
        pairs = [(site, action) for site, action, emitter in triples if reachable(emitter)]
        if not pairs:
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
        help=(
            "Exit non-zero on any dead-end hint not in KNOWN_DEAD_ENDS, or on "
            "a ledger entry that no longer matches (for use as a CI guard)"
        ),
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

    if args.fail_on_finding:
        seen = {(f.tool, site) for f in findings for site in f.sites}
        unlisted = sorted(seen - set(KNOWN_DEAD_ENDS))
        stale = sorted(set(KNOWN_DEAD_ENDS) - seen)
        for tool, site in unlisted:
            print(
                f"NEW dead-end hint: {site} names {tool!r}, which {args.mode} "
                "does not advertise. Advertise it, route the hint through an "
                "advertised name, or add it to KNOWN_DEAD_ENDS with a reason.",
                file=sys.stderr,
            )
        for tool, site in stale:
            print(
                f"STALE ledger entry: {site} / {tool!r} is listed in "
                "KNOWN_DEAD_ENDS but no longer reported. Remove the entry.",
                file=sys.stderr,
            )
        if unlisted or stale:
            return EXIT_DEAD_END_HINT
    return 0


if __name__ == "__main__":
    sys.exit(main())
