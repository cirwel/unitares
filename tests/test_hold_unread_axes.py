"""Unread behavioral axes are held, not filled (UNITARES_HOLD_UNREAD_AXES, era HU1)."""

from __future__ import annotations

import math

import pytest

from src.behavioral_assessment import assess_behavioral_state
from src.behavioral_sensor import (
    HOLD_UNREAD_AXES_ENV,
    compute_behavioral_sensor_components,
    compute_behavioral_sensor_eisv,
    hold_unread,
    hold_unread_axes_enabled,
    unread_axes,
)
from src.behavioral_state import (
    BOOTSTRAP_E,
    BOOTSTRAP_I,
    STALE_AFTER_UPDATES,
    BehavioralEISV,
)


def _sensor_inputs(**overrides) -> dict:
    inputs = dict(
        decision_history=["proceed"] * 4,
        coherence_history=[0.5] * 4,
        regime_history=["convergence"] * 4,
        E_history=[0.7] * 3,
        I_history=[0.7] * 3,
        S_history=[0.2] * 3,
        V_history=[0.0] * 3,
        drift_norm=0.3,
        continuity_E_input=0.3,
        tool_error_rate=0.0,
        unique_tools_ratio=0.02,
    )
    inputs.update(overrides)
    return inputs


# --- sensor ---------------------------------------------------------------


def test_axis_without_a_base_term_is_unread_and_the_filled_arithmetic_is_kept():
    inputs = _sensor_inputs()
    components = compute_behavioral_sensor_components(**inputs)
    filled = compute_behavioral_sensor_eisv(**inputs)

    assert unread_axes(components) == ("E", "I")
    reading = hold_unread(filled, components)
    assert reading["E"] is None and reading["I"] is None
    assert reading["S"] == filled["S"]
    # The entry point the shadows and the ODE anchor use is unchanged.
    assert all(isinstance(filled[k], float) for k in "EIS")


def test_one_supplied_base_term_makes_the_axis_a_reading():
    inputs = _sensor_inputs(complexity_divergence=0.2)
    components = compute_behavioral_sensor_components(**inputs)
    assert unread_axes(components) == ("I",)
    assert hold_unread(compute_behavioral_sensor_eisv(**inputs), components)["E"] is not None


def test_flag_off_restores_filling(monkeypatch):
    monkeypatch.setenv(HOLD_UNREAD_AXES_ENV, "0")
    assert hold_unread_axes_enabled() is False
    inputs = _sensor_inputs()
    filled = compute_behavioral_sensor_eisv(**inputs)
    assert hold_unread(filled, compute_behavioral_sensor_components(**inputs)) == filled


@pytest.mark.parametrize("record", [None, {}, {"dimensions": {"E": {"components": []}}}, {"dimensions": 3}])
def test_a_malformed_record_never_withholds_a_reading(record):
    assert unread_axes(record) == ()


# --- state ----------------------------------------------------------------


def test_unread_axis_is_held_and_takes_no_baseline_sample():
    state = BehavioralEISV()
    for _ in range(6):
        state.update(None, None, 0.4)

    assert (state.E, state.I) == (BOOTSTRAP_E, BOOTSTRAP_I)
    assert state._baseline_E.count == 0 and state._baseline_I.count == 0
    assert state.E_history == [] and state.V_history == []
    assert state._baseline_S.count == 6
    assert state.readings == {"E": 0, "I": 0, "S": 6, "V": 0}
    assert state.unread_dimensions == ["E", "I", "V"]
    assert state.update_count == 6
    assert state.to_dict_for_persistence()["raw_obs"] == [None, None, 0.4]


def test_first_reading_seeds_the_value_and_restarts_the_baseline():
    # State persisted before readings were counted: numbers from the filled formula.
    legacy = BehavioralEISV(hold_unread=False)
    for _ in range(40):
        legacy.update(0.78, 0.68, 0.47)
    persisted = legacy.to_dict_with_history()
    del persisted["readings"], persisted["since_reading"]

    restored = BehavioralEISV.from_dict(persisted)
    assert restored.I == pytest.approx(0.68, abs=1e-3)  # kept, as a placeholder
    assert restored.unread_dimensions == ["E", "I", "S", "V"]
    # The old baseline is kept until a reading restarts it, but an unscored
    # axis reports no deviation against it.
    assert restored._baseline_I.count == 40
    assert restored.deviation("I") == 0.0
    # Maturity carries over until the first reading: no cold-start drop at deploy.
    assert restored.confidence == 1.0 and restored.is_baselined

    restored.update(None, 0.45, None)
    assert restored.I == 0.45  # not smoothed into the placeholder
    assert restored._baseline_I.count == 1
    assert restored.I_history == [0.45]
    assert restored.deviation("I") == 0.0  # too few samples to z-score
    assert restored.E == pytest.approx(0.78, abs=1e-3)  # still held


