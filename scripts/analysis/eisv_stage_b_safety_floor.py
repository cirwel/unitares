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

# The separation verdict's multiple. A DECIDING STANDARD, in force by default
# rather than deferred: it converts two rates into SEPARATES or WEAK. Named so
# the choice is visible and greppable -- but there is no --separation-multiple
# flag, so it is movable by EDIT ONLY. An earlier comment said "overridable",
# which overstates what naming a constant buys.
SEPARATION_MULTIPLE = 3.0

# Split-conformal coverage needs enough calibration points for the quantile to
# exist at all: m >= 1/(1-q) - 1, i.e. 99 for q=0.99, so n >= 198.
CONFORMAL_MIN_CALIB = lambda q: math.ceil(1.0 / (1.0 - q) - 1.0)  # noqa: E731


def conformal_rank(n_calib: int, quantile: float) -> int:
    """The order-statistic INDEX the conformal threshold must use: ceil((m+1)q).

    Capped at m, where the threshold degenerates to the calibration maximum.
    """
    return min(math.ceil((n_calib + 1) * quantile), max(1, n_calib))


def rank_conformal_threshold(xs, quantile: float):
    """The ceil((m+1)q)-th order statistic. NO interpolation.

    `pct` interpolates linearly between neighbouring order statistics, which is
    the right thing for a descriptive quantile and the WRONG thing here: the
    split-conformal guarantee is a statement about RANKS, so it survives any
    continuous distribution only if the threshold IS an order statistic.
    Interpolating places it at a position that depends on the local density, and
    coverage stops being distribution-free -- see `conformal_exceedance`.
    """
    ordered = sorted(xs)
    if not ordered:
        return None
    return ordered[conformal_rank(len(ordered), quantile) - 1]


def conformal_exceedance(n_calib: int, quantile: float) -> float:
    """EXACT distribution-free exceedance for the rank threshold: 1 - k/(m+1).

    This is a theorem about ranks, not a fit. For continuous scores the new
    point is equally likely to fall in any of the m+1 gaps between (and outside)
    the calibration points, so P(exceed the k-th) = 1 - k/(m+1) regardless of
    the distribution.

    THE HISTORY MATTERS, because the same quantity was got wrong twice by
    reasoning about what the estimator ought to do:

      #1856 said "centred on 1-q because the halves are exchangeable".
      #1862 said (m - q(m-1))/(m+1), the interpolated-quantile expectation.

    Both were confirmed against a self-authored gaussian simulation, and both
    times the simulation agreed BECAUSE IT SHARED THE ERROR -- gaussian is the
    case where interpolation sits closest to the rank threshold. Measured mean
    exceedance at m=50, 6000 seeds, against the second formula's flat 2.9216%:
    uniform 2.9303%, gaussian 2.6700%, Pareto a=1.1 2.4197%, Pareto a=0.5
    2.2983%. A 0.63pp spread, ~27% relative, and only the gaussian column looks
    like agreement.

    The rank form was checked the way the others should have been -- against a
    heavy-tailed AND a bounded distribution, not just a gaussian. At m=99 theory
    1.0000% vs gaussian 1.0230%, Pareto a=0.5 1.0301%, uniform 1.0301%; at m=200
    theory 0.9950% vs 1.0070 / 1.0088 / 1.0088.
    """
    m = max(1, n_calib)
    return 1.0 - conformal_rank(m, quantile) / (m + 1)


