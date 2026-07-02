"""Deterministic checks for the label-free self-predictability eval math.

No DB: drive evaluate() / evaluate_raw() with hand-built trajectories so the
MAE / skill / individuality / AR(1)-null logic is verified on known inputs.
"""
import math
import random

from scripts.analysis.eisv_self_predictability import (
    DIMS,
    RAW_DIMS,
    WARMUP,
    WARMUP_RAW,
    _ar1_predict,
    _extract_raw,
    evaluate,
    evaluate_raw,
)


def _flat(level: float, n: int) -> list[dict]:
    return [{d: level for d in DIMS} for _ in range(n)]


def test_distinct_constant_agents_show_individuality():
    """Two agents at distinct constant levels: each agent's own mean predicts
    perfectly, the fleet mean is off — individuality must win everywhere."""
    traj = {"a": _flat(0.2, 12), "b": _flat(0.8, 12)}
    res = evaluate(traj)
    # constant series → per-agent expanding mean error ~0 on every dim
    for d in DIMS:
        assert res["mae"]["expanding_mean"][d] < 1e-9
        # global mean sits at 0.5 → off by 0.3 from each constant level
        assert abs(res["mae"]["global_mean"][d] - 0.3) < 1e-9
    # every agent×dim: agent mean beats global mean
    assert res["individuality_win_rate"] == 1.0
    # scored points = (12 - WARMUP) per agent, 2 agents
    assert res["n_predictions"] == 2 * (12 - WARMUP)
    assert res["n_agents"] == 2


def test_persistence_perfect_on_constant_series():
    traj = {"a": _flat(0.5, 10)}
    res = evaluate(traj)
    for d in DIMS:
        assert res["mae"]["persistence"][d] < 1e-9


def test_no_leakage_warmup_respected():
    """A short series at exactly WARMUP length yields no scored predictions."""
    traj = {"a": _flat(0.5, WARMUP)}  # only seed points, nothing to score
    # min_states default in evaluate is not applied here (caller filters), but
    # with len == WARMUP the walk-forward scores zero points.
    res = evaluate(traj)
    assert res["n_predictions"] == 0


# --- raw-series AR(1) null -----------------------------------------------


def _raw_noise(mean: float, n: int, sigma: float, seed: int) -> list[dict]:
    rng = random.Random(seed)
    return [{d: mean + rng.gauss(0, sigma) for d in RAW_DIMS} for _ in range(n)]


def _raw_walk(start: float, n: int, step: float, seed: int) -> list[dict]:
    rng = random.Random(seed)
    seq, level = [], {d: start for d in RAW_DIMS}
    for _ in range(n):
        level = {d: level[d] + rng.gauss(0, step) for d in RAW_DIMS}
        seq.append(dict(level))
    return seq


def test_extract_raw_shape_and_rejects():
    ok = {"behavioral_eisv": {"raw_obs": [0.7, 0.6, 0.2]}}
    assert _extract_raw(ok) == {"E": 0.7, "I": 0.6, "S": 0.2}
    assert _extract_raw({"behavioral_eisv": {}}) is None            # missing
    assert _extract_raw({"behavioral_eisv": {"raw_obs": [0.7, 0.6]}}) is None  # short
    assert _extract_raw({"behavioral_eisv": {"raw_obs": [0.7, "x", 0.2]}}) is None
    assert _extract_raw({}) is None
    assert _extract_raw(None) is None  # type: ignore[arg-type]


def test_ar1_predict_recovers_known_process():
    """Feed exact pairs from x_t = 0.1 + 0.5 x_{t-1}; prediction must be exact."""
    xs = [0.8]
    for _ in range(20):
        xs.append(0.1 + 0.5 * xs[-1])
    n = len(xs) - 1
    sx = sum(xs[:-1]); sy = sum(xs[1:])
    sxx = sum(x * x for x in xs[:-1]); sxy = sum(a * b for a, b in zip(xs[:-1], xs[1:]))
    pred = _ar1_predict(n, sx, sy, sxx, sxy, xs[-1], fallback=0.0)
    assert math.isclose(pred, 0.1 + 0.5 * xs[-1], rel_tol=1e-6)


def test_ar1_predict_constant_history_falls_back():
    """Constant history → slope unidentifiable → the fallback (mean) is used."""
    xs = [0.5] * 10
    n = len(xs) - 1
    sx = sy = 0.5 * n
    sxx = sxy = 0.25 * n
    assert _ar1_predict(n, sx, sy, sxx, sxy, 0.5, fallback=0.5) == 0.5


def test_raw_gate_passes_on_stable_distinct_means():
    """iid noise around distinct per-agent means: the EMA reference approaches
    the agent's mean, beating persistence (whose MAE ~ E|noise_t - noise_t-1|),
    AR(1) (no real slope to exploit), and the fleet mean (wrong level)."""
    traj = {
        "a": _raw_noise(0.2, 120, sigma=0.05, seed=1),
        "b": _raw_noise(0.8, 120, sigma=0.05, seed=2),
    }
    res = evaluate_raw(traj, gate_min_states=50)
    assert res["gate_eligible"] == 2
    assert res["gate_winners"] == 2
    assert res["gate_majority"] is True
    # scored points = (n - WARMUP_RAW) per agent
    assert res["n_predictions"] == 2 * (120 - WARMUP_RAW)


def test_raw_gate_fails_on_random_walk():
    """Random walks have no stable normal: persistence beats every reference
    and the gate must FAIL — the anti-dressed-up-autocorrelation direction."""
    traj = {
        "a": _raw_walk(0.5, 120, step=0.05, seed=3),
        "b": _raw_walk(0.5, 120, step=0.05, seed=4),
    }
    res = evaluate_raw(traj, gate_min_states=50)
    assert res["gate_eligible"] == 2
    assert res["gate_winners"] == 0
    assert res["gate_majority"] is False


def test_raw_gate_not_evaluable_below_floor():
    traj = {"a": _raw_noise(0.5, 20, sigma=0.05, seed=5)}
    res = evaluate_raw(traj, gate_min_states=50)
    assert res["gate_eligible"] == 0
    assert res["gate_majority"] is None
    assert res["n_agents"] == 1  # still scored for the pooled table
