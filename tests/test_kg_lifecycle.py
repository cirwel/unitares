"""
Tests for src/mcp_handlers/knowledge_graph.py - comprehensive handler coverage.

Tests cover:
- handle_store_knowledge_graph (single + batch)
- handle_search_knowledge_graph
- handle_get_knowledge_graph
- handle_list_knowledge_graph
- handle_update_discovery_status_graph
- handle_get_discovery_details
- handle_leave_note
- handle_cleanup_knowledge_graph
- handle_get_lifecycle_stats
- handle_answer_question
- _discovery_not_found helper
- _check_display_name_required helper
- _resolve_agent_display helper
"""

import pytest
import json
import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.knowledge_graph import DiscoveryNode, ResponseTo


# ============================================================================
# Shared helpers
# ============================================================================

from tests.helpers import parse_result


def make_discovery(
    id="disc-1",
    agent_id="test-agent",
    type="note",
    summary="Test discovery",
    details="Some details",
    tags=None,
    severity="low",
    status="open",
    response_to=None,
    provenance=None,
    provenance_chain=None,
) -> DiscoveryNode:
    """Create a DiscoveryNode for testing."""
    return DiscoveryNode(
        id=id,
        agent_id=agent_id,
        type=type,
        summary=summary,
        details=details,
        tags=tags or [],
        severity=severity,
        status=status,
        response_to=response_to,
        provenance=provenance,
        provenance_chain=provenance_chain,
    )


# ============================================================================
# Shared fixtures
# ============================================================================

@pytest.fixture
def mock_mcp_server():
    """Mock the shared mcp_server module."""
    server = MagicMock()
    server.agent_metadata = {}
    server.monitors = {}

    return server


@pytest.fixture
def mock_graph():
    """Mock knowledge graph backend."""
    graph = AsyncMock()
    graph.add_discovery = AsyncMock(return_value=True)
    graph.find_similar = AsyncMock(return_value=[])
    graph.query = AsyncMock(return_value=[])
    graph.get_discovery = AsyncMock(return_value=None)
    graph.get_agent_discoveries = AsyncMock(return_value=[])
    graph.get_stats = AsyncMock(return_value={"total_discoveries": 0, "total_agents": 0})
    graph.update_discovery = AsyncMock(return_value=True)
    graph.full_text_search = AsyncMock(return_value=[])
    graph._get_db = AsyncMock()
    return graph


@pytest.fixture
def patch_common(mock_mcp_server, mock_graph):
    """Patch all common dependencies for knowledge graph handlers."""
    with patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
         patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_mcp_server), \
         patch("src.mcp_handlers.knowledge.handlers.mcp_server", mock_mcp_server), \
         patch("src.mcp_handlers.knowledge.handlers.get_knowledge_graph", new_callable=AsyncMock, return_value=mock_graph), \
         patch("src.mcp_handlers.knowledge.handlers.record_ms"):
        yield mock_mcp_server, mock_graph


@pytest.fixture
def registered_agent(mock_mcp_server):
    """Register a test agent in the mock server's metadata.

    Uses a valid UUID4 as the key so require_registered_agent can find it
    via direct UUID lookup in agent_metadata.
    """
    import uuid
    agent_uuid = str(uuid.uuid4())
    meta = MagicMock()
    meta.status = "active"
    meta.health_status = "healthy"
    meta.total_updates = 5
    meta.label = "TestAgent"
    meta.display_name = "TestAgent"
    meta.structured_id = "test_agent_opus"
    meta.parent_agent_id = None
    meta.spawn_reason = None
    meta.created_at = "2026-01-01T00:00:00"
    meta.paused_at = None
    mock_mcp_server.agent_metadata[agent_uuid] = meta
    return agent_uuid


# ============================================================================
# handle_store_knowledge_graph
# ============================================================================

