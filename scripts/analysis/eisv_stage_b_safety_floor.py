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
import sys

import psycopg2
import psycopg2.extras


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url", default="postgresql://postgres:postgres@localhost:5432/governance")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
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

    # Regression bound: pick a threshold at the safe-verdict p99 and ask what it
    # would do. A residual policy thresholded above healthy variation should flag
    # ~0 currently-safe agents (the floor) while still separating non-safe.
    if safe and len(safe) > 50:
        thr = pct(safe, 0.99)
        non_safe = [z for v, xs in by_verdict.items() if v != "safe" for z in xs]
        fp = sum(1 for z in safe if z > thr) / len(safe)
        tp = (sum(1 for z in non_safe if z > thr) / len(non_safe)) if non_safe else None
        print(f"\nThreshold = safe-verdict p99 = {thr:.2f}")
        print(f"  false-positive rate among currently-safe : {fp:.1%}  (regression floor — want low)")
        if tp is not None:
            print(f"  flag rate among currently non-safe        : {tp:.1%}  (want >> fp)")
            verdict = ("SEPARATES — residual is non-regressive here" if tp > 3 * max(fp, 0.01)
                       else "WEAK — residual does not cleanly separate at this threshold")
            print(f"  → {verdict}")
        else:
            print("  (no non-safe check-ins in window to measure separation)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
