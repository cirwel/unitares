"""Guard the comparison against optimistic scoring and mismatched corpora."""
import json
from argparse import Namespace
from pathlib import Path
import statistics

import numpy as np
import pytest

from scripts.eval.kg_embedding_compare import (
    digest, embed_text, fuse, known_item_metrics, load_inputs, score, write_json,
)


def test_kg_embedding_compare_misses_and_beyond_cutoff():
    missing = known_item_metrics(["other"], {"target"})
    assert missing["rank"] is None
    assert missing["mrr@10"] == missing["hit@10"] == missing["recall@20"] == 0
    late = known_item_metrics([str(i) for i in range(10)] + ["target"], {"target"})
    assert late["rank"] == 11
    assert late["mrr@10"] == late["ndcg@10"] == late["hit@10"] == 0
    assert late["recall@20"] == 1


def test_kg_embedding_compare_partial_relevance_keeps_denominator():
    result = known_item_metrics(["a", "x"], {"a", "b"})
    assert result["recall@20"] == .5
    assert 0 < result["ndcg@10"] < 1


def test_kg_embedding_compare_text_boundary_and_fusion():
    assert embed_text({"summary": "s", "details": "a" * 6000 + "hidden"}) == "s\n" + "a" * 6000
    assert fuse(["a", "b"], ["b", "c"])[0] == "b"
    assert fuse(["a", "b"], [], pool=1) == ["a"]


def test_kg_embedding_compare_archived_target_refused(tmp_path):
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(json.dumps({"id": "a", "status": "open"}) + "\n" +
                      json.dumps({"id": "b", "status": "archived"}))
    labels = tmp_path / "labels.json"
    write_json(labels, {"pairs": [{"query": "q", "relevant_ids": ["b"]}]})
    with pytest.raises(ValueError, match="missing/out-of-scope"):
        load_inputs(corpus, labels)


def test_kg_embedding_compare_score_and_cache_validation(tmp_path):
    corpus, labels, fts, vectors, output = [tmp_path / name for name in
        ["corpus.jsonl", "labels.json", "fts.json", "bge.npz", "results.json"]]
    corpus.write_text('\n'.join(json.dumps({"id": i, "status": "open"}) for i in ["a", "b"]))
    write_json(labels, {"pairs": [{"query_id": "q", "topic": "t", "origin": "logged",
                                  "query": "q", "relevant_ids": ["b"]}]})
    hashes = {"corpus_sha256": digest(corpus), "labels_sha256": digest(labels)}
    write_json(fts, {**hashes, "queries": [{"AND": [], "OR": [{"id": "b", "score": .2}]}]})
    np.savez_compressed(vectors, documents=np.eye(2), queries=np.array([[0., 1.]]))
    meta = {**hashes, "ids": ["a", "b"], "model": "bge", "query_ms": [1., 2.]}
    write_json(vectors.with_suffix(".json"), meta)
    args = Namespace(corpus=corpus, labels=labels, fts=fts, vectors=[vectors], output=output)
    score(args)
    result = json.loads(output.read_text())
    assert result["methods"]["bge_dense"]["aggregate"]["all"]["mrr@10"] == 1
    assert result["methods"]["fts_and_then_or"]["aggregate"]["logged"]["hit@1"] == 1
    assert result["fts_and_empty"] == 1
    meta["ids"] = ["b", "a"]
    write_json(vectors.with_suffix(".json"), meta)
    with pytest.raises(ValueError, match="order mismatch"):
        score(args)
    meta["ids"] = ["a", "b"]
    meta["labels_sha256"] = "wrong"
    write_json(vectors.with_suffix(".json"), meta)
    with pytest.raises(ValueError, match="labels/order mismatch"):
        score(args)


def test_kg_embedding_compare_published_evidence_matches_ranks():
    root = Path(__file__).parent / "retrieval_eval" / "embedding_compare"
    labels = json.loads((root / "labels.json").read_text())["pairs"]
    report = json.loads((root / "results_2026-10-14.json").read_text())
    snapshot = json.loads((root / "snapshot_2026-10-14.json").read_text())
    assert report["labels_sha256"] == digest(root / "labels.json")
    assert report["corpus_sha256"] == snapshot["sha256"]
    assert report["corpus_rows"] == snapshot["active_rows"]
    assert report["query_count"] == len(labels)
    assert report["topic_count"] == len({p["topic"] for p in labels})
    for method in report["methods"].values():
        ranks = method["target_ranks"]
        assert set(ranks) == {p["query_id"] for p in labels}
        for origin, aggregate in method["aggregate"].items():
            selected = [ranks[p["query_id"]] for p in labels
                        if origin == "all" or p["origin"] == origin]
            assert aggregate["n"] == len(selected)
            assert aggregate["mrr@10"] == pytest.approx(statistics.mean(
                1 / r if r is not None and r <= 10 else 0 for r in selected))
            for k in (1, 5, 10):
                assert aggregate[f"hit@{k}"] == pytest.approx(statistics.mean(
                    r is not None and r <= k for r in selected))
