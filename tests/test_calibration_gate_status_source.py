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


def test_a_json_backend_never_claims_postgres():
    """`_backend` is config. It must not be what the label reads."""
    source = (REPO / "scripts/dev/calibration_gate_status.py").read_text()
    assert 'getattr(checker, "_backend", "") == "postgres"' not in source
    assert "if asyncio.run(checker.load_state_async()):" in source
