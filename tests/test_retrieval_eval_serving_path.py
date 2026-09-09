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
    # evaluate() reads label scope before it scores, so the graph has to be
    # stubbed even for a test that only cares about flat-miss accounting.
    # Left real, it reaches the isolated db backend and leaks an unawaited
    # AsyncMock coroutine, which the session-level guard in conftest fails on.
    import src.knowledge_graph as kg

    monkeypatch.setattr(kg, "get_knowledge_graph", _fake_graph({}))

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


def _labels(tmp_path, pairs):
    f = tmp_path / "labels.json"
    f.write_text(json.dumps({"schema_version": 1, "pairs": pairs}))
    return f


@pytest.mark.asyncio
async def test_label_scope_report_names_rows_lifecycle_moved_out_of_scope(monkeypatch):
    import src.knowledge_graph as kg

    monkeypatch.setattr(kg, "get_knowledge_graph", _fake_graph({
        "live": "open", "gone": "archived", "frozen": "cold",
    }))

    report = await retrieval_eval.label_scope_report([
        {"query": "q1", "relevant_ids": ["live", "gone"]},
        {"query": "q2", "relevant_ids": ["frozen"]},
    ])

    assert report["out_of_scope"] == {"gone": "archived", "frozen": "cold"}
    assert report["by_status"] == {"archived": 1, "cold": 1, "open": 1}


@pytest.mark.asyncio
async def test_an_unrecognised_status_is_never_called_decay(monkeypatch):
    """The serving predicate excludes only archived and cold.

    The first cut kept a parallel allowlist of reachable statuses, so a status
    nobody had thought of was reported as decay with no flag to recover it —
    the same false signal this reporting exists to expose, only quieter.
    """
    import src.knowledge_graph as kg

    monkeypatch.setattr(kg, "get_knowledge_graph", _fake_graph({
        "novel": "triaged", "cased": "Archived", "live": "open",
    }))

    report = await retrieval_eval.label_scope_report(
        [{"query": "q", "relevant_ids": ["novel", "cased", "live"]}]
    )

    assert report["out_of_scope"] == {}, "only archived/cold are out of scope"
    assert report["out_of_scope_count"] == 0


@pytest.mark.asyncio
async def test_scope_opt_ins_clear_the_matching_status(monkeypatch):
    import src.knowledge_graph as kg

    monkeypatch.setattr(kg, "get_knowledge_graph", _fake_graph({
        "a": "archived", "c": "cold",
    }))
    pairs = [{"query": "q", "relevant_ids": ["a", "c"]}]

    narrow = await retrieval_eval.label_scope_report(pairs)
    half = await retrieval_eval.label_scope_report(pairs, include_archived=True)
    wide = await retrieval_eval.label_scope_report(
        pairs, include_archived=True, include_cold=True
    )

    assert narrow["out_of_scope_count"] == 2
    assert half["out_of_scope"] == {"c": "cold"}
    assert wide["out_of_scope_count"] == 0


@pytest.mark.asyncio
async def test_missing_and_failed_lookups_are_undetermined_not_decay(monkeypatch):
    """A vanished row or a broken lookup is not a lifecycle decision.

    Calling them decay let a run against the wrong database read as an orderly
    corpus that had simply aged.
    """
    import src.knowledge_graph as kg

    class _Graph:
        async def get_discovery(self, discovery_id):
            if discovery_id == "boom":
                raise RuntimeError("backend unavailable")
            return None

    async def _get():
        return _Graph()

    monkeypatch.setattr(kg, "get_knowledge_graph", _get)

    report = await retrieval_eval.label_scope_report(
        [{"query": "q", "relevant_ids": ["vanished", "boom"]}]
    )

    assert report["out_of_scope"] == {}
    assert report["undetermined"] == {"vanished": "missing", "boom": "lookup_failed"}
    assert report["undetermined_count"] == 2


@pytest.mark.asyncio
async def test_decay_never_changes_the_denominator(monkeypatch, tmp_path):
    """Narrowing per-query ground truth made the aggregate RISE as labels aged.

    On the weekly gate's 5-query sample that would have cut n to 3 and left
    every survivor with one target, so recall climbed with no change to the
    ranker. A false improvement is not a fix for a false regression.
    """
    import src.knowledge_graph as kg

    monkeypatch.setattr(kg, "get_knowledge_graph", _fake_graph({
        "here": "open", "gone": "archived",
    }))

    async def fake_search(arguments):
        return _text_payload({"success": True, "discoveries": [{"id": "here"}]})

    monkeypatch.setattr(retrieval_eval, "handle_search_knowledge_graph", fake_search)

    labels = _labels(tmp_path, [{"query": "q", "relevant_ids": ["here", "gone"]}])
    result = await retrieval_eval.evaluate(labels)
    q = result["per_query"][0]

    assert result["corpus"]["scored_pair_count"] == 1
    # Both targets stay in the denominator: 1 of 2 retrieved.
    assert q["recall@20"] == 0.5
    # The decay is named so the miss is readable, without moving the score.
    assert q["out_of_scope_ids"] == ["gone"]
    assert result["aggregate"]["labels_out_of_scope"] == 1


