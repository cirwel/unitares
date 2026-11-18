"""
MCP Handlers for Circuit Breaker Dialectic Protocol

Implements MCP tools for peer-review dialectic resolution of circuit breaker states.
"""

from typing import Dict, Any, Sequence, Optional, List, Tuple  # noqa: F401
from mcp.types import TextContent
import asyncio  # noqa: F401
import json  # noqa: F401
import uuid  # noqa: F401
from datetime import datetime, timedelta, timezone  # noqa: F401


# Import type definitions

from src.dialectic_protocol import (
    DialecticSession,  # noqa: F401
    DialecticMessage,  # noqa: F401
    DialecticPhase,  # noqa: F401
)
from src.dialectic_reviewed_work import (
    ReviewedWorkAuthorship,  # noqa: F401
    ReviewedWorkAuthorshipError,  # noqa: F401
    declaration_for_request,  # noqa: F401
    embed_reviewed_work_authorship,  # noqa: F401
    parse_reviewed_work_author_agent_ids,  # noqa: F401
    read_reviewed_work_authorship,  # noqa: F401
)
from src.coherence_provenance import coherence_role_for_source  # noqa: F401
from ..utils import success_response, error_response, require_registered_agent  # noqa: F401
from ..decorators import mcp_tool
from ..support.coerce import LimitError, coerce_bool, parse_limit, resolve_agent_uuid  # noqa: F401
from .auth import resolve_dialectic_agent_id  # noqa: F401
from .read_projection import compact_session_read  # noqa: F401
from src.dialectic_session_writes import session_write_via  # noqa: F401
from .events import (
    emit_participant_abstained,  # noqa: F401
    emit_reviewer_abstained,  # noqa: F401
    emit_reviewer_reassigned,  # noqa: F401
)
from .responses import (
    get_agent_not_found_recovery,  # noqa: F401
    get_session_exception_recovery,  # noqa: F401
    get_session_timeout_recovery,  # noqa: F401
    llm_failed_recovery,  # noqa: F401
    llm_incomplete_recovery,  # noqa: F401
    llm_missing_root_cause_recovery,  # noqa: F401
    llm_unavailable_recovery,  # noqa: F401
    missing_session_id_recovery,  # noqa: F401
    missing_session_or_agent_recovery,  # noqa: F401
    no_sessions_found_recovery,  # noqa: F401
    next_step_negotiate_synthesis,  # noqa: F401
    next_step_no_consensus,  # noqa: F401
    next_step_resume_not_applied,  # noqa: F401
    next_step_resumed,  # noqa: F401
    next_step_submit_antithesis,  # noqa: F401
    session_not_found_recovery,  # noqa: F401
)
from src.logging_utils import get_logger
from src.broadcaster import broadcaster_instance  # noqa: F401

logger = get_logger(__name__)

# Import from mcp_server_std module (using shared utility)
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server  # noqa: F401
# Import session persistence from new module
from .session import (
    attach_attestation,  # noqa: F401
    discard_receipt_unless_written,  # noqa: F401
    seal_resolution_for_persistence,  # noqa: F401
    save_session,  # noqa: F401
    load_session,  # noqa: F401
    load_session_as_dict,  # noqa: F401
    load_all_sessions,  # noqa: F401 — re-exported for existing consumers
    list_all_sessions,  # noqa: F401
    ACTIVE_SESSIONS,  # noqa: F401
    SESSION_STORAGE_DIR,  # noqa: F401 — re-exported for existing consumers
    _SESSION_METADATA_CACHE,  # noqa: F401
    get_session_lock,  # noqa: F401
)

# Session metadata cache for fast lookups (re-exported for backward compatibility)
# Format: {agent_id: {'in_session': bool, 'timestamp': float, 'session_ids': [str]}}

# Check if aiofiles is available for async I/O
try:
    import aiofiles  # noqa: F401 — availability probe
    AIOFILES_AVAILABLE = True
except ImportError:
    AIOFILES_AVAILABLE = False


