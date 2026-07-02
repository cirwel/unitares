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
    intervention improved the trajectory. Records a trajectory-based tactical
    calibration signal only; the tool-usage feeder was removed (#1321).

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
        # `improvement` is the trajectory-quality quantity: positive = drift
        # dropped since the previous check-in (the intervention helped).
        improvement = monitor._prev_drift_norm - current_norm
        # `norm_delta` is the plain delta exposed in the payload and must follow
        # the math convention current - prev (positive = norm rose). Keeping the
        # two distinct prevents the field name from contradicting its sign (F3).
        norm_delta = current_norm - monitor._prev_drift_norm

        # Convert improvement to [0, 1] quality signal via sigmoid
        trajectory_quality = 1.0 / (1.0 + math.exp(-improvement * 10.0))

        if (monitor._prev_verdict_action in ('proceed', 'pause')
                and abs(improvement) > 0.03):
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
            'improvement': improvement,
        }

    # Store current verdict for next check-in's validation
    monitor._prev_verdict_action = decision['action']
    monitor._prev_drift_norm = current_norm
    monitor._prev_confidence = confidence
    monitor._prev_checkin_time = now_mono

    # Mint a tactical prediction id
    monitor.register_tactical_prediction(confidence, decision_action=decision.get('action'))

    # The former tool-usage feeder is deliberately gone (#1321). It graded
    # reported confidence against the agent's MCP tool-invocation success rate
    # over the last hour — a ~0.998 near-constant that measures infrastructure
    # reliability, not task outcomes. Firing on every check-in, it supplied
    # ~96% of the strategic/tactical bin mass, manufactured the fleet-wide
    # −0.29 "underconfidence" artifact, and (via the inverted low-confidence
    # scoring it used) taught the corrector to halve honest 0.5–0.7 reports.
    # Calibration ground truth must come from outcome-graded events
    # (evidence_weight-gated in outcome_events.py) — never from tool-call
    # plumbing succeeding.

    return trajectory_validation
