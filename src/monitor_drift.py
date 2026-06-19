"""Ethical drift vector computation for governance monitor."""

from typing import Dict, Optional, Tuple

from governance_core import (
    coherence, compute_ethical_drift, get_agent_baseline,
    EthicalDriftVector,
)
from governance_core.parameters import get_active_params
from src.calibration import calibration_checker
from src.logging_utils import get_logger

logger = get_logger(__name__)

# Minimum tactical samples before the calibration signal is trusted for drift.
_MIN_TACTICAL_SAMPLES = 10


def compute_calibration_error(checker) -> Optional[float]:
    """Current confidence-outcome mismatch from the TACTICAL calibration channel.

    Returns the worst populated tactical bin's overconfidence — declared
    confidence minus real success rate — clamped to [0, 1]. The worst bin, not a
    sample-weighted mean, because over- and under-confident bins otherwise
    cancel: a system miscalibrated in both directions would read ~0 and hide the
    problem (the same masking the gate avoids by scoring per bin). Underconfidence
    contributes zero drift; overconfidence is the safety-relevant direction
    (consistent with `src/calibration.py::check_calibration`).

    Returns None when no bin has enough evidence yet, so the drift engine falls
    back to its baseline estimate rather than asserting a false zero.

    This replaces a long-dead call to a non-existent `calibration_checker.check()`
    (its AttributeError was swallowed, so the drift vector never carried a real
    calibration signal) and the mis-scaled `trajectory_health / 100.0` fallback
    that treated a 0-1 value as a percentage. The strategic `trajectory_health`
    proxy is deliberately not used here — it is saturated (near-constant across
    confidence levels) and was demoted to advisory in the gate.
    """
    tactical = checker.compute_tactical_metrics()
    populated = [b for b in tactical.values() if b.count >= _MIN_TACTICAL_SAMPLES]
    if not populated:
        return None
    worst_overconfidence = max(b.expected_accuracy - b.accuracy for b in populated)
    return max(0.0, min(1.0, worst_overconfidence))


def compute_drift_vector(
    monitor,
    grounded_agent_state: Dict,
    agent_state: Dict,
    confidence,
    task_type: str,
    continuity_metrics,
) -> Tuple[EthicalDriftVector, float]:
    """Compute concrete ethical drift vector from measurable signals.

    Blends governance-computed drift with agent-reported drift. Mutates
    grounded_agent_state['ethical_drift'] with the final drift list.

    Sets monitor._last_drift_vector, monitor._consecutive_high_drift.

    Returns (drift_vector, agent_drift_norm).
    """
    agent_baseline = get_agent_baseline(monitor.agent_id)

    # Get calibration error (tactical confidence-outcome mismatch) if available.
    calibration_error = None
    try:
        calibration_error = compute_calibration_error(calibration_checker)
    except Exception as exc:
        # Degrade gracefully — compute_ethical_drift falls back to a baseline
        # estimate when this is None — but log rather than silently swallow, so a
        # real future break is visible (the prior bare `except: pass` is exactly
        # how the dead `.check()` call stayed invisible).
        logger.warning(f"calibration_error unavailable for drift vector: {exc}")

    # Get current coherence for deviation calculation
    active_params = get_active_params()
    current_coherence = coherence(monitor.state.V, monitor.state.unitaires_theta, active_params)

    # Compute concrete drift vector from governance-observed signals
    drift_vector = compute_ethical_drift(
        agent_id=monitor.agent_id,
        baseline=agent_baseline,
        current_coherence=current_coherence,
        current_confidence=confidence if confidence is not None else 0.6,
        complexity_divergence=continuity_metrics.complexity_divergence,
        calibration_error=calibration_error,
        decision=None,
        state_velocity=monitor._last_state_velocity,
        task_context=task_type,
    )

    # Blend agent-sent drift with governance-computed drift.
    agent_drift_raw = agent_state.get('ethical_drift', [0.0, 0.0, 0.0])
    if isinstance(agent_drift_raw, (list, tuple)) and len(agent_drift_raw) >= 1:
        agent_drift_norm = sum(d * d for d in agent_drift_raw) ** 0.5
    else:
        agent_drift_norm = 0.0

    if agent_drift_norm > 0.01:
        ad = list(agent_drift_raw) + [0.0] * max(0, 3 - len(agent_drift_raw))
        blend = 0.3
        drift_vector.calibration_deviation = (
            (1 - blend) * drift_vector.calibration_deviation + blend * min(1.0, abs(ad[0]))
        )
        drift_vector.coherence_deviation = (
            (1 - blend) * drift_vector.coherence_deviation + blend * min(1.0, abs(ad[1]))
        )
        drift_vector.stability_deviation = (
            (1 - blend) * drift_vector.stability_deviation + blend * min(1.0, abs(ad[2]))
        )

    # Store for later access and time-series logging
    monitor._last_drift_vector = drift_vector

    # Track consecutive high-drift updates for auto-dialectic trigger
    drift_dialectic_threshold = 0.7
    if drift_vector.norm > drift_dialectic_threshold:
        monitor._consecutive_high_drift = getattr(monitor, '_consecutive_high_drift', 0) + 1
    else:
        monitor._consecutive_high_drift = 0

    # Convert to list format for dynamics engine (all 4 components)
    drift_vector_list = drift_vector.to_list()

    # Prevent drift signal from vanishing on complex tasks
    complexity = grounded_agent_state.get('complexity', 0.0)
    drift_norm_sq = sum(d ** 2 for d in drift_vector_list)
    if drift_norm_sq < 0.001 and complexity > 0.3:
        min_component = 0.05 * complexity / max(1, len(drift_vector_list))
        drift_vector_list = [max(d, min_component) for d in drift_vector_list]

    grounded_agent_state['ethical_drift'] = drift_vector_list

    if drift_vector.norm > 0.3:
        logger.info(
            f"Ethical drift for {monitor.agent_id}: "
            f"||Δη||={drift_vector.norm:.3f} "
            f"[cal={drift_vector.calibration_deviation:.3f}, "
            f"cpx={drift_vector.complexity_divergence:.3f}, "
            f"coh={drift_vector.coherence_deviation:.3f}, "
            f"stab={drift_vector.stability_deviation:.3f}]"
            f"{' (blended with agent signal)' if agent_drift_norm > 0.01 else ''}"
        )

    return drift_vector, agent_drift_norm
