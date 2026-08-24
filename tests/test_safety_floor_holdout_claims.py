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


# --- M1: distribution-free, tested against distributions that break fits ----

DISTRIBUTIONS = [
    ("gaussian", lambda r: r.gauss(0, 1)),
    ("pareto-1.1", lambda r: r.paretovariate(1.1)),      # heavy tail
    ("pareto-0.5", lambda r: r.paretovariate(0.5)),      # no finite mean
    ("uniform", lambda r: r.random()),                   # bounded
    ("exponential", lambda r: r.expovariate(1.0)),
]


def _measure(sf, draw, n, q, seeds, threshold_fn):
    rates = []
    for seed in range(seeds):
        rng = random.Random(seed)
        safe = [draw(rng) for _ in range(n)]
        calib, holdout = sf.split_sample(safe, seed=seed)
        thr = threshold_fn(calib, q)
        rates.append(sum(1 for z in holdout if z > thr) / len(holdout))
    return statistics.mean(rates)


@pytest.mark.parametrize("name,draw", DISTRIBUTIONS)
def test_the_rank_threshold_is_distribution_free(sf, name, draw):
    """The property the previous two formulas lacked.

    Gaussian ALONE is what hid the error twice: it is the case where linear
    interpolation sits closest to the rank threshold, so a gaussian-only check
    reads as agreement. A distribution-free claim is therefore tested against a
    heavy tail and a bounded support as well.
    """
    q, m = 0.99, 200
    measured = _measure(sf, draw, 2 * m, q, 1500, sf.rank_conformal_threshold)
    assert measured == pytest.approx(sf.conformal_exceedance(m, q), abs=0.004), name


def test_the_interpolated_quantile_is_NOT_distribution_free(sf):
    """Pins the defect, so nobody reintroduces pct() here.

    At m=50 the old formula returned a flat 2.9216% for every distribution
    while the truth ranged 2.30%-2.93%.
    """
    q = 0.99
    rates = {name: _measure(sf, draw, 100, q, 2000, sf.pct)
             for name, draw in DISTRIBUTIONS}

    spread = max(rates.values()) - min(rates.values())
    assert spread > 0.004, f"interpolation should vary by distribution: {rates}"
    # ...and the heavy tail sits well below the bounded case.
    assert rates["pareto-0.5"] < rates["uniform"] - 0.004


def test_conformal_exceedance_is_the_rank_identity(sf):
    """1 - k/(m+1), checked against the definition rather than a simulation."""
    for m, q in ((50, 0.99), (99, 0.99), (200, 0.99), (1000, 0.99), (40, 0.95)):
        k = min(math.ceil((m + 1) * q), m)
        assert sf.conformal_rank(m, q) == k
        assert sf.conformal_exceedance(m, q) == pytest.approx(1 - k / (m + 1))


def test_the_boundary_the_reviewer_named(sf):
    """m=99 must give 1%, not the 1.98% the interpolated formula reported."""
    assert sf.conformal_exceedance(99, 0.99) == pytest.approx(0.01)


def test_below_the_conformal_minimum_the_threshold_degenerates(sf):
    """k>m means the calibration MAX, and exceedance is 1/(m+1)."""
    m, q = 50, 0.99
    assert m < sf.CONFORMAL_MIN_CALIB(q)
    assert sf.conformal_rank(m, q) == m
    assert sf.conformal_exceedance(m, q) == pytest.approx(1 / (m + 1))

    xs = [float(i) for i in range(m)]
    assert sf.rank_conformal_threshold(xs, q) == max(xs)


def test_the_threshold_is_an_order_statistic_never_between_two(sf):
    """The structural property interpolation broke."""
    rng = random.Random(2)
    xs = [rng.gauss(0, 1) for _ in range(200)]
    assert sf.rank_conformal_threshold(xs, 0.99) in xs
    # pct, by contrast, generally is not one of the inputs.
    assert sf.pct(xs, 0.995) not in xs


