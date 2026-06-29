"""
Tests for src/mcp_handlers/admin.py - Admin handler functions.

Tests handle_reset_monitor, handle_cleanup_stale_locks, handle_get_server_info,
handle_validate_file_path with mocked backends.
"""

import pytest
import json
import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


# ============================================================================
# handle_reset_monitor
# ============================================================================

class TestResetMonitor:

    @pytest.fixture
    def mock_mcp_server(self):
        server = MagicMock()
        server.monitors = {}
        server.agent_metadata = {}
        return server

    @pytest.mark.asyncio
    async def test_reset_existing_monitor(self, mock_mcp_server):
        mock_mcp_server.monitors = {"agent-1": MagicMock()}
        mock_mcp_server.agent_metadata = {"agent-1": MagicMock(status="active")}

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.admin.handlers.require_registered_agent", return_value=("agent-1", None)):

            from src.mcp_handlers.admin.handlers import handle_reset_monitor
            result = await handle_reset_monitor({"agent_id": "agent-1"})

            data = json.loads(result[0].text)
            assert "Monitor reset" in data["message"]
            assert "agent-1" not in mock_mcp_server.monitors

    @pytest.mark.asyncio
    async def test_reset_nonexistent_monitor(self, mock_mcp_server):
        mock_mcp_server.monitors = {}

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.admin.handlers.require_registered_agent", return_value=("agent-1", None)):

            from src.mcp_handlers.admin.handlers import handle_reset_monitor
            result = await handle_reset_monitor({"agent_id": "agent-1"})

            data = json.loads(result[0].text)
            assert "not found" in data["message"]

    @pytest.mark.asyncio
    async def test_reset_requires_registration(self, mock_mcp_server):
        from mcp.types import TextContent
        error = TextContent(type="text", text='{"error": "not registered"}')

        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.admin.handlers.require_registered_agent", return_value=(None, error)):

            from src.mcp_handlers.admin.handlers import handle_reset_monitor
            result = await handle_reset_monitor({})

            assert "not registered" in result[0].text


# ============================================================================
# handle_cleanup_stale_locks
# ============================================================================

class TestCleanupStaleLocks:

    @pytest.mark.asyncio
    async def test_cleanup_success(self):
        mock_result = {
            "cleaned": 2, "kept": 1, "errors": 0,
            "cleaned_locks": ["lock1", "lock2"], "kept_locks": ["lock3"],
        }
        # cleanup_stale_state_locks is imported inside the handler via `from src.lock_cleanup import ...`
        with patch("src.lock_cleanup.cleanup_stale_state_locks", return_value=mock_result):
            from src.mcp_handlers.admin.handlers import handle_cleanup_stale_locks
            result = await handle_cleanup_stale_locks({})

            data = json.loads(result[0].text)
            assert data["cleaned"] == 2
            assert data["kept"] == 1

    @pytest.mark.asyncio
    async def test_cleanup_dry_run(self):
        mock_result = {
            "cleaned": 0, "kept": 3, "errors": 0,
            "cleaned_locks": [], "kept_locks": ["a", "b", "c"],
        }
        with patch("src.lock_cleanup.cleanup_stale_state_locks", return_value=mock_result):
            from src.mcp_handlers.admin.handlers import handle_cleanup_stale_locks
            result = await handle_cleanup_stale_locks({"dry_run": True})

            data = json.loads(result[0].text)
            assert data["dry_run"] is True

    @pytest.mark.asyncio
    async def test_cleanup_custom_max_age(self):
        mock_result = {
            "cleaned": 0, "kept": 0, "errors": 0,
            "cleaned_locks": [], "kept_locks": [],
        }
        with patch("src.lock_cleanup.cleanup_stale_state_locks", return_value=mock_result) as mock_fn:
            from src.mcp_handlers.admin.handlers import handle_cleanup_stale_locks
            await handle_cleanup_stale_locks({"max_age_seconds": 600.0})

            # Verify max_age was passed through
            call_kwargs = mock_fn.call_args
            assert call_kwargs[1]["max_age_seconds"] == 600.0 or call_kwargs.kwargs.get("max_age_seconds") == 600.0


# ============================================================================
# handle_validate_file_path
# ============================================================================

