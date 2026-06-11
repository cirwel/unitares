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

from tests.helpers import patch_lifecycle_server

from mcp.types import TextContent


@pytest.fixture
def bound_context():
    """Bind the transport context to agent-1 for handler tests.

    handle_get_governance_metrics now guards on the ACTUAL context
    binding for all no-explicit-agent_id calls (read-purity, trust
    contract §3.5) — patching require_agent_id alone no longer
    simulates a bound caller. Classes exercising the bound path opt in
    via @pytest.mark.usefixtures("bound_context"); the unbound path is
    pinned in tests/test_zero_observation_honesty.py.
    """
    with patch(
        "src.mcp_handlers.context.get_context_agent_id",
        return_value="agent-1",
    ):
        yield


# ============================================================================
# Helpers
# ============================================================================

def _parse(result):
    """Parse TextContent result(s) into a dict."""
    if isinstance(result, (list, tuple)):
        return json.loads(result[0].text)
    return json.loads(result.text)


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

class TestAssessThermodynamicSignificance:
    """Tests for the pure significance assessment function."""

    @pytest.fixture(autouse=True)
    def import_fn(self):
        from src.mcp_handlers.core import _assess_thermodynamic_significance
        self.assess = _assess_thermodynamic_significance

    def test_no_monitor_returns_not_significant(self):
        result = self.assess(None, {})
        assert result["is_significant"] is False
        assert "No monitor available" in result["reasons"]
        assert "timestamp" in result

    def test_empty_histories_not_significant(self):
        monitor = _make_monitor(risk_history=[], coherence_history=[], V=0.0)
        result = self.assess(monitor, {})
        assert result["is_significant"] is False
        assert result["reasons"] == []

    def test_stable_state_not_significant(self):
        monitor = _make_monitor(
            risk_history=[0.3, 0.3, 0.3, 0.3, 0.3],
            coherence_history=[0.8, 0.8, 0.8, 0.8, 0.8],
            V=0.0,
        )
        result = self.assess(monitor, {"decision": {"action": "approve"}})
        assert result["is_significant"] is False

    def test_risk_spike_is_significant(self):
        monitor = _make_monitor(
            risk_history=[0.3, 0.3, 0.3, 0.3, 0.6],
            coherence_history=[0.8, 0.8, 0.8, 0.8, 0.8],
        )
        result = self.assess(monitor, {})
        assert result["is_significant"] is True
        assert any("risk_spike" in r for r in result["reasons"])

    def test_coherence_drop_is_significant(self):
        monitor = _make_monitor(
            risk_history=[0.3, 0.3, 0.3, 0.3, 0.3],
            coherence_history=[0.8, 0.8, 0.8, 0.8, 0.5],
        )
        result = self.assess(monitor, {})
        assert result["is_significant"] is True
        assert any("coherence_drop" in r for r in result["reasons"])

    def test_void_threshold_is_significant(self):
        monitor = _make_monitor(V=0.15)
        result = self.assess(monitor, {})
        assert result["is_significant"] is True
        assert any("void_significant" in r for r in result["reasons"])

    def test_void_below_threshold_not_significant(self):
        monitor = _make_monitor(V=0.05)
        result = self.assess(monitor, {})
        # V=0.05 is below default threshold of 0.10
        assert not any("void_significant" in r for r in result["reasons"])

    def test_circuit_breaker_is_significant(self):
        monitor = _make_monitor()
        result = self.assess(monitor, {"circuit_breaker": {"triggered": True}})
        assert result["is_significant"] is True
        assert "circuit_breaker_triggered" in result["reasons"]

    def test_circuit_breaker_not_triggered_not_significant(self):
        monitor = _make_monitor()
        result = self.assess(monitor, {"circuit_breaker": {"triggered": False}})
        assert "circuit_breaker_triggered" not in result["reasons"]

    def test_pause_decision_is_significant(self):
        monitor = _make_monitor()
        result = self.assess(monitor, {"decision": {"action": "pause"}})
        assert result["is_significant"] is True
        assert "decision_pause" in result["reasons"]

    def test_reject_decision_is_significant(self):
        monitor = _make_monitor()
        result = self.assess(monitor, {"decision": {"action": "reject"}})
        assert result["is_significant"] is True
        assert "decision_reject" in result["reasons"]

    def test_approve_decision_not_significant(self):
        monitor = _make_monitor()
        result = self.assess(monitor, {"decision": {"action": "approve"}})
        assert "decision_approve" not in result["reasons"]

    def test_multiple_reasons_combined(self):
        monitor = _make_monitor(
            risk_history=[0.3, 0.3, 0.3, 0.3, 0.6],
            V=0.15,
        )
        result = self.assess(monitor, {"decision": {"action": "pause"}})
        assert result["is_significant"] is True
        assert len(result["reasons"]) >= 2

    def test_single_history_entry_no_crash(self):
        monitor = _make_monitor(risk_history=[0.5], coherence_history=[0.8])
        result = self.assess(monitor, {})
        assert isinstance(result["is_significant"], bool)

    def test_two_history_entries_risk_spike(self):
        # With exactly 2 entries: baseline is [first], current is second
        monitor = _make_monitor(
            risk_history=[0.1, 0.5],
            coherence_history=[0.8, 0.8],
        )
        result = self.assess(monitor, {})
        # delta = 0.5 - 0.1 = 0.4 > 0.15 threshold
        assert any("risk_spike" in r for r in result["reasons"])

    def test_empty_decision_dict(self):
        monitor = _make_monitor()
        result = self.assess(monitor, {"decision": {}})
        assert "decision_" not in " ".join(result["reasons"])

    def test_missing_decision_key(self):
        monitor = _make_monitor()
        result = self.assess(monitor, {})
        # No crash without decision key
        assert isinstance(result, dict)

    def test_timestamp_is_isoformat(self):
        monitor = _make_monitor()
        result = self.assess(monitor, {})
        from datetime import datetime
        # Should parse as ISO format without error
        datetime.fromisoformat(result["timestamp"])


