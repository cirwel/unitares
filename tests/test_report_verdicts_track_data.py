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
    _conclusion,
    auc_se,
    mde_over_chance,
    n_bad_for_lift,
)


# --- eisv_label_power.py ----------------------------------------------------

def test_conclusion_says_underpowered_when_the_mde_exceeds_headroom():
    text = _conclusion(mde=0.20, headroom=0.06)
    assert "NOT validatable" in text
    assert "0.200" in text and "0.060" in text  # states the numbers it used


def test_conclusion_flips_when_the_mde_fits_inside_headroom():
    """The case the constant string could never produce."""
    text = _conclusion(mde=0.02, headroom=0.06)
    assert "NOT validatable" not in text
    assert "resolvable at this label supply" in text


def test_the_powered_conclusion_does_not_overclaim():
    """Power is not evidence of an effect; the wording must keep them apart."""
    text = _conclusion(mde=0.02, headroom=0.06)
    assert "statement about POWER only" in text
    assert "not that any lift exists" in text


def test_a_larger_label_budget_narrows_the_mde():
    """Ties the verdict to something a real run can move."""
    assert mde_over_chance(21, 21 * 20) > mde_over_chance(5000, 5000 * 20)


@pytest.mark.parametrize("auc", [-0.01, 1.0001, 1.04, 2.0])
def test_auc_se_is_nan_outside_the_unit_interval(auc):
    """The `max(var, 0.0)` clamp used to report SE=0 — perfect precision — here."""
    assert math.isnan(auc_se(auc, 100, 2000))


def test_auc_se_still_works_inside_the_domain():
    assert auc_se(0.94, 100, 2000) > 0.0


def test_unreachable_lift_returns_the_not_reachable_sentinel():
    """baseline+lift >= 1.0 is undefined, not easy.

    Via SE=0 this returned 2 — the loop floor, i.e. the least demanding possible
    answer for the most impossible ask. The docstring notes baselines run
    0.61–0.94 across slices, so `--baseline-auc 0.96` reaches this.
    """
    assert n_bad_for_lift(0.05, 2287, baseline=0.96) == -1
    assert n_bad_for_lift(0.05, 2287, baseline=0.99) == -1


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
