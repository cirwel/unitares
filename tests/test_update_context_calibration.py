"""Observability contract for calibration used by behavioral EISV."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.calibration import CalibrationBin, CalibrationChecker
from src.eisv_telemetry import build_behavioral_derivation, build_eisv_telemetry_envelope
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
    checker.compute_agent_tactical_calibration_channel.assert_called_once_with("agent-a")


def test_agent_tactical_calibration_channel_reports_null_when_no_pairs():
    """#2818 §3.0: n_verified_pairs == 0 reports value=None and default_reason='no_verified_agent_pairs'."""
    checker = CalibrationChecker()
    channel = checker.compute_agent_tactical_calibration_channel("agent-empty")
    assert channel["schema"] == "agent_tactical_calibration_channel.v1"
    assert channel["mode"] == "measurement_only"
    assert channel["policy_applied"] is False
    assert channel["value"] is None
    assert channel["calibration_error"] is None
    assert channel["coverage"] == "none"
    assert channel["n_verified_pairs"] == 0
    assert channel["evidence_status"] == "unavailable"
    assert channel["default_reason"] == "no_verified_agent_pairs"


def test_agent_tactical_calibration_channel_has_no_five_sample_bin_cliff():
    """#2818 §3.0/§3.1: 1-4 tactical pairs produce sample-weighted ECE instead of being dropped."""
    checker = CalibrationChecker()
    now = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)
    with patch.object(checker, "save_state"):
        checker.record_tactical_decision(
            0.9, "proceed", True, signal_source="tests",
            include_in_aggregate=True, agent_id="agent-1", observed_at=now,
        )
        checker.record_tactical_decision(
            0.9, "proceed", False, signal_source="tests",
            include_in_aggregate=True, agent_id="agent-1", observed_at=now,
        )
        # Excluded self-relative row must not enter tactical_bins_by_agent
        checker.record_tactical_decision(
            0.2, "proceed", True, signal_source="drift",
            include_in_aggregate=False, agent_id="agent-1", observed_at=now,
        )

    channel = checker.compute_agent_tactical_calibration_channel("agent-1", now=now)
    assert channel["evidence_status"] == "available"
    assert channel["default_reason"] is None
    assert channel["n_verified_pairs"] == 2
    assert channel["coverage"] == "low"
    # Bin 0.9-1.0: expected=0.9, actual=0.5 -> gap=0.4 -> value=0.6
    assert channel["calibration_error"] == pytest.approx(0.4)
    assert channel["value"] == pytest.approx(0.6)
    assert channel["freshness_status"] == "recent"

    derivation = build_behavioral_derivation(
        decision_history=["proceed"] * 4,
        coherence_history=[0.5] * 4,
        regime_history=["CONVERGENCE"] * 4,
        E_history=[0.7] * 4,
        I_history=[0.8] * 4,
        calibration_signal={"agent_channel": channel},
    )
    envelope = build_eisv_telemetry_envelope(
        metrics={"E": 0.7, "I": 0.8, "S": 0.2, "V": 0.0, "primary_eisv_source": "behavioral"},
        behavioral_snapshot=None,
        submitted_sensor=None,
        submitted_source=None,
        derivation=derivation,
        policy_evaluation={},
        enforcement={},
    )
    assert envelope["measurement"]["agent_calibration"]["value"] == pytest.approx(0.6)
    assert envelope["measurement"]["agent_calibration"]["n_verified_pairs"] == 2


def test_agent_channel_exception_reports_estimator_unavailable_and_preserves_fleet_deployed():
    """When compute_agent_tactical_calibration_channel raises, report estimator_unavailable (not no_verified_agent_pairs) and preserve fleet deployed."""
    ctx = UpdateContext(agent_id="agent-err")
    candidate = {"scope": "agent", "agent_id": "agent-err", "evidence_status": "available"}
    with patch("src.calibration.calibration_checker") as checker:
        checker.compute_calibration_metrics.return_value = {
            "0.8-0.9": _bin(count=8, error=0.2),
        }
        checker.compute_agent_calibration_candidate.return_value = candidate
        checker.compute_agent_tactical_calibration_channel.side_effect = RuntimeError("boom")

        assert get_mean_calibration_error(ctx) == 0.2

    assert ctx._calibration_signal["deployed"]["calibration_error"] == 0.2
    assert ctx._calibration_signal["agent_candidate"] == candidate
    assert ctx._calibration_signal["agent_channel"] == {
        "scope": "agent",
        "agent_id": "agent-err",
        "value": None,
        "calibration_error": None,
        "n_verified_pairs": None,
        "evidence_status": "unavailable",
        "default_reason": "estimator_unavailable",
    }
