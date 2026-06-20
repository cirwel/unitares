import json

from src import recall_telemetry


def test_record_recall_event_and_summarize(tmp_path, monkeypatch):
    telemetry_file = tmp_path / "recall_misses.jsonl"
    monkeypatch.setattr(recall_telemetry, "_telemetry_file", lambda: telemetry_file)

    recall_telemetry.record_recall_event(
        recall_telemetry.ZERO_RESULT,
        "needle in haystack",
        query_terms=3,
        search_mode="fts",
        detail={"hybrid_skipped": False},
    )
    recall_telemetry.record_recall_event(
        recall_telemetry.LOW_CONFIDENCE,
        "semantic only",
        search_mode="hybrid_rrf",
    )

    rows = [json.loads(line) for line in telemetry_file.read_text().splitlines()]
    assert rows[0]["class"] == "zero_result"
    assert rows[0]["query"] == "needle in haystack"
    assert rows[0]["query_terms"] == 3
    assert rows[0]["search_mode"] == "fts"
    assert rows[0]["detail"] == {"hybrid_skipped": False}
    assert rows[1]["class"] == "low_confidence"

    summary = recall_telemetry.summarize()
    assert summary["total"] == 2
    assert summary["by_class"] == {"zero_result": 1, "low_confidence": 1}
    assert summary["file"] == str(telemetry_file)


def test_recall_telemetry_fail_open(monkeypatch):
    def explode():
        raise OSError("no telemetry today")

    monkeypatch.setattr(recall_telemetry, "_telemetry_file", explode)

    recall_telemetry.record_recall_event(recall_telemetry.ZERO_RESULT, "query")
    summary = recall_telemetry.summarize()

    assert summary["total"] == 0
    assert "error" in summary
