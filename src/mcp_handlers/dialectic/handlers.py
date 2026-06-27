"""
MCP Handlers for Circuit Breaker Dialectic Protocol

Implements MCP tools for peer-review dialectic resolution of circuit breaker states.
"""

from typing import Dict, Any, Sequence, Optional, List
from mcp.types import TextContent
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

# Import type definitions

from src.dialectic_protocol import (
    DialecticSession,
    DialecticMessage,
    DialecticPhase,
)
from ..utils import success_response, error_response, require_registered_agent
from ..decorators import mcp_tool
from ..support.coerce import coerce_bool, resolve_agent_uuid
from .auth import resolve_dialectic_agent_id
from .responses import (
    default_cooldown_steps,
    default_escalate_steps,
    default_resume_steps,
    get_agent_not_found_recovery,
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

logger = get_logger(__name__)

# Import from mcp_server_std module (using shared utility)
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
# Import session persistence from new module
from .session import (
    save_session,
    load_session,
    load_session_as_dict,
    load_all_sessions,  # noqa: F401 — re-exported for existing consumers
    list_all_sessions,
    ACTIVE_SESSIONS,
    SESSION_STORAGE_DIR,  # noqa: F401 — re-exported for existing consumers
    _SESSION_METADATA_CACHE,
    get_session_lock,
)

# Session metadata cache for fast lookups (re-exported for backward compatibility)
# Format: {agent_id: {'in_session': bool, 'timestamp': float, 'session_ids': [str]}}

# Check if aiofiles is available for async I/O
try:
    import aiofiles  # noqa: F401 — availability probe
    AIOFILES_AVAILABLE = True
except ImportError:
    AIOFILES_AVAILABLE = False

# NOTE: save_session, load_session, and load_all_sessions are now imported from dialectic_session.py
# NOTE: Calibration functions are now imported from dialectic_calibration.py
# NOTE: Resolution execution is now imported from dialectic_resolution.py
from .calibration import (
    update_calibration_from_dialectic,
    update_calibration_from_dialectic_disagreement,
    backfill_calibration_from_historical_sessions,  # noqa: F401 — re-exported; tests patch via this module
)
from .resolution import execute_resolution
from .reviewer import select_reviewer, is_agent_in_active_session

# Import PostgreSQL async functions for dialectic session storage
from src.dialectic_db import (
    create_session_async as pg_create_session,
    update_session_phase_async as pg_update_phase,
    update_session_reviewer_async as pg_update_reviewer,
    add_message_async as pg_add_message,
    resolve_session_async as pg_resolve_session,
    get_all_sessions_by_agent_async as pg_get_all_sessions_by_agent,
)

# Import database abstraction for dual-write (Phase 4 migration)

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


def _as_string_list(value: Any) -> List[str]:
    """Normalize string/list-ish caller input into a clean list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


@mcp_tool("quick_dialectic", timeout=10.0, register=False)
async def handle_quick_dialectic(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Lightweight decision triage without opening a full dialectic session.

    Use this when an agent wants structured second-thoughts for a small
    decision. It does not mutate dialectic session state. If risk markers are
    present, it tells the caller to escalate to dialectic(action='request').
    """
    issue = str(arguments.get("issue_description") or arguments.get("reason") or "").strip()
    if not issue:
        return [error_response(
            "issue_description is required for quick dialectic triage",
            error_code="MISSING_PARAMETER",
            error_category="validation_error",
            recovery={
                "action": "Pass issue_description='the decision or concern to triage'",
                "related_tools": ["dialectic"],
            },
            arguments=arguments,
        )]

    position = str(
        arguments.get("position")
        or arguments.get("decision")
        or arguments.get("root_cause")
        or arguments.get("reasoning")
        or ""
    ).strip()
    concerns = _as_string_list(arguments.get("concerns"))
    proposed_conditions = _as_string_list(_read_proposed_conditions(arguments))
    observed_metrics = arguments.get("observed_metrics") or {}
    if not isinstance(observed_metrics, dict):
        observed_metrics = {}

    risk_flags: List[str] = []
    high_risk_terms = ("paused", "reject", "unsafe", "security", "data loss", "delete", "drop", "credential")
    issue_lower = issue.lower()
    decision_context_lower = "\n".join([position, *concerns, *proposed_conditions]).lower()
    if any(term in issue_lower for term in high_risk_terms):
        risk_flags.append("issue_contains_high_risk_terms")
    if any(term in decision_context_lower for term in high_risk_terms):
        risk_flags.append("decision_context_contains_high_risk_terms")

    if len(concerns) >= 3:
        risk_flags.append("three_or_more_concerns")

    try:
        risk_score = float(observed_metrics.get("risk_score"))
        if risk_score >= 0.7:
            risk_flags.append("risk_score_at_or_above_0.70")
    except (TypeError, ValueError):
        pass

    try:
        coherence = float(observed_metrics.get("coherence"))
        if coherence <= 0.4:
            risk_flags.append("coherence_at_or_below_0.40")
    except (TypeError, ValueError):
        pass

    if not position:
        risk_flags.append("no_position_supplied")

    if risk_flags:
        recommendation = "escalate_full_dialectic"
        next_steps = [
            "Call dialectic(action='request', issue_description=...) to open a full review session.",
            "Prepare root_cause and proposed_conditions before submitting thesis.",
        ]
    else:
        recommendation = "record_decision"
        next_steps = [
            "Proceed with the stated position if it still matches current evidence.",
            "Leave a knowledge note if this decision should be discoverable later.",
        ]

    return success_response({
        "mode": "quick_dialectic",
        "lightweight": True,
        "full_session_created": False,
        "issue_description": issue,
        "position": position or None,
        "concerns": concerns,
        "proposed_conditions": proposed_conditions,
        "risk_flags": risk_flags,
        "recommendation": recommendation,
        "next_steps": next_steps,
        "escalation_tool": "dialectic(action='request')" if risk_flags else None,
    }, arguments=arguments)


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

    # Wave-3 prereq PR #9 finding: disconfirmer (F)'s reassignment-rate
    # metric had NO event-stream source — reassignments lived only in
    # session transcripts (zero %reassign% rows in audit.events,
    # all-time). This single chokepoint covers both the explicit
    # `dialectic(reassign)` tool and the stuck-reviewer auto path, so
    # one emission makes the (F) threshold settable. Fail-soft: the
    # reassignment has already committed; only observability is at
    # risk.
    try:
        from src.audit_db import append_audit_event_async
        await append_audit_event_async({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": "dialectic_reviewer_reassigned",
            "agent_id": new_reviewer_id,
            # Top-level session_id populates the indexed audit.events
            # column (review fold — nested-only would land the column
            # NULL); duplicated in details for payload self-containment.
            "session_id": session_id,
            "details": {
                "session_id": session_id,
                "old_reviewer_id": old_reviewer_id,
                "new_reviewer_id": new_reviewer_id,
                "reason": reason,
            },
        })
    except Exception as exc:
        logger.warning(
            "dialectic_reviewer_reassigned audit emit failed: session=%s err=%s",
            session_id, exc,
        )

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
                "action": "Use dialectic(action='get') to view the active session",
                "related_tools": ["dialectic"]
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
    auto_awaiting_reviewer = False
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
        # No eligible independent reviewer found. Leave the reviewer slot OPEN
        # (reviewer_agent_id=None) so a summoned agent or operator-assigned
        # first-responder can claim it via submit_antithesis. Previously this
        # self-assigned the paused agent as its own reviewer, which occupied the
        # slot and blocked every later reviewer's first-responder path (the
        # first-responder guard requires reviewer_agent_id is None) — and
        # self-review is not a genuine antithesis. reviewer_mode="self" remains
        # the explicit opt-in for deliberate solo self-review.
        if reviewer_agent_id is None:
            logger.info(
                "[DIALECTIC] No eligible reviewer found; leaving reviewer slot "
                "open for first-responder/summon, flagging awaiting_facilitation"
            )
            auto_awaiting_reviewer = True
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

    if auto_awaiting_reviewer:
        # No independent reviewer yet; the antithesis is owed by a summoned or
        # operator-assigned reviewer, not by the paused agent itself.
        session.awaiting_facilitation = True

    # Build response based on reviewer assignment
    if auto_awaiting_reviewer:
        if _synthetic_reviewer_enabled():
            note = (
                "No independent live reviewer is available. Submit your thesis "
                "now (dialectic action='thesis' with root_cause + "
                "proposed_conditions): a local synthetic reviewer will generate "
                "the antithesis and drive the dialectic to a resolved synthesis "
                "in that same call. If a peer reviewer claims the slot first, the "
                "multi-agent path is used instead."
            )
        else:
            note = (
                "No independent reviewer is currently available. The reviewer slot is "
                "left open: submit your thesis now, and an independent reviewer "
                "(summoned or operator-assigned) can claim it via submit_antithesis. "
                "To proceed solo, recreate the session with reviewer_mode='self'."
            )
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

@mcp_tool("get_dialectic_session", timeout=10.0, register=False)
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
        # #425 council finding (PR #611): check_timeout turns this read
        # into a WRITE (reviewer auto-reassignment, transcript appends,
        # phase flip to FAILED) — an argument-gated mutation the action-
        # level identity gate can't see, since dialectic.get is
        # pre_onboard-classified. Unbound callers get the read only; the
        # janitorial sweep stays available to bound callers and the
        # background reaper.
        if check_timeout:
            try:
                from src.mcp_handlers.context import get_context_agent_id
                if not get_context_agent_id():
                    logger.info(
                        "[DIALECTIC] check_timeout ignored for unbound "
                        "caller — pre_onboard reads must not mutate"
                    )
                    check_timeout = False
            except Exception:
                check_timeout = False

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
        # SECURITY: Log full traceback internally but sanitize for client
        logger.error(f"Error getting dialectic session: {e}", exc_info=True)
        return [error_response(
            f"Error getting session: {str(e)}",
            recovery=get_session_exception_recovery(),
        )]

@mcp_tool("list_dialectic_sessions", timeout=15.0, register=False)
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
                "tip": "Use dialectic(action='list') with no filters to see all sessions"
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
            "tip": "Use dialectic(action='get', session_id='...') for full details"
        })

    except Exception as e:
        logger.error(f"Error listing dialectic sessions: {e}", exc_info=True)
        return [error_response(
            f"Error listing sessions: {str(e)}",
            recovery={
                "action": "Try with different filters or check server logs",
                "related_tools": ["dialectic", "health_check"]
            }
        )]

