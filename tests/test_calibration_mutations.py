"""Calibration effects survive a concurrent writer without snapshot replacement."""

from copy import deepcopy

import pytest

from src.calibration_mutations import (
    CalibrationVersionConflict, apply_mutation, make_mutation, projection_state,
)


def state(count=0, confidence=0.0, timestamp=None):
    return {"bins": {"0.8-0.9": {
        "count": count, "confidence_sum": confidence,
        "last_observed_at": timestamp,
    }}}


def test_concurrent_additive_writers_preserve_both_samples():
    baseline = state(4, 3.2)
    first = make_mutation(baseline, state(5, 4.0))
    second = make_mutation(baseline, state(5, 4.05))

    result = apply_mutation(apply_mutation(baseline, first), second)

    assert result["bins"]["0.8-0.9"]["count"] == 6
    assert result["bins"]["0.8-0.9"]["confidence_sum"] == pytest.approx(4.85)
    assert baseline == state(4, 3.2)


def test_old_writer_does_not_move_observation_time_backwards():
    old = "2026-10-20T10:00:00+00:00"
    newer = "2026-10-21T10:00:00+00:00"
    pending = make_mutation(state(1, 0.8), state(2, 1.6, old))

    result = apply_mutation(state(3, 2.4, newer), pending)

    assert result["bins"]["0.8-0.9"]["count"] == 4
    assert result["bins"]["0.8-0.9"]["last_observed_at"] == newer


def test_new_agent_does_not_erase_another_writers_agent():
    first = make_mutation({}, {"bins_by_agent": {"first": state(1, 0.8)["bins"]}})
    second = make_mutation({}, {"bins_by_agent": {"second": state(1, 0.9)["bins"]}})

    result = apply_mutation(apply_mutation({}, first), second)

    assert set(result["bins_by_agent"]) == {"first", "second"}


def test_new_zero_counters_are_initialized_without_erasing_concurrent_values():
    mutation = make_mutation({}, {"bins": {"0.8-0.9": {
        "count": 1, "confidence_sum": 0.8, "actual_correct": 0,
    }}})
    assert apply_mutation({}, mutation)["bins"]["0.8-0.9"]["actual_correct"] == 0
    current = {"bins": {"0.8-0.9": {"actual_correct": 2}}}
    assert apply_mutation(current, mutation)["bins"]["0.8-0.9"]["actual_correct"] == 2


@pytest.mark.parametrize("after", [state(1, 0.8), {}])
def test_destructive_edits_require_the_observed_version(after):
    mutation = make_mutation(state(3, 2.4), after)
    assert mutation["kind"] == "replace"
    with pytest.raises(CalibrationVersionConflict):
        apply_mutation(state(4, 3.2), mutation, expected_version=2, actual_version=3)
    with pytest.raises(CalibrationVersionConflict):
        apply_mutation(state(3, 2.4), mutation, actual_version=2)


def test_current_replacement_preserves_storage_receipts():
    current = state(3, 2.4) | {"_delivery_receipts": {"outcome": True}}
    mutation = make_mutation(current, {})

    result = apply_mutation(current, mutation, expected_version=7, actual_version=7)

    assert result["bins"] == {}
    assert result["_delivery_receipts"] == {"outcome": True}


def test_pending_operation_is_frozen_against_later_mutation():
    after = state(1, 0.8)
    mutation = make_mutation({}, after)
    frozen = deepcopy(mutation)
    after["bins"]["0.8-0.9"]["count"] = 20

    assert mutation == frozen
    assert apply_mutation({}, mutation)["bins"]["0.8-0.9"]["count"] == 1


def test_mutation_during_write_can_be_rebased_on_acknowledged_state():
    sent = state(2, 1.6)
    local_now = state(3, 2.4)
    fresh_durable = state(8, 6.4)

    result = apply_mutation(fresh_durable, make_mutation(sent, local_now))

    assert result["bins"]["0.8-0.9"]["count"] == 9
    assert result["bins"]["0.8-0.9"]["confidence_sum"] == pytest.approx(7.2)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_effects_are_rejected(value):
    with pytest.raises(ValueError, match="finite"):
        make_mutation({}, state(1, value))


def test_projection_omits_receipts_and_versions():
    result = projection_state(state(1, 0.8) | {"_version": 3, "_writer_receipts": {}})
    assert "_version" not in result
    assert "_writer_receipts" not in result
