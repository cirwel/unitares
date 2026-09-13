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

import copy
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
# WHAT THIS STILL DOES NOT CATCH, stated so a green result is not over-read. It
# probes only the boundaries the advertised schema NAMES, so a handler bound with
# no advertised counterpart at all (an advertised `maximum` deleted while the
# handler keeps it) is invisible to it. Catching that needs the opposite walk,
# from the handler's bounds outward; it is a separate follow-up. Two related
# classes this file used to miss are closed now: a bound emitted in a form JSON
# Schema ignores is refused by test_no_advertised_schema_carries_a_pydantic_only_key,
# and a construct the helper cannot model is recorded wherever it occurs rather
# than skipped.

_UNSYNTHESIZABLE = object()

# There is deliberately NO cap on values per property. A first draft capped at
# eight, and the catalog immediately exceeded it: nine properties carry enums of
# 9 to 15 members (``sync_state.task_type`` is 15), so the cap was silently
# dropping members while a comment claimed every member was tried. A cap here
# cannot be chosen without deciding which violations are acceptable to miss,
# which is a standard this file has no business setting quietly.

# `format` policy: a declared format is treated as an assertion of intent, so the
# value tried is a valid instance of it, and a format this table does not know
# fails loudly instead of being skipped. JSON Schema 2020-12 makes `format` an
# annotation by default, and Python's jsonschema accepts "x" as a date-time even
# with its format checker when the optional parser is absent; but a client that
# reads `date-time` and sends "x" has not built a call from the schema, so a
# server refusing it is not an advertise-versus-accept mismatch. The handler also
# accepts a bare date such as "2026-09-12" for this field: that is the server
# accepting MORE than advertised, the permitted direction, so it is not surfaced.
# Extend this table when a new format appears on the surface; until then an
# unknown one fails, which is the point.
_FORMAT_EXAMPLES = {
    "date-time": "2026-01-01T00:00:00Z",
}

# `pattern` policy, the same shape as `format`: a regex this table knows gets
# instances that match it, chosen to cover each alternative and both ends of the
# range it describes, and an unknown regex fails loudly. A regex cannot be
# sampled generically without either generating values that do not match it
# (the test would then fail for the wrong reason) or silently skipping it.
# test_every_pattern_example_matches_its_pattern keeps the examples honest.
from src.mcp_handlers.schemas.core import UNIT_INTERVAL_STRING_PATTERN  # noqa: E402

_PATTERN_EXAMPLES = {
    UNIT_INTERVAL_STRING_PATTERN: ["0", "1", "1.0", "0.5", ".5"],
}


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


