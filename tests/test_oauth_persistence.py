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
            return
        self.data[key] = value
        self.ttls[key] = ttl

    async def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)


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
