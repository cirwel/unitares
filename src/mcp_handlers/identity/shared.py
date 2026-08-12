"""
Shared identity utilities used by identity_v2.py and other modules.

This module contains the shared data structures and utility functions that
were previously in identity.py. Separating these prevents circular imports
and makes the dependency structure cleaner.

Data structures:
- _session_identities: In-memory session -> agent binding cache
- _uuid_prefix_index: UUID prefix -> full UUID for O(1) lookup

Key functions:
- get_bound_agent_id(): Get bound agent for current session
- is_session_bound(): Check if session has bound identity
- make_client_session_id(): Generate stable session ID from UUID
- require_write_permission(): Check if writes are allowed
- _get_lineage(): Get agent lineage chain
"""

from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timezone
from mcp.types import TextContent
import os

from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
from src.mcp_handlers.context import get_session_signals
from config.governance_config import session_fingerprint_check_mode
logger = get_logger(__name__)

# =============================================================================
# SHARED SESSION STATE
# =============================================================================
# This is the in-memory cache of session -> agent bindings.
# It's shared across all identity modules to prevent binding conflicts.

_session_identities: Dict[str, Dict[str, Any]] = {}

# O(1) lookup index: uuid_prefix (12 chars) -> full UUID
# Populated when identity() registers a stable session binding
_uuid_prefix_index: Dict[str, str] = {}

# Parallel dict: session_key -> binding-time ip_ua_fingerprint.
# Written by _cache_session (persistence.py) and the FALLBACK scan path
# below — only when the key is not already present, so the legitimate
# first bind is never silently overwritten by a later mismatched arrival
# (e.g., after a server restart wipes _uuid_prefix_index). Read by the
# PATH 1 sync fingerprint cross-check in _get_identity_record_sync.
# Mirrors the async-path check at resolution.py:441-487; closes the
# residual sync half of KG 2026-04-20T00:57:45.
_bind_fingerprints: Dict[str, str] = {}

# =============================================================================
# UUID PREFIX INDEX
# =============================================================================

def _register_uuid_prefix(uuid_prefix: str, full_uuid: str) -> None:
    """Register a UUID prefix -> full UUID mapping for O(1) lookup.

    Handles collision detection: if prefix already exists for a different UUID,
    logs a warning (collisions are rare with 12 chars but possible).
    """
    existing = _uuid_prefix_index.get(uuid_prefix)
    if existing and existing != full_uuid:
        logger.warning(f"[UUID_PREFIX_COLLISION] Prefix {uuid_prefix} already maps to {existing[:8]}..., not updating to {full_uuid[:8]}...")
        return
    _uuid_prefix_index[uuid_prefix] = full_uuid
    logger.debug(f"[UUID_PREFIX] Registered {uuid_prefix} -> {full_uuid[:8]}...")

def _lookup_uuid_by_prefix(uuid_prefix: str) -> Optional[str]:
    """O(1) lookup of full UUID by prefix. Returns None if not found."""
    return _uuid_prefix_index.get(uuid_prefix)

# =============================================================================
# SESSION KEY DERIVATION
# =============================================================================

def _get_session_key(arguments: Optional[Dict[str, Any]] = None, session_id: Optional[str] = None) -> str:
    """
    DEPRECATED: Use ``await identity_v2.derive_session_key(signals, arguments)`` instead.

    This sync version is kept for callers that cannot be made async (e.g.,
    ``_get_identity_record_sync``). New code should use the unified async version.

    Precedence:
    1) explicit session_id argument
    2) arguments["client_session_id"] (injected by SSE wrappers)
    3) contextvars session_key (set at dispatch entry)
    4) fallback to a stable per-process key (stdio/single-user)
    """
    if session_id:
        logger.debug("_get_session_key: using explicit session_id")
        return str(session_id)
    if arguments and arguments.get("client_session_id"):
        logger.debug("_get_session_key: using client_session_id")
        return str(arguments["client_session_id"])

    # Check contextvars for session key (set at SSE dispatch entry)
    from ..context import get_context_session_key
    context_key = get_context_session_key()
    if context_key:
        logger.debug("_get_session_key: using context session_key")
        return str(context_key)

    # STABLE FALLBACK: Use only PID (no timestamp) for single-user scenarios
    fallback = f"stdio:{os.getpid()}"
    logger.debug("_get_session_key: using stable stdio fallback")
    return fallback

# =============================================================================
# SESSION ID FORMATTING
# =============================================================================

