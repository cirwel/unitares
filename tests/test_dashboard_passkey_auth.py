"""Server-side dashboard passkey/session security contract."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.responses import JSONResponse

from src import dashboard_auth, http_api


class _Request:
    def __init__(
        self,
        *,
        headers=None,
        cookies=None,
        query=None,
        method="POST",
        body=None,
        path_params=None,
        ip="8.8.8.8",
        session=None,
    ):
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.cookies = dict(cookies or {})
        self.query_params = dict(query or {})
        self.method = method
        self.path_params = dict(path_params or {})
        self.client = SimpleNamespace(host=ip)
        self.state = SimpleNamespace()
        if session is not None:
            self.state.dashboard_session = session
        self._body = {} if body is None else body

    async def json(self):
        return self._body


class _Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _DB:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Context(self.conn)


def _body(response):
    return json.loads(response.body)


def _live_session(**overrides):
    session = {
        "session_hash": b"s" * 32,
        "credential_id": b"c" * 32,
        "operator_label": "operator",
        "created_at": datetime.now(timezone.utc),
    }
    session.update(overrides)
    return session


def test_production_rp_and_origin_are_exact_host_pair():
    assert dashboard_auth.DASHBOARD_RP_ID == "gov.cirwel.org"
    assert dashboard_auth.DASHBOARD_EXPECTED_ORIGIN == "https://gov.cirwel.org"


def test_session_lifetimes_pin_sliding_and_hard_caps():
    assert dashboard_auth.SESSION_SLIDING_SECONDS == 30 * 24 * 60 * 60
    assert dashboard_auth.SESSION_HARD_SECONDS == 90 * 24 * 60 * 60


def test_enrollment_code_is_header_only_and_never_read_from_query():
    assert dashboard_auth._enroll_code_from_request(
        _Request(query={"code": "URL-LEAK"})
    ) == ""
    assert dashboard_auth._enroll_code_from_request(
        _Request(headers={dashboard_auth.ENROLL_CODE_HEADER: "abcde-fghij"})
    ) == "ABCDEFGHIJ"


@pytest.mark.asyncio
async def test_authentication_options_are_usernameless_uv_required_and_secure(monkeypatch):
    monkeypatch.setattr(dashboard_auth, "_active_credential_count", AsyncMock(return_value=1))
    store = AsyncMock()
    monkeypatch.setattr(dashboard_auth, "_store_challenge", store)
    dashboard_auth._options_rate_buckets.clear()

    response = await dashboard_auth.http_webauthn_options(_Request())

    assert response.status_code == 200
    payload = _body(response)
    assert payload["rpId"] == "gov.cirwel.org"
    assert payload["allowCredentials"] == []
    assert payload["userVerification"] == "required"
    assert len(store.await_args.args[1]) == 32
    assert store.await_args.args[2] == "authenticate"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{dashboard_auth.PREAUTH_COOKIE}=")
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "Path=/" in cookie
    assert "SameSite=lax" in cookie


@pytest.mark.asyncio
async def test_authentication_options_are_rate_limited_per_ip(monkeypatch):
    monkeypatch.setattr(dashboard_auth, "_active_credential_count", AsyncMock(return_value=1))
    monkeypatch.setattr(dashboard_auth, "_store_challenge", AsyncMock())
    dashboard_auth._options_rate_buckets.clear()
    request = _Request(ip="203.0.113.7")
    for _ in range(dashboard_auth._OPTIONS_RATE_LIMIT):
        assert (await dashboard_auth.http_webauthn_options(request)).status_code == 200
    assert (await dashboard_auth.http_webauthn_options(request)).status_code == 429


@pytest.mark.asyncio
async def test_zero_credential_deploy_is_inert(monkeypatch):
    monkeypatch.setattr(dashboard_auth, "_active_credential_count", AsyncMock(return_value=0))
    dashboard_auth._options_rate_buckets.clear()
    signin = await dashboard_auth.http_auth_signin(_Request(method="GET"))
    options = await dashboard_auth.http_webauthn_options(_Request())
    assert signin.status_code == 303
    assert signin.headers["location"] == "/"
    assert options.status_code == 409


@pytest.mark.asyncio
async def test_registration_options_require_discoverable_uv_credential(monkeypatch):
    class Conn:
        async def fetch(self, _sql):
            return []

    monkeypatch.setattr(
        dashboard_auth,
        "_registration_authorized",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr(dashboard_auth, "_store_challenge", AsyncMock())
    monkeypatch.setattr(dashboard_auth, "get_db", lambda: _DB(Conn()))
    response = await dashboard_auth.http_webauthn_register_options(_Request())
    assert response.status_code == 200
    payload = _body(response)
    selection = payload["authenticatorSelection"]
    assert selection["residentKey"] == "required"
    assert selection["requireResidentKey"] is True
    assert selection["userVerification"] == "required"
    assert payload["attestation"] == "none"
    assert len(dashboard_auth._store_challenge.await_args.args[1]) == 32
    assert dashboard_auth._store_challenge.await_args.args[2] == "register"


@pytest.mark.asyncio
async def test_assertion_verify_creates_hashed_session_and_audits(monkeypatch):
    credential_id = b"credential-id"
    executed = []

    class Conn:
        def transaction(self):
            return _Context(self)

        async def fetchrow(self, sql, value):
            assert "core.webauthn_credentials" in sql
            assert value == credential_id
            return {
                "credential_id": credential_id,
                "public_key": b"public-key",
                "user_handle": b"user-handle",
                "sign_count": 0,
                "operator_label": "operator",
            }

        async def execute(self, sql, *args):
            executed.append((sql, args))
            return "INSERT 0 1"

    captured = {}

    def verify(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(new_sign_count=0)

    monkeypatch.setattr(
        dashboard_auth,
        "_webauthn_imports",
        lambda: {
            "parse_authentication_credential_json": lambda _body: SimpleNamespace(
                raw_id=credential_id,
                response=SimpleNamespace(user_handle=b"user-handle"),
            ),
            "verify_authentication_response": verify,
        },
    )
    monkeypatch.setattr(
        dashboard_auth, "_consume_challenge", AsyncMock(return_value=b"x" * 32)
    )
    monkeypatch.setattr(dashboard_auth, "get_db", lambda: _DB(Conn()))
    audit = AsyncMock()
    monkeypatch.setattr(dashboard_auth, "_emit_auth_event", audit)

    response = await dashboard_auth.http_webauthn_verify(
        _Request(body={"credential": {"id": "encoded"}})
    )
    assert response.status_code == 200
    assert captured["expected_rp_id"] == "gov.cirwel.org"
    assert captured["expected_origin"] == "https://gov.cirwel.org"
    assert captured["require_user_verification"] is True
    sql = "\n".join(item[0] for item in executed)
    assert "CASE WHEN $2 > 0 THEN $2 ELSE sign_count END" in sql
    assert "INSERT INTO core.dashboard_sessions" in sql
    assert "interval '30 days'" in sql
    assert "interval '90 days'" in sql
    session_insert = next(item for item in executed if "dashboard_sessions" in item[0])
    assert isinstance(session_insert[1][0], bytes) and len(session_insert[1][0]) == 32
    assert dashboard_auth.SESSION_COOKIE in response.headers["set-cookie"]
    audit.assert_awaited_once()
    assert audit.await_args.args[:2] == ("dashboard_signin", "operator")


@pytest.mark.asyncio
async def test_discoverable_assertion_must_match_stored_user_handle(monkeypatch):
    class Conn:
        async def fetchrow(self, _sql, _credential_id):
            return {
                "credential_id": b"credential-id",
                "public_key": b"public-key",
                "user_handle": b"expected-user",
                "sign_count": 0,
                "operator_label": "operator",
            }

    def should_not_verify(**_kwargs):
        pytest.fail("cryptographic verification must not run for another user handle")

    monkeypatch.setattr(
        dashboard_auth,
        "_webauthn_imports",
        lambda: {
            "parse_authentication_credential_json": lambda _body: SimpleNamespace(
                raw_id=b"credential-id",
                response=SimpleNamespace(user_handle=b"different-user"),
            ),
            "verify_authentication_response": should_not_verify,
        },
    )
    monkeypatch.setattr(
        dashboard_auth, "_consume_challenge", AsyncMock(return_value=b"x" * 32)
    )
    monkeypatch.setattr(dashboard_auth, "get_db", lambda: _DB(Conn()))
    response = await dashboard_auth.http_webauthn_verify(
        _Request(body={"credential": {"id": "encoded"}})
    )
    assert response.status_code == 401
    assert "user handle" in _body(response)["error"]


@pytest.mark.asyncio
async def test_registration_verify_consumes_code_persists_credential_and_notifies(monkeypatch):
    credential_id = b"new-credential"
    executed = []

    class Conn:
        def transaction(self):
            return _Context(self)

        async def fetchval(self, sql, *_args):
            assert "webauthn_enroll_codes" in sql
            return True

        async def execute(self, sql, *args):
            executed.append((sql, args))
            return "INSERT 0 1"

    parsed = SimpleNamespace(response=SimpleNamespace(transports=["internal"]))
    verified = SimpleNamespace(
        credential_id=credential_id,
        credential_public_key=b"public-key",
        sign_count=0,
    )
    captured = {}

    def verify(**kwargs):
        captured.update(kwargs)
        return verified

    monkeypatch.setattr(
        dashboard_auth,
        "_registration_authorized",
        AsyncMock(return_value=(True, "ENROLLCODE")),
    )
    monkeypatch.setattr(
        dashboard_auth, "_consume_challenge", AsyncMock(return_value=b"x" * 32)
    )
    monkeypatch.setattr(
        dashboard_auth,
        "_webauthn_imports",
        lambda: {
            "parse_registration_credential_json": lambda _body: parsed,
            "verify_registration_response": verify,
        },
    )
    monkeypatch.setattr(dashboard_auth, "get_db", lambda: _DB(Conn()))
    audit = AsyncMock()
    monkeypatch.setattr(dashboard_auth, "_emit_auth_event", audit)

    response = await dashboard_auth.http_webauthn_register_verify(
        _Request(body={"credential": {"id": "encoded"}, "nickname": "Phone"})
    )
    assert response.status_code == 200
    assert captured["expected_rp_id"] == "gov.cirwel.org"
    assert captured["expected_origin"] == "https://gov.cirwel.org"
    assert captured["require_user_verification"] is True
    assert any("INSERT INTO core.webauthn_credentials" in sql for sql, _ in executed)
    audit.assert_awaited_once()
    assert audit.await_args.args[:2] == ("webauthn_enrolled", "operator")
    assert audit.await_args.kwargs["notification_required"] is True
    assert audit.await_args.kwargs["severity"] == "critical"


def test_session_cookie_flags_are_exact():
    response = JSONResponse({"ok": True})
    dashboard_auth._set_session_cookie(response, "opaque")
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{dashboard_auth.SESSION_COOKIE}=opaque;")
    for attribute in (
        "HttpOnly",
        f"Max-Age={30 * 24 * 60 * 60}",
        "Path=/",
        "SameSite=lax",
        "Secure",
    ):
        assert attribute in cookie
    assert "Domain=" not in cookie


@pytest.mark.asyncio
async def test_session_validation_joins_live_credential_and_slides_to_hard_cap(monkeypatch):
    now = datetime.now(timezone.utc)

    class Conn:
        def __init__(self):
            self.executed = []

        async def fetchrow(self, sql, session_hash):
            assert "JOIN core.webauthn_credentials" in sql
            assert "c.revoked_at IS NULL" in sql
            assert "hard_expires_at > now()" in sql
            return {
                "session_hash": session_hash,
                "credential_id": b"c" * 32,
                "operator_label": "operator",
                "created_at": now,
                "expires_at": now + timedelta(days=1),
                "hard_expires_at": now + timedelta(days=60),
                "last_seen_at": now - timedelta(minutes=10),
                "user_agent": "test",
            }

        async def execute(self, sql, *args):
            self.executed.append((sql, args))
            return "UPDATE 1"

    conn = Conn()
    monkeypatch.setattr(dashboard_auth, "get_db", lambda: _DB(conn))
    session = await dashboard_auth.load_dashboard_session(
        _Request(cookies={dashboard_auth.SESSION_COOKIE: "opaque"})
    )
    assert session["operator_label"] == "operator"
    update_sql = conn.executed[0][0]
    assert "LEAST(hard_expires_at" in update_sql
    assert "interval '30 days'" in update_sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ceremony,expires_delta",
    [("register", 30), ("authenticate", -1)],
)
async def test_challenge_mismatch_or_expiry_is_consumed_and_rejected(
    monkeypatch, ceremony, expires_delta
):
    row = {
        "challenge": b"x" * 32,
        "ceremony": ceremony,
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=expires_delta),
    }

    class Conn:
        async def fetchrow(self, sql, *_args):
            assert sql.lstrip().startswith("DELETE FROM core.webauthn_challenges")
            nonlocal row
            result, row = row, None
            return result

    monkeypatch.setattr(dashboard_auth, "get_db", lambda: _DB(Conn()))
    request = _Request(cookies={dashboard_auth.PREAUTH_COOKIE: "preauth"})
    assert await dashboard_auth._consume_challenge(request, "authenticate") is None
    assert await dashboard_auth._consume_challenge(request, "authenticate") is None


@pytest.mark.asyncio
async def test_challenge_is_single_use(monkeypatch):
    row = {
        "challenge": b"x" * 32,
        "ceremony": "authenticate",
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=30),
    }

    class Conn:
        async def fetchrow(self, _sql, *_args):
            nonlocal row
            result, row = row, None
            return result

    monkeypatch.setattr(dashboard_auth, "get_db", lambda: _DB(Conn()))
    request = _Request(cookies={dashboard_auth.PREAUTH_COOKIE: "preauth"})
    assert await dashboard_auth._consume_challenge(request, "authenticate") == b"x" * 32
    assert await dashboard_auth._consume_challenge(request, "authenticate") is None


def test_cookie_write_authorization_requires_csrf_header():
    no_header = _Request(session=_live_session())
    with_header = _Request(headers={"X-Unitares-Csrf": "1"}, session=_live_session())
    assert dashboard_auth.dashboard_session_authenticated(no_header)
    assert not dashboard_auth.dashboard_session_write_authorized(no_header)
    assert dashboard_auth.dashboard_session_write_authorized(with_header)


@pytest.mark.asyncio
async def test_rest_write_accepts_session_only_with_csrf():
    no_header = _Request(session=_live_session(), body={})
    denied = await http_api.http_harness_outcome(no_header)
    assert denied.status_code == 403

    with_header = _Request(
        headers={"X-Unitares-Csrf": "1"},
        session=_live_session(),
        body={},
    )
    allowed_to_validation = await http_api.http_harness_outcome(with_header)
    assert allowed_to_validation.status_code == 400
    assert "agent_uuid" in _body(allowed_to_validation)["error"]


@pytest.mark.asyncio
async def test_credential_revoke_cascades_to_sessions(monkeypatch):
    executed = []

    class Conn:
        def transaction(self):
            return _Context(self)

        async def fetchrow(self, _sql, credential_id):
            return {"credential_id": credential_id, "operator_label": "operator"}

        async def fetchval(self, _sql, _label):
            return 2

        async def execute(self, sql, *args):
            executed.append((sql, args))
            return "UPDATE 1"

    monkeypatch.setattr(dashboard_auth, "get_db", lambda: _DB(Conn()))
    monkeypatch.setattr(dashboard_auth, "_emit_auth_event", AsyncMock())
    credential = dashboard_auth._b64url(b"z" * 32)
    response = await dashboard_auth.http_auth_credential_revoke(
        _Request(
            headers={"X-Unitares-Csrf": "1"},
            session=_live_session(),
            path_params={"credential_id": credential},
        )
    )
    assert response.status_code == 200
    sql = "\n".join(item[0] for item in executed)
    assert "UPDATE core.webauthn_credentials SET revoked_at" in sql
    assert "UPDATE core.dashboard_sessions SET revoked_at" in sql


@pytest.mark.asyncio
async def test_last_credential_requires_fresh_step_up(monkeypatch):
    executed = []

    class Conn:
        def transaction(self):
            return _Context(self)

        async def fetchrow(self, _sql, credential_id):
            return {"credential_id": credential_id, "operator_label": "operator"}

        async def fetchval(self, _sql, _label):
            return 1

        async def execute(self, sql, *args):
            executed.append((sql, args))
            return "UPDATE 1"

    monkeypatch.setattr(dashboard_auth, "get_db", lambda: _DB(Conn()))
    credential = dashboard_auth._b64url(b"z" * 32)
    response = await dashboard_auth.http_auth_credential_revoke(
        _Request(
            headers={"X-Unitares-Csrf": "1"},
            session=_live_session(created_at=datetime.now(timezone.utc) - timedelta(hours=1)),
            path_params={"credential_id": credential},
        )
    )
    assert response.status_code == 403
    assert executed == []


def test_websocket_gate_precedes_accept():
    source = inspect.getsource(http_api.websocket_eisv_stream)
    assert source.index("_check_ws_auth") < source.index("broadcaster_instance.connect")


def test_cors_does_not_allow_credentials():
    source = inspect.getsource(__import__("src.mcp_server", fromlist=["main"]))
    assert "allow_credentials=True" not in source
    assert "allow_credentials=False" in source


def test_manual_migration_057_is_self_registering_and_complete():
    path = (
        __import__("pathlib").Path(__file__).parent.parent
        / "db/postgres/migrations/057_dashboard_webauthn.sql"
    )
    sql = path.read_text()
    for table in (
        "core.webauthn_credentials",
        "core.dashboard_sessions",
        "core.webauthn_challenges",
        "core.webauthn_enroll_codes",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "VALUES (57, 'dashboard_webauthn', NOW())" in sql
    assert "ON CONFLICT (version) DO NOTHING" in sql
