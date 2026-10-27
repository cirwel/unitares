"""Hub sign-in handoff: the server half (portal-hub-v0 H2b).

The routes live in src/dashboard_auth.py. These tests pin the boundary:
inert until configured, the approval page behind a fresh passkey session,
the /v1/hub routes only on the owner-only socket with the hub credential,
and, against the real test database, single-use PKCE-bound codes and hub
sessions that end with their parent dashboard session.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette
from starlette.routing import Route

from src import dashboard_auth
from src.http_routes.dashboard import http_dashboard_redesign

ROOT = Path(__file__).parent.parent
CALLBACK = "http://127.0.0.1:8791/auth/callback"
TOKEN = "t" * 40
ORIGIN = "https://gov.example.org"


def _verifier_and_challenge() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _session(**overrides):
    session = {
        "session_hash": b"s" * 32,
        "credential_id": b"c" * 32,
        "operator_label": "operator",
        "created_at": datetime.now(timezone.utc),
    }
    session.update(overrides)
    return session


@pytest.fixture(autouse=True)
def _hub_env(monkeypatch):
    monkeypatch.setenv(dashboard_auth.HUB_CALLBACK_ENV, CALLBACK)
    monkeypatch.setenv(dashboard_auth.HUB_TOKEN_ENV, TOKEN)
    monkeypatch.setattr(dashboard_auth, "DASHBOARD_RP_ID", "gov.example.org")
    monkeypatch.setattr(dashboard_auth, "DASHBOARD_EXPECTED_ORIGIN", ORIGIN)
    monkeypatch.setattr(dashboard_auth, "_emit_auth_event", AsyncMock())


def _app(*, session=None, peer_pid=None, public=False):
    """The four hub routes, with the scope the server's middleware would set."""
    inner = Starlette(routes=[
        Route("/auth/hub/authorize", dashboard_auth.http_hub_authorize, methods=["GET", "POST"]),
        Route("/v1/hub/exchange", dashboard_auth.http_hub_exchange, methods=["POST"]),
        Route("/v1/hub/check", dashboard_auth.http_hub_check, methods=["POST"]),
        Route("/v1/hub/revoke", dashboard_auth.http_hub_revoke, methods=["POST"]),
    ])

    async def app(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            state = dict(scope.get("state") or {})
            if session is not None:
                state["dashboard_session"] = session
            scope["state"] = state
            if peer_pid is not None:
                scope["unitares_peer_pid"] = peer_pid
            if public:
                scope["unitares_public_listener"] = True
        await inner(scope, receive, send)

    return app


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)


def _approve_headers():
    return {"Origin": ORIGIN, "X-Unitares-Csrf": "1"}


