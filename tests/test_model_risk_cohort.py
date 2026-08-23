from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from src.identity.model_risk_cohort import (
    MODEL_RISK_COHORT_SQL,
    build_model_risk_cohort_report,
    collect_model_risk_observations,
    normalize_model_risk_observation,
)
from src.model_harness_provenance import build_runtime_provenance_from_values


def _runtime(model: str, harness: str, *, version: str = "1.0.0") -> dict:
    return build_runtime_provenance_from_values(
        model_identifier=model,
        model_provider="test-provider",
        model_source="provider_reported",
        model_exact=True,
        model_channel="host_hook_payload",
        harness_type=harness,
        harness_type_source="harness_reported",
        harness_version=version,
        harness_version_source="harness_reported",
    )


def _row(
    state_id: str,
    model: str,
    harness: str,
    risk: float,
    *,
    task_type: str = "bugfix",
    updates: int = 30,
    baselined: bool = True,
    exposure: float = 1800.0,
    version: str = "1.0.0",
) -> dict:
    return {
        "state_id": state_id,
        "agent_uuid": f"agent-{state_id}",
        "recorded_at": "2026-08-24T00:00:00+00:00",
        "risk_score": risk,
        "runtime_provenance": _runtime(model, harness, version=version),
        "task_type": task_type,
        "behavioral_updates": updates,
        "is_baselined": baselined,
        "measured_update_index": updates,
        "exposure_seconds": exposure,
    }


def test_normalize_stratifies_readiness_updates_task_and_exposure():
    row = normalize_model_risk_observation(_row("1", "gpt-5.6-sol", "codex-cli", 0.61))

    assert row.attribution_status == "eligible_exact"
    assert row.model_identifier == "gpt-5.6-sol"
    assert row.harness_type == "codex-cli"
    assert row.readiness == "warm"
    assert row.update_bucket == "25-99"
    assert row.task_type == "bugfix"
    assert row.exposure_bucket == "15-60m"


def test_legacy_null_is_counted_and_never_inferred_from_agent_name():
    row = normalize_model_risk_observation(
        {
            "state_id": "legacy",
            "agent_uuid": "Codex_Gpt_5_6_Sol_20260822",
            "risk_score": 0.8,
            "runtime_provenance": None,
            "behavioral_updates": 50,
            "is_baselined": True,
        }
    )

    assert row.attribution_status == "legacy_unversioned"
    assert row.model_identifier is None
    assert row.harness_type is None


def test_report_requires_like_for_like_warm_cells_before_comparison():
    rows = [
        _row("s1", "gpt-5.6-sol", "codex-cli", 0.7),
        _row("s2", "gpt-5.6-sol", "codex-cli", 0.5),
        _row("c1", "claude-opus-4-1", "claude-code", 0.3),
        _row("c2", "claude-opus-4-1", "claude-code", 0.4),
    ]

    report = build_model_risk_cohort_report(
        rows,
        capture_start="2026-08-23T00:00:00+00:00",
        capture_end="2026-08-30T00:00:00+00:00",
        min_cell_size=2,
    )

    assert report["comparison_readiness"]["status"] == "ready_for_descriptive_comparison"
    assert len(report["like_for_like_warm_cells"]) == 1
    assert len(report["like_for_like_warm_cells"][0]["cohorts"]) == 2
    assert report["authority"]["policy_change_allowed"] is False
    assert report["authority"]["causal_claim"] is False


def test_confounded_task_mix_does_not_form_a_matched_cell():
    rows = [
        _row("s1", "gpt-5.6-sol", "codex-cli", 0.7, task_type="bugfix"),
        _row("s2", "gpt-5.6-sol", "codex-cli", 0.5, task_type="bugfix"),
        _row("c1", "claude-opus-4-1", "claude-code", 0.3, task_type="review"),
        _row("c2", "claude-opus-4-1", "claude-code", 0.4, task_type="review"),
    ]

    report = build_model_risk_cohort_report(
        rows,
        capture_start="2026-08-23T00:00:00+00:00",
        capture_end="2026-08-30T00:00:00+00:00",
        min_cell_size=2,
    )

    assert report["comparison_readiness"]["status"] == "not_ready"
    assert report["like_for_like_warm_cells"] == []
    assert "no_like_for_like_warm_cell_meets_minimum_size" in report[
        "comparison_readiness"
    ]["reasons"]


def test_missing_harness_version_is_an_explicit_stratum_not_inferred():
    row = _row("x", "gpt-5.6-sol", "codex-cli", 0.7, version="")
    report = build_model_risk_cohort_report(
        [row],
        capture_start="2026-08-23T00:00:00+00:00",
        capture_end="2026-08-30T00:00:00+00:00",
    )

    assert report["coverage"]["attribution_status"] == {"eligible_exact": 1}
    assert report["coverage"]["harness_version_unavailable_exact_rows"] == 1
    assert report["cohorts"][0]["harness_version"] == "unavailable"


class _FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.call = None

    async def fetch(self, sql, *args):
        self.call = (sql, args)
        return self.rows


class _FakeDB:
    def __init__(self, rows):
        self.conn = _FakeConnection(rows)

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.mark.asyncio
async def test_collect_uses_explicit_prospective_window_and_limit():
    db = _FakeDB([_row("1", "gpt-5.6-sol", "codex-cli", 0.6)])
    start = datetime(2026, 8, 23, tzinfo=timezone.utc)
    end = datetime(2026, 8, 30, tzinfo=timezone.utc)

    rows = await collect_model_risk_observations(
        capture_start=start,
        capture_end=end,
        row_limit=321,
        db=db,
    )

    assert len(rows) == 1
    sql, args = db.conn.call
    assert sql == MODEL_RISK_COHORT_SQL
    assert args == (start, end, 321)


@pytest.mark.asyncio
async def test_collect_rejects_naive_capture_boundary():
    with pytest.raises(ValueError, match="timezone-aware"):
        await collect_model_risk_observations(
            capture_start=datetime(2026, 8, 23),
            capture_end=datetime(2026, 8, 30, tzinfo=timezone.utc),
            db=_FakeDB([]),
        )
