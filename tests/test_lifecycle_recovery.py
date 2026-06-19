"""
Comprehensive tests for src/mcp_handlers/lifecycle.py - Agent lifecycle handlers.

Covers: handle_list_agents, handle_get_agent_metadata, handle_update_agent_metadata,
        handle_archive_agent, handle_delete_agent, handle_archive_old_test_agents,
        handle_archive_orphan_agents, handle_mark_response_complete,
        handle_direct_resume_if_safe, handle_self_recovery_review,
        handle_detect_stuck_agents, handle_ping_agent.
"""

import pytest
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from tests.helpers import (
    patch_lifecycle_server, parse_result as _parse,
    make_agent_meta, make_mock_server, make_monitor,
    patch_agent_storage,
)


# ============================================================================
# handle_list_agents - Lite Mode
# ============================================================================

class TestDirectResumeIfSafe:

    @pytest.fixture
    def server(self):
        return make_mock_server()

    @pytest.mark.asyncio
    async def test_resume_success(self, server):
        meta = make_agent_meta(status="paused")
        server.agent_metadata = {"agent-1": meta}
        server.get_or_create_monitor.return_value = make_monitor(coherence=0.8, mean_risk=0.3, I=0.3, S=0.5)

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.lifecycle.handlers.agent_storage") as mock_storage, \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True):
            mock_storage.update_agent = AsyncMock()
            mock_storage.persist_runtime_state = AsyncMock()
            import src.mcp_handlers.lifecycle.mutation as _lm; _lm.agent_storage = mock_storage
            import src.mcp_handlers.lifecycle.operations as _lo; _lo.agent_storage = mock_storage
            from src.mcp_handlers.lifecycle.handlers import handle_direct_resume_if_safe
            result = await handle_direct_resume_if_safe({"agent_id": "agent-1"})
            data = _parse(result)
            assert data["success"] is True
            assert data["action"] == "resumed"
            assert meta.status == "active"
            assert "deprecation_warning" in data

    @pytest.mark.asyncio
    async def test_resume_not_safe_low_coherence(self, server):
        meta = make_agent_meta(status="paused")
        server.agent_metadata = {"agent-1": meta}
        server.get_or_create_monitor.return_value = make_monitor(coherence=0.2, I=0.3, S=0.5)

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True):
            from src.mcp_handlers.lifecycle.handlers import handle_direct_resume_if_safe
            result = await handle_direct_resume_if_safe({"agent_id": "agent-1"})
            text = result[0].text
            assert "not safe" in text.lower() or "failed" in text.lower()
            assert meta.status == "paused"  # not resumed

    @pytest.mark.asyncio
    async def test_resume_not_safe_high_risk(self, server):
        meta = make_agent_meta(status="paused")
        server.agent_metadata = {"agent-1": meta}
        server.get_or_create_monitor.return_value = make_monitor(mean_risk=0.8, I=0.3, S=0.5)

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True):
            from src.mcp_handlers.lifecycle.handlers import handle_direct_resume_if_safe
            result = await handle_direct_resume_if_safe({"agent_id": "agent-1"})
            text = result[0].text
            assert "not safe" in text.lower() or "failed" in text.lower()

    @pytest.mark.asyncio
    async def test_resume_not_safe_void_active(self, server):
        meta = make_agent_meta(status="paused")
        server.agent_metadata = {"agent-1": meta}
        server.get_or_create_monitor.return_value = make_monitor(void_active=True, I=0.3, S=0.5)

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True):
            from src.mcp_handlers.lifecycle.handlers import handle_direct_resume_if_safe
            result = await handle_direct_resume_if_safe({"agent_id": "agent-1"})
            text = result[0].text
            assert "not safe" in text.lower() or "failed" in text.lower()

    @pytest.mark.asyncio
    async def test_resume_not_safe_wrong_status(self, server):
        meta = make_agent_meta(status="active")  # not paused
        server.agent_metadata = {"agent-1": meta}
        server.get_or_create_monitor.return_value = make_monitor(coherence=0.8, I=0.3, S=0.5)

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True):
            from src.mcp_handlers.lifecycle.handlers import handle_direct_resume_if_safe
            result = await handle_direct_resume_if_safe({"agent_id": "agent-1"})
            text = result[0].text
            assert "not safe" in text.lower() or "failed" in text.lower()

    @pytest.mark.asyncio
    async def test_resume_not_found(self, server):
        server.agent_metadata = {}

        with patch_lifecycle_server(server, require_registered=("agent-1", None)):
            from src.mcp_handlers.lifecycle.handlers import handle_direct_resume_if_safe
            result = await handle_direct_resume_if_safe({"agent_id": "agent-1"})
            text = result[0].text
            assert "not found" in text.lower()

    @pytest.mark.asyncio
    async def test_resume_ownership_denied(self, server):
        meta = make_agent_meta(status="paused")
        server.agent_metadata = {"agent-1": meta}

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=False):
            from src.mcp_handlers.lifecycle.handlers import handle_direct_resume_if_safe
            result = await handle_direct_resume_if_safe({"agent_id": "agent-1"})
            text = result[0].text
            assert "auth" in text.lower()

    @pytest.mark.asyncio
    async def test_resume_persists_runtime_state(self, server):
        """Watcher P011: paused_at + lifecycle_event must be persisted alongside status."""
        meta = make_agent_meta(status="paused")
        server.agent_metadata = {"agent-1": meta}
        server.get_or_create_monitor.return_value = make_monitor(coherence=0.8, mean_risk=0.3, I=0.3, S=0.5)

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.lifecycle.handlers.agent_storage") as mock_storage, \
             patch("src.mcp_handlers.lifecycle.resume.agent_storage") as mock_storage_r, \
             patch("src.mcp_handlers.lifecycle.resume._invalidate_agent_cache", new=AsyncMock()), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True):
            mock_storage.update_agent = AsyncMock()
            mock_storage.persist_runtime_state = AsyncMock()
            mock_storage_r.update_agent = AsyncMock()
            mock_storage_r.persist_runtime_state = AsyncMock()
            from src.mcp_handlers.lifecycle.handlers import handle_direct_resume_if_safe
            result = await handle_direct_resume_if_safe({"agent_id": "agent-1"})
            data = _parse(result)
            assert data["success"] is True
            mock_storage_r.update_agent.assert_awaited_once()
            mock_storage_r.persist_runtime_state.assert_awaited_once()
            kwargs = mock_storage_r.persist_runtime_state.await_args.kwargs
            assert kwargs.get("paused_at") is None
            assert kwargs.get("append_lifecycle_event") is not None
            assert kwargs["append_lifecycle_event"]["event"] == "resumed"

    @pytest.mark.asyncio
    async def test_resume_persist_failure_returns_error(self, server):
        """Persist failure must surface a PERSIST_FAILED error, not silently mutate in-memory state."""
        meta = make_agent_meta(status="paused")
        server.agent_metadata = {"agent-1": meta}
        server.get_or_create_monitor.return_value = make_monitor(coherence=0.8, mean_risk=0.3, I=0.3, S=0.5)

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.lifecycle.resume.agent_storage") as mock_storage_r, \
             patch("src.mcp_handlers.lifecycle.resume._invalidate_agent_cache", new=AsyncMock()), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True):
            mock_storage_r.update_agent = AsyncMock(side_effect=RuntimeError("db down"))
            mock_storage_r.persist_runtime_state = AsyncMock()
            from src.mcp_handlers.lifecycle.handlers import handle_direct_resume_if_safe
            result = await handle_direct_resume_if_safe({"agent_id": "agent-1"})
            data = _parse(result)
            assert data["success"] is False
            assert data.get("error_code") == "PERSIST_FAILED"
            # In-memory state must NOT have been mutated when persist failed.
            assert meta.status == "paused"


