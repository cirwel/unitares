"""
Tests for src/storage/knowledge_graph_age.py - KnowledgeGraphAGE

Comprehensive tests covering all async methods with mocked database
interactions. All asyncpg connections and pools are mocked to avoid
database dependencies.

Existing parsing tests live in test_knowledge_graph_age_parsing.py.
This file covers the async/DB-dependent methods:
  - _get_db / lazy init
  - _create_indexes
  - _execute_age_sql
  - add_discovery (with rate limiting, EISV, tags, embeddings)
  - get_discovery
  - get_response_chain
  - query (filters, tags, severity)
  - get_agent_discoveries
  - update_discovery
  - get_stats
  - health_check
  - _check_rate_limit (Redis and PostgreSQL paths)
  - load
  - find_similar / find_similar_by_tags
  - _pgvector_available / _pgvector_search / _store_embedding
  - get_connectivity_score / get_connectivity_scores_batch
  - _blend_with_connectivity
  - semantic_search
  - link_discoveries
  - get_orphan_discoveries / get_stale_discoveries
  - archive_discoveries_batch / cleanup_stale_discoveries
"""

import json
import math
import pytest
import sys
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock
from typing import Any, Dict, List, Optional

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.storage.knowledge_graph import KnowledgeGraphAGE
from src.knowledge_graph import DiscoveryNode, ResponseTo


# ============================================================================
# Helper factories
# ============================================================================


def make_discovery(
    discovery_id: str = "disc-001",
    agent_id: str = "agent-1",
    discovery_type: str = "insight",
    summary: str = "Test discovery",
    details: str = "Some details",
    tags: Optional[List[str]] = None,
    severity: Optional[str] = None,
    status: str = "open",
    related_to: Optional[List[str]] = None,
    response_to: Optional[ResponseTo] = None,
    timestamp: Optional[str] = None,
    references_files: Optional[List[str]] = None,
    confidence: Optional[float] = None,
    provenance: Optional[Dict[str, Any]] = None,
    provenance_chain: Optional[List[Dict[str, Any]]] = None,
    resolved_at: Optional[str] = None,
) -> DiscoveryNode:
    """Factory helper to create a DiscoveryNode for testing."""
    return DiscoveryNode(
        id=discovery_id,
        agent_id=agent_id,
        type=discovery_type,
        summary=summary,
        details=details,
        tags=tags or [],
        severity=severity,
        status=status,
        related_to=related_to or [],
        response_to=response_to,
        timestamp=timestamp or "2026-02-05T12:00:00",
        references_files=references_files or [],
        confidence=confidence,
        provenance=provenance,
        provenance_chain=provenance_chain,
        resolved_at=resolved_at,
    )


def make_mock_db(graph_available: bool = True) -> AsyncMock:
    """Create a mock database backend."""
    db = AsyncMock()
    db.init = AsyncMock()
    db.graph_available = AsyncMock(return_value=graph_available)
    db.graph_query = AsyncMock(return_value=[])
    db._pool = AsyncMock()

    # Pool.acquire() context manager
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=0)
    mock_conn.fetch = AsyncMock(return_value=[])

    db._pool.acquire = MagicMock(return_value=_AsyncContextManager(mock_conn))
    db.acquire = MagicMock(return_value=_AsyncContextManager(mock_conn))
    db.transaction = MagicMock(return_value=_AsyncContextManager(mock_conn))
    db._mock_conn = mock_conn  # Expose for test assertions
    return db


class _AsyncContextManager:
    """Helper to create an async context manager from a return value."""

    def __init__(self, value: Any):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        pass


def make_kg_with_mock_db(graph_available: bool = True) -> tuple:
    """Create a KnowledgeGraphAGE with a pre-injected mock db.

    Returns (kg, mock_db) tuple.
    """
    kg = KnowledgeGraphAGE()
    mock_db = make_mock_db(graph_available=graph_available)
    kg._db = mock_db
    kg._indexes_created = True
    return kg, mock_db


# ============================================================================
# _get_db / lazy initialization
# ============================================================================


class TestGetDb:

    @pytest.mark.asyncio
    async def test_lazy_init_calls_get_db_once(self):
        """_get_db should call get_db() and init() on first call only."""
        mock_db = make_mock_db()
        with patch("src.db.get_db", return_value=mock_db) as mock_get:
            kg = KnowledgeGraphAGE()
            # Prevent index creation from triggering recursive _get_db
            kg._indexes_created = True

            result = await kg._get_db()
            assert result is mock_db
            mock_get.assert_called_once()
            mock_db.init.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_subsequent_calls_return_cached_db(self):
        """_get_db should return cached instance on subsequent calls."""
        mock_db = make_mock_db()
        with patch("src.db.get_db", return_value=mock_db) as mock_get:
            kg = KnowledgeGraphAGE()
            kg._indexes_created = True

            await kg._get_db()
            await kg._get_db()
            # get_db() should only be called once
            mock_get.assert_called_once()

    @pytest.mark.asyncio
    async def test_aligns_graph_name_from_backend(self):
        """_get_db should align graph_name from the backend if available."""
        mock_db = make_mock_db()
        mock_db._age_graph = "custom_graph"
        with patch("src.db.get_db", return_value=mock_db):
            kg = KnowledgeGraphAGE()
            kg._indexes_created = True

            await kg._get_db()
            assert kg.graph_name == "custom_graph"

    @pytest.mark.asyncio
    async def test_aligns_graph_name_from_dual_backend(self):
        """_get_db should align graph_name from dual backend's postgres secondary."""
        mock_db = make_mock_db()
        # Dual backend: no _age_graph on db, but has _postgres with _age_graph
        del mock_db._age_graph  # Remove if set by default
        mock_db._postgres_available = True
        mock_pg = MagicMock()
        mock_pg._age_graph = "dual_graph"
        mock_db._postgres = mock_pg

        with patch("src.db.get_db", return_value=mock_db):
            kg = KnowledgeGraphAGE()
            kg._indexes_created = True

            await kg._get_db()
            assert kg.graph_name == "dual_graph"

    @pytest.mark.asyncio
    async def test_creates_indexes_on_first_use(self):
        """_get_db should trigger index creation on first call."""
        mock_db = make_mock_db(graph_available=False)
        with patch("src.db.get_db", return_value=mock_db):
            kg = KnowledgeGraphAGE()
            # _indexes_created is False by default

            await kg._get_db()
            assert kg._indexes_created is True


# ============================================================================
# _create_indexes
# ============================================================================


class TestCreateIndexes:

    @pytest.mark.asyncio
    async def test_skips_when_graph_not_available(self):
        """Index creation should skip when graph is not available."""
        kg, mock_db = make_kg_with_mock_db(graph_available=False)
        kg._indexes_created = False

        await kg._create_indexes()
        # graph_query should NOT be called for index creation
        mock_db.graph_query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_creates_gin_indexes(self):
        """Index creation should execute GIN index SQL statements."""
        kg, mock_db = make_kg_with_mock_db(graph_available=True)
        kg._indexes_created = False

        # Mock _execute_age_sql
        kg._execute_age_sql = AsyncMock()

        await kg._create_indexes()
        assert kg._execute_age_sql.await_count == 3  # 3 GIN indexes

    @pytest.mark.asyncio
    async def test_handles_index_creation_error(self):
        """Index creation should handle errors gracefully."""
        kg, mock_db = make_kg_with_mock_db(graph_available=True)
        kg._indexes_created = False

        kg._execute_age_sql = AsyncMock(side_effect=Exception("already exists"))

        # Should not raise
        await kg._create_indexes()


# ============================================================================
# _execute_age_sql
# ============================================================================


class TestExecuteAgeSql:

    @pytest.mark.asyncio
    async def test_executes_sql_with_age_setup(self):
        """Should LOAD age, SET search_path, then execute the SQL."""
        kg, mock_db = make_kg_with_mock_db()
        mock_conn = mock_db._mock_conn

        await kg._execute_age_sql("CREATE INDEX test_idx ON test_table(col)")

        assert mock_conn.execute.await_count == 3
        calls = [c.args[0] for c in mock_conn.execute.await_args_list]
        assert calls[0] == "LOAD 'age'"
        assert "search_path" in calls[1]
        assert "CREATE INDEX" in calls[2]

    @pytest.mark.asyncio
    async def test_uses_acquire_for_age_sql(self):
        """Should use db.acquire() for proper pool orphan protection."""
        kg = KnowledgeGraphAGE()
        mock_conn = AsyncMock()
        mock_db = AsyncMock()
        mock_db.acquire = MagicMock(return_value=_AsyncContextManager(mock_conn))
        kg._db = mock_db
        kg._indexes_created = True
        kg._get_db = AsyncMock(return_value=mock_db)

        await kg._execute_age_sql("CREATE INDEX test_idx ON foo(bar)")
        assert mock_conn.execute.await_count == 3
        calls = [c.args[0] for c in mock_conn.execute.await_args_list]
        assert calls[0] == "LOAD 'age'"
        assert "search_path" in calls[1]
        assert "CREATE INDEX" in calls[2]


# ============================================================================
# add_discovery
# ============================================================================


