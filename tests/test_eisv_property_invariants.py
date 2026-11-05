"""Property-based invariants for the EISV decision path and behavioral state.

These are consistency checks: each asserts that the code agrees with a
property it already documents, over randomly generated inputs instead of a
handful of named examples. They carry no outcome labels and fit no threshold,
so a pass is not evidence that EISV measures or predicts anything (see the
tested-claims ledger in docs/ontology/eisv-proprioception-contract.md).

Nothing here changes the live update path, so no instrument era is crossed.

Covered:

* ``make_decision`` severity is monotone: worsening any single input never
  yields a milder action (generalises the two fixed sweeps in
  ``test_eisv_policy_stress.py``).
* Setpoint invariance (contract row 50): shifting S and the setpoint by the
  same amount leaves the decision unchanged.
* Phi isolation (contract row 52): with ``latest_risk_fast_trip=False`` the
  Phi-derived ``risk_history`` cannot move the action; with it on, the fast
  trip only ever escalates approve to guide.
* ``BehavioralEISV.update`` is a convex EMA step: each smoothed value lands
  between its previous value and the clamped reading.
* Restoring a persisted ``BehavioralEISV`` and replaying the same readings
  reproduces the original trajectory to persistence precision.

Two strict xfails record defects found while writing these (both latent or
unmeasured in production; fixing either changes the live behavioral update,
so the fix is a separate, era-recorded change):

* a NaN reading is clamped to 1.0, i.e. treated as maximal;
* the bootstrap alpha boost can push an EMA rate above 1 and overshoot.
"""
from __future__ import annotations

import math

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, assume, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from src import monitor_decision as MD  # noqa: E402
from src import runtime_config  # noqa: E402
from src.behavioral_state import (  # noqa: E402
    BOOTSTRAP_UPDATES,
    MAX_HISTORY,
    BehavioralEISV,
)


@pytest.fixture(autouse=True)
def _class_default_thresholds():
    """Run against the class-default thresholds, whatever an earlier test left."""
    saved = dict(runtime_config._runtime_overrides)
    runtime_config.clear_overrides()
    yield
    runtime_config._runtime_overrides.clear()
    runtime_config._runtime_overrides.update(saved)

SETTINGS = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

unit = st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False)
signed_unit = st.floats(-1.0, 1.0, allow_nan=False, allow_infinity=False)

VERDICTS = ["safe", "caution", "high-risk"]
TIERS = ["proceed", "soft_dampen", "hard_block"]


# ---------------------------------------------------------------------------
# make_decision helpers
# ---------------------------------------------------------------------------

class _State:
    """The attributes ``make_decision`` reads from a GovernanceState."""

    agent_class = None
    void_threshold_effective = 0.15
    coherence_role = None
    coherence_history_role = None
    lambda1 = 0.1
    update_count = 20

    def __init__(self, *, E, I, S, V, coherence, void_active, latest_risk):
        self.E, self.I, self.S, self.V = E, I, S, V
        self.coherence = coherence
        self.void_active = void_active
        self.V_history = [V] * 20
        self.coherence_history = [coherence] * 6
        self.risk_history = [latest_risk]


def _decide(p, *, s_setpoint=0.0, fast_trip=True):
    state = _State(
        E=p["E"], I=p["I"], S=p["S"], V=p["V"],
        coherence=p["coherence"], void_active=p["void_active"],
        latest_risk=p["latest_risk"],
    )
    return MD.make_decision(
        state,
        risk_score=p["risk"],
        unitares_verdict=p["verdict"],
        response_tier=p["tier"],
        s_setpoint=s_setpoint,
        latest_risk_fast_trip=fast_trip,
    )


def _severity(decision) -> int:
    """approve < guide < pause. Anything else is a new action this test must learn."""
    action, sub = decision["action"], decision.get("sub_action")
    if action == "pause":
        return 2
    if action == "proceed" and sub == "guide":
        return 1
    if action == "proceed" and sub == "approve":
        return 0
    raise AssertionError(f"unrecognised decision shape: {action!r}/{sub!r}")


def _span(lo, hi):
    return st.floats(lo, hi, allow_nan=False, allow_infinity=False)


_anywhere = st.fixed_dictionaries({
    "E": unit, "I": unit, "S": unit, "V": signed_unit,
    "coherence": unit, "risk": unit, "latest_risk": unit,
    "void_active": st.booleans(),
    "verdict": st.sampled_from(VERDICTS),
    "tier": st.sampled_from(TIERS),
})

# Uniform draws land in the HIGH basin with a safe verdict (the only place an
# unqualified approve, and so the fast trip, can occur) well under 1% of the
# time. Draw half the examples from around that region so the approve/guide
# edges are exercised. The bounds bracket BASIN_HIGH in config.governance_config.
_near_healthy = st.fixed_dictionaries({
    "E": _span(0.55, 1.0), "I": _span(0.65, 1.0), "S": _span(0.0, 0.3),
    "V": _span(-0.2, 0.2), "coherence": _span(0.4, 1.0),
    "risk": _span(0.0, 0.5), "latest_risk": unit,
    "void_active": st.just(False),
    "verdict": st.sampled_from(["safe", "safe", "caution"]),
    "tier": st.sampled_from(["proceed", "proceed", "soft_dampen"]),
})

