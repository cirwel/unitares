"""
UNITARES Governance Core - Coherence Functions (LEGACY THERMODYNAMIC FORM)

NOTE — terminology drift. The `coherence` field exposed in MCP responses
(`process_agent_update`, governance metrics, dashboards) is NO LONGER this
`C(V, Θ)`. As of EISV grounding Phase 1+2 (PR #26, merged 2026-04-19), the
canonical `coherence` slot in the runtime metrics dict is computed by
`src/grounding/coherence.py::compute_coherence` — a manifold-distance form
over (E, I, S) from a class-conditional healthy operating point. V is not
in that formula. The thermodynamic value computed here lives in metrics as
`coherence_legacy`.

Use this module when you specifically want the V-driven thermodynamic
coherence (e.g. ODE integration, drift telemetry baselines). For "is this
agent's state coherent" questions in handler/response code, read
`metrics["coherence"]` (manifold form) or `metrics["coherence_legacy"]`
(this form) — and be explicit about which one your code depends on.

See `src/mcp_handlers/updates/enrichments.py` for the swap site that
populates both fields, and the paper v6.8.1 §6.7 translation table for
the vocabulary mapping (paper ↔ runtime ↔ audit).

Coherence is a key feedback mechanism in UNITARES that stabilizes
the system. It depends on the void integral V and control parameters Θ.

Mathematical Definition (UNITARES v4.1 Section 3.4):
    C(V, Θ) = Cmax · 0.5 · (1 + tanh(Θ.C₁ · V))

    λ₁ = 0.3  (ethical drift into S)
    λ₂ = 0.05 (coherence coupling)

Physical Interpretation:
    - C(V, Θ) ∈ [0, Cmax] represents system coherence
    - When V → -∞: C → 0 (incoherent, I >> E)
    - When V → +∞: C → Cmax (coherent, E >> I)
    - Θ.C₁ controls the steepness of the transition
"""

import math
from .parameters import DynamicsParams, Theta


def coherence(V: float, theta: Theta, params: DynamicsParams) -> float:
    """
    Compute UNITARES coherence function (pure thermodynamic).

    C(V, Θ) = Cmax · 0.5 · (1 + tanh(Θ.C₁ · V))

    Args:
        V: Void integral (E-I imbalance accumulator)
        theta: Control parameters (C1, eta1)
        params: Dynamics parameters (for Cmax)

    Returns:
        Coherence value in [0, Cmax]

    Notes:
        - Coherence acts as a stabilizing feedback
        - Higher V (E > I) → higher coherence
        - Lower V (I > E) → lower coherence
        - C1 parameter controls transition steepness
        
    Physical Interpretation:
        - With V typically in [-0.1, 0.1] (actual operating range due to damping)
          and C1=1.0 (DEFAULT_THETA), coherence ranges approximately [0.45, 0.55]
        - Adaptive C1 is bounded [C1_min=0.5, C1_max=1.5]; across that range
          V=±0.1 yields C in roughly [0.43, 0.57] at the wide end
        - Mean V ≈ -0.016 with C1=1.0 → coherence ≈ 0.492
        - This reflects genuine thermodynamic state: I slightly > E (information-preserving)
        - The narrow V range is due to damping (δ=0.25 default, adaptive via governor)
          and conservative calibration

    Design Decision (2025-11-27):
        - Removed coherence_scale factor for accuracy
        - Accept ≈0.49 coherence as honest thermodynamic signal
        - Coherence function designed for V ∈ [-2, 2] but dynamics keep V ∈ [-0.1, 0.1]
        - This is correct: system genuinely operates conservatively (I > E)
    """
    return params.Cmax * 0.5 * (1.0 + math.tanh(theta.C1 * V))


def lambda1(theta: Theta, params: DynamicsParams, lambda1_min: float = 0.05, lambda1_max: float = 0.20) -> float:
    """
    Compute λ₁ parameter (adaptive via theta.eta1).

    λ₁ is now adaptive via theta.eta1, mapped to operational range [lambda1_min, lambda1_max].
    
    Mapping: eta1 ∈ [0.1, 0.5] → lambda1 ∈ [lambda1_min, lambda1_max]
    Default range: [0.05, 0.20] per UNITARES operational bounds.

    This parameter controls how much ethical drift increases
    semantic uncertainty S.

    Args:
        theta: Control parameters (eta1 controls lambda1 adaptation)
        params: Dynamics parameters (for lambda1_base - used as fallback)
        lambda1_min: Minimum lambda1 value (default: 0.05)
        lambda1_max: Maximum lambda1 value (default: 0.20)

    Returns:
        λ₁ value (drift → S coupling strength) in [lambda1_min, lambda1_max]

    Notes:
        - Adaptive lambda1 via PI controller (enables adaptive control)
        - Maps theta.eta1 [0.1, 0.5] → lambda1 [lambda1_min, lambda1_max]
        - Linear mapping: lambda1 = lambda1_min + (eta1 - 0.1) / (0.5 - 0.1) * (lambda1_max - lambda1_min)
        - Falls back to lambda1_base if eta1 outside expected range
        
    Historical:
        - 2025-11-26: Fixed bug where eta1 was incorrectly multiplied (0.3 * 0.3 = 0.09)
        - 2025-11-28: Made adaptive via eta1 mapping to enable PI controller adaptation
    """
    # Map eta1 [0.1, 0.5] → lambda1 [lambda1_min, lambda1_max]
    # Linear interpolation
    eta1_min = 0.1
    eta1_max = 0.5
    eta1_range = eta1_max - eta1_min
    lambda1_range = lambda1_max - lambda1_min
    
    # Clamp eta1 to expected range
    eta1_clamped = max(eta1_min, min(eta1_max, theta.eta1))
    
    # Linear mapping
    if eta1_range > 0:
        normalized_eta1 = (eta1_clamped - eta1_min) / eta1_range
        adaptive_lambda1 = lambda1_min + normalized_eta1 * lambda1_range
    else:
        # Fallback if range is zero
        adaptive_lambda1 = params.lambda1_base
    
    return adaptive_lambda1


def lambda2(theta: Theta, params: DynamicsParams, lambda2_min: float = 0.02, lambda2_max: float = 0.10) -> float:
    """
    Compute λ₂ parameter (adaptive via theta.eta2).

    λ₂ controls how much coherence reduces semantic uncertainty S.
    Now adaptive via theta.eta2, mapped to [lambda2_min, lambda2_max].

    Mapping: eta2 ∈ [0.1, 0.5] → lambda2 ∈ [lambda2_min, lambda2_max]
    Default range: [0.02, 0.10] — conservative, centered around lambda2_base=0.05.

    Args:
        theta: Control parameters (eta2 controls lambda2 adaptation)
        params: Dynamics parameters (for lambda2_base as fallback)
        lambda2_min: Minimum lambda2 value (default: 0.02)
        lambda2_max: Maximum lambda2 value (default: 0.10)

    Returns:
        λ₂ value (coherence → S reduction strength) in [lambda2_min, lambda2_max]
    """
    eta2 = getattr(theta, 'eta2', None)
    if eta2 is None:
        return params.lambda2_base

    eta2_min = 0.1
    eta2_max = 0.5
    eta2_range = eta2_max - eta2_min
    lambda2_range = lambda2_max - lambda2_min

    eta2_clamped = max(eta2_min, min(eta2_max, eta2))

    if eta2_range > 0:
        normalized = (eta2_clamped - eta2_min) / eta2_range
        return lambda2_min + normalized * lambda2_range

    return params.lambda2_base
