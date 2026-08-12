"""Regression tests for the generated public GitHub Pages site."""

from scripts.dev.build_public_site import build


def test_build_separates_product_landing_from_glossary(tmp_path):
    build(tmp_path, None)

    landing = (tmp_path / "index.html").read_text(encoding="utf-8")
    glossary = (tmp_path / "glossary.html").read_text(encoding="utf-8")

    assert "Runtime accountability for long-lived AI agents" in landing
    assert "External adoption remains unvalidated" in landing
    assert "selection-aware null" in landing
    assert "https://pypi.org/project/unitares-sdk/" in landing
    assert "ghcr.io/cirwel/unitares:v2.17.0" in landing
    assert "field is inventing terms" not in landing

    assert "UNITARES Glossary" in glossary
    assert "field is inventing terms" in glossary
    assert 'href="index.html"' in glossary

    assert (tmp_path / "drift-audit.html").is_file()
    assert (tmp_path / "favicon.svg").is_file()
    assert 'rel="icon" href="favicon.svg"' in landing
    assert "docs/public-site/index.md" in landing


def test_build_writes_normalized_cname(tmp_path):
    build(tmp_path, " unitares.cirwel.org ")

    assert (tmp_path / "CNAME").read_text(encoding="utf-8") == (
        "unitares.cirwel.org\n"
    )
