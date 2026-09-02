import argparse
import asyncio
import dataclasses
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts.analysis import eisv_skeptic_report as skeptic_module
from scripts.analysis.eisv_skeptic_report import (
    MIN_DISPERSION_SNAPSHOTS,
    ModelScore,
    OutcomeRow,
    auc_score,
    brier_score,
    build_model_scores,
    build_report,
    parse_as_of,
    quantile_cuts,
    risk_bucket_rates,
    score_deltas_vs_baseline,
    smoothed_rate,
    split_rows_by_telemetry_dimension,
    summarize_telemetry_strata,
    summarize_conclusion,
)
from scripts.utils.date_utils import now_utc


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


def test_parse_args_defaults_to_trusted_anchor_scope():
    default_args = skeptic_module.parse_args([])
    legacy_args = skeptic_module.parse_args(["--anchor-scope", "all"])

    assert default_args.anchor_scope == "trusted"
    assert legacy_args.anchor_scope == "all"


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


def test_summarize_conclusion_does_not_promote_a_negative_split_to_refutation():
    rows = [_row(idx, bad=idx % 5 == 0, risk=0.5) for idx in range(120)]
    scores = [
        ModelScore("previous_outcome_bad", 84, 36, 36, auc=0.70, brier=0.10),
        ModelScore("prior_risk_binned", 84, 36, 36, auc=0.60, brier=0.12),
    ]

    conclusion = summarize_conclusion(rows, scores)

    assert conclusion.startswith("DESCRIPTIVE ONLY")
    assert "does not establish harm or refutation" in conclusion
    assert "SKEPTICAL" not in conclusion


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


def test_envelope_strata_keep_legacy_source_warmup_and_enforcement_explicit():
    legacy = _row(0, bad=False, risk=0.1)
    no_prior_state = dataclasses.replace(
        _row(4, bad=True, risk=None),
        prior_state_age_seconds=None,
    )
    warming = dataclasses.replace(
        _row(1, bad=False, risk=0.2),
        prior_telemetry_schema="eisv.telemetry.v1",
        prior_measurement_id="measurement-warming",
        prior_measurement_source="ode_fallback",
        prior_warmup_phase="bootstrapping",
        prior_is_baselined=False,
        prior_missing_inputs=("outcome_history",),
        prior_enforcement_requested=False,
        prior_enforcement_applied=False,
    )
    requested = dataclasses.replace(
        _row(2, bad=True, risk=0.8),
        prior_telemetry_schema="eisv.telemetry.v1",
        prior_measurement_id="measurement-requested",
        prior_measurement_source="physical",
        prior_warmup_phase="baselined",
        prior_is_baselined=True,
        prior_missing_inputs=(),
        prior_enforcement_requested=True,
        prior_enforcement_applied=False,
    )
    applied = dataclasses.replace(
        _row(3, bad=True, risk=0.9),
        prior_telemetry_schema="eisv.telemetry.v1",
        prior_measurement_id="measurement-applied",
        prior_measurement_source="physical",
        prior_warmup_phase="baselined",
        prior_is_baselined=True,
        prior_missing_inputs=(),
        prior_enforcement_requested=True,
        prior_enforcement_applied=True,
    )
    rows = [legacy, warming, requested, applied, no_prior_state]

    assert split_rows_by_telemetry_dimension(rows, "source") == {
        "legacy/no-envelope": [legacy],
        "no_prior_state": [no_prior_state],
        "ode_fallback": [warming],
        "physical": [requested, applied],
    }
    summaries = {
        (summary.dimension, summary.stratum): summary
        for summary in summarize_telemetry_strata(rows)
    }
    assert summaries[("enforcement", "requested_not_applied")].bad == 1
    assert summaries[("enforcement", "applied")].bad_clusters == 1
    assert summaries[("missingness", "complete")].rows == 2
    assert summaries[("warmup", "legacy/no-envelope")].rows == 1
    assert summaries[("source", "no_prior_state")].clusters == 0
    assert summaries[("source", "no_prior_state")].bad_clusters == 0

    as_of = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
    report = build_report(
        rows,
        scope="task",
        window_days=90,
        lead_minutes=30,
        train_fraction=0.7,
        generated_at=as_of,
        as_of=as_of,
    )
    assert "## EISV Telemetry Strata" in report
    assert "`physical`" in report
    assert "`requested_not_applied`" in report
    assert "intervention-conditioned audit views" in report
    assert "not causal estimates" in report
    assert "Data boundary: `2026-08-09T20:00:00+00:00` (frozen)" in report


def test_parse_as_of_requires_an_explicit_timezone():
    assert parse_as_of("2026-08-09T20:00:00Z") == datetime(
        2026, 8, 9, 20, 0, tzinfo=timezone.utc
    )
    with pytest.raises(argparse.ArgumentTypeError, match="explicit timezone"):
        parse_as_of("2026-08-09T20:00:00")


