"""
Tests for action_router decorator and consolidated handler dispatch.

Tests the mechanics of src/mcp_handlers/decorators.action_router and
the consolidated handlers defined in src/mcp_handlers/consolidated.py.
"""

import json
import pytest
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from mcp.types import TextContent
from src.mcp_handlers.decorators import action_router, _TOOL_DEFINITIONS


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_registry():
    """Snapshot and restore the tool registry around every test."""
    orig = dict(_TOOL_DEFINITIONS)
    yield
    _TOOL_DEFINITIONS.clear()
    _TOOL_DEFINITIONS.update(orig)


def _ok_response(payload=None):
    """Build a list containing one TextContent with JSON body."""
    body = payload or {"ok": True}
    return [TextContent(type="text", text=json.dumps(body))]


def _make_mock_handler(return_payload=None):
    """Create an AsyncMock that returns a well-formed TextContent list."""
    return AsyncMock(return_value=_ok_response(return_payload))


def _parse_response(result):
    """Extract parsed JSON from a router result (list of TextContent)."""
    assert isinstance(result, list) and len(result) >= 1
    text = result[0].text if hasattr(result[0], "text") else str(result[0])
    return json.loads(text)


def _get_router_actions(consolidated_handler):
    """
    Access the closure 'actions' dict from a consolidated handler.

    The handler chain is: mcp_tool wrapper -> router (closure with actions dict).
    We reach the inner 'router' via __wrapped__, then read its closure cells.
    """
    inner = consolidated_handler.__wrapped__
    freevars = inner.__code__.co_freevars
    idx = list(freevars).index("actions")
    return inner.__closure__[idx].cell_contents


@contextmanager
def _patch_router_action(consolidated_handler, action_name, mock_handler):
    """
    Temporarily replace a handler in a consolidated router's actions dict.

    This patches the closure directly since module-level patching cannot
    affect references already captured in the action_router closure.
    """
    actions = _get_router_actions(consolidated_handler)
    original = actions[action_name]
    actions[action_name] = mock_handler
    try:
        yield mock_handler
    finally:
        actions[action_name] = original


# ===========================================================================
# 1. action_router mechanics (fresh test routers with mock handlers)
# ===========================================================================