def test_v_steps_only_when_e_and_i_are_both_read():
    state = BehavioralEISV()
    state.update(0.9, None, 0.2)
    state.update(None, 0.4, 0.2)
    assert state.V == 0.0 and state.readings["V"] == 0

    state.update(0.9, 0.4, 0.2)
    assert state.V == pytest.approx(0.5)
    assert state.readings["V"] == 1


def test_later_readings_take_the_ema_step_keyed_on_the_axis_own_count():
    state = BehavioralEISV()
    state.update(0.8, None, 0.2)
    for _ in range(5):
        state.update(None, None, 0.2)
    state.update(0.6, None, 0.2)
    # Second E reading: alpha 0.12 plus the catch-up ramp at count 1 of 10.
    alpha = 0.12 + 0.5 * (1 - 1 / 10)
    assert state.E == pytest.approx((1 - alpha) * 0.8 + alpha * 0.6)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_observation_is_no_reading(bad):
    state = BehavioralEISV()
    state.update(0.7, 0.7, 0.2)
    state.update(bad, 0.7, 0.2)
    assert state.E == 0.7 and state.readings["E"] == 1
    assert all(math.isfinite(getattr(state, d)) for d in "EISV")
    assert state.obs_history[-1][0] is None


def test_counters_round_trip_and_flag_off_state_reads_every_checkin(monkeypatch):
    state = BehavioralEISV()
    state.update(0.7, None, 0.3)
    state.update(None, None, 0.3)
    for dump in (state.to_dict_with_history(), state.to_dict_for_persistence()):
        restored = BehavioralEISV.from_dict(dump)
        assert restored.readings == state.readings
        assert restored.since_reading == state.since_reading

    # Rollback: the hold switched off must still load state written under it,
    # including null observations, and go back to treating every check-in as read.
    monkeypatch.setenv(HOLD_UNREAD_AXES_ENV, "0")
    rolled_back = BehavioralEISV.from_dict(state.to_dict_with_history())
    assert rolled_back.hold_unread is False
    rolled_back.update(0.6, 0.6, 0.3)
    assert rolled_back.unscored_dimensions == []

    legacy = BehavioralEISV.from_dict({"E": 0.7, "I": 0.7, "S": 0.2, "V": 0.0, "updates": 12})
    assert legacy.readings == dict.fromkeys(("E", "I", "S", "V"), 12)


def test_flag_off_update_matches_the_pre_hold_arithmetic(monkeypatch):
    monkeypatch.setenv(HOLD_UNREAD_AXES_ENV, "0")
    state = BehavioralEISV()
    state.update(0.9, 0.3, 0.6)
    # Bootstrap ramp from the 0.5 / 0.5 / 0.2 / 0.0 constants, no seeding.
    assert state.E == pytest.approx(0.38 * 0.5 + 0.62 * 0.9)
    assert state.I == pytest.approx(0.42 * 0.5 + 0.58 * 0.3)
    assert state.S == pytest.approx(0.35 * 0.2 + 0.65 * 0.6)
    assert state.V == pytest.approx(0.6 * 0.6)


# --- assessment -----------------------------------------------------------


def test_never_read_axes_are_listed_and_raise_no_risk():
    state = BehavioralEISV()
    for _ in range(4):
        state.update(None, None, 0.3)
    state.E, state.I = 0.1, 0.1  # a placeholder under the floors is not a breach

    result = assess_behavioral_state(state)
    assert result.unscored_axes == ("E", "I", "V")
    assert result.components["low_E"] == 0.0 and result.components["low_I"] == 0.0
    assert result.verdict == "safe"


def test_a_reading_is_scored_until_it_goes_stale():
    state = BehavioralEISV()
    state.update(0.1, 0.1, 0.3)  # one bad reading on E and I
    assert assess_behavioral_state(state).components["low_E"] > 0.0

    for _ in range(STALE_AFTER_UPDATES - 1):
        state.update(None, None, 0.3)
    fresh_enough = assess_behavioral_state(state)
    assert fresh_enough.components["low_E"] > 0.0
    assert fresh_enough.unscored_axes == ()

    state.update(None, None, 0.3)
    stale = assess_behavioral_state(state)
    assert state.E == 0.1  # the number is still there
    assert stale.components["low_E"] == 0.0 and stale.components["low_I"] == 0.0
    assert stale.unscored_axes == ("E", "I", "V")


def test_scored_axes_keep_their_risk_when_others_are_unscored():
    state = BehavioralEISV()
    for _ in range(3):
        state.update(None, None, 0.95)
    result = assess_behavioral_state(state)
    assert result.components["high_S"] > 0.0
    assert "S" not in result.unscored_axes


def test_flag_off_scores_everything(monkeypatch):
    monkeypatch.setenv(HOLD_UNREAD_AXES_ENV, "0")
    state = BehavioralEISV()
    state.E = 0.1
    result = assess_behavioral_state(state)
    assert result.unscored_axes == ()
    assert result.components["low_E"] > 0.0


# --- check-in path --------------------------------------------------------


