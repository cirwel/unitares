"""Shared HTTP client helpers for resident processes calling Core `/v1/*` routes.

Residents run outside the Core process (including across hosts when Core runs
on a remote Compose stack), so every read/write must go through Core's HTTP or
MCP interfaces rather than opening a direct PostgreSQL connection or importing
`src.*`.

Credential precedence matches `src/http_routes/access.py:_check_http_auth`:
- In strict / hosted posture (`UNITARES_REST_STRICT=1` or
  `UNITARES_MCP_BEARER_TOKENS` configured on Core), Core validates against the
  MCP bearer allowlist (`UNITARES_MCP_BEARER_TOKEN` on the client).
- In local non-strict posture (no MCP bearer allowlist), Core validates against
  `UNITARES_HTTP_API_TOKEN`.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

DEFAULT_GOVERNANCE_BASE_URL = "http://127.0.0.1:8767"


def _origin_from_endpoint(raw_url: str) -> str:
    """Strip path/query/fragment from a `/mcp/` or `/v1/tools/call` URL."""
    parts = urlsplit(raw_url.strip())
    if not parts.scheme or not parts.netloc:
        return raw_url.strip().rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, "", "", "")).rstrip("/")


def resolve_governance_base_url(explicit_url: str | None = None) -> str:
    """Return the Core HTTP base URL (`scheme://host[:port]`) without trailing `/`."""
    if explicit_url and explicit_url.strip():
        return _origin_from_endpoint(explicit_url)
    for env_name in ("UNITARES_METRICS_URL", "GOV_REST_URL", "MCP_SERVER_URL"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            return _origin_from_endpoint(raw)
    return DEFAULT_GOVERNANCE_BASE_URL


def resolve_http_bearer_token(explicit_token: str | None = None) -> str | None:
    """Return the bearer credential for Core HTTP routes, or `None` if unset.

    Prefers `UNITARES_MCP_BEARER_TOKEN` (required when Core runs in strict /
    cloud posture) and falls back to `UNITARES_HTTP_API_TOKEN` for local
    non-strict deployments.
    """
    if explicit_token is not None:
        stripped = explicit_token.strip()
        return stripped or None
    mcp_token = os.environ.get("UNITARES_MCP_BEARER_TOKEN", "").strip()
    if mcp_token:
        return mcp_token
    http_token = os.environ.get("UNITARES_HTTP_API_TOKEN", "").strip()
    if http_token:
        return http_token
    return None


def build_auth_headers(
    token: str | None = None,
    *,
    json_body: bool = False,
) -> dict[str, str]:
    """Build HTTP request headers with optional bearer auth and JSON content-type."""
    headers: dict[str, str] = {}
    if json_body:
        headers["Content-Type"] = "application/json"
    resolved = resolve_http_bearer_token(token)
    if resolved:
        headers["Authorization"] = f"Bearer {resolved}"
    return headers