def test_the_report_COUNTS_against_the_rank_threshold(sf):
    """Binds the CALL SITE, not just the helper.

    Reverting the noise check to pct(calib, q) left every other test passing:
    the "EXACT distribution-free" wording comes from conformal_exceedance, so
    the report would keep claiming distribution-free coverage while counting
    against an interpolated threshold. That is the same label-says-one-thing /
    value-computed-another-way defect this whole sweep exists to remove, and a
    mutation caught it here rather than a reviewer catching it later.

    Heavy-tailed data where the two thresholds genuinely disagree: at this seed
    the rank threshold yields 2 exceedances and pct yields 5.
    """
    rng = random.Random(2)
    safe = [rng.paretovariate(0.4) for _ in range(400)]
    calib, holdout = sf.split_sample(safe, seed=2)

    by_rank = sum(1 for z in holdout if z > sf.rank_conformal_threshold(calib, 0.99))
    by_pct = sum(1 for z in holdout if z > sf.pct(calib, 0.99))
    assert by_rank != by_pct, "fixture no longer discriminates the two thresholds"

    lines = "\n".join(sf.separation_report(safe, non_safe=[], seed=2))
    assert f"({by_rank}/{len(holdout)})" in lines
    assert f"({by_pct}/{len(holdout)})" not in lines


def test_the_report_states_the_exact_exceedance_not_a_fitted_centre(sf):
    rng = random.Random(4)
    lines = "\n".join(sf.separation_report([rng.gauss(0, 1) for _ in range(400)], []))
    assert "EXACT distribution-free" in lines
    assert "1 - 199/201" in lines
    assert "centred on" not in lines
    assert "because the halves are exchangeable" not in lines


def test_the_degenerate_case_says_so_in_the_report(sf):
    rng = random.Random(4)
    lines = "\n".join(sf.separation_report([rng.gauss(0, 1) for _ in range(100)], []))
    assert "no p99 rank exists" in lines
    assert "degenerates to" in lines


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


def test_clopper_pearson_survives_a_large_n(sf):
    """OverflowError at n=2000, k=1000: math.comb(2000,1000) is ~600 digits.

    Multiplying that integer by a float raised before any probability was
    computed. The safe set is thousands of check-ins, so this is the ordinary
    case, not an edge.
    """
    u = sf.clopper_pearson_upper(1000, 2000)
    assert u is not None
    assert 0.5 < u < 0.55

    assert sf.clopper_pearson_upper(4999, 5000) is not None
    assert sf.clopper_pearson_upper(2500, 5000) is not None


def test_the_interval_states_that_it_assumes_independence(sf):
    """The rows are longitudinal per-agent check-ins, which autocorrelate.

    Under positive dependence the effective sample is smaller than n, so the
    interval is anti-conservative — a floor on the uncertainty, not the
    uncertainty. That has to be said where the number is defined.
    """
    doc = sf.clopper_pearson_upper.__doc__
    assert "ANTI-CONSERVATIVE" in doc.upper()
    assert "independent" in doc.lower()
    assert "autocorrelat" in doc.lower()


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


# --- standards in force are named as applied, not deferred -----------------

def test_the_report_names_the_standards_it_is_applying(sf):
    """Calling a live default "the operator's" is applying it, not deferring it.

    MIN_SAFE_FOR_SPLIT, quantile mode as the default verdict path, and the
    separation multiple each decide an output of the run that prints them.
    """
    rng = random.Random(4)
    lines = "\n".join(sf.separation_report([rng.gauss(0, 1) for _ in range(400)], []))

    assert "STANDARDS IN FORCE ON THIS RUN (not deferred — applied)" in lines
    assert f"MIN_SAFE_FOR_SPLIT={sf.MIN_SAFE_FOR_SPLIT}" in lines
    assert "DEFAULT verdict path" in lines
    assert "3x multiple" in lines
    # ...and it states the value its own maths implies, rather than hiding it.
    assert f"is {2 * sf.CONFORMAL_MIN_CALIB(0.99)}" in lines


def test_the_separation_multiple_is_a_named_movable_constant(sf):
    """It was a literal 3 buried in a comparison."""
    assert sf.SEPARATION_MULTIPLE == 3.0
    source = (REPO / "scripts/analysis/eisv_stage_b_safety_floor.py").read_text()
    assert "tp > SEPARATION_MULTIPLE * ref" in source
    assert "tp > 3 * ref" not in source


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