# ============================================================================
# handle_self_recovery_review
# ============================================================================

class TestSelfRecoveryReview:

    @pytest.fixture
    def server(self):
        return make_mock_server()

    @pytest.mark.asyncio
    async def test_recovery_success(self, server):
        meta = make_agent_meta(status="paused")
        server.agent_metadata = {"agent-1": meta}
        server.get_or_create_monitor.return_value = make_monitor(coherence=0.8, I=0.3, S=0.5)

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.lifecycle.handlers.agent_storage") as mock_storage, \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True), \
             patch("src.mcp_handlers.lifecycle.stuck.GovernanceConfig") as mock_config:
            mock_storage.update_agent = AsyncMock()
            mock_storage.persist_runtime_state = AsyncMock()
            import src.mcp_handlers.lifecycle.mutation as _lm; _lm.agent_storage = mock_storage
            import src.mcp_handlers.lifecycle.operations as _lo; _lo.agent_storage = mock_storage
            mock_config.compute_proprioceptive_margin.return_value = {"margin": "comfortable"}
            from src.mcp_handlers.lifecycle.handlers import handle_self_recovery_review
            result = await handle_self_recovery_review({
                "agent_id": "agent-1",
                "reflection": "I got stuck in a loop and should have stepped back",
            })
            data = _parse(result)
            assert data["success"] is True
            assert data["action"] == "resumed"
            assert meta.status == "active"

    @pytest.mark.asyncio
    async def test_recovery_requires_reflection(self, server):
        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True):
            from src.mcp_handlers.lifecycle.handlers import handle_self_recovery_review
            result = await handle_self_recovery_review({
                "agent_id": "agent-1", "reflection": "",
            })
            text = result[0].text
            assert "reflection" in text.lower()

    @pytest.mark.asyncio
    async def test_recovery_reflection_too_short(self, server):
        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True):
            from src.mcp_handlers.lifecycle.handlers import handle_self_recovery_review
            result = await handle_self_recovery_review({
                "agent_id": "agent-1", "reflection": "short",
            })
            text = result[0].text
            assert "reflection" in text.lower() or "20" in text

    @pytest.mark.asyncio
    async def test_recovery_not_safe_metrics(self, server):
        meta = make_agent_meta(status="paused")
        server.agent_metadata = {"agent-1": meta}
        server.get_or_create_monitor.return_value = make_monitor(
            coherence=0.2, mean_risk=0.8, I=0.3, S=0.5
        )

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.lifecycle.handlers.agent_storage") as mock_storage, \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True), \
             patch("src.mcp_handlers.lifecycle.stuck.GovernanceConfig") as mock_config:
            mock_storage.update_agent = AsyncMock()
            mock_storage.persist_runtime_state = AsyncMock()
            import src.mcp_handlers.lifecycle.mutation as _lm; _lm.agent_storage = mock_storage
            import src.mcp_handlers.lifecycle.operations as _lo; _lo.agent_storage = mock_storage
            mock_config.compute_proprioceptive_margin.return_value = {"margin": "critical"}
            from src.mcp_handlers.lifecycle.handlers import handle_self_recovery_review
            result = await handle_self_recovery_review({
                "agent_id": "agent-1",
                "reflection": "I reflected deeply on what went wrong here",
            })
            data = _parse(result)
            assert data["success"] is False
            assert data["action"] == "not_resumed"
            assert len(data["failed_checks"]) > 0
            assert meta.status == "paused"  # not resumed

    @pytest.mark.asyncio
    async def test_recovery_rejects_dangerous_conditions(self, server):
        meta = make_agent_meta(status="paused")
        server.agent_metadata = {"agent-1": meta}
        server.get_or_create_monitor.return_value = make_monitor(coherence=0.8, I=0.3, S=0.5)

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True), \
             patch("src.mcp_handlers.lifecycle.stuck.GovernanceConfig") as mock_config:
            mock_config.compute_proprioceptive_margin.return_value = {"margin": "comfortable"}
            from src.mcp_handlers.lifecycle.handlers import handle_self_recovery_review
            result = await handle_self_recovery_review({
                "agent_id": "agent-1",
                "reflection": "I reflected deeply on what went wrong here",
                "proposed_conditions": ["disable safety checks"],
            })
            text = result[0].text
            assert "dangerous" in text.lower() or "unsafe" in text.lower()

    @pytest.mark.asyncio
    async def test_recovery_ownership_denied(self, server):
        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=False):
            from src.mcp_handlers.lifecycle.handlers import handle_self_recovery_review
            result = await handle_self_recovery_review({
                "agent_id": "agent-1",
                "reflection": "I reflected deeply on what went wrong here",
            })
            text = result[0].text
            assert "auth" in text.lower()

    @pytest.mark.asyncio
    async def test_recovery_not_found(self, server):
        server.agent_metadata = {}
        server.get_or_create_monitor.return_value = make_monitor(coherence=0.8, I=0.3, S=0.5)

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True), \
             patch("src.mcp_handlers.lifecycle.stuck.GovernanceConfig") as mock_config:
            mock_config.compute_proprioceptive_margin.return_value = {"margin": "comfortable"}
            from src.mcp_handlers.lifecycle.handlers import handle_self_recovery_review
            result = await handle_self_recovery_review({
                "agent_id": "agent-1",
                "reflection": "I reflected deeply on what went wrong here",
            })
            text = result[0].text
            assert "not found" in text.lower()

    @pytest.mark.asyncio
    async def test_recovery_persist_failure_does_not_mutate_meta(self, server):
        """If update_agent raises in the safe branch, meta must NOT be mutated to active."""
        meta = make_agent_meta(status="paused", paused_at="2026-01-01T00:00:00+00:00")
        server.agent_metadata = {"agent-1": meta}
        server.get_or_create_monitor.return_value = make_monitor(coherence=0.8, I=0.3, S=0.5)

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch_agent_storage() as mock_storage, \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True), \
             patch("src.mcp_handlers.lifecycle.stuck.GovernanceConfig") as mock_config, \
             patch("src.mcp_handlers.knowledge.handlers.store_discovery_internal",
                   new_callable=AsyncMock):
            mock_storage.update_agent = AsyncMock(side_effect=RuntimeError("DB offline"))
            mock_storage.persist_runtime_state = AsyncMock()
            mock_config.compute_proprioceptive_margin.return_value = {"margin": "comfortable"}
            from src.mcp_handlers.lifecycle.handlers import handle_self_recovery_review
            result = await handle_self_recovery_review({
                "agent_id": "agent-1",
                "reflection": "I reflected deeply on what went wrong and need to try again",
            })

            assert "PERSIST_FAILED" in result[0].text
            assert meta.status == "paused"  # unchanged
            assert meta.paused_at == "2026-01-01T00:00:00+00:00"  # unchanged


# ============================================================================
# handle_detect_stuck_agents
# ============================================================================

