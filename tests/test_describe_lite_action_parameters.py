"""describe_tool(lite=true) names the parameters an alias's action actually uses.

Observed live on 2026-09-26: describe_tool(tool_name='update_finding',
lite=true) listed content, details, summary, discovery_type and tags. It left
out discovery_id, which the update refuses to run without, and status and
resolution_notes, which are what the tool exists to set. Lite filled five
slots in the router schema's property order, and knowledge declares the store
action's text fields first. Legacy aliases that inherit the whole router
schema fared worse: get_discovery_details' lite view listed response_mode,
query, content, details and summary, and never named discovery_id.

A router's flat wire schema can only require `action`, so "required" here has
two sources: the schema's own `required`, and what the action's handler
refuses to run without. The second is derived from the handler source (every
`require_argument(arguments, "<name>")` its code path reaches), independently
of the ACTION_REQUIRED_FIELDS declaration lite serves, and the declaration is
then held to the handlers in both directions.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import json
import textwrap
from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_handlers.decorators import get_tool_registry
from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
from src.mcp_handlers.schemas.dialectic import DialecticParams
from src.mcp_handlers.schemas.knowledge import KnowledgeParams
from src.mcp_handlers.schemas.router_actions import declared_action_fields
from src.mcp_handlers.tool_stability import (
    AGENT_WORKFLOW_ALIASES,
    _TOOL_ALIASES,
    resolve_tool_alias,
)
from src.tool_schemas import get_pydantic_schemas


def _describe(**arguments) -> dict:
    return json.loads(asyncio.run(handle_describe_tool(arguments))[0].text)


def _lite(tool_name: str, **extra) -> dict[str, str]:
    """Lite parameter name -> its rendered line."""
    payload = _describe(tool_name=tool_name, lite=True, **extra)
    assert "parameters" in payload, payload
    return {line.split(":")[0].split(" (")[0]: line for line in payload["parameters"]}


def _wire_schema(tool_name: str) -> dict:
    return _describe(tool_name=tool_name, lite=False)["tool"]["inputSchema"]


def _router_actions(router_name: str) -> dict:
    """The router's own action -> handler table, as it dispatches."""
    router = inspect.unwrap(get_tool_registry()[router_name])
    actions = inspect.getclosurevars(router).nonlocals.get("actions")
    assert actions, f"{router_name} exposes no action table"
    return actions


def _action_handler(alias_name: str):
    tool_name, alias = resolve_tool_alias(alias_name)
    if alias is not None and alias.inject_action:
        return _router_actions(tool_name)[alias.inject_action]
    return inspect.unwrap(get_tool_registry()[tool_name])


def _required_by_source(handler, *, stop_at=()) -> set[str]:
    """Every parameter a `require_argument` on the handler's code path names.

    Follows calls into functions of the handler's own module. It does not
    follow into `stop_at`: a handler that hands off to ANOTHER action's
    handler (knowledge get -> details when discovery_id is present) is
    routing, and the other handler's requirements are that action's.
    """
    module = importlib.import_module(handler.__module__)
    stop = {inspect.unwrap(fn) for fn in stop_at} - {inspect.unwrap(handler)}
    pending, seen, required = [inspect.unwrap(handler)], set(), set()
    while pending:
        fn = pending.pop()
        if fn in seen:
            continue
        seen.add(fn)
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if (
                node.func.id == "require_argument"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
            ):
                required.add(node.args[1].value)
            helper = getattr(module, node.func.id, None)
            if inspect.isfunction(helper):
                helper = inspect.unwrap(helper)
                if helper.__module__ == module.__name__ and helper not in stop:
                    pending.append(helper)
    return required


def _read_by_source(handler, *, stop_at=()) -> set[str]:
    """Every argument name the handler's code path reads.

    Same traversal as `_required_by_source`; counts `arguments.get("x")`,
    `arguments.pop("x")`, `arguments["x"]` and `require_argument(arguments, "x")`.
    """
    module = importlib.import_module(handler.__module__)
    stop = {inspect.unwrap(fn) for fn in stop_at} - {inspect.unwrap(handler)}
    pending, seen, reads = [inspect.unwrap(handler)], set(), set()
    while pending:
        fn = pending.pop()
        if fn in seen:
            continue
        seen.add(fn)
        for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(fn)))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                helper = getattr(module, node.func.id, None)
                if inspect.isfunction(helper):
                    helper = inspect.unwrap(helper)
                    if helper.__module__ == module.__name__ and helper not in stop:
                        pending.append(helper)
                if (
                    node.func.id == "require_argument"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                ):
                    reads.add(node.args[1].value)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"get", "pop", "setdefault"}
                and ast.unparse(node.func.value).split(".")[-1] == "arguments"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                reads.add(node.args[0].value)
            elif (
                isinstance(node, ast.Subscript)
                and ast.unparse(node.value).split(".")[-1] == "arguments"
                and isinstance(node.slice, ast.Constant)
            ):
                reads.add(node.slice.value)
    return reads


