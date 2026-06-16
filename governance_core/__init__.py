"""
UNITARES Governance Core — EISV ODE dynamics (secondary / diagnostic path)

Despite the name, this is **not the primary verdict-driving path** in the
runtime pipeline. It implements the coupled-ODE evolution of the four EISV
variables (E, I, S, V) with contraction-style stability analysis, running
in parallel for analysis and as a warmup-phase fallback.

Primary verdict-driving path: Behavioral EISV in `src/behavioral_state.py`
and `src/behavioral_assessment.py` — EMA-smoothed observations with
per-agent Welford baselines; no ODE, no universal attractor.

Governance verdicts normally follow behavioral EISV once per-agent
confidence is established. This ODE is the fallback when behavioral
confidence is still insufficient (early check-ins) or is explicitly
disabled via ``UNITARES_DISABLE_ODE=1``.

See README §"How state works (EISV)" and docs/UNIFIED_ARCHITECTURE.md for
the full pipeline (drift → entropy, calibration, circuit breaker, dialectic).

Authoritative version is ``__version__`` at the end of this module.
"""

from .dynamics import (
    State,
    DynamicsParams,
    compute_dynamics,
    step_state,
    compute_saturation_diagnostics,
    eisv_divergence,
)

from .coherence import (
    coherence,
    lambda1,
    lambda2,
)

from .scoring import (
    phi_objective,
    verdict_from_phi,
)

from .parameters import (
    Theta,
    Weights,
    DEFAULT_PARAMS,
    DEFAULT_WEIGHTS,
    DEFAULT_THETA,
    get_i_dynamics_mode,
    get_integrator_mode,
)

from .dynamics import DEFAULT_STATE

from .utils import (
    clip,
    drift_norm,
)

from .ethical_drift import (
    EthicalDriftVector,
    AgentBaseline,
    compute_ethical_drift,
    get_agent_baseline,
    clear_baseline,
    get_all_baselines,
    set_agent_baseline,
    get_baseline_or_none,
)

from .adaptive_governor import (
    AdaptiveGovernor,
    GovernorConfig,
    GovernorState,
    Verdict,
)

from .phase_aware import (
    Phase,
    detect_phase,
    get_phase_aware_thresholds,
)

from .research import (
    approximate_stability_check,
    suggest_theta_update,
)

from .stability import (
    compute_jacobian,
    verify_lyapunov_stability,
    gershgorin_stability_bound,
    sweep_stability,
    optimize_stability_metric,
)

__all__ = [
    # Core state and dynamics
    'State',
    'DynamicsParams',
    'compute_dynamics',
    'step_state',
    'compute_saturation_diagnostics',
    'eisv_divergence',

    # Coherence functions
    'coherence',
    'lambda1',
    'lambda2',

    # Scoring functions
    'phi_objective',
    'verdict_from_phi',

    # Parameters
    'Theta',
    'Weights',
    'DEFAULT_PARAMS',
    'DEFAULT_WEIGHTS',
    'DEFAULT_THETA',
    'DEFAULT_STATE',
    'get_i_dynamics_mode',
    'get_integrator_mode',

    # Utilities
    'clip',
    'drift_norm',

    # Ethical Drift (concrete Δη)
    'EthicalDriftVector',
    'AgentBaseline',
    'compute_ethical_drift',
    'get_agent_baseline',
    'clear_baseline',
    'get_all_baselines',
    'set_agent_baseline',
    'get_baseline_or_none',

    # Phase-Aware
    'Phase',
    'detect_phase',
    'get_phase_aware_thresholds',

    # CIRS v2 Adaptive Governor
    'AdaptiveGovernor',
    'GovernorConfig',
    'GovernorState',
    'Verdict',

    # Research tools
    'approximate_stability_check',
    'suggest_theta_update',

    # Stability verification (Lyapunov)
    'compute_jacobian',
    'verify_lyapunov_stability',
    'gershgorin_stability_bound',
    'sweep_stability',
    'optimize_stability_metric',
]

__version__ = '2.5.0'  # Lyapunov stability verification
