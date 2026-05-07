"""Reverse-iteration scanner + tool-usage rotation.

Regression tests for the audit-log / tool-usage tail-scan fix. These guard the
two properties that matter:

1. Correctness — same set of entries surfaces as the old forward scan.
2. Early termination — once a record older than the cutoff is hit, the scanner
   stops. A test that fails here means the production hot path will scan the
   whole 1+ GB file again.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.audit_log import AuditLogger, _iter_jsonl_reverse
from src.tool_usage_tracker import ToolUsageTracker


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


# ---------------------------------------------------------------------------
# _iter_jsonl_reverse — primitive correctness
# ---------------------------------------------------------------------------

class TestIterJsonlReverse:
    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        assert list(_iter_jsonl_reverse(p)) == []

    def test_single_line_with_trailing_newline(self, tmp_path):
        p = tmp_path / "one.jsonl"
        p.write_text('{"a":1}\n')
        assert list(_iter_jsonl_reverse(p)) == ['{"a":1}']

    def test_single_line_no_trailing_newline(self, tmp_path):
        p = tmp_path / "one.jsonl"
        p.write_text('{"a":1}')
        assert list(_iter_jsonl_reverse(p)) == ['{"a":1}']

    def test_multiple_lines_yielded_in_reverse(self, tmp_path):
        p = tmp_path / "many.jsonl"
        p.write_text("\n".join(f'{{"i":{i}}}' for i in range(5)) + "\n")
        out = list(_iter_jsonl_reverse(p))
        assert out == [f'{{"i":{i}}}' for i in range(4, -1, -1)]

    def test_line_spans_multiple_chunks(self, tmp_path):
        p = tmp_path / "wide.jsonl"
        # One line longer than chunk_size; one short line. Iterator must reassemble.
        long_line = '{"x":"' + "a" * 200 + '"}'
        short_line = '{"y":1}'
        p.write_text(long_line + "\n" + short_line + "\n")
        out = list(_iter_jsonl_reverse(p, chunk_size=64))
        assert out == [short_line, long_line]

    def test_blank_lines_skipped(self, tmp_path):
        p = tmp_path / "blanks.jsonl"
        p.write_text('{"a":1}\n\n{"b":2}\n')
        assert list(_iter_jsonl_reverse(p)) == ['{"b":2}', '{"a":1}']


# ---------------------------------------------------------------------------
# Audit log: query_audit_log + get_skip_rate_metrics break on cutoff
# ---------------------------------------------------------------------------

class TestAuditLogTailScan:
    @pytest.fixture
    def logger_inst(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_AUDIT_WRITE_JSONL", "1")
        return AuditLogger(log_file=tmp_path / "audit.jsonl")

    def test_query_returns_filtered_results(self, logger_inst):
        logger_inst.log_lambda1_skip("a1", 0.4, 0.5, 1)
        logger_inst.log_auto_attest("a1", 0.9, True, 0.3, "approved")
        skips = logger_inst.query_audit_log(event_type="lambda1_skip")
        attests = logger_inst.query_audit_log(event_type="auto_attest")
        assert len(skips) == 1 and skips[0]["event_type"] == "lambda1_skip"
        assert len(attests) == 1 and attests[0]["event_type"] == "auto_attest"

    def test_query_respects_limit(self, logger_inst):
        for _ in range(5):
            logger_inst.log_auto_attest("a1", 0.9, True, 0.3, "approved")
        assert len(logger_inst.query_audit_log(limit=3)) == 3

    def test_query_returns_newest_first(self, logger_inst):
        # Reverse iteration is the new contract: callers asking for "recent_events"
        # now actually receive the newest entries first.
        logger_inst.log_lambda1_skip("a1", 0.1, 0.5, 1)
        logger_inst.log_lambda1_skip("a1", 0.2, 0.5, 2)
        logger_inst.log_lambda1_skip("a1", 0.3, 0.5, 3)
        out = logger_inst.query_audit_log(event_type="lambda1_skip", limit=10)
        assert [e["confidence"] for e in out] == [0.3, 0.2, 0.1]

    def test_query_breaks_on_start_time_cutoff(self, tmp_path, monkeypatch):
        # 100 old entries + 3 recent entries. With reverse iteration and a recent
        # start_time, the scanner must stop after the recent ones — never touching
        # the 100 old entries.
        monkeypatch.setenv("UNITARES_AUDIT_WRITE_JSONL", "1")
        log_file = tmp_path / "audit.jsonl"
        old_ts = (datetime.now() - timedelta(days=30)).isoformat()
        recent_ts = datetime.now().isoformat()
        entries = []
        for _ in range(100):
            entries.append({"timestamp": old_ts, "agent_id": "a", "event_type": "lambda1_skip", "confidence": 0.4, "details": {}})
        for _ in range(3):
            entries.append({"timestamp": recent_ts, "agent_id": "a", "event_type": "lambda1_skip", "confidence": 0.7, "details": {}})
        _write_jsonl(log_file, entries)

        inst = AuditLogger(log_file=log_file)

        original_loads = json.loads
        load_count = [0]

        def counting_loads(s, *a, **kw):
            load_count[0] += 1
            return original_loads(s, *a, **kw)

        monkeypatch.setattr("src.audit_log.json.loads", counting_loads)
        cutoff = (datetime.now() - timedelta(hours=1)).isoformat()
        results = inst.query_audit_log(start_time=cutoff, limit=10)
        # Exactly the 3 recent entries are returned.
        assert len(results) == 3
        # We parsed at most ~4 lines (3 recent + 1 boundary), nowhere near 100.
        assert load_count[0] <= 5, f"Scanned {load_count[0]} entries; should have stopped at the cutoff"

    def test_skip_rate_breaks_on_window(self, tmp_path):
        log_file = tmp_path / "audit.jsonl"
        old_ts = (datetime.now() - timedelta(days=30)).isoformat()
        recent_ts = datetime.now().isoformat()
        entries = []
        for _ in range(50):
            entries.append({"timestamp": old_ts, "agent_id": "a", "event_type": "lambda1_skip", "confidence": 0.4, "details": {}})
        for _ in range(2):
            entries.append({"timestamp": recent_ts, "agent_id": "a", "event_type": "lambda1_skip", "confidence": 0.4, "details": {}})
            entries.append({"timestamp": recent_ts, "agent_id": "a", "event_type": "auto_attest", "confidence": 0.9, "details": {}})
        _write_jsonl(log_file, entries)

        inst = AuditLogger(log_file=log_file)
        metrics = inst.get_skip_rate_metrics(window_hours=1)
        # Only the recent entries inside the 1-hour window count.
        assert metrics["total_skips"] == 2
        assert metrics["total_updates"] == 2


# ---------------------------------------------------------------------------
# Tool usage: get_usage_stats early-termination + rotate_log
# ---------------------------------------------------------------------------

class TestToolUsageTailScan:
    def test_get_usage_stats_breaks_on_window(self, tmp_path, monkeypatch):
        log_file = tmp_path / "tool_usage.jsonl"
        old_ts = (datetime.now() - timedelta(days=30)).isoformat()
        recent_ts = datetime.now().isoformat()
        entries = []
        for _ in range(200):
            entries.append({"timestamp": old_ts, "tool_name": "old_tool", "agent_id": "a", "success": True})
        for _ in range(4):
            entries.append({"timestamp": recent_ts, "tool_name": "fresh_tool", "agent_id": "a", "success": True})
        _write_jsonl(log_file, entries)

        tracker = ToolUsageTracker(log_file=log_file)

        original_loads = json.loads
        load_count = [0]

        def counting_loads(s, *a, **kw):
            load_count[0] += 1
            return original_loads(s, *a, **kw)

        monkeypatch.setattr("src.tool_usage_tracker.json.loads", counting_loads)
        stats = tracker.get_usage_stats(window_hours=1)
        assert stats["total_calls"] == 4
        assert "fresh_tool" in stats["tools"]
        assert "old_tool" not in stats["tools"]
        assert load_count[0] <= 6, f"Scanned {load_count[0]} entries; should have stopped at the cutoff"

    def test_rotate_log_archives_and_truncates(self, tmp_path):
        log_file = tmp_path / "tool_usage.jsonl"
        old_ts = (datetime.now() - timedelta(days=30)).isoformat()
        recent_ts = datetime.now().isoformat()
        entries = [
            {"timestamp": old_ts, "tool_name": "ancient", "agent_id": "a", "success": True},
            {"timestamp": old_ts, "tool_name": "ancient", "agent_id": "a", "success": True},
            {"timestamp": recent_ts, "tool_name": "fresh", "agent_id": "a", "success": True},
        ]
        _write_jsonl(log_file, entries)

        tracker = ToolUsageTracker(log_file=log_file)
        kept, archive_path = tracker.rotate_log(max_age_days=7)
        assert kept == 1
        assert archive_path is not None and archive_path.exists()

        # Live log keeps only the recent entry.
        with open(log_file) as f:
            live = [json.loads(line) for line in f]
        assert len(live) == 1 and live[0]["tool_name"] == "fresh"

        # Archive captured the two ancient entries.
        with open(archive_path) as f:
            archived = [json.loads(line) for line in f]
        assert len(archived) == 2
        assert all(e["tool_name"] == "ancient" for e in archived)

    def test_rotate_log_no_file_returns_none(self, tmp_path):
        tracker = ToolUsageTracker(log_file=tmp_path / "missing.jsonl")
        # Tracker init creates parent dir; remove the file so .exists() is False.
        kept, archive_path = tracker.rotate_log(max_age_days=7)
        assert kept is None and archive_path is None


# ---------------------------------------------------------------------------
# Mixed tz-aware / tz-naive timestamps — pre-existing bug
# ---------------------------------------------------------------------------

class TestMixedTimezoneTimestamps:
    """Audit entries written across the system's lifetime mix tz-aware and
    tz-naive timestamps. ``datetime.now()`` is naive; without normalisation,
    comparing one against the other raises ``TypeError`` and the rotation /
    skip-rate paths return error dicts (or no-op) on the first aware entry.
    Real-world failure: ``rotate_log`` returned ``(None, None)`` on the live
    1.3GB audit_log because the very first entry it hit was tz-aware.
    """

    @pytest.fixture
    def mixed_audit(self, tmp_path):
        log_file = tmp_path / "audit.jsonl"
        recent_naive = datetime.now().isoformat()
        recent_aware = datetime.now().astimezone().isoformat()  # has tz offset
        old_naive = (datetime.now() - timedelta(days=30)).isoformat()
        old_aware = (datetime.now() - timedelta(days=30)).astimezone().isoformat()
        entries = [
            {"timestamp": old_naive, "agent_id": "a", "event_type": "lambda1_skip", "confidence": 0.4, "details": {}},
            {"timestamp": old_aware, "agent_id": "a", "event_type": "auto_attest", "confidence": 0.9, "details": {}},
            {"timestamp": recent_naive, "agent_id": "a", "event_type": "lambda1_skip", "confidence": 0.5, "details": {}},
            {"timestamp": recent_aware, "agent_id": "a", "event_type": "auto_attest", "confidence": 0.95, "details": {}},
        ]
        _write_jsonl(log_file, entries)
        return AuditLogger(log_file=log_file)

    def test_skip_rate_handles_mixed_tz(self, mixed_audit):
        metrics = mixed_audit.get_skip_rate_metrics(window_hours=1)
        # Only the recent entries (one of each type) should count, regardless of tz form.
        assert metrics["total_skips"] == 1
        assert metrics["total_updates"] == 1

    def test_query_handles_mixed_tz(self, mixed_audit):
        cutoff = (datetime.now() - timedelta(hours=1)).isoformat()
        recent = mixed_audit.query_audit_log(start_time=cutoff, limit=100)
        assert len(recent) == 2  # one naive, one aware, both within the window

    def test_rotate_log_handles_mixed_tz(self, mixed_audit):
        kept, archive = mixed_audit.rotate_log(max_age_days=7)
        # Without the fix this returned (None, None); with normalisation we keep
        # the two recent entries and archive the two old ones.
        assert kept == 2
        assert archive is not None and archive.exists()
