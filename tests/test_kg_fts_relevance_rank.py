"""Full-text search results must carry their query rank.

The SQL computes `ts_rank_cd(...) as rank`, but TWO layers dropped it before a
consumer could read it. `_dict_to_discovery` builds a `DiscoveryNode` from
named fields and lost it (the half #1759 fixed) — and `_row_to_discovery_dict`
popped `rank` from the row dict, which made the #1759 fix inert in production:
the storage layer read `row.get("rank")` from a dict the mixin had already
stripped. That pop protected nobody — `as rank` appears in exactly one query
in the tree (kg_full_text_search's), so no other caller could ever see the
key — and it is now deleted; rank flows through the row dict deliberately.
Every consumer reading a relevance off these nodes previously saw 0 — and
`_search_kg_by_checkin_text` applies a 0.1 relevance floor, so on the Postgres
backend (which has no `semantic_search`, making full-text the live path) it
discarded every hit and returned [] unconditionally.

The mixin-layer tests here run the REAL `_row_to_discovery_dict`; the original
suite mocked `db.kg_full_text_search` with dicts that still carried "rank",
which validated exactly the premise production violated. Those storage-layer
unit tests are retained below for the attach logic they do cover.
"""

from __future__ import annotations

import asyncio
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.db.mixins.knowledge_graph import KnowledgeGraphMixin
from src.knowledge_graph import DiscoveryNode
from src.mcp_handlers.updates import enrichments as E
from src.storage.knowledge_graph_postgres import KnowledgeGraphPostgres


def _row(did: str = "d1", rank=0.5455):
    row = {
        "id": did,
        "agent_id": "a",
        "type": "note",
        "summary": "coherence gate soak read",
        "tags": ["governance"],
        "status": "open",
    }
    if rank is not None:
        row["rank"] = rank
    return row


def _backend(rows):
    backend = KnowledgeGraphPostgres()
    db = AsyncMock()
    db.kg_full_text_search = AsyncMock(return_value=rows)
    backend._get_db = AsyncMock(return_value=db)
    return backend


class TestFullTextSearchCarriesRank:
    @pytest.mark.asyncio
    async def test_rank_is_attached_as_relevance(self):
        nodes = await _backend([_row(rank=0.5455)]).full_text_search("coherence gate")
        assert nodes[0].relevance == pytest.approx(0.5455)

    @pytest.mark.asyncio
    async def test_ordering_is_preserved(self):
        rows = [_row("hi", 0.97), _row("mid", 0.55), _row("lo", 0.17)]
        nodes = await _backend(rows).full_text_search("coherence gate")
        assert [n.id for n in nodes] == ["hi", "mid", "lo"]
        assert [n.relevance for n in nodes] == pytest.approx([0.97, 0.55, 0.17])

    @pytest.mark.asyncio
    async def test_missing_rank_does_not_raise_or_fabricate(self):
        nodes = await _backend([_row(rank=None)]).full_text_search("coherence gate")
        assert getattr(nodes[0], "relevance", None) is None

    @pytest.mark.asyncio
    async def test_non_numeric_rank_is_ignored(self):
        nodes = await _backend([_row(rank="not-a-number")]).full_text_search("q")
        assert getattr(nodes[0], "relevance", None) is None

    @pytest.mark.asyncio
    async def test_wire_shape_is_unchanged(self):
        """Attached, not added to the dataclass — to_dict() must not grow a key."""
        nodes = await _backend([_row(rank=0.5455)]).full_text_search("coherence gate")
        assert "relevance" not in nodes[0].to_dict(include_details=False)


class _AcquireContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeDb(KnowledgeGraphMixin):
    """Real mixin over a fake connection — the conversion path runs for real."""

    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _AcquireContext(self.conn)


def _pg_row(did: str = "d1", rank=0.5455):
    """A row shaped like what the SELECT * + rank query actually returns."""
    return {
        "id": did,
        "agent_id": "a",
        "type": "note",
        "summary": "coherence gate soak read",
        "tags": ["governance"],
        "status": "open",
        "created_at": datetime.now(timezone.utc),
        "search_vector": "'coherence':1 'gate':2",
        "rank": rank,
    }


