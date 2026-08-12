#!/usr/bin/env python3
"""Read the prospective legacy-coherence dependency shadow.

The live path is never replayed or changed here.  This report joins the exact
prior state selected for a trusted external outcome to the paired deployed and
midpoint-neutralized values stored in ``eisv_telemetry.shadow_ablations``.

Outcome association is withheld until the pre-registered independent-bad-
cluster floor is met.  Distributional deltas remain visible before that floor
because they are capture diagnostics, not evidence that either candidate is an
outcome oracle.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.analysis.eisv_skeptic_report import (  # noqa: E402
    DEFAULT_DB_URL,
    STRICT_OUTCOMES,
    TASK_OUTCOMES,
    auc_score,
    brier_score,
    parse_as_of,
)
from scripts.analysis.outcome_inventory import (  # noqa: E402
    is_controlled_validation_fixture,
)
from src.eisv_telemetry import EISV_SHADOW_ABLATIONS_SCHEMA  # noqa: E402
from src.grounding.outcome_anchors import (  # noqa: E402
    anchored_outcomes_predicate,
)


MIN_BAD_CLUSTERS = 150
NONINFERIORITY_AUC_MARGIN = 0.05
DEFAULT_LEAD_MINUTES = 30.0
DEFAULT_RESAMPLES = 2_000
TRUSTED_ANCHOR_SQL = anchored_outcomes_predicate(table_alias="o")


SHADOW_OUTCOME_SQL = f"""
SELECT
    o.ts,
    o.outcome_id,
    o.agent_id,
    o.outcome_type,
    o.is_bad,
    o.detail,
    ps.recorded_at AS prior_state_recorded_at,
    EXTRACT(EPOCH FROM (o.ts - ps.recorded_at))::float
        AS prior_state_age_seconds,
    ps.state_json #>> '{{eisv_telemetry,measurement_id}}'
        AS prior_measurement_id,
    ps.state_json #>> '{{eisv_telemetry,derivation,kind}}'
        AS prior_derivation_kind,
    ps.state_json #>> '{{eisv_telemetry,shadow_ablations,schema}}'
        AS shadow_schema,
    ps.state_json #>> '{{eisv_telemetry,shadow_ablations,candidates,legacy_coherence_neutralized,behavioral_sensor,eligible}}'
        AS behavioral_eligible,
    ps.state_json #>> '{{eisv_telemetry,shadow_ablations,candidates,legacy_coherence_neutralized,behavioral_sensor,deployed,E}}'
        AS deployed_e,
    ps.state_json #>> '{{eisv_telemetry,shadow_ablations,candidates,legacy_coherence_neutralized,behavioral_sensor,candidate,E}}'
        AS candidate_e,
    ps.state_json #>> '{{eisv_telemetry,shadow_ablations,candidates,legacy_coherence_neutralized,behavioral_sensor,deployed,I}}'
        AS deployed_i,
    ps.state_json #>> '{{eisv_telemetry,shadow_ablations,candidates,legacy_coherence_neutralized,behavioral_sensor,candidate,I}}'
        AS candidate_i,
    ps.state_json #>> '{{eisv_telemetry,shadow_ablations,candidates,legacy_coherence_neutralized,derived_confidence,eligible}}'
        AS confidence_eligible,
    ps.state_json #>> '{{eisv_telemetry,shadow_ablations,candidates,legacy_coherence_neutralized,derived_confidence,deployed,final}}'
        AS deployed_confidence,
    ps.state_json #>> '{{eisv_telemetry,shadow_ablations,candidates,legacy_coherence_neutralized,derived_confidence,candidate,final}}'
        AS candidate_confidence
