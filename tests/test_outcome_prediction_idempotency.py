"""Exactly-once outcome_event semantics for prediction-bound submissions."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest

from src.monitor_prediction import register_tactical_prediction
from tests.helpers import parse_result


EISV_A = {
    "E": 0.71,
    "I": 0.78,
    "S": 0.16,
    "V": -0.02,
    "phi": 0.12,
    "verdict": "safe",
    "coherence": 0.51,
    "regime": "CONVERGENCE",
}
EISV_B = {
    "E": 0.22,
    "I": 0.31,
    "S": 0.74,
    "V": 0.41,
    "phi": 0.88,
    "verdict": "pause",
    "coherence": 0.09,
    "regime": "EXPLORATION",
}


class DurableBindingDB:
    """Small durable-store model; the lock models PostgreSQL key serialization."""

    def __init__(self, *, first_write: str = "ok") -> None:
        self.eisv = dict(EISV_A)
        self.first_write = first_write
        self.attempts = 0
        self.bindings: dict[tuple[str, str], dict] = {}
        self.rows: list[dict] = []
        self._lock = asyncio.Lock()

    async def get_latest_eisv_by_agent_id(self, agent_id):
        return deepcopy(self.eisv)

    async def get_latest_confidence_before(self, agent_id):
        return None

    async def record_outcome_event(self, **kwargs):
        raise AssertionError("prediction-bound submissions must use the atomic API")

    async def record_bound_outcome_event(
        self,
        *,
        agent_id,
        prediction_id,
        request_digest,
        eisv_snapshot,
        **kwargs,
    ):
        async with self._lock:
            self.attempts += 1
            key = (agent_id, prediction_id)
            existing = self.bindings.get(key)
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    return {
                        "status": "conflict",
                        "outcome_id": existing["outcome_id"],
                    }
                return {"status": "existing", **deepcopy(existing)}

            if self.first_write == "storage_error":
                self.first_write = "ok"
                return {"status": "error", "error": "storage unavailable"}
            if self.first_write == "fail_before_commit":
                self.first_write = "ok"
                return {"status": "error", "error": "transaction rolled back"}

            outcome_id = f"outcome-{len(self.rows) + 1}"
            canonical = {
                "outcome_id": outcome_id,
                "request_digest": request_digest,
                "outcome_type": kwargs["outcome_type"],
                "is_bad": kwargs["is_bad"],
                "outcome_score": kwargs["outcome_score"],
                "detail": deepcopy(kwargs["detail"]),
                "eisv_snapshot": deepcopy(eisv_snapshot),
            }
            self.bindings[key] = canonical
            self.rows.append(deepcopy(canonical))
            if self.first_write == "lost_ack":
                self.first_write = "ok"
                return {"status": "error", "error": "acknowledgement lost"}
            return {"status": "created", **deepcopy(canonical)}


def make_monitor(confidence: float = 0.83):
    monitor = MagicMock()
    monitor._open_predictions = {}
    monitor._prediction_ttl_seconds = 3600.0
    monitor._prev_confidence = None
    monitor._behavioral_state = None
    monitor.get_primary_eisv.return_value = (
        EISV_A["E"],
        EISV_A["I"],
        EISV_A["S"],
        EISV_A["V"],
    )
    prediction_id = register_tactical_prediction(
        monitor._open_predictions,
        confidence=confidence,
        decision_action="proceed",
    )
    return monitor, prediction_id


async def submit(db, monitors, prediction_id, **overrides):
    from src.mcp_handlers.observability.outcome_events import (
        _record_outcome_event_inline,
    )

    arguments = {
        "agent_id": "agent-idempotent",
        "outcome_type": "test_passed",
        "prediction_id": prediction_id,
        "verification_source": "external_signal",
        "detail": {"kind": "test", "tool": "pytest", "exit_code": 0},
    }
    arguments.update(overrides)
    server = MagicMock()
    server.monitors = monitors
    with (
        patch("src.db.get_db", return_value=db),
        patch(
            "src.mcp_handlers.observability.outcome_events.mcp_server",
            server,
        ),
        patch(
            "src.mcp_handlers.context.get_context_client_session_id",
            return_value=None,
        ),
    ):
        return await _record_outcome_event_inline(arguments)


async def public_submit(db, monitor, prediction_id, outcome_type):
    from src.mcp_handlers.observability.outcome_events import handle_outcome_event

    server = MagicMock()
    server.monitors = {"agent-idempotent": monitor}
    with (
        patch("src.db.get_db", return_value=db),
        patch(
            "src.mcp_handlers.observability.outcome_events.mcp_server",
            server,
        ),
        patch(
            "src.mcp_handlers.context.get_context_agent_id",
            return_value="agent-idempotent",
        ),
        patch(
            "src.mcp_handlers.context.get_context_client_session_id",
            return_value=None,
        ),
    ):
        return parse_result(
            await handle_outcome_event(
                {"outcome_type": outcome_type, "prediction_id": prediction_id}
            )
        )


@pytest.mark.asyncio
async def test_failure_before_commit_does_not_consume_and_retry_binds_once():
    db = DurableBindingDB(first_write="fail_before_commit")
    monitor, pid = make_monitor()

    first = await submit(db, {"agent-idempotent": monitor}, pid)
    assert "error" in first
    assert monitor._open_predictions[pid]["consumed"] is False
    assert db.rows == []

    retry = await submit(db, {"agent-idempotent": monitor}, pid)
    assert retry["outcome_id"] == "outcome-1"
    assert retry["prediction_binding"] == "registry"
    assert monitor._open_predictions[pid]["consumed"] is True
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_lost_ack_retry_returns_canonical_row_without_duplicate():
    db = DurableBindingDB(first_write="lost_ack")
    monitor, pid = make_monitor()

    first = await submit(db, {"agent-idempotent": monitor}, pid)
    assert "error" in first
    assert monitor._open_predictions[pid]["consumed"] is False
    assert len(db.rows) == 1

    retry = await submit(db, {"agent-idempotent": monitor}, pid)
    assert retry["outcome_id"] == "outcome-1"
    assert retry["prediction_binding"] == "registry"
    assert retry["prediction_source"] == "registry"
    assert len(db.rows) == 1
    assert monitor._open_predictions[pid]["consumed"] is True


@pytest.mark.asyncio
async def test_identical_retry_returns_canonical_and_calibrates_only_once():
    db = DurableBindingDB()
    monitor, pid = make_monitor()
    checker = MagicMock()
    sequential = MagicMock()

    with (
        patch("src.calibration.calibration_checker", checker),
        patch(
            "src.sequential_calibration.sequential_calibration_tracker",
            sequential,
        ),
    ):
        first = await submit(db, {"agent-idempotent": monitor}, pid)
        retry = await submit(db, {"agent-idempotent": monitor}, pid)

    assert retry["outcome_id"] == first["outcome_id"]
    assert retry["prediction_binding"] == first["prediction_binding"] == "registry"
    assert retry["prediction_source"] == first["prediction_source"] == "registry"
    assert retry["eisv_snapshot"] == first["eisv_snapshot"]
    assert len(db.rows) == 1
    checker.record_prediction.assert_called_once()
    checker.record_tactical_decision.assert_called_once()
    sequential.record_exogenous_tactical_outcome.assert_called_once()


@pytest.mark.asyncio
async def test_conflicting_retry_is_rejected_without_new_row_or_calibration():
    db = DurableBindingDB()
    monitor, pid = make_monitor()
    checker = MagicMock()

    with patch("src.calibration.calibration_checker", checker):
        first = await submit(db, {"agent-idempotent": monitor}, pid)
        conflict = await submit(
            db,
            {"agent-idempotent": monitor},
            pid,
            outcome_type="test_failed",
        )

    assert first["outcome_id"] == "outcome-1"
    assert conflict["error_code"] == "PREDICTION_REUSE_CONFLICT"
    assert conflict["canonical_outcome_id"] == "outcome-1"
    assert len(db.rows) == 1
    checker.record_prediction.assert_called_once()


@pytest.mark.asyncio
async def test_public_conflict_response_names_canonical_outcome():
    db = DurableBindingDB()
    monitor, pid = make_monitor()
    first = await public_submit(db, monitor, pid, "test_passed")
    conflict = await public_submit(db, monitor, pid, "test_failed")

    assert first["outcome_id"] == "outcome-1"
    assert conflict["success"] is False
    assert conflict["error_code"] == "PREDICTION_REUSE_CONFLICT"
    assert conflict["canonical_outcome_id"] == "outcome-1"


@pytest.mark.asyncio
async def test_concurrent_identical_submissions_have_one_row_and_one_calibration():
    db = DurableBindingDB()
    monitor, pid = make_monitor()
    checker = MagicMock()
    sequential = MagicMock()

    with (
        patch("src.calibration.calibration_checker", checker),
        patch(
            "src.sequential_calibration.sequential_calibration_tracker",
            sequential,
        ),
    ):
        results = await asyncio.gather(
            submit(db, {"agent-idempotent": monitor}, pid),
            submit(db, {"agent-idempotent": monitor}, pid),
        )

    assert {result["outcome_id"] for result in results} == {"outcome-1"}
    assert {result["prediction_binding"] for result in results} == {"registry"}
    assert len(db.rows) == 1
    checker.record_prediction.assert_called_once()
    sequential.record_exogenous_tactical_outcome.assert_called_once()


@pytest.mark.asyncio
async def test_restart_retry_preserves_original_registry_and_eisv_provenance():
    db = DurableBindingDB()
    monitor, pid = make_monitor()
    first = await submit(
        db,
        {"agent-idempotent": monitor},
        pid,
        client_session_id="before-restart",
    )

    db.eisv = dict(EISV_B)
    retry = await submit(db, {}, pid, client_session_id="after-restart")

    assert retry["outcome_id"] == first["outcome_id"]
    assert retry["prediction_binding"] == "registry"
    assert retry["prediction_source"] == "registry"
    assert retry["eisv_snapshot"] == first["eisv_snapshot"]
    assert retry["eisv_snapshot"]["primary_eisv"]["E"] == EISV_A["E"]
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_storage_failure_leaves_prediction_retryable():
    db = DurableBindingDB(first_write="storage_error")
    monitor, pid = make_monitor()

    failed = await submit(db, {"agent-idempotent": monitor}, pid)

    assert failed["error_code"] == "DB_ERROR"
    assert monitor._open_predictions[pid]["consumed"] is False
    assert db.rows == []
