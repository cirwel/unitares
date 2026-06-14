"""Agent operations mixin for PostgresBackend."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)


class AgentMixin:
    """Agent CRUD operations (core.agents table)."""

    async def upsert_agent(
        self,
        agent_id: str,
        api_key: str,
        status: str = "active",
        purpose: Optional[str] = None,
        notes: Optional[str] = None,
        tags: Optional[List[str]] = None,
        parent_agent_id: Optional[str] = None,
        spawn_reason: Optional[str] = None,
        created_at=None,
        label: Optional[str] = None,
        thread_id: Optional[str] = None,
        thread_position: Optional[int] = None,
    ) -> bool:
        """
        Create or update an agent in core.agents table.

        This is required for foreign key references in dialectic_sessions.
        Returns True if successful.
        """
        async with self.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO core.agents (
                        id, api_key, status, purpose, notes, tags,
                        created_at, parent_agent_id, spawn_reason, label,
                        thread_id, thread_position
                    ) VALUES ($1, $2, $3, $4, $5, $6, COALESCE($7, now()), $8, $9, $10, $11, $12)
                    ON CONFLICT (id) DO UPDATE SET
                        -- Only overwrite api_key if the existing value is empty and we have a non-empty one.
                        api_key = CASE
                            WHEN core.agents.api_key = '' AND EXCLUDED.api_key <> '' THEN EXCLUDED.api_key
                            ELSE core.agents.api_key
                        END,
                        status = EXCLUDED.status,
                        purpose = COALESCE(EXCLUDED.purpose, core.agents.purpose),
                        notes = COALESCE(EXCLUDED.notes, core.agents.notes),
                        tags = EXCLUDED.tags,
                        parent_agent_id = COALESCE(EXCLUDED.parent_agent_id, core.agents.parent_agent_id),
                        spawn_reason = COALESCE(EXCLUDED.spawn_reason, core.agents.spawn_reason),
                        label = COALESCE(EXCLUDED.label, core.agents.label),
                        thread_id = COALESCE(EXCLUDED.thread_id, core.agents.thread_id),
                        thread_position = COALESCE(EXCLUDED.thread_position, core.agents.thread_position),
                        updated_at = now()
                    """,
                    agent_id,
                    api_key,
                    status,
                    purpose,
                    notes,
                    tags or [],
                    created_at,
                    parent_agent_id,
                    spawn_reason,
                    label,
                    thread_id,
                    thread_position,
                )
                return True
            except Exception as e:
                logger.error(f"Failed to upsert agent {agent_id} in core.agents: {e}")
                return False

    async def update_agent_fields(
        self,
        agent_id: str,
        *,
        status: Optional[str] = None,
        purpose: Optional[str] = None,
        notes: Optional[str] = None,
        tags: Optional[List[str]] = None,
        parent_agent_id: Optional[str] = None,
        spawn_reason: Optional[str] = None,
        label: Optional[str] = None,
        archived_at: Optional["datetime"] = None,
    ) -> bool:
        """Partial update of core.agents (does NOT modify api_key).

        ``archived_at`` is written when provided so core.agents stays
        self-consistent with status='archived' (the timestamp previously
        lived only in audit.events).
        """
        async with self.acquire() as conn:
            try:
                result = await conn.execute(
                    """
                    UPDATE core.agents
                    SET
                        status = COALESCE($2, status),
                        purpose = COALESCE($3, purpose),
                        notes = COALESCE($4, notes),
                        tags = COALESCE($5, tags),
                        parent_agent_id = COALESCE($6, parent_agent_id),
                        spawn_reason = COALESCE($7, spawn_reason),
                        label = COALESCE($8, label),
                        archived_at = COALESCE($9, archived_at),
                        updated_at = now()
                    WHERE id = $1
                    """,
                    agent_id,
                    status,
                    purpose,
                    notes,
                    tags,
                    parent_agent_id,
                    spawn_reason,
                    label,
                    archived_at,
                )
                return "UPDATE 1" in result
            except Exception as e:
                logger.error(
                    "Failed to update agent fields: %s",
                    type(e).__name__,
                )
                return False

    async def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """
        Get agent record from core.agents.
        Returns dict with agent fields or None if not found.
        """
        async with self.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    """
                    SELECT id, api_key, status, purpose, notes, tags,
                           created_at, updated_at, archived_at, parent_agent_id,
                           spawn_reason, label
                    FROM core.agents
                    WHERE id = $1
                    """,
                    agent_id
                )
                if row:
                    return dict(row)
                return None
            except Exception as e:
                logger.error(f"Failed to get agent {agent_id}: {e}")
                return None

    async def get_agent_label(self, agent_id: str) -> Optional[str]:
        """Get agent's display label from core.agents."""
        async with self.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    "SELECT label FROM core.agents WHERE id = $1",
                    agent_id
                )
                return row["label"] if row else None
            except Exception as e:
                logger.debug(f"Failed to get label for {agent_id}: {e}")
                return None

    async def find_agent_by_label(self, label: str) -> Optional[str]:
        """Find agent UUID by label. Prefers active agents, most recently updated."""
        async with self.acquire() as conn:
            try:
                rows = await conn.fetch(
                    "SELECT id FROM core.agents WHERE label = $1 AND status = 'active' "
                    "ORDER BY updated_at DESC",
                    label
                )
                if len(rows) > 1:
                    logger.warning(
                        f"[IDENTITY] Multiple active agents with label '{label}': "
                        f"{[str(r['id'])[:12] for r in rows]} — returning most recent"
                    )
                return str(rows[0]["id"]) if rows else None
            except Exception as e:
                logger.debug(f"Failed to find agent by label {label}: {e}")
                return None

    async def agent_has_tag(self, agent_id: str, tag: str) -> bool:
        """Return True iff agent exists in core.agents with `tag` in tags[]."""
        async with self.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    "SELECT 1 FROM core.agents WHERE id = $1 AND $2 = ANY(tags)",
                    agent_id, tag,
                )
                return row is not None
            except Exception as e:
                logger.debug(f"Failed to check tag {tag} on {agent_id}: {e}")
                return False
