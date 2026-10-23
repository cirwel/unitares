from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.storage.knowledge_graph_age import KnowledgeGraphAGE


@pytest.mark.asyncio
async def test_load_rehydrates_when_age_empty_and_postgres_has_data():
    kg = KnowledgeGraphAGE()
    db = AsyncMock()
    db.graph_available.return_value = True
    kg._get_db = AsyncMock(return_value=db)
    kg._count_postgres_discoveries = AsyncMock(return_value=5)
    kg._count_age_discoveries = AsyncMock(return_value=0)
    kg._rehydrate_from_postgres = AsyncMock(
        return_value={"discoveries": 5, "related_edges": 2}
    )

    await kg.load()

    kg._rehydrate_from_postgres.assert_awaited_once()


@pytest.mark.asyncio
async def test_load_rehydrates_missing_when_age_has_partial_data():
    kg = KnowledgeGraphAGE()
    db = AsyncMock()
    db.graph_available.return_value = True
    kg._get_db = AsyncMock(return_value=db)
    kg._count_postgres_discoveries = AsyncMock(return_value=5)
    kg._count_age_discoveries = AsyncMock(return_value=3)
    kg._rehydrate_from_postgres = AsyncMock()
    kg._rehydrate_missing_from_postgres = AsyncMock(
        return_value={"discoveries": 2, "related_edges": 1}
    )

    await kg.load()

    kg._rehydrate_from_postgres.assert_not_called()
    kg._rehydrate_missing_from_postgres.assert_awaited_once()


@pytest.mark.asyncio
async def test_load_skips_rehydrate_when_counts_match():
    kg = KnowledgeGraphAGE()
    db = AsyncMock()
    db.graph_available.return_value = True
    kg._get_db = AsyncMock(return_value=db)
    kg._count_postgres_discoveries = AsyncMock(return_value=5)
    kg._count_age_discoveries = AsyncMock(return_value=5)
    kg._rehydrate_from_postgres = AsyncMock()
    kg._rehydrate_missing_from_postgres = AsyncMock()

    await kg.load()

    kg._rehydrate_from_postgres.assert_not_called()
    kg._rehydrate_missing_from_postgres.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_missing_postgres_discovery_rows_excludes_age_ids():
    kg = KnowledgeGraphAGE()
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"id": "pg-only"}])
    db = MagicMock()
    db.graph_query = AsyncMock(return_value=["age-1", "age-2"])

    @asynccontextmanager
    async def acquire():
        yield conn

    db.acquire = acquire
    kg._get_db = AsyncMock(return_value=db)

    rows = await kg._fetch_missing_postgres_discovery_rows()

    assert rows == [{"id": "pg-only"}]
    conn.fetch.assert_awaited_once()
    query, age_ids = conn.fetch.await_args.args
    assert "WHERE NOT (id = ANY($1::text[]))" in query
    assert set(age_ids) == {"age-1", "age-2"}


@pytest.mark.asyncio
async def test_rehydrate_missing_from_postgres_imports_only_missing_rows():
    kg = KnowledgeGraphAGE()
    rows = [{"id": "pg-only"}]
    kg._fetch_missing_postgres_discovery_rows = AsyncMock(return_value=rows)
    kg._import_discovery_row = AsyncMock()

    acquire_conn = MagicMock()
    acquire_conn.fetch = AsyncMock(return_value=[
        {
            "src_id": "pg-only",
            "dst_id": "age-1",
            "weight": 0.8,
            "metadata": {"reason": "test"},
        }
    ])
    tx_conn = MagicMock()
    db = MagicMock()
    db.graph_query = AsyncMock()

    @asynccontextmanager
    async def acquire():
        yield acquire_conn

    @asynccontextmanager
    async def transaction():
        yield tx_conn

    db.acquire = acquire
    db.transaction = transaction
    kg._get_db = AsyncMock(return_value=db)

    result = await kg._rehydrate_missing_from_postgres()

    assert result == {"discoveries": 1, "related_edges": 1}
    kg._import_discovery_row.assert_awaited_once_with(tx_conn, rows[0])
    db.graph_query.assert_awaited_once()


def _fake_db(fetch_results):
    """A db whose acquire() conn returns ``fetch_results`` in order."""
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=list(fetch_results))

    @asynccontextmanager
    async def ctx():
        yield conn

    db = MagicMock()
    db.acquire = ctx
    db.transaction = ctx
    db.graph_query = AsyncMock(return_value=[])
    return db


@pytest.mark.asyncio
async def test_rehydrate_rebuilds_related_edges_held_only_in_related_to_arrays():
    """A postgres-backend install keeps links only in related_to; switching to
    age must still rebuild them as RELATED_TO edges."""
    kg = KnowledgeGraphAGE()
    rows = [
        {"id": "a", "related_to": ["b"]},
        {"id": "b", "related_to": []},
    ]
    db = _fake_db([rows, []])  # discoveries, then no discovery_edges rows
    kg._get_db = AsyncMock(return_value=db)
    kg._import_discovery_row = AsyncMock()

    restored = await kg._rehydrate_from_postgres()

    assert restored == {"discoveries": 2, "related_edges": 1}
    edge_calls = [c for c in db.graph_query.await_args_list if "RELATED_TO" in c.args[0]]
    assert [(c.args[1]["from_id"], c.args[1]["to_id"]) for c in edge_calls] == [("a", "b")]


@pytest.mark.asyncio
async def test_rehydrate_missing_rebuilds_array_links_into_and_out_of_missing_rows():
    kg = KnowledgeGraphAGE()
    missing = [{"id": "new", "related_to": ["old"]}]
    linking = [{"id": "older", "related_to": ["new", "unrelated"]}]
    db = _fake_db([[], linking])  # no discovery_edges rows; one linking row
    kg._get_db = AsyncMock(return_value=db)
    kg._fetch_missing_postgres_discovery_rows = AsyncMock(return_value=missing)
    kg._import_discovery_row = AsyncMock()

    restored = await kg._rehydrate_missing_from_postgres()

    pairs = [(c.args[1]["from_id"], c.args[1]["to_id"]) for c in db.graph_query.await_args_list]
    assert pairs == [("new", "old"), ("older", "new")]
    assert restored == {"discoveries": 1, "related_edges": 2}


def test_related_edges_prefer_edge_rows_and_dedupe_array_entries():
    edges = KnowledgeGraphAGE._related_edges_to_rebuild(
        [{"src_id": "a", "dst_id": "b", "weight": 0.7, "metadata": {"reason": "tags"}}],
        [{"id": "a", "related_to": ["b", "a", "c"]}],
    )
    assert edges == [
        {"from_discovery_id": "a", "to_discovery_id": "b", "strength": 0.7, "reason": "tags"},
        {"from_discovery_id": "a", "to_discovery_id": "c", "strength": None, "reason": None},
    ]
