#!/usr/bin/env python3
"""Offline shadow of I's calibration repair, from frozen snapshots.

I's dominant term is ``1 - calibration_error``, where the error is one fleet
scalar that did not move in the 30 days to 2026-10-12 (ledger row 48). The
contract's repair is to repoint it at an external referent: the agent's own
stated confidence against outcomes something other than the agent verified.

The live per-agent candidate (#2194) cannot do this yet: the outcomes that carry
an agent confidence are self-reports, and the verified outcomes carry none.
But the confidence an agent stated is stored on the outcome rows the check-in
path auto-emits (``detail.confidence``), so a verified outcome can be paired
with the same identity's most recent stated confidence before it. This script
does that pairing, computes the per-agent error with the deployed estimator's
arithmetic, substitutes it causally into each persisted I observation, and
reports coverage and what the substituted term would contribute.

Estimator (``CalibrationChecker.compute_agent_calibration_candidate``): bin pairs
by confidence range (0-0.5, 0.5-0.7, 0.7-0.8, 0.8-0.9, 0.9-1.0); per bin
|mean outcome score - mean confidence|; average over bins with at least 5 pairs
whose newest pair is within 7 days of the check-in.

Measurement-only and offline: reads frozen JSONL files, never opens a database
connection, changes nothing deployed. No outcome is used to fit anything, and
no claim about outcome prediction follows. ``--print-export-sql`` prints the
read-only queries that produced the inputs.

Usage:
    python3 scripts/analysis/eisv_i_calibration_shadow_read.py \\
        --snapshot snapshot.jsonl --verified verified_outcomes.jsonl \\
        --confidence confidence_obs.jsonl
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timedelta

EXPORT_SQL = """\
-- read-only. No lower time bound: the deployed per-agent candidate bins are
-- lifetime (only the freshness check uses the newest pair), so every causally
-- prior pair must be available.
SET default_transaction_read_only = on;
COPY (SELECT jsonb_build_object('id', i.identity_id::text, 'ts', o.ts, 'type', o.outcome_type,
                                'score', o.outcome_score, 'bad', o.is_bad,
                                'grade', o.detail->>'corroboration_grade')
      FROM audit.outcome_events o JOIN core.identities i ON i.agent_id = o.agent_id
      WHERE o.ts <= :window_end
        AND o.detail->>'corroboration_grade' IN ('externally_verified', 'tool_observed')
        AND o.outcome_type IN ('task_completed', 'task_failed', 'test_passed', 'test_failed')
      ORDER BY i.identity_id, o.ts) TO STDOUT;   -- verified_outcomes.jsonl
-- Stated confidence from both places it is recorded: self-reported outcome rows
-- (detail.confidence) and, from the check-in confidence capture onward, every
-- check-in's telemetry envelope (eisv_telemetry.self_report).
COPY (SELECT row FROM (
        SELECT jsonb_build_object('id', i.identity_id::text, 'ts', o.ts,
                                  'confidence', (o.detail->>'confidence')::float,
                                  'src', 'outcome_detail') AS row, i.identity_id, o.ts
        FROM audit.outcome_events o JOIN core.identities i ON i.agent_id = o.agent_id
        WHERE o.ts <= :window_end
          AND o.detail->>'confidence' IS NOT NULL AND o.detail->>'confidence' <> 'null'
        UNION ALL
        SELECT jsonb_build_object('id', a.identity_id::text, 'ts', a.recorded_at,
                                  'confidence', (a.state_json->'eisv_telemetry'->'self_report'
                                                 ->>'confidence_reported')::float,
                                  'src', 'checkin_self_report'), a.identity_id, a.recorded_at
        FROM core.agent_state a
        WHERE a.recorded_at <= :window_end
          AND a.state_json->'eisv_telemetry'->'self_report'->>'confidence_reported' IS NOT NULL
      ) u ORDER BY identity_id, ts) TO STDOUT;   -- confidence_obs.jsonl
