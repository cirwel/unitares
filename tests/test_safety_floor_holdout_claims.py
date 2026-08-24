"""The safety-floor report must not assert things its own estimators cannot do.

Council follow-up to #1856. Four findings, all in code that PR added — an
instrument that fixed one construction and shipped four more:

  M1. The held-out line claimed a centre of `1 - quantile` "because the halves
      are exchangeable". Exchangeability gives the split-conformal centre
      (m - q(m-1))/(m+1), which only approaches 1-q for large m. At
      MIN_SAFE_FOR_SPLIT the calibration half is m=50 and the true centre is
      2.9% — and the holdout is 50 points, so the rate lives on a 2% grid and
      CANNOT take the claimed 1.0% under any draw.
  M2. The thin-sample branch carried the strongest disclaimer in the function
      ("Read nothing from the separation below") and then printed SEPARATES.
      It was the only "cannot support a reading" state that still emitted a
      verdict token.
  M5. MIN_SAFE_FOR_SPLIT's comment described the opposite of the module: every
      printed rate uses the full-sample quantile, not the calibration half's.
  M6. Policy mode printed rates at one decimal while computing the separation
      multiple on unrounded values, so "0.1%" and "0.3%" could carry a "6.0x"
      no number on the page reproduced — and "This IS a regression bound" was
      emitted for a rate resting on one observation.
"""

from __future__ import annotations

