"""Stage A — Φ/setpoint coupling (src/monitor_setpoint.py).

The per-class S setpoint (test_eisv_s_setpoint.py) moves the ODE rest-S up to
measured-healthy (~0.2). Φ is read off that same ODE state and penalizes S
against zero, so without compensation the attractor move pushes healthy at-rest
agents safe→caution (and engaged_ephemeral→high-risk) — see
scripts/analysis/eisv_stage_a_redteam.py.

These guard the coupling that prevents that regression: Φ detrends S by the SAME
σ the dynamics rest at, sharing the UNITARES_S_SETPOINT flag so the two can never
be enabled independently.
"""
import pytest

from governance_core.dynamics import State
from governance_core.scoring import phi_objective, verdict_from_phi
from governance_core.parameters import DEFAULT_WEIGHTS
from config.governance_config import (
    get_s_setpoint, get_healthy_operating_point, S_SETPOINT_DRIVER_OFFSET,
)
from src.monitor_setpoint import phi_eval_state, setpoint_for_monitor


class _FakeMonitor:
    """Minimal stand-in exposing what monitor_setpoint reads."""
    def __init__(self, agent_class, state):
        self._resolved_agent_class = agent_class

        class _S:
            pass
        self.state = _S()
        self.state.unitaires_state = state


# Measured-healthy ODE rest state per class once the setpoint is ON: E/I/V track
# the historical attractor, S lands on measured healthy (the dynamics result).
def _healthy_rest_state(agent_class):
    s_healthy = get_healthy_operating_point(agent_class)[2]
    return State(E=0.805, I=0.822, S=s_healthy, V=-0.013)


def test_phi_eval_state_noop_when_flag_off(monkeypatch):
    """Flag off → phi_eval_state returns the state unchanged (byte-identical Φ).

    S_SETPOINT now defaults ON, so force it off to exercise the no-op path.
    """
    monkeypatch.setenv("UNITARES_S_SETPOINT", "0")
    st = _healthy_rest_state("resident_persistent")
    mon = _FakeMonitor("resident_persistent", st)
    out = phi_eval_state(mon, st)
    assert (out.E, out.I, out.S, out.V) == (st.E, st.I, st.S, st.V)
    assert setpoint_for_monitor(mon) == 0.0


def test_phi_eval_state_detrends_by_sigma_when_on(monkeypatch):
    monkeypatch.setenv("UNITARES_S_SETPOINT", "1")
    st = _healthy_rest_state("resident_persistent")
    mon = _FakeMonitor("resident_persistent", st)
    sigma = get_s_setpoint("resident_persistent")
    assert sigma == pytest.approx(
        get_healthy_operating_point("resident_persistent")[2] - S_SETPOINT_DRIVER_OFFSET, abs=1e-9)
    out = phi_eval_state(mon, st)
    assert out.S == pytest.approx(st.S - sigma, abs=1e-12)
    assert (out.E, out.I, out.V) == (st.E, st.I, st.V)


@pytest.mark.parametrize("agent_class", ["default", "embodied", "resident_persistent", "engaged_ephemeral"])
def test_coupling_keeps_healthy_verdict_safe(monkeypatch, agent_class):
    """At the measured-healthy ODE rest, the coupled Φ keeps verdict 'safe';
    the uncoupled Φ (raw state) would degrade — this is the regression guard."""
    monkeypatch.setenv("UNITARES_S_SETPOINT", "1")
    st = _healthy_rest_state(agent_class)
    mon = _FakeMonitor(agent_class, st)

    raw_phi = phi_objective(st, delta_eta=[], weights=DEFAULT_WEIGHTS)
    coupled_phi = phi_objective(phi_eval_state(mon, st), delta_eta=[], weights=DEFAULT_WEIGHTS)

    # Coupling lifts Φ back to the historical safe band.
    assert verdict_from_phi(coupled_phi) == "safe", (
        f"{agent_class}: coupled Φ={coupled_phi:.3f} not safe")
    # And it is strictly higher than the uncompensated Φ (which the red-team
    # shows crosses into caution/high-risk for most classes).
    assert coupled_phi > raw_phi


def test_coupled_phi_matches_historical_rest(monkeypatch):
    """Φ at the new (correct) attractor with coupling ≈ Φ at the old S≈0.091
    attractor without it: verdict/risk are invariant under the attractor move."""
    monkeypatch.setenv("UNITARES_S_SETPOINT", "1")
    for agent_class in ("default", "embodied", "resident_persistent", "engaged_ephemeral"):
        st_new = _healthy_rest_state(agent_class)
        mon = _FakeMonitor(agent_class, st_new)
        coupled_phi = phi_objective(
            phi_eval_state(mon, st_new), delta_eta=[], weights=DEFAULT_WEIGHTS)
        # Historical attractor: same E/I/V, S at the driver offset.
        st_old = State(E=0.805, I=0.822, S=S_SETPOINT_DRIVER_OFFSET, V=-0.013)
        hist_phi = phi_objective(st_old, delta_eta=[], weights=DEFAULT_WEIGHTS)
        assert coupled_phi == pytest.approx(hist_phi, abs=1e-9), (
            f"{agent_class}: coupled Φ {coupled_phi:.4f} != historical {hist_phi:.4f}")