def clopper_pearson_upper(k: int, n: int, alpha: float = 0.05) -> float | None:
    """One-sided exact upper bound on a binomial rate. Stdlib bisection.

    A rate off a handful of trials is not the number it prints. 1-of-5 renders
    as "20.0%" while its exact one-sided 95% upper bound is about 65% -- so the
    interval, not the point, is what a reader needs before treating it as a
    regression bound. Reported, never used to suppress: choosing a minimum n
    would be a deciding standard, and the operator's.

    ASSUMES INDEPENDENT BERNOULLI TRIALS, WHICH THESE ROWS ARE NOT. The safe
    set is longitudinal per-agent check-ins, and this repo has measured how
    strongly they autocorrelate -- the previous-outcome baseline predicts the
    next outcome at AUC ~0.94. Under positive dependence the effective sample is
    smaller than n, so this interval is ANTI-CONSERVATIVE: the true bound is
    wider than the one printed. It is still worth printing, because it bounds
    the sampling error a reader would otherwise ignore entirely, but it is a
    floor on the uncertainty and not the uncertainty. Widening it correctly
    needs a cluster-aware estimate over agents, which no registered design
    specifies.
    """
    if n < 1 or not (0 <= k <= n):
        return None
    if k == n:
        return 1.0

    def cdf(p):  # P(X <= k | n, p)
        # Evaluated in log space. The direct form overflowed: math.comb(2000,
        # 1000) is a ~600-digit integer, and multiplying it by a float raises
        # OverflowError before any probability is computed. lgamma keeps every
        # term finite for any n this script can see.
        if p <= 0.0:
            return 1.0
        if p >= 1.0:
            return 1.0 if k >= n else 0.0
        log_p, log_q = math.log(p), math.log1p(-p)
        total = 0.0
        for i in range(k + 1):
            log_term = (math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                        + i * log_p + (n - i) * log_q)
            total += math.exp(log_term)
        return min(1.0, total)

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
    not make it informative: the held-out rate measures threshold-estimation
    noise, not whether the residual is safe. No amount of resampling repairs
    this, because a threshold DEFINED as a quantile of the safe distribution
    fixes its own false-positive rate on that distribution.

    (An earlier version of this paragraph added "the halves are exchangeable by
    construction, so the held-out rate is a sampling estimate centred on
    `1 - quantile`". That sentence is WITHDRAWN -- it is the #1856 claim this
    module exists to retract, and it survived here in the present tense while
    `conformal_exceedance` below named it as the error and the report printed
    "the exceedance is 1/(m+1) = 2.0%, not 1.0%". Exchangeability is real; what
    it gives is the RANK result in `conformal_exceedance`, not a centre at
    1 - q, and not anything at all for an interpolated threshold.)
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
        # Naming the standards this run is APPLYING. Calling them "the
        # operator's" while shipping them as live defaults was applying a
        # standard and labelling the application as a question.
        out.append(f"  STANDARDS IN FORCE ON THIS RUN (not deferred — applied): "
                   f"quantile mode is the\n  DEFAULT verdict path; "
                   f"MIN_SAFE_FOR_SPLIT={MIN_SAFE_FOR_SPLIT} decides whether the "
                   f"held-out check prints at all\n  (the conformal minimum implied "
                   f"by q={quantile:g} is {2 * CONFORMAL_MIN_CALIB(quantile)}); and the "
                   f"separation verdict uses a fixed\n  {SEPARATION_MULTIPLE:g}x "
                   "multiple. Each decides an output of this run.")
        if len(safe) < MIN_SAFE_FOR_SPLIT:
            out.append(f"  THIN SAMPLE: {len(safe)} safe check-ins (want "
                       f"{MIN_SAFE_FOR_SPLIT}). At p{quantile * 100:g} the threshold is "
                       "set by the top one or two\n  values, so it is noise. Read "
                       "nothing from the separation below.")
        else:
            calib, holdout = split_sample(safe, seed=seed)
            # RANK threshold, not pct(): the conformal guarantee is about ranks
            # and holds for any continuous distribution only if the threshold IS
            # an order statistic. Interpolating made the coverage
            # distribution-dependent -- the defect that made the previous two
            # centre claims wrong.
            held_k = sum(1 for z in holdout if z > rank_conformal_threshold(calib, quantile))
            held = held_k / len(holdout)
            k = conformal_rank(len(calib), quantile)
            exact = conformal_exceedance(len(calib), quantile)
            note = ""
            if len(calib) < CONFORMAL_MIN_CALIB(quantile):
                note = (f"\n   NB m={len(calib)} < {CONFORMAL_MIN_CALIB(quantile)}: no "
                        f"p{quantile * 100:g} rank exists at this calibration size, so "
                        f"the threshold degenerates to\n   the calibration maximum and "
                        f"the exceedance is 1/(m+1) = {exact:.1%}, not "
                        f"{1 - quantile:.1%}.")
            out.append(f"  (held-out check on a disjoint half: {held:.1%} "
                       f"({held_k}/{len(holdout)}) against an EXACT "
                       f"distribution-free\n   exceedance of {exact:.1%} — the "
                       f"{k}th of m={len(calib)} order statistics, 1 - {k}/{len(calib) + 1}. "
                       f"Still not a regression bound.{note})")
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
    elif tp > SEPARATION_MULTIPLE * ref:
        out.append(f"  → SEPARATES at this threshold: {tp / ref:.1f}x the {ref_kind} "
                   f"false-positive rate (exactly {tp:.4%} / {ref:.4%}). Bounds nothing "
                   "about whether the residual predicts outcomes.")
    else:
        out.append(f"  → WEAK: {tp / ref:.1f}x the {ref_kind} false-positive rate "
                   f"(exactly {tp:.4%} / {ref:.4%}); want "
                   f">{SEPARATION_MULTIPLE:g}x.")
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
