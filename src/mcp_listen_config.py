"""
Default bind address and MCP transport-security allowlists.

Security defaults:
- Listen on 127.0.0.1 unless UNITARES_BIND_ALL_INTERFACES is set (opt-in 0.0.0.0).
- allowed_hosts / allowed_origins: localhost always; extras via env (no hardcoded LAN IPs in code).

See CLAUDE.md for environment variables.
"""

from __future__ import annotations

import hmac
import os
import time
from typing import List, Optional

from mcp.server.transport_security import TransportSecuritySettings

_MCP_BEARER_TOKENS_ENV = "UNITARES_MCP_BEARER_TOKENS"


def env_truthy(name: str, default: bool = False) -> bool:
    """True if env var is 1/true/yes/on (case-insensitive)."""
    v = os.environ.get(name, "")
    if not v:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def split_csv_env(name: str) -> List[str]:
    """Split a comma-separated env var into stripped non-empty tokens."""
    raw = os.environ.get(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def default_listen_host() -> str:
    """
    Return the default socket bind address.

    127.0.0.1 unless UNITARES_BIND_ALL_INTERFACES is truthy (then 0.0.0.0).
    Override entirely with UNITARES_MCP_HOST if set (e.g. a specific LAN IP).
    """
    explicit = os.environ.get("UNITARES_MCP_HOST", "").strip()
    if explicit:
        return explicit
    if env_truthy("UNITARES_BIND_ALL_INTERFACES"):
        return "0.0.0.0"
    return "127.0.0.1"


def build_transport_security_settings() -> TransportSecuritySettings:
    """
    Build TransportSecuritySettings for FastMCP.

    Base allowlists always include localhost. Append UNITARES_MCP_ALLOWED_HOSTS and
    UNITARES_MCP_ALLOWED_ORIGINS (comma-separated). Optional opaque 'null' origin
    for file:// clients when UNITARES_MCP_ALLOW_NULL_ORIGIN is truthy (default true).

    Protection is on by default. An operator whose client sends a Host this
    deployment cannot enumerate ahead of time can unblock without a code change
    by setting UNITARES_MCP_DNS_REBIND_PROTECTION=off — the correct fix is
    normally to add that host to UNITARES_MCP_ALLOWED_HOSTS instead.
    """
    base_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    extra_hosts = split_csv_env("UNITARES_MCP_ALLOWED_HOSTS")
    allowed_hosts = base_hosts + extra_hosts

    base_origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    ]
    extra_origins = split_csv_env("UNITARES_MCP_ALLOWED_ORIGINS")
    allowed_origins = base_origins + extra_origins
    if env_truthy("UNITARES_MCP_ALLOW_NULL_ORIGIN", default=True):
        allowed_origins.append("null")

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=dns_rebinding_protection_enabled(),
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def dns_rebinding_protection_enabled() -> bool:
    """Whether Host/Origin validation is enforced on the MCP transports.

    On unless UNITARES_MCP_DNS_REBIND_PROTECTION is explicitly falsy
    (0/false/no/off). Read fresh so the value can be logged at startup.
    """
    raw = os.environ.get("UNITARES_MCP_DNS_REBIND_PROTECTION", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def build_streamable_session_manager(app, *, stateless: bool = True):
    """
    Build the StreamableHTTPSessionManager that serves ``/mcp``.

    The security settings must be attached HERE. ``/mcp`` is served by this
    manager (mounted in ``mcp_server.main()``), not by the SDK's own
    ``streamable_http_app()``, so the ``transport_security=`` handed to the
    high-level server object never reaches this transport — and
    ``security_settings=None`` makes the SDK's ``TransportSecurityMiddleware``
    default to protection DISABLED. Constructing the manager anywhere else
    silently drops UNITARES_MCP_ALLOWED_HOSTS / _ORIGINS enforcement.

    ``security_settings`` is accepted on both the mcp 1.x and 2.x lines.
    """
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    return StreamableHTTPSessionManager(
        app=app,
        stateless=stateless,
        security_settings=build_transport_security_settings(),
    )


def cors_extra_origins() -> List[str]:
    """Optional extra CORS origins from UNITARES_HTTP_CORS_EXTRA_ORIGINS (comma-separated)."""
    return split_csv_env("UNITARES_HTTP_CORS_EXTRA_ORIGINS")


def mcp_bearer_tokens() -> List[str]:
    """Allowlist of bearer tokens accepted on the ``/mcp`` endpoint.

    Read fresh each call so an operator can rotate tokens without a restart
    (mirrors the operator-token allowlist in identity/operator.py). Empty by
    default: when empty the ``/mcp`` bearer gate is OFF and the endpoint
    behaves exactly as before — this preserves the localhost / self-host
    default where no token is configured.
    """
    return split_csv_env(_MCP_BEARER_TOKENS_ENV)


def mcp_bearer_required() -> bool:
    """True when a ``/mcp`` bearer allowlist is configured (gate is ON)."""
    return bool(mcp_bearer_tokens())


def check_mcp_bearer(
    authorization_header: Optional[str],
    allow: Optional[List[str]] = None,
) -> bool:
    """Authorize an inbound ``/mcp`` request against the bearer allowlist.

    Returns ``True`` (allow) when the gate is OFF (no tokens configured) — the
    default, so localhost dev and existing self-host deployments are unchanged.
    When the gate is ON, returns ``True`` only if the request presents
    ``Authorization: Bearer <tok>`` with ``<tok>`` in the allowlist. The token
    comparison is constant-time.

    ``allow`` may be passed by a caller that already fetched the allowlist this
    request (the ASGI gate does, to avoid a second env read and the tiny
    add/remove TOCTOU between "is the gate on" and "is this token valid"). When
    omitted it is read fresh from the environment.

    Deliberately, and unlike the HTTP REST gate (``http_api._is_trusted_network``),
    there is **no trusted-network bypass** here: a hosted endpoint typically sits
    behind a reverse proxy, so the source IP is the proxy's and an IP-based
    bypass would defeat the gate. Every request authenticates.
    """
    if allow is None:
        allow = mcp_bearer_tokens()
    if not allow:
        return True  # gate off — default posture
    presented = bearer_token_from_authorization(authorization_header)
    if not presented:
        return False
    # Constant-time membership test over a small allowlist.
    return any(hmac.compare_digest(presented, tok) for tok in allow)


def bearer_token_from_authorization(authorization_header: Optional[str]) -> Optional[str]:
    """Return the bearer token from an Authorization header, if present."""
    if not authorization_header:
        return None
    # RFC 7235: the auth scheme is case-insensitive. Accept "Bearer"/"bearer"/etc.
    scheme, _, presented = authorization_header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    presented = presented.strip()
    return presented or None


async def check_oauth_bearer(
    authorization_header: Optional[str],
    provider,
    required_scopes: Optional[List[str]] = None,
) -> tuple[bool, Optional[str]]:
    """Validate an OAuth bearer token against an MCP SDK auth provider.

    Returns ``(allowed, client_id)``. OAuth is separate from the static hosted
    bearer allowlist above: a DCR client presents a provider-minted access
    token, not a value from ``UNITARES_MCP_BEARER_TOKENS``.
    """
    token = bearer_token_from_authorization(authorization_header)
    if not token or provider is None:
        return False, None

    try:
        access_token = await provider.load_access_token(token)
    except Exception:
        return False, None

    if access_token is None:
        return False, None

    expires_at = getattr(access_token, "expires_at", None)
    if expires_at and expires_at < int(time.time()):
        return False, None

    required = required_scopes or []
    if required:
        scopes = set(getattr(access_token, "scopes", None) or [])
        if any(scope not in scopes for scope in required):
            return False, None

    client_id = getattr(access_token, "client_id", None)
    return True, client_id if isinstance(client_id, str) else None
