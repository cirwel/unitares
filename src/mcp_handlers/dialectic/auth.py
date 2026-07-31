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


class AmbiguousAgentRef(Exception):
    """A caller-supplied handle names more than one registered identity."""

    def __init__(self, provided: str, matches: Sequence[str]):
        super().__init__(f"{provided!r} matches {len(matches)} identities")
        self.provided = provided
        self.matches = list(matches)


def _canonicalize_from_metadata(provided: str) -> Optional[str]:
    """
    Map a caller-supplied agent reference onto its UUID via the warm metadata
    cache. Accepts the UUID itself, `public_agent_id`, or `structured_id`.

    `public_agent_id` IS NOT UNIQUE and must never be resolved first-match-wins.
    On the live DB (2026-07-31) 4294 of 4492 identities carrying a handle share
    it with at least one other identity — 269 distinct colliding handles, the
    largest set being `mcp_20260414` with 479 holders; restricted to
    `status='active'` it is still 1848 of 2030 (91%). The metadata cache is
    built from `list_identities(... ORDER BY i.created_at DESC)`, so a naive
    scan returns the NEWEST holder of a handle — typically a live co-resident,
    not the caller. Two rules keep this honest:

    1. **Bound identity first.** If the reference is an alias of the agent the
       server already bound this session to, return the bound UUID. This is the
       overwhelmingly common case (`middleware/params_step.py` only lets a
       non-bound `agent_id` reach a dialectic call when it IS a bound alias),
       and it mirrors `require_agent_id` (`support/agent_auth.py`), which does
       the same rewrite before any fleet-wide scan.
    2. **Otherwise require uniqueness.** Collect every match and raise
       `AmbiguousAgentRef` when more than one identity answers to the handle,
       rather than silently picking one. Guessing here writes another agent's
       UUID into `core.dialectic_messages.agent_id`.

    Matching on `label` is confined to rule 1. Searching the fleet BY label is a
    standing invariant violation (no-lookup-by-label) because labels are
    self-claimed at onboard and never server-verified — but confirming that a
    string is an alias of the identity the server itself bound the caller to is
    not a lookup; it resolves to the caller's own already-verified UUID and
    cannot name anyone else. `params_step._bound_identity_aliases` already
    blesses label-of-bound, so rejecting it here would only false-reject.
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

    # Rule 1: an alias of the bound identity resolves to the bound UUID.
    try:
        from ..context import get_context_agent_id

        bound_uuid = get_context_agent_id()
        if bound_uuid:
            bound_meta = metadata.get(bound_uuid)
            if bound_meta is not None and provided in (
                getattr(bound_meta, "public_agent_id", None),
                getattr(bound_meta, "structured_id", None),
                getattr(bound_meta, "label", None),
            ):
                return bound_uuid
    except Exception:
        pass

    # Rule 2: fleet-wide scan, but only an unambiguous hit is usable.
    matches = [
        uuid_key
        for uuid_key, meta in metadata.items()
        if provided in (
            getattr(meta, "public_agent_id", None),
            getattr(meta, "structured_id", None),
        )
    ]

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AmbiguousAgentRef(provided, matches)

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
    except AmbiguousAgentRef as ambiguous:
        # Must precede the generic handler below — otherwise a collision is
        # reported as a transient "could not verify" and invites a retry that
        # will fail identically.
        return None, [error_response(
            f"Agent reference '{provided[:8]}...' is ambiguous: it names "
            f"{len(ambiguous.matches)} registered identities",
            error_code="AMBIGUOUS_AGENT_REF",
            error_category="auth_error",
            recovery={
                "error_type": "agent_ref_ambiguous",
                "action": (
                    "Pass your agent UUID (the `uuid` from onboard()/identity()) "
                    "instead of the handle. Do NOT call onboard() — that mints a "
                    "NEW identity and will not help."
                ),
                "note": (
                    "public_agent_id is not unique across the fleet, so this "
                    "handle cannot identify one agent. Resolving it by guess "
                    "would attribute your message to someone else."
                ),
                "match_count": len(ambiguous.matches),
                "related_tools": ["identity", "dialectic"],
            },
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
