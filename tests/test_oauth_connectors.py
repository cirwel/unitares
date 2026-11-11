"""Runtime connector enrollment through the real SDK authorization/token flow."""
import base64
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from src import oauth_connectors as registry_module
from src.http_routes import connectors as routes
from src.oauth_connector_transport import install_connector_transport
from src.oauth_connectors import ConnectorRegistry, secret_digest, validate_registration
from src.oauth_provider import GovernanceOAuthProvider

ID = "unitares_connector_" + "a" * 32
SECRET = "random-test-secret"
CALLBACK = "https://client.example/callback/"


class Registry:
    def __init__(self):
        self.record = {"client_id": ID, "name": "My connector", "redirect_uris": [CALLBACK],
                       "secret_hash": secret_digest(SECRET)}
        self.available = True

    async def get(self, client_id):
        if not self.available:
            raise ConnectionError("database unavailable")
        return self.record if self.record and client_id == ID else None

    async def list(self):
        if not self.available:
            raise ConnectionError()
        return [registry_module.public_record(self.record)] if self.record else []

    async def create(self, name, redirects, actor):
        validate_registration(name, redirects)
        if not self.available:
            raise ConnectionError()
        return {"client_id": ID, "client_secret": SECRET, "name": name}

    async def revoke(self, client_id, actor):
        if client_id != ID or self.record is None:
            return False
        self.record = None
        return True


def provider(registry):
    return GovernanceOAuthProvider(auto_approve=False, connector_registry=registry)


def client_app(p):
    app = Starlette(routes=create_auth_routes(
        p, AnyHttpUrl("https://server.example"),
        client_registration_options=ClientRegistrationOptions(enabled=False),
    ))
    install_connector_transport(app, p)
    return TestClient(app)


def code(client, **extra):
    response = client.get("/authorize", params={
        "client_id": ID, "redirect_uri": CALLBACK, "response_type": "code", **extra,
    }, follow_redirects=False)
    assert response.status_code == 302, response.text
    return parse_qs(urlsplit(response.headers["location"]).query)["code"][0]


def exchange(client, auth_code, secret=SECRET, basic=False, **extra):
    data = {"grant_type": "authorization_code", "code": auth_code, "redirect_uri": CALLBACK, **extra}
    headers = {}
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{ID}:{secret}".encode()).decode()
    else:
        data.update(client_id=ID, client_secret=secret)
    return client.post("/token", data=data, headers=headers)


@pytest.mark.parametrize("basic", [False, True])
def test_created_client_can_authorize_without_restart_and_refresh(basic):
    registry = Registry()
    p = provider(registry)
    client = client_app(p)
    response = exchange(client, code(client), basic=basic)
    assert response.status_code == 200, response.text
    tokens = response.json()
    assert "access_token" in tokens
    refreshed = client.post("/token", data={
        "client_id": ID, "client_secret": SECRET, "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
    })
    assert refreshed.status_code == 200
    assert ID not in p._clients  # no stale managed credential cached as DCR
    # A fresh provider sees the same durable registration.
    other = client_app(provider(registry))
    assert exchange(other, code(other)).status_code == 200


@pytest.mark.parametrize("wrong", ["", "wrong", secret_digest(SECRET)])
def test_secret_required_and_stored_hash_is_not_a_password(wrong):
    client = client_app(provider(Registry()))
    assert exchange(client, code(client), secret=wrong).status_code == 401


def test_supplied_pkce_is_preserved_and_wrong_verifier_rejected():
    import hashlib
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    client = client_app(provider(Registry()))
    auth_code = code(client, code_challenge=challenge, code_challenge_method="S256")
    assert exchange(client, auth_code, code_verifier="wrong").status_code == 400
    assert exchange(client, auth_code, code_verifier=verifier).status_code == 200


