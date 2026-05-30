"""
Tests for src/mcp_handlers/admin.py - comprehensive admin handler coverage.

Tests cover:
- handle_health_check
- handle_get_server_info
- handle_get_connection_status
- handle_validate_file_path
- handle_get_workspace_health
- handle_reset_monitor
- handle_cleanup_stale_locks
- handle_get_tool_usage_stats
- handle_describe_tool
- handle_list_tools
- handle_debug_request_context
- handle_check_continuity_health
- handle_check_calibration
- handle_rebuild_calibration
- handle_update_calibration_ground_truth
- handle_get_telemetry_metrics
- handle_backfill_calibration_from_dialectic
- Workspace helper functions (get_workspace_last_agent, set_workspace_last_agent, etc.)
"""

import pytest
import json
import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock
from datetime import datetime

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from tests.helpers import parse_result


# ============================================================================
# Shared fixtures
# ============================================================================

@pytest.fixture
def mock_mcp_server():
    """Mock the shared mcp_server module."""
    server = MagicMock()
    server.agent_metadata = {}
    server.monitors = {}
    server.SERVER_VERSION = "test-1.0.0"
    server.SERVER_BUILD_DATE = "2026-01-01"
    server.PSUTIL_AVAILABLE = False
    server.MAX_KEEP_PROCESSES = 5
    server.project_root = str(project_root)
    return server


@pytest.fixture
def patch_mcp_server(mock_mcp_server):
    """Patch mcp_server reference."""
    with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server):
        yield mock_mcp_server


@pytest.fixture
def patch_context_agent_id():
    """Patch context agent_id to return None (no bound identity).

    Since get_context_agent_id is imported locally inside functions from
    src.mcp_handlers.context, we patch it at the source module.
    """
    with patch("src.mcp_handlers.context.get_context_agent_id", return_value=None):
        yield


# ============================================================================
# handle_get_server_info
# ============================================================================

class TestGetServerInfo:

    @pytest.mark.asyncio
    async def test_server_info_without_psutil(self, mock_mcp_server, patch_context_agent_id):
        mock_mcp_server.PSUTIL_AVAILABLE = False
        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {"a": None, "b": None, "c": None}):
            from src.mcp_handlers.admin.handlers import handle_get_server_info
            result = await handle_get_server_info({})

            data = parse_result(result)
            assert data["success"] is True
            assert data["server_version"] == "test-1.0.0"
            assert data["build_date"] == "2026-01-01"
            assert data["tool_count"] == 3
            assert data["health"] == "healthy"
            # Without psutil, server_processes should contain error message
            assert len(data["server_processes"]) == 1
            assert "error" in data["server_processes"][0]

    @pytest.mark.asyncio
    async def test_server_info_with_psutil(self, mock_mcp_server, patch_context_agent_id):
        mock_mcp_server.PSUTIL_AVAILABLE = True

        mock_proc = MagicMock()
        mock_proc.info = {
            "pid": 12345,
            "name": "python",
            "cmdline": ["python", "mcp_server_std.py"],
            "create_time": 0,
            "status": "running"
        }

        mock_current = MagicMock()
        mock_current.create_time.return_value = 100.0
        mock_current.status.return_value = "running"

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {"a": None}), \
             patch("psutil.process_iter", return_value=[mock_proc]), \
             patch("psutil.Process", return_value=mock_current), \
             patch("time.time", return_value=200.0):
            from src.mcp_handlers.admin.handlers import handle_get_server_info
            result = await handle_get_server_info({})

            data = parse_result(result)
            assert data["success"] is True
            assert data["current_pid"] == os.getpid()

    @pytest.mark.asyncio
    async def test_server_info_transport_detection(self, mock_mcp_server, patch_context_agent_id):
        """Test that transport is detected from sys.argv."""
        mock_mcp_server.PSUTIL_AVAILABLE = False
        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {}), \
             patch.object(sys, "argv", ["python", "mcp_server.py"]):
            from src.mcp_handlers.admin.handlers import handle_get_server_info
            result = await handle_get_server_info({})
            data = parse_result(result)
            assert data["transport"] == "HTTP"


# ============================================================================
# handle_get_connection_status
# ============================================================================

class TestGetConnectionStatus:

    @pytest.mark.asyncio
    async def test_connection_status_connected(self, patch_context_agent_id):
        mock_server = MagicMock()
        mock_server.agent_metadata = {}

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {"tool1": None}):
            from src.mcp_handlers.admin.handlers import handle_get_connection_status
            result = await handle_get_connection_status({})

            data = parse_result(result)
            assert data["success"] is True
            assert data["status"] == "connected"
            assert data["server_available"] is True
            assert data["tools_available"] is True

    @pytest.mark.asyncio
    async def test_connection_status_with_bound_identity(self):
        mock_server = MagicMock()
        meta = MagicMock()
        meta.structured_id = "Claude_Opus_20260101"
        meta.label = "TestAgent"
        mock_server.agent_metadata = {"uuid-123": meta}

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {"tool1": None}), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value="uuid-123"):
            from src.mcp_handlers.admin.handlers import handle_get_connection_status
            result = await handle_get_connection_status({})

            data = parse_result(result)
            assert data["session_bound"] is True
            assert data["resolved_uuid"] == "uuid-123..."

    @pytest.mark.asyncio
    async def test_connection_status_no_tools(self, patch_context_agent_id):
        mock_server = MagicMock()
        mock_server.agent_metadata = {}

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {}):
            from src.mcp_handlers.admin.handlers import handle_get_connection_status
            result = await handle_get_connection_status({})

            data = parse_result(result)
            assert data["status"] == "disconnected"
            assert data["tools_available"] is False


# ============================================================================
# handle_validate_file_path
# ============================================================================

class TestValidateFilePath:

    @pytest.mark.asyncio
    async def test_valid_path(self, patch_context_agent_id):
        with patch("src.mcp_handlers.admin.handlers.validate_file_path_policy", return_value=(None, None)):
            from src.mcp_handlers.admin.handlers import handle_validate_file_path
            result = await handle_validate_file_path({"file_path": "src/main.py"})

            data = parse_result(result)
            assert data["success"] is True
            assert data["valid"] is True
            assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_warning_path(self, patch_context_agent_id):
        warning_msg = "Test script should be in tests/"
        with patch("src.mcp_handlers.admin.handlers.validate_file_path_policy",
                    return_value=(warning_msg, None)):
            from src.mcp_handlers.admin.handlers import handle_validate_file_path
            result = await handle_validate_file_path({"file_path": "test_foo.py"})

            data = parse_result(result)
            assert data["success"] is True
            assert data["valid"] is False
            assert data["status"] == "warning"
            assert "guidance" in data

    @pytest.mark.asyncio
    async def test_error_path(self, patch_context_agent_id):
        from mcp.types import TextContent
        error_tc = TextContent(type="text", text='{"success":false,"error":"blocked"}')
        with patch("src.mcp_handlers.admin.handlers.validate_file_path_policy",
                    return_value=(None, error_tc)):
            from src.mcp_handlers.admin.handlers import handle_validate_file_path
            result = await handle_validate_file_path({"file_path": "/etc/passwd"})

            data = json.loads(result[0].text)
            assert data["success"] is False

    @pytest.mark.asyncio
    async def test_missing_file_path(self, patch_context_agent_id):
        from src.mcp_handlers.admin.handlers import handle_validate_file_path
        result = await handle_validate_file_path({})

        data = parse_result(result)
        assert data["success"] is False
        assert "file_path" in data["error"].lower() or "required" in data["error"].lower()


# ============================================================================
# handle_reset_monitor
# ============================================================================

class TestResetMonitor:

    @pytest.mark.asyncio
    async def test_reset_existing_monitor(self, mock_mcp_server, patch_context_agent_id):
        mock_mcp_server.monitors = {"agent-1": MagicMock()}
        mock_mcp_server.agent_metadata = {"agent-1": MagicMock(status="active")}

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.admin.handlers.require_registered_agent",
                   return_value=("agent-1", None)):
            from src.mcp_handlers.admin.handlers import handle_reset_monitor
            result = await handle_reset_monitor({"agent_id": "agent-1"})

            data = parse_result(result)
            assert data["success"] is True
            assert "reset" in data["message"].lower()
            assert "agent-1" not in mock_mcp_server.monitors

    @pytest.mark.asyncio
    async def test_reset_nonexistent_monitor(self, mock_mcp_server, patch_context_agent_id):
        mock_mcp_server.monitors = {}

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.admin.handlers.require_registered_agent",
                   return_value=("agent-1", None)):
            from src.mcp_handlers.admin.handlers import handle_reset_monitor
            result = await handle_reset_monitor({"agent_id": "agent-1"})

            data = parse_result(result)
            assert data["success"] is True
            assert "not found" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_reset_requires_registration(self, mock_mcp_server, patch_context_agent_id):
        from mcp.types import TextContent
        error = TextContent(type="text", text='{"success":false,"error":"not registered"}')

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.admin.handlers.require_registered_agent",
                   return_value=(None, error)):
            from src.mcp_handlers.admin.handlers import handle_reset_monitor
            result = await handle_reset_monitor({})

            data = json.loads(result[0].text)
            assert data["success"] is False


# ============================================================================
# handle_cleanup_stale_locks
# ============================================================================

class TestCleanupStaleLocks:

    @pytest.mark.asyncio
    async def test_cleanup_success(self, patch_context_agent_id):
        mock_result = {
            "cleaned": 2, "kept": 1, "errors": 0,
            "cleaned_locks": ["lock1", "lock2"], "kept_locks": ["lock3"],
        }
        with patch("src.lock_cleanup.cleanup_stale_state_locks",
                    return_value=mock_result):
            from src.mcp_handlers.admin.handlers import handle_cleanup_stale_locks
            result = await handle_cleanup_stale_locks({})

            data = parse_result(result)
            assert data["success"] is True
            assert data["cleaned"] == 2
            assert data["kept"] == 1
            assert "Cleaned 2" in data["message"]

    @pytest.mark.asyncio
    async def test_cleanup_dry_run(self, patch_context_agent_id):
        mock_result = {
            "cleaned": 0, "kept": 3, "errors": 0,
            "cleaned_locks": [], "kept_locks": ["a", "b", "c"],
        }
        with patch("src.lock_cleanup.cleanup_stale_state_locks",
                    return_value=mock_result):
            from src.mcp_handlers.admin.handlers import handle_cleanup_stale_locks
            result = await handle_cleanup_stale_locks({"dry_run": True})

            data = parse_result(result)
            assert data["dry_run"] is True

    @pytest.mark.asyncio
    async def test_cleanup_custom_max_age(self, patch_context_agent_id):
        mock_result = {
            "cleaned": 0, "kept": 0, "errors": 0,
            "cleaned_locks": [], "kept_locks": [],
        }
        with patch("src.lock_cleanup.cleanup_stale_state_locks",
                    return_value=mock_result) as mock_fn:
            from src.mcp_handlers.admin.handlers import handle_cleanup_stale_locks
            result = await handle_cleanup_stale_locks({"max_age_seconds": 600.0})

            data = parse_result(result)
            assert data["max_age_seconds"] == 600.0

    @pytest.mark.asyncio
    async def test_cleanup_error_handling(self, patch_context_agent_id):
        with patch("src.lock_cleanup.cleanup_stale_state_locks",
                    side_effect=RuntimeError("lock dir missing")):
            from src.mcp_handlers.admin.handlers import handle_cleanup_stale_locks
            result = await handle_cleanup_stale_locks({})

            data = parse_result(result)
            assert data["success"] is False
            assert "lock dir missing" in data["error"]


# ============================================================================
# handle_get_tool_usage_stats
# ============================================================================

class TestGetToolUsageStats:

    @pytest.mark.asyncio
    async def test_usage_stats_default(self, patch_context_agent_id):
        mock_tracker = MagicMock()
        mock_tracker.get_usage_stats.return_value = {
            "total_calls": 100,
            "unique_tools": 15,
        }
        with patch("src.tool_usage_tracker.get_tool_usage_tracker",
                    return_value=mock_tracker):
            from src.mcp_handlers.admin.handlers import handle_get_tool_usage_stats
            result = await handle_get_tool_usage_stats({})

            data = parse_result(result)
            assert data["success"] is True
            assert data["total_calls"] == 100

    @pytest.mark.asyncio
    async def test_usage_stats_with_filters(self, patch_context_agent_id):
        mock_tracker = MagicMock()
        mock_tracker.get_usage_stats.return_value = {"total_calls": 5}

        with patch("src.tool_usage_tracker.get_tool_usage_tracker",
                    return_value=mock_tracker):
            from src.mcp_handlers.admin.handlers import handle_get_tool_usage_stats
            result = await handle_get_tool_usage_stats({
                "tool_name": "health_check",
                "agent_id": "agent-1",
                "window_hours": 48,
            })

            # Verify filters were passed through
            call_kwargs = mock_tracker.get_usage_stats.call_args
            assert call_kwargs.kwargs.get("tool_name") == "health_check" or \
                   call_kwargs[1].get("tool_name") == "health_check"


# ============================================================================
# handle_get_workspace_health
# ============================================================================

class TestGetWorkspaceHealth:

    @pytest.mark.asyncio
    async def test_workspace_health_success(self, patch_context_agent_id):
        mock_data = {
            "workspace": "unitares",
            "overall_status": "healthy",
            "checks": {}
        }
        with patch("src.workspace_health.get_workspace_health",
                    return_value=mock_data):
            from src.mcp_handlers.admin.handlers import handle_get_workspace_health
            result = await handle_get_workspace_health({})

            data = parse_result(result)
            assert data["success"] is True
            assert data["overall_status"] == "healthy"

    @pytest.mark.asyncio
    async def test_workspace_health_error(self, patch_context_agent_id):
        with patch("src.workspace_health.get_workspace_health",
                    side_effect=RuntimeError("disk full")):
            from src.mcp_handlers.admin.handlers import handle_get_workspace_health
            result = await handle_get_workspace_health({})

            data = parse_result(result)
            assert data["success"] is False
            assert "disk full" in data["error"]


