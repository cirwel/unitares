"""Every key a cirs_protocol handler reads is declared on the tool's wire schema.

``cirs_protocol`` routes ``protocol`` to one of seven handlers, and each handler
reads its own arguments straight off the dict. Until 2026-09-12 the wire schema
declared five of those keys. Two things follow from an undeclared key, and this
module holds the schema to both:

1. FastMCP builds the ``/mcp/`` argument model from the declared properties and
   discards any other key before dispatch, so a caller's ``since_hours``,
   ``action_id`` or ``trust_default`` never reached the handler over the MCP
   transport, while REST and in-process callers (merged back by
   ``middleware/params_step.validate_params``) were unaffected. Same class as
   ``observe`` / ``describe_tool``, interface release 1.2.0.

2. The dispatch middleware hands the handler every DECLARED field, None
   included, so a declared field's schema default is what the handler sees
   when the caller omits the parameter. ``limit`` was declared
   ``Optional[int] = None`` while the three query handlers did
   ``int(arguments.get("limit", 50))``: every cirs_protocol query failed with a
   TypeError through dispatch, on every transport, and the handler tests
   (which call the handlers directly) never saw it. Schema defaults therefore
   mirror the handlers', and a field with no default is one its handler reads
   None-safely.

The key inventory is read from the handlers' own source rather than
hand-listed, so a handler that starts reading a new argument fails here the
day it does.
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock

import pytest
from mcp.types import TextContent

from src.mcp_handlers.cirs.protocol import _CIRS_DISPATCHERS, handle_cirs_protocol
from src.mcp_handlers.schemas.core import CirsProtocolParams
from src.mcp_handlers.schemas.router_actions import COMMON_ROUTER_FIELDS, wire_field_names

# stability_restored emits on every call and reads no ``action``; its
# parameters are classified under ``emit``, the action it performs.
ACTIONLESS = {"stability_restored": ("emit",)}


def _admitted_protocols() -> tuple[str, ...]:
    """The protocols the schema's Literal admits (the dispatch table may route more)."""
    return get_args(CirsProtocolParams.model_fields["protocol"].annotation)


def _keys_read(handler) -> dict[str, list]:
    """``{key: [source defaults]}`` for every ``arguments.get("key"[, default])``
    in the handler's module. A default of None means the call had none."""
    tree = ast.parse(inspect.getsource(sys.modules[handler.__module__]))
    keys: dict[str, set] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "get" or not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id != "arguments" or not node.args:
            continue
        if not isinstance(node.args[0], ast.Constant):
            continue
        default = ast.unparse(node.args[1]) if len(node.args) > 1 else None
        keys.setdefault(node.args[0].value, set()).add(default)
    return {key: sorted(defaults, key=str) for key, defaults in keys.items()}


def _run_args(run_method, arguments):
    """Positional args for the internal Tool.run across mcp 1.x/2.x."""
    params = inspect.signature(run_method).parameters
    if "context" in params and params["context"].default is inspect.Parameter.empty:
        return (arguments, None)
    return (arguments,)


async def _vocabulary(protocol: str) -> tuple[str, ...]:
    """The actions a protocol routes, asked of the handler itself."""
    if protocol in ACTIONLESS:
        return ACTIONLESS[protocol]
    result = await _CIRS_DISPATCHERS[protocol].__wrapped__({"action": "__unroutable__"})
    body = json.loads(result[0].text)
    valid = (body.get("recovery") or {}).get("valid_actions")
    assert valid, f"{protocol} did not refuse an unroutable action with valid_actions"
    return tuple(valid)


def test_the_scan_sees_the_handlers():
    """Guard the inventory itself: an empty scan would pass every test below."""
    read = _keys_read(_CIRS_DISPATCHERS["void_alert"])
    assert {"since_hours", "filter_agent_id", "limit", "severity"} <= set(read)
    assert read["limit"] == ["50"]
    assert read["since_hours"] == ["1.0"]


# --- the schema declares what the handlers read ---


@pytest.mark.parametrize("protocol", _admitted_protocols())
def test_every_key_the_handler_reads_is_on_the_wire(protocol):
    wire = set(wire_field_names(CirsProtocolParams))
    missing = sorted(set(_keys_read(_CIRS_DISPATCHERS[protocol])) - wire)
    assert not missing, (
        f"{protocol}: its handler reads {missing}, which cirs_protocol's wire "
        "schema does not declare, so FastMCP drops them before dispatch on /mcp/"
    )


