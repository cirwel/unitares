"""Regression tests for the generated public GitHub Pages site."""

from pathlib import Path

from scripts.dev.build_public_site import build


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_build_separates_product_landing_from_glossary(tmp_path):
    build(tmp_path, None)

    landing = (tmp_path / "index.html").read_text(encoding="utf-8")
    glossary = (tmp_path / "glossary.html").read_text(encoding="utf-8")
    server_version = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()

    assert "Runtime accountability for long-lived AI agents" in landing
    assert "External adoption remains unvalidated" in landing
    assert "selection-aware null" in landing
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


def test_build_writes_normalized_cname(tmp_path):
    build(tmp_path, " unitares.cirwel.org ")

    assert (tmp_path / "CNAME").read_text(encoding="utf-8") == (
        "unitares.cirwel.org\n"
    )
