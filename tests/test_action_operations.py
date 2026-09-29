"""Router-action read/write classes do not depend on the legacy alias table.

Until 2026-09-28 the only record that agent(action='list') or
knowledge(action='details') is a read was the operation carried by a legacy
alias entry (list_agents, get_discovery_details). Retiring those aliases
reclassified sixteen router reads as their router's class, write or admin, so
a timed-out read told its caller the change might have been saved.
tool_meta.ACTION_OPERATIONS now declares those classes directly.
"""

import pytest

import src.mcp_handlers  # noqa: F401  (registers the routers)
import src.mcp_handlers.consolidated  # noqa: F401
from src.mcp_handlers import tool_stability
from src.mcp_handlers.decorators import _ROUTER_ACTION_HANDLERS, resolve_call_operation
from src.tool_meta import ACTION_OPERATIONS, OPERATIONS


def _classify_every_router_action():
    out = {}
    for router, actions in _ROUTER_ACTION_HANDLERS.items():
        for action, handler in actions.items():
            via_router = resolve_call_operation(router, {"action": action})
            out[(router, action)] = via_router.operation
            inner = getattr(handler, "_mcp_tool_name", None)
            if inner and inner != router:
                out[("handler", inner)] = resolve_call_operation(inner, {}).operation
    return out


def test_every_declared_class_names_a_real_router_action():
    for (router, action), operation in ACTION_OPERATIONS.items():
        assert action in _ROUTER_ACTION_HANDLERS.get(router, {}), (router, action)
        assert operation in OPERATIONS, (router, action, operation)


def test_classes_survive_without_the_legacy_aliases(monkeypatch):
    with_aliases = _classify_every_router_action()
    workflow_only = {
        name: alias
        for name, alias in tool_stability._TOOL_ALIASES.items()
        if alias.reason == "intuitive_alias"
    }
    monkeypatch.setattr(tool_stability, "_TOOL_ALIASES", workflow_only)
    without = _classify_every_router_action()
    changed = {k: (with_aliases[k], without.get(k)) for k in with_aliases if with_aliases[k] != without.get(k)}
    assert not changed, changed


@pytest.mark.parametrize("router,action", [("agent", "list"), ("knowledge", "details")])
def test_declared_reads_are_retry_safe(router, action):
    assert resolve_call_operation(router, {"action": action}).retry_safe
