"""
The paused agent may not resolve its own session over a standing reviewer rejection.

Measured on live data 2026-08-09: 13 of 15 resolved non-probe sessions since
2026-06-28 resolved because the paused agent posted its own agrees=True over a
reviewer that had just said agrees=False (median 2.2 min after the rejection).
The reviewer's verdict had never once blocked a resumption.

These tests pin the guard AND the cases it must not break — in particular that a
session still resolves when the reviewer agrees, when a third-party mediator
agrees, and when the reviewer changes its mind. Blocking must route to
facilitation rather than stranding the session, because the orchestrated reviewer
subprocess exits right after posting and would never return to ratify.
"""

import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.dialectic_protocol import (  # noqa: E402
    DialecticSession,
    DialecticMessage,
    DialecticPhase,
)

PAUSED = "agent-paused"
REVIEWER = "agent-reviewer"
MEDIATOR = "agent-third-party"


def _session_at_synthesis() -> DialecticSession:
    """A session driven to SYNTHESIS the normal way: thesis then antithesis."""
    s = DialecticSession(paused_agent_id=PAUSED, reviewer_agent_id=REVIEWER)
    s.submit_thesis(
        DialecticMessage(
            phase="thesis",
            agent_id=PAUSED,
            timestamp="2026-08-09T00:00:00Z",
            root_cause="claimed cause",
            reasoning="let me resume",
            proposed_conditions=["c1"],
        )
    )
    s.submit_antithesis(
        DialecticMessage(
            phase="antithesis",
            agent_id=REVIEWER,
            timestamp="2026-08-09T00:01:00Z",
            reasoning="the claimed cause is unproven",
            concerns=["unproven"],
        )
    )
    assert s.phase == DialecticPhase.SYNTHESIS
    return s


def _synthesis(agent_id: str, agrees: bool, ts: str = "2026-08-09T00:02:00Z") -> DialecticMessage:
    return DialecticMessage(
        phase="synthesis",
        agent_id=agent_id,
        timestamp=ts,
        root_cause="cause",
        reasoning="reasoning",
        proposed_conditions=["c1"],
        agrees=agrees,
    )


class TestSelfClearBlocked:
    def test_paused_agent_cannot_clear_standing_reviewer_rejection(self):
        s = _session_at_synthesis()
        s.submit_synthesis(_synthesis(REVIEWER, agrees=False))

        result = s.submit_synthesis(_synthesis(PAUSED, agrees=True, ts="2026-08-09T00:03:00Z"))

        assert result["success"] is True, "the message is still recorded"
        assert result["converged"] is False
        assert result["blocked"] == "reviewer_objection_stands"
        assert s.phase != DialecticPhase.RESOLVED

    def test_blocking_routes_to_facilitation_not_a_stall(self):
        """The reviewer subprocess is already gone; the session must reach a human."""
        s = _session_at_synthesis()
        s.submit_synthesis(_synthesis(REVIEWER, agrees=False))

        result = s.submit_synthesis(_synthesis(PAUSED, agrees=True, ts="2026-08-09T00:03:00Z"))

        assert result["awaiting_facilitation"] is True
        assert s.awaiting_facilitation is True

    def test_repeated_self_clear_attempts_do_not_resolve(self):
        s = _session_at_synthesis()
        s.submit_synthesis(_synthesis(REVIEWER, agrees=False))
        for i in range(3):
            r = s.submit_synthesis(
                _synthesis(PAUSED, agrees=True, ts=f"2026-08-09T00:0{4 + i}:00Z")
            )
            assert r["converged"] is False
        assert s.phase != DialecticPhase.RESOLVED


class TestLegitimateResolutionStillWorks:
    def test_reviewer_approval_resolves(self):
        s = _session_at_synthesis()
        result = s.submit_synthesis(_synthesis(REVIEWER, agrees=True))
        assert result["converged"] is True
        assert s.phase == DialecticPhase.RESOLVED

    def test_paused_agent_agrees_with_no_reviewer_verdict_yet_resolves(self):
        """Unchanged behaviour: nothing to override when the reviewer is silent."""
        s = _session_at_synthesis()
        result = s.submit_synthesis(_synthesis(PAUSED, agrees=True))
        assert result["converged"] is True
        assert s.phase == DialecticPhase.RESOLVED

    def test_reviewer_may_change_its_mind_and_then_paused_agent_may_agree(self):
        """A later reviewer verdict supersedes the earlier rejection."""
        s = _session_at_synthesis()
        s.submit_synthesis(_synthesis(REVIEWER, agrees=False))
        s.phase = DialecticPhase.SYNTHESIS  # reviewer re-engages
        result = s.submit_synthesis(_synthesis(REVIEWER, agrees=True, ts="2026-08-09T00:04:00Z"))
        assert result["converged"] is True

    def test_third_party_mediator_can_resolve_over_a_rejection(self):
        """Option-B mediator path: a non-participant is not the interested party."""
        s = _session_at_synthesis()
        s.submit_synthesis(_synthesis(REVIEWER, agrees=False))
        result = s.submit_synthesis(_synthesis(MEDIATOR, agrees=True, ts="2026-08-09T00:04:00Z"))
        assert result["converged"] is True
        assert result.get("synthesizer") == MEDIATOR
        assert s.phase == DialecticPhase.RESOLVED

    def test_paused_agent_disagreement_is_untouched(self):
        s = _session_at_synthesis()
        s.submit_synthesis(_synthesis(REVIEWER, agrees=False))
        result = s.submit_synthesis(_synthesis(PAUSED, agrees=False, ts="2026-08-09T00:04:00Z"))
        assert result["converged"] is False
        assert result.get("blocked") is None


class TestObjectionStandsHelper:
    def test_no_reviewer_means_no_standing_objection(self):
        s = DialecticSession(paused_agent_id=PAUSED, reviewer_agent_id=None)
        assert s._reviewer_objection_stands() is False

    def test_silent_reviewer_means_no_standing_objection(self):
        s = _session_at_synthesis()
        assert s._reviewer_objection_stands() is False

    def test_last_reviewer_verdict_wins(self):
        s = _session_at_synthesis()
        s.transcript.append(_synthesis(REVIEWER, agrees=False, ts="2026-08-09T00:02:00Z"))
        assert s._reviewer_objection_stands() is True
        s.transcript.append(_synthesis(REVIEWER, agrees=True, ts="2026-08-09T00:03:00Z"))
        assert s._reviewer_objection_stands() is False

    def test_paused_agent_verdicts_do_not_count_as_reviewer_verdicts(self):
        s = _session_at_synthesis()
        s.transcript.append(_synthesis(PAUSED, agrees=False, ts="2026-08-09T00:02:00Z"))
        assert s._reviewer_objection_stands() is False
