"""
MCP Handlers for Circuit Breaker Dialectic Protocol

Implements MCP tools for peer-review dialectic resolution of circuit breaker states.
"""

from typing import Dict, Any, Sequence, Optional, List
from mcp.types import TextContent
import asyncio
import json
from datetime import datetime, timedelta, timezone
import random

# Import type definitions
from ..types import (
    ToolArgumentsDict,
    DialecticSessionDict,
    ResolutionDict
)

from src.dialectic_protocol import (
    DialecticSession,
    DialecticMessage,
    DialecticPhase,
    Resolution,
    QuorumVote,
    QuorumResult,
    tally_quorum_votes,
)
from ..utils import success_response, error_response, require_registered_agent
from ..decorators import mcp_tool
from ..support.coerce import coerce_bool, resolve_agent_uuid
from .auth import resolve_dialectic_agent_id
from .responses import (
    default_cooldown_steps,
    default_escalate_steps,
    default_quorum_steps,
    default_resume_steps,
    get_agent_not_found_recovery,
    get_reviewer_stuck_recovery,
    get_session_exception_recovery,
    get_session_timeout_recovery,
    llm_failed_recovery,
    llm_incomplete_recovery,
    llm_missing_root_cause_recovery,
    llm_unavailable_recovery,
    missing_session_id_recovery,
    missing_session_or_agent_recovery,
    no_sessions_found_recovery,
    next_step_execution_failed,
    next_step_negotiate_synthesis,
    next_step_no_consensus,
    next_step_quorum_initiated,
    next_step_resume_not_applied,
    next_step_resumed,
    next_step_submit_antithesis,
    session_not_found_recovery,
)

async def _resolve_dialectic_agent_id(
    arguments: Dict[str, Any],
    *,
    enforce_session_ownership: bool = False,
) -> tuple:
    """Backward-compatible wrapper around shared dialectic auth policy."""
    return await resolve_dialectic_agent_id(
        arguments, enforce_session_ownership=enforce_session_ownership
    )
from src.logging_utils import get_logger
import sys
import os

logger = get_logger(__name__)

# Import from mcp_server_std module (using shared utility)
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
# Import session persistence from new module
from .session import (
    save_session,
    load_session,
    load_session_as_dict,
    load_all_sessions,
    list_all_sessions,
    ACTIVE_SESSIONS,
    SESSION_STORAGE_DIR,
    _SESSION_METADATA_CACHE,
    _CACHE_TTL
)

# Session metadata cache for fast lookups (re-exported for backward compatibility)
# Format: {agent_id: {'in_session': bool, 'timestamp': float, 'session_ids': [str]}}

# Check if aiofiles is available for async I/O
try:
    import aiofiles
    AIOFILES_AVAILABLE = True
except ImportError:
    AIOFILES_AVAILABLE = False

# NOTE: save_session, load_session, and load_all_sessions are now imported from dialectic_session.py
# NOTE: Calibration functions are now imported from dialectic_calibration.py
# NOTE: Resolution execution is now imported from dialectic_resolution.py
from .calibration import (
    update_calibration_from_dialectic,
    update_calibration_from_dialectic_disagreement,
    backfill_calibration_from_historical_sessions
)
from .resolution import execute_resolution
from .reviewer import select_reviewer, select_quorum_reviewers, is_agent_in_active_session

# Import PostgreSQL async functions for dialectic session storage
from src.dialectic_db import (
    create_session_async as pg_create_session,
    update_session_phase_async as pg_update_phase,
    update_session_reviewer_async as pg_update_reviewer,
    add_message_async as pg_add_message,
    resolve_session_async as pg_resolve_session,
    get_session_async as pg_get_session,
    get_session_by_agent_async as pg_get_session_by_agent,
    get_all_sessions_by_agent_async as pg_get_all_sessions_by_agent,
)

# Import database abstraction for dual-write (Phase 4 migration)
from src.db import get_db

# ==============================================================================
# NOTE: Dialectic handlers (Feb 2026)
# ==============================================================================
# ACTIVE: request_dialectic_review, submit_thesis, submit_antithesis, submit_synthesis
# ACTIVE: get_dialectic_session, list_dialectic_sessions, llm_assisted_dialectic
# Still removed: request_exploration_session, nudge_dialectic_session, handle_self_recovery
# ==============================================================================

async def check_reviewer_stuck(session: DialecticSession) -> bool:
    """
    Check if reviewer is stuck (paused or hasn't responded after thesis submission).

    Only meaningful during ANTITHESIS phase — that's when the reviewer needs to respond.
    Uses thesis submission time (not session creation time) and aligns with
    the protocol's MAX_ANTITHESIS_WAIT (2 hours).

    Returns:
        True if reviewer is stuck, False otherwise
    """
    # Only check during ANTITHESIS phase — reviewer hasn't been asked in other phases
    if session.phase != DialecticPhase.ANTITHESIS:
        return False

    reviewer_id = session.reviewer_agent_id
    if not reviewer_id:
        return False  # No reviewer assigned yet — not stuck, just unassigned

    # Wave 2 audit: force=True dropped per PR #350 precedent. The reviewer's
    # status (paused/active) is updated via the regular lifecycle write paths
    # which propagate to the in-memory dict. If the cache is briefly stale
    # for this one reviewer, the next stuck-check iteration sees the truth.
    await mcp_server.load_metadata_async()

    reviewer_meta = mcp_server.agent_metadata.get(reviewer_id)
    if not reviewer_meta:
        return True  # Reviewer doesn't exist = stuck

    # Check if reviewer is paused
    if reviewer_meta.status == "paused":
        return True

    # Measure from thesis submission time (when reviewer was actually asked to respond)
    # Aligned with protocol's MAX_ANTITHESIS_WAIT (2 hours)
    try:
        thesis_time = session.get_thesis_timestamp()
        if thesis_time is None:
            return False  # No thesis yet — reviewer can't be stuck
        # Handle timezone mismatch
        if thesis_time.tzinfo is None:
            wait_time = datetime.now() - thesis_time
        else:
            wait_time = datetime.now(timezone(timedelta(0))) - thesis_time
        stuck_threshold = timedelta(hours=2)
        return wait_time > stuck_threshold
    except (ValueError, TypeError, AttributeError):
        return False  # Can't determine — don't kill the session


def _read_proposed_conditions(arguments: Dict[str, Any]) -> List[str]:
    """Read proposed_conditions, accepting `conditions` as an alias.

    The dialectic tool surface exposes both `proposed_conditions` (used by
    thesis/antithesis/synthesis) and `conditions` (used by vote). Callers
    occasionally pass `conditions=[...]` to a synthesis call by mistake —
    the field is silently dropped, which produces a synthesis message with
    empty conditions, which trips check_hard_limits at finalize time and
    leaves the session in a self-contradictory phase=failed /
    resolution.action=resume terminal state.

    Accept either name to prevent that silent-drop class of bug. A
    falsy/missing `proposed_conditions` falls back to `conditions`; explicit
    empty values are preserved as empty.
    """
    proposed = arguments.get('proposed_conditions')
    if proposed:
        return proposed
    fallback = arguments.get('conditions')
    if fallback:
        return fallback
    return [] if proposed is None else proposed


def _meta_value(meta: Any, key: str, default: Any = None) -> Any:
    """Read a field from metadata regardless of object-vs-dict representation."""
    if meta is None:
        return default
    if isinstance(meta, dict):
        return meta.get(key, default)
    return getattr(meta, key, default)


def _agent_label(agent_id: Optional[str]) -> Optional[str]:
    """Best-effort friendly label for an agent UUID."""
    if not agent_id:
        return None
    meta = getattr(mcp_server, "agent_metadata", {}).get(agent_id)
    for key in ("label", "public_agent_id", "structured_id", "agent_id"):
        value = _meta_value(meta, key)
        if value:
            return str(value)
    return None


def _agent_status(agent_id: Optional[str]) -> Optional[str]:
    """Best-effort live status for an agent UUID."""
    if not agent_id:
        return None
    meta = getattr(mcp_server, "agent_metadata", {}).get(agent_id)
    status = _meta_value(meta, "status")
    return str(status) if status else None


def _agent_display(agent_id: Optional[str]) -> str:
    """Human-readable agent reference."""
    if not agent_id:
        return "unassigned"
    return _agent_label(agent_id) or agent_id


