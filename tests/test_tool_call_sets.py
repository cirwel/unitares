"""Every declared CallSet names only what dispatch can reach.

A ``CallSet`` (``src/tool_call_sets.py``) is declared in resolved terms:
registered tool names, and ``(tool, action)`` pairs for a single router action.
An alias in a set would be answered through its target, so writing one there
is a mistake, and so is a name nothing registers. Before this type existed,
tool-name sets drifted into exactly those entries without an error.

The declaring modules are found by scanning ``src/`` for ``call_set(`` calls,
so a new set is checked without editing this file.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

# Settle handler imports so every tool and router is registered.
import src.mcp_handlers  # noqa: F401
import src.mcp_handlers.consolidated  # noqa: F401
import src.mcp_handlers.core  # noqa: F401
import src.mcp_handlers.support.model_inference  # noqa: F401

from src.mcp_handlers.decorators import get_tool_definition, get_tool_registry
from src.mcp_handlers.tool_stability import _TOOL_ALIASES
from src.tool_call_sets import CallSet, call_set, declared_call_sets

REPO_ROOT = Path(__file__).resolve().parents[1]


def _declaring_modules() -> list[str]:
    modules = []
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        if path.name == "tool_call_sets.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "call_set"
            for node in ast.walk(tree)
        ):
            relative = path.relative_to(REPO_ROOT).with_suffix("")
            modules.append(".".join(relative.parts))
    return modules


DECLARING_MODULES = _declaring_modules()


@pytest.fixture(scope="module")
def call_sets() -> dict:
    for module in DECLARING_MODULES:
        importlib.import_module(module)
    return dict(declared_call_sets())


def test_declaring_modules_are_found():
    assert "src.services.tool_usage_recorder" in DECLARING_MODULES
    assert "src.mcp_handlers.middleware.params_step" in DECLARING_MODULES


def test_every_declaring_module_registers_a_set(call_sets):
    assert len(call_sets) >= len(DECLARING_MODULES)


def test_tool_members_are_registered_tools_not_aliases(call_sets):
    registered = set(get_tool_registry())
    problems = []
    for cs in call_sets.values():
        for tool in sorted(cs.tools):
            if tool in _TOOL_ALIASES:
                target = _TOOL_ALIASES[tool]
                problems.append(
                    f"{cs.name}: {tool!r} is an alias of {target.new_name}"
                    f"{f' (action={target.inject_action!r})' if target.inject_action else ''}; "
                    "declare the call it dispatches to"
                )
            elif tool not in registered:
                problems.append(f"{cs.name}: {tool!r} is not a registered tool")
    assert not problems, "\n".join(problems)


def test_action_members_are_routed_actions(call_sets):
    problems = []
    for cs in call_sets.values():
        for tool, action in sorted(cs.actions):
            td = get_tool_definition(tool)
            if td is None:
                problems.append(f"{cs.name}: ({tool!r}, {action!r}) names no registered tool")
            elif not td.known_actions or action not in td.known_actions:
                problems.append(
                    f"{cs.name}: {tool!r} does not route action {action!r} "
                    f"(routes {sorted(td.known_actions or [])})"
                )
            elif tool in cs.tools:
                problems.append(f"{cs.name}: ({tool!r}, {action!r}) is redundant with the whole tool")
    assert not problems, "\n".join(problems)


def test_an_alias_matches_exactly_when_its_call_does(call_sets):
    problems = []
    for cs in call_sets.values():
        for alias, info in _TOOL_ALIASES.items():
            as_invoked = cs.matches(alias, {})
            as_dispatched = cs.matches(
                info.new_name,
                {"action": info.inject_action} if info.inject_action else {},
            )
            if as_invoked != as_dispatched:
                problems.append(f"{cs.name}: {alias} -> {info.new_name} disagrees")
    assert not problems, "\n".join(problems)


def test_membership_is_not_a_bare_name_test():
    cs = CallSet(name="t", tools=frozenset({"knowledge"}), actions=frozenset())
    with pytest.raises(TypeError, match="matches"):
        "knowledge" in cs  # noqa: B015


def test_redeclaring_identical_contents_is_idempotent_and_conflicts_raise():
    first = call_set("test_tool_call_sets.redeclare", tools={"health_check"})
    assert call_set("test_tool_call_sets.redeclare", tools={"health_check"}) is first
    with pytest.raises(ValueError, match="other contents"):
        call_set("test_tool_call_sets.redeclare", tools={"list_tools"})


def test_empty_declaration_raises():
    with pytest.raises(ValueError, match="no tools and no actions"):
        call_set("test_tool_call_sets.empty")


def test_an_action_member_does_not_match_other_actions():
    cs = CallSet(
        name="t",
        tools=frozenset(),
        actions=frozenset({("knowledge", "search")}),
    )
    assert cs.matches("knowledge", {"action": "search"})
    assert cs.matches("search_shared_memory", {})
    assert not cs.matches("knowledge", {"action": "store"})
    assert not cs.matches("store_finding", {})