def test_monitor_holds_unread_axes_and_reports_them():
    from src.governance_monitor import UNITARESMonitor

    monitor = UNITARESMonitor("hold-unread-test", load_state=False)
    result = None
    for _ in range(6):
        result = monitor.process_update(
            {"response_text": "Reviewed the change and ran the tests.", "complexity": 0.5}
        )

    state = monitor._behavioral_state
    assert state.readings["I"] == 0  # nothing supplied calibration or outcomes
    assert state.I == BOOTSTRAP_I
    assert "I" in result["behavioral"]["assessment"]["unscored_axes"]
    assert result["behavioral"]["state"]["unread"] == state.unread_dimensions
    assert all(isinstance(result["metrics"][k], float) for k in "EISV")


# --- authority and late-seeded axes (review findings) ----------------------


def test_a_state_with_nothing_read_has_no_confidence_and_takes_no_authority():
    state = BehavioralEISV()
    for _ in range(5):
        state.update(None, None, None)
    assert state.update_count == 5 and state.read_update_count == 0
    assert state.confidence == 0.0
    assert state.is_baselined is False

    state.update(None, None, 0.3)
    assert state.read_update_count == 1
    assert state.confidence == pytest.approx(0.1)


def test_confidence_drops_when_every_reading_has_gone_stale():
    state = BehavioralEISV()
    for _ in range(12):
        state.update(0.7, 0.7, 0.2)
    assert state.confidence == 1.0
    for _ in range(STALE_AFTER_UPDATES):
        state.update(None, None, None)
    assert state.unscored_dimensions == ["E", "I", "S", "V"]
    assert state.confidence == 0.0


def test_cold_start_checkins_do_not_hand_the_verdict_to_an_unread_state():
    from src.governance_monitor import UNITARESMonitor

    monitor = UNITARESMonitor("hold-unread-cold-start", load_state=False)
    for _ in range(3):
        monitor.process_update(
            {"response_text": "Reviewed the change and ran the tests.", "complexity": 0.5}
        )
    state = monitor._behavioral_state
    # The sensor needs three decisions of history, so nothing is read yet.
    assert state.read_update_count == 0
    assert state.confidence == 0.0
    assert monitor.get_primary_eisv() == (
        float(monitor.state.E), float(monitor.state.I),
        float(monitor.state.S), float(monitor.state.V),
    )


def test_late_seeded_axis_is_scored_on_fixed_thresholds_until_it_has_a_baseline():
    state = BehavioralEISV()
    for _ in range(30):
        state.update(0.8, None, 0.2)
    assert state.is_baselined

    # First I readings arrive low: above the absolute floor (0.30), below the
    # fixed threshold (0.40). A z-score is unavailable below 5 samples.
    state.update(0.8, 0.35, 0.2)
    assert state._baseline_I.count == 1
    result = assess_behavioral_state(state)
    assert result.components["low_I"] == pytest.approx(0.30 * (0.4 - 0.35) / 0.4)

    for _ in range(4):
        state.update(0.8, 0.35, 0.2)
    assert state._baseline_I.count == 5  # now self-relative, at its own level
    assert assess_behavioral_state(state).components["low_I"] == 0.0


def test_flag_off_hand_built_state_ramps_on_update_count():
    state = BehavioralEISV(E=0.5, update_count=20, hold_unread=False)
    state.update(0.9, 0.5, 0.2)
    assert state.E == pytest.approx(0.88 * 0.5 + 0.12 * 0.9)  # past the ramp


def test_warmup_phase_counts_what_baselining_counts():
    state = BehavioralEISV()
    for _ in range(6):
        state.update(None, None, None)
    for _ in range(24):
        state.update(0.7, 0.7, 0.2)
    warmup = state.to_dict()["warmup"]
    assert state.update_count == 30 and state.evidence_count == 24
    assert warmup["updates_completed"] == 24
    assert warmup["phase"] == "warming_up" and warmup["is_baselined"] is False


def test_guidance_does_not_cite_a_baseline_that_did_not_score_the_axis():
    state = BehavioralEISV()
    for _ in range(30):
        state.update(0.8, None, 0.2)
    state.update(0.8, 0.10, 0.2)  # first I reading, scored on fixed thresholds

    result = assess_behavioral_state(state)
    assert result.components["low_I"] > 0.0
    assert result.guidance is not None
    assert "baseline" not in result.guidance and "typical" not in result.guidance


def test_carried_over_confidence_expires_when_nothing_is_read():
    restored = BehavioralEISV.from_dict({"E": 0.2, "I": 0.2, "S": 0.8, "V": 0.0, "updates": 40})
    assert restored.confidence == 1.0
    for _ in range(STALE_AFTER_UPDATES - 1):
        restored.update(None, None, None)
    assert restored.confidence == 1.0
    restored.update(None, None, None)
    # Ten check-ins with nothing read: no authority for an unread state.
    assert restored.confidence == 0.0
    assert assess_behavioral_state(restored).unscored_axes == ("E", "I", "S", "V")
