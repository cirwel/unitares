"""Tool-name sets that answer for the call a name dispatches to.

A call is named three ways during dispatch: the name the caller invoked,
which may be one of the aliases in ``tool_stability._TOOL_ALIASES``; the
registered tool that name resolves to; and the action a caller or an alias
selects on a router. A bare set of strings does not say which of the three it
holds, and matched against the wrong one it stops matching without an error.
A set consulted after ``resolve_alias`` never sees an alias name. A set
consulted before it sees only the name the caller happened to use, so
``store_finding`` and ``knowledge(action="store")`` get different answers from
the same set.

A ``CallSet`` is declared in resolved terms only: registered tool names, and
``(tool, action)`` pairs where one router action is meant. It answers
membership for any name through
``decorators.resolve_canonical_action_and_source``, the resolver the identity
and stakes gates already share, so an alias is a member exactly when the call
it dispatches to is, wherever in the pipeline the set is consulted.

``tests/test_tool_call_sets.py`` holds every declared entry to the registries,
and ``tests/test_no_bare_tool_name_sets.py`` fails on a new tool-name literal
in ``src/`` that bypasses this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional, Tuple

_DECLARED: Dict[str, "CallSet"] = {}


@dataclass(frozen=True)
class CallSet:
    """A named set of tools and router actions, matched on the resolved call."""

    name: str
    tools: FrozenSet[str]
    actions: FrozenSet[Tuple[str, str]]

    def matches(self, tool_name: str, arguments: Optional[Mapping[str, Any]] = None) -> bool:
        """Whether a call to ``tool_name`` with ``arguments`` is in this set.

        ``tool_name`` may be the invoked name or the canonical one; with the
        canonical name, pass the arguments the alias step already rewrote so
        an injected action is visible. A whole-tool member matches every
        action of that tool.
        """
        from src.mcp_handlers.decorators import resolve_canonical_action_and_source

        canonical, action, _source = resolve_canonical_action_and_source(
            tool_name, arguments,
        )
        if canonical in self.tools:
            return True
        return action is not None and (canonical, action) in self.actions

    def __contains__(self, item: object) -> bool:
        raise TypeError(
            f"call set {self.name!r} has no `in`: a bare name test is the "
            "mistake this type exists to prevent; use .matches(tool_name, arguments)"
        )


def call_set(
    name: str,
    *,
    tools: Iterable[str] = (),
    actions: Iterable[Tuple[str, str]] = (),
) -> CallSet:
    """Declare a ``CallSet`` under a unique ``name`` and return it.

    Re-declaring an identical set returns the existing one, so a module reload
    is harmless; declaring different contents under a taken name raises.
    """
    declared = CallSet(name=name, tools=frozenset(tools), actions=frozenset(actions))
    if not declared.tools and not declared.actions:
        raise ValueError(f"call set {name!r} declares no tools and no actions")
    existing = _DECLARED.get(name)
    if existing is not None:
        if existing != declared:
            raise ValueError(f"call set {name!r} is already declared with other contents")
        return existing
    _DECLARED[name] = declared
    return declared


def declared_call_sets() -> Mapping[str, CallSet]:
    """Every ``CallSet`` declared by the modules imported so far."""
    return dict(_DECLARED)
