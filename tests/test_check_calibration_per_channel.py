"""check_calibration must include per_channel_calibration when channels exist."""

import pytest
from src.calibration import CalibrationChecker


@pytest.fixture
def checker(tmp_path):
    return CalibrationChecker(state_file=tmp_path / "calibration_state.json")


class TestCheckCalibrationPerChannel:
    def test_response_contains_per_channel_key_when_channels_exist(self, checker):
        # Seed enough samples to clear min_samples_per_bin
        for _ in range(15):
            checker.record_tactical_decision(0.8, "proceed", True, signal_source="tasks")
        for _ in range(15):
            checker.record_tactical_decision(0.9, "proceed", True, signal_source="tests")

        is_calibrated, result = checker.check_calibration()
        assert "per_channel_calibration" in result
        assert "tasks" in result["per_channel_calibration"]
        assert "tests" in result["per_channel_calibration"]

    def test_per_channel_entry_has_required_fields(self, checker):
        for _ in range(15):
            checker.record_tactical_decision(0.8, "proceed", True, signal_source="tasks")

        _, result = checker.check_calibration()
        tasks_entry = result["per_channel_calibration"]["tasks"]
        assert "calibrated" in tasks_entry
        assert tasks_entry["calibration_status"] == "calibrated"
        assert tasks_entry["assessable"] is True
        assert "samples" in tasks_entry
        assert "calibration_gap" in tasks_entry
        assert "issues" in tasks_entry
        assert isinstance(tasks_entry["calibrated"], bool)
        assert tasks_entry["samples"] == 15

    def test_response_omits_per_channel_when_no_channels_recorded(self, checker):
        # Aggregate-only path (legacy): no per-channel data.
        for _ in range(15):
            checker.record_tactical_decision(0.8, "proceed", True)
        _, result = checker.check_calibration()
        # Either omit the key or surface empty dict; back-compat is "no key".
        assert result.get("per_channel_calibration", {}) == {}

    def test_aggregate_calibrated_field_unchanged_with_per_channel_data(self, checker):
        # Adding per-channel state must not change aggregate Yes/No semantics.
        for _ in range(15):
            checker.record_tactical_decision(0.8, "proceed", True, signal_source="tasks")
        _, result = checker.check_calibration()
        assert "is_calibrated" in result
        assert isinstance(result["is_calibrated"], bool)

    def test_underfilled_channel_is_explicitly_unassessed(self, checker):
        checker.record_tactical_decision(
            0.8, "proceed", True, signal_source="tasks"
        )

        _, result = checker.check_calibration(min_samples_per_bin=10)

        tasks_entry = result["per_channel_calibration"]["tasks"]
        assert tasks_entry["calibrated"] is True  # legacy vacuous boolean
        assert tasks_entry["calibration_status"] == "unassessed"
        assert tasks_entry["assessable"] is False
