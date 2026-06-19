"""Tests for the class-scoped overconfidence advisory.

The global calibration gate is class-unscoped: it bins by confidence across the
whole fleet, so an overconfident cohort (e.g. ephemeral sessions declaring ~1.0
and achieving ~0.67) can hide behind well-calibrated peers. This advisory makes
that masking visible without changing what gates.

`calibration_gap = empirical_accuracy - mean_confidence` (negative == overconfident).
"""
from src.mcp_handlers.admin.calibration import (
    _derive_class_overconfidence_advisories,
    _CLASS_OVERCONFIDENCE_MIN_SAMPLES,
)


def _env(classes):
    return {"bootstrapped": True, "by_class": classes}


def test_overconfident_class_is_surfaced():
    env = _env({
        "ephemeral": {
            "eligible_samples": 1503,
            "mean_confidence": 0.9995,
            "empirical_accuracy": 0.6713,
            "calibration_gap": -0.3282,
        },
    })
    out = _derive_class_overconfidence_advisories(env)
    assert len(out) == 1
    assert out[0]["class_tag"] == "ephemeral"
    assert abs(out[0]["overconfidence"] - 0.3282) < 1e-6
    assert out[0]["eligible_samples"] == 1503


def test_underconfident_class_not_surfaced():
    env = _env({
        "engaged_ephemeral": {
            "eligible_samples": 93,
            "mean_confidence": 0.7293,
            "empirical_accuracy": 1.0,
            "calibration_gap": 0.2707,  # underconfident
        },
    })
    assert _derive_class_overconfidence_advisories(env) == []


def test_overconfident_but_below_min_samples_not_surfaced():
    env = _env({
        "ephemeral": {
            "eligible_samples": _CLASS_OVERCONFIDENCE_MIN_SAMPLES - 1,
            "mean_confidence": 0.99,
            "empirical_accuracy": 0.50,
            "calibration_gap": -0.49,
        },
    })
    assert _derive_class_overconfidence_advisories(env) == []


def test_mild_overconfidence_under_gate_not_surfaced():
    env = _env({
        "session_like": {
            "eligible_samples": 500,
            "mean_confidence": 0.80,
            "empirical_accuracy": 0.65,
            "calibration_gap": -0.15,  # under the 0.20 gate
        },
    })
    assert _derive_class_overconfidence_advisories(env) == []


def test_sorted_worst_first_and_empty_safe():
    env = _env({
        "a": {"eligible_samples": 100, "mean_confidence": 0.9, "empirical_accuracy": 0.6, "calibration_gap": -0.30},
        "b": {"eligible_samples": 100, "mean_confidence": 0.9, "empirical_accuracy": 0.4, "calibration_gap": -0.50},
    })
    out = _derive_class_overconfidence_advisories(env)
    assert [a["class_tag"] for a in out] == ["b", "a"]  # worst first
    assert _derive_class_overconfidence_advisories(None) == []
    assert _derive_class_overconfidence_advisories({"by_class": {}}) == []
