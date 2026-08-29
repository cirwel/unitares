"""Governed audit events for the dialectic subsystem.

One implementation of each event's shape, called from every site that produces
it. The alternative -- each call site building its own payload -- is how the
reassignment stream came to be incomplete in the first place.

⛔SCOPE BOUNDARY, stated because an earlier draft of this docstring claimed
complete coverage it does not have. There is a THIRD writer of
`core.dialectic_sessions.reviewer_agent_id`: the first-responder auto-assign in
`handlers.py::handle_submit_antithesis`, which fires only when the slot was
`None`. It deliberately does NOT emit, because criterion 10 measures the
reviewer-*re*assignment rate and an initial assignment is not a reassignment.
⛔That is a semantic exclusion, not coverage. If the (F) metric is ever
redefined to count initial assignments, that site is the one to instrument, and
this paragraph is the reason it was left out.

WHY THIS MODULE EXISTS
----------------------
Wave-3 prereq PR #9 gave disconfirmer (F)'s reviewer-reassignment metric its
first event-stream source: `_apply_reviewer_reassignment` began emitting
`dialectic_reviewer_reassigned`. That emission was documented as "the single
chokepoint for both the explicit `dialectic(reassign)` tool and the
stuck-reviewer auto path."

It was not. Verified 2026-08-22: the periodic sweeper
`auto_resolve_stuck_sessions` writes reviewer changes directly through
`update_session_reviewer_async` and calls neither `_apply_reviewer_reassignment`
nor `check_reviewer_stuck` nor `check_timeout`, so an auto-path reassignment
would never reach the stream the (F) baseline is computed on.

⛔**The gap is STRUCTURAL AND UNEXERCISED, not an observed loss** — measured
live 2026-08-22, and stated precisely because an earlier draft of this
docstring overclaimed it as accruing damage. `audit.events` holds **zero**
`dialectic_reviewer_reassigned` rows all-time, against 4.7M events since
2026-03-15. Only **two** reviewer reassignments exist in the entire dialectic
history (2026-04-19, 2026-04-30), both predating the emitter's own deploy, and
the sweeper has performed none. So nothing has been lost yet; the next
auto-path reassignment would have been.

⛔The two producers are NOT merged into one code path here. The sweeper's write
sequence and the request-driven path differ in real ways (reopen ordering, the
BEAM-vs-Python routing gate, facilitation-flag handling), and routing one
through the other would be a behavioural change wearing an observability
change's clothes. What is shared is the event *shape*, which is the part that
must not drift.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)

REVIEWER_REASSIGNED = "dialectic_reviewer_reassigned"
FACILITATION_NEEDED = "dialectic_facilitation_needed"
WRITE_REFUSED = "dialectic_write_refused"
SWEEP_CYCLE = "dialectic_sweep_cycle"

# The three guarded writes the sweeper can have refused. Shared with the
# sweeper's own `details` entries so the event payload and the returned summary
# cannot drift apart -- they described the same three outcomes in two places
# before this constant existed.
ATTEMPT_REVIEWER_REASSIGNMENT = "reviewer_reassignment"
ATTEMPT_AWAITING_FACILITATION = "awaiting_facilitation"
ATTEMPT_REAP_FAILED = "reap_failed"


async def emit_reviewer_reassigned(
    *,
    session_id: str,
    old_reviewer_id: Optional[str],
    new_reviewer_id: str,
    reason: str,
    source: str,
) -> None:
    """Record one reviewer reassignment on the (F) measurement stream.

    Args:
        session_id: the dialectic session whose reviewer changed.
        old_reviewer_id: the outgoing reviewer, or None. ⛔None is reachable on
            the reassignment path when the slot was already empty; it does NOT
            mean this helper covers first-responder initial assignment, which
            never calls here.
        new_reviewer_id: the incoming reviewer.
        reason: why the reassignment happened.
        source: which producer emitted this -- ``"request"`` for the
            `dialectic(reassign)` / takeover / timeout paths, ``"sweeper"`` for
            `auto_resolve_stuck_sessions`. ⛔These are the two *reassignment*
            producers; first-responder initial assignment is out of scope by
            the boundary above. ⛔The (F) reassignment rate is the
            sum over BOTH; this field exists so a reader can tell them apart
            without having to re-derive which code path ran.

    Fail-soft by design: the reassignment has already committed by the time
    this is called, so a failure here costs observability, never correctness.
    An exception raised from the audit layer must not roll back a write that
    already happened.
    """
    try:
        from src.audit_db import append_audit_event_async

        await append_audit_event_async({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": REVIEWER_REASSIGNED,
            "agent_id": new_reviewer_id,
            # Top-level session_id populates the indexed audit.events column
            # (nested-only lands that column NULL); duplicated in details for
            # payload self-containment.
            "session_id": session_id,
            "details": {
                "session_id": session_id,
                "old_reviewer_id": old_reviewer_id,
                "new_reviewer_id": new_reviewer_id,
                "reason": reason,
                "source": source,
            },
        })
    except Exception as exc:
        logger.warning(
            "%s audit emit failed: session=%s source=%s err=%s",
            REVIEWER_REASSIGNED, session_id, source, exc,
        )


async def emit_facilitation_needed(
    *,
    session_id: str,
    paused_agent_id: Optional[str],
    phase: Optional[str],
    reason: str,
) -> None:
    """Announce a standing facilitation request raised by the sweeper.

    The handler path emits this through `_emit_dialectic_event`, which reads a
    live `DialecticSession` object. The sweeper only ever holds a database row,
    so it emits from row fields here rather than rehydrating a session just to
    announce one. Same event name and same `awaiting_facilitation` payload key,
    because the dashboard and the Phoenix dialectic pane treat any
    ``dialectic_*`` event as a doorbell to refetch authoritative state — a
    request nobody is told about is one the operator finds by looking.

    Fail-soft: the request is already committed when this runs, so a failure
    here costs observability, never correctness.
    """
    try:
        from src.broadcaster import broadcaster_instance

        await broadcaster_instance.broadcast_event(
            FACILITATION_NEEDED,
            agent_id=paused_agent_id,
            payload={
                "session_id": session_id,
                "phase": phase,
                "awaiting_facilitation": True,
                "reason": reason,
                "source": "sweeper",
            },
        )
    except Exception as exc:  # pragma: no cover - telemetry must not break the sweep
        logger.warning(
            "%s emit failed: session=%s err=%s", FACILITATION_NEEDED, session_id, exc
        )


async def emit_write_refused(
    *,
    session_id: str,
    attempted: str,
    paused_agent_id: Optional[str] = None,
    source: str = "sweeper",
) -> None:
    """Record one guarded write the sweeper attempted and the database refused.

    This is the direct observation of two writers converging on one row: the
    sweeper decided a session needed a reviewer change, a facilitation flag or a
    reap, and the terminal-state predicate rejected the write because somebody
    else had already finished the session.

    WHY THIS EXISTS
    ---------------
    The sweeper has counted these in `skipped_count` since #1804 added the
    guards, and that count reached nothing durable -- not `audit.events`, not
    `audit.coordination_measurements`, not a metric series, and not even the
    sweep log line, whose condition omitted it. The two emitters above fire only
    on paths where a write *succeeded*, so a refusal had no event to be emitted
    as: it was missing from the vocabulary, not from the plumbing.

    ⛔The consequence, and the reason this is a governance concern rather than a
    logging nicety: "the sweeper has never collided with another writer" and "we
    have never been able to see a collision" were the same observation. That is
    measurement-authority state 3 (*not recorded*), which must never be reported
    with the same sentence as state 4 (recorded, and genuinely zero). A
    positive refusal event establishes an observed refusal. The separate
    zero-inclusive ``dialectic_sweep_cycle`` heartbeat establishes that the
    producer actually ran when no refusal event exists. Neither event observes
    the opposite ordering where the sweeper writes first and a saga starts
    afterwards; absence of refusals must not be promoted to absence of all
    dual-writer overlap.

    Args:
        session_id: the session whose write was refused.
        attempted: which guarded write was refused -- one of the three
            ``ATTEMPT_*`` constants above.
        paused_agent_id: the session's paused agent, when the row carried one.
            Populates the indexed `agent_id` column, matching how
            `emit_facilitation_needed` attributes a sweeper-raised event.
        source: which producer observed the refusal. Only ``"sweeper"`` today;
            the parameter exists so a second producer cannot be added without
            declaring itself, which is exactly how the reassignment stream came
            to be incomplete.

    ⛔**Deliberately does NOT record the refusing predicate**, though the gate
    that commissioned this event asked for one. There is no honest source for it
    here. `DialecticDB.TERMINAL_WRITE_GUARD` is declarative only -- the three
    `UPDATE` statements inline `('resolved', 'failed')` and never interpolate
    the constant -- so recording it would stamp every event with a value that
    governs no write, and hardcoding the literal would add a third copy to drift
    against. ⛔**Nor can this event distinguish terminal-from-missing.** The
    write helpers establish which by a follow-up `SELECT` and only *log* it;
    they return a bare `False`. A reader who needs that distinction must go to
    the DB-layer log, and any future attempt to answer it from this stream alone
    must first change what those helpers return.

    Fail-soft, for the same reason as the emitters above inverted: the sweep has
    already decided to skip this session by the time this runs, so a failure
    here costs observability, never correctness. It must never turn a skipped
    session into a failed sweep.
    """
    try:
        from src.audit_db import append_audit_event_async

        await append_audit_event_async({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": WRITE_REFUSED,
            "agent_id": paused_agent_id,
            # Top-level session_id populates the indexed audit.events column;
            # duplicated in details for payload self-containment, as above.
            "session_id": session_id,
            "details": {
                "session_id": session_id,
                "attempted": attempted,
                "paused_agent_id": paused_agent_id,
                "source": source,
            },
        })
    except Exception as exc:
        logger.warning(
            "%s audit emit failed: session=%s attempted=%s source=%s err=%s",
            WRITE_REFUSED, session_id, attempted, source, exc,
        )


async def emit_sweep_cycle(
    *,
    trigger_source: str,
    active_session_count: int,
    stuck_session_count: int,
    invalid_session_count: int,
    saga_inflight_skip_count: int,
    write_attempt_count: int,
    write_refused_count: int,
    resolved_count: int,
    reassigned_count: int,
    facilitation_count: int,
    duration_ms: int,
    error: Optional[str] = None,
) -> None:
    """Record every completed resolver cycle, including an all-zero cycle.

    Positive-only refusal events cannot distinguish "the producer ran and saw
    zero refusals" from "the producer never ran". This event supplies that
    denominator and identifies the invocation source because the resolver has
    both periodic and request-triggered entry points.

    ``saga_inflight_skip_count`` records the ordering visible at the early saga
    guard. ``write_refused_count`` records guarded writes another writer beat.
    They are distinct because neither is a complete measure of all overlap;
    notably, a saga that starts after the early check can still lose to a
    successful sweeper write. Consumers must treat cycle gaps as missing
    evidence and must not infer a collision-free system from zero counts alone.

    Fail-soft: audit availability cannot decide whether session maintenance is
    allowed to run.
    """
    try:
        from src.audit_db import append_audit_event_async

        await append_audit_event_async({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": SWEEP_CYCLE,
            "agent_id": None,
            "details": {
                "trigger_source": trigger_source,
                "active_session_count": active_session_count,
                "stuck_session_count": stuck_session_count,
                "invalid_session_count": invalid_session_count,
                "saga_inflight_skip_count": saga_inflight_skip_count,
                "write_attempt_count": write_attempt_count,
                "write_refused_count": write_refused_count,
                "resolved_count": resolved_count,
                "reassigned_count": reassigned_count,
                "facilitation_count": facilitation_count,
                "duration_ms": duration_ms,
                "error": error,
            },
        })
    except Exception as exc:
        logger.warning(
            "%s audit emit failed: source=%s err=%s",
            SWEEP_CYCLE, trigger_source, exc,
        )
