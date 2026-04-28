"""
Tests for analysis scripts (basin estimation, contraction analysis, compositionality).

Pure-math tests -- no database, no network, no async.
"""

import math
import sys
from pathlib import Path

import numpy as np
import pytest

# Add project and scripts to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts" / "analysis"))

from governance_core.dynamics import compute_dynamics, State, check_basin
from governance_core.parameters import (
    DynamicsParams, Theta, DEFAULT_PARAMS, DEFAULT_THETA,
    get_active_params, get_i_dynamics_mode,
)
from governance_core.coherence import coherence

# Import analysis modules (conditional — governance_core builds may lack _derivatives)
from basin_estimation import (
    integrate_trajectory,
    classify_trajectory,
    generate_perturbations,
    run_basin_estimation,
    state_to_vec,
    vec_to_state,
)
try:
    from contraction_analysis import (
        numerical_jacobian,
        analytical_jacobian,
        check_contraction,
        gershgorin_bound,
        optimize_metric,
        compute_rhs,
    )
    HAS_CONTRACTION = True
except ImportError:
    HAS_CONTRACTION = False

from compositionality_metrics import (
    levenshtein_distance,
    generate_synthetic_data,
    compute_meaning_distances,
    compute_signal_distances,
    topographic_similarity,
    region_consistency,
)

try:
    from hypothesis import HealthCheck, given, strategies as st, settings
    HAS_HYPOTHESIS = True
except ImportError:
    HAS_HYPOTHESIS = False


# -----------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------

@pytest.fixture
def active_params():
    return get_active_params()


@pytest.fixture
def equilibrium(active_params):
    from governance_core.dynamics import compute_equilibrium
    return compute_equilibrium(active_params, DEFAULT_THETA)


# -----------------------------------------------------------------------
# Basin Estimation Tests
# -----------------------------------------------------------------------

class TestBasinEstimation:

    def test_equilibrium_matches_numerical_integration(self, active_params, equilibrium):
        """Verify linear-mode equilibrium matches numerical integration.

        Tolerance is 0.07 because the analytical equilibrium assumes V*=0 exactly,
        but the true fixed point has V* = kappa*(E*-I*)/delta < 0 (since E*<I*).
        With C1=3.0 the coherence function amplifies this: C(V*) departs from 0.5,
        shifting E* and I* away from the V*=0 approximation.
        The complexity floor also adds a nonlinearity not captured analytically.
        """
        state = State(E=0.7, I=0.8, S=0.2, V=0.0)
        for _ in range(5000):
            state = compute_dynamics(state, [0.0] * 5, DEFAULT_THETA,
                                     active_params, dt=0.1)

        assert abs(equilibrium.E - state.E) < 0.07
        assert abs(equilibrium.I - state.I) < 0.07
        assert abs(equilibrium.S - state.S) < 0.02
        assert abs(equilibrium.V - state.V) < 0.03

    def test_equilibrium_E_not_equal_I(self, equilibrium):
        """Linear mode E* != I* (E* = alpha*I*/(alpha + beta_E*S*))."""
        # E* should be slightly less than I* due to E-S cross-coupling
        assert equilibrium.E < equilibrium.I
        assert equilibrium.E > 0.7
        assert equilibrium.I > 0.7

    def test_integrate_trajectory_preserves_bounds(self, active_params):
        """All states in trajectory should be in bounds."""
        initial = State(E=0.5, I=0.3, S=1.0, V=0.5)
        traj = integrate_trajectory(initial, active_params, DEFAULT_THETA,
                                    [0.0] * 5, n_steps=50)

        for s in traj:
            assert 0.0 <= s.E <= 1.0
            assert 0.0 <= s.I <= 1.0
            assert 0.0 <= s.S <= 1.0
            assert -1.0 <= s.V <= 1.0

    def test_integrate_trajectory_length(self, active_params, equilibrium):
        """Trajectory should have n_steps + 1 states."""
        traj = integrate_trajectory(equilibrium, active_params, DEFAULT_THETA,
                                    [0.0] * 5, n_steps=10)
        assert len(traj) == 11

    def test_classify_convergent_trajectory(self, active_params, equilibrium):
        """Small perturbation from equilibrium should converge."""
        initial = State(
            E=equilibrium.E + 0.01,
            I=equilibrium.I - 0.01,
            S=equilibrium.S + 0.01,
            V=0.01,
        )
        traj = integrate_trajectory(initial, active_params, DEFAULT_THETA,
                                    [0.0] * 5, n_steps=500)
        result = classify_trajectory(traj, equilibrium, epsilon=0.05)

        assert result['classification'] == 'convergent'
        assert result['min_distance'] < 0.05

    def test_generate_perturbations_shape(self, equilibrium):
        perturbs = generate_perturbations(equilibrium, n_samples=100, seed=42)
        assert perturbs.shape == (100, 4)

    def test_generate_perturbations_in_bounds(self, equilibrium):
        perturbs = generate_perturbations(equilibrium, n_samples=1000, seed=42)

        assert np.all(perturbs[:, 0] >= 0.0) and np.all(perturbs[:, 0] <= 1.0)
        assert np.all(perturbs[:, 1] >= 0.0) and np.all(perturbs[:, 1] <= 1.0)
        assert np.all(perturbs[:, 2] >= 0.001) and np.all(perturbs[:, 2] <= 2.0)
        assert np.all(perturbs[:, 3] >= -2.0) and np.all(perturbs[:, 3] <= 2.0)

    def test_generate_perturbations_reproducible(self, equilibrium):
        p1 = generate_perturbations(equilibrium, n_samples=50, seed=123)
        p2 = generate_perturbations(equilibrium, n_samples=50, seed=123)
        np.testing.assert_array_equal(p1, p2)

    def test_run_basin_estimation_small(self):
        """End-to-end with small sample size."""
        results = run_basin_estimation(
            n_samples=20, n_steps=200, epsilon=0.05, seed=42)

        total = results['n_convergent'] + results['n_divergent'] + results['n_stuck']
        assert total == 20
        assert 0.0 <= results['convergence_fraction'] <= 1.0
        assert results['max_safe_perturbation'] >= 0.0

    def test_state_vec_roundtrip(self):
        """State -> vec -> State should preserve values."""
        s = State(E=0.5, I=0.6, S=0.1, V=-0.3)
        s2 = vec_to_state(state_to_vec(s))
        assert abs(s.E - s2.E) < 1e-10
        assert abs(s.I - s2.I) < 1e-10
        assert abs(s.S - s2.S) < 1e-10
        assert abs(s.V - s2.V) < 1e-10


