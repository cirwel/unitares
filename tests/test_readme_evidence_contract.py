"""Regression guards for the root README's product/evidence boundary."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text()
EVIDENCE = (ROOT / "docs" / "EVIDENCE_AND_LIMITS.md").read_text()


def test_readme_leads_with_the_product_and_earned_capabilities() -> None:
    assert "Accountability infrastructure for long-running AI agents" in README
    for capability in (
        "Identity and lineage",
        "Claims and evidence",
        "Governed review",
        "Outcome grounding",
        "Runtime policy",
        "Reconstruction",
    ):
        assert capability in README


def test_readme_links_to_evidence_instead_of_repeating_the_ledger() -> None:
    assert "[Evidence and limits](docs/EVIDENCE_AND_LIMITS.md)" in README
    assert "[Reviewer Guide](docs/REVIEWER_GUIDE.md)" in README
    assert "## Evidence and limits" not in README
    assert "### Current claim status" not in README
    assert "Predictive lift |" not in README
    assert "Incident prevention or benefit from pausing |" not in README


def test_detailed_qualifications_remain_on_the_evidence_surface() -> None:
    for phrase in (
        "single-operator and co-development dogfood",
        "Local control and cross-operator trust",
        "Identity binding and the lease plane",
    ):
        assert phrase in EVIDENCE


def test_readme_stays_a_landing_page() -> None:
    assert "make demo" not in README
    assert "make coordination-demo" not in README
    assert "dialectic-mediated review" not in README
    assert "cannot be described as clean single-read blinding" not in README
