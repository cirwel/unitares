"""Synthetic negative-control fixtures for ablation tests."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts.analysis.ablation_negative_controls import (
    SYNTHETIC_FIXTURE_SOURCE,
    build_negative_control_inventory,
    build_negative_control_matrix_rows,
    build_negative_control_outcome_rows,
    serialize_outcome_rows,
)

NOW = datetime(2026, 6, 14, 23, 30, tzinfo=timezone.utc)


def test_negative_control_rows_are_synthetic_strict_and_not_persistable() -> None:
    """Negative controls create known bad strict outcomes without real writes."""
    rows = build_negative_control_outcome_rows(generated_at=NOW)

    assert len(rows) >= 40
    assert {row.outcome_type for row in rows} >= {"test_failed", "tool_rejected"}
    assert any(row.is_bad for row in rows)
    assert all(row.verification_source == SYNTHETIC_FIXTURE_SOURCE for row in rows)
    assert all(row.agent_id.startswith("synthetic-negative-control/") for row in rows)
    assert all(row.detail["synthetic_negative_control"] is True for row in rows)
    assert all(row.detail["do_not_persist"] is True for row in rows)
    assert all(row.detail["prediction_binding"] == "synthetic_negative_control" for row in rows)
    assert all(row.prior_state_age_seconds is not None for row in rows)
    assert min(row.prior_risk for row in rows if row.is_bad) > max(
        row.prior_risk for row in rows if not row.is_bad
    )


def test_inventory_reports_synthetic_strict_bad_without_real_provenance() -> None:
    """Inventory sees the bad class but labels it synthetic-only."""
    inventory = build_negative_control_inventory(generated_at=NOW, lead_minutes=(0, 5, 30))

    assert inventory.total_outcomes >= 40
    assert inventory.total_bad > 0
    assert inventory.strict_bad > 0
    assert inventory.hard_exogenous_count == inventory.total_outcomes
    assert inventory.eprocess_eligible_count == inventory.total_outcomes
    assert inventory.total_prediction_id_count == inventory.total_outcomes
    assert {bucket.verification_source for bucket in inventory.buckets} == {
        SYNTHETIC_FIXTURE_SOURCE
    }
    assert {bucket.prediction_binding for bucket in inventory.buckets} == {
        "synthetic_negative_control"
    }


def test_matrix_rows_are_explicitly_labeled_synthetic_controls() -> None:
    """Ablation smoke can prove detection while refusing validation language."""
    matrix_rows = build_negative_control_matrix_rows(
        generated_at=NOW,
        scopes=("strict", "task"),
        window_days=90,
        lead_minutes=5,
    )

    assert [row.scope for row in matrix_rows] == ["strict", "task"]
    assert all(row.bad > 0 for row in matrix_rows)
    assert all(row.prior_state == row.trusted for row in matrix_rows)
    assert all("SYNTHETIC NEGATIVE CONTROL" in row.conclusion for row in matrix_rows)
    assert any(row.beats_both for row in matrix_rows)


def test_serialized_rows_keep_fixture_label_and_drop_private_fields() -> None:
    """JSONL export is local fixture data, not a DB/import payload."""
    rows = build_negative_control_outcome_rows(generated_at=NOW, count=4)

    serialized = serialize_outcome_rows(rows)

    assert len(serialized) == 4
    assert all(item["detail"]["synthetic_negative_control"] is True for item in serialized)
    assert all(item["detail"]["do_not_persist"] is True for item in serialized)
    assert all("continuity_token" not in json.dumps(item) for item in serialized)
    assert all("GOVERNANCE_DATABASE_URL" not in json.dumps(item) for item in serialized)


def test_cli_outputs_summary_and_writes_local_jsonl_only(tmp_path: Path) -> None:
    """The CLI is a safe red-team fixture generator, not a write adapter."""
    output_path = tmp_path / "negative-controls.jsonl"
    script = Path("scripts/analysis/ablation_negative_controls.py")

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--generated-at",
            "2026-06-14T23:30:00+00:00",
            "--count",
            "12",
            "--output-jsonl",
            str(output_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)
    assert summary == {
        "event_type": "ablation_negative_controls",
        "mode": "synthetic_only",
        "status": "fixtures_written",
        "generated_rows": 12,
        "strict_bad": 4,
    }
    assert str(output_path) not in completed.stdout
    assert output_path.exists()
    exported = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert len(exported) == 12
    assert all(item["detail"]["do_not_persist"] is True for item in exported)
