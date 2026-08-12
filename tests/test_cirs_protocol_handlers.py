"""
Comprehensive tests for src/mcp_handlers/cirs_protocol.py (~1107 lines).

Tests the CIRS (Collaborative Issue Resolution System) protocol handlers:
1. handle_void_alert - Broadcast/query void state alerts
2. handle_state_announce - Broadcast/query EISV + trajectory state
3. handle_coherence_report - Compute pairwise agent similarity
4. handle_boundary_contract - Declare trust policies and void response rules
5. handle_governance_action - Coordinate interventions across agents
6. handle_cirs_protocol - Unified entry point dispatching to the above

Also tests:
- maybe_emit_void_alert (auto-emit hook)
- auto_emit_state_announce (auto-emit hook)
- Helper/utility functions (_compute_decision_bias, _compute_maturity, etc.)
- Data structures (VoidAlert, StateAnnounce, CoherenceReport, etc.)

All external dependencies (MCP server, monitors, agent metadata) are mocked.
No PostgreSQL or real server required.
"""

import pytest
import json
import sys
from pathlib import Path
from contextlib import ExitStack, contextmanager
from unittest.mock import patch, MagicMock, AsyncMock
from types import SimpleNamespace
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

# Ensure project root is on sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from mcp.types import TextContent

# Module under test (facade)
MODULE = "src.mcp_handlers.cirs.protocol"

# Sub-modules where mcp_server / require_registered_agent are looked up at runtime
_CIRS_MODULES_WITH_MCP_SERVER = [
    "src.mcp_handlers.cirs.void",
    "src.mcp_handlers.cirs.state",
    "src.mcp_handlers.cirs.hooks",
    "src.mcp_handlers.cirs.coherence",
    "src.mcp_handlers.cirs.governance_action",
]
_CIRS_MODULES_WITH_REQUIRE = [
    "src.mcp_handlers.cirs.void",
    "src.mcp_handlers.cirs.state",
    "src.mcp_handlers.cirs.coherence",
    "src.mcp_handlers.cirs.boundary",
    "src.mcp_handlers.cirs.governance_action",
    "src.mcp_handlers.cirs.resonance",
]


# ============================================================================
# Helpers
# ============================================================================

from tests.helpers import parse_result


def _make_mock_server(agents=None, monitors=None):
    """Create a mock mcp_server with agent_metadata and monitors."""
    mock = MagicMock()
    mock.agent_metadata = agents or {}
    mock.monitors = monitors or {}

    # get_or_create_monitor returns a mock monitor
    def _get_or_create(agent_id):
        if agent_id not in mock.monitors:
            mock.monitors[agent_id] = _make_monitor(agent_id)
        return mock.monitors[agent_id]

    mock.get_or_create_monitor = MagicMock(side_effect=_get_or_create)
    mock.load_monitor_state = MagicMock(return_value=None)
    return mock


def _make_agent_meta(status="active", label="TestAgent", api_key="key123"):
    """Create a SimpleNamespace mimicking agent metadata."""
    return SimpleNamespace(
        status=status,
        label=label,
        api_key=api_key,
        last_update=datetime.now().isoformat(),
        paused_at=None,
        structured_id=None,
        purpose=None,
        trust_tier=None,
        display_name=label,
    )


def _make_monitor_state(
    V=0.05,
    coherence=0.7,
    lambda1=1.0,
    update_count=10,
    void_active=False,
    regime="convergence",
    task_type="mixed",
):
    """Create a mock monitor state object."""
    state = SimpleNamespace(
        V=V,
        coherence=coherence,
        lambda1=lambda1,
        update_count=update_count,
        void_active=void_active,
        regime=regime,
        task_type=task_type,
        V_history=[0.01, 0.02, 0.03, 0.04, V],
        coherence_history=[0.6, 0.65, 0.7, 0.72, coherence],
        decision_history=["proceed", "proceed", "pause", "proceed"],
        S_history=[0.3, 0.28, 0.25, 0.22, 0.2],
        risk_history=[0.2, 0.22, 0.21, 0.19, 0.18],
    )
    return state


def _make_monitor(agent_id="agent-1", **state_kwargs):
    """Create a mock monitor object with state and metrics."""
    state = _make_monitor_state(**state_kwargs)
    monitor = MagicMock()
    monitor.agent_id = agent_id
    monitor.state = state
    monitor.get_metrics = MagicMock(return_value={
        "E": 0.7,
        "I": 0.8,
        "S": 0.2,
        "V": float(state.V),
        "coherence": float(state.coherence),
        "risk_score": 0.3,
        "current_risk": 0.3,
        "regime": state.regime,
        "phi": 0.5,
        "verdict": "safe",
        "updates": state.update_count,
        "void_frequency": 0.1,
    })
    return monitor


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def clear_buffers():
    """Clear all in-memory CIRS buffers between tests."""
    from src.mcp_handlers.cirs.protocol import (
        _void_alert_buffer,
        _state_announce_buffer,
        _coherence_report_buffer,
        _boundary_contract_buffer,
        _governance_action_buffer,
    )
    _void_alert_buffer.clear()
    _state_announce_buffer.clear()
    _coherence_report_buffer.clear()
    _boundary_contract_buffer.clear()
    _governance_action_buffer.clear()
    yield
    _void_alert_buffer.clear()
    _state_announce_buffer.clear()
    _coherence_report_buffer.clear()
    _boundary_contract_buffer.clear()
    _governance_action_buffer.clear()


@pytest.fixture
def mock_server():
    """Provide a mock mcp_server with two active agents."""
    server = _make_mock_server(
        agents={
            "agent-1": _make_agent_meta(status="active", label="Agent1"),
            "agent-2": _make_agent_meta(status="active", label="Agent2"),
            "agent-paused": _make_agent_meta(status="paused", label="PausedAgent"),
        },
        monitors={
            "agent-1": _make_monitor("agent-1"),
            "agent-2": _make_monitor("agent-2", V=0.1, coherence=0.5),
        },
    )
    with ExitStack() as stack:
        for mod in _CIRS_MODULES_WITH_MCP_SERVER:
            stack.enter_context(patch(f"{mod}.mcp_server", server))
        yield server


@pytest.fixture
def patch_require_registered():
    """Factory fixture to mock require_registered_agent."""
    @contextmanager
    def _factory(agent_id="agent-1", error=None):
        with ExitStack() as stack:
            for mod in _CIRS_MODULES_WITH_REQUIRE:
                stack.enter_context(patch(
                    f"{mod}.require_registered_agent",
                    return_value=(agent_id, error),
                ))
            yield
    return _factory


# ============================================================================
# Data Structure Tests
# ============================================================================

class TestVoidAlert:
    """Tests for VoidAlert dataclass."""

    def test_to_dict(self):
        from src.mcp_handlers.cirs.protocol import VoidAlert, VoidSeverity
        alert = VoidAlert(
            agent_id="agent-1",
            timestamp="2026-01-01T00:00:00",
            severity=VoidSeverity.WARNING,
            V_snapshot=0.05,
            context_ref="test context",
            coherence_at_event=0.7,
            risk_at_event=0.3,
        )
        d = alert.to_dict()
        assert d["agent_id"] == "agent-1"
        assert d["severity"] == "warning"
        assert d["V_snapshot"] == 0.05
        assert d["context_ref"] == "test context"
        assert d["coherence_at_event"] == 0.7
        assert d["risk_at_event"] == 0.3

    def test_to_dict_optional_none(self):
        from src.mcp_handlers.cirs.protocol import VoidAlert, VoidSeverity
        alert = VoidAlert(
            agent_id="agent-1",
            timestamp="2026-01-01T00:00:00",
            severity=VoidSeverity.CRITICAL,
            V_snapshot=0.2,
        )
        d = alert.to_dict()
        assert d["context_ref"] is None
        assert d["coherence_at_event"] is None
        assert d["severity"] == "critical"


class TestStateAnnounce:
    """Tests for StateAnnounce dataclass."""

    def test_to_dict_basic(self):
        from src.mcp_handlers.cirs.protocol import StateAnnounce
        announce = StateAnnounce(
            agent_id="agent-1",
            timestamp="2026-01-01T00:00:00",
            eisv={"E": 0.7, "I": 0.8, "S": 0.2, "V": 0.0},
            coherence=0.7,
            regime="convergence",
            phi=0.5,
            verdict="safe",
            risk_score=0.3,
            update_count=10,
            coherence_source="legacy_tanh_v",
            coherence_role="ode_control_feedback",
        )
        d = announce.to_dict()
        assert d["agent_id"] == "agent-1"
        assert d["eisv"]["E"] == 0.7
        assert d["regime"] == "convergence"
        assert d["coherence_source"] == "legacy_tanh_v"
        assert d["coherence_role"] == "ode_control_feedback"
        assert "trajectory_signature" not in d
        assert "purpose" not in d

    def test_to_dict_with_trajectory(self):
        from src.mcp_handlers.cirs.protocol import StateAnnounce
        announce = StateAnnounce(
            agent_id="agent-1",
            timestamp="2026-01-01T00:00:00",
            eisv={"E": 0.7, "I": 0.8, "S": 0.2, "V": 0.0},
            coherence=0.7,
            regime="convergence",
            phi=0.5,
            verdict="safe",
            risk_score=0.3,
            trajectory_signature={"pi": {"regime": "convergence"}},
            purpose="Testing",
            trust_tier="full",
        )
        d = announce.to_dict()
        assert "trajectory_signature" in d
        assert d["purpose"] == "Testing"
        assert d["trust_tier"] == "full"