FROM audit.outcome_events o
LEFT JOIN LATERAL (
    SELECT s.recorded_at, s.state_json
    FROM core.identities i
    JOIN core.agent_state s ON s.identity_id = i.identity_id
    WHERE i.agent_id = o.agent_id
      AND s.synthetic IS NOT TRUE
      AND s.recorded_at <= o.ts - ($2::double precision * INTERVAL '1 minute')
    ORDER BY s.recorded_at DESC
    LIMIT 1
) ps ON TRUE
WHERE o.ts >= COALESCE($4::timestamptz, now())
                  - ($1::int * INTERVAL '1 day')
  AND ($4::timestamptz IS NULL OR o.ts <= $4::timestamptz)
  AND o.outcome_type = ANY($3::text[])
  AND {TRUSTED_ANCHOR_SQL}
ORDER BY o.ts ASC
"""


@dataclass(frozen=True)
class ShadowOutcomeRow:
    """One trusted outcome joined to its leak-safe prior shadow snapshot."""

    ts: datetime
    outcome_id: str
    agent_id: str
    outcome_type: str
    is_bad: bool
    prior_state_recorded_at: datetime | None
    prior_state_age_seconds: float | None
    prior_measurement_id: str | None
    prior_derivation_kind: str | None
    shadow_schema: str | None
    behavioral_eligible: bool | None
    deployed_e: float | None
    candidate_e: float | None
    deployed_i: float | None
    candidate_i: float | None
    confidence_eligible: bool | None
    deployed_confidence: float | None
    candidate_confidence: float | None


@dataclass(frozen=True)
class ChannelRead:
    """Paired capture and, once ready, outcome-safety statistics."""

    channel: str
    rows: int
    bad_rows: int
    clusters: int
    bad_clusters: int
    mean_signed_delta: float | None
    median_abs_delta: float | None
    p95_abs_delta: float | None
    deployed_auc: float | None
    candidate_auc: float | None
    candidate_minus_deployed_auc: float | None
    auc_delta_ci95: tuple[float, float] | None
    deployed_brier: float | None
    candidate_brier: float | None
    brier_improvement: float | None
    status: str


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return None


def _parse_detail(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _row_from_record(record: Any) -> ShadowOutcomeRow:
    return ShadowOutcomeRow(
        ts=record["ts"],
        outcome_id=str(record["outcome_id"]),
        agent_id=str(record["agent_id"]),
        outcome_type=str(record["outcome_type"]),
        is_bad=bool(record["is_bad"]),
        prior_state_recorded_at=record.get("prior_state_recorded_at"),
        prior_state_age_seconds=_to_float(record.get("prior_state_age_seconds")),
        prior_measurement_id=record.get("prior_measurement_id"),
        prior_derivation_kind=record.get("prior_derivation_kind"),
        shadow_schema=record.get("shadow_schema"),
        behavioral_eligible=_to_bool(record.get("behavioral_eligible")),
        deployed_e=_to_float(record.get("deployed_e")),
        candidate_e=_to_float(record.get("candidate_e")),
        deployed_i=_to_float(record.get("deployed_i")),
        candidate_i=_to_float(record.get("candidate_i")),
        confidence_eligible=_to_bool(record.get("confidence_eligible")),
        deployed_confidence=_to_float(record.get("deployed_confidence")),
        candidate_confidence=_to_float(record.get("candidate_confidence")),
    )


def prior_state_cluster_key(row: ShadowOutcomeRow) -> tuple[str, str | int | None]:
    """Group outcomes that reuse the same prior state snapshot."""

    if row.prior_measurement_id:
        return (row.agent_id, f"measurement:{row.prior_measurement_id}")
    if row.prior_state_recorded_at is not None:
        return (row.agent_id, round(row.prior_state_recorded_at.timestamp()))
    age = row.prior_state_age_seconds
    return (row.agent_id, None if age is None else round(row.ts.timestamp() - age))


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _paired_rows(
    rows: Sequence[ShadowOutcomeRow],
    deployed: Callable[[ShadowOutcomeRow], float | None],
    candidate: Callable[[ShadowOutcomeRow], float | None],
) -> list[tuple[ShadowOutcomeRow, float, float]]:
    paired: list[tuple[ShadowOutcomeRow, float, float]] = []
    for row in rows:
        deployed_value = deployed(row)
        candidate_value = candidate(row)
        if deployed_value is None or candidate_value is None:
            continue
        paired.append((row, deployed_value, candidate_value))
    return paired


def _cluster_bootstrap_auc_delta(
    paired: Sequence[tuple[ShadowOutcomeRow, float, float]],
    *,
    resamples: int,
    seed: int,
) -> tuple[float, float] | None:
    """Bootstrap the paired AUC delta over independent prior-state clusters."""

    if resamples <= 0:
        return None
    groups: dict[
        tuple[str, str | int | None],
        list[tuple[ShadowOutcomeRow, float, float]],
    ] = defaultdict(list)
    for item in paired:
        groups[prior_state_cluster_key(item[0])].append(item)
    keys = list(groups)
    if not keys:
        return None

    rng = random.Random(seed)
    deltas: list[float] = []
    for _ in range(resamples):
        sample = [groups[rng.choice(keys)] for _ in keys]
        flattened = [item for group in sample for item in group]
        labels = [int(item[0].is_bad) for item in flattened]
        deployed_scores = [1.0 - item[1] for item in flattened]
        candidate_scores = [1.0 - item[2] for item in flattened]
        deployed_auc = auc_score(labels, deployed_scores)
        candidate_auc = auc_score(labels, candidate_scores)
        if deployed_auc is not None and candidate_auc is not None:
            deltas.append(candidate_auc - deployed_auc)
    low = _percentile(deltas, 0.025)
    high = _percentile(deltas, 0.975)
    return None if low is None or high is None else (low, high)


def summarize_channel(
    rows: Sequence[ShadowOutcomeRow],
    *,
    channel: str,
    deployed: Callable[[ShadowOutcomeRow], float | None],
    candidate: Callable[[ShadowOutcomeRow], float | None],
    probability_like: bool = False,
    min_bad_clusters: int = MIN_BAD_CLUSTERS,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = 0,
) -> ChannelRead:
    paired = _paired_rows(rows, deployed, candidate)
    deltas = [
        candidate_value - deployed_value
        for _, deployed_value, candidate_value in paired
    ]
    abs_deltas = [abs(delta) for delta in deltas]
    clusters = {prior_state_cluster_key(row) for row, _, _ in paired}
    bad_clusters = {prior_state_cluster_key(row) for row, _, _ in paired if row.is_bad}

    common = dict(
        channel=channel,
        rows=len(paired),
        bad_rows=sum(int(row.is_bad) for row, _, _ in paired),
        clusters=len(clusters),
        bad_clusters=len(bad_clusters),
        mean_signed_delta=(statistics.fmean(deltas) if deltas else None),
        median_abs_delta=(statistics.median(abs_deltas) if abs_deltas else None),
        p95_abs_delta=_percentile(abs_deltas, 0.95),
    )
    labels = [int(row.is_bad) for row, _, _ in paired]
    if len(bad_clusters) < min_bad_clusters or len(set(labels)) < 2:
        return ChannelRead(
            **common,
            deployed_auc=None,
            candidate_auc=None,
            candidate_minus_deployed_auc=None,
            auc_delta_ci95=None,
            deployed_brier=None,
            candidate_brier=None,
            brier_improvement=None,
            status="WAIT_SAMPLE_FLOOR",
        )

    deployed_scores = [1.0 - deployed_value for _, deployed_value, _ in paired]
    candidate_scores = [1.0 - candidate_value for _, _, candidate_value in paired]
    deployed_auc = auc_score(labels, deployed_scores)
    candidate_auc = auc_score(labels, candidate_scores)
    auc_delta = (
        None
        if deployed_auc is None or candidate_auc is None
        else candidate_auc - deployed_auc
    )
    auc_ci = _cluster_bootstrap_auc_delta(paired, resamples=resamples, seed=seed)

    deployed_brier = brier_score(labels, deployed_scores) if probability_like else None
    candidate_brier = (
        brier_score(labels, candidate_scores) if probability_like else None
    )
    brier_improvement = (
        None
        if deployed_brier is None or candidate_brier is None
        else deployed_brier - candidate_brier
    )

    if auc_ci is None:
        status = "INCONCLUSIVE_NO_UNCERTAINTY"
    elif auc_ci[0] >= -NONINFERIORITY_AUC_MARGIN:
        status = "PASS_AUC_NONINFERIORITY"
    elif auc_ci[1] < -NONINFERIORITY_AUC_MARGIN:
        status = "FAIL_MEANINGFUL_AUC_LOSS"
    else:
        status = "INCONCLUSIVE_AUC"
    return ChannelRead(
        **common,
        deployed_auc=deployed_auc,
        candidate_auc=candidate_auc,
        candidate_minus_deployed_auc=auc_delta,
        auc_delta_ci95=auc_ci,
        deployed_brier=deployed_brier,
        candidate_brier=candidate_brier,
        brier_improvement=brier_improvement,
        status=status,
    )


def build_reads(
    rows: Sequence[ShadowOutcomeRow],
    *,
    min_bad_clusters: int = MIN_BAD_CLUSTERS,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = 0,
) -> list[ChannelRead]:
    """Build fixed, non-selected reads for E, I, and omitted confidence."""

    channels = (
        (
            "behavioral_E",
            lambda row: row.deployed_e,
            lambda row: row.candidate_e,
            False,
        ),
        (
            "behavioral_I",
            lambda row: row.deployed_i,
            lambda row: row.candidate_i,
            False,
        ),
        (
            "omitted_confidence",
            lambda row: row.deployed_confidence,
            lambda row: row.candidate_confidence,
            True,
        ),
    )
    return [
        summarize_channel(
            rows,
            channel=channel,
            deployed=deployed,
            candidate=candidate,
            probability_like=probability_like,
            min_bad_clusters=min_bad_clusters,
            resamples=resamples,
            seed=seed + index,
        )
        for index, (channel, deployed, candidate, probability_like) in enumerate(
            channels
        )
    ]


def _fmt(value: float | None, digits: int = 4) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _fmt_ci(value: tuple[float, float] | None) -> str:
    return "—" if value is None else f"[{value[0]:+.4f}, {value[1]:+.4f}]"


def build_report(
    rows: Sequence[ShadowOutcomeRow],
    *,
    scope: str,
    window_days: int,
    lead_minutes: float,
    min_bad_clusters: int = MIN_BAD_CLUSTERS,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = 0,
    as_of: datetime | None = None,
) -> str:
    instrumented = [
        row for row in rows if row.shadow_schema == EISV_SHADOW_ABLATIONS_SCHEMA
    ]
    behavioral_applicable = [
        row for row in instrumented if row.prior_derivation_kind == "behavioral_sensor"
    ]
    reads = build_reads(
        instrumented,
        min_bad_clusters=min_bad_clusters,
        resamples=resamples,
        seed=seed,
    )
    lines = [
        "# Legacy-coherence dependency shadow",
        "",
        (
            f"Scope: trusted external `{scope}` outcomes, {window_days} d, "
            f"lead {lead_minutes:g} min, as-of "
            f"{as_of.isoformat() if as_of else 'live'}"
        ),
        "",
        "This is a safety read on a prospective measurement ablation, not an "
        "outcome-oracle claim and not an actuator.",
        "",
        "## Capture",
        "",
        f"- Trusted non-fixture outcomes fetched: {len(rows)}",
        f"- Outcomes with `{EISV_SHADOW_ABLATIONS_SCHEMA}` prior state: {len(instrumented)}",
        f"- Behavioral-sensor-applicable prior states: {len(behavioral_applicable)}",
        "",
        "## Fixed paired reads",
        "",
        "| channel | paired rows | bad rows | clusters | bad clusters | mean candidate−deployed | median |Δ| | p95 |Δ| | deployed AUC | candidate AUC | ΔAUC | cluster-bootstrap 95% CI | status |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for read in reads:
        lines.append(
            f"| {read.channel} | {read.rows} | {read.bad_rows} | {read.clusters} "
            f"| {read.bad_clusters} | {_fmt(read.mean_signed_delta)} "
            f"| {_fmt(read.median_abs_delta)} | {_fmt(read.p95_abs_delta)} "
            f"| {_fmt(read.deployed_auc)} | {_fmt(read.candidate_auc)} "
            f"| {_fmt(read.candidate_minus_deployed_auc)} "
            f"| {_fmt_ci(read.auc_delta_ci95)} | {read.status} |"
        )

    confidence = next(read for read in reads if read.channel == "omitted_confidence")
    lines.extend(
        [
            "",
            "## Confidence calibration diagnostic",
            "",
            f"- Deployed Brier: {_fmt(confidence.deployed_brier)}",
            f"- Candidate Brier: {_fmt(confidence.candidate_brier)}",
            f"- Brier improvement (positive favors candidate): {_fmt(confidence.brier_improvement)}",
            "",
            "## Interpretation contract",
            "",
            f"- Outcome metrics are withheld until each channel has at least {min_bad_clusters} independent bad prior-state clusters.",
            f"- The AUC non-inferiority margin is −{NONINFERIORITY_AUC_MARGIN:.2f}, inherited from the standing operational-relevance bound; no best-channel selection is performed.",
            "- PASS does not authorize a live change. Behavioral E/I still require recursive history, EMA, baseline, and policy replay. Confidence requires a separately pre-registered calibration decision rule.",
            "- The deployed values, weights, thresholds, verdicts, and actuators remain authoritative throughout the shadow.",
        ]
    )
    return "\n".join(lines)


async def fetch_rows(
    db_url: str,
    *,
    window_days: int,
    lead_minutes: float,
    outcome_types: Sequence[str],
    as_of: datetime | None = None,
) -> list[ShadowOutcomeRow]:
    try:
        import asyncpg
    except ImportError:
        print(
            "error: asyncpg not installed. Install with `pip install asyncpg`.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    conn = await asyncpg.connect(db_url)
    try:
        records = await conn.fetch(
            SHADOW_OUTCOME_SQL,
            window_days,
            lead_minutes,
            list(outcome_types),
            as_of,
        )
    finally:
        await conn.close()

    rows: list[ShadowOutcomeRow] = []
    for record in records:
        detail = _parse_detail(record.get("detail"))
        if is_controlled_validation_fixture(detail, include_declared_purpose=False):
            continue
        rows.append(_row_from_record(record))
    return rows


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-days", type=int, default=365)
    parser.add_argument("--lead-minutes", type=float, default=DEFAULT_LEAD_MINUTES)
    parser.add_argument("--scope", choices=("strict", "task"), default="task")
    parser.add_argument("--min-bad-clusters", type=int, default=MIN_BAD_CLUSTERS)
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--as-of", type=parse_as_of)
    parser.add_argument(
        "--db-url", default=os.environ.get("GOVERNANCE_DATABASE_URL", DEFAULT_DB_URL)
    )
    parser.add_argument("--output", help="Optional markdown output path")
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    if args.window_days <= 0 or args.lead_minutes < 0:
        print(
            "error: window-days must be positive and lead-minutes non-negative",
            file=sys.stderr,
        )
        return 2
    if args.min_bad_clusters <= 0 or args.resamples <= 0:
        print("error: min-bad-clusters and resamples must be positive", file=sys.stderr)
        return 2
    outcome_types = STRICT_OUTCOMES if args.scope == "strict" else TASK_OUTCOMES
    rows = await fetch_rows(
        args.db_url,
        window_days=args.window_days,
        lead_minutes=args.lead_minutes,
        outcome_types=outcome_types,
        as_of=args.as_of,
    )
    report = build_report(
        rows,
        scope=args.scope,
        window_days=args.window_days,
        lead_minutes=args.lead_minutes,
        min_bad_clusters=args.min_bad_clusters,
        resamples=args.resamples,
        seed=args.seed,
        as_of=args.as_of,
    )
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report + "\n", encoding="utf-8")
        print(f"Wrote {path}")
    else:
        print(report)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