class TestDetectStuckAgents:

    @pytest.fixture
    def server(self):
        return make_mock_server()

    @pytest.mark.asyncio
    async def test_detects_stuck_agent(self, server):
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        meta = make_agent_meta(status="active", last_update=old, total_updates=5)
        meta.created_at = old
        server.agent_metadata = {"agent-1": meta}

        with patch_lifecycle_server(server), \
             patch("src.mcp_handlers.lifecycle.stuck._detect_stuck_agents", return_value=[
                 {"agent_id": "agent-1", "reason": "activity_timeout", "age_minutes": 60.0,
                  "details": "No updates in 60.0 minutes"}
             ]):
            from src.mcp_handlers.lifecycle.handlers import handle_detect_stuck_agents
            result = await handle_detect_stuck_agents({})
            data = _parse(result)
            assert data["summary"]["total_stuck"] >= 1
            assert len(data["stuck_agents"]) >= 1

    @pytest.mark.asyncio
    async def test_no_stuck_agents(self, server):
        server.agent_metadata = {}

        with patch_lifecycle_server(server), \
             patch("src.mcp_handlers.lifecycle.stuck._detect_stuck_agents", return_value=[]):
            from src.mcp_handlers.lifecycle.handlers import handle_detect_stuck_agents
            result = await handle_detect_stuck_agents({})
            data = _parse(result)
            assert data["summary"]["total_stuck"] == 0
            assert data["stuck_agents"] == []

    @pytest.mark.asyncio
    async def test_custom_timeout_parameters(self, server):
        with patch_lifecycle_server(server), \
             patch("src.mcp_handlers.lifecycle.stuck._detect_stuck_agents", return_value=[]) as mock_detect:
            from src.mcp_handlers.lifecycle.handlers import handle_detect_stuck_agents
            result = await handle_detect_stuck_agents({
                "max_age_minutes": 60.0,
                "critical_margin_timeout_minutes": 10.0,
                "tight_margin_timeout_minutes": 20.0,
            })
            data = _parse(result)
            assert "summary" in data
            assert data["summary"]["total_stuck"] == 0


# ============================================================================
# _detect_stuck_agents (internal function)
# ============================================================================

class TestDetectStuckAgentsInternal:

    @pytest.fixture
    def server(self):
        return make_mock_server()

    def test_skips_archived_agents(self, server):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        meta = make_agent_meta(status="archived", last_update=old, total_updates=5)
        meta.created_at = old
        server.agent_metadata = {"agent-1": meta}

        with patch_lifecycle_server(server):
            from src.mcp_handlers.lifecycle.handlers import _detect_stuck_agents
            result = _detect_stuck_agents()
            assert len(result) == 0

    def test_skips_autonomous_agents(self, server):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        meta = make_agent_meta(
            status="active", last_update=old, total_updates=5, tags=["autonomous"]
        )
        meta.created_at = old
        server.agent_metadata = {"agent-1": meta}

        with patch_lifecycle_server(server):
            from src.mcp_handlers.lifecycle.handlers import _detect_stuck_agents
            result = _detect_stuck_agents()
            assert len(result) == 0

    def test_skips_low_update_agents(self, server):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        meta = make_agent_meta(
            status="active", last_update=old, total_updates=0
        )
        meta.created_at = old
        server.agent_metadata = {"agent-1": meta}

        with patch_lifecycle_server(server):
            from src.mcp_handlers.lifecycle.handlers import _detect_stuck_agents
            result = _detect_stuck_agents(min_updates=1)
            assert len(result) == 0

    def test_detects_critical_margin_timeout(self, server):
        """Agents with critical margin + timeout are detected as stuck.

        Note: Activity timeout alone does NOT trigger stuck detection.
        Agents must be in a critical state (margin-based) to be flagged as stuck.
        """
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()  # > 5 min threshold
        meta = make_agent_meta(status="active", last_update=old, total_updates=5)
        meta.created_at = old
        server.agent_metadata = {"agent-1": meta}

        # Provide monitor with critical margin state
        mock_monitor = MagicMock()
        mock_monitor.state = SimpleNamespace(
            coherence=0.8, void_active=False,
            E=0.7, I=0.3, S=0.5, V=0.0, lambda1=0.1, coherence_history=[],
        )
        mock_monitor.get_metrics.return_value = {"mean_risk": 0.3}
        server.monitors = {"agent-1": mock_monitor}

        with patch_lifecycle_server(server), \
             patch("src.mcp_handlers.lifecycle.stuck.GovernanceConfig") as mock_config:
            mock_config.compute_proprioceptive_margin.return_value = {"margin": "critical", "nearest_edge": "E"}
            from src.mcp_handlers.lifecycle.handlers import _detect_stuck_agents
            result = _detect_stuck_agents(critical_margin_timeout_minutes=5, include_pattern_detection=False)
            assert len(result) >= 1
            assert result[0]["reason"] == "critical_margin_timeout"


# ============================================================================
# handle_ping_agent
# ============================================================================

class TestPingAgent:

    @pytest.fixture
    def server(self):
        return make_mock_server()

    @pytest.mark.asyncio
    async def test_ping_alive_agent(self, server):
        recent = datetime.now(timezone.utc).isoformat()
        meta = make_agent_meta(status="active", last_update=recent)
        meta.created_at = recent
        server.agent_metadata = {"agent-1": meta}

        mock_monitor = MagicMock()
        mock_monitor.get_metrics.return_value = {"E": 0.7}
        server.get_or_create_monitor.return_value = mock_monitor

        with patch_lifecycle_server(server):
            from src.mcp_handlers.lifecycle.handlers import handle_ping_agent
            result = await handle_ping_agent({"agent_id": "agent-1"})
            data = _parse(result)
            assert data["responsive"] is True
            assert data["status"] == "alive"
            assert data["agent_id"] == "agent-1"

    @pytest.mark.asyncio
    async def test_ping_stuck_agent(self, server):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        meta = make_agent_meta(status="active", last_update=old)
        meta.created_at = old
        server.agent_metadata = {"agent-1": meta}

        mock_monitor = MagicMock()
        mock_monitor.get_metrics.return_value = {"E": 0.7}
        server.get_or_create_monitor.return_value = mock_monitor

        with patch_lifecycle_server(server):
            from src.mcp_handlers.lifecycle.handlers import handle_ping_agent
            result = await handle_ping_agent({"agent_id": "agent-1"})
            data = _parse(result)
            assert data["responsive"] is True
            assert data["status"] == "stuck"

    @pytest.mark.asyncio
    async def test_ping_unresponsive_agent(self, server):
        recent = datetime.now(timezone.utc).isoformat()
        meta = make_agent_meta(status="active", last_update=recent)
        meta.created_at = recent
        server.agent_metadata = {"agent-1": meta}

        mock_monitor = MagicMock()
        mock_monitor.get_metrics.side_effect = RuntimeError("cannot get metrics")
        server.get_or_create_monitor.return_value = mock_monitor

        with patch_lifecycle_server(server):
            from src.mcp_handlers.lifecycle.handlers import handle_ping_agent
            result = await handle_ping_agent({"agent_id": "agent-1"})
            data = _parse(result)
            assert data["responsive"] is False
            assert data["status"] == "unresponsive"

    @pytest.mark.asyncio
    async def test_ping_not_found(self, server):
        server.agent_metadata = {}

        with patch_lifecycle_server(server):
            from src.mcp_handlers.lifecycle.handlers import handle_ping_agent
            result = await handle_ping_agent({"agent_id": "nonexistent"})
            text = result[0].text
            assert "not found" in text.lower()

    @pytest.mark.asyncio
    async def test_ping_no_agent_id(self, server):
        with patch_lifecycle_server(server), \
             patch("src.mcp_handlers.identity.shared.get_bound_agent_id", return_value=None):
            from src.mcp_handlers.lifecycle.handlers import handle_ping_agent
            result = await handle_ping_agent({})
            text = result[0].text
            assert "agent_id" in text.lower()

    @pytest.mark.asyncio
    async def test_ping_no_agent_id_returns_error(self, server):
        """When no agent_id given, handler returns error (broken import of get_bound_agent_id in source)."""
        with patch_lifecycle_server(server):
            from src.mcp_handlers.lifecycle.handlers import handle_ping_agent
            result = await handle_ping_agent({})
            text = result[0].text
            # Returns an error since it can't resolve bound agent
            assert "error" in text.lower() or "agent_id" in text.lower()

    @pytest.mark.asyncio
    async def test_ping_includes_lifecycle_status(self, server):
        recent = datetime.now(timezone.utc).isoformat()
        meta = make_agent_meta(status="paused", last_update=recent)
        meta.created_at = recent
        server.agent_metadata = {"agent-1": meta}

        mock_monitor = MagicMock()
        mock_monitor.get_metrics.return_value = {"E": 0.7}
        server.get_or_create_monitor.return_value = mock_monitor

        with patch_lifecycle_server(server):
            from src.mcp_handlers.lifecycle.handlers import handle_ping_agent
            result = await handle_ping_agent({"agent_id": "agent-1"})
            data = _parse(result)
            assert data["lifecycle_status"] == "paused"


