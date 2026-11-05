"""Resolution, reviewer reassignment, and synthesis finalization helpers for Dialectic handlers."""

from typing import Dict, Any, Sequence, Optional, Tuple
from mcp.types import TextContent
import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone

from src.dialectic_protocol import (
    DialecticSession,
    DialecticMessage,
    DialecticPhase,
)
from ..utils import success_response, error_response
from .responses import (
    get_agent_not_found_recovery,
    missing_session_id_recovery,
    next_step_no_consensus,
    next_step_resume_not_applied,
    next_step_resumed,
    session_not_found_recovery,
)
from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as _default_mcp_server
from .session import (
    discard_receipt_unless_written as _default_discard_receipt_unless_written,
    seal_resolution_for_persistence,
    save_session as _default_save_session,
    load_session as _default_load_session,
    ACTIVE_SESSIONS as _default_ACTIVE_SESSIONS,
    _SESSION_METADATA_CACHE as _default_SESSION_METADATA_CACHE,
)
from .calibration import (
    update_calibration_from_dialectic as _default_update_calibration_from_dialectic,
    update_calibration_from_dialectic_disagreement as _default_update_calibration_from_dialectic_disagreement,
)
from .resolution import (
    execute_resolution as _default_execute_resolution,
    record_resolution_outcome as _default_record_resolution_outcome,
)
from .beam_resolve_client import (
    BeamDialecticPolicyRefusal,
    beam_resolve as _default_beam_resolve,
)
from .reviewer import select_reviewer as _default_select_reviewer
from .events import (
    emit_participant_abstained as _default_emit_participant_abstained,
    emit_reviewer_abstained as _default_emit_reviewer_abstained,
    emit_reviewer_reassigned as _default_emit_reviewer_reassigned,
)
from src.dialectic_db import (
    add_message_async as _default_pg_add_message,
    resolve_session_async as _default_pg_resolve_session,
    claim_resolution_effect_async as _default_pg_claim_resolution_effect,
    complete_resolution_effect_async as _default_pg_complete_resolution_effect,
    fail_resolution_effect_async as _default_pg_fail_resolution_effect,
    get_resolution_effect_async as _default_pg_get_resolution_effect,
    acknowledge_resolution_effect_async as _default_pg_acknowledge_resolution_effect,
    quarantine_resolution_effect_async as _default_pg_quarantine_resolution_effect,
)
from .actionability import (
    _beam_policy_error_code,
    _beam_policy_recovery,
    _ResolutionCommitResult,
    _ResolutionEffectResult,
    _TranscriptAppendRefused,
    _meta_value,
    _reviewer_authorship_conflict,
    _merge_caller_reviewer_provenance,
    _terminal_commit_refusal_response,
    _resolution_effect_view,
    _parse_resolution_effect_updated_at,
    _resolution_effect_skips_resume_calibration,
)

logger = get_logger(__name__)


def _h_attr(name: str, default: Any = None) -> Any:
    mod = sys.modules.get("src.mcp_handlers.dialectic.handlers")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return default


def _h_call(name: str) -> Any:
    mod = sys.modules.get("src.mcp_handlers.dialectic.handlers")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return globals()[name]


class _McpServerProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(_h_attr("mcp_server", _default_mcp_server), name)


class _DictProxy:
    def __init__(self, attr_name: str, default_dict: dict) -> None:
        self._attr_name = attr_name
        self._default = default_dict

    def _d(self) -> dict:
        return _h_attr(self._attr_name, self._default)

    def __getitem__(self, key: Any) -> Any:
        return self._d()[key]

    def __setitem__(self, key: Any, value: Any) -> None:
        self._d()[key] = value

    def __delitem__(self, key: Any) -> None:
        del self._d()[key]

    def __contains__(self, key: Any) -> bool:
        return key in self._d()

    def __iter__(self):
        return iter(self._d())

    def __len__(self) -> int:
        return len(self._d())

    def __bool__(self) -> bool:
        return bool(self._d())

    def get(self, key: Any, default: Any = None) -> Any:
        return self._d().get(key, default)

    def pop(self, key: Any, *args: Any) -> Any:
        return self._d().pop(key, *args)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        return self._d().setdefault(key, default)

    def update(self, *args: Any, **kwargs: Any) -> None:
        self._d().update(*args, **kwargs)

    def clear(self) -> None:
        self._d().clear()

    def keys(self):
        return self._d().keys()

    def values(self):
        return self._d().values()

    def items(self):
        return self._d().items()


mcp_server = _McpServerProxy()
ACTIVE_SESSIONS = _DictProxy("ACTIVE_SESSIONS", _default_ACTIVE_SESSIONS)
_SESSION_METADATA_CACHE = _DictProxy("_SESSION_METADATA_CACHE", _default_SESSION_METADATA_CACHE)


def _proxy(name: str, default: Any = None):
    def _call(*args: Any, **kwargs: Any) -> Any:
        return _h_attr(name, default)(*args, **kwargs)
    return _call


discard_receipt_unless_written = _proxy("discard_receipt_unless_written", _default_discard_receipt_unless_written)
save_session = _proxy("save_session", _default_save_session)
load_session = _proxy("load_session", _default_load_session)
update_calibration_from_dialectic = _proxy("update_calibration_from_dialectic", _default_update_calibration_from_dialectic)
update_calibration_from_dialectic_disagreement = _proxy("update_calibration_from_dialectic_disagreement", _default_update_calibration_from_dialectic_disagreement)
execute_resolution = _proxy("execute_resolution", _default_execute_resolution)
record_resolution_outcome = _proxy("record_resolution_outcome", _default_record_resolution_outcome)
beam_resolve = _proxy("beam_resolve", _default_beam_resolve)
select_reviewer = _proxy("select_reviewer", _default_select_reviewer)
emit_participant_abstained = _proxy("emit_participant_abstained", _default_emit_participant_abstained)
emit_reviewer_abstained = _proxy("emit_reviewer_abstained", _default_emit_reviewer_abstained)
emit_reviewer_reassigned = _proxy("emit_reviewer_reassigned", _default_emit_reviewer_reassigned)
pg_add_message = _proxy("pg_add_message", _default_pg_add_message)
pg_resolve_session = _proxy("pg_resolve_session", _default_pg_resolve_session)
pg_claim_resolution_effect = _proxy("pg_claim_resolution_effect", _default_pg_claim_resolution_effect)
pg_complete_resolution_effect = _proxy("pg_complete_resolution_effect", _default_pg_complete_resolution_effect)
pg_fail_resolution_effect = _proxy("pg_fail_resolution_effect", _default_pg_fail_resolution_effect)
pg_get_resolution_effect = _proxy("pg_get_resolution_effect", _default_pg_get_resolution_effect)
pg_acknowledge_resolution_effect = _proxy("pg_acknowledge_resolution_effect", _default_pg_acknowledge_resolution_effect)
pg_quarantine_resolution_effect = _proxy("pg_quarantine_resolution_effect", _default_pg_quarantine_resolution_effect)
_append_protocol_message = _proxy("_append_protocol_message")
_commit_terminal_resolution = _proxy("_commit_terminal_resolution")
_emit_dialectic_event = _proxy("_emit_dialectic_event")
_set_awaiting_facilitation = _proxy("_set_awaiting_facilitation")
_precommit_resolution_authority_refusal = _proxy("_precommit_resolution_authority_refusal")
_self_review_requires_independent_authority = _proxy("_self_review_requires_independent_authority")
_read_proposed_conditions = _proxy("_read_proposed_conditions")
_refuse_self_review_convergence = _proxy("_refuse_self_review_convergence")
_record_operator_resolution_telemetry = _proxy("_record_operator_resolution_telemetry")
_emit_request_reviewer_reassigned = _proxy("_emit_request_reviewer_reassigned")
_attach_resolution_to_result = _proxy("_attach_resolution_to_result")


async def _commit_converged_resolution(
    session: DialecticSession,
    resolution: Resolution,
    *,
    expected_transcript_revision: int,
) -> _ResolutionCommitResult:
    """Validate the pause binding, then commit one resolved decision."""
    paused_state = getattr(session, "paused_agent_state", {}) or {}
    binding_present = "pause_effect_required" in paused_state
    effect_required = paused_state.get("pause_effect_required")
    pause_token = paused_state.get("pause_token")
    if binding_present and not isinstance(effect_required, bool):
        return _ResolutionCommitResult(
            False, False, "pause_effect_binding_invalid"
        )
    if not binding_present:
        # A pre-protocol session cannot safely attach itself to whatever pause
        # generation happens to be current now. The terminal writers record an
        # applied no-op instead.
        effect_required = False
        pause_token = None
    if (
        effect_required
        and not (isinstance(pause_token, str) and pause_token.strip())
    ) or (not effect_required and pause_token is not None):
        return _ResolutionCommitResult(
            False, False, "pause_effect_binding_invalid"
        )

    sealed = seal_resolution_for_persistence(
        session, resolution, status="resolved"
    )
    return await _commit_terminal_resolution(
        session,
        sealed,
        status=DialecticPhase.RESOLVED.value,
        expected_transcript_revision=expected_transcript_revision,
    )


