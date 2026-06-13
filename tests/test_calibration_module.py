"""
Comprehensive tests for src/calibration.py (~490 lines).

Tests the CalibrationChecker class which handles calibration of agent
confidence estimates. It tracks how well agents' confidence predictions
match reality using calibration curves, Brier-style scores, binned
accuracy, and complexity discrepancy.

This is mostly pure math/statistics -- no mocking needed.
"""

import pytest
import sys
import json
import math
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Tuple, Optional

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.calibration import (
    CalibrationChecker,
    CalibrationBin,
    ComplexityCalibrationBin,
    get_calibration_checker,
    _CalibrationCheckerProxy,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def checker(tmp_path):
    """Create a CalibrationChecker with an isolated tmp state file and JSON backend."""
    state_file = tmp_path / "cal_state.json"
    c = CalibrationChecker(state_file=state_file)
    c._backend = "json"  # force JSON so tests don't touch real DBs
    # JSON snapshot is always written (write-through cache for sync cold-start)
    c.reset()
    return c


@pytest.fixture
def checker_no_persist(tmp_path):
    """Checker that does not write state (for speed)."""
    state_file = tmp_path / "cal_state_no_persist.json"
    c = CalibrationChecker(state_file=state_file)
    c._backend = "json"
    # JSON snapshot always written but to tmpdir, so no impact
    c.reset()
    return c


# ===========================================================================
# Dataclass sanity
# ===========================================================================

class TestDataclasses:

    def test_calibration_bin_fields(self):
        b = CalibrationBin(
            bin_range=(0.8, 0.9),
            count=10,
            predicted_correct=8,
            actual_correct=7,
            accuracy=0.7,
            expected_accuracy=0.85,
            calibration_error=0.15,
        )
        assert b.count == 10
        assert b.accuracy == pytest.approx(0.7)
        assert b.calibration_error == pytest.approx(0.15)

    def test_complexity_calibration_bin_fields(self):
        b = ComplexityCalibrationBin(
            bin_range=(0.0, 0.1),
            count=5,
            mean_discrepancy=0.05,
            mean_reported=0.4,
            mean_derived=0.35,
            high_discrepancy_rate=0.0,
        )
        assert b.bin_range == (0.0, 0.1)
        assert b.high_discrepancy_rate == 0.0


# ===========================================================================
# Initialization / reset
# ===========================================================================

class TestInitAndReset:

    def test_default_bins(self, checker):
        expected = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0)]
        assert checker.bins == expected

    def test_custom_bins(self, tmp_path):
        custom = [(0.0, 0.25), (0.25, 0.50), (0.50, 0.75), (0.75, 1.0)]
        c = CalibrationChecker(bins=custom, state_file=tmp_path / "custom.json")
        assert c.bins == custom

    def test_reset_clears_bin_stats(self, checker):
        checker.record_prediction(0.85, True, 1.0)
        assert sum(s['count'] for s in checker.bin_stats.values()) > 0
        checker.reset()
        assert sum(s['count'] for s in checker.bin_stats.values()) == 0

    def test_reset_clears_tactical_stats(self, checker):
        checker.record_tactical_decision(0.85, "proceed", True)
        assert sum(s['count'] for s in checker.tactical_bin_stats.values()) > 0
        checker.reset()
        assert sum(s['count'] for s in checker.tactical_bin_stats.values()) == 0

    def test_reset_clears_complexity_stats(self, checker):
        checker.record_complexity_discrepancy(0.05)
        assert sum(s['count'] for s in checker.complexity_stats.values()) > 0
        checker.reset()
        assert sum(s['count'] for s in checker.complexity_stats.values()) == 0

    def test_complexity_bins_set(self, checker):
        assert len(checker.complexity_bins) == 4
        assert checker.complexity_bins[0] == (0.0, 0.1)


# ===========================================================================
# record_prediction -- strategic calibration
# ===========================================================================

class TestRecordPrediction:

    def test_single_prediction_count(self, checker_no_persist):
        checker_no_persist.record_prediction(0.85, True, None)
        stats = checker_no_persist.bin_stats["0.8-0.9"]
        assert stats['count'] == 1
        assert stats['predicted_correct'] == 1

    def test_actual_correct_float_signal(self, checker_no_persist):
        """actual_correct can be a weighted float, not just bool."""
        checker_no_persist.record_prediction(0.85, True, 0.7)
        stats = checker_no_persist.bin_stats["0.8-0.9"]
        assert stats['actual_correct'] == pytest.approx(0.7)

    def test_actual_correct_none_no_increment(self, checker_no_persist):
        checker_no_persist.record_prediction(0.85, True, None)
        stats = checker_no_persist.bin_stats["0.8-0.9"]
        assert stats['actual_correct'] == 0

    def test_confidence_sum_tracked(self, checker_no_persist):
        checker_no_persist.record_prediction(0.82, True, 1.0)
        checker_no_persist.record_prediction(0.88, True, 1.0)
        stats = checker_no_persist.bin_stats["0.8-0.9"]
        assert stats['confidence_sum'] == pytest.approx(1.70)

    def test_predicted_correct_false(self, checker_no_persist):
        checker_no_persist.record_prediction(0.85, False, 1.0)
        stats = checker_no_persist.bin_stats["0.8-0.9"]
        assert stats['predicted_correct'] == 0

    def test_bin_assignment_low_confidence(self, checker_no_persist):
        checker_no_persist.record_prediction(0.3, True, 1.0)
        assert checker_no_persist.bin_stats["0.0-0.5"]['count'] == 1

    def test_bin_assignment_medium_low(self, checker_no_persist):
        checker_no_persist.record_prediction(0.6, True, 1.0)
        assert checker_no_persist.bin_stats["0.5-0.7"]['count'] == 1

    def test_bin_assignment_medium_high(self, checker_no_persist):
        checker_no_persist.record_prediction(0.75, True, 1.0)
        assert checker_no_persist.bin_stats["0.7-0.8"]['count'] == 1

    def test_bin_assignment_high(self, checker_no_persist):
        checker_no_persist.record_prediction(0.85, True, 1.0)
        assert checker_no_persist.bin_stats["0.8-0.9"]['count'] == 1

    def test_bin_assignment_very_high(self, checker_no_persist):
        checker_no_persist.record_prediction(0.95, True, 1.0)
        assert checker_no_persist.bin_stats["0.9-1.0"]['count'] == 1

    def test_confidence_exactly_1(self, checker_no_persist):
        """1.0 should land in the 0.9-1.0 bin (special case)."""
        checker_no_persist.record_prediction(1.0, True, 1.0)
        assert checker_no_persist.bin_stats["0.9-1.0"]['count'] == 1

    def test_confidence_exactly_0(self, checker_no_persist):
        checker_no_persist.record_prediction(0.0, True, 1.0)
        assert checker_no_persist.bin_stats["0.0-0.5"]['count'] == 1

    def test_confidence_at_bin_boundary(self, checker_no_persist):
        """0.5 should go to 0.5-0.7, not 0.0-0.5 (left-inclusive)."""
        checker_no_persist.record_prediction(0.5, True, 1.0)
        assert checker_no_persist.bin_stats["0.5-0.7"]['count'] == 1
        assert checker_no_persist.bin_stats.get("0.0-0.5", {}).get('count', 0) == 0

    def test_complexity_discrepancy_forwarded(self, checker_no_persist):
        """When complexity_discrepancy is provided, it should be recorded."""
        checker_no_persist.record_prediction(0.85, True, 1.0, complexity_discrepancy=0.15)
        # Should have recorded in complexity_stats
        total = sum(s['count'] for s in checker_no_persist.complexity_stats.values())
        assert total == 1

    def test_many_predictions(self, checker_no_persist):
        for i in range(100):
            conf = 0.5 + (i / 200.0)  # 0.5 to ~1.0
            checker_no_persist.record_prediction(conf, True, 1.0)
        total = sum(s['count'] for s in checker_no_persist.bin_stats.values())
        assert total == 100


