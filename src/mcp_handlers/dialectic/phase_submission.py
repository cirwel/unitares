"""Session creation, session read/list, and thesis/antithesis/synthesis submission helpers for Dialectic handlers."""

from typing import Dict, Any, Sequence, Optional, List
from mcp.types import TextContent
import asyncio
import json
from datetime import datetime, timezone

from src.dialectic_protocol import (
    DialecticSession,
    DialecticMessage,
    DialecticPhase,
)
from src.dialectic_reviewed_work import (
    ReviewedWorkAuthorship,
    ReviewedWorkAuthorshipError,
    declaration_for_request,
    embed_reviewed_work_authorship,
    parse_reviewed_work_author_agent_ids,
    read_reviewed_work_authorship,
)
from src.coherence_provenance import coherence_role_for_source
from ..utils import (
    success_response,
    error_response,
    require_registered_agent as _default_require_registered_agent,
)
from ..support.coerce import (
    LimitError,
    coerce_bool,
    parse_limit,
    resolve_agent_uuid as _default_resolve_agent_uuid,
)
from .read_projection import compact_session_read
from .events import emit_reviewer_abstained as _default_emit_reviewer_abstained
from .responses import (
    get_session_exception_recovery,
    get_session_timeout_recovery,
    missing_session_id_recovery,
    missing_session_or_agent_recovery,
    no_sessions_found_recovery,
    next_step_negotiate_synthesis,
    next_step_submit_antithesis,
    session_not_found_recovery,
)
from src.logging_utils import get_logger
from .session import (
    attach_attestation,
    save_session as _default_save_session,
    load_session as _default_load_session,
    load_session_as_dict as _default_load_session_as_dict,
    list_all_sessions as _default_list_all_sessions,
    ACTIVE_SESSIONS as _default_ACTIVE_SESSIONS,
    get_session_lock as _default_get_session_lock,
)
from .beam_resolve_client import BeamDialecticPolicyRefusal
from .reviewer import (
    select_reviewer as _default_select_reviewer,
    is_agent_in_active_session as _default_is_agent_in_active_session,
)
from .actionability import (
    _synthetic_review_budget,
    _beam_policy_error_code,
    _beam_policy_recovery,
    _TranscriptAppendRefused,
    _agent_label,
    _as_string_list,
    _saved_brief_thesis_call,
    _build_dialectic_actionability,
    _reviewer_authorship_conflict,
    _PAUSE_EVIDENCE_SCHEMA,
    _capture_pause_evidence,
    AutomatedTrigger,
    _preview_topic,
    SYNTHETIC_REVIEWER_ID,
    _judgment_was_formed,
    _merge_caller_reviewer_provenance,
    _reviewer_fallback_block,
    _reviewer_profile_block,
    _synthetic_reviewer_enabled,
    _snapshot_agent_state,
    _read_terminal_readback,
    _terminal_readback_next_step,
    _apply_terminal_readback,
    _get_dialectic_next_steps,
)
from .resolution_helpers import (
    _DictProxy,
    _McpServerProxy,
    _h_call,
    _proxy,
    check_reviewer_stuck as _default_check_reviewer_stuck,
    _attach_public_resolution_effect as _default_attach_public_resolution_effect,
    _validate_explicit_reviewer_candidate as _default_validate_explicit_reviewer_candidate,
    _reviewer_reassignment_actor as _default_reviewer_reassignment_actor,
    _apply_reviewer_reassignment as _default_apply_reviewer_reassignment,
    _validate_synthesis_participant_and_conflict as _default_validate_synthesis_participant_and_conflict,
    _record_synthesis_abstention as _default_record_synthesis_abstention,
    _parse_synthesis_proposal as _default_parse_synthesis_proposal,
    _submit_and_persist_synthesis_message as _default_submit_and_persist_synthesis_message,
    _finalize_converged_synthesis as _default_finalize_converged_synthesis,
    _finalize_max_rounds_synthesis_failure as _default_finalize_max_rounds_synthesis_failure,
)
from .llm_assisted import (
    _run_synthetic_review as _default_run_synthetic_review,
    _thesis_recorded_after_facilitation_refused as _default_thesis_recorded_after_facilitation_refused,
)

logger = get_logger(__name__)

mcp_server = _McpServerProxy()
ACTIVE_SESSIONS = _DictProxy("ACTIVE_SESSIONS", _default_ACTIVE_SESSIONS)

require_registered_agent = _proxy("require_registered_agent", _default_require_registered_agent)
resolve_agent_uuid = _proxy("resolve_agent_uuid", _default_resolve_agent_uuid)
save_session = _proxy("save_session", _default_save_session)
load_session = _proxy("load_session", _default_load_session)
load_session_as_dict = _proxy("load_session_as_dict", _default_load_session_as_dict)
list_all_sessions = _proxy("list_all_sessions", _default_list_all_sessions)
get_session_lock = _proxy("get_session_lock", _default_get_session_lock)
select_reviewer = _proxy("select_reviewer", _default_select_reviewer)
is_agent_in_active_session = _proxy("is_agent_in_active_session", _default_is_agent_in_active_session)
emit_reviewer_abstained = _proxy("emit_reviewer_abstained", _default_emit_reviewer_abstained)
check_reviewer_stuck = _proxy("check_reviewer_stuck", _default_check_reviewer_stuck)
_attach_public_resolution_effect = _proxy("_attach_public_resolution_effect", _default_attach_public_resolution_effect)
_validate_explicit_reviewer_candidate = _proxy("_validate_explicit_reviewer_candidate", _default_validate_explicit_reviewer_candidate)
_reviewer_reassignment_actor = _proxy("_reviewer_reassignment_actor", _default_reviewer_reassignment_actor)
_apply_reviewer_reassignment = _proxy("_apply_reviewer_reassignment", _default_apply_reviewer_reassignment)
_validate_synthesis_participant_and_conflict = _proxy("_validate_synthesis_participant_and_conflict", _default_validate_synthesis_participant_and_conflict)
_record_synthesis_abstention = _proxy("_record_synthesis_abstention", _default_record_synthesis_abstention)
_parse_synthesis_proposal = _proxy("_parse_synthesis_proposal", _default_parse_synthesis_proposal)
_submit_and_persist_synthesis_message = _proxy("_submit_and_persist_synthesis_message", _default_submit_and_persist_synthesis_message)
_finalize_converged_synthesis = _proxy("_finalize_converged_synthesis", _default_finalize_converged_synthesis)
_finalize_max_rounds_synthesis_failure = _proxy("_finalize_max_rounds_synthesis_failure", _default_finalize_max_rounds_synthesis_failure)
_run_synthetic_review = _proxy("_run_synthetic_review", _default_run_synthetic_review)
_thesis_recorded_after_facilitation_refused = _proxy("_thesis_recorded_after_facilitation_refused", _default_thesis_recorded_after_facilitation_refused)
_resolve_dialectic_agent_id = _proxy("_resolve_dialectic_agent_id")
_read_proposed_conditions = _proxy("_read_proposed_conditions")
_append_protocol_message = _proxy("_append_protocol_message")
_commit_terminal_resolution = _proxy("_commit_terminal_resolution")
_emit_dialectic_event = _proxy("_emit_dialectic_event")
_emit_phase_changed = _proxy("_emit_phase_changed")
_set_awaiting_facilitation = _proxy("_set_awaiting_facilitation")
_reviewer_objection_stands_refusal = _proxy("_reviewer_objection_stands_refusal")
handle_llm_assisted_dialectic = _proxy("handle_llm_assisted_dialectic")
handle_submit_thesis = _proxy("handle_submit_thesis")
pg_get_all_sessions_by_agent = _proxy("pg_get_all_sessions_by_agent")
beam_create_session = _proxy("beam_create_session")
pg_create_session = _proxy("pg_create_session")
pg_mark_awaiting_facilitation = _proxy("pg_mark_awaiting_facilitation")


async def _handle_quick_dialectic(arguments: Dict[str, Any]) -> Sequence[TextContent]:
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

    coherence_diagnostic = None
    if "coherence" in observed_metrics:
        try:
            coherence_value = float(observed_metrics.get("coherence"))
        except (TypeError, ValueError):
            coherence_value = None
        coherence_source = observed_metrics.get("coherence_source") or "unknown"
        coherence_diagnostic = {
            "value": coherence_value,
            "source": coherence_source,
            "role": (
                observed_metrics.get("coherence_role")
                or coherence_role_for_source(coherence_source)
            ),
            "authoritative": False,
            "health_evidence": False,
        }

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
        "triage_policy": {
            "schema": "dialectic.triage.authority.v2",
            "authoritative_inputs": [
                "issue_terms",
                "decision_context_terms",
                "concern_count",
                "risk_score",
                "position_presence",
            ],
            "diagnostic_inputs": (
                {"coherence": coherence_diagnostic}
                if coherence_diagnostic is not None
                else {}
            ),
        },
        "recommendation": recommendation,
        "next_steps": next_steps,
        "escalation_tool": "dialectic(action='request')" if risk_flags else None,
    }, arguments=arguments)


