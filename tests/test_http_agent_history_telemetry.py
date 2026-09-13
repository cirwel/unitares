"""Telemetry projections on the per-agent append-only history endpoint."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.eisv_telemetry import build_eisv_telemetry_envelope
from src.http_api import http_agent_history


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


class _DB:
    def __init__(self, rows):
        self.conn = type("Conn", (), {})()
        self.conn.fetch = AsyncMock(return_value=rows)

    def acquire(self):
        return _Acquire(self.conn)


def _envelope():
    return build_eisv_telemetry_envelope(
        metrics={"E": 0.6, "I": 0.8, "S": 0.2, "V": -0.2,
                 "primary_eisv_source": "behavioral"},
        behavioral_snapshot={
            "E": 0.6, "I": 0.8, "S": 0.2, "V": -0.2,
            "confidence": 0.8, "raw_obs": [0.7, 0.8, 0.2],
            "obs_source": "physical",
        },
        submitted_sensor={"E": 0.7, "I": 0.8, "S": 0.2, "V": -0.1},
        submitted_source="physical",
        derivation={"kind": "caller_published_sensor", "missing_inputs": []},
        policy_evaluation={"action": "proceed", "sub_action": "guide"},
        enforcement={"requested": False, "applied": False},
        measurement_id="measurement-1",
    )


def _row():
    return {
        "recorded_at": datetime(2026, 8, 9, 18, 0, tzinfo=timezone.utc),
        "e": 0.6,
        "i": 0.8,
        "s_entropy": 0.2,
        "v": -0.2,
        "coherence": 0.5,
        "risk_score": 0.1,
        "state_json": {"E": 0.6, "eisv_telemetry": _envelope()},
        "epistemic_class": "agent_report",
        "telemetry_available": True,
        "total": 1,
        "agent_report_total": 1,
        "substrate_total": 0,
        "telemetry_total": 1,
    }


def _client():
    app = Starlette(routes=[
        Route("/v1/agents/{agent_id}/history", http_agent_history, methods=["GET"]),
    ])
    return TestClient(app)


def test_history_returns_compact_source_and_actuator_summary_by_default():
    db = _DB([_row()])
    with patch("src.http_routes.access._check_http_auth", return_value=True), \
         patch("src.db.get_db", return_value=db):
        response = _client().get("/v1/agents/agent-1/history")

    assert response.status_code == 200
    point = response.json()["points"][0]
    assert point["telemetry"]["measurement_source"] == "physical"
    assert point["telemetry"]["behavioral_confidence"] == 0.8
    assert point["epistemic_class"] == "agent_report"
    assert point["telemetry_available"] is True
    assert "telemetry_envelope" not in point
    assert response.json()["telemetry_included"] is False
    assert response.json()["observation_summary"] == {
        "state_rows": 1,
        "agent_reports": 1,
        "substrate_rows": 0,
        "other_rows": 0,
        "telemetry_envelopes": 1,
    }


def test_history_can_opt_into_the_full_append_only_envelope():
    db = _DB([_row()])
    with patch("src.http_routes.access._check_http_auth", return_value=True), \
         patch("src.db.get_db", return_value=db):
        response = _client().get(
            "/v1/agents/agent-1/history?include_telemetry=true"
        )

    point = response.json()["points"][0]
    assert point["telemetry_envelope"]["schema"] == "eisv.telemetry.v1"
    assert point["telemetry_envelope"]["measurement_id"] == "measurement-1"
    assert response.json()["telemetry_included"] is True


def test_history_exposes_legacy_substrate_rows_without_calling_them_reports():
    row = _row()
    row.update({
        "state_json": {"E": 0.6, "epistemic_class": "substrate_interpretation"},
        "epistemic_class": "substrate_interpretation",
        "telemetry_available": False,
        "total": 42,
        "agent_report_total": 0,
        "substrate_total": 42,
        "telemetry_total": 0,
    })
    db = _DB([row])
    with patch("src.http_routes.access._check_http_auth", return_value=True), \
         patch("src.db.get_db", return_value=db):
        response = _client().get("/v1/agents/agent-1/history")

    payload = response.json()
    assert payload["observation_summary"] == {
        "state_rows": 42,
        "agent_reports": 0,
        "substrate_rows": 42,
        "other_rows": 0,
        "telemetry_envelopes": 0,
    }
    assert payload["points"][0]["telemetry"]["schema"] == (
        "eisv.telemetry.summary.legacy"
    )
    assert payload["points"][0]["telemetry"]["missing_inputs"] == [
        "eisv_telemetry"
    ]


def test_history_carries_the_action_and_verdict_paired_with_each_risk():
    """The governance action / verdict tier persisted alongside risk_score.

    Both live in state_json (record_agent_state writes them there), so the
    endpoint reads them from the row it already selects — no extra column,
    no join. Verified against 30d of live core.agent_state on 2026-08-27:
    39,335 non-synthetic rows, 100% carrying action + verdict + risk_score,
    vocabulary guide / approve / cirs_block / risk_pause.
    """
    row = _row()
    row["state_json"] = {
        **row["state_json"], "action": "risk_pause", "verdict": "high-risk",
    }
    db = _DB([row])
    with patch("src.http_routes.access._check_http_auth", return_value=True), \
         patch("src.db.get_db", return_value=db):
        response = _client().get("/v1/agents/agent-1/history")

    point = response.json()["points"][0]
    assert point["action"] == "risk_pause"
    assert point["verdict"] == "high-risk"
    # The risk it is paired with must still be the persisted, pre-adjustment
    # value — the trajectory-identity enrichment rewrites the response
    # envelope, never this state row.
    assert point["risk"] == 0.1


def test_history_reports_a_missing_action_as_null_not_as_approve():
    """Rows written before the action-write carry neither key.

    Defaulting those to 'approve'/'safe' would invent a clean governance
    record the data does not support, and would silently inflate any
    approve-rate a consumer computes from this endpoint.
    """
    db = _DB([_row()])  # state_json has no action/verdict
    with patch("src.http_routes.access._check_http_auth", return_value=True), \
         patch("src.db.get_db", return_value=db):
        response = _client().get("/v1/agents/agent-1/history")

    point = response.json()["points"][0]
    assert point["action"] is None
    assert point["verdict"] is None


@pytest.mark.parametrize("option,envelope_indexes", [
    ("latest", [2]), ("true", [0, 1, 2]), ("false", []),
])
def test_history_envelope_modes_preserve_points(option, envelope_indexes):
    rows = [_row() for _ in range(3)]
    for index, row in enumerate(rows):
        row["recorded_at"] += timedelta(hours=index)
        row["total"] = 3
    db = _DB(rows)
    with patch("src.http_routes.access._check_http_auth", return_value=True), \
         patch("src.db.get_db", return_value=db):
        response = _client().get(
            f"/v1/agents/agent-1/history?include_telemetry={option}"
        )
    payload = response.json()
    assert len(payload["points"]) == 3
    assert [i for i, point in enumerate(payload["points"])
            if "telemetry_envelope" in point] == envelope_indexes
    assert all("telemetry" in point for point in payload["points"])
    assert payload["telemetry_included"] is bool(envelope_indexes)
    # One query serves both trajectory statistics and every envelope mode.
    assert db.conn.fetch.await_count == 1


def test_latest_envelope_does_not_substitute_an_older_measurement():
    rows = [_row(), _row()]
    rows[-1]["state_json"] = {"E": 0.6}
    rows[-1]["telemetry_available"] = False
    rows[-1]["recorded_at"] += timedelta(hours=1)
    db = _DB(rows)
    with patch("src.http_routes.access._check_http_auth", return_value=True), \
         patch("src.db.get_db", return_value=db):
        payload = _client().get(
            "/v1/agents/agent-1/history?include_telemetry=latest"
        ).json()
    assert not any("telemetry_envelope" in point for point in payload["points"])
    assert payload["telemetry_mode"] == "latest"


def test_missing_history_has_no_invented_duration_cadence_or_slopes():
    db = _DB([])
    with patch("src.http_routes.access._check_http_auth", return_value=True), \
         patch("src.db.get_db", return_value=db):
        payload = _client().get("/v1/agents/agent-1/history?mode=all").json()
    context = payload["trajectory_context"]
    assert context["status"] == "no_observations"
    assert context["subject"] == {"kind": "agent", "agent_id": "agent-1"}
    assert context["source"] == "core.agent_state"
    assert context["policy_applied"] is False
    assert context["observations"] == context["points_returned"] == 0
    assert context["elapsed_seconds"] is None
    assert context["first_observed_at"] is context["last_observed_at"] is None
    assert context["cadence"]["gap_count"] == 0
    assert context["cadence"]["median_seconds"] is None
    assert context["slopes_per_hour"] == dict.fromkeys("EISV")


async def _query_fixture_history(fixture_rows, mode="recent", limit=3):
    """Run the endpoint's actual SQL over inline data, in a read-only transaction.

    The existing test database supplies only the PostgreSQL engine. These CTEs
    require no schema, migrations, temporary tables, or writes. Missing local
    PostgreSQL skips only these SQL integration cases; CI provides it.
    """
    asyncpg = pytest.importorskip("asyncpg")
    from tests.test_db_utils import TEST_DB_URL

    try:
        conn = await asyncpg.connect(TEST_DB_URL, timeout=2)
    except (OSError, asyncpg.PostgresError):
        pytest.skip("PostgreSQL test connection unavailable")
    try:
        db = _DB([])
        with patch("src.http_routes.access._check_http_auth", return_value=True), \
             patch("src.db.get_db", return_value=db):
            response = _client().get(
                f"/v1/agents/agent-1/history?mode={mode}&limit={limit}"
            )
        assert response.status_code == 200
        sql, *arguments = db.conn.fetch.call_args.args
        sql = sql.replace("core.identities", "fixture_identities").replace(
            "core.agent_state", "fixture_states"
        )
        fixture_ctes = """
            WITH fixture_identities AS (
                SELECT 1::bigint AS identity_id, 'agent-1'::text AS agent_id
            ), fixture_states AS (
                SELECT * FROM jsonb_to_recordset($4::jsonb) AS fixture(
                    state_id bigint, identity_id bigint, recorded_at timestamptz,
                    integrity real, entropy real, volatility real, coherence real,
                    risk_score real, state_json jsonb, epistemic_class text,
                    synthetic boolean
                )
            ),
        """
        sql = fixture_ctes + sql.strip().removeprefix("WITH ")
        async with conn.transaction(readonly=True):
            rows = await conn.fetch(sql, *arguments, json.dumps(fixture_rows))
        # The production pool decodes jsonb; raw asyncpg uses strings.
        result = []
        for row in rows:
            decoded = dict(row)
            decoded["state_json"] = json.loads(decoded["state_json"])
            result.append(decoded)
        db = _DB(result)
        with patch("src.http_routes.access._check_http_auth", return_value=True), \
             patch("src.db.get_db", return_value=db):
            return _client().get(
                f"/v1/agents/agent-1/history?mode={mode}&limit={limit}"
            ).json()
    finally:
        await conn.close()


def _fixture_rows(hours):
    start = datetime(2026, 8, 9, 0, 0, tzinfo=timezone.utc)
    rows = []
    for index, hour in enumerate(hours):
        envelope = _envelope()
        envelope["measurement"]["primary"]["source"] = (
            "ode_fallback" if index < 2 else "behavioral"
        )
        envelope["measurement"]["behavioral"]["observation_source"] = (
            "physical" if index < 5 else "behavioral_sensor"
        )
        envelope["measurement"]["behavioral"]["warmup"] = {
            "phase": "cold" if index < 4 else "baselined"
        }
        rows.append({
            "state_id": index + 1, "identity_id": 1,
            "recorded_at": (start + timedelta(hours=hour)).isoformat(),
            "integrity": hour / 40, "entropy": 0.2, "volatility": -hour / 100,
            "coherence": 0.5, "risk_score": 0.1,
            "state_json": {"E": float(index == 3), "eisv_telemetry": envelope},
            "epistemic_class": "agent_report", "synthetic": False,
        })
    return rows


@pytest.mark.asyncio
async def test_recent_scope_excludes_prior_gaps_and_regime_transitions():
    payload = await _query_fixture_history(_fixture_rows([0, 1, 2, 10, 11, 12, 13, 20]))
    context = payload["trajectory_context"]
    assert context["observations"] == context["points_returned"] == 3
    assert context["total_observations"] == 8
    assert context["elapsed_seconds"] == 8 * 3600
    assert context["requested_window"]["basis"] == "latest_observations"
    assert context["cadence"]["gap_count"] == 2
    assert context["cadence"]["median_seconds"] == 4 * 3600
    assert context["cadence"]["max_seconds"] == 7 * 3600
    assert context["transitions"]["primary_source"] == 0
    assert context["transitions"]["measurement_source"] == 0
    assert context["transitions"]["maturity_phase"] == 0
    assert context["slopes_per_hour"]["E"] == 0
    assert context["slopes_per_hour"]["I"] == pytest.approx(0.025)
    assert context["slopes_per_hour"]["V"] == pytest.approx(-0.01)


@pytest.mark.asyncio
async def test_all_scope_statistics_use_observations_before_decimation():
    payload = await _query_fixture_history(
        _fixture_rows([0, 1, 2, 10, 11, 12, 13, 20]), mode="all"
    )
    context = payload["trajectory_context"]
    assert context["observations"] == 8
    assert context["points_returned"] == 5
    assert context["sampling"] == "event_index_decimation"
    assert context["elapsed_seconds"] == 20 * 3600
    assert context["cadence"]["gap_count"] == 7
    assert context["cadence"]["median_seconds"] == 3600
    assert context["cadence"]["p95_seconds"] == pytest.approx(7.7 * 3600)
    assert context["cadence"]["max_seconds"] == 8 * 3600
    # The first source transition occurs on a point omitted by decimation.
    assert context["transitions"]["primary_source"] == 1
    assert context["transitions"]["measurement_source"] == 2
    assert context["transitions"]["maturity_phase"] == 1
    assert context["slopes_per_hour"]["E"] == pytest.approx(11 / 2751)
    assert context["slopes_per_hour"]["I"] == pytest.approx(0.025)
    assert context["slopes_per_hour"]["S"] == pytest.approx(0)
    assert context["slopes_per_hour"]["V"] == pytest.approx(-0.01)
    assert context["slope_observations"] == dict.fromkeys("EISV", 8)


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", [[0], [0, 0]])
async def test_zero_time_span_never_produces_a_slope(hours):
    payload = await _query_fixture_history(_fixture_rows(hours))
    context = payload["trajectory_context"]
    assert context["observations"] == len(hours)
    assert context["cadence"]["gap_count"] == len(hours) - 1
    assert context["slopes_per_hour"] == dict.fromkeys("EISV")
    assert context["elapsed_seconds"] == (None if len(hours) == 1 else 0)


@pytest.mark.asyncio
async def test_statistics_preserve_missing_values_and_exclude_bootstrap_rows():
    rows = _fixture_rows([0, 1, 2, 3])
    del rows[0]["state_json"]["E"]
    del rows[1]["state_json"]["eisv_telemetry"]
    rows[2]["synthetic"] = True
    payload = await _query_fixture_history(rows, mode="all")
    context = payload["trajectory_context"]
    assert context["observations"] == context["total_observations"] == 3
    assert context["cadence"]["gap_count"] == 2
    assert context["cadence"]["max_seconds"] == 2 * 3600
    assert context["transitions"]["unknown_source_observations"] == 1
    assert context["transitions"]["unknown_maturity_observations"] == 1
    assert context["slope_observations"] == {"E": 2, "I": 3, "S": 3, "V": 3}
    assert payload["points"][0]["E"] is None
