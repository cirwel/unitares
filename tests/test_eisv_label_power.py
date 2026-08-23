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
    # a smaller lift requires more labels — compared within AUC's domain.
    #
    # This used lift=0.10 against baseline=0.94, i.e. a target AUC of 1.04. The
    # Hanley-McNeil variance is negative there and the old `max(var, 0.0)` clamp
    # reported SE = 0.0, so the search returned its loop floor of 2 and this
    # assertion passed on the least demanding possible answer to an undefined
    # question. Monotonicity held for a spurious reason, and the test pinned the
    # defect in place. 0.03 -> 99 also reconciles with POWER_NEED_BAD in
    # eisv_latent_label_supply.py, which cites this function for that figure.
    assert n_bad_for_lift(0.02, 2287, 0.94) > n_bad_for_lift(0.03, 2287, 0.94) > 0
    assert n_bad_for_lift(0.03, 2287, 0.94) > n_bad_for_lift(0.05, 2287, 0.94) > 0


def test_n_bad_for_lift_rejects_a_target_at_or_above_auc_one():
    """baseline + lift >= 1.0 is undefined, not cheap. -1 is the sentinel."""
    assert n_bad_for_lift(0.06, 2287, 0.94) == -1   # exactly 1.00
    assert n_bad_for_lift(0.10, 2287, 0.94) == -1   # 1.04, the case above
