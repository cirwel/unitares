"""#775 stakes classification — table + call-level resolver.

Inert by default: nothing gates on these results yet. This PR ships the
classification half of #775 (the load-bearing artifact) and parks the gate
mechanism pending the Wave-3 BEAM-port sequencing. These tests pin the table's
completeness (every registered surface is a deliberate classification, not a
fail-closed accident), its exactness (every key names a registered tool, never
an alias) and the resolver's alias/override/fail-closed semantics.
"""

from __future__ import annotations

import pytest

# Same import anchors as test_action_level_identity.py so consolidated +
# single-purpose tools register before we read _TOOL_DEFINITIONS.
import src.mcp_handlers.core  # noqa: F401
import src.mcp_handlers.consolidated  # noqa: F401

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
    _is_first_party_module,
    _resolve_canonical_and_action,
    get_call_stakes_requirement,
    mcp_tool,
)


def _is_core_tool(td) -> bool:
    """Whether this repo declared ``td`` — the surface the table enumerates.

    External-plugin tools (e.g. ``unitares_pi_plugin``'s ``pi`` router and
    ``pi_restart_service``) register into the same ``_TOOL_DEFINITIONS`` when
    that package is importable, but they are NOT enumerated in the core stakes
    table: they intentionally fall to the fail-closed "high" default until an
    operator classifies them when the gate is built. Provenance is read from
    the declaring module the decorator recorded (``ToolDefinition.source_module``),
    never from ``handler.__module__`` — every ``action_router`` handler is
    defined in ``src.mcp_handlers.decorators``, so the latter would call a
    plugin's router first-party, and this file used to carry a hand-kept
    allowlist of plugin routers to compensate.
    """
    return _is_first_party_module(td.source_module)


# Derive bounded action vocabularies from the registered tools. ``action_router``
# populates ``known_actions`` directly from its live routing map; a hand-rolled
# bounded tool declares the same metadata explicitly. A new route therefore
# enters this coverage check without anyone remembering to update a second list
# here.
CORE_ACTION_VOCABULARIES = {
    name: td.known_actions
    for name, td in _TOOL_DEFINITIONS.items()
    if td.known_actions and _is_core_tool(td)
}


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
    """These are DELIBERATE high classifications, not fail-closed accidents.

    Every case asserts membership in ``_HIGH`` as well as the resolved level.
    Asserting the level alone is vacuous against deletion: an absent key also
    resolves to "high", so dropping ("admin", "reset_monitor") outright left
    this test green (measured 2026-09-12). That is the same blind spot that let
    thirteen alias entries look load-bearing for as long as they did, and the
    reason to state the property as "explicitly classified" rather than "reads
    as high". Deletion is separately caught by
    test_registered_action_vocabularies_match_stakes_table and by
    test_every_known_action_is_classified_by_the_stakes_table.
    """
    for key in (("agent", "delete"), ("agent", "archive"),
                ("knowledge", "cleanup"), ("knowledge", "supersede"),
                ("calibration", "rebuild"), ("config", "set"),
                ("dialectic", "synthesis")):
        assert key in _HIGH, key
        assert get_action_stakes(*key) == "high", key
    for tool in ("archive_orphan_agents", "set_thresholds", "cirs_protocol"):
        assert (tool, None) in _HIGH, tool
        assert get_action_stakes(tool, None) == "high", tool
    # reset_monitor / cleanup_stale_locks are aliases of admin actions and carry
    # no entry of their own, so what has to hold is that the name dispatch
    # lands on is the classified one. A bare lookup on the alias would only be
    # reporting the fail-closed default.
    for alias, canonical in (("reset_monitor", ("admin", "reset_monitor")),
                             ("cleanup_stale_locks", ("admin", "cleanup_locks"))):
        assert _resolve_canonical_and_action(alias, {}) == canonical, alias
        assert canonical in _HIGH, canonical
        assert get_call_stakes_requirement(alias, {}) == "high", alias


