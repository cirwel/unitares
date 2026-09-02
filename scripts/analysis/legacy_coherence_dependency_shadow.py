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
import hashlib
import json
import math
import os
import random
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
from scripts.analysis.eisv_ablation_matrix import DEFAULT_READ_LEDGER_DIR  # noqa: E402
from src.eisv_telemetry import EISV_SHADOW_ABLATIONS_SCHEMA  # noqa: E402
from src.grounding.outcome_anchors import (  # noqa: E402
    CORRECTED_FIXTURE_RULE,
    REGISTERED_FIXTURE_RULE,
    FIXTURE_RULES,
    anchored_outcomes_predicate,
)


MIN_BAD_CLUSTERS = 150
NONINFERIORITY_AUC_MARGIN = 0.05
DEFAULT_LEAD_MINUTES = 30.0
DEFAULT_RESAMPLES = 2_000
DEFAULT_WINDOW_DAYS = 365
DEFAULT_SEED = 0
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


# Contract versions of this read (legacy-coherence-dependency-ablation-v0.md).
# v0 is the prospective contract registered 2026-08-12: its item 2 reads a
# server-stamped calibration_excluded as a fixture marker (the registered rule),
# and it is preserved as registered, zero-eligibility result included. v0.1 is a
# DISTINCT prospective read registered 2026-09-02 (decision session
# e4ebf589a1c79b9d): the corrected rule, and only outcomes at or after the
# amendment cutoff, so nothing inspected before the amendment can enter it. A
# v0.1 report carries the registered-rule result alongside as sensitivity and
# provenance, never as equivalent evidence.
CONTRACT_VERSIONS = ("v0", "v0.1")
DEFAULT_CONTRACT_VERSION = "v0"
V0_1_AMENDMENT_CUTOFF = datetime(2026, 9, 3, tzinfo=timezone.utc)
CONTRACT_FIXTURE_RULES = {"v0": REGISTERED_FIXTURE_RULE, "v0.1": CORRECTED_FIXTURE_RULE}


def contract_fixture_rule(contract: str) -> str:
    """The fixture rule a contract version fixed at registration."""
    try:
        return CONTRACT_FIXTURE_RULES[contract]
    except KeyError as exc:
        raise ValueError(f"unknown contract version {contract!r}; expected one of {CONTRACT_VERSIONS}") from exc


def contract_not_before(contract: str) -> datetime | None:
    """The earliest outcome instant a contract version admits (v0.1 excludes pre-amendment outcomes)."""
    return V0_1_AMENDMENT_CUTOFF if contract == "v0.1" else None


class ShadowReadError(RuntimeError):
    """A v0.1 read was asked to run outside its registered terms."""


# The read ids the v0.1 contract predeclares: a numbered series in its own
# namespace, so a shadow receipt can never occupy or masquerade as an ablation
# matrix receipt in the shared ledger.
V0_1_READ_ID_PATTERN = re.compile(r"legacy-coherence-dependency-v0\.1-[1-9][0-9]*")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def validate_v0_1_read(args: argparse.Namespace, *, as_of: datetime, now: datetime | None = None) -> None:
    """Refuse a v0.1 read that would not be the read the contract registered.

    v0.1 differs from v0 in exactly two respects (the corrected rule and the
    cutoff) and inherits every other default, so every other parameter is
    pinned; it requires a predeclared read id for its one-shot receipt; and it
    refuses a boundary before the amendment cutoff (empty by construction) or
    after the present (a receipt spent on a window that has not happened).
    """
    checked_at = (now or _utcnow()).astimezone(timezone.utc)
    problems: list[str] = []
    if not args.read_id or V0_1_READ_ID_PATTERN.fullmatch(args.read_id) is None:
        problems.append(
            "v0.1 reads require --read-id of the form legacy-coherence-dependency-v0.1-<n> "
            "(n from 1; a retry is the next n, disclosed)"
        )
    if args.scope != "task":
        problems.append("v0.1 pins --scope task (contract item 1)")
    if args.lead_minutes != DEFAULT_LEAD_MINUTES:
        problems.append(f"v0.1 pins --lead-minutes {DEFAULT_LEAD_MINUTES:g} (contract item 3)")
    if args.min_bad_clusters != MIN_BAD_CLUSTERS:
        problems.append(f"v0.1 pins --min-bad-clusters {MIN_BAD_CLUSTERS} (contract item 7)")
    if args.window_days != DEFAULT_WINDOW_DAYS:
        problems.append(f"v0.1 pins --window-days {DEFAULT_WINDOW_DAYS} (inherited default)")
    if args.resamples != DEFAULT_RESAMPLES:
        problems.append(f"v0.1 pins --resamples {DEFAULT_RESAMPLES} (contract item 8, inherited default)")
    if args.seed != DEFAULT_SEED:
        problems.append(f"v0.1 pins --seed {DEFAULT_SEED} (inherited default)")
    if as_of < V0_1_AMENDMENT_CUTOFF:
        problems.append(
            "v0.1 cannot read a boundary before its amendment cutoff "
            f"{V0_1_AMENDMENT_CUTOFF.isoformat()}; the result would be empty by construction"
        )
    if as_of > checked_at:
        problems.append("v0.1 cannot read a boundary in the future at access time")
    if problems:
        raise ShadowReadError("; ".join(problems))