@pytest.mark.asyncio
async def test_revocation_and_database_outage_reject_cached_access_and_refresh():
    registry = Registry()
    p = provider(registry)
    client = client_app(p)
    token = exchange(client, code(client)).json()
    assert await p.load_access_token(token["access_token"])
    registry.available = False
    assert await p.load_access_token(token["access_token"]) is None
    outage = exchange(client, "unused")
    assert outage.status_code == 503
    assert outage.json()["error"] == "temporarily_unavailable"
    registry.available = True
    assert await p.load_access_token(token["access_token"])
    await registry.revoke(ID, "operator")
    assert await p.load_access_token(token["access_token"]) is None
    assert client.post("/token", data={"client_id": ID, "client_secret": SECRET,
        "grant_type": "refresh_token", "refresh_token": token["refresh_token"]}).status_code == 401


def test_exact_callback_and_conflicting_credentials_rejected():
    client = client_app(provider(Registry()))
    response = client.get("/authorize", params={"client_id": ID, "response_type": "code",
        "redirect_uri": CALLBACK.rstrip("/")}, follow_redirects=False)
    assert response.status_code == 400
    auth = "Basic " + base64.b64encode(f"{ID}:{SECRET}".encode()).decode()
    response = client.post("/token", headers={"Authorization": auth}, data={
        "client_id": ID, "client_secret": SECRET, "grant_type": "refresh_token", "refresh_token": "fake",
    })
    assert response.status_code == 400


@pytest.mark.parametrize("uri", ["https://example.com/*", "https://example.com/#fragment",
    "http://remote.example/callback", "https://user:secret@example.com/callback",
    "https://example.com/call back", "https://example.com:bad/callback", "javascript:alert(1)",
    "https://exa<mple.com/cb"])
def test_invalid_redirects(uri):
    with pytest.raises(ValueError):
        validate_registration("App", [uri])


@pytest.mark.parametrize("uri", [CALLBACK, "http://127.0.0.1:4242/callback", "http://[::1]:4242/callback"])
def test_redirects_are_kept_exact(uri):
    assert validate_registration(" App ", [uri]) == ("App", [uri])


@pytest.mark.asyncio
async def test_registry_persists_hash_and_only_create_returns_secret(monkeypatch):
    captured = {}
    class Connection:
        async def fetchrow(self, query, *args, **kwargs):
            captured["args"] = args
            return {"client_id": args[0], "name": args[1], "redirect_uris": args[3],
                    "created_at": datetime.now(timezone.utc), "revoked_at": None}
        async def fetch(self, query, *args, **kwargs):
            return [{"client_id": ID, "name": "App", "redirect_uris": f'["{CALLBACK}"]',
                     "secret_hash": secret_digest(SECRET),
                     "created_at": datetime.now(timezone.utc), "revoked_at": None}]
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(registry_module, "get_db", lambda: SimpleNamespace(acquire=lambda **kwargs: Connection()))
    result = await ConnectorRegistry().create("App", [CALLBACK], "operator")
    assert captured["args"][2] == secret_digest(result["client_secret"])
    assert result["client_secret"] not in captured["args"]
    assert "secret_hash" not in result
    listed = await ConnectorRegistry().list()
    assert len(listed) == 1
    assert listed[0]["client_id"] == ID
    assert "secret_hash" not in listed[0]
    assert "client_secret" not in listed[0]


def management_client(monkeypatch, registry, *, operator=False, session=False, fresh=False):
    monkeypatch.setattr(routes.dashboard_auth, "_operator_token_authorized", lambda r: operator)
    monkeypatch.setattr(routes.dashboard_auth, "dashboard_session_authenticated", lambda r: session)
    monkeypatch.setattr(routes.dashboard_auth, "dashboard_session_is_fresh", lambda r: fresh)
    monkeypatch.setattr(routes.dashboard_auth, "dashboard_session_write_authorized",
                        lambda r: session and r.headers.get("x-unitares-csrf") == "1")
    async def handler(request):
        request.state.oauth_provider = provider(registry)
        request.state.oauth_issuer = "https://server.example"
        request.state.oauth_resource = "https://server.example/mcp/"
        return await routes.http_connectors(request)
    return TestClient(Starlette(routes=[
        Route("/auth/connectors", handler, methods=["GET", "POST"]),
        Route("/auth/connectors/{client_id}/revoke", handler, methods=["POST"]),
    ]))


