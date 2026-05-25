from datetime import datetime, timedelta, timezone

from scripts.analysis.eisv_skeptic_report import (
    OutcomeRow,
    auc_score,
    brier_score,
    build_model_scores,
    quantile_cuts,
    risk_bucket_rates,
    smoothed_rate,
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