# ============================================================================
# handle_get_governance_metrics
# ============================================================================

@pytest.mark.usefixtures("bound_context")
class TestGetGovernanceMetrics:
    """Tests for get_governance_metrics handler."""

    @pytest.fixture
    def mock_server(self):
        return _make_mock_mcp_server()

    @pytest.fixture
    def mock_monitor(self):
        return _make_monitor()

    @pytest.mark.asyncio
    async def test_requires_agent_id(self, mock_server):
        """Should return error when agent_id resolution fails."""
        error_tc = _make_error_text_content("agent_id required")

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=(None, error_tc)):

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({})
            data = _parse(result)
            assert "agent_id required" in json.dumps(data)

    @pytest.mark.asyncio
    async def test_lite_mode_default(self, mock_server, mock_monitor):
        """Lite mode is default; returns minimal metrics with status."""
        meta = _make_metadata(purpose="test purpose")
        mock_server.agent_metadata = {"agent-1": meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:

            MockClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({})  # lite=True by default

            data = _parse(result)
            # display_name takes precedence over auto-generated agent_id
            assert data["agent_id"] == "TestAgent"
            assert "status" in data
            assert "E" in data
            assert "I" in data
            assert "S" in data
            assert "V" in data
            assert "coherence" in data
            assert "risk_score" in data
            assert "_note" in data
            assert data["purpose"] == "test purpose"

    @pytest.mark.asyncio
    async def test_full_mode(self, mock_server, mock_monitor):
        """Full mode returns interpretation and reflection."""
        meta = _make_metadata(purpose=None)
        mock_server.agent_metadata = {"agent-1": meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:

            MockClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": False})

            data = _parse(result)
            assert "summary" in data
            # reflection is now conditional — omitted for healthy states
            # (S=0.2, no bad verdict → no reflection)

    @pytest.mark.asyncio
    async def test_uninitialized_agent_shows_pending(self, mock_server):
        """Uninitialized agent shows pending status in lite mode."""
        uninit_monitor = _make_monitor()
        uninit_monitor.get_metrics.return_value = {
            "E": 0.5, "I": 0.5, "S": 0.5, "V": 0.0,
            "coherence": None, "risk_score": None,
            "initialized": False, "status": "uninitialized",
            "complexity": None,
        }

        mock_server.agent_metadata = {"agent-1": _make_metadata(purpose=None)}
        mock_server.get_or_create_monitor.return_value = uninit_monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:

            MockClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": True})

            data = _parse(result)
            assert "uninitialized" in data["status"]

    @pytest.mark.asyncio
    async def test_no_purpose_returns_null(self, mock_server, mock_monitor):
        """Agent with no purpose set has null/missing purpose."""
        meta = _make_metadata(purpose=None)
        mock_server.agent_metadata = {"agent-1": meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:

            MockClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": True})
            data = _parse(result)
            # purpose should be None/null
            assert data.get("purpose") is None

    @pytest.mark.asyncio
    async def test_void_display_precision(self, mock_server):
        """Small non-zero void values show with precision."""
        monitor = _make_monitor()
        monitor.get_metrics.return_value = {
            "E": 0.7, "I": 0.6, "S": 0.2, "V": 0.000123,
            "coherence": 0.52, "risk_score": 0.3,
            "initialized": True, "status": "ok",
            "complexity": 0.5,
        }
        mock_server.agent_metadata = {"agent-1": _make_metadata()}
        mock_server.get_or_create_monitor.return_value = monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:

            MockClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": True})
            data = _parse(result)
            # Small non-zero V should show with precision
            v_value = data["V"]["value"]
            assert v_value != 0
            assert v_value == round(0.000123, 6)

    @pytest.mark.asyncio
    async def test_interpret_state_failure_handled_gracefully(self, mock_server):
        """If interpret_state raises, handler still returns data."""
        monitor = _make_monitor()
        monitor.state.interpret_state.side_effect = RuntimeError("interpret failed")
        mock_server.agent_metadata = {"agent-1": _make_metadata()}
        mock_server.get_or_create_monitor.return_value = monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:

            MockClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            # Full mode to trigger interpret_state
            result = await handle_get_governance_metrics({"lite": False})
            data = _parse(result)
            # Should still succeed without crashing (reflection conditional now)
            assert data["success"] is True


# ============================================================================
# handle_simulate_update
# ============================================================================

class TestSimulateUpdate:
    """Tests for simulate_update handler."""

    @pytest.fixture
    def mock_server(self):
        return _make_mock_mcp_server()

    @pytest.fixture
    def mock_monitor(self):
        return _make_monitor()

    @pytest.mark.asyncio
    async def test_fresh_state_no_agent(self, mock_server, mock_monitor):
        """Simulate with no registered agent uses fresh default state."""
        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.governance_monitor.UNITARESMonitor", return_value=mock_monitor), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=(None, None)):

            from src.mcp_handlers.core import handle_simulate_update
            result = await handle_simulate_update({"complexity": 0.5})

            data = _parse(result)
            assert data["simulation"] is True
            assert data["agent_state_source"] == "fresh"
            assert "note" in data

    @pytest.mark.asyncio
    async def test_existing_agent_uses_existing_state(self, mock_server, mock_monitor):
        """Simulate with existing agent uses their EISV state."""
        mock_server.agent_metadata = {"agent-1": _make_metadata()}
        mock_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)):

            from src.mcp_handlers.core import handle_simulate_update
            result = await handle_simulate_update({"complexity": 0.5})

            data = _parse(result)
            assert data["agent_state_source"] == "existing"
            assert "note" not in data

    @pytest.mark.asyncio
    async def test_lite_mode(self, mock_server, mock_monitor):
        """Lite mode returns minimal simulation response."""
        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.governance_monitor.UNITARESMonitor", return_value=mock_monitor), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=(None, None)):

            from src.mcp_handlers.core import handle_simulate_update
            result = await handle_simulate_update({"complexity": 0.5, "lite": True})

            data = _parse(result)
            assert data["simulation"] is True
            assert "_note" in data
            assert "decision" in data
            assert "metrics" in data

    @pytest.mark.asyncio
    async def test_full_mode_response(self, mock_server, mock_monitor):
        """Full mode returns all details from simulate_update."""
        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.governance_monitor.UNITARESMonitor", return_value=mock_monitor), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=(None, None)):

            from src.mcp_handlers.core import handle_simulate_update
            result = await handle_simulate_update({"complexity": 0.5, "lite": False})

            data = _parse(result)
            assert data["simulation"] is True
            assert "guidance" in data

    @pytest.mark.asyncio
    async def test_dialectic_conditions_cap_complexity(self, mock_server, mock_monitor):
        """Dialectic complexity_limit caps complexity and adds warning."""
        meta = _make_metadata(dialectic_conditions=[
            {"type": "complexity_limit", "value": 0.3}
        ])
        mock_server.agent_metadata = {"agent-1": meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)):

            from src.mcp_handlers.core import handle_simulate_update
            result = await handle_simulate_update({"complexity": 0.8})

            data = _parse(result)
            assert "dialectic_warning" in data

    @pytest.mark.asyncio
    async def test_dialectic_reduce_adjustment_caps_complexity(self, mock_server, mock_monitor):
        """Dialectic complexity_adjustment with action=reduce caps complexity."""
        meta = _make_metadata(dialectic_conditions=[
            {"type": "complexity_adjustment", "action": "reduce", "target_value": 0.4}
        ])
        mock_server.agent_metadata = {"agent-1": meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)):

            from src.mcp_handlers.core import handle_simulate_update
            result = await handle_simulate_update({"complexity": 0.9})

            data = _parse(result)
            assert "dialectic_warning" in data

    @pytest.mark.asyncio
    async def test_complexity_below_cap_no_warning(self, mock_server, mock_monitor):
        """Complexity below dialectic cap does not trigger warning."""
        meta = _make_metadata(dialectic_conditions=[
            {"type": "complexity_limit", "value": 0.8}
        ])
        mock_server.agent_metadata = {"agent-1": meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)):

            from src.mcp_handlers.core import handle_simulate_update
            result = await handle_simulate_update({"complexity": 0.5})

            data = _parse(result)
            assert "dialectic_warning" not in data

    @pytest.mark.asyncio
    async def test_default_complexity_and_ethical_drift(self, mock_server, mock_monitor):
        """Missing complexity and ethical_drift use sensible defaults."""
        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.governance_monitor.UNITARESMonitor", return_value=mock_monitor), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=(None, None)):

            from src.mcp_handlers.core import handle_simulate_update
            result = await handle_simulate_update({})

            data = _parse(result)
            assert data["simulation"] is True
            # Should not crash when parameters are missing

    @pytest.mark.asyncio
    async def test_confidence_none_derives_from_state(self, mock_server, mock_monitor):
        """When confidence is not provided, it derives from thermodynamic state (None)."""
        # The local import `from src.governance_monitor import UNITARESMonitor` inside
        # handle_simulate_update means we must patch at the source module level.
        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.governance_monitor.UNITARESMonitor", return_value=mock_monitor), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=(None, None)):

            from src.mcp_handlers.core import handle_simulate_update
            # No confidence parameter
            result = await handle_simulate_update({"complexity": 0.5})

            # Should call simulate_update with confidence=None
            mock_monitor.simulate_update.assert_called_once()
            call_kwargs = mock_monitor.simulate_update.call_args
            assert call_kwargs[1]["confidence"] is None