async def _apply_durable_resolution_effect(
    session: DialecticSession,
    resolution: Resolution,
) -> _ResolutionEffectResult:
    """Claim and apply the exact effect atomically queued with the decision.

    ``pending -> applying`` grants one live claimant; it is not a promise that
    a multi-step runtime effect completed exactly once. A crash or task
    cancellation before the claim leaves ``pending``; after it leaves
    ``applying`` (or best-effort ``unknown_partial``), both of which are visible
    for operator reconciliation. We never auto-replay those ambiguous states
    because conditions or a resume may already have landed before the process
    died.
    """
    sealed = resolution.to_dict()
    paused_state = getattr(session, "paused_agent_state", {}) or {}
    pause_token = paused_state.get("pause_token")
    marker = paused_state.get("pause_effect_required")
    # Sessions created before this protocol did not carry the discriminator.
    # Their terminal writer records a non-mutating applied no-op: attaching a
    # legacy session to the agent's current pause could consume a newer pause.
    effect_required = marker if isinstance(marker, bool) else False
    if not isinstance(marker, bool):
        pause_token = None
    owner_token = str(uuid.uuid4())
    try:
        effect = await pg_claim_resolution_effect(
            session.session_id,
            owner_token,
            paused_agent_id=session.paused_agent_id,
            reviewer_agent_id=session.reviewer_agent_id,
            effect_required=effect_required,
            pause_token=pause_token,
            resolution=sealed,
        )
    except Exception as exc:
        return _ResolutionEffectResult("unknown", False, error=str(exc))
    if effect is None:
        return _ResolutionEffectResult(
            "missing", False, error="terminal decision has no effect outbox row"
        )
    if (
        effect.get("paused_agent_id") != session.paused_agent_id
        or effect.get("reviewer_agent_id") != session.reviewer_agent_id
        or effect.get("effect_required") is not effect_required
        or effect.get("pause_token") != pause_token
        or effect.get("resolution_payload_json") != sealed
    ):
        return _ResolutionEffectResult(
            "conflict",
            False,
            error="existing effect does not match terminal participants/payload",
        )
    if not effect.get("claimed"):
        return _ResolutionEffectResult(
            str(effect.get("state") or "unknown"),
            False,
            execution_result=effect.get("execution_result_json"),
            error=effect.get("last_error"),
        )

    execution_result: Optional[Dict[str, Any]] = None
    try:
        execution_result = await execute_resolution(
            session,
            resolution,
            record_outcome=False,
            expected_pause_token=pause_token,
            enforce_pause_token=True,
        )
        refusal = execution_result.get("refused")
        if refusal or execution_result.get("success") is not True:
            error = (
                f"runtime effect refused: {refusal}"
                if refusal
                else str(
                    execution_result.get("warning")
                    or "runtime effect did not report success"
                )
            )
            await pg_fail_resolution_effect(
                session.session_id,
                owner_token,
                error,
                execution_result=execution_result,
            )
            return _ResolutionEffectResult(
                "unknown_partial",
                True,
                execution_result=execution_result,
                error=error,
            )

        try:
            completed = await pg_complete_resolution_effect(
                session.session_id, owner_token, execution_result
            )
        except Exception as complete_exc:
            # The UPDATE acknowledgement can be lost after PostgreSQL commits.
            # Exact readback prevents an applied effect from being mislabeled
            # partial (and, later, manually "repaired" a second time).
            try:
                current = await pg_get_resolution_effect(session.session_id)
            except Exception:
                current = None
            stored_result = (
                current.get("execution_result_json") if current else None
            )
            if (
                current
                and current.get("state") == "applied"
                and stored_result == execution_result
                and not stored_result.get("manually_reconciled", False)
            ):
                # The acknowledgement was lost after our exact payload
                # committed. No other automatic writer can apply this claim.
                completed = True
            else:
                if current and current.get("state") == "applied":
                    logger.warning(
                        "Resolution-effect claimant for %s lost ownership to "
                        "a different applied result after completion failed",
                        session.session_id,
                    )
                    return _ResolutionEffectResult(
                        "applied",
                        False,
                        execution_result=stored_result,
                        error="a different reconciliation result owns the effect",
                    )
                raise complete_exc
        if not completed:
            current = await pg_get_resolution_effect(session.session_id)
            current_state = (
                str(current.get("state") or "unknown")
                if current
                else "missing"
            )
            current_result = (
                current.get("execution_result_json") if current else None
            )
            still_owned = bool(
                current
                and current_state == "applying"
                and current.get("owner_token") == owner_token
            )
            logger.warning(
                "Resolution-effect completion CAS was refused for %s; "
                "authoritative state is %s",
                session.session_id,
                current_state,
            )
            return _ResolutionEffectResult(
                current_state,
                still_owned,
                execution_result=(
                    current_result
                    if current_result is not None
                    else execution_result
                ),
                error="effect completion compare-and-set was refused",
            )
        # Outcome telemetry is downstream of the durable applied marker. Only
        # an exception followed by our exact persisted result recovers this
        # claimant's ownership; a definite CAS loss never emits for the winner.
        try:
            await record_resolution_outcome(session, resolution, execution_result)
        except asyncio.CancelledError:
            # The authoritative marker is already applied. Cancellation may
            # drop telemetry, but must not turn a completed effect back into an
            # ambiguous or replayable state.
            raise
        except Exception as telemetry_exc:
            logger.warning(
                "Resolution-effect telemetry failed for %s after apply: %s",
                session.session_id,
                telemetry_exc,
            )
        return _ResolutionEffectResult(
            "applied", True, execution_result=execution_result
        )
    except asyncio.CancelledError:
        # Do not guess where a cancelled multi-step effect stopped. The durable
        # `applying` state is intentionally left for manual inspection; replay
        # would duplicate any condition/lifecycle/discovery work that landed.
        raise
    except Exception as exc:
        try:
            await pg_fail_resolution_effect(
                session.session_id,
                owner_token,
                str(exc),
                execution_result=execution_result,
            )
        except Exception as mark_exc:
            logger.warning(
                "Could not mark dialectic resolution effect uncertain for %s: %s",
                session.session_id,
                mark_exc,
            )
        return _ResolutionEffectResult(
            "unknown_partial",
            True,
            execution_result=execution_result,
            error=str(exc),
        )


async def check_reviewer_stuck(session: DialecticSession) -> bool:
    """
    Check if the assigned reviewer is unavailable when it owes a response.

    During ANTITHESIS, the reviewer owes its first response; the timeout uses
    thesis submission time and aligns with MAX_ANTITHESIS_WAIT (2 hours).
    During SYNTHESIS, only a session already awaiting facilitation can make the
    reviewer owe reconsideration. In that state, a missing/paused reviewer is
    stuck immediately; an active reviewer retains the protocol's synthesis
    window and is not reassigned merely because time passed.

    Returns:
        True if reviewer is stuck, False otherwise
    """
    if session.phase not in (DialecticPhase.ANTITHESIS, DialecticPhase.SYNTHESIS):
        return False
    reviewer_id = session.reviewer_agent_id
    if not reviewer_id:
        return False  # No reviewer assigned yet — not stuck, just unassigned

    # A legacy/corrupt assignment that violates the stored authorship boundary
    # is immediately repairable.  Waiting for a liveness timeout would describe
    # a live but unauthorized reviewer as merely slow.
    if _reviewer_authorship_conflict(session, reviewer_id):
        return True

    if (
        session.phase == DialecticPhase.SYNTHESIS
        and not bool(getattr(session, "awaiting_facilitation", False))
    ):
        return False

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

    # An active synthesis reviewer may still answer through its full negotiated
    # response window. The phase timeout/reaper owns that clock.
    if session.phase == DialecticPhase.SYNTHESIS:
        return False

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


