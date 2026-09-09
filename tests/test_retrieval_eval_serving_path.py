import json
import os
from pathlib import Path

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
async def test_run_query_reports_source_partition_diversity(monkeypatch):
    async def fake_search(arguments):
        return _text_payload({
            "success": True,
            "discoveries": [
                {"id": "native", "tags": ["identity"]},
                {
                    "id": "mirror",
                    "tags": ["source-claude-memory", "host-mac"],
                },
            ],
        })

    monkeypatch.setattr(retrieval_eval, "handle_search_knowledge_graph", fake_search)
    result = await retrieval_eval.run_query("identity", top_k=2)

    assert result.source_diagnostics == {
        "partition_counts": {"native": 1, "source-claude-memory": 1},
        "unique_partitions": 2,
        "dominant_partition_share": 0.5,
        "claude_memory_share": 0.5,
    }


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


@pytest.mark.asyncio
async def test_evaluate_reports_flat_miss_count_and_rate(monkeypatch, tmp_path: Path):
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({
        "schema_version": 1,
        "pairs": [
            {"query": "hit", "relevant_ids": ["target-a"]},
            {"query": "miss", "relevant_ids": ["target-b"]},
        ],
    }))

    async def fake_run_query(query, *_args, **_kwargs):
        if query == "hit":
            return ["noise", "target-a"], [1.0, 0.5], 4.0
        return ["noise"], [1.0], 6.0

    monkeypatch.setattr(retrieval_eval, "run_query", fake_run_query)

    result = await retrieval_eval.evaluate(labels)

    assert [item["flat_miss"] for item in result["per_query"]] == [False, True]
    assert result["aggregate"]["flat_miss_count"] == 1
    assert result["aggregate"]["flat_miss_rate"] == 0.5


class _Node:
    def __init__(self, status):
        self.status = status


def _fake_graph(statuses: dict):
    class _Graph:
        async def get_discovery(self, discovery_id):
            if discovery_id not in statuses:
                return None
            return _Node(statuses[discovery_id])

    async def _get():
        return _Graph()

    return _get


@pytest.mark.asyncio
async def test_label_scope_report_flags_rows_lifecycle_moved_out_of_scope(monkeypatch):
    """An archived label is unreachable by definition, not a ranking failure."""
    import src.knowledge_graph as kg

    monkeypatch.setattr(kg, "get_knowledge_graph", _fake_graph({
        "live": "open",
        "gone": "archived",
        "frozen": "cold",
    }))

    report = await retrieval_eval.label_scope_report([
        {"query": "q1", "relevant_ids": ["live", "gone"]},
        {"query": "q2", "relevant_ids": ["frozen"]},
    ])

    assert report["out_of_scope"] == {"gone": "archived", "frozen": "cold"}
    assert report["out_of_scope_count"] == 2
    assert report["by_status"] == {"archived": 1, "cold": 1, "open": 1}


@pytest.mark.asyncio
async def test_label_scope_report_widens_when_scope_is_widened(monkeypatch):
    import src.knowledge_graph as kg

    monkeypatch.setattr(kg, "get_knowledge_graph", _fake_graph({"gone": "archived"}))

    pairs = [{"query": "q", "relevant_ids": ["gone"]}]
    narrow = await retrieval_eval.label_scope_report(pairs)
    wide = await retrieval_eval.label_scope_report(pairs, include_archived=True)

    assert narrow["out_of_scope_count"] == 1
    assert wide["out_of_scope_count"] == 0


@pytest.mark.asyncio
async def test_a_missing_label_is_not_silently_read_as_reachable(monkeypatch):
    import src.knowledge_graph as kg

    monkeypatch.setattr(kg, "get_knowledge_graph", _fake_graph({}))

    report = await retrieval_eval.label_scope_report(
        [{"query": "q", "relevant_ids": ["deleted"]}]
    )

    assert report["out_of_scope"] == {"deleted": "missing"}


