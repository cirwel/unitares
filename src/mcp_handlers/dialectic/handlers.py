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
    DialecticSession,
    DialecticMessage,  # noqa: F401
    DialecticPhase,
)
from src.dialectic_reviewed_work import (
    ReviewedWorkAuthorship,  # noqa: F401
    ReviewedWorkAuthorshipError,  # noqa: F401
    declaration_for_request,  # noqa: F401
    embed_reviewed_work_authorship,  # noqa: F401
    parse_reviewed_work_author_agent_ids,  # noqa: F401
    read_reviewed_work_authorship,
)
from src.coherence_provenance import coherence_role_for_source  # noqa: F401
from ..utils import success_response, error_response, require_registered_agent  # noqa: F401
from ..decorators import mcp_tool
from ..support.coerce import LimitError, coerce_bool, parse_limit, resolve_agent_uuid  # noqa: F401
from .auth import resolve_dialectic_agent_id
from .read_projection import compact_session_read  # noqa: F401
from src.dialectic_session_writes import session_write_via
from .events import (
    emit_participant_abstained,  # noqa: F401
    emit_reviewer_abstained,  # noqa: F401
    emit_reviewer_reassigned,
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

async def _resolve_dialectic_agent_id(
    arguments: Dict[str, Any],
    *,
    enforce_session_ownership: bool = False,
    require_bound_caller: bool = False,
) -> tuple:
    """Backward-compatible wrapper around shared dialectic auth policy."""
    return await resolve_dialectic_agent_id(
        arguments,
        enforce_session_ownership=enforce_session_ownership,
        require_bound_caller=require_bound_caller,
    )
from src.logging_utils import get_logger
from src.broadcaster import broadcaster_instance

logger = get_logger(__name__)

# Import from mcp_server_std module (using shared utility)
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server  # noqa: F401
# Import session persistence from new module
from .session import (
    attach_attestation,
    discard_receipt_unless_written,  # noqa: F401
    seal_resolution_for_persistence,  # noqa: F401
    save_session,
    load_session,  # noqa: F401
    load_session_as_dict,  # noqa: F401
    load_all_sessions,  # noqa: F401 — re-exported for existing consumers
    list_all_sessions,  # noqa: F401
    ACTIVE_SESSIONS,
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
    update_calibration_from_dialectic,
    update_calibration_from_dialectic_disagreement,  # noqa: F401
    backfill_calibration_from_historical_sessions,  # noqa: F401 — re-exported; tests patch via this module
)
from .resolution import (
    agent_pause_is_live,
    execute_resolution,  # noqa: F401
    record_resolution_outcome,
)
from .beam_resolve_client import (
    ALREADY_TERMINAL_ORIGIN,
    BeamDialecticPolicyRefusal,  # noqa: F401
    BeamDialecticSagaInFlight,
    BeamDialecticTranscriptConflict,
    beam_resolve,
    beam_create_session,
    beam_update_reviewer,  # noqa: F401 - retained test/rollback patch seam
)
from .reviewer import select_reviewer, is_agent_in_active_session  # noqa: F401


# Import PostgreSQL async functions for dialectic session storage
from src.dialectic_db import (
    create_session_async as pg_create_session,
    update_session_phase_async as pg_update_phase,  # noqa: F401 - test/compat patch seam
    update_session_reviewer_async as pg_update_reviewer,  # noqa: F401 - test/rollback patch seam
    mark_awaiting_facilitation_async as pg_mark_awaiting_facilitation,
    update_session_awaiting_facilitation_async as pg_update_awaiting_facilitation,
    add_message_async as pg_add_message,
    add_bounded_message_async as pg_add_bounded_message,  # noqa: F401
    resolve_session_async as pg_resolve_session,
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
    _ResolutionCommitResult,
    _ResolutionEffectResult,  # noqa: F401
    _TranscriptAppendRefused,
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
    _resolution_effect_skips_resume_calibration,
    _get_dialectic_next_steps,  # noqa: F401
)
from .resolution_helpers import (
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
    _bind_pause_token,  # noqa: F401
    _recorded_trigger_source,  # noqa: F401
    _requested_reviewed_work_authorship,  # noqa: F401
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


async def _emit_dialectic_event(event_type: str, session, **payload_extra) -> None:
    """Best-effort broadcast of a dialectic lifecycle event (#1167 Ask 1).

    Telemetry only — must never raise into the resolution path, so it mirrors the
    knowledge handlers' guarded broadcast pattern. The broadcaster auto-persists to
    audit.events and serves the WS firehose; the dashboard and the Phoenix dialectic
    pane subscribe to any ``dialectic_*`` event as a doorbell to refetch
    authoritative state. Names include the engine-owner-ratified set from #1167
    plus the reviewer-dispatch proof point:
    ``dialectic_opened``, ``dialectic_reviewer_dispatched``,
    ``dialectic_facilitation_needed``, ``dialectic_resolved``.

    Emitted from the Python handler intentionally: when
    UNITARES_DIALECTIC_BEAM_RESOLUTION is on, BEAM is an alternative *persistence*
    backend, not an alternative control flow — this handler still runs, so a single
    emit here covers both the BEAM and pg-fallback paths.
    """
    try:
        phase = getattr(session, "phase", None)
        payload = {
            "session_id": getattr(session, "session_id", None),
            "phase": getattr(phase, "value", phase),
            "topic": getattr(session, "topic", None),
            "session_type": getattr(session, "session_type", None),
            "awaiting_facilitation": getattr(session, "awaiting_facilitation", False),
        }
        payload.update(payload_extra)
        await broadcaster_instance.broadcast_event(
            event_type,
            agent_id=getattr(session, "paused_agent_id", None),
            payload=payload,
        )
    except Exception as e:  # pragma: no cover - telemetry must not break the flow
        logger.debug("dialectic event %s emit skipped: %s", event_type, e)


async def _emit_phase_changed(session, prev_phase) -> None:
    """Emit dialectic_phase_changed when an interactive submit advanced the phase
    (thesis -> antithesis -> synthesis), for live intra-session progress. No-op if
    the phase did not move (e.g. another synthesis round stays in SYNTHESIS).
    Terminal phases (resolved/failed) are announced by dialectic_resolved, not here.
    """
    to_phase = getattr(getattr(session, "phase", None), "value", None)
    if to_phase and to_phase != prev_phase:
        await _emit_dialectic_event(
            "dialectic_phase_changed", session, from_phase=prev_phase, to_phase=to_phase
        )


async def _set_awaiting_facilitation(
    session: DialecticSession,
    awaiting: bool,
    *,
    reason: Optional[str] = None,
) -> bool:
    """Persist a real transition into or out of human-facilitation state.

    An open reviewer slot before thesis submission is not yet a failure: the
    orchestrated reviewer is summoned only after the thesis exists. Keeping the
    flag transition at the point where automated review actually succeeds or
    fails prevents false ``dialectic_facilitation_needed`` events while retaining
    the existing fail-soft telemetry/persistence contract.
    """
    awaiting = bool(awaiting)
    previous_awaiting = bool(
        getattr(session, "awaiting_facilitation", False)
    )
    saga_revision = int(
        getattr(session, "resolution_saga_revision", 0)
    )

    try:
        if awaiting and not previous_awaiting:
            persisted = await pg_mark_awaiting_facilitation(
                session.session_id,
                expected_phase=session.phase.value,
                expected_reviewer=session.reviewer_agent_id,
                expected_transcript_revision=int(session.transcript_revision),
                expected_resolution_saga_revision=saga_revision,
                exclude_inflight_saga=True,
                touch_updated_at=True,
            )
        else:
            persisted = await pg_update_awaiting_facilitation(
                session.session_id,
                awaiting,
                expected_phase=session.phase.value,
                expected_reviewer=session.reviewer_agent_id,
                expected_transcript_revision=int(session.transcript_revision),
                expected_awaiting_facilitation=previous_awaiting,
                expected_resolution_saga_revision=saga_revision,
                exclude_inflight_saga=True,
                live_only=True,
            )
    except Exception as exc:
        logger.warning(
            "Could not persist awaiting_facilitation=%s for %s: %s",
            awaiting,
            session.session_id,
            exc,
        )
        raise

    if not persisted:
        ACTIVE_SESSIONS.pop(session.session_id, None)
        raise _TranscriptAppendRefused(
            "the dialectic changed before facilitation state could be recorded"
        )

    session.awaiting_facilitation = awaiting

    if awaiting and not previous_awaiting and reason:
        await _emit_dialectic_event(
            "dialectic_facilitation_needed", session, reason=reason
        )
    return True


async def _append_protocol_message(
    session: DialecticSession,
    *,
    expected_phase: str,
    expected_reviewer: Optional[str],
    expected_transcript_revision: int,
    expected_awaiting_facilitation: Optional[bool] = None,
    next_phase: str,
    next_synthesis_round: int,
    next_reviewer: Optional[str] = None,
    update_reviewer: bool = False,
    next_awaiting_facilitation: Optional[bool] = None,
    next_status: Optional[str] = None,
    reopen_failed_facilitation: bool = False,
    **message: Any,
) -> int:
    """Append one message and atomically advance its durable protocol state."""
    detail: Dict[str, Any] = {}
    state_updates: Dict[str, Any] = {
        "next_phase": next_phase,
        "next_synthesis_round": next_synthesis_round,
    }
    state_updates["expected_awaiting_facilitation"] = (
        bool(session.awaiting_facilitation)
        if expected_awaiting_facilitation is None
        else expected_awaiting_facilitation
    )
    if update_reviewer:
        state_updates["next_reviewer"] = next_reviewer
    if next_awaiting_facilitation is not None:
        state_updates["next_awaiting_facilitation"] = (
            next_awaiting_facilitation
        )
    if next_status is not None:
        state_updates["next_status"] = next_status
    if reopen_failed_facilitation:
        state_updates["reopen_failed_facilitation"] = True
    message_id = await pg_add_message(
        session_id=session.session_id,
        expected_phase=expected_phase,
        expected_reviewer=expected_reviewer,
        expected_transcript_revision=expected_transcript_revision,
        detail=detail,
        **state_updates,
        **message,
    )
    revision = detail.get("transcript_revision")
    expected_next_revision = expected_transcript_revision + 1
    if (
        not message_id
        or type(revision) is not int
        or revision != expected_next_revision
    ):
        raise _TranscriptAppendRefused(
            "the dialectic transcript changed before the message committed"
        )
    session.transcript_revision = revision
    return revision


async def _commit_terminal_resolution(
    session: DialecticSession,
    payload: Dict[str, Any],
    *,
    status: str,
    expected_transcript_revision: int,
    expected_phase: str = DialecticPhase.SYNTHESIS.value,
) -> _ResolutionCommitResult:
    """Win one exact terminal transition at the loaded transcript frontier.

    The phase, reviewer, and monotonically increasing transcript revision form
    one authority boundary. BEAM owns the write when enabled; the Python
    fallback carries the identical predicates. A live BEAM saga is not an
    availability failure: falling back would create a second terminal writer,
    so it is returned as a transient refusal.
    """
    beam_detail: Dict[str, Any] = {}
    try:
        beam_result = await beam_resolve(
            session_id=session.session_id,
            paused_agent_id=session.paused_agent_id,
            reviewer_agent_id=session.reviewer_agent_id,
            resolution=payload,
            status=status,
            expected_phase=expected_phase,
            expected_transcript_revision=expected_transcript_revision,
            expected_awaiting_facilitation=bool(
                session.awaiting_facilitation
            ),
            detail=beam_detail,
        )
    except BeamDialecticSagaInFlight:
        return _ResolutionCommitResult(False, False, "beam_saga_in_flight")
    except BeamDialecticTranscriptConflict:
        return _ResolutionCommitResult(False, False, "transcript_changed")
    already_terminal = bool(
        isinstance(beam_result, dict)
        and beam_result.get("origin") == ALREADY_TERMINAL_ORIGIN
    )
    if beam_result is not None:
        if already_terminal:
            return _ResolutionCommitResult(False, False, "refused")
        return _ResolutionCommitResult(True, False, "beam_ack")

    # Once an HTTP request started, a lost/5xx response may hide either a
    # committed terminal row or a different live saga's definitive 409. Never
    # open a second, write-capable Python path under that ambiguity: a losing
    # payload could otherwise beat the saga that already owns the session.
    if beam_result is None and beam_detail.get("commit_unknown") is True:
        return _ResolutionCommitResult(
            False, True, "beam_commit_unknown_unconfirmed"
        )

    written = False
    winner: Dict[str, Any] = {}
    if beam_result is None:
        with session_write_via(
            "python_fallback", site="converged_resolution"
        ):
            try:
                written = bool(
                    await pg_resolve_session(
                        session_id=session.session_id,
                        resolution=payload,
                        status=status,
                        expected_phase=expected_phase,
                        expected_reviewer=session.reviewer_agent_id,
                        expected_transcript_revision=expected_transcript_revision,
                        expected_awaiting_facilitation=bool(
                            session.awaiting_facilitation
                        ),
                        expected_resolution_saga_revision=int(
                            getattr(session, "resolution_saga_revision", 0)
                        ),
                        exclude_inflight_saga=True,
                        winner=winner,
                    )
                )
            except Exception:
                raise
    if written:
        return _ResolutionCommitResult(True, False, "python_write")

    if winner.get("winner_status") in {
        DialecticPhase.RESOLVED.value,
        DialecticPhase.FAILED.value,
    }:
        return _ResolutionCommitResult(False, False, "terminal_conflict")
    return _ResolutionCommitResult(False, False, "refused")


async def _precommit_resolution_authority_refusal(
    session: DialecticSession,
) -> Optional[str]:
    """Recheck the authority facts that can change before the terminal CAS.

    The earlier handler guard gives a useful response, but an active reflection
    can become a live pause while synthesis is being persisted.  This check is
    deliberately adjacent to the effect-owner write: if the session is now a
    self-authorized resume, it stays nonterminal and answerable.  Stored
    authorship policy remains duplicated in the database guard as the final
    cross-process boundary.
    """
    agent_id = session.paused_agent_id
    if await _self_review_requires_independent_authority(session):
        return "self_review_not_authorizing"

    authorship = read_reviewed_work_authorship(
        getattr(session, "paused_agent_state", {}),
        expected_declared_by_agent_id=agent_id,
    )
    if authorship.is_invalid:
        return "review_authorship_invalid"
    if authorship.recuses(
        session.reviewer_agent_id, paused_agent_id=agent_id
    ):
        return "reviewer_author_conflict"
    return None


async def _self_review_requires_independent_authority(
    session: DialecticSession,
) -> bool:
    """True when this verdict would let one identity authorize its own pause.

    The frozen pause-generation binding is authoritative even when this
    process's metadata cache still says ``active``. The live check remains for
    the narrower active-at-open race covered by the existing TOCTOU guard.
    """
    agent_id = session.paused_agent_id
    if not session.reviewer_agent_id or session.reviewer_agent_id != agent_id:
        return False
    paused_state = getattr(session, "paused_agent_state", {})
    if (
        isinstance(paused_state, dict)
        and paused_state.get("pause_effect_required") is True
    ):
        return True
    return await agent_pause_is_live(agent_id)


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


@mcp_tool("quick_dialectic", timeout=10.0, register=False)
async def handle_quick_dialectic(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Lightweight decision triage without opening a full dialectic session."""
    return await _handle_quick_dialectic(arguments)


async def _emit_request_reviewer_reassigned(
    *,
    session_id: str,
    old_reviewer_id: Optional[str],
    new_reviewer_id: str,
    reason: str,
) -> None:
    await emit_reviewer_reassigned(
        session_id=session_id,
        old_reviewer_id=old_reviewer_id,
        new_reviewer_id=new_reviewer_id,
        reason=reason,
        source="request",
    )


# register=False: this name is a `dialectic` alias, and resolve_alias rewrites it
# before handler lookup -- see the "one name, one home" note in tool_stability.py.
@mcp_tool("request_dialectic_review", timeout=REQUEST_REVIEW_TIMEOUT, register=False)
async def handle_request_dialectic_review(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Create a dialectic review session."""
    return await _handle_request_dialectic_review(
        arguments,
        beam_create_session_fn=lambda **kw: beam_create_session(**kw),
        pg_create_session_fn=lambda **kw: pg_create_session(**kw),
    )


@mcp_tool("get_dialectic_session", timeout=10.0, register=False)
async def handle_get_dialectic_session(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """View an active or past dialectic session."""
    return await _handle_get_dialectic_session(arguments)


@mcp_tool("list_dialectic_sessions", timeout=15.0, register=False)
async def handle_list_dialectic_sessions(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """List all dialectic sessions with optional filtering."""
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
    """Record an advisory peer second opinion without advancing session phase."""
    return await _handle_submit_consult(arguments)


# Reviewer-list names that mean the local floor, as the reviewer reads them
# (agents/dialectic_reviewer/host_list.FLOOR_NAMES; a test pins the two).


# register=False: this name is a `dialectic` alias, and resolve_alias rewrites it
# before handler lookup -- see the "one name, one home" note in tool_stability.py.

# register=False: this name is a `dialectic` alias, and resolve_alias rewrites it
# before handler lookup -- see the "one name, one home" note in tool_stability.py.
@mcp_tool("submit_thesis", timeout=SUBMIT_THESIS_TIMEOUT, register=False)
async def handle_submit_thesis(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Paused agent submits thesis: 'What I did, what I think happened'."""
    return await _handle_submit_thesis(
        arguments,
        mark_awaiting_facilitation_fn=lambda *a, **kw: pg_mark_awaiting_facilitation(*a, **kw),
    )


# register=False: this name is a `dialectic` alias, and resolve_alias rewrites it
# before handler lookup -- see the "one name, one home" note in tool_stability.py.
@mcp_tool("submit_antithesis", timeout=10.0, register=False)
async def handle_submit_antithesis(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Reviewer agent submits antithesis: 'What I observe, my concerns'."""
    return await _handle_submit_antithesis(arguments)


async def _refuse_self_review_convergence(
    session: Any,
    session_id: str,
    result: Dict[str, Any],
    arguments: Dict[str, Any],
) -> Sequence[TextContent]:
    """Route a converged self-review to facilitation instead of lifting a live pause."""
    # A self-review may not release a live pause (#1585 item 1).
    #
    # `reviewer_mode="self"` collapses both roles into one
    # identity, so `submit_synthesis` converged on the paused
    # agent's own `agrees=True` and `execute_resolution` would
    # return it to active on nobody's authority but its own.
    # The existing guards cover only the neighbouring cases: a
    # self-reviewer overturning its OWN recorded rejection, and
    # a paused agent agreeing while an INDEPENDENT reviewer's
    # verdict is pending. The plain first agreeing synthesis was
    # documented as "the explicit self-review completion path" —
    # which is the self-certification hole under its own name.
    #
    # ⛔The check belongs HERE, not in `submit_synthesis`. The
    # protocol object cannot see live agent status, and the fact
    # that decides this is precisely "would a pause be released",
    # which is only knowable at the actuator. Putting it here
    # also keeps `DialecticSession` pure and reuses the handler's
    # established authority to override a convergence — the
    # safety-violation branch directly above does the same thing.
    #
    # ROUTES, does not refuse. `recusal.py` settled the shape for
    # this conflict class when the reviewing apparatus is an
    # interested party: "It never renders a verdict. It routes...
    # It fails toward facilitation — a person decides — rather
    # than toward silent auto-review by a party with a stake."
    # That module names this very case as the sibling conflict it
    # deliberately does not cover ("issue #1585,
    # reviewer_mode='self'"). Its reasoning for why introspection
    # cannot close the gap is the reasoning for this guard: "the
    # same generative process produced both the judgment and the
    # introspection, so a better model narrows the gap without
    # closing it. You close it by adding a DIFFERENT process."
    #
    # The synthesis is already recorded above, so the agent's
    # reasoning survives for whoever picks the session up; what
    # is refused is the resume, not the submission.
    session.phase = DialecticPhase.SYNTHESIS
    result["action"] = "facilitation_required"
    result["success"] = False
    result["converged"] = False
    result["blocked"] = "self_review_not_authorizing"
    await _set_awaiting_facilitation(
        session, True, reason="self_review_not_authorizing"
    )
    # Re-snapshot: the save above ran while the protocol still
    # had this session RESOLVED. `defer_terminal` suppressed the
    # terminal PostgreSQL write but the JSON snapshot happened
    # anyway, so without this the offline snapshot claims a
    # resolution that was refused. The PG phase write was skipped
    # on the converged path, so the row already reads SYNTHESIS.
    try:
        await save_session(session, primary_already_synced=True)
    except Exception as e:
        logger.warning(
            f"Could not re-save session after refusing a self-review "
            f"resolution: {e}"
        )
    return [error_response(
        "A self-review cannot release your own pause. The synthesis "
        "is recorded; resuming requires an independent reviewer or "
        "an operator.",
        error_code="SELF_REVIEW_NOT_AUTHORIZING",
        error_category="governance_refusal",
        details={
            "session_id": session_id,
            "reviewer_agent_id": session.reviewer_agent_id,
            "paused_agent_id": session.paused_agent_id,
            "awaiting_facilitation": True,
        },
        recovery={
            "action": (
                "Your synthesis WAS recorded and the session remains "
                "open — what is refused is resuming yourself on your "
                "own verdict. An operator can assign an independent "
                "reviewer with dialectic(action='reassign', "
                f"session_id='{session_id}', new_reviewer_id='<agent_id>'). "
                "Self-review remains available for reflection; it only "
                "stops short of lifting a pause."
            ),
            "related_tools": ["dialectic", "identity"],
        },
        arguments=arguments,
    )]


def _attach_resolution_to_result(result: Dict[str, Any], resolution: Any) -> None:
    result["resolution"] = resolution.to_dict()
    attach_attestation(result)


def _reviewer_objection_stands_refusal(
    session_id: str,
    result: Dict[str, Any],
) -> Sequence[TextContent]:
    """Build refusal response when a standing reviewer rejection blocks synthesis convergence."""
    operator_next_call = (
        f"dialectic(action='reassign', session_id='{session_id}', "
        "new_reviewer_id='<agent_id>', "
        "reason='Facilitate standing reviewer rejection')"
    )
    # ⛔What is refused is the RESOLUTION, not the message. This
    # submission was persisted to the transcript just above, before
    # this branch ran, and the reviewer can read and answer it. Only
    # a REPEAT attempt is refused before persistence.
    #
    # Saying only "refused" made a caller conclude its response was
    # discarded — that happened on 2026-08-19 in session
    # def32eb2b4b2ce93, where a paused agent conceded, was told
    # "do not retry ... without being recorded", reported the
    # concession as lost, and was wrong: the reviewer had already
    # answered it point by point. An error envelope that understates
    # what happened is the same defect class as a success envelope
    # that overstates it.
    return [error_response(
        result.get("reason") or result.get("error")
        or "Refused: the reviewer's standing verdict is a rejection.",
        error_code="SELF_CLEAR_REFUSED",
        error_category="governance_refusal",
        recovery={
            "action": (
                "Your response WAS recorded in the transcript and the "
                "reviewer can answer it. What is refused is resolving "
                "the session over the reviewer's standing rejection — "
                "not the submission itself. Do not retry: a repeat "
                "attempt IS refused before persistence, and it also "
                "resets nothing in your favour."
            ),
            "what_was_recorded": (
                "This synthesis is now in the transcript under your "
                "agent_id. Conceding is recorded the same way a "
                "contest is; neither resumes you on its own."
            ),
            "what_you_can_do": [
                (
                    "Reviewer: read the newly recorded synthesis and submit a "
                    "later one to maintain or revise the verdict."
                ),
                f"Operator: assign a facilitator/replacement reviewer with {operator_next_call}",
                (
                    "Paused agent: your position is on the record — poll "
                    "dialectic(action='get') for the reviewer's answer rather "
                    "than resubmitting."
                ),
            ],
            "operator_next_call": operator_next_call,
            "related_tools": ["dialectic", "agent"],
        },
    )]


# register=False: this name is a `dialectic` alias, and resolve_alias rewrites it
# before handler lookup -- see the "one name, one home" note in tool_stability.py.

# register=False: this name is a `dialectic` alias, and resolve_alias rewrites it
# before handler lookup -- see the "one name, one home" note in tool_stability.py.
@mcp_tool("submit_synthesis", timeout=15.0, register=False)
async def handle_submit_synthesis(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Either agent submits synthesis proposal during negotiation."""
    return await _handle_submit_synthesis(arguments)


# register=False: this name is a `dialectic` alias, and resolve_alias rewrites it
# before handler lookup -- see the "one name, one home" note in tool_stability.py.
@mcp_tool("reassign_reviewer", timeout=15.0, register=False)
async def handle_reassign_reviewer(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Reassign reviewer for a dialectic session without resetting progress."""
    return await _handle_reassign_reviewer(arguments)


async def _record_operator_resolution_telemetry(
    session: DialecticSession,
    execution_result: Dict[str, Any],
    *,
    record_outcome: bool,
    manually_reconciled: bool,
) -> None:
    """Best-effort downstream signals for the winning reconciliation path.

    The durable resolution-effect row remains authoritative. None of these
    signals may make an already-applied runtime effect replayable, so each is
    isolated and failures are logged. Callers report only that delivery was
    attempted: the downstream helpers are deliberately fail-soft and do not
    provide a durable delivery acknowledgement.
    """
    resolution = session.resolution
    if resolution is None:
        return

    if record_outcome:
        try:
            await record_resolution_outcome(
                session, resolution, execution_result
            )
        except Exception as exc:
            logger.warning(
                "Resolution outcome telemetry failed for %s: %s",
                session.session_id,
                exc,
            )

    if not _resolution_effect_skips_resume_calibration(execution_result):
        try:
            await update_calibration_from_dialectic(session, resolution)
        except Exception as exc:
            logger.warning(
                "Resolution calibration telemetry failed for %s: %s",
                session.session_id,
                exc,
            )

    try:
        await _emit_dialectic_event(
            "dialectic_resolved",
            session,
            action=resolution.action,
            manually_reconciled=manually_reconciled,
        )
    except Exception as exc:
        logger.warning(
            "Resolution lifecycle telemetry failed for %s: %s",
            session.session_id,
            exc,
        )


# register=False: consolidated dialectic action owns the public name.
@mcp_tool("reconcile_dialectic_effect", timeout=30.0, register=False)
async def handle_reconcile_resolution_effect(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Explicitly reconcile a pending or failed post-commit dialectic effect."""
    return await _handle_reconcile_resolution_effect(arguments)


@mcp_tool("llm_assisted_dialectic", timeout=45.0, register=False)
async def handle_llm_assisted_dialectic(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Run LLM-assisted dialectic recovery when no peer reviewer is available."""
    return await _handle_llm_assisted_dialectic(
        arguments,
        create_session_fn=lambda **kw: pg_create_session(**kw),
    )

