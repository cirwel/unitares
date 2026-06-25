"""Stage A — Φ/setpoint coupling for the EISV fixed-point calibration.

When ``UNITARES_S_SETPOINT`` is enabled, ``governance_core.dynamics`` rests the
ODE at the per-class *measured* healthy S (≈0.17-0.31) instead of S*≈0.091
(``config.get_s_setpoint``). That correctly raises the manifold readout (distance
from the healthy operating point shrinks).

But Φ (``governance_core.scoring.phi_objective``) is read off the SAME ODE state
and penalizes entropy linearly against ZERO (``-wS·S``), calibrated to the old
attractor. So moving the rest-S up by σ silently lowers Φ by ``wS·σ`` and pushes
healthy at-rest agents ``safe → caution`` (and ``engaged_ephemeral`` →
``high-risk``). Empirically verified by ``scripts/analysis/eisv_stage_a_redteam.py``
against the live corpus (production Φ rests ≈0.26, i.e. on the S≈0.091 attractor).

Φ and the manifold cannot disagree about where healthy-S lives. This module
recenters Φ's entropy term on the SAME per-class setpoint the dynamics use — Φ
then penalizes entropy ABOVE the healthy rest, not above zero — so verdict/risk
stay invariant at the new (correct) attractor while the manifold gets its range.

The coupling shares the ``UNITARES_S_SETPOINT`` flag with the dynamics setpoint:
the attractor move and the Φ recenter are one atomic change and can never be
enabled independently. Flag OFF → ``phi_eval_state`` returns the ODE state
unchanged (byte-identical historical Φ).
"""
from __future__ import annotations

from typing import Any

from governance_core.dynamics import State


def setpoint_for_monitor(monitor: Any) -> float:
    """Per-class S setpoint σ for this monitor, or 0.0 when the flag is off.

    Mirrors the resolution the dynamics path uses
    (``governance_monitor._resolve_agent_class`` + ``config.get_s_setpoint``) so
    Φ detrends by exactly the σ the ODE rests at.
    """
    from config.governance_config import s_setpoint_enabled, get_s_setpoint

    if not s_setpoint_enabled():
        return 0.0
    agent_class = getattr(monitor, "_resolved_agent_class", None)
    if agent_class is None and hasattr(monitor, "_resolve_agent_class"):
        try:
            agent_class = monitor._resolve_agent_class()
        except Exception:
            agent_class = None
    return get_s_setpoint(agent_class or "default")


def phi_eval_state(monitor: Any, state: State) -> State:
    """Return the State to evaluate Φ on: the ODE state, S-detrended by σ.

    Φ should reward sitting AT the class's healthy operating point, not at the
    unreachable S=0. Detrending S by σ makes ``Φ(S=σ_rest)`` equal the historical
    ``Φ(S=0.091)`` — verdict/risk are invariant under the attractor move.

    Off (σ=0) returns ``state`` unchanged. The shift is unclamped: an agent
    calmer than its healthy baseline (S<σ) earns a small Φ bonus, which
    ``verdict_from_phi`` caps anyway.
    """
    sigma = setpoint_for_monitor(monitor)
    if not sigma:
        return state
    return State(E=state.E, I=state.I, S=state.S - sigma, V=state.V)
