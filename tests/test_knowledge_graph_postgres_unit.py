"""Unit tests for KnowledgeGraphPostgres timestamp coercion.

Covers the regression where lifecycle cleanup passed ISO-format strings
for timestamp-typed columns, which asyncpg rejected. The backend now
coerces strings to datetime at its boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.db.mixins.knowledge_graph import KnowledgeGraphMixin
from src.knowledge_graph import DiscoveryNode
from src.storage.knowledge_graph_postgres import (
    KnowledgeGraphPostgres,
    _coerce_timestamp,
)


class TestCoerceTimestamp:
    def test_passes_datetime_through(self):
        now = datetime.now(timezone.utc)
        assert _coerce_timestamp(now) is now

    def test_parses_iso_string(self):
        result = _coerce_timestamp("2026-04-18T01:23:45.678+00:00")
        assert isinstance(result, datetime)
        assert result.year == 2026 and result.minute == 23

    def test_parses_zulu_suffix(self):
        result = _coerce_timestamp("2026-04-18T01:23:45Z")
        assert isinstance(result, datetime)
        assert result.tzinfo is not None


class _AcquireContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeKgBackend(KnowledgeGraphMixin):
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _AcquireContext(self.conn)


@pytest.mark.asyncio
async def test_kg_query_applies_lifecycle_exclusions_before_limit():
    """Archived/cold rows must not consume the SQL query's result limit."""
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    backend = _FakeKgBackend(conn)

    await backend.kg_query(
        exclude_archived=True,
        exclude_cold=True,
        limit=7,
    )

    query, *args = conn.fetch.await_args.args
    assert "status IS DISTINCT FROM 'archived'" in query
    assert "status IS DISTINCT FROM 'cold'" in query
    assert args == [7]


@pytest.mark.asyncio
async def test_kg_add_discovery_persists_provenance_chain():
    captured = {}

    async def fake_execute(query, *args):
        captured["query"] = query
        captured["args"] = args

    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=fake_execute)
    backend = _FakeKgBackend(conn)
    chain = [{"agent_id": "parent", "relationship": "direct_parent"}]

    await backend.kg_add_discovery(
        DiscoveryNode(
            id="disc-1",
            agent_id="agent-1",
            type="note",
            summary="summary",
            provenance_chain=chain,
        )
    )

    assert "provenance_chain" in captured["query"]
    assert len(captured["args"]) == 16
    assert json.loads(captured["args"][13]) == chain


def test_row_to_discovery_dict_decodes_provenance_chain_json():
    backend = _FakeKgBackend(MagicMock())
    row = {
        "id": "disc-1",
        "agent_id": "agent-1",
        "type": "note",
        "summary": "summary",
        "created_at": datetime.now(timezone.utc),
        "provenance_chain": '[{"agent_id": "parent"}]',
    }

    result = backend._row_to_discovery_dict(row)

    assert result["provenance_chain"] == [{"agent_id": "parent"}]


@pytest.mark.asyncio
async def test_kg_get_discoveries_by_ids_batches_in_one_query():
    """Batch fetch collapses the per-id get_discovery N+1 (the loop that the
    in-handler anyio<->asyncpg await tax turned into a multi-second cost) into a
    single query: one fetch, id->dict map, empty input short-circuits with no DB
    round-trip."""
    calls = {"fetch": 0}

    async def fake_fetch(query, *args):
        calls["fetch"] += 1
        assert "id = ANY" in query  # single batched query, not per-id
        now = datetime.now(timezone.utc)
        return [
            {"id": "d-1", "agent_id": "a", "type": "note", "summary": "one", "created_at": now},
            {"id": "d-2", "agent_id": "a", "type": "note", "summary": "two", "created_at": now},
        ]

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=fake_fetch)
    backend = _FakeKgBackend(conn)

    out = await backend.kg_get_discoveries_by_ids(["d-1", "d-2"])
    assert calls["fetch"] == 1
    assert set(out) == {"d-1", "d-2"}
    assert out["d-1"]["summary"] == "one"

    # Empty input must not touch the DB.
    assert await backend.kg_get_discoveries_by_ids([]) == {}
    assert calls["fetch"] == 1


@pytest.mark.asyncio
async def test_kg_tag_stats_counts_canonical_sql_assignments_with_scope():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {"tag": "bug", "count": 3},
        {"tag": "python", "count": 2},
    ])
    backend = _FakeKgBackend(conn)

    result = await backend.kg_tag_stats(
        including_cold=False,
        epoch=3,
    )

    query, *args = conn.fetch.await_args.args
    assert "CROSS JOIN LATERAL unnest(" in query
    assert "COALESCE(d.tags" in query
    assert "d.epoch = $1" in query
    assert "d.status != 'cold'" in query
    assert "ORDER BY count DESC, tag ASC" in query
    assert args == [3]
    assert result == {
        "by_tag": {"bug": 3, "python": 2},
        "total_tags": 2,
        "total_tag_assignments": 5,
    }


