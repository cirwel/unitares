"""
Per-agent bearer credentials for REST check-ins.

A resident on another machine needs to check in over the authenticated HTTPS
path without holding the shared ``UNITARES_HTTP_API_TOKEN``. An agent token
authorizes one agent's check-ins and nothing else, and it can be revoked
without touching any other client.

Design (operator decision 2026-10-27, after a tailnet-to-loopback proxy that
an embodied resident relied on was retired as an unauthenticated exposure):

- ``UNITARES_HTTP_AGENT_TOKENS`` holds comma-separated entries
  ``<agent_uuid>:sha256:<64 hex>``. The server keeps only digests, so the
  service environment carries no secret. Generate a token with
  ``openssl rand -hex 32`` and store ``sha256(token)`` here.
- Accepted only by ``POST /v1/tools/call``, in both REST postures, and only
  when the request did not already authenticate another way. No other REST
  route, the WebSocket or ``/mcp`` reads it.
- Authorization, not identity. The call still resolves its caller through the
  normal REST path (client_session_id echo); the token only admits a call
  that resolves to its own agent. It is never a binding proof.
- Only ``process_agent_update`` (and its aliases) is allowed. ``identity`` and
  ``onboard`` can create or move a binding before any check could run after
  them, so they are refused outright.
- Arguments that select or prove an identity (``agent_id``, ``agent_uuid``,
  ``continuity_token``) are refused at every ``kwargs`` depth dispatch will
  unwrap, as is the ``X-Unitares-Operator`` header.
- A malformed entry disables every agent token (fail closed) and logs an
  error. It does not stop the server, so a typo cannot take down the shared
  check-in path for everyone else.
- Rotation: list the old and new digests for one agent, then drop the old.
  The process environment is fixed at launch, so a change takes effect on
  restart.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import uuid
from typing import Any, Dict, List, Optional, Tuple

from src.logging_utils import get_logger

logger = get_logger(__name__)

AGENT_TOKENS_ENV = "UNITARES_HTTP_AGENT_TOKENS"

# Canonical names, after alias resolution.
ALLOWED_TOOLS = frozenset({"process_agent_update"})

# Keys that name or prove an identity. A check-in client that echoes
# client_session_id needs none of them.
_IDENTITY_KEYS = ("agent_id", "agent_uuid", "continuity_token")

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

_warned_raw: set[str] = set()


def _parse(raw: str) -> List[Tuple[str, bytes]]:
    """Parse the allowlist. Raises ValueError naming the first bad entry."""
    entries: List[Tuple[str, bytes]] = []
    seen_digests: set[bytes] = set()
    for position, item in enumerate((x.strip() for x in raw.split(",")), start=1):
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 3 or parts[1] != "sha256":
            raise ValueError(f"entry {position} is not <agent_uuid>:sha256:<hex>")
        agent, _scheme, digest_hex = parts
        try:
            agent = str(uuid.UUID(agent))
        except ValueError:
            raise ValueError(f"entry {position} has an invalid agent uuid") from None
        digest_hex = digest_hex.lower()
        if not _DIGEST_RE.match(digest_hex):
            raise ValueError(f"entry {position} digest is not 64 hex characters")
        digest = bytes.fromhex(digest_hex)
        if digest in seen_digests:
            raise ValueError(f"entry {position} repeats a digest")
        seen_digests.add(digest)
        entries.append((agent, digest))
    return entries


def configured_entries() -> List[Tuple[str, bytes]]:
    """The allowlist, read at call time. Empty when unset or malformed."""
    raw = os.environ.get(AGENT_TOKENS_ENV, "")
    if not raw.strip():
        return []
    try:
        return _parse(raw)
    except ValueError as exc:
        if raw not in _warned_raw:
            _warned_raw.add(raw)
            logger.error(
                "%s is malformed (%s); every agent token is refused until it is fixed",
                AGENT_TOKENS_ENV,
                exc,
            )
        return []


def agent_for_bearer(token: Optional[str]) -> Optional[str]:
    """The agent uuid a presented bearer token belongs to, or None.

    Every entry is compared, without an early exit, so the response time does
    not say which entry matched or how many there are.
    """
    if not token:
        return None
    entries = configured_entries()
    if not entries:
        return None
    presented = hashlib.sha256(token.encode("utf-8")).digest()
    matched: Optional[str] = None
    for agent, digest in entries:
        if secrets.compare_digest(presented, digest) and matched is None:
            matched = agent
    return matched


def call_refusal(
    tool_name: str,
    arguments: Dict[str, Any],
    *,
    operator_header: Optional[str],
) -> Optional[str]:
    """Why an agent-token call is refused before dispatch, or None to allow it."""
    from src.mcp_handlers.middleware.params_step import unwrapped_views
    from src.mcp_handlers.tool_stability import resolve_tool_alias

    canonical, _alias = resolve_tool_alias(tool_name)
    if canonical not in ALLOWED_TOOLS:
        return f"agent tokens may call only {', '.join(sorted(ALLOWED_TOOLS))}"
    if operator_header:
        return "agent tokens cannot be combined with an operator token"
    if not isinstance(arguments, dict):
        return "arguments must be an object"
    for view in unwrapped_views(arguments, _DISPATCH_UNWRAP_DEPTH):
        named = [key for key in _IDENTITY_KEYS if key in view]
        if named:
            return (
                "agent tokens resolve the caller from client_session_id only; "
                f"remove {', '.join(named)}"
            )
    return None


# Dispatch unwraps kwargs twice (the pipeline boundary, then the HTTP
# fallback); see src/http_routes/access.py.
_DISPATCH_UNWRAP_DEPTH = 2
