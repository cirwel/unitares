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


# Below this many safe check-ins the p99 is set by the top one or two values, so
# the held-out noise check is not worth printing at all.
#
# This comment used to say "the threshold is chosen on one half ... splitting is
# the whole point", which is the opposite of what the module does: every PRINTED
# rate uses the full-sample `pct(safe, quantile)`, and the calibration half feeds
# only the parenthetical noise check. Left as it was, it invited a future editor
# to promote that half-sample threshold into the verdict on the strength of a
# comment.
MIN_SAFE_FOR_SPLIT = 100

# Split-conformal coverage needs enough calibration points for the quantile to
# exist at all: m >= 1/(1-q) - 1, i.e. 99 for q=0.99, so n >= 198.
CONFORMAL_MIN_CALIB = lambda q: math.ceil(1.0 / (1.0 - q) - 1.0)  # noqa: E731


def expected_holdout_rate(n_calib: int, quantile: float) -> float:
    """Split-conformal exceedance centre: (m - q(m-1)) / (m+1).

    NOT `1 - quantile`. An earlier version of the report asserted that centre
    "because the halves are exchangeable" -- exchangeability gives this formula,
    which only approaches 1-q for large m. At MIN_SAFE_FOR_SPLIT the calibration
    half is m=50 and the true centre is 2.9%, nearly 3x the claimed 1.0%; worse,
    the holdout is then 50 points, so the rate lives on a 2% grid and CANNOT
    take the value 1.0% under any draw. Simulation over 400 seeds: 2.77% at
    n=100, 1.91% at n=198, 1.49% at n=400, 1.11% at n=2000.
    """
    m = max(1, n_calib)
    return (m - quantile * (m - 1)) / (m + 1)


def clopper_pearson_upper(k: int, n: int, alpha: float = 0.05) -> float | None:
    """One-sided exact upper bound on a binomial rate. Stdlib bisection.

    A rate off a handful of trials is not the number it prints. 1-of-5 renders
    as "20.0%" while its exact one-sided 95% upper bound is about 65% -- so the
    interval, not the point, is what a reader needs before treating it as a
    regression bound. Reported, never used to suppress: choosing a minimum n
    would be a deciding standard, and the operator's.
    """
    if n < 1 or not (0 <= k <= n):
        return None
    if k == n:
        return 1.0

    def cdf(p):  # P(X <= k | n, p)
        return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))

    lo, hi = k / n, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if cdf(mid) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


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
        fp_k = sum(1 for z in safe if z > thr)
        fp = fp_k / len(safe)
        upper = clopper_pearson_upper(fp_k, len(safe))
        out.append(f"\nThreshold = {thr:.2f} (supplied, not derived from this sample)")
        # Counts, not just the rate. A rate off a handful of trials is not the
        # number it prints, and the rendered percentage hides the denominator
        # the reader needs to judge it.
        out.append(f"  false-positive rate among currently-safe: {fp:.1%} "
                   f"({fp_k}/{len(safe)}); one-sided 95% upper bound "
                   f"{upper:.1%}" if upper is not None else
                   f"  false-positive rate among currently-safe: {fp:.1%} "
                   f"({fp_k}/{len(safe)})")
        out.append("  This IS a regression bound: the threshold did not come from the "
                   "safe distribution, so the rate is free to come out anywhere.")
        out.append("  Read the upper bound, not the point estimate, before treating it "
                   "as one.")
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
            held_k = sum(1 for z in holdout if z > pct(calib, quantile))
            held = held_k / len(holdout)
            centre = expected_holdout_rate(len(calib), quantile)
            note = ""
            if len(calib) < CONFORMAL_MIN_CALIB(quantile):
                note = (f"\n   NB m={len(calib)} < {CONFORMAL_MIN_CALIB(quantile)}: no "
                        f"p{quantile * 100:g} threshold with that coverage exists at "
                        f"this calibration size,\n   and the holdout rate lives on a "
                        f"{1 / len(holdout):.1%} grid, so it cannot even take the "
                        "centre's value.")
            out.append(f"  (held-out estimate on a disjoint half: {held:.1%} "
                       f"({held_k}/{len(holdout)}) — a noise check on the quantile\n"
                       f"   itself, centred on {centre:.1%} for a calibration half of "
                       f"m={len(calib)}. Still not a regression bound.{note})")
        measured_fp = None

    if not non_safe:
        out.append("  no non-safe check-ins in window — separation NOT MEASURED "
                   "(this is not a pass)")
        return out

    tp_k = sum(1 for z in non_safe if z > thr)
    tp = tp_k / len(non_safe)
    out.append(f"  flag rate among currently non-safe: {tp:.1%} "
               f"({tp_k}/{len(non_safe)})")

    # The thin-sample branch carried the strongest disclaimer in this function
    # ("Read nothing from the separation below") and then fell through to print
    # SEPARATES anyway -- the only "cannot support a reading" state that still
    # emitted a verdict token. The empty-safe and no-non-safe paths both return
    # before this point; this one now matches them.
    if threshold is None and len(safe) < MIN_SAFE_FOR_SPLIT:
        out.append("  → separation NOT ASSESSED: the threshold is set by the top one "
                   f"or two of {len(safe)} values.")
        return out

    # Compare against a rate that was actually measured. The old form floored fp
    # at 0.01 -- the same constant the identity produced -- so the comparison
    # never saw a real false-positive rate at all.
    ref = measured_fp if measured_fp is not None else (1.0 - quantile)
    ref_kind = "measured" if measured_fp is not None else "definitional"
    # The multiple is computed on unrounded rates while the rates render at
    # {:.1%}, so a printed "0.1%" and "0.3%" could carry a "6.0x" that no number
    # on the page reproduces. The counts above now make the multiple checkable;
    # the ratio is also stated as what it is -- a ratio of two rates, one of
    # which may rest on a single observation.
    if ref <= 0.0:
        out.append(f"  → separation ratio UNDEFINED (false-positive rate is 0); raw "
                   f"rates: {tp:.1%} flagged vs 0 false positives.")
    elif tp > 3 * ref:
        out.append(f"  → SEPARATES at this threshold: {tp / ref:.1f}x the {ref_kind} "
                   f"false-positive rate (exactly {tp:.4%} / {ref:.4%}). Bounds nothing "
                   "about whether the residual predicts outcomes.")
    else:
        out.append(f"  → WEAK: {tp / ref:.1f}x the {ref_kind} false-positive rate "
                   f"(exactly {tp:.4%} / {ref:.4%}); want >3x.")
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