def test_management_refuses_agent_bearer_and_cross_origin(monkeypatch):
    client = management_client(monkeypatch, Registry())
    assert client.get("/auth/connectors", headers={"Authorization": "Bearer agent"}).status_code == 403
    client = management_client(monkeypatch, Registry(), operator=True)
    assert client.post("/auth/connectors", headers={"Origin": "https://evil.example"}, json={}).status_code == 403


def test_management_requires_fresh_session_and_csrf(monkeypatch):
    payload = {"name": "App", "redirect_uris": [CALLBACK]}
    client = management_client(monkeypatch, Registry(), session=True, fresh=False)
    stale = client.post("/auth/connectors", headers={"X-Unitares-Csrf": "1"}, json=payload)
    assert stale.status_code == 403
    assert stale.json()["signin_url"] == "/auth/signin?stepup=1&next=%2F%23connectors"
    client = management_client(monkeypatch, Registry(), session=True, fresh=True)
    assert client.post("/auth/connectors", json=payload).status_code == 403
    response = client.post("/auth/connectors", headers={"X-Unitares-Csrf": "1"}, json=payload)
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["client_secret"] == SECRET
    listed = client.get("/auth/connectors")
    assert listed.json()["connectors"][0]["client_id"] == ID
    assert SECRET not in listed.text
    assert secret_digest(SECRET) not in listed.text


def test_management_storage_failure_never_claims_success(monkeypatch):
    registry = Registry()
    registry.available = False
    client = management_client(monkeypatch, registry, operator=True)
    response = client.post("/auth/connectors", json={"name": "App", "redirect_uris": [CALLBACK]})
    assert response.status_code == 503
    assert "client_secret" not in response.text


def test_revoke_of_missing_connector_returns_actionable_error(monkeypatch):
    registry = Registry()
    registry.record = None
    client = management_client(monkeypatch, registry, operator=True)
    response = client.post(f"/auth/connectors/{ID}/revoke")
    assert response.status_code == 404
    assert response.json() == {
        "success": False,
        "error": "Connector is already disabled or no longer exists. Refresh the list before trying again.",
    }


def test_local_dashboard_operator_origin_is_accepted(monkeypatch):
    client = management_client(monkeypatch, Registry(), operator=True)
    response = client.post("/auth/connectors", headers={"Origin": "http://testserver"},
                           json={"name": "App", "redirect_uris": [CALLBACK]})
    assert response.status_code == 201


def test_legacy_client_multipart_token_requests_still_reach_sdk():
    from src.oauth_provider import build_static_client
    p = GovernanceOAuthProvider(auto_approve=False, connector_registry=Registry(),
        static_clients=[build_static_client("legacy", "legacy-secret", [CALLBACK])])
    client = client_app(p)
    response = client.post("/token", files={key: (None, value) for key, value in {
        "client_id": "legacy", "client_secret": "legacy-secret",
        "grant_type": "refresh_token", "refresh_token": "unknown",
    }.items()})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"  # authenticated, SDK processed grant