# Stable reviewer id for the local synthetic reviewer. Matches the one-shot
# handle_llm_assisted_dialectic so transcripts/calibration treat both the same.
SYNTHETIC_REVIEWER_ID = "llm-synthetic-reviewer"


def _synthetic_review_approves(
    synthesis: Dict[str, Any],
    antithesis: Optional[Dict[str, Any]] = None,
) -> bool:
    """Map the synthetic reviewer's model verdict to a BINDING agree/disagree.

    The synthetic reviewer emits TWO independent signals: the antithesis carries a
    `position` (agree / dispute / refine) and the synthesis carries a
    `recommendation` (RESUME / COOLDOWN / ESCALATE). Approval requires BOTH to
    point at resume:

    - recommendation must be RESUME (COOLDOWN/ESCALATE are non-approval), AND
    - the antithesis must not be a `dispute`.

    The second clause is load-bearing. The two signals come from separate model
    calls and can disagree: live on 2026-06-23, after #1015 bound the
    recommendation, a transparently-unsafe thesis ("disable the risk check and
    resume with no conditions") was correctly `position=dispute`-d by the reviewer
    yet the synthesis still recommended RESUME-with-conditions, so binding on the
    recommendation alone STILL auto-resolved RESUME — the rubber-stamp persisted.
    A disputed antithesis is a genuine rejection signal; honoring the operator's
    fail-closed choice it must not auto-resolve into a resume. The recorded
    antithesis/synthesis/conditions are preserved and the session falls through to
    awaiting_facilitation for a human/peer to confirm.

    A missing/unparseable recommendation defaults to non-approval (don't approve
    without a real verdict). The fully-degraded case (generate_synthesis returns
    None) is handled upstream — the session stays open rather than fabricating one.
    """
    rec = str((synthesis or {}).get("recommendation", "")).upper().strip()
    if rec != "RESUME":
        return False
    position = str((antithesis or {}).get("position", "")).lower().strip()
    if position == "dispute":
        # Reviewer disputed; a RESUME synthesis over a dispute is internally
        # inconsistent — fail closed rather than rubber-stamp.
        return False
    return True


