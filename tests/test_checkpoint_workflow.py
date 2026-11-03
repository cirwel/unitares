"""Real check-in phases with a deterministic checkpoint storage boundary.

These tests exercise workflow ownership and returned receipts, not PostgreSQL
transaction semantics. Admission's real auth/loop gates have their own suite;
the estimator edge here calls the new admission callback before computing.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from mcp.types import TextContent
from pydantic import ValidationError
import pytest

from src.checkpoints import (
    CheckpointAssessmentClaim,
    CheckpointConflict,
    CheckpointReceipt,
    checkpoint_digests,
)
from src.mcp_handlers.response_base import success_response
from src.mcp_handlers.schemas.core import ProcessAgentUpdateParams
from src.services.checkpoint_service import capture_checkpoint_request
from src.services.update_workflow_service import run_process_update_workflow
from tests.helpers import parse_result
from tests.test_update_workflow_service import (
    _make_real_spine_harness,
    _patch_real_spine_edges,
)


class _CheckpointLedger:
    """Canonical frozen receipts and durable claim states across requests."""

    def __init__(self):
        self.bindings = {}
        self.claims = {}
        self.accepted = []
        self.accept_ack_error = None
        self.claim_ack_error = None
        self.claim_error_before_write = None
        self.finish_ack_error = None

    async def accept(self, **kwargs):
        key = kwargs["idempotency_key"]
        key_digest, digest = checkpoint_digests(
            b"c" * 32, key or str(uuid4()), kwargs["request_payload"],
        )
        binding = (kwargs["identity_id"], key_digest)
        if binding in self.bindings:
            prior_digest, receipt = self.bindings[binding]
            if digest != prior_digest:
                raise CheckpointConflict(receipt.checkpoint_id)
            return replace(receipt, replayed=True)
        receipt = CheckpointReceipt(
            checkpoint_id=str(uuid4()), identity_id=kwargs["identity_id"],
            seq=len(self.accepted) + 1,
            prev_checkpoint_id=self.accepted[-1].checkpoint_id if self.accepted else None,
            recorded_at=datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc),
            accepted_attempt_id=kwargs["attempt_id"], replayed=False,
            retry_safe=key is not None,
        )
        self.bindings[binding] = digest, receipt
        self.accepted.append(receipt)
        if self.accept_ack_error is not None:
            error, self.accept_ack_error = self.accept_ack_error, None
            raise error
        return receipt

    async def claim(self, *, checkpoint_id, assessor):
        if self.claim_error_before_write is not None:
            raise self.claim_error_before_write
        if checkpoint_id in self.claims:
            return replace(self.claims[checkpoint_id], claimed=False)
        claim = CheckpointAssessmentClaim(
            checkpoint_id=checkpoint_id, assessor=assessor, status="started",
            claim_token=str(uuid4()), claimed=True,
        )
        self.claims[checkpoint_id] = claim
        if self.claim_ack_error is not None:
            raise self.claim_ack_error
        return claim

    async def finish(self, *, checkpoint_id, assessor, claim_token, status):
        claim = self.claims[checkpoint_id]
        assert claim.assessor == assessor
        assert claim.claim_token == claim_token
        self.claims[checkpoint_id] = replace(claim, status=status)
        if self.finish_ack_error is not None:
            raise self.finish_ack_error
        return True


def _harness(ledger, *, text="Completed the implementation.", key="work-unit-1", **arguments):
    harness = _make_real_spine_harness(response_text=text)
    harness.ctx.arguments.update(idempotency_key=key, **arguments)
    harness.db.get_identity = AsyncMock(return_value=SimpleNamespace(identity_id=41))
    harness.db.accept_checkpoint = AsyncMock(side_effect=ledger.accept)
    harness.db.claim_checkpoint_assessment = AsyncMock(side_effect=ledger.claim)
    harness.db.finish_checkpoint_assessment = AsyncMock(side_effect=ledger.finish)
    result = deepcopy(harness.server.process_update_authenticated_async.return_value)
    harness.assess = AsyncMock(return_value=result)

    async def admitted_estimator(**kwargs):
        before_assessment = kwargs.pop("before_assessment")
        await before_assessment()
        return await harness.assess(**kwargs)

    harness.server.process_update_authenticated_async = AsyncMock(side_effect=admitted_estimator)
    return harness


async def _run(harness, *, serializer=None):
    with _patch_real_spine_edges(harness), patch(
        "src.mcp_handlers.context.get_session_proof_origin", return_value="caller_asserted",
    ):
        return parse_result(await run_process_update_workflow(harness.ctx, serializer=serializer))


def _no_assessment_effects(harness):
    harness.assess.assert_not_awaited()
    harness.storage.record_agent_state.assert_not_awaited()
    harness.db.record_outcome_event.assert_not_awaited()
    harness.db.finish_checkpoint_assessment.assert_not_awaited()


def _no_operational_verdict(data):
    for key in ("action", "decision", "metrics", "prediction_id", "state_persistence", "outcome_event"):
        assert key not in data


@pytest.mark.asyncio
async def test_keyed_retry_returns_original_receipt_without_repeating_effects():
    ledger = _CheckpointLedger()
    first = _harness(ledger)
    accepted = await _run(first)
    retry = _harness(ledger)
    replay = await _run(retry)

    assert len(ledger.accepted) == 1
    assert accepted["checkpoint"]["status"] == "accepted"
    assert accepted["checkpoint"]["assessment"]["status"] == "completed"
    for field in ("checkpoint_id", "seq", "recorded_at", "accepted_attempt_id"):
        assert replay["checkpoint"][field] == accepted["checkpoint"][field]
    assert replay["checkpoint"]["idempotent_replay"] is True
    assert replay["checkpoint"]["assessment"]["status"] == "not_repeated"
    assert replay["assessment_repeated"] is False
    assert retry.ctx.checkpoint_attempt_id != first.ctx.checkpoint_attempt_id
    first.assess.assert_awaited_once()
    first.storage.record_agent_state.assert_awaited_once()
    first.db.record_outcome_event.assert_awaited_once()
    retry.db.claim_checkpoint_assessment.assert_not_awaited()
    _no_assessment_effects(retry)
    _no_operational_verdict(replay)


@pytest.mark.asyncio
async def test_conflicting_key_stops_before_assessment_and_state_outcome_writes():
    ledger = _CheckpointLedger()
    await _run(_harness(ledger))
    changed = _harness(ledger, text="A different unit of work.")

    result = await _run(changed)

    assert result["error_code"] == "CHECKPOINT_KEY_CONFLICT"
    assert result["success"] is False
    assert result["checkpoint"] is None
    assert len(ledger.accepted) == 1
    changed.db.claim_checkpoint_assessment.assert_not_awaited()
    _no_assessment_effects(changed)
    _no_operational_verdict(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("estimator broke"), ValueError("invalid estimate")])
async def test_assessment_failure_preserves_acceptance_without_synthetic_action(failure):
    ledger = _CheckpointLedger()
    harness = _harness(ledger)
    harness.assess.side_effect = failure

    result = await _run(harness)

    assert result["success"] is False
    assert result["error_code"] == "CHECKPOINT_ASSESSMENT_FAILED"
    assert result["checkpoint"]["status"] == "accepted"
    assert result["checkpoint"]["checkpoint_id"] == ledger.accepted[0].checkpoint_id
    assert result["checkpoint"]["assessment"]["status"] == "failed"
    assert ledger.claims[ledger.accepted[0].checkpoint_id].status == "failed"
    harness.storage.record_agent_state.assert_not_awaited()
    harness.db.record_outcome_event.assert_not_awaited()
    _no_operational_verdict(result)

    retry = _harness(ledger)
    replay = await _run(retry)
    assert replay["checkpoint"]["checkpoint_id"] == result["checkpoint"]["checkpoint_id"]
    _no_assessment_effects(retry)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["work-unit-1", None])
async def test_lost_acceptance_ack_is_unconfirmed_before_estimator(key):
    ledger = _CheckpointLedger()
    ledger.accept_ack_error = ConnectionError("commit acknowledgement lost")
    harness = _harness(ledger, key=key)

    result = await _run(harness)

    assert len(ledger.accepted) == 1  # Simulate commit followed by connection loss.
    assert result["error_code"] == "CHECKPOINT_UNCONFIRMED"
    assert result["checkpoint"]["status"] == "unconfirmed"
    assert result["checkpoint"]["acceptance_retry_idempotent"] is (key is not None)
    harness.db.claim_checkpoint_assessment.assert_not_awaited()
    _no_assessment_effects(harness)
    _no_operational_verdict(result)

    if key is not None:
        retry = _harness(ledger, key=key)
        replay = await _run(retry)
        assert replay["checkpoint"]["checkpoint_id"] == ledger.accepted[0].checkpoint_id
        assert len(ledger.accepted) == 1
        _no_assessment_effects(retry)


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_ack", [False, True])
async def test_claim_failure_preserves_checkpoint_and_never_runs_estimator(lost_ack):
    ledger = _CheckpointLedger()
    error = ConnectionError("claim acknowledgement unavailable")
    if lost_ack:
        ledger.claim_ack_error = error
    else:
        ledger.claim_error_before_write = error
    harness = _harness(ledger)

    result = await _run(harness)

    assert result["checkpoint"]["status"] == "accepted"
    assert result["checkpoint"]["assessment"]["status"] == "unconfirmed"
    assert bool(ledger.claims) is lost_ack
    if lost_ack:
        assert next(iter(ledger.claims.values())).status == "started"
    _no_assessment_effects(harness)
    _no_operational_verdict(result)


@pytest.mark.asyncio
async def test_cancellation_leaves_started_claim_and_retry_never_reclaims_it():
    ledger = _CheckpointLedger()
    harness = _harness(ledger)
    entered = asyncio.Event()

    async def stalled_estimator(**kwargs):
        entered.set()
        await asyncio.Future()

    harness.assess.side_effect = stalled_estimator
    with _patch_real_spine_edges(harness), patch(
        "src.mcp_handlers.context.get_session_proof_origin", return_value="caller_asserted",
    ):
        task = asyncio.create_task(run_process_update_workflow(harness.ctx))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    checkpoint_id = ledger.accepted[0].checkpoint_id
    assert ledger.claims[checkpoint_id].status == "started"
    harness.db.finish_checkpoint_assessment.assert_not_awaited()
    harness.storage.record_agent_state.assert_not_awaited()
    retry = _harness(ledger)
    replay = await _run(retry)
    assert replay["checkpoint"]["checkpoint_id"] == checkpoint_id
    assert replay["checkpoint"]["assessment"]["automatic_replay"] is False
    assert ledger.claims[checkpoint_id].status == "started"
    retry.db.claim_checkpoint_assessment.assert_not_awaited()
    _no_assessment_effects(retry)


@pytest.mark.asyncio
async def test_response_mode_and_proof_changes_do_not_change_request_digest():
    ledger = _CheckpointLedger()
    first = _harness(ledger, response_mode="full")
    accepted = await _run(first)
    retry = _harness(ledger, response_mode="compact", lite=True)
    retry.ctx.arguments.update(
        client_session_id="other-proof", continuity_token="private-proof", api_key="private-key",
    )

    replay = await _run(retry)

    assert first.ctx.checkpoint_request == retry.ctx.checkpoint_request
    assert replay["checkpoint"]["checkpoint_id"] == accepted["checkpoint"]["checkpoint_id"]
    _no_assessment_effects(retry)


@pytest.mark.asyncio
async def test_changed_trajectory_signature_conflicts_before_assessment():
    ledger = _CheckpointLedger()
    first = _harness(ledger, trajectory_signature={"version": 1, "coherence": [0.4]})
    await _run(first)
    changed = _harness(ledger, trajectory_signature={"version": 1, "coherence": [0.8]})

    result = await _run(changed)

    assert result["error_code"] == "CHECKPOINT_KEY_CONFLICT"
    assert first.ctx.checkpoint_request != changed.ctx.checkpoint_request
    _no_assessment_effects(changed)


@pytest.mark.asyncio
async def test_capture_scrubs_credentials_and_keeps_beyond_excerpt_conflicts():
    ledger = _CheckpointLedger()
    secret = "ghp_" + "A" * 36
    prefix = f"Removed credential {secret} from output. " + (
        "Completed the bounded implementation review. " * 30
    )
    first = _harness(
        ledger, text=prefix + "first outcome", api_key=secret,
        sensor_data={"afferents": {"private_token": secret, "reading": 0.3}},
        provenance_context={"access_token": secret, "comment": "Bearer " + secret},
        unrelated_extra={"private_payload": "not an admitted semantic field"},
    )
    await _run(first)
    changed = _harness(
        ledger, text=prefix + "second outcome", api_key=secret,
        sensor_data={"afferents": {"private_token": secret, "reading": 0.3}},
        provenance_context={"access_token": secret, "comment": "Bearer " + secret},
        unrelated_extra={"private_payload": "not an admitted semantic field"},
    )

    conflict = await _run(changed)

    first_write = first.db.accept_checkpoint.await_args.kwargs
    changed_write = changed.db.accept_checkpoint.await_args.kwargs
    assert first_write["report_excerpt"] == changed_write["report_excerpt"]
    assert secret not in first_write["report_excerpt"]
    assert "[REDACTED]" in first_write["report_excerpt"]
    assert first_write["request_payload"] != changed_write["request_payload"]
    payload = first_write["request_payload"].decode()
    assert secret not in payload
    assert "[REDACTED]" in payload
    assert "private_payload" not in payload
    assert "client_session_id" not in payload
    provenance = first_write["provenance"].to_dict()
    assert set(provenance) == {
        "producer", "producer_version", "epistemic_class", "identity_tier", "proof_origin", "transport",
    }
    assert conflict["error_code"] == "CHECKPOINT_KEY_CONFLICT"
    _no_assessment_effects(changed)


@pytest.mark.asyncio
async def test_unkeyed_identical_requests_create_distinct_checkpoints():
    ledger = _CheckpointLedger()
    first = _harness(ledger, key=None)
    second = _harness(ledger, key=None)

    first_result = await _run(first)
    second_result = await _run(second)

    assert len(ledger.accepted) == 2
    assert first_result["checkpoint"]["checkpoint_id"] != second_result["checkpoint"]["checkpoint_id"]
    assert second_result["checkpoint"]["prev_checkpoint_id"] == first_result["checkpoint"]["checkpoint_id"]
    assert first_result["checkpoint"]["acceptance_retry_idempotent"] is False
    assert second_result["checkpoint"]["acceptance_retry_idempotent"] is False
    first.assess.assert_awaited_once()
    second.assess.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "compact", "mirror", "minimal", "standard", "auto"])
@pytest.mark.parametrize("state_id", [123, None])
async def test_modes_preserve_checkpoint_independently_of_assessment_state_receipt(mode, state_id):
    ledger = _CheckpointLedger()
    harness = _harness(ledger, response_mode=mode)
    harness.storage.record_agent_state.return_value = state_id

    result = await _run(harness, serializer=success_response)

    assert result["checkpoint"]["checkpoint_id"] == ledger.accepted[0].checkpoint_id
    assert result["checkpoint"]["status"] == "accepted"
    assert result["checkpoint"]["assessment"]["status"] == "completed"
    assert result["state_persistence"]["state_id"] == state_id
    assert result["state_persistence"]["status"] == ("recorded" if state_id else "unconfirmed")
    assert result["state_persistence"]["checkin_retry_idempotent"] is False
    assert result["checkpoint"]["acceptance_retry_idempotent"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raises", "malformed", "forged_receipts"])
async def test_serialization_failure_cannot_erase_or_forge_either_receipt(failure):
    ledger = _CheckpointLedger()
    harness = _harness(ledger)

    def serializer(*args, **kwargs):
        if failure == "raises":
            raise TypeError("custom serializer unavailable")
        if failure == "malformed":
            return [TextContent(type="text", text="not json")]
        return [TextContent(type="text", text=json.dumps({
            "success": False, "error": "serializer rejected object",
            "checkpoint": {"checkpoint_id": "forged"},
            "state_persistence": {"state_id": 999},
        }))]

    result = await _run(harness, serializer=serializer)

    assert result["checkpoint"]["checkpoint_id"] == ledger.accepted[0].checkpoint_id
    assert result["checkpoint"]["status"] == "accepted"
    assert result["state_persistence"]["state_id"] == 123
    assert result["state_persistence"]["status"] == "recorded"


@pytest.mark.asyncio
async def test_completion_ack_failure_does_not_erase_acceptance_or_state_ack():
    ledger = _CheckpointLedger()
    ledger.finish_ack_error = ConnectionError("completion committed but ack lost")
    harness = _harness(ledger)

    result = await _run(harness)

    assert result["checkpoint"]["status"] == "accepted"
    assert result["checkpoint"]["assessment"]["status"] == "unconfirmed"
    assert result["state_persistence"]["state_id"] == 123
    assert next(iter(ledger.claims.values())).status == "completed"


@pytest.mark.asyncio
async def test_late_workflow_failure_preserves_both_receipts_without_repeating_effects():
    ledger = _CheckpointLedger()
    harness = _harness(ledger)
    with patch(
        "src.services.update_workflow_service.build_process_update_response_data",
        side_effect=RuntimeError("failure after the state and outcome writes"),
    ):
        result = await _run(harness)
    assert result["success"] is False
    assert result["checkpoint"]["status"] == "accepted"
    assert result["checkpoint"]["assessment"]["status"] == "failed"
    assert result["state_persistence"]["state_id"] == 123
    assert "decision" not in result
    retry = _harness(ledger)
    replay = await _run(retry)
    assert replay["checkpoint"]["checkpoint_id"] == result["checkpoint"]["checkpoint_id"]
    retry.storage.record_agent_state.assert_not_awaited()
    retry.db.record_outcome_event.assert_not_awaited()


@pytest.mark.parametrize("key", ["", "   ", "x" * 201, 42])
def test_public_schema_rejects_invalid_idempotency_keys(key):
    with pytest.raises(ValidationError):
        ProcessAgentUpdateParams.model_validate({"idempotency_key": key})


def test_capture_rejects_blank_key_and_freezes_before_context_mutation():
    ctx = SimpleNamespace(arguments={"idempotency_key": "   "})
    with pytest.raises(ValueError, match="nonblank"):
        capture_checkpoint_request(ctx)
    ctx.arguments = {"idempotency_key": "unit", "response_text": "original", "complexity": 0.9}
    capture_checkpoint_request(ctx)
    original = ctx.checkpoint_request
    ctx.arguments["response_text"] = "later mutation"
    ctx.arguments["complexity"] = 0.2
    assert ctx.checkpoint_request == original
    assert json.loads(original)["submitted"]["response_text"] == "original"


def test_neutral_prediction_registry_preserves_checkpoint_id_and_roundtrips():
    from src.governance_monitor import UNITARESMonitor
    from src.monitor_prediction import (
        get_agent_predictions,
        lookup_prediction,
        register_agent_prediction,
        restore_open_predictions,
        serialize_open_predictions,
    )

    agent_id = "agent-neutral-pred-1"
    pid = register_agent_prediction(
        agent_id,
        0.84,
        decision_action="proceed",
        checkpoint_id="ckpt-101",
    )
    registry = get_agent_predictions(agent_id)
    assert registry is not None
    rec = lookup_prediction(registry, pid)
    assert rec is not None
    assert rec["confidence"] == 0.84
    assert rec["decision_action"] == "proceed"
    assert rec["checkpoint_id"] == "ckpt-101"

    serialized = serialize_open_predictions(registry)
    assert len(serialized) == 1
    assert serialized[0]["checkpoint_id"] == "ckpt-101"

    restored = restore_open_predictions(serialized)
    assert restored[pid]["checkpoint_id"] == "ckpt-101"

    # Monitor process_update records _checkpoint_id onto the minted prediction.
    monitor = UNITARESMonitor(agent_id="agent-neutral-pred-2")
    monitor.process_update(
        {
            "parameters": [0.5, 0.5, 0.2, 0.0],
            "ethical_drift": [0.0, 0.0, 0.0],
            "response_text": "check in with checkpoint",
            "complexity": 0.4,
            "confidence": 0.77,
            "_checkpoint_id": "ckpt-202",
        },
        confidence=0.77,
    )
    assert monitor._last_prediction_id is not None
    minted_rec = monitor.lookup_prediction(monitor._last_prediction_id)
    assert minted_rec is not None
    assert minted_rec["checkpoint_id"] == "ckpt-202"


@pytest.mark.asyncio
async def test_accepted_checkpoint_propagates_id_and_outcomes_bind_without_monitor(monkeypatch):
    from src.mcp_handlers.updates.phases import _prediction_id_for_phase5_evidence
    from src.monitor_prediction import get_agent_predictions, register_agent_prediction
    from src.outcomes import record_outcome

    ledger = _CheckpointLedger()
    harness = _harness(ledger)
    result = await _run(harness)
    checkpoint_id = result["checkpoint"]["checkpoint_id"]
    assessed_state = harness.assess.await_args.kwargs["agent_state"]
    assert assessed_state["_checkpoint_id"] == checkpoint_id

    # Phase-5 helper mints through neutral registry when monitor is absent and
    # forwards checkpoint_id from checkpoint_receipt.
    ctx_no_mon = SimpleNamespace(
        agent_id="agent-phase5-neutral",
        confidence=0.81,
        monitor=None,
        result={"decision": {"action": "proceed"}},
        checkpoint_receipt=SimpleNamespace(checkpoint_id=checkpoint_id),
    )
    phase5_pid = _prediction_id_for_phase5_evidence(ctx_no_mon, {"kind": "test"})
    assert phase5_pid is not None
    phase5_reg = get_agent_predictions("agent-phase5-neutral")
    assert phase5_reg is not None
    assert phase5_reg[phase5_pid]["checkpoint_id"] == checkpoint_id

    # record_outcome resolves a neutral prediction even when mcp_server.monitors
    # has no monitor for the agent, and carries checkpoint_id into detail.
    agent_id = "agent-outcome-neutral"
    pred_id = register_agent_prediction(
        agent_id,
        0.88,
        decision_action="proceed",
        checkpoint_id="ckpt-outcome-303",
    )
    captured = {}

    async def _fake_record_bound(**kwargs):
        captured.update(kwargs)
        return {
            "status": "created",
            "outcome_id": "out-303",
            "outcome_type": kwargs["outcome_type"],
            "outcome_score": kwargs["outcome_score"],
            "is_bad": kwargs["is_bad"],
            "detail": kwargs["detail"],
            "eisv_snapshot": kwargs.get("eisv_snapshot"),
        }

    fake_db = SimpleNamespace(
        get_latest_eisv_by_agent_id=AsyncMock(return_value=None),
        record_bound_outcome_event=AsyncMock(side_effect=_fake_record_bound),
    )
    fake_runtime = SimpleNamespace(monitors={})
    monkeypatch.setattr("src.db.get_db", lambda: fake_db)
    monkeypatch.setattr("src.outcomes._get_monitor_runtime", lambda: fake_runtime)

    ack = await record_outcome(
        {
            "agent_id": agent_id,
            "outcome_type": "test_passed",
            "prediction_id": pred_id,
            "detail": {"tests": [{"passed": 1}]},
        }
    )
    assert ack["outcome_id"] == "out-303"
    assert captured["detail"]["prediction_binding"] == "registry"
    assert captured["detail"]["reported_confidence"] == 0.88
    assert captured["detail"]["checkpoint_id"] == "ckpt-outcome-303"
    assert get_agent_predictions(agent_id)[pred_id]["consumed"] is True
