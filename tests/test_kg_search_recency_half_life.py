"""recency_half_life_days: an opt-in recency weight on search relevance.

Operator decision 2026-09-27 (candidate B from the KG retrieval brief): a
per-call parameter, default off. With it, each result's relevance is
multiplied by 0.5 ** (age_days / half_life) before authority ranking, so a
fresh match can overtake an older, slightly stronger one. Without it nothing
changes, which is the half this file checks as carefully as the other.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.knowledge_graph import DiscoveryNode
from src.mcp_handlers.knowledge import handlers
from src.mcp_handlers.knowledge.handlers import (
    _SearchParameterError,
    _parse_knowledge_search_request,
)

NOW = datetime.now(timezone.utc)


def _node(node_id: str, *, age: timedelta, tags=None) -> DiscoveryNode:
    return DiscoveryNode(
        id=node_id,
        agent_id="agent-a",
        type="note",
        summary=node_id,
        tags=tags or [],
        timestamp=(NOW - age).isoformat(),
    )


OLD = _node("old", age=timedelta(days=120))
FRESH = _node("fresh", age=timedelta(days=1))


async def _run(arguments, scored):
    graph = AsyncMock()

    async def semantic_search(query, *, limit, min_similarity, **_):
        return scored[:limit]

    graph.semantic_search = semantic_search
    graph.full_text_search = AsyncMock(return_value=[])
    request = _parse_knowledge_search_request({"search_mode": "semantic", **arguments})
    state = handlers._KnowledgeSearchState(request=request, graph=graph)
    await handlers._run_text_search(state)
    return state


class TestParse:
    def test_default_is_off(self):
        assert _parse_knowledge_search_request({"query": "x"}).recency_half_life_days is None

    @pytest.mark.parametrize("value, expected", [(30, 30.0), ("7.5", 7.5), (36500, 36500.0)])
    def test_accepted_values(self, value, expected):
        request = _parse_knowledge_search_request({"query": "x", "recency_half_life_days": value})
        assert request.recency_half_life_days == expected

    @pytest.mark.parametrize("value", [0, -3, "soon", 36501])
    def test_refused_values(self, value):
        with pytest.raises(_SearchParameterError, match="recency_half_life_days"):
            _parse_knowledge_search_request({"query": "x", "recency_half_life_days": value})

    def test_refused_with_newest_first(self):
        with pytest.raises(_SearchParameterError, match="sort_by='created_at'"):
            _parse_knowledge_search_request(
                {"query": "x", "recency_half_life_days": 30, "sort_by": "created_at"}
            )

    def test_refused_without_a_query(self):
        with pytest.raises(_SearchParameterError, match="needs a query"):
            _parse_knowledge_search_request({"recency_half_life_days": 30})


class TestWeighting:
    @pytest.mark.asyncio
    async def test_off_keeps_relevance_order(self):
        state = await _run({"query": "x", "limit": 2}, [(OLD, 0.80), (FRESH, 0.70)])
        assert [d.id for d in state.results] == ["old", "fresh"]

    @pytest.mark.asyncio
    async def test_a_fresh_match_overtakes_an_older_stronger_one(self):
        # 120 days at a 30-day half-life keeps 1/16 of the score.
        state = await _run(
            {"query": "x", "limit": 2, "recency_half_life_days": 30},
            [(OLD, 0.80), (FRESH, 0.70)],
        )
        assert [d.id for d in state.results] == ["fresh", "old"]

    @pytest.mark.asyncio
    async def test_a_long_half_life_leaves_a_clear_winner_alone(self):
        state = await _run(
            {"query": "x", "limit": 2, "recency_half_life_days": 3650},
            [(OLD, 0.80), (FRESH, 0.70)],
        )
        assert [d.id for d in state.results] == ["old", "fresh"]

    def test_the_weight_is_a_true_half_life(self):
        at_half_life = _node("h", age=timedelta(days=30))
        _, weighted = handlers._recency_weighted([at_half_life], {"h": 1.0}, 30.0)
        assert weighted["h"] == pytest.approx(0.5, abs=1e-3)

    def test_an_undated_row_is_left_unweighted(self):
        undated = DiscoveryNode(id="u", agent_id="a", type="note", summary="u", timestamp="?")
        _, weighted = handlers._recency_weighted([undated], {"u": 0.7}, 30.0)
        assert weighted["u"] == 0.7

    @pytest.mark.asyncio
    async def test_authority_still_applies_on_top(self):
        # Recency alone: fresh imported 0.70 * ~0.98 = 0.68 beats a 20-day-old
        # native 0.77 * 0.63 = 0.49. With authority on top the imported row's
        # 0.55 multiplier (0.37) hands the contest back to the native row.
        fresh_imported = _node("fresh-imported", age=timedelta(days=1), tags=["memory-sync"])
        native = _node("native", age=timedelta(days=20))
        scored = [(fresh_imported, 0.70), (native, 0.77)]
        raw = await _run(
            {"query": "x", "limit": 2, "recency_half_life_days": 30, "authority_mode": "all"},
            scored,
        )
        assert [d.id for d in raw.results] == ["fresh-imported", "native"]
        governed = await _run({"query": "x", "limit": 2, "recency_half_life_days": 30}, scored)
        assert [d.id for d in governed.results] == ["native", "fresh-imported"]

    @pytest.mark.asyncio
    async def test_the_pool_reaches_past_the_first_page(self):
        # authority_mode='all' switches the authority pool off, so only the
        # recency weight can widen retrieval here.
        old_rows = [(_node(f"old{i}", age=timedelta(days=200)), 0.9 - i * 0.01) for i in range(4)]
        scored = [*old_rows, (FRESH, 0.5)]
        state = await _run(
            {"query": "x", "limit": 2, "authority_mode": "all", "recency_half_life_days": 30},
            scored,
        )
        assert state.results[0].id == "fresh"
        without = await _run({"query": "x", "limit": 2, "authority_mode": "all"}, scored)
        assert "fresh" not in [d.id for d in without.results]


@pytest.mark.asyncio
async def test_response_echoes_the_half_life():
    graph = AsyncMock()
    graph.semantic_search = AsyncMock(return_value=[(FRESH, 0.7)])
    graph.full_text_search = AsyncMock(return_value=[])
    request = _parse_knowledge_search_request(
        {"query": "x", "search_mode": "semantic", "recency_half_life_days": 30}
    )
    state = handlers._KnowledgeSearchState(request=request, graph=graph)
    with patch.object(handlers, "_broadcast_knowledge_read", AsyncMock()), \
         patch.object(handlers, "_resolve_agent_display", lambda agent_id: {"display_name": agent_id}):
        response = await handlers._execute_knowledge_search(state)
    assert response["recency_half_life_days"] == 30.0
    assert handlers._lean_search_payload(response)["recency_half_life_days"] == 30.0


def test_schema_declares_it_for_search():
    from src.mcp_handlers.schemas.knowledge import KnowledgeParams

    assert "recency_half_life_days" in KnowledgeParams.ACTION_FIELDS["search"]
    params = KnowledgeParams(action="search", query="x", recency_half_life_days="30")
    assert params.recency_half_life_days == 30.0


def test_the_friendly_alias_envelope_keeps_the_order_echo():
    # Review on #2564: search_shared_memory's envelope lifts a fixed list of
    # search fields into state_summary and bounded modes omit raw_governance,
    # so the echo (this PR's, and #2517's sort_by and window) was dropped.
    from src.mcp_handlers.middleware.envelope_step import build_experience_envelope

    payload = {
        "success": True,
        "count": 1,
        "search_mode_used": "fts_newest_first",
        "discoveries": [{"id": "d1", "summary": "fresh"}],
        "sort_by": "created_at",
        "created_after": "2026-09-26T00:00:00+00:00",
        "created_before": "2026-09-27T00:00:00+00:00",
    }
    env = build_experience_envelope("search_shared_memory", "knowledge", payload)
    assert env["state_summary"]["sort_by"] == "created_at"
    assert env["state_summary"]["created_after"] == "2026-09-26T00:00:00+00:00"
    assert env["state_summary"]["created_before"] == "2026-09-27T00:00:00+00:00"

    weighted = {"success": True, "count": 1, "discoveries": [], "recency_half_life_days": 30.0}
    env = build_experience_envelope("search_shared_memory", "knowledge", weighted)
    assert env["state_summary"]["recency_half_life_days"] == 30.0

    default = {"success": True, "count": 1, "discoveries": [{"id": "d1", "summary": "x"}]}
    env = build_experience_envelope("search_shared_memory", "knowledge", default)
    for key in ("sort_by", "created_after", "created_before", "recency_half_life_days"):
        assert key not in env["state_summary"]
