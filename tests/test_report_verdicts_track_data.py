"""Analysis reports must derive their verdicts, not assert them.

Two EISV report generators printed conclusions that no input could change:

  * `eisv_label_power.py` — `build_report` contained zero conditionals. The
    sentence "Stage-B / GROUNDING_APPLY is NOT validatable" was a literal, so
    the report said it for every label budget, including one large enough to
    make it false.
  * `eisv_latent_label_supply.py` — the gate-3 clause hardcoded `**FAIL**` on
    the line directly below one that branched correctly, and the closing
    paragraph asserted "(0 balanced)" as fixed text while the real count was
    interpolated a few lines above, so a non-zero run contradicted itself.

Same defect as the coherence-gate control (#1838) and the basin-gate validator
(#1850): an output that cannot distinguish one world from another.
"""

from __future__ import annotations

import math

import pytest

from scripts.analysis.eisv_label_power import (
    N_BAD_APPROX_INVALID,
    N_BAD_OUT_OF_DOMAIN,
    _conclusion,
    auc_se,
    mde_over_chance,
    mde_vs_baseline,
    n_bad_for_lift,
)


# --- eisv_label_power.py ----------------------------------------------------

def test_conclusion_says_underpowered_when_the_mde_exceeds_headroom():
    text = _conclusion(mde_vs_base=0.20, headroom=0.06, baseline_ci=0.05)
    assert "NOT validatable" in text
    assert "0.200" in text and "0.060" in text  # states the numbers it used


def test_conclusion_flips_when_the_mde_fits_inside_headroom():
    """The case the constant string could never produce."""
    text = _conclusion(mde_vs_base=0.02, headroom=0.06, baseline_ci=0.05)
    assert "NOT validatable" not in text
    assert "NOT ESTABLISHED" in text


def test_the_fitting_branch_does_not_claim_the_test_is_powered():
    """"Fits inside the headroom" and "powered" are different claims.

    The one-sample MDE treats the baseline as a perfectly-known constant while
    it is estimated on the same scarce slice. A categorical powered verdict
    needs a paired estimand carrying that covariance, and none is registered —
    so the honest branch is NOT ESTABLISHED, and it must say why.
    """
    text = _conclusion(mde_vs_base=0.02, headroom=0.06, baseline_ci=0.05)
    assert "NOT ESTABLISHED" in text
    assert "PAIRED" in text
    assert "not powered" in text.lower() or "not: that the comparison is powered" in text.lower()
    # It must not read as a green light.
    assert "resolvable at this label supply" not in text


def test_the_underpowered_branch_states_that_it_is_the_optimistic_bound():
    """The kill verdict is robust precisely because the number is optimistic."""
    text = _conclusion(mde_vs_base=0.20, headroom=0.06, baseline_ci=0.05)
    assert "optimistic" in text
    assert "strictly larger" in text


# --- the estimand the verdict is allowed to use -----------------------------

def test_chance_mde_and_baseline_mde_are_not_interchangeable():
    """The reviewer's counterexample: substituting one flips the verdict.

    `mde_over_chance` measures from 0.5; `mde_vs_baseline` measures from the
    baseline. Only the second is commensurate with `1 - baseline`. At this
    label budget the two straddle the headroom, so a report that compared the
    chance-MDE to the headroom would say NOT validatable about a budget its own
    commensurate arithmetic says is not ruled out.
    """
    n_bad, n_good, baseline = 50, 1000, 0.94
    headroom = 1.0 - baseline

    chance_mde = mde_over_chance(n_bad, n_good)
    baseline_mde = mde_vs_baseline(baseline, n_bad, n_good)

    assert chance_mde == pytest.approx(0.1076, abs=5e-4)
    assert baseline_mde == pytest.approx(0.0583, abs=5e-4)
    assert chance_mde > headroom > baseline_mde     # they straddle it

    assert "NOT validatable" in _conclusion(chance_mde, headroom, 0.05)
    assert "NOT validatable" not in _conclusion(baseline_mde, headroom, 0.05)


def test_the_report_feeds_the_commensurate_estimand_to_the_verdict():
    """Pins which quantity reaches `_conclusion`.

    `mde_vs_base` was computed and printed but never drove the verdict, while
    the incommensurate `mde_21` did. Rendering at the counterexample budget
    detects a regression to the wrong input without matching on source text.
    """
    import argparse

    from scripts.analysis.eisv_label_power import build_report

    args = argparse.Namespace(n_bad=50, n_good=1000, baseline_auc=0.94, target_lift=0.05)
    assert "NOT validatable" not in build_report(args)


