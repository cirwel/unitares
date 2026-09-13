"""Every key a cirs_protocol handler reads is declared on the tool's wire schema.

``cirs_protocol`` routes ``protocol`` to a handler, and each handler reads its
own arguments straight off the dict. Until #2183 the wire schema declared three
of the twenty-seven keys the selectable protocols' handlers read. Two things
follow from an undeclared key, and this module holds the schema to both:

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

The survey covers the protocols validation admits (``_VALIDATED_PROTOCOLS``).
``resonance_alert`` and ``stability_restored`` are routed by the dispatch table
but refused by the schema and recorded as dormant, so no call reaches their
handlers and their keys are not declared.

The key inventory is read from the handlers' source rather than hand-listed. It
recognizes ``get`` / ``pop`` / ``setdefault`` calls, subscripts and membership
tests, follows a handler into same-module helpers under whatever name they give
the dict, and refuses what it cannot follow: a non-literal key, an alias, or
the dict escaping to a callee nobody has checked. A new read either lands in
the inventory or fails here.
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import TextContent

from src.mcp_handlers.cirs.protocol import (
    _CIRS_DISPATCHERS,
    _VALIDATED_PROTOCOLS,
    handle_cirs_protocol,
)
from src.mcp_handlers.middleware.params_step import validate_params
from src.mcp_handlers.schemas.core import CirsProtocolParams
from src.mcp_handlers.schemas.router_actions import COMMON_ROUTER_FIELDS, wire_field_names
from tests.helpers import parse_result

# `arguments` may leave a handler module only into these callees, each checked
# by hand for the keys it reads. require_registered_agent reads `agent_id`, a
# common field, and writes internal underscore keys (support/agent_auth.py).
_REVIEWED_ESCAPES = frozenset({"require_registered_agent"})

_READ_METHODS = frozenset({"get", "pop", "setdefault"})

# Recorded in place of a default for reads that carry none. The defaults test
# skips them: a subscript or a membership test sees whatever the middleware
# materialized, so there is no handler default to mirror.
_SUBSCRIPT = "<subscript>"
_MEMBERSHIP = "<membership>"


def _keys_read_source(source: str, entry: str, param: str = "arguments") -> dict[str, list]:
    """``{key: [source defaults]}`` for every read of the argument dict that
    ``entry`` reaches in ``source``. A default of None means the read had none.

    Scoped to the entry function's call graph, not to its module: a protocol
    entry point typically reads nothing itself and hands the dict to a
    per-action helper (``_handle_void_alert_query``), and two handlers sharing a
    module need not read the same keys or share defaults. Every use of the dict
    is classified, and an unclassifiable one raises rather than being skipped.
    """
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert entry in functions, f"{entry} is not a module-level function"

    def literal_key(node: ast.AST, where: str) -> str:
        assert isinstance(node, ast.Constant) and isinstance(node.value, str), (
            f"dynamic key at {where}: the inventory cannot follow a non-literal key"
        )
        return node.value

    keys: dict[str, set] = {}
    seen: set[tuple[str, str]] = set()
    pending = [(entry, param)]
    while pending:
        name, var = pending.pop()
        if (name, var) in seen:
            continue
        seen.add((name, var))
        func = functions[name]
        parents = {
            child: node for node in ast.walk(func) for child in ast.iter_child_nodes(node)
        }
        for node in ast.walk(func):
            if not (isinstance(node, ast.Name) and node.id == var):
                continue
            where = f"{name}:{node.lineno}"
            parent = parents.get(node)
            grand = parents.get(parent)
            if (
                isinstance(parent, ast.Attribute)
                and parent.attr in _READ_METHODS
                and isinstance(grand, ast.Call)
                and grand.func is parent
                and grand.args
            ):
                key = literal_key(grand.args[0], where)
                default = ast.unparse(grand.args[1]) if len(grand.args) > 1 else None
                keys.setdefault(key, set()).add(default)
            elif isinstance(parent, ast.Subscript) and parent.value is node:
                keys.setdefault(literal_key(parent.slice, where), set()).add(_SUBSCRIPT)
            elif (
                isinstance(parent, ast.Compare)
                and node in parent.comparators
                and all(isinstance(op, (ast.In, ast.NotIn)) for op in parent.ops)
            ):
                keys.setdefault(literal_key(parent.left, where), set()).add(_MEMBERSHIP)
            elif isinstance(parent, ast.Call) and node in parent.args:
                callee = parent.func.id if isinstance(parent.func, ast.Name) else None
                if callee in functions:
                    position = parent.args.index(node)
                    params = functions[callee].args.args
                    assert position < len(params), (
                        f"{where}: {callee} has no positional parameter {position}"
                    )
                    pending.append((callee, params[position].arg))
                else:
                    assert callee in _REVIEWED_ESCAPES, (
                        f"`{var}` escapes to {ast.unparse(parent.func)} at {where}: the "
                        "inventory cannot see which keys it reads; check it, then add "
                        "it to _REVIEWED_ESCAPES"
                    )
            elif isinstance(parent, ast.keyword):
                raise AssertionError(
                    f"`{var}` escapes as a keyword or ** argument at {where}; the "
                    "inventory cannot see which keys the callee reads"
                )
            elif isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                raise AssertionError(
                    f"`{var}` is aliased at {where}; reads through the alias would be invisible"
                )
            else:
                raise AssertionError(
                    f"unrecognized use of `{var}` at {where}: {ast.unparse(parent)}"
                )
    return {key: sorted(defaults, key=str) for key, defaults in keys.items()}


def _keys_read(handler) -> dict[str, list]:
    """The inventory for a registered protocol handler (``@wraps`` keeps its name)."""
    module = sys.modules[handler.__module__]
    return _keys_read_source(inspect.getsource(module), handler.__name__)


def _run_args(run_method, arguments):
    """Positional args for the internal Tool.run across mcp 1.x/2.x."""
    params = inspect.signature(run_method).parameters
    if "context" in params and params["context"].default is inspect.Parameter.empty:
        return (arguments, None)
    return (arguments,)


async def _through_dispatch_validation(arguments):
    """The real params step, then the protocol dispatcher, as dispatch runs them."""
    outcome = await validate_params("cirs_protocol", dict(arguments), SimpleNamespace())
    # Unpacking a refusal would fail as "not enough values to unpack" and hide
    # the validation error that is the actual news.
    assert isinstance(outcome, tuple), parse_result(outcome)
    return parse_result(await handle_cirs_protocol.__wrapped__(outcome[1]))


# --- the survey itself ---


def test_the_survey_covers_the_selectable_protocols():
    """Guard the fixture: an empty or misread protocol set would pass every
    parametrized test below. Deliberately not pinned to today's five, so a
    change to which protocols are selectable is decided where that set lives."""
    assert len(_VALIDATED_PROTOCOLS) >= 5
    assert set(_VALIDATED_PROTOCOLS) <= set(_CIRS_DISPATCHERS)


def test_the_scan_sees_the_handlers():
    """Guard the inventory on real source: an empty scan would pass every test
    below. void_alert also proves the walk follows calls — the entry point reads
    only ``action`` and hands ``arguments`` to a per-action helper."""
    read = _keys_read(_CIRS_DISPATCHERS["void_alert"])
    assert {"since_hours", "filter_agent_id", "limit", "severity"} <= set(read)
    assert read["limit"] == ["50"]
    assert read["since_hours"] == ["1.0"]


def test_the_scan_is_per_handler_not_per_module():
    """resonance.py holds two handlers whose keys differ: stability_restored
    emits and never queries. A module-wide scan attributed resonance_alert's
    query window to it. Neither is selectable today, but the inventory has to
    be right the day either is."""
    assert "max_age_minutes" in _keys_read(_CIRS_DISPATCHERS["resonance_alert"])
    assert "max_age_minutes" not in _keys_read(_CIRS_DISPATCHERS["stability_restored"])
    assert "tau_settled" in _keys_read(_CIRS_DISPATCHERS["stability_restored"])


_EVERY_READ_FORM = '''
def handle(arguments):
    a = arguments.get("a", 1)
    b = arguments["b"]
    c = arguments.pop("c", None)
    d = arguments.setdefault("d", 2)
    if "e" in arguments:
        pass
    agent_id, error = require_registered_agent(arguments)
    return _helper(arguments)


def _helper(args):
    return args.get("f")
'''


def test_the_scan_sees_every_read_form_and_follows_a_renamed_helper():
    read = _keys_read_source(_EVERY_READ_FORM, "handle")
    assert set(read) == {"a", "b", "c", "d", "e", "f"}
    assert read["a"] == ["1"]
    assert read["b"] == [_SUBSCRIPT]
    assert read["e"] == [_MEMBERSHIP]
    assert read["f"] == [None]


@pytest.mark.parametrize(
    "body, refusal",
    [
        ('    key = "x"\n    return arguments.get(key)', "dynamic key"),
        ("    return arguments[key]", "dynamic key"),
        ('    args = arguments\n    return args.get("x")', "aliased"),
        ("    return other_module.helper(arguments)", "escapes to"),
        ("    return helper(**arguments)", "escapes as a keyword"),
        ("    return list(arguments.items())", "unrecognized use"),
    ],
    ids=["dynamic-get", "dynamic-subscript", "alias", "escape", "splat", "iteration"],
)
def test_the_scan_refuses_what_it_cannot_follow(body, refusal):
    with pytest.raises(AssertionError, match=refusal):
        _keys_read_source(f"def handle(arguments):\n{body}\n", "handle")


# --- the schema declares what the handlers read ---


@pytest.mark.parametrize("protocol", _VALIDATED_PROTOCOLS)
def test_every_key_the_handler_reads_is_on_the_wire(protocol):
    wire = set(wire_field_names(CirsProtocolParams))
    missing = sorted(set(_keys_read(_CIRS_DISPATCHERS[protocol])) - wire)
    assert not missing, (
        f"{protocol}: its handler reads {missing}, which cirs_protocol's wire "
        "schema does not declare, so FastMCP drops them before dispatch on /mcp/"
    )


@pytest.mark.parametrize("protocol", _VALIDATED_PROTOCOLS)
def test_schema_defaults_mirror_the_handler_defaults(protocol):
    """The middleware materializes every declared field, so the schema default
    is the value the handler gets for an omitted parameter and must be the
    handler's own. A handler default of '' or none at all pairs with a None
    schema default, and the handler is then held to reading None safely
    (test_emit_and_initiate_tolerate_the_none_the_middleware_materializes, and
    test_an_action_less_call_reaches_the_protocols_own_refusal for `action`)."""
    fields = CirsProtocolParams.model_fields
    for key, defaults in _keys_read(_CIRS_DISPATCHERS[protocol]).items():
        if key in COMMON_ROUTER_FIELDS or key == "protocol" or key not in fields:
            continue
        schema_default = fields[key].default
        for source_default in defaults:
            if source_default in (_SUBSCRIPT, _MEMBERSHIP):
                continue
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
    actually hands over. Before #2183 every key here except ``protocol``,
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
    outcome = await validate_params(
        "cirs_protocol", {"protocol": protocol, "action": "query"}, SimpleNamespace()
    )
    assert isinstance(outcome, tuple), parse_result(outcome)  # a list is a refusal
    _, validated, _ = outcome
    assert validated["limit"] == 50
    body = parse_result(await handle_cirs_protocol.__wrapped__(validated))
    assert body["success"] is True, body
    assert body["filters_applied"]["limit"] == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", _VALIDATED_PROTOCOLS)
async def test_an_action_less_call_reaches_the_protocols_own_refusal(protocol):
    """``action`` is declared with no default, so the middleware hands an
    omitted action over as None. Every selectable handler read it as
    ``arguments.get("action", "").lower()``, which raised AttributeError, so the
    caller got a generic tool error instead of the protocol's valid_actions. It
    predates #2183 (``action`` was already declared) and was found in its review."""
    body = await _through_dispatch_validation({"protocol": protocol})
    assert body["success"] is False
    assert "NoneType" not in (body.get("error") or "")
    assert (body.get("recovery") or {}).get("valid_actions"), body


