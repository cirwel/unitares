from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.analysis.eisv_skeptic_report import OutcomeRow, build_report
from scripts.analysis.outcome_inventory import (
    OutcomeInventoryRow,
    build_inventory,
    format_inventory_report,
)
from scripts.analysis.prospective_prediction_cohort import (
    build_cohort_summary,
    format_cohort_report,
)

ROOT = Path(__file__).resolve().parents[1]


def _outcome_row(idx: int, *, bad: bool, risk: float | None = None) -> OutcomeRow:
    risk = risk if risk is not None else (0.9 if bad else 0.1)
    return OutcomeRow(
        ts=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=idx),
        agent_id=f"agent-{idx % 6}",
        outcome_type="task_failed" if bad else "task_completed",
        is_bad=bad,
        outcome_score=0.0 if bad else 1.0,
        verification_source="server_observation",
        reported_confidence=None,
        reported_complexity=None,
        detail={"prediction_binding": "registry", "prediction_id": f"pred-{idx}"},
        prior_state_age_seconds=30.0,
        prior_risk=risk,
        prior_phi=1.0 - risk,
        prior_verdict="high-risk" if risk > 0.7 else "safe",
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


def test_canonical_eisv_contract_doc_names_proprioception_not_grand_jury():
    doc = ROOT / "docs" / "ontology" / "eisv-proprioception-contract.md"

    text = doc.read_text()

    assert "EISV is proprioception" in text
    assert "not an outcome oracle" in text
    assert "not a grand jury" in text
    assert "bad-verdict dispenser" in text
    assert "measurement → diagnosis → policy → enforcement" in text
    assert "external outcome evidence" in text
    assert "task-negative" in text
    assert "authority/harm" in text


def test_landing_docs_link_the_proprioception_contract():
    readme = (ROOT / "README.md").read_text()
    start_here = (ROOT / "docs" / "guides" / "START_HERE.md").read_text()
    evaluation_index = (ROOT / "docs" / "EVALUATION_INDEX.md").read_text()

    for text in (readme, start_here, evaluation_index):
        assert "eisv-proprioception-contract.md" in text
    assert "not an outcome oracle" in readme
    assert "grand jury" in readme
    assert "bad-verdict dispenser" in readme
    assert "task-negative" in evaluation_index
    assert "hand down bad verdicts" in evaluation_index


def test_reports_preserve_proprioception_and_outcome_oracle_boundary():
    rows = [_outcome_row(idx, bad=idx >= 96) for idx in range(120)]

    skeptic_report = build_report(
        rows,
        scope="task",
        window_days=90,
        lead_minutes=30,
        train_fraction=0.7,
        generated_at=rows[0].ts + timedelta(days=1),
    )
    assert "not an outcome oracle or bad-verdict dispenser" in skeptic_report
    assert "Outcome labels come from external evidence/rubrics" in skeptic_report

    inventory = build_inventory(
        [
            OutcomeInventoryRow(
                outcome_type="test_failed",
                is_bad=True,
                verification_source="server_observation",
                detail={"prediction_binding": "registry", "prediction_id": "pred-1"},
                prior_state_by_lead={0.0: True},
            )
        ],
        lead_minutes=(0.0,),
    )
    inventory_report = format_inventory_report(
        inventory,
        window_days=90,
        lead_minutes=(0.0,),
    )
    assert "`bad` is an outcome-label class (`is_bad=true`)" in inventory_report
    assert "not a moral verdict or a prevented outcome" in inventory_report
    assert "not a bad-verdict dispenser" in inventory_report
    assert "CI/test failure is task-negative evidence" in inventory_report

    cohort_summary = build_cohort_summary(
        rows[:10], scope="task", window_days=90, lead_minutes=30
    )
    cohort_report = format_cohort_report(cohort_summary)
    assert "not a grand jury" in cohort_report
    assert "not an outcome oracle or bad-verdict dispenser" in cohort_report
    assert "registry-bound prediction coverage for future holdout scoring" in cohort_report