@pytest.mark.asyncio
async def test_a_fully_decayed_query_is_still_scored_and_still_counted(
    monkeypatch, tmp_path
):
    import src.knowledge_graph as kg

    monkeypatch.setattr(kg, "get_knowledge_graph", _fake_graph({"gone": "archived"}))

    async def fake_search(arguments):
        return _text_payload({"success": True, "discoveries": [{"id": "other"}]})

    monkeypatch.setattr(retrieval_eval, "handle_search_knowledge_graph", fake_search)

    labels = _labels(tmp_path, [{"query": "q", "relevant_ids": ["gone"]}])
    result = await retrieval_eval.evaluate(labels)

    assert result["corpus"]["scored_pair_count"] == 1
    assert result["aggregate"]["flat_miss_count"] == 1
    assert result["aggregate"]["labels_out_of_scope"] == 1


@pytest.mark.asyncio
async def test_decay_counts_ride_inside_aggregate_where_the_gate_can_see_them(
    monkeypatch, tmp_path
):
    """The weekly gate logs `aggregate` and nothing else.

    Decay reported anywhere outside that dict reaches no reader that exists.
    """
    import src.knowledge_graph as kg

    monkeypatch.setattr(kg, "get_knowledge_graph", _fake_graph({"gone": "archived"}))

    async def fake_search(arguments):
        return _text_payload({"success": True, "discoveries": []})

    monkeypatch.setattr(retrieval_eval, "handle_search_knowledge_graph", fake_search)

    labels = _labels(tmp_path, [{"query": "q", "relevant_ids": ["gone"]}])
    agg = (await retrieval_eval.evaluate(labels))["aggregate"]

    assert agg["labels_out_of_scope"] == 1
    assert agg["labels_undetermined"] == 0
    assert agg["labels_total"] == 1


@pytest.mark.asyncio
async def test_zero_scored_reports_no_metrics_rather_than_zero_metrics(
    monkeypatch, tmp_path, capsys
):
    """An empty labels file used to raise KeyError in print_human, while the
    --json path emitted flat_miss_rate 0.0 — a broken run recorded as clean."""
    import src.knowledge_graph as kg

    monkeypatch.setattr(kg, "get_knowledge_graph", _fake_graph({}))

    labels = _labels(tmp_path, [])
    result = await retrieval_eval.evaluate(labels)

    assert result["corpus"]["scored_pair_count"] == 0
    assert result["aggregate"]["flat_miss_rate"] is None, "0.0 would read as a clean run"

    retrieval_eval.print_human(result)  # must not raise
    assert "NO QUERIES SCORED" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_non_string_label_ids_do_not_abort_the_run(monkeypatch, tmp_path):
    """sorted() over mixed str/int/None raises TypeError before any query runs."""
    import src.knowledge_graph as kg

    monkeypatch.setattr(kg, "get_knowledge_graph", _fake_graph({}))

    async def fake_search(arguments):
        return _text_payload({"success": True, "discoveries": []})

    monkeypatch.setattr(retrieval_eval, "handle_search_knowledge_graph", fake_search)

    labels = _labels(tmp_path, [{"query": "q", "relevant_ids": [123, None, "ok"]}])
    result = await retrieval_eval.evaluate(labels)

    assert result["corpus"]["scored_pair_count"] == 1


@pytest.mark.asyncio
async def test_scope_flags_are_forwarded_to_the_search_arguments(monkeypatch):
    """Argument wiring only. This CANNOT prove the handler honours them.

    Named for what it checks: review found a handler branch
    (_candidate_matches_semantic_fallback) that reads include_archived and
    never checks cold at all, which a mocked handler can never surface.
    """
    calls = []

    async def fake_search(arguments):
        calls.append(dict(arguments))
        return _text_payload({"success": True, "discoveries": []})

    monkeypatch.setattr(retrieval_eval, "handle_search_knowledge_graph", fake_search)

    await retrieval_eval.run_query("q", top_k=2, include_archived=True, include_cold=True)

    assert calls[0]["include_archived"] is True
    assert calls[0]["include_cold"] is True
