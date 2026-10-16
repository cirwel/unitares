"""The offline diagnostic uses real handler stages and refuses changed labels."""
import argparse
import json

import pytest

from scripts.eval import kg_serving_ablation as ablation


@pytest.fixture
def frozen_inputs(tmp_path):
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"pairs": [{
        "query_id": "q1", "query": "incident cause", "topic": "incident", "origin": "logged",
        "relevant_ids": ["answer"],
    }]}))
    rows = [{"id": docid, "agent_id": "author", "type": "insight", "summary": docid,
             "status": status, "created_at": created, "tags": [], "bge_vector": json.dumps(vector)}
            for docid, status, created, vector in [
                ("answer", "resolved", "2026-01-01T00:00:00+00:00", [1., 0.]),
                ("recent", "open", "2026-10-13T00:00:00+00:00", [.8, .6]),
            ]]
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({
        "labels_sha256": ablation.digest(labels), "captured_at": "2026-10-14T04:00:00+00:00",
        "documents": rows, "query_vectors": [[1., 0.]],
        "queries": [{"query_id": "q1", "ann": [{"id": "answer", "score": 1.}, {"id": "recent", "score": .8}],
                     "fts": {"AND": [], "OR": []}}],
        "connectivity_counts": [{"id": "recent", "related": 40, "responds": 0, "superseded_by": 0}],
        "hnsw_ef_search": "40", "ann_plan": ["Seq Scan"],
    }))
    return argparse.Namespace(labels=str(labels), snapshot=str(snapshot),
                              output=str(tmp_path / "result.json"), server_timezone="America/Denver")


@pytest.mark.asyncio
async def test_replay_distinguishes_old_weights_from_current_handler(frozen_inputs, monkeypatch):
    monkeypatch.setattr(ablation, "VARIANTS", {"production_exact": {}, "handler_current": {"ann": True}})
    await ablation.replay(frozen_inputs)
    data = json.loads(open(frozen_inputs.output).read())
    assert data["variants"]["production_exact"]["per_query"][0]["rank"] == 2
    assert data["variants"]["handler_current"]["per_query"][0]["rank"] == 1
    assert data["variants"]["handler_current"]["aggregate"]["all"]["n"] == 1
    assert data["source_sha256"]["src/mcp_handlers/knowledge/handlers.py"]


@pytest.mark.asyncio
async def test_changed_labels_are_rejected(frozen_inputs):
    with open(frozen_inputs.labels, "a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="labels changed"):
        await ablation.replay(frozen_inputs)


@pytest.mark.asyncio
async def test_archived_target_is_rejected_without_changing_denominator(frozen_inputs):
    with open(frozen_inputs.snapshot) as stream:
        data = json.load(stream)
    data["documents"][0]["status"] = "archived"
    with open(frozen_inputs.snapshot, "w") as stream:
        json.dump(data, stream)
    with pytest.raises(ValueError, match="out-of-scope"):
        await ablation.replay(frozen_inputs)
