#!/usr/bin/env python3
"""Static candidates for hints naming tools absent from a profile.

HINT_KEYS is a heuristic seed list, not proof that a value reaches a caller.
The scan follows literals, nested containers, local value bindings and local
return builders under those keys. Bare names count only in structured
related_tools lists. Other prose needs an adjacent tool( call shape. Dynamic
strings, arbitrary data flow, argument validation and path conditions are not
proven. Comments and docstrings are not seed values.

Emitter resolution conservatively follows plain, imported and attribute calls
up to three hops. Same-name functions can be conflated; unresolved paths and
middleware count as possibly reachable. A profile can suppress a site only
when all resolved emitters are outside it. This is an advertisement inventory,
not observed failure incidence, a severity score, or proof of runtime delivery.
An operator remedy in an agent refusal is not necessarily the caller's action.

Alias coverage is per hinted name/action. Two actions can be covered by two
aliases; one uncovered action must stay visible. Even complete name/action
coverage does not justify a blind text replacement: alias schemas can hide
parameters (check_working_state hides agent_id), and response contracts differ.

The four reviewed candidates inherited from #2119 remain in KNOWN_DEAD_ENDS.
The broader scan also reports previously unseen candidates. --fail-on-finding
therefore currently exits 3 on standard: those sites have not been accepted or
fixed. Do not broaden the surface or rubber-stamp a baseline to make it green.

Usage:
    python3 scripts/diagnostics/hint_target_advertisement.py --classify
    python3 scripts/diagnostics/hint_target_advertisement.py --mode lite --json
    python3 scripts/diagnostics/hint_target_advertisement.py --fail-on-finding
"""

from __future__ import annotations

import ast
import io
import json
import re
import sys
import tokenize
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
#: names tools; leaving one out under-reports. Inclusion is a candidate, not proof
#: of serialization into a response.
HINT_KEYS: Set[str] = {
    "action_required", "all_inline", "fix", "guidance", "hint", "hints",
    "how_to_strengthen", "next_action", "next_step", "next_steps", "open_one",
    "raw_governance_hint", "recovery", "recovery_hint", "related_tools",
    "remediation", "resolution", "suggestion", "suggestions", "whose_move",
    "next_call", "recommended_action", "call", "safe_options", "note",
    "recommendation", "tip", "what_you_can_do", "workflow", "message",
    "error", "how_to", "instructions",
}

#: A hint reads as a call: the tool name immediately followed by an open paren.
#: Bare mentions in prose ("the knowledge graph") are not instructions and are
#: not matched -- requiring the paren is what keeps this from flagging English.
#:
#: The paren must be ADJACENT. Allowing whitespace (as this did until
#: 2026-09-08) makes an ordinary English parenthetical parse as a call: the
#: sentence "this guard only blocks ACTING AS another agent (writes/mutations)"
#: in src/mcp_handlers/middleware/params_step.py was reported as a dead-end
#: hint naming `agent`. The English plural session(s) is excluded too. These heuristics
#: reduce noise; they do not certify all remaining matches as instructions.
#:
#: The optional second group is an explicit ``action='...'`` argument, which
#: decides whether an advertised alias actually covers the hinted call: an
#: alias pins ONE action of its router, so `request_review` (action='request')
#: does not cover a hint that says `dialectic(action='get', ...)`.
CALL_PATTERN = re.compile(
    r"\b([a-z_][a-z0-9_]{2,})\((?!s\))\s*(?:action\s*=\s*['\"]([a-z_]+)['\"])?"
)


def _hinted_action(text: str, match: re.Match) -> Optional[str]:
    if match.group(2):
        # This prefix stays readable even in a partial f-string literal.
        return match.group(2)
    # Keyword order is irrelevant, but quoted prose such as
    # query="action='store'" is not an action argument. Parse just this call,
    # respecting nesting and quoted parentheses. Pseudocode that cannot be
    # parsed has unknown action, never a guessed covering alias.
    tokens = []
    depth = 0
    try:
        for token in tokenize.generate_tokens(io.StringIO(text[match.start():]).readline):
            tokens.append((token.type, token.string))
            if token.type == tokenize.OP:
                if token.string in "([{":
                    depth += 1
                elif token.string in ")]}":
                    depth -= 1
                    if depth == 0:
                        break
        call = ast.parse(tokenize.untokenize(tokens), mode="eval").body
    except (SyntaxError, tokenize.TokenError, ValueError):
        return None
    if isinstance(call, ast.Call):
        for keyword in call.keywords:
            if keyword.arg == "action" and isinstance(keyword.value, ast.Constant):
                return keyword.value.value if isinstance(keyword.value.value, str) else None
    return None