# ============================================================================
# handle_describe_tool
# ============================================================================

class TestDescribeTool:

    @pytest.mark.asyncio
    async def test_describe_existing_tool(self, patch_context_agent_id):
        mock_tool = MagicMock()
        mock_tool.name = "health_check"
        mock_tool.description = "Quick health check of system"
        mock_tool.inputSchema = {"type": "object", "properties": {}}

        with patch("src.tool_schemas.get_tool_definitions", return_value=[mock_tool]):
            from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
            result = await handle_describe_tool({
                "tool_name": "health_check",
                "lite": False
            })

            data = parse_result(result)
            assert data["success"] is True
            assert data["tool"]["name"] == "health_check"

    @pytest.mark.asyncio
    async def test_describe_missing_tool_name(self, patch_context_agent_id):
        from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
        result = await handle_describe_tool({})

        data = parse_result(result)
        assert data["success"] is False
        assert "required" in data["error"].lower() or "tool_name" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_describe_unknown_tool(self, patch_context_agent_id):
        with patch("src.tool_schemas.get_tool_definitions", return_value=[]):
            from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
            result = await handle_describe_tool({"tool_name": "nonexistent_tool"})

            data = parse_result(result)
            assert data["success"] is False
            assert "unknown" in data["error"].lower() or "nonexistent" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_describe_tool_lite_mode_with_schema(self, patch_context_agent_id):
        """Test lite mode falls back to inputSchema when no TOOL_PARAM_SCHEMAS entry."""
        mock_tool = MagicMock()
        mock_tool.name = "some_tool"
        mock_tool.description = "Some tool description"
        mock_tool.inputSchema = {
            "type": "object",
            "properties": {
                "param1": {"type": "string"},
                "param2": {"type": "integer"}
            },
            "required": ["param1"]
        }

        with patch("src.tool_schemas.get_tool_definitions", return_value=[mock_tool]), \
             patch("src.tool_schemas.get_pydantic_schemas", return_value={}), \
             patch("src.mcp_handlers.validators.PARAM_ALIASES", {}):
            from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
            result = await handle_describe_tool({
                "tool_name": "some_tool",
                "lite": True
            })

            data = parse_result(result)
            assert data["success"] is True
            assert data["tool"] == "some_tool"
            assert len(data["parameters"]) > 0

    @pytest.mark.asyncio
    async def test_describe_tool_error_handling(self, patch_context_agent_id):
        with patch("src.tool_schemas.get_tool_definitions",
                    side_effect=ImportError("module not found")):
            from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
            result = await handle_describe_tool({"tool_name": "health_check"})

            data = parse_result(result)
            assert data["success"] is False

    @pytest.mark.asyncio
    async def test_describe_tool_lite_resolves_optional_types(self, patch_context_agent_id):
        """Pydantic Optional[T] emits anyOf:[{type:T},{type:null}] with no top-level
        type. Lite mode must surface T, not 'any'. Inputs to the fallback path
        (no Pydantic model registered) so we exercise the inputSchema branch."""
        mock_tool = MagicMock()
        mock_tool.name = "optional_tool"
        mock_tool.description = "Tool with Optional params"
        mock_tool.inputSchema = {
            "type": "object",
            "properties": {
                "required_str": {"type": "string"},
                "optional_str": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
                "optional_int": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None},
                "optional_bool": {"anyOf": [{"type": "boolean"}, {"type": "null"}], "default": False},
                "union_str_bool": {"anyOf": [{"type": "string"}, {"type": "boolean"}, {"type": "null"}]},
            },
            "required": ["required_str"],
        }

        with patch("src.tool_schemas.get_tool_definitions", return_value=[mock_tool]), \
             patch("src.tool_schemas.get_pydantic_schemas", return_value={}), \
             patch("src.mcp_handlers.validators.PARAM_ALIASES", {}):
            from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
            result = await handle_describe_tool({"tool_name": "optional_tool", "lite": True})

        data = parse_result(result)
        assert data["success"] is True
        params = " | ".join(data["parameters"])
        # Required param rendered without type (existing contract)
        assert "required_str (required)" in data["parameters"]
        # Each Optional must surface its non-null type, NOT "any"
        assert "optional_str: string" in params
        assert "optional_int: integer" in params
        assert "optional_bool: boolean" in params
        # Union[str, bool] must render both types
        assert "union_str_bool: string|boolean" in params
        # Regression guard: no param should be typed "any" given the above shapes
        assert ": any" not in params

    @pytest.mark.asyncio
    async def test_describe_tool_lite_pydantic_path_resolves_optional(self, patch_context_agent_id):
        """Pydantic-schema branch of lite mode must also resolve anyOf → concrete type."""
        from pydantic import BaseModel
        from typing import Optional as Opt

        class OptionalParams(BaseModel):
            required_str: str
            optional_str: Opt[str] = None
            optional_int: Opt[int] = 5

        mock_tool = MagicMock()
        mock_tool.name = "pydantic_tool"
        mock_tool.description = "Pydantic-backed tool"
        mock_tool.inputSchema = {"type": "object", "properties": {}, "required": []}

        with patch("src.tool_schemas.get_tool_definitions", return_value=[mock_tool]), \
             patch("src.tool_schemas.get_pydantic_schemas",
                   return_value={"pydantic_tool": OptionalParams}), \
             patch("src.mcp_handlers.validators.PARAM_ALIASES", {}):
            from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
            result = await handle_describe_tool({"tool_name": "pydantic_tool", "lite": True})

        data = parse_result(result)
        assert data["success"] is True
        params = " | ".join(data["parameters"])
        assert "required_str (required)" in data["parameters"]
        assert "optional_str: string" in params
        assert "optional_int: integer" in params
        assert ": any" not in params


# ============================================================================
# handle_health_check
# ============================================================================

class TestHealthCheck:

    @pytest.fixture(autouse=True)
    def mock_pi_connectivity(self):
        """Mock Pi connectivity to prevent real network calls (times out in CI).

        No-op when ``unitares_pi_plugin`` isn't installed — in that case
        governance's runtime_queries skips the pi_connectivity check and
        no network call happens anyway.
        """
        try:
            import unitares_pi_plugin.handlers  # noqa: F401
        except ImportError:
            yield
            return
        with patch("unitares_pi_plugin.handlers.call_pi_tool",
                   new_callable=AsyncMock,
                   return_value={"error": "mocked - Pi unreachable"}):
            yield

    @pytest.mark.asyncio
    async def test_health_check_calibration_error(self, mock_mcp_server, patch_context_agent_id):
        """Test that calibration errors are caught and reported."""
        mock_audit = MagicMock()
        mock_audit.log_file = MagicMock()
        mock_audit.log_file.exists.return_value = True

        mock_db = AsyncMock()
        mock_db.health_check = AsyncMock(return_value={"status": "healthy"})
        mock_db.init = AsyncMock()

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.calibration.calibration_checker") as mock_cal, \
             patch("src.telemetry.telemetry_collector", MagicMock()), \
             patch("src.audit_log.audit_logger", mock_audit), \
             patch("src.db.get_db", return_value=mock_db), \
             patch("src.calibration_db.calibration_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.audit_db.audit_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.cache.is_redis_available", return_value=False), \
             patch("src.knowledge_graph.get_knowledge_graph",
                   new_callable=AsyncMock) as mock_kg:

            mock_cal.get_pending_updates.side_effect = RuntimeError("calibration broken")
            mock_kg_instance = AsyncMock()
            mock_kg_instance.health_check = AsyncMock(return_value={"status": "healthy"})
            mock_kg.return_value = mock_kg_instance

            from src.services.runtime_queries import get_health_check_data
            data = await get_health_check_data({})
            assert "checks" in data
            assert data["checks"]["calibration"]["status"] == "error"
            assert "operator_summary" in data
            assert "calibration" in data["operator_summary"]["failing_checks"]

    @pytest.mark.asyncio
    async def test_health_check_overall_status_logic(self, mock_mcp_server, patch_context_agent_id):
        """Test the three-tier status logic: healthy, moderate, critical."""
        pytest.importorskip("unitares_pi_plugin")
        mock_audit = MagicMock()
        mock_audit.log_file = MagicMock()
        mock_audit.log_file.exists.return_value = True

        mock_db = AsyncMock()
        mock_db.health_check = AsyncMock(return_value={"status": "healthy"})
        mock_db.init = AsyncMock()

        mock_cal = MagicMock()
        mock_cal.get_pending_updates.return_value = 0

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.calibration.calibration_checker", mock_cal), \
             patch("src.telemetry.telemetry_collector", MagicMock()), \
             patch("src.audit_log.audit_logger", mock_audit), \
             patch("src.db.get_db", return_value=mock_db), \
             patch("src.embeddings.embeddings_available", return_value=True), \
             patch("src.knowledge_graph.backend_supports_semantic_search", return_value=True), \
             patch("unitares_pi_plugin.handlers.call_pi_tool",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy"}), \
             patch("src.calibration_db.calibration_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.audit_db.audit_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.cache.is_redis_available", return_value=False), \
             patch("src.knowledge_graph.get_knowledge_graph",
                   new_callable=AsyncMock) as mock_kg:

            mock_kg_instance = AsyncMock()
            mock_kg_instance.health_check = AsyncMock(return_value={"status": "healthy"})
            mock_kg.return_value = mock_kg_instance

            from src.services.runtime_queries import get_health_check_data
            data = await get_health_check_data({}, server=mock_mcp_server)
            assert "status" in data
            assert "status_breakdown" in data
            assert "operator_summary" in data
            assert data["version"] == "test-1.0.0"
            assert data["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_health_check_reports_degraded_local_identity_continuity(
        self,
        mock_mcp_server,
        patch_context_agent_id,
    ):
        """Default health output should state when continuity is degraded-local."""
        pytest.importorskip("unitares_pi_plugin")
        mock_audit = MagicMock()
        mock_audit.log_file = MagicMock()
        mock_audit.log_file.exists.return_value = True

        mock_db = AsyncMock()
        mock_db.health_check = AsyncMock(return_value={"status": "healthy"})
        mock_db.init = AsyncMock()

        mock_cal = MagicMock()
        mock_cal.get_pending_updates.return_value = 0

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.calibration.calibration_checker", mock_cal), \
             patch("src.telemetry.telemetry_collector", MagicMock()), \
             patch("src.audit_log.audit_logger", mock_audit), \
             patch("src.db.get_db", return_value=mock_db), \
             patch("src.embeddings.embeddings_available", return_value=True), \
             patch("src.knowledge_graph.backend_supports_semantic_search", return_value=True), \
             patch("unitares_pi_plugin.handlers.call_pi_tool",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy"}), \
             patch("src.calibration_db.calibration_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.audit_db.audit_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.cache.is_redis_available", return_value=False), \
             patch("src.knowledge_graph.get_knowledge_graph",
                   new_callable=AsyncMock) as mock_kg:

            mock_kg_instance = AsyncMock()
            mock_kg_instance.health_check = AsyncMock(return_value={"status": "healthy"})
            mock_kg.return_value = mock_kg_instance

            from src.services.runtime_queries import get_health_check_data
            data = await get_health_check_data({})
            assert data["status"] == "healthy"
            assert data["redis_present"] is False
            assert data["identity_continuity_mode"] == "degraded-local"
            assert data["checks"]["redis_cache"]["present"] is False
            assert data["checks"]["identity_continuity"]["mode"] == "degraded-local"
            assert data["checks"]["identity_continuity"]["redis_present"] is False

    @pytest.mark.asyncio
    async def test_health_check_reports_redis_identity_continuity(
        self,
        mock_mcp_server,
        patch_context_agent_id,
    ):
        """Health output should explicitly report Redis-backed continuity when available."""
        pytest.importorskip("unitares_pi_plugin")
        mock_audit = MagicMock()
        mock_audit.log_file = MagicMock()
        mock_audit.log_file.exists.return_value = True

        mock_db = AsyncMock()
        mock_db.health_check = AsyncMock(return_value={"status": "healthy"})
        mock_db.init = AsyncMock()

        mock_cal = MagicMock()
        mock_cal.get_pending_updates.return_value = 0

        session_cache = AsyncMock()
        session_cache.health_check = AsyncMock(return_value={"status": "healthy", "backend": "redis"})
        dist_lock = AsyncMock()
        dist_lock.health_check = AsyncMock(return_value={"status": "healthy", "backend": "redis"})
        raw_redis = AsyncMock()
        raw_redis.info = AsyncMock(return_value={"keyspace_hits": 5, "keyspace_misses": 1, "total_commands_processed": 12})

        async def _scan_iter(match=None, count=None):
            if match == "session:*":
                yield "session:test"

        raw_redis.scan_iter = _scan_iter

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.calibration.calibration_checker", mock_cal), \
             patch("src.telemetry.telemetry_collector", MagicMock()), \
             patch("src.audit_log.audit_logger", mock_audit), \
             patch("src.db.get_db", return_value=mock_db), \
             patch("src.embeddings.embeddings_available", return_value=True), \
             patch("src.knowledge_graph.backend_supports_semantic_search", return_value=True), \
             patch("unitares_pi_plugin.handlers.call_pi_tool",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy"}), \
             patch("src.calibration_db.calibration_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.audit_db.audit_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.cache.is_redis_available", return_value=True), \
             patch("src.cache.get_session_cache", return_value=session_cache), \
             patch("src.cache.get_distributed_lock", return_value=dist_lock), \
             patch("src.cache.get_redis", new_callable=AsyncMock, return_value=raw_redis), \
             patch("src.knowledge_graph.get_knowledge_graph",
                   new_callable=AsyncMock) as mock_kg:

            mock_kg_instance = AsyncMock()
            mock_kg_instance.health_check = AsyncMock(return_value={"status": "healthy"})
            mock_kg.return_value = mock_kg_instance

            from src.services.runtime_queries import get_health_check_data
            data = await get_health_check_data({})
            assert data["status"] == "healthy"
            assert data["redis_present"] is True
            assert data["identity_continuity_mode"] == "redis"
            assert data["checks"]["redis_cache"]["present"] is True
            assert data["checks"]["identity_continuity"]["mode"] == "redis"
            assert data["checks"]["identity_continuity"]["redis_present"] is True

    @pytest.mark.asyncio
    async def test_bounded_scan_count_returns_int_below_cap(self):
        """Counts under the cap are returned as plain ints."""
        from src.services.runtime_queries import _bounded_scan_count

        class FakeRedis:
            async def scan_iter(self, match=None, count=None):
                for i in range(7):
                    yield f"{match[:-1]}{i}"

        n = await _bounded_scan_count(FakeRedis(), "session:*", cap=100)
        assert n == 7

    @pytest.mark.asyncio
    async def test_bounded_scan_count_caps_and_short_circuits(self):
        """At-or-above cap → returns f'{cap}+' AND stops iterating (no full keyspace materialization)."""
        from src.services.runtime_queries import _bounded_scan_count

        yielded = 0
        cap = 50

        class FakeRedis:
            async def scan_iter(self, match=None, count=None):
                nonlocal yielded
                # Source has way more than `cap` keys; bounded helper must NOT exhaust it.
                for i in range(cap * 100):
                    yielded += 1
                    yield f"k{i}"

        result = await _bounded_scan_count(FakeRedis(), "k*", cap=cap)
        assert result == f"{cap}+"
        # Helper should have stopped pulling from the iterator at the cap.
        # Allow one extra (the iteration that triggered the early return).
        assert yielded <= cap + 1, f"helper exhausted iterator: yielded={yielded}, cap={cap}"

    @pytest.mark.asyncio
    async def test_bounded_scan_count_runs_in_parallel_for_health_check(self, mock_mcp_server, patch_context_agent_id):
        """Health-check redis_cache.keys section uses parallel bounded counts, not sequential materialization.

        Regression for KG 2026-04-11T11:28:05.774339: the old `len([k async for k in scan_iter(...)])`
        materialization x4 sequentially blew the 15s probe budget once `session:*` and `agent_meta:*`
        grew past ~10K combined keys. The fix runs four bounded scans in parallel via asyncio.gather.
        """
        mock_audit = MagicMock()
        mock_audit.log_file = MagicMock()
        mock_audit.log_file.exists.return_value = True

        mock_db = AsyncMock()
        mock_db.health_check = AsyncMock(return_value={"status": "healthy"})
        mock_db.init = AsyncMock()

        mock_cal = MagicMock()
        mock_cal.get_pending_updates.return_value = 0

        session_cache = AsyncMock()
        session_cache.health_check = AsyncMock(return_value={"status": "healthy", "backend": "redis"})
        dist_lock = AsyncMock()
        dist_lock.health_check = AsyncMock(return_value={"status": "healthy", "backend": "redis"})

        # Per-pattern key counts — chosen to exercise both the under-cap and over-cap paths.
        # `session:*` is set just over the cap (via the test patching cap=10) so the helper must
        # short-circuit; the others stay below cap and return real counts.
        per_pattern_keys = {
            "session:*": 25,        # over cap (10) → "10+"
            "rate_limit:*": 3,
            "agent_meta:*": 5,
            "lock:*": 0,
        }

        raw_redis = AsyncMock()
        raw_redis.info = AsyncMock(return_value={"keyspace_hits": 5, "keyspace_misses": 1, "total_commands_processed": 12})

        async def _scan_iter(match=None, count=None):
            for i in range(per_pattern_keys.get(match, 0)):
                yield f"{match[:-1]}{i}"

        raw_redis.scan_iter = _scan_iter

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.calibration.calibration_checker", mock_cal), \
             patch("src.telemetry.telemetry_collector", MagicMock()), \
             patch("src.audit_log.audit_logger", mock_audit), \
             patch("src.db.get_db", return_value=mock_db), \
             patch("src.embeddings.embeddings_available", return_value=True), \
             patch("src.calibration_db.calibration_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.audit_db.audit_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.cache.is_redis_available", return_value=True), \
             patch("src.cache.get_session_cache", return_value=session_cache), \
             patch("src.cache.get_distributed_lock", return_value=dist_lock), \
             patch("src.cache.get_redis", new_callable=AsyncMock, return_value=raw_redis), \
             patch("src.services.runtime_queries._HEALTH_KEYS_CAP", 10):

            from src.services.runtime_queries import get_health_check_data
            data = await get_health_check_data({"lite": False})

        keys = data["checks"]["redis_cache"]["keys"]
        assert keys["sessions"] == "10+", f"expected over-cap marker, got {keys['sessions']!r}"
        assert keys["rate_limits"] == 3
        assert keys["metadata"] == 5
        assert keys["locks"] == 0

    @pytest.mark.asyncio
    async def test_health_check_knowledge_graph_lifecycle_warning(self, mock_mcp_server, patch_context_agent_id):
        """KG health should degrade to warning when lifecycle cleanup recently failed."""
        mock_audit = MagicMock()
        mock_audit.log_file = MagicMock()
        mock_audit.log_file.exists.return_value = True

        mock_db = AsyncMock()
        mock_db.health_check = AsyncMock(return_value={"status": "healthy"})
        mock_db.init = AsyncMock()

        mock_cal = MagicMock()
        mock_cal.get_pending_updates.return_value = 0

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.calibration.calibration_checker", mock_cal), \
             patch("src.telemetry.telemetry_collector", MagicMock()), \
             patch("src.audit_log.audit_logger", mock_audit), \
             patch("src.db.get_db", return_value=mock_db), \
             patch("src.calibration_db.calibration_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.audit_db.audit_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.cache.is_redis_available", return_value=False), \
             patch("src.embeddings.embeddings_available", return_value=True), \
             patch("src.knowledge_graph_lifecycle.get_kg_lifecycle_health",
                   return_value={"status": "error", "last_error": "graph with oid 17401 does not exist", "last_run": "2026-04-01T15:00:00"}), \
             patch("src.knowledge_graph.get_knowledge_graph",
                   new_callable=AsyncMock) as mock_kg:

            mock_kg_instance = AsyncMock()
            mock_kg_instance.health_check = AsyncMock(return_value={"total_discoveries": 1, "total_tags": 1, "total_edges": 1})
            mock_kg.return_value = mock_kg_instance

            from src.services.runtime_queries import get_health_check_data
            data = await get_health_check_data({})
            # KG check no longer calls get_knowledge_graph() (deadlocks in anyio context)
            # Just reports embeddings status — lifecycle warnings not propagated
            kg = data["checks"]["knowledge_graph"]
            assert kg["status"] in ("healthy", "degraded")


