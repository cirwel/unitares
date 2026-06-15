#!/usr/bin/env python3
"""Phase 1 mirror-effectiveness re-eval (mirror-effectiveness-measurement-v0).

Reads the Phase 0 ``mirror_signal.emit`` instrumentation and asks, per numeric
signal type, whether *surfacing* the signal (response_mode == "mirror") moves
the agent's metric in the intended direction more than a matched shadow control
(agents who computed the same signal under a non-mirror mode and never saw it).

Estimator — surfaced-vs-shadow value trend
------------------------------------------
Each ``mirror_signal.emit`` carries the metric ``value`` at the firing check-in
(autopilot: complexity/confidence variance; complexity_divergence: |divergence|).
For each (agent, signal_type) with >= 2 firings in the window we take the trend
``last_value - first_value`` across its firings, and assign the agent to the
**surfaced** or **shadow** cohort by whether its *first* firing was surfaced
(the moment we would first have nudged it). We then compare cohort mean trends.

Intended direction per signal:
  - autopilot_complexity / autopilot_confidence : variance should rise (the
    agent re-introduces real variance) -> direction +1.
  - complexity_divergence : |divergence| should fall -> direction -1.

A signal is ``effective`` when the surfaced cohort's mean trend beats the shadow
cohort's by at least ``--min-effect`` in the intended direction, with both
cohorts at >= ``--min-agents``. Otherwise ``no_measurable_effect`` (enough data,
no edge) or ``insufficient_data`` (cohorts too small).

Threshold discontinuity (RDD-flavored, Phase 0.5)
-------------------------------------------------
Phase 0.5 also logs the just-above-threshold NON-fired band, so a second,
mode-independent estimator runs at the ``variance < 0.005`` cutoff: agents whose
first near-threshold check-in fired (just below, *treated*) vs did not (just
above, *control*) are compared on their subsequent value trend. The contrast is
a mean difference at the cutoff, not a local-linear RDD fit — it corroborates
surfaced-vs-shadow, it does not replace it. Only the autopilot signals have the
non-fired band logged; complexity_divergence fires ABOVE its threshold and is
excluded from the RDD.

Honest scope (carry into any Phase 2 retire/keep call)
------------------------------------------------------
* Per-check-in complexity is not persisted in ``core.agent_state`` (migration
  040), so the join the proposal sketched against agent_state is unavailable;
  the emission log is the panel instead. The trend therefore samples only
  firing check-ins, and a censored recovery (signal simply stops firing) is not
  captured by the value trend — read "no_measurable_effect" as "no edge in the
  firing-trend view," not as proof of no effect.
* Cohort assignment is by observed mode, not randomized; verbosity preference
  may correlate with calibration quality. Treat a positive read as associational.

Data source: Postgres ``audit.events`` by default (queryable, like
``section_129_reeval.py``); ``--jsonl PATH`` reads the durable local truth log
(``data/audit_log.jsonl``) instead, since DB audit writes are fire-and-forget.

Exit codes:
  0 — ran; at least one signal produced a verdict other than insufficient_data
  2 — ran, but every signal was insufficient_data (collect a longer window)

Usage:
    python3 scripts/dev/mirror_effectiveness_reeval.py
    python3 scripts/dev/mirror_effectiveness_reeval.py --json
    python3 scripts/dev/mirror_effectiveness_reeval.py --jsonl data/audit_log.jsonl
    python3 scripts/dev/mirror_effectiveness_reeval.py --start 2026-06-15 --days 14
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

# Intended-improvement direction per signal type (+1 = higher metric is better).
SIGNAL_DIRECTION = {
    "autopilot_complexity": +1,
    "autopilot_confidence": +1,
    "complexity_divergence": -1,
}

DEFAULT_DAYS = 14
DEFAULT_MIN_AGENTS = 5
DEFAULT_MIN_EFFECT = 0.0


# ---------------------------------------------------------------------------
# Pure analysis (no DB / no IO — unit-tested directly)
# ---------------------------------------------------------------------------

@dataclass
class CohortStat:
    n_agents: int
    mean_trend: Optional[float]


@dataclass
class SignalVerdict:
    signal_type: str
    direction: int
    surfaced: CohortStat
    shadow: CohortStat
    improvement: Optional[float]  # surfaced advantage in the intended direction
    verdict: str  # effective | no_measurable_effect | insufficient_data
    detail: dict = field(default_factory=dict)


def flatten_emissions(events: list[dict]) -> list[dict]:
    """Explode raw mirror_signal.emit events into one row per (agent, signal).

    Each event: {agent_id, ts, update_index, surfaced, signals:[{signal_type,
    value, ...}]}. Returns rows {agent_id, order_key, surfaced, signal_type,
    value}. ``order_key`` is update_index when present (monotonic per agent),
    else the timestamp, so firings sort stably within an agent.
    """
    rows: list[dict] = []
    for ev in events:
        agent_id = ev.get("agent_id")
        if not agent_id:
            continue
        order_key = ev.get("update_index")
        if order_key is None:
            order_key = ev.get("ts")
        surfaced = bool(ev.get("surfaced"))
        for sig in ev.get("signals") or []:
            stype = sig.get("signal_type")
            value = sig.get("value")
            if stype is None or value is None:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            rows.append({
                "agent_id": agent_id,
                "order_key": order_key,
                "surfaced": surfaced,
                "signal_type": stype,
                "value": value,
                # Phase 0.5: legacy Phase 0 rows had no fired flag and only ever
                # logged fired check-ins, so default True.
                "fired": bool(sig.get("fired", True)),
            })
    return rows


def _agent_trends(rows: list[dict], signal_type: str) -> dict[str, dict]:
    """Per-agent firing trend for one signal type (FIRED firings only).

    Returns {agent_id: {"trend": last-first, "first_surfaced": bool}} for agents
    with >= 2 firings of the signal. Sorted by order_key (None sorts first but
    is rare — only when update_index was absent). Phase 0.5 non-fired control
    rows are excluded here so the surfaced-vs-shadow estimator keeps its Phase 1
    "trajectory across actual signal events" meaning; they feed the RDD instead.
    """
    by_agent: dict[str, list[dict]] = {}
    for r in rows:
        if r["signal_type"] != signal_type or not r.get("fired", True):
            continue
        by_agent.setdefault(r["agent_id"], []).append(r)

    trends: dict[str, dict] = {}
    for agent_id, seq in by_agent.items():
        seq_sorted = sorted(seq, key=lambda r: (r["order_key"] is None, r["order_key"]))
        if len(seq_sorted) < 2:
            continue
        trend = seq_sorted[-1]["value"] - seq_sorted[0]["value"]
        trends[agent_id] = {
            "trend": trend,
            "first_surfaced": bool(seq_sorted[0]["surfaced"]),
        }
    return trends


def _mean(xs: list[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def evaluate_signal(
    rows: list[dict],
    signal_type: str,
    *,
    min_agents: int = DEFAULT_MIN_AGENTS,
    min_effect: float = DEFAULT_MIN_EFFECT,
) -> SignalVerdict:
    direction = SIGNAL_DIRECTION.get(signal_type, +1)
    trends = _agent_trends(rows, signal_type)

    surfaced_trends = [t["trend"] for t in trends.values() if t["first_surfaced"]]
    shadow_trends = [t["trend"] for t in trends.values() if not t["first_surfaced"]]

    surfaced_mean = _mean(surfaced_trends)
    shadow_mean = _mean(shadow_trends)

    surfaced_stat = CohortStat(len(surfaced_trends), surfaced_mean)
    shadow_stat = CohortStat(len(shadow_trends), shadow_mean)

    # Insufficient data: either cohort below the floor.
    if len(surfaced_trends) < min_agents or len(shadow_trends) < min_agents:
        return SignalVerdict(
            signal_type=signal_type,
            direction=direction,
            surfaced=surfaced_stat,
            shadow=shadow_stat,
            improvement=None,
            verdict="insufficient_data",
            detail={"min_agents": min_agents,
                    "agents_with_trend": len(trends)},
        )

    # Surfaced advantage, oriented so positive = better in the intended direction.
    improvement = direction * (surfaced_mean - shadow_mean)
    verdict = "effective" if improvement >= min_effect and improvement > 0 else "no_measurable_effect"
    return SignalVerdict(
        signal_type=signal_type,
        direction=direction,
        surfaced=surfaced_stat,
        shadow=shadow_stat,
        improvement=improvement,
        verdict=verdict,
        detail={"min_agents": min_agents, "min_effect": min_effect,
                "agents_with_trend": len(trends)},
    )


def evaluate_all(rows: list[dict], *, min_agents: int, min_effect: float) -> list[SignalVerdict]:
    return [
        evaluate_signal(rows, st, min_agents=min_agents, min_effect=min_effect)
        for st in SIGNAL_DIRECTION
    ]


# Signals with a `value < threshold` cutoff, for which Phase 0.5 logs the
# just-above-threshold non-fired band — the only signals an RDD-flavored
# threshold contrast can run on. (complexity_divergence fires ABOVE its
# threshold and has no non-fired band logged, so it is excluded.)
RDD_SIGNALS = ("autopilot_complexity", "autopilot_confidence")


@dataclass
class DiscontinuityVerdict:
    signal_type: str
    direction: int
    treated_n: int           # first obs just below threshold (got the nudge)
    control_n: int           # first obs just above threshold (Phase 0.5 control)
    treated_mean_outcome: Optional[float]
    control_mean_outcome: Optional[float]
    discontinuity: Optional[float]  # treated advantage in the intended direction
    verdict: str  # local_effect | no_local_effect | insufficient_data
    detail: dict = field(default_factory=dict)


def threshold_discontinuity(
    rows: list[dict],
    signal_type: str,
    *,
    min_per_side: int = DEFAULT_MIN_AGENTS,
) -> DiscontinuityVerdict:
    """RDD-flavored local contrast at the firing threshold.

    Assignment is by the agent's FIRST near-threshold observation: *treated* if
    it fired (value < threshold), *control* if it did not (value in the Phase 0.5
    just-above band). Outcome is the subsequent trend (next observation's value
    minus the first). The discontinuity is the treated-minus-control outcome
    difference oriented to the intended direction. This is a mean-difference
    contrast at the cutoff, not a local-linear RDD fit — treat it as the second,
    mode-independent estimator that corroborates surfaced-vs-shadow, not proof.
    """
    direction = SIGNAL_DIRECTION.get(signal_type, +1)
    by_agent: dict[str, list[dict]] = {}
    for r in rows:
        if r["signal_type"] != signal_type:
            continue
        by_agent.setdefault(r["agent_id"], []).append(r)

    treated_outcomes: list[float] = []
    control_outcomes: list[float] = []
    for seq in by_agent.values():
        seq_sorted = sorted(seq, key=lambda r: (r["order_key"] is None, r["order_key"]))
        if len(seq_sorted) < 2:
            continue
        first, nxt = seq_sorted[0], seq_sorted[1]
        outcome = nxt["value"] - first["value"]
        if first.get("fired", True):
            treated_outcomes.append(outcome)
        else:
            control_outcomes.append(outcome)

    treated_mean = _mean(treated_outcomes)
    control_mean = _mean(control_outcomes)

    if len(treated_outcomes) < min_per_side or len(control_outcomes) < min_per_side:
        return DiscontinuityVerdict(
            signal_type=signal_type, direction=direction,
            treated_n=len(treated_outcomes), control_n=len(control_outcomes),
            treated_mean_outcome=treated_mean, control_mean_outcome=control_mean,
            discontinuity=None, verdict="insufficient_data",
            detail={"min_per_side": min_per_side},
        )

    discontinuity = direction * (treated_mean - control_mean)
    verdict = "local_effect" if discontinuity > 0 else "no_local_effect"
    return DiscontinuityVerdict(
        signal_type=signal_type, direction=direction,
        treated_n=len(treated_outcomes), control_n=len(control_outcomes),
        treated_mean_outcome=treated_mean, control_mean_outcome=control_mean,
        discontinuity=discontinuity, verdict=verdict,
        detail={"min_per_side": min_per_side},
    )


def evaluate_rdd(rows: list[dict], *, min_per_side: int) -> list[DiscontinuityVerdict]:
    return [
        threshold_discontinuity(rows, st, min_per_side=min_per_side)
        for st in RDD_SIGNALS
    ]


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

def _load_from_postgres(start: datetime, end: datetime) -> list[dict]:
    import psycopg2  # type: ignore

    dsn = os.environ.get(
        "GOVERNANCE_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/governance",
    )
    events: list[dict] = []
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            # details maps flat to audit.events.payload (no nesting — unlike
            # coordination_failure). agent_id and ts are columns.
            cur.execute(
                """
                SELECT agent_id, ts,
                       payload->>'update_index',
                       payload->>'surfaced',
                       payload->'signals'
                FROM audit.events
                WHERE event_type = 'mirror_signal.emit'
                  AND ts >= %s AND ts < %s
                ORDER BY agent_id, ts
                """,
                (start, end),
            )
            for agent_id, ts, update_index, surfaced, signals in cur.fetchall():
                events.append({
                    "agent_id": agent_id,
                    "ts": ts.isoformat() if hasattr(ts, "isoformat") else ts,
                    "update_index": int(update_index) if update_index is not None else None,
                    "surfaced": str(surfaced).lower() == "true",
                    "signals": signals or [],
                })
    return events


def _load_from_jsonl(path: str, start: datetime, end: datetime) -> list[dict]:
    events: list[dict] = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or "mirror_signal.emit" not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("event_type") != "mirror_signal.emit":
                continue
            ts_raw = entry.get("timestamp")
            ts = None
            if ts_raw:
                try:
                    ts = datetime.fromisoformat(ts_raw)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except ValueError:
                    ts = None
            if ts is not None and not (start <= ts < end):
                continue
            details = entry.get("details", {}) or {}
            events.append({
                "agent_id": entry.get("agent_id"),
                "ts": ts_raw,
                "update_index": details.get("update_index"),
                "surfaced": bool(details.get("surfaced")),
                "signals": details.get("signals") or [],
            })
    return events


# ---------------------------------------------------------------------------
# Render / CLI
# ---------------------------------------------------------------------------

def render_text(window_start: str, window_end: str, source: str,
                verdicts: list[SignalVerdict],
                discontinuities: list[DiscontinuityVerdict], total_events: int) -> str:
    lines = [
        "Mirror effectiveness re-eval (Phases 1 + RDD)",
        f"Window: {window_start} -> {window_end}",
        f"Source: {source}   mirror_signal.emit events: {total_events}",
        "",
        "== Surfaced-vs-shadow value trend ==",
    ]
    for v in verdicts:
        arrow = "higher=better" if v.direction > 0 else "lower=better"
        lines.append(f"[{v.verdict}] {v.signal_type} ({arrow})")
        lines.append(
            f"    surfaced: n={v.surfaced.n_agents} "
            f"mean_trend={_fmt(v.surfaced.mean_trend)}"
        )
        lines.append(
            f"    shadow:   n={v.shadow.n_agents} "
            f"mean_trend={_fmt(v.shadow.mean_trend)}"
        )
        if v.improvement is not None:
            lines.append(f"    surfaced advantage (intended dir): {_fmt(v.improvement)}")
        for k, val in v.detail.items():
            lines.append(f"    {k}: {val}")
        lines.append("")

    lines.append("== Threshold discontinuity (RDD-flavored, Phase 0.5) ==")
    for d in discontinuities:
        arrow = "higher=better" if d.direction > 0 else "lower=better"
        lines.append(f"[{d.verdict}] {d.signal_type} ({arrow})")
        lines.append(
            f"    treated (fired<thr): n={d.treated_n} "
            f"mean_outcome={_fmt(d.treated_mean_outcome)}"
        )
        lines.append(
            f"    control (near-miss): n={d.control_n} "
            f"mean_outcome={_fmt(d.control_mean_outcome)}"
        )
        if d.discontinuity is not None:
            lines.append(f"    discontinuity (intended dir): {_fmt(d.discontinuity)}")
        for k, val in d.detail.items():
            lines.append(f"    {k}: {val}")
        lines.append("")
    lines.append(
        "Note: the RDD is a mean-difference contrast at the cutoff, not a "
        "local-linear fit; it corroborates surfaced-vs-shadow, it is not proof."
    )
    return "\n".join(lines)


def _fmt(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:.5f}"


def run(*, start: datetime, end: datetime, source: str, jsonl_path: Optional[str],
        min_agents: int, min_effect: float):
    if source == "jsonl":
        events = _load_from_jsonl(jsonl_path, start, end)
    else:
        events = _load_from_postgres(start, end)
    rows = flatten_emissions(events)
    verdicts = evaluate_all(rows, min_agents=min_agents, min_effect=min_effect)
    discontinuities = evaluate_rdd(rows, min_per_side=min_agents)
    return events, verdicts, discontinuities


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", type=lambda s: date.fromisoformat(s),
                        default=None, help="window start date (UTC). Default: --days back from now.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"window length in days. Default {DEFAULT_DAYS}")
    parser.add_argument("--jsonl", default=None,
                        help="read from this JSONL audit-truth file instead of Postgres")
    parser.add_argument("--min-agents", type=int, default=DEFAULT_MIN_AGENTS,
                        help=f"min agents per cohort before a verdict. Default {DEFAULT_MIN_AGENTS}")
    parser.add_argument("--min-effect", type=float, default=DEFAULT_MIN_EFFECT,
                        help="min surfaced advantage to call 'effective'. Default 0.0")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args()

    if args.start is not None:
        start = datetime.combine(args.start, datetime.min.time(), tzinfo=timezone.utc)
        end = start + timedelta(days=args.days)
    else:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=args.days)

    source = "jsonl" if args.jsonl else "postgres"
    events, verdicts, discontinuities = run(
        start=start, end=end, source=source, jsonl_path=args.jsonl,
        min_agents=args.min_agents, min_effect=args.min_effect)

    if args.json:
        print(json.dumps({
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "source": source,
            "total_events": len(events),
            "signals": [asdict(v) for v in verdicts],
            "threshold_discontinuity": [asdict(d) for d in discontinuities],
        }, indent=2, default=str))
    else:
        print(render_text(start.isoformat(), end.isoformat(), source,
                          verdicts, discontinuities, len(events)))

    all_verdicts = [v.verdict for v in verdicts] + [d.verdict for d in discontinuities]
    if all(v == "insufficient_data" for v in all_verdicts):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