def _candidate_values(
    schema: Any,
    defs: dict,
    depth: int = 0,
    gaps: list | None = None,
    path: str = "value",
) -> list:
    """Every boundary value the advertised ``schema`` calls legal.

    Boundaries rather than arbitrary values, because a bound and an accepting
    model disagree at the edges or nowhere: the shortest AND longest permitted
    string, the lowest AND highest permitted number, every enum member rather
    than the first. Returns a list so one lenient value cannot mask a strict
    one — the failure this replaced was exactly that.

    An empty list means the helper does not model the construct (a ``pattern``,
    an unknown ``format``, an unresolvable ref). That is a gap in the helper,
    not a failure of the invariant. When ``gaps`` is given, every such gap inside
    a composite — an ``anyOf`` branch, a nested field, array items — is
    appended to it with its ``path``. A previous version skipped those without
    a word, which is how a nested date-time field went unchecked while the test
    reported green.
    """

    def _gap(where: str, why: str) -> None:
        if gaps is not None:
            gaps.append(f"{where}: {why}")

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
            for index, branch in enumerate(schema[keyword]):
                if isinstance(branch, dict) and branch.get("type") == "null":
                    continue
                branch_path = f"{path}|{keyword}[{index}]"
                branch_values = _candidate_values(branch, defs, depth + 1, gaps, branch_path)
                if not branch_values:
                    # One modelled branch must not hide an unmodelled sibling.
                    _gap(branch_path, "branch has no candidates")
                values.extend(branch_values)
            return values

    json_type = schema.get("type")
    if isinstance(json_type, list):
        json_type = next((t for t in json_type if t != "null"), None)

    if json_type == "string":
        if "pattern" in schema:
            return list(_PATTERN_EXAMPLES.get(schema["pattern"], []))
        declared_format = schema.get("format")
        if declared_format:
            example = _FORMAT_EXAMPLES.get(declared_format)
            return [example] if example is not None else []
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
        items_path = f"{path}[]"
        items = _candidate_values(
            schema.get("items") or {"type": "string"}, defs, depth + 1, gaps, items_path
        )
        if not items:
            _gap(items_path, "array items have no candidates")
            return []
        low = int(schema.get("minItems") or 0)
        high = schema.get("maxItems")
        high = high if isinstance(high, int) and high >= low else None
        values = [[items[0]] * low]
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
        required = schema.get("required") or []
        base: dict[str, Any] = {}
        for name in required:
            nested = _candidate_values(
                properties.get(name) or {}, defs, depth + 1, gaps, f"{path}.{name}"
            )
            if not nested:
                _gap(f"{path}.{name}", "required nested field has no candidates")
                return []
            base[name] = nested[0]
        # A nested field's bounds are as advertised as a top-level one's, and
        # fourteen of them carry a constraint. Vary one at a time off the base,
        # the same walk this test does at the top level.
        instances = [base]
        for name, definition in properties.items():
            nested_values = _candidate_values(
                definition, defs, depth + 1, gaps, f"{path}.{name}"
            )
            if not nested_values and name not in required:
                _gap(f"{path}.{name}", "nested field has no candidates")
            for value in nested_values:
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
    - the handler path dispatch actually runs: the alias's normalizer, if it
      has one, then the handler's own params model. That is where advertised
      bounds and enums are enforced. A rejection here means the mount advertises
      a value that dies one layer deeper, the shape a hand-authored entry in
      ``ALIAS_SCHEMA_PROPERTY_OVERRIDES`` could produce.

    The normalizer matters for ``sync_state``: its ``complexity`` accepts named
    levels that the canonical model refuses, because the normalizer maps them to
    numbers first. Judging ``sync_state`` against the canonical model alone would
    report its real vocabulary as refused.

    An action-injecting alias strips ``action`` from its advertised schema
    while the router's model requires it, so the injected action is supplied
    here exactly as dispatch supplies it. Without that, the 57 properties of the
    four action-injecting aliases fail on a missing field rather than on
    anything this test is about.

    Any construct the helper cannot model is reported, at any depth: top-level,
    inside a nested model, in an ``anyOf`` branch, or in array items.
    """
    from src import mcp_server
    from src.mcp_handlers.support.param_normalization import ParamNormalizationError
    from src.mcp_handlers.tool_stability import resolve_tool_alias
    from src.tool_schemas import get_pydantic_schemas

    handler_models = get_pydantic_schemas()
    checked = 0
    unsynthesizable: list[str] = []
    transport_rejected: list[str] = []
    handler_rejected: list[str] = []

    for name, tool in sorted(mcp_server.mcp._tool_manager._tools.items()):
        schema = tool.parameters or {}
        defs = schema.get("$defs") or {}
        properties = schema.get("properties") or {}
        canonical, alias = resolve_tool_alias(name)
        handler_model = handler_models.get(canonical)
        normalizer = getattr(alias, "param_normalizer", None) if alias is not None else None

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
            gaps: list[str] = []
            candidates = _candidate_values(definition, defs, gaps=gaps, path=f"{name}.{prop}")
            unsynthesizable.extend(gaps)
            if not candidates:
                if not gaps:
                    unsynthesizable.append(f"{name}.{prop}")
                continue
            for value in candidates:
                instance = {**base, prop: value}
                shown = repr(value)
                if len(shown) > 60:
                    shown = f"{shown[:57]}..."
                try:
                    tool.fn_metadata.arg_model.model_validate(instance)
                except ValidationError as exc:
                    transport_rejected.append(
                        f"{name}.{prop}={shown}: {exc.errors()[:1]}"
                    )
                if handler_model is not None:
                    dispatched = copy.deepcopy(instance)
                    try:
                        if normalizer is not None:
                            normalizer(dispatched)
                        handler_model.model_validate(dispatched)
                    except (ValidationError, ParamNormalizationError) as exc:
                        detail = exc.errors()[:1] if isinstance(exc, ValidationError) else str(exc)
                        handler_rejected.append(
                            f"{name}.{prop}={shown} -> {canonical}: {detail}"
                        )
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

    # No allowance for constructs the helper cannot model. A tolerated fraction
    # would be a deciding standard chosen silently — how much of the surface is
    # acceptable to leave unchecked — and this repo requires such a standard to
    # be stated as a choice rather than absorbed. So a `pattern`, an unknown
    # `format`, or an unresolvable ref fails here wherever it sits.
    assert not unsynthesizable, (
        "advertised properties the helper cannot synthesize, so the invariant "
        "went unchecked for them. Extend _candidate_values or _FORMAT_EXAMPLES, "
        f"or name the construct deliberately ({len(unsynthesizable)}):\n  "
        + "\n  ".join(unsynthesizable)
    )


def test_every_pattern_example_matches_its_pattern():
    """An example that failed its own regex would be an illegal value.

    The invariant would then fail for the wrong reason, blaming the handler for
    refusing something the schema never called legal.
    """
    import re

    for pattern, examples in _PATTERN_EXAMPLES.items():
        for example in examples:
            assert re.fullmatch(pattern, example), f"{example!r} does not match {pattern!r}"


def test_alias_schema_repeats_the_unit_interval_regex_exactly():
    """src/alias_schema.py repeats the regex instead of importing it.

    A drift would make sync_state advertise a different set of numeric strings
    than the canonical tools, with nothing to notice.
    """
    from src.alias_schema import UNIT_INTERVAL_STRING_PATTERN as ALIAS_COPY

    assert ALIAS_COPY == UNIT_INTERVAL_STRING_PATTERN


def test_sync_state_named_levels_match_its_normalizer():
    """The advertised enum is a hand-written copy of the normalizer's table.

    src/alias_schema.py lists the named levels rather than importing them, to
    stay free of runtime imports. A level added to the normalizer but not here
    would be accepted and unadvertised, the safe direction; one added here but
    not to the normalizer would be advertised and refused. Pin them equal so
    neither can drift in silence.
    """
    from src.alias_schema import SYNC_STATE_COMPLEXITY_NAMED_LEVELS
    from src.mcp_handlers.support.param_normalization import NAMED_LEVELS

    assert sorted(SYNC_STATE_COMPLEXITY_NAMED_LEVELS) == sorted(NAMED_LEVELS)


# ---------------------------------------------------------------------------
# Constraint keys JSON Schema does not know
# ---------------------------------------------------------------------------
# Pydantic accepts constraint arguments under its own names. Most are translated
# to JSON Schema keywords when a schema is emitted, but not in every position:
# `Union[float, str, None] = Field(ge=0.0, le=1.0)` emitted `ge`/`le` verbatim
# beside an inner anyOf, at twelve places on three check-in tools. A client
# validator ignores an unknown key, so those bounds were invisible to every
# client while the handler enforced them. The boundary walk above cannot see
# this class at all, because it only probes keywords it recognises.
_PYDANTIC_ONLY_KEYS = frozenset({
    "ge", "gt", "le", "lt",
    "multiple_of",
    "min_length", "max_length",
    "min_items", "max_items",
    "decimal_places", "max_digits",
    "allow_inf_nan",
    "strict",
})

# Subschema maps whose keys are caller-chosen names, and keywords whose values
# are data rather than schema. A parameter literally named `ge`, or a default
# value containing a `le` key, is content and must not be flagged.
_NAME_KEYED = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})
_DATA_KEYWORDS = frozenset({"default", "const", "enum", "examples", "example"})


def _pydantic_only_keys(node: Any, path: str) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _DATA_KEYWORDS:
                continue
            if key in _NAME_KEYED and isinstance(value, dict):
                for name, sub in value.items():
                    found.extend(_pydantic_only_keys(sub, f"{path}.{key}.{name}"))
                continue
            if key in _PYDANTIC_ONLY_KEYS:
                found.append(f"{path}.{key}={value!r}")
            found.extend(_pydantic_only_keys(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(_pydantic_only_keys(item, f"{path}[{index}]"))
    return found


def test_no_advertised_schema_carries_a_pydantic_only_key():
    """Every constraint a client is told about must be one a client can read."""
    mount = _fresh_mount()
    assert mount, "no tools were listed; the check would be vacuous"
    leaks = [hit for name, schema in sorted(mount.items()) for hit in _pydantic_only_keys(schema, name)]
    assert not leaks, (
        "advertised schemas carry Pydantic constraint keys that JSON Schema does "
        "not define, so every client ignores those bounds while the handler "
        "enforces them. Move the constraint onto the member it applies to, e.g. "
        f"Optional[Annotated[float, Field(ge=0.0, le=1.0)]] ({len(leaks)}):\n  "
        + "\n  ".join(leaks)
    )


def test_the_key_ban_finds_a_reintroduced_leak_and_ignores_content():
    """The scanner must fire on the real shape and stay quiet on look-alikes."""
    leaked = {"anyOf": [{"anyOf": [{"type": "number"}, {"type": "string"}], "ge": 0.0, "le": 1.0}, {"type": "null"}]}
    assert _pydantic_only_keys(leaked, "tool.complexity") == [
        "tool.complexity.anyOf[0].ge=0.0",
        "tool.complexity.anyOf[0].le=1.0",
    ]

    look_alikes = {
        "type": "object",
        "properties": {"ge": {"type": "number", "minimum": 0}},
        "default": {"le": 1},
        "enum": [{"lt": 2}],
    }
    assert _pydantic_only_keys(look_alikes, "tool") == []


# ---------------------------------------------------------------------------
# The invariant's own sensitivity
# ---------------------------------------------------------------------------
# Every entry is a way the invariant above was once blind, found by mutation
# rather than by reading, and each was fixed. Committing them is what keeps the
# fixes fixed: a later tidy-up of _candidate_values that reintroduces a cap,
# drops nested exploration, or stops recording a skip would still pass the
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
_SYNTHESIS_GAP = "cannot synthesize"

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
    "a named level the sync_state normalizer does not accept": (
        "property", "sync_state", "complexity",
        {"anyOf": [
            {"type": "number", "minimum": 0.0, "maximum": 1.0},
            {"type": "string", "enum": ["medium", "not_a_named_level"]},
            {"type": "null"},
        ]},
        _HANDLER_GATE,
    ),
    "a required field the helper cannot synthesize": (
        "property", "delegate_inference", "prompt",
        {"type": "string", "pattern": "^x+$"},
        _SYNTHESIS_GAP,
    ),
    "an unknown format on an optional nested field": (
        "nested", "sync_state", ("ToolResultEvidence", "observed_at"),
        {"anyOf": [{"type": "string", "format": "uuid"}, {"type": "null"}]},
        _SYNTHESIS_GAP,
    ),
    "an anyOf branch the helper cannot model beside one it can": (
        "property", "delegate_inference", "timeout_s",
        {"anyOf": [{"type": "integer", "minimum": 5, "maximum": 420}, {"type": "string", "pattern": "^\\d+$"}]},
        _SYNTHESIS_GAP,
    ),
}


@pytest.mark.parametrize(
    "direction", sorted(_REVIEW_ROUND_MUTATIONS), ids=lambda label: label.replace(" ", "-")
)
def test_the_invariant_fails_when_the_catalog_is_broken(direction):
    """Break the advertised schema in one known way; the invariant must fail."""
    import re

    from src import mcp_server

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
