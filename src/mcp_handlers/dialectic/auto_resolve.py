"""
Auto-Resolve Stuck Dialectic Sessions

Automatically handles sessions that are stuck/inactive for >2 hours.
At ANTITHESIS it first attempts reviewer re-assignment, then marks awaiting
facilitation; at SYNTHESIS it marks awaiting facilitation without reassigning
(the verdict's author keeps review authority). Sessions are only failed after
extended inactivity (4+ hours total).
"""

import asyncio
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Awaitable, Callable, Dict, Any, List, Optional

from src.dialectic_protocol import DialecticPhase
from src.dialectic_session_writes import attempt_id_scope, session_write_via
from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
from .events import (
    ATTEMPT_AWAITING_FACILITATION,
    ATTEMPT_REAP_FAILED,
    ATTEMPT_REVIEWER_REASSIGNMENT,
    emit_facilitation_needed,
    emit_guarded_write,
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
    get_session_async,
    has_inflight_saga_async,
    probe_saga_since_async,
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


@dataclass
class _CycleCounts:
    """Every count one resolver cycle reports, held outside the cycle's frame.

    The counts live here rather than in `_auto_resolve_stuck_sessions`'s
    locals so a cycle cut short by the periodic timeout still reports what it
    had committed: the caller owns this object and reads it after the
    cancellation, when the cycle's own frame is gone.

    Two identities hold whenever no guarded write is mid-flight (see
    `emit_sweep_cycle`); `_attempt_guarded_write` is the only code that moves
    the write and probe counts, so it keeps them.
    """

    active_session_count: int = 0
    active_session_batch_truncated: bool = False
    stuck_session_count: int = 0
    invalid_session_count: int = 0
    saga_inflight_skip_count: int = 0
    write_attempt_count: int = 0
    write_succeeded_count: int = 0
    write_error_count: int = 0
    overlap_clean_count: int = 0
    overlap_detected_count: int = 0
    overlap_probe_failed_count: int = 0
    resolved_count: int = 0
    reassigned_count: int = 0
    facilitation_count: int = 0
    # Guarded writes the database refused; reported as write_refused_count.
    skipped_count: int = 0
    # Joins this cycle's row to its `dialectic_guarded_write` rows.
    cycle_id: str = ""
    # The per-write row owed but not yet written: set before every await that
    # could be cancelled between a counted attempt and its row, cleared once
    # the row is written. A cycle cut short emits it afterwards, so the
    # per-write rows still sum to the cycle's counts. Not a count; never
    # reported in the result.
    pending_row: Optional[Dict[str, Any]] = field(default=None, repr=False, compare=False)

    def as_result(
        self,
        details: List[Dict[str, Any]],
        message: str,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            f.name: getattr(self, f.name) for f in fields(self) if f.name != "pending_row"
        }
        result["details"] = details
        result["message"] = message
        if error is not None:
            result["error"] = error
        return result


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

    Deliberately does NOT claim a verdict. The sweeper reads the transcript
    only to decide whether a SYNTHESIS stall is waiting on its reviewer
    (`_synthesis_reviewer_owes_reply`), and this text does not use that read:
    whose move it is says nothing about who was right, and asserting a verdict
    would trade one confident wrong sentence for another. It reports what it
    observed and points at the record that has the rest.
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
    *,
    early_check_ts: Optional[datetime] = None,
    commit_ts: Optional[datetime] = None,
) -> str:
    """Look for a saga on the session immediately after a guarded write landed.

    The early saga guard ran before `select_reviewer` and several DB round
    trips. If a saga began after that check, the sweeper wrote while BEAM was
    also acting on the row -- the dual-writer ordering neither the early skip
    count nor the refusal event can see.

    ⛔Time-correlated, not state-matched (instrument v2; Wave 3 gate council
    2026-09-27, finding B1). Sagas commit in milliseconds, so a probe that only
    matches non-terminal states misses every saga that started and finished
    between the early check and here. `probe_saga_since_async` returns any
    saga created at or after ``early_check_ts``, whatever state it reached.

    Returns ``"detected"``, ``"clean"``, or ``"probe_failed"``. ⛔The third is
    not the second: the probe returns None when it could not look, and counting
    that as clean would turn an outage into evidence of a collision-free
    system.

    Never raises an ``Exception``. A measurement failure must not fail a sweep
    whose write has already committed.
    """
    try:
        found = await probe_saga_since_async(session_id, early_check_ts)
    except Exception as exc:
        logger.warning(
            f"overlap probe raised for {session_id[:16]}...: {exc}"
        )
        return "probe_failed"

    if not isinstance(found, dict):
        # None is the probe's own "could not look"; anything else that is not
        # a row is an answer this code cannot read, which is the same state.
        return "probe_failed"
    if not found:
        return "clean"

    logger.info(
        f"Session {session_id[:16]} saga {found.get('state')!r} seen after a "
        f"successful sweeper {attempted} write (created at or after the early "
        "check) — sweeper-first overlap"
    )
    await emit_write_overlap(
        session_id=session_id,
        attempted=attempted,
        paused_agent_id=paused_agent_id,
        source="sweeper",
        saga=found,
        early_check_ts=early_check_ts,
        commit_ts=commit_ts,
    )
    return "detected"


