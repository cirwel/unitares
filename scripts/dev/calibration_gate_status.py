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

# How close to the min-samples floor counts as "near" it, in either direction.
NEAR_FLOOR_MARGIN = 5


def _load_checker() -> tuple[CalibrationChecker, str]:
    """Construct a checker and load the freshest state available.

    __init__ already does a sync JSON load; we then try the canonical async DB
    load on top. Returns (checker, source_label).
    """
    checker = CalibrationChecker()
    source = "json_snapshot"
    try:
        asyncio.run(checker.load_state_async())
        if getattr(checker, "_backend", "") == "postgres":
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
                "why": ("counted by the gate, but {} more samples from dropping out "
                        "of it".format(min_samples + NEAR_FLOOR_MARGIN - m.count))
                if populated else
                ("BELOW the {}-sample floor, so its overconfidence is NOT counted "
                 "by the gate".format(min_samples)),
            })

    if worst_blocker is None:
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
        "issues": metrics.get("issues", []),
        "advisories": metrics.get("advisories", []),
        "tactical_bins": bins,
        "distance_to_green": distance,
        "cheap_green_watch": near_floor,
    }


def print_report(r: dict) -> None:
    age = r["snapshot_age_seconds"]
    if r["source"].startswith("postgres"):
        freshness = "live DB"
    elif isinstance(age, (int, float)):
        freshness = f"snapshot {age:.0f}s old"
    else:
        freshness = "snapshot age unknown"
    print("UNITARES calibration gate — overconfidence read")
    print(f"  source: {r['source']}   {freshness}   "
          f"min_samples/bin={r['min_samples_per_bin']}   "
          f"gate=±{r['overconfidence_gate']:.2f}")
    print()
    verdict = "GREEN (calibrated)" if r["calibrated"] else "RED (miscalibrated)"
    print(f"VERDICT: {verdict}")
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
    else:
        print(f"Distance to green: blocking bin {d['blocking_bin']} is "
              f"overconfident by {d['worst_gap']:.3f}; needs to drop below "
              f"{d['gate']:.2f} — close the gap by {d['close_by']:.3f}.")
    print()

    if r["cheap_green_watch"]:
        if r["calibrated"]:
            print("Cheap-green watch — THE GREEN ABOVE MAY BE AN ARTIFACT. These bins")
            print("are overconfident enough to gate, and their sample counts are what")
            print("decides whether the gate sees them:")
        else:
            print("Cheap-green watch (a green that appears because one of these")
            print("bins depopulates would be a measurement artifact, not calibration):")
        for n in r["cheap_green_watch"]:
            print(f"  - bin {n['bin']}: {n['count']} samples — {n['why']}")
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
