"""The tool edge index must not silently lose edges.

`scripts/dev/tool_edge_index.py` recovers the action->delegate map by reading
the closure of the function `action_router` generates. That is the only way to
get it — the map is never bound to a module-level name — but it is also the
fragile part: rename the inner `router` function or its `actions` local and the
extraction returns nothing. The generator would keep succeeding, the CI
freshness gate would keep passing, and the index would quietly drop every
consolidated tool's routing table, which is the half a reader cannot recover by
grep.

So the guard here is a cross-check, not a snapshot. `ToolDefinition.known_actions`
is built by `action_router` from `frozenset(actions.keys())` at decoration time —
an independent path to the same truth. If the closure read breaks, the two
disagree.
"""

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts/dev"))
sys.path.insert(0, str(REPO))

import tool_edge_index as tei  # noqa: E402


@pytest.fixture(scope="module")
def collected():
    return tei.collect()


def test_every_registered_tool_is_indexed(collected):
    """The index must cover the whole registry, not a subset of it."""
    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS

    tools, _aliases, _failures, _unbound = collected
    assert {t.name for t in tools} == set(_TOOL_DEFINITIONS), (
        "indexed tools diverge from the live registry — a tool is missing from "
        "the only readable map of dispatch"
    )


def test_router_actions_match_the_decorator_derived_set(collected):
    """Closure-read actions must equal the router's own known_actions.

    Two independent derivations of the routing table: this one reads the
    closure, the decorator's came from the same dict at registration. Drift
    means the closure extraction silently broke.
    """
    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS

    tools, _aliases, _failures, _unbound = collected
    routers = [t for t in tools if t.actions]
    assert routers, (
        "no consolidated tools resolved — action_router's closure shape "
        "changed and the index lost every routing table"
    )

    for tool in routers:
        known = _TOOL_DEFINITIONS[tool.name].known_actions or frozenset()
        assert {edge.action for edge in tool.actions} == set(known), (
            f"{tool.name}: closure-read actions disagree with known_actions"
        )


def test_every_action_resolves_to_a_real_source_location(collected):
    """An unresolved delegate is worse than no row — it reads as an answer."""
    tools, _aliases, _failures, _unbound = collected
    unresolved = [
        f"{tool.name}(action={edge.action!r})"
        for tool in tools
        for edge in tool.actions
        if edge.target == tei.UNKNOWN or ":" not in edge.target
    ]
    assert not unresolved, f"delegates with no source location: {unresolved}"


def test_handler_modules_all_import(collected):
    """A module the generator cannot import is a hole in the index.

    The generator reports these rather than failing, so the doc stays honest
    when a dependency is missing. In this repo, on the test environment's
    dependency floor, there should be none.
    """
    _tools, _aliases, failures, _unbound = collected
    assert not failures, f"handler modules failed to import: {failures}"


def test_render_is_deterministic(collected):
    """Byte-identical across runs, or the CI freshness gate flaps."""
    assert tei.render(*collected) == tei.render(*collected)


def test_committed_index_is_fresh(collected):
    """The checked-in doc must match what the live registries produce."""
    committed = tei.OUT.read_text(encoding="utf-8") if tei.OUT.exists() else ""
    assert committed == tei.render(*collected), (
        f"{tei.OUT.relative_to(REPO)} is stale — run: "
        "python3 scripts/dev/tool_edge_index.py"
    )
