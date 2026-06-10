"""
Tests for src/mcp_handlers/identity_v2.py - Redis integration paths.

Tests resolve_session_identity 3-tier pipeline (Redis → PostgreSQL → Create),
_derive_session_key, _generate_agent_id, _cache_session, and session key
validation using fakeredis for real Redis protocol testing.

PostgreSQL is mocked (AsyncMock) since we're testing the Redis layer specifically.
"""

import pytest
import json
import sys
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from types import SimpleNamespace

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

fakeredis = pytest.importorskip("fakeredis")
import fakeredis.aioredis

from src.cache.session_cache import SessionCache, _fallback_cache, SESSION_PREFIX


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def fake_redis():
    """Create a fakeredis async client."""
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def session_cache(fake_redis):
    """SessionCache backed by fakeredis."""
    _fallback_cache.clear()

    async def _get_fake_redis():
        return fake_redis

    with patch("src.cache.session_cache.get_redis", new=_get_fake_redis):
        yield SessionCache()

    _fallback_cache.clear()


@pytest.fixture
def mock_db():
    """Mock database that returns None for all lookups (no existing agent)."""
    db = AsyncMock()
    db.init = AsyncMock()
    db.get_session = AsyncMock(return_value=None)
    db.get_identity = AsyncMock(return_value=None)
    db.get_agent = AsyncMock(return_value=None)
    db.get_agent_label = AsyncMock(return_value=None)
    db.upsert_agent = AsyncMock()
    db.upsert_identity = AsyncMock()
    db.create_session = AsyncMock()
    db.update_session_activity = AsyncMock()
    db.find_agent_by_label = AsyncMock(return_value=None)
    db.update_agent_fields = AsyncMock(return_value=True)
    return db


@pytest.fixture
def patch_identity_deps(session_cache, mock_db, fake_redis):
    """
    Patch identity_v2 dependencies: Redis via SessionCache, PostgreSQL via mock_db.

    get_session_cache is imported locally inside _get_redis(), so patch at source.
    get_db is imported at module level.
    _cache_session also does 'from src.cache.redis_client import get_redis' locally.
    """
    async def _get_fake_raw():
        return fake_redis

    # Reset the module-level _redis_cache so _get_redis() re-initializes
    with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
         patch("src.cache.get_session_cache", return_value=session_cache), \
         patch("src.cache.session_cache.get_redis", new=_get_fake_raw), \
         patch("src.cache.redis_client.get_redis", new=_get_fake_raw), \
         patch("src.mcp_handlers.identity.resolution.get_db", return_value=mock_db), \
         patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
        yield


# ============================================================================
# _generate_agent_id (pure function)
# ============================================================================

class TestGenerateAgentId:

    @pytest.fixture(autouse=True)
    def import_fn(self):
        from src.mcp_handlers.identity.handlers import _generate_agent_id
        self.generate = _generate_agent_id

    def test_with_model_type(self):
        result = self.generate(model_type="claude-opus-4-5")
        assert result.startswith("Claude_Opus_4_5_")
        assert len(result.split("_")[-1]) == 8  # YYYYMMDD

    def test_with_client_hint(self):
        result = self.generate(client_hint="cursor")
        assert result.startswith("cursor_")

    def test_fallback(self):
        result = self.generate()
        assert result.startswith("anon_")

    def test_third_party_client_prefixed(self):
        result = self.generate(model_type="gemini-pro", client_hint="cursor")
        assert result.startswith("Cursor_Gemini_Pro_")

    def test_empty_client_hint_uses_fallback(self):
        result = self.generate(client_hint="")
        assert result.startswith("anon_")

    def test_unknown_client_hint_uses_fallback(self):
        result = self.generate(client_hint="unknown")
        assert result.startswith("anon_")


# ============================================================================
# derive_session_key (async)
# ============================================================================

