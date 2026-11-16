"""Read-only state telemetry: unavailable evidence never looks like zero."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.services.runtime_queries import _read_observation_summary
from src.mcp_handlers.middleware.envelope_step import build_experience_envelope
from tests.test_reading_coverage import _inputs
from src.eisv_telemetry import build_behavioral_derivation
from src.behavioral_sensor import compute_behavioral_sensor_eisv


def monitor(count=3):
    return SimpleNamespace(_behavioral_state=SimpleNamespace(update_count=count))


@pytest.mark.asyncio
async def test_persisted_age_and_defaulted_axes_are_from_real_evidence():
    inputs = _inputs(drift_norm=0.3)
    derivation = build_behavioral_derivation(
        **inputs, computed=compute_behavioral_sensor_eisv(**inputs))
    then = datetime.now(timezone.utc) - timedelta(seconds=120)
    row = SimpleNamespace(recorded_at=then, epistemic_class="substrate_interpretation",
                          state_json={"eisv_telemetry": {"derivation": derivation}})
    with patch("src.agent_storage.get_latest_agent_state", AsyncMock(return_value=row)):
        result = await _read_observation_summary("agent", monitor())
    assert result["behavioral_updates"] == 3
    snapshot = result["latest_persisted"]
    assert snapshot["recorded_at"] == then.isoformat()
    assert 119 <= snapshot["age_seconds"] < 125
    assert snapshot["reading_coverage"]["measured"] == ["S"]
    assert set(snapshot["reading_coverage"]["defaulted"]) == {"E", "I"}
    assert snapshot["epistemic_class"] == "substrate_interpretation"


@pytest.mark.asyncio
@pytest.mark.parametrize("row,status", [(None, "no_observation"), (RuntimeError(), "unavailable")])
async def test_empty_and_unavailable_are_distinct(row, status):
    mock = AsyncMock(side_effect=row) if isinstance(row, Exception) else AsyncMock(return_value=row)
    with patch("src.agent_storage.get_latest_agent_state", mock):
        result = await _read_observation_summary("agent", monitor(0))
    assert result == {"behavioral_updates": 0, "latest_persisted": None,
                      "persistence_status": status}


@pytest.mark.asyncio
async def test_older_row_has_unknown_coverage_not_all_measured():
    row = SimpleNamespace(recorded_at=None, state_json={}, epistemic_class="agent_report")
    with patch("src.agent_storage.get_latest_agent_state", AsyncMock(return_value=row)):
        result = await _read_observation_summary("agent", monitor())
    snapshot = result["latest_persisted"]
    assert snapshot["reading_coverage"] is None
    assert snapshot["coverage_status"] == "unavailable"
    assert snapshot["age_seconds"] is None


@pytest.mark.asyncio
async def test_bad_metadata_is_not_reported_as_storage_failure():
    row = SimpleNamespace(recorded_at="not-a-date", state_json={"eisv_telemetry": {}},
                          epistemic_class="agent_report")
    with patch("src.agent_storage.get_latest_agent_state", AsyncMock(return_value=row)), \
         patch("src.reading_coverage.build_reading_coverage", side_effect=ValueError):
        result = await _read_observation_summary("agent", monitor())
    assert result["persistence_status"] == "recorded"
    assert result["latest_persisted"]["age_seconds"] is None
    assert result["latest_persisted"]["coverage_status"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("verbosity", ["minimal", "standard", "full"])
async def test_all_read_tiers_preserve_observation_summary(verbosity):
    from tests.helpers.metrics_producer import real_metrics_payload
    with patch("src.agent_storage.get_latest_agent_state", AsyncMock(return_value=None)):
        payload, args = await real_metrics_payload({"verbosity": verbosity})
    summary = payload["observation_summary"]
    env = build_experience_envelope("check_working_state", "get_governance_metrics", payload, args)
    assert env["observation_summary"] == summary
    assert summary["behavioral_updates"] == 2