# ===========================================================================
# record_tactical_decision
# ===========================================================================

class TestRecordTacticalDecision:

    def test_basic_tactical(self, checker):
        checker.record_tactical_decision(0.85, "proceed", True)
        stats = checker.tactical_bin_stats["0.8-0.9"]
        assert stats['count'] == 1
        assert stats['actual_correct'] == 1

    def test_tactical_predicted_correct_threshold(self, checker):
        """confidence >= 0.5 means predicted_correct = True."""
        checker.record_tactical_decision(0.5, "proceed", True)
        stats = checker.tactical_bin_stats["0.5-0.7"]
        assert stats['predicted_correct'] == 1

    def test_tactical_predicted_incorrect_below_threshold(self, checker):
        """confidence < 0.5 means predicted_correct = False."""
        checker.record_tactical_decision(0.3, "proceed", True)
        stats = checker.tactical_bin_stats["0.0-0.5"]
        assert stats['predicted_correct'] == 0

    def test_tactical_immediate_outcome_false(self, checker):
        checker.record_tactical_decision(0.85, "pause", False)
        stats = checker.tactical_bin_stats["0.8-0.9"]
        assert stats['actual_correct'] == 0
        assert stats['count'] == 1

    def test_tactical_confidence_sum(self, checker):
        checker.record_tactical_decision(0.82, "proceed", True)
        checker.record_tactical_decision(0.88, "proceed", False)
        stats = checker.tactical_bin_stats["0.8-0.9"]
        assert stats['confidence_sum'] == pytest.approx(1.70)
        assert stats['count'] == 2

    def test_tactical_confidence_1_0(self, checker):
        checker.record_tactical_decision(1.0, "proceed", True)
        assert checker.tactical_bin_stats["0.9-1.0"]['count'] == 1


# ===========================================================================
# record_complexity_discrepancy
# ===========================================================================

class TestRecordComplexityDiscrepancy:

    def test_low_discrepancy_bin(self, checker_no_persist):
        checker_no_persist.record_complexity_discrepancy(0.05)
        assert checker_no_persist.complexity_stats["0.0-0.1"]['count'] == 1

    def test_medium_discrepancy_bin(self, checker_no_persist):
        checker_no_persist.record_complexity_discrepancy(0.2)
        assert checker_no_persist.complexity_stats["0.1-0.3"]['count'] == 1

    def test_high_discrepancy_bin(self, checker_no_persist):
        checker_no_persist.record_complexity_discrepancy(0.4)
        assert checker_no_persist.complexity_stats["0.3-0.5"]['count'] == 1

    def test_very_high_discrepancy_bin(self, checker_no_persist):
        checker_no_persist.record_complexity_discrepancy(0.7)
        assert checker_no_persist.complexity_stats["0.5-1.0"]['count'] == 1

    def test_discrepancy_exactly_1(self, checker_no_persist):
        checker_no_persist.record_complexity_discrepancy(1.0)
        assert checker_no_persist.complexity_stats["0.5-1.0"]['count'] == 1

    def test_high_discrepancy_count_threshold(self, checker_no_persist):
        """Discrepancy > 0.3 should increment high_discrepancy_count."""
        checker_no_persist.record_complexity_discrepancy(0.35)
        stats = checker_no_persist.complexity_stats["0.3-0.5"]
        assert stats['high_discrepancy_count'] == 1

    def test_low_discrepancy_no_high_count(self, checker_no_persist):
        checker_no_persist.record_complexity_discrepancy(0.05)
        stats = checker_no_persist.complexity_stats["0.0-0.1"]
        assert stats['high_discrepancy_count'] == 0

    def test_reported_and_derived_tracked(self, checker_no_persist):
        checker_no_persist.record_complexity_discrepancy(
            0.2, reported_complexity=0.6, derived_complexity=0.4
        )
        stats = checker_no_persist.complexity_stats["0.1-0.3"]
        assert stats['reported_sum'] == pytest.approx(0.6)
        assert stats['derived_sum'] == pytest.approx(0.4)

    def test_reported_none_not_added(self, checker_no_persist):
        checker_no_persist.record_complexity_discrepancy(0.2)
        stats = checker_no_persist.complexity_stats["0.1-0.3"]
        assert stats['reported_sum'] == 0.0


# ===========================================================================
# get_complexity_calibration_weight
# ===========================================================================