# -----------------------------------------------------------------------
# Contraction Analysis Tests
# -----------------------------------------------------------------------

@pytest.mark.skipif(not HAS_CONTRACTION, reason="governance_core missing _derivatives export")
class TestContractionAnalysis:

    def test_numerical_jacobian_shape(self, active_params):
        state = State(E=0.83, I=0.85, S=0.06, V=0.0)
        J = numerical_jacobian(state, active_params, DEFAULT_THETA)
        assert J.shape == (4, 4)

    def test_numerical_jacobian_finite(self, active_params):
        state = State(E=0.83, I=0.85, S=0.06, V=0.0)
        J = numerical_jacobian(state, active_params, DEFAULT_THETA)
        assert np.all(np.isfinite(J))

    def test_analytical_matches_numerical(self, active_params, equilibrium):
        """Jacobians should agree within tolerance at equilibrium."""
        J_num = numerical_jacobian(equilibrium, active_params, DEFAULT_THETA)
        J_ana = analytical_jacobian(equilibrium, active_params, DEFAULT_THETA)

        np.testing.assert_allclose(J_num, J_ana, atol=1e-4)

    def test_analytical_matches_numerical_off_equilibrium(self, active_params):
        """Jacobians should agree at a non-equilibrium point too."""
        state = State(E=0.5, I=0.7, S=0.3, V=0.1)
        J_num = numerical_jacobian(state, active_params, DEFAULT_THETA)
        J_ana = analytical_jacobian(state, active_params, DEFAULT_THETA)

        np.testing.assert_allclose(J_num, J_ana, atol=1e-3)

    def test_equilibrium_is_stable(self, active_params, equilibrium):
        """System should be stable at the equilibrium (all eigenvalues have Re < 0).

        Note: with C1=3.0 the system loses strict contraction (symmetric Jacobian
        measure) but retains asymptotic stability. The stronger coherence feedback
        introduces non-symmetric coupling that can cause transient oscillations,
        but all trajectories still converge to the fixed point.
        """
        J = analytical_jacobian(equilibrium, active_params, DEFAULT_THETA)
        eigenvalues = np.linalg.eigvals(J)
        max_real = max(e.real for e in eigenvalues)

        assert max_real < 0, f"Unstable: max real eigenvalue = {max_real:.6f}"
        # Stability margin: eigenvalue should be well away from zero
        assert max_real < -0.01, f"Marginal stability: max real eigenvalue = {max_real:.6f}"

    def test_stability_margin_exceeds_minimum(self, active_params, equilibrium):
        """Stability margin (negative of max real eigenvalue) should be meaningful."""
        J = analytical_jacobian(equilibrium, active_params, DEFAULT_THETA)
        eigenvalues = np.linalg.eigvals(J)
        max_real = max(e.real for e in eigenvalues)

        # With C1=3.0, max real eigenvalue is approximately -0.059
        assert max_real < -0.02, f"Insufficient stability margin: {max_real:.6f}"

    def test_gershgorin_bound_consistent(self, active_params, equilibrium):
        """Gershgorin disks should contain actual eigenvalues."""
        J = analytical_jacobian(equilibrium, active_params, DEFAULT_THETA)
        actual_eigs = np.linalg.eigvals(J)
        gersh = gershgorin_bound(J)

        # Each eigenvalue should lie within at least one Gershgorin disk
        for eig in actual_eigs:
            in_some_disk = False
            for disk in gersh['disks']:
                if abs(eig.real - disk['center']) <= disk['radius'] + 1e-10:
                    in_some_disk = True
                    break
            assert in_some_disk, f"Eigenvalue {eig} not in any Gershgorin disk"

    def test_metric_optimization_improves_rate(self, active_params, equilibrium):
        """Optimized metric should achieve >= initial contraction rate."""
        J = analytical_jacobian(equilibrium, active_params, DEFAULT_THETA)
        result = optimize_metric(J)

        assert result['optimal_contraction_rate'] >= result['initial_rate'] - 1e-6

    def test_check_contraction_with_identity_metric(self, active_params, equilibrium):
        """Identity metric should give same result as bare Jacobian."""
        J = analytical_jacobian(equilibrium, active_params, DEFAULT_THETA)
        bare = check_contraction(J)
        identity = check_contraction(J, np.eye(4))

        np.testing.assert_allclose(
            bare['eigenvalues'], identity['eigenvalues'], atol=1e-10)

    def test_compute_rhs_returns_finite(self, active_params, equilibrium):
        """RHS should always be finite for valid states."""
        rhs = compute_rhs(equilibrium, active_params, DEFAULT_THETA)
        assert rhs.shape == (4,)
        assert np.all(np.isfinite(rhs))

    def test_rhs_near_zero_at_equilibrium(self, active_params, equilibrium):
        """Derivatives should be near zero at equilibrium."""
        rhs = compute_rhs(equilibrium, active_params, DEFAULT_THETA)
        # Not exactly zero due to Euler integration and clipping
        assert np.max(np.abs(rhs)) < 0.01

    @pytest.mark.skipif(not HAS_HYPOTHESIS, reason="hypothesis not installed")
    def test_jacobian_finite_for_any_valid_state(self, active_params):
        """Jacobian should have all finite entries for any valid state."""
        @given(
            st.floats(0.01, 0.99),
            st.floats(0.01, 0.99),
            st.floats(0.01, 1.99),
            st.floats(-1.99, 1.99),
        )
        @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
        def _test(E, I, S, V):
            state = State(E=E, I=I, S=S, V=V)
            J = analytical_jacobian(state, active_params, DEFAULT_THETA)
            assert np.all(np.isfinite(J))

        _test()

    def test_diagonal_elements_negative(self, active_params, equilibrium):
        """All diagonal elements of Jacobian should be negative (self-damping)."""
        J = analytical_jacobian(equilibrium, active_params, DEFAULT_THETA)
        for i in range(4):
            assert J[i, i] < 0, f"J[{i},{i}] = {J[i,i]} is not negative"


