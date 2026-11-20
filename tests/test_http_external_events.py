"""Tests for POST /v1/external/events — GitHub Outcome Observer Stage 1 Part 2.

Covers:
- allowlist parity with agents.github_observer.events.EVENT_TYPES
- operator-credential gate (X-Unitares-Operator / passkey session + CSRF)
- 64 KiB body cap, JSON & field validation
- §8.5 pr_author trust enforcement (OWNER/MEMBER/COLLABORATOR + configured bots)
- unknown agent_uuid refusal (404)
- atomic persistence + broadcast_event(..., persist=False) + duplicate 409
- AuditMixin.record_external_event partition creation/fallback and rollback safety
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from agents.github_observer import events as observer_events
from src.db.mixins.audit import AuditMixin
from src.http_api import http_record_external_event
from src.http_routes.external_events import (
    EXTERNAL_GITHUB_EVENT_TYPES,
    configured_trusted_github_bot_ids,
)

OP_TOKEN = "test-operator-token"
API_TOKEN = "test-http-api-token"
AGENT_UUID = "3a7f2c91-5b64-4e08-9d13-8c2f6a4e7b50"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UNITARES_OPERATOR_TOKENS", OP_TOKEN)
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", API_TOKEN)
    monkeypatch.delenv("UNITARES_EXTERNAL_TRUSTED_GITHUB_APPS", raising=False)
    app = Starlette(
        routes=[Route("/v1/external/events", http_record_external_event, methods=["POST"])]
    )
    return TestClient(app)


def _op_headers():
    return {"X-Unitares-Operator": OP_TOKEN}


def _body(**overrides):
    body = {
        "type": "github.ci_result",
        "agent_uuid": AGENT_UUID,
        "fingerprint": "100#42:ci:700:1",
        "repo": "example/repo",
        "pr": 42,
        "observed_at": "2026-11-19T12:15:00Z",
        "pr_author": {"association": "OWNER", "id": 1, "type": "User"},
        "head_sha": "abc1234",
        "source": "actions",
        "workflow": "CI",
        "run_id": 700,
        "attempt": 1,
        "conclusion": "failure",
        "failed_jobs": ["smoke"],
    }
    body.update(overrides)
    return body


def _db_with_identity(found=True):
    return SimpleNamespace(
        get_identity=AsyncMock(return_value=SimpleNamespace(agent_id=AGENT_UUID) if found else None)
    )


def test_external_event_types_match_github_observer():
    assert EXTERNAL_GITHUB_EVENT_TYPES == observer_events.EVENT_TYPES


class TestAuthGate:
    def test_missing_credentials_is_403(self, client):
        r = client.post("/v1/external/events", json=_body())
        assert r.status_code == 403
        assert "operator" in r.json()["error"].lower()

    def test_plain_bearer_token_without_operator_header_is_403(self, client):
        r = client.post(
            "/v1/external/events",
            json=_body(),
            headers={"Authorization": f"Bearer {API_TOKEN}"},
        )
        assert r.status_code == 403

    def test_wrong_operator_token_is_403(self, client):
        r = client.post(
            "/v1/external/events",
            json=_body(),
            headers={"X-Unitares-Operator": "wrong-token"},
        )
        assert r.status_code == 403

    def test_dashboard_session_write_authorized_passes_gate(self, client):
        db = _db_with_identity(True)
        with (
            patch("src.http_routes.external_events.dashboard_session_write_authorized", return_value=True),
            patch("src.db.get_db", return_value=db),
            patch(
                "src.audit_db.record_external_event_async",
                AsyncMock(return_value=(True, "11111111-1111-4111-8111-111111111111", datetime.now(timezone.utc))),
            ),
            patch("src.http_routes.external_events.broadcaster_instance.broadcast_event", AsyncMock()),
        ):
            r = client.post("/v1/external/events", json=_body())
        assert r.status_code == 201


class TestValidation:
    def test_payload_over_64_kib_is_413(self, client):
        huge = b'{"x":"' + (b"a" * 65537) + b'"}'
        r = client.post(
            "/v1/external/events",
            content=huge,
            headers={**_op_headers(), "Content-Type": "application/json"},
        )
        assert r.status_code == 413

    def test_invalid_json_is_400(self, client):
        r = client.post(
            "/v1/external/events",
            content=b"{not-json",
            headers={**_op_headers(), "Content-Type": "application/json"},
        )
        assert r.status_code == 400

    def test_non_object_json_is_400(self, client):
        r = client.post("/v1/external/events", json=["not", "an", "object"], headers=_op_headers())
        assert r.status_code == 400

    @pytest.mark.parametrize(
        "bad_type",
        ["github.unknown", "test_passed", "lifecycle_paused", "", None],
    )
    def test_disallowed_type_is_400(self, client, bad_type):
        r = client.post("/v1/external/events", json=_body(type=bad_type), headers=_op_headers())
        assert r.status_code == 400

    @pytest.mark.parametrize(
        "bad_uuid",
        ["", "not-a-uuid", "3a7f2c915b644e089d138c2f6a4e7b50", 123, None],
    )
    def test_invalid_agent_uuid_is_400(self, client, bad_uuid):
        r = client.post("/v1/external/events", json=_body(agent_uuid=bad_uuid), headers=_op_headers())
        assert r.status_code == 400

    @pytest.mark.parametrize(
        "bad_fp",
        ["", "has space", "has\nnewline", "x" * 257, None],
    )
    def test_invalid_fingerprint_is_400(self, client, bad_fp):
        r = client.post("/v1/external/events", json=_body(fingerprint=bad_fp), headers=_op_headers())
        assert r.status_code == 400

    @pytest.mark.parametrize("bad_repo", ["", "noslash", "a/b/c", 42])
    def test_invalid_repo_is_400(self, client, bad_repo):
        r = client.post("/v1/external/events", json=_body(repo=bad_repo), headers=_op_headers())
        assert r.status_code == 400

    @pytest.mark.parametrize("bad_pr", [0, -5, "42", True, None])
    def test_invalid_pr_is_400(self, client, bad_pr):
        r = client.post("/v1/external/events", json=_body(pr=bad_pr), headers=_op_headers())
        assert r.status_code == 400

    @pytest.mark.parametrize("bad_ts", ["2026-11-19T12:00:00", "not-iso", "", 12345])
    def test_invalid_observed_at_is_400(self, client, bad_ts):
        r = client.post("/v1/external/events", json=_body(observed_at=bad_ts), headers=_op_headers())
        assert r.status_code == 400

    @pytest.mark.parametrize("bad_producer", ["", "   ", "p" * 129, 99])
    def test_invalid_producer_is_400(self, client, bad_producer):
        r = client.post("/v1/external/events", json=_body(producer=bad_producer), headers=_op_headers())
        assert r.status_code == 400

    def test_missing_or_non_dict_pr_author_is_400(self, client):
        r = client.post("/v1/external/events", json=_body(pr_author="owner"), headers=_op_headers())
        assert r.status_code == 400
        body = _body()
        del body["pr_author"]
        r2 = client.post("/v1/external/events", json=body, headers=_op_headers())
        assert r2.status_code == 400


class TestAuthorTrustAndDelivery:
    @pytest.mark.parametrize(
        "untrusted_author",
        [
            {"association": "NONE", "id": 999, "type": "User"},
            {"association": "CONTRIBUTOR", "id": 12345, "type": "Bot"},
            {"association": "CONTRIBUTOR", "id": 12345, "type": "User"},
            {},
        ],
    )
    def test_untrusted_pr_author_is_403_before_db_lookup(self, client, untrusted_author):
        with patch("src.db.get_db") as get_db:
            r = client.post(
                "/v1/external/events",
                json=_body(pr_author=untrusted_author),
                headers=_op_headers(),
            )
        assert r.status_code == 403
        assert "untrusted" in r.json()["error"]
        get_db.assert_not_called()

    def test_trusted_bot_from_env_is_admitted(self, client, monkeypatch):
        monkeypatch.setenv("UNITARES_EXTERNAL_TRUSTED_GITHUB_APPS", "1001:98765, 2002:11111, bad")
        assert configured_trusted_github_bot_ids() == frozenset({98765, 11111})
        # User with bot id must NOT be admitted as Bot.
        r_user = client.post(
            "/v1/external/events",
            json=_body(pr_author={"association": "CONTRIBUTOR", "id": 98765, "type": "User"}),
            headers=_op_headers(),
        )
        assert r_user.status_code == 403

        db = _db_with_identity(True)
        with (
            patch("src.db.get_db", return_value=db),
            patch(
                "src.audit_db.record_external_event_async",
                AsyncMock(return_value=(True, "22222222-2222-4222-8222-222222222222", datetime(2026, 11, 19, 12, 15, tzinfo=timezone.utc))),
            ),
            patch("src.http_routes.external_events.broadcaster_instance.broadcast_event", AsyncMock()),
        ):
            r_bot = client.post(
                "/v1/external/events",
                json=_body(pr_author={"association": "CONTRIBUTOR", "id": 98765, "type": "Bot"}),
                headers=_op_headers(),
            )
        assert r_bot.status_code == 201

    def test_unknown_agent_uuid_is_404(self, client):
        db = _db_with_identity(False)
        with (
            patch("src.db.get_db", return_value=db),
            patch("src.audit_db.record_external_event_async", AsyncMock()) as write_mock,
            patch("src.http_routes.external_events.broadcaster_instance.broadcast_event", AsyncMock()) as bc_mock,
        ):
            r = client.post("/v1/external/events", json=_body(), headers=_op_headers())
        assert r.status_code == 404
        assert r.json()["error"] == "unknown agent_uuid"
        write_mock.assert_not_awaited()
        bc_mock.assert_not_awaited()

    def test_valid_event_writes_audit_and_broadcasts_with_persist_false(self, client):
        db = _db_with_identity(True)
        event_id = "33333333-3333-4333-8333-333333333333"
        stored_ts = datetime(2026, 11, 19, 12, 15, tzinfo=timezone.utc)
        write_mock = AsyncMock(return_value=(True, event_id, stored_ts))
        bc_mock = AsyncMock()

        with (
            patch("src.db.get_db", return_value=db),
            patch("src.audit_db.record_external_event_async", write_mock),
            patch("src.http_routes.external_events.broadcaster_instance.broadcast_event", bc_mock),
        ):
            r = client.post(
                "/v1/external/events",
                json=_body(pr_author={"association": "MEMBER", "id": 1, "type": "User", "extra": "drop"}),
                headers=_op_headers(),
            )
        assert r.status_code == 201
        data = r.json()
        assert data == {
            "success": True,
            "deduped": False,
            "event_id": event_id,
            "event_type": "github.ci_result",
            "agent_uuid": AGENT_UUID,
            "fingerprint": "100#42:ci:700:1",
            "ts": stored_ts.isoformat(),
        }
        write_kw = write_mock.await_args.kwargs
        assert write_kw["payload"]["pr_author"] == {"association": "MEMBER", "id": 1, "type": "User"}
        assert bc_mock.await_args.kwargs["persist"] is False
        assert bc_mock.await_args.args == ("github.ci_result",)
        assert bc_mock.await_args.kwargs["agent_id"] == AGENT_UUID
        assert bc_mock.await_args.kwargs["payload"]["event_id"] == event_id
        assert bc_mock.await_args.kwargs["payload"]["fingerprint"] == "100#42:ci:700:1"

    def test_duplicate_fingerprint_is_409_without_broadcast(self, client):
        db = _db_with_identity(True)
        write_mock = AsyncMock(return_value=(False, None, datetime.now(timezone.utc)))
        bc_mock = AsyncMock()
        with (
            patch("src.db.get_db", return_value=db),
            patch("src.audit_db.record_external_event_async", write_mock),
            patch("src.http_routes.external_events.broadcaster_instance.broadcast_event", bc_mock),
        ):
            r = client.post("/v1/external/events", json=_body(), headers=_op_headers())
        assert r.status_code == 409
        assert r.json()["deduped"] is True
        bc_mock.assert_not_awaited()


class _TxCtx:
    def __init__(self, log, label):
        self._log = log
        self._label = label

    async def __aenter__(self):
        self._log.append(f"enter:{self._label}")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._log.append(f"rollback:{self._label}" if exc_type else f"commit:{self._label}")
        return False


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _DummyAudit(AuditMixin):
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


@pytest.mark.asyncio
async def test_record_external_event_partition_retry_and_rollback():
    tx_log: list[str] = []
    calls: list[tuple[str, tuple]] = []
    insert_attempts = 0

    async def fetchval(sql, *args):
        calls.append((sql, args))
        if "external_event_fingerprints" in sql:
            return args[0]
        if "create_events_partition" in sql:
            return "Created partition"
        return None

    async def execute(sql, *args):
        nonlocal insert_attempts
        calls.append((sql, args))
        if "INSERT INTO audit.events" in sql:
            insert_attempts += 1
            if insert_attempts == 1:
                raise asyncpg.exceptions.CheckViolationError(
                    'no partition of relation "events" found for row'
                )
        return "OK"

    tx_idx = 0

    def transaction():
        nonlocal tx_idx
        tx_idx += 1
        return _TxCtx(tx_log, f"tx{tx_idx}")

    conn = SimpleNamespace(fetchval=fetchval, execute=execute, transaction=transaction)
    audit = _DummyAudit(conn)
    ts = datetime.now(timezone.utc) - timedelta(days=5)
    inserted, event_id, stored_ts = await audit.record_external_event(
        fingerprint="100#1:opened",
        event_type="github.pr_opened",
        agent_id=AGENT_UUID,
        ts=ts,
        producer="github_observer",
        payload={"fingerprint": "100#1:opened"},
    )
    assert inserted is True and event_id is not None and stored_ts == ts
    assert insert_attempts == 2
    assert any("create_events_partition" in s for s, _ in calls)
    assert "commit:tx1" in tx_log


@pytest.mark.asyncio
async def test_record_external_event_out_of_window_clamped_and_error_rolls_back():
    tx_log: list[str] = []

    async def fetchval(sql, *args):
        return args[0]

    async def execute(sql, *args):
        raise RuntimeError("disk full")

    conn = SimpleNamespace(
        fetchval=fetchval,
        execute=execute,
        transaction=lambda: _TxCtx(tx_log, "outer"),
    )
    audit = _DummyAudit(conn)
    old_ts = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(RuntimeError, match="disk full"):
        await audit.record_external_event(
            fingerprint="100#2:opened",
            event_type="github.pr_opened",
            agent_id=AGENT_UUID,
            ts=old_ts,
            producer="github_observer",
            payload={"fingerprint": "100#2:opened"},
        )
    # Outer transaction rolled back so fingerprint is not burned on failure.
    assert "rollback:outer" in tx_log


@pytest.mark.asyncio
async def test_live_postgres_external_events_end_to_end(live_postgres_backend, monkeypatch):
    import uuid as _uuid

    monkeypatch.setenv("UNITARES_OPERATOR_TOKENS", OP_TOKEN)
    monkeypatch.delenv("UNITARES_EXTERNAL_TRUSTED_GITHUB_APPS", raising=False)
    be = live_postgres_backend
    unique_fp = f"100#42:ci:{_uuid.uuid4().hex}:1"

    async with be.acquire() as conn:
        await conn.execute(
            f"INSERT INTO core.agents (id, api_key) VALUES ('{AGENT_UUID}', 'k1') ON CONFLICT DO NOTHING;"
        )
        await conn.execute(
            f"INSERT INTO core.identities (agent_id, api_key_hash, metadata) "
            f"VALUES ('{AGENT_UUID}', 'h1', '{{}}'::jsonb) ON CONFLICT DO NOTHING;"
        )

    app = Starlette(
        routes=[Route("/v1/external/events", http_record_external_event, methods=["POST"])]
    )
    test_client = TestClient(app)
    payload = _body(fingerprint=unique_fp)

    with (
        patch("src.db.get_db", return_value=be),
        patch("src.http_routes.external_events.broadcaster_instance.broadcast_event", AsyncMock()),
    ):
        r1 = test_client.post("/v1/external/events", json=payload, headers=_op_headers())
        r2 = test_client.post("/v1/external/events", json=payload, headers=_op_headers())

    assert r1.status_code == 201, r1.text
    assert r2.status_code == 409, r2.text

    async with be.acquire() as conn:
        fp_rows = await conn.fetch(
            "SELECT fingerprint, event_id FROM audit.external_event_fingerprints WHERE fingerprint = $1",
            unique_fp,
        )
        audit_rows = await conn.fetch(
            "SELECT event_type, agent_id, payload FROM audit.events WHERE payload->>'fingerprint' = $1",
            unique_fp,
        )
        outcome_count = await conn.fetchval(
            "SELECT count(*) FROM audit.outcome_events WHERE agent_id = $1",
            AGENT_UUID,
        )

    assert len(fp_rows) == 1
    assert len(audit_rows) == 1
    assert audit_rows[0]["event_type"] == "github.ci_result"
    assert outcome_count == 0


def test_http_external_events_rejects_stale_or_future_observed_at_with_422(monkeypatch):
    from datetime import timedelta

    monkeypatch.setenv("UNITARES_OPERATOR_TOKENS", OP_TOKEN)
    monkeypatch.delenv("UNITARES_EXTERNAL_TRUSTED_GITHUB_APPS", raising=False)
    app = Starlette(
        routes=[Route("/v1/external/events", http_record_external_event, methods=["POST"])]
    )
    client = TestClient(app)

    stale_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    r_old = client.post("/v1/external/events", json=_body(observed_at=stale_ts), headers=_op_headers())
    assert r_old.status_code == 422
    assert "retention window" in r_old.json()["error"]

    future_ts = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    r_future = client.post("/v1/external/events", json=_body(observed_at=future_ts), headers=_op_headers())
    assert r_future.status_code == 422


@pytest.mark.asyncio
async def test_record_external_event_partition_fallback_marks_adjusted_timestamp():
    import asyncpg

    tx_log: list[str] = []
    inserted_payloads: list[tuple[datetime, dict]] = []

    async def fetchval(sql, *args):
        if "create_events_partition" in sql:
            raise RuntimeError("partition helper unavailable")
        return args[0]

    attempts = 0

    async def execute_with_fallback(sql, *args):
        nonlocal attempts
        if "INSERT INTO audit.events" in sql:
            attempts += 1
            if attempts == 1:
                raise asyncpg.exceptions.CheckViolationError("no partition")
            inserted_payloads.append((args[0], json.loads(args[6])))

    conn = SimpleNamespace(
        fetchval=fetchval,
        execute=execute_with_fallback,
        transaction=lambda: _TxCtx(tx_log, "tx"),
    )
    audit = _DummyAudit(conn)
    ts = datetime(2026, 11, 1, tzinfo=timezone.utc)
    caller_payload = {"fingerprint": "100#3:opened"}

    inserted, _event_id, stored_ts = await audit.record_external_event(
        fingerprint="100#3:opened",
        event_type="github.pr_opened",
        agent_id=AGENT_UUID,
        ts=ts,
        producer="github_observer",
        payload=caller_payload,
    )
    assert inserted is True
    assert stored_ts > ts
    assert attempts == 2
    assert len(inserted_payloads) == 1
    assert inserted_payloads[0][1]["timestamp_adjusted"] is True
    assert inserted_payloads[0][1]["original_observed_at"] == ts.isoformat()
    assert "timestamp_adjusted" not in caller_payload
