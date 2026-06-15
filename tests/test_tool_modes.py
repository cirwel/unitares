"""
Tests for src/tool_modes.py - Tool mode filtering and classification.

Tests mode sets, get_tools_for_mode(), should_include_tool(), and constants.
Pure data + function tests, no external dependencies.
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import patch

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.tool_modes import (
    TOOL_MODE,
    MINIMAL_MODE_TOOLS,
    LITE_MODE_TOOLS,
    OPERATOR_READONLY_MODE_TOOLS,
    OPERATOR_RECOVERY_MODE_TOOLS,
    TOOL_TIERS,
    TOOL_OPERATIONS,
    TOOL_CATEGORIES,
    CLAUDE_DESKTOP_EXCLUDED_TOOLS,
    get_tools_for_mode,
    should_include_tool,
    is_claude_desktop_client,
)


# --- Mode Sets Tests ---


class TestModeSets:
    """Tests for the mode tool sets."""

    def test_minimal_mode_has_essentials(self):
        assert "start_session" in MINIMAL_MODE_TOOLS
        assert "sync_state" in MINIMAL_MODE_TOOLS
        assert "check_working_state" in MINIMAL_MODE_TOOLS
        assert "list_tools" in MINIMAL_MODE_TOOLS
        assert "onboard" not in MINIMAL_MODE_TOOLS
        assert "process_agent_update" not in MINIMAL_MODE_TOOLS
        assert "get_governance_metrics" not in MINIMAL_MODE_TOOLS

    def test_lite_mode_superset_of_minimal_essentials(self):
        """Lite should include the primary workflow tools from minimal."""
        for tool in ["start_session", "sync_state", "check_working_state"]:
            assert tool in LITE_MODE_TOOLS, f"{tool} should be in lite mode"

    def test_lite_mode_has_consolidated_tools(self):
        """Lite mode should have Feb 2026 consolidated tools."""
        consolidated = ["agent", "knowledge", "observe", "config", "export", "calibration"]
        for tool in consolidated:
            assert tool in LITE_MODE_TOOLS, f"Consolidated tool '{tool}' should be in lite mode"

    def test_operator_readonly_has_detection(self):
        assert "detect_stuck_agents" in OPERATOR_READONLY_MODE_TOOLS

    def test_operator_recovery_extends_readonly(self):
        """Recovery mode should include all readonly tools plus recovery tools."""
        for tool in OPERATOR_READONLY_MODE_TOOLS:
            assert tool in OPERATOR_RECOVERY_MODE_TOOLS, \
                f"Readonly tool '{tool}' should be in recovery mode"
        assert "operator_resume_agent" in OPERATOR_RECOVERY_MODE_TOOLS

    def test_minimal_is_smallest(self):
        assert len(MINIMAL_MODE_TOOLS) < len(LITE_MODE_TOOLS)


# --- get_tools_for_mode Tests ---


class TestGetToolsForMode:
    """Tests for get_tools_for_mode()."""

    def test_minimal_mode(self):
        tools = get_tools_for_mode("minimal")
        assert tools == MINIMAL_MODE_TOOLS

    def test_lite_mode(self):
        tools = get_tools_for_mode("lite")
        assert tools == LITE_MODE_TOOLS

    def test_operator_readonly(self):
        tools = get_tools_for_mode("operator_readonly")
        assert tools == OPERATOR_READONLY_MODE_TOOLS

    def test_operator_recovery(self):
        tools = get_tools_for_mode("operator_recovery")
        assert tools == OPERATOR_RECOVERY_MODE_TOOLS

    def test_full_mode_returns_all(self):
        tools = get_tools_for_mode("full")
        # Full mode should include everything from lite + more
        assert len(tools) >= len(LITE_MODE_TOOLS)

    def test_category_mode(self):
        """Passing a category name should return that category's tools."""
        tools = get_tools_for_mode("core")
        assert tools == TOOL_CATEGORIES["core"]

    def test_returns_copy(self):
        """Should return a copy, not the original set."""
        tools1 = get_tools_for_mode("minimal")
        tools1.add("fake_tool")
        tools2 = get_tools_for_mode("minimal")
        assert "fake_tool" not in tools2

    def test_unknown_mode_returns_all(self):
        """Unknown mode should fall through to union of categories."""
        tools = get_tools_for_mode("nonexistent_mode")
        assert len(tools) > 0


# --- should_include_tool Tests ---


