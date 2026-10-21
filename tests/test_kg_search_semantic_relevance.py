"""Search must not demote a useful answer because its incident was resolved.

Exercise the real AGE semantic and blend implementations through the handler;
only vector/graph storage and query encoding are replaced with fixed inputs.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import embeddings
from src.mcp_handlers.knowledge import handlers
from src.storage.knowledge_graph_age import KnowledgeGraphAGE


def row(docid, *, status="open", days=1, tags=None):
    return {"id": docid, "agent_id": "author", "type": "insight",
            "summary": docid, "details": "retrieval evidence", "tags": tags or [],
            "status": status,
            "timestamp": (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()}


async def run_search(monkeypatch, rows, scores, *, options=None, lexical=()):
    graph = KnowledgeGraphAGE()
    by_id = {r["id"]: r for r in rows}
    graph._pgvector_available = AsyncMock(return_value=True)
    graph._pgvector_search = AsyncMock(return_value=list(scores))
    graph._get_db = AsyncMock(return_value=SimpleNamespace(
        kg_get_discoveries_by_ids=AsyncMock(return_value=by_id)))
    # A weak, recent candidate has many inbound references; that must not turn
    # it into a stronger semantic match than the directly relevant old answer.
    graph.get_connectivity_scores_batch = AsyncMock(return_value={"recent": .9})
    graph.full_text_search = AsyncMock(return_value=[graph._dict_to_discovery(by_id[d]) for d in lexical])
    monkeypatch.setattr(embeddings, "embeddings_available", lambda: True)
    monkeypatch.setattr(embeddings, "get_embeddings_service", AsyncMock(return_value=SimpleNamespace(
        embed=AsyncMock(return_value=[1.0, 0.0]))))
    monkeypatch.setattr("src.retrieval.hybrid_enabled", lambda: True)
    monkeypatch.setattr("src.retrieval.related_expansion_enabled", lambda: False)
    monkeypatch.setattr("src.reranker.reranker_enabled", lambda: False)
    request = handlers._parse_knowledge_search_request({"query": "incident diagnosis", "limit": 10, **(options or {})})
    state = handlers._KnowledgeSearchState(request=request, graph=graph)
    await handlers._run_text_search(state)
    return state, graph


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,query", [
    ("semantic", "incident diagnosis"), ("hybrid", "incident diagnosis"),
    ("auto", "incident diagnosis"),
    ("auto", "Why did the old resolved incident happen despite all of the recent unrelated notes"),
])
async def test_relevance_preserves_old_resolved_answer(monkeypatch, mode, query):
    state, _ = await run_search(
        monkeypatch, [row("answer", status="resolved", days=180), row("recent")],
        [("answer", .88), ("recent", .64)], options={"search_mode": mode, "query": query},
    )
    assert [d.id for d in state.results] == ["answer", "recent"]
    assert state.semantic_scores == {"answer": .88, "recent": .64}


@pytest.mark.asyncio
async def test_explicit_recency_still_promotes_newer_matches(monkeypatch):
    state, _ = await run_search(
        monkeypatch, [row("answer", status="resolved", days=180), row("recent")],
        [("answer", .88), ("recent", .64)],
        options={"search_mode": "semantic", "recency_half_life_days": 30},
    )
    assert [d.id for d in state.results] == ["recent", "answer"]
    # Semantic scores remain cosine; the explicit request adds its weight later.
    assert state.semantic_scores == {"answer": .88, "recent": .64}


@pytest.mark.asyncio
async def test_authority_policy_still_prefers_native_evidence(monkeypatch):
    state, _ = await run_search(
        monkeypatch, [row("imported", tags=["source-bridge-memory"]), row("answer", status="resolved", days=180)],
        [("imported", .95), ("answer", .85)], options={"search_mode": "semantic"},
    )
    assert [d.id for d in state.results] == ["answer", "imported"]
    assert state.authority_reranked is True


@pytest.mark.asyncio
@pytest.mark.parametrize("options,expected", [
    ({}, ["answer"]),
    ({"include_archived": True}, ["archived", "answer"]),
    ({"include_cold": True}, ["cold", "answer"]),
    ({"status": "archived"}, ["archived"]),
])
async def test_lifecycle_visibility_remains_a_filter(monkeypatch, options, expected):
    state, _ = await run_search(
        monkeypatch, [row("archived", status="archived"), row("cold", status="cold"), row("answer", status="resolved")],
        [("archived", .98), ("cold", .95), ("answer", .88)],
        options={"search_mode": "semantic", **options},
    )
    assert [d.id for d in state.results] == expected


@pytest.mark.asyncio
async def test_lexical_fusion_and_similarity_floor_are_preserved(monkeypatch):
    state, graph = await run_search(
        monkeypatch, [row("answer"), row("exact")], [("answer", .88), ("exact", .64)],
        lexical=["exact"], options={"search_mode": "hybrid", "min_similarity": .6},
    )
    assert [d.id for d in state.results] == ["exact", "answer"]
    assert state.search_mode == "hybrid_rrf"
    assert graph._pgvector_search.call_args.kwargs["min_similarity"] == .6
