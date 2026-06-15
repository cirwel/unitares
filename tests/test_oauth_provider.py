"""Regression tests for src/oauth_provider.py.

GovernanceOAuthProvider is the in-memory OAuth 2.1 authorization server backing
the MCP transport. It had no direct test coverage despite being security-
sensitive: it mints bearer/refresh tokens, scopes them, enforces one-time-use
authorization codes, binds codes/tokens to a client_id, and expires/revokes
credentials. These tests pin that contract so a regression that (e.g.) lets a
code be redeemed twice, skips the client binding, or fails to rotate a refresh
token is caught immediately.

All provider methods are async; the SDK runs in pytest-asyncio STRICT mode so
each coroutine test is explicitly marked.
"""

from __future__ import annotations

import time

import pytest

from mcp.server.auth.provider import AccessToken, AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from src.oauth_provider import (
    GovernanceOAuthProvider,
    AuthCodeEntry,
    RefreshTokenEntry,
)


@pytest.fixture
def provider():
    return GovernanceOAuthProvider(secret="test-secret")


def _client(client_id="client-1", redirect="https://app.example/cb"):
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=[redirect],
    )


def _auth_params(redirect="https://app.example/cb", state="state-123",
                 scopes=None, resource=None):
    return AuthorizationParams(
        state=state,
        scopes=scopes if scopes is not None else ["mcp:tools"],
        code_challenge="challenge-abc",
        redirect_uri=redirect,
        redirect_uri_provided_explicitly=True,
        resource=resource,
    )


# --------------------------------------------------------------------------- #
# Dataclass entries
# --------------------------------------------------------------------------- #

class TestEntryExpiry:
    def test_auth_code_not_expired_when_fresh(self):
        entry = AuthCodeEntry(code="c", client_id="x", redirect_uri="u",
                              code_challenge="ch", scopes=[])
        assert entry.is_expired(ttl=300) is False

    def test_auth_code_expired_after_ttl(self):
        entry = AuthCodeEntry(code="c", client_id="x", redirect_uri="u",
                              code_challenge="ch", scopes=[])
        entry.created_at = time.time() - 301
        assert entry.is_expired(ttl=300) is True

    def test_refresh_token_expiry_boundary(self):
        entry = RefreshTokenEntry(token="rt", client_id="x", scopes=[])
        assert entry.is_expired(ttl=604800) is False
        entry.created_at = time.time() - 604801
        assert entry.is_expired(ttl=604800) is True


# --------------------------------------------------------------------------- #
# Client registration
# --------------------------------------------------------------------------- #

class TestRegisterClient:
    @pytest.mark.asyncio
    async def test_fills_in_missing_id_and_secret(self, provider):
        client = OAuthClientInformationFull(redirect_uris=["https://app.example/cb"])
        assert client.client_id is None
        await provider.register_client(client)
        assert client.client_id.startswith("unitares_")
        assert client.client_secret
        assert client.client_id_issued_at
        # registered and retrievable
        assert await provider.get_client(client.client_id) is client

    @pytest.mark.asyncio
    async def test_preserves_provided_id(self, provider):
        client = _client(client_id="my-fixed-id")
        await provider.register_client(client)
        assert client.client_id == "my-fixed-id"
        assert await provider.get_client("my-fixed-id") is client

    @pytest.mark.asyncio
    async def test_unknown_client_is_none(self, provider):
        assert await provider.get_client("nope") is None


# --------------------------------------------------------------------------- #
# Authorization code issuance + redirect
# --------------------------------------------------------------------------- #

class TestAuthorize:
    @pytest.mark.asyncio
    async def test_redirect_includes_code_and_state(self, provider):
        client = _client()
        redirect = await provider.authorize(client, _auth_params(state="st-9"))
        assert "code=" in redirect
        assert "state=st-9" in redirect
        assert redirect.startswith("https://app.example/cb?")

    @pytest.mark.asyncio
    async def test_uses_ampersand_when_redirect_has_query(self, provider):
        client = _client(redirect="https://app.example/cb?foo=bar")
        params = _auth_params(redirect="https://app.example/cb?foo=bar")
        redirect = await provider.authorize(client, params)
        assert "?foo=bar&code=" in redirect

    @pytest.mark.asyncio
    async def test_authorize_then_load_roundtrips(self, provider):
        client = _client()
        redirect = await provider.authorize(client, _auth_params())
        code = redirect.split("code=")[1].split("&")[0]
        entry = await provider.load_authorization_code(client, code)
        assert entry is not None
        assert entry.client_id == client.client_id
        assert entry.code_challenge == "challenge-abc"


# --------------------------------------------------------------------------- #
# Authorization code loading: client binding + expiry
# --------------------------------------------------------------------------- #

class TestLoadAuthorizationCode:
    @pytest.mark.asyncio
    async def test_unknown_code_is_none(self, provider):
        assert await provider.load_authorization_code(_client(), "missing") is None

    @pytest.mark.asyncio
    async def test_code_bound_to_issuing_client(self, provider):
        client_a = _client(client_id="A")
        redirect = await provider.authorize(client_a, _auth_params())
        code = redirect.split("code=")[1].split("&")[0]
        # A different client must not be able to load A's code.
        client_b = _client(client_id="B")
        assert await provider.load_authorization_code(client_b, code) is None
        # but the issuing client can.
        assert await provider.load_authorization_code(client_a, code) is not None

    @pytest.mark.asyncio
    async def test_expired_code_is_dropped(self, provider):
        client = _client()
        redirect = await provider.authorize(client, _auth_params())
        code = redirect.split("code=")[1].split("&")[0]
        provider._auth_codes[code].created_at = time.time() - 10_000
        assert await provider.load_authorization_code(client, code) is None
        # and purged from storage
        assert code not in provider._auth_codes


