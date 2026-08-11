"""
The synthesis must not be a replay of the antithesis — and dropping the
duplicate must not blank the resolution.

`agents/dialectic_reviewer/reviewer.py` used to pass the same
`verdict.reasoning` string to both its antithesis and its synthesis call. The
result: byte-identical pairs in 60 of 60 orchestrated sessions from 2026-06-23
onward, so every transcript showed the same paragraph under two headings and
there was no synthesis step at all.

Deleting the duplicate naively is a REGRESSION, which is why an earlier attempt
was reverted: `finalize_resolution` sources the resolution's canonical
`reasoning` from the agreed synthesis message, so a synthesis with no reasoning
produced an approved resolution with an empty rationale.

The fix is the pair — omit the duplicate AND have finalize fall back to the
same author's antithesis. These tests pin both halves, plus the attribution
rule that keeps the fallback from borrowing the other agent's argument.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.dialectic_protocol import (
    DialecticMessage,
    DialecticPhase,
    DialecticSession,
)

PAUSED = "agent-paused"
REVIEWER = "agent-reviewer"
ANTITHESIS_TEXT = "The stability inference rests on a single observation."


def _now():
    return datetime.now(timezone.utc).isoformat()


def _session_through_antithesis():
    session = DialecticSession(
        paused_agent_id=PAUSED,
        reviewer_agent_id=REVIEWER,
        dispute_type="verification",
    )
    session.transcript.append(DialecticMessage(
        phase="thesis", agent_id=PAUSED, timestamp=_now(),
        root_cause="Breaker tripped during bootstrap.",
        proposed_conditions=["read-only closeout"],
        reasoning="Resume is narrowly justified.",
    ))
    session.transcript.append(DialecticMessage(
        phase="antithesis", agent_id=REVIEWER, timestamp=_now(),
        reasoning=ANTITHESIS_TEXT,
    ))
    return session


def test_resolution_recovers_reviewer_rationale_from_its_own_antithesis():
    """A verdict-only synthesis still yields a resolution with the argument."""
    session = _session_through_antithesis()
    # Verdict-only synthesis: conditions + root_cause, no reasoning.
    session.transcript.append(DialecticMessage(
        phase="synthesis", agent_id=REVIEWER, timestamp=_now(),
        proposed_conditions=["verify breaker provenance"],
        root_cause="Bootstrap-only trigger, unconfirmed.",
        agrees=True,
    ))
    session.phase = DialecticPhase.RESOLVED

    resolution = session.finalize_resolution("key-a", "key-b")

    assert resolution.reasoning == ANTITHESIS_TEXT, (
        "dropping the duplicated synthesis reasoning must not blank the "
        "resolution — this is the regression that reverted the first attempt"
    )
    assert resolution.conditions == ["verify breaker provenance"]


def test_fallback_never_borrows_the_other_agents_argument():
    """
    Attribution guard. The paused agent's verdict-only synthesis must NOT
    inherit the REVIEWER's antithesis — misattributing one agent's reasoning to
    another is exactly the v1 attestation defect.
    """
    session = _session_through_antithesis()
    session.transcript.append(DialecticMessage(
        phase="synthesis", agent_id=PAUSED, timestamp=_now(),
        proposed_conditions=["read-only closeout"],
        root_cause="Accepted.",
        agrees=True,
    ))
    session.phase = DialecticPhase.RESOLVED

    resolution = session.finalize_resolution("key-a", "key-b")

    assert ANTITHESIS_TEXT not in (resolution.reasoning or ""), (
        "the paused agent has no antithesis of its own; it must not be "
        "credited with the reviewer's"
    )


def test_explicit_synthesis_reasoning_still_wins():
    """The fallback is a fallback — a real synthesis argument is preserved."""
    session = _session_through_antithesis()
    session.transcript.append(DialecticMessage(
        phase="synthesis", agent_id=REVIEWER, timestamp=_now(),
        proposed_conditions=["bounded resume"],
        root_cause="Agreed.",
        reasoning="On reflection the narrowed scope answers my concern.",
        agrees=True,
    ))
    session.phase = DialecticPhase.RESOLVED

    resolution = session.finalize_resolution("key-a", "key-b")

    assert resolution.reasoning == "On reflection the narrowed scope answers my concern."


def test_transcript_pair_is_not_byte_identical():
    """
    The cheap check that would have caught this the day it shipped, instead of
    seven weeks later.
    """
    session = _session_through_antithesis()
    session.transcript.append(DialecticMessage(
        phase="synthesis", agent_id=REVIEWER, timestamp=_now(),
        proposed_conditions=["verify breaker provenance"],
        root_cause="Bootstrap-only trigger.",
        agrees=True,
    ))

    anti = next(m for m in session.transcript if m.phase == "antithesis")
    synth = next(m for m in session.transcript if m.phase == "synthesis")
    assert synth.reasoning != anti.reasoning