def _build_dialectic_actionability(session_data: Dict[str, Any]) -> Dict[str, Any]:
    """Annotate a session payload with concrete next-action metadata."""
    paused_agent_id = session_data.get("paused_agent_id")
    reviewer_agent_id = session_data.get("reviewer_agent_id") or session_data.get("reviewer")
    phase = str(session_data.get("phase") or "").lower()

    try:
        from ..context import get_context_agent_id
        current_agent_id = get_context_agent_id()
    except Exception:
        current_agent_id = None

    current_agent_role = None
    if current_agent_id:
        if current_agent_id == paused_agent_id:
            current_agent_role = "paused_agent"
        elif reviewer_agent_id and current_agent_id == reviewer_agent_id:
            current_agent_role = "reviewer"
        else:
            current_agent_role = "observer"

    allowed_agent_ids: List[str] = []
    required_role = "observer"
    required_agent_id = None
    required_agent_label = None
    recommended_action = "Inspect the session transcript."

    if phase == "thesis":
        required_role = "paused_agent"
        required_agent_id = paused_agent_id
        required_agent_label = _agent_label(paused_agent_id)
        allowed_agent_ids = [paused_agent_id] if paused_agent_id else []
        recommended_action = (
            f"Paused agent '{_agent_display(paused_agent_id)}' should submit thesis."
        )
    elif phase == "antithesis":
        required_role = "reviewer"
        required_agent_id = reviewer_agent_id
        required_agent_label = _agent_label(reviewer_agent_id)
        if reviewer_agent_id:
            allowed_agent_ids = [reviewer_agent_id]
            recommended_action = (
                f"Reviewer '{_agent_display(reviewer_agent_id)}' should submit antithesis."
            )
            if current_agent_id and current_agent_id not in {paused_agent_id, reviewer_agent_id}:
                recommended_action += (
                    " If the operator wants this bound agent to answer instead, retry antithesis with "
                    "`take_over_if_requested=true` or use `dialectic(action='reassign')`."
                )
        else:
            if current_agent_id and current_agent_id != paused_agent_id:
                allowed_agent_ids = [current_agent_id]
            recommended_action = (
                "Any eligible reviewer may claim this session by submitting antithesis."
            )
    elif phase == "synthesis":
        required_role = "participant"
        required_agent_id = None
        required_agent_label = None
        allowed_agent_ids = [
            agent_id for agent_id in [paused_agent_id, reviewer_agent_id] if agent_id
        ]
        recommended_action = (
            "Paused agent and reviewer should negotiate via submit_synthesis() until convergence."
        )
    elif phase in {"resolved", "failed", "escalated", "quorum_voting"}:
        required_role = "none"
        recommended_action = (
            "No direct write step is pending. Review the transcript or follow the recorded resolution."
        )

    current_agent_can_submit: Optional[bool]
    if current_agent_id is None:
        current_agent_can_submit = None
    elif phase == "antithesis" and reviewer_agent_id is None:
        current_agent_can_submit = current_agent_id != paused_agent_id
    else:
        current_agent_can_submit = current_agent_id in allowed_agent_ids

    return {
        "paused_agent_label": _agent_label(paused_agent_id),
        "reviewer_label": _agent_label(reviewer_agent_id),
        "reviewer_status": _agent_status(reviewer_agent_id),
        "required_role": required_role,
        "required_agent_id": required_agent_id,
        "required_agent_label": required_agent_label,
        "allowed_agent_ids": allowed_agent_ids,
        "current_agent_id": current_agent_id,
        "current_agent_role": current_agent_role,
        "current_agent_can_submit": current_agent_can_submit,
        "recommended_action": recommended_action,
    }


async def _validate_explicit_reviewer_candidate(
    session: DialecticSession,
    candidate_id: str,
) -> Optional[Sequence[TextContent]]:
    """Validate a concrete reviewer assignment target."""
    # Wave 2 audit: force=True dropped per PR #350 precedent. Single-agent
    # existence check; in-memory cache is fresh enough for validation.
    await mcp_server.load_metadata_async()
    candidate_meta = mcp_server.agent_metadata.get(candidate_id)
    if not candidate_meta:
        return [error_response(
            f"Agent '{candidate_id}' not found in metadata",
            recovery=get_agent_not_found_recovery(),
        )]

    status = _meta_value(candidate_meta, "status")
    if status == "paused":
        return [error_response(
            f"Agent '{candidate_id}' is paused and cannot review",
            error_code="REVIEWER_PAUSED",
            error_category="validation_error",
        )]

    if candidate_id == session.paused_agent_id:
        return [error_response(
            "Cannot assign paused agent as its own reviewer (use reviewer_mode='self' for self-review)",
            error_code="SELF_REVIEW",
            error_category="validation_error",
        )]

    return None


async def _apply_reviewer_reassignment(
    session_id: str,
    session: DialecticSession,
    new_reviewer_id: str,
    *,
    reason: str,
    strict_persistence: bool = False,
) -> Dict[str, Any]:
    """Update the reviewer assignment in memory and persistence layers."""
    old_reviewer_id = session.reviewer_agent_id
    session.reviewer_agent_id = new_reviewer_id
    session.awaiting_facilitation = False

    reasoning = (
        f"Reviewer reassigned: {old_reviewer_id or 'unassigned'} -> {new_reviewer_id}. "
        f"Reason: {reason}"
    )
    reassign_msg = DialecticMessage(
        phase=session.phase.value,
        agent_id="system",
        timestamp=datetime.now(timezone.utc).isoformat(),
        reasoning=reasoning,
    )
    session.transcript.append(reassign_msg)

    try:
        await pg_update_reviewer(session_id, new_reviewer_id)
        await pg_add_message(
            session_id=session_id,
            agent_id="system",
            message_type="system",
            reasoning=reasoning,
        )
    except Exception as e:
        logger.error(f"Failed to persist reviewer reassignment: {e}")
        if strict_persistence:
            raise

    return {
        "old_reviewer_id": old_reviewer_id,
        "new_reviewer_id": new_reviewer_id,
        "reason": reason,
        "reasoning": reasoning,
    }

