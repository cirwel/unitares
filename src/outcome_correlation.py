"""Outcome correlation study and export helpers for empirical validation.

Joins EISV snapshots with outcome events to compute:
1. Verdict distribution: bad-outcome rate per verdict
2. Metric correlations: Pearson r between each EISV metric and outcome_score
3. Risk binning: bad-outcome rate per risk score range
4. Coverage: how much of the dataset is grounded in behavioral/primary/exogenous data

Data source: audit.outcome_events table (EISV snapshot embedded at outcome time).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)


@dataclass
class CorrelationReport:
    """Result of an outcome correlation study."""
    total_outcomes: int
    good_outcomes: int
    bad_outcomes: int
    verdict_distribution: Dict[str, Dict[str, Any]]
    metric_correlations: Dict[str, Optional[float]]
    risk_bins: List[Dict[str, Any]]
    summary: str = ""
    coverage: Dict[str, Any] = field(default_factory=dict)


def _pearson_r(xs: List[float], ys: List[float]) -> Optional[float]:
    """Pearson correlation coefficient. Returns None if insufficient data or zero variance."""
    n = len(xs)
    if n < 3 or len(ys) != n:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    denom = math.sqrt(var_x * var_y)
    if denom < 1e-12:
        return None
    return cov / denom


def compute_verdict_distribution(outcomes: List[Dict]) -> Dict[str, Dict[str, Any]]:
    """Group outcomes by eisv_verdict, compute bad-outcome rate per verdict."""
    buckets: Dict[str, List[Dict]] = {}
    for o in outcomes:
        v = o.get("eisv_verdict") or "unknown"
        buckets.setdefault(v, []).append(o)

    result = {}
    for verdict, items in sorted(buckets.items()):
        bad = sum(1 for i in items if i.get("is_bad"))
        scores = [i["outcome_score"] for i in items if i.get("outcome_score") is not None]
        result[verdict] = {
            "count": len(items),
            "bad_count": bad,
            "bad_rate": round(bad / len(items), 4) if items else 0,
            "avg_score": round(sum(scores) / len(scores), 4) if scores else None,
        }
    return result


def compute_metric_correlations(outcomes: List[Dict]) -> Dict[str, Optional[float]]:
    """Pearson r between each EISV metric and outcome_score."""
    metrics = ["eisv_e", "eisv_i", "eisv_s", "eisv_v", "eisv_phi", "eisv_coherence"]
    scores = [o["outcome_score"] for o in outcomes if o.get("outcome_score") is not None]

    result = {}
    for m in metrics:
        xs = []
        ys = []
        for o in outcomes:
            if o.get(m) is not None and o.get("outcome_score") is not None:
                xs.append(float(o[m]))
                ys.append(float(o["outcome_score"]))
        r = _pearson_r(xs, ys)
        label = m.replace("eisv_", "").upper()
        result[label] = round(r, 4) if r is not None else None
    return result


def _count_pct(count: int, total: int) -> Dict[str, Any]:
    """Return a compact count/percentage pair."""
    pct = round(count / total, 4) if total else 0.0
    return {"count": count, "pct": pct}


def _get_detail(outcome: Dict[str, Any]) -> Dict[str, Any]:
    """Return detail as a dict, never None."""
    detail = outcome.get("detail")
    return detail if isinstance(detail, dict) else {}


def _has_snapshot(detail: Dict[str, Any], key: str) -> bool:
    """Return True when a snapshot payload exists and has at least one populated field."""
    payload = detail.get(key)
    if not isinstance(payload, dict):
        return False
    return any(payload.get(field) is not None for field in ("E", "I", "S", "V", "risk", "confidence"))


def _exogenous_signal_flags(detail: Dict[str, Any]) -> Dict[str, bool]:
    """Return booleans for exogenous evidence types present in outcome detail."""
    return {
        "tests": bool(detail.get("tests")),
        "commands": bool(detail.get("commands")),
        "files": bool(detail.get("files")),
        "lint": bool(detail.get("lint")),
        "tool_observations": bool(detail.get("tool_usage") or detail.get("tool_results")),
        "outcome_events": bool(detail.get("outcome_events")),
    }


def _is_eprocess_eligible(detail: Dict[str, Any]) -> bool:
    """Return True when the outcome is eligible for the hard exogenous e-process lane."""
    return bool(detail.get("eprocess_eligible"))


def compute_observability_coverage(outcomes: List[Dict]) -> Dict[str, Any]:
    """Measure how grounded the current outcome dataset is."""
    total = len(outcomes)
    if total == 0:
        return {
            "total_outcomes": 0,
            "with_primary_eisv": _count_pct(0, 0),
            "with_behavioral_eisv": _count_pct(0, 0),
            "with_behavioral_primary": _count_pct(0, 0),
            "with_exogenous_signals": _count_pct(0, 0),
            "with_eprocess_eligible": _count_pct(0, 0),
            "with_snapshot": _count_pct(0, 0),
            "with_outcome_score": _count_pct(0, 0),
            "primary_source_counts": {},
            "exogenous_signal_counts": {},
            "eprocess_signal_counts": {},
        }

    with_primary_eisv = 0
    with_behavioral_eisv = 0
    with_behavioral_primary = 0
    with_exogenous_signals = 0
    with_eprocess_eligible = 0
    with_snapshot = 0
    with_outcome_score = 0
    primary_source_counts: Dict[str, int] = {}
    exogenous_signal_counts = {
        "tests": 0,
        "commands": 0,
        "files": 0,
        "lint": 0,
        "tool_observations": 0,
        "outcome_events": 0,
    }
    eprocess_signal_counts: Dict[str, int] = {}

    for outcome in outcomes:
        detail = _get_detail(outcome)
        if detail.get("snapshot_missing") is False:
            with_snapshot += 1
        if _has_snapshot(detail, "primary_eisv"):
            with_primary_eisv += 1
        if _has_snapshot(detail, "behavioral_eisv"):
            with_behavioral_eisv += 1

        source = detail.get("primary_eisv_source") or "unknown"
        primary_source_counts[source] = primary_source_counts.get(source, 0) + 1
        if source == "behavioral":
            with_behavioral_primary += 1

        if outcome.get("outcome_score") is not None:
            with_outcome_score += 1

        signal_flags = _exogenous_signal_flags(detail)
        if any(signal_flags.values()):
            with_exogenous_signals += 1
        for signal_type, present in signal_flags.items():
            if present:
                exogenous_signal_counts[signal_type] += 1

        if _is_eprocess_eligible(detail):
            with_eprocess_eligible += 1
            source = detail.get("hard_exogenous_signal") or "unknown"
            eprocess_signal_counts[source] = eprocess_signal_counts.get(source, 0) + 1

    return {
        "total_outcomes": total,
        "with_primary_eisv": _count_pct(with_primary_eisv, total),
        "with_behavioral_eisv": _count_pct(with_behavioral_eisv, total),
        "with_behavioral_primary": _count_pct(with_behavioral_primary, total),
        "with_exogenous_signals": _count_pct(with_exogenous_signals, total),
        "with_eprocess_eligible": _count_pct(with_eprocess_eligible, total),
        "with_snapshot": _count_pct(with_snapshot, total),
        "with_outcome_score": _count_pct(with_outcome_score, total),
        "primary_source_counts": primary_source_counts,
        "exogenous_signal_counts": exogenous_signal_counts,
        "eprocess_signal_counts": eprocess_signal_counts,
    }


def flatten_outcome_for_export(outcome: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten an outcome event into a row suitable for CSV/JSONL export."""
    detail = _get_detail(outcome)
    primary = detail.get("primary_eisv") if isinstance(detail.get("primary_eisv"), dict) else {}
    behavioral = detail.get("behavioral_eisv") if isinstance(detail.get("behavioral_eisv"), dict) else {}
    signal_flags = _exogenous_signal_flags(detail)

    row = {
        "agent_id": outcome.get("agent_id"),
        "ts": outcome.get("ts"),
        "outcome_type": outcome.get("outcome_type"),
        "is_bad": outcome.get("is_bad"),
        "outcome_score": outcome.get("outcome_score"),
        "eisv_verdict": outcome.get("eisv_verdict"),
        "eisv_e": outcome.get("eisv_e"),
        "eisv_i": outcome.get("eisv_i"),
        "eisv_s": outcome.get("eisv_s"),
        "eisv_v": outcome.get("eisv_v"),
        "eisv_phi": outcome.get("eisv_phi"),
        "eisv_coherence": outcome.get("eisv_coherence"),
        "eisv_regime": outcome.get("eisv_regime"),
        "primary_eisv_source": detail.get("primary_eisv_source"),
        "snapshot_missing": detail.get("snapshot_missing"),
        "reported_confidence": detail.get("reported_confidence"),
        "decision_action": detail.get("decision_action"),
        "hard_exogenous_signal": detail.get("hard_exogenous_signal"),
        "eprocess_eligible": detail.get("eprocess_eligible"),
        "primary_e": primary.get("E"),
        "primary_i": primary.get("I"),
        "primary_s": primary.get("S"),
        "primary_v": primary.get("V"),
        "behavioral_e": behavioral.get("E"),
        "behavioral_i": behavioral.get("I"),
        "behavioral_s": behavioral.get("S"),
        "behavioral_v": behavioral.get("V"),
        "behavioral_confidence": behavioral.get("confidence"),
        "behavioral_risk": behavioral.get("risk"),
    }
    row.update(signal_flags)
    row["has_exogenous_signals"] = any(signal_flags.values())
    return row