def test_no_external_finding_is_asserted_as_settled_fact():
    """A report that recomputes from arguments must not carry fixed conclusions.

    Two survived the first pass: "the skeptic eval already found no feature
    beats the baseline at all" here, and "the autocorrelation baseline is
    unbeatable regardless" in the supply report. Neither could change with any
    input, which is the defect this module is meant to repair.
    """
    import argparse
    import inspect

    import scripts.analysis.eisv_label_power as power
    import scripts.analysis.eisv_latent_label_supply as supply
    from scripts.analysis.eisv_label_power import build_report

    args = argparse.Namespace(n_bad=114, n_good=2287, baseline_auc=0.94, target_lift=0.05)
    assert "already found no feature beats" not in build_report(args)

    for mod in (power, supply):
        # Strip comments: a comment naming a removed claim is provenance, and
        # deleting it would make the code less honest, not more portable.
        source = "\n".join(
            line for line in inspect.getsource(mod).splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "unbeatable" not in source
        assert "skeptic eval already found" not in source
        assert "More labels do not lower that bar" not in source


def test_a_larger_label_budget_narrows_the_mde():
    """Ties the verdict to something a real run can move."""
    assert mde_over_chance(21, 21 * 20) > mde_over_chance(5000, 5000 * 20)


@pytest.mark.parametrize("auc", [-0.01, 1.0001, 1.04, 2.0])
def test_auc_se_is_nan_outside_the_unit_interval(auc):
    """The `max(var, 0.0)` clamp used to report SE=0 — perfect precision — here."""
    assert math.isnan(auc_se(auc, 100, 2000))


def test_auc_se_still_works_inside_the_domain():
    assert auc_se(0.94, 100, 2000) > 0.0


def test_a_target_above_one_is_out_of_domain():
    """baseline+lift > 1.0 is undefined, not easy.

    Via SE=0 this returned 2 — the loop floor, i.e. the least demanding possible
    answer for the most impossible ask. The docstring notes baselines run
    0.61–0.94 across slices, so `--baseline-auc 0.96` reaches this.
    """
    assert n_bad_for_lift(0.05, 2287, baseline=0.96) == N_BAD_OUT_OF_DOMAIN
    assert n_bad_for_lift(0.05, 2287, baseline=0.99) == N_BAD_OUT_OF_DOMAIN


def test_a_target_at_exactly_one_is_degenerate_not_out_of_domain():
    """AUC 1.0 is a real AUC; the approximation, not the target, is what fails.

    `auc_se` accepts 1.0 — but Hanley-McNeil variance collapses to exactly 0
    there for EVERY n, so the search would return its floor of 2 and report
    perfect separation as the cheapest thing to demonstrate. Lumping it in with
    "above 1.0" would mislabel a valid AUC as no AUC, so it gets its own status.
    """
    assert auc_se(1.0, 114, 2287) == 0.0            # the degeneracy itself
    assert not math.isnan(auc_se(1.0, 114, 2287))   # in domain, unlike 1.0001

    assert n_bad_for_lift(0.06, 2287, baseline=0.94) == N_BAD_APPROX_INVALID
    assert N_BAD_APPROX_INVALID != N_BAD_OUT_OF_DOMAIN
    # ...and neither is ever mistaken for a sample size.
    assert N_BAD_APPROX_INVALID < 0 and N_BAD_OUT_OF_DOMAIN < 0


def test_the_two_boundary_statuses_render_differently():
    """A reader must be able to tell "no such AUC" from "cannot be costed"."""
    import argparse

    from scripts.analysis.eisv_label_power import build_report

    # The shipped lifts are 0.10 / 0.05 / 0.03, so a 0.95 baseline reaches both
    # boundaries in one render: 1.05 (above domain) and exactly 1.00 (degenerate).
    text = build_report(argparse.Namespace(
        n_bad=114, n_good=2287, baseline_auc=0.95, target_lift=0.05))
    assert "NOT REACHABLE" in text
    assert "NOT COSTABLE" in text
    # Neither sentinel may leak into the prose as if it were a count.
    assert "~-1 bad labels" not in text
    assert "~-2 bad labels" not in text


def test_reachable_lift_is_unchanged_at_the_shipped_default():
    """The guard must not move the answer for in-domain inputs."""
    assert n_bad_for_lift(0.05, 2287, baseline=0.94) == 13


# --- eisv_latent_label_supply.py -------------------------------------------

def _supply_report(balanced: int, trusted_eligible_q: float):
    """Render the verdict block without touching a database."""
    import scripts.analysis.eisv_latent_label_supply as supply

    FLOOR = supply.FLOOR_PER_QTR
    db = {"balanced_agents": balanced, "agents_with_bad": max(balanced, 1)}
    vol_pass = trusted_eligible_q >= FLOOR
    bal = db["balanced_agents"]
    a: list[str] = []
    if bal == 0:
        a.append("**FAIL** — 0 balanced agents.")
    else:
        a.append(f"**NOT ESTABLISHED** — {bal} balanced agents observed")
    a.append("clears" if vol_pass else "short of")
    a.append(f"({bal} balanced)")
    return "\n".join(a)


def test_supply_gate_reports_fail_only_when_nothing_is_balanced():
    assert "**FAIL**" in _supply_report(balanced=0, trusted_eligible_q=100)


def test_supply_gate_does_not_report_fail_when_agents_are_balanced():
    """The case the hardcoded literal could never produce."""
    out = _supply_report(balanced=12, trusted_eligible_q=100)
    assert "**FAIL**" not in out
    assert "NOT ESTABLISHED" in out
    assert "12 balanced" in out


def test_supply_report_source_no_longer_hardcodes_its_verdict():
    """Guards the file itself, since the helper above only models the logic."""
    import inspect

    import scripts.analysis.eisv_latent_label_supply as supply

    src = inspect.getsource(supply.build_report)
    assert 'f"**FAIL** — {db[' not in src        # the original literal clause
    assert '"agents (0 balanced)' not in src         # the frozen count in the prose
    assert 'bal = db["balanced_agents"]' in src      # derived instead