def record_shadow_read_receipt(
    args: argparse.Namespace,
    *,
    as_of: datetime,
    now: datetime | None = None,
) -> Path:
    """Write the one-shot receipt of a v0.1 read before any database access.

    Same ledger and same refusal as the ablation matrix: a read id that already
    has a receipt cannot run again; a retry is a new id, disclosed.
    """
    recorded_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ledger_dir = Path(args.read_ledger_dir)
    ledger_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(args.read_id.encode("utf-8")).hexdigest()
    receipt_path = ledger_dir / f"{digest}.json"
    receipt = {
        "schema": "unitares.coherence_shadow_read_receipt.v1",
        "status": "access_started",
        "read_id": args.read_id,
        "contract": args.contract,
        "fixture_rule": contract_fixture_rule(args.contract),
        "not_before": V0_1_AMENDMENT_CUTOFF.isoformat(),
        "as_of": as_of.isoformat(),
        "recorded_at": recorded_at.isoformat(),
        "parameters": {
            "scope": args.scope,
            "window_days": args.window_days,
            "lead_minutes": args.lead_minutes,
            "min_bad_clusters": args.min_bad_clusters,
            "resamples": args.resamples,
            "seed": args.seed,
        },
    }
    payload = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        fd = os.open(os.fspath(receipt_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ShadowReadError(
            f"read id {args.read_id!r} already has a receipt; a retry is a new id, disclosed"
        ) from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    return receipt_path


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
    fixture_rule: str | None = None,
    contract: str | None = None,
    not_before: datetime | None = None,
    registered_sensitivity: Sequence[ShadowOutcomeRow] | None = None,
    read_id: str | None = None,
) -> str:
    """Render the read. ``registered_sensitivity`` (v0.1 only) is the same window under
    the registered rule, reported alongside as provenance, never as equivalent evidence."""
    if contract == "v0.1" and not_before is None:
        raise ValueError("a v0.1 report must carry the cutoff it was read under")
    admitted: list[str] = []
    if contract == "v0.1" and as_of is not None and not_before is not None:
        window_start = max(as_of - timedelta(days=window_days), not_before)
        admitted = [
            "Admitted window: "
            f"{window_start.isoformat()} to {as_of.isoformat()} "
            f"({(as_of - window_start).total_seconds() / 86400:.1f} d; the cutoff bounds the start)",
            "",
        ]
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
        *([f"Contract: `{contract}`" + (
            " (prospective read registered 2026-08-12; item 2 as registered)" if contract == "v0"
            else f" (distinct prospective read registered 2026-09-02; outcomes at or after {not_before.isoformat()})"
        ), *([f"Read ID: `{read_id}`"] if read_id else []), ""] if contract else []),
        *admitted,
        *(
            [
                f"Fixture rule: `{fixture_rule}`"
                + (
                    " (the contract's item 2 as registered)"
                    if fixture_rule == "registered" and contract in (None, "v0")
                    else " (the v0.1 contract's rule)"
                    if contract == "v0.1" and fixture_rule == "corrected"
                    else " (a disclosed deviation from the contract's item 2)"
                ),
                "",
            ]
            if fixture_rule
            else []
        ),
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
    if registered_sensitivity is not None:
        sens_reads = build_reads(
            [r for r in registered_sensitivity if r.shadow_schema == EISV_SHADOW_ABLATIONS_SCHEMA],
            min_bad_clusters=min_bad_clusters,
            resamples=resamples,
            seed=seed,
        )
        lines.extend(
            [
                "",
                "## Registered-rule sensitivity (provenance only)",
                "",
                "The same window under the registered fixture rule, reported beside the "
                "v0.1 result as sensitivity and provenance, not as equivalent evidence.",
                "",
                f"- Trusted non-fixture outcomes fetched: {len(registered_sensitivity)}",
                *(f"- {read.channel}: {read.status}" for read in sens_reads),
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
    fixture_rule: str = REGISTERED_FIXTURE_RULE,
    not_before: datetime | None = None,
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
        if not_before is not None:
            ts = record.get("ts")
            if ts is None:
                continue  # no timestamp, no admission: the cutoff fails closed
            if ts.tzinfo is None:
                # TIMESTAMPTZ arrives aware; a naive value is UTC, and the row
                # keeps the aware value so cluster keys do not depend on host time.
                ts = ts.replace(tzinfo=timezone.utc)
                record = {**dict(record), "ts": ts}
            if ts < not_before:
                continue  # v0.1 admits no outcome from before its amendment
        detail = _parse_detail(record.get("detail"))
        if is_controlled_validation_fixture(
            detail, include_declared_purpose=False, rule=fixture_rule
        ):
            continue
        rows.append(_row_from_record(record))
    return rows


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--lead-minutes", type=float, default=DEFAULT_LEAD_MINUTES)
    parser.add_argument("--scope", choices=("strict", "task"), default="task")
    parser.add_argument(
        "--contract",
        choices=CONTRACT_VERSIONS,
        default=DEFAULT_CONTRACT_VERSION,
        help=(
            "Which registered contract this read runs: 'v0' (default; item 2 as "
            "registered, the registered fixture rule) or 'v0.1' (the distinct "
            "prospective read registered 2026-09-02: corrected rule, outcomes at or "
            "after the amendment cutoff, registered-rule result reported alongside)."
        ),
    )
    parser.add_argument(
        "--fixture-rule",
        choices=FIXTURE_RULES,
        default=None,
        help=(
            "The contract fixes this (v0: registered; v0.1: corrected). Passing a "
            "different value is rejected: run the other contract instead."
        ),
    )
    parser.add_argument(
        "--read-id",
        default=None,
        help="v0.1 only: the predeclared read id (one receipt per id; a retry is a new id).",
    )
    parser.add_argument(
        "--read-ledger-dir",
        default=DEFAULT_READ_LEDGER_DIR,
        help="Where v0.1 read receipts are written (shared with the ablation matrix).",
    )
    parser.add_argument("--min-bad-clusters", type=int, default=MIN_BAD_CLUSTERS)
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
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
    fixture_rule = contract_fixture_rule(args.contract)
    if args.fixture_rule not in (None, fixture_rule):
        print(
            f"error: contract {args.contract} runs under the {fixture_rule} fixture rule; "
            f"--fixture-rule {args.fixture_rule} is not that contract. Run the other contract "
            "instead of moving this one",
            file=sys.stderr,
        )
        return 2
    not_before = contract_not_before(args.contract)
    as_of = args.as_of
    receipt_path: Path | None = None
    if args.contract == "v0.1" and as_of is None:
        # Both v0.1 fetches must see one boundary; a live `now()` per query
        # would let an outcome arrive between the corrected and the registered
        # read and enter only one of them.
        as_of = _utcnow()
    if args.contract == "v0.1":
        try:
            validate_v0_1_read(args, as_of=as_of)
            receipt_path = record_shadow_read_receipt(args, as_of=as_of)
        except ShadowReadError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"read receipt: {receipt_path}", file=sys.stderr)
    outcome_types = STRICT_OUTCOMES if args.scope == "strict" else TASK_OUTCOMES
    fetch_kwargs = dict(
        window_days=args.window_days,
        lead_minutes=args.lead_minutes,
        outcome_types=outcome_types,
        as_of=as_of,
        not_before=not_before,
    )
    rows = await fetch_rows(args.db_url, fixture_rule=fixture_rule, **fetch_kwargs)
    registered_sensitivity = (
        await fetch_rows(args.db_url, fixture_rule=REGISTERED_FIXTURE_RULE, **fetch_kwargs)
        if args.contract == "v0.1"
        else None
    )
    report = build_report(
        rows,
        scope=args.scope,
        window_days=args.window_days,
        lead_minutes=args.lead_minutes,
        min_bad_clusters=args.min_bad_clusters,
        resamples=args.resamples,
        fixture_rule=fixture_rule,
        seed=args.seed,
        as_of=as_of,
        # v0 renders exactly as it did before v0.1 existed; only v0.1 names itself.
        contract=args.contract if args.contract != "v0" else None,
        not_before=not_before,
        registered_sensitivity=registered_sensitivity,
        read_id=args.read_id if args.contract == "v0.1" else None,
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
