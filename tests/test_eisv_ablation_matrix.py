from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

from scripts.analysis.eisv_ablation_matrix import (
    AblationMatrixRow,
    build_matrix_row,
    estimate_delta_uncertainty,
    filter_rows_for_validation,
    format_matrix_report,
)
from scripts.analysis.eisv_skeptic_report import ModelScore, OutcomeRow


def _row(idx: int, *, bad: bool, risk: float | None, agent: str = "agent-a") -> OutcomeRow:
    ts = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=idx)
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
        prior_state_age_seconds=30.0 if risk is not None else None,
        prior_risk=risk,
        prior_phi=1.0 - risk if risk is not None else None,
        prior_verdict="high-risk" if risk is not None and risk > 0.7 else "safe",
        prior_coherence=0.5,
        prior_e=0.7,
        prior_i=0.7,
        prior_s=risk if risk is not None else None,
        prior_v=0.0,
        snapshot_verdict=None,
        snapshot_e=None,
        snapshot_i=None,
        snapshot_s=None,
        snapshot_v=None,
        snapshot_phi=None,
        snapshot_coherence=None,
    )


def test_filter_rows_for_validation_excludes_beam_harness_by_default():
    substrate = _row(0, bad=False, risk=0.1)
    beam = _row(1, bad=True, risk=None)
    beam = OutcomeRow(**{**beam.__dict__, "detail": {"harness": "beam"}})

    filtered = filter_rows_for_validation([substrate, beam])

    assert filtered == [substrate]
    assert filter_rows_for_validation([substrate, beam], exclude_harness_lanes=()) == [substrate, beam]


def test_build_matrix_row_summarizes_baseline_and_best_candidate():
    rows = []
    for idx in range(120):
        bad = idx % 10 in (8, 9)
        risk = 0.9 if bad else 0.1
        rows.append(_row(idx, bad=bad, risk=risk, agent=f"agent-{idx % 6}"))

    row = build_matrix_row(
        rows,
        scope="task",
        window_days=90,
        lead_minutes=30,
        train_fraction=0.7,
        min_feature_rows=10,
        uncertainty_resamples=50,
        uncertainty_seed=7,
    )

    assert row.scope == "task"
    assert row.window_days == 90
    assert row.lead_minutes == 30
    assert row.trusted == 120
    assert row.bad == 24
    assert row.prior_state == 120
    assert row.baseline_auc is not None
    assert row.baseline_brier is not None
    assert row.best_candidate in {
        "previous_bad_plus_prior_risk",
        "prior_risk_binned",
        "prior_phi_binned",
        "prior_s_binned",
        "prior_verdict",
    }
    assert row.best_auc_delta is not None
    assert row.best_brier_improvement is not None
    assert row.best_auc_delta_ci is not None
    assert row.best_brier_improvement_ci is not None
    assert row.best_brier_permutation_p is not None
    assert isinstance(row.beats_both, bool)


def _score(name: str, probs: tuple[float, ...], auc_scores: tuple[float, ...]) -> ModelScore:
    y_true = (0, 0, 0, 1, 1, 1)
    keys = tuple(f"row-{idx}" for idx in range(len(y_true)))
    return ModelScore(
        name=name,
        n_train=10,
        n_test=len(y_true),
        n_test_scored=len(y_true),
        auc=None,
        brier=None,
        scored_row_keys=keys,
        y_true=y_true,
        y_prob=probs,
        y_auc_score=auc_scores,
    )


def test_estimate_delta_uncertainty_reports_bootstrap_ci_and_permutation_p():
    baseline = _score(
        "previous_outcome_bad",
        probs=(0.30, 0.30, 0.30, 0.70, 0.70, 0.70),
        auc_scores=(0.30, 0.30, 0.30, 0.70, 0.70, 0.70),
    )
    candidate = _score(
        "prior_risk_binned",
        probs=(0.05, 0.10, 0.20, 0.80, 0.90, 0.95),
        auc_scores=(0.05, 0.10, 0.20, 0.80, 0.90, 0.95),
    )

    uncertainty = estimate_delta_uncertainty(
        baseline,
        candidate,
        resamples=80,
        seed=17,
    )

    assert uncertainty is not None
    assert uncertainty.paired_n == 6
    assert uncertainty.auc_delta_ci is not None
    assert uncertainty.brier_improvement_ci is not None
    assert uncertainty.brier_improvement_ci[0] > 0
    assert 0.0 <= uncertainty.brier_permutation_p <= 1.0

def test_format_matrix_report_contains_skeptical_ablation_table():
    rows = [
        AblationMatrixRow(
            scope="task",
            window_days=90,
            lead_minutes=30,
            trusted=120,
            bad=24,
            prior_state=120,
            prior_risk=120,
            baseline_auc=0.70,
            baseline_brier=0.12,
            best_candidate="prior_risk_binned",
            best_auc_delta=0.03,
            best_brier_improvement=0.01,
            best_auc_delta_ci=(0.01, 0.05),
            best_brier_improvement_ci=(0.002, 0.02),
            best_brier_permutation_p=0.04,
            beats_both=True,
            conclusion="KEEP TESTING: synthetic row",
        )
    ]

    report = format_matrix_report(rows, excluded_harness_lanes=("beam",))

    assert report.startswith("# EISV Ablation Matrix")
    assert "Excluded harness lanes: `beam`" in report
    assert "| Scope | Window days | Lead min | Trusted | Bad | Prior state | Prior risk |" in report
    assert "AUC delta 95% CI" in report
    assert "Brier improvement 95% CI" in report
    assert "Brier perm p" in report
    assert "| task | 90 | 30 | 120 | 24 | 120 | 120 |" in report
    assert "[0.010, 0.050]" in report
    assert "[0.0020, 0.0200]" in report
    assert "0.040" in report
    assert "prior_risk_binned" in report
    assert "KEEP TESTING" in report


def test_cli_help_runs_when_invoked_as_a_file():
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/analysis/eisv_ablation_matrix.py", "--help"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Run a compact EISV ablation matrix" in result.stdout
