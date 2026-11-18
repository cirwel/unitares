#!/usr/bin/env python3
"""
Validate Implementation Against Theoretical Foundations

Checks that the actual ``governance_core/`` ODE implementation honors the
equations, bounds, control feedback, and objective function documented in
``docs/EISV_COMPUTATION.md`` (including Stage A S-setpoint detrending, I-rest
``S - s_setpoint`` coupling, v5 linear I damping default, and RK1 substepped
RK4 integration).

Usage:
    python3 scripts/analysis/validate_theoretical_foundations.py
"""

import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# governance_core lives at the top level of this repo. Source validation
# (reading its .py files) needs that directory, so run from a source checkout.
GOVERNANCE_CORE_SOURCE = project_root / "governance_core"
HAS_SOURCE = GOVERNANCE_CORE_SOURCE.is_dir() and (GOVERNANCE_CORE_SOURCE / "dynamics.py").exists()

from governance_core.coherence import coherence, lambda1
from governance_core.dynamics import DEFAULT_STATE, State, _integration_substeps, compute_dynamics
from governance_core.parameters import DEFAULT_PARAMS, DEFAULT_THETA, Theta, get_i_dynamics_mode


class ValidationResult:
    """Track validation results."""

    def __init__(self):
        self.passed = []
        self.failed = []
        self.warnings = []

    def add_pass(self, check: str, details: str = ""):
        self.passed.append((check, details))

    def add_fail(self, check: str, details: str = ""):
        self.failed.append((check, details))

    def add_warning(self, check: str, details: str = ""):
        self.warnings.append((check, details))

    def print_summary(self):
        print("\n" + "=" * 70)
        print("THEORETICAL FOUNDATIONS VALIDATION")
        print("=" * 70)

        print(f"\n✅ PASSED: {len(self.passed)}")
        for check, details in self.passed:
            print(f"   ✓ {check}")
            if details:
                print(f"     {details}")

        if self.warnings:
            print(f"\n⚠️  WARNINGS: {len(self.warnings)}")
            for check, details in self.warnings:
                print(f"   ⚠ {check}")
                if details:
                    print(f"     {details}")

        if self.failed:
            print(f"\n❌ FAILED: {len(self.failed)}")
            for check, details in self.failed:
                print(f"   ✗ {check}")
                if details:
                    print(f"     {details}")

        print("\n" + "=" * 70)
        total = len(self.passed) + len(self.failed) + len(self.warnings)
        if self.failed:
            print(f"RESULT: {len(self.passed)}/{total} checks passed")
            return False
        print(f"RESULT: All {len(self.passed)} checks passed!")
        return True


def check_equations_match(result: ValidationResult):
    """Check deployed differential equations (including Stage A / I-rest detrending)."""
    print("\n1. Checking Differential Equations...")

    dynamics_file = project_root / "governance_core" / "dynamics.py"
    code = dynamics_file.read_text(encoding="utf-8")

    checks = {
        "Detrended S excess (S - σ)": (
            "S_excess = S - s_setpoint" in code,
            "Stage A / I-rest update: E, I, and S decay share S - σ",
        ),
        "dE/dt = α(I - E)": (
            "params.alpha * (I - E)" in code,
            "E relaxes toward I",
        ),
        "dE/dt includes -βE·E·(S - σ)": (
            "params.beta_E * E * S_excess" in code,
            "Stage-A/I-rest detrended E-S damping",
        ),
        "dE/dt includes γE·‖Δη‖²": (
            "params.gamma_E * d_eta_sq" in code,
            "Drift feedback into E",
        ),
        "dI/dt includes βI·C(V,Θ) - k·(S - σ)": (
            "params.beta_I * C - params.k * S_excess" in code,
            "Directional control boost minus detrended S coupling",
        ),
        "dI/dt includes -γI·I (v5 linear default) and -γI·I·(1-I) (legacy logistic)": (
            'if i_mode == "linear":' in code
            and "dI_dt = A - params.gamma_I * I\n" in code
            and "dI_dt = A - params.gamma_I * I * (1 - I)" in code,
            "Both v5 linear damping branch and legacy logistic damping present",
        ),
        "dS/dt includes -μ·(S - σ)": (
            "-params.mu * (S - s_setpoint)" in code,
            "Stage A S-setpoint decay toward σ",
        ),
        "dS/dt includes λ₁·‖Δη‖²": (
            "lam1 * d_eta_sq" in code,
            "Adaptive drift-to-S coupling",
        ),
        "dS/dt includes -λ₂·C(V,Θ)": (
            "- lam2 * C" in code,
            "Control-feedback S reduction",
        ),
        "dS/dt includes β_complexity·complexity": (
            "params.beta_complexity * complexity" in code,
            "Documented complexity forcing term beyond minimal 4-term paper form",
        ),
        "dV/dt = κ(E - I) - δ·V": (
            "params.kappa * (E - I)" in code and "- params.delta * V" in code,
            "E-I imbalance accumulator with damping",
        ),
    }

    all_passed = True
    for eq_term, (found, detail) in checks.items():
        if found:
            result.add_pass(f"Equation term: {eq_term}", detail)
        else:
            result.add_fail(f"Equation term: {eq_term}", "Not found in implementation")
            all_passed = False

    return all_passed


