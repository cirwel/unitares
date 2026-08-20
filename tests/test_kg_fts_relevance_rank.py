"""Full-text search results must carry their query rank.

The SQL computes `ts_rank_cd(...) as rank` and `_row_to_discovery_dict` passes
it through, but `_dict_to_discovery` builds a `DiscoveryNode` from named fields
and dropped it. Every consumer reading a relevance off these nodes therefore saw
0 — and `_search_kg_by_checkin_text` applies a 0.1 relevance floor, so on the
Postgres backend (which has no `semantic_search`, making full-text the live
path) it discarded every hit and returned [] unconditionally.
"""

from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
        """The floor must remain a real gate, not become a no-op."""
        assert self._run(self._node(0.02)) == []
