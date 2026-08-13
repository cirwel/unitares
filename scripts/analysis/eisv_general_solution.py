"""General (closed-form) solution of the reduced 3-state EISV system.

Companion to ``docs/proposals/eisv-general-solution-v0.md``. The reduced
dynamics proposed in ``docs/proposals/eisv-grounded-coherence-rederivation-v0.md``
(section 3.2) are upper-triangular:

    dS/dt = -mu*S + lambda1*u + beta_c*c
    dI/dt = -k*S - gamma_I*(I - I_bar)
    dE/dt = alpha*(I - E) - beta_E*E*S + gamma_E*u

with piecewise-constant inputs u = ||delta_eta||^2 and c = C_task and the
per-agent baseline I_bar. Because S decouples, and I sees only S, the system
is solvable by quadratures: S(t) and I(t) are elementary, and E(t) is a
linear time-varying scalar ODE whose integrating-factor integral is an
incomplete-gamma expression (evaluated here by high-order Gauss-Legendre
quadrature, which is the analytic-continuation-safe form for either sign of
S0 - S*).

The module also closes, at the general level, the two proofs the
rederivation proposal deferred (its section 3.5):

  * slow-drift tracking: an explicit ISS-style bound
    ||x(t) - x*(r(t))||_M <= exp(-alpha_c*t)*||e0||_M + nu/alpha_c,
    with nu computed from the closed-form equilibrium sensitivity Dx*(r);
  * the stochastic mean-square constant: the stationary covariance P of the
    S-noise-driven fluctuation solves the Lyapunov equation J*P + P*J' = -Sigma
    which, J being triangular, has a closed form by back-substitution. The
    (I, S) block is exact for the full nonlinear system (that subsystem is
    linear); only the E row is a linearization.

Finally it computes analytic statistics of the residual coherence readout
C = exp(-0.5 * (z - r)' W (z - r)) under the stationary Gaussian law
z ~ N(m + r, P): E[C] and Var(C) via the Gaussian quadratic-form MGF. This is
the piece that speaks to the doctor's ``signal_degeneracy`` finding: the
dispersion of the repaired coherence signal is a computable function of the
physical parameters, not a number to be tuned after deployment.

Run ``python scripts/analysis/eisv_general_solution.py`` for the full
verification suite (closed forms vs. numerical integration, Lyapunov residual
and Monte-Carlo covariance, MGF vs. Monte-Carlo coherence stats, contraction
certificate grid, tracking-bound simulation). Exit code 0 iff all checks pass.

No deployed behaviour is changed by this module; it is analysis only.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, replace

import numpy as np

@dataclass(frozen=True)
class ReducedParams:
    """Parameters of the reduced 3-state system (rederivation proposal 3.2)."""

    alpha: float = 0.42     # I -> E coupling (deployed default)
    beta_E: float = 0.1     # S damping on E
    gamma_E: float = 0.05   # drift feedback to E
    k: float = 0.1          # S -> I coupling
    gamma_I: float = 0.25   # I relaxation rate toward the agent baseline
    mu: float = 0.8         # S decay rate (paper value; deployed uses 0.5)
    lambda1: float = 0.3    # drift -> S coupling
    beta_c: float = 0.15    # task complexity -> S coupling


# The parameter sets the certificate discussion refers to: the certified set
# (paper mu = 0.8), the deployed-derived set (mu = 0.5), and the ACTIVE
# deployed configuration — get_active_params() auto-applies gamma_I = 0.169
# (V42P tuning) whenever linear I-dynamics mode is on with the default
# profile, which is the running default (governance_core/parameters.py).
CERTIFICATE_PARAMS = ReducedParams()
DEPLOYED_PARAMS = replace(CERTIFICATE_PARAMS, mu=0.5)
ACTIVE_PARAMS = replace(DEPLOYED_PARAMS, gamma_I=0.169)

# Contraction metric and certified rate from rederivation proposal 3.4.
METRIC_M = np.diag([0.1, 1.0, 0.2])
ALPHA_C = 0.15


@dataclass(frozen=True)
class Inputs:
    """Piecewise-constant exogenous inputs and the per-agent baseline."""

    u: float = 0.2       # ||delta_eta||^2
    c: float = 0.4       # task complexity C_task
    I_bar: float = 0.8   # agent's own slow baseline for I


def equilibrium(p: ReducedParams, r: Inputs) -> np.ndarray:
    """Closed-form per-agent equilibrium (E*, I*, S*) — proposal 3.3."""
    S_star = (p.lambda1 * r.u + p.beta_c * r.c) / p.mu
    I_star = r.I_bar - (p.k / p.gamma_I) * S_star
    A = p.alpha + p.beta_E * S_star
    E_star = (p.alpha * I_star + p.gamma_E * r.u) / A
    return np.array([E_star, I_star, S_star])


def equilibrium_sensitivity(p: ReducedParams, r: Inputs) -> np.ndarray:
    """Jacobian Dx*(r) of the equilibrium w.r.t. r = (I_bar, u, c).

    Rows are (E*, I*, S*); columns are (I_bar, u, c). All entries are
    elementary because the equilibrium map is closed-form.
    """
    x = equilibrium(p, r)
    E_star, _, S_star = x
    A = p.alpha + p.beta_E * S_star

    dS = np.array([0.0, p.lambda1 / p.mu, p.beta_c / p.mu])
    dI = np.array([1.0, 0.0, 0.0]) - (p.k / p.gamma_I) * dS
    dE = (p.alpha * dI + np.array([0.0, p.gamma_E, 0.0]) - E_star * p.beta_E * dS) / A
    return np.vstack([dE, dI, dS])


def solve_S(t: float, S0: float, p: ReducedParams, r: Inputs) -> float:
    """Exact S(t) = S* + (S0 - S*) exp(-mu t)."""
    S_star = equilibrium(p, r)[2]
    return S_star + (S0 - S_star) * math.exp(-p.mu * t)


def solve_I(t: float, S0: float, I0: float, p: ReducedParams, r: Inputs) -> float:
    """Exact I(t), in a form uniformly stable through the resonance gamma_I == mu.

    The textbook b = -k a / (gamma_I - mu) two-exponential form cancels
    catastrophically near resonance; rewriting the particular term as
    -k a e^{-gamma_I t} * expm1((gamma_I - mu) t) / (gamma_I - mu) is exact for
    gamma_I != mu and has the exact resonant limit t as gamma_I -> mu.
    """
    _, I_star, S_star = equilibrium(p, r)
    a = S0 - S_star
    d = p.gamma_I - p.mu
    ramp = t if d == 0.0 else math.expm1(d * t) / d
    return I_star + (I0 - I_star) * math.exp(-p.gamma_I * t) - p.k * a * ramp * math.exp(-p.gamma_I * t)


def solve_E(
    t: float,
    S0: float,
    I0: float,
    E0: float,
    p: ReducedParams,
    r: Inputs,
    quad_nodes: int = 200,
) -> float:
    """E(t) by the integrating-factor formula (solution by quadratures).

    E(t) = exp(-Psi(t)) E0 + int_0^t exp(-(Psi(t) - Psi(tau))) g(tau) dtau
    with Psi(t) = A t + kappa (1 - exp(-mu t)), A = alpha + beta_E S*,
    kappa = beta_E (S0 - S*) / mu, and g(tau) = alpha I(tau) + gamma_E u.

    In incomplete-gamma terms the integral is
    (exp(kappa) kappa^s / mu) [Gamma(-s, kappa e^{-mu t}) - Gamma(-s, kappa)]
    per forcing exponential (s = (A - rho)/mu); Gauss-Legendre quadrature on
    the tau-form evaluates the same expression for either sign of kappa.
    """
    if t == 0.0:
        return E0
    _, _, S_star = equilibrium(p, r)
    a = S0 - S_star
    A = p.alpha + p.beta_E * S_star
    kappa = p.beta_E * a / p.mu

    def psi(x: float) -> float:
        return A * x + kappa * (1.0 - math.exp(-p.mu * x))

    nodes, weights = np.polynomial.legendre.leggauss(quad_nodes)
    tau = 0.5 * t * (nodes + 1.0)
    w = 0.5 * t * weights

    I_tau = np.array([solve_I(x, S0, I0, p, r) for x in tau])
    g = p.alpha * I_tau + p.gamma_E * r.u
    # Psi(t) - Psi(tau) >= 0 up to the bounded kappa term; exp stays tame.
    decay = np.exp(-(A * (t - tau) + kappa * (np.exp(-p.mu * tau) - math.exp(-p.mu * t))))
    integral = float(np.sum(w * decay * g))
    return math.exp(-psi(t)) * E0 + integral


def solve_state(
    t: float, x0: np.ndarray, p: ReducedParams, r: Inputs
) -> np.ndarray:
    """Closed-form (E, I, S)(t) from initial state x0 = (E0, I0, S0)."""
    E0, I0, S0 = x0
    return np.array(
        [
            solve_E(t, S0, I0, E0, p, r),
            solve_I(t, S0, I0, p, r),
            solve_S(t, S0, p, r),
        ]
    )


def rhs(x: np.ndarray, p: ReducedParams, r: Inputs) -> np.ndarray:
    """Vector field of the reduced system, state ordered (E, I, S)."""
    E, I, S = x
    dE = p.alpha * (I - E) - p.beta_E * E * S + p.gamma_E * r.u
    dI = -p.k * S - p.gamma_I * (I - r.I_bar)
    dS = -p.mu * S + p.lambda1 * r.u + p.beta_c * r.c
    return np.array([dE, dI, dS])


def jacobian(p: ReducedParams, E: float, S: float) -> np.ndarray:
    """Upper-triangular Jacobian at (E, S) (I does not enter)."""
    return np.array(
        [
            [-(p.alpha + p.beta_E * S), p.alpha, -p.beta_E * E],
            [0.0, -p.gamma_I, -p.k],
            [0.0, 0.0, -p.mu],
        ]
    )


def stationary_covariance(p: ReducedParams, r: Inputs, sigma_S: float) -> np.ndarray:
    """Closed-form stationary covariance for S-channel noise of amplitude sigma_S.

    Solves J P + P J' = -diag(0, 0, sigma_S^2) by back-substitution around the
    equilibrium (J evaluated at (E*, S*)). The (I, S) block is exact for the
    full nonlinear system — that subsystem is linear; the E row linearizes the
    bilinear E*S term.
    """
    E_star, _, S_star = equilibrium(p, r)
    A = p.alpha + p.beta_E * S_star
    s2 = sigma_S * sigma_S

    P33 = s2 / (2.0 * p.mu)
    P23 = -p.k * P33 / (p.mu + p.gamma_I)
    P22 = -p.k * P23 / p.gamma_I
    P13 = (p.alpha * P23 - p.beta_E * E_star * P33) / (p.mu + A)
    P12 = (-p.k * P13 + p.alpha * P22 - p.beta_E * E_star * P23) / (p.gamma_I + A)
    P11 = (p.alpha * P12 - p.beta_E * E_star * P13) / A
    return np.array([[P11, P12, P13], [P12, P22, P23], [P13, P23, P33]])


def mean_square_ball(P: np.ndarray, M: np.ndarray = METRIC_M) -> float:
    """Exact stationary E||x - x*||_M^2 = tr(M P)."""
    return float(np.trace(M @ P))


def generic_contraction_ball(sigma_S: float, M: np.ndarray = METRIC_M,
                             alpha_c: float = ALPHA_C) -> float:
    """The generic Lohmiller-Slotine bound sigma^2 * max(M_noise) / (2 alpha_c).

    For noise entering only the S channel the standard mean-square bound is
    sigma^2 * M_SS / (2 alpha_c); reported for comparison with the exact
    tr(M P) constant.
    """
    return sigma_S * sigma_S * float(M[2, 2]) / (2.0 * alpha_c)


def coherence_moments(
    m: np.ndarray, P: np.ndarray, W: np.ndarray
) -> tuple[float, float]:
    """(E[C], sd(C)) for C = exp(-0.5 y' W y), y ~ N(m, P), exactly.

    Gaussian quadratic-form MGF: E[exp(-0.5 y' W y)]
      = det(I + P W)^{-1/2} * exp(-0.5 m' W (I + P W)^{-1} m),
    and E[C^2] is the same expression with W -> 2W.
    """

    def _mgf(Wx: np.ndarray) -> float:
        eye = np.eye(len(m))
        core = eye + P @ Wx
        quad = float(m @ (Wx @ np.linalg.solve(core, m)))
        return float(np.linalg.det(core) ** -0.5 * math.exp(-0.5 * quad))

    mean = _mgf(W)
    second = _mgf(2.0 * W)
    var = max(0.0, second - mean * mean)
    return mean, math.sqrt(var)


def tracking_bound_rate(
    p: ReducedParams,
    r: Inputs,
    drift_rate: float,
    M: np.ndarray = METRIC_M,
    alpha_c: float = ALPHA_C,
) -> float:
    """Asymptotic tracking-error bound nu / alpha_c for baseline drift.

    drift_rate bounds |d I_bar / dt|; nu = ||M^{1/2} dx*/dI_bar||_2 * drift_rate
    is the induced equilibrium velocity in the metric norm. The full bound is
    ||x(t) - x*(r(t))||_M <= exp(-alpha_c t) ||e0||_M + nu / alpha_c.
    """
    sens = equilibrium_sensitivity(p, r)[:, 0]  # d x* / d I_bar
    sqrt_M = np.sqrt(np.diag(M))
    nu = float(np.linalg.norm(sqrt_M * sens)) * drift_rate
    return nu / alpha_c


def certificate_grid_margin(
    p: ReducedParams,
    M: np.ndarray = METRIC_M,
    alpha_c: float = ALPHA_C,
    n_grid: int = 26,
) -> float:
    """Max eigenvalue of M J + J' M + 2 alpha_c M over (E, S) in [0, 1]^2.

    Contraction certificate holds iff the returned value is <= 0
    (rederivation proposal 3.4 reports -0.036 at the worst corner for the
    certificate parameter set).
    """
    worst = -np.inf
    for E in np.linspace(0.0, 1.0, n_grid):
        for S in np.linspace(0.0, 1.0, n_grid):
            J = jacobian(p, E, S)
            F = M @ J + J.T @ M + 2.0 * alpha_c * M
            worst = max(worst, float(np.linalg.eigvalsh(F)[-1]))
    return worst


def max_certifiable_alpha_c(
    p: ReducedParams, M: np.ndarray = METRIC_M, tol: float = 1e-4
) -> float:
    """Largest contraction rate the grid certificate supports for params p."""
    lo, hi = 0.0, 1.0
    while hi - lo > tol:
        mid = 0.5 * (lo + hi)
        if certificate_grid_margin(p, M, alpha_c=mid) <= 0.0:
            lo = mid
        else:
            hi = mid
    return lo


# ---------------------------------------------------------------------------
# Verification suite
# ---------------------------------------------------------------------------


def _integrate_numeric(t_eval, x0, p, r):
    from scipy.integrate import solve_ivp

    sol = solve_ivp(
        lambda _t, x: rhs(x, p, r),
        (0.0, float(max(t_eval))),
        x0,
        t_eval=t_eval,
        rtol=1e-11,
        atol=1e-13,
        method="RK45",
        dense_output=False,
    )
    return sol.y.T


def verify_closed_forms(seed: int = 7, n_draws: int = 4) -> float:
    """Max |closed form - RK45| over random parameter draws and times."""
    rng = np.random.default_rng(seed)
    worst = 0.0
    draws = [CERTIFICATE_PARAMS, DEPLOYED_PARAMS]
    for _ in range(n_draws):
        draws.append(
            ReducedParams(
                alpha=rng.uniform(0.2, 0.6),
                beta_E=rng.uniform(0.05, 0.3),
                gamma_E=rng.uniform(0.0, 0.1),
                k=rng.uniform(0.05, 0.3),
                gamma_I=rng.uniform(0.1, 0.6),
                mu=rng.uniform(0.3, 1.0),
                lambda1=rng.uniform(0.1, 0.5),
                beta_c=rng.uniform(0.05, 0.3),
            )
        )
    # Resonant case gamma_I == mu exercised explicitly.
    draws.append(replace(CERTIFICATE_PARAMS, gamma_I=0.5, mu=0.5))

    t_eval = np.array([0.5, 1.0, 3.0, 8.0, 20.0])
    for p in draws:
        r = Inputs(
            u=float(rng.uniform(0.0, 0.5)),
            c=float(rng.uniform(0.0, 1.0)),
            I_bar=float(rng.uniform(0.4, 0.95)),
        )
        x0 = np.array(
            [rng.uniform(0.1, 0.9), rng.uniform(0.1, 0.9), rng.uniform(0.0, 0.8)]
        )
        numeric = _integrate_numeric(t_eval, x0, p, r)
        for i, t in enumerate(t_eval):
            closed = solve_state(float(t), x0, p, r)
            worst = max(worst, float(np.max(np.abs(closed - numeric[i]))))
    return worst


def verify_lyapunov(p: ReducedParams, r: Inputs, sigma_S: float = 0.05) -> float:
    """Residual ||J P + P J' + Sigma||_max of the closed-form covariance."""
    x_star = equilibrium(p, r)
    J = jacobian(p, x_star[0], x_star[2])
    P = stationary_covariance(p, r, sigma_S)
    Sigma = np.diag([0.0, 0.0, sigma_S * sigma_S])
    return float(np.max(np.abs(J @ P + P @ J.T + Sigma)))


def verify_covariance_monte_carlo(
    p: ReducedParams,
    r: Inputs,
    sigma_S: float = 0.05,
    n_steps: int = 400_000,
    dt: float = 0.02,
    seed: int = 11,
) -> float:
    """Max relative error of closed-form P vs Euler-Maruyama on the FULL
    NONLINEAR SDE (dx = rhs(x) dt + sigma_S dW on the S channel).

    Running the nonlinear field (not the linearized one) makes this check
    evidence for the adequacy of the E-row linearization, not merely a
    re-check of the Lyapunov algebra. Covariance is taken about the empirical
    mean, so the O(sigma^2) stationary-mean shift does not contaminate it.
    """
    rng = np.random.default_rng(seed)
    P = stationary_covariance(p, r, sigma_S)

    x = equilibrium(p, r).copy()
    burn = n_steps // 10
    samples = np.empty((n_steps - burn, 3))
    sq = math.sqrt(dt)
    for i in range(n_steps):
        x = x + dt * rhs(x, p, r)
        x[2] += sigma_S * sq * rng.standard_normal()
        if i >= burn:
            samples[i - burn] = x
    dev = samples - samples.mean(axis=0)
    P_mc = dev.T @ dev / len(dev)
    scale = max(float(np.max(np.abs(P))), 1e-12)
    return float(np.max(np.abs(P_mc - P)) / scale)


def verify_coherence_mgf(
    p: ReducedParams,
    r: Inputs,
    sigma_S: float = 0.05,
    n_samples: int = 400_000,
    seed: int = 13,
) -> float:
    """Max relative error of (E[C], sd(C)) MGF values vs direct Monte Carlo."""
    rng = np.random.default_rng(seed)
    P = stationary_covariance(p, r, sigma_S)
    # Baseline offset: r slightly displaced from x* (imperfect baseline).
    m = np.array([0.02, -0.03, 0.05])
    # Two weight matrices: NIS-style diagonal, and a rotated NON-diagonal one
    # so that an ordering error like (I+WP) vs (I+PW) could not slip through.
    W_diag = np.diag(1.0 / np.diag(P))
    theta_rot = 0.3
    c, s = math.cos(theta_rot), math.sin(theta_rot)
    R = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    W_rot = R @ W_diag @ R.T

    worst = 0.0
    for W in (W_diag, W_rot):
        mean_a, sd_a = coherence_moments(m, P, W)
        y = rng.multivariate_normal(m, P, size=n_samples)
        d2 = np.einsum("ij,jk,ik->i", y, W, y)
        C = np.exp(-0.5 * d2)
        mean_mc, sd_mc = float(np.mean(C)), float(np.std(C))
        worst = max(worst, abs(mean_a - mean_mc) / mean_mc, abs(sd_a - sd_mc) / sd_mc)
    return worst


def verify_tracking_bound(
    p: ReducedParams,
    r0: Inputs,
    drift_rate: float = 0.004,
    t_end: float = 120.0,
    dt: float = 0.005,
    alpha_c: float = ALPHA_C,
) -> tuple[float, float]:
    """(max observed tracking error, bound) under a sinusoidal baseline drift.

    I_bar(t) = I_bar0 + (drift_rate / omega) * sin(omega t) so that
    |d I_bar / dt| <= drift_rate exactly. Returns the largest M-norm error
    after the exp(-alpha_c t) transient term has decayed below 1e-6, and the
    asymptotic bound nu / alpha_c; the check passes iff error <= bound.
    """
    omega = 0.05
    amp = drift_rate / omega
    sqrt_M = np.sqrt(np.diag(METRIC_M))

    x = equilibrium(p, r0).copy()
    steps = int(t_end / dt)
    worst = 0.0
    t_settle = -math.log(1e-6) / alpha_c
    for i in range(steps):
        t = i * dt
        r_t = replace(r0, I_bar=r0.I_bar + amp * math.sin(omega * t))
        # RK4 step on the frozen-input field (inputs vary slowly vs dt).
        k1 = rhs(x, p, r_t)
        k2 = rhs(x + 0.5 * dt * k1, p, r_t)
        k3 = rhs(x + 0.5 * dt * k2, p, r_t)
        k4 = rhs(x + dt * k3, p, r_t)
        x = x + (dt / 6.0) * (k1 + 2 * k2 + 2 * k3 + k4)
        if t > t_settle:
            err = float(np.linalg.norm(sqrt_M * (x - equilibrium(p, r_t))))
            worst = max(worst, err)
    return worst, tracking_bound_rate(p, r0, drift_rate, alpha_c=alpha_c)


def main() -> int:
    r = Inputs()
    sigma = 0.05
    checks: list[tuple[str, bool, str]] = []

    err = verify_closed_forms()
    checks.append(
        ("closed forms vs RK45", err < 1e-6, f"max abs error {err:.3e}")
    )

    for label, p in (("certificate", CERTIFICATE_PARAMS), ("deployed", DEPLOYED_PARAMS)):
        res = verify_lyapunov(p, r, sigma)
        checks.append(
            (f"Lyapunov residual ({label})", res < 1e-14, f"residual {res:.3e}")
        )

    mc_err = verify_covariance_monte_carlo(CERTIFICATE_PARAMS, r, sigma)
    checks.append(
        ("stationary covariance vs Monte Carlo", mc_err < 0.08,
         f"max rel error {mc_err:.3f}")
    )

    mgf_err = verify_coherence_mgf(CERTIFICATE_PARAMS, r, sigma)
    checks.append(
        ("coherence moments (MGF) vs Monte Carlo", mgf_err < 0.02,
         f"max rel error {mgf_err:.4f}")
    )

    margin = certificate_grid_margin(CERTIFICATE_PARAMS)
    checks.append(
        ("contraction certificate grid (mu=0.8)", margin <= 0.0,
         f"worst eigenvalue {margin:.4f} (proposal reports -0.036)")
    )
    margin_dep = certificate_grid_margin(DEPLOYED_PARAMS)
    checks.append(
        ("contraction certificate grid (mu=0.5)", margin_dep <= 0.0,
         f"worst eigenvalue {margin_dep:.4f}")
    )
    # The ACTIVE deployed config (gamma_I auto-set to 0.169 in linear mode) is
    # EXPECTED to fail at alpha_c = 0.15; the check asserts that finding and
    # reports the largest rate the certificate does support there.
    margin_act = certificate_grid_margin(ACTIVE_PARAMS)
    alpha_act = max_certifiable_alpha_c(ACTIVE_PARAMS)
    checks.append(
        ("certificate FAILS at active config (gamma_I=0.169), as documented",
         margin_act > 0.0 and alpha_act < ALPHA_C,
         f"worst eigenvalue {margin_act:.4f} at alpha_c=0.15; "
         f"max certifiable alpha_c ~= {alpha_act:.4f}")
    )

    obs, bound = verify_tracking_bound(CERTIFICATE_PARAMS, r)
    checks.append(
        ("tracking bound under baseline drift", obs <= bound,
         f"observed {obs:.5f} <= bound {bound:.5f}")
    )

    print("== EISV general-solution verification ==")
    ok = True
    for name, passed, detail in checks:
        ok &= passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}: {detail}")

    # Report the constants the proposal deferred (its section 3.5, items 1+3).
    # All E-row quantities depend on the evaluation point below; P_SS, P_IS,
    # P_II and the NIS-weight coherence moments are input-invariant.
    print(f"\n== derived constants (certificate params; evaluation point "
          f"u={r.u}, c={r.c}, I_bar={r.I_bar}, sigma_S = {sigma}) ==")
    P = stationary_covariance(CERTIFICATE_PARAMS, r, sigma)
    ball = mean_square_ball(P)
    generic = generic_contraction_ball(sigma)
    print(f"  stationary sd: E {math.sqrt(P[0, 0]):.4f}, "
          f"I {math.sqrt(P[1, 1]):.4f}, S {math.sqrt(P[2, 2]):.4f}")
    print(f"  exact mean-square ball tr(M P) = {ball:.3e} "
          f"(= {ball / sigma**2:.4f} * sigma^2)")
    print(f"  generic contraction bound      = {generic:.3e} "
          f"(= {generic / sigma**2:.4f} * sigma^2), "
          f"{generic / ball:.1f}x looser than exact")
    W = np.diag(1.0 / np.diag(P))
    meanC, sdC = coherence_moments(np.zeros(3), P, W)
    print(f"  residual coherence (NIS weights, unbiased baseline): "
          f"E[C] = {meanC:.4f}, sd(C) = {sdC:.4f}")
    eps_star = 0.05 / tracking_bound_rate(CERTIFICATE_PARAMS, r, 1.0)
    print(f"  baseline drift rate for M-norm tracking error <= 0.05: "
          f"|dI_bar/dt| <= {eps_star:.5f} per unit time")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
