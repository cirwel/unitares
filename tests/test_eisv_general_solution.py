"""Tests for the reduced 3-state general solution (scripts/analysis).

Fast, deterministic subset of the module's own verification suite; the full
suite (with larger Monte-Carlo budgets) runs via
``python scripts/analysis/eisv_general_solution.py``.
"""

import math

import numpy as np
import pytest

from scripts.analysis.eisv_general_solution import (
    ALPHA_C,
    CERTIFICATE_PARAMS,
    DEPLOYED_PARAMS,
    Inputs,
    certificate_grid_margin,
    coherence_moments,
    equilibrium,
    equilibrium_sensitivity,
    generic_contraction_ball,
    mean_square_ball,
    rhs,
    solve_state,
    stationary_covariance,
    tracking_bound_rate,
    verify_closed_forms,
    verify_coherence_mgf,
    verify_lyapunov,
    verify_tracking_bound,
)


def test_equilibrium_matches_proposal_worked_example():
    # Rederivation proposal section 3.3: I_bar = 0.8 -> I* = 0.74 at S* = 0.15.
    r = Inputs(u=0.2, c=0.4, I_bar=0.8)
    E_star, I_star, S_star = equilibrium(CERTIFICATE_PARAMS, r)
    assert S_star == pytest.approx(0.15)
    assert I_star == pytest.approx(0.74)
    # Equilibrium is a fixed point of the vector field.
    assert np.allclose(rhs(np.array([E_star, I_star, S_star]), CERTIFICATE_PARAMS, r), 0.0, atol=1e-14)


def test_equilibrium_is_per_agent_not_fleet_constant():
    # Individuality axiom: I* moves 1:1 with the agent's own baseline.
    for i_bar, expected in ((0.5, 0.44), (0.8, 0.74), (0.9, 0.84)):
        r = Inputs(u=0.2, c=0.4, I_bar=i_bar)
        assert equilibrium(CERTIFICATE_PARAMS, r)[1] == pytest.approx(expected)


def test_closed_forms_match_numerical_integration():
    assert verify_closed_forms(seed=7, n_draws=2) < 1e-6


def test_equilibrium_sensitivity_matches_finite_differences():
    r = Inputs(u=0.2, c=0.4, I_bar=0.8)
    sens = equilibrium_sensitivity(CERTIFICATE_PARAMS, r)
    h = 1e-7
    for j, bumped in enumerate(
        (
            Inputs(u=r.u, c=r.c, I_bar=r.I_bar + h),
            Inputs(u=r.u + h, c=r.c, I_bar=r.I_bar),
            Inputs(u=r.u, c=r.c + h, I_bar=r.I_bar),
        )
    ):
        fd = (equilibrium(CERTIFICATE_PARAMS, bumped) - equilibrium(CERTIFICATE_PARAMS, r)) / h
        assert np.allclose(sens[:, j], fd, atol=1e-5)


@pytest.mark.parametrize("params", [CERTIFICATE_PARAMS, DEPLOYED_PARAMS])
def test_stationary_covariance_solves_lyapunov(params):
    assert verify_lyapunov(params, Inputs(), sigma_S=0.05) < 1e-14
    P = stationary_covariance(params, Inputs(), sigma_S=0.05)
    assert np.all(np.linalg.eigvalsh(P) > 0.0)


def test_exact_ball_is_tighter_than_generic_bound():
    sigma = 0.05
    P = stationary_covariance(CERTIFICATE_PARAMS, Inputs(), sigma)
    assert mean_square_ball(P) < generic_contraction_ball(sigma)


def test_coherence_moments_match_monte_carlo():
    err = verify_coherence_mgf(CERTIFICATE_PARAMS, Inputs(), n_samples=200_000)
    assert err < 0.03


def test_coherence_nis_normalization_gives_expected_d2():
    # With W = diag(1 / P_jj) and unbiased baseline, E[D^2] = tr(W P) = d = 3.
    P = stationary_covariance(CERTIFICATE_PARAMS, Inputs(), sigma_S=0.05)
    W = np.diag(1.0 / np.diag(P))
    assert float(np.trace(W @ P)) == pytest.approx(3.0)
    mean_c, sd_c = coherence_moments(np.zeros(3), P, W)
    # The repaired signal has real dynamic range by construction.
    assert 0.1 < mean_c < 0.9
    assert sd_c > 0.05


@pytest.mark.parametrize("params", [CERTIFICATE_PARAMS, DEPLOYED_PARAMS])
def test_contraction_certificate_grid(params):
    assert certificate_grid_margin(params) <= 0.0


def test_tracking_bound_holds_under_baseline_drift():
    obs, bound = verify_tracking_bound(
        CERTIFICATE_PARAMS, Inputs(), drift_rate=0.004, t_end=80.0, dt=0.01
    )
    assert obs <= bound


def test_tracking_bound_rate_formula():
    # nu / alpha_c with nu = ||M^{1/2} dx*/dI_bar|| * eps.
    r = Inputs()
    sens = equilibrium_sensitivity(CERTIFICATE_PARAMS, r)[:, 0]
    expected = math.sqrt(0.1 * sens[0] ** 2 + 1.0 * sens[1] ** 2 + 0.2 * sens[2] ** 2)
    assert tracking_bound_rate(CERTIFICATE_PARAMS, r, 1.0) == pytest.approx(
        expected / ALPHA_C
    )
