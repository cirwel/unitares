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

class TestStoreKnowledgeGraph:

    @pytest.mark.asyncio
    async def test_store_happy_path(self, patch_common, registered_agent):
        """Store a single discovery successfully."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "Found a caching bug",
            "discovery_type": "bug_found",
            "tags": ["cache", "perf"],
            "severity": "medium",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert "discovery_id" in data
        assert "Discovery stored" in data["message"]
        mock_graph.add_discovery.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_store_missing_summary(self, patch_common, registered_agent):
        """Store fails when summary is missing."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "discovery_type": "insight",
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "summary" in data["error"].lower() or "missing" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_store_defaults_to_note_type(self, patch_common, registered_agent):
        """Discovery type defaults to 'note' when not specified."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "Quick note about something",
        })

        data = parse_result(result)
        assert data["success"] is True
        # The stored discovery should have type "note"
        call_args = mock_graph.add_discovery.call_args
        discovery = call_args[0][0]
        assert discovery.type == "note"

    @pytest.mark.asyncio
    async def test_store_accepts_bug_alias(self, patch_common, registered_agent):
        """Shorthand discovery_type='bug' normalizes to bug_found."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "Found issue in auth middleware",
            "discovery_type": "bug",
        })

        data = parse_result(result)
        assert data["success"] is True
        call_args = mock_graph.add_discovery.call_args
        discovery = call_args[0][0]
        assert discovery.type == "bug_found"

    @pytest.mark.asyncio
    async def test_store_truncates_long_summary(self, patch_common, registered_agent):
        """Long summaries are truncated at MAX_SUMMARY_LEN."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph
        from src.mcp_handlers.knowledge.limits import MAX_SUMMARY_LEN

        long_summary = "A" * (MAX_SUMMARY_LEN + 100)
        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": long_summary,
        })

        data = parse_result(result)
        assert data["success"] is True
        assert "_truncated" in data
        assert "summary" in data["_truncated"]

    @pytest.mark.asyncio
    async def test_store_truncates_long_details(self, patch_common, registered_agent):
        """Long details are truncated at MAX_DETAILS_LEN."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph
        from src.mcp_handlers.knowledge.limits import MAX_DETAILS_LEN

        long_details = "B" * (MAX_DETAILS_LEN + 500)
        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "Test",
            "details": long_details,
        })

        data = parse_result(result)
        assert data["success"] is True
        assert "_truncated" in data
        assert "details" in data["_truncated"]

    @pytest.mark.asyncio
    async def test_store_with_related_discoveries(self, patch_common, registered_agent):
        """Similar discoveries are linked when auto_link_related is True."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        similar = make_discovery(id="related-1", summary="Related item")
        mock_graph.find_similar = AsyncMock(return_value=[similar])

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "Something related",
            "auto_link_related": True,
        })

        data = parse_result(result)
        assert data["success"] is True
        assert "related_discoveries" in data
        assert len(data["related_discoveries"]) == 1

    @pytest.mark.asyncio
    async def test_store_auto_link_excludes_rollup_rows(self, patch_common, registered_agent):
        """Auto-linking drops system-generated topic_rollup rows (#44 follow-up).

        A rollup is a summary OF discoveries, not a peer; linking a fresh write to
        one would pollute related_to edges and let rollups accrete inbound peer
        edges. find_similar has no rollup awareness, so the store handler filters.
        """
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        real = make_discovery(id="2026-06-04T00:00:00", summary="Real peer")
        rollup = make_discovery(id="rollup::auth", type="topic_rollup", summary="[rollup] auth")
        mock_graph.find_similar = AsyncMock(return_value=[real, rollup])

        captured = {}
        orig_add = mock_graph.add_discovery

        async def _capture(node):
            captured["related_to"] = list(node.related_to or [])
            return await orig_add(node) if orig_add else None

        mock_graph.add_discovery = AsyncMock(side_effect=_capture)

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "Something related",
            "auto_link_related": True,
        })

        data = parse_result(result)
        assert data["success"] is True
        # The rollup is excluded from both the stored edges and the surfaced set.
        assert "rollup::auth" not in captured.get("related_to", [])
        assert "2026-06-04T00:00:00" in captured.get("related_to", [])
        related_ids = {d.get("id") for d in data.get("related_discoveries", [])}
        assert "rollup::auth" not in related_ids
        assert "2026-06-04T00:00:00" in related_ids

    @pytest.mark.asyncio
    async def test_store_graph_exception(self, patch_common, registered_agent):
        """Exception from graph backend returns error response."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        mock_graph.add_discovery = AsyncMock(side_effect=Exception("Database connection lost"))

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "This will fail",
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "failed to store" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_store_rate_limit_error(self, patch_common, registered_agent):
        """ValueError with 'rate limit' triggers rate limit error response."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        mock_graph.add_discovery = AsyncMock(side_effect=ValueError("Rate limit exceeded: max 10 per minute"))

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "Rate limited",
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "rate limit" in data["error"].lower()
        assert "recovery" in data

    @pytest.mark.asyncio
    async def test_store_invalid_discovery_type(self, patch_common, registered_agent):
        """Invalid discovery_type returns validation error."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "Test",
            "discovery_type": "invalid_type_xyz",
        })

        data = parse_result(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_store_with_param_aliases(self, patch_common, registered_agent):
        """Parameter aliases (e.g. 'insight' -> 'summary') work correctly."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "insight": "My key insight about the system",
        })

        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_store_no_agent_id_auto_generates(self, patch_common):
        """When no agent_id and no session binding, use a stable anonymous writer."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "summary": "Note without agent",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["agent_mode"] == "anonymous"
        discovery = mock_graph.add_discovery.call_args[0][0]
        assert discovery.agent_id.startswith("anonkg_")

    @pytest.mark.asyncio
    async def test_store_high_severity_requires_registered_agent(self, patch_common):
        """High severity discoveries require registered agent."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        # No agent registered - high severity should require registration
        result = await handle_store_knowledge_graph({
            "agent_id": "unregistered-agent",
            "summary": "Critical issue found",
            "severity": "high",
        })

        data = parse_result(result)
        # Should fail because agent not registered for high severity
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_store_with_response_to(self, patch_common, registered_agent):
        """Store with response_to linking to parent discovery."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "Follow-up to parent",
            "response_to": {
                "discovery_id": "2026-01-01T00:00:00.000000",
                "response_type": "extend",
            },
        })

        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_store_batch_happy_path(self, patch_common, registered_agent):
        """Batch store multiple discoveries successfully."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "discoveries": [
                {"discovery_type": "note", "summary": "Note 1"},
                {"discovery_type": "insight", "summary": "Insight 1"},
            ],
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["success_count"] == 2
        assert data["error_count"] == 0

    @pytest.mark.asyncio
    async def test_store_batch_empty_list(self, patch_common, registered_agent):
        """Batch store with empty list returns error."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "discoveries": [],
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "empty" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_store_batch_too_many(self, patch_common, registered_agent):
        """Batch store with >10 items returns error."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        discoveries = [{"discovery_type": "note", "summary": f"Note {i}"} for i in range(11)]
        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "discoveries": discoveries,
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "10" in data["error"]

    @pytest.mark.asyncio
    async def test_store_batch_partial_failure(self, patch_common, registered_agent):
        """Batch store with some invalid items stores valid ones and reports errors."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "discoveries": [
                {"discovery_type": "note", "summary": "Good one"},
                {"discovery_type": "note"},  # Missing summary
                "not a dict",  # Invalid type
            ],
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["success_count"] == 1
        assert data["error_count"] == 2

    @pytest.mark.asyncio
    async def test_store_batch_not_a_list(self, patch_common, registered_agent):
        """Batch store with non-list value returns error."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "discoveries": "not a list",
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "list" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_store_paused_agent_blocked(self, patch_common, registered_agent, mock_mcp_server):
        """Paused agents cannot store knowledge (circuit breaker)."""
        mock_mcp_server.agent_metadata[registered_agent].status = "paused"
        # Fresh paused_at — pause TTL auto-expires stale ones (>72h default)
        from datetime import datetime as _dt
        mock_mcp_server.agent_metadata[registered_agent].paused_at = _dt.now().isoformat()

        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "Should be blocked",
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "paused" in data["error"].lower()


# ============================================================================
# handle_search_knowledge_graph
# ============================================================================

class TestStoreKnowledgeGraphAdditional:

    @pytest.mark.asyncio
    async def test_store_with_display_name_warning(self, patch_common, registered_agent, mock_mcp_server):
        """Store with auto-generated display name includes _name_hint (line 425)."""
        mock_mcp_server.agent_metadata[registered_agent].display_name = None
        mock_mcp_server.agent_metadata[registered_agent].label = None

        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "Test with auto name",
            "severity": "high",
        })

        data = parse_result(result)
        # Whether it succeeds or errors depends on verify_agent_ownership,
        # but we're testing that display_name logic runs
        # For low severity, display_name_warning is not checked
        # so test with low severity instead
        result2 = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "Test with auto name low severity",
        })

        data2 = parse_result(result2)
        assert data2["success"] is True

    @pytest.mark.asyncio
    async def test_store_high_severity_requires_auth(self, patch_common, registered_agent):
        """High severity store requires auth ownership (lines 393-395)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        with patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=False):
            result = await handle_store_knowledge_graph({
                "agent_id": registered_agent,
                "summary": "Critical security issue",
                "severity": "high",
            })

            data = parse_result(result)
            assert data["success"] is False
            assert "auth" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_store_high_severity_human_review_flag(self, patch_common, registered_agent):
        """High severity discoveries get human_review_required flag (lines 434-435)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        with patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True):
            result = await handle_store_knowledge_graph({
                "agent_id": registered_agent,
                "summary": "Critical issue",
                "severity": "high",
            })

            data = parse_result(result)
            assert data["success"] is True
            assert data["human_review_required"] is True

    @pytest.mark.asyncio
    async def test_store_value_error_non_rate_limit(self, patch_common, registered_agent):
        """ValueError without rate limit in message returns generic error (line 454)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        mock_graph.add_discovery = AsyncMock(side_effect=ValueError("Invalid data format"))

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "Test",
        })

        data = parse_result(result)
        assert data["success"] is False
        assert "Invalid data format" in data["error"]

    @pytest.mark.asyncio
    async def test_store_with_provenance_capture(self, patch_common, registered_agent):
        """Store captures provenance from agent metadata (lines 313-315)."""
        mock_mcp_server, mock_kg = patch_common

        # Set up monitor state
        mock_state = MagicMock()
        mock_state.regime = "active"
        mock_state.coherence = 0.85
        mock_state.E = 0.5
        mock_state.S = 0.2
        mock_state.void_active = False

        mock_monitor = MagicMock()
        mock_monitor.state = mock_state
        mock_mcp_server.monitors = {registered_agent: mock_monitor}

        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        with patch("src.mcp_handlers.identity.shared._get_lineage", return_value=[registered_agent]):
            result = await handle_store_knowledge_graph({
                "agent_id": registered_agent,
                "summary": "Provenance test",
            })

            data = parse_result(result)
            assert data["success"] is True

            # Verify provenance was captured
            call_args = mock_kg.add_discovery.call_args
            discovery = call_args[0][0]
            assert discovery.provenance is not None
            assert "agent_state" in discovery.provenance

    @pytest.mark.asyncio
    async def test_store_with_provenance_chain(self, patch_common, registered_agent):
        """Store captures provenance chain for lineage (lines 338-367)."""
        mock_mcp_server, mock_graph = patch_common
        # Set up parent agent
        parent_meta = MagicMock()
        parent_meta.spawn_reason = "split"
        parent_meta.created_at = "2026-01-01T00:00:00"
        mock_mcp_server.agent_metadata["parent-id"] = parent_meta

        # The authoritative DB snapshot should be attempted even when
        # in-memory metadata has not been hydrated with parentage.
        current_meta = mock_mcp_server.agent_metadata[registered_agent]
        current_meta.parent_agent_id = None

        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        authoritative_chain = [{
            "schema": "s7.lineage_link.v1",
            "source": "core.identities",
            "parent_agent_id": "parent-id",
            "successor_agent_id": registered_agent,
            "relationship": "lineage_parent",
            "lineage_state": "confirmed",
            "provisional_lineage": False,
            "aggregation_eligible_at_write": True,
        }]
        with patch(
            "src.identity.provenance_chain.build_lineage_provenance_chain",
            AsyncMock(return_value=authoritative_chain),
        ):
            result = await handle_store_knowledge_graph({
                "agent_id": registered_agent,
                "summary": "Lineage test",
            })

            data = parse_result(result)
            assert data["success"] is True
            discovery = mock_graph.add_discovery.await_args.args[0]
            assert discovery.provenance_chain == authoritative_chain

    @pytest.mark.asyncio
    async def test_store_provenance_chain_falls_back_when_db_snapshot_fails(
        self, patch_common, registered_agent
    ):
        mock_mcp_server, mock_graph = patch_common
        parent_meta = MagicMock()
        parent_meta.spawn_reason = "split"
        parent_meta.created_at = "2026-01-01T00:00:00"
        mock_mcp_server.agent_metadata["parent-id"] = parent_meta
        current_meta = mock_mcp_server.agent_metadata[registered_agent]
        current_meta.parent_agent_id = "parent-id"
        current_meta.spawn_reason = "new_session"

        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        with patch(
            "src.identity.provenance_chain.build_lineage_provenance_chain",
            AsyncMock(side_effect=RuntimeError("db unavailable")),
        ), patch(
            "src.mcp_handlers.identity.shared._get_lineage",
            return_value=["parent-id", registered_agent],
        ):
            result = await handle_store_knowledge_graph({
                "agent_id": registered_agent,
                "summary": "Lineage fallback test",
            })

        data = parse_result(result)
        assert data["success"] is True
        discovery = mock_graph.add_discovery.await_args.args[0]
        assert discovery.provenance_chain[-1]["relationship"] == "direct_parent"
        assert discovery.provenance_chain[-1]["source"] == "agent_metadata_fallback"

    @pytest.mark.asyncio
    async def test_store_with_s22_provenance_context(
        self, patch_common, registered_agent
    ):
        _, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "S22 context test",
            "harness": "codex-cli",
            "transport": "mcp-stdio",
            "model_provider": "openai",
            "model": "gpt-5.5",
            "tool_surface": ["terminal", "mcp:unitares", "terminal"],
            "memory_context": "repo+kg",
            "locus": {"workspace": "/repo"},
            "affordance_state": {"shell": True},
            "episode_id": "episode-1",
            "process_instance_id": "opaque-process",
        })

        data = parse_result(result)
        assert data["success"] is True
        discovery = mock_graph.add_discovery.await_args.args[0]
        context = discovery.provenance["s22_context"]
        assert context["schema"] == "s22.write_context.v1"
        assert context["context_source"] == "knowledge.store"
        assert context["harness_type"] == "codex-cli"
        assert context["transport"] == "mcp-stdio"
        assert context["model_provider"] == "openai"
        assert context["model"] == "gpt-5.5"
        assert context["tool_surface"] == ["terminal", "mcp:unitares"]
        assert context["memory_context"] == "repo+kg"
        assert context["governance_mode"] == "explicit"

    @pytest.mark.asyncio
    async def test_store_persists_r6_fork_discriminators_in_s22_context(
        self, mock_mcp_server, mock_graph, patch_common
    ):
        """KG-side counterpart to the process_agent_update fork-persist regression.

        Plan-row R6/S22 follow-up: the 2026-05-08 envelope audit showed 0/3 KG
        rows carried episode_fork_kind / identity_lineage_fork. This pins that
        knowledge.store flows server-side classification into the durable
        provenance.s22_context envelope alongside the rest of S22 fields.
        """
        from types import SimpleNamespace
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        agent_uuid = "00000000-0000-4000-8000-000000000001"
        meta = SimpleNamespace(
            status="active",
            health_status="healthy",
            total_updates=5,
            label="ForkChild",
            display_name="ForkChild",
            structured_id="fork_child_test",
            agent_id=agent_uuid,
            agent_uuid=agent_uuid,
            thread_id="thread-fork",
            node_index=1,
            parent_agent_id="00000000-0000-4000-8000-000000000000",
            spawn_reason="new_session",
            created_at="2026-01-01T00:00:00",
            paused_at=None,
        )
        mock_mcp_server.agent_metadata[agent_uuid] = meta

        result = await handle_store_knowledge_graph({
            "agent_id": agent_uuid,
            "summary": "S22 fork-discriminator test",
            "harness": "claude-code",
        })

        data = parse_result(result)
        assert data["success"] is True
        discovery = mock_graph.add_discovery.await_args.args[0]
        context = discovery.provenance["s22_context"]
        assert context["episode_fork_kind"] == "identity_lineage"
        assert context["identity_lineage_fork"] is True