async def _attempt_guarded_write(
    counts: _CycleCounts,
    details: List[Dict[str, Any]],
    *,
    attempted: str,
    session: Dict[str, Any],
    write: Callable[[Dict[str, Any]], Awaitable[Any]],
    decision_read_ts: Optional[datetime],
    early_check_ts: Optional[datetime],
    new_reviewer_agent_id: Optional[str] = None,
) -> bool:
    """Run one guarded write and account for it completely.

    The single place the write and probe counts move, which is what makes
    every cycle row balance (see `emit_sweep_cycle`):

    * attempted = succeeded + refused + error, where "error" is a write that
      raised -- an ``Exception`` or a cancellation from the periodic cycle
      timeout. Its effect is unknown, so it is neither of the other two.
    * succeeded = clean + detected + probe_failed: every write that landed is
      probed exactly once, and an interrupted probe counts as failed.

    It also owes one `dialectic_guarded_write` row per attempt, carrying the
    state the sweeper read (``session``) and the times that place the write,
    so those rows sum to the cycle row's counts. The row is written here,
    except when a cancellation cuts the attempt short: then it is left in
    ``counts.pending_row`` and `auto_resolve_stuck_sessions` writes it once
    the cancellation has completed.

    ``write`` receives a dict the DB helper fills with the refusing row's
    status and reason (`DialecticDB._record_winner`); the helper's boolean
    answer is unchanged.

    Returns True only when this call performed the write. Re-raises whatever
    the write raised, after counting it, so each call site keeps its own
    recovery path.
    """
    session_id = session.get("session_id")
    paused_agent_id = session.get("paused_agent_id")
    # One id for this attempt, shared by the sweeper's own row and the
    # `dialectic_session_write` pair the DB helper records for the same write.
    attempt_id = str(uuid.uuid4())
    record = dict(
        session_id=session_id,
        attempted=attempted,
        attempt_id=attempt_id,
        cycle_id=counts.cycle_id or None,
        decision_read_ts=decision_read_ts,
        early_check_ts=early_check_ts,
        read_state=session,
        paused_agent_id=paused_agent_id,
        new_reviewer_agent_id=new_reviewer_agent_id,
    )

    async def _write_row(**outcome: Any) -> None:
        # Owed before the await, cleared after: a cancellation inside the emit
        # leaves the row for the cycle's caller to write (see `pending_row`).
        counts.pending_row = {**record, **outcome}
        await emit_guarded_write(**counts.pending_row)
        counts.pending_row = None

    counts.write_attempt_count += 1
    winner: Dict[str, Any] = {}
    # Until the write returns, its outcome is unknown: if the cycle is cut
    # short here, the row it is owed says so.
    counts.pending_row = {**record, "outcome": "error", "commit_ts": None,
                          "error": "interrupted"}
    try:
        with attempt_id_scope(attempt_id):
            written = await write(winner)
    except Exception as exc:
        counts.write_error_count += 1
        await _write_row(
            outcome="error",
            commit_ts=datetime.now(timezone.utc),
            error=type(exc).__name__,
        )
        raise
    except BaseException:
        # Cancellation (the periodic timeout) or interpreter exit. Counted so
        # the row still balances; the per-write row stays pending and is
        # written by the cycle's caller once the cancellation has completed,
        # because awaiting an audit write mid-cancellation could hang the
        # loop the timeout exists to free.
        counts.write_error_count += 1
        raise
    commit_ts = datetime.now(timezone.utc)

    if not written:
        counts.skipped_count += 1
        counts.pending_row = {
            **record, "outcome": "refused", "commit_ts": commit_ts,
            "winner_status": winner.get("winner_status"),
            "winner_reason": winner.get("winner_reason"),
        }
        details.append({
            "session_id": session_id,
            "action": "write_refused",
            "attempted": attempted,
            "winner_status": winner.get("winner_status"),
        })
        await emit_write_refused(
            session_id=session_id,
            attempted=attempted,
            paused_agent_id=paused_agent_id,
            source="sweeper",
            winner_status=winner.get("winner_status"),
            winner_reason=winner.get("winner_reason"),
        )
        await _write_row(
            outcome="refused",
            commit_ts=commit_ts,
            winner_status=winner.get("winner_status"),
            winner_reason=winner.get("winner_reason"),
        )
        return False

    counts.write_succeeded_count += 1
    counts.pending_row = {**record, "outcome": "succeeded", "commit_ts": commit_ts,
                          "probe_outcome": "probe_failed"}
    try:
        probe = await _probe_write_overlap(
            session_id,
            attempted,
            paused_agent_id,
            early_check_ts=early_check_ts,
            commit_ts=commit_ts,
        )
    except BaseException:
        counts.overlap_probe_failed_count += 1
        raise
    if probe == "detected":
        counts.overlap_detected_count += 1
    elif probe == "clean":
        counts.overlap_clean_count += 1
    else:
        counts.overlap_probe_failed_count += 1
    await _write_row(outcome="succeeded", commit_ts=commit_ts, probe_outcome=probe)
    return True


