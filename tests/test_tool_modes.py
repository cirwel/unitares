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


class TestUnifiedCatalog:
    @pytest.mark.parametrize("mode", ["minimal", "standard", "lite", "full", "operator_readonly", "operator_recovery", "core", "unknown"])
    def test_legacy_modes_cannot_hide_capabilities(self, mode):
        from src.tool_modes import advertised_tool_names_full
        assert get_tools_for_mode(mode) == advertised_tool_names_full()
        for name in ("agent", "observe", "admin", "config", "list_tools", "dialectic", "self_recovery"):
            assert should_include_tool(name, mode=mode)

    @pytest.mark.parametrize("setting", [None, "minimal", "standard", "lite", "operator_readonly", "unknown"])
    def test_environment_cannot_fragment_the_catalog(self, monkeypatch, setting):
        import importlib
        import src.tool_modes as module
        if setting is None:
            monkeypatch.delenv("GOVERNANCE_TOOL_MODE", raising=False)
        else:
            monkeypatch.setenv("GOVERNANCE_TOOL_MODE", setting)
        importlib.reload(module)
        assert module.TOOL_MODE == "full"
        assert module.get_tools_for_mode() == module.advertised_tool_names_full()

    def test_returns_independent_sets(self):
        first = get_tools_for_mode("minimal")
        first.add("fake_tool")
        assert "fake_tool" not in get_tools_for_mode("minimal")
        assert not should_include_tool("fake_tool")

    def test_legacy_constants_share_one_first_party_catalog(self):
        from src.tool_meta import TOOL_META_BY_NAME
        for names in (MINIMAL_MODE_TOOLS, STANDARD_MODE_TOOLS, LITE_MODE_TOOLS,
                      OPERATOR_READONLY_MODE_TOOLS, OPERATOR_RECOVERY_MODE_TOOLS):
            assert names == set(TOOL_META_BY_NAME)


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

    def test_instructions_explain_one_catalog(self):
        for mode in ("minimal", "standard", "lite", "full"):
            text = build_server_instructions(mode)
            assert text == build_server_instructions()
            assert "every registered tool" in text
            assert "settings are ignored" in text
            assert "authorization" in text
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
