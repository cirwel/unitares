"""Regression coverage for issue #1995's decision-neutral floor telemetry."""

import copy
import json
from datetime import datetime
from unittest.mock import patch

import pytest

from src import behavioral_assessment
from src.behavioral_assessment import assess_behavioral_state
from src.behavioral_floor_observability import (
    ABSOLUTE_FLOOR_OBSERVATION_SCHEMA,
    build_absolute_floor_observation,
)
from src.behavioral_state import BehavioralEISV
from src.governance_monitor import UNITARESMonitor


def _state(**overrides: float) -> BehavioralEISV:
    state = BehavioralEISV()
    values = {"E": 0.8, "I": 0.8, "S": 0.2, "V": 0.0}
    values.update(overrides)
    for dimension, value in values.items():
        setattr(state, dimension, value)
    return state


def _normalize_baseline_at_current_state(state: BehavioralEISV) -> None:
    """Make self-relative deviations zero at the supplied absolute state."""

    state.update_count = 30
    for dimension in ("E", "I", "S", "V"):
        baseline = getattr(state, f"_baseline_{dimension}")
        baseline.count = 30
        baseline.mean = getattr(state, dimension)
        baseline.m2 = 0.0
    assert state.is_baselined is True


@pytest.mark.parametrize(
    ("overrides", "dimension", "component", "risk"),
    [
        ({"E": 0.0, "I": 0.5, "V": -0.5}, "E", "low_E", 0.30),
        ({"E": 0.5, "I": 0.0, "V": 0.5}, "I", "low_I", 0.30),
        ({"S": 1.0}, "S", "high_S", 0.20),
        ({"E": 1.0, "I": 0.4, "V": 0.6}, "V", "high_V", 0.04),
        ({"E": 0.4, "I": 1.0, "V": -0.6}, "V", "high_V", 0.04),
    ],
)
def test_reachable_steady_single_floor_breach_is_observed_with_safe_verdict(
    overrides,
    dimension,
    component,
    risk,
):
    state = _state(**overrides)
    _normalize_baseline_at_current_state(state)
    assessment = assess_behavioral_state(state)

    observation = build_absolute_floor_observation(
        state,
        assessment,
        resolved_verdict_source="behavioral_assessment",
    )

    assert assessment.risk == pytest.approx(risk)
    assert assessment.verdict == "safe"
    assert observation["breached_dimensions"] == [dimension]
    assert list(observation["floor_component_contributions"]) == [component]
    assert observation["breach_count"] == 1
    assert observation["breach_with_safe_behavioral_verdict"] is True
    assert observation["behavioral_risk"] == assessment.risk
    assert observation["behavioral_verdict"] == assessment.verdict
    assert observation["behavioral_baselined"] is True
    assert observation["behavioral_verdict_authoritative"] is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"E": 0.30},
        {"I": 0.30},
        {"S": 0.70},
        {"V": 0.50},
        {"V": -0.50},
    ],
)
def test_exact_threshold_is_an_evaluated_lean_non_breach(overrides):
    state = _state(**overrides)
    assessment = assess_behavioral_state(state)

    observation = build_absolute_floor_observation(state, assessment)

    assert observation["schema"] == ABSOLUTE_FLOOR_OBSERVATION_SCHEMA
    assert observation["evaluated"] is True
    assert observation["eligible_for_production_counter"] is True
    assert observation["breached_dimensions"] == []
    assert observation["breach_count"] == 0
    assert observation["breach_with_safe_behavioral_verdict"] is False
    assert "breach_measurement_snapshot" not in observation
    assert "floor_component_contributions" not in observation


def test_multiple_breaches_preserve_order_and_non_safe_verdict():
    state = _state(E=0.0, I=0.0, S=1.0, V=-1.0)
    assessment = assess_behavioral_state(state)

    observation = build_absolute_floor_observation(state, assessment)

    assert assessment.risk == 1.0
    assert assessment.verdict == "high-risk"
    assert observation["breached_dimensions"] == ["E", "I", "S", "V"]
    assert observation["breach_count"] == 4
    assert observation["breach_with_safe_behavioral_verdict"] is False
    assert set(observation["breach_measurement_snapshot"]) == {"E", "I", "S", "V"}


def test_observation_schema_is_bounded_json_and_snapshots_verdict_geometry():
    state = _state(E=0.0)
    assessment = assess_behavioral_state(state)

    observation = build_absolute_floor_observation(state, assessment)

    assert set(observation) == {
        "schema",
        "evaluated",
        "measurement_role",
        "policy_effect",
        "measurement_scope",
        "eligible_for_production_counter",
        "threshold_snapshot",
        "behavioral_confidence",
        "behavioral_baselined",
        "resolved_verdict_source",
        "behavioral_verdict_authoritative",
        "breached_dimensions",
        "breach_count",
        "behavioral_risk",
        "behavioral_verdict",
        "breach_with_safe_behavioral_verdict",
        "breach_measurement_snapshot",
        "floor_component_contributions",
    }
    assert observation["threshold_snapshot"] == {
        "E_breach_lt": behavioral_assessment.ABSOLUTE_E_FLOOR,
        "I_breach_lt": behavioral_assessment.ABSOLUTE_I_FLOOR,
        "S_breach_gt": behavioral_assessment.ABSOLUTE_S_CEILING,
        "abs_V_breach_gt": behavioral_assessment.ABSOLUTE_V_CEILING,
        "safe_risk_lt": behavioral_assessment.RISK_SAFE_THRESHOLD,
        "caution_risk_lt": behavioral_assessment.RISK_CAUTION_THRESHOLD,
    }
    assert observation["measurement_role"] == "telemetry_only"
    assert observation["policy_effect"] == "none"
    assert json.loads(json.dumps(observation)) == observation


