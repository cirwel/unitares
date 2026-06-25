#!/usr/bin/env python3
"""Hysteresis grounding — measure the residual's temporal structure.

The roadmap's policy hysteresis (§4c/§8.2) was an arbitrary guess (0.5–0.6).
This replaces the guess with a measurement: how persistent vs flickery is the
per-agent residual? A signal that barely moves step-to-step needs little
hysteresis; a noisy one needs more.

Output:
  - lag-1 autocorrelation ρ of the residual (pooled within-agent pairs)
  - fraction of high (>p90) excursions that last exactly one check-in (noise)

Finding (2026-06-25): ρ ≈ 0.994, ~90% of excursions persist — the residual is
already very smooth. That smoothing is largely *pre-paid* by the behavioral EISV
the residual is built on (EMA alphas ~0.08–0.15), so adding a 0.5–0.6 policy dwell
on top would be double-damping. Don't re-introduce it as a magic number; the real
question is smoothing *placement* (estimator vs policy), keeping the total near
today's. See roadmap §8.2.

Usage: PYTHONPATH=. python3 scripts/analysis/eisv_residual_autocorr.py
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict

import psycopg2


def _std(d):
    c = d.get("count", 0)
    if not c or c < 2:
        return None
    v = d.get("m2", 0.0) / c
    return math.sqrt(v) if v > 0 else None


def _residual(b):
    bs = b.get("baseline_stats") or {}
    z = 0.0
    for ax in ("E", "I", "S", "V"):
        d = bs.get(ax)
        cur = b.get(ax)
        if not d or cur is None:
            return None
        s = _std(d)
        if not s:
            return None
        z += abs(float(cur) - d.get("mean", 0.0)) / s
    return z


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url", default="postgresql://postgres:postgres@localhost:5432/governance")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-series", type=int, default=5)
    args = ap.parse_args()
    conn = psycopg2.connect(args.db_url)

    series = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT identity_id, state_json->'behavioral_eisv' AS b
            FROM core.agent_state
            WHERE recorded_at > now() - (%s::int * interval '1 day')
              AND state_json ? 'behavioral_eisv'
              AND state_json->'behavioral_eisv'->'warmup'->>'is_baselined' = 'true'
            ORDER BY identity_id, recorded_at
            """,
            (args.days,),
        )
        for iid, b in cur:
            if isinstance(b, str):
                b = json.loads(b)
            z = _residual(b)
            if z is not None:
                series[iid].append(z)
    conn.close()

    pairs = [(xs[i], xs[i + 1]) for xs in series.values()
             if len(xs) >= args.min_series for i in range(len(xs) - 1)]
    usable = sum(1 for xs in series.values() if len(xs) >= args.min_series)
    print(f"baselined agents with usable series (≥{args.min_series}): {usable}")
    if not pairs:
        print("no usable residual series")
        return 0

    x = [p[0] for p in pairs]
    y = [p[1] for p in pairs]
    mx, my = statistics.mean(x), statistics.mean(y)
    den = math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))
    rho = sum((a - mx) * (b - my) for a, b in pairs) / den if den else float("nan")
    print(f"lag-1 residual autocorrelation ρ = {rho:.3f}  (n_pairs={len(pairs)})")
    if 0 < rho < 1:
        print(f"  → persistence timescale τ ≈ {-1/math.log(rho):.1f} check-ins")
        print(f"  → high ρ ⇒ little added hysteresis needed (smoothing pre-paid by behavioral EMA)")

    allv = sorted(v for xs in series.values() for v in xs)
    p90 = allv[int(0.9 * len(allv))]
    one_step = total = 0
    for xs in series.values():
        i = 0
        while i < len(xs):
            if xs[i] > p90:
                j = i
                while j + 1 < len(xs) and xs[j + 1] > p90:
                    j += 1
                total += 1
                if j == i:
                    one_step += 1
                i = j + 1
            else:
                i += 1
    if total:
        print(f"excursions >p90 lasting exactly 1 check-in (noise): "
              f"{one_step}/{total} = {100*one_step/total:.0f}%")
        print("  → low transient rate ⇒ excursions are real, not flicker; "
              "policy hysteresis should be SMALL, not 0.5–0.6 (roadmap §8.2)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
