"""S10 tests — class_states plumbing on SequentialCalibrationTracker.

Covers:
- record_exogenous_tactical_outcome accepts and writes through to class_states
- omitted class_tag buckets into UNKNOWN_CLASS_BUCKET (lossless against global)
- compute_metrics_by_class envelope shape (bootstrapped flag + by_class dict)
- class-scope envelope omits log_evidence/capped_alarm/last_alt_probability
  (S10 council finding: not anytime-valid at class granularity)
- rebucket_from_agent_states moves counters when class membership shifts,
  flips bootstrapped, logs to stderr on classifier exceptions
- persistence round-trips class_states; pre-S10 files load with bootstrapped=False
"""

import json

from src.sequential_calibration import (
    SequentialCalibrationTracker,
    UNKNOWN_CLASS_BUCKET,
)


def _record(tracker, *, confidence, outcome_correct, agent_id, class_tag, signal_source="tests"):
    return tracker.record_exogenous_tactical_outcome(
        confidence=confidence,
        outcome_correct=outcome_correct,
        agent_id=agent_id,
        class_tag=class_tag,
        signal_source=signal_source,
        decision_action="proceed",
        outcome_type="test_passed" if outcome_correct else "test_failed",
    )


def test_class_tag_writes_through_to_class_states(tmp_path):
    tracker = SequentialCalibrationTracker(state_file=tmp_path / "seq.json")

    _record(tracker, confidence=0.9, outcome_correct=False, agent_id="a", class_tag="substrate")
    _record(tracker, confidence=0.9, outcome_correct=True, agent_id="b", class_tag="session_like")
    _record(tracker, confidence=0.9, outcome_correct=False, agent_id="c", class_tag="session_like")

    envelope = tracker.compute_metrics_by_class()
    by_class = envelope["by_class"]
    assert envelope["bootstrapped"] is True
    assert set(by_class.keys()) == {"substrate", "session_like"}
    assert by_class["substrate"]["eligible_samples"] == 1
    assert by_class["session_like"]["eligible_samples"] == 2
    assert by_class["session_like"]["scope"] == "class"
    assert by_class["session_like"]["class_tag"] == "session_like"


def test_class_envelope_omits_e_process_fields(tmp_path):
    """S10 council finding: class-scope metrics cannot include log_evidence /
    capped_alarm / last_alt_probability because rebucket sums log_e_values
    across agents with different q-trajectories — no martingale interpretation.
    Drop those fields unconditionally at class scope (even on live-write data,
    so consumers can't accidentally read them as anytime-valid)."""
    tracker = SequentialCalibrationTracker(state_file=tmp_path / "seq.json")

    _record(tracker, confidence=0.9, outcome_correct=False, agent_id="a", class_tag="substrate")
    _record(tracker, confidence=0.9, outcome_correct=False, agent_id="a", class_tag="substrate")

    by_class = tracker.compute_metrics_by_class()["by_class"]
    metrics = by_class["substrate"]
    assert metrics["status"] == "tracking"
    # Descriptive stats present:
    for field in ("eligible_samples", "mean_confidence", "empirical_accuracy",
                  "calibration_gap", "signal_sources", "last_updated"):
        assert field in metrics, f"class envelope missing {field}"
    # E-process fields explicitly absent:
    for field in ("log_evidence", "capped_alarm", "last_alt_probability", "last_e_value"):
        assert field not in metrics, f"class envelope must not carry {field}"

    # Global view still carries the e-process fields.
    global_metrics = tracker.compute_metrics()
    assert "log_evidence" in global_metrics
    assert "capped_alarm" in global_metrics


def test_class_envelope_no_data_also_omits_e_process_fields(tmp_path):
    """No-data and zero-sample empty envelopes must also keep the contract."""
    tracker = SequentialCalibrationTracker(state_file=tmp_path / "seq.json")
    # Force a zero-sample class_states entry by direct dict access:
    tracker.class_states["ephemeral"]  # touch defaultdict to materialize
    envelope = tracker.compute_metrics_by_class()
    metrics = envelope["by_class"]["ephemeral"]
    assert metrics["status"] == "no_data"
    assert "log_evidence" not in metrics
    assert "capped_alarm" not in metrics


def test_omitted_class_tag_buckets_into_unknown(tmp_path):
    tracker = SequentialCalibrationTracker(state_file=tmp_path / "seq.json")

    result = tracker.record_exogenous_tactical_outcome(
        confidence=0.5,
        outcome_correct=True,
        agent_id="a",
        signal_source="tests",
    )
    assert result["class_tag"] == UNKNOWN_CLASS_BUCKET

    by_class = tracker.compute_metrics_by_class()["by_class"]
    assert UNKNOWN_CLASS_BUCKET in by_class
    assert by_class[UNKNOWN_CLASS_BUCKET]["eligible_samples"] == 1


