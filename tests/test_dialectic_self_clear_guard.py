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
        assert "no self-service" in result["operator_note"]
        assert "action='reassign'" in result["operator_note"]

    def test_repeat_attempts_are_refused_without_being_recorded(self):
        """Sweeper-starvation guard: only the FIRST attempt is persisted.

        Every persisted message refreshes `dialectic_sessions.updated_at`, and the
        auto-resolve sweeper selects on `updated_at < now() - threshold`. Recording
        every retry let an agent hold a blocked session `active` indefinitely — no
        resolution, no failure, no facilitation. So retries must not reach the
        transcript.
        """
        s = _session_at_synthesis()
        s.submit_synthesis(_synthesis(REVIEWER, agrees=False))

        first = s.submit_synthesis(_synthesis(PAUSED, agrees=True, ts="2026-08-09T00:04:00Z"))
        assert first["success"] is True, "the first attempt IS recorded (audit signal)"
        assert first["blocked"] == "reviewer_objection_stands"
        recorded = len(s.transcript)

        for i in range(3):
            r = s.submit_synthesis(
                _synthesis(PAUSED, agrees=True, ts=f"2026-08-09T00:0{5 + i}:00Z")
            )
            assert r["success"] is False, "retries are refused outright"
            assert r["blocked"] == "reviewer_objection_stands"
        assert len(s.transcript) == recorded, "no retry may be persisted"
        assert s.phase != DialecticPhase.RESOLVED

    def test_each_new_reviewer_rejection_opens_one_new_response_slot(self):
        """A renewed verdict starts a new round; it is not a retry.

        The old whole-transcript scan refused this second response even though
        ``dialectic(get)`` reported that the paused agent owed it.  The only
        escape was operator reassignment.
        """
        s = _session_at_synthesis()
        s.submit_synthesis(_synthesis(REVIEWER, agrees=False))

        first = s.submit_synthesis(
            _synthesis(PAUSED, agrees=True, ts="2026-08-09T00:03:00Z")
        )
        assert first["blocked"] == "reviewer_objection_stands"
        assert first["success"] is True

        renewed = s.submit_synthesis(
            _synthesis(REVIEWER, agrees=False, ts="2026-08-09T00:04:00Z")
        )
        assert renewed["success"] is True
        assert renewed["converged"] is False

        second = s.submit_synthesis(
            _synthesis(PAUSED, agrees=True, ts="2026-08-09T00:05:00Z")
        )
        assert second["blocked"] == "reviewer_objection_stands"
        assert second["success"] is True, "a new rejection reopens one response slot"

        recorded = len(s.transcript)
        repeat = s.submit_synthesis(
            _synthesis(PAUSED, agrees=True, ts="2026-08-09T00:06:00Z")
        )
        assert repeat["success"] is False
        assert len(s.transcript) == recorded

    def test_reassigning_the_reviewer_cannot_erase_a_standing_objection(self):
        """`_apply_reviewer_reassignment` repoints reviewer_agent_id and clears the
        facilitation flag. A guard keyed on the CURRENT reviewer id would orphan the
        previous reviewer's rejection and self-clear on the next call.
        """
        s = _session_at_synthesis()
        s.submit_synthesis(_synthesis(REVIEWER, agrees=False))
        assert s._reviewer_objection_stands() is True

        s.reviewer_agent_id = "reviewer-2"      # what reassignment does
        s.awaiting_facilitation = False
        assert s._reviewer_objection_stands() is True, "reassignment must not disarm the guard"

        result = s.submit_synthesis(_synthesis(PAUSED, agrees=True, ts="2026-08-09T00:05:00Z"))
        assert result["blocked"] == "reviewer_objection_stands"
        assert s.phase != DialecticPhase.RESOLVED


class TestLegitimateResolutionStillWorks:
    def test_reviewer_approval_resolves(self):
        s = _session_at_synthesis()
        result = s.submit_synthesis(_synthesis(REVIEWER, agrees=True))
        assert result["converged"] is True
        assert s.phase == DialecticPhase.RESOLVED

    def test_paused_agent_cannot_resolve_before_reviewer_posts_its_verdict(self):
        """The two-call reviewer boundary must not become a self-clear race."""
        s = _session_at_synthesis()
        result = s.submit_synthesis(_synthesis(PAUSED, agrees=True))
        assert result["success"] is False
        assert result["converged"] is False
        assert result["blocked"] == "reviewer_verdict_pending"
        assert result["awaiting_facilitation"] is False
        assert s.phase == DialecticPhase.SYNTHESIS
        assert not any(
            msg.phase == "synthesis" and msg.agent_id == PAUSED
            for msg in s.transcript
        )

    def test_reviewer_may_change_its_mind(self):
        """A later reviewer verdict supersedes the earlier rejection.

        No phase reset here: a rejecting synthesis leaves the session in SYNTHESIS,
        which the assertion below pins. An earlier version of this test reset
        `s.phase` by hand, which would have silently masked a regression that moved
        the phase off SYNTHESIS after a rejection.
        """
        s = _session_at_synthesis()
        s.submit_synthesis(_synthesis(REVIEWER, agrees=False))
        assert s.phase == DialecticPhase.SYNTHESIS
        result = s.submit_synthesis(_synthesis(REVIEWER, agrees=True, ts="2026-08-09T00:04:00Z"))
        assert result["converged"] is True

    def test_third_party_mediator_resolves_at_the_PROTOCOL_layer_only(self):
        """Pins protocol behaviour for a path that is NOT reachable in production.

        `handle_submit_synthesis` gates submission behind an allow-list of
        {paused_agent_id, reviewer_agent_id}, so a non-participant cannot reach
        `submit_synthesis` through the `dialectic` tool at all. This test therefore
        proves only that the protocol layer would accept a mediator if the handler
        ever let one through — it must NOT be read as evidence that third-party
        mediation is an available escape hatch for a blocked session.
        """
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


