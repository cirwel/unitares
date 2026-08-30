"""
Auto-Resolve Stuck Dialectic Sessions

Automatically handles sessions that are stuck/inactive for >2 hours.
First attempts reviewer re-assignment, then marks awaiting facilitation,
and only fails sessions after extended inactivity (4+ hours total).
"""

from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Dict, Any, Optional

from src.dialectic_protocol import DialecticPhase
from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
from .events import (
    ATTEMPT_AWAITING_FACILITATION,
    ATTEMPT_REAP_FAILED,
    ATTEMPT_REVIEWER_REASSIGNMENT,
    emit_facilitation_needed,
    emit_reviewer_reassigned,
    emit_sweep_cycle,
    emit_write_refused,
    emit_write_overlap,
)
from .session import ACTIVE_SESSIONS
from .sweep_context import AUTO_RESOLVE_IN_PROGRESS
from src.dialectic_db import (
    get_active_sessions_async,
    update_session_status_async,
    update_session_reviewer_async,
    update_session_awaiting_facilitation_async,
    mark_awaiting_facilitation_async,
    add_message_async,
    has_inflight_saga_async,
    probe_inflight_saga_async,
)

logger = get_logger(__name__)

# Stuck session threshold: 2 hours of inactivity
# Rationale: DialecticProtocol.MAX_ANTITHESIS_WAIT is 2 hours - agents need time to think
STUCK_SESSION_THRESHOLD = timedelta(hours=2)

# Extended threshold before marking FAILED (gives human time to facilitate)
FACILITATION_TIMEOUT = timedelta(hours=4)

# Fetch one extra row so a full maintenance batch is distinguishable from the
# complete active set. The overflow row is not processed in this cycle.
SWEEP_BATCH_SIZE = 100


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



def _sync_cached_session(session_id: str, **fields) -> None:
    """Mirror a committed sweeper write into the in-process session cache.

    The sweeper writes straight to PostgreSQL, and `ACTIVE_SESSIONS` is never
    evicted — no writer in `src/` removes an entry — so a session this process
    already holds keeps whatever state Python last set on it and never learns
    what the sweeper committed. Every reader that ANSWERS a facilitation
    request is cache-first: `handle_reassign_reviewer` reads `ACTIVE_SESSIONS`
    before the database, and `_apply_reviewer_reassignment` decides revival
    from `session.awaiting_facilitation` and `session.phase`. Unmirrored, a
    sweeper-raised request is answerable only from a process that has never
    seen the session — and in the process that raised it, the reassignment
    succeeds in memory while the guarded UPDATE refuses the terminal row.

    `phase` is accepted as the string the row carries and converted; an
    unrecognised value is skipped rather than stored, so a cache entry never
    ends up holding a phase the protocol cannot act on. Best-effort by design:
    the row is authoritative and already committed, so a failure here must not
    unwind it.
    """
    cached = ACTIVE_SESSIONS.get(session_id)
    if cached is None:
        return
    for name, value in fields.items():
        if name == "phase":
            try:
                value = DialecticPhase(value)
            except ValueError:
                logger.debug(
                    f"_sync_cached_session: {session_id[:16]}... unknown phase {value!r}; "
                    "cache left as-is"
                )
                continue
        setattr(cached, name, value)


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


async def _probe_write_overlap(
    session_id: str,
    attempted: str,
    paused_agent_id: Optional[str],
) -> str:
    """Re-check for a saga immediately after a guarded write succeeded.

    The early saga guard ran before `select_reviewer` and several DB round
    trips. If a saga appears between that check and here, the sweeper wrote
    first and BEAM is now acting on a row it did not own when the sweeper
    decided -- the one dual-writer ordering neither the early skip count nor
    the refusal event can see.

    Returns ``"detected"``, ``"clean"``, or ``"probe_failed"``. ⛔The third is
    not the second: `probe_inflight_saga_async` returns None when it could not
    look, and counting that as clean would turn an outage into evidence of a
    collision-free system.

    Never raises. A measurement failure must not fail a sweep whose write has
    already committed.
    """
    try:
        found = await probe_inflight_saga_async(session_id)
    except Exception as exc:
        logger.warning(
            f"overlap probe raised for {session_id[:16]}...: {exc}"
        )
        return "probe_failed"

    if found is None:
        return "probe_failed"
    if not found:
        return "clean"

    logger.info(
        f"Session {session_id[:16]} saga appeared AFTER a successful sweeper "
        f"{attempted} write — sweeper-first overlap"
    )
    await emit_write_overlap(
        session_id=session_id,
        attempted=attempted,
        paused_agent_id=paused_agent_id,
        source="sweeper",
    )
    return "detected"


