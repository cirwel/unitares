"""The gate-status read must report what answered, not what was configured.

Three defects, all of the class the #1856 sweep was closing — a reading that
reports its own construction:

  1. The source label came from `checker._backend`, a config value read once
     from UNITARES_CALIBRATION_BACKEND and never mutated. It printed
     "postgres(canonical)  live DB" whenever postgres was merely *configured*,
     including immediately after the connection failed. `load_state_async`
     cannot surface that through an exception — it catches its own, and falls
     through SILENTLY when the row is missing or its `bins` are empty.
  2. Snapshot age was suppressed whenever the source claimed postgres, removing
     the one field that would have exposed a stale read at exactly the moment
     the label was wrong.
  3. The gate flag reads True on an empty tactical table. The gate is TACTICAL;
     with no populated tactical bin, no bin can be overconfident, so it passes
     vacuously. `check_calibration`'s no-data guard does not catch this: it
     requires BOTH strategic and tactical empty, so one unrelated strategic bin
     satisfies it.

Reproduced together on a host with no database: connection error on stderr,
"source: postgres(canonical)  live DB", VERDICT GREEN, zero tactical bins —
printed beside the state's own "No tactical data yet" note.

These tests drive the real `build_report` through the `_load_checker` seam. They
do not re-implement its rules: a test that mirrors the logic it is checking is
the same defect one level up.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def gate_mod():
    spec = importlib.util.spec_from_file_location(
        "calibration_gate_status", REPO / "scripts/dev/calibration_gate_status.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bin(count, expected, actual, lo):
    from src.calibration import CalibrationBin
    return CalibrationBin(
        bin_range=(lo, lo + 0.1),
        count=count,
        predicted_correct=int(round(expected * count)),
        actual_correct=int(round(actual * count)),
        accuracy=actual,
        expected_accuracy=expected,
        calibration_error=abs(actual - expected),
    )


class _StubChecker:
    """Minimal stand-in for CalibrationChecker — no database, no state file."""

    def __init__(self, tactical, is_calibrated=True, state_file="/nonexistent"):
        self._tactical = tactical
        self._is_calibrated = is_calibrated
        self.state_file = state_file

    def check_calibration(self, min_samples_per_bin=10, include_complexity=True):
        return self._is_calibrated, {"issues": [], "advisories": []}

    def compute_tactical_metrics(self):
        return self._tactical


def _report(gate_mod, monkeypatch, tactical, source="json_snapshot",
            is_calibrated=True, min_samples=10):
    checker = _StubChecker(tactical, is_calibrated=is_calibrated)
    monkeypatch.setattr(gate_mod, "_load_checker", lambda: (checker, source))
    monkeypatch.setattr(gate_mod, "_snapshot_age_seconds", lambda c: 4815.0)
    return gate_mod.build_report(min_samples)


# --- 3. a gate that cannot fail has not passed ------------------------------

def test_empty_tactical_table_is_unassessed_not_green(gate_mod, monkeypatch, capsys):
    """The reproduced case: flag True, zero tactical bins, nothing checked."""
    r = _report(gate_mod, monkeypatch, tactical={}, is_calibrated=True)

    assert r["calibrated"] is True          # the flag is reported as given...
    assert r["assessable"] is False         # ...and named as resting on nothing
    assert r["populated_tactical_bins"] == 0

    gate_mod.print_report(r)
    out = capsys.readouterr().out
    assert "UNASSESSED" in out
    assert "This is not a pass" in out
    assert "GREEN (calibrated)" not in out


def test_bins_below_the_sample_floor_are_still_unassessed(gate_mod, monkeypatch):
    """Present but unpopulated is the same as absent, for a gate that skips them."""
    r = _report(gate_mod, monkeypatch,
                tactical={"0.6-0.7": _bin(count=3, expected=0.65, actual=0.64, lo=0.6)})
    assert r["assessable"] is False
    assert r["total_tactical_bins"] == 1 and r["populated_tactical_bins"] == 0


def test_one_populated_bin_makes_the_gate_assessable(gate_mod, monkeypatch, capsys):
    """The other direction — a real green must still be able to read GREEN."""
    r = _report(gate_mod, monkeypatch,
                tactical={"0.6-0.7": _bin(count=50, expected=0.65, actual=0.64, lo=0.6)},
                is_calibrated=True)
    assert r["assessable"] is True

    gate_mod.print_report(r)
    out = capsys.readouterr().out
    assert "GREEN (calibrated)" in out
    assert "UNASSESSED" not in out


def test_a_real_failure_still_reads_red(gate_mod, monkeypatch, capsys):
    r = _report(gate_mod, monkeypatch,
                tactical={"0.6-0.7": _bin(count=50, expected=0.65, actual=0.20, lo=0.6)},
                is_calibrated=False)
    gate_mod.print_report(r)
    out = capsys.readouterr().out
    assert "RED (miscalibrated)" in out
    assert "UNASSESSED" not in out


# --- 2. staleness must survive the live-DB label ---------------------------

def test_snapshot_age_is_printed_even_on_the_live_db_path(gate_mod, monkeypatch, capsys):
    """Age used to vanish whenever the label claimed postgres."""
    r = _report(gate_mod, monkeypatch, tactical={}, source="postgres(canonical)")
    gate_mod.print_report(r)
    out = capsys.readouterr().out
    assert "live DB" in out
    assert "4815s old" in out          # the field that exposes a stale read


def test_snapshot_age_is_printed_on_the_json_path_too(gate_mod, monkeypatch, capsys):
    r = _report(gate_mod, monkeypatch, tactical={}, source="json_snapshot")
    out_r = gate_mod.print_report(r) or capsys.readouterr().out
    assert "4815s old" in out_r
    assert "live DB" not in out_r


# --- a verdict must never mask a failure -----------------------------------

def _report_v(gate_mod, monkeypatch, tactical, is_calibrated, issues=()):
    class _C(_StubChecker):
        def check_calibration(self, min_samples_per_bin=10, include_complexity=True):
            return is_calibrated, {"issues": list(issues), "advisories": []}

    checker = _C(tactical, is_calibrated=is_calibrated)
    monkeypatch.setattr(gate_mod, "_load_checker", lambda: (checker, "json_snapshot"))
    monkeypatch.setattr(gate_mod, "_snapshot_age_seconds", lambda c: 100.0)
    return gate_mod.build_report(10)


def test_unassessed_never_masks_a_red(gate_mod, monkeypatch, capsys):
    """The reviewer's case, pinned.

    is_calibrated=False for a strategic reason, with zero tactical bins, used
    to print three contradictory readings on one page: "VERDICT: UNASSESSED",
    "these keep it RED", and "Distance to green: GREEN" — with the suppressing
    one in the headline.

    A vacuous GREEN overstates health on a state nothing checked. A vacuous
    UNASSESSED hides a finding that WAS checked and failed, which is worse.
    """
    r = _report_v(gate_mod, monkeypatch, tactical={}, is_calibrated=False,
                  issues=["strategic danger: confident agents trending unhealthy"])
    gate_mod.print_report(r)
    out = capsys.readouterr().out

    assert "VERDICT: RED (miscalibrated)" in out
    assert "UNASSESSED" not in out.split("VERDICT:")[1].split("\n")[0]
    assert "Distance to green: GREEN" not in out
    # ...and the tactical arm is still qualified, just not in the headline.
    assert "Tactical arm UNASSESSED" in out
    assert "rests on the issues listed below" in out


def test_the_three_verdict_axes_do_not_contradict(gate_mod, monkeypatch, capsys):
    """No page may carry two different verdicts. All four states."""
    cases = [
        ({}, False, ["strategic danger"], "RED (miscalibrated)"),
        ({}, True, [], "UNASSESSED"),
        ({"0.6-0.7": _bin(50, 0.65, 0.64, 0.6)}, True, [], "GREEN (calibrated)"),
        ({"0.6-0.7": _bin(50, 0.65, 0.20, 0.6)}, False, ["overconfident"],
         "RED (miscalibrated)"),
    ]
    for tactical, ok, issues, expected in cases:
        r = _report_v(gate_mod, monkeypatch, tactical, ok, issues)
        gate_mod.print_report(r)
        out = capsys.readouterr().out
        headline = out.split("VERDICT: ")[1].split("\n")[0]
        assert expected in headline, (expected, headline)
        # "Distance to green: GREEN" may appear ONLY under a GREEN headline.
        if "Distance to green: GREEN" in out:
            assert "GREEN (calibrated)" in headline


def test_a_distance_is_not_green_when_there_is_nothing_to_measure(gate_mod, monkeypatch):
    """"No populated bin is overconfident" is trivial with no populated bin."""
    r = _report_v(gate_mod, monkeypatch, tactical={}, is_calibrated=True)
    d = r["distance_to_green"]
    assert d["green"] is False
    assert d["blocked_by"] == "unassessable"


def test_a_red_with_no_tactical_blocker_reports_no_tactical_distance(gate_mod, monkeypatch):
    r = _report_v(gate_mod, monkeypatch, tactical={}, is_calibrated=False,
                  issues=["strategic danger"])
    d = r["distance_to_green"]
    assert d["green"] is False
    assert d["blocked_by"] == "non_tactical"


# --- the watch's own numbers must not contradict its sentences -------------

def test_gate_headroom_grows_with_population(gate_mod, monkeypatch):
    """The printed number used to run backwards.

    `min_samples + NEAR_FLOOR_MARGIN - count` is the distance to leaving the
    WATCH WINDOW, and it shrinks as a bin gets better populated — so under
    "the green may be an artifact" the thinnest bin got the most comfortable
    number. At min_samples=10 it printed "1 more samples" for count=14 and
    "5" for count=10. Gate headroom is count - min_samples.
    """
    seen = {}
    for count in (10, 12, 14):
        r = _report(gate_mod, monkeypatch,
                    tactical={"0.6-0.7": _bin(count, expected=0.65, actual=0.20, lo=0.6)})
        why = r["cheap_green_watch"][0]["why"]
        seen[count] = why
        assert "more samples from dropping out" not in why

    assert "0 sample(s) of headroom" in seen[10]
    assert "2 sample(s) of headroom" in seen[12]
    assert "4 sample(s) of headroom" in seen[14]


def test_an_unpopulated_bin_is_not_given_headroom(gate_mod, monkeypatch):
    """Below the floor there is no headroom to report — it is not counted."""
    r = _report(gate_mod, monkeypatch,
                tactical={"0.6-0.7": _bin(count=4, expected=0.65, actual=0.20, lo=0.6)})
    why = r["cheap_green_watch"][0]["why"]
    assert "headroom" not in why
    assert "NOT counted by the gate" in why


def test_the_noise_tail_reproduces_a_known_well_calibrated_flag_rate(gate_mod):
    """A bin at its declared rate still trips the gate, often, when tiny.

    declared 0.75 with n=2 and one miss: a perfectly-calibrated bin looks this
    bad 43.8% of the time. The watch window has no lower bound on count, so
    such a bin enters it — hence the headline is conditional and this number
    is printed beside the bin.
    """
    assert gate_mod.calibrated_gap_tail(2, 0.75, 0.5) == pytest.approx(0.4375, abs=1e-4)
    assert gate_mod.calibrated_gap_tail(3, 0.60, 2 / 3) == pytest.approx(0.784, abs=1e-3)


def test_the_noise_tail_separates_a_real_miscalibration(gate_mod):
    """It must not flatten everything to "could be noise"."""
    well_calibrated = gate_mod.calibrated_gap_tail(200, 0.65, 0.64)
    genuinely_bad = gate_mod.calibrated_gap_tail(200, 0.65, 0.20)
    assert well_calibrated > 0.3
    assert genuinely_bad < 1e-20


def test_the_noise_tail_names_its_one_p_approximation(gate_mod):
    """Bins are unequal-width; the tail uses the bin's MEAN confidence.

    The exact statistic is a Poisson-binomial over per-prediction confidences,
    which `record_tactical_decision` does not retain — it accumulates
    `confidence_sum` and discards the individuals. So no exact form is
    computable, and the approximation is not neutral: at n=12 with six
    predictions at 0.05 and six at 0.45, all wrong, the mean-p tail gives 3.17%
    against an exact 2.03%. 1.56x too large, biased toward "this could be
    chance" — the direction that makes a real miscalibration look benign.
    """
    exact = (1 - 0.05) ** 6 * (1 - 0.45) ** 6
    approx = gate_mod.calibrated_gap_tail(12, (6 * 0.05 + 6 * 0.45) / 12, 0.0)

    assert approx == pytest.approx(0.0317, abs=5e-4)
    assert exact == pytest.approx(0.0203, abs=5e-4)
    assert approx > exact                       # biased high, not merely different

    doc = gate_mod.calibrated_gap_tail.__doc__
    assert "APPROXIMATION" in doc.upper()
    assert "Poisson-binomial" in doc
    assert "biased toward" in doc


def test_the_printed_tail_flags_itself_as_approximate(gate_mod, monkeypatch, capsys):
    """A reader of the output, not the docstring, must see the caveat too."""
    r = _report(gate_mod, monkeypatch,
                tactical={"0.7-0.8": _bin(count=2, expected=0.75, actual=0.5, lo=0.7)})
    gate_mod.print_report(r)
    out = capsys.readouterr().out
    assert "one-p approximation, biased high" in out


def test_the_noise_tail_is_reported_not_thresholded(gate_mod, monkeypatch, capsys):
    """No significance level is chosen here — that would be the operator's."""
    r = _report(gate_mod, monkeypatch,
                tactical={"0.7-0.8": _bin(count=2, expected=0.75, actual=0.5, lo=0.7)})
    gate_mod.print_report(r)
    out = capsys.readouterr().out

    assert "43.8% of the time" in out
    # The bin is still listed — the tail annotates it, it does not filter it.
    assert "bin 0.7-0.8" in out
    # And the headline no longer asserts the gap is a property of the bin.
    assert "are overconfident enough to gate" not in out