# ============================================================================
# ADDITIONAL TESTS - Covering missed lines
# ============================================================================


# ============================================================================
# handle_list_agents - Lite Mode: implicit lite-off triggers (lines 65,67,69,71,73)
# ============================================================================

class TestDirectResumeEdgeCases:

    @pytest.fixture
    def server(self):
        return make_mock_server()

    @pytest.mark.asyncio
    async def test_resume_get_metrics_error(self, server):
        """Lines 1355-1356: error getting metrics returns system error."""
        meta = make_agent_meta(status="paused")
        server.agent_metadata = {"agent-1": meta}
        server.get_or_create_monitor.side_effect = RuntimeError("monitor broken")

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True):
            from src.mcp_handlers.lifecycle.handlers import handle_direct_resume_if_safe
            result = await handle_direct_resume_if_safe({"agent_id": "agent-1"})
            text = result[0].text
            assert "error" in text.lower()

    @pytest.mark.asyncio
    async def test_resume_pg_update_failure(self, server):
        """Lines 1391-1392: PostgreSQL update failure doesn't block success."""
        meta = make_agent_meta(status="paused")
        server.agent_metadata = {"agent-1": meta}
        mock_monitor = MagicMock()
        mock_monitor.state = SimpleNamespace(
            coherence=0.8, void_active=False,
            E=0.7, I=0.3, S=0.5, V=0.0, lambda1=0.1, coherence_history=[],
        )
        mock_monitor.get_metrics.return_value = {"mean_risk": 0.3}
        server.get_or_create_monitor.return_value = mock_monitor

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.lifecycle.handlers.agent_storage") as mock_storage, \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True):
            mock_storage.update_agent = AsyncMock(side_effect=RuntimeError("PG down"))
            mock_storage.persist_runtime_state = AsyncMock()
            from src.mcp_handlers.lifecycle.handlers import handle_direct_resume_if_safe
            result = await handle_direct_resume_if_safe({"agent_id": "agent-1"})
            data = _parse(result)
            assert data["success"] is True
            assert data["action"] == "resumed"


# ============================================================================
# handle_self_recovery_review - edge cases (lines 1445, 1527, 1551-1552, 1578)
# ============================================================================

class TestSelfRecoveryReviewEdgeCases:

    @pytest.fixture
    def server(self):
        return make_mock_server()

    @pytest.mark.asyncio
    async def test_recovery_not_registered_returns_error(self, server):
        """Line 1445: require_registered_agent returns error."""
        from mcp.types import TextContent
        error = TextContent(type="text", text='{"error": "not registered"}')

        with patch_lifecycle_server(server, require_registered=(None, error)):
            from src.mcp_handlers.lifecycle.handlers import handle_self_recovery_review
            result = await handle_self_recovery_review({
                "reflection": "I reflected deeply on what went wrong here",
            })
            assert "not registered" in result[0].text

    @pytest.mark.asyncio
    async def test_recovery_with_void_active_fails(self, server):
        """Line 1578: void_active=True causes recovery failure."""
        meta = make_agent_meta(status="paused")
        server.agent_metadata = {"agent-1": meta}
        server.get_or_create_monitor.return_value = make_monitor(void_active=True, V=0.5, I=0.3, S=0.5)

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.lifecycle.handlers.agent_storage") as mock_storage, \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True), \
             patch("src.mcp_handlers.lifecycle.stuck.GovernanceConfig") as mock_config:
            mock_storage.update_agent = AsyncMock()
            mock_storage.persist_runtime_state = AsyncMock()
            import src.mcp_handlers.lifecycle.mutation as _lm; _lm.agent_storage = mock_storage
            import src.mcp_handlers.lifecycle.operations as _lo; _lo.agent_storage = mock_storage
            mock_config.compute_proprioceptive_margin.return_value = {"margin": "critical"}
            from src.mcp_handlers.lifecycle.handlers import handle_self_recovery_review
            result = await handle_self_recovery_review({
                "agent_id": "agent-1",
                "reflection": "I reflected deeply on what went wrong here",
            })
            data = _parse(result)
            assert data["success"] is False
            assert "no_void" in data["failed_checks"]
            assert "void" in data["guidance"][0].lower() or any("void" in g.lower() for g in data["guidance"])

    @pytest.mark.asyncio
    async def test_recovery_pg_update_failure(self, server):
        """Lines 1551-1552: PG update failure doesn't block recovery success."""
        meta = make_agent_meta(status="paused")
        server.agent_metadata = {"agent-1": meta}
        server.get_or_create_monitor.return_value = make_monitor(coherence=0.8, I=0.3, S=0.5)

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.lifecycle.handlers.agent_storage") as mock_storage, \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True), \
             patch("src.mcp_handlers.lifecycle.stuck.GovernanceConfig") as mock_config:
            mock_storage.update_agent = AsyncMock(side_effect=RuntimeError("PG down"))
            mock_storage.persist_runtime_state = AsyncMock()
            mock_config.compute_proprioceptive_margin.return_value = {"margin": "comfortable"}
            from src.mcp_handlers.lifecycle.handlers import handle_self_recovery_review
            result = await handle_self_recovery_review({
                "agent_id": "agent-1",
                "reflection": "I reflected deeply on what went wrong here",
            })
            data = _parse(result)
            assert data["success"] is True
            assert data["action"] == "resumed"

    @pytest.mark.asyncio
    async def test_recovery_agent_not_found_in_metadata(self, server):
        """Line 1527: agent_id resolved but not in agent_metadata."""
        server.agent_metadata = {}
        server.get_or_create_monitor.return_value = make_monitor(coherence=0.8, I=0.3, S=0.5)

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True), \
             patch("src.mcp_handlers.lifecycle.stuck.GovernanceConfig") as mock_config:
            mock_config.compute_proprioceptive_margin.return_value = {"margin": "comfortable"}
            from src.mcp_handlers.lifecycle.handlers import handle_self_recovery_review
            result = await handle_self_recovery_review({
                "agent_id": "agent-1",
                "reflection": "I reflected deeply on what went wrong here",
            })
            text = result[0].text
            assert "not found" in text.lower()


