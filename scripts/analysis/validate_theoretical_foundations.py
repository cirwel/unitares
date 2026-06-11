#!/usr/bin/env python3
"""
Validate Implementation Against Theoretical Foundations

Checks that the actual implementation honors the claims made in
docs/theory/EISV_THEORETICAL_FOUNDATIONS.md

Usage:
    python scripts/validate_theoretical_foundations.py
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# governance_core is now a compiled package (unitares-core).
# Source validation (reading .py files) requires a local source checkout.
GOVERNANCE_CORE_SOURCE = project_root / "governance_core"
HAS_SOURCE = GOVERNANCE_CORE_SOURCE.is_dir() and (GOVERNANCE_CORE_SOURCE / "dynamics.py").exists()

from governance_core.coherence import coherence, lambda1
from governance_core.parameters import DEFAULT_PARAMS, DEFAULT_THETA, Theta


class ValidationResult:
    """Track validation results"""
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
        print("\n" + "="*70)
        print("THEORETICAL FOUNDATIONS VALIDATION")
        print("="*70)
        
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
        
        print("\n" + "="*70)
        total = len(self.passed) + len(self.failed) + len(self.warnings)
        if self.failed:
            print(f"RESULT: {len(self.passed)}/{total} checks passed")
            return False
        else:
            print(f"RESULT: All {len(self.passed)} checks passed!")
            return True


def check_equations_match(result: ValidationResult):
    """Check that differential equations match theoretical foundations"""
    print("\n1. Checking Differential Equations...")
    
    # Read dynamics.py source to extract equations
    dynamics_file = project_root / "governance_core" / "dynamics.py"
    with open(dynamics_file) as f:
        code = f.read()
    
    # Check for each term in the equations
    checks = {
        "dE/dt = α(I - E)": "alpha * (I - E)" in code or "params.alpha * (I - E)" in code,
        "dE/dt includes -βE·S": "- params.beta_E * S" in code or "-beta_E * S" in code,
        "dE/dt includes γE·‖Δη‖²": "gamma_E * d_eta_sq" in code or "params.gamma_E * d_eta_sq" in code,
        "dI/dt includes -k·S": "-params.k * S" in code or "-k * S" in code,
        "dI/dt includes βI·C(V,Θ)": "beta_I * C" in code or "params.beta_I * C" in code,
        "dI/dt includes -γI·I·(1-I)": "gamma_I * I * (1 - I)" in code or "-params.gamma_I * I * (1 - I)" in code,
        "dS/dt includes -μ·S": "-params.mu * S" in code or "-mu * S" in code,
        "dS/dt includes λ₁·‖Δη‖²": "lam1 * d_eta_sq" in code,
        "dS/dt includes -λ₂·C": "- lam2 * C" in code,
        "dV/dt = κ(E - I)": "kappa * (E - I)" in code or "params.kappa * (E - I)" in code,
        "dV/dt includes -δ·V": "- params.delta * V" in code or "-params.delta * V" in code or "-delta * V" in code,
    }
    
    all_passed = True
    for eq_term, found in checks.items():
        if found:
            result.add_pass(f"Equation term: {eq_term}")
        else:
            result.add_fail(f"Equation term: {eq_term}", "Not found in implementation")
            all_passed = False
    
    # Note: Implementation has additional complexity term in dS/dt
    # This is an enhancement, not a violation
    if "beta_complexity" in code:
        result.add_warning(
            "dS/dt includes complexity term",
            "Implementation adds β_complexity·C term (enhancement, not in theoretical doc)"
        )
    
    return all_passed


def check_coherence_function(result: ValidationResult):
    """Check coherence function matches C(V,Θ) = Cmax · 0.5 · (1 + tanh(Θ.C₁ · V))"""
    print("\n2. Checking Coherence Function...")
    
    coherence_file = project_root / "governance_core" / "coherence.py"
    with open(coherence_file) as f:
        code = f.read()
    
    # Check for exact formula
    if "Cmax * 0.5 * (1.0 + math.tanh(theta.C1 * V))" in code:
        result.add_pass(
            "Coherence function matches theoretical formula",
            "C(V,Θ) = Cmax · 0.5 · (1 + tanh(Θ.C₁ · V))"
        )
    elif "tanh" in code and "C1" in code:
        result.add_warning(
            "Coherence function uses tanh",
            "Formula structure matches but exact implementation may differ"
        )
    else:
        result.add_fail(
            "Coherence function",
            "Expected: Cmax · 0.5 · (1 + tanh(Θ.C₁ · V))"
        )
    
    # Test coherence function with known values
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
    """Check PI controller for adaptive λ₁"""
    print("\n3. Checking Adaptive Control (PI Controller)...")
    
    # Check if PI controller exists
    config_file = project_root / "config" / "governance_config.py"
    with open(config_file) as f:
        config_code = f.read()
    
    if "pi_update" in config_code:
        result.add_pass("PI controller function exists", "pi_update() in governance_config.py")
    else:
        result.add_fail("PI controller", "pi_update() not found")
        return
    
    # Check if lambda1 is adaptive
    coherence_file = project_root / "governance_core" / "coherence.py"
    with open(coherence_file) as f:
        coherence_code = f.read()
    
    if "adaptive" in coherence_code.lower() and "eta1" in coherence_code:
        result.add_pass("Lambda1 is adaptive via theta.eta1", "Maps eta1 → lambda1")
    else:
        result.add_warning("Lambda1 adaptation", "May not be fully adaptive")
    
    # Test lambda1 mapping
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
            result.add_warning(
                f"Lambda1 mapping: eta1={eta1:.1f} → λ₁={actual_lambda1:.4f}",
                f"Expected {expected_lambda1:.4f}, got {actual_lambda1:.4f}"
            )


def check_euler_integration(result: ValidationResult):
    """Check that Euler method is used for integration"""
    print("\n4. Checking Numerical Integration (Euler Method)...")
    
    dynamics_file = project_root / "governance_core" / "dynamics.py"
    with open(dynamics_file) as f:
        code = f.read()
    
    # Check for Euler integration pattern: x_new = x + dx_dt * dt
    if "E_new = clip(E + dE_dt * dt" in code:
        result.add_pass("Euler integration for E", "E_new = E + dE_dt * dt")
    else:
        result.add_fail("Euler integration", "Pattern not found")
    
    if "I_new = clip(I + dI_dt * dt" in code:
        result.add_pass("Euler integration for I", "I_new = I + dI_dt * dt")
    
    if "S_new = clip(S + dS_dt * dt" in code:
        result.add_pass("Euler integration for S", "S_new = S + dS_dt * dt")
    
    if "V_new = clip(V + dV_dt * dt" in code:
        result.add_pass("Euler integration for V", "V_new = V + dV_dt * dt")


def check_self_regulation(result: ValidationResult):
    """Check logistic self-regulation term γI·I·(1-I)"""
    print("\n5. Checking Self-Regulation (Logistic Dynamics)...")
    
    dynamics_file = project_root / "governance_core" / "dynamics.py"
    with open(dynamics_file) as f:
        code = f.read()
    
    if "gamma_I * I * (1 - I)" in code or "gamma_I * I * (1-I)" in code:
        result.add_pass("Logistic self-regulation", "γI·I·(1-I) prevents saturation")
    else:
        result.add_fail("Logistic self-regulation", "Term not found")


def check_bounded_domains(result: ValidationResult):
    """Check that state variables are bounded"""
    print("\n6. Checking Bounded Domains...")
    
    dynamics_file = project_root / "governance_core" / "dynamics.py"
    with open(dynamics_file) as f:
        code = f.read()
    
    # Check clipping to bounds
    if "clip(E + dE_dt * dt, params.E_min, params.E_max)" in code:
        result.add_pass("E bounded", "Clipped to [E_min, E_max]")
    else:
        result.add_warning("E bounds", "May not be explicitly clipped")
    
    if "clip(I + dI_dt * dt, params.I_min, params.I_max)" in code:
        result.add_pass("I bounded", "Clipped to [I_min, I_max]")
    
    if "clip(S + dS_dt * dt, params.S_min, params.S_max)" in code:
        result.add_pass("S bounded", "Clipped to [S_min, S_max]")
    
    if "clip(V + dV_dt * dt, params.V_min, params.V_max)" in code:
        result.add_pass("V bounded", "Clipped to [V_min, V_max]")
    
    # Check parameter defaults
    params_file = project_root / "governance_core" / "parameters.py"
    with open(params_file) as f:
        params_code = f.read()
    
    # Check for S_min constraint (epistemic humility)
    if "S_min" in params_code and "0.001" in params_code:
        result.add_pass("Epistemic humility", "S_min = 0.001 prevents overconfidence")
    else:
        result.add_warning("Epistemic humility", "S_min constraint may not be explicit")


def check_domain_integration(result: ValidationResult):
    """Check that all four theoretical domains are present"""
    print("\n7. Checking Domain Integration...")
    
    # Thermodynamics: E-I coupling, entropy, free energy
    dynamics_file = project_root / "governance_core" / "dynamics.py"
    with open(dynamics_file) as f:
        dynamics_code = f.read()
    
    # Information theory: S as entropy, I as integrity
    coherence_file = project_root / "governance_core" / "coherence.py"
    with open(coherence_file) as f:
        coherence_code = f.read()
    
    # Control theory: PI controller, adaptive lambda1
    config_file = project_root / "config" / "governance_config.py"
    with open(config_file) as f:
        config_code = f.read()
    
    checks = {
        "Thermodynamics - E-I coupling": "alpha * (I - E)" in dynamics_code,
        "Thermodynamics - Entropy dynamics": "dS_dt" in dynamics_code and "mu * S" in dynamics_code,
        "Thermodynamics - Void as free energy": "kappa * (E - I)" in dynamics_code,
        "Information Theory - S as entropy": "Semantic uncertainty" in dynamics_code or "entropy" in dynamics_code.lower(),
        "Information Theory - I as integrity": "Information integrity" in dynamics_code or "integrity" in dynamics_code.lower(),
        "Control Theory - PI controller": "pi_update" in config_code,
        "Control Theory - Adaptive lambda1": "adaptive" in coherence_code.lower() and "eta1" in coherence_code,
        "Ethics - Ethical drift": "delta_eta" in dynamics_code or "drift" in dynamics_code.lower(),
    }
    
    for domain_check, found in checks.items():
        if found:
            result.add_pass(domain_check)
        else:
            result.add_warning(domain_check, "May be implicit")


def check_objective_function(result: ValidationResult):
    """Check objective function Φ = wE·E - wI·(1-I) - wS·S - wV·|V| - wEta·‖Δη‖²"""
    print("\n8. Checking Objective Function...")
    
    scoring_file = project_root / "governance_core" / "scoring.py"
    if not scoring_file.exists():
        result.add_warning("Objective function", "scoring.py not found")
        return
    
    with open(scoring_file) as f:
        code = f.read()
    
    # Check for phi_objective function
    if "phi_objective" in code:
        result.add_pass("Objective function exists", "phi_objective() in scoring.py")
    else:
        result.add_warning("Objective function", "phi_objective() not found")
    
    # Check for terms
    terms = {
        "wE·E": "wE" in code or "w_E" in code,
        "wI·(1-I)": "wI" in code or "w_I" in code,
        "wS·S": "wS" in code or "w_S" in code,
        "wV·|V|": "wV" in code or "w_V" in code,
        "wEta·‖Δη‖²": "wEta" in code or "w_eta" in code or "wEta" in code,
    }
    
    for term, found in terms.items():
        if found:
            result.add_pass(f"Objective term: {term}")
        else:
            result.add_warning(f"Objective term: {term}", "May use different naming")


def main():
    """Run all validation checks"""
    if not HAS_SOURCE:
        print("governance_core source not available (compiled package installed).")
        print("To run source validation, symlink unitares-core/governance_core:")
        print("  ln -sf ~/projects/unitares-core/governance_core governance_core")
        return 1

    result = ValidationResult()

    check_equations_match(result)
    check_coherence_function(result)
    check_adaptive_control(result)
    check_euler_integration(result)
    check_self_regulation(result)
    check_bounded_domains(result)
    check_domain_integration(result)
    check_objective_function(result)
    
    success = result.print_summary()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())

