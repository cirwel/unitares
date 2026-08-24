"""The safety-floor report must not assert things its own estimators cannot do.

Council follow-up to #1856. Four findings, all in code that PR added — an
instrument that fixed one construction and shipped four more:

  M1. The held-out line claimed a centre of `1 - quantile` "because the halves
      are exchangeable". The rank correction is distribution-free only within
      an exchangeable model; a fixed split does not establish that premise for
      longitudinal check-ins.
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


# --- M1: distribution-free under exchangeability, not unconditionally -------

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
def test_the_rank_threshold_is_distribution_free_for_exchangeable_draws(sf, name, draw):
    """The property the previous two formulas lacked.

    Gaussian ALONE is what hid the error twice: it is the case where linear
    interpolation sits closest to the rank threshold, so a gaussian-only check
    reads as agreement. A distribution-free claim is therefore tested against a
    heavy tail and a bounded support as well. Every fixture here is iid, which
    is the exchangeability premise the rank theorem requires.
    """
    q, m = 0.99, 200
    measured = _measure(sf, draw, 2 * m, q, 1500, sf.rank_conformal_threshold)
    assert measured == pytest.approx(sf.conformal_exceedance(m, q), abs=0.004), name


def test_fixed_split_does_not_make_a_longitudinal_trend_exchangeable(sf):
    """A reproducible partition cannot manufacture the theorem's premise.

    Each window has a monotone time trend plus continuous noise. Reusing seed 0
    fixes which time positions land in each half, so the calibration and holdout
    scores are not exchangeable. The mean exceedance reproduces the independent
    probe's roughly 0.4%, far from the 1% exchangeable-rank reference. This one
    low-side example does not establish a direction; the controlled probe below
    demonstrates that the violation can move the rate either way.
    """
    q, m = 0.99, 200
    rates = []
    for noise_seed in range(1000):
        rng = random.Random(noise_seed)
        safe = [0.3 * t + rng.gauss(0, 1) for t in range(2 * m)]
        calib, holdout = sf.split_sample(safe, seed=0)
        threshold = sf.rank_conformal_threshold(calib, q)
        rates.append(sum(z > threshold for z in holdout) / len(holdout))

    observed = statistics.mean(rates)
    reference = sf.conformal_exceedance(m, q)
    assert observed == pytest.approx(0.00415, abs=0.0005)
    assert observed < reference - 0.004


def test_nonexchangeability_can_miss_in_either_direction(sf):
    """A failed premise supplies no conservative or anti-conservative ordering.

    Hold the rank threshold, Gaussian noise, calibration size, and random seeds
    fixed while varying only the sign of a longitudinal drift. The flat control
    reproduces the exchangeable reference, which validates the harness. Opposite
    drifts then move exceedance to opposite sides of that reference.
    """
    q, m, windows = 0.99, 200, 2000
    rates = {}
    for slope in (0.01, 0.0, -0.01):
        exceedances = 0
        for seed in range(windows):
            rng = random.Random(seed)
            scores = [slope * t + rng.gauss(0, 1) for t in range(m + 1)]
            threshold = sf.rank_conformal_threshold(scores[:m], q)
            exceedances += scores[m] > threshold
        rates[slope] = exceedances / windows

    reference = sf.conformal_exceedance(m, q)
    assert rates == pytest.approx({0.01: 0.042, 0.0: 0.01, -0.01: 0.0005})
    assert rates[-0.01] < reference < rates[0.01]
    assert rates[0.0] == pytest.approx(reference, abs=0.0001)


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
    the exact-under-exchangeability wording comes from conformal_exceedance, so
    the report would keep claiming rank coverage while counting
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
    assert "EXACT under exchangeability" in lines
    assert "exchangeability premise is NOT ESTABLISHED" in lines
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


def test_the_printed_target_tracks_the_constant_that_decides(sf, monkeypatch):
    """The verdict line hardcoded "want >3x" while the rule used the constant.

    Set SEPARATION_MULTIPLE to 10.0 and the report printed
    "WEAK: 7.0x ...; want >3x" — a verdict contradicting its own stated target,
    in the very constant introduced to make the standard visible. Naming a
    standard is worth nothing if the printout still quotes a literal.
    """
    safe = [0.0] * 990 + [9.0] * 10      # definitional ref = 1%
    non_safe = [9.0] * 7 + [0.0] * 93    # tp = 7%

    monkeypatch.setattr(sf, "SEPARATION_MULTIPLE", 10.0)
    lines = "\n".join(sf.separation_report(safe, non_safe))
    assert "WEAK" in lines
    assert "want >10x" in lines
    assert "want >3x" not in lines

    monkeypatch.setattr(sf, "SEPARATION_MULTIPLE", 3.0)
    lines = "\n".join(sf.separation_report(safe, non_safe))
    assert "SEPARATES" in lines


def test_no_docstring_still_asserts_the_withdrawn_centre(sf):
    """R3: the retracted #1856 claim survived in a docstring, present tense.

    The earlier source scan greped two phrases that do not occur in docstrings,
    so `separation_report` went on asserting "the halves are exchangeable by
    construction, so the held-out rate is a sampling estimate centred on
    1 - quantile" while `conformal_exceedance` named that as the error and the
    report printed the contradicting number. Docstrings are shipped prose and
    are scanned as such.

    Marked retractions are exempt, per the house rule on provenance — the test
    requires the withdrawal marker to be adjacent, not the words to be absent.
    """
    import inspect

    withdrawn = "centred on `1 - quantile`"
    for name in ("separation_report", "conformal_exceedance", "clopper_pearson_upper",
                 "rank_conformal_threshold", "conformal_rank"):
        doc = inspect.getdoc(getattr(sf, name)) or ""
        if withdrawn in doc:
            window = doc[max(0, doc.index(withdrawn) - 400): doc.index(withdrawn) + 400]
            assert "WITHDRAWN" in window or "earlier version" in window, name


def test_the_module_docstring_scan_covers_docstrings(sf):
    """Guards the guard that missed R3.

    The source scan must see docstring text, not only executable lines — that
    gap is precisely why a withdrawn claim survived a passing suite.
    """
    import inspect

    doc = inspect.getdoc(sf.separation_report) or ""
    assert "WITHDRAWN" in doc
    assert "not a centre at" in doc or "not a centre" in doc


def test_the_separation_multiple_is_a_named_movable_constant(sf):
    """It was a literal 3 buried in a comparison."""
    assert sf.SEPARATION_MULTIPLE == 3.0
    source = (REPO / "scripts/analysis/eisv_stage_b_safety_floor.py").read_text()
    live = "\n".join(ln for ln in source.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "tp > SEPARATION_MULTIPLE * ref" in live
    assert "tp > 3 * ref" not in live
    # "overridable" overstated it: there is no flag, so it moves by edit only.
    assert "overridable" not in live


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


# --- second blocking review: three claims the output made that its own -------
# --- implementation did not support -----------------------------------------
#
# All three were reproduced before being fixed. They share the shape this module
# exists to catch: a printed claim computed by a different path than the value
# printed beside it.


def _quantile_report(sf, safe, non_safe, quantile=0.99):
    return "\n".join(sf.separation_report(safe, non_safe, quantile=quantile))


def _policy_report(sf, safe, non_safe, threshold):
    return "\n".join(sf.separation_report(safe, non_safe, threshold=threshold))


# 1. the dependence caveat existed only where no operator would read it

def test_policy_mode_prints_the_dependence_caveat_not_just_the_interval(sf):
    """The docstring carried it; the report did not.

    Worse than silence: the report told the reader to prefer the upper bound
    over the point estimate, while never saying that bound is itself optimistic
    under the autocorrelation these rows are known to carry.
    """
    text = _policy_report(sf, [float(i) for i in range(200)], [250.0] * 10, threshold=180.0)
    assert "one-sided 95% upper bound" in text
    low = text.lower()
    assert "independent" in low
    assert "autocorrelated" in low
    assert "anti-conservative" in low
    assert "floor on the uncertainty" in low


def test_the_reader_meets_the_caveat_before_the_verdict(sf):
    """Ordering is the whole point: a caveat printed after the conclusion is
    decoration. It has to sit between the interval it qualifies and the
    SEPARATES/WEAK line a reader would stop at."""
    text = _policy_report(sf, [float(i) for i in range(200)], [250.0] * 10, threshold=180.0)
    assert text.index("upper bound") < text.index("CAVEAT") < text.index("→")


# 2. exact-under-exchangeability equality is false when residuals tie

def test_all_tied_residuals_do_not_claim_an_exact_exceedance(sf):
    """safe=[1.0]*100 observed 0/50 while claiming an EXACT 2.0%.

    The rank derivation orders m+1 exchangeable draws, which needs a strict
    total order. Under the strict `>` at the call site every tie at the
    threshold falls on the non-exceeding side, so the realized rate lands at or
    below the rank value — an upper bound, never an equality.
    """
    text = _quantile_report(sf, [1.0] * 100, [])
    assert "EXACT under exchangeability" not in text
    assert "TIE-DEGRADED" in text
    assert "UPPER BOUND" in text
    # and it must say the observed shortfall is expected rather than a finding
    assert "expected, not evidence" in text


def test_ties_anywhere_in_the_safe_set_degrade_the_claim(sf):
    """Not only the all-tied extreme: one duplicate is enough to break equality."""
    safe = [float(i) for i in range(150)]
    safe[7] = safe[8]                       # a single tie
    assert "TIE-DEGRADED" in _quantile_report(sf, safe, [])


def test_tie_split_across_calibration_and_holdout_degrades_the_claim(sf):
    """A cross-half duplicate must not escape two per-half uniqueness checks."""
    safe = [float(i) for i in range(100)]
    calib_indices, holdout_indices = sf.split_sample(range(len(safe)), seed=0)
    safe[calib_indices[0]] = 1000.0
    safe[holdout_indices[0]] = 1000.0

    calib, holdout = sf.split_sample(safe, seed=0)
    assert len(set(calib)) == len(calib)
    assert len(set(holdout)) == len(holdout)
    assert len(set(calib + holdout)) < len(calib) + len(holdout)

    text = _quantile_report(sf, safe, [])
    assert "EXACT under exchangeability" not in text
    assert "TIE-DEGRADED" in text
    assert "UPPER BOUND" in text


def test_distinct_residuals_keep_the_exact_claim(sf):
    """The exact reference remains, with its exchangeability premise visible."""
    text = _quantile_report(sf, [float(i) for i in range(150)], [])
    assert "EXACT under exchangeability" in text
    assert "distribution-free within that model" in text
    assert "NOT ESTABLISHED" in text
    assert "TIE-DEGRADED" not in text


def test_the_tie_note_is_absent_when_there_is_nothing_to_note(sf):
    assert "UPPER BOUND" not in _quantile_report(sf, [float(i) for i in range(150)], [])


# 3. the verdict compared against the nominal rate, not the realized one

def test_quantile_mode_reports_the_rate_the_threshold_actually_realizes(sf):
    """n=150 at q=0.99 interpolates between the 148th and 149th order statistics,
    so two of 150 exceed it — 1.333%, not the nominal 1.000%."""
    text = _quantile_report(sf, [float(i) for i in range(150)], [])
    assert "1.3% (2/150)" in text
    assert "REALIZED on this sample" in text
    # the nominal is still shown, as the target it missed
    assert "Nominal target was 1.0%" in text


def test_the_nominal_rate_no_longer_drives_the_separation_verdict(sf):
    """The reviewer's counterexample, which flipped the verdict on its own.

    7/200 non-safe flagged is 3.5%. Against the nominal 1% that clears the 3x
    multiple and printed SEPARATES; against the realized 1.333% it is 2.6x,
    which is WEAK. The printed ratio also has to agree with the printed counts.
    """
    safe = [float(i) for i in range(150)]
    thr = sf.pct(safe, 0.99)
    non_safe = [thr + 1.0] * 7 + [0.0] * 193
    text = _quantile_report(sf, safe, non_safe)

    assert "WEAK" in text
    assert "SEPARATES" not in text
    assert "2.6x" in text
    assert "3.5000% / 1.3333%" in text     # the ratio reproduces from the page


def test_a_genuinely_separating_residual_still_reads_as_separating(sf):
    """The fix must not make SEPARATES unreachable — that would be the same
    defect class inverted: a verdict with a branch nothing can take."""
    safe = [float(i) for i in range(150)]
    thr = sf.pct(safe, 0.99)
    non_safe = [thr + 1.0] * 60 + [0.0] * 140      # 30%, far above 3 x 1.333%
    text = _quantile_report(sf, safe, non_safe)
    assert "SEPARATES" in text
    assert "WEAK" not in text


def test_a_zero_realized_rate_yields_no_ratio_rather_than_a_verdict(sf):
    """All-tied safe values realize 0%, and a ratio against 0 is undefined.

    This must reach the UNDEFINED branch rather than dividing, and must not be
    reported as a pass.
    """
    text = _quantile_report(sf, [1.0] * 100, [5.0] * 40)
    assert "UNDEFINED" in text
    assert "SEPARATES" not in text