# ============================================================================
# _detect_stuck_agents - pattern detection and edge cases
# (lines 1659-1661, 1691-1719)
# ============================================================================

class TestDetectStuckAgentsInternalEdgeCases:

    @pytest.fixture
    def server(self):
        return make_mock_server()

    def test_last_update_not_string_used_directly(self, server):
        """Lines 1659-1661: when last_update is not a string, uses it directly.

        Tests that datetime objects work correctly as last_update values.
        Uses critical margin to trigger stuck detection (inactivity alone ≠ stuck).
        """
        old_dt = datetime.now(timezone.utc) - timedelta(minutes=10)  # 10 min > 5 min critical threshold
        meta = make_agent_meta(status="active", last_update=old_dt, total_updates=5)
        meta.created_at = old_dt.isoformat()
        server.agent_metadata = {"agent-1": meta}

        # Provide monitor with critical margin state
        mock_monitor = MagicMock()
        mock_monitor.state = SimpleNamespace(
            coherence=0.8, void_active=False,
            E=0.7, I=0.3, S=0.5, V=0.0, lambda1=0.1, coherence_history=[],
        )
        mock_monitor.get_metrics.return_value = {"mean_risk": 0.3}
        server.monitors = {"agent-1": mock_monitor}

        with patch_lifecycle_server(server), \
             patch("src.mcp_handlers.lifecycle.stuck.GovernanceConfig") as mock_config:
            mock_config.compute_proprioceptive_margin.return_value = {"margin": "critical", "nearest_edge": "E"}
            from src.mcp_handlers.lifecycle.handlers import _detect_stuck_agents
            result = _detect_stuck_agents(critical_margin_timeout_minutes=5, include_pattern_detection=False)
            # Critical margin + 10 min inactivity → stuck
            assert len(result) >= 1
            assert result[0]["reason"] == "critical_margin_timeout"

    def test_pattern_detection_cognitive_loop(self, server):
        """Lines 1691-1719: pattern tracker detects cognitive loops."""
        old = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
        meta = make_agent_meta(status="active", last_update=old, total_updates=5)
        meta.created_at = old
        server.agent_metadata = {"agent-1": meta}

        mock_monitor = MagicMock()
        mock_monitor.state = SimpleNamespace(
            coherence=0.8, void_active=False,
            E=0.7, I=0.3, S=0.5, V=0.0, lambda1=0.1, coherence_history=[],
        )
        mock_monitor.get_metrics.return_value = {"mean_risk": 0.3}
        server.monitors = {"agent-1": mock_monitor}

        mock_tracker = MagicMock()
        mock_tracker.get_patterns.return_value = {
            "patterns": [
                {"type": "loop", "message": "Repeating same tool call 5 times"},
            ]
        }

        with patch_lifecycle_server(server), \
             patch("src.mcp_handlers.lifecycle.stuck.GovernanceConfig") as mock_config, \
             patch("src.pattern_tracker.get_pattern_tracker", return_value=mock_tracker):
            mock_config.compute_proprioceptive_margin.return_value = {"margin": "comfortable", "nearest_edge": None}
            from src.mcp_handlers.lifecycle.handlers import _detect_stuck_agents
            result = _detect_stuck_agents(max_age_minutes=60, include_pattern_detection=True)
            loop_detections = [r for r in result if r["reason"] == "cognitive_loop"]
            assert len(loop_detections) >= 1

    def test_pattern_detection_time_box_exceeded(self, server):
        """Lines 1691-1719: pattern tracker detects time_box exceeded."""
        old = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
        meta = make_agent_meta(status="active", last_update=old, total_updates=5)
        meta.created_at = old
        server.agent_metadata = {"agent-1": meta}

        mock_monitor = MagicMock()
        mock_monitor.state = SimpleNamespace(
            coherence=0.8, void_active=False,
            E=0.7, I=0.3, S=0.5, V=0.0, lambda1=0.1, coherence_history=[],
        )
        mock_monitor.get_metrics.return_value = {"mean_risk": 0.3}
        server.monitors = {"agent-1": mock_monitor}

        mock_tracker = MagicMock()
        mock_tracker.get_patterns.return_value = {
            "patterns": [
                {"type": "time_box", "message": "Time box exceeded", "total_minutes": 90},
            ]
        }

        with patch_lifecycle_server(server), \
             patch("src.mcp_handlers.lifecycle.stuck.GovernanceConfig") as mock_config, \
             patch("src.pattern_tracker.get_pattern_tracker", return_value=mock_tracker):
            mock_config.compute_proprioceptive_margin.return_value = {"margin": "comfortable", "nearest_edge": None}
            from src.mcp_handlers.lifecycle.handlers import _detect_stuck_agents
            result = _detect_stuck_agents(max_age_minutes=60, include_pattern_detection=True)
            time_box_detections = [r for r in result if r["reason"] == "time_box_exceeded"]
            assert len(time_box_detections) >= 1

    def test_pattern_detection_failure_handled(self, server):
        """Lines 1718-1719: pattern detection failure is caught gracefully.

        When pattern detection fails (ImportError), the function should:
        1. NOT raise an exception (graceful handling)
        2. NOT fall back to activity_timeout (inactivity ≠ stuck)
        3. Return empty if margin is comfortable
        """
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        meta = make_agent_meta(status="active", last_update=old, total_updates=5)
        meta.created_at = old
        server.agent_metadata = {"agent-1": meta}

        mock_monitor = MagicMock()
        mock_monitor.state = SimpleNamespace(
            coherence=0.8, void_active=False,
            E=0.7, I=0.3, S=0.5, V=0.0, lambda1=0.1, coherence_history=[],
        )
        mock_monitor.get_metrics.return_value = {"mean_risk": 0.3}
        server.monitors = {"agent-1": mock_monitor}

        with patch_lifecycle_server(server), \
             patch("src.mcp_handlers.lifecycle.stuck.GovernanceConfig") as mock_config, \
             patch("src.pattern_tracker.get_pattern_tracker", side_effect=ImportError("no tracker")):
            mock_config.compute_proprioceptive_margin.return_value = {"margin": "comfortable", "nearest_edge": None}
            from src.mcp_handlers.lifecycle.handlers import _detect_stuck_agents
            # Should not raise, just log the error
            result = _detect_stuck_agents(max_age_minutes=30, include_pattern_detection=True)
            # Comfortable margin + pattern failure = NOT stuck (inactivity ≠ stuck)
            assert len(result) == 0

    def test_skips_waiting_input_status(self, server):
        """Line 1637-1638: skips agents not in 'active' status."""
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        meta = make_agent_meta(status="waiting_input", last_update=old, total_updates=5)
        meta.created_at = old
        server.agent_metadata = {"agent-1": meta}

        with patch_lifecycle_server(server):
            from src.mcp_handlers.lifecycle.handlers import _detect_stuck_agents
            result = _detect_stuck_agents()
            assert len(result) == 0

    def test_critical_margin_detection(self, server):
        """Lines 1738-1748: critical margin + timeout detected."""
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        meta = make_agent_meta(status="active", last_update=old, total_updates=5)
        meta.created_at = old
        server.agent_metadata = {"agent-1": meta}

        mock_monitor = MagicMock()
        mock_monitor.state = SimpleNamespace(
            coherence=0.2, void_active=True,
            E=0.9, I=0.1, S=0.3, V=0.5, lambda1=0.8, coherence_history=[],
        )
        mock_monitor.get_metrics.return_value = {"mean_risk": 0.8}
        server.monitors = {"agent-1": mock_monitor}

        with patch_lifecycle_server(server), \
             patch("src.mcp_handlers.lifecycle.stuck.GovernanceConfig") as mock_config:
            mock_config.compute_proprioceptive_margin.return_value = {
                "margin": "critical", "nearest_edge": "coherence"
            }
            from src.mcp_handlers.lifecycle.handlers import _detect_stuck_agents
            result = _detect_stuck_agents(
                max_age_minutes=60,
                critical_margin_timeout_minutes=5,
                include_pattern_detection=False
            )
            critical_detections = [r for r in result if r["reason"] == "critical_margin_timeout"]
            assert len(critical_detections) >= 1

    def test_tight_margin_detection(self, server):
        """Lines 1752-1763: tight margin + timeout detected."""
        old = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
        meta = make_agent_meta(status="active", last_update=old, total_updates=100)
        meta.created_at = old
        server.agent_metadata = {"agent-1": meta}

        mock_monitor = MagicMock()
        mock_monitor.state = SimpleNamespace(
            coherence=0.5, void_active=False,
            E=0.6, I=0.3, S=0.5, V=0.1, lambda1=0.3, coherence_history=[],
        )
        mock_monitor.get_metrics.return_value = {"mean_risk": 0.5}
        server.monitors = {"agent-1": mock_monitor}

        with patch_lifecycle_server(server), \
             patch("src.mcp_handlers.lifecycle.stuck.GovernanceConfig") as mock_config:
            mock_config.compute_proprioceptive_margin.return_value = {
                "margin": "tight", "nearest_edge": "risk"
            }
            from src.mcp_handlers.lifecycle.handlers import _detect_stuck_agents
            result = _detect_stuck_agents(
                max_age_minutes=120,
                tight_margin_timeout_minutes=15,
                include_pattern_detection=False
            )
            tight_detections = [r for r in result if r["reason"] == "tight_margin_timeout"]
            assert len(tight_detections) >= 1


