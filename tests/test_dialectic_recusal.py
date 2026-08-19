"""The review system must not adjudicate claims about the review system.

Courts do not rely on a judge noticing their own conflict; recusal is a routing
rule that runs before the judge sees the case. These tests pin that the same is
true here — detection happens at request time, on text, before any reviewer is
assigned, and never asks the reviewer to introspect.

Motivating case: session ``def32eb2b4b2ce93`` (2026-08-19). A thesis claiming the
reviewer-quality track could be closed was auto-assigned to the orchestrated
reviewer. The reviewer reviewed it well — it found two objections withheld as an
answer key — but never raised that it was the instrument under test and stood to
be rated favourably by agreeing. That objection required noticing its own
position, which is the thing self-review is worst at.
"""

import pytest

from src.mcp_handlers.dialectic.recusal import (
    RECUSAL_ENV,
    detect_subject_matter_conflict,
    recusal_mode,
)


@pytest.fixture(autouse=True)
def _default_mode(monkeypatch):
    monkeypatch.delenv(RECUSAL_ENV, raising=False)


class TestMode:
    def test_defaults_to_enforce(self):
        assert recusal_mode() == "enforce"

    @pytest.mark.parametrize("value", ["enforce", "flag", "off"])
    def test_known_modes_round_trip(self, monkeypatch, value):
        monkeypatch.setenv(RECUSAL_ENV, value)
        assert recusal_mode() == value

    def test_unknown_mode_fails_toward_enforce(self, monkeypatch):
        # An unreadable setting must not silently disable the guard.
        monkeypatch.setenv(RECUSAL_ENV, "yes-please")
        assert recusal_mode() == "enforce"

    def test_off_disables_detection(self, monkeypatch):
        monkeypatch.setenv(RECUSAL_ENV, "off")
        assert detect_subject_matter_conflict("reviewer quality is the issue") is None


class TestDetectsTheConflictItWasBuiltFor:
    def test_the_actual_thesis_that_motivated_this(self):
        """Verbatim shape of the 2026-08-19 self-test thesis."""
        finding = detect_subject_matter_conflict(
            "CLAIM: the dialectic's reviewer-quality problem is solved, and the "
            "track can be closed. Splitting at 2026-07-02, when "
            "UNITARES_DIALECTIC_REVIEWER_HOST=codex was activated..."
        )
        assert finding is not None
        assert "review system" in finding.reason

    def test_one_decisive_term_is_enough(self):
        for text in [
            "is the orchestrated reviewer trustworthy yet?",
            "reviewers rubber-stamp rather than engage",
            "we should measure reviewer independence directly",
        ]:
            assert detect_subject_matter_conflict(text) is not None, text

    def test_two_protocol_terms_are_enough(self):
        finding = detect_subject_matter_conflict(
            "the antithesis never lands because the dialectic session times out"
        )
        assert finding is not None
        assert len(finding.matched) >= 2

    def test_scans_every_field_not_just_reasoning(self):
        # topic and reason are set at request time and are often where the
        # subject is stated most plainly.
        assert detect_subject_matter_conflict(None, None, "Reviewer quality", None)
        assert detect_subject_matter_conflict(None, None, None, "audit the dialectic reviewer")

    def test_matches_across_line_breaks(self):
        assert detect_subject_matter_conflict("is reviewer\n   quality good enough?")


class TestDoesNotOverreach:
    def test_ordinary_theses_are_not_recused(self):
        for text in [
            "The migration failed because slot 059 was already taken.",
            "Resume: CI is green and the deploy verified on the running process.",
            "Root cause: the lease was held past its TTL by a crashed worker.",
            "The calibration constant is fitted on one device, not a class.",
        ]:
            assert detect_subject_matter_conflict(text) is None, text

    def test_a_single_incidental_protocol_mention_is_not_enough(self):
        # The whole point of the two-term rule: a thesis may name one protocol
        # term in passing while being about something else.
        assert detect_subject_matter_conflict(
            "I paused mid-refactor; the antithesis of my approach would be to "
            "rewrite the module wholesale, which I am not proposing."
        ) is None

    def test_governance_vocabulary_alone_does_not_trip_it(self):
        # "governance", "verdict", "pause" are ordinary here and must stay usable.
        assert detect_subject_matter_conflict(
            "The governance verdict paused me at high risk; I believe the pause "
            "was correct and I am proposing conditions to resume."
        ) is None

    def test_empty_input_is_not_a_conflict(self):
        assert detect_subject_matter_conflict() is None
        assert detect_subject_matter_conflict(None, "", None) is None


class TestFindingShape:
    def test_finding_is_a_routing_decision_not_a_verdict(self):
        finding = detect_subject_matter_conflict("reviewer quality")
        payload = finding.as_dict()
        assert payload["recused"] is True
        assert payload["basis"] == "subject_matter_conflict"
        assert "not a verdict" in payload["note"]
        assert payload["matched_terms"]

    def test_finding_names_what_matched_so_a_false_positive_is_diagnosable(self):
        finding = detect_subject_matter_conflict(
            "the dialectic session stalled and no reviewer verdict arrived"
        )
        assert finding is not None
        assert set(finding.matched) >= {"dialectic session", "reviewer verdict"}
