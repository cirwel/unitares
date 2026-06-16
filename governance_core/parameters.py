"""
UNITARES Governance Core - Parameter Definitions

Canonical parameter definitions for UNITARES Phase-3 dynamics.

This is the single source of truth for all parameter values,
bounds, and default configurations.
"""

from __future__ import annotations
from dataclasses import dataclass
import json
import os


@dataclass
class DynamicsParams:
    """
    UNITARES Phase-3 Dynamics Parameters

    These parameters control the thermodynamic evolution of the
    governance state (E, I, S, V).

    Source: UNITARES Phase-3 specification
    """

    # E dynamics
    alpha: float = 0.42          # I → E coupling strength (the answer)
    beta_E: float = 0.1          # S damping on E
    gamma_E: float = 0.05        # Drift feedback to E (enabled with conservative value)

    # I dynamics
    k: float = 0.1               # S → I coupling
    beta_I: float = 0.3          # Coherence boost to I
    gamma_I: float = 0.25        # I self-regulation

    # S dynamics
    mu: float = 0.5              # S decay rate (paper: 0.80, reduced for meaningful S variability)
    lambda1_base: float = 0.3    # Drift → S coupling base
    lambda2_base: float = 0.05   # Coherence → S reduction base
    beta_complexity: float = 0.15  # Complexity → S coupling (task difficulty increases uncertainty)

    # V dynamics
    kappa: float = 0.3           # (E-I) → V coupling
    delta: float = 0.4           # V decay rate (reverted from 0.25 — caused coherence spiral)

    # Sensor anchoring (for agents with physical sensors, e.g. Lumen)
    k_anchor: float = 0.1        # Spring coupling to sensor-derived EISV (0 = no anchoring)

    # Coherence parameters
    Cmax: float = 1.0            # Maximum coherence value
    coherence_scale: float = 1.0  # V scaling factor (1.0 = pure thermodynamic, no scaling)

    # State bounds
    E_min: float = 0.0
    E_max: float = 1.0
    I_min: float = 0.0
    I_max: float = 1.0
    S_min: float = 0.001  # Epistemic humility floor - prevents S=0.0 without external validation
    S_max: float = 1.0
    V_min: float = -1.0
    V_max: float = 1.0

    # Soft barrier parameters (replaces hard clipping as primary bound enforcement)
    barrier_strength: float = 2.0    # Repulsion force at boundary
    barrier_margin: float = 0.05     # Distance from bound where barrier activates

    # Control parameter bounds (for Theta optimization)
    C1_min: float = 0.5
    C1_max: float = 1.5
    eta1_min: float = 0.1
    eta1_max: float = 0.5


@dataclass
class Theta:
    """
    UNITARES Control Parameters

    These parameters are tunable for optimization and adaptation.

    Attributes:
        C1: Coherence function control parameter (affects tanh steepness)
        eta1: Ethical drift sensitivity multiplier (controls lambda1 adaptation)
        eta2: Coherence coupling multiplier (controls lambda2 adaptation)
    """
    C1: float
    eta1: float
    eta2: float = 0.3  # Default mirrors eta1 initial value


@dataclass
class Weights:
    """
    Objective Function Weights

    Used in computing Φ (phi) governance score.

    Φ = wE·E - wI·(1-I) - wS·S - wV·|V| - wEta·‖Δη‖²
    """
    wE: float = 0.5      # Weight for energy/exploration capacity
    wI: float = 0.5      # Weight for information integrity
    wS: float = 0.5      # Weight for semantic uncertainty
    wV: float = 0.5      # Weight for void imbalance
    wEta: float = 0.5    # Weight for ethical drift


# Default configurations
DEFAULT_PARAMS: DynamicsParams = DynamicsParams()
DEFAULT_THETA: Theta = Theta(C1=1.0, eta1=0.3)
DEFAULT_WEIGHTS: Weights = Weights()

# UNITARES v4.1 paper-aligned parameters (opt-in)
#
# IMPORTANT: This is intentionally NOT the default, because beta_I=0.05 is a
# significant behavior change from the current operational value 0.3.
V41_PARAMS: DynamicsParams = DynamicsParams(
    alpha=0.5,
    beta_I=0.05,
    gamma_I=0.3,
)

# UNITARES v4.2-P linear damping parameters
# Replaces logistic I(1-I) with linear I to prevent boundary saturation
# gamma_I tuned for I* ≈ 0.80 equilibrium with typical A ≈ 0.135
V42P_PARAMS: DynamicsParams = DynamicsParams(
    gamma_I=0.169,  # Tuned for I* = A/gamma_I ≈ 0.80
)


def get_integrator_mode() -> str:
    """
    Returns the ODE integration method.

    Supported:
    - UNITARES_INTEGRATOR=rk4 (default): 4th-order Runge-Kutta, O(dt^4) error
    - UNITARES_INTEGRATOR=euler: Forward Euler, O(dt) error (legacy)

    RK4 is the default since governance-core v2.4.0. It provides significantly
    better accuracy at the same dt, especially important for gap-aware dt
    scaling where dt can reach 1.0.
    """
    return os.getenv("UNITARES_INTEGRATOR", "rk4").strip().lower()


