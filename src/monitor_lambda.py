"""PI controller for lambda1 adaptation in governance monitor."""

import numpy as np

from config.governance_config import config
from governance_core import Theta, DEFAULT_THETA
from src.logging_utils import get_logger
from src.hck_reflexive import modulate_gains
from src.monitor_void import calculate_void_frequency

logger = get_logger(__name__)


def update_lambda1(state) -> float:
    """
    Update lambda1 using PI controller based on void frequency and coherence targets.

    Uses PI controller to adapt lambda1 to maintain:
    - Target void frequency: 2% (TARGET_VOID_FREQ)
    - Target coherence: 55% (TARGET_COHERENCE)

    HCK v3.0: When update coherence rho(t) is low, PI gains are modulated
    to reduce controller aggressiveness and prevent instability.

    Updates state.unitaires_theta to reflect new lambda1.

    Args:
        state: GovernanceState instance.

    Returns:
        Updated lambda1 value.
    """
    from config.governance_config import GovernanceConfig as GovConfig

    void_freq_current = calculate_void_frequency(state)
    lambda1_current = state.lambda1

    # When behavioral verdict is active, use rho-derived coherence as PI target
    # rho ∈ [-1,1] → coherence ∈ [0,1]
    if GovConfig.BEHAVIORAL_VERDICT_ENABLED:
        rho_val = getattr(state, 'current_rho', 0.0)
        coherence_current = (rho_val + 1.0) / 2.0
    else:
        coherence_current = state.coherence

    if not hasattr(state, 'pi_integral'):
        state.pi_integral = 0.0

    # HCK v3.0: Modulate PI gains based on update coherence rho(t)
    rho = getattr(state, 'current_rho', 0.0)
    base_K_p = config.PI_KP
    base_K_i = config.PI_KI
    K_p_adj, K_i_adj = modulate_gains(base_K_p, base_K_i, rho)

    gains_modulated = (K_p_adj != base_K_p or K_i_adj != base_K_i)
    # Surface the modulation so the monitor/result can report it honestly.
    # Previously this flag stayed local and `_gains_modulated` was always False
    # even when ρ(t) drove a real gain reduction — the HCK signal was decoupled
    # from its own reported effect (F4).
    state.gains_modulated = gains_modulated

    # PI calculation with modulated gains
    error_void = config.TARGET_VOID_FREQ - void_freq_current
    error_coherence = coherence_current - config.TARGET_COHERENCE

    # Proportional term (weighted combination)
    P = K_p_adj * (0.7 * error_void + 0.3 * error_coherence)

    # Integral term (only void frequency, with anti-windup)
    state.pi_integral += error_void * 1.0
    state.pi_integral = np.clip(
        state.pi_integral,
        -config.PI_INTEGRAL_MAX,
        config.PI_INTEGRAL_MAX,
    )
    I = K_i_adj * state.pi_integral

    delta_lambda = P + I

    new_lambda1 = lambda1_current + delta_lambda
    new_lambda1 = np.clip(new_lambda1, config.LAMBDA1_MIN, config.LAMBDA1_MAX)

    # Map new lambda1 back to theta.eta1
    lambda1_range = config.LAMBDA1_MAX - config.LAMBDA1_MIN
    eta1_min = 0.1
    eta1_max = 0.5
    eta1_range = eta1_max - eta1_min

    if lambda1_range > 0:
        normalized_lambda1 = (new_lambda1 - config.LAMBDA1_MIN) / lambda1_range
        new_eta1 = eta1_min + normalized_lambda1 * eta1_range
        new_eta1 = np.clip(new_eta1, eta1_min, eta1_max)
    else:
        new_eta1 = state.unitaires_theta.eta1

    state.unitaires_theta = Theta(
        C1=DEFAULT_THETA.C1,
        eta1=new_eta1,
    )

    updated_lambda1 = state.lambda1

    if abs(updated_lambda1 - lambda1_current) > 0.01:
        gain_info = ""
        if gains_modulated:
            gain_info = f", rho={rho:.3f}, gains_modulated=True"
        logger.info(
            f"PI Controller lambda1 update: {lambda1_current:.4f} -> {updated_lambda1:.4f} "
            f"(void_freq={void_freq_current:.3f}, coherence={coherence_current:.3f}, "
            f"eta1->{new_eta1:.3f}{gain_info})"
        )

    return updated_lambda1
