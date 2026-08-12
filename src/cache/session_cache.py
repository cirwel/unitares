"""
Session cache: session_id -> agent_id bindings.

Provides persistent session binding that survives server restarts.
Falls back to in-memory cache if Redis is unavailable.

Redis keys:
- session:{session_id} -> JSON {agent_id, bound_at, api_key_hash, bind_count}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from .redis_client import get_redis
from src.logging_utils import get_logger

logger = get_logger(__name__)

# Key prefix for session bindings
SESSION_PREFIX = "session:"

# In-memory fallback cache
_fallback_cache: Dict[str, Dict[str, Any]] = {}

# Prune fallback cache every N operations to prevent unbounded growth
_FALLBACK_PRUNE_INTERVAL = 50
_FALLBACK_MAX_AGE_SECONDS = 86400  # 24 hours
_fallback_op_counter = 0


def _prune_fallback_cache() -> None:
    """Remove stale entries from in-memory fallback cache."""
    global _fallback_op_counter
    _fallback_op_counter += 1
    if _fallback_op_counter < _FALLBACK_PRUNE_INTERVAL:
        return
    _fallback_op_counter = 0

    now = datetime.now(timezone.utc)
    stale = []
    for sid, data in _fallback_cache.items():
        bound_at = data.get("bound_at")
        if bound_at:
            try:
                age = (now - datetime.fromisoformat(bound_at)).total_seconds()
                if age > _FALLBACK_MAX_AGE_SECONDS:
                    stale.append(sid)
            except (ValueError, TypeError):
                stale.append(sid)
        else:
            stale.append(sid)
    for sid in stale:
        del _fallback_cache[sid]
    if stale:
        logger.debug(f"Pruned {len(stale)} stale sessions from fallback cache")


class SessionCache:
    """
    Session cache with Redis backend and in-memory fallback.

    Thread-safe: All operations are atomic Redis commands or
    protected by GIL (fallback dict).
    """

    async def bind(
        self,
        session_id: str,
        agent_id: str,
        *,
        api_key_hash: Optional[str] = None,
    ) -> bool:
        """
        Bind session to agent.

        Args:
            session_id: Session identifier
            agent_id: Agent UUID to bind
            api_key_hash: Optional API key hash for validation

        Returns:
            True if binding succeeded
        """
        data = {
            "agent_id": agent_id,
            "bound_at": datetime.now(timezone.utc).isoformat(),
            "api_key_hash": api_key_hash or "",
            "bind_count": 1,
        }

        # Try Redis first
        redis = await get_redis()
        if redis is not None:
            try:
                key = f"{SESSION_PREFIX}{session_id}"
                # Check if already bound (increment bind_count)
                existing = await redis.get(key)
                if existing:
                    try:
                        existing_data = json.loads(existing)
                        data["bind_count"] = existing_data.get("bind_count", 0) + 1
                    except (json.JSONDecodeError, TypeError):
                        pass
                from config.governance_config import GovernanceConfig
                await redis.setex(key, GovernanceConfig.SESSION_TTL_SECONDS, json.dumps(data))
                # Also update in-memory cache to prevent stale data
                _fallback_cache[session_id] = data
                logger.debug("Session bound in Redis")
                return True
            except Exception as e:
                logger.warning(f"Redis bind failed: {e}")

        # Fallback to in-memory (already handles fallback if Redis fails or is None)
        _prune_fallback_cache()
        existing = _fallback_cache.get(session_id)
        if existing:
            data["bind_count"] = existing.get("bind_count", 0) + 1
        _fallback_cache[session_id] = data
        logger.debug("Session bound in memory")
        return True

    async def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Get session binding.

        Args:
            session_id: Session identifier

        Returns:
            Binding data dict if found, None otherwise.
            Keys: agent_id, bound_at, api_key_hash, bind_count
        """
        # Try Redis first
        redis = await get_redis()
        if redis is not None:
            try:
                key = f"{SESSION_PREFIX}{session_id}"
                data = await redis.get(key)
                if data:
                    result = json.loads(data)
                    # Sync in-memory cache with Redis (authoritative source)
                    _fallback_cache[session_id] = result
                    return result
                else:
                    # Key expired or deleted in Redis — evict stale fallback
                    _fallback_cache.pop(session_id, None)
                    return None
            except Exception as e:
                logger.warning(f"Redis get failed: {e}")

        # Fallback to in-memory (only when Redis is unavailable, not when key is missing)
        return _fallback_cache.get(session_id)

    async def get_agent_id(self, session_id: str) -> Optional[str]:
        """
        Get just the agent_id for a session (convenience method).

        Args:
            session_id: Session identifier

        Returns:
            Agent ID if bound, None otherwise
        """
        data = await self.get(session_id)
        return data.get("agent_id") if data else None

    async def unbind(self, session_id: str) -> bool:
        """
        Remove session binding.

        Args:
            session_id: Session identifier

        Returns:
            True if binding was removed
        """
        removed = False

        # Try Redis first
        redis = await get_redis()
        if redis is not None:
            try:
                key = f"{SESSION_PREFIX}{session_id}"
                result = await redis.delete(key)
                removed = result > 0
            except Exception as e:
                logger.warning(f"Redis unbind failed: {e}")

        # Also remove from fallback
        if session_id in _fallback_cache:
            del _fallback_cache[session_id]
            removed = True

        return removed

    async def exists(self, session_id: str) -> bool:
        """Check if session is bound."""
        return await self.get_agent_id(session_id) is not None

    async def get_by_agent_id(self, agent_id: str) -> Optional[str]:
        """
        Reverse lookup: find session_id for an agent_id.

        Note: This is O(n) in Redis (SCAN) and should be used sparingly.
        For production, consider maintaining a reverse index.

        Args:
            agent_id: Agent UUID

        Returns:
            Session ID if found, None otherwise
        """
        # Try Redis first (using SCAN to avoid blocking)
        redis = await get_redis()
        if redis is not None:
            try:
                cursor = 0
                while True:
                    cursor, keys = await redis.scan(
                        cursor, match=f"{SESSION_PREFIX}*", count=100
                    )
                    for key in keys:
                        data = await redis.get(key)
                        if data:
                            try:
                                parsed = json.loads(data)
                                if parsed.get("agent_id") == agent_id:
                                    return key[len(SESSION_PREFIX):]  # Remove prefix
                            except (json.JSONDecodeError, TypeError):
                                pass
                    if cursor == 0:
                        break
            except Exception as e:
                logger.warning(f"Redis reverse lookup failed: {e}")

        # Fallback to in-memory
        for session_id, data in _fallback_cache.items():
            if data.get("agent_id") == agent_id:
                return session_id

        return None

    async def health_check(self) -> Dict[str, Any]:
        """
        Get cache health status.

        Returns:
            Dict with backend, status, and stats
        """
        redis = await get_redis()
        if redis is not None:
            try:
                await redis.ping()
                # Count session keys
                cursor = 0
                count = 0
                while True:
                    cursor, keys = await redis.scan(
                        cursor, match=f"{SESSION_PREFIX}*", count=100
                    )
                    count += len(keys)
                    if cursor == 0:
                        break
                return {
                    "backend": "redis",
                    "status": "healthy",
                    "session_count": count,
                    "fallback_count": len(_fallback_cache),
                }
            except Exception as e:
                return {
                    "backend": "redis",
                    "status": "error",
                    "error": str(e),
                    "fallback_count": len(_fallback_cache),
                }

        return {
            "backend": "memory",
            "status": "healthy",
            "session_count": len(_fallback_cache),
        }


# Singleton instance
_session_cache: Optional[SessionCache] = None


def get_session_cache() -> SessionCache:
    """Get singleton session cache instance."""
    global _session_cache
    if _session_cache is None:
        _session_cache = SessionCache()
    return _session_cache
