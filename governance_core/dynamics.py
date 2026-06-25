"""
UNITARES Governance Core - Dynamics Engine

Canonical implementation of UNITARES v5 thermodynamic dynamics.

This module contains the differential equations that govern the evolution
of the UNITARES state (E, I, S, V). This is the single source of truth
for all dynamics computations.

Mathematical Framework:
    dE/dt = α(I - E) - βE·S + γE·‖Δη‖²
    dI/dt = -k·S + βI·C(V,Θ) - γI·I          [linear mode, default since v5]
         or -k·S + βI·C(V,Θ) - γI·I·(1-I)    [logistic mode, legacy]
    dS/dt = -μ·S + λ₁(Θ)·‖Δη‖² - λ₂(Θ)·C(V,Θ) + β_complexity·C + noise
    dV/dt = κ(E - I) - δ·V

where:
    E: Energy (exploration/productive capacity) [0,1]
    I: Information integrity [0,1]
    S: Semantic uncertainty [0,2]
    V: E-I imbalance integral (damped accumulator, like Helmholtz free energy) [-2,2]
        V > 0: energy surplus (running hot), V < 0: integrity surplus (running careful)
        Feeds back through coherence: C(V,Θ) = Cmax · 0.5 · (1 + tanh(C₁·V))
        Note: "Void" name comes from Lumen mapping V=(1-presence)*0.3; the ODE
        evolves V as a signed integrator, which is a different quantity.
    C(V,Θ): Coherence function
    ‖Δη‖: Ethical drift norm
    C: Task complexity [0,1] - increases entropy S
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional

from .parameters import DynamicsParams, Theta
from .utils import clip, drift_norm, barrier
from .coherence import coherence, lambda1, lambda2


@dataclass
class State:
    """
    UNITARES Thermodynamic State

    Represents the four core state variables of the UNITARES system.

    Attributes:
        E: Energy (exploration/productive capacity) [0, 1]
        I: Information integrity [0, 1]
        S: Semantic uncertainty / disorder [0, 2]
        V: E-I imbalance integral [-2, 2]. Positive=energy surplus, negative=integrity surplus.
           Drives coherence feedback. Named "Void" in Lumen's observation layer.
    """
    E: float
    I: float
    S: float
    V: float

    def to_dict(self) -> dict:
        """Convert state to dictionary"""
        return {
            'E': self.E,
            'I': self.I,
            'S': self.S,
            'V': self.V,
        }


# Default initial state
DEFAULT_STATE = State(E=0.7, I=0.8, S=0.2, V=0.0)


def eisv_divergence(sensor_eisv: State, ode_state: State) -> dict:
    """Per-axis divergence between the body (sensor) and the model (ODE).

    Returns ``sensor - ode`` on each axis plus the L2 magnitude. This is the
    "compare, don't couple" measurement: the ODE evolves as an independent
    predictor and the sensor is the measured body, so their sustained
    disagreement is itself the signal (cf. allostatic load) rather than noise
    to be sprung away.

    The values are in each axis's NATIVE units and are not comparable across
    axes: the ODE's V is a signed E-I-imbalance accumulator in [-2, 2], whereas
    a sensor layer (e.g. Lumen's Pi mapping V=(1-presence)*0.3) may put V in a
    different range derived from a different quantity. Read the axes
    individually; treat ``magnitude`` as a coarse "how far apart" scalar only.
    """
    dE = sensor_eisv.E - ode_state.E
    dI = sensor_eisv.I - ode_state.I
    dS = sensor_eisv.S - ode_state.S
    dV = sensor_eisv.V - ode_state.V
    magnitude = (dE * dE + dI * dI + dS * dS + dV * dV) ** 0.5
    return {"dE": dE, "dI": dI, "dS": dS, "dV": dV, "magnitude": magnitude}


def _derivatives(
    state: State,
    d_eta_sq: float,
    theta: Theta,
    params: DynamicsParams,
    noise_S: float,
    complexity: float,
    sensor_eisv: Optional[State],
    s_setpoint: float = 0.0,
) -> tuple:
    """
    Compute raw EISV derivatives at a given state.

    This is the pure derivative function f(x) for the ODE system,
    separated from integration to support higher-order methods (RK4).

    Args:
        state: Current EISV state
        d_eta_sq: Squared drift norm ‖Δη‖²
        theta: Control parameters
        params: Dynamics parameters
        noise_S: Calibration penalty / noise term for S
        complexity: Task complexity [0, 1]
        sensor_eisv: Optional sensor state for spring coupling
        s_setpoint: Class-conditional rest target for S. Default 0.0 makes the
            S decay term ``-μS`` (historical behavior). When non-zero the term
            becomes ``-μ(S - s_setpoint)``, shifting the S equilibrium toward a
            measured-healthy operating point (see config.get_s_setpoint). Off by
            default; gated by UNITARES_S_SETPOINT at the call site.

    Returns:
        (dE_dt, dI_dt, dS_dt, dV_dt) tuple
    """
    # Compute coherence at this state's V
    C = coherence(state.V, theta, params)

    # Compute adaptive lambda values (theta-dependent, state-independent)
    lam1 = lambda1(theta, params)
    lam2 = lambda2(theta, params)

    E, I, S, V = state.E, state.I, state.S, state.V

    # E dynamics: Ė = α(I - E) - βₑES + γₑ‖Δη‖²
    dE_dt = (
        params.alpha * (I - E)
        - params.beta_E * E * S
        + params.gamma_E * d_eta_sq
    )

    # I dynamics
    A = params.beta_I * C - params.k * S
    from .parameters import get_i_dynamics_mode
    i_mode = get_i_dynamics_mode()
    if i_mode == "linear":
        dI_dt = A - params.gamma_I * I
    else:
        dI_dt = A - params.gamma_I * I * (1 - I)

    # S dynamics: Ṡ = -μ(S - σ) + λ₁‖Δη‖² - λ₂C + β_c·complexity + noise
    # σ (s_setpoint) defaults to 0.0 → -μS, the historical behavior.
    dS_dt = (
        -params.mu * (S - s_setpoint)
        + lam1 * d_eta_sq
        - lam2 * C
        + params.beta_complexity * complexity
        + noise_S
    )

    # V dynamics: V̇ = κ(E - I) - δV
    dV_dt = (
        params.kappa * (E - I)
        - params.delta * V
    )

    # Sensor anchoring: spring coupling to observed sensor state.
    # Only reached when coupling is opt-in enabled — compute_dynamics passes
    # sensor_eisv=None here by default (compare, don't couple).
    if sensor_eisv is not None:
        E_range = params.E_max - params.E_min
        S_range = params.S_max - params.S_min
        V_range = params.V_max - params.V_min
        dE_dt += params.k_anchor * (sensor_eisv.E - E) / E_range
        dI_dt += params.k_anchor * (sensor_eisv.I - I) / E_range
        dS_dt += params.k_anchor * (sensor_eisv.S - S) / S_range
        dV_dt += params.k_anchor * (sensor_eisv.V - V) / V_range

    # Soft barrier: smooth repulsion near state bounds
    # Replaces hard clipping as primary bound enforcement. Clips in the
    # integrators remain as a safety net but should never activate.
    m = params.barrier_margin
    s = params.barrier_strength
    S_range_ratio = params.S_max - params.S_min   # ~1.0
    V_range_ratio = params.V_max - params.V_min   # ~2.0

    dE_dt += barrier(E, params.E_min, params.E_max, s, m)
    dI_dt += barrier(I, params.I_min, params.I_max, s, m)
    dS_dt += barrier(S, params.S_min, params.S_max, s, m * S_range_ratio)
    dV_dt += barrier(V, params.V_min, params.V_max, s, m * V_range_ratio)

    return (dE_dt, dI_dt, dS_dt, dV_dt)


def _integrate_euler(
    state: State,
    d_eta_sq: float,
    theta: Theta,
    params: DynamicsParams,
    dt: float,
    noise_S: float,
    complexity: float,
    sensor_eisv: Optional[State],
    s_setpoint: float = 0.0,
) -> State:
    """Forward Euler integration: x_new = x + dt * f(x)."""
    dE, dI, dS, dV = _derivatives(state, d_eta_sq, theta, params, noise_S, complexity, sensor_eisv, s_setpoint)

    E_new = clip(state.E + dE * dt, params.E_min, params.E_max)
    I_new = clip(state.I + dI * dt, params.I_min, params.I_max)
    S_new = clip(state.S + dS * dt, params.S_min, params.S_max)
    V_new = clip(state.V + dV * dt, params.V_min, params.V_max)

    return State(E=E_new, I=I_new, S=S_new, V=V_new)


def _integrate_rk4(
    state: State,
    d_eta_sq: float,
    theta: Theta,
    params: DynamicsParams,
    dt: float,
    noise_S: float,
    complexity: float,
    sensor_eisv: Optional[State],
    s_setpoint: float = 0.0,
) -> State:
    """
    4th-order Runge-Kutta integration.

    x_new = x + (dt/6)(k1 + 2k2 + 2k3 + k4)

    Intermediate states are clipped to bounds to prevent derivative
    evaluation at unphysical points.
    """
    E, I, S, V = state.E, state.I, state.S, state.V

    # k1 = f(state)
    k1 = _derivatives(state, d_eta_sq, theta, params, noise_S, complexity, sensor_eisv, s_setpoint)

    # k2 = f(state + 0.5*dt*k1)
    s2 = State(
        E=clip(E + 0.5 * dt * k1[0], params.E_min, params.E_max),
        I=clip(I + 0.5 * dt * k1[1], params.I_min, params.I_max),
        S=clip(S + 0.5 * dt * k1[2], params.S_min, params.S_max),
        V=clip(V + 0.5 * dt * k1[3], params.V_min, params.V_max),
    )
    k2 = _derivatives(s2, d_eta_sq, theta, params, noise_S, complexity, sensor_eisv, s_setpoint)

    # k3 = f(state + 0.5*dt*k2)
    s3 = State(
        E=clip(E + 0.5 * dt * k2[0], params.E_min, params.E_max),
        I=clip(I + 0.5 * dt * k2[1], params.I_min, params.I_max),
        S=clip(S + 0.5 * dt * k2[2], params.S_min, params.S_max),
        V=clip(V + 0.5 * dt * k2[3], params.V_min, params.V_max),
    )
    k3 = _derivatives(s3, d_eta_sq, theta, params, noise_S, complexity, sensor_eisv, s_setpoint)

    # k4 = f(state + dt*k3)
    s4 = State(
        E=clip(E + dt * k3[0], params.E_min, params.E_max),
        I=clip(I + dt * k3[1], params.I_min, params.I_max),
        S=clip(S + dt * k3[2], params.S_min, params.S_max),
        V=clip(V + dt * k3[3], params.V_min, params.V_max),
    )
    k4 = _derivatives(s4, d_eta_sq, theta, params, noise_S, complexity, sensor_eisv, s_setpoint)

    # Combine: x_new = x + (dt/6)(k1 + 2k2 + 2k3 + k4)
    dt6 = dt / 6.0
    E_new = clip(E + dt6 * (k1[0] + 2*k2[0] + 2*k3[0] + k4[0]), params.E_min, params.E_max)
    I_new = clip(I + dt6 * (k1[1] + 2*k2[1] + 2*k3[1] + k4[1]), params.I_min, params.I_max)
    S_new = clip(S + dt6 * (k1[2] + 2*k2[2] + 2*k3[2] + k4[2]), params.S_min, params.S_max)
    V_new = clip(V + dt6 * (k1[3] + 2*k2[3] + 2*k3[3] + k4[3]), params.V_min, params.V_max)

    return State(E=E_new, I=I_new, S=S_new, V=V_new)


def compute_dynamics(
    state: State,
    delta_eta: List[float],
    theta: Theta,
    params: DynamicsParams,
    dt: float = 0.1,
    noise_S: float = 0.0,
    complexity: float = 0.5,
    sensor_eisv: Optional[State] = None,
    s_setpoint: float = 0.0,
) -> State:
    """
    Compute one time step of UNITARES Phase-3 dynamics.

    This is the canonical dynamics implementation. Both the production
    UNITARES system and the research unitaires system should use this
    function for state evolution.

    Supports two integration methods:
    - RK4 (default): 4th-order Runge-Kutta, O(dt^4) error
    - Euler: Forward Euler, O(dt) error (legacy, for backward compat)

    Set via UNITARES_INTEGRATOR env var ('rk4' or 'euler').

    Args:
        state: Current UNITARES state (E, I, S, V)
        delta_eta: Ethical drift vector (list of floats)
        theta: Control parameters (C1, eta1)
        params: Dynamics parameters (alpha, beta, etc.)
        dt: Time step for integration
        noise_S: Optional noise term for S dynamics
        complexity: Task complexity [0, 1] - increases entropy S (default: 0.5)
        sensor_eisv: Optional sensor-derived EISV state (e.g. from Lumen's Pi, or
            the behavioral sensor for non-embodied agents). When coupling is
            enabled (default; UNITARES_SENSOR_COUPLING), it adds a spring term
            k_anchor*(sensor - state) to each derivative. When disabled, the ODE
            evolves independently and callers compare against the sensor via
            eisv_divergence() ("compare, don't couple").

    Returns:
        New state after dt time evolution
    """
    # SECURITY: Clip complexity to valid range [0,1] as defense-in-depth
    complexity = max(0.0, min(1.0, complexity))

    # Compute derived quantities (constant across RK4 sub-steps)
    d_eta = drift_norm(delta_eta)
    d_eta_sq = d_eta * d_eta

    # Sensor EISV coupling is flag-gated (default ON; see
    # parameters.sensor_coupling_enabled). When disabled, the ODE evolves as an
    # independent predictor and callers instead compare the result against the
    # sensor via eisv_divergence() — "compare, don't couple". Coupling can inject
    # bias when the sensor->EISV mapping is not commensurate per-axis.
    from .parameters import get_integrator_mode, sensor_coupling_enabled
    coupling_sensor = sensor_eisv if sensor_coupling_enabled() else None

    # Select integrator
    integrator = get_integrator_mode()

    if integrator == "euler":
        new_state = _integrate_euler(
            state, d_eta_sq, theta, params, dt, noise_S, complexity, coupling_sensor, s_setpoint,
        )
    else:
        new_state = _integrate_rk4(
            state, d_eta_sq, theta, params, dt, noise_S, complexity, coupling_sensor, s_setpoint,
        )

    # Post-integration: complexity-proportional entropy floor
    # Applied after integration (not inside derivative) to avoid
    # non-smooth dynamics affecting RK4 intermediate evaluations
    complexity_floor = params.S_min + 0.049 * complexity
    S_final = max(new_state.S, complexity_floor)

    return State(E=new_state.E, I=new_state.I, S=S_final, V=new_state.V)


def step_state(
    state: State,
    theta: Theta,
    delta_eta: List[float],
    dt: float,
    noise_S: float = 0.0,
    params: Optional[DynamicsParams] = None,
    complexity: float = 0.5,
    sensor_eisv: Optional[State] = None,
    s_setpoint: float = 0.0,
) -> State:
    """
    Convenience wrapper for compute_dynamics with default params.

    This function maintains API compatibility with the original
    unitaires_core.step_state() function.

    Args:
        state: Current state
        theta: Control parameters
        delta_eta: Ethical drift vector
        dt: Time step
        noise_S: Optional noise for S
        params: Optional parameters (uses DEFAULT_PARAMS if None)
        complexity: Task complexity [0, 1] (default: 0.5)
        sensor_eisv: Optional sensor-derived EISV for spring coupling

    Returns:
        New state after dt
    """
    from .parameters import get_active_params

    if params is None:
        params = get_active_params()

    return compute_dynamics(
        state=state,
        delta_eta=delta_eta,
        theta=theta,
        params=params,
        dt=dt,
        noise_S=noise_S,
        complexity=complexity,
        sensor_eisv=sensor_eisv,
        s_setpoint=s_setpoint,
    )


def compute_equilibrium(
    params: DynamicsParams,
    theta: Theta,
    ethical_drift_norm_sq: float = 0.0,
    complexity: float = 0.5,
) -> State:
    """
    Compute an equilibrium for the current softened ODE.

    The active dynamics include coherence feedback, soft barriers, and a
    complexity-aware entropy floor, so the older closed-form approximation can
    miss the true operating point. We therefore relax the full system forward
    until it reaches a fixed point, then return that settled state.

    Args:
        params: Dynamics parameters
        theta: Control parameters
        ethical_drift_norm_sq: ‖Δη‖² (default 0)
        complexity: Task complexity [0, 1] (default 0.5, affects S* via β_complexity)

    Returns:
        Equilibrium state for the current dynamics.
    """
    complexity = max(0.0, min(1.0, complexity))
    delta_eta = [ethical_drift_norm_sq ** 0.5] if ethical_drift_norm_sq > 0 else []
    state = State(
        E=clip(DEFAULT_STATE.E, params.E_min, params.E_max),
        I=clip(DEFAULT_STATE.I, params.I_min, params.I_max),
        S=clip(DEFAULT_STATE.S, params.S_min, params.S_max),
        V=clip(DEFAULT_STATE.V, params.V_min, params.V_max),
    )

    max_steps = 4000
    dt = 0.2
    state_tol = 1e-10
    deriv_tol = 1e-9

    for _ in range(max_steps):
        next_state = compute_dynamics(
            state=state,
            delta_eta=delta_eta,
            theta=theta,
            params=params,
            dt=dt,
            noise_S=0.0,
            complexity=complexity,
            sensor_eisv=None,
        )
        max_delta = max(
            abs(next_state.E - state.E),
            abs(next_state.I - state.I),
            abs(next_state.S - state.S),
            abs(next_state.V - state.V),
        )
        state = next_state
        if max_delta < state_tol:
            break

    derivs = _derivatives(state, ethical_drift_norm_sq, theta, params, 0.0, complexity, None)
    if max(abs(d) for d in derivs) > deriv_tol:
        raise RuntimeError(
            "compute_equilibrium failed to converge to a fixed point: "
            f"state={state}, derivs={derivs}"
        )

    return state


def estimate_convergence(
    current: State,
    equilibrium: State,
    params: DynamicsParams,
    contraction_rate: float = 0.1,
    target_fraction: float = 0.05,
) -> dict:
    """
    Estimate time/updates to convergence.

    Uses exponential bound from contraction theory:
    ‖x(t) - x*‖ ≤ e^{-αt} ‖x(0) - x*‖

    Args:
        current: Current state
        equilibrium: Target equilibrium
        params: Dynamics parameters (for dt)
        contraction_rate: α from contraction analysis (default 0.1)
        target_fraction: Convergence threshold (default 0.05 = 95%)

    Returns:
        dict with distance, time_to_convergence, updates_to_convergence
    """
    import math

    # Compute distance to equilibrium
    distance = math.sqrt(
        (current.E - equilibrium.E)**2 +
        (current.I - equilibrium.I)**2 +
        (current.S - equilibrium.S)**2 +
        (current.V - equilibrium.V)**2
    )

    if distance < 1e-6:
        return {
            'distance': distance,
            'time_to_convergence': 0.0,
            'updates_to_convergence': 0,
            'converged': True,
        }

    # Time to reach target_fraction: e^{-αt} = target_fraction
    # t = -ln(target_fraction) / α
    dt = 0.1  # Default time step
    time_to_convergence = -math.log(target_fraction) / contraction_rate
    updates_to_convergence = int(math.ceil(time_to_convergence / dt))

    return {
        'distance': distance,
        'time_to_convergence': time_to_convergence,
        'updates_to_convergence': updates_to_convergence,
        'converged': False,
    }


def check_basin(state: State, threshold: float = 0.5) -> str:
    """
    Check which basin of attraction the state is in.

    The bistable UNITARES system has two basins:
    - 'high': I > threshold, converges to high equilibrium
    - 'low': I < threshold, converges to low equilibrium
    - 'boundary': I ≈ threshold, unstable region

    Args:
        state: Current state
        threshold: Basin boundary (default 0.5)

    Returns:
        'high', 'low', or 'boundary'
    """
    margin = 0.05
    if state.I > threshold + margin:
        return 'high'
    elif state.I < threshold - margin:
        return 'low'
    else:
        return 'boundary'


def compute_saturation_diagnostics(
    state: State,
    theta: Theta,
    params: Optional[DynamicsParams] = None,
) -> dict:
    """
    Compute I-channel saturation diagnostics.
    
    This is the "pressure gauge" for understanding boundary saturation behavior.
    Critical for monitoring system stability and validating dynamics mode choice.
    
    Args:
        state: Current UNITARES state
        theta: Control parameters
        params: Dynamics parameters (uses DEFAULT_PARAMS if None)
    
    Returns:
        dict with:
        - A: Forcing term (β_I·C - k·S)
        - gamma_over_4: Maximum logistic damping (γ_I/4)
        - sat_margin: A - γ_I/4 (positive = push-to-boundary in logistic mode)
        - I_equilibrium_linear: Predicted equilibrium under linear damping (A/γ_I)
        - I_equilibrium_logistic: Predicted equilibria under logistic (if they exist)
        - dynamics_mode: Current mode (linear/logistic)
        - will_saturate: Whether logistic mode will saturate to I=1
    """
    from .parameters import DEFAULT_PARAMS, get_i_dynamics_mode
    from .coherence import coherence
    
    if params is None:
        params = DEFAULT_PARAMS
    
    # Compute coherence
    C = coherence(state.V, theta, params)
    
    # Forcing term (isolated input)
    A = params.beta_I * C - params.k * state.S
    
    # Logistic damping maximum
    gamma_over_4 = params.gamma_I / 4.0
    
    # Saturation margin (the "smoking gun" metric)
    sat_margin = A - gamma_over_4
    
    # Linear equilibrium (always exists)
    I_eq_linear = A / params.gamma_I if params.gamma_I > 0 else float('inf')
    
    # Logistic equilibria (may not exist if sat_margin > 0)
    I_eq_logistic = []
    if sat_margin <= 0 and params.gamma_I > 0:
        # Quadratic: γ·I² - γ·I + (k·S - β·C) = 0
        # Roots: I = (1 ± sqrt(1 - 4A/γ)) / 2
        import math
        discriminant = 1 - 4 * A / params.gamma_I
        if discriminant >= 0:
            sqrt_d = math.sqrt(discriminant)
            I_low = (1 - sqrt_d) / 2
            I_high = (1 + sqrt_d) / 2
            if 0 <= I_low <= 1:
                I_eq_logistic.append(('stable_low', I_low))
            if 0 <= I_high <= 1:
                I_eq_logistic.append(('unstable_high', I_high))
    
    dynamics_mode = get_i_dynamics_mode()
    
    return {
        'A': A,
        'C': C,
        'S': state.S,
        'gamma_I': params.gamma_I,
        'gamma_over_4': gamma_over_4,
        'sat_margin': sat_margin,
        'I_current': state.I,
        'I_equilibrium_linear': min(1.0, max(0.0, I_eq_linear)),  # Clipped to valid range
        'I_equilibrium_logistic': I_eq_logistic,
        'dynamics_mode': dynamics_mode,
        'will_saturate': sat_margin > 0 and dynamics_mode == 'logistic',
        'at_boundary': state.I >= params.I_max - 0.001,
    }
