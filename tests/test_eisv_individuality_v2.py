"""Model-organism checks for the pre-registered individuality v2 machinery.

No DB. Each organism pins a row of the discrimination table in
docs/proposals/eisv-individuality-v2-preregistration.md — the spec's promise
that the legs separate anchoredness, individuality, drift, and stickiness.
Seeds are fixed; permutation nulls are seeded inside the module.
"""
import math
import random

from scripts.analysis.eisv_individuality_v2 import (
    RAW_DIMS,
    VR_HORIZON,
    evaluate_v2,
    leg_a_agent,
    leg_b,
    leg_c_agent,
    variance_ratio,
    _binom_p_greater_half,
    _moved_count,
    _spearman,
)


# --- organisms --------------------------------------------------------------


def _iid(mean: float, n: int, sigma: float, seed: int) -> list[dict]:
    rng = random.Random(seed)
    return [{d: mean + rng.gauss(0, sigma) for d in RAW_DIMS} for _ in range(n)]


def _walk(start: float, n: int, step: float, seed: int) -> list[dict]:
    rng = random.Random(seed)
    seq, level = [], {d: start for d in RAW_DIMS}
    for _ in range(n):
        level = {d: level[d] + rng.gauss(0, step) for d in RAW_DIMS}
        seq.append(dict(level))
    return seq


def _sticky_anchored(home: float, n: int, seed: int, *,
                     p_jump: float = 0.25, delta: float = 0.1) -> list[dict]:
    """Pins at a home level; occasional excursions that revert next jump.
    The v1 blind spot: 1-step persistence is near-optimal, yet the series is
    strongly anchored."""
    rng = random.Random(seed)
    seq = []
    level = {d: home for d in RAW_DIMS}
    excursion = {d: False for d in RAW_DIMS}
    for _ in range(n):
        for d in RAW_DIMS:
            if rng.random() < p_jump:
                if excursion[d]:
                    level[d] = home          # revert
                    excursion[d] = False
                else:
                    level[d] = home + rng.choice((-delta, delta))
                    excursion[d] = True
        seq.append(dict(level))
    return seq


def _sticky_drift(start: float, n: int, seed: int, *,
                  p_jump: float = 0.25, delta: float = 0.02) -> list[dict]:
    """Pins punctuated by same-sign jumps — a drifting staircase. Anti-
    laundering organism: must NOT pass anchoredness."""
    rng = random.Random(seed)
    seq = []
    level = {d: start for d in RAW_DIMS}
    for _ in range(n):
        for d in RAW_DIMS:
            if rng.random() < p_jump:
                level[d] = level[d] + delta
        seq.append(dict(level))
    return seq


def _by_dim(seq: list[dict]) -> dict:
    return {d: [m[d] for m in seq] for d in RAW_DIMS}


# --- variance ratio basics ---------------------------------------------------


def test_vr_random_walk_near_one():
    s = [m["E"] for m in _walk(0.5, 600, step=0.02, seed=1)]
    vr = variance_ratio(s, 8)
    assert 0.6 < vr < 1.6


def test_vr_iid_well_below_one():
    s = [m["E"] for m in _iid(0.5, 600, sigma=0.05, seed=2)]
    assert variance_ratio(s, 8) < 0.5


def test_vr_undefined_on_constant_or_short():
    assert variance_ratio([0.5] * 50, 8) != variance_ratio([0.5] * 50, 8)  # nan
    assert variance_ratio([0.1, 0.2], 8) != variance_ratio([0.1, 0.2], 8)  # nan


# --- leg A organisms ---------------------------------------------------------


def test_leg_a_random_walk_fails():
    res = leg_a_agent(_by_dim(_walk(0.5, 300, step=0.02, seed=3)), n_perm=300)
    assert res["passes"] is False


def test_leg_a_iid_passes():
    res = leg_a_agent(_by_dim(_iid(0.5, 300, sigma=0.05, seed=4)), n_perm=300)
    assert res["passes"] is True


def test_leg_a_sticky_anchored_passes():
    """The v1 blind spot organism: persistence near-optimal 1-step, yet
    anchored — leg A must see through the stickiness."""
    res = leg_a_agent(_by_dim(_sticky_anchored(0.5, 300, seed=5)), n_perm=300)
    assert res["passes"] is True


def test_leg_a_sticky_drift_fails():
    """Anti-laundering: same-sign staircase drift is permutation-invariant —
    observed VR sits inside the permuted distribution, no rejection."""
    res = leg_a_agent(_by_dim(_sticky_drift(0.3, 300, seed=6)), n_perm=300)
    assert res["passes"] is False


def test_leg_a_constant_series_fails_not_crashes():
    res = leg_a_agent({d: [0.5] * 200 for d in RAW_DIMS}, n_perm=50)
    assert res["passes"] is False


# --- leg B organisms ---------------------------------------------------------


def _fleet(means: list[float], n: int, sigma: float, seed0: int) -> dict:
    return {f"a{i}": _by_dim(_iid(mu, n, sigma=sigma, seed=seed0 + i))
            for i, mu in enumerate(means)}


