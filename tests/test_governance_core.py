#!/usr/bin/env python3
"""
Test script for governance_core module

Verifies that the governance_core module works correctly and
produces expected results for basic dynamics operations.
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent  # Go up from tests/ to project root
sys.path.insert(0, str(project_root))


def test_imports():
    """Test that all imports work"""
    print("Testing imports...")

    from governance_core import (
        State, DynamicsParams, Theta, Weights,
        compute_dynamics, step_state,
        control_feedback, coherence, lambda1, lambda2,
        phi_objective, verdict_from_phi,
        clip, drift_norm,
        DEFAULT_PARAMS, DEFAULT_WEIGHTS, DEFAULT_THETA, DEFAULT_STATE,
    )

    print("✅ All imports successful")


def test_state_creation():
    """Test State creation"""
    print("\nTesting State creation...")

    from governance_core import State

    state = State(E=0.7, I=0.8, S=0.2, V=0.0)
    assert state.E == 0.7
    assert state.I == 0.8
    assert state.S == 0.2
    assert state.V == 0.0

    print(f"✅ State created: E={state.E}, I={state.I}, S={state.S}, V={state.V}")


def test_utils():
    """Test utility functions"""
    print("\nTesting utility functions...")

    from governance_core import clip, drift_norm

    # Test clip
    assert clip(0.5, 0.0, 1.0) == 0.5
    assert clip(-0.5, 0.0, 1.0) == 0.0
    assert clip(1.5, 0.0, 1.0) == 1.0
    print("✅ clip() working correctly")

    # Test drift_norm
    assert drift_norm([]) == 0.0
    assert abs(drift_norm([0.3, 0.4]) - 0.5) < 1e-6  # 3-4-5 triangle
    print("✅ drift_norm() working correctly")


def test_coherence():
    """Test coherence functions"""
    print("\nTesting coherence functions...")

    from governance_core import control_feedback, coherence, lambda1, lambda2
    from governance_core import DEFAULT_THETA, DEFAULT_PARAMS

    # Test coherence at V=0 (should be ~0.5*Cmax)
    C = coherence(0.0, DEFAULT_THETA, DEFAULT_PARAMS)
    print(f"  C(V=0) = {C:.4f}")
    assert abs(C - 0.5) < 0.01
    # The old public name remains an exact compatibility alias; new ODE code
    # uses the role-accurate name.
    assert control_feedback(0.0, DEFAULT_THETA, DEFAULT_PARAMS) == C

    # Test coherence at high V (should approach Cmax)
    C_high = coherence(5.0, DEFAULT_THETA, DEFAULT_PARAMS)
    print(f"  C(V=5) = {C_high:.4f}")
    assert C_high > 0.9

    # Test lambda functions
    l1 = lambda1(DEFAULT_THETA, DEFAULT_PARAMS)
    l2 = lambda2(DEFAULT_THETA, DEFAULT_PARAMS)
    print(f"  λ₁ = {l1:.4f}, λ₂ = {l2:.4f}")

    print("✅ Coherence functions working correctly")


def test_dynamics():
    """Test dynamics computation"""
    print("\nTesting dynamics computation...")

    from governance_core import (
        compute_dynamics, State,
        DEFAULT_THETA, DEFAULT_PARAMS
    )

    # Start with default state
    state = State(E=0.7, I=0.8, S=0.2, V=0.0)
    delta_eta = [0.1, 0.0, -0.05]

    # Evolve one step
    new_state = compute_dynamics(
        state=state,
        delta_eta=delta_eta,
        theta=DEFAULT_THETA,
        params=DEFAULT_PARAMS,
        dt=0.1,
    )

    print(f"  Initial: E={state.E:.3f}, I={state.I:.3f}, S={state.S:.3f}, V={state.V:.3f}")
    print(f"  After dt=0.1: E={new_state.E:.3f}, I={new_state.I:.3f}, S={new_state.S:.3f}, V={new_state.V:.3f}")

    # State should have changed
    assert new_state.E != state.E or new_state.I != state.I or new_state.S != state.S or new_state.V != state.V

    # State should remain in bounds
    assert 0.0 <= new_state.E <= 1.0
    assert 0.0 <= new_state.I <= 1.0
    assert 0.0 <= new_state.S <= 1.0
    assert -1.0 <= new_state.V <= 1.0

    print("✅ Dynamics computation working correctly")


def test_scoring():
    """Test scoring functions"""
    print("\nTesting scoring functions...")

    from governance_core import (
        phi_objective, verdict_from_phi,
        State, DEFAULT_WEIGHTS
    )

    # Good state (high E, high I, low S, low V)
    good_state = State(E=0.9, I=0.9, S=0.1, V=0.0)
    phi_good = phi_objective(good_state, delta_eta=[0.0], weights=DEFAULT_WEIGHTS)
    verdict_good = verdict_from_phi(phi_good)
    print(f"  Good state: Φ={phi_good:.3f} → {verdict_good}")

    # Bad state (low E, low I, high S, high V)
    bad_state = State(E=0.2, I=0.3, S=1.5, V=1.0)
    phi_bad = phi_objective(bad_state, delta_eta=[0.5, 0.3], weights=DEFAULT_WEIGHTS)
    verdict_bad = verdict_from_phi(phi_bad)
    print(f"  Bad state: Φ={phi_bad:.3f} → {verdict_bad}")

    # Good state should have higher phi
    assert phi_good > phi_bad

    print("✅ Scoring functions working correctly")


def test_step_state_wrapper():
    """Test step_state convenience wrapper"""
    print("\nTesting step_state wrapper...")

    from governance_core import step_state, State, DEFAULT_THETA

    state = State(E=0.7, I=0.8, S=0.2, V=0.0)
    new_state = step_state(
        state=state,
        theta=DEFAULT_THETA,
        delta_eta=[0.1],
        dt=0.1,
    )

    print(f"  step_state: E={new_state.E:.3f}, I={new_state.I:.3f}")
    print("✅ step_state wrapper working correctly")


def run_all_tests():
    """Run all tests"""
    print("=" * 60)
    print("GOVERNANCE CORE MODULE TEST SUITE")
    print("=" * 60)

    tests = [
        test_imports,
        test_state_creation,
        test_utils,
        test_coherence,
        test_dynamics,
        test_scoring,
        test_step_state_wrapper,
    ]

    results = []
    for test in tests:
        try:
            test()
            results.append(True)
        except Exception as e:
            print(f"❌ {test.__name__} failed: {e}")
            import traceback
            traceback.print_exc()
            results.append(False)

    print("\n" + "=" * 60)
    print(f"RESULTS: {sum(results)}/{len(results)} tests passed")
    print("=" * 60)

    if all(results):
        print("\n🎉 All tests passed! governance_core is working correctly.")
        return 0
    else:
        print("\n⚠️  Some tests failed. Please review the output above.")
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