def test_the_source_derivation_finds_the_known_requirements():
    """Guard the fixture: an empty derivation passes every check below."""
    assert _required_by_source(_action_handler("update_finding")) == {"discovery_id"}
    assert _required_by_source(_action_handler("store_finding")) == {"summary"}


# ---------------------------------------------------------------------------
# Every advertised alias
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alias_name", AGENT_WORKFLOW_ALIASES)
def test_lite_view_names_every_call_time_required_parameter(alias_name):
    router_handlers = (
        _router_actions(resolve_tool_alias(alias_name)[0]).values()
        if resolve_tool_alias(alias_name)[1].inject_action
        else ()
    )
    required = set(_wire_schema(alias_name).get("required") or ())
    required |= _required_by_source(
        _action_handler(alias_name), stop_at=router_handlers
    )

    lite = _lite(alias_name)
    missing = sorted(required - set(lite))
    assert not missing, (
        f"{alias_name}'s lite view omits {missing}, which the call cannot run "
        f"without. It shows: {list(lite.values())}"
    )
    for name in required - set(_wire_schema(alias_name).get("required") or ()):
        assert lite[name].endswith("(required at call time)"), lite[name]


@pytest.mark.parametrize("alias_name", AGENT_WORKFLOW_ALIASES)
def test_lite_view_names_only_what_the_alias_wire_carries(alias_name):
    """A lite line for a parameter the wire drops sends a caller into a no-op."""
    properties = set(_wire_schema(alias_name).get("properties") or ())
    extra = sorted(set(_lite(alias_name)) - properties)
    assert not extra, f"{alias_name}'s lite view names {extra}, absent from its wire"


def test_update_finding_lite_view_names_what_the_tool_exists_for():
    lite = _lite("update_finding")
    assert lite["discovery_id"] == "discovery_id (required at call time)"
    assert {"status", "resolution_notes"} <= set(lite)


# ---------------------------------------------------------------------------
# Every alias that pins a router action, advertised or not
# ---------------------------------------------------------------------------


def _action_pinning_aliases():
    models = get_pydantic_schemas()
    out = []
    for name, alias in sorted(_TOOL_ALIASES.items()):
        if not alias.inject_action:
            continue
        model = models.get(alias.new_name)
        declared = declared_action_fields(model) if model is not None else None
        if declared and alias.inject_action in declared:
            out.append((name, alias.new_name, alias.inject_action, model))
    return out


PINNED = _action_pinning_aliases()


def test_the_survey_found_the_legacy_aliases():
    names = {name for name, *_ in PINNED}
    assert {"update_finding", "get_discovery_details", "store_knowledge_graph"} <= names


@pytest.mark.parametrize(
    "alias_name,router,action,model", PINNED, ids=[p[0] for p in PINNED]
)
def test_pinned_alias_lite_view_stays_inside_its_action(
    alias_name, router, action, model
):
    """An alias that inherits the whole router schema must not list another
    action's parameters in its short view, and must list its own required ones.

    A keep-listed alias may deliberately carry more than its action: request_review
    takes the thesis fields so one call can reach a verdict. Its keep-list is its
    surface.
    """
    from src.alias_schema import ALIAS_SCHEMA_KEEP

    lite = set(_lite(alias_name))
    wire_required = set(_wire_schema(alias_name).get("required") or ())
    own = set(declared_action_fields(model)[action]) | set(
        ALIAS_SCHEMA_KEEP.get(alias_name, ())
    )
    stray = sorted(lite - own - wire_required)
    assert not stray, f"{alias_name} ({router} action={action}) lists {stray}"

    needed = _required_by_source(
        _router_actions(router)[action],
        stop_at=_router_actions(router).values(),
    )
    missing = sorted(needed - lite)
    assert not missing, f"{alias_name} omits call-time required {missing}"


def _router_models():
    """Every router model that declares ACTION_FIELDS, with its tool name."""
    return sorted(
        (name, model)
        for name, model in get_pydantic_schemas().items()
        if declared_action_fields(model)
    )


