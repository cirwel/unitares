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
# 964 boundary values, none rejected by the transport, and six rejected by a
# handler for the recorded catalog imprecision below.
#
# WHAT THIS DOES NOT CATCH, stated so the green result is not over-read. It
# probes only the boundaries the advertised schema NAMES, so it is structurally
# blind to a bound the advertised side omits or cannot express:
#   - a handler bound with no advertised counterpart, e.g. an advertised
#     `maximum` deleted while the handler keeps it;
#   - a bound the schema emits in a form JSON Schema ignores. This is LIVE, not
#     hypothetical: the check-in `complexity`/`confidence` bound is emitted as
#     Pydantic `ge`/`le`, so 99.0 is advertised-legal and the handler refuses
#     it, and this test passes because it only ever tries 1.0 there;
#   - an optional nested field whose construct the helper does not model. This
#     is also live: `ToolResultEvidence.observed_at` (format date-time) yields no
#     candidates and is skipped without being recorded.
# Closing these needs decisions this file should not make silently; see the
# knowledge-graph record superseding 2026-09-12T18:41:44.

_UNSYNTHESIZABLE = object()

# There is deliberately NO cap on values per property. A first draft capped at
# eight, and the catalog immediately exceeded it: nine properties carry enums of
# 9 to 15 members (``sync_state.task_type`` is 15), so the cap was silently
# dropping members while a comment claimed every member was tried. A cap here
# cannot be chosen without deciding which violations are acceptable to miss,
# which is a standard this file has no business setting quietly.

