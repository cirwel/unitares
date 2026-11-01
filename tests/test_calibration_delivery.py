"""File crash boundaries and preservation of sequential delivery receipts."""
import json

import pytest

from src import calibration_file_store as store
from src import calibration_delivery as delivery
from src.sequential_calibration import SequentialCalibrationTracker


def deliver(tracker, identity="canonical-outcome"):
    return tracker.record_exogenous_tactical_outcome(
        confidence=0.8, outcome_correct=True, signal_source="tests",
        agent_id="subject", class_tag="default", timestamp="2026-11-01T00:00:00+00:00",
        delivery_id=identity, delivery_digest="a" * 64,
    )


def test_sequential_rename_then_lost_barrier_does_not_repeat_bet(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    tracker = SequentialCalibrationTracker(path)
    barrier = store.sync_directory
    monkeypatch.setattr(store, "sync_directory", lambda _: (_ for _ in ()).throw(OSError("lost barrier")))
    with pytest.raises(OSError):
        deliver(tracker)
    # The rename may already be durable. A fresh consumer proves the barrier
    # before acknowledging the receipt, without applying a second sample.
    monkeypatch.setattr(store, "sync_directory", barrier)
    fresh = SequentialCalibrationTracker(path)
    assert deliver(fresh)["idempotent_replay"]
    assert fresh.compute_metrics()["eligible_samples"] == 1


def test_failed_sequential_replace_restores_memory_before_retry(tmp_path, monkeypatch):
    tracker = SequentialCalibrationTracker(tmp_path / "state.json")
    replace = store.os.replace
    monkeypatch.setattr(store.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("rename failed")))
    with pytest.raises(OSError):
        deliver(tracker)
    assert tracker.compute_metrics()["eligible_samples"] == 0
    monkeypatch.setattr(store.os, "replace", replace)
    deliver(tracker)
    assert tracker.compute_metrics()["eligible_samples"] == 1


def test_stale_bulk_save_cannot_erase_receipt(tmp_path):
    path = tmp_path / "state.json"
    stale, worker = SequentialCalibrationTracker(path), SequentialCalibrationTracker(path)
    stale.record_exogenous_tactical_outcome(confidence=0.5, outcome_correct=False,
                                           signal_source="tasks", persist=False)
    deliver(worker)
    assert stale.save_state() is False
    assert stale.compute_metrics()["eligible_samples"] == 1
    assert deliver(stale)["idempotent_replay"]


def test_rebucket_prune_and_ordinary_write_preserve_receipt(tmp_path):
    tracker = SequentialCalibrationTracker(tmp_path / "state.json")
    deliver(tracker)
    tracker.rebucket_from_agent_states(lambda _: "default")
    tracker.drop_agent_state("subject")
    tracker.record_exogenous_tactical_outcome(confidence=0.4, outcome_correct=False, signal_source="tasks")
    assert deliver(tracker)["idempotent_replay"]
    assert tracker.compute_metrics()["eligible_samples"] == 2


def test_corrupt_file_is_not_replaced_with_an_empty_projection(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{broken state")
    tracker = SequentialCalibrationTracker(path)
    with pytest.raises(json.JSONDecodeError):
        deliver(tracker)
    assert path.read_text() == "{broken state"


def test_configured_instrument_survives_container_hostname_change(monkeypatch):
    monkeypatch.setenv("UNITARES_CALIBRATION_INSTRUMENT_ID", "stable-fixture-instrument")
    monkeypatch.setattr(delivery.socket, "gethostname", lambda: "container-a")
    first = delivery.delivery_target()
    monkeypatch.setattr(delivery.socket, "gethostname", lambda: "container-b")
    assert delivery.delivery_target() == first