class TestUpdateDiscoveryStatusGraph:

    @pytest.mark.asyncio
    async def test_update_happy_path(self, patch_common, registered_agent):
        """Update discovery status successfully."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        disc = make_discovery(id="2026-01-01T00:00:00.000000", severity="low", agent_id=registered_agent)
        mock_graph.get_discovery = AsyncMock(return_value=disc)
        mock_graph.update_discovery = AsyncMock(return_value=True)

        result = await handle_update_discovery_status_graph({
            "agent_id": registered_agent,
            "discovery_id": "2026-01-01T00:00:00.000000",
            "status": "resolved",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert "resolved" in data["message"]

    @pytest.mark.asyncio
    async def test_update_rejects_toolcall_markup(self, patch_common, registered_agent):
        """KG 2026-06-13 footgun: the update path gets the same degenerate-write
        guard as store — an edit whose content absorbed tool-call markup is
        rejected, not silently persisted."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        result = await handle_update_discovery_status_graph({
            "agent_id": registered_agent,
            "discovery_id": "2026-01-01T00:00:00.000000",
            "content": 'edited <parameter name="tags">["x"]</parameter>',
        })

        data = parse_result(result)
        assert data["success"] is False
        assert data["error_code"] == "degenerate_write_rejected"

    @pytest.mark.asyncio
    async def test_update_missing_discovery_id(self, patch_common, registered_agent):
        """Update fails when discovery_id is missing."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        result = await handle_update_discovery_status_graph({
            "agent_id": registered_agent,
            "status": "resolved",
        })

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_update_missing_status(self, patch_common, registered_agent):
        """Update fails when status is missing."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        result = await handle_update_discovery_status_graph({
            "agent_id": registered_agent,
            "discovery_id": "2026-01-01T00:00:00.000000",
        })

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_update_discovery_not_found(self, patch_common, registered_agent):
        """Update fails when discovery doesn't exist."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        mock_graph.get_discovery = AsyncMock(return_value=None)
        # Mock _get_db for the _discovery_not_found helper
        mock_db = AsyncMock()
        mock_db.graph_query = AsyncMock(return_value=[])
        mock_graph._get_db = AsyncMock(return_value=mock_db)

        result = await handle_update_discovery_status_graph({
            "agent_id": registered_agent,
            "discovery_id": "2026-01-01T00:00:00.000000",
            "status": "resolved",
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_update_low_severity_unregistered_allowed(self, patch_common):
        """Low/medium updates accept an unregistered writer (mirrors store).

        Credential Loop Asymmetry fix: store() lets unregistered / low-friction
        callers create low+medium rows, so update() must let them edit those
        rows too — otherwise a caller can create a row it cannot maintain
        without re-running onboard() (which mints a sibling identity).
        """
        mock_mcp_server, mock_graph = patch_common
        mock_graph.get_discovery = AsyncMock(return_value=make_discovery(severity="low"))
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        result = await handle_update_discovery_status_graph({
            "agent_id": "unregistered",
            "discovery_id": "disc-1",
            "status": "resolved",
        })

        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_update_high_severity_unregistered_rejected(self, patch_common):
        """High/critical updates still require a registered agent (unchanged).

        The Credential Loop Asymmetry fix only relaxes the low/medium gate; the
        security boundary on high+critical rows is preserved.
        """
        mock_mcp_server, mock_graph = patch_common
        mock_graph.get_discovery = AsyncMock(return_value=make_discovery(severity="critical"))
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        result = await handle_update_discovery_status_graph({
            "agent_id": "unregistered",
            "discovery_id": "disc-1",
            "status": "resolved",
        })

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_update_invalid_status(self, patch_common, registered_agent):
        """Update fails with invalid status value."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        result = await handle_update_discovery_status_graph({
            "agent_id": registered_agent,
            "discovery_id": "2026-01-01T00:00:00.000000",
            "status": "invalid_status",
        })

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_update_exception_handling(self, patch_common, registered_agent):
        """Exception from graph backend returns error."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        mock_graph.get_discovery = AsyncMock(side_effect=Exception("Connection error"))

        result = await handle_update_discovery_status_graph({
            "agent_id": registered_agent,
            "discovery_id": "2026-01-01T00:00:00.000000",
            "status": "resolved",
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "failed to update" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_update_resolved_sets_timestamp(self, patch_common, registered_agent):
        """Updating to 'resolved' sets resolved_at timestamp."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        disc = make_discovery(id="2026-01-01T00:00:00.000000", severity="low", agent_id=registered_agent)
        mock_graph.get_discovery = AsyncMock(return_value=disc)
        mock_graph.update_discovery = AsyncMock(return_value=True)

        result = await handle_update_discovery_status_graph({
            "agent_id": registered_agent,
            "discovery_id": "2026-01-01T00:00:00.000000",
            "status": "resolved",
        })

        # Verify update_discovery was called with resolved_at
        call_args = mock_graph.update_discovery.call_args
        updates = call_args[0][1]
        assert "resolved_at" in updates
        assert updates["status"] == "resolved"