# ============================================================================
# handle_process_agent_update (the most important handler)
# ============================================================================


# ============================================================================
# handle_get_system_history (export.py)
# ============================================================================

class TestGetSystemHistory:
    """Tests for get_system_history handler."""

    @pytest.fixture
    def mock_server(self):
        return _make_mock_mcp_server()

    @pytest.fixture
    def mock_monitor(self):
        return _make_monitor(
            E_history=[0.7, 0.75],
            timestamp_history=["2026-01-20T12:00:00", "2026-01-20T13:00:00"],
        )

    @pytest.mark.asyncio
    async def test_returns_history_json(self, mock_server, mock_monitor):
        """Happy path: returns history in JSON format."""
        mock_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.introspection.export.mcp_server", mock_server), \
             patch("src.mcp_handlers.introspection.export.require_registered_agent", return_value=("agent-1", None)), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value="agent-1"):

            from src.mcp_handlers.introspection.export import handle_get_system_history
            result = await handle_get_system_history({"format": "json"})

            data = _parse(result)
            assert data.get("format") == "json"
            assert "history" in data
            assert data["agent_id"] == "agent-1"

    @pytest.mark.asyncio
    async def test_no_history_returns_empty_success(self, mock_server):
        """Agent with no history returns an empty export instead of an error."""
        empty_monitor = _make_monitor(E_history=[], timestamp_history=[])
        mock_server.get_or_create_monitor.return_value = empty_monitor

        with patch("src.mcp_handlers.introspection.export.mcp_server", mock_server), \
             patch("src.mcp_handlers.introspection.export.require_registered_agent", return_value=("agent-1", None)), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value="agent-1"):

            from src.mcp_handlers.introspection.export import handle_get_system_history
            result = await handle_get_system_history({})

            data = _parse(result)
            assert data["success"] is True
            assert data["empty"] is True
            assert data["history"] == []
            assert data["agent_id"] == "agent-1"

    @pytest.mark.asyncio
    async def test_requires_agent_registration(self, mock_server):
        """Without registered agent and no context, returns error."""
        error_tc = _make_error_text_content("not registered")

        with patch("src.mcp_handlers.introspection.export.mcp_server", mock_server), \
             patch("src.mcp_handlers.introspection.export.require_registered_agent", return_value=(None, error_tc)), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None):

            from src.mcp_handlers.introspection.export import handle_get_system_history
            result = await handle_get_system_history({})
            data = _parse(result)
            assert "not registered" in json.dumps(data)

    @pytest.mark.asyncio
    async def test_uses_context_agent_id(self, mock_server, mock_monitor):
        """Uses context agent_id when no explicit agent_id."""
        mock_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.introspection.export.mcp_server", mock_server), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value="ctx-agent-1"):

            from src.mcp_handlers.introspection.export import handle_get_system_history
            result = await handle_get_system_history({})

            data = _parse(result)
            assert data.get("agent_id") == "ctx-agent-1"

    @pytest.mark.asyncio
    async def test_explicit_agent_id_takes_precedence(self, mock_server, mock_monitor):
        """Explicit agent_id in arguments takes precedence over context."""
        mock_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.introspection.export.mcp_server", mock_server), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value="ctx-agent"):

            from src.mcp_handlers.introspection.export import handle_get_system_history
            result = await handle_get_system_history({"agent_id": "explicit-agent"})

            data = _parse(result)
            assert data.get("agent_id") == "explicit-agent"

    @pytest.mark.asyncio
    async def test_malformed_json_returns_error_envelope(self, mock_server, mock_monitor):
        """Watcher P016: when export_history returns non-JSON, do not lie with success=True."""
        mock_monitor.export_history = MagicMock(return_value="not json {{{")
        mock_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.introspection.export.mcp_server", mock_server), \
             patch("src.mcp_handlers.introspection.export.require_registered_agent", return_value=("agent-1", None)), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value="agent-1"):

            from src.mcp_handlers.introspection.export import handle_get_system_history
            result = await handle_get_system_history({"format": "json"})
            data = _parse(result)
            assert data.get("success") is False
            assert data.get("error_code") == "EXPORT_MALFORMED"