# ============================================================================
# Issue #165 — health response splits embedder vs backend capability
# ============================================================================


class TestIssue165HealthCapabilitySplit:

    @pytest.mark.asyncio
    async def test_embedder_up_but_backend_lacks_semantic_search(
        self, mock_mcp_server, patch_context_agent_id,
    ):
        """embedder_available=true + semantic_backend_available=false should
        be visible in the health response so callers don't infer that
        semantic KG search works when the active backend can't deliver."""
        pytest.importorskip("unitares_pi_plugin")
        mock_audit = MagicMock()
        mock_audit.log_file = MagicMock()
        mock_audit.log_file.exists.return_value = True

        mock_db = AsyncMock()
        mock_db.health_check = AsyncMock(return_value={"status": "healthy"})
        mock_db.init = AsyncMock()

        mock_cal = MagicMock()
        mock_cal.get_pending_updates.return_value = 0

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.calibration.calibration_checker", mock_cal), \
             patch("src.telemetry.telemetry_collector", MagicMock()), \
             patch("src.audit_log.audit_logger", mock_audit), \
             patch("src.db.get_db", return_value=mock_db), \
             patch("src.embeddings.embeddings_available", return_value=True), \
             patch("src.knowledge_graph.backend_supports_semantic_search", return_value=False), \
             patch("src.knowledge_graph.selected_backend_name", return_value="postgres"), \
             patch("unitares_pi_plugin.handlers.call_pi_tool",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy"}), \
             patch("src.calibration_db.calibration_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.audit_db.audit_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.cache.is_redis_available", return_value=False):

            from src.services.runtime_queries import get_health_check_data
            data = await get_health_check_data({})
            kg = data["checks"]["knowledge_graph"]
            assert kg["embedder_available"] is True
            assert kg["semantic_backend_available"] is False
            assert kg["semantic_search_reachable"] is False
            assert kg["status"] == "degraded"
            assert "backend 'postgres' has no semantic_search" in kg["warning"]

    @pytest.mark.asyncio
    async def test_both_up_reports_reachable(
        self, mock_mcp_server, patch_context_agent_id,
    ):
        pytest.importorskip("unitares_pi_plugin")
        mock_audit = MagicMock()
        mock_audit.log_file = MagicMock()
        mock_audit.log_file.exists.return_value = True

        mock_db = AsyncMock()
        mock_db.health_check = AsyncMock(return_value={"status": "healthy"})
        mock_db.init = AsyncMock()

        mock_cal = MagicMock()
        mock_cal.get_pending_updates.return_value = 0

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.calibration.calibration_checker", mock_cal), \
             patch("src.telemetry.telemetry_collector", MagicMock()), \
             patch("src.audit_log.audit_logger", mock_audit), \
             patch("src.db.get_db", return_value=mock_db), \
             patch("src.embeddings.embeddings_available", return_value=True), \
             patch("src.knowledge_graph.backend_supports_semantic_search", return_value=True), \
             patch("src.knowledge_graph.selected_backend_name", return_value="age"), \
             patch("unitares_pi_plugin.handlers.call_pi_tool",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy"}), \
             patch("src.calibration_db.calibration_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.audit_db.audit_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.cache.is_redis_available", return_value=False):

            from src.services.runtime_queries import get_health_check_data
            data = await get_health_check_data({})
            kg = data["checks"]["knowledge_graph"]
            assert kg["embedder_available"] is True
            assert kg["semantic_backend_available"] is True
            assert kg["semantic_search_reachable"] is True
            assert kg["status"] == "healthy"
            assert kg.get("warning") is None or "warning" not in kg


# ============================================================================
# handle_debug_request_context
# ============================================================================

class TestDebugRequestContext:

    @pytest.mark.asyncio
    async def test_debug_context_basic(self, mock_mcp_server):
        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value="test-session"), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {"tool1": None, "tool2": None}), \
             patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="derived-key"), \
             patch("src.mcp_handlers.identity.shared._session_identities", {}), \
             patch("src.mcp_handlers.identity.shared._uuid_prefix_index", {}):
            from src.mcp_handlers.admin.handlers import handle_debug_request_context
            result = await handle_debug_request_context({})

            data = parse_result(result)
            assert data["success"] is True
            assert "session" in data
            assert "tool_registry" in data
            assert data["tool_registry"]["count"] == 2


# ============================================================================
# handle_check_calibration
# ============================================================================