async def _synthesis_reviewer_owes_reply(
    session_id: str,
    paused_agent_id: Optional[str],
    reviewer_agent_id: str,
) -> bool:
    """True when a SYNTHESIS session is waiting on its REVIEWER.

    Uses the handlers' own turn predicates, the ones `whose_move` is built
    from, so the sweeper and the agent-facing answer cannot disagree. The
    reviewer owes the move when its FIRST synthesis verdict is pending (an
    independent antithesis and no verdict yet; `submit_antithesis` enters
    SYNTHESIS before that verdict), or when its standing objection is
    answered by the paused agent's latest synthesis (reconsideration owed).
    A self-review has no separate reviewer to wait on.

    Never raises. A failed read answers False, which leaves the row on its
    pre-#2202 path rather than raising a flag the sweeper cannot justify.
    """
    if not paused_agent_id or paused_agent_id == reviewer_agent_id:
        return False
    try:
        # Function-local, like the select_reviewer import in the sweeper:
        # handlers is heavy and reaches reviewer.py, which imports this
        # module. Inside the try so a failed import answers False for this
        # row instead of aborting the whole sweep.
        from .handlers import (
            _latest_synthesis_agent_in_session_data,
            _reviewer_objection_stands_in_session_data,
            _reviewer_verdict_pending_in_session_data,
        )
        row = dict(await get_session_async(session_id) or {})
        row["paused_agent_id"] = paused_agent_id
        row["reviewer_agent_id"] = reviewer_agent_id
        if _reviewer_verdict_pending_in_session_data(row):
            return True
        return bool(
            _reviewer_objection_stands_in_session_data(row)
            and _latest_synthesis_agent_in_session_data(row) == paused_agent_id
        )
    except Exception as exc:
        logger.warning(
            f"Could not decide SYNTHESIS ownership for {session_id[:16]}: {exc}"
        )
        return False


