"""Regression tests for the calibration signal feeding the drift vector.

Guards against the long-lived bug where `src/monitor_drift.py` called a
non-existent `calibration_checker.check()`, swallowed the AttributeError, and
silently fed `calibration_error=None` into every drift vector — plus the
mis-scaled `trajectory_health / 100.0` fallback. The signal now comes from the
TACTICAL overconfidence gap (declared confidence minus real success rate).
"""
from src.calibration import CalibrationChecker
from src.monitor_drift import compute_calibration_error, _MIN_TACTICAL_SAMPLES


def _fresh_checker(tmp_path) -> CalibrationChecker:
    # Isolate state to a temp file so the test never touches live calibration.
    return CalibrationChecker(state_file=str(tmp_path / "cal_state.json"))


def test_none_without_tactical_evidence(tmp_path):
    """No tactical samples -> None, so the drift engine uses its baseline."""
    checker = _fresh_checker(tmp_path)
    assert compute_calibration_error(checker) is None


def test_overconfidence_produces_positive_signal(tmp_path):
    """Declared >> actual success -> a real, positive calibration_error in (0, 1]."""
    checker = _fresh_checker(tmp_path)
    # Declared ~0.6, actual 0 -> overconfidence gap ~0.6.
    for _ in range(_MIN_TACTICAL_SAMPLES + 10):
        checker.record_tactical_decision(0.6, "proceed", False)
    err = compute_calibration_error(checker)
    assert err is not None
    assert 0.5 < err <= 1.0  # ~0.6


def test_underconfidence_clamps_to_zero(tmp_path):
    """Declared << actual success is not drift -> 0.0 (measured, not baseline)."""
    checker = _fresh_checker(tmp_path)
    for _ in range(_MIN_TACTICAL_SAMPLES + 10):
        checker.record_tactical_decision(0.3, "proceed", True)  # underconfident
    err = compute_calibration_error(checker)
    assert err == 0.0


def test_below_min_samples_returns_none(tmp_path):
    """A handful of samples is not enough to trust the signal."""
    checker = _fresh_checker(tmp_path)
    for _ in range(_MIN_TACTICAL_SAMPLES - 1):
        checker.record_tactical_decision(0.6, "proceed", False)
    assert compute_calibration_error(checker) is None


def test_worst_bin_not_cancelled_by_underconfident_bin(tmp_path):
    """An overconfident bin must not be cancelled by an underconfident one.

    A sample-weighted mean would average these toward ~0 and hide the real
    overconfidence; the worst-bin signal surfaces it.
    """
    checker = _fresh_checker(tmp_path)
    for _ in range(100):
        checker.record_tactical_decision(0.6, "proceed", False)   # overconfident, gap ~0.6
    for _ in range(100):
        checker.record_tactical_decision(0.9, "proceed", True)    # underconfident, gap ~ -0.1
    err = compute_calibration_error(checker)
    assert err is not None
    assert err > 0.5  # the 0.6 bin dominates, not a ~0.25 average
