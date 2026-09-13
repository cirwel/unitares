"""Judging a served tool name against the MCP wire.

A name a discovery surface emits is usable by a schema-driven client only if
tools/list carries it. On /mcp/ FastMCP registers the register=True tools and
the workflow aliases and nothing else (src/tool_registration.py), so a
dispatch-only twin such as `list_agents` -- resolved through the alias table
on REST and stdio -- is `Unknown tool` there. A hint may instead be written as
a call shape against a router on the wire (`agent(action='list')`); then the
router must be mounted and the action one it routes.

Shared by tests/test_tool_registry_bookkeeping.py (the source table) and
tests/test_list_tools_names_the_wire.py (the served payload) so the two hold
one standard.
"""

from __future__ import annotations

import ast
import re
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple

#: A whole value written as one call: the name immediately followed by an
#: argument list that runs to the end. Adjacency matters -- `agent (action=...)`
#: is prose about the router, not a call.
_CALL_SHAPE = re.compile(r"\A([a-z_][a-z0-9_]{2,})\((.*)\)\Z", re.S)
_ACTION_ARG = re.compile(r"\baction\s*=\s*['\"]([a-z_]+)['\"]")


def parse_call_shape(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """(tool, action) for a value written as one call, else None.

    `agent(action='list')` -> ("agent", "list"); `consult(brief='...')` ->
    ("consult", None); `list_agents` -> None. Only a value that IS a call
    parses; prose containing one does not (the hint scanner's CALL_PATTERN
    covers prose). The action is read by parsing the call, so a quoted
    `action='store'` inside another argument is not mistaken for it; a
    pseudo-call Python cannot parse falls back to a regex read.
    """
    stripped = text.strip()
    match = _CALL_SHAPE.match(stripped)
    if match is None:
        return None
    tool = match.group(1)
    try:
        call = ast.parse(stripped, mode="eval").body
    except SyntaxError:
        found = _ACTION_ARG.search(match.group(2))
        return tool, found.group(1) if found else None
    if isinstance(call, ast.Call):
        for keyword in call.keywords:
            if (
                keyword.arg == "action"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                return tool, keyword.value.value
    return tool, None


def declared_actions() -> Dict[str, FrozenSet[str]]:
    """tool -> the actions its router declares; flat tools are absent."""
    from src.mcp_handlers.decorators import get_tool_definition, get_tool_registry

    out: Dict[str, FrozenSet[str]] = {}
    for name in get_tool_registry():
        definition = get_tool_definition(name)
        if definition is not None and definition.known_actions:
            out[name] = frozenset(definition.known_actions)
    return out


def build_mount() -> Set[str]:
    """The names the production registrars put on a fresh FastMCP mount.

    Built through auto_register_all_tools and _register_common_aliases on a
    throwaway instance rather than read from the server module, so the answer
    is what the registrars register, not what an earlier import left mounted.
    """
    from src.mcp_compat import FastMCP
    from src.tool_registration import _register_common_aliases, auto_register_all_tools

    server = FastMCP("wire-names-probe")
    auto_register_all_tools(server)
    _register_common_aliases(server)
    return set(server._tool_manager._tools)


def dead_ends(
    entries: Iterable[Tuple[str, str]],
    mounted: Set[str],
    actions: Mapping[str, FrozenSet[str]],
) -> List[str]:
    """Entries a client offered only ``mounted`` could not act on.

    ``entries`` are (where, value) pairs. A bare value must be a mounted
    name; a call shape must name a mounted tool and, when it states an
    action, one ``actions`` declares for that tool.
    """
    problems: List[str] = []
    for where, value in entries:
        shape = parse_call_shape(value)
        if shape is None:
            if value not in mounted:
                problems.append(f"{where}: {value!r} is not a name on the wire")
            continue
        tool, action = shape
        if tool not in mounted:
            problems.append(f"{where}: {value!r} calls a tool that is not on the wire")
        elif action is not None and action not in actions.get(tool, frozenset()):
            problems.append(f"{where}: {value!r} names an action {tool} does not route")
    return problems
