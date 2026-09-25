"""Regression tests for the generated public GitHub Pages site."""

from pathlib import Path

from scripts.dev.build_public_site import build


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_build_separates_product_landing_from_glossary(tmp_path):
    build(tmp_path, None)

    landing = (tmp_path / "index.html").read_text(encoding="utf-8")
    glossary = (tmp_path / "glossary.html").read_text(encoding="utf-8")
    server_version = (PROJECT_ROOT / "PUBLISHED_VERSION").read_text(encoding="utf-8").strip()
    landing_text = " ".join(landing.split())

    # The landing copies the README's tagline and definition verbatim; cirwel.org
    # pins the same strings against the README (its claims register), so one
    # sentence stays one sentence across every surface.
    assert "Accountability infrastructure for long-running AI agents" in landing_text
    readme_text = " ".join((PROJECT_ROOT / "README.md").read_text(encoding="utf-8").split())
    definition = (
        "Its single-operator federation kernel connects independent runtimes to one "
        "operator-controlled server over MCP or HTTP"
    )
    assert definition in readme_text
    assert definition in landing_text
    # The landing states what runs and links the ledger; the scope wording
    # lives in the ledger, where a reviewer reads it beside the data.
    assert "docs/EVIDENCE_AND_LIMITS.md" in landing
    ledger_text = " ".join(
        (PROJECT_ROOT / "docs" / "EVIDENCE_AND_LIMITS.md").read_text(encoding="utf-8").split()
    )
    assert "External adoption remains unvalidated" in ledger_text
    assert "did not establish predictive lift" in ledger_text
    assert "read-specific power" in ledger_text
    assert "inconclusive, not a demonstrated negative" in ledger_text
    assert "its claims, evidence, and behavior drift apart" in landing_text
    assert "Claims and evidence" in landing
    assert 'class="hero-actions"' in landing
    assert 'href="#try-the-released-surfaces"' in landing
    assert 'href="#evaluate-the-project"' in landing
    assert "github.com/cirwel/unitares/discussions" in landing
    assert "https://pypi.org/project/unitares-sdk/" in landing
    assert f"releases/tag/v{server_version}" in landing
    assert f"ghcr.io/cirwel/unitares:v{server_version}" in landing
    assert "field is inventing terms" not in landing

    assert "UNITARES Glossary" in glossary
    assert "field is inventing terms" in glossary
    assert 'href="index.html"' in glossary

    assert (tmp_path / "drift-audit.html").is_file()
    assert (tmp_path / "favicon.svg").is_file()
    assert 'rel="icon" href="favicon.svg"' in landing
    assert "docs/public-site/index.md" in landing


def test_build_generates_viewer_from_glossary_markdown(tmp_path):
    build(tmp_path, None)

    viewer = (tmp_path / "glossary-viewer.html").read_text(encoding="utf-8")

    # Template tokens replaced with real data parsed from glossary.md.
    assert "__GLOSSARY_DATA_JSON__" not in viewer
    assert "__REPO_URL__" not in viewer
    assert "substrate (inference)" in viewer
    assert '"rosetta"' in viewer

    # The other pages link to the viewer.
    glossary = (tmp_path / "glossary.html").read_text(encoding="utf-8")
    assert 'href="glossary-viewer.html"' in glossary


def test_build_writes_normalized_cname(tmp_path):
    build(tmp_path, " unitares.cirwel.org ")

    assert (tmp_path / "CNAME").read_text(encoding="utf-8") == (
        "unitares.cirwel.org\n"
    )
