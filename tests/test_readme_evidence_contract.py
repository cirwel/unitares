"""Regression guards for the root README's product/evidence boundary."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text()
EVIDENCE = (ROOT / "docs" / "EVIDENCE_AND_LIMITS.md").read_text()
NORMALIZED_EVIDENCE = " ".join(EVIDENCE.split())


def test_readme_leads_with_the_product_and_earned_capabilities() -> None:
    assert "Accountability infrastructure for long-running AI agents" in README
    for capability in (
        "Identity and lineage",
        "Claims and evidence",
        "Governed review",
        "Outcome grounding",
        "Digital proprioception",
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
    """Keep deployment and trust limits, not a particular section structure."""
    for phrase in (
        "single-operator deployment",
        "agents also help build the system",
        "trust boundary is one operator and its explicitly trusted issuer",
        "Multi-issuer trust requires lease principals to retain both issuer and subject",
    ):
        assert phrase in NORMALIZED_EVIDENCE
    for reference in (
        "operations/DEPLOYMENT_DATA_CAVEAT.md",
        "proposals/active/federation-trust-decisions-2026-09-25.md",
        "UNIFIED_ARCHITECTURE.md",
    ):
        assert reference in EVIDENCE


def test_readme_stays_a_landing_page() -> None:
    assert "make demo" not in README
    assert "make coordination-demo" not in README
    assert "dialectic-mediated review" not in README
    assert "cannot be described as clean single-read blinding" not in README


def test_readme_offers_a_no_install_path_and_complements_git_and_handoffs() -> None:
    demo = "https://cirwel.github.io/unitares/demo.html"
    assert README.count(demo) >= 2
    assert README.index(demo) < README.index("## Install")
    assert "Do not install UNITARES" not in README
    assert "works alongside Git, issue trackers, handoff notes" in README