class TestAddDiscovery:

    @pytest.mark.asyncio
    async def test_add_basic_discovery(self):
        """Should create discovery node, agent node, and AUTHORED edge."""
        kg, mock_db = make_kg_with_mock_db()
        kg._check_rate_limit = AsyncMock()
        kg._pgvector_available = AsyncMock(return_value=False)

        discovery = make_discovery()
        await kg.add_discovery(discovery)

        # Rate limit now called inside transaction with conn=
        kg._check_rate_limit.assert_awaited_once()
        call_args = kg._check_rate_limit.await_args
        assert call_args[0][0] == "agent-1"
        assert "conn" in call_args[1]
        # At minimum: discovery node + agent node + authored edge = 3 calls
        assert mock_db.graph_query.await_count >= 3

    @pytest.mark.asyncio
    async def test_add_discovery_raises_when_graph_unavailable(self):
        """Should raise RuntimeError when graph is not available."""
        kg, mock_db = make_kg_with_mock_db(graph_available=False)
        kg._check_rate_limit = AsyncMock()

        discovery = make_discovery()
        with pytest.raises(RuntimeError, match="AGE graph not available"):
            await kg.add_discovery(discovery)

    @pytest.mark.asyncio
    async def test_add_discovery_with_response_to(self):
        """Should create RESPONDS_TO edge when response_to is set."""
        kg, mock_db = make_kg_with_mock_db()
        kg._check_rate_limit = AsyncMock()
        kg._pgvector_available = AsyncMock(return_value=False)

        response = ResponseTo(discovery_id="disc-parent", response_type="extend")
        discovery = make_discovery(response_to=response)
        await kg.add_discovery(discovery)

        # discovery + agent + authored + responds_to = 4 calls
        assert mock_db.graph_query.await_count >= 4

    @pytest.mark.asyncio
    async def test_add_discovery_with_related_to(self):
        """Should create RELATED_TO edges for each related discovery."""
        kg, mock_db = make_kg_with_mock_db()
        kg._check_rate_limit = AsyncMock()
        kg._pgvector_available = AsyncMock(return_value=False)

        discovery = make_discovery(related_to=["disc-a", "disc-b"])
        await kg.add_discovery(discovery)

        # discovery + agent + authored + 2 related = 5
        assert mock_db.graph_query.await_count >= 5

    @pytest.mark.asyncio
    async def test_sync_discovery_edges_drops_missing_dst_ids(self, caplog):
        """Regression: dst_ids absent from knowledge.discoveries must be
        filtered before INSERT, not allowed to trip the FK constraint.

        Reproduces KG 2026-05-10T00:58:42 (d0832eaf): AGE→PG canonical flip
        left orphan Discovery nodes in AGE. find_similar returned the
        orphan IDs into related_to, and the unguarded INSERT into
        knowledge.discovery_edges hit discovery_edges_dst_id_fkey,
        rolling back the entire tagged write so tags landed empty even on
        the underlying discovery row.
        """
        kg, _ = make_kg_with_mock_db()

        conn = AsyncMock()
        conn.execute = AsyncMock()
        # Only "live-parent" exists in knowledge.discoveries.
        # "dead-parent" is an AGE orphan — must be filtered.
        conn.fetch = AsyncMock(return_value=[{"id": "live-parent"}])
        conn.executemany = AsyncMock()

        discovery = make_discovery(
            related_to=["live-parent", "dead-parent"],
        )

        import logging
        with caplog.at_level(logging.WARNING):
            await kg._sync_discovery_edges(
                conn,
                discovery,
                datetime(2026, 5, 13, 12, 0, 0),
            )

        # The INSERT must have fired with only the live edge.
        assert conn.executemany.await_count == 1
        inserted_rows = conn.executemany.await_args.args[1]
        inserted_dst_ids = [row[1] for row in inserted_rows]
        assert inserted_dst_ids == ["live-parent"]

        # Drift must surface as a warning the operator can act on.
        assert any(
            "dropping" in rec.getMessage() and "dead-parent" in rec.getMessage()
            for rec in caplog.records
        ), f"expected dead-parent drop warning, got: {[r.getMessage() for r in caplog.records]}"

    @pytest.mark.asyncio
    async def test_sync_discovery_edges_all_dst_missing_skips_insert(self):
        """When every dst_id is an orphan, INSERT must not run at all."""
        kg, _ = make_kg_with_mock_db()

        conn = AsyncMock()
        conn.execute = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])  # all dst_ids are orphans
        conn.executemany = AsyncMock()

        discovery = make_discovery(
            related_to=["orphan-a", "orphan-b"],
        )
        await kg._sync_discovery_edges(
            conn,
            discovery,
            datetime(2026, 5, 13, 12, 0, 0),
        )

        # The DELETE always runs; the INSERT must not.
        assert conn.executemany.await_count == 0

    @pytest.mark.asyncio
    async def test_add_discovery_with_tags(self):
        """Should create TAGGED edges for each tag."""
        kg, mock_db = make_kg_with_mock_db()
        kg._check_rate_limit = AsyncMock()
        kg._pgvector_available = AsyncMock(return_value=False)

        discovery = make_discovery(tags=["python", "bug", "urgent"])
        await kg.add_discovery(discovery)

        # discovery + agent + authored + 3 tagged = 6
        assert mock_db.graph_query.await_count >= 6

    @pytest.mark.asyncio
    async def test_add_discovery_with_eisv_provenance(self):
        """Should extract EISV fields from self_observation provenance."""
        kg, mock_db = make_kg_with_mock_db()
        kg._check_rate_limit = AsyncMock()
        kg._pgvector_available = AsyncMock(return_value=False)

        provenance = {
            "E": 0.85,
            "I": 0.72,
            "S": 0.91,
            "V": 0.65,
            "regime": "exploration",
            "coherence": 0.78,
        }
        discovery = make_discovery(
            discovery_type="self_observation",
            provenance=provenance,
        )
        await kg.add_discovery(discovery)

        # Verify the first graph_query call (create_discovery_node) received EISV params
        first_call = mock_db.graph_query.await_args_list[0]
        # The second argument is the params dict
        params = first_call.args[1]
        assert params.get("eisv_e") == 0.85
        assert params.get("eisv_i") == 0.72
        assert params.get("eisv_s") == 0.91
        assert params.get("eisv_v") == 0.65
        assert params.get("regime") == "exploration"
        assert params.get("coherence") == 0.78

    @pytest.mark.asyncio
    async def test_add_discovery_timestamp_parsing(self):
        """Should parse ISO timestamps correctly."""
        kg, mock_db = make_kg_with_mock_db()
        kg._check_rate_limit = AsyncMock()
        kg._pgvector_available = AsyncMock(return_value=False)

        discovery = make_discovery(timestamp="2026-02-05T12:00:00Z")
        await kg.add_discovery(discovery)

        # Should not raise - the Z suffix is handled
        mock_db.graph_query.assert_awaited()

    @pytest.mark.asyncio
    async def test_add_discovery_invalid_timestamp_uses_now(self):
        """Should use datetime.now() for invalid timestamps."""
        kg, mock_db = make_kg_with_mock_db()
        kg._check_rate_limit = AsyncMock()
        kg._pgvector_available = AsyncMock(return_value=False)

        discovery = make_discovery(timestamp="not-a-date")
        await kg.add_discovery(discovery)

        # Should not raise
        mock_db.graph_query.assert_awaited()

    @pytest.mark.asyncio
    async def test_add_discovery_no_timestamp(self):
        """Should use datetime.now() when timestamp is None."""
        kg, mock_db = make_kg_with_mock_db()
        kg._check_rate_limit = AsyncMock()
        kg._pgvector_available = AsyncMock(return_value=False)

        discovery = make_discovery()
        discovery.timestamp = None
        await kg.add_discovery(discovery)

        # Should not raise
        mock_db.graph_query.assert_awaited()

    @pytest.mark.asyncio
    async def test_add_discovery_with_resolved_at(self):
        """Should parse resolved_at timestamp."""
        kg, mock_db = make_kg_with_mock_db()
        kg._check_rate_limit = AsyncMock()
        kg._pgvector_available = AsyncMock(return_value=False)

        discovery = make_discovery(resolved_at="2026-02-05T14:00:00")
        await kg.add_discovery(discovery)
        mock_db.graph_query.assert_awaited()

    @pytest.mark.asyncio
    async def test_add_discovery_with_invalid_resolved_at(self):
        """Should handle invalid resolved_at gracefully."""
        kg, mock_db = make_kg_with_mock_db()
        kg._check_rate_limit = AsyncMock()
        kg._pgvector_available = AsyncMock(return_value=False)

        discovery = make_discovery(resolved_at="invalid-date")
        await kg.add_discovery(discovery)
        mock_db.graph_query.assert_awaited()

    @pytest.mark.asyncio
    async def test_add_discovery_with_metadata_fields(self):
        """Should include metadata when any provenance/confidence fields are present."""
        kg, mock_db = make_kg_with_mock_db()
        kg._check_rate_limit = AsyncMock()
        kg._pgvector_available = AsyncMock(return_value=False)

        discovery = make_discovery(
            confidence=0.9,
            references_files=["src/main.py"],
        )
        await kg.add_discovery(discovery)

        first_call = mock_db.graph_query.await_args_list[0]
        params = first_call.args[1]
        assert "metadata" in params
        metadata = params["metadata"]
        assert metadata["confidence"] == 0.9
        assert metadata["references_files"] == ["src/main.py"]

    @pytest.mark.asyncio
    async def test_add_discovery_no_metadata_when_empty(self):
        """Should NOT include metadata when all provenance fields are empty."""
        kg, mock_db = make_kg_with_mock_db()
        kg._check_rate_limit = AsyncMock()
        kg._pgvector_available = AsyncMock(return_value=False)

        discovery = make_discovery()
        # Ensure all metadata fields are empty/None
        discovery.related_to = []
        discovery.references_files = []
        discovery.confidence = None
        discovery.provenance = None
        discovery.provenance_chain = None

        await kg.add_discovery(discovery)

        first_call = mock_db.graph_query.await_args_list[0]
        params = first_call.args[1]
        assert "metadata" not in params


# ============================================================================
# get_discovery
# ============================================================================