def test_leg_b_distinct_homes_pass():
    fleet = _fleet([0.2, 0.35, 0.5, 0.65, 0.8], 200, sigma=0.03, seed0=10)
    res = leg_b(fleet)
    assert res["passes"] is True


def test_leg_b_shared_home_fails():
    """Anchored but identical: rank order of split-half means is noise."""
    fleet = _fleet([0.5, 0.5, 0.5, 0.5, 0.5], 200, sigma=0.03, seed0=20)
    res = leg_b(fleet)
    assert res["passes"] is False


# --- leg C ------------------------------------------------------------------


def test_leg_c_iid_reference_beats_persistence():
    """iid noise: the EMA hugs the mean; last-value carries full noise."""
    res = leg_c_agent(_by_dim(_iid(0.5, 400, sigma=0.05, seed=30)))
    assert res["passes"] is True
    assert res["n_moved"] > 300


def test_leg_c_random_walk_persistence_wins():
    res = leg_c_agent(_by_dim(_walk(0.5, 400, step=0.02, seed=31)))
    assert res["passes"] is False


def test_moved_count_ignores_exact_repeats():
    seq = [{d: 0.5 for d in RAW_DIMS}] * 10
    assert _moved_count(seq) == 0
    seq = seq + [{d: 0.6 for d in RAW_DIMS}]
    assert _moved_count(seq) == 1


# --- verdict rule -------------------------------------------------------------


def test_verdict_not_evaluable_below_agent_floor():
    """Floor is 4 eligible agents: 3 must be NOT EVALUABLE, 4 evaluable."""
    means = [0.2, 0.5, 0.8]
    traj = {f"a{i}": _iid(mu, 200, sigma=0.03, seed=40 + i)
            for i, mu in enumerate(means)}
    res = evaluate_v2(traj)
    assert res["n_eligible"] == 3
    assert res["verdict"] == "NOT EVALUABLE"
    traj["a3"] = _iid(0.35, 200, sigma=0.03, seed=44)
    res4 = evaluate_v2(traj)
    assert res4["n_eligible"] == 4
    assert res4["verdict"] != "NOT EVALUABLE"


def test_verdict_earned_on_textbook_fleet():
    means = [0.2, 0.35, 0.5, 0.65, 0.8]
    traj = {f"a{i}": _iid(mu, 150, sigma=0.03, seed=50 + i)
            for i, mu in enumerate(means)}
    res = evaluate_v2(traj)
    assert res["n_eligible"] == 5
    assert res["leg_a_majority"] is True
    assert res["leg_b"]["passes"] is True
    assert res["verdict"] == "AXIOM EARNED"


def test_verdict_fail_on_random_walk_fleet():
    traj = {f"a{i}": _walk(0.5, 150, step=0.02, seed=60 + i)
            for i in range(5)}
    res = evaluate_v2(traj)
    assert res["n_eligible"] == 5
    assert res["verdict"] == "FAIL"


def test_ineligible_agents_excluded_not_scored():
    traj = {"big": _iid(0.5, 150, sigma=0.03, seed=70),
            "small": _iid(0.5, 20, sigma=0.03, seed=71)}
    res = evaluate_v2(traj)
    rows = {r["agent"]: r for r in res["per_agent"]}
    assert rows["small"]["eligible"] is False
    assert rows["small"]["leg_a"] is None


def test_eligibility_floors_are_independent():
    """MIN_STATES and MIN_MOVED must AND: an agent can fail either alone."""
    frozen = [{d: 0.5 for d in RAW_DIMS}] * 150            # 150 states, 0 moved
    frozen[10] = {d: 0.6 for d in RAW_DIMS}                # 2 moved transitions
    short_busy = _iid(0.5, 60, sigma=0.05, seed=72)        # 59 moved, 60 states
    res = evaluate_v2({"frozen": frozen, "short_busy": short_busy})
    rows = {r["agent"]: r for r in res["per_agent"]}
    assert rows["frozen"]["n_states"] >= 100
    assert rows["frozen"]["eligible"] is False             # fails MIN_MOVED
    assert rows["short_busy"]["n_moved"] >= 30
    assert rows["short_busy"]["eligible"] is False         # fails MIN_STATES


def test_agent_majority_is_strict():
    """Exactly half of eligible agents passing leg A is NOT a majority."""
    traj = {
        "i1": _iid(0.2, 150, sigma=0.03, seed=80),
        "i2": _iid(0.8, 150, sigma=0.03, seed=81),
        "w1": _walk(0.5, 150, step=0.02, seed=82),
        "w2": _walk(0.5, 150, step=0.02, seed=83),
    }
    res = evaluate_v2(traj)
    assert res["n_eligible"] == 4
    assert res["leg_a_winners"] == 2
    assert res["leg_a_majority"] is False
    assert res["verdict"] == "FAIL"


