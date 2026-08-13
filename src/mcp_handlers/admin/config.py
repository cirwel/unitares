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
    from src.runtime_config import describe_thresholds, get_thresholds

    thresholds = get_thresholds()
    sources = describe_thresholds()
    overridden = sorted(n for n, d in sources.items() if d["source"] == "runtime_override")

    return success_response(
        {
            "thresholds": thresholds,
            # `thresholds` merges operator overrides into shipped defaults, so on
            # its own it cannot answer "did I set this, or is it stock?" — nor
            # that most of these keys are structural and cannot be set at all.
            # `sources` carries that per value; `overridden` is the short answer.
            "sources": sources,
            "overridden": overridden,
            "note": (
                "`thresholds` are effective values (runtime overrides merged over "
                "class defaults). `sources` says which layer supplied each one and "
                "whether it is settable; a displaced default is kept as "
                "`class_default`. Layer of origin only — not trust-contract §1 "
                "provenance."
            ),
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
    
    # SECURITY: Admin-only threshold modification.
    #
    # The `total_updates >= 100` "high reputation" route was removed 2026-08-05:
    # total_updates is a raw check-in counter incremented unconditionally on
    # every update (governance_monitor.py, agent_loop_detection.py), so it
    # measured persistence, not standing. Any agent could reach it in ~100
    # check-ins -- roughly two minutes under the 60/min limiter -- and buy
    # write access to fleet-global governance parameters. The two health
    # checks that guarded that route (status != critical, risk <= 0.60) do not
    # constrain it in practice: the basin gate zeroes EISV risk components
    # inside the basin, so 99.85% of fleet check-ins score below 0.3 risk.
    #
    # No threshold change has ever been recorded (audit.events, 0 rows), so
    # this removes an unused escalation path, not a working workflow.
    is_admin = "admin" in meta.tags

    if not is_admin:
        return [error_response(
            "Threshold modification is admin-only. Only agents with the 'admin' tag can modify thresholds.",
            recovery={
                "action": "Threshold modification requires an operator-granted 'admin' tag. Contact the system administrator.",
                "related_tools": ["get_thresholds", "get_agent_metadata"],
                "note": "This restriction prevents agents from modifying critical governance parameters"
            }
        )]
    
    # The status/risk health checks that used to sit here only ever guarded the
    # removed high-reputation route (`if not is_admin and is_high_reputation`),
    # so they were unreachable for admins and are now unreachable for everyone.
    # Gating an operator-granted capability on the agent's own self-reported
    # EISV would reintroduce the same farmable-input problem this change closes.

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