class TestComplexityCalibrationWeight:

    def test_none_returns_1(self, checker):
        assert checker.get_complexity_calibration_weight(None) == 1.0

    def test_zero_returns_1(self, checker):
        assert checker.get_complexity_calibration_weight(0.0) == 1.0

    def test_low_discrepancy(self, checker):
        w = checker.get_complexity_calibration_weight(0.05)
        assert w == 1.0

    def test_boundary_0_1(self, checker):
        """At exactly 0.1, should be the start of the medium range."""
        w = checker.get_complexity_calibration_weight(0.1)
        assert w == pytest.approx(1.0)

    def test_medium_discrepancy_interpolation(self, checker):
        """At 0.2 (midpoint of 0.1-0.3 range), weight should be ~0.85."""
        w = checker.get_complexity_calibration_weight(0.2)
        # Formula: 1.0 - (0.2 - 0.1) * 1.5 = 1.0 - 0.15 = 0.85
        assert w == pytest.approx(0.85)

    def test_boundary_0_3(self, checker):
        """At exactly 0.3, falls into high discrepancy branch -> 0.4."""
        w = checker.get_complexity_calibration_weight(0.3)
        # abs_discrepancy < 0.3 is False, so enters else branch:
        # 0.4 - (0.3 - 0.3) * (0.4 / 0.7) = 0.4
        assert w == pytest.approx(0.4)

    def test_high_discrepancy(self, checker):
        w = checker.get_complexity_calibration_weight(0.5)
        # Formula: 0.4 - (0.5 - 0.3) * (0.4 / 0.7) ≈ 0.4 - 0.114 ≈ 0.286
        assert 0.0 < w < 0.4

    def test_discrepancy_1_returns_0(self, checker):
        assert checker.get_complexity_calibration_weight(1.0) == 0.0

    def test_very_high_discrepancy(self, checker):
        w = checker.get_complexity_calibration_weight(0.9)
        assert w >= 0.0
        assert w < 0.1  # very low weight

    def test_negative_discrepancy_uses_abs(self, checker):
        """Negative values use abs()."""
        w_neg = checker.get_complexity_calibration_weight(-0.2)
        w_pos = checker.get_complexity_calibration_weight(0.2)
        assert w_neg == pytest.approx(w_pos)

    def test_monotonically_decreasing(self, checker):
        """Weight should decrease as discrepancy increases."""
        prev = 2.0
        for d in [0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.7, 1.0]:
            w = checker.get_complexity_calibration_weight(d)
            assert w <= prev, f"Weight increased at discrepancy={d}"
            prev = w


# ===========================================================================
# compute_calibration_metrics (strategic)
# ===========================================================================

class TestComputeCalibrationMetrics:

    def test_empty_returns_empty(self, checker):
        metrics = checker.compute_calibration_metrics()
        assert metrics == {}

    def test_single_bin(self, checker_no_persist):
        checker_no_persist.record_prediction(0.85, True, 1.0)
        metrics = checker_no_persist.compute_calibration_metrics()
        assert "0.8-0.9" in metrics
        b = metrics["0.8-0.9"]
        assert b.count == 1
        assert b.accuracy == pytest.approx(1.0)
        assert b.expected_accuracy == pytest.approx(0.85)
        assert b.calibration_error == pytest.approx(0.15)

    def test_perfect_calibration(self, checker_no_persist):
        """When accuracy matches expected, calibration_error should be 0."""
        # 10 predictions at 0.85 with exactly 85% correct
        for i in range(20):
            actual = 1.0 if i < 17 else 0.0  # 17/20 = 0.85
            checker_no_persist.record_prediction(0.85, True, actual)
        metrics = checker_no_persist.compute_calibration_metrics()
        b = metrics["0.8-0.9"]
        assert b.accuracy == pytest.approx(0.85)
        assert b.expected_accuracy == pytest.approx(0.85)
        assert b.calibration_error == pytest.approx(0.0, abs=0.01)

    def test_overconfident_high_error(self, checker_no_persist):
        """Agent claims 0.9 but only 50% correct -> large error."""
        for i in range(20):
            actual = 1.0 if i < 10 else 0.0  # 10/20 = 0.50
            checker_no_persist.record_prediction(0.9, True, actual)
        metrics = checker_no_persist.compute_calibration_metrics()
        b = metrics["0.9-1.0"]
        assert b.accuracy == pytest.approx(0.5)
        assert b.calibration_error == pytest.approx(0.4)

    def test_underconfident(self, checker_no_persist):
        """Agent claims 0.3 but actually 80% correct."""
        for i in range(20):
            actual = 1.0 if i < 16 else 0.0
            checker_no_persist.record_prediction(0.3, True, actual)
        metrics = checker_no_persist.compute_calibration_metrics()
        b = metrics["0.0-0.5"]
        assert b.accuracy == pytest.approx(0.8)
        assert b.expected_accuracy == pytest.approx(0.3)
        assert b.calibration_error == pytest.approx(0.5)

    def test_multiple_bins_independent(self, checker_no_persist):
        checker_no_persist.record_prediction(0.3, True, 1.0)
        checker_no_persist.record_prediction(0.6, True, 0.0)
        checker_no_persist.record_prediction(0.85, True, 1.0)
        metrics = checker_no_persist.compute_calibration_metrics()
        assert len(metrics) == 3
        assert "0.0-0.5" in metrics
        assert "0.5-0.7" in metrics
        assert "0.8-0.9" in metrics

    def test_bin_range_tuple(self, checker_no_persist):
        checker_no_persist.record_prediction(0.85, True, 1.0)
        metrics = checker_no_persist.compute_calibration_metrics()
        b = metrics["0.8-0.9"]
        assert b.bin_range == (0.8, 0.9)

    def test_weighted_actual_correct(self, checker_no_persist):
        """actual_correct can accumulate floats, not just integers."""
        checker_no_persist.record_prediction(0.85, True, 0.7)
        checker_no_persist.record_prediction(0.85, True, 0.3)
        metrics = checker_no_persist.compute_calibration_metrics()
        b = metrics["0.8-0.9"]
        # accuracy = (0.7 + 0.3) / 2 = 0.5
        assert b.accuracy == pytest.approx(0.5)

    def test_zero_count_bins_excluded(self, checker):
        """Bins with count=0 should not appear in metrics."""
        # Manually set a bin to 0 count
        checker.bin_stats["0.0-0.5"] = {
            'count': 0, 'predicted_correct': 0,
            'actual_correct': 0, 'confidence_sum': 0.0,
        }
        metrics = checker.compute_calibration_metrics()
        assert "0.0-0.5" not in metrics


# ===========================================================================
# compute_tactical_metrics
# ===========================================================================

class TestComputeTacticalMetrics:

    def test_empty_returns_empty(self, checker):
        metrics = checker.compute_tactical_metrics()
        assert metrics == {}

    def test_single_tactical_entry(self, checker):
        checker.record_tactical_decision(0.85, "proceed", True)
        metrics = checker.compute_tactical_metrics()
        assert "0.8-0.9" in metrics
        b = metrics["0.8-0.9"]
        assert b.count == 1
        assert b.accuracy == pytest.approx(1.0)

    def test_tactical_accuracy(self, checker):
        for i in range(10):
            checker.record_tactical_decision(0.85, "proceed", i < 7)
        metrics = checker.compute_tactical_metrics()
        b = metrics["0.8-0.9"]
        assert b.accuracy == pytest.approx(0.7)

    def test_backward_compat_no_tactical_attr(self, checker):
        """If tactical_bin_stats doesn't exist, should return empty."""
        del checker.tactical_bin_stats
        metrics = checker.compute_tactical_metrics()
        assert metrics == {}


