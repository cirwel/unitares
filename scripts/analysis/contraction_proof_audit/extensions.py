#!/usr/bin/env python3
"""Exact scalar checks for selected v6 extension claims; no live data.

Reviewed source: unitares-paper-v6 commit
8a888b30fcca783ba76a83334ca85ba287fc7c09, unitares-v6.tex.

Uses closed-form arithmetic, not simulation. The scalar OU example embeds
in four dimensions by adding independent deterministic contracting channels.
The network example uses four-dimensional states and H=-I_4; each coordinate
has the same scalar disagreement equation. These counterexamples test the
stated general claims; they do not simulate the UNITARES implementation or
assert that its configured numerical integrator is unstable.
"""

import json
import math


def close(actual, expected):
    assert math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-14), (
        actual,
        expected,
    )


def main():
    alpha = 0.019
    sigma = 0.02
    time = 100.0
    diffusion_bound = sigma**2
    # dx_i=-alpha*x_i*dt+sigma*dW_i; independent Wiener processes;
    # x_1(0)=x_2(0)=0. The two variances add exactly.
    single_variance = diffusion_bound / (2 * alpha) * (1 - math.exp(-2 * alpha * time))
    independent_pair_ms = 2 * single_variance
    claimed_pair_bound = diffusion_bound / (2 * alpha)
    pair_asymptotic_ms = diffusion_bound / alpha
    assert independent_pair_ms > claimed_pair_bound
    close(independent_pair_ms, 0.020581667960922832)
    close(pair_asymptotic_ms, 2 * claimed_pair_bound)
    rms_claimed = math.sqrt(claimed_pair_bound)
    rms_pair = math.sqrt(pair_asymptotic_ms)
    close(rms_claimed, 0.10259783520851541)
    close(rms_pair, 0.14509525002200233)
    # Even the smaller RMS upper bound is not tenfold below .1 or .3.
    # This rejects the inference from the bound, not a possible lower
    # actual noise level in another system.
    assert all(rms_claimed > excursion / 10 for excursion in (0.1, 0.3))

    # Two-node undirected graph L=[[1,-1],[-1,1]], isolated f(x)=-x,
    # H=-I_4, epsilon=1. Isolated metric M=I_4 and rate alpha_network=1.
    # d=x_1-x_2 obeys d'=(-alpha_network-2*epsilon*h)*d=+d.
    alpha_network, epsilon, h = 1.0, 1.0, -1.0
    laplacian_largest_eigenvalue = 2.0
    network_condition_rhs = laplacian_largest_eigenvalue * h
    disagreement_exponent = -alpha_network - 2 * epsilon * h
    assert alpha_network > network_condition_rhs
    assert disagreement_exponent > 0
    close(disagreement_exponent, 1.0)

    gamma = 0.25
    jacobian_values = [-gamma * (1 - 2 * integrity) for integrity in (0, 0.5, 1)]
    assert jacobian_values == [-0.25, 0.0, 0.25]
    forcing = 0.03
    margin = 1 - 4 * forcing / gamma
    lower_root = 0.5 - 0.5 * math.sqrt(margin)
    upper_root = 0.5 + 0.5 * math.sqrt(margin)
    initial_integrity = 0.95
    initial_derivative = forcing - gamma * initial_integrity * (1 - initial_integrity)
    assert margin > 0
    assert initial_integrity > upper_root
    assert initial_derivative > 0
    # f'(I)>0 throughout [.95,1], hence the derivative stays positive
    # and bounded below by its initial value until the upper clamp.
    time_to_boundary_upper_bound = (1 - initial_integrity) / initial_derivative
    close(upper_root, 0.860555127546399)
    close(initial_derivative, 0.018125)
    assert time_to_boundary_upper_bound > 0

    # At zero margin, f(I)=gamma*(I-.5)^2. For I(0)<.5, write
    # z=.5-I: z'=-gamma*z^2 and I(t)=.5-z0/(1+gamma*z0*t).
    zero_margin_forcing = gamma / 4
    zero_margin_initial_integrity = 0.25
    z0 = 0.5 - zero_margin_initial_integrity
    zero_margin_integrity_at_100 = 0.5 - z0 / (1 + gamma * z0 * 100)
    close(1 - 4 * zero_margin_forcing / gamma, 0)
    assert zero_margin_initial_integrity < zero_margin_integrity_at_100 < 0.5

    # Exact continuous flow x'=-x contracts. A finite Euler step followed
    # by convex-box projection does not inherit that property for all dt.
    dt, x, y = 3.0, 0.1, 0.0
    clip = lambda value: max(-1.0, min(1.0, value))
    projected_x, projected_y = clip(x - dt * x), clip(y - dt * y)
    initial_distance = abs(x - y)
    projected_distance = abs(projected_x - projected_y)
    close(projected_distance / initial_distance, 2.0)
    assert projected_distance > initial_distance

    result = {
        "source_commit": "8a888b30fcca783ba76a83334ca85ba287fc7c09",
        "method": "closed-form deterministic checks; no simulation or live data",
        "ou_independent_pair": {
            "source_lines": "1447-1463",
            "alpha": alpha,
            "sigma": sigma,
            "time": time,
            "initial_states": [0, 0],
            "single_trajectory_variance": single_variance,
            "exact_pair_mean_square": independent_pair_ms,
            "paper_pair_upper_bound": claimed_pair_bound,
            "violates_paper_bound": independent_pair_ms > claimed_pair_bound,
            "pair_asymptotic_mean_square": pair_asymptotic_ms,
            "asymptotic_factor_relative_to_paper": 2,
        },
        "rms_comparison": {
            "source_lines": "1481-1495",
            "sqrt_paper_bound": rms_claimed,
            "sqrt_independent_pair_bound": rms_pair,
            "claimed_signal_amplitudes": [0.1, 0.3],
            "supports_tenfold_amplitude_separation": False,
            "qualification": "An upper bound does not establish actual noise magnitude or decision crossings.",
        },
        "network": {
            "source_lines": "1522-1543",
            "isolated_dynamics": "f(x)=-x in R^4; M=I_4",
            "laplacian": [[1, -1], [-1, 1]],
            "coupling_matrix": "H=-I_4",
            "epsilon": epsilon,
            "alpha": alpha_network,
            "condition_rhs": network_condition_rhs,
            "paper_condition_holds": alpha_network > network_condition_rhs,
            "disagreement_exponent": disagreement_exponent,
            "synchronizes": False,
        },
        "logistic": {
            "source_lines": "2978-3001",
            "gamma": gamma,
            "jacobian_at_I_0_half_1": jacobian_values,
            "positive_margin_counterexample": {
                "forcing": forcing,
                "margin": margin,
                "equilibria": [lower_root, upper_root],
                "initial_I": initial_integrity,
                "initial_derivative": initial_derivative,
                "time_to_upper_boundary_at_most": time_to_boundary_upper_bound,
                "qualification": "Constant forcing; projected dynamics stay at upper clamp after contact.",
            },
            "zero_margin_counterexample": {
                "forcing": zero_margin_forcing,
                "initial_I": zero_margin_initial_integrity,
                "I_at_t100": zero_margin_integrity_at_100,
                "limiting_I": 0.5,
                "reaches_upper_boundary": False,
            },
        },
        "projected_euler": {
            "source_lines": "3035-3051",
            "continuous_dynamics": "x'=-x",
            "constraint_set": [-1, 1],
            "dt": dt,
            "initial_states": [x, y],
            "projected_next_states": [projected_x, projected_y],
            "initial_distance": initial_distance,
            "next_distance": projected_distance,
            "distance_multiplier": projected_distance / initial_distance,
            "qualification": "Counterexample to unrestricted inference; not a test of deployed dt or RK4.",
        },
        "assertions": "passed",
    }
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
