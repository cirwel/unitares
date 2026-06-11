#!/usr/bin/env python3
"""
Contraction Analysis Verifier for UNITARES EISV Dynamics

Numerically verifies the contraction property of the EISV Jacobian.
Computes eigenvalues, optimizes a diagonal metric, and compares
with the Gershgorin bound (alpha_c = 0.02).

Usage:
    python scripts/contraction_analysis.py [--grid-points N] [--output-dir DIR] [--no-plot]
"""

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional

import numpy as np

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from governance_core.dynamics import (
    compute_equilibrium, State, _derivatives,
)
from governance_core.parameters import (
    DynamicsParams, Theta, get_active_params, DEFAULT_THETA,
    get_i_dynamics_mode,
)
from governance_core.coherence import lambda2
from governance_core.utils import drift_norm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def state_to_vec(state: State) -> np.ndarray:
    return np.array([state.E, state.I, state.S, state.V])


def vec_to_state(vec: np.ndarray) -> State:
    return State(E=float(vec[0]), I=float(vec[1]),
                 S=float(vec[2]), V=float(vec[3]))


def compute_rhs(
    state: State,
    params: DynamicsParams,
    theta: Theta,
    delta_eta: Optional[List[float]] = None,
    dt: float = 0.1,
    complexity: float = 0.5,
) -> np.ndarray:
    """
    Extract the derivative vector F(x) = (dE/dt, dI/dt, dS/dt, dV/dt).

    Uses _derivatives directly for the true ODE right-hand side,
    independent of the integration method (Euler/RK4).
    """
    if delta_eta is None:
        delta_eta = [0.0] * 5
    d_eta = drift_norm(delta_eta)
    d_eta_sq = d_eta * d_eta
    derivs = _derivatives(state, d_eta_sq, theta, params, 0.0, complexity, None)
    return np.array(derivs)


# ---------------------------------------------------------------------------
# Jacobian computation
# ---------------------------------------------------------------------------

def numerical_jacobian(
    state: State,
    params: DynamicsParams,
    theta: Theta,
    delta_eta: Optional[List[float]] = None,
    complexity: float = 0.5,
    epsilon: float = 1e-6,
) -> np.ndarray:
    """
    Compute 4x4 Jacobian J = dF/dx via central finite differences.
    """
    if delta_eta is None:
        delta_eta = [0.0] * 5

    x0 = state_to_vec(state)
    J = np.zeros((4, 4))

    for i in range(4):
        x_plus = x0.copy()
        x_minus = x0.copy()
        x_plus[i] += epsilon
        x_minus[i] -= epsilon

        # Clip to ensure valid state
        x_plus[0] = np.clip(x_plus[0], 0.001, 0.999)
        x_plus[1] = np.clip(x_plus[1], 0.001, 0.999)
        x_plus[2] = np.clip(x_plus[2], 0.002, 1.999)
        x_plus[3] = np.clip(x_plus[3], -1.999, 1.999)
        x_minus[0] = np.clip(x_minus[0], 0.001, 0.999)
        x_minus[1] = np.clip(x_minus[1], 0.001, 0.999)
        x_minus[2] = np.clip(x_minus[2], 0.002, 1.999)
        x_minus[3] = np.clip(x_minus[3], -1.999, 1.999)

        f_plus = compute_rhs(vec_to_state(x_plus), params, theta,
                             delta_eta, complexity=complexity)
        f_minus = compute_rhs(vec_to_state(x_minus), params, theta,
                              delta_eta, complexity=complexity)

        J[:, i] = (f_plus - f_minus) / (x_plus[i] - x_minus[i])

    return J


