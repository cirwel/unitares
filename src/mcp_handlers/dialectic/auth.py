"""Shared dialectic authorization helpers.

Centralizes identity resolution and optional session-ownership enforcement for
dialectic submit handlers so authorization policy is defined in one place.
"""

from __future__ import annotations

import uuid as _uuid
from typing import Any, Dict, Optional, Sequence, Tuple

from ..utils import error_response, require_registered_agent
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server


def _looks_like_uuid(value: str) -> bool:
    """True when `value` parses as a v4 UUID (the identity-key shape)."""
    try:
        _uuid.UUID(value, version=4)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _canonicalize_from_metadata(provided: str) -> Optional[str]:
    """
    Map a caller-supplied agent reference onto its UUID via the warm metadata
    cache. Accepts the UUID itself, `public_agent_id`, or `structured_id`.

    Deliberately does NOT match on `label`: labels are self-claimed at onboard
    and never server-verified, so resolving identity by label is a standing
    invariant violation (no-lookup-by-label). `require_registered_agent` is
    laxer here; the dialectic path is not.
    """
    try:
        ensure_metadata_loaded = getattr(mcp_server, "ensure_metadata_loaded", None)
        if ensure_metadata_loaded:
            ensure_metadata_loaded()
    except Exception:
        pass

    metadata = getattr(mcp_server, "agent_metadata", None) or {}

    # The reference already IS an identity key.
    if provided in metadata:
        return provided

    for uuid_key, meta in metadata.items():
        if provided in (
            getattr(meta, "public_agent_id", None),
            getattr(meta, "structured_id", None),
        ):
            return uuid_key

    return None


async def resolve_dialectic_agent_id(
    arguments: Dict[str, Any],
    *,
    enforce_session_ownership: bool = False,
) -> Tuple[Optional[str], Optional[Sequence[Any]]]:
    """
    Resolve caller identity for dialectic submit tools.

    CONTRACT: on success this ALWAYS returns an agent **UUID** — never a public
    handle, structured id, or label. Every consumer compares the result against
    `session.paused_agent_id` / `session.reviewer_agent_id` and writes it into
    `core.dialectic_messages.agent_id`, all of which hold UUIDs. Returning a
    handle silently breaks every submit path (#1414).

    - No `agent_id`: use `require_registered_agent` (bound/session identity),
      which already returns the UUID.
    - With `agent_id`: canonicalize the reference to its UUID, verify
      registration, and optionally enforce ownership.

    `arguments["_agent_uuid"]` is deliberately NOT consulted here. Pydantic
    validation preserves unknown caller keys (`middleware/params_step.py`) and
    `_agent_uuid` is not scrubbed from caller input, so trusting it would let
    `agent_id=<mine> _agent_uuid=<victim>` impersonate the victim.
    """
    provided = arguments.get("agent_id")
    if isinstance(provided, str):
        provided = provided.strip()

    if not provided:
        agent_id, error = require_registered_agent(arguments)
        if error:
            return None, [error]
        return agent_id, None

    unresolvable_recovery = {
        "error_type": "agent_ref_unresolvable",
        "action": (
            "Pass your agent UUID (the `uuid` from onboard()/identity()), or omit "
            "agent_id entirely to use your bound session identity. Do NOT call "
            "onboard() — that mints a NEW identity and will not help."
        ),
        "note": (
            "agent_id accepts a UUID or a server-issued public handle; labels are "
            "not identity keys."
        ),
        "related_tools": ["identity", "dialectic"],
    }

    try:
        resolved = _canonicalize_from_metadata(provided)

        if resolved is None:
            # Cold metadata cache: a bare UUID can still be confirmed straight
            # from Postgres. A handle cannot (no index-free lookup that does not
            # re-introduce label matching), so it errors — see the follow-up note
            # in the PR body.
            if _looks_like_uuid(provided):
                from ..identity.handlers import _agent_exists_in_postgres

                if await _agent_exists_in_postgres(provided):
                    resolved = provided

        if resolved is None:
            return None, [error_response(
                f"Agent reference '{provided[:8]}...' could not be resolved to a "
                "registered agent",
                recovery=unresolvable_recovery,
            )]
    except Exception:
        return None, [error_response(
            f"Could not verify agent '{provided[:8]}...' registration",
            recovery={
                "action": "Retry or call identity() to confirm your current binding.",
                "related_tools": ["identity", "dialectic"],
            },
        )]

    if enforce_session_ownership:
        try:
            from ..context import get_context_agent_id
            from ..utils import verify_agent_ownership

            bound_uuid = get_context_agent_id()
            # UUID-vs-UUID: `get_context_agent_id` returns the bound UUID, so the
            # canonicalized `resolved` is the only flavor this comparison can be
            # correct against.
            if bound_uuid and not verify_agent_ownership(resolved, arguments):
                return None, [error_response(
                    "agent_id override is not allowed for this call. Use your bound identity.",
                    error_code="AUTH_REQUIRED",
                    error_category="auth_error",
                    recovery={
                        "action": "Remove agent_id and retry, or bind to the reviewer identity first.",
                        "related_tools": ["identity", "bind_session"],
                    },
                )]
        except Exception:
            return None, [error_response(
                "Could not verify session ownership for provided agent_id",
                error_code="AUTH_REQUIRED",
                error_category="auth_error",
                recovery={
                    "action": "Retry without agent_id override or re-bind session identity.",
                    "related_tools": ["identity", "bind_session"],
                },
            )]

    return resolved, None