@pytest.mark.parametrize("protocol", _admitted_protocols())
def test_schema_defaults_mirror_the_handler_defaults(protocol):
    """The middleware materializes every declared field, so the schema default
    is the value the handler gets for an omitted parameter and must be the
    handler's own. A handler default of '' or none at all pairs with a None
    schema default, and the handler is then held to reading None safely
    (test_emit_and_initiate_tolerate_the_none_the_middleware_materializes)."""
    fields = CirsProtocolParams.model_fields
    for key, defaults in _keys_read(_CIRS_DISPATCHERS[protocol]).items():
        if key in COMMON_ROUTER_FIELDS or key == "protocol" or key not in fields:
            continue
        schema_default = fields[key].default
        for source_default in defaults:
            value = ast.literal_eval(source_default) if source_default is not None else None
            if value is None or value == "":
                assert schema_default is None, (
                    f"{protocol}.{key}: the handler treats an omitted value as "
                    f"{source_default!r}; the schema must not invent {schema_default!r}"
                )
            else:
                assert schema_default == value, (
                    f"{protocol}.{key}: the handler defaults to {source_default}, "
                    f"the schema advertises and delivers {schema_default!r}"
                )


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", _admitted_protocols())
async def test_every_key_is_classified_under_an_action_the_protocol_routes(protocol):
    """ACTION_FIELDS is what describe_tool(action=...) shows. A key read by
    void_alert's query handler but classified only under ``set`` would be
    declared, delivered, and invisible to the caller asking about query."""
    vocabulary = await _vocabulary(protocol)
    shown = set().union(*(CirsProtocolParams.ACTION_FIELDS[action] for action in vocabulary))
    unclassified = sorted(
        set(_keys_read(_CIRS_DISPATCHERS[protocol])) - shown - COMMON_ROUTER_FIELDS
    )
    assert not unclassified, (
        f"{protocol} routes {vocabulary}; its handler reads {unclassified}, "
        "classified under none of them in CirsProtocolParams.ACTION_FIELDS"
    )


# --- the /mcp/ transport delivers them ---

# One value of the right shape for every non-common wire field. The coverage
# test below makes a new field fail here until it is added.
SAMPLE_VALUES = {
    "target_agent_id": "peer",
    "limit": 3,
    "severity": "warning",
    "context_ref": "ticket-1",
    "filter_agent_id": "a1",
    "filter_severity": "critical",
    "since_hours": 5.0,
    "include_trajectory": False,
    "agent_ids": ["a1", "a2"],
    "regime": "stable",
    "max_risk": 0.3,
    "min_coherence": 0.5,
    "source_agent_id": "s1",
    "min_similarity": 0.7,
    "trust_default": "full",
    "trust_overrides": {"a2": "none"},
    "void_response_policy": "assist",
    "max_delegation_complexity": 0.9,
    "accept_coherence_threshold": 0.2,
    "action_type": "coordination_sync",
    "payload": {"k": 1},
    "action_id": "abc",
    "accept": True,
    "response_data": {"r": 2},
    "as_initiator": False,
    "as_target": False,
    "status_filter": "pending",
}


def test_sample_covers_every_wire_field():
    declared = set(wire_field_names(CirsProtocolParams)) - COMMON_ROUTER_FIELDS - {"protocol"}
    assert set(SAMPLE_VALUES) == declared, (
        f"add {sorted(declared - set(SAMPLE_VALUES))} to SAMPLE_VALUES / drop "
        f"{sorted(set(SAMPLE_VALUES) - declared)}"
    )


def test_fastmcp_argument_model_keeps_every_declared_parameter():
    """The registered FastMCP tool's argument model, not just the schema builder."""
    from src import mcp_server

    tool = mcp_server.mcp._tool_manager.get_tool("cirs_protocol")
    assert tool is not None
    validated = tool.fn_metadata.arg_model.model_validate(
        {"protocol": "void_alert", "action": "query", **SAMPLE_VALUES}
    )
    dumped = validated.model_dump_one_level()
    for key, value in SAMPLE_VALUES.items():
        assert dumped[key] == value, key


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"protocol": "void_alert", "action": "query", "since_hours": 5.0,
         "filter_agent_id": "a1", "limit": 3},
        {"protocol": "governance_action", "action": "status", "action_id": "abc"},
        {"protocol": "boundary_contract", "action": "set", "trust_default": "full",
         "trust_overrides": {"a2": "none"}},
    ],
    ids=["void_alert.query", "governance_action.status", "boundary_contract.set"],
)
async def test_mcp_wire_delivers_the_parameters_to_dispatch(monkeypatch, arguments):
    """End to end through the registered FastMCP tool: ``Tool.run`` validates
    against the argument model, calls the typed wrapper, which calls
    ``get_tool_wrapper("cirs_protocol")``, whose closure calls
    ``dispatch_tool``. Spying on that seam shows what the ``/mcp/`` transport
    actually hands over. Before 2026-09-12 every key here except ``protocol``,
    ``action`` and ``limit`` was discarded at the first step."""
    import src.tool_registration as registration
    from src import mcp_server

    delivered: dict = {}

    async def spy(name, kwargs):
        delivered[name] = dict(kwargs)
        return [TextContent(type="text", text=json.dumps({"success": True}))]

    monkeypatch.setattr(registration, "dispatch_tool", spy)
    tool = mcp_server.mcp._tool_manager.get_tool("cirs_protocol")
    await tool.run(*_run_args(tool.run, arguments), convert_result=False)
    handed_over = delivered["cirs_protocol"]
    for key, value in arguments.items():
        assert handed_over.get(key) == value, (
            f"{key} was sent over the MCP wire and not delivered to dispatch"
        )