def make_client_session_id(agent_uuid: str) -> str:
    """
    Generate a stable client_session_id from an agent UUID.

    This is THE canonical formatter for session IDs. All code that generates
    session IDs must use this function to prevent format drift.

    Format: "agent-{uuid_prefix_12}"
    Example: "agent-5e728ecb1234"

    Args:
        agent_uuid: The full 36-char UUID

    Returns:
        Stable session ID in format "agent-{uuid[:12]}"
    """
    if not agent_uuid or len(agent_uuid) < 12:
        raise ValueError(f"Invalid UUID: {agent_uuid}")
    return f"agent-{agent_uuid[:12]}"

# =============================================================================
# BOUND AGENT LOOKUP
# =============================================================================

def _check_path1_fingerprint_sync(key: str, agent_uuid: Optional[str]) -> bool:
    """Cross-check binding-time fingerprint against current request's fingerprint.

    Mirrors the async-path check at resolution.py:441-487. Closes the residual
    sync half of KG 2026-04-20T00:57:45 (PATH 1 hijack via agent-{uuid12}
    prefix-bind). Gated by UNITARES_SESSION_FINGERPRINT_CHECK
    (off / log (default) / strict).

    Returns:
        True  — caller should return the cached/resolved binding (match, off
                mode, or no fingerprint data on either side).
        False — strict-mode mismatch; caller should fall through to an empty
                record so the legitimate owner can still resume from the
                correct fingerprint.
    """
    bound_fp = _bind_fingerprints.get(key)
    if not bound_fp:
        # Pre-fingerprint binding, cold cache, or background-task caller path
        # that bypassed _cache_session. Visible at debug for the future case
        # where a non-MCP-dispatch caller hits this code (so it doesn't
        # silently bypass the gate).
        logger.debug(
            f"[PATH1_SYNC_FP_SKIP] no bind fingerprint for {key[:20]}... "
            f"— pre-fingerprint binding or cold cache"
        )
        return True

    try:
        sig = get_session_signals()
        current_fp = getattr(sig, "ip_ua_fingerprint", None) if sig else None
    except Exception:
        current_fp = None

    if not current_fp:
        logger.debug(
            f"[PATH1_SYNC_FP_SKIP] no current fingerprint for {key[:20]}... "
            f"— likely background-task caller without session signals"
        )
        return True

    if current_fp == bound_fp:
        return True

    try:
        mode = session_fingerprint_check_mode()
    except Exception:
        mode = "log"

    if mode == "off":
        return True

    logger.warning(
        "[PATH1_FINGERPRINT_MISMATCH] session_key=%s... bound_fp=%s current_fp=%s "
        "— suspected hijack of agent=%s... (mode=%s, surface=sync)",
        key[:20],
        bound_fp[:16],
        current_fp[:16],
        (agent_uuid or "?")[:8],
        mode,
    )

    # Fire-and-forget broadcast. Sync context can't await; create_task on the
    # running loop if there is one. Telemetry only — never load-bearing for
    # the gate decision.
    try:
        import asyncio
        from .handlers import _broadcaster
        b = _broadcaster()
        if b is not None:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None
            if loop is not None and loop.is_running():
                loop.create_task(
                    b.broadcast_event(
                        event_type="identity_hijack_suspected",
                        agent_id=agent_uuid,
                        payload={
                            "path": "path1_sync_session_id",
                            "mode": mode,
                            "source": "path1_sync_fingerprint_mismatch",
                            "bind_fp_prefix": bound_fp[:8],
                            "current_fp_prefix": current_fp[:8],
                        },
                    )
                )
    except Exception as be:
        logger.warning(f"[PATH1_FINGERPRINT_MISMATCH] broadcast failed: {be}")

    return mode != "strict"


def _strict_mismatch_record() -> Dict[str, Any]:
    """Return the empty-binding record served when strict-mode fingerprint
    check rejects a PATH 1 sync resume."""
    return {
        "bound_agent_id": None,
        "api_key": None,
        "bound_at": None,
        "bind_count": 0,
        "_session_key_type": "path1_sync_fingerprint_strict_mismatch",
    }


