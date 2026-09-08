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
    STANDARD_MODE_TOOLS,
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
    build_server_instructions,
)


# --- Mode Sets Tests ---


class TestModeSets:
    """Tests for the mode tool sets."""

    def test_minimal_mode_is_the_five_tool_checkpoint_loop(self):
        """Minimal is exactly the loop: bind, re-bind, check in, record, read.

        Discovery tools are deliberately absent -- with five tools the MCP
        client's native tools/list is the discovery surface -- and the raw
        implementation names stay behind their task-verb aliases.
        """
        assert MINIMAL_MODE_TOOLS == {
            "start_session",
            "identity",
            "sync_state",
            "record_result",
            "check_working_state",
        }
        assert "list_tools" not in MINIMAL_MODE_TOOLS
        assert "describe_tool" not in MINIMAL_MODE_TOOLS
        assert "onboard" not in MINIMAL_MODE_TOOLS
        assert "process_agent_update" not in MINIMAL_MODE_TOOLS
        assert "get_governance_metrics" not in MINIMAL_MODE_TOOLS
        assert "outcome_event" not in MINIMAL_MODE_TOOLS

    def test_standard_mode_is_the_loop_plus_the_unreachable_capabilities(self):
        """Standard adds exactly the names that carry an otherwise-dormant capability.

        A mode filters tools/list, and a schema-driven client offers the model
        only what tools/list returns -- so a capability that is never
        advertised cannot be reached by such a client at all. Standard
        advertises shared memory, structured review, and advisory inference on
        top of the checkpoint loop, and nothing else: no routers, no discovery
        tools, no operator surface.
        """
        assert STANDARD_MODE_TOOLS == MINIMAL_MODE_TOOLS | {
            "search_shared_memory",
            "store_finding",
            "update_finding",
            "request_review",
            "consult",
        }
        assert len(STANDARD_MODE_TOOLS) == 10
        for router in ("knowledge", "agent", "observe", "dialectic", "config"):
            assert router not in STANDARD_MODE_TOOLS
        assert "list_tools" not in STANDARD_MODE_TOOLS
        assert "describe_tool" not in STANDARD_MODE_TOOLS
        assert "admin" not in STANDARD_MODE_TOOLS

    def test_default_mode_is_standard(self, monkeypatch):
        """GOVERNANCE_TOOL_MODE unset means the ten-tool surface."""
        import importlib

        import src.tool_modes as tool_modes

        monkeypatch.delenv("GOVERNANCE_TOOL_MODE", raising=False)
        reloaded = importlib.reload(tool_modes)
        try:
            assert reloaded.TOOL_MODE == "standard"
            assert reloaded.get_tools_for_mode(reloaded.TOOL_MODE) == (
                reloaded.STANDARD_MODE_TOOLS
            )
        finally:
            importlib.reload(tool_modes)

    def test_modes_nest_from_minimal_through_lite(self):
        """Widening never drops a name a narrower profile advertised."""
        assert MINIMAL_MODE_TOOLS < STANDARD_MODE_TOOLS < LITE_MODE_TOOLS

    def test_lite_mode_superset_of_minimal(self):
        """Widening the mode never drops a tool from the checkpoint loop."""
        assert MINIMAL_MODE_TOOLS <= LITE_MODE_TOOLS

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
        assert len(MINIMAL_MODE_TOOLS) < len(STANDARD_MODE_TOOLS) < len(LITE_MODE_TOOLS)


# --- get_tools_for_mode Tests ---


