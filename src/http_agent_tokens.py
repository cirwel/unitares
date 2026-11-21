"""
Per-agent bearer credentials for scoped REST tool calls.

A resident on another machine needs to check in, report outcomes, and store
observations over the authenticated HTTPS path without holding the shared
``UNITARES_HTTP_API_TOKEN``. Each agent token authorizes only the granted
capabilities for one agent UUID, and multiple tokens for the same agent can be
issued or revoked independently.

Design (operator decision 2026-10-27, extended for scoped outcome and
append-only knowledge writes):

- ``UNITARES_HTTP_AGENT_TOKENS`` holds comma-separated entries
  ``<agent_uuid>:sha256:<64 hex>`` or
  ``<agent_uuid>:sha256:<64 hex>:<scope1+scope2>``.
  When the fourth scope field is omitted, the grant defaults to ``checkin``
  only so existing credentials gain no additional permissions on deploy.
  Valid scopes are ``checkin``, ``outcome``, ``knowledge_store``, and
  ``findings``.
- The server keeps only SHA-256 digests, so the service environment carries no
  secret. Generate a token with ``openssl rand -hex 32`` and store
  ``sha256(token)`` here.
- Accepted only by ``POST /v1/tools/call`` (for ``checkin``, ``outcome``, and
  ``knowledge_store``) and ``POST /api/findings`` (for ``findings``), in both
  REST postures. A ``findings``-only token cannot call any tool on
  ``POST /v1/tools/call``, and tokens lacking ``findings`` are refused on
  ``POST /api/findings``. No other REST route, WebSocket, or ``/mcp`` reads it.
- On ``POST /v1/tools/call``, the token is authorization, not identity: the
  call still resolves its caller through the normal REST path using the body's
  explicit ``client_session_id``, and the token only admits a call that resolves
  to its own agent. On ``POST /api/findings``, a valid ``findings``-scoped
  token additionally stamps server-verified actor provenance
  (``provenance_verified=True`` and ``verified_actor_agent_uuid=<agent_uuid>``)
  onto the ingested finding event.
- Enforces flat top-level arguments with an explicit ``client_session_id`` in
  the body across all scopes (including default ``checkin`` tokens), and
  enforces per-tool argument allowlists on ``outcome`` and ``knowledge_store``.
  Wrapper keys (``kwargs``, ``arguments``, ``params``),
  ``X-Session-ID``-only injection without body ``client_session_id``,
  identity-selecting arguments (``agent_id``, ``agent_uuid``,
  ``continuity_token``), ``X-Agent-Id``, and ``X-Unitares-Operator`` are
  refused. ``identity`` and ``onboard`` remain forbidden.
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
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from src.knowledge_graph_lifecycle import PERMANENT_TAGS, PERMANENT_TYPES
from src.logging_utils import get_logger

logger = get_logger(__name__)

AGENT_TOKENS_ENV = "UNITARES_HTTP_AGENT_TOKENS"

DEFAULT_SCOPES: FrozenSet[str] = frozenset({"checkin"})
VALID_SCOPES: FrozenSet[str] = frozenset(
    {"checkin", "outcome", "knowledge_store", "findings"}
)

# Kept for callers/tests inspecting the default un-scoped tool set.
ALLOWED_TOOLS: FrozenSet[str] = frozenset({"process_agent_update"})

_IDENTITY_KEYS = ("agent_id", "agent_uuid", "continuity_token")
_WRAPPER_KEYS: FrozenSet[str] = frozenset({"kwargs", "arguments", "params"})

_OUTCOME_ALLOWED_KEYS: FrozenSet[str] = frozenset(
    {
        "client_session_id",
        "outcome_type",
        "outcome_score",
        "is_bad",
        "detail",
        "confidence",
        "decision_action",
        "response_mode",
    }
)

_KNOWLEDGE_STORE_ALLOWED_KEYS: FrozenSet[str] = frozenset(
    {
        "client_session_id",
        "action",
        "summary",
        "discovery_type",
        "tags",
        "details",
        "content",
        "severity",
    }
)
_KNOWLEDGE_STORE_ALLOWED_SEVERITIES: FrozenSet[str] = frozenset({"low", "medium"})
_KNOWLEDGE_STORE_ALLOWED_TYPES: FrozenSet[str] = (
    frozenset(
        {
            "observation",
            "insight",
            "note",
            "exploration",
            "question",
            "experiment",
        }
    )
    - PERMANENT_TYPES
)
_KNOWLEDGE_STORE_RESERVED_TAGS: FrozenSet[str] = (
    frozenset(tag.strip().lower() for tag in PERMANENT_TAGS)
    | frozenset({"source-claude-memory"})
)

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

_warned_raw: set[str] = set()


@dataclass(frozen=True)
class AgentTokenGrant:
    """Authorized scopes and audit prefix bound to an agent token digest."""

    agent_uuid: str
    scopes: FrozenSet[str]
    digest_prefix: str


def _parse_scopes(raw_scopes: Optional[str], position: int) -> FrozenSet[str]:
    if raw_scopes is None:
        return DEFAULT_SCOPES
    if not raw_scopes:
        raise ValueError(f"entry {position} has an empty scope suffix")
    parts = raw_scopes.split("+")
    if any(not scope for scope in parts):
        raise ValueError(f"entry {position} has an empty scope name")
    unknown = [scope for scope in parts if scope not in VALID_SCOPES]
    if unknown:
        raise ValueError(
            f"entry {position} has unknown scope(s): {', '.join(unknown)}"
        )
    if len(parts) != len(set(parts)):
        raise ValueError(f"entry {position} repeats a scope")
    return frozenset(parts)


def _parse(raw: str) -> List[Tuple[AgentTokenGrant, bytes]]:
    """Parse the allowlist. Raises ValueError naming the first bad entry."""
    entries: List[Tuple[AgentTokenGrant, bytes]] = []
    seen_digests: set[bytes] = set()
    for position, item in enumerate((x.strip() for x in raw.split(",")), start=1):
        if not item:
            continue
        parts = item.split(":")
        if len(parts) == 3:
            agent, scheme, digest_hex = parts
            raw_scopes: Optional[str] = None
        elif len(parts) == 4:
            agent, scheme, digest_hex, raw_scopes = parts
        else:
            raise ValueError(
                f"entry {position} is not <agent_uuid>:sha256:<hex>[:<scopes>]"
            )
        if scheme != "sha256":
            raise ValueError(
                f"entry {position} is not <agent_uuid>:sha256:<hex>[:<scopes>]"
            )
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
        scopes = _parse_scopes(raw_scopes, position)
        entries.append(
            (
                AgentTokenGrant(
                    agent_uuid=agent,
                    scopes=scopes,
                    digest_prefix=digest[:4].hex(),
                ),
                digest,
            )
        )
    return entries


def configured_entries() -> List[Tuple[AgentTokenGrant, bytes]]:
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


def grant_for_bearer(token: Optional[str]) -> Optional[AgentTokenGrant]:
    """Return the grant a presented bearer token resolves to, or None.

    Every entry is compared without an early exit so timing does not leak which
    entry matched or how many entries exist.
    """
    if not token:
        return None
    entries = configured_entries()
    if not entries:
        return None
    presented = hashlib.sha256(token.encode("utf-8")).digest()
    matched: Optional[AgentTokenGrant] = None
    for grant, digest in entries:
        if secrets.compare_digest(presented, digest) and matched is None:
            matched = grant
    return matched


def agent_for_bearer(token: Optional[str]) -> Optional[str]:
    """The agent uuid a presented bearer token belongs to, or None."""
    grant = grant_for_bearer(token)
    return grant.agent_uuid if grant is not None else None


def call_refusal(
    tool_name: str,
    arguments: Dict[str, Any],
    *,
    operator_header: Optional[str],
    agent_id_header: Optional[str] = None,
    scopes: FrozenSet[str] = DEFAULT_SCOPES,
) -> Optional[str]:
    """Why an agent-token call is refused before dispatch, or None to allow it."""
    from src.mcp_handlers.tool_stability import resolve_tool_alias

    if operator_header:
        return "agent tokens cannot be combined with an operator token"
    if agent_id_header:
        return "agent tokens cannot be combined with X-Agent-Id"
    if not isinstance(arguments, dict):
        return "arguments must be an object"

    wrappers = sorted(key for key in _WRAPPER_KEYS if key in arguments)
    if wrappers:
        return (
            "agent tokens require flat top-level arguments; "
            f"remove {', '.join(wrappers)}"
        )

    named = [key for key in _IDENTITY_KEYS if key in arguments]
    if named:
        return (
            "agent tokens resolve the caller from client_session_id only; "
            f"remove {', '.join(named)}"
        )

    csid = arguments.get("client_session_id")
    if not isinstance(csid, str) or not csid.strip():
        return "agent tokens require a non-empty client_session_id argument"

    canonical, alias = resolve_tool_alias(tool_name)

    if canonical == "process_agent_update":
        if "checkin" not in scopes:
            return "agent token lacks 'checkin' scope for process_agent_update"
        return None

    if canonical == "outcome_event":
        if "outcome" not in scopes:
            return "agent token lacks 'outcome' scope for outcome_event"
        disallowed = sorted(set(arguments.keys()) - _OUTCOME_ALLOWED_KEYS)
        if disallowed:
            return (
                "agent token forbids argument(s) on outcome_event: "
                f"{', '.join(disallowed)}"
            )
        return None

    if canonical == "knowledge":
        if "knowledge_store" not in scopes:
            return "agent token lacks 'knowledge_store' scope for knowledge"
        disallowed = sorted(set(arguments.keys()) - _KNOWLEDGE_STORE_ALLOWED_KEYS)
        if disallowed:
            return (
                "agent token forbids argument(s) on knowledge: "
                f"{', '.join(disallowed)}"
            )
        if "action" in arguments:
            action = arguments["action"]
            if not isinstance(action, str) or action != "store":
                return "agent tokens admit only action='store' on knowledge"
        elif not (alias is not None and alias.inject_action == "store"):
            return "agent tokens admit only action='store' on knowledge"

        if "severity" in arguments and arguments["severity"] is not None:
            severity = arguments["severity"]
            if (
                not isinstance(severity, str)
                or severity not in _KNOWLEDGE_STORE_ALLOWED_SEVERITIES
            ):
                return "agent tokens allow only low or medium severity on knowledge store"

        if "discovery_type" in arguments and arguments["discovery_type"] is not None:
            dtype = arguments["discovery_type"]
            if (
                not isinstance(dtype, str)
                or dtype not in _KNOWLEDGE_STORE_ALLOWED_TYPES
            ):
                return "agent token forbids discovery_type on knowledge store"

        tags = arguments.get("tags")
        if tags is not None:
            if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
                return "tags must be a list of strings"
            from src.knowledge_graph import normalize_tags

            reserved = [
                t for t in normalize_tags(tags) if t in _KNOWLEDGE_STORE_RESERVED_TAGS
            ]
            if reserved:
                return f"agent token forbids reserved tag(s): {', '.join(reserved)}"

        return None

    return "agent tokens may call only process_agent_update (and explicitly granted outcome/knowledge_store tools)"


def findings_refusal(
    *,
    operator_header: Optional[str] = None,
    agent_id_header: Optional[str] = None,
    scopes: FrozenSet[str] = DEFAULT_SCOPES,
) -> Optional[str]:
    """Why an agent-token POST /api/findings call is refused, or None to allow it."""
    if operator_header:
        return "agent tokens cannot be combined with an operator token"
    if agent_id_header:
        return "agent tokens cannot be combined with X-Agent-Id"
    if "findings" not in scopes:
        return "agent token lacks 'findings' scope for /api/findings"
    return None
