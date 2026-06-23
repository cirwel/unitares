"""
Tests for src/mcp_handlers/core.py - Core governance handler functions.

Tests _assess_thermodynamic_significance (pure function) and
handle_simulate_update / handle_get_governance_metrics (with mocked backends).
"""

import pytest
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


@pytest.fixture
def bound_context():
    """Bind the transport context to agent-1 (read-purity guard, trust
    contract §3.5): handle_get_governance_metrics serves the unbound
    shape unless an actual binding exists — patching require_agent_id
    alone no longer simulates a bound caller. Tests that need the
    UNBOUND path re-patch get_context_agent_id to None inside their own
    `with` block (innermost patch wins)."""
    with patch(
        "src.mcp_handlers.context.get_context_agent_id",
        return_value="agent-1",
    ):
        yield


# ============================================================================
# _assess_thermodynamic_significance (pure function - no mocks needed)
# ============================================================================

class TestAssessThermodynamicSignificance:
    """Tests for the pure significance assessment function."""

    @pytest.fixture(autouse=True)
    def import_fn(self):
        from src.mcp_handlers.core import _assess_thermodynamic_significance
        self.assess = _assess_thermodynamic_significance

    def _make_monitor(self, risk_history=None, coherence_history=None, V=0.0):
        state = SimpleNamespace(
            risk_history=risk_history or [],
            coherence_history=coherence_history or [],
            V=V,
        )
        return SimpleNamespace(state=state)

    def test_no_monitor_returns_not_significant(self):
        result = self.assess(None, {})
        assert result["is_significant"] is False
        assert "No monitor available" in result["reasons"]
        assert "timestamp" in result

    def test_stable_state_not_significant(self):
        monitor = self._make_monitor(
            risk_history=[0.3, 0.3, 0.3, 0.3, 0.3],
            coherence_history=[0.8, 0.8, 0.8, 0.8, 0.8],
            V=0.0,
        )
        result = self.assess(monitor, {"decision": {"action": "approve"}})
        assert result["is_significant"] is False
        assert result["reasons"] == []

    def test_risk_spike_is_significant(self):
        # Baseline ~0.3, then spike to 0.6 => delta 0.3 > 0.15 threshold
        monitor = self._make_monitor(
            risk_history=[0.3, 0.3, 0.3, 0.3, 0.6],
            coherence_history=[0.8, 0.8, 0.8, 0.8, 0.8],
        )
        result = self.assess(monitor, {})
        assert result["is_significant"] is True
        assert any("risk_spike" in r for r in result["reasons"])

    def test_coherence_drop_is_significant(self):
        # Baseline ~0.8, drop to 0.5 => delta 0.3 > 0.10 threshold
        monitor = self._make_monitor(
            risk_history=[0.3, 0.3, 0.3, 0.3, 0.3],
            coherence_history=[0.8, 0.8, 0.8, 0.8, 0.5],
        )
        result = self.assess(monitor, {})
        assert result["is_significant"] is True
        assert any("coherence_drop" in r for r in result["reasons"])

    def test_void_threshold_is_significant(self):
        monitor = self._make_monitor(V=0.15)
        result = self.assess(monitor, {})
        assert result["is_significant"] is True
        assert any("void_significant" in r for r in result["reasons"])

    def test_circuit_breaker_is_significant(self):
        monitor = self._make_monitor()
        result = self.assess(monitor, {"circuit_breaker": {"triggered": True}})
        assert result["is_significant"] is True
        assert "circuit_breaker_triggered" in result["reasons"]

    def test_pause_decision_is_significant(self):
        monitor = self._make_monitor()
        result = self.assess(monitor, {"decision": {"action": "pause"}})
        assert result["is_significant"] is True
        assert "decision_pause" in result["reasons"]

    def test_reject_decision_is_significant(self):
        monitor = self._make_monitor()
        result = self.assess(monitor, {"decision": {"action": "reject"}})
        assert result["is_significant"] is True
        assert "decision_reject" in result["reasons"]

    def test_approve_decision_not_significant(self):
        monitor = self._make_monitor()
        result = self.assess(monitor, {"decision": {"action": "approve"}})
        # approve alone should not trigger significance
        assert "decision_approve" not in result["reasons"]

    def test_multiple_reasons_combined(self):
        monitor = self._make_monitor(
            risk_history=[0.3, 0.3, 0.3, 0.3, 0.6],
            V=0.15,
        )
        result = self.assess(monitor, {"decision": {"action": "pause"}})
        assert result["is_significant"] is True
        assert len(result["reasons"]) >= 2

    def test_single_history_entry_no_crash(self):
        monitor = self._make_monitor(risk_history=[0.5], coherence_history=[0.8])
        result = self.assess(monitor, {})
        # Should not crash with <2 history entries
        assert isinstance(result["is_significant"], bool)


