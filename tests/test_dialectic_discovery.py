"""
Test for Pull-Based Dialectic Discovery

Verifies that agents can discover pending dialectic sessions through the
current process-update enrichment path.
"""

import pytest
from types import SimpleNamespace


class TestDialecticDiscovery:
    """Test the current pull-style pending dialectic discovery surface."""

    @pytest.mark.asyncio
    async def test_reviewer_pending_antithesis_is_enriched(self, monkeypatch):
        """process_agent_update enrichment surfaces reviewer action items."""
        from src.dialectic_protocol import DialecticPhase
        from src.mcp_handlers.updates.context import UpdateContext
        from src.mcp_handlers.updates.enrichments import enrich_pending_dialectic
        import src.mcp_handlers.dialectic as dialectic_module

        session = SimpleNamespace(
            reviewer_agent_id="agent-reviewer",
            paused_agent_id="agent-paused",
            phase=DialecticPhase.ANTITHESIS,
            topic="Recovery threshold",
            created_at=SimpleNamespace(
                isoformat=lambda: "2026-06-14T12:00:00+00:00"
            ),
        )
        monkeypatch.setattr(
            dialectic_module, "ACTIVE_SESSIONS", {"session-1": session}
        )

        ctx = UpdateContext(agent_id="agent-reviewer")
        await enrich_pending_dialectic(ctx)

        pending = ctx.response_data["pending_dialectic"]
        assert pending["sessions"] == [
            {
                "session_id": "session-1",
                "role": "reviewer",
                "phase": "antithesis",
                "partner": "agent-paused",
                "topic": "Recovery threshold",
                "action_needed": "Submit antithesis via submit_antithesis()",
                "created_at": "2026-06-14T12:00:00+00:00",
            }
        ]

    @pytest.mark.asyncio
    async def test_initiator_pending_synthesis_is_enriched(self, monkeypatch):
        """process_agent_update enrichment surfaces initiator action items."""
        from src.dialectic_protocol import DialecticPhase
        from src.mcp_handlers.updates.context import UpdateContext
        from src.mcp_handlers.updates.enrichments import enrich_pending_dialectic
        import src.mcp_handlers.dialectic as dialectic_module

        session = SimpleNamespace(
            reviewer_agent_id="agent-reviewer",
            paused_agent_id="agent-paused",
            phase=DialecticPhase.SYNTHESIS,
            topic="Recovery threshold",
            created_at=None,
        )
        monkeypatch.setattr(
            dialectic_module, "ACTIVE_SESSIONS", {"session-2": session}
        )

        ctx = UpdateContext(agent_id="agent-paused")
        await enrich_pending_dialectic(ctx)

        pending = ctx.response_data["pending_dialectic"]
        assert pending["sessions"][0]["role"] == "initiator"
        assert pending["sessions"][0]["phase"] == "synthesis"
        assert pending["sessions"][0]["partner"] == "agent-reviewer"
        assert pending["sessions"][0]["created_at"] is None


class TestBasinCheckingIntegration:
    """Test UNITARES basin checking with dialectic states."""

    def test_check_basin_high(self):
        """Agent in high basin should be flagged as healthy."""
        from governance_core.dynamics import State, check_basin

        state = State(E=0.8, I=0.9, S=0.1, V=0.0)
        basin = check_basin(state)

        assert basin == 'high', f"Expected 'high' basin, got '{basin}'"
        print(f"State with I={state.I} is in '{basin}' basin")

    def test_check_basin_low(self):
        """Agent in low basin should be flagged as collapsed."""
        from governance_core.dynamics import State, check_basin

        state = State(E=0.2, I=0.1, S=0.8, V=0.0)
        basin = check_basin(state)

        assert basin == 'low', f"Expected 'low' basin, got '{basin}'"
        print(f"State with I={state.I} is in '{basin}' basin")

    def test_check_basin_boundary(self):
        """Agent near boundary should be flagged as unstable."""
        from governance_core.dynamics import State, check_basin

        state = State(E=0.5, I=0.5, S=0.3, V=0.0)
        basin = check_basin(state)

        assert basin == 'boundary', f"Expected 'boundary' basin, got '{basin}'"
        print(f"State with I={state.I} is in '{basin}' basin (unstable)")


class TestConvergenceEstimation:
    """Test convergence estimation functions."""

    def test_compute_equilibrium(self):
        """Test equilibrium computation returns valid state."""
        from governance_core.dynamics import compute_equilibrium
        from governance_core.parameters import DEFAULT_PARAMS, DEFAULT_THETA

        eq = compute_equilibrium(DEFAULT_PARAMS, DEFAULT_THETA)

        # Equilibrium should be in high basin
        assert eq.I > 0.5, f"Equilibrium I={eq.I} should be > 0.5"
        assert 0 <= eq.E <= 1, f"E={eq.E} out of bounds"
        assert eq.S >= 0, f"S={eq.S} should be non-negative"

        print(f"High equilibrium: E={eq.E:.3f}, I={eq.I:.3f}, S={eq.S:.3f}, V={eq.V:.3f}")

    def test_estimate_convergence(self):
        """Test convergence estimation provides useful info."""
        from governance_core.dynamics import (
            State, compute_equilibrium, estimate_convergence
        )
        from governance_core.parameters import DEFAULT_PARAMS, DEFAULT_THETA

        current = State(E=0.7, I=0.8, S=0.2, V=0.0)
        eq = compute_equilibrium(DEFAULT_PARAMS, DEFAULT_THETA)

        conv = estimate_convergence(current, eq, DEFAULT_PARAMS)

        assert 'distance' in conv
        assert 'converged' in conv
        assert 'updates_to_convergence' in conv

        print(f"Distance to equilibrium: {conv['distance']:.4f}")
        print(f"Estimated updates to 95% convergence: {conv['updates_to_convergence']}")
