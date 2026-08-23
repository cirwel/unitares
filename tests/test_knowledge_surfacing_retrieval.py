"""Knowledge surfacing: payload shape, retrieval delegation, degradation.

`memory_suggestions` could never be populated from its only producer. The
breakages were independent and each was sufficient on its own:

  * every formatter rebuilt `result` from a passthrough allowlist that omitted
    `relevant_discoveries`, so the key never reached a response;
  * `_strip_context` also popped it for established agents;
  * the producer emits {"message": ..., "discoveries": [...]} while both readers
    required a bare list and dropped it on an isinstance check;
  * retrieval intersected agent tags with discovery tags, and those vocabularies
    do not meet — recent identity tags are lifecycle values, discovery tags are
    topical;
  * a failed lookup was logged at debug and left the key absent, so "broken" and
    "nothing relevant" read identically.

Retrieval now delegates to the shared `_search_kg_by_checkin_text`, which is
semantic-first, applies the relevance floor, and — the part that matters on the
check-in path — budgets every KG call with `_KG_SEARCH_TIMEOUT`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import asyncio

import pytest

from src.mcp_handlers.middleware.envelope_step import _memory_suggestions
from src.mcp_handlers.response_formatter import normalize_discovery_list
from src.mcp_handlers.updates.enrichments import (
    _cached_kg_search_by_checkin_text,
    _distinctive_terms,
    _full_text_search_with_recall,
    _mark_surfacing_degraded,
    _search_kg_by_checkin_text,
    _topical_agent_tags,
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


class TestTopicalAgentTags:
    def test_drops_identity_and_lifecycle_classes(self):
        assert _topical_agent_tags(
            ["ephemeral", "engaged_ephemeral", "persistent", "autonomous"]
        ) == []

    def test_retains_caller_supplied_topics(self):
        assert _topical_agent_tags(["ephemeral", "knowledge-graph", "testing"]) == [
            "knowledge-graph",
            "testing",
        ]


class TestEnrichKnowledgeSurfacing:
    """The enrichment now delegates retrieval to the shared, timeout-bounded
    `_search_kg_by_checkin_text` rather than issuing its own KG calls, so these
    exercise the delegation seam and the tag fallback behind it."""

    def _ctx(self, response_text="", tags=None):
        return SimpleNamespace(
            response_text=response_text,
            meta=SimpleNamespace(tags=tags or []),
            response_data={},
        )

    @pytest.mark.asyncio
    async def test_prefers_check_in_text_and_does_not_touch_tags(self):
        ctx = self._ctx(response_text="the coherence gate soak read", tags=["governance"])
        found = [{"discovery_id": "d1", "summary": "coherence gate soak", "relevance": 0.4}]
        with patch(
            "src.mcp_handlers.updates.enrichments._search_kg_by_checkin_text",
            AsyncMock(return_value=found),
        ) as search:
            await enrich_knowledge_surfacing(ctx)

        search.assert_awaited_once()
        surfaced = ctx.response_data["relevant_discoveries"]
        assert surfaced["match_basis"] == "your check-in text"
        assert surfaced["discoveries"] == found

    @pytest.mark.asyncio
    async def test_falls_back_to_tags_when_text_finds_nothing(self):
        ctx = self._ctx(response_text="coherence gate", tags=["governance"])
        graph = AsyncMock()
        graph.query = AsyncMock(
            return_value=[_discovery("d1", "tagged", tags=["governance"])]
        )
        with patch(
            "src.mcp_handlers.updates.enrichments._search_kg_by_checkin_text",
            AsyncMock(return_value=[]),
        ):
            with patch.dict(
                "sys.modules",
                {
                    "src.knowledge_graph": MagicMock(
                        get_knowledge_graph=AsyncMock(return_value=graph)
                    )
                },
            ):
                await enrich_knowledge_surfacing(ctx)

        assert ctx.response_data["relevant_discoveries"]["match_basis"] == "your tags"

    @pytest.mark.asyncio
    async def test_does_not_fall_back_to_lifecycle_tags(self):
        ctx = self._ctx(response_text="coherence gate", tags=["ephemeral"])
        get_graph = AsyncMock()
        with patch(
            "src.mcp_handlers.updates.enrichments._search_kg_by_checkin_text",
            AsyncMock(return_value=[]),
        ):
            with patch.dict(
                "sys.modules",
                {"src.knowledge_graph": MagicMock(get_knowledge_graph=get_graph)},
            ):
                await enrich_knowledge_surfacing(ctx)

        get_graph.assert_not_awaited()
        assert ctx.response_data == {}

    @pytest.mark.asyncio
    async def test_caps_at_three(self):
        ctx = self._ctx(response_text="coherence gate soak")
        found = [{"discovery_id": f"d{i}", "summary": f"s{i}"} for i in range(9)]
        with patch(
            "src.mcp_handlers.updates.enrichments._search_kg_by_checkin_text",
            AsyncMock(return_value=found),
        ):
            await enrich_knowledge_surfacing(ctx)

        assert len(ctx.response_data["relevant_discoveries"]["discoveries"]) == 3

    @pytest.mark.asyncio
    async def test_tag_fallback_timeout_is_marked_not_silent(self):
        ctx = self._ctx(response_text="coherence gate", tags=["governance"])
        graph = AsyncMock()
        graph.query = AsyncMock(side_effect=asyncio.TimeoutError())
        with patch(
            "src.mcp_handlers.updates.enrichments._search_kg_by_checkin_text",
            AsyncMock(return_value=[]),
        ):
            with patch.dict(
                "sys.modules",
                {
                    "src.knowledge_graph": MagicMock(
                        get_knowledge_graph=AsyncMock(return_value=graph)
                    )
                },
            ):
                await enrich_knowledge_surfacing(ctx)

        assert ctx.response_data["knowledge_surfacing_degraded"] is True
        assert "relevant_discoveries" not in ctx.response_data

    @pytest.mark.asyncio
    async def test_no_text_and_no_tags_is_a_quiet_no_op(self):
        ctx = self._ctx()
        with patch(
            "src.mcp_handlers.updates.enrichments._search_kg_by_checkin_text",
            AsyncMock(return_value=[]),
        ):
            await enrich_knowledge_surfacing(ctx)

        assert ctx.response_data == {}


class TestSurfacingDegradedMarker:
    def test_marker_is_set_on_response_data(self):
        ctx = SimpleNamespace(response_data={})
        _mark_surfacing_degraded(ctx)
        assert ctx.response_data["knowledge_surfacing_degraded"] is True

    def test_marker_never_raises_on_a_bad_ctx(self):
        _mark_surfacing_degraded(SimpleNamespace(response_data=None))


class TestFullTextRecall:
    @pytest.mark.asyncio
    async def test_falls_back_from_and_to_or(self):
        graph = AsyncMock()
        graph.full_text_search = AsyncMock(
            side_effect=[[], [{"discovery_id": "d1", "relevance": 0.4}]]
        )

        result = await _full_text_search_with_recall(
            graph, "coherence gate soak recovery"
        )

        assert result[0]["discovery_id"] == "d1"
        assert graph.full_text_search.await_count == 2
        assert graph.full_text_search.await_args_list[1].kwargs["operator"] == "OR"

    @pytest.mark.asyncio
    async def test_does_not_issue_or_when_and_matches(self):
        graph = AsyncMock()
        graph.full_text_search = AsyncMock(
            return_value=[{"discovery_id": "d1", "relevance": 0.4}]
        )

        await _full_text_search_with_recall(graph, "coherence gate soak recovery")

        graph.full_text_search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_and_or_sequence_shares_one_timeout_budget(self):
        class Graph:
            async def full_text_search(self, query, limit=3, operator="AND"):
                if operator == "AND":
                    return []
                await asyncio.sleep(5)
                return [{"discovery_id": "late", "relevance": 0.5}]

        ctx = SimpleNamespace(
            response_text="coherence gate soak recovery",
            response_data={},
        )
        with patch(
            "src.knowledge_graph.get_knowledge_graph",
            AsyncMock(return_value=Graph()),
        ), patch(
            "src.mcp_handlers.updates.enrichments._KG_SEARCH_TIMEOUT", 0.05
        ):
            started = asyncio.get_running_loop().time()
            result = await _search_kg_by_checkin_text(ctx)
            elapsed = asyncio.get_running_loop().time() - started

        assert result == []
        assert elapsed < 1
        assert ctx.response_data["knowledge_surfacing_degraded"] is True


class TestCheckinSearchCache:
    @pytest.mark.asyncio
    async def test_reuses_one_lookup_for_multiple_enrichments(self):
        ctx = SimpleNamespace(_kg_search_ready=False, _kg_search_results=[])
        found = [{"discovery_id": "d1", "relevance": 0.4}]
        with patch(
            "src.mcp_handlers.updates.enrichments._search_kg_by_checkin_text",
            AsyncMock(return_value=found),
        ) as search:
            first = await _cached_kg_search_by_checkin_text(ctx)
            second = await _cached_kg_search_by_checkin_text(ctx)

        assert first == found
        assert second == found
        search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_applies_a_stricter_floor_without_another_lookup(self):
        ctx = SimpleNamespace(_kg_search_ready=False, _kg_search_results=[])
        found = [
            {"discovery_id": "weak", "relevance": 0.2},
            {"discovery_id": "strong", "relevance": 0.5},
        ]
        with patch(
            "src.mcp_handlers.updates.enrichments._search_kg_by_checkin_text",
            AsyncMock(return_value=found),
        ) as search:
            result = await _cached_kg_search_by_checkin_text(ctx, floor=0.35)

        assert [entry["discovery_id"] for entry in result] == ["strong"]
        search.assert_awaited_once()
