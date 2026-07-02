"""Deterministic checks for the Stage-B falsifier math (no DB).

residual_z_norm / auc / permutation_p are pure; drive them with hand-built
inputs so the §6.3 emission is verified on known values. The DB join itself is
exercised by running the probe against a live governance DB, not here.
"""
import math

from scripts.analysis.stage_b_viability import auc, permutation_p, residual_z_norm


def _beh(values: dict, mean: float, var: float, count: int = 100) -> dict:
    m2 = var * (count - 1)
    return {
        **values,
        "baseline_stats": {
            ch: {"mean": mean, "m2": m2, "count": count} for ch in ("E", "I", "S", "V")
        },
    }


def test_residual_z_norm_known_value():
    # every channel exactly 2 std above its mean -> RMS z == 2
    beh = _beh({"E": 0.7, "I": 0.7, "S": 0.7, "V": 0.7}, mean=0.5, var=0.01)
    assert math.isclose(residual_z_norm(beh), 2.0, rel_tol=1e-9)


def test_residual_z_norm_at_baseline_is_zero():
    beh = _beh({"E": 0.5, "I": 0.5, "S": 0.5, "V": 0.5}, mean=0.5, var=0.01)
    assert residual_z_norm(beh) == 0.0


def test_residual_z_norm_none_without_baseline():
    assert residual_z_norm({"E": 0.5}) is None
    assert residual_z_norm({}) is None
    # count < 2 or zero variance -> unusable channel
    assert residual_z_norm(
        {"E": 0.5, "baseline_stats": {"E": {"mean": 0.5, "m2": 0.0, "count": 1}}}
    ) is None


def test_residual_z_norm_skips_unusable_channels():
    # only E has a usable baseline; z_E = 3 -> RMS over 1 channel == 3
    beh = {
        "E": 0.8, "I": 0.5,
        "baseline_stats": {
            "E": {"mean": 0.5, "m2": 0.01 * 99, "count": 100},
            "I": {"mean": 0.5, "m2": 0.0, "count": 1},
        },
    }
    assert math.isclose(residual_z_norm(beh), 3.0, rel_tol=1e-9)


def test_auc_perfect_separation():
    assert auc([1.0, 2.0, 9.0, 10.0], [False, False, True, True]) == 1.0


def test_auc_anti_predictive():
    assert auc([9.0, 10.0, 1.0, 2.0], [False, False, True, True]) == 0.0


def test_auc_all_tied_is_half():
    assert auc([5.0, 5.0, 5.0, 5.0], [False, True, False, True]) == 0.5


def test_auc_partial_tie_average_rank():
    # bad tied with one good above one good: ranks (1, 2.5, 2.5, 4)
    a = auc([1.0, 3.0, 3.0, 4.0], [False, False, True, True])
    assert math.isclose(a, (2.5 + 4 - 3) / 4, rel_tol=1e-9)  # U/(n1*n2) = 3.5/4


def test_auc_single_class_undefined():
    assert auc([1.0, 2.0], [True, True]) is None
    assert auc([1.0, 2.0], [False, False]) is None


def test_permutation_p_bounds_and_determinism():
    scores = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    labels = [False, False, False, False, True, True]
    observed = auc(scores, labels)
    p1 = permutation_p(scores, labels, observed, n_perm=2000, seed=0)
    p2 = permutation_p(scores, labels, observed, n_perm=2000, seed=0)
    assert p1 == p2  # seeded -> reproducible
    assert 0.0 < p1 < 0.2  # perfect separation on 2-of-6 is rare under shuffle


def test_permutation_p_null_is_large_for_random_scores():
    scores = [1.0, 2.0, 3.0, 4.0]
    labels = [True, False, True, False]  # observed AUC = 0.5
    p = permutation_p(scores, labels, auc(scores, labels), n_perm=500, seed=1)
    assert p > 0.3
