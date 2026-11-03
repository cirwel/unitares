"""Tactical prediction registry for governance monitor.

Mints per-check-in prediction IDs so outcome_event can reference a specific
(confidence, timestamp) pair exactly instead of relying on temporal proxy.
Monitor-backed registries live on the monitor and travel with its state
snapshot (serialize/restore below), surviving a server restart; predictions
minted on the monitor-free fallback path live in `_NEUTRAL_PREDICTIONS` in
process memory until adopted by a monitor or expired by TTL.
"""

import math
import time as _time
import uuid
from datetime import datetime
from typing import Dict, Any, Optional


DEFAULT_PREDICTION_TTL_SECONDS: float = 3600.0

_AGENT_PREDICTIONS: Dict[str, Dict[str, Dict[str, Any]]] = {}
_NEUTRAL_PREDICTIONS: Dict[str, Dict[str, Dict[str, Any]]] = {}


def prune_agent_predictions(
    ttl_seconds: float = DEFAULT_PREDICTION_TTL_SECONDS,
) -> int:
    """Expire stale entries in `_NEUTRAL_PREDICTIONS` and drop empty registries.

    Never mutates another agent's monitor-owned `_open_predictions` dict, so
    a no-monitor mint cannot race a concurrent `monitor.process_update` running
    on the executor thread.
    """
    removed_agents = 0
    for aid, preds in list(_NEUTRAL_PREDICTIONS.items()):
        if not isinstance(preds, dict):
            _NEUTRAL_PREDICTIONS.pop(aid, None)
            removed_agents += 1
            continue
        expire_old_predictions(preds, ttl_seconds)
        consumed_ids = [
            pid for pid, rec in list(preds.items()) if rec.get("consumed")
        ]
        for pid in consumed_ids:
            preds.pop(pid, None)
        if not preds:
            _NEUTRAL_PREDICTIONS.pop(aid, None)
            if _AGENT_PREDICTIONS.get(aid) is preds:
                _AGENT_PREDICTIONS.pop(aid, None)
            removed_agents += 1
    for aid, preds in list(_AGENT_PREDICTIONS.items()):
        if not isinstance(preds, dict) or not preds:
            _AGENT_PREDICTIONS.pop(aid, None)
            removed_agents += 1
    return removed_agents


def bind_agent_predictions(
    agent_id: Optional[str],
    open_predictions: Dict[str, Dict[str, Any]],
    *,
    merge_existing: bool = True,
    clear_if_empty: bool = False,
    overwrite_existing: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """Bind `open_predictions` as the shared prediction registry for `agent_id`.

    Merges any unconsumed predictions minted without a monitor
    (`_NEUTRAL_PREDICTIONS`) into `open_predictions`, while ignoring empty
    monitor initializations so a throwaway `UNITARESMonitor(..., load_state=False)`
    cannot clobber an active registry entry and fresh test monitors do not
    inherit predictions from prior monitor instances.
    """
    if isinstance(agent_id, str) and agent_id and isinstance(open_predictions, dict):
        existing = _AGENT_PREDICTIONS.get(agent_id)
        if merge_existing:
            neutral = _NEUTRAL_PREDICTIONS.get(agent_id)
            if isinstance(neutral, dict):
                for pid, rec in list(neutral.items()):
                    if rec.get("consumed"):
                        neutral.pop(pid, None)
                        continue
                    open_predictions.setdefault(pid, rec)
                if not neutral:
                    _NEUTRAL_PREDICTIONS.pop(agent_id, None)
        if open_predictions:
            if (
                not overwrite_existing
                and isinstance(existing, dict)
                and existing is not open_predictions
            ):
                for pid, rec in list(existing.items()):
                    open_predictions.setdefault(pid, rec)
            _AGENT_PREDICTIONS[agent_id] = open_predictions
        elif clear_if_empty and existing is open_predictions:
            _AGENT_PREDICTIONS.pop(agent_id, None)
    return open_predictions


def get_agent_predictions(
    agent_id: Optional[str],
    monitor: Any = None,
) -> Optional[Dict[str, Dict[str, Any]]]:
    """Return the open-prediction registry for `agent_id`, syncing `monitor` when present."""
    if monitor is not None:
        mon_preds = getattr(monitor, "_open_predictions", None)
        if isinstance(mon_preds, dict):
            return bind_agent_predictions(agent_id, mon_preds)
        return None
    if isinstance(agent_id, str) and agent_id:
        return _AGENT_PREDICTIONS.get(agent_id)
    return None


def register_agent_prediction(
    agent_id: str,
    confidence: float,
    *,
    decision_action: Optional[str] = None,
    checkpoint_id: Optional[str] = None,
    prediction_ttl_seconds: float = DEFAULT_PREDICTION_TTL_SECONDS,
    monitor: Any = None,
    advertise: bool = True,
) -> str:
    """Mint and register a tactical prediction for `agent_id` via the neutral registry."""
    prune_agent_predictions(prediction_ttl_seconds)
    open_predictions = get_agent_predictions(agent_id, monitor)
    if open_predictions is None:
        open_predictions = {}
    pid = register_tactical_prediction(
        open_predictions,
        confidence,
        decision_action=decision_action,
        prediction_ttl_seconds=prediction_ttl_seconds,
        checkpoint_id=checkpoint_id,
    )
    if monitor is None and isinstance(agent_id, str) and agent_id:
        _NEUTRAL_PREDICTIONS.setdefault(agent_id, {})[pid] = open_predictions[pid]
    bind_agent_predictions(agent_id, open_predictions)
    if monitor is not None and advertise:
        monitor._last_prediction_id = pid
    return pid


def register_tactical_prediction(
    open_predictions: Dict[str, Dict],
    confidence: float,
    *,
    decision_action: Optional[str] = None,
    prediction_ttl_seconds: float = 600.0,
    checkpoint_id: Optional[str] = None,
) -> str:
    """Mint a prediction id and register it. Returns the prediction_id."""
    expire_old_predictions(open_predictions, prediction_ttl_seconds)

    prediction_id = str(uuid.uuid4())
    record: Dict[str, Any] = {
        "confidence": float(confidence),
        "decision_action": decision_action,
        "created_at": _time.monotonic(),
        # Wall-clock twin of created_at: the monotonic clock restarts with the
        # process, so only this can carry the forecast's age across a restart.
        "created_at_epoch": _time.time(),
        "created_at_iso": datetime.now().isoformat(),
        "consumed": False,
    }
    if checkpoint_id is not None:
        record["checkpoint_id"] = str(checkpoint_id)
    open_predictions[prediction_id] = record
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
        row = {
            "prediction_id": pid,
            "confidence": rec.get("confidence"),
            "decision_action": rec.get("decision_action"),
            "created_at_epoch": rec.get("created_at_epoch", now_wall - age),
            "created_at_iso": rec.get("created_at_iso"),
        }
        if rec.get("checkpoint_id") is not None:
            row["checkpoint_id"] = str(rec["checkpoint_id"])
        rows.append(row)
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
        rec: Dict[str, Any] = {
            "confidence": confidence,
            "decision_action": row.get("decision_action"),
            "created_at": now_mono - age,
            "created_at_epoch": created_epoch,
            "created_at_iso": row.get("created_at_iso"),
            "consumed": False,
        }
        if row.get("checkpoint_id"):
            rec["checkpoint_id"] = str(row["checkpoint_id"])
        restored[pid] = rec
    return restored