decision_inputs = st.one_of(_anywhere, _near_healthy)
# The Phi-isolation and fast-trip properties can only differ on an approve, so
# they draw mostly from around the healthy region.
approve_heavy_inputs = st.one_of(_anywhere, _near_healthy, _near_healthy, _near_healthy)

_HEALTHY_CENTRE = {
    "E": 0.8, "I": 0.85, "S": 0.1, "V": 0.0, "coherence": 0.7,
    "risk": 0.2, "latest_risk": 0.1, "void_active": False,
    "verdict": "safe", "tier": "proceed",
}


def test_near_healthy_strategy_reaches_approve_and_the_fast_trip():
    """Guards the generators: if BASIN_HIGH or the approve threshold moves so
    that the near-healthy region no longer brackets it, the approve-dependent
    properties below would pass vacuously. This fails first instead."""
    d = _decide(_HEALTHY_CENTRE)
    assert (d["basin"], d["action"], d["sub_action"]) == ("high", "proceed", "approve")
    tripped = _decide({**_HEALTHY_CENTRE, "latest_risk": 0.95})
    assert (tripped["action"], tripped["sub_action"]) == ("proceed", "guide")
    assert "latest_risk_fast_trip" in tripped


def _worsen(p, dim, amount):
    """Return a copy of ``p`` moved ``amount`` toward 'worse' on one input."""
    q = dict(p)
    if dim == "risk":
        q["risk"] = min(1.0, p["risk"] + amount)
    elif dim == "coherence":
        q["coherence"] = max(0.0, p["coherence"] - amount)
    elif dim == "I":
        q["I"] = max(0.0, p["I"] - amount)
    elif dim == "E":
        q["E"] = max(0.0, p["E"] - amount)
    elif dim == "S":
        q["S"] = min(1.0, p["S"] + amount)
    elif dim == "abs_V":
        q["V"] = math.copysign(min(1.0, abs(p["V"]) + amount), p["V"] or 1.0)
    elif dim == "latest_risk":
        q["latest_risk"] = min(1.0, p["latest_risk"] + amount)
    elif dim == "void_active":
        q["void_active"] = True
    elif dim == "verdict":
        q["verdict"] = VERDICTS[min(len(VERDICTS) - 1, VERDICTS.index(p["verdict"]) + 1)]
    elif dim == "tier":
        q["tier"] = TIERS[min(len(TIERS) - 1, TIERS.index(p["tier"]) + 1)]
    else:  # pragma: no cover - guards the sampled_from list below
        raise AssertionError(dim)
    return q


WORSEN_DIMS = ["risk", "latest_risk", "coherence", "I", "E", "S", "abs_V",
               "void_active", "verdict", "tier"]


# ---------------------------------------------------------------------------
# make_decision properties
# ---------------------------------------------------------------------------

@SETTINGS
@given(p=decision_inputs, dim=st.sampled_from(WORSEN_DIMS), amount=unit)
def test_worsening_one_input_never_yields_a_milder_decision(p, dim, amount):
    before = _severity(_decide(p))
    after = _severity(_decide(_worsen(p, dim, amount)))
    assert after >= before, (dim, amount, p)


@SETTINGS
@given(p=decision_inputs)
def test_hard_block_and_void_always_pause(p):
    assert _decide({**p, "tier": "hard_block"})["action"] == "pause"
    assert _decide({**p, "void_active": True})["action"] == "pause"


@SETTINGS
@given(
    p=decision_inputs,
    sigma=st.floats(0.0, 0.3, allow_nan=False),
    shift=st.floats(-0.2, 0.2, allow_nan=False),
)
def test_shifting_S_and_setpoint_together_leaves_the_decision_unchanged(p, sigma, shift):
    shifted = {**p, "S": p["S"] + shift}
    # Float subtraction is not exactly shift-invariant; stay clear of the one
    # S-dependent edge (BASIN_HIGH.S_max) so rounding cannot decide the case.
    assume(abs((p["S"] - sigma) - 0.25) > 1e-9)
    assume(abs((shifted["S"] - (sigma + shift)) - 0.25) > 1e-9)
    base = _decide(p, s_setpoint=sigma)
    moved = _decide(shifted, s_setpoint=sigma + shift)
    for key in ("action", "sub_action", "basin"):
        assert base[key] == moved[key], (key, p, sigma, shift)


@SETTINGS
@given(p=approve_heavy_inputs, other_latest=unit)
def test_phi_risk_history_cannot_move_the_action_when_fast_trip_is_off(p, other_latest):
    a = _decide(p, fast_trip=False)
    b = _decide({**p, "latest_risk": other_latest}, fast_trip=False)
    assert (a["action"], a["sub_action"]) == (b["action"], b["sub_action"])