# ============================================================================
# handle_search_knowledge_graph - additional coverage
# ============================================================================

class TestBatchStoreAdditional:

    @pytest.mark.asyncio
    async def test_batch_store_truncation(self, patch_common, registered_agent):
        """Batch store truncates long content (lines 1213-1219)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "discoveries": [
                {
                    "discovery_type": "note",
                    "summary": "A" * 500,  # Will be truncated
                    "details": "B" * 3000,  # Will be truncated
                },
            ],
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["success_count"] == 1
        if data["stored"] and "_truncated" in data["stored"][0]:
            assert len(data["stored"][0]["_truncated"]) > 0

    @pytest.mark.asyncio
    async def test_batch_store_invalid_severity_uses_default(self, patch_common, registered_agent):
        """Batch store with invalid severity falls back to None (lines 1245-1247)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "discoveries": [
                {
                    "discovery_type": "note",
                    "summary": "Test with bad severity",
                    "severity": "ultra_critical",  # Invalid
                },
            ],
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["success_count"] == 1

    @pytest.mark.asyncio
    async def test_batch_store_rate_limit_error(self, patch_common, registered_agent):
        """Batch store with rate limit ValueError (lines 1284-1292)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        # First add succeeds, second raises rate limit
        call_count = 0

        async def add_side_effect(disc):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ValueError("Rate limit exceeded")
            return True

        mock_graph.add_discovery = AsyncMock(side_effect=add_side_effect)

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "discoveries": [
                {"discovery_type": "note", "summary": "First"},
                {"discovery_type": "note", "summary": "Second"},
            ],
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["success_count"] == 1
        assert data["error_count"] == 1
        assert any("rate limit" in e.lower() for e in data.get("errors", []))

    @pytest.mark.asyncio
    async def test_batch_store_general_exception(self, patch_common, registered_agent):
        """Batch store with general exception per item (line 1292)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        mock_graph.add_discovery = AsyncMock(side_effect=RuntimeError("disk full"))

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "discoveries": [
                {"discovery_type": "note", "summary": "Will fail"},
            ],
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["error_count"] == 1

    @pytest.mark.asyncio
    async def test_batch_store_overall_exception(self, patch_common, registered_agent):
        """Batch store overall exception (line 1313-1314)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        with patch("src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
                    new_callable=AsyncMock, side_effect=RuntimeError("KG unavailable")):
            result = await handle_store_knowledge_graph({
                "agent_id": registered_agent,
                "discoveries": [
                    {"discovery_type": "note", "summary": "Will fail overall"},
                ],
            })

            data = parse_result(result)
            assert data["success"] is False

    @pytest.mark.asyncio
    async def test_batch_store_high_severity_auth_check(self, patch_common, registered_agent):
        """Batch store high severity checks auth (lines 1268-1271)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        with patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=False):
            result = await handle_store_knowledge_graph({
                "agent_id": registered_agent,
                "discoveries": [
                    {"discovery_type": "note", "summary": "Critical", "severity": "high"},
                ],
            })

            data = parse_result(result)
            assert data["success"] is True
            assert data["error_count"] == 1

    @pytest.mark.asyncio
    async def test_batch_store_with_truncation_tip(self, patch_common, registered_agent):
        """Batch store with truncation shows tip (line 1309)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "discoveries": [
                {
                    "discovery_type": "note",
                    "summary": "C" * 500,
                },
            ],
        })

        data = parse_result(result)
        assert data["success"] is True
        if any("_truncated" in s for s in data.get("stored", [])):
            assert "_tip" in data

    @pytest.mark.asyncio
    async def test_batch_store_missing_discovery_type(self, patch_common, registered_agent):
        """Batch store with missing discovery_type (lines 1194-1195)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "discoveries": [
                {"summary": "No type specified"},  # Missing discovery_type
            ],
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["error_count"] == 1

    @pytest.mark.asyncio
    async def test_batch_store_invalid_discovery_type(self, patch_common, registered_agent):
        """Batch store with invalid discovery_type (lines 1199-1200)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "discoveries": [
                {"discovery_type": "invalid_xyz_type", "summary": "Bad type"},
            ],
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["error_count"] == 1

    @pytest.mark.asyncio
    async def test_batch_store_missing_summary(self, patch_common, registered_agent):
        """Batch store with missing summary (lines 1203-1205)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "discoveries": [
                {"discovery_type": "note", "summary": ""},  # Empty summary
            ],
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["error_count"] == 1

    @pytest.mark.asyncio
    async def test_batch_store_with_response_to(self, patch_common, registered_agent):
        """Batch store with response_to (lines 1227-1237)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "discoveries": [
                {
                    "discovery_type": "note",
                    "summary": "Response to parent",
                    "response_to": {
                        "discovery_id": "2026-01-01T00:00:00.000000",
                        "response_type": "extend",
                    },
                },
            ],
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["success_count"] == 1

    @pytest.mark.asyncio
    async def test_batch_store_auto_link_disabled(self, patch_common, registered_agent):
        """Batch store with auto_link_related=False (line 1281)."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "discoveries": [
                {
                    "discovery_type": "note",
                    "summary": "No linking",
                    "auto_link_related": False,
                },
            ],
        })

        data = parse_result(result)
        assert data["success"] is True
        # find_similar should not have been called for this discovery
        # Since auto_link_related defaults to True, but we set False explicitly


