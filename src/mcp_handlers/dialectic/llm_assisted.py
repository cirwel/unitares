"""Consult, synthetic review, and LLM-assisted dialectic helpers."""

from typing import Dict, Any, Sequence, Optional
from mcp.types import TextContent
import json
from datetime import datetime, timezone

from src.dialectic_protocol import (
    DialecticSession,
    DialecticMessage,
    DialecticPhase,
)
from src.dialectic_reviewed_work import embed_reviewed_work_authorship
from ..utils import success_response, error_response, require_registered_agent as _default_require_registered_agent
from ..support.coerce import resolve_agent_uuid as _default_resolve_agent_uuid
from .responses import (
    llm_failed_recovery,
    llm_incomplete_recovery,
    llm_missing_root_cause_recovery,
    llm_unavailable_recovery,
    missing_session_id_recovery,
    session_not_found_recovery,
)
from src.logging_utils import get_logger
from .session import (
    discard_receipt_unless_written as _default_discard_receipt_unless_written,
    load_session as _default_load_session,
    load_session_as_dict as _default_load_session_as_dict,
    ACTIVE_SESSIONS as _default_ACTIVE_SESSIONS,
)
from .resolution import record_resolution_outcome as _default_record_resolution_outcome
from .events import emit_reviewer_abstained as _default_emit_reviewer_abstained
from src.dialectic_db import (
    create_session_async as _default_pg_create_session,
    add_bounded_message_async as _default_pg_add_bounded_message,
)
from .actionability import (
    SYNTHETIC_REVIEWER_ID,
    _judgment_was_formed,
    _reviewer_provenance_stamp,
    _synthetic_reviewer_provenance,
    CONSULT_PHASE,
    MAX_CONSULTS_PER_SESSION,
    _consult_list,
    _synthetic_review_approves,
    _capture_pause_evidence,
    _snapshot_agent_state,
    _read_terminal_readback,
    _terminal_readback_next_step,
    _apply_terminal_readback,
    _thesis_session_advanced,
    _get_dialectic_next_steps,
)
from .resolution_helpers import _DictProxy, _McpServerProxy, _h_call, _proxy

logger = get_logger(__name__)

mcp_server = _McpServerProxy()
ACTIVE_SESSIONS = _DictProxy("ACTIVE_SESSIONS", _default_ACTIVE_SESSIONS)

require_registered_agent = _proxy("require_registered_agent", _default_require_registered_agent)
resolve_agent_uuid = _proxy("resolve_agent_uuid", _default_resolve_agent_uuid)
discard_receipt_unless_written = _proxy("discard_receipt_unless_written", _default_discard_receipt_unless_written)
load_session = _proxy("load_session", _default_load_session)
load_session_as_dict = _proxy("load_session_as_dict", _default_load_session_as_dict)
record_resolution_outcome = _proxy("record_resolution_outcome", _default_record_resolution_outcome)
emit_reviewer_abstained = _proxy("emit_reviewer_abstained", _default_emit_reviewer_abstained)
pg_create_session = _proxy("pg_create_session", _default_pg_create_session)
pg_add_bounded_message = _proxy("pg_add_bounded_message", _default_pg_add_bounded_message)
_append_protocol_message = _proxy("_append_protocol_message")
_commit_terminal_resolution = _proxy("_commit_terminal_resolution")
_commit_converged_resolution = _proxy("_commit_converged_resolution")
_apply_durable_resolution_effect = _proxy("_apply_durable_resolution_effect")
_emit_dialectic_event = _proxy("_emit_dialectic_event")
_set_awaiting_facilitation = _proxy("_set_awaiting_facilitation")
_resolve_dialectic_agent_id = _proxy("_resolve_dialectic_agent_id")
_bind_pause_token = _proxy("_bind_pause_token")
_recorded_trigger_source = _proxy("_recorded_trigger_source")
_requested_reviewed_work_authorship = _proxy("_requested_reviewed_work_authorship")


