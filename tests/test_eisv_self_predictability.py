"""Deterministic checks for the label-free self-predictability eval math.

No DB: drive evaluate() with hand-built trajectories so the MAE / skill /
individuality logic is verified on known inputs.
"""
from scripts.analysis.eisv_self_predictability import DIMS, WARMUP, evaluate


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