async def _auto_resolve_stuck_sessions() -> Dict[str, Any]:
    """
    Handle sessions that are stuck/inactive.

    For each stuck session:
    1. If reviewer is gone and phase is ANTITHESIS: try auto re-assignment
    2. If no replacement available: mark awaiting_facilitation (not FAILED)
    3. Only mark FAILED after extended inactivity (4+ hours)

    Returns:
        Dict with counts of resolved/reassigned sessions and details
    """
    active_session_count = 0
    active_session_batch_truncated = False
    stuck_session_count = 0
    invalid_session_count = 0
    saga_inflight_skip_count = 0
    write_attempt_count = 0
    overlap_detected_count = 0
    overlap_probe_failed_count = 0
    resolved_count = 0
    reassigned_count = 0
    facilitation_count = 0
    skipped_count = 0
    details = []

    try:
        now = datetime.now(timezone.utc)
        threshold_time = now - STUCK_SESSION_THRESHOLD
        fail_time = now - FACILITATION_TIMEOUT

        active_sessions = await get_active_sessions_async(
            limit=SWEEP_BATCH_SIZE + 1,
            least_recently_updated_first=True,
        )
        active_session_batch_truncated = len(active_sessions) > SWEEP_BATCH_SIZE
        if active_session_batch_truncated:
            active_sessions = active_sessions[:SWEEP_BATCH_SIZE]
            logger.warning(
                "Dialectic sweep active-session batch truncated at %s rows; "
                "least-recently-updated rows were prioritized",
                SWEEP_BATCH_SIZE,
            )
        active_session_count = len(active_sessions)

        if not active_sessions:
            return {
                "resolved_count": 0,
                "reassigned_count": 0,
                "facilitation_count": 0,
                "skipped_count": 0,
                "active_session_count": 0,
                "active_session_batch_truncated": False,
                "stuck_session_count": 0,
                "invalid_session_count": 0,
                "saga_inflight_skip_count": 0,
                "write_attempt_count": 0,
                "overlap_detected_count": 0,
                "overlap_probe_failed_count": 0,
                "details": [],
                "message": "No active sessions found"
            }

        # Filter to stuck sessions (inactive for >2 hours)
        stuck_sessions = []
        for session in active_sessions:
            check_time = _parse_timestamp(session.get("updated_at") or session.get("created_at"))
            if check_time and check_time < threshold_time:
                stuck_sessions.append(session)
        stuck_session_count = len(stuck_sessions)

        if not stuck_sessions:
            return {
                "resolved_count": 0,
                "reassigned_count": 0,
                "facilitation_count": 0,
                "skipped_count": 0,
                "active_session_count": active_session_count,
                "active_session_batch_truncated": active_session_batch_truncated,
                "stuck_session_count": 0,
                "invalid_session_count": 0,
                "saga_inflight_skip_count": 0,
                "write_attempt_count": 0,
                "overlap_detected_count": 0,
                "overlap_probe_failed_count": 0,
                "details": [],
                "message": "No stuck sessions found"
            }

        for session in stuck_sessions:
            session_id = session.get("session_id")
            paused_agent_id = session.get("paused_agent_id")
            reviewer_agent_id = session.get("reviewer_agent_id")
            phase = session.get("phase")
            awaiting_facilitation = bool(session.get("awaiting_facilitation"))

            if not session_id:
                invalid_session_count += 1
                continue

            # Saga-inflight guard (C1, council 2026-06-28): if a BEAM session
            # owner is mid-resolution for this session, skip it entirely this
            # cycle. Marking it failed / reassigning its reviewer here would race
            # the saga and corrupt the outcome. Fail-open (no saga infra -> no
            # skip), so this is a no-op until BEAM begins writing sagas.
            if await has_inflight_saga_async(session_id):
                saga_inflight_skip_count += 1
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
                            write_attempt_count += 1
                            if not await update_session_reviewer_async(session_id, new_reviewer):
                                # The guarded UPDATE wrote nothing — the row
                                # is terminal (dual-writer TOCTOU during
                                # reviewer selection) or gone; the DB-layer
                                # log distinguishes which. Don't narrate a
                                # reassignment that never happened.
                                logger.info(
                                    f"Session {session_id[:16]} reviewer write refused "
                                    "(row terminal or missing); reassignment skipped"
                                )
                                skipped_count += 1
                                details.append({
                                    "session_id": session_id,
                                    "action": "write_refused",
                                    "attempted": ATTEMPT_REVIEWER_REASSIGNMENT,
                                })
                                await emit_write_refused(
                                    session_id=session_id,
                                    attempted=ATTEMPT_REVIEWER_REASSIGNMENT,
                                    paused_agent_id=paused_agent_id,
                                    source="sweeper",
                                )
                                continue
                            # ⛔EMIT IMMEDIATELY AFTER THE WRITE COMMITS, and
                            # before the transcript append. The (F)
                            # reassignment-rate baseline is computed from this
                            # stream, and until 2026-08-22 the auto path
                            # emitted nothing at all while a comment in
                            # handlers.py called the other producer "the single
                            # chokepoint".
                            #
                            # ⛔Order is load-bearing (review 2026-08-22). An
                            # earlier draft emitted after `add_message_async`,
                            # inside the same try — so a transcript failure on
                            # an ALREADY-COMMITTED reassignment unwound to the
                            # except, which has no `continue`, and fell through
                            # to the facilitation branch below: no event, no
                            # count, and a facilitation message naming the
                            # stale reviewer. `persisted ⇒ recorded` is the
                            # direction this metric needs; the converse is
                            # already guaranteed by the refusal check above.
                            _overlap = await _probe_write_overlap(
                                session_id, ATTEMPT_REVIEWER_REASSIGNMENT, paused_agent_id
                            )
                            if _overlap == "detected":
                                overlap_detected_count += 1
                            elif _overlap == "probe_failed":
                                overlap_probe_failed_count += 1
                            await emit_reviewer_reassigned(
                                session_id=session_id,
                                old_reviewer_id=reviewer_agent_id,
                                new_reviewer_id=new_reviewer,
                                reason="reviewer_unresponsive",
                                source="sweeper",
                            )
                            try:
                                await add_message_async(
                                    session_id=session_id,
                                    agent_id="system",
                                    message_type="system",
                                    reasoning=f"Reviewer auto-reassigned: {reviewer_agent_id} -> {new_reviewer} (previous reviewer unresponsive)",
                                )
                            except Exception as msg_exc:
                                # Narration only. The reassignment is committed
                                # and recorded; do not unwind it.
                                logger.warning(
                                    f"Reassignment transcript append failed for "
                                    f"{session_id[:16]}: {msg_exc}"
                                )
                            # A reassignment ANSWERS a standing request, so
                            # the request must not outlive it. The handler
                            # path clears the flag deliberately (#1167); this
                            # one never did, which was harmless only while the
                            # sweeper could not raise the flag itself. A stale
                            # `awaiting_facilitation` makes a later ordinary
                            # failure revivable by `reassign` — exactly the
                            # hazard `mark_awaiting_facilitation` is guarded
                            # against creating.
                            if awaiting_facilitation:
                                try:
                                    await update_session_awaiting_facilitation_async(
                                        session_id, False
                                    )
                                except Exception as clear_exc:
                                    logger.warning(
                                        f"Could not clear awaiting_facilitation for "
                                        f"{session_id[:16]}: {clear_exc}"
                                    )
                            _sync_cached_session(
                                session_id,
                                reviewer_agent_id=new_reviewer,
                                awaiting_facilitation=False,
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

                    # No replacement found — record a standing facilitation
                    # request if not too old.
                    #
                    # ⛔PERSIST THE FLAG, don't just narrate it. Until 2026-08-26
                    # this branch appended the message, counted a facilitation
                    # and returned — while `awaiting_facilitation` stayed false
                    # in the row, because nothing here wrote it. Two costs, both
                    # measured by replaying the sweeper over one stuck session:
                    #
                    #   1. `add_message` inserts into dialectic_messages and does
                    #      NOT touch dialectic_sessions.updated_at (no trigger;
                    #      migration 003), so the row kept looking stuck and this
                    #      branch re-fired every sweep — three cycles, three
                    #      identical transcript messages, `facilitation_count`
                    #      counting cycles rather than sessions.
                    #   2. At the 4h timeout the row was reaped with
                    #      `awaiting_facilitation=false`, so `reopen_session` and
                    #      `_apply_reviewer_reassignment` — both of which key on
                    #      that flag — read it as an ordinary failure and refused
                    #      to revive it. That is the same dead-end #1577 closed
                    #      for requests raised at THESIS through the handler;
                    #      requests the SWEEPER raises at ANTITHESIS never had
                    #      the flag to be rescued by.
                    #
                    # The re-entry guard is what keeps the request to one
                    # message and one count: `mark_awaiting_facilitation`
                    # deliberately leaves `updated_at` alone (see its
                    # docstring), so the row stays in the stuck set and this
                    # branch is re-entered on every sweep — which is what keeps
                    # `select_reviewer` retrying while a human is waited on.
                    if check_time and check_time > fail_time and not awaiting_facilitation:
                        try:
                            write_attempt_count += 1
                            recorded = await mark_awaiting_facilitation_async(session_id)
                        except Exception as e:
                            # Guarded like the neighbouring DB writes: this
                            # runs inside the per-session loop of a sweep that
                            # has already committed reaps, and letting it reach
                            # the outer handler would discard their counts and
                            # report the whole cycle as an error. Skip the
                            # session; the next sweep retries it.
                            logger.warning(
                                f"Could not record facilitation request for "
                                f"{session_id[:16]}: {e}"
                            )
                            continue
                        if not recorded:
                            # Guarded UPDATE wrote nothing — another writer
                            # finished this session (dual-writer TOCTOU) or the
                            # row is gone. Same posture as the refused reviewer
                            # write above: don't narrate, don't count.
                            logger.info(
                                f"Session {session_id[:16]} facilitation write refused "
                                "(row terminal or missing); request not recorded"
                            )
                            skipped_count += 1
                            details.append({
                                "session_id": session_id,
                                "action": "write_refused",
                                "attempted": ATTEMPT_AWAITING_FACILITATION,
                            })
                            await emit_write_refused(
                                session_id=session_id,
                                attempted=ATTEMPT_AWAITING_FACILITATION,
                                paused_agent_id=paused_agent_id,
                                source="sweeper",
                            )
                            continue
                        _sync_cached_session(session_id, awaiting_facilitation=True)
                        _overlap = await _probe_write_overlap(
                            session_id, ATTEMPT_AWAITING_FACILITATION, paused_agent_id
                        )
                        if _overlap == "detected":
                            overlap_detected_count += 1
                        elif _overlap == "probe_failed":
                            overlap_probe_failed_count += 1
                        await emit_facilitation_needed(
                            session_id=session_id,
                            paused_agent_id=paused_agent_id,
                            phase=phase,
                            reason="reviewer_unresponsive",
                        )
                        try:
                            await add_message_async(
                                session_id=session_id,
                                agent_id="system",
                                message_type="system",
                                reasoning=f"Reviewer '{reviewer_agent_id}' unresponsive. Awaiting human facilitation.",
                            )
                        except Exception as e:
                            # Narration only. The request is committed; do not
                            # unwind it, and do not fall through to the reap.
                            logger.warning(f"Could not add facilitation message for {session_id[:16]}: {e}")
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
                write_attempt_count += 1
                if not await update_session_status_async(session_id, "failed"):
                    # The guarded UPDATE wrote nothing: another writer
                    # finished this session after our early saga/staleness
                    # checks (even one that also wrote 'failed' — that
                    # outcome is theirs, with their resolution payload), or
                    # the row is gone. The DB-layer log distinguishes which.
                    # Skip the failure narrative and the count.
                    logger.info(
                        f"Session {session_id[:16]} status write refused "
                        "(row terminal or missing); reap skipped"
                    )
                    skipped_count += 1
                    details.append({
                        "session_id": session_id,
                        "action": "write_refused",
                        "attempted": ATTEMPT_REAP_FAILED,
                    })
                    await emit_write_refused(
                        session_id=session_id,
                        attempted=ATTEMPT_REAP_FAILED,
                        paused_agent_id=paused_agent_id,
                        source="sweeper",
                    )
                    continue
                # `update_session_status` writes status AND phase; mirror the
                # reap so a cached session in this process does not go on
                # reading as live. Without it `_apply_reviewer_reassignment`
                # sees a non-terminal phase, skips `reopen_session`, and the
                # guarded reviewer write is then refused by the row it never
                # reopened — the standing request answered in memory only.
                _sync_cached_session(session_id, phase="failed")
                _overlap = await _probe_write_overlap(
                    session_id, ATTEMPT_REAP_FAILED, paused_agent_id
                )
                if _overlap == "detected":
                    overlap_detected_count += 1
                elif _overlap == "probe_failed":
                    overlap_probe_failed_count += 1
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
            "skipped_count": skipped_count,
            "active_session_count": active_session_count,
            "active_session_batch_truncated": active_session_batch_truncated,
            "stuck_session_count": stuck_session_count,
            "invalid_session_count": invalid_session_count,
            "saga_inflight_skip_count": saga_inflight_skip_count,
            "write_attempt_count": write_attempt_count,
            "overlap_detected_count": overlap_detected_count,
            "overlap_probe_failed_count": overlap_probe_failed_count,
            "details": details,
            "message": (
                f"Processed {len(stuck_sessions)} stuck session(s): "
                f"{reassigned_count} reassigned, {facilitation_count} awaiting facilitation, "
                f"{resolved_count} failed, {skipped_count} skipped (write refused)"
            ),
        }

    except Exception as e:
        logger.error(f"Error auto-resolving stuck sessions: {e}", exc_info=True)
        return {
            # Earlier iterations may already have committed. Preserve their
            # outcome evidence instead of turning a partial cycle into an
            # all-zero one because a later row aborted the scan.
            "resolved_count": resolved_count,
            "reassigned_count": reassigned_count,
            "facilitation_count": facilitation_count,
            "skipped_count": skipped_count,
            "active_session_count": active_session_count,
            "active_session_batch_truncated": active_session_batch_truncated,
            "stuck_session_count": stuck_session_count,
            "invalid_session_count": invalid_session_count,
            "saga_inflight_skip_count": saga_inflight_skip_count,
            "write_attempt_count": write_attempt_count,
            "overlap_detected_count": overlap_detected_count,
            "overlap_probe_failed_count": overlap_probe_failed_count,
            "details": details,
            "error": str(e),
            "message": "Failed to auto-resolve stuck sessions"
        }


