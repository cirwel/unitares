"""
Tests for src/mcp_handlers/tool_stability.py - Tool stability and alias system.

All functions are pure. Tests data classes, alias resolution, stability tiers.
"""

import pytest
import sys
from pathlib import Path
from datetime import datetime

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.tool_stability import (
    ToolStability,
    ToolAlias,
    ToolLifecycle,
    _TOOL_ALIASES,
    _TOOL_STABILITY,
    resolve_tool_alias,
    get_tool_stability,
    list_all_aliases,
)


# ============================================================================
# ToolStability Enum
# ============================================================================

class TestToolStabilityEnum:

    def test_values(self):
        assert ToolStability.STABLE.value == "stable"
        assert ToolStability.BETA.value == "beta"
        assert ToolStability.EXPERIMENTAL.value == "experimental"


# ============================================================================
# ToolAlias Dataclass
# ============================================================================

class TestToolAlias:

    def test_creation(self):
        alias = ToolAlias(old_name="old", new_name="new", reason="renamed")
        assert alias.old_name == "old"
        assert alias.new_name == "new"
        assert alias.reason == "renamed"

    def test_optional_fields(self):
        alias = ToolAlias(old_name="old", new_name="new", reason="deprecated",
                          deprecated_since=datetime(2026, 1, 1),
                          migration_note="Use new_tool",
                          inject_action="get")
        assert alias.deprecated_since == datetime(2026, 1, 1)
        assert alias.migration_note == "Use new_tool"
        assert alias.inject_action == "get"


# ============================================================================
# ToolLifecycle Dataclass
# ============================================================================

class TestToolLifecycle:

    def test_creation(self):
        lc = ToolLifecycle(name="test_tool", stability=ToolStability.STABLE, created_at=datetime.now())
        assert lc.name == "test_tool"
        assert lc.aliases == []  # post_init sets empty list

    def test_with_aliases(self):
        lc = ToolLifecycle(name="test", stability=ToolStability.BETA,
                           created_at=datetime.now(), aliases=["old1", "old2"])
        assert lc.aliases == ["old1", "old2"]


# ============================================================================
# resolve_tool_alias
# ============================================================================

class TestResolveToolAlias:

    def test_known_alias(self):
        name, alias = resolve_tool_alias("status")
        assert name == "get_governance_metrics"
        assert alias is not None
        assert alias.old_name == "status"

    def test_not_an_alias(self):
        name, alias = resolve_tool_alias("process_agent_update")
        assert name == "process_agent_update"
        assert alias is None

    def test_start_alias(self):
        name, alias = resolve_tool_alias("start")
        assert name == "onboard"

    def test_login_alias(self):
        name, alias = resolve_tool_alias("login")
        assert name == "onboard"

    def test_checkin_alias(self):
        name, alias = resolve_tool_alias("checkin")
        assert name == "process_agent_update"

    def test_sync_state_alias(self):
        name, alias = resolve_tool_alias("sync_state")
        assert name == "process_agent_update"
        assert alias.reason == "intuitive_alias"

    def test_checkin_aliases_carry_complexity_normalizer(self):
        """All intuitive check-in aliases normalize complexity, so the agent
        experience is consistent regardless of which friendly name is used."""
        for alias_name in ("checkin", "log", "update", "sync_state"):
            _, alias = resolve_tool_alias(alias_name)
            assert alias.param_normalizer is not None, alias_name

    def test_canonical_tools_have_no_normalizer(self):
        """Tolerance lives on the friendly aliases only — no alias of a
        non-check-in tool grows a normalizer by accident."""
        for name, alias in _TOOL_ALIASES.items():
            if alias.new_name != "process_agent_update":
                assert alias.param_normalizer is None, name

    def test_pi_health_alias(self):
        pytest.importorskip("unitares_pi_plugin")
        import unitares_pi_plugin as _plugin
        _plugin.register()
        name, alias = resolve_tool_alias("pi_health")
        assert name == "pi"
        assert alias.inject_action == "health"

    def test_list_agents_alias(self):
        name, alias = resolve_tool_alias("list_agents")
        assert name == "agent"
        assert alias.inject_action == "list"


# ============================================================================
# get_tool_stability
# ============================================================================

class TestGetToolStability:

    def test_stable_tool(self):
        assert get_tool_stability("identity") == ToolStability.STABLE

    def test_beta_tool(self):
        assert get_tool_stability("dialectic") == ToolStability.BETA

    def test_experimental_tool(self):
        assert get_tool_stability("simulate_update") == ToolStability.EXPERIMENTAL

    def test_unknown_tool_default(self):
        assert get_tool_stability("totally_unknown") == ToolStability.BETA


# ============================================================================
# list_all_aliases
# ============================================================================

class TestListAllAliases:

    def test_returns_dict(self):
        result = list_all_aliases()
        assert isinstance(result, dict)
        assert len(result) > 0

    def test_returns_copy(self):
        result = list_all_aliases()
        result["test_inject"] = "bad"
        assert "test_inject" not in _TOOL_ALIASES


# ============================================================================
# Alias registry sanity
# ============================================================================

class TestAliasRegistrySanity:

    def test_all_aliases_have_required_fields(self):
        for name, alias in _TOOL_ALIASES.items():
            assert alias.old_name == name
            assert alias.new_name
            assert alias.reason in ("renamed", "consolidated", "deprecated", "intuitive_alias")

    def test_inject_action_set_for_consolidated(self):
        """Most consolidated tools should have inject_action set."""
        # Some consolidated aliases don't need inject_action because the target
        # tool doesn't use an action= pattern (identity) or the mapping is
        # to a different specific tool (search_knowledge_graph, knowledge variants)
        exempt = {"authenticate", "session", "quick_start", "recall_identity",
                  "bind_identity", "hello", "find_similar_discoveries_graph",
                  "get_related_discoveries_graph", "get_response_chain_graph",
                  "reply_to_question"}
        for name, alias in _TOOL_ALIASES.items():
            if alias.reason == "consolidated" and name not in exempt:
                assert alias.inject_action is not None, f"Consolidated alias '{name}' missing inject_action"
