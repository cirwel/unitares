#!/usr/bin/env python3
"""Run a compact EISV ablation matrix across scopes, windows, and lead times.

This wraps ``eisv_skeptic_report`` so skeptical checks are reproducible instead
of depending on ad-hoc shell loops. It still makes the same limited claim: do
EISV/prior-state candidates beat the boring previous-outcome baseline on both
ranking and calibration in each slice?

Usage:
    python3 scripts/analysis/eisv_ablation_matrix.py --windows 30,90 --leads 0,30
    python3 scripts/analysis/eisv_ablation_matrix.py --scopes strict,task --output data/analysis/eisv_ablation_matrix.md

Env:
    GOVERNANCE_DATABASE_URL  (default inherited from eisv_skeptic_report; redact in reports)
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.analysis.eisv_skeptic_report import (
    DEFAULT_DB_URL,
    STRICT_OUTCOMES,
    TASK_OUTCOMES,
    ModelScore,
    OutcomeRow,
    auc_score,
    brier_score,
    build_model_scores,
    fetch_rows,
    score_deltas_vs_baseline,
    summarize_conclusion,
)
from scripts.analysis.outcome_inventory import harness_lane_from_detail

DEFAULT_EXCLUDED_HARNESS_LANES = ("beam",)


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
    trusted: int
    bad: int
    prior_state: int
    prior_risk: int
    baseline_auc: float | None
    baseline_brier: float | None
    best_candidate: str | None
    best_auc_delta: float | None
    best_brier_improvement: float | None
    beats_both: bool
    conclusion: str
    best_auc_delta_ci: tuple[float, float] | None = None
    best_brier_improvement_ci: tuple[float, float] | None = None
    best_brier_permutation_p: float | None = None


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


def _baseline(scores: Sequence[ModelScore]) -> ModelScore | None:
    return next((score for score in scores if score.name == "previous_outcome_bad"), None)


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
    return y_true, candidate_prob, candidate_auc_score, baseline_prob, baseline_auc_score


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

    y_true, candidate_prob, candidate_auc, baseline_prob, baseline_auc = _paired_vectors(
        baseline,
        candidate,
    )
    n = len(y_true)
    if n == 0 or resamples <= 0:
        return None

    rng = random.Random(seed)
    auc_deltas: list[float] = []
    brier_improvements: list[float] = []
    for _ in range(resamples):
        sample_indices = [rng.randrange(n) for _ in range(n)]
        sample_true = [y_true[idx] for idx in sample_indices]
        sample_candidate_prob = [candidate_prob[idx] for idx in sample_indices]
        sample_candidate_auc = [candidate_auc[idx] for idx in sample_indices]
        sample_baseline_prob = [baseline_prob[idx] for idx in sample_indices]
        sample_baseline_auc = [baseline_auc[idx] for idx in sample_indices]
        candidate_auc_value = auc_score(sample_true, sample_candidate_auc)
        baseline_auc_value = auc_score(sample_true, sample_baseline_auc)
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
        null_mean = sum(
            value if rng.choice((True, False)) else -value
            for value in per_row_improvements
        ) / n
        if abs(null_mean) >= abs(observed):
            extreme += 1
    permutation_p = (extreme + 1) / (resamples + 1)

    return DeltaUncertainty(
        paired_n=n,
        auc_delta_ci=auc_ci,
        brier_improvement_ci=brier_ci,
        brier_permutation_p=permutation_p,
    )


def filter_rows_for_validation(
    rows: Sequence[OutcomeRow],
    *,
    exclude_harness_lanes: Sequence[str] = DEFAULT_EXCLUDED_HARNESS_LANES,
) -> list[OutcomeRow]:
    """Exclude runtime-harness telemetry from EISV predictive slices.

    Harness rows remain visible in the outcome inventory, but the ablation matrix
    is about prior-state/EISV predictive lift. A runtime harness such as BEAM can
    emit many externally verified task outcomes without a matching agent-state
    trajectory; keeping it in the same slice can look like an EISV signal or a
    coverage collapse when it is really instrumentation.
    """
    excluded = {str(lane) for lane in exclude_harness_lanes if str(lane)}
    if not excluded:
        return list(rows)
    return [row for row in rows if harness_lane_from_detail(row.detail) not in excluded]


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
        candidate_score = next((score for score in scores if score.name == best_delta.name), None)
        if candidate_score:
            uncertainty = estimate_delta_uncertainty(
                baseline,
                candidate_score,
                resamples=uncertainty_resamples,
                seed=uncertainty_seed,
            )
    return AblationMatrixRow(
        scope=scope,
        window_days=window_days,
        lead_minutes=lead_minutes,
        trusted=len(rows),
        bad=sum(int(row.is_bad) for row in rows),
        prior_state=sum(1 for row in rows if row.prior_state_age_seconds is not None),
        prior_risk=sum(1 for row in rows if row.prior_risk is not None),
        baseline_auc=baseline.auc if baseline else None,
        baseline_brier=baseline.brier if baseline else None,
        best_candidate=best_delta.name if best_delta else None,
        best_auc_delta=best_delta.auc_delta if best_delta else None,
        best_brier_improvement=(best_delta.brier_improvement if best_delta else None),
        beats_both=bool(best_delta and best_delta.beats_baseline),
        conclusion=summarize_conclusion(rows, scores),
        best_auc_delta_ci=uncertainty.auc_delta_ci if uncertainty else None,
        best_brier_improvement_ci=(
            uncertainty.brier_improvement_ci if uncertainty else None
        ),
        best_brier_permutation_p=(
            uncertainty.brier_permutation_p if uncertainty else None
        ),
    )


def format_matrix_report(
    rows: Sequence[AblationMatrixRow],
    *,
    generated_at: datetime | None = None,
    excluded_harness_lanes: Sequence[str] = (),
) -> str:
    """Render a compact markdown table for skeptical multi-slice reporting."""
    generated_at = generated_at or datetime.now(timezone.utc)
    lines = [
        "# EISV Ablation Matrix",
        "",
        f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
    ]
    excluded = tuple(str(lane) for lane in excluded_harness_lanes if str(lane))
    if excluded:
        lines.extend(
            [
                "Excluded harness lanes: " + ", ".join(f"`{lane}`" for lane in excluded),
                "",
            ]
        )
    lines.extend([
        "Positive AUC delta means better ranking than `previous_outcome_bad`; positive Brier improvement means lower probability error. `Beats both?` is the conservative quick read.",
        "",
        "| Scope | Window days | Lead min | Trusted | Bad | Prior state | Prior risk | Baseline AUC | Baseline Brier | Best EISV/prior model | AUC delta | AUC delta 95% CI | Brier improvement | Brier improvement 95% CI | Brier perm p | Beats both? | Conclusion |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---|---|",
    ])
    if rows:
        for row in rows:
            lines.append(
                "| "
                f"{row.scope} | {row.window_days} | {_fmt_lead(row.lead_minutes)} "
                f"| {row.trusted} | {row.bad} | {row.prior_state} | {row.prior_risk} "
                f"| {_fmt_float(row.baseline_auc, 3)} | {_fmt_float(row.baseline_brier, 4)} "
                f"| {row.best_candidate or '-'} | {_fmt_float(row.best_auc_delta, 3)} "
                f"| {_fmt_ci(row.best_auc_delta_ci, 3)} "
                f"| {_fmt_float(row.best_brier_improvement, 4)} "
                f"| {_fmt_ci(row.best_brier_improvement_ci, 4)} "
                f"| {_fmt_float(row.best_brier_permutation_p, 3)} "
                f"| {'yes' if row.beats_both else 'no'} | {row.conclusion} |"
            )
    else:
        lines.append("| - | - | - | 0 | 0 | 0 | 0 | - | - | - | - | - | - | - | - | no | no rows |")
    lines.extend(
        [
            "",
            "Interpretation rule: this matrix does not validate EISV as ontology. It only checks whether EISV/prior-state fields add measurable predictive signal over a simple previous-outcome baseline across slices.",
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


async def build_matrix_from_db(
    db_url: str,
    *,
    scopes: Sequence[str],
    windows: Sequence[int],
    leads: Sequence[float],
    train_fraction: float = 0.7,
    min_feature_rows: int = 30,
    exclude_harness_lanes: Sequence[str] = DEFAULT_EXCLUDED_HARNESS_LANES,
    uncertainty_resamples: int = 0,
    uncertainty_seed: int = 0,
) -> list[AblationMatrixRow]:
    matrix_rows: list[AblationMatrixRow] = []
    for scope in scopes:
        outcome_types = STRICT_OUTCOMES if scope == "strict" else TASK_OUTCOMES
        for window_days in windows:
            for lead_minutes in leads:
                outcome_rows = filter_rows_for_validation(
                    await fetch_rows(
                        db_url,
                        window_days=window_days,
                        lead_minutes=lead_minutes,
                        outcome_types=outcome_types,
                    ),
                    exclude_harness_lanes=exclude_harness_lanes,
                )
                matrix_rows.append(
                    build_matrix_row(
                        outcome_rows,
                        scope=scope,
                        window_days=window_days,
                        lead_minutes=lead_minutes,
                        train_fraction=train_fraction,
                        min_feature_rows=min_feature_rows,
                        uncertainty_resamples=uncertainty_resamples,
                        uncertainty_seed=uncertainty_seed,
                    )
                )
    return matrix_rows


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--scopes", type=_parse_scope_list, default="strict,task")
    parser.add_argument("--windows", type=_parse_int_list, default="30,90,365")
    parser.add_argument("--leads", type=_parse_float_list, default="0,5,30")
    parser.add_argument("--train-fraction", type=float, default=0.7)
    parser.add_argument("--min-feature-rows", type=int, default=30)
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
        "--exclude-harness-lanes",
        type=_parse_string_list,
        default=DEFAULT_EXCLUDED_HARNESS_LANES,
        help="Comma-separated runtime harness lanes excluded from predictive slices; empty string includes all.",
    )
    parser.add_argument("--output", help="Optional markdown output path")
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    if not (0.1 <= args.train_fraction <= 0.9):
        print("error: --train-fraction must be between 0.1 and 0.9")
        return 2
    rows = await build_matrix_from_db(
        args.db_url,
        scopes=args.scopes,
        windows=args.windows,
        leads=args.leads,
        train_fraction=args.train_fraction,
        min_feature_rows=args.min_feature_rows,
        exclude_harness_lanes=args.exclude_harness_lanes,
        uncertainty_resamples=args.uncertainty_resamples,
        uncertainty_seed=args.uncertainty_seed,
    )
    report = format_matrix_report(rows, excluded_harness_lanes=args.exclude_harness_lanes)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report + "\n", encoding="utf-8")
        print(f"Wrote {path}")
    else:
        print(report)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
