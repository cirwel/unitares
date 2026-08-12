"""Shadow-measure a behavioral-V deviation gate against legacy coherence gates.

Measurement only. Nothing here mutates a decision, a verdict, or a threshold —
it records what a per-agent gate WOULD have said next to what the fleet-constant
gate actually said, so the two can be compared on real traffic before anything
is changed. Same discipline as ``grounding_shadow``.

Why this exists (docs/proposals/coherence-proprioceptive-thresholds-v0.md):

`69ee5a79` promoted E/I/S/V to behavioral values because "the ODE attractor
convergence made all agents look identical", but left coherence reading the
demoted ODE V. Four gates were then calibrated against that frozen signal. If
coherence is recomputed from the primary V, per-agent means span 0.2848-0.5201
with between-agent sd 0.0798 against a within-agent sd averaging 0.0265 -- about
3:1. On a signal like that a fleet constant does not measure deviation, it
measures identity: today's COHERENCE_CRITICAL_THRESHOLD of 0.40 would put two
live agents at 100% violation while three others could never trip it.

The v1 shadow made a category error: because C(V) is monotone, it treated only
negative V deviations as lower health. Monotonicity preserves order; it does
not turn the sign of imbalance into quality. Positive means running hot and
negative means running careful, so both directions are information. v2 uses
the magnitude of V's deviation from the agent's own recent operating point.

The statistic is deliberately:
  * two-sided -- neither sign is declared healthier;
  * recent-window -- old observations cannot desensitize a long-lived agent;
  * leave-current-out -- the observation being scored cannot move its own mean
    or variance before it is scored;
  * explicit about scale -- when the empirical standard deviation is below the
    calibrated floor, telemetry says ``scale_source=floor`` and does not call
    the resulting units empirical sigma.

``is_baselined`` remains a first maturity gate. A second gate requires enough
post-restart recent history to estimate dispersion; ineligible rows are emitted
explicitly and can never be counted as agreement.
"""

from __future__ import annotations

import os
import statistics
from typing import Any, Dict, Optional

from src.behavioral_state import eisv_min_std_for_dimension
from src.coherence_provenance import (
    LEGACY_COHERENCE_SOURCE,
    ODE_CONTROL_FEEDBACK_ROLE,
)
from src.logging_utils import get_logger

logger = get_logger(__name__)

# k per severity tier, preserving the ordering of the gates they shadow.
# STATED TOLERANCES, not derived constants -- see the proposal correction.
# They were inherited from v1 only as candidate shadow tiers; v2's bounded
# window and optional scale floor change their empirical firing rates. Never
# translate them into Gaussian tail probabilities. The shadow data exists to
# revise or reject them before any policy use.
K_PAUSE = 3.0    # shadows COHERENCE_CRITICAL_THRESHOLD (0.40) -> coherence_pause
K_BLOCK = 4.0    # shadows CIRS tau_low (0.30) -> hard block
K_FLOOR = 5.0    # shadows AdaptiveGovernor tau_floor (0.25) -> hard block

# Bounded, leave-current-out baseline. BehavioralEISV retains at most 100
# values; 60 keeps the statistic recent while leaving enough samples for a
# useful dispersion estimate. These are shadow calibration choices, not claims
# about a universal physiological timescale.
RECENT_WINDOW = 60
RECENT_MIN_SAMPLES = 30
STATISTIC_VERSION = "behavioral_v_recent_two_sided_v2"
COMPARISON_VERSION = "coherence_cause_attribution_v2"


def coherence_gate_shadow_enabled() -> bool:
    """Whether to record the behavioral-V-vs-legacy-gate comparison.

    UNITARES_COHERENCE_GATE_SHADOW, default off. Measurement only; there is no
    corresponding APPLY flag, because applying a per-agent gate requires the
    signal repair to land in the same change (proposal section 6) and is not
    something a flag should be able to do on its own.
    """
    return os.getenv("UNITARES_COHERENCE_GATE_SHADOW", "").strip().lower() in {
        "1", "true", "on", "yes",
    }


def _recent_v_score(behavioral: Any) -> tuple[Optional[float], Dict[str, Any]]:
    """Score current V against prior values in a bounded recent window."""
    history = list(getattr(behavioral, "V_history", ()) or ())
    current = float(getattr(behavioral, "V", history[-1] if history else 0.0))
    prior = [float(value) for value in history[:-1]][-RECENT_WINDOW:]
    meta: Dict[str, Any] = {
        "current_v": current,
        "sample_count": len(prior),
        "sample_mean": None,
        "sample_std": None,
        "effective_scale": None,
        "scale_source": None,
        "window": {"max_samples": RECENT_WINDOW, "min_samples": RECENT_MIN_SAMPLES},
    }
    if len(prior) < RECENT_MIN_SAMPLES:
        return None, meta

    mean = statistics.fmean(prior)
    sample_std = statistics.stdev(prior)
    floor = eisv_min_std_for_dimension(
        "V", getattr(behavioral, "alphas", None)
    )
    effective_scale = max(sample_std, floor)
    meta.update({
        "sample_mean": mean,
        "sample_std": sample_std,
        "effective_scale": effective_scale,
        "scale_source": "sample_std" if sample_std >= floor else "floor",
    })
    if effective_scale <= 0.0:  # defensive; configured floor is positive
        return None, meta
    return (current - mean) / effective_scale, meta


