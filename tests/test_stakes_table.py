"""#775 stakes classification — table + call-level resolver.

Inert by default: nothing gates on these results yet. This PR ships the
classification half of #775 (the load-bearing artifact) and parks the gate
mechanism pending the Wave-3 BEAM-port sequencing. These tests pin the table's
completeness (every registered surface is a deliberate classification, not a
fail-closed accident) and the resolver's alias/override/fail-closed semantics.
"""

from __future__ import annotations

import pytest

# Same import anchors as test_action_level_identity.py so consolidated +
# single-purpose tools register before we read _TOOL_DEFINITIONS.
import src.mcp_handlers.core  # noqa: F401
import src.mcp_handlers.consolidated  # noqa: F401
import src.mcp_handlers.research_registry  # noqa: F401

from src.mcp_handlers import stakes_table
from src.mcp_handlers.stakes_table import (
    _BASELINE,
    _HIGH,
    export_table,
    get_action_stakes,
    is_high_stakes,
)
from src.mcp_handlers.decorators import (
    _TOOL_DEFINITIONS,
    _resolve_canonical_and_action,
    get_call_stakes_requirement,
    mcp_tool,
)

# Known action inventory for the action_router tools, extracted from the live
# router definitions (consolidated.py + research_registry.py). This doubles as
# a drift guard: add a router action without classifying it and the coverage
# test below fails.
ROUTER_ACTIONS = {
    "knowledge": ["store", "search", "get", "list", "update", "details",
                  "note", "cleanup", "synthesize", "stats", "supersede", "audit"],
    "agent": ["list", "get", "update", "archive", "resume", "delete"],
    "calibration": ["check", "update", "backfill", "rebuild"],
    "config": ["get", "set"],
    "export": ["history", "file"],
    "observe": ["agent", "compare", "similar", "anomalies", "aggregate",
                "telemetry", "audit_events", "outcome_evidence"],
    "dialectic": ["get", "list", "quick", "request", "thesis", "antithesis",
                  "synthesis", "reassign"],
    "research_registry": ["list", "query", "get", "stats", "export", "record"],
}

# External-plugin surfaces this server does not own. They register only when an
# external package (e.g. unitares_pi_plugin) is importable, so they are NOT
# enumerated in the core stakes table — they intentionally fall to the
# fail-closed "high" default until an operator classifies them when the gate is
# built. The module filter below excludes external SINGLE-PURPOSE tools
# automatically (their handler module is outside ``src.``); external
# action_routers like ``pi`` need this explicit allowlist because every
# action_router wrapper's module is ``src.mcp_handlers.decorators`` regardless
# of where its action handlers live.
_EXTERNAL_PLUGIN_TOOLS = {"pi", "pi_restart_service"}


# ---------------------------------------------------------------------------
# Table integrity
# ---------------------------------------------------------------------------

def test_high_and_baseline_are_disjoint():
    assert not (_HIGH & _BASELINE), "a key cannot be both high and baseline"


def test_levels_are_valid():
    for level in stakes_table._STAKES.values():
        assert level in stakes_table.STAKES_LEVELS


def test_get_action_stakes_exact_then_tool_then_fail_closed():
    # exact (tool, action)
    assert get_action_stakes("agent", "archive") == "high"
    assert get_action_stakes("agent", "list") == "baseline"
    # tool-level (tool, None)
    assert get_action_stakes("set_thresholds", None) == "high"
    assert get_action_stakes("health_check", None) == "baseline"
    # genuinely unknown -> fail closed to high
    assert get_action_stakes("totally_unregistered_tool", "whatever") == "high"
    assert get_action_stakes("totally_unregistered_tool", None) == "high"


def test_action_is_case_insensitive():
    assert get_action_stakes("agent", "ARCHIVE") == "high"
    assert get_action_stakes("knowledge", "Search") == "baseline"


def test_is_high_stakes_matches():
    assert is_high_stakes("config", "set") is True
    assert is_high_stakes("config", "get") is False


# ---------------------------------------------------------------------------
# Classification intent — the load-bearing exemptions
# ---------------------------------------------------------------------------

def test_process_agent_update_is_baseline():
    """The chicken-and-egg guard: process_agent_update PRODUCES the verdict, so
    gating it on a prior verdict would permanently block every new agent's first
    check-in. It must never be high-stakes."""
    assert get_action_stakes("process_agent_update", None) == "baseline"


def test_identity_lifecycle_is_baseline():
    for tool in ("onboard", "identity", "bind_session", "self_recovery",
                 "verify_trajectory_identity"):
        assert get_action_stakes(tool, None) == "baseline", tool


