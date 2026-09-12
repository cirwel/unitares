"""The /mcp/ mount advertises the catalog schema, constraint for constraint.

Finding F12 of docs/operations/tool-surface-audit-2026-09-12.md. FastMCP derives
a tool's schema from the typed wrapper's signature, and the derivation is lossy.
At be117c2 the mount advertised 106 non-null catalog defaults as ``null``,
dropped twelve bounds (``consult.brief`` length 1–32000,
``delegate_inference.timeout_s`` 5–420, ``dashboard.limit`` 1–100, ...) and the
``items`` / ``additionalProperties`` of nine more properties, and lost the
``$defs`` blocks of the four tools with nested models — while stdio and REST,
which serve the catalog, advertised all of it. A registry indexing ``/mcp/``
therefore graded a different parameter contract than one indexing stdio.

``src/tool_registration.py`` now replaces the registered tool's ``parameters``
with the catalog schema after registration (``_advertise_catalog_schema``).
These tests pin the parity per tool and per property, in every field-description
and title mode, and pin the part that must NOT move with it: what FastMCP
accepts before dispatch is decided by the argument model built from the wrapper
signature, exactly as before, and the advertised bounds are enforced by the
handler's Pydantic model.

Pure registry + data tests; no DB or network.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

import src.mcp_handlers  # noqa: F401  (settles the decorator registry)
from src.mcp_compat import FastMCP, InternalTool, get_tool_input_schema

pytestmark = pytest.mark.usefixtures("first_party_tool_surface")

# The keywords F12 found missing, plus the rest of the JSON Schema validation
# vocabulary the catalog uses. ``default`` is checked separately: a catalog
# default of ``null`` and a missing default advertise the same thing.
CONSTRAINT_KEYWORDS = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minItems",
    "maxItems",
    "enum",
    "items",
    "additionalProperties",
)

# The tools whose catalog schema carries a ``$defs`` block (nested models).
NESTED_MODEL_TOOLS = ("onboard", "process_agent_update", "start_session", "sync_state")


def _catalog() -> dict[str, dict[str, Any]]:
    from src.interface_contract import get_public_tool_definitions

    return {
        tool.name: get_tool_input_schema(tool, {}) or {}
        for tool in get_public_tool_definitions("full")
    }


def _fresh_mount() -> dict[str, dict[str, Any]]:
    """Register onto a fresh server through the production registrars and list.

    Same construction as ``scripts/dev/tool_edge_index.py::_collect_wire_catalog``
    and ``mode_filtered_server_class.list_tools`` in src/tool_mode_listing.py:
    both registrars, then the listing policy the mounted server applies.
    """
    from src.tool_mode_listing import apply_listed_schema_policy
    from src.tool_registration import _register_common_aliases, auto_register_all_tools

    mcp = FastMCP(name="schema-parity-probe")
    auto_register_all_tools(mcp)
    _register_common_aliases(mcp)
    listed = apply_listed_schema_policy(asyncio.run(mcp.list_tools()))
    return {tool.name: get_tool_input_schema(tool, {}) or {} for tool in listed}


def _constraint_and_default_gaps(
    catalog: dict[str, dict[str, Any]], mount: dict[str, dict[str, Any]]
) -> list[str]:
    """Every constraint keyword and non-null default the mount does not repeat.

    One line per gap, ``tool.property.keyword: catalog X, mount Y``, so a
    failure names what a /mcp/ client would be missing rather than dumping two
    fifty-tool catalogs.
    """
    gaps: list[str] = []
    for name in sorted(catalog):
        catalog_properties = catalog[name].get("properties") or {}
        mount_properties = mount.get(name, {}).get("properties") or {}
        for prop, catalog_def in catalog_properties.items():
            mount_def = mount_properties.get(prop)
            if not isinstance(mount_def, dict):
                gaps.append(f"{name}.{prop}: not advertised on the mount")
                continue
            for keyword in CONSTRAINT_KEYWORDS:
                if keyword in catalog_def and catalog_def[keyword] != mount_def.get(keyword):
                    gaps.append(
                        f"{name}.{prop}.{keyword}: catalog {catalog_def[keyword]!r}, "
                        f"mount {mount_def.get(keyword)!r}"
                    )
            default = catalog_def.get("default")
            if default is not None and default != mount_def.get("default"):
                gaps.append(
                    f"{name}.{prop}.default: catalog {default!r}, "
                    f"mount {mount_def.get('default')!r}"
                )
    return gaps


def _differing_properties(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    a_props = a.get("properties") or {}
    b_props = b.get("properties") or {}
    names = sorted(set(a_props) | set(b_props))
    differing = [name for name in names if a_props.get(name) != b_props.get(name)]
    top_level = sorted(
        key for key in set(a) | set(b) if key != "properties" and a.get(key) != b.get(key)
    )
    return differing + [f"<{key}>" for key in top_level]


def test_mount_advertises_every_catalog_constraint_and_default():
    """The per-tool, per-property diff F12 asked for. Empty means parity."""
    catalog = _catalog()
    mount = _fresh_mount()

    assert set(mount) == set(catalog), (
        f"mount-only: {sorted(set(mount) - set(catalog))}; "
        f"catalog-only: {sorted(set(catalog) - set(mount))}"
    )
    gaps = _constraint_and_default_gaps(catalog, mount)
    assert not gaps, (
        "constraints or defaults the catalog advertises and /mcp/ does not "
        f"({len(gaps)}):\n  " + "\n  ".join(gaps)
    )
    for name in NESTED_MODEL_TOOLS:
        assert "$defs" in catalog[name], f"{name} lost its nested models in the catalog"
        assert mount[name].get("$defs") == catalog[name]["$defs"], (
            f"{name}: the mount does not advertise the catalog's $defs"
        )


def test_the_diff_is_not_vacuous_against_fastmcp_regeneration():
    """Negative control: the schema FastMCP derives on its own fails the diff.

    Builds a Tool the way FastMCP would without the registrar's replacement,
    for the two tools F12 quoted, and checks that the gap list names exactly
    the bounds and defaults the audit found missing. If FastMCP ever starts
    carrying these through on its own, this test — not the parity test — is
    the one that should be revisited.
    """
    from src.mcp_handlers.support.wrapper_generator import create_typed_wrapper

    catalog = _catalog()
    regenerated: dict[str, dict[str, Any]] = {}
    for name in ("delegate_inference", "dashboard"):
        wrapper = create_typed_wrapper(name, catalog[name], lambda tool_name: None)
        regenerated[name] = InternalTool.from_function(
            wrapper, structured_output=False
        ).parameters

    gaps = _constraint_and_default_gaps(
        {name: catalog[name] for name in regenerated}, regenerated
    )
    assert "delegate_inference.timeout_s.minimum: catalog 5, mount None" in gaps
    assert "delegate_inference.timeout_s.maximum: catalog 420, mount None" in gaps
    assert "dashboard.limit.maximum: catalog 100, mount None" in gaps
    assert any(gap.startswith("delegate_inference.timeout_s.default:") for gap in gaps)


@pytest.mark.parametrize("field_descriptions", ["brief", "full", "off"])
@pytest.mark.parametrize("property_titles", ["strip", "keep"])
def test_mount_listing_is_the_catalog_schema_verbatim(
    monkeypatch, field_descriptions, property_titles
):
    """Whole-schema equality, in every advertised-text mode.

    The constraint diff above is the readable check; this is the strong one.
    Both surfaces read the two env switches at call time — the catalog when it
    is built, the mount when it registers (descriptions) and when it lists
    (titles) — so parity has to hold under every combination, not only the
    default. The alias property overrides were the one place this could break
    before 2026-09-11: the catalog applied them in the default mode while the
    registrar re-applied them in the resolved mode.
    """
    monkeypatch.setenv("UNITARES_TOOL_SCHEMA_FIELD_DESCRIPTIONS", field_descriptions)
    monkeypatch.setenv("UNITARES_TOOL_SCHEMA_PROPERTY_TITLES", property_titles)

    catalog = _catalog()
    mount = _fresh_mount()

    # An empty catalog would satisfy every assertion below by having nothing to
    # compare. The sibling tests pin the name sets, so a mount-only tool is
    # already caught there; this closes the one vacuity that is not.
    assert catalog, "no catalog tools were built; the comparison would be vacuous"

    differing = {
        name: _differing_properties(catalog[name], mount.get(name, {}))
        for name in sorted(catalog)
        if mount.get(name) != catalog[name]
    }
    assert not differing, (
        f"/mcp/ listing differs from the catalog for {len(differing)} tool(s) "
        f"under field_descriptions={field_descriptions}, "
        f"property_titles={property_titles}: {differing}"
    )


@pytest.mark.asyncio
async def test_live_mount_advertises_the_catalog(monkeypatch):
    """The deployed server object, not a rebuilt one, in the default modes."""
    from src import mcp_server

    monkeypatch.setattr("src.tool_modes.TOOL_MODE", "full")
    monkeypatch.delenv("UNITARES_TOOL_SCHEMA_PROPERTY_TITLES", raising=False)
    listed = {
        tool.name: get_tool_input_schema(tool, {}) or {}
        for tool in await mcp_server.mcp.list_tools()
    }
    catalog = _catalog()

    assert set(listed) == set(catalog)
    gaps = _constraint_and_default_gaps(catalog, listed)
    assert not gaps, "\n  ".join(gaps)
    differing = [name for name in sorted(catalog) if listed[name] != catalog[name]]
    assert not differing, differing


def test_advertised_bounds_are_enforced_by_dispatch_not_by_the_transport():
    """The stated consequence of replacing the schema: validation did not move.

    The catalog says ``delegate_inference.timeout_s`` is 5–420 and ``/mcp/``
    now says so too. What accepts or rejects the value is what it always was:
    FastMCP's argument model, built from the wrapper signature, lets it through
    to dispatch, and the handler's Pydantic model (``validate_params``) rejects
    it with the tool's own error envelope rather than FastMCP's. Option 1 of
    F12 — carrying the bounds into the wrapper's ``Annotated`` metadata — would
    have moved that boundary; this pins that it did not.
    """
    from src import mcp_server
    from src.tool_schemas import get_pydantic_schemas

    tool = mcp_server.mcp._tool_manager.get_tool("delegate_inference")
    assert tool.parameters["properties"]["timeout_s"]["maximum"] == 420

    accepted = tool.fn_metadata.arg_model.model_validate(
        {"prompt": "probe", "timeout_s": 9999}
    )
    assert accepted.model_dump_one_level()["timeout_s"] == 9999

    params_model = get_pydantic_schemas()["delegate_inference"]
    with pytest.raises(ValidationError):
        params_model.model_validate({"prompt": "probe", "timeout_s": 9999})


def test_extra_argument_passthrough_survives_the_schema_replacement():
    """``EXTRA_ARGUMENT_PASSTHROUGH_TOOLS`` lives on the argument model, not the schema.

    ``sync_state`` must still let internal harness fields through FastMCP's
    validation while advertising none of them — the advertised schema and the
    accepted arguments are deliberately different objects.
    """
    from src import mcp_server

    tool = mcp_server.mcp._tool_manager.get_tool("sync_state")
    assert tool.fn_metadata.arg_model.model_config.get("extra") == "allow"
    accepted = tool.fn_metadata.arg_model.model_validate(
        {"response_text": "probe", "harness_type": "r6_dogfood"}
    ).model_dump_one_level()
    assert accepted["harness_type"] == "r6_dogfood"
    assert "harness_type" not in tool.parameters["properties"]
    assert tool.parameters["properties"]["complexity"]["default"] == 0.5


# ---------------------------------------------------------------------------
# advertise-subset-of-accept
# ---------------------------------------------------------------------------
# Replacing the advertised schema made a tool's contract two objects rather than
# one: the catalog schema says what a caller is told, and the wrapper's argument
# model decides what the transport accepts. They are built from the same Pydantic
# model, so they agree today, but nothing was checking that they still do — the
# suite pinned exactly one instance of it (delegate_inference.timeout_s). The
# direction that matters is advertise-narrow / accept-wide: a value the schema
# calls legal must reach dispatch. The reverse is fine and deliberate, because
# the wrapper widens types for coercion.
#
# Every value is taken at a BOUNDARY the schema names, not in the middle of the
# range. The first draft of this test synthesized only the minimum, and a probe
# showed what that misses: advertising `maximum: 99999` on a parameter the
# handler caps at 420 passed, because the value tried was 5. An over-generous
# advertised ceiling is the likelier drift of the two, so the ceiling has to be
# one of the values tried.
#
# Measured 2026-09-12 on the full surface: 507 properties across 50 tools,
# 862 boundary values, none unsynthesizable, none rejected by the transport,
# and six rejected by a handler for the one recorded catalog imprecision below.

_UNSYNTHESIZABLE = object()

#: Cap on values tried per property, so a wide ``anyOf`` of enums cannot turn
#: this into a combinatorial walk. Generous next to the widest real property.
_MAX_CANDIDATES = 8

# Advertised string branches the handler is stricter than. These parameters take
# a number in [0, 1], a named level ("trivial" ... "very_high"), or a
# {"value": N, "scale": M} object, and the handler's own validator says so — but
# the advertised schema carries a bare `string` branch with no enum, so it tells
# a caller that any string is legal and dispatch then refuses "x".
#
# This is a real imprecision in the CATALOG, not something the schema swap
# introduced: stdio and REST have advertised the same bare branch all along,
# and docs/interface-contract.v1.json hashes it. Narrowing it is a contract
# change that moves every input_schema_sha256 and needs an interface release, so
# it is a decision rather than a fix to fold into a test. Recorded here, keyed
# on the canonical tool, so the exception is named and counted instead of
# silently passing; only STRING values are exempted, so a regression on the
# numeric branch of these same parameters still fails.
_ADVERTISED_STRING_WIDER_THAN_HANDLER = frozenset({
    ("process_agent_update", "complexity"),
    ("process_agent_update", "confidence"),
    ("simulate_update", "complexity"),
    ("simulate_update", "confidence"),
})


def _resolve_ref(schema: Any, defs: dict) -> Any:
    """Follow ``$ref`` into ``$defs``; ``None`` for a ref this file cannot follow."""
    for _ in range(10):
        if not (isinstance(schema, dict) and "$ref" in schema):
            return schema
        ref = schema["$ref"]
        if not ref.startswith("#/$defs/"):
            return None
        schema = defs.get(ref.rsplit("/", 1)[-1])
    return schema


def _candidate_values(schema: Any, defs: dict, depth: int = 0) -> list:
    """Every boundary value the advertised ``schema`` calls legal.

    Boundaries rather than arbitrary values, because a bound and an accepting
    model disagree at the edges or nowhere: the shortest AND longest permitted
    string, the lowest AND highest permitted number, every enum member rather
    than the first. Returns a list so one lenient value cannot mask a strict
    one — the failure this replaced was exactly that.

    An empty list means the helper does not model the construct (a ``pattern``,
    a ``format``, an unresolvable ref). That is a gap in the helper, not a
    failure of the invariant, and the test reports the two separately.
    """
    schema = _resolve_ref(schema, defs)
    if not isinstance(schema, dict) or depth > 6:
        return []
    if "const" in schema:
        return [schema["const"]]
    if schema.get("enum"):
        # Every member: a bogus one added anywhere in the list must be caught,
        # not only when it happens to be first.
        return list(schema["enum"])[:_MAX_CANDIDATES]

    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            values: list = []
            for branch in schema[keyword]:
                if isinstance(branch, dict) and branch.get("type") == "null":
                    continue
                values.extend(_candidate_values(branch, defs, depth + 1))
            return values[:_MAX_CANDIDATES]

    json_type = schema.get("type")
    if isinstance(json_type, list):
        json_type = next((t for t in json_type if t != "null"), None)

    if json_type == "string":
        if "pattern" in schema or schema.get("format"):
            return []
        shortest = max(1, int(schema.get("minLength") or 1))
        lengths = {shortest}
        longest = schema.get("maxLength")
        if isinstance(longest, int) and longest >= shortest:
            lengths.add(longest)
        return ["x" * length for length in sorted(lengths)]
    if json_type in ("integer", "number"):
        cast = int if json_type == "integer" else float
        bounds = set()
        lower = schema.get("minimum", schema.get("exclusiveMinimum"))
        if isinstance(lower, (int, float)):
            bounds.add(lower + 1 if "exclusiveMinimum" in schema
                       and schema.get("minimum") is None else lower)
        upper = schema.get("maximum", schema.get("exclusiveMaximum"))
        if isinstance(upper, (int, float)):
            bounds.add(upper - 1 if "exclusiveMaximum" in schema
                       and schema.get("maximum") is None else upper)
        return [cast(bound) for bound in sorted(bounds)] or [cast(1)]
    if json_type == "boolean":
        return [True, False]
    if json_type == "array":
        values = []
        if int(schema.get("minItems") or 0) == 0:
            values.append([])
        items = _candidate_values(schema.get("items") or {"type": "string"}, defs, depth + 1)
        if items:
            values.append([items[0]])
        return values
    if json_type == "object":
        instance: dict[str, Any] = {}
        properties = schema.get("properties") or {}
        for name in schema.get("required") or []:
            nested = _candidate_values(properties.get(name) or {}, defs, depth + 1)
            if not nested:
                return []
            instance[name] = nested[0]
        return [instance]
    if json_type == "null":
        return [None]
    return []


def _synthesize(schema: Any, defs: dict, depth: int = 0) -> Any:
    """One legal value, for building the base instance a tool's required fields need."""
    values = _candidate_values(schema, defs, depth)
    return values[0] if values else _UNSYNTHESIZABLE