class TestCoherenceReport:
    """Tests for CoherenceReport dataclass."""

    def test_to_dict(self):
        from src.mcp_handlers.cirs.protocol import CoherenceReport
        report = CoherenceReport(
            source_agent_id="agent-1",
            timestamp="2026-01-01T00:00:00",
            target_agent_id="agent-2",
            similarity_score=0.75,
            eisv_similarity={"E": 0.9, "I": 0.8, "S": 0.7, "V": 0.6},
            regime_match=True,
            verdict_match=True,
            recommendation="High alignment",
        )
        d = report.to_dict()
        assert d["similarity_score"] == 0.75
        assert d["regime_match"] is True
        assert d["recommendation"] == "High alignment"

    def test_to_dict_without_optional(self):
        from src.mcp_handlers.cirs.protocol import CoherenceReport
        report = CoherenceReport(
            source_agent_id="agent-1",
            timestamp="2026-01-01T00:00:00",
            target_agent_id="agent-2",
            similarity_score=0.5,
            eisv_similarity={"E": 0.5, "I": 0.5, "S": 0.5, "V": 0.5},
            regime_match=False,
            verdict_match=False,
        )
        d = report.to_dict()
        assert "trajectory_similarity" not in d
        assert "recommendation" not in d


class TestBoundaryContract:
    """Tests for BoundaryContract dataclass."""

    def test_to_dict(self):
        from src.mcp_handlers.cirs.protocol import (
            BoundaryContract, TrustLevel, VoidResponsePolicy,
        )
        contract = BoundaryContract(
            agent_id="agent-1",
            timestamp="2026-01-01T00:00:00",
            trust_default=TrustLevel.PARTIAL,
            trust_overrides={"agent-2": "full"},
            void_response_policy=VoidResponsePolicy.NOTIFY,
            max_delegation_complexity=0.5,
            accept_coherence_threshold=0.4,
            boundary_violations=2,
        )
        d = contract.to_dict()
        assert d["trust_default"] == "partial"
        assert d["void_response_policy"] == "notify"
        assert d["boundary_violations"] == 2
        assert d["trust_overrides"] == {"agent-2": "full"}


class TestGovernanceAction:
    """Tests for GovernanceAction dataclass."""

    def test_to_dict(self):
        from src.mcp_handlers.cirs.protocol import (
            GovernanceAction, GovernanceActionType,
        )
        action = GovernanceAction(
            action_id="action-123",
            timestamp="2026-01-01T00:00:00",
            action_type=GovernanceActionType.VOID_INTERVENTION,
            initiator_agent_id="agent-1",
            target_agent_id="agent-2",
            payload={"context": "help"},
            status="pending",
        )
        d = action.to_dict()
        assert d["action_id"] == "action-123"
        assert d["action_type"] == "void_intervention"
        assert d["status"] == "pending"
        assert "response" not in d

    def test_to_dict_with_response(self):
        from src.mcp_handlers.cirs.protocol import (
            GovernanceAction, GovernanceActionType,
        )
        action = GovernanceAction(
            action_id="action-123",
            timestamp="2026-01-01T00:00:00",
            action_type=GovernanceActionType.DELEGATION_REQUEST,
            initiator_agent_id="agent-1",
            target_agent_id="agent-2",
            payload={},
            status="accepted",
            response={"accepted": True},
        )
        d = action.to_dict()
        assert d["response"] == {"accepted": True}


# ============================================================================
# Helper Function Tests
# ============================================================================

class TestHelperFunctions:
    """Tests for trajectory signature helper functions."""

    def test_compute_decision_bias_neutral(self):
        from src.mcp_handlers.cirs.protocol import _compute_decision_bias
        state = SimpleNamespace(decision_history=[])
        assert _compute_decision_bias(state) == "neutral"

    def test_compute_decision_bias_no_history(self):
        from src.mcp_handlers.cirs.protocol import _compute_decision_bias
        state = SimpleNamespace()
        assert _compute_decision_bias(state) == "neutral"

    def test_compute_decision_bias_proceed_bias(self):
        from src.mcp_handlers.cirs.protocol import _compute_decision_bias
        state = SimpleNamespace(
            decision_history=["proceed"] * 8 + ["pause"] * 2,
        )
        assert _compute_decision_bias(state) == "proceed_bias"

    def test_compute_decision_bias_pause_bias(self):
        from src.mcp_handlers.cirs.protocol import _compute_decision_bias
        state = SimpleNamespace(
            decision_history=["pause"] * 8 + ["proceed"] * 2,
        )
        assert _compute_decision_bias(state) == "pause_bias"

    def test_compute_decision_bias_balanced(self):
        from src.mcp_handlers.cirs.protocol import _compute_decision_bias
        state = SimpleNamespace(
            decision_history=["proceed", "pause"] * 5,
        )
        assert _compute_decision_bias(state) == "balanced"

    def test_compute_focus_stability_insufficient_data(self):
        from src.mcp_handlers.cirs.protocol import _compute_focus_stability
        state = SimpleNamespace(coherence_history=[0.5, 0.6])
        assert _compute_focus_stability(state) is None

    def test_compute_focus_stability_no_attr(self):
        from src.mcp_handlers.cirs.protocol import _compute_focus_stability
        state = SimpleNamespace()
        assert _compute_focus_stability(state) is None

    def test_compute_focus_stability_constant_signal_is_retired(self):
        from src.mcp_handlers.cirs.protocol import _compute_focus_stability
        state = SimpleNamespace(
            coherence_history=[0.7] * 10,
        )
        assert _compute_focus_stability(state) is None

    def test_compute_maturity_nascent(self):
        from src.mcp_handlers.cirs.protocol import _compute_maturity
        state = SimpleNamespace(update_count=2)
        assert _compute_maturity(state) == "nascent"

    def test_compute_maturity_developing(self):
        from src.mcp_handlers.cirs.protocol import _compute_maturity
        state = SimpleNamespace(update_count=10)
        assert _compute_maturity(state) == "developing"

    def test_compute_maturity_maturing(self):
        from src.mcp_handlers.cirs.protocol import _compute_maturity
        state = SimpleNamespace(update_count=30)
        assert _compute_maturity(state) == "maturing"

    def test_compute_maturity_mature(self):
        from src.mcp_handlers.cirs.protocol import _compute_maturity
        state = SimpleNamespace(update_count=100)
        assert _compute_maturity(state) == "mature"

    def test_compute_maturity_no_attr(self):
        from src.mcp_handlers.cirs.protocol import _compute_maturity
        state = SimpleNamespace()
        assert _compute_maturity(state) == "nascent"

    def test_compute_convergence_rate_insufficient_data(self):
        from src.mcp_handlers.cirs.protocol import _compute_convergence_rate
        state = SimpleNamespace(S_history=[0.3, 0.2])
        assert _compute_convergence_rate(state) == 0.0

    def test_compute_convergence_rate_no_attr(self):
        from src.mcp_handlers.cirs.protocol import _compute_convergence_rate
        state = SimpleNamespace()
        assert _compute_convergence_rate(state) == 0.0

    def test_compute_convergence_rate_decreasing_entropy(self):
        from src.mcp_handlers.cirs.protocol import _compute_convergence_rate
        # Entropy decreasing = positive convergence rate
        state = SimpleNamespace(S_history=[0.5, 0.45, 0.4, 0.35, 0.3])
        rate = _compute_convergence_rate(state)
        assert rate > 0  # Decreasing entropy -> positive convergence

    def test_compute_risk_trend_stable(self):
        from src.mcp_handlers.cirs.protocol import _compute_risk_trend
        state = SimpleNamespace(risk_history=[0.3, 0.3, 0.3, 0.3, 0.3])
        assert _compute_risk_trend(state) == "stable"

    def test_compute_risk_trend_increasing(self):
        from src.mcp_handlers.cirs.protocol import _compute_risk_trend
        state = SimpleNamespace(risk_history=[0.1, 0.2, 0.3, 0.5, 0.7])
        assert _compute_risk_trend(state) == "increasing"

    def test_compute_risk_trend_decreasing(self):
        from src.mcp_handlers.cirs.protocol import _compute_risk_trend
        state = SimpleNamespace(risk_history=[0.7, 0.5, 0.3, 0.2, 0.1])
        assert _compute_risk_trend(state) == "decreasing"

    def test_compute_risk_trend_insufficient_data(self):
        from src.mcp_handlers.cirs.protocol import _compute_risk_trend
        state = SimpleNamespace(risk_history=[0.3, 0.4])
        assert _compute_risk_trend(state) == "stable"

    def test_compute_risk_trend_no_attr(self):
        from src.mcp_handlers.cirs.protocol import _compute_risk_trend
        state = SimpleNamespace()
        assert _compute_risk_trend(state) == "stable"


