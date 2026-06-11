#!/usr/bin/env python3
"""Outcome validation for the verdict counterfactual.

Tests whether basin-flip direction (predominantly into the low basin under
class-conditional grounded coherence) corresponds to agents actually performing
worse on objective tasks.

Signal: audit.outcome_events filtered to detail->>'eprocess_eligible' = 'true'
(hard exogenous signals only: test pass/fail, tool rejection, trajectory
validation, task completion — NOT self-reported outcomes).

Unit of analysis: (agent_id, day).

For each day an agent appears in core.agent_state, we:
  1. Classify every state_row into legacy_basin and grounded_basin using the
     same logic as scripts/verdict_counterfactual.py.
  2. Characterize the day's flip pattern (fraction flipping to low, dominant
     transition, etc.).
  3. Aggregate outcome_events on the same day: mean outcome_score, any is_bad.
  4. Bucket (agent_id, day) pairs by flip category and compare outcome rates.

Key test: if grounded form's "flip to low" has bias (not just disagreement),
agent-days dominated by high→low transitions should show measurably worse
outcomes than agent-days that stayed high→high.

Usage:
    python3 scripts/analysis/outcome_validation.py --window-days 30
    python3 scripts/analysis/outcome_validation.py --csv outcome_validation.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import asyncpg

# Reuse the counterfactual logic verbatim by path import.
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root / "scripts"))
from verdict_counterfactual import (  # noqa: E402
    classify_from_db_row,
    classify_basin,
    compute_manifold_coherence,
)


FLIP_REFERENCE = "high→high"
FLIP_CATEGORIES_OF_INTEREST = [
    "high→low",
    "high→boundary",
    "boundary→low",
    "boundary→boundary",
    "low→low",
    "high→high",
]


async def fetch_state_rows(conn, window_days: int) -> List[Dict]:
    """One row per core.agent_state tuple, carrying agent_id + ts + EISV + coherence_legacy."""
    sql = """
        SELECT
          i.agent_id                          AS agent_id,
          i.metadata->>'label'                AS label,
          COALESCE(i.metadata->'tags', '[]')  AS tags,
          s.recorded_at                       AS ts,
          s.entropy                           AS entropy,
          s.integrity                         AS integrity,
          s.volatility                        AS volatility,
          s.coherence                         AS coherence,
          s.state_json                        AS state_json
        FROM core.agent_state s
        JOIN core.identities  i  USING (identity_id)
        WHERE s.recorded_at >= now() - ($1::int * INTERVAL '1 day')
    """
    rows = []
    for r in await conn.fetch(sql, window_days):
        state_json = r["state_json"] or {}
        if isinstance(state_json, str):
            try:
                state_json = json.loads(state_json)
            except (ValueError, TypeError):
                state_json = {}
        tags_raw = r["tags"]
        if isinstance(tags_raw, str):
            try:
                tags = json.loads(tags_raw)
            except (ValueError, TypeError):
                tags = []
        else:
            tags = tags_raw or []
        e = state_json.get("E")
        if e is None:
            continue
        try:
            e = float(e)
            i_val = float(r["integrity"])
            s_val = float(r["entropy"])
            v_val = float(r["volatility"])
            c_legacy = float(r["coherence"])
            risk = float(state_json.get("risk_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        rows.append({
            "agent_id": r["agent_id"],
            "ts": r["ts"],
            "class": classify_from_db_row(r["label"], tags),
            "E": e, "I": i_val, "S": s_val, "V": v_val,
            "c_legacy": c_legacy,
            "risk": risk,
        })
    return rows


async def fetch_outcome_aggregates(conn, window_days: int) -> Dict[Tuple[str, str], Dict]:
    """Per (agent_id, day): count, mean outcome_score, any is_bad, dominant outcome_type.

    Filtered to outcome_type IN (test_passed, test_failed, task_completed,
    task_failed) — hard objective outcomes with is_bad/outcome_score columns
    directly populated. The narrower eprocess_eligible='true' filter yields too
    few events (<100/30d) to be usable.
    """
    sql = """
        SELECT
          agent_id,
          date_trunc('day', ts)::date  AS day,
          count(*)                     AS n_events,
          avg(outcome_score)           AS mean_score,
          bool_or(is_bad)              AS any_bad,
          sum(CASE WHEN is_bad THEN 1 ELSE 0 END)::int AS n_bad
        FROM audit.outcome_events
        WHERE ts >= now() - ($1::int * INTERVAL '1 day')
          AND outcome_type IN ('test_passed', 'test_failed',
                               'task_completed', 'task_failed')
        GROUP BY agent_id, date_trunc('day', ts)
    """
    out: Dict[Tuple[str, str], Dict] = {}
    for r in await conn.fetch(sql, window_days):
        out[(r["agent_id"], r["day"].isoformat())] = {
            "n_events": r["n_events"],
            "mean_score": float(r["mean_score"]) if r["mean_score"] is not None else None,
            "any_bad": bool(r["any_bad"]),
            "n_bad": r["n_bad"],
        }
    return out


def classify_flips(state_rows: List[Dict]) -> List[Dict]:
    """Add legacy_basin, grounded_basin, and flip label to each state row."""
    annotated = []
    for r in state_rows:
        cls = r["class"]
        c_grounded = compute_manifold_coherence(r["E"], r["I"], r["S"], cls)
        basin_legacy = classify_basin(r["E"], r["I"], r["S"], r["V"], r["c_legacy"], r["risk"])
        basin_grounded = classify_basin(r["E"], r["I"], r["S"], r["V"], c_grounded, r["risk"])
        annotated.append({
            **r,
            "c_grounded": c_grounded,
            "basin_legacy": basin_legacy,
            "basin_grounded": basin_grounded,
            "flip": f"{basin_legacy}→{basin_grounded}",
            "day": r["ts"].date().isoformat(),
        })
    return annotated


def bucket_agent_days(flipped: List[Dict]) -> Dict[Tuple[str, str], Dict]:
    """For each (agent_id, day), summarize the day's flip pattern."""
    days: Dict[Tuple[str, str], Dict] = defaultdict(lambda: {
        "class": None,
        "n_rows": 0,
        "flip_counts": Counter(),
    })
    for r in flipped:
        key = (r["agent_id"], r["day"])
        d = days[key]
        d["class"] = r["class"]
        d["n_rows"] += 1
        d["flip_counts"][r["flip"]] += 1

    for key, d in days.items():
        # Dominant flip = most common transition on that day.
        d["dominant_flip"] = d["flip_counts"].most_common(1)[0][0]
        d["any_to_low"] = any(f.endswith("→low") and not f.startswith("low")
                              for f in d["flip_counts"])
        d["any_degradation"] = any(
            f in {"high→boundary", "high→low", "boundary→low"}
            for f in d["flip_counts"]
        )
    return dict(days)