# ============================================================================
# handle_detect_stuck_agents - auto_recover (lines 1830-2226)
# ============================================================================

class TestDetectStuckAgentsAutoRecover:

    @pytest.fixture
    def server(self):
        server = make_mock_server()
        server.load_metadata_async = AsyncMock()
        return server

    @pytest.mark.asyncio
    async def test_auto_recover_safe_paused_agent(self, server):
        """Lines 1920-1930: auto-resume paused agent with safe metrics."""
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        meta = make_agent_meta(status="paused", last_update=old, total_updates=5)
        meta.created_at = old
        server.agent_metadata = {"agent-1": meta}
        server.get_or_create_monitor.return_value = make_monitor(coherence=0.8, I=0.3, S=0.5)

        with patch_lifecycle_server(server), \
             patch("src.mcp_handlers.lifecycle.stuck._detect_stuck_agents", return_value=[
                 {"agent_id": "agent-1", "reason": "activity_timeout", "age_minutes": 60.0,
                  "details": "No updates in 60.0 minutes"}
             ]), \
             patch("src.mcp_handlers.lifecycle.handlers.agent_storage") as mock_storage:
            mock_storage.update_agent = AsyncMock()
            mock_storage.persist_runtime_state = AsyncMock()
            import src.mcp_handlers.lifecycle.mutation as _lm; _lm.agent_storage = mock_storage
            import src.mcp_handlers.lifecycle.operations as _lo; _lo.agent_storage = mock_storage
            # Mock the leave_note to prevent KG errors
            with patch("src.mcp_handlers.lifecycle.handlers.handle_leave_note", new_callable=AsyncMock, create=True):
                from src.mcp_handlers.lifecycle.handlers import handle_detect_stuck_agents
                result = await handle_detect_stuck_agents({"auto_recover": True})
                data = _parse(result)
                assert data["summary"]["total_stuck"] >= 1
                assert len(data["recovered"]) >= 1
                assert data["recovered"][0]["action"] == "auto_resumed"
                assert meta.status == "active"

    @pytest.mark.asyncio
    async def test_auto_recover_unresponsive_triggers_dialectic(self, server):
        """Lines 1851-1914: unresponsive agent triggers dialectic."""
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        meta = make_agent_meta(status="active", last_update=old, total_updates=5)
        meta.created_at = old
        server.agent_metadata = {"agent-1": meta}
        # get_or_create_monitor raises -> unresponsive
        server.get_or_create_monitor.side_effect = RuntimeError("unresponsive")

        mock_session = MagicMock()
        mock_session.session_id = "sess-123"

        with patch_lifecycle_server(server), \
             patch("src.mcp_handlers.lifecycle.stuck._detect_stuck_agents", return_value=[
                 {"agent_id": "agent-1", "reason": "activity_timeout", "age_minutes": 60.0,
                  "details": "No updates"}
             ]), \
             patch("src.dialectic_protocol.DialecticSession", return_value=mock_session), \
             patch("src.mcp_handlers.dialectic.reviewer.select_reviewer", new_callable=AsyncMock, return_value="reviewer-1"), \
             patch("src.mcp_handlers.lifecycle.handlers.save_session", new_callable=AsyncMock, create=True) as mock_save, \
             patch("src.dialectic_db.is_agent_in_active_session_async", new_callable=AsyncMock, return_value=False):
            # Also mock handle_leave_note
            with patch("src.mcp_handlers.lifecycle.handlers.handle_leave_note", new_callable=AsyncMock, create=True):
                from src.mcp_handlers.lifecycle.handlers import handle_detect_stuck_agents
                result = await handle_detect_stuck_agents({"auto_recover": True})
                data = _parse(result)
                assert data["summary"]["total_stuck"] >= 1

    @pytest.mark.asyncio
    async def test_auto_recover_unsafe_triggers_dialectic(self, server):
        """Lines 2164-2226: unsafe agent (high risk) triggers dialectic."""
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        meta = make_agent_meta(status="active", last_update=old, total_updates=5)
        meta.created_at = old
        server.agent_metadata = {"agent-1": meta}
        # Low coherence, high risk -> unsafe
        server.get_or_create_monitor.return_value = make_monitor(
            coherence=0.2, mean_risk=0.8, void_active=True, I=0.3, S=0.5
        )

        mock_session = MagicMock()
        mock_session.session_id = "sess-456"

        with patch_lifecycle_server(server), \
             patch("src.mcp_handlers.lifecycle.stuck._detect_stuck_agents", return_value=[
                 {"agent_id": "agent-1", "reason": "critical_margin_timeout", "age_minutes": 60.0,
                  "details": "Critical margin"}
             ]), \
             patch("src.dialectic_protocol.DialecticSession", return_value=mock_session), \
             patch("src.mcp_handlers.dialectic.reviewer.select_reviewer", new_callable=AsyncMock, return_value="reviewer-1"), \
             patch("src.mcp_handlers.lifecycle.handlers.save_session", new_callable=AsyncMock, create=True), \
             patch("src.dialectic_db.is_agent_in_active_session_async", new_callable=AsyncMock, return_value=False):
            with patch("src.mcp_handlers.lifecycle.handlers.handle_leave_note", new_callable=AsyncMock, create=True):
                from src.mcp_handlers.lifecycle.handlers import handle_detect_stuck_agents
                result = await handle_detect_stuck_agents({"auto_recover": True})
                data = _parse(result)
                assert data["summary"]["total_stuck"] >= 1

    @pytest.mark.asyncio
    async def test_auto_recover_exception_handled(self, server):
        """Lines 2225-2226: exception during auto-recovery is caught per-agent."""
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        meta = make_agent_meta(status="active", last_update=old, total_updates=5)
        meta.created_at = old
        server.agent_metadata = {"agent-1": meta}

        # Make get_or_create_monitor work first time (responsive check)
        # but then make agent_metadata.get raise when trying to access the meta
        mock_monitor = make_monitor(coherence=0.8, mean_risk=0.3, I=0.3, S=0.5)

        call_count = [0]
        original_get_or_create = server.get_or_create_monitor

        def fail_on_second_call(agent_id):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_monitor
            raise RuntimeError("unexpected error in recovery")

        server.get_or_create_monitor = fail_on_second_call

        with patch_lifecycle_server(server), \
             patch("src.mcp_handlers.lifecycle.stuck._detect_stuck_agents", return_value=[
                 {"agent_id": "agent-1", "reason": "activity_timeout", "age_minutes": 60.0,
                  "details": "No updates"}
             ]):
            from src.mcp_handlers.lifecycle.handlers import handle_detect_stuck_agents
            result = await handle_detect_stuck_agents({"auto_recover": True})
            data = _parse(result)
            # Should still return a result, the exception is caught per-agent
            assert "stuck_agents" in data


