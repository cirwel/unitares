"""Risk estimation for governance monitor."""

from typing import Dict, Optional

import numpy as np

from config.governance_config import config
from governance_core import phi_objective, verdict_from_phi, DEFAULT_WEIGHTS


def estimate_risk(state, agent_state: Dict, score_result: Optional[Dict] = None,
                   behavioral_risk: Optional[float] = None) -> float:
    """
    Estimate risk score using governance_core phi_objective and verdict_from_phi.

    **Risk Score Composition:**
    - 70% UNITARES phi-based risk (includes ethical drift, E, I, S, V state)
    - 30% Traditional safety risk (length, complexity, coherence, keywords)

    When behavioral_risk is provided and BEHAVIORAL_VERDICT_ENABLED, uses that
    as primary risk signal instead of phi-based.

    Args:
        state: GovernanceState instance.
        agent_state: Agent state dictionary.
        score_result: Optional pre-computed score_result to avoid recomputation.
        behavioral_risk: Optional behavioral assessment risk [0, 1].

    Returns:
        Risk score in [0, 1].
    """
    from config.governance_config import GovernanceConfig as GovConfig
    if GovConfig.BEHAVIORAL_VERDICT_ENABLED and behavioral_risk is not None:
        # Behavioral risk is primary, but still add velocity component
        risk = behavioral_risk
        velocity_risk = 0.0
        if len(state.E_history) >= 3:
            diffs = [
                abs(h[-1] - h[-2])
                for h in [state.E_history, state.I_history, state.S_history, state.V_history]
                if len(h) >= 2
            ]
            velocity_magnitude = sum(diffs)
            velocity_risk = min(0.15, velocity_magnitude * 2.0)
        risk += velocity_risk
        risk = float(np.clip(risk, 0.0, 1.0))
        state.risk_history.append(risk)
        if len(state.risk_history) > config.HISTORY_WINDOW:
            state.risk_history = state.risk_history[-config.HISTORY_WINDOW:]
        return risk
    if score_result is None:
        ethical_signals = np.array(agent_state.get('ethical_drift', [0.0, 0.0, 0.0, 0.0]))
        if len(ethical_signals) == 0:
            delta_eta = [0.0, 0.0, 0.0]
        else:
            delta_eta = ethical_signals.tolist()

        phi = phi_objective(
            state=state.unitaires_state,
            delta_eta=delta_eta,
            weights=DEFAULT_WEIGHTS,
        )
        verdict = verdict_from_phi(phi)
        score_result = {'phi': phi, 'verdict': verdict}

    phi = score_result['phi']

    phi_safe_threshold = getattr(config, 'PHI_SAFE_THRESHOLD', 0.3)
    phi_caution_threshold = getattr(config, 'PHI_CAUTION_THRESHOLD', 0.0)

    if phi >= phi_safe_threshold:
        risk = max(0.0, 0.3 - (phi - phi_safe_threshold) * 0.5)
    elif phi >= phi_caution_threshold:
        range_size = phi_safe_threshold - phi_caution_threshold
        if range_size > 0:
            risk = 0.3 + (phi_safe_threshold - phi) / range_size * 0.4
        else:
            risk = 0.5
    else:
        risk = min(1.0, 0.7 + abs(phi - phi_caution_threshold) * 2.0)

    # Blend with traditional risk
    response_text = agent_state.get('response_text', '')
    traditional_risk = config.estimate_risk(
        response_text,
        complexity=0.5,
        coherence=state.coherence,
    )

    phi_weight = getattr(config, 'RISK_PHI_WEIGHT', 1.0)
    traditional_weight = getattr(config, 'RISK_TRADITIONAL_WEIGHT', 0.0)
    risk = phi_weight * risk + traditional_weight * traditional_risk

    # Velocity-based risk
    velocity_risk = 0.0
    if len(state.E_history) >= 3:
        diffs = [
            abs(h[-1] - h[-2])
            for h in [state.E_history, state.I_history, state.S_history, state.V_history]
            if len(h) >= 2
        ]
        velocity_magnitude = sum(diffs)
        velocity_risk = min(0.15, velocity_magnitude * 2.0)
    risk += velocity_risk

    # The public return contract is [0, 1]. Persist that same value so
    # latest/smoothed history consumers cannot observe a different range.
    risk = float(np.clip(risk, 0.0, 1.0))
    state.risk_history.append(risk)
    if len(state.risk_history) > config.HISTORY_WINDOW:
        state.risk_history = state.risk_history[-config.HISTORY_WINDOW:]

    return risk
