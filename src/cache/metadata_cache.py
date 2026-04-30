"""
Redis cache for agent metadata.

Provides fast, distributed caching of agent metadata to reduce PostgreSQL load.
Falls back to direct PostgreSQL queries if Redis is unavailable.

Usage:
    from src.cache import get_metadata_cache
    
    cache = get_metadata_cache()
    metadata = await cache.get(agent_id)
    if not metadata:
        metadata = load_from_postgres(agent_id)
        await cache.set(agent_id, metadata, ttl=300)
"""

from __future__ import annotations

import json
from typing import Optional, Dict, Any

from .redis_client import get_redis
from src.logging_utils import get_logger

logger = get_logger(__name__)

# Key prefix for metadata cache
METADATA_PREFIX = "agent_meta:"

# Default TTL (5 minutes)
DEFAULT_TTL = 300


class MetadataCache:
    """
    Redis cache for agent metadata.
    
    Caches agent metadata to reduce PostgreSQL queries.
    Automatically expires after TTL.
    """

    async def get(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """
        Get cached agent metadata.
        
        Args:
            agent_id: Agent UUID
        
        Returns:
            Metadata dict if cached, None if not found or expired
        """
        redis = await get_redis()
        if redis is None:
            return None
        
        key = f"{METADATA_PREFIX}{agent_id}"
        try:
            cached = await redis.get(key)
            if cached:
                metadata = json.loads(cached)
                logger.debug(f"Metadata cache hit: {agent_id[:8]}...")
                return metadata
            return None
        except Exception as e:
            logger.debug(f"Metadata cache get failed: {e}")
            return None

    async def set(
        self,
        agent_id: str,
        metadata: Dict[str, Any],
        ttl: int = DEFAULT_TTL,
    ) -> bool:
        """
        Cache agent metadata.
        
        Args:
            agent_id: Agent UUID
            metadata: Metadata dict to cache
            ttl: Time to live in seconds (default: 5 minutes)
        
        Returns:
            True if cached successfully
        """
        redis = await get_redis()
        if redis is None:
            return False
        
        key = f"{METADATA_PREFIX}{agent_id}"
        try:
            await redis.setex(key, ttl, json.dumps(metadata))
            logger.debug("Metadata cached (TTL: %ss)", ttl)
            return True
        except Exception as e:
            logger.debug("Metadata cache set failed: %s", type(e).__name__)
            return False

    async def invalidate(self, agent_id: str) -> bool:
        """
        Invalidate cached metadata for agent.
        
        Args:
            agent_id: Agent UUID
        
        Returns:
            True if invalidated successfully
        """
        redis = await get_redis()
        if redis is None:
            return False
        
        key = f"{METADATA_PREFIX}{agent_id}"
        try:
            await redis.delete(key)
            logger.debug("Metadata cache invalidated")
            return True
        except Exception as e:
            logger.debug("Metadata cache invalidate failed: %s", type(e).__name__)
            return False

    async def invalidate_all(self) -> int:
        """
        Invalidate all cached metadata (for testing/admin).
        
        Returns:
            Number of keys deleted
        """
        redis = await get_redis()
        if redis is None:
            return 0
        
        try:
            # Find all metadata keys
            pattern = f"{METADATA_PREFIX}*"
            keys = []
            async for key in redis.scan_iter(match=pattern):
                keys.append(key)
            
            if keys:
                deleted = await redis.delete(*keys)
                logger.debug(f"Invalidated {deleted} metadata cache entries")
                return deleted
            return 0
        except Exception as e:
            logger.warning(f"Metadata cache invalidate_all failed: {e}")
            return 0


# Singleton instance
_metadata_cache: Optional[MetadataCache] = None


def get_metadata_cache() -> MetadataCache:
    """Get singleton metadata cache instance."""
    global _metadata_cache
    if _metadata_cache is None:
        _metadata_cache = MetadataCache()
    return _metadata_cache
