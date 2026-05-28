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
    build_model_scores,
    fetch_rows,
    score_deltas_vs_baseline,
    summarize_conclusion,
)


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


def _fmt_float(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def _fmt_lead(value: float) -> str:
    return f"{value:g}"


def _baseline(scores: Sequence[ModelScore]) -> ModelScore | None:
    return next((score for score in scores if score.name == "previous_outcome_bad"), None)


def build_matrix_row(
    rows: Sequence[OutcomeRow],
    *,
    scope: str,
    window_days: int,
    lead_minutes: float,
    train_fraction: float = 0.7,
    min_feature_rows: int = 30,
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
    )


def format_matrix_report(
    rows: Sequence[AblationMatrixRow],
    *,
    generated_at: datetime | None = None,
) -> str:
    """Render a compact markdown table for skeptical multi-slice reporting."""
    generated_at = generated_at or datetime.now(timezone.utc)
    lines = [
        "# EISV Ablation Matrix",
        "",
        f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        "Positive AUC delta means better ranking than `previous_outcome_bad`; positive Brier improvement means lower probability error. `Beats both?` is the conservative quick read.",
        "",
        "| Scope | Window days | Lead min | Trusted | Bad | Prior state | Prior risk | Baseline AUC | Baseline Brier | Best EISV/prior model | AUC delta | Brier improvement | Beats both? | Conclusion |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---|---|",
    ]
    if rows:
        for row in rows:
            lines.append(
                "| "
                f"{row.scope} | {row.window_days} | {_fmt_lead(row.lead_minutes)} "
                f"| {row.trusted} | {row.bad} | {row.prior_state} | {row.prior_risk} "
                f"| {_fmt_float(row.baseline_auc, 3)} | {_fmt_float(row.baseline_brier, 4)} "
                f"| {row.best_candidate or '-'} | {_fmt_float(row.best_auc_delta, 3)} "
                f"| {_fmt_float(row.best_brier_improvement, 4)} "
                f"| {'yes' if row.beats_both else 'no'} | {row.conclusion} |"
            )
    else:
        lines.append("| - | - | - | 0 | 0 | 0 | 0 | - | - | - | - | - | no | no rows |")
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
) -> list[AblationMatrixRow]:
    matrix_rows: list[AblationMatrixRow] = []
    for scope in scopes:
        outcome_types = STRICT_OUTCOMES if scope == "strict" else TASK_OUTCOMES
        for window_days in windows:
            for lead_minutes in leads:
                outcome_rows = await fetch_rows(
                    db_url,
                    window_days=window_days,
                    lead_minutes=lead_minutes,
                    outcome_types=outcome_types,
                )
                matrix_rows.append(
                    build_matrix_row(
                        outcome_rows,
                        scope=scope,
                        window_days=window_days,
                        lead_minutes=lead_minutes,
                        train_fraction=train_fraction,
                        min_feature_rows=min_feature_rows,
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
    )
    report = format_matrix_report(rows)
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
