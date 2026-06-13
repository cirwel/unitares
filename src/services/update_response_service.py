"""Helpers for final process_agent_update response assembly."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Sequence

from mcp.types import TextContent

from src.logging_utils import get_logger

logger = get_logger(__name__)


def build_process_update_response_data(
    *,
    result: Dict[str, Any],
    agent_id: str,
    identity_assurance: Any,
    monitor: Any = None,
    ctx_warnings: Sequence[str] = (),
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
    return response_data


def serialize_process_update_response(
    *,
    response_data: Dict[str, Any],
    agent_uuid: str,
    arguments: Dict[str, Any],
    fallback_result: Dict[str, Any],
    serializer=None,
) -> Sequence[TextContent]:
    """Serialize the final process_agent_update payload with a safe fallback."""
    try:
        if serializer is not None:
            return serializer(response_data, agent_id=agent_uuid, arguments=arguments)
        payload = {"success": True, "server_time": datetime.now(timezone.utc).isoformat(), **response_data}
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, default=str))]
    except Exception as serialization_error:
        logger.error(f"Failed to serialize response: {serialization_error}", exc_info=True)
        metrics = fallback_result.get("metrics", {})
        fallback_payload = {
            "success": True,
            "status": fallback_result.get("status", "unknown"),
            "decision": fallback_result.get("decision", {}),
            "metrics": {
                "E": float(metrics.get("E", 0)),
                "I": float(metrics.get("I", 0)),
                "S": float(metrics.get("S", 0)),
                "V": float(metrics.get("V", 0)),
                "coherence": float(metrics.get("coherence", 0)),
                "risk_score": float(metrics.get("risk_score", 0))
            },
            "_warning": "Response serialization had issues - some fields may be missing"
        }
        try:
            fallback_text = json.JSONEncoder(ensure_ascii=False).encode(fallback_payload)
        except Exception:
            fallback_text = (
                '{"success":true,"status":"unknown","decision":{},'
                '"metrics":{"E":0.0,"I":0.0,"S":0.0,"V":0.0,"coherence":0.0,"risk_score":0.0},'
                '"_warning":"Response serialization had issues - some fields may be missing"}'
            )
        return [TextContent(
            type="text",
            text=fallback_text
        )]
