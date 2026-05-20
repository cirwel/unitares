#!/usr/bin/env python3
"""Verdict counterfactual: how many basin assignments would flip under
class-conditional grounded coherence vs the legacy fleet-wide tanh form?

For each production agent_state row in the window, compute:
  - legacy basin: classify_basin(E, I, S, V, coherence_legacy, risk)
  - grounded basin: classify_basin(E, I, S, V, coherence_manifold(class), risk)
where coherence_manifold uses class-conditional ||Δ||_max and
healthy operating point measured by Phase 2 calibration.

Reports per-class flip counts and direction (high↔boundary↔low).

This is the result that converts the paper from methodology to
paper-with-a-result, per the peer-review feedback.

Usage:
  python3 scripts/verdict_counterfactual.py --window-days 30
  python3 scripts/verdict_counterfactual.py --output verdict_flips.csv --csv
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary", file=sys.stderr)
    sys.exit(1)

# Ensure repo root is importable so we can use the canonical class fold from
# src/. The module is pure-stdlib (no governance-server runtime dependency).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.grounding.class_indicator import (  # noqa: E402
    classify_by_label_and_tags as classify_from_db_row,
)


# ── Mirror of config.governance_config.classify_basin ─────────────────
BASIN_HIGH_E_MIN = 0.6
BASIN_HIGH_I_MIN = 0.7
BASIN_HIGH_S_MAX = 0.25
BASIN_HIGH_V_ABS_MAX = 0.15
BASIN_HIGH_COHERENCE_MIN = 0.45
BASIN_HIGH_RISK_MAX = 0.45

BASIN_LOW_I_CEIL = 0.5
BASIN_LOW_COHERENCE_CEIL = 0.40
BASIN_LOW_V_ABS_FLOOR = 0.30
BASIN_LOW_RISK_FLOOR = 0.70


def classify_basin(E: float, I: float, S: float, V: float,
                   coherence: float, risk_score: float) -> str:
    if risk_score is None:
        risk_score = 0.0
    V_abs = abs(V)
    if (I < BASIN_LOW_I_CEIL
            or coherence < BASIN_LOW_COHERENCE_CEIL
            or V_abs > BASIN_LOW_V_ABS_FLOOR
            or risk_score >= BASIN_LOW_RISK_FLOOR):
        return "low"
    if (E >= BASIN_HIGH_E_MIN
            and I >= BASIN_HIGH_I_MIN
            and S <= BASIN_HIGH_S_MAX
            and V_abs <= BASIN_HIGH_V_ABS_MAX
            and coherence >= BASIN_HIGH_COHERENCE_MIN
            and risk_score <= BASIN_HIGH_RISK_MAX):
        return "high"
    return "boundary"


# ── Phase 2 measured constants (replicated for self-containment) ──────
DELTA_NORM_MAX_BY_CLASS = {
    "Lumen":    0.1187,
    "default":  0.2018,
    "Sentinel": 0.1702,
    "Vigil":    0.1705,
    "Watcher":  0.3948,
}
DELTA_NORM_MAX_DEFAULT = 1.8

HEALTHY_OPERATING_POINT_BY_CLASS = {
    "Lumen":    (0.7454, 0.8001, 0.1678),
    "default":  (0.7264, 0.7934, 0.2364),
    "Sentinel": (0.7506, 0.7981, 0.1934),
    "Vigil":    (0.7371, 0.7896, 0.2404),
    "Watcher":  (0.7482, 0.7686, 0.2477),
}
# Fleet fallback: BASIN_HIGH corner per governance_config.py
HEALTHY_OPERATING_POINT_DEFAULT = (BASIN_HIGH_E_MIN, BASIN_HIGH_I_MIN, 0.0)


def compute_manifold_coherence(E: float, I: float, S: float, agent_class: str) -> float:
    healthy = HEALTHY_OPERATING_POINT_BY_CLASS.get(agent_class, HEALTHY_OPERATING_POINT_DEFAULT)
    delta_max = DELTA_NORM_MAX_BY_CLASS.get(agent_class, DELTA_NORM_MAX_DEFAULT)
    dx = E - healthy[0]
    dy = I - healthy[1]
    dz = S - healthy[2]
    norm = math.sqrt(dx * dx + dy * dy + dz * dz)
    ratio = norm / delta_max
    return 1.0 - max(0.0, min(1.0, ratio))


def fetch_rows(conn, window_days: int):
    sql = """
        SELECT
          i.metadata->>'label'                AS label,
          COALESCE(i.metadata->'tags', '[]')  AS tags,
          s.entropy,
          s.integrity,
          s.volatility,
          s.coherence,
          s.state_json
        FROM core.agent_state s
        JOIN core.identities  i  USING (identity_id)
        WHERE s.recorded_at >= now() - (%s::int * INTERVAL '1 day')
    """
    rows = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (window_days,))
        for row in cur:
            tags_raw = row["tags"]
            if isinstance(tags_raw, str):
                try:
                    tags = json.loads(tags_raw)
                except (ValueError, TypeError):
                    tags = []
            else:
                tags = tags_raw or []
            state_json = row["state_json"] or {}
            if isinstance(state_json, str):
                try:
                    state_json = json.loads(state_json)
                except (ValueError, TypeError):
                    state_json = {}
            e = state_json.get("E")
            risk = state_json.get("risk_score", 0.0)
            if e is None:
                continue
            try:
                e = float(e)
                i_val = float(row["integrity"])
                s_val = float(row["entropy"])
                v_val = float(row["volatility"])
                c_legacy = float(row["coherence"])
                risk = float(risk) if risk is not None else 0.0
            except (TypeError, ValueError):
                continue
            cls = classify_from_db_row(row["label"], tags)
            rows.append({
                "class": cls,
                "E": e, "I": i_val, "S": s_val, "V": v_val,
                "coherence_legacy": c_legacy,
                "risk_score": risk,
            })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--db-url", type=str,
                        default="postgresql://postgres:postgres@localhost:5432/governance")
    parser.add_argument("--csv", action="store_true",
                        help="Output CSV of all flips for further analysis")
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    print(f"[counterfactual] Connecting to DB...", file=sys.stderr)
    conn = psycopg2.connect(args.db_url)

    print(f"[counterfactual] Pulling agent_state rows from last {args.window_days} days...", file=sys.stderr)
    rows = fetch_rows(conn, args.window_days)
    print(f"[counterfactual] {len(rows)} rows with computable E (state_json contains 'E')", file=sys.stderr)

    # Tally by class
    flips_by_class: Dict[str, Counter] = defaultdict(Counter)
    totals_by_class: Dict[str, int] = defaultdict(int)
    coherence_diff_by_class: Dict[str, list] = defaultdict(list)

    csv_lines: List[str] = []
    if args.csv:
        csv_lines.append("class,E,I,S,V,risk,c_legacy,c_grounded,basin_legacy,basin_grounded,flipped")

    for r in rows:
        cls = r["class"]
        c_grounded = compute_manifold_coherence(r["E"], r["I"], r["S"], cls)
        basin_legacy = classify_basin(r["E"], r["I"], r["S"], r["V"],
                                       r["coherence_legacy"], r["risk_score"])
        basin_grounded = classify_basin(r["E"], r["I"], r["S"], r["V"],
                                         c_grounded, r["risk_score"])
        flipped = basin_legacy != basin_grounded
        totals_by_class[cls] += 1
        if flipped:
            flips_by_class[cls][f"{basin_legacy}→{basin_grounded}"] += 1
        coherence_diff_by_class[cls].append(c_grounded - r["coherence_legacy"])

        if args.csv:
            csv_lines.append(
                f"{cls},{r['E']:.4f},{r['I']:.4f},{r['S']:.4f},{r['V']:.4f},"
                f"{r['risk_score']:.4f},{r['coherence_legacy']:.4f},{c_grounded:.4f},"
                f"{basin_legacy},{basin_grounded},{int(flipped)}"
            )

    if args.csv:
        out = "\n".join(csv_lines) + "\n"
        if args.output:
            with open(args.output, "w") as f:
                f.write(out)
            print(f"[counterfactual] CSV written to {args.output}", file=sys.stderr)
        else:
            print(out)
        return 0

    # Summary report
    print()
    print("=" * 78)
    print("VERDICT COUNTERFACTUAL — class-conditional grounded vs fleet-wide legacy")
    print("=" * 78)
    print()
    total_flips = sum(sum(c.values()) for c in flips_by_class.values())
    total_rows = sum(totals_by_class.values())
    overall_pct = 100.0 * total_flips / total_rows if total_rows else 0.0
    print(f"Window:        {args.window_days} days")
    print(f"Total rows:    {total_rows:>6}")
    print(f"Total flips:   {total_flips:>6}  ({overall_pct:.1f}%)")
    print()
    print(f"{'Class':<12} {'N':>6} {'Flips':>7} {'%':>6}  {'Δc mean':>8} {'Δc max':>8}  Transitions")
    print("-" * 78)
    for cls in sorted(totals_by_class.keys(), key=lambda c: -totals_by_class[c]):
        n = totals_by_class[cls]
        f = sum(flips_by_class[cls].values())
        pct = 100.0 * f / n if n else 0.0
        deltas = coherence_diff_by_class[cls]
        dc_mean = sum(deltas) / len(deltas) if deltas else 0.0
        dc_max_abs = max((abs(d) for d in deltas), default=0.0)
        transitions = ", ".join(
            f"{k}:{v}" for k, v in sorted(flips_by_class[cls].items(),
                                          key=lambda kv: -kv[1])
        ) or "—"
        print(f"{cls:<12} {n:>6} {f:>7} {pct:>5.1f}%  {dc_mean:>+8.4f} {dc_max_abs:>8.4f}  {transitions}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