def check_coherence_function(result: ValidationResult):
    """Check coherence function matches C(V,Θ) = Cmax · 0.5 · (1 + tanh(Θ.C₁ · V))."""
    print("\n2. Checking Coherence Function...")

    coherence_file = project_root / "governance_core" / "coherence.py"
    code = coherence_file.read_text(encoding="utf-8")

    if "Cmax * 0.5 * (1.0 + math.tanh(theta.C1 * V))" in code:
        result.add_pass(
            "Coherence function matches theoretical formula",
            "C(V,Θ) = Cmax · 0.5 · (1 + tanh(Θ.C₁ · V))",
        )
    elif "tanh" in code and "C1" in code:
        result.add_warning(
            "Coherence function uses tanh",
            "Formula structure matches but exact implementation may differ",
        )
    else:
        result.add_fail(
            "Coherence function",
            "Expected: Cmax · 0.5 · (1 + tanh(Θ.C₁ · V))",
        )

    test_cases = [
        (0.0, "V=0 should give C ≈ Cmax/2"),
        (-0.016, "Typical operating point"),
        (0.1, "V=0.1 should give higher coherence"),
    ]

    for V, description in test_cases:
        C = coherence(V, DEFAULT_THETA, DEFAULT_PARAMS)
        if 0 <= C <= DEFAULT_PARAMS.Cmax:
            result.add_pass(f"Coherence bounds: V={V:.3f} → C={C:.4f}", description)
        else:
            result.add_fail(f"Coherence bounds: V={V:.3f} → C={C:.4f}", "Outside [0, Cmax]")


def check_adaptive_control(result: ValidationResult):
    """Check PI controller for adaptive λ₁."""
    print("\n3. Checking Adaptive Control (PI Controller)...")

    config_file = project_root / "config" / "governance_config.py"
    config_code = config_file.read_text(encoding="utf-8")

    if "pi_update" in config_code:
        result.add_pass("PI controller function exists", "pi_update() in governance_config.py")
    else:
        result.add_fail("PI controller", "pi_update() not found")
        return

    coherence_file = project_root / "governance_core" / "coherence.py"
    coherence_code = coherence_file.read_text(encoding="utf-8")

    if "adaptive" in coherence_code.lower() and "eta1" in coherence_code:
        result.add_pass("Lambda1 is adaptive via theta.eta1", "Maps eta1 → lambda1")
    else:
        result.add_fail("Lambda1 adaptation", "eta1 mapping not found in coherence.py")

    test_cases = [
        (0.1, 0.05, "Minimum eta1 → minimum lambda1"),
        (0.3, 0.125, "Midpoint eta1 → midpoint lambda1"),
        (0.5, 0.20, "Maximum eta1 → maximum lambda1"),
    ]

    for eta1, expected_lambda1, description in test_cases:
        theta = Theta(C1=1.0, eta1=eta1)
        actual_lambda1 = lambda1(theta, DEFAULT_PARAMS)
        if abs(actual_lambda1 - expected_lambda1) < 0.01:
            result.add_pass(f"Lambda1 mapping: eta1={eta1:.1f} → λ₁={actual_lambda1:.4f}", description)
        else:
            result.add_fail(
                f"Lambda1 mapping: eta1={eta1:.1f} → λ₁={actual_lambda1:.4f}",
                f"Expected {expected_lambda1:.4f}, got {actual_lambda1:.4f}",
            )