def get_i_dynamics_mode() -> str:
    """
    Returns the I-channel dynamics mode.

    Supported:
    - UNITARES_I_DYNAMICS=linear (default, v5: -γ_I·I, prevents boundary saturation)
    - UNITARES_I_DYNAMICS=logistic (legacy v4.1: -γ_I·I·(1-I), can saturate to I=1)

    Linear mode is the default since UNITARES v5. It prevents boundary saturation
    (m_sat = -1.23 under logistic with production parameters) and guarantees a
    stable interior equilibrium at I* = A/γ_I ≈ 0.80.

    See: papers/unitares-v5, Section 4 (I-Channel Saturation Analysis).
    """
    return os.getenv("UNITARES_I_DYNAMICS", "linear").strip().lower()


def sensor_coupling_enabled() -> bool:
    """
    Whether sensor-derived EISV spring-couples into the ODE.

    Default ON — preserves current production behavior. This is a fleet-wide
    switch, NOT Lumen-only: when an agent publishes no physical sensor, a
    behavioral sensor EISV (derived from governance observables) is injected and
    also spring-coupled (see src/mcp_handlers/updates/phases.py). Flipping the
    default therefore changes the dynamics for every agent with ≥3 check-ins.

    The "compare, don't couple" posture (disable this) lets the ODE evolve as an
    independent predictor and compares the sensor against it via
    ``governance_core.dynamics.eisv_divergence`` — recording divergence (cf.
    allostatic load) rather than springing it away. That divergence is recorded
    regardless of this flag, so an operator can review real divergence data
    before deciding to cut the spring.

    Disable with UNITARES_SENSOR_COUPLING in {0, false, off, no}.
    """
    val = os.getenv("UNITARES_SENSOR_COUPLING")
    if val is None:
        return True  # default: coupling on (no behavior change vs. pre-flag)
    return val.strip().lower() not in {"0", "false", "off", "no"}


def sensor_coupling_mode() -> str:
    """
    Coupling policy from UNITARES_SENSOR_COUPLING, resolved to a canonical mode:

      - 'on'              (default; any truthy / unset)  — couple every sensor source
      - 'off'             ({0,false,off,no})             — couple nothing
      - 'behavioral_only' — couple the behavioral sensor, NOT physical (cuts an
                            embodied agent's spring while leaving the disembodied
                            fleet anchored — the Lumen-only cut)
      - 'physical_only'   — couple physical sensors, NOT the behavioral sensor

    'behavioral_only'/'physical_only' read as truthy to the coarse
    sensor_coupling_enabled() gate (they are not off-values), so the fine-grained
    source decision lives in sensor_coupling_allows() and is applied where the
    sensor source is known (the monitor).
    """
    val = os.getenv("UNITARES_SENSOR_COUPLING")
    if val is None:
        return "on"
    v = val.strip().lower()
    if v in {"0", "false", "off", "no"}:
        return "off"
    if v in {"behavioral_only", "physical_only", "on"}:
        return v
    return "on"  # any other truthy value (1/true/yes/...) couples everything


def sensor_coupling_allows(source) -> bool:
    """Whether a sensor of the given source ('physical' | 'behavioral' | None)
    should spring-couple into the ODE under the current policy.

    Unknown/None source is treated as physical: physical sensors are the
    caller-published ``sensor_data["eisv"]``; only the behavioral sensor
    self-identifies as 'behavioral'."""
    mode = sensor_coupling_mode()
    if mode == "on":
        return True
    if mode == "off":
        return False
    is_behavioral = source == "behavioral"
    if mode == "behavioral_only":
        return is_behavioral
    if mode == "physical_only":
        return not is_behavioral
    return True


def get_params_profile_name() -> str:
    """
    Returns the active parameters profile name.

    Supported:
    - UNITARES_PARAMS_PROFILE=default (default)
    - UNITARES_PARAMS_PROFILE=v41
    """
    return os.getenv("UNITARES_PARAMS_PROFILE", "default").strip().lower()


def get_active_params() -> DynamicsParams:
    """
    Returns the active dynamics parameters.

    Resolution order:
    1) UNITARES_PARAMS_JSON (full/partial override as JSON object)
    2) UNITARES_PARAMS_PROFILE (default|v41)
    3) Auto-adjust γ_I when linear mode + default profile (v5 behavior)

    When linear I-dynamics mode is active with the default profile,
    γ_I is automatically set to 0.169 (V42P tuning) for the designed
    equilibrium at I* ≈ 0.80. Override with UNITARES_PARAMS_JSON if needed.
    """
    profile = get_params_profile_name()
    base = V41_PARAMS if profile == "v41" else DEFAULT_PARAMS

    # Auto-apply linear-tuned γ_I when using linear mode + default profile
    if profile != "v41" and get_i_dynamics_mode() == "linear":
        base = DynamicsParams(**{
            **base.__dict__,
            'gamma_I': V42P_PARAMS.gamma_I,  # 0.169, tuned for I*≈0.80
        })

    raw = os.getenv("UNITARES_PARAMS_JSON")
    if not raw:
        return base

    try:
        overrides = json.loads(raw)
    except Exception:
        # Invalid JSON: ignore override (defensive)
        return base

    # Apply only known fields
    d = base.__dict__.copy()
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            if k in d and v is not None:
                d[k] = v
    try:
        return DynamicsParams(**d)
    except Exception:
        # If overrides make the dataclass invalid, fall back to base
        return base

# Default initial state
# This is imported from dynamics.py to avoid circular imports
# See dynamics.py for State definition
