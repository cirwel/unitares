"""Real-file durability and idempotency boundaries for calibration writes."""

from unittest.mock import patch
from uuid import uuid4

import pytest

from src.calibration_file_store import (
    apply_file_mutation, deliver_file_sample, read_state,
)
from src.calibration_mutations import CalibrationVersionConflict, make_mutation


def sample(count):
    return {"bins": {"0.8-0.9": {"count": count, "actual_correct": count,
                                "predicted_correct": count, "confidence_sum": count * 0.85}}}


def test_acknowledgement_lost_after_rename_does_not_apply_writer_twice(tmp_path):
    path = tmp_path / "calibration.json"
    request = dict(writer_id=str(uuid4()), sequence=1, mutation=make_mutation({}, sample(1)))
    with patch("src.calibration_file_store.sync_directory", side_effect=OSError("lost ack")):
        with pytest.raises(OSError):
            apply_file_mutation(path, **request)

    replay = apply_file_mutation(path, **request)

    assert replay["applied"] is False
    assert read_state(path)["bins"]["0.8-0.9"]["count"] == 1


def test_failure_before_rename_leaves_no_receipt_or_training(tmp_path):
    path = tmp_path / "calibration.json"
    request = dict(writer_id=str(uuid4()), sequence=1, mutation=make_mutation({}, sample(1)))
    with patch("src.calibration_file_store.os.replace", side_effect=OSError("storage failure")):
        with pytest.raises(OSError):
            apply_file_mutation(path, **request)
    assert not path.exists()
    assert apply_file_mutation(path, **request)["applied"] is True
    assert read_state(path)["bins"]["0.8-0.9"]["count"] == 1


def test_fresh_consumer_replays_a_durable_outcome_receipt(tmp_path):
    path = tmp_path / "calibration.json"
    args = dict(outcome_id=str(uuid4()), payload_digest="a" * 64,
                observation_time="2026-11-01T00:00:00+00:00", apply_sample=lambda state: sample(1))
    with patch("src.calibration_file_store.sync_directory", side_effect=OSError("lost ack")):
        with pytest.raises(OSError):
            deliver_file_sample(path, **args)
    args["apply_sample"] = lambda state: pytest.fail("receipt replay must not train")

    assert deliver_file_sample(path, **args)["bins"]["0.8-0.9"]["count"] == 1


def test_same_receipt_cannot_be_reused_with_changed_payload(tmp_path):
    path = tmp_path / "calibration.json"
    args = dict(outcome_id=str(uuid4()), payload_digest="a" * 64,
                observation_time="2026-11-01T00:00:00+00:00", apply_sample=lambda state: sample(1))
    deliver_file_sample(path, **args)
    with pytest.raises(CalibrationVersionConflict):
        deliver_file_sample(path, **(args | {"payload_digest": "b" * 64}))


def test_stale_calibration_checker_preserves_durable_outcome(tmp_path, monkeypatch):
    from src.calibration import CalibrationChecker

    monkeypatch.setenv("UNITARES_CALIBRATION_BACKEND", "json")
    path = tmp_path / "calibration.json"
    first = CalibrationChecker(state_file=path)
    stale = CalibrationChecker(state_file=path)
    first.record_prediction(0.85, True, 1.0)
    stale.record_prediction(0.85, True, 1.0)

    assert read_state(path)["bins"]["0.8-0.9"]["count"] == 2
    assert stale.bin_stats["0.8-0.9"]["count"] == 2


def test_stale_reset_is_rejected_and_cache_returns_to_durable_state(tmp_path, monkeypatch):
    from src.calibration import CalibrationChecker

    monkeypatch.setenv("UNITARES_CALIBRATION_BACKEND", "json")
    path = tmp_path / "calibration.json"
    first = CalibrationChecker(state_file=path)
    first.record_prediction(0.85, True, 1.0)
    stale = CalibrationChecker(state_file=path)
    first.record_prediction(0.85, True, 1.0)
    stale.reset()

    assert stale.save_state() is False
    assert read_state(path)["bins"]["0.8-0.9"]["count"] == 2
    assert stale.bin_stats["0.8-0.9"]["count"] == 2


def test_symlink_alias_shares_calibration_state_and_lock(tmp_path, monkeypatch):
    from src.calibration import CalibrationChecker

    monkeypatch.setenv("UNITARES_CALIBRATION_BACKEND", "json")
    path, alias = tmp_path / "canonical.json", tmp_path / "alias.json"
    path.write_text("{}")
    alias.symlink_to(path)
    first, second = CalibrationChecker(state_file=path), CalibrationChecker(state_file=alias)
    first.record_prediction(0.85, True, 1.0)
    second.record_prediction(0.85, True, 1.0)
    assert alias.is_symlink()
    assert read_state(path)["bins"]["0.8-0.9"]["count"] == 2


def test_symlink_alias_preserves_sequential_delivery_identity(tmp_path):
    from src.sequential_calibration import SequentialCalibrationTracker

    path, alias = tmp_path / "canonical.json", tmp_path / "alias.json"
    first = SequentialCalibrationTracker(path)
    first.save_state()
    alias.symlink_to(path)
    second = SequentialCalibrationTracker(alias)
    args = dict(confidence=0.8, outcome_correct=True, signal_source="tests",
                delivery_id="canonical-outcome", delivery_digest="a" * 64)
    first.record_exogenous_tactical_outcome(**args)
    second.record_exogenous_tactical_outcome(**args)
    second.record_exogenous_tactical_outcome(confidence=0.7, outcome_correct=False, signal_source="tasks")
    assert alias.is_symlink()
    assert SequentialCalibrationTracker(path).compute_metrics()["eligible_samples"] == 2