async def _attach_public_resolution_effect(
    session_data: Dict[str, Any], session_id: str
) -> None:
    """Attach the non-sensitive effect projection used by public reads.

    The reconstructed-session path loses database-only fields when it passes
    through ``DialecticSession.to_dict``. Re-read just the state needed for
    actionability, while keeping owner tokens, execution results, and exception
    text on the operator-only reconciliation action.
    """
    session_data.update({
        "resolution_effect_state": None,
        "resolution_effect_state_observed": True,
        "resolution_effect_attempt_count": 0,
        "resolution_effect_claimed_at": None,
        "resolution_effect_applied_at": None,
        "resolution_effect_uncertain_at": None,
    })
    try:
        effect = await pg_get_resolution_effect(session_id)
    except Exception as exc:
        logger.warning(
            "Could not read resolution effect for %s: %s", session_id, exc
        )
        # A read failure is not equivalent to a successful legacy lookup with
        # no effect row. Preserve that distinction in the public projection so
        # a resolved session cannot look clean while its runtime effect is
        # unobservable.
        session_data["resolution_effect_state"] = "unknown"
        session_data["resolution_effect_state_observed"] = False
        return
    if effect is None:
        return
    session_data["resolution_effect_state"] = effect.get("state")
    session_data["resolution_effect_attempt_count"] = int(
        effect.get("attempt_count") or 0
    )
    for field in ("claimed_at", "applied_at", "uncertain_at"):
        value = effect.get(field)
        public_name = f"resolution_effect_{field}"
        session_data[public_name] = (
            value.isoformat() if hasattr(value, "isoformat") else value
        )


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

    authorship_conflict = _reviewer_authorship_conflict(session, candidate_id)
    if authorship_conflict:
        error_code, message = authorship_conflict
        return [error_response(
            message,
            error_code=error_code,
            error_category="governance_refusal",
            recovery={
                "action": "Assign an agent who did not author the reviewed work.",
                "related_tools": ["dialectic", "request_review"],
            },
        )]

    return None


def _reviewer_reassignment_actor(session: DialecticSession) -> Optional[str]:
    """Return the authorized reassignment role for the current request.

    A normal bound identity is accountability, not operator authority. Public
    reassignment is therefore limited to a credentialed operator or the current
    independent reviewer handing its own assignment off. The paused agent is
    never allowed to choose its replacement reviewer, including self-review
    sessions where it occupies both participant fields.
    """
    try:
        from ..identity.operator import is_operator_caller
        if is_operator_caller():
            return "operator"
    except Exception:
        pass

    try:
        from ..context import get_context_agent_id
        current_agent_id = get_context_agent_id()
    except Exception:
        current_agent_id = None

    if (
        current_agent_id
        and current_agent_id == session.reviewer_agent_id
        and current_agent_id != session.paused_agent_id
        and not _reviewer_authorship_conflict(session, current_agent_id)
    ):
        return "current_reviewer"
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
    authorship_conflict = _reviewer_authorship_conflict(session, new_reviewer_id)
    if authorship_conflict:
        error_code, message = authorship_conflict
        raise RuntimeError(f"{error_code}: {message}")

    old_reviewer_id = session.reviewer_agent_id
    old_reviewer_conflicted = bool(
        _reviewer_authorship_conflict(session, old_reviewer_id)
    )
    was_awaiting = bool(getattr(session, "awaiting_facilitation", False))
    old_phase = session.phase
    old_transcript_length = len(session.transcript)
    old_transcript_revision = int(
        getattr(session, "transcript_revision", 0)
    )
    was_terminal = session.phase in (DialecticPhase.RESOLVED, DialecticPhase.FAILED)
    session.reviewer_agent_id = new_reviewer_id
    session.awaiting_facilitation = False

    # Revive a session that was swept while its facilitation request stood.
    # Assigning a reviewer to a row still marked `failed` would answer the
    # request on paper and leave it dead in fact: no phase the new reviewer can
    # act in. Reopen to the phase the session was actually waiting in —
    # ANTITHESIS if a thesis is already on the record, THESIS otherwise.
    #
    # Scoped to was_awaiting: an ordinary failed/resolved session is never
    # resurrected by a reassign, only one carrying an unanswered request.
    reopen_failed = False
    if was_terminal and was_awaiting:
        has_thesis = any(m.phase == "thesis" for m in session.transcript)
        session.phase = DialecticPhase.ANTITHESIS if has_thesis else DialecticPhase.THESIS
        reopen_failed = True
    elif (
        (was_awaiting or old_reviewer_conflicted)
        and old_reviewer_id
        and (
            old_reviewer_id == session.paused_agent_id
            or old_reviewer_conflicted
        )
        and session.phase == DialecticPhase.SYNTHESIS
    ):
        # Answering a refused self-review or declared-author assignment: rewind
        # to ANTITHESIS so the incoming independent reviewer actually gets a turn.
        #
        # Left at SYNTHESIS, the new reviewer inherits a session whose only
        # antithesis and synthesis were authored by the conflicted identity.
        # `_reviewer_verdict_pending` needs an INDEPENDENT antithesis to exist
        # before it will hold the paused agent, so it stays False, no objection
        # stands, and the paused agent can resolve the session with its new
        # reviewer never having spoken — the same self-certification arriving
        # by a longer route.
        #
        # Governed review required this (session cfb3f0085a4d5c06, 2026-08-26):
        # "Reassignment must leave a newly independent reviewer a reachable
        # phase; reopening to ANTITHESIS is safest because the prior antithesis
        # and synthesis came from the conflicted identity and should not be
        # treated as the independent reviewer's verdict." The self-review
        # messages stay in the transcript as attributed audit evidence.
        session.phase = DialecticPhase.ANTITHESIS

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

    # ⛔`persisted` gates the (F) emission below. It must NOT be conditioned on
    # `strict_persistence` (default False): when persistence raises and strict is
    # off, the except swallows and execution falls through, so an unconditional
    # emit would record a reassignment that never happened. That inflates the
    # numerator of the very metric this emission exists to make measurable, and
    # it contradicts the "do not report success on it" guard below. Found in
    # review 2026-08-22; the live caller at handle_get_dialectic_session's
    # check_timeout branch uses the default.
    persisted = False
    try:
        # Reviewer ownership, the audit message, the protocol phase/round and
        # the durable transcript frontier move in one transaction. Besides
        # keeping the in-memory revision current, the old-reviewer predicate
        # prevents a stale current reviewer from overwriting an operator's
        # concurrent reassignment. A swept facilitation request is reopened by
        # the same guarded UPDATE rather than by a preceding partial write.
        await _append_protocol_message(
            session,
            expected_phase=old_phase.value,
            expected_reviewer=old_reviewer_id,
            expected_transcript_revision=old_transcript_revision,
            expected_awaiting_facilitation=was_awaiting,
            next_phase=session.phase.value,
            next_synthesis_round=session.synthesis_round,
            next_reviewer=new_reviewer_id,
            update_reviewer=True,
            next_awaiting_facilitation=False,
            next_status="active" if reopen_failed else None,
            reopen_failed_facilitation=reopen_failed,
            agent_id="system",
            message_type="system",
            reasoning=reasoning,
        )
        persisted = True
    except BeamDialecticPolicyRefusal:
        session.reviewer_agent_id = old_reviewer_id
        session.awaiting_facilitation = was_awaiting
        session.phase = old_phase
        session.transcript_revision = old_transcript_revision
        del session.transcript[old_transcript_length:]
        raise
    except Exception as e:
        session.reviewer_agent_id = old_reviewer_id
        session.awaiting_facilitation = was_awaiting
        session.phase = old_phase
        session.transcript_revision = old_transcript_revision
        del session.transcript[old_transcript_length:]
        logger.error(f"Failed to persist reviewer reassignment: {e}")
        if strict_persistence:
            raise

    # Wave-3 prereq PR #9 finding: disconfirmer (F)'s reassignment-rate
    # metric had NO event-stream source — reassignments lived only in
    # session transcripts (zero %reassign% rows in audit.events,
    # all-time).
    #
    # ⛔This is NOT the single chokepoint, and a comment here claimed it
    # was until 2026-08-22. `auto_resolve_stuck_sessions` writes reviewer
    # changes directly and never calls this function, so an auto-path
    # reassignment would never reach the (F) stream. Both *reassignment*
    # producers now emit through `events.emit_reviewer_reassigned`, tagged by
    # `source`. ⛔First-responder initial assignment (`handle_submit_antithesis`)
    # is a third writer of this column and deliberately does not emit — see the
    # scope boundary in `events.py`.
    if persisted:
        await _emit_request_reviewer_reassigned(
            session_id=session_id,
            old_reviewer_id=old_reviewer_id,
            new_reviewer_id=new_reviewer_id,
            reason=reason,
        )

    return {
        "old_reviewer_id": old_reviewer_id,
        "new_reviewer_id": new_reviewer_id,
        "reason": reason,
        "reasoning": reasoning,
        "persisted": persisted,
    }


