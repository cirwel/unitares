"""SDK integration for operator-managed confidential OAuth clients.

Keep SDK authorization-code/PKCE/token handling; replace only client secret
verification. Legacy env/DCR clients retain the SDK authenticator. Managed
clients never carry a plaintext secret (or a digest-as-password) in SDK objects.
"""
from __future__ import annotations

import hmac
from urllib.parse import parse_qsl

from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, request_response

from src.oauth_connectors import managed_client_id, secret_digest
from src.oauth_provider import (
    StaticClientBasicAuthShim, _basic_credentials, _is_urlencoded,
    _MAX_TOKEN_BODY, _peek_body, static_pkce_verifier,
)


class ConnectorAuthenticator(ClientAuthenticator):
    async def authenticate_request(self, request: Request):
        fields = await request.form()
        client_id = str(fields.get("client_id") or "")
        if not managed_client_id(client_id):
            return await super().authenticate_request(request)
        if not _is_urlencoded(request.scope):
            raise AuthenticationError("Managed connectors require form-encoded token requests")
        record = await self.provider.managed_connector(client_id)
        supplied = fields.get("client_secret")
        if (record is None or not isinstance(supplied, str)
                or not hmac.compare_digest(secret_digest(supplied), record["secret_hash"])):
            raise AuthenticationError("Invalid connector credentials or disabled connector")
        client = await self.provider.get_client(client_id)
        if client is None:
            raise AuthenticationError("Connector is disabled or unavailable")
        return client


class ConnectorCompatibility:
    """Bounded request handling, plus the existing confidential-client shim.

    The shim accepts Basic and form authentication and supports confidential
    clients that omit PKCE. It never replaces a supplied PKCE challenge or
    verifier. Authentication always still requires the real client secret.
    """
    def __init__(self, app, *, provider):
        self.app = app
        self.provider = provider

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") not in ("/token", "/authorize"):
            return await self.app(scope, receive, send)
        method = scope.get("method")
        if method == "GET":
            fields = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        elif method == "POST":
            # All token requests are bounded, including unknown clients. Reject
            # ambiguous encodings instead of letting the authenticator and shim
            # interpret different credentials.
            _, body, complete, replay = await _peek_body(receive, _MAX_TOKEN_BODY)
            if not complete:
                return await JSONResponse({"error": "invalid_request"}, 413)(scope, receive, send)
            receive = replay
            if not _is_urlencoded(scope):
                # Preserve SDK-supported encodings for legacy clients. The
                # managed authenticator independently requires urlencoding.
                return await self.app(scope, receive, send)
            fields = parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        else:
            return await self.app(scope, receive, send)
        values = dict(fields)
        basic = _basic_credentials(scope) if scope.get("path") == "/token" else None
        client_id = values.get("client_id") or (basic[0] if basic else "")
        if not managed_client_id(client_id):
            return await self.app(scope, receive, send)
        if method == "POST" and b";" in body:
            return await JSONResponse({"error": "invalid_request"}, 400)(scope, receive, send)
        # No duplicated fields or conflicting credential sources for managed
        # clients. The Basic shim removes Authorization after its rewrite.
        keys = [key for key, _ in fields]
        if len(keys) != len(set(keys)) or (basic and (
            values.get("client_id", basic[0]) != basic[0] or "client_secret" in values
        )):
            return await JSONResponse({"error": "invalid_request"}, 400)(scope, receive, send)
        record = await self.provider.managed_connector(client_id)
        if record is None:
            return await JSONResponse({"error": "invalid_client"}, 401)(scope, receive, send)
        shim = StaticClientBasicAuthShim(
            self.app, client_id=client_id,
            pkce_verifier=static_pkce_verifier(record["secret_hash"]),
        )
        await shim(scope, receive, send)


def install_connector_transport(app, provider):
    """Replace the SDK token route without replacing its grant implementation."""
    handler = TokenHandler(provider, ConnectorAuthenticator(provider))
    token_app = CORSMiddleware(
        request_response(handler.handle), allow_origins=["*"],
        allow_methods=["POST", "OPTIONS"], allow_headers=["Authorization", "Content-Type"],
    )
    found = False
    for index, route in enumerate(app.routes):
        if getattr(route, "path", None) == "/token":
            app.routes[index] = Route("/token", token_app, methods=["POST", "OPTIONS"])
            found = True
    if not found:
        raise RuntimeError("OAuth SDK token route missing; cannot install connector authentication")
    app.add_middleware(ConnectorCompatibility, provider=provider)
