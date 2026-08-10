"""
UNITARES Governance Core - Coherence Functions (LEGACY THERMODYNAMIC FORM)

NOTE — terminology drift, and a correction. An earlier version of this note
claimed the `coherence` field exposed in MCP responses is "NO LONGER this
C(V, Θ)" as of EISV grounding Phase 1+2 (PR #26). **That is not true as
deployed, and was not true when it was written.** The swap it describes is
gated behind `UNITARES_GROUNDING_APPLY`, which is OFF by default and off in
production: `run_grounding_stage` computes the grounded value, audits the
delta as a `grounding_shadow` event, then RESTORES the ungrounded one and
drops `coherence_legacy`/`coherence_source` entirely
(`src/mcp_handlers/updates/enrichments.py`). So the value in MCP responses
and in `core.agent_state.coherence` is THIS function.

Read the row's `state_json.coherence_form` to know which form produced a
stored value ("legacy_tanh_v" here, "manifold" under APPLY); rows written
before that tag shipped, or with both grounding flags off, carry no tag.

The manifold-distance form over (E, I, S) lives in
`src/grounding/coherence.py::compute_coherence` and becomes canonical only
when APPLY is enabled. It is not a drop-in: see the degeneracy note below.

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

    ⛔ THE 2025-11 RATIONALE ABOVE IS OBSOLETE — it was overtaken by 69ee5a79
    (2026-04-01) and is kept only to show what the reasoning was:

        "Promote behavioral EISV to primary metrics, demote ODE to diagnostic.
         The ODE attractor convergence made all agents look identical
         regardless of actual behavior."

        That commit swapped E, I, S and V in the surfaced metrics dict to the
        behavioral (EMA + Welford) values and moved the ODE values to a
        `metrics['ode']` diagnostic sub-field. **Coherence was not part of the
        swap** — verified: the commit changes zero lines mentioning coherence.
        `governance_monitor.py` still computes it as
        `coherence(self.state.V, ...)`, i.e. from the ODE V that the same
        commit demoted.

        So this function is the last surfaced field still reporting the ODE
        attractor, and "all agents look identical" is still true of it alone.
        Accepting ≈0.49 as an "honest thermodynamic signal" was defensible in
        2025-11, when the ODE V *was* the signal. After April it is a defense
        of a number nobody decided to keep.

    Measured consequence (2026-08-10, live `core.agent_state` over 7d, n=6553):
        coherence range [0.4696, 0.5039], sd 0.0077 — the doctor's
        `signal_degeneracy` check flags it, correctly. Compare the
        between-agent spread (16 agents, >=20 check-ins): V 0.128, S 0.046,
        I 0.035, coherence **0.0032**. Coherence collapses the coordinate with
        the widest genuine spread of the four.

        The persisted V is NOT confined to [-0.1, 0.1] as claimed above: live
        range [-0.619, +0.045], sd 0.218. That band describes the ODE V, which
        is what this function reads — not the V we store. The two are visibly
        different variables: corr(coherence, stored V) is ~0.99 for agents
        whose behavioral V sits near the ODE attractor and falls to 0.15 for
        those that drift away from it. At V=-0.48 this function cannot return
        the observed 0.482 for ANY legal C1 in [0.5, 1.5] (the range is
        [0.192, 0.382]) — proof they are not the same input.

        This matters because a live gate is placed on it:
        `AdaptiveGovernor.make_verdict` hard-blocks on
        `coherence < tau_floor` (0.25). Nothing in the range above can reach
        0.25, so that branch has never fired — 0 hard-blocks in 5330 sampled
        check-ins. `AdaptiveGovernor.update` also drives its tau PID off
        `e_tau = tau_ref - coherence` (tau_ref 0.38/0.44), which against a
        value pinned at ~0.484 is a near-constant error term.

        Do NOT respond by lowering tau_floor to make the gate fire; that fits
        a threshold to a signal with no information in it.

        There are two candidate repairs, and BOTH hit the same wall:
          * finish 69ee5a79 — feed this function the primary (behavioral) V
            instead of the demoted ODE V. Recomputing C from the stored V at
            C1=1 gives sd 0.101 (13x today) and puts 7.1% below tau_floor.
          * enable UNITARES_GROUNDING_APPLY — the manifold form, sd 0.285
            (37x today), puts 18.09% below tau_floor.

        Either way a gate that has never fired starts firing, because
        tau_floor was calibrated while coherence was frozen by an incomplete
        migration. The threshold has to be re-derived against whichever
        distribution is adopted BEFORE that repair lands, and that derivation
        needs outcome evidence rather than a chosen alarm rate.
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
