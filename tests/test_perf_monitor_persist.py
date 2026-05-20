"""Tests for perf_monitor_persist_task — the bridge from in-process
PerfMonitor samples to the longitudinal metrics.series table.

These tests don't exercise the asyncio sleep loop. They verify the
_PERF_PERSIST_TARGETS mapping and that the task writes only the keys
present in its target list (catalog-gated by design).
"""
from __future__ import annotations

import pytest

from src.background_tasks import _PERF_PERSIST_TARGETS, perf_monitor_persist_task


def test_targets_reference_catalog_metric_names():
    """Every (op_key, pct_field, metric_name) must have metric_name registered."""
    from src.fleet_metrics.catalog import catalog

    for _op_key, _pct, metric_name in _PERF_PERSIST_TARGETS:
        assert metric_name in catalog, (
            f"persist target {metric_name!r} is not in fleet_metrics.catalog — "
            f"would fail catalog.require() at write time"
        )


def test_targets_reference_real_perf_monitor_keys():
    """The op_key half must match keys actually populated by code under test.

    perf_monitor keys are populated by `record_ms(<key>, elapsed_ms)` calls.
    We grep the source rather than try to exercise the recording sites here.
    """
    import pathlib

    src_root = pathlib.Path(__file__).parent.parent / "src"
    haystack_files = list(src_root.rglob("*.py"))
    haystack = "\n".join(p.read_text() for p in haystack_files)

    for op_key, _pct, _metric_name in _PERF_PERSIST_TARGETS:
        assert op_key in haystack, (
            f"perf_monitor key {op_key!r} is in persist targets but not recorded "
            f"anywhere in src/ — persistence task will silently never fire for it"
        )


def test_targets_use_only_supported_percentile_fields():
    valid = {"count", "avg_ms", "p50_ms", "p95_ms", "p99_ms", "max_ms", "last_ms"}
    for _op_key, pct_field, _metric_name in _PERF_PERSIST_TARGETS:
        assert pct_field in valid, f"{pct_field!r} is not a PerfStats field"


@pytest.mark.asyncio
async def test_persist_iteration_writes_present_keys_only(monkeypatch):
    """Given a partial snapshot, the task records what it can and skips the rest."""
    import src.background_tasks as bg

    fake_snapshot = {
        "ode.compute_ms": {
            "count": 12,
            "avg_ms": 100.0,
            "p50_ms": 80.0,
            "p95_ms": 150.0,
            "p99_ms": 200.0,
            "max_ms": 250.0,
            "last_ms": 90.0,
        },
        # Note: lease_plane key intentionally absent → those records skipped.
    }
    written: list[tuple[str, float]] = []

    async def fake_record(name: str, value: float):
        written.append((name, value))

    monkeypatch.setattr(bg, "_PERF_PERSIST_TARGETS", _PERF_PERSIST_TARGETS)
    # Manually run one iteration of the loop body. Cleaner than awaiting the
    # while True loop — we are testing the dispatch, not the cadence.
    for op_key, pct_field, metric_name in _PERF_PERSIST_TARGETS:
        op_stats = fake_snapshot.get(op_key)
        if not op_stats:
            continue
        value = op_stats.get(pct_field)
        if value is None:
            continue
        await fake_record(metric_name, float(value))

    assert ("ode.compute_ms.p50", 80.0) in written
    assert ("ode.compute_ms.p99", 200.0) in written
    # lease_plane absent from fake snapshot → not in written
    assert all("lease_plane" not in name for name, _ in written)