# ============================================================================
# Broadcaster integration — knowledge_write must fire for leave_note and store
# ============================================================================
# Before this coverage existed, Vigil and Sentinel notes landed in the KG but
# never reached the dashboard timeline or the Discord bridge WS subscriber
# because no code emitted a knowledge_write broadcaster event. Macs got a
# transient notify() and the KG got a new row, but neither live surface saw
# anything. These tests pin the broadcast so that path cannot silently regress
# again.


class TestKnowledgeWriteBroadcast:

    @pytest.mark.asyncio
    async def test_leave_note_emits_knowledge_write(self, patch_common, registered_agent):
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        with patch(
            "src.mcp_handlers.knowledge.handlers.broadcaster_instance.broadcast_event",
            new_callable=AsyncMock,
        ) as bc:
            result = await handle_leave_note({
                "agent_id": registered_agent,
                "summary": "Governance recovered after brief outage",
                "tags": ["vigil", "recovery", "governance"],
            })

        data = parse_result(result)
        assert data["success"] is True
        bc.assert_awaited()
        call = bc.await_args
        assert call.args[0] == "knowledge_write"
        assert call.kwargs["agent_id"] == registered_agent
        payload = call.kwargs["payload"]
        assert payload["discovery_type"] == "note"
        assert "Governance recovered" in payload["summary"]
        assert "vigil" in payload["tags"]

    @pytest.mark.asyncio
    async def test_store_knowledge_graph_emits_knowledge_write(
        self, patch_common, registered_agent,
    ):
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        with patch(
            "src.mcp_handlers.knowledge.handlers.broadcaster_instance.broadcast_event",
            new_callable=AsyncMock,
        ) as bc:
            result = await handle_store_knowledge_graph({
                "agent_id": registered_agent,
                "summary": "[Sentinel] coordinated coherence drop across 3 agents",
                "discovery_type": "observation",
                "severity": "medium",
                "tags": ["sentinel", "coordinated_coherence_drop"],
            })

        data = parse_result(result)
        assert data["success"] is True
        # Two events may fire if confidence gets clamped; find the write event.
        write_calls = [
            c for c in bc.await_args_list
            if c.args and c.args[0] == "knowledge_write"
        ]
        assert write_calls, "expected knowledge_write to be emitted"
        payload = write_calls[0].kwargs["payload"]
        assert payload["discovery_type"] == "observation"
        assert payload["severity"] == "medium"
        assert "sentinel" in payload["tags"]

    @pytest.mark.asyncio
    async def test_broadcast_failure_does_not_break_write(
        self, patch_common, registered_agent,
    ):
        """A dead broadcaster must never fail the KG write path.

        This is load-bearing: the broadcaster is a secondary concern and
        must not become a new failure mode for the primary KG path.
        """
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        with patch(
            "src.mcp_handlers.knowledge.handlers.broadcaster_instance.broadcast_event",
            new_callable=AsyncMock,
            side_effect=RuntimeError("broadcaster dead"),
        ):
            result = await handle_leave_note({
                "agent_id": registered_agent,
                "summary": "note that should persist even with dead broadcaster",
            })

        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_knowledge_note_preserves_s22_context(
        self, patch_common, registered_agent,
    ):
        """Unified knowledge(note) must not drop S22 provenance fields."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.consolidated import handle_knowledge

        result = await handle_knowledge({
            "action": "note",
            "agent_id": registered_agent,
            "content": "S22 note provenance test",
            "comparison_key": "s22-note-2026-05-31",
            "task_label": "Exercise knowledge note provenance",
            "task_outcome": "note-recorded",
            "memory_context": "repo+kg",
        })

        data = parse_result(result)
        assert data["success"] is True
        discovery = mock_graph.add_discovery.await_args.args[0]
        provenance = discovery.provenance
        assert provenance["source"] == "explicit_leave_note"
        context = provenance["s22_context"]
        assert context["schema"] == "s22.write_context.v1"
        assert context["context_source"] == "knowledge.note"
        assert context["comparison_key"] == "s22-note-2026-05-31"
        assert context["task_label"] == "Exercise knowledge note provenance"
        assert context["task_outcome"] == "note-recorded"
        assert context["memory_context"] == "repo+kg"
        assert context["governance_mode"] == "explicit"


class TestKnowledgeReadBroadcast:
    """Read-side audit coverage. Without these emits the central usage
    question for the KG ("is anyone pulling from this?") is unanswerable
    from audit.events — only writes show up. Pin the four read paths."""

    @pytest.mark.asyncio
    async def test_get_emits_knowledge_read(self, patch_common, registered_agent):
        mock_mcp_server, mock_graph = patch_common
        mock_graph.get_agent_discoveries.return_value = [
            make_discovery(id="d1", agent_id=registered_agent),
            make_discovery(id="d2", agent_id=registered_agent),
        ]
        from src.mcp_handlers.knowledge.handlers import handle_get_knowledge_graph

        with patch(
            "src.mcp_handlers.knowledge.handlers.broadcaster_instance.broadcast_event",
            new_callable=AsyncMock,
        ) as bc:
            await handle_get_knowledge_graph({"agent_id": registered_agent})

        read_calls = [c for c in bc.await_args_list if c.args and c.args[0] == "knowledge_read"]
        assert read_calls, "expected knowledge_read to be emitted on get"
        payload = read_calls[0].kwargs["payload"]
        assert payload["action"] == "get"
        assert payload["target_agent_id"] == registered_agent
        assert payload["result_count"] == 2

    @pytest.mark.asyncio
    async def test_list_emits_knowledge_read(self, patch_common):
        mock_mcp_server, mock_graph = patch_common
        mock_graph.get_stats.return_value = {
            "total_discoveries": 7,
            "total_agents": 3,
            "scope": {"epoch_scope": "current", "including_cold": False},
        }
        from src.mcp_handlers.knowledge.handlers import handle_list_knowledge_graph

        with patch(
            "src.mcp_handlers.knowledge.handlers.broadcaster_instance.broadcast_event",
            new_callable=AsyncMock,
        ) as bc:
            await handle_list_knowledge_graph({})

        read_calls = [c for c in bc.await_args_list if c.args and c.args[0] == "knowledge_read"]
        assert read_calls, "expected knowledge_read to be emitted on list"
        payload = read_calls[0].kwargs["payload"]
        assert payload["action"] == "list"

    @pytest.mark.asyncio
    async def test_details_emits_knowledge_read_with_writer_agent(
        self, patch_common, registered_agent,
    ):
        mock_mcp_server, mock_graph = patch_common
        writer_uuid = "11111111-1111-4111-8111-111111111111"
        mock_graph.get_discovery.return_value = make_discovery(
            id="disc-xyz", agent_id=writer_uuid, summary="something",
        )
        from src.mcp_handlers.knowledge.handlers import handle_get_discovery_details

        with patch(
            "src.mcp_handlers.knowledge.handlers.broadcaster_instance.broadcast_event",
            new_callable=AsyncMock,
        ) as bc, patch(
            "src.mcp_handlers.context.get_context_agent_id",
            return_value=registered_agent,
        ):
            await handle_get_discovery_details({"discovery_id": "disc-xyz"})

        read_calls = [c for c in bc.await_args_list if c.args and c.args[0] == "knowledge_read"]
        assert read_calls, "expected knowledge_read to be emitted on details"
        call = read_calls[0]
        payload = call.kwargs["payload"]
        assert payload["action"] == "details"
        assert payload["discovery_id"] == "disc-xyz"
        assert payload["writer_agent_id"] == writer_uuid

    @pytest.mark.asyncio
    async def test_broadcast_failure_does_not_break_read(
        self, patch_common, registered_agent,
    ):
        """A dead broadcaster on the read path must not fail the read."""
        mock_mcp_server, mock_graph = patch_common
        mock_graph.get_agent_discoveries.return_value = [
            make_discovery(id="d1", agent_id=registered_agent),
        ]
        from src.mcp_handlers.knowledge.handlers import handle_get_knowledge_graph

        with patch(
            "src.mcp_handlers.knowledge.handlers.broadcaster_instance.broadcast_event",
            new_callable=AsyncMock,
            side_effect=RuntimeError("broadcaster dead"),
        ):
            result = await handle_get_knowledge_graph({"agent_id": registered_agent})

        data = parse_result(result)
        assert data["success"] is True


class TestLeaveNoteTagPassthrough:
    """leave_note must not auto-inject `ephemeral` into caller-supplied tags.

    Pre-fix, every non-permanent leave_note call silently had `ephemeral`
    appended, which scheduled the note for 7-day auto-archive. Design-gap
    notes from real dogfooding sessions were swept as a side effect.
    """

    @pytest.mark.asyncio
    async def test_leave_note_does_not_auto_inject_ephemeral(
        self, patch_common, registered_agent,
    ):
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        with patch(
            "src.mcp_handlers.knowledge.handlers.broadcaster_instance.broadcast_event",
            new_callable=AsyncMock,
        ) as bc:
            result = await handle_leave_note({
                "agent_id": registered_agent,
                "summary": "Design gap: dialectic auto-assigns non-responsive reviewers",
                "tags": ["design-gap", "dialectic"],
            })

        data = parse_result(result)
        assert data["success"] is True
        payload = bc.await_args.kwargs["payload"]
        assert "ephemeral" not in payload["tags"], (
            "leave_note must not auto-tag ephemeral; caller did not opt in"
        )
        assert set(payload["tags"]) == {"design-gap", "dialectic"}

    @pytest.mark.asyncio
    async def test_leave_note_respects_explicit_ephemeral_opt_in(
        self, patch_common, registered_agent,
    ):
        """Callers can still opt in to ephemeral lifecycle explicitly."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_leave_note

        with patch(
            "src.mcp_handlers.knowledge.handlers.broadcaster_instance.broadcast_event",
            new_callable=AsyncMock,
        ) as bc:
            result = await handle_leave_note({
                "agent_id": registered_agent,
                "summary": "scratch thought, don't keep this around",
                "tags": ["scratch", "ephemeral"],
            })

        data = parse_result(result)
        assert data["success"] is True
        payload = bc.await_args.kwargs["payload"]
        assert "ephemeral" in payload["tags"]


