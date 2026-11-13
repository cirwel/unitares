"""Shared HTTP client helpers for resident processes calling Core `/v1/*` routes.

Residents run outside the Core process (including across hosts when Core runs
on a remote Compose stack), so every read/write must go through Core's HTTP or
MCP interfaces rather than opening a direct PostgreSQL connection or importing
`src.*`.

Credential resolution & `UNITARES_REST_STRICT` posture:
- Under strict / hosted posture (`UNITARES_REST_STRICT=1`, the default whenever
  Core has `UNITARES_MCP_BEARER_TOKENS` configured), Core validates HTTP
  bearers against `UNITARES_MCP_BEARER_TOKENS` (`UNITARES_MCP_BEARER_TOKEN` on
  the client).
- Under explicit loosened posture (`UNITARES_REST_STRICT=0` even with
  `UNITARES_MCP_BEARER_TOKENS` set on Core) or local non-strict posture, Core
  validates HTTP bearers against `UNITARES_HTTP_API_TOKEN`.
- When both env tokens are present and `UNITARES_REST_STRICT` is unset on the
  client, non-loopback targets (e.g. `https://gov.example`) try
  `UNITARES_MCP_BEARER_TOKEN` first so one-shot remote residents and CLI tools
  authenticate cleanly without a 401 or sending a local HTTP token, while
  loopback targets (`localhost` / `127.0.0.1` / `::1`) try
  `UNITARES_HTTP_API_TOKEN` first. If Core answers `401` while a distinct
  secondary env token is configured, `send_with_bearer_fallback` /
  `async_send_with_bearer_fallback` retry once with the alternate token and
  cache the winning env var in-process for long-lived residents.
"""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

DEFAULT_GOVERNANCE_BASE_URL = "http://127.0.0.1:8767"

_ENDPOINT_PATH_SUFFIXES = (
    "/mcp/",
    "/mcp",
    "/v1/tools/call/",
    "/v1/tools/call",
    "/api/findings/",
    "/api/findings",
)

# Per-base-URL cache of the env var name ("UNITARES_MCP_BEARER_TOKEN" or
# "UNITARES_HTTP_API_TOKEN") that most recently succeeded after a 401 probe.
_preferred_env_by_base: dict[str, str] = {}


def clear_bearer_preference_cache() -> None:
    """Reset the in-process bearer-token winner cache (used in tests)."""
    _preferred_env_by_base.clear()


def _normalize_base_url(raw_url: str) -> str:
    """Strip known endpoint suffixes (`/mcp`, `/v1/tools/call`, `/api/findings`)
    while preserving any reverse-proxy path prefix (e.g. `https://host/unitares`)."""
    cleaned = raw_url.strip()
    parts = urlsplit(cleaned)
    if not parts.scheme or not parts.netloc:
        return cleaned.rstrip("/")
    path = parts.path or ""
    for suffix in _ENDPOINT_PATH_SUFFIXES:
        if path == suffix or path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    path = path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")