# ============================================================================
# handle_export_to_file (export.py)
# ============================================================================

class TestExportToFile:
    """Tests for export_to_file handler."""

    @pytest.fixture
    def mock_server(self):
        return _make_mock_mcp_server()

    @pytest.fixture
    def mock_monitor(self):
        return _make_monitor(
            E_history=[0.7, 0.75],
            V_history=[0.0, 0.0],
        )

    @pytest.mark.asyncio
    async def test_requires_registered_agent(self, mock_server):
        """Without registered agent returns error."""
        error_tc = _make_error_text_content("not registered")

        with patch("src.mcp_handlers.introspection.export.mcp_server", mock_server), \
             patch("src.mcp_handlers.introspection.export.require_registered_agent", return_value=(None, error_tc)):

            from src.mcp_handlers.introspection.export import handle_export_to_file
            result = await handle_export_to_file({})
            data = _parse(result)
            assert "not registered" in json.dumps(data)

    @pytest.mark.asyncio
    async def test_export_json_history(self, mock_server, mock_monitor, tmp_path):
        """Exports JSON history file successfully."""
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.project_root = str(tmp_path)

        with patch("src.mcp_handlers.introspection.export.mcp_server", mock_server), \
             patch("src.mcp_handlers.introspection.export.require_registered_agent", return_value=("agent-1", None)):

            from src.mcp_handlers.introspection.export import handle_export_to_file
            result = await handle_export_to_file({
                "format": "json",
                "filename": "test_export",
            })

            data = _parse(result)
            assert data.get("format") == "json"
            assert "file_path" in data
            assert data.get("agent_id") == "agent-1"

    @pytest.mark.asyncio
    async def test_export_csv_history(self, mock_server, mock_monitor, tmp_path):
        """Exports CSV history file successfully."""
        mock_monitor.export_history.return_value = "E,I,S,V\n0.7,0.6,0.2,0.0\n"
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.project_root = str(tmp_path)

        with patch("src.mcp_handlers.introspection.export.mcp_server", mock_server), \
             patch("src.mcp_handlers.introspection.export.require_registered_agent", return_value=("agent-1", None)):

            from src.mcp_handlers.introspection.export import handle_export_to_file
            result = await handle_export_to_file({
                "format": "csv",
                "filename": "test_csv",
            })

            data = _parse(result)
            assert data.get("format") == "csv"

    @pytest.mark.asyncio
    async def test_complete_package_export(self, mock_server, mock_monitor, tmp_path):
        """Complete package includes metadata + history + validation."""
        meta = _make_metadata()
        mock_server.agent_metadata = {"agent-1": meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.project_root = str(tmp_path)

        with patch("src.mcp_handlers.introspection.export.mcp_server", mock_server), \
             patch("src.mcp_handlers.introspection.export.require_registered_agent", return_value=("agent-1", None)):

            from src.mcp_handlers.introspection.export import handle_export_to_file
            result = await handle_export_to_file({
                "format": "json",
                "complete_package": True,
                "filename": "test_complete",
            })

            data = _parse(result)
            assert data.get("complete_package") is True
            assert "layers_included" in data

    @pytest.mark.asyncio
    async def test_complete_package_csv_not_supported(self, mock_server, mock_monitor):
        """CSV format not supported for complete package."""
        meta = _make_metadata()
        mock_server.agent_metadata = {"agent-1": meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.introspection.export.mcp_server", mock_server), \
             patch("src.mcp_handlers.introspection.export.require_registered_agent", return_value=("agent-1", None)):

            from src.mcp_handlers.introspection.export import handle_export_to_file
            result = await handle_export_to_file({
                "format": "csv",
                "complete_package": True,
            })

            data = _parse(result)
            assert "error" in data or "not supported" in json.dumps(data).lower()

    @pytest.mark.asyncio
    async def test_write_failure_returns_error(self, mock_server, mock_monitor, tmp_path):
        """File write failure returns informative error."""
        mock_server.get_or_create_monitor.return_value = mock_monitor
        # Set project_root to a path whose parent is a regular file — mkdir/write
        # under it fails with ENOTDIR even when running as root (a bare
        # "/nonexistent/..." path is creatable by root, so it doesn't inject a failure)
        blocker = tmp_path / "blocker"
        blocker.write_text("")
        mock_server.project_root = str(blocker / "not-a-dir")

        with patch("src.mcp_handlers.introspection.export.mcp_server", mock_server), \
             patch("src.mcp_handlers.introspection.export.require_registered_agent", return_value=("agent-1", None)):

            from src.mcp_handlers.introspection.export import handle_export_to_file
            result = await handle_export_to_file({
                "format": "json",
                "filename": "will_fail",
            })

            data = _parse(result)
            assert "error" in data or "Failed" in json.dumps(data)

    @pytest.mark.asyncio
    async def test_auto_generated_filename(self, mock_server, mock_monitor, tmp_path):
        """When no filename provided, auto-generates one with timestamp."""
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.project_root = str(tmp_path)

        with patch("src.mcp_handlers.introspection.export.mcp_server", mock_server), \
             patch("src.mcp_handlers.introspection.export.require_registered_agent", return_value=("agent-1", None)):

            from src.mcp_handlers.introspection.export import handle_export_to_file
            result = await handle_export_to_file({"format": "json"})

            data = _parse(result)
            assert "filename" in data
            assert "agent-1" in data["filename"]
            assert "history" in data["filename"]


# ============================================================================
# handle_mark_response_complete (lifecycle.py)
# ============================================================================

class TestMarkResponseComplete:
    """Tests for mark_response_complete handler."""

    @pytest.fixture
    def mock_server(self):
        return _make_mock_mcp_server()

    @pytest.mark.asyncio
    async def test_requires_registered_agent(self, mock_server):
        """Without registered agent returns error."""
        error_tc = _make_error_text_content("not registered")

        with patch_lifecycle_server(mock_server, require_registered=(None, error_tc)):

            from src.mcp_handlers.lifecycle.handlers import handle_mark_response_complete
            result = await handle_mark_response_complete({})
            data = _parse(result)
            assert "not registered" in json.dumps(data)

    @pytest.mark.asyncio
    async def test_requires_ownership(self, mock_server):
        """Agent must own the session (verify_agent_ownership)."""
        meta = _make_metadata()
        mock_server.agent_metadata = {"agent-1": meta}

        with patch_lifecycle_server(mock_server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=False):

            from src.mcp_handlers.lifecycle.handlers import handle_mark_response_complete
            result = await handle_mark_response_complete({})
            data = _parse(result)
            assert "error" in data or "auth" in json.dumps(data).lower()

    @pytest.mark.asyncio
    async def test_successful_mark_complete(self, mock_server):
        """Happy path: marks response as complete."""
        meta = _make_metadata()
        mock_server.agent_metadata = {"agent-1": meta}

        with patch_lifecycle_server(mock_server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True), \
             patch("src.mcp_handlers.lifecycle.handlers.agent_storage", MagicMock(
                 update_agent=AsyncMock(),
                 persist_runtime_state=AsyncMock(),
             )):

            from src.mcp_handlers.lifecycle.handlers import handle_mark_response_complete
            import src.mcp_handlers.lifecycle.operations as _lo; _lo.agent_storage = __import__("sys").modules["src.mcp_handlers.lifecycle.handlers"].agent_storage
            import src.mcp_handlers.lifecycle.mutation as _lm; _lm.agent_storage = __import__("sys").modules["src.mcp_handlers.lifecycle.handlers"].agent_storage
            result = await handle_mark_response_complete({})
            data = _parse(result)
            assert data.get("status") == "waiting_input"
            assert data.get("response_completed") is True
            assert meta.status == "waiting_input"

    @pytest.mark.asyncio
    async def test_includes_summary_in_lifecycle(self, mock_server):
        """Summary argument is recorded in lifecycle event."""
        meta = _make_metadata()
        mock_server.agent_metadata = {"agent-1": meta}

        with patch_lifecycle_server(mock_server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True), \
             patch("src.mcp_handlers.lifecycle.handlers.agent_storage", MagicMock(
                 update_agent=AsyncMock(),
                 persist_runtime_state=AsyncMock(),
             )):

            from src.mcp_handlers.lifecycle.handlers import handle_mark_response_complete
            import src.mcp_handlers.lifecycle.operations as _lo; _lo.agent_storage = __import__("sys").modules["src.mcp_handlers.lifecycle.handlers"].agent_storage
            import src.mcp_handlers.lifecycle.mutation as _lm; _lm.agent_storage = __import__("sys").modules["src.mcp_handlers.lifecycle.handlers"].agent_storage
            result = await handle_mark_response_complete({"summary": "Done with tests"})

            meta.add_lifecycle_event.assert_called_once_with(
                "response_completed", "Done with tests"
            )

    @pytest.mark.asyncio
    async def test_postgres_failure_returns_error_without_mutating_meta(self, mock_server):
        """On persist failure, return PERSIST_FAILED and keep in-memory meta unchanged.

        Prevents the mutation-before-persistence divergence Watcher P011 flagged:
        if the in-memory mutation ran but the DB write failed, the next metadata
        load would clobber the in-memory status back to its persisted value.
        """
        meta = _make_metadata()
        original_status = meta.status
        mock_server.agent_metadata = {"agent-1": meta}

        failing_storage = MagicMock()
        failing_storage.update_agent = AsyncMock(side_effect=Exception("PG down"))
        failing_storage.persist_runtime_state = AsyncMock()

        with patch_lifecycle_server(mock_server, require_registered=("agent-1", None)), \
             patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True), \
             patch("src.mcp_handlers.lifecycle.handlers.agent_storage", failing_storage), \
             patch("src.mcp_handlers.lifecycle.operations.agent_storage", failing_storage), \
             patch("src.mcp_handlers.lifecycle.mutation.agent_storage", failing_storage):

            from src.mcp_handlers.lifecycle.handlers import handle_mark_response_complete
            result = await handle_mark_response_complete({})
            data = _parse(result)
            assert "PERSIST_FAILED" in result[0].text
            assert data.get("status") != "waiting_input"  # error, not status update
            assert meta.status == original_status  # in-memory unchanged
            meta.add_lifecycle_event.assert_not_called()


# ============================================================================
# Edge cases & integration-like tests
# ============================================================================

@pytest.mark.usefixtures("bound_context")
class TestEdgeCases:
    """Edge cases and cross-cutting concerns."""

    def test_parse_helper_with_list(self):
        """_parse works with list result."""
        tc = TextContent(type="text", text='{"key": "value"}')
        assert _parse([tc]) == {"key": "value"}

    def test_parse_helper_with_single(self):
        """_parse works with single TextContent."""
        tc = TextContent(type="text", text='{"key": "value"}')
        assert _parse(tc) == {"key": "value"}

    @pytest.mark.asyncio
    async def test_get_metrics_saturation_diagnostics_failure_handled(self):
        """Failure to compute saturation diagnostics is handled gracefully.

        The saturation diagnostics import is inside a try/except in the handler,
        so we test by setting unitaires_state=None which skips the computation.
        """
        mock_server = _make_mock_mcp_server()
        monitor = _make_monitor()
        # unitaires_state is None by default, so diagnostics are skipped
        mock_server.agent_metadata = {"agent-1": _make_metadata()}
        mock_server.get_or_create_monitor.return_value = monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:

            MockClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": False})
            data = _parse(result)
            # Should still return valid data without saturation_diagnostics key
            assert data["success"] is True
            assert "saturation_diagnostics" not in data

    @pytest.mark.asyncio
    async def test_simulate_with_explicit_confidence(self):
        """Simulate passes explicit confidence to monitor."""
        mock_server = _make_mock_mcp_server()
        monitor = _make_monitor()

        # Patch at source module since handle_simulate_update does local import
        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.governance_monitor.UNITARESMonitor", return_value=monitor), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=(None, None)):

            from src.mcp_handlers.core import handle_simulate_update
            result = await handle_simulate_update({
                "complexity": 0.5,
                "confidence": 0.8,
            })

            # Verify confidence=0.8 was passed
            monitor.simulate_update.assert_called_once()
            call_kwargs = monitor.simulate_update.call_args
            assert call_kwargs[1]["confidence"] == 0.8

    @pytest.mark.asyncio
    async def test_simulate_dialectic_condition_parsing_failure(self):
        """Dialectic condition parsing failure is non-blocking."""
        mock_server = _make_mock_mcp_server()
        monitor = _make_monitor()

        # Create meta with bad dialectic_conditions that will cause parsing error
        meta = _make_metadata(dialectic_conditions="not a list")
        mock_server.agent_metadata = {"agent-1": meta}
        mock_server.get_or_create_monitor.return_value = monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)):

            from src.mcp_handlers.core import handle_simulate_update
            # Should not crash
            result = await handle_simulate_update({"complexity": 0.5})
            data = _parse(result)
            assert data["simulation"] is True

    @pytest.mark.asyncio
    async def test_get_metrics_with_zero_void(self):
        """Zero void displays as 0.0."""
        mock_server = _make_mock_mcp_server()
        monitor = _make_monitor()
        monitor.get_metrics.return_value = {
            "E": 0.7, "I": 0.6, "S": 0.2, "V": 0,
            "coherence": 0.52, "risk_score": 0.3,
            "initialized": True, "status": "ok",
            "complexity": 0.5,
        }
        mock_server.agent_metadata = {"agent-1": _make_metadata()}
        mock_server.get_or_create_monitor.return_value = monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:

            MockClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": True})
            data = _parse(result)
            assert data["V"]["value"] == 0.0

    @pytest.mark.asyncio
    async def test_get_metrics_none_coherence_shows_unknown(self):
        """None coherence shows as unknown status."""
        mock_server = _make_mock_mcp_server()
        monitor = _make_monitor()
        monitor.get_metrics.return_value = {
            "E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0,
            "coherence": None, "risk_score": 0.3,
            "initialized": True, "status": "ok",
            "complexity": 0.5,
        }
        mock_server.agent_metadata = {"agent-1": _make_metadata()}
        mock_server.get_or_create_monitor.return_value = monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:

            MockClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": True})
            data = _parse(result)
            assert "unknown" in data["coherence"]["status"]

    @pytest.mark.asyncio
    async def test_get_metrics_high_risk_shows_warning(self):
        """High risk score shows as high status."""
        mock_server = _make_mock_mcp_server()
        monitor = _make_monitor()
        monitor.get_metrics.return_value = {
            "E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0,
            "coherence": 0.52, "risk_score": 0.85,
            "initialized": True, "status": "ok",
            "complexity": 0.5,
        }
        mock_server.agent_metadata = {"agent-1": _make_metadata()}
        mock_server.get_or_create_monitor.return_value = monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:

            MockClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": True})
            data = _parse(result)
            assert "high" in data["risk_score"]["status"]

    @pytest.mark.asyncio
    async def test_get_metrics_medium_risk(self):
        """Medium risk score shows correct status."""
        mock_server = _make_mock_mcp_server()
        monitor = _make_monitor()
        monitor.get_metrics.return_value = {
            "E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0,
            "coherence": 0.52, "risk_score": 0.6,
            "initialized": True, "status": "ok",
            "complexity": 0.5,
        }
        mock_server.agent_metadata = {"agent-1": _make_metadata()}
        mock_server.get_or_create_monitor.return_value = monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:

            MockClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": True})
            data = _parse(result)
            assert "medium" in data["risk_score"]["status"]

    @pytest.mark.asyncio
    async def test_get_metrics_low_coherence(self):
        """Low coherence shows red status."""
        mock_server = _make_mock_mcp_server()
        monitor = _make_monitor()
        monitor.get_metrics.return_value = {
            "E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0,
            "coherence": 0.40, "risk_score": 0.3,
            "initialized": True, "status": "ok",
            "complexity": 0.5,
        }
        mock_server.agent_metadata = {"agent-1": _make_metadata()}
        mock_server.get_or_create_monitor.return_value = monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:

            MockClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": True})
            data = _parse(result)
            assert "low" in data["coherence"]["status"]

    @pytest.mark.asyncio
    async def test_get_metrics_moderate_coherence(self):
        """Moderate coherence (0.45-0.50) shows yellow status."""
        mock_server = _make_mock_mcp_server()
        monitor = _make_monitor()
        monitor.get_metrics.return_value = {
            "E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0,
            "coherence": 0.47, "risk_score": 0.3,
            "initialized": True, "status": "ok",
            "complexity": 0.5,
        }
        mock_server.agent_metadata = {"agent-1": _make_metadata()}
        mock_server.get_or_create_monitor.return_value = monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:

            MockClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": True})
            data = _parse(result)
            assert "moderate" in data["coherence"]["status"]

    @pytest.mark.asyncio
    async def test_get_metrics_good_coherence(self):
        """Good coherence (>=0.50) shows green status."""
        mock_server = _make_mock_mcp_server()
        monitor = _make_monitor()
        monitor.get_metrics.return_value = {
            "E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0,
            "coherence": 0.55, "risk_score": 0.3,
            "initialized": True, "status": "ok",
            "complexity": 0.5,
        }
        mock_server.agent_metadata = {"agent-1": _make_metadata()}
        mock_server.get_or_create_monitor.return_value = monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:

            MockClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": True})
            data = _parse(result)
            assert "good" in data["coherence"]["status"]

    @pytest.mark.asyncio
    async def test_get_metrics_none_risk_shows_unknown(self):
        """None risk_score shows as unknown."""
        mock_server = _make_mock_mcp_server()
        monitor = _make_monitor()
        monitor.get_metrics.return_value = {
            "E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0,
            "coherence": 0.52, "risk_score": None,
            "initialized": True, "status": "ok",
            "complexity": 0.5,
        }
        mock_server.agent_metadata = {"agent-1": _make_metadata()}
        mock_server.get_or_create_monitor.return_value = monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:

            MockClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": True})
            data = _parse(result)
            # None risk_score means unknown status (not high)
            assert "unknown" in data["risk_score"]["status"]

    @pytest.mark.asyncio
    async def test_get_metrics_includes_mode_and_basin_in_lite(self):
        """Lite mode includes mode and basin from interpreted state."""
        mock_server = _make_mock_mcp_server()
        monitor = _make_monitor()
        mock_server.agent_metadata = {"agent-1": _make_metadata()}
        mock_server.get_or_create_monitor.return_value = monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:

            MockClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": True})
            data = _parse(result)
            # mode and basin are wrapped with glossary entries (#428) — the
            # raw value lives at .value, with peer keys "meaning" /
            # "next_action" / range etc. when known. Unknown values still
            # surface .value with a "meaning: unknown..." fallback.
            assert data.get("mode", {}).get("value") == "convergent"
            assert data.get("basin", {}).get("value") == "stable"


