"""describe_tool(lite=true) names the parameters an alias's action actually uses.

Observed live on 2026-09-26: describe_tool(tool_name='update_finding',
lite=true) listed content, details, summary, discovery_type and tags. It left
out discovery_id, which the update refuses to run without, and status and
resolution_notes, which are what the tool exists to set. Lite filled five
slots in the router schema's property order, and knowledge declares the store
action's text fields first. Legacy aliases that inherit the whole router
schema fared worse: get_discovery_details' lite view led with query, a search
parameter, and never named discovery_id.

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


def test_every_knowledge_action_declares_the_parameters_its_handler_reads():
    """Lite now shows only an action's declared fields, so a lagging declaration
    would hide a working parameter. update's summary, discovery_type and tags
    were read by _parse_knowledge_update_request and missing from it."""
    from src.mcp_handlers.schemas.router_actions import (
        COMMON_ROUTER_FIELDS,
        wire_field_names,
    )

    handlers = _router_actions("knowledge")
    fields = set(wire_field_names(KnowledgeParams))
    declared = declared_action_fields(KnowledgeParams)
    for action, handler in sorted(handlers.items()):
        reads = _read_by_source(handler, stop_at=handlers.values()) & fields
        undeclared = sorted(reads - set(declared[action]) - COMMON_ROUTER_FIELDS)
        assert not undeclared, (
            f"knowledge action={action} reads {undeclared}, which its "
            "ACTION_FIELDS entry does not declare"
        )


def test_declaration_names_every_requirement_its_handlers_reach():
    handlers = _router_actions("knowledge")
    declared = KnowledgeParams.ACTION_REQUIRED_FIELDS
    for action, handler in sorted(handlers.items()):
        found = _required_by_source(handler, stop_at=handlers.values())
        missing = sorted(found - set(declared.get(action, ())))
        assert not missing, (
            f"knowledge action={action} requires {missing} (require_argument in "
            "its handler) but ACTION_REQUIRED_FIELDS does not declare it"
        )


def test_declared_requirements_are_real_parameters_of_their_action():
    own = declared_action_fields(KnowledgeParams)
    for action, names in KnowledgeParams.ACTION_REQUIRED_FIELDS.items():
        assert action in own, f"{action} is not a knowledge action"
        stray = sorted(set(names) - set(own[action]))
        assert not stray, (
            f"action={action} requires {stray}, not among its ACTION_FIELDS"
        )


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
