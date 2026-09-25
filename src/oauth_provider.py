"""
OAuth 2.1 Authorization Server Provider for unitares-governance.

Implements the MCP SDK's OAuthAuthorizationServerProvider protocol.
State lives in memory and, when a store is given (Redis in production), is
written through so issued tokens and DCR registrations survive a restart: a
connector signed in before a restart stays signed in after it. Authorization
codes stay in memory only (five-minute lifetime; a restart mid-sign-in just
means signing in again).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlencode, urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    OAuthToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull
from starlette.responses import JSONResponse


@dataclass
class AuthCodeEntry:
    """Stored authorization code with metadata."""
    code: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    scopes: list[str]
    redirect_uri_provided_explicitly: bool = True
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    resource: str | None = None

    def is_expired(self, ttl: int = 300) -> bool:
        return time.time() > self.created_at + ttl


@dataclass
class RefreshTokenEntry:
    """Stored refresh token with metadata."""
    token: str
    client_id: str
    scopes: list[str]
    created_at: float = field(default_factory=time.time)
    #: Read by the SDK's token handler on every refresh grant; without it the
    #: grant raised AttributeError (HTTP 500) and no connector could refresh.
    expires_at: int | None = None

    def is_expired(self, ttl: int = 604800) -> bool:
        return time.time() > self.created_at + ttl


logger = logging.getLogger(__name__)

#: A DCR registration is written to the store only when a token is issued
#: to it, then expires unless another token is issued within this window.
#: POST /register alone writes nothing; with registration open and sign-in
#: auto-approved, anyone can still obtain a token (and so a stored client),
#: which only UNITARES_OAUTH_DYNAMIC_REGISTRATION=false bounds.
CLIENT_STATE_TTL = 30 * 86400


class OAuthStateStore:
    """Key/value store for OAuth state. Every method must fail soft: a store
    that is down degrades the provider to memory-only, never to an error."""

    async def get(self, key: str) -> str | None:  # pragma: no cover - interface
        raise NotImplementedError

    async def set(self, key: str, value: str, ttl: int) -> bool:  # pragma: no cover
        """Write; True if it landed."""
        raise NotImplementedError

    async def delete(self, *keys: str) -> bool:  # pragma: no cover
        """Delete; True if it landed."""
        raise NotImplementedError


class RedisOAuthStore(OAuthStateStore):
    """OAuth state in Redis, the server's session store, with per-key TTLs.

    Keys hold a SHA-256 of each token, never the token itself. Every call is
    bounded by ``asyncio.wait_for`` (async Redis is not ExecutorPool-wrapped)
    and any failure reads as a miss.
    """

    PREFIX = "unitares:oauth:"

    def __init__(self, timeout: float = 1.0):
        self._timeout = timeout

    async def _redis(self):
        from src.cache.redis_client import get_redis

        return await asyncio.wait_for(get_redis(), timeout=self._timeout)

    async def get(self, key: str) -> str | None:
        try:
            redis = await self._redis()
            if redis is None:
                return None
            return await asyncio.wait_for(redis.get(self.PREFIX + key), timeout=self._timeout)
        except Exception as exc:
            logger.warning("OAuth store read failed (%s); using memory only", type(exc).__name__)
            return None

    async def set(self, key: str, value: str, ttl: int) -> bool:
        if ttl <= 0:
            return True
        try:
            redis = await self._redis()
            if redis is None:
                return False
            await asyncio.wait_for(
                redis.set(self.PREFIX + key, value, ex=int(ttl)), timeout=self._timeout
            )
            return True
        except Exception as exc:
            logger.warning("OAuth store write failed (%s); state is memory-only", type(exc).__name__)
            return False

    async def delete(self, *keys: str) -> bool:
        if not keys:
            return True
        try:
            redis = await self._redis()
            if redis is None:
                return False
            await asyncio.wait_for(
                redis.delete(*(self.PREFIX + k for k in keys)), timeout=self._timeout
            )
            return True
        except Exception as exc:
            logger.warning("OAuth store delete failed (%s)", type(exc).__name__)
            return False


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class GovernanceOAuthProvider(OAuthAuthorizationServerProvider):
    """
    OAuth 2.1 Authorization Server for unitares-governance.

    Implements OAuthAuthorizationServerProvider protocol from the MCP SDK.
    Single-user, personal server — optimized for simplicity. With a ``store``,
    tokens and DCR clients are written through and loaded back on a miss.
    """

    def __init__(
        self,
        secret: str | None = None,
        auto_approve: bool = True,
        access_token_ttl: int = 3600,
        refresh_token_ttl: int = 604800,
        auth_code_ttl: int = 300,
        static_clients: list[OAuthClientInformationFull] | None = None,
        store: OAuthStateStore | None = None,
    ):
        self._secret = secret or secrets.token_hex(32)
        self._auto_approve = auto_approve
        self._access_token_ttl = access_token_ttl
        self._refresh_token_ttl = refresh_token_ttl
        self._auth_code_ttl = auth_code_ttl

        # Pre-registered clients survive restarts because they come from the
        # environment, not from DCR. Connectors that cannot self-register
        # (Google's custom MCP connector asks for a client ID and secret) need one.
        self._clients: dict[str, OAuthClientInformationFull] = {
            c.client_id: c for c in (static_clients or []) if c.client_id
        }
        self._auth_codes: dict[str, AuthCodeEntry] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshTokenEntry] = {}
        self._store = store
        self._static_client_ids = frozenset(self._clients)
        # Digests of refresh tokens already exchanged, with their expiry. The
        # store delete is awaited (and can fail), so without this a request
        # arriving meanwhile would reload the used token from the store.
        self._consumed_refresh: dict[str, float] = {}

    # -- persistence helpers ------------------------------------------------

    async def _persist_client(self, client: OAuthClientInformationFull) -> None:
        if self._store is not None and client.client_id not in self._static_client_ids:
            await self._store.set(
                f"client:{client.client_id}", client.model_dump_json(), CLIENT_STATE_TTL
            )

    async def _persist_access(self, entry: AccessToken) -> None:
        if self._store is None:
            return
        ttl = (entry.expires_at - int(time.time())) if entry.expires_at else self._access_token_ttl
        await self._store.set(
            f"at:{_digest(entry.token)}",
            entry.model_dump_json(exclude={"token"}),
            ttl,
        )

    async def _persist_refresh(self, entry: RefreshTokenEntry) -> None:
        if self._store is None:
            return
        ttl = int(entry.created_at + self._refresh_token_ttl - time.time())
        await self._store.set(
            f"rt:{_digest(entry.token)}",
            json.dumps({
                "client_id": entry.client_id,
                "scopes": list(entry.scopes),
                "created_at": entry.created_at,
            }),
            ttl,
        )

    async def _issued(self, client: OAuthClientInformationFull,
                      access: AccessToken, refresh: RefreshTokenEntry) -> None:
        await self._persist_access(access)
        await self._persist_refresh(refresh)
        # A client that just got a token is in use; keep its registration.
        await self._persist_client(client)

    def _new_refresh(self, token: str, client_id: str, scopes: list[str]) -> RefreshTokenEntry:
        now = time.time()
        return RefreshTokenEntry(
            token=token,
            client_id=client_id,
            scopes=scopes,
            created_at=now,
            expires_at=int(now + self._refresh_token_ttl),
        )

    def _generate_token(self, prefix: str = "at") -> str:
        """Generate a cryptographically random token."""
        raw = secrets.token_hex(32)
        return f"{prefix}_{raw}"

    def get_token_client_id(self, token: str) -> str | None:
        """Get the client_id associated with a bearer token, or None."""
        at = self._access_tokens.get(token)
        return at.client_id if at and hasattr(at, "client_id") else None

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        client = self._clients.get(client_id)
        if client is None and self._store is not None:
            raw = await self._store.get(f"client:{client_id}")
            if raw:
                try:
                    client = OAuthClientInformationFull.model_validate_json(raw)
                except ValueError:
                    return None
                self._clients[client_id] = client
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            client_info.client_id = f"unitares_{secrets.token_hex(16)}"
        if not client_info.client_secret:
            client_info.client_secret = secrets.token_hex(32)
        if not client_info.client_id_issued_at:
            client_info.client_id_issued_at = int(time.time())
        # Memory only until a token is issued to it (see CLIENT_STATE_TTL).
        self._clients[client_info.client_id] = client_info

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams,
    ) -> str:
        code = secrets.token_hex(24)

        entry = AuthCodeEntry(
            code=code,
            client_id=client.client_id,
            redirect_uri=str(params.redirect_uri),
            code_challenge=params.code_challenge,
            scopes=params.scopes or [],
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            expires_at=time.time() + self._auth_code_ttl,
            resource=params.resource,
        )
        self._auth_codes[code] = entry

        query = {"code": code}
        if params.state:
            query["state"] = params.state
        redirect = str(params.redirect_uri)
        separator = "&" if "?" in redirect else "?"
        return f"{redirect}{separator}{urlencode(query)}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str,
    ) -> AuthCodeEntry | None:
        entry = self._auth_codes.get(authorization_code)
        if entry is None:
            return None
        if entry.client_id != client.client_id:
            return None
        if entry.is_expired(self._auth_code_ttl):
            del self._auth_codes[authorization_code]
            return None
        return entry

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthCodeEntry,
    ) -> OAuthToken:
        self._auth_codes.pop(authorization_code.code, None)

        access_token_str = self._generate_token("at")
        refresh_token_str = self._generate_token("rt")
        expires_at = int(time.time()) + self._access_token_ttl

        self._access_tokens[access_token_str] = AccessToken(
            token=access_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes or ["mcp:tools"],
            expires_at=expires_at,
            resource=authorization_code.resource,
        )

        self._refresh_tokens[refresh_token_str] = self._new_refresh(
            refresh_token_str, client.client_id, authorization_code.scopes or ["mcp:tools"]
        )
        await self._issued(
            client,
            self._access_tokens[access_token_str],
            self._refresh_tokens[refresh_token_str],
        )

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=self._access_token_ttl,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else "mcp:tools",
            refresh_token=refresh_token_str,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str,
    ) -> RefreshTokenEntry | None:
        if _digest(refresh_token) in self._consumed_refresh:
            return None
        entry = self._refresh_tokens.get(refresh_token)
        if entry is None and self._store is not None and refresh_token.startswith("rt_"):
            raw = await self._store.get(f"rt:{_digest(refresh_token)}")
            if raw:
                try:
                    data = json.loads(raw)
                    created = float(data["created_at"])
                    entry = RefreshTokenEntry(
                        token=refresh_token,
                        client_id=str(data["client_id"]),
                        scopes=list(data.get("scopes") or []),
                        created_at=created,
                        expires_at=int(created + self._refresh_token_ttl),
                    )
                except (ValueError, KeyError, TypeError):
                    return None
                self._refresh_tokens[refresh_token] = entry
        if entry is None:
            return None
        if entry.client_id != client.client_id:
            return None
        if entry.is_expired(self._refresh_token_ttl) or await self._revoked_after(entry):
            self._refresh_tokens.pop(refresh_token, None)
            if self._store is not None:
                await self._store.delete(f"rt:{_digest(refresh_token)}")
            return None
        return entry

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshTokenEntry,
        scopes: list[str],
    ) -> OAuthToken:
        # Claim before any await: load_refresh_token awaits the store, so two
        # requests with the same token can both pass it. Only the one that
        # pops the entry proceeds; the other gets invalid_grant. Recording the
        # digest as consumed (also before any await) stops a later load from
        # reloading the token from the store while, or if, its delete lags.
        digest = _digest(refresh_token.token)
        if (
            self._refresh_tokens.pop(refresh_token.token, None) is None
            or digest in self._consumed_refresh
        ):
            raise TokenError(
                error="invalid_grant", error_description="refresh token already used"
            )
        now = time.time()
        self._consumed_refresh = {
            d: exp for d, exp in self._consumed_refresh.items() if exp > now
        }
        self._consumed_refresh[digest] = refresh_token.created_at + self._refresh_token_ttl
        if self._store is not None and not await self._store.delete(f"rt:{digest}"):
            # This process refuses it (consumed set); another process, or this
            # one after a restart, would not until it expires.
            logger.error(
                "A used refresh token could NOT be deleted from the store; it "
                "stays redeemable after a restart until it expires"
            )

        access_token_str = self._generate_token("at")
        new_refresh_str = self._generate_token("rt")
        expires_at = int(time.time()) + self._access_token_ttl
        effective_scopes = scopes or refresh_token.scopes or ["mcp:tools"]

        self._access_tokens[access_token_str] = AccessToken(
            token=access_token_str,
            client_id=client.client_id,
            scopes=effective_scopes,
            expires_at=expires_at,
        )

        self._refresh_tokens[new_refresh_str] = self._new_refresh(
            new_refresh_str, client.client_id, effective_scopes
        )
        await self._issued(
            client,
            self._access_tokens[access_token_str],
            self._refresh_tokens[new_refresh_str],
        )

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=self._access_token_ttl,
            scope=" ".join(effective_scopes),
            refresh_token=new_refresh_str,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        entry = self._access_tokens.get(token)
        # Every bearer on every request reaches this (the SDK's auth middleware
        # and the /mcp gate), including static allowlist and REST tokens that
        # were never stored; only a token this provider minted can be there.
        if entry is None and self._store is not None and token.startswith("at_"):
            raw = await self._store.get(f"at:{_digest(token)}")
            if raw:
                try:
                    entry = AccessToken.model_validate({**json.loads(raw), "token": token})
                except (ValueError, TypeError):
                    return None
                self._access_tokens[token] = entry
        if entry is None:
            return None
        if entry.expires_at and entry.expires_at < int(time.time()):
            self._access_tokens.pop(token, None)
            return None
        return entry

    async def _revoked_after(self, entry: RefreshTokenEntry) -> bool:
        """True if the client's refresh tokens were revoked after this one was
        issued. A marker, because a restarted process cannot enumerate the
        persisted refresh tokens it has not loaded."""
        if self._store is None:
            return False
        raw = await self._store.get(f"revoked:{entry.client_id}")
        try:
            return raw is not None and entry.created_at <= float(raw)
        except ValueError:
            return False

    async def revoke_token(self, token: AccessToken | RefreshTokenEntry) -> None:
        if isinstance(token, AccessToken):
            self._access_tokens.pop(token.token, None)
            to_remove = [k for k, v in self._refresh_tokens.items()
                         if v.client_id == token.client_id]
            for k in to_remove:
                del self._refresh_tokens[k]
            if self._store is not None:
                # Marker first: it is what refuses the client's persisted refresh
                # tokens this process never loaded.
                marked = await self._store.set(
                    f"revoked:{token.client_id}", repr(time.time()), self._refresh_token_ttl
                )
                deleted = await self._store.delete(
                    f"at:{_digest(token.token)}", *(f"rt:{_digest(k)}" for k in to_remove)
                )
                if not (marked and deleted):
                    logger.error(
                        "OAuth revocation for a client did NOT reach the store; its "
                        "persisted tokens stay valid until they expire or the "
                        "revocation is repeated"
                    )
        elif isinstance(token, RefreshTokenEntry):
            self._refresh_tokens.pop(token.token, None)
            if self._store is not None and not await self._store.delete(
                f"rt:{_digest(token.token)}"
            ):
                logger.error(
                    "OAuth refresh-token revocation did NOT reach the store; the "
                    "token stays valid until it expires or is revoked again"
                )


def build_static_client(
    client_id: str,
    client_secret: str,
    redirect_uris: list[str],
) -> OAuthClientInformationFull:
    """Build a pre-registered confidential client for a non-DCR connector.

    Registered as ``client_secret_post``; :class:`StaticClientBasicAuthShim`
    lets the same client authenticate with HTTP Basic, since the connector's
    choice between the two is not documented.
    """
    if not client_id or not client_secret:
        raise ValueError("static OAuth client needs both a client_id and a client_secret")
    if not redirect_uris:
        raise ValueError("static OAuth client needs at least one redirect URI")
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret=client_secret,
        client_id_issued_at=int(time.time()),
        redirect_uris=redirect_uris,
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="mcp:tools",
    )


def static_clients_from_env() -> list[OAuthClientInformationFull]:
    """Build the static client from the environment, or none if unconfigured.

    Any one of the three variables set means the operator asked for a static
    client, so an incomplete set raises (failing OAuth setup, which closes the
    gated route) instead of silently registering nothing.
    """
    client_id = os.environ.get("UNITARES_OAUTH_STATIC_CLIENT_ID", "")
    client_secret = os.environ.get("UNITARES_OAUTH_STATIC_CLIENT_SECRET", "")
    redirects = os.environ.get("UNITARES_OAUTH_STATIC_REDIRECT_URIS", "")
    if not (client_id or client_secret or redirects):
        return []
    return [
        build_static_client(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uris=[u.strip() for u in redirects.split(",") if u.strip()],
        )
    ]


class StaticClientBasicAuthShim:
    """Compatibility rewrites for the static client only, ahead of the SDK.

    ``POST /token``: HTTP Basic client auth is moved into form fields. The SDK
    authenticates a client only by its registered method, and its Basic path
    still requires ``client_id`` in the form body, which RFC 6749 §2.3.1
    clients commonly omit. Either style then reaches the
    ``client_secret_post`` check.

    With ``pkce_verifier`` set, two more rewrites let a confidential connector
    that does not implement PKCE (the SDK requires it on every flow) sign in:

    - ``/authorize`` without ``code_challenge`` gets one derived from the
      server-held verifier, and requested scopes are narrowed to
      ``mcp:tools`` (dropped if nothing remains) instead of failing with
      ``invalid_scope``.
    - ``POST /token`` for an authorization code without ``code_verifier``
      gets that verifier.

    PKCE guards a public client's code in transit; this client must present
    its secret to redeem a code, which OAuth 2.0 accepts in place of PKCE for
    confidential clients. A request's own PKCE is never altered; scope
    narrowing applies to every static-client ``/authorize`` and
    refresh-token request. Every other client passes through untouched.
    """

    def __init__(self, app, *, client_id: str, pkce_verifier: str | None = None,
                 path: str = "/token", authorize_path: str = "/authorize"):
        self.app = app
        self._client_id = client_id
        self._verifier = pkce_verifier
        self._challenge = _s256(pkce_verifier) if pkce_verifier else None
        self._path = path
        self._authorize_path = authorize_path

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path, method = scope.get("path"), scope.get("method")
        if self._verifier and path == self._authorize_path and method == "GET":
            fields = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
            if dict(fields).get("client_id") == self._client_id:
                fields = self._authorize_fields(fields)
                scope = {**scope, "query_string": urlencode(fields).encode()}
            await self.app(scope, receive, send)
            return
        if method != "POST" or path not in (self._path, self._authorize_path):
            await self.app(scope, receive, send)
            return
        creds = _basic_credentials(scope) if path == self._path else None
        if path == self._authorize_path and not self._verifier:
            await self.app(scope, receive, send)
            return
        if path == self._path and creds is None and not self._verifier:
            await self.app(scope, receive, send)
            return
        if creds is not None and creds[0] != self._client_id:
            await self.app(scope, receive, send)
            return

        body, early = await _read_body(receive, _MAX_TOKEN_BODY)
        if early is not None:
            if early == "too_large":
                await JSONResponse(
                    {"error": "invalid_request", "error_description": "request body too large"},
                    status_code=413,
                )(scope, receive, send)
            else:
                # Client went away before the body finished; nothing to rewrite.
                await self.app(scope, _replay([{"type": "http.disconnect"}]), send)
            return

        fields = parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        values = dict(fields)
        client = creds[0] if creds is not None else values.get("client_id")
        if client != self._client_id:
            await self.app(scope, _replay([_body_message(body)]), send)
            return
        present = set(values)
        if path == self._authorize_path:
            fields = self._authorize_fields(fields)
        else:
            if creds is not None:
                if "client_id" not in present:
                    fields.append(("client_id", creds[0]))
                if "client_secret" not in present:
                    fields.append(("client_secret", creds[1]))
            if (
                self._verifier
                and values.get("grant_type") == "authorization_code"
                and not values.get("code_verifier")
            ):
                fields = [(k, v) for k, v in fields if k != "code_verifier"]
                fields.append(("code_verifier", self._verifier))
            if self._verifier and values.get("grant_type") == "refresh_token":
                # A connector that re-sends its originally requested scope on
                # refresh would otherwise lose the session an hour after linking.
                fields = _narrow_scope(fields)
        new_body = urlencode(fields).encode()

        headers = [
            (k, v)
            for k, v in scope.get("headers", [])
            if k.lower() not in (b"content-length",)
            and not (creds is not None and k.lower() == b"authorization")
        ]
        headers.append((b"content-length", str(len(new_body)).encode()))
        await self.app({**scope, "headers": headers}, _replay([_body_message(new_body)]), send)

    def _authorize_fields(self, fields: list[tuple[str, str]]) -> list[tuple[str, str]]:
        values = dict(fields)
        out = _narrow_scope(fields)
        if not values.get("code_challenge"):
            # Absent or blank: a blank challenge is no PKCE at all.
            out = [(k, v) for k, v in out if k not in ("code_challenge", "code_challenge_method")]
            out += [("code_challenge", self._challenge), ("code_challenge_method", "S256")]
        return out


def _narrow_scope(fields: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep only ``mcp:tools`` from a requested scope; drop the parameter if
    nothing remains (the SDK then uses the client's or grant's scope)."""
    values = dict(fields)
    out = [(k, v) for k, v in fields if k != "scope"]
    kept = [s for s in (values.get("scope") or "").split() if s == "mcp:tools"]
    if kept:
        out.append(("scope", " ".join(kept)))
    return out