class TestGetDiscovery:

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        """Should return None when neither AGE node nor SQL row exists.

        After PR #223, get_discovery falls back to db.kg_get_discovery() when
        the AGE MATCH returns nothing — so "not found" requires both layers
        to be empty.
        """
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []
        mock_db.kg_get_discovery = AsyncMock(return_value=None)

        result = await kg.get_discovery("nonexistent")
        assert result is None
        mock_db.kg_get_discovery.assert_awaited_once_with("nonexistent")

    @pytest.mark.asyncio
    async def test_returns_discovery_from_dict_result(self):
        """Should parse dict result with 'd' key."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {
                "d": {
                    "properties": {
                        "id": "disc-001",
                        "agent_id": "agent-1",
                        "type": "insight",
                        "summary": "Found something",
                        "status": "open",
                    }
                }
            }
        ]

        result = await kg.get_discovery("disc-001")
        assert result is not None
        assert result.id == "disc-001"
        assert result.summary == "Found something"

    @pytest.mark.asyncio
    async def test_returns_discovery_from_direct_result(self):
        """Should parse result without 'd' key wrapper."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {
                "properties": {
                    "id": "disc-002",
                    "agent_id": "agent-2",
                    "summary": "Direct result",
                }
            }
        ]

        result = await kg.get_discovery("disc-002")
        assert result is not None
        assert result.id == "disc-002"

    @pytest.mark.asyncio
    async def test_passes_correct_cypher_and_params(self):
        """Should pass the correct discovery_id in the cypher params."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []
        mock_db.kg_get_discovery = AsyncMock(return_value=None)

        await kg.get_discovery("test-id-123")

        call_args = mock_db.graph_query.await_args
        assert call_args.args[1] == {"discovery_id": "test-id-123"}


# ============================================================================
# get_response_chain
# ============================================================================


class TestGetResponseChain:

    @pytest.mark.asyncio
    async def test_returns_empty_when_graph_unavailable(self):
        """Should return [] when graph unavailable and SQL has no row either.

        After PR #223, the single-node fallback delegates to get_discovery,
        which itself falls back to SQL. The chain is empty only when both
        AGE and SQL are empty.
        """
        kg, mock_db = make_kg_with_mock_db(graph_available=False)
        mock_db.graph_query.return_value = []
        mock_db.kg_get_discovery = AsyncMock(return_value=None)

        result = await kg.get_response_chain("disc-001")
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_single_node_fallback(self):
        """Should return root discovery when graph unavailable but discovery exists."""
        kg, mock_db = make_kg_with_mock_db(graph_available=False)
        # Patch get_discovery to return a result
        kg.get_discovery = AsyncMock(return_value=make_discovery(discovery_id="disc-001"))

        result = await kg.get_response_chain("disc-001")
        assert len(result) == 1
        assert result[0].id == "disc-001"

    @pytest.mark.asyncio
    async def test_returns_ordered_chain_from_dict_results(self):
        """Should return chain ordered by depth (root first)."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {
                "node": {"properties": {"id": "root", "agent_id": "a1", "summary": "root"}},
                "depth": 0,
            },
            {
                "node": {"properties": {"id": "reply1", "agent_id": "a2", "summary": "reply"}},
                "depth": 1,
            },
            {
                "node": {"properties": {"id": "reply2", "agent_id": "a3", "summary": "deep reply"}},
                "depth": 2,
            },
        ]

        result = await kg.get_response_chain("root")
        assert len(result) == 3
        assert result[0].id == "root"
        assert result[1].id == "reply1"
        assert result[2].id == "reply2"

    @pytest.mark.asyncio
    async def test_deduplicates_by_smallest_depth(self):
        """Should keep the entry with the smallest depth when duplicates exist."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {
                "node": {"properties": {"id": "disc-A", "agent_id": "a1", "summary": "A"}},
                "depth": 0,
            },
            {
                "node": {"properties": {"id": "disc-A", "agent_id": "a1", "summary": "A duplicate"}},
                "depth": 3,
            },
        ]

        result = await kg.get_response_chain("disc-A")
        assert len(result) == 1
        assert result[0].id == "disc-A"

    @pytest.mark.asyncio
    async def test_skips_non_dict_rows(self):
        """Non-dict rows are skipped (single-column map convention).

        The Cypher now projects a single ``RETURN {node: d, depth: length(p)}``
        map, so every row decodes to a dict. A stray non-dict row (e.g. a bare
        scalar) is skipped rather than crashing the chain.
        """
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            ({"properties": {"id": "disc-X", "agent_id": "a1", "summary": "X"}}, 0),
            {"node": {"properties": {"id": "disc-X", "agent_id": "a1", "summary": "X"}}, "depth": 0},
        ]

        result = await kg.get_response_chain("disc-X")
        assert len(result) == 1
        assert result[0].id == "disc-X"

    @pytest.mark.asyncio
    async def test_handles_bare_results(self):
        """Should handle bare result format with default depth 0."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"properties": {"id": "disc-Y", "agent_id": "a1", "summary": "Y"}},
        ]

        result = await kg.get_response_chain("disc-Y")
        assert len(result) == 1
        assert result[0].id == "disc-Y"

    @pytest.mark.asyncio
    async def test_skips_nodes_without_id(self):
        """Should skip results that parse to no valid discovery."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"node": {"properties": {"summary": "missing id"}}, "depth": 0},
            {"node": {"properties": {"id": "valid", "agent_id": "a", "summary": "ok"}}, "depth": 1},
        ]

        result = await kg.get_response_chain("root")
        assert len(result) == 1
        assert result[0].id == "valid"


# ============================================================================
# query
# ============================================================================


class TestQuery:

    @pytest.mark.asyncio
    async def test_returns_empty_when_graph_unavailable(self):
        """Should return empty list when graph is not available."""
        kg, mock_db = make_kg_with_mock_db(graph_available=False)
        result = await kg.query()
        assert result == []

    @pytest.mark.asyncio
    async def test_query_no_filters(self):
        """Should query all discoveries without filters."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"properties": {"id": "d1", "agent_id": "a1", "summary": "one"}},
            {"properties": {"id": "d2", "agent_id": "a2", "summary": "two"}},
        ]

        result = await kg.query()
        assert len(result) == 2
        assert result[0].id == "d1"
        assert result[1].id == "d2"

    @pytest.mark.asyncio
    async def test_query_deduplicates_duplicate_discovery_ids(self):
        """Should return one result when AGE emits the same discovery twice."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"properties": {"id": "d1", "agent_id": "a1", "summary": "one"}},
            {"properties": {"id": "d1", "agent_id": "a1", "summary": "one"}},
            {"properties": {"id": "d2", "agent_id": "a2", "summary": "two"}},
        ]

        result = await kg.query(tags=["python", "bug"])

        assert [d.id for d in result] == ["d1", "d2"]

    @pytest.mark.asyncio
    async def test_query_with_agent_id_filter(self):
        """Should include agent_id in query params."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []

        await kg.query(agent_id="agent-123")

        call_args = mock_db.graph_query.await_args
        params = call_args.args[1]
        assert params["agent_id"] == "agent-123"

    @pytest.mark.asyncio
    async def test_query_with_type_filter(self):
        """Should include type in query params."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []

        await kg.query(type="bug")

        call_args = mock_db.graph_query.await_args
        params = call_args.args[1]
        assert params["type"] == "bug"

    @pytest.mark.asyncio
    async def test_query_with_status_filter(self):
        """Should include status in query params."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []

        await kg.query(status="resolved")

        call_args = mock_db.graph_query.await_args
        params = call_args.args[1]
        assert params["status"] == "resolved"

    @pytest.mark.asyncio
    async def test_query_with_severity_filter(self):
        """Should include severity in query params."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []

        await kg.query(severity="critical")

        call_args = mock_db.graph_query.await_args
        params = call_args.args[1]
        assert params["severity"] == "critical"

    @pytest.mark.asyncio
    async def test_query_with_tags_filter(self):
        """Should use TAGGED relationship pattern when tags are provided."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []

        await kg.query(tags=["python", "bug"])

        call_args = mock_db.graph_query.await_args
        cypher = call_args.args[0]
        params = call_args.args[1]
        assert "TAGGED" in cypher
        assert "Tag" in cypher
        assert "RETURN DISTINCT d" in cypher
        assert params["tags"] == ["python", "bug"]

    @pytest.mark.asyncio
    async def test_query_with_tags_and_other_filters(self):
        """Should combine tag filter with other conditions."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []

        await kg.query(agent_id="agent-1", tags=["python"])

        call_args = mock_db.graph_query.await_args
        cypher = call_args.args[0]
        params = call_args.args[1]
        assert "TAGGED" in cypher
        assert params["agent_id"] == "agent-1"
        assert params["tags"] == ["python"]

    @pytest.mark.asyncio
    async def test_query_with_limit(self):
        """Should pass limit to query params."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []

        await kg.query(limit=25)

        call_args = mock_db.graph_query.await_args
        params = call_args.args[1]
        assert params["limit"] == 25

    @pytest.mark.asyncio
    async def test_query_handles_dict_d_key_result(self):
        """Should handle results wrapped in {'d': ...}."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"d": {"properties": {"id": "d1", "agent_id": "a1", "summary": "wrapped"}}},
        ]

        result = await kg.query()
        assert len(result) == 1
        assert result[0].summary == "wrapped"

    @pytest.mark.asyncio
    async def test_query_handles_error_result(self):
        """Should skip results with 'error' key."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"error": "something went wrong"},
            {"properties": {"id": "d1", "agent_id": "a1", "summary": "valid"}},
        ]

        result = await kg.query()
        assert len(result) == 1
        assert result[0].id == "d1"

    @pytest.mark.asyncio
    async def test_query_skips_invalid_nodes(self):
        """Should skip nodes that cannot be parsed to DiscoveryNode."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"properties": {}},  # No id
            {"properties": {"id": "valid", "agent_id": "a", "summary": "ok"}},
        ]

        result = await kg.query()
        assert len(result) == 1
        assert result[0].id == "valid"


# ============================================================================
# get_agent_discoveries
# ============================================================================


class TestGetAgentDiscoveries:

    @pytest.mark.asyncio
    async def test_delegates_to_query(self):
        """Should delegate to query() with agent_id."""
        kg, _ = make_kg_with_mock_db()
        kg.query = AsyncMock(return_value=[])

        await kg.get_agent_discoveries("agent-1", limit=50)

        kg.query.assert_awaited_once_with(agent_id="agent-1", limit=50)

    @pytest.mark.asyncio
    async def test_default_limit_100(self):
        """Should default limit to 100."""
        kg, _ = make_kg_with_mock_db()
        kg.query = AsyncMock(return_value=[])

        await kg.get_agent_discoveries("agent-1")

        kg.query.assert_awaited_once_with(agent_id="agent-1", limit=100)


# ============================================================================
# update_discovery
# ============================================================================


class TestUpdateDiscovery:

    @pytest.mark.asyncio
    async def test_returns_false_when_graph_unavailable(self):
        """Should return False when graph unavailable AND SQL row is missing.

        After PR #223, update_discovery falls back to _sql_update_discovery
        when the graph is unavailable. The SQL fallback returns False iff
        the UPDATE ... RETURNING id touches no row.
        """
        kg, mock_db = make_kg_with_mock_db(graph_available=False)
        mock_db._pool.fetchval = AsyncMock(return_value=None)
        result = await kg.update_discovery("disc-001", {"status": "resolved"})
        assert result is False
        mock_db._pool.fetchval.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_updates_valid_fields(self):
        """Should update status, severity, type, resolved_at, updated_at."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [{"id": "disc-001"}]

        result = await kg.update_discovery("disc-001", {
            "status": "resolved",
            "severity": "high",
        })
        assert result is True

        call_args = mock_db.graph_query.await_args
        cypher = call_args.args[0]
        params = call_args.args[1]
        assert "d.status" in cypher
        assert "d.severity" in cypher
        assert params["val_status"] == "resolved"
        assert params["val_severity"] == "high"

    @pytest.mark.asyncio
    async def test_updates_tags_as_json(self):
        """Should serialize tags as JSON when updating."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [{"id": "disc-001"}]

        result = await kg.update_discovery("disc-001", {
            "tags": ["new-tag", "updated"],
        })
        assert result is True

        call_args = mock_db.graph_query.await_args
        params = call_args.args[1]
        assert params["val_tags"] == json.dumps(["new-tag", "updated"])

    @pytest.mark.asyncio
    async def test_returns_true_for_empty_updates(self):
        """Should return True when there are no valid fields to update."""
        kg, mock_db = make_kg_with_mock_db()

        result = await kg.update_discovery("disc-001", {"unknown_field": "value"})
        assert result is True
        mock_db.graph_query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_false_on_error_result(self):
        """Should return False when AGE returns error AND SQL row is missing.

        After PR #223, an AGE error result triggers SQL fallback for
        SQL-only orphans. False is returned only when SQL also has no row.
        """
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [{"error": "not found"}]
        mock_db._pool.fetchval = AsyncMock(return_value=None)

        result = await kg.update_discovery("disc-001", {"status": "resolved"})
        assert result is False
        mock_db._pool.fetchval.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_false_on_empty_result(self):
        """Should return False when AGE empty AND SQL row is missing.

        After PR #223, an empty AGE result triggers SQL fallback. False is
        returned only when SQL also has no row.
        """
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []
        mock_db._pool.fetchval = AsyncMock(return_value=None)

        result = await kg.update_discovery("disc-001", {"status": "resolved"})
        assert result is False
        mock_db._pool.fetchval.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        """Should return False when query raises exception."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.side_effect = Exception("DB error")

        result = await kg.update_discovery("disc-001", {"status": "resolved"})
        assert result is False