@mcp_tool("request_dialectic_review", timeout=60.0, register=True)
async def handle_request_dialectic_review(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Create a dialectic recovery session.

    This is a lightweight entry point restored for recovery workflows.
    It sets up the session and persists it, but does not auto-progress the protocol.
    """
    # Require a registered agent and use authoritative UUID for internal IDs
    # (Same pipeline as onboard/identity — dialectic was previously wonky using get_bound_agent_id)
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]

    agent_uuid = resolve_agent_uuid(arguments, agent_id)

    # SECURITY: Verify ownership via session binding (UUID-based auth, Dec 2025)
    from ..utils import verify_agent_ownership
    if not verify_agent_ownership(agent_uuid, arguments):
        return [error_response(
            "Authentication required. You can only request recovery for your own agent.",
            error_code="AUTH_REQUIRED",
            error_category="auth_error",
            recovery={
                "action": "Ensure your session is bound to this agent",
                "related_tools": ["identity"],
                "workflow": "Identity auto-binds on first tool call. Use identity() to check binding."
            },
            arguments=arguments
        )]

    meta = mcp_server.agent_metadata.get(agent_uuid)
    if not meta:
        return [error_response(
            f"Agent '{agent_uuid}' not found.",
            error_code="AGENT_NOT_FOUND",
            error_category="validation_error",
            recovery={
                "action": "Call identity() or process_agent_update() to register.",
                "related_tools": ["identity", "process_agent_update"]
            },
            arguments=arguments
        )]

    # Skip if agent is waiting for input (not stuck)
    if meta.status == "waiting_input":
        return success_response({
            "success": True,
            "skipped": True,
            "reason": "Agent is waiting_input; not stuck",
            "agent_id": agent_uuid,
            "status": meta.status,
            "recommendation": "No dialectic needed. Use process_agent_update() when new work starts."
        })

    # Skip auto-triggered sessions for non-reasoning agents (embodied/anima).
    # These agents can't submit theses — sessions would remain stuck at thesis phase forever.
    reviewer_mode = arguments.get("reviewer_mode", "")
    if reviewer_mode == "auto":
        agent_tags = set(t.lower() for t in (getattr(meta, "tags", None) or []))
        if agent_tags & {"autonomous", "embodied", "anima"}:
            logger.info("[DIALECTIC] Skipping auto-recovery for non-reasoning agent")
            return success_response({
                "success": True,
                "skipped": True,
                "reason": "Non-reasoning agent cannot participate in dialectic",
                "agent_id": agent_uuid,
                "agent_tags": list(agent_tags & {"autonomous", "embodied", "anima"}),
                "recommendation": "Pause event logged. Recovery handled via agent lifecycle, not dialectic."
            })

    # Prevent duplicate sessions
    if await is_agent_in_active_session(agent_uuid):
        return [error_response(
            "Agent already has an active dialectic session.",
            error_code="SESSION_EXISTS",
            error_category="validation_error",
            recovery={
                "action": "Use get_dialectic_session() to view the active session",
                "related_tools": ["get_dialectic_session"]
            },
            arguments=arguments
        )]

    reason = arguments.get("reason", "Dialectic review requested")
    session_type = arguments.get("session_type", "review")
    discovery_id = arguments.get("discovery_id")
    dispute_type = arguments.get("dispute_type")
    topic = arguments.get("topic") or reason
    reviewer_mode = arguments.get("reviewer_mode", "auto")  # auto|self|llm
    max_synthesis_rounds = arguments.get("max_synthesis_rounds", 5)
    # Determine trigger source: explicit param > inferred from reason > "manual"
    trigger_source = arguments.get("trigger_source")
    if not trigger_source:
        reason_lower = (reason or "").lower()
        if "auto-recovery" in reason_lower or "auto-triggered" in reason_lower:
            trigger_source = "circuit_breaker"
        elif "loop" in reason_lower:
            trigger_source = "loop_detection"
        elif "drift" in reason_lower and "auto" in reason_lower:
            trigger_source = "drift_detection"
        else:
            trigger_source = "manual"

    # LLM-assisted dialectic: delegate to synthetic reviewer
    if reviewer_mode == "llm":
        llm_args = {
            "root_cause": reason,
            "proposed_conditions": arguments.get("proposed_conditions", []),
            "reasoning": arguments.get("reasoning", ""),
        }
        for key in ("agent_id", "client_session_id", "api_key", "session_type"):
            if key in arguments:
                llm_args[key] = arguments[key]
        return await handle_llm_assisted_dialectic(llm_args)

    # Capture paused agent state snapshot if available
    paused_agent_state = {}
    try:
        monitor = getattr(mcp_server, "monitors", {}).get(agent_uuid)
        if monitor and hasattr(monitor, "state") and hasattr(monitor.state, "to_dict"):
            paused_agent_state = monitor.state.to_dict()
    except Exception:
        paused_agent_state = {}

    # Reviewer selection
    auto_self_review = False
    if reviewer_mode == "self":
        reviewer_agent_id = agent_uuid
    elif reviewer_mode == "auto":
        # Auto-select a reviewer from eligible agents
        # Wave 2 audit: force=True dropped per PR #350 precedent. Reviewer
        # selection scans the in-memory fleet; cache is fresh enough.
        await mcp_server.load_metadata_async()
        try:
            reviewer_agent_id = await select_reviewer(
                paused_agent_id=agent_uuid,
                metadata=mcp_server.agent_metadata,
            )
        except Exception as e:
            logger.warning(f"Auto reviewer selection failed: {e}")
            reviewer_agent_id = None
        # Fall back to self-review if no eligible reviewer found
        if reviewer_agent_id is None:
            logger.info(
                "[DIALECTIC] No eligible reviewer found; falling back to self-review"
            )
            reviewer_agent_id = agent_uuid
            auto_self_review = True
    else:
        # Manual mode or unknown - no reviewer assigned
        # First responder can claim via submit_antithesis
        reviewer_agent_id = None

    # Create session
    session = DialecticSession(
        paused_agent_id=agent_uuid,
        reviewer_agent_id=reviewer_agent_id,
        paused_agent_state=paused_agent_state,
        discovery_id=discovery_id,
        dispute_type=dispute_type,
        session_type=session_type,
        topic=topic,
        max_synthesis_rounds=int(max_synthesis_rounds or 5),
        reason=reason,
        trigger_source=trigger_source,
    )

    # Persist to PostgreSQL (single source of truth)
    # JSON snapshots removed - use export_dialectic_session() for debugging
    try:
        await pg_create_session(
            session_id=session.session_id,
            paused_agent_id=session.paused_agent_id,
            reviewer_agent_id=session.reviewer_agent_id,
            reason=reason,
            discovery_id=discovery_id,
            dispute_type=dispute_type,
            session_type=session_type,
            topic=topic,
            max_synthesis_rounds=session.max_synthesis_rounds,
            synthesis_round=session.synthesis_round,
            paused_agent_state=paused_agent_state,
            trigger_source=trigger_source,
        )
        logger.info(f"Dialectic session {session.session_id} persisted to PostgreSQL")
    except Exception as e:
        logger.error(f"Dialectic session create FAILED: {e}")
        return [error_response(
            f"Failed to persist dialectic session: {e}",
            error_code="DB_WRITE_FAILED",
            error_category="system_error",
            arguments=arguments
        )]

    # Cache in-memory for quick access
    ACTIVE_SESSIONS[session.session_id] = session

    # Build response based on reviewer assignment
    if auto_self_review:
        note = "No eligible reviewer available — configured for self-review. Use submit_thesis, then submit_antithesis for your counter-perspective."
    elif session.reviewer_agent_id and session.reviewer_agent_id != agent_uuid:
        note = f"Reviewer assigned: {session.reviewer_agent_id[:12]}... Use submit_thesis to add your thesis."
    elif session.reviewer_agent_id:
        note = "Session created with self-review. Use submit_thesis to add your thesis."
    else:
        note = "Session created. Awaiting reviewer assignment. Operator should assign a reviewer, then paused agent submits thesis."

    return success_response({
        "success": True,
        "message": "Dialectic session created",
        "session_id": session.session_id,
        "paused_agent_id": session.paused_agent_id,
        "reviewer_agent_id": session.reviewer_agent_id,
        "awaiting_reviewer": session.reviewer_agent_id is None,
        "phase": session.phase.value,
        "session_type": session.session_type,
        "reason": session.reason,
        "trigger_source": session.trigger_source,
        "note": note
    })

@mcp_tool("get_dialectic_session", timeout=10.0, rate_limit_exempt=True, register=False)
async def handle_get_dialectic_session(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    View an active or past dialectic session.

    Args:
        session_id: Dialectic session ID (optional if agent_id provided)
        agent_id: Agent ID to find sessions for (optional if session_id provided)

    Returns:
        Session state including transcript and next-action guidance
    """
    try:
        session_id = arguments.get('session_id')
        agent_id = arguments.get('agent_id')
        check_timeout = arguments.get('check_timeout', False)

        # If session_id provided, use it directly
        if session_id:
            # Fast path: skip object reconstruction when timeout checks disabled (dashboard use case)
            if not check_timeout:
                fast_result = await load_session_as_dict(session_id)
                if fast_result:
                    fast_result["success"] = True
                    fast_result.update(_build_dialectic_actionability(fast_result))
                    return success_response(fast_result)

            # PG-first, fallback to in-memory cache
            session = await load_session(session_id)
            if session:
                ACTIVE_SESSIONS[session_id] = session
            else:
                session = ACTIVE_SESSIONS.get(session_id)
            
            if not session:
                return [error_response(f"Session '{session_id}' not found")]
            
            # Check for timeouts if requested
            if check_timeout:
                # Check if reviewer is stuck FIRST — try re-assignment before failing
                if await check_reviewer_stuck(session):
                    from .responses import get_reviewer_reassigned_recovery, get_awaiting_facilitation_recovery
                    old_reviewer = session.reviewer_agent_id

                    # Attempt auto re-assignment
                    exclude = [session.paused_agent_id]
                    if old_reviewer:
                        exclude.append(old_reviewer)
                    try:
                        new_reviewer = await select_reviewer(
                            paused_agent_id=session.paused_agent_id,
                            metadata=mcp_server.agent_metadata,
                            exclude_agent_ids=exclude,
                        )
                    except Exception:
                        new_reviewer = None

                    if new_reviewer:
                        try:
                            await _apply_reviewer_reassignment(
                                session_id,
                                session,
                                new_reviewer,
                                reason="Previous reviewer did not respond within the antithesis timeout.",
                            )
                        except Exception as e:
                            logger.error(f"Failed to persist reviewer reassignment: {e}")
                        logger.info(f"[DIALECTIC] Auto-reassigned reviewer for {session_id[:16]}: {old_reviewer} -> {new_reviewer}")
                        result = session.to_dict()
                        result["success"] = True
                        result["reviewer_reassigned"] = True
                        result["old_reviewer_id"] = old_reviewer
                        result["recovery"] = get_reviewer_reassigned_recovery(
                            old_reviewer,
                            new_reviewer,
                            reason="Previous reviewer did not respond within the antithesis timeout.",
                        )
                        result.update(_build_dialectic_actionability(result))
                        return success_response(result)

                    # No replacement found — mark awaiting facilitation (not FAILED yet)
                    if not getattr(session, 'awaiting_facilitation', False):
                        session.awaiting_facilitation = True
                        facilitation_msg = DialecticMessage(
                            phase=session.phase.value,
                            agent_id="system",
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            reasoning=f"Reviewer '{old_reviewer}' stuck. No auto-replacement available. Awaiting human facilitation.",
                        )
                        session.transcript.append(facilitation_msg)
                        try:
                            await pg_add_message(
                                session_id=session_id,
                                agent_id="system",
                                message_type="system",
                                reasoning=f"Reviewer '{old_reviewer}' stuck. Awaiting human facilitation.",
                            )
                        except Exception as e:
                            logger.error(f"Failed to persist facilitation status: {e}")
                        logger.info(f"[DIALECTIC] Session {session_id[:16]} awaiting human facilitation (reviewer {old_reviewer} stuck)")

                    return success_response({
                        "success": False,
                        "error": "Reviewer stuck - awaiting human facilitation",
                        "awaiting_facilitation": True,
                        "session": session.to_dict(),
                        "recovery": get_awaiting_facilitation_recovery(session_id),
                    })

                # General timeout check (non-reviewer timeouts: synthesis, total time)
                timeout_reason = session.check_timeout()
                if timeout_reason:
                    session.phase = DialecticPhase.FAILED
                    timeout_msg = DialecticMessage(
                        phase="synthesis",
                        agent_id="system",
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        reasoning=f"Session auto-failed: {timeout_reason}"
                    )
                    session.transcript.append(timeout_msg)
                    try:
                        await pg_add_message(
                            session_id=session_id,
                            agent_id="system",
                            message_type="failed",
                            reasoning=f"Session auto-failed: {timeout_reason}",
                        )
                        await pg_update_phase(session_id, "failed")
                    except Exception as e:
                        logger.error(f"Failed to persist timeout to PostgreSQL: {e}")
                    return success_response({
                        "success": False,
                        "error": timeout_reason,
                        "session": session.to_dict(),
                        "recovery": get_session_timeout_recovery(timeout_reason),
                    })

            result = session.to_dict()
            result["success"] = True
            result.update(_build_dialectic_actionability(result))
            return success_response(result)
        
        # If agent_id provided, find all sessions for this agent
        if agent_id:
            matching_sessions = []

            # Query PostgreSQL for all sessions involving this agent
            try:
                pg_sessions = await pg_get_all_sessions_by_agent(agent_id)
                for s in pg_sessions:
                    matching_sessions.append(s)
            except Exception as e:
                logger.warning(f"PG query for agent sessions failed: {e}")

            # Also check in-memory cache for sessions not yet persisted
            for sid, session in ACTIVE_SESSIONS.items():
                if session.paused_agent_id == agent_id or session.reviewer_agent_id == agent_id:
                    if not any(s.get('session_id') == sid for s in matching_sessions):
                        matching_sessions.append(session.to_dict())

            if not matching_sessions:
                return [error_response(
                    f"No dialectic sessions found for agent '{agent_id}'",
                    recovery=no_sessions_found_recovery(),
                )]

            # If single session, return it directly
            if len(matching_sessions) == 1:
                result = matching_sessions[0]
                result["success"] = True
                return success_response(result)

            # Multiple sessions - return list
            return success_response({
                "success": True,
                "agent_id": agent_id,
                "session_count": len(matching_sessions),
                "sessions": matching_sessions
            })
        
        # Neither provided
        return [error_response(
            "Either session_id or agent_id is required",
            recovery=missing_session_or_agent_recovery(),
        )]

    except Exception as e:
        import traceback
        # SECURITY: Log full traceback internally but sanitize for client
        logger.error(f"Error getting dialectic session: {e}", exc_info=True)
        return [error_response(
            f"Error getting session: {str(e)}",
            recovery=get_session_exception_recovery(),
        )]

@mcp_tool("list_dialectic_sessions", timeout=15.0, rate_limit_exempt=True, register=False)
async def handle_list_dialectic_sessions(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    List all dialectic sessions with optional filtering.

    Allows agents to browse active and past dialectic sessions. Returns
    summaries by default for efficiency.

    Args:
        agent_id: Filter by agent (either requestor or reviewer) - optional
        status: Filter by phase (e.g., 'resolved', 'failed', 'thesis') - optional
        limit: Max sessions to return (default 50, max 200)
        include_transcript: Include full transcript in results (default False)

    Returns:
        List of session summaries with optional full transcript
    """
    try:
        agent_id = arguments.get('agent_id')
        status = arguments.get('status')
        limit = min(int(arguments.get('limit', 50) or 50), 200)
        include_transcript = bool(arguments.get('include_transcript', False))

        sessions = await list_all_sessions(
            agent_id=agent_id,
            status=status,
            limit=limit,
            include_transcript=include_transcript
        )

        if not sessions:
            return success_response({
                "success": True,
                "message": "No dialectic sessions found matching criteria",
                "sessions": [],
                "filters_applied": {
                    "agent_id": agent_id,
                    "status": status,
                    "limit": limit
                },
                "tip": "Use list_dialectic_sessions() with no filters to see all sessions"
            })

        return success_response({
            "success": True,
            "session_count": len(sessions),
            "sessions": sessions,
            "filters_applied": {
                "agent_id": agent_id,
                "status": status,
                "limit": limit,
                "include_transcript": include_transcript
            },
            "tip": "Use get_dialectic_session(session_id='...') for full details"
        })

    except Exception as e:
        logger.error(f"Error listing dialectic sessions: {e}", exc_info=True)
        return [error_response(
            f"Error listing sessions: {str(e)}",
            recovery={
                "action": "Try with different filters or check server logs",
                "related_tools": ["get_dialectic_session", "health_check"]
            }
        )]

@mcp_tool("submit_thesis", timeout=10.0, register=True)
async def handle_submit_thesis(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Paused agent submits thesis: "What I did, what I think happened"

    Args:
        session_id: Dialectic session ID
        agent_id: Paused agent ID (or use bound identity)
        root_cause: Agent's understanding of what caused the issue
        proposed_conditions: List of conditions for resumption
        reasoning: Natural language explanation

    Returns:
        Status with next phase
    """
    try:
        session_id = arguments.get('session_id')
        api_key = arguments.get('api_key', '')

        if not session_id:
            return [error_response(
                "session_id is required",
                recovery=missing_session_id_recovery(),
            )]

        # Use same identity pipeline as onboard/identity (consistent UUID)
        agent_id, agent_error = await _resolve_dialectic_agent_id(
            arguments, enforce_session_ownership=True
        )
        if agent_error:
            return agent_error

        # Get session - PG-first, fallback to in-memory cache
        session = await load_session(session_id)
        if session:
            ACTIVE_SESSIONS[session_id] = session
        else:
            session = ACTIVE_SESSIONS.get(session_id)
            if not session:
                return [error_response(
                    f"Session '{session_id}' not found",
                    recovery=session_not_found_recovery(),
                )]

        # Resolve proposed_conditions, accepting `conditions` as an alias
        # to prevent silent data loss from parameter-name confusion.
        proposed_conditions = _read_proposed_conditions(arguments)

        # Create thesis message
        message = DialecticMessage(
            phase="thesis",
            agent_id=agent_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            root_cause=arguments.get('root_cause'),
            proposed_conditions=proposed_conditions,
            reasoning=arguments.get('reasoning')
        )

        # Submit to session
        result = session.submit_thesis(message, api_key)

        if result["success"]:
            result["next_step"] = next_step_submit_antithesis(session.reviewer_agent_id)

            # Persist to PostgreSQL
            try:
                await pg_add_message(
                    session_id=session_id,
                    agent_id=agent_id,
                    message_type="thesis",
                    root_cause=arguments.get('root_cause'),
                    proposed_conditions=proposed_conditions,
                    reasoning=arguments.get('reasoning'),
                )
                await pg_update_phase(session_id, session.phase.value)
            except Exception as e:
                logger.warning(f"Could not update PostgreSQL after thesis: {e}")

            # Persist to JSON (export snapshot)
            try:
                await save_session(session)
            except Exception as e:
                logger.warning(f"Could not save session after thesis: {e}")

        return success_response(result)

    except Exception as e:
        return [error_response(f"Error submitting thesis: {str(e)}")]

@mcp_tool("submit_antithesis", timeout=10.0, register=True)
async def handle_submit_antithesis(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Reviewer agent submits antithesis: "What I observe, my concerns"

    Args:
        session_id: Dialectic session ID
        agent_id: Reviewer agent ID (or use bound identity)
        observed_metrics: Metrics observed about paused agent
        concerns: List of concerns
        reasoning: Natural language explanation

    Returns:
        Status with next phase
    """
    try:
        session_id = arguments.get('session_id')
        api_key = arguments.get('api_key', '')
        take_over_if_requested = coerce_bool(arguments.get("take_over_if_requested"), default=False)
        takeover_reason = arguments.get("takeover_reason") or (
            "Operator-requested reviewer takeover during antithesis submission"
        )

        if not session_id:
            return [error_response(
                "session_id is required",
                recovery=missing_session_id_recovery(),
            )]

        # Use same identity pipeline as onboard/identity (consistent UUID)
        agent_id, agent_error = await _resolve_dialectic_agent_id(
            arguments, enforce_session_ownership=True
        )
        if agent_error:
            return agent_error

        # Get session - PG-first, fallback to in-memory cache
        session = await load_session(session_id)
        if session:
            ACTIVE_SESSIONS[session_id] = session
        else:
            session = ACTIVE_SESSIONS.get(session_id)
            if not session:
                return [error_response(
                    f"Session '{session_id}' not found",
                    recovery=session_not_found_recovery(),
                )]

        original_reviewer_id = session.reviewer_agent_id
        reviewer_takeover = None

        if session.reviewer_agent_id and agent_id != session.reviewer_agent_id:
            if not take_over_if_requested:
                workflow = (
                    "Wait for the assigned reviewer to answer, or use "
                    "dialectic(action='reassign', session_id='...', new_reviewer_id='...')."
                )
                if agent_id != session.paused_agent_id:
                    workflow = (
                        "Retry with take_over_if_requested=true from your bound session, "
                        "or use dialectic(action='reassign', session_id='...', new_reviewer_id='...')."
                    )
                return [error_response(
                    "Only the assigned reviewer can submit antithesis for this session.",
                    recovery={
                        "action": "Use the assigned reviewer or explicitly take over reviewer ownership",
                        "assigned_reviewer_id": session.reviewer_agent_id,
                        "assigned_reviewer_label": _agent_label(session.reviewer_agent_id),
                        "related_tools": ["get_dialectic_session", "dialectic", "identity"],
                        "workflow": workflow,
                    },
                )]

            validation_error = await _validate_explicit_reviewer_candidate(session, agent_id)
            if validation_error:
                return validation_error

            try:
                reviewer_takeover = await _apply_reviewer_reassignment(
                    session_id,
                    session,
                    agent_id,
                    reason=takeover_reason,
                    strict_persistence=True,
                )
            except Exception as e:
                return [error_response(f"Reviewer takeover failed during persistence: {e}")]

        # First-responder eligibility: if no reviewer assigned, validate the
        # submitter before the protocol auto-assigns them as reviewer.
        # Without this, any agent could claim the reviewer slot with no checks.
        if session.reviewer_agent_id is None and agent_id != session.paused_agent_id:
            from .reviewer import _has_recently_reviewed, is_agent_in_active_session
            try:
                if await is_agent_in_active_session(agent_id):
                    return [error_response(
                        "Cannot become reviewer: already in an active dialectic session",
                        recovery={"action": "Wait for your current session to complete"}
                    )]
                if await _has_recently_reviewed(agent_id, session.paused_agent_id):
                    return [error_response(
                        "Cannot become reviewer: recently reviewed this agent (24h cooldown)",
                        recovery={"action": "A different agent should review this session"}
                    )]
            except Exception as e:
                logger.warning(f"First-responder eligibility check failed (proceeding): {e}")

        # Create antithesis message
        message = DialecticMessage(
            phase="antithesis",
            agent_id=agent_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            observed_metrics=arguments.get('observed_metrics', {}),
            concerns=arguments.get('concerns', []),
            reasoning=arguments.get('reasoning')
        )

        # Submit to session
        result = session.submit_antithesis(message, api_key)

        if result["success"]:
            result["next_step"] = next_step_negotiate_synthesis()

            # If reviewer was auto-assigned (first-responder pattern), persist to PG
            if original_reviewer_id is None and session.reviewer_agent_id == agent_id:
                try:
                    await pg_update_reviewer(session_id, agent_id)
                    result["reviewer_auto_assigned"] = True
                    logger.info("Reviewer auto-assigned for dialectic session")
                except Exception as e:
                    logger.warning(
                        "Could not persist reviewer assignment to PostgreSQL: %s",
                        type(e).__name__,
                    )
            elif reviewer_takeover:
                result["reviewer_takeover"] = reviewer_takeover

            # Persist to PostgreSQL
            try:
                await pg_add_message(
                    session_id=session_id,
                    agent_id=agent_id,
                    message_type="antithesis",
                    observed_metrics=arguments.get('observed_metrics', {}),
                    concerns=arguments.get('concerns', []),
                    reasoning=arguments.get('reasoning'),
                )
                await pg_update_phase(session_id, session.phase.value)
            except Exception as e:
                logger.warning(f"Could not update PostgreSQL after antithesis: {e}")

            # Persist to JSON
            try:
                await save_session(session)
            except Exception as e:
                logger.warning(f"Could not save session after antithesis: {e}")

        return success_response(result)

    except Exception as e:
        return [error_response(f"Error submitting antithesis: {str(e)}")]

@mcp_tool("submit_synthesis", timeout=15.0, register=True)
async def handle_submit_synthesis(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Either agent submits synthesis proposal during negotiation.

    Args:
        session_id: Dialectic session ID
        agent_id: Agent ID (either paused or reviewer)
        proposed_conditions: Proposed resumption conditions
        root_cause: Agreed understanding of root cause
        reasoning: Explanation of proposal
        agrees: Whether this agent agrees with current proposal (bool)

    Returns:
        Status with convergence info and resolution if converged
    """
    try:
        session_id = arguments.get('session_id')
        api_key = arguments.get('api_key', '')

        if not session_id:
            return [error_response(
                "session_id is required",
                recovery=missing_session_id_recovery(),
            )]

        # Use same identity pipeline as onboard/identity (consistent UUID)
        # Supports third-party synthesizer when agent_id is explicitly provided
        agent_id, agent_error = await _resolve_dialectic_agent_id(arguments)
        if agent_error:
            return agent_error

        # Always reload from disk to get latest state
        session = await load_session(session_id)
        if session:
            ACTIVE_SESSIONS[session_id] = session
        else:
            session = ACTIVE_SESSIONS.get(session_id)
            if not session:
                return [error_response(
                    f"Session '{session_id}' not found",
                    recovery=session_not_found_recovery(),
                )]

        # Participant-set eligibility gate. The sibling handlers
        # (submit_thesis / submit_antithesis) pass enforce_session_ownership=True
        # to _resolve_dialectic_agent_id; submit_synthesis intentionally relaxes
        # that check to support the "third-party synthesizer" pattern. Without
        # a compensating allow-list, any registered agent could drive a
        # synthesis to convergence and trigger resolution execution — a real
        # privilege escalation surface. The allow-list is: the paused agent,
        # the assigned reviewer, and any quorum reviewer (if escalated).
        eligible = set()
        if getattr(session, "paused_agent_id", None):
            eligible.add(session.paused_agent_id)
        if getattr(session, "reviewer_agent_id", None):
            eligible.add(session.reviewer_agent_id)
        eligible.update(getattr(session, "quorum_reviewer_ids", []) or [])
        agent_uuid = resolve_agent_uuid(arguments, agent_id)
        if agent_id not in eligible and (not agent_uuid or agent_uuid not in eligible):
            return [error_response(
                f"Agent '{agent_id}' is not a participant in this dialectic session.",
                recovery=(
                    "Only the paused agent, the assigned reviewer, or assigned "
                    "quorum members may submit synthesis."
                ),
            )]

        # Coerce agrees to bool (MCP tools may send "true"/"false" strings)
        raw_agrees = arguments.get('agrees', False)
        if isinstance(raw_agrees, str):
            agrees = raw_agrees.lower() in ('true', '1', 'yes')
        else:
            agrees = bool(raw_agrees)

        # Resolve proposed_conditions, accepting `conditions` as an alias
        # to prevent silent data loss from parameter-name confusion.
        proposed_conditions = _read_proposed_conditions(arguments)

        # Early-fail: synthesis with agrees=True must contribute conditions, OR
        # there must already be a prior synthesis in transcript that did.
        # Without this gate, the empty-conditions path slips through to
        # check_hard_limits at finalize time and leaves the session in a
        # phase=failed / resolution.action=resume inconsistent terminal state.
        if agrees and not proposed_conditions:
            prior_has_conditions = any(
                getattr(msg, "phase", None) == "synthesis"
                and getattr(msg, "proposed_conditions", None)
                for msg in (session.transcript or [])
            )
            if not prior_has_conditions:
                return [error_response(
                    "Cannot agree (agrees=True) with empty proposed_conditions when no "
                    "prior synthesis has supplied any. Either pass proposed_conditions=[...] "
                    "(or its alias `conditions=[...]`) populated with the agreed terms, "
                    "or set agrees=False to register disagreement instead.",
                    error_code="EMPTY_AGREEMENT",
                    error_category="validation_error",
                    recovery={
                        "action": (
                            "Re-submit synthesis with proposed_conditions populated. "
                            "If you intended to disagree, set agrees=false."
                        ),
                        "related_tools": ["dialectic"],
                    },
                    arguments=arguments,
                )]

        # Create synthesis message
        message = DialecticMessage(
            phase="synthesis",
            agent_id=agent_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            proposed_conditions=proposed_conditions,
            root_cause=arguments.get('root_cause'),
            reasoning=arguments.get('reasoning'),
            agrees=agrees
        )

        # Submit to session
        result = session.submit_synthesis(message, api_key)

        if result.get("success"):
            # Persist synthesis message to PostgreSQL
            # NOTE: Defer phase update for converged sessions until after finalize_resolution
            # succeeds, to avoid "resolved" phase in PG without a resolution if finalize fails.
            try:
                await pg_add_message(
                    session_id=session_id,
                    agent_id=agent_id,
                    message_type="synthesis",
                    root_cause=arguments.get('root_cause'),
                    proposed_conditions=proposed_conditions,
                    reasoning=arguments.get('reasoning'),
                    agrees=agrees,
                )
                if not result.get("converged"):
                    await pg_update_phase(session_id, session.phase.value)
            except Exception as e:
                logger.warning(f"Could not update PostgreSQL after synthesis: {e}")

            # Persist to JSON
            try:
                await save_session(session)
            except Exception as e:
                logger.warning(f"Could not save session after synthesis: {e}")

        # If converged, finalize resolution
        if result.get("success") and result.get("converged"):
            # Generate signatures
            paused_meta = mcp_server.agent_metadata.get(session.paused_agent_id)
            reviewer_meta = mcp_server.agent_metadata.get(session.reviewer_agent_id)

            api_key_a = paused_meta.api_key if paused_meta and paused_meta.api_key else api_key
            api_key_b = reviewer_meta.api_key if reviewer_meta and reviewer_meta.api_key else ""

            synthesis_messages = [msg for msg in session.transcript if msg.phase == "synthesis" and msg.agrees]
            if synthesis_messages:
                last_msg = synthesis_messages[-1]
                signature_a = last_msg.sign(api_key_a) if api_key_a else ""
                signature_b = last_msg.sign(api_key_b) if api_key_b else ""
            else:
                import hashlib
                session_data = f"{session.session_id}:{api_key_a}"
                signature_a = hashlib.sha256(session_data.encode()).hexdigest()[:32]
                session_data = f"{session.session_id}:{api_key_b}"
                signature_b = hashlib.sha256(session_data.encode()).hexdigest()[:32] if api_key_b else ""

            resolution = session.finalize_resolution(signature_a, signature_b)
            is_safe, violation = session.check_hard_limits(resolution)

            if not is_safe:
                # Safety violation: mark session as FAILED (not RESOLVED)
                session.phase = DialecticPhase.FAILED
                result["action"] = "block"
                result["success"] = False
                result["reason"] = f"Safety violation: {violation}"
                try:
                    await pg_resolve_session(session_id=session_id, resolution={"action": "block", "reason": violation}, status="failed")
                except Exception as e:
                    logger.warning(f"Could not resolve session in PostgreSQL: {e}")
            else:
                result["action"] = "resume"
                result["resolution"] = resolution.to_dict()

                try:
                    execution_result = await execute_resolution(session, resolution)
                    result["execution"] = execution_result
                    if execution_result.get("success"):
                        result["next_step"] = next_step_resumed()
                    else:
                        result["next_step"] = next_step_resume_not_applied(
                            execution_result.get("warning")
                        )

                    # Execution succeeded - mark resolved in PG
                    try:
                        await pg_resolve_session(session_id=session_id, resolution=resolution.to_dict(), status="resolved")
                    except Exception as e:
                        logger.warning(f"Could not resolve session in PostgreSQL: {e}")
                except Exception as e:
                    # Execution failed - mark FAILED, not resolved
                    session.phase = DialecticPhase.FAILED
                    result["success"] = False
                    result["execution_error"] = str(e)
                    result["next_step"] = next_step_execution_failed(e)
                    try:
                        await pg_resolve_session(session_id=session_id, resolution=resolution.to_dict(), status="failed")
                    except Exception as pg_e:
                        logger.warning(f"Could not mark failed session in PostgreSQL: {pg_e}")

            # Update calibration from dialectic outcome
            try:
                if result.get("action") == "resume":
                    await update_calibration_from_dialectic(session)
                elif not is_safe:
                    await update_calibration_from_dialectic_disagreement(session)
            except Exception as cal_e:
                logger.debug(f"Calibration update after dialectic: {cal_e}")

            # Always invalidate cache after convergence (success or failure)
            for aid in (session.paused_agent_id, session.reviewer_agent_id):
                if aid and aid in _SESSION_METADATA_CACHE:
                    del _SESSION_METADATA_CACHE[aid]

            await save_session(session)

        elif not result.get("success"):
            # Max rounds exceeded — try quorum escalation before conservative default
            quorum_info = await _initiate_quorum(session)
            if quorum_info:
                result["autonomous_resolution"] = False
                result["resolution_type"] = "quorum_escalation"
                result["quorum"] = quorum_info
                result["next_step"] = next_step_quorum_initiated(len(quorum_info["reviewer_ids"]))
                result["next_steps"] = default_quorum_steps()
            else:
                # Not enough agents for quorum — fall back to conservative default
                result["autonomous_resolution"] = True
                result["resolution_type"] = "conservative_default"
                result["next_step"] = next_step_no_consensus()
                result["cooldown_until"] = (datetime.now() + timedelta(hours=1)).isoformat()

        return success_response(result)

    except Exception as e:
        return [error_response(f"Error submitting synthesis: {str(e)}")]


@mcp_tool("reassign_reviewer", timeout=15.0, register=True)
async def handle_reassign_reviewer(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Reassign the reviewer for an active dialectic session.

    Use when the current reviewer is unresponsive (ephemeral session ended)
    or you need to manually assign a specific reviewer.

    Args:
        session_id: Dialectic session ID
        new_reviewer_id: Agent ID to assign as new reviewer (optional — auto-selects if omitted)
        reason: Why the reviewer is being reassigned (optional)

    Returns:
        Updated session state with new reviewer info
    """
    from .responses import get_reviewer_reassigned_recovery, get_awaiting_facilitation_recovery

    session_id = arguments.get("session_id")
    new_reviewer_id = arguments.get("new_reviewer_id")
    reason = arguments.get("reason", "Reviewer unresponsive")

    if not session_id:
        return [error_response(
            "session_id is required",
            error_code="MISSING_PARAM",
            error_category="validation_error",
            recovery=missing_session_id_recovery(),
            arguments=arguments,
        )]

    # Load session
    session = ACTIVE_SESSIONS.get(session_id)
    if not session:
        session = await load_session(session_id)
        if session:
            ACTIVE_SESSIONS[session_id] = session

    if not session:
        return [error_response(
            f"Session '{session_id}' not found",
            recovery=session_not_found_recovery(),
        )]

    # Validate phase — only reassign during THESIS or ANTITHESIS
    if session.phase not in (DialecticPhase.THESIS, DialecticPhase.ANTITHESIS):
        return [error_response(
            f"Cannot reassign reviewer in phase '{session.phase.value}'. Only THESIS or ANTITHESIS phases allow reassignment.",
            error_code="INVALID_PHASE",
            error_category="validation_error",
        )]

    old_reviewer_id = session.reviewer_agent_id

    if new_reviewer_id:
        # Validate the new reviewer exists and is eligible
        validation_error = await _validate_explicit_reviewer_candidate(session, new_reviewer_id)
        if validation_error:
            return validation_error
    else:
        # Auto-select a replacement
        # Wave 2 audit: force=True dropped per PR #350 precedent. Reviewer
        # selection scans the in-memory fleet; cache is fresh enough.
        await mcp_server.load_metadata_async()
        exclude = [session.paused_agent_id]
        if old_reviewer_id:
            exclude.append(old_reviewer_id)
        try:
            new_reviewer_id = await select_reviewer(
                paused_agent_id=session.paused_agent_id,
                metadata=mcp_server.agent_metadata,
                exclude_agent_ids=exclude,
            )
        except Exception as e:
            logger.warning(f"Auto reviewer re-selection failed: {e}")
            new_reviewer_id = None

        if not new_reviewer_id:
            return [error_response(
                "No eligible reviewer found for auto-assignment. Provide new_reviewer_id manually.",
                error_code="NO_REVIEWER",
                error_category="no_candidates",
                recovery=get_awaiting_facilitation_recovery(session_id),
            )]

    try:
        await _apply_reviewer_reassignment(
            session_id,
            session,
            new_reviewer_id,
            reason=reason,
            strict_persistence=True,
        )
    except Exception as e:
        logger.error(f"Failed to persist reviewer reassignment: {e}")
        return [error_response(f"Reassignment failed during persistence: {e}")]

    logger.info(
        f"[DIALECTIC] Reviewer reassigned for session {session_id[:16]}: "
        f"{old_reviewer_id} -> {new_reviewer_id} (reason: {reason})"
    )

    return success_response({
        "success": True,
        "message": "Reviewer reassigned",
        "session_id": session_id,
        "old_reviewer_id": old_reviewer_id,
        "new_reviewer_id": new_reviewer_id,
        "phase": session.phase.value,
        "reason": reason,
        "recovery": get_reviewer_reassigned_recovery(
            old_reviewer_id,
            new_reviewer_id,
            reason=reason,
        ),
    })


async def _initiate_quorum(session: DialecticSession) -> Optional[Dict[str, Any]]:
    """
    Attempt to initiate quorum voting for an escalated dialectic session.

    Selects 3-5 reviewers, sets session phase to QUORUM_VOTING,
    and persists quorum data to PostgreSQL.

    Returns:
        Dict with reviewer_ids, scores, and deadline if quorum formed.
        None if fewer than 3 eligible reviewers.
    """
    # Wave 2 audit: force=True dropped per PR #350 precedent. Quorum
    # selection scans the in-memory fleet; cache is fresh enough.
    await mcp_server.load_metadata_async()
    metadata = mcp_server.agent_metadata or {}

    reviewers = await select_quorum_reviewers(session, metadata)
    if not reviewers:
        return None

    reviewer_ids = [r[0] for r in reviewers]
    reviewer_scores = {r[0]: r[1] for r in reviewers}
    deadline = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

    session.phase = DialecticPhase.QUORUM_VOTING
    session.quorum_reviewer_ids = reviewer_ids
    session.quorum_deadline = deadline

    # Persist to PostgreSQL. Wrapped with asyncio.wait_for because a direct
    # ``await get_db()`` in an MCP handler path can deadlock under the
    # anyio-asyncio conflict documented in CLAUDE.md. On timeout we fall
    # through to the in-memory session state (already mutated above) and
    # the subsequent ``save_session`` JSON write, matching the existing
    # degrade-on-failure behavior.
    try:
        from src.db import get_db
        db = await asyncio.wait_for(get_db(), timeout=0.5)
        await asyncio.wait_for(
            db.execute(
                """UPDATE core.dialectic_sessions
                   SET phase = 'quorum_voting',
                       status = 'quorum_voting',
                       quorum_reviewer_ids = $1::jsonb,
                       quorum_deadline = $2::timestamptz,
                       updated_at = NOW()
                 WHERE session_id = $3""",
                json.dumps(reviewer_ids),
                deadline,
                session.session_id,
            ),
            timeout=1.0,
        )
    except (asyncio.TimeoutError, Exception) as e:
        logger.warning(f"Could not persist quorum to PostgreSQL: {e}")

    try:
        await save_session(session)
    except Exception as e:
        logger.warning(f"Could not save quorum session to JSON: {e}")

    return {
        "reviewer_ids": reviewer_ids,
        "reviewer_scores": reviewer_scores,
        "deadline": deadline,
        "session_id": session.session_id,
    }


@mcp_tool("submit_quorum_vote", register=True)
async def handle_submit_quorum_vote(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Submit a vote in a quorum-escalated dialectic session.

    Only agents assigned to the quorum may vote. Voting closes when all
    assigned reviewers have voted or when 3+ votes are received.
    A 2/3 authority-weighted supermajority decides the outcome.

    Args:
        session_id: Dialectic session ID
        vote: "resume", "block", or "cooldown"
        reasoning: Explanation for your vote
        conditions: Optional conditions (for resume votes)
    """
    try:
        # Require registered agent
        agent_id, error = require_registered_agent(arguments)
        if error:
            return [error]

        session_id = arguments.get("session_id")
        if not session_id:
            return [error_response(
                "session_id is required",
                recovery=missing_session_id_recovery(),
            )]

        vote_value = arguments.get("vote")
        if vote_value not in ("resume", "block", "cooldown"):
            return [error_response("vote must be 'resume', 'block', or 'cooldown'")]

        reasoning = arguments.get("reasoning", "")
        conditions = arguments.get("conditions")

        # Load session
        session = await load_session(session_id)
        if not session:
            return [error_response(
                f"Session '{session_id}' not found",
                recovery=session_not_found_recovery(),
            )]

        # Verify phase
        if session.phase != DialecticPhase.QUORUM_VOTING:
            return [error_response(
                f"Session is in phase '{session.phase.value}', not 'quorum_voting'"
            )]

        # Read quorum reviewer IDs from session (set by _initiate_quorum)
        quorum_reviewer_ids = getattr(session, 'quorum_reviewer_ids', []) or []

        # PG fallback for sessions reconstructed before quorum_reviewer_ids was added.
        # See _initiate_quorum above for why asyncio.wait_for is required here.
        if not quorum_reviewer_ids:
            try:
                from src.db import get_db
                db = await asyncio.wait_for(get_db(), timeout=0.5)
                row = await asyncio.wait_for(
                    db.fetchrow(
                        "SELECT quorum_reviewer_ids FROM core.dialectic_sessions WHERE session_id = $1",
                        session_id,
                    ),
                    timeout=1.0,
                )
                if row and row["quorum_reviewer_ids"]:
                    qr = row["quorum_reviewer_ids"]
                    quorum_reviewer_ids = json.loads(qr) if isinstance(qr, str) else qr
                    session.quorum_reviewer_ids = quorum_reviewer_ids  # Cache on session
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"Could not load quorum_reviewer_ids from PG: {e}")

        if not quorum_reviewer_ids:
            return [error_response("No quorum reviewers assigned to this session")]

        # Verify agent is in the quorum
        agent_uuid = resolve_agent_uuid(arguments, agent_id)
        if agent_uuid not in quorum_reviewer_ids and agent_id not in quorum_reviewer_ids:
            return [error_response(
                f"Agent '{agent_id}' is not in the quorum for this session"
            )]

        # Check for duplicate vote
        existing_votes = [
            msg for msg in session.transcript
            if msg.phase == "quorum_vote" and msg.agent_id in (agent_id, agent_uuid)
        ]
        if existing_votes:
            return [error_response(f"Agent '{agent_id}' has already voted in this session")]

        # Get authority weight for this voter
        # Wave 2 audit: force=True dropped per PR #350 precedent. Single-agent
        # weight read; in-memory cache is fresh enough.
        await mcp_server.load_metadata_async()
        voter_meta = mcp_server.agent_metadata.get(agent_uuid) or mcp_server.agent_metadata.get(agent_id)
        meta_dict = {}
        if voter_meta and not isinstance(voter_meta, str):
            for attr in ('tags', 'total_reviews', 'successful_reviews', 'last_update'):
                val = getattr(voter_meta, attr, None) if not isinstance(voter_meta, dict) else voter_meta.get(attr)
                if val is not None:
                    meta_dict[attr] = val
        try:
            from src.dialectic_protocol import calculate_authority_score
            authority_weight = calculate_authority_score(meta_dict)
        except Exception:
            authority_weight = 0.5

        # Store vote as DialecticMessage
        vote_msg = DialecticMessage(
            phase="quorum_vote",
            agent_id=agent_uuid or agent_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            reasoning=reasoning,
            proposed_conditions=conditions,
            observed_metrics={"authority_weight": authority_weight, "vote": vote_value},
        )
        session.transcript.append(vote_msg)

        # Persist message to PostgreSQL
        try:
            await pg_add_message(
                session_id=session_id,
                agent_id=agent_uuid or agent_id,
                message_type="quorum_vote",
                reasoning=reasoning,
                proposed_conditions=conditions or [],
                observed_metrics={"authority_weight": authority_weight, "vote": vote_value},
            )
        except Exception as e:
            logger.warning(f"Could not persist quorum vote to PG: {e}")

        await save_session(session)

        # Collect all quorum votes from transcript
        all_vote_msgs = [
            msg for msg in session.transcript
            if msg.phase == "quorum_vote"
        ]
        vote_count = len(all_vote_msgs)

        # Tally when: all reviewers voted OR 3+ votes received
        should_tally = (vote_count >= len(quorum_reviewer_ids)) or (vote_count >= 3)

        result: Dict[str, Any] = {
            "success": True,
            "session_id": session_id,
            "vote_recorded": True,
            "votes_received": vote_count,
            "votes_needed": len(quorum_reviewer_ids),
        }

        if not should_tally:
            result["status"] = "awaiting_more_votes"
            result["next_step"] = f"Waiting for more votes ({vote_count}/{len(quorum_reviewer_ids)})"
            return success_response(result)

        # Build QuorumVote objects and tally
        quorum_votes = []
        for msg in all_vote_msgs:
            metrics = msg.observed_metrics or {}
            quorum_votes.append(QuorumVote(
                agent_id=msg.agent_id,
                vote=metrics.get("vote", "cooldown"),
                authority_weight=metrics.get("authority_weight", 0.5),
                reasoning=msg.reasoning or "",
                conditions=msg.proposed_conditions,
                timestamp=msg.timestamp,
            ))

        tally = tally_quorum_votes(quorum_votes)
        result["quorum_result"] = tally.to_dict()

        # Persist quorum result. See _initiate_quorum above for the rationale
        # behind the asyncio.wait_for deadlock guard.
        try:
            from src.db import get_db
            db = await asyncio.wait_for(get_db(), timeout=0.5)
            await asyncio.wait_for(
                db.execute(
                    """UPDATE core.dialectic_sessions
                       SET quorum_result = $1::jsonb, updated_at = NOW()
                     WHERE session_id = $2""",
                    json.dumps(tally.to_dict()),
                    session_id,
                ),
                timeout=1.0,
            )
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(f"Could not persist quorum_result to PG: {e}")

        if tally.achieved_supermajority and tally.action == "resume":
            # Merge conditions from resume voters
            merged_conditions = []
            for qv in quorum_votes:
                if qv.vote == "resume" and qv.conditions:
                    for c in qv.conditions:
                        if c not in merged_conditions:
                            merged_conditions.append(c)
            if not merged_conditions:
                merged_conditions = ["Monitor coherence for 24h"]

            # Build resolution and execute
            resolution = Resolution(
                action="resume",
                conditions=merged_conditions,
                root_cause=f"Quorum vote: {tally.vote_counts}",
                reasoning=f"Quorum supermajority ({tally.margin:.0%}) voted to resume",
                signature_a="quorum",
                signature_b="quorum",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            session.resolution = resolution
            session.phase = DialecticPhase.RESOLVED

            try:
                execution_result = await execute_resolution(session, resolution)
                result["execution"] = execution_result
                result["next_step"] = next_step_resumed()
                result["next_steps"] = default_resume_steps()
            except Exception as e:
                result["execution_error"] = str(e)
                result["next_step"] = next_step_execution_failed(e)

            try:
                await pg_resolve_session(
                    session_id=session_id,
                    resolution=resolution.to_dict(),
                    status="resolved",
                )
            except Exception as e:
                logger.warning(f"Could not mark quorum-resolved session in PG: {e}")

        elif tally.achieved_supermajority and tally.action == "block":
            session.phase = DialecticPhase.FAILED
            result["next_step"] = "Quorum voted to block. Agent remains paused."
            try:
                await pg_resolve_session(
                    session_id=session_id,
                    resolution={"action": "block", "quorum_result": tally.to_dict()},
                    status="failed",
                )
            except Exception as e:
                logger.warning(f"Could not mark quorum-blocked session in PG: {e}")

        else:
            # No supermajority (including cooldown majority) — conservative default
            session.phase = DialecticPhase.ESCALATED
            result["next_step"] = next_step_no_consensus()
            result["cooldown_until"] = (datetime.now() + timedelta(hours=1)).isoformat()
            try:
                await pg_resolve_session(
                    session_id=session_id,
                    resolution={"action": "cooldown", "quorum_result": tally.to_dict()},
                    status="escalated",
                )
            except Exception as e:
                logger.warning(f"Could not mark quorum-cooldown session in PG: {e}")

        # Invalidate cache
        for aid in (session.paused_agent_id, session.reviewer_agent_id):
            if aid and aid in _SESSION_METADATA_CACHE:
                del _SESSION_METADATA_CACHE[aid]

        await save_session(session)
        return success_response(result)

    except Exception as e:
        return [error_response(f"Error submitting quorum vote: {str(e)}")]


@mcp_tool("llm_assisted_dialectic", timeout=45.0, register=False)
async def handle_llm_assisted_dialectic(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Run LLM-assisted dialectic recovery when no peer reviewer is available.

    This tool enables single-agent dialectic recovery by using a local LLM
    as a "synthetic reviewer". It runs the full thesis -> antithesis -> synthesis
    protocol, generating counterarguments and synthesizing a resolution.

    Use this when:
    - Agent is stuck/paused and needs recovery
    - No peer reviewer is available or responding
    - You want structured reflection on what went wrong

    Args:
        root_cause: Your understanding of what caused the issue
        proposed_conditions: List of conditions you propose for recovery
        reasoning: Your explanation/reasoning (optional)

    Returns:
        Complete dialectic result with antithesis, synthesis, and recommendation
    """
    # Import LLM delegation functions
    from ..support.llm_delegation import run_full_dialectic, is_llm_available

    # Require registered agent
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]

    agent_uuid = resolve_agent_uuid(arguments, agent_id)

    # Check LLM availability
    if not await is_llm_available():
        return [error_response(
            "Local LLM (Ollama) not available for dialectic review",
            error_code="LLM_UNAVAILABLE",
            error_category="system_error",
            recovery=llm_unavailable_recovery(),
        )]

    # Get thesis components from arguments
    root_cause = arguments.get("root_cause")
    proposed_conditions = arguments.get("proposed_conditions", [])
    reasoning = arguments.get("reasoning", "")

    if not root_cause:
        return [error_response(
            "root_cause is required - explain what you think caused the issue",
            error_code="MISSING_ARGUMENT",
            error_category="validation_error",
            recovery=llm_missing_root_cause_recovery(),
        )]

    # Ensure proposed_conditions is a list
    if isinstance(proposed_conditions, str):
        proposed_conditions = [proposed_conditions]

    # Build thesis
    thesis = {
        "root_cause": root_cause,
        "proposed_conditions": proposed_conditions,
        "reasoning": reasoning,
        "agent_id": agent_uuid,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    # Get agent state for context
    agent_state = None
    try:
        monitor = getattr(mcp_server, "monitors", {}).get(agent_uuid)
        if monitor:
            agent_state = {
                "risk_score": getattr(monitor, "risk_score", None),
                "coherence": getattr(monitor.state, "coherence", None) if hasattr(monitor, "state") else None,
                "E": getattr(monitor.state, "E", None) if hasattr(monitor, "state") else None,
                "I": getattr(monitor.state, "I", None) if hasattr(monitor, "state") else None,
                "S": getattr(monitor.state, "S", None) if hasattr(monitor, "state") else None,
                "V": getattr(monitor.state, "V", None) if hasattr(monitor, "state") else None,
            }
    except Exception as e:
        logger.debug(f"Could not get agent state: {e}")

    # Run full dialectic
    logger.info("Running LLM-assisted dialectic")
    result = await run_full_dialectic(
        thesis=thesis,
        agent_state=agent_state,
        max_synthesis_rounds=2
    )

    if not result:
        return [error_response(
            "Dialectic process failed - LLM did not respond",
            error_code="DIALECTIC_FAILED",
            error_category="system_error",
            recovery=llm_failed_recovery(),
        )]

    if not result.get("success"):
        return [error_response(
            f"Dialectic incomplete: {result.get('error', 'Unknown error')}",
            error_code="DIALECTIC_INCOMPLETE",
            error_category="system_error",
            recovery=llm_incomplete_recovery(result),
        )]

    # Format successful response
    recommendation = result.get("recommendation", "ESCALATE")
    synthesis = result.get("synthesis", {})
    antithesis_data = result.get("antithesis", {})

    # Persist as a proper dialectic session via the DialecticSession protocol
    session_id = None
    try:
        from src.dialectic_protocol import DialecticSession, DialecticMessage as DMsg, Resolution
        session = DialecticSession(
            paused_agent_id=agent_uuid,
            reviewer_agent_id="llm-synthetic-reviewer",
            session_type=arguments.get("session_type", "review"),
            topic=root_cause[:200],
            max_synthesis_rounds=2,
            reason=root_cause,
        )
        session_id = session.session_id

        await pg_create_session(
            session_id=session_id,
            paused_agent_id=agent_uuid,
            reviewer_agent_id="llm-synthetic-reviewer",
            reason=root_cause,
            session_type=arguments.get("session_type", "review"),
            topic=root_cause[:200],
            max_synthesis_rounds=2,
            synthesis_round=0,
        )

        now = datetime.now(timezone.utc).isoformat()

        # 1. Submit thesis through protocol
        thesis_msg = DMsg(
            phase="thesis",
            agent_id=agent_uuid,
            timestamp=now,
            root_cause=root_cause,
            proposed_conditions=proposed_conditions,
            reasoning=reasoning,
        )
        session.submit_thesis(thesis_msg)
        await pg_add_message(
            session_id=session_id,
            agent_id=agent_uuid,
            message_type="thesis",
            root_cause=root_cause,
            proposed_conditions=proposed_conditions,
            reasoning=reasoning,
        )
        await pg_update_phase(session_id, session.phase.value)

        # 2. Submit antithesis through protocol
        anti_reasoning = antithesis_data.get("counter_reasoning", antithesis_data.get("raw_response", "")[:500])
        anti_concerns = [antithesis_data.get("concerns", "")] if antithesis_data.get("concerns") else []
        anti_msg = DMsg(
            phase="antithesis",
            agent_id="llm-synthetic-reviewer",
            timestamp=now,
            reasoning=anti_reasoning,
            concerns=anti_concerns,
        )
        session.submit_antithesis(anti_msg)
        await pg_add_message(
            session_id=session_id,
            agent_id="llm-synthetic-reviewer",
            message_type="antithesis",
            reasoning=anti_reasoning,
            concerns=anti_concerns or None,
        )
        await pg_update_phase(session_id, session.phase.value)

        # 3. Submit synthesis with agrees=True through protocol
        synth_conditions = [synthesis.get("merged_conditions", "")] if synthesis.get("merged_conditions") else []
        synth_msg = DMsg(
            phase="synthesis",
            agent_id="llm-synthetic-reviewer",
            timestamp=now,
            root_cause=synthesis.get("agreed_root_cause", ""),
            proposed_conditions=synth_conditions,
            reasoning=synthesis.get("reasoning", ""),
            agrees=True,
        )
        session.submit_synthesis(synth_msg)
        await pg_add_message(
            session_id=session_id,
            agent_id="llm-synthetic-reviewer",
            message_type="synthesis",
            root_cause=synthesis.get("agreed_root_cause", ""),
            proposed_conditions=synth_conditions or None,
            reasoning=synthesis.get("reasoning", ""),
            agrees=True,
        )

        # 4. Finalize resolution through protocol (canonical schema)
        resolution_obj = session.finalize_resolution(
            signature_a=f"llm-{agent_uuid[:8]}",
            signature_b="llm-synthetic-reviewer",
        )
        session.resolution = resolution_obj
        await pg_resolve_session(
            session_id=session_id,
            resolution=resolution_obj.to_dict(),
            status="resolved",
        )

        # Store in ACTIVE_SESSIONS
        ACTIVE_SESSIONS[session_id] = session

        logger.info(f"LLM dialectic session {session_id} persisted via protocol")
    except Exception as e:
        logger.warning(f"Could not persist LLM dialectic session: {e}")

    response_data = {
        "success": True,
        "message": f"Dialectic complete. Recommendation: {recommendation}",
        "recommendation": recommendation,
        "session_id": session_id,
        "thesis": {
            "root_cause": thesis["root_cause"],
            "proposed_conditions": thesis["proposed_conditions"]
        },
        "antithesis": {
            "concerns": antithesis_data.get("concerns", ""),
            "counter_reasoning": antithesis_data.get("counter_reasoning", ""),
            "suggested_conditions": antithesis_data.get("suggested_conditions", "")
        },
        "synthesis": {
            "agreed_root_cause": synthesis.get("agreed_root_cause", ""),
            "merged_conditions": synthesis.get("merged_conditions", ""),
            "reasoning": synthesis.get("reasoning", "")
        },
        "next_steps": _get_dialectic_next_steps(recommendation),
        "_note": "Generated via LLM-assisted dialectic (no peer reviewer required)"
    }

    # Store as discovery in knowledge graph for learning
    try:
        from src.knowledge_graph import (
            get_knowledge_graph,
            DiscoveryNode,
            tag_provenance_source,
        )
        from datetime import datetime as _dt

        graph = await get_knowledge_graph()
        # Backends expose add_discovery, not store — the prior call silently
        # errored into logger.debug for as long as that branch existed (#165
        # phantom-write audit incidentally surfaced this dead path).
        discovery = DiscoveryNode(
            id=_dt.now().isoformat(),
            agent_id=agent_uuid,
            summary=f"LLM dialectic: {root_cause[:80]}... → {recommendation}",
            type="dialectic_synthesis",
            tags=["dialectic", "llm-assisted", "recovery", recommendation.lower()],
            details=json.dumps({
                "thesis": thesis,
                "antithesis": result.get("antithesis"),
                "synthesis": synthesis,
                "recommendation": recommendation,
            }, indent=2),
            provenance=tag_provenance_source(None, "dialectic_synthesis"),
        )
        await graph.add_discovery(discovery)
        response_data["discovery_stored"] = True
    except Exception as e:
        logger.debug(f"Could not store dialectic discovery: {e}")

    return success_response(response_data, agent_id=agent_uuid, arguments=arguments)

def _get_dialectic_next_steps(recommendation: str) -> List[str]:
    """Get next steps based on dialectic recommendation."""
    if recommendation == "RESUME":
        return default_resume_steps()
    elif recommendation == "COOLDOWN":
        return default_cooldown_steps()
    else:  # ESCALATE
        return default_escalate_steps()