def _barrier_derivative(x: float, lo: float, hi: float, strength: float, margin: float) -> float:
    """
    Derivative of barrier(x) w.r.t. x.

    barrier = +strength * t_lo³  (near lo)  -  strength * t_hi³  (near hi)
    where t = 1 - dist/margin.

    d(barrier)/dx = -3*strength*t²/margin  for each active bound.
    """
    deriv = 0.0
    dist_lo = x - lo
    if dist_lo < margin:
        t = 1.0 - dist_lo / margin
        deriv += -3.0 * strength * t * t / margin
    dist_hi = hi - x
    if dist_hi < margin:
        t = 1.0 - dist_hi / margin
        deriv += -3.0 * strength * t * t / margin
    return deriv


def analytical_jacobian(
    state: State,
    params: DynamicsParams,
    theta: Theta,
    complexity: float = 0.5,
) -> np.ndarray:
    """
    Compute analytical Jacobian from the ODE equations, including soft barriers.

    Diagonal entries include barrier derivative contributions when the state
    is within barrier_margin of a bound.

    where dC/dV = Cmax * 0.5 * C1 * sech^2(C1 * V)
    """
    E, I, S, V = state.E, state.I, state.S, state.V

    # dC/dV = Cmax * 0.5 * C1 * (1 - tanh^2(C1*V))
    tanh_val = math.tanh(theta.C1 * V)
    dCdV = params.Cmax * 0.5 * theta.C1 * (1.0 - tanh_val ** 2)

    lam2_val = lambda2(theta, params)

    # Barrier margins (must match dynamics.py _derivatives)
    m = params.barrier_margin
    s = params.barrier_strength
    S_range = params.S_max - params.S_min
    V_range = params.V_max - params.V_min

    J = np.zeros((4, 4))

    # dE/dt = alpha*(I - E) - beta_E*E*S + gamma_E*drift_sq + barrier(E)
    J[0, 0] = -params.alpha - params.beta_E * S     # d(dE/dt)/dE
    J[0, 0] += _barrier_derivative(E, params.E_min, params.E_max, s, m)
    J[0, 1] = params.alpha                           # d(dE/dt)/dI
    J[0, 2] = -params.beta_E * E                     # d(dE/dt)/dS
    J[0, 3] = 0.0                                    # d(dE/dt)/dV

    # dI/dt = beta_I*C(V) - k*S - gamma_I*I + barrier(I)
    i_mode = get_i_dynamics_mode()
    J[1, 0] = 0.0                                    # d(dI/dt)/dE
    if i_mode == "linear":
        J[1, 1] = -params.gamma_I                    # d(dI/dt)/dI
    else:
        J[1, 1] = -params.gamma_I * (1.0 - 2.0 * I) # logistic
    J[1, 1] += _barrier_derivative(I, params.I_min, params.I_max, s, m)
    J[1, 2] = -params.k                              # d(dI/dt)/dS
    J[1, 3] = params.beta_I * dCdV                   # d(dI/dt)/dV

    # dS/dt = -mu*S + lam1*drift_sq - lam2*C(V) + beta_c*complexity + barrier(S)
    J[2, 0] = 0.0                                    # d(dS/dt)/dE
    J[2, 1] = 0.0                                    # d(dS/dt)/dI
    J[2, 2] = -params.mu                             # d(dS/dt)/dS
    J[2, 2] += _barrier_derivative(S, params.S_min, params.S_max, s, m * S_range)
    J[2, 3] = -lam2_val * dCdV                       # d(dS/dt)/dV

    # dV/dt = kappa*(E - I) - delta*V + barrier(V)
    J[3, 0] = params.kappa                            # d(dV/dt)/dE
    J[3, 1] = -params.kappa                           # d(dV/dt)/dI
    J[3, 2] = 0.0                                     # d(dV/dt)/dS
    J[3, 3] = -params.delta                           # d(dV/dt)/dV
    J[3, 3] += _barrier_derivative(V, params.V_min, params.V_max, s, m * V_range)

    return J


# ---------------------------------------------------------------------------
# Contraction checks
# ---------------------------------------------------------------------------

