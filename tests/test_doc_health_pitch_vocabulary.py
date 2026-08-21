"""Regression tests for the pitch-surface vocabulary lint."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "diagnostics"
    / "check_doc_health.py"
)


@pytest.fixture(scope="module")
def doc_health():
    spec = importlib.util.spec_from_file_location(
        "check_doc_health_pitch_vocab", _SCRIPT_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _warnings(
    tmp_path: Path, monkeypatch, doc_health, rel_path: str, content: str
) -> list[str]:
    doc = tmp_path / rel_path
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(content)
    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)
    return doc_health.check_pitch_vocabulary([doc])


def test_flags_thermodynamic_on_root_readme(tmp_path, monkeypatch, doc_health):
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "README.md",
        "EISV is a thermodynamic health reading for agents.",
    )
    assert any("retired pitch vocabulary" in warning for warning in warnings)


def test_flags_thermodynamic_even_in_a_negation(tmp_path, monkeypatch, doc_health):
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "README.md",
        "Drawn from auditable heuristics rather than literal thermodynamic "
        "quantities.",
    )
    assert any("retired pitch vocabulary" in warning for warning in warnings)


def test_flags_freestanding_information_theoretic(tmp_path, monkeypatch, doc_health):
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "docs/public-site/index.md",
        "The information-theoretic and ODE formulation remains a research "
        "target.",
    )
    assert any("reserved pitch vocabulary" in warning for warning in warnings)


def test_allows_verbatim_paper_title_citation(tmp_path, monkeypatch, doc_health):
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "README.md",
        "  title  = {{UNITARES}: Information-Theoretic Governance of "
        "Heterogeneous Agent Fleets},",
    )
    assert warnings == []


def test_title_citation_does_not_shield_banned_terms(
    tmp_path, monkeypatch, doc_health
):
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "README.md",
        'See "Information-Theoretic Governance of Heterogeneous Agent Fleets"; '
        "the deployed system remains thermodynamic in spirit.",
    )
    assert any("retired pitch vocabulary" in warning for warning in warnings)


def test_flags_product_definition_surface(tmp_path, monkeypatch, doc_health):
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "docs/PRODUCT_DEFINITION.md",
        "A thermodynamic governance framework for AI agents.",
    )
    assert any("retired pitch vocabulary" in warning for warning in warnings)


def test_ignores_non_pitch_surfaces(tmp_path, monkeypatch, doc_health):
    for rel_path in (
        "docs/essays/thermodynamics.md",
        "docs/proposals/eisv-maths-roadmap-v0.md",
        "docs/ontology/eisv-proprioception-contract.md",
        "agents/sdk/README.md",
    ):
        warnings = _warnings(
            tmp_path,
            monkeypatch,
            doc_health,
            rel_path,
            "The thermodynamic and information-theoretic lineage is discussed "
            "here in full.",
        )
        assert warnings == []