async def _handle_submit_consult(arguments: Dict[str, Any]) -> Sequence[TextContent]:
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
    try:
        session_id = arguments.get("session_id")
        if not session_id:
            return [error_response(
                "session_id is required",
                recovery=missing_session_id_recovery(),
            )]

        agent_id, agent_error = await _resolve_dialectic_agent_id(
            arguments, enforce_session_ownership=True, require_bound_caller=True,
        )
        if agent_error:
            return agent_error

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

        reasoning = str(arguments.get("reasoning") or "").strip()
        if not reasoning:
            return [error_response(
                "reasoning is required: a consult files the outside reviewer's argument",
                error_code="MISSING_PARAM",
                error_category="validation_error",
                arguments=arguments,
            )]
        # A consult with no judgment is not filed: the record exists to count
        # outside verdicts, and an abstention is not one.
        if arguments.get("judgment_formed") is not None and not _judgment_was_formed(
            arguments.get("judgment_formed")
        ):
            return [error_response(
                "No judgment was formed, so there is no consult to file. Nothing was recorded.",
                error_code="NO_JUDGMENT",
                error_category="validation_error",
                arguments=arguments,
            )]
        provenance = arguments.get("reviewer_provenance")
        # The stamp keeps only known keys, so a mapping of misspelt or
        # irrelevant keys would file an external_consult row that names no
        # source at all. At least one of these must say where it came from.
        named_source = isinstance(provenance, dict) and any(
            isinstance(provenance.get(key), str) and provenance.get(key).strip()
            for key in ("backend", "model_used", "consult_source")
        )
        if not named_source:
            return [error_response(
                "reviewer_provenance must name where the verdict came from: at least "
                "one of backend, model_used or consult_source",
                error_code="MISSING_PARAM",
                error_category="validation_error",
                arguments=arguments,
            )]

        position = None
        raw_agrees = arguments.get("agrees")
        if raw_agrees is not None:
            # Only unambiguous spellings. coerce_bool's default would turn a
            # typo or "approve" into a recorded disagreement.
            spelled = str(raw_agrees).strip().lower() if not isinstance(raw_agrees, bool) else raw_agrees
            if spelled in (True, "true"):
                position = "agrees"
            elif spelled in (False, "false"):
                position = "disagrees"
            else:
                return [error_response(
                    f"agrees must be true or false, not {raw_agrees!r}; omit it to state no position",
                    error_code="INVALID_PARAM",
                    error_category="validation_error",
                    arguments=arguments,
                )]
        # The kind is forced: whatever the caller wrote, this route files an
        # outside verdict, and a record of one must say so.
        backend_stamp = _reviewer_provenance_stamp(
            provenance,
            kind="external_consult",
            degraded=bool(provenance.get("degraded")),
        )
        filed = {}

        def metrics_at_filing(row):
            # Built from the session row read inside the insert transaction,
            # so a phase move or reassignment racing this consult cannot leave
            # the record describing the state before it. The raw phase is kept:
            # the loaded session folds phases such as awaiting_thesis into
            # THESIS. The snapshot is only the fallback for a session with no
            # row.
            if row is not None:
                phase = row.get("phase")
                paused, reviewer = row.get("paused_agent_id"), row.get("reviewer_agent_id")
            else:
                phase = session.phase.value
                paused, reviewer = session.paused_agent_id, session.reviewer_agent_id
            if agent_id == paused:
                filed["filer_role"] = "paused_agent"
            elif agent_id == reviewer:
                filed["filer_role"] = "reviewer"
            else:
                filed["filer_role"] = "third_party"
            filed["observed_metrics"] = {
                "reviewer_backend": backend_stamp,
                "consult": {
                    "position": position,
                    "filer_role": filed["filer_role"],
                    "session_phase_at_filing": phase,
                },
            }
            return filed["observed_metrics"]
        proposed_conditions = _consult_list(
            arguments.get("proposed_conditions") or arguments.get("conditions")
        )
        concerns = _consult_list(arguments.get("concerns"))
        root_cause = arguments.get("root_cause")

        message_id = await pg_add_bounded_message(
            session_id=session_id,
            agent_id=agent_id,
            message_type=CONSULT_PHASE,
            max_of_type=MAX_CONSULTS_PER_SESSION,
            root_cause=root_cause,
            proposed_conditions=proposed_conditions or None,
            reasoning=reasoning,
            concerns=concerns or None,
            metrics_from_session=metrics_at_filing,
        )
        if message_id is None:
            return [error_response(
                f"This session already holds {MAX_CONSULTS_PER_SESSION} consults, "
                "the most one session keeps. Nothing was recorded.",
                error_code="CONSULT_LIMIT",
                error_category="validation_error",
                arguments=arguments,
            )]
        session.transcript.append(DialecticMessage(
            phase=CONSULT_PHASE,
            agent_id=agent_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            root_cause=root_cause,
            observed_metrics=filed["observed_metrics"],
            proposed_conditions=proposed_conditions or None,
            reasoning=reasoning,
            agrees=None,
            concerns=concerns or None,
        ))

        return success_response({
            "success": True,
            "session_id": session_id,
            "message_id": message_id,
            "recorded_as": CONSULT_PHASE,
            "reviewer_kind": "external_consult",
            "filer_role": filed["filer_role"],
            "position": position,
            "authority": (
                "none: a consult is a record beside the review. It does not "
                "advance the phase, count as a verdict, or refresh the "
                "session's liveness clock."
            ),
        })
    except Exception as e:
        return [error_response(f"Error submitting consult: {str(e)}")]


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
        local_endpoint_refusal,
    )

    # The thesis and agent state are the operator's data, so this reviewer only
    # runs against an endpoint that classifies local. Recorded as an abstention
    # with its reason, so the open slot does not read as "no reviewer tried".
    refusal = local_endpoint_refusal()
    if refusal is not None:
        logger.info("[DIALECTIC] Synthetic reviewer abstained: %s", refusal)
        await emit_reviewer_abstained(
            session_id=session.session_id,
            reviewer_agent_id=SYNTHETIC_REVIEWER_ID,
            paused_agent_id=session.paused_agent_id,
            phase=session.phase.value,
            reason="local_model_endpoint_not_local",
        )
        return None

    if not await is_llm_available():
        logger.info("[DIALECTIC] Synthetic reviewer skipped — local LLM unavailable")
        return None

    antithesis = await generate_antithesis(thesis, agent_state)
    if not antithesis:
        logger.info("[DIALECTIC] Synthetic reviewer produced no antithesis")
        # Already the right DECISION — it declines to fabricate a verdict. What
        # was missing is any trace of it: a caller reading the session saw an
        # untouched thesis, identical to one no reviewer ever looked at. Record
        # the attempt on the same stream as every other abstention.
        await emit_reviewer_abstained(
            session_id=session.session_id,
            reviewer_agent_id=SYNTHETIC_REVIEWER_ID,
            paused_agent_id=session.paused_agent_id,
            phase=session.phase.value,
            reason="synthetic_reviewer_no_antithesis",
        )
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
    previous_reviewer_id = session.reviewer_agent_id
    previous_phase = session.phase
    previous_synthesis_round = session.synthesis_round
    previous_awaiting_facilitation = bool(session.awaiting_facilitation)
    previous_transcript_length = len(session.transcript)
    previous_transcript_revision = int(
        getattr(session, "transcript_revision", 0)
    )

    def restore_before_antithesis() -> None:
        session.reviewer_agent_id = previous_reviewer_id
        session.phase = previous_phase
        session.synthesis_round = previous_synthesis_round
        session.awaiting_facilitation = previous_awaiting_facilitation
        session.transcript_revision = previous_transcript_revision
        del session.transcript[previous_transcript_length:]

    anti_result = session.submit_antithesis(anti_msg)
    if not anti_result.get("success"):
        restore_before_antithesis()
        logger.warning(
            "[DIALECTIC] Synthetic antithesis rejected by protocol: %s",
            anti_result.get("error"),
        )
        return None

    try:
        await _append_protocol_message(
            session,
            expected_phase=previous_phase.value,
            expected_reviewer=previous_reviewer_id,
            expected_transcript_revision=previous_transcript_revision,
            expected_awaiting_facilitation=previous_awaiting_facilitation,
            next_phase=session.phase.value,
            next_synthesis_round=session.synthesis_round,
            next_reviewer=SYNTHETIC_REVIEWER_ID,
            update_reviewer=previous_reviewer_id is None,
            next_awaiting_facilitation=(
                False if previous_awaiting_facilitation else None
            ),
            agent_id=SYNTHETIC_REVIEWER_ID,
            message_type="antithesis",
            reasoning=anti_reasoning,
            concerns=anti_concerns or None,
            observed_metrics={
                "reviewer_backend": _synthetic_reviewer_provenance(antithesis)
            },
        )
    except Exception as exc:
        restore_before_antithesis()
        logger.warning(
            "[DIALECTIC] Synthetic antithesis append refused: %s",
            type(exc).__name__,
        )
        return None
    if previous_awaiting_facilitation:
        session.awaiting_facilitation = False
    synthesis = await generate_synthesis(thesis, antithesis, synthesis_round=1)
    if not synthesis:
        # Antithesis landed but synthesis failed: leave the session at SYNTHESIS
        # for the paused agent/operator to finish. Better than no antithesis.
        logger.warning("[DIALECTIC] Synthetic antithesis recorded but synthesis failed")
        # The antithesis DID land, so this abstention is narrower than the one
        # above: a real objection is on the record and only the synthesis half
        # is missing. Recorded so "the reviewer stopped here" is a readable
        # fact rather than something inferred from a gap in the transcript.
        await emit_reviewer_abstained(
            session_id=session.session_id,
            reviewer_agent_id=SYNTHETIC_REVIEWER_ID,
            paused_agent_id=session.paused_agent_id,
            phase=session.phase.value,
            reviewer_backend=_synthetic_reviewer_provenance(antithesis),
            reason="synthetic_reviewer_no_synthesis",
            slot_claimed=True,
        )
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
    pre_synthesis_phase = session.phase
    pre_synthesis_round = session.synthesis_round
    pre_synthesis_length = len(session.transcript)
    pre_synthesis_revision = int(session.transcript_revision)
    synth_result = session.submit_synthesis(synth_msg)
    if not synth_result.get("success"):
        session.phase = pre_synthesis_phase
        session.synthesis_round = pre_synthesis_round
        del session.transcript[pre_synthesis_length:]
        return None
    try:
        await _append_protocol_message(
            session,
            expected_phase=pre_synthesis_phase.value,
            expected_reviewer=SYNTHETIC_REVIEWER_ID,
            expected_transcript_revision=pre_synthesis_revision,
            next_phase=(
                pre_synthesis_phase.value
                if synth_result.get("converged")
                else session.phase.value
            ),
            next_synthesis_round=session.synthesis_round,
            agent_id=SYNTHETIC_REVIEWER_ID,
            message_type="synthesis",
            root_cause=synthesis.get("agreed_root_cause", ""),
            proposed_conditions=synth_conditions or None,
            reasoning=synthesis.get("reasoning", ""),
            agrees=synth_agrees,
            observed_metrics={
                "reviewer_backend": _synthetic_reviewer_provenance(synthesis)
            },
        )
    except Exception as exc:
        session.phase = pre_synthesis_phase
        session.synthesis_round = pre_synthesis_round
        session.transcript_revision = pre_synthesis_revision
        del session.transcript[pre_synthesis_length:]
        logger.warning(
            "[DIALECTIC] Synthetic synthesis append refused: %s",
            type(exc).__name__,
        )
        return None

    resolved = False
    terminal_committed = False
    effect_pending = False
    commit_unknown = False
    violation: Optional[str] = None
    if session.phase == DialecticPhase.RESOLVED:
        # No party signature: party-HMAC minting is retired (#2449), so no key
        # is looked up. Before that, this site once derived a fallback key as
        # f"llm-{agent_uuid[:8]}" (removed in #2155), which was forgeable from
        # the publicly served uuid.
        try:
            resolution_obj = session.finalize_resolution()
            is_safe, violation = session.check_hard_limits(resolution_obj)
            if is_safe:
                session.resolution = resolution_obj
                commit = await _h_call("_commit_converged_resolution")(
                    session,
                    resolution_obj,
                    expected_transcript_revision=int(session.transcript_revision),
                )
            else:
                session.resolution = None
                commit = await _commit_terminal_resolution(
                    session,
                    {"action": "block", "reason": violation},
                    status=DialecticPhase.FAILED.value,
                    expected_transcript_revision=int(session.transcript_revision),
                )
            discard_receipt_unless_written(
                resolution_obj,
                written=commit.terminal_committed,
                had_receipt=False,
            )
            if commit.terminal_committed:
                terminal_committed = True
                session.awaiting_facilitation = False
                if not is_safe:
                    session.phase = DialecticPhase.FAILED
                else:
                    effect = await _h_call("_apply_durable_resolution_effect")(
                        session, resolution_obj
                    )
                    if effect.state == "applied":
                        resolved = True
                        execution_result = effect.execution_result or {}
                        newly_applied = bool(
                            effect.claimed
                            or (
                                commit.terminal_committed
                                and execution_result.get("no_op") is True
                            )
                        )
                        if newly_applied and not effect.claimed:
                            await record_resolution_outcome(
                                session, resolution_obj, execution_result
                            )
                        if newly_applied:
                            await _emit_dialectic_event(
                                "dialectic_resolved", session, action="resume"
                            )
                    else:
                        effect_pending = True
                        logger.warning(
                            "[DIALECTIC] Synthetic resolution effect for %s is %s: %s",
                            session.session_id,
                            effect.state,
                            effect.error,
                        )
            elif commit.commit_unknown:
                # The remote POST may still commit after this coroutine exits.
                # Its SQL atomically creates the pending outbox row, so reload
                # is the only truthful next step; never report a refusal.
                commit_unknown = True
                effect_pending = True
                session.phase = DialecticPhase.SYNTHESIS
                session.resolution = None
            else:
                # The DB is the effect owner. A stale/malformed declaration or
                # participant race may refuse the terminal write after local
                # protocol convergence; keep the recorded synthesis answerable
                # and never announce a resolution that did not persist.
                session.phase = DialecticPhase.SYNTHESIS
                session.resolution = None
                logger.warning(
                    "[DIALECTIC] Synthetic terminal write was refused; session "
                    "%s left in synthesis for facilitation",
                    session.session_id,
                )
        except Exception as e:
            discard_receipt_unless_written(session.resolution, written=False, had_receipt=False)
            session.phase = DialecticPhase.SYNTHESIS
            session.resolution = None
            logger.warning("[DIALECTIC] Synthetic resolution finalize failed: %s", e)

    return {
        "antithesis": antithesis,
        "synthesis": synthesis,
        "recommendation": synthesis.get("recommendation", "ESCALATE"),
        "resolved": resolved,
        "terminal_committed": terminal_committed,
        "commit_unknown": commit_unknown,
        "effect_pending": effect_pending,
        "hard_limit_blocked": (
            session.phase == DialecticPhase.FAILED and terminal_committed
        ),
        "hard_limit_violation": (
            violation
            if session.phase == DialecticPhase.FAILED and terminal_committed
            else None
        ),
    }