class TestActionRouterDispatch:
    """Core dispatch logic: valid/invalid/missing actions, defaults, case."""

    @pytest.mark.asyncio
    async def test_valid_action_dispatches_to_correct_handler(self):
        handler_a = _make_mock_handler({"handler": "a"})
        handler_b = _make_mock_handler({"handler": "b"})

        router = action_router(
            "test_dispatch_1",
            actions={"alpha": handler_a, "beta": handler_b},
        )

        result = await router({"action": "alpha"})
        data = _parse_response(result)
        assert data["handler"] == "a"
        handler_a.assert_awaited_once()
        handler_b.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_valid_action_dispatches_second_handler(self):
        handler_a = _make_mock_handler({"handler": "a"})
        handler_b = _make_mock_handler({"handler": "b"})

        router = action_router(
            "test_dispatch_2",
            actions={"alpha": handler_a, "beta": handler_b},
        )

        result = await router({"action": "beta"})
        data = _parse_response(result)
        assert data["handler"] == "b"
        handler_b.assert_awaited_once()
        handler_a.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_action_returns_error(self):
        handler = _make_mock_handler()
        router = action_router(
            "test_missing_action",
            actions={"do_it": handler},
        )

        result = await router({})
        data = _parse_response(result)
        assert data["success"] is False
        assert "action" in data["error"].lower()
        assert "valid_actions" in data.get("recovery", {})

    @pytest.mark.asyncio
    async def test_invalid_action_returns_error(self):
        handler = _make_mock_handler()
        router = action_router(
            "test_invalid_action",
            actions={"valid_one": handler},
        )

        result = await router({"action": "bogus"})
        data = _parse_response(result)
        assert data["success"] is False
        assert "bogus" in data["error"].lower() or "unknown" in data["error"].lower()
        assert "valid_actions" in data.get("recovery", {})

    @pytest.mark.asyncio
    async def test_invalid_action_lists_valid_options(self):
        handler = _make_mock_handler()
        router = action_router(
            "test_invalid_action_list",
            actions={"alpha": handler, "beta": handler, "gamma": handler},
        )

        result = await router({"action": "nope"})
        data = _parse_response(result)
        valid = data["recovery"]["valid_actions"]
        assert sorted(valid) == ["alpha", "beta", "gamma"]

    @pytest.mark.asyncio
    async def test_default_action_used_when_action_omitted(self):
        handler_default = _make_mock_handler({"used": "default"})
        handler_other = _make_mock_handler({"used": "other"})

        router = action_router(
            "test_default",
            actions={"check": handler_default, "rebuild": handler_other},
            default_action="check",
        )

        result = await router({})
        data = _parse_response(result)
        assert data["used"] == "default"
        handler_default.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_default_action_used_when_action_empty_string(self):
        handler_default = _make_mock_handler({"used": "default"})
        router = action_router(
            "test_default_empty",
            actions={"check": handler_default},
            default_action="check",
        )

        result = await router({"action": ""})
        data = _parse_response(result)
        assert data["used"] == "default"

    @pytest.mark.asyncio
    async def test_action_is_case_insensitive(self):
        handler = _make_mock_handler({"matched": True})
        router = action_router(
            "test_case",
            actions={"lowercase": handler},
        )

        result = await router({"action": "LOWERCASE"})
        data = _parse_response(result)
        assert data["matched"] is True
        handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_action_mixed_case(self):
        handler = _make_mock_handler({"matched": True})
        router = action_router(
            "test_case_mixed",
            actions={"myaction": handler},
        )

        result = await router({"action": "MyAction"})
        data = _parse_response(result)
        assert data["matched"] is True

    @pytest.mark.asyncio
    async def test_arguments_passed_to_handler(self):
        handler = AsyncMock(return_value=_ok_response())
        router = action_router(
            "test_passthrough",
            actions={"go": handler},
        )

        await router({"action": "go", "foo": "bar", "count": 42})
        args_passed = handler.call_args[0][0]
        assert args_passed["foo"] == "bar"
        assert args_passed["count"] == 42


class TestActionRouterParamMaps:
    """Parameter remapping logic."""

    @pytest.mark.asyncio
    async def test_param_map_remaps_parameter(self):
        handler = AsyncMock(return_value=_ok_response())
        router = action_router(
            "test_param_remap",
            actions={"search": handler},
            param_maps={"search": {"query": "search_query"}},
        )

        await router({"action": "search", "query": "test query"})
        args_passed = handler.call_args[0][0]
        assert args_passed["search_query"] == "test query"
        # Original key is also preserved
        assert args_passed["query"] == "test query"

    @pytest.mark.asyncio
    async def test_param_map_does_not_overwrite_existing_dst(self):
        handler = AsyncMock(return_value=_ok_response())
        router = action_router(
            "test_param_no_overwrite",
            actions={"search": handler},
            param_maps={"search": {"query": "search_query"}},
        )

        await router({"action": "search", "query": "from_query", "search_query": "already_set"})
        args_passed = handler.call_args[0][0]
        # Existing destination value should NOT be overwritten
        assert args_passed["search_query"] == "already_set"

    @pytest.mark.asyncio
    async def test_param_map_only_applies_to_matching_action(self):
        handler_a = AsyncMock(return_value=_ok_response())
        handler_b = AsyncMock(return_value=_ok_response())
        router = action_router(
            "test_param_action_scope",
            actions={"search": handler_a, "list": handler_b},
            param_maps={"search": {"query": "search_query"}},
        )

        await router({"action": "list", "query": "should_not_remap"})
        args_passed = handler_b.call_args[0][0]
        assert "search_query" not in args_passed

    @pytest.mark.asyncio
    async def test_multiple_param_maps_for_same_action(self):
        handler = AsyncMock(return_value=_ok_response())
        router = action_router(
            "test_multi_param",
            actions={"do": handler},
            param_maps={"do": {"a": "x", "b": "y"}},
        )

        await router({"action": "do", "a": 1, "b": 2})
        args_passed = handler.call_args[0][0]
        assert args_passed["x"] == 1
        assert args_passed["y"] == 2