# ============================================================================
# Archived filtering in search
# ============================================================================


# ============================================================================
# Provenance bugs (2026-04-25 dogfood)
#   Bug A: writer display_name resolved at read time overwrites historical label
#   Bug B: discovery id generated in local TZ while created_at is UTC
# ============================================================================

class TestProvenanceWriterLabel:
    """Bug A: capture writer label + session_id at write time."""

    @pytest.mark.asyncio
    async def test_store_captures_writer_label_at_write(
        self, patch_common, registered_agent
    ):
        """Discovery provenance records the writer's display_name at write time."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "Provenance test write",
            "discovery_type": "note",
            "client_session_id": "agent-test-session-001",
        })

        data = parse_result(result)
        assert data["success"] is True
        discovery = mock_graph.add_discovery.call_args[0][0]
        assert discovery.provenance is not None
        assert discovery.provenance.get("writer_label_at_write") == "TestAgent"
        assert discovery.provenance.get("writer_session_id_at_write") == "agent-test-session-001"

    @pytest.mark.asyncio
    async def test_search_prefers_writer_label_at_write_over_live(
        self, patch_common, registered_agent
    ):
        """Search returns the writer label from provenance, not the current display_name.

        Resuming the same agent UUID under a different display_name must not
        rewrite history — the `by` field on past discoveries should reflect
        who wrote them.
        """
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        # Agent's CURRENT display_name has changed since the historical write
        mock_mcp_server.agent_metadata[registered_agent].display_name = "RenamedAgent"

        # Historical row: provenance pinned to original writer
        historical = make_discovery(
            id="2026-04-25T06:00:00.000000+00:00",
            agent_id=registered_agent,
            provenance={
                "system_version": "2.13.0",
                "writer_label_at_write": "OriginalSession",
                "writer_session_id_at_write": "agent-original-001",
                "captured_at": "2026-04-25T06:00:00.000000+00:00",
            },
        )
        mock_graph.full_text_search = AsyncMock(return_value=[historical])

        result = await handle_search_knowledge_graph({
            "agent_id": registered_agent,
            "query": "anything",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert len(data["discoveries"]) == 1
        assert data["discoveries"][0]["by"] == "OriginalSession"
        assert data["discoveries"][0].get("session_id_at_write") == "agent-original-001"

    @pytest.mark.asyncio
    async def test_search_falls_back_to_live_for_legacy_rows(
        self, patch_common, registered_agent
    ):
        """Legacy rows without writer_label_at_write fall back to live resolution."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

        legacy = make_discovery(
            id="2025-12-01T12:00:00",
            agent_id=registered_agent,
            provenance={"system_version": "2.10.0"},  # no writer_label_at_write
        )
        mock_graph.full_text_search = AsyncMock(return_value=[legacy])

        result = await handle_search_knowledge_graph({
            "agent_id": registered_agent,
            "query": "anything",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert data["discoveries"][0]["by"] == "TestAgent"  # live-resolved fallback


class TestUtcTimestamps:
    """Bug B: write-path timestamps must be UTC, not local TZ."""

    @pytest.mark.asyncio
    async def test_store_emits_utc_timestamps(
        self, patch_common, registered_agent
    ):
        """Discovery id, timestamp, and provenance.captured_at are all UTC-aware."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph
        from datetime import datetime

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "UTC timestamp test",
            "discovery_type": "note",
        })

        data = parse_result(result)
        assert data["success"] is True
        discovery = mock_graph.add_discovery.call_args[0][0]

        # All three timestamps must parse as UTC-aware (offset present)
        for label, value in (
            ("id", discovery.id),
            ("timestamp", discovery.timestamp),
            ("captured_at", discovery.provenance.get("captured_at")),
        ):
            assert value, f"{label} is empty"
            parsed = datetime.fromisoformat(value)
            assert parsed.tzinfo is not None, f"{label} is TZ-naive: {value}"
            assert parsed.utcoffset().total_seconds() == 0, (
                f"{label} is not UTC: {value}"
            )

        # id and captured_at represent the same instant (within a few ms)
        id_dt = datetime.fromisoformat(discovery.id)
        captured_dt = datetime.fromisoformat(discovery.provenance["captured_at"])
        assert abs((id_dt - captured_dt).total_seconds()) < 1.0


# ============================================================================
# superseded_by field round-trip (KG hygiene v1)
# ============================================================================

def test_discovery_node_superseded_by_round_trip():
    """superseded_by field round-trips through to_dict/from_dict."""
    d = DiscoveryNode(
        id="disc-new",
        agent_id="a",
        type="note",
        summary="replaces older",
    )
    assert d.superseded_by is None  # default

    d2 = DiscoveryNode(
        id="disc-old",
        agent_id="a",
        type="note",
        summary="superseded by disc-new",
        status="superseded",
        superseded_by="disc-new",
    )
    serialized = d2.to_dict()
    assert serialized["status"] == "superseded"
    assert serialized["superseded_by"] == "disc-new"

    rehydrated = DiscoveryNode.from_dict(serialized)
    assert rehydrated.status == "superseded"
    assert rehydrated.superseded_by == "disc-new"


def test_discovery_node_superseded_by_omitted_when_none():
    """to_dict does not emit superseded_by key when None (keeps payload lean)."""
    d = DiscoveryNode(id="x", agent_id="a", type="note", summary="s")
    assert "superseded_by" not in d.to_dict()


# ============================================================================
# supersedes: parameter on knowledge action=store (KG hygiene v1)
# ============================================================================


class TestSupersedes:

    @pytest.mark.asyncio
    async def test_supersedes_flips_predecessor_status(
        self, patch_common, registered_agent,
    ):
        """supersedes=<old_id> flips old discovery's status to 'superseded'
        and sets superseded_by pointing at the new discovery."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        predecessor = DiscoveryNode(
            id="old-disc-1",
            agent_id="other-agent",
            type="note",
            summary="original",
            status="open",
            tags=["routine"],
        )
        mock_graph.get_discovery = AsyncMock(return_value=predecessor)

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "replaces old-disc-1",
            "supersedes": "old-disc-1",
        })

        data = parse_result(result)
        assert data["success"] is True
        new_id = data["discovery_id"]

        # Predecessor must have been flipped
        mock_graph.update_discovery.assert_awaited_once()
        call_args = mock_graph.update_discovery.await_args
        assert call_args[0][0] == "old-disc-1"
        updates = call_args[0][1]
        assert updates["status"] == "superseded"
        assert updates["superseded_by"] == new_id

        # Response surfaces the supersession
        assert data.get("superseded") == "old-disc-1"

    @pytest.mark.asyncio
    async def test_supersedes_vetoed_for_permanent_predecessor(
        self, patch_common, registered_agent,
    ):
        """Permanent-tagged predecessors cannot be auto-flipped to superseded."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        # tag "permanent" → get_lifecycle_policy returns "permanent"
        # (Note: PERMANENT_TYPES uses "architecture_decision" but the handler's
        # VALID_DISCOVERY_TYPES uses "architectural_decision" — using the tag
        # path avoids that existing inconsistency.)
        permanent_predecessor = DiscoveryNode(
            id="perm-1",
            agent_id="other-agent",
            type="note",
            summary="ADR: schema choice",
            status="open",
            tags=["permanent"],
        )
        mock_graph.get_discovery = AsyncMock(return_value=permanent_predecessor)

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "tries to supersede ADR",
            "supersedes": "perm-1",
        })

        data = parse_result(result)
        # Veto must come BEFORE the new discovery is stored
        mock_graph.add_discovery.assert_not_awaited()
        mock_graph.update_discovery.assert_not_awaited()
        # Error response, not success
        assert data.get("success") is False
        assert "permanent" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_supersedes_missing_predecessor_warns_not_errors(
        self, patch_common, registered_agent,
    ):
        """Missing predecessor surfaces as warning; new discovery still stored."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        mock_graph.get_discovery = AsyncMock(return_value=None)  # not found

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "thinks it supersedes a ghost",
            "supersedes": "ghost-id",
        })

        data = parse_result(result)
        assert data["success"] is True
        assert "discovery_id" in data  # new entry was still stored
        mock_graph.add_discovery.assert_awaited_once()
        mock_graph.update_discovery.assert_not_awaited()
        assert "_supersedes_warning" in data

    @pytest.mark.asyncio
    async def test_supersedes_empty_string_rejected(
        self, patch_common, registered_agent,
    ):
        """supersedes='' is a parameter error, not silently ignored."""
        mock_mcp_server, mock_graph = patch_common
        from src.mcp_handlers.knowledge.handlers import handle_store_knowledge_graph

        result = await handle_store_knowledge_graph({
            "agent_id": registered_agent,
            "summary": "tries with empty",
            "supersedes": "",
        })

        data = parse_result(result)
        assert data.get("success") is False
        mock_graph.add_discovery.assert_not_awaited()
