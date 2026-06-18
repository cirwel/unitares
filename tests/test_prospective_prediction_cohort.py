from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.analysis.eisv_skeptic_report import OutcomeRow
from scripts.analysis.prospective_prediction_cohort import (
    build_cohort_summary,
    format_cohort_report,
)


def _row(
    idx: int,
    *,
    bad: bool = False,
    prediction_id: str | None = None,
    binding: str | None = None,
    harness: str | None = None,
    prior_state: bool = True,
) -> OutcomeRow:
    detail = {}
    if prediction_id:
        detail["prediction_id"] = prediction_id
    if binding:
        detail["prediction_binding"] = binding
    if harness:
        detail["harness"] = harness
    return OutcomeRow(
        ts=datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=idx),
        agent_id=f"agent-{idx % 2}",
        outcome_type="task_failed" if bad else "task_completed",
        is_bad=bad,
        outcome_score=0.0 if bad else 1.0,
        verification_source="server_observation",
        reported_confidence=None,
        reported_complexity=None,
        detail=detail,
        prior_state_age_seconds=30.0 if prior_state else None,
        prior_risk=0.8 if bad and prior_state else (0.2 if prior_state else None),
        prior_phi=None,
        prior_verdict=None,
        prior_coherence=None,
        prior_e=None,
        prior_i=None,
        prior_s=None,
        prior_v=None,
        snapshot_verdict=None,
        snapshot_e=None,
        snapshot_i=None,
        snapshot_s=None,
        snapshot_v=None,
        snapshot_phi=None,
        snapshot_coherence=None,
    )


def test_build_cohort_summary_counts_only_registry_prediction_bound_rows():
    rows = [
        _row(0, prediction_id="pred-1", binding="registry", prior_state=True),
        _row(1, bad=True, prediction_id="pred-2", binding="registry", harness="beam", prior_state=False),
        _row(2, prediction_id="pred-3", binding="prev_confidence_fallback"),
        _row(3),
    ]

    summary = build_cohort_summary(rows, scope="task", window_days=90, lead_minutes=30)

    assert summary.total_outcomes == 4
    assert summary.prediction_bound == 2
    assert summary.prediction_coverage == 0.5
    assert summary.prediction_bound_bad == 1
    assert summary.prediction_bound_prior_state == 1
    assert summary.by_harness_lane == {"beam": 1, "substrate": 1}


def test_format_cohort_report_keeps_holdout_language_and_lane_counts():
    rows = [
        _row(0, prediction_id="pred-1", binding="registry", prior_state=True),
        _row(1, bad=True, prediction_id="pred-2", binding="registry", harness="beam", prior_state=False),
    ]
    summary = build_cohort_summary(rows, scope="task", window_days=90, lead_minutes=30)

    report = format_cohort_report(summary)

    assert report.startswith("# Prospective Prediction Cohort")
    assert "scope: task" in report
    assert "prediction_bound: 2" in report
    assert "prediction_coverage: 1.000" in report
    assert "prediction_bound_prior_state: 1/2" in report
    assert "harness_lanes: beam=1,substrate=1" in report
    assert "prospective holdout" in report
