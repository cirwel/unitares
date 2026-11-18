"""A real EISV pause, carried through lifecycle to both write gates.

The constant-substitution probes of 2026-11-15 found that no test both
reached the real estimator and followed its verdict to a refused write.
The posture and actuator tests in test_policy_actuation_posture.py feed a
hand-built pause result to a MagicMock monitor, and the write-gate tests in
test_circuit_breaker_enforcement.py and test_write_gate_and_all_clear.py
set ``meta.status = "paused"`` directly. Each link was covered on its own;
the chain was not.

Here a real ``UNITARESMonitor`` computes the verdict from its input, the
authenticated update boundary applies the deployment posture, and the same
metadata cache is then read by the check-in gate
(``resolve_identity_and_guards``) and the shared-memory gate
(``check_agent_can_operate``). PostgreSQL, authentication, and monitor-file
persistence are stubbed; the estimator, the posture resolver, the actuator
and both gates run unmodified.

The contract under test (src/agent_loop_detection.py):
- ``enforce``: an estimator pause becomes a lifecycle hold, and both gates
  refuse the next write with AGENT_PAUSED.
- ``observe`` (the default): the pause is kept in ``policy_evaluation`` but
  changes no lifecycle state, and both gates let the next write through.
- A benign input never holds writes, in either posture, so the gates react
  to the estimator's values and not merely to its being called.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.db as dbmod
import src.grounding.onboard_classifier as classifier
import src.mcp_handlers.context as ctxmod
import src.mcp_handlers.identity.handlers as id_handlers
import src.mcp_handlers.identity_bootstrap as ib
from src import agent_loop_detection as ald
from src import agent_storage
from src.agent_metadata_model import AgentMetadata
from src.governance_monitor import UNITARESMonitor
from src.mcp_handlers.support.agent_auth import check_agent_can_operate
from src.mcp_handlers.support.pause_ttl import describe_write_gate
from src.mcp_handlers.updates.context import UpdateContext
from src.mcp_handlers.updates.phases import resolve_identity_and_guards
from tests.helpers import parse_result

AGENT_UUID = "e2e0e2e0-1111-4222-8333-444455556666"

# A fresh monitor pauses on this input (CIRS risk ceiling). The values are
# the ones the probe used to obtain a genuine pause result.
PAUSE_INPUT = {
    "complexity": 1.0,
    "ethical_drift": [0.9, 0.9, 0.9],
    "task_type": "mixed",
    "response_text": "end-to-end pause probe",
}
PAUSE_CONFIDENCE = 0.05

BENIGN_INPUT = {
    "complexity": 0.3,
    "ethical_drift": [0.0, 0.0, 0.0],
    "task_type": "mixed",
    "response_text": "routine check-in",
}
BENIGN_CONFIDENCE = 0.8


def _fresh_meta() -> AgentMetadata:
    now = datetime.now(timezone.utc).isoformat()
    return AgentMetadata(
        agent_id=AGENT_UUID,
        status="active",
        created_at=now,
        last_update=now,
        label="e2e-pause-agent",
    )


def _patch_checkin_identity(monkeypatch) -> AsyncMock:
    """Resolve the next check-in to AGENT_UUID with caller-asserted proof."""
    monkeypatch.setattr(ctxmod, "get_context_agent_id", lambda: AGENT_UUID)
    monkeypatch.setattr(ctxmod, "get_context_session_key", lambda: "k")
    monkeypatch.setattr(
        ctxmod, "get_session_resolution_source", lambda: "explicit_client_session_id"
    )
    monkeypatch.setattr(ctxmod, "get_session_proof_origin", lambda: "caller_asserted")
    monkeypatch.setattr(ctxmod, "get_trajectory_confidence", lambda: None)
    monkeypatch.setattr(ib, "is_strict_identity_required", lambda: False)
    monkeypatch.setattr(
        classifier, "reconcile_resident_tags", AsyncMock(return_value=None)
    )

    class _DB:
        async def update_agent_fields(self, *a, **k):
            return None

        async def get_agent_label(self, agent_id):
            return None

    monkeypatch.setattr(dbmod, "get_db", lambda: _DB())
    persisted = AsyncMock(return_value=False)
    monkeypatch.setattr(id_handlers, "ensure_agent_persisted", persisted)
    return persisted


async def _check_in_through_real_estimator(
    monkeypatch, posture: str, agent_state: dict, confidence: float
):
    """Run one authenticated update with a real monitor; return the pieces."""
    monkeypatch.setenv("UNITARES_POLICY_ACTUATION", posture)
    monkeypatch.setenv("UNITARES_AUTO_DIALECTIC_RECOVERY", "0")

    meta = _fresh_meta()
    monitor = UNITARESMonitor(AGENT_UUID, load_state=False)
    cache = {AGENT_UUID: meta}
    db = MagicMock()
    db.increment_update_count = AsyncMock(return_value=1)
    durable_pause = AsyncMock(return_value={"applied": True})

    with patch.object(ald, "agent_metadata", cache), \
         patch.object(ald, "monitors", {AGENT_UUID: monitor}), \
         patch.object(ald, "verify_agent_ownership", return_value=(True, None)), \
         patch.object(ald, "detect_loop_pattern", return_value=(False, "")), \
         patch.object(ald, "save_monitor_state_async", AsyncMock()), \
         patch("src.agent_lifecycle.get_or_create_monitor", return_value=monitor), \
         patch(
             "src.agent_monitor_state.hydrate_from_db_if_fresh",
             AsyncMock(return_value=False),
         ), \
         patch.object(agent_storage, "get_db", return_value=db), \
         patch.object(agent_storage, "pause_agent_with_token", durable_pause), \
         patch(
             "src.broadcaster.broadcaster_instance.broadcast_event",
             AsyncMock(),
         ):
        result = await ald.process_update_authenticated_async(
            AGENT_UUID,
            "test-key",
            dict(agent_state),
            confidence=confidence,
        )

    # The estimator really ran: its state advanced past the fresh monitor.
    assert monitor.state.update_count >= 1
    return SimpleNamespace(
        result=result, meta=meta, cache=cache, durable_pause=durable_pause
    )


async def _gates_refuse(monkeypatch, cache: dict) -> tuple[bool, bool]:
    """Ask both write gates about the next write, reading the same cache."""
    _patch_checkin_identity(monkeypatch)
    server = SimpleNamespace(agent_metadata=cache)

    with patch("src.mcp_handlers.shared.get_mcp_server", return_value=server):
        kg_refusal = check_agent_can_operate(AGENT_UUID)

    ctx = UpdateContext(arguments={"agent_id": AGENT_UUID})
    ctx.mcp_server = server
    checkin_refusal = await resolve_identity_and_guards(ctx)

    for refusal in (kg_refusal, checkin_refusal):
        if refusal is not None:
            data = parse_result(refusal)
            assert data["success"] is False
            assert data["error_code"] == "AGENT_PAUSED"
    return kg_refusal is not None, checkin_refusal is not None


@pytest.mark.asyncio
async def test_enforced_estimator_pause_holds_both_write_gates(monkeypatch):
    run = await _check_in_through_real_estimator(
        monkeypatch, "enforce", PAUSE_INPUT, PAUSE_CONFIDENCE
    )
    result, meta = run.result, run.meta

    # The verdict came from the estimator, not from a fixture.
    assert result["policy_evaluation"]["action"] == "pause"
    assert result["decision"]["action"] == "pause"

    # Lifecycle: the actuator committed a durable pause and recorded it.
    run.durable_pause.assert_awaited_once()
    assert run.durable_pause.await_args.args[0] == AGENT_UUID
    assert meta.status == "paused"
    assert meta.paused_at
    enforcement = result["enforcement"]
    assert enforcement["posture"] == "enforce"
    assert enforcement["applied"] is True
    assert enforcement["durable"] is True
    paused_events = [e for e in meta.lifecycle_events if e["event"] == "paused"]
    assert len(paused_events) == 1
    assert paused_events[0]["actuation_id"] == enforcement["actuation_id"]

    # Write gates: the next check-in and the next shared-memory entry are refused.
    assert describe_write_gate(meta)["writes_held"] is True
    assert await _gates_refuse(monkeypatch, run.cache) == (True, True)


@pytest.mark.asyncio
async def test_observed_estimator_pause_changes_no_lifecycle_or_gate(monkeypatch):
    run = await _check_in_through_real_estimator(
        monkeypatch, "observe", PAUSE_INPUT, PAUSE_CONFIDENCE
    )
    result, meta = run.result, run.meta

    # The same input yields the same raw recommendation ...
    assert result["policy_evaluation"]["action"] == "pause"
    # ... projected to non-coercive guidance, with nothing applied.
    assert result["decision"]["action"] == "proceed"
    assert result["decision"]["recommended_action"] == "pause"
    assert result["enforcement"]["posture"] == "observe"
    assert result["enforcement"]["applied"] is False

    run.durable_pause.assert_not_awaited()
    assert meta.status == "active"
    assert meta.paused_at is None
    assert not [e for e in meta.lifecycle_events if e["event"] == "paused"]

    assert describe_write_gate(meta)["writes_held"] is False
    assert await _gates_refuse(monkeypatch, run.cache) == (False, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("posture", ["enforce", "observe"])
async def test_benign_estimator_verdict_never_holds_writes(monkeypatch, posture):
    run = await _check_in_through_real_estimator(
        monkeypatch, posture, BENIGN_INPUT, BENIGN_CONFIDENCE
    )

    assert run.result["policy_evaluation"]["action"] == "proceed"
    run.durable_pause.assert_not_awaited()
    assert run.meta.status == "active"
    assert await _gates_refuse(monkeypatch, run.cache) == (False, False)