async def _validate_synthesis_participant_and_conflict(
    session: Any,
    agent_id: str,
    arguments: Dict[str, Any],
) -> Optional[Sequence[TextContent]]:
    """Verify participant membership and absence of reviewer authorship conflict."""
    # Participant-set eligibility gate. The caller is already bound and
    # submitting as itself (require_bound_caller above); this restricts
    # which bound identities may synthesize at all: the paused agent and
    # the assigned reviewer. Without it, any registered agent could drive
    # a synthesis to convergence and trigger resolution execution.
    eligible = set()
    if getattr(session, "paused_agent_id", None):
        eligible.add(session.paused_agent_id)
    if getattr(session, "reviewer_agent_id", None):
        eligible.add(session.reviewer_agent_id)
    # #1414: `agent_id` is now canonically a UUID (dialectic/auth.py
    # contract), so the second-chance lookup through
    # `resolve_agent_uuid` is no longer needed — and it read a
    # caller-supplied `_agent_uuid` on a path where
    # `require_registered_agent` never ran, which let a registered
    # non-participant forge their way past this eligibility gate.
    if agent_id not in eligible:
        return [error_response(
            f"Agent '{agent_id}' is not a participant in this dialectic session.",
            recovery=(
                "Only the paused agent or the assigned reviewer "
                "may submit synthesis."
            ),
        )]

    # A legacy or externally-written row may already carry a reviewer
    # who is recused by the stored reviewed-work declaration. Refuse
    # after authenticating participant membership, but before abstention
    # events, protocol mutation, or persistence. This ordering keeps an
    # unrelated bound caller from learning protected session metadata
    # while ensuring a conflicted reviewer has no synthesis authority.
    authorship_conflict = _reviewer_authorship_conflict(
        session, session.reviewer_agent_id
    )
    if authorship_conflict:
        error_code, message = authorship_conflict
        try:
            await _set_awaiting_facilitation(
                session, True, reason="reviewer_author_conflict"
            )
        except Exception:
            return [error_response(
                "The reviewer authorship conflict was detected, but "
                "the facilitation route could not be recorded because "
                "the dialectic changed. Reload the session.",
                error_code="DIALECTIC_TRANSCRIPT_CHANGED",
                error_category="governance_refusal",
                recovery={
                    "action": "Reload the session before reassigning its reviewer.",
                    "related_tools": ["dialectic"],
                },
                arguments=arguments,
            )]
        return [error_response(
            message,
            details={"awaiting_facilitation": True},
            error_code=error_code,
            error_category="governance_refusal",
            recovery={
                "action": "Ask an operator to assign an independent reviewer.",
                "related_tools": ["dialectic"],
            },
            arguments=arguments,
        )]
    return None


async def _record_synthesis_abstention(
    session: Any,
    session_id: str,
    agent_id: str,
    arguments: Dict[str, Any],
) -> Sequence[TextContent]:
    """Record a non-judgment abstention without consuming a synthesis round."""
    # ── A NON-JUDGMENT IS NOT A VERDICT (synthesis side) ─────────────
    # The reconsideration rounds re-ask the model, so they can also come
    # back unparseable. Filing that would burn a synthesis round against
    # max_synthesis_rounds AND overwrite a REASONED standing objection
    # with an empty one -- destroying the very thing the paused agent is
    # in the middle of answering. Abstain: the standing verdict stands
    # untouched and the round is not spent.
    if agent_id == session.paused_agent_id:
        abstention_recorded = await emit_participant_abstained(
            session_id=session_id,
            participant_agent_id=agent_id,
            paused_agent_id=session.paused_agent_id,
            phase=session.phase.value,
            reason="no_judgment_formed",
        )
    else:
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
        "phase": session.phase.value,
        "synthesis_round": getattr(session, "synthesis_round", None),
        "round_consumed": False,
        "message": (
            "Recorded an abstention: no judgment was formed, so no "
            "synthesis was filed. Any standing verdict is unchanged "
            "and no synthesis round was consumed."
        ),
    })


