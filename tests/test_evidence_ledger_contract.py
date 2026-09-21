"""Regression guards for the public claim ledger.

The ledger moved from the root README to docs/EVIDENCE_AND_LIMITS.md when the
README became a landing page. These guards moved with it, unchanged in intent.
"""

from __future__ import annotations

import re
from pathlib import Path


EVIDENCE = (
    Path(__file__).resolve().parents[1] / "docs" / "EVIDENCE_AND_LIMITS.md"
).read_text()
NORMALIZED = " ".join(EVIDENCE.split())


def test_ledger_uses_evidence_classes_instead_of_project_sentiment() -> None:
    """The ledger must distinguish result types before interpreting them."""
    for evidence_class in (
        "Operational observation",
        "Benchmark pass / fail",
        "Non-detection",
        "Unidentified / inconclusive",
        "Mismatch / path bound",
        "Untested",
    ):
        assert evidence_class in NORMALIZED

    assert "it is not a positive or negative judgement about the project" in NORMALIZED
    assert (
        "A registered operational `FAIL` can close a scheduled line of work"
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
    assert "Sustained operation | **Operational observation**" in NORMALIZED
    assert (
        "Pause actuation and delivery | **Event reconciled; protection untested**"
        in NORMALIZED
    )
    assert (
        "Predictive lift | **Non-detection; inconclusive for weak effects**"
        in NORMALIZED
    )
    assert "Incident prevention or benefit from pausing | **Untested**" in NORMALIZED
    assert "sets no standing AUC ceiling" in NORMALIZED


def test_ledger_separates_the_review_mechanism_from_its_benefit() -> None:
    """One row cannot carry both claims without one of them being wrong.

    The mechanism is exercised and countable; the benefit is not measured. A
    single "Untested" row understated the first, and a single "Exercised path"
    row would overstate the second.
    """
    assert "Review binds on the reviewed agent | **Exercised path**" in NORMALIZED
    assert "Comparative benefit from review and coordination | **Untested**" in NORMALIZED


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
