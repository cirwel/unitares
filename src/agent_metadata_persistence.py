"""
Agent metadata persistence.

Loading/saving metadata from PostgreSQL, JSON snapshots, cache management.
"""

from __future__ import annotations

import os
import json
import time
import fcntl
import asyncio
from pathlib import Path
from datetime import datetime, timezone

from src.logging_utils import get_logger
from src.agent_metadata_model import (
    project_root,
    AgentMetadata,
    agent_metadata,
    _metadata_loading_lock,
    _metadata_cache_state,
    _metadata_loaded_event,
)
# Use module import for patchable access to loading flags
from src import agent_metadata_model as _model

logger = get_logger(__name__)

# Path to metadata file
METADATA_FILE = Path(project_root) / "data" / "agent_metadata.json"

# Metadata backend configuration
UNITARES_METADATA_BACKEND = os.getenv("UNITARES_METADATA_BACKEND", "postgres").strip().lower()
UNITARES_METADATA_WRITE_JSON_SNAPSHOT = os.getenv("UNITARES_METADATA_WRITE_JSON_SNAPSHOT", "0").strip().lower() in (
    "1", "true", "yes",
)

_metadata_backend_resolved: str | None = None

# Prevent fire-and-forget tasks from being GC'd (P001). See
# src/agent_loop_detection.py:40 for the canonical pattern.
_background_tasks: set[asyncio.Task] = set()


def _track_background_task(task: asyncio.Task) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _resolve_metadata_backend() -> str:
    """
    Resolve metadata backend.

    - json: always use METADATA_FILE (legacy)
    - postgres: use PostgreSQL (default)
    - auto: use PostgreSQL
    """
    global _metadata_backend_resolved
    if _metadata_backend_resolved:
        return _metadata_backend_resolved

    backend = UNITARES_METADATA_BACKEND
    if backend == "json":
        _metadata_backend_resolved = backend
        return backend

    _metadata_backend_resolved = "postgres"
    return _metadata_backend_resolved


