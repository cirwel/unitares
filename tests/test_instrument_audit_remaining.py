"""Instruments whose readings could not come out the other way.

The sweep that produced #1838, #1850, #1852 and #1853 left five findings. Four
were confirmed and are fixed here; the fifth is recorded as clean.

Each is the same defect in a different disguise — a number that reports the
shape of its own construction rather than the data:

  * `eisv_stage_b_safety_floor` — the false-positive rate was the fraction of a
    sample above its own 99th percentile, i.e. ~1% by the definition of a
    percentile, for every possible input.
  * `stage_b_viability` — Φ was scored under a higher-is-worse convention while
    Φ is higher-is-better, so a Φ that discriminated well scored near 0 and lost
    to the residual almost regardless of the data.
  * `calibration_gate_status` — the "is this green REAL?" watch required a
    currently-blocking bin, so it could only fire while the gate was RED.
  * `eisv_stage_a_feasibility` — the residual reading was two fixed numbers
    asserted below the code that computes them, and wrong for 2 of 5 classes.

`scripts/dev/adoption_kpi.py` was the fifth, and this file originally recorded
it as clean. THAT WAS WRONG (corrected 2026-08-24 after independent review):
its `cohort_engaged` predicate filtered on the dead `search_knowledge_graph`
alias and omitted the live `search_shared_memory`, understating engagement. The
naming defect was indeed already repaired, but "the naming was fixed" is not
"the file is clean", and the test shipped here asserted only that no verdict
token appears — which cannot see a wrong tool name. See
tests/test_adoption_kpi_tool_names.py.
"""

from __future__ import annotations