async def _auto_resolve_stuck_sessions(
    counts: Optional[_CycleCounts] = None,
) -> Dict[str, Any]:
    """
    Handle sessions that are stuck/inactive.

    For each stuck session:
    1. If reviewer is gone and phase is ANTITHESIS: try auto re-assignment
    2. If no replacement available: mark awaiting_facilitation (not FAILED)
    3. If phase is SYNTHESIS: mark awaiting_facilitation, never reassign
    4. Only mark FAILED after extended inactivity (4+ hours)

    ``counts`` is owned by the caller so a cycle cancelled by the periodic
    timeout still reports what it committed (see `_CycleCounts`).

    Returns:
        Dict with counts of resolved/reassigned sessions and details
    """
    c = counts if counts is not None else _CycleCounts()
    details: List[Dict[str, Any]] = []

    try:
        now = datetime.now(timezone.utc)
        threshold_time = now - STUCK_SESSION_THRESHOLD
        fail_time = now - FACILITATION_TIMEOUT

        # The decision read: every per-session decision below is made on row
        # state read by this call, so the time taken just before it is the
        # lower bound of "the sweeper acted on stale state" (harm ordering (b)
        # in the collision report). Recorded on every guarded write. Taken
        # BEFORE the read, not after, so a competing write inside the read's
        # own milliseconds is over-included as a harm candidate (adjudication
        # removes it) rather than silently missed.
        decision_read_ts = datetime.now(timezone.utc)
        active_sessions = await get_active_sessions_async(
            limit=SWEEP_BATCH_SIZE + 1,
            least_recently_updated_first=True,
        )
        c.active_session_batch_truncated = len(active_sessions) > SWEEP_BATCH_SIZE
        if c.active_session_batch_truncated:
            active_sessions = active_sessions[:SWEEP_BATCH_SIZE]
            logger.warning(
                "Dialectic sweep active-session batch truncated at %s rows; "
                "least-recently-updated rows were prioritized",
                SWEEP_BATCH_SIZE,
            )
        c.active_session_count = len(active_sessions)

        if not active_sessions:
            return c.as_result([], "No active sessions found")

        # Filter to stuck sessions (inactive for >2 hours)
        stuck_sessions = []
        for session in active_sessions:
            check_time = _parse_timestamp(session.get("updated_at") or session.get("created_at"))
            if check_time and check_time < threshold_time:
                stuck_sessions.append(session)
        c.stuck_session_count = len(stuck_sessions)

        if not stuck_sessions:
            return c.as_result([], "No stuck sessions found")

        for session in stuck_sessions:
            session_id = session.get("session_id")
            paused_agent_id = session.get("paused_agent_id")
            reviewer_agent_id = session.get("reviewer_agent_id")
            phase = session.get("phase")
            awaiting_facilitation = bool(session.get("awaiting_facilitation"))

            if not session_id:
                c.invalid_session_count += 1
                continue

            # Saga-inflight guard (C1, council 2026-06-28): if a BEAM session
            # owner is mid-resolution for this session, skip it entirely this
            # cycle. Marking it failed / reassigning its reviewer here would race
            # the saga and corrupt the outcome. Fail-open (no saga infra -> no
            # skip), so this is a no-op until BEAM begins writing sagas.
            #
            # `early_check_ts` is taken BEFORE the check so the post-write
            # probe's "created at or after the early check" window cannot miss
            # a saga that started while this query ran.
            early_check_ts = datetime.now(timezone.utc)
            if await has_inflight_saga_async(session_id):
                c.saga_inflight_skip_count += 1
                logger.info(
                    f"Skipping stuck-session sweep for {session_id[:16]}...: "
                    "resolution saga in flight (BEAM owns this transition)"
                )
                continue

            check_time = _parse_timestamp(session.get("updated_at") or session.get("created_at"))

            # Set by the phase branches below when this session should ask
            # for a human instead of falling through to the reap. The request
            # itself is recorded once, after the branches, so both phases
            # share one guarded write path.
            facilitation_reason: str | None = None
            facilitation_note: str | None = None

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
                            if not await _attempt_guarded_write(
                                c,
                                details,
                                attempted=ATTEMPT_REVIEWER_REASSIGNMENT,
                                session=session,
                                write=lambda winner: update_session_reviewer_async(
                                    session_id, new_reviewer, winner=winner
                                ),
                                decision_read_ts=decision_read_ts,
                                early_check_ts=early_check_ts,
                                new_reviewer_agent_id=new_reviewer,
                            ):
                                # The guarded UPDATE wrote nothing — the row
                                # is terminal (dual-writer TOCTOU during
                                # reviewer selection) or gone; the refusal
                                # event names the winner. Don't narrate a
                                # reassignment that never happened.
                                logger.info(
                                    f"Session {session_id[:16]} reviewer write refused "
                                    "(row terminal or missing); reassignment skipped"
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
                            # (The overlap probe ran inside
                            # `_attempt_guarded_write`, before this emit.)
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
                            c.reassigned_count += 1
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

                    # No replacement found — ask for a human (recorded below).
                    facilitation_reason = "reviewer_unresponsive"
                    facilitation_note = (
                        f"Reviewer '{reviewer_agent_id}' unresponsive. Awaiting human facilitation."
                    )

            # For SYNTHESIS phase: ask for a human, never reassign (#2202).
            #
            # A reviewer that delivered a verdict and then went silent leaves the
            # session here, and until this branch existed the sweeper never
            # looked at it: the row fell straight through to FAILED at 2h while
            # an ANTITHESIS stall got the 4h operator window above. The escape
            # already exists — `handle_reassign_reviewer` admits any phase once
            # `awaiting_facilitation` is set — but nothing raised the flag, so
            # nobody could use it. What the operator's reassign then does
            # depends on the row: inside the 4h window the session is still
            # active, so `_apply_reviewer_reassignment` leaves it in SYNTHESIS
            # and the replacement continues by submitting synthesis
            # (`test_reassign_facilitates_standing_rejection_in_synthesis`);
            # it rewinds to ANTITHESIS for a refused self-review (old reviewer
            # == paused agent), and a row already reaped as `failed` is revived
            # at ANTITHESIS too, since the revive path reopens any reaped row
            # that has a thesis to that phase.
            #
            # ⛔NOT reusing the reassignment path above. The protocol requires
            # the SAME reviewer to revise its own verdict; a replacement the
            # sweeper chose would carry a new identity that never formed the
            # objection. So the sweeper raises the request and stops.
            #
            # What the flag then enables is the existing SYNTHESIS design, not
            # an operator-only gate: `check_reviewer_stuck` treats a flagged
            # row whose reviewer is PAUSED or MISSING as stuck, and a bound
            # caller's `get(check_timeout=true)` may then auto-replace it —
            # exactly as it already can after a standing objection raises the
            # same flag. A reviewer whose stored status reads active (the
            # #2202 case) is not stuck by that test, so its session waits for
            # an operator `reassign` inside the window.
            #
            # Deliberately NOT gated on the reviewer's stored status. That field
            # is what `reviewer_status` reports and it read "active" on the live
            # instance while the reviewer process was gone — a stored field, not
            # a liveness probe. The reviewer's own continuation wait is 1h
            # (`DEFAULT_CONTINUATION_WAIT_S`), so a SYNTHESIS row idle past the
            # 2h stuck threshold has already outlived the window in which the
            # reviewer could come back on its own.
            #
            # ⛔ONLY when the REVIEWER owes the move. At SYNTHESIS the flag is
            # not neutral: `check_reviewer_stuck` reads it as "the reviewer
            # owes reconsideration" (the other two writers — a standing
            # objection, an LLM reviewer that did not approve — both mean
            # that), and a stuck reviewer is then auto-replaced by any bound
            # caller's `dialectic(action="get", check_timeout=true)`. Raising
            # it on a row where the PAUSED agent owes the move would hand the
            # returning paused agent a machine-picked reviewer with authority
            # over the original verdict. So the transcript decides: the flag
            # goes up only when the reviewer owes the move (its first verdict
            # is pending, or the paused agent has answered its standing
            # objection). Otherwise the
            # stall is the paused agent's own and the row keeps its prior
            # behaviour (reaped at the stuck threshold). The read is skipped
            # for rows already flagged, which the hold below handles.
            #
            # ⛔Known gap, not closed here: the check runs only when the flag
            # is raised, and nothing clears the flag when the reviewer later
            # answers (the standing-objection writer has the same property).
            # A row flagged while the reviewer owed the move therefore keeps
            # the 4h hold and `check_reviewer_stuck`'s reading after the move
            # passes back to the paused agent.
            elif (
                phase in ("synthesis", "SYNTHESIS")
                and reviewer_agent_id
                and not awaiting_facilitation
                and check_time and check_time > fail_time
                and await _synthesis_reviewer_owes_reply(
                    session_id, paused_agent_id, reviewer_agent_id
                )
            ):
                facilitation_reason = "synthesis_stalled"
                facilitation_note = (
                    f"Session stalled in SYNTHESIS awaiting reviewer '{reviewer_agent_id}' "
                    "(the move is the reviewer's; no reply past the stuck threshold). "
                    "Awaiting human facilitation."
                )

            # Record a standing facilitation request if not too old.
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
            # branch is re-entered on every sweep — which, at ANTITHESIS,
            # is what keeps `select_reviewer` retrying while a human is
            # waited on. The SYNTHESIS path never calls it.
            if (
                facilitation_reason
                and check_time and check_time > fail_time
                and not awaiting_facilitation
            ):
                try:
                    recorded = await _attempt_guarded_write(
                        c,
                        details,
                        attempted=ATTEMPT_AWAITING_FACILITATION,
                        session=session,
                        write=lambda winner: mark_awaiting_facilitation_async(
                            session_id, winner=winner
                        ),
                        decision_read_ts=decision_read_ts,
                        early_check_ts=early_check_ts,
                    )
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
                    continue
                _sync_cached_session(session_id, awaiting_facilitation=True)
                await emit_facilitation_needed(
                    session_id=session_id,
                    paused_agent_id=paused_agent_id,
                    phase=phase,
                    reason=facilitation_reason,
                )
                try:
                    await add_message_async(
                        session_id=session_id,
                        agent_id="system",
                        message_type="system",
                        reasoning=facilitation_note,
                    )
                except Exception as e:
                    # Narration only. The request is committed; do not
                    # unwind it, and do not fall through to the reap.
                    logger.warning(f"Could not add facilitation message for {session_id[:16]}: {e}")
                c.facilitation_count += 1
                details.append({
                    "session_id": session_id,
                    "paused_agent_id": paused_agent_id,
                    "phase": phase,
                    "action": "awaiting_facilitation",
                    "stuck_reviewer": reviewer_agent_id,
                })
                logger.info(
                    f"Session {session_id[:16]} awaiting human facilitation "
                    f"(reviewer {reviewer_agent_id}, {facilitation_reason})"
                )
                continue  # Don't fail yet — give human time

            # A session already awaiting human facilitation runs on the HUMAN's
            # clock, not the stuck-process clock. STUCK_SESSION_THRESHOLD (2h)
            # measures "this process is wedged"; an operator may simply be
            # asleep. FACILITATION_TIMEOUT (4h) exists for exactly this and was
            # historically (until #2202 hoisted the facilitation write above
            # out of the phase branches) only reachable inside the ANTITHESIS
            # branch, so a session that asked for a human at THESIS — which is
            # where all 50 facilitation events actually came from — fell
            # straight through to FAILED at 2h.
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
                if not await _attempt_guarded_write(
                    c,
                    details,
                    attempted=ATTEMPT_REAP_FAILED,
                    session=session,
                    write=lambda winner: update_session_status_async(
                        session_id, "failed", winner=winner
                    ),
                    decision_read_ts=decision_read_ts,
                    early_check_ts=early_check_ts,
                ):
                    # The guarded UPDATE wrote nothing: another writer
                    # finished this session after our early saga/staleness
                    # checks (even one that also wrote 'failed' — that
                    # outcome is theirs, with their resolution payload), or
                    # the row is gone. The refusal event names the winner.
                    # Skip the failure narrative and the count.
                    logger.info(
                        f"Session {session_id[:16]} status write refused "
                        "(row terminal or missing); reap skipped"
                    )
                    continue
                # `update_session_status` writes status AND phase; mirror the
                # reap so a cached session in this process does not go on
                # reading as live. Without it `_apply_reviewer_reassignment`
                # sees a non-terminal phase, skips `reopen_session`, and the
                # guarded reviewer write is then refused by the row it never
                # reopened — the standing request answered in memory only.
                _sync_cached_session(session_id, phase="failed")
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

                c.resolved_count += 1
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

        return c.as_result(
            details,
            (
                f"Processed {len(stuck_sessions)} stuck session(s): "
                f"{c.reassigned_count} reassigned, {c.facilitation_count} awaiting facilitation, "
                f"{c.resolved_count} failed, {c.skipped_count} skipped (write refused)"
            ),
        )

    except Exception as e:
        logger.error(f"Error auto-resolving stuck sessions: {e}", exc_info=True)
        # Earlier iterations may already have committed. Preserve their
        # outcome evidence instead of turning a partial cycle into an
        # all-zero one because a later row aborted the scan.
        return c.as_result(details, "Failed to auto-resolve stuck sessions", error=str(e))


async def auto_resolve_stuck_sessions(
    *,
    trigger_source: str = "direct",
    timeout_s: Optional[float] = None,
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

    ``timeout_s`` bounds the cycle (the periodic task passes
    ``UNITARES_DIALECTIC_SWEEP_CYCLE_TIMEOUT_S``). A cycle that exceeds it is
    cancelled and still emits its row, with ``error="timeout"`` and the counts
    it had committed (Wave 3 gate council 2026-09-27, finding B5: the loop
    awaited each cycle with no timeout and emitted only in ``finally``, so a
    hung cycle and a lost audit write looked the same). An external
    cancellation (shutdown) emits ``error="cancelled"``.
    """
    if AUTO_RESOLVE_IN_PROGRESS.get():
        logger.debug(
            "Dialectic stuck-session resolver re-entry suppressed (source=%s)",
            trigger_source,
        )
        suppressed = _CycleCounts().as_result([], "Resolver re-entry suppressed")
        suppressed["reentrant_suppressed"] = True
        return suppressed

    token = AUTO_RESOLVE_IN_PROGRESS.set(True)
    started = monotonic()
    counts = _CycleCounts(cycle_id=str(uuid.uuid4()))
    result: Dict[str, Any] | None = None
    interrupted: Optional[str] = None
    try:
        if timeout_s is not None and timeout_s > 0:
            deadline = asyncio.timeout(timeout_s)
            try:
                async with deadline:
                    with session_write_via("sweeper"):
                        result = await _auto_resolve_stuck_sessions(counts)
            except TimeoutError:
                if not deadline.expired():
                    raise
                interrupted = "timeout"
                logger.warning(
                    "[DIALECTIC_SWEEP] %s cycle exceeded %.0fs and was cancelled; "
                    "emitting its committed counts with error=timeout",
                    trigger_source, timeout_s,
                )
                result = counts.as_result(
                    [], f"Cycle timed out after {timeout_s:.0f}s", error="timeout"
                )
        else:
            with session_write_via("sweeper"):
                result = await _auto_resolve_stuck_sessions(counts)
        return result
    except asyncio.CancelledError:
        interrupted = "cancelled"
        raise
    finally:
        elapsed_ms = max(0, round((monotonic() - started) * 1000))
        cycle = result if result is not None else counts.as_result(
            [], "", error=interrupted
        )

        def _n(key: str) -> int:
            return int(cycle.get(key, 0) or 0)

        try:
            if counts.pending_row is not None:
                # A write (or its row) was cut short. Its row is written now,
                # after the cancellation, so the per-write rows still sum to
                # this cycle's counts; a write whose outcome is unknown says
                # why ("timeout" / "cancelled").
                owed = dict(counts.pending_row)
                counts.pending_row = None
                if owed.get("error") == "interrupted":
                    owed["error"] = interrupted or "interrupted"
                await emit_guarded_write(**owed)
            await emit_sweep_cycle(
                trigger_source=trigger_source,
                active_session_count=_n("active_session_count"),
                active_session_batch_truncated=bool(
                    cycle.get("active_session_batch_truncated", False)
                ),
                stuck_session_count=_n("stuck_session_count"),
                invalid_session_count=_n("invalid_session_count"),
                saga_inflight_skip_count=_n("saga_inflight_skip_count"),
                write_attempt_count=_n("write_attempt_count"),
                write_succeeded_count=_n("write_succeeded_count"),
                write_refused_count=_n("skipped_count"),
                write_error_count=_n("write_error_count"),
                overlap_clean_count=_n("overlap_clean_count"),
                overlap_detected_count=_n("overlap_detected_count"),
                overlap_probe_failed_count=_n("overlap_probe_failed_count"),
                resolved_count=_n("resolved_count"),
                reassigned_count=_n("reassigned_count"),
                facilitation_count=_n("facilitation_count"),
                duration_ms=elapsed_ms,
                error=str(cycle["error"]) if cycle.get("error") else interrupted,
                cycle_id=counts.cycle_id,
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
