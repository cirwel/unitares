"""
Unit tests for the AGE backend SQL fallback paths.

Covers the regression where get_discovery and update_discovery only did
Cypher MATCH lookups, silently returning None/False for SQL-only orphan
discoveries (rows that exist in knowledge.discoveries but have no AGE node,
written while UNITARES_KNOWLEDGE_BACKEND was postgres).

KG bug ID: 2026-04-25T21:33:15.971499
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Pre-stub the mcp/pydantic import chain before loading the AGE backend.
# src.storage.knowledge_graph_age imports src.mcp_handlers.knowledge.limits
# which pulls in src/mcp_handlers/__init__.py → mcp.types → pydantic.RootModel.
# On Python 3.14 + pydantic < 3.x, RootModel class creation fails with
# "TypeError: _eval_type() got an unexpected keyword argument 'prefer_fwd_module'".
# Stubbing at module level is safe because conftest.py does not import mcp.types.
# ---------------------------------------------------------------------------
_limits_stub = MagicMock()
_limits_stub.EMBED_DETAILS_WINDOW = 200
sys.modules.setdefault("src.mcp_handlers.knowledge.limits", _limits_stub)
sys.modules.setdefault("src.mcp_handlers.knowledge", MagicMock())
_mcp_types_stub = MagicMock()
_mcp_types_stub.TextContent = MagicMock
sys.modules.setdefault("mcp.types", _mcp_types_stub)
sys.modules.setdefault("mcp.server", MagicMock())
sys.modules.setdefault("mcp.server.fastmcp", MagicMock())
_mcp_stub = MagicMock()
_mcp_stub.types = _mcp_types_stub
sys.modules.setdefault("mcp", _mcp_stub)

# Only stub src.mcp_handlers if it has not already been imported as the real module.
# (Avoids overwriting a partially-initialised real module that another conftest fixture
# may have set up; our test doesn't call any handler code anyway.)
if "src.mcp_handlers" not in sys.modules:
    _mcp_handlers_stub = MagicMock()
    _mcp_handlers_stub.knowledge = MagicMock()
    sys.modules["src.mcp_handlers"] = _mcp_handlers_stub

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.storage.knowledge_graph_age import KnowledgeGraphAGE  # noqa: E402
from src.knowledge_graph import DiscoveryNode, ResponseTo  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sql_row(
    *,
    discovery_id: str = "disc-sql-001",
    agent_id: str = "agent-a",
    type: str = "bug",
    summary: str = "A SQL-only discovery",
    status: str = "open",
    severity: str = "medium",
    tags: list | None = None,
    created_at: str = "2026-04-24T01:03:00+00:00",
    updated_at: str | None = None,
    resolved_at: str | None = None,
    response_to_id: str | None = None,
    response_type: str | None = None,
    references_files: list | None = None,
    related_to: list | None = None,
    provenance: dict | None = None,
) -> dict:
    return {
        "id": discovery_id,
        "agent_id": agent_id,
        "type": type,
        "summary": summary,
        "details": "Some details",
        "status": status,
        "severity": severity,
        "tags": tags or ["k8s", "regression"],
        "created_at": created_at,
        "updated_at": updated_at,
        "resolved_at": resolved_at,
        "response_to_id": response_to_id,
        "response_type": response_type,
        "references_files": references_files or [],
        "related_to": related_to or [],
        "provenance": provenance,
    }


def _make_db(
    *,
    graph_available: bool = True,
    graph_query_returns=None,
    kg_get_discovery_returns=None,
) -> MagicMock:
    db = MagicMock()
    db.graph_available = AsyncMock(return_value=graph_available)
    db.graph_query = AsyncMock(return_value=graph_query_returns if graph_query_returns is not None else [])
    db.kg_get_discovery = AsyncMock(return_value=kg_get_discovery_returns)

    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=None)
    pool.execute = AsyncMock()
    pool.executemany = AsyncMock()

    @asynccontextmanager
    async def fake_acquire():
        yield pool

    pool.acquire = fake_acquire
    db._pool = pool

    @asynccontextmanager
    async def fake_transaction():
        yield pool

    db.transaction = fake_transaction
    return db


async def _make_kg(db: MagicMock) -> KnowledgeGraphAGE:
    kg = KnowledgeGraphAGE()
    kg._db = db

    async def _get_db():
        return db

    kg._get_db = _get_db  # type: ignore[assignment]
    return kg


# ===========================================================================
# _dict_to_discovery
# ===========================================================================

class TestDictToDiscovery:

    def setup_method(self):
        self.kg = KnowledgeGraphAGE()

    def test_none_returns_none(self):
        assert self.kg._dict_to_discovery(None) is None

    def test_empty_dict_returns_none(self):
        assert self.kg._dict_to_discovery({}) is None

    def test_minimal_row(self):
        row = _sql_row()
        d = self.kg._dict_to_discovery(row)
        assert d is not None
        assert d.id == "disc-sql-001"
        assert d.agent_id == "agent-a"
        assert d.type == "bug"
        assert d.summary == "A SQL-only discovery"
        assert d.status == "open"
        assert d.severity == "medium"

    def test_tags_preserved(self):
        row = _sql_row(tags=["perf", "db"])
        d = self.kg._dict_to_discovery(row)
        assert d.tags == ["perf", "db"]

    def test_response_to_populated(self):
        row = _sql_row(response_to_id="disc-parent", response_type="extend")
        d = self.kg._dict_to_discovery(row)
        assert d.response_to is not None
        assert d.response_to.discovery_id == "disc-parent"
        assert d.response_to.response_type == "extend"

    def test_response_to_absent_when_no_id(self):
        row = _sql_row(response_to_id=None, response_type="extend")
        d = self.kg._dict_to_discovery(row)
        assert d.response_to is None

    def test_timestamps_passed_through(self):
        row = _sql_row(
            updated_at="2026-04-25T10:00:00+00:00",
            resolved_at="2026-04-25T11:00:00+00:00",
        )
        d = self.kg._dict_to_discovery(row)
        assert d.updated_at == "2026-04-25T10:00:00+00:00"
        assert d.resolved_at == "2026-04-25T11:00:00+00:00"

    def test_created_at_used_as_timestamp_fallback(self):
        row = _sql_row(created_at="2026-04-24T01:03:00+00:00")
        row.pop("timestamp", None)
        d = self.kg._dict_to_discovery(row)
        assert d.timestamp == "2026-04-24T01:03:00+00:00"

    def test_provenance_passed_through(self):
        prov = {"source": "claude", "session": "abc123"}
        row = _sql_row(provenance=prov)
        d = self.kg._dict_to_discovery(row)
        assert d.provenance == prov


# ===========================================================================
# get_discovery — SQL fallback
# ===========================================================================

@pytest.mark.asyncio
class TestGetDiscoverySQLFallback:

    async def test_returns_sql_row_when_no_age_node(self):
        """When graph_query returns [], fall back to kg_get_discovery."""
        sql_row = _sql_row()
        db = _make_db(graph_query_returns=[], kg_get_discovery_returns=sql_row)
        kg = await _make_kg(db)

        result = await kg.get_discovery("disc-sql-001")

        db.kg_get_discovery.assert_awaited_once_with("disc-sql-001")
        assert result is not None
        assert result.id == "disc-sql-001"
        assert result.type == "bug"

    async def test_returns_none_when_missing_everywhere(self):
        """No AGE node AND no SQL row → None."""
        db = _make_db(graph_query_returns=[], kg_get_discovery_returns=None)
        kg = await _make_kg(db)

        result = await kg.get_discovery("nonexistent-id")

        assert result is None

    async def test_age_result_used_when_node_exists(self):
        """When graph_query returns a node, SQL fallback is NOT called."""
        age_node = {
            "d": {
                "properties": {
                    "id": "disc-age-001",
                    "agent_id": "agent-b",
                    "summary": "AGE discovery",
                    "type": "insight",
                }
            }
        }
        db = _make_db(graph_query_returns=[age_node])
        kg = await _make_kg(db)

        result = await kg.get_discovery("disc-age-001")

        db.kg_get_discovery.assert_not_awaited()
        assert result is not None
        assert result.id == "disc-age-001"

    async def test_fallback_returns_tags(self):
        sql_row = _sql_row(tags=["timeout", "network"])
        db = _make_db(graph_query_returns=[], kg_get_discovery_returns=sql_row)
        kg = await _make_kg(db)

        result = await kg.get_discovery("disc-sql-001")
        assert result.tags == ["timeout", "network"]

    async def test_fallback_returns_response_to(self):
        sql_row = _sql_row(response_to_id="parent-disc", response_type="refute")
        db = _make_db(graph_query_returns=[], kg_get_discovery_returns=sql_row)
        kg = await _make_kg(db)

        result = await kg.get_discovery("disc-sql-001")
        assert result.response_to is not None
        assert result.response_to.discovery_id == "parent-disc"
        assert result.response_to.response_type == "refute"


# ===========================================================================
# _sql_update_discovery
# ===========================================================================

@pytest.mark.asyncio
class TestSqlUpdateDiscovery:

    async def test_status_update(self):
        db = _make_db()
        db._pool.fetchval = AsyncMock(return_value="disc-sql-001")
        kg = await _make_kg(db)

        ok = await kg._sql_update_discovery("disc-sql-001", {"status": "resolved"})

        assert ok is True
        db._pool.fetchval.assert_awaited_once()
        query, *args = db._pool.fetchval.call_args[0]
        assert "UPDATE knowledge.discoveries" in query
        assert "status" in query
        assert "RETURNING id" in query
        assert "resolved" in args
        assert "disc-sql-001" in args

    async def test_returns_false_when_row_not_found(self):
        db = _make_db()
        db._pool.fetchval = AsyncMock(return_value=None)
        kg = await _make_kg(db)

        ok = await kg._sql_update_discovery("nonexistent", {"status": "archived"})
        assert ok is False

    async def test_empty_updates_returns_true(self):
        db = _make_db()
        kg = await _make_kg(db)

        ok = await kg._sql_update_discovery("disc-sql-001", {})
        assert ok is True
        db._pool.fetchval.assert_not_awaited()

    async def test_timestamp_coerced_to_datetime(self):
        db = _make_db()
        captured: list = []

        async def record_fetchval(query, *args):
            captured.append((query, args))
            return "disc-sql-001"

        db._pool.fetchval = AsyncMock(side_effect=record_fetchval)
        kg = await _make_kg(db)

        await kg._sql_update_discovery(
            "disc-sql-001",
            {"updated_at": "2026-04-25T21:33:15.971499+00:00"},
        )

        assert len(captured) == 1
        _q, args = captured[0]
        updated_at_arg = next(a for a in args if isinstance(a, datetime))
        assert updated_at_arg.year == 2026

    async def test_tags_sync_called_when_tags_updated(self):
        db = _make_db()
        db._pool.fetchval = AsyncMock(return_value="disc-sql-001")

        sync_called: list = []

        async def fake_sync_tags(conn, disc_id, tags):
            sync_called.append((disc_id, tags))

        kg = await _make_kg(db)
        kg._sync_discovery_tags = fake_sync_tags  # type: ignore[assignment]

        await kg._sql_update_discovery("disc-sql-001", {"tags": ["new-tag"]})
        assert len(sync_called) == 1
        assert sync_called[0][0] == "disc-sql-001"
        assert "new-tag" in sync_called[0][1]

    async def test_tags_sync_not_called_when_row_missing(self):
        db = _make_db()
        db._pool.fetchval = AsyncMock(return_value=None)
        sync_called: list = []

        async def fake_sync_tags(conn, disc_id, tags):
            sync_called.append((disc_id, tags))

        kg = await _make_kg(db)
        kg._sync_discovery_tags = fake_sync_tags  # type: ignore[assignment]

        await kg._sql_update_discovery("nonexistent", {"tags": ["t"]})
        assert sync_called == []

    async def test_multiple_fields(self):
        db = _make_db()
        captured: list = []

        async def record_fetchval(query, *args):
            captured.append((query, list(args)))
            return "disc-sql-001"

        db._pool.fetchval = AsyncMock(side_effect=record_fetchval)
        kg = await _make_kg(db)

        await kg._sql_update_discovery(
            "disc-sql-001",
            {"status": "resolved", "severity": "high", "summary": "Fixed"},
        )

        assert len(captured) == 1
        query, args = captured[0]
        assert "status" in query
        assert "severity" in query
        assert "summary" in query
        assert "resolved" in args
        assert "high" in args
        assert "Fixed" in args


# ===========================================================================
# update_discovery — SQL fallback integration
# ===========================================================================

@pytest.mark.asyncio
class TestUpdateDiscoverySQLFallback:

    async def test_sql_fallback_when_no_age_node(self):
        """Cypher MATCH returns [] → SQL UPDATE called."""
        db = _make_db(graph_available=True, graph_query_returns=[])
        db._pool.fetchval = AsyncMock(return_value="disc-sql-001")
        kg = await _make_kg(db)

        ok = await kg.update_discovery("disc-sql-001", {"status": "resolved"})

        assert ok is True
        db._pool.fetchval.assert_awaited_once()

    async def test_sql_fallback_when_graph_unavailable(self):
        """graph_available() False → directly calls SQL fallback."""
        db = _make_db(graph_available=False)
        db._pool.fetchval = AsyncMock(return_value="disc-sql-001")
        kg = await _make_kg(db)

        ok = await kg.update_discovery("disc-sql-001", {"status": "archived"})

        assert ok is True
        db._pool.fetchval.assert_awaited_once()
        db.graph_query.assert_not_awaited()

    async def test_returns_false_when_sql_row_also_missing(self):
        """No AGE node AND no SQL row → False."""
        db = _make_db(graph_available=True, graph_query_returns=[])
        db._pool.fetchval = AsyncMock(return_value=None)
        kg = await _make_kg(db)

        ok = await kg.update_discovery("nonexistent", {"status": "archived"})
        assert ok is False

    async def test_age_path_not_sql_when_age_node_exists(self):
        """When Cypher MATCH succeeds, SQL fallback pool.fetchval is NOT called."""
        age_result = [{"d.id": "disc-age-001"}]
        db = _make_db(graph_available=True, graph_query_returns=age_result)
        db._pool.fetchval = AsyncMock(return_value=None)

        sync_calls: list = []

        kg = await _make_kg(db)

        async def fake_sync(conn, disc_id, updates):
            sync_calls.append(disc_id)

        kg._sync_updated_discovery_row = fake_sync  # type: ignore[assignment]
        kg._refresh_embedding = AsyncMock()  # type: ignore[assignment]

        ok = await kg.update_discovery("disc-age-001", {"status": "resolved"})

        assert ok is True
        db._pool.fetchval.assert_not_awaited()
        assert "disc-age-001" in sync_calls

    async def test_empty_updates_skips_both_paths(self):
        """No-op update returns True without DB round-trips."""
        db = _make_db(graph_available=True)
        kg = await _make_kg(db)

        ok = await kg.update_discovery("disc-001", {})
        assert ok is True
        db.graph_query.assert_not_awaited()
        db._pool.fetchval.assert_not_awaited()


# ===========================================================================
# update_discovery concurrent-update retry (AGE TM_Updated)
# ===========================================================================

@pytest.mark.asyncio
class TestUpdateDiscoveryConcurrentRetry:
    """AGE raises "Entity failed to be updated: <TM_Result>" on a write-write
    race instead of re-evaluating the tuple like plain PostgreSQL UPDATE.
    The conflict is transient, so update_discovery retries once."""

    def _kg_with_age_node(self, db):
        db._pool.fetchval = AsyncMock(return_value=None)
        return db

    async def test_retries_once_on_concurrent_update_conflict(self):
        db = _make_db(graph_available=True)
        db.graph_query = AsyncMock(
            side_effect=[
                Exception("Entity failed to be updated: 3"),
                [{"d.id": "disc-age-001"}],
            ]
        )
        kg = await _make_kg(db)
        kg._sync_updated_discovery_row = AsyncMock()  # type: ignore[assignment]
        kg._refresh_embedding = AsyncMock()  # type: ignore[assignment]

        ok = await kg.update_discovery("disc-age-001", {"status": "resolved"})

        assert ok is True
        assert db.graph_query.await_count == 2
        kg._sync_updated_discovery_row.assert_awaited_once()

    async def test_returns_false_when_conflict_persists(self):
        db = _make_db(graph_available=True)
        db.graph_query = AsyncMock(
            side_effect=Exception("Entity failed to be updated: 3")
        )
        kg = await _make_kg(db)

        ok = await kg.update_discovery("disc-age-001", {"status": "resolved"})

        assert ok is False
        assert db.graph_query.await_count == 2  # initial attempt + one retry

    async def test_no_retry_on_unrelated_errors(self):
        db = _make_db(graph_available=True)
        db.graph_query = AsyncMock(side_effect=ValueError("boom"))
        kg = await _make_kg(db)

        ok = await kg.update_discovery("disc-age-001", {"status": "resolved"})

        assert ok is False
        assert db.graph_query.await_count == 1


# ===========================================================================
# last_referenced removed from the update surface
# ===========================================================================

@pytest.mark.asyncio
class TestLastReferencedRemoved:
    """last_referenced was a write-only AGE vertex property: no reader
    anywhere, no SQL column, and its fire-and-forget touches raced between
    concurrent sessions producing TM_Updated error storms. It is no longer
    an updatable field on either path."""

    async def test_age_path_treats_last_referenced_as_noop(self):
        db = _make_db(graph_available=True)
        kg = await _make_kg(db)

        ok = await kg.update_discovery(
            "disc-age-001", {"last_referenced": "2026-06-11T00:00:00+00:00"}
        )

        assert ok is True
        db.graph_query.assert_not_awaited()
        db._pool.fetchval.assert_not_awaited()

    async def test_sql_fallback_treats_last_referenced_as_noop(self):
        db = _make_db(graph_available=False)
        kg = await _make_kg(db)

        ok = await kg._sql_update_discovery(
            "disc-sql-001", {"last_referenced": "2026-06-11T00:00:00+00:00"}
        )

        assert ok is True
        db._pool.fetchval.assert_not_awaited()

    async def test_update_discovery_last_referenced_noop_when_graph_unavailable(self):
        db = _make_db(graph_available=False)
        kg = await _make_kg(db)

        ok = await kg.update_discovery(
            "disc-sql-001", {"last_referenced": "2026-06-11T00:00:00+00:00"}
        )

        assert ok is True
        db.graph_query.assert_not_awaited()
        db._pool.fetchval.assert_not_awaited()


# ===========================================================================
# query — SQL fallback
# ===========================================================================
#
# Same bug class as get_discovery above, one read path over: query() did a
# Cypher-only MATCH and returned [] for SQL-only orphan rows. That made the
# knowledge(action="search") no-text path and the list/stats surface look
# write-only (store + get worked, list + search returned nothing).

@pytest.mark.asyncio
class TestQuerySQLFallback:

    async def test_returns_sql_rows_when_graph_unavailable(self):
        """graph_available() False → read straight from knowledge.discoveries."""
        db = _make_db(graph_available=False)
        db.kg_query = AsyncMock(return_value=[_sql_row(), _sql_row(discovery_id="disc-sql-002")])
        kg = await _make_kg(db)

        results = await kg.query(limit=50)

        db.kg_query.assert_awaited_once()
        db.graph_query.assert_not_awaited()
        assert [d.id for d in results] == ["disc-sql-001", "disc-sql-002"]

    async def test_returns_sql_rows_when_age_empty(self):
        """Graph available but holds no vertex for the row → SQL fallback."""
        db = _make_db(graph_available=True, graph_query_returns=[])
        db.kg_query = AsyncMock(return_value=[_sql_row()])
        kg = await _make_kg(db)

        results = await kg.query(limit=50)

        db.graph_query.assert_awaited()  # AGE attempted first
        db.kg_query.assert_awaited_once()
        assert len(results) == 1
        assert results[0].id == "disc-sql-001"

    async def test_age_results_used_without_sql_fallback(self):
        """When AGE returns nodes, the SQL table is not consulted."""
        age_node = {
            "d": {
                "properties": {
                    "id": "disc-age-001",
                    "agent_id": "agent-b",
                    "summary": "AGE discovery",
                    "type": "insight",
                }
            }
        }
        db = _make_db(graph_available=True, graph_query_returns=[age_node])
        db.kg_query = AsyncMock(return_value=[_sql_row()])
        kg = await _make_kg(db)

        results = await kg.query(limit=50)

        db.kg_query.assert_not_awaited()
        assert [d.id for d in results] == ["disc-age-001"]

    async def test_fallback_excludes_archived(self):
        """exclude_archived drops archived SQL rows (kg_query can't express it)."""
        db = _make_db(graph_available=False)
        db.kg_query = AsyncMock(return_value=[
            _sql_row(discovery_id="open-1", status="open"),
            _sql_row(discovery_id="arch-1", status="archived"),
        ])
        kg = await _make_kg(db)

        results = await kg.query(limit=50, exclude_archived=True)

        assert [d.id for d in results] == ["open-1"]

    async def test_fallback_forwards_filters(self):
        """Filters reach kg_query so the SQL scan stays selective."""
        db = _make_db(graph_available=False)
        db.kg_query = AsyncMock(return_value=[])
        kg = await _make_kg(db)

        await kg.query(agent_id="agent-a", type="bug", status="open", limit=10)

        kwargs = db.kg_query.call_args.kwargs
        assert kwargs["agent_id"] == "agent-a"
        assert kwargs["type"] == "bug"
        assert kwargs["status"] == "open"
        assert kwargs["limit"] == 10


# ===========================================================================
# get_stats — SQL fallback
# ===========================================================================

def _stats_graph_query(total_discoveries: int):
    """Build a graph_query side_effect that reports `total_discoveries` vertices."""
    async def fake(cypher, params=None, conn=None):
        if "count(d)" in cypher:
            return [total_discoveries]
        if "collect(d.agent_id)" in cypher:
            return [["agent-a"] * total_discoveries]
        if "collect(d.type)" in cypher:
            return [["bug"] * total_discoveries]
        if "collect(d.status)" in cypher:
            return [["open"] * total_discoveries]
        if "count(r)" in cypher:
            return [0]
        if "(t:Tag) RETURN count" in cypher:
            return [0]
        if "collect(t.name)" in cypher:
            return [[]]
        return []
    return fake


@pytest.mark.asyncio
class TestGetStatsSQLFallback:

    async def test_falls_back_to_sql_when_age_empty(self):
        """AGE holds 0 Discovery vertices but SQL has rows → report SQL counts."""
        db = _make_db(graph_available=True)
        db.graph_query = AsyncMock(side_effect=_stats_graph_query(0))
        db.kg_stats = AsyncMock(return_value={
            "total_discoveries": 7,
            "by_agent": {"agent-a": 5, "agent-b": 2},
            "by_type": {"bug": 7},
            "by_status": {"open": 7},
            "by_tag": {},
            "total_edges": 0,
            "total_tags": 0,
            "total_agents": 2,
        })
        kg = await _make_kg(db)

        stats = await kg.get_stats()

        db.kg_stats.assert_awaited_once()
        assert stats["total_discoveries"] == 7
        assert stats["total_agents"] == 2
        assert stats["scope"]["note"].startswith("AGE graph held no Discovery vertices")

    async def test_uses_age_counts_when_present(self):
        """AGE has vertices → SQL stats fallback is not consulted."""
        db = _make_db(graph_available=True)
        db.graph_query = AsyncMock(side_effect=_stats_graph_query(3))
        db.kg_stats = AsyncMock(return_value={"total_discoveries": 999})
        kg = await _make_kg(db)

        stats = await kg.get_stats()

        db.kg_stats.assert_not_awaited()
        assert stats["total_discoveries"] == 3

    async def test_age_empty_and_sql_empty_returns_age_response(self):
        """Both empty → graceful AGE-derived zero response (no crash)."""
        db = _make_db(graph_available=True)
        db.graph_query = AsyncMock(side_effect=_stats_graph_query(0))
        db.kg_stats = AsyncMock(return_value={"total_discoveries": 0, "by_agent": {}})
        kg = await _make_kg(db)

        stats = await kg.get_stats()

        assert stats["total_discoveries"] == 0
        # Falls through to the AGE response shape, which carries the AGE note.
        assert "AGE backend has no epoch property" in stats["scope"]["note"]
