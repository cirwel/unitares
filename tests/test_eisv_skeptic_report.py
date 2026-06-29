import dataclasses
from datetime import datetime, timedelta, timezone

from scripts.analysis import eisv_skeptic_report as skeptic_module
from scripts.analysis.eisv_skeptic_report import (
    MIN_DISPERSION_SNAPSHOTS,
    ModelScore,
    OutcomeRow,
    auc_score,
    brier_score,
    build_model_scores,
    build_report,
    quantile_cuts,
    risk_bucket_rates,
    score_deltas_vs_baseline,
    smoothed_rate,
    summarize_conclusion,
)


def _row(idx: int, *, bad: bool, risk: float | None, agent: str = "agent-a") -> OutcomeRow:
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=idx)
    return OutcomeRow(
        ts=ts,
        agent_id=agent,
        outcome_type="task_failed" if bad else "task_completed",
        is_bad=bad,
        outcome_score=0.0 if bad else 1.0,
        verification_source="server_observation",
        reported_confidence=None,
        reported_complexity=None,
        detail={},
        prior_state_age_seconds=30.0,
        prior_risk=risk,
        prior_phi=1.0 - risk if risk is not None else None,
        prior_verdict="high-risk" if risk is not None and risk > 0.7 else "safe",
        prior_coherence=0.5,
        prior_e=0.7,
        prior_i=0.7,
        prior_s=0.2,
        prior_v=0.0,
        snapshot_verdict=None,
        snapshot_e=None,
        snapshot_i=None,
        snapshot_s=None,
        snapshot_v=None,
        snapshot_phi=None,
        snapshot_coherence=None,
    )


def test_auc_handles_ties_with_average_ranks():
    assert auc_score([0, 1, 0, 1], [0.1, 0.2, 0.2, 0.9]) == 0.875


def test_auc_returns_none_for_single_class():
    assert auc_score([0, 0, 0], [0.1, 0.2, 0.3]) is None


def test_brier_score():
    assert brier_score([0, 1], [0.25, 0.75]) == 0.0625


def test_smoothed_rate_avoids_zero_and_one():
    assert smoothed_rate(0, 0) == 0.5
    assert smoothed_rate(0, 10) > 0.0
    assert smoothed_rate(10, 10) < 1.0


def test_quantile_cuts_are_monotonic():
    assert quantile_cuts([0.1, 0.2, 0.3, 0.4, 0.5]) == [0.2, 0.3, 0.4]


def test_risk_bucket_rates_groups_bad_rows():
    rows = [
        _row(0, bad=False, risk=0.1),
        _row(1, bad=False, risk=0.2),
        _row(2, bad=True, risk=0.8),
        _row(3, bad=True, risk=0.9),
    ]
    _cuts, buckets = risk_bucket_rates(rows, bucket_count=2)
    assert buckets[0][1:] == (2, 0, 0.0)
    assert buckets[1][1:] == (2, 2, 1.0)


def test_build_model_scores_includes_prior_risk_when_covered():
    rows = []
    for idx in range(100):
        bad = idx >= 80
        risk = 0.9 if bad else 0.1
        rows.append(_row(idx, bad=bad, risk=risk, agent=f"agent-{idx % 5}"))
    scores = build_model_scores(rows, train_fraction=0.7, min_feature_rows=10)
    names = {score.name for score in scores}
    assert "global_bad_rate" in names
    assert "prior_risk_binned" in names


def test_score_deltas_vs_baseline_reports_auc_and_brier_lift():
    deltas = score_deltas_vs_baseline([
        ModelScore("previous_outcome_bad", 70, 30, 30, auc=0.70, brier=0.120),
        ModelScore("prior_risk_binned", 70, 30, 30, auc=0.73, brier=0.110),
        ModelScore("prior_phi_binned", 70, 30, 30, auc=0.74, brier=0.130),
        ModelScore("prior_verdict", 70, 30, 30, auc=None, brier=0.115),
    ])

    assert [delta.name for delta in deltas] == ["prior_risk_binned", "prior_phi_binned"]
    assert deltas[0].auc_delta == 0.03
    assert deltas[0].brier_improvement == 0.01
    assert deltas[0].beats_baseline is True
    assert deltas[1].auc_delta == 0.04
    assert deltas[1].brier_improvement == -0.01
    assert deltas[1].beats_baseline is False