def _parse_synthesis_proposal(
    session: Any,
    arguments: Dict[str, Any],
) -> Tuple[bool, Any, Optional[Sequence[TextContent]]]:
    """Coerce agrees flag and validate proposed_conditions for agreement."""
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
            return agrees, proposed_conditions, [error_response(
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
    return agrees, proposed_conditions, None


async def _submit_and_persist_synthesis_message(
    session: Any,
    session_id: str,
    agent_id: str,
    api_key: str,
    agrees: bool,
    proposed_conditions: Any,
    arguments: Dict[str, Any],
    *,
    prev_phase: str,
    prev_reviewer_id: Optional[str],
    prev_transcript_revision: int,
    prev_awaiting_facilitation: bool,
    restore_pre_synthesis_state: Any,
) -> Tuple[Dict[str, Any], Optional[Sequence[TextContent]]]:
    """Submit synthesis to in-memory protocol and persist the transcript row."""
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

    result = session.submit_synthesis(message, api_key)

    # Ordering guard: the independent reviewer has posted antithesis but
    # its verdict call has not landed yet. This attempt was not recorded
    # and is not a max-rounds failure; tell the paused agent to wait for
    # the reviewer instead of applying the conservative-default branch.
    if result.get("blocked") == "reviewer_verdict_pending":
        restore_pre_synthesis_state()
        return result, [error_response(
            result.get("error")
            or "The independent reviewer's synthesis verdict is still pending.",
            error_code="REVIEWER_VERDICT_PENDING",
            error_category="governance_refusal",
            recovery={
                "action": (
                    "Wait for the assigned reviewer to submit its synthesis "
                    "verdict, then inspect the session before responding."
                ),
                "reviewer_agent_id": session.reviewer_agent_id,
                "next_call": (
                    f"dialectic(action='get', session_id='{session_id}')"
                ),
                "related_tools": ["dialectic"],
            },
            arguments=arguments,
        )]

    if result.get("blocked") == "reviewer_author_conflict":
        restore_pre_synthesis_state()
        conflict = _reviewer_authorship_conflict(
            session, session.reviewer_agent_id
        )
        error_code, message_text = conflict or (
            "REVIEWER_AUTHOR_CONFLICT",
            "The assigned reviewer cannot supply independent review authority.",
        )
        return result, [error_response(
            message_text,
            error_code=error_code,
            error_category="governance_refusal",
            recovery={
                "action": "Ask an operator to assign an independent reviewer.",
                "related_tools": ["dialectic"],
            },
            arguments=arguments,
        )]

    if result.get("success"):
        # Persist synthesis message to PostgreSQL
        # NOTE: Defer phase update for converged sessions until after finalize_resolution
        # succeeds, to avoid "resolved" phase in PG without a resolution if finalize fails.
        _blocked = result.get("blocked") == "reviewer_objection_stands"
        try:
            # The synthesis row previously persisted no observed_metrics
            # at all, leaving zero provenance surface on the verdict that
            # actually decides the session — stamp it when supplied.
            synth_observed_metrics = _merge_caller_reviewer_provenance(
                None, arguments.get('reviewer_provenance')
            )
            await _append_protocol_message(
                session,
                expected_phase=prev_phase,
                expected_reviewer=prev_reviewer_id,
                expected_transcript_revision=prev_transcript_revision,
                expected_awaiting_facilitation=prev_awaiting_facilitation,
                next_phase=(
                    prev_phase
                    if result.get("converged")
                    else session.phase.value
                ),
                next_synthesis_round=session.synthesis_round,
                next_awaiting_facilitation=True if _blocked else None,
                agent_id=agent_id,
                message_type="synthesis",
                root_cause=arguments.get('root_cause'),
                proposed_conditions=proposed_conditions,
                reasoning=arguments.get('reasoning'),
                agrees=agrees,
                observed_metrics=synth_observed_metrics or None,
            )
        except _TranscriptAppendRefused:
            restore_pre_synthesis_state()
            ACTIVE_SESSIONS.pop(session_id, None)
            return result, [error_response(
                "The dialectic changed before the synthesis could be recorded.",
                error_code="DIALECTIC_TRANSCRIPT_CHANGED",
                error_category="governance_refusal",
                recovery={
                    "action": "Reload the session before submitting another synthesis.",
                    "related_tools": ["dialectic"],
                },
                arguments=arguments,
            )]
        except Exception as e:
            restore_pre_synthesis_state()
            logger.warning(
                "Could not persist synthesis: %s", type(e).__name__
            )
            return result, [error_response(
                "Could not persist the synthesis; no synthesis was acknowledged.",
                error_code="DIALECTIC_MESSAGE_WRITE_FAILED",
                error_category="system_error",
                arguments=arguments,
            )]

        if _blocked:
            session.awaiting_facilitation = True

        # Persist to JSON.
        #
        # On the converged path this must NOT commit the terminal row:
        # submit_synthesis has already set phase=RESOLVED, but the
        # resolution object is only built by finalize_resolution a few
        # lines below. An eager terminal write here lands an empty
        # `{}` resolution, and the guarded write (B-4 / the BEAM saga)
        # then treats the real one as an already-terminal conflict and
        # drops it. Same deferral the phase write above already uses.
        if not result.get("converged"):
            try:
                await save_session(session, primary_already_synced=True)
            except Exception as e:
                logger.warning(f"Could not save session after synthesis: {e}")

    return result, None


async def _finalize_hard_limit_block(
    session: Any,
    session_id: str,
    result: Dict[str, Any],
    violation: Optional[str],
    terminal_frontier: int,
    arguments: Dict[str, Any],
) -> Optional[Sequence[TextContent]]:
    """Commit a FAILED/block terminal outcome when a synthesis violates hard limits."""
    # Safety violation: mark session as FAILED (not RESOLVED)
    session.phase = DialecticPhase.FAILED
    # finalize_resolution() installed the provisional proposal
    # on the session before the hard-limit gate examined it.
    # It is unsafe and did not become the durable resolution;
    # never retain or snapshot that RESUME-shaped object beside
    # the committed FAILED/block terminal record.
    session.resolution = None
    result["action"] = "block"
    result["success"] = False
    # ``submit_synthesis`` reports the agreement it observed
    # before the hard-limit gate runs.  Once the guarded
    # terminal write below commits FAILED, never leave its
    # provisional RESOLVED phase/convergence semantics in the
    # public envelope: phase-keyed clients must see the same
    # terminal state that durable storage records.
    result["phase"] = DialecticPhase.FAILED.value
    result["agreement_reached"] = True
    result["converged"] = False
    result["resolved"] = False
    result["hard_limit_blocked"] = True
    result["reason"] = f"Safety violation: {violation}"
    try:
        _block = {"action": "block", "reason": violation}
        commit = await _commit_terminal_resolution(
            session,
            _block,
            status="failed",
            expected_transcript_revision=terminal_frontier,
        )
    except Exception as e:
        logger.warning(
            "Could not commit hard-limit block: %s", type(e).__name__
        )
        ACTIVE_SESSIONS.pop(session_id, None)
        return [error_response(
            "The hard-limit decision could not be persisted; no terminal outcome was recorded.",
            error_code="SESSION_RESOLUTION_WRITE_FAILED",
            error_category="system_error",
            arguments=arguments,
        )]
    if commit.commit_unknown:
        ACTIVE_SESSIONS.pop(session_id, None)
        return [error_response(
            "The hard-limit terminal write outcome is unknown; reload before continuing.",
            error_code="SESSION_RESOLUTION_COMMIT_UNKNOWN",
            error_category="system_error",
            details={
                "session_id": session_id,
                "commit_origin": commit.origin,
                "terminal_committed": None,
            },
            recovery={
                "action": "Reload the session and inspect its terminal state.",
                "related_tools": ["dialectic"],
            },
            arguments=arguments,
        )]
    if not commit.terminal_committed:
        session.phase = DialecticPhase.SYNTHESIS
        ACTIVE_SESSIONS.pop(session_id, None)
        return _terminal_commit_refusal_response(
            session_id,
            commit,
            arguments,
            changed_msg="The dialectic changed before the hard-limit decision could commit.",
            refused_msg="The persisted session refused the hard-limit decision.",
            recovery_action="Reload the session before attempting another resolution.",
        )
    session.phase = DialecticPhase.FAILED
    return None


async def _commit_and_apply_converged_resume(
    session: Any,
    session_id: str,
    resolution: Any,
    result: Dict[str, Any],
    terminal_frontier: int,
    arguments: Dict[str, Any],
) -> Tuple[bool, Optional[Sequence[TextContent]]]:
    """Commit a safe converged resolution via CAS and apply its durable runtime effect."""
    result["action"] = "resume"
    _attach_resolution_to_result(result, resolution)

    # The terminal compare-and-set is the effect owner. Win it
    # before applying conditions or resuming the agent so a
    # stale/reassigned reviewer cannot mutate runtime state and
    # only afterwards learn that another writer won.
    try:
        authority_refusal = (
            await _precommit_resolution_authority_refusal(session)
        )
        if authority_refusal:
            session.phase = DialecticPhase.SYNTHESIS
            result["action"] = "facilitation_required"
            result["success"] = False
            result["converged"] = False
            result["blocked"] = authority_refusal
            await _set_awaiting_facilitation(
                session, True, reason=authority_refusal
            )
            await save_session(
                session, primary_already_synced=True
            )
            refusal_code = {
                "self_review_not_authorizing": (
                    "SELF_REVIEW_NOT_AUTHORIZING"
                ),
                "review_authorship_invalid": (
                    "REVIEW_AUTHORSHIP_METADATA_INVALID"
                ),
                "reviewer_author_conflict": (
                    "REVIEWER_AUTHOR_CONFLICT"
                ),
            }[authority_refusal]
            refusal_message = {
                "self_review_not_authorizing": (
                    "A self-review cannot release your own pause."
                ),
                "review_authorship_invalid": (
                    "Malformed reviewed-work authorship metadata "
                    "cannot authorize a resume."
                ),
                "reviewer_author_conflict": (
                    "A declared author of the reviewed work cannot "
                    "authorize its resume as an independent reviewer."
                ),
            }[authority_refusal]
            return False, [error_response(
                f"{refusal_message} The synthesis is recorded; "
                "resuming requires an independent reviewer or an operator.",
                error_code=refusal_code,
                error_category="governance_refusal",
                details={
                    "session_id": session_id,
                    "reviewer_agent_id": session.reviewer_agent_id,
                    "paused_agent_id": session.paused_agent_id,
                    "awaiting_facilitation": True,
                    "terminal_committed": False,
                    "detected_at": "precommit_authority_recheck",
                },
                recovery={
                    "action": (
                        "Assign an independent reviewer or ask an "
                        "operator to facilitate the open synthesis."
                    ),
                    "related_tools": ["dialectic", "identity"],
                },
                arguments=arguments,
            )]

        commit = await _h_call("_commit_converged_resolution")(
            session,
            resolution,
            expected_transcript_revision=terminal_frontier,
        )
        if commit.commit_unknown:
            discard_receipt_unless_written(
                resolution, written=False, had_receipt=False
            )
            ACTIVE_SESSIONS.pop(session_id, None)
            return False, [error_response(
                "The terminal write outcome is unknown because the "
                "BEAM response and PostgreSQL readback both failed. "
                "If the decision committed, its runtime effect is "
                "durably queued for reconciliation.",
                error_code="SESSION_RESOLUTION_COMMIT_UNKNOWN",
                error_category="system_error",
                details={
                    "session_id": session_id,
                    "terminal_committed": None,
                    "effect_state": "unknown",
                    "commit_origin": commit.origin,
                },
                recovery={
                    "action": (
                        "Reload the session. If it is resolved, inspect "
                        "resolution_effect and use dialectic(action='reconcile_effect')."
                    ),
                    "related_tools": ["dialectic"],
                },
                arguments=arguments,
            )]
        if not commit.terminal_committed:
            discard_receipt_unless_written(
                resolution, written=False, had_receipt=False
            )
            ACTIVE_SESSIONS.pop(session_id, None)
            return False, _terminal_commit_refusal_response(
                session_id,
                commit,
                arguments,
                changed_msg="The dialectic changed before the terminal resolution could commit.",
                refused_msg="The persisted session refused the terminal resolution; no resolved outcome was recorded.",
                recovery_action=(
                    "Reload the session and resolve any reviewer "
                    "authorship or concurrent-state conflict."
                ),
            )
    except BeamDialecticPolicyRefusal as e:
        discard_receipt_unless_written(
            resolution, written=False, had_receipt=False
        )
        session.phase = DialecticPhase.SYNTHESIS
        result["success"] = False
        result["converged"] = False
        result["blocked"] = e.error_code
        if e.error_code not in {
            "identity_proof_invalid",
            "identity_proof_replayed",
            "identity_verification_unavailable",
        }:
            await _set_awaiting_facilitation(
                session, True, reason=e.error_code
            )
        return False, [error_response(
            str(e),
            error_code=_beam_policy_error_code(e),
            error_category="governance_refusal",
            recovery=_beam_policy_recovery(e),
            arguments=arguments,
        )]
    except Exception as e:
        discard_receipt_unless_written(
            resolution, written=False, had_receipt=False
        )
        logger.warning(
            "Could not commit terminal dialectic resolution: %s", e
        )
        ACTIVE_SESSIONS.pop(session_id, None)
        return False, [error_response(
            "The terminal resolution could not be persisted; no "
            "resolved outcome was recorded.",
            error_code="SESSION_RESOLUTION_WRITE_FAILED",
            error_category="system_error",
            recovery={
                "action": (
                    "Reload the session before retrying or ask an "
                    "operator to reconcile its current reviewer."
                ),
                "related_tools": ["dialectic"],
            },
            arguments=arguments,
        )]

    discard_receipt_unless_written(
        resolution, written=True, had_receipt=False
    )
    # The database transition is authoritative; keep a mocked
    # or reconstructed protocol object aligned with the row.
    session.phase = DialecticPhase.RESOLVED
    _attach_resolution_to_result(result, resolution)

    effect = await _h_call("_apply_durable_resolution_effect")(
        session, resolution
    )
    result["resolution_effect_state"] = effect.state
    execution_result = effect.execution_result or {}
    newly_applied_resolution_effect = bool(
        effect.claimed
        or (
            commit.terminal_committed
            and effect.state == "applied"
            and execution_result.get("no_op") is True
        )
    )

    if effect.state != "applied":
        result["success"] = False
        result["effect_pending"] = True
        if effect.error:
            logger.warning(
                "Dialectic resolution effect %s requires "
                "reconciliation: %s",
                session_id,
                effect.error,
            )
        ACTIVE_SESSIONS.pop(session_id, None)
        authority_refusal = execution_result.get("refused")
        refusal_codes = {
            "self_review_not_authorizing": "SELF_REVIEW_NOT_AUTHORIZING",
            "review_authorship_invalid": "REVIEW_AUTHORSHIP_METADATA_INVALID",
            "reviewer_author_conflict": "REVIEWER_AUTHOR_CONFLICT",
        }
        return False, [error_response(
            (
                "The terminal decision was committed, but its runtime "
                "effect is not confirmed applied and requires operator "
                "reconciliation."
            ),
            error_code=refusal_codes.get(
                authority_refusal,
                "SESSION_RESOLUTION_EFFECT_RECONCILIATION_REQUIRED",
            ),
            error_category=(
                "governance_refusal"
                if authority_refusal
                else "system_error"
            ),
            details={
                "session_id": session_id,
                "terminal_committed": True,
                "effect_pending": True,
                "effect_state": effect.state,
                "effect_claimed_here": effect.claimed,
                "commit_origin": commit.origin,
                "effect_reason": (
                    authority_refusal or "not_confirmed_applied"
                ),
            },
            recovery={
                "action": (
                    "Inspect the paused agent and effect state, then use "
                    "dialectic(action='reconcile_effect', session_id=...)."
                ),
                "related_tools": ["dialectic", "check_working_state"],
            },
            arguments=arguments,
        )]

    # A reflection session has no runtime mutation to claim:
    # its outbox row is born applied. The winning terminal
    # writer still owns one telemetry emission; an
    # already-terminal replay never reaches this branch.
    if (
        newly_applied_resolution_effect
        and not effect.claimed
        and execution_result.get("no_op") is True
    ):
        await record_resolution_outcome(
            session, resolution, execution_result
        )

    result["execution"] = execution_result
    if execution_result.get("no_op"):
        no_op_reason = execution_result.get("reason")
        if execution_result.get("superseded"):
            result["next_step"] = (
                "Review completed, but the pause generation bound to "
                "this session was already consumed or superseded. No "
                "current pause was resumed and no conditions were applied."
            )
        elif no_op_reason == "legacy_unbound_session":
            result["next_step"] = (
                "Review completed as a safe no-op because this session "
                "predates pause-generation binding. Open a new review for "
                "any current pause; no agent resume or condition mutation "
                "was authorized here."
            )
        else:
            result["next_step"] = (
                "Review completed. No pause was bound at session open, "
                "so no agent resume or condition mutation was authorized."
            )
    elif execution_result.get("success"):
        result["next_step"] = next_step_resumed()
    else:
        result["next_step"] = next_step_resume_not_applied(
            execution_result.get("warning")
        )

    return newly_applied_resolution_effect, None


async def _finalize_terminal_session_state(
    session: Any,
    result: Dict[str, Any],
    *,
    emit_resolved: bool,
) -> None:
    """Clear metadata caches, facilitation flag, emit terminal event, and write offline snapshot."""
    # Always invalidate cache after convergence (success or failure)
    for aid in (session.paused_agent_id, session.reviewer_agent_id):
        if aid and aid in _SESSION_METADATA_CACHE:
            del _SESSION_METADATA_CACHE[aid]

    # The terminal CAS clears this flag atomically in both the BEAM
    # and Python writers. Mirror that durable result in memory; a
    # second unfenced write here could clobber a later transition.
    if getattr(session, "awaiting_facilitation", False):
        session.awaiting_facilitation = False

    # Resolution is committed (BEAM or pg fallback above); announce the
    # terminal outcome. session.phase is RESOLVED or FAILED here.
    if emit_resolved:
        await _emit_dialectic_event(
            "dialectic_resolved", session, action=result.get("action")
        )

    # The terminal CAS above is authoritative. This call writes the
    # offline snapshot only; it must not issue a second terminal
    # transition and misattribute an already-terminal readback.
    await save_session(session, defer_terminal=True)


async def _finalize_converged_synthesis(
    session: Any,
    session_id: str,
    result: Dict[str, Any],
    arguments: Dict[str, Any],
) -> Optional[Sequence[TextContent]]:
    """Finalize a converged synthesis (hard-limit check, self-review gate, CAS commit, and calibration)."""
    # No party signature: party-HMAC minting is retired (#2449),
    # so neither party's api_key is looked up, and the synthesis
    # caller's `api_key` never reaches the resolution (the #2450
    # defect signed party A's slot with it).
    resolution = session.finalize_resolution()
    is_safe, violation = session.check_hard_limits(resolution)
    terminal_frontier = int(session.transcript_revision)
    newly_applied_resolution_effect = False

    if not is_safe:
        block_err = await _h_call("_finalize_hard_limit_block")(
            session,
            session_id,
            result,
            violation,
            terminal_frontier,
            arguments,
        )
        if block_err:
            return block_err
        newly_applied_resolution_effect = True
    elif await _self_review_requires_independent_authority(session):
        return await _refuse_self_review_convergence(
            session, session_id, result, arguments
        )
    else:
        newly_applied_resolution_effect, resume_err = (
            await _h_call("_commit_and_apply_converged_resume")(
                session,
                session_id,
                resolution,
                result,
                terminal_frontier,
                arguments,
            )
        )
        if resume_err:
            return resume_err

    # Update calibration from dialectic outcome
    if newly_applied_resolution_effect:
        try:
            if result.get("action") == "resume":
                applied_execution = result.get("execution") or {}
                if not _resolution_effect_skips_resume_calibration(
                    applied_execution
                ):
                    await update_calibration_from_dialectic(session)
            elif not is_safe:
                await update_calibration_from_dialectic_disagreement(session)
        except Exception as cal_e:
            logger.debug(f"Calibration update after dialectic: {cal_e}")

    await _h_call("_finalize_terminal_session_state")(
        session, result, emit_resolved=newly_applied_resolution_effect
    )
    return None


async def _finalize_max_rounds_synthesis_failure(
    session: Any,
    session_id: str,
    result: Dict[str, Any],
    arguments: Dict[str, Any],
    *,
    prev_phase: str,
    prev_transcript_revision: int,
    restore_pre_synthesis_state: Any,
) -> Optional[Sequence[TextContent]]:
    """Commit conservative terminal default when max synthesis rounds are exceeded."""
    # Max rounds exceeded — durably commit the conservative
    # terminal default at the exact frontier that was loaded.  The
    # protocol object changes its in-memory phase to FAILED before
    # returning this result; acknowledging that state without a
    # terminal CAS left PostgreSQL live in SYNTHESIS and allowed a
    # stale process to continue the review.
    conservative_resolution = {
        "action": "failed_max_rounds",
        "reason": result.get("error")
        or "Max synthesis rounds exceeded",
        "resolution_type": "conservative_default",
        "rounds": result.get("rounds"),
    }
    try:
        commit = await _commit_terminal_resolution(
            session,
            conservative_resolution,
            status=DialecticPhase.FAILED.value,
            expected_phase=prev_phase,
            expected_transcript_revision=prev_transcript_revision,
        )
    except Exception as exc:
        restore_pre_synthesis_state()
        ACTIVE_SESSIONS.pop(session_id, None)
        logger.warning(
            "Could not commit max-round terminal failure: %s",
            type(exc).__name__,
        )
        return [error_response(
            "The max-round terminal decision could not be persisted; no terminal outcome was recorded.",
            error_code="SESSION_RESOLUTION_WRITE_FAILED",
            error_category="system_error",
            arguments=arguments,
        )]
    if commit.commit_unknown:
        ACTIVE_SESSIONS.pop(session_id, None)
        return [error_response(
            "The max-round terminal write outcome is unknown; reload before continuing.",
            error_code="SESSION_RESOLUTION_COMMIT_UNKNOWN",
            error_category="system_error",
            details={
                "session_id": session_id,
                "commit_origin": commit.origin,
                "terminal_committed": None,
            },
            recovery={
                "action": "Reload the session and inspect its terminal state.",
                "related_tools": ["dialectic"],
            },
            arguments=arguments,
        )]
    if not commit.terminal_committed:
        restore_pre_synthesis_state()
        ACTIVE_SESSIONS.pop(session_id, None)
        return _terminal_commit_refusal_response(
            session_id,
            commit,
            arguments,
            changed_msg="The dialectic changed before the max-round decision could commit.",
            refused_msg="The persisted session refused the max-round decision.",
            recovery_action="Reload the session before attempting another resolution.",
        )

    # The quorum-voting escalation path was retired (council
    # 2026-05-06: 0 sessions ever escalated to quorum across 47
    # historical sessions / 6 months).  This is a terminal failure,
    # not permission to execute a resolution effect.
    session.phase = DialecticPhase.FAILED
    result["autonomous_resolution"] = True
    result["resolution_type"] = "conservative_default"
    result["phase"] = DialecticPhase.FAILED.value
    result["converged"] = False
    result["resolved"] = False
    result["terminal_committed"] = True
    result["next_step"] = next_step_no_consensus()
    result["cooldown_until"] = (datetime.now() + timedelta(hours=1)).isoformat()
    await _h_call("_finalize_terminal_session_state")(
        session, result, emit_resolved=True
    )
    return None


async def _handle_reassign_reviewer(arguments: Dict[str, Any]) -> Sequence[TextContent]:
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

    reassignment_actor = _h_call("_reviewer_reassignment_actor")(session)
    if reassignment_actor is None:
        return [error_response(
            "Reviewer reassignment requires an operator credential or the current "
            "independent reviewer handing off its own assignment.",
            error_code="REASSIGN_FORBIDDEN",
            error_category="auth_error",
            recovery={
                "action": (
                    "Ask an authenticated operator or the currently assigned "
                    "reviewer to perform the reassignment."
                ),
                "related_tools": ["dialectic", "identity"],
            },
            arguments=arguments,
        )]

    # Validate phase — THESIS/ANTITHESIS, or any session explicitly waiting on a
    # human.
    #
    # A session that raised its hand for facilitation and was then swept to
    # `failed` used to become unreassignable at exactly the moment someone might
    # act on it: the request is visible, and the one operation that answers it
    # is refused. Honouring `awaiting_facilitation` here is what makes the
    # request answerable at all — assigning a reviewer IS the facilitation.
    #
    # Deliberately NOT a blanket relaxation: an ordinary resolved/failed session
    # stays closed. Only a standing, unanswered request reopens.
    _awaiting = bool(getattr(session, "awaiting_facilitation", False))
    _recused_synthesis = bool(
        session.phase == DialecticPhase.SYNTHESIS
        and _reviewer_authorship_conflict(session, session.reviewer_agent_id)
    )
    if (
        session.phase not in (DialecticPhase.THESIS, DialecticPhase.ANTITHESIS)
        and not _awaiting
        and not _recused_synthesis
    ):
        return [error_response(
            f"Cannot reassign reviewer in phase '{session.phase.value}'. Only THESIS or ANTITHESIS phases allow reassignment.",
            error_code="INVALID_PHASE",
            error_category="validation_error",
        )]

    old_reviewer_id = session.reviewer_agent_id

    if new_reviewer_id:
        # Validate the new reviewer exists and is eligible
        validation_error = await _h_call("_validate_explicit_reviewer_candidate")(session, new_reviewer_id)
        if validation_error:
            return validation_error
    else:
        # Auto-select a replacement
        # Wave 2 audit: force=True dropped per PR #350 precedent. Reviewer
        # selection scans the in-memory fleet; cache is fresh enough.
        await mcp_server.load_metadata_async()
        declaration = session.reviewed_work_authorship
        if declaration.is_invalid:
            return [error_response(
                "Reviewed-work authorship metadata is malformed; reviewer assignment fails closed.",
                error_code="REVIEW_AUTHORSHIP_METADATA_INVALID",
                error_category="governance_refusal",
                recovery=get_awaiting_facilitation_recovery(session_id),
            )]
        exclude = [
            session.paused_agent_id,
            *declaration.author_agent_ids,
        ]
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
        await _h_call("_apply_reviewer_reassignment")(
            session_id,
            session,
            new_reviewer_id,
            reason=reason,
            strict_persistence=True,
        )
    except BeamDialecticPolicyRefusal as e:
        error_code = _beam_policy_error_code(e)
        return [error_response(
            str(e),
            error_code=error_code,
            error_category="governance_refusal",
            recovery=get_awaiting_facilitation_recovery(session_id),
        )]
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
        "reassigned_by_role": reassignment_actor,
        "phase": session.phase.value,
        "reason": reason,
        "recovery": get_reviewer_reassigned_recovery(
            old_reviewer_id,
            new_reviewer_id,
            reason=reason,
            phase=session.phase.value,
        ),
    })