@pytest.mark.asyncio
async def test_evaluate_excludes_a_fully_decayed_query_instead_of_scoring_it_a_miss(
    monkeypatch, tmp_path
):
    """The 2026-09-09 regression: two queries lost their only target to archival.

    Scored as flat misses they dragged recall from 0.567 to 0.167 with no
    change to the retrieval stack. They must leave the aggregate entirely.
    """
    import src.knowledge_graph as kg

    monkeypatch.setattr(kg, "get_knowledge_graph", _fake_graph({
        "reachable": "open",
        "archived-since-labeling": "archived",
    }))

    async def fake_search(arguments):
        return _text_payload({"success": True, "discoveries": [{"id": "reachable"}]})

    monkeypatch.setattr(retrieval_eval, "handle_search_knowledge_graph", fake_search)

    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({
        "schema_version": 1,
        "pairs": [
            {"query": "scorable", "relevant_ids": ["reachable"]},
            {"query": "decayed", "relevant_ids": ["archived-since-labeling"]},
        ],
    }))

    result = await retrieval_eval.evaluate(labels)

    assert result["corpus"]["pair_count"] == 2
    assert result["corpus"]["scored_pair_count"] == 1
    assert result["corpus"]["unscorable_pair_count"] == 1
    assert [q["query"] for q in result["unscorable"]] == ["decayed"]
    # The decayed query must not appear as a miss in the headline numbers.
    assert result["aggregate"]["flat_miss_count"] == 0
    assert result["aggregate"]["flat_miss_rate"] == 0.0
    assert result["label_health"]["out_of_scope_count"] == 1


@pytest.mark.asyncio
async def test_partially_decayed_query_scores_against_what_is_reachable(
    monkeypatch, tmp_path
):
    import src.knowledge_graph as kg

    monkeypatch.setattr(kg, "get_knowledge_graph", _fake_graph({
        "here": "open",
        "gone": "archived",
    }))

    async def fake_search(arguments):
        return _text_payload({"success": True, "discoveries": [{"id": "here"}]})

    monkeypatch.setattr(retrieval_eval, "handle_search_knowledge_graph", fake_search)

    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({
        "schema_version": 1,
        "pairs": [{"query": "q", "relevant_ids": ["here", "gone"]}],
    }))

    result = await retrieval_eval.evaluate(labels)
    q = result["per_query"][0]

    assert result["corpus"]["scored_pair_count"] == 1
    assert q["scored_against_ids"] == ["here"]
    assert q["out_of_scope_ids"] == ["gone"]
    # Recall is 1.0 over the reachable target, not 0.5 over a target that the
    # measured scope cannot return.
    assert q["recall@20"] == 1.0


@pytest.mark.asyncio
async def test_widened_scope_reaches_the_search_not_only_the_classification(monkeypatch):
    """A label counted reachable while the search still drops it is worse than
    the bug it replaces: the query scores a real miss for a bookkeeping reason."""
    calls = []

    async def fake_search(arguments):
        calls.append(dict(arguments))
        return _text_payload({"success": True, "discoveries": []})

    monkeypatch.setattr(retrieval_eval, "handle_search_knowledge_graph", fake_search)

    await retrieval_eval.run_query(
        "q", top_k=2, include_archived=True, include_cold=True
    )

    assert calls[0]["include_archived"] is True
    assert calls[0]["include_cold"] is True


@pytest.mark.asyncio
async def test_a_broken_status_lookup_fails_open_and_never_drops_a_query(monkeypatch):
    """Excluding a query is a strong action; a broken lookup is not evidence.

    Fail-closed here would let a transient backend problem quietly shrink the
    scored set, which reads as a clean run rather than a degraded one.
    """
    import src.knowledge_graph as kg

    class _Graph:
        async def get_discovery(self, discovery_id):
            raise RuntimeError("backend unavailable")

    async def _get():
        return _Graph()

    monkeypatch.setattr(kg, "get_knowledge_graph", _get)

    report = await retrieval_eval.label_scope_report(
        [{"query": "q", "relevant_ids": ["unknowable"]}]
    )

    assert report["out_of_scope"] == {}
    assert report["by_status"] == {"lookup_failed": 1}