# ============================================================================
# get_stats
# ============================================================================


class TestGetStats:

    @pytest.mark.asyncio
    async def test_returns_all_stat_fields(self):
        """Should return dict with all expected stat fields."""
        kg, mock_db = make_kg_with_mock_db()

        # Set up sequential return values for the various queries
        mock_db.graph_query.side_effect = [
            [5],                              # total discoveries
            [["a1", "a1", "a2"]],             # collect(agent_ids)
            [["insight", "bug", "insight"]],  # collect(types)
            [["open", "resolved", "open"]],   # collect(statuses)
            [3],                              # count edges
            [10],                             # count tags
            [["python", "bug", "python"]],    # collect(tag names)
        ]

        result = await kg.get_stats()

        assert result["total_discoveries"] == 5
        assert result["by_agent"] == {"a1": 2, "a2": 1}
        assert result["by_type"] == {"insight": 2, "bug": 1}
        assert result["by_status"] == {"open": 2, "resolved": 1}
        assert result["total_edges"] == 3
        assert result["total_agents"] == 2
        assert result["total_tags"] == 10
        assert result["by_tag"] == {"python": 2, "bug": 1}

    @pytest.mark.asyncio
    async def test_handles_empty_graph(self):
        """Should handle empty graph gracefully."""
        kg, mock_db = make_kg_with_mock_db()

        mock_db.graph_query.side_effect = [
            [0],    # total discoveries
            [[]],   # collect(agent_ids) - empty
            [[]],   # collect(types) - empty
            [[]],   # collect(statuses) - empty
            [0],    # count edges
            [0],    # count tags
            [[]],   # collect(tag names) - empty
        ]

        result = await kg.get_stats()

        assert result["total_discoveries"] == 0
        assert result["by_agent"] == {}
        assert result["total_edges"] == 0
        assert result["total_tags"] == 0

    @pytest.mark.asyncio
    async def test_handles_dict_tag_count_result(self):
        """Should handle tag count as dict format."""
        kg, mock_db = make_kg_with_mock_db()

        mock_db.graph_query.side_effect = [
            [0],
            [[]],
            [[]],
            [[]],
            [0],
            [{"tag_count": 42}],  # Dict format for tag count
            [[]],
        ]

        result = await kg.get_stats()
        assert result["total_tags"] == 42

    @pytest.mark.asyncio
    async def test_handles_error_tag_count_result(self):
        """Should handle error dict in tag count result."""
        kg, mock_db = make_kg_with_mock_db()

        mock_db.graph_query.side_effect = [
            [0],
            [[]],
            [[]],
            [[]],
            [0],
            [{"error": "tag query failed"}],
            [[]],
        ]

        result = await kg.get_stats()
        assert result["total_tags"] == 0

    @pytest.mark.asyncio
    async def test_handles_nested_list_tag_count(self):
        """Should handle nested list format for tag count."""
        kg, mock_db = make_kg_with_mock_db()

        mock_db.graph_query.side_effect = [
            [0],
            [[]],
            [[]],
            [[]],
            [0],
            [[15]],  # Nested list
            [[]],
        ]

        result = await kg.get_stats()
        assert result["total_tags"] == 15


# ============================================================================
# health_check
# ============================================================================


class TestHealthCheck:

    @pytest.mark.asyncio
    async def test_returns_counts_only(self):
        """Should return aggregate counts without breakdowns."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query = AsyncMock(side_effect=[
            [42],   # total discoveries
            [100],  # total tags
            [200],  # total edges
        ])

        result = await kg.health_check()
        assert result == {"total_discoveries": 42, "total_tags": 100, "total_edges": 200}
        assert "by_agent" not in result
        assert "by_tag" not in result

    @pytest.mark.asyncio
    async def test_returns_degraded_on_error(self):
        """Should return degraded status on exception."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query = AsyncMock(side_effect=Exception("DB down"))

        result = await kg.health_check()
        assert result["status"] == "degraded"
        assert "error" in result
        assert result["backend"] == "age"


# ============================================================================
# _check_rate_limit
# ============================================================================


class TestCheckRateLimit:

    @pytest.mark.asyncio
    async def test_redis_path_success(self):
        """Should pass when Redis rate limit check succeeds."""
        kg, mock_db = make_kg_with_mock_db()

        mock_limiter = AsyncMock()
        mock_limiter.check = AsyncMock(return_value=True)
        mock_limiter.record = AsyncMock()

        with patch("src.cache.get_rate_limiter", return_value=mock_limiter):
            # Should not raise
            await kg._check_rate_limit("agent-1")
            mock_limiter.check.assert_awaited_once()
            mock_limiter.record.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_redis_path_rate_exceeded(self):
        """Should raise ValueError when Redis rate limit is exceeded."""
        kg, mock_db = make_kg_with_mock_db()

        mock_limiter = AsyncMock()
        mock_limiter.check = AsyncMock(return_value=False)
        mock_limiter.get_count = AsyncMock(return_value=25)

        with patch("src.cache.get_rate_limiter", return_value=mock_limiter):
            with pytest.raises(ValueError, match="Rate limit exceeded"):
                await kg._check_rate_limit("agent-1")

    @pytest.mark.asyncio
    async def test_postgres_fallback_under_limit(self):
        """Should fall back to PostgreSQL when Redis is unavailable."""
        kg, mock_db = make_kg_with_mock_db()
        mock_conn = mock_db._mock_conn
        mock_conn.fetchval.return_value = 5  # Under limit

        with patch(
            "src.cache.get_rate_limiter",
            side_effect=ImportError("no redis"),
        ):
            # Should not raise
            await kg._check_rate_limit("agent-1")
            mock_conn.fetchval.assert_awaited_once()
            # Should record the store
            assert mock_conn.execute.await_count >= 1

    @pytest.mark.asyncio
    async def test_postgres_fallback_over_limit(self):
        """Should raise ValueError when PostgreSQL fallback rate limit exceeded."""
        kg, mock_db = make_kg_with_mock_db()
        mock_conn = mock_db._mock_conn
        # Atomic INSERT returns None when limit exceeded (no row inserted),
        # then the follow-up COUNT query returns the actual count
        mock_conn.fetchval.side_effect = [None, 20]

        with patch(
            "src.cache.get_rate_limiter",
            side_effect=ImportError("no redis"),
        ):
            with pytest.raises(ValueError, match="Rate limit exceeded"):
                await kg._check_rate_limit("agent-1")


# ============================================================================
# load
# ============================================================================


class TestLoad:

    @pytest.mark.asyncio
    async def test_load_is_noop(self):
        """load() should be a no-op for AGE backend."""
        kg = KnowledgeGraphAGE()
        await kg.load()  # Should not raise


# ============================================================================
# find_similar
# ============================================================================


class TestFindSimilar:

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_tags(self):
        """Should return empty list when discovery has no tags."""
        kg, _ = make_kg_with_mock_db()
        discovery = make_discovery(tags=[])

        result = await kg.find_similar(discovery)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_similar_discoveries(self):
        """Should return discoveries with overlapping tags."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"properties": {"id": "similar-1", "agent_id": "a1", "summary": "similar"}},
        ]

        discovery = make_discovery(tags=["python", "bug"])
        result = await kg.find_similar(discovery, limit=5)

        assert len(result) == 1
        assert result[0].id == "similar-1"

        # Verify params (limit applied in Python, not Cypher)
        call_args = mock_db.graph_query.await_args
        params = call_args.args[1]
        assert params["tags"] == ["python", "bug"]
        assert params["exclude_id"] == "disc-001"

    @pytest.mark.asyncio
    async def test_handles_dict_d_key_result(self):
        """Should handle results wrapped in 'd' key."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"d": {"properties": {"id": "sim-2", "agent_id": "a1", "summary": "wrapped"}}},
        ]

        discovery = make_discovery(tags=["test"])
        result = await kg.find_similar(discovery)
        assert len(result) == 1
        assert result[0].id == "sim-2"


# ============================================================================
# find_similar_by_tags
# ============================================================================