def test_threshold_snapshot_reads_the_assessment_source_at_evaluation_time(monkeypatch):
    monkeypatch.setattr(behavioral_assessment, "ABSOLUTE_E_FLOOR", 0.25)
    state = _state(E=0.20)
    assessment = assess_behavioral_state(state)

    observation = build_absolute_floor_observation(state, assessment)

    assert observation["threshold_snapshot"]["E_breach_lt"] == 0.25
    assert observation["breached_dimensions"] == ["E"]


def _agent_state() -> dict:
    return {
        "parameters": [0.0, 0.0],
        "ethical_drift": [0.0, 0.0, 0.0],
        "response_text": "Focused integration test.",
        "complexity": 0.2,
    }


def _run_with_observer(observer_result, run_label="single"):
    monitor = UNITARESMonitor(
        f"test-floor-observation-ab-{run_label}",
        load_state=False,
    )
    monitor._cold_start_confirmation_lineage = "test-floor-observation-lineage"
    # Force every run through the same capped-dt path rather than comparing
    # sub-millisecond wall-clock differences between monitor construction.
    monitor.last_update = datetime(2020, 1, 1)
    observer_error = observer_result if isinstance(observer_result, Exception) else None
    observer_value = None if observer_error else observer_result
    with (
        patch(
            "src.behavioral_floor_observability.build_absolute_floor_observation",
            return_value=observer_value,
            side_effect=observer_error,
        ),
        patch(
            "src.governance_monitor.compute_behavioral_sensor_eisv",
            return_value={"E": 0.5, "I": 0.5, "S": 0.2},
        ),
        patch("src.governance_monitor.audit_logger.log_auto_attest") as log_auto_attest,
    ):
        result = monitor.process_update(_agent_state(), confidence=0.8)
    return monitor, result, log_auto_attest


def test_observer_variants_are_non_actuating_and_reach_result_and_existing_audit():
    breach = {
        "schema": ABSOLUTE_FLOOR_OBSERVATION_SCHEMA,
        "evaluated": True,
        "measurement_role": "telemetry_only",
        "policy_effect": "none",
        "measurement_scope": "live",
        "eligible_for_production_counter": True,
        "breached_dimensions": ["E"],
        "breach_count": 1,
        "behavioral_verdict": "safe",
        "breach_with_safe_behavioral_verdict": True,
    }
    no_breach = {
        **breach,
        "breached_dimensions": [],
        "breach_count": 0,
        "breach_with_safe_behavioral_verdict": False,
    }

    runs = [
        _run_with_observer(breach, "breach"),
        _run_with_observer(no_breach, "no-breach"),
        _run_with_observer(RuntimeError("telemetry unavailable"), "failure"),
    ]
    control_monitor, control_result, _control_audit = runs[0]

    for monitor, result, logged in runs:
        assessment = copy.deepcopy(result["behavioral"]["assessment"])
        observation = assessment.pop("absolute_floor_observation")
        control_assessment = copy.deepcopy(control_result["behavioral"]["assessment"])
        control_assessment.pop("absolute_floor_observation")

        assert assessment == control_assessment
        assert result["decision"] == control_result["decision"]
        assert result["policy_evaluation"] == control_result["policy_evaluation"]
        assert result["enforcement"] == control_result["enforcement"]
        for field in ("risk_score", "verdict", "verdict_source", "primary_eisv_source"):
            assert result["metrics"].get(field) == control_result["metrics"].get(field)
        assert monitor.state.risk_history == control_monitor.state.risk_history
        assert monitor.state.decision_history == control_monitor.state.decision_history
        assert monitor.state.verdict_history == control_monitor.state.verdict_history

        assert logged.call_count == 1
        audit_observation = logged.call_args.kwargs["details"]["behavioral"][
            "absolute_floor_observation"
        ]
        assert audit_observation == observation
        assert "absolute_floor_observation" not in result["policy_evaluation"]["inputs"]


def test_observation_failure_is_explicit_unknown_not_a_zero():
    _monitor, result, logged = _run_with_observer(
        RuntimeError("telemetry unavailable")
    )

    observation = result["behavioral"]["assessment"]["absolute_floor_observation"]
    assert observation == {
        "schema": ABSOLUTE_FLOOR_OBSERVATION_SCHEMA,
        "evaluated": False,
        "measurement_role": "telemetry_only",
        "policy_effect": "none",
        "measurement_scope": "live",
        "eligible_for_production_counter": False,
        "unavailable_reason": "evaluation_failed",
    }
    assert logged.call_count == 1


def test_simulation_is_excluded_from_counter_and_restores_real_observation():
    monitor = UNITARESMonitor("test-floor-observation-simulation", load_state=False)
    real_observation = {"marker": "last-real-observation"}
    monitor._last_absolute_floor_observation = real_observation

    with patch(
        "src.governance_monitor.audit_logger.log_auto_attest"
    ) as log_auto_attest:
        result = monitor.simulate_update(_agent_state(), confidence=0.8)

    simulated = result["behavioral"]["assessment"]["absolute_floor_observation"]
    assert result["simulation"] is True
    assert simulated["measurement_scope"] == "simulation"
    assert simulated["eligible_for_production_counter"] is False
    assert log_auto_attest.call_args.kwargs["details"]["behavioral"][
        "absolute_floor_observation"
    ] == simulated
    assert monitor._last_absolute_floor_observation is real_observation
