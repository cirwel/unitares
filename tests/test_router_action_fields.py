"""Every router's ACTION_FIELDS declaration is held to its own routing table.

A consolidated router advertises one flat schema for every action it routes,
because the MCP wrapper builds a tool's argument model from top-level
properties alone. `ACTION_FIELDS` on the router's parameter model is the only
machine-readable statement of which flat parameter belongs to which action,
and `describe_tool(action=...)` serves it. A hand-written map that nothing
checks is exactly the drift the tool-registry cleanup removed elsewhere, so
these tests check it against the live routing table on every run.

The advertised `action` vocabulary is held to the same declared actions here.
It is generated from a hand-written `Literal` on the parameter model, and until
2026-09-13 only two routers had it compared with their actions, against
hardcoded sets (`test_defaulted_consolidated_schemas_are_advertised` in
tests/test_tool_schema_validation.py, whose job is the default action).
"""

import json

import pytest

from src.mcp_compat import get_tool_input_schema
from src.mcp_handlers.decorators import (
    get_tool_definition,
    get_tool_registry,
    list_plugin_registered_tools,
)
from src.mcp_handlers.schemas.router_actions import (
    COMMON_ROUTER_FIELDS,
    declared_action_fields,
    fields_for_action,
    narrow_schema_to_action,
    wire_field_names,
)
from src.tool_schemas import advertised_input_schema, get_pydantic_schemas, get_tool_definitions


# Tools that declare an action vocabulary but dispatch on a SECOND selector
# first, so an action-keyed field map cannot describe any single call. For
# `cirs_protocol`, `protocol` picks the sub-handler and every flat parameter is
# scoped by protocol rather than by action, so a union keyed on `action`
# asserted call shapes the handlers refuse; see the comment on
# `CirsProtocolParams`. Exempt from the ACTION_FIELDS requirement, and held to
# actually having that second selector by
# `test_two_level_tools_really_have_a_second_selector` — this set is not a
# parking space for an ordinary router that nobody wanted to declare.
_TWO_LEVEL_TOOLS = frozenset({"cirs_protocol"})


def _routers():
    """(tool name, routed actions, parameter model) for every first-party action tool.

    Plugin and test-registered tools are left out. Whether one is in the
    registry when this module is collected depends on what was imported first
    (see `first_party_tool_surface` in tests/conftest.py), and whether a
    plugin's tools are coherent is the plugin's suite to answer.
    """
    models = get_pydantic_schemas()
    external = set(list_plugin_registered_tools())
    out = []
    for name in sorted(get_tool_registry()):
        if name in _TWO_LEVEL_TOOLS or name in external:
            continue
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


def test_the_survey_misses_no_first_party_action_tool(first_party_tool_surface):
    """A partial survey passes as quietly as an empty one.

    `_routers` keeps a tool only when it has both `known_actions` and a
    registered parameter model, so a tool whose model went missing would drop
    out of every test below, and the floor above would not notice while eight
    others remained.

    The population is what is actually served: an advertised tool declaring
    `known_actions` must be surveyed, and a tool that is not advertised needs
    no model. `hidden` is not a substitute for that test, because a hidden tool
    listed in TOOL_ORDER is still advertised.
    """
    external = set(list_plugin_registered_tools())
    declared = {
        tool.name
        for tool in get_tool_definitions()
        if tool.name not in external
        and tool.name not in _TWO_LEVEL_TOOLS
        and getattr(get_tool_definition(tool.name), "known_actions", None)
    }
    missing = sorted(declared - set(ROUTER_IDS))
    assert not missing, (
        f"{missing!r} declare known_actions but were not surveyed: each needs a "
        "registered parameter model, or a justified place in _TWO_LEVEL_TOOLS"
    )


# Keys that annotate a schema node without narrowing what it accepts.
_ANNOTATION_KEYS = frozenset({"title", "description", "default", "examples", "deprecated"})


