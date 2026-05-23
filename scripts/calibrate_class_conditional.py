#!/usr/bin/env python3
"""Phase 2 calibration: measure per-class scale constants from production DB.

Implements the measurement protocol of paper §7 Class-Conditional Calibration
against the existing core.agent_state corpus. For each calibration class
(Lumen / Vigil / Sentinel / Watcher / Steward / embodied / resident_persistent /
ephemeral / default), computes:

  - corpus size N
  - healthy operating point: median (E, I, S) on healthy regime slice
  - manifold radius ||Δ||_max: 95th percentile of state-space distance from
    the class's own healthy operating point

Outputs a Python snippet ready to paste into config/governance_config.py
populating DELTA_NORM_MAX_BY_CLASS and HEALTHY_OPERATING_POINT_BY_CLASS.

Usage:
  python3 scripts/calibrate_class_conditional.py
  python3 scripts/calibrate_class_conditional.py --window-days 30
  python3 scripts/calibrate_class_conditional.py --output measured_constants.py

Note on scope: tier-3 heuristic is what's currently in production. This script
measures the empirical envelope of those tier-3 values per class — which is
what the manifold coherence form actually consumes. The S_SCALE/I_SCALE/E_SCALE
constants only matter when tier-1 (logprobs) or tier-2 (multi-sample) ships;
those columns are reported here as descriptive but not used to populate the
*_BY_CLASS dicts until tier-1/2 produces real S_raw/I_raw/E_raw.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass
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
    KNOWN_RESIDENT_LABELS,
    classify_by_label_and_tags as classify_from_db_row,
)

HEALTHY_REGIMES = ("nominal", "STABLE", "CONVERGENCE", "EXPLORATION")


@dataclass
class ClassStats:
    name: str
    n: int
    e_median: float
    i_median: float
    s_median: float
    e_p90: float
    i_p90: float
    s_p90: float
    delta_p95: float


def fetch_class_observations(
    conn,
    window_days: int,
) -> Dict[str, List[Tuple[float, float, float]]]:
    """Return {class_name: [(E, I, S), ...]} for healthy turns in the window."""
    sql = """
        SELECT
          i.metadata->>'label'                AS label,
          COALESCE(i.metadata->'tags', '[]')  AS tags,
          s.state_json,
          s.entropy,
          s.integrity
        FROM core.agent_state s
        JOIN core.identities  i  USING (identity_id)
        WHERE s.recorded_at >= now() - (%s::int * INTERVAL '1 day')
          AND s.regime = ANY (%s)
    """
    by_class: Dict[str, List[Tuple[float, float, float]]] = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (window_days, list(HEALTHY_REGIMES)))
        for row in cur:
            label = row["label"]
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
            if e is None:
                # No E in state_json → unusable for manifold calibration
                continue
            try:
                e = float(e)
                i_val = float(row["integrity"])
                s_val = float(row["entropy"])
            except (TypeError, ValueError):
                continue
            if not (0.0 <= e <= 1.0 and 0.0 <= i_val <= 1.0 and 0.0 <= s_val <= 1.0):
                continue

            cls = classify_from_db_row(label, tags)
            by_class.setdefault(cls, []).append((e, i_val, s_val))
    return by_class


def percentile(xs: List[float], p: float) -> float:
    """Linear-interpolation percentile (p in [0, 100])."""
    if not xs:
        return 0.0
    xs_sorted = sorted(xs)
    k = (len(xs_sorted) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return xs_sorted[int(k)]
    return xs_sorted[f] * (c - k) + xs_sorted[c] * (k - f)


def compute_class_stats(
    name: str,
    obs: List[Tuple[float, float, float]],
) -> ClassStats:
    es = [t[0] for t in obs]
    is_ = [t[1] for t in obs]
    ss = [t[2] for t in obs]

    e_med = statistics.median(es)
    i_med = statistics.median(is_)
    s_med = statistics.median(ss)

    deltas = [
        math.sqrt((e - e_med) ** 2 + (i - i_med) ** 2 + (s - s_med) ** 2)
        for (e, i, s) in obs
    ]

    return ClassStats(
        name=name,
        n=len(obs),
        e_median=e_med,
        i_median=i_med,
        s_median=s_med,
        e_p90=percentile(es, 90),
        i_p90=percentile(is_, 90),
        s_p90=percentile(ss, 90),
        delta_p95=percentile(deltas, 95),
    )


def render_python_snippet(
    stats: List[ClassStats],
    measured_on: str,
    n_min: int,
    known_residents: Optional[set] = None,
) -> str:
    """Render a Python snippet ready to paste into governance_config.py.

    known_residents: labels that MUST appear in the output — either with a
    measured value, a below-threshold skip comment, or an explicit missing
    comment. Prevents silent omission of a resident class that happened to
    have zero observations in the window (the Steward regression of 2026-04-18).
    """
    if known_residents is None:
        known_residents = KNOWN_RESIDENT_LABELS

    stats_by_name = {st.name: st for st in stats}
    residents_missing = sorted(known_residents - stats_by_name.keys())

    lines = [
        "# AUTOGENERATED by scripts/calibrate_class_conditional.py",
        f'# Measured {measured_on} on healthy production fleet.',
        "# Paste into config/governance_config.py to replace the empty Phase 1 dicts.",
        "",
        "DELTA_NORM_MAX_BY_CLASS: Dict[str, ScaleConstant] = {",
    ]
    for st in stats:
        if st.n < n_min:
            lines.append(
                f'    # "{st.name}": skipped — N={st.n} below threshold {n_min}; falls back to default'
            )
            continue
        lines.append(
            f'    "{st.name}": ScaleConstant('
            f'name="DELTA_NORM_MAX[{st.name}]", value={st.delta_p95:.4f}, '
            f'measured_on="{measured_on}", corpus_size={st.n}, '
            f'percentile=95, provenance="measured", '
            f'notes="Class-conditional manifold radius from healthy slice."),'
        )
    for resident in residents_missing:
        lines.append(
            f'    # "{resident}": MISSING — resident had 0 healthy observations '
            f'in window. Consider adding an explicit alias entry (provenance="alias") '
            f'so the fallback to default is visible, not silent.'
        )
    lines.append("}")
    lines.append("")

    lines.append(
        "# Healthy operating points per class — feed _compute_manifold's baseline."
    )
    lines.append("HEALTHY_OPERATING_POINT_BY_CLASS: Dict[str, tuple] = {")
    for st in stats:
        if st.n < n_min:
            continue
        lines.append(
            f'    "{st.name}": ({st.e_median:.4f}, {st.i_median:.4f}, {st.s_median:.4f}),  # N={st.n}'
        )
    for resident in residents_missing:
        lines.append(
            f'    # "{resident}": MISSING — see DELTA_NORM_MAX_BY_CLASS above.'
        )
    lines.append("}")
    lines.append("")

    # Descriptive stats for the report — not yet used in code (tier-1/2 scope)
    lines.append("# Descriptive: 90th-percentile envelopes (used once tier-1 logprobs ship)")
    lines.append("# Class            N      E_p90   I_p90   S_p90")
    for st in stats:
        lines.append(
            f"# {st.name:<16} {st.n:<6} {st.e_p90:.4f}  {st.i_p90:.4f}  {st.s_p90:.4f}"
        )

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-days", type=int, default=30,
                        help="Days of history to include (default: 30)")
    parser.add_argument("--n-min", type=int, default=30,
                        help="Minimum class population for measurement; below this falls back (default: 30)")
    parser.add_argument("--output", type=str, default=None,
                        help="Write snippet to this file instead of stdout")
    parser.add_argument("--db-url", type=str,
                        default="postgresql://postgres:postgres@localhost:5432/governance",
                        help="Postgres connection string")
    args = parser.parse_args()

    measured_on = __import__("datetime").datetime.now().strftime("%Y-%m-%d")

    print(f"[calibrate] Connecting to DB...", file=sys.stderr)
    conn = psycopg2.connect(args.db_url)

    print(f"[calibrate] Pulling healthy turns from last {args.window_days} days...", file=sys.stderr)
    by_class = fetch_class_observations(conn, args.window_days)

    print(f"[calibrate] Found {len(by_class)} classes with observations:", file=sys.stderr)
    for cls, obs in sorted(by_class.items(), key=lambda kv: -len(kv[1])):
        print(f"  {cls:<24} N={len(obs)}", file=sys.stderr)

    stats = [compute_class_stats(cls, obs) for cls, obs in by_class.items()]
    stats.sort(key=lambda s: -s.n)

    snippet = render_python_snippet(stats, measured_on, args.n_min)
    if args.output:
        with open(args.output, "w") as f:
            f.write(snippet)
        print(f"[calibrate] Wrote {args.output}", file=sys.stderr)
    else:
        print(snippet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
