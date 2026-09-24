"""Tactical prediction registry for governance monitor.

Mints per-check-in prediction IDs so outcome_event can reference a specific
(confidence, timestamp) pair exactly instead of relying on temporal proxy.
The registry lives on the monitor and travels with its state snapshot
(serialize/restore below), so an open forecast survives a server restart;
orphaned entries are expired by TTL.
"""

import math
import time as _time
import uuid
from datetime import datetime
from typing import Dict, Any, Optional


def register_tactical_prediction(
    open_predictions: Dict[str, Dict],
    confidence: float,
    *,
    decision_action: Optional[str] = None,
    prediction_ttl_seconds: float = 600.0,
) -> str:
    """Mint a prediction id and register it. Returns the prediction_id."""
    expire_old_predictions(open_predictions, prediction_ttl_seconds)

    prediction_id = str(uuid.uuid4())
    open_predictions[prediction_id] = {
        "confidence": float(confidence),
        "decision_action": decision_action,
        "created_at": _time.monotonic(),
        # Wall-clock twin of created_at: the monotonic clock restarts with the
        # process, so only this can carry the forecast's age across a restart.
        "created_at_epoch": _time.time(),
        "created_at_iso": datetime.now().isoformat(),
        "consumed": False,
    }
    return prediction_id


def lookup_prediction(
    open_predictions: Dict[str, Dict],
    prediction_id: str,
) -> Optional[Dict[str, Any]]:
    """Return the registered record for prediction_id, or None if unknown."""
    if not prediction_id:
        return None
    record = open_predictions.get(prediction_id)
    if not record:
        return None
    return dict(record)


def consume_prediction(
    open_predictions: Dict[str, Dict],
    prediction_id: str,
    *,
    ttl_seconds: float = 3600.0,
) -> Optional[Dict[str, Any]]:
    """Mark a prediction as consumed and return its record.

    Returns None if the id is unknown, already consumed, or past TTL.
    Expired records are NOT marked consumed — they remain in the registry
    so callers using lookup_prediction can distinguish "missing" from
    "expired" when computing prediction_binding labels.
    """
    if not prediction_id:
        return None
    record = open_predictions.get(prediction_id)
    if not record or record.get("consumed"):
        return None
    age = _time.monotonic() - float(record.get("created_at", 0.0))
    if age > ttl_seconds:
        return None
    record["consumed"] = True
    return dict(record)


def expire_old_predictions(
    open_predictions: Dict[str, Dict],
    ttl_seconds: float = 600.0,
) -> int:
    """Drop prediction records older than ttl_seconds. Returns count removed."""
    now = _time.monotonic()
    stale_ids = [
        pid for pid, rec in open_predictions.items()
        if (now - float(rec.get("created_at", 0.0))) > ttl_seconds
    ]
    for pid in stale_ids:
        open_predictions.pop(pid, None)
    return len(stale_ids)


def serialize_open_predictions(
    open_predictions: Dict[str, Dict],
    ttl_seconds: float = 3600.0,
) -> list:
    """Open, unexpired forecasts as JSON-safe rows for the monitor snapshot.

    Without this the registry lived only in process memory, and every
    restart (each deploy is one) dropped forecasts still waiting for their
    outcome: record_result then reported missing_prediction and the outcome
    could not grade the check-in. Consumed rows are left out; the database
    claim at outcome time is the exactly-once authority either way.
    """
    now_mono = _time.monotonic()
    now_wall = _time.time()
    rows = []
    for pid, rec in open_predictions.items():
        if rec.get("consumed"):
            continue
        age = now_mono - float(rec.get("created_at", now_mono))
        if age > ttl_seconds:
            continue
        rows.append({
            "prediction_id": pid,
            "confidence": rec.get("confidence"),
            "decision_action": rec.get("decision_action"),
            "created_at_epoch": rec.get("created_at_epoch", now_wall - age),
            "created_at_iso": rec.get("created_at_iso"),
        })
    return rows


def restore_open_predictions(rows: Any, ttl_seconds: float = 3600.0) -> Dict[str, Dict]:
    """Rebuild the registry from snapshot rows, dropping expired or malformed ones.

    Each forecast's monotonic created_at is re-derived from its wall-clock age,
    so TTL checks behave exactly as if the process had never restarted.
    """
    restored: Dict[str, Dict] = {}
    if not isinstance(rows, list):
        return restored
    now_mono = _time.monotonic()
    now_wall = _time.time()
    for row in rows:
        try:
            pid = str(row["prediction_id"])
            confidence = float(row["confidence"])
            created_epoch = float(row["created_at_epoch"])
        except (KeyError, TypeError, ValueError):
            continue
        # float() accepts nan/inf; a restored forecast must be a real
        # confidence minted at a real past moment, or it could bind after the
        # restart and carry a non-finite value into calibration.
        if not (math.isfinite(confidence) and 0.0 <= confidence <= 1.0):
            continue
        if not math.isfinite(created_epoch) or created_epoch > now_wall + 60.0:
            continue
        age = max(0.0, now_wall - created_epoch)
        if age > ttl_seconds:
            continue
        restored[pid] = {
            "confidence": confidence,
            "decision_action": row.get("decision_action"),
            "created_at": now_mono - age,
            "created_at_epoch": created_epoch,
            "created_at_iso": row.get("created_at_iso"),
            "consumed": False,
        }
    return restored
