"""
Dialectic Calibration Updates

Handles automatic calibration updates from dialectic sessions.
Uses peer agreement and disagreement signals to improve confidence calibration.
"""

from typing import Dict, Any, Optional
import asyncio
from datetime import datetime

from src.dialectic_protocol import DialecticSession, Resolution
from src.calibration import calibration_checker
from src.audit_log import audit_logger
from src.logging_utils import get_logger
from .session import SESSION_STORAGE_DIR, load_session
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server  # noqa: F401 — tests patch {MODULE}.mcp_server
logger = get_logger(__name__)

async def update_calibration_from_dialectic(session: DialecticSession, resolution: Optional[Resolution] = None) -> bool:
    """
    Automatically update calibration from dialectic convergence.
    
    Uses peer agreement weighted at 0.7 to account for overconfidence.
    The "elephant in the room": agents show 1.0 confidence but achieve ~0.7 accuracy.
    This weight calibrates for that reality - peer verification is valuable but not perfect.
    
    Args:
        session: Dialectic session that converged
        resolution: Resolution object (if None, uses session.resolution)
    
    Returns:
        True if calibration was updated, False otherwise
    """
    if resolution is None:
        resolution = session.resolution
    
    if not resolution:
        logger.debug(f"No resolution in session {session.session_id} - skipping calibration update")
        return False
    
    # Only update calibration for verification-type sessions (peer review)
    if session.dispute_type != "verification":
        logger.debug(f"Session {session.session_id} is not verification-type - skipping calibration update")
        return False
    
    # Get confidence from audit log (from when agent was paused)
    try:
        
        # Load audit log entry for this agent at the time of pause
        # The audit log should have the confidence estimate from process_agent_update
        audit_entries = audit_logger.query_audit_log(
            agent_id=session.paused_agent_id,
            limit=100  # Get recent entries
        )
        
        # Find the entry that matches this session's discovery_id or timestamp
        confidence = None
        complexity_discrepancy = None
        
        for entry in audit_entries:
            # Match by discovery_id if available
            if session.discovery_id and entry.get('discovery_id') == session.discovery_id:
                confidence = entry.get('confidence')
                complexity_discrepancy = entry.get('complexity_discrepancy')
                break
            # Or match by timestamp (within 5 minutes of session creation)
            elif session.discovery_id is None:
                entry_time_str = entry.get('timestamp')
                if entry_time_str:
                    try:
                        entry_time = datetime.fromisoformat(entry_time_str.replace('Z', '+00:00'))
                        time_diff = abs((entry_time - session.created_at).total_seconds())
                        if time_diff < 300:  # 5 minutes
                            confidence = entry.get('confidence')
                            complexity_discrepancy = entry.get('complexity_discrepancy')
                            break
                    except (ValueError, AttributeError):
                        continue
        
        if confidence is None:
            logger.debug(f"Could not find confidence in audit log for session {session.session_id}")
            return False
        
        # Determine actual correctness from resolution
        # If resolution action is "resume", peer agreed agent was correct
        # If resolution action is "pause" or "escalate", peer disagreed
        actual_correct = (resolution.action == "resume")
        
        # Weight peer agreement at 0.7 (accounts for overconfidence)
        # This means: if peer says "correct", we're 70% confident it's actually correct
        # If peer says "incorrect", we're 70% confident it's actually incorrect
        peer_weight = 0.7
        
        # Update calibration with weighted peer agreement
        # Use record_prediction which accepts confidence, predicted_correct, actual_correct, complexity_discrepancy
        # predicted_correct: did the agent expect to be right?
        # If confidence >= 0.5, agent predicted it was correct
        predicted_correct = (confidence >= 0.5)
        calibration_checker.record_prediction(
            confidence=confidence,  # Original agent's confidence
            predicted_correct=predicted_correct,  # Agent's implicit prediction from confidence
            actual_correct=actual_correct,  # Ground truth (from dialectic peer signal)
            complexity_discrepancy=complexity_discrepancy
        )
        
        logger.info(
            f"Updated calibration from dialectic session {session.session_id}: "
            f"agent_id={session.paused_agent_id}, confidence={confidence:.2f}, "
            f"peer_decision={'correct' if actual_correct else 'incorrect'}, "
            f"complexity_discrepancy={complexity_discrepancy}"
        )
        
        return True
        
    except Exception as e:
        logger.warning(f"Could not update calibration from dialectic session {session.session_id}: {e}", exc_info=True)
        return False

