"""Tests for the ProgressFlatProbe orchestrator (Task 8).

All tests use mocks — no real DB, no real heartbeat calls.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from src.resident_progress.probe_task import STARTUP_GRACE_TICKS, ProgressFlatProbe
from src.resident_progress.registry import ResidentConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hb(alive, eval_error=None):
    """Return a minimal HeartbeatStatus-like object."""
    return type("HS", (), {
        "alive": alive,
        "last_update": None,
        "expected_cadence_s": 60,
        "in_critical_silence": not alive,
        "eval_error": eval_error,
        "to_jsonable": lambda self: {"alive": alive},
    })()


def _make_probe(
    sources_by_name=None,
    heartbeat_evaluator=None,
    writer=None,
    audit_emitter=None,
    _now_tick=0,
):
    """Build a ProgressFlatProbe with sensible AsyncMock defaults."""
    if sources_by_name is None:
        sources_by_name = {}
    if heartbeat_evaluator is None:
        heartbeat_evaluator = MagicMock()
        heartbeat_evaluator.evaluate = AsyncMock(return_value=_hb(True))
    if writer is None:
        writer = MagicMock()
        writer.write = AsyncMock()
    if audit_emitter is None:
        audit_emitter = MagicMock()
        audit_emitter.emit = AsyncMock()
    return ProgressFlatProbe(
        sources_by_name=sources_by_name,
        heartbeat_evaluator=heartbeat_evaluator,
        writer=writer,
        audit_emitter=audit_emitter,
        _now_tick=_now_tick,
    )


def _registry_one_vigil():
    return {
        "vigil": ResidentConfig(
            source="kg_writes",
            metric="rows_written",
            window=timedelta(minutes=60),
            threshold=1,
            expected_cadence_s=1800,
        )
    }


# ---------------------------------------------------------------------------
# Test 1: happy path — one resident, source returns count, heartbeat alive
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_writes_one_resident_row_plus_dogfood(monkeypatch):
    """Writer called twice: resident batch (1 row labeled 'vigil') + dogfood row."""
    vigil_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    monkeypatch.setattr(
        "src.resident_progress.probe_task.RESIDENT_PROGRESS_REGISTRY",
        _registry_one_vigil(),
    )
    monkeypatch.setattr(
        "src.resident_progress.probe_task.resolve_resident_uuid",
        lambda label: vigil_uuid if label == "vigil" else None,
    )

    source = MagicMock()
    source.fetch = AsyncMock(return_value={vigil_uuid: 5})  # metric=5, threshold=1 → not below

    heartbeat = MagicMock()
    heartbeat.evaluate = AsyncMock(return_value=_hb(True))

    writer = MagicMock()
    writer.write = AsyncMock()

    audit = MagicMock()
    audit.emit = AsyncMock()

    probe = _make_probe(
        sources_by_name={"kg_writes": source},
        heartbeat_evaluator=heartbeat,
        writer=writer,
        audit_emitter=audit,
    )

    await probe.tick()

    assert writer.write.call_count == 2

    # First call: resident batch
    resident_batch = writer.write.call_args_list[0][0][0]
    assert len(resident_batch) == 1
    row = resident_batch[0]
    assert row.resident_label == "vigil"
    assert row.resident_uuid == vigil_uuid
    assert row.candidate is False  # metric=5 >= threshold=1
    assert row.suppressed_reason is None

    # Second call: dogfood
    dogfood_batch = writer.write.call_args_list[1][0][0]
    assert len(dogfood_batch) == 1
    df = dogfood_batch[0]
    assert df.resident_label == "progress_flat_probe"
    assert df.source == "probe_self"
    assert df.candidate is False

    # No audit event because resident is not a candidate
    audit.emit.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: unresolved label after startup grace — suppressed_reason="unresolved_label"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unresolved_label_writes_suppressed_row_no_event(monkeypatch):
    """UUID resolves to None, tick > STARTUP_GRACE_TICKS → unresolved_label."""
    monkeypatch.setattr(
        "src.resident_progress.probe_task.RESIDENT_PROGRESS_REGISTRY",
        _registry_one_vigil(),
    )
    monkeypatch.setattr(
        "src.resident_progress.probe_task.resolve_resident_uuid",
        lambda label: None,
    )

    writer = MagicMock()
    writer.write = AsyncMock()
    audit = MagicMock()
    audit.emit = AsyncMock()

    # Start _now_tick=STARTUP_GRACE_TICKS so after increment tick_count = STARTUP_GRACE_TICKS+1
    probe = _make_probe(
        writer=writer,
        audit_emitter=audit,
        _now_tick=STARTUP_GRACE_TICKS,
    )

    await probe.tick()

    # Writer called once (resident/unresolved batch) + once (dogfood)
    assert writer.write.call_count == 2
    resident_batch = writer.write.call_args_list[0][0][0]
    assert len(resident_batch) == 1
    row = resident_batch[0]
    assert row.resident_label == "vigil"
    assert row.resident_uuid is None
    assert row.suppressed_reason == "unresolved_label"
    assert row.candidate is False

    # No audit emit
    audit.emit.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: startup grace — first two ticks use "startup_unresolved_label"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_startup_grace_first_two_ticks(monkeypatch):
    """UUID resolves to None on tick=1 → suppressed_reason='startup_unresolved_label'."""
    monkeypatch.setattr(
        "src.resident_progress.probe_task.RESIDENT_PROGRESS_REGISTRY",
        _registry_one_vigil(),
    )
    monkeypatch.setattr(
        "src.resident_progress.probe_task.resolve_resident_uuid",
        lambda label: None,
    )

    writer = MagicMock()
    writer.write = AsyncMock()

    # _now_tick=0 → after increment tick_count=1 ≤ STARTUP_GRACE_TICKS=2
    probe = _make_probe(writer=writer, _now_tick=0)

    await probe.tick()

    resident_batch = writer.write.call_args_list[0][0][0]
    assert len(resident_batch) == 1
    row = resident_batch[0]
    assert row.suppressed_reason == "startup_unresolved_label"

    # Second tick (_now_tick=1 → 2 ≤ 2) also uses startup reason
    writer.write.reset_mock()
    await probe.tick()
    resident_batch2 = writer.write.call_args_list[0][0][0]
    assert resident_batch2[0].suppressed_reason == "startup_unresolved_label"

    # Third tick (_now_tick=2 → 3 > 2) switches to unresolved_label
    writer.write.reset_mock()
    await probe.tick()
    resident_batch3 = writer.write.call_args_list[0][0][0]
    assert resident_batch3[0].suppressed_reason == "unresolved_label"


# ---------------------------------------------------------------------------
# Test 4: source error isolation — one source raises, other succeeds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_source_error_isolated_per_resident(monkeypatch):
    """Two residents, two sources. One source raises; other succeeds.

    Erroring resident: suppressed_reason='source_error', candidate=False.
    Healthy resident (metric below threshold, alive): candidate=True.
    """
    vigil_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    watcher_uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    registry = {
        "vigil": ResidentConfig(
            source="kg_writes",
            metric="rows_written",
            window=timedelta(minutes=60),
            threshold=1,
            expected_cadence_s=1800,
        ),
        "watcher": ResidentConfig(
            source="watcher_findings",
            metric="rows_any",
            window=timedelta(hours=6),
            threshold=1,
            expected_cadence_s=21600,
        ),
    }
    monkeypatch.setattr(
        "src.resident_progress.probe_task.RESIDENT_PROGRESS_REGISTRY",
        registry,
    )

    uuid_map = {"vigil": vigil_uuid, "watcher": watcher_uuid}
    monkeypatch.setattr(
        "src.resident_progress.probe_task.resolve_resident_uuid",
        lambda label: uuid_map.get(label),
    )

    # kg_writes source raises; watcher_findings returns metric=0 (below threshold=1)
    bad_source = MagicMock()
    bad_source.fetch = AsyncMock(side_effect=RuntimeError("db exploded"))

    good_source = MagicMock()
    good_source.fetch = AsyncMock(return_value={watcher_uuid: 0})

    heartbeat = MagicMock()
    heartbeat.evaluate = AsyncMock(return_value=_hb(True))

    writer = MagicMock()
    writer.write = AsyncMock()
    audit = MagicMock()
    audit.emit = AsyncMock()

    probe = _make_probe(
        sources_by_name={"kg_writes": bad_source, "watcher_findings": good_source},
        heartbeat_evaluator=heartbeat,
        writer=writer,
        audit_emitter=audit,
    )

    await probe.tick()

    resident_batch = writer.write.call_args_list[0][0][0]
    assert len(resident_batch) == 2

    by_label = {r.resident_label: r for r in resident_batch}

    vigil_row = by_label["vigil"]
    assert vigil_row.suppressed_reason == "source_error"
    assert vigil_row.candidate is False
    assert vigil_row.error_details is not None
    assert vigil_row.error_details["source"] == "kg_writes"
    assert "RuntimeError" in vigil_row.error_details["error"]

    watcher_row = by_label["watcher"]
    assert watcher_row.candidate is True
    assert watcher_row.suppressed_reason is None
    assert watcher_row.metric_value == 0
    assert watcher_row.metric_below_threshold is True

    # Audit event emitted for watcher (candidate=True)
    audit.emit.assert_called_once()
    call_kwargs = audit.emit.call_args
    assert call_kwargs.kwargs["event_type"] == "progress_flat_candidate"
    assert call_kwargs.kwargs["payload"]["resident_label"] == "watcher"


# ---------------------------------------------------------------------------
# Test 5: dogfood write failure is non-fatal; candidates still get audit events
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dogfood_write_failure_is_non_fatal(monkeypatch):
    """Writer raises on dogfood batch. Tick completes; audit events still fire."""
    vigil_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    monkeypatch.setattr(
        "src.resident_progress.probe_task.RESIDENT_PROGRESS_REGISTRY",
        _registry_one_vigil(),
    )
    monkeypatch.setattr(
        "src.resident_progress.probe_task.resolve_resident_uuid",
        lambda label: vigil_uuid if label == "vigil" else None,
    )

    source = MagicMock()
    # metric=0, threshold=1 → below, candidate=True (if alive)
    source.fetch = AsyncMock(return_value={vigil_uuid: 0})

    heartbeat = MagicMock()
    heartbeat.evaluate = AsyncMock(return_value=_hb(True))

    write_call_count = 0

    async def _write_side_effect(rows):
        nonlocal write_call_count
        write_call_count += 1
        if write_call_count == 2:
            raise RuntimeError("dogfood write failed")
        # First call (resident batch) succeeds

    writer = MagicMock()
    writer.write = AsyncMock(side_effect=_write_side_effect)

    audit = MagicMock()
    audit.emit = AsyncMock()

    probe = _make_probe(
        sources_by_name={"kg_writes": source},
        heartbeat_evaluator=heartbeat,
        writer=writer,
        audit_emitter=audit,
    )

    # Must not raise
    await probe.tick()

    # Writer was called twice (first succeeds, second raises)
    assert writer.write.call_count == 2

    # Audit event emitted despite dogfood failure (candidate vigil row)
    audit.emit.assert_called_once()
    assert audit.emit.call_args.kwargs["payload"]["resident_label"] == "vigil"


# ---------------------------------------------------------------------------
# Test 6: heartbeat_eval_error branch — alive=True ignored, forced to False
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_eval_error_forces_dead_and_suppresses(monkeypatch):
    """hb.alive=True but hb.eval_error='db down' → heartbeat_alive=False, suppressed."""
    vigil_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    monkeypatch.setattr(
        "src.resident_progress.probe_task.RESIDENT_PROGRESS_REGISTRY",
        _registry_one_vigil(),
    )
    monkeypatch.setattr(
        "src.resident_progress.probe_task.resolve_resident_uuid",
        lambda label: vigil_uuid if label == "vigil" else None,
    )

    source = MagicMock()
    source.fetch = AsyncMock(return_value={vigil_uuid: 5})

    heartbeat = MagicMock()
    # alive=True but eval_error set — orchestrator must override alive to False
    heartbeat.evaluate = AsyncMock(return_value=_hb(alive=True, eval_error="db down"))

    writer = MagicMock()
    writer.write = AsyncMock()

    audit = MagicMock()
    audit.emit = AsyncMock()

    probe = _make_probe(
        sources_by_name={"kg_writes": source},
        heartbeat_evaluator=heartbeat,
        writer=writer,
        audit_emitter=audit,
    )

    await probe.tick()

    resident_batch = writer.write.call_args_list[0][0][0]
    assert len(resident_batch) == 1
    row = resident_batch[0]

    # Critical: heartbeat_alive must be forced False despite hb.alive=True
    assert row.heartbeat_alive is False
    assert row.suppressed_reason == "heartbeat_eval_error"
    assert row.error_details == {"heartbeat_error": "db down"}
    assert row.candidate is False
    assert row.metric_value is None

    # No audit event (not a candidate)
    audit.emit.assert_not_called()


# ---------------------------------------------------------------------------
# Test 7: heartbeat_not_alive suppression — metric below threshold but not candidate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_not_alive_suppresses_candidate(monkeypatch):
    """metric=0 < threshold=1 AND hb.alive=False → suppressed_reason='heartbeat_not_alive', candidate=False."""
    vigil_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    monkeypatch.setattr(
        "src.resident_progress.probe_task.RESIDENT_PROGRESS_REGISTRY",
        _registry_one_vigil(),
    )
    monkeypatch.setattr(
        "src.resident_progress.probe_task.resolve_resident_uuid",
        lambda label: vigil_uuid if label == "vigil" else None,
    )

    source = MagicMock()
    source.fetch = AsyncMock(return_value={vigil_uuid: 0})  # metric=0 < threshold=1

    heartbeat = MagicMock()
    heartbeat.evaluate = AsyncMock(return_value=_hb(alive=False))  # no eval_error

    writer = MagicMock()
    writer.write = AsyncMock()

    audit = MagicMock()
    audit.emit = AsyncMock()

    probe = _make_probe(
        sources_by_name={"kg_writes": source},
        heartbeat_evaluator=heartbeat,
        writer=writer,
        audit_emitter=audit,
    )

    await probe.tick()

    resident_batch = writer.write.call_args_list[0][0][0]
    assert len(resident_batch) == 1
    row = resident_batch[0]

    assert row.suppressed_reason == "heartbeat_not_alive"
    assert row.candidate is False  # NOT True even though metric_below_threshold=True
    assert row.metric_below_threshold is True
    assert row.heartbeat_alive is False

    # No audit event (suppressed)
    audit.emit.assert_not_called()


# ---------------------------------------------------------------------------
# Test 8: dead resident with metric OK — no suppression, just record
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dead_resident_metric_ok_no_suppression(monkeypatch):
    """hb.alive=False AND metric=5 >= threshold=1 → suppressed_reason=None, candidate=False."""
    vigil_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    monkeypatch.setattr(
        "src.resident_progress.probe_task.RESIDENT_PROGRESS_REGISTRY",
        _registry_one_vigil(),
    )
    monkeypatch.setattr(
        "src.resident_progress.probe_task.resolve_resident_uuid",
        lambda label: vigil_uuid if label == "vigil" else None,
    )

    source = MagicMock()
    source.fetch = AsyncMock(return_value={vigil_uuid: 5})  # metric=5 >= threshold=1

    heartbeat = MagicMock()
    heartbeat.evaluate = AsyncMock(return_value=_hb(alive=False))  # dead, no eval_error

    writer = MagicMock()
    writer.write = AsyncMock()

    audit = MagicMock()
    audit.emit = AsyncMock()

    probe = _make_probe(
        sources_by_name={"kg_writes": source},
        heartbeat_evaluator=heartbeat,
        writer=writer,
        audit_emitter=audit,
    )

    await probe.tick()

    resident_batch = writer.write.call_args_list[0][0][0]
    assert len(resident_batch) == 1
    row = resident_batch[0]

    # Dead but metric not flat: no suppression, just record
    assert row.suppressed_reason is None
    assert row.candidate is False  # alive=False so not a candidate
    assert row.heartbeat_alive is False
    assert row.metric_below_threshold is False

    # No audit event (not a candidate)
    audit.emit.assert_not_called()
