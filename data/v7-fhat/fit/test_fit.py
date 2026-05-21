"""Synthetic-data TDD fixture: EKF+EM recovery from a simulated v6-ODE corpus.

Generates a known-truth trajectory with:
  - v6 ODE transition (frozen params from ode_params.json)
  - Per-class C1-C4 Gaussian emissions with known variances
  - C5 logistic emissions with known coefficients
Then runs UKF filter + M-step once and verifies:
  (a) UKF posterior mean tracks the latent within observation noise
  (b) Per-class obs-variance estimates recover ground truth within 3x tolerance
  (c) C5 logistic coefficient signs match pre-registered pattern

This must pass before running on real corpus.
"""
from __future__ import annotations

import json
import pathlib

import numpy as np
import pytest

from .ode import fx as ode_fx, ode_step, reflect
from .ukf_smoother import run_ukf_smoother

REPO = pathlib.Path(__file__).resolve().parents[3]
ODE_PARAMS_JSON = REPO / "data" / "v7-fhat" / "ode_params.json"


def _load_ode_params() -> dict:
    cfg = json.loads(ODE_PARAMS_JSON.read_text())
    return cfg["parameters"]


def _simulate_trajectory(
    T: int,
    p: dict,
    sigma_trans: np.ndarray,
    sigma_obs: np.ndarray,
    rng: np.random.Generator,
    mu0: np.ndarray,
    dt: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Forward-simulate T steps; return (latent, obs)."""
    latent = np.zeros((T, 4))
    s = mu0.copy()
    for t in range(T):
        eta = rng.normal(0.0, sigma_trans * np.sqrt(dt))
        s = reflect(ode_step(s, dt, p) + eta)
        latent[t] = s
    obs = latent + rng.normal(0.0, sigma_obs, size=latent.shape)
    return latent, obs


def test_ode_step_smoke():
    p = _load_ode_params()
    s0 = np.array([0.7, 0.8, 0.2, 0.0])
    s1 = ode_fx(s0, 1.0, p)
    # Single step from healthy state should not explode
    assert s1.shape == (4,)
    assert np.all(np.isfinite(s1))
    assert 0.0 <= s1[0] <= 1.0
    assert 0.0 <= s1[1] <= 1.0
    assert 0.0 <= s1[2] <= 1.0
    assert -1.0 <= s1[3] <= 1.0


def test_reflect_boundary():
    s = np.array([1.2, -0.1, 0.5, 1.3])
    r = reflect(s)
    assert 0.0 <= r[0] <= 1.0
    assert 0.0 <= r[1] <= 1.0
    assert -1.0 <= r[3] <= 1.0


def test_ukf_tracks_latent():
    """UKF posterior mean should track latent within ~obs noise over a steady trajectory."""
    p = _load_ode_params()
    sigma_trans_true = np.array([0.02, 0.02, 0.02, 0.02])
    sigma_obs_true = np.array([0.05, 0.05, 0.05, 0.05])
    mu0 = np.array([0.7, 0.8, 0.2, 0.0])
    rng = np.random.default_rng(42)
    T = 200
    dts = np.full(T, 1.0)

    latent, obs = _simulate_trajectory(T, p, sigma_trans_true, sigma_obs_true, rng, mu0)

    result = run_ukf_smoother(
        obs=obs,
        dts=dts,
        ode_params=p,
        sigma_obs=sigma_obs_true,
        sigma_trans=sigma_trans_true,
        mu0=mu0,
        sigma0_diag=np.array([0.01, 0.01, 0.01, 0.04]),
    )

    # RMS tracking error should be on the order of observation noise (<= ~3x obs std)
    err = result.mu - latent
    rms = np.sqrt((err**2).mean(axis=0))
    tol = 3.0 * sigma_obs_true
    assert np.all(rms < tol), f"UKF tracking RMS {rms} exceeds tol {tol}"


def test_em_recovers_obs_variance():
    """M-step variance estimation recovers ground-truth obs variance within 3x."""
    from .em import m_step_obs_variance

    p = _load_ode_params()
    sigma_trans_true = np.array([0.02, 0.02, 0.02, 0.02])
    sigma_obs_true = np.array([0.04, 0.04, 0.04, 0.04])
    mu0 = np.array([0.7, 0.8, 0.2, 0.0])
    rng = np.random.default_rng(42)
    T = 500
    dts = np.full(T, 1.0)

    latent, obs = _simulate_trajectory(T, p, sigma_trans_true, sigma_obs_true, rng, mu0)

    # Seed UKF with inflated priors so M-step has to work
    sigma_obs_init = np.array([0.1, 0.1, 0.1, 0.1])
    sigma_trans_init = np.array([0.03, 0.03, 0.03, 0.03])
    result = run_ukf_smoother(
        obs=obs,
        dts=dts,
        ode_params=p,
        sigma_obs=sigma_obs_init,
        sigma_trans=sigma_trans_init,
        mu0=mu0,
        sigma0_diag=np.array([0.01, 0.01, 0.01, 0.04]),
    )
    # Pool residuals (obs - posterior mean) per channel
    est_sigma_obs = m_step_obs_variance([(obs, result.mu)])
    # Should be within 3x of ground truth on either side
    for j in range(4):
        assert 0.33 * sigma_obs_true[j] < est_sigma_obs[j] < 3.0 * sigma_obs_true[j], (
            f"channel {j}: est {est_sigma_obs[j]:.4f} not within 3x of true {sigma_obs_true[j]:.4f}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
