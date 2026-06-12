"""
Telemetry caching layer to reduce file I/O.

Caches telemetry query results with TTL-based expiration.
Helps future agents by reducing blocking file operations.
"""

from typing import Dict, Optional, Any
from datetime import datetime, timedelta
import hashlib
import json
import threading


class TelemetryCache:
    """Simple TTL-based cache for telemetry queries"""
    
    MAX_ENTRIES = 2000

    def __init__(self, default_ttl_seconds: int = 60):
        """
        Initialize cache with default TTL.

        Args:
            default_ttl_seconds: Default cache TTL in seconds (default: 60s)
        """
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.default_ttl = default_ttl_seconds
        self._sweep_counter = 0
        self._lock = threading.Lock()
    
    def _make_key(self, query_type: str, agent_id: Optional[str] = None, 
                  window_hours: int = 24, **kwargs) -> str:
        """Generate cache key from query parameters"""
        key_parts = [query_type]
        if agent_id:
            key_parts.append(f"agent:{agent_id}")
        key_parts.append(f"window:{window_hours}")
        if kwargs:
            # Sort kwargs for consistent keys
            sorted_kwargs = sorted(kwargs.items())
            key_parts.append(json.dumps(sorted_kwargs, sort_keys=True))
        
        key_str = "|".join(key_parts)
        return hashlib.md5(key_str.encode()).hexdigest()
    
    def get(self, query_type: str, agent_id: Optional[str] = None,
            window_hours: int = 24, **kwargs) -> Optional[Dict[str, Any]]:
        """
        Get cached result if available and not expired.
        
        Returns:
            Cached result dict or None if not found/expired
        """
        key = self._make_key(query_type, agent_id, window_hours, **kwargs)

        with self._lock:
            if key not in self.cache:
                return None

            entry = self.cache[key]
            expires_at = entry.get('expires_at')

            if expires_at and datetime.now() > expires_at:
                del self.cache[key]
                return None

            return entry.get('data')
    
    def set(self, query_type: str, data: Dict[str, Any],
            agent_id: Optional[str] = None, window_hours: int = 24,
            ttl_seconds: Optional[int] = None, **kwargs) -> None:
        """
        Cache query result with TTL.
        
        Args:
            query_type: Type of query (e.g., 'skip_rate', 'confidence_dist')
            data: Result data to cache
            agent_id: Optional agent ID
            window_hours: Time window for query
            ttl_seconds: Optional TTL override (uses default if None)
            **kwargs: Additional query parameters
        """
        key = self._make_key(query_type, agent_id, window_hours, **kwargs)
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl

        with self._lock:
            self.cache[key] = {
                'data': data,
                'expires_at': datetime.now() + timedelta(seconds=ttl),
                'cached_at': datetime.now().isoformat(),
                'query_type': query_type
            }
            self._sweep_counter += 1
            if self._sweep_counter >= 50 or len(self.cache) > self.MAX_ENTRIES:
                self._sweep_locked()
                self._sweep_counter = 0
    
    def invalidate(self, query_type: Optional[str] = None,
                   agent_id: Optional[str] = None) -> int:
        """
        Invalidate cache entries.
        
        Args:
            query_type: If provided, only invalidate this query type
            agent_id: If provided, only invalidate entries for this agent
            
        Returns:
            Number of entries invalidated
        """
        with self._lock:
            if query_type is None and agent_id is None:
                count = len(self.cache)
                self.cache.clear()
                return count

            keys_to_remove = []
            for key, entry in self.cache.items():
                entry_query_type = entry.get('query_type', '')
                entry_data = entry.get('data', {})

                should_remove = False
                if query_type and entry_query_type == query_type:
                    should_remove = True
                if agent_id and entry_data.get('agent_id') == agent_id:
                    should_remove = True

                if should_remove:
                    keys_to_remove.append(key)

            for key in keys_to_remove:
                del self.cache[key]

            return len(keys_to_remove)
    
    def sweep(self) -> int:
        """Remove all expired entries. Returns number removed."""
        with self._lock:
            return self._sweep_locked()

    def _sweep_locked(self) -> int:
        """Remove expired/excess entries. Caller must hold self._lock."""
        now = datetime.now()
        expired = [k for k, v in self.cache.items()
                   if v.get('expires_at') and now > v['expires_at']]
        for k in expired:
            del self.cache[k]
        if len(self.cache) > self.MAX_ENTRIES:
            by_age = sorted(self.cache.items(), key=lambda x: x[1].get('cached_at', ''))
            to_evict = by_age[:len(self.cache) - self.MAX_ENTRIES]
            for k, _ in to_evict:
                del self.cache[k]
            expired.extend([k for k, _ in to_evict])
        return len(expired)

    def clear(self) -> None:
        """Clear all cache entries"""
        with self._lock:
            self.cache.clear()

    def stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        with self._lock:
            total_entries = len(self.cache)
            expired_count = 0
            now = datetime.now()

            for entry in self.cache.values():
                if entry.get('expires_at') and now > entry.get('expires_at'):
                    expired_count += 1

            return {
                'total_entries': total_entries,
                'expired_entries': expired_count,
                'active_entries': total_entries - expired_count,
                'default_ttl_seconds': self.default_ttl
            }


# Global cache instance
_telemetry_cache: Optional[TelemetryCache] = None


def get_telemetry_cache() -> TelemetryCache:
    """Get or create global telemetry cache instance"""
    global _telemetry_cache
    if _telemetry_cache is None:
        _telemetry_cache = TelemetryCache(default_ttl_seconds=60)
    return _telemetry_cache