def static_pkce_verifier(client_secret: str) -> str:
    """Server-held PKCE verifier for the static client, derived from its
    secret so it is stable across restarts and never leaves the server."""
    digest = hmac.new(client_secret.encode(), b"unitares-static-client-pkce", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def _body_message(body: bytes) -> dict:
    return {"type": "http.request", "body": body, "more_body": False}


async def _read_body(receive, limit: int) -> tuple[bytes, str | None]:
    """Read a request body up to ``limit``. Returns (body, None) or
    (partial, "too_large" | "disconnect")."""
    body = bytearray()
    more = True
    while more:
        message = await receive()
        if message["type"] != "http.request":
            return bytes(body), "disconnect"
        body += message.get("body", b"")
        if len(body) > limit:
            return bytes(body), "too_large"
        more = message.get("more_body", False)
    return bytes(body), None


_OAUTH_LOG_PATHS = ("/authorize", "/token")
_LOG_FIELD_CAP = 120


def _log_safe(value) -> str:
    """Caller-supplied, so quote it (a space or ``->`` inside would fake
    another field), escape control characters (a newline would forge a line)
    and cap the length."""
    text = "-" if value is None else str(value)
    if len(text) > _LOG_FIELD_CAP:
        text = text[:_LOG_FIELD_CAP] + "..."
    return json.dumps(text, ensure_ascii=True)


def _strip_queries(text):
    """Drop URL query strings and userinfo from an error description (the SDK
    echoes an unregistered redirect URI in full)."""
    if not text:
        return text
    text = re.sub(r"//[^/\s'\"@]*@", "//", str(text))
    return re.sub(r"\?[^\s'\"]*", "?...", text)


def _url_for_log(url: str | None, *, host_only: bool = False) -> str:
    """A caller-supplied URL reduced to what is safe to log: no userinfo, no
    query or fragment. Unparseable input is named as such, never raised."""
    if not url:
        return "-"
    try:
        parts = urlparse(url)
        host = parts.netloc.rsplit("@", 1)[-1]
    except ValueError:
        return "unparseable"
    if host_only:
        return host or "-"
    return f"{parts.scheme}://{host}{parts.path}" if parts.scheme else (host + parts.path or "-")


def _attempt_facts(path: str, values: dict, basic) -> dict:
    facts = {
        "client": values.get("client_id") or (basic[0] if basic else None),
        "auth": "basic" if basic else ("post" if "client_secret" in values else "none"),
    }
    if path == "/authorize":
        facts.update(
            pkce="yes" if values.get("code_challenge") else "no",
            scope=values.get("scope") or "-",
            redirect_host=_url_for_log(values.get("redirect_uri"), host_only=True),
            resource=_url_for_log(values.get("resource")),
        )
    else:
        facts.update(
            grant=values.get("grant_type") or "-",
            verifier="yes" if values.get("code_verifier") else "no",
        )
    return facts


async def _peek_body(receive, limit: int):
    """Buffer up to ``limit`` bytes of a request body without consuming it.

    Returns (messages seen, body prefix, whether the whole body was read, a
    receive callable that replays the seen messages and then continues the
    original stream)."""
    seen: list[dict] = []
    body = bytearray()
    complete = False
    while True:
        message = await receive()
        seen.append(message)
        if message["type"] != "http.request":
            break
        body += message.get("body", b"")
        if not message.get("more_body", False):
            complete = True
            break
        if len(body) > limit:
            break
    pending = list(seen)
    exhausted = complete or seen[-1]["type"] != "http.request"

    async def chained():
        if pending:
            return pending.pop(0)
        if exhausted:
            return {"type": "http.disconnect"}
        return await receive()

    return seen, bytes(body), complete and len(body) <= limit, chained


class _LineBudget:
    """At most ``per_window`` [OAUTH] lines per ``window`` seconds, so anyone
    who can reach /authorize or /token cannot grow the log at request rate.
    ``take`` returns None when the line must be dropped, else the number of
    lines dropped since the last one logged."""

    def __init__(self, per_window: int = 60, window: float = 60.0):
        self._per_window = per_window
        self._window = window
        self._start = 0.0
        self._used = 0
        self._dropped = 0

    def take(self) -> int | None:
        now = time.monotonic()
        if now - self._start >= self._window:
            self._start, self._used = now, 0
        if self._used >= self._per_window:
            self._dropped += 1
            return None
        self._used += 1
        dropped, self._dropped = self._dropped, 0
        return dropped


_LOG_BUDGET = _LineBudget()


class OAuthAttemptLogger:
    """Log one line per ``/authorize`` and ``/token`` request: client, PKCE
    and scope facts, the auth style, the status and any OAuth error.

    Without it a failed connector sign-in leaves no trace (the server keeps no
    access log). Never logs a secret, a code, a verifier, a token, URL
    userinfo or a URL query. Installed outside ``StaticClientBasicAuthShim``
    so it sees what the client actually sent, not the compat rewrite (other
    outer layers ignore these paths). Never changes a response: it only reads
    a prefix of a POST body and passes the whole stream on. GET and POST are
    logged; other methods (CORS preflight, HEAD) are not. Lines are
    rate-limited (``_LineBudget``); drops are counted in the next line.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") not in _OAUTH_LOG_PATHS:
            await self.app(scope, receive, send)
            return
        path, method = scope["path"], scope.get("method")
        if method == "GET":
            fields = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        elif method == "POST":
            # Read a prefix only, then hand the SDK the whole stream untouched:
            # an oversize body or a mid-body disconnect reaches it exactly as
            # it would without this logger.
            seen, body, complete, receive = await _peek_body(receive, _MAX_TOKEN_BODY)
            fields = (
                parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True)
                if complete else [("body", "unparsed (over cap or disconnected)")]
            )
        else:
            await self.app(scope, receive, send)
            return
        # Observing must never change the response: any failure to describe
        # the request degrades the log line, not the request.
        try:
            facts = _attempt_facts(path, dict(fields), _basic_credentials(scope))
        except Exception as exc:
            facts = {"facts": f"unavailable ({type(exc).__name__})"}

        outcome = {"status": None, "error": None, "error_description": None}

        async def logging_send(message):
            if message["type"] == "http.response.start":
                outcome["status"] = message.get("status")
                for k, v in message.get("headers", []):
                    if k.lower() == b"location":
                        try:
                            q = dict(parse_qsl(urlparse(v.decode("latin-1")).query))
                        except ValueError:
                            q = {}
                        outcome["error"] = q.get("error")
                        outcome["error_description"] = q.get("error_description")
            elif message["type"] == "http.response.body" and outcome["status"] and outcome["status"] >= 400:
                try:
                    data = json.loads(message.get("body", b"") or b"{}")
                    outcome["error"] = outcome["error"] or data.get("error")
                    outcome["error_description"] = outcome["error_description"] or data.get("error_description")
                except (ValueError, AttributeError):
                    pass
            await send(message)

        try:
            await self.app(scope, receive, logging_send)
        finally:
            suppressed = _LOG_BUDGET.take()
            if suppressed is None:
                return
            if suppressed:
                logger.info("[OAUTH] %d attempt line(s) suppressed by the rate limit", suppressed)
            logger.info(
                "[OAUTH] %s %s -> %s%s",
                path.strip("/"),
                " ".join(f"{k}={_log_safe(v)}" for k, v in facts.items()),
                outcome["status"],
                (
                    f" error={_log_safe(outcome['error'])}"
                    f" ({_log_safe(_strip_queries(outcome['error_description']))})"
                    if outcome["error"] else ""
                ),
            )


_MAX_TOKEN_BODY = 64 * 1024


def _basic_credentials(scope) -> tuple[str, str] | None:
    for key, value in scope.get("headers", []):
        if key.lower() != b"authorization":
            continue
        scheme, _, encoded = value.decode("latin-1").partition(" ")
        if scheme.lower() != "basic":
            return None
        try:
            decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
        client_id, sep, client_secret = decoded.partition(":")
        if not sep:
            return None
        return unquote(client_id), unquote(client_secret)
    return None


def _replay(messages):
    pending = list(messages)

    async def receive():
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    return receive