class TestMixinPushesTagPredicateIntoRankedQuery:
    """The tag filter has to live inside the ranked query: filtering the top-N
    afterwards drops every tagged row that ranked below N. These run the real
    SQL builder, so an unfiltered query body fails them even if its signature
    accepts the kwarg."""

    @pytest.mark.asyncio
    async def test_tags_add_predicate_and_renumber_limit(self):
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[_pg_row()])
        await _FakeDb(conn).kg_full_text_search("coherence gate", 7, tags=["KG Search", "governance"])
        sql, *params = conn.fetch.await_args.args
        assert "AND tags && $2" in sql
        assert "LIMIT $3" in sql
        assert params[1] == ["kg-search", "governance"]  # normalized exactly as the store path does
        assert params[2] == 7

    @pytest.mark.asyncio
    async def test_without_tags_the_query_is_unchanged(self):
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[_pg_row()])
        await _FakeDb(conn).kg_full_text_search("coherence gate", 7)
        sql, *params = conn.fetch.await_args.args
        assert "tags &&" not in sql
        assert "LIMIT $2" in sql
        assert params[1] == 7


class TestMixinKeepsRankThroughRowConversion:
    """The layer the #1759 tests skipped: `_row_to_discovery_dict` used to pop
    rank, silently severing the SQL→storage carry. These run the real
    conversion, so they fail if any layer starts stripping rank again."""

    @pytest.mark.asyncio
    async def test_rank_survives_the_real_row_conversion(self):
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[_pg_row(rank=0.5455)])
        rows = await _FakeDb(conn).kg_full_text_search("coherence gate")
        assert rows[0]["rank"] == pytest.approx(0.5455)

    @pytest.mark.asyncio
    async def test_wire_strip_still_applies_to_internal_columns(self):
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[_pg_row()])
        rows = await _FakeDb(conn).kg_full_text_search("coherence gate")
        assert "search_vector" not in rows[0]

    @pytest.mark.asyncio
    async def test_null_rank_passes_through_without_fabricating_relevance(self):
        """`ts_rank_cd` can't be NULL for an `@@`-matched row, but the storage
        guard must stay honest if a layer ever hands it one anyway."""
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[_pg_row(rank=None)])
        backend = KnowledgeGraphPostgres()
        backend._get_db = AsyncMock(return_value=_FakeDb(conn))
        nodes = await backend.full_text_search("coherence gate")
        assert getattr(nodes[0], "relevance", None) is None

    @pytest.mark.asyncio
    async def test_storage_pipeline_carries_relevance_end_to_end(self):
        """Storage → real mixin → real row conversion → node.relevance.

        This is the composition that was dead in production while every
        layer's own mocked test stayed green. It must fail if any layer
        between the SQL row and the node drops the rank again.
        """
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[_pg_row(rank=0.5455)])
        backend = KnowledgeGraphPostgres()
        backend._get_db = AsyncMock(return_value=_FakeDb(conn))
        nodes = await backend.full_text_search("coherence gate")
        assert nodes[0].relevance == pytest.approx(0.5455)


class TestCheckinSearchRevives:
    """The regression this fixes, at the consumer that was returning []."""

    def _run(self, node):
        ctx = types.SimpleNamespace(
            response_text="the coherence gate soak read failed today",
            response_data={},
            meta=types.SimpleNamespace(tags=[]),
        )

        class Graph:  # Postgres-shaped: no semantic_search, so FTS is the path
            async def full_text_search(self, q, limit=3, operator="AND"):
                return [node]

        with patch.dict(
            "sys.modules",
            {
                "src.knowledge_graph": MagicMock(
                    get_knowledge_graph=AsyncMock(return_value=Graph())
                )
            },
        ):
            return asyncio.run(E._search_kg_by_checkin_text(ctx))

    def _node(self, relevance=None):
        node = DiscoveryNode(
            id="d1", agent_id="a", type="note", summary="coherence gate soak read"
        )
        if relevance is not None:
            node.relevance = relevance
        return node

    def test_rankless_node_is_still_dropped_by_the_floor(self):
        """Pins the old behaviour so the cause stays legible, not just the cure."""
        assert self._run(self._node()) == []

    def test_ranked_node_now_survives(self):
        surfaced = self._run(self._node(0.5455))
        assert len(surfaced) == 1
        assert surfaced[0]["discovery_id"] == "d1"
        assert surfaced[0]["relevance"] == pytest.approx(0.5455)

    def test_genuinely_weak_rank_is_still_floored(self):
        """The floor stays a degenerate-score guard. 0.02 cannot come from
        normalized FTS (corpus-min raw 0.2 → ~0.167 normalized), but the same
        floor gates the semantic producer on the AGE backend, where post-blend
        scores this weak are real — the guard must still drop them."""
        assert self._run(self._node(0.02)) == []