ROUTERS = _router_models()
_NARROWED = [
    (name, action, model)
    for name, model in ROUTERS
    for action in sorted(declared_action_fields(model))
]


def _selector_default(model):
    """The router's default action, or None where `action` is required."""
    field = model.model_fields["action"]
    return None if field.is_required() else field.default


def test_the_survey_found_the_routers_with_a_default_action():
    """Guard the fixture: the selector check below matters where a default exists."""
    defaults = {name for name, model in ROUTERS if _selector_default(model)}
    assert defaults == {"self_recovery", "dialectic", "calibration", "config", "export"}


@pytest.mark.parametrize(
    "router,action,model", _NARROWED, ids=[f"{n}-{a}" for n, a, _ in _NARROWED]
)
def test_narrowed_lite_view_says_to_pass_the_action(router, action, model):
    """describe_tool(tool_name=router, action=X, lite=true) is read by a caller
    who fills in the listed parameters. The action fields never include the
    selector, so on a router whose `action` has a default the view named
    neither it nor the value: self_recovery(action='quick') came back as
    `reason` alone, and self_recovery(reason='...') runs the default 'check'.
    """
    lite = _lite(router, action=action)
    assert "action" in lite, (
        f"{router}(action={action!r}) lite view omits the selector: "
        f"{list(lite.values())}"
    )
    assert list(lite)[0] == "action", list(lite.values())
    default = _selector_default(model)
    if default is not None and default != action:
        assert f"'{action}'" in lite["action"], lite["action"]


def test_router_lite_view_narrowed_to_an_action_leads_with_its_requirements():
    lite = _lite("knowledge", action="update")
    names = list(lite)
    assert names[:2] == ["action", "discovery_id"], names
    assert lite["discovery_id"] == "discovery_id (required at call time)"
    assert {"status", "resolution_notes", "closure_class", "closure_evidence"} <= set(
        lite
    )


# ---------------------------------------------------------------------------
# ACTION_REQUIRED_FIELDS is held to the handlers, both directions
# ---------------------------------------------------------------------------


# self_recovery dispatches by hand rather than through action_router, so it
# has no action table to walk; test_router_action_fields.py holds its
# declaration to its known_actions.
_NO_ACTION_TABLE = {"self_recovery"}
_WALKED = [(name, model) for name, model in ROUTERS if name not in _NO_ACTION_TABLE]


def test_every_router_but_the_listed_ones_has_an_action_table_to_walk():
    """A router added without an action_router table must be listed, not skipped."""
    unwalkable = set()
    for name, _model in ROUTERS:
        try:
            _router_actions(name)
        except AssertionError:
            unwalkable.add(name)
    assert unwalkable == _NO_ACTION_TABLE


@pytest.mark.parametrize("router,model", _WALKED, ids=[n for n, _ in _WALKED])
def test_every_action_declares_the_parameters_its_handler_reads(router, model):
    """Lite now shows only an action's declared fields, so a lagging declaration
    hides a working parameter. knowledge update's summary, discovery_type and
    tags, observe anomalies' and aggregate's agent_ids, and dialectic quick's
    issue_description were read by their handlers and missing from it.

    The walk follows helpers in the handler's own module only: a read made
    through another module (observe bridge hands its arguments to
    src/bridge_events.py) is not seen here.
    """
    from src.mcp_handlers.schemas.router_actions import (
        COMMON_ROUTER_FIELDS,
        wire_field_names,
    )

    handlers = _router_actions(router)
    fields = set(wire_field_names(model))
    declared = declared_action_fields(model)
    for action, handler in sorted(handlers.items()):
        reads = _read_by_source(handler, stop_at=handlers.values()) & fields
        undeclared = sorted(reads - set(declared[action]) - COMMON_ROUTER_FIELDS)
        assert not undeclared, (
            f"{router} action={action} reads {undeclared}, which its "
            "ACTION_FIELDS entry does not declare"
        )


@pytest.mark.parametrize("router,model", _WALKED, ids=[n for n, _ in _WALKED])
def test_declaration_names_every_requirement_its_handlers_reach(router, model):
    handlers = _router_actions(router)
    declared = getattr(model, "ACTION_REQUIRED_FIELDS", {})
    for action, handler in sorted(handlers.items()):
        found = _required_by_source(handler, stop_at=handlers.values())
        missing = sorted(found - set(declared.get(action, ())))
        assert not missing, (
            f"{router} action={action} requires {missing} (require_argument in "
            "its handler) but ACTION_REQUIRED_FIELDS does not declare it"
        )