def resolve_governance_base_url(explicit_url: str | None = None) -> str:
    """Return the Core HTTP base URL (`scheme://host[:port][/prefix]`) without trailing `/`.

    Checks `explicit_url` first, then `GOV_REST_URL`, then `MCP_SERVER_URL`.
    Chronicler's `UNITARES_METRICS_URL` stays scoped to Chronicler so a
    metrics-only target never hijacks Sentinel or Vigil's governance base URL.
    """
    if explicit_url and explicit_url.strip():
        return _normalize_base_url(explicit_url)
    for env_name in ("GOV_REST_URL", "MCP_SERVER_URL"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            return _normalize_base_url(raw)
    return DEFAULT_GOVERNANCE_BASE_URL


def _is_loopback_base_url(base_url: str | None) -> bool:
    """True when `base_url` is absent or targets a loopback host (`localhost`, `127.0.0.1`, `::1`)."""
    if not base_url or not base_url.strip():
        return True
    host = (urlsplit(base_url.strip()).hostname or "").lower()
    return host in {"", "localhost", "127.0.0.1", "::1"}


def _ordered_env_token_pairs(base_url: str | None = None) -> list[tuple[str, str]]:
    """Return `(env_name, token)` candidates in attempt order."""
    mcp_token = os.environ.get("UNITARES_MCP_BEARER_TOKEN", "").strip()
    http_token = os.environ.get("UNITARES_HTTP_API_TOKEN", "").strip()

    strict_hint = os.environ.get("UNITARES_REST_STRICT", "").strip().lower()
    if strict_hint in {"1", "true", "yes", "on"}:
        env_order = ["UNITARES_MCP_BEARER_TOKEN", "UNITARES_HTTP_API_TOKEN"]
    elif strict_hint in {"0", "false", "no", "off"}:
        env_order = ["UNITARES_HTTP_API_TOKEN", "UNITARES_MCP_BEARER_TOKEN"]
    elif not _is_loopback_base_url(base_url):
        # Non-loopback targets (e.g. `https://gov.example`) default to strict
        # posture on Core whenever `UNITARES_MCP_BEARER_TOKENS` is set, so one-shot
        # remote residents and CLI tools try `UNITARES_MCP_BEARER_TOKEN` first
        # without incurring a 401 or sending a local HTTP token.
        env_order = ["UNITARES_MCP_BEARER_TOKEN", "UNITARES_HTTP_API_TOKEN"]
    else:
        # Loopback targets (`localhost` / `127.0.0.1` / `::1`) default to
        # `UNITARES_HTTP_API_TOKEN` first; long-lived residents against a strict
        # local server retry once on 401 and cache the winner in-process.
        env_order = ["UNITARES_HTTP_API_TOKEN", "UNITARES_MCP_BEARER_TOKEN"]

    if base_url:
        norm = _normalize_base_url(base_url)
        remembered = _preferred_env_by_base.get(norm)
        if remembered in env_order:
            env_order = [remembered] + [k for k in env_order if k != remembered]

    values = {
        "UNITARES_MCP_BEARER_TOKEN": mcp_token,
        "UNITARES_HTTP_API_TOKEN": http_token,
    }
    seen_tokens: set[str] = set()
    out: list[tuple[str, str]] = []
    for env_name in env_order:
        tok = values[env_name]
        if tok and tok not in seen_tokens:
            seen_tokens.add(tok)
            out.append((env_name, tok))
    return out


def iter_http_bearer_tokens(
    explicit_token: str | None = None,
    *,
    base_url: str | None = None,
) -> list[str]:
    """Return ordered bearer token candidates (at most 1 when `explicit_token` given)."""
    if explicit_token is not None:
        stripped = explicit_token.strip()
        return [stripped] if stripped else []
    return [tok for _, tok in _ordered_env_token_pairs(base_url=base_url)]


def record_bearer_winner(base_url: str | None, winning_token: str) -> None:
    """Remember which env var produced `winning_token` for `base_url`."""
    if not base_url or not winning_token:
        return
    norm = _normalize_base_url(base_url)
    for env_name in ("UNITARES_MCP_BEARER_TOKEN", "UNITARES_HTTP_API_TOKEN"):
        if os.environ.get(env_name, "").strip() == winning_token:
            _preferred_env_by_base[norm] = env_name
            return


def resolve_http_bearer_token(
    explicit_token: str | None = None,
    *,
    base_url: str | None = None,
) -> str | None:
    """Return the primary bearer credential for Core HTTP routes, or `None` if unset."""
    candidates = iter_http_bearer_tokens(explicit_token, base_url=base_url)
    return candidates[0] if candidates else None


def build_auth_headers(
    token: str | None = None,
    *,
    json_body: bool = False,
    base_url: str | None = None,
) -> dict[str, str]:
    """Build HTTP request headers with optional bearer auth and JSON content-type."""
    headers: dict[str, str] = {}
    if json_body:
        headers["Content-Type"] = "application/json"
    resolved = resolve_http_bearer_token(token, base_url=base_url)
    if resolved:
        headers["Authorization"] = f"Bearer {resolved}"
    return headers


def send_with_bearer_fallback(
    send_fn: Callable[[dict[str, str]], Any],
    *,
    explicit_token: str | None = None,
    base_url: str | None = None,
    json_body: bool = False,
    lowercase_auth_header: bool = False,
) -> Any:
    """Execute sync `send_fn(headers)` and retry once on HTTP 401 with alternate env token."""
    auth_key = "authorization" if lowercase_auth_header else "Authorization"
    content_key = "content-type" if lowercase_auth_header else "Content-Type"

    def _make_headers(tok: str | None) -> dict[str, str]:
        hdrs: dict[str, str] = {}
        if json_body:
            hdrs[content_key] = "application/json"
        if tok:
            hdrs[auth_key] = f"Bearer {tok}"
        return hdrs

    candidates = iter_http_bearer_tokens(explicit_token, base_url=base_url)
    if not candidates:
        return send_fn(_make_headers(None))

    resp = send_fn(_make_headers(candidates[0]))
    if getattr(resp, "status_code", 0) == 401 and len(candidates) > 1:
        retry_resp = send_fn(_make_headers(candidates[1]))
        if getattr(retry_resp, "status_code", 0) < 400:
            record_bearer_winner(base_url, candidates[1])
        return retry_resp
    if getattr(resp, "status_code", 0) < 400 and explicit_token is None:
        record_bearer_winner(base_url, candidates[0])
    return resp


async def async_send_with_bearer_fallback(
    send_fn: Callable[[dict[str, str]], Awaitable[Any]],
    *,
    explicit_token: str | None = None,
    base_url: str | None = None,
    json_body: bool = False,
) -> Any:
    """Async mirror of `send_with_bearer_fallback`."""
    def _make_headers(tok: str | None) -> dict[str, str]:
        hdrs: dict[str, str] = {}
        if json_body:
            hdrs["Content-Type"] = "application/json"
        if tok:
            hdrs["Authorization"] = f"Bearer {tok}"
        return hdrs

    candidates = iter_http_bearer_tokens(explicit_token, base_url=base_url)
    if not candidates:
        return await send_fn(_make_headers(None))

    resp = await send_fn(_make_headers(candidates[0]))
    if getattr(resp, "status_code", 0) == 401 and len(candidates) > 1:
        retry_resp = await send_fn(_make_headers(candidates[1]))
        if getattr(retry_resp, "status_code", 0) < 400:
            record_bearer_winner(base_url, candidates[1])
        return retry_resp
    if getattr(resp, "status_code", 0) < 400 and explicit_token is None:
        record_bearer_winner(base_url, candidates[0])
    return resp