def test_every_advertised_value_survives_both_validation_boundaries():
    """A value the mount calls legal must reach the handler, through both gates.

    There are two, and they refuse different things, so the test reports them
    separately because the fixes differ:

    - ``fn_metadata.arg_model``, built from the typed wrapper's signature, is
      what the transport applies before dispatch. It is deliberately WIDE (a
      boolean parameter also accepts ``"true"``, a literal is widened to
      ``str``), so a rejection here means the advertised schema is structurally
      incompatible with the wrapper — the one way the schema swap could have
      broken a caller that generated bindings from the listing.
    - the handler's own params model, which ``validate_params`` applies, is
      where the advertised bounds and enums are actually enforced. A rejection
      here means the mount advertises a value that dies one layer deeper: the
      shape a hand-authored override in ``ALIAS_SCHEMA_PROPERTY_OVERRIDES``
      could produce, since those are written by hand rather than derived.

    An action-injecting alias strips ``action`` from its advertised schema
    while the router's model requires it, so the injected action is supplied
    here exactly as dispatch supplies it. Without that, all 48 alias properties
    fail on a missing field rather than on anything this test is about.

    A property the helper cannot synthesize is counted and named rather than
    silently passed over, so this cannot decay into a test that checks nothing.
    Measured 2026-09-12: 507 properties, 862 boundary values, none
    unsynthesizable, none rejected outside the recorded exemption.
    """
    from src import mcp_server
    from src.mcp_handlers.tool_stability import resolve_tool_alias
    from src.tool_schemas import get_pydantic_schemas

    handler_models = get_pydantic_schemas()
    checked = 0
    unsynthesizable: list[str] = []
    transport_rejected: list[str] = []
    handler_rejected: list[str] = []
    exempted: list[str] = []

    for name, tool in sorted(mcp_server.mcp._tool_manager._tools.items()):
        schema = tool.parameters or {}
        defs = schema.get("$defs") or {}
        properties = schema.get("properties") or {}
        canonical, alias = resolve_tool_alias(name)
        handler_model = handler_models.get(canonical)

        # Required properties ride along on every instance, or a model would
        # reject for a missing field rather than for the property under test.
        base: dict[str, Any] = {}
        if alias is not None and alias.inject_action:
            base["action"] = alias.inject_action
        skip_tool = False
        for required in schema.get("required") or []:
            value = _synthesize(properties.get(required) or {}, defs)
            if value is _UNSYNTHESIZABLE:
                unsynthesizable.append(f"{name}.{required} (required)")
                skip_tool = True
                break
            base[required] = value
        if skip_tool:
            continue

        for prop, definition in properties.items():
            candidates = _candidate_values(definition, defs)
            if not candidates:
                unsynthesizable.append(f"{name}.{prop}")
                continue
            for value in candidates:
                instance = {**base, prop: value}
                shown = repr(value)
                if len(shown) > 60:
                    shown = f"{shown[:57]}... (len {len(value)})"
                try:
                    tool.fn_metadata.arg_model.model_validate(instance)
                except ValidationError as exc:
                    transport_rejected.append(
                        f"{name}.{prop}={shown}: {exc.errors()[:1]}"
                    )
                exempt = isinstance(value, str) and (
                    (canonical, prop) in _ADVERTISED_STRING_WIDER_THAN_HANDLER
                )
                if handler_model is not None and not exempt:
                    try:
                        handler_model.model_validate(instance)
                    except ValidationError as exc:
                        handler_rejected.append(
                            f"{name}.{prop}={shown} -> {canonical}: {exc.errors()[:1]}"
                        )
                if exempt:
                    exempted.append(f"{name}.{prop}={shown}")
                checked += 1

    assert not transport_rejected, (
        "the mount advertises values its own argument model refuses, so a client "
        "generating bindings from tools/list would build a call the transport "
        f"rejects ({len(transport_rejected)}):\n  " + "\n  ".join(transport_rejected)
    )
    assert not handler_rejected, (
        "the mount advertises values the handler's own model refuses, so a "
        "schema-conforming call reaches dispatch and fails there "
        f"({len(handler_rejected)}):\n  " + "\n  ".join(handler_rejected)
    )
    assert checked, "no advertised property was exercised; the check is vacuous"
    # The exemption is only honest while it still describes something real. If
    # the catalog is narrowed, or the handler widened, this fires and the entry
    # comes out rather than sitting there implying a guard it no longer needs.
    assert exempted, (
        "no advertised value hit _ADVERTISED_STRING_WIDER_THAN_HANDLER, so the "
        "exemption no longer describes the catalog. Delete the stale entries."
    )
    # A few unmodelled constructs are tolerable; a wave of them means the helper
    # stopped keeping up with the catalog and the invariant is going unchecked.
    assert len(unsynthesizable) <= checked // 20, (
        f"{len(unsynthesizable)} of {checked + len(unsynthesizable)} advertised "
        "properties could not be synthesized, so the invariant is largely "
        f"unchecked. Extend _synthesize for: {unsynthesizable[:10]}"
    )
