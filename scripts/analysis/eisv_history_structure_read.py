#!/usr/bin/env python3
"""Descriptive EISV history-structure read: how much series exists, and what shape it has.

Companion to the label-free evals. ``eisv_self_predictability.py`` asks whether
the estimator predicts its own next state better than baselines;
``eisv_residual_autocorr.py`` measures how smooth the per-agent residual is.
This script asks the question upstream of both: for each identity, how much
EISV history exists at all, at what cadence, and how much of the variance in
that history sits on an hour-of-day clock versus elsewhere.

Column semantics (per the mapping comment in ``src/db/mixins/state.py``):
``state_json.E`` is E, ``integrity`` is I, ``entropy`` is **S**, and
``volatility`` is V. The column named ``entropy`` does NOT hold E; an earlier
draft of this script made exactly that mislabel, so the mapping is asserted
here where the next analyst will look first. ``coherence`` is the derived
scalar whose instrument is tagged per-row in ``state_json.coherence_form``;
rows with different forms are different instruments, so the report prints the
form mix per identity rather than silently pooling.

Reads only ``core.agent_state`` joined to identities/agents for display
labels. No outcome labels are read.

Scope limits, stated up front:

- **Identity-scoped, not agent-scoped.** Sessions mint fresh identities by
  design (co-location is not lineage), so an agent running many sessions
  appears here as many short identities. Rolling histories up by lineage is
  deliberately out of scope.
- The census counts identities with at least one measured row; identities
  that onboarded and never synced are invisible to it. The identity-side
  view, including zero-check-in buckets, is
  ``src/identity/agent_fragmentation.py``.
- A small per-identity row count does not distinguish a short-lived identity
  from a recording gap (never surfaced / not reachable / not recorded /
  genuinely short — the measurement-authority contract's four states); the
  census reports the count and rules out none of them.
- Label patterns group a cosmetic, agent-supplied field for display; durable
  harness attribution lives in ``state_json.provenance_context`` and is read
  by ``scripts/analysis/harness_census.py``.
- The hour-of-day share is computed on hourly means, an already-low-passed
  series, and within-hour averaging favors high-cadence identities; shares
  are not comparable across very different cadences.
- The census pools all epochs and epistemic classes; per-identity sections
  print the epoch and coherence-form mix so instrument changes are visible.

Interpretation guards (also printed in every report):

- Descriptive only. Nothing here is an outcome-prediction claim, and nothing
  here feeds the pre-registered read in
  ``docs/proposals/eisv-outcome-grounding-stop-rule-v0.md``.
- Raw-step smoothness at sub-hour cadence reflects the estimator's EMA
  low-pass, not agent dynamics.
- The hour-of-day share carries no phase and therefore supports no claim
  about WHICH clock (daylight, an operator's schedule, a cron); each share is
  printed beside its chance floor, the value pure noise would produce.
- Windows are regimes: numbers from one window do not transfer.

Usage:
    PYTHONPATH=. python3 scripts/analysis/eisv_history_structure_read.py
    PYTHONPATH=. python3 scripts/analysis/eisv_history_structure_read.py \
        --window-days 14 --top 3 \
        --label-like 'claude%' --label-like '%codex%' \
        --output data/analysis/eisv_history_structure.md

Env:
    GOVERNANCE_DATABASE_URL  (default: postgresql://postgres:postgres@localhost:5432/governance)
"""

from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Sequence

DEFAULT_DB_URL = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)

# Report label -> SQL select expression. See the column-semantics block in the
# module docstring; the mapping is the one src/db/mixins/state.py documents.
# Note V is a derived E-I imbalance readout and coherence is V-coupled for
# attractor-adjacent agents (docs/EISV_COMPUTATION.md); the five rows are not
# five independent axes.
DIMS = {
    "E": "NULLIF(state_json->>'E','')::float",
    "I": "integrity",
    "S": "entropy",
    "V": "volatility",
    "coherence": "coherence",
}

CENSUS_BUCKETS = ((1, 9), (10, 99), (100, 999), (1000, None))
MIN_ACF_PAIRS = 30
TOP_SHARE_COUNT = 10