async def _bind_pause_token(
    evidence: Dict[str, Any], agent_id: str
) -> Dict[str, Any]:
    """Bind a review to the authoritative pause generation at session open."""
    from src.agent_storage import read_authoritative_pause_binding

    binding = await read_authoritative_pause_binding(agent_id)
    cached = getattr(mcp_server, "agent_metadata", {}).get(agent_id)
    cached_status = getattr(cached, "status", None) if cached is not None else None
    cached_token = getattr(cached, "paused_at", None) if cached is not None else None
    if cached_status == "paused":
        if not binding["pause_effect_required"]:
            raise RuntimeError(
                "cache reports a pause that is absent from durable status"
            )
        if cached_token and cached_token != binding["pause_token"]:
            raise RuntimeError(
                "cache and durable state report different pause generations"
            )
    bound = dict(evidence)
    bound["pause_effect_required"] = binding["pause_effect_required"]
    bound["pause_token"] = binding["pause_token"]
    return bound


def _recorded_trigger_source(arguments: Dict[str, Any]) -> str:
    supplied = arguments.get("trigger_source")
    if isinstance(supplied, AutomatedTrigger) and str(supplied):
        return str(supplied)
    return "manual"


async def _requested_reviewed_work_authorship(
    arguments: Dict[str, Any], *, declared_by_agent_id: str
) -> tuple[Optional[ReviewedWorkAuthorship], Optional[Sequence[TextContent]]]:
    """Validate the requester's author-recusal declaration.

    This is a conflict declaration, not an authorship attestation.  UUID-only
    lookup avoids first-match label/public-id ambiguity, and every accepted ID
    must already be registered.  The field can only shrink the eligible
    reviewer pool.
    """
    raw = arguments.get("reviewed_work_author_agent_ids")
    try:
        author_ids = parse_reviewed_work_author_agent_ids(raw)
    except ReviewedWorkAuthorshipError as exc:
        return None, [error_response(
            str(exc),
            error_code="REVIEW_AUTHOR_DECLARATION_INVALID",
            error_category="validation_error",
            recovery={
                "action": (
                    "Pass canonical UUIDs of registered agents whose authored "
                    "work is being reviewed, or omit the field when unspecified."
                ),
                "related_tools": ["identity", "agent", "request_review"],
            },
            arguments=arguments,
        )]

    if author_ids:
        await mcp_server.load_metadata_async()
        missing = [
            author_id
            for author_id in author_ids
            if author_id not in mcp_server.agent_metadata
        ]
        if missing:
            return None, [error_response(
                "Every reviewed-work author must be a registered UNITARES agent UUID.",
                error_code="REVIEW_AUTHOR_NOT_FOUND",
                error_category="validation_error",
                recovery={
                    "action": "Correct or remove the unregistered author UUIDs.",
                    "unregistered_agent_ids": missing,
                    "related_tools": ["agent", "identity"],
                },
                arguments=arguments,
            )]

    return declaration_for_request(
        author_ids, declared_by_agent_id=declared_by_agent_id
    ), None