class TestDeriveSessionKey:

    @pytest.fixture(autouse=True)
    def import_fn(self):
        from src.mcp_handlers.identity.handlers import derive_session_key
        self.derive = derive_session_key

    @pytest.mark.asyncio
    async def test_explicit_client_session_id(self):
        result = await self.derive(None, {"client_session_id": "my-session-123"})
        assert result == "my-session-123"

    @pytest.mark.asyncio
    async def test_mcp_session_id_header(self):
        with patch("src.mcp_handlers.context.get_mcp_session_id", return_value="mcp-abc123"):
            result = await self.derive(None, {})
            assert result == "mcp:mcp-abc123"

    @pytest.mark.asyncio
    async def test_context_session_key(self):
        with patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value="ctx-key-456"):
            result = await self.derive(None, {})
            assert result == "ctx-key-456"

    @pytest.mark.asyncio
    async def test_stdio_fallback(self):
        with patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value=None):
            result = await self.derive(None, {})
            assert result.startswith("stdio:")

    @pytest.mark.asyncio
    async def test_client_session_id_takes_priority(self):
        with patch("src.mcp_handlers.context.get_mcp_session_id", return_value="mcp-abc"):
            result = await self.derive(None, {"client_session_id": "explicit"})
            assert result == "explicit"


# ============================================================================
# resolve_session_identity - PATH 3: Create new (no existing binding)
# ============================================================================

class TestResolveCreateNew:

    @pytest.mark.asyncio
    async def test_creates_new_agent_lazy(self, patch_identity_deps, mock_db):
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        result = await resolve_session_identity(
            session_key="test-session-1",
            model_type="claude-opus-4",
        )

        assert result["created"] is True
        assert result["source"] == "memory_only"
        assert result["persisted"] is False
        assert "agent_uuid" in result
        assert result["agent_id"].startswith("Claude_Opus_4_")
        # Should NOT have called upsert (lazy creation)
        mock_db.upsert_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_new_agent_persisted(self, patch_identity_deps, mock_db):
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        # Make get_identity return a mock so create_session works
        mock_identity = SimpleNamespace(identity_id="ident-1", metadata={})
        mock_db.get_identity.return_value = mock_identity

        result = await resolve_session_identity(
            session_key="test-session-2",
            persist=True,
            model_type="gemini-pro",
        )

        assert result["created"] is True
        assert result["persisted"] is True
        assert result["source"] == "created"
        mock_db.upsert_agent.assert_called_once()
        mock_db.upsert_identity.assert_called_once()

    @pytest.mark.asyncio
    async def test_new_agent_gets_uuid(self, patch_identity_deps):
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        result = await resolve_session_identity(session_key="test-session-3")

        agent_uuid = result["agent_uuid"]
        # Should be valid UUID format
        assert len(agent_uuid) == 36
        assert agent_uuid.count("-") == 4


# ============================================================================
# resolve_session_identity - PATH 1: Redis cache hit
# ============================================================================

class TestResolveRedisHit:

    @pytest.mark.asyncio
    async def test_returns_from_redis_cache(self, patch_identity_deps, session_cache, mock_db):
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        # First call creates a new agent
        first = await resolve_session_identity(
            session_key="cache-test-session",
            model_type="claude-sonnet",
        )
        assert first["created"] is True
        agent_uuid = first["agent_uuid"]

        # Reset mock call counts
        mock_db.reset_mock()
        # Ensure get_session returns None so PATH 2 doesn't kick in
        mock_db.get_session.return_value = None

        # Second call should hit Redis cache (resume=True to reuse existing)
        second = await resolve_session_identity(session_key="cache-test-session", resume=True)

        assert second["created"] is False
        assert second["source"] == "redis"
        assert second["agent_uuid"] == agent_uuid

    @pytest.mark.asyncio
    async def test_redis_cache_stores_uuid_format(self, patch_identity_deps, session_cache, fake_redis):
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        result = await resolve_session_identity(
            session_key="uuid-format-test",
            model_type="claude-opus",
        )

        # Check what's actually in Redis
        raw = await fake_redis.get(f"{SESSION_PREFIX}uuid-format-test")
        assert raw is not None
        data = json.loads(raw)
        # Should store UUID as agent_id (not model+date)
        assert data["agent_id"] == result["agent_uuid"]


