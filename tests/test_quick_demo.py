from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_quick_demo():
    path = Path(__file__).resolve().parents[1] / "scripts" / "demo" / "quick_demo.py"
    spec = importlib.util.spec_from_file_location("quick_demo", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


quick_demo = _load_quick_demo()


def test_extract_decision_from_compact_shape():
    result = {
        "decision": {
            "action": "pause",
            "reason": "risk crossed threshold",
            "margin": "critical",
        }
    }

    assert quick_demo.extract_decision(result) == {
        "action": "pause",
        "reason": "risk crossed threshold",
        "margin": "critical",
    }


def test_extract_decision_from_mirror_verdict_shape():
    result = {
        "verdict": {
            "value": "pause",
            "meaning": "Needs attention.",
            "next_action": "Stop current work.",
        },
        "margin": "critical",
    }

    assert quick_demo.extract_decision(result) == {
        "action": "pause",
        "reason": "Needs attention.",
        "margin": "critical",
    }


def test_extract_decision_from_minimal_shape():
    result = {"action": "proceed", "margin": "wide"}

    assert quick_demo.extract_decision(result) == {
        "action": "proceed",
        "reason": "No reason supplied.",
        "margin": "wide",
    }


def test_extract_decision_from_standard_shape():
    result = {"decision": "guide", "summary": "guide | coherence=0.42"}

    assert quick_demo.extract_decision(result) == {
        "action": "guide",
        "reason": "guide | coherence=0.42",
        "margin": "-",
    }


def test_extract_decision_from_metrics_verdict_fallback():
    result = {
        "metrics": {
            "verdict": {
                "value": "safe",
                "meaning": "Behavioral assessment: low risk.",
            }
        }
    }

    assert quick_demo.extract_decision(result) == {
        "action": "safe",
        "reason": "Behavioral assessment: low risk.",
        "margin": "-",
    }


def test_extract_decision_reports_available_keys():
    with pytest.raises(KeyError, match="keys: metrics, success"):
        quick_demo.extract_decision({"success": True, "metrics": {}})


def test_extract_metrics_accepts_top_level_minimal_fields():
    result = {
        "E": 0.7,
        "I": 0.8,
        "S": 0.1,
        "V": 0.0,
        "coherence": 0.52,
        "risk_score": 0.22,
        "risk_score_latest": 0.47,
    }

    assert quick_demo.extract_metrics(result) == result


def test_fmt_metrics_tolerates_missing_values():
    assert quick_demo.fmt_metrics({}) == "E=- I=- S=- V=-  coh=- risk=-"


def test_declared_baseline_threshold_matches_behavioral_state():
    """The demo prints a baseline threshold; re-derive it from the live constant.

    The demo is a standalone stdlib script (no repo imports), so it hardcodes
    the number it prints. This test is the guard: if BASELINE_WARMUP_UPDATES or
    the is_baselined rule changes, the demo's claim about how far it is from
    baselining goes stale and this fails.
    """
    from src.behavioral_state import BASELINE_WARMUP_UPDATES

    # is_baselined <=> baseline_confidence >= 0.8, where
    # baseline_confidence = (update_count - 5) / (BASELINE_WARMUP_UPDATES - 5)
    expected = 5 + 0.8 * (BASELINE_WARMUP_UPDATES - 5)

    assert quick_demo.BASELINE_UPDATES_REQUIRED == expected, (
        f"demo claims baseline at {quick_demo.BASELINE_UPDATES_REQUIRED} check-ins, "
        f"but behavioral_state implies {expected}"
    )


def test_trajectory_stays_under_baseline_and_carries_no_confession():
    """The demo must not look like a detection demo.

    Two properties it has to keep:
      1. It runs entirely in warmup, so nothing it prints can be read as
         evidence about the per-agent self-relative model.
      2. No step has the agent confess a failure in its own response text.
         Text-level self-incrimination is not a signal the server can rely on.
    """
    assert len(quick_demo.TRAJECTORY) < quick_demo.BASELINE_UPDATES_REQUIRED

    confession_markers = ("did not actually", "i lied", "actually failed")
    for text, _complexity, _confidence in quick_demo.TRAJECTORY:
        lowered = text.lower()
        for marker in confession_markers:
            assert marker not in lowered, f"confession-style step reintroduced: {text!r}"
