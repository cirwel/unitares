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
from types import SimpleNamespace
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
    recall_event_recorder = MagicMock()
    with patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
         patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_mcp_server), \
         patch("src.mcp_handlers.knowledge.handlers.mcp_server", mock_mcp_server), \
         patch("src.mcp_handlers.knowledge.handlers.get_knowledge_graph", new_callable=AsyncMock, return_value=mock_graph), \
         patch("src.mcp_handlers.knowledge.handlers.record_recall_event", recall_event_recorder), \
         patch("src.mcp_handlers.knowledge.handlers.record_ms"):
        mock_mcp_server.recall_event_recorder = recall_event_recorder
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

class TestSearchKnowledgeGraph:

    @pytest.mark.asyncio
    async def test_search_no_filters(self, patch_common):
        """Search with no filters returns indexed filter results."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        discoveries = [make_discovery(id=f"d-{i}", summary=f"Item {i}") for i in range(3)]
        mock_graph.query = AsyncMock(return_value=discoveries)

        result = await handle_search_knowledge_graph({})

        data = parse_result(result)
        assert data["success"] is True
        assert data["count"] == 3
        assert data["search_mode_used"] == "indexed_filters"

    @pytest.mark.asyncio
    async def test_search_coalesces_none_min_similarity(self, patch_common):
        """Regression (dogfood 2026-06-27): the knowledge schema defaults
        min_similarity to None, so arguments.get('min_similarity', 0.3) returned
        None (key present, not absent) and semantic_search crashed at
        `similarity < min_similarity` with "'<' not supported between instances of
        'float' and 'NoneType'". The handler must coalesce None -> 0.3 before
        calling semantic_search."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        mock_graph.semantic_search = AsyncMock(return_value=[])
        mock_graph.full_text_search = AsyncMock(return_value=[])

        # arguments as the validated schema produces them when the caller does not
        # set a threshold: the key is present with value None.
        result = await handle_search_knowledge_graph(
            {"query": "MagicDNS", "limit": 3, "min_similarity": None,
             "search_mode": "semantic"}
        )

        assert parse_result(result)["success"] is True
        assert mock_graph.semantic_search.await_count >= 1
        for call in mock_graph.semantic_search.await_args_list:
            passed = call.kwargs.get("min_similarity")
            assert passed is not None, "handler must not pass min_similarity=None down"
            assert passed == 0.3

    @pytest.mark.asyncio
    async def test_superseded_result_is_flagged(self, patch_common):
        """Agent-facing trust flag (2026-06-21): a superseded result is marked
        superseded + carries the replacement id, so an agent doesn't cite stale
        knowledge. Default search returns superseded rows (only down-ranked)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        old = make_discovery(id="old-1", summary="stale finding", status="superseded")
        mock_graph.full_text_search = AsyncMock(return_value=[old])
        if hasattr(mock_graph, "semantic_search"):
            del mock_graph.semantic_search  # force FTS path
        mock_graph.get_superseded_by = AsyncMock(return_value={"old-1": ["new-1"]})

        result = await handle_search_knowledge_graph({"query": "stale"})
        data = parse_result(result)
        disc = next(d for d in data["discoveries"] if d["id"] == "old-1")
        assert disc["superseded"] is True
        assert disc["superseded_by"] == ["new-1"]
        assert "superseded_warning" in disc
        assert "new-1" in disc["superseded_warning"]

    @pytest.mark.asyncio
    async def test_superseded_flag_failsoft_without_successor(self, patch_common):
        """If the AGE successor lookup fails, the row is still flagged superseded
        from status (free) — just without superseded_by. Never breaks search."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        old = make_discovery(id="old-2", summary="stale", status="superseded")
        mock_graph.full_text_search = AsyncMock(return_value=[old])
        if hasattr(mock_graph, "semantic_search"):
            del mock_graph.semantic_search
        mock_graph.get_superseded_by = AsyncMock(side_effect=RuntimeError("AGE down"))

        result = await handle_search_knowledge_graph({"query": "stale"})
        data = parse_result(result)
        disc = next(d for d in data["discoveries"] if d["id"] == "old-2")
        assert disc["superseded"] is True
        assert "superseded_by" not in disc
        assert "superseded_warning" in disc

    @pytest.mark.asyncio
    async def test_non_superseded_result_not_flagged(self, patch_common):
        """An open result carries no supersession fields and triggers no lookup."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        fresh = make_discovery(id="ok-1", summary="current", status="open")
        mock_graph.full_text_search = AsyncMock(return_value=[fresh])
        if hasattr(mock_graph, "semantic_search"):
            del mock_graph.semantic_search
        mock_graph.get_superseded_by = AsyncMock(return_value={})

        result = await handle_search_knowledge_graph({"query": "current"})
        data = parse_result(result)
        disc = next(d for d in data["discoveries"] if d["id"] == "ok-1")
        assert "superseded" not in disc
        mock_graph.get_superseded_by.assert_not_awaited()  # no superseded ids -> no AGE read

    @pytest.mark.asyncio
    async def test_update_superseded_by_records_edge(self, patch_common, registered_agent):
        """Write-path fix (2026-06-21): update with superseded_by creates the
        SUPERSEDES edge, not just status. Previously superseded_by was read
        nowhere, so the successor link was silently dropped (18 rows, 0 edges)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_update_discovery_status_graph

        mock_graph.get_discovery = AsyncMock(return_value=make_discovery(id="old-1", severity="low"))
        mock_graph.update_discovery = AsyncMock(return_value=True)
        mock_graph.supersede_discovery = AsyncMock(return_value={"success": True})

        result = await handle_update_discovery_status_graph({
            "agent_id": registered_agent,
            "discovery_id": "old-1",
            "status": "superseded",
            "superseded_by": "new-1",
        })
        data = parse_result(result)
        assert data["success"] is True
        mock_graph.supersede_discovery.assert_awaited_once_with(new_id="new-1", old_id="old-1")
        assert data["superseded_by"] == "new-1"

    @pytest.mark.asyncio
    async def test_search_with_query_text_fts(self, patch_common):
        """Search with query text uses FTS when available."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        # Make graph have full_text_search but no semantic_search
        mock_graph.full_text_search = AsyncMock(return_value=[
            make_discovery(id="fts-1", summary="Matching result"),
        ])
        # Remove semantic_search to force FTS path
        if hasattr(mock_graph, 'semantic_search'):
            del mock_graph.semantic_search

        result = await handle_search_knowledge_graph({
            "query": "matching",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["count"] == 1
        assert data["search_mode_used"] == "fts"

    @pytest.mark.asyncio
    async def test_search_with_filters(self, patch_common):
        """Search with metadata filters."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        discoveries = [make_discovery(id="d-1", type="bug_found", severity="high")]
        mock_graph.query = AsyncMock(return_value=discoveries)

        result = await handle_search_knowledge_graph({
            "discovery_type": "bug_found",
            "severity": "high",
        })

        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_search_empty_results(self, patch_common):
        """Search returning no results includes helpful hints."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        mock_graph.query = AsyncMock(return_value=[])

        result = await handle_search_knowledge_graph({
            "query": "nonexistent stuff",
            "semantic": False,
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["count"] == 0
        mock_mcp_server.recall_event_recorder.assert_called_once()
        args, kwargs = mock_mcp_server.recall_event_recorder.call_args
        assert args[:2] == ("zero_result", "nonexistent stuff")
        assert kwargs["query_terms"] == 2
        assert kwargs["search_mode"] == "fts"
        assert kwargs["detail"] == {
            "hybrid_skipped": False,
            "fts_or_fallback_skipped": False,
        }

    @pytest.mark.asyncio
    async def test_hybrid_no_lexical_match_flags_low_confidence(self, patch_common):
        """Hybrid hits with zero FTS (lexical) anchor are flagged low_confidence.

        Anisotropic cosine lets a query get semantic-only hits with no keyword
        match (the 'confident noise' case the 2026-06-20 probes exposed). When
        the FTS lane returns nothing, the surfaced results are semantic-only, so
        the handler marks the response low_confidence instead of presenting it
        as a trustworthy match.
        """
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        disc = make_discovery(id="sem-only", summary="tangential semantic neighbor")
        mock_graph.semantic_search = AsyncMock(return_value=[(disc, 0.36)])
        mock_graph.full_text_search = AsyncMock(return_value=[])  # no keyword match

        result = await handle_search_knowledge_graph({
            "query": "how to bake sourdough bread",
            "search_mode": "hybrid",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data.get("low_confidence") is True
        assert "confidence_note" in data
        mock_mcp_server.recall_event_recorder.assert_called_once_with(
            "low_confidence",
            "how to bake sourdough bread",
            query_terms=5,
            search_mode="hybrid_rrf",
        )

    @pytest.mark.asyncio
    async def test_hybrid_with_lexical_match_not_low_confidence(self, patch_common):
        """A keyword-anchored hybrid result is NOT flagged low_confidence."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        disc = make_discovery(id="anchored", summary="exact keyword match here")
        mock_graph.semantic_search = AsyncMock(return_value=[(disc, 0.36)])
        mock_graph.full_text_search = AsyncMock(return_value=[disc])  # FTS anchored it

        result = await handle_search_knowledge_graph({
            "query": "keyword",
            "search_mode": "hybrid",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data.get("low_confidence") is not True
        assert "confidence_note" not in data

    @pytest.mark.asyncio
    async def test_long_query_gets_or_recall_fallback(self, patch_common):
        """Recall floor (2026-06-20): a long natural-language query whose
        AND-FTS misses must still get the OR-recall fallback.

        Reproduces the live zero-result floor: semantic returns nothing, the
        semantic→FTS fallback runs AND (which a 14-term query rarely satisfies),
        and the OR retry — the one that would hit — was gated off by the old
        complex_query_term_limit=4. With the cap raised, OR recall fires and the
        answer is returned instead of a bare 0.
        """
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        hit = make_discovery(id="or-hit", summary="agent adoption governance answer")

        async def fts(query, limit, operator):
            # AND matches nothing (not all 14 terms co-occur); OR recovers it.
            return [hit] if operator == "OR" else []

        mock_graph.semantic_search = AsyncMock(return_value=[])
        mock_graph.full_text_search = AsyncMock(side_effect=fts)

        result = await handle_search_knowledge_graph({
            "query": "what should I work on next to make agents actually want to use governance",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["count"] == 1
        # OR recall fired rather than being skipped for being "too long".
        assert "fts_fallback_skipped_reason" not in data
        assert data.get("fts_fallback_used") is True

    @pytest.mark.asyncio
    async def test_search_with_include_details(self, patch_common):
        """Search with include_details=True returns full content."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        discoveries = [make_discovery(id="d-1", details="Full details here")]
        mock_graph.query = AsyncMock(return_value=discoveries)

        result = await handle_search_knowledge_graph({
            "include_details": True,
        })

        data = parse_result(result)
        assert data["success"] is True
        if data["count"] > 0:
            assert "details" in data["discoveries"][0]

    @pytest.mark.asyncio
    async def test_search_exception_handling(self, patch_common):
        """Exception from graph backend returns error response."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        mock_graph.query = AsyncMock(side_effect=Exception("DB down"))

        result = await handle_search_knowledge_graph({})

        data = parse_result(result)
        assert data["success"] is False
        assert "failed to search" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_search_substring_scan_fallback(self, patch_common):
        """When no FTS/semantic available, falls back to substring scan."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        # Remove both search methods to trigger substring scan
        mock_graph_spec = AsyncMock()
        mock_graph_spec.query = AsyncMock(return_value=[
            make_discovery(id="d-1", summary="Contains keyword here"),
        ])
        # Make hasattr return False for semantic_search and full_text_search
        del mock_graph_spec.semantic_search
        del mock_graph_spec.full_text_search

        with patch("src.mcp_handlers.knowledge.handlers.get_knowledge_graph", new_callable=AsyncMock, return_value=mock_graph_spec):
            result = await handle_search_knowledge_graph({
                "query": "keyword",
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data["search_mode_used"] == "substring_scan"

    @pytest.mark.asyncio
    async def test_search_with_agent_id_filter(self, patch_common):
        """Search filtered by agent_id."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        discoveries = [make_discovery(id="d-1", agent_id="specific-agent")]
        mock_graph.query = AsyncMock(return_value=discoveries)

        result = await handle_search_knowledge_graph({
            "agent_id": "specific-agent",
        })

        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_exclude_agent_labels_drops_matching_rows(self, patch_common):
        """exclude_agent_labels filters post-query so the main Discoveries feed
        can hide janitorial residents (e.g. Vigil) without losing them from
        agent-drill-down views. The match is on display_name, case-insensitive."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        # Register two agents with distinct labels in agent_metadata so the
        # display-name resolver returns something meaningful.
        mock_mcp_server.agent_metadata = {
            "vigil-uuid": SimpleNamespace(label="Vigil", display_name="Vigil"),
            "worker-uuid": SimpleNamespace(label="Worker", display_name="Worker"),
        }

        mock_graph.query = AsyncMock(return_value=[
            make_discovery(id="d-v1", agent_id="vigil-uuid", summary="Groundskeeper"),
            make_discovery(id="d-w1", agent_id="worker-uuid", summary="Real finding"),
            make_discovery(id="d-v2", agent_id="vigil-uuid", summary="Another groundskeeper"),
        ])

        result = await handle_search_knowledge_graph({
            "exclude_agent_labels": ["Vigil"],
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["count"] == 1
        ids = [d["id"] for d in data["discoveries"]]
        assert ids == ["d-w1"]

    @pytest.mark.asyncio
    async def test_exclude_agent_labels_empty_list_is_no_op(self, patch_common):
        """Passing an empty list (or omitting the param) must not filter
        anything — otherwise the default MCP-tool call would silently lose
        results for any caller that passes the param unconditionally."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        mock_graph.query = AsyncMock(return_value=[
            make_discovery(id="d-1", summary="One"),
            make_discovery(id="d-2", summary="Two"),
        ])

        result = await handle_search_knowledge_graph({
            "exclude_agent_labels": [],
        })

        data = parse_result(result)
        assert data["count"] == 2

    @pytest.mark.asyncio
    async def test_search_with_tags(self, patch_common):
        """Search filtered by tags."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        discoveries = [make_discovery(id="d-1", tags=["python", "bug"])]
        mock_graph.query = AsyncMock(return_value=discoveries)

        result = await handle_search_knowledge_graph({
            "tags": ["python"],
        })

        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_search_param_aliases(self, patch_common):
        """Parameter aliases work (e.g. 'search' -> 'query')."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        # "search" is an alias for "query" in PARAM_ALIASES
        mock_graph.query = AsyncMock(return_value=[])
        # Remove semantic/FTS to test substring path
        del mock_graph.semantic_search
        del mock_graph.full_text_search

        result = await handle_search_knowledge_graph({
            "search": "test query",
        })

        data = parse_result(result)
        assert data["success"] is True
        # The query should have been resolved
        assert data.get("query") == "test query"

    @pytest.mark.asyncio
    async def test_search_with_provenance(self, patch_common):
        """Search with include_provenance=True returns provenance data."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        disc = make_discovery(
            id="d-1",
            provenance={"agent_state": {"status": "active"}},
            provenance_chain=[{"agent_id": "parent", "relationship": "direct_parent"}],
        )
        mock_graph.query = AsyncMock(return_value=[disc])

        result = await handle_search_knowledge_graph({
            "include_provenance": True,
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["count"] == 1
        assert "provenance" in data["discoveries"][0]
        assert "provenance_chain" in data["discoveries"][0]


# ============================================================================
# handle_get_knowledge_graph
# ============================================================================

class TestGetKnowledgeGraph:

    @pytest.mark.asyncio
    async def test_get_happy_path(self, patch_common, registered_agent):
        """Get discoveries for a registered agent."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_get_knowledge_graph

        discoveries = [
            make_discovery(id="d-1", agent_id=registered_agent, summary="First"),
            make_discovery(id="d-2", agent_id=registered_agent, summary="Second"),
        ]
        mock_graph.get_agent_discoveries = AsyncMock(return_value=discoveries)

        result = await handle_get_knowledge_graph({
            "agent_id": registered_agent,
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["count"] == 2

    @pytest.mark.asyncio
    async def test_get_unregistered_agent(self, patch_common):
        """Get for unregistered agent returns error."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_get_knowledge_graph

        result = await handle_get_knowledge_graph({
            "agent_id": "nonexistent-agent",
        })

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_get_empty_results(self, patch_common, registered_agent):
        """Get returns empty list when no discoveries found."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_get_knowledge_graph

        mock_graph.get_agent_discoveries = AsyncMock(return_value=[])

        result = await handle_get_knowledge_graph({
            "agent_id": registered_agent,
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["count"] == 0
        assert data["discoveries"] == []

    @pytest.mark.asyncio
    async def test_get_with_limit(self, patch_common, registered_agent):
        """Get respects limit parameter."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_get_knowledge_graph

        mock_graph.get_agent_discoveries = AsyncMock(return_value=[])

        result = await handle_get_knowledge_graph({
            "agent_id": registered_agent,
            "limit": 5,
        })

        # Verify limit was passed to the graph backend
        mock_graph.get_agent_discoveries.assert_awaited_once_with(registered_agent, limit=5)

    @pytest.mark.asyncio
    async def test_get_exception_handling(self, patch_common, registered_agent):
        """Exception from graph backend returns error."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_get_knowledge_graph

        mock_graph.get_agent_discoveries = AsyncMock(side_effect=Exception("DB error"))

        result = await handle_get_knowledge_graph({
            "agent_id": registered_agent,
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "failed to retrieve" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_get_with_include_details(self, patch_common, registered_agent):
        """Get with include_details=True includes details in output."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_get_knowledge_graph

        disc = make_discovery(id="d-1", agent_id=registered_agent, details="Full details content")
        mock_graph.get_agent_discoveries = AsyncMock(return_value=[disc])

        result = await handle_get_knowledge_graph({
            "agent_id": registered_agent,
            "include_details": True,
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["count"] == 1
        assert "details" in data["discoveries"][0]


# ============================================================================
# handle_list_knowledge_graph
# ============================================================================

class TestListKnowledgeGraph:

    @pytest.mark.asyncio
    async def test_list_happy_path(self, patch_common):
        """List returns graph statistics."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_list_knowledge_graph

        mock_graph.get_stats = AsyncMock(return_value={
            "total_discoveries": 42,
            "total_agents": 5,
        })

        result = await handle_list_knowledge_graph({})

        data = parse_result(result)
        assert data["success"] is True
        assert data["stats"]["total_discoveries"] == 42
        assert data["stats"]["total_agents"] == 5
        assert "42" in data["message"]
        assert "5" in data["message"]

    @pytest.mark.asyncio
    async def test_list_empty_graph(self, patch_common):
        """List returns zero counts for empty graph."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_list_knowledge_graph

        mock_graph.get_stats = AsyncMock(return_value={
            "total_discoveries": 0,
            "total_agents": 0,
        })

        result = await handle_list_knowledge_graph({})

        data = parse_result(result)
        assert data["success"] is True
        assert data["stats"]["total_discoveries"] == 0

    @pytest.mark.asyncio
    async def test_list_exception_handling(self, patch_common):
        """Exception from graph backend returns error."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_list_knowledge_graph

        mock_graph.get_stats = AsyncMock(side_effect=Exception("Stats error"))

        result = await handle_list_knowledge_graph({})

        data = parse_result(result)
        assert data["success"] is False
        assert "failed to list" in data["error"].lower()


# ============================================================================
# handle_update_discovery_status_graph
# ============================================================================

class TestAnswerQuestion:

    @pytest.mark.asyncio
    async def test_answer_question_happy_path(self, patch_common, registered_agent):
        """Answer a matching question successfully."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_answer_question

        question_disc = make_discovery(
            id="q-1",
            type="question",
            summary="What is the meaning of life?",
            agent_id="other-agent",
        )
        mock_graph.query = AsyncMock(return_value=[question_disc])

        result = await handle_answer_question({
            "agent_id": registered_agent,
            "question": "What is the meaning of life?",
            "answer": "42",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert "answer_id" in data
        assert data["question"]["id"] == "q-1"
        mock_graph.add_discovery.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_answer_question_missing_question(self, patch_common, registered_agent):
        """Answer fails without question text."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_answer_question

        result = await handle_answer_question({
            "agent_id": registered_agent,
            "answer": "42",
        })

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_answer_question_missing_answer(self, patch_common, registered_agent):
        """Answer fails without answer text."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_answer_question

        result = await handle_answer_question({
            "agent_id": registered_agent,
            "question": "What is the meaning?",
        })

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_answer_question_no_match(self, patch_common, registered_agent):
        """Answer fails when no matching question found."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_answer_question

        mock_graph.query = AsyncMock(return_value=[])

        result = await handle_answer_question({
            "agent_id": registered_agent,
            "question": "Something completely unrelated",
            "answer": "My answer",
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "no matching question" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_answer_question_with_resolve(self, patch_common, registered_agent):
        """Answer resolves question when resolve_question=True."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_answer_question

        question_disc = make_discovery(
            id="q-1",
            type="question",
            summary="How does caching work?",
        )
        mock_graph.query = AsyncMock(return_value=[question_disc])

        result = await handle_answer_question({
            "agent_id": registered_agent,
            "question": "How does caching work?",
            "answer": "It uses LRU eviction policy",
            "resolve_question": True,
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["question"]["status"] == "resolved"
        # update_discovery should have been called to resolve the question
        mock_graph.update_discovery.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_answer_question_unregistered_agent(self, patch_common):
        """Answer fails for unregistered agent."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_answer_question

        result = await handle_answer_question({
            "agent_id": "nonexistent-agent",
            "question": "What?",
            "answer": "Nothing",
        })

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_answer_question_exception_handling(self, patch_common, registered_agent):
        """Exception from graph backend returns error."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_answer_question

        mock_graph.query = AsyncMock(side_effect=Exception("Query error"))

        result = await handle_answer_question({
            "agent_id": registered_agent,
            "question": "What?",
            "answer": "Something",
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "failed to answer" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_answer_question_truncates_long_answer(self, patch_common, registered_agent):
        """Long answers are truncated to 2000 chars."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_answer_question

        question_disc = make_discovery(
            id="q-1",
            type="question",
            summary="Tell me everything",
        )
        mock_graph.query = AsyncMock(return_value=[question_disc])

        long_answer = "Z" * 3000
        result = await handle_answer_question({
            "agent_id": registered_agent,
            "question": "Tell me everything",
            "answer": long_answer,
        })

        data = parse_result(result)
        assert data["success"] is True
        # Verify the stored answer's details were truncated
        call_args = mock_graph.add_discovery.call_args
        answer_disc = call_args[0][0]
        assert len(answer_disc.details) <= 2020  # 2000 + "... [truncated]"


# ============================================================================
# _discovery_not_found helper
# ============================================================================

class TestDiscoveryNotFound:

    @pytest.mark.asyncio
    async def test_not_found_no_suggestions(self, patch_common):
        """Returns plain not-found error when no prefix matches."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import _discovery_not_found

        mock_db = AsyncMock()
        mock_db.graph_query = AsyncMock(return_value=[])
        mock_graph._get_db = AsyncMock(return_value=mock_db)

        result = await _discovery_not_found("2026-nonexistent", mock_graph)

        data = json.loads(result.text)
        assert data["success"] is False
        assert "not found" in data["error"].lower()
        assert "recovery" not in data

    @pytest.mark.asyncio
    async def test_not_found_with_suggestions(self, patch_common):
        """Returns suggestions when prefix matches exist."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import _discovery_not_found

        mock_db = AsyncMock()
        mock_db.graph_query = AsyncMock(return_value=[
            {"d.id": "2026-01-01T00:00:00.123456"},
            {"d.id": "2026-01-01T00:00:00.789012"},
        ])
        mock_graph._get_db = AsyncMock(return_value=mock_db)

        result = await _discovery_not_found("2026", mock_graph)

        data = json.loads(result.text)
        assert data["success"] is False
        assert "did you mean" in data["error"].lower()
        assert "recovery" in data
        assert len(data["recovery"]["matching_ids"]) == 2

    @pytest.mark.asyncio
    async def test_not_found_db_error_graceful(self, patch_common):
        """Falls back to plain error when DB query fails."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import _discovery_not_found

        mock_graph._get_db = AsyncMock(side_effect=Exception("DB unavailable"))

        result = await _discovery_not_found("2026-missing", mock_graph)

        data = json.loads(result.text)
        assert data["success"] is False
        assert "not found" in data["error"].lower()


# ============================================================================
# _check_display_name_required helper
# ============================================================================

class TestCheckDisplayNameRequired:

    def test_has_real_display_name(self, patch_common, registered_agent, mock_mcp_server):
        """Returns (None, None) when agent has a meaningful display_name."""
        from src.mcp_handlers.knowledge.handlers import _check_display_name_required

        error, warning = _check_display_name_required(registered_agent, {})

        assert error is None
        assert warning is None

    def test_auto_generates_for_uuid_display_name(self, patch_common, mock_mcp_server):
        """Auto-generates display_name when current one is a UUID."""
        import uuid
        agent_id = str(uuid.uuid4())
        meta = MagicMock()
        meta.status = "active"
        meta.display_name = agent_id  # Display name is the UUID itself
        meta.label = agent_id
        mock_mcp_server.agent_metadata[agent_id] = meta

        from src.mcp_handlers.knowledge.handlers import _check_display_name_required

        with patch("src.mcp_handlers.knowledge.handlers._check_display_name_required.__module__"):
            error, warning = _check_display_name_required(agent_id, {})

        assert error is None
        # Warning should mention auto-generated
        if warning:
            assert "auto-generated" in warning.lower()

    def test_no_metadata_graceful(self, patch_common):
        """Gracefully handles agents not in metadata."""
        from src.mcp_handlers.knowledge.handlers import _check_display_name_required

        error, warning = _check_display_name_required("unknown-agent", {})

        # Should not error - just auto-generate
        assert error is None


# ============================================================================
# _resolve_agent_display helper
# ============================================================================

class TestResolveAgentDisplay:

    def test_resolve_known_agent(self, patch_common, registered_agent, mock_mcp_server):
        """Resolves agent display info from metadata."""
        from src.mcp_handlers.knowledge.handlers import _resolve_agent_display

        result = _resolve_agent_display(registered_agent)

        assert "agent_id" in result
        assert "display_name" in result
        assert result["display_name"] == "TestAgent"

    def test_resolve_unknown_agent(self, patch_common):
        """Returns agent_id as fallback for unknown agents."""
        from src.mcp_handlers.knowledge.handlers import _resolve_agent_display

        result = _resolve_agent_display("unknown-agent-xyz")

        assert result["agent_id"] == "unknown-agent-xyz"
        assert result["display_name"] == "unknown-agent-xyz"

    def test_resolve_by_structured_id(self, patch_common, mock_mcp_server):
        """Resolves agent by structured_id (not UUID key)."""
        meta = MagicMock()
        meta.structured_id = "opus_agent_20260101"
        meta.display_name = "Opus Agent"
        meta.label = "Opus Agent"
        mock_mcp_server.agent_metadata["uuid-123"] = meta

        from src.mcp_handlers.knowledge.handlers import _resolve_agent_display

        result = _resolve_agent_display("opus_agent_20260101")

        assert result["display_name"] == "Opus Agent"


# ============================================================================
# Integration-level edge cases
# ============================================================================

class TestDiscoveryNotFoundAdditional:

    @pytest.mark.asyncio
    async def test_not_found_with_string_rows(self, patch_common):
        """Returns suggestions from string-typed rows (lines 52-53)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import _discovery_not_found

        mock_db = AsyncMock()
        mock_db.graph_query = AsyncMock(return_value=[
            "2026-01-01T00:00:00.111111",
            "2026-01-01T00:00:00.222222",
        ])
        mock_graph._get_db = AsyncMock(return_value=mock_db)

        result = await _discovery_not_found("2026", mock_graph)

        data = json.loads(result.text)
        assert data["success"] is False
        assert "did you mean" in data["error"].lower()
        assert len(data["recovery"]["matching_ids"]) == 2


# ============================================================================
# _check_display_name_required - additional edge cases
# ============================================================================

class TestCheckDisplayNameAdditional:

    def test_auto_pattern_display_name(self, patch_common, mock_mcp_server):
        """Auto-generated display name (auto_ prefix) triggers auto-generation (line 97)."""
        import uuid
        agent_id = str(uuid.uuid4())
        meta = MagicMock()
        meta.status = "active"
        meta.display_name = "auto_20260101_abc"  # auto_ pattern
        meta.label = "auto_20260101_abc"
        mock_mcp_server.agent_metadata[agent_id] = meta

        from src.mcp_handlers.knowledge.handlers import _check_display_name_required

        error, warning = _check_display_name_required(agent_id, {})

        assert error is None
        if warning:
            assert "auto-generated" in warning.lower()

    def test_agent_prefix_display_name(self, patch_common, mock_mcp_server):
        """Agent_ prefix display name triggers auto-generation."""
        import uuid
        agent_id = str(uuid.uuid4())
        meta = MagicMock()
        meta.status = "active"
        meta.display_name = "Agent_abc123"
        meta.label = "Agent_abc123"
        mock_mcp_server.agent_metadata[agent_id] = meta

        from src.mcp_handlers.knowledge.handlers import _check_display_name_required

        error, warning = _check_display_name_required(agent_id, {})

        assert error is None
        if warning:
            assert "auto-generated" in warning.lower()

    def test_check_display_name_exception_graceful(self):
        """Exception in check is suppressed (lines 139-141)."""
        from src.mcp_handlers.knowledge.handlers import _check_display_name_required

        # Patch get_mcp_server at the import source to raise
        with patch("src.mcp_handlers.shared.get_mcp_server", side_effect=RuntimeError("broken")):
            error, warning = _check_display_name_required("any-agent", {})

        assert error is None
        assert warning is None


# ============================================================================
# _resolve_agent_display - additional edge cases
# ============================================================================

class TestResolveAgentDisplayAdditional:

    def test_resolve_exception_graceful(self, patch_common):
        """Exception in resolve returns fallback (lines 176-177)."""
        from src.mcp_handlers.knowledge.handlers import _resolve_agent_display

        with patch("src.mcp_handlers.shared.get_mcp_server", side_effect=RuntimeError("broken")):
            result = _resolve_agent_display("any-agent")

        assert result["agent_id"] == "any-agent"
        assert result["display_name"] == "any-agent"


# ============================================================================
# handle_store_knowledge_graph - additional coverage
# ============================================================================

class TestSearchKnowledgeGraphAdditional:

    @pytest.mark.asyncio
    async def test_search_semantic_mode(self, patch_common):
        """Search with semantic=True uses semantic search (lines 513-521)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        disc = make_discovery(id="sem-1", summary="Semantic result")
        mock_graph.semantic_search = AsyncMock(return_value=[(disc, 0.85)])

        result = await handle_search_knowledge_graph({
            "query": "conceptual similarity test",
            "semantic": True,
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["search_mode_used"] == "semantic"
        assert data["count"] == 1

    @pytest.mark.asyncio
    async def test_search_semantic_with_filters(self, patch_common):
        """Search semantic with metadata filters (lines 544-557)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        disc1 = make_discovery(id="sem-1", summary="Match", type="bug_found", severity="high", tags=["python"])
        disc2 = make_discovery(id="sem-2", summary="Wrong type", type="note")
        mock_graph.semantic_search = AsyncMock(return_value=[
            (disc1, 0.9), (disc2, 0.8)
        ])

        result = await handle_search_knowledge_graph({
            "query": "matching concept",
            "semantic": True,
            "discovery_type": "bug_found",
            "severity": "high",
            "tags": ["python"],
            "status": "open",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["count"] == 1

    @pytest.mark.asyncio
    async def test_search_semantic_fallback_to_fts(self, patch_common):
        """Search semantic returning 0 results falls back to FTS (lines 602-632)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        disc = make_discovery(id="fts-1", summary="FTS fallback result")
        mock_graph.semantic_search = AsyncMock(return_value=[])
        mock_graph.full_text_search = AsyncMock(return_value=[disc])

        result = await handle_search_knowledge_graph({
            "query": "search with fallback",
            "semantic": True,
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["fallback_used"] is True
        assert "semantic_fallback_fts" in data["search_mode_used"]

    @pytest.mark.asyncio
    async def test_search_fts_multi_term_or_default(self, patch_common):
        """FTS multi-term queries use OR by default (no per-term fallback needed)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        disc = make_discovery(id="fts-or-1", summary="Matches one of the terms")
        # Remove semantic_search to force FTS path
        del mock_graph.semantic_search
        # Single FTS call should find results with OR-default
        mock_graph.full_text_search = AsyncMock(return_value=[disc])

        result = await handle_search_knowledge_graph({
            "query": "multiple word query",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["count"] == 1
        assert data["search_mode_used"] == "fts"
        # No fallback needed — OR-default handles multi-term in primary query
        assert data.get("fallback_used") is not True

    @pytest.mark.asyncio
    async def test_search_returns_empty_when_all_fallbacks_miss(self, patch_common):
        """When semantic AND FTS both return zero, search returns an honest empty
        result rather than retrying semantic at a noise-floor threshold. The old
        lower-threshold fallback (min_similarity=0.2) confidently surfaced random
        results on genuine misses; removed 2026-04-20."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        mock_graph.semantic_search = AsyncMock(return_value=[])
        mock_graph.full_text_search = AsyncMock(return_value=[])

        result = await handle_search_knowledge_graph({
            "query": "obscure search concept with no matches",
            "semantic": True,
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["count"] == 0
        # Semantic was called exactly once — no lower-threshold retry
        assert mock_graph.semantic_search.await_count == 1
        assert "lower_threshold" not in data["search_mode_used"]

    @pytest.mark.asyncio
    async def test_search_fts_with_agent_filter(self, patch_common):
        """Search FTS with agent_id filter (line 547)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        disc1 = make_discovery(id="fts-1", summary="Match", agent_id="agent-a")
        disc2 = make_discovery(id="fts-2", summary="Other", agent_id="agent-b")
        del mock_graph.semantic_search
        mock_graph.full_text_search = AsyncMock(return_value=[disc1, disc2])

        result = await handle_search_knowledge_graph({
            "query": "test",
            "agent_id": "agent-a",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["count"] == 1

    @pytest.mark.asyncio
    async def test_search_empty_with_long_query_hints(self, patch_common):
        """Empty results with long query show specific hints (lines 787-788)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        del mock_graph.semantic_search
        del mock_graph.full_text_search
        mock_graph.query = AsyncMock(return_value=[])

        result = await handle_search_knowledge_graph({
            "query": "this is a very long query with five or more words",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["count"] == 0
        assert "empty_results_hints" in data or "tip" in data

    @pytest.mark.asyncio
    async def test_search_empty_with_single_word_hints(self, patch_common):
        """Empty results with single word query shows tag suggestion (lines 795-796)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        del mock_graph.semantic_search
        del mock_graph.full_text_search
        mock_graph.query = AsyncMock(return_value=[])

        result = await handle_search_knowledge_graph({
            "query": "identity",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["count"] == 0
        assert "empty_results_hints" in data or "tip" in data

    @pytest.mark.asyncio
    async def test_search_empty_with_filter_hints(self, patch_common):
        """Empty results with active filters show filter-specific hints (lines 803-809)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        mock_graph.query = AsyncMock(return_value=[])

        result = await handle_search_knowledge_graph({
            "query": "test",
            "agent_id": "specific-agent",
            "tags": ["python"],
            "discovery_type": "insight",
            "severity": "high",
            "semantic": False,
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["count"] == 0

    @pytest.mark.asyncio
    async def test_search_limit_cap_hint(self, patch_common):
        """Results at limit show _more_available hint (lines 829)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        discoveries = [make_discovery(id=f"d-{i}", summary=f"Item {i}") for i in range(5)]
        mock_graph.query = AsyncMock(return_value=discoveries)

        result = await handle_search_knowledge_graph({
            "limit": 5,
        })

        data = parse_result(result)
        assert data["success"] is True
        if data["count"] == 5:
            assert "_more_available" in data

    @pytest.mark.asyncio
    async def test_search_semantic_threshold_explanation(self, patch_common):
        """Semantic search includes threshold explanation (lines 833-837)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        disc = make_discovery(id="sem-1", summary="Result")
        mock_graph.semantic_search = AsyncMock(return_value=[(disc, 0.5)])

        result = await handle_search_knowledge_graph({
            "query": "conceptual search query",
            "semantic": True,
        })

        data = parse_result(result)
        assert data["success"] is True
        if data["count"] > 0:
            assert "similarity_threshold_explanation" not in data

    @pytest.mark.asyncio
    async def test_search_similarity_scores_included(self, patch_common):
        """Semantic search includes similarity scores (lines 856-862)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        disc1 = make_discovery(id="sem-1", summary="Close match")
        disc2 = make_discovery(id="sem-2", summary="Another match")
        mock_graph.semantic_search = AsyncMock(return_value=[
            (disc1, 0.85), (disc2, 0.72)
        ])

        result = await handle_search_knowledge_graph({
            "query": "test concept query",
            "semantic": True,
        })

        data = parse_result(result)
        assert data["success"] is True
        if data["count"] > 0 and "similarity_scores" in data:
            assert "sem-1" in data["similarity_scores"]

    @pytest.mark.asyncio
    async def test_search_synthesize_with_enough_results(self, patch_common):
        """Search with synthesize=True when enough results triggers synthesis (lines 877-890)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        discoveries = [make_discovery(id=f"d-{i}", summary=f"Item {i}") for i in range(5)]
        mock_graph.query = AsyncMock(return_value=discoveries)

        with patch("src.mcp_handlers.knowledge.handlers.synthesize_results",
                    new_callable=AsyncMock,
                    return_value={"summary": "Synthesized results"}):
            result = await handle_search_knowledge_graph({
                "synthesize": True,
            })

            data = parse_result(result)
            assert data["success"] is True
            if data["count"] >= 3:
                assert "synthesis" in data

    @pytest.mark.asyncio
    async def test_search_synthesize_below_threshold(self, patch_common):
        """Search with synthesize=True but too few results skips synthesis (line 892)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        discoveries = [make_discovery(id="d-1", summary="Single")]
        mock_graph.query = AsyncMock(return_value=discoveries)

        result = await handle_search_knowledge_graph({
            "synthesize": True,
        })

        data = parse_result(result)
        assert data["success"] is True
        assert "_synthesis_note" in data
        assert "fewer than" in data["_synthesis_note"]

    @pytest.mark.asyncio
    async def test_search_indexed_status_filter(self, patch_common):
        """Search with status filter in indexed mode (line 593)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        discoveries = [make_discovery(id="d-1", status="resolved")]
        mock_graph.query = AsyncMock(return_value=discoveries)

        result = await handle_search_knowledge_graph({
            "status": "resolved",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert "status" in data["fields_searched"]

    @pytest.mark.asyncio
    async def test_search_substring_scan_empty(self, patch_common):
        """Substring scan with no matches shows search_hint (line 823)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        # Remove both search methods to trigger substring scan
        mock_graph_spec = AsyncMock()
        mock_graph_spec.query = AsyncMock(return_value=[])
        del mock_graph_spec.semantic_search
        del mock_graph_spec.full_text_search

        with patch("src.mcp_handlers.knowledge.handlers.get_knowledge_graph", new_callable=AsyncMock, return_value=mock_graph_spec), \
             patch("src.mcp_handlers.knowledge.handlers.record_ms"):
            result = await handle_search_knowledge_graph({
                "query": "nonexistent",
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data["count"] == 0
        if data["search_mode_used"] == "substring_scan":
            assert "search_hint" in data

    @pytest.mark.asyncio
    async def test_search_fts_multi_term_operator_note(self, patch_common):
        """FTS multi-term queries report the operator that ran (#165)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        disc = make_discovery(id="fts-1", summary="Match found")
        del mock_graph.semantic_search
        mock_graph.full_text_search = AsyncMock(return_value=[disc])

        result = await handle_search_knowledge_graph({
            "query": "first second",
        })

        data = parse_result(result)
        assert data["success"] is True
        if data["search_mode_used"] == "fts" and data["count"] > 0:
            # Default switched from OR to AND in #165 (precision over recall);
            # OR is now reachable via the AND→OR fallback or operator=OR.
            assert data["operator_used"] == "AND"
            assert data["fts_operator_used"] == "AND"
            assert data["fts_fallback_used"] is False

    @pytest.mark.asyncio
    async def test_search_no_details_tip(self, patch_common):
        """Search without include_details shows tip when >3 results (auto-detail for ≤3)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        # >3 results avoids auto-detail promotion
        discoveries = [make_discovery(id=f"d-{i}") for i in range(5)]
        mock_graph.query = AsyncMock(return_value=discoveries)

        result = await handle_search_knowledge_graph({})

        data = parse_result(result)
        assert data["success"] is True
        if data["count"] > 3:
            assert "_tip" in data


# ============================================================================
# handle_get_knowledge_graph - additional coverage
# ============================================================================

class TestGetKnowledgeGraphAdditional:

    @pytest.mark.asyncio
    async def test_get_limit_reached_hint(self, patch_common, registered_agent):
        """Get with results at limit shows _more_available hint (line 950)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_get_knowledge_graph

        discoveries = [make_discovery(id=f"d-{i}", agent_id=registered_agent) for i in range(3)]
        mock_graph.get_agent_discoveries = AsyncMock(return_value=discoveries)

        result = await handle_get_knowledge_graph({
            "agent_id": registered_agent,
            "limit": 3,
        })

        data = parse_result(result)
        assert data["success"] is True
        assert "_more_available" in data


# ============================================================================
# handle_update_discovery_status_graph - additional coverage
# ============================================================================

class TestAnswerQuestionAdditional:

    @pytest.mark.asyncio
    async def test_answer_question_no_match_with_recent_questions(self, patch_common, registered_agent):
        """No matching question lists recent questions (lines 1366-1370)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_answer_question

        # First call (question search): returns non-matching questions
        question1 = make_discovery(id="q-1", type="question", summary="Unrelated question about X")
        question2 = make_discovery(id="q-2", type="question", summary="Another question about Y")
        # Second call (recent questions): returns same
        mock_graph.query = AsyncMock(side_effect=[
            [question1, question2],  # Search results (no match for our query)
            [question1, question2],  # Recent questions for error message
        ])

        result = await handle_answer_question({
            "agent_id": registered_agent,
            "question": "Completely different topic ZZZZZ",
            "answer": "My answer",
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "recent_questions" in data.get("details", {}) or "no matching" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_answer_question_truncates_long_answer(self, patch_common, registered_agent):
        """Long answers are truncated (lines 1382-1383)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_answer_question

        question_disc = make_discovery(
            id="q-1", type="question", summary="Tell me everything about this"
        )
        mock_graph.query = AsyncMock(return_value=[question_disc])

        long_answer = "A" * 3000
        result = await handle_answer_question({
            "agent_id": registered_agent,
            "question": "Tell me everything about this",
            "answer": long_answer,
        })

        data = parse_result(result)
        assert data["success"] is True


# ============================================================================
# handle_leave_note - additional coverage
# ============================================================================

class TestSearchArchivedFiltering:

    @pytest.mark.asyncio
    async def test_search_excludes_archived_by_default(self, patch_common):
        """Archived entries should be excluded from search results by default."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        discoveries = [
            make_discovery(id="d-open", status="open"),
            make_discovery(id="d-archived", status="archived"),
            make_discovery(id="d-resolved", status="resolved"),
        ]

        # Mock query to respect exclude_archived parameter (like real backend)
        async def query_with_filtering(**kwargs):
            if kwargs.get("exclude_archived", False):
                return [d for d in discoveries if d.status != "archived"]
            return discoveries

        mock_graph.query = AsyncMock(side_effect=query_with_filtering)

        result = await handle_search_knowledge_graph({})
        data = parse_result(result)

        assert data["success"] is True
        result_ids = [d["id"] for d in data["discoveries"]]
        assert "d-open" in result_ids
        assert "d-resolved" in result_ids
        assert "d-archived" not in result_ids

    @pytest.mark.asyncio
    async def test_search_includes_archived_when_requested(self, patch_common):
        """Archived entries should be included when include_archived=True."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        discoveries = [
            make_discovery(id="d-open", status="open"),
            make_discovery(id="d-archived", status="archived"),
        ]
        mock_graph.query = AsyncMock(return_value=discoveries)

        result = await handle_search_knowledge_graph({"include_archived": True})
        data = parse_result(result)

        assert data["success"] is True
        result_ids = [d["id"] for d in data["discoveries"]]
        assert "d-open" in result_ids
        assert "d-archived" in result_ids

    @pytest.mark.asyncio
    async def test_search_includes_archived_when_status_filter_set(self, patch_common):
        """When status filter is explicitly set, don't apply archived exclusion."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        discoveries = [
            make_discovery(id="d-archived", status="archived"),
        ]
        mock_graph.query = AsyncMock(return_value=discoveries)

        result = await handle_search_knowledge_graph({"status": "archived"})
        data = parse_result(result)

        assert data["success"] is True
        result_ids = [d["id"] for d in data["discoveries"]]
        assert "d-archived" in result_ids

    @pytest.mark.asyncio
    async def test_search_status_active_alias_maps_to_open(self, patch_common):
        """Legacy status='active' should behave as status='open'."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        open_discovery = make_discovery(id="d-open", status="open")
        mock_graph.query = AsyncMock(return_value=[open_discovery])

        result = await handle_search_knowledge_graph({"status": "active"})
        data = parse_result(result)

        assert data["success"] is True
        assert data["count"] == 1
        assert data["discoveries"][0]["id"] == "d-open"

    @pytest.mark.asyncio
    async def test_search_fts_excludes_archived_by_default(self, patch_common):
        """FTS search should also exclude archived entries by default."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        discoveries = [
            make_discovery(id="d-open", summary="matching text", status="open"),
            make_discovery(id="d-archived", summary="matching text", status="archived"),
        ]
        mock_graph.full_text_search = AsyncMock(return_value=discoveries)
        if hasattr(mock_graph, 'semantic_search'):
            del mock_graph.semantic_search

        result = await handle_search_knowledge_graph({"query": "matching"})
        data = parse_result(result)

        assert data["success"] is True
        result_ids = [d["id"] for d in data["discoveries"]]
        assert "d-open" in result_ids
        assert "d-archived" not in result_ids


class TestSearchColdFiltering:
    """Cold storage is opt-in (include_cold), mirroring archived exclusion."""

    @pytest.mark.asyncio
    async def test_search_excludes_cold_by_default(self, patch_common):
        """Cold-storage entries should be excluded from search results by default."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        discoveries = [
            make_discovery(id="d-open", status="open"),
            make_discovery(id="d-cold", status="cold"),
            make_discovery(id="d-resolved", status="resolved"),
        ]

        # Mock query to respect exclude_cold parameter (like real backend)
        async def query_with_filtering(**kwargs):
            rows = discoveries
            if kwargs.get("exclude_cold", False):
                rows = [d for d in rows if d.status != "cold"]
            return rows

        mock_graph.query = AsyncMock(side_effect=query_with_filtering)

        result = await handle_search_knowledge_graph({})
        data = parse_result(result)

        assert data["success"] is True
        result_ids = [d["id"] for d in data["discoveries"]]
        assert "d-open" in result_ids
        assert "d-resolved" in result_ids
        assert "d-cold" not in result_ids

    @pytest.mark.asyncio
    async def test_search_includes_cold_when_requested(self, patch_common):
        """Cold entries should be included when include_cold=True."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        discoveries = [
            make_discovery(id="d-open", status="open"),
            make_discovery(id="d-cold", status="cold"),
        ]
        mock_graph.query = AsyncMock(return_value=discoveries)

        result = await handle_search_knowledge_graph({"include_cold": True})
        data = parse_result(result)

        assert data["success"] is True
        result_ids = [d["id"] for d in data["discoveries"]]
        assert "d-open" in result_ids
        assert "d-cold" in result_ids

    @pytest.mark.asyncio
    async def test_search_includes_cold_when_status_filter_set(self, patch_common):
        """When status='cold' is explicitly set, don't apply cold exclusion."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        discoveries = [
            make_discovery(id="d-cold", status="cold"),
        ]
        mock_graph.query = AsyncMock(return_value=discoveries)

        result = await handle_search_knowledge_graph({"status": "cold"})
        data = parse_result(result)

        assert data["success"] is True
        result_ids = [d["id"] for d in data["discoveries"]]
        assert "d-cold" in result_ids

    @pytest.mark.asyncio
    async def test_search_fts_excludes_cold_by_default(self, patch_common):
        """FTS/post-filter path should also exclude cold entries by default."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        discoveries = [
            make_discovery(id="d-open", summary="matching text", status="open"),
            make_discovery(id="d-cold", summary="matching text", status="cold"),
        ]
        mock_graph.full_text_search = AsyncMock(return_value=discoveries)
        if hasattr(mock_graph, 'semantic_search'):
            del mock_graph.semantic_search

        result = await handle_search_knowledge_graph({"query": "matching"})
        data = parse_result(result)

        assert data["success"] is True
        result_ids = [d["id"] for d in data["discoveries"]]
        assert "d-open" in result_ids
        assert "d-cold" not in result_ids


class TestStatusMultipliersCold:
    """Cold ranks below archived in the AGE blend's status multipliers."""

    def test_cold_multiplier_present_and_below_archived(self):
        from src.storage.knowledge_graph_age import KnowledgeGraphAGE

        mults = KnowledgeGraphAGE.STATUS_MULTIPLIERS
        assert "cold" in mults, "cold must have an explicit ranking multiplier"
        assert mults["cold"] < mults["archived"], (
            "cold storage should rank below archived when surfaced"
        )


# ============================================================================
# _or_default_query helper
# ============================================================================

class TestOrDefaultQuery:
    """Tests for the FTS OR-default query preprocessor."""

    def test_single_term_unchanged(self):
        """Single terms pass through unchanged."""
        from src.db.mixins.knowledge_graph import _or_default_query
        assert _or_default_query("bug") == "bug"

    def test_multi_term_inserts_or(self):
        """Multiple terms get OR inserted between them."""
        from src.db.mixins.knowledge_graph import _or_default_query
        assert _or_default_query("bug database") == "bug OR database"

    def test_three_terms(self):
        """Three terms all get OR-joined."""
        from src.db.mixins.knowledge_graph import _or_default_query
        assert _or_default_query("bug database search") == "bug OR database OR search"

    def test_existing_or_preserved(self):
        """Explicit OR in query leaves it unchanged."""
        from src.db.mixins.knowledge_graph import _or_default_query
        assert _or_default_query("bug OR database") == "bug OR database"

    def test_existing_and_preserved(self):
        """Explicit AND in query leaves it unchanged."""
        from src.db.mixins.knowledge_graph import _or_default_query
        assert _or_default_query("bug AND database") == "bug AND database"

    def test_quoted_phrase_single_token(self):
        """Quoted phrase is kept as a single token."""
        from src.db.mixins.knowledge_graph import _or_default_query
        assert _or_default_query('"exact phrase"') == '"exact phrase"'

    def test_quoted_phrase_with_unquoted(self):
        """Mixed quoted + unquoted terms get OR between them."""
        from src.db.mixins.knowledge_graph import _or_default_query
        assert _or_default_query('"exact phrase" bug') == '"exact phrase" OR bug'

    def test_negation_preserved(self):
        """Negation prefix is preserved."""
        from src.db.mixins.knowledge_graph import _or_default_query
        assert _or_default_query("-bug database") == "-bug OR database"

    def test_empty_string(self):
        """Empty string returns empty string."""
        from src.db.mixins.knowledge_graph import _or_default_query
        assert _or_default_query("") == ""

    def test_whitespace_only(self):
        """Whitespace-only input returns empty."""
        from src.db.mixins.knowledge_graph import _or_default_query
        result = _or_default_query("   ")
        assert result == "   "  # No tokens found, returned as-is


# ============================================================================
# Supersede handler
# ============================================================================


# ============================================================================
# Issue #165 — search_mode + routing-reason + AND default + scope + provenance
# ============================================================================


class TestIssue165SearchMode:
    """Forced search modes error honestly when the backend can't deliver."""

    @pytest.mark.asyncio
    async def test_auto_falls_back_with_reason_when_backend_lacks_semantic(self, patch_common):
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        # Backend has FTS only — no semantic_search method on the instance.
        del mock_graph.semantic_search
        mock_graph.full_text_search = AsyncMock(
            return_value=[make_discovery(id="f1", summary="Match")]
        )

        result = await handle_search_knowledge_graph({"query": "anything goes here"})
        data = parse_result(result)

        assert data["success"] is True
        assert data["search_mode_used"] == "fts"
        assert data["search_mode_requested"] == "auto"
        assert "semantic_skipped_reason" in data
        assert "no semantic_search" in data["semantic_skipped_reason"]

    @pytest.mark.asyncio
    async def test_forced_semantic_errors_when_backend_lacks_it(self, patch_common):
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        del mock_graph.semantic_search
        mock_graph.full_text_search = AsyncMock(return_value=[])

        result = await handle_search_knowledge_graph({
            "query": "doesnt matter",
            "search_mode": "semantic",
        })
        data = parse_result(result)
        assert data["success"] is False
        assert "semantic_search" in data["error"]

    @pytest.mark.asyncio
    async def test_forced_hybrid_errors_when_backend_partial(self, patch_common):
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        del mock_graph.semantic_search  # only FTS available

        result = await handle_search_knowledge_graph({
            "query": "test",
            "search_mode": "hybrid",
        })
        data = parse_result(result)
        assert data["success"] is False
        assert "hybrid" in data["error"]

    @pytest.mark.asyncio
    async def test_invalid_search_mode_rejected(self, patch_common):
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph
        result = await handle_search_knowledge_graph({
            "query": "test",
            "search_mode": "wat",
        })
        data = parse_result(result)
        assert data["success"] is False
        assert "Invalid search_mode" in data["error"]


class TestIssue165FtsOperator:
    """AND default with OR fallback on zero hits — surfaced in response."""

    @pytest.mark.asyncio
    async def test_and_default_records_operator_in_response(self, patch_common):
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        del mock_graph.semantic_search
        # AND returns one match — no fallback fires.
        mock_graph.full_text_search = AsyncMock(
            return_value=[make_discovery(id="f1", summary="Hit")]
        )

        result = await handle_search_knowledge_graph({"query": "two terms"})
        data = parse_result(result)
        assert data["success"] is True
        assert data["fts_operator_used"] == "AND"
        assert data["fts_fallback_used"] is False
        # Backend was called with operator=AND
        first_call = mock_graph.full_text_search.await_args_list[0]
        assert first_call.kwargs.get("operator") == "AND"

    @pytest.mark.asyncio
    async def test_and_then_or_fallback_on_zero(self, patch_common):
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        del mock_graph.semantic_search

        call_count = {"n": 0}

        async def fts(query, limit=20, operator="AND"):
            call_count["n"] += 1
            # First call (AND): empty. Second call (OR): one hit.
            if operator == "AND":
                return []
            return [make_discovery(id="r1", summary="Recovered")]

        mock_graph.full_text_search = AsyncMock(side_effect=fts)

        result = await handle_search_knowledge_graph({"query": "two terms"})
        data = parse_result(result)
        assert data["success"] is True
        assert data["count"] == 1
        assert data["fts_operator_used"] == "OR"
        assert data["fts_fallback_used"] is True
        assert call_count["n"] == 2

    @pytest.mark.asyncio
    async def test_explicit_or_skips_fallback(self, patch_common):
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        del mock_graph.semantic_search

        async def fts(query, limit=20, operator="AND"):
            assert operator == "OR"  # caller forced it
            return [make_discovery(id="o1")]

        mock_graph.full_text_search = AsyncMock(side_effect=fts)

        result = await handle_search_knowledge_graph({
            "query": "two terms",
            "operator": "OR",
        })
        data = parse_result(result)
        assert data["success"] is True
        assert data["fts_operator_used"] == "OR"
        assert data["fts_fallback_used"] is False

    @pytest.mark.asyncio
    async def test_invalid_operator_rejected(self, patch_common):
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph
        result = await handle_search_knowledge_graph({
            "query": "x y",
            "operator": "XOR",
        })
        data = parse_result(result)
        assert data["success"] is False
        assert "Invalid operator" in data["error"]


class TestKgSearchComplexityCeiling:
    """Broad auto queries stay inside the MCP tool budget."""

    @pytest.mark.asyncio
    async def test_auto_hybrid_skips_long_query_unless_forced(self, patch_common, monkeypatch):
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        monkeypatch.setenv("UNITARES_ENABLE_HYBRID", "1")
        disc = make_discovery(id="sem-1", summary="Semantic match")
        mock_graph.semantic_search = AsyncMock(return_value=[(disc, 0.82)])
        mock_graph.full_text_search = AsyncMock(return_value=[disc])

        # 13 terms — above the (decoupled) hybrid cap of 12, so auto still
        # skips RRF fusion for truly pathological term dumps.
        result = await handle_search_knowledge_graph({
            "query": "a b c d e f g h i j k l m",
        })
        data = parse_result(result)

        assert data["success"] is True
        assert data["search_mode_used"] == "semantic"
        assert "hybrid_skipped_reason" in data
        assert "limit 12" in data["hybrid_skipped_reason"]
        mock_graph.semantic_search.assert_awaited_once()
        mock_graph.full_text_search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_hybrid_runs_for_normal_multiterm_query(self, patch_common, monkeypatch):
        """Dogfood 2026-06-13 P2.8: a 10-term conceptual query is normal for
        agents and must get RRF fusion, not silently degrade to pure-semantic.
        The hybrid cap is decoupled from the (lower) OR-recall cap."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        monkeypatch.setenv("UNITARES_ENABLE_HYBRID", "1")
        disc = make_discovery(id="sem-1", summary="Semantic match")
        mock_graph.semantic_search = AsyncMock(return_value=[(disc, 0.82)])
        mock_graph.full_text_search = AsyncMock(return_value=[disc])

        # 10 terms — above the old shared cap of 4, below the new hybrid cap.
        result = await handle_search_knowledge_graph({
            "query": "alpha beta gamma delta epsilon zeta eta theta iota kappa",
        })
        data = parse_result(result)

        assert data["success"] is True
        assert data["search_mode_used"] == "hybrid_rrf"
        assert "hybrid_skipped_reason" not in data
        # Hybrid fuses semantic + an AND FTS query.
        mock_graph.semantic_search.assert_awaited()
        mock_graph.full_text_search.assert_awaited()
        assert mock_graph.full_text_search.await_args.kwargs["operator"] == "AND"

    @pytest.mark.asyncio
    async def test_term_dump_still_skips_automatic_or_fallback(self, patch_common):
        """The OR-recall cap still bounds a pathological term-dump.

        Cap raised 4→24 (2026-06-20) so ordinary natural-language questions get
        OR recall, but a 25-term dump still skips the automatic OR fallback so a
        pasted paragraph can't OR against the whole corpus. Pass operator='OR'
        explicitly to override.
        """
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        del mock_graph.semantic_search
        mock_graph.full_text_search = AsyncMock(return_value=[])

        # 25 terms — above the new OR-recall cap of 24.
        result = await handle_search_knowledge_graph({
            "query": " ".join(f"term{i}" for i in range(25)),
        })
        data = parse_result(result)

        assert data["success"] is True
        assert data["search_mode_used"] == "fts"
        assert data["fts_operator_used"] == "AND"
        assert data["fts_fallback_used"] is False
        assert "fts_fallback_skipped_reason" in data
        assert mock_graph.full_text_search.await_count == 1
        assert mock_graph.full_text_search.await_args.kwargs["operator"] == "AND"


class TestIssue165ListScope:
    """list response declares its scope and accepts epoch_scope/including_cold."""

    @pytest.mark.asyncio
    async def test_list_passes_scope_params_through(self, patch_common):
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_list_knowledge_graph

        captured: Dict[str, Any] = {}

        async def get_stats(**kw):
            captured.update(kw)
            return {
                "total_discoveries": 7,
                "total_agents": 1,
                "scope": {
                    "kind": "raw_status_aggregate",
                    "epoch_scope": kw.get("epoch_scope"),
                    "including_cold": kw.get("including_cold"),
                },
            }

        mock_graph.get_stats = AsyncMock(side_effect=get_stats)

        result = await handle_list_knowledge_graph({
            "epoch_scope": "all",
            "including_cold": True,
        })
        data = parse_result(result)
        assert data["success"] is True
        assert captured == {"epoch_scope": "all", "including_cold": True}
        assert data["stats"]["scope"]["epoch_scope"] == "all"
        assert data["stats"]["scope"]["including_cold"] is True

    @pytest.mark.asyncio
    async def test_list_rejects_invalid_epoch_scope(self, patch_common):
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_list_knowledge_graph
        result = await handle_list_knowledge_graph({"epoch_scope": "nope"})
        data = parse_result(result)
        assert data["success"] is False
        assert "Invalid epoch_scope" in data["error"]

    @pytest.mark.asyncio
    async def test_list_falls_back_for_old_backend(self, patch_common):
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_list_knowledge_graph

        # Older backend signature (no scope kwargs) → TypeError → handler
        # retries without kwargs and annotates the response.
        async def old_get_stats(**kw):
            if kw:
                raise TypeError("get_stats() got unexpected kw")
            return {"total_discoveries": 1, "total_agents": 1}

        mock_graph.get_stats = AsyncMock(side_effect=old_get_stats)
        result = await handle_list_knowledge_graph({"epoch_scope": "current"})
        data = parse_result(result)
        assert data["success"] is True
        assert data["stats"]["scope"]["epoch_scope"] == "unknown"


class TestIssue165Provenance:
    """Implicit and explicit writes are distinguishable via provenance.source."""

    def test_explicit_source_classifier(self):
        from src.knowledge_graph import is_explicit_source, tag_provenance_source
        assert is_explicit_source(tag_provenance_source(None, "explicit_store"))
        assert is_explicit_source(tag_provenance_source(None, "explicit_answer"))
        assert is_explicit_source(tag_provenance_source(None, "explicit_leave_note"))
        # Implicit / unknown / legacy
        assert not is_explicit_source(tag_provenance_source(None, "self_recovery_quick_resume"))
        assert not is_explicit_source(tag_provenance_source(None, "operator_resume"))
        assert not is_explicit_source(None)
        assert not is_explicit_source({})
        assert not is_explicit_source({"source": "rando"})

    def test_tag_does_not_clobber_existing_keys(self):
        from src.knowledge_graph import tag_provenance_source
        existing = {"system_version": "v2.13.0", "captured_at": "2026-04-25"}
        out = tag_provenance_source(existing, "explicit_store")
        assert out["source"] == "explicit_store"
        assert out["system_version"] == "v2.13.0"
        assert out["captured_at"] == "2026-04-25"
        # Existing source must not be overwritten
        with_existing_source = {"source": "preserved"}
        out2 = tag_provenance_source(with_existing_source, "explicit_store")
        assert out2["source"] == "preserved"

    @pytest.mark.asyncio
    async def test_store_discovery_internal_requires_source(self, patch_common):
        from src.mcp_handlers.knowledge.handlers import store_discovery_internal
        with pytest.raises(TypeError):
            await store_discovery_internal(  # type: ignore[call-arg]
                agent_id="a", summary="s",
            )

    @pytest.mark.asyncio
    async def test_store_discovery_internal_tags_provenance(self, patch_common):
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import store_discovery_internal
        await store_discovery_internal(
            agent_id="a-1", summary="recovery", source="self_recovery_quick_resume",
        )
        mock_graph.add_discovery.assert_awaited()
        node = mock_graph.add_discovery.await_args.args[0]
        assert node.provenance["source"] == "self_recovery_quick_resume"
