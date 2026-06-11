"""
Formal Lyapunov Stability Verification for EISV Dynamics

Provides rigorous eigenvalue-based stability analysis of the EISV system
at any operating point. Replaces Monte Carlo sampling with mathematical
verification via Jacobian eigenvalue analysis, Gershgorin bounds, and
optional metric optimization.

Public API:
    compute_jacobian()           - 4x4 Jacobian at a state
    verify_lyapunov_stability()  - full stability check with contraction rate
    gershgorin_stability_bound() - conservative eigenvalue bound
    sweep_stability()            - parameter robustness sweep
    optimize_stability_metric()  - find best diagonal metric
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

import numpy as np

from .dynamics import State, DynamicsParams, compute_equilibrium, _derivatives
from .parameters import (
    Theta,
    DEFAULT_THETA,
    get_active_params,
    get_i_dynamics_mode,
)
from .coherence import lambda2 as _lambda2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _state_to_vec(state: State) -> np.ndarray:
    return np.array([state.E, state.I, state.S, state.V])


def _vec_to_state(vec: np.ndarray) -> State:
    return State(E=float(vec[0]), I=float(vec[1]),
                 S=float(vec[2]), V=float(vec[3]))


def _compute_rhs(
    state: State,
    params: DynamicsParams,
    theta: Theta,
    complexity: float = 0.5,
) -> np.ndarray:
    """Evaluate the ODE right-hand side F(x) at zero drift."""
    derivs = _derivatives(state, 0.0, theta, params, 0.0, complexity, None)
    return np.array(derivs)


# ---------------------------------------------------------------------------
# Jacobian computation
# ---------------------------------------------------------------------------

def compute_jacobian(
    state: State,
    params: DynamicsParams = None,
    theta: Theta = None,
    complexity: float = 0.5,
    method: str = "analytical",
) -> np.ndarray:
    """
    Compute the 4x4 Jacobian matrix dF/dx at a given state.

    Args:
        state: EISV state point
        params: Dynamics parameters (default: active params)
        theta: Theta parameters (default: DEFAULT_THETA)
        complexity: Task complexity (default: 0.5)
        method: 'analytical' (exact) or 'numerical' (finite differences)

    Returns:
        4x4 numpy array
    """
    if params is None:
        params = get_active_params()
    if theta is None:
        theta = DEFAULT_THETA

    if method == "numerical":
        return _numerical_jacobian(state, params, theta, complexity)
    return _analytical_jacobian(state, params, theta, complexity)


def _numerical_jacobian(
    state: State,
    params: DynamicsParams,
    theta: Theta,
    complexity: float = 0.5,
    epsilon: float = 1e-6,
) -> np.ndarray:
    """Central finite-difference Jacobian."""
    x0 = _state_to_vec(state)
    J = np.zeros((4, 4))

    for i in range(4):
        x_plus = x0.copy()
        x_minus = x0.copy()
        x_plus[i] += epsilon
        x_minus[i] -= epsilon

        # Clip to valid bounds
        x_plus[0] = np.clip(x_plus[0], 0.001, 0.999)
        x_plus[1] = np.clip(x_plus[1], 0.001, 0.999)
        x_plus[2] = np.clip(x_plus[2], 0.002, 1.999)
        x_plus[3] = np.clip(x_plus[3], -1.999, 1.999)
        x_minus[0] = np.clip(x_minus[0], 0.001, 0.999)
        x_minus[1] = np.clip(x_minus[1], 0.001, 0.999)
        x_minus[2] = np.clip(x_minus[2], 0.002, 1.999)
        x_minus[3] = np.clip(x_minus[3], -1.999, 1.999)

        f_plus = _compute_rhs(_vec_to_state(x_plus), params, theta, complexity)
        f_minus = _compute_rhs(_vec_to_state(x_minus), params, theta, complexity)

        J[:, i] = (f_plus - f_minus) / (x_plus[i] - x_minus[i])

    return J


def _analytical_jacobian(
    state: State,
    params: DynamicsParams,
    theta: Theta,
    complexity: float = 0.5,
) -> np.ndarray:
    """
    Analytical Jacobian from the ODE equations.

    Accounts for both linear and logistic I-dynamics modes.
    """
    E, I, S, V = state.E, state.I, state.S, state.V

    # dC/dV = Cmax * 0.5 * C1 * sech^2(C1 * V)
    tanh_val = math.tanh(theta.C1 * V)
    dCdV = params.Cmax * 0.5 * theta.C1 * (1.0 - tanh_val ** 2)

    lam2_val = _lambda2(theta, params)

    J = np.zeros((4, 4))

    # dE/dt = alpha*(I - E) - beta_E*E*S + gamma_E*drift_sq
    J[0, 0] = -params.alpha - params.beta_E * S
    J[0, 1] = params.alpha
    J[0, 2] = -params.beta_E * E
    J[0, 3] = 0.0

    # dI/dt
    i_mode = get_i_dynamics_mode()
    J[1, 0] = 0.0
    if i_mode == "linear":
        J[1, 1] = -params.gamma_I
    else:
        J[1, 1] = -params.gamma_I * (1.0 - 2.0 * I)
    J[1, 2] = -params.k
    J[1, 3] = params.beta_I * dCdV

    # dS/dt = -mu*S + lam1*drift_sq - lam2*C(V) + beta_c*complexity
    J[2, 0] = 0.0
    J[2, 1] = 0.0
    J[2, 2] = -params.mu
    J[2, 3] = -lam2_val * dCdV

    # dV/dt = kappa*(E - I) - delta*V
    J[3, 0] = params.kappa
    J[3, 1] = -params.kappa
    J[3, 2] = 0.0
    J[3, 3] = -params.delta

    # Barrier Jacobian contributions (only non-zero near boundaries)
    # d(barrier)/dx for lower bound: -strength * 3 * t² / margin
    # d(barrier)/dx for upper bound: -strength * 3 * t² / margin
    m = params.barrier_margin
    s = params.barrier_strength
    S_range_ratio = params.S_max - params.S_min
    V_range_ratio = params.V_max - params.V_min

    # E barrier
    if E - params.E_min < m:
        t = 1.0 - (E - params.E_min) / m
        J[0, 0] += -s * 3.0 * t * t / m
    if params.E_max - E < m:
        t = 1.0 - (params.E_max - E) / m
        J[0, 0] += -s * 3.0 * t * t / m

    # I barrier
    if I - params.I_min < m:
        t = 1.0 - (I - params.I_min) / m
        J[1, 1] += -s * 3.0 * t * t / m
    if params.I_max - I < m:
        t = 1.0 - (params.I_max - I) / m
        J[1, 1] += -s * 3.0 * t * t / m

    # S barrier (scaled margin)
    m_S = m * S_range_ratio
    if S - params.S_min < m_S:
        t = 1.0 - (S - params.S_min) / m_S
        J[2, 2] += -s * 3.0 * t * t / m_S
    if params.S_max - S < m_S:
        t = 1.0 - (params.S_max - S) / m_S
        J[2, 2] += -s * 3.0 * t * t / m_S

    # V barrier (scaled margin)
    m_V = m * V_range_ratio
    if V - params.V_min < m_V:
        t = 1.0 - (V - params.V_min) / m_V
        J[3, 3] += -s * 3.0 * t * t / m_V
    if params.V_max - V < m_V:
        t = 1.0 - (params.V_max - V) / m_V
        J[3, 3] += -s * 3.0 * t * t / m_V

    return J


# ---------------------------------------------------------------------------
# Stability verification
# ---------------------------------------------------------------------------

def verify_lyapunov_stability(
    state: State = None,
    params: DynamicsParams = None,
    theta: Theta = None,
    complexity: float = 0.5,
    M: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    """
    Formal Lyapunov stability check via eigenvalue analysis.

    If no state is provided, computes the equilibrium point.

    Args:
        state: EISV state (default: equilibrium)
        params: Dynamics parameters (default: active)
        theta: Theta parameters (default: DEFAULT_THETA)
        complexity: Task complexity
        M: Optional diagonal metric matrix for weighted analysis

    Returns:
        Dict with:
            stable: bool
            contraction_rate: float (positive = contracting)
            max_eigenvalue: float
            eigenvalues: list of 4 floats
            method: str describing the analysis method
    """
    if params is None:
        params = get_active_params()
    if theta is None:
        theta = DEFAULT_THETA
    if state is None:
        state = compute_equilibrium(params, theta, complexity=complexity)

    J = _analytical_jacobian(state, params, theta, complexity)
    result = _check_contraction(J, M)

    return {
        "stable": result["is_contracting"],
        "contraction_rate": result["contraction_rate"],
        "max_eigenvalue": result["max_eigenvalue"],
        "eigenvalues": result["eigenvalues"],
        "method": "lyapunov_eigenvalue_analysis",
    }


def _check_contraction(
    J: np.ndarray,
    M: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    """
    Check contraction of Jacobian J with optional diagonal metric M.

    Contraction holds iff all eigenvalues of the symmetric part of
    M^{1/2} J M^{-1/2} are negative.
    """
    if M is not None:
        m_diag = np.diag(M)
        m_sqrt = np.sqrt(m_diag)
        m_inv_sqrt = 1.0 / m_sqrt
        J_transformed = np.diag(m_sqrt) @ J @ np.diag(m_inv_sqrt)
    else:
        J_transformed = J

    J_sym = 0.5 * (J_transformed + J_transformed.T)
    eigenvalues = np.linalg.eigvalsh(J_sym)
    eigenvalues.sort()

    max_eig = float(eigenvalues[-1])

    return {
        "eigenvalues": eigenvalues.tolist(),
        "max_eigenvalue": max_eig,
        "contraction_rate": -max_eig,
        "is_contracting": max_eig < -1e-10,
    }


# ---------------------------------------------------------------------------
# Gershgorin bound
# ---------------------------------------------------------------------------

def gershgorin_stability_bound(
    state: State = None,
    params: DynamicsParams = None,
    theta: Theta = None,
    complexity: float = 0.5,
) -> Dict[str, Any]:
    """
    Conservative Gershgorin circle bound on eigenvalues.

    Faster than full eigenvalue decomposition but less tight.

    Returns:
        Dict with disks, max_real_bound, is_stable, gershgorin_rate
    """
    if params is None:
        params = get_active_params()
    if theta is None:
        theta = DEFAULT_THETA
    if state is None:
        state = compute_equilibrium(params, theta, complexity=complexity)

    J = _analytical_jacobian(state, params, theta, complexity)
    n = J.shape[0]
    disks = []
    for i in range(n):
        center = J[i, i]
        radius = sum(abs(J[i, j]) for j in range(n) if j != i)
        disks.append({"center": float(center), "radius": float(radius)})

    max_real = max(d["center"] + d["radius"] for d in disks)

    return {
        "disks": disks,
        "max_real_bound": float(max_real),
        "is_stable": max_real < 0,
        "gershgorin_rate": -max_real if max_real < 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Parameter sweep
# ---------------------------------------------------------------------------

def sweep_stability(
    params: DynamicsParams = None,
    theta_base: Theta = None,
    n_points: int = 20,
    complexity: float = 0.5,
) -> Dict[str, Any]:
    """
    Sweep C1 and eta1 parameters for contraction robustness.

    Returns:
        Dict with C1_values, eta1_values, contraction_rates (2D),
        all_stable, min_rate, max_rate, mean_rate
    """
    if params is None:
        params = get_active_params()
    if theta_base is None:
        theta_base = DEFAULT_THETA

    C1_values = np.linspace(params.C1_min, params.C1_max, n_points)
    eta1_values = np.linspace(params.eta1_min, params.eta1_max, n_points)

    rates = np.zeros((n_points, n_points))
    all_stable = True

    for i, c1 in enumerate(C1_values):
        for j, eta1 in enumerate(eta1_values):
            theta = Theta(C1=c1, eta1=eta1)
            eq = compute_equilibrium(params, theta, complexity=complexity)
            J = _analytical_jacobian(eq, params, theta, complexity)
            result = _check_contraction(J)
            rates[i, j] = result["contraction_rate"]
            if not result["is_contracting"]:
                all_stable = False

    return {
        "C1_values": C1_values.tolist(),
        "eta1_values": eta1_values.tolist(),
        "contraction_rates": rates.tolist(),
        "all_stable": all_stable,
        "min_rate": float(np.min(rates)),
        "max_rate": float(np.max(rates)),
        "mean_rate": float(np.mean(rates)),
    }


# ---------------------------------------------------------------------------
# Metric optimization
# ---------------------------------------------------------------------------

def optimize_stability_metric(
    state: State = None,
    params: DynamicsParams = None,
    theta: Theta = None,
    complexity: float = 0.5,
    initial_M: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    """
    Find diagonal metric M that maximizes contraction rate.

    Uses Nelder-Mead optimization with M = diag(exp(z)) for positivity.

    Returns:
        Dict with optimal_M, optimal_contraction_rate, eigenvalues,
        initial_rate, improvement
    """
    from scipy.optimize import minimize

    if params is None:
        params = get_active_params()
    if theta is None:
        theta = DEFAULT_THETA
    if state is None:
        state = compute_equilibrium(params, theta, complexity=complexity)

    J = _analytical_jacobian(state, params, theta, complexity)

    if initial_M is None:
        initial_M = np.diag([0.1, 0.2, 1.0, 0.08])

    initial_z = np.log(np.diag(initial_M))
    initial_result = _check_contraction(J, initial_M)
    initial_rate = initial_result["contraction_rate"]

    def objective(z):
        M = np.diag(np.exp(z))
        result = _check_contraction(J, M)
        return -result["contraction_rate"]

    result = minimize(
        objective, initial_z, method="Nelder-Mead",
        options={"maxiter": 5000, "xatol": 1e-8, "fatol": 1e-10},
    )

    optimal_M_diag = np.exp(result.x)
    optimal_M = np.diag(optimal_M_diag)
    optimal_result = _check_contraction(J, optimal_M)

    return {
        "optimal_M": optimal_M_diag.tolist(),
        "optimal_contraction_rate": optimal_result["contraction_rate"],
        "eigenvalues": optimal_result["eigenvalues"],
        "initial_rate": initial_rate,
        "improvement": optimal_result["contraction_rate"] - initial_rate,
    }
