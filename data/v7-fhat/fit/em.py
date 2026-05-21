"""EM loop over 22 params — v5-amendment scope.

Per-class emission params (× 2 classes):
  - 4 Gaussian obs variances (C1-C4)
  - 5 logistic C5 coefficients (beta0, beta_E, beta_I, beta_S, beta_V_abs)
Fleet-wide:
  - 4 transition noise variances
Total = 2 * 9 + 4 = 22.

E-step: per-agent UKF filter with current emission/transition variances.
M-step:
  - Obs variance (per-class per-channel): Var(o - mu_post), clamped.
  - C5 coefficients (per-class): logistic regression on posterior-mean features
    with L2 λ=0.01, sign-constrained via feature encoding (all coefs free;
    post-hoc sign check against pre-registered pattern in SC1).
  - Transition noise (fleet-wide): Var(s_t - fx(s_{t-1})) from smoother pairs.

Convergence: 50 iters OR |Δ log L| < 1e-4.
L2 λ=0.01, seed=42.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import pathlib
from typing import Iterable

import numpy as np
from sklearn.linear_model import LogisticRegression

from .ode import fx as ode_fx
from .ukf_smoother import run_ukf_smoother, SmootherResult

REPO = pathlib.Path(__file__).resolve().parents[3]
ODE_PARAMS_JSON = REPO / "data" / "v7-fhat" / "ode_params.json"

SIGMA_OBS_MIN = 0.01
SIGMA_OBS_MAX = 0.30
SIGMA_TRANS_MIN = 0.005
SIGMA_TRANS_MAX = 0.05


@dataclass
class AgentSeries:
    agent_id: str
    cls: str  # "resident_persistent" or "session_or_unlabeled"
    obs: np.ndarray  # (T, 4) observed_E,I,S,V with np.nan for missing
    dts: np.ndarray  # (T,) hours since previous obs
    is_bad: np.ndarray  # (T,) 0/1 with np.nan if no joined outcome
    T: int = field(init=False)

    def __post_init__(self) -> None:
        self.T = self.obs.shape[0]


@dataclass
class FitState:
    sigma_obs: dict  # cls -> np.ndarray(4)
    sigma_trans: np.ndarray  # (4,) fleet-wide
    c5_coef: dict  # cls -> np.ndarray(5) [beta0, beta_E, beta_I, beta_S, beta_V_abs]
    loglik_history: list[float] = field(default_factory=list)
    iter_count: int = 0
    converged: bool = False


def initial_fit_state() -> FitState:
    init_sigma_obs = np.array([0.1, 0.1, 0.1, 0.1])
    return FitState(
        sigma_obs={
            "resident_persistent": init_sigma_obs.copy(),
            "session_or_unlabeled": init_sigma_obs.copy(),
        },
        sigma_trans=np.array([0.02, 0.02, 0.02, 0.02]),
        c5_coef={
            "resident_persistent": np.zeros(5),
            "session_or_unlabeled": np.zeros(5),
        },
    )


def e_step(
    agents: list[AgentSeries],
    ode_params: dict,
    fit: FitState,
    mu0: np.ndarray,
    sigma0_diag: np.ndarray,
) -> tuple[list[SmootherResult], float]:
    """Run UKF per agent; return posteriors and total log-likelihood."""
    results: list[SmootherResult] = []
    total_ll = 0.0
    for ag in agents:
        r = run_ukf_smoother(
            obs=ag.obs,
            dts=ag.dts,
            ode_params=ode_params,
            sigma_obs=fit.sigma_obs[ag.cls],
            sigma_trans=fit.sigma_trans,
            mu0=mu0,
            sigma0_diag=sigma0_diag,
        )
        results.append(r)
        total_ll += r.loglik
    return results, total_ll


def m_step_obs_variance(
    triples: Iterable[tuple[np.ndarray, np.ndarray, np.ndarray]]
) -> np.ndarray:
    """Proper E-step-aware MLE of observation variance per channel.

    For each (obs, mu_post, diag_var_post) triple, the expected squared residual
    under q(s_t) is E_q[(o - s)^2] = (o - mu_post)^2 + Var_q[s]_j. Using only
    (o - mu_post)^2 systematically under-estimates sigma_obs and can shrink it
    to the lower clip; including the posterior variance is the correct E-step
    contribution.

    Accepts both 3-tuples (obs, mu, var_diag) and legacy 2-tuples (obs, mu)
    for backward compat with tests.
    """
    acc_sq = np.zeros(4)
    acc_n = np.zeros(4)
    for item in triples:
        if len(item) == 3:
            obs, mu, var_diag = item
        else:
            obs, mu = item
            var_diag = np.zeros_like(obs)
        diff = obs - mu
        mask = ~np.isnan(diff)
        acc_sq += np.where(mask, diff**2 + var_diag, 0.0).sum(axis=0)
        acc_n += mask.sum(axis=0)
    var = np.where(acc_n > 0, acc_sq / np.maximum(acc_n, 1), (SIGMA_OBS_MIN**2))
    sigma = np.sqrt(var)
    return np.clip(sigma, SIGMA_OBS_MIN, SIGMA_OBS_MAX)


def m_step_trans_variance(
    agents: list[AgentSeries], results: list[SmootherResult], ode_params: dict
) -> np.ndarray:
    """Pool transition residuals across all agents → fleet-wide sigma_trans.

    E-step-aware: Var_q[s_t - fx(s_{t-1})] ≈ (mu_t - fx(mu_{t-1}))^2 + Var_q[s_t] + Var_q[s_{t-1}]
    (ignoring cross-covariance across time, which requires RTS smoother). Uses filter
    marginals only (smoother omitted; documented caveat).
    """
    acc_sq = np.zeros(4)
    acc_n = 0
    for ag, r in zip(agents, results):
        mu = r.mu
        cov = r.cov
        if mu.shape[0] < 2:
            continue
        for t in range(1, mu.shape[0]):
            dt = ag.dts[t]
            pred = ode_fx(mu[t - 1], dt, ode_params)
            d = mu[t] - pred
            post_var_t = np.diag(cov[t])
            post_var_tm1 = np.diag(cov[t - 1])
            d_norm_sq = (d**2 + post_var_t + post_var_tm1) / max(dt, 1e-6)
            acc_sq += d_norm_sq
            acc_n += 1
    if acc_n == 0:
        return np.full(4, SIGMA_TRANS_MIN)
    var = acc_sq / acc_n
    sigma = np.sqrt(var)
    return np.clip(sigma, SIGMA_TRANS_MIN, SIGMA_TRANS_MAX)


def m_step_c5_coefficients(
    agents: list[AgentSeries], results: list[SmootherResult], l2_lambda: float = 0.01
) -> dict:
    """Per-class logistic regression on (posterior_mean, |V|) → is_bad."""
    out = {}
    for cls in ("resident_persistent", "session_or_unlabeled"):
        X_rows = []
        y_rows = []
        for ag, r in zip(agents, results):
            if ag.cls != cls:
                continue
            T = ag.T
            for t in range(T):
                if np.isnan(ag.is_bad[t]):
                    continue
                E, I, S, V = r.mu[t]
                X_rows.append([E, I, S, abs(V)])
                y_rows.append(int(ag.is_bad[t]))
        if len(y_rows) < 10 or len(set(y_rows)) < 2:
            # Not enough joined outcomes or no positives — skip, keep prior
            out[cls] = np.zeros(5)
            continue
        X = np.asarray(X_rows)
        y = np.asarray(y_rows)
        # sklearn L2: C = 1 / (n * lambda); for lambda=0.01 and typical n, C is large.
        C_val = 1.0 / (len(y) * l2_lambda) if l2_lambda > 0 else 1e6
        clf = LogisticRegression(
            C=C_val, penalty="l2", solver="lbfgs", max_iter=500, fit_intercept=True
        )
        clf.fit(X, y)
        # Spec sign pattern (§2.4 C5):
        #   sigma(beta0 - beta_E*E - beta_I*I + beta_S*S + beta_V*|V|)
        # sklearn gives coefficients directly for logit = beta0 + w·x.
        # To fit the spec sign pattern, we store [beta0, beta_E, beta_I, beta_S, beta_V_abs]
        # where beta_E = -w_E, beta_I = -w_I, beta_S = w_S, beta_V_abs = w_Vabs.
        w = clf.coef_[0]
        b0 = float(clf.intercept_[0])
        coef = np.array([b0, -w[0], -w[1], w[2], w[3]])
        out[cls] = coef
    return out


def em_loop(
    agents: list[AgentSeries],
    ode_params: dict,
    mu0: np.ndarray,
    sigma0_diag: np.ndarray,
    max_iters: int = 50,
    tol: float = 1e-4,
    l2_lambda: float = 0.01,
    verbose: bool = True,
) -> FitState:
    fit = initial_fit_state()
    prev_ll = -np.inf

    for it in range(max_iters):
        # E-step
        results, ll = e_step(agents, ode_params, fit, mu0, sigma0_diag)
        fit.loglik_history.append(ll)
        fit.iter_count = it + 1
        if verbose:
            print(f"[em] iter {it:2d}  log L = {ll:.3f}", flush=True)

        # M-step — obs variance per class (E-step-aware: includes posterior variance)
        new_sigma_obs = {}
        for cls in ("resident_persistent", "session_or_unlabeled"):
            triples = []
            for ag, r in zip(agents, results):
                if ag.cls == cls:
                    # per-time diag of posterior cov for channels (E,I,S,V)
                    var_diag = np.array([np.diag(r.cov[t]) for t in range(r.mu.shape[0])])
                    triples.append((ag.obs, r.mu, var_diag))
            if triples:
                new_sigma_obs[cls] = m_step_obs_variance(triples)
            else:
                new_sigma_obs[cls] = fit.sigma_obs[cls]
        fit.sigma_obs = new_sigma_obs

        # M-step — transition variance (fleet-wide)
        fit.sigma_trans = m_step_trans_variance(agents, results, ode_params)

        # M-step — C5 coefficients
        fit.c5_coef = m_step_c5_coefficients(agents, results, l2_lambda=l2_lambda)

        # Convergence
        if it > 0 and abs(ll - prev_ll) < tol * max(abs(prev_ll), 1.0):
            fit.converged = True
            if verbose:
                print(f"[em] converged at iter {it}  |Δ log L| < tol")
            break
        prev_ll = ll

    return fit