# ===========================================================================
# compute_complexity_calibration_metrics
# ===========================================================================

class TestComputeComplexityCalibrationMetrics:

    def test_empty_returns_empty(self, checker):
        metrics = checker.compute_complexity_calibration_metrics()
        assert metrics == {}

    def test_single_entry(self, checker_no_persist):
        checker_no_persist.record_complexity_discrepancy(
            0.05, reported_complexity=0.5, derived_complexity=0.45
        )
        metrics = checker_no_persist.compute_complexity_calibration_metrics()
        assert "0.0-0.1" in metrics
        b = metrics["0.0-0.1"]
        assert b.count == 1
        assert b.mean_discrepancy == pytest.approx(0.05)
        assert b.mean_reported == pytest.approx(0.5)
        assert b.mean_derived == pytest.approx(0.45)
        assert b.high_discrepancy_rate == 0.0

    def test_high_discrepancy_rate(self, checker_no_persist):
        # 3 entries at discrepancy 0.4 (all > 0.3 threshold)
        for _ in range(3):
            checker_no_persist.record_complexity_discrepancy(0.4)
        metrics = checker_no_persist.compute_complexity_calibration_metrics()
        b = metrics["0.3-0.5"]
        assert b.high_discrepancy_rate == pytest.approx(1.0)

    def test_mixed_discrepancies(self, checker_no_persist):
        # 2 low + 3 high discrepancy in same complexity bin (0.3-0.5)
        checker_no_persist.record_complexity_discrepancy(0.31)  # > 0.3 threshold
        checker_no_persist.record_complexity_discrepancy(0.32)  # > 0.3 threshold
        checker_no_persist.record_complexity_discrepancy(0.45)  # > 0.3 threshold
        metrics = checker_no_persist.compute_complexity_calibration_metrics()
        b = metrics["0.3-0.5"]
        assert b.count == 3
        assert b.high_discrepancy_rate == pytest.approx(1.0)

    def test_no_reported_complexity(self, checker_no_persist):
        checker_no_persist.record_complexity_discrepancy(0.05)
        metrics = checker_no_persist.compute_complexity_calibration_metrics()
        b = metrics["0.0-0.1"]
        assert b.mean_reported == 0.0
        assert b.mean_derived == 0.0


# ===========================================================================
# check_calibration (two-dimensional)
# ===========================================================================

class TestCheckCalibration:

    def test_no_data_returns_not_calibrated(self, checker):
        is_cal, metrics = checker.check_calibration()
        assert is_cal is False
        assert "error" in metrics

    def test_well_calibrated_strategic(self, checker):
        """High confidence + high accuracy -> calibrated."""
        for _ in range(20):
            checker.update_ground_truth(0.85, True, True)
        is_cal, metrics = checker.check_calibration(min_samples_per_bin=5)
        assert is_cal is True
        assert len(metrics.get("issues", [])) == 0

    def test_miscalibrated_strategic(self, checker):
        """High confidence + low accuracy -> not calibrated."""
        for i in range(20):
            checker.update_ground_truth(0.85, True, i < 5)
        is_cal, metrics = checker.check_calibration(min_samples_per_bin=5)
        assert is_cal is False
        assert len(metrics.get("issues", [])) > 0

    def test_strategic_and_tactical_keys(self, checker):
        for _ in range(10):
            checker.update_ground_truth(0.85, True, True)
        _, metrics = checker.check_calibration(min_samples_per_bin=5)
        assert "strategic_calibration" in metrics
        assert "tactical_calibration" in metrics
        assert "bins" in metrics  # backward compat key
        assert "honesty_note" in metrics

    def test_tactical_note_when_no_data(self, checker):
        # Only strategic data
        for _ in range(10):
            checker.update_ground_truth(0.85, True, True)
        _, metrics = checker.check_calibration()
        tactical = metrics.get("tactical_calibration", {})
        assert "note" in tactical or "bins" in tactical

    def test_tactical_data_included(self, checker):
        for _ in range(15):
            checker.record_tactical_decision(0.85, "proceed", True)
        # Also need strategic data to avoid "No calibration data" error
        for _ in range(15):
            checker.update_ground_truth(0.85, True, True)
        _, metrics = checker.check_calibration(min_samples_per_bin=5)
        tactical = metrics.get("tactical_calibration", {})
        assert tactical.get("bins", {})

    def test_complexity_calibration_included(self, checker):
        for _ in range(10):
            checker.update_ground_truth(0.85, True, True)
        for _ in range(5):
            checker.record_complexity_discrepancy(0.05)
        _, metrics = checker.check_calibration(include_complexity=True)
        assert "complexity_calibration" in metrics

    def test_complexity_calibration_excluded(self, checker):
        for _ in range(10):
            checker.update_ground_truth(0.85, True, True)
        _, metrics = checker.check_calibration(include_complexity=False)
        assert "complexity_calibration" not in metrics

    def test_insufficient_samples_skipped_not_flagged(self, checker):
        """Bins with too few samples are skipped, not flagged as issues."""
        checker.update_ground_truth(0.85, True, True)
        is_cal, metrics = checker.check_calibration(min_samples_per_bin=10)
        issues = metrics.get("issues", [])
        # No real miscalibration issues — just insufficient data
        assert len(issues) == 0
        assert is_cal is True

    def test_large_calibration_error_flagged(self, checker):
        """Calibration error > 0.2 should be flagged."""
        # Confidence ~0.6 but all wrong -> accuracy=0, expected=0.6, error=0.6
        for _ in range(20):
            checker.update_ground_truth(0.6, True, False)
        is_cal, metrics = checker.check_calibration(min_samples_per_bin=5)
        assert is_cal is False
        issues = metrics.get("issues", [])
        assert any("calibration error" in i.lower() for i in issues)

    def test_backward_compat_bins_key(self, checker):
        for _ in range(10):
            checker.update_ground_truth(0.85, True, True)
        _, metrics = checker.check_calibration()
        bins = metrics.get("bins", {})
        if bins:
            for key, val in bins.items():
                assert "accuracy" in val
                assert "trajectory_health" in val

    def test_high_complexity_discrepancy_uncalibrates(self, checker):
        """Over 50% high complexity discrepancy should trigger uncalibrated."""
        for _ in range(20):
            checker.update_ground_truth(0.6, True, True)
        # Record all discrepancies as high
        for _ in range(20):
            checker.record_complexity_discrepancy(0.5)  # high discrepancy > 0.3
        is_cal, metrics = checker.check_calibration(
            min_samples_per_bin=5, include_complexity=True
        )
        # The high_discrepancy_rate is 100% which > 50%
        assert is_cal is False


