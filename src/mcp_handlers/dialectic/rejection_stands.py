"""Automated exit from facilitation once a reviewer's rejection stands.

Operator decision, 2026-10-11 ("why is dialectic waiting on me; fix"), answering
the open question that ``awaiting_facilitation`` had no automated exit.

WHY
---
The self-clear guard (#1543) routes a paused agent's ``agrees=True`` over a
standing reviewer rejection to human facilitation. Nobody facilitates, and four
hours later the BEAM liveness reaper closes the session ``failed`` with the
rejection still standing. Of 27 real (non-canary) facilitation sessions in the
60 days to 2026-10-11, 26 already had the reviewer's rejection standing when
they entered the wait (22 with a reply from the paused agent, often a full
concession; 4 with none) and 1 had no reviewer answer at all. The human wait
never changed an outcome. ``agrees=True`` from the paused agent is ambiguous:
"I concede the rejection" and "I fixed it, approve me" arrive identically.

WHAT
----
When the session is awaiting facilitation, the reviewer's rejection stands, and
the reviewer has had its last word, the session closes ``failed`` with a
``resolution_json`` whose reason is ``rejection_stands``. "Last word" is one of:

* ``reviewer_answered_reply`` -- the reviewer filed a rejection that answers a
  reply the paused agent made to an EARLIER reviewer verdict (it reconsidered
  and still rejects);
* ``continuation_window_ended`` -- the orchestrated reviewer's bounded
  continuation window (``agents/dialectic_reviewer/reviewer.py``
  ``continue_after_disagreement``) has ended, so no process remains that could
  revise the verdict.

While the window is open nothing changes: the reviewer may still revise.

WHAT IT NEVER DOES
------------------
* It never resolves in the paused agent's favour. The only terminal it writes is
  ``failed``, and only over a standing rejection.
* It never reassigns. Auto-reassigning to escape a rejection is
  appeal-until-you-win and was refused; an operator ``reassign`` before the
  close remains the manual override.
* It never terminates on acceptance. ``verdict_acceptance`` (accepted /
  contested / no_reply, the BEAM liveness vocabulary) is recorded as a
  description of the transcript. The close is decided by whether anyone with
  authority to revise the verdict remains, not by whether the paused agent
  agreed, and ``no_reply`` is never read as assent.
* It does not cover the session whose reviewer never answered (no reviewer
  verdict at all) or a non-orchestrated reviewer's window, which has no declared
  bound to end. Those keep the existing facilitation path.

The assessment here is pure: the caller passes the transcript, the clock, and
the reviewer-kind test. The one caller that writes is the stuck-session sweeper
(`auto_resolve._close_standing_rejections`), every cycle, regardless of idle
time, so a session closes within one sweep of its reviewer's last word. The
sweeper's write is a compare-and-set against the row it assessed. The synthesis
handler deliberately does not write this exit: its terminal path is BEAM's
resolve, which has no compare-and-set predicates and reports ``origin="new"``
even when its guarded row write found the row already terminal, so a close
from there could land on, or claim, a row that had moved on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

REJECTION_STANDS = "rejection_stands"
BASIS_REVIEWER_ANSWERED = "reviewer_answered_reply"
BASIS_WINDOW_ENDED = "continuation_window_ended"

ACCEPTED = "accepted"
CONTESTED = "contested"
NO_REPLY = "no_reply"

# Margin added to every window edge before the session is closed. The reviewer
# starts its continuation clock when its first verdict filing RETURNS, a moment
# after the message timestamp the server records, and a reconsideration that
# finishes at the hard deadline still has to file. Five minutes errs toward
# leaving the session open: closing a moment late costs nothing, closing while
# the reviewer could still revise would cut it off.
LAST_WORD_GRACE_S = 300.0


def continuation_window_s() -> Tuple[float, float]:
    """(response wait, total lifetime) the spawned reviewer was given, in seconds.

    Read from the same setting `orchestrator_dispatch` forwards to the reviewer,
    with the same sanitising: an unusable value is replaced by the default, and
    a negative one disables continuation (the reviewer clamps it to zero).
    """
    from .orchestrator_dispatch import (
        CONTINUATION_TOTAL_WAIT_FACTOR,
        DEFAULT_CONTINUATION_WAIT_S,
        _seconds,
    )

    wait_s = _seconds(
        os.environ.get("UNITARES_DIALECTIC_CONTINUATION_WAIT_S"),
        DEFAULT_CONTINUATION_WAIT_S,
    )
    wait_s = max(0.0, wait_s)
    return wait_s, wait_s * CONTINUATION_TOTAL_WAIT_FACTOR


@dataclass(frozen=True)
class _Msg:
    index: int
    phase: Optional[str]
    agent_id: Optional[str]
    agrees: Optional[bool]
    ts: Optional[datetime]


def _agrees(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"true", "1", "yes"}:
            return True
        if token in {"false", "0", "no"}:
            return False
        return None
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


def _ts(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _messages(transcript: Iterable[Any]) -> List[_Msg]:
    out: List[_Msg] = []
    for index, message in enumerate(transcript or []):
        if isinstance(message, dict):
            phase = message.get("phase") or message.get("message_type") or message.get("role")
            agent_id = message.get("agent_id")
            agrees = message.get("agrees")
            stamp = message.get("timestamp")
        else:
            phase = getattr(message, "phase", None)
            agent_id = getattr(message, "agent_id", None)
            agrees = getattr(message, "agrees", None)
            stamp = getattr(message, "timestamp", None)
        out.append(_Msg(index, phase, agent_id, _agrees(agrees), _ts(stamp)))
    return out


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def assess_rejection_stands(
    transcript: Iterable[Any],
    *,
    paused_agent_id: Optional[str],
    reviewer_agent_id: Optional[str],
    awaiting_facilitation: bool,
    now: datetime,
    is_orchestrated: Callable[[str], bool],
    wait_s: float,
    total_s: float,
) -> Optional[Dict[str, Any]]:
    """Return the ``rejection_stands`` resolution payload, or None to stay open.

    None whenever any condition is unmet or unreadable: not awaiting
    facilitation, a self-review, no independent reviewer verdict, a reviewer
    whose latest verdict is an approval, or a window that is open or cannot be
    computed. Every unknown keeps the session on its existing path.
    """
    if not awaiting_facilitation:
        return None
    if not paused_agent_id or not reviewer_agent_id or reviewer_agent_id == paused_agent_id:
        # Self-review is out of scope: there is no separate reviewer whose
        # window could end, and its guard has its own facilitation route.
        return None

    msgs = _messages(transcript)
    syntheses = [m for m in msgs if m.phase == "synthesis"]
    # Same reading as DialecticSession._reviewer_objection_stands: the last
    # synthesis verdict NOT from the paused agent, which survives reassignment.
    verdicts = [m for m in syntheses if m.agrees is not None and m.agent_id != paused_agent_id]
    if not verdicts:
        return None  # no reviewer answer: out of scope
    last = verdicts[-1]
    if last.agrees is not False:
        return None  # the reviewer's latest verdict is not a rejection
    if last.agent_id != reviewer_agent_id:
        # The standing rejection belongs to a reviewer since replaced (an
        # operator reassign). The current reviewer has not yet spoken and
        # still holds authority to revise it, so nobody has had a last word.
        return None

    paused = [m for m in syntheses if m.agent_id == paused_agent_id]
    replies_after = [m for m in paused if m.index > last.index]
    previous = verdicts[-2] if len(verdicts) > 1 else None
    answered = [
        m for m in paused
        if previous is not None and previous.index < m.index < last.index
    ]

    window_closed_at: Optional[datetime] = None
    if not replies_after and answered:
        # The reviewer reconsidered a reply to its earlier verdict and still
        # rejects. Nothing is pending on its side.
        basis = BASIS_REVIEWER_ANSWERED
    else:
        reviewer = last.agent_id
        if not is_orchestrated(reviewer):
            # Only the orchestrated reviewer declares a bounded window. A human
            # or unmanaged reviewer has no deadline to have passed.
            return None
        first = next((m for m in verdicts if m.agent_id == reviewer), None)
        if first is None or first.ts is None or last.ts is None:
            return None
        grace = timedelta(seconds=LAST_WORD_GRACE_S)
        hard_end = first.ts + timedelta(seconds=total_s)
        response_end = min(last.ts + timedelta(seconds=wait_s), hard_end)
        if replies_after:
            first_reply = replies_after[0].ts
            if first_reply is None:
                return None
            if first_reply < response_end + grace:
                # A timely reply: the reviewer owes a reconsideration and may
                # file one until its lifetime ends.
                window_end = hard_end + grace
            else:
                # The reply arrived after the reviewer stopped listening.
                window_end = response_end
        else:
            window_end = response_end + grace
        if now < window_end:
            return None
        basis = BASIS_WINDOW_ENDED
        window_closed_at = window_end

    if not replies_after:
        acceptance = NO_REPLY
    elif any(m.agrees is True for m in replies_after):
        acceptance = ACCEPTED
    else:
        acceptance = CONTESTED

    payload: Dict[str, Any] = {
        "action": REJECTION_STANDS,
        "reason": REJECTION_STANDS,
        "termination_basis": REJECTION_STANDS,
        "standing_verdict": "reject",
        "reviewer_approved": False,
        "last_word": "reviewer",
        "last_word_basis": basis,
        "verdict_agent_id": last.agent_id,
        "verdict_at": _iso(last.ts),
        "verdict_acceptance": acceptance,
        "paused_agent_reply_count": len(replies_after),
        "facilitation_requested": True,
        "window_closed_at": _iso(window_closed_at),
        "note": (
            "Closed because the reviewer's rejection stands and the reviewer has "
            "had its last word: no party with authority to revise the verdict "
            "remains. This is NOT a reviewer approval and NOT an inactivity sweep. "
            "verdict_acceptance describes the transcript only; the session was not "
            "closed on it, and no_reply is not assent."
        ),
    }
    if basis == BASIS_REVIEWER_ANSWERED and answered:
        payload["answered_reply_agrees"] = answered[-1].agrees
    return payload