# --- Handler timeout derivation (#1442) -------------------------------------
# Every handler that can run the inline synthetic review must have a decorator
# timeout that clears _synthetic_review_budget() PLUS the other work sharing
# the same call. #825 satisfied this for submit_thesis by hand (90s); #1385's
# one-call request_review then did submit_thesis's work under its own original
# 60s timeout — arithmetically "budget < timeout" held (55 < 60) while the
# call died at 60s with nothing persisted (#1442). Deriving the ceilings from
# the budget makes that drift unrepresentable; the ordering is locked by
# tests/test_dialectic_one_call_review.py.
#
# Dispatch margin: orchestrated reviewer dispatch (httpx ≤10s) + fast-crash
# watch (await_seconds 15s + 5s client slack) + pg/BEAM persistence writes.
# One-call request_review = session creation (auth, reviewer selection,
# BEAM/pg insert) + the full nested submit_thesis work above.

# NOTE: save_session, load_session, and load_all_sessions are now imported from dialectic_session.py
# NOTE: Calibration functions are now imported from dialectic_calibration.py
# NOTE: Resolution execution is now imported from dialectic_resolution.py
from .calibration import (
    update_calibration_from_dialectic,  # noqa: F401
    update_calibration_from_dialectic_disagreement,  # noqa: F401
    backfill_calibration_from_historical_sessions,  # noqa: F401 — re-exported; tests patch via this module
)
from .resolution import (
    agent_pause_is_live,  # noqa: F401
    execute_resolution,  # noqa: F401
    record_resolution_outcome,  # noqa: F401
)
from .beam_resolve_client import (
    ALREADY_TERMINAL_ORIGIN,  # noqa: F401
    BeamDialecticPolicyRefusal,  # noqa: F401
    BeamDialecticSagaInFlight,  # noqa: F401
    BeamDialecticTranscriptConflict,  # noqa: F401
    beam_resolve,  # noqa: F401
    beam_create_session,  # noqa: F401
    beam_update_reviewer,  # noqa: F401 - retained test/rollback patch seam
)
from .reviewer import select_reviewer, is_agent_in_active_session  # noqa: F401


# Import PostgreSQL async functions for dialectic session storage
from src.dialectic_db import (
    create_session_async as pg_create_session,  # noqa: F401
    update_session_phase_async as pg_update_phase,  # noqa: F401 - test/compat patch seam
    update_session_reviewer_async as pg_update_reviewer,  # noqa: F401 - test/rollback patch seam
    mark_awaiting_facilitation_async as pg_mark_awaiting_facilitation,  # noqa: F401
    update_session_awaiting_facilitation_async as pg_update_awaiting_facilitation,  # noqa: F401
    add_message_async as pg_add_message,  # noqa: F401
    add_bounded_message_async as pg_add_bounded_message,  # noqa: F401
    resolve_session_async as pg_resolve_session,  # noqa: F401
    claim_resolution_effect_async as pg_claim_resolution_effect,  # noqa: F401
    complete_resolution_effect_async as pg_complete_resolution_effect,  # noqa: F401
    fail_resolution_effect_async as pg_fail_resolution_effect,  # noqa: F401
    get_resolution_effect_async as pg_get_resolution_effect,  # noqa: F401
    acknowledge_resolution_effect_async as pg_acknowledge_resolution_effect,  # noqa: F401
    quarantine_resolution_effect_async as pg_quarantine_resolution_effect,  # noqa: F401
    get_all_sessions_by_agent_async as pg_get_all_sessions_by_agent,  # noqa: F401
)

