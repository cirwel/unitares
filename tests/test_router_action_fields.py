"""Every router's ACTION_FIELDS declaration is held to its own routing table.

A consolidated router advertises one flat schema for every action it routes,
because the MCP wrapper builds a tool's argument model from top-level
properties alone. `ACTION_FIELDS` on the router's parameter model is the only
machine-readable statement of which flat parameter belongs to which action,
and `describe_tool(action=...)` serves it. A hand-written map that nothing
checks is exactly the drift the tool-registry cleanup removed elsewhere, so
these tests check it against the live routing table on every run.
"""

import json

import pytest

from src.mcp_handlers.decorators import get_tool_definition, get_tool_registry
from src.mcp_handlers.schemas.router_actions import (
    COMMON_ROUTER_FIELDS,
    declared_action_fields,
    fields_for_action,
    narrow_schema_to_action,
    wire_field_names,
)
from src.tool_schemas import advertised_input_schema, get_pydantic_schemas, get_tool_definitions


def _routers():
    """(tool name, routed actions, parameter model) for every action tool."""
    models = get_pydantic_schemas()
    out = []
    for name in sorted(get_tool_registry()):
        definition = get_tool_definition(name)
        actions = getattr(definition, "known_actions", None) if definition else None
        model = models.get(name)
        if actions and model is not None:
            out.append((name, frozenset(actions), model))
    return out


ROUTERS = _routers()
ROUTER_IDS = [name for name, _, _ in ROUTERS]


def test_the_survey_found_the_routers():
    """Guard the fixture itself: a silent empty list would pass everything."""
    assert len(ROUTERS) >= 8
    for expected in ("knowledge", "dialectic", "observe", "agent"):
        assert expected in ROUTER_IDS


@pytest.mark.parametrize("name,actions,model", ROUTERS, ids=ROUTER_IDS)
def test_every_router_declares_its_action_fields(name, actions, model):
    assert declared_action_fields(model) is not None, (
        f"{name}: the parameter model declares no ACTION_FIELDS, so "
        "describe_tool cannot narrow its flat schema to one action"
    )


@pytest.mark.parametrize("name,actions,model", ROUTERS, ids=ROUTER_IDS)
def test_declared_actions_are_exactly_the_routed_actions(name, actions, model):
    """A declared action that does not route is a parameter pointing nowhere.

    This is how the `dialectic` `vote` parameter was found: two wire fields
    documented "for action=vote" against a router with no such action and no
    handler reading them.
    """
    declared = set(declared_action_fields(model))
    assert declared == set(actions), (
        f"{name}: ACTION_FIELDS declares {sorted(declared - set(actions))!r} "
        f"that do not route and omits {sorted(set(actions) - declared)!r}"
    )


@pytest.mark.parametrize("name,actions,model", ROUTERS, ids=ROUTER_IDS)
def test_every_declared_field_exists_on_the_model(name, actions, model):
    """Named in the vocabulary a caller sends: the alias, where a field has one."""
    fields = set(wire_field_names(model))
    for action, declared in declared_action_fields(model).items():
        unknown = sorted(set(declared) - fields)
        assert not unknown, f"{name}(action={action!r}) names absent fields {unknown!r}"


@pytest.mark.parametrize("name,actions,model", ROUTERS, ids=ROUTER_IDS)
def test_every_field_is_classified_or_common(name, actions, model):
    """No parameter may be silently meaningful on every action.

    An unclassified field would appear on every narrowed schema, which is the
    same "shows everything, means nothing" problem the declaration exists to
    fix. Common parameters are named once, centrally.
    """
    classified = {f for fields in declared_action_fields(model).values() for f in fields}
    unclassified = sorted(set(wire_field_names(model)) - classified - COMMON_ROUTER_FIELDS)
    assert not unclassified, (
        f"{name}: {unclassified!r} belong to no action and are not common — "
        "add each to the action(s) it serves in ACTION_FIELDS, or to "
        "COMMON_ROUTER_FIELDS if it truly applies to every action"
    )