# ---------------------------------------------------------------------------
# Coverage drift guard — every registered surface is deliberately classified
# ---------------------------------------------------------------------------

def test_registered_action_vocabularies_match_stakes_table():
    """Registered actions and exact stakes classifications cannot drift."""
    classified_actions = {}
    for tool, action in stakes_table._STAKES:
        if action is not None:
            classified_actions.setdefault(tool, set()).add(action)

    for tool, actions in CORE_ACTION_VOCABULARIES.items():
        if (tool, None) in stakes_table._STAKES:
            # A deliberate tool-level classification covers every bounded
            # action (for example, all self_recovery actions are baseline).
            assert classified_actions.get(tool, set()) <= set(actions), (
                f"{tool} has exact stakes entries for unregistered actions"
            )
            continue
        assert classified_actions.get(tool, set()) == set(actions), (
            f"{tool} action vocabulary differs from stakes classifications"
        )

    unknown_tools = set(classified_actions) - set(CORE_ACTION_VOCABULARIES)
    assert not unknown_tools, (
        "stakes table classifies actions for tools without a registered "
        f"action vocabulary: {sorted(unknown_tools)}"
    )


def test_every_core_tool_is_known_to_the_table():
    """Every CORE governance tool (declared under this repo's packages) is a
    deliberate classification, so the fail-closed default only ever catches a
    genuinely unclassified name — never a core surface.

    External-plugin tools (e.g. the ``unitares_pi_plugin`` device tools, whose
    declaring module is not under ``src.``) are intentionally excluded: they
    fall to the fail-closed ``high`` default until an operator classifies them
    when the gate is built. Filtering by declaring module keeps this test
    deterministic regardless of which plugins another test in the same process
    imported."""
    action_tools = set(CORE_ACTION_VOCABULARIES)
    unknown = []
    for name, td in _TOOL_DEFINITIONS.items():
        if not _is_core_tool(td):
            continue  # external tool — fail-closed-high by design
        if name in action_tools:
            continue  # covered per-action or tool-level by the test above
        if (name, None) not in stakes_table._STAKES:
            unknown.append(name)
    assert not unknown, (
        f"core single-purpose tools missing a stakes classification: "
        f"{sorted(unknown)} — add them to stakes_table._HIGH or _BASELINE"
    )


def test_every_table_key_names_a_registered_tool():
    """table -> registered, the reverse of the two coverage tests above.

    ``get_call_stakes_requirement`` canonicalizes an alias before it looks the
    call up, so a table entry keyed on an alias name is never consulted — it
    only pads ``export_table()``, the serialization a non-Python gate is meant
    to load. Thirteen such entries had accumulated by 2026-09-12 (F6 of the
    tool-surface audit), every one an alias of a router action the table
    already classified. The failure message names the canonical key to use.

    "Registered" is not quite the property: alias resolution runs first, so a
    name that is BOTH registered and alias-shadowed would still be dispatched
    past. What has to hold is that a key is the name dispatch actually lands
    on, which is what the ``canonical == tool`` half asserts. The two
    conditions cannot diverge today — ``test_no_alias_name_is_also_a_registered_tool``
    keeps the alias table and the registry disjoint — so this is a guard
    against that invariant being relaxed, not a live defect.
    """
    stale = []
    for tool, action in stakes_table._STAKES:
        canonical, canonical_action = _resolve_canonical_and_action(tool, {})
        if tool in _TOOL_DEFINITIONS and canonical == tool:
            continue
        key = tool if action is None else f"{tool}:{action}"
        resolved = canonical if canonical_action is None else f"{canonical}:{canonical_action}"
        stale.append(f"{key} -> {resolved}")
    assert stale == [], (
        "stakes table keys that are not registered dispatch tools "
        f"(shown with the canonical key the name resolves to): {sorted(stale)}"
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
