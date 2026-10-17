"""Numerical regressions for frozen-input integration near stiff barriers."""

from dataclasses import replace
import math

import numpy as np
import pytest
from scipy.integrate import solve_ivp

from governance_core import dynamics as d
from governance_core.parameters import DEFAULT_THETA, DynamicsParams, get_active_params


@pytest.fixture(autouse=True)
def fixed_modes(monkeypatch):
    monkeypatch.setenv("UNITARES_I_DYNAMICS", "linear")
    monkeypatch.setenv("UNITARES_INTEGRATOR", "rk4")
    monkeypatch.setenv("UNITARES_SENSOR_COUPLING", "off")
    monkeypatch.setenv("UNITARES_PARAMS_PROFILE", "default")
    monkeypatch.setenv("UNITARES_PARAMS_JSON", "{}")


def vector(state):
    return np.array([state.E, state.I, state.S, state.V])


@pytest.mark.parametrize("mode", ["rk4", "euler"])
@pytest.mark.parametrize("dt", [0.1, 0.25, 0.5, 1.0])
def test_stiff_integrity_witness_no_longer_amplifies(mode, dt, monkeypatch):
    """The pre-fix default RK4 map amplified this pair 4.6x at dt=.1."""
    monkeypatch.setenv("UNITARES_INTEGRATOR", mode)
    params = get_active_params()
    x = d.State(0.5, 0.999, 0.3, 0.0)
    y = replace(x, I=x.I + 1e-7)
    weight = np.array([0.1, 0.2, 1.0, 0.08])
    outputs = [d.compute_dynamics(s, [], DEFAULT_THETA, params, dt) for s in (x, y)]
    before = np.sum(weight * (vector(x) - vector(y)) ** 2)
    after = np.sum(weight * (vector(outputs[0]) - vector(outputs[1])) ** 2)
    assert after < before


@pytest.mark.parametrize("mode,tolerance", [("rk4", 2e-5), ("euler", 5e-3)])
@pytest.mark.parametrize("dt", [0.1, 1.0])
@pytest.mark.parametrize(
    "axis,near_bound",
    [
        (0, 0.001),
        (0, 0.999),
        (1, 0.001),
        (1, 0.999),
        (2, 0.0011),
        (2, 0.999),
        (3, -0.999),
        (3, 0.999),
    ],
)
def test_boundary_steps_agree_with_independent_implicit_solver(
    mode,
    tolerance,
    dt,
    axis,
    near_bound,
    monkeypatch,
):
    """Check all eight boundary neighborhoods, not only the original pair."""
    monkeypatch.setenv("UNITARES_INTEGRATOR", mode)
    params = get_active_params()
    initial = np.array([0.5, 0.5, 0.3, 0.0])
    initial[axis] = near_bound
    solution = solve_ivp(
        lambda t, y: d._derivatives(
            d.State(*y),
            0.0,
            DEFAULT_THETA,
            params,
            0.0,
            0.5,
            None,
        ),
        (0, dt),
        initial,
        method="Radau",
        rtol=1e-10,
        atol=1e-12,
    )
    assert solution.success
    # Reference trajectories stay in the box, so no projected-flow ambiguity.
    lower = np.array([params.E_min, params.I_min, params.S_min, params.V_min])
    upper = np.array([params.E_max, params.I_max, params.S_max, params.V_max])
    assert np.all(solution.y >= lower[:, None])
    assert np.all(solution.y <= upper[:, None])
    reference = solution.y[:, -1].copy()
    reference[2] = max(reference[2], params.S_min + 0.049 * 0.5)
    result = d.compute_dynamics(d.State(*initial), [], DEFAULT_THETA, params, dt)
    np.testing.assert_allclose(vector(result), reference, rtol=0, atol=tolerance)