async def _handle_reconcile_resolution_effect(
    arguments: Dict[str, Any],
) -> Sequence[TextContent]:
    """Apply a pending effect or acknowledge an operator-repaired partial one.

    Applying/unknown-partial states are never replayed automatically: the worker
    may have applied only some non-idempotent conditions before it stopped. An
    operator first confirms an applying worker has stopped and fences that exact
    claim with ``mark_uncertain``. After inspecting and repairing live state,
    ``mark_applied`` closes only the resulting unknown-partial record.
    """
    try:
        from ..identity.operator import is_operator_caller

        operator_caller = is_operator_caller()
    except Exception:
        operator_caller = False
    if not operator_caller:
        return [error_response(
            "Resolution-effect reconciliation requires an operator credential.",
            error_code="EFFECT_RECONCILE_FORBIDDEN",
            error_category="auth_error",
            recovery={
                "action": "Ask an authenticated operator to inspect the effect.",
                "related_tools": ["dialectic", "identity"],
            },
            arguments=arguments,
        )]

    session_id = arguments.get("session_id")
    disposition = arguments.get("effect_disposition") or "inspect"
    supplied_owner_token = str(
        arguments.get("effect_owner_token") or ""
    ).strip()
    supplied_effect_updated_at = arguments.get("effect_updated_at")
    reason = str(arguments.get("reason") or "").strip()
    if not session_id:
        return [error_response(
            "session_id is required",
            error_code="MISSING_PARAM",
            error_category="validation_error",
            arguments=arguments,
        )]
    if disposition not in {
        "inspect", "apply_pending", "mark_uncertain", "mark_applied"
    }:
        return [error_response(
            "effect_disposition must be 'inspect', 'apply_pending', "
            "'mark_uncertain', or 'mark_applied'.",
            error_code="INVALID_EFFECT_DISPOSITION",
            error_category="validation_error",
            arguments=arguments,
        )]

    effect = await pg_get_resolution_effect(session_id)
    if effect is None:
        return [error_response(
            f"No durable resolution effect exists for session '{session_id}'.",
            error_code="RESOLUTION_EFFECT_NOT_FOUND",
            error_category="not_found",
            arguments=arguments,
        )]

    state = str(effect.get("state") or "unknown")
    if disposition == "mark_applied" and state == "applied":
        return success_response({
            "success": True,
            "idempotent": True,
            "message": "Resolution effect is already applied.",
            "resolution_effect": _resolution_effect_view(effect),
        })
    if disposition == "mark_uncertain" and state == "unknown_partial":
        return success_response({
            "success": True,
            "idempotent": True,
            "message": "Resolution effect is already fenced as unknown_partial.",
            "resolution_effect": _resolution_effect_view(effect),
        })

    if disposition in {"mark_uncertain", "mark_applied"} and not reason:
        return [error_response(
            "reason is required when changing an ambiguous effect state.",
            error_code="MISSING_PARAM",
            error_category="validation_error",
            arguments=arguments,
        )]

    if disposition == "mark_uncertain":
        if not supplied_owner_token:
            return [error_response(
                "effect_owner_token from a current inspect response is required "
                "to fence an abandoned claimant.",
                error_code="MISSING_PARAM",
                error_category="validation_error",
                arguments=arguments,
            )]
        quarantined = await pg_quarantine_resolution_effect(
            session_id,
            supplied_owner_token,
            f"operator quarantined abandoned claim: {reason}",
        )
        current = await pg_get_resolution_effect(session_id)
        if not quarantined:
            return [error_response(
                "The applying claim changed or is no longer applying; no state "
                "was changed. Inspect it again before acting.",
                error_code="RESOLUTION_EFFECT_QUARANTINE_REFUSED",
                error_category="governance_refusal",
                details={
                    "resolution_effect": _resolution_effect_view(
                        current or effect
                    )
                },
                arguments=arguments,
            )]
        return success_response({
            "success": True,
            "message": (
                "Abandoned claim fenced as unknown_partial. Inspect and repair "
                "live state before marking it applied."
            ),
            "resolution_effect": _resolution_effect_view(current or effect),
        })

    if disposition == "mark_applied":
        if state != "unknown_partial":
            return [error_response(
                "The effect must be inspected in unknown_partial state before "
                "it can be marked applied. Inspect it again after fencing any "
                "live applying claim.",
                error_code="RESOLUTION_EFFECT_ACK_REFUSED",
                error_category="governance_refusal",
                details={"resolution_effect": _resolution_effect_view(effect)},
                arguments=arguments,
            )]
        if supplied_effect_updated_at is None or (
            isinstance(supplied_effect_updated_at, str)
            and not supplied_effect_updated_at.strip()
        ):
            return [error_response(
                "effect_updated_at from the inspect response is required to "
                "mark an unknown-partial effect applied.",
                error_code="MISSING_PARAM",
                error_category="validation_error",
                arguments=arguments,
            )]
        try:
            inspected_updated_at = _parse_resolution_effect_updated_at(
                supplied_effect_updated_at
            )
        except ValueError as exc:
            return [error_response(
                str(exc),
                error_code="INVALID_EFFECT_UPDATED_AT",
                error_category="validation_error",
                arguments=arguments,
            )]
        if effect.get("updated_at") is None:
            return [error_response(
                "The current effect has no durable version timestamp; no "
                "acknowledgement was recorded. Inspect it again.",
                error_code="RESOLUTION_EFFECT_ACK_VERSION_MISSING",
                error_category="system_error",
                details={"resolution_effect": _resolution_effect_view(effect)},
                arguments=arguments,
            )]
        acknowledged = await pg_acknowledge_resolution_effect(
            session_id,
            reason,
            expected_updated_at=inspected_updated_at,
        )
        if not acknowledged:
            current = await pg_get_resolution_effect(session_id)
            return [error_response(
                "The inspected unknown_partial record changed or is no longer "
                "unknown_partial; no acknowledgement was recorded. Inspect it "
                "again before acting.",
                error_code="RESOLUTION_EFFECT_ACK_REFUSED",
                error_category="governance_refusal",
                details={"resolution_effect": _resolution_effect_view(current or effect)},
                arguments=arguments,
            )]
        current = await pg_get_resolution_effect(session_id)
        # The outbox row is authoritative. These emissions are best-effort
        # downstream telemetry and run only for the winning acknowledgement.
        telemetry_attempted = False
        telemetry_warning = None
        try:
            session = await load_session(session_id)
            if session is not None and session.resolution is not None:
                stored_result = (current or effect).get(
                    "execution_result_json"
                )
                if not isinstance(stored_result, dict):
                    stored_result = effect.get("execution_result_json")
                manual_result = (
                    dict(stored_result)
                    if isinstance(stored_result, dict)
                    else {}
                )
                manual_result.update({
                    "success": True,
                    "manually_reconciled": True,
                    "reconciliation_reason": reason,
                })
                manual_result.setdefault("reason", reason)
                telemetry_attempted = True
                await _record_operator_resolution_telemetry(
                    session,
                    manual_result,
                    record_outcome=True,
                    manually_reconciled=True,
                )
            else:
                telemetry_warning = (
                    "The effect is applied, but its terminal session could not "
                    "be loaded for downstream telemetry."
                )
        except Exception as exc:
            telemetry_warning = (
                "The effect is applied, but downstream telemetry could not be "
                "reconstructed."
            )
            logger.warning(
                "Manual effect telemetry failed for %s: %s", session_id, exc
            )
        response = {
            "success": True,
            "message": "Resolution effect acknowledged after manual reconciliation.",
            "resolution_effect": _resolution_effect_view(current or effect),
            "telemetry_attempted": telemetry_attempted,
            "telemetry_note": (
                "Downstream outcome, calibration and lifecycle signals are "
                "best-effort; this response does not attest their delivery."
            ),
        }
        if telemetry_warning:
            response["telemetry_warning"] = telemetry_warning
        return success_response(response)

    if state == "pending" and disposition == "apply_pending":
        session = await load_session(session_id)
        if session is None or session.resolution is None:
            return [error_response(
                "The pending effect cannot be reconstructed from its terminal session.",
                error_code="RESOLUTION_EFFECT_SESSION_INVALID",
                error_category="system_error",
                details={"resolution_effect": _resolution_effect_view(effect)},
                arguments=arguments,
            )]
        applied = await _h_call("_apply_durable_resolution_effect")(
            session, session.resolution
        )
        current = await pg_get_resolution_effect(session_id)
        if applied.state == "applied":
            telemetry_attempted = False
            if applied.claimed:
                telemetry_attempted = True
                await _record_operator_resolution_telemetry(
                    session,
                    applied.execution_result or {},
                    record_outcome=False,
                    manually_reconciled=False,
                )
            return success_response({
                "success": True,
                "message": "Pending resolution effect applied.",
                "resolution_effect": _resolution_effect_view(current or effect),
                "execution": applied.execution_result,
                "telemetry_owner": applied.claimed,
                "telemetry_attempted": telemetry_attempted,
                "telemetry_note": (
                    "Downstream calibration and lifecycle signals are "
                    "best-effort; this response does not attest their delivery."
                ),
            })
        return [error_response(
            "The effect was claimed but is not confirmed applied. Inspect live "
            "agent state before acknowledging any manual repair.",
            error_code="SESSION_RESOLUTION_EFFECT_RECONCILIATION_REQUIRED",
            error_category="system_error",
            details={
                "resolution_effect": _resolution_effect_view(current or effect),
                "effect_error": applied.error,
            },
            arguments=arguments,
        )]

    if state == "pending":
        return success_response({
            "success": True,
            "requires_reconciliation": True,
            "message": (
                "Resolution effect is pending and has not been executed. Inspect "
                "the terminal decision, then explicitly use apply_pending."
            ),
            "resolution_effect": _resolution_effect_view(effect),
            "next_call": (
                f"dialectic(action='reconcile_effect', session_id='{session_id}', "
                "effect_disposition='apply_pending')"
            ),
        })

    if state == "applied":
        return success_response({
            "success": True,
            "message": "Resolution effect is already applied.",
            "resolution_effect": _resolution_effect_view(effect),
        })

    return success_response({
        "success": False,
        "requires_manual_reconciliation": True,
        "message": (
            "Automatic replay is unsafe because the effect may be partially "
            "applied. If it is applying, first confirm its worker stopped and "
            "fence the exact owner token with effect_disposition='mark_uncertain'. "
            "Then inspect and repair live state before using mark_applied."
        ),
        "resolution_effect": _resolution_effect_view(effect),
        "next_call": (
            f"dialectic(action='reconcile_effect', session_id='{session_id}', "
            + (
                "effect_disposition='mark_uncertain', "
                f"effect_owner_token='{effect.get('owner_token')}', reason='worker stopped')"
                if state == "applying"
                else (
                    "effect_disposition='mark_applied', "
                    f"effect_updated_at='{_resolution_effect_view(effect).get('updated_at')}', "
                    "reason='manual repair completed')"
                )
            )
        ),
    })