class TestActionRouterRegistration:
    """Verify action_router registers the tool in _TOOL_DEFINITIONS."""

    def test_router_registers_in_tool_definitions(self):
        handler = _make_mock_handler()
        action_router(
            "test_reg_check",
            actions={"ping": handler},
            description="Test registration",
        )
        assert "test_reg_check" in _TOOL_DEFINITIONS

    def test_router_uses_provided_description(self):
        handler = _make_mock_handler()
        action_router(
            "test_reg_desc",
            actions={"ping": handler},
            description="My description",
        )
        td = _TOOL_DEFINITIONS["test_reg_desc"]
        assert td.description == "My description"

    def test_router_uses_provided_timeout(self):
        handler = _make_mock_handler()
        action_router(
            "test_reg_timeout",
            actions={"ping": handler},
            timeout=99.0,
        )
        td = _TOOL_DEFINITIONS["test_reg_timeout"]
        assert td.timeout == 99.0


class TestActionRouterErrorHandling:
    """Handler exceptions are caught by the mcp_tool wrapper."""

    @pytest.mark.asyncio
    async def test_handler_exception_returns_error(self):
        handler = AsyncMock(side_effect=RuntimeError("boom"))
        router = action_router(
            "test_exception",
            actions={"explode": handler},
        )

        result = await router({"action": "explode"})
        data = _parse_response(result)
        assert data["success"] is False
        assert "boom" in data["error"].lower() or "error" in data["error"].lower()


# ===========================================================================
# 2. Consolidated handler registration verification
# ===========================================================================

class TestConsolidatedRegistration:
    """Verify all consolidated handlers are registered after module import."""

    def test_knowledge_registered(self):
        from src.mcp_handlers import consolidated  # noqa: F401
        assert "knowledge" in _TOOL_DEFINITIONS

    def test_agent_registered(self):
        from src.mcp_handlers import consolidated  # noqa: F401
        assert "agent" in _TOOL_DEFINITIONS

    def test_calibration_registered(self):
        from src.mcp_handlers import consolidated  # noqa: F401
        assert "calibration" in _TOOL_DEFINITIONS

    def test_config_registered(self):
        from src.mcp_handlers import consolidated  # noqa: F401
        assert "config" in _TOOL_DEFINITIONS

    def test_export_registered(self):
        from src.mcp_handlers import consolidated  # noqa: F401
        assert "export" in _TOOL_DEFINITIONS

    def test_observe_registered(self):
        from src.mcp_handlers import consolidated  # noqa: F401
        assert "observe" in _TOOL_DEFINITIONS

    def test_pi_registered(self):
        """``pi`` is registered only when the ``unitares_pi_plugin`` is
        installed AND its ``register()`` has run. Under pytest we don't
        invoke the plugin loader, so skip when the plugin is absent.
        """
        pytest.importorskip("unitares_pi_plugin")
        unitares_pi_plugin = pytest.importorskip("unitares_pi_plugin")
        unitares_pi_plugin.register()
        assert "pi" in _TOOL_DEFINITIONS

    def test_dialectic_registered(self):
        from src.mcp_handlers import consolidated  # noqa: F401
        assert "dialectic" in _TOOL_DEFINITIONS


# ===========================================================================
# 3. Consolidated handler error paths and delegation
#    Uses _patch_router_action to swap handlers in the closure directly.
# ===========================================================================

