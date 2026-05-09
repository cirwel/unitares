"""
Tests for src/perf_monitor.py - Lightweight in-process performance counters.

Pure in-memory, no I/O or external dependencies.
"""

import pytest
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.perf_monitor import PerfStats, PerfMonitor, record_ms, snapshot


# ============================================================================
# PerfStats
# ============================================================================

class TestPerfStats:

    def test_creation(self):
        s = PerfStats(count=10, avg_ms=5.0, p50_ms=4.5, p95_ms=9.0, p99_ms=9.8, max_ms=10.0, last_ms=6.0)
        assert s.count == 10
        assert s.avg_ms == 5.0
        assert s.p50_ms == 4.5
        assert s.p95_ms == 9.0
        assert s.p99_ms == 9.8
        assert s.max_ms == 10.0
        assert s.last_ms == 6.0

    def test_frozen(self):
        s = PerfStats(count=1, avg_ms=1.0, p50_ms=1.0, p95_ms=1.0, p99_ms=1.0, max_ms=1.0, last_ms=1.0)
        with pytest.raises(AttributeError):
            s.count = 2


# ============================================================================
# PerfMonitor - record_ms
# ============================================================================

class TestPerfMonitorRecord:

    def test_record_single(self):
        pm = PerfMonitor()
        pm.record_ms("test_op", 5.0)
        snap = pm.snapshot()
        assert "test_op" in snap
        assert snap["test_op"]["count"] == 1

    def test_record_multiple(self):
        pm = PerfMonitor()
        for i in range(10):
            pm.record_ms("test_op", float(i))
        snap = pm.snapshot()
        assert snap["test_op"]["count"] == 10

    def test_record_empty_op_ignored(self):
        pm = PerfMonitor()
        pm.record_ms("", 5.0)
        snap = pm.snapshot()
        assert len(snap) == 0

    def test_record_negative_ignored(self):
        pm = PerfMonitor()
        pm.record_ms("test_op", -1.0)
        snap = pm.snapshot()
        assert "test_op" not in snap

    def test_record_nan_ignored(self):
        pm = PerfMonitor()
        pm.record_ms("test_op", float('nan'))
        snap = pm.snapshot()
        assert "test_op" not in snap

    def test_max_samples(self):
        pm = PerfMonitor(max_samples_per_op=5)
        for i in range(20):
            pm.record_ms("op", float(i))
        snap = pm.snapshot()
        assert snap["op"]["count"] == 5  # Capped at max_samples


# ============================================================================
# PerfMonitor - snapshot
# ============================================================================

class TestPerfMonitorSnapshot:

    def test_empty_snapshot(self):
        pm = PerfMonitor()
        snap = pm.snapshot()
        assert snap == {}

    def test_snapshot_fields(self):
        pm = PerfMonitor()
        pm.record_ms("op", 5.0)
        snap = pm.snapshot()
        entry = snap["op"]
        assert "count" in entry
        assert "avg_ms" in entry
        assert "p50_ms" in entry
        assert "p95_ms" in entry
        assert "p99_ms" in entry
        assert "max_ms" in entry
        assert "last_ms" in entry
        assert "sample_window" in entry

    def test_snapshot_accuracy(self):
        pm = PerfMonitor()
        pm.record_ms("op", 10.0)
        pm.record_ms("op", 20.0)
        pm.record_ms("op", 30.0)
        snap = pm.snapshot()
        assert snap["op"]["avg_ms"] == 20.0
        assert snap["op"]["max_ms"] == 30.0
        assert snap["op"]["count"] == 3

    def test_snapshot_single_sample(self):
        pm = PerfMonitor()
        pm.record_ms("op", 7.5)
        snap = pm.snapshot()
        assert snap["op"]["avg_ms"] == 7.5
        assert snap["op"]["p50_ms"] == 7.5
        assert snap["op"]["p95_ms"] == 7.5
        assert snap["op"]["p99_ms"] == 7.5
        assert snap["op"]["max_ms"] == 7.5

    def test_snapshot_multiple_ops(self):
        pm = PerfMonitor()
        pm.record_ms("op_a", 5.0)
        pm.record_ms("op_b", 10.0)
        snap = pm.snapshot()
        assert "op_a" in snap
        assert "op_b" in snap

    def test_snapshot_last_ms(self):
        pm = PerfMonitor()
        pm.record_ms("op", 1.0)
        pm.record_ms("op", 2.0)
        pm.record_ms("op", 3.0)
        snap = pm.snapshot()
        assert snap["op"]["last_ms"] == 3.0

    def test_values_rounded(self):
        pm = PerfMonitor()
        pm.record_ms("op", 1.23456789)
        snap = pm.snapshot()
        assert snap["op"]["avg_ms"] == 1.235  # 3 decimal places


# ============================================================================
# Module-level functions
# ============================================================================

class TestModuleFunctions:

    def test_record_ms_func(self):
        # Uses global perf_monitor
        record_ms("global_test", 5.0)
        snap = snapshot()
        assert "global_test" in snap

    def test_snapshot_func(self):
        snap = snapshot()
        assert isinstance(snap, dict)
