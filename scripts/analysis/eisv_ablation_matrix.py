#!/usr/bin/env python3
"""Run a compact EISV ablation matrix across scopes, windows, and lead times.

This wraps ``eisv_skeptic_report`` so skeptical checks are reproducible instead
of depending on ad-hoc shell loops. It still makes the same limited claim: do
EISV/prior-state candidates beat the boring previous-outcome baseline on both
ranking and calibration in each slice?

The default cohort uses trusted external anchors with a joinable prior-state
snapshot. ``--anchor-scope all`` remains available only for reproducing the
historical contaminated series.

Because it reports the BEST candidate per slice, the table also reports the null
distribution of that maximum when EISV readings are permuted between clusters.
Read `AUC delta` against `Null max median`, never against zero, and read `Bad`
against `Bad clusters`.

Usage:
    python3 scripts/analysis/eisv_ablation_matrix.py \
        --read-protocol exploratory --read-id exploratory-YYYYMMDD-HHMMSS \
        --acknowledge-contamination --windows 30,90 --leads 0,30

Every CLI database read must declare its protocol and unique read ID. Registered
reads also require frozen ``--as-of`` and ``--not-before`` boundaries. The CLI
records an atomic access receipt before querying; repeated IDs fail closed.

Env:
    GOVERNANCE_DATABASE_URL  (default inherited from eisv_skeptic_report; redact in reports)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.analysis.eisv_skeptic_report import (
    DEFAULT_DB_URL,
    STRICT_OUTCOMES,
    TASK_OUTCOMES,
    ModelScore,
    OutcomeRow,
    TELEMETRY_STRATA_DIMENSIONS,
    auc_score,
    brier_score,
    build_model_scores,
    fetch_rows,
    parse_as_of,
    prior_state_cluster_key,
    score_deltas_vs_baseline,
    split_rows_by_telemetry_dimension,
    summarize_conclusion,
)
from scripts.analysis.outcome_inventory import (
    harness_lane_from_detail,
    is_controlled_validation_fixture,
)
from src.grounding.outcome_anchors import (
    DEFAULT_FIXTURE_RULE,
    FIXTURE_RULES,
    REGISTERED_FIXTURE_RULE,
    anchored_outcomes_predicate,
    normalize_fixture_rule,
)

DEFAULT_EXCLUDED_HARNESS_LANES = ("beam",)
READ_PROTOCOLS = ("registered", "exploratory", "reproduction")
CONTAMINATING_READ_PROTOCOLS = frozenset({"exploratory", "reproduction"})
READ_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}")
DEFAULT_READ_LEDGER_DIR = Path(
    os.environ.get(
        "UNITARES_OUTCOME_READ_LEDGER_DIR",
        str(Path.home() / ".local" / "state" / "unitares" / "outcome-reads"),
    )
)


class ReadProtocolError(ValueError):
    """Raised before data access when a live-read protocol is incomplete."""


@dataclass(frozen=True)
class DeltaUncertainty:
    """Bootstrap/permutation uncertainty for one paired candidate delta."""

    paired_n: int
    auc_delta_ci: tuple[float, float] | None
    brier_improvement_ci: tuple[float, float] | None
    brier_permutation_p: float | None


@dataclass(frozen=True)
class AblationMatrixRow:
    scope: str
    window_days: int
    lead_minutes: float
    # Row count in this slice under the *configured anchor scope*. It was called
    # `trusted` and rendered under a "Trusted" header while the matrix passed no
    # anchor predicate at all, so the number printed was the unanchored count.
    rows: int
    bad: int
    bad_clusters: int
    bad_agents: int
    prior_state: int
    prior_risk: int
    baseline_auc: float | None
    baseline_brier: float | None
    best_candidate: str | None
    best_auc_delta: float | None
    best_brier_improvement: float | None
    beats_both: bool
    conclusion: str
    agents: int | None = None
    inference_class: str = "UNASSESSED"
    best_auc_delta_ci: tuple[float, float] | None = None
    best_brier_improvement_ci: tuple[float, float] | None = None
    best_brier_permutation_p: float | None = None
    harness_lane: str | None = None
    selective_null_median: float | None = None
    selective_null_p95: float | None = None
    selective_p: float | None = None
    selective_null_clusters: int | None = None
    anchor_scope: str | None = None
    telemetry_envelope: int | None = None
    telemetry_dimension: str | None = None
    telemetry_stratum: str | None = None


def _fmt_float(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def _fmt_lead(value: float) -> str:
    return f"{value:g}"


def _fmt_ci(value: tuple[float, float] | None, digits: int) -> str:
    if value is None:
        return "-"
    low, high = value
    return f"[{low:.{digits}f}, {high:.{digits}f}]"


def _redact_sensitive_report_text(text: str) -> str:
    """Redact credential-shaped substrings before report storage/stdout."""
    redacted = re.sub(
        r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+):([^\s/@]+)@",
        r"\1:[REDACTED]@",
        text,
    )
    return re.sub(
        r"(?i)\b(api[_-]?key|passwd|password|secret|token)\s*([:=])\s*[^\s,;|]+",
        r"\1\2[REDACTED]",
        redacted,
    )


def _baseline(scores: Sequence[ModelScore]) -> ModelScore | None:
    return next(
        (score for score in scores if score.name == "previous_outcome_bad"), None
    )


def _paired_vectors(
    baseline: ModelScore,
    candidate: ModelScore,
) -> tuple[list[int], list[float], list[float], list[float], list[float]]:
    """Return paired y/candidate/baseline vectors over candidate-covered rows."""

    baseline_by_key = {key: idx for idx, key in enumerate(baseline.scored_row_keys)}
    y_true: list[int] = []
    candidate_prob: list[float] = []
    candidate_auc_score: list[float] = []
    baseline_prob: list[float] = []
    baseline_auc_score: list[float] = []
    for candidate_idx, key in enumerate(candidate.scored_row_keys):
        baseline_idx = baseline_by_key.get(key)
        if baseline_idx is None:
            continue
        if baseline.y_true[baseline_idx] != candidate.y_true[candidate_idx]:
            continue
        y_true.append(candidate.y_true[candidate_idx])
        candidate_prob.append(candidate.y_prob[candidate_idx])
        candidate_auc_score.append(candidate.y_auc_score[candidate_idx])
        baseline_prob.append(baseline.y_prob[baseline_idx])
        baseline_auc_score.append(baseline.y_auc_score[baseline_idx])
    return (
        y_true,
        candidate_prob,
        candidate_auc_score,
        baseline_prob,
        baseline_auc_score,
    )


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * fraction
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    weight = pos - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def estimate_delta_uncertainty(
    baseline: ModelScore,
    candidate: ModelScore,
    *,
    resamples: int = 200,
    seed: int = 0,
    confidence: float = 0.95,
) -> DeltaUncertainty | None:
    """Estimate paired delta uncertainty with bootstrap CIs and permutation p.

    The bootstrap resamples paired rows with replacement and reports confidence
    intervals for AUC delta and Brier improvement. The permutation p-value is a
    paired sign-flip test over per-row Brier improvements.
    """

    y_true, candidate_prob, candidate_auc, baseline_prob, baseline_auc = (
        _paired_vectors(
            baseline,
            candidate,
        )
    )
    n = len(y_true)
    if n == 0 or resamples <= 0:
        return None

    # AUC is resampled on the fitted probabilities for BOTH sides, matching
    # `score_deltas_vs_baseline`. The raw-feature vectors stay unused here on
    # purpose: ranking the candidate by its raw feature and the baseline by its
    # fitted step function is the asymmetry this CI is supposed to bound.
    del candidate_auc, baseline_auc
    rng = random.Random(seed)
    auc_deltas: list[float] = []
    brier_improvements: list[float] = []
    for _ in range(resamples):
        sample_indices = [rng.randrange(n) for _ in range(n)]
        sample_true = [y_true[idx] for idx in sample_indices]
        sample_candidate_prob = [candidate_prob[idx] for idx in sample_indices]
        sample_baseline_prob = [baseline_prob[idx] for idx in sample_indices]
        candidate_auc_value = auc_score(sample_true, sample_candidate_prob)
        baseline_auc_value = auc_score(sample_true, sample_baseline_prob)
        if candidate_auc_value is not None and baseline_auc_value is not None:
            auc_deltas.append(candidate_auc_value - baseline_auc_value)
        candidate_brier = brier_score(sample_true, sample_candidate_prob)
        baseline_brier = brier_score(sample_true, sample_baseline_prob)
        if candidate_brier is not None and baseline_brier is not None:
            brier_improvements.append(baseline_brier - candidate_brier)

    alpha = (1.0 - confidence) / 2.0
    auc_ci = None
    if auc_deltas:
        low = _percentile(auc_deltas, alpha)
        high = _percentile(auc_deltas, 1.0 - alpha)
        auc_ci = None if low is None or high is None else (low, high)
    brier_ci = None
    if brier_improvements:
        low = _percentile(brier_improvements, alpha)
        high = _percentile(brier_improvements, 1.0 - alpha)
        brier_ci = None if low is None or high is None else (low, high)

    per_row_improvements = [
        (base - truth) ** 2 - (cand - truth) ** 2
        for truth, cand, base in zip(y_true, candidate_prob, baseline_prob)
    ]
    observed = sum(per_row_improvements) / n
    extreme = 0
    for _ in range(resamples):
        null_mean = (
            sum(
                value if rng.choice((True, False)) else -value
                for value in per_row_improvements
            )
            / n
        )
        if abs(null_mean) >= abs(observed):
            extreme += 1
    permutation_p = (extreme + 1) / (resamples + 1)

    return DeltaUncertainty(
        paired_n=n,
        auc_delta_ci=auc_ci,
        brier_improvement_ci=brier_ci,
        brier_permutation_p=permutation_p,
    )


#: Prior-state fields that constitute one agent's EISV reading. They are
#: constant within a (agent, snapshot) cluster, which is what lets the null
#: below permute them as a block.
PRIOR_STATE_FIELDS = (
    "prior_state_age_seconds",
    "prior_risk",
    "prior_phi",
    "prior_verdict",
    "prior_coherence",
    "prior_e",
    "prior_i",
    "prior_s",
    "prior_v",
    "n_prior_snapshots",
    "prior_s_disp",
    "prior_e_disp",
    "prior_i_disp",
    "prior_v_disp",
    "prior_risk_disp",
)


@dataclass(frozen=True)
class SelectiveNull:
    """Null distribution of the *reported* statistic: max delta over candidates.

    The matrix reports `max(deltas)`, not a pre-registered candidate, so the
    honest reference is the distribution of that maximum under no association --
    not zero. With ~7 candidates on a few dozen paired rows, noise alone
    routinely yields a sizeable best-candidate lift.

    The permutation reassigns whole EISV readings between (agent, snapshot)
    clusters and leaves labels untouched. Two consequences make this the right
    null here rather than shuffling `is_bad`:

    * The previous-outcome baseline depends only on labels, so it is identical
      in every resample. The null therefore isolates exactly the question asked:
      does the EISV block add anything over the baseline?
    * Prior state is constant within a cluster, so a row-level shuffle would
      break a dependence the real data has and understate the null. Permuting at
      cluster granularity keeps cluster sizes and label structure exact.

    `clusters` is the number of permutable units. Few clusters bound how small
    the p-value can be, so read it alongside the value.
    """

    resamples: int
    clusters: int
    median: float | None
    p95: float | None
    selective_p: float | None


def _cluster_key(row: OutcomeRow) -> tuple[str, str | int | None]:
    """Return the (agent, prior-state snapshot) identity of a row."""
    return prior_state_cluster_key(row)


def _cluster_sort_key(key: tuple[str, str | int | None]) -> tuple[str, int, str, int]:
    """Total order over a deliberately heterogeneous cluster key.

    The snapshot half of the key is a measurement-id ``str`` when provenance
    telemetry carries one and a rounded-epoch ``int`` when it does not, so any
    cohort holding both kinds makes a naive ``key[1] or 0`` compare ``str``
    against ``int``. Rank by type first, then compare within type.

    This is ordering, not a statistic. ``keys`` exists so a given RNG seed
    reproduces the same permutation; the null is over random reassignment of
    prior-state blocks between clusters, so every deterministic total order is
    equally valid. Nothing here changes which clusters exist, how many there
    are, or what is estimated. ``None`` still sorts last within an agent, as
    the previous expression made it.
    """
    agent, snapshot = key
    if snapshot is None:
        return (agent, 2, "", 0)
    if isinstance(snapshot, str):
        return (agent, 0, snapshot, 0)
    return (agent, 1, "", snapshot)


def _best_delta_value(
    rows: Sequence[OutcomeRow],
    *,
    train_fraction: float,
    min_feature_rows: int,
) -> float | None:
    deltas = score_deltas_vs_baseline(
        build_model_scores(
            rows,
            train_fraction=train_fraction,
            min_feature_rows=min_feature_rows,
        )
    )
    if not deltas:
        return None
    return max(
        deltas,
        key=lambda delta: (
            delta.beats_baseline,
            delta.auc_delta,
            delta.brier_improvement,
        ),
    ).auc_delta


def estimate_selective_null(
    rows: Sequence[OutcomeRow],
    *,
    observed_best_delta: float | None,
    resamples: int,
    seed: int = 0,
    train_fraction: float = 0.7,
    min_feature_rows: int = 30,
) -> SelectiveNull | None:
    """Permute EISV readings across clusters and collect the max-over-candidates."""
    if resamples <= 0 or observed_best_delta is None or not rows:
        return None

    # One representative EISV reading per cluster; constant within, by construction.
    blocks: dict[tuple[str, str | int | None], dict[str, Any]] = {}
    for row in rows:
        blocks.setdefault(
            _cluster_key(row),
            {field: getattr(row, field) for field in PRIOR_STATE_FIELDS},
        )
    keys = sorted(blocks, key=_cluster_sort_key)
    if len(keys) < 3:
        # Fewer than three permutable units cannot produce a meaningful null.
        return SelectiveNull(
            resamples=0, clusters=len(keys), median=None, p95=None, selective_p=None
        )

    rng = random.Random(seed)
    null_best: list[float] = []
    for _ in range(resamples):
        shuffled = keys[:]
        rng.shuffle(shuffled)
        remap = dict(zip(keys, shuffled))
        permuted = [replace(row, **blocks[remap[_cluster_key(row)]]) for row in rows]
        value = _best_delta_value(
            permuted,
            train_fraction=train_fraction,
            min_feature_rows=min_feature_rows,
        )
        if value is not None:
            null_best.append(value)

    if not null_best:
        return SelectiveNull(
            resamples=0, clusters=len(keys), median=None, p95=None, selective_p=None
        )

    at_least_as_extreme = sum(1 for value in null_best if value >= observed_best_delta)
    return SelectiveNull(
        resamples=len(null_best),
        clusters=len(keys),
        median=_percentile(null_best, 0.5),
        p95=_percentile(null_best, 0.95),
        selective_p=(at_least_as_extreme + 1) / (len(null_best) + 1),
    )


def classify_inference_with_selective_null(
    selective_null: SelectiveNull | None,
    *,
    alpha: float = 0.05,
) -> str:
    """Return the narrow evidence class licensed by the selective-null read."""

    if selective_null is None or selective_null.selective_p is None:
        return "UNASSESSED"
    if selective_null.selective_p > alpha:
        return "NON_DETECTION"
    return "SIGNAL_CANDIDATE"


def qualify_conclusion_with_selective_null(
    conclusion: str,
    *,
    best_auc_delta: float | None,
    selective_null: SelectiveNull | None,
    alpha: float = 0.05,
) -> str:
    """Attach the evidence class to the heuristic summary that gets quoted.

    `summarize_conclusion` compares the best candidate against fixed thresholds,
    which is a comparison to zero. The reported delta is a maximum over ~7
    candidates, so zero is the wrong reference and "KEEP TESTING" fires on noise.
    The conclusion string is the part that gets quoted downstream, so it has to
    carry the qualification -- putting it only in a column has already proven
    insufficient.
    """
    if (
        selective_null is None
        or selective_null.selective_p is None
        or selective_null.median is None
        or best_auc_delta is None
    ):
        return (
            "UNASSESSED: a selection-aware null was not available, so this row "
            "licenses no inferential conclusion. Heuristic summary was: "
            f"{conclusion}"
        )
    detail = (
        f"selective p={selective_null.selective_p:.3f}, "
        f"null max median={selective_null.median:.3f}, "
        f"{selective_null.clusters} permutable clusters"
    )
    if selective_null.selective_p > alpha:
        return (
            f"NON-DETECTION ({detail}): the reported lift is not separated from "
            "the best-of-candidates selective null. This does not establish no "
            "effect or refutation; scientific status remains INCONCLUSIVE until "
            "read-specific power and protocol are established. Heuristic summary "
            f"was: {conclusion}"
        )
    return (
        f"SIGNAL CANDIDATE ({detail}): the reported lift clears the selective "
        "null. This is not confirmatory without a registered protocol and "
        f"read-specific power. Heuristic summary was: {conclusion}"
    )


def count_bad_clusters(rows: Sequence[OutcomeRow]) -> tuple[int, int]:
    """Return (bad prior-state permutation blocks, distinct bad agents).

    A cluster is one (agent, prior-state snapshot) pair. Prior state is constant
    within a cluster, so every candidate feature is constant across its rows.
    Reporting only `Bad` would overcount distinct feature readings in an
    edit-test-retry burst. The block count does not establish independence of
    outcomes between clusters.
    """
    clusters: set[tuple[str, int | None]] = set()
    agents: set[str] = set()
    for row in rows:
        if not row.is_bad:
            continue
        agents.add(row.agent_id)
        clusters.add(_cluster_key(row))
    return len(clusters), len(agents)


def filter_rows_for_validation(
    rows: Sequence[OutcomeRow],
    *,
    exclude_harness_lanes: Sequence[str] = DEFAULT_EXCLUDED_HARNESS_LANES,
    fixture_rule: str = DEFAULT_FIXTURE_RULE,
) -> list[OutcomeRow]:
    """Exclude runtime-harness telemetry from EISV predictive slices.

    Harness rows remain visible in the outcome inventory, but the ablation matrix
    is about prior-state/EISV predictive lift. A runtime harness such as BEAM can
    emit many externally verified task outcomes without a matching agent-state
    trajectory; keeping it in the same slice can look like an EISV signal or a
    coverage collapse when it is really instrumentation.
    """
    excluded = {str(lane) for lane in exclude_harness_lanes if str(lane)}
    eligible_rows = [
        row
        for row in rows
        if not is_controlled_validation_fixture(
            row.detail, include_declared_purpose=False, rule=fixture_rule
        )
    ]
    if not excluded:
        return eligible_rows
    return [
        row
        for row in eligible_rows
        if harness_lane_from_detail(row.detail) not in excluded
    ]


def split_rows_by_harness_lane(
    rows: Sequence[OutcomeRow],
) -> dict[str, list[OutcomeRow]]:
    """Group rows by explicit runtime harness lane, defaulting to substrate."""
    grouped: dict[str, list[OutcomeRow]] = {}
    for row in rows:
        grouped.setdefault(harness_lane_from_detail(row.detail), []).append(row)
    return dict(sorted(grouped.items()))


def build_matrix_row(
    rows: Sequence[OutcomeRow],
    *,
    scope: str,
    window_days: int,
    lead_minutes: float,
    train_fraction: float = 0.7,
    min_feature_rows: int = 30,
    uncertainty_resamples: int = 0,
    uncertainty_seed: int = 0,
    harness_lane: str | None = None,
    selective_null_resamples: int = 0,
    anchor_scope: str | None = None,
    telemetry_dimension: str | None = None,
    telemetry_stratum: str | None = None,
) -> AblationMatrixRow:
    """Summarize one scope/window/lead ablation slice."""
    scores = build_model_scores(
        rows,
        train_fraction=train_fraction,
        min_feature_rows=min_feature_rows,
    )
    baseline = _baseline(scores)
    deltas = score_deltas_vs_baseline(scores)
    best_delta = max(
        deltas,
        key=lambda delta: (
            delta.beats_baseline,
            delta.auc_delta,
            delta.brier_improvement,
        ),
        default=None,
    )
    uncertainty = None
    if best_delta and uncertainty_resamples > 0 and baseline:
        candidate_score = next(
            (score for score in scores if score.name == best_delta.name), None
        )
        if candidate_score:
            uncertainty = estimate_delta_uncertainty(
                baseline,
                candidate_score,
                resamples=uncertainty_resamples,
                seed=uncertainty_seed,
            )
    selective_null = estimate_selective_null(
        rows,
        observed_best_delta=best_delta.auc_delta if best_delta else None,
        resamples=selective_null_resamples,
        seed=uncertainty_seed,
        train_fraction=train_fraction,
        min_feature_rows=min_feature_rows,
    )
    bad_clusters, bad_agents = count_bad_clusters(rows)
    return AblationMatrixRow(
        scope=scope,
        window_days=window_days,
        lead_minutes=lead_minutes,
        rows=len(rows),
        bad=sum(int(row.is_bad) for row in rows),
        bad_clusters=bad_clusters,
        bad_agents=bad_agents,
        prior_state=sum(1 for row in rows if row.prior_state_age_seconds is not None),
        prior_risk=sum(1 for row in rows if row.prior_risk is not None),
        baseline_auc=baseline.auc if baseline else None,
        baseline_brier=baseline.brier if baseline else None,
        best_candidate=best_delta.name if best_delta else None,
        best_auc_delta=best_delta.auc_delta if best_delta else None,
        best_brier_improvement=(best_delta.brier_improvement if best_delta else None),
        beats_both=bool(best_delta and best_delta.beats_baseline),
        conclusion=qualify_conclusion_with_selective_null(
            summarize_conclusion(rows, scores),
            best_auc_delta=best_delta.auc_delta if best_delta else None,
            selective_null=selective_null,
        ),
        agents=len({row.agent_id for row in rows}),
        inference_class=classify_inference_with_selective_null(selective_null),
        best_auc_delta_ci=uncertainty.auc_delta_ci if uncertainty else None,
        best_brier_improvement_ci=(
            uncertainty.brier_improvement_ci if uncertainty else None
        ),
        best_brier_permutation_p=(
            uncertainty.brier_permutation_p if uncertainty else None
        ),
        harness_lane=harness_lane,
        selective_null_median=selective_null.median if selective_null else None,
        selective_null_p95=selective_null.p95 if selective_null else None,
        selective_p=selective_null.selective_p if selective_null else None,
        selective_null_clusters=selective_null.clusters if selective_null else None,
        anchor_scope=anchor_scope,
        telemetry_envelope=sum(
            1 for row in rows if row.prior_telemetry_schema is not None
        ),
        telemetry_dimension=telemetry_dimension,
        telemetry_stratum=telemetry_stratum,
    )


def format_matrix_report(
    rows: Sequence[AblationMatrixRow],
    *,
    generated_at: datetime | None = None,
    excluded_harness_lanes: Sequence[str] = (),
    anchor_scope: str | None = None,
    as_of: datetime | None = None,
    read_protocol: str | None = None,
    read_id: str | None = None,
    protocol_not_before: datetime | None = None,
    contamination_acknowledged: bool = False,
    fixture_rule: str | None = None,
) -> str:
    """Render a compact markdown table for skeptical multi-slice reporting."""
    generated_at = generated_at or datetime.now(timezone.utc)
    lines = [
        "# EISV Ablation Matrix",
        "",
        f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
    ]
    if read_protocol and read_id:
        lines.extend(
            [
                f"Read ID: `{read_id}`",
                f"Read protocol: `{read_protocol}`",
            ]
        )
        if protocol_not_before is not None:
            lines.append(
                "Not-before boundary: "
                f"`{protocol_not_before.astimezone(timezone.utc).isoformat()}`"
            )
        if read_protocol in CONTAMINATING_READ_PROTOCOLS:
            lines.append(
                "Confirmatory authority: **none** — this read explicitly "
                "acknowledged protocol contamination."
            )
        else:
            lines.append(
                "Confirmatory authority: determined by the registered design, "
                "its disclosed prior accesses, and read-specific power — not by "
                "the protocol label alone."
            )
        if contamination_acknowledged:
            lines.append("Contamination acknowledgement: recorded")
        lines.append("")
    excluded = tuple(str(lane) for lane in excluded_harness_lanes if str(lane))
    if excluded:
        lines.extend(
            [
                "Excluded harness lanes: "
                + ", ".join(f"`{lane}`" for lane in excluded),
                "",
            ]
        )
    if anchor_scope:
        lines.extend(
            [
                f"Anchor scope: `{anchor_scope}`"
                + (
                    "  (**contaminated** -- includes self-referential and "
                    "snapshot-less rows; not the trusted-anchor population)"
                    if anchor_scope == "all"
                    else ""
                ),
                "",
            ]
        )
    if fixture_rule:
        lines.extend(
            [
                f"Fixture rule: `{fixture_rule}`"
                + (
                    "  (the pre-registered predicate: a scraped confidence classifies "
                    "as fixture traffic)"
                    if fixture_rule == REGISTERED_FIXTURE_RULE
                    else "  (scraped-confidence rows are validation-visible; not the "
                    "registered predicate)"
                ),
                "",
            ]
        )
    if as_of is not None:
        lines.extend(
            [
                f"Data boundary: `{as_of.astimezone(timezone.utc).isoformat()}` (frozen)",
                "Frozen runs exclude mutable present-day identity metadata from fixture classification.",
                "",
            ]
        )
    grouped_by_harness_lane = any(row.harness_lane is not None for row in rows)
    if grouped_by_harness_lane:
        lines.extend(["Harness lane mode: grouped", ""])
    grouped_by_telemetry = any(row.telemetry_dimension is not None for row in rows)
    if grouped_by_telemetry:
        dimensions = [
            dimension
            for dimension in TELEMETRY_STRATA_DIMENSIONS
            if any(row.telemetry_dimension == dimension for row in rows)
        ]
        lines.extend(
            [
                "Telemetry strata mode: marginal (overall plus "
                + ", ".join(f"`{dimension}`" for dimension in dimensions)
                + ")",
                "Rows recur across dimensions. These exploratory strata are not "
                "multiple-comparison corrected across strata.",
                "Enforcement strata are intervention-conditioned audit views, not "
                "causal estimates; enforcement is never added as a predictor.",
                "",
            ]
        )
    columns = [
        *(["Lane"] if grouped_by_harness_lane else []),
        *(["Telemetry dimension", "Telemetry stratum"] if grouped_by_telemetry else []),
        "Scope",
        "Window days",
        "Lead min",
        "Rows",
        "Bad",
        "Bad clusters",
        "Bad agents",
        "Agents",
        "Prior state",
        "Prior risk",
        "Envelope",
        "Baseline AUC",
        "Baseline Brier",
        "Best EISV/prior model",
        "AUC delta",
        "AUC delta 95% CI",
        "Null max median",
        "Null max p95",
        "Selective p",
        "Null clusters",
        "Brier improvement",
        "Brier improvement 95% CI",
        "Brier perm p",
        "Beats both?",
        "Inference class",
        "Conclusion",
    ]
    lines.extend(
        [
            "Positive AUC delta means better ranking than `previous_outcome_bad`; positive Brier improvement means lower probability error. `Beats both?` is the conservative quick read.",
            "",
            "**Read `AUC delta` against `Null max median`, never against zero.** The "
            "reported delta is the maximum over ~7 candidates, so its null is the "
            "distribution of that maximum when EISV readings carry no information, "
            "not 0. A delta below the null median is compatible with the "
            "selection-aware null distribution. `Selective p` "
            "tests exactly the reported statistic; `Brier perm p` tests only Brier and "
            "has never tested the AUC delta.",
            "",
            "The null permutes whole EISV readings between (agent, prior-state "
            "snapshot) clusters and leaves labels fixed, so the previous-outcome "
            "baseline is identical in every resample. `Null clusters` is how many "
            "units were permutable -- few clusters bound how small `Selective p` can "
            "get, so read them together.",
            "",
            "**Read `Bad` against `Bad clusters`.** Features are constant within a "
            "cluster, so an edit-test-retry burst does not contribute N distinct "
            "feature readings. Clusters are permutation blocks, not proof of "
            "independent outcomes; report bad rows and agents alongside them.",
            "",
            "**Inference class is narrower than a project verdict.** "
            "`NON_DETECTION` means this read did not separate the selected candidate "
            "from its selective null; without adequate read-specific power it remains "
            "scientifically inconclusive. `SIGNAL_CANDIDATE` is not confirmation, and "
            "`UNASSESSED` licenses no inference.",
            "",
            "| " + " | ".join(columns) + " |",
            "|" + "|".join("---" for _ in columns) + "|",
        ]
    )
    if rows:
        for row in rows:
            cells = [
                *([row.harness_lane or "substrate"] if grouped_by_harness_lane else []),
                *(
                    [
                        row.telemetry_dimension or "overall",
                        row.telemetry_stratum or "all",
                    ]
                    if grouped_by_telemetry
                    else []
                ),
                row.scope,
                str(row.window_days),
                _fmt_lead(row.lead_minutes),
                str(row.rows),
                str(row.bad),
                str(row.bad_clusters),
                str(row.bad_agents),
                "-" if row.agents is None else str(row.agents),
                str(row.prior_state),
                str(row.prior_risk),
                "-" if row.telemetry_envelope is None else str(row.telemetry_envelope),
                _fmt_float(row.baseline_auc, 3),
                _fmt_float(row.baseline_brier, 4),
                row.best_candidate or "-",
                _fmt_float(row.best_auc_delta, 3),
                _fmt_ci(row.best_auc_delta_ci, 3),
                _fmt_float(row.selective_null_median, 3),
                _fmt_float(row.selective_null_p95, 3),
                _fmt_float(row.selective_p, 3),
                (
                    "-"
                    if row.selective_null_clusters is None
                    else str(row.selective_null_clusters)
                ),
                _fmt_float(row.best_brier_improvement, 4),
                _fmt_ci(row.best_brier_improvement_ci, 4),
                _fmt_float(row.best_brier_permutation_p, 3),
                "yes" if row.beats_both else "no",
                row.inference_class,
                row.conclusion,
            ]
            lines.append("| " + " | ".join(cells) + " |")
    else:
        empty_cells = ["-"] * len(columns)
        lines.append("| " + " | ".join(empty_cells) + " |")
    lines.extend(
        [
            "",
            "Interpretation rule: this matrix evaluates online agent-state estimation, not bad-action prevention. `Bad` means rows labeled `is_bad=true` by external/rubric evidence; it is not a moral verdict or a count of prevented outcomes.",
            "It only checks whether EISV/prior-state fields add measurable predictive signal over a simple previous-outcome baseline across slices; it does not make EISV an outcome oracle, bad-verdict authority, or bad-agent detector.",
        ]
    )
    return "\n".join(lines)


def _parse_int_list(raw: str) -> list[int]:
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def _parse_float_list(raw: str) -> list[float]:
    return [float(part.strip()) for part in raw.split(",") if part.strip()]


def _parse_string_list(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_scope_list(raw: str) -> list[str]:
    scopes = [part.strip() for part in raw.split(",") if part.strip()]
    invalid = [scope for scope in scopes if scope not in {"strict", "task"}]
    if invalid:
        raise argparse.ArgumentTypeError(f"invalid scope(s): {', '.join(invalid)}")
    return scopes


def _parse_telemetry_strata(raw: str) -> tuple[str, ...]:
    dimensions = _parse_string_list(raw)
    invalid = [
        dimension
        for dimension in dimensions
        if dimension not in TELEMETRY_STRATA_DIMENSIONS
    ]
    if invalid:
        raise argparse.ArgumentTypeError(
            f"invalid telemetry strata: {', '.join(invalid)}"
        )
    return dimensions


def validate_read_protocol(
    args: argparse.Namespace,
    *,
    now: datetime | None = None,
) -> datetime:
    """Validate live-read authority before any database query can begin."""

    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        raise ReadProtocolError("protocol validation time must be timezone-aware")

    errors: list[str] = []
    if args.read_protocol is None:
        errors.append("--read-protocol is required for every database read")
    if not args.read_id:
        errors.append("--read-id is required for an immutable access receipt")
    elif READ_ID_PATTERN.fullmatch(args.read_id) is None:
        errors.append(
            "--read-id must be 3-128 characters using letters, digits, '.', '_', ':', or '-'"
        )

    if args.as_of is not None and args.as_of > checked_at:
        errors.append("--as-of cannot be in the future at access time")

    if args.read_protocol == "registered":
        if args.not_before is None:
            errors.append("registered reads require --not-before")
        elif checked_at < args.not_before:
            errors.append(
                "registered read is early: not-before boundary is "
                f"{args.not_before.astimezone(timezone.utc).isoformat()}"
            )
        if args.as_of is None:
            errors.append("registered reads require a frozen --as-of boundary")
        if getattr(args, "fixture_rule", None) not in (None, REGISTERED_FIXTURE_RULE):
            errors.append(
                "registered reads run with the registered fixture rule; the "
                "pre-declared sensitivity cohort is a separate "
                "--read-protocol reproduction --fixture-rule corrected read"
            )
    elif (
        args.read_protocol in CONTAMINATING_READ_PROTOCOLS
        and not args.acknowledge_contamination
    ):
        errors.append(f"{args.read_protocol} reads require --acknowledge-contamination")

    if errors:
        raise ReadProtocolError("; ".join(errors))
    return checked_at.astimezone(timezone.utc)


def effective_fixture_rule(args: argparse.Namespace) -> str:
    """The fixture rule a read runs under.

    A registered read is pinned to the rule it was registered with and rejects
    any other value, so no default change can alter its cohort. Every other
    protocol takes the explicit ``--fixture-rule`` or the shared default, which
    is also ``registered``; the pre-declared sensitivity cohort passes
    ``--fixture-rule corrected`` explicitly.
    """
    if getattr(args, "read_protocol", None) == "registered":
        return REGISTERED_FIXTURE_RULE
    explicit = getattr(args, "fixture_rule", None)
    return normalize_fixture_rule(explicit) if explicit else DEFAULT_FIXTURE_RULE


def record_read_receipt(
    args: argparse.Namespace,
    *,
    exclude_harness_lanes: Sequence[str],
    now: datetime | None = None,
) -> tuple[Path, datetime]:
    """Atomically record one declared access before querying outcome data."""

    checked_at = validate_read_protocol(args, now=now)
    ledger_dir = Path(args.read_ledger_dir)
    try:
        ledger_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ReadProtocolError(
            f"cannot create read ledger {ledger_dir}: {exc}"
        ) from exc

    digest = hashlib.sha256(args.read_id.encode("utf-8")).hexdigest()
    receipt_path = ledger_dir / f"{digest}.json"
    receipt = {
        "schema": "unitares.outcome_read_receipt.v1",
        "status": "access_started",
        "read_id": args.read_id,
        "read_protocol": args.read_protocol,
        "recorded_at": checked_at.isoformat(),
        "not_before": args.not_before.isoformat() if args.not_before else None,
        "as_of": args.as_of.isoformat() if args.as_of else None,
        "contamination_acknowledged": bool(args.acknowledge_contamination),
        "parameters": {
            "scopes": list(args.scopes),
            "windows": list(args.windows),
            "leads": list(args.leads),
            "train_fraction": args.train_fraction,
            "fixture_rule": effective_fixture_rule(args),
            "min_feature_rows": args.min_feature_rows,
            "anchor_scope": args.anchor_scope,
            "exclude_harness_lanes": list(exclude_harness_lanes),
            "group_by_harness_lane": bool(args.group_by_harness_lane),
            "telemetry_strata": list(args.telemetry_strata),
            "uncertainty_resamples": args.uncertainty_resamples,
            "uncertainty_seed": args.uncertainty_seed,
            "selective_null_resamples": args.selective_null_resamples,
        },
    }
    payload = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        fd = os.open(
            os.fspath(receipt_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise ReadProtocolError(
            f"read id {args.read_id!r} already has a receipt; repeated reads need "
            "a new ID and explicit disclosure"
        ) from exc
    except OSError as exc:
        raise ReadProtocolError(
            f"cannot record read receipt {receipt_path}: {exc}"
        ) from exc
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise ReadProtocolError(
                    f"short write while recording read receipt {receipt_path}"
                )
            remaining = remaining[written:]
        os.fsync(fd)
    except OSError as exc:
        raise ReadProtocolError(
            f"cannot finish read receipt {receipt_path}: {exc}"
        ) from exc
    finally:
        os.close(fd)
    return receipt_path, checked_at


async def build_matrix_from_db(
    db_url: str,
    *,
    scopes: Sequence[str],
    windows: Sequence[int],
    leads: Sequence[float],
    train_fraction: float = 0.7,
    min_feature_rows: int = 30,
    exclude_harness_lanes: Sequence[str] = DEFAULT_EXCLUDED_HARNESS_LANES,
    group_by_harness_lane: bool = False,
    uncertainty_resamples: int = 0,
    uncertainty_seed: int = 0,
    selective_null_resamples: int = 0,
    anchor_scope: str = "trusted",
    telemetry_strata: Sequence[str] = (),
    as_of: datetime | None = None,
    fixture_rule: str = DEFAULT_FIXTURE_RULE,
) -> list[AblationMatrixRow]:
    fixture_rule = normalize_fixture_rule(fixture_rule)
    matrix_rows: list[AblationMatrixRow] = []
    excluded = {str(lane) for lane in exclude_harness_lanes if str(lane)}
    anchor_predicate = (
        None
        if anchor_scope == "all"
        else anchored_outcomes_predicate(
            include_soft=(anchor_scope == "soft"), table_alias="o"
        )
    )
    for scope in scopes:
        outcome_types = STRICT_OUTCOMES if scope == "strict" else TASK_OUTCOMES
        for window_days in windows:
            for lead_minutes in leads:
                fetched_rows = await fetch_rows(
                    db_url,
                    fixture_rule=fixture_rule,
                    window_days=window_days,
                    lead_minutes=lead_minutes,
                    outcome_types=outcome_types,
                    anchor_predicate=anchor_predicate,
                    as_of=as_of,
                    include_identity_metadata=as_of is None,
                )
                if group_by_harness_lane:
                    lane_groups = {
                        lane: lane_rows
                        for lane, lane_rows in split_rows_by_harness_lane(
                            fetched_rows
                        ).items()
                        if lane not in excluded
                    }
                else:
                    lane_groups = {
                        None: filter_rows_for_validation(
                            fetched_rows,
                            exclude_harness_lanes=exclude_harness_lanes,
                            fixture_rule=fixture_rule,
                        )
                    }
                for harness_lane, outcome_rows in lane_groups.items():
                    telemetry_groups: list[
                        tuple[str | None, str | None, Sequence[OutcomeRow]]
                    ] = [(None, None, outcome_rows)]
                    for dimension in dict.fromkeys(telemetry_strata):
                        telemetry_groups.extend(
                            (dimension, stratum, stratum_rows)
                            for stratum, stratum_rows in split_rows_by_telemetry_dimension(
                                outcome_rows, dimension
                            ).items()
                        )
                    for (
                        telemetry_dimension,
                        telemetry_stratum,
                        stratum_rows,
                    ) in telemetry_groups:
                        matrix_rows.append(
                            build_matrix_row(
                                stratum_rows,
                                scope=scope,
                                window_days=window_days,
                                lead_minutes=lead_minutes,
                                train_fraction=train_fraction,
                                min_feature_rows=min_feature_rows,
                                uncertainty_resamples=uncertainty_resamples,
                                uncertainty_seed=uncertainty_seed,
                                harness_lane=harness_lane,
                                selective_null_resamples=selective_null_resamples,
                                anchor_scope=anchor_scope,
                                telemetry_dimension=telemetry_dimension,
                                telemetry_stratum=telemetry_stratum,
                            )
                        )
    return matrix_rows


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument(
        "--read-protocol",
        choices=READ_PROTOCOLS,
        help=(
            "Required classification for database access. Registered reads need "
            "--not-before and --as-of; exploratory/reproduction reads need an "
            "explicit contamination acknowledgement."
        ),
    )
    parser.add_argument(
        "--read-id",
        help="Unique stable ID used to create an immutable local access receipt.",
    )
    parser.add_argument(
        "--not-before",
        type=parse_as_of,
        help="Earliest UTC instant a registered read may access the database.",
    )
    parser.add_argument(
        "--acknowledge-contamination",
        action="store_true",
        help=(
            "Acknowledge that an exploratory/reproduction read is not confirmatory. "
            "May also disclose known prior access on a registered operational read."
        ),
    )
    parser.add_argument(
        "--read-ledger-dir",
        type=Path,
        default=DEFAULT_READ_LEDGER_DIR,
        help=(
            "Directory for atomic read receipts (default: "
            "$UNITARES_OUTCOME_READ_LEDGER_DIR or local state)."
        ),
    )
    parser.add_argument(
        "--fixture-rule",
        choices=FIXTURE_RULES,
        default=None,
        help=(
            "How the server-stamped calibration_excluded flag is read. Registered "
            "reads are pinned to 'registered' (the predicate they were registered "
            "with) and reject anything else; every other protocol defaults to it "
            "too. 'corrected' keeps rows whose only exclusion is a scraped "
            "confidence and must be asked for explicitly. Recorded in the read "
            "receipt and the report header."
        ),
    )
    parser.add_argument("--scopes", type=_parse_scope_list, default="strict,task")
    parser.add_argument("--windows", type=_parse_int_list, default="30,90,365")
    parser.add_argument("--leads", type=_parse_float_list, default="0,5,30")
    parser.add_argument("--train-fraction", type=float, default=0.7)
    parser.add_argument("--min-feature-rows", type=int, default=30)
    parser.add_argument(
        "--as-of",
        type=parse_as_of,
        help=(
            "Freeze every window at a timezone-aware ISO-8601 boundary. Frozen "
            "runs exclude mutable present-day identity metadata."
        ),
    )
    parser.add_argument(
        "--telemetry-strata",
        type=_parse_telemetry_strata,
        default=(),
        help=(
            "Comma-separated marginal envelope strata: source,warmup,enforcement,missingness. "
            "Overall rows remain in the report."
        ),
    )
    parser.add_argument(
        "--uncertainty-resamples",
        type=int,
        default=0,
        help="Bootstrap/permutation resamples for best-candidate delta uncertainty; 0 disables.",
    )
    parser.add_argument(
        "--uncertainty-seed",
        type=int,
        default=0,
        help="Deterministic seed for bootstrap/permutation uncertainty estimates.",
    )
    parser.add_argument(
        "--selective-null-resamples",
        type=int,
        default=200,
        help=(
            "Permutations used to build the null distribution of the reported "
            "max-over-candidates AUC delta; 0 disables. Whole EISV readings are "
            "reassigned between (agent, prior-state snapshot) clusters and "
            "LABELS ARE HELD FIXED, so the previous-outcome baseline is "
            "identical in every resample. Without it the table implies a null "
            "of zero, which understates the selection bias of reporting a "
            "maximum."
        ),
    )
    parser.add_argument(
        "--anchor-scope",
        choices=("trusted", "soft", "all"),
        default="trusted",
        help=(
            "Which outcomes may anchor the slice (src.grounding.outcome_anchors). "
            "Default 'trusted' requires external_signal plus a joinable EISV "
            "snapshot; 'all' is the legacy contaminated scope retained only "
            "for historical reproduction."
        ),
    )
    parser.add_argument(
        "--exclude-harness-lanes",
        type=_parse_string_list,
        default=None,
        help="Comma-separated runtime harness lanes excluded from predictive slices; empty string includes all.",
    )
    parser.add_argument(
        "--group-by-harness-lane",
        action="store_true",
        help="Run separate substrate/BEAM/runtime-harness slices instead of mixing lanes.",
    )
    parser.add_argument("--output", help="Optional markdown output path")
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    if not (0.1 <= args.train_fraction <= 0.9):
        print("error: --train-fraction must be between 0.1 and 0.9")
        return 2
    if args.exclude_harness_lanes is None:
        exclude_harness_lanes = (
            () if args.group_by_harness_lane else DEFAULT_EXCLUDED_HARNESS_LANES
        )
    else:
        exclude_harness_lanes = args.exclude_harness_lanes
    receipt_path, read_started_at = record_read_receipt(
        args,
        exclude_harness_lanes=exclude_harness_lanes,
    )
    fixture_rule = effective_fixture_rule(args)
    print(f"read receipt: {receipt_path}", file=sys.stderr)
    rows = await build_matrix_from_db(
        args.db_url,
        scopes=args.scopes,
        windows=args.windows,
        leads=args.leads,
        train_fraction=args.train_fraction,
        min_feature_rows=args.min_feature_rows,
        exclude_harness_lanes=exclude_harness_lanes,
        group_by_harness_lane=args.group_by_harness_lane,
        uncertainty_resamples=args.uncertainty_resamples,
        uncertainty_seed=args.uncertainty_seed,
        selective_null_resamples=args.selective_null_resamples,
        anchor_scope=args.anchor_scope,
        telemetry_strata=args.telemetry_strata,
        as_of=args.as_of,
        fixture_rule=fixture_rule,
    )
    report = _redact_sensitive_report_text(
        format_matrix_report(
            rows,
            generated_at=read_started_at,
            excluded_harness_lanes=exclude_harness_lanes,
            anchor_scope=args.anchor_scope,
            as_of=args.as_of,
            read_protocol=args.read_protocol,
            read_id=args.read_id,
            protocol_not_before=args.not_before,
            contamination_acknowledged=args.acknowledge_contamination,
            fixture_rule=fixture_rule,
        )
    )
    payload = (report + "\n").encode("utf-8")
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Store only redacted aggregate report bytes, not raw DB rows or DSNs.
        fd = os.open(os.fspath(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        print(f"Wrote {path}")
    else:
        # Emit only redacted aggregate report bytes, not raw DB rows or DSNs.
        sys.stdout.buffer.write(payload)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except ReadProtocolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
