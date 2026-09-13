"""Observability contract for calibration used by behavioral EISV."""

from unittest.mock import patch

from src.calibration import CalibrationBin
from src.mcp_handlers.updates.context import (
    UpdateContext,
    get_mean_calibration_error,
)


def _bin(*, count: int, error: float) -> CalibrationBin:
    return CalibrationBin(
        bin_range=(0.8, 0.9),
        count=count,
        predicted_correct=count,
        actual_correct=count,
        accuracy=1.0,
        expected_accuracy=1.0 - error,
        calibration_error=error,
    )


def test_deployed_value_stays_fleet_scoped_while_agent_candidate_is_shadowed():
    ctx = UpdateContext(agent_id="agent-a")
    candidate = {
        "schema": "agent_calibration_candidate.v1",
        "mode": "measurement_only",
        "policy_applied": False,
        "scope": "agent",
        "agent_id": "agent-a",
        "evidence_status": "available",
        "calibration_error": 0.1,
    }

    with patch("src.calibration.calibration_checker") as checker:
        checker.compute_calibration_metrics.return_value = {
            "0.8-0.9": _bin(count=8, error=0.2),
            "0.5-0.7": _bin(count=2, error=0.9),
        }
        checker.compute_agent_calibration_candidate.return_value = candidate

        assert get_mean_calibration_error(ctx) == 0.2

    assert ctx._calibration_signal["policy_applied"] is False
    assert ctx._calibration_signal["deployed"] == {
        "scope": "fleet",
        "estimator": "mean_absolute_strategic_bin_error",
        "evidence_channel": "strategic_mixed_proxy",
        "calibration_error": 0.2,
        "sample_count": 10,
        "eligible_sample_count": 8,
        "eligible_bin_count": 1,
        "minimum_samples_per_bin": 5,
        "sample_window": "lifetime",
        "freshness_status": "unknown",
        "freshness_reason": "legacy_fleet_bins_have_no_timestamps",
    }
    assert ctx._calibration_signal["agent_candidate"] == candidate


def test_calibration_signal_and_value_are_computed_once_per_update():
    ctx = UpdateContext(agent_id="agent-a")
    with patch("src.calibration.calibration_checker") as checker:
        checker.compute_calibration_metrics.return_value = {
            "0.8-0.9": _bin(count=8, error=0.2),
        }
        checker.compute_agent_calibration_candidate.return_value = {}

        assert get_mean_calibration_error(ctx) == 0.2
        assert get_mean_calibration_error(ctx) == 0.2

    checker.compute_calibration_metrics.assert_called_once_with()
    checker.compute_agent_calibration_candidate.assert_called_once_with("agent-a")
