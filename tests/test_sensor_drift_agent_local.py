"""The behavioral sensor's drift input keeps only agent-local components.

Under SN1 the behavioral S of an agent that supplies no complexity divergence
is built from one base term, the drift-vector norm. Two of that vector's four
components carry no per-agent information on this path: stability_deviation
is 0.2 for every agent (the drift engine is never given a decision) and
calibration_deviation is the server-wide tactical worst bin. Read on live SN1
rows 2026-11-12, the norm was 0.313 on every row of two residents, so S was a
constant. These tests pin the agent-local input (UNITARES_SENSOR_DRIFT_AGENT_LOCAL).
"""

from __future__ import annotations

import math

import pytest

from governance_core.ethical_drift import AgentBaseline, EthicalDriftVector, compute_ethical_drift
from src.behavioral_sensor import (
    SENSOR_DRIFT_AGENT_LOCAL_ENV,
    SENSOR_DRIFT_MIN_AGENT_CALIBRATION_PAIRS,
    compute_behavioral_sensor_components,
    compute_sensor_drift_input,
    unread_axes,
)

# The live shape: stability 0.2 (no decision), fleet calibration 0.2414
# (tactical bin 0.9-1.0, 0.991 declared vs 0.75 actual), complexity excluded,
# coherence near zero for a steady agent.
FROZEN = EthicalDriftVector(
    calibration_deviation=0.2414,
    complexity_divergence=0.0,
    coherence_deviation=0.0,
    stability_deviation=0.2,
)


def _channel(pairs: int, error: float, freshness: str = "recent") -> dict:
    return {
        "schema": "agent_tactical_calibration_channel.v1",
        "evidence_status": "available",
        "n_verified_pairs": pairs,
        "calibration_error": error,
        "freshness_status": freshness,
        "default_reason": None,
    }


def _s_components(drift_norm, complexity_divergence):
    return compute_behavioral_sensor_components(
        decision_history=["approve"] * 5,
        coherence_history=[0.5] * 5,
        regime_history=["CONVERGENCE"] * 5,
        E_history=[0.7] * 5,
        I_history=[0.7] * 5,
        S_history=[0.3] * 5,
        V_history=[0.0] * 5,
        drift_norm=drift_norm,
        complexity_divergence=complexity_divergence,
    )


@pytest.fixture(autouse=True)
def _agent_local_on(monkeypatch):
    monkeypatch.setenv(SENSOR_DRIFT_AGENT_LOCAL_ENV, "1")
    monkeypatch.setenv("UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS", "1")


def test_the_frozen_vector_reproduces_the_live_constant():
    """Arithmetic check of the trace: the norm is 0.313 and SN1 S is 0.47."""
    assert FROZEN.norm == pytest.approx(0.3135, abs=1e-4)
    assert min(1.0, 1.5 * FROZEN.norm) == pytest.approx(0.470, abs=1e-3)


def test_the_drift_engine_never_moves_stability_without_a_decision():
    """decision=None (how compute_drift_vector calls it) pins stability at 0.2."""
    baseline = AgentBaseline(agent_id="probe")
    vectors = [
        compute_ethical_drift(
            agent_id="probe",
            baseline=baseline,
            current_coherence=0.5,
            current_confidence=0.7,
            complexity_divergence=None,
            calibration_error=0.2414,
            decision=None,
        )
        for _ in range(30)
    ]
    assert {round(v.stability_deviation, 12) for v in vectors[10:]} == {0.2}


def test_no_agent_local_component_means_no_drift_reading():
    record = compute_sensor_drift_input(FROZEN, complexity_divergence_observed=False)
    assert record["value"] is None
    assert record["full_norm"] == pytest.approx(FROZEN.norm)
    assert set(record["excluded"]) >= {
        "stability_deviation",
        "calibration_deviation",
        "coherence_deviation",
        "complexity_divergence",
        "agent_calibration_error",
    }
    assert record["used"] == {}


def test_unread_drift_and_divergence_leave_s_unobserved():
    """With no drift reading and no divergence, S has no observed base term."""
    record = compute_sensor_drift_input(FROZEN, complexity_divergence_observed=False)
    components = _s_components(record["value"], None)
    s_terms = components["dimensions"]["S"]["components"]
    assert not any(term["observed"] for term in s_terms)
    assert "S" in unread_axes(components)