# ===========================================================================
# update_ground_truth
# ===========================================================================

class TestUpdateGroundTruth:

    def test_increments_count(self, checker):
        checker.update_ground_truth(0.85, True, True)
        assert checker.bin_stats["0.8-0.9"]['count'] == 1

    def test_increments_actual_correct_on_true(self, checker):
        checker.update_ground_truth(0.85, True, True)
        assert checker.bin_stats["0.8-0.9"]['actual_correct'] == 1

    def test_no_increment_on_false(self, checker):
        checker.update_ground_truth(0.85, True, False)
        assert checker.bin_stats["0.8-0.9"]['actual_correct'] == 0

    def test_predicted_correct_tracked(self, checker):
        checker.update_ground_truth(0.85, True, True)
        checker.update_ground_truth(0.85, False, True)
        assert checker.bin_stats["0.8-0.9"]['predicted_correct'] == 1

    def test_confidence_sum(self, checker):
        checker.update_ground_truth(0.82, True, True)
        checker.update_ground_truth(0.88, True, True)
        assert checker.bin_stats["0.8-0.9"]['confidence_sum'] == pytest.approx(1.70)

    def test_actual_correct_capped_at_count(self, checker):
        """Safety: actual_correct should never exceed count."""
        stats = checker.bin_stats["0.8-0.9"]
        stats['count'] = 5
        stats['actual_correct'] = 10  # artificially set too high
        stats['confidence_sum'] = 4.25
        checker.update_ground_truth(0.85, True, True)
        # After update: count=6, actual_correct should be capped
        assert stats['actual_correct'] <= stats['count']

    def test_bin_fallback(self, checker):
        """Confidence outside all bins should use last bin."""
        checker.update_ground_truth(1.5, True, True)
        # Should fall into 0.9-1.0 via fallback
        total = sum(s['count'] for s in checker.bin_stats.values())
        assert total == 1


# ===========================================================================
# update_from_peer_verification
# ===========================================================================

class TestPeerVerification:

    def test_peer_agreed_weighted(self, checker):
        checker.update_from_peer_verification(0.85, True, True, weight=0.7)
        stats = checker.bin_stats["0.8-0.9"]
        assert stats['count'] == 1
        assert stats['actual_correct'] == pytest.approx(0.7)

    def test_peer_disagreed_no_actual(self, checker):
        checker.update_from_peer_verification(0.85, True, False, weight=0.7)
        stats = checker.bin_stats["0.8-0.9"]
        assert stats['count'] == 1
        assert stats['actual_correct'] == 0

    def test_custom_weight(self, checker):
        checker.update_from_peer_verification(0.85, True, True, weight=0.5)
        stats = checker.bin_stats["0.8-0.9"]
        assert stats['actual_correct'] == pytest.approx(0.5)

    def test_complexity_reduces_weight(self, checker):
        """High complexity discrepancy should reduce effective weight."""
        # weight=0.7, complexity_weight ~0.0 at discrepancy=1.0
        checker.update_from_peer_verification(
            0.85, True, True, weight=0.7,
            complexity_discrepancy=1.0
        )
        stats = checker.bin_stats["0.8-0.9"]
        assert stats['actual_correct'] == pytest.approx(0.0, abs=0.01)

    def test_low_complexity_preserves_weight(self, checker):
        """Low complexity discrepancy should not reduce weight."""
        checker.update_from_peer_verification(
            0.85, True, True, weight=0.7,
            complexity_discrepancy=0.05
        )
        stats = checker.bin_stats["0.8-0.9"]
        # complexity_weight = 1.0 at discrepancy 0.05
        assert stats['actual_correct'] == pytest.approx(0.7)

    def test_actual_correct_capped(self, checker):
        """After many high-weight verifications, actual_correct stays <= count."""
        for _ in range(5):
            checker.update_from_peer_verification(0.85, True, True, weight=1.0)
        stats = checker.bin_stats["0.8-0.9"]
        assert stats['actual_correct'] <= stats['count']

    def test_complexity_discrepancy_recorded(self, checker):
        checker.update_from_peer_verification(
            0.85, True, True, complexity_discrepancy=0.2
        )
        total = sum(s['count'] for s in checker.complexity_stats.values())
        assert total == 1


# ===========================================================================
# update_from_peer_disagreement
# ===========================================================================

class TestPeerDisagreement:

    def test_predicted_correct_with_full_severity(self, checker):
        """severity=1.0 => actual_correct += 0.0 (1.0 - 1.0)."""
        checker.update_from_peer_disagreement(0.85, True, 1.0)
        stats = checker.bin_stats["0.8-0.9"]
        assert stats['count'] == 1
        assert stats['actual_correct'] == pytest.approx(0.0)

    def test_predicted_correct_with_half_severity(self, checker):
        """severity=0.5 => actual_correct += 0.5."""
        checker.update_from_peer_disagreement(0.85, True, 0.5)
        stats = checker.bin_stats["0.8-0.9"]
        assert stats['actual_correct'] == pytest.approx(0.5)

    def test_predicted_correct_with_no_severity(self, checker):
        """severity=0.0 => actual_correct += 1.0."""
        checker.update_from_peer_disagreement(0.85, True, 0.0)
        stats = checker.bin_stats["0.8-0.9"]
        assert stats['actual_correct'] == pytest.approx(1.0)

    def test_predicted_incorrect_gets_small_credit(self, checker):
        """When predicted_correct=False, agent gets 0.3 credit for being cautious."""
        checker.update_from_peer_disagreement(0.85, False, 0.5)
        stats = checker.bin_stats["0.8-0.9"]
        assert stats['actual_correct'] == pytest.approx(0.3)

    def test_actual_correct_never_negative(self, checker):
        """Safety check: actual_correct should never go negative."""
        # Set artificially low
        checker.bin_stats["0.8-0.9"]['actual_correct'] = -5
        checker.bin_stats["0.8-0.9"]['count'] = 1
        checker.update_from_peer_disagreement(0.85, True, 1.0)
        # After fix: actual_correct should be >= 0
        assert checker.bin_stats["0.8-0.9"]['actual_correct'] >= 0

    def test_actual_correct_capped_at_count(self, checker):
        checker.update_from_peer_disagreement(0.85, True, 0.0)
        # actual_correct = 1.0, count = 1 -> ok
        assert checker.bin_stats["0.8-0.9"]['actual_correct'] <= checker.bin_stats["0.8-0.9"]['count']


