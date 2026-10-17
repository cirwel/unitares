"""The S setpoint σ moves ODE S and nothing else (contract row 50).

Every reader of ODE S that treated S = 0 as healthy reads S - σ: Φ, the basin,
the E and I equations and the regime fallback, and a fresh monitor's S seed sits
σ above the σ = 0 seed. So with no sensor spring, a monitor run with the
setpoint on and the same monitor run with it off agree on everything but S,
which differs by σ, check-in for check-in.

The claim holds while S - σ stays clear of S's floor; the last two tests pin
where it ends. Left out on purpose: the sensor scenarios, because the spring pulls ODE S toward
a measured S that the setpoint does not shift (the gap Stage A closes); and the
derived-confidence formula, which still reads raw S and is not exercised here
because every sensor-free scenario reports its confidence (contract row 50 says
why it is left on raw S).
"""
import os

import pytest

import tests.eisv_preservation_driver as driver
from config.governance_config import get_s_setpoint

SENSOR_FREE = (
    "warmup_to_self_relative",
    "high_risk_first_checkins",
    "saturating_gap",
    "behavioral_state_roundtrip",
)
SAME = ("E", "I", "V", "phi", "risk_score", "coherence", "verdict", "sub_action",
        "action", "regime", "status")


def _run(monkeypatch, flag):
    for key in list(os.environ):
        if key.startswith(driver.CONFIG_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    if flag is not None:
        monkeypatch.setenv("UNITARES_S_SETPOINT", flag)
    return driver.run_all()["trajectories"]


@pytest.fixture(scope="module")
def runs():
    mp = pytest.MonkeyPatch()
    try:
        on = _run(mp, None)
        off = _run(mp, "0")
    finally:
        mp.undo()
    return on, off


@pytest.mark.parametrize("scenario", SENSOR_FREE)
def test_setpoint_moves_S_alone(runs, scenario):
    """The recorded E/I/S/V are the ODE's until the behavioral estimate becomes
    primary and the behavioral estimate's after, so the σ offset in S shows only
    on the ODE-primary check-ins. Φ and coherence are read off the ODE on every
    check-in, so their equality covers the ODE throughout."""
    on, off = runs
    sigma = get_s_setpoint("default")
    assert sigma > 0
    assert len(on[scenario]) == len(off[scenario])
    ode_primary = 0
    for i, (a, b) in enumerate(zip(on[scenario], off[scenario])):
        for field in SAME:
            if isinstance(a.get(field), float):
                assert a[field] == pytest.approx(b[field], abs=1e-9), (scenario, i, field)
            else:
                assert a.get(field) == b.get(field), (scenario, i, field)
        offset = a["S"] - b["S"]
        if offset == pytest.approx(sigma, abs=1e-6):
            ode_primary += 1
        else:
            assert offset == pytest.approx(0.0, abs=1e-9), (scenario, i, offset)
    assert ode_primary >= 1, scenario


def test_regime_fallback_detrends_level_but_not_the_S_delta():
    """The ODE fallback compares S - σ with its absolute thresholds, and its dS
    is a difference of two detrended values, so σ cancels from it."""
    from types import SimpleNamespace

    from src.monitor_regime import detect_regime

    sigma = 0.145

    def state(S_now, S_prev, V, offset):
        return SimpleNamespace(
            I=0.80, S=S_now + offset, V=V, locked_persistence_count=0,
            S_history=[S_prev + offset, S_now + offset], I_history=[0.80, 0.80])

    seen = set()
    for V in (0.01, 0.10):
        for S_now, S_prev in ((0.20, 0.20), (0.20, 0.30), (0.30, 0.20), (0.05, 0.05)):
            off = detect_regime(state(S_now, S_prev, V, 0.0))
            on = detect_regime(state(S_now, S_prev, V, sigma), s_setpoint=sigma)
            assert on == off, (S_now, S_prev, V, on, off)
            seen.add(off)
    assert {"DIVERGENCE", "CONVERGENCE"} <= seen


def _noise_free_rest(complexity, sigma, steps=5000, dt=0.05):
    from governance_core.dynamics import DEFAULT_STATE, State, step_state
    from governance_core.parameters import DEFAULT_THETA, get_active_params

    params = get_active_params()
    s = State(E=DEFAULT_STATE.E, I=DEFAULT_STATE.I, S=DEFAULT_STATE.S + sigma, V=DEFAULT_STATE.V)
    for _ in range(steps):
        s = step_state(state=s, theta=DEFAULT_THETA, delta_eta=[], dt=dt, noise_S=0.0,
                       params=params, complexity=complexity, sensor_eisv=None,
                       s_setpoint=sigma)
    return s, params


@pytest.mark.parametrize("complexity", [0.4, 0.5, 0.7])
def test_invariance_holds_while_S_stays_clear_of_its_floor(complexity):
    sigma = get_s_setpoint("default")
    off, params = _noise_free_rest(complexity, 0.0)
    on, _ = _noise_free_rest(complexity, sigma)
    assert off.S >= params.S_min + params.barrier_margin
    for field in ("E", "I", "V"):
        assert getattr(on, field) == pytest.approx(getattr(off, field), abs=1e-6), field
    assert on.S - off.S == pytest.approx(sigma, abs=1e-6)


def test_invariance_ends_at_the_S_floor():
    """The bound of the claim, pinned. S's floor (S_min, its soft barrier and the
    complexity floor) is entropy's own and stays on raw S. Where the setpoint-off
    S rest falls inside the barrier margin (noise-free, low complexity), the off
    run is held up by the floor while the shifted run's S - σ may go below it, so
    the shifted run rests with higher E and I. Recentring the floor on σ would
    forbid ODE S under σ and fight the sensor spring toward a measured S that is
    calmer than healthy; Φ's detrend is unclamped below σ for the same reason
    (Stage A.2). Live exposure is recorded in contract row 50."""
    sigma = get_s_setpoint("default")
    off, params = _noise_free_rest(0.15, 0.0)
    on, _ = _noise_free_rest(0.15, sigma)
    assert off.S < params.S_min + params.barrier_margin
    assert on.S - sigma < off.S
    assert on.I > off.I and on.E > off.E