# Advertised string branches the handler is stricter than. The canonical
# process_agent_update and simulate_update models accept a number or a numeric
# string such as "0.5" and refuse everything else, named levels and
# {"value", "scale"} objects included; only the sync_state alias path normalizes
# those upstream. But the advertised schema carries a bare `string` branch with
# no enum, so it tells a caller any string is legal and the handler refuses "x".
#
# This is a real imprecision in the CATALOG, not something the schema swap
# introduced: stdio and REST have advertised the same bare branch all along,
# and docs/interface-contract.v1.json hashes it. Narrowing it is a contract
# change that moves those input_schema_sha256 values and needs an interface
# release, so it is a decision rather than a fix to fold into a test. Recorded
# here so the exception is named and counted instead of silently passing.
#
# Two limits of this exemption, both measured. It does NOT guard the numeric
# branch: that bound is emitted as Pydantic `ge`/`le`, which JSON Schema ignores,
# so the numeric branch is already out of step and this test cannot see it. And
# it covers every string, so narrowing the branch to the named-level enum would
# still pass here even though these canonical handlers refuse named levels.
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
        # not only when it happens to be first or to fall inside a cap.
        return list(schema["enum"])

    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            values: list = []
            for branch in schema[keyword]:
                if isinstance(branch, dict) and branch.get("type") == "null":
                    continue
                values.extend(_candidate_values(branch, defs, depth + 1))
            return values

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
        # Cardinality is a bound like any other: a schema saying exactly three
        # items must not be answered with one. BootstrapStateParams.ethical_drift
        # declares minItems and maxItems of 3, and an earlier draft handed it a
        # single-element list, i.e. a value its own schema calls illegal.
        items = _candidate_values(schema.get("items") or {"type": "string"}, defs, depth + 1)
        if not items:
            return []
        low = int(schema.get("minItems") or 0)
        high = schema.get("maxItems")
        high = high if isinstance(high, int) and high >= low else None
        values: list = [[items[0]] * low]
        if high is not None and high != low:
            values.append([items[0]] * high)
        # The shortest and longest arrays above test cardinality but carry only
        # the first item candidate, and an empty one carries none. Every item
        # candidate must also appear, at a length that holds an item, or the
        # bounds of an object inside an array are generated and then discarded:
        # an overshooting ToolResultEvidence.summary maxLength escaped exactly so.
        item_length = max(low, 1)
        if high is not None:
            item_length = min(item_length, high)
        if item_length:
            values.extend([value] * item_length for value in items)
        return values
    if json_type == "object":
        properties = schema.get("properties") or {}
        base: dict[str, Any] = {}
        for name in schema.get("required") or []:
            nested = _candidate_values(properties.get(name) or {}, defs, depth + 1)
            if not nested:
                return []
            base[name] = nested[0]
        # A nested field's bounds are as advertised as a top-level one's, and
        # fourteen of them carry a constraint. Vary one at a time off the base,
        # the same walk this test does at the top level.
        instances = [base]
        for name, definition in properties.items():
            for value in _candidate_values(definition, defs, depth + 1):
                instances.append({**base, name: value})
        return instances
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
    here exactly as dispatch supplies it. Without that, the 57 properties of the
    four action-injecting aliases fail on a missing field rather than on
    anything this test is about.

    A TOP-LEVEL property the helper cannot synthesize is counted and named rather
    than silently passed over. An optional NESTED field it cannot synthesize is
    not yet counted; see the module comment above the helpers for that gap.
    Measured 2026-09-12: 507 properties, 964 boundary values, none
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
    stale_exemptions: list[str] = []
    exercised_exemptions: set[tuple[str, str]] = set()

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
        # If a required field cannot be built, no instance for this tool is
        # valid, so every one of its properties goes unchecked. Name all of
        # them: recording only the required field would let the rest vanish
        # from every counter while the report looked like one small gap.
        unbuildable = [
            required
            for required in schema.get("required") or []
            if _synthesize(properties.get(required) or {}, defs) is _UNSYNTHESIZABLE
        ]
        if unbuildable:
            unsynthesizable.append(
                f"{name}: required {unbuildable} cannot be synthesized, so all "
                f"{len(properties)} of its properties went unchecked"
            )
            continue
        for required in schema.get("required") or []:
            base[required] = _synthesize(properties.get(required) or {}, defs)

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
                if handler_model is not None:
                    try:
                        handler_model.model_validate(instance)
                    except ValidationError as exc:
                        if not exempt:
                            handler_rejected.append(
                                f"{name}.{prop}={shown} -> {canonical}: {exc.errors()[:1]}"
                            )
                    else:
                        # An exemption records that the handler REFUSES this.
                        # If it now accepts, the entry describes nothing and has
                        # to go, or it silently suppresses a real check forever.
                        if exempt:
                            stale_exemptions.append(f"{name}.{prop}={shown}")
                if exempt:
                    exercised_exemptions.add((canonical, prop))
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

    # The exemption stays honest only while it still describes something real,
    # which takes two checks rather than one. Checking the handler still
    # REFUSES catches a widened handler; requiring every declared entry to be
    # exercised catches a narrowed catalog one entry at a time. A single
    # "something matched" assertion caught neither: three stale entries would
    # have sat behind one live one.
    assert not stale_exemptions, (
        "the handler now ACCEPTS values _ADVERTISED_STRING_WIDER_THAN_HANDLER "
        "exempts, so those entries suppress a check that would pass. Remove them "
        f"({len(stale_exemptions)}):\n  " + "\n  ".join(stale_exemptions)
    )
    unexercised = _ADVERTISED_STRING_WIDER_THAN_HANDLER - exercised_exemptions
    assert not unexercised, (
        "these exemptions were never reached, so the catalog no longer advertises "
        f"the branch they excuse. Delete them: {sorted(unexercised)}"
    )

    # No allowance for constructs the helper cannot model. A tolerated fraction
    # would be a deciding standard chosen silently — how much of the surface is
    # acceptable to leave unchecked — and this repo requires such a standard to
    # be stated as a choice rather than absorbed. So a `pattern` or `format` on a
    # TOP-LEVEL property fails here. The same construct on an optional field
    # inside a nested model does not reach this list today: the object walk
    # skips a field with no candidates without recording it, which is how
    # ToolResultEvidence.observed_at (format date-time) goes unchecked. Counting
    # those would fail immediately on that field and needs a decision about
    # whether `format` is an assertion or only an annotation.
    assert not unsynthesizable, (
        "advertised properties the helper cannot synthesize, so the invariant "
        "went unchecked for them. Extend _candidate_values, or name the construct "
        f"deliberately ({len(unsynthesizable)}):\n  " + "\n  ".join(unsynthesizable)
    )


# ---------------------------------------------------------------------------
# The invariant's own sensitivity
# ---------------------------------------------------------------------------
# Every entry is a way the invariant above was once blind, found by mutation
# rather than by reading, and each was fixed. Committing them is what keeps the
# fixes fixed: a later tidy-up of _candidate_values that reintroduces a cap,
# drops nested exploration, or softens the exemption check would still pass the
# invariant itself, because the catalog is correct today. Only a test that
# BREAKS the catalog and demands a failure can see that kind of decay.
#
# Each mutation is applied to the live mount and restored in a finally block, so
# no other test observes it. Each also names the gate that must catch it: an
# unqualified pytest.raises(AssertionError) would pass if the invariant failed
# for any reason at all, including the wrong one, which is the same lenience
# this section exists to prevent.