class TestValidateFilePath:

    @pytest.mark.asyncio
    async def test_validate_normal_path(self):
        with patch("src.mcp_handlers.admin.handlers.validate_file_path_policy") as mock_validator:
            mock_validator.return_value = (True, None, [])

            from src.mcp_handlers.admin.handlers import handle_validate_file_path
            result = await handle_validate_file_path({"file_path": "src/main.py"})

            data = json.loads(result[0].text)
            assert data.get("allowed") is True or "valid" in json.dumps(data).lower()

    @pytest.mark.asyncio
    async def test_validate_blocked_path(self):
        with patch("src.mcp_handlers.admin.handlers.validate_file_path_policy") as mock_validator:
            mock_validator.return_value = (False, "File creation blocked by policy", ["anti-proliferation"])

            from src.mcp_handlers.admin.handlers import handle_validate_file_path
            result = await handle_validate_file_path({"file_path": "/tmp/junk.py"})

            text = result[0].text
            # Should indicate the path is blocked
            assert "blocked" in text.lower() or "not allowed" in text.lower() or "error" in text.lower() or "policy" in text.lower()


# ============================================================================
# handle_get_server_info (lightweight - mostly process introspection)
# ============================================================================

class TestGetServerInfo:

    @pytest.fixture
    def mock_mcp_server(self):
        server = MagicMock()
        server.SERVER_VERSION = "2.5.8"
        server.SERVER_BUILD_DATE = "2026-02-05"
        server.PSUTIL_AVAILABLE = False
        server.project_root = str(project_root)
        return server

    @pytest.mark.asyncio
    async def test_server_info_without_psutil(self, mock_mcp_server):
        with patch("src.mcp_handlers.admin.handlers.mcp_server", mock_mcp_server):
            from src.mcp_handlers.admin.handlers import handle_get_server_info
            # Patch TOOL_HANDLERS for tool count
            with patch("src.mcp_handlers.TOOL_HANDLERS", {"tool1": None, "tool2": None}):
                result = await handle_get_server_info({})

                data = json.loads(result[0].text)
                assert data["server_version"] == "2.5.8"
                assert data["tool_count"] == 2


# ============================================================================
# handle_get_tool_usage_stats
# ============================================================================

class TestGetToolUsageStats:

    @pytest.mark.asyncio
    async def test_usage_stats_default(self):
        # DB sink (audit.tool_usage) is now the primary source.
        db_stats = {"total_calls": 100, "unique_tools": 15, "source": "db"}
        with patch("src.audit_db.get_tool_usage_stats_async", new=AsyncMock(return_value=db_stats)):
            from src.mcp_handlers.admin.handlers import handle_get_tool_usage_stats
            result = await handle_get_tool_usage_stats({})

            data = json.loads(result[0].text)
            assert data["total_calls"] == 100

    @pytest.mark.asyncio
    async def test_usage_stats_with_filters(self):
        db_reader = AsyncMock(return_value={"total_calls": 10, "source": "db"})
        with patch("src.audit_db.get_tool_usage_stats_async", new=db_reader):
            from src.mcp_handlers.admin.handlers import handle_get_tool_usage_stats
            result = await handle_get_tool_usage_stats({
                "tool_name": "ping_agent",
                "agent_id": "agent-1",
                "window_hours": 48,
            })

            data = json.loads(result[0].text)
            assert data["total_calls"] == 10
            # Filters flow through to the DB reader.
            call_kwargs = db_reader.call_args.kwargs
            assert call_kwargs.get("tool_name") == "ping_agent"
            assert call_kwargs.get("agent_id") == "agent-1"
            assert call_kwargs.get("window_hours") == 48


# ============================================================================
# handle_health_check
# ============================================================================

class TestHealthCheck:

    @pytest.mark.asyncio
    async def test_health_check_all_healthy(self):
        mock_calibration = MagicMock()
        mock_calibration.get_pending_updates.return_value = 0

        mock_telemetry = MagicMock()
        mock_telemetry.get_health.return_value = {"status": "healthy"}

        mock_audit = MagicMock()
        mock_audit.get_health.return_value = {"status": "healthy"}

        mock_db = AsyncMock()
        mock_db.health_check = AsyncMock(return_value={"status": "healthy"})

        with patch("src.mcp_handlers.admin.handlers.mcp_server") as mock_server, \
             patch("src.calibration.calibration_checker", mock_calibration), \
             patch("src.telemetry.telemetry_collector", mock_telemetry), \
             patch("src.audit_log.audit_logger", mock_audit), \
             patch("src.db.get_db", return_value=mock_db), \
             patch("src.calibration_db.calibration_health_check_async", new_callable=AsyncMock, return_value={"status": "healthy", "total_entries": 5}):

            mock_server.project_root = str(project_root)

            # Option F: handle_health_check reads from the cached snapshot.
            # Test the underlying builder directly (get_health_check_data).
            from src.services.runtime_queries import get_health_check_data
            data = await get_health_check_data({}, server=mock_server)

            assert "checks" in data
            assert "calibration" in data["checks"]
            assert data["checks"]["calibration"]["status"] == "healthy"
            # Overall status depends on all backends; just verify it exists
            assert "status" in data
            assert data["status"] in ("healthy", "moderate", "critical")
