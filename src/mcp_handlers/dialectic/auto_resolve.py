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
    has_inflight_saga_async,
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



def _describe_reap(
    *,
    phase: str | None,
    awaiting_facilitation: bool,
    idle_seconds: float | None,
) -> str:
    """Say what actually happened, using what this loop already knows.

    The previous text was a single hardcoded string — "Session auto-resolved:
    inactive for >120 minutes" — written identically whether the session had
    stalled mid-negotiation or had been sitting in `awaiting_facilitation`
    waiting for a human who never arrived. Both facts are local variables three
    lines up.

    That cost real diagnosis time: reading those rows produces "the agent opened
    a session and walked away", which is the opposite of what the transcripts
    show — paused agents came back, submitted a synthesis, and were correctly
    refused by the self-clear guard. A reader who trusts this field reconstructs
    the wrong causal story, and the row is the only artifact that outlives the
    session.

    Deliberately does NOT claim a verdict. The sweeper does not load the
    transcript, so it cannot know whether the reviewer rejected or the parties
    simply stopped; asserting either would trade one confident wrong sentence
    for another. It reports what it observed and points at the record that has
    the rest.
    """
    idle = ""
    if idle_seconds is not None and idle_seconds >= 0:
        idle = f" after {idle_seconds / 3600:.1f}h idle"

    where = f" in phase '{phase}'" if phase else ""

    if awaiting_facilitation:
        return (
            f"Reaped by the inactivity sweep{idle}{where} while awaiting human "
            "facilitation. No operator acted. This is a sweep outcome, not a "
            "reviewer verdict, and not evidence that the paused agent abandoned "
            "the session."
        )
    return (
        f"Reaped by the inactivity sweep{idle}{where}. This is a sweep outcome, "
        "not a reviewer verdict — read the last synthesis for the position that "
        "was standing when the sweep ran."
    )


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
            awaiting_facilitation = bool(session.get("awaiting_facilitation"))

            if not session_id:
                continue

            # Saga-inflight guard (C1, council 2026-06-28): if a BEAM session
            # owner is mid-resolution for this session, skip it entirely this
            # cycle. Marking it failed / reassigning its reviewer here would race
            # the saga and corrupt the outcome. Fail-open (no saga infra -> no
            # skip), so this is a no-op until BEAM begins writing sagas.
            if await has_inflight_saga_async(session_id):
                logger.info(
                    f"Skipping stuck-session sweep for {session_id[:16]}...: "
                    "resolution saga in flight (BEAM owns this transition)"
                )
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
                            if not await update_session_reviewer_async(session_id, new_reviewer):
                                # Session reached a terminal state during
                                # reviewer selection (dual-writer TOCTOU);
                                # nothing was written, so don't narrate a
                                # reassignment that never happened.
                                logger.info(
                                    f"Session {session_id[:16]} went terminal mid-sweep; "
                                    "reviewer reassignment skipped"
                                )
                                continue
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

            # A session already awaiting human facilitation runs on the HUMAN's
            # clock, not the stuck-process clock. STUCK_SESSION_THRESHOLD (2h)
            # measures "this process is wedged"; an operator may simply be
            # asleep. FACILITATION_TIMEOUT (4h) exists for exactly this and was
            # only reachable inside the ANTITHESIS branch, so a session that
            # asked for a human at THESIS — which is where all 50 facilitation
            # events actually come from — fell straight through to FAILED at 2h.
            #
            # That is the whole dead-end: swept to `failed`, and `reassign` then
            # refuses any phase but THESIS/ANTITHESIS, so the session became
            # unfacilitatable before anyone could act. 32 of 38 such sessions
            # are scheduled probes, but the other 6 were real requests that
            # nothing could be done with by the time they were noticed.
            if awaiting_facilitation and check_time and check_time > fail_time:
                logger.info(
                    f"Session {session_id[:16]} awaiting facilitation "
                    f"({(now - check_time).total_seconds()/3600:.1f}h) — holding for the "
                    f"operator until {FACILITATION_TIMEOUT.total_seconds()/3600:.0f}h"
                )
                continue

            # Fall through: mark as FAILED (session too old or non-reassignable phase)
            try:
                if not await update_session_status_async(session_id, "failed"):
                    # Another writer finished this session after our early
                    # saga/staleness checks; the guarded UPDATE refused the
                    # overwrite. Skip the failure message and the count.
                    logger.info(
                        f"Session {session_id[:16]} went terminal mid-sweep; reap skipped"
                    )
                    continue
                failure_reason = _describe_reap(
                    phase=phase,
                    awaiting_facilitation=awaiting_facilitation,
                    idle_seconds=(now - check_time).total_seconds() if check_time else None,
                )
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
