"""
UNITARES Governance Core - Scoring Functions

Objective function Φ (phi) for evaluating governance quality.

Mathematical Definition:
    Φ = wE·E - wI·(1-I) - wS·S - wV·|V| - wEta·‖Δη‖²

Interpretation:
    - Positive Φ → good governance state
    - Negative Φ → problematic state
    - Φ balances multiple competing objectives

Verdict Thresholds:
    Φ ≥ 0.08 → "safe"
    Φ ≥ 0.0  → "caution"
    Φ < 0.0  → "high-risk"
"""

from typing import List
from .dynamics import State
from .parameters import Weights, DEFAULT_WEIGHTS
from .utils import drift_norm


def phi_objective(
    state: State,
    delta_eta: List[float],
    weights: Weights = DEFAULT_WEIGHTS,
) -> float:
    """
    Compute UNITARES objective function Φ.

    Φ = wE·E - wI·(1-I) - wS·S - wV·|V| - wEta·‖Δη‖²

    Args:
        state: Current UNITARES state (E, I, S, V)
        delta_eta: Ethical drift vector
        weights: Objective weights (wE, wI, wS, wV, wEta)

    Returns:
        Φ score (higher is better)

    Interpretation:
        - Φ rewards high E (energy/exploration capacity)
        - Φ rewards high I (information integrity)
        - Φ penalizes high S (semantic uncertainty)
        - Φ penalizes high |V| (E-I imbalance)
        - Φ penalizes high ‖Δη‖ (ethical drift)

    Notes:
        - DEPLOYED REALITY (2026-08-06): this function is NOT research-only. It is
          the live cold-start / pre-warmup verdict authority — `src/monitor_phi.py`
          calls it every check-in, and it owns the verdict for updates 1-2 and every
          cold start (it produced the one live high-risk verdict, 2026-08-02). The
          older "production uses coherence-based decision making" note is stale:
          coherence sits in ~[0.455, 0.499] as deployed and its gates cannot fire
          (see docs/ontology/eisv-proprioception-contract.md, rows 16, 25, 39).
        - The verdict this produces is ADVISORY as deployed, not enforcement — a
          produced `high-risk` is usually gap-suppressed, not delivered.
    """
    d_eta = drift_norm(delta_eta)

    phi = (
        weights.wE * state.E                    # Reward energy/exploration capacity
        - weights.wI * (1.0 - state.I)          # Reward information integrity
        - weights.wS * state.S                  # Penalize uncertainty
        - weights.wV * abs(state.V)             # Penalize imbalance
        - weights.wEta * d_eta * d_eta          # Penalize drift
    )

    return phi


def verdict_from_phi(phi: float, safe_threshold: float = 0.08, caution_threshold: float = 0.0) -> str:
    """
    Convert Φ score to verdict category.

    Thresholds (configurable):
        Φ ≥ safe_threshold (default 0.08)  → "safe"
        Φ ≥ caution_threshold (default 0.0) → "caution"
        Φ < caution_threshold              → "high-risk"

    Args:
        phi: Φ objective score
        safe_threshold: Threshold for "safe" verdict (default 0.08)
        caution_threshold: Threshold for "caution" verdict (default 0.0)

    Returns:
        Verdict string: "safe", "caution", or "high-risk"

    Notes:
        - These thresholds are heuristic and tunable
        - "safe" suggests proceeding normally
        - "caution" suggests proceeding with safeguards (returns a guide, not a pause)
        - "high-risk" produces a pause VERDICT — advisory as deployed, usually
          gap-suppressed and not delivered (see docs/ontology/eisv-proprioception-
          contract.md, "Deployed posture"); not an enforced halt or human review by
          itself
        - Steady-state margin: the legacy note "phi≈0.11" is for the setpoint-free
          equilibrium; with UNITARES_S_SETPOINT on (the live default since #1133),
          deployed Φ rests ≈0.26, so the margin to the "caution" edge is ~2× wider
          than this note implies
    """
    if phi >= safe_threshold:
        return "safe"
    elif phi >= caution_threshold:
        return "caution"
    else:
        return "high-risk"
