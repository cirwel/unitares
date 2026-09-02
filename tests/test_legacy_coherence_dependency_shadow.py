from datetime import datetime, timedelta, timezone

import pytest

from scripts.analysis.legacy_coherence_dependency_shadow import (
    MIN_BAD_CLUSTERS,
    SHADOW_OUTCOME_SQL,
    ShadowOutcomeRow,
    build_report,
    summarize_channel,
)
from src.eisv_telemetry import EISV_SHADOW_ABLATIONS_SCHEMA


def _row(
    idx: int,
    *,
    bad: bool,
    deployed: float,
    candidate: float,
    measurement: str | None = None,
    agent: str | None = None,
) -> ShadowOutcomeRow:
    ts = datetime(2026, 8, 12, tzinfo=timezone.utc) + timedelta(minutes=idx)
    return ShadowOutcomeRow(
        ts=ts,
        outcome_id=f"outcome-{idx}",
        agent_id=agent or f"agent-{idx % 5}",
        outcome_type="task_failed" if bad else "task_completed",
        is_bad=bad,
        prior_state_recorded_at=ts - timedelta(minutes=30),
        prior_state_age_seconds=1800.0,
        prior_measurement_id=measurement or f"measurement-{idx}",
        prior_derivation_kind="behavioral_sensor",
        shadow_schema=EISV_SHADOW_ABLATIONS_SCHEMA,
        behavioral_eligible=True,
        deployed_e=deployed,
        candidate_e=candidate,
        deployed_i=deployed,
        candidate_i=candidate,
        confidence_eligible=True,
        deployed_confidence=deployed,
        candidate_confidence=candidate,
    )


def test_query_uses_trusted_external_anchor_and_leak_safe_prior_state():
    assert "o.verification_source = 'external_signal'" in SHADOW_OUTCOME_SQL
    assert "s.synthetic IS NOT TRUE" in SHADOW_OUTCOME_SQL
    assert "s.recorded_at <= o.ts -" in SHADOW_OUTCOME_SQL
    assert "ORDER BY s.recorded_at DESC" in SHADOW_OUTCOME_SQL


def test_outcome_metrics_are_withheld_below_bad_cluster_floor():
    rows = [
        _row(0, bad=False, deployed=0.8, candidate=0.8),
        _row(1, bad=True, deployed=0.2, candidate=0.2),
    ]

    read = summarize_channel(
        rows,
        channel="behavioral_E",
        deployed=lambda row: row.deployed_e,
        candidate=lambda row: row.candidate_e,
        min_bad_clusters=MIN_BAD_CLUSTERS,
        resamples=50,
    )

    assert read.status == "WAIT_SAMPLE_FLOOR"
    assert read.deployed_auc is None
    assert read.candidate_auc is None
    assert read.mean_signed_delta == 0.0


def test_identical_candidate_passes_after_cluster_floor():
    rows = [
        _row(
            idx,
            bad=idx >= 5,
            deployed=0.8 if idx < 5 else 0.2,
            candidate=0.8 if idx < 5 else 0.2,
        )
        for idx in range(10)
    ]

    read = summarize_channel(
        rows,
        channel="behavioral_E",
        deployed=lambda row: row.deployed_e,
        candidate=lambda row: row.candidate_e,
        min_bad_clusters=5,
        resamples=100,
        seed=7,
    )

    assert read.status == "PASS_AUC_NONINFERIORITY"
    assert read.deployed_auc == pytest.approx(1.0)
    assert read.candidate_auc == pytest.approx(1.0)
    assert read.candidate_minus_deployed_auc == pytest.approx(0.0)
    assert read.auc_delta_ci95 == pytest.approx((0.0, 0.0))


def test_cluster_floor_counts_shared_prior_state_once():
    rows = [
        _row(
            0,
            bad=True,
            deployed=0.2,
            candidate=0.2,
            measurement="shared",
            agent="agent-shared",
        ),
        _row(
            1,
            bad=True,
            deployed=0.2,
            candidate=0.2,
            measurement="shared",
            agent="agent-shared",
        ),
        _row(2, bad=False, deployed=0.8, candidate=0.8),
    ]

    read = summarize_channel(
        rows,
        channel="behavioral_I",
        deployed=lambda row: row.deployed_i,
        candidate=lambda row: row.candidate_i,
        min_bad_clusters=2,
        resamples=50,
    )

    assert read.bad_rows == 2
    assert read.bad_clusters == 1
    assert read.status == "WAIT_SAMPLE_FLOOR"


def test_report_names_non_actuation_and_recursive_replay_boundary():
    report = build_report(
        [_row(0, bad=False, deployed=0.8, candidate=0.75)],
        scope="task",
        window_days=365,
        lead_minutes=30.0,
        min_bad_clusters=2,
        resamples=20,
    )

    assert "not an actuator" in report
    assert "Behavioral E/I still require recursive history" in report
    assert "WAIT_SAMPLE_FLOOR" in report


def test_report_header_names_the_fixture_rule_and_its_contract_standing():
    from scripts.analysis.legacy_coherence_dependency_shadow import build_report

    registered = build_report([], scope="task", window_days=21, lead_minutes=30.0, fixture_rule="registered")
    assert "Fixture rule: `registered` (the contract's item 2 as registered)" in registered
    corrected = build_report([], scope="task", window_days=21, lead_minutes=30.0, fixture_rule="corrected")
    assert "Fixture rule: `corrected` (a disclosed deviation from the contract's item 2)" in corrected


def test_cli_threads_one_fixture_rule_into_fetch_and_report(monkeypatch, tmp_path):
    from scripts.analysis import legacy_coherence_dependency_shadow as shadow_module

    seen: dict = {}

    async def fake_fetch_rows(_db_url, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(shadow_module, "fetch_rows", fake_fetch_rows)
    out = tmp_path / "shadow.md"
    rc = shadow_module.main(
        ["--db-url", "postgresql://unused", "--fixture-rule", "corrected", "--output", str(out)]
    )
    assert rc == 0
    assert seen["fixture_rule"] == "corrected"
    assert "Fixture rule: `corrected` (a disclosed deviation from the contract's item 2)" in out.read_text(encoding="utf-8")