def test_full_elapsed_time_and_single_final_entropy_floor():
    """S decays below its final floor; applying that floor per substep changes I."""
    params = DynamicsParams(
        alpha=10,
        beta_E=0,
        beta_I=0,
        gamma_I=0,
        k=1,
        mu=1,
        Cmax=0,
        beta_complexity=0,
        barrier_strength=0,
    )
    result = d.compute_dynamics(
        d.State(0.5, 0.8, 0.01, 0),
        [],
        DEFAULT_THETA,
        params,
        dt=1,
        complexity=1,
    )
    assert result.I == pytest.approx(0.8 - 0.01 * (1 - math.exp(-1)), abs=1e-8)
    assert result.S == pytest.approx(params.S_min + 0.049)


@pytest.mark.parametrize("mode", ["linear", "logistic"])
def test_global_bound_covers_custom_box_overlap_and_sensor(mode, monkeypatch):
    """Numerical derivative checks exercise terms absent in the original proof."""
    monkeypatch.setenv("UNITARES_I_DYNAMICS", mode)
    params = DynamicsParams(
        E_min=-0.2,
        E_max=0.3,
        I_min=-0.1,
        I_max=1.4,
        S_min=0.01,
        S_max=0.8,
        V_min=-2,
        V_max=3,
        barrier_margin=0.8,
        barrier_strength=3,
        k_anchor=4,
    )
    theta = replace(DEFAULT_THETA, C1=1.5, eta2=0.45)
    dt = 0.4
    h = dt / d._integration_substeps(params, theta, dt, True, 0.2)
    rng = np.random.default_rng(12)
    lo = np.array([params.E_min, params.I_min, params.S_min, params.V_min])
    hi = np.array([params.E_max, params.I_max, params.S_max, params.V_max])
    sensor = d.State(0.9, 0.8, 2, 2)  # Fixed forcing can lie outside the box.
    for state in rng.uniform(lo + 1e-5, hi - 1e-5, size=(20, 4)):

        def rhs(values):
            return np.array(
                d._derivatives(
                    d.State(*values),
                    0.2,
                    theta,
                    params,
                    0.1,
                    0.8,
                    sensor,
                    0.2,
                )
            )

        jacobian = np.column_stack(
            [
                (rhs(state + delta) - rhs(state - delta)) / 2e-6
                for delta in np.eye(4) * 1e-6
            ]
        )
        assert h * np.linalg.norm(jacobian, ord=np.inf) <= 1


def _jacobian_inf_norm(state, theta, params, s_setpoint):
    def rhs(values):
        return np.array(
            d._derivatives(d.State(*values), 0.0, theta, params, 0.0, 0.5, None, s_setpoint)
        )

    return np.linalg.norm(
        np.column_stack(
            [(rhs(state + delta) - rhs(state - delta)) / 2e-6 for delta in np.eye(4) * 1e-6]
        ),
        ord=np.inf,
    )


@pytest.mark.parametrize("s_setpoint", [0.0, 0.2, 0.9, -0.5])
def test_bound_covers_the_shifted_s_term(s_setpoint):
    """The E row reads S - σ (#2834), so the bound must too.

    Counterexample from adversarial design review: with this box and σ = 0.9
    the raw-S bound chose two substeps while h·L reached 4.45 at S = 0.01.
    """
    params = DynamicsParams(
        E_min=0.0, E_max=0.1, I_min=0.0, I_max=1.0, S_min=0.001, S_max=0.1,
        V_min=-1.0, V_max=1.0, alpha=0.0, beta_E=100.0, barrier_strength=0.0,
        barrier_margin=0.0,
    )
    dt = 0.1
    h = dt / d._integration_substeps(params, DEFAULT_THETA, dt, False, s_setpoint)
    rng = np.random.default_rng(7)
    lo = np.array([params.E_min, params.I_min, params.S_min, params.V_min])
    hi = np.array([params.E_max, params.I_max, params.S_max, params.V_max])
    states = list(rng.uniform(lo + 1e-5, hi - 1e-5, size=(20, 4)))
    states.append(np.array([0.05, 0.5, 0.01, 0.0]))
    for state in states:
        assert h * _jacobian_inf_norm(state, DEFAULT_THETA, params, s_setpoint) <= 1


