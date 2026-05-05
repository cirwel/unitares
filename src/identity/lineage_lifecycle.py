"""R2 PR 2: lineage lifecycle FSM.

Drives the provisional → {confirmed, demoted, archived} state machine
described in `docs/ontology/r2-honest-memory-integration.md`
§"Promotion / demotion / archival protocol".

The FSM:

1. Reads the current lineage state from the storage layer in a single
   query (`read_lineage_state`).
2. Skips eval if the row was evaluated within the cadence window.
3. Short-circuits to ``archived`` if the grace window expired before
   the row was promoted.
4. Otherwise calls R1's `score_trajectory_continuity` and applies the
   verdict:
   - ``plausible`` on a provisional row → promote (confirm).
   - ``unsupported`` on any row → demote. ``confirmed`` rows clawback
     their `chain_obs_count` to 0 inside ``demote_lineage``.
   - ``inconclusive`` → no transition; only `lineage_last_eval_at` is
     stamped so the cadence guard can fire on the next call.

Side effects:

- Stamps `lineage_last_eval_at` after every R1 invocation (after the
  storage transition fires, so a guard-skip on the storage helper is
  observable in the outcome) and after a grace short-circuit. Also
  stamps best-effort on R1 exception paths to prevent the sweeper
  from hot-looping on persistent errors. No stamp on
  cadence skip / no-parent skip / terminal-state short-circuit.
- Emits one of three audit events per *storage-confirmed* terminal
  transition: ``lineage_promoted``, ``lineage_demoted``,
  ``lineage_grace_expired``. If a storage helper's WHERE guard fires
  (e.g. concurrent archive landed), no audit emits and the outcome
  carries a ``skipped_reason`` of
  ``confirm_skipped_terminal_state`` / ``demote_skipped_terminal_state``.
  (The ``lineage_declared`` and ``lineage_cross_role_rejected`` events
  fire from the onboard handler in PR 3.)

Wiring into `process_agent_update` is PR 5's job; the sweeper task is
PR 4. This module exposes a single async entry point —
:func:`evaluate_lineage_for` — that callers schedule from a tracked
task outside the anyio context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Literal, Optional

from src.db import get_db
from src.identity.trajectory_continuity import score_trajectory_continuity

logger = logging.getLogger(__name__)


TerminalState = Literal["confirmed", "demoted", "archived"]
Transition = Literal[
    "provisional_to_confirmed",
    "provisional_to_demoted",
    "confirmed_to_demoted",
    "provisional_to_archived",
]


@dataclass(frozen=True)
class LineageEvalOutcome:
    """Result of one FSM evaluation.

    - ``terminal_state`` and ``transition`` are set when the FSM moved
      the row; both are ``None`` when the eval was a no-op (cadence
      skip, no parent, inconclusive verdict, or already-confirmed row
      that re-scored plausible).
    - ``r1_verdict`` is the verdict R1 returned, or ``None`` when R1
      was not invoked (cadence skip / no parent / grace short-circuit).
    - ``skipped_reason`` is one of ``"no_parent"``,
      ``"within_cadence"``, ``"terminal_state"`` (row already
      archived/demoted at top of FSM), ``"r1_error"`` (R1 raised),
      ``"confirm_skipped_terminal_state"`` /
      ``"demote_skipped_terminal_state"`` (storage helper's WHERE
      guard fired, indicating concurrent terminal transition between
      our read and write) when the FSM declined to evaluate or the
      transition was skipped; otherwise ``None``.
    """

    successor_id: str
    parent_id: Optional[str]
    terminal_state: Optional[TerminalState]
    transition: Optional[Transition]
    r1_verdict: Optional[str]
    skipped_reason: Optional[str]


_DEFAULT_MIN_OBSERVATIONS = 5
_DEFAULT_GRACE_WINDOW = timedelta(days=30)
_DEFAULT_EVAL_CADENCE = timedelta(hours=1)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _emit_audit(
    event_type: str,
    agent_id: str,
    *,
    details: Dict[str, Any],
) -> None:
    """Append a lineage-lifecycle audit event.

    Lazy-imports `append_audit_event_async` to avoid pulling
    `src.audit_db` at module-import time (mirrors the pattern in
    `src.identity.trajectory_continuity` and `src.background_tasks`).

    Timestamp shape: ISO-8601 string with timezone — matches the
    `agent_silent` event in `src/background_tasks.py:1023` and
    `lifecycle_*` events in `src/agent_metadata_model.py:87`. The
    `append_audit_event_async` helper accepts datetime objects too,
    but ISO strings are the more common convention at active call
    sites so we follow it here.

    Fail-soft: an audit-write failure is logged at warn level and does
    not propagate. The storage transition has already committed by
    the time this helper runs; observability is the only thing at
    risk on failure.
    """
    from src.audit_db import append_audit_event_async  # lazy import

    try:
        await append_audit_event_async({
            "timestamp": _utcnow().isoformat(),
            "event_type": event_type,
            "agent_id": agent_id,
            "details": details,
        })
    except Exception as exc:  # pragma: no cover — fail-soft observability
        logger.warning(
            "lineage audit emit failed: event=%s agent=%s err=%s",
            event_type, agent_id, exc,
        )


async def evaluate_lineage_for(
    successor_id: str,
    *,
    min_observations: int = _DEFAULT_MIN_OBSERVATIONS,
    grace_window: timedelta = _DEFAULT_GRACE_WINDOW,
    eval_cadence: timedelta = _DEFAULT_EVAL_CADENCE,
    now: Callable[[], datetime] = _utcnow,
) -> LineageEvalOutcome:
    """Evaluate one lineage edge and apply the next FSM transition.

    See module docstring for the full contract. Caller is responsible
    for scheduling this from outside the anyio task group (e.g. via
    ``create_tracked_task``) — direct ``await`` from an MCP handler
    would re-introduce the anyio-asyncpg deadlock noted in CLAUDE.md.

    Parameters
    ----------
    successor_id
        The agent_id of the row whose lineage is being evaluated.
    min_observations
        Forwarded to R1's ``score_trajectory_continuity``.
    grace_window
        How long a provisional row may remain unverified before it
        gets archived. Default 30 days per the design doc.
    eval_cadence
        Minimum interval between R1 evals for the same row. Default
        1 hour — protects against hot-loop re-evaluation when both
        the sweeper and the inline check-in path target the same edge.
    now
        Injection seam for tests. Real callers use the default.
    """
    backend = get_db()
    row = await backend.read_lineage_state(successor_id)

    # 1. No row, or row has no declared parent → no FSM work to do.
    if row is None or row.get("parent_agent_id") is None:
        return LineageEvalOutcome(
            successor_id=successor_id,
            parent_id=None,
            terminal_state=None,
            transition=None,
            r1_verdict=None,
            skipped_reason="no_parent",
        )

    parent_id: str = row["parent_agent_id"]

    # 2. Terminal-state short-circuit. Storage helpers (confirm_lineage,
    #    demote_lineage, archive_lineage) all have WHERE guards on
    #    lineage_archived_at IS NULL AND lineage_demoted_at IS NULL, so
    #    they're safe — but reaching them wastes an R1 call and risks
    #    audit-event noise. Filter out at the very top, before the
    #    cadence guard so a terminal row doesn't even consume a cycle.
    if (
        row.get("lineage_archived_at") is not None
        or row.get("lineage_demoted_at") is not None
    ):
        return LineageEvalOutcome(
            successor_id=successor_id,
            parent_id=parent_id,
            terminal_state=None,
            transition=None,
            r1_verdict=None,
            skipped_reason="terminal_state",
        )

    # 3. Cadence guard — if we evaluated this row recently, skip.
    last_eval = row.get("lineage_last_eval_at")
    current = now()
    if last_eval is not None and (current - last_eval) < eval_cadence:
        return LineageEvalOutcome(
            successor_id=successor_id,
            parent_id=parent_id,
            terminal_state=None,
            transition=None,
            r1_verdict=None,
            skipped_reason="within_cadence",
        )

    # 4. Grace expiration short-circuit — only meaningful for
    #    provisional rows. Confirmed rows can stay confirmed
    #    indefinitely; archival is a provisional-only outcome.
    declared_at = row.get("lineage_declared_at")
    if (
        row.get("provisional_lineage")
        and declared_at is not None
        and (current - declared_at) >= grace_window
    ):
        await backend.archive_lineage(successor_id)
        await backend.stamp_lineage_eval(successor_id)
        await _emit_audit(
            "lineage_grace_expired",
            successor_id,
            details={
                "parent_id": parent_id,
                "declared_at": declared_at.isoformat(),
            },
        )
        return LineageEvalOutcome(
            successor_id=successor_id,
            parent_id=parent_id,
            terminal_state="archived",
            transition="provisional_to_archived",
            r1_verdict=None,
            skipped_reason=None,
        )

    # 5. Score the lineage with R1, then apply the verdict. The whole
    #    scoring path is wrapped in try/except: R1 can raise (audit
    #    write failure, asyncpg connection error) and if it does we
    #    must still stamp `lineage_last_eval_at` to prevent a tight
    #    sweeper-retry loop on persistent errors. Catch Exception (not
    #    BaseException) so CancelledError still propagates.
    try:
        score = await score_trajectory_continuity(
            parent_id, successor_id, min_observations=min_observations,
        )
        verdict = score.verdict
        is_confirmed_already = (
            row.get("confirmed_at") is not None
            and not row.get("provisional_lineage")
        )

        # 5a. Promotion — provisional → confirmed.
        # Ordering: storage transition FIRST, then stamp, then audit.
        # If the WHERE guard on `confirm_lineage` fires (concurrent
        # archive/demote landed between our read and write), the
        # helper returns False and we degrade the outcome to skipped
        # rather than emitting a false-positive `lineage_promoted`.
        if verdict == "plausible" and not is_confirmed_already:
            confirm_ok = await backend.confirm_lineage(successor_id)
            await backend.stamp_lineage_eval(successor_id)
            if not confirm_ok:
                return LineageEvalOutcome(
                    successor_id=successor_id,
                    parent_id=parent_id,
                    terminal_state=None,
                    transition=None,
                    r1_verdict=verdict,
                    skipped_reason="confirm_skipped_terminal_state",
                )
            await _emit_audit(
                "lineage_promoted",
                successor_id,
                details={
                    "parent_id": parent_id,
                    "score_id": score.score_id,
                    "plausibility": score.plausibility,
                },
            )
            return LineageEvalOutcome(
                successor_id=successor_id,
                parent_id=parent_id,
                terminal_state="confirmed",
                transition="provisional_to_confirmed",
                r1_verdict=verdict,
                skipped_reason=None,
            )

        # 5b. Demotion — provisional or confirmed, both flow through
        #     `demote_lineage` which handles the chain_obs_count clawback.
        #     Same ordering invariant as 5a: storage → stamp → audit;
        #     guard-skip degrades outcome to skipped (no audit fired).
        if verdict == "unsupported":
            reason = (
                "post_promotion_divergence" if is_confirmed_already
                else "r1_unsupported"
            )
            demote_ok = await backend.demote_lineage(successor_id, reason=reason)
            await backend.stamp_lineage_eval(successor_id)
            if not demote_ok:
                return LineageEvalOutcome(
                    successor_id=successor_id,
                    parent_id=parent_id,
                    terminal_state=None,
                    transition=None,
                    r1_verdict=verdict,
                    skipped_reason="demote_skipped_terminal_state",
                )
            await _emit_audit(
                "lineage_demoted",
                successor_id,
                details={
                    "parent_id": parent_id,
                    "score_id": score.score_id,
                    "reason": reason,
                    "plausibility": score.plausibility,
                },
            )
            transition: Transition = (
                "confirmed_to_demoted" if is_confirmed_already
                else "provisional_to_demoted"
            )
            return LineageEvalOutcome(
                successor_id=successor_id,
                parent_id=parent_id,
                terminal_state="demoted",
                transition=transition,
                r1_verdict=verdict,
                skipped_reason=None,
            )

        # 5c. Inconclusive — and the "already confirmed + plausible"
        #     no-op path. Both leave the row's terminal state untouched.
        #     Stamp cadence so the next sweeper tick honors the guard.
        await backend.stamp_lineage_eval(successor_id)
        return LineageEvalOutcome(
            successor_id=successor_id,
            parent_id=parent_id,
            terminal_state=None,
            transition=None,
            r1_verdict=verdict,
            skipped_reason=None,
        )
    except Exception as exc:
        logger.error(
            "[r2_fsm] score_trajectory_continuity failed for "
            "successor=%s parent=%s: %s",
            successor_id[:8], parent_id[:8], exc,
        )
        # Best-effort cadence stamp to prevent tight-looping on
        # persistent errors. If the stamp itself fails (e.g. the same
        # underlying asyncpg failure), swallow — the next tick will
        # try again, but cadence noise is preferable to crashing.
        try:
            await backend.stamp_lineage_eval(successor_id)
        except Exception:  # pragma: no cover — defense in depth
            pass
        return LineageEvalOutcome(
            successor_id=successor_id,
            parent_id=parent_id,
            terminal_state=None,
            transition=None,
            r1_verdict=None,
            skipped_reason="r1_error",
        )
