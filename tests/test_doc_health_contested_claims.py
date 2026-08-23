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


def test_flags_a_ceiling_claim_built_on_the_non_detection(
    tmp_path, monkeypatch, doc_health
):
    """The negative direction is a contested claim too, not a safe default."""
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "The permutation null found no predictive lift, and that bounds the EISV "
        "score's forecasting power.",
    )
    assert any("unsupported in the negative direction" in w for w in warnings)


def test_flags_calling_the_frozen_read_a_negative_result(
    tmp_path, monkeypatch, doc_health
):
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "The frozen 2026-08-09 outcome-lift evaluation is a negative result.",
    )
    assert any("non-detection and a dated" in w for w in warnings)


def test_weak_signal_rule_no_longer_claims_the_frozen_read_superseded_it(
    tmp_path, monkeypatch, doc_health
):
    """The rule stands; its justification was wrong and must stay corrected.

    A run with ~3% power against a weak effect cannot supersede a weak-signal
    read — it is simply silent about it. See
    docs/operations/falsifiability-power-audit-2026-08-23.md.
    """
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "The current honest read is a weak early signal.",
    )
    assert warnings, "the optimistic overclaim must still be flagged"
    assert any("unresolved pending" in w for w in warnings)
    assert not any("superseded" in w for w in warnings)


def test_allows_the_corrected_non_detection_wording(tmp_path, monkeypatch, doc_health):
    """The accurate framing must pass, or the lint just moves the ratchet."""
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "The frozen 2026-08-09 evaluation detected no predictive lift. Predictive "
        "lift is unresolved pending the pre-registered 2026-12-01 read.",
    )
    assert warnings == []


def test_flags_the_working_circuit_breaker_claim(tmp_path, monkeypatch, doc_health):
    """The positive-direction overclaim, on the claim that actually sells.

    The contract ledger refutes "the enforcement path working live" (row 28) and
    records fleet protection as untested (row 27). Only actuation is earned.
    """
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "The defensible claim today is an accountability instrument with one "
        "working circuit breaker, not incident prevention.",
    )
    assert any("demonstrably" in w.lower() for w in warnings)


def test_allows_the_actuation_claim_the_ledger_supports(
    tmp_path, monkeypatch, doc_health
):
    warnings = _warnings(
        tmp_path,
        monkeypatch,
        doc_health,
        "An accountability instrument whose circuit breaker demonstrably "
        "actuates; that it protects is untested.",
    )
    assert warnings == []