def test_destructive_and_fleet_ops_are_high():
    for key in (("agent", "delete"), ("agent", "archive"),
                ("knowledge", "cleanup"), ("knowledge", "supersede"),
                ("calibration", "rebuild"), ("config", "set"),
                ("dialectic", "synthesis")):
        assert get_action_stakes(*key) == "high", key
    for tool in ("archive_orphan_agents", "reset_monitor", "set_thresholds",
                 "cirs_protocol", "cleanup_stale_locks"):
        assert get_action_stakes(tool, None) == "high", tool


# ---------------------------------------------------------------------------
# Coverage drift guard — every registered surface is deliberately classified
# ---------------------------------------------------------------------------

def test_every_router_action_is_classified():
    """No router action falls to the fail-closed default by accident."""
    for tool, actions in ROUTER_ACTIONS.items():
        for action in actions:
            assert (tool, action) in stakes_table._STAKES, f"{tool}:{action} unclassified"


def test_every_core_tool_is_known_to_the_table():
    """Every CORE governance tool (handler defined under ``src.``) is a
    deliberate classification, so the fail-closed default only ever catches a
    genuinely unclassified name — never a core surface.

    External-plugin tools (e.g. the ``unitares_pi_plugin`` device tools, whose
    handler module is not under ``src.``) are intentionally excluded: they fall
    to the fail-closed ``high`` default until an operator classifies them when
    the gate is built. Filtering by handler module keeps this test deterministic
    regardless of which plugins another test in the same process imported."""
    routers = set(ROUTER_ACTIONS)
    unknown = []
    for name, td in _TOOL_DEFINITIONS.items():
        if name in _EXTERNAL_PLUGIN_TOOLS:
            continue  # external action_router — fail-closed-high by design
        module = getattr(td.handler, "__module__", "") or ""
        if not module.startswith("src."):
            continue  # external single-purpose tool — fail-closed-high by design
        if name in routers:
            continue  # covered per-action by the test above
        if (name, None) not in stakes_table._STAKES:
            unknown.append(name)
    assert not unknown, (
        f"core single-purpose tools missing a stakes classification: "
        f"{sorted(unknown)} — add them to stakes_table._HIGH or _BASELINE"
    )


def test_export_table_is_serializable_and_complete():
    table = export_table()
    assert len(table) == len(stakes_table._STAKES)
    for key, level in table.items():
        assert level in stakes_table.STAKES_LEVELS
        assert ":" in key or "_" in key or key.isalpha()


# ---------------------------------------------------------------------------
# Call-level resolver
# ---------------------------------------------------------------------------

def test_resolver_baseline_read():
    assert get_call_stakes_requirement("knowledge", {"action": "search"}) == "baseline"


def test_resolver_high_write():
    assert get_call_stakes_requirement("agent", {"action": "archive"}) == "high"


def test_resolver_uses_default_action_when_actionless():
    # dialectic default_action is "list" (baseline); calibration is "check" (baseline)
    assert get_call_stakes_requirement("dialectic", {}) == "baseline"
    assert get_call_stakes_requirement("calibration", {}) == "baseline"


def test_resolver_unknown_tool_fails_closed():
    assert get_call_stakes_requirement("no_such_tool", {}) == "high"


def test_resolver_alias_canonicalizes():
    # sync_state aliases to process_agent_update (baseline). The resolver must
    # judge the canonical call, not the alias string.
    canonical, _ = _resolve_canonical_and_action("sync_state", {})
    assert canonical == "process_agent_update"
    assert get_call_stakes_requirement("sync_state", {}) == "baseline"


def test_resolver_op_key_alias_for_action():
    # the resolver accepts "op" as an alias for "action" (parity with #425)
    assert get_call_stakes_requirement("agent", {"op": "delete"}) == "high"


def test_tool_level_requires_verdict_override_wins():
    @mcp_tool("stakes_test_high_tool", register=True, requires_verdict="high")
    async def _h(arguments):
        return []
    try:
        assert get_call_stakes_requirement("stakes_test_high_tool", {}) == "high"
    finally:
        _TOOL_DEFINITIONS.pop("stakes_test_high_tool", None)


def test_requires_verdict_validation_rejects_bad_value():
    with pytest.raises(ValueError, match="requires_verdict"):
        @mcp_tool("stakes_test_bad_tool", register=False, requires_verdict="nonsense")
        async def _b(arguments):
            return []


def test_requires_verdict_defaults_to_baseline_on_tooldef():
    # an ordinary tool gets the inert default
    td = _TOOL_DEFINITIONS.get("health_check")
    assert td is not None
    assert td.requires_verdict == "baseline"