# register=False: this name is a `dialectic` alias, and resolve_alias rewrites it
# before handler lookup -- see the "one name, one home" note in tool_stability.py.
async def _handle_request_dialectic_review(
    arguments: Dict[str, Any],
    *,
    beam_create_session_fn: Any = None,
    pg_create_session_fn: Any = None,
) -> Sequence[TextContent]:
    """
    Create a dialectic review session.

    Explicit thesis fields progress the protocol in the same call. Callers may
    also set ``use_brief_as_thesis`` to reuse the issue description as those
    fields; the friendly ``request_review`` alias does this by default.
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
                "workflow": "Pass client_session_id on this call to bind as yourself. identity(client_session_id=...) reports the binding; a bare identity() mints a new agent instead of reading yours."
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
                "action": "Call identity() or sync_state() to register.",
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

    # Skip auto-triggered sessions for non-reasoning agents (NON_REASONING_TAGS).
    # These agents can't submit theses — sessions would remain stuck at thesis phase forever.
    reviewer_mode = arguments.get("reviewer_mode", "")
    if reviewer_mode == "auto":
        from src.grounding.class_indicator import NON_REASONING_TAGS

        agent_tags = set(t.lower() for t in (getattr(meta, "tags", None) or []))
        if agent_tags & NON_REASONING_TAGS:
            logger.info("[DIALECTIC] Skipping auto-recovery for non-reasoning agent")
            return success_response({
                "success": True,
                "skipped": True,
                "reason": "Non-reasoning agent cannot participate in dialectic",
                "agent_id": agent_uuid,
                "agent_tags": sorted(agent_tags & NON_REASONING_TAGS),
                "recommendation": "Pause event logged. Recovery handled via agent lifecycle, not dialectic."
            })

    # Prevent duplicate sessions
    if await is_agent_in_active_session(agent_uuid):
        return [error_response(
            "Agent already has an active dialectic session.",
            error_code="SESSION_EXISTS",
            error_category="validation_error",
            recovery={
                "action": (
                    f"Use dialectic(action='get', agent_id={agent_uuid!r}, "
                    "fields='compact') to inspect your sessions before continuing "
                    "the existing review."
                ),
                "tool": "dialectic",
                "arguments": {
                    "action": "get",
                    "agent_id": agent_uuid,
                    "fields": "compact",
                    "check_timeout": False,
                },
                "related_tools": ["dialectic"],
            },
            arguments=arguments
        )]

    reviewed_work_authorship, authorship_error = (
        await _h_call("_requested_reviewed_work_authorship")(
            arguments, declared_by_agent_id=agent_uuid
        )
    )
    if authorship_error:
        return authorship_error
    assert reviewed_work_authorship is not None

    # Pydantic `model_dump()` materializes Optional fields as an explicit None
    # (schemas/dialectic.py + middleware/params_step.py), so `.get(k, default)`
    # returns the stored None, never the default. Use `or`-chains.
    # #1414: also fold in issue_description — the parameter the tool's own
    # examples advertise — so the review's subject is not silently discarded.
    # (Live row c7b26e6adcf68d62 was written with reason NULL and topic NULL.)
    reason = (
        arguments.get("reason")
        or arguments.get("issue_description")
        or "Dialectic review requested"
    )
    session_type = arguments.get("session_type") or "review"
    discovery_id = arguments.get("discovery_id")
    dispute_type = arguments.get("dispute_type")
    topic = arguments.get("topic") or arguments.get("issue_description") or reason
    reviewer_mode = arguments.get("reviewer_mode", "auto")  # auto|self|llm
    max_synthesis_rounds = arguments.get("max_synthesis_rounds", 5)
    trigger_source = _h_call("_recorded_trigger_source")(arguments)

    # LLM-assisted dialectic: delegate to synthetic reviewer
    if reviewer_mode == "llm":
        llm_args = {
            "root_cause": reason,
            "proposed_conditions": arguments.get("proposed_conditions", []),
            "reasoning": arguments.get("reasoning", ""),
            # #1414: forward the AUTHORITATIVE UUID. `require_registered_agent`
            # has already rewritten arguments["agent_id"] to the PUBLIC HANDLE,
            # which is not an identity key. This path happens to survive today
            # because the callee re-runs require_registered_agent, but it is the
            # same latent shape as the one-call-review launder below.
            "agent_id": agent_uuid,
        }
        for key in ("client_session_id", "api_key", "session_type", "trigger_source"):
            if key in arguments:
                llm_args[key] = arguments[key]
        if reviewed_work_authorship.is_declared:
            llm_args["reviewed_work_author_agent_ids"] = list(
                reviewed_work_authorship.author_agent_ids
            )
        return await handle_llm_assisted_dialectic(llm_args)

    # Capture the exact decision-time evidence bundle. Never substitute the
    # monitor's bare ODE state: it is diagnostic and not the verdict authority.
    try:
        monitor = getattr(mcp_server, "monitors", {}).get(agent_uuid)
        paused_agent_state = _capture_pause_evidence(monitor)
    except Exception:
        paused_agent_state = {
            "schema": _PAUSE_EVIDENCE_SCHEMA,
            "evidence_status": "unavailable",
            "source": "capture_error",
            "selection": "last_completed_governance_update_at_session_open",
            "limitation": "Governance evidence capture failed at session open.",
        }
    # Overwrite the reserved key only after evidence capture; callers never get
    # a path to inject server-owned review metadata through observed state.
    paused_agent_state = embed_reviewed_work_authorship(
        paused_agent_state, reviewed_work_authorship
    )
    try:
        paused_agent_state = await _h_call("_bind_pause_token")(
            paused_agent_state, agent_uuid
        )
    except Exception as exc:
        logger.warning(
            "Could not bind dialectic to authoritative agent state (%s)",
            type(exc).__name__,
        )
        return [error_response(
            "The agent's authoritative pause state could not be verified, so "
            "the review was not opened.",
            error_code="PAUSE_BINDING_UNAVAILABLE",
            error_category="system_error",
            recovery={
                "action": "Retry after the agent and identity status stores agree.",
                "related_tools": ["identity", "health_check"],
            },
            arguments=arguments,
        )]

    # Reviewer selection
    auto_open_reviewer_slot = False
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
                exclude_agent_ids=list(
                    reviewed_work_authorship.author_agent_ids
                ),
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
                "open for first-responder/summon after thesis submission"
            )
            auto_open_reviewer_slot = True
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
        # BEAM owns the write + starts a liveness watcher at birth when flagged.
        # Fall back only when it is known not to have accepted the write; an
        # ambiguous post-send failure must be reconciled before another writer
        # is opened.
        # (dialectic-on-BEAM Slice 2.1)
        beam_detail: Dict[str, Any] = {}
        _beam_create_fn = beam_create_session_fn or beam_create_session
        beam_created = await _beam_create_fn(
            session_id=session.session_id,
            paused_agent_id=session.paused_agent_id,
            fields={
                "reviewer_agent_id": session.reviewer_agent_id,
                "reason": reason,
                "discovery_id": discovery_id,
                "dispute_type": dispute_type,
                "session_type": session_type,
                "topic": topic,
                "max_synthesis_rounds": session.max_synthesis_rounds,
                "synthesis_round": session.synthesis_round,
                "paused_agent_state": paused_agent_state,
                "trigger_source": trigger_source,
            },
            detail=beam_detail,
        )
        if beam_created is None and beam_detail.get("commit_unknown"):
            return [error_response(
                "The session-creation outcome is unknown; do not open another "
                "session until this session ID is reconciled.",
                error_code="DIALECTIC_SESSION_CREATE_COMMIT_UNKNOWN",
                error_category="system_error",
                details={
                    "session_id": session.session_id,
                    "commit_unknown": True,
                },
                recovery={
                    "action": "Inspect this session ID before retrying creation.",
                    "related_tools": ["dialectic"],
                },
                arguments=arguments,
            )]
        if beam_created is None:
            _pg_create_fn = pg_create_session_fn or pg_create_session
            await _pg_create_fn(
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
    except BeamDialecticPolicyRefusal as e:
        error_code = _beam_policy_error_code(e)
        return [error_response(
            str(e),
            error_code=error_code,
            error_category="governance_refusal",
            recovery=_beam_policy_recovery(e),
            arguments=arguments,
        )]
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
    await _emit_dialectic_event("dialectic_opened", session)

    # Build response based on reviewer assignment
    if auto_open_reviewer_slot:
        if _synthetic_reviewer_enabled():
            note = (
                "No standing peer is assigned yet. Submit your thesis now "
                "(dialectic action='thesis' with root_cause + proposed_conditions): "
                "a configured independent reviewer will be summoned, with the local "
                "synthetic reviewer as fallback. If a peer claims the slot first, "
                "the multi-agent path is used instead."
            )
        else:
            note = (
                "No standing peer is assigned yet. The reviewer slot is left open: "
                "submit your thesis now, and an independent reviewer (summoned or "
                "operator-assigned) can claim it via dialectic(action='antithesis'). Human "
                "facilitation is requested only if those review paths fail."
            )
    elif session.reviewer_agent_id and session.reviewer_agent_id != agent_uuid:
        note = f"Reviewer assigned: {session.reviewer_agent_id[:12]}... Use dialectic(action='thesis') to add your thesis."
    elif session.reviewer_agent_id:
        note = "Session created with self-review. Use dialectic(action='thesis') to add your thesis."
    else:
        note = "Session created. Awaiting reviewer assignment. Operator should assign a reviewer, then paused agent submits thesis."

    # One-call review (adoption UX): when the request already carries thesis
    # content, submit it in the same call. The friendly request_review alias
    # sets use_brief_as_thesis for a lone issue description; raw dialectic
    # requests retain the explicit two-call flow unless callers opt in.
    use_brief_as_thesis = coerce_bool(
        arguments.get("use_brief_as_thesis"), default=False
    )
    if (
        arguments.get("reasoning")
        or arguments.get("root_cause")
        or use_brief_as_thesis
    ):
        saved_brief = arguments.get("issue_description") or reason
        thesis_args: Dict[str, Any] = {
            "session_id": session.session_id,
            "root_cause": arguments.get("root_cause")
            or saved_brief,
            "reasoning": arguments.get("reasoning")
            or (saved_brief if use_brief_as_thesis else ""),
            "proposed_conditions": arguments.get("proposed_conditions") or [],
            # #1414: forward the AUTHORITATIVE UUID, not arguments["agent_id"] —
            # require_registered_agent has already rewritten that slot to the
            # PUBLIC HANDLE, which is not an identity key. Passing the handle
            # made the nested submit_thesis auth fail ("not registered") against
            # a session the caller had just been authorized to create, leaving a
            # committed session row with no thesis in it.
            "agent_id": agent_uuid,
        }
        for key in ("client_session_id", "api_key"):
            if key in arguments:
                thesis_args[key] = arguments[key]
        thesis_result = await handle_submit_thesis(thesis_args)
        try:
            payload = json.loads(thesis_result[0].text)
        except Exception:
            # Un-parseable thesis response — return it untouched rather than
            # dropping information; the session exists and the thesis call's
            # own error/recovery text stands.
            return thesis_result
        payload.setdefault("session_id", session.session_id)
        payload.setdefault(
            "reviewed_work_authorship", reviewed_work_authorship.as_dict()
        )
        payload["one_call_review"] = True
        if use_brief_as_thesis:
            payload["thesis_source"] = "issue_description"
        # #1414: the nested thesis can fail AFTER the session row committed
        # (bad phase, missing root_cause, DB error). Say so, instead of letting
        # a failed payload fall through to the `else` branch below and be
        # labelled "thesis recorded". This must stay ABOVE the resolution read
        # so no later branch can reach a failed payload.
        # A payload that itself says thesis_recorded=True (the facilitation
        # mark was refused after the thesis committed, #2905) is not this case.
        if (
            payload.get("success") is False
            and payload.get("thesis_recorded") is not True
        ):
            payload["thesis_recorded"] = False
            payload["session_created"] = True
            payload["whose_move"] = (
                "YOURS — the session exists but your thesis was NOT recorded"
            )
            payload["next_call"] = (
                _saved_brief_thesis_call(session.session_id, session.session_type)
            )
            return success_response(payload, arguments=arguments)
        resolution = payload.get("resolution") or {}
        if payload.get("thesis_recorded") is True and payload.get("effect_pending"):
            # #2905 review: a terminal decision committed while the fallback
            # ran, but its durable effect is not confirmed applied. Not
            # "resolved in this call" until someone reconciles it.
            payload["review_verdict"] = resolution.get("action") or payload.get(
                "terminal_status"
            )
            payload["whose_move"] = (
                "an operator's — thesis recorded and a terminal decision "
                "committed, but its effect is not confirmed; reconcile with "
                "dialectic(action='reconcile_effect', "
                f"session_id='{session.session_id}')"
            )
        elif (
            payload.get("thesis_recorded") is True
            and payload.get("terminal_status") == "failed"
        ):
            payload["review_verdict"] = (
                payload.get("action") or resolution.get("action") or "failed"
            )
            payload["whose_move"] = (
                "nobody — thesis recorded; the review ended in a terminal "
                "failure. Read the recorded reason before starting a "
                "replacement review"
            )
        elif payload.get("phase") == "resolved" or resolution:
            payload["review_verdict"] = resolution.get("action") or "resolved"
            payload["whose_move"] = "nobody — review resolved in this call"
        elif payload.get("reviewer_dispatch"):
            payload["whose_move"] = (
                "the reviewer's — an independent reviewer was spawned; poll "
                f"dialectic(action='get', session_id='{session.session_id}') "
                "for its antithesis/synthesis"
            )
        elif payload.get("state_reload_required"):
            payload["whose_move"] = (
                "unknown — thesis recorded, but the session changed before its "
                "state could be confirmed; reload with "
                f"dialectic(action='get', session_id='{session.session_id}') "
                "before another write"
            )
        elif payload.get("spawned_reviewer_exited"):
            payload["whose_move"] = (
                "unknown — thesis recorded; the reviewer this request spawned "
                "exited, and the reviewer now holding the slot may be that one; "
                f"check dialectic(action='get', session_id='{session.session_id}') "
                "and reassign or request facilitation if it does not move"
            )
        elif payload.get("session_advanced"):
            payload["whose_move"] = (
                "the reviewer's — thesis recorded and a reviewer has already "
                "taken the session; poll "
                f"dialectic(action='get', session_id='{session.session_id}')"
            )
        else:
            payload["whose_move"] = (
                "a reviewer's — thesis recorded, awaiting a reviewer to claim "
                "the open slot"
            )
        return success_response(payload, arguments=arguments)

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
        "reviewed_work_authorship": reviewed_work_authorship.as_dict(),
        "note": note,
        "whose_move": "YOURS — your thesis is owed; the saved brief can be reused",
        "next_call": _saved_brief_thesis_call(
            session.session_id, session.session_type
        ),
    })

async def _handle_get_dialectic_session(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    View an active or past dialectic session.

    Args:
        session_id: Dialectic session ID (optional if agent_id provided)
        agent_id: Agent ID to find sessions for (optional if session_id provided)

    Returns:
        Session state with next-action guidance; fields=compact omits reconstruction bodies
    """
    def respond(payload: Dict[str, Any]) -> Sequence[TextContent]:
        if arguments.get("fields") == "compact":
            payload = compact_session_read(payload)
        return success_response(payload)

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
                    return respond(fast_result)

            # PG-first, fallback to in-memory cache
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
            
            # Check for timeouts if requested
            if check_timeout:
                # Check if reviewer is stuck FIRST — try re-assignment before failing
                if await check_reviewer_stuck(session):
                    from .responses import get_reviewer_reassigned_recovery, get_awaiting_facilitation_recovery
                    old_reviewer = session.reviewer_agent_id

                    # Attempt auto re-assignment
                    declaration = session.reviewed_work_authorship
                    exclude = [
                        session.paused_agent_id,
                        *declaration.author_agent_ids,
                    ]
                    if old_reviewer:
                        exclude.append(old_reviewer)
                    try:
                        new_reviewer = (
                            None
                            if declaration.is_invalid
                            else await select_reviewer(
                                paused_agent_id=session.paused_agent_id,
                                metadata=mcp_server.agent_metadata,
                                exclude_agent_ids=exclude,
                            )
                        )
                    except Exception:
                        new_reviewer = None

                    if new_reviewer:
                        try:
                            reassignment = await _apply_reviewer_reassignment(
                                session_id,
                                session,
                                new_reviewer,
                                reason="Previous reviewer did not respond within the antithesis timeout.",
                            )
                        except Exception as e:
                            logger.error(f"Failed to persist reviewer reassignment: {e}")
                            new_reviewer = None
                        else:
                            if not reassignment.get("persisted"):
                                new_reviewer = None
                            else:
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
                                return respond(result)

                    # No replacement found — mark awaiting facilitation (not FAILED yet)
                    if not getattr(session, 'awaiting_facilitation', False):
                        facilitation_reason = (
                            f"Reviewer '{old_reviewer}' stuck. "
                            "No auto-replacement available. "
                            "Awaiting human facilitation."
                        )
                        facilitation_msg = DialecticMessage(
                            phase=session.phase.value,
                            agent_id="system",
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            reasoning=(
                                f"Reviewer '{old_reviewer}' stuck. "
                                "No auto-replacement available. "
                                "Awaiting human facilitation."
                            ),
                        )
                        try:
                            await _append_protocol_message(
                                session,
                                expected_phase=session.phase.value,
                                expected_reviewer=old_reviewer,
                                expected_transcript_revision=int(
                                    session.transcript_revision
                                ),
                                next_phase=session.phase.value,
                                next_synthesis_round=session.synthesis_round,
                                next_awaiting_facilitation=True,
                                agent_id="system",
                                message_type="system",
                                reasoning=facilitation_reason,
                            )
                        except _TranscriptAppendRefused:
                            ACTIVE_SESSIONS.pop(session_id, None)
                            return [error_response(
                                "The dialectic changed before facilitation could be recorded.",
                                error_code="DIALECTIC_TRANSCRIPT_CHANGED",
                                error_category="governance_refusal",
                                recovery={
                                    "action": "Reload the session before checking its timeout again.",
                                    "related_tools": ["dialectic"],
                                },
                                arguments=arguments,
                            )]
                        except Exception as e:
                            logger.error(
                                "Failed to persist facilitation status: %s",
                                type(e).__name__,
                            )
                            return [error_response(
                                "Could not persist the facilitation request; it was not acknowledged.",
                                error_code="DIALECTIC_MESSAGE_WRITE_FAILED",
                                error_category="system_error",
                                arguments=arguments,
                            )]
                        session.awaiting_facilitation = True
                        session.transcript.append(facilitation_msg)
                        logger.info(f"[DIALECTIC] Session {session_id[:16]} awaiting human facilitation (reviewer {old_reviewer} stuck)")
                        await _emit_dialectic_event(
                            "dialectic_facilitation_needed", session, reason="reviewer_stuck"
                        )

                    return respond({
                        "success": False,
                        "error": "Reviewer stuck - awaiting human facilitation",
                        "awaiting_facilitation": True,
                        "session": session.to_dict(),
                        "recovery": get_awaiting_facilitation_recovery(session_id),
                    })

                # General timeout check (non-reviewer timeouts: synthesis, total time)
                timeout_reason = session.check_timeout()
                if timeout_reason:
                    timeout_phase = session.phase.value
                    timeout_frontier = int(session.transcript_revision)
                    timeout_payload = {
                        "reason": timeout_reason,
                        "closed_by": "get_timeout_check",
                    }
                    try:
                        commit = await _commit_terminal_resolution(
                            session,
                            timeout_payload,
                            status=DialecticPhase.FAILED.value,
                            expected_transcript_revision=timeout_frontier,
                            expected_phase=timeout_phase,
                        )
                    except BeamDialecticPolicyRefusal as e:
                        logger.info(
                            "Timeout persistence deferred for %s: %s",
                            session_id[:16],
                            e.error_code,
                        )
                        result = session.to_dict()
                        result["success"] = True
                        result["timeout_persistence_deferred"] = e.error_code
                        attach_attestation(result)
                        await _attach_public_resolution_effect(result, session_id)
                        result.update(_build_dialectic_actionability(result))
                        return respond(result)
                    except Exception as e:
                        logger.error(
                            "Failed to persist timeout to PostgreSQL: %s",
                            type(e).__name__,
                        )
                        ACTIVE_SESSIONS.pop(session_id, None)
                        return [error_response(
                            "Could not persist the timeout; no terminal outcome was recorded.",
                            error_code="SESSION_RESOLUTION_WRITE_FAILED",
                            error_category="system_error",
                            arguments=arguments,
                        )]
                    if commit.commit_unknown:
                        ACTIVE_SESSIONS.pop(session_id, None)
                        return [error_response(
                            "The timeout write outcome is unknown; reload before continuing.",
                            error_code="SESSION_RESOLUTION_COMMIT_UNKNOWN",
                            error_category="system_error",
                            details={
                                "session_id": session_id,
                                "commit_origin": commit.origin,
                                "terminal_committed": None,
                            },
                            arguments=arguments,
                        )]
                    if not commit.terminal_committed:
                        ACTIVE_SESSIONS.pop(session_id, None)
                        return [error_response(
                            "The dialectic changed before the timeout could be committed.",
                            error_code="DIALECTIC_TRANSCRIPT_CHANGED",
                            error_category="governance_refusal",
                            recovery={
                                "action": "Reload the session before checking its timeout again.",
                                "related_tools": ["dialectic"],
                            },
                            arguments=arguments,
                        )]
                    session.phase = DialecticPhase.FAILED
                    session.awaiting_facilitation = False
                    return respond({
                        "success": False,
                        "error": timeout_reason,
                        "session": attach_attestation(session.to_dict()),
                        "recovery": get_session_timeout_recovery(timeout_reason),
                    })

            result = session.to_dict()
            result["success"] = True
            # Same descriptor the PostgreSQL fast path adds. Without it, whether a
            # caller sees `attestation` depended on check_timeout, so the same
            # resolved session answered two ways through the same tool.
            attach_attestation(result)
            await _attach_public_resolution_effect(result, session_id)
            result.update(_build_dialectic_actionability(result))
            return respond(result)
        
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

            for matched in matching_sessions:
                matched["reviewed_work_authorship"] = (
                    read_reviewed_work_authorship(matched).as_dict()
                )

            if not matching_sessions:
                return [error_response(
                    f"No dialectic sessions found for agent '{agent_id}'",
                    recovery=no_sessions_found_recovery(),
                )]

            # If single session, return it directly
            if len(matching_sessions) == 1:
                result = matching_sessions[0]
                result["success"] = True
                attach_attestation(result)
                return respond(result)

            # Multiple sessions - return list
            return respond({
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

#: Characters of a session topic a list row shows before it is cut.


async def _handle_list_dialectic_sessions(arguments: Dict[str, Any]) -> Sequence[TextContent]:
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
    try:
        agent_id = arguments.get('agent_id')
        status = arguments.get('status')
        try:
            limit = parse_limit(arguments.get('limit'), default=50, maximum=200)
        except LimitError as exc:
            return [error_response(
                str(exc),
                recovery={
                    "action": (
                        "Retry with limit between 1 and 200, "
                        "or omit limit to use the default of 50"
                    ),
                    "related_tools": ["dialectic"],
                },
            )]
        include_transcript = coerce_bool(
            arguments.get('include_transcript'), default=False
        )
        # Opt-in projection for callers that only tally outcomes. The dashboard's
        # Overview card reads phase/status off 50 sessions to show "N open" and
        # "M of 50 recent failed", and nothing else — measured 130,936 B to
        # compute two integers that need 1,114 B, 99.1% discarded, on the DEFAULT
        # page on every load. `resolution` alone is ~629 B/session and is the
        # bulk of it.
        #
        # A WHITELIST, so a field added upstream cannot silently re-inflate a
        # polled response, and a strict SUBSET of the full shape, so one client
        # parser handles either and a server without the parameter simply
        # returns everything.
        compact = str(arguments.get('fields') or '').strip().lower() == 'compact'

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
                    "limit": limit,
                    "include_transcript": include_transcript
                },
                "tip": "Use dialectic(action='list') with no filters to see all sessions"
            })

        if compact:
            keep = (
                "session_id",
                "phase",
                "status",
                "created",
                "paused_agent_label",
                "resolution_effect_state",
            )
            sessions = [
                {k: sess[k] for k in keep if k in sess}
                for sess in sessions
            ]
        elif not include_transcript:
            # A list row carries a preview of the topic, not the whole issue
            # description: topics run to ~12 KB, and 50 untruncated rows made
            # the default list 173 KB (measured 2026-09-25). get, or
            # include_transcript=true, still returns the full text.
            for sess in sessions:
                _preview_topic(sess)

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
                "related_tools": ["dialectic", "health_check"],
            }
        )]


