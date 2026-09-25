"""OAuth state survives a server restart.

gov restarts often (every deploy, every launchd respawn). With tokens held
only in memory, each restart signed every connector out, and Google's
connector needs a manual reconnect to recover. These tests stand a second
provider up on the same store to model the restarted process.
"""

from __future__ import annotations

import json
import time

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from src.oauth_provider import (
    GovernanceOAuthProvider,
    OAuthStateStore,
    RedisOAuthStore,
    build_static_client,
)


class _DictStore(OAuthStateStore):
    def __init__(self):
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ttl):
        if ttl <= 0:
            return True
        self.data[key] = value
        self.ttls[key] = ttl
        return True

    async def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)
        return True


def _dcr_client(client_id="dcr-1"):
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="dcr-secret",
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
    )


async def _sign_in(provider, client):
    code_url = await provider.authorize(
        client,
        AuthorizationParams(
            state="s",
            scopes=["mcp:tools"],
            code_challenge="c",
            redirect_uri="https://claude.ai/api/mcp/auth_callback",
            redirect_uri_provided_explicitly=True,
        ),
    )
    code = code_url.split("code=")[1].split("&")[0]
    entry = await provider.load_authorization_code(client, code)
    return await provider.exchange_authorization_code(client, entry)


@pytest.mark.asyncio
async def test_tokens_and_dcr_client_survive_a_restart():
    store = _DictStore()
    before = GovernanceOAuthProvider(store=store)
    client = _dcr_client()
    await before.register_client(client)
    tokens = await _sign_in(before, client)

    after = GovernanceOAuthProvider(store=store)  # the restarted process
    reloaded = await after.get_client(client.client_id)
    assert reloaded is not None and reloaded.client_secret == "dcr-secret"
    access = await after.load_access_token(tokens.access_token)
    assert access is not None and access.client_id == client.client_id
    assert access.scopes == ["mcp:tools"]


@pytest.mark.asyncio
async def test_refresh_works_after_a_restart_and_is_single_use():
    store = _DictStore()
    before = GovernanceOAuthProvider(store=store)
    client = _dcr_client()
    await before.register_client(client)
    tokens = await _sign_in(before, client)

    after = GovernanceOAuthProvider(store=store)
    rt = await after.load_refresh_token(client, tokens.refresh_token)
    assert rt is not None
    rotated = await after.exchange_refresh_token(client, rt, [])
    assert await after.load_access_token(rotated.access_token) is not None

    again = GovernanceOAuthProvider(store=store)
    assert await again.load_refresh_token(client, tokens.refresh_token) is None
    assert await again.load_refresh_token(client, rotated.refresh_token) is not None


@pytest.mark.asyncio
async def test_the_store_never_holds_a_raw_token():
    store = _DictStore()
    provider = GovernanceOAuthProvider(store=store)
    client = _dcr_client()
    await provider.register_client(client)
    tokens = await _sign_in(provider, client)
    blob = json.dumps(store.data)
    assert tokens.access_token not in blob
    assert tokens.refresh_token not in blob


@pytest.mark.asyncio
async def test_ttls_follow_token_lifetimes():
    store = _DictStore()
    provider = GovernanceOAuthProvider(store=store, access_token_ttl=3600, refresh_token_ttl=604800)
    client = _dcr_client()
    await provider.register_client(client)
    await _sign_in(provider, client)
    at_ttl = next(v for k, v in store.ttls.items() if k.startswith("at:"))
    rt_ttl = next(v for k, v in store.ttls.items() if k.startswith("rt:"))
    assert 3590 <= at_ttl <= 3600
    assert 604790 <= rt_ttl <= 604800


@pytest.mark.asyncio
async def test_an_expired_persisted_access_token_is_refused():
    store = _DictStore()
    provider = GovernanceOAuthProvider(store=store)
    client = _dcr_client()
    await provider.register_client(client)
    tokens = await _sign_in(provider, client)
    key = next(k for k in store.data if k.startswith("at:"))
    data = json.loads(store.data[key])
    data["expires_at"] = int(time.time()) - 1
    store.data[key] = json.dumps(data)

    after = GovernanceOAuthProvider(store=store)
    assert await after.load_access_token(tokens.access_token) is None


