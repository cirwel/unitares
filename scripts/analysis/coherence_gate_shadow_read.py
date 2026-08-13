"""Soak read + positive control for the coherence gate shadow.

This is step 4 of `docs/proposals/coherence-proprioceptive-thresholds-v0.md`
section 6: after the `coherence_gate_shadow` instrument (built in
`src/coherence_gate_shadow.py`, flag `UNITARES_COHERENCE_GATE_SHADOW`) has
soaked on real traffic, re-read the gate crossing counts — and interpret
silence honestly. A proprioceptive gate that never fires during the soak is an
*unfair zero* (the lever is untested, not disproven) unless a positive control
shows the instrument fires when a genuine excursion is injected.

Two modes:

  * soak read (default): pull `coherence_gate_shadow` events from
    `audit.events` (or a JSONL export via --input) and report volume,
    eligibility, scale provenance, per-agent deviation structure, firing
    rates at the candidate k tiers, a k sweep, and agreement with the
    attributable fleet gate firings. v1 and v2 statistic versions are never
    pooled; agreement uses only rows whose fleet cause is attributable
    (`agrees` is tri-state by design).

  * --positive-control: no DB. Drives the REAL `evaluate()` code path with
    synthetic BehavioralEISV stand-ins carrying injected V excursions at each
    candidate tier, plus the floor-scale and immature-baseline edge cases, and
    reports PASS/FAIL per scenario. This is the evidence that soak silence is
    informative. The same scenarios run in CI via
    `tests/test_coherence_gate_shadow_read.py`.

Measurement only: this script changes no flag, threshold, verdict, or weight.
Choosing k values remains a recorded policy call (proposal section 4).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.coherence_gate_shadow import (  # noqa: E402
    K_BLOCK,
    K_FLOOR,
    K_PAUSE,
    RECENT_MIN_SAMPLES,
    STATISTIC_VERSION,
    evaluate,
)

DEFAULT_DB_URL = "postgresql://localhost:5432/governance"
DEFAULT_WINDOW_DAYS = 14
K_SWEEP = [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0]

FETCH_QUERY = """
    SELECT agent_id, ts, payload
    FROM audit.events
    WHERE event_type = 'coherence_gate_shadow'
      AND ts >= now() - ($1::int * interval '1 day')
    ORDER BY ts
"""


# ---------------------------------------------------------------------------
# Soak read
# ---------------------------------------------------------------------------


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return math.nan
    idx = q * (len(sorted_values) - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return sorted_values[lo]
    frac = idx - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def summarize(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate shadow rows into the soak report. Pure; rows are payload
    dicts with an ``agent_id`` key (DB or JSONL provenance is identical)."""
    rows = list(rows)
    by_version: Counter = Counter(
        (row.get("statistic_version") or "unknown") for row in rows
    )

    # Everything below is v2-only; v1 used a one-sided statistic that the
    # proposal explicitly superseded, so pooling would corrupt every rate.
    v2 = [row for row in rows if row.get("statistic_version") == STATISTIC_VERSION]
    eligible = [row for row in v2 if row.get("eligible")]
    ineligible_reasons: Counter = Counter(
        (row.get("eligibility_reason") or "unknown")
        for row in v2
        if not row.get("eligible")
    )
    scale_sources: Counter = Counter(
        (row.get("scale_source") or "unknown") for row in eligible
    )

    magnitudes = sorted(
        float(row["v_deviation_magnitude"])
        for row in eligible
        if row.get("v_deviation_magnitude") is not None
    )
    would_actions: Counter = Counter(
        (row.get("would_action") or "none") for row in eligible
    )

    k_sweep = {
        k: sum(1 for m in magnitudes if m >= k) for k in K_SWEEP
    }

    per_agent: Dict[str, Dict[str, Any]] = {}
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        grouped[row.get("agent_id") or "unknown"].append(row)
    for agent_id, agent_rows in grouped.items():
        mags = sorted(
            float(r["v_deviation_magnitude"])
            for r in agent_rows
            if r.get("v_deviation_magnitude") is not None
        )
        per_agent[agent_id] = {
            "n_eligible": len(agent_rows),
            "p50": round(_percentile(mags, 0.50), 3) if mags else None,
            "p90": round(_percentile(mags, 0.90), 3) if mags else None,
            "p99": round(_percentile(mags, 0.99), 3) if mags else None,
            "max": round(mags[-1], 3) if mags else None,
            "fired_pause": sum(1 for m in mags if m >= K_PAUSE),
            "fired_block": sum(1 for m in mags if m >= K_BLOCK),
            "fired_floor": sum(1 for m in mags if m >= K_FLOOR),
        }

    # Agreement: only rows where BOTH sides are attributable. `agrees` is
    # tri-state; None rows are counted but never enter the rate.
    attributable = [row for row in eligible if row.get("agrees") is not None]
    agree_count = sum(1 for row in attributable if row["agrees"])
    divergence_by_family: Counter = Counter(
        (row.get("fleet_gate_family") or "unknown")
        for row in attributable
        if not row["agrees"]
    )

    fired_any = any(m >= K_PAUSE for m in magnitudes)
    return {
        "n_rows": len(rows),
        "by_statistic_version": dict(by_version),
        "n_v2": len(v2),
        "n_eligible": len(eligible),
        "ineligible_reasons": dict(ineligible_reasons),
        "scale_sources": dict(scale_sources),
        "would_actions": dict(would_actions),
        "magnitude_percentiles": {
            "p50": round(_percentile(magnitudes, 0.50), 3) if magnitudes else None,
            "p90": round(_percentile(magnitudes, 0.90), 3) if magnitudes else None,
            "p99": round(_percentile(magnitudes, 0.99), 3) if magnitudes else None,
            "max": round(magnitudes[-1], 3) if magnitudes else None,
        },
        "k_sweep": k_sweep,
        "per_agent": per_agent,
        "agreement": {
            "n_attributable": len(attributable),
            "n_unattributable": len(eligible) - len(attributable),
            "agree": agree_count,
            "diverge": len(attributable) - agree_count,
            "rate": (
                round(agree_count / len(attributable), 4) if attributable else None
            ),
            "divergence_by_fleet_family": dict(divergence_by_family),
        },
        "fired_any_at_k_pause": fired_any,
    }