# --- the dispatch path gives the handlers what they expect ---


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["void_alert", "state_announce", "coherence_report"])
async def test_dispatch_validation_hands_query_handlers_their_own_defaults(protocol):
    """Through the real params step. Before, ``limit`` arrived as None and
    ``int(None)`` raised, so every query failed on every transport."""
    from src.mcp_handlers.middleware.params_step import validate_params

    outcome = await validate_params(
        "cirs_protocol", {"protocol": protocol, "action": "query"}, SimpleNamespace()
    )
    assert isinstance(outcome, tuple), outcome  # a list is a validation-error envelope
    _, validated, _ = outcome
    assert validated["limit"] == 50
    body = json.loads((await handle_cirs_protocol.__wrapped__(validated))[0].text)
    assert body["success"] is True, body
    assert body["filters_applied"]["limit"] == 50


@pytest.mark.asyncio
async def test_dispatch_validation_carries_explicit_filters_to_the_handler():
    from src.mcp_handlers.middleware.params_step import validate_params

    async def through_dispatch_validation(arguments):
        _, validated, _ = await validate_params("cirs_protocol", arguments, SimpleNamespace())
        return json.loads((await handle_cirs_protocol.__wrapped__(validated))[0].text)

    body = await through_dispatch_validation(
        {"protocol": "void_alert", "action": "query", "since_hours": 5, "filter_severity": "critical"}
    )
    assert body["success"] is True, body
    assert body["summary"]["query_window_hours"] == 5.0
    assert body["filters_applied"]["severity"] == "critical"

    # Omitted: the handler's defaults, and no None where the handler expects a value.
    body = await through_dispatch_validation({"protocol": "void_alert", "action": "query"})
    assert body["filters_applied"] == {
        "agent_id": None, "severity": None, "since_hours": 1.0, "limit": 50,
    }

    # governance_action status reaches its own lookup rather than "action_id required".
    body = await through_dispatch_validation(
        {"protocol": "governance_action", "action": "status", "action_id": "no-such-action"}
    )
    assert body["success"] is False
    assert "not found" in body["error"]


@pytest.mark.asyncio
async def test_emit_and_initiate_tolerate_the_none_the_middleware_materializes(monkeypatch):
    """``severity``, ``filter_severity`` and ``action_type`` default to '' in
    the handlers and were read as ``.get(key, "").lower()``; a declared field
    the caller omitted arrives as None, which raised AttributeError instead of
    reaching the handler's structured refusal."""
    from src.mcp_handlers.cirs import governance_action as governance_module
    from src.mcp_handlers.cirs import void as void_module
    from src.mcp_handlers.cirs.storage import _void_alert_buffer

    monkeypatch.setattr(void_module, "require_registered_agent", lambda arguments: ("agent-1", None))
    monitor = SimpleNamespace(
        state=SimpleNamespace(V=0.0, coherence=0.7, V_history=[], void_active=False),
        get_metrics=lambda: {"risk_score": 0.1},
    )
    monkeypatch.setattr(
        void_module, "mcp_server", SimpleNamespace(get_or_create_monitor=lambda agent_id: monitor)
    )
    monkeypatch.setattr("src.agent_monitor_state.ensure_hydrated", AsyncMock())

    omitted = CirsProtocolParams(protocol="void_alert", action="emit").model_dump()
    assert omitted["severity"] is None
    body = json.loads((await void_module.handle_void_alert.__wrapped__(omitted))[0].text)
    # V=0 sits under any void threshold: the handler's own refusal, reached past the None.
    assert body["success"] is False
    assert "threshold" in body["error"] and "NoneType" not in body["error"]
    assert not _void_alert_buffer

    monkeypatch.setattr(
        governance_module, "require_registered_agent", lambda arguments: ("agent-1", None)
    )
    omitted = CirsProtocolParams(
        protocol="governance_action", action="initiate", target_agent_id="peer"
    ).model_dump()
    assert omitted["action_type"] is None
    body = json.loads(
        (await governance_module.handle_governance_action.__wrapped__(omitted))[0].text
    )
    assert body["success"] is False
    assert "action_type" in body["error"] and "NoneType" not in body["error"]
