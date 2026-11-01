"""Regression guards for the public evidence record.

The detailed record lives in docs/EVIDENCE_AND_LIMITS.md so the README can stay
a landing page. Guard material evidence boundaries without requiring a table
layout or a taxonomy ahead of the results.
"""

from __future__ import annotations

import re
from pathlib import Path


EVIDENCE = (
    Path(__file__).resolve().parents[1] / "docs" / "EVIDENCE_AND_LIMITS.md"
).read_text()
NORMALIZED = " ".join(EVIDENCE.split())


def test_ledger_distinguishes_operational_decisions_from_scientific_refutation() -> None:
    """The full taxonomy may be linked, but its inference boundary stays clear."""
    assert "ontology/falsification-inference-containment-2026-08-22.md" in EVIDENCE
    assert "operational `FAIL` can close a scheduled line of work" in NORMALIZED
    assert "without scientifically refuting the underlying capability" in NORMALIZED
    assert (
        "target, counterfactual, independent unit, power, decision rule, and read protocol"
        in NORMALIZED
    )


def test_ledger_does_not_turn_the_december_gate_into_scientific_authority() -> None:
    """Repeated interim reads prevent clean single-read confirmatory framing.

    These assert the standing claims, not a snapshot. A ledger that carries all
    three cannot also frame December as decisive without contradicting itself,
    so banning any particular phrasing adds no coverage and would fail on the
    next innocent use of those words.
    """
    assert "not the only post-registration read" in NORMALIZED
    assert "cannot be described as clean single-read blinding" in NORMALIZED
    assert "remains an operational decision rule" in NORMALIZED


def test_ledger_discloses_the_interim_execution_counts() -> None:
    """The disclosure must carry concrete counts — but the counts are a dated
    audit reading, not an invariant.

    Pinning `42 of 51` would make a snapshot load-bearing: those jobs are
    paused, and if they resume or are re-audited the true number changes and
    this test fails for being right. Per the ephemeral-snapshot rule in
    CLAUDE.md, a reading taken at a moment does not get frozen. Assert the
    shape of the disclosure and let the values move.
    """
    assert re.search(r"completed \d+ of \d+ executions", NORMALIZED)
    assert re.search(r"guard completed \d+ of \d+", NORMALIZED)
    assert "must disclose the interim access and read-specific power" in NORMALIZED


def test_ledger_preserves_earned_and_unearned_claims_side_by_side() -> None:
    """Operational evidence stays visible without being promoted to efficacy."""
    assert "records sustained maintainer traffic" in NORMALIZED
    assert "Governed pauses are delivered" in NORMALIZED
    assert "current rate has not been re-measured" in NORMALIZED
    assert "did not establish predictive lift" in NORMALIZED
    assert "inconclusive, not a demonstrated negative" in NORMALIZED
    # The effect of pausing is stated as an open research question, not framed
    # against a prevention claim the project does not make (operator direction 2026-11-01).
    assert "Whether pausing changes outcomes is an open research question" in NORMALIZED
    assert "sets no standing AUC ceiling" in NORMALIZED


def test_ledger_separates_the_review_mechanism_from_its_benefit() -> None:
    """Review execution and corrections do not establish comparative benefit.

    The mechanism is exercised and countable; comparative benefit is not
    measured. Neither should erase the other, regardless of the page layout.
    """
    assert "cannot resolve its own review over a standing reviewer objection" in NORMALIZED
    assert "no matched unreviewed or conventional-review control" in NORMALIZED
    assert (
        "no estimate of incident reduction, net productivity, or UNITARES's incremental contribution"
        in NORMALIZED
    )


def test_ledger_states_the_review_coverage_boundary() -> None:
    """These records cover one review channel, not every review that happens.

    Consequential review also runs through subagent councils and external
    models, which write no dialectic row unless filed through
    `reviewer_provenance`. A claim derived from these records without the
    boundary silently generalises past the channel it measured.
    """
    assert (
        "Dialectic review, councils, external models, and ordinary repository review"
        in NORMALIZED
    )
    assert "incomplete common instrumentation" in NORMALIZED


def test_ledger_does_not_launder_a_parse_failure_as_a_verdict() -> None:
    """One recorded dissent is a parser default, not a reviewer's judgement.

    `agrees` is the column a reviewer sets, and a model whose output failed to
    parse defaults to disagreement in it. Reporting the dissent count without
    that carve-out overstates recorded disagreement.
    """
    assert "parse failure recorded as disagreement" in NORMALIZED