class TestKnowledgeHandler:
    """Tests for the knowledge consolidated handler."""

    @pytest.mark.asyncio
    async def test_missing_action_returns_error(self):
        from src.mcp_handlers.consolidated import handle_knowledge
        result = await handle_knowledge({})
        data = _parse_response(result)
        assert data["success"] is False
        assert "valid_actions" in data.get("recovery", {})

    @pytest.mark.asyncio
    async def test_invalid_action_returns_error(self):
        from src.mcp_handlers.consolidated import handle_knowledge
        result = await handle_knowledge({"action": "nonexistent"})
        data = _parse_response(result)
        assert data["success"] is False
        assert "unknown" in data["error"].lower() or "nonexistent" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_valid_action_delegates(self):
        from src.mcp_handlers.consolidated import handle_knowledge
        mock_search = _make_mock_handler({"results": []})
        with _patch_router_action(handle_knowledge, "search", mock_search):
            result = await handle_knowledge({"action": "search", "query": "test"})
            data = _parse_response(result)
            assert data["results"] == []
            mock_search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handler_failure_propagates(self):
        from src.mcp_handlers.consolidated import handle_knowledge
        failing = AsyncMock(side_effect=RuntimeError("db down"))
        with _patch_router_action(handle_knowledge, "store", failing):
            result = await handle_knowledge({"action": "store", "summary": "x"})
            data = _parse_response(result)
            assert data["success"] is False

    @pytest.mark.asyncio
    async def test_valid_actions_list_complete(self):
        from src.mcp_handlers.consolidated import handle_knowledge
        result = await handle_knowledge({"action": "bad"})
        data = _parse_response(result)
        valid = sorted(data["recovery"]["valid_actions"])
        expected = sorted(["store", "search", "get", "list", "update",
                           "details", "note", "cleanup", "synthesize", "stats", "supersede", "audit"])
        assert valid == expected