def test_dim_majority_two_of_three():
    """2 anchored dims + 1 random-walk dim must still pass leg A (>= 2 of 3)."""
    anchored = _iid(0.5, 250, sigma=0.05, seed=90)
    walk = _walk(0.5, 250, step=0.02, seed=91)
    seq_by_dim = {
        "E": [m["E"] for m in anchored],
        "I": [m["I"] for m in anchored],
        "S": [m["S"] for m in walk],
    }
    res = leg_a_agent(seq_by_dim, n_perm=300)
    assert res["dims"]["E"]["passes"] and res["dims"]["I"]["passes"]
    assert not res["dims"]["S"]["passes"]
    assert res["n_dims_pass"] == 2
    assert res["passes"] is True


# --- windowed-feature organisms (adversarial review F1) ---------------------


def _windowed_feature(events: list[float], w: int = 10) -> list[float]:
    """Rolling-window mean — the shape of the live raw_obs features."""
    out = []
    for i in range(len(events)):
        win = events[max(0, i - w + 1):i + 1]
        out.append(sum(win) / len(win))
    return out


def _windowed_stable_rate(p: float, n: int, seed: int) -> dict:
    """Constant underlying behavior rate seen through the 10-event window.
    Under the reframed leg A this HAS a home (the rate itself) and passes —
    disclosed as correct-by-design in the spec, not laundering."""
    rng = random.Random(seed)
    series = {}
    for d in RAW_DIMS:
        events = [1.0 if rng.random() < p else 0.0 for _ in range(n)]
        series[d] = _windowed_feature(events)
    return series


def _windowed_drifting_rate(n: int, seed: int, *, step: float = 0.03) -> dict:
    """Random-walking underlying rate through the same window — no stable
    home at the behavior level. MUST fail leg A (the anti-laundering pin
    demanded by the adversarial review). One shared walk drives all dims,
    as a real behavior change would: the drift is agent-level, so the veto
    must fire on the dim majority, not on one lucky realization."""
    rng = random.Random(seed)
    p = 0.5
    rates = []
    for _ in range(n):
        p = min(0.95, max(0.05, p + rng.gauss(0, step)))
        rates.append(p)
    series = {}
    for d in RAW_DIMS:
        events = [1.0 if rng.random() < r else 0.0 for r in rates]
        series[d] = _windowed_feature(events)
    return series


def test_leg_a_windowed_stable_rate_passes():
    res = leg_a_agent(_windowed_stable_rate(0.7, 400, seed=95), n_perm=300)
    assert res["passes"] is True


def test_leg_a_windowed_drifting_rate_fails():
    res = leg_a_agent(_windowed_drifting_rate(400, seed=96), n_perm=300)
    assert res["passes"] is False


# --- helper-level pins (implementation review) -------------------------------


def test_vr_length_boundary():
    rng = random.Random(97)
    xs = [rng.random() for _ in range(VR_HORIZON + 1)]   # one short of floor
    assert variance_ratio(xs, VR_HORIZON) != variance_ratio(xs, VR_HORIZON)
    xs.append(rng.random())                              # exactly h + 2
    assert variance_ratio(xs, VR_HORIZON) == variance_ratio(xs, VR_HORIZON)


def test_spearman_tie_handling():
    # ties in xs: ranks (1.5, 1.5, 3); ys strictly increasing (1, 2, 3)
    rho = _spearman([0.5, 0.5, 0.9], [0.1, 0.2, 0.3])
    expected = _spearman([1.5, 1.5, 3.0], [1.0, 2.0, 3.0])
    assert math.isclose(rho, expected)
    assert 0 < rho < 1  # ties dilute but don't destroy the correlation
    assert _spearman([0.5, 0.5], [0.1, 0.2]) != _spearman([0.5, 0.5], [0.1, 0.2])  # nan


def test_binom_exact_branch():
    assert math.isclose(_binom_p_greater_half(0, 10), 1.0)
    # P(X >= 8 | n=10, p=.5) = (45 + 10 + 1)/1024
    assert math.isclose(_binom_p_greater_half(8, 10), 56 / 1024)
    # boundary continuity: exact at n=100 vs approx at n=101, similar tails
    exact = _binom_p_greater_half(60, 100)
    approx = _binom_p_greater_half(61, 101)
    assert abs(exact - approx) < 0.02


def test_leg_b_sampled_branch_eight_agents():
    fleet = _fleet([0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85],
                   150, sigma=0.03, seed0=100)
    res = leg_b(fleet, n_perm=400)
    assert res["n_agents"] == 8
    assert res["passes"] is True


def test_leg_c_alpha_mismatch_invalidates_c_only():
    traj = {"a": _iid(0.5, 150, sigma=0.03, seed=110)}
    res = evaluate_v2(traj, alpha_mismatch={"a"})
    row = res["per_agent"][0]
    assert row["eligible"] is True
    assert row["leg_a"] is not None      # A unaffected (alpha-free)
    assert row["leg_c"] is None          # C invalidated
    assert row["leg_c_alpha_mismatch"] is True