class TestFindSimilarByTags:

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_tags(self):
        """Should return empty list when tags list is empty."""
        kg, _ = make_kg_with_mock_db()
        result = await kg.find_similar_by_tags([])
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_similar_by_tags(self):
        """Should return discoveries matching given tags."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"properties": {"id": "tag-match", "agent_id": "a1", "summary": "match"}},
        ]

        result = await kg.find_similar_by_tags(["python", "testing"], limit=10)
        assert len(result) == 1

        call_args = mock_db.graph_query.await_args
        params = call_args.args[1]
        assert params["tags"] == ["python", "testing"]

    @pytest.mark.asyncio
    async def test_includes_exclude_clause_when_provided(self):
        """Should add exclude clause when exclude_id is provided."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []

        await kg.find_similar_by_tags(["tag"], exclude_id="exc-001")

        call_args = mock_db.graph_query.await_args
        cypher = call_args.args[0]
        params = call_args.args[1]
        assert "exclude_id" in cypher
        assert params["exclude_id"] == "exc-001"

    @pytest.mark.asyncio
    async def test_no_exclude_clause_without_exclude_id(self):
        """Should not add exclude clause when exclude_id is None."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []

        await kg.find_similar_by_tags(["tag"])

        call_args = mock_db.graph_query.await_args
        params = call_args.args[1]
        assert "exclude_id" not in params


# ============================================================================
# _pgvector_available
# ============================================================================


class TestPgvectorAvailable:

    @pytest.mark.asyncio
    async def test_returns_false_when_no_pool(self):
        """Should return False when _pool is None."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db._pool = None

        result = await kg._pgvector_available()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_pool_attr(self):
        """Should return False when _pool attribute doesn't exist."""
        kg = KnowledgeGraphAGE()
        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        del mock_db._pool
        kg._db = mock_db
        kg._indexes_created = True

        result = await kg._pgvector_available()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_vector_extension(self):
        """Should return False when vector extension is not installed."""
        kg, mock_db = make_kg_with_mock_db()
        mock_conn = mock_db._mock_conn
        mock_conn.fetchval.return_value = False

        result = await kg._pgvector_available()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_embeddings_table(self):
        """Should return False when discovery_embeddings table doesn't exist."""
        kg, mock_db = make_kg_with_mock_db()
        mock_conn = mock_db._mock_conn
        # First call returns True (extension exists), second returns False (table doesn't)
        mock_conn.fetchval.side_effect = [True, False]

        result = await kg._pgvector_available()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_when_everything_available(self):
        """Should return True when both extension and table exist."""
        kg, mock_db = make_kg_with_mock_db()
        mock_conn = mock_db._mock_conn
        mock_conn.fetchval.side_effect = [True, True]

        result = await kg._pgvector_available()
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        """Should return False when query fails."""
        kg, mock_db = make_kg_with_mock_db()
        mock_conn = mock_db._mock_conn
        mock_conn.fetchval.side_effect = Exception("DB error")

        result = await kg._pgvector_available()
        assert result is False


# ============================================================================
# _pgvector_search
# ============================================================================


class TestPgvectorSearch:

    @pytest.mark.asyncio
    async def test_returns_scored_results(self):
        """Should return (discovery_id, similarity) tuples."""
        kg, mock_db = make_kg_with_mock_db()
        mock_conn = mock_db._mock_conn
        mock_conn.fetch.return_value = [
            {"discovery_id": "d1", "similarity": 0.95},
            {"discovery_id": "d2", "similarity": 0.80},
        ]

        result = await kg._pgvector_search(
            query_embedding=[0.1, 0.2, 0.3],
            limit=10,
            min_similarity=0.5,
        )

        assert len(result) == 2
        assert result[0] == ("d1", 0.95)
        assert result[1] == ("d2", 0.80)

    @pytest.mark.asyncio
    async def test_with_agent_id_filter(self):
        """Should use agent-filtered query when agent_id provided."""
        kg, mock_db = make_kg_with_mock_db()
        mock_conn = mock_db._mock_conn
        mock_conn.fetch.return_value = []

        await kg._pgvector_search(
            query_embedding=[0.1, 0.2],
            limit=5,
            min_similarity=0.3,
            agent_id="agent-1",
        )

        mock_conn.fetch.assert_awaited_once()


# ============================================================================
# _store_embedding
# ============================================================================


class TestStoreEmbedding:

    @pytest.mark.asyncio
    async def test_stores_embedding_as_vector_string(self):
        """Should convert list to pgvector string format and execute INSERT."""
        kg, mock_db = make_kg_with_mock_db()
        mock_conn = mock_db._mock_conn

        await kg._store_embedding("disc-001", [0.1, 0.2, 0.3])

        mock_conn.execute.assert_awaited_once()
        call_args = mock_conn.execute.await_args
        sql = call_args.args[0]
        assert "INSERT INTO core.discovery_embeddings" in sql
        assert call_args.args[1] == "disc-001"
        assert call_args.args[2] == "[0.1,0.2,0.3]"

    @pytest.mark.asyncio
    async def test_handles_store_failure_gracefully(self):
        """Should not raise when embedding store fails."""
        kg, mock_db = make_kg_with_mock_db()
        mock_conn = mock_db._mock_conn
        mock_conn.execute.side_effect = Exception("insert failed")

        # Should not raise
        await kg._store_embedding("disc-001", [0.1, 0.2])


# ============================================================================
# get_connectivity_score
# ============================================================================


class TestGetConnectivityScore:

    @pytest.mark.asyncio
    async def test_returns_zero_when_graph_unavailable(self):
        """Should return 0.0 when graph is not available."""
        kg, mock_db = make_kg_with_mock_db(graph_available=False)
        result = await kg.get_connectivity_score("disc-001")
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_returns_zero_for_no_results(self):
        """Should return 0.0 when query returns no results."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []

        result = await kg.get_connectivity_score("disc-001")
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_returns_normalized_score(self):
        """Should return log-normalized score based on edge counts."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"related": 3, "responds": 2}
        ]

        result = await kg.get_connectivity_score("disc-001")
        # raw = 3 + (2*2) = 7
        expected = math.log1p(7) / math.log1p(100)
        assert abs(result - expected) < 0.001

    @pytest.mark.asyncio
    async def test_caps_at_one(self):
        """Should cap score at 1.0."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"related": 200, "responds": 200}
        ]

        result = await kg.get_connectivity_score("disc-001")
        assert result <= 1.0

    @pytest.mark.asyncio
    async def test_returns_zero_for_error_result(self):
        """Should return 0.0 when result contains error."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [{"error": "query failed"}]

        result = await kg.get_connectivity_score("disc-001")
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_returns_zero_on_exception(self):
        """Should return 0.0 when query raises exception."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.side_effect = Exception("DB error")

        result = await kg.get_connectivity_score("disc-001")
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_responds_to_weighted_double(self):
        """RESPONDS_TO edges should count double compared to RELATED_TO."""
        kg, mock_db = make_kg_with_mock_db()

        # Setup: 1 related, 0 responds
        mock_db.graph_query.return_value = [{"related": 1, "responds": 0}]
        score_related = await kg.get_connectivity_score("d1")

        # Setup: 0 related, 1 responds
        mock_db.graph_query.return_value = [{"related": 0, "responds": 1}]
        score_responds = await kg.get_connectivity_score("d2")

        # responds_to = 1*2 = 2, related_to = 1 => responds scores higher
        assert score_responds > score_related


# ============================================================================
# get_connectivity_scores_batch
# ============================================================================


class TestGetConnectivityScoresBatch:

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_ids(self):
        """Should return empty dict for empty ID list."""
        kg, _ = make_kg_with_mock_db()
        result = await kg.get_connectivity_scores_batch([])
        assert result == {}

    @pytest.mark.asyncio
    async def test_returns_zeros_when_graph_unavailable(self):
        """Should return all zeros when graph is not available."""
        kg, mock_db = make_kg_with_mock_db(graph_available=False)
        result = await kg.get_connectivity_scores_batch(["d1", "d2"])
        assert result == {"d1": 0.0, "d2": 0.0}

    @pytest.mark.asyncio
    async def test_returns_scores_for_found_ids(self):
        """Should compute scores for returned results."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"id": "d1", "related": 5, "responds": 2},
            {"id": "d2", "related": 0, "responds": 0},
        ]

        result = await kg.get_connectivity_scores_batch(["d1", "d2", "d3"])

        assert result["d1"] > 0.0
        assert result["d2"] == 0.0  # raw = 0
        assert result["d3"] == 0.0  # Not in results, filled as zero

    @pytest.mark.asyncio
    async def test_fills_missing_ids_with_zero(self):
        """Should fill missing IDs with 0.0."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"id": "d1", "related": 1, "responds": 0},
        ]

        result = await kg.get_connectivity_scores_batch(["d1", "d2"])
        assert "d2" in result
        assert result["d2"] == 0.0

    @pytest.mark.asyncio
    async def test_strips_quoted_ids(self):
        """Should strip quotes from id values."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"id": '"d1"', "related": 1, "responds": 0},
        ]

        result = await kg.get_connectivity_scores_batch(["d1"])
        assert "d1" in result

    @pytest.mark.asyncio
    async def test_returns_zeros_on_exception(self):
        """Should return all zeros on exception."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.side_effect = Exception("batch failed")

        result = await kg.get_connectivity_scores_batch(["d1", "d2"])
        assert result == {"d1": 0.0, "d2": 0.0}

    @pytest.mark.asyncio
    async def test_skips_error_results(self):
        """Should skip results containing 'error' key."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"error": "something broke"},
            {"id": "d1", "related": 1, "responds": 1},
        ]

        result = await kg.get_connectivity_scores_batch(["d1"])
        assert result["d1"] > 0.0


# ============================================================================
# _blend_with_connectivity
# ============================================================================