@SETTINGS
@given(p=approve_heavy_inputs)
def test_fast_trip_only_escalates_approve_to_guide(p):
    off = _decide(p, fast_trip=False)
    on = _decide(p, fast_trip=True)
    if (off["action"], off["sub_action"]) != (on["action"], on["sub_action"]):
        assert (off["action"], off["sub_action"]) == ("proceed", "approve")
        assert (on["action"], on["sub_action"]) == ("proceed", "guide")


# ---------------------------------------------------------------------------
# BehavioralEISV properties
# ---------------------------------------------------------------------------

reading = st.tuples(unit, unit, unit)


def _between(x, a, b, tol=1e-12):
    lo, hi = min(a, b), max(a, b)
    return lo - tol <= x <= hi + tol


def _assert_convex_step(prev, new, obs):
    e_obs, i_obs, s_obs = obs
    assert _between(new.E, prev["E"], e_obs), ("E", prev, obs, new.E)
    assert _between(new.I, prev["I"], i_obs), ("I", prev, obs, new.I)
    assert _between(new.S, prev["S"], s_obs), ("S", prev, obs, new.S)
    assert _between(new.V, prev["V"], e_obs - i_obs), ("V", prev, obs, new.V)


@SETTINGS
@given(
    start=st.tuples(unit, unit, unit, signed_unit),
    warmup=st.integers(0, 40),
    readings=st.lists(reading, min_size=1, max_size=40),
)
def test_behavioral_update_is_a_convex_ema_step_with_default_rates(start, warmup, readings):
    b = BehavioralEISV(E=start[0], I=start[1], S=start[2], V=start[3],
                       update_count=warmup)
    for obs in readings:
        prev = {"E": b.E, "I": b.I, "S": b.S, "V": b.V}
        b.update(*obs)
        _assert_convex_step(prev, b, obs)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "Bootstrap adds up to 0.5 to each EMA rate without capping at 1, so a "
        "per-agent rate above 0.5 overshoots the reading during the first "
        f"{BOOTSTRAP_UPDATES} updates. Default rates (<=0.15) are unaffected. "
        "Fixing it changes the live behavioral update (an instrument change)."
    ),
)
@settings(SETTINGS, report_multiple_bugs=False)  # one AssertionError, not a group
@given(
    rate=st.floats(0.01, 1.0, allow_nan=False),
    start=st.tuples(unit, unit, unit, signed_unit),
    readings=st.lists(reading, min_size=1, max_size=12),
)
def test_behavioral_update_stays_convex_for_any_valid_rate(rate, start, readings):
    b = BehavioralEISV(E=start[0], I=start[1], S=start[2], V=start[3])
    b.alphas = {k: rate for k in b.alphas}
    for obs in readings:
        prev = {"E": b.E, "I": b.I, "S": b.S, "V": b.V}
        b.update(*obs)
        _assert_convex_step(prev, b, obs)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "update() clamps with max(0, min(1, x)); min(1.0, nan) is 1.0, so a NaN "
        "reading is recorded as the maximal value. Whether production can feed "
        "NaN here is not established. Fixing it changes the live behavioral "
        "update (an instrument change)."
    ),
)
@pytest.mark.parametrize("slot", [0, 1, 2])
def test_a_nan_reading_is_not_treated_as_maximal(slot):
    b = BehavioralEISV()
    obs = [0.5, 0.5, 0.2]
    obs[slot] = float("nan")
    try:
        b.update(*obs)
    except (ValueError, TypeError):
        return  # rejecting the reading is an acceptable fix
    if b.update_count == 0 and not b.obs_history:
        return  # dropping the reading without raising is also acceptable
    assert b.obs_history[-1][slot] != 1.0


@SETTINGS
@given(readings=st.lists(reading, min_size=0, max_size=3 * MAX_HISTORY))
def test_behavioral_histories_stay_bounded_and_aligned(readings):
    b = BehavioralEISV()
    for obs in readings:
        b.update(*obs)
    lengths = {len(b.E_history), len(b.I_history), len(b.S_history),
               len(b.V_history), len(b.obs_history)}
    assert lengths == {min(len(readings), MAX_HISTORY)}
    assert b.update_count == len(readings)


@SETTINGS
@given(
    history=st.lists(reading, min_size=0, max_size=60),
    future=st.lists(reading, min_size=1, max_size=30),
    with_history=st.booleans(),
)
def test_restored_state_replays_the_same_trajectory(history, future, with_history):
    original = BehavioralEISV()
    for obs in history:
        original.update(*obs)
    snapshot = (original.to_dict_with_history() if with_history
                else original.to_dict_for_persistence())
    restored = BehavioralEISV.from_dict(snapshot)

    assert restored.update_count == original.update_count
    assert restored.alphas == original.alphas
    assert restored.is_baselined == original.is_baselined

    for obs in future:
        original.update(*obs)
        restored.update(*obs)
        # Persistence rounds E/I/S/V to 4 dp; each EMA step only shrinks that
        # initial error, so it never exceeds half a unit in the last place.
        for dim in "EISV":
            assert abs(getattr(original, dim) - getattr(restored, dim)) <= 5e-5 + 1e-12, dim