# ============================================================================
# resolve_session_identity - session key validation
# ============================================================================

class TestSessionKeyValidation:

    @pytest.mark.asyncio
    async def test_empty_session_key_raises(self, patch_identity_deps):
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        with pytest.raises(ValueError, match="session_key is required"):
            await resolve_session_identity(session_key="")

    @pytest.mark.asyncio
    async def test_long_session_key_truncated(self, patch_identity_deps):
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        long_key = "a" * 500
        result = await resolve_session_identity(session_key=long_key)
        # Should succeed (truncated internally)
        assert result["created"] is True

    @pytest.mark.asyncio
    async def test_special_chars_sanitized(self, patch_identity_deps):
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        # Key with SQL injection attempt
        result = await resolve_session_identity(session_key="user'; DROP TABLE agents;--")
        assert result["created"] is True


# ============================================================================
# resolve_session_identity - force_new
# ============================================================================

class TestForceNew:

    @pytest.mark.asyncio
    async def test_force_new_skips_cache(self, patch_identity_deps, session_cache):
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        # Create initial binding
        first = await resolve_session_identity(session_key="force-test")
        first_uuid = first["agent_uuid"]

        # force_new should create new identity
        second = await resolve_session_identity(
            session_key="force-test",
            force_new=True,
        )

        assert second["created"] is True
        assert second["agent_uuid"] != first_uuid


# ============================================================================
# _cache_session
# ============================================================================

class TestCacheSession:

    @pytest.mark.asyncio
    async def test_cache_with_display_agent_id(self, fake_redis):
        """Cache stores both UUID and display agent_id."""
        async def _get_fake_redis():
            return fake_redis

        sc = SessionCache()

        # _cache_session calls _get_redis() which does local import from src.cache
        # Then for display_agent_id path, it also does local import from src.cache.redis_client
        with patch("src.cache.session_cache.get_redis", new=_get_fake_redis), \
             patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=sc), \
             patch("src.cache.redis_client.get_redis", new=_get_fake_redis):

            from src.mcp_handlers.identity.handlers import _cache_session

            await _cache_session("sess-1", "uuid-1234", display_agent_id="Claude_Opus_20260205")

            raw = await fake_redis.get("session:sess-1")
            assert raw is not None
            data = json.loads(raw)
            assert data["agent_id"] == "uuid-1234"
            assert data["display_agent_id"] == "Claude_Opus_20260205"

    @pytest.mark.asyncio
    async def test_cache_without_display_id_uses_bind(self, session_cache, fake_redis):
        """Without display_agent_id, uses SessionCache.bind()."""
        with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=session_cache):

            from src.mcp_handlers.identity.handlers import _cache_session

            await _cache_session("sess-2", "uuid-5678")

            raw = await fake_redis.get(f"{SESSION_PREFIX}sess-2")
            assert raw is not None
            data = json.loads(raw)
            assert data["agent_id"] == "uuid-5678"


# ============================================================================
# _extract_base_fingerprint
# ============================================================================

class TestExtractBaseFingerprint:

    @pytest.fixture(autouse=True)
    def import_fn(self):
        from src.mcp_handlers.identity.handlers import _extract_base_fingerprint
        self.extract = _extract_base_fingerprint

    def test_mcp_key_returns_none(self):
        assert self.extract("mcp:abc123") is None

    def test_stdio_key_returns_none(self):
        assert self.extract("stdio:12345") is None

    def test_agent_key_returns_none(self):
        assert self.extract("agent-uuid-1234") is None

    def test_ip_ua_hash_extracts_ua(self):
        result = self.extract("192.168.1.1:abc123hash")
        assert result == "ua:abc123hash"

    def test_ip_ua_hash_suffix_extracts_ua(self):
        result = self.extract("192.168.1.1:abc123hash:extra")
        assert result == "ua:abc123hash"

    def test_single_part_returns_as_is(self):
        result = self.extract("singlepart")
        assert result == "singlepart"

    def test_none_returns_none(self):
        assert self.extract(None) is None

    def test_empty_returns_none(self):
        assert self.extract("") is None