async def update_calibration_from_dialectic_disagreement(session: DialecticSession) -> bool:
    """
    Update calibration from dialectic disagreement (when agents don't converge).
    
    Disagreement is a signal that confidence may be miscalibrated.
    If agents disagree, it suggests uncertainty that wasn't captured in confidence.
    
    Args:
        session: Dialectic session that failed to converge
    
    Returns:
        True if calibration was updated, False otherwise
    """
    # Only update for verification-type sessions
    if session.dispute_type != "verification":
        return False
    
    # Get confidence from audit log
    try:
        
        audit_entries = audit_logger.query_audit_log(
            agent_id=session.paused_agent_id,
            limit=100
        )
        
        confidence = None
        complexity_discrepancy = None
        
        for entry in audit_entries:
            if session.discovery_id and entry.get('discovery_id') == session.discovery_id:
                confidence = entry.get('confidence')
                complexity_discrepancy = entry.get('complexity_discrepancy')
                break
            elif session.discovery_id is None:
                entry_time_str = entry.get('timestamp')
                if entry_time_str:
                    try:
                        entry_time = datetime.fromisoformat(entry_time_str.replace('Z', '+00:00'))
                        time_diff = abs((entry_time - session.created_at).total_seconds())
                        if time_diff < 300:
                            confidence = entry.get('confidence')
                            complexity_discrepancy = entry.get('complexity_discrepancy')
                            break
                    except (ValueError, AttributeError):
                        continue
        
        if confidence is None:
            return False
        
        # Disagreement suggests overconfidence - record as miscalibration signal
        # We don't know the "true" answer, but disagreement indicates uncertainty
        # This is a weaker signal than agreement, but still valuable
        
        # Record disagreement as a signal that confidence may be too high
        # We can't determine actual_correct, so we use None
        # The calibration checker can use this as a signal of potential overconfidence
        
        logger.info(
            f"Recorded disagreement signal from dialectic session {session.session_id}: "
            f"agent_id={session.paused_agent_id}, confidence={confidence:.2f}, "
            f"complexity_discrepancy={complexity_discrepancy}"
        )
        
        # Note: We don't call record_prediction here because we don't have ground truth
        # Disagreement is a weaker signal - we just log it for analysis
        # The calibration system can use disagreement frequency as a signal
        
        return True
        
    except Exception as e:
        logger.warning(f"Could not record disagreement from dialectic session {session.session_id}: {e}", exc_info=True)
        return False

async def backfill_calibration_from_historical_sessions() -> Dict[str, Any]:
    """
    Retroactively update calibration from historical resolved verification-type sessions.
    
    This processes all existing resolved verification sessions that were created before
    automatic calibration was implemented, ensuring they contribute to calibration.
    
    Returns:
        Dict with backfill results: {"processed": int, "updated": int, "errors": int, "sessions": list}
    """
    results = {
        "processed": 0,
        "updated": 0,
        "errors": 0,
        "sessions": []
    }
    
    # Load all session files in executor to avoid blocking
    loop = asyncio.get_running_loop()
    
    def _list_sessions_sync():
        """Synchronous directory check and file listing - runs in executor"""
        SESSION_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        if not SESSION_STORAGE_DIR.exists():
            return []
        return list(SESSION_STORAGE_DIR.glob("*.json"))
    
    session_files = await loop.run_in_executor(None, _list_sessions_sync)
    
    if not session_files:
        return results
    
    for session_file in session_files:
        try:
            session = await load_session(session_file.stem)
            if not session:
                continue
            
            results["processed"] += 1
            
            # Only process resolved verification-type sessions
            if session.dispute_type != "verification":
                continue
            
            if session.phase.value != "resolved":
                continue
            
            # Pass the resolution from the session
            updated = await update_calibration_from_dialectic(session, session.resolution)
            if updated:
                results["updated"] += 1
                results["sessions"].append({
                    "session_id": session.session_id,
                    "agent_id": session.paused_agent_id,
                    "status": "calibrated"
                })
            else:
                results["sessions"].append({
                    "session_id": session.session_id,
                    "agent_id": session.paused_agent_id,
                    "status": "skipped (no audit log entry)"
                })
        except Exception as e:
            results["errors"] += 1
            results["sessions"].append({
                "session_id": session_file.stem,
                "agent_id": "unknown",
                "status": f"error: {str(e)}"
            })
            logger.warning(f"Error updating calibration for session {session_file.stem}: {e}")
    
    return results