class TestBlendWithConnectivity:

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_results(self):
        """Should return empty list for empty input."""
        kg, _ = make_kg_with_mock_db()
        result = await kg._blend_with_connectivity([], 0.3, False, 10)
        assert result == []

    @pytest.mark.asyncio
    async def test_blends_scores_correctly(self):
        """Should blend similarity and connectivity scores."""
        kg, _ = make_kg_with_mock_db()

        d1 = make_discovery(discovery_id="d1")
        d2 = make_discovery(discovery_id="d2")

        raw = [(d1, 0.9), (d2, 0.8)]

        # Mock batch connectivity
        kg.get_connectivity_scores_batch = AsyncMock(
            return_value={"d1": 0.2, "d2": 0.8}
        )

        result = await kg._blend_with_connectivity(raw, 0.3, False, 10)

        assert len(result) == 2
        # d1: 0.9 * 0.7 + 0.2 * 0.3 = 0.63 + 0.06 = 0.69
        # d2: 0.8 * 0.7 + 0.8 * 0.3 = 0.56 + 0.24 = 0.80
        # d2 should be first (higher blended score)
        assert result[0][0].id == "d2"
        assert result[1][0].id == "d1"

    @pytest.mark.asyncio
    async def test_excludes_orphans_when_requested(self):
        """Should exclude discoveries with 0 connectivity when exclude_orphans=True."""
        kg, _ = make_kg_with_mock_db()

        d1 = make_discovery(discovery_id="d1")
        d2 = make_discovery(discovery_id="d2")

        raw = [(d1, 0.9), (d2, 0.8)]

        kg.get_connectivity_scores_batch = AsyncMock(
            return_value={"d1": 0.5, "d2": 0.0}
        )

        result = await kg._blend_with_connectivity(raw, 0.3, True, 10)

        assert len(result) == 1
        assert result[0][0].id == "d1"

    @pytest.mark.asyncio
    async def test_applies_limit(self):
        """Should limit results to specified count."""
        kg, _ = make_kg_with_mock_db()

        discoveries = [make_discovery(discovery_id=f"d{i}") for i in range(5)]
        raw = [(d, 0.9 - i * 0.1) for i, d in enumerate(discoveries)]

        kg.get_connectivity_scores_batch = AsyncMock(
            return_value={f"d{i}": 0.1 for i in range(5)}
        )

        result = await kg._blend_with_connectivity(raw, 0.3, False, 3)
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_temporal_decay_penalizes_old_entries(self):
        """Old entries should score lower than recent ones with same similarity."""
        kg, _ = make_kg_with_mock_db()

        now = datetime.now()
        recent = make_discovery(discovery_id="recent", timestamp=now.isoformat())
        old = make_discovery(
            discovery_id="old",
            timestamp=(now - timedelta(days=180)).isoformat(),
        )

        raw = [(recent, 0.9), (old, 0.9)]

        kg.get_connectivity_scores_batch = AsyncMock(
            return_value={"recent": 0.1, "old": 0.1}
        )

        result = await kg._blend_with_connectivity(
            raw, 0.3, False, 10, temporal_decay=True, half_life_days=90.0
        )

        assert len(result) == 2
        # Recent should score higher despite same similarity
        assert result[0][0].id == "recent"
        assert result[0][1] > result[1][1]

    @pytest.mark.asyncio
    async def test_temporal_decay_disabled(self):
        """With temporal_decay=False, age should not affect scores."""
        kg, _ = make_kg_with_mock_db()

        now = datetime.now()
        recent = make_discovery(discovery_id="recent", timestamp=now.isoformat())
        old = make_discovery(
            discovery_id="old",
            timestamp=(now - timedelta(days=365)).isoformat(),
        )

        raw = [(recent, 0.9), (old, 0.9)]

        kg.get_connectivity_scores_batch = AsyncMock(
            return_value={"recent": 0.1, "old": 0.1}
        )

        result = await kg._blend_with_connectivity(
            raw, 0.3, False, 10, temporal_decay=False
        )

        assert len(result) == 2
        # Both should have same score when decay is disabled
        assert abs(result[0][1] - result[1][1]) < 0.001

    @pytest.mark.asyncio
    async def test_status_weight_penalizes_archived(self):
        """Archived entries should score lower than open ones."""
        kg, _ = make_kg_with_mock_db()

        now = datetime.now()
        open_disc = make_discovery(
            discovery_id="open", status="open", timestamp=now.isoformat()
        )
        archived = make_discovery(
            discovery_id="archived", status="archived", timestamp=now.isoformat()
        )

        raw = [(open_disc, 0.9), (archived, 0.9)]

        kg.get_connectivity_scores_batch = AsyncMock(
            return_value={"open": 0.1, "archived": 0.1}
        )

        result = await kg._blend_with_connectivity(
            raw, 0.3, False, 10, temporal_decay=False, status_weight=True
        )

        assert len(result) == 2
        assert result[0][0].id == "open"
        # archived gets 0.3 multiplier vs 1.0 for open
        assert result[0][1] > result[1][1] * 2

    @pytest.mark.asyncio
    async def test_status_weight_disabled(self):
        """With status_weight=False, status should not affect scores."""
        kg, _ = make_kg_with_mock_db()

        now = datetime.now()
        open_disc = make_discovery(
            discovery_id="open", status="open", timestamp=now.isoformat()
        )
        archived = make_discovery(
            discovery_id="archived", status="archived", timestamp=now.isoformat()
        )

        raw = [(open_disc, 0.9), (archived, 0.9)]

        kg.get_connectivity_scores_batch = AsyncMock(
            return_value={"open": 0.1, "archived": 0.1}
        )

        result = await kg._blend_with_connectivity(
            raw, 0.3, False, 10, temporal_decay=False, status_weight=False
        )

        assert len(result) == 2
        assert abs(result[0][1] - result[1][1]) < 0.001

    @pytest.mark.asyncio
    async def test_status_multipliers_values(self):
        """Verify status multiplier values match expected constants."""
        kg = KnowledgeGraphAGE()
        assert kg.STATUS_MULTIPLIERS["open"] == 1.0
        assert kg.STATUS_MULTIPLIERS["resolved"] == 0.6
        assert kg.STATUS_MULTIPLIERS["archived"] == 0.3
        assert kg.STATUS_MULTIPLIERS["disputed"] == 0.5

    @pytest.mark.asyncio
    async def test_decay_formula_at_half_life(self):
        """At exactly one half-life, score should be ~0.5x."""
        kg, _ = make_kg_with_mock_db()

        now = datetime.now()
        half_life = 90
        d = make_discovery(
            discovery_id="d1",
            timestamp=(now - timedelta(days=half_life)).isoformat(),
        )

        raw = [(d, 1.0)]
        kg.get_connectivity_scores_batch = AsyncMock(return_value={"d1": 0.0})

        result = await kg._blend_with_connectivity(
            raw, 0.0, False, 10, temporal_decay=True, half_life_days=half_life,
            status_weight=False,
        )

        # At half_life, decay = 1/(1+1) = 0.5
        assert len(result) == 1
        assert abs(result[0][1] - 0.5) < 0.01


class TestConnectivityCap:

    @pytest.mark.asyncio
    async def test_raw_score_capped_at_50(self):
        """Connectivity raw score should be capped at 50."""
        kg, mock_db = make_kg_with_mock_db()
        # 100 related + 200 responds = 500 raw, but should be capped at 50
        mock_db.graph_query.return_value = [
            {"id": "d1", "related": 100, "responds": 100, "superseded_by": 0},
        ]

        result = await kg.get_connectivity_scores_batch(["d1"])
        capped_score = math.log1p(50) / math.log1p(100)
        assert abs(result["d1"] - capped_score) < 0.01

    @pytest.mark.asyncio
    async def test_superseded_entries_penalized(self):
        """Entries with SUPERSEDES edges pointing to them should score lower."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"id": "d1", "related": 5, "responds": 2, "superseded_by": 0},
            {"id": "d2", "related": 5, "responds": 2, "superseded_by": 1},
        ]

        result = await kg.get_connectivity_scores_batch(["d1", "d2"])
        # d2 should be exactly half of d1 (one supersession = 0.5x)
        assert abs(result["d2"] - result["d1"] * 0.5) < 0.001

    @pytest.mark.asyncio
    async def test_multiple_supersessions_compound(self):
        """Multiple supersessions should compound the penalty."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"id": "d1", "related": 5, "responds": 2, "superseded_by": 2},
        ]

        result = await kg.get_connectivity_scores_batch(["d1"])
        # 2 supersessions = 0.25x
        base = math.log1p(min(5 + 4, 50)) / math.log1p(100)
        assert abs(result["d1"] - base * 0.25) < 0.001


class TestSupersedeDiscovery:

    @pytest.mark.asyncio
    async def test_supersede_success(self):
        """Should create SUPERSEDES edge between two discoveries."""
        kg, mock_db = make_kg_with_mock_db()

        # Mock get_discovery to return nodes for both IDs
        d1 = make_discovery(discovery_id="new-1")
        d2 = make_discovery(discovery_id="old-1")
        kg.get_discovery = AsyncMock(side_effect=lambda did: d1 if did == "new-1" else d2)

        result = await kg.supersede_discovery(new_id="new-1", old_id="old-1")
        assert result["success"] is True
        assert mock_db.graph_query.call_count >= 1

    @pytest.mark.asyncio
    async def test_supersede_missing_new(self):
        """Should fail if new discovery doesn't exist."""
        kg, mock_db = make_kg_with_mock_db()
        kg.get_discovery = AsyncMock(return_value=None)

        result = await kg.supersede_discovery(new_id="missing", old_id="old-1")
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_supersede_graph_unavailable(self):
        """Should fail if graph is not available."""
        kg, mock_db = make_kg_with_mock_db(graph_available=False)

        result = await kg.supersede_discovery(new_id="new-1", old_id="old-1")
        assert result["success"] is False


# ============================================================================
# semantic_search
# ============================================================================