def render(summary: Dict[str, Any]) -> str:
    lines = ["== coherence gate shadow — soak read =="]
    lines.append(f"rows: {summary['n_rows']}  by version: {summary['by_statistic_version']}")
    lines.append(
        f"v2 rows: {summary['n_v2']}  eligible: {summary['n_eligible']}  "
        f"ineligible: {summary['ineligible_reasons']}"
    )
    lines.append(f"scale source (eligible): {summary['scale_sources']}")
    lines.append(f"would-action counts: {summary['would_actions']}")
    lines.append(f"|z| percentiles: {summary['magnitude_percentiles']}")
    lines.append("k sweep (eligible rows with |z| >= k):")
    for k, count in summary["k_sweep"].items():
        n = summary["n_eligible"] or 1
        marker = ""
        if k == K_PAUSE:
            marker = "   <- k_pause"
        elif k == K_BLOCK:
            marker = "   <- k_block"
        elif k == K_FLOOR:
            marker = "   <- k_floor"
        lines.append(f"  k={k:>4}: {count:>6}  ({100.0 * count / n:.3f}%){marker}")
    lines.append("per-agent (eligible):")
    for agent_id, stats in sorted(summary["per_agent"].items()):
        lines.append(
            f"  {agent_id[:12]:>12}  n={stats['n_eligible']:>5}  "
            f"p50={stats['p50']}  p90={stats['p90']}  p99={stats['p99']}  "
            f"max={stats['max']}  fired(pause/block/floor)="
            f"{stats['fired_pause']}/{stats['fired_block']}/{stats['fired_floor']}"
        )
    agr = summary["agreement"]
    lines.append(
        f"agreement (attributable rows only): {agr['agree']}/{agr['n_attributable']}"
        f" = {agr['rate']}  (unattributable excluded: {agr['n_unattributable']})"
    )
    if agr["divergence_by_fleet_family"]:
        lines.append(f"  divergence by fleet family: {agr['divergence_by_fleet_family']}")
    if not summary["fired_any_at_k_pause"]:
        lines.append(
            "\nUNFAIR ZERO GUARD: no eligible row reached k_pause during this soak."
            "\nSilence is NOT evidence the gate is useless — the lever is untested"
            "\non this traffic. Run --positive-control to confirm the instrument"
            "\nfires on injected excursions before concluding anything (proposal"
            "\nsection 6, step 4)."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Positive control — drives the REAL evaluate() path, no mocks of the logic
# ---------------------------------------------------------------------------


@dataclass
class _SyntheticBehavioral:
    """Minimal stand-in carrying exactly the attributes evaluate() reads."""

    V_history: List[float] = field(default_factory=list)
    V: float = 0.0
    is_baselined: bool = True
    alphas: Optional[Any] = None


def _history_with_excursion(
    base: float, spread: float, magnitude_sigma: float, n: int = 61
) -> _SyntheticBehavioral:
    """Deterministic history with known dispersion, current value displaced.

    Alternating +/- spread around base gives sample sd == spread exactly (for
    even prior counts), so the injected displacement lands at a predictable
    standardized magnitude when spread dominates the calibrated floor.
    """
    prior = [base + spread * (1 if i % 2 == 0 else -1) for i in range(n - 1)]
    current = base + magnitude_sigma * spread
    return _SyntheticBehavioral(V_history=prior + [current], V=current)


def positive_control() -> List[Dict[str, Any]]:
    """Scenario table proving the shadow statistic fires at each tier.

    Uses spread 0.2 so the empirical sd dominates the calibrated V floor and
    the injected magnitudes land where they were aimed.
    """
    spread = 0.2
    scenarios = [
        ("quiet_baseline", _history_with_excursion(0.0, spread, 0.0), "proceed"),
        ("pause_tier", _history_with_excursion(0.0, spread, K_PAUSE + 0.5), "coherence_pause"),
        ("block_tier", _history_with_excursion(0.0, spread, K_BLOCK + 0.5), "hard_block"),
        ("floor_tier", _history_with_excursion(0.0, spread, K_FLOOR + 0.5), "hard_block_floor"),
        # Near-constant history: the calibrated floor supplies the scale and
        # a physically large excursion must still fire, tagged scale_source=floor.
        (
            "floor_scale_excursion",
            _SyntheticBehavioral(V_history=[0.1] * 60 + [0.9], V=0.9),
            "fires_via_floor",
        ),
        (
            "immature_baseline",
            _SyntheticBehavioral(V_history=[0.1] * 61, V=0.1, is_baselined=False),
            "ineligible",
        ),
        (
            "short_history",
            _SyntheticBehavioral(
                V_history=[0.1] * (RECENT_MIN_SAMPLES - 1), V=0.1
            ),
            "ineligible",
        ),
    ]

    results = []
    for name, behavioral, expectation in scenarios:
        outcome = evaluate(behavioral, fleet_action="proceed")
        if expectation == "ineligible":
            passed = not outcome["eligible"]
        elif expectation == "fires_via_floor":
            passed = (
                outcome["eligible"]
                and outcome["scale_source"] == "floor"
                and outcome["would_action"] != "proceed"
            )
        else:
            passed = outcome["eligible"] and outcome["would_action"] == expectation
        results.append(
            {
                "scenario": name,
                "expected": expectation,
                "would_action": outcome.get("would_action"),
                "eligible": outcome.get("eligible"),
                "scale_source": outcome.get("scale_source"),
                "magnitude": outcome.get("v_deviation_magnitude"),
                "passed": passed,
            }
        )
    return results


def render_positive_control(results: List[Dict[str, Any]]) -> str:
    lines = ["== coherence gate shadow — positive control =="]
    for r in results:
        lines.append(
            f"  [{'PASS' if r['passed'] else 'FAIL'}] {r['scenario']}: "
            f"expected {r['expected']}, got would_action={r['would_action']} "
            f"(eligible={r['eligible']}, scale={r['scale_source']}, "
            f"|z|={r['magnitude']})"
        )
    ok = all(r["passed"] for r in results)
    lines.append(
        "instrument CAN fire at every tier — soak silence is informative"
        if ok
        else "instrument FAILED a control scenario — soak silence is NOT interpretable"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------


def load_jsonl(path: str) -> List[Dict[str, Any]]:
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


async def fetch_rows(db_url: str, window_days: int) -> List[Dict[str, Any]]:
    try:
        import asyncpg
    except ImportError:
        print(
            "error: asyncpg not installed. Install with `pip install asyncpg`,"
            " or use --input with a JSONL export.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    conn = await asyncpg.connect(db_url)
    try:
        records = await conn.fetch(FETCH_QUERY, window_days)
    finally:
        await conn.close()

    rows = []
    for record in records:
        payload = record["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        row = dict(payload or {})
        row.setdefault("agent_id", record["agent_id"])
        rows.append(row)
    return rows


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-url",
        default=os.environ.get("GOVERNANCE_DATABASE_URL", DEFAULT_DB_URL),
    )
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument(
        "--input",
        help="JSONL export of coherence_gate_shadow payloads (bypasses the DB)",
    )
    parser.add_argument(
        "--positive-control",
        action="store_true",
        help="run the injected-excursion control scenarios only (no DB)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    if args.positive_control:
        results = positive_control()
        print(
            json.dumps(results, indent=2)
            if args.json
            else render_positive_control(results)
        )
        return 0 if all(r["passed"] for r in results) else 1

    if args.input:
        rows = load_jsonl(args.input)
    else:
        rows = asyncio.run(fetch_rows(args.db_url, args.window_days))

    summary = summarize(rows)
    print(json.dumps(summary, indent=2) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