_HANDLER_GATE = "values the handler's own model refuses"
_TRANSPORT_GATE = "values its own argument model refuses"

_REVIEW_ROUND_MUTATIONS = {
    "an advertised maximum above the handler's": (
        "property", "delegate_inference", "timeout_s",
        {"type": "integer", "minimum": 5, "maximum": 99999},
        _HANDLER_GATE,
    ),
    "an advertised maxLength above the handler's": (
        "property", "record_progress_pulse", "metric_name",
        {"type": "string", "minLength": 1, "maxLength": 99999},
        _HANDLER_GATE,
    ),
    "a minimum above the handler's maximum": (
        "property", "delegate_inference", "timeout_s",
        {"type": "integer", "minimum": 99999},
        _HANDLER_GATE,
    ),
    "a bogus enum member in a non-first position": (
        "property", "sync_state", "task_type",
        {"type": "string", "enum": ["mixed", "not_a_task_type"]},
        _HANDLER_GATE,
    ),
    "a type the wrapper's argument model refuses": (
        "property", "delegate_inference", "prompt",
        {"type": "array", "items": {"type": "integer"}, "minItems": 1},
        _TRANSPORT_GATE,
    ),
    "a bogus enum member on a field inside a nested model": (
        "nested", "start_session", ("BootstrapStateParams", "task_type"),
        {"type": "string", "enum": ["mixed", "not_a_task_type"]},
        _HANDLER_GATE,
    ),
    "an array cardinality above the handler's": (
        "nested", "start_session", ("BootstrapStateParams", "ethical_drift"),
        {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 99},
        _HANDLER_GATE,
    ),
    "a field of an object inside an array whose maxLength overshoots": (
        "nested", "sync_state", ("ToolResultEvidence", "summary"),
        {"type": "string", "maxLength": 99999},
        _HANDLER_GATE,
    ),
    "an exemption whose advertised branch no longer exists": (
        "property", "simulate_update", "confidence",
        {"anyOf": [{"type": "number", "minimum": 0, "maximum": 1}, {"type": "null"}]},
        "exemptions were never reached",
    ),
    "a required field the helper cannot synthesize": (
        "property", "delegate_inference", "prompt",
        {"type": "string", "pattern": "^x+$"},
        "cannot synthesize",
    ),
}


@pytest.mark.parametrize(
    "direction", sorted(_REVIEW_ROUND_MUTATIONS), ids=lambda label: label.replace(" ", "-")
)
def test_the_invariant_fails_when_the_catalog_is_broken(direction):
    """Break the advertised schema in one known way; the invariant must fail."""
    import copy

    from src import mcp_server

    import re

    kind, tool_name, location, replacement, expected_gate = (
        _REVIEW_ROUND_MUTATIONS[direction]
    )
    parameters = mcp_server.mcp._tool_manager.get_tool(tool_name).parameters
    if kind == "property":
        container, key = parameters["properties"], location
    else:
        model_name, key = location
        container = parameters["$defs"][model_name]["properties"]
    original = copy.deepcopy(container[key])

    container[key] = replacement
    try:
        with pytest.raises(AssertionError, match=re.escape(expected_gate)):
            test_every_advertised_value_survives_both_validation_boundaries()
    finally:
        container[key] = original

    # Restoration is part of the contract: a mutation that leaked would make
    # every later test in the session observe a broken catalog.
    test_every_advertised_value_survives_both_validation_boundaries()


def test_a_widened_handler_invalidates_the_exemption():
    """The exemption records that the handler REFUSES a value; accepting it must fail.

    Separate from the mutations above because it breaks the handler rather than
    the catalog. It is the direction the first exemption guard could not see:
    exempted values skipped handler validation, so nothing learned the handler
    had changed.
    """
    from pydantic import ConfigDict, create_model

    from src.tool_schemas import get_pydantic_schemas

    schemas = get_pydantic_schemas()
    permissive = create_model("WidenedHandler", __config__=ConfigDict(extra="allow"))
    for canonical in sorted({tool for tool, _ in _ADVERTISED_STRING_WIDER_THAN_HANDLER}):
        original = schemas[canonical]
        schemas[canonical] = permissive
        try:
            with pytest.raises(AssertionError, match="handler now ACCEPTS"):
                test_every_advertised_value_survives_both_validation_boundaries()
        finally:
            schemas[canonical] = original

    test_every_advertised_value_survives_both_validation_boundaries()