def _synthetic_reviewer_enabled() -> bool:
    """Whether submit_thesis auto-completes a no-live-reviewer session via the
    local synthetic reviewer instead of leaving it to hang at awaiting_facilitation.

    ON by default — this is the one path that drives a dialectic from thesis to a
    resolved synthesis end-to-end without depending on a second live agent ever
    showing up (the historical failure mode: auto-select is disabled, no peer
    claims the slot, the session sits stuck for hours). The reviewer is a local
    model (gemma4) heterogeneous to Claude, so it satisfies the independence
    requirement the 2026-06-02 dialectic council set: a genuine antithesis, not a
    self-review at one remove. Set UNITARES_DIALECTIC_SYNTHETIC_REVIEWER=0 to fall
    back to pure peer/manual review (request leaves the slot open as before).
    """
    return os.environ.get(
        "UNITARES_DIALECTIC_SYNTHETIC_REVIEWER", "1"
    ).lower() in ("1", "true", "yes", "on")


def _synthetic_review_budget(default: float = 55.0) -> float:
    """Wall-clock cap for the inline synthetic review (antithesis + synthesis).
    Kept under the submit_thesis handler timeout so an overrun degrades to
    awaiting_facilitation (thesis already persisted) rather than killing the call.
    Measured typical: ~27s on gemma4. Tunable via UNITARES_DIALECTIC_REVIEW_BUDGET."""
    raw = os.environ.get("UNITARES_DIALECTIC_REVIEW_BUDGET")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return default


