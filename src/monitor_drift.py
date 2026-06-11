"""Ethical drift vector computation for governance monitor."""

from typing import Dict, Tuple

from governance_core import (
    coherence, compute_ethical_drift, get_agent_baseline,
    EthicalDriftVector,
)
from governance_core.parameters import get_active_params
from src.calibration import calibration_checker
from src.logging_utils import get_logger

logger = get_logger(__name__)


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

    # Get calibration error from calibration system if available
    calibration_error = None
    try:
        cal_status = calibration_checker.check()
        if cal_status.get('calibrated') and cal_status.get('total_samples', 0) > 10:
            trajectory_health = cal_status.get('trajectory_health', 0.5)
            if trajectory_health is not None:
                calibration_error = abs((trajectory_health / 100.0) - 0.5) * 2
    except Exception:
        pass

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
