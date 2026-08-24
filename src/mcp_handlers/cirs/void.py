"""
CIRS void_alert handler — emit and query void state alerts.
"""

from typing import Dict, Any, Sequence
from datetime import datetime

from mcp.types import TextContent
from ..decorators import mcp_tool
from ..utils import success_response, error_response, require_registered_agent
from src.logging_utils import get_logger
from .types import VoidAlert, VoidSeverity
from .storage import _store_void_alert, _get_recent_void_alerts, _void_alert_buffer

logger = get_logger(__name__)
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
@mcp_tool("void_alert", timeout=10.0, register=False, description="CIRS Protocol: Broadcast or query void state alerts for multi-agent coordination")
async def handle_void_alert(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    CIRS VOID_ALERT - Multi-agent void state coordination.

    Two modes:
    1. EMIT mode (action='emit'): Broadcast a void alert to peers
    2. QUERY mode (action='query'): Get recent void alerts from the system
    """
    action = arguments.get("action", "").lower()

    if not action or action not in ("emit", "query"):
        return [error_response(
            "action parameter required: 'emit' or 'query'",
            recovery={
                "valid_actions": ["emit", "query"],
                "emit_example": "void_alert(action='emit', severity='warning')",
                "query_example": "void_alert(action='query', since_hours=1.0)"
            }
        )]

    if action == "emit":
        return await _handle_void_alert_emit(arguments)
    else:
        return await _handle_void_alert_query(arguments)

async def _handle_void_alert_emit(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Handle VOID_ALERT emit action"""
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]

    # Get current state for auto-detection
    monitor = mcp_server.get_or_create_monitor(agent_id)
    from src.agent_monitor_state import ensure_hydrated
    await ensure_hydrated(monitor, agent_id)
    current_V = float(monitor.state.V)
    current_coherence = float(monitor.state.coherence)

    # Get risk score from metrics
    metrics = monitor.get_metrics()
    current_risk = metrics.get("risk_score")
    if current_risk is None:
        current_risk = metrics.get("current_risk")

    # Determine severity
    severity_str = arguments.get("severity", "").lower()
    if severity_str:
        if severity_str not in ("warning", "critical"):
            return [error_response(
                f"Invalid severity: {severity_str}",
                recovery={"valid_values": ["warning", "critical"]}
            )]
        severity = VoidSeverity(severity_str)
    else:
        # Auto-detect severity from V value
        from config.governance_config import config
        import numpy as np
        V_history = np.array(monitor.state.V_history) if monitor.state.V_history else np.array([current_V])
        threshold = config.get_void_threshold(V_history, adaptive=True)

        if abs(current_V) > threshold * 1.5:
            severity = VoidSeverity.CRITICAL
        elif abs(current_V) > threshold:
            severity = VoidSeverity.WARNING
        else:
            return [error_response(
                f"V value ({current_V:.4f}) is below void threshold ({threshold:.4f}). "
                f"Set severity='warning' or severity='critical' to emit anyway.",
                recovery={
                    "current_V": current_V,
                    "threshold": threshold,
                    "void_active": monitor.state.void_active,
                    "note": "Void alerts should reflect actual void state"
                }
            )]

    # Create and store the alert
    alert = VoidAlert(
        agent_id=agent_id,
        timestamp=datetime.now().isoformat(),
        severity=severity,
        V_snapshot=current_V,
        context_ref=arguments.get("context_ref"),
        coherence_at_event=current_coherence,
        risk_at_event=current_risk
    )

    _store_void_alert(alert)

    return success_response({
        "action": "emit",
        "alert": alert.to_dict(),
        "message": f"VOID_ALERT broadcast: {severity.value.upper()} from {agent_id}",
        "cirs_protocol": "VOID_ALERT",
        "active_alerts_count": len(_void_alert_buffer)
    }, agent_id=agent_id)

async def _handle_void_alert_query(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Handle VOID_ALERT query action"""
    filter_agent_id = arguments.get("filter_agent_id")
    filter_severity_str = arguments.get("filter_severity", "").lower()
    since_hours = float(arguments.get("since_hours", 1.0))
    limit = int(arguments.get("limit", 50))

    filter_severity = None
    if filter_severity_str:
        if filter_severity_str not in ("warning", "critical"):
            return [error_response(
                f"Invalid filter_severity: {filter_severity_str}",
                recovery={"valid_values": ["warning", "critical"]}
            )]
        filter_severity = VoidSeverity(filter_severity_str)

    alerts = _get_recent_void_alerts(
        agent_id=filter_agent_id,
        severity=filter_severity,
        since_hours=since_hours,
        limit=limit
    )

    summary = {
        "total_alerts": len(alerts),
        "by_severity": {
            "warning": sum(1 for a in alerts if a["severity"] == "warning"),
            "critical": sum(1 for a in alerts if a["severity"] == "critical")
        },
        "unique_agents": len(set(a["agent_id"] for a in alerts)),
        "query_window_hours": since_hours
    }

    return success_response({
        "action": "query",
        "alerts": alerts,
        "summary": summary,
        "cirs_protocol": "VOID_ALERT",
        "filters_applied": {
            "agent_id": filter_agent_id,
            "severity": filter_severity_str or None,
            "since_hours": since_hours,
            "limit": limit
        }
    })
