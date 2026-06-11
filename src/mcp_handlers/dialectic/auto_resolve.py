"""
Auto-Resolve Stuck Dialectic Sessions

Automatically handles sessions that are stuck/inactive for >2 hours.
First attempts reviewer re-assignment, then marks awaiting facilitation,
and only fails sessions after extended inactivity (4+ hours total).
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, Any

from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
from src.dialectic_db import (
    get_active_sessions_async,
    update_session_status_async,
    update_session_reviewer_async,
    add_message_async,
)

logger = get_logger(__name__)

# Stuck session threshold: 2 hours of inactivity
# Rationale: DialecticProtocol.MAX_ANTITHESIS_WAIT is 2 hours - agents need time to think
STUCK_SESSION_THRESHOLD = timedelta(hours=2)

# Extended threshold before marking FAILED (gives human time to facilitate)
FACILITATION_TIMEOUT = timedelta(hours=4)


def _parse_timestamp(value) -> datetime | None:
    """Parse a timestamp value into a timezone-aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        try:
            if 'T' in value:
                dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            else:
                dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None
    return None


async def auto_resolve_stuck_sessions() -> Dict[str, Any]:
    """
    Handle sessions that are stuck/inactive.

    For each stuck session:
    1. If reviewer is gone and phase is ANTITHESIS: try auto re-assignment
    2. If no replacement available: mark awaiting_facilitation (not FAILED)
    3. Only mark FAILED after extended inactivity (4+ hours)

    Returns:
        Dict with counts of resolved/reassigned sessions and details
    """
    try:
        now = datetime.now(timezone.utc)
        threshold_time = now - STUCK_SESSION_THRESHOLD
        fail_time = now - FACILITATION_TIMEOUT

        active_sessions = await get_active_sessions_async(limit=100)

        if not active_sessions:
            return {
                "resolved_count": 0,
                "reassigned_count": 0,
                "message": "No active sessions found"
            }

        # Filter to stuck sessions (inactive for >2 hours)
        stuck_sessions = []
        for session in active_sessions:
            check_time = _parse_timestamp(session.get("updated_at") or session.get("created_at"))
            if check_time and check_time < threshold_time:
                stuck_sessions.append(session)

        if not stuck_sessions:
            return {
                "resolved_count": 0,
                "reassigned_count": 0,
                "message": "No stuck sessions found"
            }

        resolved_count = 0
        reassigned_count = 0
        facilitation_count = 0
        details = []

        for session in stuck_sessions:
            session_id = session.get("session_id")
            paused_agent_id = session.get("paused_agent_id")
            reviewer_agent_id = session.get("reviewer_agent_id")
            phase = session.get("phase")

            if not session_id:
                continue

            check_time = _parse_timestamp(session.get("updated_at") or session.get("created_at"))

            # For ANTITHESIS phase: try reviewer re-assignment
            if phase in ("antithesis", "ANTITHESIS") and reviewer_agent_id:
                # Wave 2 audit: force=True dropped per PR #350 precedent. This
                # is a periodic resolver that fired on every session × phase;
                # force-reload at each iteration was N×3221 awaits. If the
                # reviewer was paused, the regular write path already updated
                # the in-memory cache; if not, the next iteration sees it.
                await mcp_server.load_metadata_async()
                reviewer_meta = mcp_server.agent_metadata.get(reviewer_agent_id)
                reviewer_gone = not reviewer_meta or getattr(reviewer_meta, 'status', None) == "paused"

                if reviewer_gone:
                    # Try auto re-assignment
                    from .reviewer import select_reviewer
                    try:
                        new_reviewer = await select_reviewer(
                            paused_agent_id=paused_agent_id,
                            metadata=mcp_server.agent_metadata,
                            exclude_agent_ids=[paused_agent_id, reviewer_agent_id],
                        )
                    except Exception as e:
                        logger.warning(f"Auto re-selection failed for {session_id[:16]}: {e}")
                        new_reviewer = None

                    if new_reviewer:
                        try:
                            await update_session_reviewer_async(session_id, new_reviewer)
                            await add_message_async(
                                session_id=session_id,
                                agent_id="system",
                                message_type="system",
                                reasoning=f"Reviewer auto-reassigned: {reviewer_agent_id} -> {new_reviewer} (previous reviewer unresponsive)",
                            )
                            reassigned_count += 1
                            details.append({
                                "session_id": session_id,
                                "paused_agent_id": paused_agent_id,
                                "phase": phase,
                                "action": "reviewer_reassigned",
                                "old_reviewer": reviewer_agent_id,
                                "new_reviewer": new_reviewer,
                            })
                            logger.info(
                                f"Auto-reassigned reviewer for {session_id[:16]}: "
                                f"{reviewer_agent_id} -> {new_reviewer}"
                            )
                            continue  # Session saved, move to next
                        except Exception as e:
                            logger.warning(f"Could not persist reviewer reassignment for {session_id[:16]}: {e}")

                    # No replacement found — mark awaiting facilitation if not too old
                    if check_time and check_time > fail_time:
                        try:
                            await add_message_async(
                                session_id=session_id,
                                agent_id="system",
                                message_type="system",
                                reasoning=f"Reviewer '{reviewer_agent_id}' unresponsive. Awaiting human facilitation.",
                            )
                            facilitation_count += 1
                            details.append({
                                "session_id": session_id,
                                "paused_agent_id": paused_agent_id,
                                "phase": phase,
                                "action": "awaiting_facilitation",
                                "stuck_reviewer": reviewer_agent_id,
                            })
                            logger.info(
                                f"Session {session_id[:16]} awaiting human facilitation "
                                f"(reviewer {reviewer_agent_id} unresponsive)"
                            )
                            continue  # Don't fail yet — give human time
                        except Exception as e:
                            logger.warning(f"Could not add facilitation message for {session_id[:16]}: {e}")

            # Fall through: mark as FAILED (session too old or non-reassignable phase)
            try:
                await update_session_status_async(session_id, "failed")
                failure_reason = f"Session auto-resolved: inactive for >{STUCK_SESSION_THRESHOLD.total_seconds()/60:.0f} minutes"
                try:
                    await add_message_async(
                        session_id=session_id,
                        agent_id="system",
                        message_type="failed",
                        reasoning=failure_reason,
                    )
                except Exception as msg_error:
                    logger.warning(f"Could not add failure message: {msg_error}")

                resolved_count += 1
                details.append({
                    "session_id": session_id,
                    "paused_agent_id": paused_agent_id,
                    "phase": phase,
                    "action": "failed",
                    "reason": "inactive_too_long",
                })
                logger.info(f"Auto-resolved stuck session {session_id[:16]} as FAILED (paused_agent: {paused_agent_id}, phase: {phase})")

            except Exception as e:
                logger.warning(f"Could not resolve session {session_id}: {e}")

        return {
            "resolved_count": resolved_count,
            "reassigned_count": reassigned_count,
            "facilitation_count": facilitation_count,
            "details": details,
            "message": (
                f"Processed {len(stuck_sessions)} stuck session(s): "
                f"{reassigned_count} reassigned, {facilitation_count} awaiting facilitation, "
                f"{resolved_count} failed"
            ),
        }

    except Exception as e:
        logger.error(f"Error auto-resolving stuck sessions: {e}", exc_info=True)
        return {
            "resolved_count": 0,
            "reassigned_count": 0,
            "error": str(e),
            "message": "Failed to auto-resolve stuck sessions"
        }


async def check_and_resolve_stuck_sessions() -> Dict[str, Any]:
    """
    Check for stuck sessions and auto-resolve them.
    Called automatically when checking for active sessions.

    Returns:
        Dict with resolution results
    """
    try:
        return await auto_resolve_stuck_sessions()
    except Exception as e:
        logger.warning(f"Could not auto-resolve stuck sessions: {e}")
        return {"resolved_count": 0, "reassigned_count": 0, "error": str(e)}