# ============================================================================
# _agent_exists_in_postgres (helper)
# ============================================================================

class TestAgentExistsInPostgres:

    @pytest.mark.asyncio
    async def test_returns_true_when_found(self):
        mock_db = AsyncMock()
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            from src.mcp_handlers.identity.handlers import _agent_exists_in_postgres
            assert await _agent_exists_in_postgres("uuid-1") is True

    @pytest.mark.asyncio
    async def test_returns_false_when_not_found(self):
        mock_db = AsyncMock()
        mock_db.get_identity.return_value = None

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            from src.mcp_handlers.identity.handlers import _agent_exists_in_postgres
            assert await _agent_exists_in_postgres("uuid-2") is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        mock_db = AsyncMock()
        mock_db.get_identity.side_effect = Exception("DB down")

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            from src.mcp_handlers.identity.handlers import _agent_exists_in_postgres
            assert await _agent_exists_in_postgres("uuid-3") is False


# ============================================================================
# CircuitBreaker (from redis_client.py - pure logic, no mocks needed)
# ============================================================================

class TestCircuitBreaker:

    @pytest.fixture(autouse=True)
    def import_cb(self):
        from src.cache.redis_client import CircuitBreaker
        self.CircuitBreaker = CircuitBreaker

    def test_starts_closed(self):
        cb = self.CircuitBreaker()
        assert cb.state == "closed"
        assert cb.is_available() is True

    def test_opens_after_threshold(self):
        cb = self.CircuitBreaker(threshold=3, timeout=30)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"
        cb.record_failure()
        assert cb.state == "open"
        assert cb.is_available() is False

    def test_success_resets_count(self):
        cb = self.CircuitBreaker(threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb._failure_count == 0
        assert cb.state == "closed"

    def test_half_open_after_timeout(self):
        cb = self.CircuitBreaker(threshold=1, timeout=0.01)
        clock = {"now": 100.0}

        with patch("src.cache.redis_client.time.time", side_effect=lambda: clock["now"]):
            cb.record_failure()
            assert cb.state == "open"
            clock["now"] += 0.02
            assert cb.state == "half_open"

    def test_half_open_success_closes(self):
        cb = self.CircuitBreaker(threshold=1, timeout=0.01)
        clock = {"now": 200.0}

        with patch("src.cache.redis_client.time.time", side_effect=lambda: clock["now"]):
            cb.record_failure()
            clock["now"] += 0.02
            assert cb.state == "half_open"
            cb.record_success()
            assert cb.state == "closed"

    def test_half_open_failure_reopens(self):
        cb = self.CircuitBreaker(threshold=1, timeout=0.01)
        clock = {"now": 300.0}

        with patch("src.cache.redis_client.time.time", side_effect=lambda: clock["now"]):
            cb.record_failure()
            clock["now"] += 0.02
            assert cb.state == "half_open"
            clock["now"] += 0.001
            cb.record_failure()
            assert cb.state == "open"

    def test_reset(self):
        cb = self.CircuitBreaker(threshold=1)
        cb.record_failure()
        assert cb.state == "open"
        cb.reset()
        assert cb.state == "closed"
        assert cb._failure_count == 0


# ============================================================================
# RedisMetrics (from redis_client.py - pure dataclass)
# ============================================================================

class TestRedisMetrics:

    @pytest.fixture(autouse=True)
    def import_metrics(self):
        from src.cache.redis_client import RedisMetrics
        self.RedisMetrics = RedisMetrics

    def test_defaults(self):
        m = self.RedisMetrics()
        assert m.operations_total == 0
        assert m.operations_success == 0

    def test_to_dict(self):
        m = self.RedisMetrics()
        m.operations_total = 100
        m.operations_success = 95
        d = m.to_dict()
        assert d["operations"]["total"] == 100
        assert d["operations"]["success_rate"] == 95.0
        assert "uptime_seconds" in d

    def test_to_dict_zero_total(self):
        m = self.RedisMetrics()
        d = m.to_dict()
        # Should not divide by zero
        assert d["operations"]["success_rate"] == 0.0
