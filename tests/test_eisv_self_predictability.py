"""Deterministic checks for the label-free self-predictability eval math.

No DB: drive evaluate() / evaluate_raw() with hand-built trajectories so the
MAE / skill / individuality / AR(1)-null logic is verified on known inputs.
"""
import datetime as dt
import math
import random

from scripts.analysis.eisv_self_predictability import (
    DIMS,
    RAW_DIMS,
    WARMUP,
    WARMUP_RAW,
    _ar1_predict,
    _cadence_band,
    _extract_raw,
    _median_gap_min,
    evaluate,
    evaluate_decimation,
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


# --- cadence stratification + decimation diagnostic ------------------------


def _ts(n: int, gap_min: float) -> list[dt.datetime]:
    t0 = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
    return [t0 + dt.timedelta(minutes=gap_min * i) for i in range(n)]


def test_median_gap_and_bands():
    assert math.isclose(_median_gap_min(_ts(10, 5.0)), 5.0)
    assert _median_gap_min([]) is None
    assert _median_gap_min(_ts(1, 5.0)) is None
    assert _median_gap_min([1, 2, 3]) is None  # not datetimes
    assert _cadence_band(5.0) == "fast (<10m)"
    assert _cadence_band(30.0) == "mid (10-60m)"
    assert _cadence_band(600.0) == "slow (>=60m)"
    assert _cadence_band(None) is None


def test_evaluate_raw_reports_cadence_and_phi():
    traj = {
        "fast": _raw_noise(0.2, 120, sigma=0.05, seed=1),
        "slow": _raw_noise(0.8, 120, sigma=0.05, seed=2),
    }
    ts = {"fast": _ts(120, 5.0), "slow": _ts(120, 120.0)}
    res = evaluate_raw(traj, timestamps=ts, gate_min_states=50)
    by_agent = {a["agent"]: a for a in res["per_agent"]}
    assert by_agent["fast"]["cadence_band"] == "fast (<10m)"
    assert by_agent["slow"]["cadence_band"] == "slow (>=60m)"
    assert math.isclose(by_agent["fast"]["median_gap_min"], 5.0)
    # iid noise: fitted AR(1) slope must be near 0, nowhere near random-walk 1
    assert abs(by_agent["fast"]["phi_mean"]) < 0.4
    bands = {b["band"]: b for b in res["cadence_bands"]}
    assert bands["fast (<10m)"]["eligible"] == 1
    assert bands["slow (>=60m)"]["eligible"] == 1
    # verdict fields unchanged by stratification
    assert res["gate_majority"] is True


def test_evaluate_raw_without_timestamps_backward_compatible():
    traj = {"a": _raw_noise(0.2, 120, sigma=0.05, seed=1)}
    res = evaluate_raw(traj, gate_min_states=50)
    ag = res["per_agent"][0]
    assert ag["median_gap_min"] is None
    assert ag["cadence_band"] is None


def test_random_walk_phi_near_one():
    traj = {"a": _raw_walk(0.5, 200, step=0.02, seed=7)}
    res = evaluate_raw(traj, gate_min_states=50)
    assert res["per_agent"][0]["phi_mean"] > 0.85


def test_decimation_stable_normal_beats_persistence_at_every_k():
    """iid noise around a stable per-agent mean: a stable-normal reference
    (ema or the fitted ar1, whose intercept encodes the mean) must beat raw
    persistence at native cadence AND every decimation, with low phi."""
    traj = {"a": _raw_noise(0.3, 400, sigma=0.05, seed=11)}
    ts = {"a": _ts(400, 5.0)}
    res = evaluate_decimation(traj, timestamps=ts, gate_min_states=50)
    assert len(res["agents"]) == 1
    rows = res["agents"][0]["rows"]
    assert rows[0]["k"] == 1
    assert all(r["normal_beats_pers"] for r in rows)
    assert all(abs(r["phi_mean"]) < 0.5 for r in rows)
    # effective gap scales with k
    ks = {r["k"]: r for r in rows}
    assert math.isclose(ks[4]["eff_gap_min"], 20.0)


def test_strict_gate_asymptotically_lost_to_ar1_on_stationary_normal():
    """Pins the structural property the decimation report documents: on a
    PERFECTLY stable normal with a long sample, the expanding AR(1) fit
    (which nests the per-agent mean at phi=0) converges to the optimal
    predictor, so the fixed-alpha runtime ema loses the strict ema-vs-ar1
    comparison even though individuality is maximally true. The strict gate
    therefore tests estimator choice on stationary data — a FAIL with
    normal_beats_pers=True and low phi must not be read as individuality
    being false."""
    traj = {"a": _raw_noise(0.3, 400, sigma=0.05, seed=11)}
    res = evaluate_decimation(traj, gate_min_states=50)
    k1 = res["agents"][0]["rows"][0]
    assert k1["k"] == 1 and k1["n_scored"] > 300
    assert k1["normal_beats_pers"] is True     # individuality visible
    assert k1["ema_beats_nulls"] is False      # strict gate lost to ar1
    assert k1["mae_avg"]["ar1"] < k1["mae_avg"]["ema"]


def test_decimation_random_walk_never_beats_persistence():
    """Random walk: persistence is optimal at EVERY cadence — decimation must
    not manufacture a pass on either column (the anti-laundering direction)."""
    traj = {"a": _raw_walk(0.5, 800, step=0.02, seed=13)}
    res = evaluate_decimation(traj, gate_min_states=50)
    rows = res["agents"][0]["rows"]
    assert len(rows) >= 3  # 800 states supports several factors
    assert not any(r["ema_beats_nulls"] for r in rows)
    assert not any(r["normal_beats_pers"] for r in rows)
    assert all(r["phi_mean"] > 0.8 for r in rows)


def test_decimation_skips_underpowered_factors():
    """A 60-state series at k=4 leaves 15 states → fewer than min_scored
    predictions → the row must be dropped, not reported as noise."""
    traj = {"a": _raw_noise(0.3, 60, sigma=0.05, seed=17)}
    res = evaluate_decimation(traj, gate_min_states=50, min_scored=20)
    ks = [r["k"] for r in res["agents"][0]["rows"]]
    assert 1 in ks
    assert 4 not in ks and 8 not in ks and 16 not in ks


def test_decimation_ignores_ineligible_agents():
    traj = {"a": _raw_noise(0.3, 30, sigma=0.05, seed=19)}
    res = evaluate_decimation(traj, gate_min_states=50)
    assert res["agents"] == []