async def _thesis_recorded_after_facilitation_refused(
    session: DialecticSession,
    result: Dict[str, Any],
    *,
    spawned_reviewer_exited: bool = False,
) -> Sequence[TextContent]:
    """Report a recorded thesis whose facilitation request was refused.

    The facilitation mark is a compare-and-set against the post-thesis snapshot.
    It is refused when the session moved on while the automated review paths
    ran: an orchestrated reviewer that claimed the slot and then exited inside
    the fast-crash wait, or one spawned by a dispatch call that timed out on
    its side (#2905). The thesis append committed before any of that, so the
    refusal is never evidence that the thesis is missing. Re-read the row and
    report what is there instead of failing the whole call.
    """
    session_id = session.session_id
    try:
        persisted = await load_session_as_dict(session_id)
    except Exception as exc:
        logger.warning(
            "Could not reload %s after a refused facilitation mark: %s",
            session_id[:16],
            type(exc).__name__,
        )
        persisted = None

    result["thesis_recorded"] = True
    result["awaiting_facilitation"] = bool(
        (persisted or {}).get("awaiting_facilitation", False)
    )
    poll = f"dialectic(action='get', session_id='{session_id}')"
    if _thesis_session_advanced(session, persisted):
        persisted_phase = str(persisted.get("phase") or "").lower()
        persisted_reviewer = persisted.get("reviewer")
        result["success"] = True
        result["session_advanced"] = True
        result["reviewer_agent_id"] = persisted_reviewer
        result["phase"] = persisted_phase
        if persisted.get("resolution"):
            result["resolution"] = persisted["resolution"]
        holder = (
            f"reviewer {persisted_reviewer} holds the slot"
            if persisted_reviewer
            else "another writer changed it"
        )
        result["note"] = (
            "The thesis is recorded. The session moved on while the automated "
            f"fallback ran ({holder}; phase is now '{persisted_phase}'), so the "
            "fallback stood down."
        )
        # The other writer may already have committed a terminal decision. A
        # terminal failure, or a decision whose durable effect is not confirmed
        # applied, is reported exactly as the synthetic timeout path reports
        # it: unsuccessful, with reconciliation guidance. thesis_recorded stays.
        readback = _read_terminal_readback(persisted)
        _apply_terminal_readback(
            result,
            readback,
            default_failure_reason=(
                "The session's reviewer committed a terminal failure."
            ),
        )
        result["next_step"] = (
            _terminal_readback_next_step(readback, session_id)
            or f"Follow the review with {poll}."
        )
        if spawned_reviewer_exited and persisted_reviewer and not readback.terminal:
            # The reviewer this call spawned exited inside the fast-crash wait.
            # Nothing here links that execution to the identity now holding
            # the slot, so the holder may be the exited reviewer or a live one
            # that claimed it since. Say so rather than naming the holder as
            # the next to move or marking the session on a guess; a stalled
            # synthesis is raised for facilitation by the timeout sweep.
            result["spawned_reviewer_exited"] = True
            result["note"] = (
                "The thesis is recorded. Reviewer "
                f"{persisted_reviewer} holds the slot (phase is now "
                f"'{persisted_phase}'), but the reviewer this call spawned "
                "exited inside the fast-crash wait, and the holder may be that "
                "reviewer. If the session does not move, reassign the reviewer "
                "or ask for facilitation."
            )
            result["next_step"] = (
                f"Check the session with {poll}; if the reviewer does not "
                "act, use dialectic(action='reassign') or request facilitation."
            )
        logger.info(
            "[DIALECTIC] session %s advanced during the thesis fallback "
            "(phase=%s); reporting the recorded thesis",
            session_id[:16],
            persisted_phase,
        )
    else:
        # The refusal is real but the re-read cannot say who moved the row (or
        # the re-read failed). The slot may still be open with no facilitation
        # request on record, so this is not a clean success either.
        result["success"] = False
        result["state_reload_required"] = True
        result["note"] = (
            "The thesis is recorded, but the session changed before the request "
            "for facilitation could be recorded. Reload the session before "
            "another write."
        )
        result["next_step"] = f"Reload with {poll}."
    return success_response(result)