def test_literal_semicolon_is_rejected_only_for_managed_clients():
    from src.oauth_provider import build_static_client

    p = GovernanceOAuthProvider(auto_approve=False, connector_registry=Registry(),
        static_clients=[build_static_client("legacy", "legacy-secret", [CALLBACK])])
    client = client_app(p)
    tokens = exchange(client, code(client)).json()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    legacy = client.post("/token", headers=headers,
        content="client_id=legacy&client_secret=legacy-secret&grant_type=refresh_token&refresh_token=unknown;value")
    assert legacy.status_code == 400
    assert legacy.json()["error"] == "invalid_grant"  # SDK handled the legacy request

    managed = client.post("/token", headers=headers,
        content=f"client_id={ID}&client_secret={SECRET}&grant_type=refresh_token&refresh_token=unknown;value")
    assert managed.status_code == 400
    assert managed.json()["error"] == "invalid_request"

    # Semicolon-delimited managed client_id hidden behind a non-managed & field,
    # Basic header, or percent-encoding must also be rejected as invalid_request
    # rather than reaching ConnectorAuthenticator's semicolon-splitting form parser.
    smuggled = client.post("/token", headers=headers, content=(
        f"grant_type=refresh_token&refresh_token={tokens['refresh_token']}"
        f"&client_id=legacy&z=1;client_id={ID};client_secret={SECRET}"
    ))
    assert smuggled.status_code == 400
    assert smuggled.json()["error"] == "invalid_request"

    encoded_id = "unitares_%63onnector_" + "a" * 32
    pct_smuggled = client.post("/token", headers=headers, content=(
        f"grant_type=refresh_token&refresh_token={tokens['refresh_token']}"
        f"&z=1;%63lient_id={encoded_id};client_secret={SECRET}"
    ))
    assert pct_smuggled.status_code == 400
    assert pct_smuggled.json()["error"] == "invalid_request"

    basic_legacy = base64.b64encode(b"legacy:legacy-secret").decode("ascii")
    basic_smuggled = client.post(
        "/token",
        headers={**headers, "Authorization": f"Basic {basic_legacy}"},
        content=f"grant_type=refresh_token&refresh_token={tokens['refresh_token']};client_id={ID};client_secret={SECRET}",
    )
    assert basic_smuggled.status_code == 400
    assert basic_smuggled.json()["error"] == "invalid_request"


def test_managed_multipart_cannot_bypass_client_authentication():
    client = client_app(provider(Registry()))
    response = client.post("/token", files={key: (None, value) for key, value in {
        "client_id": ID, "client_secret": secret_digest(SECRET),
        "grant_type": "refresh_token", "refresh_token": "unknown",
    }.items()})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_persisted_tokens_after_restart_are_denied_on_connector_revocation():
    from tests.test_oauth_persistence import _DictStore
    registry, store = Registry(), _DictStore()
    first = GovernanceOAuthProvider(auto_approve=False, connector_registry=registry, store=store)
    client = client_app(first)
    tokens = exchange(client, code(client)).json()
    restarted = GovernanceOAuthProvider(auto_approve=False, connector_registry=registry, store=store)
    info = await restarted.get_client(ID)
    assert await restarted.load_access_token(tokens["access_token"])
    assert await restarted.load_refresh_token(info, tokens["refresh_token"])
    assert f"client:{ID}" not in store.data  # no alternate DCR authority
    await registry.revoke(ID, "operator")
    assert await restarted.load_access_token(tokens["access_token"]) is None
    assert await restarted.load_refresh_token(info, tokens["refresh_token"]) is None


def test_management_through_real_session_and_operator_middleware(monkeypatch):
    from datetime import timedelta
    from src import dashboard_auth, http_api
    cookie = "synthetic-dashboard-session"
    row = {"created_at": datetime.now(timezone.utc), "last_seen_at": datetime.now(timezone.utc)}
    class Connection:
        async def fetchrow(self, query, digest):
            assert digest == dashboard_auth._sha256(cookie)
            return row
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(dashboard_auth, "get_db", lambda: SimpleNamespace(acquire=Connection))
    monkeypatch.setenv("UNITARES_OPERATOR_TOKENS", "synthetic-operator-token")
    app = Starlette()
    http_api.register_http_routes(app, server_ready_fn=lambda: True, server_start_time=0,
        server_version="test", has_streamable_http=True, mount_route_packs=False,
        oauth_provider=provider(Registry()), oauth_settings=SimpleNamespace(
            issuer_url="https://server.example", resource_server_url="https://server.example/mcp/"))
    client = TestClient(app)
    payload = {"name": "App", "redirect_uris": [CALLBACK]}
    assert client.post("/auth/connectors", json=payload).status_code == 403
    assert client.post("/auth/connectors", headers={"Authorization": "Bearer synthetic-operator-token"}, json=payload).status_code == 403
    assert client.post("/auth/connectors", headers={"X-Unitares-Operator": "synthetic-operator-token", "Origin": "http://testserver"}, json=payload).status_code == 201
    client.cookies.set(dashboard_auth.SESSION_COOKIE, cookie)
    assert client.get("/auth/connectors").json()["server_url"] == "https://server.example/mcp/"
    assert client.post("/auth/connectors", json=payload).status_code == 403
    headers = {"X-Unitares-Csrf": "1", "Origin": "http://testserver"}
    assert client.post("/auth/connectors", headers=headers, json=payload).status_code == 201
    row["created_at"] -= timedelta(seconds=dashboard_auth.STEP_UP_TTL_SECONDS + 1)
    assert client.post("/auth/connectors", headers=headers, json=payload).status_code == 403
    row = None
    assert client.get("/auth/connectors").status_code == 403


