"""The tool bookkeeping maps describe the advertised roster, exactly.

Five hand-maintained maps say something about every tool: TOOL_CATEGORIES,
TOOL_TIERS, and TOOL_OPERATIONS in ``src/tool_modes.py``, ``_TOOL_STABILITY``
in ``src/mcp_handlers/tool_stability.py``, and TOOL_RELATIONSHIPS in
``src/mcp_handlers/introspection/tool_catalog.py``. Nothing regenerates them,
so they drift whenever a tool is consolidated, renamed, or added: on
2026-09-07, 23 of 43 registered tools were in no category or tier (every
action router among them), 26 pre-consolidation names were still listed, the
stability map named four tools that no longer existed and rated every router
beta by omission, and ``get_tools_for_mode("full")`` returned the 66 schema
definitions against the 51 names the registrar could advertise.

The rule these tests hold:

* the **roster** is every register=True dispatch tool plus the workflow
  aliases (``advertised_tool_names_full``);
* TOOL_CATEGORIES and TOOL_TIERS **partition** the roster -- every advertised
  name in exactly one bucket, nothing else in any bucket;
* TOOL_OPERATIONS covers the roster and may also carry legacy alias names
  (a legacy name's own read/write class is narrower than its router's);
* ``_TOOL_STABILITY`` is keyed by the registered tools, all of them, and an
  alias reports its canonical tool's tier;
* TOOL_RELATIONSHIPS covers the roster, and every name it mentions resolves
  to something callable (or is a documented plugin-provided tool);
* ``full`` mode is the roster.
"""

from __future__ import annotations

import pytest

import src.mcp_handlers  # noqa: F401  -- settles every @mcp_tool decorator
from src.mcp_handlers import tool_stability as ts
from src.mcp_handlers.decorators import get_tool_registry
from src.mcp_handlers.introspection import tool_catalog as tc
from src.tool_modes import (
    TOOL_CATEGORIES,
    TOOL_OPERATIONS,
    TOOL_TIERS,
    advertised_tool_names_full,
    get_tools_for_mode,
)

# Tools the catalog may describe although this repository never registers
# them: they live in the separately installed unitares-pi-plugin and register
# only when it is loaded (see the note in src/tool_schemas.py).
PLUGIN_PROVIDED_TOOLS = frozenset({"pi", "pi_restart_service"})


@pytest.fixture(autouse=True)
def first_party_aliases(monkeypatch):
    # Plugin register() also installs aliases, independently of its handler
    # decorators. Isolate those alongside the first-party tool roster below;
    # otherwise a full run tests foreign alias dates/stability against a
    # deliberately first-party registry. Restore every alias after the test.
    monkeypatch.setattr(ts, "_TOOL_ALIASES", {
        name: alias for name, alias in ts.list_all_aliases().items()
        if alias.new_name not in PLUGIN_PROVIDED_TOOLS
    })


@pytest.fixture
def roster(first_party_tool_surface) -> set[str]:
    return advertised_tool_names_full()


@pytest.fixture
def registered(first_party_tool_surface) -> set[str]:
    return set(get_tool_registry())


@pytest.fixture
def callable_names(roster) -> set[str]:
    """Every name a caller can dispatch: the roster plus resolvable aliases."""
    return roster | set(ts.list_all_aliases())


def _partition_violations(buckets: dict[str, set[str]], roster: set[str]) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for bucket, names in buckets.items():
        for name in names:
            seen.setdefault(name, []).append(bucket)
    return {
        "not_advertised": sorted(n for n in seen if n not in roster),
        "unassigned": sorted(n for n in roster if n not in seen),
        "in_several": sorted(n for n, b in seen.items() if len(b) > 1),
    }


def test_roster_is_registered_tools_plus_workflow_aliases(roster, registered):
    assert roster == registered | set(ts.AGENT_WORKFLOW_ALIASES)
    assert not (registered & set(ts.AGENT_WORKFLOW_ALIASES)), (
        "a workflow alias name must not also be a registered tool"
    )


def test_full_mode_is_the_roster(roster):
    assert get_tools_for_mode("full") == roster


def test_every_narrower_mode_is_inside_the_roster(roster):
    for mode in ("minimal", "lite", "operator_readonly", "operator_recovery"):
        assert get_tools_for_mode(mode) <= roster, mode


def test_categories_partition_the_roster(roster):
    violations = _partition_violations(TOOL_CATEGORIES, roster)
    assert violations == {"not_advertised": [], "unassigned": [], "in_several": []}


def test_tiers_partition_the_roster(roster):
    violations = _partition_violations(TOOL_TIERS, roster)
    assert violations == {"not_advertised": [], "unassigned": [], "in_several": []}


def test_categories_agree_with_the_introspection_catalog(roster):
    """list_tools(category=...) filters on the catalog's category; the mode
    map must say the same thing about every advertised name."""
    mode_category = {n: c for c, names in TOOL_CATEGORIES.items() for n in names}
    disagreements = {
        n: (mode_category[n], tc.TOOL_RELATIONSHIPS.get(n, {}).get("category"))
        for n in sorted(roster)
        if tc.TOOL_RELATIONSHIPS.get(n, {}).get("category") != mode_category[n]
    }
    assert disagreements == {}


