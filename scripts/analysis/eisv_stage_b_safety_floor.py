#!/usr/bin/env python3
"""Stage B safety-floor — would a per-agent RESIDUAL statistic shift healthy
agents' verdicts? (Non-regression; computable now, no outcome labels needed.)

B proposes judging an agent on its residual from its OWN baseline rather than on
absolute Φ. Before any of that ships, the safety floor (roadmap §6.1-6.2): does
the residual flag agents who are currently healthy? If currently-safe check-ins
carry low residuals, the residual is non-regressive; if many carry high
residuals, swapping to it would pause healthy agents.

residual = Σ over {E,I,S,V} |current_axis − baseline_mean| / baseline_std
           (per-agent z-distance from its own Welford baseline)

This needs no exogenous labels (that's B's *justification*, blocked on the
anchor bridge). It only asks: is the residual *consistent with current health*?

Caveat: "healthy" is proxied by the current (Φ-based) verdict — the very signal
we're moving away from — so agreement is necessary, not sufficient. A residual
that flags a currently-"safe" agent is either a regression OR a case where Φ was
too lax; this probe can't tell which without outcomes. It bounds the regression
risk, it doesn't bless the residual.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys


def welford_std(d):
    c = d.get("count", 0)
    if not c or c < 2:
        return None
    v = d.get("m2", 0.0) / c
    return math.sqrt(v) if v > 0 else None


def residual(beisv):
    bs = beisv.get("baseline_stats") or {}
    z = 0.0
    for ax in ("E", "I", "S", "V"):
        d = bs.get(ax)
        cur = beisv.get(ax)
        if not d or cur is None:
            return None
        std = welford_std(d)
        if not std:
            return None
        z += abs(float(cur) - d.get("mean", 0.0)) / std
    return z


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    f = math.floor(k)
    return xs[f] if f == len(xs) - 1 else xs[f] + (xs[f + 1] - xs[f]) * (k - f)


# The threshold is chosen on one half of the safe check-ins and the
# false-positive rate is measured on the OTHER half. Splitting is the whole
# point -- see `separation_report`.
MIN_SAFE_FOR_SPLIT = 100


def split_sample(xs, seed=0):
    """Deterministic random halves: (calibration, holdout)."""
    shuffled = list(xs)
    random.Random(seed).shuffle(shuffled)
    mid = len(shuffled) // 2
    return shuffled[:mid], shuffled[mid:]


def separation_report(safe, non_safe, seed=0, quantile=0.99, threshold=None):
    """Does the residual flag currently-healthy agents?

    THE DEFECT. The regression bound used to be computed like this:

        thr = pct(safe, 0.99)
        fp  = sum(1 for z in safe if z > thr) / len(safe)

    which is the fraction of a sample lying above its own 99th percentile --
    ~1% for ANY sample. It is the definition of a percentile, not a property of
    the residual. Across normal, heavy-tailed and bimodal draws at n=200 and
    n=2000 it returns exactly 1.000% every time. So the printed line
    "false-positive rate among currently-safe: 1.0% (want low)" was a pass the
    instrument could not withhold, and the verdict `tp > 3 * max(fp, 0.01)`
    collapsed to the constant `tp > 0.03`.

    THE POINT THAT SURVIVES THE OBVIOUS FIX. Splitting the safe set into
    calibration and holdout halves stops `fp` being an exact identity, but does
    not make it informative: the halves are exchangeable by construction, so the
    held-out rate is a sampling estimate centred on `1 - quantile`. It measures
    threshold-estimation noise, not whether the residual is safe. No amount of
    resampling repairs this, because a threshold DEFINED as a quantile of the
    safe distribution fixes its own false-positive rate on that distribution.
    The regression bound is simply not identified from a quantile threshold.

    So the two modes are reported as the different things they are:

      * `threshold=None` -- quantile mode. `fp` is stated as definitional, with
        the held-out estimate shown as a noise check on the quantile, explicitly
        NOT as a regression bound. The informative outputs are the threshold's
        absolute value (which a policy would have to adopt) and the separation.
      * `threshold=<float>` -- policy mode. An externally chosen threshold makes
        `fp` a genuine measurement on the safe set, free to come out anywhere.
        This is the only mode that bounds regression risk.
    """
    out = []
    if not safe:
        out.append("\nSKIPPED: no safe-verdict check-ins. Nothing was measured.")
        return out

    if threshold is not None:
        thr = float(threshold)
        fp = sum(1 for z in safe if z > thr) / len(safe)
        out.append(f"\nThreshold = {thr:.2f} (supplied, not derived from this sample)")
        out.append(f"  false-positive rate among currently-safe (n={len(safe)}): {fp:.1%}")
        out.append("  This IS a regression bound: the threshold did not come from the "
                   "safe distribution, so the rate is free to come out anywhere.")
        measured_fp = fp
    else:
        thr = pct(safe, quantile)
        definitional = 1.0 - quantile
        out.append(f"\nThreshold = p{quantile * 100:g} of the safe check-ins "
                   f"(n={len(safe)}) = {thr:.2f}")
        out.append(f"  false-positive rate among currently-safe: {definitional:.1%} "
                   "BY CONSTRUCTION, not measured.")
        out.append("  A threshold defined as a quantile of the safe distribution fixes "
                   "its own false-positive\n  rate on that distribution. This mode "
                   "CANNOT bound regression risk; pass --threshold with a\n  "
                   "policy-registered absolute value to get a bound.")
        if len(safe) < MIN_SAFE_FOR_SPLIT:
            out.append(f"  THIN SAMPLE: {len(safe)} safe check-ins (want "
                       f"{MIN_SAFE_FOR_SPLIT}). At p{quantile * 100:g} the threshold is "
                       "set by the top one or two\n  values, so it is noise. Read "
                       "nothing from the separation below.")
        else:
            calib, holdout = split_sample(safe, seed=seed)
            held = sum(1 for z in holdout if z > pct(calib, quantile)) / len(holdout)
            out.append(f"  (held-out estimate on a disjoint half: {held:.1%} — a noise "
                       f"check on the quantile itself,\n   centred on "
                       f"{definitional:.1%} because the halves are exchangeable. Still "
                       "not a regression bound.)")
        measured_fp = None

    if not non_safe:
        out.append("  no non-safe check-ins in window — separation NOT MEASURED "
                   "(this is not a pass)")
        return out

    tp = sum(1 for z in non_safe if z > thr) / len(non_safe)
    out.append(f"  flag rate among currently non-safe (n={len(non_safe)}): {tp:.1%}")

    # Compare against a rate that was actually measured. The old form floored fp
    # at 0.01 -- the same constant the identity produced -- so the comparison
    # never saw a real false-positive rate at all.
    ref = measured_fp if measured_fp is not None else (1.0 - quantile)
    ref_kind = "measured" if measured_fp is not None else "definitional"
    if ref <= 0.0:
        out.append(f"  → separation ratio UNDEFINED (false-positive rate is 0); raw "
                   f"rates: {tp:.1%} flagged vs 0 false positives.")
    elif tp > 3 * ref:
        out.append(f"  → SEPARATES at this threshold: {tp / ref:.1f}x the {ref_kind} "
                   f"false-positive rate ({ref:.1%}). Says nothing about whether the "
                   "residual predicts outcomes.")
    else:
        out.append(f"  → WEAK: {tp / ref:.1f}x the {ref_kind} false-positive rate "
                   f"({ref:.1%}); want >3x.")
    return out



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url", default="postgresql://postgres:postgres@localhost:5432/governance")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--seed", type=int, default=0,
                    help="split seed for the quantile noise check (fixed so it "
                         "cannot be shopped silently)")
    ap.add_argument("--threshold", type=float, default=None,
                    help="policy-registered absolute residual threshold. Only this "
                         "mode yields a regression bound; without it the "
                         "false-positive rate is definitional.")
    args = ap.parse_args()

    # Imported here, not at module scope: the statistics above are pure and must
    # stay importable (and testable) on a machine with no database driver, and
    # --help must work without one.
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(args.db_url)

    by_verdict = {}  # verdict -> [residual,...]
    n_rows = 0
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT state_json->'behavioral_eisv' AS beisv, state_json->>'verdict' AS verdict
            FROM core.agent_state
            WHERE recorded_at > now() - (%s::int * interval '1 day')
              AND state_json ? 'behavioral_eisv'
              AND state_json->'behavioral_eisv'->'warmup'->>'is_baselined' = 'true'
              AND state_json ? 'verdict'
            """,
            (args.days,),
        )
        for r in cur:
            beisv = r["beisv"]
            if isinstance(beisv, str):
                beisv = json.loads(beisv)
            z = residual(beisv)
            if z is None:
                continue
            n_rows += 1
            by_verdict.setdefault(r["verdict"], []).append(z)
    conn.close()

    print(f"baselined check-ins with computable residual (last {args.days}d): N={n_rows}\n")
    print(f"{'verdict':<12}{'n':>7}{'resid p50':>11}{'p90':>9}{'p99':>9}")
    print("-" * 48)
    safe = by_verdict.get("safe", [])
    for v in sorted(by_verdict, key=lambda k: -len(by_verdict[k])):
        xs = by_verdict[v]
        print(f"{v:<12}{len(xs):>7}{pct(xs,0.5):>11.2f}{pct(xs,0.9):>9.2f}{pct(xs,0.99):>9.2f}")

    non_safe = [z for v, xs in by_verdict.items() if v != "safe" for z in xs]
    for line in separation_report(safe, non_safe, seed=args.seed, threshold=args.threshold):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
