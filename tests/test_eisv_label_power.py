"""Sanity checks for the label power / MDE calc (Hanley-McNeil)."""
from scripts.analysis.eisv_label_power import auc_se, mde_over_chance, n_bad_for_lift


def test_se_positive_and_shrinks_with_more_labels():
    assert auc_se(0.7, 20, 400) > auc_se(0.7, 200, 4000) > auc_se(0.7, 2000, 40000) > 0


def test_se_collapses_near_ceiling():
    # the artifact the report warns about: variance is smaller near AUC=1
    assert auc_se(0.94, 100, 2000) < auc_se(0.70, 100, 2000)


def test_mde_over_chance_shrinks_with_more_labels():
    assert mde_over_chance(21, 420) > mde_over_chance(114, 2287) > mde_over_chance(1000, 20000) > 0


def test_n_bad_for_lift_monotone_in_target():
    # a smaller lift requires more labels
    assert n_bad_for_lift(0.03, 2287, 0.94) > n_bad_for_lift(0.10, 2287, 0.94) > 0
