"""Helpers for final process_agent_update response assembly."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Sequence

from mcp.types import TextContent

from src.governance_glossary import get_eisv_glossary
from src.logging_utils import get_logger
from src.state_persistence import (
    StatePersistenceReceipt,
    STATE_PERSISTENCE_UNCONFIRMED_WARNING,
)

logger = get_logger(__name__)


def build_process_update_response_data(
    *,
    result: Dict[str, Any],
    agent_id: str,
    identity_assurance: Any,
    monitor: Any = None,
    ctx_warnings: Sequence[str] = (),
    state_persistence: StatePersistenceReceipt | None = None,
) -> Dict[str, Any]:
    """Build the base response payload before enrichments and mode filtering.

    If a monitor is provided and has a recently-minted tactical prediction id
    (self._last_prediction_id), surface it as top-level `prediction_id` so the
    agent can echo it back on outcome_event for exact filtration in the
    sequential calibration lane.

    ctx_warnings: per-call non-fatal warnings (e.g. from Phase-5 evidence
    iteration). De-duplicated preserving order; only written to response_data
    if non-empty. Spec §2.
    """
    response_data = result.copy()
    response_data["agent_id"] = agent_id
    response_data["identity_assurance"] = identity_assurance
    response_data["eisv_labels"] = get_eisv_glossary()
    if monitor is not None:
        last_prediction_id = getattr(monitor, "_last_prediction_id", None)
        if last_prediction_id:
            response_data["prediction_id"] = last_prediction_id
    # Merge ctx.warnings (de-duped, preserving first-occurrence order) per spec §2.
    warnings_seen: list = []
    for w in (ctx_warnings or []):
        if w not in warnings_seen:
            warnings_seen.append(w)
    if warnings_seen:
        response_data["warnings"] = warnings_seen
    _restore_state_persistence(response_data, state_persistence)
    return response_data


def _restore_state_persistence(
    payload: Dict[str, Any], receipt: StatePersistenceReceipt | None,
) -> None:
    """The typed write acknowledgement, not mutable response data, is authority."""
    payload.pop("state_persistence", None)
    if receipt is None:
        return
    payload["state_persistence"] = receipt.to_dict()
    if not receipt.confirmed:
        warnings = payload.get("warnings")
        warnings = list(warnings) if isinstance(warnings, list) else []
        if STATE_PERSISTENCE_UNCONFIRMED_WARNING not in warnings:
            warnings.append(STATE_PERSISTENCE_UNCONFIRMED_WARNING)
        payload["warnings"] = warnings


def serialize_process_update_response(
    *,
    response_data: Dict[str, Any],
    agent_uuid: str,
    arguments: Dict[str, Any],
    fallback_result: Dict[str, Any],
    serializer=None,
    state_persistence: StatePersistenceReceipt | None = None,
) -> Sequence[TextContent]:
    """Serialize the final process_agent_update payload with a safe fallback."""
    # The shared serializer may return a serialization-error payload without
    # raising, or trim dictionaries after 100 keys. Reserve the immutable
    # receipt across that conversion, then restore it into the primitive JSON.
    # Track actual returned error status even if restoring the receipt fails.
    serialization_failed = False
    try:
        response_data = dict(response_data)
        _restore_state_persistence(response_data, state_persistence)
        if serializer is not None:
            result = serializer(response_data, agent_id=agent_uuid, arguments=arguments)
            if state_persistence is None:
                return result
            result = list(result)
            content = result[0]
            payload = json.loads(content.text)
            if not isinstance(payload, dict):
                raise ValueError("Check-in serializer did not return an object")
            serialization_failed = payload.get("success") is False or "error" in payload
            _restore_state_persistence(payload, state_persistence)
            result[0] = content.model_copy(update={
                "text": json.JSONEncoder(ensure_ascii=False).encode(payload),
            })
            return result
        payload = {"success": True, "server_time": datetime.now(timezone.utc).isoformat(), **response_data}
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, default=str))]
    except Exception as serialization_error:
        logger.error(f"Failed to serialize response: {serialization_error}", exc_info=True)
        try:
            # Metric conversion itself can fail, so it belongs inside the
            # fallback guard along with the encoder.
            metrics = fallback_result.get("metrics", {})
            fallback_payload = {
                "success": not serialization_failed,
                "status": fallback_result.get("status", "unknown"),
                "decision": fallback_result.get("decision", {}),
                "metrics": {
                    "E": float(metrics.get("E", 0)),
                    "I": float(metrics.get("I", 0)),
                    "S": float(metrics.get("S", 0)),
                    "V": float(metrics.get("V", 0)),
                    "coherence": float(metrics.get("coherence", 0)),
                    "coherence_source": metrics.get("coherence_source", "unknown"),
                    "coherence_role": metrics.get("coherence_role", "unknown"),
                    "risk_score": float(metrics.get("risk_score", 0)),
                },
                "_warning": "Response serialization had issues - some fields may be missing",
            }
            if serialization_failed:
                fallback_payload["error"] = "Internal serialization error"
            _restore_state_persistence(fallback_payload, state_persistence)
            fallback_text = json.JSONEncoder(ensure_ascii=False).encode(fallback_payload)
        except Exception:
            fallback_text = (
                '{"success":' + ('false' if serialization_failed else 'true')
                + ',"status":"unknown","decision":{},'
                '"metrics":{"E":0.0,"I":0.0,"S":0.0,"V":0.0,"coherence":0.0,"risk_score":0.0},'
                '"_warning":"Response serialization had issues - some fields may be missing"'
            )
            if serialization_failed:
                fallback_text += ',"error":"Internal serialization error"'
            if state_persistence is not None:
                fallback_text += ',"state_persistence":' + state_persistence.to_json()
                if not state_persistence.confirmed:
                    # This fixed message contains no characters requiring JSON
                    # escaping; no caller-controlled string reaches this path.
                    fallback_text += ',"warnings":["' + STATE_PERSISTENCE_UNCONFIRMED_WARNING + '"]'
            fallback_text += '}'
        return [TextContent(
            type="text",
            text=fallback_text
        )]