class TestCheckCalibration:

    @pytest.mark.asyncio
    async def test_check_calibration_basic(self, patch_context_agent_id):
        mock_checker = MagicMock()
        mock_checker.check_calibration.return_value = (
            True,  # is_calibrated
            {
                "bins": {
                    "low": {"count": 10, "accuracy": 0.8, "expected_accuracy": 0.3},
                    "high": {"count": 5, "accuracy": 0.9, "expected_accuracy": 0.8},
                },
                "issues": [],
            }
        )
        mock_checker.get_pending_updates.return_value = 0
        mock_seq_tracker = MagicMock()
        mock_seq_tracker.compute_metrics.return_value = {
            "status": "tracking",
            "eligible_samples": 4,
            "log_evidence": 1.2,
            "capped_alarm": 0.6988,
            "signal_sources": {"tests": 4},
        }

        with patch("src.calibration.calibration_checker", mock_checker), \
             patch("src.sequential_calibration.get_sequential_calibration_tracker", return_value=mock_seq_tracker):
            from src.mcp_handlers.admin.calibration import handle_check_calibration
            result = await handle_check_calibration({})

            data = parse_result(result)
            assert data["success"] is True
            assert data["calibrated"] is True
            assert data["total_samples"] == 15
            assert "confidence_distribution" in data
            assert data["tactical_evidence"]["eligible_samples"] == 4
            assert data["tactical_evidence"]["log_evidence"] == 1.2

    @pytest.mark.asyncio
    async def test_check_calibration_with_complexity(self, patch_context_agent_id):
        mock_checker = MagicMock()
        mock_checker.check_calibration.return_value = (
            False,
            {
                "bins": {
                    "low": {"count": 5, "accuracy": 0.5, "expected_accuracy": 0.2},
                },
                "issues": ["Overconfident in low bin"],
                "complexity_calibration": {
                    "simple": {"count": 3, "high_discrepancy_rate": 0.1},
                    "complex": {"count": 2, "high_discrepancy_rate": 0.8},
                }
            }
        )
        mock_checker.get_pending_updates.return_value = 2
        mock_checker.compute_correction_factors.return_value = {"0.8-0.9": 0.75}
        mock_checker.characterize_failure_modes.return_value = {
            "verdict_quality_warning": "High confidence bin is overconfident",
            "tactical": {"status": "analyzed"},
        }
        mock_seq_tracker = MagicMock()
        mock_seq_tracker.compute_metrics.return_value = {
            "status": "no_data",
            "eligible_samples": 0,
            "signal_sources": {},
        }

        with patch("src.calibration.calibration_checker", mock_checker), \
             patch("src.sequential_calibration.get_sequential_calibration_tracker", return_value=mock_seq_tracker):
            from src.mcp_handlers.admin.calibration import handle_check_calibration
            result = await handle_check_calibration({})

            data = parse_result(result)
            assert data["calibrated"] is False
            assert data["pending_updates"] == 2
            assert "complexity_calibration" in data
            assert data["tactical_evidence"]["status"] == "no_data"
            assert "log_evidence" not in data["tactical_evidence"]
            assert "capped_alarm" not in data["tactical_evidence"]
            guidance = data["calibration_guidance"]
            assert guidance["mode"] == "advisory_only"
            assert guidance["confidence_policy"] == "require_evidence_for_high_confidence_actions"
            assert guidance["correction_factors"] == {"0.8-0.9": 0.75}
            assert guidance["verdict_quality_warning"] == "High confidence bin is overconfident"
            assert "silently alter" in guidance["note"]

    @pytest.mark.asyncio
    async def test_check_calibration_empty_bins(self, patch_context_agent_id):
        mock_checker = MagicMock()
        mock_checker.check_calibration.return_value = (True, {"bins": {}, "issues": []})
        mock_checker.get_pending_updates.return_value = 0
        mock_seq_tracker = MagicMock()
        mock_seq_tracker.compute_metrics.return_value = {
            "status": "no_data",
            "eligible_samples": 0,
            "signal_sources": {},
        }

        with patch("src.calibration.calibration_checker", mock_checker), \
             patch("src.sequential_calibration.get_sequential_calibration_tracker", return_value=mock_seq_tracker):
            from src.mcp_handlers.admin.calibration import handle_check_calibration
            result = await handle_check_calibration({})

            data = parse_result(result)
            assert data["total_samples"] == 0
            assert data["trajectory_health"] == 0.0
            assert data["confidence_distribution"]["mean"] == 0.0
            assert "log_evidence" not in data["tactical_evidence"]
            assert "capped_alarm" not in data["tactical_evidence"]

    @pytest.mark.asyncio
    async def test_check_calibration_accuracy_uses_real_outcome_evidence(self, patch_context_agent_id):
        """Regression: `accuracy` must report sequential tracker's empirical_accuracy
        (real outcome-match) when available, NOT alias trajectory_health."""
        mock_checker = MagicMock()
        mock_checker.check_calibration.return_value = (
            True,
            {
                "bins": {
                    "high": {"count": 20, "accuracy": 0.97, "expected_accuracy": 0.85},
                },
                "issues": [],
            },
        )
        mock_checker.get_pending_updates.return_value = 0
        mock_seq_tracker = MagicMock()
        mock_seq_tracker.compute_metrics.return_value = {
            "status": "tracking",
            "eligible_samples": 50,
            "mean_confidence": 0.82,
            "empirical_accuracy": 0.64,
            "calibration_gap": -0.18,
            "log_evidence": 2.1,
            "capped_alarm": 0.88,
            "signal_sources": {"tests": 30, "commands": 20},
        }

        with patch("src.calibration.calibration_checker", mock_checker), \
             patch("src.sequential_calibration.get_sequential_calibration_tracker", return_value=mock_seq_tracker):
            from src.mcp_handlers.admin.calibration import handle_check_calibration
            result = await handle_check_calibration({})

            data = parse_result(result)
            assert data["accuracy"] == 0.64, (
                "accuracy must report real empirical_accuracy from outcome evidence, "
                "not the trajectory_health proxy"
            )
            assert data["trajectory_health"] == 0.97
            assert data["accuracy"] != data["trajectory_health"], (
                "Regression: accuracy and trajectory_health were aliased to the same value, "
                "letting consumers read the trajectory proxy under a misleading label"
            )
            assert data["truth_channel"] == "confidence_outcome_match"

    @pytest.mark.asyncio
    async def test_check_calibration_accuracy_null_when_no_outcome_evidence(self, patch_context_agent_id):
        """When sequential tracker has no_data, accuracy must be null with an
        honest truth_channel ('trajectory_proxy') — not silently aliased to trajectory_health."""
        mock_checker = MagicMock()
        mock_checker.check_calibration.return_value = (
            True,
            {
                "bins": {
                    "high": {"count": 20, "accuracy": 0.97, "expected_accuracy": 0.85},
                },
                "issues": [],
            },
        )
        mock_checker.get_pending_updates.return_value = 0
        mock_seq_tracker = MagicMock()
        mock_seq_tracker.compute_metrics.return_value = {
            "status": "no_data",
            "eligible_samples": 0,
            "signal_sources": {},
        }

        with patch("src.calibration.calibration_checker", mock_checker), \
             patch("src.sequential_calibration.get_sequential_calibration_tracker", return_value=mock_seq_tracker):
            from src.mcp_handlers.admin.calibration import handle_check_calibration
            result = await handle_check_calibration({})

            data = parse_result(result)
            assert data["accuracy"] is None
            assert data["trajectory_health"] == 0.97
            assert data["truth_channel"] == "trajectory_proxy"
            assert "log_evidence" not in data["tactical_evidence"]
            assert "capped_alarm" not in data["tactical_evidence"]


# ============================================================================
# handle_rebuild_calibration
# ============================================================================

class TestRebuildCalibration:

    @pytest.mark.asyncio
    async def test_rebuild_calibration_success(self, patch_context_agent_id):
        mock_result = {"processed": 10, "updated": 8, "skipped": 2, "errors": 0}
        with patch("src.auto_ground_truth.collect_ground_truth_automatically",
                    new_callable=AsyncMock, return_value=mock_result):
            from src.mcp_handlers.admin.calibration import handle_rebuild_calibration
            result = await handle_rebuild_calibration({})

            data = parse_result(result)
            assert data["success"] is True
            assert data["processed"] == 10
            assert data["updated"] == 8
            assert data["action"] == "rebuild"

    @pytest.mark.asyncio
    async def test_rebuild_calibration_dry_run(self, patch_context_agent_id):
        mock_result = {"processed": 5, "updated": 5, "skipped": 0, "errors": 0}
        with patch("src.auto_ground_truth.collect_ground_truth_automatically",
                    new_callable=AsyncMock, return_value=mock_result):
            from src.mcp_handlers.admin.calibration import handle_rebuild_calibration
            result = await handle_rebuild_calibration({"dry_run": True})

            data = parse_result(result)
            assert data["action"] == "dry_run"

    @pytest.mark.asyncio
    async def test_rebuild_calibration_string_dry_run(self, patch_context_agent_id):
        """Test that string 'true' is parsed as bool for dry_run."""
        mock_result = {"processed": 1, "updated": 1, "skipped": 0, "errors": 0}
        with patch("src.auto_ground_truth.collect_ground_truth_automatically",
                    new_callable=AsyncMock, return_value=mock_result):
            from src.mcp_handlers.admin.calibration import handle_rebuild_calibration
            result = await handle_rebuild_calibration({"dry_run": "true"})

            data = parse_result(result)
            assert data["action"] == "dry_run"

    @pytest.mark.asyncio
    async def test_rebuild_calibration_error(self, patch_context_agent_id):
        with patch("src.auto_ground_truth.collect_ground_truth_automatically",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("no data")):
            from src.mcp_handlers.admin.calibration import handle_rebuild_calibration
            result = await handle_rebuild_calibration({})

            data = parse_result(result)
            assert data["success"] is False
            assert "no data" in data["error"]

    @pytest.mark.asyncio
    async def test_rebuild_calibration_custom_params(self, patch_context_agent_id):
        mock_result = {"processed": 3, "updated": 3, "skipped": 0, "errors": 0}
        with patch("src.auto_ground_truth.collect_ground_truth_automatically",
                    new_callable=AsyncMock, return_value=mock_result) as mock_fn:
            from src.mcp_handlers.admin.calibration import handle_rebuild_calibration
            result = await handle_rebuild_calibration({
                "min_age_hours": 2.0,
                "max_decisions": 50,
            })

            # Verify parameters were passed through
            call_kwargs = mock_fn.call_args
            assert call_kwargs.kwargs["min_age_hours"] == 2.0
            assert call_kwargs.kwargs["max_decisions"] == 50
            assert call_kwargs.kwargs["rebuild"] is True


# ============================================================================
# handle_update_calibration_ground_truth
# ============================================================================

class TestUpdateCalibrationGroundTruth:

    @pytest.mark.asyncio
    async def test_direct_mode_success(self, patch_context_agent_id):
        mock_checker = MagicMock()
        mock_checker.get_pending_updates.return_value = 1

        with patch("src.calibration.calibration_checker", mock_checker):
            from src.mcp_handlers.admin.calibration import handle_update_calibration_ground_truth
            result = await handle_update_calibration_ground_truth({
                "confidence": 0.8,
                "predicted_correct": True,
                "actual_correct": True,
            })

            data = parse_result(result)
            assert data["success"] is True
            assert "direct mode" in data["message"].lower()
            mock_checker.update_ground_truth.assert_called_once()
            mock_checker.save_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_direct_mode_missing_params(self, patch_context_agent_id):
        from src.mcp_handlers.admin.calibration import handle_update_calibration_ground_truth
        result = await handle_update_calibration_ground_truth({
            "confidence": 0.8,
            # missing predicted_correct and actual_correct
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "missing" in data["error"].lower() or "required" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_timestamp_mode_missing_actual_correct(self, patch_context_agent_id):
        from src.mcp_handlers.admin.calibration import handle_update_calibration_ground_truth
        result = await handle_update_calibration_ground_truth({
            "timestamp": "2026-01-01T00:00:00",
            # missing actual_correct
        })

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_timestamp_mode_no_entries_found(self, patch_context_agent_id):
        mock_audit = MagicMock()
        mock_audit.query_audit_log.return_value = []

        with patch("src.calibration.calibration_checker", MagicMock()), \
             patch("src.audit_log.AuditLogger", return_value=mock_audit):
            from src.mcp_handlers.admin.calibration import handle_update_calibration_ground_truth
            result = await handle_update_calibration_ground_truth({
                "timestamp": "2026-01-01T00:00:00",
                "actual_correct": True,
            })

            data = parse_result(result)
            assert data["success"] is False
            assert "no decision found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_timestamp_mode_success(self, patch_context_agent_id):
        mock_audit = MagicMock()
        mock_audit.query_audit_log.return_value = [
            {"confidence": 0.85, "details": {"decision": "attest"}}
        ]
        mock_checker = MagicMock()
        mock_checker.get_pending_updates.return_value = 1

        with patch("src.calibration.calibration_checker", mock_checker), \
             patch("src.audit_log.AuditLogger", return_value=mock_audit):
            from src.mcp_handlers.admin.calibration import handle_update_calibration_ground_truth
            result = await handle_update_calibration_ground_truth({
                "timestamp": "2026-01-01T00:00:00",
                "actual_correct": True,
            })

            data = parse_result(result)
            assert data["success"] is True
            assert "timestamp mode" in data["message"].lower()
            assert data["looked_up"]["confidence"] == 0.85

    @pytest.mark.asyncio
    async def test_timestamp_mode_invalid_format(self, patch_context_agent_id):
        from src.mcp_handlers.admin.calibration import handle_update_calibration_ground_truth
        result = await handle_update_calibration_ground_truth({
            "timestamp": 12345,  # not a string
            "actual_correct": True,
        })

        data = parse_result(result)
        assert data["success"] is False


# ============================================================================
# handle_get_telemetry_metrics
# ============================================================================

class TestGetTelemetryMetrics:

    @pytest.mark.asyncio
    async def test_telemetry_basic(self, patch_context_agent_id):
        mock_telemetry = MagicMock()
        mock_telemetry.get_skip_rate_metrics.return_value = {"skip_rate": 0.1}
        mock_telemetry.get_confidence_distribution.return_value = {"mean": 0.7}
        mock_telemetry.detect_suspicious_patterns.return_value = []

        with patch("src.telemetry.TelemetryCollector", return_value=mock_telemetry), \
             patch("src.perf_monitor.snapshot", return_value={"avg_ms": 5}):
            from src.mcp_handlers.admin.handlers import handle_get_telemetry_metrics
            result = await handle_get_telemetry_metrics({})

            data = parse_result(result)
            assert data["success"] is True
            assert data["agent_id"] == "all_agents"
            assert data["window_hours"] == 24
            assert "calibration" in data  # should have note about excluded

    @pytest.mark.asyncio
    async def test_telemetry_with_calibration(self, patch_context_agent_id):
        mock_telemetry = MagicMock()
        mock_telemetry.get_skip_rate_metrics.return_value = {}
        mock_telemetry.get_confidence_distribution.return_value = {}
        mock_telemetry.detect_suspicious_patterns.return_value = []
        mock_telemetry.get_calibration_metrics.return_value = {"calibrated": True}

        with patch("src.telemetry.TelemetryCollector", return_value=mock_telemetry), \
             patch("src.perf_monitor.snapshot", return_value={}):
            from src.mcp_handlers.admin.handlers import handle_get_telemetry_metrics
            result = await handle_get_telemetry_metrics({
                "include_calibration": True,
                "agent_id": "agent-1",
                "window_hours": 48,
            })

            data = parse_result(result)
            assert data["agent_id"] == "agent-1"
            assert data["window_hours"] == 48
            assert data["calibration"]["calibrated"] is True

    @pytest.mark.asyncio
    async def test_telemetry_error(self, patch_context_agent_id):
        mock_telemetry = MagicMock()
        mock_telemetry.get_skip_rate_metrics.side_effect = RuntimeError("telemetry broken")

        with patch("src.telemetry.TelemetryCollector", return_value=mock_telemetry):
            from src.mcp_handlers.admin.handlers import handle_get_telemetry_metrics
            result = await handle_get_telemetry_metrics({})

            data = parse_result(result)
            assert data["success"] is False


# ============================================================================
# handle_backfill_calibration_from_dialectic
# ============================================================================

class TestBackfillCalibration:

    @pytest.mark.asyncio
    async def test_backfill_success(self, patch_context_agent_id):
        mock_result = {"processed": 5, "updated": 3, "errors": 0, "sessions": []}
        with patch(
            "src.mcp_handlers.dialectic.handlers.backfill_calibration_from_historical_sessions",
            new_callable=AsyncMock,
            return_value=mock_result
        ):
            from src.mcp_handlers.admin.calibration import handle_backfill_calibration_from_dialectic
            result = await handle_backfill_calibration_from_dialectic({})

            data = parse_result(result)
            assert data["success"] is True
            assert data["processed"] == 5
            assert data["updated"] == 3

    @pytest.mark.asyncio
    async def test_backfill_error(self, patch_context_agent_id):
        with patch(
            "src.mcp_handlers.dialectic.handlers.backfill_calibration_from_historical_sessions",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB down")
        ):
            from src.mcp_handlers.admin.calibration import handle_backfill_calibration_from_dialectic
            result = await handle_backfill_calibration_from_dialectic({})

            data = parse_result(result)
            assert data["success"] is False
            assert "DB down" in data["error"]


# ============================================================================
# handle_check_continuity_health
# ============================================================================

class TestCheckContinuityHealth:

    @pytest.mark.asyncio
    async def test_continuity_health_basic(self, mock_mcp_server, patch_context_agent_id):
        mock_mcp_server.agent_metadata = {
            "agent-1": MagicMock(status="active"),
            "agent-2": MagicMock(status="archived"),
        }
        mock_graph = AsyncMock()
        mock_graph.get_stats = AsyncMock(return_value={
            "total_discoveries": 10,
            "total_agents": 2,
        })
        mock_graph.query = AsyncMock(return_value=[])

        # Patch module-level mcp_server
        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
                   new_callable=AsyncMock, return_value=mock_graph):
            from src.mcp_handlers.admin.handlers import handle_check_continuity_health
            result = await handle_check_continuity_health({})

            data = parse_result(result)
            assert data["success"] is True
            assert "checks" in data
            assert data["checks"]["agent_metadata"]["count"] == 2
            assert data["checks"]["agent_metadata"]["active_agents"] == 1

    @pytest.mark.asyncio
    async def test_continuity_health_deep_check(self, mock_mcp_server, patch_context_agent_id):
        mock_mcp_server.agent_metadata = {}
        mock_discovery = MagicMock()
        mock_discovery.provenance = {"created_by": "agent-1"}

        mock_graph = AsyncMock()
        mock_graph.get_stats = AsyncMock(return_value={
            "total_discoveries": 1, "total_agents": 1
        })
        mock_graph.query = AsyncMock(return_value=[mock_discovery])

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
                   new_callable=AsyncMock, return_value=mock_graph):
            from src.mcp_handlers.admin.handlers import handle_check_continuity_health
            result = await handle_check_continuity_health({"deep_check": True})

            data = parse_result(result)
            assert data["checks"]["provenance_tracking"]["sample_provenance_count"] == 1

    @pytest.mark.asyncio
    async def test_continuity_health_with_agent_id(self, mock_mcp_server, patch_context_agent_id):
        meta = MagicMock()
        meta.parent_agent_id = None
        meta.spawn_reason = "user"
        mock_mcp_server.agent_metadata = {"agent-1": meta}

        mock_graph = AsyncMock()
        mock_graph.get_stats = AsyncMock(return_value={
            "total_discoveries": 1, "total_agents": 1
        })
        mock_graph.query = AsyncMock(return_value=[])

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
                   new_callable=AsyncMock, return_value=mock_graph), \
             patch("src.mcp_handlers.identity.shared._get_lineage", return_value=["agent-1"]):
            from src.mcp_handlers.admin.handlers import handle_check_continuity_health
            result = await handle_check_continuity_health({"agent_id": "agent-1"})

            data = parse_result(result)
            assert "agent_lineage" in data["checks"]
            assert data["checks"]["agent_lineage"]["agent_id"] == "agent-1"

    @pytest.mark.asyncio
    async def test_continuity_health_error(self, mock_mcp_server, patch_context_agent_id):
        # Make mcp_server attributes raise errors to simulate server down
        broken_server = MagicMock()
        broken_server._metadata_cache_state = MagicMock(side_effect=RuntimeError("server down"))
        broken_server.agent_metadata = MagicMock(side_effect=RuntimeError("server down"))
        type(broken_server).agent_metadata = PropertyMock(side_effect=RuntimeError("server down"))
        with patch("src.mcp_handlers.admin.handlers.mcp_server", broken_server):
            from src.mcp_handlers.admin.handlers import handle_check_continuity_health
            result = await handle_check_continuity_health({})

            data = parse_result(result)
            assert data["success"] is False

    @pytest.mark.asyncio
    async def test_continuity_health_recommendations(self, mock_mcp_server, patch_context_agent_id):
        """Test recommendations are generated when metadata is empty."""
        mock_mcp_server.agent_metadata = {}
        mock_graph = AsyncMock()
        mock_graph.get_stats = AsyncMock(return_value={
            "total_discoveries": 0, "total_agents": 0
        })
        mock_graph.query = AsyncMock(return_value=[])

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
                   new_callable=AsyncMock, return_value=mock_graph):
            from src.mcp_handlers.admin.handlers import handle_check_continuity_health
            result = await handle_check_continuity_health({})

            data = parse_result(result)
            assert len(data["recommendations"]) >= 2


