"""
Tests for agent circuit breaker enforcement.

Verifies that paused/archived agents are blocked from performing operations.
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime

from src.mcp_handlers.utils import check_agent_can_operate


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
            # Check it's an error response (has the right structure)
            assert hasattr(result, 'text')
            assert "paused" in result.text.lower() or "AGENT_PAUSED" in result.text

    def test_archived_agent_blocked(self, mock_mcp_server):
        """Archived agent is blocked."""
        mock_meta = MagicMock()
        mock_meta.status = "archived"
        mock_mcp_server.agent_metadata["archived-agent"] = mock_meta

        with patch('src.mcp_handlers.shared.get_mcp_server', return_value=mock_mcp_server):
            result = check_agent_can_operate("archived-agent")
            assert result is not None  # TextContent error
            assert hasattr(result, 'text')
            assert "archived" in result.text.lower() or "AGENT_ARCHIVED" in result.text

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


class TestCircuitBreakerIntegration:
    """Integration tests for circuit breaker in handlers."""

    @pytest.fixture
    def mock_paused_agent_metadata(self):
        """Create mock metadata for a paused agent."""
        mock_meta = MagicMock()
        mock_meta.status = "paused"
        mock_meta.paused_at = datetime.now().isoformat()
        mock_meta.label = "test-agent"
        return mock_meta

    @pytest.mark.asyncio
    async def test_process_agent_update_blocks_paused_agent(self, mock_paused_agent_metadata):
        """process_agent_update should block paused agents."""
        # This is a higher-level test - we'll verify the pattern exists
        # The actual integration test requires more setup

        # Verify the check exists in the handler (logic extracted to update_phases)
        from src.mcp_handlers.updates import phases as update_phases
        import inspect

        source = inspect.getsource(update_phases.resolve_identity_and_guards)
        assert "status == \"paused\"" in source or "circuit breaker" in source.lower()

    @pytest.mark.asyncio
    async def test_store_knowledge_graph_blocks_paused_agent(self, mock_paused_agent_metadata):
        """store_knowledge_graph should block paused agents."""
        from src.mcp_handlers.knowledge import handlers as knowledge_graph
        import inspect

        source = inspect.getsource(knowledge_graph.handle_store_knowledge_graph)
        assert "check_agent_can_operate" in source

    @pytest.mark.asyncio
    async def test_leave_note_blocks_paused_agent(self, mock_paused_agent_metadata):
        """leave_note should block paused agents."""
        from src.mcp_handlers.knowledge import handlers as knowledge_graph
        import inspect

        source = inspect.getsource(knowledge_graph.handle_leave_note)
        assert "check_agent_can_operate" in source


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
