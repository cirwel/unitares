"""
Configuration tool handlers.
"""

from typing import Dict, Any, Sequence
from mcp.types import TextContent
from ..utils import success_response, error_response
from ..decorators import mcp_tool
from ..error_helpers import agent_not_found_error
from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
logger = get_logger(__name__)

# Import from mcp_server_std module (using shared utility)

@mcp_tool("get_thresholds", timeout=10.0)
async def handle_get_thresholds(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Get current governance threshold configuration"""
    from src.runtime_config import get_thresholds
    
    thresholds = get_thresholds()
    
    return success_response(
        {
            "thresholds": thresholds,
            "note": "These are the effective thresholds (runtime overrides + defaults)"
        },
        arguments=arguments,
    )

@mcp_tool("set_thresholds", timeout=15.0)
async def handle_set_thresholds(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Set runtime threshold overrides - requires elevated permissions"""
    from src.runtime_config import set_thresholds, get_thresholds
    from src.audit_log import audit_logger
    
    # SECURITY: Require session ownership for threshold modification (UUID-based auth, Dec 2025)
    agent_id = arguments.get("agent_id")
    if not agent_id:
        return [error_response(
            "agent_id required to modify thresholds.",
            error_code="MISSING_PARAM",
            recovery={
                "action": "Provide agent_id parameter",
                "related_tools": ["get_thresholds", "identity"]
            }
        )]

    if agent_id not in mcp_server.agent_metadata:
        return agent_not_found_error(agent_id)

    from ..utils import verify_agent_ownership
    if not verify_agent_ownership(agent_id, arguments):
        return [error_response(
            "Authentication required to modify thresholds.",
            error_code="AUTH_REQUIRED",
            error_category="auth_error",
            recovery={
                "action": "Ensure your session is bound to this agent",
                "related_tools": ["identity"],
                "workflow": "Identity auto-binds on first tool call. Use identity() to check binding."
            }
        )]

    meta = mcp_server.agent_metadata[agent_id]
    
    # SECURITY: Admin-only threshold modification
    # Only allow threshold changes from admin agents or high-reputation agents
    is_admin = "admin" in meta.tags
    is_high_reputation = meta.total_updates >= 100  # Established agents
    
    if not (is_admin or is_high_reputation):
        return [error_response(
            "Threshold modification is admin-only. Only agents with 'admin' tag or 100+ updates can modify thresholds.",
            recovery={
                "action": "Threshold modification requires admin privileges. Contact system administrator or build reputation (100+ updates).",
                "related_tools": ["get_thresholds", "get_agent_metadata"],
                "note": "This restriction prevents agents from modifying critical governance parameters"
            }
        )]
    
    # Additional health checks for non-admin high-reputation agents
    if not is_admin and is_high_reputation:
        monitor = mcp_server.monitors.get(agent_id)
        if monitor:
            metrics = monitor.get_metrics()
            risk_score = metrics.get("risk_score")
            status = metrics.get("status", "unknown")
            
            # Block threshold changes from critical/moderate agents
            if status == "critical":
                return [error_response(
                    "Threshold modification blocked: Agent status is critical. Fix agent health before modifying thresholds.",
                    recovery={
                        "action": "Improve agent health metrics before attempting threshold changes",
                        "related_tools": ["get_governance_metrics", "process_agent_update"]
                    }
                )]
            
            # Block threshold changes from high-risk agents
            if risk_score and risk_score > 0.60:
                return [error_response(
                    f"Threshold modification blocked: Agent risk score ({risk_score:.2f}) is too high. Reduce risk before modifying thresholds.",
                    recovery={
                        "action": "Reduce agent risk score before attempting threshold changes",
                        "related_tools": ["get_governance_metrics"]
                    }
                )]
    
    thresholds = arguments.get("thresholds", {})
    validate = arguments.get("validate", True)
    
    # AUDIT: Log threshold modification attempt
    audit_logger.log("threshold_modification_attempt", {
        "agent_id": agent_id,
        "thresholds": thresholds,
        "validate": validate
    })
    
    result = set_thresholds(thresholds, validate=validate)
    
    # AUDIT: Log successful modification
    if result["success"]:
        audit_logger.log("threshold_modification_success", {
            "agent_id": agent_id,
            "updated": result["updated"]
        })
    
    current_thresholds = get_thresholds() if result["success"] else None
    
    response_data = {
        "success": result["success"],
        "updated": result["updated"],
        "errors": result["errors"],
        "warning": "Threshold modifications are logged and may affect system behavior"
    }
    
    if current_thresholds:
        response_data["current_thresholds"] = current_thresholds
    
    return success_response(response_data)
