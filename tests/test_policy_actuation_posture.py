"""The EISV policy instrument has no lifecycle authority by default."""

from __future__ import annotations

import pytest


def _pause_result() -> dict:
    return {
        "decision": {
            "action": "pause",
            "sub_action": "risk_pause",
            "reason": "risk observation crossed the policy threshold",
            "guidance": "inspect the observation",
            "critical": True,
        },
        "policy_evaluation": {
            "policy_name": "monitor_decision",
            "action": "pause",
            "sub_action": "risk_pause",
        },
        "enforcement": {
            "schema": "governance.enforcement.v1",
            "scope": "runtime_circuit_breaker",
            "requested": True,
            "applied": False,
            "mode": "circuit_breaker_candidate",
            "basis": "policy_request",
        },
    }


def test_default_posture_keeps_pause_observational(monkeypatch):
    from src.agent_loop_detection import apply_policy_actuation_posture

    monkeypatch.delenv("UNITARES_POLICY_ACTUATION", raising=False)
    result = _pause_result()

    assert apply_policy_actuation_posture(result) is False
    assert result["policy_evaluation"]["action"] == "pause"
    assert result["decision"]["action"] == "proceed"
    assert result["decision"]["sub_action"] == "guide"
    assert result["decision"]["recommended_action"] == "pause"
    assert result["decision"]["recommended_sub_action"] == "risk_pause"
    assert result["decision"]["advisory"] is True
    assert result["enforcement"] == {
        "schema": "governance.enforcement.v1",
        "scope": "runtime_circuit_breaker",
        "posture": "observe",
        "requested": True,
        "recommended_action": "pause",
        "effective_action": "proceed",
        "applied": False,
        "mode": "observation_only",
        "basis": "posture_observe",
        "policy_basis": "policy_request",
        "actor": None,
        "effect": None,
        "actuation_id": None,
        "applied_at": None,
        "note": (
            "Policy recommended a pause, but this deployment keeps inferred "
            "policy observational. No lifecycle state or write gate changed."
        ),
    }


def test_enforce_posture_preserves_legacy_pause(monkeypatch):
    from src.agent_loop_detection import apply_policy_actuation_posture

    monkeypatch.setenv("UNITARES_POLICY_ACTUATION", "enforce")
    result = _pause_result()

    assert apply_policy_actuation_posture(result) is True
    assert result["decision"]["action"] == "pause"
    assert result["policy_evaluation"]["action"] == "pause"
    assert result["enforcement"]["posture"] == "enforce"
    assert result["enforcement"]["effective_action"] == "pause"
    assert result["enforcement"]["mode"] == "circuit_breaker_candidate"


def test_observe_posture_does_not_turn_proceed_into_a_safety_claim(monkeypatch):
    from src.agent_loop_detection import apply_policy_actuation_posture

    monkeypatch.setenv("UNITARES_POLICY_ACTUATION", "observe")
    result = {
        "decision": {"action": "proceed", "sub_action": "approve"},
        "policy_evaluation": {"action": "proceed"},
    }

    assert apply_policy_actuation_posture(result) is False
    assert result["decision"] == {"action": "proceed", "sub_action": "approve"}
    assert result["enforcement"]["posture"] == "observe"
    assert result["enforcement"]["requested"] is False
    assert result["enforcement"]["applied"] is False


def test_invalid_posture_fails_instead_of_silently_changing_authority(monkeypatch):
    from src.agent_loop_detection import apply_policy_actuation_posture

    monkeypatch.setenv("UNITARES_POLICY_ACTUATION", "maybe")

    with pytest.raises(ValueError, match="must be one of enforce, observe"):
        apply_policy_actuation_posture(_pause_result())


@pytest.mark.asyncio
async def test_observe_pause_never_reaches_lifecycle_actuator(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock, patch

    from src import agent_loop_detection as ald
    from src import agent_storage
    from tests.test_lifecycle_clobber import AGENT_ID, make_agent_meta, make_monitor

    monkeypatch.setenv("UNITARES_POLICY_ACTUATION", "observe")
    meta = make_agent_meta(status="active")
    meta.recent_update_timestamps = []
    meta.recent_decisions = []
    meta.loop_cooldown_until = None
    meta.add_recent_update = MagicMock()
    meta.add_lifecycle_event = MagicMock()
    monitor = make_monitor()
    monitor.process_update = MagicMock(return_value=_pause_result())
    mock_db = MagicMock()
    mock_db.increment_update_count = AsyncMock(return_value=42)
    pause = AsyncMock()

    with patch.object(ald, "agent_metadata", {AGENT_ID: meta}), \
         patch.object(ald, "monitors", {AGENT_ID: monitor}), \
         patch.object(ald, "verify_agent_ownership", return_value=(True, None)), \
         patch.object(ald, "detect_loop_pattern", return_value=(False, "")), \
         patch("src.agent_lifecycle.get_or_create_monitor", return_value=monitor), \
         patch.object(agent_storage, "get_db", return_value=mock_db), \
         patch.object(agent_storage, "pause_agent_with_token", pause), \
         patch.object(ald, "save_monitor_state_async", AsyncMock()):
        result = await ald.process_update_authenticated_async(
            AGENT_ID, "test-key", {"task_type": "mixed"}
        )

    pause.assert_not_awaited()
    assert meta.status == "active"
    assert meta.paused_at is None
    assert result["decision"]["action"] == "proceed"
    assert result["policy_evaluation"]["action"] == "pause"
    assert result["enforcement"]["applied"] is False
    assert result["enforcement"]["posture"] == "observe"
    meta.add_lifecycle_event.assert_not_called()
