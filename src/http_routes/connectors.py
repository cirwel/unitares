"""Operator-only connector setup. Agent bearer tokens grant no registry access."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

from src import dashboard_auth
from src.oauth_connectors import validate_registration

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _reply(body, status=200):
    return JSONResponse(body, status_code=status, headers=NO_STORE)


def _connector_management_gate(request):
    operator = dashboard_auth._operator_token_authorized(request)
    session = dashboard_auth.dashboard_session_authenticated(request)
    if not operator and not session:
        return _reply({"error": "Sign in to the dashboard or provide the operator credential in Security."}, 403)
    origin = request.headers.get("origin")
    if origin:
        issuer = str(getattr(request.state, "oauth_issuer", "") or "")
        parsed = urlsplit(issuer)
        allowed = {
            dashboard_auth.DASHBOARD_EXPECTED_ORIGIN,
            f"{parsed.scheme}://{parsed.netloc}",
            # A local or alternate dashboard origin need not be the OAuth
            # issuer. Browser Origin and request authority must agree; this
            # still requires operator auth (or a fresh session + CSRF).
            f"{request.url.scheme}://{request.url.netloc}",
        }
        if origin not in allowed or origin == "null":
            return _reply({"error": "Cross-origin connector management is not allowed."}, 403)
    if request.method != "GET" and not operator:
        if not dashboard_auth.dashboard_session_write_authorized(request):
            return _reply({"error": "Refresh this page and try again (missing CSRF header)."}, 403)
        if not dashboard_auth.dashboard_session_is_fresh(request):
            return _reply({"error": "Sign in again to add or disable a connector.",
                           "signin_url": "/auth/signin?stepup=1"}, 403)
    return None


def _actor(request):
    if dashboard_auth._operator_token_authorized(request):
        return "operator_credential"
    session = dashboard_auth._session_state(request) or {}
    # Hash already held by the dashboard session, never the session cookie.
    key = session.get("session_hash", b"")
    return "dashboard_session:" + (key.hex() if isinstance(key, bytes) else str(key))


async def http_connectors(request):
    if denied := _connector_management_gate(request):
        return denied
    provider = getattr(request.state, "oauth_provider", None)
    registry = getattr(provider, "connector_registry", None)
    if registry is None:
        return _reply({"error": "OAuth is not configured on this server. Complete the public HTTPS server setup first.",
                       "setup_url": "https://github.com/cirwel/unitares/blob/master/docs/integration/MCP_CLIENTS.md"}, 503)
    try:
        if request.method == "GET":
            issuer = str(request.state.oauth_issuer).rstrip("/")
            return _reply({
                "connectors": await registry.list(),
                "server_url": str(request.state.oauth_resource),
                "authorization_endpoint": issuer + "/authorize",
                "token_endpoint": issuer + "/token",
                "scopes": "mcp:tools", "token_auth_method": "client_secret_basic",
                "legacy_client_configured": bool(provider._static_client_ids),
            })
        if "client_id" in request.path_params:
            changed = await registry.revoke(request.path_params["client_id"], _actor(request))
            return _reply({"success": changed}, 200 if changed else 404)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16 * 1024:
                return _reply({"error": "Connector details are too large."}, 413)
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Enter a connector name and callback URLs.")
            name, redirects = validate_registration(data.get("name"), data.get("redirect_uris"))
        except (ValueError, UnicodeDecodeError) as exc:
            # JSON decoder text includes no raw payload; do not echo invalid
            # values (this may be a mistakenly pasted credential).
            message = str(exc) if not isinstance(exc, json.JSONDecodeError) else "Invalid JSON body."
            return _reply({"error": message}, 400)
        created = await registry.create(name, redirects, _actor(request))
        return _reply(created, 201)
    except Exception:
        # No soft fallback: claiming success on a memory-only registration
        # would lose credentials at restart. Do not log payloads or DB errors.
        return _reply({"error": "Connector storage is unavailable. Check database health and apply migration 082 before trying again."}, 503)
