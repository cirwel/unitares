#!/usr/bin/env python3
"""Φ→telemetry red-team — what changes when Φ stops flooring the verdict?

UNITARES_PHI_TELEMETRY_ONLY makes the behavioral/residual assessment
authoritative for confident agents instead of ``more_severe(Φ, behavioral)``.
Because behavioral ≤ more_severe(Φ, behavioral), the flag can only *de-escalate*
— it never adds a pause. So the red-team's question is: WHAT de-escalates, and
is it Φ over-flagging hard work (good) or a real drift signal behavioral missed
(bad)?

Method: replay persisted baselined check-ins. For each, the persisted ``verdict``
is the floored (production, flag-off) result; rebuild the behavioral assessment
from ``behavioral_eisv`` and compare. A check-in where floored is MORE severe
than behavioral-alone is one the flag would de-escalate.

No outcome labels (that's the anchor bridge). This characterises the verdict
delta and surfaces any high-risk→safe drops for eyeballing — it does not certify
that every de-escalation was correct.

The window is the last ``--days`` days up to now. A window that crosses an EISV
instrument change (src/eisv_eras.py) is refused unless
``--allow-cross-era REASON`` is given, and the crossed changes are printed.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import eisv_eras  # noqa: E402 — standalone module, no server imports
from scripts.analysis import eisv_era_sql  # noqa: E402
from src.agent_behavioral_baseline import WelfordStats  # noqa: E402
from src.behavioral_state import BehavioralEISV  # noqa: E402
from src.behavioral_assessment import assess_behavioral_state  # noqa: E402
from src.governance_monitor import _VERDICT_SEVERITY  # noqa: E402

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402


def rebuild(beisv) -> BehavioralEISV:
    st = BehavioralEISV()
    bs = beisv.get("baseline_stats") or {}
    counts = []
    for dim in ("E", "I", "S", "V"):
        d = bs.get(dim)
        if not d:
            return None
        bl: WelfordStats = getattr(st, f"_baseline_{dim}")
        bl.count = int(d.get("count", 0))
        bl.mean = float(d.get("mean", 0.0))
        bl.m2 = float(d.get("m2", 0.0))
        counts.append(bl.count)
    for dim in ("E", "I", "S", "V"):
        v = beisv.get(dim)
        if v is not None:
            setattr(st, dim, float(v))
    st.update_count = max(counts) if counts else 0
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url", default="postgresql://postgres:postgres@localhost:5432/governance")
    ap.add_argument("--days", type=int, default=7)
    eisv_eras.add_cross_era_argument(ap)
    args = ap.parse_args()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    if eisv_eras.check_window(start, end, allow_cross=args.allow_cross_era) is None:
        return 2
    conn = psycopg2.connect(args.db_url)
    # One read-only REPEATABLE READ snapshot: the instrument-id check and the
    # read below see the same committed rows.
    conn.set_session(isolation_level="REPEATABLE READ", readonly=True)

    transitions = {}   # (floored -> behavioral) -> count
    deescalated = 0
    examined = 0
    severe_drops = []  # high-risk floored -> safe behavioral (eyeball these)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(eisv_era_sql.instrument_id_counts_sql("%s", "%s"), (start, end))
        id_counts = {r["instrument_id"]: r["n"] for r in cur.fetchall()}
        provenance = eisv_eras.check_window(
            start, end, allow_cross=args.allow_cross_era, id_counts=id_counts)
        if provenance is None:
            conn.close()
            return 2
        cur.execute(
            """
            SELECT i.metadata->>'label' AS label,
                   s.state_json->'behavioral_eisv' AS beisv,
                   s.state_json->>'verdict' AS floored
            FROM core.agent_state s JOIN core.identities i USING(identity_id)
            WHERE s.recorded_at > %s AND s.recorded_at < %s
              AND s.state_json ? 'behavioral_eisv'
              AND s.state_json->'behavioral_eisv'->'warmup'->>'is_baselined' = 'true'
              AND s.state_json ? 'verdict'
            """,
            (start, end),
        )
        for r in cur:
            beisv = r["beisv"]
            if isinstance(beisv, str):
                beisv = json.loads(beisv)
            st = rebuild(beisv)
            if st is None or float(beisv.get("confidence", 0) or 0) < 0.3:
                continue
            res = assess_behavioral_state(st, rho=0.0)
            beh = res.verdict
            floored = r["floored"]
            if floored not in _VERDICT_SEVERITY or beh not in _VERDICT_SEVERITY:
                continue
            examined += 1
            if _VERDICT_SEVERITY[floored] > _VERDICT_SEVERITY[beh]:
                deescalated += 1
                transitions[(floored, beh)] = transitions.get((floored, beh), 0) + 1
                if floored == "high-risk" and beh == "safe":
                    severe_drops.append(r["label"])
            elif _VERDICT_SEVERITY[beh] > _VERDICT_SEVERITY[floored]:
                # behavioral worse than floored — should not happen (floor=max);
                # count as an escalation the flag would NOT cause (sanity check).
                transitions[(floored, f"!{beh}")] = transitions.get((floored, f"!{beh}"), 0) + 1
    conn.close()

    print(f"=== Φ→telemetry red-team (last {args.days}d, confident baselined) ===")
    for line in provenance:
        print(line)
    print(f"examined={examined}  would de-escalate under flag={deescalated} "
          f"({100*deescalated/examined:.1f}%)" if examined else "examined=0")
    print("\nverdict transitions (floored → behavioral-authoritative):")
    for (f, b), n in sorted(transitions.items(), key=lambda kv: -kv[1]):
        mark = "  ⚠ behavioral MORE severe (flag would NOT cause)" if b.startswith("!") else ""
        print(f"  {f:<10} → {b:<10} {n}{mark}")
    print(f"\nhigh-risk → safe drops (eyeball — did behavioral miss real drift?): {len(severe_drops)}")
    if severe_drops:
        from collections import Counter
        for label, n in Counter(severe_drops).most_common(10):
            print(f"  {label}: {n}")
    print("\nNote: the flag only de-escalates; it cannot add a pause. Remaining risk is"
          "\nwhether any de-escalation dropped a genuine drift signal — needs outcome"
          "\nlabels (anchor bridge) to settle; this bounds and surfaces, doesn't certify.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