class TestGetToolsForMode:
    """Tests for get_tools_for_mode()."""

    def test_minimal_mode(self):
        tools = get_tools_for_mode("minimal")
        assert tools == MINIMAL_MODE_TOOLS

    def test_standard_mode(self):
        tools = get_tools_for_mode("standard")
        assert tools == STANDARD_MODE_TOOLS

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

    def test_discovery_tools_follow_the_mode_set(self):
        """Nothing is force-included: list_tools/describe_tool are ordinary
        members of lite and full, and absent from minimal by design."""
        assert should_include_tool("list_tools", mode="minimal") is False
        assert should_include_tool("describe_tool", mode="minimal") is False
        assert should_include_tool("list_tools", mode="lite") is True
        assert should_include_tool("describe_tool", mode="lite") is True
        assert should_include_tool("list_tools", mode="full") is True

    def test_minimal_advertises_only_the_five(self):
        for name in ("start_session", "identity", "sync_state",
                     "record_result", "check_working_state"):
            assert should_include_tool(name, mode="minimal") is True
        for name in ("onboard", "knowledge", "self_recovery", "health_check",
                     "search_shared_memory", "request_review", "consult"):
            assert should_include_tool(name, mode="minimal") is False

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
        assert TOOL_OPERATIONS["observe"] == "read"

    def test_write_operations(self):
        assert TOOL_OPERATIONS["process_agent_update"] == "write"
        assert TOOL_OPERATIONS["knowledge"] == "write"
        assert TOOL_OPERATIONS["agent"] == "write"

    def test_admin_operations(self):
        assert TOOL_OPERATIONS["admin"] == "admin"
        assert TOOL_OPERATIONS["operator_resume_agent"] == "admin"

    def test_legacy_alias_narrower_than_its_router_declares_its_own_class(self):
        """TOOL_OPERATIONS is keyed by the roster only (2026-09-07); a legacy
        alias that pins a read action of a write router says so on its entry."""
        from src.mcp_handlers.tool_stability import list_all_aliases

        aliases = list_all_aliases()
        assert "list_agents" not in TOOL_OPERATIONS
        assert aliases["list_agents"].operation == "read"
        assert TOOL_OPERATIONS[aliases["list_agents"].new_name] == "write"
        assert aliases["cleanup_stale_locks"].operation is None  # same class as admin

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

    def test_degrades_when_psutil_is_absent(self, monkeypatch):
        """psutil is an OPTIONAL dependency; a core install must still list tools.

        Until 2026-09-08 the process walk lived in a try whose except clause
        named ``psutil.NoSuchProcess`` while ``import psutil`` inside that same
        try bound the name as a function local. On ImportError the except tuple
        itself raised UnboundLocalError, which propagated out of
        should_include_tool and get_public_tool_definitions -- tools/list
        crashed outright instead of degrading to "not Claude Desktop".
        """
        import builtins

        real_import = builtins.__import__

        def _no_psutil(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("psutil is not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_psutil)
        monkeypatch.delitem(sys.modules, "psutil", raising=False)

        assert is_claude_desktop_client() is False
        # and the listing predicate above it stays usable
        assert should_include_tool("sync_state", mode="standard") is True

    def test_env_detection_survives_missing_psutil(self, monkeypatch):
        """The env-var check is not best-effort and must be reached regardless."""
        import builtins

        real_import = builtins.__import__

        def _no_psutil(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("psutil is not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_psutil)
        monkeypatch.delitem(sys.modules, "psutil", raising=False)
        monkeypatch.setenv("CLAUDE_DESKTOP", "1")

        assert is_claude_desktop_client() is True

    def test_empty_exclusion_set_skips_the_process_walk(self, monkeypatch):
        """should_include_tool runs once per included tool; the walk is uncached.

        While CLAUDE_DESKTOP_EXCLUDED_TOOLS is empty the detection decides
        nothing, so it must not run at all.
        """
        import src.tool_modes as tool_modes

        calls = []
        monkeypatch.setattr(
            tool_modes,
            "is_claude_desktop_client",
            lambda: calls.append(1) or True,
        )
        assert tool_modes.CLAUDE_DESKTOP_EXCLUDED_TOOLS == set()
        tool_modes.should_include_tool("sync_state", mode="standard")
        assert calls == []

    def test_exclusion_still_applies_when_the_set_is_populated(self, monkeypatch):
        """The mechanism stays wired: the guard is a fast path, not a removal."""
        import src.tool_modes as tool_modes

        monkeypatch.setattr(
            tool_modes, "CLAUDE_DESKTOP_EXCLUDED_TOOLS", {"sync_state"}
        )
        monkeypatch.setattr(tool_modes, "is_claude_desktop_client", lambda: True)
        assert tool_modes.should_include_tool("sync_state", mode="standard") is False
        assert (
            tool_modes.should_include_tool(
                "sync_state", mode="standard", client_type="other"
            )
            is True
        )


# --- Server instructions Tests ---


class TestServerInstructions:
    """The MCP `instructions` string is the one in-band surface description.

    A mode filters tools/list, so an unadvertised capability is unreachable for
    a schema-driven client. `instructions` reaches every client in the
    initialize response, before any tool call, and is where a narrow profile
    says what else the server does.
    """

    def test_names_the_workflow_on_every_profile(self):
        for mode in ("minimal", "standard", "lite", "full"):
            text = build_server_instructions(mode)
            for name in ("start_session", "sync_state", "record_result",
                         "check_working_state"):
                assert name in text, f"{name} missing from {mode} instructions"

    def test_narrow_profiles_name_what_they_do_not_advertise(self):
        minimal = build_server_instructions("minimal")
        # The capabilities minimal hides are named, so an agent can ask for them.
        for name in ("search_shared_memory", "store_finding", "update_finding",
                     "request_review", "consult"):
            assert name in minimal
        assert "callable by name" in minimal
        assert "GOVERNANCE_TOOL_MODE=lite or full" in minimal

        standard = build_server_instructions("standard")
        # Standard advertises those five, so it points at the routers instead.
        assert "list_tools" in standard
        assert "knowledge" in standard

    def test_reports_the_advertised_count_of_the_profile(self):
        assert "advertises 5 tools" in build_server_instructions("minimal")
        assert "advertises 10 tools" in build_server_instructions("standard")
        assert (
            f"advertises {len(LITE_MODE_TOOLS)} tools"
            in build_server_instructions("lite")
        )

    def test_full_claims_no_hidden_surface(self):
        text = build_server_instructions("full")
        assert "every registered tool" in text
        assert "Not listed here" not in text

    def test_no_empty_clause_on_any_known_mode(self):
        """A profile with nothing to disclose must not emit a dangling list."""
        for mode in ("minimal", "standard", "lite", "full",
                     "operator_readonly", "operator_recovery"):
            text = build_server_instructions(mode)
            assert ": ." not in text, f"empty disclosure clause in {mode}"
            for line in text.splitlines():
                assert "  " not in line, f"double space in {mode}: {line!r}"
                assert not line.endswith(" ")

    def test_defaults_to_the_servers_own_mode(self):
        assert build_server_instructions() == build_server_instructions(TOOL_MODE)

    def test_is_registry_free(self, monkeypatch):
        """It runs while the server object is built, before handlers import.

        Touching the tool registry there would be a circular import, so the
        string must come from the static mode sets alone.
        """
        import src.tool_modes as tool_modes

        def _explode():  # pragma: no cover - only runs on regression
            raise AssertionError("build_server_instructions touched the registry")

        monkeypatch.setattr(tool_modes, "advertised_tool_names_full", _explode)
        assert tool_modes.build_server_instructions("standard")