import importlib.util
import math
import random
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load(relpath, name):
    """Import a script by path — these live outside any package."""
    spec = importlib.util.spec_from_file_location(name, REPO / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- eisv_stage_b_safety_floor: the in-sample identity ----------------------

@pytest.fixture(scope="module")
def floor_mod():
    return _load("scripts/analysis/eisv_stage_b_safety_floor.py", "safety_floor")


@pytest.mark.parametrize("draw,label", [
    (lambda r: r.gauss(0, 1), "normal"),
    (lambda r: r.paretovariate(1.1), "heavy-tail"),
    (lambda r: r.gauss(0, 1) if r.random() < 0.5 else r.gauss(20, 1), "bimodal"),
])
def test_the_old_in_sample_rate_is_a_constant(floor_mod, draw, label):
    """Demonstrates the defect: it is ~1% for any distribution whatsoever.

    This is the calculation the script used to print as "false-positive rate
    among currently-safe ... (want low)". It measures the definition of a
    percentile, not the residual, so the instrument could not withhold a pass.
    """
    rng = random.Random(7)
    for n in (200, 2000):
        sample = [draw(rng) for _ in range(n)]
        thr = floor_mod.pct(sample, 0.99)
        in_sample_fp = sum(1 for z in sample if z > thr) / len(sample)
        assert in_sample_fp == pytest.approx(0.01, abs=1e-9), label


def test_quantile_mode_reports_its_rate_as_definitional_not_measured(floor_mod):
    """The obvious fix (split-sample) is not enough, and the report says so.

    A threshold DEFINED as a quantile of the safe distribution fixes its own
    false-positive rate on that distribution. Splitting into calibration and
    holdout halves stops the rate being an exact identity, but it still does not
    bound regression risk: the held-out number is a noise check on the quantile.

    An earlier version of this docstring finished that sentence with "a sampling
    estimate centred on 1-q, because the halves are exchangeable". That is the
    #1856 claim #1862 retracted, and it survived here in a TEST docstring after
    being withdrawn from the module — so the suite documenting the retraction
    still asserted the retracted thing. Exchangeability gives the RANK result in
    `conformal_exceedance`, not a centre at 1-q.

    The assertion below checks the PROPERTY (the rate is reported as
    definitional, not as a measurement) rather than one phrasing of it. The
    wording moved when the report began printing the rate its threshold actually
    realizes instead of the nominal 1-q; the property did not.
    """
    rng = random.Random(11)
    safe = [rng.gauss(0, 1) for _ in range(1000)]
    lines = "\n".join(floor_mod.separation_report(safe, non_safe=[], seed=0))

    assert "definitional" in lines and "not measured" in lines
    # ...and it must not sell the quantile-mode rate as a bound
    assert "This IS a regression bound" not in lines
    assert "CANNOT bound regression risk" in lines
    assert "Still not a regression bound" in lines
    assert "--threshold" in lines           # names the mode that does bound it


def test_policy_mode_yields_a_rate_that_can_come_out_anywhere(floor_mod):
    """With an externally chosen threshold, fp is a real measurement.

    Same safe sample, two supplied thresholds: one below almost everything and
    one above almost everything. A definitional rate could not do this.
    """
    safe = [float(i) for i in range(1000)]

    low = "\n".join(floor_mod.separation_report(safe, [], threshold=10.0))
    high = "\n".join(floor_mod.separation_report(safe, [], threshold=990.0))

    assert "98.9%" in low          # flags nearly every healthy check-in
    assert "0.9%" in high
    for text in (low, high):
        assert "This IS a regression bound" in text
        assert "BY CONSTRUCTION" not in text


def test_policy_mode_separation_uses_the_measured_rate(floor_mod):
    """The old comparison floored fp at 0.01 — the constant the identity gave.

    A flag rate of 30% would have cleared `tp > 3 * max(fp, 0.01)`. Measured
    against a real 50% false-positive rate it must read WEAK.
    """
    safe = [float(i) for i in range(1000)]
    non_safe = [999.0] * 30 + [0.0] * 70          # 30% above the threshold
    lines = "\n".join(floor_mod.separation_report(safe, non_safe, threshold=499.0))

    assert "measured false-positive rate" in lines
    assert "WEAK" in lines and "SEPARATES" not in lines


def test_a_thin_sample_says_its_threshold_is_noise(floor_mod):
    """At n=20 a p99 threshold is set by the top value or two."""
    lines = "\n".join(floor_mod.separation_report([1.0] * 20, non_safe=[9.0], seed=0))
    assert "THIN SAMPLE" in lines
    assert "Read nothing from the separation below" in lines


def test_an_empty_safe_set_measures_nothing(floor_mod):
    lines = "\n".join(floor_mod.separation_report([], non_safe=[9.0], seed=0))
    assert "SKIPPED" in lines
    assert "Nothing was measured" in lines
    assert "SEPARATES" not in lines


def test_no_non_safe_rows_is_not_measured_either(floor_mod):
    lines = "\n".join(floor_mod.separation_report([1.0] * 200, non_safe=[], seed=0))
    assert "NOT MEASURED" in lines
    assert "not a pass" in lines
    assert "SEPARATES" not in lines


def test_a_low_measured_rate_is_not_floored_up_to_one_percent(floor_mod):
    """The other direction of the same defect.

    `max(fp, 0.01)` did not only hide a HIGH false-positive rate — it also
    inflated a genuinely low one up to the identity's constant, turning a
    clearly separating threshold into WEAK. Here the measured rate is 0.2% and
    the flag rate 2%: 10x separation, which the floor would report as 2x.
    """
    safe = [float(i) for i in range(1000)]          # 998, 999 exceed -> fp = 0.2%
    non_safe = [999.0] * 2 + [0.0] * 98             # tp = 2%
    lines = "\n".join(floor_mod.separation_report(safe, non_safe, threshold=997.5))

    assert "0.2%" in lines
    assert "SEPARATES" in lines
    assert "10.0x" in lines
    assert "WEAK" not in lines


def test_the_split_is_reproducible(floor_mod):
    """A seeded split cannot be shopped by re-running."""
    safe = [float(i) for i in range(400)]
    assert floor_mod.split_sample(safe, seed=0) == floor_mod.split_sample(safe, seed=0)
    assert floor_mod.split_sample(safe, seed=0) != floor_mod.split_sample(safe, seed=1)


# --- stage_b_viability: the sign of Φ ---------------------------------------

@pytest.fixture(scope="module")
def viability_mod():
    return _load("scripts/analysis/stage_b_viability.py", "stage_b_viability")


def test_phi_is_higher_is_better_in_governance_core():
    """The premise of the fix, read from the source of truth, not assumed."""
    from governance_core.scoring import phi_objective, verdict_from_phi

    assert "higher is better" in phi_objective.__doc__
    assert verdict_from_phi(0.5) == "safe"
    assert verdict_from_phi(-0.5) == "high-risk"


def test_raw_phi_scored_as_higher_is_worse_inverts_a_good_predictor(viability_mod):
    """A perfectly discriminating Φ scored AUC 0.0 under the old call."""
    labels = [True] * 20 + [False] * 20
    # Bad outcomes have LOW Φ — the correct polarity for a working Φ.
    phis = [-1.0] * 20 + [1.0] * 20

    assert viability_mod.auc(phis, labels) == pytest.approx(0.0)      # the old call
    assert viability_mod.auc([-p for p in phis], labels) == pytest.approx(1.0)


def test_the_inversion_flipped_the_comparison_against_phi(viability_mod):
    """Why it mattered: the bias ran toward the option being proposed.

    A Φ that separates perfectly (true AUC 1.0) versus a residual that barely
    beats chance. Scored raw, Φ reports 0.0 and 'loses' to the residual.
    """
    labels = [True] * 20 + [False] * 20
    phis = [-1.0] * 20 + [1.0] * 20
    residuals = [1.0] * 11 + [0.0] * 9 + [0.0] * 20   # weak, higher-is-worse

    a_res = viability_mod.auc(residuals, labels)
    a_phi_raw = viability_mod.auc(phis, labels)
    a_phi_fixed = viability_mod.auc([-p for p in phis], labels)

    assert a_res > a_phi_raw           # the old verdict: "residual > Φ"
    assert a_phi_fixed > a_res         # the truth: Φ is the better predictor


def test_the_source_negates_phi_before_scoring(viability_mod):
    """Pins the call site, so a revert to raw Φ fails here."""
    import inspect

    source = inspect.getsource(viability_mod.emit_auc)
    assert "auc([-p for p in phis], labels)" in source
    assert "auc(phis, labels)" not in source


# --- calibration_gate_status: a check that could not fire when green --------

class _Bin:
    def __init__(self, count, expected, actual, lo):
        self.count = count
        self.expected_accuracy = expected
        self.accuracy = actual
        self.bin_range = (lo, lo + 0.1)


def _near_floor(bins_by_key, min_samples, gate):
    """The repaired selection rule, mirrored so it can be tested without a DB."""
    margin = 5
    out = []
    for key, m in sorted(bins_by_key.items()):
        gap = m.expected_accuracy - m.accuracy
        would_gate = gap > gate or (m.bin_range[0] >= 0.8 and m.accuracy < 0.7)
        if would_gate and m.count < min_samples + margin:
            out.append(key)
    return out


def test_the_watch_fires_on_a_green_gate():
    """The defect: an unpopulated overconfident bin is exactly the artifact.

    A bin that dropped BELOW min_samples takes its overconfidence out of the
    gate's view, turning the flag green. The old rule required the bin to be
    currently gating — which requires it to be populated — so the one situation
    the watch exists to catch was the one it could not see.
    """
    gate = 0.20
    bins = {"0.6-0.7": _Bin(count=8, expected=0.65, actual=0.20, lo=0.6)}

    # Old rule: `gates` requires populated, so a green gate emptied the watch.
    old = [k for k, m in bins.items()
           if (m.count >= 10 and m.bin_range[0] < 0.8
               and m.expected_accuracy - m.accuracy > gate)
           and m.count < 15]
    assert old == []

    assert _near_floor(bins, min_samples=10, gate=gate) == ["0.6-0.7"]


def test_the_watch_still_catches_a_barely_populated_blocker():
    """The case the old rule did cover must keep working."""
    bins = {"0.6-0.7": _Bin(count=11, expected=0.65, actual=0.20, lo=0.6)}
    assert _near_floor(bins, min_samples=10, gate=0.20) == ["0.6-0.7"]


def test_a_well_calibrated_thin_bin_is_not_flagged():
    """Low samples alone is not a cheap-green risk — overconfidence is."""
    bins = {"0.6-0.7": _Bin(count=3, expected=0.65, actual=0.64, lo=0.6)}
    assert _near_floor(bins, min_samples=10, gate=0.20) == []


def test_a_well_populated_blocker_is_not_a_cheap_green_risk():
    """It cannot vanish by depopulating, so it does not belong on the watch."""
    bins = {"0.6-0.7": _Bin(count=500, expected=0.65, actual=0.20, lo=0.6)}
    assert _near_floor(bins, min_samples=10, gate=0.20) == []


def test_the_script_qualifies_bins_by_overconfidence_not_by_gating():
    """Pins the repaired rule at its real call site."""
    source = (REPO / "scripts/dev/calibration_gate_status.py").read_text()
    assert "would_gate and m.count < min_samples + NEAR_FLOOR_MARGIN" in source
    assert "if gates and m.count < min_samples + 5:" not in source


# --- eisv_stage_a_feasibility: numbers asserted below the code -------------

@pytest.fixture(scope="module")
def feasibility_report():
    mod = _load("scripts/analysis/eisv_stage_a_feasibility.py", "stage_a_feasibility")
    return mod.build_report()


def test_the_residual_reading_is_derived_per_class(feasibility_report):
    """The old text named one dominant axis for every class. It was wrong.

    "Residual after S-only fix is dominated by E* (0.805 vs healthy ~0.73)" was
    a fixed sentence. Derived per class, two of the five are dominated by I.
    """
    # Not a digit ban — 0.805 is the equilibrium E and the report prints it
    # legitimately. What must be gone is the fixed SENTENCE asserting it.
    assert "dominated by E* (" not in feasibility_report
    assert "healthy ~0.73" not in feasibility_report

    dominated = [line.split("dominated by ")[1].split(" ")[0]
                 for line in feasibility_report.splitlines()
                 if "dominated by " in line]
    assert len(dominated) >= 5
    assert len(set(dominated)) > 1, "a per-class reading that never varies is a constant"


def test_the_ceiling_is_labelled_as_a_definition(feasibility_report):
    """manifold=1.0 under full alignment is arithmetic, not a finding."""
    assert "by construction" in feasibility_report
    assert "establishes nothing about" in feasibility_report


def test_importing_the_module_does_not_run_the_integration():
    """It used to execute and print at import, which is why it had no tests."""
    mod = _load("scripts/analysis/eisv_stage_a_feasibility.py", "stage_a_feasibility_2")
    assert callable(mod.build_report)
    assert callable(mod.main)


# --- adoption_kpi: the finding I got wrong ---------------------------------

def test_adoption_kpi_asserts_no_verdict():
    """The NO-VERDICT property still holds. THE "CLEAN" VERDICT DID NOT.

    CORRECTED 2026-08-24, independent review. This sweep judged
    `adoption_kpi.py` clean and shipped only the assertion below. The file did
    carry a real defect: the `cohort_engaged` predicate filtered on the dead
    `search_knowledge_graph` alias and omitted the live `search_shared_memory`,
    so an agent whose only value action was a shared-memory search read as not
    engaged and the metric UNDERSTATED engagement. The file's own note recorded
    that rename; the correction was applied to two queries and missed in a third.

    This test could not have caught it. It asserts what the module refrains
    from SAYING, not what its queries SELECT, so it passed over a wrong tool
    name while reading as coverage — the same shape as the instruments this
    sweep was repairing. Coverage of the actual defect is in
    tests/test_adoption_kpi_tool_names.py; this one is kept for the narrower
    property it does check, and relabelled so it no longer stands as a
    clean bill of health.
    """
    source = (REPO / "scripts/dev/adoption_kpi.py").read_text()

    assert "voluntary KG retrieval" not in source.replace('WAS "voluntary KG retrieval"', "")
    assert "What it CANNOT support" in source

    body = source.split('"""', 2)[2]           # past the module docstring
    for banned in ("PASS", "FAIL", "→ KILL", "recommend removing"):
        assert banned not in body, f"a verdict token appeared in adoption_kpi: {banned}"
