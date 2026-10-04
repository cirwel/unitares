"""
Tests for agent circuit breaker enforcement.

Verifies that paused/archived agents are blocked from performing operations.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime

import src.db as dbmod
import src.grounding.onboard_classifier as classifier
import src.mcp_handlers.context as ctxmod
import src.mcp_handlers.identity.handlers as id_handlers
import src.mcp_handlers.identity_bootstrap as ib
import src.mcp_handlers.support.pause_ttl as pause_ttl
from src.mcp_handlers.updates.context import UpdateContext
from src.mcp_handlers.updates.phases import resolve_identity_and_guards
from src.mcp_handlers.utils import check_agent_can_operate
from tests.helpers import parse_result


class TestCircuitBreakerEnforcement:
    """Tests for check_agent_can_operate function."""

    @pytest.fixture
    def mock_mcp_server(self):
        """Mock MCP server with agent_metadata."""
        mock_server = MagicMock()
        mock_server.agent_metadata = {}
        return mock_server

    def test_new_agent_can_operate(self, mock_mcp_server):
        """New agent (not in metadata) can operate."""
        with patch('src.mcp_handlers.shared.get_mcp_server', return_value=mock_mcp_server):
            result = check_agent_can_operate("new-agent-uuid")
            assert result is None  # None means can operate

    def test_active_agent_can_operate(self, mock_mcp_server):
        """Active agent can operate."""
        mock_meta = MagicMock()
        mock_meta.status = "active"
        mock_mcp_server.agent_metadata["active-agent"] = mock_meta

        with patch('src.mcp_handlers.shared.get_mcp_server', return_value=mock_mcp_server):
            result = check_agent_can_operate("active-agent")
            assert result is None  # None means can operate

    def test_paused_agent_blocked(self, mock_mcp_server):
        """Paused agent is blocked."""
        mock_meta = MagicMock()
        mock_meta.status = "paused"
        mock_meta.paused_at = datetime.now().isoformat()  # fresh — pause TTL would expire stale ones
        mock_mcp_server.agent_metadata["paused-agent"] = mock_meta

        with patch('src.mcp_handlers.shared.get_mcp_server', return_value=mock_mcp_server):
            result = check_agent_can_operate("paused-agent")
            assert result is not None  # TextContent error
            data = parse_result(result)
            assert data["success"] is False
            assert data["error_code"] == "AGENT_PAUSED"

    def test_archived_agent_blocked(self, mock_mcp_server):
        """Archived agent is blocked."""
        mock_meta = MagicMock()
        mock_meta.status = "archived"
        mock_mcp_server.agent_metadata["archived-agent"] = mock_meta

        with patch('src.mcp_handlers.shared.get_mcp_server', return_value=mock_mcp_server):
            result = check_agent_can_operate("archived-agent")
            assert result is not None  # TextContent error
            data = parse_result(result)
            assert data["success"] is False
            assert data["error_code"] == "AGENT_ARCHIVED"

    def test_paused_agent_error_has_recovery_guidance(self, mock_mcp_server):
        """Paused agent error includes recovery guidance."""
        mock_meta = MagicMock()
        mock_meta.status = "paused"
        mock_meta.paused_at = datetime.now().isoformat()  # fresh — pause TTL would expire stale ones
        mock_mcp_server.agent_metadata["paused-agent"] = mock_meta

        with patch('src.mcp_handlers.shared.get_mcp_server', return_value=mock_mcp_server):
            result = check_agent_can_operate("paused-agent")
            assert result is not None
            # Check recovery guidance is included
            assert "self_recovery" in result.text or "resume" in result.text.lower()


AGENT_UUID = "11111111-2222-3333-4444-555555555555"


def _patch_checkin_context(monkeypatch):
    """Resolve the check-in to AGENT_UUID with caller-asserted proof, so the
    strict write gate passes and the test reaches the pause guard."""
    monkeypatch.setattr(ctxmod, "get_context_agent_id", lambda: AGENT_UUID)
    monkeypatch.setattr(ctxmod, "get_context_session_key", lambda: "k")
    monkeypatch.setattr(ctxmod, "get_session_resolution_source",
                        lambda: "explicit_client_session_id")
    monkeypatch.setattr(ctxmod, "get_session_proof_origin", lambda: "caller_asserted")
    monkeypatch.setattr(ctxmod, "get_trajectory_confidence", lambda: None)
    monkeypatch.setattr(ib, "is_strict_identity_required", lambda: False)
    monkeypatch.setattr(classifier, "reconcile_resident_tags", AsyncMock(return_value=None))

    class _DB:
        async def update_agent_fields(self, *a, **k):
            return None
    monkeypatch.setattr(dbmod, "get_db", lambda: _DB())

    persisted = AsyncMock(return_value=False)
    monkeypatch.setattr(id_handlers, "ensure_agent_persisted", persisted)
    return persisted


def _checkin_ctx(status, paused_at=None):
    meta = SimpleNamespace(status=status, paused_at=paused_at, label="test-agent", tags=[])
    ctx = UpdateContext(arguments={"agent_id": AGENT_UUID})
    ctx.mcp_server = SimpleNamespace(agent_metadata={AGENT_UUID: meta})
    return ctx, meta


class TestCheckinPauseGuard:
    """The process_agent_update pause guard, run through the real identity phase.

    The knowledge-graph write paths gated by check_agent_can_operate are
    exercised through their handlers in test_kg_store.py (store, batch store),
    test_kg_lifecycle.py (note) and test_knowledge_authority.py (promotion).
    """

    @pytest.mark.asyncio
    async def test_paused_agent_checkin_refused_before_persistence(self, monkeypatch):
        persisted = _patch_checkin_context(monkeypatch)
        ctx, _ = _checkin_ctx("paused", paused_at=datetime.now().isoformat())

        result = await resolve_identity_and_guards(ctx)

        assert result is not None, "a paused agent's check-in must early-exit"
        data = parse_result(result)
        assert data["success"] is False
        assert data["error_code"] == "AGENT_PAUSED"
        persisted.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_expired_pause_lets_checkin_through(self, monkeypatch):
        """A pause past its TTL is cleared and the check-in is re-evaluated."""
        _patch_checkin_context(monkeypatch)
        expire = AsyncMock(return_value=True)
        monkeypatch.setattr(pause_ttl, "maybe_auto_expire_pause_async", expire)
        ctx, meta = _checkin_ctx("paused", paused_at="2026-01-01T00:00:00")

        result = await resolve_identity_and_guards(ctx)

        assert result is None
        expire.assert_awaited_once_with(AGENT_UUID, meta)

    @pytest.mark.asyncio
    async def test_archived_agent_checkin_not_refused_here(self, monkeypatch):
        """Archived agents pass this phase on purpose: the onboarding phase
        auto-resumes them on engagement, and refusing here blocked that."""
        _patch_checkin_context(monkeypatch)
        ctx, _ = _checkin_ctx("archived")

        assert await resolve_identity_and_guards(ctx) is None


class TestCircuitBreakerStates:
    """Tests for different agent states."""

    @pytest.fixture
    def mock_mcp_server(self):
        mock_server = MagicMock()
        mock_server.agent_metadata = {}
        return mock_server

    def test_all_valid_statuses(self, mock_mcp_server):
        """Test all expected agent statuses."""
        statuses_and_expected = [
            ("active", None),      # Can operate
            ("paused", "blocked"), # Blocked
            ("archived", "blocked"), # Blocked
        ]

        for status, expected in statuses_and_expected:
            mock_meta = MagicMock()
            mock_meta.status = status
            mock_meta.paused_at = datetime.now().isoformat() if status == "paused" else None  # fresh — pause TTL would expire stale ones
            mock_mcp_server.agent_metadata["test-agent"] = mock_meta

            with patch('src.mcp_handlers.shared.get_mcp_server', return_value=mock_mcp_server):
                result = check_agent_can_operate("test-agent")
                if expected is None:
                    assert result is None, f"Status '{status}' should allow operation"
                else:
                    assert result is not None, f"Status '{status}' should block operation"