from .actionability import (
    _synthetic_review_budget,  # noqa: F401
    SUBMIT_THESIS_TIMEOUT,
    REQUEST_REVIEW_TIMEOUT,
    _beam_policy_error_code,  # noqa: F401
    _beam_policy_recovery,  # noqa: F401
    _ResolutionCommitResult,  # noqa: F401
    _ResolutionEffectResult,  # noqa: F401
    _TranscriptAppendRefused,  # noqa: F401
    _meta_value,  # noqa: F401
    _agent_label,  # noqa: F401
    _agent_status,  # noqa: F401
    _agent_display,  # noqa: F401
    _as_string_list,  # noqa: F401
    _reviewer_objection_stands_in_session_data,  # noqa: F401
    _reviewer_verdict_pending_in_session_data,  # noqa: F401
    _latest_synthesis_agent_in_session_data,  # noqa: F401
    _saved_brief_thesis_call,  # noqa: F401
    _last_activity_age_s,  # noqa: F401
    _has_orchestrated_reviewer_budget,  # noqa: F401
    _build_dialectic_actionability,  # noqa: F401
    _reviewer_authorship_conflict,  # noqa: F401
    _PAUSE_EVIDENCE_SCHEMA,  # noqa: F401
    _capture_pause_evidence,  # noqa: F401
    AutomatedTrigger,  # noqa: F401
    LIST_TOPIC_PREVIEW_CHARS,  # noqa: F401
    _preview_topic,  # noqa: F401
    SYNTHETIC_REVIEWER_ID,  # noqa: F401
    _judgment_was_formed,  # noqa: F401
    _REVIEWER_PROVENANCE_KEYS,  # noqa: F401
    _REVIEWER_KINDS,  # noqa: F401
    _reviewer_provenance_stamp,  # noqa: F401
    _synthetic_reviewer_provenance,  # noqa: F401
    _merge_caller_reviewer_provenance,  # noqa: F401
    CONSULT_PHASE,  # noqa: F401
    MAX_CONSULTS_PER_SESSION,  # noqa: F401
    _consult_list,  # noqa: F401
    _synthetic_review_approves,  # noqa: F401
    _REVIEWER_FLOOR_NAMES,  # noqa: F401
    _reviewer_fallback_block,  # noqa: F401
    _reviewer_profile_block,  # noqa: F401
    _synthetic_reviewer_enabled,  # noqa: F401
    _snapshot_agent_state,  # noqa: F401
    _read_terminal_readback,  # noqa: F401
    _terminal_readback_next_step,  # noqa: F401
    _apply_terminal_readback,  # noqa: F401
    _thesis_session_advanced,  # noqa: F401
    _terminal_commit_refusal_response,  # noqa: F401
    _resolution_effect_view,  # noqa: F401
    _parse_resolution_effect_updated_at,  # noqa: F401
    _resolution_effect_skips_resume_calibration,  # noqa: F401
    _get_dialectic_next_steps,  # noqa: F401
)
from .resolution_helpers import (
    _resolve_dialectic_agent_id,  # noqa: F401
    _emit_dialectic_event,  # noqa: F401
    _emit_phase_changed,  # noqa: F401
    _set_awaiting_facilitation,  # noqa: F401
    _append_protocol_message,  # noqa: F401
    _commit_terminal_resolution,  # noqa: F401
    _precommit_resolution_authority_refusal,  # noqa: F401
    _self_review_requires_independent_authority,  # noqa: F401
    _read_proposed_conditions,  # noqa: F401
    _emit_request_reviewer_reassigned,  # noqa: F401
    _refuse_self_review_convergence,  # noqa: F401
    _attach_resolution_to_result,  # noqa: F401
    _reviewer_objection_stands_refusal,  # noqa: F401
    _record_operator_resolution_telemetry,  # noqa: F401
    _bind_pause_token,  # noqa: F401
    _recorded_trigger_source,  # noqa: F401
    _requested_reviewed_work_authorship,  # noqa: F401
    _commit_converged_resolution,  # noqa: F401
    _apply_durable_resolution_effect,  # noqa: F401
    check_reviewer_stuck,  # noqa: F401
    _attach_public_resolution_effect,  # noqa: F401
    _validate_explicit_reviewer_candidate,  # noqa: F401
    _reviewer_reassignment_actor,  # noqa: F401
    _apply_reviewer_reassignment,  # noqa: F401
    _validate_synthesis_participant_and_conflict,  # noqa: F401
    _record_synthesis_abstention,  # noqa: F401
    _parse_synthesis_proposal,  # noqa: F401
    _submit_and_persist_synthesis_message,  # noqa: F401
    _finalize_hard_limit_block,  # noqa: F401
    _commit_and_apply_converged_resume,  # noqa: F401
    _finalize_terminal_session_state,  # noqa: F401
    _finalize_converged_synthesis,  # noqa: F401
    _finalize_max_rounds_synthesis_failure,  # noqa: F401
    _handle_reassign_reviewer,
    _handle_reconcile_resolution_effect,
)
from .llm_assisted import (
    _handle_submit_consult,
    _run_synthetic_review,  # noqa: F401
    _thesis_recorded_after_facilitation_refused,  # noqa: F401
    _handle_llm_assisted_dialectic,
)

