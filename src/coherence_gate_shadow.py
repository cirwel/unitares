"""Shadow-measure a proprioceptive coherence gate against the live fleet-constant one.

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

The gate statistic is the agent's own V z-score, NOT a separate coherence
baseline. Since C(V) = Cmax*0.5*(1+tanh(C1*V)) is strictly monotone in V,
"coherence below this agent's own normal" and "V below this agent's own normal"
are the same event, so a coherence baseline would be a second copy of state that
can only drift out of sync with the first. Gating on V also gates on the
measurement rather than on a squashed view of it. The tanh is not affine, so a
fixed z on V is not exactly a fixed z on C -- that is acceptable precisely
because k is a stated tolerance rather than a derived constant, and it is
recorded here so nobody later mistakes it for an identity.

Reused rather than reinvented, both from BehavioralEISV:
  * ``is_baselined`` (30 updates, baseline_confidence >= 0.8) as the maturity
    gate. An immature baseline reports eligible=False and fires nothing --
    the proposal's requirement that a per-agent threshold never apply before
    its dispersion can be estimated.
  * the ``min_std`` floor inside ``deviation()``, which is an empirical constant
    calibrated against the 2026-06-13 Sentinel false-pause trace. That is the
    robust-dispersion guard the proposal asks for; a fresh estimator here would
    discard a calibration already paid for in production.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)

# k per severity tier, preserving the ordering of the gates they shadow.
# STATED TOLERANCES, not derived constants -- see the proposal section 4. The
# measured firing rate at 3 sigma is 0.13-0.25% for high-n agents but 1.4-3.2%
# at n=55-79, so normality does not hold well enough to quote a nominal rate.
# These exist to be revised against the shadow data this module produces.
K_PAUSE = 3.0    # shadows COHERENCE_CRITICAL_THRESHOLD (0.40) -> coherence_pause
K_BLOCK = 4.0    # shadows CIRS tau_low (0.30) -> hard block
K_FLOOR = 5.0    # shadows AdaptiveGovernor tau_floor (0.25) -> hard block


def coherence_gate_shadow_enabled() -> bool:
    """Whether to record the proprioceptive-vs-fleet gate comparison.

    UNITARES_COHERENCE_GATE_SHADOW, default off. Measurement only; there is no
    corresponding APPLY flag, because applying a per-agent gate requires the
    signal repair to land in the same change (proposal section 6) and is not
    something a flag should be able to do on its own.
    """
    return os.getenv("UNITARES_COHERENCE_GATE_SHADOW", "").strip().lower() in {
        "1", "true", "on", "yes",
    }


def evaluate(
    behavioral: Any,
    fleet_action: Optional[str],
    coherence: Optional[float] = None,
) -> Dict[str, Any]:
    """Return what a per-agent coherence gate would have said. Applies nothing.

    Args:
        behavioral: the agent's BehavioralEISV (for ``deviation`` + maturity).
        fleet_action: the action the live fleet-constant gates actually chose,
            recorded alongside so agreement/divergence is readable directly.
        coherence: the coherence value the fleet gates saw, for context only --
            deliberately NOT the gate statistic (see module docstring).

    Returns a dict that is always shaped the same, so an ineligible agent is
    still an explicit observation rather than an absence.
    """
    eligible = bool(getattr(behavioral, "is_baselined", False))

    # deviation() already returns 0.0 when the baseline is immature, but that is
    # indistinguishable from a genuine "exactly at baseline". Keep the maturity
    # verdict separate from the statistic so the two never get conflated.
    z = float(behavioral.deviation("V")) if eligible else None

    would = None
    if z is not None:
        if z <= -K_FLOOR:
            would = "hard_block_floor"
        elif z <= -K_BLOCK:
            would = "hard_block"
        elif z <= -K_PAUSE:
            would = "coherence_pause"
        else:
            would = "proceed"

    fleet_paused = fleet_action in {"pause", "coherence_pause", "cirs_block", "reject"}
    prop_paused = would in {"coherence_pause", "hard_block", "hard_block_floor"}

    return {
        "eligible": eligible,
        "v_zscore": round(z, 4) if z is not None else None,
        "would_action": would,
        "fleet_action": fleet_action,
        "coherence_seen": round(float(coherence), 6) if coherence is not None else None,
        # Only meaningful when eligible; None keeps ineligible rows out of any
        # later agreement rate rather than silently counting them as agreement.
        "agrees": (fleet_paused == prop_paused) if eligible else None,
        "k": {"pause": K_PAUSE, "block": K_BLOCK, "floor": K_FLOOR},
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
