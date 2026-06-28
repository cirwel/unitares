"""Stage A — per-class S setpoint (EISV fixed-point calibration).

Guards two contracts:
  1. OFF by default: s_setpoint=0.0 reproduces the historical `-μS` equilibrium
     (S* ≈ 0.091) exactly — merging the change is a no-op until enabled.
  2. ON: decaying toward σ shifts S* to σ + driver-offset, so a per-class σ =
     healthy_S − offset lands the equilibrium on measured-healthy S and lifts the
     manifold readout above the 0.40 critical threshold.

Runs on governance_core directly — no DB / server needed.
"""
import math

import pytest

from governance_core.dynamics import State, step_state, compute_equilibrium
from governance_core.parameters import get_active_params, DEFAULT_THETA
from config.governance_config import (
    get_s_setpoint, s_setpoint_enabled, get_healthy_operating_point,
    get_delta_norm_max, S_SETPOINT_DRIVER_OFFSET, GovernanceConfig as GC,
)

P, TH = get_active_params(), DEFAULT_THETA


def _rest(s_setpoint, steps=3000, dt=0.05):
    s = compute_equilibrium(P, TH, complexity=0.5)
    for _ in range(steps):
        s = step_state(state=s, theta=TH, delta_eta=[], dt=dt, noise_S=0.0,
                       params=P, complexity=0.5, sensor_eisv=None, s_setpoint=s_setpoint)
    return s


def test_setpoint_zero_reproduces_historical_equilibrium():
    """Default (s_setpoint=0.0) must leave the S equilibrium at ~0.091."""
    assert _rest(0.0).S == pytest.approx(0.091, abs=0.01)


def test_default_call_matches_explicit_zero():
    """Omitting s_setpoint == passing 0.0 (no behavior change on the hot path)."""
    s = compute_equilibrium(P, TH, complexity=0.5)
    a = step_state(state=s, theta=TH, delta_eta=[], dt=0.05, noise_S=0.0,
                   params=P, complexity=0.5, sensor_eisv=None)
    b = step_state(state=s, theta=TH, delta_eta=[], dt=0.05, noise_S=0.0,
                   params=P, complexity=0.5, sensor_eisv=None, s_setpoint=0.0)
    assert (a.E, a.I, a.S, a.V) == (b.E, b.I, b.S, b.V)


def test_setpoint_shifts_equilibrium_by_offset():
    """S* = σ + driver-offset: decaying toward σ raises the rest point linearly."""
    sigma = 0.146  # default healthy_S − offset
    assert _rest(sigma).S == pytest.approx(sigma + 0.091, abs=0.01)


def test_get_s_setpoint_on_by_default(monkeypatch):
    # Default ON (live-proven) when the env is unset.
    monkeypatch.delenv("UNITARES_S_SETPOINT", raising=False)
    assert s_setpoint_enabled()


def test_get_s_setpoint_off_when_disabled(monkeypatch):
    # Explicit off restores the legacy -μS behavior (setpoint 0.0).
    monkeypatch.setenv("UNITARES_S_SETPOINT", "0")
    assert not s_setpoint_enabled()
    assert get_s_setpoint("default") == 0.0
    assert get_s_setpoint("embodied") == 0.0


def test_get_s_setpoint_enabled(monkeypatch):
    monkeypatch.setenv("UNITARES_S_SETPOINT", "1")
    assert s_setpoint_enabled()
    for cls in ("default", "embodied", "resident_persistent"):
        expected = get_healthy_operating_point(cls)[2] - S_SETPOINT_DRIVER_OFFSET
        assert get_s_setpoint(cls) == pytest.approx(expected, abs=1e-9)


def test_setpoint_lands_on_measured_healthy_S():
    """With the per-class setpoint, the ODE S-rest ≈ that class's measured healthy
    S, for every class (S is the only axis the setpoint drives)."""
    for cls in ("default", "embodied", "resident_persistent", "engaged_ephemeral"):
        hp = get_healthy_operating_point(cls)
        sigma = hp[2] - S_SETPOINT_DRIVER_OFFSET
        r = _rest(sigma)
        assert r.S == pytest.approx(hp[2], abs=0.02), f"{cls}: S-rest off target"


def _manifold_at_rest(cls: str) -> float:
    hp = get_healthy_operating_point(cls)
    r = _rest(hp[2] - S_SETPOINT_DRIVER_OFFSET)
    norm = math.sqrt((r.E-hp[0])**2 + (r.I-hp[1])**2 + (r.S-hp[2])**2)
    return 1.0 - max(0.0, min(1.0, norm / get_delta_norm_max(cls).value))


def test_manifold_at_ode_rest_only_clears_critical_for_default():
    """Characterizes a real, currently-open gap (NOT a passing Stage-B enabler).

    The S-setpoint lands S on each class's measured healthy S, but there is no
    E/I setpoint — the ODE rests near a single universal point (E≈0.70, I≈0.73)
    regardless of class. With the generic 2026-06-27 anchors (which reflect where
    classes ACTUALLY operate), only `default` sits close enough to that ODE rest
    for manifold-at-rest to clear the 0.40 critical line; `embodied` (low-E),
    `resident_persistent`, and `engaged_ephemeral` do not. (The old April anchors
    masked this — they were all clustered near the ODE rest.)

    Harmless live: manifold coherence is read on the *behavioral* EISV (which
    matches the anchor) and grounding APPLY is off. But it is the concrete
    precondition for GROUNDING_APPLY: low-/off-rest classes need an E/I setpoint
    or a behavioral-path manifold before the ODE rest reads coherent. When that
    lands, this test should flip — that's the signal to re-evaluate APPLY."""
    assert _manifold_at_rest("default") >= GC.COHERENCE_CRITICAL_THRESHOLD
    for cls in ("embodied", "resident_persistent", "engaged_ephemeral"):
        assert _manifold_at_rest(cls) < GC.COHERENCE_CRITICAL_THRESHOLD, (
            f"{cls}: manifold@rest now clears critical — E/I-setpoint gap may be "
            f"closed; re-evaluate GROUNDING_APPLY and update this characterization"
        )
