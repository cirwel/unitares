"""Archived/cold second pass for short query searches.

KG decision 2026-09-29T04:11:58.480600+00:00 (proposal 1): about two thirds of
the graph is archived or cold, and default search hides both, so a question
whose only answer had aged into cold storage came back empty. A query search
that finds fewer than ARCHIVED_COLD_MIN_RESULTS active rows now runs the same
retrieval again with those statuses included and appends what it finds after
every active row. These tests pin both halves: the cold-only answer comes back
and says how it got there, and nothing changes when the first pass was enough
or the caller said no.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.knowledge_graph import DiscoveryNode
from src.mcp_handlers.knowledge import handlers
from src.mcp_handlers.knowledge.handlers import (
    ARCHIVED_COLD_MIN_RESULTS,
    ARCHIVED_COLD_SCORE_PENALTY,
    ARCHIVED_COLD_TIER,
    handle_search_knowledge_graph,
)


def _node(node_id: str, status: str = "open", summary: str = "dormancy note") -> DiscoveryNode:
    return DiscoveryNode(
        id=node_id,
        agent_id="agent-a",
        type="note",
        summary=summary,
        details="",
        tags=[],
        severity="low",
        status=status,
    )


@pytest.fixture
def graph():
    mock_server = MagicMock()
    mock_server.agent_metadata = {}
    graph = MagicMock()
    graph.query = AsyncMock(return_value=[])
    with patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
         patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server), \
         patch("src.mcp_handlers.knowledge.handlers.mcp_server", mock_server), \
         patch("src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
               new_callable=AsyncMock, return_value=graph), \
         patch("src.mcp_handlers.knowledge.handlers.record_recall_event"), \
         patch("src.mcp_handlers.knowledge.handlers.record_ms"), \
         patch("src.retrieval.hybrid_enabled", return_value=False), \
         patch("src.reranker.reranker_enabled", return_value=False):
        yield graph


def _fts_only(graph, rows):
    """An FTS backend that, like the real one in rank mode, ignores status."""
    graph.full_text_search = AsyncMock(return_value=rows)
    if hasattr(graph, "semantic_search"):
        del graph.semantic_search


def _semantic_only(graph, scored):
    async def semantic_search(query, *, limit, min_similarity, **_):
        return scored[:limit]

    graph.semantic_search = semantic_search
    graph.full_text_search = AsyncMock(return_value=[])


async def _search(arguments):
    result = await handle_search_knowledge_graph(arguments)
    return json.loads(result[0].text)


@pytest.mark.asyncio
async def test_cold_only_answer_comes_back(graph):
    _fts_only(graph, [_node("d-cold", status="cold")])

    data = await _search({"query": "dormancy"})

    assert data["success"] is True
    assert [d["id"] for d in data["discoveries"]] == ["d-cold"]
    assert data["discoveries"][0]["retrieval_tier"] == ARCHIVED_COLD_TIER
    envelope = data[ARCHIVED_COLD_TIER]
    assert envelope["used"] is True
    assert envelope["primary_count"] == 0
    assert envelope["added"] == 1
    assert envelope["statuses_searched"] == ["archived", "cold"]
    assert "include_cold=false" in envelope["note"]
    # A filled page is not an empty search.
    assert "empty_results_hints" not in data


@pytest.mark.asyncio
async def test_second_pass_rows_rank_after_every_active_row(graph):
    # The backend ranks the cold row first; the tier order must still win.
    _fts_only(graph, [
        _node("d-cold", status="cold"),
        _node("d-archived", status="archived"),
        _node("d-open", status="open"),
    ])

    data = await _search({"query": "dormancy"})

    ids = [d["id"] for d in data["discoveries"]]
    assert ids[0] == "d-open"
    assert set(ids[1:]) == {"d-cold", "d-archived"}
    assert "retrieval_tier" not in data["discoveries"][0]
    assert all(d["retrieval_tier"] == ARCHIVED_COLD_TIER for d in data["discoveries"][1:])
    assert data[ARCHIVED_COLD_TIER]["primary_count"] == 1


@pytest.mark.asyncio
async def test_second_pass_scores_carry_the_penalty(graph):
    _semantic_only(graph, [(_node("d-open"), 0.6), (_node("d-cold", status="cold"), 0.9)])

    data = await _search({"query": "dormancy", "search_mode": "semantic"})

    assert [d["id"] for d in data["discoveries"]] == ["d-open", "d-cold"]
    scores = data["similarity_scores"]
    assert scores["d-open"] == pytest.approx(0.6)
    assert scores["d-cold"] == pytest.approx(0.9 * ARCHIVED_COLD_SCORE_PENALTY)


@pytest.mark.asyncio
async def test_enough_active_results_skip_the_second_pass(graph):
    rows = [_node(f"d-open-{i}") for i in range(ARCHIVED_COLD_MIN_RESULTS)]
    _fts_only(graph, rows + [_node("d-cold", status="cold")])

    data = await _search({"query": "dormancy"})

    assert "d-cold" not in [d["id"] for d in data["discoveries"]]
    assert ARCHIVED_COLD_TIER not in data
    assert graph.full_text_search.await_count == 1


@pytest.mark.asyncio
async def test_explicit_false_keeps_that_status_out(graph):
    _fts_only(graph, [_node("d-cold", status="cold"), _node("d-archived", status="archived")])

    data = await _search({"query": "dormancy", "include_cold": False})

    assert [d["id"] for d in data["discoveries"]] == ["d-archived"]
    assert data[ARCHIVED_COLD_TIER]["statuses_searched"] == ["archived"]


@pytest.mark.asyncio
async def test_both_explicit_false_runs_no_second_pass(graph):
    _fts_only(graph, [_node("d-cold", status="cold")])

    data = await _search({"query": "dormancy", "include_cold": "false", "include_archived": False})

    assert data["discoveries"] == []
    assert ARCHIVED_COLD_TIER not in data
    assert graph.full_text_search.await_count == 1


@pytest.mark.asyncio
async def test_explicit_status_filter_is_not_widened(graph):
    _fts_only(graph, [_node("d-cold", status="cold")])

    data = await _search({"query": "dormancy", "status": "open"})

    assert data["discoveries"] == []
    assert ARCHIVED_COLD_TIER not in data


@pytest.mark.asyncio
async def test_queryless_listing_is_not_padded(graph):
    rows = [_node("d-open"), _node("d-cold", status="cold")]

    async def query(**kwargs):
        if kwargs.get("exclude_cold"):
            return [row for row in rows if row.status != "cold"]
        return rows

    graph.query = AsyncMock(side_effect=query)

    data = await _search({"tags": ["anything"]})

    assert "d-cold" not in [d["id"] for d in data["discoveries"]]
    assert ARCHIVED_COLD_TIER not in data


@pytest.mark.asyncio
async def test_second_pass_failure_keeps_the_first_page(graph):
    calls = {"n": 0}

    async def fts(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return [_node("d-open")]
        raise RuntimeError("backend down")

    graph.full_text_search = fts
    if hasattr(graph, "semantic_search"):
        del graph.semantic_search

    data = await _search({"query": "dormancy"})

    assert [d["id"] for d in data["discoveries"]] == ["d-open"]
    assert ARCHIVED_COLD_TIER not in data
    assert "backend down" in data[f"{ARCHIVED_COLD_TIER}_error"]


@pytest.mark.asyncio
async def test_limit_one_with_one_active_row_does_not_widen(graph):
    _fts_only(graph, [_node("d-open"), _node("d-cold", status="cold")])

    data = await _search({"query": "dormancy", "limit": 1})

    assert [d["id"] for d in data["discoveries"]] == ["d-open"]
    assert graph.full_text_search.await_count == 1


def test_widening_rules():
    parse = handlers._parse_knowledge_search_request
    assert handlers._archived_cold_widening(parse({"query": "x"})) == {
        "include_archived": True,
        "include_cold": True,
    }
    assert handlers._archived_cold_widening(parse({"query": "x", "include_cold": True})) == {
        "include_archived": True,
        "include_cold": True,
    }
    assert handlers._archived_cold_widening(
        parse({"query": "x", "include_cold": True, "include_archived": True})
    ) is None
    assert handlers._archived_cold_widening(parse({})) is None
