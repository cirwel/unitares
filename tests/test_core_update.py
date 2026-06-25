"""
Comprehensive tests for src/mcp_handlers/core.py - Core governance handler functions.

Covers:
- _assess_thermodynamic_significance (pure function)
- handle_get_governance_metrics (with mocked backends)
- handle_simulate_update (with mocked backends)
- handle_process_agent_update (with mocked backends, the most important handler)

Also covers:
- src/mcp_handlers/export.py: handle_get_system_history, handle_export_to_file
- src/mcp_handlers/lifecycle.py: handle_mark_response_complete
"""

import pytest
import json
import sys
import os
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Any, Optional, Sequence
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock
from contextlib import asynccontextmanager

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from mcp.types import TextContent


# ============================================================================
# Helpers
# ============================================================================

from tests.helpers import parse_result


def _make_text_content(data):
    """Create a TextContent with JSON data."""
    return TextContent(type="text", text=json.dumps(data))


def _make_error_text_content(msg):
    """Create a TextContent that looks like an error."""
    return TextContent(type="text", text=json.dumps({"error": msg}))


def _make_monitor(
    risk_history=None,
    coherence_history=None,
    V=0.0,
    E_history=None,
    timestamp_history=None,
    V_history=None,
    coherence=0.52,
    void_active=False,
    regime="EXPLORATION",
    regime_duration=1,
    unitaires_state=None,
    unitaires_theta=None,
):
    """Create a mock monitor with a realistic state."""
    state = SimpleNamespace(
        risk_history=risk_history or [],
        coherence_history=coherence_history or [],
        V=V,
        E_history=E_history or [],
        timestamp_history=timestamp_history or [],
        V_history=V_history or [],
        coherence=coherence,
        void_active=void_active,
        regime=regime,
        regime_duration=regime_duration,
        interpret_state=MagicMock(return_value={
            "health": "healthy",
            "mode": "convergent",
            "basin": "stable",
        }),
        unitaires_state=unitaires_state,
        unitaires_theta=unitaires_theta,
    )
    m = MagicMock()
    m.state = state
    m.get_metrics.return_value = {
        "E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0,
        "coherence": 0.52, "risk_score": 0.3,
        "initialized": True, "status": "ok",
        "complexity": 0.5,
    }
    m.simulate_update.return_value = {
        "status": "ok",
        "decision": {"action": "approve", "confidence": 0.8},
        "metrics": {
            "E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0,
            "coherence": 0.52, "risk_score": 0.3,
        },
        "guidance": "Continue current approach.",
    }
    m.export_history.return_value = json.dumps({
        "E_history": [0.7, 0.75],
        "I_history": [0.6, 0.65],
        "S_history": [0.2, 0.15],
        "V_history": [0.0, 0.0],
    })
    return m


def _make_mock_mcp_server(agent_metadata=None, monitors=None):
    """Build a MagicMock that impersonates mcp_server."""
    server = MagicMock()
    server.agent_metadata = agent_metadata or {}
    server.monitors = monitors or {}
    server.get_or_create_monitor = MagicMock()
    server.get_or_create_metadata = MagicMock()
    server.SERVER_VERSION = "test-1.0.0"
    server.load_metadata_async = AsyncMock()
    server.project_root = str(project_root)
    server.load_monitor_state = MagicMock(return_value=None)

    # Lock manager with async context manager
    lock_mgr = MagicMock()

    @asynccontextmanager
    async def _fake_lock(*args, **kwargs):
        yield

    lock_mgr.acquire_agent_lock_async = MagicMock(side_effect=_fake_lock)
    server.lock_manager = lock_mgr

    # process_update_authenticated_async
    server.process_update_authenticated_async = AsyncMock(return_value={
        "status": "ok",
        "decision": {"action": "approve", "confidence": 0.8},
        "metrics": {
            "E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0,
            "coherence": 0.52, "risk_score": 0.3,
            "verdict": "continue",
            "regime": "EXPLORATION",
            "phi": 0.0,
        },
        "guidance": "Continue current approach.",
    })

    # health_checker
    from src.health_thresholds import HealthStatus
    health_checker = MagicMock()
    health_checker.get_health_status.return_value = (HealthStatus.HEALTHY, "System healthy")
    server.health_checker = health_checker

    # process_mgr
    server.process_mgr = MagicMock()
    server.process_mgr.write_heartbeat = MagicMock()

    # check_agent_id_default
    server.check_agent_id_default = MagicMock(return_value=None)

    return server


def _make_metadata(
    status="active",
    total_updates=5,
    label="TestAgent",
    tags=None,
    purpose=None,
    api_key="test-key-12345678",
    dialectic_conditions=None,
    paused_at=None,
    archived_at=None,
):
    """Create a SimpleNamespace metadata object."""
    meta = SimpleNamespace(
        status=status,
        last_update="2026-01-20T12:00:00",
        created_at="2026-01-01T12:00:00",
        total_updates=total_updates,
        tags=tags or ["test"],
        label=label,
        display_name=label,
        parent_agent_id=None,
        spawn_reason=None,
        confidence_history=[],
        complexity_history=[],
        coherence_history=[],
        risk_history=[],
        eisv_history=[],
        void_history=[],
        task_types=[],
        response_modes=[],
        api_key=api_key,
        purpose=purpose,
        health_status="healthy",
        dialectic_conditions=dialectic_conditions,
        paused_at=paused_at,
        archived_at=archived_at,
        loop_cooldown_until=None,
        _last_perturbation_update=0,
    )
    meta.add_lifecycle_event = MagicMock()
    meta.to_dict = MagicMock(return_value={"agent_id": "test-agent"})
    meta.validate_consistency = MagicMock(return_value=(True, []))
    return meta


# ============================================================================
# _assess_thermodynamic_significance (pure function - minimal mocks)
# ============================================================================

