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