# ============================================================================
# Auto-emit Hook Tests
# ============================================================================

class TestMaybeEmitVoidAlert:
    """Tests for maybe_emit_void_alert auto-emit hook."""

    def test_emit_on_void_transition(self):
        from src.mcp_handlers.cirs.protocol import maybe_emit_void_alert
        result = maybe_emit_void_alert(
            agent_id="agent-1",
            V=0.1,
            void_active=True,
            coherence=0.5,
            risk_score=0.3,
            previous_void_active=False,
        )
        assert result is not None
        assert result["agent_id"] == "agent-1"
        assert result["severity"] == "warning"

    def test_emit_critical_on_high_V(self):
        from src.mcp_handlers.cirs.protocol import maybe_emit_void_alert
        result = maybe_emit_void_alert(
            agent_id="agent-1",
            V=0.2,
            void_active=True,
            coherence=0.3,
            risk_score=0.7,
            previous_void_active=False,
        )
        assert result is not None
        assert result["severity"] == "critical"

    def test_no_emit_when_staying_in_void(self):
        from src.mcp_handlers.cirs.protocol import maybe_emit_void_alert
        result = maybe_emit_void_alert(
            agent_id="agent-1",
            V=0.1,
            void_active=True,
            coherence=0.5,
            risk_score=0.3,
            previous_void_active=True,
        )
        assert result is None

    def test_no_emit_when_not_in_void(self):
        from src.mcp_handlers.cirs.protocol import maybe_emit_void_alert
        result = maybe_emit_void_alert(
            agent_id="agent-1",
            V=0.01,
            void_active=False,
            coherence=0.7,
            risk_score=0.1,
            previous_void_active=False,
        )
        assert result is None

    def test_no_emit_when_exiting_void(self):
        from src.mcp_handlers.cirs.protocol import maybe_emit_void_alert
        result = maybe_emit_void_alert(
            agent_id="agent-1",
            V=0.01,
            void_active=False,
            coherence=0.7,
            risk_score=0.1,
            previous_void_active=True,
        )
        assert result is None


class TestAutoEmitStateAnnounce:
    """Tests for auto_emit_state_announce."""

    def test_emit_on_5th_update(self):
        from src.mcp_handlers.cirs.protocol import auto_emit_state_announce
        metrics = {
            "E": 0.7, "I": 0.8, "S": 0.2, "V": 0.0,
            "coherence": 0.7, "regime": "convergence",
            "phi": 0.5, "verdict": "safe",
            "risk_score": 0.3, "updates": 5,
        }
        with patch("src.mcp_handlers.cirs.hooks.mcp_server", _make_mock_server()):
            result = auto_emit_state_announce("agent-1", metrics, None)
        assert result is not None
        assert result["agent_id"] == "agent-1"

    def test_emit_on_10th_update(self):
        from src.mcp_handlers.cirs.protocol import auto_emit_state_announce
        metrics = {
            "E": 0.7, "I": 0.8, "S": 0.2, "V": 0.0,
            "coherence": 0.7, "regime": "stable",
            "phi": 0.5, "verdict": "safe",
            "risk_score": 0.2, "updates": 10,
        }
        with patch("src.mcp_handlers.cirs.hooks.mcp_server", _make_mock_server()):
            result = auto_emit_state_announce("agent-1", metrics, None)
        assert result is not None

    def test_skip_non_5th_update(self):
        from src.mcp_handlers.cirs.protocol import auto_emit_state_announce
        metrics = {"updates": 3}
        result = auto_emit_state_announce("agent-1", metrics, None)
        assert result is None

    def test_emit_on_first_update(self):
        from src.mcp_handlers.cirs.protocol import auto_emit_state_announce
        metrics = {
            "E": 0.7, "I": 0.8, "S": 0.2, "V": 0.0,
            "coherence": 0.7, "regime": "divergence",
            "phi": 0.1, "verdict": "caution",
            "risk_score": 0.5, "updates": 1,
        }
        with patch("src.mcp_handlers.cirs.hooks.mcp_server", _make_mock_server()):
            result = auto_emit_state_announce("agent-1", metrics, None)
        # updates=1 -> 1 % 5 != 0 but update_count > 1 is False, so it emits
        assert result is not None

    def test_emit_on_zeroth_update(self):
        from src.mcp_handlers.cirs.protocol import auto_emit_state_announce
        metrics = {
            "E": 0.7, "I": 0.8, "S": 0.2, "V": 0.0,
            "coherence": 0.7, "regime": "divergence",
            "phi": 0.1, "verdict": "caution",
            "risk_score": 0.5, "updates": 0,
        }
        with patch("src.mcp_handlers.cirs.hooks.mcp_server", _make_mock_server()):
            result = auto_emit_state_announce("agent-1", metrics, None)
        # updates=0 -> 0 % 5 == 0, so it emits
        assert result is not None

    def test_handles_mcp_server_exception_gracefully(self):
        from src.mcp_handlers.cirs.protocol import auto_emit_state_announce
        # mcp_server.agent_metadata.get() failure is caught by inner try/except
        metrics = {"updates": 5, "E": 0.7, "I": 0.8, "S": 0.2, "V": 0.0,
                   "coherence": 0.7, "regime": "stable", "phi": 0.5,
                   "verdict": "safe", "risk_score": 0.2}
        broken_server = MagicMock()
        broken_server.agent_metadata = MagicMock()
        broken_server.agent_metadata.get = MagicMock(side_effect=Exception("boom"))
        with patch("src.mcp_handlers.cirs.hooks.mcp_server", broken_server):
            result = auto_emit_state_announce("agent-1", metrics, None)
        # Still succeeds because mcp_server access is only for trust_tier (inner try)
        assert result is not None
        assert result["agent_id"] == "agent-1"

    def test_handles_outer_exception_returns_none(self):
        from src.mcp_handlers.cirs.protocol import auto_emit_state_announce
        # Cause the outer try to fail by providing non-numeric data
        metrics = {"updates": 5, "E": "not_a_number"}
        with patch("src.mcp_handlers.cirs.hooks.mcp_server", _make_mock_server()):
            result = auto_emit_state_announce("agent-1", metrics, None)
        # float("not_a_number") raises ValueError, caught by outer try
        assert result is None


# ============================================================================
# VOID_ALERT Handler Tests
# ============================================================================

