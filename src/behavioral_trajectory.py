"""Behavioral trajectory: compute TrajectorySignature from governance history.

Pure function — no imports from governance modules. Takes extracted history lists
and returns a dict compatible with TrajectorySignature.from_dict().
"""

import math
from datetime import datetime, timezone
from typing import Dict, List, Optional


LEGACY_COHERENCE_IDENTITY_ABLATION_SCHEMA = (
    "legacy_coherence_identity_ablation.v1"
)


def compute_behavioral_trajectory(
    E_history: list,
    I_history: list,
    S_history: list,
    V_history: list,
    coherence_history: list,
    decision_history: list,
    regime_history: list,
    update_count: int,
    task_type_counts: dict | None = None,
    calibration_error: float | None = None,
) -> dict | None:
    """Compute behavioral trajectory signature from governance observables.

    Returns a dict compatible with TrajectorySignature.from_dict(), or None
    if insufficient history (< 10 entries).
    """
    if len(E_history) < 10 or len(coherence_history) < 10:
        return None

    preferences = _compute_preferences(decision_history, task_type_counts)
    beliefs = _compute_beliefs(E_history, I_history, S_history, V_history, calibration_error)
    attractor = _compute_attractor(E_history, I_history, S_history, V_history)
    recovery = _compute_recovery(coherence_history)
    relational = _compute_relational(decision_history, task_type_counts, regime_history)

    # Eta: Homeostatic Identity (paper Definition 3.6)
    homeostatic = None
    if attractor and recovery:
        homeostatic = {
            "set_point": attractor.get("center"),
            "basin_shape": attractor.get("covariance"),
            "recovery_tau": recovery.get("tau_estimate"),
            "viability_bounds": {
                "E": (0.1, 0.9), "I": (0.3, 1.0),
                "S": (0.0, 0.6), "V": (-0.2, 0.15),
            },
        }

    stability = _compute_stability(coherence_history)
    confidence = min(1.0, update_count / 200.0) * stability

    return {
        "preferences": preferences,
        "beliefs": beliefs,
        "attractor": attractor,
        "recovery": recovery,
        "relational": relational,
        "homeostatic": homeostatic,
        "observation_count": update_count,
        "stability_score": stability,
        "identity_confidence": confidence,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def compute_legacy_coherence_identity_shadow(
    *,
    coherence_history: list,
    update_count: int,
    eisv_history_count: int,
) -> dict:
    """Measure the legacy ``C(V)`` dependency in trajectory identity.

    The deployed trajectory is left untouched.  The shadow treats every field
    derived solely from legacy coherence as unavailable evidence instead of
    replacing a degenerate signal with another constant.  For visibility it
    also reports the maturity-only factor that remains when the legacy
    stability multiplier is removed; that number is not a candidate identity
    policy and is never persisted as a trajectory signature.
    """
    base = {
        "schema": LEGACY_COHERENCE_IDENTITY_ABLATION_SCHEMA,
        "mode": "measurement_only",
        "policy_applied": False,
        "health_evidence": False,
        "intervention": {
            "field": "coherence_history",
            "source": "legacy_tanh_v",
            "role": "ode_control_feedback",
            "operation": "remove_from_identity_evidence",
            "affected_fields": [
                "recovery.tau_estimate",
                "homeostatic.recovery_tau",
                "stability_score",
                "identity_confidence.stability_multiplier",
            ],
        },
        "not_modeled": [
            "counterfactual_trajectory_signature",
            "genesis_or_current_signature_replay",
            "trajectory_similarity",
            "trust_tier",
            "risk_adjustment",
            "candidate_signature_persistence_or_alerts",
            "future_outcomes",
        ],
    }
    if len(coherence_history) < 10 or eisv_history_count < 10:
        return {
            **base,
            "eligible": False,
            "eligibility_reason": "insufficient_trajectory_history",
            "deployed": None,
            "candidate": None,
            "candidate_minus_deployed": None,
        }

    stability = _compute_stability(coherence_history)
    maturity_factor = min(1.0, max(0, update_count) / 200.0)
    deployed_confidence = maturity_factor * stability
    recovery_tau = _compute_recovery(coherence_history).get("tau_estimate")
    candidate_confidence = maturity_factor

    return {
        **base,
        "eligible": True,
        "eligibility_reason": None,
        "deployed": {
            "recovery_tau": recovery_tau,
            "stability_score": stability,
            "identity_confidence": deployed_confidence,
            "maturity_factor": maturity_factor,
        },
        "candidate": {
            "recovery_tau": None,
            "stability_score": None,
            "identity_confidence_without_legacy_stability_multiplier": (
                candidate_confidence
            ),
            "maturity_factor": maturity_factor,
            "removed_similarity_evidence": [
                "recovery",
                "homeostatic.recovery_tau",
            ],
        },
        "candidate_minus_deployed": {
            "identity_confidence_without_legacy_stability_multiplier": (
                candidate_confidence - deployed_confidence
            )
        },
    }


# --- Preferences (Π): task-type distribution + decision bias ---

_DECISION_CATEGORIES = {
    "proceed": "proceed", "approve": "proceed",
    "guide": "guide",
    "revise": "guide", "reflect": "guide",
    "pause": "pause", "reject": "pause",
}


def _compute_preferences(decision_history: list, task_type_counts: dict | None) -> dict:
    # Task-type distribution
    tt_dist = {}
    if task_type_counts:
        total = sum(task_type_counts.values())
        if total > 0:
            tt_dist = {k: v / total for k, v in task_type_counts.items()}

    # Decision bias (last 20)
    window = decision_history[-20:]
    counts = {"proceed": 0, "guide": 0, "pause": 0}
    for d in window:
        cat = _DECISION_CATEGORIES.get(str(d).lower(), "guide")
        counts[cat] += 1
    total = sum(counts.values()) or 1
    bias = {k: v / total for k, v in counts.items()}

    return {"task_type_distribution": tt_dist, "decision_bias": bias}


# --- Beliefs (B): EISV center-of-gravity as values vector ---

def _compute_beliefs(
    E_history: list, I_history: list, S_history: list, V_history: list,
    calibration_error: float | None,
) -> dict:
    window = 20
    e = E_history[-window:]
    i = I_history[-window:]
    s = S_history[-window:]
    v = V_history[-window:]

    values = [
        sum(e) / len(e),
        sum(i) / len(i),
        sum(s) / len(s),
        sum(v) / len(v),
    ]

    conf = 1.0 - calibration_error if calibration_error is not None else 0.5
    conf = max(0.0, min(1.0, conf))

    return {"values": values, "confidence": conf}


# --- Attractor (A): EISV center + radius ---

def _compute_attractor(
    E_history: list, I_history: list, S_history: list, V_history: list,
) -> dict:
    window = 20
    e = E_history[-window:]
    i = I_history[-window:]
    s = S_history[-window:]
    v = V_history[-window:]

    center = [
        sum(e) / len(e),
        sum(i) / len(i),
        sum(s) / len(s),
        sum(v) / len(v),
    ]

    # Mean Euclidean distance from center
    n = min(len(e), len(i), len(s), len(v))
    distances = []
    for j in range(n):
        d = math.sqrt(
            (e[j] - center[0]) ** 2
            + (i[j] - center[1]) ** 2
            + (s[j] - center[2]) ** 2
            + (v[j] - center[3]) ** 2
        )
        distances.append(d)
    radius = sum(distances) / len(distances) if distances else 0.0

    # 4x4 covariance matrix with epsilon regularization (for Bhattacharyya)
    covariance = None
    if n >= 5:
        data = list(zip(e[:n], i[:n], s[:n], v[:n]))
        covariance = _compute_covariance_4x4(data, center)

    return {"center": center, "radius": radius, "covariance": covariance}


def _compute_covariance_4x4(data: list, center: list) -> list:
    """Compute 4x4 sample covariance matrix with epsilon regularization."""
    n = len(data)
    cov = [[0.0] * 4 for _ in range(4)]
    for point in data:
        for r in range(4):
            for c in range(4):
                cov[r][c] += (point[r] - center[r]) * (point[c] - center[c])
    for r in range(4):
        for c in range(4):
            cov[r][c] /= n
    # Epsilon regularization for numerical stability
    for r in range(4):
        cov[r][r] += 1e-6
    return cov


# --- Recovery (R): coherence recovery time constant ---

def _compute_recovery(coherence_history: list) -> dict:
    """Estimate recovery tau from coherence dip/recovery cycles.

    Thresholds are set wide enough (dip < 0.46, recover >= 0.49) to avoid
    noise from agents oscillating near steady-state coherence (~0.49).
    Dips must persist for 2+ consecutive steps to count.
    """
    dip_threshold = 0.46
    recovery_threshold = 0.49
    min_dip_duration = 2  # Must stay below threshold for 2+ steps
    recovery_times = []

    in_dip = False
    dip_start = 0
    consecutive_below = 0

    for idx, c in enumerate(coherence_history):
        if c < dip_threshold:
            consecutive_below += 1
            if not in_dip and consecutive_below >= min_dip_duration:
                in_dip = True
                dip_start = idx - min_dip_duration + 1
        else:
            consecutive_below = 0
            if in_dip and c >= recovery_threshold:
                recovery_times.append(idx - dip_start)
                in_dip = False

    if recovery_times:
        tau = sum(recovery_times) / len(recovery_times)
    else:
        tau = 3.0  # Default: healthy agent, no dips observed

    return {"tau_estimate": tau}


# --- Relational (Δ): behavioral signals for non-embodied agents ---

_DECISION_VALENCE = {
    "proceed": 1.0, "approve": 1.0,
    "guide": 0.0,
    "revise": -0.3, "reflect": -0.3,
    "pause": -0.7, "reject": -1.0,
}


def _compute_relational(
    decision_history: list,
    task_type_counts: dict | None,
    regime_history: list,
) -> dict:
    """Compute relational disposition from behavioral signals.

    Maps governance observables to the paper's Delta components:
    - valence_tendency: decision pattern as social stance [-1, 1]
    - topic_entropy: task-type diversity (Shannon entropy)
    - bonding_tendency: regime stability (proportion of stable/convergence)
    """
    # Valence from decision history
    window = decision_history[-20:]
    if window:
        vals = [_DECISION_VALENCE.get(str(d).lower(), 0.0) for d in window]
        valence = sum(vals) / len(vals)
    else:
        valence = 0.0

    # Topic entropy from task-type distribution
    topic_entropy = 0.0
    if task_type_counts:
        total = sum(task_type_counts.values())
        if total > 0:
            for count in task_type_counts.values():
                p = count / total
                if p > 0:
                    topic_entropy -= p * math.log(p)

    # Bonding tendency from regime stability
    bonding = 0.5  # Default neutral
    regime_window = regime_history[-20:]
    if regime_window:
        stable_count = sum(
            1 for r in regime_window
            if str(r).upper() in ("STABLE", "CONVERGENCE")
        )
        bonding = stable_count / len(regime_window)

    return {
        "valence_tendency": max(-1.0, min(1.0, valence)),
        "bonding_tendency": bonding,
        "topic_entropy": round(topic_entropy, 4),
        "agent_type": "non_embodied",
    }


# --- Stability score ---

def _compute_stability(coherence_history: list) -> float:
    """1.0 - std(recent coherence) * 5, clipped [0, 1]."""
    window = coherence_history[-20:]
    if len(window) < 2:
        return 0.5

    mean = sum(window) / len(window)
    variance = sum((x - mean) ** 2 for x in window) / len(window)
    std = math.sqrt(variance)

    return max(0.0, min(1.0, 1.0 - std * 5.0))


# --- Forward Projection ---

def project_eisv_trajectory(
    E_history: List[float],
    I_history: List[float],
    S_history: List[float],
    V_history: List[float],
    steps: int = 5,
    dt: float = 0.1,
) -> Optional[Dict]:
    """Project EISV forward using ODE dynamics from current state.

    Uses governance_core.step_state for physics-based projection with
    zero drift input (assumes no new perturbation). Falls back to
    EWMA extrapolation if governance_core is unavailable.

    Args:
        E_history, I_history, S_history, V_history: Recent EISV histories
        steps: Number of steps to project forward
        dt: Time step for projection

    Returns:
        Dict with projected E/I/S/V lists and crossing warnings, or None
        if insufficient history.
    """
    if not E_history or len(E_history) < 3:
        return None

    current = {
        "E": E_history[-1],
        "I": I_history[-1],
        "S": S_history[-1],
        "V": V_history[-1],
    }

    projected = {"E": [], "I": [], "S": [], "V": []}

    try:
        from governance_core import step_state, State, DEFAULT_THETA

        state = State(
            E=current["E"], I=current["I"],
            S=current["S"], V=current["V"],
        )
        for _ in range(steps):
            state = step_state(
                state, DEFAULT_THETA,
                delta_eta=[0.0, 0.0, 0.0],
                dt=dt, noise_S=0.0,
            )
            projected["E"].append(round(state.E, 4))
            projected["I"].append(round(state.I, 4))
            projected["S"].append(round(state.S, 4))
            projected["V"].append(round(state.V, 4))
    except ImportError:
        # Fallback: EWMA extrapolation toward attractor
        attractor = _compute_attractor(E_history, I_history, S_history, V_history)
        center = attractor["center"]
        decay = 0.9  # Exponential decay toward center per step

        vals = [current["E"], current["I"], current["S"], current["V"]]
        for _ in range(steps):
            vals = [v + (c - v) * (1 - decay) for v, c in zip(vals, center)]
            projected["E"].append(round(vals[0], 4))
            projected["I"].append(round(vals[1], 4))
            projected["S"].append(round(vals[2], 4))
            projected["V"].append(round(vals[3], 4))

    # Detect concerning trends in projection
    warnings = []
    if projected["S"] and projected["S"][-1] > 0.5:
        warnings.append("entropy_rising")
    if projected["I"] and projected["I"][-1] < 0.4:
        warnings.append("integrity_falling")
    if projected["V"] and abs(projected["V"][-1]) > 0.5:
        warnings.append("void_accumulating")

    return {
        "current": current,
        "projected": projected,
        "steps": steps,
        "dt": dt,
        "warnings": warnings,
    }
