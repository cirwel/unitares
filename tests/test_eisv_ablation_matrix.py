from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

from scripts.analysis.eisv_ablation_matrix import (
    AblationMatrixRow,
    build_matrix_row,
    format_matrix_report,
)
from scripts.analysis.eisv_skeptic_report import OutcomeRow


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
    assert isinstance(row.beats_both, bool)


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
            beats_both=True,
            conclusion="KEEP TESTING: synthetic row",
        )
    ]

    report = format_matrix_report(rows)

    assert report.startswith("# EISV Ablation Matrix")
    assert "| Scope | Window days | Lead min | Trusted | Bad | Prior state | Prior risk |" in report
    assert "| task | 90 | 30 | 120 | 24 | 120 | 120 |" in report
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