# ===========================================================================
# compute_correction_factors
# ===========================================================================

class TestComputeCorrectionFactors:

    def test_empty_returns_empty(self, checker):
        assert checker.compute_correction_factors() == {}

    def test_insufficient_samples(self, checker):
        checker.tactical_bin_stats["0.8-0.9"] = {
            "count": 3, "actual_correct": 2, "predicted_correct": 3,
            "confidence_sum": 2.55,
        }
        assert checker.compute_correction_factors(min_samples=5) == {}

    def test_well_calibrated_near_one(self, checker):
        checker.tactical_bin_stats["0.8-0.9"] = {
            "count": 20, "actual_correct": 17, "predicted_correct": 20,
            "confidence_sum": 17.0,
        }
        factors = checker.compute_correction_factors(min_samples=5)
        assert "0.8-0.9" in factors
        assert factors["0.8-0.9"] == pytest.approx(1.0, abs=0.05)

    def test_overconfident_below_one(self, checker):
        # Avg confidence 0.85 but only 50% correct
        checker.tactical_bin_stats["0.8-0.9"] = {
            "count": 20, "actual_correct": 10, "predicted_correct": 20,
            "confidence_sum": 17.0,
        }
        factors = checker.compute_correction_factors(min_samples=5)
        assert factors["0.8-0.9"] < 1.0

    def test_underconfident_above_one(self, checker):
        checker.tactical_bin_stats["0.5-0.7"] = {
            "count": 20, "actual_correct": 16, "predicted_correct": 20,
            "confidence_sum": 11.0,
        }
        factors = checker.compute_correction_factors(min_samples=5)
        assert factors["0.5-0.7"] > 1.0

    def test_factor_clipped_lower(self, checker):
        checker.tactical_bin_stats["0.8-0.9"] = {
            "count": 20, "actual_correct": 1, "predicted_correct": 20,
            "confidence_sum": 17.0,
        }
        factors = checker.compute_correction_factors(min_samples=5)
        assert factors["0.8-0.9"] >= 0.5

    def test_factor_upper_reflects_underconfidence(self, checker):
        # Critique #3: a 0.0-0.5 bin running at ~1.0 accuracy is severely
        # underconfident (true factor ~4.0). The old symmetric clip hid this at
        # 1.5; the widened upside reports it honestly (bounded at 4.0).
        checker.tactical_bin_stats["0.0-0.5"] = {
            "count": 20, "actual_correct": 20, "predicted_correct": 20,
            "confidence_sum": 5.0,  # avg 0.25, actual 1.0 -> factor 4.0
        }
        factors = checker.compute_correction_factors(min_samples=5)
        assert factors["0.0-0.5"] == pytest.approx(4.0, abs=0.01)
        # And still bounded — never reports an unbounded factor.
        assert factors["0.0-0.5"] <= 4.0

    def test_near_zero_expected_skipped(self, checker):
        checker.tactical_bin_stats["0.0-0.5"] = {
            "count": 20, "actual_correct": 5, "predicted_correct": 0,
            "confidence_sum": 0.1,  # avg 0.005 -> below 0.01 threshold
        }
        factors = checker.compute_correction_factors(min_samples=5)
        assert "0.0-0.5" not in factors


# ===========================================================================
# apply_confidence_correction
# ===========================================================================

class TestApplyConfidenceCorrection:

    def test_no_data_unchanged(self, checker):
        corrected, info = checker.apply_confidence_correction(0.85)
        assert corrected == 0.85
        assert info is None

    def test_significant_correction(self, checker):
        checker.tactical_bin_stats["0.8-0.9"] = {
            "count": 20, "actual_correct": 10, "predicted_correct": 20,
            "confidence_sum": 17.0,
        }
        corrected, info = checker.apply_confidence_correction(0.85, min_samples=5)
        assert corrected < 0.85
        assert info is not None
        assert "calibration_adjusted" in info

    def test_small_correction_not_reported(self, checker):
        checker.tactical_bin_stats["0.8-0.9"] = {
            "count": 20, "actual_correct": 17, "predicted_correct": 20,
            "confidence_sum": 17.0,
        }
        corrected, info = checker.apply_confidence_correction(0.85, min_samples=5)
        # factor = 1.0, correction < 5%
        assert info is None

    def test_input_clamped_high(self, checker):
        corrected, _ = checker.apply_confidence_correction(1.5)
        assert corrected == 1.0

    def test_input_clamped_low(self, checker):
        corrected, _ = checker.apply_confidence_correction(-0.5)
        assert corrected == 0.0

    def test_output_clamped_to_01(self, checker):
        # Even with extreme correction, output stays in [0, 1]
        checker.tactical_bin_stats["0.8-0.9"] = {
            "count": 20, "actual_correct": 20, "predicted_correct": 20,
            "confidence_sum": 17.0,
        }
        corrected, _ = checker.apply_confidence_correction(0.99, min_samples=5)
        assert 0.0 <= corrected <= 1.0

    def test_confidence_exactly_1(self, checker):
        checker.tactical_bin_stats["0.9-1.0"] = {
            "count": 10, "actual_correct": 7, "predicted_correct": 10,
            "confidence_sum": 9.5,
        }
        corrected, info = checker.apply_confidence_correction(1.0, min_samples=5)
        assert 0.0 <= corrected <= 1.0

    def test_underconfident_lower_bin_corrected_up(self, checker, monkeypatch):
        # Critique #3 core: bin 0.0-0.5 reports ~0.25 confidence but is actually
        # ~1.0 accurate. The old 1.5 factor cap left a 0.25 report at 0.375; the
        # evidence-bounded fix lifts it toward the bin's measured accuracy.
        monkeypatch.setattr(checker, "_tactical_signal_age_days", lambda: 0.5)
        checker.tactical_bin_stats["0.0-0.5"] = {
            "count": 20, "actual_correct": 20, "predicted_correct": 20,
            "confidence_sum": 5.0,  # avg 0.25, actual 1.0
        }
        corrected, info = checker.apply_confidence_correction(0.25, min_samples=5)
        # 0.25 * 4.0 = 1.0, bounded by measured accuracy (1.0).
        assert corrected == pytest.approx(1.0, abs=1e-6)
        # Strictly better than the old 1.5-cap ceiling of 0.375.
        assert corrected > 0.375
        assert info is not None and "calibration_adjusted" in info

    def test_underconfidence_bounded_by_measured_accuracy(self, checker, monkeypatch):
        # The lift never exceeds the bin's measured accuracy (evidence ceiling),
        # even when reported * factor would overshoot it.
        monkeypatch.setattr(checker, "_tactical_signal_age_days", lambda: 0.5)
        checker.tactical_bin_stats["0.5-0.7"] = {
            "count": 20, "actual_correct": 18, "predicted_correct": 20,
            "confidence_sum": 11.0,  # avg 0.55, actual 0.90
        }
        # reported 0.69 (top of bin) * factor(0.90/0.55=1.636) = 1.13 -> capped at 0.90
        corrected, info = checker.apply_confidence_correction(0.69, min_samples=5)
        assert corrected == pytest.approx(0.90, abs=1e-6)

    def test_overconfident_regime_unchanged(self, checker, monkeypatch):
        # The overconfident path keeps the historical 0.5 floor behavior.
        monkeypatch.setattr(checker, "_tactical_signal_age_days", lambda: 0.5)
        checker.tactical_bin_stats["0.8-0.9"] = {
            "count": 20, "actual_correct": 2, "predicted_correct": 20,
            "confidence_sum": 17.0,  # avg 0.85, actual 0.10 -> raw factor 0.118, floored 0.5
        }
        corrected, info = checker.apply_confidence_correction(0.85, min_samples=5)
        assert corrected == pytest.approx(0.85 * 0.5, abs=1e-6)


