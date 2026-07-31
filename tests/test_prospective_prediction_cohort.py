from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.analysis.eisv_skeptic_report import OutcomeRow
from scripts.analysis.prospective_prediction_cohort import (
    ReadinessThresholds,
    build_cohort_summary,
    evaluate_readiness,
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
    verification_source: str | None = "external_signal",
    snapshot_present: bool = True,
    snapshot_missing: bool = False,
) -> OutcomeRow:
    detail = {}
    if prediction_id:
        detail["prediction_id"] = prediction_id
    if binding:
        detail["prediction_binding"] = binding
    if harness:
        detail["harness"] = harness
    if snapshot_missing:
        detail["snapshot_missing"] = True
    return OutcomeRow(
        ts=datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=idx),
        agent_id=f"agent-{idx % 2}",
        outcome_type="task_failed" if bad else "task_completed",
        is_bad=bad,
        outcome_score=0.0 if bad else 1.0,
        verification_source=verification_source,
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
        snapshot_e=0.1 if snapshot_present else None,
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


def test_build_cohort_summary_uses_only_trusted_joinable_outcomes():
    rows = [
        _row(0, prediction_id="pred-trusted", binding="registry"),
        _row(
            1,
            prediction_id="pred-no-state",
            binding="registry",
            snapshot_present=False,
        ),
        _row(
            2,
            prediction_id="pred-self-observed",
            binding="registry",
            verification_source="server_observation",
        ),
        _row(
            3,
            prediction_id="pred-soft",
            binding="registry",
            verification_source="agent_reported_tool_result",
        ),
        _row(
            4,
            prediction_id="pred-explicitly-missing",
            binding="registry",
            snapshot_missing=True,
        ),
    ]

    summary = build_cohort_summary(
        rows,
        scope="task",
        window_days=90,
        lead_minutes=30,
    )

    assert summary.total_outcomes == 1
    assert summary.prediction_bound == 1
    assert summary.prediction_coverage == 1.0


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
    assert "online agent-state estimation (agent proprioception)" in report
    assert "not an outcome oracle or bad-verdict dispenser" in report
    assert "external labels still own outcome truth" in report


def test_evaluate_readiness_reports_strong_when_thresholds_are_met():
    rows = [
        _row(0, prediction_id="pred-1", binding="registry", prior_state=True),
        _row(1, bad=True, prediction_id="pred-2", binding="registry", prior_state=True),
        _row(2, prediction_id="pred-3", binding="registry", prior_state=False),
        _row(3),
    ]
    summary = build_cohort_summary(rows, scope="task", window_days=90, lead_minutes=30)
    thresholds = ReadinessThresholds(
        min_prediction_bound=3,
        min_prediction_bound_bad=1,
        min_prediction_coverage=0.5,
        min_prediction_prior_state_coverage=0.6,
    )

    readiness = evaluate_readiness(summary, thresholds)

    assert readiness.status == "strong"
    assert readiness.reasons == ()


def test_evaluate_readiness_explains_weak_dataset_gaps():
    rows = [
        _row(0, prediction_id="pred-1", binding="registry", prior_state=False),
        _row(1),
        _row(2),
        _row(3),
    ]
    summary = build_cohort_summary(rows, scope="task", window_days=90, lead_minutes=30)
    thresholds = ReadinessThresholds(
        min_prediction_bound=3,
        min_prediction_bound_bad=1,
        min_prediction_coverage=0.5,
        min_prediction_prior_state_coverage=0.8,
    )

    readiness = evaluate_readiness(summary, thresholds)

    assert readiness.status == "not_ready"
    assert "prediction_bound 1 < 3" in readiness.reasons
    assert "prediction_bound_bad 0 < 1" in readiness.reasons
    assert "prediction_coverage 0.250 < 0.500" in readiness.reasons
    assert "prediction_prior_state_coverage 0.000 < 0.800" in readiness.reasons


def test_format_cohort_report_includes_readiness_gate():
    rows = [
        _row(0, prediction_id="pred-1", binding="registry", prior_state=False),
        _row(1),
    ]
    summary = build_cohort_summary(rows, scope="task", window_days=90, lead_minutes=30)
    thresholds = ReadinessThresholds(
        min_prediction_bound=2,
        min_prediction_bound_bad=1,
        min_prediction_coverage=0.8,
        min_prediction_prior_state_coverage=0.5,
    )

    report = format_cohort_report(summary, thresholds=thresholds)

    assert "readiness: not_ready" in report
    assert "readiness_reasons:" in report
    assert "- prediction_bound 1 < 2" in report
    assert "readiness_thresholds: min_prediction_bound=2" in report