def test_compute_dynamics_passes_the_setpoint_to_the_bound(monkeypatch):
    seen = []
    real = d._integration_substeps

    def spy(*args):
        seen.append(args)
        return real(*args)

    monkeypatch.setattr(d, "_integration_substeps", spy)
    d.compute_dynamics(d.DEFAULT_STATE, [], DEFAULT_THETA, get_active_params(), s_setpoint=0.3)
    assert seen and seen[0][4] == 0.3


def test_rejects_a_non_finite_setpoint():
    with pytest.raises(ValueError, match="s_setpoint must be finite"):
        d._integration_substeps(get_active_params(), DEFAULT_THETA, 0.1, False, math.nan)


def test_projects_initial_state_before_first_derivative():
    params = get_active_params()
    outside = d.State(-3, 5, -1, 4)
    projected = d.State(params.E_min, params.I_max, params.S_min, params.V_max)
    assert d.compute_dynamics(outside, [], DEFAULT_THETA, params) == (
        d.compute_dynamics(projected, [], DEFAULT_THETA, params)
    )


@pytest.mark.parametrize("dt", [-0.1, float("nan"), float("inf")])
def test_rejects_invalid_elapsed_time(dt):
    with pytest.raises(ValueError, match="dt must be"):
        d.compute_dynamics(d.DEFAULT_STATE, [], DEFAULT_THETA, get_active_params(), dt)


def test_rejects_excessive_work_instead_of_loosening_step_bound():
    params = replace(get_active_params(), barrier_strength=1e8)
    with pytest.raises(ValueError, match="more than 10000 substeps"):
        d.compute_dynamics(d.DEFAULT_STATE, [], DEFAULT_THETA, params)


def test_zero_time_and_disabled_barrier():
    params = replace(get_active_params(), barrier_strength=0, barrier_margin=0)
    assert (
        d.compute_dynamics(d.DEFAULT_STATE, [], DEFAULT_THETA, params, dt=0)
        == d.DEFAULT_STATE
    )
    below_floor = replace(d.DEFAULT_STATE, S=0.01)
    result = d.compute_dynamics(
        below_floor, [], DEFAULT_THETA, params, dt=0, complexity=1
    )
    assert result == replace(below_floor, S=params.S_min + 0.049)


@pytest.mark.parametrize("delta", [0.15, 0.25, 0.4, 0.5])
def test_preserves_sensor_drift_noise_and_setpoint_for_whole_interval(
    delta, monkeypatch
):
    monkeypatch.setenv("UNITARES_SENSOR_COUPLING", "on")
    params = replace(get_active_params(), delta=delta)
    sensor = d.State(0.6, 0.8, 0.2, -0.2)
    initial = d.State(0.5, 0.999, 0.3, 0)
    solution = solve_ivp(
        lambda t, y: d._derivatives(
            d.State(*y),
            0.25,
            DEFAULT_THETA,
            params,
            0.02,
            0.8,
            sensor,
            0.2,
        ),
        (0, 1),
        vector(initial),
        method="Radau",
        rtol=1e-10,
        atol=1e-12,
    )
    assert solution.success
    result = d.compute_dynamics(
        initial,
        [0.3, 0.4],
        DEFAULT_THETA,
        params,
        dt=1,
        noise_S=0.02,
        complexity=0.8,
        sensor_eisv=sensor,
        s_setpoint=0.2,
    )
    np.testing.assert_allclose(vector(result), solution.y[:, -1], rtol=0, atol=2e-5)


@pytest.mark.parametrize(
    "changes",
    [
        {"S_max": 0.001},
        {"I_min": float("nan")},
        {"barrier_margin": -0.1},
        {"mu": float("inf")},
    ],
)
def test_rejects_invalid_slope_bound_parameters(changes):
    with pytest.raises(ValueError):
        d.compute_dynamics(
            d.DEFAULT_STATE, [], DEFAULT_THETA, replace(get_active_params(), **changes)
        )