# --------------------------------------------------------------------------- #
# Authorization code exchange
# --------------------------------------------------------------------------- #

class TestExchangeAuthorizationCode:
    async def _issue_code(self, provider, client, **kw):
        redirect = await provider.authorize(client, _auth_params(**kw))
        code = redirect.split("code=")[1].split("&")[0]
        return await provider.load_authorization_code(client, code)

    @pytest.mark.asyncio
    async def test_issues_access_and_refresh_tokens(self, provider):
        client = _client()
        entry = await self._issue_code(provider, client)
        token = await provider.exchange_authorization_code(client, entry)
        assert token.access_token.startswith("at_")
        assert token.refresh_token.startswith("rt_")
        assert token.token_type == "Bearer"
        assert token.scope == "mcp:tools"
        # access token is loadable and bound to the client
        loaded = await provider.load_access_token(token.access_token)
        assert loaded is not None
        assert loaded.client_id == client.client_id

    @pytest.mark.asyncio
    async def test_code_is_single_use(self, provider):
        client = _client()
        entry = await self._issue_code(provider, client)
        await provider.exchange_authorization_code(client, entry)
        # The code must be consumed — a replay can no longer be loaded.
        assert await provider.load_authorization_code(client, entry.code) is None

    @pytest.mark.asyncio
    async def test_empty_scopes_default_to_mcp_tools(self, provider):
        client = _client()
        entry = await self._issue_code(provider, client, scopes=[])
        token = await provider.exchange_authorization_code(client, entry)
        loaded = await provider.load_access_token(token.access_token)
        assert loaded.scopes == ["mcp:tools"]


# --------------------------------------------------------------------------- #
# Refresh token rotation
# --------------------------------------------------------------------------- #

class TestRefreshFlow:
    async def _full_token(self, provider, client, **kw):
        redirect = await provider.authorize(client, _auth_params(**kw))
        code = redirect.split("code=")[1].split("&")[0]
        entry = await provider.load_authorization_code(client, code)
        return await provider.exchange_authorization_code(client, entry)

    @pytest.mark.asyncio
    async def test_load_refresh_token_client_binding(self, provider):
        client = _client(client_id="A")
        token = await self._full_token(provider, client)
        assert await provider.load_refresh_token(client, token.refresh_token) is not None
        other = _client(client_id="B")
        assert await provider.load_refresh_token(other, token.refresh_token) is None

    @pytest.mark.asyncio
    async def test_exchange_rotates_refresh_token(self, provider):
        client = _client()
        token = await self._full_token(provider, client)
        rt_entry = await provider.load_refresh_token(client, token.refresh_token)
        new_token = await provider.exchange_refresh_token(client, rt_entry, scopes=[])
        # new access + refresh issued
        assert new_token.access_token.startswith("at_")
        assert new_token.refresh_token != token.refresh_token
        # old refresh token is consumed (rotation)
        assert await provider.load_refresh_token(client, token.refresh_token) is None
        # new refresh token works
        assert await provider.load_refresh_token(client, new_token.refresh_token) is not None

    @pytest.mark.asyncio
    async def test_requested_scopes_override(self, provider):
        client = _client()
        token = await self._full_token(provider, client)
        rt_entry = await provider.load_refresh_token(client, token.refresh_token)
        new_token = await provider.exchange_refresh_token(
            client, rt_entry, scopes=["mcp:tools", "extra"])
        loaded = await provider.load_access_token(new_token.access_token)
        assert loaded.scopes == ["mcp:tools", "extra"]


# --------------------------------------------------------------------------- #
# Access token loading + expiry + revocation
# --------------------------------------------------------------------------- #

class TestAccessTokenLifecycle:
    @pytest.mark.asyncio
    async def test_unknown_access_token_is_none(self, provider):
        assert await provider.load_access_token("at_missing") is None

    @pytest.mark.asyncio
    async def test_expired_access_token_dropped(self, provider):
        client = _client()
        provider._access_tokens["at_x"] = AccessToken(
            token="at_x", client_id=client.client_id, scopes=["mcp:tools"],
            expires_at=int(time.time()) - 5,
        )
        assert await provider.load_access_token("at_x") is None
        assert "at_x" not in provider._access_tokens

    @pytest.mark.asyncio
    async def test_get_token_client_id(self, provider):
        provider._access_tokens["at_y"] = AccessToken(
            token="at_y", client_id="owner-1", scopes=["mcp:tools"],
            expires_at=int(time.time()) + 3600,
        )
        assert provider.get_token_client_id("at_y") == "owner-1"
        assert provider.get_token_client_id("nope") is None

    @pytest.mark.asyncio
    async def test_revoke_access_token_clears_client_refresh_tokens(self, provider):
        client = _client()
        redirect = await provider.authorize(client, _auth_params())
        code = redirect.split("code=")[1].split("&")[0]
        entry = await provider.load_authorization_code(client, code)
        token = await provider.exchange_authorization_code(client, entry)
        access = await provider.load_access_token(token.access_token)

        await provider.revoke_token(access)
        # access token gone
        assert await provider.load_access_token(token.access_token) is None
        # associated refresh token(s) for that client also cleared
        assert await provider.load_refresh_token(client, token.refresh_token) is None

    @pytest.mark.asyncio
    async def test_revoke_refresh_token_entry(self, provider):
        client = _client()
        redirect = await provider.authorize(client, _auth_params())
        code = redirect.split("code=")[1].split("&")[0]
        entry = await provider.load_authorization_code(client, code)
        token = await provider.exchange_authorization_code(client, entry)
        rt_entry = await provider.load_refresh_token(client, token.refresh_token)

        await provider.revoke_token(rt_entry)
        assert await provider.load_refresh_token(client, token.refresh_token) is None
