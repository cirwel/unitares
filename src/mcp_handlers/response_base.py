"""Response formatting utilities for MCP handlers."""
from typing import Dict, Any, Sequence
from mcp.types import TextContent
import json
from datetime import datetime, timezone

from src.logging_utils import get_logger

logger = get_logger(__name__)


def format_metrics_report(
    metrics: Dict[str, Any],
    agent_id: str,
    include_timestamp: bool = True,
    include_context: bool = True,
    format_style: str = "structured"
) -> Dict[str, Any]:
    """
    Standardize metric reporting with agent_id and context.
    """
    standardized = {
        "agent_id": agent_id,
        **metrics
    }

    standardized["agent_id"] = agent_id

    if include_timestamp:
        standardized["timestamp"] = datetime.now(timezone.utc).isoformat()

    if include_context:
        if "health_status" not in standardized and "health_status" in metrics:
            standardized["health_status"] = metrics["health_status"]

        eisv_metrics = {}
        for key in ["E", "I", "S", "V"]:
            if key in metrics:
                eisv_metrics[key] = metrics[key]
        if eisv_metrics:
            standardized["eisv"] = eisv_metrics

    if format_style == "text":
        return format_metrics_text(standardized)

    return standardized


def format_metrics_text(metrics: Dict[str, Any]) -> str:
    """
    Format metrics as human-readable text with agent_id and context.
    """
    lines = []

    agent_id = metrics.get("agent_id", "unknown")
    lines.append(f"Agent: {agent_id}")

    if "timestamp" in metrics:
        lines.append(f"Timestamp: {metrics['timestamp']}")

    if "health_status" in metrics:
        status = metrics["health_status"]
        lines.append(f"Health: {status}")

    if "eisv" in metrics:
        eisv = metrics["eisv"]
        lines.append(f"EISV: E={eisv.get('E', 0):.3f} I={eisv.get('I', 0):.3f} S={eisv.get('S', 0):.3f} V={eisv.get('V', 0):.3f}")
    elif any(k in metrics for k in ["E", "I", "S", "V"]):
        e = metrics.get("E", 0)
        i = metrics.get("I", 0)
        s = metrics.get("S", 0)
        v = metrics.get("V", 0)
        lines.append(f"EISV: E={e:.3f} I={i:.3f} S={s:.3f} V={v:.3f}")

    key_metrics = ["coherence", "risk_score", "phi", "verdict", "lambda1"]
    for key in key_metrics:
        if key in metrics:
            value = metrics[key]
            if isinstance(value, float):
                lines.append(f"{key}: {value:.3f}")
            else:
                lines.append(f"{key}: {value}")

    return "\n".join(lines)


def success_response(data: Dict[str, Any], agent_id: str = None, arguments: Dict[str, Any] = None) -> Sequence[TextContent]:
    """
    Create a success response with optional agent signature.

    Returns Sequence[TextContent] containing SuccessResponseDict.
    """
    from . import serialization as _ser
    from .support import agent_auth as _auth

    response = {
        "success": True,
        # tz-aware UTC so server_time carries an explicit offset, matching
        # tz-aware fields like paused_at (dogfood 2026-06-13: a bare-local
        # server_time next to a +00:00 paused_at made duration math 6h wrong).
        "server_time": datetime.now(timezone.utc).isoformat(),
        **data
    }

    lite_response = (arguments or {}).get("lite_response", False)

    if lite_response:
        pass
    else:
        response["agent_signature"] = _auth.compute_agent_signature(agent_id=agent_id, arguments=arguments)

    param_coercions = (arguments or {}).get("_param_coercions")
    if param_coercions and not lite_response:
        response["_param_coercions"] = {
            "applied": param_coercions,
            "note": "Your parameters were auto-corrected. Use native types (e.g., 0.5 not '0.5') to avoid coercion."
        }

    try:
        serializable_response = _ser._make_json_serializable(response)
        json_text = json.dumps(serializable_response, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.error(f"JSON serialization error: {e}", exc_info=True)
        try:
            serializable_response = _ser._make_json_serializable(response)
            json_text = json.dumps(serializable_response, ensure_ascii=False, default=str)
        except Exception as e2:
            logger.error(f"Failed to serialize response even after conversion: {e2}", exc_info=True)
            try:
                minimal_response = {
                    "success": False,
                    "error": "Response serialization failed",
                    "recovery": {"action": "Check server logs for details"}
                }
                json_text = json.dumps(minimal_response, ensure_ascii=False)
            except Exception as e3:
                logger.critical(f"Even minimal response failed: {e3}", exc_info=True)
                json_text = '{"success":false,"error":"Serialization failed"}'

    return [TextContent(
        type="text",
        text=json_text
    )]