# Risk bin boundaries aligned with behavioral assessment thresholds
_RISK_BINS = [
    (0.0, 0.35, "healthy"),
    (0.35, 0.60, "moderate"),
    (0.60, 1.01, "critical"),
]


def compute_risk_bins(outcomes: List[Dict]) -> List[Dict[str, Any]]:
    """Bin outcomes by behavioral risk score and compute bad rate per bin.

    Falls back to ODE-derived risk from EISV metrics if behavioral risk
    is not available in the detail payload.
    """
    bins: List[List[Dict]] = [[] for _ in _RISK_BINS]

    for o in outcomes:
        # Try behavioral risk from detail first
        risk = None
        detail = o.get("detail") or {}
        if isinstance(detail, dict):
            beh = detail.get("behavioral_eisv") or {}
            risk = beh.get("risk")

        # Fallback: rough risk from EISV metrics
        if risk is None:
            e = o.get("eisv_e")
            i_val = o.get("eisv_i")
            s = o.get("eisv_s")
            if e is not None and i_val is not None and s is not None:
                # Simplified risk proxy: lower E/I and higher S → higher risk
                risk = max(0.0, min(1.0, 0.3 * (1 - e) + 0.3 * (1 - i_val) + 0.4 * s))

        if risk is None:
            continue

        for idx, (lo, hi, _label) in enumerate(_RISK_BINS):
            if lo <= risk < hi:
                bins[idx].append(o)
                break

    result = []
    for idx, (lo, hi, label) in enumerate(_RISK_BINS):
        items = bins[idx]
        bad = sum(1 for i in items if i.get("is_bad"))
        result.append({
            "range": f"{lo:.2f}-{hi:.2f}",
            "label": label,
            "count": len(items),
            "bad_count": bad,
            "bad_rate": round(bad / len(items), 4) if items else 0,
        })
    return result