# ---------------------------------------------------------------------------
# Pure math (unit-tested without a DB)
# ---------------------------------------------------------------------------


def lag_acf(values: Sequence[float], lag: int) -> float | None:
    """Positional lag-k autocorrelation; None when undefined.

    Positional: element i is paired with element i+k regardless of how far
    apart in time they are. Use only on step-indexed raw series; for hourly
    series with possible gaps use ``time_lag_acf``, which pairs by timestamp.
    Undefined for series shorter than ``lag + 3`` points or with zero
    variance.
    """
    xs = [v for v in values if v is not None and not math.isnan(v)]
    if lag < 1 or len(xs) < lag + 3:
        return None
    mean = statistics.fmean(xs)
    centered = [x - mean for x in xs]
    denom = sum(c * c for c in centered)
    if denom <= 0.0:
        return None
    num = sum(centered[i] * centered[i + lag] for i in range(len(centered) - lag))
    return num / denom


def time_lag_acf(
    keys: Sequence[datetime],
    values: Sequence[float],
    lag_hours: int,
    min_pairs: int = MIN_ACF_PAIRS,
) -> tuple[float, int] | None:
    """Autocorrelation at an exact wall-clock lag, gap-aware.

    Pairs bucket t with bucket t + lag_hours only when BOTH exist, so a
    gappy series measures the stated lag rather than "lag positions in a
    compacted list". Normalizes by the pair count and the overall variance,
    which avoids the (n - lag)/n attenuation of the positional estimator.
    Returns (acf, n_pairs), or None with fewer than ``min_pairs`` pairs or
    zero variance.
    """
    if lag_hours < 1 or len(keys) != len(values):
        return None
    by_key = dict(zip(keys, values))
    mean = statistics.fmean(values) if values else 0.0
    var = statistics.fmean([(v - mean) ** 2 for v in values]) if values else 0.0
    if var <= 0.0:
        return None
    pairs = []
    for k, v in by_key.items():
        other = by_key.get(_add_hours(k, lag_hours))
        if other is not None:
            pairs.append((v - mean) * (other - mean))
    if len(pairs) < min_pairs:
        return None
    return sum(pairs) / (len(pairs) * var), len(pairs)


def _add_hours(ts: datetime, hours: int) -> datetime:
    from datetime import timedelta

    return ts + timedelta(hours=hours)


def hour_of_day_variance_share(hours: Sequence[int], values: Sequence[float]) -> float | None:
    """Fraction of hourly-mean variance explained by hour-of-day.

    ``hours`` are bucket labels 0-23 paired with ``values`` (hourly means).
    Returns the size-weighted between-hour variance over total variance
    (in-sample eta squared), or None when total variance is zero or fewer
    than two distinct hours are present. Compare against
    ``hour_of_day_chance_floor`` before reading a share as structure: an
    unadjusted eta squared is positive on pure noise.
    """
    if len(hours) != len(values) or len(values) < 2:
        return None
    total_mean = statistics.fmean(values)
    total_var = statistics.fmean([(v - total_mean) ** 2 for v in values])
    if total_var <= 0.0:
        return None
    by_hour: dict[int, list[float]] = defaultdict(list)
    for h, v in zip(hours, values):
        by_hour[h].append(v)
    if len(by_hour) < 2:
        return None
    between = statistics.fmean(
        [(statistics.fmean(by_hour[h]) - total_mean) ** 2 for h in hours]
    )
    return between / total_var


def hour_of_day_chance_floor(hours: Sequence[int]) -> float | None:
    """Expected hour-of-day share of pure noise: (k - 1) / (n - 1).

    k is the number of populated hour bins, n the number of hourly means.
    A share near or below this floor is indistinguishable from noise, and a
    share can only be read as clock structure to the extent it exceeds it.
    (Independence approximation: autocorrelated hourly means make the true
    floor higher, so this is a lower bound on skepticism, not an upper one.)
    """
    n = len(hours)
    if n < 2:
        return None
    k = len(set(hours))
    if k < 2:
        return None
    return (k - 1) / (n - 1)