@pytest.mark.parametrize("name,actions,model", ROUTERS, ids=ROUTER_IDS)
def test_narrowing_keeps_the_wire_schema_a_strict_subset(name, actions, model):
    """A narrowed schema never invents a parameter the wire does not carry."""
    wire = {t.name: t for t in get_tool_definitions()}[name]
    schema = advertised_input_schema(name, wire.inputSchema if hasattr(wire, "inputSchema")
                                     else wire.input_schema)
    full = set(schema.get("properties", {}))
    for action in actions:
        narrowed = narrow_schema_to_action(schema, model, action)
        assert narrowed is not None, f"{name}: {action} did not narrow"
        kept = set(narrowed["properties"])
        assert kept <= full, f"{name}({action}): invented {sorted(kept - full)!r}"
        assert "action" in kept, f"{name}({action}): dropped the action selector"
        assert narrowed["properties"]["action"].get("const") == action
        for required in narrowed.get("required", ()):
            assert required in kept


@pytest.mark.parametrize("name,actions,model", ROUTERS, ids=ROUTER_IDS)
def test_narrowing_actually_narrows_somewhere(name, actions, model):
    """If every action kept every field the declaration would be decoration."""
    wire = {t.name: t for t in get_tool_definitions()}[name]
    schema = advertised_input_schema(name, wire.inputSchema if hasattr(wire, "inputSchema")
                                     else wire.input_schema)
    full = len(schema.get("properties", {}))
    sizes = [len(narrow_schema_to_action(schema, model, a)["properties"]) for a in actions]
    assert min(sizes) < full, f"{name}: no action narrows the {full}-field schema"


def test_common_fields_are_identity_and_selection_only():
    """COMMON_ROUTER_FIELDS is not a parking space for unclassified names."""
    assert COMMON_ROUTER_FIELDS == frozenset({
        "action", "op", "agent_id", "client_session_id", "continuity_token",
    })


def test_unknown_action_returns_none_rather_than_an_empty_schema():
    models = get_pydantic_schemas()
    assert fields_for_action(models["knowledge"], "no_such_action") is None
    assert narrow_schema_to_action({}, models["knowledge"], "no_such_action") is None


def test_a_model_without_a_declaration_is_reported_as_such():
    """A non-router tool has no action map and must not fake one."""
    models = get_pydantic_schemas()
    assert declared_action_fields(models["health_check"]) is None
    assert fields_for_action(models["health_check"], "anything") is None


# --- describe_tool serves the per-action view ---


def _describe(**kwargs):
    import asyncio
    import json

    from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool

    return json.loads(asyncio.run(handle_describe_tool(kwargs))[0].text)


def test_describe_tool_lists_the_actions_when_none_is_asked_for():
    payload = _describe(tool_name="knowledge", lite=False)
    assert payload["actions"]["actions"] == sorted(
        get_tool_definition("knowledge").known_actions
    )
    assert "action=" in payload["actions"]["note"]


def test_describe_tool_narrows_the_schema_to_one_action():
    payload = _describe(tool_name="knowledge", action="search", lite=False)
    schema = payload["tool"]["inputSchema"]
    shown = set(schema["properties"])
    assert "query" in shown
    # Parameters of other actions are gone from the view.
    for other in ("dry_run", "topic", "supersedes_id", "closure_class"):
        assert other not in shown
    assert schema["properties"]["action"]["const"] == "search"
    view = payload["actions"]
    assert view["action"] == "search"
    assert view["parameters_shown"] == len(shown)
    assert view["parameters_shown"] < view["parameters_on_the_wire"]
    # The wire is unchanged: the narrowing is a description, not a restriction.
    assert "still accepted" in view["note"]


def test_describe_tool_rejects_an_action_the_router_does_not_route():
    payload = _describe(tool_name="knowledge", action="vote", lite=False)
    assert payload.get("success") is False
    assert "vote" in json.dumps(payload)
    assert "search" in json.dumps(payload["recovery"]["valid_actions"])


def test_describe_tool_leaves_a_non_router_alone():
    payload = _describe(tool_name="health_check", lite=False)
    assert "actions" not in payload


def test_describe_tool_action_is_declared_on_its_own_wire_schema():
    """A parameter the handler reads must be declared, or FastMCP drops it."""
    schema = {t.name: t for t in get_tool_definitions()}["describe_tool"]
    props = advertised_input_schema(
        "describe_tool",
        schema.inputSchema if hasattr(schema, "inputSchema") else schema.input_schema,
    )["properties"]
    assert "action" in props