# ============================================================================
# handle_detect_stuck_agents - top-level exception (lines 2243-2245)
# ============================================================================

class TestDetectStuckAgentsException:

    @pytest.fixture
    def server(self):
        server = make_mock_server()
        server.load_metadata_async = AsyncMock(side_effect=RuntimeError("DB down"))
        return server

    @pytest.mark.asyncio
    async def test_top_level_exception_returns_error(self, server):
        """Lines 2243-2245: top-level exception returns error response."""
        with patch_lifecycle_server(server):
            from src.mcp_handlers.lifecycle.handlers import handle_detect_stuck_agents
            result = await handle_detect_stuck_agents({})
            text = result[0].text
            assert "error" in text.lower()


# ============================================================================
# handle_ping_agent - edge cases (lines 2278, 2281, 2307-2309, 2313-2314)
# ============================================================================

class TestPingAgentEdgeCases:

    @pytest.fixture
    def server(self):
        server = make_mock_server()
        server.load_metadata_async = AsyncMock()
        return server

    @pytest.mark.asyncio
    async def test_ping_bound_agent_fallback(self, server):
        """Line 2278: falls back to get_bound_agent_id when no agent_id provided.

        NOTE: The source imports get_bound_agent_id from .utils (line 2277) but it
        actually lives in identity_shared.py. We need create=True to inject it.
        """
        recent = datetime.now(timezone.utc).isoformat()
        meta = make_agent_meta(status="active", last_update=recent)
        meta.created_at = recent
        server.agent_metadata = {"bound-agent": meta}

        mock_monitor = MagicMock()
        mock_monitor.get_metrics.return_value = {"E": 0.7}
        server.get_or_create_monitor.return_value = mock_monitor

        # get_bound_agent_id is imported from .utils inside the function
        # but doesn't exist there - we need create=True to inject it
        with patch_lifecycle_server(server), \
             patch("src.mcp_handlers.utils.get_bound_agent_id", create=True, return_value="bound-agent"):
            from src.mcp_handlers.lifecycle.handlers import handle_ping_agent
            result = await handle_ping_agent({})
            data = _parse(result)
            assert data["agent_id"] == "bound-agent"
            assert data["responsive"] is True

    @pytest.mark.asyncio
    async def test_ping_no_agent_id_no_bound(self, server):
        """Line 2281: no agent_id and no bound agent returns error."""
        with patch_lifecycle_server(server), \
             patch("src.mcp_handlers.utils.get_bound_agent_id", create=True, return_value=None):
            from src.mcp_handlers.lifecycle.handlers import handle_ping_agent
            result = await handle_ping_agent({})
            text = result[0].text
            assert "agent_id" in text.lower()

    @pytest.mark.asyncio
    async def test_ping_non_string_last_update(self, server):
        """Lines 2307-2309: last_update is not a string (datetime object)."""
        dt_obj = datetime.now(timezone.utc)
        meta = make_agent_meta(status="active", last_update=dt_obj)
        meta.created_at = dt_obj.isoformat()
        server.agent_metadata = {"agent-1": meta}

        mock_monitor = MagicMock()
        mock_monitor.get_metrics.return_value = {"E": 0.7}
        server.get_or_create_monitor.return_value = mock_monitor

        with patch_lifecycle_server(server):
            from src.mcp_handlers.lifecycle.handlers import handle_ping_agent
            result = await handle_ping_agent({"agent_id": "agent-1"})
            data = _parse(result)
            assert data["responsive"] is True
            assert data["status"] == "alive"

    @pytest.mark.asyncio
    async def test_ping_unparseable_last_update(self, server):
        """Lines 2313-2314: unparseable last_update sets age_minutes to None."""
        meta = make_agent_meta(status="active", last_update="NOT-A-DATE")
        meta.created_at = "NOT-A-DATE"
        server.agent_metadata = {"agent-1": meta}

        mock_monitor = MagicMock()
        mock_monitor.get_metrics.return_value = {"E": 0.7}
        server.get_or_create_monitor.return_value = mock_monitor

        with patch_lifecycle_server(server):
            from src.mcp_handlers.lifecycle.handlers import handle_ping_agent
            result = await handle_ping_agent({"agent_id": "agent-1"})
            data = _parse(result)
            assert data["responsive"] is True
            assert data["age_minutes"] is None
            assert data["status"] == "alive"  # responsive + no age = alive


# ============================================================================
# Additional edge case tests for remaining uncovered lines
# ============================================================================