def test_management_deeply_nested_json_returns_400_and_storage_failure_logs(monkeypatch, caplog):
    import logging

    registry = Registry()
    client = management_client(monkeypatch, registry, operator=True)
    deeply_nested = b"[" * 15000
    nested = client.post(
        "/auth/connectors",
        headers={"Content-Type": "application/json"},
        content=deeply_nested,
    )
    assert nested.status_code == 400
    assert nested.json() == {"error": "Invalid JSON body."}

    with monkeypatch.context() as mp:
        mp.setattr(routes.json, "loads", lambda _raw: (_ for _ in ()).throw(RecursionError()))
        rec_err = client.post(
            "/auth/connectors",
            headers={"Content-Type": "application/json"},
            content=b"{}",
        )
    assert rec_err.status_code == 400
    assert rec_err.json() == {"error": "Invalid JSON body."}

    registry.available = False
    with caplog.at_level(logging.WARNING, logger="src.http_routes.connectors"):
        outage = client.get("/auth/connectors")
    assert outage.status_code == 503
    assert any("Connector registry operation failed (ConnectionError)" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_managed_connector_outage_logs_are_rate_limited(monkeypatch, caplog):
    import logging
    from src import oauth_provider as op_mod

    budget = op_mod._LineBudget(per_window=2, window=60.0)
    monkeypatch.setattr(op_mod, "_CONNECTOR_LOG_BUDGET", budget)
    registry = Registry()
    registry.available = False
    p = provider(registry)
    with caplog.at_level(logging.WARNING, logger="src.oauth_provider"):
        for _ in range(5):
            assert await p.managed_connector(ID) is None
    warnings = [r.message for r in caplog.records if "Connector registry unavailable" in r.message]
    assert len(warnings) == 2


@pytest.mark.asyncio
async def test_sdk_default_authenticator_fails_closed_for_managed_client():
    from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
    from starlette.requests import Request

    p = provider(Registry())
    body = f"client_id={ID}&client_secret={SECRET}&grant_type=refresh_token&refresh_token=unused".encode()
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/token",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        },
        receive,
    )
    with pytest.raises(AuthenticationError, match="no stored secret"):
        await ClientAuthenticator(p).authenticate_request(req)


def test_authorize_allows_repeated_rfc8707_resource_params_but_rejects_duplicate_client_id():
    client = client_app(provider(Registry()))
    ok = client.get(
        "/authorize",
        params=[
            ("client_id", ID),
            ("redirect_uri", CALLBACK),
            ("response_type", "code"),
            ("resource", "https://server.example/mcp/"),
            ("resource", "https://server.example/other/"),
        ],
        follow_redirects=False,
    )
    assert ok.status_code == 302
    redirect_params = parse_qs(urlsplit(ok.headers["location"]).query)
    assert "code" in redirect_params
    assert "error" not in redirect_params

    dup_client = client.get(
        "/authorize",
        params=[
            ("client_id", ID),
            ("client_id", ID),
            ("redirect_uri", CALLBACK),
            ("response_type", "code"),
        ],
        follow_redirects=False,
    )
    assert dup_client.status_code == 400
    assert dup_client.json() == {"error": "invalid_request"}


