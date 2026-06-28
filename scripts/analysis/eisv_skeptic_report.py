#!/usr/bin/env python3
"""Skeptical EISV validation report.

This is intentionally a small, falsifiable report rather than a proof of the
model. It asks whether EISV-adjacent signals available before a trusted outcome
add predictive lift over boring baselines.

Usage:
    python3 scripts/analysis/eisv_skeptic_report.py --window-days 365
    python3 scripts/analysis/eisv_skeptic_report.py --scope strict
    python3 scripts/analysis/eisv_skeptic_report.py --lead-minutes 5
    python3 scripts/analysis/eisv_skeptic_report.py --output data/analysis/eisv_skeptic_report.md

Env:
    GOVERNANCE_DATABASE_URL  (default: postgresql://postgres:postgres@localhost:5432/governance)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.analysis.outcome_inventory import is_controlled_validation_fixture


DEFAULT_DB_URL = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)

STRICT_OUTCOMES = ("test_passed", "test_failed", "tool_rejected")
TASK_OUTCOMES = (
    "test_passed",
    "test_failed",
    "tool_rejected",
    "task_completed",
    "task_failed",
)

SMOOTHING_ALPHA = 1.0

# Probe A — dispersion-as-feature (docs/proposals/resolved/eisv-distributional-signal-probe-v0.md).
# A point-estimate EISV hides its own uncertainty; the dispersion of recent state
# snapshots is a stored proxy for it. We aggregate stddev over the snapshots in a
# window strictly before the lead cutoff (leak-safe), and require a minimum count
# because a stddev over 1-2 points is noise, not a signal.
DISPERSION_WINDOW_MINUTES = 90.0
MIN_DISPERSION_SNAPSHOTS = 5
# Headline dispersion axis: S is the entropy/uncertainty axis, so its volatility is
# the most on-thesis proxy. One-line swap to prior_risk_disp / another axis if a
# bad-rate table below shows a cleaner monotonic separation.
DISPERSION_FEATURE = "prior_s_disp"

EISV_PRIOR_STATE_MODELS = (
    "previous_bad_plus_prior_risk",
    "prior_risk_binned",
    "prior_phi_binned",
    "prior_s_binned",
    "prior_verdict",
    "prior_eisv_dispersion_binned",
    "previous_bad_plus_dispersion",
)


@dataclass(frozen=True)
class OutcomeRow:
    ts: datetime
    agent_id: str
    outcome_type: str
    is_bad: bool
    outcome_score: float | None
    verification_source: str | None
    reported_confidence: float | None
    reported_complexity: float | None
    detail: dict[str, Any]
    prior_state_age_seconds: float | None
    prior_risk: float | None
    prior_phi: float | None
    prior_verdict: str | None
    prior_coherence: float | None
    prior_e: float | None
    prior_i: float | None
    prior_s: float | None
    prior_v: float | None
    snapshot_verdict: str | None
    snapshot_e: float | None
    snapshot_i: float | None
    snapshot_s: float | None
    snapshot_v: float | None
    snapshot_phi: float | None
    snapshot_coherence: float | None
    row_key: str | None = None
    previous_bad: bool | None = None
    # Probe A — dispersion over recent prior snapshots (None unless
    # n_prior_snapshots >= MIN_DISPERSION_SNAPSHOTS so sparse agents do not
    # pollute the quantile bins).
    n_prior_snapshots: int | None = None
    prior_s_disp: float | None = None
    prior_e_disp: float | None = None
    prior_i_disp: float | None = None
    prior_v_disp: float | None = None
    prior_risk_disp: float | None = None


@dataclass(frozen=True)
class ModelScore:
    name: str
    n_train: int
    n_test: int
    n_test_scored: int
    auc: float | None
    brier: float | None
    note: str = ""
    scored_row_keys: tuple[Any, ...] = ()
    y_true: tuple[int, ...] = ()
    y_prob: tuple[float, ...] = ()
    y_auc_score: tuple[float, ...] = ()


@dataclass(frozen=True)
class ScoreDelta:
    """A paired scoreboard delta against a named boring baseline."""

    name: str
    baseline_name: str
    auc_delta: float
    brier_improvement: float
    paired_n: int
    beats_baseline: bool


def _score_row_key(row: OutcomeRow) -> Any:
    return row.row_key if row.row_key is not None else id(row)


def _paired_model_metrics(
    baseline: ModelScore,
    candidate: ModelScore,
) -> tuple[float | None, float | None, float | None, float | None, int]:
    """Return candidate and baseline metrics over the candidate-covered rows."""
    baseline_n = len(baseline.scored_row_keys)
    candidate_n = len(candidate.scored_row_keys)
    if not (
        baseline_n
        and len(baseline.y_true) == baseline_n
        and len(baseline.y_prob) == baseline_n
        and len(baseline.y_auc_score) == baseline_n
        and candidate_n
        and len(candidate.y_true) == candidate_n
        and len(candidate.y_prob) == candidate_n
        and len(candidate.y_auc_score) == candidate_n
    ):
        return (
            candidate.auc,
            candidate.brier,
            baseline.auc,
            baseline.brier,
            min(baseline.n_test_scored, candidate.n_test_scored),
        )

    baseline_by_key = {
        key: idx for idx, key in enumerate(baseline.scored_row_keys)
    }
    paired_true: list[int] = []
    paired_candidate_prob: list[float] = []
    paired_candidate_auc_score: list[float] = []
    paired_baseline_prob: list[float] = []
    paired_baseline_auc_score: list[float] = []
    for candidate_idx, key in enumerate(candidate.scored_row_keys):
        baseline_idx = baseline_by_key.get(key)
        if baseline_idx is None:
            continue
        if baseline.y_true[baseline_idx] != candidate.y_true[candidate_idx]:
            continue
        paired_true.append(candidate.y_true[candidate_idx])
        paired_candidate_prob.append(candidate.y_prob[candidate_idx])
        paired_candidate_auc_score.append(candidate.y_auc_score[candidate_idx])
        paired_baseline_prob.append(baseline.y_prob[baseline_idx])
        paired_baseline_auc_score.append(baseline.y_auc_score[baseline_idx])

    if not paired_true:
        return None, None, None, None, 0

    return (
        auc_score(paired_true, paired_candidate_auc_score),
        brier_score(paired_true, paired_candidate_prob),
        auc_score(paired_true, paired_baseline_auc_score),
        brier_score(paired_true, paired_baseline_prob),
        len(paired_true),
    )


def score_deltas_vs_baseline(
    scores: Sequence[ModelScore],
    *,
    baseline_name: str = "previous_outcome_bad",
    candidate_names: Sequence[str] = EISV_PRIOR_STATE_MODELS,
) -> list[ScoreDelta]:
    """Return AUC/Brier deltas for EISV/prior-state candidates vs baseline.

    Positive AUC delta means better ranking than the baseline. Positive Brier
    improvement means lower probability error than the baseline. Candidates with
    missing AUC or Brier are skipped so single-class/sparse slices do not pretend
    to have measurable lift.
    """
    baseline = next((score for score in scores if score.name == baseline_name), None)
    if baseline is None:
        return []

    deltas: list[ScoreDelta] = []
    for score in scores:
        if score.name not in candidate_names:
            continue
        candidate_auc, candidate_brier, baseline_auc, baseline_brier, paired_n = (
            _paired_model_metrics(baseline, score)
        )
        if (
            candidate_auc is None
            or candidate_brier is None
            or baseline_auc is None
            or baseline_brier is None
        ):
            continue
        auc_delta = round(candidate_auc - baseline_auc, 12)
        brier_improvement = round(baseline_brier - candidate_brier, 12)
        deltas.append(
            ScoreDelta(
                name=score.name,
                baseline_name=baseline_name,
                auc_delta=auc_delta,
                brier_improvement=brier_improvement,
                paired_n=paired_n,
                beats_baseline=auc_delta > 0.0 and brier_improvement > 0.0,
            )
        )
    return deltas


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _clamp_probability(value: float) -> float:
    return max(0.0, min(1.0, value))


def smoothed_rate(bad_count: int, total_count: int, alpha: float = SMOOTHING_ALPHA) -> float:
    """Return a smoothed bad-outcome probability."""
    return (bad_count + alpha) / (total_count + 2.0 * alpha)


def brier_score(y_true: Sequence[int], y_prob: Sequence[float]) -> float | None:
    """Mean squared probability error."""
    if not y_true or len(y_true) != len(y_prob):
        return None
    return sum((p - y) ** 2 for y, p in zip(y_true, y_prob)) / len(y_true)


def auc_score(y_true: Sequence[int], y_score: Sequence[float]) -> float | None:
    """ROC AUC using average ranks for ties.

    Returns None when the test set has only one class.
    """
    if not y_true or len(y_true) != len(y_score):
        return None
    positives = sum(1 for y in y_true if y == 1)
    negatives = len(y_true) - positives
    if positives == 0 or negatives == 0:
        return None

    indexed = sorted(enumerate(y_score), key=lambda item: item[1])
    ranks = [0.0] * len(y_score)
    i = 0
    while i < len(indexed):
        j = i + 1
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank
        i = j

    pos_rank_sum = sum(rank for rank, y in zip(ranks, y_true) if y == 1)
    return (pos_rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def bucket_index(value: float | None, cuts: Sequence[float]) -> int | None:
    if value is None:
        return None
    for idx, cut in enumerate(cuts):
        if value <= cut:
            return idx
    return len(cuts)


def quantile_cuts(values: Sequence[float], bucket_count: int = 4) -> list[float]:
    """Return monotonic cut points for roughly equal-sized buckets."""
    clean = sorted(v for v in values if math.isfinite(v))
    if len(clean) < bucket_count:
        return []
    cuts: list[float] = []
    for n in range(1, bucket_count):
        pos = (len(clean) - 1) * n / bucket_count
        lo = math.floor(pos)
        hi = math.ceil(pos)
        if lo == hi:
            cut = clean[int(pos)]
        else:
            cut = clean[lo] * (hi - pos) + clean[hi] * (pos - lo)
        if not cuts or cut > cuts[-1]:
            cuts.append(cut)
    return cuts


def assign_previous_bad(rows: Sequence[OutcomeRow]) -> list[OutcomeRow]:
    """Annotate rows with the previous trusted outcome for the same agent."""
    last_by_agent: dict[str, bool] = {}
    annotated: list[OutcomeRow] = []
    for row in sorted(rows, key=lambda r: r.ts):
        previous = last_by_agent.get(row.agent_id)
        annotated.append(replace(row, previous_bad=previous))
        last_by_agent[row.agent_id] = row.is_bad
    return annotated


def split_by_time(
    rows: Sequence[OutcomeRow],
    train_fraction: float,
) -> tuple[list[OutcomeRow], list[OutcomeRow]]:
    ordered = sorted(rows, key=lambda r: r.ts)
    if not ordered:
        return [], []
    split_at = int(len(ordered) * train_fraction)
    split_at = max(1, min(len(ordered) - 1, split_at))
    return ordered[:split_at], ordered[split_at:]


def _fit_group_rates(
    train_rows: Sequence[OutcomeRow],
    key_fn,
    default_probability: float,
) -> dict[Any, float]:
    counts: dict[Any, list[int]] = defaultdict(lambda: [0, 0])
    for row in train_rows:
        key = key_fn(row)
        if key is None:
            continue
        counts[key][0] += 1
        counts[key][1] += int(row.is_bad)
    return {
        key: smoothed_rate(bad_count=bad, total_count=total)
        for key, (total, bad) in counts.items()
        if total > 0
    } | {"__default__": default_probability}


def _score_predictions(
    name: str,
    train_rows: Sequence[OutcomeRow],
    test_rows: Sequence[OutcomeRow],
    predict_fn,
    raw_score_fn=None,
    note: str = "",
) -> ModelScore:
    y_true: list[int] = []
    y_prob: list[float] = []
    y_auc_score: list[float] = []
    scored_row_keys: list[Any] = []
    for row in test_rows:
        prediction = predict_fn(row)
        if prediction is None:
            continue
        probability = _clamp_probability(float(prediction))
        scored_row_keys.append(_score_row_key(row))
        y_true.append(int(row.is_bad))
        y_prob.append(probability)
        if raw_score_fn is None:
            y_auc_score.append(probability)
        else:
            raw_score = raw_score_fn(row)
            y_auc_score.append(probability if raw_score is None else float(raw_score))
    return ModelScore(
        name=name,
        n_train=len(train_rows),
        n_test=len(test_rows),
        n_test_scored=len(y_true),
        auc=auc_score(y_true, y_auc_score),
        brier=brier_score(y_true, y_prob),
        note=note,
        scored_row_keys=tuple(scored_row_keys),
        y_true=tuple(y_true),
        y_prob=tuple(y_prob),
        y_auc_score=tuple(y_auc_score),
    )


def build_model_scores(
    rows: Sequence[OutcomeRow],
    train_fraction: float = 0.7,
    min_feature_rows: int = 30,
) -> list[ModelScore]:
    """Compare boring baselines with EISV-derived signals."""
    rows = assign_previous_bad(rows)
    train_rows, test_rows = split_by_time(rows, train_fraction)
    if not train_rows or not test_rows:
        return []

    train_bad = sum(int(r.is_bad) for r in train_rows)
    global_probability = smoothed_rate(train_bad, len(train_rows))

    scores = [
        _score_predictions(
            "global_bad_rate",
            train_rows,
            test_rows,
            lambda _row: global_probability,
            note="constant train-set bad rate",
        )
    ]

    previous_bad_rates = _fit_group_rates(
        train_rows,
        lambda row: row.previous_bad,
        global_probability,
    )
    scores.append(
        _score_predictions(
            "previous_outcome_bad",
            train_rows,
            test_rows,
            lambda row: previous_bad_rates.get(
                row.previous_bad,
                previous_bad_rates["__default__"],
            ),
            note="smoothed rate grouped by previous same-agent outcome",
        )
    )

    confidence_train = [r for r in train_rows if r.reported_confidence is not None]
    if len(confidence_train) >= min_feature_rows:
        scores.append(
            _score_predictions(
                "reported_confidence_raw",
                confidence_train,
                [r for r in test_rows if r.reported_confidence is not None],
                lambda row: (
                    1.0 - row.reported_confidence
                    if row.reported_confidence is not None
                    else None
                ),
                note="uses 1 - reported_confidence as bad probability",
            )
        )

    risk_train = [r for r in train_rows if r.prior_risk is not None]
    risk_test = [r for r in test_rows if r.prior_risk is not None]
    risk_cuts = quantile_cuts([
        float(r.prior_risk)
        for r in risk_train
        if r.prior_risk is not None
    ])
    if len(risk_train) >= min_feature_rows and risk_cuts:
        risk_rates = _fit_group_rates(
            risk_train,
            lambda row: bucket_index(row.prior_risk, risk_cuts),
            global_probability,
        )
        scores.append(
            _score_predictions(
                "prior_risk_binned",
                risk_train,
                risk_test,
                lambda row: risk_rates.get(
                    bucket_index(row.prior_risk, risk_cuts),
                    risk_rates["__default__"],
                ),
                raw_score_fn=lambda row: row.prior_risk,
                note=f"risk quartile cuts={', '.join(f'{cut:.3f}' for cut in risk_cuts)}",
            )
        )

        combined_rates = _fit_group_rates(
            risk_train,
            lambda row: (row.previous_bad, bucket_index(row.prior_risk, risk_cuts)),
            global_probability,
        )
        scores.append(
            _score_predictions(
                "previous_bad_plus_prior_risk",
                risk_train,
                risk_test,
                lambda row: combined_rates.get(
                    (row.previous_bad, bucket_index(row.prior_risk, risk_cuts)),
                    risk_rates.get(bucket_index(row.prior_risk, risk_cuts), global_probability),
                ),
                raw_score_fn=lambda row: row.prior_risk,
                note="smoothed rate grouped by previous_bad and prior-risk quartile",
            )
        )

    # --- Probe A: dispersion-as-feature -------------------------------------
    # Mirrors prior_risk_binned / previous_bad_plus_prior_risk exactly, on the
    # dispersion of recent state instead of the prior risk level.
    def _disp(row: OutcomeRow) -> float | None:
        return getattr(row, DISPERSION_FEATURE)

    disp_train = [r for r in train_rows if _disp(r) is not None]
    disp_test = [r for r in test_rows if _disp(r) is not None]
    disp_cuts = quantile_cuts([
        float(_disp(r)) for r in disp_train if _disp(r) is not None
    ])
    if len(disp_train) >= min_feature_rows and disp_cuts:
        disp_rates = _fit_group_rates(
            disp_train,
            lambda row: bucket_index(_disp(row), disp_cuts),
            global_probability,
        )
        scores.append(
            _score_predictions(
                "prior_eisv_dispersion_binned",
                disp_train,
                disp_test,
                lambda row: disp_rates.get(
                    bucket_index(_disp(row), disp_cuts),
                    disp_rates["__default__"],
                ),
                raw_score_fn=_disp,
                note=(
                    f"{DISPERSION_FEATURE} quartile cuts="
                    f"{', '.join(f'{cut:.3f}' for cut in disp_cuts)}"
                ),
            )
        )

        disp_combined_rates = _fit_group_rates(
            disp_train,
            lambda row: (row.previous_bad, bucket_index(_disp(row), disp_cuts)),
            global_probability,
        )
        scores.append(
            _score_predictions(
                "previous_bad_plus_dispersion",
                disp_train,
                disp_test,
                lambda row: disp_combined_rates.get(
                    (row.previous_bad, bucket_index(_disp(row), disp_cuts)),
                    disp_rates.get(bucket_index(_disp(row), disp_cuts), global_probability),
                ),
                raw_score_fn=_disp,
                note="smoothed rate grouped by previous_bad and dispersion quartile",
            )
        )

    phi_train = [r for r in train_rows if r.prior_phi is not None]
    phi_test = [r for r in test_rows if r.prior_phi is not None]
    phi_cuts = quantile_cuts([
        float(r.prior_phi)
        for r in phi_train
        if r.prior_phi is not None
    ])
    if len(phi_train) >= min_feature_rows and phi_cuts:
        phi_rates = _fit_group_rates(
            phi_train,
            lambda row: bucket_index(row.prior_phi, phi_cuts),
            global_probability,
        )
        scores.append(
            _score_predictions(
                "prior_phi_binned",
                phi_train,
                phi_test,
                lambda row: phi_rates.get(
                    bucket_index(row.prior_phi, phi_cuts),
                    phi_rates["__default__"],
                ),
                raw_score_fn=lambda row: (
                    -row.prior_phi if row.prior_phi is not None else None
                ),
                note=f"phi quartile cuts={', '.join(f'{cut:.3f}' for cut in phi_cuts)}",
            )
        )

    entropy_train = [r for r in train_rows if r.prior_s is not None]
    entropy_test = [r for r in test_rows if r.prior_s is not None]
    entropy_cuts = quantile_cuts([
        float(r.prior_s)
        for r in entropy_train
        if r.prior_s is not None
    ])
    if len(entropy_train) >= min_feature_rows and entropy_cuts:
        entropy_rates = _fit_group_rates(
            entropy_train,
            lambda row: bucket_index(row.prior_s, entropy_cuts),
            global_probability,
        )
        scores.append(
            _score_predictions(
                "prior_s_binned",
                entropy_train,
                entropy_test,
                lambda row: entropy_rates.get(
                    bucket_index(row.prior_s, entropy_cuts),
                    entropy_rates["__default__"],
                ),
                raw_score_fn=lambda row: row.prior_s,
                note=f"S quartile cuts={', '.join(f'{cut:.3f}' for cut in entropy_cuts)}",
            )
        )

    verdict_train = [r for r in train_rows if r.prior_verdict]
    verdict_test = [r for r in test_rows if r.prior_verdict]
    if len(verdict_train) >= min_feature_rows:
        verdict_rates = _fit_group_rates(
            verdict_train,
            lambda row: row.prior_verdict,
            global_probability,
        )
        scores.append(
            _score_predictions(
                "prior_verdict",
                verdict_train,
                verdict_test,
                lambda row: verdict_rates.get(
                    row.prior_verdict,
                    verdict_rates["__default__"],
                ),
                note="smoothed rate grouped by previous state verdict",
            )
        )

    return scores


def bad_rate_by_key(rows: Sequence[OutcomeRow], key_fn) -> list[tuple[str, int, int, float]]:
    buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        key = key_fn(row)
        key_str = "(missing)" if key is None else str(key)
        buckets[key_str][0] += 1
        buckets[key_str][1] += int(row.is_bad)
    return sorted(
        (
            key,
            total,
            bad,
            bad / total if total else 0.0,
        )
        for key, (total, bad) in buckets.items()
    )


def risk_bucket_rates(
    rows: Sequence[OutcomeRow],
    bucket_count: int = 4,
) -> tuple[list[float], list[tuple[str, int, int, float]]]:
    risk_rows = [row for row in rows if row.prior_risk is not None]
    cuts = quantile_cuts([float(row.prior_risk) for row in risk_rows], bucket_count=bucket_count)
    if not cuts:
        return [], []
    buckets: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for row in risk_rows:
        idx = bucket_index(row.prior_risk, cuts)
        if idx is None:
            continue
        buckets[idx][0] += 1
        buckets[idx][1] += int(row.is_bad)

    labels = []
    lower = "-inf"
    for idx in range(len(cuts) + 1):
        upper = f"{cuts[idx]:.3f}" if idx < len(cuts) else "inf"
        labels.append(f"({lower}, {upper}]")
        lower = upper
    return cuts, [
        (labels[idx], total, bad, bad / total if total else 0.0)
        for idx, (total, bad) in sorted(buckets.items())
    ]


def metric_bucket_rates(
    rows: Sequence[OutcomeRow],
    *,
    metric_name: str,
    metric_fn,
    bucket_count: int = 4,
    reverse_labels: bool = False,
) -> tuple[list[float], list[tuple[str, int, int, float]]]:
    """Bucket an arbitrary numeric metric and compute bad-outcome rates."""
    metric_rows = [(row, metric_fn(row)) for row in rows]
    metric_rows = [
        (row, value)
        for row, value in metric_rows
        if value is not None and math.isfinite(float(value))
    ]
    cuts = quantile_cuts([float(value) for _row, value in metric_rows], bucket_count)
    if not cuts:
        return [], []

    buckets: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for row, value in metric_rows:
        idx = bucket_index(float(value), cuts)
        if idx is None:
            continue
        buckets[idx][0] += 1
        buckets[idx][1] += int(row.is_bad)

    labels = []
    lower = "-inf"
    for idx in range(len(cuts) + 1):
        upper = f"{cuts[idx]:.3f}" if idx < len(cuts) else "inf"
        labels.append(f"({lower}, {upper}]")
        lower = upper
    if reverse_labels:
        labels = list(reversed(labels))
    return cuts, [
        (labels[idx], total, bad, bad / total if total else 0.0)
        for idx, (total, bad) in sorted(buckets.items())
    ]


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def _fmt_float(value: float | None, digits: int = 4) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def _coverage(rows: Sequence[OutcomeRow]) -> dict[str, Any]:
    total = len(rows)
    prior_state = [r for r in rows if r.prior_state_age_seconds is not None]
    prior_risk = [r for r in rows if r.prior_risk is not None]
    confidence = [r for r in rows if r.reported_confidence is not None]
    complexity = [r for r in rows if r.reported_complexity is not None]
    dispersion = [r for r in rows if getattr(r, DISPERSION_FEATURE) is not None]
    ages = [r.prior_state_age_seconds for r in prior_state if r.prior_state_age_seconds is not None]
    return {
        "total": total,
        "prior_state": len(prior_state),
        "prior_risk": len(prior_risk),
        "confidence": len(confidence),
        "complexity": len(complexity),
        "dispersion": len(dispersion),
        "median_prior_age_seconds": statistics.median(ages) if ages else None,
        "max_prior_age_seconds": max(ages) if ages else None,
    }


def summarize_conclusion(rows: Sequence[OutcomeRow], scores: Sequence[ModelScore]) -> str:
    trusted = len(rows)
    bad = sum(int(r.is_bad) for r in rows)
    if trusted < 100:
        return "INCONCLUSIVE: fewer than 100 trusted outcomes in this window."
    if bad < 10:
        return "INCONCLUSIVE: fewer than 10 bad outcomes; predictive lift is too fragile."

    deltas = score_deltas_vs_baseline(scores)
    if not deltas:
        return "INCONCLUSIVE: EISV/prior-state coverage is too low for model comparison."

    best_delta = max(
        deltas,
        key=lambda d: (d.beats_baseline, d.auc_delta, d.brier_improvement),
    )
    best = next((s for s in scores if s.name == best_delta.name), None)
    if best_delta.auc_delta >= 0.03 and best_delta.brier_improvement >= 0.001:
        return (
            "KEEP TESTING: EISV/prior-state features show modest lift over the "
            f"previous-outcome baseline (best={best_delta.name}, "
            f"paired N={best_delta.paired_n}, AUC lift={best_delta.auc_delta:.3f}, "
            f"Brier improvement={best_delta.brier_improvement:.4f})."
        )
    if best_delta.auc_delta <= 0.0 or best_delta.brier_improvement <= 0.0:
        return (
            "SKEPTICAL: EISV/prior-state features do not beat the boring "
            "previous-outcome baseline across both ranking and calibration "
            f"in this split (best={best_delta.name}, paired N={best_delta.paired_n}, "
            f"AUC lift={best_delta.auc_delta:.3f}, "
            f"Brier improvement={best_delta.brier_improvement:.4f})."
        )
    if best is not None and best.auc is not None and best.auc < 0.55:
        return f"SKEPTICAL: best EISV/prior-state AUC is weak ({best.name} AUC={best.auc:.3f})."
    best_name = best.name if best is not None else best_delta.name
    return f"WEAK SIGNAL: {best_name} has some predictive signal, but compare across more windows."


def build_report(
    rows: Sequence[OutcomeRow],
    *,
    scope: str,
    window_days: int,
    lead_minutes: float,
    train_fraction: float,
    generated_at: datetime | None = None,
) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)
    rows = sorted(rows, key=lambda r: r.ts)
    coverage = _coverage(rows)
    bad = sum(int(r.is_bad) for r in rows)
    scores = build_model_scores(rows, train_fraction=train_fraction)
    conclusion = summarize_conclusion(rows, scores)
    source_counts = Counter(r.verification_source or "(null)" for r in rows)
    detail_signal_counts = Counter()
    evidence_keys = (
        "tests",
        "commands",
        "files",
        "lint",
        "tool_usage",
        "tool_results",
        "eprocess_eligible",
    )
    for row in rows:
        detail = row.detail
        for key in evidence_keys:
            if detail.get(key):
                detail_signal_counts[key] += 1

    lines: list[str] = []
    a = lines.append
    a("# EISV Skeptic Report")
    a("")
    a(f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    a(f"Window: last {window_days} days")
    a(f"Outcome scope: `{scope}`")
    a(f"Prior-state lead: {lead_minutes:g} minutes before outcome")
    a(f"Train/test split: chronological {train_fraction:.0%}/{1 - train_fraction:.0%}")
    a("")
    a("## Coverage")
    a("")
    a("| Metric | Count | Percent |")
    a("|---|---:|---:|")
    a(f"| Trusted outcomes | {coverage['total']} | 100.0% |")
    total = coverage["total"]
    a(f"| Bad outcomes | {bad} | {_fmt_pct(bad / total if total else None)} |")
    a(
        f"| Rows with prior state | {coverage['prior_state']} "
        f"| {_fmt_pct(coverage['prior_state'] / total if total else None)} |"
    )
    a(
        f"| Rows with prior risk | {coverage['prior_risk']} "
        f"| {_fmt_pct(coverage['prior_risk'] / total if total else None)} |"
    )
    a(
        f"| Rows with reported confidence | {coverage['confidence']} "
        f"| {_fmt_pct(coverage['confidence'] / total if total else None)} |"
    )
    a(
        f"| Rows with reported complexity | {coverage['complexity']} "
        f"| {_fmt_pct(coverage['complexity'] / total if total else None)} |"
    )
    a(
        f"| Rows with state dispersion (>= {MIN_DISPERSION_SNAPSHOTS} snapshots) "
        f"| {coverage['dispersion']} "
        f"| {_fmt_pct(coverage['dispersion'] / total if total else None)} |"
    )
    a("")
    if coverage["median_prior_age_seconds"] is not None:
        a(
            f"Prior-state age: median {coverage['median_prior_age_seconds']:.1f}s, "
            f"max {coverage['max_prior_age_seconds']:.1f}s."
        )
        a("")

    a("## Provenance")
    a("")
    a("| verification_source | Count |")
    a("|---|---:|")
    for key, count in source_counts.most_common():
        a(f"| `{key}` | {count} |")
    a("")
    a("| detail evidence flag | Count |")
    a("|---|---:|")
    if detail_signal_counts:
        for key, count in detail_signal_counts.most_common():
            a(f"| `{key}` | {count} |")
    else:
        a("| (none) | 0 |")
    a("")

    a("## Bad Rates")
    a("")
    a("By outcome type:")
    a("")
    a("| Outcome type | N | Bad | Bad rate |")
    a("|---|---:|---:|---:|")
    for key, total, bad_count, rate in bad_rate_by_key(rows, lambda r: r.outcome_type):
        a(f"| `{key}` | {total} | {bad_count} | {_fmt_pct(rate)} |")
    a("")
    a("By prior verdict:")
    a("")
    a("| Prior verdict | N | Bad | Bad rate |")
    a("|---|---:|---:|---:|")
    for key, total, bad_count, rate in bad_rate_by_key(rows, lambda r: r.prior_verdict):
        a(f"| `{key}` | {total} | {bad_count} | {_fmt_pct(rate)} |")
    a("")
    cuts, bucket_rows = risk_bucket_rates(rows)
    a("By prior risk quartile:")
    a("")
    if cuts:
        a(f"Risk cuts: {', '.join(f'{cut:.4f}' for cut in cuts)}")
        a("")
    a("| Prior risk bucket | N | Bad | Bad rate |")
    a("|---|---:|---:|---:|")
    if bucket_rows:
        for key, total, bad_count, rate in bucket_rows:
            a(f"| `{key}` | {total} | {bad_count} | {_fmt_pct(rate)} |")
    else:
        a("| (insufficient prior risk coverage) | 0 | 0 | - |")
    a("")

    phi_cuts, phi_bucket_rows = metric_bucket_rates(
        rows,
        metric_name="prior_phi",
        metric_fn=lambda row: row.prior_phi,
        reverse_labels=False,
    )
    a("By prior phi quartile:")
    a("")
    if phi_cuts:
        a(f"Phi cuts: {', '.join(f'{cut:.4f}' for cut in phi_cuts)}")
        a("")
    a("| Prior phi bucket | N | Bad | Bad rate |")
    a("|---|---:|---:|---:|")
    if phi_bucket_rows:
        for key, total, bad_count, rate in phi_bucket_rows:
            a(f"| `{key}` | {total} | {bad_count} | {_fmt_pct(rate)} |")
    else:
        a("| (insufficient prior phi coverage) | 0 | 0 | - |")
    a("")

    s_cuts, s_bucket_rows = metric_bucket_rates(
        rows,
        metric_name="prior_s",
        metric_fn=lambda row: row.prior_s,
        reverse_labels=False,
    )
    a("By prior S quartile:")
    a("")
    if s_cuts:
        a(f"S cuts: {', '.join(f'{cut:.4f}' for cut in s_cuts)}")
        a("")
    a("| Prior S bucket | N | Bad | Bad rate |")
    a("|---|---:|---:|---:|")
    if s_bucket_rows:
        for key, total, bad_count, rate in s_bucket_rows:
            a(f"| `{key}` | {total} | {bad_count} | {_fmt_pct(rate)} |")
    else:
        a("| (insufficient prior S coverage) | 0 | 0 | - |")
    a("")

    disp_cuts, disp_bucket_rows = metric_bucket_rates(
        rows,
        metric_name=DISPERSION_FEATURE,
        metric_fn=lambda row: getattr(row, DISPERSION_FEATURE),
        reverse_labels=False,
    )
    a(f"By recent-state dispersion quartile (`{DISPERSION_FEATURE}`):")
    a("")
    a("Probe A: a rising bad-rate across dispersion quartiles is the uncertainty-as-signal read.")
    a("")
    if disp_cuts:
        a(f"Dispersion cuts: {', '.join(f'{cut:.4f}' for cut in disp_cuts)}")
        a("")
    a("| Dispersion bucket | N | Bad | Bad rate |")
    a("|---|---:|---:|---:|")
    if disp_bucket_rows:
        for key, total, bad_count, rate in disp_bucket_rows:
            a(f"| `{key}` | {total} | {bad_count} | {_fmt_pct(rate)} |")
    else:
        a("| (insufficient dispersion coverage) | 0 | 0 | - |")
    a("")

    a("## Model Comparison")
    a("")
    a("Lower Brier is better. AUC above 0.5 means ranking is better than chance.")
    a("")
    a("| Model | Train N | Test N | Scored Test N | AUC | Brier | Note |")
    a("|---|---:|---:|---:|---:|---:|---|")
    for score in scores:
        a(
            f"| `{score.name}` | {score.n_train} | {score.n_test} | {score.n_test_scored} "
            f"| {_fmt_float(score.auc, 3)} | {_fmt_float(score.brier, 4)} | {score.note} |"
        )
    a("")

    a("## Ablation vs Previous-Outcome Baseline")
    a("")
    a(
        "Deltas compare each candidate against the previous-outcome baseline over "
        "the candidate-scored rows."
    )
    a("Positive AUC delta means better ranking; positive Brier improvement means lower probability error.")
    a("")
    a("| EISV/prior-state model | Paired N | AUC delta | Brier improvement | Beats both? |")
    a("|---|---:|---:|---:|---|")
    deltas = score_deltas_vs_baseline(scores)
    if deltas:
        for delta in deltas:
            a(
                f"| `{delta.name}` | {delta.paired_n} "
                f"| {_fmt_float(delta.auc_delta, 3)} "
                f"| {_fmt_float(delta.brier_improvement, 4)} "
                f"| {'yes' if delta.beats_baseline else 'no'} |"
            )
    else:
        a("| (insufficient paired baseline/candidate metrics) | 0 | - | - | no |")
    a("")

    a("## Conclusion")
    a("")
    a(conclusion)
    a("")
    a(
        "Interpretation rule: EISV is online agent-state estimation "
        "(agent proprioception), not an outcome oracle or bad-verdict dispenser."
    )
    a("Outcome labels come from external evidence/rubrics; this report only checks")
    a("whether EISV/prior-state fields add measurable predictive signal over simpler")
    a("baselines in this data slice.")
    return "\n".join(lines)


def _parse_detail(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _row_from_record(record: Any) -> OutcomeRow:
    detail = _parse_detail(record.get("detail"))
    n_prior = record.get("n_prior_snapshots")
    n_prior = int(n_prior) if n_prior is not None else None
    # Gate dispersion on a minimum snapshot count: a stddev over 1-2 points is
    # noise. Below the floor, leave all dispersion fields None.
    sufficient = n_prior is not None and n_prior >= MIN_DISPERSION_SNAPSHOTS

    def _disp_field(key: str) -> float | None:
        return _to_float(record.get(key)) if sufficient else None

    return OutcomeRow(
        ts=record["ts"],
        row_key=str(record["outcome_id"]) if record.get("outcome_id") is not None else None,
        agent_id=record["agent_id"],
        outcome_type=record["outcome_type"],
        is_bad=bool(record["is_bad"]),
        outcome_score=_to_float(record.get("outcome_score")),
        verification_source=record.get("verification_source"),
        reported_confidence=_to_float(detail.get("reported_confidence", detail.get("confidence"))),
        reported_complexity=_to_float(detail.get("reported_complexity", detail.get("complexity"))),
        detail=detail,
        prior_state_age_seconds=_to_float(record.get("prior_state_age_seconds")),
        prior_risk=_to_float(record.get("prior_risk")),
        prior_phi=_to_float(record.get("prior_phi")),
        prior_verdict=record.get("prior_verdict"),
        prior_coherence=_to_float(record.get("prior_coherence")),
        prior_e=_to_float(record.get("prior_e")),
        prior_i=_to_float(record.get("prior_i")),
        prior_s=_to_float(record.get("prior_s")),
        prior_v=_to_float(record.get("prior_v")),
        snapshot_verdict=record.get("eisv_verdict"),
        snapshot_e=_to_float(record.get("eisv_e")),
        snapshot_i=_to_float(record.get("eisv_i")),
        snapshot_s=_to_float(record.get("eisv_s")),
        snapshot_v=_to_float(record.get("eisv_v")),
        snapshot_phi=_to_float(record.get("eisv_phi")),
        snapshot_coherence=_to_float(record.get("eisv_coherence")),
        n_prior_snapshots=n_prior,
        prior_s_disp=_disp_field("prior_s_disp"),
        prior_e_disp=_disp_field("prior_e_disp"),
        prior_i_disp=_disp_field("prior_i_disp"),
        prior_v_disp=_disp_field("prior_v_disp"),
        prior_risk_disp=_disp_field("prior_risk_disp"),
    )


async def fetch_rows(
    db_url: str,
    *,
    window_days: int,
    lead_minutes: float,
    outcome_types: Sequence[str],
    dispersion_window_minutes: float = DISPERSION_WINDOW_MINUTES,
) -> list[OutcomeRow]:
    try:
        import asyncpg
    except ImportError:
        print("error: asyncpg not installed. Install with `pip install asyncpg`.", file=sys.stderr)
        raise SystemExit(1)

    conn = await asyncpg.connect(db_url)
    try:
        records = await conn.fetch(
            """
            SELECT
                o.ts,
                o.outcome_id,
                o.agent_id,
                o.outcome_type,
                o.outcome_score,
                o.is_bad,
                o.eisv_e,
                o.eisv_i,
                o.eisv_s,
                o.eisv_v,
                o.eisv_phi,
                o.eisv_verdict,
                o.eisv_coherence,
                o.detail,
                o.verification_source,
                EXTRACT(EPOCH FROM (o.ts - ps.recorded_at))::float AS prior_state_age_seconds,
                ps.risk_score AS prior_risk,
                ps.state_json->>'phi' AS prior_phi,
                ps.coherence AS prior_coherence,
                ps.state_json->>'verdict' AS prior_verdict,
                COALESCE(
                    ps.state_json->'primary_eisv'->>'E',
                    ps.state_json->'ode_eisv'->>'E',
                    ps.state_json->>'E'
                ) AS prior_e,
                COALESCE(
                    ps.state_json->'primary_eisv'->>'I',
                    ps.state_json->'ode_eisv'->>'I',
                    ps.integrity::text
                ) AS prior_i,
                COALESCE(
                    ps.state_json->'primary_eisv'->>'S',
                    ps.state_json->'ode_eisv'->>'S',
                    ps.entropy::text
                ) AS prior_s,
                COALESCE(
                    ps.state_json->'primary_eisv'->>'V',
                    ps.state_json->'ode_eisv'->>'V',
                    ps.volatility::text
                ) AS prior_v,
                disp.n_prior_snapshots,
                disp.prior_s_disp,
                disp.prior_e_disp,
                disp.prior_i_disp,
                disp.prior_v_disp,
                disp.prior_risk_disp
            FROM audit.outcome_events o
            LEFT JOIN LATERAL (
                SELECT
                    s.recorded_at,
                    s.risk_score,
                    s.coherence,
                    s.state_json,
                    s.integrity,
                    s.entropy,
                    s.volatility
                FROM core.identities i
                JOIN core.agent_state s ON s.identity_id = i.identity_id
                WHERE i.agent_id = o.agent_id
                  AND s.synthetic IS NOT TRUE
                  AND s.recorded_at <= o.ts - ($2::double precision * INTERVAL '1 minute')
                ORDER BY s.recorded_at DESC
                LIMIT 1
            ) ps ON TRUE
            LEFT JOIN LATERAL (
                -- Probe A dispersion window: stddev of recent state over the
                -- snapshots strictly before the lead cutoff (same leak-safe
                -- upper bound as ps) and within DISPERSION_WINDOW_MINUTES.
                SELECT
                    count(*) AS n_prior_snapshots,
                    stddev_samp(COALESCE(
                        (s.state_json->'primary_eisv'->>'S')::float,
                        (s.state_json->'ode_eisv'->>'S')::float,
                        s.entropy
                    )) AS prior_s_disp,
                    stddev_samp(COALESCE(
                        (s.state_json->'primary_eisv'->>'E')::float,
                        (s.state_json->'ode_eisv'->>'E')::float,
                        NULL
                    )) AS prior_e_disp,
                    stddev_samp(COALESCE(
                        (s.state_json->'primary_eisv'->>'I')::float,
                        (s.state_json->'ode_eisv'->>'I')::float,
                        s.integrity
                    )) AS prior_i_disp,
                    stddev_samp(COALESCE(
                        (s.state_json->'primary_eisv'->>'V')::float,
                        (s.state_json->'ode_eisv'->>'V')::float,
                        s.volatility
                    )) AS prior_v_disp,
                    stddev_samp(s.risk_score) AS prior_risk_disp
                FROM core.identities i
                JOIN core.agent_state s ON s.identity_id = i.identity_id
                WHERE i.agent_id = o.agent_id
                  AND s.synthetic IS NOT TRUE
                  AND s.recorded_at <= o.ts - ($2::double precision * INTERVAL '1 minute')
                  AND s.recorded_at >  o.ts - (($2 + $4)::double precision * INTERVAL '1 minute')
            ) disp ON TRUE
            WHERE o.ts >= now() - ($1::int * INTERVAL '1 day')
              AND o.outcome_type = ANY($3::text[])
            ORDER BY o.ts ASC
            """,
            window_days,
            lead_minutes,
            list(outcome_types),
            dispersion_window_minutes,
        )
    finally:
        await conn.close()
    return [
        row
        for record in records
        if not is_controlled_validation_fixture(
            (row := _row_from_record(record)).detail
        )
    ]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-days", type=int, default=365)
    parser.add_argument("--lead-minutes", type=float, default=0.0)
    parser.add_argument(
        "--dispersion-window-minutes",
        type=float,
        default=DISPERSION_WINDOW_MINUTES,
        help="Window before the lead cutoff over which recent-state dispersion is measured (Probe A).",
    )
    parser.add_argument("--train-fraction", type=float, default=0.7)
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument(
        "--scope",
        choices=("strict", "task"),
        default="task",
        help=(
            "strict = tests/tool_rejections only; task = strict plus task_completed/task_failed. "
            "The task scope has more data but weaker objectivity."
        ),
    )
    parser.add_argument("--output", help="Optional markdown output path")
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    if not (0.1 <= args.train_fraction <= 0.9):
        print("error: --train-fraction must be between 0.1 and 0.9", file=sys.stderr)
        return 2
    outcome_types = STRICT_OUTCOMES if args.scope == "strict" else TASK_OUTCOMES
    rows = await fetch_rows(
        args.db_url,
        window_days=args.window_days,
        lead_minutes=args.lead_minutes,
        outcome_types=outcome_types,
        dispersion_window_minutes=args.dispersion_window_minutes,
    )
    report = build_report(
        rows,
        scope=args.scope,
        window_days=args.window_days,
        lead_minutes=args.lead_minutes,
        train_fraction=args.train_fraction,
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
    args = parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