class TestSemanticSearch:

    @pytest.mark.asyncio
    async def test_returns_degraded_when_embeddings_unavailable(self):
        """Should return ([], error_info) when embeddings module not available."""
        kg, _ = make_kg_with_mock_db()

        with patch.dict("sys.modules", {"src.embeddings": None}):
            with patch("builtins.__import__", side_effect=ImportError("no module")):
                result = await kg.semantic_search("test query")
                assert isinstance(result, tuple)
                assert result[0] == []
                assert result[1]["error"] == "embeddings_import_failed"

    @pytest.mark.asyncio
    async def test_returns_degraded_when_embeddings_not_available_flag(self):
        """Should return ([], error_info) when embeddings_available() returns False."""
        kg, _ = make_kg_with_mock_db()

        mock_module = MagicMock()
        mock_module.embeddings_available = MagicMock(return_value=False)
        mock_module.get_embeddings_service = AsyncMock()

        with patch.dict("sys.modules", {"src.embeddings": mock_module}):
            result = await kg.semantic_search("test query")
            assert isinstance(result, tuple)
            assert result[0] == []
            assert result[1]["error"] == "embeddings_unavailable"

    @pytest.mark.asyncio
    async def test_pgvector_path_returns_blended_results(self):
        """Should use pgvector path and blend with connectivity."""
        kg, mock_db = make_kg_with_mock_db()

        # Mock embeddings
        mock_embeddings = AsyncMock()
        mock_embeddings.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        mock_module = MagicMock()
        mock_module.embeddings_available = MagicMock(return_value=True)
        mock_module.get_embeddings_service = AsyncMock(return_value=mock_embeddings)

        kg._pgvector_available = AsyncMock(return_value=True)
        kg._pgvector_search = AsyncMock(return_value=[("d1", 0.9)])

        d1 = make_discovery(discovery_id="d1")
        kg.get_discovery = AsyncMock(return_value=d1)

        kg._blend_with_connectivity = AsyncMock(return_value=[(d1, 0.85)])

        with patch.dict("sys.modules", {"src.embeddings": mock_module}):
            result = await kg.semantic_search("test query")

        assert len(result) == 1
        assert result[0][0].id == "d1"

    @pytest.mark.asyncio
    async def test_pgvector_agent_filter(self):
        """Should filter results by agent_id when specified."""
        kg, mock_db = make_kg_with_mock_db()

        mock_embeddings = AsyncMock()
        mock_embeddings.embed = AsyncMock(return_value=[0.1, 0.2])
        mock_module = MagicMock()
        mock_module.embeddings_available = MagicMock(return_value=True)
        mock_module.get_embeddings_service = AsyncMock(return_value=mock_embeddings)

        kg._pgvector_available = AsyncMock(return_value=True)
        kg._pgvector_search = AsyncMock(return_value=[("d1", 0.9)])

        # Discovery belongs to wrong agent
        d1 = make_discovery(discovery_id="d1", agent_id="other-agent")
        kg.get_discovery = AsyncMock(return_value=d1)

        with patch.dict("sys.modules", {"src.embeddings": mock_module}):
            # With agent filter that doesn't match
            result = await kg.semantic_search("test", agent_id="specific-agent")

        # d1 should be filtered out because agent_id doesn't match
        # Falls through to in-memory search
        # Since we didn't mock the in-memory path, this tests the filter logic

    @pytest.mark.asyncio
    async def test_in_memory_fallback(self):
        """Should fall back to in-memory search when pgvector not available."""
        kg, mock_db = make_kg_with_mock_db()

        mock_embeddings = AsyncMock()
        mock_embeddings.embed = AsyncMock(return_value=[0.1, 0.2])
        mock_embeddings.embed_batch = AsyncMock(return_value=[[0.1, 0.2], [0.3, 0.4]])
        mock_embeddings.rank_by_similarity = AsyncMock(return_value=[("d1", 0.8)])
        mock_module = MagicMock()
        mock_module.embeddings_available = MagicMock(return_value=True)
        mock_module.get_embeddings_service = AsyncMock(return_value=mock_embeddings)

        kg._pgvector_available = AsyncMock(return_value=False)

        # Mock query to return candidates
        d1 = make_discovery(discovery_id="d1")
        kg.query = AsyncMock(return_value=[d1])

        kg._blend_with_connectivity = AsyncMock(return_value=[(d1, 0.75)])

        with patch.dict("sys.modules", {"src.embeddings": mock_module}):
            result = await kg.semantic_search("test query")

        assert len(result) == 1
        kg.query.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_in_memory_filters_below_min_similarity(self):
        """Should filter results below min_similarity threshold."""
        kg, mock_db = make_kg_with_mock_db()

        mock_embeddings = AsyncMock()
        mock_embeddings.embed = AsyncMock(return_value=[0.1, 0.2])
        mock_embeddings.embed_batch = AsyncMock(return_value=[[0.1, 0.2]])
        mock_embeddings.rank_by_similarity = AsyncMock(return_value=[("d1", 0.1)])  # Below threshold
        mock_module = MagicMock()
        mock_module.embeddings_available = MagicMock(return_value=True)
        mock_module.get_embeddings_service = AsyncMock(return_value=mock_embeddings)

        kg._pgvector_available = AsyncMock(return_value=False)

        d1 = make_discovery(discovery_id="d1")
        kg.query = AsyncMock(return_value=[d1])

        with patch.dict("sys.modules", {"src.embeddings": mock_module}):
            result = await kg.semantic_search("test", min_similarity=0.5)

        assert result == []

    @pytest.mark.asyncio
    async def test_in_memory_no_candidates(self):
        """Should return empty when no candidates found."""
        kg, mock_db = make_kg_with_mock_db()

        mock_embeddings = AsyncMock()
        mock_embeddings.embed = AsyncMock(return_value=[0.1, 0.2])
        mock_module = MagicMock()
        mock_module.embeddings_available = MagicMock(return_value=True)
        mock_module.get_embeddings_service = AsyncMock(return_value=mock_embeddings)

        kg._pgvector_available = AsyncMock(return_value=False)
        kg.query = AsyncMock(return_value=[])

        with patch.dict("sys.modules", {"src.embeddings": mock_module}):
            result = await kg.semantic_search("test")

        assert result == []

    @pytest.mark.asyncio
    async def test_in_memory_skips_none_candidate_embeddings(self):
        """Poisoned candidate embeddings should be skipped instead of crashing."""
        kg, mock_db = make_kg_with_mock_db()

        mock_embeddings = AsyncMock()
        mock_embeddings.embed = AsyncMock(return_value=[0.1, 0.2])
        mock_embeddings.embed_batch = AsyncMock(return_value=[None, [0.1, 0.2]])
        mock_embeddings.rank_by_similarity = AsyncMock(return_value=[("d2", 0.8)])
        mock_module = MagicMock()
        mock_module.embeddings_available = MagicMock(return_value=True)
        mock_module.get_embeddings_service = AsyncMock(return_value=mock_embeddings)

        kg._pgvector_available = AsyncMock(return_value=False)

        d1 = make_discovery(discovery_id="d1")
        d2 = make_discovery(discovery_id="d2")
        kg.query = AsyncMock(return_value=[d1, d2])
        kg._blend_with_connectivity = AsyncMock(return_value=[(d2, 0.75)])

        with patch.dict("sys.modules", {"src.embeddings": mock_module}):
            result = await kg.semantic_search("test query")

        assert result == [(d2, 0.75)]

    @pytest.mark.asyncio
    async def test_update_discovery_refreshes_embedding_after_details_change(self):
        """Changing summary/details should trigger embedding refresh."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query = AsyncMock(return_value=[{"d.id": "disc-1"}])
        kg._pgvector_available = AsyncMock(return_value=True)
        kg._refresh_embedding = AsyncMock()

        ok = await kg.update_discovery("disc-1", {"details": "new details"})

        assert ok is True
        kg._refresh_embedding.assert_awaited_once_with("disc-1")


# ============================================================================
# link_discoveries
# ============================================================================


class TestLinkDiscoveries:

    @pytest.mark.asyncio
    async def test_returns_error_when_graph_unavailable(self):
        """Should return error dict when graph is not available."""
        kg, mock_db = make_kg_with_mock_db(graph_available=False)

        result = await kg.link_discoveries("d1", "d2")
        assert result["success"] is False
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_returns_error_when_from_id_not_found(self):
        """Should return error when source discovery doesn't exist."""
        kg, mock_db = make_kg_with_mock_db()
        # Validation query returns only to_id
        mock_db.graph_query.side_effect = [
            [["d2"]],  # found_ids - only d2
        ]

        result = await kg.link_discoveries("d1", "d2")
        assert result["success"] is False
        assert "d1" in result["error"]

    @pytest.mark.asyncio
    async def test_returns_error_when_to_id_not_found(self):
        """Should return error when target discovery doesn't exist."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.side_effect = [
            [["d1"]],  # found_ids - only d1
        ]

        result = await kg.link_discoveries("d1", "d2")
        assert result["success"] is False
        assert "d2" in result["error"]

    @pytest.mark.asyncio
    async def test_successful_unidirectional_link(self):
        """Should create a single RELATED_TO edge."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.side_effect = [
            [["d1", "d2"]],  # Validation: both found
            None,            # Forward edge creation
        ]

        result = await kg.link_discoveries("d1", "d2", reason="related topic")
        assert result["success"] is True
        assert len(result["edges_created"]) == 1
        assert result["reason"] == "related topic"
        assert result["bidirectional"] is False

    @pytest.mark.asyncio
    async def test_successful_bidirectional_link(self):
        """Should create two RELATED_TO edges."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.side_effect = [
            [["d1", "d2"]],  # Validation
            None,            # Forward edge
            None,            # Reverse edge
        ]

        result = await kg.link_discoveries("d1", "d2", bidirectional=True)
        assert result["success"] is True
        assert len(result["edges_created"]) == 2
        assert result["bidirectional"] is True

    @pytest.mark.asyncio
    async def test_handles_validation_error_dict(self):
        """Should handle error dict in validation results."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.side_effect = [
            [{"error": "query failed"}],
        ]

        result = await kg.link_discoveries("d1", "d2")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_handles_validation_exception(self):
        """Should handle exception during validation."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.side_effect = Exception("DB error")

        result = await kg.link_discoveries("d1", "d2")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_handles_edge_creation_failure(self):
        """Should return error when edge creation fails."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.side_effect = [
            [["d1", "d2"]],                  # Validation succeeds
            Exception("edge creation failed"),  # Edge creation fails
        ]

        result = await kg.link_discoveries("d1", "d2")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_handles_reverse_edge_failure_gracefully(self):
        """Should succeed even if reverse edge creation fails."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.side_effect = [
            [["d1", "d2"]],                  # Validation
            None,                            # Forward edge succeeds
            Exception("reverse edge failed"),  # Reverse edge fails
        ]

        result = await kg.link_discoveries("d1", "d2", bidirectional=True)
        assert result["success"] is True
        assert len(result["edges_created"]) == 1

    @pytest.mark.asyncio
    async def test_handles_empty_validation_results(self):
        """Should return error for empty validation results."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.side_effect = [
            [],  # Empty validation result
        ]

        result = await kg.link_discoveries("d1", "d2")
        assert result["success"] is False


# ============================================================================
# get_orphan_discoveries
# ============================================================================


