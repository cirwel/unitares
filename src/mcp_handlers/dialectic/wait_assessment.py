"""How long a dialectic session has been waiting, and whether that is yet odd.

`whose_move` (handlers.py) exists because the protocol fields are accurate but
easy to misread as a hang: on 2026-07-28 a live session waiting on the CALLER's
own synthesis was read as stalled by two experienced operators. That fix
answered **who** the session is waiting on.

It never answered **when**, and the same misreading recurs on the time axis. The
open-reviewer-slot text reads "wait or ask for facilitation" identically at
seventy seconds and at seventy minutes, so a poller has nothing to separate "too
early to tell" from "genuinely stuck." Dogfooding this on 2026-09-13 produced
the wrong call twice inside one session: a reviewer polled at 72s and again at
2m15s looked absent both times and was in fact mid-model-call, arriving at
~2m20s. An operator reading the same text has the same problem, and the error is
asymmetric — escalating early wastes a facilitation, while escalating late is
the backlog this whole surface is trying to drain.

So this module answers the second question, from the one clock that is always
present: the age of the last thing that happened in the transcript.

WHAT THE BUDGETS ARE, AND WHAT THEY ARE NOT
-------------------------------------------
The budgets below are derived from the reviewer's own configured ceilings, not
chosen to make sessions look healthy:

* a spawned reviewer must onboard, claim the slot, and return a model verdict;
  the model call alone is allowed ``UNITARES_DIALECTIC_CODEX_TIMEOUT_S`` (420s
  by default) in ``agents/dialectic_reviewer/reviewer.py``
* after a disagreement it polls for the paused agent's response every
  ``DEFAULT_CONTINUATION_POLL_S`` (15s) and may spend another model call
  reconsidering

`expected_by_s` is therefore "the point past which the reviewer has exceeded its
own declared budget" — NOT a promise, NOT an SLA, and NOT a liveness reading. A
session inside its budget is *unremarkable*, which is different from healthy: the
reviewer may already be dead and simply not yet late. Only `OVERDUE` is
evidence, and even then it is evidence that something should be *looked at*,
never authority to conclude the reviewer is gone — see issue #2202, where a
stall reproduced but its proximate cause did not.

This module reads. It never reassigns, fails, or escalates a session.
"""

from __future__ import annotations

import os
from typing import Any, Optional

# Mirror of agents.dialectic_reviewer.reviewer's model-call ceiling. Read from
# the same env var so an operator who widens one widens both; a contract test
# pins the default against the reviewer's without importing the runner into the
# governance server.
DEFAULT_VERDICT_TIMEOUT_S = 420.0
# Onboarding, slot claim, and protocol writes sit either side of the model call.
SPAWN_OVERHEAD_S = 60.0
# A reviewer reconsidering a response polls on a 15s cycle, then spends another
# model call. Same ceiling, same overhead for the write.
RECONSIDER_OVERHEAD_S = 30.0

TOO_EARLY = "too_early"
OVERDUE = "overdue"
UNREMARKABLE = "unremarkable"


def _verdict_timeout_s() -> float:
    raw = os.environ.get("UNITARES_DIALECTIC_CODEX_TIMEOUT_S", str(DEFAULT_VERDICT_TIMEOUT_S))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_VERDICT_TIMEOUT_S
    return value if value > 0 else DEFAULT_VERDICT_TIMEOUT_S


def expected_budget_s(*, awaiting: str) -> float:
    """Seconds within which the awaited party should normally have acted.

    ``awaiting`` is ``"verdict"`` (a spawned reviewer owes its first antithesis
    or synthesis) or ``"reconsideration"`` (a reviewer owes an answer to the
    paused agent's synthesis).
    """
    if awaiting == "reconsideration":
        return _verdict_timeout_s() + RECONSIDER_OVERHEAD_S
    return _verdict_timeout_s() + SPAWN_OVERHEAD_S


def assess_wait(
    *,
    elapsed_s: Optional[float],
    awaiting: str,
    orchestrated: bool = True,
) -> dict[str, Any]:
    """Classify a wait without claiming anything about liveness.

    Returns a block safe to merge into a session read. ``elapsed_s`` of None
    means the clock could not be established — reported as such rather than
    defaulted, because a missing reading and a short one are different findings
    and only one of them is reassuring.
    """
    budget = expected_budget_s(awaiting=awaiting)

    if elapsed_s is None:
        return {
            "elapsed_s": None,
            "expected_by_s": budget,
            "assessment": None,
            "note": (
                "Elapsed time could not be established from the transcript, so "
                "this wait is unclassified. Do not read that as either healthy "
                "or stuck."
            ),
        }

    if not orchestrated:
        # A human or unmanaged reviewer has no declared budget to exceed, so
        # there is nothing to be late against and inventing a deadline would
        # manufacture urgency the protocol never promised.
        return {
            "elapsed_s": round(elapsed_s, 1),
            "expected_by_s": None,
            "assessment": None,
            "note": (
                "No orchestrated reviewer is responsible for this slot, so there "
                "is no declared budget to exceed."
            ),
        }

    if elapsed_s < budget:
        remaining = budget - elapsed_s
        return {
            "elapsed_s": round(elapsed_s, 1),
            "expected_by_s": round(budget, 1),
            "assessment": TOO_EARLY,
            "note": (
                f"Waiting {elapsed_s:.0f}s of a {budget:.0f}s reviewer budget "
                f"({remaining:.0f}s left). A reviewer mid-model-call looks "
                f"identical to an absent one from here, so an absence read now "
                f"is not evidence. Poll again after the budget, not before."
            ),
        }

    return {
        "elapsed_s": round(elapsed_s, 1),
        "expected_by_s": round(budget, 1),
        "assessment": OVERDUE,
        "note": (
            f"Waiting {elapsed_s:.0f}s, past the {budget:.0f}s the reviewer is "
            f"allowed for this step. That is worth investigating; it is not by "
            f"itself proof the reviewer is gone (see issue #2202)."
        ),
    }


def suggests_facilitation(assessment: Optional[str]) -> bool:
    """Whether a response may invite the caller to escalate.

    Only an overdue wait may. Suggesting facilitation inside the budget is what
    produced two wrong calls in one session on 2026-09-13, and a facilitation
    spent on a reviewer that was about to answer is worse than waiting.
    """
    return assessment == OVERDUE
