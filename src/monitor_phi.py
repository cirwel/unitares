"""Phi objective and risk computation with task-type adjustments."""

from typing import Dict, Optional, Tuple

from governance_core import phi_objective, verdict_from_phi, DEFAULT_WEIGHTS


def compute_phi_and_risk(
    monitor,
    grounded_agent_state: Dict,
    agent_state: Dict,
    task_type: str,
) -> Tuple[float, str, float, Optional[Dict], float]:
    """Compute phi objective, UNITARES verdict, risk score with task-type adjustments.

    Returns (phi, unitares_verdict, risk_score, task_type_adjustment, original_risk_score).
    """
    delta_eta = grounded_agent_state.get('ethical_drift', [0.0, 0.0, 0.0, 0.0])
    if not delta_eta:
        delta_eta = [0.0, 0.0, 0.0, 0.0]

    # Stage A coupling: when UNITARES_S_SETPOINT is on, the ODE rests at the
    # measured-healthy S (≈0.2) not S*≈0.091. Φ penalizes S against zero, so we
    # detrend by the SAME per-class σ — Φ then measures entropy above the healthy
    # rest, keeping verdict/risk invariant under the attractor move. Off → no-op.
    from src.monitor_setpoint import phi_eval_state
    phi = phi_objective(
        state=phi_eval_state(monitor, monitor.state.unitaires_state),
        delta_eta=delta_eta,
        weights=DEFAULT_WEIGHTS
    )
    unitares_verdict = verdict_from_phi(phi)
    score_result = {'phi': phi, 'verdict': unitares_verdict}

    risk_score = monitor.estimate_risk(agent_state, score_result=score_result)

    # Adjust decision based on task_type context
    if task_type == "mixed":
        task_type = agent_state.get("task_type", "mixed")
    task_type_adjustment = None
    original_risk_score = risk_score

    if task_type == "convergent" and monitor.state.S == 0.0:
        if risk_score > 0.3:
            risk_score = max(0.2, risk_score * 0.8)
            task_type_adjustment = {
                "applied": True,
                "reason": "Convergent task with S=0 (healthy standardization)",
                "original_risk": original_risk_score,
                "adjusted_risk": risk_score,
                "adjustment": "reduced"
            }
    elif task_type == "divergent" and monitor.state.S == 0.0:
        if risk_score < 0.4:
            risk_score = min(0.5, risk_score * 1.15)
            task_type_adjustment = {
                "applied": True,
                "reason": "Divergent task with S=0 (may indicate lack of divergence)",
                "original_risk": original_risk_score,
                "adjusted_risk": risk_score,
                "adjustment": "increased"
            }
    elif task_type in ("exploration", "introspection") and risk_score > 0.5:
        risk_adjustment = -0.08
        risk_score = max(0.45, risk_score + risk_adjustment)
        task_type_adjustment = {
            "applied": True,
            "reason": f"{task_type} task: low confidence is appropriate epistemic state",
            "original_risk": original_risk_score,
            "adjusted_risk": risk_score,
            "adjustment": "reduced",
            "risk_adjusted_by": risk_adjustment,
        }

    return phi, unitares_verdict, risk_score, task_type_adjustment, original_risk_score
