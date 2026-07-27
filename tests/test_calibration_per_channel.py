"""Per-channel tactical calibration breakdown.

Tests that record_tactical_decision routes to a per-channel structure when
signal_source is provided, and that the aggregate path is preserved.
"""

import json
import pytest
from src.calibration import CalibrationChecker


@pytest.fixture
def checker(tmp_path):
    return CalibrationChecker(state_file=tmp_path / "calibration_state.json")


class TestPerChannelTacticalStats:
    def test_record_with_signal_source_populates_channel_dict(self, checker):
        checker.record_tactical_decision(
            confidence=0.8, decision="proceed", immediate_outcome=True,
            signal_source="tasks",
        )
        channel_stats = checker.tactical_bin_stats_by_channel["tasks"]
        # Bin 0.7-0.8 captures confidence=0.8
        assert any(stats["count"] == 1 for stats in channel_stats.values())

    def test_record_without_signal_source_leaves_per_channel_empty(self, checker):
        checker.record_tactical_decision(
            confidence=0.8, decision="proceed", immediate_outcome=True,
        )
        # Aggregate gets the row
        assert any(s["count"] == 1 for s in checker.tactical_bin_stats.values())
        # Per-channel does not
        assert sum(
            s["count"] for ch in checker.tactical_bin_stats_by_channel.values()
            for s in ch.values()
        ) == 0

    def test_record_with_signal_source_also_populates_aggregate(self, checker):
        # Back-compat: aggregate must remain populated when signal_source is given.
        checker.record_tactical_decision(
            confidence=0.8, decision="proceed", immediate_outcome=True,
            signal_source="tasks",
        )
        assert sum(s["count"] for s in checker.tactical_bin_stats.values()) == 1

    def test_compute_per_channel_returns_per_bin_breakdown(self, checker):
        for _ in range(5):
            checker.record_tactical_decision(0.8, "proceed", True, signal_source="tasks")
        for _ in range(3):
            checker.record_tactical_decision(0.3, "pause", False, signal_source="tasks")
        for _ in range(4):
            checker.record_tactical_decision(0.9, "proceed", True, signal_source="tests")

        per_channel = checker.compute_tactical_metrics_per_channel()
        assert "tasks" in per_channel and "tests" in per_channel
        assert sum(b.count for b in per_channel["tasks"].values()) == 8
        assert sum(b.count for b in per_channel["tests"].values()) == 4

    def test_per_channel_state_round_trips_through_persistence(self, checker, tmp_path):
        checker.record_tactical_decision(0.8, "proceed", True, signal_source="tasks")
        checker.save_state()

        reloaded = CalibrationChecker(state_file=tmp_path / "calibration_state.json")
        per_channel = reloaded.compute_tactical_metrics_per_channel()
        assert sum(b.count for b in per_channel["tasks"].values()) == 1

    def test_unknown_state_key_load_does_not_crash(self, tmp_path):
        # Existing state files lack tactical_bins_by_channel; loading must
        # not raise and per-channel state defaults to empty.
        state_file = tmp_path / "calibration_state.json"
        state_file.write_text(json.dumps({"tactical_bins": {}, "bins": {}}))
        checker = CalibrationChecker(state_file=state_file)
        per_channel = checker.compute_tactical_metrics_per_channel()
        assert per_channel == {}


class TestAggregateGate:
    """#1321 semantics unification: the aggregate tactical bins are reserved
    for hard-exogenous outcome-graded rows; self-relative feeders opt out."""

    def test_include_in_aggregate_false_routes_channel_only(self, checker):
        checker.record_tactical_decision(
            confidence=0.8, decision="proceed", immediate_outcome=True,
            signal_source="trajectory", include_in_aggregate=False,
        )
        assert sum(s["count"] for s in checker.tactical_bin_stats.values()) == 0
        channel = checker.tactical_bin_stats_by_channel["trajectory"]
        assert any(s["count"] == 1 for s in channel.values())

    def test_channel_row_still_scores_prediction_fields(self, checker):
        checker.record_tactical_decision(
            confidence=0.8, decision="proceed", immediate_outcome=True,
            signal_source="trajectory", include_in_aggregate=False,
        )
        # 0.8 lands in the half-open 0.8-0.9 bin (bin_min <= c < bin_max)
        stats = checker.tactical_bin_stats_by_channel["trajectory"]["0.8-0.9"]
        assert stats["predicted_correct"] == 1
        assert stats["actual_correct"] == 1

    def test_default_include_in_aggregate_preserves_existing_behavior(self, checker):
        checker.record_tactical_decision(
            confidence=0.8, decision="proceed", immediate_outcome=True,
            signal_source="tests",
        )
        assert sum(s["count"] for s in checker.tactical_bin_stats.values()) == 1
        assert any(
            s["count"] == 1
            for s in checker.tactical_bin_stats_by_channel["tests"].values()
        )