class TestSelfReviewIsNotExempt:
    """reviewer_mode='self' sets reviewer_agent_id == paused_agent_id.

    An earlier revision EXEMPTED self-review, reasoning that with no independent check
    there is nothing to override, and that blocking would strand self-review sessions
    that opened with a rejection. The record refutes the premise: 17 self-review
    verdicts across 10 sessions, ALL agrees=True, not one self-rejection in the entire
    history, and none at all since 2026-06-28. The exemption never fired, and its only
    live effect was to let a self-reviewer overturn its own rejection.

    So there is no exemption. A self-reviewer that records a rejection cannot then
    clear itself.
    """

    def _self_review_session(self) -> DialecticSession:
        s = DialecticSession(paused_agent_id=PAUSED, reviewer_agent_id=PAUSED)
        s.submit_thesis(
            DialecticMessage(
                phase="thesis",
                agent_id=PAUSED,
                timestamp="2026-08-09T00:00:00Z",
                root_cause="cause",
                reasoning="reasoning",
                proposed_conditions=["c1"],
            )
        )
        s.submit_antithesis(
            DialecticMessage(
                phase="antithesis",
                agent_id=PAUSED,
                timestamp="2026-08-09T00:01:00Z",
                reasoning="self-objection",
            )
        )
        return s

    def test_self_reviewer_cannot_overturn_its_own_rejection(self):
        s = self._self_review_session()
        s.submit_synthesis(_synthesis(PAUSED, agrees=False))
        result = s.submit_synthesis(_synthesis(PAUSED, agrees=True, ts="2026-08-09T00:03:00Z"))
        assert result["blocked"] == "reviewer_objection_stands"
        assert result["converged"] is False
        assert s.phase != DialecticPhase.RESOLVED

    def test_self_review_with_no_prior_rejection_still_resolves(self):
        """The guard fires on a recorded rejection, not on self-review per se.

        ⛔This pins the PROTOCOL layer only, and is not a statement that a
        self-review may resume its own agent. Whether that convergence releases
        a live pause is decided at the actuator — `handle_submit_synthesis`
        refuses to execute a self-reviewed resolution while the agent is
        actually paused and routes it to facilitation instead (#1585 item 1,
        `test_self_review_cannot_release_a_live_pause`). The protocol object
        cannot make that call because it cannot see live agent status.
        """
        s = self._self_review_session()
        result = s.submit_synthesis(_synthesis(PAUSED, agrees=True))
        assert result["converged"] is True
        assert s.phase == DialecticPhase.RESOLVED

    def test_the_scan_is_not_fooled_by_the_message_under_evaluation(self):
        """Regression pin: the helper must be read BEFORE the append.

        With a post-append scan, this agent's own agrees=True would be found as the
        'reviewer's latest verdict' and supersede its own earlier rejection — which
        is exactly how a two-party session would have been silently unblocked too.
        """
        s = _session_at_synthesis()
        s.submit_synthesis(_synthesis(REVIEWER, agrees=False))
        assert s._reviewer_objection_stands() is True
        s.transcript.append(_synthesis(PAUSED, agrees=True, ts="2026-08-09T00:03:00Z"))
        assert s._reviewer_objection_stands() is True, (
            "a paused-agent message must never satisfy the reviewer-verdict scan"
        )


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


class TestReviewerVerdictPendingHelper:
    def test_independent_antithesis_without_synthesis_is_pending(self):
        assert _session_at_synthesis()._reviewer_verdict_pending() is True

    def test_reviewer_synthesis_clears_pending_state(self):
        s = _session_at_synthesis()
        s.submit_synthesis(_synthesis(REVIEWER, agrees=False))
        assert s._reviewer_verdict_pending() is False

    def test_paused_negotiation_does_not_satisfy_reviewer_verdict(self):
        s = _session_at_synthesis()
        s.submit_synthesis(_synthesis(PAUSED, agrees=False))
        assert s._reviewer_verdict_pending() is True

    def test_explicit_self_review_has_no_independent_verdict_boundary(self):
        s = DialecticSession(paused_agent_id=PAUSED, reviewer_agent_id=PAUSED)
        s.transcript.append(DialecticMessage(
            phase="antithesis",
            agent_id=PAUSED,
            timestamp="2026-08-09T00:01:00Z",
            reasoning="self-review",
        ))
        s.phase = DialecticPhase.SYNTHESIS
        assert s._reviewer_verdict_pending() is False
