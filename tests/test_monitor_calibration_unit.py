"""Unit tests for monitor_calibration.run_calibration_recording.

Scope: trajectory validation gating (elapsed time + prev verdict), state mutation,
and strategic/tactical calibration recording branches (src/monitor_calibration.py
lines 40-103).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.monitor_calibration import run_calibration_recording


def _make_monitor(
    *,
    prev_verdict=None,
    prev_drift_norm=None,
    prev_confidence=0.5,
    prev_checkin_time=None,
    drift_norm=0.1,
    agent_id="agent-test",
):
    monitor = MagicMock()
    monitor._prev_verdict_action = prev_verdict
    monitor._prev_drift_norm = prev_drift_norm
    monitor._prev_confidence = prev_confidence
    monitor._prev_checkin_time = prev_checkin_time
    monitor._last_drift_vector = SimpleNamespace(norm=drift_norm)
    monitor.agent_id = agent_id
    monitor.register_tactical_prediction = MagicMock()
    return monitor


def _drift(norm=0.1):
    return SimpleNamespace(norm=norm)


class TestTrajectoryValidationGating:
    def test_no_prev_verdict_returns_none(self):
        monitor = _make_monitor(prev_verdict=None, prev_drift_norm=None)
        with patch("src.monitor_calibration.calibration_checker"), \
             patch("src.tool_usage_tracker.get_tool_usage_tracker") as g:
            g.side_effect = Exception("no tracker")
            result = run_calibration_recording(
                monitor, confidence=0.7,
                decision={"action": "proceed"},
                drift_vector=_drift(0.1),
            )
        assert result is None

    def test_elapsed_under_10s_returns_none(self):
        import time
        monitor = _make_monitor(
            prev_verdict="proceed", prev_drift_norm=0.2,
            prev_checkin_time=time.monotonic() - 5.0,  # only 5s ago
        )
        with patch("src.monitor_calibration.calibration_checker"), \
             patch("src.tool_usage_tracker.get_tool_usage_tracker") as g:
            g.side_effect = Exception()
            result = run_calibration_recording(
                monitor, confidence=0.7,
                decision={"action": "proceed"},
                drift_vector=_drift(0.1),
            )
        assert result is None

    def test_elapsed_over_10s_returns_validation(self):
        import time
        monitor = _make_monitor(
            prev_verdict="proceed", prev_drift_norm=0.3,
            prev_checkin_time=time.monotonic() - 15.0,
        )
        with patch("src.monitor_calibration.calibration_checker"), \
             patch("src.tool_usage_tracker.get_tool_usage_tracker") as g:
            g.side_effect = Exception()
            result = run_calibration_recording(
                monitor, confidence=0.7,
                decision={"action": "proceed"},
                drift_vector=_drift(0.1),
            )
        assert result is not None
        assert result["prev_verdict"] == "proceed"
        assert result["prev_norm"] == 0.3
        assert result["current_norm"] == 0.1
        # norm_delta follows the plain math convention current - prev; the norm
        # dropped 0.3 -> 0.1, so the delta is negative (F3).
        assert result["norm_delta"] == pytest.approx(-0.2)
        # improvement is the trajectory-quality quantity (positive = drift dropped).
        assert result["improvement"] == pytest.approx(0.2)
        assert 0.0 <= result["quality"] <= 1.0
        assert result["quality"] > 0.5  # improvement → quality above 0.5


class TestTacticalDecisionRecording:
    def test_large_positive_delta_records_success(self):
        import time
        monitor = _make_monitor(
            prev_verdict="proceed", prev_drift_norm=0.5,
            prev_confidence=0.8,
            prev_checkin_time=time.monotonic() - 15.0,
        )
        with patch("src.monitor_calibration.calibration_checker") as ccm, \
             patch("src.tool_usage_tracker.get_tool_usage_tracker") as g:
            g.side_effect = Exception()
            run_calibration_recording(
                monitor, confidence=0.7,
                decision={"action": "proceed"},
                drift_vector=_drift(0.1),
            )
        # Tactical decision recorded with immediate_outcome=True
        ccm.record_tactical_decision.assert_called_once()
        call = ccm.record_tactical_decision.call_args
        assert call.kwargs["confidence"] == 0.8
        assert call.kwargs["decision"] == "proceed"
        assert call.kwargs["immediate_outcome"] is True

    def test_small_delta_no_tactical_recording(self):
        import time
        monitor = _make_monitor(
            prev_verdict="proceed", prev_drift_norm=0.11,
            prev_checkin_time=time.monotonic() - 15.0,
        )
        with patch("src.monitor_calibration.calibration_checker") as ccm, \
             patch("src.tool_usage_tracker.get_tool_usage_tracker") as g:
            g.side_effect = Exception()
            run_calibration_recording(
                monitor, confidence=0.7,
                decision={"action": "proceed"},
                drift_vector=_drift(0.10),
            )
        # abs(norm_delta)=0.01 < 0.03 threshold → no tactical record
        ccm.record_tactical_decision.assert_not_called()

    def test_guide_verdict_skips_tactical_record(self):
        import time
        monitor = _make_monitor(
            prev_verdict="guide", prev_drift_norm=0.5,
            prev_checkin_time=time.monotonic() - 15.0,
        )
        with patch("src.monitor_calibration.calibration_checker") as ccm, \
             patch("src.tool_usage_tracker.get_tool_usage_tracker") as g:
            g.side_effect = Exception()
            run_calibration_recording(
                monitor, confidence=0.7,
                decision={"action": "guide"},
                drift_vector=_drift(0.1),
            )
        # prev_verdict "guide" not in ('proceed', 'pause') → tactical from prev skipped
        # current decision "guide" also not in ('proceed', 'pause')
        ccm.record_tactical_decision.assert_not_called()


class TestStrategicPrediction:
    def test_tool_stats_with_enough_calls_records_prediction(self):
        import time
        monitor = _make_monitor(
            prev_verdict=None, prev_checkin_time=time.monotonic(),
        )
        fake_stats = {
            "total_calls": 5,
            "tools": {"t1": {"success_count": 4}, "t2": {"success_count": 0}},
        }
        tracker = MagicMock()
        tracker.get_usage_stats = MagicMock(return_value=fake_stats)

        with patch("src.monitor_calibration.calibration_checker") as ccm, \
             patch("src.tool_usage_tracker.get_tool_usage_tracker",
                   return_value=tracker):
            run_calibration_recording(
                monitor, confidence=0.75,
                decision={"action": "proceed"},
                drift_vector=_drift(0.1),
            )
        ccm.record_prediction.assert_called_once()
        call = ccm.record_prediction.call_args
        assert call.kwargs["confidence"] == 0.75
        assert call.kwargs["predicted_correct"] is True
        assert call.kwargs["actual_correct"] == pytest.approx(0.8)
        # tool_accuracy 0.8 >= 0.6 → outcome_was_good True
        # confidence 0.75 >= 0.6 → immediate_outcome = True
        ccm.record_tactical_decision.assert_called_once()
        assert ccm.record_tactical_decision.call_args.kwargs["immediate_outcome"] is True

    def test_low_confidence_inverts_immediate_outcome(self):
        import time
        monitor = _make_monitor(
            prev_verdict=None, prev_checkin_time=time.monotonic(),
        )
        fake_stats = {
            "total_calls": 5,
            "tools": {"t1": {"success_count": 1}},
        }
        tracker = MagicMock()
        tracker.get_usage_stats = MagicMock(return_value=fake_stats)
        with patch("src.monitor_calibration.calibration_checker") as ccm, \
             patch("src.tool_usage_tracker.get_tool_usage_tracker",
                   return_value=tracker):
            run_calibration_recording(
                monitor, confidence=0.3,
                decision={"action": "proceed"},
                drift_vector=_drift(0.1),
            )
        # tool_accuracy 0.2 < 0.6 → outcome_was_good False
        # confidence 0.3 < 0.6 → immediate_outcome = not outcome_was_good = True
        assert ccm.record_tactical_decision.call_args.kwargs["immediate_outcome"] is True

    def test_too_few_calls_no_prediction(self):
        import time
        monitor = _make_monitor(
            prev_verdict=None, prev_checkin_time=time.monotonic(),
        )
        tracker = MagicMock()
        tracker.get_usage_stats = MagicMock(return_value={"total_calls": 2, "tools": {}})
        with patch("src.monitor_calibration.calibration_checker") as ccm, \
             patch("src.tool_usage_tracker.get_tool_usage_tracker",
                   return_value=tracker):
            run_calibration_recording(
                monitor, confidence=0.7,
                decision={"action": "proceed"},
                drift_vector=_drift(0.1),
            )
        ccm.record_prediction.assert_not_called()

    def test_tracker_exception_silently_skips_prediction(self):
        import time
        monitor = _make_monitor(
            prev_verdict=None, prev_checkin_time=time.monotonic(),
        )
        with patch("src.monitor_calibration.calibration_checker") as ccm, \
             patch("src.tool_usage_tracker.get_tool_usage_tracker") as g:
            g.side_effect = RuntimeError("boom")
            # Should not raise
            run_calibration_recording(
                monitor, confidence=0.7,
                decision={"action": "proceed"},
                drift_vector=_drift(0.1),
            )
        ccm.record_prediction.assert_not_called()


class TestStateMutation:
    def test_mutates_prev_state_after_recording(self):
        import time
        monitor = _make_monitor(
            prev_verdict="proceed", prev_drift_norm=0.5,
            prev_confidence=0.2,
            prev_checkin_time=time.monotonic() - 20.0,
        )
        before = monitor._prev_checkin_time
        with patch("src.monitor_calibration.calibration_checker"), \
             patch("src.tool_usage_tracker.get_tool_usage_tracker") as g:
            g.side_effect = Exception()
            run_calibration_recording(
                monitor, confidence=0.9,
                decision={"action": "pause"},
                drift_vector=_drift(0.07),
            )
        assert monitor._prev_verdict_action == "pause"
        assert monitor._prev_drift_norm == 0.07
        assert monitor._prev_confidence == 0.9
        assert monitor._prev_checkin_time > before

    def test_registers_tactical_prediction(self):
        import time
        monitor = _make_monitor(
            prev_verdict=None, prev_checkin_time=time.monotonic(),
        )
        with patch("src.monitor_calibration.calibration_checker"), \
             patch("src.tool_usage_tracker.get_tool_usage_tracker") as g:
            g.side_effect = Exception()
            run_calibration_recording(
                monitor, confidence=0.65,
                decision={"action": "proceed"},
                drift_vector=_drift(0.1),
            )
        monitor.register_tactical_prediction.assert_called_once_with(
            0.65, decision_action="proceed"
        )
