"""Friendly sync_state keeps capture evidence separate from operational advice."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcp.types import TextContent
import pytest

from src.checkpoints import CheckpointReceipt
from src.mcp_handlers.middleware import DispatchContext
from src.mcp_handlers.middleware.envelope_step import (
    apply_experience_envelope,
    build_experience_envelope,
)
from src.mcp_handlers.response_formatter import format_response
from src.services.checkpoint_service import (
    checkpoint_boundary_response,
    checkpoint_response,
)
from src.services.update_response_service import serialize_process_update_response
from src.state_persistence import StatePersistenceReceipt
from tests.helpers import parse_result


_MODES = ["auto", "compact", "mirror", "standard", "minimal", "full", "lite", "interpreted", "verbose"]
_AGENT = "88d266a0-c058-4053-bc38-13f6bcfb7551"
_CHECKPOINT = "bd7fd3ae-10d7-4124-9b55-d2f5d44ed6f7"
_ATTEMPT = "c9d2be90-f0ca-42b6-b8b6-8c00a42ff151"


@pytest.fixture(autouse=True)
def _isolate_later_notices(monkeypatch):
    monkeypatch.setattr(
        "src.services.model_observations.attach_model_observation",
        AsyncMock(side_effect=lambda result: result),
    )
    monkeypatch.setattr(
        "src.knowledge_notices.attach_notices",
        AsyncMock(side_effect=lambda name, arguments, ctx, result: result),
    )


def _service_result(kind, mode="compact", *, state_receipt=None):
    receipt = CheckpointReceipt(
        checkpoint_id=_CHECKPOINT, identity_id=41, seq=1, prev_checkpoint_id=None,
        recorded_at=datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc),
        accepted_attempt_id=_ATTEMPT, replayed=kind == "replay", retry_safe=True,
    )
    ctx = SimpleNamespace(
        agent_id=_AGENT, checkpoint_receipt=None if kind == "unconfirmed" else receipt,
        checkpoint_unconfirmed=kind == "unconfirmed", checkpoint_key="stable-key",
        checkpoint_attempt_id=_ATTEMPT, checkpoint_assessment=SimpleNamespace(claimed=True),
        checkpoint_assessment_status={
            "accepted": "completed", "failed": "failed", "replay": "not_repeated",
            "completion_unconfirmed": "unconfirmed", "unconfirmed": None,
        }[kind],
    )
    if kind in {"accepted", "completion_unconfirmed"}:
        source = {
            "status": "healthy", "agent_id": _AGENT,
            "decision": {"action": "proceed", "reason": "Assessment returned"},
            "metrics": {"E": 0.7, "I": 0.6, "S": 0.2, "V": 0.0, "risk_score": 0.2},
            "prediction_id": "pred-current",
        }
        arguments = {"response_mode": mode}
        formatted = format_response(deepcopy(source), arguments)
        return serialize_process_update_response(
            response_data=formatted, agent_uuid=_AGENT, arguments=arguments,
            fallback_result=source, state_persistence=state_receipt,
            checkpoint=checkpoint_response(ctx),
        )
    return checkpoint_boundary_response(
        ctx,
        code={
            "replay": "CHECKPOINT_REPLAY", "failed": "CHECKPOINT_ASSESSMENT_FAILED",
            "unconfirmed": "CHECKPOINT_UNCONFIRMED",
        }[kind],
        message={
            "replay": "Returning the original checkpoint without repeating assessment.",
            "failed": "Checkpoint accepted; assessment did not complete.",
            "unconfirmed": "Checkpoint acceptance could not be confirmed.",
        }[kind],
        replay=kind == "replay",
    )


def _result(payload):
    return [TextContent(type="text", text=json.dumps(payload))]


async def _normalize(raw, mode="compact", *, invoked="sync_state"):
    return parse_result(await apply_experience_envelope(
        "process_agent_update", {"response_mode": mode},
        DispatchContext(original_name=invoked), raw,
    ))


def _assert_no_operational_fields(payload):
    forbidden = {
        "action", "decision", "action_summary", "state_summary", "next_action",
        "risk_summary", "metrics", "prediction_id", "recovery_hint", "legacy_diagnostics",
    }
    assert forbidden.isdisjoint(payload)
    if "raw_governance" in payload:
        _assert_no_operational_fields(payload["raw_governance"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("state_id", [41, None])
async def test_real_alias_step_preserves_checkpoint_and_independent_state_receipt(mode, state_id):
    state_receipt = StatePersistenceReceipt(state_id)
    raw = _service_result("accepted", mode, state_receipt=state_receipt)
    source = parse_result(raw)

    envelope = await _normalize(raw, mode)

    assert envelope["tool"] == "sync_state"
    assert envelope["checkpoint"] == source["checkpoint"]
    assert envelope["action_summary"]["action"] == "proceed"
    assert envelope["state_persistence"] == state_receipt.to_dict()
    assert envelope["checkpoint"]["acceptance_retry_idempotent"] is True
    assert envelope["state_persistence"]["checkin_retry_idempotent"] is False
    if mode in {"full", "verbose"}:
        assert envelope["raw_governance"]["checkpoint"] == source["checkpoint"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "compact", "full", "verbose"])
@pytest.mark.parametrize("kind", ["replay", "failed", "unconfirmed"])
async def test_receipt_only_service_responses_never_infer_operational_permission(kind, mode):
    raw = _service_result(kind, mode)
    source = parse_result(raw)

    envelope = await _normalize(raw, mode)

    assert envelope["checkpoint"] == source["checkpoint"]
    assert envelope["success"] == source["success"]
    if kind != "replay":
        assert envelope["error_code"] == source["error_code"]
        assert envelope["error"] == source["error"]
    else:
        assert envelope["message"] == source["message"]
    _assert_no_operational_fields(envelope)
    assert "Keep working" not in json.dumps(envelope)
    assert "state_persistence" not in envelope


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["replay", "failed", "unconfirmed"])
@pytest.mark.parametrize("nested", [False, True])
async def test_receipt_only_projection_discards_stale_state_even_in_full_mode(kind, nested):
    source = parse_result(_service_result(kind))
    source.update(
        prediction_id="stale-prediction", decision={"action": "proceed"},
        metrics={"risk_score": 0.1}, next_action="Keep working", private_blob="x" * 20_000,
    )
    payload = source
    if nested:
        payload = {
            "success": True, "raw_governance": source,
            "prediction_id": "outer-stale-prediction", "decision": {"action": "proceed"},
            "checkpoint": {"status": "outer-stale-receipt"},
        }

    envelope = await _normalize(_result(payload), "full")

    assert envelope["checkpoint"] == source["checkpoint"]
    _assert_no_operational_fields(envelope)
    encoded = json.dumps(envelope)
    assert "stale-prediction" not in encoded
    assert "private_blob" not in encoded
    assert len(encoded) < 2500


@pytest.mark.asyncio
async def test_current_assessment_remains_visible_when_completion_ack_is_unconfirmed():
    raw = _service_result("completion_unconfirmed", "full")

    envelope = await _normalize(raw, "full")

    assert envelope["checkpoint"]["assessment"]["status"] == "unconfirmed"
    assert envelope["action_summary"]["action"] == "proceed"
    assert envelope["prediction_id"] == "pred-current"


@pytest.mark.asyncio
async def test_accepted_receipt_without_current_decision_is_neutral():
    source = parse_result(_service_result("accepted"))
    # No estimator result is present; the acceptance receipt alone cannot
    # justify the generic check-in continuation wording.
    source = {"success": True, "checkpoint": source["checkpoint"]}

    envelope = await _normalize(_result(source))

    assert envelope["checkpoint"] == source["checkpoint"]
    _assert_no_operational_fields(envelope)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["minimal", "full"])
async def test_current_decision_is_preserved_without_optional_assessment_status(mode):
    source = parse_result(_service_result("accepted", mode))
    source["checkpoint"].pop("assessment")

    envelope = await _normalize(_result(source), mode)

    assert envelope["checkpoint"] == source["checkpoint"]
    assert envelope["action_summary"]["action"] == "proceed"


@pytest.mark.parametrize("mode", ["compact", "full"])
def test_receipt_projection_allowlists_keys_and_bounds_optional_assessment(mode):
    source = parse_result(_service_result("accepted", mode))
    expected = deepcopy(source["checkpoint"])
    source["checkpoint"]["private_extra"] = "x" * 20_000
    source["checkpoint"]["assessment"]["private_extra"] = {"secret": "do not lift"}

    envelope = build_experience_envelope("sync_state", "process_agent_update", source, {"response_mode": mode})

    assert envelope["checkpoint"] == expected
    assert "private_extra" not in json.dumps(envelope)
    source["checkpoint"]["assessment"]["status"] = []
    bounded = build_experience_envelope("sync_state", "process_agent_update", source, {"response_mode": mode})
    assert bounded["checkpoint"]["checkpoint_id"] == _CHECKPOINT
    assert "assessment" not in bounded["checkpoint"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", "unknown.v1"), ("status", "recorded"),
        ("checkpoint_id", "not-a-uuid"), ("event_id", _ATTEMPT),
        ("accepted_attempt_id", "x" * 20_000), ("identity_id", True),
        ("seq", 0), ("recorded_at", "2026-11-02T12:00:00"),
        ("prev_checkpoint_id", {}), ("idempotent_replay", "false"),
        ("acceptance_retry_idempotent", 1),
    ],
)
def test_malformed_receipt_is_not_lifted_or_preserved_as_full_receipt(field, value):
    source = parse_result(_service_result("accepted", "full"))
    source["checkpoint"][field] = value

    envelope = build_experience_envelope("sync_state", "process_agent_update", source, {"response_mode": "full"})

    assert "checkpoint" not in envelope
    assert "checkpoint" not in envelope["raw_governance"]


def test_nested_canonical_receipt_wins_and_missing_one_does_not_revive_outer_copy():
    source = parse_result(_service_result("accepted", "compact"))
    stale = CheckpointReceipt(
        checkpoint_id=_ATTEMPT, identity_id=99, seq=1, prev_checkpoint_id=None,
        recorded_at=datetime(2026, 11, 2, tzinfo=timezone.utc),
        accepted_attempt_id=_CHECKPOINT, replayed=False, retry_safe=True,
    ).to_dict()
    payload = {"raw_governance": source, "checkpoint": stale}

    envelope = build_experience_envelope("sync_state", "process_agent_update", payload, {"response_mode": "compact"})
    assert envelope["checkpoint"] == source["checkpoint"]
    source.pop("checkpoint")
    absent = build_experience_envelope("sync_state", "process_agent_update", payload, {"response_mode": "compact"})
    assert "checkpoint" not in absent


@pytest.mark.asyncio
async def test_canonical_invocation_and_unrelated_errors_stay_unchanged():
    canonical = _service_result("replay")
    untouched = await apply_experience_envelope(
        "process_agent_update", {}, DispatchContext(original_name="process_agent_update"), canonical,
    )
    assert untouched is canonical
    error = _result({"success": False, "error": "an unrelated refusal", "recovery": {"action": "retry"}})
    untouched = await apply_experience_envelope(
        "process_agent_update", {}, DispatchContext(original_name="sync_state"), error,
    )
    assert untouched is error


def test_budget_pressure_does_not_remove_checkpoint(monkeypatch):
    from src.mcp_handlers.middleware import envelope_step

    source = parse_result(_service_result("accepted", "compact"))
    monkeypatch.setattr(envelope_step, "_SYNC_ROUTINE_BUDGET_BYTES", 1)
    envelope = build_experience_envelope("sync_state", "process_agent_update", source, {"response_mode": "compact"})
    assert envelope["checkpoint"] == source["checkpoint"]
    assert "_response_size" in envelope
