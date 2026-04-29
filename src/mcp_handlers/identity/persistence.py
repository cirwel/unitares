"""
Agent persistence, caching, and label management.

Houses _get_redis, _cache_session, ensure_agent_persisted, set_agent_label,
and DB helper functions for identity resolution.
"""

from __future__ import annotations

import asyncio
from typing import Optional, Dict, Any
from datetime import datetime, timedelta, timezone
import json
import os

from src.logging_utils import get_logger
from src.db import get_db

from config.governance_config import GovernanceConfig

logger = get_logger(__name__)
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
# =============================================================================
# CACHE LAYER (Redis)
# =============================================================================

_redis_cache = None

# Tight timeout for Redis writes from inside MCP handler paths. The MCP SDK's
# anyio task group can deadlock against asyncpg/Redis async calls (see
# CLAUDE.md "Known Issue: anyio-asyncio Conflict"); on deadlock we bail out
# and leave the in-memory session cache as the source of truth rather than
# hanging the request. Matches `_REDIS_RECOVERY_TIMEOUT` in
# src/mcp_handlers/middleware/identity_step.py.
#
# S21-a (2026-04-27): bumped from 0.5s -> 1.0s to cover two serial awaits
# (mint_guard `redis.get` + `redis.setex`) instead of one. Sized so each
# leg has the same headroom as before; under Redis pressure the guard
# read times out cleanly rather than silently no-opping the write.
_REDIS_WRITE_TIMEOUT = 1.0