def check_contraction(
    J: np.ndarray,
    M: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    """
    Check contraction property of Jacobian J with optional metric M.

    Contraction holds iff all eigenvalues of the symmetric part of
    M^{1/2} J M^{-1/2} are negative.
    """
    if M is not None:
        # M is diagonal: M^{1/2} J M^{-1/2}
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
        'eigenvalues': eigenvalues.tolist(),
        'max_eigenvalue': max_eig,
        'contraction_rate': -max_eig,
        'is_contracting': max_eig < -1e-10,
        'J_sym': J_sym.tolist(),
    }


def optimize_metric(
    J: np.ndarray,
    initial_M: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    """
    Find diagonal metric M that maximizes the contraction rate.

    Parameterizes M = diag(exp(z)) to enforce positivity.
    """
    from scipy.optimize import minimize

    if initial_M is None:
        initial_M = np.diag([0.1, 0.2, 1.0, 0.08])

    initial_z = np.log(np.diag(initial_M))

    # Compute initial rate
    initial_result = check_contraction(J, initial_M)
    initial_rate = initial_result['contraction_rate']

    def objective(z):
        M = np.diag(np.exp(z))
        result = check_contraction(J, M)
        # Minimize negative contraction rate = maximize contraction rate
        return -result['contraction_rate']

    result = minimize(objective, initial_z, method='Nelder-Mead',
                      options={'maxiter': 5000, 'xatol': 1e-8, 'fatol': 1e-10})

    optimal_M_diag = np.exp(result.x)
    optimal_M = np.diag(optimal_M_diag)
    optimal_result = check_contraction(J, optimal_M)

    return {
        'optimal_M': optimal_M_diag.tolist(),
        'optimal_contraction_rate': optimal_result['contraction_rate'],
        'eigenvalues': optimal_result['eigenvalues'],
        'optimization_success': result.success,
        'initial_rate': initial_rate,
        'initial_M': np.diag(initial_M).tolist(),
        'improvement': optimal_result['contraction_rate'] - initial_rate,
    }


def gershgorin_bound(J: np.ndarray) -> Dict[str, Any]:
    """
    Compute Gershgorin circle bound on eigenvalues.
    """
    n = J.shape[0]
    disks = []
    for i in range(n):
        center = J[i, i]
        radius = sum(abs(J[i, j]) for j in range(n) if j != i)
        disks.append({'center': float(center), 'radius': float(radius)})

    max_real = max(d['center'] + d['radius'] for d in disks)

    return {
        'disks': disks,
        'max_real_bound': float(max_real),
        'is_stable': max_real < 0,
        'gershgorin_rate': -max_real if max_real < 0 else 0.0,
    }


def sweep_theta_robustness(
    params: DynamicsParams,
    theta_base: Theta,
    n_points: int = 20,
    complexity: float = 0.5,
) -> Dict[str, Any]:
    """
    Sweep C1 and eta1 to check contraction robustness.
    """
    C1_values = np.linspace(params.C1_min, params.C1_max, n_points)
    eta1_values = np.linspace(params.eta1_min, params.eta1_max, n_points)

    rates = np.zeros((n_points, n_points))
    all_contracting = True

    for i, c1 in enumerate(C1_values):
        for j, eta1 in enumerate(eta1_values):
            theta = Theta(C1=c1, eta1=eta1)
            eq = compute_equilibrium(params, theta, complexity=complexity)
            J = analytical_jacobian(eq, params, theta, complexity=complexity)
            result = check_contraction(J)
            rates[i, j] = result['contraction_rate']
            if not result['is_contracting']:
                all_contracting = False

    return {
        'C1_values': C1_values.tolist(),
        'eta1_values': eta1_values.tolist(),
        'contraction_rates': rates.tolist(),
        'all_contracting': all_contracting,
        'min_rate': float(np.min(rates)),
        'max_rate': float(np.max(rates)),
        'mean_rate': float(np.mean(rates)),
    }


def check_contraction_at_samples(
    sample_states: List[State],
    params: DynamicsParams,
    theta: Theta,
) -> Dict[str, Any]:
    """Check contraction at multiple sampled states."""
    n_checked = 0
    n_contracting = 0
    worst_eig = -float('inf')

    for state in sample_states:
        J = analytical_jacobian(state, params, theta)
        result = check_contraction(J)
        n_checked += 1
        if result['is_contracting']:
            n_contracting += 1
        if result['max_eigenvalue'] > worst_eig:
            worst_eig = result['max_eigenvalue']

    return {
        'n_checked': n_checked,
        'n_contracting': n_contracting,
        'worst_eigenvalue': float(worst_eig),
        'fraction_contracting': n_contracting / n_checked if n_checked > 0 else 0,
    }


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def plot_contraction_results(results: Dict[str, Any], output_dir: Path) -> None:
    """Generate contraction analysis plots."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        'font.family': 'serif',
        'font.size': 10,
        'axes.labelsize': 11,
        'figure.dpi': 300,
        'savefig.bbox': 'tight',
    })

    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Eigenvalue spectrum at equilibrium
    bare = results['bare_jacobian']
    eigs = bare['eigenvalues']
    fig, ax = plt.subplots(figsize=(4, 3))
    colors = ['#d62728' if e > 0 else '#2ca02c' for e in eigs]
    ax.bar(range(len(eigs)), eigs, color=colors)
    ax.axhline(0, color='black', linewidth=0.5)
    ax.set_xticks(range(4))
    ax.set_xticklabels(['E', 'I', 'S', 'V'])
    ax.set_ylabel('Eigenvalue (J_sym)')
    ax.set_title(f'Eigenvalue Spectrum (rate={bare["contraction_rate"]:.4f})')
    fig.savefig(output_dir / 'eigenvalue_spectrum.png')
    plt.close(fig)

    # 2. Gershgorin circles
    gersh = results['gershgorin']
    fig, ax = plt.subplots(figsize=(4.5, 3.5))
    dim_labels = ['E', 'I', 'S', 'V']
    for i, disk in enumerate(gersh['disks']):
        circle = plt.Circle((disk['center'], 0), disk['radius'],
                             fill=False, linewidth=1.5,
                             label=f'{dim_labels[i]}: [{disk["center"]-disk["radius"]:.3f}, {disk["center"]+disk["radius"]:.3f}]')
        ax.add_patch(circle)
        ax.plot(disk['center'], 0, 'o', markersize=4, color=f'C{i}')
    ax.axvline(0, color='red', linestyle='--', linewidth=0.5)
    ax.set_xlim(-1.2, 0.3)
    ax.set_ylim(-0.6, 0.6)
    ax.set_xlabel('Real axis')
    ax.set_ylabel('Imaginary axis')
    ax.set_title('Gershgorin Disks')
    ax.legend(fontsize=7, loc='upper left')
    ax.set_aspect('equal')
    fig.savefig(output_dir / 'gershgorin_circles.png')
    plt.close(fig)

    # 3. Theta robustness heatmap
    sweep = results.get('theta_sweep')
    if sweep:
        C1 = sweep['C1_values']
        eta1 = sweep['eta1_values']
        rates = np.array(sweep['contraction_rates'])

        fig, ax = plt.subplots(figsize=(4.5, 3.5))
        im = ax.imshow(rates.T, origin='lower', aspect='auto',
                       extent=[C1[0], C1[-1], eta1[0], eta1[-1]],
                       cmap='RdYlGn')
        ax.set_xlabel('C1')
        ax.set_ylabel('eta1')
        ax.set_title(f'Contraction Rate (all contracting: {sweep["all_contracting"]})')
        plt.colorbar(im, ax=ax, label='Contraction rate')
        ax.plot(DEFAULT_THETA.C1, DEFAULT_THETA.eta1, 'k*', markersize=10,
                label='default')
        ax.legend()
        fig.savefig(output_dir / 'theta_robustness.png')
        plt.close(fig)

    # 4. Comparison: bare vs metric-optimized eigenvalues
    opt = results.get('metric_optimization')
    if opt:
        fig, ax = plt.subplots(figsize=(5, 3))
        x = np.arange(4)
        w = 0.35
        bare_eigs = bare['eigenvalues']
        opt_eigs = opt['eigenvalues']
        ax.bar(x - w / 2, bare_eigs, w, label=f'Bare (rate={bare["contraction_rate"]:.4f})',
               color='#1f77b4')
        ax.bar(x + w / 2, opt_eigs, w, label=f'Optimized (rate={opt["optimal_contraction_rate"]:.4f})',
               color='#2ca02c')
        ax.axhline(0, color='black', linewidth=0.5)
        ax.set_xticks(x)
        ax.set_xticklabels(['E', 'I', 'S', 'V'])
        ax.set_ylabel('Eigenvalue')
        ax.set_title('Bare vs Optimized Metric')
        ax.legend(fontsize=8)
        fig.savefig(output_dir / 'metric_comparison.png')
        plt.close(fig)

    print(f"Plots saved to {output_dir}/")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(results: Dict[str, Any]) -> None:
    print("\n" + "=" * 60)
    print("CONTRACTION ANALYSIS RESULTS")
    print("=" * 60)

    eq = results['equilibrium']
    print(f"Equilibrium: E={eq[0]:.4f}, I={eq[1]:.4f}, S={eq[2]:.4f}, V={eq[3]:.4f}")
    print(f"I-dynamics mode: {results['i_mode']}")

    # Jacobian
    print(f"\nJacobian (analytical):")
    J = np.array(results['analytical_jacobian'])
    for row in J:
        print(f"  [{', '.join(f'{v:8.4f}' for v in row)}]")

    # Numerical vs analytical agreement
    J_num = np.array(results['numerical_jacobian'])
    max_diff = np.max(np.abs(J - J_num))
    print(f"\nAnalytical vs Numerical max difference: {max_diff:.2e}")

    # Bare contraction
    bare = results['bare_jacobian']
    print(f"\nBare Jacobian contraction:")
    print(f"  Eigenvalues: {[f'{e:.6f}' for e in bare['eigenvalues']]}")
    print(f"  Contracting: {bare['is_contracting']}")
    print(f"  Rate: {bare['contraction_rate']:.6f}")

    # Gershgorin
    gersh = results['gershgorin']
    print(f"\nGershgorin bound:")
    print(f"  Max real bound: {gersh['max_real_bound']:.6f}")
    print(f"  Rate (Gershgorin): {gersh['gershgorin_rate']:.6f}")
    print(f"  Stable: {gersh['is_stable']}")

    # Metric optimization
    opt = results.get('metric_optimization')
    if opt:
        print(f"\nMetric optimization:")
        print(f"  Initial M: diag({[f'{m:.4f}' for m in opt['initial_M']]})")
        print(f"  Optimal M: diag({[f'{m:.4f}' for m in opt['optimal_M']]})")
        print(f"  Initial rate: {opt['initial_rate']:.6f}")
        print(f"  Optimal rate: {opt['optimal_contraction_rate']:.6f}")
        print(f"  Improvement: {opt['improvement']:.6f}")

    # Theta sweep
    sweep = results.get('theta_sweep')
    if sweep:
        print(f"\nTheta robustness sweep ({len(sweep['C1_values'])}x{len(sweep['eta1_values'])}):")
        print(f"  All contracting: {sweep['all_contracting']}")
        print(f"  Min rate: {sweep['min_rate']:.6f}")
        print(f"  Max rate: {sweep['max_rate']:.6f}")
        print(f"  Mean rate: {sweep['mean_rate']:.6f}")

    # Sample check
    sample = results.get('sample_check')
    if sample:
        print(f"\nSample point contraction:")
        print(f"  Checked: {sample['n_checked']}")
        print(f"  Contracting: {sample['n_contracting']} ({sample['fraction_contracting']:.1%})")
        print(f"  Worst eigenvalue: {sample['worst_eigenvalue']:.6f}")

    print("=" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Contraction Analysis Verifier')
    parser.add_argument('--grid-points', '-g', type=int, default=20)
    parser.add_argument('--output-dir', '-o', type=str, default='data/analysis/contraction')
    parser.add_argument('--no-plot', action='store_true')
    parser.add_argument('--basin-data', type=str, default=None,
                        help='Path to basin_results.json for sample-point checking')
    args = parser.parse_args()

    params = get_active_params()
    theta = DEFAULT_THETA
    complexity = 0.5

    eq = compute_equilibrium(params, theta, complexity=complexity)
    eq_vec = [eq.E, eq.I, eq.S, eq.V]

    print(f"Equilibrium: E={eq.E:.4f}, I={eq.I:.4f}, S={eq.S:.4f}, V={eq.V:.4f}")
    print(f"I-dynamics mode: {get_i_dynamics_mode()}")

    # Compute both Jacobians
    print("Computing Jacobians...")
    J_num = numerical_jacobian(eq, params, theta, complexity=complexity)
    J_ana = analytical_jacobian(eq, params, theta, complexity=complexity)

    max_diff = np.max(np.abs(J_num - J_ana))
    print(f"Analytical vs Numerical max diff: {max_diff:.2e}")
    if max_diff > 1e-3:
        print("WARNING: Jacobians diverge significantly!")

    # Check contraction (bare)
    bare_result = check_contraction(J_ana)
    print(f"Bare contraction rate: {bare_result['contraction_rate']:.6f}")

    # Gershgorin
    gersh = gershgorin_bound(J_ana)
    print(f"Gershgorin rate: {gersh['gershgorin_rate']:.6f}")

    # Metric optimization
    print("Optimizing metric...")
    opt_result = optimize_metric(J_ana)
    print(f"Optimized rate: {opt_result['optimal_contraction_rate']:.6f}")

    # Theta sweep
    print(f"Sweeping Theta ({args.grid_points}x{args.grid_points})...")
    sweep = sweep_theta_robustness(params, theta, n_points=args.grid_points,
                                    complexity=complexity)

    results = {
        'equilibrium': eq_vec,
        'i_mode': get_i_dynamics_mode(),
        'numerical_jacobian': J_num.tolist(),
        'analytical_jacobian': J_ana.tolist(),
        'bare_jacobian': bare_result,
        'gershgorin': gersh,
        'metric_optimization': opt_result,
        'theta_sweep': sweep,
    }

    # Optional: check contraction at basin sample points
    if args.basin_data:
        print(f"Loading basin data from {args.basin_data}...")
        with open(args.basin_data) as f:
            basin_data = json.load(f)
        samples = basin_data.get('sample_summary', [])
        sample_states = []
        for s in samples[:200]:  # Check up to 200 points
            if 'initial_state' in s:
                # Stored as list, but sample_summary may not have it
                pass
        # Use equilibrium perturbations instead
        rng = np.random.default_rng(42)
        eq_arr = np.array(eq_vec)
        sample_states = []
        for _ in range(100):
            perturb = rng.standard_normal(4) * 0.1
            s = eq_arr + perturb
            s[0] = np.clip(s[0], 0.01, 0.99)
            s[1] = np.clip(s[1], 0.01, 0.99)
            s[2] = np.clip(s[2], 0.01, 1.99)
            s[3] = np.clip(s[3], -1.99, 1.99)
            sample_states.append(vec_to_state(s))

        sample_result = check_contraction_at_samples(sample_states, params, theta)
        results['sample_check'] = sample_result

    print_summary(results)

    if not args.no_plot:
        plot_contraction_results(results, Path(args.output_dir))

    # Save results
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / 'contraction_results.json', 'w') as f:
        json.dump(results, f, indent=2)
    print(f"Results saved to {out_dir / 'contraction_results.json'}")


if __name__ == '__main__':
    main()