class TestAgentHandler:
    """Tests for the agent consolidated handler."""

    @pytest.mark.asyncio
    async def test_missing_action_returns_error(self):
        from src.mcp_handlers.consolidated import handle_agent
        result = await handle_agent({})
        data = _parse_response(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_invalid_action_returns_error(self):
        from src.mcp_handlers.consolidated import handle_agent
        result = await handle_agent({"action": "fly"})
        data = _parse_response(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_list_delegates(self):
        from src.mcp_handlers.consolidated import handle_agent
        mock_list = _make_mock_handler({"agents": []})
        with _patch_router_action(handle_agent, "list", mock_list):
            result = await handle_agent({"action": "list"})
            data = _parse_response(result)
            assert data["agents"] == []
            mock_list.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_valid_actions_list_complete(self):
        from src.mcp_handlers.consolidated import handle_agent
        result = await handle_agent({"action": "bad"})
        data = _parse_response(result)
        valid = sorted(data["recovery"]["valid_actions"])
        expected = sorted(["list", "get", "update", "archive", "resume", "delete"])
        assert valid == expected


class TestCalibrationHandler:
    """Tests for the calibration consolidated handler (has default_action='check')."""

    @pytest.mark.asyncio
    async def test_default_action_is_check(self):
        from src.mcp_handlers.consolidated import handle_calibration
        mock_check = _make_mock_handler({"calibration": "ok"})
        with _patch_router_action(handle_calibration, "check", mock_check):
            result = await handle_calibration({})
            data = _parse_response(result)
            assert data["calibration"] == "ok"
            mock_check.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_action_returns_error(self):
        from src.mcp_handlers.consolidated import handle_calibration
        result = await handle_calibration({"action": "nope"})
        data = _parse_response(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_explicit_action_overrides_default(self):
        from src.mcp_handlers.consolidated import handle_calibration
        mock_rebuild = _make_mock_handler({"rebuilt": True})
        with _patch_router_action(handle_calibration, "rebuild", mock_rebuild):
            result = await handle_calibration({"action": "rebuild"})
            data = _parse_response(result)
            assert data["rebuilt"] is True
            mock_rebuild.assert_awaited_once()


class TestConfigHandler:
    """Tests for the config consolidated handler (has default_action='get')."""

    @pytest.mark.asyncio
    async def test_default_action_is_get(self):
        from src.mcp_handlers.consolidated import handle_config
        mock_get = _make_mock_handler({"thresholds": {}})
        with _patch_router_action(handle_config, "get", mock_get):
            result = await handle_config({})
            data = _parse_response(result)
            assert data["thresholds"] == {}
            mock_get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_action_returns_error(self):
        from src.mcp_handlers.consolidated import handle_config
        result = await handle_config({"action": "delete"})
        data = _parse_response(result)
        assert data["success"] is False


class TestExportHandler:
    """Tests for the export consolidated handler (has default_action='history')."""

    @pytest.mark.asyncio
    async def test_default_action_is_history(self):
        from src.mcp_handlers.consolidated import handle_export
        mock_hist = _make_mock_handler({"history": []})
        with _patch_router_action(handle_export, "history", mock_hist):
            result = await handle_export({})
            data = _parse_response(result)
            assert data["history"] == []
            mock_hist.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_action_returns_error(self):
        from src.mcp_handlers.consolidated import handle_export
        result = await handle_export({"action": "shred"})
        data = _parse_response(result)
        assert data["success"] is False


class TestObserveHandler:
    """Tests for the observe consolidated handler."""

    @pytest.mark.asyncio
    async def test_missing_action_returns_error(self):
        from src.mcp_handlers.consolidated import handle_observe
        result = await handle_observe({})
        data = _parse_response(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_invalid_action_returns_error(self):
        from src.mcp_handlers.consolidated import handle_observe
        result = await handle_observe({"action": "spy"})
        data = _parse_response(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_valid_actions_list_complete(self):
        from src.mcp_handlers.consolidated import handle_observe
        result = await handle_observe({"action": "bad"})
        data = _parse_response(result)
        valid = sorted(data["recovery"]["valid_actions"])
        expected = sorted(["agent", "compare", "similar", "anomalies",
                           "aggregate", "telemetry", "audit_events"])
        assert valid == expected


@pytest.fixture
def _pi_handler():
    """``pi`` action router lives in unitares-pi-plugin as of Phase B1.

    Plugin's ``register()`` builds and registers the router; this fixture
    returns it, or skips the whole TestPiHandler class when the plugin
    isn't installed.
    """
    plugin = pytest.importorskip("unitares_pi_plugin")
    plugin.register()
    handler = _TOOL_DEFINITIONS["pi"].handler
    yield handler


class TestPiHandler:
    """Tests for the pi consolidated handler (requires unitares-pi-plugin)."""

    @pytest.mark.asyncio
    async def test_missing_action_returns_error(self, _pi_handler):
        result = await _pi_handler({})
        data = _parse_response(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_invalid_action_returns_error(self, _pi_handler):
        result = await _pi_handler({"action": "self_destruct"})
        data = _parse_response(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_valid_action_delegates(self, _pi_handler):
        mock_health = _make_mock_handler({"status": "healthy"})
        with _patch_router_action(_pi_handler, "health", mock_health):
            result = await _pi_handler({"action": "health"})
            data = _parse_response(result)
            assert data["status"] == "healthy"
            mock_health.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_valid_actions_list_complete(self, _pi_handler):
        result = await _pi_handler({"action": "bad"})
        data = _parse_response(result)
        valid = sorted(data["recovery"]["valid_actions"])
        expected = sorted(["tools", "context", "health", "sync_eisv", "display",
                           "say", "message", "qa", "query", "workflow",
                           "git_pull", "power"])
        assert valid == expected


class TestDialecticHandler:
    """Tests for the dialectic consolidated handler (has default_action='list')."""

    @pytest.mark.asyncio
    async def test_default_action_is_list(self):
        from src.mcp_handlers.consolidated import handle_dialectic
        mock_list = _make_mock_handler({"sessions": []})
        with _patch_router_action(handle_dialectic, "list", mock_list):
            result = await handle_dialectic({})
            data = _parse_response(result)
            assert data["sessions"] == []
            mock_list.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_action_returns_error(self):
        from src.mcp_handlers.consolidated import handle_dialectic
        result = await handle_dialectic({"action": "destroy"})
        data = _parse_response(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_get_action_delegates(self):
        from src.mcp_handlers.consolidated import handle_dialectic
        mock_get = _make_mock_handler({"session": {"id": "abc123"}})
        with _patch_router_action(handle_dialectic, "get", mock_get):
            result = await handle_dialectic({"action": "get", "session_id": "abc123"})
            data = _parse_response(result)
            assert data["session"]["id"] == "abc123"
            mock_get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_quick_action_triages_without_session(self):
        from src.mcp_handlers.consolidated import handle_dialectic

        result = await handle_dialectic({
            "action": "quick",
            "issue_description": "Should I merge this small refactor?",
            "position": "Proceed after focused tests pass",
            "concerns": ["limited blast radius"],
            "proposed_conditions": ["focused tests pass"],
        })

        data = _parse_response(result)
        assert data["success"] is True
        assert data["mode"] == "quick_dialectic"
        assert data["full_session_created"] is False
        assert data["recommendation"] == "record_decision"

    @pytest.mark.asyncio
    async def test_quick_action_escalates_high_risk(self):
        from src.mcp_handlers.consolidated import handle_dialectic

        result = await handle_dialectic({
            "action": "quick",
            "issue_description": "Paused agent wants to delete credential state",
            "position": "Proceed",
            "observed_metrics": {"risk_score": 0.8},
        })

        data = _parse_response(result)
        assert data["success"] is True
        assert data["recommendation"] == "escalate_full_dialectic"
        assert data["escalation_tool"] == "dialectic(action='request')"
        assert "issue_contains_high_risk_terms" in data["risk_flags"]

    @pytest.mark.asyncio
    async def test_quick_action_escalates_high_risk_in_position_or_concerns(self):
        from src.mcp_handlers.consolidated import handle_dialectic

        result = await handle_dialectic({
            "action": "quick",
            "issue_description": "Should I proceed?",
            "position": "Delete credential state after the run",
            "concerns": ["Possible data loss if the cache is wrong"],
        })

        data = _parse_response(result)
        assert data["success"] is True
        assert data["recommendation"] == "escalate_full_dialectic"
        assert "decision_context_contains_high_risk_terms" in data["risk_flags"]


# ===========================================================================
# 4. Parameter mapping on real consolidated handlers
# ===========================================================================

class TestKnowledgeParamMaps:
    """Verify parameter remapping in the knowledge consolidated handler."""

    @pytest.mark.asyncio
    async def test_search_maps_query_to_search_query(self):
        from src.mcp_handlers.consolidated import handle_knowledge
        mock_search = AsyncMock(return_value=_ok_response({"results": []}))
        with _patch_router_action(handle_knowledge, "search", mock_search):
            await handle_knowledge({"action": "search", "query": "auth bugs"})
            args_passed = mock_search.call_args[0][0]
            assert args_passed["search_query"] == "auth bugs"

    @pytest.mark.asyncio
    async def test_note_maps_content_to_note(self):
        from src.mcp_handlers.consolidated import handle_knowledge
        mock_note = AsyncMock(return_value=_ok_response({"noted": True}))
        with _patch_router_action(handle_knowledge, "note", mock_note):
            await handle_knowledge({"action": "note", "content": "remember this"})
            args_passed = mock_note.call_args[0][0]
            assert args_passed["note"] == "remember this"

    @pytest.mark.asyncio
    async def test_update_maps_content_to_details(self):
        from src.mcp_handlers.consolidated import handle_knowledge
        mock_update = AsyncMock(return_value=_ok_response({"updated": True}))
        with _patch_router_action(handle_knowledge, "update", mock_update):
            await handle_knowledge({"action": "update", "discovery_id": "disc-1", "content": "remember this"})
            args_passed = mock_update.call_args[0][0]
            assert args_passed["details"] == "remember this"

    @pytest.mark.asyncio
    async def test_search_does_not_overwrite_existing_search_query(self):
        from src.mcp_handlers.consolidated import handle_knowledge
        mock_search = AsyncMock(return_value=_ok_response({"results": []}))
        with _patch_router_action(handle_knowledge, "search", mock_search):
            await handle_knowledge({
                "action": "search",
                "query": "ignored",
                "search_query": "explicit",
            })
            args_passed = mock_search.call_args[0][0]
            assert args_passed["search_query"] == "explicit"

    @pytest.mark.asyncio
    async def test_cleanup_preserves_dry_run_false(self):
        from src.mcp_handlers.consolidated import handle_knowledge
        mock_cleanup = AsyncMock(return_value=_ok_response({"cleanup_result": {}}))
        with _patch_router_action(handle_knowledge, "cleanup", mock_cleanup):
            await handle_knowledge({"action": "cleanup", "dry_run": "false"})
            args_passed = mock_cleanup.call_args[0][0]
            assert args_passed["dry_run"] == "false"