def test_score_deltas_use_candidate_covered_rows_for_baseline():
    deltas = score_deltas_vs_baseline([
        ModelScore(
            "previous_outcome_bad",
            70,
            30,
            30,
            auc=0.0,
            brier=0.80,
            scored_row_keys=("a", "b", "c", "d"),
            y_true=(0, 0, 1, 1),
            y_prob=(0.9, 0.1, 0.1, 0.9),
            y_auc_score=(0.9, 0.1, 0.1, 0.9),
        ),
        ModelScore(
            "prior_risk_binned",
            70,
            30,
            2,
            auc=0.9,
            brier=0.10,
            scored_row_keys=("b", "d"),
            y_true=(0, 1),
            y_prob=(0.4, 0.6),
            y_auc_score=(0.4, 0.6),
        ),
    ])

    assert len(deltas) == 1
    assert deltas[0].auc_delta == 0.0
    assert deltas[0].brier_improvement == -0.15
    assert deltas[0].paired_n == 2
    assert deltas[0].beats_baseline is False


def test_summarize_conclusion_prefers_candidates_that_beat_both_metrics():
    rows = [_row(idx, bad=idx % 5 == 0, risk=0.5) for idx in range(120)]
    scores = [
        ModelScore("previous_outcome_bad", 84, 36, 36, auc=0.50, brier=0.020),
        ModelScore("prior_phi_binned", 84, 36, 36, auc=0.95, brier=0.030),
        ModelScore("prior_risk_binned", 84, 36, 36, auc=0.80, brier=0.0195),
    ]

    conclusion = summarize_conclusion(rows, scores)

    assert "prior_risk_binned" in conclusion
    assert "do not beat" not in conclusion


def _with_dispersion(row: OutcomeRow, disp: float | None, n: int) -> OutcomeRow:
    return dataclasses.replace(row, prior_s_disp=disp, n_prior_snapshots=n)


def test_build_model_scores_includes_dispersion_when_covered():
    rows = []
    for idx in range(100):
        bad = idx >= 80
        # high dispersion separates bad outcomes; low dispersion for trusted ones
        disp = 0.9 if bad else 0.1
        row = _row(idx, bad=bad, risk=0.5, agent=f"agent-{idx % 5}")
        rows.append(_with_dispersion(row, disp, n=MIN_DISPERSION_SNAPSHOTS + 2))
    scores = build_model_scores(rows, train_fraction=0.7, min_feature_rows=10)
    names = {score.name for score in scores}
    assert "prior_eisv_dispersion_binned" in names
    assert "previous_bad_plus_dispersion" in names


def test_dispersion_models_absent_without_coverage():
    # prior_s_disp left None (default) -> no dispersion models built
    rows = [
        _row(idx, bad=idx >= 80, risk=0.5, agent=f"agent-{idx % 5}")
        for idx in range(100)
    ]
    scores = build_model_scores(rows, train_fraction=0.7, min_feature_rows=10)
    names = {score.name for score in scores}
    assert "prior_eisv_dispersion_binned" not in names
    assert "previous_bad_plus_dispersion" not in names


def test_build_report_includes_ablation_delta_section():
    rows = []
    for idx in range(120):
        bad = idx >= 96
        risk = 0.9 if bad else 0.1
        rows.append(_row(idx, bad=bad, risk=risk, agent=f"agent-{idx % 6}"))

    report = build_report(
        rows,
        scope="task",
        window_days=90,
        lead_minutes=30,
        train_fraction=0.7,
        generated_at=rows[0].ts + timedelta(days=1),
    )

    assert "## Ablation vs Previous-Outcome Baseline" in report
    assert "| `prior_risk_binned` |" in report
    assert "AUC delta" in report
    assert "Brier improvement" in report


def test_skeptic_record_conversion_preserves_identity_metadata_for_fixture_filtering():
    row = skeptic_module._row_from_record(
        {
            "ts": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "outcome_id": "outcome-1",
            "agent_id": "agent-demo",
            "outcome_type": "task_failed",
            "outcome_score": 0.0,
            "is_bad": True,
            "detail": {"source": "auto_checkin"},
            "identity_metadata": {"label": "perf-profile-checkin_be34425f"},
            "verification_source": "agent_reported_tool_result",
            "prior_state_age_seconds": None,
            "prior_risk": None,
            "prior_phi": None,
            "prior_verdict": None,
            "prior_coherence": None,
            "prior_e": None,
            "prior_i": None,
            "prior_s": None,
            "prior_v": None,
            "eisv_verdict": None,
            "eisv_e": None,
            "eisv_i": None,
            "eisv_s": None,
            "eisv_v": None,
            "eisv_phi": None,
            "eisv_coherence": None,
            "n_prior_snapshots": None,
            "prior_s_disp": None,
            "prior_e_disp": None,
            "prior_i_disp": None,
            "prior_v_disp": None,
            "prior_risk_disp": None,
        }
    )

    assert row.detail["_identity_metadata"] == {"label": "perf-profile-checkin_be34425f"}