_DECLARING = [
    (name, model)
    for name, model in ROUTERS
    if getattr(model, "ACTION_REQUIRED_FIELDS", None)
]


def test_the_survey_found_every_requirement_declaration():
    assert {name for name, _ in _DECLARING} == {"knowledge", "dialectic"}


@pytest.mark.parametrize("router,model", _DECLARING, ids=[n for n, _ in _DECLARING])
def test_declared_requirements_are_real_parameters_of_their_action(router, model):
    own = declared_action_fields(model)
    for action, names in model.ACTION_REQUIRED_FIELDS.items():
        assert action in own, f"{action} is not a {router} action"
        stray = sorted(set(names) - set(own[action]))
        assert not stray, (
            f"{router} action={action} requires {stray}, not among its ACTION_FIELDS"
        )


_DECLARED_ANYWHERE = [
    (router, action, name)
    for router, model in _DECLARING
    for action, names in sorted(model.ACTION_REQUIRED_FIELDS.items())
    for name in names
]


@pytest.mark.parametrize(
    "router,action,name",
    _DECLARED_ANYWHERE,
    ids=[f"{r}-{a}-{n}" for r, a, n in _DECLARED_ANYWHERE],
)
def test_narrowed_lite_view_marks_each_declared_requirement(router, action, name):
    lite = _lite(router, action=action)
    assert lite.get(name) == f"{name} (required at call time)", list(lite.values())


_DIALECTIC_QUICK_PROBE = {
    "issue_description": "ship the lite fix",
    "position": "ship it",
    "reasoning": "the focused tests pass",
}


@pytest.mark.parametrize(
    "name", sorted(DialecticParams.ACTION_REQUIRED_FIELDS.get("quick", ()))
)
def test_quick_dialectic_refuses_a_call_without_a_declared_requirement(name):
    """Soundness for dialectic's declaration, checked like knowledge's below.

    handle_quick_dialectic is a read: it triages the arguments it is given and
    touches no session state, so it runs unwrapped and unpatched.
    """
    quick = inspect.unwrap(_router_actions("dialectic")["quick"])

    def run(arguments):
        return json.loads(asyncio.run(quick(arguments))[0].text)

    answered = run(dict(_DIALECTIC_QUICK_PROBE))
    assert answered.get("success") is not False, answered

    refused = run({k: v for k, v in _DIALECTIC_QUICK_PROBE.items() if k != name})
    assert refused.get("success") is False, refused
    assert name in refused.get("error", ""), refused


# One plausible value per declared parameter, so each probe removes exactly one.
_PROBE_VALUES = {
    "summary": "a probe summary",
    "discovery_id": "2026-09-26T00:00:00.000000+00:00",
    "supersedes_id": "2026-09-25T00:00:00.000000+00:00",
    "evidence_ids": ["2026-09-24T00:00:00.000000+00:00"],
    "verification_basis": "probe",
    "decision_standard": "probe",
}

_DECLARED = sorted(
    (action, name)
    for action, names in KnowledgeParams.ACTION_REQUIRED_FIELDS.items()
    for name in names
)


@pytest.mark.parametrize(
    "action,name", _DECLARED, ids=[f"{a}-{n}" for a, n in _DECLARED]
)
def test_each_declared_requirement_is_refused_when_missing(action, name):
    """Soundness: the handler refuses the call before it reaches the graph."""
    from src.mcp_handlers.consolidated import handle_knowledge

    arguments = {"action": action, "discovery_type": "note"}
    for other in KnowledgeParams.ACTION_REQUIRED_FIELDS[action]:
        if other != name:
            arguments[other] = _PROBE_VALUES[other]

    graph = AsyncMock(side_effect=AssertionError("reached the graph"))
    with (
        patch("src.mcp_handlers.context.get_context_agent_id", return_value=None),
        patch("src.mcp_handlers.knowledge.handlers.get_knowledge_graph", graph),
        patch(
            "src.mcp_handlers.knowledge.handlers.require_registered_agent",
            return_value=("probe-agent", None),
        ),
        patch("src.mcp_handlers.utils.check_agent_can_operate", return_value=None),
        patch(
            "src.mcp_handlers.support.agent_auth.verify_agent_ownership",
            return_value=True,
        ),
    ):
        result = asyncio.run(handle_knowledge(arguments))

    payload = json.loads(result[0].text)
    assert payload.get("success") is False, payload
    assert name in payload.get("error", ""), payload
    graph.assert_not_called()