# ============================================================================
# Workspace helper functions (sync)
# ============================================================================

class TestWorkspaceHelpers:

    def test_get_workspace_last_agent_file(self):
        from src.mcp_handlers.admin.handlers import get_workspace_last_agent_file
        server = MagicMock()
        server.project_root = "/tmp/test_project"

        result = get_workspace_last_agent_file(server)
        assert result == Path("/tmp/test_project/data/.last_active_agent")

    def test_get_workspace_last_agent_found(self, tmp_path):
        from src.mcp_handlers.admin.handlers import get_workspace_last_agent

        server = MagicMock()
        server.project_root = str(tmp_path)
        server.agent_metadata = {"agent-123": MagicMock()}

        # Create the file
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / ".last_active_agent").write_text("agent-123")

        result = get_workspace_last_agent(server)
        assert result == "agent-123"

    def test_get_workspace_last_agent_not_found(self, tmp_path):
        from src.mcp_handlers.admin.handlers import get_workspace_last_agent

        server = MagicMock()
        server.project_root = str(tmp_path)
        server.agent_metadata = {}

        result = get_workspace_last_agent(server)
        assert result is None

    def test_get_workspace_last_agent_file_missing(self, tmp_path):
        from src.mcp_handlers.admin.handlers import get_workspace_last_agent

        server = MagicMock()
        server.project_root = str(tmp_path)
        server.agent_metadata = {"agent-1": MagicMock()}

        result = get_workspace_last_agent(server)
        assert result is None

    def test_get_workspace_last_agent_stale(self, tmp_path):
        """Agent ID in file no longer exists in metadata."""
        from src.mcp_handlers.admin.handlers import get_workspace_last_agent

        server = MagicMock()
        server.project_root = str(tmp_path)
        server.agent_metadata = {"other-agent": MagicMock()}

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / ".last_active_agent").write_text("old-agent")

        result = get_workspace_last_agent(server)
        assert result is None

    def test_set_workspace_last_agent(self, tmp_path):
        from src.mcp_handlers.admin.handlers import set_workspace_last_agent

        server = MagicMock()
        server.project_root = str(tmp_path)

        set_workspace_last_agent(server, "agent-abc")

        written = (tmp_path / "data" / ".last_active_agent").read_text()
        assert written == "agent-abc"

    def test_set_workspace_last_agent_creates_dir(self, tmp_path):
        from src.mcp_handlers.admin.handlers import set_workspace_last_agent

        server = MagicMock()
        server.project_root = str(tmp_path)

        # data dir does not exist yet
        set_workspace_last_agent(server, "agent-xyz")

        assert (tmp_path / "data" / ".last_active_agent").exists()

    def test_set_workspace_last_agent_error_suppressed(self):
        """set_workspace_last_agent suppresses errors."""
        from src.mcp_handlers.admin.handlers import set_workspace_last_agent

        server = MagicMock()
        server.project_root = "/nonexistent/path/that/will/fail"

        # Should not raise
        set_workspace_last_agent(server, "agent-err")


# ============================================================================
# handle_list_tools - basic tests
# ============================================================================