def hourly_means(
    timestamps: Sequence[datetime], values: Sequence[float]
) -> tuple[list[datetime], list[float]]:
    """Bucket a series to hourly means keyed by wall-clock hour.

    Empty hours are dropped (not interpolated). ``time_lag_acf`` pairs by
    timestamp, so downstream ACFs remain honest about gaps.
    """
    buckets: dict[datetime, list[float]] = defaultdict(list)
    for ts, v in zip(timestamps, values):
        if v is None:
            continue
        buckets[ts.replace(minute=0, second=0, microsecond=0)].append(v)
    keys = sorted(buckets)
    return keys, [statistics.fmean(buckets[k]) for k in keys]


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile of an ascending-sorted sequence."""
    if not sorted_values:
        raise ValueError("percentile of empty sequence")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    frac = pos - lo
    return float(sorted_values[lo]) * (1 - frac) + float(sorted_values[hi]) * frac


@dataclass
class CensusRow:
    identity_id: int
    label: str | None
    n: int
    span_seconds: float


@dataclass
class CensusSummary:
    identities: int
    total_rows: int
    n_p50: float
    n_p90: float
    n_max: int
    span_hours_p50: float
    top_share: float
    buckets: dict[str, int] = field(default_factory=dict)


def summarize_census(rows: Sequence[CensusRow]) -> CensusSummary | None:
    """Distribution of history length over identities with measured rows."""
    if not rows:
        return None
    ns = sorted(r.n for r in rows)
    spans = sorted(r.span_seconds / 3600.0 for r in rows)
    total = sum(ns)
    top = sum(ns[-TOP_SHARE_COUNT:])
    buckets: dict[str, int] = {}
    for lo, hi in CENSUS_BUCKETS:
        name = f"{lo}+" if hi is None else f"{lo}-{hi}"
        buckets[name] = sum(1 for r in rows if r.n >= lo and (hi is None or r.n <= hi))
    return CensusSummary(
        identities=len(rows),
        total_rows=total,
        n_p50=percentile(ns, 0.5),
        n_p90=percentile(ns, 0.9),
        n_max=ns[-1],
        span_hours_p50=percentile(spans, 0.5),
        top_share=top / total if total else 0.0,
        buckets=buckets,
    )


def sql_like(text: str, pattern: str) -> bool:
    """Case-sensitive SQL LIKE semantics for % and _ wildcards.

    Case-sensitive to match what ``WHERE label LIKE <pattern>`` would return
    in Postgres, so a reader can reproduce a subgroup count with the obvious
    query.
    """
    import re

    regex = "".join(
        ".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern
    )
    return re.fullmatch(regex, text) is not None


# ---------------------------------------------------------------------------
# Report assembly (DB access stays inside main)
# ---------------------------------------------------------------------------


def _fmt(x: float | None, nd: int = 4) -> str:
    if x is None:
        return "n/a"
    return f"{x:.{nd}f}"


def build_report(
    *,
    census_all: CensusSummary | None,
    census_patterns: dict[str, CensusSummary | None],
    series_stats: list[dict],
    window_days: int,
    top: int,
    generated_at: str,
) -> str:
    lines: list[str] = []
    lines.append("# EISV history-structure read (descriptive)")
    lines.append("")
    lines.append(f"Generated: {generated_at} · window: last {window_days} days")
    lines.append("")
    lines.append(
        "Deciding standards, chosen not derived: analysis window "
        f"{window_days}d; per-identity structure reported for the top {top} "
        f"identities by in-window row count (a most-rows selection, so the "
        f"section describes the deepest histories, not typical ones); "
        f"time-lag ACFs need >= {MIN_ACF_PAIRS} timestamp-matched pairs."
    )
    lines.append("")
    lines.append("Interpretation guards:")
    lines.append("- Descriptive only; no outcome labels read; not a stop-rule input.")
    lines.append(
        "- Raw-step smoothness at sub-hour cadence is the estimator's EMA "
        "low-pass, not agent dynamics."
    )
    lines.append(
        "- Hour-of-day share carries no phase: it cannot say WHICH clock "
        "(daylight, an operator schedule, a cron). Read each share against "
        "its chance floor; near or below the floor is noise."
    )
    lines.append(
        "- Identity-scoped: sessions mint fresh identities by design, so this "
        "is not an agent- or lineage-level census."
    )
    lines.append("- Windows are regimes; these numbers do not transfer across windows.")
    lines.append("")

    lines.append("## History-length census (all time, non-synthetic rows)")
    lines.append("")
    if census_all is None:
        lines.append("No rows found.")
    else:
        c = census_all
        lines.append(
            f"{c.identities} identities with measured rows · {c.total_rows} state "
            f"rows · per-identity rows p50={c.n_p50:.0f} p90={c.n_p90:.0f} "
            f"max={c.n_max} · span p50={c.span_hours_p50:.2f}h · top "
            f"{TOP_SHARE_COUNT} identities hold {c.top_share:.1%} of rows"
        )
        lines.append("")
        lines.append("| rows-per-identity bucket | identities |")
        lines.append("|---|---|")
        for name, count in c.buckets.items():
            lines.append(f"| {name} | {count} |")
        lines.append("")
        lines.append(
            "Census caveats: identities with zero measured rows are invisible "
            "here (see src/identity/agent_fragmentation.py for that side); all "
            "epochs and epistemic classes are pooled; a small count does not "
            "distinguish a short-lived identity from a recording gap."
        )
    lines.append("")
    for pattern, summary in census_patterns.items():
        lines.append(
            f"### Label pattern `{pattern}` (SQL LIKE, case-sensitive; cosmetic "
            "field, display grouping only)"
        )
        lines.append("")
        if summary is None:
            lines.append("No matching identities.")
        else:
            lines.append(
                f"{summary.identities} identities · rows p50={summary.n_p50:.0f} "
                f"p90={summary.n_p90:.0f} max={summary.n_max} · "
                f"span p50={summary.span_hours_p50:.2f}h"
            )
        lines.append("")
        lines.append(
            "Labels are agent-supplied and unvalidated; durable harness "
            "attribution is scripts/analysis/harness_census.py. A small median "
            "history means per-identity longitudinal statistics are mostly "
            "undefined for this population; it establishes nothing about the "
            "agents behind the labels."
        )
        lines.append("")

    lines.append(f"## Temporal structure, top {top} identities by in-window rows")
    lines.append("")
    if not series_stats:
        lines.append("No identity had enough in-window rows.")
    for stat in series_stats:
        lines.append(
            f"### identity {stat['identity_id']}"
            + (f" ({stat['label']})" if stat.get("label") else "")
        )
        lines.append("")
        lines.append(
            f"n={stat['n']} · cadence p50={stat['gap_p50_s']:.0f}s "
            f"p90={stat['gap_p90_s']:.0f}s · max gap={stat['gap_max_h']:.1f}h · "
            f"hourly buckets {stat['hourly_n']} of <= {window_days * 24 + 1}"
        )
        lines.append(
            f"provenance mix: epochs {stat['epochs']} · coherence_form "
            f"{stat['coherence_forms']}"
        )
        lines.append("")
        lines.append(
            "| dim | n | mean | p50 | sd | raw ACF lag1 | 24h ACF (pairs) | "
            "hour-of-day share | chance floor |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for dim_label, d in stat["dims"].items():
            acf24 = d["acf_24h"]
            acf24_s = f"{acf24[0]:.2f} ({acf24[1]})" if acf24 else "n/a"
            lines.append(
                f"| {dim_label} | {d['n']} | {_fmt(d['mean'])} | {_fmt(d['p50'])} | "
                f"{_fmt(d['sd'])} | {_fmt(d['acf_raw1'], 2)} | {acf24_s} | "
                f"{_fmt(d['hod_share'], 3)} | {_fmt(d['hod_floor'], 3)} |"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--window-days", type=int, default=14)
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument(
        "--label-like",
        action="append",
        default=[],
        help=(
            "SQL LIKE pattern (case-sensitive) to summarize as a census "
            "subgroup (repeatable). Display grouping over a cosmetic field; "
            "the analysis never branches on labels."
        ),
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    import psycopg2  # local import: keeps the pure-math surface importable without a DB driver

    conn = psycopg2.connect(args.db_url)
    # One REPEATABLE READ read-only transaction: every query sees the same
    # snapshot, so the census, top selection, and per-identity series describe
    # one moment even while check-ins continue.
    conn.set_session(isolation_level="REPEATABLE READ", readonly=True, autocommit=False)
    cur = conn.cursor()
    cur.execute("SELECT now()")
    cutoff_now = cur.fetchone()[0]

    cur.execute(
        """
        SELECT s.identity_id, a.label, count(*) AS n,
               EXTRACT(EPOCH FROM (max(s.recorded_at) - min(s.recorded_at))) AS span_s
        FROM core.agent_state s
        JOIN core.identities i ON i.identity_id = s.identity_id
        LEFT JOIN core.agents a ON a.id = i.agent_id
        WHERE s.synthetic = false
        GROUP BY s.identity_id, a.label
        """
    )
    census_rows = [CensusRow(r[0], r[1], int(r[2]), float(r[3])) for r in cur.fetchall()]
    census_all = summarize_census(census_rows)

    census_patterns: dict[str, CensusSummary | None] = {}
    for pattern in args.label_like:
        matching = [
            r for r in census_rows if r.label is not None and sql_like(r.label, pattern)
        ]
        census_patterns[pattern] = summarize_census(matching)

    cur.execute(
        """
        SELECT identity_id, count(*) AS n
        FROM core.agent_state
        WHERE synthetic = false AND recorded_at >= %s - make_interval(days => %s)
        GROUP BY identity_id
        ORDER BY n DESC
        """,
        (cutoff_now, args.window_days),
    )
    in_window = cur.fetchall()
    top_ids = [r[0] for r in in_window[: args.top]]
    labels = {r.identity_id: r.label for r in census_rows}

    dim_select = ", ".join(DIMS.values())
    series_stats: list[dict] = []
    for identity_id in top_ids:
        cur.execute(
            f"""
            SELECT recorded_at, epoch, state_json->>'coherence_form', {dim_select}
            FROM core.agent_state
            WHERE synthetic = false AND identity_id = %s
              AND recorded_at >= %s - make_interval(days => %s)
            ORDER BY recorded_at
            """,
            (identity_id, cutoff_now, args.window_days),
        )
        rows = cur.fetchall()
        if len(rows) < 3:
            continue
        timestamps = [r[0] for r in rows]
        gaps = sorted(
            (b - a).total_seconds() for a, b in zip(timestamps, timestamps[1:])
        )
        epochs = sorted({r[1] for r in rows})
        coherence_forms = dict(Counter(r[2] or "untagged" for r in rows))
        dims: dict[str, dict] = {}
        hourly_n = 0
        for offset, dim_label in enumerate(DIMS):
            column = [r[3 + offset] for r in rows]
            values = [float(v) for v in column if v is not None]
            if len(values) < 2:
                continue
            hour_keys, hvals = hourly_means(
                timestamps, [float(v) if v is not None else None for v in column]
            )
            hourly_n = max(hourly_n, len(hour_keys))
            dims[dim_label] = {
                "n": len(values),
                "mean": statistics.fmean(values),
                "p50": percentile(sorted(values), 0.5),
                "sd": statistics.stdev(values),
                "acf_raw1": lag_acf(values, 1),
                "acf_24h": time_lag_acf(hour_keys, hvals, 24),
                "hod_share": hour_of_day_variance_share(
                    [k.hour for k in hour_keys], hvals
                ),
                "hod_floor": hour_of_day_chance_floor([k.hour for k in hour_keys]),
            }
        series_stats.append(
            {
                "identity_id": identity_id,
                "label": labels.get(identity_id),
                "n": len(rows),
                "gap_p50_s": percentile(gaps, 0.5) if gaps else 0.0,
                "gap_p90_s": percentile(gaps, 0.9) if gaps else 0.0,
                "gap_max_h": (gaps[-1] / 3600.0) if gaps else 0.0,
                "hourly_n": hourly_n,
                "epochs": epochs,
                "coherence_forms": coherence_forms,
                "dims": dims,
            }
        )

    conn.rollback()
    conn.close()

    report = build_report(
        census_all=census_all,
        census_patterns=census_patterns,
        series_stats=series_stats,
        window_days=args.window_days,
        top=args.top,
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report)
        print(f"wrote {args.output}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
