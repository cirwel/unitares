"""Tests for the exogenous-only sequential calibration tracker."""

from src.sequential_calibration import SequentialCalibrationTracker


def test_no_data_omits_e_process_fields(tmp_path):
    tracker = SequentialCalibrationTracker(state_file=tmp_path / "seq_state.json")

    global_metrics = tracker.compute_metrics()
    agent_metrics = tracker.compute_metrics(agent_id="missing-agent")

    for metrics in (global_metrics, agent_metrics):
        assert metrics["status"] == "no_data"
        assert metrics["eligible_samples"] == 0
        assert "log_evidence" not in metrics
        assert "capped_alarm" not in metrics
        assert "last_alt_probability" not in metrics


def test_overconfident_failures_accumulate_positive_evidence(tmp_path):
    tracker = SequentialCalibrationTracker(state_file=tmp_path / "seq_state.json")

    tracker.record_exogenous_tactical_outcome(
        confidence=0.9,
        outcome_correct=False,
        agent_id="agent-a",
        signal_source="tests",
        decision_action="proceed",
        outcome_type="test_failed",
    )
    tracker.record_exogenous_tactical_outcome(
        confidence=0.9,
        outcome_correct=False,
        agent_id="agent-a",
        signal_source="tests",
        decision_action="proceed",
        outcome_type="test_failed",
    )

    metrics = tracker.compute_metrics()
    assert metrics["status"] == "tracking"
    assert metrics["eligible_samples"] == 2
    assert metrics["empirical_accuracy"] == 0.0
    assert metrics["mean_confidence"] == 0.9
    assert metrics["log_evidence"] > 0.0
    assert metrics["capped_alarm"] > 0.0
    assert metrics["signal_sources"]["tests"] == 2


def test_positive_log_is_clamped_for_alarm(tmp_path):
    tracker = SequentialCalibrationTracker(state_file=tmp_path / "seq_state.json")

    tracker.record_exogenous_tactical_outcome(
        confidence=0.9,
        outcome_correct=True,
        agent_id="agent-a",
        signal_source="tests",
        decision_action="proceed",
        outcome_type="test_passed",
    )

    metrics = tracker.compute_metrics()
    assert metrics["eligible_samples"] == 1
    assert metrics["log_evidence"] == 0.0
    assert metrics["capped_alarm"] == 0.0


def test_agent_metrics_are_isolated(tmp_path):
    tracker = SequentialCalibrationTracker(state_file=tmp_path / "seq_state.json")

    tracker.record_exogenous_tactical_outcome(
        confidence=0.9,
        outcome_correct=False,
        agent_id="agent-a",
        signal_source="tests",
        decision_action="proceed",
        outcome_type="test_failed",
    )
    tracker.record_exogenous_tactical_outcome(
        confidence=0.8,
        outcome_correct=True,
        agent_id="agent-b",
        signal_source="lint",
        decision_action="proceed",
        outcome_type="task_completed",
    )

    global_metrics = tracker.compute_metrics()
    a_metrics = tracker.compute_metrics(agent_id="agent-a")
    b_metrics = tracker.compute_metrics(agent_id="agent-b")

    assert global_metrics["eligible_samples"] == 2
    assert a_metrics["eligible_samples"] == 1
    assert b_metrics["eligible_samples"] == 1
    assert a_metrics["empirical_accuracy"] == 0.0
    assert b_metrics["empirical_accuracy"] == 1.0
    assert a_metrics["signal_sources"] == {"tests": 1}
    assert b_metrics["signal_sources"] == {"lint": 1}


def test_persisted_record_reloads_before_write_to_avoid_stale_overwrite(tmp_path):
    state_file = tmp_path / "seq_state.json"
    first = SequentialCalibrationTracker(state_file=state_file)
    stale_second = SequentialCalibrationTracker(state_file=state_file)

    first.record_exogenous_tactical_outcome(
        confidence=0.9,
        outcome_correct=False,
        agent_id="agent-a",
        signal_source="tests",
        decision_action="proceed",
        outcome_type="test_failed",
    )
    stale_second.record_exogenous_tactical_outcome(
        confidence=0.8,
        outcome_correct=True,
        agent_id="agent-b",
        signal_source="lint",
        decision_action="proceed",
        outcome_type="task_completed",
    )

    reloaded = SequentialCalibrationTracker(state_file=state_file)

    assert reloaded.compute_metrics()["eligible_samples"] == 2
    assert reloaded.compute_metrics(agent_id="agent-a")["eligible_samples"] == 1
    assert reloaded.compute_metrics(agent_id="agent-b")["eligible_samples"] == 1


def test_persisted_record_preserves_local_unpersisted_samples_when_not_stale(tmp_path):
    state_file = tmp_path / "seq_state.json"
    tracker = SequentialCalibrationTracker(state_file=state_file)
    tracker.record_exogenous_tactical_outcome(
        confidence=0.9,
        outcome_correct=False,
        agent_id="agent-a",
        signal_source="tests",
        decision_action="proceed",
        outcome_type="test_failed",
        persist=False,
    )

    tracker.record_exogenous_tactical_outcome(
        confidence=0.8,
        outcome_correct=True,
        agent_id="agent-b",
        signal_source="lint",
        decision_action="proceed",
        outcome_type="task_completed",
    )
    reloaded = SequentialCalibrationTracker(state_file=state_file)

    assert reloaded.compute_metrics()["eligible_samples"] == 2
    assert reloaded.compute_metrics(agent_id="agent-a")["eligible_samples"] == 1
    assert reloaded.compute_metrics(agent_id="agent-b")["eligible_samples"] == 1


def test_drop_agent_state_prunes_agent_and_invalidates_class_rollup(tmp_path):
    state_file = tmp_path / "seq_state.json"
    tracker = SequentialCalibrationTracker(state_file=state_file)
    tracker.record_exogenous_tactical_outcome(
        confidence=0.9,
        outcome_correct=False,
        agent_id="agent-a",
        class_tag="ephemeral",
        signal_source="tests",
        decision_action="proceed",
        outcome_type="test_failed",
    )
    tracker.record_exogenous_tactical_outcome(
        confidence=0.8,
        outcome_correct=True,
        agent_id="agent-b",
        class_tag="session_like",
        signal_source="lint",
        decision_action="proceed",
        outcome_type="task_completed",
    )

    assert tracker.drop_agent_state("agent-a") is True
    reloaded = SequentialCalibrationTracker(state_file=state_file)

    assert reloaded.compute_metrics()["eligible_samples"] == 2
    assert reloaded.compute_metrics(agent_id="agent-a")["status"] == "no_data"
    assert reloaded.compute_metrics(agent_id="agent-b")["eligible_samples"] == 1
    by_class = reloaded.compute_metrics_by_class()
    assert by_class["bootstrapped"] is False
    assert by_class["by_class"] == {}


def test_prediction_id_is_echoed_in_return_payload(tmp_path):
    """record_exogenous_tactical_outcome should pass prediction_id through for audit."""
    tracker = SequentialCalibrationTracker(state_file=tmp_path / "seq_state.json")

    result = tracker.record_exogenous_tactical_outcome(
        confidence=0.85,
        outcome_correct=True,
        agent_id="agent-pid",
        signal_source="tests",
        decision_action="proceed",
        outcome_type="test_passed",
        prediction_id="pid-trace-1",
    )

    assert result["prediction_id"] == "pid-trace-1"
    assert result["agent_id"] == "agent-pid"
    assert result["signal_source"] == "tests"
