"""Unit tests for the KG usage report's pure aggregation core.

Exercises summarize_usage() against synthetic audit-event rows so the
self-vs-cross-agent read classification and verdict labelling are verified
without a database.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.analysis.kg_usage_report import summarize_usage, _writer_ids_from_read


def _write(agent_id):
    return {"agent_id": agent_id, "payload": {"id": "disc-x"}}


def _read(reader, action, *, writer=None, writers=None):
    payload = {"action": action}
    if writer is not None:
        payload["writer_agent_id"] = writer
    if writers is not None:
        payload["writer_agent_ids"] = writers
    return {"agent_id": reader, "payload": payload}


def test_cross_agent_read_detected():
    s = summarize_usage(
        writes=[_write("A"), _write("A")],
        reads=[_read("B", "details", writer="A")],
    )
    assert s["total_writes"] == 2
    assert s["unique_authors"] == 1
    assert s["attributable_reads"] == 1
    assert s["cross_agent_reads"] == 1
    assert s["self_reads"] == 0
    assert s["top_reader_writer_pairs"] == [{"reader": "B", "writer": "A", "reads": 1}]
    assert s["verdict"].startswith("CROSS-AGENT ACTIVE")


def test_cross_agent_concentration_flagged_when_one_reader_dominates():
    # One reader (B) does all the cross-agent reading -> concentration caveat
    s = summarize_usage(
        writes=[_write("A"), _write("C")],
        reads=[
            _read("B", "details", writer="A"),
            _read("B", "details", writer="C"),
            _read("B", "get", writer="A"),
        ],
    )
    assert s["cross_agent_reads"] == 3
    assert s["cross_agent_unique_readers"] == 1
    assert s["top_cross_reader_share"] == 1.0
    assert "CONCENTRATED" in s["verdict"]


def test_broad_cross_agent_use_not_flagged_as_concentrated():
    # Three distinct readers, evenly spread -> no concentration caveat
    reads = [_read(r, "details", writer="A") for r in ("B", "C", "D")]
    reads += [_read(r, "get", writer="E") for r in ("B", "C", "D")]
    s = summarize_usage(writes=[_write("A"), _write("E")], reads=reads)
    assert s["cross_agent_unique_readers"] == 3
    assert s["top_cross_reader_share"] < 0.7
    assert "CONCENTRATED" not in s["verdict"]


def test_self_read_not_counted_as_cross_agent():
    s = summarize_usage(
        writes=[_write("A")],
        reads=[_read("A", "details", writer="A"), _read("A", "get", writer="A")],
    )
    assert s["attributable_reads"] == 2
    assert s["self_reads"] == 2
    assert s["cross_agent_reads"] == 0
    assert s["verdict"].startswith("SINGLE-AGENT")


def test_search_sample_lower_bounds_cross_agent():
    # search carries a SAMPLE of writer ids; any non-reader writer => cross-agent
    s = summarize_usage(
        writes=[_write("A"), _write("C")],
        reads=[_read("B", "search", writers=["A", "C"])],
    )
    assert s["cross_agent_reads"] == 1
    # both distinct writers recorded as pairs
    pairs = {(p["reader"], p["writer"]) for p in s["top_reader_writer_pairs"]}
    assert pairs == {("B", "A"), ("B", "C")}


def test_unattributed_reader_counted_separately():
    # reader id unknown (pre-onboard) — counted, but not attributable
    s = summarize_usage(
        writes=[_write("A")],
        reads=[_read(None, "details", writer="A"), _read("B", "list")],
    )
    assert s["unattributed_reader_reads"] == 1
    assert s["attributable_reads"] == 0  # null reader + list (no writer)
    assert s["unique_readers"] == 1      # only B has an id


def test_write_only_verdict():
    s = summarize_usage(writes=[_write("A")], reads=[])
    assert s["total_reads"] == 0
    assert s["read_write_ratio"] == 0.0
    assert s["verdict"].startswith("WRITE-ONLY")


def test_read_but_unattributed_verdict():
    s = summarize_usage(writes=[], reads=[_read("B", "list"), _read(None, "search")])
    assert s["attributable_reads"] == 0
    assert s["verdict"].startswith("READ-BUT-UNATTRIBUTED")
    assert s["read_write_ratio"] is None  # no writes


def test_reads_by_action_tally():
    s = summarize_usage(
        writes=[],
        reads=[_read("B", "search", writers=["A"]), _read("B", "get", writer="A"),
               _read("B", "get", writer="A")],
    )
    assert s["reads_by_action"] == {"search": 1, "get": 2}


def test_writer_id_extraction_helper():
    assert _writer_ids_from_read({"writer_agent_id": "A"}) == ["A"]
    assert _writer_ids_from_read({"writer_agent_ids": ["A", "B"]}) == ["A", "B"]
    assert _writer_ids_from_read({"action": "list"}) == []
    assert _writer_ids_from_read("not-a-dict") == []
