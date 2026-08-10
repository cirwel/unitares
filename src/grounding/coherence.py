"""Coherence — spec §3.1 Coherence (INTENDED runtime form; NOT canonical yet).

Corrected 2026-08-10. This header used to call itself "CANONICAL runtime
form" and claim it has been populated into `metrics["coherence"]` since
PR #26. **Neither is true as deployed.** `run_grounding_stage` only keeps
this value when `UNITARES_GROUNDING_APPLY` is set; that flag is off by
default and off in production, so the stage reverts to the legacy value and
drops `coherence_legacy`/`coherence_source`. What ships in MCP responses and
lands in `core.agent_state.coherence` is the legacy thermodynamic
`C(V, Θ) = 0.5 · (1 + tanh(Θ.C₁ · V))` from `governance_core/coherence.py`.

This form does run on every check-in under `UNITARES_GROUNDING_SHADOW` (on
in production), which is how the two are compared: the `grounding_shadow`
audit event carries {grounded, ungrounded, delta} per dimension.

⚠️ Enabling APPLY is a coherence-only change in practice but NOT a safe
flip. Measured 2026-08-10 over 7d of shadow events (n=5330): E, I and S move
in 0 of 5330 rows (no caller supplies logprobs, so those stay heuristic),
while coherence moves in 5330 of 5330 — from sd 0.0077 to sd 0.285. 18.09%
of the grounded values fall below `AdaptiveGovernor`'s `tau_floor` of 0.25,
which the legacy form has never once reached. Derive the threshold against
this distribution before enabling APPLY, not after.

Two grounded forms:
  - manifold:  C = 1 - ||Δ||_2 / ||Δ||_max, Δ = (E,I,S) - (E,I,S)_healthy
  - kl:        C = exp(-D_KL(q_now || q_ref)), requires reference distribution

Manifold form ships as primary (needs only existing EISV). KL stubbed for Phase 2.

Physical interpretation: "how close is this agent's (E, I, S) state to its
class's healthy operating point?" V is NOT in this formula — for V-driven
thermodynamic coherence read `coherence_legacy`. See paper v6.8.1 §6.7
translation table for the paper ↔ runtime ↔ audit vocabulary mapping.
"""
import math
from typing import Any, Dict

from src.grounding.types import GroundedValue


def compute_coherence(ctx: Any, metrics: Dict[str, Any]) -> GroundedValue:
    if "q_now" in metrics and "q_ref" in metrics:
        try:
            return _compute_kl(metrics["q_now"], metrics["q_ref"])
        except NotImplementedError:
            pass

    agent_class = getattr(ctx, "agent_class", None) or "default"

    try:
        return _compute_manifold(
            E=float(metrics["E"]),
            I=float(metrics["I"]),
            S=float(metrics["S"]),
            agent_class=agent_class,
        )
    except (KeyError, TypeError, ValueError):
        pass

    return _compute_heuristic(metrics)


def _compute_kl(q_now: list, q_ref: list) -> GroundedValue:
    raise NotImplementedError(
        "tier-1 KL coherence requires a calibrated reference distribution q_ref; "
        "Phase 2 scope"
    )


def _compute_manifold(E: float, I: float, S: float, agent_class: str = "default") -> GroundedValue:
    """Manifold distance from class-conditional healthy operating point.

    Uses both class-conditional ||Δ||_max and class-conditional healthy
    operating point if the class has measured values; otherwise falls back
    to fleet-wide defaults.
    """
    from config.governance_config import get_delta_norm_max, get_healthy_operating_point

    healthy_E, healthy_I, healthy_S = get_healthy_operating_point(agent_class)

    dx = E - healthy_E
    dy = I - healthy_I
    dz = S - healthy_S
    norm = math.sqrt(dx * dx + dy * dy + dz * dz)
    ratio = norm / get_delta_norm_max(agent_class).value
    val = 1.0 - max(0.0, min(1.0, ratio))
    return GroundedValue(value=val, source="manifold")


def _compute_heuristic(metrics: Dict[str, Any]) -> GroundedValue:
    raw = metrics.get("coherence", 0.5)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        val = 0.5
    val = max(0.0, min(1.0, val))
    return GroundedValue(value=val, source="heuristic")