def _get_identity_record_sync(session_id: Optional[str] = None, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Get identity record for a session (synchronous, in-memory only).

    This is a lightweight sync version that only checks in-memory cache.
    For full PostgreSQL support, use the async version in identity_v2.py.
    """

    key = _get_session_key(arguments=arguments, session_id=session_id)

    # Check in-memory cache first
    if key in _session_identities:
        cached = _session_identities[key]
        if _check_path1_fingerprint_sync(key, cached.get("bound_agent_id")):
            return cached
        return _strict_mismatch_record()

    # SPECIAL CASE: agent-{uuid12} format session IDs
    if key.startswith("agent-"):
        uuid_prefix = key[6:]  # Remove "agent-" prefix

        # O(1) LOOKUP via index
        full_uuid = _lookup_uuid_by_prefix(uuid_prefix)
        if full_uuid:
            meta = mcp_server.agent_metadata.get(full_uuid)
            if meta:
                if not _check_path1_fingerprint_sync(key, full_uuid):
                    return _strict_mismatch_record()
                _session_identities[key] = {
                    "bound_agent_id": full_uuid,
                    "api_key": getattr(meta, 'api_key', None),
                    "spawn_reason": getattr(meta, "spawn_reason", None),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "bind_count": 0,
                }
                return _session_identities[key]

        # FALLBACK: Scan agent metadata
        for agent_uuid, meta in mcp_server.agent_metadata.items():
            if agent_uuid.startswith(uuid_prefix):
                _register_uuid_prefix(uuid_prefix, agent_uuid)
                # Capture binding-time fingerprint on FIRST bind only.
                # After a restart wipes _uuid_prefix_index, FALLBACK fires
                # again — without this guard an attacker on a different
                # IP/UA could overwrite the legitimate bind fingerprint.
                if key not in _bind_fingerprints:
                    try:
                        sig = get_session_signals()
                        current_fp = getattr(sig, "ip_ua_fingerprint", None) if sig else None
                        if current_fp:
                            _bind_fingerprints[key] = current_fp
                    except Exception:
                        pass
                _session_identities[key] = {
                    "bound_agent_id": agent_uuid,
                    "api_key": getattr(meta, 'api_key', None),
                    "spawn_reason": getattr(meta, "spawn_reason", None),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "bind_count": 0,
                }
                return _session_identities[key]

        # No match
        return {
            "bound_agent_id": None,
            "api_key": None,
            "bound_at": None,
            "bind_count": 0,
            "_session_key_type": "agent_prefix_not_found",
        }

    # Not in cache, return empty (async version handles DB lookup)
    if key not in _session_identities:
        _session_identities[key] = {
            "bound_agent_id": None,
            "api_key": None,
            "bound_at": None,
            "bind_count": 0,
        }
    return _session_identities[key]

def get_bound_agent_id(session_id: Optional[str] = None, arguments: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Get currently bound agent_id (if any) for this session.

    PRIORITY: Checks contextvars first (set at dispatch entry) for consistency
    across all tools in the same request. Falls back to identity record lookup.
    """
    # PRIORITY 0: Check contextvars (set at dispatch entry)
    try:
        from ..context import get_context_agent_id
        context_agent_id = get_context_agent_id()
        if context_agent_id:
            logger.debug(f"get_bound_agent_id: using context agent_id={context_agent_id[:8]}...")
            return context_agent_id
    except Exception:
        pass

    # FALLBACK: Use identity record lookup
    rec = _get_identity_record_sync(session_id=session_id, arguments=arguments)
    return rec.get("bound_agent_id")

def is_session_bound(session_id: Optional[str] = None, arguments: Optional[Dict[str, Any]] = None) -> bool:
    """Check if session has bound identity."""
    return get_bound_agent_id(session_id=session_id, arguments=arguments) is not None

# =============================================================================
# WRITE PERMISSION
# =============================================================================

def require_write_permission(arguments: Optional[Dict[str, Any]] = None) -> Tuple[bool, Optional[TextContent]]:
    """
    Check if writes are allowed (bound=true).

    Rules:
    - Writes are allowed only when bound=true
    - Read-only operations work even when unbound

    Returns:
        (allowed, error_response) - allowed is True if writes are permitted
    """
    from ..utils import error_response as make_error

    if not is_session_bound(arguments=arguments):
        return False, make_error(
            "Write operations require session binding",
            details={"error_type": "write_requires_binding"},
            recovery={
                "action": "Call any tool (e.g. process_agent_update) to auto-bind identity",
                "note": "Read-only operations work without binding, but writes require bound=true"
            }
        )
    return True, None

# =============================================================================
# LINEAGE HELPERS
# =============================================================================

def _get_lineage(agent_id: str) -> list:
    """Get full lineage as list [oldest_ancestor, ..., parent, self]."""

    lineage = []
    current = agent_id
    seen = set()

    while current and current not in seen:
        seen.add(current)
        lineage.append(current)
        meta = mcp_server.agent_metadata.get(current)
        if meta and meta.parent_agent_id:
            current = meta.parent_agent_id
        else:
            break

    lineage.reverse()  # Oldest ancestor first
    return lineage

# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Data structures
    '_session_identities',
    '_uuid_prefix_index',
    '_bind_fingerprints',
    # UUID prefix functions
    '_register_uuid_prefix',
    '_lookup_uuid_by_prefix',
    # Session key functions
    '_get_session_key',
    'make_client_session_id',
    # Identity lookup
    'get_bound_agent_id',
    'is_session_bound',
    '_get_identity_record_sync',
    # Write permission
    'require_write_permission',
    # Lineage
    '_get_lineage',
]