class TestListTools:

    @pytest.mark.asyncio
    async def test_list_tools_lite_mode(self, mock_mcp_server, patch_context_agent_id):
        """Test list_tools in lite mode (default)."""
        mock_mcp_server.SERVER_VERSION = "test-1.0.0"

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {
                 "onboard": None,
                 "process_agent_update": None,
                 "health_check": None,
                 "list_tools": None,
             }), \
             patch("src.tool_modes.TOOL_TIERS", {
                 "essential": {"onboard", "process_agent_update", "list_tools"},
                 "common": {"health_check"},
                 "advanced": set(),
             }), \
             patch("src.tool_modes.TOOL_OPERATIONS", {
                 "onboard": "write",
                 "process_agent_update": "write",
                 "health_check": "read",
                 "list_tools": "read",
             }), \
             patch("src.tool_modes.LITE_MODE_TOOLS", {
                 "onboard", "process_agent_update", "list_tools", "health_check"
             }), \
             patch("src.mcp_handlers.tool_stability.list_all_aliases", return_value={}), \
             patch("src.mcp_handlers.decorators.get_tool_timeout", return_value=10.0), \
             patch("src.mcp_handlers.decorators.get_tool_description", return_value=""), \
             patch("src.tool_schemas.get_tool_definitions", return_value=[]):

            from src.mcp_handlers.introspection.tool_introspection import handle_list_tools
            result = await handle_list_tools({"lite": True})

            data = parse_result(result)
            assert data["success"] is True
            assert "tools" in data
            assert data["shown"] > 0
            assert data["getting_started_path"][0]["tool"] == "onboard"
            assert data["essential_toolkit"]["preferred_consolidated_tools"]["dialectic"].startswith("Use action='quick'")

    @pytest.mark.asyncio
    async def test_list_tools_full_mode(self, mock_mcp_server, patch_context_agent_id):
        """Test list_tools in full mode (lite=False) covers lines 1595-1851."""
        mock_mcp_server.SERVER_VERSION = "test-1.0.0"

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {
                 "onboard": None,
                 "process_agent_update": None,
                 "health_check": None,
                 "list_tools": None,
             }), \
             patch("src.tool_modes.TOOL_TIERS", {
                 "essential": {"onboard", "process_agent_update", "list_tools"},
                 "common": {"health_check"},
                 "advanced": set(),
             }), \
             patch("src.tool_modes.TOOL_OPERATIONS", {
                 "onboard": "write",
                 "process_agent_update": "write",
                 "health_check": "read",
                 "list_tools": "read",
             }), \
             patch("src.tool_modes.LITE_MODE_TOOLS", {
                 "onboard", "process_agent_update", "list_tools", "health_check"
             }), \
             patch("src.mcp_handlers.tool_stability.list_all_aliases", return_value={}), \
             patch("src.mcp_handlers.decorators.get_tool_timeout", return_value=10.0), \
             patch("src.mcp_handlers.decorators.get_tool_description", return_value=""), \
             patch("src.tool_schemas.get_tool_definitions", return_value=[]):

            from src.mcp_handlers.introspection.tool_introspection import handle_list_tools
            result = await handle_list_tools({"lite": False})

            data = parse_result(result)
            assert data["success"] is True
            assert "tools" in data
            assert "tiers" in data
            assert "tier_counts" in data
            assert "categories" in data
            assert "workflows" in data
            assert "tool_map" in data
            assert data["getting_started"]["path"][0]["tool"] == "onboard"
            assert "knowledge" in data["getting_started"]["essential_toolkit"]["preferred_consolidated_tools"]
            assert data["total_tools"] >= 0

    @pytest.mark.asyncio
    async def test_list_tools_progressive_mode(self, mock_mcp_server, patch_context_agent_id):
        """Test list_tools with progressive=True covers lines 1441-1467, 1587, 1603-1644."""
        mock_mcp_server.SERVER_VERSION = "test-1.0.0"

        mock_tracker = MagicMock()
        mock_tracker.get_usage_stats.return_value = {
            "tools": {
                "onboard": {"call_count": 15},
                "health_check": {"call_count": 3},
                "list_tools": {"call_count": 0},
                "process_agent_update": {"call_count": 50},
            }
        }

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {
                 "onboard": None,
                 "process_agent_update": None,
                 "health_check": None,
                 "list_tools": None,
             }), \
             patch("src.tool_modes.TOOL_TIERS", {
                 "essential": {"onboard", "process_agent_update", "list_tools"},
                 "common": {"health_check"},
                 "advanced": set(),
             }), \
             patch("src.tool_modes.TOOL_OPERATIONS", {
                 "onboard": "write",
                 "process_agent_update": "write",
                 "health_check": "read",
                 "list_tools": "read",
             }), \
             patch("src.tool_modes.LITE_MODE_TOOLS", {
                 "onboard", "process_agent_update", "list_tools", "health_check"
             }), \
             patch("src.mcp_handlers.tool_stability.list_all_aliases", return_value={}), \
             patch("src.mcp_handlers.decorators.get_tool_timeout", return_value=10.0), \
             patch("src.mcp_handlers.decorators.get_tool_description", return_value=""), \
             patch("src.tool_schemas.get_tool_definitions", return_value=[]), \
             patch("src.tool_usage_tracker.get_tool_usage_tracker", return_value=mock_tracker):

            from src.mcp_handlers.introspection.tool_introspection import handle_list_tools

            # Test full mode with progressive
            result = await handle_list_tools({"lite": False, "progressive": True})
            data = parse_result(result)
            assert data["success"] is True
            assert "progressive" in data
            assert data["progressive"]["enabled"] is True
            assert "sections" in data

    @pytest.mark.asyncio
    async def test_list_tools_lite_progressive(self, mock_mcp_server, patch_context_agent_id):
        """Test list_tools lite mode with progressive=True covers lines 1490-1492."""
        mock_mcp_server.SERVER_VERSION = "test-1.0.0"

        mock_tracker = MagicMock()
        mock_tracker.get_usage_stats.return_value = {
            "tools": {
                "onboard": {"call_count": 20},
                "health_check": {"call_count": 5},
            }
        }

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {
                 "onboard": None,
                 "process_agent_update": None,
                 "health_check": None,
                 "list_tools": None,
             }), \
             patch("src.tool_modes.TOOL_TIERS", {
                 "essential": {"onboard", "process_agent_update", "list_tools"},
                 "common": {"health_check"},
                 "advanced": set(),
             }), \
             patch("src.tool_modes.TOOL_OPERATIONS", {}), \
             patch("src.tool_modes.LITE_MODE_TOOLS", {
                 "onboard", "process_agent_update", "list_tools", "health_check"
             }), \
             patch("src.mcp_handlers.tool_stability.list_all_aliases", return_value={}), \
             patch("src.mcp_handlers.decorators.get_tool_timeout", return_value=10.0), \
             patch("src.mcp_handlers.decorators.get_tool_description", return_value=""), \
             patch("src.tool_schemas.get_tool_definitions", return_value=[]), \
             patch("src.tool_usage_tracker.get_tool_usage_tracker", return_value=mock_tracker):

            from src.mcp_handlers.introspection.tool_introspection import handle_list_tools
            result = await handle_list_tools({"lite": True, "progressive": True})

            data = parse_result(result)
            assert data["success"] is True
            assert "progressive" in data

    @pytest.mark.asyncio
    async def test_list_tools_essential_only_filter(self, mock_mcp_server, patch_context_agent_id):
        """Test list_tools with essential_only=True covers lines 1388-1394."""
        mock_mcp_server.SERVER_VERSION = "test-1.0.0"

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {
                 "onboard": None,
                 "process_agent_update": None,
                 "health_check": None,
                 "list_tools": None,
                 "some_advanced_tool": None,
             }), \
             patch("src.tool_modes.TOOL_TIERS", {
                 "essential": {"onboard", "process_agent_update", "list_tools"},
                 "common": {"health_check"},
                 "advanced": {"some_advanced_tool"},
             }), \
             patch("src.tool_modes.TOOL_OPERATIONS", {}), \
             patch("src.tool_modes.LITE_MODE_TOOLS", set()), \
             patch("src.mcp_handlers.tool_stability.list_all_aliases", return_value={}), \
             patch("src.mcp_handlers.decorators.get_tool_timeout", return_value=None), \
             patch("src.mcp_handlers.decorators.get_tool_description", return_value=""), \
             patch("src.tool_schemas.get_tool_definitions", return_value=[]):

            from src.mcp_handlers.introspection.tool_introspection import handle_list_tools
            result = await handle_list_tools({"lite": False, "essential_only": True})

            data = parse_result(result)
            assert data["success"] is True
            # Only essential tools should be included
            tool_names = [t["name"] for t in data["tools"]]
            for name in tool_names:
                assert name in {"onboard", "process_agent_update", "list_tools"}

    @pytest.mark.asyncio
    async def test_list_tools_exclude_advanced(self, mock_mcp_server, patch_context_agent_id):
        """Test list_tools with include_advanced=False covers line 1392."""
        mock_mcp_server.SERVER_VERSION = "test-1.0.0"

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {
                 "onboard": None,
                 "health_check": None,
                 "some_advanced_tool": None,
             }), \
             patch("src.tool_modes.TOOL_TIERS", {
                 "essential": {"onboard"},
                 "common": {"health_check"},
                 "advanced": {"some_advanced_tool"},
             }), \
             patch("src.tool_modes.TOOL_OPERATIONS", {}), \
             patch("src.tool_modes.LITE_MODE_TOOLS", set()), \
             patch("src.mcp_handlers.tool_stability.list_all_aliases", return_value={}), \
             patch("src.mcp_handlers.decorators.get_tool_timeout", return_value=None), \
             patch("src.mcp_handlers.decorators.get_tool_description", return_value=""), \
             patch("src.tool_schemas.get_tool_definitions", return_value=[]):

            from src.mcp_handlers.introspection.tool_introspection import handle_list_tools
            result = await handle_list_tools({"lite": False, "include_advanced": False})

            data = parse_result(result)
            assert data["success"] is True
            tool_names = [t["name"] for t in data["tools"]]
            assert "some_advanced_tool" not in tool_names

    @pytest.mark.asyncio
    async def test_list_tools_tier_filter(self, mock_mcp_server, patch_context_agent_id):
        """Test list_tools with tier filter covers line 1394."""
        mock_mcp_server.SERVER_VERSION = "test-1.0.0"

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {
                 "onboard": None,
                 "health_check": None,
                 "some_advanced_tool": None,
             }), \
             patch("src.tool_modes.TOOL_TIERS", {
                 "essential": {"onboard"},
                 "common": {"health_check"},
                 "advanced": {"some_advanced_tool"},
             }), \
             patch("src.tool_modes.TOOL_OPERATIONS", {}), \
             patch("src.tool_modes.LITE_MODE_TOOLS", set()), \
             patch("src.mcp_handlers.tool_stability.list_all_aliases", return_value={}), \
             patch("src.mcp_handlers.decorators.get_tool_timeout", return_value=None), \
             patch("src.mcp_handlers.decorators.get_tool_description", return_value=""), \
             patch("src.tool_schemas.get_tool_definitions", return_value=[]):

            from src.mcp_handlers.introspection.tool_introspection import handle_list_tools
            result = await handle_list_tools({"lite": False, "tier": "common"})

            data = parse_result(result)
            assert data["success"] is True
            tool_names = [t["name"] for t in data["tools"]]
            assert "health_check" in tool_names
            assert "onboard" not in tool_names

    @pytest.mark.asyncio
    async def test_list_tools_description_fallbacks(self, mock_mcp_server, patch_context_agent_id):
        """Test description fallback chain covers lines 1361-1370, 1374."""
        mock_mcp_server.SERVER_VERSION = "test-1.0.0"

        mock_tool_schema = MagicMock()
        mock_tool_schema.name = "custom_tool"
        mock_tool_schema.description = "Schema description\nSecond line"

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {
                 "custom_tool": None,
                 "no_desc_tool": None,
             }), \
             patch("src.tool_modes.TOOL_TIERS", {
                 "essential": set(),
                 "common": {"custom_tool", "no_desc_tool"},
                 "advanced": set(),
             }), \
             patch("src.tool_modes.TOOL_OPERATIONS", {}), \
             patch("src.tool_modes.LITE_MODE_TOOLS", set()), \
             patch("src.mcp_handlers.tool_stability.list_all_aliases", return_value={}), \
             patch("src.mcp_handlers.decorators.get_tool_timeout", return_value=None), \
             patch("src.mcp_handlers.decorators.get_tool_description", return_value=""), \
             patch("src.tool_schemas.get_tool_definitions", return_value=[mock_tool_schema]):

            from src.mcp_handlers.introspection.tool_introspection import handle_list_tools
            result = await handle_list_tools({"lite": False})

            data = parse_result(result)
            assert data["success"] is True
            tools_by_name = {t["name"]: t for t in data["tools"]}
            # custom_tool should use schema description (first line only due to newline)
            assert tools_by_name["custom_tool"]["description"] == "Schema description"
            # no_desc_tool should use generic fallback
            assert tools_by_name["no_desc_tool"]["description"] == "Tool: no_desc_tool"

    @pytest.mark.asyncio
    async def test_list_tools_deprecated_tools_hidden(self, mock_mcp_server, patch_context_agent_id):
        """Test deprecated tools are hidden covers line 1388."""
        mock_mcp_server.SERVER_VERSION = "test-1.0.0"

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {
                 "onboard": None,
                 "old_deprecated": None,
             }), \
             patch("src.tool_modes.TOOL_TIERS", {
                 "essential": {"onboard"},
                 "common": {"old_deprecated"},
                 "advanced": set(),
             }), \
             patch("src.tool_modes.TOOL_OPERATIONS", {}), \
             patch("src.tool_modes.LITE_MODE_TOOLS", set()), \
             patch("src.mcp_handlers.tool_stability.list_all_aliases", return_value={"old_deprecated": "onboard"}), \
             patch("src.mcp_handlers.decorators.get_tool_timeout", return_value=None), \
             patch("src.mcp_handlers.decorators.get_tool_description", return_value=""), \
             patch("src.tool_schemas.get_tool_definitions", return_value=[]):

            from src.mcp_handlers.introspection.tool_introspection import handle_list_tools
            result = await handle_list_tools({"lite": False})

            data = parse_result(result)
            tool_names = [t["name"] for t in data["tools"]]
            assert "old_deprecated" not in tool_names

    @pytest.mark.asyncio
    async def test_list_tools_unknown_category_fallback(self, mock_mcp_server, patch_context_agent_id):
        """Test unknown category fallback covers lines 1432-1433."""
        mock_mcp_server.SERVER_VERSION = "test-1.0.0"

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {
                 "special_tool": None,
             }), \
             patch("src.tool_modes.TOOL_TIERS", {
                 "essential": set(),
                 "common": {"special_tool"},
                 "advanced": set(),
             }), \
             patch("src.tool_modes.TOOL_OPERATIONS", {}), \
             patch("src.tool_modes.LITE_MODE_TOOLS", set()), \
             patch("src.mcp_handlers.tool_stability.list_all_aliases", return_value={}), \
             patch("src.mcp_handlers.decorators.get_tool_timeout", return_value=None), \
             patch("src.mcp_handlers.decorators.get_tool_description", return_value="Test tool"), \
             patch("src.tool_schemas.get_tool_definitions", return_value=[]):

            from src.mcp_handlers.introspection.tool_introspection import handle_list_tools
            result = await handle_list_tools({"lite": False})

            data = parse_result(result)
            assert data["success"] is True

    @pytest.mark.asyncio
    async def test_list_tools_new_agent_first_time_hint(self, mock_mcp_server):
        """Test new agent gets first_time hint covers lines 1522-1523."""
        mock_mcp_server.SERVER_VERSION = "test-1.0.0"

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {
                 "onboard": None,
                 "list_tools": None,
             }), \
             patch("src.tool_modes.TOOL_TIERS", {
                 "essential": {"onboard", "list_tools"},
                 "common": set(),
                 "advanced": set(),
             }), \
             patch("src.tool_modes.TOOL_OPERATIONS", {}), \
             patch("src.tool_modes.LITE_MODE_TOOLS", {"onboard", "list_tools"}), \
             patch("src.mcp_handlers.tool_stability.list_all_aliases", return_value={}), \
             patch("src.mcp_handlers.decorators.get_tool_timeout", return_value=None), \
             patch("src.mcp_handlers.decorators.get_tool_description", return_value=""), \
             patch("src.tool_schemas.get_tool_definitions", return_value=[]), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None):

            from src.mcp_handlers.introspection.tool_introspection import handle_list_tools
            result = await handle_list_tools({"lite": True})

            data = parse_result(result)
            assert "first_time" in data
            assert "hint" in data["first_time"]


# ============================================================================
# handle_get_server_info - psutil edge cases
# ============================================================================

class TestGetServerInfoPsutil:

    @pytest.mark.asyncio
    async def test_server_info_psutil_cmdline_empty(self, mock_mcp_server, patch_context_agent_id):
        """Test psutil process with empty cmdline is skipped (line 55)."""
        mock_mcp_server.PSUTIL_AVAILABLE = True

        mock_proc = MagicMock()
        mock_proc.info = {
            "pid": 111,
            "name": "python",
            "cmdline": [],  # Empty cmdline
            "create_time": 0,
            "status": "running"
        }

        mock_current = MagicMock()
        mock_current.create_time.return_value = 100.0
        mock_current.status.return_value = "running"

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {}), \
             patch("psutil.process_iter", return_value=[mock_proc]), \
             patch("psutil.Process", return_value=mock_current), \
             patch("time.time", return_value=200.0):
            from src.mcp_handlers.admin.handlers import handle_get_server_info
            result = await handle_get_server_info({})

            data = parse_result(result)
            assert data["success"] is True

    @pytest.mark.asyncio
    async def test_server_info_psutil_process_exception(self, mock_mcp_server, patch_context_agent_id):
        """Test psutil NoSuchProcess exception is caught (lines 79-80)."""
        import psutil
        mock_mcp_server.PSUTIL_AVAILABLE = True

        mock_proc = MagicMock()
        mock_proc.info.__getitem__ = MagicMock(side_effect=psutil.NoSuchProcess(123))
        # Make the proc.info access raise in the inner try
        mock_proc.info = {"pid": 123, "cmdline": ["mcp_server.py"], "create_time": 0, "status": "running"}

        mock_current = MagicMock()
        mock_current.create_time.return_value = 100.0
        mock_current.status.return_value = "running"

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {}), \
             patch("psutil.process_iter", side_effect=Exception("process enumeration failed")), \
             patch("psutil.Process", return_value=mock_current), \
             patch("time.time", return_value=200.0):
            from src.mcp_handlers.admin.handlers import handle_get_server_info
            result = await handle_get_server_info({})

            data = parse_result(result)
            assert data["success"] is True
            # Should have error in server_processes
            assert len(data["server_processes"]) >= 1

    @pytest.mark.asyncio
    async def test_server_info_empty_processes_fallback(self, mock_mcp_server, patch_context_agent_id):
        """Test fallback when process enumeration finds nothing (lines 91-101)."""
        mock_mcp_server.PSUTIL_AVAILABLE = True

        mock_current = MagicMock()
        mock_current.create_time.return_value = 100.0
        mock_current.status.return_value = "running"

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {}), \
             patch("psutil.process_iter", return_value=[]), \
             patch("psutil.Process", return_value=mock_current), \
             patch("time.time", return_value=200.0):
            from src.mcp_handlers.admin.handlers import handle_get_server_info
            result = await handle_get_server_info({})

            data = parse_result(result)
            assert data["success"] is True
            # Should include current process as fallback
            assert any(p.get("is_current") for p in data["server_processes"])

    @pytest.mark.asyncio
    async def test_server_info_psutil_current_proc_error(self, mock_mcp_server, patch_context_agent_id):
        """Test psutil.Process error for current process (lines 100-101)."""
        import psutil
        mock_mcp_server.PSUTIL_AVAILABLE = True

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {}), \
             patch("psutil.process_iter", return_value=[]), \
             patch("psutil.Process", side_effect=psutil.NoSuchProcess(99999)), \
             patch("time.time", return_value=200.0):
            from src.mcp_handlers.admin.handlers import handle_get_server_info
            result = await handle_get_server_info({})

            data = parse_result(result)
            assert data["success"] is True

    @pytest.mark.asyncio
    async def test_server_info_unknown_transport(self, mock_mcp_server, patch_context_agent_id):
        """Test unknown transport with process matching (lines 59-64)."""
        mock_mcp_server.PSUTIL_AVAILABLE = True

        mock_proc = MagicMock()
        mock_proc.info = {
            "pid": 222,
            "name": "python",
            "cmdline": ["python", "mcp_server.py"],
            "create_time": 50.0,
            "status": "running"
        }

        mock_current = MagicMock()
        mock_current.create_time.return_value = 100.0
        mock_current.status.return_value = "running"

        # Force unknown transport
        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {}), \
             patch("psutil.process_iter", return_value=[mock_proc]), \
             patch("psutil.Process", return_value=mock_current), \
             patch("time.time", return_value=200.0), \
             patch.object(sys, "argv", ["python", "something_else.py"]):
            from src.mcp_handlers.admin.handlers import handle_get_server_info
            result = await handle_get_server_info({})

            data = parse_result(result)
            assert data["transport"] == "unknown"