def summarize(
    agent_days: Dict[Tuple[str, str], Dict],
    outcomes: Dict[Tuple[str, str], Dict],
) -> Dict:
    """Join agent_days with outcomes and aggregate by flip bucket."""
    by_bucket: Dict[str, Dict] = defaultdict(lambda: {
        "n_agent_days": 0,
        "n_with_outcome": 0,
        "outcome_scores": [],
        "n_bad_days": 0,
        "n_events_total": 0,
        "classes": Counter(),
    })
    by_bucket_class: Dict[Tuple[str, str], Dict] = defaultdict(lambda: {
        "n_agent_days": 0,
        "n_with_outcome": 0,
        "outcome_scores": [],
        "n_bad_days": 0,
    })

    for key, d in agent_days.items():
        bucket = d["dominant_flip"]
        b = by_bucket[bucket]
        b["n_agent_days"] += 1
        b["classes"][d["class"]] += 1
        bc = by_bucket_class[(bucket, d["class"])]
        bc["n_agent_days"] += 1

        o = outcomes.get(key)
        if o is not None:
            b["n_with_outcome"] += 1
            b["n_events_total"] += o["n_events"]
            if o["mean_score"] is not None:
                b["outcome_scores"].append(o["mean_score"])
            if o["any_bad"]:
                b["n_bad_days"] += 1
            bc["n_with_outcome"] += 1
            if o["mean_score"] is not None:
                bc["outcome_scores"].append(o["mean_score"])
            if o["any_bad"]:
                bc["n_bad_days"] += 1

    # Compute means
    def finalize(row):
        if row["outcome_scores"]:
            row["mean_outcome_score"] = sum(row["outcome_scores"]) / len(row["outcome_scores"])
        else:
            row["mean_outcome_score"] = None
        if row["n_with_outcome"]:
            row["bad_day_rate"] = row["n_bad_days"] / row["n_with_outcome"]
        else:
            row["bad_day_rate"] = None
        row.pop("outcome_scores", None)
        return row

    for b in by_bucket.values():
        finalize(b)
    for bc in by_bucket_class.values():
        finalize(bc)

    return {"by_bucket": dict(by_bucket), "by_bucket_class": dict(by_bucket_class)}