def check_numerical_integration(result: ValidationResult):
    """Check RK1 substepped RK4 (default) and legacy Euler integrator."""
    print("\n4. Checking Numerical Integration (Substepped RK4 & Euler Fallback)...")

    dynamics_file = project_root / "governance_core" / "dynamics.py"
    code = dynamics_file.read_text(encoding="utf-8")

    if "def _integrate_rk4" in code and "dt / 6.0" in code and "k1[0] + 2*k2[0] + 2*k3[0] + k4[0]" in code:
        result.add_pass("RK4 integration implemented", "_integrate_rk4 (default since v2.4.0)")
    else:
        result.add_fail("RK4 integration", "_integrate_rk4 pattern not found")

    if "def _integrate_euler" in code and "state.E + dE * dt" in code:
        result.add_pass("Legacy Euler fallback retained", "_integrate_euler (UNITARES_INTEGRATOR=euler)")
    else:
        result.add_fail("Legacy Euler integration", "_integrate_euler pattern not found")

    if "def _integration_substeps" in code and "math.ceil(work)" in code and "for _ in range(steps):" in code:
        steps = _integration_substeps(DEFAULT_PARAMS, DEFAULT_THETA, 0.1, False, s_setpoint=0.145)
        stiff_steps = _integration_substeps(DEFAULT_PARAMS, DEFAULT_THETA, 10.0, False, s_setpoint=0.145)
        next_state = compute_dynamics(DEFAULT_STATE, [0.0, 0.0, 0.0], DEFAULT_THETA, DEFAULT_PARAMS, dt=0.1)
        stiff_state = compute_dynamics(DEFAULT_STATE, [0.0, 0.0, 0.0], DEFAULT_THETA, DEFAULT_PARAMS, dt=10.0)
        if (
            steps >= 1
            and stiff_steps > steps
            and 0.0 <= next_state.E <= 1.0
            and 0.0 <= next_state.I <= 1.0
            and 0.0 <= stiff_state.E <= 1.0
            and 0.0 <= stiff_state.I <= 1.0
        ):
            result.add_pass(
                "RK1 deterministic substepping active",
                f"_integration_substeps scales with dt (dt=0.1 -> {steps}, dt=10.0 -> {stiff_steps})",
            )
        else:
            result.add_fail("RK1 deterministic substepping", "Substep scaling or state bounds check failed")
    else:
        result.add_fail("RK1 deterministic substepping", "_integration_substeps loop not found in dynamics.py")


# Backward-compatible name for callers importing the earlier function.
check_euler_integration = check_numerical_integration


def check_self_regulation(result: ValidationResult):
    """Check I-channel self-regulation (v5 linear default & legacy logistic)."""
    print("\n5. Checking Self-Regulation (Linear Default & Logistic Legacy)...")

    dynamics_file = project_root / "governance_core" / "dynamics.py"
    code = dynamics_file.read_text(encoding="utf-8")

    orig_mode = os.environ.pop("UNITARES_I_DYNAMICS", None)
    try:
        default_mode = get_i_dynamics_mode()
        test_state = State(E=0.6, I=0.5, S=0.2, V=0.0)
        linear_out = compute_dynamics(test_state, [0.0, 0.0, 0.0], DEFAULT_THETA, DEFAULT_PARAMS, dt=0.1)
        os.environ["UNITARES_I_DYNAMICS"] = "logistic"
        logistic_out = compute_dynamics(test_state, [0.0, 0.0, 0.0], DEFAULT_THETA, DEFAULT_PARAMS, dt=0.1)
    finally:
        if orig_mode is None:
            os.environ.pop("UNITARES_I_DYNAMICS", None)
        else:
            os.environ["UNITARES_I_DYNAMICS"] = orig_mode

    if (
        default_mode == "linear"
        and "dI_dt = A - params.gamma_I * I\n" in code
        and "dI_dt = A - params.gamma_I * I * (1 - I)" in code
        and linear_out.I < logistic_out.I
    ):
        result.add_pass(
            "I-channel self-regulation",
            f"v5 linear -γI·I (default) damps faster than legacy logistic at I=0.5 ({linear_out.I:.6f} < {logistic_out.I:.6f})",
        )
    else:
        result.add_fail("I-channel self-regulation", f"Unexpected default mode ({default_mode!r}) or branch delta")