async def _handle_submit_thesis(
    arguments: Dict[str, Any],
    *,
    mark_awaiting_facilitation_fn: Any = None,
) -> Sequence[TextContent]:
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
            arguments, enforce_session_ownership=True, require_bound_caller=True,
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

        root_cause = arguments.get('root_cause')
        reasoning = arguments.get('reasoning')
        reused_session_brief = False
        if coerce_bool(arguments.get("use_brief_as_thesis"), default=False):
            saved_brief = getattr(session, "topic", None) or getattr(
                session, "reason", None
            )
            if saved_brief:
                root_cause = root_cause or saved_brief
                reasoning = reasoning or saved_brief
                reused_session_brief = True

        # Create thesis message
        message = DialecticMessage(
            phase="thesis",
            agent_id=agent_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            root_cause=root_cause,
            proposed_conditions=proposed_conditions,
            reasoning=reasoning,
        )

        # Submit to session
        _prev_phase = session.phase.value
        _prev_reviewer_id = session.reviewer_agent_id
        _prev_synthesis_round = session.synthesis_round
        _prev_awaiting_facilitation = bool(session.awaiting_facilitation)
        _prev_transcript_length = len(session.transcript)
        _prev_transcript_revision = int(
            getattr(session, "transcript_revision", 0)
        )
        result = session.submit_thesis(message, api_key)
        if reused_session_brief:
            result["thesis_source"] = "saved_session_brief"

        if result["success"]:
            result["next_step"] = next_step_submit_antithesis(session.reviewer_agent_id)
            _clear_stale_facilitation = bool(
                session.awaiting_facilitation
                and session.reviewer_agent_id is None
            )

            # Persist the message against the exact protocol state we loaded.
            # Another process may hold a different in-process lock, so this
            # durable frontier is the concurrency boundary.
            try:
                await _append_protocol_message(
                    session,
                    expected_phase=_prev_phase,
                    expected_reviewer=_prev_reviewer_id,
                    expected_transcript_revision=_prev_transcript_revision,
                    next_phase=session.phase.value,
                    next_synthesis_round=session.synthesis_round,
                    next_awaiting_facilitation=(
                        False if _clear_stale_facilitation else None
                    ),
                    agent_id=agent_id,
                    message_type="thesis",
                    root_cause=root_cause,
                    proposed_conditions=proposed_conditions,
                    reasoning=reasoning,
                )
            except _TranscriptAppendRefused:
                session.phase = DialecticPhase(_prev_phase)
                session.reviewer_agent_id = _prev_reviewer_id
                session.synthesis_round = _prev_synthesis_round
                session.transcript_revision = _prev_transcript_revision
                del session.transcript[_prev_transcript_length:]
                ACTIVE_SESSIONS.pop(session_id, None)
                return [error_response(
                    "The dialectic changed before the thesis could be recorded.",
                    error_code="DIALECTIC_TRANSCRIPT_CHANGED",
                    error_category="governance_refusal",
                    recovery={
                        "action": "Reload the session before submitting another thesis.",
                        "related_tools": ["dialectic"],
                    },
                    arguments=arguments,
                )]
            except Exception as e:
                session.phase = DialecticPhase(_prev_phase)
                session.reviewer_agent_id = _prev_reviewer_id
                session.synthesis_round = _prev_synthesis_round
                session.transcript_revision = _prev_transcript_revision
                del session.transcript[_prev_transcript_length:]
                logger.warning("Could not persist thesis: %s", type(e).__name__)
                return [error_response(
                    "Could not persist the thesis; no thesis was acknowledged.",
                    error_code="DIALECTIC_MESSAGE_WRITE_FAILED",
                    error_category="system_error",
                    arguments=arguments,
                )]

            # Live progress: thesis submitted -> phase advanced (-> antithesis).
            await _emit_phase_changed(session, _prev_phase)

            # Persist to JSON (export snapshot)
            try:
                await save_session(session, primary_already_synced=True)
            except Exception as e:
                logger.warning(f"Could not save session after thesis: {e}")

            # Older sessions may carry the creation-time facilitation flag from
            # before dispatch was attempted. A recorded thesis activates the
            # automated review paths below, so clear that stale request now and
            # set it again only if every eligible path actually fails.
            if _clear_stale_facilitation:
                session.awaiting_facilitation = False

            # Escalation tier (design b): when orchestrated review is enabled and
            # no live reviewer has claimed the slot, spawn an INDEPENDENT reviewer
            # process through the agent-orchestrator. That process onboards with its
            # own identity, claims the still-open slot via the multi-agent path, and
            # submits its verdict — so on success we must NOT also run the in-process
            # reviewer. ANY dispatch failure falls through to the in-process path
            # below (the orchestrator being down never breaks dialectic).
            review_path_failure_reason = "no_automated_reviewer"
            # Set when the spawned reviewer exits inside the fast-crash wait;
            # the in-process fallback below overwrites the failure reason.
            orchestrated_reviewer_exited = False
            if session.reviewer_agent_id is None:
                from .orchestrator_dispatch import (
                    orchestrated_review_enabled,
                    dispatch_orchestrated_review,
                    reviewer_crashed_fast,
                )
                from .recusal import detect_subject_matter_conflict, recusal_mode

                # RECUSAL, before any reviewer sees the thesis. A thesis whose
                # subject is the review system must not be auto-assigned to the
                # review system — the reviewer would be an interested party, and
                # asking it to notice that itself is the one thing self-review
                # reliably fails at (session def32eb2b4b2ce93, 2026-08-19).
                # Checked here rather than inside the reviewer for exactly that
                # reason: a routing rule runs before the conflict can bite.
                _recusal = detect_subject_matter_conflict(
                    reasoning,
                    root_cause,
                    getattr(session, "topic", None),
                    getattr(session, "reason", None),
                )
                if _recusal is not None:
                    result["recusal"] = _recusal.as_dict()
                    logger.info(
                        "dialectic recusal (%s): session=%s reason=%s",
                        recusal_mode(), session_id, _recusal.reason,
                    )
                    try:
                        from src.audit_db import append_audit_event_async
                        await append_audit_event_async({
                            "event_type": "dialectic_recusal",
                            "session_id": session_id,
                            "agent_id": session.paused_agent_id,
                            "details": {
                                **_recusal.as_dict(),
                                "mode": recusal_mode(),
                            },
                        })
                    except Exception as _e:  # never let the record break the flow
                        logger.warning("could not record recusal: %s", _e)

                if _recusal is not None and recusal_mode() == "enforce":
                    # Leave the thesis recorded and the slot open. A human, or a
                    # reviewer without a stake, decides. Deliberately does NOT
                    # fall through to the in-process synthetic reviewer — that is
                    # the same interested party by another route.
                    await _set_awaiting_facilitation(
                        session, True, reason="reviewer_recused"
                    )
                    result["awaiting_facilitation"] = True
                    result["note"] = (
                        "RECUSED: this thesis is about the review system, so the "
                        "review system is an interested party and was not "
                        "auto-assigned. The thesis is recorded and the reviewer "
                        "slot is open. Assign an uninvolved reviewer with "
                        "dialectic(action='reassign'), or facilitate as operator. "
                        f"Set {'UNITARES_DIALECTIC_RECUSAL'}=flag to review anyway."
                    )
                    return success_response(result)

                if orchestrated_review_enabled():
                    review_path_failure_reason = "reviewer_dispatch_failed"
                    situation_parts = []
                    if getattr(session, "topic", None):
                        situation_parts.append(f"Topic: {session.topic}")
                    if getattr(session, "reason", None):
                        situation_parts.append(f"Reason: {session.reason}")
                    dispatched = await dispatch_orchestrated_review(
                        session_id,
                        {
                            "root_cause": root_cause,
                            "proposed_conditions": proposed_conditions,
                            "reasoning": reasoning or "",
                            # Server-owned context and telemetry captured when the
                            # session was opened. The reviewer receives these
                            # separately from the paused agent's authored claims.
                            "situation": "\n".join(situation_parts),
                            "paused_agent_state": getattr(
                                session, "paused_agent_state", {}
                            ) or {},
                        },
                        session.paused_agent_id,
                    )
                    if dispatched:
                        execution_id_spawned = (
                            dispatched.get("execution_id")
                            or dispatched.get("agent_id")
                            or dispatched.get("id")
                        )
                        # Catch a FAST reviewer crash (bad import/url/etc, exits in
                        # <12s) and fall back to in-process inline so the session
                        # resolves now instead of stranding at antithesis. A success
                        # or still-running reviewer owns the slot → async path.
                        if not await reviewer_crashed_fast(execution_id_spawned):
                            result["orchestrated_review"] = True
                            result["reviewer_dispatch"] = {
                                "execution_id": execution_id_spawned,
                                "agent_id": execution_id_spawned,
                                "via": "agent-orchestrator",
                            }
                            # No facilitation reconcile here. The thesis append
                            # above already cleared any stale flag, so a
                            # compare-and-set against the pre-dispatch snapshot
                            # could not change state, only fail: it was refused
                            # whenever the session moved on during the wait,
                            # usually because the spawned reviewer had already
                            # claimed the slot (dialectic canary, 2026-10-18).
                            await _emit_dialectic_event(
                                "dialectic_reviewer_dispatched",
                                session,
                                orchestrator_execution_id=execution_id_spawned,
                                orchestrator_agent_id=execution_id_spawned,
                                effect_id=dispatched.get("effect_id"),
                                governed=bool(dispatched.get("governed")),
                            )
                            result["note"] = (
                                "Independent reviewer spawned via the agent-orchestrator; "
                                "it will claim the reviewer slot and submit its verdict. "
                                "Poll dialectic action='get' for the antithesis/synthesis."
                            )
                            return success_response(result)
                        # reviewer crashed fast → fall through to in-process
                        review_path_failure_reason = "reviewer_crashed_fast"
                        orchestrated_reviewer_exited = True
                    # dispatch failed → fall through to in-process synthetic reviewer

            # End-to-end completion: when no independent live reviewer has claimed
            # the slot, drive the dialectic to a resolved synthesis with the local
            # synthetic reviewer instead of stranding it at awaiting_facilitation.
            # This is the one path that completes thesis → antithesis → synthesis
            # without a second live agent. Bounded so an overrun degrades to the
            # prior await-facilitation behaviour (thesis stays recorded above).
            # With a reviewer host list (or one the server could not resolve),
            # only a listed host may approve; the orchestrated child enforces
            # that, and the in-process fallback must too, or a dispatch failure
            # would hand approval to the local reviewer.
            fallback_block, fallback_detail = _reviewer_fallback_block()
            profile_block, profile_name = _reviewer_profile_block(review_path_failure_reason)
            if profile_block:
                fallback_block, fallback_detail = profile_block, profile_name
            if session.reviewer_agent_id is None and fallback_block:
                review_path_failure_reason = fallback_block
                if fallback_block == "reviewer_hosts_unresolved":
                    result["reviewer_hosts_error"] = fallback_detail
                elif profile_block:
                    result["reviewer_profile"] = fallback_detail
                else:
                    result["reviewer_hosts"] = fallback_detail
            if (
                session.reviewer_agent_id is None
                and _synthetic_reviewer_enabled()
                and not fallback_block
            ):
                review_path_failure_reason = "synthetic_reviewer_unavailable"
                thesis_dict = {
                    "root_cause": root_cause,
                    "proposed_conditions": proposed_conditions,
                    "reasoning": reasoning or "",
                }
                agent_state = _snapshot_agent_state(agent_id)
                synthetic_abort_reason = None
                try:
                    review = await asyncio.wait_for(
                        _run_synthetic_review(session, thesis_dict, agent_id, agent_state),
                        timeout=_synthetic_review_budget(),
                    )
                except asyncio.TimeoutError:
                    synthetic_abort_reason = "synthetic_reviewer_timeout"
                    logger.warning(
                        "[DIALECTIC] Synthetic review exceeded budget; session "
                        "%s left awaiting facilitation", session_id,
                    )
                    review = None
                except Exception as e:
                    synthetic_abort_reason = "synthetic_reviewer_failed"
                    logger.warning("[DIALECTIC] Synthetic review failed: %s", e)
                    review = None

                if review is None and synthetic_abort_reason == "synthetic_reviewer_timeout":
                    # wait_for cancellation can race an executor-backed BEAM
                    # request that commits after the coroutine exits. Re-read
                    # first, then mark only that same live phase/reviewer with
                    # a guarded CAS. Every terminal writer clears the flag in
                    # its own statement, so a late commit cannot strand a
                    # resolved row as still awaiting a human.
                    try:
                        persisted = await load_session_as_dict(session_id)
                    except Exception as exc:
                        logger.warning(
                            "Could not reload timed-out synthetic review %s: %s",
                            session_id[:16],
                            type(exc).__name__,
                        )
                        persisted = None
                    persisted_phase = str(
                        (persisted or {}).get("phase") or ""
                    ).lower()
                    persisted_reviewer = (persisted or {}).get("reviewer")
                    readback = _read_terminal_readback(persisted)
                    terminal = readback.terminal
                    facilitation_written = False
                    if (
                        persisted
                        and not terminal
                        and persisted_phase
                        and persisted_reviewer in {None, SYNTHETIC_REVIEWER_ID}
                    ):
                        try:
                            facilitation_written = bool(
                                await (mark_awaiting_facilitation_fn or pg_mark_awaiting_facilitation)(
                                    session_id,
                                    expected_phase=persisted_phase,
                                    expected_reviewer=persisted_reviewer,
                                    expected_updated_at=persisted.get(
                                        "updated_at"
                                    ),
                                    expected_transcript_revision=persisted.get(
                                        "transcript_revision", 0
                                    ),
                                    expected_resolution_saga_revision=(
                                        persisted.get(
                                            "resolution_saga_revision", 0
                                        )
                                    ),
                                    exclude_inflight_saga=True,
                                )
                            )
                        except Exception as exc:
                            logger.warning(
                                "Could not guard facilitation after synthetic "
                                "timeout for %s: %s",
                                session_id[:16],
                                type(exc).__name__,
                            )
                        if facilitation_written:
                            session.reviewer_agent_id = persisted_reviewer
                            session.awaiting_facilitation = True
                            await _emit_dialectic_event(
                                "dialectic_facilitation_needed",
                                session,
                                reason=synthetic_abort_reason,
                            )

                    terminal_next_step = _terminal_readback_next_step(
                        readback, session_id
                    )
                    if terminal_next_step:
                        next_step = terminal_next_step
                    elif facilitation_written:
                        next_step = (
                            "The timed-out review is marked for facilitation. "
                            "Assign an independent reviewer or facilitate the "
                            "recorded partial review as operator."
                        )
                    else:
                        next_step = (
                            "Reload this session before acting. Do not submit "
                            "another antithesis while the timed-out write may "
                            "still finish."
                        )
                    result.update({
                        "synthetic_review": True,
                        "reviewer_agent_id": persisted_reviewer,
                        "awaiting_facilitation": facilitation_written,
                        "terminal_state_unknown": not terminal
                        and not facilitation_written,
                        "next_step": next_step,
                    })
                    if result["terminal_state_unknown"]:
                        # A lost readback/CAS proves no durable state at all,
                        # so it is not a successful operation either.
                        result["success"] = False
                    _apply_terminal_readback(
                        result,
                        readback,
                        default_failure_reason=(
                            "The synthetic review committed a terminal failure."
                        ),
                    )
                    return success_response(result)

                # The coroutine may have persisted its reviewer claim before a
                # transcript, phase, or synthesis write failed (or before
                # wait_for cancelled it). That synthetic identity has no
                # resident process to continue later. Mark the claimed session
                # for facilitation explicitly instead of relying on the open-
                # slot branch below, which correctly does not fire once the
                # reviewer column is non-NULL.
                if (
                    review is None
                    and synthetic_abort_reason
                    and session.reviewer_agent_id == SYNTHETIC_REVIEWER_ID
                ):
                    await _set_awaiting_facilitation(
                        session, True, reason=synthetic_abort_reason
                    )
                    result["reviewer_agent_id"] = SYNTHETIC_REVIEWER_ID
                    result["awaiting_facilitation"] = True
                    result["note"] = (
                        "The synthetic reviewer claimed the session but did not "
                        "finish. Assign an independent reviewer or facilitate "
                        "the recorded partial review as operator."
                    )

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
                    if review.get("terminal_committed"):
                        result["terminal_committed"] = True
                    if review.get("effect_pending"):
                        # The thesis itself persisted, but the requested
                        # terminal outcome has not been established.  Match the
                        # one-shot dialectic contract: a commit-unknown or
                        # unapplied durable effect is never a successful
                        # operation envelope.
                        result["success"] = False
                        result["effect_pending"] = True
                    if review.get("commit_unknown"):
                        result["commit_unknown"] = True
                    if review.get("effect_pending"):
                        result["next_step"] = (
                            "Reload this session. If the terminal decision committed, "
                            "an operator should run dialectic(action='reconcile_effect', "
                            f"session_id='{session_id}')."
                        )
                        result["next_steps"] = [
                            "Do not submit another antithesis or synthesis while the "
                            "terminal/effect state is being reconciled."
                        ]
                    elif review.get("hard_limit_blocked"):
                        result["success"] = False
                        result["action"] = "block"
                        result["reason"] = (
                            "Safety violation: "
                            f"{review.get('hard_limit_violation')}"
                        )
                        result["next_step"] = (
                            "The requested resume was blocked by a hard limit; "
                            "review the recorded terminal decision."
                        )
                    elif review.get("resolved"):
                        result["next_step"] = (
                            f"Dialectic resolved by synthetic reviewer. "
                            f"Recommendation: {review.get('recommendation')}. "
                            "Review the merged_conditions, then proceed or self_recovery accordingly."
                        )
                        result["next_steps"] = _get_dialectic_next_steps(
                            (review.get("recommendation") or "ESCALATE")
                        )
                    else:
                        # The in-process reviewer has no resident continuation
                        # identity. A rejection or incomplete synthesis therefore
                        # genuinely requires a peer/operator, unlike the open slot
                        # that existed before review was attempted.
                        await _set_awaiting_facilitation(
                            session,
                            True,
                            reason="synthetic_reviewer_did_not_approve",
                        )
                        result["awaiting_facilitation"] = True

            # No reviewer claimed the slot and every enabled automatic path
            # declined, crashed, timed out, or was disabled. This is the first
            # point at which a human-facilitation request is truthful.
            if session.reviewer_agent_id is None:
                try:
                    await _set_awaiting_facilitation(
                        session, True, reason=review_path_failure_reason
                    )
                except _TranscriptAppendRefused:
                    # The local snapshot says nobody claimed the slot, but the
                    # durable row moved on: typically an orchestrated reviewer
                    # that claimed it and then exited inside the fast-crash
                    # wait, or one spawned by a dispatch call that timed out
                    # after the spawn (#2905). The thesis is recorded either way.
                    return await _thesis_recorded_after_facilitation_refused(
                        session,
                        result,
                        spawned_reviewer_exited=orchestrated_reviewer_exited,
                    )
                result["awaiting_facilitation"] = True
                result["note"] = (
                    "The thesis is recorded, but no automated reviewer could take "
                    "the open slot. Assign an independent reviewer or facilitate "
                    "this session as operator."
                )

        return success_response(result)

    except Exception as e:
        return [error_response(f"Error submitting thesis: {str(e)}")]