# ============================================================================
# Conditional Reflection
# ============================================================================

class TestGenerateContextualReflection:
    """Tests for _generate_contextual_reflection()."""

    def test_uninitialized_returns_first_checkin_message(self):
        from src.services.runtime_queries import _generate_contextual_reflection
        result = _generate_contextual_reflection(
            {"initialized": False, "status": "uninitialized"},
            {}
        )
        assert result is not None
        assert "First check-in" in result

    def test_guide_verdict_returns_reflection(self):
        from src.services.runtime_queries import _generate_contextual_reflection
        result = _generate_contextual_reflection(
            {"initialized": True, "verdict": "guide", "S": 0.1},
            {"state": {}}
        )
        assert result is not None
        assert "guide" in result

    def test_pause_verdict_returns_reflection(self):
        from src.services.runtime_queries import _generate_contextual_reflection
        result = _generate_contextual_reflection(
            {"initialized": True, "verdict": "pause", "S": 0.1},
            {"state": {}}
        )
        assert "pause" in result

    def test_basin_boundary_returns_reflection(self):
        from src.services.runtime_queries import _generate_contextual_reflection
        result = _generate_contextual_reflection(
            {"initialized": True, "verdict": "proceed", "S": 0.1},
            {"state": {"borderline": {"S": {"value": 0.28}}}}
        )
        assert result is not None
        assert "basin boundary" in result

    def test_high_entropy_returns_reflection(self):
        from src.services.runtime_queries import _generate_contextual_reflection
        result = _generate_contextual_reflection(
            {"initialized": True, "verdict": "proceed", "S": 0.45},
            {"state": {}}
        )
        assert result is not None
        assert "Entropy" in result
        assert "0.45" in result

    def test_healthy_state_returns_none(self):
        from src.services.runtime_queries import _generate_contextual_reflection
        result = _generate_contextual_reflection(
            {"initialized": True, "verdict": "proceed", "S": 0.15},
            {"state": {}}
        )
        assert result is None