def check_bounded_domains(result: ValidationResult):
    """Check that state variables are bounded by clips, soft barriers, and complexity floor."""
    print("\n6. Checking Bounded Domains...")

    dynamics_file = project_root / "governance_core" / "dynamics.py"
    code = dynamics_file.read_text(encoding="utf-8")

    for dim in ("E", "I", "S", "V"):
        if f"params.{dim}_min, params.{dim}_max" in code and "clip(" in code:
            result.add_pass(f"{dim} bounded", f"Clipped to [{dim}_min, {dim}_max] with soft barrier")
        else:
            result.add_fail(f"{dim} bounds", f"Bounds [{dim}_min, {dim}_max] not found")

    if "complexity_floor = params.S_min + 0.049 * complexity" in code:
        result.add_pass("Post-step complexity entropy floor", "S_final = max(new_state.S, complexity_floor)")
    else:
        result.add_fail("Post-step complexity entropy floor", "complexity_floor not found")

    params_file = project_root / "governance_core" / "parameters.py"
    params_code = params_file.read_text(encoding="utf-8")

    if "S_min" in params_code and "0.001" in params_code:
        result.add_pass("Epistemic humility", "S_min = 0.001 prevents overconfidence")
    else:
        result.add_fail("Epistemic humility", "S_min = 0.001 constraint not found")


def check_domain_integration(result: ValidationResult):
    """Check that all core modeling mechanisms are wired."""
    print("\n7. Checking Domain Integration...")

    dynamics_code = (project_root / "governance_core" / "dynamics.py").read_text(encoding="utf-8")
    coherence_code = (project_root / "governance_core" / "coherence.py").read_text(encoding="utf-8")
    config_code = (project_root / "config" / "governance_config.py").read_text(encoding="utf-8")

    checks = {
        "E-I coupling": "params.alpha * (I - E)" in dynamics_code,
        "Entropy dynamics (setpoint-detrended)": (
            "dS_dt" in dynamics_code and "params.mu * (S - s_setpoint)" in dynamics_code
        ),
        "E-I imbalance accumulator V": "params.kappa * (E - I)" in dynamics_code,
        "Information integrity I": "dI_dt" in dynamics_code and "integrity" in dynamics_code.lower(),
        "PI controller": "pi_update" in config_code,
        "Adaptive lambda1": "adaptive" in coherence_code.lower() and "eta1" in coherence_code,
        "Ethical drift norm": "d_eta_sq" in dynamics_code and "drift_norm" in dynamics_code,
    }

    for domain_check, found in checks.items():
        if found:
            result.add_pass(domain_check)
        else:
            result.add_fail(domain_check, "Expected mechanism not found")


def check_objective_function(result: ValidationResult):
    """Check objective function Φ = wE·E - wI·(1-I) - wS·S - wV·|V| - wEta·‖Δη‖²."""
    print("\n8. Checking Objective Function...")

    scoring_file = project_root / "governance_core" / "scoring.py"
    if not scoring_file.exists():
        result.add_fail("Objective function", "scoring.py not found")
        return

    code = scoring_file.read_text(encoding="utf-8")

    if "phi_objective" in code:
        result.add_pass("Objective function exists", "phi_objective() in scoring.py")
    else:
        result.add_fail("Objective function", "phi_objective() not found")

    terms = {
        "wE·E": "wE" in code or "w_E" in code,
        "wI·(1-I)": "wI" in code or "w_I" in code,
        "wS·S": "wS" in code or "w_S" in code,
        "wV·|V|": "wV" in code or "w_V" in code,
        "wEta·‖Δη‖²": "wEta" in code or "w_eta" in code,
    }

    for term, found in terms.items():
        if found:
            result.add_pass(f"Objective term: {term}")
        else:
            result.add_fail(f"Objective term: {term}", "Term not found in scoring.py")


def main():
    """Run all validation checks."""
    if not HAS_SOURCE:
        print(f"governance_core source not found at {GOVERNANCE_CORE_SOURCE}.")
        print("governance_core/ ships at the top level of the unitares repo; run this")
        print("script from a full source checkout, where that directory is present.")
        return 1

    result = ValidationResult()

    check_equations_match(result)
    check_coherence_function(result)
    check_adaptive_control(result)
    check_numerical_integration(result)
    check_self_regulation(result)
    check_bounded_domains(result)
    check_domain_integration(result)
    check_objective_function(result)

    success = result.print_summary()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
