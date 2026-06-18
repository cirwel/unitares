#!/usr/bin/env python3
"""Report prospective prediction-bound outcome cohorts.

This is a small holdout-oriented companion to the skeptical ablation matrix. It
counts only outcomes tied to a real prospective prediction registry binding,
not fallback confidence/audit bindings, so future validation can be separated
from retrospective or heuristic labels.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.analysis.eisv_skeptic_report import (  # noqa: E402
    DEFAULT_DB_URL,
    STRICT_OUTCOMES,
    TASK_OUTCOMES,
    OutcomeRow,
    fetch_rows,
)
from scripts.analysis.outcome_inventory import (  # noqa: E402
    harness_lane_from_detail,
    is_controlled_validation_fixture,
)


@dataclass(frozen=True)
class ProspectiveCohortSummary:
    """Compact prospective prediction-bound cohort summary."""

    scope: str
    window_days: int
    lead_minutes: float
    total_outcomes: int
    prediction_bound: int
    prediction_bound_bad: int
    prediction_bound_prior_state: int
    by_harness_lane: dict[str, int]

    @property
    def prediction_coverage(self) -> float:
        """Share of trusted rows with registry-bound prospective predictions."""

        return self.prediction_bound / self.total_outcomes if self.total_outcomes else 0.0

    @property
    def prediction_bound_bad_rate(self) -> float:
        """Bad-outcome rate within the prospective prediction-bound cohort."""

        return (
            self.prediction_bound_bad / self.prediction_bound
            if self.prediction_bound
            else 0.0
        )


def is_prospective_prediction_bound(row: OutcomeRow) -> bool:
    """True only for rows tied to a real registry prediction binding."""

    return bool(row.detail.get("prediction_id")) and row.detail.get("prediction_binding") == "registry"


def build_cohort_summary(
    rows: Sequence[OutcomeRow],
    *,
    scope: str,
    window_days: int,
    lead_minutes: float,
) -> ProspectiveCohortSummary:
    """Summarize prospective prediction-bound rows without fallback leakage."""

    trusted = [row for row in rows if not is_controlled_validation_fixture(row.detail)]
    prediction_rows = [row for row in trusted if is_prospective_prediction_bound(row)]
    by_lane: dict[str, int] = {}
    for row in prediction_rows:
        lane = harness_lane_from_detail(row.detail)
        by_lane[lane] = by_lane.get(lane, 0) + 1
    return ProspectiveCohortSummary(
        scope=scope,
        window_days=window_days,
        lead_minutes=lead_minutes,
        total_outcomes=len(trusted),
        prediction_bound=len(prediction_rows),
        prediction_bound_bad=sum(int(row.is_bad) for row in prediction_rows),
        prediction_bound_prior_state=sum(
            1 for row in prediction_rows if row.prior_state_age_seconds is not None
        ),
        by_harness_lane=dict(sorted(by_lane.items())),
    )


def _fmt_lead(value: float) -> str:
    return f"{value:g}"


def format_cohort_report(summary: ProspectiveCohortSummary) -> str:
    """Render a markdown summary for prospective holdout readiness."""

    lanes = ",".join(
        f"{lane}={count}" for lane, count in summary.by_harness_lane.items()
    ) or "none"
    return "\n".join(
        [
            "# Prospective Prediction Cohort",
            "",
            f"scope: {summary.scope}",
            f"window_days: {summary.window_days}",
            f"lead_minutes: {_fmt_lead(summary.lead_minutes)}",
            f"trusted_outcomes: {summary.total_outcomes}",
            f"prediction_bound: {summary.prediction_bound}",
            f"prediction_coverage: {summary.prediction_coverage:.3f}",
            f"prediction_bound_bad: {summary.prediction_bound_bad}",
            f"prediction_bound_bad_rate: {summary.prediction_bound_bad_rate:.3f}",
            "prediction_bound_prior_state: "
            f"{summary.prediction_bound_prior_state}/{summary.prediction_bound}",
            f"harness_lanes: {lanes}",
            "",
            "Interpretation rule: this is prospective holdout plumbing only. "
            "It counts registry-bound predictions that existed before outcomes; "
            "it does not validate EISV unless a frozen holdout later beats boring baselines.",
        ]
    )


async def build_summary_from_db(
    db_url: str,
    *,
    scope: str,
    window_days: int,
    lead_minutes: float,
) -> ProspectiveCohortSummary:
    """Fetch trusted rows and summarize registry-bound prediction coverage."""

    outcome_types = STRICT_OUTCOMES if scope == "strict" else TASK_OUTCOMES
    rows = await fetch_rows(
        db_url,
        window_days=window_days,
        lead_minutes=lead_minutes,
        outcome_types=outcome_types,
    )
    return build_cohort_summary(
        rows,
        scope=scope,
        window_days=window_days,
        lead_minutes=lead_minutes,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI options."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--scope", choices=("strict", "task"), default="task")
    parser.add_argument("--window-days", type=int, default=90)
    parser.add_argument("--lead-minutes", type=float, default=30.0)
    parser.add_argument("--output", help="Optional markdown output path")
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    """Run the prospective cohort report."""

    summary = await build_summary_from_db(
        args.db_url,
        scope=args.scope,
        window_days=args.window_days,
        lead_minutes=args.lead_minutes,
    )
    report = format_cohort_report(summary)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report + "\n", encoding="utf-8")
        print(f"Wrote {path}")
    else:
        print(report)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""

    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