def test_supplied_divergence_is_the_drift_input():
    vector = EthicalDriftVector(
        calibration_deviation=0.2414,
        complexity_divergence=0.35,
        coherence_deviation=0.05,
        stability_deviation=0.2,
    )
    record = compute_sensor_drift_input(vector, complexity_divergence_observed=True)
    assert record["used"] == {"complexity_divergence": pytest.approx(0.35)}
    assert record["value"] == pytest.approx(0.35)


def test_the_agents_own_calibration_counts_when_it_has_enough_fresh_pairs():
    record = compute_sensor_drift_input(
        FROZEN,
        complexity_divergence_observed=False,
        agent_calibration_channel=_channel(SENSOR_DRIFT_MIN_AGENT_CALIBRATION_PAIRS, 0.12),
    )
    assert record["used"] == {"agent_calibration_error": pytest.approx(0.12)}
    assert record["value"] == pytest.approx(0.12)


@pytest.mark.parametrize(
    "channel, reason",
    [
        (None, "no_agent_calibration_channel"),
        ({"evidence_status": "unavailable", "default_reason": "no_verified_agent_pairs"},
         "no_verified_agent_pairs"),
        (_channel(SENSOR_DRIFT_MIN_AGENT_CALIBRATION_PAIRS - 1, 0.12),
         "agent_calibration_below_min_pairs"),
        (_channel(40, 0.12, freshness="stale"), "agent_calibration_not_recent"),
        (_channel(40, 0.12, freshness="unknown"), "agent_calibration_not_recent"),
        (_channel(40, float("nan")), "agent_calibration_invalid"),
    ],
)
def test_unusable_agent_calibration_is_excluded_with_its_reason(channel, reason):
    record = compute_sensor_drift_input(
        FROZEN,
        complexity_divergence_observed=False,
        agent_calibration_channel=channel,
    )
    assert record["value"] is None
    assert record["excluded"]["agent_calibration_error"] == reason


def test_both_agent_local_parts_combine_as_a_norm():
    vector = EthicalDriftVector(complexity_divergence=0.3, stability_deviation=0.2)
    record = compute_sensor_drift_input(
        vector,
        complexity_divergence_observed=True,
        agent_calibration_channel=_channel(25, 0.4),
    )
    assert record["value"] == pytest.approx(math.hypot(0.3, 0.4))


def test_the_input_moves_with_the_agent_not_with_the_fleet():
    """Two agents with the same fleet scalar and different divergence differ."""
    low = EthicalDriftVector(calibration_deviation=0.2414, complexity_divergence=0.1,
                             stability_deviation=0.2)
    high = EthicalDriftVector(calibration_deviation=0.2414, complexity_divergence=0.6,
                              stability_deviation=0.2)
    a = compute_sensor_drift_input(low, complexity_divergence_observed=True)["value"]
    b = compute_sensor_drift_input(high, complexity_divergence_observed=True)["value"]
    assert a != b
    assert (a, b) == (pytest.approx(0.1), pytest.approx(0.6))


def test_flag_off_restores_the_full_norm(monkeypatch):
    monkeypatch.setenv(SENSOR_DRIFT_AGENT_LOCAL_ENV, "0")
    record = compute_sensor_drift_input(FROZEN, complexity_divergence_observed=False)
    assert record["mode"] == "full_norm"
    assert record["value"] == pytest.approx(FROZEN.norm)
    assert record["excluded"] == {}


def test_no_vector_is_no_reading_either_way(monkeypatch):
    assert compute_sensor_drift_input(None, complexity_divergence_observed=True)["value"] is None
    monkeypatch.setenv(SENSOR_DRIFT_AGENT_LOCAL_ENV, "0")
    assert compute_sensor_drift_input(None, complexity_divergence_observed=True)["value"] is None


def test_the_flag_is_part_of_the_instrument_config(monkeypatch):
    from src import eisv_instrument

    eisv_instrument._static_config.cache_clear()
    try:
        on = eisv_instrument._static_config()
        assert on.get("sensor_drift_agent_local") == "agent_local_v1"
        monkeypatch.setenv(SENSOR_DRIFT_AGENT_LOCAL_ENV, "0")
        eisv_instrument._static_config.cache_clear()
        assert "sensor_drift_agent_local" not in eisv_instrument._static_config()
    finally:
        eisv_instrument._static_config.cache_clear()
