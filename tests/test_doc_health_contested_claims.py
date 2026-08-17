"""Regression tests for evaluator-facing contested-claim lint."""

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
        "check_doc_health_contested", _SCRIPT_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _warnings(tmp_path: Path, monkeypatch, doc_health, content: str) -> list[str]:
    doc = tmp_path / "docs" / "reader.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(content)
    monkeypatch.setattr(doc_health, "REPO_ROOT", tmp_path)
    return doc_health.check_contested_claims([doc])


def test_flags_cold_start_claim_applied_to_all_warmup(
    tmp_path, monkeypatch, doc_health
):
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "During baseline warmup, the live verdict falls back to the cold-start prior.",
    )
    assert any(
        "cold-start prior owns only check-ins 1–2" in warning for warning in warnings
    )


def test_allows_precise_three_stage_warmup(tmp_path, monkeypatch, doc_health):
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "Check-ins 1–2 use the cold-start prior; check-ins 3–24 use behavioral "
        "fixed thresholds; self-relative scoring starts at check-in 25.",
    )
    assert warnings == []


def test_flags_optional_redis_cache_wording(tmp_path, monkeypatch, doc_health):
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "Redis is an optional session cache.",
    )
    assert any("de-facto primary session store" in warning for warning in warnings)


@pytest.mark.parametrize(
    "claim",
    [
        "Redis is a session cache only and has a graceful fallback.",
        "Redis (optional) provides sticky session bindings.",
        "# Redis optional cache",
    ],
)
def test_flags_other_stale_redis_postures(
    tmp_path, monkeypatch, doc_health, claim
):
    warnings = _warnings(tmp_path, monkeypatch, doc_health, claim)
    assert any("de-facto primary session store" in warning for warning in warnings)


def test_flags_schema_auto_create_recovery(tmp_path, monkeypatch, doc_health):
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "Restart the server after reset; the schema auto-creates.",
    )
    assert any("refuses an uninitialized database" in warning for warning in warnings)


def test_flags_pre_pypi_sdk_claim(tmp_path, monkeypatch, doc_health):
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "Until its first PyPI release, install the SDK from Git.",
    )
    assert any("unitares-sdk 0.1.0 is published" in warning for warning in warnings)


def test_flags_stale_rest_tool_key(tmp_path, monkeypatch, doc_health):
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "curl -d '{\"tool\":\"health_check\",\"arguments\":{}}'",
    )
    assert any("accepts `name` plus `arguments`" in warning for warning in warnings)


def test_flags_unqualified_unforgeable_outcome_claim(tmp_path, monkeypatch, doc_health):
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "The signal is anchored to outcomes an agent can't fake.",
    )
    assert any("producer and provenance" in warning for warning in warnings)


def test_allows_provenance_bounded_outcome_claim(tmp_path, monkeypatch, doc_health):
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "CI-authored evidence is stronger than an unverified agent-authored outcome.",
    )
    assert warnings == []


def test_flags_superseded_weak_signal_headline(tmp_path, monkeypatch, doc_health):
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "The current honest read is a weak early signal at short lead.",
    )
    assert any("frozen 2026-08-09" in warning for warning in warnings)