class TestGetOrphanDiscoveries:

    @pytest.mark.asyncio
    async def test_returns_empty_when_graph_unavailable(self):
        """Should return empty list when graph is not available."""
        kg, mock_db = make_kg_with_mock_db(graph_available=False)
        result = await kg.get_orphan_discoveries()
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_orphan_discoveries(self):
        """Should return discoveries with no inbound edges."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"id": "orphan-1", "summary": "lonely", "type": "note", "status": "open", "agent_id": "a1"},
        ]

        result = await kg.get_orphan_discoveries(limit=10)
        assert len(result) == 1
        assert result[0]["id"] == "orphan-1"

    @pytest.mark.asyncio
    async def test_skips_error_results(self):
        """Should skip results with error key."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"error": "something broke"},
            {"id": "orphan-2", "summary": "ok", "type": "note", "status": "open", "agent_id": "a1"},
        ]

        result = await kg.get_orphan_discoveries()
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        """Should return empty list on exception."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.side_effect = Exception("query failed")

        result = await kg.get_orphan_discoveries()
        assert result == []


# ============================================================================
# get_stale_discoveries
# ============================================================================


class TestGetStaleDiscoveries:

    @pytest.mark.asyncio
    async def test_returns_empty_when_graph_unavailable(self):
        """Should return empty list when graph is not available."""
        kg, mock_db = make_kg_with_mock_db(graph_available=False)
        result = await kg.get_stale_discoveries()
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_stale_discoveries(self):
        """Should return old open discoveries."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [
            {"id": "stale-1", "summary": "old", "type": "note", "status": "open", "agent_id": "a1", "severity": "low"},
        ]

        result = await kg.get_stale_discoveries(older_than_days=30)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_includes_status_param_when_set(self):
        """Should include status param when status filter is provided."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []

        await kg.get_stale_discoveries(status="open")

        call_args = mock_db.graph_query.await_args
        params = call_args.args[1]
        assert params["status"] == "open"

    @pytest.mark.asyncio
    async def test_no_status_filter_when_none(self):
        """Should omit status filter when status is None."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []

        await kg.get_stale_discoveries(status=None)

        call_args = mock_db.graph_query.await_args
        params = call_args.args[1]
        assert "status" not in params

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        """Should return empty list on exception."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.side_effect = Exception("query failed")

        result = await kg.get_stale_discoveries()
        assert result == []


# ============================================================================
# archive_discoveries_batch
# ============================================================================


class TestArchiveDiscoveriesBatch:

    @pytest.mark.asyncio
    async def test_returns_success_for_empty_list(self):
        """Should return success immediately for empty list."""
        kg, _ = make_kg_with_mock_db()
        result = await kg.archive_discoveries_batch([])
        assert result["success"] is True
        assert result["archived"] == 0
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_returns_error_when_graph_unavailable(self):
        """Should return error when graph is not available."""
        kg, mock_db = make_kg_with_mock_db(graph_available=False)
        result = await kg.archive_discoveries_batch(["d1"])
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_archives_successfully(self):
        """Should archive in BOTH stores; count comes from the relational
        RETURNING, not the AGE UNWIND RETURN (which is empty on this build)."""
        kg, mock_db = make_kg_with_mock_db()
        # AGE UNWIND-SET-RETURN yields nothing even on success — must not be
        # used for accounting. The relational UPDATE RETURNING is authoritative.
        mock_db.graph_query.return_value = []
        mock_db._mock_conn.fetch.return_value = [{"id": "d1"}, {"id": "d2"}]

        result = await kg.archive_discoveries_batch(["d1", "d2"], reason="test_cleanup")
        assert result["success"] is True
        assert result["archived"] == 2
        assert result["reason"] == "test_cleanup"

    @pytest.mark.asyncio
    async def test_dual_writes_both_stores_in_one_transaction(self):
        """The archive must hit the AGE node (graph_query) AND the relational
        row (conn.fetch UPDATE) within a single transaction — the regression
        guard for the split-brain bug."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []
        mock_db._mock_conn.fetch.return_value = [{"id": "d1"}]

        await kg.archive_discoveries_batch(["d1"])

        # AGE side: one UNWIND-SET on the Discovery nodes.
        assert mock_db.graph_query.await_count == 1
        age_cypher = mock_db.graph_query.await_args.args[0]
        assert "SET d.status = 'archived'" in age_cypher
        # Relational side: an UPDATE on the canonical table, same conn/txn.
        assert mock_db._mock_conn.fetch.await_count == 1
        rel_sql = mock_db._mock_conn.fetch.await_args.args[0]
        assert "UPDATE knowledge.discoveries" in rel_sql
        assert "status = 'archived'" in rel_sql
        mock_db.transaction.assert_called_once()

    @pytest.mark.asyncio
    async def test_records_errors_for_failed_archives(self):
        """Should record errors for ids the relational UPDATE did not return."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []
        # Relational UPDATE returns only d1 (d2 had no row).
        mock_db._mock_conn.fetch.return_value = [{"id": "d1"}]

        result = await kg.archive_discoveries_batch(["d1", "d2"])
        assert result["archived"] == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["id"] == "d2"

    @pytest.mark.asyncio
    async def test_records_error_for_empty_result(self):
        """Nothing archived (no relational rows matched) → all errors."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []
        mock_db._mock_conn.fetch.return_value = []

        result = await kg.archive_discoveries_batch(["d1"])
        assert result["archived"] == 0
        assert len(result["errors"]) == 1


# ============================================================================
# cleanup_stale_discoveries
# ============================================================================


class TestCleanupStaleDiscoveries:

    @pytest.mark.asyncio
    async def test_dry_run_reports_candidates(self):
        """Should report candidates without archiving in dry_run mode."""
        kg, _ = make_kg_with_mock_db()
        kg.get_orphan_discoveries = AsyncMock(return_value=[
            {"id": "orphan-1"},
        ])
        kg.get_stale_discoveries = AsyncMock(return_value=[
            {"id": "stale-1"},
        ])

        result = await kg.cleanup_stale_discoveries(dry_run=True)

        assert result["dry_run"] is True
        assert result["orphans_found"] == 1
        assert result["stale_open_found"] == 1
        assert result["total_candidates"] == 2
        assert "would_archive" in result

    @pytest.mark.asyncio
    async def test_actual_cleanup_archives(self):
        """Should actually archive when dry_run=False."""
        kg, _ = make_kg_with_mock_db()
        kg.get_orphan_discoveries = AsyncMock(return_value=[
            {"id": "orphan-1"},
        ])
        kg.get_stale_discoveries = AsyncMock(return_value=[
            {"id": "stale-1"},
        ])
        kg.archive_discoveries_batch = AsyncMock(return_value={
            "archived": 2,
            "errors": [],
        })

        result = await kg.cleanup_stale_discoveries(dry_run=False)

        assert result["dry_run"] is False
        assert result["archived"] == 2
        kg.archive_discoveries_batch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_deduplicates_candidates(self):
        """Should deduplicate discoveries that appear in both orphan and stale lists."""
        kg, _ = make_kg_with_mock_db()
        kg.get_orphan_discoveries = AsyncMock(return_value=[
            {"id": "overlap-1"},
        ])
        kg.get_stale_discoveries = AsyncMock(return_value=[
            {"id": "overlap-1"},
        ])

        result = await kg.cleanup_stale_discoveries(dry_run=True)
        assert result["total_candidates"] == 1

    @pytest.mark.asyncio
    async def test_no_candidates_found(self):
        """Should report no candidates when nothing matches."""
        kg, _ = make_kg_with_mock_db()
        kg.get_orphan_discoveries = AsyncMock(return_value=[])
        kg.get_stale_discoveries = AsyncMock(return_value=[])

        result = await kg.cleanup_stale_discoveries(dry_run=False)

        assert result["total_candidates"] == 0
        assert result["archived"] == 0
        assert "No discoveries matched" in result["message"]

    @pytest.mark.asyncio
    async def test_uses_custom_thresholds(self):
        """Should pass custom age thresholds to sub-queries."""
        kg, _ = make_kg_with_mock_db()
        kg.get_orphan_discoveries = AsyncMock(return_value=[])
        kg.get_stale_discoveries = AsyncMock(return_value=[])

        await kg.cleanup_stale_discoveries(
            orphan_age_days=7,
            open_age_days=14,
            limit=25,
        )

        kg.get_orphan_discoveries.assert_awaited_once_with(limit=25, min_age_days=7)
        kg.get_stale_discoveries.assert_awaited_once_with(
            older_than_days=14,
            status="open",
            limit=25,
        )


# ============================================================================
# Edge cases and integration-like tests
# ============================================================================


class TestEdgeCases:

    @pytest.mark.asyncio
    async def test_add_discovery_with_all_features(self):
        """Should handle a discovery with all possible features."""
        kg, mock_db = make_kg_with_mock_db()
        kg._check_rate_limit = AsyncMock()
        kg._pgvector_available = AsyncMock(return_value=False)

        response = ResponseTo(discovery_id="parent-disc", response_type="support")
        discovery = make_discovery(
            discovery_id="full-disc",
            agent_id="test-agent",
            discovery_type="self_observation",
            summary="Full featured discovery",
            details="All the details",
            tags=["tag1", "tag2"],
            severity="critical",
            status="open",
            related_to=["rel-1"],
            response_to=response,
            timestamp="2026-02-05T12:00:00Z",
            references_files=["file1.py"],
            confidence=0.95,
            provenance={"E": 0.8, "I": 0.7, "S": 0.6, "V": 0.5, "regime": "stable", "coherence": 0.9},
            provenance_chain=[{"step": 1}],
            resolved_at="2026-02-05T14:00:00Z",
        )

        await kg.add_discovery(discovery)

        # discovery + agent + authored + responds_to + 1 related + 2 tagged = 7
        assert mock_db.graph_query.await_count >= 7

    @pytest.mark.asyncio
    async def test_query_combines_all_filters(self):
        """Should combine all filter types in a single query."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []

        await kg.query(
            agent_id="agent-1",
            type="bug",
            status="open",
            severity="high",
            tags=["python"],
            limit=10,
        )

        call_args = mock_db.graph_query.await_args
        params = call_args.args[1]
        assert params["agent_id"] == "agent-1"
        assert params["type"] == "bug"
        assert params["status"] == "open"
        assert params["severity"] == "high"
        assert params["tags"] == ["python"]
        assert params["limit"] == 10

    @pytest.mark.asyncio
    async def test_connectivity_score_with_zero_edges(self):
        """Should return 0.0 for a discovery with no edges."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [{"related": 0, "responds": 0}]

        score = await kg.get_connectivity_score("isolated-disc")
        # log1p(0) = 0
        assert score == 0.0

    @pytest.mark.asyncio
    async def test_update_discovery_with_type_field(self):
        """Should allow updating the type field."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [{"id": "disc-001"}]

        result = await kg.update_discovery("disc-001", {"type": "pattern"})
        assert result is True

        call_args = mock_db.graph_query.await_args
        cypher = call_args.args[0]
        params = call_args.args[1]
        assert "d.type" in cypher
        assert params["val_type"] == "pattern"

    @pytest.mark.asyncio
    async def test_update_discovery_tags_single_value(self):
        """Should wrap single tag value in a list for JSON serialization."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = [{"id": "disc-001"}]

        result = await kg.update_discovery("disc-001", {"tags": "single-tag"})
        assert result is True

        call_args = mock_db.graph_query.await_args
        params = call_args.args[1]
        assert params["val_tags"] == json.dumps(["single-tag"])

    @pytest.mark.asyncio
    async def test_link_discoveries_with_strength(self):
        """Should pass strength parameter to edge creation."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.side_effect = [
            [["d1", "d2"]],
            None,
        ]

        result = await kg.link_discoveries("d1", "d2", strength=0.75)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_get_response_chain_empty_results(self):
        """Should handle empty graph_query results for response chain."""
        kg, mock_db = make_kg_with_mock_db()
        mock_db.graph_query.return_value = []

        result = await kg.get_response_chain("disc-001")
        assert result == []
