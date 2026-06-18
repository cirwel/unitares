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
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
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
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return False
    presented = authorization_header[len("Bearer "):].strip()
    if not presented:
        return False
    # Constant-time membership test over a small allowlist.
    return any(hmac.compare_digest(presented, tok) for tok in allow)
