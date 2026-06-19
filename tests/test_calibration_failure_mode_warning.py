"""Tests for the verdict-quality warning in characterize_failure_modes.

The alarming "INVERTED calibration curve" warning previously fired off the
STRATEGIC channel (trajectory_health), a saturated proxy whose near-ceiling
jitter (e.g. 0.969 low-confidence vs 0.933 high-confidence) tripped it falsely.
Now: a curve counts as inverted only past a material margin, and the warning is
sourced from the TACTICAL (real-outcome) channel, with strategic used only as a
clearly-labeled proxy fallback.
"""
from src.calibration import CalibrationBin, CalibrationChecker


def _bin(lo, hi, *, n, declared, actual) -> CalibrationBin:
    correct = round(actual * n)
    return CalibrationBin(
        bin_range=(lo, hi),
        count=n,
        predicted_correct=correct,
        actual_correct=correct,
        accuracy=actual,
        expected_accuracy=declared,
        calibration_error=abs(actual - declared),
    )


def _checker(tmp_path) -> CalibrationChecker:
    return CalibrationChecker(state_file=str(tmp_path / "cal.json"))


def test_near_ceiling_jitter_is_not_inverted(tmp_path):
    """0.969 (low conf) vs 0.933 (high conf): margin 0.036 < 0.05 -> not inverted."""
    checker = _checker(tmp_path)
    bins = {
        "0.0-0.5": _bin(0.0, 0.5, n=50, declared=0.30, actual=0.969),
        "0.9-1.0": _bin(0.9, 1.0, n=50, declared=0.95, actual=0.933),
    }
    dim = checker._characterize_dimension(bins, min_samples=5)
    assert dim["curve_inverted"] is False


def test_material_inversion_still_flagged(tmp_path):
    """A real inversion (high-conf 0.60 << low-conf 0.90) exceeds the margin."""
    checker = _checker(tmp_path)
    bins = {
        "0.0-0.5": _bin(0.0, 0.5, n=50, declared=0.30, actual=0.90),
        "0.9-1.0": _bin(0.9, 1.0, n=50, declared=0.95, actual=0.60),
    }
    dim = checker._characterize_dimension(bins, min_samples=5)
    assert dim["curve_inverted"] is True


def test_warning_prefers_tactical_over_saturated_strategic(tmp_path, monkeypatch):
    """A materially-inverted strategic proxy must not warn when tactical is clean."""
    checker = _checker(tmp_path)
    strat_inverted = {
        "0.0-0.5": _bin(0.0, 0.5, n=50, declared=0.30, actual=0.90),
        "0.9-1.0": _bin(0.9, 1.0, n=50, declared=0.95, actual=0.60),
    }
    tactical_clean = {
        "0.0-0.5": _bin(0.0, 0.5, n=50, declared=0.30, actual=0.35),
        "0.9-1.0": _bin(0.9, 1.0, n=50, declared=0.92, actual=0.93),
    }
    monkeypatch.setattr(checker, "compute_calibration_metrics", lambda: strat_inverted)
    monkeypatch.setattr(checker, "compute_tactical_metrics", lambda: tactical_clean)
    fm = checker.characterize_failure_modes(min_samples=5)
    assert fm["verdict_quality_warning"] is None


def test_strategic_only_warning_is_labeled_proxy(tmp_path, monkeypatch):
    """With no tactical data, a strategic warning must disclose it is a proxy."""
    checker = _checker(tmp_path)
    strat_inverted = {
        "0.0-0.5": _bin(0.0, 0.5, n=50, declared=0.30, actual=0.90),
        "0.9-1.0": _bin(0.9, 1.0, n=50, declared=0.95, actual=0.60),
    }
    monkeypatch.setattr(checker, "compute_calibration_metrics", lambda: strat_inverted)
    monkeypatch.setattr(checker, "compute_tactical_metrics", lambda: {})
    fm = checker.characterize_failure_modes(min_samples=5)
    warning = fm["verdict_quality_warning"]
    assert warning is not None
    assert "proxy" in warning.lower()


def test_tactical_inversion_warns_without_proxy_label(tmp_path, monkeypatch):
    """A genuine tactical inversion warns, and is NOT labeled a proxy."""
    checker = _checker(tmp_path)
    tactical_inverted = {
        "0.0-0.5": _bin(0.0, 0.5, n=50, declared=0.30, actual=0.90),
        "0.9-1.0": _bin(0.9, 1.0, n=50, declared=0.95, actual=0.60),
    }
    monkeypatch.setattr(checker, "compute_calibration_metrics", lambda: {})
    monkeypatch.setattr(checker, "compute_tactical_metrics", lambda: tactical_inverted)
    fm = checker.characterize_failure_modes(min_samples=5)
    warning = fm["verdict_quality_warning"]
    assert warning is not None
    assert "INVERTED" in warning
    assert "proxy" not in warning.lower()