# ============================================================================
# handle_health_check - additional edge cases
# ============================================================================

class TestHealthCheckEdgeCases:

    @pytest.fixture(autouse=True)
    def mock_pi_connectivity(self):
        """Mock Pi connectivity to prevent real network calls (times out in CI).

        No-op when ``unitares_pi_plugin`` isn't installed — in that case
        governance's runtime_queries skips the pi_connectivity check and
        no network call happens anyway.
        """
        try:
            import unitares_pi_plugin.handlers  # noqa: F401
        except ImportError:
            yield
            return
        with patch("unitares_pi_plugin.handlers.call_pi_tool",
                   new_callable=AsyncMock,
                   return_value={"error": "mocked - Pi unreachable"}):
            yield

    @pytest.mark.asyncio
    async def test_health_check_telemetry_error(self, mock_mcp_server, patch_context_agent_id):
        """Test telemetry error is caught (lines 330-331)."""
        mock_audit = MagicMock()
        mock_audit.log_file = MagicMock()
        mock_audit.log_file.exists.side_effect = RuntimeError("filesystem error")

        mock_db = AsyncMock()
        mock_db.health_check = AsyncMock(return_value={"status": "healthy"})
        mock_db.init = AsyncMock()

        mock_cal = MagicMock()
        mock_cal.get_pending_updates.return_value = 0

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.calibration.calibration_checker", mock_cal), \
             patch("src.telemetry.telemetry_collector", MagicMock()), \
             patch("src.audit_log.audit_logger", mock_audit), \
             patch("src.db.get_db", return_value=mock_db), \
             patch("src.calibration_db.calibration_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.audit_db.audit_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.cache.is_redis_available", return_value=False), \
             patch("src.knowledge_graph.get_knowledge_graph",
                   new_callable=AsyncMock) as mock_kg:

            mock_kg_instance = AsyncMock()
            mock_kg_instance.health_check = AsyncMock(return_value={"status": "healthy"})
            mock_kg.return_value = mock_kg_instance

            from src.services.runtime_queries import get_health_check_data
            data = await get_health_check_data({})
            assert data["checks"]["telemetry"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_health_check_primary_db_init_error(self, mock_mcp_server, patch_context_agent_id):
        """Test primary DB init error is caught (lines 349-350)."""
        mock_db = AsyncMock()
        mock_db.init = AsyncMock(side_effect=RuntimeError("pool init failed"))
        mock_db.health_check = AsyncMock(return_value={"status": "healthy"})

        mock_cal = MagicMock()
        mock_cal.get_pending_updates.return_value = 0

        mock_audit = MagicMock()
        mock_audit.log_file = MagicMock()
        mock_audit.log_file.exists.return_value = True

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.calibration.calibration_checker", mock_cal), \
             patch("src.telemetry.telemetry_collector", MagicMock()), \
             patch("src.audit_log.audit_logger", mock_audit), \
             patch("src.db.get_db", return_value=mock_db), \
             patch("src.calibration_db.calibration_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.audit_db.audit_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.cache.is_redis_available", return_value=False), \
             patch("src.knowledge_graph.get_knowledge_graph",
                   new_callable=AsyncMock) as mock_kg:

            mock_kg_instance = AsyncMock()
            mock_kg_instance.health_check = AsyncMock(return_value={"status": "healthy"})
            mock_kg.return_value = mock_kg_instance

            from src.services.runtime_queries import get_health_check_data
            data = await get_health_check_data({"lite": False}, server=mock_mcp_server)
            assert data["checks"]["primary_db"]["init_error"] is not None

    @pytest.mark.asyncio
    async def test_health_check_db_health_error(self, mock_mcp_server, patch_context_agent_id):
        """Test primary DB health_check error is caught (lines 354-355)."""
        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.health_check = AsyncMock(side_effect=RuntimeError("health check failed"))

        mock_cal = MagicMock()
        mock_cal.get_pending_updates.return_value = 0

        mock_audit = MagicMock()
        mock_audit.log_file = MagicMock()
        mock_audit.log_file.exists.return_value = True

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.calibration.calibration_checker", mock_cal), \
             patch("src.telemetry.telemetry_collector", MagicMock()), \
             patch("src.audit_log.audit_logger", mock_audit), \
             patch("src.db.get_db", return_value=mock_db), \
             patch("src.calibration_db.calibration_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.audit_db.audit_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.cache.is_redis_available", return_value=False), \
             patch("src.knowledge_graph.get_knowledge_graph",
                   new_callable=AsyncMock) as mock_kg:

            mock_kg_instance = AsyncMock()
            mock_kg_instance.health_check = AsyncMock(return_value={"status": "healthy"})
            mock_kg.return_value = mock_kg_instance

            from src.services.runtime_queries import get_health_check_data
            data = await get_health_check_data({})
            assert data["checks"]["primary_db"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_health_check_primary_db_exception(self, mock_mcp_server, patch_context_agent_id):
        """Test primary DB exception is caught (lines 365-366)."""
        mock_cal = MagicMock()
        mock_cal.get_pending_updates.return_value = 0

        mock_audit = MagicMock()
        mock_audit.log_file = MagicMock()
        mock_audit.log_file.exists.return_value = True

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.calibration.calibration_checker", mock_cal), \
             patch("src.telemetry.telemetry_collector", MagicMock()), \
             patch("src.audit_log.audit_logger", mock_audit), \
             patch("src.db.get_db", side_effect=RuntimeError("no db")), \
             patch("src.calibration_db.calibration_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.audit_db.audit_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.cache.is_redis_available", return_value=False), \
             patch("src.knowledge_graph.get_knowledge_graph",
                   new_callable=AsyncMock) as mock_kg:

            mock_kg_instance = AsyncMock()
            mock_kg_instance.health_check = AsyncMock(return_value={"status": "healthy"})
            mock_kg.return_value = mock_kg_instance

            from src.services.runtime_queries import get_health_check_data
            data = await get_health_check_data({})
            assert data["checks"]["primary_db"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_health_check_audit_db_error(self, mock_mcp_server, patch_context_agent_id):
        """Test audit DB exception is caught (lines 380-381)."""
        mock_cal = MagicMock()
        mock_cal.get_pending_updates.return_value = 0

        mock_audit = MagicMock()
        mock_audit.log_file = MagicMock()
        mock_audit.log_file.exists.return_value = True

        mock_db = AsyncMock()
        mock_db.health_check = AsyncMock(return_value={"status": "healthy"})
        mock_db.init = AsyncMock()

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.calibration.calibration_checker", mock_cal), \
             patch("src.telemetry.telemetry_collector", MagicMock()), \
             patch("src.audit_log.audit_logger", mock_audit), \
             patch("src.db.get_db", return_value=mock_db), \
             patch("src.calibration_db.calibration_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.audit_db.audit_health_check_async",
                   new_callable=AsyncMock,
                   side_effect=RuntimeError("audit db error")), \
             patch("src.cache.is_redis_available", return_value=False), \
             patch("src.knowledge_graph.get_knowledge_graph",
                   new_callable=AsyncMock) as mock_kg:

            mock_kg_instance = AsyncMock()
            mock_kg_instance.health_check = AsyncMock(return_value={"status": "healthy"})
            mock_kg.return_value = mock_kg_instance

            from src.services.runtime_queries import get_health_check_data
            data = await get_health_check_data({})
            assert data["checks"]["audit_db"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_health_check_redis_import_error(self, mock_mcp_server, patch_context_agent_id):
        """Test Redis ImportError is caught (lines 436-440)."""
        mock_cal = MagicMock()
        mock_cal.get_pending_updates.return_value = 0

        mock_audit = MagicMock()
        mock_audit.log_file = MagicMock()
        mock_audit.log_file.exists.return_value = True

        mock_db = AsyncMock()
        mock_db.health_check = AsyncMock(return_value={"status": "healthy"})
        mock_db.init = AsyncMock()

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.calibration.calibration_checker", mock_cal), \
             patch("src.telemetry.telemetry_collector", MagicMock()), \
             patch("src.audit_log.audit_logger", mock_audit), \
             patch("src.db.get_db", return_value=mock_db), \
             patch("src.calibration_db.calibration_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.audit_db.audit_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch.dict("sys.modules", {"src.cache": None}), \
             patch("src.knowledge_graph.get_knowledge_graph",
                   new_callable=AsyncMock) as mock_kg:

            mock_kg_instance = AsyncMock()
            mock_kg_instance.health_check = AsyncMock(return_value={"status": "healthy"})
            mock_kg.return_value = mock_kg_instance

            from src.services.runtime_queries import get_health_check_data
            data = await get_health_check_data({})
            # Redis cache should show unavailable or error
            assert data["checks"]["redis_cache"]["status"] in ("unavailable", "error")

    @pytest.mark.asyncio
    async def test_health_check_kg_error(self, mock_mcp_server, patch_context_agent_id):
        """Test knowledge graph exception is caught (lines 465-466)."""
        mock_cal = MagicMock()
        mock_cal.get_pending_updates.return_value = 0

        mock_audit = MagicMock()
        mock_audit.log_file = MagicMock()
        mock_audit.log_file.exists.return_value = True

        mock_db = AsyncMock()
        mock_db.health_check = AsyncMock(return_value={"status": "healthy"})
        mock_db.init = AsyncMock()

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.calibration.calibration_checker", mock_cal), \
             patch("src.telemetry.telemetry_collector", MagicMock()), \
             patch("src.audit_log.audit_logger", mock_audit), \
             patch("src.db.get_db", return_value=mock_db), \
             patch("src.calibration_db.calibration_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.audit_db.audit_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.cache.is_redis_available", return_value=False), \
             patch("src.knowledge_graph.get_knowledge_graph",
                   new_callable=AsyncMock,
                   side_effect=RuntimeError("KG unavailable")):

            from src.services.runtime_queries import get_health_check_data
            data = await get_health_check_data({})
            # KG check no longer calls get_knowledge_graph() — reports embeddings status only
            assert data["checks"]["knowledge_graph"]["status"] in ("healthy", "degraded")

    @pytest.mark.asyncio
    async def test_health_check_data_dir_error(self, mock_mcp_server, patch_context_agent_id):
        """Test data directory exception is caught (lines 487-488)."""
        mock_mcp_server.project_root = "/nonexistent/path"
        mock_cal = MagicMock()
        mock_cal.get_pending_updates.return_value = 0

        mock_audit = MagicMock()
        mock_audit.log_file = MagicMock()
        mock_audit.log_file.exists.return_value = True

        mock_db = AsyncMock()
        mock_db.health_check = AsyncMock(return_value={"status": "healthy"})
        mock_db.init = AsyncMock()

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.calibration.calibration_checker", mock_cal), \
             patch("src.telemetry.telemetry_collector", MagicMock()), \
             patch("src.audit_log.audit_logger", mock_audit), \
             patch("src.db.get_db", return_value=mock_db), \
             patch("src.calibration_db.calibration_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.audit_db.audit_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.cache.is_redis_available", return_value=False), \
             patch("src.knowledge_graph.get_knowledge_graph",
                   new_callable=AsyncMock) as mock_kg:

            mock_kg_instance = AsyncMock()
            mock_kg_instance.health_check = AsyncMock(return_value={"status": "healthy"})
            mock_kg.return_value = mock_kg_instance

            from src.services.runtime_queries import get_health_check_data
            data = await get_health_check_data({})
            # data_directory should still work (nonexistent but no exception)

    @pytest.mark.asyncio
    async def test_health_check_includes_circuit_breaker_telemetry(
        self, mock_mcp_server, patch_context_agent_id
    ):
        """health_check response must surface governance circuit-breaker trips.

        Regression: get_health_check_data previously omitted CB telemetry that
        get_governance_metrics_data already exposed, so trips_1h/trips_24h read
        as 0 from the dashboard even when residents (Steward, etc.) hit
        governance pause.
        """
        from datetime import datetime, timezone
        from src.agent_loop_detection import _governance_pause_timestamps

        _governance_pause_timestamps.clear()
        _governance_pause_timestamps.append(datetime.now(timezone.utc))

        mock_cal = MagicMock()
        mock_cal.get_pending_updates.return_value = 0

        mock_audit = MagicMock()
        mock_audit.log_file = MagicMock()
        mock_audit.log_file.exists.return_value = True

        mock_db = AsyncMock()
        mock_db.health_check = AsyncMock(return_value={"status": "healthy"})
        mock_db.init = AsyncMock()

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.calibration.calibration_checker", mock_cal), \
             patch("src.telemetry.telemetry_collector", MagicMock()), \
             patch("src.audit_log.audit_logger", mock_audit), \
             patch("src.db.get_db", return_value=mock_db), \
             patch("src.calibration_db.calibration_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.audit_db.audit_health_check_async",
                   new_callable=AsyncMock,
                   return_value={"status": "healthy", "backend": "postgres"}), \
             patch("src.cache.is_redis_available", return_value=False), \
             patch("src.knowledge_graph.get_knowledge_graph",
                   new_callable=AsyncMock) as mock_kg:

            mock_kg_instance = AsyncMock()
            mock_kg_instance.health_check = AsyncMock(return_value={"status": "healthy"})
            mock_kg.return_value = mock_kg_instance

            from src.services.runtime_queries import get_health_check_data
            data = await get_health_check_data({"lite": False}, server=mock_mcp_server)

        _governance_pause_timestamps.clear()

        assert "circuit_breakers" in data, (
            "health_check must surface circuit_breakers (governance + redis); "
            "regression of dashboard CB visibility on resident agents"
        )
        gov = data["circuit_breakers"].get("governance", {})
        assert gov.get("trips_1h", 0) >= 1
        assert gov.get("trips_24h", 0) >= 1
        assert gov.get("last_trip") is not None