class TestHandleVoidAlert:
    """Tests for handle_void_alert handler."""

    @pytest.mark.asyncio
    async def test_missing_action(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_void_alert
        result = await handle_void_alert.__wrapped__({"no_action": True})
        data = parse_result(result)
        assert data["success"] is False
        assert "action" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_invalid_action(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_void_alert
        result = await handle_void_alert.__wrapped__({"action": "invalid"})
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_query_empty(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_void_alert
        result = await handle_void_alert.__wrapped__({"action": "query"})
        data = parse_result(result)
        assert data["success"] is True
        assert data["alerts"] == []
        assert data["summary"]["total_alerts"] == 0

    @pytest.mark.asyncio
    async def test_query_with_alerts(self, mock_server):
        from src.mcp_handlers.cirs.protocol import (
            handle_void_alert, _store_void_alert, VoidAlert, VoidSeverity,
        )
        # Pre-populate some alerts
        _store_void_alert(VoidAlert(
            agent_id="agent-1",
            timestamp=datetime.now().isoformat(),
            severity=VoidSeverity.WARNING,
            V_snapshot=0.05,
        ))
        _store_void_alert(VoidAlert(
            agent_id="agent-2",
            timestamp=datetime.now().isoformat(),
            severity=VoidSeverity.CRITICAL,
            V_snapshot=0.2,
        ))

        result = await handle_void_alert.__wrapped__({"action": "query"})
        data = parse_result(result)
        assert data["success"] is True
        assert data["summary"]["total_alerts"] == 2
        assert data["summary"]["by_severity"]["warning"] == 1
        assert data["summary"]["by_severity"]["critical"] == 1

    @pytest.mark.asyncio
    async def test_query_filter_by_agent(self, mock_server):
        from src.mcp_handlers.cirs.protocol import (
            handle_void_alert, _store_void_alert, VoidAlert, VoidSeverity,
        )
        _store_void_alert(VoidAlert(
            agent_id="agent-1",
            timestamp=datetime.now().isoformat(),
            severity=VoidSeverity.WARNING,
            V_snapshot=0.05,
        ))
        _store_void_alert(VoidAlert(
            agent_id="agent-2",
            timestamp=datetime.now().isoformat(),
            severity=VoidSeverity.CRITICAL,
            V_snapshot=0.2,
        ))

        result = await handle_void_alert.__wrapped__({
            "action": "query",
            "filter_agent_id": "agent-1",
        })
        data = parse_result(result)
        assert data["summary"]["total_alerts"] == 1
        assert data["alerts"][0]["agent_id"] == "agent-1"

    @pytest.mark.asyncio
    async def test_query_filter_by_severity(self, mock_server):
        from src.mcp_handlers.cirs.protocol import (
            handle_void_alert, _store_void_alert, VoidAlert, VoidSeverity,
        )
        _store_void_alert(VoidAlert(
            agent_id="agent-1",
            timestamp=datetime.now().isoformat(),
            severity=VoidSeverity.WARNING,
            V_snapshot=0.05,
        ))
        _store_void_alert(VoidAlert(
            agent_id="agent-2",
            timestamp=datetime.now().isoformat(),
            severity=VoidSeverity.CRITICAL,
            V_snapshot=0.2,
        ))

        result = await handle_void_alert.__wrapped__({
            "action": "query",
            "filter_severity": "critical",
        })
        data = parse_result(result)
        assert data["summary"]["total_alerts"] == 1
        assert data["alerts"][0]["severity"] == "critical"

    @pytest.mark.asyncio
    async def test_query_invalid_severity_filter(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_void_alert
        result = await handle_void_alert.__wrapped__({
            "action": "query",
            "filter_severity": "banana",
        })
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_emit_requires_registered_agent(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_void_alert
        err = TextContent(type="text", text=json.dumps({
            "success": False, "error": "Not registered",
        }))
        with patch("src.mcp_handlers.cirs.void.require_registered_agent", return_value=(None, err)):
            result = await handle_void_alert.__wrapped__({
                "action": "emit",
                "severity": "warning",
            })
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_emit_with_explicit_severity(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_void_alert
        with patch_require_registered("agent-1"):
            result = await handle_void_alert.__wrapped__({
                "action": "emit",
                "severity": "critical",
                "context_ref": "test emit",
            })
        data = parse_result(result)
        assert data["success"] is True
        assert data["alert"]["severity"] == "critical"
        assert data["cirs_protocol"] == "VOID_ALERT"

    @pytest.mark.asyncio
    async def test_emit_invalid_severity(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_void_alert
        with patch_require_registered("agent-1"):
            result = await handle_void_alert.__wrapped__({
                "action": "emit",
                "severity": "banana",
            })
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_emit_auto_detect_severity(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_void_alert
        # Set a high V value so auto-detection triggers
        monitor = mock_server.monitors["agent-1"]
        monitor.state.V = 0.3
        monitor.state.V_history = [0.1, 0.2, 0.3]

        with patch_require_registered("agent-1"):
            with patch(
                "config.governance_config.config.get_void_threshold",
                return_value=0.1,
            ):
                result = await handle_void_alert.__wrapped__({
                    "action": "emit",
                })
        data = parse_result(result)
        assert data["success"] is True
        # V=0.3, threshold=0.1, V > threshold*1.5 => critical
        assert data["alert"]["severity"] == "critical"

    @pytest.mark.asyncio
    async def test_emit_auto_detect_below_threshold(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_void_alert
        # Set a low V value
        monitor = mock_server.monitors["agent-1"]
        monitor.state.V = 0.01
        monitor.state.V_history = [0.01]

        with patch_require_registered("agent-1"):
            with patch(
                "config.governance_config.config.get_void_threshold",
                return_value=0.1,
            ):
                result = await handle_void_alert.__wrapped__({
                    "action": "emit",
                })
        data = parse_result(result)
        # V below threshold -> error saying V is below threshold
        assert data["success"] is False
        assert "threshold" in data["error"].lower() or "below" in data["error"].lower()


# ============================================================================
# STATE_ANNOUNCE Handler Tests
# ============================================================================

class TestHandleStateAnnounce:
    """Tests for handle_state_announce handler."""

    @pytest.mark.asyncio
    async def test_missing_action(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_state_announce
        result = await handle_state_announce.__wrapped__({})
        data = parse_result(result)
        assert data["success"] is False
        assert "action" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_invalid_action(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_state_announce
        result = await handle_state_announce.__wrapped__({"action": "bad"})
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_query_empty(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_state_announce
        result = await handle_state_announce.__wrapped__({"action": "query"})
        data = parse_result(result)
        assert data["success"] is True
        assert data["announcements"] == []
        assert data["summary"]["total_agents"] == 0

    @pytest.mark.asyncio
    async def test_query_with_announces(self, mock_server):
        from src.mcp_handlers.cirs.protocol import (
            handle_state_announce, _store_state_announce, StateAnnounce,
        )
        _store_state_announce(StateAnnounce(
            agent_id="agent-1",
            timestamp=datetime.now().isoformat(),
            eisv={"E": 0.7, "I": 0.8, "S": 0.2, "V": 0.0},
            coherence=0.7, regime="convergence", phi=0.5,
            verdict="safe", risk_score=0.2, update_count=10,
            coherence_source="legacy_tanh_v",
            coherence_role="ode_control_feedback",
        ))
        result = await handle_state_announce.__wrapped__({"action": "query"})
        data = parse_result(result)
        assert data["summary"]["total_agents"] == 1
        assert data["summary"]["coherence_role_counts"] == {
            "ode_control_feedback": 1
        }
        assert data["summary"]["avg_coherence_provenance"][
            "health_evidence"
        ] is False

    @pytest.mark.asyncio
    async def test_query_filter_by_regime(self, mock_server):
        from src.mcp_handlers.cirs.protocol import (
            handle_state_announce, _store_state_announce, StateAnnounce,
        )
        _store_state_announce(StateAnnounce(
            agent_id="agent-1",
            timestamp=datetime.now().isoformat(),
            eisv={"E": 0.7, "I": 0.8, "S": 0.2, "V": 0.0},
            coherence=0.7, regime="convergence", phi=0.5,
            verdict="safe", risk_score=0.2, update_count=10,
        ))
        _store_state_announce(StateAnnounce(
            agent_id="agent-2",
            timestamp=datetime.now().isoformat(),
            eisv={"E": 0.6, "I": 0.7, "S": 0.3, "V": 0.1},
            coherence=0.5, regime="divergence", phi=0.3,
            verdict="caution", risk_score=0.5, update_count=5,
        ))
        result = await handle_state_announce.__wrapped__({
            "action": "query",
            "regime": "convergence",
        })
        data = parse_result(result)
        assert data["summary"]["total_agents"] == 1
        assert data["announcements"][0]["regime"] == "convergence"

    @pytest.mark.asyncio
    async def test_query_invalid_regime(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_state_announce
        result = await handle_state_announce.__wrapped__({
            "action": "query",
            "regime": "banana",
        })
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_query_rejects_retired_min_coherence_filter(self, mock_server):
        from src.mcp_handlers.cirs.protocol import (
            handle_state_announce, _store_state_announce, StateAnnounce,
        )
        _store_state_announce(StateAnnounce(
            agent_id="agent-1",
            timestamp=datetime.now().isoformat(),
            eisv={"E": 0.7, "I": 0.8, "S": 0.2, "V": 0.0},
            coherence=0.9, regime="convergence", phi=0.5,
            verdict="safe", risk_score=0.1, update_count=50,
        ))
        _store_state_announce(StateAnnounce(
            agent_id="agent-2",
            timestamp=datetime.now().isoformat(),
            eisv={"E": 0.6, "I": 0.7, "S": 0.3, "V": 0.1},
            coherence=0.3, regime="divergence", phi=0.3,
            verdict="caution", risk_score=0.5, update_count=5,
        ))
        result = await handle_state_announce.__wrapped__({
            "action": "query",
            "min_coherence": 0.5,
        })
        data = parse_result(result)
        assert data["success"] is False
        assert data["error_code"] == "UNSUPPORTED_COHERENCE_FILTER"
        assert "retired" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_emit_requires_registered_agent(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_state_announce
        err = TextContent(type="text", text=json.dumps({
            "success": False, "error": "Not registered",
        }))
        with patch("src.mcp_handlers.cirs.state.require_registered_agent", return_value=(None, err)):
            result = await handle_state_announce.__wrapped__({"action": "emit"})
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_emit_happy_path(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_state_announce
        with patch_require_registered("agent-1"):
            result = await handle_state_announce.__wrapped__({
                "action": "emit",
                "include_trajectory": False,
            })
        data = parse_result(result)
        assert data["success"] is True
        assert data["announcement"]["agent_id"] == "agent-1"
        assert data["cirs_protocol"] == "STATE_ANNOUNCE"

    @pytest.mark.asyncio
    async def test_emit_with_trajectory(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_state_announce
        with patch_require_registered("agent-1"):
            result = await handle_state_announce.__wrapped__({
                "action": "emit",
                "include_trajectory": True,
            })
        data = parse_result(result)
        assert data["success"] is True
        announcement = data["announcement"]
        signature = announcement["trajectory_signature"]
        assert signature["alpha"]["focus_stability"] is None
        provenance = signature["alpha"]["focus_stability_provenance"]
        assert provenance["status"] == "retired"
        assert provenance["health_evidence"] is False

    @pytest.mark.asyncio
    async def test_emit_with_agent_purpose(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_state_announce
        # Set agent metadata with purpose
        mock_server.agent_metadata["agent-1"].purpose = "Testing purpose"
        mock_server.agent_metadata["agent-1"].trust_tier = "full"

        with patch_require_registered("agent-1"):
            result = await handle_state_announce.__wrapped__({
                "action": "emit",
                "include_trajectory": False,
            })
        data = parse_result(result)
        assert data["success"] is True
        assert data["announcement"].get("purpose") == "Testing purpose"
        assert data["announcement"].get("trust_tier") == "full"


# ============================================================================
# COHERENCE_REPORT Handler Tests
# ============================================================================

class TestHandleCoherenceReport:
    """Tests for handle_coherence_report handler."""

    @pytest.mark.asyncio
    async def test_missing_action(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_coherence_report
        result = await handle_coherence_report.__wrapped__({})
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_invalid_action(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_coherence_report
        result = await handle_coherence_report.__wrapped__({"action": "invalid"})
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_query_empty(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_coherence_report
        result = await handle_coherence_report.__wrapped__({"action": "query"})
        data = parse_result(result)
        assert data["success"] is True
        assert data["reports"] == []

    @pytest.mark.asyncio
    async def test_query_with_reports(self, mock_server):
        from src.mcp_handlers.cirs.protocol import (
            handle_coherence_report, _store_coherence_report, CoherenceReport,
        )
        _store_coherence_report(CoherenceReport(
            source_agent_id="agent-1",
            timestamp=datetime.now().isoformat(),
            target_agent_id="agent-2",
            similarity_score=0.75,
            eisv_similarity={"E": 0.9, "I": 0.8, "S": 0.7, "V": 0.6},
            regime_match=True,
            verdict_match=True,
        ))
        result = await handle_coherence_report.__wrapped__({"action": "query"})
        data = parse_result(result)
        assert data["summary"]["total_reports"] == 1

    @pytest.mark.asyncio
    async def test_query_filter_by_min_similarity(self, mock_server):
        from src.mcp_handlers.cirs.protocol import (
            handle_coherence_report, _store_coherence_report, CoherenceReport,
        )
        _store_coherence_report(CoherenceReport(
            source_agent_id="agent-1",
            timestamp=datetime.now().isoformat(),
            target_agent_id="agent-2",
            similarity_score=0.9,
            eisv_similarity={"E": 0.9, "I": 0.9, "S": 0.9, "V": 0.9},
            regime_match=True, verdict_match=True,
        ))
        _store_coherence_report(CoherenceReport(
            source_agent_id="agent-1",
            timestamp=datetime.now().isoformat(),
            target_agent_id="agent-3",
            similarity_score=0.3,
            eisv_similarity={"E": 0.3, "I": 0.3, "S": 0.3, "V": 0.3},
            regime_match=False, verdict_match=False,
        ))
        result = await handle_coherence_report.__wrapped__({
            "action": "query",
            "min_similarity": 0.5,
        })
        data = parse_result(result)
        assert data["summary"]["total_reports"] == 1

    @pytest.mark.asyncio
    async def test_compute_requires_registered_agent(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_coherence_report
        err = TextContent(type="text", text=json.dumps({
            "success": False, "error": "Not registered",
        }))
        with patch("src.mcp_handlers.cirs.coherence.require_registered_agent", return_value=(None, err)):
            result = await handle_coherence_report.__wrapped__({
                "action": "compute",
                "target_agent_id": "agent-2",
            })
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_compute_missing_target(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_coherence_report
        with patch_require_registered("agent-1"):
            result = await handle_coherence_report.__wrapped__({
                "action": "compute",
            })
        data = parse_result(result)
        assert data["success"] is False
        assert "target_agent_id" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_compute_target_not_found(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_coherence_report
        with patch_require_registered("agent-1"):
            result = await handle_coherence_report.__wrapped__({
                "action": "compute",
                "target_agent_id": "nonexistent",
            })
        data = parse_result(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_compute_happy_path(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_coherence_report
        with patch_require_registered("agent-1"):
            result = await handle_coherence_report.__wrapped__({
                "action": "compute",
                "target_agent_id": "agent-2",
            })
        data = parse_result(result)
        assert data["success"] is True
        assert "report" in data
        report = data["report"]
        assert report["source_agent_id"] == "agent-1"
        assert report["target_agent_id"] == "agent-2"
        assert 0 <= report["similarity_score"] <= 1
        assert "eisv_similarity" in report
        assert report["similarity_provenance"]["formula"] == "cirs_pairwise_similarity.v2"
        if "trajectory_similarity" in report:
            assert "coherence" not in report["trajectory_similarity"]
        assert data["cirs_protocol"] == "COHERENCE_REPORT"


# ============================================================================
# BOUNDARY_CONTRACT Handler Tests
# ============================================================================

class TestHandleBoundaryContract:
    """Tests for handle_boundary_contract handler."""

    @pytest.mark.asyncio
    async def test_missing_action(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_boundary_contract
        result = await handle_boundary_contract.__wrapped__({})
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_invalid_action(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_boundary_contract
        result = await handle_boundary_contract.__wrapped__({"action": "invalid"})
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_list_empty(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_boundary_contract
        result = await handle_boundary_contract.__wrapped__({"action": "list"})
        data = parse_result(result)
        assert data["success"] is True
        assert data["contracts"] == []
        assert data["summary"]["total_contracts"] == 0

    @pytest.mark.asyncio
    async def test_set_requires_registered_agent(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_boundary_contract
        err = TextContent(type="text", text=json.dumps({
            "success": False, "error": "Not registered",
        }))
        with patch("src.mcp_handlers.cirs.boundary.require_registered_agent", return_value=(None, err)):
            result = await handle_boundary_contract.__wrapped__({"action": "set"})
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_set_happy_path(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_boundary_contract
        with patch_require_registered("agent-1"):
            result = await handle_boundary_contract.__wrapped__({
                "action": "set",
                "trust_default": "partial",
                "void_response_policy": "assist",
                "max_delegation_complexity": 0.7,
                "accept_coherence_threshold": 0.5,
            })
        data = parse_result(result)
        assert data["success"] is True
        contract = data["contract"]
        assert contract["trust_default"] == "partial"
        assert contract["void_response_policy"] == "assist"
        assert contract["max_delegation_complexity"] == 0.7
        assert contract["accept_coherence_threshold"] == 0.5

    @pytest.mark.asyncio
    async def test_set_with_trust_overrides(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_boundary_contract
        with patch_require_registered("agent-1"):
            result = await handle_boundary_contract.__wrapped__({
                "action": "set",
                "trust_overrides": {"agent-2": "full", "agent-3": "none"},
            })
        data = parse_result(result)
        assert data["success"] is True
        assert data["contract"]["trust_overrides"]["agent-2"] == "full"
        assert data["contract"]["trust_overrides"]["agent-3"] == "none"

    @pytest.mark.asyncio
    async def test_set_invalid_trust_default(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_boundary_contract
        with patch_require_registered("agent-1"):
            result = await handle_boundary_contract.__wrapped__({
                "action": "set",
                "trust_default": "banana",
            })
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_set_invalid_trust_override_value(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_boundary_contract
        with patch_require_registered("agent-1"):
            result = await handle_boundary_contract.__wrapped__({
                "action": "set",
                "trust_overrides": {"agent-2": "banana"},
            })
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_set_invalid_void_policy(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_boundary_contract
        with patch_require_registered("agent-1"):
            result = await handle_boundary_contract.__wrapped__({
                "action": "set",
                "void_response_policy": "banana",
            })
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_set_preserves_violation_count(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import (
            handle_boundary_contract, _store_boundary_contract,
            BoundaryContract, TrustLevel, VoidResponsePolicy,
        )
        # Pre-store a contract with violations
        _store_boundary_contract(BoundaryContract(
            agent_id="agent-1",
            timestamp=datetime.now().isoformat(),
            trust_default=TrustLevel.FULL,
            trust_overrides={},
            void_response_policy=VoidResponsePolicy.NOTIFY,
            max_delegation_complexity=0.5,
            accept_coherence_threshold=0.4,
            boundary_violations=5,
        ))
        with patch_require_registered("agent-1"):
            result = await handle_boundary_contract.__wrapped__({
                "action": "set",
                "trust_default": "none",
            })
        data = parse_result(result)
        assert data["success"] is True
        assert data["contract"]["boundary_violations"] == 5
        assert data["contract"]["trust_default"] == "none"

    @pytest.mark.asyncio
    async def test_set_clamps_complexity(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_boundary_contract
        with patch_require_registered("agent-1"):
            result = await handle_boundary_contract.__wrapped__({
                "action": "set",
                "max_delegation_complexity": 2.0,
            })
        data = parse_result(result)
        assert data["success"] is True
        assert data["contract"]["max_delegation_complexity"] == 1.0

    @pytest.mark.asyncio
    async def test_get_missing_target(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_boundary_contract
        result = await handle_boundary_contract.__wrapped__({"action": "get"})
        data = parse_result(result)
        assert data["success"] is False
        assert "target_agent_id" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_get_not_found(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_boundary_contract
        result = await handle_boundary_contract.__wrapped__({
            "action": "get",
            "target_agent_id": "nonexistent",
        })
        data = parse_result(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower() or "no boundary" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_get_found(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import (
            handle_boundary_contract, _store_boundary_contract,
            BoundaryContract, TrustLevel, VoidResponsePolicy,
        )
        _store_boundary_contract(BoundaryContract(
            agent_id="agent-1",
            timestamp=datetime.now().isoformat(),
            trust_default=TrustLevel.PARTIAL,
            trust_overrides={},
            void_response_policy=VoidResponsePolicy.NOTIFY,
            max_delegation_complexity=0.5,
            accept_coherence_threshold=0.4,
        ))
        result = await handle_boundary_contract.__wrapped__({
            "action": "get",
            "target_agent_id": "agent-1",
        })
        data = parse_result(result)
        assert data["success"] is True
        assert data["contract"]["trust_default"] == "partial"

    @pytest.mark.asyncio
    async def test_list_with_contracts(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import (
            handle_boundary_contract, _store_boundary_contract,
            BoundaryContract, TrustLevel, VoidResponsePolicy,
        )
        _store_boundary_contract(BoundaryContract(
            agent_id="agent-1",
            timestamp=datetime.now().isoformat(),
            trust_default=TrustLevel.PARTIAL,
            trust_overrides={},
            void_response_policy=VoidResponsePolicy.NOTIFY,
            max_delegation_complexity=0.5,
            accept_coherence_threshold=0.4,
        ))
        _store_boundary_contract(BoundaryContract(
            agent_id="agent-2",
            timestamp=datetime.now().isoformat(),
            trust_default=TrustLevel.FULL,
            trust_overrides={},
            void_response_policy=VoidResponsePolicy.ASSIST,
            max_delegation_complexity=0.8,
            accept_coherence_threshold=0.3,
        ))
        result = await handle_boundary_contract.__wrapped__({"action": "list"})
        data = parse_result(result)
        assert data["success"] is True
        assert data["summary"]["total_contracts"] == 2
        assert data["summary"]["trust_distribution"]["partial"] == 1
        assert data["summary"]["trust_distribution"]["full"] == 1


# ============================================================================
# GOVERNANCE_ACTION Handler Tests
# ============================================================================

class TestHandleGovernanceAction:
    """Tests for handle_governance_action handler."""

    @pytest.mark.asyncio
    async def test_missing_action(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_governance_action
        result = await handle_governance_action.__wrapped__({})
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_invalid_action(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_governance_action
        result = await handle_governance_action.__wrapped__({"action": "invalid"})
        data = parse_result(result)
        assert data["success"] is False

    # --- INITIATE ---

    @pytest.mark.asyncio
    async def test_initiate_requires_registered_agent(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_governance_action
        err = TextContent(type="text", text=json.dumps({
            "success": False, "error": "Not registered",
        }))
        with patch("src.mcp_handlers.cirs.governance_action.require_registered_agent", return_value=(None, err)):
            result = await handle_governance_action.__wrapped__({
                "action": "initiate",
                "action_type": "void_intervention",
                "target_agent_id": "agent-2",
            })
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_initiate_missing_action_type(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_governance_action
        with patch_require_registered("agent-1"):
            result = await handle_governance_action.__wrapped__({
                "action": "initiate",
                "target_agent_id": "agent-2",
            })
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_initiate_invalid_action_type(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_governance_action
        with patch_require_registered("agent-1"):
            result = await handle_governance_action.__wrapped__({
                "action": "initiate",
                "action_type": "banana",
                "target_agent_id": "agent-2",
            })
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_initiate_missing_target(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_governance_action
        with patch_require_registered("agent-1"):
            result = await handle_governance_action.__wrapped__({
                "action": "initiate",
                "action_type": "coordination_sync",
            })
        data = parse_result(result)
        assert data["success"] is False
        assert "target_agent_id" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_initiate_happy_path(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_governance_action
        with patch_require_registered("agent-1"):
            result = await handle_governance_action.__wrapped__({
                "action": "initiate",
                "action_type": "coordination_sync",
                "target_agent_id": "agent-2",
                "payload": {"task": "sync up"},
            })
        data = parse_result(result)
        assert data["success"] is True
        ga = data["governance_action"]
        assert ga["action_type"] == "coordination_sync"
        assert ga["target_agent_id"] == "agent-2"
        assert ga["status"] == "pending"
        assert ga["payload"]["task"] == "sync up"

    @pytest.mark.asyncio
    async def test_initiate_void_intervention_adds_state(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_governance_action
        with patch_require_registered("agent-1"):
            result = await handle_governance_action.__wrapped__({
                "action": "initiate",
                "action_type": "void_intervention",
                "target_agent_id": "agent-2",
            })
        data = parse_result(result)
        assert data["success"] is True
        payload = data["governance_action"]["payload"]
        assert "initiator_state" in payload
        assert "coherence" in payload["initiator_state"]

    @pytest.mark.asyncio
    async def test_initiate_blocked_by_trust_none(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import (
            handle_governance_action, _store_boundary_contract,
            BoundaryContract, TrustLevel, VoidResponsePolicy,
        )
        # Target has trust_default=none
        _store_boundary_contract(BoundaryContract(
            agent_id="agent-2",
            timestamp=datetime.now().isoformat(),
            trust_default=TrustLevel.NONE,
            trust_overrides={},
            void_response_policy=VoidResponsePolicy.ISOLATE,
            max_delegation_complexity=0.0,
            accept_coherence_threshold=1.0,
        ))
        with patch_require_registered("agent-1"):
            result = await handle_governance_action.__wrapped__({
                "action": "initiate",
                "action_type": "coordination_sync",
                "target_agent_id": "agent-2",
            })
        data = parse_result(result)
        assert data["success"] is False
        assert "none" in data["error"].lower() or "trust" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_initiate_warning_observe_trust(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import (
            handle_governance_action, _store_boundary_contract,
            BoundaryContract, TrustLevel, VoidResponsePolicy,
        )
        _store_boundary_contract(BoundaryContract(
            agent_id="agent-2",
            timestamp=datetime.now().isoformat(),
            trust_default=TrustLevel.OBSERVE,
            trust_overrides={},
            void_response_policy=VoidResponsePolicy.NOTIFY,
            max_delegation_complexity=0.5,
            accept_coherence_threshold=0.4,
        ))
        with patch_require_registered("agent-1"):
            result = await handle_governance_action.__wrapped__({
                "action": "initiate",
                "action_type": "coordination_sync",
                "target_agent_id": "agent-2",
            })
        data = parse_result(result)
        # Should succeed but with a warning
        assert data["success"] is True
        assert "warning" in data

    @pytest.mark.asyncio
    async def test_initiate_trust_override_allows(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import (
            handle_governance_action, _store_boundary_contract,
            BoundaryContract, TrustLevel, VoidResponsePolicy,
        )
        # Default = none, but override for agent-1 = full
        _store_boundary_contract(BoundaryContract(
            agent_id="agent-2",
            timestamp=datetime.now().isoformat(),
            trust_default=TrustLevel.NONE,
            trust_overrides={"agent-1": "full"},
            void_response_policy=VoidResponsePolicy.NOTIFY,
            max_delegation_complexity=0.5,
            accept_coherence_threshold=0.4,
        ))
        with patch_require_registered("agent-1"):
            result = await handle_governance_action.__wrapped__({
                "action": "initiate",
                "action_type": "coordination_sync",
                "target_agent_id": "agent-2",
            })
        data = parse_result(result)
        assert data["success"] is True

    # --- RESPOND ---

    @pytest.mark.asyncio
    async def test_respond_requires_registered_agent(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_governance_action
        err = TextContent(type="text", text=json.dumps({
            "success": False, "error": "Not registered",
        }))
        with patch("src.mcp_handlers.cirs.governance_action.require_registered_agent", return_value=(None, err)):
            result = await handle_governance_action.__wrapped__({
                "action": "respond",
                "action_id": "abc",
            })
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_respond_missing_action_id(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_governance_action
        with patch_require_registered("agent-2"):
            result = await handle_governance_action.__wrapped__({
                "action": "respond",
            })
        data = parse_result(result)
        assert data["success"] is False
        assert "action_id" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_respond_action_not_found(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_governance_action
        with patch_require_registered("agent-2"):
            result = await handle_governance_action.__wrapped__({
                "action": "respond",
                "action_id": "nonexistent",
            })
        data = parse_result(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_respond_not_target(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import (
            handle_governance_action, _store_governance_action,
            GovernanceAction, GovernanceActionType,
        )
        _store_governance_action(GovernanceAction(
            action_id="act-1",
            timestamp=datetime.now().isoformat(),
            action_type=GovernanceActionType.COORDINATION_SYNC,
            initiator_agent_id="agent-1",
            target_agent_id="agent-2",
            payload={},
            status="pending",
        ))
        # Agent-3 tries to respond but is not the target
        with patch_require_registered("agent-3"):
            result = await handle_governance_action.__wrapped__({
                "action": "respond",
                "action_id": "act-1",
            })
        data = parse_result(result)
        assert data["success"] is False
        assert "not the target" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_respond_already_responded(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import (
            handle_governance_action, _store_governance_action,
            GovernanceAction, GovernanceActionType,
        )
        _store_governance_action(GovernanceAction(
            action_id="act-1",
            timestamp=datetime.now().isoformat(),
            action_type=GovernanceActionType.COORDINATION_SYNC,
            initiator_agent_id="agent-1",
            target_agent_id="agent-2",
            payload={},
            status="accepted",
        ))
        with patch_require_registered("agent-2"):
            result = await handle_governance_action.__wrapped__({
                "action": "respond",
                "action_id": "act-1",
                "accept": True,
            })
        data = parse_result(result)
        assert data["success"] is False
        assert "accepted" in data["error"].lower() or "non-pending" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_respond_accept(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import (
            handle_governance_action, _store_governance_action,
            GovernanceAction, GovernanceActionType,
        )
        _store_governance_action(GovernanceAction(
            action_id="act-1",
            timestamp=datetime.now().isoformat(),
            action_type=GovernanceActionType.DELEGATION_REQUEST,
            initiator_agent_id="agent-1",
            target_agent_id="agent-2",
            payload={"task": "help me"},
            status="pending",
        ))
        with patch_require_registered("agent-2"):
            result = await handle_governance_action.__wrapped__({
                "action": "respond",
                "action_id": "act-1",
                "accept": True,
                "response_data": {"eta": "10 minutes"},
            })
        data = parse_result(result)
        assert data["success"] is True
        ga = data["governance_action"]
        assert ga["status"] == "accepted"
        assert ga["response"]["accepted"] is True
        assert ga["response"]["data"]["eta"] == "10 minutes"

    @pytest.mark.asyncio
    async def test_respond_reject(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import (
            handle_governance_action, _store_governance_action,
            GovernanceAction, GovernanceActionType,
        )
        _store_governance_action(GovernanceAction(
            action_id="act-1",
            timestamp=datetime.now().isoformat(),
            action_type=GovernanceActionType.DELEGATION_REQUEST,
            initiator_agent_id="agent-1",
            target_agent_id="agent-2",
            payload={},
            status="pending",
        ))
        with patch_require_registered("agent-2"):
            result = await handle_governance_action.__wrapped__({
                "action": "respond",
                "action_id": "act-1",
                "accept": False,
            })
        data = parse_result(result)
        assert data["success"] is True
        assert data["governance_action"]["status"] == "rejected"

    # --- QUERY ---

    @pytest.mark.asyncio
    async def test_query_requires_registered_agent(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_governance_action
        err = TextContent(type="text", text=json.dumps({
            "success": False, "error": "Not registered",
        }))
        with patch("src.mcp_handlers.cirs.governance_action.require_registered_agent", return_value=(None, err)):
            result = await handle_governance_action.__wrapped__({
                "action": "query",
            })
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_query_empty(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import handle_governance_action
        with patch_require_registered("agent-1"):
            result = await handle_governance_action.__wrapped__({
                "action": "query",
            })
        data = parse_result(result)
        assert data["success"] is True
        assert data["actions"] == []
        assert data["summary"]["total_actions"] == 0

    @pytest.mark.asyncio
    async def test_query_with_actions(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import (
            handle_governance_action, _store_governance_action,
            GovernanceAction, GovernanceActionType,
        )
        _store_governance_action(GovernanceAction(
            action_id="act-1",
            timestamp=datetime.now().isoformat(),
            action_type=GovernanceActionType.COORDINATION_SYNC,
            initiator_agent_id="agent-1",
            target_agent_id="agent-2",
            payload={},
            status="pending",
        ))
        _store_governance_action(GovernanceAction(
            action_id="act-2",
            timestamp=datetime.now().isoformat(),
            action_type=GovernanceActionType.COHERENCE_BOOST,
            initiator_agent_id="agent-1",
            target_agent_id="agent-3",
            payload={},
            status="accepted",
        ))
        with patch_require_registered("agent-1"):
            result = await handle_governance_action.__wrapped__({
                "action": "query",
            })
        data = parse_result(result)
        assert data["summary"]["total_actions"] == 2
        assert data["summary"]["pending"] == 1
        assert data["summary"]["accepted"] == 1

    @pytest.mark.asyncio
    async def test_query_filter_by_status(self, mock_server, patch_require_registered):
        from src.mcp_handlers.cirs.protocol import (
            handle_governance_action, _store_governance_action,
            GovernanceAction, GovernanceActionType,
        )
        _store_governance_action(GovernanceAction(
            action_id="act-1",
            timestamp=datetime.now().isoformat(),
            action_type=GovernanceActionType.COORDINATION_SYNC,
            initiator_agent_id="agent-1",
            target_agent_id="agent-2",
            payload={},
            status="pending",
        ))
        _store_governance_action(GovernanceAction(
            action_id="act-2",
            timestamp=datetime.now().isoformat(),
            action_type=GovernanceActionType.COHERENCE_BOOST,
            initiator_agent_id="agent-1",
            target_agent_id="agent-3",
            payload={},
            status="accepted",
        ))
        with patch_require_registered("agent-1"):
            result = await handle_governance_action.__wrapped__({
                "action": "query",
                "status_filter": "pending",
            })
        data = parse_result(result)
        assert data["summary"]["total_actions"] == 1
        assert data["actions"][0]["status"] == "pending"

    # --- STATUS ---

    @pytest.mark.asyncio
    async def test_status_missing_action_id(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_governance_action
        result = await handle_governance_action.__wrapped__({
            "action": "status",
        })
        data = parse_result(result)
        assert data["success"] is False
        assert "action_id" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_status_not_found(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_governance_action
        result = await handle_governance_action.__wrapped__({
            "action": "status",
            "action_id": "nonexistent",
        })
        data = parse_result(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_status_found(self, mock_server):
        from src.mcp_handlers.cirs.protocol import (
            handle_governance_action, _store_governance_action,
            GovernanceAction, GovernanceActionType,
        )
        _store_governance_action(GovernanceAction(
            action_id="act-1",
            timestamp=datetime.now().isoformat(),
            action_type=GovernanceActionType.DELEGATION_REQUEST,
            initiator_agent_id="agent-1",
            target_agent_id="agent-2",
            payload={"task": "review code"},
            status="pending",
        ))
        result = await handle_governance_action.__wrapped__({
            "action": "status",
            "action_id": "act-1",
        })
        data = parse_result(result)
        assert data["success"] is True
        assert data["governance_action"]["action_id"] == "act-1"
        assert data["governance_action"]["status"] == "pending"


# ============================================================================
# CIRS_PROTOCOL Unified Entry Point Tests
# ============================================================================

class TestHandleCirsProtocol:
    """Tests for handle_cirs_protocol unified dispatch handler."""

    @pytest.mark.asyncio
    async def test_missing_protocol(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_cirs_protocol
        result = await handle_cirs_protocol.__wrapped__({})
        data = parse_result(result)
        assert data["success"] is False
        assert "protocol" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_unknown_protocol(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_cirs_protocol
        result = await handle_cirs_protocol.__wrapped__({"protocol": "banana"})
        data = parse_result(result)
        assert data["success"] is False
        assert "unknown" in data["error"].lower() or "banana" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_dispatch_void_alert(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_cirs_protocol
        result = await handle_cirs_protocol.__wrapped__({
            "protocol": "void_alert",
            "action": "query",
        })
        data = parse_result(result)
        assert data["success"] is True
        assert data["cirs_protocol"] == "VOID_ALERT"

    @pytest.mark.asyncio
    async def test_dispatch_state_announce(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_cirs_protocol
        result = await handle_cirs_protocol.__wrapped__({
            "protocol": "state_announce",
            "action": "query",
        })
        data = parse_result(result)
        assert data["success"] is True
        assert data["cirs_protocol"] == "STATE_ANNOUNCE"

    @pytest.mark.asyncio
    async def test_dispatch_coherence_report(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_cirs_protocol
        result = await handle_cirs_protocol.__wrapped__({
            "protocol": "coherence_report",
            "action": "query",
        })
        data = parse_result(result)
        assert data["success"] is True
        assert data["cirs_protocol"] == "COHERENCE_REPORT"

    @pytest.mark.asyncio
    async def test_dispatch_boundary_contract(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_cirs_protocol
        result = await handle_cirs_protocol.__wrapped__({
            "protocol": "boundary_contract",
            "action": "list",
        })
        data = parse_result(result)
        assert data["success"] is True
        assert data["cirs_protocol"] == "BOUNDARY_CONTRACT"

    @pytest.mark.asyncio
    async def test_dispatch_governance_action(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_cirs_protocol
        result = await handle_cirs_protocol.__wrapped__({
            "protocol": "governance_action",
            "action": "status",
            "action_id": "nonexistent",
        })
        data = parse_result(result)
        # This should fail with "not found" but still dispatches correctly
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_dispatch_case_insensitive(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_cirs_protocol
        result = await handle_cirs_protocol.__wrapped__({
            "protocol": "VOID_ALERT",
            "action": "query",
        })
        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_dispatch_strips_whitespace(self, mock_server):
        from src.mcp_handlers.cirs.protocol import handle_cirs_protocol
        result = await handle_cirs_protocol.__wrapped__({
            "protocol": "  void_alert  ",
            "action": "query",
        })
        data = parse_result(result)
        assert data["success"] is True


# ============================================================================
# Buffer Management / Storage Tests
# ============================================================================

class TestBufferManagement:
    """Tests for in-memory buffer operations and TTL cleanup."""

    def test_void_alert_buffer_stores_and_retrieves(self):
        from src.mcp_handlers.cirs.protocol import (
            _store_void_alert, _get_recent_void_alerts,
            VoidAlert, VoidSeverity,
        )
        _store_void_alert(VoidAlert(
            agent_id="agent-1",
            timestamp=datetime.now().isoformat(),
            severity=VoidSeverity.WARNING,
            V_snapshot=0.05,
        ))
        alerts = _get_recent_void_alerts()
        assert len(alerts) == 1
        assert alerts[0]["agent_id"] == "agent-1"

    def test_void_alert_buffer_respects_limit(self):
        from src.mcp_handlers.cirs.protocol import (
            _store_void_alert, _get_recent_void_alerts,
            VoidAlert, VoidSeverity,
        )
        for i in range(10):
            _store_void_alert(VoidAlert(
                agent_id=f"agent-{i}",
                timestamp=datetime.now().isoformat(),
                severity=VoidSeverity.WARNING,
                V_snapshot=0.05,
            ))
        alerts = _get_recent_void_alerts(limit=3)
        assert len(alerts) == 3

    def test_state_announce_overwrites_per_agent(self):
        from src.mcp_handlers.cirs.protocol import (
            _store_state_announce, _get_state_announces, StateAnnounce,
        )
        _store_state_announce(StateAnnounce(
            agent_id="agent-1",
            timestamp=datetime.now().isoformat(),
            eisv={"E": 0.5, "I": 0.5, "S": 0.5, "V": 0.0},
            coherence=0.5, regime="divergence", phi=0.3,
            verdict="caution", risk_score=0.5, update_count=1,
        ))
        _store_state_announce(StateAnnounce(
            agent_id="agent-1",
            timestamp=datetime.now().isoformat(),
            eisv={"E": 0.9, "I": 0.9, "S": 0.1, "V": 0.0},
            coherence=0.9, regime="stable", phi=0.8,
            verdict="safe", risk_score=0.1, update_count=100,
        ))
        announces = _get_state_announces()
        # Should only have one entry (overwritten)
        assert len(announces) == 1
        assert announces[0]["coherence"] == 0.9
        assert announces[0]["regime"] == "stable"

    def test_coherence_report_overwrites_per_pair(self):
        from src.mcp_handlers.cirs.protocol import (
            _store_coherence_report, _get_coherence_reports, CoherenceReport,
        )
        _store_coherence_report(CoherenceReport(
            source_agent_id="agent-1",
            timestamp=datetime.now().isoformat(),
            target_agent_id="agent-2",
            similarity_score=0.5,
            eisv_similarity={"E": 0.5, "I": 0.5, "S": 0.5, "V": 0.5},
            regime_match=False, verdict_match=False,
        ))
        _store_coherence_report(CoherenceReport(
            source_agent_id="agent-1",
            timestamp=datetime.now().isoformat(),
            target_agent_id="agent-2",
            similarity_score=0.9,
            eisv_similarity={"E": 0.9, "I": 0.9, "S": 0.9, "V": 0.9},
            regime_match=True, verdict_match=True,
        ))
        reports = _get_coherence_reports()
        assert len(reports) == 1
        assert reports[0]["similarity_score"] == 0.9

    def test_governance_action_stored_by_id(self):
        from src.mcp_handlers.cirs.protocol import (
            _store_governance_action, _get_governance_action,
            _get_governance_actions_for_agent,
            GovernanceAction, GovernanceActionType,
        )
        _store_governance_action(GovernanceAction(
            action_id="act-1",
            timestamp=datetime.now().isoformat(),
            action_type=GovernanceActionType.COORDINATION_SYNC,
            initiator_agent_id="agent-1",
            target_agent_id="agent-2",
            payload={},
        ))
        # Get by ID
        action = _get_governance_action("act-1")
        assert action is not None
        assert action["action_id"] == "act-1"
        # Get for agent
        actions = _get_governance_actions_for_agent("agent-1")
        assert len(actions) == 1
        actions_target = _get_governance_actions_for_agent("agent-2")
        assert len(actions_target) == 1
        # Get for unrelated agent
        actions_none = _get_governance_actions_for_agent("agent-3")
        assert len(actions_none) == 0

    def test_governance_actions_for_agent_filters(self):
        from src.mcp_handlers.cirs.protocol import (
            _store_governance_action, _get_governance_actions_for_agent,
            GovernanceAction, GovernanceActionType,
        )
        _store_governance_action(GovernanceAction(
            action_id="act-1",
            timestamp=datetime.now().isoformat(),
            action_type=GovernanceActionType.COORDINATION_SYNC,
            initiator_agent_id="agent-1",
            target_agent_id="agent-2",
            payload={},
            status="pending",
        ))
        _store_governance_action(GovernanceAction(
            action_id="act-2",
            timestamp=datetime.now().isoformat(),
            action_type=GovernanceActionType.DELEGATION_REQUEST,
            initiator_agent_id="agent-2",
            target_agent_id="agent-1",
            payload={},
            status="accepted",
        ))
        # Only as initiator
        as_init = _get_governance_actions_for_agent(
            "agent-1", as_initiator=True, as_target=False,
        )
        assert len(as_init) == 1
        assert as_init[0]["action_id"] == "act-1"
        # Only as target
        as_tgt = _get_governance_actions_for_agent(
            "agent-1", as_initiator=False, as_target=True,
        )
        assert len(as_tgt) == 1
        assert as_tgt[0]["action_id"] == "act-2"
        # Filter by status
        pending = _get_governance_actions_for_agent(
            "agent-1", status="pending",
        )
        assert len(pending) == 1
        assert pending[0]["action_id"] == "act-1"

    def test_boundary_contract_get_and_store(self):
        from src.mcp_handlers.cirs.protocol import (
            _store_boundary_contract, _get_boundary_contract,
            _get_all_boundary_contracts,
            BoundaryContract, TrustLevel, VoidResponsePolicy,
        )
        assert _get_boundary_contract("agent-1") is None
        assert _get_all_boundary_contracts() == []

        _store_boundary_contract(BoundaryContract(
            agent_id="agent-1",
            timestamp=datetime.now().isoformat(),
            trust_default=TrustLevel.PARTIAL,
            trust_overrides={},
            void_response_policy=VoidResponsePolicy.NOTIFY,
            max_delegation_complexity=0.5,
            accept_coherence_threshold=0.4,
        ))
        contract = _get_boundary_contract("agent-1")
        assert contract is not None
        assert contract["trust_default"] == "partial"
        assert len(_get_all_boundary_contracts()) == 1


# ============================================================================
# Enum Tests
# ============================================================================

class TestEnums:
    """Tests for CIRS protocol enums."""

    def test_void_severity_values(self):
        from src.mcp_handlers.cirs.protocol import VoidSeverity
        assert VoidSeverity.WARNING.value == "warning"
        assert VoidSeverity.CRITICAL.value == "critical"

    def test_agent_regime_values(self):
        from src.mcp_handlers.cirs.protocol import AgentRegime
        assert AgentRegime.DIVERGENCE.value == "divergence"
        assert AgentRegime.TRANSITION.value == "transition"
        assert AgentRegime.CONVERGENCE.value == "convergence"
        assert AgentRegime.STABLE.value == "stable"

    def test_trust_level_values(self):
        from src.mcp_handlers.cirs.protocol import TrustLevel
        assert TrustLevel.FULL.value == "full"
        assert TrustLevel.PARTIAL.value == "partial"
        assert TrustLevel.OBSERVE.value == "observe"
        assert TrustLevel.NONE.value == "none"

    def test_void_response_policy_values(self):
        from src.mcp_handlers.cirs.protocol import VoidResponsePolicy
        assert VoidResponsePolicy.NOTIFY.value == "notify"
        assert VoidResponsePolicy.ASSIST.value == "assist"
        assert VoidResponsePolicy.ISOLATE.value == "isolate"
        assert VoidResponsePolicy.COORDINATE.value == "coordinate"

    def test_governance_action_type_values(self):
        from src.mcp_handlers.cirs.protocol import GovernanceActionType
        assert GovernanceActionType.VOID_INTERVENTION.value == "void_intervention"
        assert GovernanceActionType.COHERENCE_BOOST.value == "coherence_boost"
        assert GovernanceActionType.DELEGATION_REQUEST.value == "delegation_request"
        assert GovernanceActionType.DELEGATION_RESPONSE.value == "delegation_response"
        assert GovernanceActionType.COORDINATION_SYNC.value == "coordination_sync"