# ===========================================================================
# save_state / load_state (JSON backend)
# ===========================================================================

class TestSaveLoadState:

    def test_round_trip_json(self, tmp_path):
        state_file = tmp_path / "rt.json"
        c = CalibrationChecker(state_file=state_file)
        c._backend = "json"
        # JSON snapshot always written (write-through cache)
        c.reset()

        c.record_prediction(0.85, True, 1.0)
        c.record_tactical_decision(0.6, "proceed", True)
        c.record_complexity_discrepancy(0.05)
        c.save_state()

        # Create a new checker and load from same file
        c2 = CalibrationChecker(state_file=state_file)
        c2._backend = "json"
        # JSON snapshot always written (write-through cache)
        c2.load_state()

        # Strategic bins
        assert c2.bin_stats["0.8-0.9"]['count'] == 1
        assert c2.bin_stats["0.8-0.9"]['actual_correct'] == pytest.approx(1.0)

        # Tactical bins
        assert c2.tactical_bin_stats["0.5-0.7"]['count'] == 1

        # Complexity bins
        assert c2.complexity_stats["0.0-0.1"]['count'] == 1

    def test_load_missing_file_resets(self, tmp_path):
        state_file = tmp_path / "nonexistent.json"
        c = CalibrationChecker(state_file=state_file)
        c._backend = "json"
        # JSON snapshot always written (write-through cache)
        c.load_state()
        # Should reset to empty
        total = sum(s['count'] for s in c.bin_stats.values())
        assert total == 0

    def test_load_corrupt_file_resets(self, tmp_path):
        state_file = tmp_path / "corrupt.json"
        state_file.write_text("not valid json {{{")
        c = CalibrationChecker(state_file=state_file)
        c._backend = "json"
        # JSON snapshot always written (write-through cache)
        c.load_state()
        total = sum(s['count'] for s in c.bin_stats.values())
        assert total == 0

    def test_load_old_state_without_tactical(self, tmp_path):
        """Old state files may not have tactical_bins key."""
        state_file = tmp_path / "old.json"
        state_file.write_text(json.dumps({
            "bins": {"0.8-0.9": {"count": 5, "predicted_correct": 3, "actual_correct": 3, "confidence_sum": 4.25}},
        }))
        c = CalibrationChecker(state_file=state_file)
        c._backend = "json"
        # JSON snapshot always written (write-through cache)
        c.load_state()
        assert c.bin_stats["0.8-0.9"]['count'] == 5
        # tactical should be empty defaultdict
        assert sum(s['count'] for s in c.tactical_bin_stats.values()) == 0


# ===========================================================================
# get_pending_updates (deprecated, always returns 0)
# ===========================================================================

class TestGetPendingUpdates:

    def test_always_returns_zero(self, checker):
        assert checker.get_pending_updates() == 0

    def test_returns_zero_after_predictions(self, checker):
        checker.record_prediction(0.85, True, 1.0)
        assert checker.get_pending_updates() == 0


# ===========================================================================
# _CalibrationCheckerProxy / get_calibration_checker
# ===========================================================================

class TestGlobalAccessors:

    def test_proxy_getattr(self, monkeypatch, tmp_path):
        """Proxy should delegate attribute access to the real checker."""
        state_file = tmp_path / "proxy.json"
        instance = CalibrationChecker(state_file=state_file)
        instance._backend = "json"
        # JSON snapshot always written (write-through cache)
        instance.reset()
        monkeypatch.setattr(
            "src.calibration._calibration_checker_instance", instance
        )
        proxy = _CalibrationCheckerProxy()
        assert proxy.bins == instance.bins

    def test_proxy_call(self, monkeypatch, tmp_path):
        """Calling the proxy should return the real checker."""
        state_file = tmp_path / "proxy_call.json"
        instance = CalibrationChecker(state_file=state_file)
        instance._backend = "json"
        monkeypatch.setattr(
            "src.calibration._calibration_checker_instance", instance
        )
        proxy = _CalibrationCheckerProxy()
        result = proxy()
        assert result is instance


# ===========================================================================
# Edge cases & mathematical properties
# ===========================================================================