from .phase_submission import (
    _handle_quick_dialectic,
    _handle_request_dialectic_review,
    _handle_get_dialectic_session,
    _handle_list_dialectic_sessions,
    _handle_submit_thesis,
    _handle_submit_antithesis,
    _handle_submit_synthesis,
)
# Import database abstraction for dual-write (Phase 4 migration)

# ==============================================================================
# NOTE: Dialectic handlers (Feb 2026)
# ==============================================================================
# ACTIVE: request_dialectic_review, submit_thesis, submit_antithesis, submit_synthesis
# ACTIVE: get_dialectic_session, list_dialectic_sessions, llm_assisted_dialectic
# Still removed: request_exploration_session, nudge_dialectic_session, handle_self_recovery
# ==============================================================================


@mcp_tool("quick_dialectic", timeout=10.0, register=False)
async def handle_quick_dialectic(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Lightweight decision triage without opening a full dialectic session.

    Use this when an agent wants structured second-thoughts for a small
    decision. It does not mutate dialectic session state. If risk markers are
    present, it tells the caller to escalate to dialectic(action='request').
    """
    return await _handle_quick_dialectic(arguments)


# register=False: this name is a `dialectic` alias, and resolve_alias rewrites it
# before handler lookup -- see the "one name, one home" note in tool_stability.py.
@mcp_tool("request_dialectic_review", timeout=REQUEST_REVIEW_TIMEOUT, register=False)
async def handle_request_dialectic_review(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Create a dialectic review session.

    Explicit thesis fields progress the protocol in the same call. Callers may
    also set ``use_brief_as_thesis`` to reuse the issue description as those
    fields; the friendly ``request_review`` alias does this by default.
    """
    return await _handle_request_dialectic_review(
        arguments,
        llm_assisted_fn=lambda args: handle_llm_assisted_dialectic(args),
        submit_thesis_fn=lambda args: handle_submit_thesis(args),
    )


@mcp_tool("get_dialectic_session", timeout=10.0, register=False)
async def handle_get_dialectic_session(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    View an active or past dialectic session.

    Args:
        session_id: Dialectic session ID (optional if agent_id provided)
        agent_id: Agent ID to find sessions for (optional if session_id provided)

    Returns:
        Session state with next-action guidance; fields=compact omits reconstruction bodies
    """
    return await _handle_get_dialectic_session(arguments)


@mcp_tool("list_dialectic_sessions", timeout=15.0, register=False)
async def handle_list_dialectic_sessions(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    List all dialectic sessions with optional filtering.

    Allows agents to browse active and past dialectic sessions. Returns
    summaries by default for efficiency.

    Args:
        agent_id: Filter by agent (either requestor or reviewer) - optional
        status: Filter by phase (e.g., 'resolved', 'failed', 'thesis') - optional
        limit: Max sessions to return (default 50, capped at 200). Must be
            >= 1; a non-positive or non-numeric limit is rejected rather than
            silently substituted.
        include_transcript: Include full transcript in results (default False)

    Returns:
        List of session summaries with optional full transcript
    """
    return await _handle_list_dialectic_sessions(arguments)

# Stable reviewer id for the local synthetic reviewer. Matches the one-shot
# handle_llm_assisted_dialectic so transcripts/calibration treat both the same.


# Reviewer/model provenance persisted with a verdict, riding the namespaced
# observed_metrics["reviewer_backend"] key the orchestrated reviewer already
# writes (agents/dialectic_reviewer/reviewer.py:_provenance_for_message — see
# its docstring for why observed_metrics is the surviving persisted slot).
# Allowlist, not denylist: the dict is assembled from provider responses and
# caller input, and a denylist would leak whatever a future backend adds.
# Motivation: the 2026-08-18 replay put reviewer verdicts 36-50% apart by
# capability tier while 0 of 194 live verdicts recorded which model produced
# them or whether a fallback fired.


# Bounds transcript growth from one session's outside filings. A consult does
# not touch the liveness clock, so this is about size, not about holding a
# session open.


# register=False: reached through dialectic(action='consult') only.
@mcp_tool("submit_consult", timeout=10.0, register=False)
async def handle_submit_consult(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """File an outside verdict on a review as a record with no authority.

    The documented way to file a review produced outside this server was an
    antithesis carrying ``reviewer_provenance={"reviewer_kind":
    "external_consult", ...}``. An antithesis needs the reviewer slot, and the
    orchestrated reviewer takes it within about a minute of the request, with
    takeover reserved to an operator. So an agent holding an outside model's
    verdict had nowhere to put it, and ``external_consult`` counted zero rows.

    A consult needs no slot. Any bound agent, including the paused agent or
    the assigned reviewer, may file one on any session, open or closed. It is
    stored as a ``consult`` transcript entry stamped
    ``reviewer_kind='external_consult'``, with the filer's role and the phase
    at filing. It has no authority: it never advances a phase, never counts as
    a verdict (``agrees`` stays NULL; the position it states lives in
    ``observed_metrics.consult.position``), and never refreshes the session's
    liveness clock. The per-session bound is enforced atomically in
    PostgreSQL (``add_bounded_message``), not on a loaded transcript.
    """
    return await _handle_submit_consult(arguments)


# Reviewer-list names that mean the local floor, as the reviewer reads them
# (agents/dialectic_reviewer/host_list.FLOOR_NAMES; a test pins the two).


# register=False: this name is a `dialectic` alias, and resolve_alias rewrites it
# before handler lookup -- see the "one name, one home" note in tool_stability.py.
@mcp_tool("submit_thesis", timeout=SUBMIT_THESIS_TIMEOUT, register=False)
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
    return await _handle_submit_thesis(arguments)


# register=False: this name is a `dialectic` alias, and resolve_alias rewrites it
# before handler lookup -- see the "one name, one home" note in tool_stability.py.
@mcp_tool("submit_antithesis", timeout=10.0, register=False)
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
    return await _handle_submit_antithesis(arguments)


# register=False: this name is a `dialectic` alias, and resolve_alias rewrites it
# before handler lookup -- see the "one name, one home" note in tool_stability.py.
@mcp_tool("submit_synthesis", timeout=15.0, register=False)
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
    return await _handle_submit_synthesis(arguments)


# register=False: this name is a `dialectic` alias, and resolve_alias rewrites it
# before handler lookup -- see the "one name, one home" note in tool_stability.py.
@mcp_tool("reassign_reviewer", timeout=15.0, register=False)
async def handle_reassign_reviewer(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Reassign the reviewer for an active dialectic session.

    Use when the current reviewer is unresponsive (ephemeral session ended)
    or a facilitation request needs a replacement reviewer. Requires either an
    authenticated operator credential or the current independent reviewer
    handing off its own assignment.

    Args:
        session_id: Dialectic session ID
        new_reviewer_id: Agent ID to assign as new reviewer (optional — auto-selects if omitted)
        reason: Why the reviewer is being reassigned (optional)

    Returns:
        Updated session state with new reviewer info
    """
    return await _handle_reassign_reviewer(arguments)


# register=False: consolidated dialectic action owns the public name.
@mcp_tool("reconcile_dialectic_effect", timeout=30.0, register=False)
async def handle_reconcile_resolution_effect(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Apply a pending effect or acknowledge an operator-repaired partial one.

    Applying/unknown-partial states are never replayed automatically: the worker
    may have applied only some non-idempotent conditions before it stopped. An
    operator first confirms an applying worker has stopped and fences that exact
    claim with ``mark_uncertain``. After inspecting and repairing live state,
    ``mark_applied`` closes only the resulting unknown-partial record.
    """
    return await _handle_reconcile_resolution_effect(arguments)


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
    return await _handle_llm_assisted_dialectic(arguments)

