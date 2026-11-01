"""Durable delivery of newly bound outcomes to calibration instruments.

The outbox belongs to the outcome transaction. A PostgreSQL receipt belongs to
the projection transaction; a file receipt belongs to the atomic state replace.
Retries therefore never infer delivery from the outcome acknowledgement.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket

from config.governance_config import GovernanceConfig
from src.logging_utils import get_logger
from src.outcome_corroboration import GRADE_WEIGHTS, TOOL_OBSERVED

logger = get_logger(__name__)
HARD_EXOGENOUS_CHANNELS = {
    "test_passed": "tests", "test_failed": "tests",
    "task_completed": "tasks", "task_failed": "tasks",
}
CONFIDENCE_BINS = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0)]


def digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def delivery_target() -> dict:
    # Importing these modules does not instantiate either lazy singleton or
    # require a writable sink. Canonical outcome persistence must survive a
    # temporarily unavailable file system.
    from src.calibration import DEFAULT_STATE_FILE as calibration_file
    from src.sequential_calibration import DEFAULT_STATE_FILE as sequential_file

    backend = os.getenv("UNITARES_CALIBRATION_BACKEND", "postgres").strip().lower()
    if backend not in {"json", "postgres"}:
        backend = "postgres"
    return {
        "revision": 1, "epoch": GovernanceConfig.CURRENT_EPOCH,
        # Containers whose hostnames change on recreation should configure a
        # stable instrument identity, shared only by consumers of the same
        # durable files. The hostname default isolates independent host files.
        "host": os.getenv("UNITARES_CALIBRATION_INSTRUMENT_ID") or socket.gethostname(),
        "backend": backend,
        "calibration_file": str(Path(calibration_file).resolve()),
        "sequential_file": str(Path(sequential_file).resolve()),
        "bins": CONFIDENCE_BINS,
    }


async def enqueue_delivery(conn, *, claim, agent_id: str, prediction_id: str) -> None:
    """Called only by the owner of a newly inserted canonical binding."""
    detail = claim["canonical_detail"]
    detail = json.loads(detail) if isinstance(detail, str) else detail
    confidence = detail.get("reported_confidence")
    if (confidence is None or detail.get("calibration_excluded")
            or float(detail.get("evidence_weight") or 0) < GRADE_WEIGHTS[TOOL_OBSERVED]):
        return
    class_tag = None
    if detail.get("eprocess_eligible"):
        try:
            from src.agent_metadata_model import agent_metadata
            from src.grounding.class_indicator import classify_agent
            class_tag = classify_agent(agent_metadata.get(agent_id))
        except Exception:
            # Freeze the unknown bucket; retry must not reinterpret membership.
            pass
    target = delivery_target()
    payload = {
        "target": target, "agent_id": agent_id, "prediction_id": prediction_id,
        "confidence": confidence, "score": claim["canonical_outcome_score"],
        "prediction_source": detail.get("prediction_source"),
        "prediction_binding": detail.get("prediction_binding"),
        "evidence_weight": detail.get("evidence_weight"),
        "corroboration_grade": detail.get("corroboration_grade"),
        "is_bad": claim["canonical_is_bad"],
        "outcome_type": claim["canonical_outcome_type"],
        "observed_at": claim["canonical_outcome_ts"].isoformat(),
        "class_tag": class_tag, "signal_source": detail.get("hard_exogenous_signal"),
        "decision_action": detail.get("decision_action"),
    }
    await conn.execute(
        "INSERT INTO audit.outcome_calibration_deliveries "
        "(outcome_id, outcome_ts, agent_id, prediction_id, target_scope, payload, needs_sequential) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)",
        claim["canonical_outcome_id"], claim["canonical_outcome_ts"],
        agent_id, prediction_id, digest(target), json.dumps(payload, allow_nan=False),
        bool(detail.get("eprocess_eligible")),
    )


def apply_calibration_sample(state: dict, payload: dict) -> dict:
    """Use the existing model math without creating a persistence writer."""
    from src.calibration import CalibrationChecker

    checker = CalibrationChecker.__new__(CalibrationChecker)
    checker.bins = payload["target"]["bins"]
    checker._apply_state_data(state)
    checker.save_state = lambda: None
    confidence = payload["confidence"]
    checker.record_prediction(
        confidence, confidence >= 0.5, payload["score"],
        agent_id=payload["agent_id"], observed_at=payload["observed_at"],
    )
    channel = HARD_EXOGENOUS_CHANNELS.get(payload["outcome_type"])
    if channel:
        checker.record_tactical_decision(
            confidence, "proceed", not payload["is_bad"], signal_source=channel,
            agent_id=payload["agent_id"], observed_at=payload["observed_at"],
        )
    return checker._snapshot_state()


def _deliver_file_sinks(row, payload: dict) -> None:
    from src.calibration_file_store import deliver_file_sample
    from src.sequential_calibration import SequentialCalibrationTracker

    outcome_id, payload_digest = str(row["outcome_id"]), digest(payload)
    if payload["target"]["backend"] == "json":
        deliver_file_sample(
            Path(payload["target"]["calibration_file"]), outcome_id=outcome_id,
            payload_digest=payload_digest, observation_time=payload["observed_at"],
            apply_sample=lambda current: apply_calibration_sample(current, payload),
        )
    if row["needs_sequential"]:
        tracker = SequentialCalibrationTracker(Path(payload["target"]["sequential_file"]))
        tracker.record_exogenous_tactical_outcome(
            confidence=payload["confidence"], outcome_correct=not payload["is_bad"],
            agent_id=payload["agent_id"], class_tag=payload["class_tag"],
            signal_source=payload["signal_source"], decision_action=payload["decision_action"],
            outcome_type=payload["outcome_type"], prediction_id=payload["prediction_id"],
            timestamp=payload["observed_at"], delivery_id=outcome_id,
            delivery_digest=payload_digest,
        )


async def drain_deliveries(db, *, limit: int = 20, target: dict | None = None) -> int:
    """Bounded recovery pass. Independent workers arbitrate with row locks."""
    target = target or delivery_target()
    completed = 0
    for _ in range(limit):
        outcome_id = None
        try:
            async with db.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "SELECT * FROM audit.outcome_calibration_deliveries "
                        "WHERE target_scope = $1 AND completed_at IS NULL "
                        "AND next_attempt_at <= now() ORDER BY outcome_ts, outcome_id "
                        "FOR UPDATE SKIP LOCKED LIMIT 1", digest(target),
                    )
                    if row is None:
                        break
                    outcome_id = row["outcome_id"]
                    payload = row["payload"]
                    payload = json.loads(payload) if isinstance(payload, str) else payload
                    if digest(payload["target"]) != digest(target):
                        raise ValueError("calibration delivery destination mismatch")
                    if target["backend"] == "postgres":
                        state_row = await conn.fetchrow(
                            "SELECT data FROM core.calibration WHERE id = TRUE FOR UPDATE"
                        )
                        if state_row is None:
                            raise RuntimeError("calibration state is not initialized")
                        state = state_row["data"]
                        state = json.loads(state) if isinstance(state, str) else state
                        updated = {**state, **apply_calibration_sample(state, payload)}
                        await conn.execute(
                            "UPDATE core.calibration SET data = $1::jsonb, version = version + 1, "
                            "updated_at = now() WHERE id = TRUE", json.dumps(updated, allow_nan=False),
                        )
                    # A file may commit before PostgreSQL acknowledges it. Its
                    # own receipt makes replay after rollback/restart harmless.
                    await asyncio.to_thread(_deliver_file_sinks, row, payload)
                    await conn.execute(
                        "UPDATE audit.outcome_calibration_deliveries SET "
                        "calibration_delivered_at = now(), "
                        "sequential_delivered_at = CASE WHEN needs_sequential THEN now() END, "
                        "completed_at = now(), attempts = attempts + 1, last_error = NULL "
                        "WHERE outcome_id = $1", outcome_id,
                    )
            completed += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if outcome_id is None:
                raise
            async with db.acquire() as conn:
                await conn.execute(
                    "UPDATE audit.outcome_calibration_deliveries SET attempts = attempts + 1, "
                    "last_error = $2, next_attempt_at = now() + "
                    "make_interval(secs => LEAST(300, 5 * (attempts + 1))) "
                    "WHERE outcome_id = $1 AND completed_at IS NULL",
                    outcome_id, type(exc).__name__,
                )
            logger.warning("Calibration delivery deferred: %s", type(exc).__name__)
    return completed


async def calibration_delivery_task() -> None:
    """Supervised server task; restart always scans durable pending work."""
    from src.calibration import get_calibration_checker
    from src.db import get_db

    while True:
        try:
            await drain_deliveries(get_db())
            checker = get_calibration_checker()
            # Also retries an ordinary writer's frozen operation after transient
            # storage failure, even if no further observation arrives.
            await checker.flush_state_async()
            # A different consumer, or a commit whose acknowledgement was
            # lost, can advance durable state even when this pass reports zero.
            await checker.load_state_async()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Calibration recovery deferred: %s", type(exc).__name__)
        await asyncio.sleep(5)