def print_report(summary: Dict, total_agent_days: int, total_with_outcome: int) -> None:
    print()
    print("=" * 88)
    print("OUTCOME VALIDATION — do basin flips predict worse objective outcomes?")
    print("=" * 88)
    print(f"Agent-days total:                         {total_agent_days:>6}")
    print(f"Agent-days with eprocess-eligible outcome: {total_with_outcome:>6}")
    coverage = 100.0 * total_with_outcome / total_agent_days if total_agent_days else 0.0
    print(f"Outcome coverage:                          {coverage:>5.1f}%")
    print()

    print("── By dominant flip bucket (agent-day level) ──")
    header = f"{'Bucket':<22} {'N':>5} {'w/out':>6} {'mean_score':>11} {'bad_rate':>9} {'n_events':>9}  top classes"
    print(header)
    print("-" * len(header))

    # Order: high→high first (reference), then degrading buckets, then others
    order_priority = {
        "high→high": 0,
        "high→boundary": 1,
        "high→low": 2,
        "boundary→low": 3,
        "boundary→high": 4,
        "boundary→boundary": 5,
        "low→low": 6,
        "low→boundary": 7,
        "low→high": 8,
    }
    buckets = sorted(
        summary["by_bucket"].items(),
        key=lambda kv: (order_priority.get(kv[0], 99), -kv[1]["n_agent_days"]),
    )
    for name, row in buckets:
        if row["n_agent_days"] < 3 and name not in order_priority:
            continue
        score = f"{row['mean_outcome_score']:>11.3f}" if row["mean_outcome_score"] is not None else f"{'—':>11}"
        bad = f"{row['bad_day_rate']:>8.1%}" if row["bad_day_rate"] is not None else f"{'—':>9}"
        top_classes = ", ".join(f"{c}:{n}" for c, n in row["classes"].most_common(3))
        print(f"{name:<22} {row['n_agent_days']:>5} {row['n_with_outcome']:>6} "
              f"{score} {bad} {row['n_events_total']:>9}  {top_classes}")
    print()

    # Per-class breakdown for the key comparison: high→high vs degradation buckets
    print("── Per-class breakdown (key buckets only) ──")
    keys = [b for b in summary["by_bucket"] if b in {"high→high", "high→low", "high→boundary", "boundary→low"}]
    classes = sorted({k[1] for k in summary["by_bucket_class"]})
    if keys and classes:
        print(f"{'Class':<12} " + " ".join(f"{k:<14}" for k in keys))
        print("-" * (12 + 15 * len(keys)))
        for cls in classes:
            cells = []
            for k in keys:
                row = summary["by_bucket_class"].get((k, cls))
                if not row or row["n_agent_days"] == 0:
                    cells.append(f"{'—':<14}")
                    continue
                score = row["mean_outcome_score"]
                n = row["n_agent_days"]
                w = row["n_with_outcome"]
                if score is None or w == 0:
                    cells.append(f"N={n},w={w:<10}")
                else:
                    cells.append(f"{score:.2f} (N={n},w={w})")
            print(f"{cls:<12} " + " ".join(cells))
    print()


def write_csv(summary: Dict, path: str) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["bucket", "class", "n_agent_days", "n_with_outcome",
                    "mean_outcome_score", "bad_day_rate"])
        for (bucket, cls), row in sorted(summary["by_bucket_class"].items()):
            w.writerow([
                bucket, cls, row["n_agent_days"], row["n_with_outcome"],
                f"{row['mean_outcome_score']:.4f}" if row["mean_outcome_score"] is not None else "",
                f"{row['bad_day_rate']:.4f}" if row["bad_day_rate"] is not None else "",
            ])


async def main_async(args) -> int:
    print(f"[outcome-validation] connecting...", file=sys.stderr)
    conn = await asyncpg.connect(args.db_url)
    try:
        print(f"[outcome-validation] fetching state rows (window={args.window_days}d)...",
              file=sys.stderr)
        state_rows = await fetch_state_rows(conn, args.window_days)
        print(f"[outcome-validation]   {len(state_rows)} state rows with computable E",
              file=sys.stderr)

        print(f"[outcome-validation] fetching outcome aggregates (eprocess_eligible=true)...",
              file=sys.stderr)
        outcomes = await fetch_outcome_aggregates(conn, args.window_days)
        print(f"[outcome-validation]   {len(outcomes)} (agent_id, day) outcome buckets",
              file=sys.stderr)
    finally:
        await conn.close()

    print(f"[outcome-validation] classifying flips + bucketing by agent-day...",
          file=sys.stderr)
    flipped = classify_flips(state_rows)
    agent_days = bucket_agent_days(flipped)
    total_with_outcome = sum(1 for k in agent_days if k in outcomes)

    summary = summarize(agent_days, outcomes)
    print_report(summary, total_agent_days=len(agent_days),
                 total_with_outcome=total_with_outcome)

    if args.csv:
        write_csv(summary, args.csv)
        print(f"[outcome-validation] per-bucket-class CSV → {args.csv}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--db-url", type=str,
                        default="postgresql://postgres:postgres@localhost:5432/governance")
    parser.add_argument("--csv", type=str, default=None,
                        help="Write per-bucket-class CSV to this path")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