def test_class_sum_equals_global(tmp_path):
    """The class breakdown must be lossless against the global rollup."""
    tracker = SequentialCalibrationTracker(state_file=tmp_path / "seq.json")

    _record(tracker, confidence=0.8, outcome_correct=True, agent_id="a", class_tag="substrate")
    _record(tracker, confidence=0.8, outcome_correct=False, agent_id="b", class_tag="session_like")
    _record(tracker, confidence=0.8, outcome_correct=True, agent_id="c", class_tag=None)

    by_class = tracker.compute_metrics_by_class()["by_class"]
    class_samples = sum(m["eligible_samples"] for m in by_class.values())
    global_metrics = tracker.compute_metrics()

    assert class_samples == global_metrics["eligible_samples"] == 3


def test_compute_metrics_by_class_no_data(tmp_path):
    tracker = SequentialCalibrationTracker(state_file=tmp_path / "seq.json")
    envelope = tracker.compute_metrics_by_class()
    assert envelope == {"bootstrapped": True, "by_class": {}}


def test_rebucket_moves_counters_under_promotion(tmp_path):
    """An agent promoted ephemeral → session_like should move counters cleanly
    when rebucket runs; the donor bucket should not retain inflated counts."""
    tracker = SequentialCalibrationTracker(state_file=tmp_path / "seq.json")

    _record(tracker, confidence=0.7, outcome_correct=True, agent_id="promoted", class_tag="ephemeral")
    _record(tracker, confidence=0.7, outcome_correct=True, agent_id="promoted", class_tag="ephemeral")
    _record(tracker, confidence=0.7, outcome_correct=True, agent_id="stable", class_tag="substrate")

    # Pre-rebucket: ephemeral has both samples from "promoted".
    pre = tracker.compute_metrics_by_class()["by_class"]
    assert pre["ephemeral"]["eligible_samples"] == 2
    assert pre["substrate"]["eligible_samples"] == 1

    # Promote "promoted" to session_like via the classifier callback.
    current_class = {"promoted": "session_like", "stable": "substrate"}
    telemetry = tracker.rebucket_from_agent_states(classifier=lambda aid: current_class.get(aid))

    post = tracker.compute_metrics_by_class()["by_class"]
    assert post["session_like"]["eligible_samples"] == 2
    assert post["substrate"]["eligible_samples"] == 1
    assert "ephemeral" not in post  # donor bucket cleared, not inflated
    assert telemetry["tracked_agents"] == 2
    assert telemetry["unresolved_agents"] == 0
    assert telemetry["classifier_errors"] == 0
    assert telemetry["buckets"] == {"session_like": 1, "substrate": 1}


def test_rebucket_unresolved_classifier_falls_to_unknown(tmp_path):
    tracker = SequentialCalibrationTracker(state_file=tmp_path / "seq.json")
    _record(tracker, confidence=0.9, outcome_correct=True, agent_id="mystery", class_tag="ephemeral")

    telemetry = tracker.rebucket_from_agent_states(classifier=lambda aid: None)
    by_class = tracker.compute_metrics_by_class()["by_class"]

    assert UNKNOWN_CLASS_BUCKET in by_class
    assert by_class[UNKNOWN_CLASS_BUCKET]["eligible_samples"] == 1
    assert telemetry["unresolved_agents"] == 1
    assert telemetry["classifier_errors"] == 0  # None is not an error, it's "unresolved"


def test_rebucket_classifier_exception_logs_to_stderr(tmp_path, capsys):
    """A classifier raising should (a) bucket the agent into unknown so the
    sweep doesn't crash, (b) increment classifier_errors in telemetry, and
    (c) emit a stderr line so operators can distinguish classifier bugs from
    legitimate mass-UNKNOWN scenarios. Council finding: bare except without
    logging masks programming errors."""
    tracker = SequentialCalibrationTracker(state_file=tmp_path / "seq.json")
    _record(tracker, confidence=0.9, outcome_correct=True, agent_id="brittle", class_tag="ephemeral")

    def raising_classifier(_aid):
        raise RuntimeError("db unavailable")

    telemetry = tracker.rebucket_from_agent_states(classifier=raising_classifier)
    by_class = tracker.compute_metrics_by_class()["by_class"]

    assert by_class[UNKNOWN_CLASS_BUCKET]["eligible_samples"] == 1
    assert telemetry["unresolved_agents"] == 1
    assert telemetry["classifier_errors"] == 1

    captured = capsys.readouterr()
    assert "[S10 rebucket]" in captured.err
    assert "brittle" in captured.err
    assert "RuntimeError" in captured.err
    assert "db unavailable" in captured.err


def test_class_states_persists_round_trip(tmp_path):
    state_file = tmp_path / "seq.json"
    t1 = SequentialCalibrationTracker(state_file=state_file)
    _record(t1, confidence=0.6, outcome_correct=True, agent_id="a", class_tag="substrate")
    _record(t1, confidence=0.6, outcome_correct=False, agent_id="b", class_tag="session_like")

    t2 = SequentialCalibrationTracker(state_file=state_file)
    envelope = t2.compute_metrics_by_class()
    assert envelope["bootstrapped"] is True
    by_class = envelope["by_class"]
    assert by_class["substrate"]["eligible_samples"] == 1
    assert by_class["session_like"]["eligible_samples"] == 1


