"""
Direct resume handler (deprecated).

Extracted from lifecycle.py for maintainability.
"""

from typing import Dict, Any, Sequence

from mcp.types import TextContent

from ..decorators import mcp_tool
from ..utils import success_response, error_response, require_registered_agent
from ..error_helpers import agent_not_found_error, system_error as system_error_helper
from ..support.coerce import resolve_agent_uuid
from .helpers import _resume_with_persistence, _invalidate_agent_cache  # noqa: F401
from src import agent_storage
from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server

logger = get_logger(__name__)

@mcp_tool("direct_resume_if_safe", timeout=10.0, deprecated=True, superseded_by="quick_resume or self_recovery_review")
async def handle_direct_resume_if_safe(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """⚠️ DEPRECATED: Use quick_resume() or self_recovery_review() instead.

    This tool is deprecated in favor of clearer recovery paths:
    - quick_resume() - for clearly safe states (coherence > 0.60, risk < 0.40, no reflection needed)
    - self_recovery_review() - for moderate states with reflection (coherence > 0.35, risk < 0.65)

    Migration guidance:
    - If coherence > 0.60 and risk < 0.40 → use quick_resume()
    - Otherwise → use self_recovery_review(reflection="...")

    This tool will be removed in v2.0. Current thresholds: coherence > 0.40, risk < 0.60.

    SECURITY: Requires registered agent_id and API key authentication.
    """
    # SECURITY FIX: Require registered agent_id (prevents phantom agent_ids)
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]

    # Use authoritative UUID for internal lookups
    agent_uuid = resolve_agent_uuid(arguments, agent_id)

    # Reload metadata from PostgreSQL (async)
    await mcp_server.load_metadata_async(force=True)

    meta = mcp_server.agent_metadata.get(agent_uuid)
    if not meta:
        return agent_not_found_error(agent_id)

    # SECURITY: Verify ownership via session binding (UUID-based auth, Dec 2025)
    from ..utils import verify_agent_ownership
    if not verify_agent_ownership(agent_uuid, arguments):
        return [error_response(
            "Authentication required. You can only resume your own agent.",
            error_code="AUTH_REQUIRED",
            error_category="auth_error",
            recovery={
                "action": "Ensure your session is bound to this agent",
                "related_tools": ["identity"],
                "workflow": "Identity auto-binds on first tool call. Use identity() to check binding."
            }
        )]

    # Get current governance metrics
    try:
        monitor = mcp_server.get_or_create_monitor(agent_uuid)
        from src.agent_monitor_state import ensure_hydrated
        await ensure_hydrated(monitor, agent_uuid)
        metrics = monitor.get_metrics()

        coherence = float(monitor.state.coherence)
        risk_score = float(metrics.get("mean_risk") or 0.5)
        void_active = bool(monitor.state.void_active)
        status = meta.status

    except Exception as e:
        return system_error_helper(
            "get_governance_metrics",
            e,
            context={"agent_id": agent_id}
        )

    # Safety checks
    safety_checks = {
        "coherence_ok": coherence > 0.40,
        "risk_ok": risk_score < 0.60,
        "no_void": not void_active,
        "status_ok": status in ["paused", "waiting_input", "moderate"]
    }

    if not all(safety_checks.values()):
        failed_checks = [k for k, v in safety_checks.items() if not v]
        return [error_response(
            f"Not safe to resume. Failed checks: {failed_checks}. "
            f"Metrics: coherence={coherence:.3f}, risk={risk_score:.3f}, "
            f"void_active={void_active}, status={status}. "
            f"Check get_governance_metrics and reflect on what needs to change."
        )]

    # Get conditions if provided
    conditions = arguments.get("conditions", [])
    reason = arguments.get("reason", "Direct resume - state is safe")
    event_details = f"Direct resume: {reason}. Conditions: {conditions}"
    persist_error = await _resume_with_persistence(
        meta,
        agent_uuid=agent_uuid,
        event_name="resumed",
        reason=event_details,
        error_response_id=agent_id,
        error_action="resume",
        storage_module=agent_storage,
    )
    if persist_error:
        return persist_error

    response_data = {
        "success": True,
        "message": "Agent resumed successfully",
        "agent_id": agent_id,
        "action": "resumed",
        "conditions": conditions,
        "reason": reason,
        "metrics": {
            "coherence": coherence,
            "risk_score": risk_score,
            "void_active": void_active,
            "previous_status": status
        },
        "note": "Agent resumed. Check get_governance_metrics periodically to stay aware of your state.",
        "deprecation_warning": {
            "tool": "direct_resume_if_safe",
            "status": "deprecated",
            "message": "This tool is deprecated. Use quick_resume() or self_recovery_review() instead.",
            "migration": {
                "if_coherence_gt_0_60_and_risk_lt_0_40": "Use quick_resume() - fastest path, no reflection needed",
                "otherwise": "Use self_recovery_review(reflection='...') - requires reflection but allows recovery at lower thresholds",
                "related_tools": ["quick_resume", "self_recovery_review", "check_recovery_options"]
            },
            "removal_version": "v2.0"
        }
    }
    return success_response(response_data)