# ============================================================================
# handle_get_discovery_details
# ============================================================================

class TestGetDiscoveryDetails:

    @pytest.mark.asyncio
    async def test_get_details_happy_path(self, patch_common):
        """Get full details for a discovery."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_get_discovery_details

        disc = make_discovery(id="2026-01-01T00:00:00.000000", details="Full details content here")
        mock_graph.get_discovery = AsyncMock(return_value=disc)

        result = await handle_get_discovery_details({
            "discovery_id": "2026-01-01T00:00:00.000000",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert "discovery" in data
        assert "Full details" in data["message"]

    @pytest.mark.asyncio
    async def test_get_details_missing_id(self, patch_common):
        """Get details fails when discovery_id is missing."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_get_discovery_details

        result = await handle_get_discovery_details({})

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_get_details_not_found(self, patch_common):
        """Get details for nonexistent discovery returns error."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_get_discovery_details

        mock_graph.get_discovery = AsyncMock(return_value=None)
        # Mock _get_db for the _discovery_not_found helper
        mock_db = AsyncMock()
        mock_db.graph_query = AsyncMock(return_value=[])
        mock_graph._get_db = AsyncMock(return_value=mock_db)

        result = await handle_get_discovery_details({
            "discovery_id": "2026-01-01T00:00:00.000000",
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_get_details_with_pagination(self, patch_common):
        """Get details with pagination (offset/length)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_get_discovery_details

        long_details = "A" * 5000
        disc = make_discovery(id="2026-01-01T00:00:00.000000", details=long_details)
        mock_graph.get_discovery = AsyncMock(return_value=disc)

        result = await handle_get_discovery_details({
            "discovery_id": "2026-01-01T00:00:00.000000",
            "offset": 100,
            "length": 500,
        })

        data = parse_result(result)
        assert data["success"] is True
        assert "pagination" in data
        assert data["pagination"]["offset"] == 100
        assert data["pagination"]["total_length"] == 5000
        assert data["pagination"]["has_more"] is True

    @pytest.mark.asyncio
    async def test_get_details_short_content_no_pagination(self, patch_common):
        """Short details don't trigger pagination."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_get_discovery_details

        disc = make_discovery(id="2026-01-01T00:00:00.000000", details="Short content")
        mock_graph.get_discovery = AsyncMock(return_value=disc)

        result = await handle_get_discovery_details({
            "discovery_id": "2026-01-01T00:00:00.000000",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert "pagination" not in data

    @pytest.mark.asyncio
    async def test_get_details_with_response_chain(self, patch_common):
        """Get details with response chain traversal."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_get_discovery_details

        disc = make_discovery(id="2026-01-01T00:00:00.000000", details="Details")
        chain_disc = make_discovery(id="2026-01-02T00:00:00.000000", summary="Response")
        mock_graph.get_discovery = AsyncMock(return_value=disc)
        mock_graph.get_response_chain = AsyncMock(return_value=[disc, chain_disc])

        result = await handle_get_discovery_details({
            "discovery_id": "2026-01-01T00:00:00.000000",
            "include_response_chain": True,
        })

        data = parse_result(result)
        assert data["success"] is True
        assert "response_chain" in data
        assert data["response_chain"]["count"] == 2

    @pytest.mark.asyncio
    async def test_get_details_response_chain_not_supported(self, patch_common):
        """Response chain gracefully handles unsupported backend."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_get_discovery_details

        disc = make_discovery(id="2026-01-01T00:00:00.000000", details="Details")
        mock_graph.get_discovery = AsyncMock(return_value=disc)
        # Remove get_response_chain to simulate unsupported backend
        del mock_graph.get_response_chain

        result = await handle_get_discovery_details({
            "discovery_id": "2026-01-01T00:00:00.000000",
            "include_response_chain": True,
        })

        data = parse_result(result)
        assert data["success"] is True
        assert "response_chain" in data
        assert "error" in data["response_chain"]

    @pytest.mark.asyncio
    async def test_get_details_exception_handling(self, patch_common):
        """Exception from graph backend returns error."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_get_discovery_details

        mock_graph.get_discovery = AsyncMock(side_effect=Exception("Timeout"))

        result = await handle_get_discovery_details({
            "discovery_id": "2026-01-01T00:00:00.000000",
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "failed to get discovery" in data["error"].lower()


# ============================================================================
# handle_leave_note
# ============================================================================

class TestLeaveNote:

    @pytest.mark.asyncio
    async def test_leave_note_happy_path(self, patch_common, registered_agent):
        """Leave a note successfully."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        result = await handle_leave_note({
            "agent_id": registered_agent,
            "summary": "Quick observation about caching",
            "tags": ["cache"],
        })

        data = parse_result(result)
        assert data["success"] is True
        assert "note_id" in data
        assert data["visibility"] == "shared"
        mock_graph.add_discovery.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_leave_note_missing_text(self, patch_common, registered_agent):
        """Leave note fails without content."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        result = await handle_leave_note({
            "agent_id": registered_agent,
        })

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_leave_note_param_aliases(self, patch_common, registered_agent):
        """Note content can use aliases (text, note, content, etc.)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        # "text" is an alias for "summary"
        result = await handle_leave_note({
            "agent_id": registered_agent,
            "text": "Using text alias",
        })

        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_leave_note_truncation(self, patch_common, registered_agent):
        """Long notes split at MAX_SUMMARY_LEN into summary + details."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note
        from src.mcp_handlers.knowledge.limits import MAX_SUMMARY_LEN

        long_text = "X" * (MAX_SUMMARY_LEN + 200)
        result = await handle_leave_note({
            "agent_id": registered_agent,
            "summary": long_text,
        })

        data = parse_result(result)
        assert data["success"] is True
        # The stored discovery's summary should fit the limit (may have ellipsis)
        call_args = mock_graph.add_discovery.call_args
        discovery = call_args[0][0]
        assert len(discovery.summary) <= MAX_SUMMARY_LEN + 4  # cap + "..."

    @pytest.mark.asyncio
    async def test_leave_note_auto_links_with_tags(self, patch_common, registered_agent):
        """Notes with tags auto-link to similar discoveries."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        similar = make_discovery(id="similar-1")
        mock_graph.find_similar = AsyncMock(return_value=[similar])

        result = await handle_leave_note({
            "agent_id": registered_agent,
            "summary": "Tagged note",
            "tags": ["important"],
        })

        data = parse_result(result)
        assert data["success"] is True
        mock_graph.find_similar.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_leave_note_unregistered_agent(self, patch_common):
        """Leave note without binding uses anonymous writer mode."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        result = await handle_leave_note({
            "summary": "Anonymous note",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["agent_mode"] == "anonymous"
        discovery = mock_graph.add_discovery.call_args[0][0]
        assert discovery.agent_id.startswith("anonkg_")

    @pytest.mark.asyncio
    async def test_leave_note_paused_agent(self, patch_common, registered_agent, mock_mcp_server):
        """Paused agents cannot leave notes (circuit breaker)."""
        mock_mcp_server.agent_metadata[registered_agent].status = "paused"
        # Fresh paused_at — pause TTL auto-expires stale ones (>72h default)
        from datetime import datetime as _dt
        mock_mcp_server.agent_metadata[registered_agent].paused_at = _dt.now().isoformat()

        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        result = await handle_leave_note({
            "agent_id": registered_agent,
            "summary": "Should be blocked",
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "paused" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_leave_note_with_response_to(self, patch_common, registered_agent):
        """Leave note with response_to for threading."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        result = await handle_leave_note({
            "agent_id": registered_agent,
            "summary": "A threaded note",
            "response_to": {
                "discovery_id": "2026-01-01T00:00:00.000000",
                "response_type": "extend",
            },
        })

        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_leave_note_exception_handling(self, patch_common, registered_agent):
        """Exception from graph backend returns error."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        mock_graph.add_discovery = AsyncMock(side_effect=Exception("Write error"))

        result = await handle_leave_note({
            "agent_id": registered_agent,
            "summary": "Will fail",
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "failed to leave note" in data["error"].lower()


# ============================================================================
# handle_cleanup_knowledge_graph
# ============================================================================

class TestCleanupKnowledgeGraph:

    @pytest.mark.asyncio
    async def test_cleanup_dry_run(self, patch_common):
        """Cleanup in dry run mode previews changes without applying."""
        mock_mcp_server, mock_graph = patch_common

        mock_cleanup = AsyncMock(return_value={"archived": 3, "total_processed": 10})
        # The import is local: from src.knowledge_graph_lifecycle import run_kg_lifecycle_cleanup
        import src.knowledge_graph_lifecycle as lifecycle_mod
        with patch.object(lifecycle_mod, "run_kg_lifecycle_cleanup", mock_cleanup):
            from src.mcp_handlers.knowledge.handlers import handle_cleanup_knowledge_graph
            result = await handle_cleanup_knowledge_graph({"dry_run": True})

        data = parse_result(result)
        assert data["success"] is True
        assert "DRY RUN" in data["message"]

    @pytest.mark.asyncio
    async def test_cleanup_execute(self, patch_common):
        """Cleanup actually executes when dry_run=False."""
        mock_mcp_server, mock_graph = patch_common

        mock_cleanup = AsyncMock(return_value={"archived": 5, "total_processed": 20})
        import src.knowledge_graph_lifecycle as lifecycle_mod
        with patch.object(lifecycle_mod, "run_kg_lifecycle_cleanup", mock_cleanup):
            from src.mcp_handlers.knowledge.handlers import handle_cleanup_knowledge_graph
            result = await handle_cleanup_knowledge_graph({"dry_run": False})

        data = parse_result(result)
        assert data["success"] is True
        assert "DRY RUN" not in data["message"]

    @pytest.mark.asyncio
    async def test_cleanup_execute_with_string_false(self, patch_common):
        """String dry_run=false from SDK/consolidated callers must execute."""
        mock_mcp_server, mock_graph = patch_common

        mock_cleanup = AsyncMock(return_value={"archived": 5, "total_processed": 20})
        import src.knowledge_graph_lifecycle as lifecycle_mod
        with patch.object(lifecycle_mod, "run_kg_lifecycle_cleanup", mock_cleanup):
            from src.mcp_handlers.knowledge.handlers import handle_cleanup_knowledge_graph
            result = await handle_cleanup_knowledge_graph({"dry_run": "false"})

        mock_cleanup.assert_awaited_once_with(dry_run=False)
        data = parse_result(result)
        assert data["success"] is True
        assert "DRY RUN" not in data["message"]

    @pytest.mark.asyncio
    async def test_cleanup_defaults_to_dry_run(self, patch_common):
        """Cleanup defaults to dry_run=True when not specified."""
        mock_mcp_server, mock_graph = patch_common

        mock_cleanup = AsyncMock(return_value={"archived": 0})
        import src.knowledge_graph_lifecycle as lifecycle_mod
        with patch.object(lifecycle_mod, "run_kg_lifecycle_cleanup", mock_cleanup):
            from src.mcp_handlers.knowledge.handlers import handle_cleanup_knowledge_graph
            result = await handle_cleanup_knowledge_graph({})

        mock_cleanup.assert_awaited_once_with(dry_run=True)

    @pytest.mark.asyncio
    async def test_cleanup_exception_handling(self, patch_common):
        """Exception from lifecycle cleanup returns error."""
        mock_mcp_server, mock_graph = patch_common

        mock_cleanup = AsyncMock(side_effect=Exception("Cleanup failed"))
        import src.knowledge_graph_lifecycle as lifecycle_mod
        with patch.object(lifecycle_mod, "run_kg_lifecycle_cleanup", mock_cleanup):
            from src.mcp_handlers.knowledge.handlers import handle_cleanup_knowledge_graph
            result = await handle_cleanup_knowledge_graph({})

        data = parse_result(result)
        assert data["success"] is False
        assert "failed to run lifecycle" in data["error"].lower()


# ============================================================================
# handle_get_lifecycle_stats
# ============================================================================

class TestGetLifecycleStats:

    @pytest.mark.asyncio
    async def test_lifecycle_stats_happy_path(self, patch_common):
        """Get lifecycle stats successfully."""
        mock_mcp_server, mock_graph = patch_common

        stats_data = {
            "by_status": {"open": 10, "resolved": 5, "archived": 2},
            "by_policy": {"permanent": 3, "standard": 12, "ephemeral": 2},
            "total_discoveries": 17,
        }
        mock_graph.get_stats = AsyncMock(return_value={
            "total_discoveries": 12,
            "by_status": {"open": 7, "resolved": 5},
            "scope": {"kind": "raw_status_aggregate", "epoch_scope": "current"},
        })
        import src.knowledge_graph_lifecycle as lifecycle_mod
        with patch.object(lifecycle_mod, "get_kg_lifecycle_stats",
                          AsyncMock(return_value=stats_data)):
            from src.mcp_handlers.knowledge.handlers import handle_get_lifecycle_stats
            result = await handle_get_lifecycle_stats({})

        data = parse_result(result)
        assert data["success"] is True
        assert "stats" in data
        assert data["stats"]["by_status"]["open"] == 10
        assert data["stats"]["raw_current_counts"]["by_status"]["open"] == 7
        assert "count_scope_warning" in data["stats"]

    @pytest.mark.asyncio
    async def test_lifecycle_stats_exception_handling(self, patch_common):
        """Exception returns error response."""
        mock_mcp_server, mock_graph = patch_common

        import src.knowledge_graph_lifecycle as lifecycle_mod
        with patch.object(lifecycle_mod, "get_kg_lifecycle_stats",
                          AsyncMock(side_effect=Exception("Stats unavailable"))):
            from src.mcp_handlers.knowledge.handlers import handle_get_lifecycle_stats
            result = await handle_get_lifecycle_stats({})

        data = parse_result(result)
        assert data["success"] is False
        assert "failed to get lifecycle" in data["error"].lower()


# ============================================================================
# handle_answer_question
# ============================================================================

class TestEdgeCases:

    @pytest.mark.asyncio
    async def test_store_with_invalid_severity(self, patch_common, registered_agent):
        """Invalid severity returns validation error."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "Test",
            "severity": "super_critical",
        })

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_store_with_empty_summary(self, patch_common, registered_agent):
        """Empty string summary is treated as missing."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": None,
        })

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_search_limit_respected(self, patch_common):
        """Custom limit parameter is respected in search."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        mock_graph.query = AsyncMock(return_value=[])

        result = await handle_search_knowledge_graph({
            "limit": 5,
        })

        mock_graph.query.assert_awaited_once()
        call_kwargs = mock_graph.query.call_args
        assert call_kwargs[1]["limit"] == 5

    @pytest.mark.asyncio
    async def test_leave_note_sets_type_to_note(self, patch_common, registered_agent):
        """Leave note always creates discoveries with type='note'."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        result = await handle_leave_note({
            "agent_id": registered_agent,
            "summary": "A quick note",
        })

        data = parse_result(result)
        assert data["success"] is True
        call_args = mock_graph.add_discovery.call_args
        discovery = call_args[0][0]
        assert discovery.type == "note"
        assert discovery.severity == "low"

    @pytest.mark.asyncio
    async def test_store_no_auto_link(self, patch_common, registered_agent):
        """Store with auto_link_related=False skips similarity search."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "No linking please",
            "auto_link_related": False,
        })

        data = parse_result(result)
        assert data["success"] is True
        mock_graph.find_similar.assert_not_awaited()
        assert "related_discoveries" not in data

    @pytest.mark.asyncio
    async def test_get_details_response_chain_error(self, patch_common):
        """Response chain traversal error is non-fatal."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_get_discovery_details

        disc = make_discovery(id="2026-01-01T00:00:00.000000", details="Details")
        mock_graph.get_discovery = AsyncMock(return_value=disc)
        mock_graph.get_response_chain = AsyncMock(side_effect=Exception("Chain broken"))

        result = await handle_get_discovery_details({
            "discovery_id": "2026-01-01T00:00:00.000000",
            "include_response_chain": True,
        })

        data = parse_result(result)
        assert data["success"] is True  # Main request succeeded
        assert "response_chain" in data
        assert "error" in data["response_chain"]  # Chain error is noted


