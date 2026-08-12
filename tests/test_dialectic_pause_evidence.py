"""Decision-time evidence supplied to dialectic reviewers."""

from types import SimpleNamespace

from src.mcp_handlers.dialectic.handlers import _capture_pause_evidence


def test_pause_evidence_prefers_persisted_telemetry_envelope_fields():
    measurement = {
        "primary": {
            "source": "behavioral",
            "values": {"E": 0.31, "I": 0.82, "S": 0.74, "V": -0.51},
        },
        "ode": {
            "source": "ode_diagnostic",
            "values": {"E": 0.7, "I": 0.7, "S": 0.1, "V": 0.0},
        },
    }
    policy = {
        "action": "pause",
        "inputs": {
            "risk_score": 0.81,
            "verdict": "high-risk",
            "verdict_source": "behavioral",
        },
    }
    enforcement = {"requested": True, "requested_action": "pause"}
    monitor = SimpleNamespace(_last_governance_result={
        "timestamp": "2026-08-11T20:00:00Z",
        "metrics": {"E": 0.99},  # must not override telemetry
        "eisv_telemetry": {
            "measurement_id": "measurement-1",
            "observed_at": "2026-08-11T19:59:59Z",
            "measurement": measurement,
            "policy_evaluation": policy,
            "enforcement": enforcement,
        },
        "risk_attribution": {"primary_driver": "behavioral"},
    })

    evidence = _capture_pause_evidence(monitor)

    assert evidence["evidence_status"] == "available"
    assert evidence["measurement"] == measurement
    assert evidence["policy_evaluation"] == policy
    assert evidence["enforcement"] == enforcement
    assert evidence["measurement_id"] == "measurement-1"
    assert evidence["observed_at"] == "2026-08-11T19:59:59Z"
    assert evidence["risk_attribution"] == {"primary_driver": "behavioral"}


def test_pause_evidence_builds_behavioral_first_measurement_without_telemetry():
    monitor = SimpleNamespace(_last_governance_result={
        "timestamp": "2026-08-11T20:00:00Z",
        "metrics": {
            "E": 0.4,
            "I": 0.8,
            "S": 0.6,
            "V": -0.4,
            "primary_eisv_source": "behavioral",
            "ode": {"E": 0.7, "I": 0.7, "S": 0.1, "V": 0.0},
        },
        "policy_evaluation": {"action": "pause"},
        "enforcement": {"requested": True},
    })

    evidence = _capture_pause_evidence(monitor)

    assert evidence["measurement"]["primary"] == {
        "source": "behavioral",
        "values": {"E": 0.4, "I": 0.8, "S": 0.6, "V": -0.4},
    }
    assert evidence["measurement"]["ode"]["source"] == "ode_diagnostic"
    assert evidence["policy_evaluation"]["action"] == "pause"


def test_pause_evidence_never_substitutes_bare_diagnostic_state():
    diagnostic_state = SimpleNamespace(
        to_dict=lambda: {"E": 0.7, "I": 0.7, "S": 0.1, "V": 0.0}
    )
    evidence = _capture_pause_evidence(SimpleNamespace(state=diagnostic_state))

    assert evidence["evidence_status"] == "unavailable"
    assert "measurement" not in evidence
    assert "diagnostic ODE state was intentionally not substituted" in evidence["limitation"]