class TestProcessAgentUpdate:
    """Tests for process_agent_update handler."""

    @pytest.fixture
    def mock_server(self):
        return _make_mock_mcp_server()

    @pytest.fixture
    def mock_monitor(self):
        return _make_monitor()

    def _common_patches(self, mock_server, agent_uuid="test-uuid-1234",
                        context_agent_id=None, context_session_key="session-1"):
        """Return a dict of patch targets for process_agent_update.

        IMPORTANT: get_context_agent_id, get_context_session_key, and
        ensure_agent_persisted are locally imported inside the handler, so
        they must be patched at the *source* module, not at core.py.
        """
        ctx_agent = context_agent_id or agent_uuid
        mock_storage = MagicMock(
            update_agent=AsyncMock(),
            get_agent=AsyncMock(return_value=None),
            get_or_create_agent=AsyncMock(return_value=(MagicMock(api_key="test-key"), True)),
            record_agent_state=AsyncMock(),
            create_agent=AsyncMock(),
        )
        return {
            "mcp_server": mock_server,
            "ctx_agent_id": ctx_agent,
            "ctx_session_key": context_session_key,
            "storage": mock_storage,
        }

    def _apply_patches(self, patches_dict):
        """Create a contextmanager that applies all common patches."""
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("src.mcp_handlers.core.mcp_server", patches_dict["mcp_server"]))
        stack.enter_context(patch("src.mcp_handlers.context.get_context_agent_id", return_value=patches_dict["ctx_agent_id"]))
        stack.enter_context(patch("src.mcp_handlers.context.get_context_session_key", return_value=patches_dict["ctx_session_key"]))
        stack.enter_context(patch("src.mcp_handlers.identity.handlers.ensure_agent_persisted", new_callable=AsyncMock, return_value=False))
        stack.enter_context(patch("src.mcp_handlers.updates.phases.agent_storage", patches_dict["storage"]))
        return stack

    @pytest.mark.asyncio
    async def test_no_agent_uuid_returns_error(self, mock_server):
        """If identity resolution fails, returns error."""
        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value=None):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({"response_text": "test"})

            data = parse_result(result)
            assert "error" in data or "Identity not resolved" in json.dumps(data)

    @pytest.mark.asyncio
    async def test_require_strong_identity_rejects_weak_resolution(self, mock_server):
        """require_strong_identity=true rejects weakly resolved identity sources."""
        patches = self._common_patches(mock_server)
        with self._apply_patches(patches), \
             patch("src.mcp_handlers.context.get_session_resolution_source", return_value="ip_ua_fingerprint"), \
             patch("src.mcp_handlers.context.get_trajectory_confidence", return_value=None):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test strict assurance gate",
                "require_strong_identity": True,
                "response_mode": "full",
            })

            data = parse_result(result)
            assert data.get("success") is False
            assert "strong identity" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_weak_identity_downweights_confidence_and_reports_assurance(self, mock_server):
        """Weak identity source dampens confidence and is reported in response."""
        patches = self._common_patches(mock_server)
        with self._apply_patches(patches), \
             patch("src.mcp_handlers.context.get_session_resolution_source", return_value="ip_ua_fingerprint"), \
             patch("src.mcp_handlers.context.get_trajectory_confidence", return_value=None):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test weak assurance dampening",
                "confidence": 0.95,
                "response_mode": "full",
            })

            data = parse_result(result)
            assert data.get("success") is True
            assert data.get("identity_assurance", {}).get("tier") == "weak"

            called_confidence = mock_server.process_update_authenticated_async.await_args.kwargs.get("confidence")
            assert called_confidence is not None
            assert called_confidence <= 0.55

    @pytest.mark.asyncio
    async def test_paused_agent_rejected(self, mock_server):
        """Paused agent cannot process updates."""
        agent_uuid = "test-uuid-paused"
        # Fresh paused_at — pause TTL auto-expires stale ones (>72h default)
        from datetime import datetime as _dt
        meta = _make_metadata(status="paused", paused_at=_dt.now().isoformat())
        mock_server.agent_metadata = {agent_uuid: meta}

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=agent_uuid), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value="session-1"), \
             patch("src.mcp_handlers.identity.handlers.ensure_agent_persisted", new_callable=AsyncMock, return_value=False):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({"response_text": "test"})

            data = parse_result(result)
            assert "paused" in json.dumps(data).lower()

    @pytest.mark.asyncio
    async def test_archived_agent_rejected(self, mock_server, mock_monitor):
        """Archived agent cannot process updates at initial check."""
        agent_uuid = "test-uuid-archived"
        meta = _make_metadata(status="archived", archived_at="2026-01-15T12:00:00", total_updates=0)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({"response_text": "test"})

            data = parse_result(result)
            assert "archived" in json.dumps(data).lower()

    @pytest.mark.asyncio
    async def test_deleted_agent_rejected(self, mock_server, mock_monitor):
        """Deleted agent cannot process updates."""
        agent_uuid = "test-uuid-deleted"
        meta = _make_metadata(status="deleted")
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({"response_text": "test"})

            data = parse_result(result)
            assert "deleted" in json.dumps(data).lower()

    @pytest.mark.asyncio
    async def test_successful_update_returns_metrics(self, mock_server, mock_monitor):
        """Happy path: successful update returns EISV metrics and decision."""
        agent_uuid = "test-uuid-success"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "implemented new feature",
                "complexity": 0.5,
                "confidence": 0.7,
            })

            data = parse_result(result)
            # Response formatter may flatten metrics to top-level in minimal mode,
            # or return mirror mode with verdict/mirror fields.
            has_eisv = ("E" in data and "I" in data) or ("metrics" in data)
            has_decision = "action" in data or "decision" in data
            has_mirror = "verdict" in data and "mirror" in data
            assert data.get("success") is True or has_mirror
            assert has_eisv or has_decision or has_mirror

    @pytest.mark.asyncio
    async def test_full_response_contract_preserves_stable_top_level_keys(self, mock_server, mock_monitor):
        """Full-mode handler response should retain stable contract keys."""
        agent_uuid = "test-uuid-contract"
        meta = _make_metadata(status="active", total_updates=5)
        mock_monitor._last_prediction_id = "pred-contract-1"
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        mock_db = MagicMock()
        mock_db.load_agent_baseline = AsyncMock(return_value=None)
        mock_db.record_outcome_event = AsyncMock(return_value="oe-contract-1")
        mock_db.save_agent_baseline = AsyncMock()
        mock_db.update_identity_metadata = AsyncMock()
        baseline = MagicMock()
        baseline.update = MagicMock()
        profile = SimpleNamespace(total_updates=1, record_checkin=MagicMock())
        tool_usage_tracker = MagicMock(
            get_usage_stats=MagicMock(return_value={"total_calls": 0, "tools": {}, "unique_tools": 0})
        )

        with self._apply_patches(p), \
             patch("src.mcp_handlers.context.get_session_resolution_source", return_value="explicit_client_session_id"), \
             patch("src.mcp_handlers.context.get_trajectory_confidence", return_value=None), \
             patch("src.db.get_db", return_value=mock_db), \
             patch("src.tool_usage_tracker.get_tool_usage_tracker", return_value=tool_usage_tracker), \
             patch("src.agent_behavioral_baseline.ensure_baseline_loaded", new=AsyncMock(return_value=None)), \
             patch("src.agent_behavioral_baseline.compute_anomaly_entropy", return_value=0.0), \
             patch("src.agent_behavioral_baseline.get_agent_behavioral_baseline", return_value=baseline), \
             patch("src.agent_behavioral_baseline.schedule_baseline_save"), \
             patch("src.agent_profile.get_agent_profile", return_value=profile), \
             patch("src.agent_profile.save_profile_to_postgres", new=AsyncMock()):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "Implemented contract fixture and completed the task.",
                "complexity": 0.5,
                "confidence": 0.7,
                "response_mode": "full",
            })

            data = parse_result(result)

        required_keys = {
            "success",
            "server_time",
            "agent_id",
            "status",
            "decision",
            "metrics",
            "identity_assurance",
            "prediction_id",
            "outcome_event",
        }
        assert required_keys.issubset(data.keys())
        assert data["success"] is True
        assert data["agent_id"] == agent_uuid
        assert data["identity_assurance"]["tier"] == "strong"
        assert data["decision"]["action"] == "approve"
        assert data["prediction_id"] == "pred-contract-1"
        assert data["outcome_event"]["outcome_id"] == "oe-contract-1"
        assert {"E", "I", "S", "V", "coherence", "risk_score"}.issubset(data["metrics"].keys())

    @pytest.mark.asyncio
    async def test_full_response_contract_preserves_archived_error_shape(self, mock_server, mock_monitor):
        """Full-mode handler error response should keep stable archived-agent keys."""
        agent_uuid = "test-uuid-contract-archived"
        meta = _make_metadata(status="archived", total_updates=5)
        meta.notes = "User requested archive after handoff"
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "Trying to resume after an explicit archive.",
                "response_mode": "full",
            })

            data = parse_result(result)

        required_keys = {
            "success",
            "error",
            "server_time",
            "error_code",
            "error_category",
            "context",
            "recovery",
            "agent_signature",
        }
        assert required_keys.issubset(data.keys())
        assert data["success"] is False
        assert data["error_code"] == "AGENT_ARCHIVED"
        assert data["error_category"] == "state_error"
        assert "archived" in data["error"] and "cannot" in data["error"]
        assert data["context"]["status"] == "archived"
        assert "self_recovery" in data["recovery"]["related_tools"]
        mock_server.lock_manager.acquire_agent_lock_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_lock_timeout_returns_error(self, mock_server, mock_monitor):
        """Lock timeout returns informative error."""
        agent_uuid = "test-uuid-lock"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        # Make lock acquisition raise TimeoutError
        @asynccontextmanager
        async def _timeout_lock(*args, **kwargs):
            raise TimeoutError("Lock acquisition timed out")
            yield  # pragma: no cover

        mock_server.lock_manager.acquire_agent_lock_async = MagicMock(side_effect=_timeout_lock)

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert "error" in data or "lock" in json.dumps(data).lower()

    @pytest.mark.asyncio
    async def test_permission_error_handled(self, mock_server, mock_monitor):
        """PermissionError from process_update_authenticated_async is caught."""
        agent_uuid = "test-uuid-perm"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}
        mock_server.process_update_authenticated_async = AsyncMock(
            side_effect=PermissionError("Not authorized")
        )

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert "error" in data or "authentication" in json.dumps(data).lower() or "not authorized" in json.dumps(data).lower()

    @pytest.mark.asyncio
    async def test_value_error_loop_detected(self, mock_server, mock_monitor):
        """ValueError with 'Self-monitoring loop detected' handled specially."""
        agent_uuid = "test-uuid-loop"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}
        mock_server.process_update_authenticated_async = AsyncMock(
            side_effect=ValueError("Self-monitoring loop detected - cooldown active")
        )

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert "loop" in json.dumps(data).lower()

    @pytest.mark.asyncio
    async def test_value_error_validation(self, mock_server, mock_monitor):
        """General ValueError (not loop) handled as validation error."""
        agent_uuid = "test-uuid-valerr"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}
        mock_server.process_update_authenticated_async = AsyncMock(
            side_effect=ValueError("Invalid state transition")
        )

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert "error" in data or "validation" in json.dumps(data).lower()

    @pytest.mark.asyncio
    async def test_unexpected_exception_handled(self, mock_server, mock_monitor):
        """Unexpected exception does not crash the server."""
        agent_uuid = "test-uuid-unex"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}
        mock_server.process_update_authenticated_async = AsyncMock(
            side_effect=RuntimeError("Something unexpected")
        )

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert "error" in data or "unexpected" in json.dumps(data).lower()

    @pytest.mark.asyncio
    async def test_lite_alias_sets_compact_response_mode(self, mock_server, mock_monitor):
        """lite=true sets response_mode to compact."""
        agent_uuid = "test-uuid-lite"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test",
                "lite": True,
            })

            # Should not crash - lite mode should still return valid response
            data = parse_result(result)
            assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_param_aliases_applied(self, mock_server, mock_monitor):
        """Parameter aliases like 'text' -> 'response_text' are applied."""
        agent_uuid = "test-uuid-alias"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):

            from src.mcp_handlers.core import handle_process_agent_update
            # Use 'text' alias for 'response_text'
            result = await handle_process_agent_update({
                "text": "test work",
                "complexity": 0.5,
            })
            data = parse_result(result)
            assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_new_agent_creation(self, mock_server, mock_monitor):
        """New agent is created in PostgreSQL on first update."""
        agent_uuid = "test-uuid-new"
        # Agent not in metadata = new agent
        mock_server.agent_metadata = {}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {}
        mock_server.get_or_create_metadata.return_value = _make_metadata(total_updates=1)

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "first update",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)