def _fleet_coherence_gate_attribution(
    action: Optional[str],
    sub_action: Optional[str],
    nearest_edge: Optional[str],
) -> tuple[Optional[bool], str]:
    """Say whether the chosen fleet action was caused by a coherence gate.

    ``decision.action`` alone is insufficient: every pause used to be counted
    as a coherence-gate firing, including risk, void, and basin pauses.  That
    made the shadow's agreement label measure "did both systems pause for any
    reason?" rather than the question it claims to answer.

    The result is tri-state.  ``None`` means an old or incomplete decision did
    not carry enough causal detail; analytics must exclude it from agreement
    rates instead of guessing.
    """
    action_value = (action or "").strip().lower()
    sub_action_value = (sub_action or "").strip().lower()
    edge_value = (nearest_edge or "").strip().lower()

    # Compatibility with v1 callers that supplied a sub-action in the action
    # slot.  New runtime calls always pass the binary action plus its qualifier.
    if action_value == "coherence_pause":
        return True, "legacy_coherence_action_label"

    if action_value in {"proceed", "approve", "guide", "continue"}:
        return False, "fleet_proceeded"

    if sub_action_value == "coherence_pause":
        return True, "coherence_pause"

    if sub_action_value == "cirs_block":
        if edge_value == "coherence":
            return True, "cirs_coherence_floor"
        if edge_value:
            return False, "cirs_non_coherence"
        return None, "cirs_cause_unknown"

    if action_value in {"pause", "reject"}:
        if sub_action_value in {
            "void_pause",
            "basin_pause",
            "risk_pause",
            "reject",
        }:
            return False, "non_coherence_pause"
        return None, "pause_cause_unknown"

    return None, "decision_cause_unknown"


def evaluate(
    behavioral: Any,
    fleet_action: Optional[str],
    coherence: Optional[float] = None,
    fleet_sub_action: Optional[str] = None,
    fleet_nearest_edge: Optional[str] = None,
) -> Dict[str, Any]:
    """Return what a two-sided behavioral-V gate would have said. Applies nothing.

    Args:
        behavioral: the agent's BehavioralEISV (recent V history + maturity).
        fleet_action: the action the live fleet-constant gates actually chose,
            recorded alongside so agreement/divergence is readable directly.
        coherence: the coherence value the fleet gates saw, for context only --
            deliberately NOT the gate statistic (see module docstring).
        fleet_sub_action: qualifier identifying which policy branch chose a
            pause (for example ``coherence_pause`` or ``risk_pause``).
        fleet_nearest_edge: causal edge for multiplexed branches such as CIRS.

    Returns a dict that is always shaped the same, so an ineligible agent is
    still an explicit observation rather than an absence.
    """
    baseline_ready = bool(getattr(behavioral, "is_baselined", False))
    z_candidate, scale = _recent_v_score(behavioral)
    eligible = baseline_ready and z_candidate is not None
    z = z_candidate if eligible else None
    magnitude = abs(z) if z is not None else None

    would = None
    if magnitude is not None:
        if magnitude >= K_FLOOR:
            would = "hard_block_floor"
        elif magnitude >= K_BLOCK:
            would = "hard_block"
        elif magnitude >= K_PAUSE:
            would = "coherence_pause"
        else:
            would = "proceed"

    fleet_coherence_gated, fleet_gate_family = _fleet_coherence_gate_attribution(
        fleet_action,
        fleet_sub_action,
        fleet_nearest_edge,
    )
    prop_paused = would in {"coherence_pause", "hard_block", "hard_block_floor"}

    return {
        "eligible": eligible,
        "eligibility_reason": (
            None if eligible
            else "behavioral_baseline_immature" if not baseline_ready
            else "insufficient_recent_history"
        ),
        "statistic_version": STATISTIC_VERSION,
        "comparison_version": COMPARISON_VERSION,
        "tail": "two_sided",
        # Canonical name: the denominator may be the calibrated floor. Keep
        # v_zscore as a compatibility alias for already-written analytics.
        "v_standardized_residual": round(z, 4) if z is not None else None,
        "v_zscore": round(z, 4) if z is not None else None,
        "v_deviation_magnitude": round(magnitude, 4) if magnitude is not None else None,
        "deviation_direction": (
            "higher_v" if z is not None and z > 0
            else "lower_v" if z is not None and z < 0
            else "at_recent_mean" if z is not None
            else None
        ),
        "would_action": would,
        "fleet_action": fleet_action,
        "fleet_sub_action": fleet_sub_action,
        "fleet_nearest_edge": fleet_nearest_edge,
        "fleet_coherence_gated": fleet_coherence_gated,
        "fleet_gate_family": fleet_gate_family,
        "coherence_seen": round(float(coherence), 6) if coherence is not None else None,
        # The comparison is against the value consumed by today's fleet gates,
        # not whichever producer may later win at the response surface.
        "coherence_seen_source": LEGACY_COHERENCE_SOURCE,
        "coherence_seen_role": ODE_CONTROL_FEEDBACK_ROLE,
        # Only meaningful when BOTH the candidate statistic and the fleet
        # gate's cause are attributable. None keeps immature/ambiguous rows out
        # of agreement rates rather than silently guessing.
        "agrees": (
            fleet_coherence_gated == prop_paused
            if eligible and fleet_coherence_gated is not None
            else None
        ),
        "k": {"pause": K_PAUSE, "block": K_BLOCK, "floor": K_FLOOR},
        **{
            key: round(value, 6) if isinstance(value, float) else value
            for key, value in scale.items()
        },
    }


def record(audit_logger: Any, agent_id: str, payload: Dict[str, Any]) -> None:
    """Emit the comparison. Never raises into the check-in path.

    Fail-open by design: this is optional measurement sitting on the mandatory
    check-in path, so a broken audit sink must not cost anyone a check-in.
    """
    try:
        audit_logger.log_coherence_gate_shadow(
            agent_id=agent_id or "unknown", **payload
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"coherence_gate_shadow record failed: {exc}")
