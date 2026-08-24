#!/usr/bin/env python3
"""Calibration gate read: is the `calibrated` flag green, how close, and is it *real*?

The gate (src/calibration.py::check_calibration, council-reviewed 2026-06-19)
flips to green only when no populated TACTICAL bin is overconfident by more than
`_OVERCONFIDENCE_GATE` (declared confidence minus real success rate), and no
danger-direction bin trips. Strategic-proxy error and underconfidence are
advisory, not gating.

This script reuses the real CalibrationChecker so its verdict equals the
running server's gate. It answers three operator questions in one read:

  1. Is it green? (the authoritative is_calibrated + issues)
  2. How close? (per-bin overconfidence gap; distance from the worst bin to the
     0.20 line)
  3. Is a green REAL? (bins near the min-samples floor — a flag that flips
     green because the blocking bin simply depopulated is a measurement
     artifact, not calibration improvement)

Usage:
    python3 scripts/dev/calibration_gate_status.py [--min-samples 10] [--json]

Source: attempts the canonical Postgres load, falls back to the JSON
write-through snapshot. The source + its freshness are printed so a stale read
is never mistaken for a live one.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path

# Repo root on the path so `import src.calibration` works when run directly.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.calibration import (  # noqa: E402
    CalibrationChecker,
    _OVERCONFIDENCE_GATE,
)

# How close to the min-samples floor counts as "near" it. Applies in both
# directions but is not symmetric about the floor: the test is
# `count < min_samples + NEAR_FLOOR_MARGIN`, so at min_samples=10 the window is
# counts 0..14 -- every bin below the floor, plus the four just above it.
# count == 15 is outside. The value is the operator's; it decides no verdict and
# only widens an advisory list.
NEAR_FLOOR_MARGIN = 5


def calibrated_gap_tail(count: int, declared: float, accuracy: float) -> float | None:
    """P(a perfectly-calibrated bin of this size shows a gap this large).

    One-sided binomial tail: if the bin's true accuracy equalled its declared
    confidence, how often would it come out this bad or worse by chance alone?

    This is reported, never thresholded. At the small end of the watch window a
    gap is mostly the draw: a well-calibrated bin at declared 0.75 with n=2
    trips the overconfidence gate 43.8% of the time. Printing the tail says so
    without anyone choosing a significance level -- which would be a deciding
    standard, and the operator's.
    """
    if count < 1 or not (0.0 <= declared <= 1.0):
        return None
    k = max(0, min(count, int(round(accuracy * count))))
    return sum(math.comb(count, i) * declared ** i * (1 - declared) ** (count - i)
               for i in range(k + 1))


def _load_checker() -> tuple[CalibrationChecker, str]:
    """Construct a checker and load the freshest state available.

    __init__ already does a sync JSON load; we then try the canonical async DB
    load on top. Returns (checker, source_label).
    """
    checker = CalibrationChecker()
    source = "json_snapshot"
    try:
        # The label must come from what ANSWERED, not from what was requested.
        # This read `checker._backend`, which is a config value from
        # UNITARES_CALIBRATION_BACKEND set once at __init__ and never mutated --
        # so it printed "postgres(canonical) live DB" whenever postgres was
        # merely *configured*, including when the connection had just failed.
        # Reproduced on a host with no database: the connection error printed to
        # stderr, and the report still read "live DB" over a snapshot that was
        # by then well over an hour old. `load_state_async` cannot surface this
        # through an exception either -- it catches its own, and falls through
        # SILENTLY when the row is missing or its `bins` are empty -- so it now
        # reports whether canonical state was applied.
        if asyncio.run(checker.load_state_async()):
            source = "postgres(canonical)"
    except Exception as exc:  # pragma: no cover - diagnostic resilience
        print(f"# async DB load failed ({exc}); using sync snapshot", file=sys.stderr)
    return checker, source


def _snapshot_age_seconds(checker: CalibrationChecker) -> float | None:
    try:
        return max(0.0, time.time() - Path(checker.state_file).stat().st_mtime)
    except Exception:
        return None


def build_report(min_samples: int) -> dict:
    checker, source = _load_checker()
    is_calibrated, metrics = checker.check_calibration(min_samples_per_bin=min_samples)
    tactical = checker.compute_tactical_metrics()

    bins = []
    worst_blocker = None  # (bin_key, gap)
    near_floor = []
    for bin_key, m in sorted(tactical.items()):
        gap = m.expected_accuracy - m.accuracy  # >0 == overconfident
        populated = m.count >= min_samples
        gates = populated and m.bin_range[0] < 0.8 and gap > _OVERCONFIDENCE_GATE
        danger = populated and m.bin_range[0] >= 0.8 and m.accuracy < 0.7
        bins.append({
            "bin": bin_key,
            "count": m.count,
            "declared": round(m.expected_accuracy, 3),
            "actual": round(m.accuracy, 3),
            "overconfidence_gap": round(gap, 3),
            "populated": populated,
            "gates": bool(gates or danger),
        })
        if populated and gap > _OVERCONFIDENCE_GATE:
            if worst_blocker is None or gap > worst_blocker[1]:
                worst_blocker = (bin_key, gap)
        # "Near floor" = a bin whose overconfidence WOULD gate, sitting close
        # enough to the evaluation threshold that its sample count decides
        # whether the gate sees it at all.
        #
        # This used to require `gates`, which is False for every bin whenever
        # the flag is green -- so on a GREEN gate the watch was always empty and
        # always printed "no blocking bin is near the sample floor". The check
        # the docstring promises ("is a green REAL?") could only fire while the
        # gate was RED, i.e. in the one state where nobody needs it. The exact
        # artifact it exists to catch -- a bin that dropped BELOW min_samples,
        # taking its overconfidence out of the gate's view and turning the flag
        # green -- was invisible to it, because such a bin is unpopulated and so
        # `gates` is False.
        #
        # Overconfidence is now what qualifies a bin, and population is what is
        # reported about it, so the watch is live in both gate states.
        would_gate = gap > _OVERCONFIDENCE_GATE or (m.bin_range[0] >= 0.8 and m.accuracy < 0.7)
        if would_gate and m.count < min_samples + NEAR_FLOOR_MARGIN:
            near_floor.append({
                "bin": bin_key,
                "count": m.count,
                "populated": populated,
                # `min_samples + NEAR_FLOOR_MARGIN - m.count` used to be printed
                # as "N more samples from dropping out of [the gate]". It is
                # neither: it is the distance to leaving this WATCH WINDOW, and
                # it DECREASES as a bin gets better populated -- so under
                # "the green may be an artifact" the thinnest bin was given the
                # most comfortable-looking number. At min_samples=10: count=14
                # printed "1 more samples", count=10 printed "5". Gate headroom
                # is count - min_samples, and only a populated bin has any.
                "why": ("counted by the gate, with {} sample(s) of headroom above "
                        "the {}-sample floor".format(m.count - min_samples, min_samples))
                if populated else
                ("BELOW the {}-sample floor, so its overconfidence is NOT counted "
                 "by the gate".format(min_samples)),
                "calibrated_gap_tail": calibrated_gap_tail(
                    m.count, m.expected_accuracy, m.accuracy),
            })

    # A gate that cannot fail has not passed. The gate is TACTICAL: it asks
    # whether any populated tactical bin is overconfident. With no populated
    # tactical bin there is no such bin, so `is_calibrated` comes back True on
    # an empty table -- vacuously, not because anything was checked.
    #
    # `check_calibration`'s own no-data guard does not catch this: it requires
    # BOTH strategic and tactical to be empty, so a single unrelated strategic
    # bin satisfies it. Reproduced on this deployment: 1 strategic bin (count=1),
    # 0 tactical bins, VERDICT GREEN -- printed beside the state's own
    # "No tactical data yet" note.
    #
    # This is reported, NOT decided here. `check_calibration` is the live gate
    # (council-reviewed 2026-06-19) and changing its return is the operator's
    # call, so this script keeps the flag it was given and says what the flag
    # rests on. NOTE this script is a REPORTER, not a gate: main() returns None
    # and every verdict exits 0. An earlier version of this comment claimed
    # "UNASSESSED follows the exit 0/1/2 convention from #1850" — it does not,
    # and asserting behaviour the code does not have is the defect #1850 itself
    # was about. Making RED non-zero would turn this reporter into a gate, which
    # is an operator decision and not one taken here.
    populated_tactical = sum(1 for b in bins if b["populated"])
    assessable = populated_tactical > 0

    if worst_blocker is None and not is_calibrated:
        # A tactical distance of "GREEN" is true only about the TACTICAL arm.
        # Printed unqualified beside a RED flag it read as a third, contradictory
        # verdict on the same page. The flag can be False for a strategic or
        # danger-direction reason that has no tactical blocker to measure a
        # distance to.
        distance = {"green": False,
                    "note": "no populated TACTICAL bin is overconfident, but the gate "
                            "flag is False — see the issues above. There is no "
                            "tactical distance to report.",
                    "blocked_by": "non_tactical"}
    elif worst_blocker is None and not assessable:
        # "No populated bin is overconfident" is trivially true when there is no
        # populated bin. A distance measured against an empty set is not a
        # distance, and printing GREEN for it recreates the vacuous pass one
        # line lower down.
        distance = {"green": False,
                    "note": "no populated TACTICAL bin exists, so there is no "
                            "distance to measure. Not a green.",
                    "blocked_by": "unassessable"}
    elif worst_blocker is None:
        distance = {"green": True, "note": "no populated bin overconfident by > "
                    f"{_OVERCONFIDENCE_GATE:.2f}"}
    else:
        distance = {
            "green": False,
            "blocking_bin": worst_blocker[0],
            "worst_gap": round(worst_blocker[1], 3),
            "gate": _OVERCONFIDENCE_GATE,
            "close_by": round(worst_blocker[1] - _OVERCONFIDENCE_GATE, 3),
        }

    return {
        "source": source,
        "snapshot_age_seconds": _snapshot_age_seconds(checker),
        "min_samples_per_bin": min_samples,
        "overconfidence_gate": _OVERCONFIDENCE_GATE,
        "calibrated": is_calibrated,
        "assessable": assessable,
        "populated_tactical_bins": populated_tactical,
        "total_tactical_bins": len(bins),
        "issues": metrics.get("issues", []),
        "advisories": metrics.get("advisories", []),
        "tactical_bins": bins,
        "distance_to_green": distance,
        "cheap_green_watch": near_floor,
    }


def print_report(r: dict) -> None:
    age = r["snapshot_age_seconds"]
    # Age is printed even on the live-DB path. It used to be suppressed whenever
    # the source claimed postgres, which removed the one field that would have
    # exposed a stale read at exactly the moment the label was wrong.
    if isinstance(age, (int, float)):
        stamp = f"snapshot {age:.0f}s old"
    else:
        stamp = "snapshot age unknown"
    freshness = f"live DB ({stamp})" if r["source"].startswith("postgres") else stamp
    print("UNITARES calibration gate — overconfidence read")
    print(f"  source: {r['source']}   {freshness}   "
          f"min_samples/bin={r['min_samples_per_bin']}   "
          f"gate=±{r['overconfidence_gate']:.2f}")
    print()
    # TWO AXES, never collapsed into one headline.
    #
    # Did the gate FAIL, and was the tactical arm ASSESSABLE, are independent
    # questions. An earlier version let non-assessability win the headline
    # outright, so a run with is_calibrated=False and zero tactical bins printed
    # "UNASSESSED" over the top of its own "these keep it RED" list. That is a
    # worse failure than the vacuous GREEN it replaced: a vacuous green
    # overstates health on a state nothing checked, while a vacuous UNASSESSED
    # SUPPRESSES a finding that was checked and failed.
    #
    # A failure is always reported as a failure. Non-assessability qualifies the
    # tactical arm underneath it, never the verdict line.
    if not r["calibrated"]:
        verdict = "RED (miscalibrated)"
    elif not r["assessable"]:
        verdict = "UNASSESSED — no populated tactical bin"
    else:
        verdict = "GREEN (calibrated)"
    print(f"VERDICT: {verdict}")

    if not r["assessable"]:
        print(f"  Tactical arm UNASSESSED: {r['populated_tactical_bins']} of "
              f"{r['total_tactical_bins']} tactical bins clear the "
              f"{r['min_samples_per_bin']}-sample floor,")
        print("  so no bin could be overconfident and the tactical gate checked nothing.")
        if r["calibrated"]:
            print("  The flag reads True for that reason alone. This is not a pass.")
        else:
            print("  The RED above therefore rests on the issues listed below, NOT on "
                  "the tactical gate.")
    print()

    if r["issues"]:
        print("Gating issues (these keep it RED):")
        for i in r["issues"]:
            print(f"  - {i}")
        print()
    if r["advisories"]:
        print("Advisories (non-gating — real but not safety-relevant):")
        for a in r["advisories"]:
            print(f"  - {a}")
        print()

    print("Tactical bins (the gate input):")
    print(f"  {'bin':<10} {'n':>7}  {'declared':>8} {'actual':>7} "
          f"{'gap(d-a)':>9}  gates?")
    for b in r["tactical_bins"]:
        marker = "  <-- blocker" if b["gates"] and b["overconfidence_gap"] > 0 else ""
        gate_str = "YES" if b["gates"] else ("--" if b["populated"] else "n<min")
        print(f"  {b['bin']:<10} {b['count']:>7}  {b['declared']:>8.3f} "
              f"{b['actual']:>7.3f} {b['overconfidence_gap']:>+9.3f}  "
              f"{gate_str}{marker}")
    print()

    d = r["distance_to_green"]
    if d.get("green"):
        print(f"Distance to green: GREEN — {d['note']}.")
    elif d.get("blocked_by") in ("non_tactical", "unassessable"):
        # This line reports a distance along the TACTICAL axis only. Printed as
        # an unqualified "GREEN" beside a RED flag it read as a third verdict
        # contradicting the other two.
        print(f"Distance to green: NOT GREEN — {d['note']}")
    else:
        print(f"Distance to green: blocking bin {d['blocking_bin']} is "
              f"overconfident by {d['worst_gap']:.3f}; needs to drop below "
              f"{d['gate']:.2f} — close the gap by {d['close_by']:.3f}.")
    print()

    if r["cheap_green_watch"]:
        if r["calibrated"]:
            # Stated conditionally. The window has no lower bound on count, so a
            # count=1 bin enters it -- and at that size an overconfidence gap is
            # mostly the draw, not a property of the bin. The old headline said
            # these bins ARE overconfident enough to gate; the per-bin tail below
            # says how often a perfectly-calibrated bin of that size would look
            # this way anyway.
            print("Cheap-green watch — THE GREEN ABOVE MAY BE AN ARTIFACT. Each bin")
            print("below shows a gap that would gate, and its sample count is what")
            print("decides whether the gate sees it at all:")
        else:
            print("Cheap-green watch (a green that appears because one of these")
            print("bins depopulates would be a measurement artifact, not calibration):")
        for n in r["cheap_green_watch"]:
            tail = n.get("calibrated_gap_tail")
            noise = ""
            if isinstance(tail, (int, float)):
                noise = (f"; a perfectly-calibrated bin this size shows a gap this "
                         f"large {tail:.1%} of the time")
            print(f"  - bin {n['bin']}: {n['count']} samples — {n['why']}{noise}")
    else:
        print(f"Cheap-green watch: no overconfident bin is within "
              f"{NEAR_FLOOR_MARGIN} samples of the "
              f"{r['min_samples_per_bin']}-sample floor.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-samples", type=int, default=10,
                    help="Min samples per bin to evaluate (gate default: 10).")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = ap.parse_args()

    report = build_report(args.min_samples)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