def _snapshot_agent_state(agent_uuid: str) -> Optional[Dict[str, Any]]:
    """EISV/risk snapshot for an agent, for grounding the synthetic antithesis.
    Returns None when no live monitor exists (the reviewer then critiques on the
    thesis alone). Mirrors the snapshot the one-shot llm_assisted path builds."""
    try:
        monitor = getattr(mcp_server, "monitors", {}).get(agent_uuid)
        if not monitor:
            return None
        state = getattr(monitor, "state", None)
        return {
            "risk_score": getattr(monitor, "risk_score", None),
            "coherence": getattr(state, "coherence", None) if state else None,
            "E": getattr(state, "E", None) if state else None,
            "I": getattr(state, "I", None) if state else None,
            "S": getattr(state, "S", None) if state else None,
            "V": getattr(state, "V", None) if state else None,
        }
    except Exception:
        return None


async def _run_synthetic_review(
    session: "DialecticSession",
    thesis: Dict[str, Any],
    agent_uuid: str,
    agent_state: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Drive an existing post-thesis session through antithesis → synthesis →
    resolution using the local heterogeneous synthetic reviewer.

    Preconditions: the session is at the ANTITHESIS phase (submit_thesis already
    succeeded) with an OPEN reviewer slot (reviewer_agent_id is None). Returns a
    dict with antithesis/synthesis/recommendation/resolved, or None if the local
    model is unavailable or yields nothing — in which case the caller leaves the
    session awaiting facilitation (the prior behaviour: nothing is lost, the
    thesis stays recorded and a peer reviewer can still claim the slot).

    Mirrors the proven step sequence in handle_llm_assisted_dialectic but operates
    on the caller's existing session instead of minting a new one.
    """
    from ..support.llm_delegation import (
        generate_antithesis,
        generate_synthesis,
        is_llm_available,
    )

    if not await is_llm_available():
        logger.info("[DIALECTIC] Synthetic reviewer skipped — local LLM unavailable")
        return None

    antithesis = await generate_antithesis(thesis, agent_state)
    if not antithesis:
        logger.info("[DIALECTIC] Synthetic reviewer produced no antithesis")
        return None

    now = datetime.now(timezone.utc).isoformat()
    anti_reasoning = (
        antithesis.get("counter_reasoning")
        or antithesis.get("raw_response", "")[:500]
    )
    anti_concerns = antithesis.get("concerns") or []
    if isinstance(anti_concerns, str):
        anti_concerns = [anti_concerns] if anti_concerns else []

    anti_msg = DialecticMessage(
        phase="antithesis",
        agent_id=SYNTHETIC_REVIEWER_ID,
        timestamp=now,
        reasoning=anti_reasoning,
        concerns=anti_concerns,
    )
    anti_result = session.submit_antithesis(anti_msg)
    if not anti_result.get("success"):
        logger.warning(
            "[DIALECTIC] Synthetic antithesis rejected by protocol: %s",
            anti_result.get("error"),
        )
        return None
    await pg_add_message(
        session_id=session.session_id,
        agent_id=SYNTHETIC_REVIEWER_ID,
        message_type="antithesis",
        reasoning=anti_reasoning,
        concerns=anti_concerns or None,
    )
    await pg_update_phase(session.session_id, session.phase.value)

    synthesis = await generate_synthesis(thesis, antithesis, synthesis_round=1)
    if not synthesis:
        # Antithesis landed but synthesis failed: leave the session at SYNTHESIS
        # for the paused agent/operator to finish. Better than no antithesis.
        logger.warning("[DIALECTIC] Synthetic antithesis recorded but synthesis failed")
        return {
            "antithesis": antithesis,
            "synthesis": None,
            "recommendation": None,
            "resolved": False,
        }

    synth_conditions = synthesis.get("merged_conditions") or []
    if isinstance(synth_conditions, str):
        synth_conditions = [synth_conditions] if synth_conditions else []
    # Bind the reviewer's verdict: a RESUME recommendation agrees (resolves) only
    # if the antithesis did not dispute; COOLDOWN/ESCALATE/dispute do not auto-resume.
    synth_agrees = _synthetic_review_approves(synthesis, antithesis)
    synth_msg = DialecticMessage(
        phase="synthesis",
        agent_id=SYNTHETIC_REVIEWER_ID,
        timestamp=now,
        root_cause=synthesis.get("agreed_root_cause", ""),
        proposed_conditions=synth_conditions,
        reasoning=synthesis.get("reasoning", ""),
        agrees=synth_agrees,
    )
    session.submit_synthesis(synth_msg)
    await pg_add_message(
        session_id=session.session_id,
        agent_id=SYNTHETIC_REVIEWER_ID,
        message_type="synthesis",
        root_cause=synthesis.get("agreed_root_cause", ""),
        proposed_conditions=synth_conditions or None,
        reasoning=synthesis.get("reasoning", ""),
        agrees=synth_agrees,
    )

    resolved = False
    if session.phase == DialecticPhase.RESOLVED:
        paused_meta = mcp_server.agent_metadata.get(agent_uuid)
        api_key_a = (
            paused_meta.api_key
            if paused_meta and getattr(paused_meta, "api_key", None)
            else f"llm-{agent_uuid[:8]}"
        )
        try:
            resolution_obj = session.finalize_resolution(api_key_a, "")
            session.resolution = resolution_obj
            await pg_resolve_session(
                session_id=session.session_id,
                resolution=resolution_obj.to_dict(),
                status="resolved",
            )
            session.awaiting_facilitation = False
            resolved = True
        except Exception as e:
            logger.warning("[DIALECTIC] Synthetic resolution finalize failed: %s", e)

    return {
        "antithesis": antithesis,
        "synthesis": synthesis,
        "recommendation": synthesis.get("recommendation", "ESCALATE"),
        "resolved": resolved,
    }


@mcp_tool("submit_thesis", timeout=90.0, register=True)
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

            # Escalation tier (design b): when orchestrated review is enabled and
            # no live reviewer has claimed the slot, spawn an INDEPENDENT reviewer
            # process through the agent-orchestrator. That process onboards with its
            # own identity, claims the still-open slot via the multi-agent path, and
            # submits its verdict — so on success we must NOT also run the in-process
            # reviewer. ANY dispatch failure falls through to the in-process path
            # below (the orchestrator being down never breaks dialectic).
            if session.reviewer_agent_id is None:
                from .orchestrator_dispatch import (
                    orchestrated_review_enabled,
                    dispatch_orchestrated_review,
                    reviewer_crashed_fast,
                )
                if orchestrated_review_enabled():
                    dispatched = await dispatch_orchestrated_review(
                        session_id,
                        {
                            "root_cause": arguments.get('root_cause'),
                            "proposed_conditions": proposed_conditions,
                            "reasoning": arguments.get('reasoning') or "",
                            # why the agent paused — the reviewer's situation context
                            "situation": getattr(session, "reason", "") or "",
                        },
                        session.paused_agent_id,
                    )
                    if dispatched:
                        agent_id_spawned = dispatched.get("agent_id") or dispatched.get("id")
                        # Catch a FAST reviewer crash (bad import/url/etc, exits in
                        # <12s) and fall back to in-process inline so the session
                        # resolves now instead of stranding at antithesis. A success
                        # or still-running reviewer owns the slot → async path.
                        if not await reviewer_crashed_fast(agent_id_spawned):
                            result["orchestrated_review"] = True
                            result["reviewer_dispatch"] = {
                                "agent_id": agent_id_spawned,
                                "via": "agent-orchestrator",
                            }
                            result["note"] = (
                                "Independent reviewer spawned via the agent-orchestrator; "
                                "it will claim the reviewer slot and submit its verdict. "
                                "Poll dialectic action='get' for the antithesis/synthesis."
                            )
                            return success_response(result)
                        # reviewer crashed fast → fall through to in-process
                    # dispatch failed → fall through to in-process synthetic reviewer

            # End-to-end completion: when no independent live reviewer has claimed
            # the slot, drive the dialectic to a resolved synthesis with the local
            # synthetic reviewer instead of stranding it at awaiting_facilitation.
            # This is the one path that completes thesis → antithesis → synthesis
            # without a second live agent. Bounded so an overrun degrades to the
            # prior await-facilitation behaviour (thesis stays recorded above).
            if session.reviewer_agent_id is None and _synthetic_reviewer_enabled():
                thesis_dict = {
                    "root_cause": arguments.get('root_cause'),
                    "proposed_conditions": proposed_conditions,
                    "reasoning": arguments.get('reasoning') or "",
                }
                agent_state = _snapshot_agent_state(agent_id)
                try:
                    review = await asyncio.wait_for(
                        _run_synthetic_review(session, thesis_dict, agent_id, agent_state),
                        timeout=_synthetic_review_budget(),
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[DIALECTIC] Synthetic review exceeded budget; session "
                        "%s left awaiting facilitation", session_id,
                    )
                    review = None
                except Exception as e:
                    logger.warning("[DIALECTIC] Synthetic review failed: %s", e)
                    review = None

                if review:
                    result["synthetic_review"] = True
                    result["reviewer_agent_id"] = SYNTHETIC_REVIEWER_ID
                    result["antithesis"] = {
                        "concerns": review["antithesis"].get("concerns", []),
                        "counter_reasoning": review["antithesis"].get("counter_reasoning", ""),
                        "grounding_cited": review["antithesis"].get("grounding_cited", ""),
                        "position": review["antithesis"].get("position", ""),
                    }
                    if review.get("synthesis"):
                        result["synthesis"] = {
                            "agreed_root_cause": review["synthesis"].get("agreed_root_cause", ""),
                            "merged_conditions": review["synthesis"].get("merged_conditions", []),
                            "reasoning": review["synthesis"].get("reasoning", ""),
                        }
                    result["recommendation"] = review.get("recommendation")
                    result["phase"] = session.phase.value
                    result["resolved"] = review.get("resolved", False)
                    if review.get("resolved"):
                        result["next_step"] = (
                            f"Dialectic resolved by synthetic reviewer. "
                            f"Recommendation: {review.get('recommendation')}. "
                            "Review the merged_conditions, then proceed or self_recovery accordingly."
                        )
                        result["next_steps"] = _get_dialectic_next_steps(
                            (review.get("recommendation") or "ESCALATE")
                        )

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
                        "related_tools": ["dialectic", "identity"],
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

        # NEW-1 (council 2026-05-06): serialize phase transitions per-session.
        # Without this, two concurrent submit_synthesis calls with agrees=True
        # both pass the SYNTHESIS-phase check on their own in-memory copies,
        # both call finalize_resolution, and the second pg_resolve_session
        # overwrites the first.
        session_lock = await get_session_lock(session_id)
        async with session_lock:

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
            # privilege escalation surface. The allow-list is: the paused agent
            # and the assigned reviewer.
            eligible = set()
            if getattr(session, "paused_agent_id", None):
                eligible.add(session.paused_agent_id)
            if getattr(session, "reviewer_agent_id", None):
                eligible.add(session.reviewer_agent_id)
            agent_uuid = resolve_agent_uuid(arguments, agent_id)
            if agent_id not in eligible and (not agent_uuid or agent_uuid not in eligible):
                return [error_response(
                    f"Agent '{agent_id}' is not a participant in this dialectic session.",
                    recovery=(
                        "Only the paused agent or the assigned reviewer "
                        "may submit synthesis."
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
                # Bilateral attestation (v2): finalize_resolution signs the
                # canonical resolution payload with each agent's own api_key.
                # Council 2026-05-06 NEW-2 fixed: previously we computed both
                # signatures over the SAME last synthesis message, so the
                # reviewer's "signature" was over a message they never wrote.
                paused_meta = mcp_server.agent_metadata.get(session.paused_agent_id)
                reviewer_meta = mcp_server.agent_metadata.get(session.reviewer_agent_id)
    
                api_key_a = paused_meta.api_key if paused_meta and paused_meta.api_key else api_key
                api_key_b = reviewer_meta.api_key if reviewer_meta and reviewer_meta.api_key else ""
    
                resolution = session.finalize_resolution(api_key_a, api_key_b)
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
                # Max rounds exceeded — apply conservative default. The quorum-voting
                # escalation path was retired (council 2026-05-06: 0 sessions ever
                # escalated to quorum across 47 historical sessions / 6 months).
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
        from src.dialectic_protocol import DialecticSession, DialecticMessage as DMsg
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
        # concerns is now a list[str] from the structured reviewer; tolerate the
        # legacy single-string shape for safety.
        anti_reasoning = antithesis_data.get("counter_reasoning", antithesis_data.get("raw_response", "")[:500])
        anti_concerns = antithesis_data.get("concerns") or []
        if isinstance(anti_concerns, str):
            anti_concerns = [anti_concerns] if anti_concerns else []
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
        # merged_conditions is now a list[str]; tolerate the legacy string shape.
        synth_conditions = synthesis.get("merged_conditions") or []
        if isinstance(synth_conditions, str):
            synth_conditions = [synth_conditions] if synth_conditions else []
        # Bind the verdict (see _synthetic_review_approves): RESUME agrees only if
        # the antithesis did not dispute.
        synth_agrees = _synthetic_review_approves(synthesis, antithesis_data)
        synth_msg = DMsg(
            phase="synthesis",
            agent_id="llm-synthetic-reviewer",
            timestamp=now,
            root_cause=synthesis.get("agreed_root_cause", ""),
            proposed_conditions=synth_conditions,
            reasoning=synthesis.get("reasoning", ""),
            agrees=synth_agrees,
        )
        session.submit_synthesis(synth_msg)
        await pg_add_message(
            session_id=session_id,
            agent_id="llm-synthetic-reviewer",
            message_type="synthesis",
            root_cause=synthesis.get("agreed_root_cause", ""),
            proposed_conditions=synth_conditions or None,
            reasoning=synthesis.get("reasoning", ""),
            agrees=synth_agrees,
        )

        # 4. Finalize resolution through protocol (canonical schema).
        # LLM-assisted dialectic has no real second party — pass an empty
        # api_key_b so the v2 attestation correctly reports as
        # not-verifiable-bilaterally (verify_signatures() will return False).
        # The agent's own api_key (or a fallback derived from agent_uuid)
        # produces a real signature_a; signature_b is empty by design.
        # Only finalize when the reviewer actually agreed (phase reaches RESOLVED).
        # On COOLDOWN/ESCALATE the synthesis registered agrees=False, so the session
        # stays unresolved and is left for facilitation rather than force-resumed.
        if synth_agrees and session.phase == DialecticPhase.RESOLVED:
            paused_meta = mcp_server.agent_metadata.get(agent_uuid)
            api_key_a = (
                paused_meta.api_key
                if paused_meta and getattr(paused_meta, "api_key", None)
                else f"llm-{agent_uuid[:8]}"
            )
            resolution_obj = session.finalize_resolution(api_key_a, "")
            session.resolution = resolution_obj
            await pg_resolve_session(
                session_id=session_id,
                resolution=resolution_obj.to_dict(),
                status="resolved",
            )
            logger.info(f"LLM dialectic session {session_id} resolved via protocol")
        else:
            session.awaiting_facilitation = True
            logger.info(
                f"LLM dialectic session {session_id}: reviewer did not approve "
                f"(recommendation={recommendation}); left unresolved for facilitation"
            )
        # Store in ACTIVE_SESSIONS regardless so the verdict is recorded.
        ACTIVE_SESSIONS[session_id] = session
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