def _hub_headers(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# --- configuration ---------------------------------------------------------


@pytest.mark.parametrize("callback", [
    "https://127.0.0.1:8791/cb",          # not plain loopback http
    "http://localhost:8791/cb",           # a name, not a numeric address
    "http://127.0.0.1/cb",                # no explicit port
    "http://127.0.0.1:8791/cb?x=1",       # query
    "http://127.0.0.1:8791/cb#f",         # fragment
    "http://user@127.0.0.1:8791/cb",      # user info
    "http://10.0.0.5:8791/cb",            # off this machine
    "http://127.0.0.1:8791",              # no path
])
def test_callback_must_be_numeric_loopback_http(monkeypatch, callback):
    monkeypatch.setenv(dashboard_auth.HUB_CALLBACK_ENV, callback)
    config, problem = dashboard_auth.hub_config()
    assert config is None and dashboard_auth.HUB_CALLBACK_ENV in problem


def test_ipv6_loopback_callback_is_accepted(monkeypatch):
    monkeypatch.setenv(dashboard_auth.HUB_CALLBACK_ENV, "http://[::1]:8791/auth/callback")
    config, _ = dashboard_auth.hub_config()
    assert config["origin"] == "http://[::1]:8791"


def test_short_hub_token_turns_the_handoff_off(monkeypatch):
    monkeypatch.setenv(dashboard_auth.HUB_TOKEN_ENV, "tooSmall")
    config, problem = dashboard_auth.hub_config()
    assert config is None
    assert "tooSmall" not in problem  # the problem names the setting, never the value


@pytest.mark.asyncio
@pytest.mark.parametrize("method, path", [
    ("GET", "/auth/hub/authorize"),
    ("POST", "/auth/hub/authorize"),
    ("POST", "/v1/hub/exchange"),
    ("POST", "/v1/hub/check"),
    ("POST", "/v1/hub/revoke"),
])
async def test_every_hub_route_is_inert_until_configured(monkeypatch, method, path):
    monkeypatch.delenv(dashboard_auth.HUB_CALLBACK_ENV)
    monkeypatch.setattr(dashboard_auth, "get_db", lambda: pytest.fail("reached storage"))
    async with _client(_app(session=_session(), peer_pid=os.getpid())) as client:
        response = await client.request(method, path, headers={**_approve_headers(), **_hub_headers()}, json={})
    assert response.status_code == 404
    assert response.json()["error"] == "hub sign-in is not configured"


# --- the approval page -------------------------------------------------------


def _authorize_query(challenge: str, state: str = "s" * 22) -> dict[str, str]:
    return {"code_challenge": challenge, "code_challenge_method": "S256", "state": state}


@pytest.mark.asyncio
async def test_approval_without_a_session_goes_through_the_passkey_and_back():
    _, challenge = _verifier_and_challenge()
    async with _client(_app()) as client:
        response = await client.get("/auth/hub/authorize", params=_authorize_query(challenge))
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/auth/signin?stepup=1&next=")
    back = parse_qs(urlsplit(location).query)["next"][0]
    assert back.startswith("/auth/hub/authorize?") and challenge in back


@pytest.mark.asyncio
async def test_a_stale_session_must_verify_again():
    _, challenge = _verifier_and_challenge()
    stale = _session(created_at=datetime.now(timezone.utc) - timedelta(minutes=11))
    async with _client(_app(session=stale)) as client:
        page = await client.get("/auth/hub/authorize", params=_authorize_query(challenge))
        mint = await client.post(
            "/auth/hub/authorize", headers=_approve_headers(), json=_authorize_query(challenge)
        )
    assert page.status_code == 303
    assert mint.status_code == 403


@pytest.mark.asyncio
async def test_approval_page_names_the_hub_and_cannot_be_framed():
    _, challenge = _verifier_and_challenge()
    async with _client(_app(session=_session())) as client:
        response = await client.get("/auth/hub/authorize", params=_authorize_query(challenge))
    assert response.status_code == 200
    assert "http://127.0.0.1:8791" in response.text
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    {"code_challenge": "x" * 43, "code_challenge_method": "plain", "state": "s" * 22},
    {"code_challenge": "short", "code_challenge_method": "S256", "state": "s" * 22},
    {"code_challenge": "x" * 43, "code_challenge_method": "S256", "state": "short"},
    {"code_challenge": "x" * 43, "code_challenge_method": "S256"},
])
async def test_approval_needs_s256_and_a_hub_state(query):
    async with _client(_app(session=_session())) as client:
        response = await client.get("/auth/hub/authorize", params=query)
    assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {"Origin": ORIGIN},                                   # no CSRF header
    {"X-Unitares-Csrf": "1"},                             # no Origin
    {"Origin": "https://evil.example", "X-Unitares-Csrf": "1"},
])
async def test_minting_a_code_needs_csrf_and_the_dashboard_origin(monkeypatch, headers):
    monkeypatch.setattr(dashboard_auth, "get_db", lambda: pytest.fail("reached storage"))
    _, challenge = _verifier_and_challenge()
    async with _client(_app(session=_session())) as client:
        response = await client.post(
            "/auth/hub/authorize", headers=headers, json=_authorize_query(challenge)
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_the_static_route_never_serves_the_approval_page():
    class _Request:
        path_params = {"file": "auth/hub-authorize.html"}

    response = await http_dashboard_redesign(_Request())
    assert response.status_code == 404


def test_signin_returns_only_to_the_hub_approval_path():
    source = (ROOT / "dashboard" / "redesign" / "auth" / "signin.html").read_text()
    assert 'next.startsWith("/auth/hub/authorize?")' in source


# --- the internal routes -----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/hub/exchange", "/v1/hub/check", "/v1/hub/revoke"])
@pytest.mark.parametrize("scope", [
    {},                                         # TCP: no kernel peer pid
    {"peer_pid": os.getpid(), "public": True},  # the public tunnel listener
])
async def test_internal_routes_refuse_tcp_even_with_the_hub_token(monkeypatch, path, scope):
    monkeypatch.setattr(dashboard_auth, "get_db", lambda: pytest.fail("reached storage"))
    async with _client(_app(**scope)) as client:
        response = await client.post(path, headers=_hub_headers(), json={"handle": "h" * 43})
    assert response.status_code == 403
    assert "local socket" in response.json()["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}])
async def test_internal_routes_need_the_hub_token(monkeypatch, headers):
    monkeypatch.setattr(dashboard_auth, "get_db", lambda: pytest.fail("reached storage"))
    async with _client(_app(peer_pid=os.getpid())) as client:
        response = await client.post("/v1/hub/check", headers=headers, json={"handle": "h" * 43})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_check_fails_closed_when_storage_is_down(monkeypatch):
    def _down():
        raise RuntimeError("db down")

    monkeypatch.setattr(dashboard_auth, "get_db", _down)
    async with _client(_app(peer_pid=os.getpid())) as client:
        response = await client.post("/v1/hub/check", headers=_hub_headers(), json={"handle": "h" * 43})
    assert response.status_code == 503
    assert response.json()["ok"] is False


@pytest.mark.asyncio
async def test_the_real_socket_listener_stamps_what_the_gate_reads():
    """Over the real owner-only UDS listener the gate passes; it is not a mock key."""
    if sys.platform != "darwin":
        pytest.skip("LOCAL_PEERPID is macOS-specific")
    from src import uds_listener

    with tempfile.TemporaryDirectory(prefix="hub") as tmp:
        sock_path = os.path.join(tmp, "g.sock")
        task = await uds_listener.start_uds_listener(_app(), sock_path, log_level="error")
        try:
            transport = httpx.AsyncHTTPTransport(uds=sock_path)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
                no_token = await client.post("/v1/hub/check", json={"handle": "h" * 43})
                malformed = await client.post(
                    "/v1/hub/check", headers=_hub_headers(), json={"handle": "!"}
                )
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    # Past the socket check: the token is what refuses, then the handle.
    assert no_token.status_code == 401
    assert malformed.status_code == 401 and malformed.json() == {"ok": False}


# --- end to end against the test database -----------------------------------


@pytest_asyncio.fixture
async def hub_db(live_postgres_backend, monkeypatch):
    """A passkey credential and a fresh dashboard session in governance_test."""
    backend = live_postgres_backend
    monkeypatch.setattr(dashboard_auth, "get_db", lambda: backend)
    credential_id = secrets.token_bytes(32)
    session_hash = secrets.token_bytes(32)
    async with backend.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO core.webauthn_credentials
                (credential_id, public_key, user_handle, operator_label)
            VALUES ($1, $2, $3, 'operator')
            """,
            credential_id, b"pk", b"uh",
        )
        created_at = await conn.fetchval(
            """
            INSERT INTO core.dashboard_sessions
                (session_hash, credential_id, operator_label, created_at,
                 expires_at, hard_expires_at)
            VALUES ($1, $2, 'operator', now(), now() + interval '30 days',
                    now() + interval '90 days')
            RETURNING created_at
            """,
            session_hash, credential_id,
        )
    session = _session(
        session_hash=session_hash, credential_id=credential_id, created_at=created_at
    )
    yield backend, session
    async with backend.acquire() as conn:
        await conn.execute("DELETE FROM core.hub_sessions WHERE credential_id = $1", credential_id)
        await conn.execute("DELETE FROM core.hub_auth_codes WHERE credential_id = $1", credential_id)
        await conn.execute("DELETE FROM core.dashboard_sessions WHERE credential_id = $1", credential_id)
        await conn.execute("DELETE FROM core.webauthn_credentials WHERE credential_id = $1", credential_id)


async def _approve(session, challenge, state="hub-state-0123456789") -> dict[str, str]:
    async with _client(_app(session=session)) as client:
        response = await client.post(
            "/auth/hub/authorize",
            headers=_approve_headers(),
            json=_authorize_query(challenge, state),
        )
    assert response.status_code == 200, response.text
    redirect = urlsplit(response.json()["redirect"])
    assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == CALLBACK
    query = {k: v[0] for k, v in parse_qs(redirect.query).items()}
    assert query["state"] == state
    return query


async def _internal(path, body):
    async with _client(_app(peer_pid=os.getpid())) as client:
        return await client.post(path, headers=_hub_headers(), json=body)


@pytest.mark.asyncio
async def test_handoff_end_to_end(hub_db):
    backend, session = hub_db
    verifier, challenge = _verifier_and_challenge()
    code = (await _approve(session, challenge))["code"]

    exchanged = await _internal("/v1/hub/exchange", {"code": code, "code_verifier": verifier})
    assert exchanged.status_code == 200, exchanged.text
    handle = exchanged.json()["handle"]
    assert exchanged.json()["operator_label"] == "operator"

    # The code is single-use.
    replay = await _internal("/v1/hub/exchange", {"code": code, "code_verifier": verifier})
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"

    checked = await _internal("/v1/hub/check", {"handle": handle})
    assert checked.status_code == 200
    assert checked.json()["ok"] is True and checked.json()["fresh"] is True

    # Checking never touches the parent dashboard session.
    async with backend.acquire() as conn:
        before = await conn.fetchrow(
            "SELECT expires_at, last_seen_at FROM core.dashboard_sessions WHERE session_hash = $1",
            session["session_hash"],
        )
    await _internal("/v1/hub/check", {"handle": handle})
    async with backend.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT expires_at, last_seen_at FROM core.dashboard_sessions WHERE session_hash = $1",
            session["session_hash"],
        )
    assert dict(before) == dict(after)

    # Only digests are stored.
    async with backend.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT count(*) FROM core.hub_sessions WHERE handle_hash = $1",
            hashlib.sha256(handle.encode()).digest(),
        )
    assert stored == 1

    revoked = await _internal("/v1/hub/revoke", {"handle": handle})
    assert revoked.json() == {"success": True, "revoked": True}
    after_revoke = await _internal("/v1/hub/check", {"handle": handle})
    assert after_revoke.status_code == 401


@pytest.mark.asyncio
async def test_a_wrong_verifier_burns_the_code(hub_db):
    _, session = hub_db
    verifier, challenge = _verifier_and_challenge()
    code = (await _approve(session, challenge))["code"]
    wrong, _ = _verifier_and_challenge()

    refused = await _internal("/v1/hub/exchange", {"code": code, "code_verifier": wrong})
    assert refused.status_code == 400
    retry = await _internal("/v1/hub/exchange", {"code": code, "code_verifier": verifier})
    assert retry.status_code == 400


@pytest.mark.asyncio
async def test_an_expired_code_is_refused(hub_db):
    backend, session = hub_db
    verifier, challenge = _verifier_and_challenge()
    code = (await _approve(session, challenge))["code"]
    async with backend.acquire() as conn:
        await conn.execute(
            "UPDATE core.hub_auth_codes SET expires_at = now() - interval '1 second' WHERE code_hash = $1",
            hashlib.sha256(code.encode()).digest(),
        )
    refused = await _internal("/v1/hub/exchange", {"code": code, "code_verifier": verifier})
    assert refused.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("ended", ["dashboard_sessions", "webauthn_credentials"])
async def test_ending_the_parent_ends_the_hub_session(hub_db, ended):
    backend, session = hub_db
    verifier, challenge = _verifier_and_challenge()
    code = (await _approve(session, challenge))["code"]
    handle = (await _internal("/v1/hub/exchange", {"code": code, "code_verifier": verifier})).json()["handle"]
    assert (await _internal("/v1/hub/check", {"handle": handle})).status_code == 200

    key = "session_hash" if ended == "dashboard_sessions" else "credential_id"
    async with backend.acquire() as conn:
        await conn.execute(
            f"UPDATE core.{ended} SET revoked_at = now() WHERE {key} = $1", session[key]
        )
    assert (await _internal("/v1/hub/check", {"handle": handle})).status_code == 401


@pytest.mark.asyncio
async def test_a_revoked_parent_cannot_exchange_a_code_it_approved(hub_db):
    backend, session = hub_db
    verifier, challenge = _verifier_and_challenge()
    code = (await _approve(session, challenge))["code"]
    async with backend.acquire() as conn:
        await conn.execute(
            "UPDATE core.dashboard_sessions SET revoked_at = now() WHERE session_hash = $1",
            session["session_hash"],
        )
    refused = await _internal("/v1/hub/exchange", {"code": code, "code_verifier": verifier})
    assert refused.status_code == 400


@pytest.mark.asyncio
async def test_an_old_hub_session_is_live_but_not_fresh(hub_db):
    backend, session = hub_db
    verifier, challenge = _verifier_and_challenge()
    code = (await _approve(session, challenge))["code"]
    handle = (await _internal("/v1/hub/exchange", {"code": code, "code_verifier": verifier})).json()["handle"]
    async with backend.acquire() as conn:
        await conn.execute(
            "UPDATE core.hub_sessions SET created_at = now() - interval '11 minutes' WHERE handle_hash = $1",
            hashlib.sha256(handle.encode()).digest(),
        )
    checked = await _internal("/v1/hub/check", {"handle": handle})
    assert checked.json()["ok"] is True and checked.json()["fresh"] is False
