"""Executable checks for the repeated public UNITARES product framing."""

from __future__ import annotations

import importlib.util
import json
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


def test_agent_facing_surfaces_are_pinned() -> None:
    """Agents read the bootstraps and skills before any public surface."""
    checks = check_doc_drift.PUBLIC_POSITIONING_CHECKS
    for surface in (
        "CLAUDE.md",
        "AGENTS.md",
        "skills/unitares-governance/SKILL.md",
        "skills/governance-fundamentals/SKILL.md",
    ):
        assert check_doc_drift.CANONICAL_TAGLINE in checks[surface], surface
        assert check_doc_drift.PRODUCT_CATEGORY in checks[surface], surface


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


@pytest.fixture
def eisv_frontdoor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    owner = tmp_path / "docs/EISV_COMPUTATION.md"
    owner.parent.mkdir()
    owner.write_text("## Role, runtime and evidence\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[EISV contract](docs/EISV_COMPUTATION.md#role-runtime-and-evidence)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_doc_drift, "EISV_FRAMING_SURFACES", ("README.md",))
    monkeypatch.setattr(check_doc_drift, "EISV_FRAMING_LINK_SURFACES", ("README.md",))
    return tmp_path


def test_current_eisv_framing_satisfies_contract() -> None:
    assert check_doc_drift.eisv_framing_failures(PROJECT_ROOT) == []


@pytest.mark.parametrize("claim", [
    "Two optional pieces sit around the record.",
    "EISV is an optional behavioral-state instrument.",
    "EISV --> recommendation; optional",
    "Denying it undermines your credibility.",
    "Entropy below X within Y check-ins",
    "**Ignoring the metrics**: Arguing against a pause while your entropy is near 1.0 and energy is at 0.3.",
])
@pytest.mark.parametrize("wrapped", [False, True])
def test_observed_eisv_framing_regressions_are_reported(eisv_frontdoor, claim, wrapped):
    if wrapped:
        claim = claim.replace(" ", "\n")
    with (eisv_frontdoor / "README.md").open("a", encoding="utf-8") as out:
        out.write(claim + "\n")
    failures = check_doc_drift.eisv_framing_failures(eisv_frontdoor)
    assert len(failures) == 1
    assert "EISV framing drift" in failures[0]
    assert check_doc_drift.EISV_FRAMING_OWNER in failures[0]


def test_eisv_guard_allows_staged_direction_and_scoped_interpretations(eisv_frontdoor):
    with (eisv_frontdoor / "README.md").open("a", encoding="utf-8") as out:
        out.write(
            "Optional-module separation is staged architecture work.\n"
            "The estimator currently runs synchronously.\n"
            "A policy may label its classification `healthy`.\n"
            "Thermodynamic and information-theoretic meanings remain research targets.\n"
            "Physical sensor inputs can supply EISV observations.\n"
        )
    assert check_doc_drift.eisv_framing_failures(eisv_frontdoor) == []


def test_eisv_guard_excludes_historical_and_registered_artifacts(eisv_frontdoor):
    for rel in (
        "docs/proposals/archive/historical.md",
        "docs/proposals/registered/protocol.md",
        "docs/evaluations/dated-result.md",
    ):
        path = eisv_frontdoor / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Two optional pieces sit around the record.\n", encoding="utf-8")
    assert check_doc_drift.eisv_framing_failures(eisv_frontdoor) == []


@pytest.mark.parametrize("replacement", [
    "EISV has a computation contract.\n",
    "[wrong owner](elsewhere/EISV_COMPUTATION.md#role-runtime-and-evidence)\n",
    "[wrong section](docs/EISV_COMPUTATION.md#other)\n",
    f"[public owner]({check_doc_drift.EISV_FRAMING_PUBLIC_OWNER})\n",
])
def test_eisv_frontdoor_must_link_to_actual_owner(eisv_frontdoor, replacement):
    (eisv_frontdoor / "README.md").write_text(replacement, encoding="utf-8")
    failures = check_doc_drift.eisv_framing_failures(eisv_frontdoor)
    assert len(failures) == 1
    assert "missing link to EISV framing owner" in failures[0]


@pytest.mark.parametrize("link,valid", [
    (check_doc_drift.EISV_FRAMING_PUBLIC_OWNER, True),
    ("../../docs/EISV_COMPUTATION.md#role-runtime-and-evidence", False),
    (check_doc_drift.EISV_FRAMING_PUBLIC_OWNER.replace("cirwel/unitares/", "another/repository/"), False),
    (check_doc_drift.EISV_FRAMING_PUBLIC_OWNER.replace("#role-runtime-and-evidence", "#other"), False),
])
def test_eisv_skill_owner_link_must_work_outside_the_repo(eisv_frontdoor, monkeypatch, link, valid):
    rel_path = "skills/governance-fundamentals/SKILL.md"
    skill = eisv_frontdoor / rel_path
    skill.parent.mkdir(parents=True)
    skill.write_text(f"[EISV contract]({link})\n", encoding="utf-8")
    monkeypatch.setattr(check_doc_drift, "EISV_FRAMING_SURFACES", (rel_path,))
    monkeypatch.setattr(check_doc_drift, "EISV_FRAMING_LINK_SURFACES", (rel_path,))
    failures = check_doc_drift.eisv_framing_failures(eisv_frontdoor)
    if valid:
        assert failures == []
    else:
        assert len(failures) == 1
        assert f"missing link to EISV framing owner {check_doc_drift.EISV_FRAMING_PUBLIC_OWNER}" in failures[0]


def test_eisv_owner_link_requires_existing_heading(eisv_frontdoor):
    (eisv_frontdoor / "docs/EISV_COMPUTATION.md").write_text(
        "## A different section\n", encoding="utf-8",
    )
    assert check_doc_drift.eisv_framing_failures(eisv_frontdoor) == [
        f"{check_doc_drift.EISV_FRAMING_OWNER}: missing EISV framing owner heading"
    ]


@pytest.mark.parametrize("key,description", [
    ("process_agent_update", "EISV includes Void\n(V)"),
    ("process_agent_update", '{"eisv_labels": {"V": "Void"}}'),
    ("simulate_update", '"phi": float, # Primary\nphysics signal'),
])
def test_eisv_description_regressions_are_scoped_to_relevant_tools(eisv_frontdoor, key, description):
    (eisv_frontdoor / "src").mkdir()
    path = eisv_frontdoor / "src/tool_descriptions.json"
    path.write_text(json.dumps({key: description}), encoding="utf-8")
    failures = check_doc_drift.eisv_framing_failures(eisv_frontdoor)
    assert len(failures) == 1
    assert f"src/tool_descriptions.json:{key}: EISV framing drift" in failures[0]
    path.write_text(json.dumps({"unrelated_tool": description}), encoding="utf-8")
    assert check_doc_drift.eisv_framing_failures(eisv_frontdoor) == []


def test_eisv_description_guard_preserves_legitimate_void_and_research_terms(eisv_frontdoor):
    (eisv_frontdoor / "src").mkdir()
    (eisv_frontdoor / "src/tool_descriptions.json").write_text(json.dumps({
        "process_agent_update": 'Behavioral Valence (V); ODE void_active is a distinct field.',
        "simulate_update": 'Physical and information-theoretic interpretations remain research targets.',
    }), encoding="utf-8")
    assert check_doc_drift.eisv_framing_failures(eisv_frontdoor) == []


def test_eisv_framing_check_runs_in_existing_doc_drift_gate(monkeypatch, capsys):
    monkeypatch.setattr(check_doc_drift, "eisv_framing_failures", lambda root: ["EISV framing sentinel"])
    assert check_doc_drift.main() == 1
    assert "EISV framing sentinel" in capsys.readouterr().out
