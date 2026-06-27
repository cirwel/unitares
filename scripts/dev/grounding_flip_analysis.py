#!/usr/bin/env python3
"""Grounding-shadow flip analysis — the convergence readout for the GROUNDING_APPLY decision.

`UNITARES_GROUNDING_SHADOW=1` records, per check-in, what E/I/S/coherence *would* be
under grounding (manifold coherence, logprob/heuristic S, ...) vs the ungrounded values
that actually drove the decision — as `grounding_shadow` audit events. Activating
grounding (`GROUNDING_APPLY`) is consequential: the manifold coherence shift moves
basin/verdict fleet-wide. This tool answers "how much shadow data is enough to flip APPLY
safely" operationally — not with a clock, but by measuring **decision flips**, per agent
class, and whether they've converged.

Method: for each `grounding_shadow` event, recompute `classify_basin` (+ coherence-critical)
under grounded vs ungrounded coherence (grounding changes coherence/S, not V/risk — those
come from the matching `core.agent_state` row), and classify any flip as:
  * TIGHTEN — grounded is MORE conservative (high→boundary/low, or trips coherence-critical):
    a *false-pause* risk if grounding is wrong, a *correction* if the V-thermo coherence was
    an over-rosy artifact.
  * RELAX   — grounded is healthier (boundary/low→high): a *missed-pause* risk, or a correction.

A class is "ready to APPLY" when, over the window, its flip rate is stable across two
consecutive sub-windows AND either >= --min-flips flips are observed (so their TIGHTEN/RELAX
character is judgeable) OR >= --min-near near-threshold check-ins with zero flips (rule of 3:
0/300 bounds the harmful rate below ~1%). Per-class, never fleet-aggregate (the basin-shadow
lesson: an aggregate hides bifurcation).

Usage:
    python3 scripts/dev/grounding_flip_analysis.py            # full history
    python3 scripts/dev/grounding_flip_analysis.py --hours 48 --window-hours 24
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from config.governance_config import classify_basin, config  # noqa: E402

COH_CRIT = float(getattr(config, "COHERENCE_CRITICAL_THRESHOLD", 0.40))
_ORDER = {"low": 0, "boundary": 1, "high": 2}


def _psql() -> str:
    for c in ("psql", "/opt/homebrew/opt/postgresql@17/bin/psql"):
        if subprocess.run(["bash", "-lc", f"command -v {c}"], capture_output=True).returncode == 0 \
                or Path(c).exists():
            return c
    return "psql"


def fetch(db_url: str, hours: int | None) -> list[dict]:
    window = f"AND e.ts > now() - interval '{int(hours)} hours'" if hours else ""
    sql = f"""
    WITH gs AS (
      SELECT e.agent_id, e.ts,
        (e.payload->'E'->>'grounded')::float AS e,
        (e.payload->'I'->>'grounded')::float AS i,
        (e.payload->'S'->>'grounded')::float AS s,
        (e.payload->'coherence'->>'ungrounded')::float AS coh_ung,
        (e.payload->'coherence'->>'grounded')::float   AS coh_grd,
        e.payload->'sources'->>'coherence' AS coh_src,
        e.payload->'sources'->>'S' AS s_src
      FROM audit.events e WHERE e.event_type='grounding_shadow' {window}
    )
    SELECT row_to_json(t) FROM (
      SELECT gs.*, a.label,
        (SELECT st.volatility FROM core.agent_state st
           WHERE st.recorded_at BETWEEN gs.ts - interval '3s' AND gs.ts + interval '3s'
           ORDER BY abs(extract(epoch from st.recorded_at - gs.ts)) LIMIT 1) AS v,
        (SELECT COALESCE(st.risk_score,0) FROM core.agent_state st
           WHERE st.recorded_at BETWEEN gs.ts - interval '3s' AND gs.ts + interval '3s'
           ORDER BY abs(extract(epoch from st.recorded_at - gs.ts)) LIMIT 1) AS risk,
        extract(epoch from (now() - gs.ts))/3600.0 AS age_h
      FROM gs LEFT JOIN core.agents a ON a.id::text = gs.agent_id
    ) t;
    """
    env = dict(os.environ, PGPASSWORD=os.environ.get("PGPASSWORD", "postgres"))
    out = subprocess.run([_psql(), db_url, "-At", "-c", sql] if db_url.startswith("postgres")
                         else [_psql(), "-h", "localhost", "-U", "postgres", "-d", "governance",
                               "-At", "-c", sql],
                         capture_output=True, text=True, env=env)
    if out.returncode != 0:
        sys.exit(f"query failed: {out.stderr.strip()}")
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def flip_of(r: dict) -> tuple[bool, str | None, bool]:
    """Return (near_threshold, 'tighten'|'relax'|None, joined)."""
    if r.get("v") is None:
        return (False, None, False)
    kw = dict(E=r["e"], I=r["i"], S=r["s"], V=r["v"], risk_score=r["risk"])
    b_ung = classify_basin(coherence=r["coh_ung"], **kw)
    b_grd = classify_basin(coherence=r["coh_grd"], **kw)
    crit_ung, crit_grd = r["coh_ung"] < COH_CRIT, r["coh_grd"] < COH_CRIT
    near = b_ung != "high" or b_grd != "high" or crit_ung or crit_grd
    if b_ung == b_grd and crit_ung == crit_grd:
        return (near, None, True)
    tighten = _ORDER[b_grd] < _ORDER[b_ung] or (crit_grd and not crit_ung)
    return (near, "tighten" if tighten else "relax", True)


def analyze(rows: list[dict], window_h: float, min_near: int, min_flips: int) -> None:
    per: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    # split into two consecutive windows for the convergence test (recent vs prior)
    recent, prior = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])  # [flips, near]
    for r in rows:
        lbl = r.get("label") or "(unlabeled)"
        p = per[lbl]
        p["n"] += 1
        near, direction, joined = flip_of(r)
        if not joined:
            continue
        p["joined"] += 1
        if near:
            p["near"] += 1
        if direction:
            p["flip"] += 1
            p[direction] += 1
        bucket = recent if r["age_h"] <= window_h else prior
        bucket[lbl][0] += 1 if direction else 0
        bucket[lbl][1] += 1 if near else 0

    print(f"grounding_shadow flip analysis — COH_CRIT={COH_CRIT}, "
          f"rule: ready when stable AND (>={min_flips} flips characterized OR "
          f">={min_near} near / 0 flips)\n")
    hdr = f"{'class':20} {'n':>4} {'join':>4} {'near':>4} {'flip':>4} {'tght':>4} {'rlx':>4}  {'stable?':>7}  verdict"
    print(hdr)
    print("-" * len(hdr))
    ready, waiting = [], []
    for lbl, p in sorted(per.items(), key=lambda x: -x[1]["n"]):
        # convergence: recent vs prior flip-rate (per near-threshold event)
        rf, rn = recent[lbl]
        pf, pn = prior[lbl]
        if rn >= 10 and pn >= 10:
            r_rate, p_rate = rf / rn, pf / pn
            stable = "yes" if abs(r_rate - p_rate) <= 0.05 else "no"
        else:
            stable = "—"  # not enough in both windows yet
        if p["joined"] < 30:
            verdict = "INSUFFICIENT (warm volume)"
            waiting.append(lbl)
        elif p["flip"] >= min_flips:
            verdict = (f"FLIPS: {p['tighten']} tighten / {p['relax']} relax — "
                       f"judge harm{' [STABLE]' if stable == 'yes' else ''}")
            (ready if stable == "yes" else waiting).append(lbl)
        elif p["near"] >= min_near and p["flip"] == 0:
            verdict = f"CONVERGED-SAFE (0 flips / {p['near']} near)"
            ready.append(lbl)
        else:
            need = max(min_near - p["near"], 0)
            verdict = f"need ~{need} more near-threshold (or {min_flips - p['flip']} more flips)"
            waiting.append(lbl)
        print(f"{lbl[:20]:20} {p['n']:>4} {p['joined']:>4} {p['near']:>4} {p['flip']:>4} "
              f"{p['tighten']:>4} {p['relax']:>4}  {stable:>7}  {verdict}")

    print(f"\nAPPLY readiness (per-class, NOT fleet-wide):")
    print(f"  ready to judge:  {', '.join(ready) or '(none yet)'}")
    print(f"  still gathering: {', '.join(waiting) or '(none)'}")
    print("  → Flip APPLY only for classes with a characterized, acceptable flip profile. "
          "Leave thin/ephemeral classes ungrounded until covered.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url", default=os.environ.get("DB_POSTGRES_URL", ""))
    ap.add_argument("--hours", type=int, default=None, help="lookback window (default: all)")
    ap.add_argument("--window-hours", type=float, default=24.0, help="convergence sub-window")
    ap.add_argument("--min-near", type=int, default=300)
    ap.add_argument("--min-flips", type=int, default=10)
    args = ap.parse_args()
    rows = fetch(args.db_url, args.hours)
    if not rows:
        print("No grounding_shadow events. Is UNITARES_GROUNDING_SHADOW=1 on the live server?")
        return 0
    analyze(rows, args.window_hours, args.min_near, args.min_flips)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