def _write_metadata_snapshot_json_sync() -> None:
    """Write JSON snapshot of in-memory agent_metadata for backward compatibility."""
    if not UNITARES_METADATA_WRITE_JSON_SNAPSHOT:
        return
    METADATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    metadata_lock_file = METADATA_FILE.parent / ".metadata.lock"
    lock_fd = os.open(str(metadata_lock_file), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        snapshot = {aid: meta.to_dict() for aid, meta in agent_metadata.items() if isinstance(meta, AgentMetadata)}
        with open(METADATA_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except Exception:
            pass
        os.close(lock_fd)


async def _load_metadata_from_postgres_async() -> dict:
    """
    Load agent metadata from PostgreSQL into AgentMetadata dict.

    Cold-start path: shaped to avoid per-agent awaits. With ~3000 agents
    the prior per-agent provisional-lineage fetch + per-agent
    `metadata_cache.set` produced a ~17s first-call tax on observe (see
 `` v0.2 RESOLUTION). Now:
    bulk PG reads up front (list_agents, get_identities_batch,
    get_provisional_lineage_set), per-agent computation is sync
    (compute_trust_tier with prefetched_provisional), and redis
    hydration is fire-and-forget after the in-memory dict is ready.
    """
    from src import agent_storage

    agents = await agent_storage.list_agents(
        limit=10000,
        include_archived=True,
        include_deleted=False,
    )

    result = {}
    now = datetime.now(timezone.utc).isoformat()

    for agent in agents:
        meta = AgentMetadata(
            agent_id=agent.agent_id,
            status=agent.status or "active",
            created_at=agent.created_at.isoformat() if agent.created_at else now,
            last_update=(agent.last_activity_at or agent.updated_at).isoformat() if (agent.last_activity_at or agent.updated_at) else now,
            tags=agent.tags or [],
            notes=agent.notes or "",
            purpose=agent.purpose,
            parent_agent_id=agent.parent_agent_id,
            spawn_reason=agent.spawn_reason,
            health_status=agent.health_status or "unknown",
            api_key=agent.metadata.get("api_key", ""),
            agent_uuid=agent.metadata.get("agent_uuid"),
            public_agent_id=agent.metadata.get("public_agent_id"),
            label=agent.metadata.get("label"),
            structured_id=agent.metadata.get("structured_id"),
            preferences=agent.metadata.get("preferences", {}),
            active_session_key=agent.metadata.get("active_session_key"),
            session_bound_at=agent.metadata.get("session_bound_at"),
            thread_id=agent.metadata.get("thread_id", None),
            node_index=agent.metadata.get("node_index", 1),
            dialectic_conditions=agent.metadata.get("dialectic_conditions", []),
            lifecycle_events=agent.metadata.get("lifecycle_events", [])[-AgentMetadata.MAX_LIFECYCLE_EVENTS:],
            recent_update_timestamps=agent.metadata.get("recent_update_timestamps", []),
            recent_decisions=agent.metadata.get("recent_decisions", []),
            total_updates=agent.metadata.get("total_updates", 0),
            # Runtime state fields — see agent_storage.persist_runtime_state().
            # Without these, the in-memory mutations in lifecycle/operations.py
            # are clobbered on every force-reload (Watcher P011).
            paused_at=agent.metadata.get("paused_at"),
            last_response_at=agent.metadata.get("last_response_at"),
            response_completed=agent.metadata.get("response_completed", False),
            recovery_attempt_at=agent.metadata.get("recovery_attempt_at"),
            loop_detected_at=agent.metadata.get("loop_detected_at"),
            loop_cooldown_until=agent.metadata.get("loop_cooldown_until"),
        )
        result[agent.agent_id] = meta

    agent_ids = list(result.keys())

    # Single batch identity read shared by profile hydration + trust-tier
    # resolution. Replaces two get_identities_batch calls and removes the
    # per-agent provisional fetchrow that was the dominant cold-start cost.
    identities: dict = {}
    provisional_set: set = set()
    try:
        from src.db import get_db
        db = get_db()
        identities = await db.get_identities_batch(agent_ids) or {}
        if not isinstance(identities, dict):
            logger.debug(
                "get_identities_batch expected dict, got %s",
                type(identities).__name__,
            )
            identities = {}
        provisional_set = await db.get_provisional_lineage_set(agent_ids)
    except Exception as e:
        logger.debug(f"Identity batch read skipped: {e}")

    # Profile hydration (sync in-memory writes once identities are fetched)
    try:
        from src.agent_profile import hydrate_profile
        hydrated = 0
        for aid, identity in identities.items():
            if identity and identity.metadata and "profile" in identity.metadata:
                hydrate_profile(aid, identity.metadata["profile"])
                hydrated += 1
        if hydrated:
            logger.debug(f"Hydrated {hydrated} agent profiles from PostgreSQL")
    except Exception as e:
        logger.debug(f"Agent profile hydration skipped: {e}")

    # Trust-tier resolution. With prefetched_provisional + prefetched_tags
    # the resolve_trust_tier hot path is sync (compute_trust_tier or
    # evaluate_substrate_earned), so the per-agent await yields one event
    # loop tick each — no I/O blocking the cold-start.
    try:
        from src.identity.trust_tier_routing import resolve_trust_tier
        for aid, identity in identities.items():
            if identity and identity.metadata and "trajectory_current" in identity.metadata:
                _meta = result.get(aid)
                tier_info = await resolve_trust_tier(
                    aid,
                    identity.metadata,
                    prefetched_tags=getattr(_meta, "tags", None) if _meta else None,
                    prefetched_label=getattr(_meta, "label", None) if _meta else None,
                    prefetched_provisional=(aid in provisional_set),
                )
                result[aid].trust_tier = tier_info.get("name", "unknown")
                result[aid].trust_tier_num = tier_info.get("tier", 0)
    except Exception as e:
        logger.debug(f"Batch trust tier load skipped: {e}")

    return result


async def _hydrate_metadata_cache_async(snapshot: dict) -> None:
    """Fire-and-forget redis cache hydration after the in-memory dict is
    populated. Sequential `await metadata_cache.set` for ~3000 agents was
    the dominant cold-start cost; doing it after `_metadata_loaded=True`
    means observe handlers don't block on it. Errors are swallowed —
    redis is a cross-process optimization, not the source of truth.
    """
    try:
        from src.cache import get_metadata_cache
        metadata_cache = get_metadata_cache()
    except Exception:
        return
    if metadata_cache is None:
        return
    set_count = 0
    for agent_id, meta in snapshot.items():
        try:
            await metadata_cache.set(agent_id, meta.to_dict(), ttl=300)
            set_count += 1
        except Exception as e:
            logger.debug(f"Failed to cache metadata for {agent_id[:8]}...: {e}")
    if set_count:
        logger.debug(f"Hydrated metadata cache for {set_count} agents (deferred)")


def _parse_metadata_dict(data: dict) -> dict:
    """
    Helper function to parse metadata dictionary and create AgentMetadata objects.
    Handles missing fields and validation.
    """
    from dataclasses import fields as dataclass_fields
    allowed_fields = {f.name for f in dataclass_fields(AgentMetadata)}

    parsed_metadata = {}
    for agent_id, meta in data.items():
        if not isinstance(meta, dict):
            logger.warning(f"Metadata for {agent_id} is not a dict (type: {type(meta).__name__}), skipping")
            continue

        meta = {k: v for k, v in meta.items() if k in allowed_fields}

        defaults = {
            "parent_agent_id": None,
            "spawn_reason": None,
            "thread_id": None,
            "node_index": 1,
            "recent_update_timestamps": None,
            "recent_decisions": None,
            "loop_detected_at": None,
            "loop_cooldown_until": None,
            "recovery_attempt_at": None,
            "last_response_at": None,
            "response_completed": False,
            "health_status": "unknown",
            "dialectic_conditions": None,
        }
        for key, default_value in defaults.items():
            if key not in meta:
                meta[key] = default_value

        try:
            parsed_metadata[agent_id] = AgentMetadata(**meta)
        except (TypeError, KeyError) as e:
            logger.warning(f"Could not create AgentMetadata for {agent_id}: {e}", exc_info=True)
            continue

    return parsed_metadata


def _acquire_metadata_read_lock(timeout: float = 2.0) -> tuple[int, bool]:
    """
    Helper function to acquire shared lock for metadata reads.

    Returns:
        Tuple of (lock_fd, lock_acquired)
    """
    metadata_lock_file = METADATA_FILE.parent / ".metadata.lock"
    lock_fd = os.open(str(metadata_lock_file), os.O_CREAT | os.O_RDWR)
    lock_acquired = False
    start_time = time.time()

    try:
        while time.time() - start_time < timeout:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                lock_acquired = True
                break
            except IOError:
                time.sleep(0.05)

        if not lock_acquired:
            logger.warning(f"Metadata lock timeout ({timeout}s) for read, reading without lock")
    except Exception:
        lock_acquired = False

    return lock_fd, lock_acquired


async def load_metadata_async(force: bool = False) -> None:
    """
    Async version of load_metadata() for use in async contexts.

    Directly calls the async PostgreSQL loader without sync wrappers.
    Set force=True to reload from DB even if already loaded.
    """
    # Fast path: already loaded (unless forced)
    if _model._metadata_loaded and not force:
        return

    try:
        result = await _load_metadata_from_postgres_async()
        # CRITICAL: Use .clear()/.update() to preserve the dict reference
        # across all modules that imported agent_metadata.
        agent_metadata.clear()
        agent_metadata.update(result)
        _metadata_cache_state["last_load_time"] = time.time()
        _metadata_cache_state["dirty"] = False
        _model._metadata_loaded = True
        _metadata_loaded_event.set()
        logger.debug(f"Loaded {len(agent_metadata)} agents from PostgreSQL (async)")
        # Redis cache hydration is fire-and-forget — happens after the
        # in-memory dict is ready and the loaded event is set, so cold-
        # start observe calls don't block on it. Snapshot the dict so
        # later mutations don't race the hydration loop.
        try:
            _track_background_task(
                asyncio.create_task(_hydrate_metadata_cache_async(dict(result)))
            )
        except RuntimeError:
            pass
    except Exception as e:
        logger.error(f"Could not load metadata from PostgreSQL: {e}", exc_info=True)
        _metadata_loaded_event.set()
        raise


def ensure_metadata_loaded() -> None:
    """
    Ensure metadata is loaded (lazy load if needed).

    Schedules async load if needed, then blocks (up to 5s) until complete.
    Thread-safe.
    """
    if _model._metadata_loaded:
        return

    with _metadata_loading_lock:
        if _model._metadata_loaded:
            return

        if _model._metadata_loading:
            pass
        else:
            _model._metadata_loading = True
            _metadata_loaded_event.clear()

            try:
                loop = asyncio.get_running_loop()
                asyncio.run_coroutine_threadsafe(load_metadata_async(), loop)
                logger.info("Scheduled async metadata load from ensure_metadata_loaded()")
            except RuntimeError:
                logger.warning("No running event loop for metadata load — metadata will be empty until async load completes")
                _model._metadata_loading = False
                return

    if not _model._metadata_loaded:
        if _metadata_loaded_event.wait(timeout=5.0):
            logger.debug("ensure_metadata_loaded: async load completed")
        else:
            logger.warning("ensure_metadata_loaded: timed out waiting for async metadata load (5s)")
        _model._metadata_loading = False


def load_metadata() -> None:
    """
    Load agent metadata from storage with caching.

    PostgreSQL is the single source of truth.
    WARNING: PostgreSQL backend requires async loading. This sync version
    uses in-memory cache if available; use load_metadata_async() in async functions.
    """
    if agent_metadata:
        logger.debug(f"Using in-memory metadata cache ({len(agent_metadata)} agents)")
        return
    raise RuntimeError("PostgreSQL backend requires async load_metadata_async(). Sync load_metadata() is not supported.")


def get_or_create_metadata(
    agent_id: str,
    *,
    emit_lifecycle_created: bool = False,
    **kwargs,
) -> AgentMetadata:
    """
    Get metadata for agent, creating if needed.

    In-memory cache helper only — PostgreSQL persistence is the caller's
    responsibility (via agent_storage). Do not add DB writes here; callers
    like onboard/register_agent write to PG first and then populate this
    cache.

    Args:
        agent_id: Agent identifier (human-readable label, can be renamed)
        **kwargs: Optional fields to set on creation (e.g., purpose, notes, tags)
    """
    ensure_metadata_loaded()

    if agent_id not in agent_metadata:
        from src.agent_identity_auth import generate_api_key
        now = datetime.now(timezone.utc).isoformat()
        api_key = generate_api_key()
        import uuid
        import re
        UUID4_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$', re.I)
        if UUID4_PATTERN.match(agent_id):
            agent_uuid = agent_id
        else:
            agent_uuid = str(uuid.uuid4())
        metadata = AgentMetadata(
            agent_id=agent_id,
            status="active",
            created_at=now,
            last_update=now,
            api_key=api_key,
            agent_uuid=agent_uuid
        )

        if agent_id == "default_agent":
            metadata.tags.append("pioneer")
            metadata.notes = "First agent - pioneer of the governance system"

        for key, value in kwargs.items():
            if hasattr(metadata, key) and value is not None:
                setattr(metadata, key, value)

        if emit_lifecycle_created:
            metadata.add_lifecycle_event("created")

        agent_metadata[agent_id] = metadata

        if emit_lifecycle_created:
            logger.info(f"Created new agent metadata '{agent_id}'")
        else:
            logger.debug(f"Hydrated in-memory metadata for '{agent_id}'")
    return agent_metadata[agent_id]


# Alias for cleaner naming (backward compatible)
register_agent = get_or_create_metadata


def register_minted_agent_in_dict(
    agent_uuid: str,
    *,
    status: str = "active",
    label: str | None = None,
    public_agent_id: str | None = None,
    structured_id: str | None = None,
    parent_agent_id: str | None = None,
    spawn_reason: str | None = None,
    thread_id: str | None = None,
    node_index: int = 1,
    api_key: str = "",
) -> bool:
    """Hydrate `agent_metadata` immediately after a fresh `core.identities` mint.

    S21-b §1: closes the H14 axiom-#3 gap where freshly-minted identities
    were invisible to `require_registered_agent` until the next bulk reload.
    Any path that performs `db.upsert_identity` for a new UUID should call
    this so the auth check sees the row in the same request that created it.

    Returns True if a new entry was added, False if the UUID was already
    present (no overwrite — `_load_metadata_from_postgres_async` and
    label-update paths own existing entries).

    Keying note: pass whatever string was used as the `agent_id` in the
    preceding `db.upsert_agent` / `db.upsert_identity` call — that string
    becomes the `core.agents.id` PK and is what `_load_metadata_from_postgres_async`
    will key the dict by. Most call sites pass a UUID; the phases.py
    self-healing path may pass a label. Consistency is per-callsite, not
    enforced here.
    """
    if agent_uuid in agent_metadata:
        # Existing entry — typically from an auto-mint path that ran with
        # thread_id=None default. Backfill thread_id / node_index when this
        # call has them and the existing entry doesn't, otherwise the
        # in-memory cache stays desynced from PG and `process_agent_update`
        # will mint a fresh thread_id (see #424). Strict fill-only — never
        # overwrites a non-None value.
        existing = agent_metadata[agent_uuid]
        backfilled = False
        if thread_id is not None and getattr(existing, "thread_id", None) is None:
            existing.thread_id = thread_id
            backfilled = True
        if node_index is not None and not getattr(existing, "node_index", None):
            existing.node_index = node_index
            backfilled = True
        if backfilled:
            logger.debug(
                f"Backfilled thread_id/node_index on existing dict entry for "
                f"{agent_uuid[:8]}... (was registered by an earlier path "
                f"without thread context)"
            )
        return False
    now = datetime.now(timezone.utc).isoformat()
    agent_metadata[agent_uuid] = AgentMetadata(
        agent_id=public_agent_id or label or agent_uuid,
        status=status,
        created_at=now,
        last_update=now,
        label=label,
        public_agent_id=public_agent_id,
        structured_id=structured_id,
        agent_uuid=agent_uuid,
        parent_agent_id=parent_agent_id,
        spawn_reason=spawn_reason,
        thread_id=thread_id,
        node_index=node_index,
        api_key=api_key,
    )
    return True


def mirror_status_to_dict(agent_uuid: str, status: str) -> bool:
    """Mirror a `core.identities.status` write into the in-memory dict.

    S21-b §3: `db.update_identity_status` writes only PG. Without this
    mirror, the in-memory copy stays at the prior status and
    `require_registered_agent` returns stale-positive (live-verifier
    observed 67 active/archived inversions).

    Returns True if the in-memory entry was updated, False if the UUID
    is not currently in the dict (nothing to mirror — the next bulk
    reload will pick up the PG state).
    """
    meta = agent_metadata.get(agent_uuid)
    if meta is None:
        return False
    meta.status = status
    meta.last_update = datetime.now(timezone.utc).isoformat()
    return True
