"""
In-memory OAuth 2.1 Authorization Server Provider for unitares-governance.

Implements the MCP SDK's OAuthAuthorizationServerProvider protocol.
Tokens stored in-memory — reset on server restart (Claude.ai re-authenticates).
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    OAuthToken,
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

    def is_expired(self, ttl: int = 604800) -> bool:
        return time.time() > self.created_at + ttl


class GovernanceOAuthProvider(OAuthAuthorizationServerProvider):
    """
    In-memory OAuth 2.1 Authorization Server for unitares-governance.

    Implements OAuthAuthorizationServerProvider protocol from the MCP SDK.
    Single-user, personal server — optimized for simplicity.
    """

    def __init__(
        self,
        secret: str | None = None,
        auto_approve: bool = True,
        access_token_ttl: int = 3600,
        refresh_token_ttl: int = 604800,
        auth_code_ttl: int = 300,
        static_clients: list[OAuthClientInformationFull] | None = None,
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

    def _generate_token(self, prefix: str = "at") -> str:
        """Generate a cryptographically random token."""
        raw = secrets.token_hex(32)
        return f"{prefix}_{raw}"

    def get_token_client_id(self, token: str) -> str | None:
        """Get the client_id associated with a bearer token, or None."""
        at = self._access_tokens.get(token)
        return at.client_id if at and hasattr(at, "client_id") else None

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            client_info.client_id = f"unitares_{secrets.token_hex(16)}"
        if not client_info.client_secret:
            client_info.client_secret = secrets.token_hex(32)
        if not client_info.client_id_issued_at:
            client_info.client_id_issued_at = int(time.time())
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

        self._refresh_tokens[refresh_token_str] = RefreshTokenEntry(
            token=refresh_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes or ["mcp:tools"],
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
        entry = self._refresh_tokens.get(refresh_token)
        if entry is None:
            return None
        if entry.client_id != client.client_id:
            return None
        if entry.is_expired(self._refresh_token_ttl):
            del self._refresh_tokens[refresh_token]
            return None
        return entry

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshTokenEntry,
        scopes: list[str],
    ) -> OAuthToken:
        self._refresh_tokens.pop(refresh_token.token, None)

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

        self._refresh_tokens[new_refresh_str] = RefreshTokenEntry(
            token=new_refresh_str,
            client_id=client.client_id,
            scopes=effective_scopes,
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
        if entry is None:
            return None
        if entry.expires_at and entry.expires_at < int(time.time()):
            del self._access_tokens[token]
            return None
        return entry

    async def revoke_token(self, token: AccessToken | RefreshTokenEntry) -> None:
        if isinstance(token, AccessToken):
            self._access_tokens.pop(token.token, None)
            to_remove = [k for k, v in self._refresh_tokens.items()
                         if v.client_id == token.client_id]
            for k in to_remove:
                del self._refresh_tokens[k]
        elif isinstance(token, RefreshTokenEntry):
            self._refresh_tokens.pop(token.token, None)


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
    """Rewrite HTTP Basic client auth on ``POST /token`` into form fields.

    The SDK authenticates a client only by its registered method, and its Basic
    path still requires ``client_id`` in the form body, which RFC 6749 §2.3.1
    clients commonly omit. For the static client only, move the Basic
    credentials into the form and drop the header, so either style reaches the
    ``client_secret_post`` check. Every other request passes through untouched,
    including DCR clients that registered for Basic.
    """

    def __init__(self, app, *, client_id: str, path: str = "/token"):
        self.app = app
        self._client_id = client_id
        self._path = path

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != self._path
        ):
            await self.app(scope, receive, send)
            return
        creds = _basic_credentials(scope)
        if creds is None or creds[0] != self._client_id:
            await self.app(scope, receive, send)
            return

        # The client_id is public, so this runs for anonymous callers before the
        # SDK's own body limit. A token request is a few hundred bytes.
        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                # Client went away before the body finished; nothing to rewrite.
                await self.app(scope, _replay([message]), send)
                return
            body += message.get("body", b"")
            if len(body) > _MAX_TOKEN_BODY:
                await JSONResponse(
                    {"error": "invalid_request", "error_description": "request body too large"},
                    status_code=413,
                )(scope, receive, send)
                return
            more = message.get("more_body", False)

        fields = parse_qsl(bytes(body).decode("utf-8", errors="replace"), keep_blank_values=True)
        present = {k for k, _ in fields}
        if "client_id" not in present:
            fields.append(("client_id", creds[0]))
        if "client_secret" not in present:
            fields.append(("client_secret", creds[1]))
        new_body = urlencode(fields).encode()

        headers = [
            (k, v)
            for k, v in scope.get("headers", [])
            if k.lower() not in (b"authorization", b"content-length")
        ]
        headers.append((b"content-length", str(len(new_body)).encode()))
        await self.app(
            {**scope, "headers": headers},
            _replay([{"type": "http.request", "body": new_body, "more_body": False}]),
            send,
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