# ============================================================================
# Verbosity Tiers
# ============================================================================

@pytest.mark.usefixtures("bound_context")
class TestVerbosityTiers:
    """Tests for the verbosity parameter on get_governance_metrics."""

    @pytest.fixture
    def mock_server(self):
        server = MagicMock()
        server.agent_metadata = {"agent-1": _make_metadata()}
        server.get_or_create_monitor.return_value = _make_monitor()
        return server

    @pytest.mark.asyncio
    async def test_standard_verbosity_returns_key_fields(self, mock_server):
        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:
            MockClass.get_eisv_labels.return_value = {}
            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"verbosity": "standard"})
            data = _parse(result)

        assert data["success"] is True
        # Key fields present
        for key in ("E", "I", "S", "V", "coherence", "verdict", "risk_score", "basin", "summary"):
            assert key in data, f"Missing key: {key}"
        # Diagnostic fields absent
        for key in ("saturation_diagnostics", "stability", "calibration_feedback", "thresholds", "eisv_labels"):
            assert key not in data, f"Unexpected key: {key}"
        assert data.get("_note") is not None

    @pytest.mark.asyncio
    async def test_lite_backward_compat(self, mock_server):
        """lite=true still works and maps to minimal verbosity."""
        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:
            MockClass.get_eisv_labels.return_value = {}
            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": True})
            data = _parse(result)

        # Minimal mode has structured EISV (dicts with value/range)
        assert isinstance(data.get("E"), dict)

    @pytest.mark.asyncio
    async def test_verbosity_full(self, mock_server):
        """verbosity=full returns diagnostic fields."""
        with patch("src.mcp_handlers.core.mcp_server", mock_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockClass:
            MockClass.get_eisv_labels.return_value = {}
            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"verbosity": "full"})
            data = _parse(result)

        # Full mode has raw EISV values (not dicts)
        assert isinstance(data.get("E"), (int, float))
        assert "summary" in data


# ============================================================================
# EXTENDED COVERAGE: process_agent_update deeper paths
# ============================================================================