def test_pre_s10_state_file_loads_with_bootstrap_false(tmp_path):
    """A state file written before S10 (no `classes` key) must load without
    error AND signal bootstrapped=False so dashboards can label the by-class
    view as a sparse bootstrap window rather than fleet-representative."""
    state_file = tmp_path / "seq.json"
    pre_s10 = {
        "global": {
            "eligible_samples": 100,
            "successes": 80,
            "confidence_sum": 75.0,
            "log_e_value": 0.0,
            "last_e_value": 1.0,
            "last_alt_probability": 0.5,
            "signal_sources": {"tests": 100},
            "signal_source_outcomes": {"tests": {"samples": 100, "successes": 80}},
            "last_updated": "2026-05-01T00:00:00+00:00",
        },
        "agents": {
            "legacy-agent": {
                "eligible_samples": 50,
                "successes": 40,
                "confidence_sum": 37.5,
                "log_e_value": 0.0,
                "last_e_value": 1.0,
                "last_alt_probability": 0.5,
                "signal_sources": {"tests": 50},
                "signal_source_outcomes": {"tests": {"samples": 50, "successes": 40}},
                "last_updated": "2026-05-01T00:00:00+00:00",
            },
        },
        "prior_success": 1.0,
        "prior_failure": 1.0,
    }
    from config.governance_config import GovernanceConfig
    pre_s10["epoch"] = GovernanceConfig.CURRENT_EPOCH
    state_file.write_text(json.dumps(pre_s10))

    tracker = SequentialCalibrationTracker(state_file=state_file)
    envelope = tracker.compute_metrics_by_class()
    # Honest gap labeling: no class_states populated yet AND we know there's
    # agent history that isn't represented.
    assert envelope["bootstrapped"] is False
    assert envelope["by_class"] == {}

    # Rebucket flips bootstrapped to True.
    tracker.rebucket_from_agent_states(classifier=lambda aid: "substrate")
    envelope_post = tracker.compute_metrics_by_class()
    assert envelope_post["bootstrapped"] is True
    assert envelope_post["by_class"]["substrate"]["eligible_samples"] == 50


def test_fresh_tracker_is_bootstrapped(tmp_path):
    """A brand-new tracker (no prior state file) starts bootstrapped=True —
    there is no agent history hiding behind an empty class_states."""
    tracker = SequentialCalibrationTracker(state_file=tmp_path / "seq.json")
    envelope = tracker.compute_metrics_by_class()
    assert envelope["bootstrapped"] is True


def test_bootstrap_flag_persists_after_rebucket(tmp_path):
    state_file = tmp_path / "seq.json"
    from config.governance_config import GovernanceConfig

    pre_s10 = {
        "global": {"eligible_samples": 1, "successes": 1, "confidence_sum": 0.5,
                   "log_e_value": 0.0, "last_e_value": 1.0, "last_alt_probability": 0.5,
                   "signal_sources": {"tests": 1},
                   "signal_source_outcomes": {"tests": {"samples": 1, "successes": 1}},
                   "last_updated": "2026-05-01T00:00:00+00:00"},
        "agents": {"a": {"eligible_samples": 1, "successes": 1, "confidence_sum": 0.5,
                         "log_e_value": 0.0, "last_e_value": 1.0, "last_alt_probability": 0.5,
                         "signal_sources": {"tests": 1},
                         "signal_source_outcomes": {"tests": {"samples": 1, "successes": 1}},
                         "last_updated": "2026-05-01T00:00:00+00:00"}},
        "prior_success": 1.0, "prior_failure": 1.0,
        "epoch": GovernanceConfig.CURRENT_EPOCH,
    }
    state_file.write_text(json.dumps(pre_s10))

    t1 = SequentialCalibrationTracker(state_file=state_file)
    assert t1.compute_metrics_by_class()["bootstrapped"] is False
    t1.rebucket_from_agent_states(classifier=lambda aid: "substrate")

    t2 = SequentialCalibrationTracker(state_file=state_file)
    assert t2.compute_metrics_by_class()["bootstrapped"] is True


def test_rebucket_preserves_last_updated_max(tmp_path):
    tracker = SequentialCalibrationTracker(state_file=tmp_path / "seq.json")
    _record(tracker, confidence=0.6, outcome_correct=True, agent_id="a", class_tag="ephemeral")
    _record(tracker, confidence=0.6, outcome_correct=True, agent_id="b", class_tag="ephemeral")

    # Force last_updated divergence between the two agents.
    tracker.agent_states["a"]["last_updated"] = "2026-05-01T00:00:00+00:00"
    tracker.agent_states["b"]["last_updated"] = "2026-05-10T00:00:00+00:00"

    tracker.rebucket_from_agent_states(classifier=lambda _aid: "session_like")
    by_class = tracker.compute_metrics_by_class()["by_class"]
    assert by_class["session_like"]["last_updated"] == "2026-05-10T00:00:00+00:00"