@pytest.mark.asyncio
async def test_dispatch_validation_carries_explicit_filters_to_the_handler():
    body = await _through_dispatch_validation(
        {"protocol": "void_alert", "action": "query", "since_hours": 5, "filter_severity": "critical"}
    )
    assert body["success"] is True, body
    assert body["summary"]["query_window_hours"] == 5.0
    assert body["filters_applied"]["severity"] == "critical"

    # Omitted: the handler's defaults, and no None where the handler expects a value.
    body = await _through_dispatch_validation({"protocol": "void_alert", "action": "query"})
    assert body["filters_applied"] == {
        "agent_id": None, "severity": None, "since_hours": 1.0, "limit": 50,
    }

    # governance_action status reaches its own lookup rather than "action_id required".
    body = await _through_dispatch_validation(
        {"protocol": "governance_action", "action": "status", "action_id": "no-such-action"}
    )
    assert body["success"] is False
    assert "not found" in body["error"]


@pytest.mark.asyncio
async def test_status_filter_is_normalized_and_an_unknown_status_is_refused(monkeypatch):
    """Statuses are stored lowercase and matched exactly. Until #2183 "PENDING"
    or a typo returned a successful empty result, which reads as "no pending
    actions"; an empty string means no filter, as it does for filter_severity."""
    from src.mcp_handlers.cirs import governance_action as governance_module
    from src.mcp_handlers.cirs.storage import (
        _governance_action_buffer,
        _store_governance_action,
    )
    from src.mcp_handlers.cirs.types import GovernanceAction, GovernanceActionType

    monkeypatch.setattr(
        governance_module, "require_registered_agent", lambda arguments: ("agent-1", None)
    )
    _governance_action_buffer.clear()
    try:
        _store_governance_action(GovernanceAction(
            action_id="pending-1",
            timestamp=datetime.now().isoformat(),
            action_type=GovernanceActionType("coordination_sync"),
            initiator_agent_id="peer",
            target_agent_id="agent-1",
            payload={},
            status="pending",
        ))

        def query(status_filter):
            return _through_dispatch_validation({
                "protocol": "governance_action", "action": "query",
                "status_filter": status_filter,
            })

        assert (await query("PENDING"))["summary"]["pending"] == 1
        refused = await query("pendng")
        assert refused["success"] is False
        assert refused["recovery"]["valid_values"] == ["pending", "accepted", "rejected"]
        assert (await query(""))["summary"]["total_actions"] == 1
    finally:
        _governance_action_buffer.clear()