# register=False: this name is a `dialectic` alias, and resolve_alias rewrites it
# before handler lookup -- see the "one name, one home" note in tool_stability.py.
async def _handle_submit_antithesis(arguments: Dict[str, Any]) -> Sequence[TextContent]:
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
            arguments, enforce_session_ownership=True, require_bound_caller=True,
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

        # This must sit outside the best-effort active-session/cooldown block
        # below.  A declared work author is a hard conflict, not an eligibility
        # lookup that may fail open; reject before the protocol can claim the
        # reviewer slot or append a message.
        authorship_conflict = _reviewer_authorship_conflict(session, agent_id)
        if authorship_conflict:
            error_code, message = authorship_conflict
            return [error_response(
                message,
                error_code=error_code,
                error_category="governance_refusal",
                recovery={
                    "action": "Use an independent reviewer who did not author the reviewed work.",
                    "related_tools": ["dialectic", "request_review"],
                },
                arguments=arguments,
            )]

        judgment_formed = _judgment_was_formed(arguments.get("judgment_formed"))

        # Validate every deterministic antithesis precondition before an
        # operator takeover can durably transfer reviewer authority. The
        # reassignment and antithesis are separate audited transcript writes;
        # avoid committing the first when the second is already known to fail.
        if session.phase != DialecticPhase.ANTITHESIS:
            return success_response({
                "success": False,
                "session_id": session_id,
                "error": f"Cannot submit antithesis in phase {session.phase.value}",
            })
        original_reviewer_id = session.reviewer_agent_id
        reviewer_takeover = None

        if session.reviewer_agent_id and agent_id != session.reviewer_agent_id:
            if not take_over_if_requested:
                workflow = (
                    "Wait for the assigned reviewer to answer, or ask an "
                    "authenticated operator/current reviewer to use "
                    "dialectic(action='reassign', session_id='...', new_reviewer_id='...')."
                )
                if agent_id != session.paused_agent_id:
                    workflow = (
                        "Ask an authenticated operator to retry with "
                        "take_over_if_requested=true, or ask the current reviewer/operator "
                        "to use dialectic(action='reassign', session_id='...', "
                        "new_reviewer_id='...')."
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

            # ``take_over_if_requested`` is an in-one-call reassignment alias.
            # Identity binding alone must not let an unrelated agent seize an
            # occupied reviewer slot, so require the same operator credential as
            # an operator-initiated explicit reassignment.
            if _reviewer_reassignment_actor(session) != "operator":
                return [error_response(
                    "Reviewer takeover requires an authenticated operator.",
                    error_code="TAKEOVER_FORBIDDEN",
                    error_category="auth_error",
                    recovery={
                        "action": (
                            "Wait for the assigned reviewer, or ask an authenticated "
                            "operator/current reviewer to reassign the session."
                        ),
                        "related_tools": ["dialectic", "identity"],
                    },
                    arguments=arguments,
                )]

            validation_error = await _validate_explicit_reviewer_candidate(session, agent_id)
            if validation_error:
                return validation_error

            if judgment_formed:
                if not str(arguments.get("reasoning") or "").strip():
                    return success_response({
                        "success": False,
                        "session_id": session_id,
                        "error": (
                            "Antithesis requires reasoning: provide your "
                            "counter-perspective or observations"
                        ),
                    })
                try:
                    reviewer_takeover = await _apply_reviewer_reassignment(
                        session_id,
                        session,
                        agent_id,
                        reason=takeover_reason,
                        strict_persistence=True,
                    )
                except BeamDialecticPolicyRefusal as e:
                    error_code = _beam_policy_error_code(e)
                    return [error_response(
                        str(e),
                        error_code=error_code,
                        error_category="governance_refusal",
                        arguments=arguments,
                    )]
                except Exception as e:
                    return [error_response(f"Reviewer takeover failed during persistence: {e}")]

        # The paused agent may answer an assigned reviewer's objection during
        # synthesis, but it cannot become the reviewer merely by declaring an
        # abstention. Preserve the same self-review boundary as the normal
        # submit_antithesis path before recording any abstention event.
        if (
            not judgment_formed
            and session.reviewer_agent_id is None
            and agent_id == session.paused_agent_id
        ):
            return [error_response(
                "Requestor cannot review their own session (use reviewer_mode='self' for self-review)",
            )]

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

        if judgment_formed and not str(arguments.get("reasoning") or "").strip():
            return success_response({
                "success": False,
                "session_id": session_id,
                "error": (
                    "Antithesis requires reasoning: provide your "
                    "counter-perspective or observations"
                ),
            })

        # ── A NON-JUDGMENT IS NOT A VERDICT ──────────────────────────────────
        # Run the normal ownership and eligibility gates first, but return
        # before session.submit_antithesis so no abstention can claim a slot or
        # advance the phase. A reviewer that could not form a judgment has not
        # reviewed anything, and filing on its behalf records a binding
        # rejection with empty reasoning.
        if not judgment_formed:
            reviewer_slot_open = session.reviewer_agent_id is None
            abstention_recorded = await emit_reviewer_abstained(
                session_id=session_id,
                reviewer_agent_id=agent_id,
                paused_agent_id=session.paused_agent_id,
                phase=session.phase.value,
                reviewer_backend=_merge_caller_reviewer_provenance(
                    arguments.get("observed_metrics"),
                    arguments.get("reviewer_provenance"),
                ).get("reviewer_backend"),
                reason="no_judgment_formed",
            )
            if abstention_recorded is False:
                return [error_response(
                    "Could not record the abstention audit event; no abstention was acknowledged",
                    error_code="ABSTENTION_AUDIT_WRITE_FAILED",
                )]
            return success_response({
                "abstained": True,
                "session_id": session_id,
                "reviewer_slot_claimed": False,
                "reviewer_slot_open": reviewer_slot_open,
                "phase": session.phase.value,
                "message": (
                    "Recorded an abstention: no judgment was formed, so no verdict "
                    + (
                        "was filed and the reviewer slot remains OPEN."
                        if reviewer_slot_open
                        else "was filed; the existing reviewer assignment remains unchanged."
                    )
                ),
                "next_step": (
                    (
                        "A reviewer that can judge may still claim this session."
                        if reviewer_slot_open
                        else "The assigned reviewer or an authenticated operator must "
                        "reassign this session before another reviewer can claim it."
                    )
                    + " The abstention is on the audit stream as "
                    "dialectic_reviewer_abstained; it is not a rejection and "
                    "does not count as a review round."
                ),
            })

        # Create antithesis message. An explicit reviewer_provenance argument
        # (e.g. an external Codex/other-model consult being filed as a governed
        # verdict) is folded into observed_metrics["reviewer_backend"].
        anti_observed_metrics = _merge_caller_reviewer_provenance(
            arguments.get('observed_metrics'),
            arguments.get('reviewer_provenance'),
        )
        message = DialecticMessage(
            phase="antithesis",
            agent_id=agent_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            observed_metrics=anti_observed_metrics,
            concerns=arguments.get('concerns', []),
            reasoning=arguments.get('reasoning')
        )

        # Submit to session
        _prev_phase = session.phase.value
        _prev_reviewer_id = session.reviewer_agent_id
        _prev_synthesis_round = session.synthesis_round
        _prev_awaiting_facilitation = bool(session.awaiting_facilitation)
        _prev_transcript_length = len(session.transcript)
        _prev_transcript_revision = int(
            getattr(session, "transcript_revision", 0)
        )
        result = session.submit_antithesis(message, api_key)

        def _restore_pre_antithesis_state() -> None:
            session.reviewer_agent_id = _prev_reviewer_id
            session.phase = DialecticPhase(_prev_phase)
            session.synthesis_round = _prev_synthesis_round
            session.awaiting_facilitation = _prev_awaiting_facilitation
            session.transcript_revision = _prev_transcript_revision
            del session.transcript[_prev_transcript_length:]

        if not result["success"]:
            _restore_pre_antithesis_state()
            if reviewer_takeover:
                # The takeover is already durable even though the second
                # transcript write was refused. Never report this as a
                # mutation-free antithesis failure.
                result["reviewer_reassigned"] = True
                result["reviewer_takeover"] = reviewer_takeover

        if result["success"]:
            result["next_step"] = next_step_negotiate_synthesis()

            if reviewer_takeover:
                result["reviewer_takeover"] = reviewer_takeover

            # Persist against the exact transcript frontier loaded above. A
            # real reviewer message also answers any standing facilitation
            # request in the same transaction; a later writer cannot have its
            # newer request erased by a separate clear.
            _clear_facilitation = bool(
                session.awaiting_facilitation
                and session.reviewer_agent_id
                and session.reviewer_agent_id != session.paused_agent_id
            )
            try:
                await _append_protocol_message(
                    session,
                    expected_phase=_prev_phase,
                    expected_reviewer=_prev_reviewer_id,
                    expected_transcript_revision=_prev_transcript_revision,
                    expected_awaiting_facilitation=(
                        _prev_awaiting_facilitation
                    ),
                    next_phase=session.phase.value,
                    next_synthesis_round=session.synthesis_round,
                    next_reviewer=agent_id,
                    update_reviewer=(
                        original_reviewer_id is None
                        and session.reviewer_agent_id == agent_id
                    ),
                    next_awaiting_facilitation=(
                        False if _clear_facilitation else None
                    ),
                    agent_id=agent_id,
                    message_type="antithesis",
                    observed_metrics=anti_observed_metrics,
                    concerns=arguments.get('concerns', []),
                    reasoning=arguments.get('reasoning'),
                )
            except _TranscriptAppendRefused:
                _restore_pre_antithesis_state()
                ACTIVE_SESSIONS.pop(session_id, None)
                return [error_response(
                    "The dialectic changed before the antithesis could be recorded.",
                    details=(
                        {
                            "reviewer_reassigned": True,
                            "reviewer_takeover": reviewer_takeover,
                        }
                        if reviewer_takeover
                        else None
                    ),
                    error_code="DIALECTIC_TRANSCRIPT_CHANGED",
                    error_category="governance_refusal",
                    recovery={
                        "action": (
                            "Reload the session; reviewer authority changed, "
                            "but the antithesis was not recorded. Submit again "
                            "as the new reviewer."
                            if reviewer_takeover
                            else "Reload the session before submitting another antithesis."
                        ),
                        "related_tools": ["dialectic"],
                    },
                    arguments=arguments,
                )]
            except Exception as e:
                _restore_pre_antithesis_state()
                logger.warning(
                    "Could not persist antithesis: %s", type(e).__name__
                )
                return [error_response(
                    "Could not persist the antithesis; no antithesis was acknowledged.",
                    details=(
                        {
                            "reviewer_reassigned": True,
                            "reviewer_takeover": reviewer_takeover,
                        }
                        if reviewer_takeover
                        else None
                    ),
                    error_code="DIALECTIC_MESSAGE_WRITE_FAILED",
                    error_category="system_error",
                    recovery=(
                        {
                            "action": (
                                "Reload the session; reviewer authority changed, "
                                "but the antithesis was not recorded. Submit again "
                                "as the new reviewer."
                            ),
                            "related_tools": ["dialectic"],
                        }
                        if reviewer_takeover
                        else None
                    ),
                    arguments=arguments,
                )]

            if _clear_facilitation:
                session.awaiting_facilitation = False
            if original_reviewer_id is None and session.reviewer_agent_id == agent_id:
                result["reviewer_auto_assigned"] = True
                logger.info("Reviewer auto-assigned with antithesis")

            # Live progress: antithesis submitted -> phase advanced (-> synthesis).
            await _emit_phase_changed(session, _prev_phase)

            # Persist to JSON
            try:
                await save_session(session, primary_already_synced=True)
            except Exception as e:
                logger.warning(f"Could not save session after antithesis: {e}")

        return success_response(result)

    except Exception as e:
        durable_takeover = locals().get("reviewer_takeover")
        return [error_response(
            f"Error submitting antithesis: {str(e)}",
            details=(
                {
                    "reviewer_reassigned": True,
                    "reviewer_takeover": durable_takeover,
                }
                if durable_takeover
                else None
            ),
        )]


async def _handle_submit_synthesis(arguments: Dict[str, Any]) -> Sequence[TextContent]:
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

        # Use same identity pipeline as onboard/identity (consistent UUID). A
        # synthesis can converge the session and release a pause, so the caller
        # must be bound by identity resolution and submitting as itself.
        agent_id, agent_error = await _resolve_dialectic_agent_id(
            arguments, require_bound_caller=True,
        )
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

            eligibility_error = await _validate_synthesis_participant_and_conflict(
                session, agent_id, arguments
            )
            if eligibility_error:
                return eligibility_error

            if (
                not _judgment_was_formed(arguments.get("judgment_formed"))
                and session.phase != DialecticPhase.SYNTHESIS
            ):
                return success_response({
                    "success": False,
                    "session_id": session_id,
                    "error": f"Cannot submit synthesis in phase {session.phase.value}",
                })

            if not _judgment_was_formed(arguments.get("judgment_formed")):
                return await _record_synthesis_abstention(
                    session, session_id, agent_id, arguments
                )

            agrees, proposed_conditions, proposal_error = _parse_synthesis_proposal(
                session, arguments
            )
            if proposal_error:
                return proposal_error

            # Submit to the in-memory protocol, but retain the exact loaded
            # state so a refused durable append can be rolled back truthfully.
            _prev_phase = session.phase.value
            _prev_reviewer_id = session.reviewer_agent_id
            _prev_synthesis_round = session.synthesis_round
            _prev_awaiting_facilitation = bool(
                session.awaiting_facilitation
            )
            _prev_transcript_length = len(session.transcript)
            _prev_transcript_revision = int(
                getattr(session, "transcript_revision", 0)
            )

            def _restore_pre_synthesis_state() -> None:
                session.phase = DialecticPhase(_prev_phase)
                session.reviewer_agent_id = _prev_reviewer_id
                session.synthesis_round = _prev_synthesis_round
                session.awaiting_facilitation = (
                    _prev_awaiting_facilitation
                )
                session.transcript_revision = _prev_transcript_revision
                session.resolution = None
                del session.transcript[_prev_transcript_length:]

            result, persist_error = await _submit_and_persist_synthesis_message(
                session,
                session_id,
                agent_id,
                api_key,
                agrees,
                proposed_conditions,
                arguments,
                prev_phase=_prev_phase,
                prev_reviewer_id=_prev_reviewer_id,
                prev_transcript_revision=_prev_transcript_revision,
                prev_awaiting_facilitation=_prev_awaiting_facilitation,
                restore_pre_synthesis_state=_restore_pre_synthesis_state,
            )
            if persist_error:
                return persist_error

            # If converged, finalize resolution
            if result.get("success") and result.get("converged"):
                converged_error = await _finalize_converged_synthesis(
                    session, session_id, result, arguments
                )
                if converged_error:
                    return converged_error

            elif result.get("action") == "failed_max_rounds":
                max_rounds_error = await _finalize_max_rounds_synthesis_failure(
                    session,
                    session_id,
                    result,
                    arguments,
                    prev_phase=_prev_phase,
                    prev_transcript_revision=_prev_transcript_revision,
                    restore_pre_synthesis_state=_restore_pre_synthesis_state,
                )
                if max_rounds_error:
                    return max_rounds_error

            # A refusal must not reach the caller inside a success envelope. The
            # blocked path deliberately returns success=True from the protocol layer so
            # the attempt still gets persisted above (it is the audit signal), but any
            # caller keying on `success` would otherwise read a governance refusal as
            # convergence.
            if result.get("blocked") == "reviewer_objection_stands":
                return _reviewer_objection_stands_refusal(session_id, result)

            return success_response(result)

    except Exception as e:
        return [error_response(f"Error submitting synthesis: {str(e)}")]

