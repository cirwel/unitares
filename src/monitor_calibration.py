"""Calibration recording for governance monitor.

Retrospective trajectory validation + strategic/tactical calibration.
"""

import math
import time as _time
from typing import Dict, Optional

from src.calibration import calibration_checker
from src.logging_utils import get_logger

logger = get_logger(__name__)


def run_calibration_recording(monitor, confidence: float, decision: Dict, drift_vector) -> Optional[Dict]:
    """Retrospective trajectory validation + strategic/tactical calibration.

    Compares previous verdict to current drift norm to assess whether the
    intervention improved the trajectory.  Records calibration signals for
    both trajectory-based and tool-usage ground truth.

    Mutates monitor._prev_verdict_action, monitor._prev_drift_norm,
    monitor._prev_confidence, monitor._prev_checkin_time.

    Returns trajectory_validation dict or None.
    """
    current_norm = drift_vector.norm if monitor._last_drift_vector else 0.0
    trajectory_validation = None

    now_mono = _time.monotonic()
    elapsed_since_prev = (now_mono - monitor._prev_checkin_time) if monitor._prev_checkin_time else float('inf')

    # Only record trajectory-based calibration when enough time has elapsed
    # (>10s) to prevent rapid-fire calibration pollution from burst check-ins
    if (monitor._prev_verdict_action is not None
            and monitor._prev_drift_norm is not None
            and elapsed_since_prev > 10.0):
        norm_delta = monitor._prev_drift_norm - current_norm  # positive = improved

        # Convert to [0, 1] quality signal via sigmoid
        trajectory_quality = 1.0 / (1.0 + math.exp(-norm_delta * 10.0))

        if (monitor._prev_verdict_action in ('proceed', 'pause')
                and abs(norm_delta) > 0.03):
            calibration_checker.record_tactical_decision(
                confidence=monitor._prev_confidence,
                decision=monitor._prev_verdict_action,
                immediate_outcome=(trajectory_quality > 0.5),
            )

        trajectory_validation = {
            'quality': trajectory_quality,
            'prev_verdict': monitor._prev_verdict_action,
            'prev_norm': monitor._prev_drift_norm,
            'current_norm': current_norm,
            'norm_delta': norm_delta,
        }

    # Store current verdict for next check-in's validation
    monitor._prev_verdict_action = decision['action']
    monitor._prev_drift_norm = current_norm
    monitor._prev_confidence = confidence
    monitor._prev_checkin_time = now_mono

    # Mint a tactical prediction id
    monitor.register_tactical_prediction(confidence, decision_action=decision.get('action'))

    # Record prediction for STRATEGIC calibration
    predicted_correct = confidence >= 0.5
    actual_correct = None

    try:
        from src.tool_usage_tracker import get_tool_usage_tracker
        tracker = get_tool_usage_tracker()
        stats = tracker.get_usage_stats(window_hours=1, agent_id=monitor.agent_id)
        total_calls = stats.get('total_calls', 0)
        if total_calls >= 3:
            tools = stats.get('tools', {})
            total_success = sum(t.get('success_count', 0) for t in tools.values())
            tool_accuracy = float(total_success) / float(total_calls)
            actual_correct = tool_accuracy
    except Exception:
        pass

    if actual_correct is not None:
        calibration_checker.record_prediction(
            confidence=confidence,
            predicted_correct=predicted_correct,
            actual_correct=actual_correct
        )

        decision_action = decision['action']
        outcome_was_good = actual_correct >= 0.6

        if confidence >= 0.6:
            immediate_outcome = outcome_was_good
        else:
            immediate_outcome = not outcome_was_good

        if decision_action in ('proceed', 'pause'):
            calibration_checker.record_tactical_decision(
                confidence=confidence,
                decision=decision_action,
                immediate_outcome=immediate_outcome
            )

    return trajectory_validation