#: Handlers under this package run on EVERY dispatch regardless of which tool
#: was called, so a hint emitted there reaches a caller on any profile.
MIDDLEWARE_MARKER = "/middleware/"

#: Dead ends that are known, understood, and deliberately left. The guard
#: (--fail-on-finding) fails on anything NOT listed here, so newly discovered
#: sites remain visible until separately reviewed. Do not auto-baseline them. Each entry
#: is (tool, "path:line", action) and must carry its reason. An extra action at
#: the same line is not covered by an accepted entry for a different action.
#:
#: An entry that stops matching is also reported, so a fixed site cannot sit
#: here forever pretending to be outstanding.
#:
#: Scoped to the DEFAULT profile. `--fail-on-finding --mode <other>` will report
#: that profile's own unlisted findings, which is intended: a wider profile has
#: different reachability and its own ledger question, not this one's.
KNOWN_DEAD_ENDS: Dict[tuple, str] = {
    ("observe", "src/mcp_handlers/consolidated.py:121", None):
        "observe's own identity refusal, naming observe. Only a caller who "
        "already invoked observe can receive it, so it strands nobody. The "
        "emitter is unresolvable here because consolidated.py builds its "
        "routers through a factory rather than @mcp_tool, so the call-graph "
        "hop bottoms out at a closure.",
    ("observe", "src/mcp_handlers/consolidated.py:126", None):
        "Same refusal payload as :121 (its next_step half).",
    ("agent", "src/mcp_handlers/support/agent_auth.py:211", "update"):
        "The archived-identity refusal offers the agent path first "
        "(start_session(resume=true), advertised) and names "
        "agent(action='update') explicitly as 'Operator restore:'. Naming "
        "another party's remedy is not a dead end for the caller.",
    ("get_governance_metrics", "src/mcp_handlers/core.py:218", None):
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
    """A static hint candidate naming a tool absent from the profile."""

    tool: str
    sites: List[str] = field(default_factory=list)
    #: Router actions the hints ask for, when stated. Empty when no hint named
    #: one (a flat tool, or a router call written without ``action=``).
    actions: List[str] = field(default_factory=list)
    #: The advertised workflow alias that dispatches to this tool AND pins the
    #: action the hints ask for -- class 1 above, fixable by editing the hint
    #: text. None when no advertised alias covers the hinted call.
    advertised_alias: Optional[str] = None
    advertised_aliases: List[str] = field(default_factory=list)
    calls: List[dict] = field(default_factory=list)

    @property
    def kind(self) -> str:
        if self.calls and all(call["advertised_alias"] for call in self.calls):
            return "names_unadvertised_twin"
        if self.advertised_aliases:
            return "partially_covered_actions"
        return "no_mapped_advertised_name"


def _hint_value_nodes(tree: ast.AST, keys=HINT_KEYS) -> Iterator[ast.AST]:
    """Value nodes that land under a caller-facing response key."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in keys:
                    yield value
        elif isinstance(node, ast.keyword) and node.arg in keys:
            yield node.value
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                name = getattr(target, "attr", None) or getattr(target, "id", None)
                if name in keys:
                    yield node.value
                elif (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value in keys
                ):
                    yield node.value


def _string_constants(node: ast.AST, bindings=None, seen=None) -> Iterator[ast.Constant]:
    """Every string constant reachable from a value node.

    Walks into f-strings, lists and nested dicts, because hints are written all
    three ways.
    """
    seen = set() if seen is None else seen
    if id(node) in seen:
        return
    seen.add(id(node))
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node
    if bindings:
        name = node.id if isinstance(node, ast.Name) else (
            node.func.id if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) else None
        )
        for value in bindings.get(name, ()):
            yield from _string_constants(value, bindings, seen)
    for child in ast.iter_child_nodes(node):
        yield from _string_constants(child, bindings, seen)


def _value_bindings(tree: ast.AST) -> Dict[str, List[ast.AST]]:
    """Conservative file-local sources for named hint values and builders.

    Multiple assignments are unioned, not treated as proof of a single live
    path. Dynamic values and imports remain outside this static inventory.
    """
    bindings: Dict[str, List[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings.setdefault(target.id, []).append(node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bindings.setdefault(node.name, []).extend(
                sub.value for sub in ast.walk(node)
                if isinstance(sub, ast.Return) and sub.value is not None
            )
    return bindings


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

    Include attribute calls and import aliases. Names may conservatively join
    unrelated same-named helpers; this can over-report, but must not hide an
    agent caller just because an operator uses a different spelling.
    """
    spans = _function_spans(tree)
    # Imports in different scopes can bind the same spelling differently.
    # This file-wide inventory must retain every possible target rather than
    # let whichever import is visited last erase a reachable caller.
    imported: Dict[str, Set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.setdefault(alias.asname or alias.name, set()).add(alias.name)

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
        callee = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if not callee:
            continue
        caller = enclosing(node.lineno)
        if caller:
            names = out.setdefault(caller, set())
            names.add(callee)
            # A bare import alias does not rename module.helper(). Preserve
            # the original spelling too: another scope may bind it locally.
            if isinstance(node.func, ast.Name):
                names.update(imported.get(callee, ()))
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
        if relative in self._middleware:
            return {MIDDLEWARE_SENTINEL}
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
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        trees[relative] = tree
        index.add(relative, tree)

    if not trees:
        raise OSError(f"No handler source files found under {HANDLER_ROOT}")
    for relative, tree in trees.items():
        bindings = _value_bindings(tree)
        for value in _hint_value_nodes(tree):
            for constant in _string_constants(value, bindings):
                for match in CALL_PATTERN.finditer(constant.value):
                    name = match.group(1)
                    if name in roster:
                        emitters = index.emitters(relative, constant.lineno)
                        sites.setdefault(name, set()).add(
                            (
                                f"{relative}:{constant.lineno}",
                                _hinted_action(constant.value, match),
                                frozenset(emitters) if emitters else None,
                            )
                        )
        # Structured name lists are unambiguous; they do not need a call
        # regex, and legacy aliases are part of the callable-name roster.
        for value in _hint_value_nodes(tree, {"related_tools"}):
            for constant in _string_constants(value, bindings):
                if constant.value in roster:
                    emitters = index.emitters(relative, constant.lineno)
                    sites.setdefault(constant.value, set()).add((
                        f"{relative}:{constant.lineno}", None,
                        frozenset(emitters) if emitters else None,
                    ))
    return sites


def find_dead_end_hints(mode: str) -> List[DeadEndHint]:
    """Hints in `mode` that name a tool `mode` does not advertise."""
    import src.mcp_handlers  # noqa: F401  -- settles every @mcp_tool decorator

    from src.mcp_handlers.decorators import get_tool_registry
    from src.mcp_handlers.tool_stability import (
        AGENT_WORKFLOW_ALIASES,
        list_all_aliases,
        resolve_tool_alias,
    )
    from src.tool_modes import get_tools_for_mode
    from scripts.diagnostics.tool_surface_cost import validate_mode

    validate_mode(mode)
    roster = set(get_tool_registry()) | set(list_all_aliases())
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
        # Keep sites with potentially reachable or unresolved emitters. A hint
        # inside `handle_operator_resume_agent` is not `standard`'s problem:
        # an agent on `standard` cannot call that tool, so it never sees the
        # string. Judging every hint against one profile reported three
        # operator-path tools as stranding agents when they are advertised
        # exactly where their callers are.
        pairs = [(site, action) for site, action, emitter in triples if reachable(emitter)]
        if not pairs:
            continue
        sites = sorted({site for site, _ in pairs})
        actions = sorted({action for _, action in pairs if action})
        # Keep coverage for every action, including uncovered actions beside
        # covered ones. Names/actions alone do not prove argument compatibility.
        implementation, hinted_alias = resolve_tool_alias(tool)
        calls = []
        for site, action in sorted(set(pairs), key=lambda pair: (pair[0], pair[1] or "")):
            effective_action = action or (hinted_alias.inject_action if hinted_alias else None)
            covering_alias = (implementation if implementation in advertised else
                              alias_for_call.get((implementation, effective_action)))
            calls.append({"site": site, "action": effective_action,
                          "advertised_alias": covering_alias})
        covering = {call["advertised_alias"] for call in calls}
        aliases = sorted(covering - {None})
        alias = (
            next(iter(covering))
            if len(covering) == 1 and None not in covering
            else None
        )
        findings.append(
            DeadEndHint(
                tool=tool, sites=sites, actions=actions, advertised_alias=alias,
                advertised_aliases=aliases, calls=calls,
            )
        )
    return sorted(findings, key=lambda f: (-len(f.sites), f.tool))


def _render(findings: List[DeadEndHint], mode: str, classify: bool) -> None:
    if not findings:
        print(f"{mode}: no candidate advertisement mismatches in the scanned fields; dynamic hints are not covered.")
        return

    total_sites = sum(len(f.sites) for f in findings)
    print(
        f"{mode}: {len(findings)} unadvertised names in static hint candidates "
        f"({total_sites} sites; caller severity requires review)."
    )
    groups = (
        [
            ("advertised names cover every hinted name/action "
             "(check arguments before rewriting)",
             [f for f in findings if f.kind == "names_unadvertised_twin"]),
            ("only some hinted actions have advertised names",
             [f for f in findings if f.kind == "partially_covered_actions"]),
            ("no advertised name resolved by the alias map "
             "(inspect router paths and caller context)",
             [f for f in findings if f.kind == "no_mapped_advertised_name"]),
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
                f"  [candidate aliases: {', '.join(finding.advertised_aliases)}]"
                if finding.advertised_aliases
                else ""
            )
            if finding.kind == "partially_covered_actions":
                suffix += " [partial action coverage]"
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


def finding_keys(findings: List[DeadEndHint]) -> Set[tuple]:
    return {(finding.tool, call["site"], call["action"])
            for finding in findings for call in finding.calls}


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
            "Exit non-zero on any candidate not in KNOWN_DEAD_ENDS, or on "
            "a ledger entry that no longer matches (for use as a CI guard)"
        ),
    )
    args = parser.parse_args()

    try:
        findings = find_dead_end_hints(args.mode)
    except (ModuleNotFoundError, OSError, SyntaxError, UnicodeDecodeError, ValueError) as exc:
        print(
            f"WARNING: hint scan unavailable ({exc}); the handler tree could "
            "not be read/validated, so this is 'unknown', not 'clean'",
            file=sys.stderr,
        )
        return EXIT_REGISTRY_UNAVAILABLE

    if args.json:
        print(
            json.dumps(
                {
                    "mode": args.mode,
                    "scope": "static candidate inventory; dynamic hints and path conditions are not proven",
                    "severity": "unassessed",
                    "alias_coverage_scope": "name and action only; arguments, authorization and response shapes require review",
                    "findings": [
                        {
                            "tool": f.tool,
                            "kind": f.kind,
                            "advertised_alias": f.advertised_alias,
                            "advertised_aliases": f.advertised_aliases,
                            "actions": f.actions,
                            "site_count": len(f.sites),
                            "sites": f.sites,
                            "calls": f.calls,
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
        seen = finding_keys(findings)
        ledger = KNOWN_DEAD_ENDS if args.mode == "standard" else {}
        order = lambda key: (key[0], key[1], key[2] or "")
        unlisted = sorted(seen - set(ledger), key=order)
        stale = sorted(set(ledger) - seen, key=order)
        for tool, site, action in unlisted:
            print(
                f"UNREVIEWED hint candidate: {site} names {tool!r} action={action!r}, which {args.mode} "
                "does not advertise. Check caller/profile, arguments and the "
                "recovery path before changing the surface or accepting it.",
                file=sys.stderr,
            )
        for tool, site, action in stale:
            print(
                f"STALE ledger entry: {site} / {tool!r} action={action!r} is listed in "
                "KNOWN_DEAD_ENDS but no longer reported. Remove the entry.",
                file=sys.stderr,
            )
        if unlisted or stale:
            return EXIT_DEAD_END_HINT
    return 0


if __name__ == "__main__":
    sys.exit(main())