@pytest.mark.asyncio
async def test_revocation_survives_a_restart():
    """A restarted process cannot enumerate unloaded refresh tokens, so an
    access-token revocation leaves a per-client marker they are checked against."""
    store = _DictStore()
    before = GovernanceOAuthProvider(store=store)
    client = _dcr_client()
    await before.register_client(client)
    tokens = await _sign_in(before, client)

    middle = GovernanceOAuthProvider(store=store)
    access = await middle.load_access_token(tokens.access_token)
    await middle.revoke_token(access)

    after = GovernanceOAuthProvider(store=store)
    assert await after.load_access_token(tokens.access_token) is None
    assert await after.load_refresh_token(client, tokens.refresh_token) is None


@pytest.mark.asyncio
async def test_a_refresh_token_is_bound_to_its_client_after_a_restart():
    store = _DictStore()
    before = GovernanceOAuthProvider(store=store)
    client = _dcr_client()
    await before.register_client(client)
    tokens = await _sign_in(before, client)

    after = GovernanceOAuthProvider(store=store)
    other = _dcr_client("dcr-2")
    assert await after.load_refresh_token(other, tokens.refresh_token) is None


@pytest.mark.asyncio
async def test_the_static_client_is_not_written_to_the_store():
    """It comes from the environment on every start; persisting it would let a
    stale secret outlive a rotation."""
    store = _DictStore()
    static = build_static_client("google", "s3cret", ["https://example.org/cb"])
    provider = GovernanceOAuthProvider(store=store, static_clients=[static])
    tokens = await _sign_in_static(provider, static)
    assert "client:google" not in store.data
    assert await GovernanceOAuthProvider(store=store, static_clients=[static]).load_access_token(
        tokens.access_token
    ) is not None


async def _sign_in_static(provider, client):
    code_url = await provider.authorize(
        client,
        AuthorizationParams(
            state=None,
            scopes=["mcp:tools"],
            code_challenge="c",
            redirect_uri="https://example.org/cb",
            redirect_uri_provided_explicitly=True,
        ),
    )
    code = code_url.split("code=")[1].split("&")[0]
    return await provider.exchange_authorization_code(
        client, await provider.load_authorization_code(client, code)
    )


@pytest.mark.asyncio
async def test_without_a_store_behaviour_is_memory_only():
    provider = GovernanceOAuthProvider()
    client = _dcr_client()
    await provider.register_client(client)
    tokens = await _sign_in(provider, client)
    assert await provider.load_access_token(tokens.access_token) is not None
    assert await GovernanceOAuthProvider().load_access_token(tokens.access_token) is None


# --------------------------------------------------------------------------- #
# RedisOAuthStore fails soft and never touches live Redis here
# --------------------------------------------------------------------------- #


class _FakeRedis:
    def __init__(self, fail=False):
        self.fail = fail
        self.data = {}
        self.ex = {}

    async def get(self, key):
        if self.fail:
            raise ConnectionError("down")
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("down")
        self.data[key] = value
        self.ex[key] = ex

    async def delete(self, *keys):
        if self.fail:
            raise ConnectionError("down")
        for k in keys:
            self.data.pop(k, None)


def _patch_redis(monkeypatch, client):
    async def _get_redis():
        return client

    monkeypatch.setattr("src.cache.redis_client.get_redis", _get_redis)


@pytest.mark.asyncio
async def test_redis_store_prefixes_keys_and_sets_ttls(monkeypatch):
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)
    store = RedisOAuthStore()
    await store.set("at:abc", "v", 60)
    assert fake.data == {"unitares:oauth:at:abc": "v"}
    assert fake.ex == {"unitares:oauth:at:abc": 60}
    assert await store.get("at:abc") == "v"
    await store.delete("at:abc")
    assert fake.data == {}


@pytest.mark.asyncio
async def test_redis_store_skips_non_positive_ttls(monkeypatch):
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)
    await RedisOAuthStore().set("at:abc", "v", 0)
    assert fake.data == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("client", [None, _FakeRedis(fail=True)])
async def test_redis_store_fails_soft(monkeypatch, client):
    """Redis down or disabled: a miss, never an error, so the provider keeps
    working from memory."""
    _patch_redis(monkeypatch, client)
    store = RedisOAuthStore()
    assert await store.get("at:abc") is None
    await store.set("at:abc", "v", 60)
    await store.delete("at:abc")

    provider = GovernanceOAuthProvider(store=store)
    c = _dcr_client()
    await provider.register_client(c)
    tokens = await _sign_in(provider, c)
    assert await provider.load_access_token(tokens.access_token) is not None


