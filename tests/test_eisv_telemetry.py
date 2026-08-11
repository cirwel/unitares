"""Tests for the versioned, observational EISV telemetry envelope."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.eisv_telemetry import (
    BEHAVIORAL_SENSOR_FORMULA_VERSION,
    EISV_TELEMETRY_SCHEMA,
    build_behavioral_derivation,
    build_eisv_telemetry_envelope,
    summarize_eisv_telemetry,
    summarize_state_eisv_telemetry,
)


def _derivation():
    return build_behavioral_derivation(
        decision_history=["old", "old", "proceed", "guide", "proceed"] * 3,
        coherence_history=[i / 20 for i in range(15)],
        regime_history=["STABLE", "STABLE", "EXPLORATION"] * 5,
        E_history=[0.4 + i / 100 for i in range(15)],
        I_history=[0.8 - i / 100 for i in range(15)],
        calibration_error=0.12,
        drift_norm=None,
        complexity_divergence=0.2,
        continuity_E_input=0.7,
        continuity_I_input=0.8,
        continuity_S_input=0.1,
        outcome_history=[{
            "outcome_type": "test_passed",
            "is_bad": False,
            "outcome_score": 0.9,
            "verification_source": "server_observation",
            "detail": {"response_text": "must not enter telemetry"},
        }],
        tool_error_rate=0.05,
        tool_call_velocity=1.2,
        unique_tools_ratio=0.4,
        computed={"E": 0.71, "I": 0.82, "S": 0.16, "V": -0.1},
    )


def test_behavioral_derivation_is_bounded_exact_and_privacy_reduced():
    trace = _derivation()

    assert trace["formula_version"] == BEHAVIORAL_SENSOR_FORMULA_VERSION
    assert len(trace["inputs"]["history"]["decision"]) == 10
    assert len(trace["inputs"]["history"]["coherence"]) == 10
    assert trace["inputs"]["features"]["calibration_error"] == 0.12
    assert "drift_norm" in trace["missing_inputs"]
    assert trace["unused_legacy_parameters"] == ["S_history", "V_history"]
    assert "detail" not in trace["inputs"]["outcomes"][0]
    assert "response_text" not in json.dumps(trace)


def test_full_envelope_keeps_measurement_policy_and_actuator_separate():
    envelope = build_eisv_telemetry_envelope(
        metrics={
            "E": 0.69, "I": 0.81, "S": 0.17, "V": -0.12,
            "primary_eisv_source": "behavioral",
            "ode": {"E": 0.51, "I": 0.53, "S": 0.31, "V": -0.02},
        },
        behavioral_snapshot={
            "E": 0.69, "I": 0.81, "S": 0.17, "V": -0.12,
            "confidence": 0.9, "updates": 42, "raw_obs": [0.71, 0.82, 0.16],
            "obs_source": "physical", "v_formula_version": 2,
        },
        submitted_sensor={"E": 0.71, "I": 0.82, "S": 0.16, "V": -0.1},
        submitted_source="physical",
        derivation=_derivation(),
        policy_evaluation={"action": "proceed", "sub_action": "guide"},
        enforcement={"requested": False, "applied": False, "mode": "advisory"},
        observed_at="2026-08-09T18:00:00+00:00",
        measurement_id="measurement-1",
    )

    assert envelope["schema"] == EISV_TELEMETRY_SCHEMA
    assert envelope["measurement"]["primary"]["values"]["E"] == 0.69
    assert envelope["measurement"]["behavioral"]["raw_observation"] == {
        "E": 0.71, "I": 0.82, "S": 0.16,
    }
    assert envelope["measurement"]["ode"]["values"]["E"] == 0.51
    assert envelope["policy_evaluation"]["action"] == "proceed"
    assert envelope["enforcement"]["applied"] is False
    json.dumps(envelope)  # no default=str escape hatch required


def test_summary_prefers_consumed_observation_source_and_stays_compact():
    envelope = build_eisv_telemetry_envelope(
        metrics={"E": 0.6, "I": 0.8, "S": 0.2, "V": -0.2,
                 "primary_eisv_source": "behavioral"},
        behavioral_snapshot={
            "E": 0.6, "I": 0.8, "S": 0.2, "V": -0.2,
            "confidence": 0.8, "raw_obs": [0.7, 0.8, 0.2],
            "obs_source": "physical",
        },
        submitted_sensor={"E": 0.7, "I": 0.8, "S": 0.2, "V": -0.1},
        submitted_source="physical",
        derivation=_derivation(),
        policy_evaluation={
            "action": "pause",
            "sub_action": "risk_pause",
            "inputs": {"verdict_source": "behavioral_assessment"},
            "maturity_gate": {
                "measurement_phase": "behavioral_ready",
                "measurement_ready": True,
                "outcome": "ineligible",
                "eligible": False,
                "would_defer": False,
                "confirmation_count": 0,
                "confirmations_required": 2,
                "actuation_enabled": False,
                "actuation_ready": False,
                "actuation_applied": False,
            },
            "epistemic_gate": {
                "applied": False,
                "epistemic_class": "agent_report",
                "ineligibility_reason": "agent_authored_report",
            },
        },
        enforcement={
            "requested": True,
            "applied": True,
            "basis": "risk_policy",
        },
    )

    summary = summarize_eisv_telemetry(envelope)
    assert summary["measurement_source"] == "physical"
    assert summary["primary_source"] == "behavioral"
    assert summary["behavioral_confidence"] == 0.8
    assert summary["verdict_source"] == "behavioral_assessment"
    assert summary["maturity_gate_outcome"] == "ineligible"
    assert summary["maturity_gate_would_defer"] is False
    assert summary["confirmation_count"] == 0
    assert summary["confirmations_required"] == 2
    assert summary["actuation_applied"] is False
    assert summary["epistemic_guard_applied"] is False
    assert summary["epistemic_guard_class"] == "agent_report"
    assert summary["epistemic_guard_ineligibility_reason"] == (
        "agent_authored_report"
    )
    assert summary["enforcement_basis"] == "risk_policy"
    assert summary["enforcement_requested"] is True
    assert summary["enforcement_applied"] is True
    assert "inputs" not in summary


def test_summary_labels_ode_fallback_values_during_behavioral_warmup():
    envelope = build_eisv_telemetry_envelope(
        metrics={
            "E": 0.51, "I": 0.53, "S": 0.31, "V": -0.02,
            "primary_eisv_source": "ode_fallback",
        },
        behavioral_snapshot={
            "E": 0.65, "I": 0.75, "S": 0.2, "V": -0.1,
            "confidence": 0.2, "raw_obs": [0.7, 0.8, 0.2],
            "obs_source": "physical",
        },
        submitted_sensor={"E": 0.7, "I": 0.8, "S": 0.2, "V": -0.1},
        submitted_source="physical",
        derivation=_derivation(),
        policy_evaluation={"action": "proceed"},
        enforcement={"requested": False, "applied": False},
    )

    summary = summarize_eisv_telemetry(envelope)
    assert summary["measurement_source"] == "ode_fallback"
    assert summary["behavioral_source"] == "physical"


def test_legacy_rows_are_labeled_incomplete_instead_of_inventing_provenance():
    summary = summarize_state_eisv_telemetry({
        "behavioral_eisv": {"confidence": 0.75, "obs_source": "behavioral_sensor"},
        "sensor_eisv_source": "behavioral",
        "action": "guide",
    })

    assert summary["schema"] == "eisv.telemetry.summary.legacy"
    assert summary["measurement_source"] == "behavioral_sensor"
    assert summary["missing_inputs"] == ["eisv_telemetry"]
    assert summary["epistemic_guard_applied"] is None
    assert summary["enforcement_requested"] is None


@pytest.mark.asyncio
async def test_websocket_event_carries_summary_not_full_derivation():
    from src.broadcaster import broadcaster_instance
    from src.mcp_handlers.updates.context import UpdateContext
    from src.mcp_handlers.updates.enrichments import enrich_websocket_broadcast

    envelope = build_eisv_telemetry_envelope(
        metrics={"E": 0.6, "I": 0.8, "S": 0.2, "V": -0.2,
                 "primary_eisv_source": "behavioral"},
        behavioral_snapshot={
            "E": 0.6, "I": 0.8, "S": 0.2, "V": -0.2,
            "confidence": 0.8, "raw_obs": [0.7, 0.8, 0.2],
            "obs_source": "behavioral_sensor",
        },
        submitted_sensor={"E": 0.7, "I": 0.8, "S": 0.2, "V": -0.1},
        submitted_source="behavioral",
        derivation=_derivation(),
        policy_evaluation={"action": "proceed", "sub_action": "guide"},
        enforcement={"requested": False, "applied": False},
    )
    ctx = UpdateContext(
        arguments={},
        agent_uuid="agent-uuid",
        agent_id="agent-uuid",
        declared_agent_id="Agent_1",
        label="Agent 1",
        complexity=0.4,
        confidence=0.8,
        ethical_drift=[0.0, 0.0, 0.0],
        response_data={
            "metrics": {
                "E": 0.6, "I": 0.8, "S": 0.2, "V": -0.2,
                "coherence": 0.5, "risk_score": 0.1,
            },
            "decision": {"action": "proceed", "sub_action": "guide"},
            "eisv_telemetry": envelope,
        },
    )
    ctx.mcp_server = MagicMock(agent_metadata={})

    with patch.object(
        broadcaster_instance, "broadcast", new_callable=AsyncMock
    ) as broadcast:
        await enrich_websocket_broadcast(ctx)

    event = broadcast.await_args.args[0]
    assert event["eisv_telemetry"]["measurement_source"] == "behavioral_sensor"
    assert event["eisv_telemetry"]["policy_sub_action"] == "guide"
    assert "derivation" not in event["eisv_telemetry"]


@pytest.mark.asyncio
async def test_post_update_persists_the_same_envelope_exposed_in_full_result():
    from types import SimpleNamespace

    from src.behavioral_state import BehavioralEISV
    from src.mcp_handlers.updates import phases
    from src.mcp_handlers.updates.context import UpdateContext

    behavioral = BehavioralEISV()
    behavioral.update(0.7, 0.8, 0.2)
    monitor = SimpleNamespace(
        _behavioral_state=behavioral,
        _behavioral_obs_source="behavioral_sensor",
    )
    ctx = UpdateContext(
        agent_id="agent-1",
        agent_uuid="agent-1",
        epistemic_class="agent_report",
        metrics_dict={
            "E": behavioral.E, "I": behavioral.I, "S": behavioral.S,
            "V": behavioral.V, "primary_eisv_source": "behavioral",
            "ode": {"E": 0.5, "I": 0.5, "S": 0.3, "V": 0.0},
            "regime": "EXPLORATION", "coherence": 0.5,
            "risk_score": 0.1, "phi": 0.0, "verdict": "safe",
        },
        result={
            "timestamp": "2026-08-09T18:00:00+00:00",
            "decision": {"action": "proceed", "sub_action": "guide"},
            "policy_evaluation": {"action": "proceed", "sub_action": "guide"},
            "enforcement": {"requested": False, "applied": False},
        },
        monitor=monitor,
        health_status=SimpleNamespace(value="healthy"),
        risk_score=0.1,
        agent_state={"_eisv_derivation": _derivation()},
    )
    ctx.mcp_server = MagicMock()

    with patch.object(
        phases.agent_storage, "record_agent_state", new_callable=AsyncMock
    ) as record:
        assert await phases._post_update_record_state(ctx) is True

    persisted = record.await_args.kwargs["eisv_telemetry"]
    assert persisted is ctx.result["eisv_telemetry"]
    assert persisted["policy_evaluation"]["sub_action"] == "guide"
    assert persisted["measurement"]["behavioral"]["raw_observation"]["E"] == 0.7