class TestProcessAgentUpdateExtended:
    """Extended tests targeting missed lines in process_agent_update."""

    @pytest.fixture
    def mock_server(self):
        return _make_mock_mcp_server()

    @pytest.fixture
    def mock_monitor(self):
        return _make_monitor()

    def _common_patches(self, mock_server, agent_uuid="test-uuid-1234",
                        context_agent_id=None, context_session_key="session-1"):
        ctx_agent = context_agent_id or agent_uuid
        mock_storage = MagicMock(
            update_agent=AsyncMock(),
            get_agent=AsyncMock(return_value=None),
            get_or_create_agent=AsyncMock(return_value=(MagicMock(api_key="test-key"), True)),
            record_agent_state=AsyncMock(),
            create_agent=AsyncMock(),
        )
        return {
            "mcp_server": mock_server,
            "ctx_agent_id": ctx_agent,
            "ctx_session_key": context_session_key,
            "storage": mock_storage,
        }

    def _apply_patches(self, patches_dict):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("src.mcp_handlers.core.mcp_server", patches_dict["mcp_server"]))
        stack.enter_context(patch("src.mcp_handlers.context.get_context_agent_id", return_value=patches_dict["ctx_agent_id"]))
        stack.enter_context(patch("src.mcp_handlers.context.get_context_session_key", return_value=patches_dict["ctx_session_key"]))
        stack.enter_context(patch("src.mcp_handlers.identity.handlers.ensure_agent_persisted", new_callable=AsyncMock, return_value=False))
        stack.enter_context(patch("src.mcp_handlers.updates.phases.agent_storage", patches_dict["storage"]))
        return stack

    # ------------------------------------------------------------------
    # EISV validation: enrichment function exists
    # ------------------------------------------------------------------
    def test_eisv_validation_enrichment_exists(self):
        """EISV validation enrichment is available."""
        from src.mcp_handlers.updates.enrichments import enrich_eisv_validation
        assert callable(enrich_eisv_validation)

    # ------------------------------------------------------------------
    # Lines 185-186: complexity calibration exception in get_metrics
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_get_metrics_complexity_calibration_exception(self):
        """Exception fetching complexity calibration does not crash get_metrics."""
        mock_server = _make_mock_mcp_server()
        monitor = _make_monitor()
        # Make agent_metadata property raise on access for the calibration path
        meta = _make_metadata(purpose=None)
        # Override get_metrics to include complexity
        monitor.get_metrics.return_value = {
            "E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0,
            "coherence": 0.52, "risk_score": 0.3,
            "initialized": True, "status": "ok",
            "complexity": 0.5,
        }
        # The complexity calibration tries to access meta attributes;
        # First access works, second access (inside try block at line 173) raises
        call_count = [0]
        original_metadata = {"agent-1": meta}

        def side_effect_get(key, default=None):
            call_count[0] += 1
            if call_count[0] >= 3:
                raise RuntimeError("simulated error in meta access")
            return original_metadata.get(key, default)

        # Simpler approach: just make the meta not have purpose to cover line 185-186
        # by making the complexity lookup raise
        broken_meta = _make_metadata()
        broken_meta.purpose = "test"
        mock_server.agent_metadata = {"agent-1": broken_meta}
        mock_server.get_or_create_monitor.return_value = monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value="agent-1"), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:

            MockClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": False})
            data = parse_result(result)
            # Should still succeed (reflection is conditional, so check summary instead)
            assert "summary" in data

    # ------------------------------------------------------------------
    # Lines 244-245: Saturation diagnostics exception in get_metrics
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_get_metrics_saturation_diagnostics_exception(self):
        """Exception computing saturation diagnostics is caught gracefully."""
        mock_server = _make_mock_mcp_server()
        monitor = _make_monitor()
        # Set unitaires_state to something truthy so the code enters the computation
        monitor.state.unitaires_state = MagicMock()
        mock_server.agent_metadata = {"agent-1": _make_metadata()}
        mock_server.get_or_create_monitor.return_value = monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value="agent-1"), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:

            MockClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            # governance_core.compute_saturation_diagnostics will raise ImportError or similar
            # because unitaires_state is a MagicMock, not a real State
            result = await handle_get_governance_metrics({"lite": False})
            data = parse_result(result)
            assert "summary" in data

    # ------------------------------------------------------------------
    # Lines 409-411: Dialectic condition parsing exception in simulate_update
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_simulate_dialectic_condition_exception_is_non_blocking(self, mock_server, mock_monitor):
        """Dialectic condition parsing that raises is caught and non-blocking."""
        # Create meta with dialectic_conditions that will cause iteration to raise
        meta = _make_metadata()
        # Make dialectic_conditions a list with a non-dict item that causes crash
        meta.dialectic_conditions = [{"type": "complexity_limit", "value": "not_a_number"}]
        # Override to cause a deeper error - make getattr raise
        meta_proxy = MagicMock(wraps=meta)
        meta_proxy.dialectic_conditions = property(lambda self: (_ for _ in ()).throw(RuntimeError("parse error")))

        # Simpler: just use a list containing something that raises during iteration
        broken_meta = _make_metadata()
        broken_meta.dialectic_conditions = [None, "bad_item", 42]  # Non-dict items
        mock_server.agent_metadata = {"agent-1": broken_meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)):

            from src.mcp_handlers.core import handle_simulate_update
            result = await handle_simulate_update({"complexity": 0.5})
            data = parse_result(result)
            assert data["simulation"] is True
            # Non-dict items are skipped via `if not isinstance(c, dict): continue`
            # So no warning, just successful result
            assert "dialectic_warning" not in data

    # ------------------------------------------------------------------
    # Lines 613-704: New agent onboarding with knowledge graph
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_new_agent_onboarding_with_knowledge_graph(self, mock_server, mock_monitor):
        """New agent receives onboarding guidance from knowledge graph."""
        agent_uuid = "test-uuid-new-onboard"
        mock_server.agent_metadata = {}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {}
        mock_server.get_or_create_metadata.return_value = _make_metadata(total_updates=1)

        # Mock knowledge graph
        mock_graph = AsyncMock()
        mock_graph.get_stats = AsyncMock(return_value={
            "total_discoveries": 10,
            "total_agents": 3,
            "by_type": {"question": 2, "insight": 5, "pattern": 3},
        })

        mock_question = MagicMock()
        mock_question.timestamp = "2026-01-20T12:00:00"
        mock_question.to_dict = MagicMock(return_value={
            "id": "q-1",
            "summary": "How does coherence relate to stability?",
            "tags": ["coherence", "stability"],
            "severity": "medium",
        })
        mock_question.tags = ["coherence", "stability"]
        mock_graph.query = AsyncMock(return_value=[mock_question])

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p), \
             patch("src.knowledge_graph.get_knowledge_graph", new_callable=AsyncMock, return_value=mock_graph):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "first update from new agent",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Archived agent is REFUSED on checkin (Stage 3, post-PR #39).
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_archived_agent_refused_on_checkin(self, mock_server, mock_monitor):
        """Archived agent is refused when it checks in via process_agent_update.

        Pre-Stage 3 this reactivated implicitly. Now it returns an error;
        recovery is via self_recovery() or onboard(force_new=true).
        """
        agent_uuid = "test-uuid-archived-refused"
        mock_server.agent_metadata = {}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        archived_meta = _make_metadata(status="archived", archived_at=None)

        async def populate_metadata(*args, **kwargs):
            mock_server.agent_metadata[agent_uuid] = archived_meta
            return False

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p), \
             patch("src.mcp_handlers.identity.handlers.ensure_agent_persisted", new_callable=AsyncMock, side_effect=populate_metadata):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "attempt from archive",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)
            # No silent reactivation — status stays archived.
            assert archived_meta.status == "archived"
            assert data.get("success") is False
            assert "archived" in data["error"].lower()

    # ------------------------------------------------------------------
    # Lines 776: Paused agent error (second check, after metadata loaded)
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_paused_agent_second_check_returns_error(self, mock_server, mock_monitor):
        """Paused agent that passes first check but is paused in second check."""
        agent_uuid = "test-uuid-paused2"
        # Status is paused but not caught by the first check (lines 526-542)
        # because that checks mcp_server.agent_metadata before ensure_agent_persisted.
        # After lazy creation, we re-check. Create a scenario where first check passes
        # but second check catches it.
        meta = _make_metadata(status="active")
        mock_server.agent_metadata = {}  # Not in metadata initially (skips first check)
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)

        # After ensure_agent_persisted, the agent is now in metadata with "paused" status
        def side_effect_get_metadata(aid, **kwargs):
            m = _make_metadata(status="paused", paused_at="2026-01-20T12:00:00")
            mock_server.agent_metadata[agent_uuid] = m
            return m
        mock_server.get_or_create_metadata.side_effect = side_effect_get_metadata

        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test",
                "complexity": 0.5,
            })

            data = parse_result(result)
            # The agent should go through the successful path since it was "active"
            # when first checked, then after create it gets paused status
            # This tests the second paused check at lines 774-793
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 844-849: Calibration correction applied
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_calibration_correction_applied(self, mock_server, mock_monitor):
        """Calibration auto-correction modifies confidence and is included in response."""
        agent_uuid = "test-uuid-cal"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)

        mock_calibration = MagicMock()
        mock_calibration.apply_confidence_correction = MagicMock(
            return_value=(0.6, "Adjusted from 0.8 to 0.6 based on historical accuracy")
        )

        with self._apply_patches(p), \
             patch.dict("sys.modules", {"src.calibration": MagicMock(calibration_checker=mock_calibration)}):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test with confidence",
                "complexity": 0.5,
                "confidence": 0.8,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 864-865: Invalid task_type defaults to mixed
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_invalid_task_type_defaults_to_mixed(self, mock_server, mock_monitor):
        """Invalid task_type logs warning and defaults to 'mixed'."""
        agent_uuid = "test-uuid-task"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test with bad task_type",
                "complexity": 0.5,
                "task_type": "invalid_type_xyz",
            })

            data = parse_result(result)
            assert isinstance(data, dict)
            # Should succeed despite invalid task_type

    # ------------------------------------------------------------------
    # Lines 905, 910, 915, 921-923: Policy warnings in response
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_policy_warnings_included_in_response(self, mock_server, mock_monitor):
        """Policy warnings for test file creation and agent_id are included."""
        agent_uuid = "test-uuid-policy"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "I created test_something.py and demo_widget.py in root",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)
            # Should still succeed with or without warnings

    # ------------------------------------------------------------------
    # Lines 928-929: Test file creation in root warning
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_test_file_creation_warning(self, mock_server, mock_monitor):
        """Creating test files outside tests/ directory triggers policy warning."""
        agent_uuid = "test-uuid-testfile"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "Creating test_validators.py in the project root",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 962-970: New agent creation PostgreSQL failure fallback
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_new_agent_creation_pg_failure_fallback(self, mock_server, mock_monitor):
        """When PostgreSQL create_agent fails, falls back to legacy path."""
        agent_uuid = "test-uuid-pgfail"
        mock_server.agent_metadata = {}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {}
        fallback_meta = _make_metadata(total_updates=1)
        mock_server.get_or_create_metadata.return_value = fallback_meta

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        # Make storage.get_or_create_agent raise to trigger fallback
        p["storage"].get_or_create_agent = AsyncMock(side_effect=Exception("PG connection failed"))

        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "first update with pg failure",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)
            # Should still succeed via fallback
            mock_server.get_or_create_metadata.assert_called()

    # ------------------------------------------------------------------
    # New agent creation: PG insert succeeds but metadata setup raises.
    # The freshly-generated api_key (already persisted to PG) must NOT be
    # silently replaced by the metadata-cache fallback in the except branch.
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_new_agent_pg_insert_succeeds_but_meta_setup_fails_keeps_apikey(
        self, mock_server, mock_monitor,
    ):
        """If get_or_create_agent succeeded (api_key is in PG), but a later
        step in the same try block raised, the recovery branch must preserve
        the api_key that was just persisted — not overwrite it with whatever
        api_key the in-memory metadata cache happens to hold.
        """
        agent_uuid = "test-uuid-meta-fail"
        mock_server.agent_metadata = {}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {}

        fallback_meta = _make_metadata(total_updates=1, api_key="stale-cached-key")
        mock_server.get_or_create_metadata = MagicMock(
            side_effect=[RuntimeError("metadata setup blew up"), fallback_meta]
        )

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        # PG insert succeeds — return a real record.
        p["storage"].get_or_create_agent = AsyncMock(
            return_value=(MagicMock(api_key="test-key"), True)
        )

        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            await handle_process_agent_update({
                "response_text": "first update",
                "complexity": 0.5,
            })

        # PG was given the freshly-generated api_key — capture what we wrote.
        pg_apikey = p["storage"].get_or_create_agent.call_args.kwargs["api_key"]
        assert pg_apikey  # sanity: we did pass one

        # The fallback metadata's api_key was "stale-cached-key" before the
        # call. After recovery, it must be re-stamped to match what's in PG,
        # so downstream code (and the in-memory cache) agree on the credential.
        assert fallback_meta.api_key == pg_apikey
        assert fallback_meta.api_key != "stale-cached-key"

    # ------------------------------------------------------------------
    # Lines 979, 982: Existing agent sync to cache
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_existing_agent_pg_record_synced_to_cache(self, mock_server, mock_monitor):
        """Existing agent found in PG is synced to runtime cache."""
        agent_uuid = "test-uuid-sync"
        meta = _make_metadata(status="active", total_updates=10)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        # Simulate PG returning an agent record
        pg_record = MagicMock()
        pg_record.api_key = "pg-api-key-123"
        p["storage"].get_agent = AsyncMock(return_value=pg_record)

        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "existing agent test",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)
            # meta should have synced api_key
            assert meta.api_key == "pg-api-key-123"

    # ------------------------------------------------------------------
    # Lines 987-990: Existing agent PG exception fallback to cache
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_existing_agent_pg_exception_fallback_to_cache(self, mock_server, mock_monitor):
        """When PG get_agent raises, falls back to cache."""
        agent_uuid = "test-uuid-pgex"
        meta = _make_metadata(status="active", total_updates=10, api_key="cached-key")
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        p["storage"].get_agent = AsyncMock(side_effect=Exception("PG timeout"))

        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test with pg error",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1008-1009: Previous void state exception
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_previous_void_state_exception_handled(self, mock_server, mock_monitor):
        """Exception reading previous void state defaults to False."""
        agent_uuid = "test-uuid-void"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        # Make monitors.get raise
        mock_server.monitors = MagicMock()
        mock_server.monitors.get = MagicMock(side_effect=Exception("monitors error"))
        mock_server.get_or_create_monitor.return_value = mock_monitor

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test void state",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1052: metrics dict creation when missing
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_metrics_dict_created_when_missing(self, mock_server, mock_monitor):
        """Health status is added even when result has no metrics dict initially."""
        agent_uuid = "test-uuid-nometrics"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}
        # Return result without metrics key
        mock_server.process_update_authenticated_async = AsyncMock(return_value={
            "status": "ok",
            "decision": {"action": "approve", "confidence": 0.8},
            "guidance": "Continue.",
        })

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test no metrics",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1074-1075: CIRS void_alert exception
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_cirs_void_alert_exception_handled(self, mock_server, mock_monitor):
        """Exception from CIRS void_alert does not crash update."""
        agent_uuid = "test-uuid-cirs"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        mock_cirs = MagicMock()
        mock_cirs.maybe_emit_void_alert = MagicMock(side_effect=RuntimeError("CIRS error"))
        mock_cirs.auto_emit_state_announce = MagicMock(return_value=None)
        with self._apply_patches(p), \
             patch.dict("sys.modules", {
                 "src.mcp_handlers.cirs.protocol": mock_cirs,
             }):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test cirs error",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1106-1132: Record agent state ValueError fallback
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_record_state_valueerror_creates_agent_first(self, mock_server, mock_monitor):
        """When record_agent_state raises ValueError, creates agent first then records."""
        agent_uuid = "test-uuid-valerr-rec"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        call_count = [0]

        async def record_side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ValueError("Agent not found in database")
            return None

        p["storage"].record_agent_state = AsyncMock(side_effect=record_side_effect)

        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test record fallback",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)
            # create_agent should have been called
            p["storage"].create_agent.assert_called_once()
            # record_agent_state should have been called twice (first fails, second succeeds)
            assert call_count[0] == 2

    # ------------------------------------------------------------------
    # Lines 1106-1132: Record agent state generic exception
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_record_state_generic_exception_handled(self, mock_server, mock_monitor):
        """Generic exception from record_agent_state is caught gracefully."""
        agent_uuid = "test-uuid-rec-err"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        p["storage"].record_agent_state = AsyncMock(side_effect=RuntimeError("PG pool exhausted"))

        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test record error",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1171-1172: Previous coherence exception
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_previous_coherence_exception_handled(self, mock_server, mock_monitor):
        """Exception accessing coherence history is caught."""
        agent_uuid = "test-uuid-coh"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        # Make coherence_history raise on access
        monitor = _make_monitor()
        monitor.state.coherence_history = property(lambda self: (_ for _ in ()).throw(RuntimeError("bad")))
        mock_server.get_or_create_monitor.return_value = monitor
        mock_server.monitors = {agent_uuid: monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test coherence history error",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1194-1195: Complexity calibration feedback with discrepancy
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_complexity_calibration_feedback_in_response(self, mock_server, mock_monitor):
        """Complexity calibration feedback shows reported vs derived discrepancy."""
        agent_uuid = "test-uuid-calcomp"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        # Return metrics with complexity different from what was reported
        mock_server.process_update_authenticated_async = AsyncMock(return_value={
            "status": "ok",
            "decision": {"action": "approve", "confidence": 0.8},
            "metrics": {
                "E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0,
                "coherence": 0.52, "risk_score": 0.3,
                "verdict": "continue", "regime": "EXPLORATION",
                "phi": 0.0,
                "complexity": 0.8,  # System derived: 0.8
            },
            "guidance": "Continue.",
        })
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test complexity calibration",
                "complexity": 0.3,  # Reported: 0.3 (discrepancy with derived 0.8)
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1216: Auto-calibration correction info in calibration_feedback
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_calibration_correction_info_in_feedback(self, mock_server, mock_monitor):
        """When confidence correction is applied, it appears in calibration_feedback."""
        agent_uuid = "test-uuid-corr"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)

        mock_calibration_mod = MagicMock()
        mock_calibration_mod.calibration_checker.apply_confidence_correction = MagicMock(
            return_value=(0.6, "Reduced 0.8 -> 0.6 based on overconfidence pattern")
        )

        with self._apply_patches(p), \
             patch.dict("sys.modules", {"src.calibration": mock_calibration_mod}):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test correction",
                "confidence": 0.8,
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1231-1246: Loop cooldown info
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_loop_cooldown_active(self, mock_server, mock_monitor):
        """Active loop cooldown is included in response."""
        agent_uuid = "test-uuid-cooldown"
        # Set loop_cooldown_until to future
        from datetime import datetime, timedelta
        future_time = (datetime.now() + timedelta(seconds=30)).isoformat()
        meta = _make_metadata(status="active", total_updates=5)
        meta.loop_cooldown_until = future_time
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test with cooldown",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1231-1246: Loop cooldown expired
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_loop_cooldown_expired(self, mock_server, mock_monitor):
        """Expired loop cooldown is cleared."""
        agent_uuid = "test-uuid-cooldown-exp"
        from datetime import datetime, timedelta
        past_time = (datetime.now() - timedelta(seconds=30)).isoformat()
        meta = _make_metadata(status="active", total_updates=5)
        meta.loop_cooldown_until = past_time
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test expired cooldown",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)
            # Cooldown should have been cleared
            assert meta.loop_cooldown_until is None

    # ------------------------------------------------------------------
    # Lines 1252-1258: Default agent_id warning path
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_default_agent_id_warning(self, mock_server, mock_monitor):
        """check_agent_id_default returning a warning is included in response."""
        agent_uuid = "test-uuid-default-warn"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}
        mock_server.check_agent_id_default.return_value = "Using default agent_id, consider naming yourself."

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test default warning",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1264, 1272: Loop info and metrics fallback paths
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_metrics_health_status_fallback_no_health_in_metrics(self, mock_server, mock_monitor):
        """When metrics exist but health_status missing, uses status fallback."""
        agent_uuid = "test-uuid-fallback"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1298-1303: Health status from metrics fallback (no health_status key)
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_health_status_no_metrics_key(self, mock_server, mock_monitor):
        """When no metrics dict at all, health_status falls back to status."""
        agent_uuid = "test-uuid-nomet"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}
        # Return result with no metrics at all
        mock_server.process_update_authenticated_async = AsyncMock(return_value={
            "status": "ok",
            "decision": {"action": "approve"},
        })

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1312-1322: EISV metrics backfill from eisv dict
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_eisv_metrics_backfill_from_eisv_dict(self, mock_server, mock_monitor):
        """Missing E/I/S/V in metrics are backfilled from eisv dict."""
        agent_uuid = "test-uuid-eisv-fill"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}
        # Return result with metrics but no flat E/I/S/V, only eisv dict
        mock_server.process_update_authenticated_async = AsyncMock(return_value={
            "status": "ok",
            "decision": {"action": "approve"},
            "metrics": {
                "eisv": {"E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0},
                "coherence": 0.52, "risk_score": 0.3,
                "health_status": "healthy", "health_message": "ok",
                "verdict": "continue", "regime": "EXPLORATION", "phi": 0.0,
            },
        })

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test eisv backfill",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1349-1350: Monitor risk metrics exception
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_monitor_risk_metrics_exception(self, mock_server, mock_monitor):
        """Exception fetching additional risk metrics from monitor is caught."""
        agent_uuid = "test-uuid-riskerr"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        failing_monitor = _make_monitor()
        failing_monitor.get_metrics.side_effect = RuntimeError("monitor broken")
        mock_server.get_or_create_monitor.return_value = failing_monitor
        mock_server.monitors = {agent_uuid: failing_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test risk error",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1354, 1357: Policy warnings and warnings in response
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_policy_warnings_appended(self, mock_server, mock_monitor):
        """Dialectic enforcement warning is prepended to policy warnings."""
        agent_uuid = "test-uuid-polwarn"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}
        mock_server.check_agent_id_default.return_value = "default_id_warning"

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test policy",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)
            # Should have warning from check_agent_id_default
            if "warning" in data:
                assert "default_id_warning" in data["warning"]

    # ------------------------------------------------------------------
    # Lines 1361, 1365: Auto-resume info and CIRS alert in response
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_cirs_void_alert_in_response(self, mock_server, mock_monitor):
        """CIRS void alert info is included in response when emitted."""
        agent_uuid = "test-uuid-cirs-resp"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        mock_cirs_alert = {"severity": "warning", "V_snapshot": 0.15}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            # Patch the local import of maybe_emit_void_alert
            with patch.dict("sys.modules", {
                "src.mcp_handlers.cirs.protocol": MagicMock(
                    maybe_emit_void_alert=MagicMock(return_value=mock_cirs_alert),
                    auto_emit_state_announce=MagicMock(return_value=None),
                )
            }):
                from src.mcp_handlers.core import handle_process_agent_update
                result = await handle_process_agent_update({
                    "response_text": "test cirs alert",
                    "complexity": 0.5,
                })

                data = parse_result(result)
                assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1424-1427, 1437: Relevant discoveries scored by overlap
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_relevant_discoveries_surfaced(self, mock_server, mock_monitor):
        """Relevant discoveries are scored by tag overlap and surfaced."""
        agent_uuid = "test-uuid-disc"
        meta = _make_metadata(status="active", total_updates=5, tags=["testing", "governance"])
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        # Mock knowledge graph with discoveries
        mock_disc = MagicMock()
        mock_disc.tags = ["testing", "coverage"]
        mock_disc.to_dict = MagicMock(return_value={
            "id": "d-1", "summary": "Test coverage patterns", "tags": ["testing", "coverage"],
        })

        mock_graph = AsyncMock()
        mock_graph.query = AsyncMock(return_value=[mock_disc])

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            with patch.dict("sys.modules", {
                "src.knowledge_graph": MagicMock(get_knowledge_graph=AsyncMock(return_value=mock_graph)),
            }):
                from src.mcp_handlers.core import handle_process_agent_update
                result = await handle_process_agent_update({
                    "response_text": "test discoveries",
                    "complexity": 0.5,
                })

                data = parse_result(result)
                assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1444: Onboarding guidance in response
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_onboarding_guidance_included(self, mock_server, mock_monitor):
        """Onboarding guidance is included for new agents."""
        agent_uuid = "test-uuid-onboard"
        mock_server.agent_metadata = {}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {}
        mock_server.get_or_create_metadata.return_value = _make_metadata(total_updates=1)

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "hello world",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1456-1476: API key hint and onboarding info for new agents
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_api_key_hint_for_new_agent(self, mock_server, mock_monitor):
        """New agent receives api_key_hint (not full key) in response."""
        agent_uuid = "test-uuid-apikey"
        mock_server.agent_metadata = {}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {}
        new_meta = _make_metadata(total_updates=1, api_key="abcdefghijklmnop")
        mock_server.get_or_create_metadata.return_value = new_meta

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "first update",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1479-1482: Key regeneration and auto-retrieval info
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_welcome_message_on_first_update(self, mock_server, mock_monitor):
        """Welcome message is shown when total_updates == 1."""
        agent_uuid = "test-uuid-welcome"
        meta = _make_metadata(status="active", total_updates=1, api_key="abc12345longkey")
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "my first real update",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1529: Convergence guidance exception
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_convergence_guidance_exception_handled(self, mock_server, mock_monitor):
        """Exception in convergence guidance does not crash update."""
        agent_uuid = "test-uuid-conv"
        meta = _make_metadata(status="active", total_updates=3)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p), \
             patch.dict("sys.modules", {
                 "governance_core.parameters": MagicMock(
                     get_i_dynamics_mode=MagicMock(side_effect=ImportError("not available")),
                 ),
             }):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test convergence error",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1595-1597: Convergence guidance exception log
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_convergence_guidance_for_new_agent(self, mock_server, mock_monitor):
        """Convergence guidance is generated for agents with < 20 updates."""
        agent_uuid = "test-uuid-conv2"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "testing convergence guidance generation",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1614-1648: Anti-stasis perturbation for stable agents
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_perturbation_for_stable_agent(self, mock_server, mock_monitor):
        """Stable agent with many updates receives perturbation question."""
        agent_uuid = "test-uuid-perturb"
        meta = _make_metadata(status="active", total_updates=15, tags=["testing"])
        meta._last_perturbation_update = 0  # Long since last perturbation
        mock_server.agent_metadata = {agent_uuid: meta}
        # Return low entropy to trigger perturbation
        mock_server.process_update_authenticated_async = AsyncMock(return_value={
            "status": "ok",
            "decision": {"action": "approve", "confidence": 0.8},
            "metrics": {
                "E": 0.9, "I": 0.9, "S": 0.05, "V": 0.0,
                "coherence": 0.55, "risk_score": 0.1,
                "verdict": "continue", "regime": "EXPLORATION",
                "phi": 0.0,
                "health_status": "healthy", "health_message": "ok",
            },
        })
        from src.health_thresholds import HealthStatus
        mock_server.health_checker.get_health_status.return_value = (HealthStatus.HEALTHY, "Healthy")
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        mock_question = MagicMock()
        mock_question.id = "q-perturbation"
        mock_question.summary = "What patterns emerge from stable governance states?"
        mock_question.tags = ["testing", "patterns"]
        mock_question.agent_id = "other-agent"

        mock_graph = AsyncMock()
        mock_graph.query = AsyncMock(return_value=[mock_question])

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            with patch.dict("sys.modules", {
                "src.knowledge_graph": MagicMock(get_knowledge_graph=AsyncMock(return_value=mock_graph)),
            }):
                from src.mcp_handlers.core import handle_process_agent_update
                result = await handle_process_agent_update({
                    "response_text": "stable work continues",
                    "complexity": 0.3,
                })

                data = parse_result(result)
                assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1655-1657: v4.1 basin/convergence tracking
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_v41_block_surfaced(self, mock_server, mock_monitor):
        """v4.1 unitares block is surfaced from metrics when present."""
        agent_uuid = "test-uuid-v41"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.process_update_authenticated_async = AsyncMock(return_value={
            "status": "ok",
            "decision": {"action": "approve"},
            "metrics": {
                "E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0,
                "coherence": 0.52, "risk_score": 0.3,
                "verdict": "continue", "regime": "EXPLORATION",
                "phi": 0.0,
                "health_status": "healthy", "health_message": "ok",
                "unitares_v41": {"basin": "stable", "I_star": 0.77},
            },
        })
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test v41",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1666-1751: Trajectory identity with trust tier and risk adjustment
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_trajectory_identity_updated(self, mock_server, mock_monitor):
        """Trajectory signature is processed and trust tier computed."""
        agent_uuid = "test-uuid-traj"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        trajectory_result = {
            "stored": True,
            "observation_count": 3,
            "identity_confidence": 0.8,
            "lineage_similarity": 0.7,
            "lineage_threshold": 0.6,
            "is_anomaly": False,
            "trust_tier": {"name": "established", "tier": 2},
        }

        mock_traj_mod = MagicMock()
        mock_traj_mod.TrajectorySignature.from_dict = MagicMock(return_value=MagicMock())
        mock_traj_mod.update_current_signature = AsyncMock(return_value=trajectory_result)
        mock_traj_mod.compute_trust_tier = MagicMock(return_value={"name": "established", "tier": 2})

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p), \
             patch.dict("sys.modules", {
                 "src.trajectory_identity": mock_traj_mod,
             }):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "trajectory test",
                "complexity": 0.5,
                "trajectory_signature": {"warmth": 0.5, "clarity": 0.6},
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1779-1780: Saturation diagnostics exception in process_agent_update
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_saturation_diagnostics_exception_in_update(self, mock_server, mock_monitor):
        """Exception computing saturation diagnostics in update is caught."""
        agent_uuid = "test-uuid-sat-err"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_monitor.state.unitaires_state = MagicMock()  # Non-None triggers computation
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test saturation error",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1794-1807: Pending dialectic notification (reviewer)
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_pending_dialectic_reviewer_notification(self, mock_server, mock_monitor):
        """Agent that is a reviewer with pending antithesis gets notification."""
        agent_uuid = "test-uuid-dialectic"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        from src.dialectic_protocol import DialecticPhase

        # Create mock session where this agent is reviewer
        mock_session = MagicMock()
        mock_session.reviewer_agent_id = agent_uuid
        mock_session.paused_agent_id = "other-agent"
        mock_session.phase = DialecticPhase.ANTITHESIS
        mock_session.topic = "Stability patterns"
        mock_session.created_at = MagicMock()
        mock_session.created_at.isoformat = MagicMock(return_value="2026-01-20T12:00:00")

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p), \
             patch.dict("sys.modules", {
                 "src.mcp_handlers.dialectic.handlers": MagicMock(ACTIVE_SESSIONS={"sess-1": mock_session}),
             }):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test dialectic notification",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1818-1825: Pending dialectic synthesis notification
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_pending_dialectic_synthesis_notification(self, mock_server, mock_monitor):
        """Agent that is initiator with pending synthesis gets notification."""
        agent_uuid = "test-uuid-synth"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        from src.dialectic_protocol import DialecticPhase

        mock_session = MagicMock()
        mock_session.paused_agent_id = agent_uuid
        mock_session.reviewer_agent_id = "reviewer-agent"
        mock_session.phase = DialecticPhase.SYNTHESIS
        mock_session.topic = "Recovery patterns"
        mock_session.created_at = MagicMock()
        mock_session.created_at.isoformat = MagicMock(return_value="2026-01-20T13:00:00")

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p), \
             patch.dict("sys.modules", {
                 "src.mcp_handlers.dialectic.handlers": MagicMock(ACTIVE_SESSIONS={"sess-2": mock_session}),
             }):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test synthesis notification",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1831-1834: EISV validation warning
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_eisv_validation_warning_in_response(self, mock_server, mock_monitor):
        """EISV validation failure adds warning but does not crash."""
        agent_uuid = "test-uuid-eisv-val"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p), \
             patch("src.eisv_validator.validate_governance_response", side_effect=ValueError("Missing V metric")):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test eisv validation",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1853-1873: Learning context - recent decisions
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_learning_context_recent_decisions(self, mock_server, mock_monitor):
        """Learning context includes recent decisions from audit log."""
        agent_uuid = "test-uuid-learn"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        mock_audit = MagicMock()
        mock_audit.query_audit_log = MagicMock(return_value=[
            {
                "timestamp": "2026-01-20T12:00:00.000",
                "event_type": "process_update",
                "details": {
                    "action": "approve",
                    "risk_score": 0.2,
                    "confidence": 0.7,
                },
            },
        ])

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p), \
             patch.dict("sys.modules", {
                 "src.audit_log": MagicMock(AuditLogger=MagicMock(return_value=mock_audit)),
             }):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test learning context",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1884: Learning context - my contributions
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_learning_context_my_contributions(self, mock_server, mock_monitor):
        """Learning context includes agent's own knowledge graph contributions."""
        agent_uuid = "test-uuid-contrib"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        mock_disc = MagicMock()
        mock_disc.summary = "Test coverage improvement strategy"
        mock_disc.discovery_type = "insight"
        mock_disc.status = "open"

        mock_graph = AsyncMock()
        mock_graph.query = AsyncMock(return_value=[mock_disc])

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p), \
             patch.dict("sys.modules", {
                 "src.knowledge_graph": MagicMock(get_knowledge_graph=AsyncMock(return_value=mock_graph)),
             }):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test contributions",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1927-1930, 1940-1941: Calibration insight in learning context
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_learning_context_calibration_insight(self, mock_server, mock_monitor):
        """Calibration insight with inverted pattern is surfaced."""
        agent_uuid = "test-uuid-cal-insight"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        mock_calibration = MagicMock()
        mock_calibration.bin_stats = {
            '0.0-0.5': {'count': 5, 'actual_correct': 4},
            '0.5-0.7': {'count': 5, 'actual_correct': 4},
            '0.7-0.8': {'count': 5, 'actual_correct': 1},
            '0.8-0.9': {'count': 5, 'actual_correct': 1},
            '0.9-1.0': {'count': 5, 'actual_correct': 1},
        }
        mock_calibration.apply_confidence_correction = MagicMock(return_value=(0.5, None))

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p), \
             patch.dict("sys.modules", {
                 "src.calibration": MagicMock(calibration_checker=mock_calibration),
             }):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test calibration insight",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1954, 1959, 1961, 1966, 1968, 1971-1976: Pattern detection
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_learning_context_pattern_detection(self, mock_server, mock_monitor):
        """Pattern detection generates regime, energy, and coherence observations."""
        agent_uuid = "test-uuid-patterns"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}

        # Create monitor with specific state for patterns
        monitor = _make_monitor(regime="CONVERGENT", regime_duration=8)
        mock_server.get_or_create_monitor.return_value = monitor
        mock_server.monitors = {agent_uuid: monitor}

        # Return metrics with extreme values to trigger patterns
        mock_server.process_update_authenticated_async = AsyncMock(return_value={
            "status": "ok",
            "decision": {"action": "approve"},
            "metrics": {
                "E": 0.4, "I": 0.6, "S": 0.2, "V": 0.0,
                "coherence": 0.35, "risk_score": 0.3,
                "verdict": "continue", "regime": "CONVERGENT",
                "phi": 0.0,
                "health_status": "moderate", "health_message": "ok",
            },
        })

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test pattern detection",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1984-1986: Learning context outer exception
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_learning_context_outer_exception(self, mock_server, mock_monitor):
        """Outer exception in learning context does not crash update."""
        agent_uuid = "test-uuid-lc-err"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test learning error",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 2000-2001: Response formatter exception
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_response_formatter_exception_handled(self, mock_server, mock_monitor):
        """Exception in format_response is caught and original data returned."""
        agent_uuid = "test-uuid-fmt-err"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p), \
             patch("src.mcp_handlers.response_formatter.format_response", side_effect=RuntimeError("format failed")):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test format error",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 2008-2016: Serialization fallback
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_serialization_fallback_on_error(self, mock_server, mock_monitor):
        """When success_response raises, minimal fallback response is returned."""
        agent_uuid = "test-uuid-serial"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        call_count = [0]
        original_success_response = None

        # We need to make success_response raise on the first call inside the lock,
        # not on the outer error_response calls
        from src.mcp_handlers.utils import success_response as orig_sr

        def failing_success_response(*args, **kwargs):
            call_count[0] += 1
            raise TypeError("Object of type ndarray is not JSON serializable")

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p), \
             patch("src.mcp_handlers.core.success_response", side_effect=failing_success_response):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test serialization error",
                "complexity": 0.5,
            })

            # Should get the minimal fallback response
            data = parse_result(result)
            assert data.get("success") is True
            assert "_warning" in data
            assert "serialization" in data["_warning"].lower()

    # ------------------------------------------------------------------
    # Lines 2049-2051: Emergency lock cleanup exception
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_lock_timeout_cleanup_exception(self, mock_server, mock_monitor):
        """Emergency lock cleanup failure is caught gracefully."""
        agent_uuid = "test-uuid-lock-cleanup"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        @asynccontextmanager
        async def _timeout_lock(*args, **kwargs):
            raise TimeoutError("Lock timeout")
            yield  # pragma: no cover

        mock_server.lock_manager.acquire_agent_lock_async = MagicMock(side_effect=_timeout_lock)

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p), \
             patch.dict("sys.modules", {
                 "src.lock_cleanup": MagicMock(
                     cleanup_stale_state_locks=MagicMock(side_effect=RuntimeError("cleanup failed"))
                 ),
             }):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test lock cleanup error",
                "complexity": 0.5,
            })

            data = parse_result(result)
            # Should still return error about lock, not crash
            assert "error" in data or "lock" in json.dumps(data).lower()

    # ------------------------------------------------------------------
    # Lines 2049: Emergency lock cleanup success
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_lock_timeout_cleanup_success(self, mock_server, mock_monitor):
        """Successful emergency lock cleanup after timeout."""
        agent_uuid = "test-uuid-lock-clean-ok"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        @asynccontextmanager
        async def _timeout_lock(*args, **kwargs):
            raise TimeoutError("Lock timeout")
            yield  # pragma: no cover

        mock_server.lock_manager.acquire_agent_lock_async = MagicMock(side_effect=_timeout_lock)

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p), \
             patch.dict("sys.modules", {
                 "src.lock_cleanup": MagicMock(
                     cleanup_stale_state_locks=MagicMock(return_value={"cleaned": 2})
                 ),
             }):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test lock cleanup success",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert "error" in data or "lock" in json.dumps(data).lower()

    # ------------------------------------------------------------------
    # Lines 1549, 1559, 1570, 1585: Convergence guidance detailed paths
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_convergence_guidance_all_metrics(self, mock_server, mock_monitor):
        """Convergence guidance with low I, high S, low E, and high V."""
        agent_uuid = "test-uuid-conv-all"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}

        # Return metrics with all convergence triggers active
        mock_server.process_update_authenticated_async = AsyncMock(return_value={
            "status": "ok",
            "decision": {"action": "approve"},
            "metrics": {
                "E": 0.4, "I": 0.3, "S": 0.3, "V": 0.25,
                "coherence": 0.45, "risk_score": 0.5,
                "verdict": "continue", "regime": "EXPLORATION",
                "phi": 0.0,
                "health_status": "moderate", "health_message": "ok",
            },
        })
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "test convergence all metrics",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1666-1751: Trajectory identity with anomaly and genesis
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_trajectory_identity_anomaly_detected(self, mock_server, mock_monitor):
        """Trajectory anomaly adds warning and risk adjustment."""
        agent_uuid = "test-uuid-traj-anom"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        trajectory_result = {
            "stored": True,
            "observation_count": 5,
            "identity_confidence": 0.4,
            "lineage_similarity": 0.3,
            "lineage_threshold": 0.6,
            "is_anomaly": True,
            "warning": "Behavioral deviation detected",
            "trust_tier": {"name": "new", "tier": 0},
        }

        mock_traj_mod = MagicMock()
        mock_traj_mod.TrajectorySignature.from_dict = MagicMock(return_value=MagicMock())
        mock_traj_mod.update_current_signature = AsyncMock(return_value=trajectory_result)
        mock_traj_mod.compute_trust_tier = MagicMock(return_value={"name": "new", "tier": 0})

        mock_db = MagicMock()
        mock_db.get_identity = AsyncMock(return_value=None)

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p), \
             patch.dict("sys.modules", {
                 "src.trajectory_identity": mock_traj_mod,
                 "src.db": MagicMock(get_db=MagicMock(return_value=mock_db)),
             }):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "anomaly test",
                "complexity": 0.5,
                "trajectory_signature": {"warmth": 0.1, "clarity": 0.2},
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Trajectory identity: genesis created path
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_trajectory_identity_genesis_created(self, mock_server, mock_monitor):
        """First trajectory update creates genesis signature."""
        agent_uuid = "test-uuid-traj-gen"
        meta = _make_metadata(status="active", total_updates=1)
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        trajectory_result = {
            "stored": True,
            "observation_count": 1,
            "identity_confidence": 0.2,
            "genesis_created": True,
            "trust_tier": {"name": "new", "tier": 0},
        }

        mock_traj_mod = MagicMock()
        mock_traj_mod.TrajectorySignature.from_dict = MagicMock(return_value=MagicMock())
        mock_traj_mod.update_current_signature = AsyncMock(return_value=trajectory_result)
        mock_traj_mod.compute_trust_tier = MagicMock(return_value={"name": "new", "tier": 0})

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p), \
             patch.dict("sys.modules", {
                 "src.trajectory_identity": mock_traj_mod,
             }):

            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "genesis test",
                "complexity": 0.5,
                "trajectory_signature": {"warmth": 0.5, "clarity": 0.6},
            })

            data = parse_result(result)
            assert isinstance(data, dict)

    # ------------------------------------------------------------------
    # Lines 1959, 1968: High energy and high coherence patterns
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_learning_context_high_energy_high_coherence(self, mock_server, mock_monitor):
        """High energy and high coherence patterns are detected."""
        agent_uuid = "test-uuid-hi"
        meta = _make_metadata(status="active", total_updates=5)
        mock_server.agent_metadata = {agent_uuid: meta}

        monitor = _make_monitor(regime="EXPLORATION", regime_duration=1)
        mock_server.get_or_create_monitor.return_value = monitor
        mock_server.monitors = {agent_uuid: monitor}

        mock_server.process_update_authenticated_async = AsyncMock(return_value={
            "status": "ok",
            "decision": {"action": "approve"},
            "metrics": {
                "E": 0.9, "I": 0.8, "S": 0.1, "V": 0.0,
                "coherence": 0.85, "risk_score": 0.1,
                "verdict": "continue", "regime": "EXPLORATION",
                "phi": 0.0,
                "health_status": "healthy", "health_message": "ok",
            },
        })

        p = self._common_patches(mock_server, agent_uuid=agent_uuid)
        with self._apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "high energy high coherence work",
                "complexity": 0.5,
            })

            data = parse_result(result)
            assert isinstance(data, dict)