import importlib.util
import math
import random
import statistics
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def sf():
    spec = importlib.util.spec_from_file_location(
        "safety_floor", REPO / "scripts/analysis/eisv_stage_b_safety_floor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- M1: the centre the estimator actually has -----------------------------

@pytest.mark.parametrize("n", [100, 198, 400, 2000])
def test_expected_holdout_rate_matches_simulation(sf, n):
    """The formula is checked against the thing it predicts, not asserted."""
    q, m = 0.99, n // 2
    rates = []
    for seed in range(300):
        rng = random.Random(seed)
        safe = [rng.gauss(0, 1) for _ in range(n)]
        calib, holdout = sf.split_sample(safe, seed=seed)
        thr = sf.pct(calib, q)
        rates.append(sum(1 for z in holdout if z > thr) / len(holdout))

    assert statistics.mean(rates) == pytest.approx(
        sf.expected_holdout_rate(m, q), abs=0.004)


def test_the_old_centre_claim_was_wrong_at_the_shipped_floor(sf):
    """1-q is off by ~3x exactly where MIN_SAFE_FOR_SPLIT puts operators."""
    m = sf.MIN_SAFE_FOR_SPLIT // 2
    assert sf.expected_holdout_rate(m, 0.99) == pytest.approx(0.0292, abs=1e-3)
    assert sf.expected_holdout_rate(m, 0.99) > 2.5 * 0.01


def test_the_centre_converges_to_one_minus_q_only_for_large_m(sf):
    """Which is why the claim looked right and was not."""
    assert sf.expected_holdout_rate(50, 0.99) > 0.028
    assert sf.expected_holdout_rate(10_000, 0.99) == pytest.approx(0.01, abs=5e-4)


def test_the_report_names_the_grid_when_the_centre_is_unreachable(sf):
    """At m=50 the holdout rate cannot take the centre's value at all."""
    rng = random.Random(4)
    lines = "\n".join(sf.separation_report([rng.gauss(0, 1) for _ in range(100)], []))

    assert "centred on 2.9%" in lines
    assert "1.0% because the halves are exchangeable" not in lines
    assert f"NB m=50 < {sf.CONFORMAL_MIN_CALIB(0.99)}" in lines
    assert "grid" in lines


def test_the_grid_note_disappears_once_the_sample_supports_the_quantile(sf):
    rng = random.Random(4)
    lines = "\n".join(sf.separation_report([rng.gauss(0, 1) for _ in range(400)], []))
    assert "centred on 1.5%" in lines
    assert "NB m=" not in lines


# --- M2: no verdict token from a state that cannot support one -------------

def test_thin_sample_does_not_print_a_separation_verdict(sf):
    lines = "\n".join(sf.separation_report(
        [0.5], non_safe=[1.0, 1.1, 0.2, 0.3, 0.1, 0.05, 0.06, 0.07, 0.08, 0.09]))
    assert "THIN SAMPLE" in lines
    assert "separation NOT ASSESSED" in lines
    assert "SEPARATES" not in lines
    assert "WEAK" not in lines


def test_the_three_cannot_assess_states_now_agree(sf):
    """Empty safe, no non-safe, and thin sample all withhold the verdict."""
    for lines in (
        "\n".join(sf.separation_report([], non_safe=[1.0])),
        "\n".join(sf.separation_report([1.0] * 200, non_safe=[])),
        "\n".join(sf.separation_report([0.5], non_safe=[9.0])),
    ):
        assert "SEPARATES" not in lines


def test_a_supplied_threshold_still_assesses_a_small_sample(sf):
    """Thin-sample suppression is about the QUANTILE, not the sample size.

    With a policy threshold the rate is a real measurement regardless of n, so
    the verdict is not withheld — the interval is what qualifies it.
    """
    safe = [0.1, 0.2, 0.3, 9.9]      # one false positive, so the ratio is defined
    lines = "\n".join(sf.separation_report(safe, [9.0, 9.1], threshold=1.0))
    assert "NOT ASSESSED" not in lines
    assert "SEPARATES" in lines
    assert "(1/4)" in lines


# --- M6: precision the numbers do not have ---------------------------------

def test_the_separation_multiple_reconciles_with_printed_numbers(sf):
    """The council's case: 0.1% and 0.3% carrying a 6.0x."""
    lines = "\n".join(sf.separation_report(
        safe=[0.0] * 1999 + [9.0], non_safe=[9.0] * 3 + [0.0] * 997, threshold=1.0))

    assert "6.0x" in lines
    assert "(1/2000)" in lines and "(3/1000)" in lines      # the counts
    assert "exactly 0.3000% / 0.0500%" in lines             # and the unrounded rates
    assert 0.3000 / 0.0500 == pytest.approx(6.0)


def test_a_rate_off_a_handful_of_trials_carries_its_interval(sf):
    """1-of-5 renders 20.0%; the exact one-sided 95% upper bound is ~66%."""
    lines = "\n".join(sf.separation_report(
        [0.1, 0.2, 0.3, 0.4, 5.0], [2.0, 0.2, 3.0], threshold=1.0))
    assert "(1/5)" in lines
    assert "65.7%" in lines
    assert "Read the upper bound, not the point estimate" in lines


def test_clopper_pearson_brackets_the_binomial(sf):
    """Checked against the binomial CDF it inverts, not against a table."""
    for k, n in ((0, 10), (1, 5), (2, 1000), (7, 20)):
        u = sf.clopper_pearson_upper(k, n, alpha=0.05)
        cdf = sum(math.comb(n, i) * u ** i * (1 - u) ** (n - i) for i in range(k + 1))
        assert cdf == pytest.approx(0.05, abs=1e-6)
        assert u >= k / n


def test_clopper_pearson_edges(sf):
    assert sf.clopper_pearson_upper(5, 5) == 1.0
    assert sf.clopper_pearson_upper(0, 0) is None
    assert sf.clopper_pearson_upper(3, 2) is None


# --- M5: the constant's comment must describe the module -------------------

def test_the_split_constant_does_not_claim_to_set_the_threshold(sf):
    """Every printed rate uses the FULL-sample quantile, not the half's."""
    # Comments are stripped first: the constant carries a note quoting the
    # removed claim, and that provenance is deliberately kept. Scanning it as
    # if it were live text is the same mistake one level up.
    raw = (REPO / "scripts/analysis/eisv_stage_b_safety_floor.py").read_text()
    source = "\n".join(ln for ln in raw.splitlines()
                       if not ln.lstrip().startswith("#"))
    assert "Splitting is the whole point" not in source
    assert "the threshold is chosen on one half" not in source.lower()
    # ...and the note itself must still be there.
    assert "the threshold is chosen on one half" in raw.lower()


def test_the_printed_threshold_is_the_full_sample_quantile(sf):
    """Behavioural, not a grep: the printed value must equal pct(safe, q)."""
    rng = random.Random(9)
    safe = [rng.gauss(0, 1) for _ in range(400)]
    lines = "\n".join(sf.separation_report(safe, non_safe=[]))
    assert f"= {sf.pct(safe, 0.99):.2f}" in lines

    calib, _ = sf.split_sample(safe, seed=0)
    assert f"= {sf.pct(calib, 0.99):.2f}" not in lines.split("held-out")[0]
