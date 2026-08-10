"""Tests for the durable EISV telemetry-health report and HTTP surface."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.eisv_telemetry import EISV_TELEMETRY_SCHEMA
from src.eisv_telemetry_health import (
    EISV_TELEMETRY_CALIBRATION_SQL,
    EISV_TELEMETRY_HEALTH_SCHEMA,
    EISV_TELEMETRY_STATE_HEALTH_SQL,
    build_calibration_summary,
    query_eisv_telemetry_health,
)
from src.http_api import (
    _eisv_telemetry_health_cache,
    http_eisv_telemetry_health,
)


def _outcome(
    *,
    risk: float | None = 0.3,
    bad: bool = False,
    measurement: str | None = "m-1",
    schema: str | None = EISV_TELEMETRY_SCHEMA,
    prior: bool = True,
    detail: dict | None = None,
    agent: str = "agent-1",
):
    return {
        "outcome_id": f"outcome-{agent}-{measurement}-{bad}",
        "ts": datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc),
        "agent_id": agent,
        "is_bad": bad,
        "detail": detail or {},
        "prior_recorded_at": (
            datetime(2026, 8, 9, 19, 50, tzinfo=timezone.utc) if prior else None
        ),
        "prior_risk": risk,
        "prior_telemetry_schema": schema,
        "prior_measurement_id": measurement,
    }


def test_calibration_is_strict_clustered_and_future_only():
    rows = [
        _outcome(measurement="shared", risk=0.31, bad=False),
        _outcome(measurement="shared", risk=0.31, bad=True),
        _outcome(measurement="high", risk=0.72, bad=True, agent="agent-2"),
        _outcome(measurement="legacy", schema=None, risk=0.2, agent="legacy"),
        _outcome(measurement="none", prior=False, risk=None, agent="no-state"),
        _outcome(
            measurement="fixture",
            risk=0.9,
            bad=True,
            agent="fixture",
            detail={"synthetic_calibration_fixture": True},
        ),
    ]

    report = build_calibration_summary(
        rows,
        min_bin_clusters=1,
        min_cohort_clusters=10,
    )

    assert report["status"] == "inconclusive"
    assert report["anchor_scope"] == "strict_external"
    assert report["fixtures_excluded"] == 1
    assert report["strict_outcomes"] == 5
    assert report["with_prior_state"] == 4
    assert report["with_envelope"] == 3
    assert report["clusters"] == 2
    assert report["bad_clusters"] == 2
    assert report["envelope_coverage_rate"] == pytest.approx(3 / 5)

    middle = next(row for row in report["bins"] if row["band"] == "0.2-0.4")
    assert middle["outcomes"] == 2
    assert middle["clusters"] == 1
    assert middle["bad_clusters"] == 1
    assert middle["bad_cluster_rate"] == 1.0


def test_calibration_keeps_boundary_and_invalid_risk_honest():
    report = build_calibration_summary([
        _outcome(measurement="max", risk=1.0, bad=True),
        _outcome(measurement="invalid", risk=-0.1, bad=False, agent="agent-2"),
    ])

    assert report["bins"][-1]["clusters"] == 1
    assert report["bins"][-1]["bad_clusters"] == 1
    assert report["invalid_risk_rows"] == 1


def test_sql_contract_is_measured_only_and_externally_anchored():
    assert "s.synthetic IS NOT TRUE" in EISV_TELEMETRY_STATE_HEALTH_SQL
    assert "eisv.telemetry.v1" in EISV_TELEMETRY_STATE_HEALTH_SQL
    assert "policy_risk_mismatch" in EISV_TELEMETRY_STATE_HEALTH_SQL
    assert "measurement_contract_missing" in EISV_TELEMETRY_STATE_HEALTH_SQL
    assert "contract_checked_rows" in EISV_TELEMETRY_STATE_HEALTH_SQL
    assert "enforcement_delivery_rate" in EISV_TELEMETRY_STATE_HEALTH_SQL
    assert "applied_without_request" in EISV_TELEMETRY_STATE_HEALTH_SQL
    assert "maturity_gate_contract_missing" in EISV_TELEMETRY_STATE_HEALTH_SQL
    assert "eisv.cold-start-confirmation.v1" in EISV_TELEMETRY_STATE_HEALTH_SQL
    assert "maturity_would_defer" in EISV_TELEMETRY_STATE_HEALTH_SQL
    assert "maturity_actuation_without_readiness" in EISV_TELEMETRY_STATE_HEALTH_SQL
    assert "enforcement_basis_distribution" in EISV_TELEMETRY_STATE_HEALTH_SQL
    assert "o.verification_source = 'external_signal'" in EISV_TELEMETRY_CALIBRATION_SQL
    assert "o.eisv_e IS NOT NULL" in EISV_TELEMETRY_CALIBRATION_SQL
    assert "s.synthetic IS NOT TRUE" in EISV_TELEMETRY_CALIBRATION_SQL


@pytest.mark.asyncio
async def test_query_combines_state_contracts_and_clustered_calibration():
    conn = AsyncMock()
    conn.fetchval.return_value = {
        "generated_at": "2026-08-09T20:00:00Z",
        "summary": {"states": 10, "envelopes": 2},
        "timeline": [],
        "contract_checks": {"checked_rows": 2, "by_type": []},
        "maturity_gate": {
            "strata": [],
            "ineligibility_reasons": [],
            "reset_reasons": [],
        },
        "enforcement": {"strata": [], "bases": []},
    }
    conn.fetch.return_value = [_outcome(measurement="m-1", risk=0.4)]

    report = await query_eisv_telemetry_health(conn, window_days=30)

    assert report["success"] is True
    assert report["schema"] == EISV_TELEMETRY_HEALTH_SCHEMA
    assert report["window_days"] == 30
    assert report["calibration"]["with_envelope"] == 1
    assert [item["surface"] for item in report["risk_vocabularies"]] == [
        "behavioral_verdict",
        "experience_summary",
        "health_status",
    ]
    conn.fetchval.assert_awaited_once_with(EISV_TELEMETRY_STATE_HEALTH_SQL, 30)
    conn.fetch.assert_awaited_once_with(EISV_TELEMETRY_CALIBRATION_SQL, 30, 5.0)


class _Acquire:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return None


class _DB:
    def acquire(self):
        return _Acquire()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)
    _eisv_telemetry_health_cache.clear()
    monkeypatch.setattr("src.db.get_db", lambda: _DB())
    app = Starlette(routes=[
        Route(
            "/v1/eisv/telemetry-health",
            http_eisv_telemetry_health,
            methods=["GET"],
        ),
    ])
    yield TestClient(app, client=("127.0.0.1", 50000))
    _eisv_telemetry_health_cache.clear()


def test_http_endpoint_clamps_window_and_caches(client, monkeypatch):
    query = AsyncMock(return_value={
        "success": True,
        "schema": EISV_TELEMETRY_HEALTH_SCHEMA,
        "window_days": 90,
        "summary": {"states": 0, "envelopes": 0},
    })
    monkeypatch.setattr(
        "src.eisv_telemetry_health.query_eisv_telemetry_health",
        query,
    )

    first = client.get("/v1/eisv/telemetry-health?days=999")
    second = client.get("/v1/eisv/telemetry-health?days=999")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["window_days"] == 90
    assert first.headers["cache-control"] == "private, max-age=30"
    query.assert_awaited_once()
    assert query.await_args.kwargs["window_days"] == 90