def test_the_noise_tail_is_none_for_a_degenerate_bin(gate_mod):
    assert gate_mod.calibrated_gap_tail(0, 0.65, 0.0) is None


# --- 1. the label must come from what answered -----------------------------

def test_load_state_async_reports_whether_db_state_applied():
    """The signal the label now depends on, driven on the real method.

    Both silent fallbacks are covered: a falsy result, and a result whose
    `bins` are empty. Neither raises, which is why the caller could not detect
    them from an exception.
    """
    import asyncio

    from src.calibration import CalibrationChecker

    checker = CalibrationChecker()
    checker._backend = "postgres"
    applied = []

    class _DB:
        def __init__(self, result):
            self._result = result

        async def get_calibration(self):
            return self._result

    def _run(result):
        import src.db
        real = src.db.get_db
        src.db.get_db = lambda: _DB(result)
        try:
            checker.load_state = lambda: applied.append("json")
            return asyncio.run(checker.load_state_async())
        finally:
            src.db.get_db = real

    assert _run(None) is False                          # falsy result
    assert _run({"bins": {}}) is False                  # present but empty bins
    assert _run({"bins": {"0.6-0.7": {"count": 5}}}) is True
    assert applied == ["json", "json"]                  # fell back exactly twice


def test_a_json_backend_never_claims_postgres(gate_mod, monkeypatch):
    """`_backend` is config. It must not be what the label reads.

    BEHAVIOURAL, because the grep version did not hold. It pinned the exact
    spelling `getattr(checker, "_backend", "")` — evaded by writing
    `checker._backend` — and its positive assertion was satisfied by the target
    line surviving inside a COMMENT. Reinstating the original defect that way
    left all 18 tests passing while `_load_checker` returned
    "postgres(canonical)" for a database that had not answered.

    It also broke the house rule this repo states plainly: a test that scans
    source must strip comments first. The sibling test in #1862 does; this one
    did not. Driving the function is better than scanning it either way.
    """
    class _Stub:
        state_file = "/nonexistent"

        def __init__(self, applied):
            self._applied = applied
            self._backend = "postgres"       # CONFIG says postgres...

        async def load_state_async(self):
            return self._applied             # ...but this is what ANSWERED

    for applied, expected in ((True, "postgres(canonical)"), (False, "json_snapshot")):
        monkeypatch.setattr(gate_mod, "CalibrationChecker", lambda a=applied: _Stub(a))
        _, source = gate_mod._load_checker()
        assert source == expected, (applied, source)


