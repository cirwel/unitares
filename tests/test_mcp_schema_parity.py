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