class TestShouldIncludeTool:
    """Tests for should_include_tool()."""

    def test_tool_in_mode(self):
        assert should_include_tool("start_session", mode="minimal") is True

    def test_tool_not_in_mode(self):
        assert should_include_tool("call_model", mode="minimal") is False

    def test_list_tools_always_included(self):
        """list_tools should be included in any mode."""
        assert should_include_tool("list_tools", mode="minimal") is True
        assert should_include_tool("list_tools", mode="lite") is True

    def test_describe_tool_always_included(self):
        """describe_tool should be included in any mode."""
        assert should_include_tool("describe_tool", mode="minimal") is True

    def test_full_mode_includes_all(self):
        assert should_include_tool("process_agent_update", mode="full") is True

    def test_claude_desktop_exclusion(self):
        """Tools in CLAUDE_DESKTOP_EXCLUDED_TOOLS should be excluded for Claude Desktop."""
        # Currently empty set, but test the mechanism
        if CLAUDE_DESKTOP_EXCLUDED_TOOLS:
            tool = next(iter(CLAUDE_DESKTOP_EXCLUDED_TOOLS))
            assert should_include_tool(tool, mode="full", client_type="claude_desktop") is False

    def test_non_claude_desktop_no_exclusion(self):
        """Non-Claude-Desktop clients should not be affected by exclusions."""
        assert should_include_tool("start_session", mode="lite", client_type=None) is True


# --- TOOL_TIERS Tests ---


class TestToolTiers:
    """Tests for TOOL_TIERS constant."""

    def test_has_three_tiers(self):
        assert "essential" in TOOL_TIERS
        assert "common" in TOOL_TIERS
        assert "advanced" in TOOL_TIERS

    def test_essential_has_core_tools(self):
        assert "start_session" in TOOL_TIERS["essential"]
        assert "sync_state" in TOOL_TIERS["essential"]
        assert "check_working_state" in TOOL_TIERS["essential"]
        assert "onboard" not in TOOL_TIERS["essential"]
        assert "process_agent_update" not in TOOL_TIERS["essential"]
        assert "health_check" in TOOL_TIERS["essential"]

    def test_tiers_are_sets(self):
        for tier_name, tier_tools in TOOL_TIERS.items():
            assert isinstance(tier_tools, set), f"Tier '{tier_name}' should be a set"


# --- TOOL_OPERATIONS Tests ---


class TestToolOperations:
    """Tests for TOOL_OPERATIONS classification."""

    def test_read_operations(self):
        assert TOOL_OPERATIONS["get_governance_metrics"] == "read"
        assert TOOL_OPERATIONS["health_check"] == "read"
        assert TOOL_OPERATIONS["list_agents"] == "read"

    def test_write_operations(self):
        assert TOOL_OPERATIONS["process_agent_update"] == "write"
        assert TOOL_OPERATIONS["store_knowledge_graph"] == "write"
        assert TOOL_OPERATIONS["archive_agent"] == "write"

    def test_admin_operations(self):
        assert TOOL_OPERATIONS["cleanup_stale_locks"] == "admin"

    def test_all_ops_are_valid(self):
        valid_ops = {"read", "write", "admin"}
        for tool, op in TOOL_OPERATIONS.items():
            assert op in valid_ops, f"Tool '{tool}' has invalid operation '{op}'"


# --- TOOL_CATEGORIES Tests ---


class TestToolCategories:
    """Tests for TOOL_CATEGORIES groupings."""

    def test_has_expected_categories(self):
        expected = ["core", "identity", "admin", "export", "config",
                    "lifecycle", "observability", "knowledge", "dialectic"]
        for cat in expected:
            assert cat in TOOL_CATEGORIES, f"Category '{cat}' should exist"

    def test_categories_are_sets(self):
        for cat_name, cat_tools in TOOL_CATEGORIES.items():
            assert isinstance(cat_tools, set), f"Category '{cat_name}' should be a set"

    def test_categories_non_empty(self):
        for cat_name, cat_tools in TOOL_CATEGORIES.items():
            assert len(cat_tools) > 0, f"Category '{cat_name}' should not be empty"


# --- is_claude_desktop_client Tests ---


class TestIsClaudeDesktopClient:
    """Tests for is_claude_desktop_client()."""

    def test_returns_bool(self):
        result = is_claude_desktop_client()
        assert isinstance(result, bool)

    @patch.dict("os.environ", {"CLAUDE_DESKTOP": "1"})
    def test_env_var_detection(self):
        assert is_claude_desktop_client() is True

    @patch.dict("os.environ", {"ANTHROPIC_CLAUDE": "1"})
    def test_anthropic_env_var_detection(self):
        assert is_claude_desktop_client() is True