class TestEdgeCases:

    def test_all_correct_brier_like(self, checker_no_persist):
        """100% accuracy with 100% confidence: error = 0."""
        for _ in range(50):
            checker_no_persist.record_prediction(1.0, True, 1.0)
        metrics = checker_no_persist.compute_calibration_metrics()
        b = metrics["0.9-1.0"]
        assert b.accuracy == pytest.approx(1.0)
        assert b.expected_accuracy == pytest.approx(1.0)
        assert b.calibration_error == pytest.approx(0.0)

    def test_all_wrong_brier_like(self, checker_no_persist):
        """0% accuracy with 100% confidence: error = 1.0."""
        for _ in range(50):
            checker_no_persist.record_prediction(1.0, True, 0.0)
        metrics = checker_no_persist.compute_calibration_metrics()
        b = metrics["0.9-1.0"]
        assert b.accuracy == pytest.approx(0.0)
        assert b.calibration_error == pytest.approx(1.0)

    def test_symmetry_of_error(self, checker_no_persist):
        """Calibration error is absolute: over- and under-confidence both positive."""
        for _ in range(20):
            checker_no_persist.record_prediction(0.3, True, 1.0)
        metrics = checker_no_persist.compute_calibration_metrics()
        b = metrics["0.0-0.5"]
        assert b.calibration_error > 0  # accuracy 1.0 vs expected 0.3

    def test_large_volume_stability(self, checker_no_persist):
        """With many samples, metrics should be stable and computable."""
        import random
        random.seed(42)
        for _ in range(1000):
            conf = random.uniform(0.0, 1.0)
            actual = 1.0 if random.random() < conf else 0.0
            checker_no_persist.record_prediction(conf, conf >= 0.5, actual)
        metrics = checker_no_persist.compute_calibration_metrics()
        assert len(metrics) > 0
        for key, b in metrics.items():
            assert 0.0 <= b.accuracy <= 1.0
            assert 0.0 <= b.expected_accuracy <= 1.0
            assert b.calibration_error >= 0.0

    def test_well_calibrated_random(self, checker_no_persist):
        """With perfectly calibrated random data, error should be small per bin."""
        import random
        random.seed(12345)
        for _ in range(5000):
            conf = random.uniform(0.0, 1.0)
            actual = 1.0 if random.random() < conf else 0.0
            checker_no_persist.record_prediction(conf, True, actual)
        metrics = checker_no_persist.compute_calibration_metrics()
        for key, b in metrics.items():
            # With 5000 samples, law of large numbers => each bin should
            # have accuracy close to expected_accuracy
            if b.count >= 50:
                assert b.calibration_error < 0.15, (
                    f"Bin {key}: error {b.calibration_error:.3f} too high "
                    f"(accuracy={b.accuracy:.3f}, expected={b.expected_accuracy:.3f})"
                )

    def test_confidence_boundary_values(self, checker_no_persist):
        """Test all bin boundary values."""
        boundaries = [0.0, 0.5, 0.7, 0.8, 0.9, 1.0]
        for conf in boundaries:
            checker_no_persist.record_prediction(conf, True, 1.0)
        total = sum(s['count'] for s in checker_no_persist.bin_stats.values())
        assert total == len(boundaries)

    def test_mixed_strategic_and_tactical(self, checker):
        """Strategic and tactical track independently."""
        checker.record_prediction(0.85, True, 1.0)
        checker.record_tactical_decision(0.85, "proceed", False)
        strategic = checker.compute_calibration_metrics()
        tactical = checker.compute_tactical_metrics()
        # Strategic: accuracy = 1.0
        assert strategic["0.8-0.9"].accuracy == pytest.approx(1.0)
        # Tactical: accuracy = 0.0
        assert tactical["0.8-0.9"].accuracy == pytest.approx(0.0)

    def test_fallback_bin_for_out_of_range(self, checker_no_persist):
        """Confidence > 1.0 should still record (last bin fallback)."""
        checker_no_persist.record_prediction(1.5, True, 1.0)
        total = sum(s['count'] for s in checker_no_persist.bin_stats.values())
        assert total == 1

    def test_correction_round_trip(self, checker):
        """Record tactical decisions then apply correction."""
        for i in range(20):
            checker.record_tactical_decision(0.85, "proceed", i < 14)
        corrected, info = checker.apply_confidence_correction(0.85, min_samples=5)
        assert 0.0 <= corrected <= 1.0
        # 14/20 = 0.7 accuracy, avg confidence 0.85 -> factor = 0.7/0.85 ~= 0.824
        assert corrected < 0.85


# ===========================================================================
# Integration: full pipeline
# ===========================================================================

class TestIntegrationPipeline:

    def test_full_lifecycle(self, tmp_path):
        """Test the complete lifecycle: create, record, check, correct, save, reload."""
        state_file = tmp_path / "lifecycle.json"
        c = CalibrationChecker(state_file=state_file)
        c._backend = "json"
        # JSON snapshot always written (write-through cache)
        c.reset()

        # Phase 1: Record strategic predictions
        for i in range(30):
            c.record_prediction(0.85, True, 1.0 if i < 25 else 0.0)

        # Phase 2: Record tactical decisions
        for i in range(30):
            c.record_tactical_decision(0.85, "proceed", i < 20)

        # Phase 3: Record complexity
        for i in range(10):
            c.record_complexity_discrepancy(0.05 + i * 0.05)

        # Phase 4: Peer verification
        c.update_from_peer_verification(0.75, True, True, weight=0.7)
        c.update_from_peer_verification(0.75, True, False, weight=0.7)

        # Phase 5: Peer disagreement
        c.update_from_peer_disagreement(0.95, True, 0.8)

        # Phase 6: Check calibration
        is_cal, metrics = c.check_calibration(min_samples_per_bin=5, include_complexity=True)
        assert isinstance(is_cal, bool)
        assert "strategic_calibration" in metrics
        assert "tactical_calibration" in metrics

        # Phase 7: Correction factors
        factors = c.compute_correction_factors(min_samples=5)
        assert isinstance(factors, dict)

        # Phase 8: Apply correction
        corrected, info = c.apply_confidence_correction(0.85, min_samples=5)
        assert 0.0 <= corrected <= 1.0

        # Phase 9: Save and reload
        c.save_state()
        c2 = CalibrationChecker(state_file=state_file)
        c2._backend = "json"
        # JSON snapshot always written (write-through cache)
        c2.load_state()

        # Verify reloaded state matches
        for bin_key in c.bin_stats:
            if c.bin_stats[bin_key]['count'] > 0:
                assert c2.bin_stats[bin_key]['count'] == c.bin_stats[bin_key]['count']

    def test_overconfidence_detection_pipeline(self, checker):
        """Simulate a genuinely overconfident agent and verify detection."""
        # Agent reports 0.95 confidence but is only correct 40% of the time
        import random
        random.seed(99)
        for _ in range(50):
            actual = random.random() < 0.4
            checker.update_ground_truth(0.95, True, actual)

        is_cal, metrics = checker.check_calibration(min_samples_per_bin=10)
        assert is_cal is False
        issues = metrics.get("issues", [])
        # Should flag either low trajectory health or large calibration error
        assert len(issues) > 0