"""

BINS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0))
MIN_PER_BIN = 5
MAX_STALENESS = timedelta(days=7)
PAIR_WINDOW = timedelta(hours=24)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _bin(conf: float) -> int:
    for i, (lo, hi) in enumerate(BINS):
        if lo <= conf < hi or (i == len(BINS) - 1 and conf == hi):
            return i
    return len(BINS) - 1 if conf > 1 else 0


def _q(xs):
    xs = sorted(xs)
    if not xs:
        return "n=0"
    def pct(p):
        return xs[min(len(xs) - 1, int(p * (len(xs) - 1) + 0.5))]
    return f"n={len(xs)} mean={st.fmean(xs):.4f} p10={pct(.1):.4f} p50={pct(.5):.4f} p90={pct(.9):.4f}"


def _load(path):
    raw = open(path, "rb").read()
    return raw, [json.loads(line) for line in raw.decode().splitlines() if line.strip()]


def agent_error(pairs, as_of):
    """Deployed-estimator error over pairs observed before ``as_of``."""
    by_bin = defaultdict(list)
    for ts, conf, score in pairs:
        if ts < as_of:
            by_bin[_bin(conf)].append((ts, conf, score))
    gaps = []
    for rows in by_bin.values():
        newest = max(r[0] for r in rows)
        if len(rows) >= MIN_PER_BIN and as_of - newest <= MAX_STALENESS:
            gaps.append(abs(st.fmean(r[2] for r in rows) - st.fmean(r[1] for r in rows)))
    return (st.fmean(gaps), len(gaps)) if gaps else (None, 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--snapshot")
    ap.add_argument("--verified")
    ap.add_argument("--confidence")
    ap.add_argument("--print-export-sql", action="store_true")
    args = ap.parse_args()
    if args.print_export_sql:
        print(EXPORT_SQL)
        return 0
    if not (args.snapshot and args.verified and args.confidence):
        ap.error("--snapshot, --verified and --confidence are required")

    inputs = {}
    for name in ("snapshot", "verified", "confidence"):
        raw, rows = _load(getattr(args, name))
        inputs[name] = rows
        print(f"{name}: {len(rows)} rows, sha256 {hashlib.sha256(raw).hexdigest()}")

    conf_by_id = defaultdict(list)
    sources = defaultdict(int)
    for c in inputs["confidence"]:
        conf_by_id[c["id"]].append((_ts(c["ts"]), float(c["confidence"])))
        sources[c.get("src", "outcome_detail")] += 1
    print(f"confidence observations by source: {dict(sources)}")
    for v in conf_by_id.values():
        v.sort()

    pairs_by_id = defaultdict(list)
    unpaired = 0
    for o in inputs["verified"]:
        series = conf_by_id.get(o["id"], [])
        ts = _ts(o["ts"])
        k = bisect.bisect_left([t for t, _ in series], ts) - 1
        if k >= 0 and ts - series[k][0] <= PAIR_WINDOW:
            pairs_by_id[o["id"]].append((ts, series[k][1], float(o["score"])))
        else:
            unpaired += 1
    n_pairs = sum(len(v) for v in pairs_by_id.values())
    print(f"\n== Pairing verified outcomes with the identity's last stated confidence (<= 24 h before)")
    print(f"  verified outcomes: {len(inputs['verified'])}; paired: {n_pairs}; unpaired: {unpaired}")
    print(f"  identities with any pair: {len(pairs_by_id)}; pairs per identity {_q([len(v) for v in pairs_by_id.values()])}")

    rows = [r for r in inputs["snapshot"] if not r.get("synthetic") and (r.get("I") or {}).get("components")]
    covered = 0
    deltas = []
    by_id_obs = defaultdict(list)
    by_id_cf = defaultdict(list)
    errs = []
    fleet_vals = set()
    for r in rows:
        dim = r["I"]
        comp = next((c for c in dim["components"] if c["name"] == "calibration_accuracy"), None)
        if comp is None:
            continue
        retained = 1.0
        for adj in dim.get("adjustments") or []:
            retained *= float(adj.get("retained_weight", 1.0))
        eff_w = float(comp["weight"]) * retained
        fleet_vals.add(round(float(comp["value"]), 9))
        err, nbins = agent_error(pairs_by_id.get(r["id"], []), _ts(r["ts"]))
        obs = float(dim["value"])
        cf = obs
        if err is not None:
            covered += 1
            errs.append(err)
            cf = obs + eff_w * ((1.0 - err) - float(comp["value"]))
            deltas.append(cf - obs)
        by_id_obs[r["id"]].append(obs)
        by_id_cf[r["id"]].append(cf)

    print(f"\n== Substituting the per-agent error into persisted I observations (causal, as of each check-in)")
    print(f"  I observations: {len(rows)}; with an eligible per-agent error: {covered} "
          f"({covered / len(rows):.1%}); identities covered: "
          f"{sum(1 for i in by_id_obs if any(o != c for o, c in zip(by_id_obs[i], by_id_cf[i])))}")
    print(f"  fleet calibration component values seen: {sorted(fleet_vals)}")
    print(f"  per-agent error where eligible: {_q(errs)}  (fleet error 1 - component value)")
    print(f"  I change where substituted (cf - observed): {_q(deltas)}")
    sd_obs, sd_cf = [], []
    for i in by_id_obs:
        if len(by_id_obs[i]) >= 20 and any(o != c for o, c in zip(by_id_obs[i], by_id_cf[i])):
            sd_obs.append(st.pstdev(by_id_obs[i]))
            sd_cf.append(st.pstdev(by_id_cf[i]))
    print(f"  covered identities with >= 20 rows: {len(sd_obs)}")
    if sd_obs:
        print(f"    within-identity SD of I observation: {_q(sd_obs)}")
        print(f"    within-identity SD with the substituted term: {_q(sd_cf)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
