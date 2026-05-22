"""
PostgreSQL-Only Agent Storage

Single source of truth for agent data. PostgreSQL-only.

Usage:
    from src.agent_storage import (
        get_agent, create_agent, update_agent, list_agents,
        archive_agent, delete_agent, record_agent_state
    )

    # Create new agent
    agent = await create_agent(
        agent_id="my-agent-id",
        api_key="generated-key",
        status="active",
        tags=["cli", "claude-code"]
    )

    # Get agent
    agent = await get_agent("my-agent-id")

    # Update agent
    await update_agent("my-agent-id", notes="Updated notes", tags=["new-tag"])

    # Record EISV state
    await record_agent_state(
        agent_id="my-agent-id",
        E=0.7, I=0.8, S=0.15, V=-0.01,
        regime="EXPLORATION",
        coherence=0.5,
        health_status="healthy"
    )
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from src.db import get_db
from src.db.base import IdentityRecord, AgentStateRecord
from src.logging_utils import get_logger

logger = get_logger(__name__)


# _sync_cache_entry removed - PostgreSQL is the persistence layer.
# mcp_server.agent_metadata is the active runtime cache (not deprecated).

# Database initialization cache (ensures init() is called before operations)
_db_ready_cache: Dict[int, bool] = {}


async def _ensure_db_ready() -> None:
    """Ensure database is initialized before operations."""
    try:
        db = get_db()
        key = id(db)
        if _db_ready_cache.get(key):
            return
        if hasattr(db, "init"):
            await db.init()
        _db_ready_cache[key] = True
    except Exception as e:
        logger.warning(f"DB init in agent_storage: {e}")
        raise


@dataclass
class AgentRecord:
    """
    Unified agent record combining data from core.agents and core.identities.

    This replaces the old AgentMetadata dataclass from mcp_server_std.py.
    """
    agent_id: str
    api_key: str  # Stored hashed in DB, but we may need plaintext for new agents
    api_key_hash: str
    status: str = "active"  # active, paused, archived, deleted, waiting_input
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None

    # Metadata
    tags: List[str] = field(default_factory=list)
    notes: Optional[str] = None
    purpose: Optional[str] = None

    # Lineage
    parent_agent_id: Optional[str] = None
    spawn_reason: Optional[str] = None

    # Latest state (from core.agent_state, if available)
    health_status: str = "unknown"
    identity_id: Optional[int] = None

    # Extra metadata (from core.identities.metadata JSONB)
    metadata: Dict[str, Any] = field(default_factory=dict)


async def get_agent(agent_id: str) -> Optional[AgentRecord]:
    """
    Get agent by ID.

    Returns AgentRecord combining data from core.agents and core.identities.
    Returns None if agent doesn't exist.
    """
    await _ensure_db_ready()
    db = get_db()

    identity = await db.get_identity(agent_id)
    if not identity:
        return None

    # Get latest state for health_status (gracefully handle schema differences)
    health_status = "unknown"
    if identity.identity_id:
        try:
            state = await db.get_latest_agent_state(identity.identity_id)
            if state and state.state_json:
                health_status = state.state_json.get("health_status", "unknown")
        except Exception as e:
            # Schema mismatch between old/new agent_state tables - use metadata instead
            health_status = identity.metadata.get("health_status", "unknown")
            logger.debug(f"Could not get agent state (schema mismatch?): {e}")

    return AgentRecord(
        agent_id=identity.agent_id,
        api_key="",  # Never return plaintext
        api_key_hash=identity.api_key_hash,
        status=identity.status,
        created_at=identity.created_at,
        updated_at=identity.updated_at,
        last_activity_at=identity.last_activity_at,
        tags=identity.metadata.get("tags", []),
        notes=identity.metadata.get("notes"),
        purpose=identity.metadata.get("purpose"),
        parent_agent_id=identity.parent_agent_id,
        spawn_reason=identity.spawn_reason,
        health_status=health_status,
        identity_id=identity.identity_id,
        metadata=identity.metadata,
    )


async def agent_exists(agent_id: str) -> bool:
    """Check if agent exists."""
    await _ensure_db_ready()
    db = get_db()
    identity = await db.get_identity(agent_id)
    return identity is not None


async def create_agent(
    agent_id: str,
    api_key: str,
    *,
    status: str = "active",
    tags: Optional[List[str]] = None,
    notes: Optional[str] = None,
    purpose: Optional[str] = None,
    parent_agent_id: Optional[str] = None,
    spawn_reason: Optional[str] = None,
    created_at: Optional[datetime] = None,
) -> AgentRecord:
    """
    Create a new agent.

    Creates entries in both core.agents and core.identities tables.
    Returns the created AgentRecord.

    Raises ValueError if agent already exists.
    """
    await _ensure_db_ready()
    db = get_db()

    # Check if agent exists
    existing = await db.get_identity(agent_id)
    if existing:
        raise ValueError(f"Agent '{agent_id}' already exists")

    # Hash API key for storage
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest() if api_key else ""

    created_at = created_at or datetime.now(timezone.utc)

    # Create in core.agents first (FK constraint)
    if hasattr(db, "upsert_agent"):
        await db.upsert_agent(
            agent_id=agent_id,
            api_key=api_key,
            status=status,
            purpose=purpose,
            notes=notes,
            tags=tags,
            created_at=created_at,
            parent_agent_id=parent_agent_id,
            spawn_reason=spawn_reason,
        )

    # Create in core.identities
    await db.upsert_identity(
        agent_id=agent_id,
        api_key_hash=api_key_hash,
        metadata={
            "tags": tags or [],
            "notes": notes,
            "purpose": purpose,
            "source": "agent_storage.create_agent",
            "total_updates": 0,  # Initialize counter for persistence
        },
    )

    logger.info(f"Created agent: {agent_id}")

    return AgentRecord(
        agent_id=agent_id,
        api_key=api_key,  # Return plaintext for new agent (caller may need it)
        api_key_hash=api_key_hash,
        status=status,
        created_at=created_at,
        updated_at=created_at,
        tags=tags or [],
        notes=notes,
        purpose=purpose,
        parent_agent_id=parent_agent_id,
        spawn_reason=spawn_reason,
        health_status="unknown",
    )


async def get_or_create_agent(
    agent_id: str,
    api_key: str,
    **kwargs,
) -> tuple[AgentRecord, bool]:
    """
    Get existing agent or create new one.

    Returns (AgentRecord, is_new) tuple.
    """
    existing = await get_agent(agent_id)
    if existing:
        # Update api_key field for the returned record (if caller generated a new key)
        existing.api_key = api_key
        return existing, False

    agent = await create_agent(agent_id, api_key, **kwargs)
    return agent, True


async def update_agent(
    agent_id: str,
    *,
    status: Optional[str] = None,
    tags: Optional[List[str]] = None,
    notes: Optional[str] = None,
    purpose: Optional[str] = None,
    parent_agent_id: Optional[str] = None,
    spawn_reason: Optional[str] = None,
) -> bool:
    """
    Update agent metadata.

    Only updates fields that are explicitly provided (non-None).
    Returns True if update succeeded, False if agent doesn't exist.
    """
    await _ensure_db_ready()
    db = get_db()

    # Update core.agents
    if hasattr(db, "update_agent_fields"):
        await db.update_agent_fields(
            agent_id=agent_id,
            status=status,
            purpose=purpose,
            notes=notes,
            tags=tags,
            parent_agent_id=parent_agent_id,
            spawn_reason=spawn_reason,
        )

    # Update core.identities metadata
    metadata_updates = {}
    if tags is not None:
        metadata_updates["tags"] = tags
    if notes is not None:
        metadata_updates["notes"] = notes
    if purpose is not None:
        metadata_updates["purpose"] = purpose
    metadata_updates["updated_at"] = datetime.now(timezone.utc).isoformat()

    if metadata_updates:
        await db.update_identity_metadata(agent_id, metadata_updates, merge=True)

    # Update status if provided
    if status is not None:
        disabled_at = datetime.now(timezone.utc) if status in ("archived", "deleted") else None
        await db.update_identity_status(agent_id, status, disabled_at)
        # S21-b §3: mirror PG status into the in-memory dict so the auth
        # check doesn't return stale-positive (live-verifier's 67-row
        # active/archived inversion class).
        try:
            from src.agent_metadata_persistence import mirror_status_to_dict
            mirror_status_to_dict(agent_id, status)
        except Exception as e:
            logger.debug("Status mirror failed: %s", type(e).__name__)

    return True


_RUNTIME_STATE_UNSET = object()


async def persist_runtime_state(
    agent_id: str,
    *,
    paused_at: Any = _RUNTIME_STATE_UNSET,
    last_response_at: Any = _RUNTIME_STATE_UNSET,
    response_completed: Any = _RUNTIME_STATE_UNSET,
    recovery_attempt_at: Any = _RUNTIME_STATE_UNSET,
    loop_detected_at: Any = _RUNTIME_STATE_UNSET,
    loop_cooldown_until: Any = _RUNTIME_STATE_UNSET,
    append_lifecycle_event: Optional[Dict[str, Any]] = None,
) -> bool:
    """Persist transient runtime state to the identity metadata JSON blob.

    These fields have no dedicated columns in core.agents. Without this
    helper, handlers that mutate them only in memory (meta.paused_at = None,
    meta.response_completed = True, meta.recovery_attempt_at, ...) lose
    the mutation on the next load_metadata_async(force=True) (Watcher P011).

    Fields left as the _UNSET sentinel are not written, so callers can
    target exactly the mutated subset. append_lifecycle_event is merged
    into the lifecycle_events list via a read-modify-write (JSONB append
    semantics do not support bounded trailing-window on the server side).
    """
    await _ensure_db_ready()
    db = get_db()

    updates: Dict[str, Any] = {}
    for name, value in (
        ("paused_at", paused_at),
        ("last_response_at", last_response_at),
        ("response_completed", response_completed),
        ("recovery_attempt_at", recovery_attempt_at),
        ("loop_detected_at", loop_detected_at),
        ("loop_cooldown_until", loop_cooldown_until),
    ):
        if value is not _RUNTIME_STATE_UNSET:
            updates[name] = value

    if append_lifecycle_event is not None:
        identity = None
        try:
            identity = await db.get_identity(agent_id)
        except Exception as e:
            logger.debug(
                "persist_runtime_state: get_identity failed: %s",
                type(e).__name__,
            )
        existing_events: List[Dict[str, Any]] = []
        if identity is not None and getattr(identity, "metadata", None):
            existing_events = list(identity.metadata.get("lifecycle_events") or [])
        existing_events.append(append_lifecycle_event)
        # Bounded window — mirrors AgentMetadata.MAX_LIFECYCLE_EVENTS semantics.
        try:
            from src.agent_metadata_model import AgentMetadata
            max_events = AgentMetadata.MAX_LIFECYCLE_EVENTS
        except Exception:
            max_events = 50
        updates["lifecycle_events"] = existing_events[-max_events:]

    if not updates:
        return True

    return await db.update_identity_metadata(agent_id, updates, merge=True)


async def archive_agent(
    agent_id: str,
    *,
    archived_at: str | None = None,
    lifecycle_event: str | None = None,
) -> bool:
    """
    Archive an agent.

    Sets status to 'archived' and records disabled_at timestamp.
    Returns True if successful.
    """
    await _ensure_db_ready()
    db = get_db()

    disabled_at = (
        datetime.fromisoformat(archived_at) if archived_at
        else datetime.now(timezone.utc)
    )
    await db.update_identity_status(
        agent_id=agent_id,
        status="archived",
        disabled_at=disabled_at,
    )

    if hasattr(db, "update_agent_fields"):
        await db.update_agent_fields(agent_id=agent_id, status="archived")

    # S21-b §3
    try:
        from src.agent_metadata_persistence import mirror_status_to_dict
        mirror_status_to_dict(agent_id, "archived")
    except Exception as e:
        logger.debug("Status mirror failed during archive: %s", type(e).__name__)

    logger.info("Archived agent")
    return True


async def delete_agent(agent_id: str) -> bool:
    """
    Mark agent as deleted.

    Sets status to 'deleted' and records disabled_at timestamp.
    Does NOT physically delete the data (soft delete).
    Returns True if successful.
    """
    await _ensure_db_ready()
    db = get_db()

    await db.update_identity_status(
        agent_id=agent_id,
        status="deleted",
        disabled_at=datetime.now(timezone.utc),
    )
    # S21-b §3
    try:
        from src.agent_metadata_persistence import mirror_status_to_dict
        mirror_status_to_dict(agent_id, "deleted")
    except Exception as e:
        logger.debug("Status mirror failed during delete: %s", type(e).__name__)

    if hasattr(db, "update_agent_fields"):
        await db.update_agent_fields(agent_id=agent_id, status="deleted")

    logger.info("Deleted agent")
    return True


async def list_agents(
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    include_archived: bool = False,
    include_deleted: bool = False,
) -> List[AgentRecord]:
    """
    List agents with optional filtering.

    By default excludes archived and deleted agents unless explicitly included.
    """
    await _ensure_db_ready()
    db = get_db()

    identities = await db.list_identities(status=status, limit=limit, offset=offset)

    # Fetch labels from core.agents in one batch query
    agent_labels = {}
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch("SELECT id, label FROM core.agents WHERE label IS NOT NULL AND label != ''")
            agent_labels = {row['id']: row['label'] for row in rows}
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(f"Could not fetch labels from core.agents: {e}")

    agents = []
    for identity in identities:
        # Filter out archived/deleted unless requested
        if identity.status == "archived" and not include_archived:
            continue
        if identity.status == "deleted" and not include_deleted:
            continue

        # Get latest state for health_status (gracefully handle schema differences)
        health_status = "unknown"
        if identity.identity_id:
            try:
                state = await db.get_latest_agent_state(identity.identity_id)
                if state and state.state_json:
                    health_status = state.state_json.get("health_status", "unknown")
            except Exception:
                # Schema mismatch - use metadata instead
                health_status = identity.metadata.get("health_status", "unknown")

        # Merge label from core.agents if available
        merged_metadata = dict(identity.metadata)
        if identity.agent_id in agent_labels:
            merged_metadata["label"] = agent_labels[identity.agent_id]

        agents.append(AgentRecord(
            agent_id=identity.agent_id,
            api_key="",
            api_key_hash=identity.api_key_hash,
            status=identity.status,
            created_at=identity.created_at,
            updated_at=identity.updated_at,
            last_activity_at=identity.last_activity_at,
            tags=identity.metadata.get("tags", []),
            notes=identity.metadata.get("notes"),
            purpose=identity.metadata.get("purpose"),
            parent_agent_id=identity.parent_agent_id,
            spawn_reason=identity.spawn_reason,
            health_status=health_status,
            identity_id=identity.identity_id,
            metadata=merged_metadata,
        ))

    return agents


async def record_agent_state(
    agent_id: str,
    *,
    E: float,
    I: float,
    S: float,
    V: float,
    regime: str,
    coherence: float,
    health_status: str = "unknown",
    risk_score: Optional[float] = None,
    phi: Optional[float] = None,
    verdict: Optional[str] = None,
    action: Optional[str] = None,
    provenance_context: Optional[Mapping[str, Any]] = None,
    epistemic_class: Optional[str] = "agent_report",
) -> int:
    """
    Record agent EISV state to PostgreSQL.

    `action` is the governance decision sub_action / action vocabulary
    ('proceed' | 'pause' | 'approve' | 'reflect' | 'revise' | 'reject') —
    distinct from `verdict` ('safe' | 'caution' | 'high-risk') which is the
    EISV verdict tier. Both are persisted into state_json; hydrate_from_db
    uses `action` to reconstruct decision_history so observe summary's
    decision_distribution survives a JSON-snapshot loss.

    Returns the state_id of the created record.
    """
    await _ensure_db_ready()
    db = get_db()

    # Get identity_id
    identity = await db.get_identity(agent_id)
    if not identity:
        raise ValueError(f"Agent '{agent_id}' not found")

    # Map regime to allowed DB values
    allowed_regimes = {
        'nominal', 'warning', 'critical', 'recovery',
        'EXPLORATION', 'CONVERGENCE', 'DIVERGENCE', 'STABLE'
    }
    db_regime = regime if regime in allowed_regimes else 'nominal'

    # Build state_json
    state_json = {
        "E": E,
        "health_status": health_status,
    }
    if risk_score is not None:
        state_json["risk_score"] = risk_score
    if phi is not None:
        state_json["phi"] = phi
    if verdict is not None:
        state_json["verdict"] = verdict
    if action is not None:
        state_json["action"] = action
    if provenance_context:
        state_json["provenance_context"] = dict(provenance_context)
    if epistemic_class is not None:
        state_json["epistemic_class"] = epistemic_class

    state_id = await db.record_agent_state(
        identity_id=identity.identity_id,
        entropy=S,
        integrity=I,
        stability_index=0.0,  # Dead field — no longer computed
        void=V,
        regime=db_regime,
        coherence=coherence,
        state_json=state_json,
        risk_score=risk_score,
        epistemic_class=epistemic_class,
    )

    return state_id


async def get_agent_state_history(
    agent_id: str,
    limit: int = 100,
) -> List[AgentStateRecord]:
    """Get agent state history."""
    await _ensure_db_ready()
    db = get_db()

    identity = await db.get_identity(agent_id)
    if not identity:
        return []

    return await db.get_agent_state_history(identity.identity_id, limit=limit)


async def get_latest_agent_state(agent_id: str) -> Optional[AgentStateRecord]:
    """Get latest agent state."""
    await _ensure_db_ready()
    db = get_db()

    identity = await db.get_identity(agent_id)
    if not identity:
        return None

    return await db.get_latest_agent_state(identity.identity_id)