def test_token_refresh_reuses_middleware_connector_lookup_against_mid_request_outage():
    registry = Registry()
    client = client_app(provider(registry))
    tokens = exchange(client, code(client)).json()

    calls = 0
    real_get = registry.get

    async def flaky_get(client_id):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ConnectionError("pool exhausted mid-request")
        return await real_get(client_id)

    registry.get = flaky_get
    refreshed = client.post(
        "/token",
        data={
            "client_id": ID,
            "client_secret": SECRET,
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert refreshed.status_code == 200
    assert calls == 1

    # When the first lookup itself fails, the client receives retryable 503.
    outage = client.post(
        "/token",
        data={
            "client_id": ID,
            "client_secret": SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refreshed.json()["refresh_token"],
        },
    )
    assert outage.status_code == 503
    assert outage.json() == {"error": "temporarily_unavailable"}


def test_mcp_bearer_gate_returns_503_on_connector_registry_blip_without_double_lookup(monkeypatch):
    from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
    from mcp.server.auth.provider import ProviderTokenVerifier
    from starlette.middleware import Middleware
    from starlette.middleware.authentication import AuthenticationMiddleware
    from starlette.responses import JSONResponse
    from starlette.routing import Mount

    from src.services.mcp_transport_service import McpAuthConfig, make_streamable_mcp_asgi

    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)
    registry = Registry()
    p = provider(registry)
    auth_client = client_app(p)
    tokens = exchange(auth_client, code(auth_client)).json()

    class _DummySessionManager:
        async def handle_request(self, scope, receive, send):
            await JSONResponse({"ok": True})(scope, receive, send)

    app = Starlette(
        routes=[
            *create_auth_routes(
                p,
                AnyHttpUrl("https://server.example"),
                client_registration_options=ClientRegistrationOptions(enabled=False),
            ),
            Mount(
                "/mcp",
                app=make_streamable_mcp_asgi(
                    _DummySessionManager(),
                    auth_config=McpAuthConfig(oauth_provider=p),
                ),
            ),
        ],
        middleware=[
            Middleware(
                AuthenticationMiddleware,
                backend=BearerAuthBackend(ProviderTokenVerifier(p)),
            )
        ],
    )
    install_connector_transport(app, p)
    mcp_client = TestClient(app)

    calls = 0
    real_get = registry.get

    async def counting_get(client_id):
        nonlocal calls
        calls += 1
        return await real_get(client_id)

    registry.get = counting_get
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    ok = mcp_client.post("/mcp/", headers=headers)
    assert ok.status_code == 200
    assert calls == 1

    # Postgres blip -> 503 temporarily_unavailable (not 401), single DB attempt
    registry.available = False
    blip = mcp_client.post("/mcp/", headers=headers)
    assert blip.status_code == 503
    assert blip.headers["retry-after"] == "5"
    assert blip.json()["error"] == "temporarily_unavailable"
    assert calls == 2

    # Break-glass static bearer allowlist still overrides a connector outage
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKENS", "break-glass-token")
    override = mcp_client.post(
        "/mcp/", headers={"Authorization": "Bearer break-glass-token"}
    )
    assert override.status_code == 200

    # Revoked connector when DB recovers -> 401 unauthorized
    monkeypatch.delenv("UNITARES_MCP_BEARER_TOKENS", raising=False)
    registry.available = True
    registry.record = None
    revoked = mcp_client.post("/mcp/", headers=headers)
    assert revoked.status_code == 401


def test_static_client_rejects_reserved_managed_connector_prefix():
    from mcp.shared.auth import OAuthClientInformationFull

    from src.oauth_provider import build_static_client

    with pytest.raises(ValueError, match="unitares_connector_"):
        build_static_client(
            "unitares_connector_custom", "s3cret", ["https://client.example/cb"]
        )

    raw_static = OAuthClientInformationFull(
        client_id="unitares_connector_custom",
        client_secret="s3cret",
        redirect_uris=["https://client.example/cb"],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )
    with pytest.raises(ValueError, match="unitares_connector_"):
        GovernanceOAuthProvider(static_clients=[raw_static])