def test_the_reporter_exits_zero_on_every_verdict(gate_mod, monkeypatch, capsys):
    """Pins what the script ACTUALLY does, so no comment can misdescribe it.

    A comment claimed "UNASSESSED follows the exit 0/1/2 convention from #1850".
    It does not: main() returns None and every verdict exits 0. Asserting
    behaviour the code lacks is the defect #1850 was about, committed while
    citing #1850.

    This does not argue the exit codes SHOULD differ — making RED non-zero
    turns a reporter into a gate, and that is an operator decision. It pins
    that they currently do not, so the next contract sentence has to be made
    true before it can be written.
    """
    import inspect

    assert inspect.signature(gate_mod.main).return_annotation in (None, "None", type(None))

    for tactical, ok, issues in (
        ({}, True, []),                                          # UNASSESSED
        ({}, False, ["strategic danger"]),                       # RED
        ({"0.6-0.7": _bin(50, 0.65, 0.64, 0.6)}, True, []),      # GREEN
    ):
        _report_v(gate_mod, monkeypatch, tactical, ok, issues)
        monkeypatch.setattr("sys.argv", ["calibration_gate_status.py"])
        assert gate_mod.main() is None
        capsys.readouterr()

    # Comments stripped: the correction note names the false claim on purpose.
    live = "\n".join(
        ln for ln in (REPO / "scripts/dev/calibration_gate_status.py").read_text().splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert "exit 0/1/2 convention" not in live