def _nx_fail_closed_enabled() -> bool:
    return os.getenv("UNITARES_NX_FAIL_CLOSED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _metadata_public_agent_id(metadata: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return the canonical public agent handle from identity metadata."""
    if not isinstance(metadata, dict):
        return None
    return (
        metadata.get("public_agent_id")
        or metadata.get("agent_id")
        or metadata.get("structured_id")
    )

def _get_redis():
    """Lazy load Redis connection."""
    global _redis_cache
    if _redis_cache is None:
        try:
            from src.cache import get_session_cache
            _redis_cache = get_session_cache()
        except Exception as e:
            logger.debug(f"Redis not available: {e}")
            _redis_cache = False  # Mark as unavailable
    return _redis_cache if _redis_cache else None

# =============================================================================
# CACHE HELPERS
# =============================================================================

async def _cache_session(
    session_key: str,
    agent_uuid: str,
    display_agent_id: str = None,
    trajectory_required: bool = False,
    label: str = None,
    spawn_reason: Optional[str] = None,
    *,
    mint_guard: bool = False,
) -> None:
    """Cache session->UUID mapping in Redis, with optional display agent_id.

    Args:
        trajectory_required: If True, indicates this identity has a stored
            trajectory genesis. Lets PATH 1 skip the get_trajectory_status()
            call on subsequent hits (optimization hint).
        label: Auto-generated or user-set label to store alongside the binding.
        spawn_reason: Lineage reason for this binding, if the session was
            created by an explicit fork/mint path.
        mint_guard: Set True at PATH 3 mint sites only. When True, this
            function refuses to overwrite an existing in-memory or Redis
            binding for the same session_key whose agent_uuid differs from
            the one being written (S21-a, 2026-04-27 — see
            docs/ontology/s21-session-resolution-bypass-incident.md).
            PATH 2 / PATH 2.8 / set_agent_label callers leave it False —
            they are corrective writes from authoritative sources and may
            overwrite a stale Redis ghost.
    """
    in_memory_blocked = False
    try:
        from .shared import _session_identities

        binding = _session_identities.get(session_key) or {}

        # S21-a in-memory guard: refuse to ratify a different UUID over an
        # existing live binding when the caller is a PATH 3 mint site. This
        # is the load-bearing fix — PATH 3 ghost-mints used to silently
        # ratify themselves into _session_identities (and via the redis
        # write below, into the Redis slot), producing the 95% fleet-wide
        # ghost-fork rate documented in the S21 incident report.
        existing_uuid = binding.get("bound_agent_id") or binding.get("agent_uuid")
        if mint_guard and existing_uuid and existing_uuid != agent_uuid:
            logger.warning(
                "[S21A_OVERWRITE_BLOCKED] in-memory session_key=%s... "
                "existing=%s... attempted=%s... — refusing PATH 3 overwrite",
                session_key[:20], str(existing_uuid)[:8], agent_uuid[:8],
            )
            in_memory_blocked = True
        else:
            bind_count = binding.get("bind_count", 0)
            new_binding = {
                "bound_agent_id": agent_uuid,
                "agent_uuid": agent_uuid,
                "public_agent_id": display_agent_id or agent_uuid,
                "display_agent_id": display_agent_id,
                "agent_label": label or binding.get("agent_label"),
                "spawn_reason": spawn_reason or binding.get("spawn_reason"),
                "created_at": binding.get("created_at") or datetime.now(timezone.utc).isoformat(),
                "bound_at": datetime.now(timezone.utc).isoformat(),
                "bind_count": bind_count,
                "trajectory_required": trajectory_required,
            }
            if label:
                new_binding["label"] = label
            _session_identities[session_key] = new_binding
    except Exception as e:
        logger.debug(f"In-memory session cache update failed for {session_key[:20]}...: {e}")

    # If the in-memory guard fired, skip the Redis write too — the slot for
    # this session_key already maps to a different live UUID.
    if in_memory_blocked:
        return

    # Capture binding-time fingerprint for PATH 1 cross-check.
    # Council follow-up to identity-honesty (KG 2026-04-20T00:57:45): session
    # IDs of shape `agent-{uuid[:12]}` are UUID-derivable, so PATH 1 resume
    # by session_id alone has no ownership proof. Writing the bind fingerprint
    # here lets the PATH 1 lookup site compare against the resume-time
    # fingerprint and emit `identity_hijack_suspected` when they diverge.
    bind_ip_ua = None
    try:
        from ..context import get_session_signals
        _sig = get_session_signals()
        if _sig is not None:
            bind_ip_ua = getattr(_sig, "ip_ua_fingerprint", None)
    except Exception:
        bind_ip_ua = None

    # Mirror the bind fingerprint into the in-memory parallel dict consumed
    # by the sync PATH 1 check in shared.py:_check_path1_fingerprint_sync.
    # Only write on first bind so re-binds (legitimate or not) don't
    # silently overwrite the original owner's fingerprint.
    if bind_ip_ua:
        try:
            from .shared import _bind_fingerprints
            if session_key not in _bind_fingerprints:
                _bind_fingerprints[session_key] = bind_ip_ua
        except Exception as e:
            logger.debug(f"_bind_fingerprints update failed: {e}")

    session_cache = _get_redis()
    if session_cache:
        try:
            await asyncio.wait_for(
                _cache_session_redis_write(
                    session_cache,
                    session_key,
                    agent_uuid,
                    display_agent_id,
                    trajectory_required,
                    label,
                    spawn_reason,
                    bind_ip_ua,
                    mint_guard=mint_guard,
                ),
                timeout=_REDIS_WRITE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            # Likely the anyio-asyncio deadlock described in CLAUDE.md.
            # Bail out — the in-memory _session_identities write above is
            # still honored for this session; later reads miss Redis and
            # fall through to PostgreSQL resolution.
            logger.warning(
                f"[IDENTITY] Redis cache write timed out after "
                f"{_REDIS_WRITE_TIMEOUT}s for {session_key[:20]}... "
                f"— in-memory only (anyio-asyncio guard)"
            )
        except Exception as e:
            # WARNING level (v2.5.7): Cache failures can cause identity loss
            logger.warning(f"Redis cache write failed for session {session_key[:20]}...: {e}")


async def _cache_session_redis_write(
    session_cache,
    session_key: str,
    agent_uuid: str,
    display_agent_id: Optional[str],
    trajectory_required: bool,
    label: Optional[str],
    spawn_reason: Optional[str],
    bind_ip_ua: Optional[str],
    *,
    mint_guard: bool = False,
) -> None:
    """Inner: Redis writes for _cache_session, wrapped by asyncio.wait_for.

    Separated so the caller can bound execution time with a tight timeout
    to avoid hanging MCP handlers when the anyio-asyncio deadlock triggers.

    When ``mint_guard`` is True (set by PATH 3 mint sites), this function
    refuses to overwrite an existing Redis slot for the same session_key
    whose agent_id differs from the one being written. See S21-a for the
    incident this guards against.
    """
    # Store a richer payload whenever the binding carries metadata that
    # SessionCache.bind() cannot represent. S21-b: spawn_reason must survive
    # across cache hydration or lazy persistence writes NULL lineage.
    needs_rich_payload = bool(
        (display_agent_id and display_agent_id != agent_uuid)
        or label
        or spawn_reason
        or bind_ip_ua
        or trajectory_required
    )
    if needs_rich_payload:
        # Get raw Redis client for custom write
        from src.cache.redis_client import get_redis
        redis = await get_redis()
        if redis:
            key = f"session:{session_key}"

            # S21-a guard: refuse to overwrite a different live binding.
            if mint_guard and await _redis_slot_blocks_overwrite(redis, key, agent_uuid):
                return

            data = {
                "agent_id": agent_uuid,
                "bound_at": datetime.now(timezone.utc).isoformat(),
                "trajectory_required": trajectory_required,
            }
            if display_agent_id:
                data["display_agent_id"] = display_agent_id
                data["public_agent_id"] = display_agent_id
            if label:
                data["label"] = label
            if spawn_reason:
                data["spawn_reason"] = spawn_reason
            if bind_ip_ua:
                data["bind_ip_ua"] = bind_ip_ua
            await redis.setex(key, GovernanceConfig.SESSION_TTL_SECONDS, json.dumps(data))
            # Keep SessionCache's in-memory fallback coherent with the richer
            # raw Redis payload so subsequent lookups see the same binding
            # even if they fall back from Redis during this process lifetime.
            try:
                from src.cache import session_cache as _session_cache_mod
                _session_cache_mod._fallback_cache[session_key] = data
            except Exception:
                pass
        else:
            # Raw Redis unavailable but degraded-local cache is still usable.
            if mint_guard and await _session_cache_blocks_overwrite(session_cache, session_key, agent_uuid):
                return
            await session_cache.bind(session_key, agent_uuid)
            try:
                from src.cache import session_cache as _session_cache_mod
                existing = _session_cache_mod._fallback_cache.get(session_key, {})
                data = {
                    "agent_id": agent_uuid,
                    "bound_at": existing.get("bound_at") or datetime.now(timezone.utc).isoformat(),
                    "bind_count": existing.get("bind_count", 0),
                    "spawn_reason": spawn_reason or existing.get("spawn_reason"),
                }
                if display_agent_id:
                    data["display_agent_id"] = display_agent_id
                    data["public_agent_id"] = display_agent_id
                if trajectory_required:
                    data["trajectory_required"] = trajectory_required
                if bind_ip_ua:
                    data["bind_ip_ua"] = bind_ip_ua
                if label:
                    data["label"] = label
                _session_cache_mod._fallback_cache[session_key] = data
            except Exception:
                pass
    else:
        if mint_guard and await _session_cache_blocks_overwrite(session_cache, session_key, agent_uuid):
            return
        await session_cache.bind(session_key, agent_uuid)


async def _redis_slot_blocks_overwrite(redis, key: str, agent_uuid: str) -> bool:
    """Return True iff the raw-Redis slot for `key` already binds a *different*
    agent_uuid. Used by the S21-a guard inside _cache_session_redis_write —
    PATH 3 mint sites must not silently ratify a fresh ghost over a legitimate
    session binding.

    Redis read errors are warning-visible. By default they remain fail-open so
    a transient Redis hiccup doesn't break minting on truly empty slots. Set
    UNITARES_NX_FAIL_CLOSED=1 for deployments that prefer refusing PATH 3 cache
    writes when the guard cannot inspect the existing Redis slot.
    """
    try:
        existing_raw = await redis.get(key)
        if not existing_raw:
            return False
        if isinstance(existing_raw, bytes):
            try:
                existing_raw = existing_raw.decode()
            except Exception:
                return False
        if isinstance(existing_raw, dict):
            existing_data = existing_raw
        else:
            try:
                existing_data = json.loads(existing_raw)
            except (json.JSONDecodeError, TypeError):
                return False
        existing_id = existing_data.get("agent_id") if isinstance(existing_data, dict) else None
        if existing_id and existing_id != agent_uuid:
            existing_prefix = existing_id[:8] if isinstance(existing_id, str) else "?"
            logger.warning(
                "[S21A_OVERWRITE_BLOCKED] redis key=%s existing=%s... attempted=%s... "
                "— refusing PATH 3 overwrite",
                key, existing_prefix, agent_uuid[:8],
            )
            return True
    except Exception as e:
        fail_closed = _nx_fail_closed_enabled()
        logger.warning(
            "[S21A_REDIS_GUARD_READ_FAILED] key=%s attempted=%s... "
            "fail_closed=%s error=%s",
            key,
            agent_uuid[:8],
            fail_closed,
            e,
        )
        return fail_closed
    return False


async def _session_cache_blocks_overwrite(session_cache, session_key: str, agent_uuid: str) -> bool:
    """Return True iff the SessionCache view of `session_key` binds a different
    agent_uuid. Used by the S21-a guard for the bare-bind path (raw Redis
    unavailable, falling back through session_cache.bind).
    """
    try:
        existing = await session_cache.get(session_key)
        if isinstance(existing, dict):
            existing_id = existing.get("agent_id")
            if existing_id and existing_id != agent_uuid:
                existing_prefix = existing_id[:8] if isinstance(existing_id, str) else "?"
                logger.warning(
                    "[S21A_OVERWRITE_BLOCKED] session_cache session_key=%s... "
                    "existing=%s... attempted=%s... — refusing PATH 3 overwrite",
                    session_key[:20], existing_prefix, agent_uuid[:8],
                )
                return True
    except Exception as e:
        logger.debug(f"S21-a session_cache guard read failed: {e}")
    return False

# =============================================================================
# DB HELPERS
# =============================================================================

async def _agent_exists_in_postgres(agent_uuid: str) -> bool:
    """Check if agent exists in PostgreSQL."""
    try:
        db = get_db()
        identity = await db.get_identity(agent_uuid)
        return identity is not None
    except Exception:
        return False

async def _get_agent_status(agent_uuid: str) -> Optional[str]:
    """Fetch agent's status from PostgreSQL (e.g., 'active', 'archived', 'deleted').

    Returns None if agent not found or on error.
    """
    try:
        db = get_db()
        identity = await db.get_identity(agent_uuid)
        if identity and hasattr(identity, "status"):
            return identity.status
        return None
    except Exception:
        return None

async def _get_agent_label(agent_uuid: str) -> Optional[str]:
    """Fetch agent's label from PostgreSQL."""
    try:
        db = get_db()
        return await db.get_agent_label(agent_uuid)
    except Exception:
        return None

async def _get_agent_id_from_metadata(agent_uuid: str) -> Optional[str]:
    """Fetch the public/human-facing agent ID from identity metadata."""
    try:
        db = get_db()
        identity = await db.get_identity(agent_uuid)
        if identity and identity.metadata:
            metadata = identity.metadata
            return (
                metadata.get("public_agent_id")
                or metadata.get("agent_id")
                or metadata.get("structured_id")
            )
    except Exception:
        pass
    return None

async def _find_agent_by_label(label: str) -> Optional[str]:
    """Find agent UUID by label (for collision detection)."""
    try:
        db = get_db()
        return await db.find_agent_by_label(label)
    except Exception:
        return None


def _broadcaster():
    """Lazy accessor for the shared broadcaster. Returns None when broadcaster
    isn't importable (e.g., unit tests without a live server). Kept as a
    module-level function so tests can patch persistence._broadcaster."""
    try:
        from src.broadcaster import broadcaster as _b
        return _b
    except Exception:
        return None

# =============================================================================
# LAZY CREATION HELPERS (v2.4.1+)
# =============================================================================

async def ensure_agent_persisted(
    agent_uuid: str,
    session_key: str,
    *,
    parent_agent_id: Optional[str] = None,
    spawn_reason: Optional[str] = None,
    thread_id: Optional[str] = None,
    thread_position: Optional[int] = None,
) -> bool:
    """
    Persist agent to PostgreSQL if not already persisted.

    Call this from write operations (process_agent_update, identity(name=...))
    to ensure the agent exists before recording state.

    Args:
        agent_uuid: The agent's UUID (from resolve_session_identity)
        session_key: The session key for session binding
        parent_agent_id: UUID of parent agent (for thread/fork lineage)
        spawn_reason: Why this fork was created
        thread_id: Thread this agent belongs to
        thread_position: Node position within thread

    Returns:
        True if newly persisted, False if already existed
    """
    try:
        db = get_db()
        # Note: db.init() is called once at startup (mcp_server.py:1306).
        # Do NOT call it here — it was creating a new connection pool on every request.

        identity = await db.get_identity(agent_uuid)
        agent_record = await db.get_agent(agent_uuid)

        public_agent_id = None
        structured_id = None
        label = None
        try:
            meta = mcp_server.agent_metadata.get(agent_uuid)
            if meta:
                structured_id = getattr(meta, "structured_id", None)
                public_agent_id = getattr(meta, "public_agent_id", None) or structured_id
                label = getattr(meta, "label", None) or getattr(meta, "display_name", None)
        except Exception:
            pass

        if not public_agent_id or not label or not spawn_reason:
            session_cache = _get_redis()
            if session_cache:
                try:
                    cached = await session_cache.get(session_key)
                    if isinstance(cached, dict):
                        display_agent_id = cached.get("display_agent_id")
                        if not public_agent_id and display_agent_id and display_agent_id != agent_uuid:
                            public_agent_id = display_agent_id
                        if not label:
                            label = cached.get("label")
                        if not spawn_reason:
                            spawn_reason = cached.get("spawn_reason")
                except Exception as e:
                    logger.debug(f"Could not hydrate identity handles from session cache: {e}")
        if not public_agent_id or not label or not spawn_reason:
            try:
                from .shared import _session_identities

                cached = _session_identities.get(session_key)
                if isinstance(cached, dict):
                    display_agent_id = cached.get("display_agent_id") or cached.get("public_agent_id")
                    if not public_agent_id and display_agent_id and display_agent_id != agent_uuid:
                        public_agent_id = display_agent_id
                    if not label:
                        label = cached.get("agent_label") or cached.get("label")
                    if not spawn_reason:
                        spawn_reason = cached.get("spawn_reason")
            except Exception as e:
                logger.debug(f"Could not hydrate identity handles from in-memory session cache: {e}")

        if identity and agent_record:
            if public_agent_id and public_agent_id != agent_uuid:
                existing_public_id = None
                if getattr(identity, "metadata", None):
                    existing_public_id = _metadata_public_agent_id(identity.metadata)
                if not existing_public_id:
                    metadata_patch = {
                        "public_agent_id": public_agent_id,
                        "agent_id": public_agent_id,
                    }
                    if structured_id:
                        metadata_patch["structured_id"] = structured_id
                    if label:
                        metadata_patch["label"] = label
                    await db.upsert_identity(
                        agent_id=agent_uuid,
                        api_key_hash="",
                        parent_agent_id=parent_agent_id,
                        metadata=metadata_patch,
                    )
            return False  # Already fully persisted

        if not agent_record:
            await db.upsert_agent(
                agent_id=agent_uuid,
                api_key="",
                status="active",
                parent_agent_id=parent_agent_id,
                spawn_reason=spawn_reason,
                thread_id=thread_id,
                thread_position=thread_position,
            )

        if not identity:
            identity_metadata = {
                "source": "lazy_creation",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "total_updates": 0,  # Initialize counter for persistence
                "thread_id": thread_id,
                "thread_position": thread_position,
                "node_index": thread_position,  # AgentMetadata uses node_index
            }
            if public_agent_id and public_agent_id != agent_uuid:
                identity_metadata["public_agent_id"] = public_agent_id
                identity_metadata["agent_id"] = public_agent_id
            if structured_id:
                identity_metadata["structured_id"] = structured_id
            if label:
                identity_metadata["label"] = label
            await db.upsert_identity(
                agent_id=agent_uuid,
                api_key_hash="",
                parent_agent_id=parent_agent_id,
                spawn_reason=spawn_reason,
                metadata=identity_metadata,
            )
            identity = await db.get_identity(agent_uuid)

        # Create session binding once we have a durable identity row.
        if identity:
            client_info = {"agent_id": agent_uuid, "agent_uuid": agent_uuid, "lazy_created": True}
            if public_agent_id and public_agent_id != agent_uuid:
                client_info["public_agent_id"] = public_agent_id
            await db.create_session(
                session_id=session_key,
                identity_id=identity.identity_id,
                expires_at=datetime.now() + timedelta(hours=GovernanceConfig.SESSION_TTL_HOURS),
                client_type="mcp",
                client_info=client_info,
            )

        logger.info(f"Lazy-persisted agent on first work: {agent_uuid[:8]}...")
        return True

    except Exception as e:
        logger.warning(f"Failed to persist agent: {e}")
        return False

# =============================================================================
# LABEL MANAGEMENT
# =============================================================================

async def set_agent_label(agent_uuid: str, label: str, session_key: Optional[str] = None) -> bool:
    """
    Set display name for an agent.

    This is a simple UPDATE, not identity resolution.
    Label uniqueness is NOT enforced - duplicates get UUID suffix.

    If agent is not yet persisted (lazy creation), this will persist it first.
    """
    if not agent_uuid or not label:
        return False

    try:
        # Ensure agent is persisted before setting label (lazy creation support)
        if session_key:
            await ensure_agent_persisted(agent_uuid, session_key)

        db = get_db()
        identity_record = await db.get_identity(agent_uuid)
        existing_metadata = getattr(identity_record, "metadata", None) or {}
        existing_public_agent_id = _metadata_public_agent_id(existing_metadata)

        # Check for duplicate labels.
        # Resident-fork detector (ontology plan.md §S5 — inverted 2026-04-23):
        # Under ontology v2 a resident restart is expected to declare
        # parent_agent_id=<existing_uuid>. The event fires only when a
        # persistent-tagged collision occurs *without* that lineage
        # declaration — i.e., a silent/unlineaged fork. Lineage-declared
        # restarts log at INFO and rename silently. The rename still happens
        # (can't block onboard). See docs/ontology/identity.md §"Pattern —
        # Substrate-Earned Identity".
        existing = await _find_agent_by_label(label)
        if existing and existing != agent_uuid:
            new_label = f"{label}_{agent_uuid[:8]}"
            existing_is_resident = await db.agent_has_tag(existing, "persistent")

            # Resolve new agent's declared lineage. existing_metadata above
            # is the new-agent's own identity metadata (keyed by agent_uuid
            # at line 452), despite the misleading "existing_" prefix.
            declared_parent: Optional[str] = None
            if isinstance(existing_metadata, dict):
                dp = existing_metadata.get("parent_agent_id")
                if isinstance(dp, str) and dp:
                    declared_parent = dp
            if declared_parent is None:
                try:
                    meta_map = getattr(mcp_server, "agent_metadata", None)
                    meta = meta_map.get(agent_uuid) if meta_map else None
                    dp = getattr(meta, "parent_agent_id", None) if meta else None
                    if isinstance(dp, str) and dp:
                        declared_parent = dp
                except Exception:
                    pass
            lineage_declared = declared_parent == existing

            if existing_is_resident and not lineage_declared:
                logger.warning(
                    "[RESIDENT_FORK] unlineaged collision on persistent agent %s: "
                    "fresh onboard minted %s with label %r and parent_agent_id=%r "
                    "(expected %s). Renaming to %r. Under v2 ontology a resident "
                    "restart declares parent_agent_id=<existing_uuid>; missing "
                    "lineage suggests rotation wipe, anchor corruption, or "
                    "misconfigured bootstrap. See docs/ontology/identity.md "
                    "§'Pattern — Substrate-Earned Identity'.",
                    existing[:8], agent_uuid[:8], label, declared_parent,
                    existing[:8], new_label,
                )
                b = _broadcaster()
                if b is not None:
                    try:
                        await b.broadcast_event(
                            event_type="resident_fork_detected",
                            agent_id=agent_uuid,
                            payload={
                                "existing_agent_id": existing,
                                "label": label,
                                "new_label": new_label,
                                "declared_parent": declared_parent,
                            },
                        )
                    except Exception as e:
                        logger.warning(
                            f"[RESIDENT_FORK] broadcast_event failed: {e}"
                        )
            elif existing_is_resident and lineage_declared:
                logger.info(
                    "[RESIDENT_LINEAGE] restart of persistent agent %s by %s "
                    "with declared parent — expected. Renaming to %r.",
                    existing[:8], agent_uuid[:8], new_label,
                )
            else:
                logger.info(f"Label collision, using: {new_label}")
            label = new_label

        # Update agent label using the proper backend method
        success = await db.update_agent_fields(agent_uuid, label=label)

        if success:
            # Sync label to runtime cache so compute_agent_signature can find it
            structured_id = None
            public_agent_id = existing_public_agent_id
            try:
                if agent_uuid in mcp_server.agent_metadata:
                    meta = mcp_server.agent_metadata[agent_uuid]
                    meta.label = label

                    # Generate structured_id if missing (migration for pre-v2.5.0 agents)
                    if not getattr(meta, 'structured_id', None):
                        try:
                            from ..support.naming_helpers import detect_interface_context, generate_structured_id
                            from ..context import get_context_client_hint
                            context = detect_interface_context()
                            existing_ids = [
                                getattr(m, 'structured_id', None)
                                for m in mcp_server.agent_metadata.values()
                                if getattr(m, 'structured_id', None)
                            ]
                            meta.structured_id = generate_structured_id(
                                context=context,
                                existing_ids=existing_ids,
                                client_hint=get_context_client_hint()
                            )
                            logger.info(f"Migrated structured_id: {meta.structured_id}")
                        except Exception as e:
                            logger.debug(f"Could not generate structured_id: {e}")

                    structured_id = getattr(meta, "structured_id", None)
                    if not public_agent_id:
                        public_agent_id = getattr(meta, "public_agent_id", None)
                    if not public_agent_id:
                        public_agent_id = existing_public_agent_id or structured_id
                    if public_agent_id and getattr(meta, "public_agent_id", None) != public_agent_id:
                        meta.public_agent_id = public_agent_id
                    logger.info(f"Synced label '{label}' to existing cache entry for {agent_uuid[:8]}")
                else:
                    # Agent not in cache yet - create proper AgentMetadata entry
                    # Import the real AgentMetadata class to avoid missing attribute errors
                    from src.agent_state import AgentMetadata
                    now = datetime.now(timezone.utc).isoformat()
                    meta = AgentMetadata(
                        agent_id=agent_uuid,
                        status='active',
                        created_at=now,
                        last_update=now,
                        public_agent_id=public_agent_id,
                    )
                    meta.label = label  # Set label after creation
                    meta.agent_uuid = agent_uuid  # Set UUID attribute

                    # Generate structured_id (three-tier identity model v2.5.0+)
                    try:
                        from ..support.naming_helpers import detect_interface_context, generate_structured_id
                        from ..context import get_context_client_hint
                        context = detect_interface_context()
                        existing_ids = [
                            getattr(m, 'structured_id', None)
                            for m in mcp_server.agent_metadata.values()
                            if getattr(m, 'structured_id', None)
                        ]
                        meta.structured_id = generate_structured_id(
                            context=context,
                            existing_ids=existing_ids,
                            client_hint=get_context_client_hint()
                        )
                        logger.info(f"Generated structured_id: {meta.structured_id}")
                    except Exception as e:
                        logger.debug(f"Could not generate structured_id: {e}")

                    structured_id = getattr(meta, "structured_id", None)
                    if not public_agent_id:
                        public_agent_id = existing_public_agent_id or structured_id
                    if public_agent_id:
                        meta.public_agent_id = public_agent_id
                    mcp_server.agent_metadata[agent_uuid] = meta
                    logger.info(f"Created cache entry with label '{label}' for {agent_uuid[:8]}")

                # Also update session binding cache so get_or_create_session_identity returns correct label
                try:
                    from .shared import _session_identities
                    for session_key, binding in _session_identities.items():
                        if binding.get("bound_agent_id") == agent_uuid or binding.get("agent_uuid") == agent_uuid:
                            binding["agent_label"] = label
                            if public_agent_id:
                                binding["public_agent_id"] = public_agent_id
                                binding["display_agent_id"] = binding.get("display_agent_id") or public_agent_id
                            logger.debug(f"Updated session binding label for {session_key}")
                except Exception as e:
                    logger.debug(f"Could not update session binding cache: {e}")
            except Exception as e:
                logger.warning(f"Runtime cache sync failed: {e}")

            # Sync label + public identity handles into core.identities.metadata
            try:
                if identity_record:
                    identity_metadata = {"label": label}
                    if structured_id:
                        identity_metadata["structured_id"] = structured_id
                    public_to_persist = public_agent_id or existing_public_agent_id
                    if public_to_persist:
                        identity_metadata["public_agent_id"] = public_to_persist
                        identity_metadata["agent_id"] = public_to_persist
                    await db.upsert_identity(
                        agent_id=agent_uuid,
                        api_key_hash="",
                        metadata=identity_metadata,
                    )
            except Exception as e:
                logger.debug(f"Could not sync identity metadata after label update: {e}")

            # Invalidate any other cached data
            redis = _get_redis()
            if redis:
                try:
                    from src.cache import get_metadata_cache
                    await get_metadata_cache().invalidate(agent_uuid)
                except Exception:
                    pass

        return success

    except Exception as e:
        logger.warning(f"Failed to set label: {e}")
        return False