def _closed_vocabulary(node, defs):
    """The exact values a JSON-schema node accepts when they form a closed set, else None.

    Pydantic spells a closed vocabulary as one constraint keyword plus
    annotations: `enum` for a multi-value `Literal`, `const` for a single
    value, `anyOf` with a `null` branch for an optional one, and a `$ref` into
    `$defs` for an `Enum` class. Those four are read, and `type` is read only
    as `"string"`, which keeps exactly the string members. `null` is dropped: it
    is the absence of an action, which a router resolves to its default.

    Every other shape returns None and fails the caller loudly: any other
    `type`, a constraint keyword beside another such as `enum` with `const`,
    `$ref` with its own constraints, `allOf`, or `oneOf`. Reading those means
    intersecting constraints, and a reader that guessed could report a wider
    set than the schema accepts, passing a schema that refuses a routed action.
    """
    keys = set(node) - _ANNOTATION_KEYS
    node_type = node.get("type")
    if "type" in keys:
        if node_type not in ("string", "null"):
            return None
        keys.discard("type")
    if node_type == "null":
        return set() if not keys else None
    if keys == {"$ref"} and node_type is None and node["$ref"].startswith("#/$defs/"):
        return _closed_vocabulary(defs.get(node["$ref"].rsplit("/", 1)[-1], {}), defs)
    if keys == {"anyOf"} and node_type is None:
        values = set()
        for branch in node["anyOf"]:
            branch_values = _closed_vocabulary(branch, defs)
            if branch_values is None:
                return None
            values |= branch_values
        return values
    if keys == {"enum"}:
        values = set(node["enum"])
    elif keys == {"const"}:
        values = {node["const"]}
    else:
        return None
    if node_type == "string":
        values = {value for value in values if isinstance(value, str)}
    values.discard(None)
    return values


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
def test_advertised_action_enum_is_exactly_the_routed_actions(
    name, actions, model, first_party_tool_surface
):
    """The action list a client is shown must be the list the tool declares.

    The advertised vocabulary is generated from the `Literal` on the tool's
    parameter model, and server-side parameter validation checks that same
    `Literal` before the handler runs. So an action the enum omits is refused
    at validation, a route no call through dispatch can reach, and an enum
    value the tool does not handle passes validation only to fail later.

    What the enum is compared with depends on the tool. For an `action_router`,
    `known_actions` is derived from its `actions={}` map, so this reaches the
    routing table itself. For a tool that declares `known_actions` by hand and
    dispatches on its own, currently `self_recovery`, it checks only that the
    two declarations agree; neither is checked against the dispatch branches.

    The served description names these actions too, but prose may abbreviate
    (tests/test_action_router_description_drift.py). `cirs_protocol` is not
    surveyed: it advertises `action` as a free string, so there is no closed
    vocabulary here to compare.
    """
    catalog = {tool.name: tool for tool in get_tool_definitions()}
    if name not in catalog:
        pytest.skip(f"{name} is not advertised, so no client is shown a vocabulary")
    schema = get_tool_input_schema(catalog[name])
    action = (schema.get("properties") or {}).get("action")
    assert action is not None, f"{name}: the advertised schema has no action property"
    advertised = _closed_vocabulary(action, schema.get("$defs") or {})
    assert advertised is not None, (
        f"{name}: the advertised action property is not a closed vocabulary: {action!r}"
    )
    assert advertised == set(actions), (
        f"{name}: the advertised action enum lists "
        f"{sorted(set(advertised) - set(actions))!r} that do not route and omits "
        f"{sorted(set(actions) - set(advertised))!r}"
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


@pytest.mark.parametrize("name", sorted(_TWO_LEVEL_TOOLS))
def test_two_level_tools_really_have_a_second_selector(name):
    """The exemption has to be earned, not asserted.

    A tool skips the ACTION_FIELDS requirement only because its parameters are
    scoped by a selector other than `action`. Hold it to that: the model must
    carry a required multi-valued Literal that is not `action`, and it must not
    declare ACTION_FIELDS after all — a declaration here would silently put
    describe_tool back into the narrowing mode this exemption exists to avoid.
    """
    definition = get_tool_definition(name)
    assert definition is not None, f"{name} is not registered"
    assert definition.known_actions, (
        f"{name} declares no known_actions, so it needs no exemption"
    )

    model = get_pydantic_schemas()[name]
    assert declared_action_fields(model) is None, (
        f"{name} declares ACTION_FIELDS while claiming the two-level exemption; "
        "remove one or the other"
    )

    schema = model.model_json_schema()
    required = set(schema.get("required") or ())
    selectors = [
        field
        for field in required
        if field != "action"
        and len((schema["properties"][field].get("enum") or [])) > 1
    ]
    assert selectors, (
        f"{name} has no required multi-valued selector besides `action`, so it "
        "is an ordinary router and must declare ACTION_FIELDS"
    )


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

