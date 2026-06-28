"""Session operations mixin for PostgresBackend."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..base import SessionRecord
from src.logging_utils import get_logger

logger = get_logger(__name__)


class SessionMixin:
    """Session CRUD operations."""

    async def create_session(
        self,
        session_id: str,
        identity_id: int,
        expires_at,
        client_type: Optional[str] = None,
        client_info: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Create a new session row.

        Returns True only when a new session is inserted. If the session_id
        already exists, this method returns False and does not mutate existing
        session state.
        """
        async with self.acquire() as conn:
            try:
                result = await conn.execute(
                    """
                    INSERT INTO core.sessions (session_id, identity_id, expires_at, client_type, client_info)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (session_id) DO NOTHING
                    """,
                    session_id, identity_id, expires_at, client_type, json.dumps(client_info or {}),
                )
                return "INSERT 0 1" in result
            except Exception:
                return False

    async def get_session(self, session_id: str) -> Optional[SessionRecord]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT s.session_id, s.identity_id, i.agent_id, s.created_at, s.last_active,
                       s.expires_at, s.is_active, s.client_type, s.client_info, s.metadata
                FROM core.sessions s
                JOIN core.identities i ON i.identity_id = s.identity_id
                WHERE s.session_id = $1
                """,
                session_id,
            )
            if not row:
                return None
            return self._row_to_session(row)

    async def update_session_activity(self, session_id: str) -> bool:
        from config.governance_config import GovernanceConfig
        ttl_hours = int(GovernanceConfig.SESSION_TTL_HOURS)
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE core.sessions
                SET last_active = now(),
                    expires_at = now() + ($2 * interval '1 hour')
                WHERE session_id = $1 AND is_active = TRUE
                """,
                session_id,
                ttl_hours,
            )
            return "UPDATE 1" in result

    async def end_session(self, session_id: str) -> bool:
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE core.sessions
                SET is_active = FALSE
                WHERE session_id = $1
                """,
                session_id,
            )
            return "UPDATE 1" in result

    async def get_active_sessions_for_identity(
        self,
        identity_id: int,
    ) -> List[SessionRecord]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.session_id, s.identity_id, i.agent_id, s.created_at, s.last_active,
                       s.expires_at, s.is_active, s.client_type, s.client_info, s.metadata
                FROM core.sessions s
                JOIN core.identities i ON i.identity_id = s.identity_id
                WHERE s.identity_id = $1 AND s.is_active = TRUE AND s.expires_at > now()
                ORDER BY s.last_active DESC
                """,
                identity_id,
            )
            return [self._row_to_session(r) for r in rows]

    async def get_last_inactive_session(
        self,
        identity_id: int,
    ) -> Optional[SessionRecord]:
        """Get most recent inactive session for an identity."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT s.session_id, s.identity_id, i.agent_id, s.created_at, s.last_active,
                       s.expires_at, s.is_active, s.client_type, s.client_info, s.metadata
                FROM core.sessions s
                JOIN core.identities i ON i.identity_id = s.identity_id
                WHERE s.identity_id = $1 AND s.is_active = FALSE
                ORDER BY s.last_active DESC
                LIMIT 1
                """,
                identity_id,
            )
            if not row:
                return None
            return self._row_to_session(row)

    async def cleanup_expired_sessions(self) -> int:
        async with self.acquire() as conn:
            result = await conn.fetchval("SELECT core.cleanup_expired_sessions()")
            return result or 0

    def _row_to_session(self, row) -> SessionRecord:
        return SessionRecord(
            session_id=row["session_id"],
            identity_id=row["identity_id"],
            agent_id=row["agent_id"],
            created_at=row["created_at"],
            last_active=row["last_active"],
            expires_at=row["expires_at"],
            is_active=row["is_active"],
            client_type=row["client_type"],
            client_info=json.loads(row["client_info"]) if isinstance(row["client_info"], str) else row["client_info"],
            metadata=json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"],
        )

    # ------------------------------------------------------------------
    # Session-binding mirror (Redis-retirement Phase 1)
    #
    # FK-less durable mirror of the Redis session: payload, keyed by
    # session_key. INERT — nothing in production calls these yet; the
    # dual-write/read wiring is a separate flag-gated PR. See
    # docs/proposals/redis-retirement-phase-1-plan.md.
    # ------------------------------------------------------------------

    async def upsert_session_binding(
        self,
        session_key: str,
        agent_uuid: str,
        *,
        public_agent_id: Optional[str] = None,
        display_agent_id: Optional[str] = None,
        api_key_hash: Optional[str] = None,
        spawn_reason: Optional[str] = None,
        bind_ip_ua: Optional[str] = None,
        trajectory_required: bool = False,
        expires_at=None,
        mint_guard: bool = False,
    ) -> str:
        """Upsert a session_key -> agent_uuid binding into core.session_bindings.

        Returns one of:
          - "inserted": a new row was created
          - "updated":  an existing row for the same agent_uuid was refreshed
          - "blocked":  mint_guard=True and an existing row binds a *different*
                        agent_uuid — the S21-a collision case; the write is
                        refused (atomic, via the ON CONFLICT ... WHERE guard).

        The (xmax = 0) RETURNING idiom distinguishes insert (xmax=0) from update
        (xmax<>0); when mint_guard blocks, the conditional UPDATE matches no row
        and RETURNING yields nothing -> "blocked".
        """
        # mint_guard blocks an overwrite ONLY when a *live* row binds a
        # different agent_uuid. An expired row (expires_at in the past; NULL =
        # permanent, never expired) is overwritable — it matches Redis TTL
        # semantics where the key has vanished, so a fresh claim succeeds rather
        # than being blocked by a stale binding (Codex review #1: TTL/NX parity).
        guard_clause = (
            "WHERE core.session_bindings.agent_uuid = EXCLUDED.agent_uuid "
            "OR (core.session_bindings.expires_at IS NOT NULL "
            "AND core.session_bindings.expires_at <= now())"
            if mint_guard else ""
        )
        sql = f"""
            INSERT INTO core.session_bindings (
                session_key, agent_uuid, public_agent_id, display_agent_id,
                api_key_hash, spawn_reason, bind_ip_ua, trajectory_required,
                bound_at, expires_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now(), $9)
            ON CONFLICT (session_key) DO UPDATE SET
                agent_uuid          = EXCLUDED.agent_uuid,
                public_agent_id     = EXCLUDED.public_agent_id,
                display_agent_id    = EXCLUDED.display_agent_id,
                api_key_hash        = EXCLUDED.api_key_hash,
                spawn_reason        = COALESCE(EXCLUDED.spawn_reason, core.session_bindings.spawn_reason),
                bind_ip_ua          = COALESCE(EXCLUDED.bind_ip_ua, core.session_bindings.bind_ip_ua),
                trajectory_required = EXCLUDED.trajectory_required,
                bind_count          = core.session_bindings.bind_count + 1,
                expires_at          = EXCLUDED.expires_at
            {guard_clause}
            RETURNING (xmax = 0) AS inserted
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                sql,
                session_key, agent_uuid, public_agent_id, display_agent_id,
                api_key_hash, spawn_reason, bind_ip_ua, trajectory_required,
                expires_at,
            )
        if row is None:
            return "blocked"
        return "inserted" if row["inserted"] else "updated"

    async def get_session_binding(self, session_key: str) -> Optional[Dict[str, Any]]:
        """Read a live (unexpired) session binding as a plain dict, or None.

        Returns a dict (not a dataclass) so callers like the PATH 2 fingerprint
        hijack check can do binding.get("bind_ip_ua") uniformly. Expired rows
        (expires_at <= now) are treated as absent — Redis enforced this via TTL;
        PG must filter explicitly. expires_at IS NULL means permanent.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT session_key, agent_uuid, public_agent_id, display_agent_id,
                       api_key_hash, spawn_reason, bind_ip_ua, trajectory_required,
                       bind_count, bound_at, expires_at
                FROM core.session_bindings
                WHERE session_key = $1
                  AND (expires_at IS NULL OR expires_at > now())
                """,
                session_key,
            )
            return dict(row) if row else None

    async def delete_session_binding(self, session_key: str) -> bool:
        """Remove a session binding (e.g. on force_new). True if a row was removed."""
        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM core.session_bindings WHERE session_key = $1",
                session_key,
            )
            return "DELETE 1" in result

    # ------------------------------------------------------------------
    # Onboard pins (Redis-retirement Phase 1A) — durable recent_onboard:* mirror
    # ------------------------------------------------------------------

    async def set_onboard_pin_pg(
        self,
        fingerprint: str,
        agent_uuid: str,
        client_session_id: str,
        *,
        ttl_seconds: int = 1800,
        if_absent: bool = False,
    ) -> bool:
        """Write an onboard pin (fingerprint -> client_session_id) with a TTL.

        if_absent=True implements the NX-claim semantics (a subagent must not
        displace the driver's *live* pin), returning True only when this call
        actually claimed the slot. An EXPIRED pin is claimable — it matches Redis
        SET NX EX where the expired key is gone, so a new NX claim succeeds
        (Codex review #1: TTL/NX parity). if_absent=False upserts unconditionally
        and always returns True.
        """
        async with self.acquire() as conn:
            if if_absent:
                # Claim when the slot is empty OR the existing pin is expired;
                # a live pin (expires_at in the future) is left untouched (the
                # conditional UPDATE matches no row -> RETURNING is empty).
                row = await conn.fetchrow(
                    """
                    INSERT INTO core.onboard_pins (fingerprint, agent_uuid, client_session_id, expires_at)
                    VALUES ($1, $2, $3, now() + ($4 * interval '1 second'))
                    ON CONFLICT (fingerprint) DO UPDATE SET
                        agent_uuid        = EXCLUDED.agent_uuid,
                        client_session_id = EXCLUDED.client_session_id,
                        expires_at        = EXCLUDED.expires_at
                    WHERE core.onboard_pins.expires_at <= now()
                    RETURNING 1
                    """,
                    fingerprint, agent_uuid, client_session_id, ttl_seconds,
                )
                return row is not None
            await conn.execute(
                """
                INSERT INTO core.onboard_pins (fingerprint, agent_uuid, client_session_id, expires_at)
                VALUES ($1, $2, $3, now() + ($4 * interval '1 second'))
                ON CONFLICT (fingerprint) DO UPDATE SET
                    agent_uuid        = EXCLUDED.agent_uuid,
                    client_session_id = EXCLUDED.client_session_id,
                    expires_at        = EXCLUDED.expires_at
                """,
                fingerprint, agent_uuid, client_session_id, ttl_seconds,
            )
            return True

    async def lookup_onboard_pin_pg(
        self,
        fingerprint: str,
        *,
        refresh_ttl: bool = False,
        ttl_seconds: int = 1800,
    ) -> Optional[str]:
        """Return the pinned client_session_id for a fingerprint, or None.

        Expired pins are treated as absent. refresh_ttl extends the pin's TTL on
        a hit (mirrors the Redis expire-on-read behavior).
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT client_session_id
                FROM core.onboard_pins
                WHERE fingerprint = $1 AND expires_at > now()
                """,
                fingerprint,
            )
            if not row:
                return None
            if refresh_ttl:
                await conn.execute(
                    """
                    UPDATE core.onboard_pins
                    SET expires_at = now() + ($2 * interval '1 second')
                    WHERE fingerprint = $1
                    """,
                    fingerprint, ttl_seconds,
                )
            return row["client_session_id"]