async def _handle_llm_assisted_dialectic(arguments: Dict[str, Any], *, create_session_fn: Any = None) -> Sequence[TextContent]:
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
    from ..support.llm_delegation import (
        is_llm_available,
        local_endpoint_refusal,
        run_full_dialectic,
    )
    from src.local_inference_env import ENDPOINT_NOT_LOCAL

    # Require registered agent
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]

    agent_uuid = resolve_agent_uuid(arguments, agent_id)

    reviewed_work_authorship, authorship_error = (
        await _requested_reviewed_work_authorship(
            arguments, declared_by_agent_id=agent_uuid
        )
    )
    if authorship_error:
        return authorship_error
    assert reviewed_work_authorship is not None

    # The thesis goes to the local model, so it is privacy='local' by nature:
    # refused before any request when the endpoint does not classify local.
    refusal = local_endpoint_refusal()
    if refusal is not None:
        return [error_response(
            refusal,
            error_code=ENDPOINT_NOT_LOCAL,
            error_category="validation_error",
            recovery={
                "action": (
                    "Use dialectic(action='request') for peer review, or have the "
                    "operator reclassify the model endpoint (UNITARES_TRUSTED_NETWORKS, "
                    "UNITARES_MODEL_LOCAL_HOSTS or UNITARES_MODEL_PRIVACY)."
                ),
                "related_tools": ["dialectic"],
            },
        )]

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

    # Use the same decision-time, behavioral-first evidence projection as the
    # peer/orchestrated reviewer path. The prior code read ``monitor.risk_score``
    # (an attribute the monitor does not define) plus raw ODE state.
    pause_evidence = _capture_pause_evidence(
        getattr(mcp_server, "monitors", {}).get(agent_uuid)
    )
    pause_evidence = embed_reviewed_work_authorship(
        pause_evidence, reviewed_work_authorship
    )
    try:
        pause_evidence = await _bind_pause_token(
            pause_evidence, agent_uuid
        )
    except Exception as exc:
        logger.warning(
            "Could not bind LLM dialectic to authoritative agent state (%s)",
            type(exc).__name__,
        )
        return [error_response(
            "The agent's authoritative pause state could not be verified, so "
            "the review was not opened.",
            error_code="PAUSE_BINDING_UNAVAILABLE",
            error_category="system_error",
            arguments=arguments,
        )]
    agent_state = _snapshot_agent_state(agent_uuid)

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
    session = None
    persisted_resolution = False
    terminal_committed = False
    commit_unknown = False
    resolution_effect_state: Optional[str] = None
    effect_pending = False
    awaiting_facilitation = False
    governance_blocked = False
    hard_limit_violation: Optional[str] = None
    try:
        from src.dialectic_protocol import DialecticSession, DialecticMessage as DMsg
        trigger_source = _recorded_trigger_source(arguments)
        session = DialecticSession(
            paused_agent_id=agent_uuid,
            reviewer_agent_id="llm-synthetic-reviewer",
            session_type=arguments.get("session_type", "review"),
            topic=root_cause[:200],
            max_synthesis_rounds=2,
            reason=root_cause,
            paused_agent_state=pause_evidence,
            trigger_source=trigger_source,
        )
        session_id = session.session_id

        await (create_session_fn or pg_create_session)(
            session_id=session_id,
            paused_agent_id=agent_uuid,
            reviewer_agent_id="llm-synthetic-reviewer",
            reason=root_cause,
            session_type=arguments.get("session_type", "review"),
            topic=root_cause[:200],
            max_synthesis_rounds=2,
            synthesis_round=0,
            paused_agent_state=pause_evidence,
            trigger_source=trigger_source,
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
        thesis_phase = session.phase.value
        thesis_revision = int(session.transcript_revision)
        session.submit_thesis(thesis_msg)
        await _append_protocol_message(
            session,
            expected_phase=thesis_phase,
            expected_reviewer=session.reviewer_agent_id,
            expected_transcript_revision=thesis_revision,
            next_phase=session.phase.value,
            next_synthesis_round=session.synthesis_round,
            agent_id=agent_uuid,
            message_type="thesis",
            root_cause=root_cause,
            proposed_conditions=proposed_conditions,
            reasoning=reasoning,
        )
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
        antithesis_phase = session.phase.value
        antithesis_revision = int(session.transcript_revision)
        session.submit_antithesis(anti_msg)
        await _append_protocol_message(
            session,
            expected_phase=antithesis_phase,
            expected_reviewer=session.reviewer_agent_id,
            expected_transcript_revision=antithesis_revision,
            next_phase=session.phase.value,
            next_synthesis_round=session.synthesis_round,
            agent_id="llm-synthetic-reviewer",
            message_type="antithesis",
            reasoning=anti_reasoning,
            concerns=anti_concerns or None,
        )
        # 3. Submit synthesis through protocol (agrees is bound below)
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
        synthesis_phase = session.phase.value
        synthesis_revision = int(session.transcript_revision)
        session.submit_synthesis(synth_msg)
        await _append_protocol_message(
            session,
            expected_phase=synthesis_phase,
            expected_reviewer=session.reviewer_agent_id,
            expected_transcript_revision=synthesis_revision,
            next_phase=(
                synthesis_phase if synth_agrees else session.phase.value
            ),
            next_synthesis_round=session.synthesis_round,
            agent_id="llm-synthetic-reviewer",
            message_type="synthesis",
            root_cause=synthesis.get("agreed_root_cause", ""),
            proposed_conditions=synth_conditions or None,
            reasoning=synthesis.get("reasoning", ""),
            agrees=synth_agrees,
        )

        # 4. Finalize resolution through protocol (canonical schema).
        # No party signature: party-HMAC minting is retired (#2449), so the
        # record carries signature_version 3 with both signatures empty and
        # reads as `unsigned` by design.
        # Only finalize when the reviewer actually agreed (phase reaches RESOLVED).
        # On COOLDOWN/ESCALATE the synthesis registered agrees=False, so the session
        # stays unresolved and is left for facilitation rather than force-resumed.
        if synth_agrees and session.phase == DialecticPhase.RESOLVED:
            resolution_obj = session.finalize_resolution()
            is_safe, hard_limit_violation = session.check_hard_limits(
                resolution_obj
            )
            if is_safe:
                session.resolution = resolution_obj
                commit = await _h_call("_commit_converged_resolution")(
                    session,
                    resolution_obj,
                    expected_transcript_revision=int(session.transcript_revision),
                )
            else:
                session.resolution = None
                commit = await _commit_terminal_resolution(
                    session,
                    {"action": "block", "reason": hard_limit_violation},
                    status=DialecticPhase.FAILED.value,
                    expected_transcript_revision=int(session.transcript_revision),
                )
            discard_receipt_unless_written(
                resolution_obj,
                written=commit.terminal_committed,
                had_receipt=False,
            )
            if commit.terminal_committed:
                terminal_committed = True
                if not is_safe:
                    governance_blocked = True
                    session.phase = DialecticPhase.FAILED
                    session.awaiting_facilitation = False
                else:
                    effect = await _h_call("_apply_durable_resolution_effect")(
                        session, resolution_obj
                    )
                    resolution_effect_state = effect.state
                    if effect.state == "applied":
                        persisted_resolution = True
                        logger.info(
                            "LLM dialectic session %s resolved and applied via protocol",
                            session_id,
                        )
                        execution_result = effect.execution_result or {}
                        newly_applied = bool(
                            effect.claimed
                            or (
                                commit.terminal_committed
                                and execution_result.get("no_op") is True
                            )
                        )
                        if newly_applied and not effect.claimed:
                            await record_resolution_outcome(
                                session, resolution_obj, execution_result
                            )
                        if newly_applied:
                            await _emit_dialectic_event(
                                "dialectic_resolved", session, action="resume"
                            )
                    else:
                        effect_pending = True
                        logger.warning(
                            "LLM dialectic session %s committed but effect is %s: %s",
                            session_id,
                            effect.state,
                            effect.error,
                        )
            elif commit.commit_unknown:
                commit_unknown = True
                effect_pending = True
                session.phase = DialecticPhase.SYNTHESIS
                session.resolution = None
                logger.warning(
                    "LLM dialectic session %s terminal commit outcome is unknown; "
                    "reload before further protocol writes",
                    session_id,
                )
            else:
                session.phase = DialecticPhase.SYNTHESIS
                session.resolution = None
                logger.warning(
                    "LLM dialectic session %s terminal write was refused; "
                    "left unresolved for facilitation",
                    session_id,
                )
                await _set_awaiting_facilitation(
                    session,
                    True,
                    reason="resolution_write_refused",
                )
                ACTIVE_SESSIONS.pop(session_id, None)
                return [error_response(
                    "The terminal decision was refused because the dialectic changed; reload before continuing.",
                    error_code="SESSION_RESOLUTION_WRITE_REFUSED",
                    error_category="governance_refusal",
                    details={
                        "session_id": session_id,
                        "commit_origin": commit.origin,
                        "terminal_committed": False,
                    },
                    arguments=arguments,
                )]
        else:
            awaiting_facilitation = True
            logger.info(
                f"LLM dialectic session {session_id}: reviewer did not approve "
                f"(recommendation={recommendation}); left unresolved for facilitation"
            )
            await _set_awaiting_facilitation(
                session, True, reason="reviewer_did_not_approve"
            )
        # Store in ACTIVE_SESSIONS regardless so the verdict is recorded.
        ACTIVE_SESSIONS[session_id] = session
    except Exception as e:
        discard_receipt_unless_written(getattr(session, "resolution", None), written=False, had_receipt=False)
        logger.warning(f"Could not persist LLM dialectic session: {e}")
        if session_id:
            ACTIVE_SESSIONS.pop(session_id, None)
        return [error_response(
            "The LLM review could not be persisted; no successful outcome is being reported.",
            error_code="DIALECTIC_PERSISTENCE_FAILED",
            error_category="system_error",
            details={"session_id": session_id} if session_id else None,
            arguments=arguments,
        )]

    if governance_blocked:
        response_message = (
            f"Dialectic terminally blocked by a hard limit: {hard_limit_violation}"
        )
        response_next_steps = [
            "Do not resume work from this review.",
            "Inspect the recorded terminal block and address its hard-limit reason before opening a new review.",
        ]
    elif commit_unknown:
        response_message = (
            "Dialectic terminal ownership is unknown; no successful outcome is being reported."
        )
        response_next_steps = [
            "Reload the dialectic before any further protocol write.",
            "Have an operator reconcile the durable resolution effect; do not resume work from this response.",
        ]
    elif effect_pending:
        response_message = (
            "The dialectic decision committed, but its runtime effect is not confirmed."
        )
        response_next_steps = [
            "Do not resume work until the durable effect is reconciled.",
            "Have an operator run dialectic(action='reconcile_effect') for this session.",
        ]
    elif awaiting_facilitation:
        response_message = (
            f"Dialectic review recorded recommendation {recommendation}, but it did not authorize a resolution."
        )
        response_next_steps = [
            "Do not treat the recommendation as permission to resume.",
            "Assign an independent reviewer or ask an operator to facilitate the session.",
        ]
    else:
        response_message = f"Dialectic complete. Recommendation: {recommendation}"
        response_next_steps = _get_dialectic_next_steps(recommendation)

    response_data = {
        "success": (
            not governance_blocked
            and not effect_pending
            and not commit_unknown
        ),
        "message": response_message,
        "recommendation": recommendation,
        "session_id": session_id,
        "resolved": persisted_resolution,
        "terminal_committed": (
            terminal_committed if not commit_unknown else None
        ),
        "commit_unknown": commit_unknown,
        "effect_pending": effect_pending,
        "resolution_effect_state": resolution_effect_state,
        "awaiting_facilitation": awaiting_facilitation,
        "hard_limit_blocked": governance_blocked,
        "hard_limit_violation": hard_limit_violation,
        "reviewed_work_authorship": reviewed_work_authorship.as_dict(),
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
        "next_steps": response_next_steps,
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
        if governance_blocked:
            discovery_outcome = "hard-limit-blocked"
        elif commit_unknown:
            discovery_outcome = "terminal-commit-unknown"
        elif effect_pending:
            discovery_outcome = "effect-pending"
        elif persisted_resolution:
            discovery_outcome = "resolution-applied"
        elif awaiting_facilitation:
            discovery_outcome = "awaiting-facilitation"
        else:
            discovery_outcome = "review-only"
        discovery_tags = [
            "dialectic",
            "llm-assisted",
            discovery_outcome,
        ]
        if persisted_resolution:
            discovery_tags.extend(["recovery", recommendation.lower()])
        else:
            discovery_tags.append("non-authorizing")

        discovery = DiscoveryNode(
            id=_dt.now().isoformat(),
            agent_id=agent_uuid,
            summary=f"LLM dialectic: {root_cause[:80]}... → {discovery_outcome}",
            type="dialectic_synthesis",
            tags=discovery_tags,
            details=json.dumps({
                "thesis": thesis,
                "antithesis": result.get("antithesis"),
                "synthesis": synthesis,
                "recommendation": recommendation,
                "outcome": discovery_outcome,
                "success": response_data["success"],
                "resolved": persisted_resolution,
                "terminal_committed": response_data["terminal_committed"],
                "hard_limit_blocked": governance_blocked,
                "commit_unknown": commit_unknown,
                "effect_pending": effect_pending,
                "resolution_effect_state": resolution_effect_state,
            }, indent=2),
            provenance=tag_provenance_source(None, "dialectic_synthesis"),
        )
        await graph.add_discovery(discovery)
        response_data["discovery_stored"] = True
    except Exception as e:
        logger.debug(f"Could not store dialectic discovery: {e}")

    return success_response(response_data, agent_id=agent_uuid, arguments=arguments)
