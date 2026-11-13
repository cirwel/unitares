"""Tests for `GET /v1/audit/volume-summary` and `fetch_audit_volume_summary`."""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock

import asyncpg
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src.db.mixins.audit import fetch_audit_volume_summary
from src.http_routes import access
from src.http_routes.audit_volume import http_get_audit_volume_summary


def _make_app() -> Starlette:
    app = Starlette(
        routes=[
            Route(
                "/v1/audit/volume-summary",
                http_get_audit_volume_summary,
                methods=["GET"],
            )
        ]
    )
    return app


def test_audit_volume_route_strict_auth_and_floor_validation(monkeypatch):
    monkeypatch.setenv("UNITARES_REST_STRICT", "1")
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "strict-mcp-secret")
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "local-api-secret")
    monkeypatch.setattr(access, "_is_trusted_network", lambda _host: True)

    fake_query = AsyncMock(
        return_value=[
            {
                "event_type": "session_resolve_miss_observed",
                "source": "agent-storm-0000",
                "recent": 5850,
                "prior": 0,
            }
        ]
    )
    monkeypatch.setattr("src.db.mixins.audit.query_audit_volume_summary", fake_query)

    client = TestClient(_make_app())

    # Unauthenticated and local API token both fail under UNITARES_REST_STRICT=1.
    assert client.get("/v1/audit/volume-summary").status_code == 401
    assert (
        client.get(
            "/v1/audit/volume-summary",
            headers={"Authorization": "Bearer local-api-secret"},
        ).status_code
        == 401
    )

    headers = {"Authorization": "Bearer strict-mcp-secret"}

    # Invalid floor parameters return 400 without touching the DB.
    assert client.get("/v1/audit/volume-summary?floor=abc", headers=headers).status_code == 400
    assert client.get("/v1/audit/volume-summary?floor=-1", headers=headers).status_code == 400
    assert (
        client.get("/v1/audit/volume-summary?floor=10000001", headers=headers).status_code
        == 400
    )
    assert fake_query.await_count == 0

    # Valid authenticated call returns rows and coerced floor.
    resp = client.get("/v1/audit/volume-summary?floor=450", headers=headers)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload == {
        "success": True,
        "floor": 450,
        "rows": [
            {
                "event_type": "session_resolve_miss_observed",
                "source": "agent-storm-0000",
                "recent": 5850,
                "prior": 0,
            }
        ],
    }
    fake_query.assert_awaited_once_with(floor=450)

    # DB errors return a sanitized 500 error without leaking internal exception text.
    fake_query.side_effect = RuntimeError("secret-dsn-password-in-error")
    err_resp = client.get("/v1/audit/volume-summary?floor=300", headers=headers)
    assert err_resp.status_code == 500
    assert err_resp.json() == {
        "success": False,
        "error": "Failed to query audit volume summary",
    }
    assert "secret-dsn-password-in-error" not in err_resp.text


@pytest.mark.asyncio
async def test_fetch_audit_volume_summary_sql_parity_with_elixir():
    """Mirrors `UnitaresSentinel.AuditVolumeWatchTest` (`1_875` recent / `5_000` prior)."""
    dsn = (
        os.getenv("TEST_DB_URL")
        or os.getenv("POSTGRES_TEST_URL")
        or "postgresql://postgres:postgres@localhost:5432/governance_test"
    )
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as exc:
        pytest.skip(f"governance_test database unavailable: {exc}")

    key = f"py-volume-watch-test-{uuid.uuid4().hex[:12]}"
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(
                """
                INSERT INTO audit.events (ts, agent_id, session_id, event_type, payload)
                SELECT now(), NULL, $1, 'volume_watch_probe',
                       jsonb_build_object('suppressed_since_last', 99)
                FROM generate_series(1, 11)
                """,
                key,
            )
            await conn.execute(
                """
                INSERT INTO audit.events (ts, agent_id, session_id, event_type, payload)
                VALUES
                  (now(), NULL, $1, 'volume_watch_probe',
                   jsonb_build_object('suppressed_since_last', 5000,
                                      'resolution_source', 'throttle_flush',
                                      'suppressed_last_at',
                                      to_char(now() - interval '3 hours', 'YYYY-MM-DD"T"HH24:MI:SS'))),
                  (now(), NULL, $1, 'volume_watch_probe',
                   jsonb_build_object('suppressed_since_last', 700,
                                      'resolution_source', 'throttle_flush',
                                      'suppressed_last_at',
                                      to_char(now() - interval '10 minutes', 'YYYY-MM-DD"T"HH24:MI:SS'))),
                  (now(), NULL, $1, 'volume_watch_probe',
                   jsonb_build_object('suppressed_since_last', 50,
                                      'resolution_source', 'throttle_flush',
                                      'suppressed_last_at', '2026-13-45T99:00:00')),
                  (now(), NULL, $1, 'volume_watch_probe',
                   jsonb_build_object('suppressed_since_last', 25,
                                      'resolution_source', 'throttle_flush',
                                      'suppressed_last_at',
                                      to_char((now() - interval '10 minutes') AT TIME ZONE 'UTC',
                                              'YYYY-MM-DD"T"HH24:MI:SS"+00:00"')))
                """,
                key,
            )

            rows = await fetch_audit_volume_summary(conn, floor=1000)
            match = next((r for r in rows if r["source"] == key), None)
            assert match is not None
            assert isinstance(match["recent"], int)
            assert isinstance(match["prior"], int)
            assert match["recent"] == 1875
            assert match["prior"] == 5000
        finally:
            await tx.rollback()
    finally:
        await conn.close()