async def auto_resolve_stuck_sessions(
    *, trigger_source: str = "direct"
) -> Dict[str, Any]:
    """Run one resolver cycle under the shared task-local reentrancy guard.

    Both the periodic task and the lazy active-session pre-check enter here.
    Owning the ContextVar at this common boundary prevents reviewer selection
    inside a direct/background cycle from recursively starting another cycle.
    It does not serialize independent processes or replace the database write
    guards.

    Every real invocation emits one zero-inclusive cycle event. A nested call
    is suppressed rather than counted as a cycle because it did no scan and
    would corrupt the telemetry denominator.
    """
    if AUTO_RESOLVE_IN_PROGRESS.get():
        logger.debug(
            "Dialectic stuck-session resolver re-entry suppressed (source=%s)",
            trigger_source,
        )
        return {
            "resolved_count": 0,
            "reassigned_count": 0,
            "facilitation_count": 0,
            "skipped_count": 0,
            "active_session_count": 0,
            "active_session_batch_truncated": False,
            "stuck_session_count": 0,
            "invalid_session_count": 0,
            "saga_inflight_skip_count": 0,
            "write_attempt_count": 0,
            "overlap_detected_count": 0,
            "overlap_probe_failed_count": 0,
            "details": [],
            "reentrant_suppressed": True,
            "message": "Resolver re-entry suppressed",
        }

    token = AUTO_RESOLVE_IN_PROGRESS.set(True)
    started = monotonic()
    result: Dict[str, Any] | None = None
    try:
        result = await _auto_resolve_stuck_sessions()
        return result
    finally:
        elapsed_ms = max(0, round((monotonic() - started) * 1000))
        cycle = result or {}
        try:
            await emit_sweep_cycle(
                trigger_source=trigger_source,
                active_session_count=int(cycle.get("active_session_count", 0) or 0),
                active_session_batch_truncated=bool(
                    cycle.get("active_session_batch_truncated", False)
                ),
                stuck_session_count=int(cycle.get("stuck_session_count", 0) or 0),
                invalid_session_count=int(cycle.get("invalid_session_count", 0) or 0),
                saga_inflight_skip_count=int(
                    cycle.get("saga_inflight_skip_count", 0) or 0
                ),
                write_attempt_count=int(cycle.get("write_attempt_count", 0) or 0),
                write_refused_count=int(cycle.get("skipped_count", 0) or 0),
                overlap_detected_count=int(
                    cycle.get("overlap_detected_count", 0) or 0
                ),
                overlap_probe_failed_count=int(
                    cycle.get("overlap_probe_failed_count", 0) or 0
                ),
                resolved_count=int(cycle.get("resolved_count", 0) or 0),
                reassigned_count=int(cycle.get("reassigned_count", 0) or 0),
                facilitation_count=int(cycle.get("facilitation_count", 0) or 0),
                duration_ms=elapsed_ms,
                error=str(cycle["error"]) if cycle.get("error") else None,
            )
        finally:
            AUTO_RESOLVE_IN_PROGRESS.reset(token)


async def check_and_resolve_stuck_sessions() -> Dict[str, Any]:
    """
    Check for stuck sessions and auto-resolve them.
    Called automatically when checking for active sessions.

    Returns:
        Dict with resolution results
    """
    try:
        return await auto_resolve_stuck_sessions(trigger_source="active_session_check")
    except Exception as e:
        logger.warning(f"Could not auto-resolve stuck sessions: {e}")
        return {"resolved_count": 0, "reassigned_count": 0, "error": str(e)}
