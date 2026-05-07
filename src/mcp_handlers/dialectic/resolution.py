"""
Dialectic Resolution Execution

Handles executing resolutions from dialectic sessions.
Applies conditions and resumes agents based on peer agreement.
"""

from typing import Dict, Any
from datetime import datetime

from src.dialectic_protocol import DialecticSession, Resolution
from src.logging_utils import get_logger
from ..support.condition_parser import parse_condition, apply_condition
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
from src.mcp_handlers.observability.outcome_events import _record_outcome_event_inline
logger = get_logger(__name__)

async def execute_resolution(session: DialecticSession, resolution: Resolution) -> Dict[str, Any]:
    """
    Execute the resolution: resume agent with agreed conditions.
    
    This actually modifies agent state and applies conditions.
    
    Args:
        session: Dialectic session with resolution
        resolution: Resolution object with action and conditions
    
    Returns:
        Dict with execution results
    """
    agent_id = session.paused_agent_id
    
    # Wave 2 audit: force=True dropped per PR #350 precedent. In-memory
    # cache is kept current by process_agent_update / onboard / background
    # load paths; force-reload here triggered 3221 sequential per-agent
    # cache.set awaits (~16s) on every resolution call.
    await mcp_server.load_metadata_async()

    if agent_id not in mcp_server.agent_metadata:
        raise ValueError(f"Agent '{agent_id}' not found")
    
    meta = mcp_server.agent_metadata[agent_id]
    
    # Verify agent is actually paused
    if meta.status != "paused":
        return {
            "success": False,
            "warning": f"Agent status is '{meta.status}', not 'paused'. No action taken."
        }
    
    # Apply conditions using condition parser
    applied_conditions = []
    for condition in resolution.conditions:
        try:
            # Parse condition into structured format
            parsed = parse_condition(condition)
            
            # Apply condition to agent metadata
            apply_result = await apply_condition(parsed, agent_id, mcp_server)
            
            applied_conditions.append(apply_result)
        except Exception as e:
            applied_conditions.append({
                "condition": condition,
                "status": "failed",
                "error": str(e)
            })
            logger.warning(f"Failed to apply condition '{condition}': {e}", exc_info=True)
    
    # Resume the agent (if paused - skip if discovery dispute)
    status_changed = False
    if meta.status == "paused":
        resume_reason = f"Resumed via dialectic synthesis: {resolution.root_cause}"
        meta.status = "active"
        meta.paused_at = None
        meta.add_lifecycle_event("resumed", resume_reason)
        status_changed = True

        # P011: persist runtime-state mutations so paused_at=None and the
        # lifecycle event survive reload (Watcher #81b876bf, #a8b049c8, #9cf3ec6a).
        try:
            from src import agent_storage
            await agent_storage.persist_runtime_state(
                agent_id,
                paused_at=None,
                append_lifecycle_event={
                    "event": "resumed",
                    "timestamp": datetime.now().isoformat(),
                    "reason": resume_reason,
                },
            )
        except Exception as e:
            logger.warning(
                f"persist_runtime_state(resumed) failed for {agent_id[:8]}...: {e}"
            )

        # PostgreSQL: Update status (single source of truth)
        try:
            from src import agent_storage
            await agent_storage.update_agent(agent_id, status="active")
        except Exception as e:
            logger.debug(f"PostgreSQL status update failed: {e}")

    # If linked to discovery, update discovery status based on resolution
    discovery_updated = False
    if session.discovery_id:
        try:
            from src.knowledge_graph import get_knowledge_graph
            graph = await get_knowledge_graph()
            discovery = await graph.get_discovery(session.discovery_id)
            
            if discovery:
                if resolution.action == "resume":  # Agreed correction/verification
                    # Discovery was disputed and corrected
                    if session.dispute_type in ["dispute", "correction"]:
                        # Update discovery details with correction note
                        updated_details = discovery.details
                        if updated_details:
                            updated_details += f"\n\n[Disputed and corrected via dialectic {session.session_id} on {datetime.now().isoformat()}]\nResolution: {resolution.root_cause}"
                        else:
                            updated_details = f"[Disputed and corrected via dialectic {session.session_id} on {datetime.now().isoformat()}]\nResolution: {resolution.root_cause}"
                        
                        await graph.update_discovery(session.discovery_id, {
                            "status": "resolved",
                            "resolved_at": datetime.now().isoformat(),
                            "details": updated_details,
                            "updated_at": datetime.now().isoformat()
                        })
                        discovery_updated = True
                elif resolution.action == "block":  # Dispute rejected, discovery verified
                    # Discovery was disputed but verified correct
                    updated_details = discovery.details
                    if updated_details:
                        updated_details += f"\n\n[Disputed but verified correct via dialectic {session.session_id} on {datetime.now().isoformat()}]\nResolution: {resolution.root_cause}"
                    else:
                        updated_details = f"[Disputed but verified correct via dialectic {session.session_id} on {datetime.now().isoformat()}]\nResolution: {resolution.root_cause}"
                    
                    await graph.update_discovery(session.discovery_id, {
                        "status": "open",  # Back to open (verified)
                        "details": updated_details,
                        "updated_at": datetime.now().isoformat()
                    })
                    discovery_updated = True
        except Exception as e:
            logger.warning(f"Could not update discovery {session.discovery_id}: {e}")
            # Don't fail resolution if discovery update fails

    result = {
        "success": True,
        "agent_id": agent_id,
        "new_status": meta.status,
        "applied_conditions": applied_conditions,
        "resolution_hash": resolution.hash()
    }

    # Add discovery update info if present
    if session.discovery_id:
        result["discovery_id"] = session.discovery_id
        result["discovery_updated"] = discovery_updated
        if discovery_updated:
            result["discovery_status"] = "resolved" if resolution.action == "resume" else "open"

    # Emit outcome_event so downstream calibration / back-tests can correlate
    # resumption with subsequent agent state. dialectic_resolved is neutral
    # (process succeeded); whether conditions hold up is a separate later test.
    # Failure isolation: emit failure must not break resolution execution.
    try:
        applied_count = sum(
            1 for c in applied_conditions
            if isinstance(c, dict) and c.get("status") != "failed"
        )
        await _record_outcome_event_inline({
            "agent_id": agent_id,
            "outcome_type": "dialectic_resolved",
            "is_bad": False,
            "outcome_score": 1.0,
            "decision_action": "proceed",
            "detail": {
                "dialectic_session_id": session.session_id,
                "session_type": getattr(session, "session_type", None),
                "root_cause": resolution.root_cause,
                "conditions": resolution.conditions,
                "conditions_applied": applied_count,
                "conditions_total": len(applied_conditions),
                "synthesis_round": session.synthesis_round,
                "resolution_hash": resolution.hash(),
                "discovery_id": session.discovery_id,
                "status_changed": status_changed,
            },
        })
    except Exception as e:
        logger.warning(
            f"outcome_event emit on dialectic_resolved failed for "
            f"session {session.session_id}: {e}"
        )

    return result