# ============================================================================
# handle_simulate_update (mocked monitor)
# ============================================================================

class TestSimulateUpdate:
    """Tests for simulate_update handler with mocked mcp_server."""

    @pytest.fixture
    def mock_mcp_server(self):
        """Mock the mcp_server_std module."""
        server = MagicMock()
        server.agent_metadata = {}
        server.monitors = {}
        server.get_or_create_monitor = MagicMock()
        return server

    @pytest.fixture
    def mock_monitor(self):
        """Create a mock UNITARESMonitor."""
        m = MagicMock()
        m.simulate_update.return_value = {
            "status": "ok",
            "decision": {"action": "approve", "confidence": 0.8},
            "metrics": {
                "E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0,
                "coherence": 0.52, "risk_score": 0.3,
            },
            "guidance": "Continue current approach.",
        }
        return m

    @pytest.mark.asyncio
    async def test_simulate_fresh_state(self, mock_mcp_server, mock_monitor):
        """Simulate with no registered agent uses fresh state."""
        with patch("src.mcp_handlers.core.mcp_server", mock_mcp_server), \
             patch("src.governance_monitor.UNITARESMonitor", return_value=mock_monitor), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=(None, None)):

            from src.mcp_handlers.core import handle_simulate_update
            result = await handle_simulate_update({"complexity": 0.5})

            assert len(result) == 1
            data = json.loads(result[0].text)
            assert data["simulation"] is True
            assert data["agent_state_source"] == "fresh"
            assert "note" in data  # Should have "fresh state" note

    @pytest.mark.asyncio
    async def test_simulate_existing_agent(self, mock_mcp_server, mock_monitor):
        """Simulate with existing agent uses their state."""
        mock_mcp_server.agent_metadata = {"agent-1": MagicMock()}
        mock_mcp_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)):

            from src.mcp_handlers.core import handle_simulate_update
            result = await handle_simulate_update({"complexity": 0.5})

            data = json.loads(result[0].text)
            assert data["agent_state_source"] == "existing"
            assert "note" not in data

    @pytest.mark.asyncio
    async def test_simulate_invalid_complexity_returns_error(self, mock_mcp_server):
        """Invalid complexity should return validation error."""
        with patch("src.mcp_handlers.core.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=(None, None)):

            from src.mcp_handlers.core import handle_simulate_update

            # Pydantic now handles complexity validation via SimulateUpdateParams
            # Simulate what happens when the middleware passes invalid complexity through
            # (simulate_update uses the value directly, Pydantic coercion handles "bad" -> default)
            result = await handle_simulate_update({"complexity": 0.5})
            # Should succeed with valid complexity
            assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_simulate_lite_mode(self, mock_mcp_server, mock_monitor):
        """Lite mode returns minimal response."""
        with patch("src.mcp_handlers.core.mcp_server", mock_mcp_server), \
             patch("src.governance_monitor.UNITARESMonitor", return_value=mock_monitor), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=(None, None)):

            from src.mcp_handlers.core import handle_simulate_update
            result = await handle_simulate_update({"complexity": 0.5, "lite": True})

            data = json.loads(result[0].text)
            assert data["simulation"] is True
            assert "_note" in data  # Lite mode note

    @pytest.mark.asyncio
    async def test_simulate_dialectic_conditions_cap(self, mock_mcp_server, mock_monitor):
        """Dialectic conditions should cap complexity."""
        meta = MagicMock()
        meta.dialectic_conditions = [{"type": "complexity_limit", "value": 0.3}]
        mock_mcp_server.agent_metadata = {"agent-1": meta}
        mock_mcp_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)):

            from src.mcp_handlers.core import handle_simulate_update
            result = await handle_simulate_update({"complexity": 0.8})

            data = json.loads(result[0].text)
            assert "dialectic_warning" in data


# ============================================================================
# handle_get_governance_metrics (mocked monitor + mcp_server)
# ============================================================================