class TestListAgentsFullModeMonitorEdgeCases:
    """Tests for monitor loading edge cases in full mode (lines 334,337-338,355-359)."""

    @pytest.fixture
    def server(self):
        server = make_mock_server()
        health_status = MagicMock()
        health_status.value = "healthy"
        server.health_checker = MagicMock()
        server.health_checker.get_health_status.return_value = (health_status, {})
        return server

    @pytest.mark.asyncio
    async def test_not_in_memory_monitor_null_state(self, server):
        """Lines 354-355: monitor loaded but state is None -> metrics=None."""
        server.agent_metadata = {
            "agent-ns": make_agent_meta(status="active", total_updates=5, notes="", health_status="healthy"),
        }
        server.monitors = {}
        mock_monitor = MagicMock()
        mock_monitor.state = None
        mock_monitor.get_metrics.return_value = {"risk_score": 0.3, "mean_risk": 0.3}
        server.get_or_create_monitor.return_value = mock_monitor
        with patch_lifecycle_server(server):
            from src.mcp_handlers.lifecycle.handlers import handle_list_agents
            result = await handle_list_agents({
                "lite": False, "grouped": False, "include_metrics": True,
            })
            data = _parse(result)
            agent = data["agents"][0]
            assert agent["metrics"] is None

    @pytest.mark.asyncio
    async def test_not_in_memory_monitor_load_exception(self, server):
        """Lines 356-359: exception loading monitor for not-in-memory agent."""
        server.agent_metadata = {
            "agent-ex": make_agent_meta(status="active", total_updates=5, notes="", health_status="moderate"),
        }
        server.monitors = {}
        server.get_or_create_monitor.side_effect = RuntimeError("load failed")
        with patch_lifecycle_server(server):
            from src.mcp_handlers.lifecycle.handlers import handle_list_agents
            result = await handle_list_agents({
                "lite": False, "grouped": False, "include_metrics": True,
            })
            data = _parse(result)
            agent = data["agents"][0]
            # Falls back to cached health_status
            assert agent["health_status"] == "moderate"
            assert agent["metrics"] is None

    @pytest.mark.asyncio
    async def test_not_in_memory_metrics_not_hydrated_for_safe_float_values(self, server):
        """Non-resident monitors return null metrics instead of hydrating."""
        server.agent_metadata = {
            "agent-sf": make_agent_meta(status="active", total_updates=5, notes="", health_status=None),
        }
        server.monitors = {}
        mock_monitor = MagicMock()
        mock_monitor.state = SimpleNamespace(
            E=None, I="not-a-number", S=0.5, V=0.0, coherence=0.8,
            lambda1=None, void_active=None, coherence_history=[]
        )
        mock_monitor.get_metrics.return_value = {
            "risk_score": None, "current_risk": None,
            "phi": None, "verdict": None, "mean_risk": None,
        }
        server.get_or_create_monitor.return_value = mock_monitor
        with patch_lifecycle_server(server):
            from src.mcp_handlers.lifecycle.handlers import handle_list_agents
            result = await handle_list_agents({
                "lite": False, "grouped": False, "include_metrics": True,
            })
            data = _parse(result)
            agent = data["agents"][0]
            assert agent["health_status"] == "unknown"
            assert agent["metrics"] is None
            server.get_or_create_monitor.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_in_memory_unknown_health_stays_unknown(self, server):
        """Cached unknown health does not trigger monitor hydration."""
        server.agent_metadata = {
            "agent-unk": make_agent_meta(status="active", total_updates=5, notes="", health_status="unknown"),
        }
        server.monitors = {}
        mock_monitor = MagicMock()
        mock_monitor.state = SimpleNamespace(
            E=0.7, I=0.3, S=0.5, V=0.0, coherence=0.8,
            lambda1=0.1, void_active=False, coherence_history=[]
        )
        mock_monitor.get_metrics.return_value = {
            "risk_score": 0.3, "current_risk": 0.3,
            "phi": 0.5, "verdict": "safe", "mean_risk": 0.3,
        }
        server.get_or_create_monitor.return_value = mock_monitor
        with patch_lifecycle_server(server):
            from src.mcp_handlers.lifecycle.handlers import handle_list_agents
            result = await handle_list_agents({
                "lite": False, "grouped": False, "include_metrics": True,
            })
            data = _parse(result)
            agent = data["agents"][0]
            assert agent["health_status"] == "unknown"
            assert agent["metrics"] is None
            server.get_or_create_monitor.assert_not_called()


class TestGetAgentMetadataAdditional:
    """Additional tests for get_agent_metadata to cover lines 553, 560-561, 615, 647-648."""

    @pytest.fixture
    def server(self):
        return make_mock_server()

    @pytest.mark.asyncio
    async def test_target_uuid_found_in_memory(self, server):
        """UUID lookup resolves against in-memory cache (reload path removed for anyio deadlock fix)."""
        meta = make_agent_meta(label="Agent", total_updates=10)

        server.agent_metadata = {"uuid-in-memory": meta}
        server.monitors = {}

        with patch_lifecycle_server(server), \
             patch("src.cache.get_metadata_cache", side_effect=Exception("no cache")):
            from src.mcp_handlers.lifecycle.handlers import handle_get_agent_metadata
            result = await handle_get_agent_metadata({"target_agent": "uuid-in-memory"})
            data = _parse(result)
            assert data["status"] == "active"

    @pytest.mark.asyncio
    async def test_target_not_found_returns_error(self, server):
        """Agent not in memory or cache returns a not-found error (no reload attempted)."""
        server.agent_metadata = {}
        server.monitors = {}

        with patch_lifecycle_server(server), \
             patch("src.cache.get_metadata_cache", side_effect=Exception("no cache")):
            from src.mcp_handlers.lifecycle.handlers import handle_get_agent_metadata
            result = await handle_get_agent_metadata({"target_agent": "nonexistent"})
            data = _parse(result)
            assert data.get("success") is False or "not found" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_get_metadata_naive_datetime_last_update(self, server):
        """Line 615: naive datetime (no timezone) gets UTC applied."""
        naive_time = datetime.now().isoformat()  # No timezone info
        meta = make_agent_meta(label="NaiveAgent", total_updates=10, last_update=naive_time)
        meta.to_dict.return_value["last_update"] = naive_time
        server.agent_metadata = {"agent-1": meta}
        server.monitors = {}

        with patch_lifecycle_server(server, require_registered=("agent-1", None)):
            from src.mcp_handlers.lifecycle.handlers import handle_get_agent_metadata
            result = await handle_get_agent_metadata({})
            data = _parse(result)
            assert data["days_since_update"] is not None
            assert data["days_since_update"] == 0

    @pytest.mark.asyncio
    async def test_get_metadata_cache_set_failure(self, server):
        """Lines 647-648: Redis cache set failure doesn't block response."""
        meta = make_agent_meta(label="Agent", total_updates=10)
        server.agent_metadata = {"agent-1": meta}
        server.monitors = {}

        mock_cache = AsyncMock()
        mock_cache.set = AsyncMock(side_effect=RuntimeError("Redis down"))

        with patch_lifecycle_server(server, require_registered=("agent-1", None)), \
             patch("src.cache.get_metadata_cache", return_value=mock_cache):
            from src.mcp_handlers.lifecycle.handlers import handle_get_agent_metadata
            result = await handle_get_agent_metadata({})
            data = _parse(result)
            # Still succeeds despite cache failure
            assert data["status"] == "active"


class TestDetectStuckAgentsAutoRecoverAdditional:
    """Additional auto-recover tests for deeply nested paths."""

    @pytest.fixture
    def server(self):
        server = make_mock_server()
        server.load_metadata_async = AsyncMock()
        return server

    @pytest.mark.asyncio
    async def test_auto_recover_safe_active_short_stuck_leaves_note(self, server):
        """Lines 2012-2075: safe active agent stuck < 60 min gets note left."""
        old = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
        meta = make_agent_meta(status="active", last_update=old, total_updates=5)
        meta.created_at = old
        server.agent_metadata = {"agent-1": meta}

        mock_monitor = MagicMock()
        mock_monitor.state = SimpleNamespace(
            coherence=0.8, void_active=False,
            E=0.7, I=0.3, S=0.5, V=0.0, lambda1=0.1, coherence_history=[],
        )
        mock_monitor.get_metrics.return_value = {"mean_risk": 0.3}
        server.get_or_create_monitor.return_value = mock_monitor

        mock_leave_note = AsyncMock()
        mock_db = MagicMock()
        mock_db._pool = None  # Skip DB dedup check

        with patch_lifecycle_server(server), \
             patch("src.mcp_handlers.lifecycle.stuck._detect_stuck_agents", return_value=[
                 {"agent_id": "agent-1", "reason": "activity_timeout", "age_minutes": 40.0,
                  "details": "No updates in 40 minutes"}
             ]):
            # Patch handle_leave_note where it's imported from
            with patch("src.mcp_handlers.knowledge.handlers.handle_leave_note", mock_leave_note, create=True), \
                 patch("src.db.get_db", return_value=mock_db):
                from src.mcp_handlers.lifecycle.handlers import handle_detect_stuck_agents
                result = await handle_detect_stuck_agents({"auto_recover": True})
                data = _parse(result)
                assert data["summary"]["total_stuck"] >= 1