def _build_summary(report: CorrelationReport) -> str:
    """Generate a human-readable summary of the correlation study."""
    lines = [f"Outcomes: {report.total_outcomes} ({report.good_outcomes} good, {report.bad_outcomes} bad)"]

    coverage = report.coverage or {}
    if coverage:
        exogenous = coverage.get("with_exogenous_signals", {})
        behavioral_primary = coverage.get("with_behavioral_primary", {})
        if isinstance(exogenous, dict) and isinstance(behavioral_primary, dict):
            lines.append(
                "Coverage: "
                f"{exogenous.get('count', 0)}/{report.total_outcomes} outcomes have exogenous signals; "
                f"{behavioral_primary.get('count', 0)}/{report.total_outcomes} use behavioral primary state"
            )
        eprocess_eligible = coverage.get("with_eprocess_eligible", {})
        if isinstance(eprocess_eligible, dict):
            lines.append(
                "Sequential lane: "
                f"{eprocess_eligible.get('count', 0)}/{report.total_outcomes} outcomes are hard-exogenous tactical candidates"
            )

    # Verdict distribution insight
    for v, d in report.verdict_distribution.items():
        if d["count"] >= 3:
            lines.append(f"  {v}: {d['count']} outcomes, {d['bad_rate']*100:.1f}% bad")

    # Strongest correlations
    best_pos = max(
        ((k, v) for k, v in report.metric_correlations.items() if v is not None),
        key=lambda x: abs(x[1]),
        default=None,
    )
    if best_pos:
        k, v = best_pos
        direction = "positively" if v > 0 else "negatively"
        lines.append(f"Strongest correlation: {k} ({direction}, r={v:.3f})")

    # Risk bin insight
    for b in report.risk_bins:
        if b["count"] >= 3:
            lines.append(f"  Risk {b['label']} ({b['range']}): {b['count']} outcomes, {b['bad_rate']*100:.1f}% bad")

    return "\n".join(lines)


