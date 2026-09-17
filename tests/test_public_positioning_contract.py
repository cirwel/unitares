"""Executable checks for the repeated public UNITARES product framing."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "check_doc_drift_public_positioning",
    PROJECT_ROOT / "scripts" / "diagnostics" / "check_doc_drift.py",
)
assert _SPEC and _SPEC.loader
check_doc_drift = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_doc_drift)


def test_current_public_positioning_satisfies_contract() -> None:
    assert check_doc_drift.public_positioning_failures(PROJECT_ROOT) == []


def test_missing_requirement_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "README.md").write_text("an accountability server\n", encoding="utf-8")
    monkeypatch.setattr(
        check_doc_drift,
        "PUBLIC_POSITIONING_CHECKS",
        {"README.md": [("product category", ("federation kernel",))]},
    )

    assert check_doc_drift.public_positioning_failures(tmp_path) == [
        "README.md: missing public positioning requirement 'product category'"
    ]


def test_requirement_accepts_any_declared_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "README.md").write_text("multi-harness kernel\n", encoding="utf-8")
    monkeypatch.setattr(
        check_doc_drift,
        "PUBLIC_POSITIONING_CHECKS",
        {
            "README.md": [
                ("product category", ("federation kernel", "multi-harness kernel"))
            ]
        },
    )

    assert check_doc_drift.public_positioning_failures(tmp_path) == []


def test_every_positioning_surface_shares_one_product_category() -> None:
    for rel_path, requirements in check_doc_drift.PUBLIC_POSITIONING_CHECKS.items():
        categories = [req for req in requirements if req[0] == "product category"]
        if categories:
            assert categories == [check_doc_drift.PRODUCT_CATEGORY], rel_path


def test_current_readme_has_no_volatile_literals() -> None:
    assert check_doc_drift.readme_volatility_failures(PROJECT_ROOT) == []


def test_readme_volatile_literals_are_reported(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "**Status:** v2.22.0.\n"
        "\n"
        "The deployment has run continuously since November 2025.\n"
        "\n"
        "The snapshot holds 4,573,890\n"
        "audit/telemetry events.\n",
        encoding="utf-8",
    )

    labels = [
        failure.split(": ", 1)[1].split(" '", 1)[0]
        for failure in check_doc_drift.readme_volatility_failures(tmp_path)
    ]
    assert labels == ["release version", "running-since claim", "count of changing things"]


def test_readme_badge_urls_are_not_volatile(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "[![Python](https://img.shields.io/badge/python-3.12.1+-blue)](https://example.org/v1.2.3)\n",
        encoding="utf-8",
    )

    assert check_doc_drift.readme_volatility_failures(tmp_path) == []


def test_every_positioning_surface_shares_one_tagline() -> None:
    """A surface may omit the tagline, but must not carry a variant of it."""
    for rel_path, requirements in check_doc_drift.PUBLIC_POSITIONING_CHECKS.items():
        taglines = [req for req in requirements if req[0] == "canonical tagline"]
        if taglines:
            assert taglines == [check_doc_drift.CANONICAL_TAGLINE], rel_path


def test_non_markdown_reader_surfaces_are_pinned() -> None:
    """CITATION.cff drifted because nothing but .md was ever checked."""
    checks = check_doc_drift.PUBLIC_POSITIONING_CHECKS
    for surface in ("README.md", "CITATION.cff", "docs/public-site/index.md"):
        assert check_doc_drift.CANONICAL_TAGLINE in checks[surface], surface


def test_citation_software_title_carries_the_tagline() -> None:
    citation = (PROJECT_ROOT / "CITATION.cff").read_text(encoding="utf-8")
    title_line = next(
        line for line in citation.splitlines() if line.startswith("title:")
    )
    assert check_doc_drift.CANONICAL_TAGLINE[1][0].casefold() in title_line.casefold()


def test_the_repo_description_guard_is_actually_wired() -> None:
    """A guard nothing invokes is not coverage, whatever the registry says.

    check_repo_description.py shipped wired to nothing while
    CANONICAL_SOURCES.md claimed the surface was covered -- the same
    "reports green, guards nothing" failure the positioning checks exist to
    prevent.
    """
    workflows = (PROJECT_ROOT / ".github" / "workflows").glob("*.yml")
    invoked = [
        wf.name
        for wf in workflows
        if "check_repo_description.py" in wf.read_text(encoding="utf-8")
    ]
    assert invoked, "no workflow runs scripts/diagnostics/check_repo_description.py"