@pytest.mark.asyncio
async def test_kg_tag_stats_all_scope_including_cold_has_no_filters():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    backend = _FakeKgBackend(conn)

    result = await backend.kg_tag_stats(including_cold=True, epoch=None)

    query = conn.fetch.await_args.args[0]
    assert "d.epoch =" not in query
    assert "d.status != 'cold'" not in query
    assert result == {
        "by_tag": {},
        "total_tags": 0,
        "total_tag_assignments": 0,
    }


@pytest.mark.asyncio
async def test_kg_topic_candidates_filters_partition_tags_before_limit():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {"topic": "identity", "member_count": 9, "open_count": 4},
    ])
    backend = _FakeKgBackend(conn)

    result = await backend.kg_topic_candidates(
        min_members=5,
        limit=7,
        exclude_types=["topic_rollup"],
        exclude_tags=["ephemeral", "audit"],
        exclude_tag_prefixes=["slug-", "source-"],
    )

    query, *args = conn.fetch.await_args.args
    assert "NOT (tag = ANY($4::text[]))" in query
    assert "tag LIKE blocked.prefix || '%'" in query
    assert args == [
        5,
        7,
        ["topic_rollup"],
        ["ephemeral", "audit"],
        ["slug-", "source-"],
    ]
    assert result == [
        {"topic": "identity", "member_count": 9, "open_count": 4}
    ]


@pytest.mark.asyncio
async def test_postgres_stats_use_sql_tag_counts_with_current_epoch_scope():
    from config.governance_config import GovernanceConfig

    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[3, 3])
    conn.fetch = AsyncMock(side_effect=[
        [{"agent_id": "agent-1", "count": 3}],
        [{"type": "note", "count": 3}],
        [{"status": "open", "count": 3}],
        [{"source": "explicit_store", "count": 3}],
        [{"agent_id": "agent-1", "source": "explicit_store", "count": 3}],
    ])
    db = MagicMock()
    db.acquire = lambda: _AcquireContext(conn)
    db.kg_tag_stats = AsyncMock(return_value={
        "by_tag": {"memory": 3, "kg": 2},
        "total_tags": 2,
        "total_tag_assignments": 5,
    })
    backend = KnowledgeGraphPostgres()

    async def _get_db():
        return db

    backend._get_db = _get_db  # type: ignore[assignment]

    result = await backend.get_stats(
        epoch_scope="current",
        including_cold=False,
    )

    db.kg_tag_stats.assert_awaited_once_with(
        including_cold=False,
        epoch=GovernanceConfig.CURRENT_EPOCH,
    )
    assert result["by_tag"] == {"memory": 3, "kg": 2}
    assert result["total_tags"] == 2
    assert result["total_tag_assignments"] == 5


@pytest.mark.asyncio
class TestUpdateDiscoveryTimestampCoercion:
    async def _make_backend(self, captured: list):
        """KnowledgeGraphPostgres with a mocked acquire() that records fetchval args."""
        backend = KnowledgeGraphPostgres()

        async def fake_fetchval(query, *args):
            captured.append((query, args))
            return "discovery-id"

        conn = MagicMock()
        conn.fetchval = AsyncMock(side_effect=fake_fetchval)
        db = MagicMock()
        db.acquire = lambda: _AcquireContext(conn)
        backend._db = db
        backend._initialized = True

        async def _get_db():
            return db

        backend._get_db = _get_db  # type: ignore[assignment]
        return backend

    async def test_updated_at_string_coerced_to_datetime(self):
        captured: list = []
        backend = await self._make_backend(captured)

        ok = await backend.update_discovery(
            "discovery-id",
            {"status": "archived", "updated_at": "2026-04-18T01:23:45+00:00"},
        )

        assert ok is True
        assert len(captured) == 1
        _query, args = captured[0]
        # args: (discovery_id, status, updated_at)
        assert args[0] == "discovery-id"
        assert args[1] == "archived"
        assert isinstance(args[2], datetime), (
            f"updated_at must be datetime for asyncpg, got {type(args[2]).__name__}"
        )

    async def test_resolved_at_string_coerced_to_datetime(self):
        captured: list = []
        backend = await self._make_backend(captured)

        await backend.update_discovery(
            "discovery-id",
            {"resolved_at": "2026-04-18T01:23:45+00:00"},
        )

        assert len(captured) == 1
        _query, args = captured[0]
        assert isinstance(args[1], datetime)

    async def test_datetime_passes_through(self):
        captured: list = []
        backend = await self._make_backend(captured)
        now = datetime.now(timezone.utc)

        await backend.update_discovery(
            "discovery-id",
            {"updated_at": now},
        )

        _query, args = captured[0]
        assert args[1] is now


def test_dict_to_discovery_preserves_provenance_chain():
    backend = KnowledgeGraphPostgres()

    discovery = backend._dict_to_discovery(
        {
            "id": "disc-1",
            "agent_id": "agent-1",
            "type": "note",
            "summary": "summary",
            "provenance_chain": [{"agent_id": "parent"}],
        }
    )

    assert discovery.provenance_chain == [{"agent_id": "parent"}]
