"""
UNITARES Research Tools

Monte Carlo stability checking and gradient-based theta optimization.
These are research/analysis utilities, not core dynamics.

Migrated from src/unitaires-server/unitaires_core.py during cleanup.
"""

from __future__ import annotations
from dataclasses import asdict
from typing import Dict

from .dynamics import State, DynamicsParams, step_state
from .parameters import Theta, Weights, DEFAULT_PARAMS, DEFAULT_WEIGHTS
from .scoring import phi_objective
from .utils import clip


def approximate_stability_check(
    theta: Theta,
    params: DynamicsParams = DEFAULT_PARAMS,
    samples: int = 200,
    steps_per_sample: int = 20,
    dt: float = 0.05,
) -> Dict:
    """
    Stability check via Lyapunov eigenvalue analysis at equilibrium.

    Uses rigorous mathematical verification instead of Monte Carlo sampling.
    The samples and steps_per_sample parameters are kept for backward
    compatibility but are not used.

    Returns dict with 'stable', 'alpha_estimate', 'violations', 'notes'.
    """
    from .stability import verify_lyapunov_stability
    from .dynamics import compute_equilibrium

    eq = compute_equilibrium(params, theta)
    result = verify_lyapunov_stability(eq, params, theta, complexity=0.5)

    return {
        "stable": result["stable"],
        "alpha_estimate": result["contraction_rate"],
        "violations": 0 if result["stable"] else 1,
        "notes": (
            f"Lyapunov eigenvalue analysis at equilibrium. "
            f"Max eigenvalue: {result['max_eigenvalue']:.6f}, "
            f"contraction rate: {result['contraction_rate']:.6f}"
        ),
    }


def _project_theta(theta: Theta, params: DynamicsParams = DEFAULT_PARAMS) -> Theta:
    """Project theta to valid parameter bounds."""
    return Theta(
        C1=clip(theta.C1, params.C1_min, params.C1_max),
        eta1=clip(theta.eta1, params.eta1_min, params.eta1_max),
        eta2=theta.eta2,
    )


def suggest_theta_update(
    theta: Theta,
    state: State,
    horizon: float,
    step: float,
    params: DynamicsParams = DEFAULT_PARAMS,
    weights: Weights = DEFAULT_WEIGHTS,
) -> Dict:
    """
    Suggest theta update via antithetic finite-difference gradient estimation.

    Simulates forward from `state` under perturbed theta values and returns
    the gradient direction that improves the Phi objective.
    """

    def simulate_with_theta(theta_local: Theta) -> float:
        s = State(**asdict(state))
        T = max(horizon, step)
        dt = min(0.05, T / 20.0)
        t = 0.0
        phis = []
        while t < T:
            delta_eta = [0.1, 0.0, 0.0]
            s = step_state(s, theta_local, delta_eta, dt=dt, params=params)
            phis.append(phi_objective(s, delta_eta, weights))
            t += dt
        return sum(phis) / max(1, len(phis))

    theta_p = Theta(C1=theta.C1 + step, eta1=theta.eta1, eta2=theta.eta2)
    theta_m = Theta(C1=theta.C1 - step, eta1=theta.eta1, eta2=theta.eta2)
    f_p, f_m = simulate_with_theta(theta_p), simulate_with_theta(theta_m)
    grad_C1 = (f_p - f_m) / (2.0 * step)

    theta_p = Theta(C1=theta.C1, eta1=theta.eta1 + step, eta2=theta.eta2)
    theta_m = Theta(C1=theta.C1, eta1=theta.eta1 - step, eta2=theta.eta2)
    f_p, f_m = simulate_with_theta(theta_p), simulate_with_theta(theta_m)
    grad_eta1 = (f_p - f_m) / (2.0 * step)

    eps = 0.1
    theta_new = Theta(
        C1=theta.C1 + eps * grad_C1,
        eta1=theta.eta1 + eps * grad_eta1,
        eta2=theta.eta2,
    )
    theta_new = _project_theta(theta_new, params)
    rationale = (
        f"θ updated via antithetic finite differences on Φ over "
        f"horizon={horizon}. dΦ/dC1={grad_C1:.4f}, dΦ/deta1={grad_eta1:.4f}."
    )
    return {
        "theta_new": asdict(theta_new),
        "gradient": [grad_C1, grad_eta1],
        "rationale": rationale,
    }