def test_skeptic_record_conversion_preserves_identity_metadata_for_fixture_filtering():
    record = {
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
            "prior_telemetry_schema": "eisv.telemetry.v1",
            "prior_measurement_id": "measurement-1",
            "prior_measurement_source": "physical",
            "prior_primary_source": "behavioral",
            "prior_behavioral_source": "physical",
            "prior_submitted_source": "physical",
            "prior_behavioral_confidence": "0.8",
            "prior_warmup_phase": "baselined",
            "prior_is_baselined": "true",
            "prior_missing_inputs": '["drift_norm"]',
            "prior_formula_version": "behavioral_sensor.v1",
            "prior_policy_action": "pause",
            "prior_policy_sub_action": "risk_pause",
            "prior_enforcement_requested": "true",
            "prior_enforcement_applied": "false",
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
    row = skeptic_module._row_from_record(record)

    assert row.detail["_identity_metadata"] == {"label": "perf-profile-checkin_be34425f"}
    assert row.prior_measurement_source == "physical"
    assert row.prior_behavioral_confidence == 0.8
    assert row.prior_is_baselined is True
    assert row.prior_missing_inputs == ("drift_norm",)
    assert row.prior_enforcement_requested is True
    assert row.prior_enforcement_applied is False


def test_skeptic_record_conversion_can_exclude_mutable_identity_metadata():
    record = {
        "ts": now_utc(),
        "outcome_id": "outcome-frozen",
        "agent_id": "agent-demo",
        "outcome_type": "task_failed",
        "outcome_score": 0.0,
        "is_bad": True,
        "detail": {"source": "auto_checkin"},
        "identity_metadata": {"purpose": "testing"},
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

    row = skeptic_module._row_from_record(
        record,
        include_identity_metadata=False,
    )

    assert row.detail == {"source": "auto_checkin"}


def test_fetch_rows_omits_mutable_identity_metadata_join_when_disabled(
    monkeypatch,
):
    observed: dict[str, str] = {}

    class FakeConnection:
        async def fetch(self, query: str, *_args: object) -> list[object]:
            observed["query"] = query
            return []

        async def close(self) -> None:
            return None

    async def fake_connect(_db_url: str) -> FakeConnection:
        return FakeConnection()

    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        SimpleNamespace(connect=fake_connect),
    )

    rows = asyncio.run(
        skeptic_module.fetch_rows(
            "postgresql://example.invalid/db",
            window_days=90,
            lead_minutes=30,
            outcome_types=("task_completed",),
            include_identity_metadata=False,
        )
    )

    assert rows == []
    assert "NULL::jsonb AS identity_metadata" in observed["query"]
    assert "ident_meta.metadata" not in observed["query"]
    assert "ORDER BY ident_meta.updated_at" not in observed["query"]
    assert "eisv_telemetry,measurement,primary,source" in observed["query"]
    assert "prior_enforcement_applied" in observed["query"]


def test_zero_positive_training_split_yields_no_deltas():
    """A baseline fitted on no positives is untrained, not accurate.

    Its group rates then differ only in the Laplace denominator, so its ranking
    is tie-break noise that any continuous candidate clears. Reporting lift over
    it manufactures a result -- observed live at strict/365d, where the baseline
    read AUC 0.355 (below chance) purely because every bad label landed in the
    chronological test half.
    """
    rows = [
        _row(idx, bad=idx >= 80, risk=0.9 if idx >= 80 else 0.1, agent=f"agent-{idx % 5}")
        for idx in range(100)
    ]
    scores = build_model_scores(rows, train_fraction=0.7, min_feature_rows=10)

    baseline = next(s for s in scores if s.name == "previous_outcome_bad")
    assert baseline.n_train_bad == 0

    assert score_deltas_vs_baseline(scores) == []
    assert "no bad outcomes" in summarize_conclusion(rows, scores)


def test_auc_delta_is_symmetric_and_keeps_the_legacy_raw_number():
    """Deltas must not score the candidate raw and the baseline fitted.

    `prior_s_binned` supplies `raw_score_fn`, so its old AUC ranked by the
    continuous feature while the baseline ranked by a tie-saturated step
    function. Only the fitted side degrades under label starvation, so the
    asymmetry showed up as candidate lift.
    """
    rows = [
        _row(idx, bad=(idx % 7 == 0), risk=0.9 if idx % 7 == 0 else 0.1, agent=f"agent-{idx % 4}")
        for idx in range(140)
    ]
    scores = build_model_scores(rows, train_fraction=0.7, min_feature_rows=10)
    deltas = score_deltas_vs_baseline(scores)
    assert deltas

    by_name = {delta.name: delta for delta in deltas}
    risk_delta = by_name["prior_risk_binned"]
    # Symmetric delta is reproducible from the fitted AUCs the table prints.
    candidate = next(s for s in scores if s.name == "prior_risk_binned")
    baseline = next(s for s in scores if s.name == "previous_outcome_bad")
    assert candidate.auc_fitted is not None
    assert baseline.auc_fitted is not None
    # The legacy asymmetric number is retained but distinct in general.
    assert risk_delta.auc_delta_raw is not None


def test_cli_threads_one_fixture_rule_into_fetch_and_report(monkeypatch, tmp_path):
    import asyncio

    from scripts.analysis import eisv_skeptic_report as skeptic_module

    seen: dict = {}

    async def fake_fetch_rows(_db_url, **kwargs):
        seen["fetch"] = kwargs
        return []

    def fake_build_report(rows, **kwargs):
        seen["report"] = kwargs
        return "stub report"

    monkeypatch.setattr(skeptic_module, "fetch_rows", fake_fetch_rows)
    monkeypatch.setattr(skeptic_module, "build_report", fake_build_report)
    out = tmp_path / "skeptic.md"
    args = skeptic_module.parse_args(
        ["--db-url", "postgresql://unused", "--fixture-rule", "corrected", "--output", str(out)]
    )
    rc = asyncio.run(skeptic_module.main_async(args))
    assert rc == 0
    assert seen["fetch"]["fixture_rule"] == "corrected"
    assert seen["report"]["fixture_rule"] == "corrected"


def test_parse_args_fixture_rule_defaults_to_corrected():
    from scripts.analysis import eisv_skeptic_report as skeptic_module

    assert skeptic_module.parse_args([]).fixture_rule == "corrected"
    assert skeptic_module.parse_args(["--fixture-rule", "registered"]).fixture_rule == "registered"