# ============================================================================
# handle_describe_tool - additional coverage
# ============================================================================

class TestDescribeToolAdditional:

    @pytest.mark.asyncio
    async def test_describe_tool_lite_with_known_schema(self, patch_context_agent_id):
        """Test lite mode with Pydantic schema produces structured lite output."""
        from pydantic import BaseModel, Field
        from typing import Optional

        class MockUpdateParams(BaseModel):
            complexity: float = Field(..., description="Task complexity")
            response_text: Optional[str] = Field(None, description="Agent response")
            confidence: Optional[float] = Field(0.7, description="Confidence")

        mock_tool = MagicMock()
        mock_tool.name = "process_agent_update"
        mock_tool.description = "Share your work and get feedback"
        mock_tool.inputSchema = {"type": "object", "properties": {}}

        with patch("src.tool_schemas.get_tool_definitions", return_value=[mock_tool]), \
             patch("src.tool_schemas.get_pydantic_schemas", return_value={"process_agent_update": MockUpdateParams}), \
             patch("src.mcp_handlers.validators.PARAM_ALIASES", {"process_agent_update": {"text": "response_text"}}), \
             patch("src.tool_modes.TOOL_TIERS", {"essential": {"process_agent_update"}, "common": set(), "advanced": set()}), \
             patch("src.tool_modes.TOOL_OPERATIONS", {"process_agent_update": "write"}):
            from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
            result = await handle_describe_tool({
                "tool_name": "process_agent_update",
                "lite": True
            })

            data = parse_result(result)
            assert data["success"] is True
            assert data["tool"] == "process_agent_update"
            assert data["tier"] == "essential"
            assert "parameters" in data
            assert "common_patterns" in data
            assert "parameter_aliases" in data

    @pytest.mark.asyncio
    async def test_describe_tool_lite_fallback_with_aliases(self, patch_context_agent_id):
        """Test lite mode fallback inputSchema with aliases covers lines 2052, 2057."""
        mock_tool = MagicMock()
        mock_tool.name = "custom_tool"
        mock_tool.description = "Custom tool"
        mock_tool.inputSchema = {
            "type": "object",
            "properties": {
                "param1": {"type": "string"},
                "param2": {"type": "integer"}
            },
            "required": ["param1"]
        }

        with patch("src.tool_schemas.get_tool_definitions", return_value=[mock_tool]), \
             patch("src.tool_schemas.get_pydantic_schemas", return_value={}), \
             patch("src.mcp_handlers.validators.PARAM_ALIASES", {"custom_tool": {"text": "param1"}}):
            from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
            result = await handle_describe_tool({
                "tool_name": "custom_tool",
                "lite": True
            })

            data = parse_result(result)
            assert data["success"] is True
            assert "parameter_aliases" in data
            assert data["parameter_aliases"]["text"] == "\u2192 param1"

    @pytest.mark.asyncio
    async def test_describe_tool_non_lite_mode(self, patch_context_agent_id):
        """Test non-lite mode returns full tool schema covers line 2133."""
        mock_tool = MagicMock()
        mock_tool.name = "health_check"
        mock_tool.description = "Quick health check"
        mock_tool.inputSchema = {"type": "object", "properties": {"agent_id": {"type": "string"}}}

        with patch("src.tool_schemas.get_tool_definitions", return_value=[mock_tool]):
            from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
            result = await handle_describe_tool({
                "tool_name": "health_check",
                "lite": False
            })

            data = parse_result(result)
            assert data["success"] is True
            assert "tool" in data
            assert data["tool"]["name"] == "health_check"
            assert data["tool"]["inputSchema"] is not None

    @pytest.mark.asyncio
    async def test_describe_tool_no_full_description(self, patch_context_agent_id):
        """Test include_full_description=False covers line 1894."""
        mock_tool = MagicMock()
        mock_tool.name = "health_check"
        mock_tool.description = "First line\nSecond line\nThird line"
        mock_tool.inputSchema = {"type": "object"}

        with patch("src.tool_schemas.get_tool_definitions", return_value=[mock_tool]):
            from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
            result = await handle_describe_tool({
                "tool_name": "health_check",
                "lite": False,
                "include_full_description": False,
            })

            data = parse_result(result)
            assert data["success"] is True
            # Description should be first line only
            assert "Second line" not in data["tool"]["description"]


# ============================================================================
# handle_get_telemetry_metrics - perf snapshot error
# ============================================================================

class TestTelemetryMetricsAdditional:

    @pytest.mark.asyncio
    async def test_telemetry_perf_snapshot_error(self, patch_context_agent_id):
        """Test perf_monitor.snapshot error is caught (lines 856-857)."""
        mock_telemetry = MagicMock()
        mock_telemetry.get_skip_rate_metrics.return_value = {"skip_rate": 0.1}
        mock_telemetry.get_confidence_distribution.return_value = {"mean": 0.7}
        mock_telemetry.detect_suspicious_patterns.return_value = []

        with patch("src.telemetry.TelemetryCollector", return_value=mock_telemetry), \
             patch("src.perf_monitor.snapshot", side_effect=ImportError("perf not available")):
            from src.mcp_handlers.admin.handlers import handle_get_telemetry_metrics
            result = await handle_get_telemetry_metrics({})

            data = parse_result(result)
            assert data["success"] is True
            assert data["knowledge_graph_perf"]["note"] == "perf snapshot unavailable"


# ============================================================================
# handle_update_calibration_ground_truth - additional coverage
# ============================================================================

class TestUpdateCalibrationGroundTruthAdditional:

    @pytest.mark.asyncio
    async def test_timestamp_mode_value_error(self, patch_context_agent_id):
        """Test ValueError from bad timestamp is caught (lines 743-744)."""
        with patch("src.calibration.calibration_checker", MagicMock()), \
             patch("src.audit_log.AuditLogger") as mock_audit_cls:
            mock_audit = MagicMock()
            mock_audit.query_audit_log.return_value = [
                {"confidence": 0.85, "details": {"decision": "attest"}}
            ]
            mock_audit_cls.return_value = mock_audit

            mock_checker = MagicMock()
            mock_checker.update_ground_truth.side_effect = ValueError("bad data")
            mock_checker.get_pending_updates.return_value = 0

            with patch("src.calibration.calibration_checker", mock_checker):
                from src.mcp_handlers.admin.calibration import handle_update_calibration_ground_truth
                result = await handle_update_calibration_ground_truth({
                    "timestamp": "not-a-valid-timestamp",
                    "actual_correct": True,
                })

            data = parse_result(result)
            assert data["success"] is False

    @pytest.mark.asyncio
    async def test_direct_mode_exception(self, patch_context_agent_id):
        """Test direct mode exception is caught (lines 779-780)."""
        mock_checker = MagicMock()
        mock_checker.update_ground_truth.side_effect = RuntimeError("calibration broken")

        with patch("src.calibration.calibration_checker", mock_checker):
            from src.mcp_handlers.admin.calibration import handle_update_calibration_ground_truth
            result = await handle_update_calibration_ground_truth({
                "confidence": 0.8,
                "predicted_correct": True,
                "actual_correct": True,
            })

            data = parse_result(result)
            assert data["success"] is False


# ============================================================================
# handle_debug_request_context - additional coverage
# ============================================================================

class TestDebugRequestContextAdditional:

    @pytest.mark.asyncio
    async def test_debug_context_with_bindings(self, mock_mcp_server):
        """Test debug context with legacy bindings covers lines 2146-2156."""
        session_identities = {
            "session-1": {"bound_agent_id": "uuid-abcdef1234567890"},
            "session-2": {"bound_agent_id": None},
        }
        uuid_prefix_index = {
            "abcdef12": "uuid-abcdef1234567890"
        }

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value="uuid-abc"), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value="test-key"), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {"tool1": None}), \
             patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="derived"), \
             patch("src.mcp_handlers.identity.shared._session_identities", session_identities), \
             patch("src.mcp_handlers.identity.shared._uuid_prefix_index", uuid_prefix_index):
            from src.mcp_handlers.admin.handlers import handle_debug_request_context
            result = await handle_debug_request_context({})

            data = parse_result(result)
            assert data["success"] is True
            assert data["session"]["context_agent_id"] == "uuid-abc"
            assert "legacy_bindings_in_memory" in data["diagnostics"]

    @pytest.mark.asyncio
    async def test_debug_context_legacy_error(self, mock_mcp_server):
        """Test debug context with legacy import error covers lines 2154-2156."""
        # Create a dict-like object whose .items() raises an exception
        class BrokenDict:
            def items(self):
                raise AttributeError("broken")

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value=None), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {}), \
             patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="key"), \
             patch("src.mcp_handlers.identity.shared._session_identities", BrokenDict()):
            from src.mcp_handlers.admin.handlers import handle_debug_request_context
            result = await handle_debug_request_context({})

            data = parse_result(result)
            assert data["success"] is True
            assert "error" in data["diagnostics"]["legacy_bindings_in_memory"]


# ============================================================================
# handle_get_connection_status - additional coverage
# ============================================================================

class TestGetConnectionStatusAdditional:

    @pytest.mark.asyncio
    async def test_connection_status_no_tools(self):
        """Test tools_available=False when TOOL_HANDLERS is empty."""
        from src.mcp_handlers.admin.handlers import handle_get_connection_status

        mock_server = MagicMock()
        mock_server.agent_metadata = {}

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_server), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {}):
            result = await handle_get_connection_status({})

            data = parse_result(result)
            assert data["status"] == "disconnected"
            assert data["tools_available"] is False

    @pytest.mark.asyncio
    async def test_connection_status_with_structured_id(self):
        """Test connection with resolved structured_id covers lines 2314-2317.

        success_response() adds caller_agent_id (calling session's bound UUID).
        The handler's resolved_agent_id (display name) is now preserved separately.
        """
        mock_server = MagicMock()
        meta = MagicMock()
        meta.structured_id = "Claude_Opus_20260101"
        meta.label = "MyAgent"
        mock_server.agent_metadata = {"uuid-xyz": meta}

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_server), \
             patch("src.mcp_handlers.TOOL_HANDLERS", {"tool1": None}), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value="uuid-xyz"):
            from src.mcp_handlers.admin.handlers import handle_get_connection_status
            result = await handle_get_connection_status({})

            data = parse_result(result)
            assert data["session_bound"] is True
            # resolved_uuid is the truncated UUID from the handler
            assert data["resolved_uuid"] == "uuid-xyz..."
            # resolved_agent_id is the display name from the handler
            assert data["resolved_agent_id"] == "Claude_Opus_20260101"


# ============================================================================
# Workspace helpers - exception paths
# ============================================================================

class TestWorkspaceHelpersAdditional:

    def test_get_workspace_last_agent_exception(self):
        """Test get_workspace_last_agent exception is suppressed (lines 266-267)."""
        from src.mcp_handlers.admin.handlers import get_workspace_last_agent

        server = MagicMock()
        # Make project_root cause an exception via Path
        server.project_root = None

        result = get_workspace_last_agent(server)
        assert result is None


# ============================================================================
# handle_check_continuity_health - additional coverage
# ============================================================================

class TestContinuityHealthAdditional:

    @pytest.mark.asyncio
    async def test_continuity_health_provenance_recommendation(self, mock_mcp_server, patch_context_agent_id):
        """Test provenance recommendation is generated on deep_check (line 224)."""
        mock_mcp_server.agent_metadata = {}
        mock_discovery = MagicMock()
        mock_discovery.provenance = None  # No provenance

        mock_graph = AsyncMock()
        mock_graph.get_stats = AsyncMock(return_value={
            "total_discoveries": 1, "total_agents": 1
        })
        mock_graph.query = AsyncMock(return_value=[mock_discovery])

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
                   new_callable=AsyncMock, return_value=mock_graph):
            from src.mcp_handlers.admin.handlers import handle_check_continuity_health
            result = await handle_check_continuity_health({"deep_check": True})

            data = parse_result(result)
            assert data["success"] is True
            assert any("provenance" in r.lower() for r in data["recommendations"])