@pytest.mark.asyncio
async def test_redis_store_is_bounded_when_redis_hangs(monkeypatch):
    """A hung Redis must not hang the token endpoint or every /mcp request."""
    import asyncio

    class _Hangs(_FakeRedis):
        async def get(self, key):
            await asyncio.sleep(10)

    _patch_redis(monkeypatch, _Hangs())
    started = time.monotonic()
    assert await RedisOAuthStore(timeout=0.05).get("at:abc") is None
    assert time.monotonic() - started < 1


@pytest.mark.asyncio
async def test_registration_alone_writes_nothing_to_the_store():
    """Unauthenticated POST /register must not grow Redis, the session store."""
    store = _DictStore()
    provider = GovernanceOAuthProvider(store=store)
    for i in range(50):
        await provider.register_client(_dcr_client(f"spam-{i}"))
    assert store.data == {}


@pytest.mark.asyncio
async def test_a_client_is_persisted_once_it_gets_a_token():
    store = _DictStore()
    provider = GovernanceOAuthProvider(store=store)
    client = _dcr_client()
    await provider.register_client(client)
    await _sign_in(provider, client)
    assert f"client:{client.client_id}" in store.data


class _BrokenStore(_DictStore):
    async def set(self, key, value, ttl):
        return False

    async def delete(self, *keys):
        return False


@pytest.mark.asyncio
async def test_a_revocation_that_misses_the_store_is_logged(caplog):
    """Swallowing it would silently undo the revocation once the store is back."""
    import logging

    provider = GovernanceOAuthProvider(store=_BrokenStore())
    client = _dcr_client()
    await provider.register_client(client)
    tokens = await _sign_in(provider, client)
    access = await provider.load_access_token(tokens.access_token)
    with caplog.at_level(logging.ERROR, logger="src.oauth_provider"):
        await provider.revoke_token(access)
    assert any("did NOT reach the store" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "raw,expected",
    [(None, True), ("true", True), ("1", True), ("false", False), ("0", False), ("nope", False)],
)
def test_dynamic_registration_switch(monkeypatch, raw, expected):
    from src.mcp_listen_config import oauth_dynamic_registration_enabled

    if raw is None:
        monkeypatch.delenv("UNITARES_OAUTH_DYNAMIC_REGISTRATION", raising=False)
    else:
        monkeypatch.setenv("UNITARES_OAUTH_DYNAMIC_REGISTRATION", raw)
    assert oauth_dynamic_registration_enabled() is expected


def test_with_registration_closed_there_is_no_register_route():
    from mcp.server.auth.routes import create_auth_routes
    from mcp.server.auth.settings import ClientRegistrationOptions
    from pydantic import AnyHttpUrl
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    provider = GovernanceOAuthProvider(
        static_clients=[build_static_client("google", "s3cret", ["https://example.org/cb"])]
    )
    app = Starlette(routes=create_auth_routes(
        provider,
        issuer_url=AnyHttpUrl("https://gov.example.org"),
        client_registration_options=ClientRegistrationOptions(enabled=False),
    ))
    client = TestClient(app)
    resp = client.post("/register", json={"redirect_uris": ["https://x.example/cb"]})
    assert resp.status_code in (404, 405)
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert "registration_endpoint" not in meta


# --------------------------------------------------------------------------- #
# Refresh over HTTP, through the SDK's own token handler
# --------------------------------------------------------------------------- #


def _http_app(provider):
    from mcp.server.auth.routes import create_auth_routes
    from mcp.server.auth.settings import ClientRegistrationOptions
    from pydantic import AnyHttpUrl
    from starlette.applications import Starlette

    return Starlette(routes=create_auth_routes(
        provider,
        issuer_url=AnyHttpUrl("https://gov.example.org"),
        client_registration_options=ClientRegistrationOptions(enabled=False),
    ))


def _http_sign_in(client_http, client_id, secret, redirect):
    import base64
    import hashlib
    from urllib.parse import parse_qs, urlparse

    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    resp = client_http.get(
        "/authorize",
        params={
            "response_type": "code", "client_id": client_id, "redirect_uri": redirect,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "s",
        },
        follow_redirects=False,
    )
    code = parse_qs(urlparse(resp.headers["location"]).query)["code"][0]
    resp = client_http.post("/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
        "code_verifier": verifier, "client_id": client_id, "client_secret": secret,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def _http_refresh(client_http, client_id, secret, refresh_token):
    return client_http.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": client_id, "client_secret": secret,
    })