class OutcomeCorrelation:
    """Correlate EISV state with outcome quality."""

    async def run(
        self,
        agent_id: Optional[str] = None,
        since_hours: float = 168.0,
    ) -> CorrelationReport:
        """Run the correlation study against the outcome_events table."""
        outcomes = await self._fetch_outcomes(agent_id, since_hours)

        good = sum(1 for o in outcomes if not o.get("is_bad"))
        bad = sum(1 for o in outcomes if o.get("is_bad"))

        verdict_dist = compute_verdict_distribution(outcomes)
        correlations = compute_metric_correlations(outcomes)
        risk_bins = compute_risk_bins(outcomes)
        coverage = compute_observability_coverage(outcomes)

        report = CorrelationReport(
            total_outcomes=len(outcomes),
            good_outcomes=good,
            bad_outcomes=bad,
            verdict_distribution=verdict_dist,
            metric_correlations=correlations,
            risk_bins=risk_bins,
            coverage=coverage,
            summary="",
        )
        report.summary = _build_summary(report)
        return report

    async def export_rows(
        self,
        agent_id: Optional[str] = None,
        since_hours: float = 168.0,
    ) -> List[Dict[str, Any]]:
        """Fetch and flatten outcome rows for offline validation analysis."""
        outcomes = await self._fetch_outcomes(agent_id, since_hours)
        return [flatten_outcome_for_export(outcome) for outcome in outcomes]

    async def _fetch_outcomes(
        self, agent_id: Optional[str], since_hours: float
    ) -> List[Dict]:
        """Fetch outcome events with EISV snapshots from DB."""
        from src.db import get_db
        from src.db.mixins.tool_usage import fetch_outcomes_for_correlation

        db = get_db()
        async with db.acquire() as conn:
            rows = await fetch_outcomes_for_correlation(
                conn, agent_id=agent_id, since_hours=since_hours
            )
            results = []
            for row in rows:
                d = dict(row)
                # Parse detail JSON if it's a string
                if isinstance(d.get("detail"), str):
                    import json
                    try:
                        d["detail"] = json.loads(d["detail"])
                    except (json.JSONDecodeError, TypeError):
                        d["detail"] = {}
                results.append(d)
            return results