def test_operations_cover_the_roster_and_only_callable_names(roster, callable_names):
    assert sorted(roster - set(TOOL_OPERATIONS)) == []
    assert sorted(set(TOOL_OPERATIONS) - callable_names) == []
    assert set(TOOL_OPERATIONS.values()) <= {"read", "write", "admin"}


def test_stability_map_is_keyed_by_every_registered_tool(registered):
    assert set(ts._TOOL_STABILITY) == registered


def test_stability_of_an_alias_is_its_canonical_tools(registered):
    for alias, info in ts.list_all_aliases().items():
        assert info.new_name in registered, alias
        assert ts.get_tool_stability(alias) == ts.get_tool_stability(info.new_name), alias


def test_catalog_relationships_cover_the_roster_and_name_only_callable_tools(roster, callable_names):
    allowed = callable_names | PLUGIN_PROVIDED_TOOLS
    assert sorted(roster - set(tc.TOOL_RELATIONSHIPS)) == []
    assert sorted(set(tc.TOOL_RELATIONSHIPS) - allowed) == []
    dangling = {
        f"{name}.{field}": sorted(set(entry.get(field) or []) - allowed)
        for name, entry in tc.TOOL_RELATIONSHIPS.items()
        for field in ("depends_on", "related_to", "replaces")
        if set(entry.get(field) or []) - allowed
    }
    assert dangling == {}


def test_deprecation_registry_names_only_callable_tools(callable_names):
    for name, entry in tc.DEPRECATION_REGISTRY.items():
        assert name in callable_names, name
        assert entry["superseded_by"] in callable_names, name


def test_consolidated_and_deprecated_aliases_carry_the_date_the_old_name_stopped_being_canonical():
    """deprecated_since is the retirement ledger: a superseded name has one, a friendly guess has none.

    Until 2026-09-07 no entry carried a date, so the field the wave-3a probe
    and describe_tool report was always null and nothing could say how long an
    old name had been a compatibility shim.
    """
    from datetime import datetime

    for name, info in ts.list_all_aliases().items():
        if info.reason == "intuitive_alias":
            assert info.deprecated_since is None, f"{name}: an intuitive alias was never canonical"
        else:
            assert isinstance(info.deprecated_since, datetime), f"{name}: {info.reason} alias needs deprecated_since"
            assert info.deprecated_since <= datetime.now(), name
    # Where the introspection registry also names the tool, the two ledgers agree.
    for name, entry in tc.DEPRECATION_REGISTRY.items():
        alias = ts.list_all_aliases().get(name)
        if alias is not None:
            assert alias.deprecated_since is not None, name
            assert alias.deprecated_since.date().isoformat() == entry["deprecated_since"], name


def test_the_record_table_is_the_roster_and_the_wire_order(roster, registered):
    """src/tool_meta.py has one record per advertised name and nothing else.

    The registered records, in table order, are the wire order of tools/list;
    the workflow-alias records are exactly AGENT_WORKFLOW_ALIASES.
    """
    from src import tool_meta
    from src.tool_schemas import TOOL_ORDER, get_tool_definitions

    names = [m.name for m in tool_meta.TOOL_META]
    assert len(names) == len(set(names))
    assert set(names) == roster
    assert set(tool_meta.WORKFLOW_ALIAS_NAMES) == set(ts.AGENT_WORKFLOW_ALIASES)
    assert set(tool_meta.WIRE_ORDER) == registered
    assert list(tool_meta.WIRE_ORDER) == TOOL_ORDER
    # get_tool_definitions() returns the registered tools in wire order, and
    # only those: the 23 register=False delegates it carried until 2026-09-07
    # were filtered out again by every consumer that reaches a wire.
    assert [t.name for t in get_tool_definitions()] == TOOL_ORDER


def test_the_five_maps_derive_from_the_record_table():
    from src import tool_meta
    from src.tool_modes import TOOL_CATEGORIES, TOOL_OPERATIONS, TOOL_TIERS

    for m in tool_meta.TOOL_META:
        assert m.name in TOOL_TIERS[m.tier]
        assert m.name in TOOL_CATEGORIES[m.category]
        assert TOOL_OPERATIONS[m.name] == m.operation
        assert tc.TOOL_RELATIONSHIPS[m.name]["category"] == m.category
        if m.workflow_alias:
            assert m.name not in ts._TOOL_STABILITY
            assert m.stability is None
        else:
            assert ts._TOOL_STABILITY[m.name] is m.stability
    assert set(TOOL_OPERATIONS) == {m.name for m in tool_meta.TOOL_META}


def test_a_legacy_alias_operation_is_only_declared_where_it_narrows_its_router():
    from src.tool_modes import TOOL_OPERATIONS

    for name, info in ts.list_all_aliases().items():
        if info.operation is None:
            continue
        assert name not in TOOL_OPERATIONS, f"{name}: a roster name has its own class"
        assert info.operation in {"read", "write", "admin"}, name
        assert info.operation != TOOL_OPERATIONS[info.new_name], (
            f"{name}: declares the same class as its router; drop the override"
        )