def test_refresh_grant_works_over_http():
    """The SDK handler reads refresh_token.expires_at; without it every
    refresh was a 500 and connectors were signed out hourly."""
    from starlette.testclient import TestClient

    redirect = "https://example.org/cb"
    static = build_static_client("google", "s3cret", [redirect])
    http = TestClient(_http_app(GovernanceOAuthProvider(static_clients=[static])))
    tokens = _http_sign_in(http, "google", "s3cret", redirect)
    resp = _http_refresh(http, "google", "s3cret", tokens["refresh_token"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"] != tokens["access_token"]
    # single use
    assert _http_refresh(http, "google", "s3cret", tokens["refresh_token"]).status_code == 400


def test_refresh_grant_works_over_http_after_a_restart():
    from starlette.testclient import TestClient

    redirect = "https://example.org/cb"
    static = build_static_client("google", "s3cret", [redirect])
    store = _DictStore()
    before = TestClient(_http_app(GovernanceOAuthProvider(static_clients=[static], store=store)))
    tokens = _http_sign_in(before, "google", "s3cret", redirect)

    after = TestClient(_http_app(GovernanceOAuthProvider(static_clients=[static], store=store)))
    resp = _http_refresh(after, "google", "s3cret", tokens["refresh_token"])
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_an_expired_persisted_refresh_token_is_refused():
    store = _DictStore()
    before = GovernanceOAuthProvider(store=store, refresh_token_ttl=604800)
    client = _dcr_client()
    await before.register_client(client)
    tokens = await _sign_in(before, client)
    key = next(k for k in store.data if k.startswith("rt:"))
    data = json.loads(store.data[key])
    data["created_at"] = time.time() - 604801
    store.data[key] = json.dumps(data)

    after = GovernanceOAuthProvider(store=store, refresh_token_ttl=604800)
    assert await after.load_refresh_token(client, tokens.refresh_token) is None


@pytest.mark.asyncio
async def test_concurrent_refreshes_with_one_token_mint_one_pair():
    """load_refresh_token awaits the store, so both requests can pass it;
    only one may exchange."""
    import asyncio

    from mcp.server.auth.provider import TokenError

    store = _DictStore()
    provider = GovernanceOAuthProvider(store=store)
    client = _dcr_client()
    await provider.register_client(client)
    tokens = await _sign_in(provider, client)

    first = await provider.load_refresh_token(client, tokens.refresh_token)
    second = await provider.load_refresh_token(client, tokens.refresh_token)
    assert first is not None and second is not None

    results = await asyncio.gather(
        provider.exchange_refresh_token(client, first, []),
        provider.exchange_refresh_token(client, second, []),
        return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, TokenError)]
    assert len(ok) == 1 and len(refused) == 1
    assert refused[0].error == "invalid_grant"


class _SlowDeleteStore(_DictStore):
    """A store whose delete yields, as Redis does, so the gap between the
    in-memory claim and the store delete is real."""

    async def delete(self, *keys):
        import asyncio

        await asyncio.sleep(0.01)
        return await super().delete(*keys)


@pytest.mark.asyncio
async def test_a_refresh_arriving_during_the_store_delete_is_refused():
    import asyncio

    from mcp.server.auth.provider import TokenError

    store = _SlowDeleteStore()
    provider = GovernanceOAuthProvider(store=store)
    client = _dcr_client()
    await provider.register_client(client)
    tokens = await _sign_in(provider, client)

    async def refresh(delay):
        await asyncio.sleep(delay)
        entry = await provider.load_refresh_token(client, tokens.refresh_token)
        if entry is None:
            return "refused"
        try:
            await provider.exchange_refresh_token(client, entry, [])
            return "minted"
        except TokenError:
            return "refused"

    results = await asyncio.gather(refresh(0), refresh(0.005))
    assert sorted(results) == ["minted", "refused"]


class _FailingDeleteStore(_DictStore):
    async def delete(self, *keys):
        return False


@pytest.mark.asyncio
async def test_a_used_refresh_token_stays_refused_when_the_store_delete_fails(caplog):
    import logging

    store = _FailingDeleteStore()
    provider = GovernanceOAuthProvider(store=store)
    client = _dcr_client()
    await provider.register_client(client)
    tokens = await _sign_in(provider, client)
    entry = await provider.load_refresh_token(client, tokens.refresh_token)
    with caplog.at_level(logging.ERROR, logger="src.oauth_provider"):
        await provider.exchange_refresh_token(client, entry, [])
    assert any("could NOT be deleted" in r.getMessage() for r in caplog.records)
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None
