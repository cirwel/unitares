"""`op` names an action only on a tool whose handler reads it.

``action_router`` generates a router that reads ``action or op``. A tool that
declares ``known_actions`` by hand routes on ``action`` alone:
``self_recovery`` reads ``arguments.get("action", "check")`` and its parameter
model has no ``op`` field, so ``self_recovery(op="quick")`` runs ``check``.
The shared call resolver read ``op`` for every tool and named that call
``quick``, so identity, stakes and ``audit.tool_usage`` judged an action that
never ran.
"""

from __future__ import annotations

import pytest

# Settle handler imports so every tool and router is registered.
import src.mcp_handlers  # noqa: F401
import src.mcp_handlers.consolidated  # noqa: F401
import src.mcp_handlers.core  # noqa: F401
import src.mcp_handlers.support.model_inference  # noqa: F401

from src.mcp_handlers.decorators import (
    get_tool_registry,
    get_tool_definition,
    resolve_canonical_action_and_source,
)


def _action_tools() -> list[str]:
    return sorted(
        name for name in get_tool_registry()
        if get_tool_definition(name).known_actions
    )


def _is_action_router(name: str) -> bool:
    # action_router's generated handler is defined in decorators.py.
    return get_tool_definition(name).handler.__module__ == "src.mcp_handlers.decorators"


def test_the_flag_is_set_exactly_on_action_routers():
    tools = _action_tools()
    assert {"knowledge", "self_recovery", "cirs_protocol"} <= set(tools)
    wrong = [
        name for name in tools
        if get_tool_definition(name).reads_op_as_action != _is_action_router(name)
    ]
    assert not wrong, wrong


@pytest.mark.parametrize("name", _action_tools())
def test_op_selects_an_action_only_where_the_handler_reads_it(name):
    td = get_tool_definition(name)
    other = sorted(a for a in td.known_actions if a != td.default_action)[0]
    _canonical, action, source = resolve_canonical_action_and_source(name, {"op": other})
    if td.reads_op_as_action:
        assert (action, source) == (other, "explicit")
    else:
        expected_source = "default" if td.default_action else None
        assert (action, source) == (td.default_action, expected_source)


def test_hand_declared_action_tools_do_not_accept_op():
    """What makes ignoring `op` correct: the parameter model has no such field."""
    from src.mcp_compat import get_tool_input_schema
    from src.tool_schemas import get_tool_definitions

    definitions = {tool.name: tool for tool in get_tool_definitions()}
    hand_declared = [n for n in _action_tools() if not get_tool_definition(n).reads_op_as_action]
    assert hand_declared, "no hand-declared action tool left to check"
    accepting = [
        name for name in hand_declared
        if "op" in (get_tool_input_schema(definitions[name], {}).get("properties") or {})
    ]
    assert not accepting, accepting


def test_explicit_action_still_wins_on_every_action_tool():
    for name in _action_tools():
        td = get_tool_definition(name)
        chosen = sorted(td.known_actions)[-1]
        _c, action, source = resolve_canonical_action_and_source(
            name, {"action": chosen, "op": sorted(td.known_actions)[0]},
        )
        assert (action, source) == (chosen, "explicit"), name