# -----------------------------------------------------------------------
# Compositionality Metrics Tests
# -----------------------------------------------------------------------

class TestCompositionality:

    def test_levenshtein_known_cases(self):
        assert levenshtein_distance("", "") == 0
        assert levenshtein_distance("abc", "abc") == 0
        assert levenshtein_distance("abc", "abd") == 1
        assert levenshtein_distance("abc", "") == 3
        assert levenshtein_distance("kitten", "sitting") == 3

    def test_levenshtein_symmetric(self):
        assert levenshtein_distance("hello", "world") == levenshtein_distance("world", "hello")

    def test_synthetic_data_schema(self):
        data = generate_synthetic_data(n_samples=100, seed=42)
        assert len(data) == 100
        for record in data:
            assert 'tokens' in record
            assert 'warmth' in record
            assert 'brightness' in record
            assert 'stability' in record
            assert 'presence' in record
            assert isinstance(record['tokens'], list)
            assert len(record['tokens']) >= 1
            assert 0.0 <= record['warmth'] <= 1.0
            assert 0.0 <= record['brightness'] <= 1.0
            assert 0.0 <= record['stability'] <= 1.0
            assert -1.0 <= record['presence'] <= 1.0

    def test_synthetic_data_reproducible(self):
        d1 = generate_synthetic_data(n_samples=50, seed=123)
        d2 = generate_synthetic_data(n_samples=50, seed=123)
        for r1, r2 in zip(d1, d2):
            assert r1['tokens'] == r2['tokens']
            assert r1['warmth'] == r2['warmth']

    def test_meaning_distances_shape(self):
        data = generate_synthetic_data(n_samples=20, seed=42)
        md = compute_meaning_distances(data)
        assert md.shape == (20, 20)

    def test_meaning_distances_symmetric(self):
        data = generate_synthetic_data(n_samples=20, seed=42)
        md = compute_meaning_distances(data)
        np.testing.assert_allclose(md, md.T)

    def test_meaning_distances_zero_diagonal(self):
        data = generate_synthetic_data(n_samples=20, seed=42)
        md = compute_meaning_distances(data)
        np.testing.assert_allclose(np.diag(md), 0.0, atol=1e-10)

    def test_signal_distances_shape(self):
        data = generate_synthetic_data(n_samples=20, seed=42)
        sd = compute_signal_distances(data)
        assert sd.shape == (20, 20)

    def test_signal_distances_symmetric(self):
        data = generate_synthetic_data(n_samples=20, seed=42)
        sd = compute_signal_distances(data)
        np.testing.assert_allclose(sd, sd.T)

    def test_topographic_similarity_range(self):
        data = generate_synthetic_data(n_samples=100, seed=42)
        md = compute_meaning_distances(data)
        sd = compute_signal_distances(data)
        ts = topographic_similarity(md, sd)

        assert -1.0 <= ts['rho'] <= 1.0

    def test_perfect_compositionality(self):
        """If signal distance tracks meaning distance exactly, TS ~ 1."""
        N = 50
        # Create distance matrices with perfect monotonic relationship
        rng = np.random.default_rng(42)
        meaning_dist = np.zeros((N, N))
        signal_dist = np.zeros((N, N))
        for i in range(N):
            for j in range(i + 1, N):
                d = rng.uniform(0, 2)
                meaning_dist[i, j] = d
                meaning_dist[j, i] = d
                signal_dist[i, j] = d * 3 + 0.1  # Perfect monotonic
                signal_dist[j, i] = d * 3 + 0.1

        ts = topographic_similarity(meaning_dist, signal_dist)
        assert ts['rho'] > 0.95

    def test_region_consistency_with_uniform_tokens(self):
        """Random tokens should give low consistency."""
        rng = np.random.default_rng(42)
        tokens = list(PRIMITIVES_KEYS)
        data = []
        for _ in range(200):
            n = rng.integers(1, 4)
            data.append({
                'tokens': list(rng.choice(tokens, size=n, replace=False)),
                'warmth': rng.uniform(0, 1),
                'brightness': rng.uniform(0, 1),
                'stability': rng.uniform(0, 1),
                'presence': rng.uniform(-1, 1),
            })

        result = region_consistency(data)
        # Random tokens → high entropy → low consistency
        assert result['consistency_score'] < 0.7

    def test_region_consistency_score_range(self):
        data = generate_synthetic_data(n_samples=200, seed=42)
        result = region_consistency(data)
        assert 0.0 <= result['consistency_score'] <= 1.0
        assert result['mean_entropy'] >= 0.0
        assert result['mean_entropy'] <= result['max_possible_entropy']


# Need the token list for the uniform test
from compositionality_metrics import ALL_TOKENS as PRIMITIVES_KEYS