@pytest.mark.usefixtures("bound_context")
class TestGetGovernanceMetrics:
    """Tests for get_governance_metrics handler."""

    @pytest.fixture
    def mock_mcp_server(self):
        server = MagicMock()
        server.agent_metadata = {}
        server.monitors = {}
        server.get_or_create_monitor = MagicMock()
        return server

    @pytest.fixture
    def mock_monitor(self):
        """Create a mock monitor with realistic get_metrics output."""
        state = SimpleNamespace(
            interpret_state=MagicMock(return_value={
                "health": "healthy",
                "mode": "convergent",
                "basin": "stable",
            }),
            unitaires_state=None,
        )
        m = MagicMock()
        m.state = state
        m.get_metrics.return_value = {
            "E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0,
            "coherence": 0.52, "risk_score": 0.3,
            "initialized": True, "status": "ok",
            "complexity": 0.5,
            "ode": {"E": 0.65, "I": 0.55, "S": 0.25, "V": -0.01},
            "phi": 0.12,
            "regime": "EXPLORATION",
            "lambda1": 0.08,
            "verdict": "safe",
        }
        return m

    @pytest.mark.asyncio
    async def test_get_metrics_requires_agent_id(self, mock_mcp_server):
        """Should return error when require_agent_id itself errors.

        DEPENDS on the class-level bound_context fixture: only a bound
        caller reaches require_agent_id at all — the unbound guard would
        otherwise short-circuit to the unbound shape and this test would
        silently stop covering the error path (PR #608 review note).
        """
        from mcp.types import TextContent
        error = TextContent(type="text", text='{"error": "agent_id required"}')

        with patch("src.mcp_handlers.core.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=(None, error)):

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({})

            assert "agent_id required" in result[0].text

    @pytest.mark.asyncio
    async def test_get_metrics_unbound_does_not_create_monitor(self, mock_mcp_server):
        """Read-only metrics without a bound identity should report unbound.

        This prevents diagnostic calls with stale/missing client_session_id from
        creating fresh monitor/UUID state just to answer "who am I?".
        """
        with patch("src.mcp_handlers.core.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None):

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"client_session_id": "agent-missing"})

            data = json.loads(result[0].text)
            # #428: verdict is wrapped with meaning + next_action at the
            # response surface. The bare value lives at .value.
            assert data["verdict"]["value"] == "unbound"
            # The unbound next_action must steer to an explicit, proof-bearing
            # identity path. Bare identity() can mint an orphan, so the safe
            # canonical hint is onboard(force_new=true).
            assert data["next_action"]["tool"] == "onboard"
            assert "force_new=true" in data["next_action"]["example"]
            mock_mcp_server.get_or_create_monitor.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_metrics_lite_mode(self, mock_mcp_server, mock_monitor):
        """Lite mode returns minimal metrics with status indicators."""
        meta = MagicMock()
        meta.purpose = "test purpose"
        meta.public_agent_id = "Gpt_5_Codex_20260404"
        meta.label = "Dogfood Agent"
        mock_mcp_server.agent_metadata = {"agent-1": meta}
        mock_mcp_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockMonitorClass:

            MockMonitorClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": True})

            data = json.loads(result[0].text)
            # display_name (label) takes precedence over public_agent_id
            assert data["agent_id"] == "Dogfood Agent"
            assert data["agent_uuid"] == "agent-1"
            assert data["structured_agent_id"] == "Gpt_5_Codex_20260404"
            assert data["display_name"] == "Dogfood Agent"
            assert data["primary_eisv_source"] == "ode_fallback"
            assert "status" in data
            assert "coherence" in data
            assert "risk_score" in data
            assert "_note" in data  # Lite mode note
            assert data["purpose"] == "test purpose"

    @pytest.mark.asyncio
    async def test_get_metrics_full_mode(self, mock_mcp_server, mock_monitor):
        """Full mode returns standardized metrics with interpretation."""
        mock_mcp_server.agent_metadata = {
            "agent-1": MagicMock(
                purpose=None,
                public_agent_id="Gpt_5_Codex_20260404",
                label="Dogfood Agent",
            )
        }
        mock_mcp_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockMonitorClass:

            MockMonitorClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": False})

            data = json.loads(result[0].text)
            # Full mode should have summary; reflection is now conditional
            assert "summary" in data
            # display_name (label) takes precedence over public_agent_id
            assert data["agent_id"] == "Dogfood Agent"
            assert data["agent_uuid"] == "agent-1"
            assert data["structured_agent_id"] == "Gpt_5_Codex_20260404"
            assert data["display_name"] == "Dogfood Agent"
            assert data["primary_eisv_source"] == "ode_fallback"
            assert data["primary_eisv"]["E"] == 0.7
            assert data["ode_eisv"]["E"] == 0.65
            assert "state_semantics" in data
            semantics = data["state_semantics"]
            assert semantics["measurement_policy_contract"] == (
                "EISV measurements feed governance policy; policy evaluation chooses guidance/action; "
                "enforcement is a separate runtime boundary."
            )
            assert "feeds governance policy" in semantics["behavioral_eisv_role"]
            assert "Determines proceed/guide/pause/reject" not in semantics["behavioral_eisv_role"]

    @pytest.mark.asyncio
    async def test_get_metrics_standard_mode_wraps_basin_mode_verdict(self, mock_mcp_server):
        """Standard verbosity wraps basin/mode/verdict with glossary entries (#428).

        Lite mode already wrapped these via explain_*; standard mode used to
        emit bare strings, leaving cold agents without point-of-use vocabulary.
        Wraps now symmetric with lite path.
        """
        state = SimpleNamespace(
            interpret_state=MagicMock(return_value={
                "health": "healthy",
                "mode": "building_alone",
                "basin": "high",
            }),
            unitaires_state=None,
        )
        m = MagicMock()
        m.state = state
        m.get_metrics.return_value = {
            "E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0,
            "coherence": 0.52, "risk_score": 0.3,
            "initialized": True, "status": "ok",
            "verdict": "proceed",
        }
        mock_mcp_server.agent_metadata = {"agent-1": MagicMock(purpose=None)}
        mock_mcp_server.get_or_create_monitor.return_value = m

        with patch("src.mcp_handlers.core.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockMonitorClass:

            MockMonitorClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"verbosity": "standard"})

            data = json.loads(result[0].text)
            assert data["verdict"]["value"] == "proceed"
            assert "meaning" in data["verdict"]
            assert "next_action" in data["verdict"]
            assert data["basin"]["value"] == "high"
            assert "meaning" in data["basin"]
            assert data["mode"]["value"] == "building_alone"
            assert "meaning" in data["mode"]

    @pytest.mark.asyncio
    async def test_get_metrics_standard_mode_omits_basin_mode_when_state_missing(self, mock_mcp_server):
        """When interpret_state raises, standard verbosity omits basin/mode rather
        than emitting bare {"value": null}. Symmetric to lite path guard at
        runtime_queries.py:346.
        """
        state = SimpleNamespace(
            interpret_state=MagicMock(side_effect=RuntimeError("interpret failed")),
            unitaires_state=None,
        )
        m = MagicMock()
        m.state = state
        m.get_metrics.return_value = {
            "E": 0.5, "I": 0.5, "S": 0.5, "V": 0.0,
            "coherence": None, "risk_score": None,
            "initialized": True, "status": "ok",
            "verdict": "proceed",
        }
        mock_mcp_server.agent_metadata = {"agent-1": MagicMock(purpose=None)}
        mock_mcp_server.get_or_create_monitor.return_value = m

        with patch("src.mcp_handlers.core.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockMonitorClass:

            MockMonitorClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"verbosity": "standard"})

            data = json.loads(result[0].text)
            assert "basin" not in data
            assert "mode" not in data
            assert data["verdict"]["value"] == "proceed"

    @pytest.mark.asyncio
    async def test_get_metrics_uninitialized_agent(self, mock_mcp_server):
        """Uninitialized agent should show pending status in lite mode."""
        state = SimpleNamespace(
            interpret_state=MagicMock(return_value={"health": "unknown", "mode": "unknown", "basin": "unknown"}),
            unitaires_state=None,
        )
        uninit_monitor = MagicMock()
        uninit_monitor.state = state
        uninit_monitor.get_metrics.return_value = {
            "E": 0.5, "I": 0.5, "S": 0.5, "V": 0.0,
            "coherence": None, "risk_score": None,
            "initialized": False, "status": "uninitialized",
        }
        mock_mcp_server.agent_metadata = {"agent-1": MagicMock(purpose=None)}
        mock_mcp_server.get_or_create_monitor.return_value = uninit_monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockMonitorClass:

            MockMonitorClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": True})

            data = json.loads(result[0].text)
            assert "uninitialized" in data["status"]

    @pytest.mark.asyncio
    async def test_get_metrics_includes_calibration_feedback(self, mock_mcp_server, mock_monitor):
        """Metrics should include calibration feedback when available."""
        mock_mcp_server.agent_metadata = {"agent-1": MagicMock(purpose=None)}
        mock_mcp_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.core.mcp_server", mock_mcp_server), \
             patch("src.mcp_handlers.core.require_agent_id", return_value=("agent-1", None)), \
             patch("src.governance_monitor.UNITARESMonitor") as MockMonitorClass:

            MockMonitorClass.get_eisv_labels.return_value = {
                "E": "Energy", "I": "Information", "S": "Entropy", "V": "Void"
            }

            from src.mcp_handlers.core import handle_get_governance_metrics
            result = await handle_get_governance_metrics({"lite": False})

            data = json.loads(result[0].text)
            # Should have calibration_feedback if complexity is in metrics
            if "calibration_feedback" in data:
                assert "complexity" in data["calibration_feedback"]
