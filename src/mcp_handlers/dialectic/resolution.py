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
        meta.status = "active"
        meta.paused_at = None
        meta.add_lifecycle_event("resumed", f"Resumed via dialectic synthesis: {resolution.root_cause}")
        status_changed = True

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
    
    return result