@pytest.mark.asyncio
async def test_rest_callers_now_get_validated_values():
    """Before #2183 these keys were undeclared, so REST and in-process callers
    had them merged back raw: ``accept="false"`` was a truthy string that
    ACCEPTED an action, an explicit null for ``include_trajectory`` read as
    false, and ``agent_ids`` items or a ``payload`` of any type passed through
    to the handler. Declared, they are parsed and refused by the middleware
    instead. The /mcp/ transport already strips nulls in the typed wrapper, so
    there a null has always meant "omitted". Pinned so the change stays a
    decision rather than an accident."""
    outcome = await validate_params(
        "cirs_protocol",
        {"protocol": "governance_action", "action": "respond", "action_id": "x", "accept": "false"},
        SimpleNamespace(),
    )
    assert isinstance(outcome, tuple), parse_result(outcome)
    assert outcome[1]["accept"] is False

    for arguments in (
        {"protocol": "state_announce", "action": "emit", "include_trajectory": None},
        {"protocol": "state_announce", "action": "query", "agent_ids": [1, "a"]},
        {"protocol": "governance_action", "action": "initiate", "payload": "not-an-object"},
    ):
        refused = await validate_params("cirs_protocol", dict(arguments), SimpleNamespace())
        assert not isinstance(refused, tuple), arguments


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
    body = parse_result(await void_module.handle_void_alert.__wrapped__(omitted))
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
    body = parse_result(
        await governance_module.handle_governance_action.__wrapped__(omitted)
    )
    assert body["success"] is False
    assert "action_type" in body["error"] and "NoneType" not in body["error"]
