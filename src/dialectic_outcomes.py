"""Terminal-outcome classification for dialectic sessions.

Issue #1689. `core.dialectic_sessions.status` conflates three different
endings, and every reader that partitions on it gets the same wrong answer:

  * protocol failure -- a crash or a malformed submission;
  * canary/probe traffic -- which ends `failed` BY DESIGN and was never
    going to answer;
  * a legitimate unresolved disagreement awaiting facilitation -- the case
    where the dialectic worked exactly as intended.

The third reads as the first. A reviewer that does its job well, refutes a
thesis correctly, and is never facilitated is recorded identically to a dead
probe, so a raw resolution rate penalises the dialectic for producing real
disagreement.

The signal already exists on the row: `awaiting_facilitation` is set when the
self-clear guard refuses a paused agent's attempt to resolve over a standing
objection. Measured over the live corpus, it is exact -- true on every swept
session, false on every resolved one. So this module fixes the readers rather
than the terminal write, which is the far smaller blast radius on a
load-bearing path.

Canary partitioning uses `core.agents.label LIKE 'canary_dialectic%'`.
It deliberately does NOT use `trigger_source`, which is the literal string
'manual' for every row in the table and therefore partitions nothing.
"""

from __future__ import annotations

from typing import Dict, Optional

# The terminal outcomes a reader should actually distinguish.
RESOLVED = "resolved"
UNRESOLVED_AWAITING_FACILITATION = "unresolved_awaiting_facilitation"
FAILED = "failed"
CANARY = "canary"
OPEN = "open"

# Statuses that mean the session has stopped, whatever the reason.
#
# ⛔TWO OF THESE FOUR ARE UNSTORABLE, and that is not a defect to "fix".
# `dialectic_sessions_status_check` permits only
# {active, resolved, escalated, failed, quorum_voting}, so `timeout` and
# `abandoned` are rejected by Postgres with 23514 and have never existed. They
# are listed here defensively: this is an analytics classifier, and a status it
# does not know reads as OPEN, so dropping them would silently misclassify if
# the CHECK is ever widened. See `test_terminal_statuses_vs_schema_domain`.
#
# ⛔DO NOT propagate this set into write guards. It is not a write gate, and
# `DialecticDB.TERMINAL_WRITE_GUARD` deliberately differs — read the rationale
# there before changing either. On 2026-08-23 a session read the difference
# between the two as five drifted lists and "reconciled" them: widening the
# guards to include `escalated` made a stray escalated row IMMORTAL (the
# sweeper's reader still returns it as live, every write is then refused, and
# `reopen_session` only accepts `failed`), which is exactly the outcome
# `TERMINAL_WRITE_GUARD`'s comment says it exists to prevent. The two sets look
# inconsistent and are not.
TERMINAL_STATUSES = frozenset({"resolved", "failed", "timeout", "abandoned"})

CANARY_LABEL_PREFIX = "canary_dialectic"


def is_canary_label(label: Optional[str]) -> bool:
    """True for the probe traffic that always ends `failed` by design."""
    return bool(label) and label.startswith(CANARY_LABEL_PREFIX)


def classify_outcome(
    status: Optional[str],
    awaiting_facilitation: Optional[bool],
    paused_agent_label: Optional[str] = None,
    standing_rejection: Optional[bool] = None,
) -> str:
    """Classify one session into the outcome a reader should key on.

    Order matters. Canary is checked first because a probe's `failed` says
    nothing about dialectic quality either way, so it must not land in the
    denominator. The unresolved case is checked before `failed` because a
    standing, unfacilitated objection is what `failed` was hiding.

    ``standing_rejection`` -- does the session's most recent reviewer synthesis
    carry ``agrees=false``, unsuperseded -- is the RELIABLE signal, and callers
    that can supply it should. ``awaiting_facilitation`` is the fallback, kept
    so existing callers keep working, and it is a fallback rather than the
    primary for a measured reason:

    This module's header used to claim the flag is exact -- "true on every
    swept session, false on every resolved one" -- measured 2026-07-28..08-18.
    It has since decayed. Re-measured 2026-09-09 over 30 days: still perfectly
    SPECIFIC (no resolved session carries it) but no longer SENSITIVE, missing
    13 of 44 failed sessions. 11 of those 13 were reaped by the Python
    inactivity sweeper, which does not set it.

    ⛔The flag does not mean what its name suggests, so do not "repair" it at
    the writer. It is set in `mcp_handlers/dialectic/auto_resolve.py` under
    `check_time > fail_time`, a reviewer-liveness TIMING condition recording
    that the sweeper raised a facilitation request -- not a verdict about the
    transcript. An earlier reading of it as "the paused agent returned and its
    self-clear was refused" was REFUTED by measurement: 24 sessions carry the
    flag with the paused agent having never replied. And the Python sweeper's
    own reap-description docstring says it deliberately never claims a verdict
    because it never loads the transcript, so making it set the flag would
    fabricate a facilitation request that was never raised.

    Hence the fix belongs here, in the reader, with the transcript-derived
    predicate supplied by the caller.
    """
    if is_canary_label(paused_agent_label):
        return CANARY
    if status not in TERMINAL_STATUSES:
        return OPEN
    if status == "resolved":
        return RESOLVED
    if standing_rejection:
        return UNRESOLVED_AWAITING_FACILITATION
    if standing_rejection is None and awaiting_facilitation:
        # No transcript-derived answer available; fall back to the flag, which
        # is specific enough that a true reading is still trustworthy.
        return UNRESOLVED_AWAITING_FACILITATION
    return FAILED


def resolution_rate(counts: Dict[str, int]) -> Optional[float]:
    """resolved / (resolved + failed), or None when the denominator is empty.

    Unresolved-awaiting-facilitation sessions are excluded from BOTH terms.
    They are not successes and they are not failures -- they are sessions
    still waiting on a human, and counting them either way asserts an outcome
    that has not happened yet.
    """
    resolved = counts.get(RESOLVED, 0)
    failed = counts.get(FAILED, 0)
    denominator = resolved + failed
    if denominator == 0:
        return None
    return resolved / denominator
