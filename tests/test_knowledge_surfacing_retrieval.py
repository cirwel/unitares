"""Knowledge surfacing: payload shape and retrieval key.

Two independent defects made `memory_suggestions` unreachable from its only
producer:

  1. shape — the enrichment emits {"message": ..., "discoveries": [...]} while
     both readers required a bare list and dropped it on an isinstance check.
  2. key — retrieval intersected agent tags with discovery tags, but agent tags
     are lifecycle values and discovery tags are topical, so the vocabularies
     do not overlap.

These tests pin both fixes.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_handlers.middleware.envelope_step import _memory_suggestions
from src.mcp_handlers.response_formatter import normalize_discovery_list
from src.mcp_handlers.updates.enrichments import (
    _distinctive_terms,
    enrich_knowledge_surfacing,
)


def _discovery(did: str, summary: str, tags=None, status: str = "open"):
    disc = MagicMock()
    disc.tags = tags or []
    disc.status = status
    disc.to_dict = MagicMock(
        return_value={"id": did, "summary": summary, "tags": disc.tags}
    )
    return disc


class TestNormalizeDiscoveryList:
    def test_unwraps_the_producer_dict_shape(self):
        value = {"message": "Found 1", "discoveries": [{"id": "d1"}]}
        assert normalize_discovery_list(value) == [{"id": "d1"}]

    def test_passes_through_a_bare_list(self):
        assert normalize_discovery_list([{"id": "d1"}]) == [{"id": "d1"}]

    @pytest.mark.parametrize("value", [None, "nope", 7, {}, {"discoveries": None}])
    def test_returns_empty_for_anything_else(self, value):
        assert normalize_discovery_list(value) == []

    def test_drops_non_dict_members(self):
        assert normalize_discovery_list([{"id": "d1"}, "junk", None]) == [{"id": "d1"}]


class TestMemorySuggestionsRegression:
    def test_populates_from_the_producer_dict_shape(self):
        """The actual bug: this returned None, so the field never appeared."""
        payload = {
            "relevant_discoveries": {
                "message": "Found 2",
                "discoveries": [
                    {"discovery_id": "d1", "summary": "prior art"},
                    {"discovery_id": "d2", "summary": "more prior art"},
                ],
            }
        }
        suggestions = _memory_suggestions(payload)
        assert suggestions is not None
        assert [s["discovery_id"] for s in suggestions] == ["d1", "d2"]

    def test_bare_list_still_works(self):
        payload = {"relevant_discoveries": [{"discovery_id": "d1", "summary": "s"}]}
        assert _memory_suggestions(payload)[0]["discovery_id"] == "d1"

    def test_absent_key_still_returns_none(self):
        assert _memory_suggestions({}) is None


class TestDistinctiveTerms:
    def test_drops_stopwords_and_short_tokens(self):
        assert _distinctive_terms("the coherence of a gate is with that") == [
            "coherence",
            "gate",
        ]

    def test_dedupes_preserving_order(self):
        assert _distinctive_terms("lease lease deadlock lease") == ["lease", "deadlock"]

    def test_is_bounded(self):
        text = " ".join(f"token{i}" for i in range(50))
        assert len(_distinctive_terms(text)) == 8

    @pytest.mark.parametrize("text", ["", "   ", None, 42])
    def test_empty_for_unusable_input(self, text):
        assert _distinctive_terms(text) == []


class TestEnrichKnowledgeSurfacing:
    def _ctx(self, response_text="", tags=None):
        return SimpleNamespace(
            response_text=response_text,
            meta=SimpleNamespace(tags=tags or []),
            response_data={},
        )

    async def _run(self, ctx, graph):
        with patch.dict(
            "sys.modules",
            {
                "src.knowledge_graph": MagicMock(
                    get_knowledge_graph=AsyncMock(return_value=graph)
                )
            },
        ):
            await enrich_knowledge_surfacing(ctx)

    @pytest.mark.asyncio
    async def test_retrieves_on_check_in_text_not_tags(self):
        graph = AsyncMock()
        graph.full_text_search = AsyncMock(
            return_value=[_discovery("d1", "coherence gate soak")]
        )
        graph.query = AsyncMock(return_value=[])
        ctx = self._ctx(response_text="investigating the coherence gate soak read")

        await self._run(ctx, graph)

        assert ctx.response_data["relevant_discoveries"]["match_basis"] == (
            "your check-in text"
        )
        graph.query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_to_or_when_and_finds_nothing(self):
        graph = AsyncMock()
        graph.full_text_search = AsyncMock(
            side_effect=[[], [_discovery("d1", "partial match")]]
        )
        graph.query = AsyncMock(return_value=[])
        ctx = self._ctx(response_text="coherence gate soak")

        await self._run(ctx, graph)

        assert graph.full_text_search.await_count == 2
        assert graph.full_text_search.await_args_list[0].kwargs["operator"] == "AND"
        assert graph.full_text_search.await_args_list[1].kwargs["operator"] == "OR"
        assert len(ctx.response_data["relevant_discoveries"]["discoveries"]) == 1

    @pytest.mark.asyncio
    async def test_falls_back_to_tags_when_text_yields_nothing(self):
        graph = AsyncMock()
        graph.full_text_search = AsyncMock(return_value=[])
        graph.query = AsyncMock(
            return_value=[_discovery("d1", "tagged", tags=["governance"])]
        )
        ctx = self._ctx(response_text="coherence gate", tags=["governance"])

        await self._run(ctx, graph)

        assert ctx.response_data["relevant_discoveries"]["match_basis"] == "your tags"

    @pytest.mark.asyncio
    async def test_caps_at_three(self):
        graph = AsyncMock()
        graph.full_text_search = AsyncMock(
            return_value=[_discovery(f"d{i}", f"s{i}") for i in range(9)]
        )
        ctx = self._ctx(response_text="coherence gate soak")

        await self._run(ctx, graph)

        assert len(ctx.response_data["relevant_discoveries"]["discoveries"]) == 3

    @pytest.mark.asyncio
    async def test_excludes_non_open_discoveries(self):
        graph = AsyncMock()
        graph.full_text_search = AsyncMock(
            return_value=[_discovery("d1", "archived one", status="archived")]
        )
        graph.query = AsyncMock(return_value=[])
        ctx = self._ctx(response_text="coherence gate soak")

        await self._run(ctx, graph)

        assert "relevant_discoveries" not in ctx.response_data

    @pytest.mark.asyncio
    async def test_failure_is_marked_not_swallowed_silently(self):
        graph = AsyncMock()
        graph.full_text_search = AsyncMock(side_effect=RuntimeError("kg down"))
        ctx = self._ctx(response_text="coherence gate soak")

        await self._run(ctx, graph)

        assert ctx.response_data["knowledge_surfacing_degraded"] is True
        assert "relevant_discoveries" not in ctx.response_data

    @pytest.mark.asyncio
    async def test_no_text_and_no_tags_is_a_quiet_no_op(self):
        graph = AsyncMock()
        graph.full_text_search = AsyncMock(return_value=[])
        graph.query = AsyncMock(return_value=[])
        ctx = self._ctx()

        await self._run(ctx, graph)

        assert ctx.response_data == {}
