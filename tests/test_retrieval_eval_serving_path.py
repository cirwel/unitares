import json
import os

import pytest
from mcp.types import TextContent

from scripts.eval import retrieval_eval


def _text_payload(payload: dict):
    return [TextContent(type="text", text=json.dumps(payload))]


@pytest.mark.asyncio
async def test_run_query_uses_serving_search_handler(monkeypatch):
    calls = []

    async def fake_search(arguments):
        calls.append(dict(arguments))
        return _text_payload({
            "success": True,
            "discoveries": [{"id": "disc-a"}, {"id": "disc-b"}],
            "similarity_scores": {"disc-a": 0.72},
        })

    monkeypatch.setattr(retrieval_eval, "handle_search_knowledge_graph", fake_search)

    ranked, scores, dt_ms = await retrieval_eval.run_query("identity bug", top_k=2)

    assert calls == [{"query": "identity bug", "limit": 2}]
    assert ranked == ["disc-a", "disc-b"]
    assert scores == [0.72, 0.5]
    assert dt_ms >= 0.0


@pytest.mark.asyncio
async def test_run_query_maps_cli_flags_to_serving_handler(monkeypatch):
    observed = {}

    async def fake_search(arguments):
        observed["arguments"] = dict(arguments)
        observed["env"] = {
            "UNITARES_ENABLE_HYBRID": os.environ.get("UNITARES_ENABLE_HYBRID"),
            "UNITARES_ENABLE_GRAPH_EXPANSION": os.environ.get("UNITARES_ENABLE_GRAPH_EXPANSION"),
            "UNITARES_ENABLE_RERANKER": os.environ.get("UNITARES_ENABLE_RERANKER"),
        }
        return _text_payload({
            "success": True,
            "discoveries": [{"id": "disc-a"}],
            "rrf_scores": {"disc-a": 0.0312},
        })

    monkeypatch.setattr(retrieval_eval, "handle_search_knowledge_graph", fake_search)

    ranked, scores, _ = await retrieval_eval.run_query(
        "retrieval eval",
        top_k=1,
        rerank=True,
        hybrid=True,
        graph_expand=True,
    )

    assert observed["arguments"] == {
        "query": "retrieval eval",
        "limit": 50,
        "search_mode": "hybrid",
    }
    assert observed["env"] == {
        "UNITARES_ENABLE_HYBRID": "1",
        "UNITARES_ENABLE_GRAPH_EXPANSION": "1",
        "UNITARES_ENABLE_RERANKER": "1",
    }
    assert ranked == ["disc-a"]
    assert scores == [0.0312]


@pytest.mark.asyncio
async def test_run_query_restores_existing_env(monkeypatch):
    monkeypatch.setenv("UNITARES_ENABLE_HYBRID", "operator-value")

    async def fake_search(arguments):
        assert os.environ["UNITARES_ENABLE_HYBRID"] == "1"
        return _text_payload({"success": True, "discoveries": []})

    monkeypatch.setattr(retrieval_eval, "handle_search_knowledge_graph", fake_search)

    await retrieval_eval.run_query("retrieval eval", top_k=1, hybrid=True)

    assert os.environ["UNITARES_ENABLE_HYBRID"] == "operator-value"