# ============================================================================
# _discovery_not_found - additional suggestions paths
# ============================================================================

class TestUpdateDiscoveryStatusAdditional:

    @pytest.mark.asyncio
    async def test_update_high_severity_requires_auth(self, patch_common, registered_agent):
        """High severity update requires auth (lines 1018-1033)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        disc = make_discovery(
            id="2026-01-01T00:00:00.000000",
            severity="high",
            agent_id=registered_agent,
        )
        mock_graph.get_discovery = AsyncMock(return_value=disc)

        with patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=False):
            result = await handle_update_discovery_status_graph({
                "agent_id": registered_agent,
                "discovery_id": "2026-01-01T00:00:00.000000",
                "status": "resolved",
            })

            data = parse_result(result)
            assert data["success"] is False
            assert "auth" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_update_high_severity_non_owner_reopen_denied(self, patch_common, registered_agent):
        """Non-owner cannot reopen high severity discovery (lines 1032-1033)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        disc = make_discovery(
            id="2026-01-01T00:00:00.000000",
            severity="critical",
            agent_id="other-agent",  # Different owner
        )
        mock_graph.get_discovery = AsyncMock(return_value=disc)

        with patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True):
            result = await handle_update_discovery_status_graph({
                "agent_id": registered_agent,
                "discovery_id": "2026-01-01T00:00:00.000000",
                "status": "open",  # Reopening - denied for non-owners
            })

            data = parse_result(result)
            assert data["success"] is False
            assert "permission" in data["error"].lower()
            assert "resolved" in data["error"]
            assert "closed" in data["error"]
            assert "wont_fix" in data["error"]

    @pytest.mark.asyncio
    async def test_update_high_severity_non_owner_cannot_edit_details_while_resolving(self, patch_common, registered_agent):
        """Non-owner cannot change content on a high severity discovery, even with an allowed closing status."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        disc = make_discovery(
            id="2026-01-01T00:00:00.000000",
            severity="critical",
            agent_id="other-agent",
        )
        mock_graph.get_discovery = AsyncMock(return_value=disc)

        with patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True):
            result = await handle_update_discovery_status_graph({
                "agent_id": registered_agent,
                "discovery_id": "2026-01-01T00:00:00.000000",
                "status": "resolved",
                "details": "quietly changing the body",
            })

            data = parse_result(result)
            assert data["success"] is False
            assert "cannot edit" in data["error"].lower()
            mock_graph.update_discovery.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_success_returns_false(self, patch_common, registered_agent):
        """Update returning False triggers not found error (line 1049)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        disc = make_discovery(id="2026-01-01T00:00:00.000000", severity="low", agent_id=registered_agent)
        mock_graph.get_discovery = AsyncMock(return_value=disc)
        mock_graph.update_discovery = AsyncMock(return_value=False)

        result = await handle_update_discovery_status_graph({
            "agent_id": registered_agent,
            "discovery_id": "2026-01-01T00:00:00.000000",
            "status": "archived",
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower()


# ============================================================================
# handle_get_discovery_details - additional coverage
# ============================================================================

class TestGetDiscoveryDetailsAdditional:

    @pytest.mark.asyncio
    async def test_get_details_validate_discovery_id_error(self, patch_common):
        """Invalid discovery_id format returns error (line 1084)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_get_discovery_details

        # Pass an invalid discovery_id format (depends on validator)
        result = await handle_get_discovery_details({
            "discovery_id": "",  # Empty string
        })

        data = parse_result(result)
        assert data["success"] is False


# ============================================================================
# handle_answer_question - additional coverage
# ============================================================================

class TestLeaveNoteAdditional:

    @pytest.mark.asyncio
    async def test_leave_note_response_to_invalid_id(self, patch_common, registered_agent):
        """Leave note with invalid response_to discovery_id returns error (line 1474)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        result = await handle_leave_note({
            "agent_id": registered_agent,
            "summary": "Note with bad response_to",
            "response_to": {
                "discovery_id": "",  # Invalid empty ID
                "response_type": "extend",
            },
        })

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_leave_note_response_to_invalid_type(self, patch_common, registered_agent):
        """Leave note with invalid response_type returns error (line 1479)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        result = await handle_leave_note({
            "agent_id": registered_agent,
            "summary": "Note with bad response type",
            "response_to": {
                "discovery_id": "2026-01-01T00:00:00.000000",
                "response_type": "invalid_type",
            },
        })

        data = parse_result(result)
        assert data["success"] is False


# ============================================================================
# Batch store - additional coverage
# ============================================================================

class TestSupersedeHandler:

    @pytest.mark.asyncio
    async def test_supersede_success(self, patch_common):
        """Should create SUPERSEDES edge via handler."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_supersede_discovery

        mock_graph.supersede_discovery = AsyncMock(return_value={
            "success": True,
            "new_id": "new-1",
            "old_id": "old-1",
            "message": "Superseded",
        })

        result = await handle_supersede_discovery({
            "discovery_id": "new-1",
            "supersedes_id": "old-1",
        })
        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_supersede_missing_params(self, patch_common):
        """Should fail when required params are missing."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_supersede_discovery

        result = await handle_supersede_discovery({"discovery_id": "new-1"})
        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_supersede_no_age_backend(self, patch_common):
        """Should fail gracefully when AGE backend not available."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_supersede_discovery

        # Remove supersede_discovery to simulate non-AGE backend
        if hasattr(mock_graph, 'supersede_discovery'):
            del mock_graph.supersede_discovery

        result = await handle_supersede_discovery({
            "discovery_id": "new-1",
            "supersedes_id": "old-1",
        })
        data = parse_result(result)
        assert data["success"] is False


class TestUpdateDiscoveryExtended:

    @pytest.mark.asyncio
    async def test_update_accepts_details_without_status(self, patch_common, registered_agent):
        """Details-only updates should be allowed for amending discoveries."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        discovery = make_discovery(id="disc-1", severity="low", details="old")
        mock_graph.get_discovery = AsyncMock(side_effect=[discovery, discovery])

        result = await handle_update_discovery_status_graph({
            "agent_id": registered_agent,
            "discovery_id": "disc-1",
            "details": "new details",
        })

        data = parse_result(result)
        assert data["success"] is True
        updates = mock_graph.update_discovery.call_args[0][1]
        assert updates["details"] == "new details"
        assert "status" not in updates

    @pytest.mark.asyncio
    async def test_update_uses_content_alias_for_details(self, patch_common, registered_agent):
        """content should behave as an alias for details during update."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        discovery = make_discovery(id="disc-1", severity="low", details="old")
        mock_graph.get_discovery = AsyncMock(side_effect=[discovery, discovery])

        result = await handle_update_discovery_status_graph({
            "agent_id": registered_agent,
            "discovery_id": "disc-1",
            "content": "aliased details",
        })

        data = parse_result(result)
        assert data["success"] is True
        updates = mock_graph.update_discovery.call_args[0][1]
        assert updates["details"] == "aliased details"

    @pytest.mark.asyncio
    async def test_update_appends_resolution_notes(self, patch_common, registered_agent):
        """resolution_notes should allow one-call close-with-rationale."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        discovery = make_discovery(id="disc-1", severity="low", details="existing details")
        mock_graph.get_discovery = AsyncMock(side_effect=[discovery, discovery])

        result = await handle_update_discovery_status_graph({
            "agent_id": registered_agent,
            "discovery_id": "disc-1",
            "status": "resolved",
            "resolution_notes": "Fixed by current KG UX pass.",
        })

        data = parse_result(result)
        assert data["success"] is True
        updates = mock_graph.update_discovery.call_args[0][1]
        assert updates["status"] == "resolved"
        assert updates["resolved_at"]
        assert "existing details" in updates["details"]
        assert "Resolution notes (" in updates["details"]
        assert "Fixed by current KG UX pass." in updates["details"]

    @pytest.mark.asyncio
    async def test_update_rejects_blank_resolution_notes_as_only_update(self, patch_common, registered_agent):
        """Blank resolution_notes should not create an empty update."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        result = await handle_update_discovery_status_graph({
            "agent_id": registered_agent,
            "discovery_id": "disc-1",
            "resolution_notes": "   ",
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "At least one updatable field is required" in data["error"]
        mock_graph.get_discovery.assert_not_called()
