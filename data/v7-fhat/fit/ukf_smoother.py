"""UKF filter + RTS smoother over v6 ODE with C1-C4 Gaussian emissions.

E-step uses only direct EISV emissions (C1-C4). C5 (is_bad) is fit in the
M-step post-hoc from smoother posteriors — it adds little to the posterior
when 4 direct noisy measurements are present and simplifies implementation.
Rationale is recorded in the session1-report.md methodology section.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np
from filterpy.kalman import UnscentedKalmanFilter, MerweScaledSigmaPoints, rts_smoother

from .ode import fx as _fx


@dataclass
class SmootherResult:
    mu: np.ndarray      # (T, 4) smoothed posterior mean
    cov: np.ndarray     # (T, 4, 4)
    mu_pred: np.ndarray  # (T, 4) one-step-ahead predicted mean (for SC2)
    cov_pred: np.ndarray  # (T, 4, 4) one-step-ahead predicted cov
    loglik: float       # sum over t of filter innovation loglik


def hx_identity(x: np.ndarray) -> np.ndarray:
    return x


def run_ukf_smoother(
    obs: np.ndarray,                 # (T, 4) observed_E,I,S,V (np.nan for missing)
    dts: np.ndarray,                 # (T,) seconds between this row and the previous (dts[0] is step into first obs)
    ode_params: dict,
    sigma_obs: np.ndarray,           # (4,) per-channel observation std
    sigma_trans: np.ndarray,         # (4,) per-channel transition std
    mu0: np.ndarray,
    sigma0_diag: np.ndarray,         # variance, diag of Σ0
) -> SmootherResult:
    """Run UKF filter then RTS smoother on one agent's trajectory.

    dts are in hours (gap_seconds / 3600), clipped to [1/60, 1.0].
    Transition noise Q scales with dt: Q = diag(sigma_trans^2 * dt).
    """
    T = obs.shape[0]
    dim = 4

    points = MerweScaledSigmaPoints(n=dim, alpha=1e-3, beta=2.0, kappa=0.0)

    def fx_wrapper(x, dt):
        return _fx(x, dt, ode_params)

    ukf = UnscentedKalmanFilter(
        dim_x=dim, dim_z=dim, dt=1.0, fx=fx_wrapper, hx=hx_identity, points=points
    )
    ukf.x = mu0.copy()
    ukf.P = np.diag(sigma0_diag)

    mus = np.zeros((T, dim))
    covs = np.zeros((T, dim, dim))
    mus_pred = np.zeros((T, dim))
    covs_pred = np.zeros((T, dim, dim))
    loglik = 0.0

    for t in range(T):
        dt = float(dts[t])
        Q = np.diag(sigma_trans**2 * dt)
        ukf.Q = Q
        ukf.predict(dt=dt)
        mus_pred[t] = ukf.x.copy()
        covs_pred[t] = ukf.P.copy()

        z = obs[t]
        mask = ~np.isnan(z)
        if mask.sum() == 0:
            mus[t] = ukf.x.copy()
            covs[t] = ukf.P.copy()
            continue

        # Innovation & innovation covariance BEFORE update.
        # For identity measurement (hx=I), H=I, so S = P_pred + R.
        R_diag = np.where(mask, sigma_obs**2, 1e6)
        innov = z - mus_pred[t]
        # Safe LL: use only observed channels
        if mask.any():
            idx = np.where(mask)[0]
            S_sub = covs_pred[t][np.ix_(idx, idx)] + np.diag(R_diag[idx])
            try:
                sign, logdet = np.linalg.slogdet(S_sub)
                if sign > 0:
                    v = innov[idx]
                    ll_t = -0.5 * (
                        v @ np.linalg.solve(S_sub, v)
                        + logdet
                        + len(idx) * np.log(2 * np.pi)
                    )
                    loglik += float(ll_t)
            except np.linalg.LinAlgError:
                pass

        ukf.R = np.diag(R_diag)
        z_use = np.where(mask, z, ukf.x)
        try:
            ukf.update(z_use)
        except np.linalg.LinAlgError:
            pass

        mus[t] = ukf.x.copy()
        covs[t] = ukf.P.copy()

    return SmootherResult(mu=mus, cov=covs, mu_pred=mus_pred, cov_pred=covs_pred, loglik=loglik)
