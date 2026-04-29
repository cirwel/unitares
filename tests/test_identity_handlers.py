"""
Comprehensive tests for src/mcp_handlers/identity_v2.py.

Covers the full identity resolution pipeline:
- resolve_session_identity() 3-tier: Redis -> PostgreSQL -> Create new
- derive_session_key() unified async + _derive_session_key() deprecated sync wrapper
- _validate_session_key() / sanitization within resolve_session_identity
- persist_identity via ensure_agent_persisted()
- get_agent_label / _get_agent_label
- _agent_exists_in_postgres
- _find_agent_by_label
- _get_agent_id_from_metadata
- _generate_agent_id (pure function)
- _normalize_model_type (pure function)
- set_agent_label
- _cache_session
- _extract_base_fingerprint
- ua_hash_from_header
- lookup_onboard_pin / set_onboard_pin
- handle_identity_v2 (tool handler)
- ensure_agent_persisted (lazy creation)

All external I/O (Redis, PostgreSQL, MCP server) is mocked.
"""

import pytest
import json
import sys
import os
import uuid
import re
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from tests.helpers import parse_result


# ============================================================================
# Shared Fixtures
# ============================================================================

@pytest.fixture
def mock_db():
    """Mock PostgreSQL database with all methods used by identity_v2."""
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
    db.get_agent_thread_info = AsyncMock(return_value=None)
    db.get_thread_nodes = AsyncMock(return_value=[])
    db.get_active_sessions_for_identity = AsyncMock(return_value=[])
    db.get_last_inactive_session = AsyncMock(return_value=None)
    db.get_latest_agent_state = AsyncMock(return_value=None)
    db.get_agent_state_history = AsyncMock(return_value=[])
    db.kg_query = AsyncMock(return_value=[])
    return db


@pytest.fixture
def mock_redis():
    """Mock Redis session cache (SessionCache interface)."""
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=None)
    cache.bind = AsyncMock()
    return cache


@pytest.fixture
def mock_raw_redis():
    """Mock raw Redis client for setex/expire/get operations."""
    r = AsyncMock()
    r.setex = AsyncMock()
    r.expire = AsyncMock()
    r.get = AsyncMock(return_value=None)
    return r


@pytest.fixture
def patch_all_deps(mock_db, mock_redis, mock_raw_redis):
    """
    Patch all identity_v2 external dependencies: Redis, PostgreSQL, raw Redis.

    This fixture resets the module-level _redis_cache so _get_redis() re-initializes,
    and patches get_db, get_session_cache, and raw get_redis.
    """
    async def _get_raw():
        return mock_raw_redis

    with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
         patch("src.cache.get_session_cache", return_value=mock_redis), \
         patch("src.mcp_handlers.identity.resolution.get_db", return_value=mock_db), \
         patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
         patch("src.mcp_handlers.identity.handlers.get_db", return_value=mock_db), \
         patch("src.cache.redis_client.get_redis", new=_get_raw):
        yield


@pytest.fixture
def patch_no_redis(mock_db):
    """Patch dependencies with Redis unavailable (cache returns None)."""
    with patch("src.mcp_handlers.identity.persistence._redis_cache", False), \
         patch("src.mcp_handlers.identity.resolution.get_db", return_value=mock_db), \
         patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
         patch("src.mcp_handlers.identity.handlers.get_db", return_value=mock_db):
        yield


@pytest.fixture
def patch_mcp_server():
    """Patch get_mcp_server to return a mock with agent_metadata dict."""
    mock_server = MagicMock()
    mock_server.agent_metadata = {}
    with patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server):
        yield mock_server


# ============================================================================
# _generate_agent_id (pure function - no I/O)
# ============================================================================

class TestEnsureAgentPersisted:

    @pytest.mark.asyncio
    async def test_persists_new_agent(self):
        """When agent doesn't exist in PG, persists and returns True."""
        from src.mcp_handlers.identity.handlers import ensure_agent_persisted

        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_agent.return_value = None
        # First call: not persisted. After upsert: return identity for session creation.
        mock_db.get_identity.side_effect = [
            None,  # First check: not persisted
            SimpleNamespace(identity_id="new-ident", metadata={}),  # After upsert: for session creation
        ]
        mock_db.upsert_agent = AsyncMock()
        mock_db.upsert_identity = AsyncMock()
        mock_db.create_session = AsyncMock()

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            result = await ensure_agent_persisted("uuid-lazy", "session-lazy")

        assert result is True
        mock_db.upsert_agent.assert_called_once()
        mock_db.upsert_identity.assert_called_once()
        mock_db.create_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_already_persisted(self):
        """When agent already exists in PG, returns False without writing."""
        from src.mcp_handlers.identity.handlers import ensure_agent_persisted

        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_agent.return_value = {"id": "uuid-existing"}
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="existing-ident", metadata={}
        )

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            result = await ensure_agent_persisted("uuid-existing", "session-existing")

        assert result is False
        mock_db.upsert_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_repairs_missing_agent_row_when_identity_exists(self):
        """Identity-only persistence should recreate the missing core.agents row."""
        from src.mcp_handlers.identity.handlers import ensure_agent_persisted

        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_agent.return_value = None
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="existing-ident", metadata={}
        )
        mock_db.upsert_agent = AsyncMock()
        mock_db.upsert_identity = AsyncMock()
        mock_db.create_session = AsyncMock()

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            result = await ensure_agent_persisted("uuid-missing-agent", "session-missing-agent")

        assert result is True
        mock_db.upsert_agent.assert_called_once()
        mock_db.upsert_identity.assert_not_called()
        mock_db.create_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        """On exception, returns False (non-fatal)."""
        from src.mcp_handlers.identity.handlers import ensure_agent_persisted

        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_identity.side_effect = Exception("DB error")

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            result = await ensure_agent_persisted("uuid-error", "session-error")

        assert result is False

    @pytest.mark.asyncio
    async def test_persists_public_identity_handles_from_runtime_metadata(self):
        """Lazy persistence should carry structured/public identity info into metadata."""
        from src.mcp_handlers.identity.handlers import ensure_agent_persisted

        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_agent.return_value = None
        mock_db.get_identity.side_effect = [
            None,
            SimpleNamespace(identity_id="new-ident", metadata={}),
        ]
        mock_db.upsert_agent = AsyncMock()
        mock_db.upsert_identity = AsyncMock()
        mock_db.create_session = AsyncMock()

        mock_server = MagicMock()
        mock_server.agent_metadata = {
            "uuid-lazy": SimpleNamespace(structured_id="mcp_20260404", label="Codex Agent")
        }

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence.mcp_server", mock_server):
            result = await ensure_agent_persisted("uuid-lazy", "session-lazy")

        assert result is True
        metadata = mock_db.upsert_identity.await_args.kwargs["metadata"]
        assert metadata["public_agent_id"] == "mcp_20260404"
        assert metadata["structured_id"] == "mcp_20260404"
        assert metadata["label"] == "Codex Agent"

    @pytest.mark.asyncio
    async def test_persists_public_identity_handles_from_session_cache(self):
        """Lazy persistence should recover cached display_agent_id when runtime metadata is absent."""
        from src.mcp_handlers.identity.handlers import ensure_agent_persisted

        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_agent.return_value = None
        mock_db.get_identity.side_effect = [
            None,
            SimpleNamespace(identity_id="new-ident", metadata={}),
        ]
        mock_db.upsert_agent = AsyncMock()
        mock_db.upsert_identity = AsyncMock()
        mock_db.create_session = AsyncMock()

        mock_cache = AsyncMock()
        mock_cache.get.return_value = {
            "agent_id": "uuid-lazy",
            "display_agent_id": "mcp_20260404",
            "label": "Codex Agent",
        }

        mock_server = MagicMock()
        mock_server.agent_metadata = {}

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence._get_redis", return_value=mock_cache), \
             patch("src.mcp_handlers.identity.persistence.mcp_server", mock_server):
            result = await ensure_agent_persisted("uuid-lazy", "session-lazy")

        assert result is True
        metadata = mock_db.upsert_identity.await_args.kwargs["metadata"]
        assert metadata["public_agent_id"] == "mcp_20260404"
        assert metadata["label"] == "Codex Agent"

    @pytest.mark.asyncio
    async def test_persists_public_identity_handles_from_in_memory_session_cache(self):
        """Lazy persistence should recover public identity handles from in-memory session bindings when Redis is unavailable."""
        from src.mcp_handlers.identity.handlers import ensure_agent_persisted

        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_agent.return_value = None
        mock_db.get_identity.side_effect = [
            None,
            SimpleNamespace(identity_id="new-ident", metadata={}),
        ]
        mock_db.upsert_agent = AsyncMock()
        mock_db.upsert_identity = AsyncMock()
        mock_db.create_session = AsyncMock()

        mock_server = MagicMock()
        mock_server.agent_metadata = {}
        in_memory_bindings = {
            "session-lazy": {
                "bound_agent_id": "uuid-lazy",
                "display_agent_id": "mcp_20260404",
                "agent_label": "Codex Agent",
            }
        }

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence._get_redis", return_value=None), \
             patch("src.mcp_handlers.identity.persistence.mcp_server", mock_server), \
             patch("src.mcp_handlers.identity.shared._session_identities", in_memory_bindings):
            result = await ensure_agent_persisted("uuid-lazy", "session-lazy")

        assert result is True
        metadata = mock_db.upsert_identity.await_args.kwargs["metadata"]
        assert metadata["public_agent_id"] == "mcp_20260404"
        assert metadata["label"] == "Codex Agent"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode_fixture", ["patch_all_deps", "patch_no_redis"])
    async def test_session_binding_parity_between_redis_and_degraded_local(
        self,
        request,
        mode_fixture,
        mock_db,
    ):
        """Redis-backed and degraded-local modes should persist the same public identity handles."""
        from src.mcp_handlers.identity.handlers import _cache_session, ensure_agent_persisted

        request.getfixturevalue(mode_fixture)
        mock_db.get_agent.return_value = None
        mock_db.get_identity.side_effect = [
            None,
            SimpleNamespace(identity_id="new-ident", metadata={}),
        ]
        mock_db.upsert_agent = AsyncMock()
        mock_db.upsert_identity = AsyncMock()
        mock_db.create_session = AsyncMock()

        mock_server = MagicMock()
        mock_server.agent_metadata = {}
        session_bindings = {}

        with patch("src.mcp_handlers.identity.persistence.mcp_server", mock_server), \
             patch("src.mcp_handlers.identity.shared._session_identities", session_bindings):
            await _cache_session(
                "session-parity",
                "uuid-lazy",
                display_agent_id="mcp_20260404",
                label="Codex Agent",
            )
            assert session_bindings["session-parity"]["public_agent_id"] == "mcp_20260404"
            assert session_bindings["session-parity"]["agent_label"] == "Codex Agent"

            result = await ensure_agent_persisted("uuid-lazy", "session-parity")

        assert result is True
        metadata = mock_db.upsert_identity.await_args.kwargs["metadata"]
        assert metadata["public_agent_id"] == "mcp_20260404"
        assert metadata["agent_id"] == "mcp_20260404"
        assert metadata["label"] == "Codex Agent"

        client_info = mock_db.create_session.await_args.kwargs["client_info"]
        assert client_info["agent_id"] == "uuid-lazy"
        assert client_info["agent_uuid"] == "uuid-lazy"
        assert client_info["public_agent_id"] == "mcp_20260404"


# ============================================================================
# set_agent_label
# ============================================================================

class TestOnboardPin:

    @pytest.mark.asyncio
    async def test_set_and_lookup_pin(self):
        """Pin can be set and looked up."""
        from src.mcp_handlers.identity.handlers import set_onboard_pin, lookup_onboard_pin

        mock_raw = AsyncMock()
        stored_data = {}

        async def mock_setex(key, ttl, value):
            stored_data[key] = value

        async def mock_get(key):
            return stored_data.get(key)

        async def mock_expire(key, ttl):
            pass

        mock_raw.setex = mock_setex
        mock_raw.get = mock_get
        mock_raw.expire = mock_expire

        async def _get_raw():
            return mock_raw

        with patch("src.cache.redis_client.get_redis", new=_get_raw):
            set_result = await set_onboard_pin("ua:d20c2f", "uuid-123", "agent-uuid-123456")
            assert set_result is True

            lookup_result = await lookup_onboard_pin("ua:d20c2f")
            assert lookup_result == "agent-uuid-123456"

    @pytest.mark.asyncio
    async def test_set_pin_no_fingerprint(self):
        """set_onboard_pin with empty fingerprint returns False."""
        from src.mcp_handlers.identity.handlers import set_onboard_pin
        result = await set_onboard_pin("", "uuid-1", "sess-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_set_pin_none_fingerprint(self):
        """set_onboard_pin with None fingerprint returns False."""
        from src.mcp_handlers.identity.handlers import set_onboard_pin
        result = await set_onboard_pin(None, "uuid-1", "sess-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_lookup_pin_none_fingerprint(self):
        """lookup_onboard_pin with None fingerprint returns None."""
        from src.mcp_handlers.identity.handlers import lookup_onboard_pin
        result = await lookup_onboard_pin(None)
        assert result is None

    @pytest.mark.asyncio
    async def test_lookup_pin_empty_fingerprint(self):
        """lookup_onboard_pin with empty fingerprint returns None."""
        from src.mcp_handlers.identity.handlers import lookup_onboard_pin
        result = await lookup_onboard_pin("")
        assert result is None

    @pytest.mark.asyncio
    async def test_lookup_pin_no_redis(self):
        """lookup_onboard_pin returns None when Redis is unavailable."""
        from src.mcp_handlers.identity.handlers import lookup_onboard_pin

        async def _get_no_redis():
            return None

        with patch("src.cache.redis_client.get_redis", new=_get_no_redis):
            result = await lookup_onboard_pin("ua:test")
            assert result is None

    @pytest.mark.asyncio
    async def test_set_pin_no_redis(self):
        """set_onboard_pin returns False when Redis is unavailable."""
        from src.mcp_handlers.identity.handlers import set_onboard_pin

        async def _get_no_redis():
            return None

        with patch("src.cache.redis_client.get_redis", new=_get_no_redis):
            result = await set_onboard_pin("ua:test", "uuid-1", "sess-1")
            assert result is False


# ============================================================================
# handle_identity_v2 (tool handler, not the decorator adapter)
# ============================================================================

class TestHandleIdentityV2:

    @pytest.mark.asyncio
    async def test_basic_identity_resolution(self, patch_all_deps, mock_db):
        """Basic identity() call resolves and returns identity."""
        from src.mcp_handlers.identity.handlers import handle_identity_v2

        result = await handle_identity_v2(
            arguments={},
            session_key="handle-test-session",
        )

        assert result["success"] is True
        assert "agent_id" in result
        assert "agent_uuid" in result
        assert result["bound"] is True

    @pytest.mark.asyncio
    async def test_identity_with_model_type(self, patch_all_deps, mock_db):
        """identity(model_type=...) uses model in agent_id generation."""
        from src.mcp_handlers.identity.handlers import handle_identity_v2

        result = await handle_identity_v2(
            arguments={"model_type": "claude-opus-4"},
            session_key="model-type-session",
            model_type="claude-opus-4",
        )

        assert result["success"] is True
        assert "Claude_Opus_4" in result["agent_id"]

    @pytest.mark.asyncio
    async def test_identity_with_name_sets_label(self, patch_all_deps, mock_db):
        """identity(name='X') sets the agent label."""
        from src.mcp_handlers.identity.handlers import handle_identity_v2

        mock_db.find_agent_by_label.return_value = None
        mock_db.update_agent_fields.return_value = True
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.upsert_agent = AsyncMock()
        mock_db.upsert_identity = AsyncMock()
        mock_db.create_session = AsyncMock()

        with patch("src.mcp_handlers.shared.get_mcp_server", side_effect=Exception("no server")):
            result = await handle_identity_v2(
                arguments={"name": "TestBot"},
                session_key="name-set-session",
            )

        assert result["success"] is True
        assert result.get("label") == "TestBot"
        assert result.get("display_name") == "TestBot"




class TestIdentityResolutionIntegration:

    @pytest.mark.asyncio
    async def test_redis_miss_pg_miss_creates_new(self, patch_all_deps, mock_redis, mock_db):
        """Full pipeline: Redis miss -> PG miss -> Create new."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None

        result = await resolve_session_identity(
            session_key="integration-test-1",
            model_type="claude-opus-4",
        )

        assert result["created"] is True
        assert result["source"] == "memory_only"
        assert result["agent_id"].startswith("Claude_Opus_4_")

    @pytest.mark.asyncio
    async def test_consistent_uuid_on_second_call_via_redis(self, patch_all_deps, mock_redis, mock_db, mock_raw_redis):
        """Second call should get same UUID back from Redis cache."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        # First call: creates new agent (Redis and PG both miss)
        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None

        first = await resolve_session_identity(session_key="consistency-test")
        first_uuid = first["agent_uuid"]

        # Simulate Redis cache being populated: second call returns cached data
        mock_redis.get.return_value = {
            "agent_id": first_uuid,
            "display_agent_id": first["agent_id"],
        }
        mock_db.get_identity.return_value = None  # Not persisted

        second = await resolve_session_identity(session_key="consistency-test", resume=True)

        assert second["agent_uuid"] == first_uuid
        assert second["source"] == "redis"
        assert second["created"] is False

    @pytest.mark.asyncio
    async def test_ephemeral_then_persisted_via_ensure(self, patch_all_deps, mock_db):
        """Agent starts ephemeral, then gets persisted via ensure_agent_persisted."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity, ensure_agent_persisted

        # Create ephemeral
        result = await resolve_session_identity(session_key="ephemeral-test")
        assert result["persisted"] is False
        agent_uuid = result["agent_uuid"]

        # Now persist
        mock_db.get_identity.side_effect = [
            None,  # Not yet persisted
            SimpleNamespace(identity_id="new-ident", metadata={}),  # After upsert
        ]

        newly_persisted = await ensure_agent_persisted(agent_uuid, "ephemeral-test")
        assert newly_persisted is True
        mock_db.upsert_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_all_redis_and_pg_down_still_creates(self):
        """Even when both Redis and PG are down, a new identity is created."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_session.side_effect = Exception("PG down")
        mock_db.find_agent_by_label.return_value = None

        with patch("src.mcp_handlers.identity.persistence._redis_cache", False), \
             patch("src.mcp_handlers.identity.resolution.get_db", return_value=mock_db):
            result = await resolve_session_identity(session_key="all-down-test")

        assert result["created"] is True
        assert result["source"] == "memory_only"
        assert len(result["agent_uuid"]) == 36


# ============================================================================
# Edge cases
# ============================================================================

class TestEdgeCases:

    @pytest.mark.asyncio
    async def test_legacy_non_uuid_in_redis_cache(self, patch_all_deps, mock_redis, mock_db):
        """Legacy Redis entries with model+date format (not UUID) are handled."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        # Legacy format: agent_id is model+date, not UUID
        mock_redis.get.return_value = {"agent_id": "Claude_Opus_20260205"}
        mock_db.get_agent.return_value = SimpleNamespace(
            label="LegacyAgent", status="active"
        )
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="i1", metadata={"agent_uuid": "legacy-uuid-1234"}
        )

        result = await resolve_session_identity(session_key="legacy-redis-test", resume=True)

        assert result["source"] == "redis"
        assert result["agent_id"] == "Claude_Opus_20260205"

    @pytest.mark.asyncio
    async def test_legacy_non_uuid_in_pg(self, patch_no_redis, mock_db):
        """Legacy PG entries with model+date format session.agent_id are handled."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        # Legacy PG: agent_id stored as model+date
        mock_db.get_session.return_value = SimpleNamespace(
            agent_id="Gemini_Pro_20260101"
        )
        mock_db.get_agent.return_value = SimpleNamespace(
            label="LegacyPGAgent", status="active"
        )
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="i1", metadata={"agent_uuid": "legacy-pg-uuid"}
        )
        mock_db.get_agent_label.return_value = "LegacyPGAgent"

        result = await resolve_session_identity(session_key="legacy-pg-test", resume=True)

        assert result["source"] == "postgres"
        assert result["agent_id"] == "Gemini_Pro_20260101"

    @pytest.mark.asyncio
    async def test_session_key_with_only_colons(self, patch_all_deps):
        """Session key with only colons is valid (allowed chars)."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        result = await resolve_session_identity(session_key=":::")
        assert result["created"] is True

    @pytest.mark.asyncio
    async def test_session_key_at_exact_max_length(self, patch_all_deps):
        """Session key at exactly 256 chars passes without truncation."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        key = "a" * 256
        result = await resolve_session_identity(session_key=key)
        assert result["created"] is True

    @pytest.mark.asyncio
    async def test_session_key_at_257_chars_truncated(self, patch_all_deps):
        """Session key at 257 chars is truncated to 256."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        key = "a" * 257
        result = await resolve_session_identity(session_key=key)
        assert result["created"] is True


# ============================================================================
# GovernanceConfig import fallback (lines 41-45)
# ============================================================================

class TestGovernanceConfigFallback:
    """Test that GovernanceConfig has expected constants available."""

    def test_session_ttl_seconds_exists(self):
        """GovernanceConfig.SESSION_TTL_SECONDS is available."""
        from config.governance_config import GovernanceConfig
        assert hasattr(GovernanceConfig, "SESSION_TTL_SECONDS")
        assert isinstance(GovernanceConfig.SESSION_TTL_SECONDS, int)

    def test_session_ttl_hours_exists(self):
        """GovernanceConfig.SESSION_TTL_HOURS is available."""
        from config.governance_config import GovernanceConfig
        assert hasattr(GovernanceConfig, "SESSION_TTL_HOURS")


# ============================================================================
# _get_redis exception path (lines 62-64)
# ============================================================================

class TestOnboardPinExceptionPaths:

    @pytest.mark.asyncio
    async def test_lookup_pin_redis_exception_returns_none(self):
        """lookup_onboard_pin returns None when Redis throws (lines 1138-1140)."""
        from src.mcp_handlers.identity.handlers import lookup_onboard_pin

        async def _get_error_redis():
            raise Exception("Connection reset")

        with patch("src.cache.redis_client.get_redis", new=_get_error_redis):
            result = await lookup_onboard_pin("ua:test123")

        assert result is None

    @pytest.mark.asyncio
    async def test_set_pin_redis_exception_returns_false(self):
        """set_onboard_pin returns False when Redis throws (lines 1173-1175)."""
        from src.mcp_handlers.identity.handlers import set_onboard_pin

        async def _get_error_redis():
            raise Exception("Connection reset")

        with patch("src.cache.redis_client.get_redis", new=_get_error_redis):
            result = await set_onboard_pin("ua:test456", "uuid-1", "sess-1")

        assert result is False

    @pytest.mark.asyncio
    async def test_lookup_pin_with_bytes_data(self):
        """lookup_onboard_pin handles bytes data from Redis."""
        from src.mcp_handlers.identity.handlers import lookup_onboard_pin

        mock_raw = AsyncMock()
        pin_data = json.dumps({"client_session_id": "agent-abc123", "agent_uuid": "uuid-123"})
        mock_raw.get.return_value = pin_data.encode("utf-8")  # bytes, not str
        mock_raw.expire = AsyncMock()

        async def _get_raw():
            return mock_raw

        with patch("src.cache.redis_client.get_redis", new=_get_raw):
            result = await lookup_onboard_pin("ua:bytes-test")

        assert result == "agent-abc123"

    @pytest.mark.asyncio
    async def test_lookup_pin_no_refresh_ttl(self):
        """lookup_onboard_pin with refresh_ttl=False does not call expire."""
        from src.mcp_handlers.identity.handlers import lookup_onboard_pin

        mock_raw = AsyncMock()
        pin_data = json.dumps({"client_session_id": "agent-norefresh", "agent_uuid": "uuid-123"})
        mock_raw.get.return_value = pin_data
        mock_raw.expire = AsyncMock()

        async def _get_raw():
            return mock_raw

        with patch("src.cache.redis_client.get_redis", new=_get_raw):
            result = await lookup_onboard_pin("ua:norefresh", refresh_ttl=False)

        assert result == "agent-norefresh"
        mock_raw.expire.assert_not_called()

    @pytest.mark.asyncio
    async def test_lookup_pin_no_data_returns_none(self):
        """lookup_onboard_pin returns None when no pin data at key."""
        from src.mcp_handlers.identity.handlers import lookup_onboard_pin

        mock_raw = AsyncMock()
        mock_raw.get.return_value = None

        async def _get_raw():
            return mock_raw

        with patch("src.cache.redis_client.get_redis", new=_get_raw):
            result = await lookup_onboard_pin("ua:nodata")

        assert result is None


# ============================================================================
# handle_bind_session - explicit binding guardrails
# ============================================================================

class TestHandleBindSession:

    @pytest.mark.asyncio
    async def test_bind_session_accepts_matching_agent_id(self):
        """bind_session succeeds when expected agent_id matches resolved identity."""
        from src.mcp_handlers.identity.handlers import handle_bind_session

        target_uuid = str(uuid.uuid4())
        target_agent_id = "GPT_5_3_20260315"
        resolved = {
            "agent_uuid": target_uuid,
            "agent_id": target_agent_id,
            "label": "TestAgent",
            "created": False,
        }
        mock_db = AsyncMock()
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="ident-1")
        mock_db.create_session = AsyncMock()

        with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new=AsyncMock(return_value=resolved)), \
             patch("src.mcp_handlers.identity.handlers.derive_session_key", new=AsyncMock(return_value="mcp:test-session")), \
             patch("src.mcp_handlers.identity.handlers._cache_session", new=AsyncMock()), \
             patch("src.mcp_handlers.identity.handlers.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.context.get_session_signals", return_value=SimpleNamespace(user_agent="test")):
            result = await handle_bind_session({
                "client_session_id": "agent-abc123",
                "agent_id": target_agent_id,
                "resume": True,
            })
        data = parse_result(result)

        assert data["success"] is True
        assert data["bound"] is True
        assert data["agent_uuid"] == target_uuid

    @pytest.mark.asyncio
    async def test_bind_session_accepts_structured_alias(self):
        """bind_session strict mode should accept structured/public aliases for the target UUID."""
        from src.mcp_handlers.identity.handlers import handle_bind_session

        target_uuid = str(uuid.uuid4())
        resolved = {
            "agent_uuid": target_uuid,
            "agent_id": "Gpt_5_Codex_20260404",
            "label": "DogfoodIdentity",
            "created": False,
        }
        mock_db = AsyncMock()
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="ident-1",
            metadata={
                "public_agent_id": "Gpt_5_Codex_20260404",
                "structured_id": "mcp_20260404_5",
                "label": "DogfoodIdentity",
            },
        )
        mock_db.create_session = AsyncMock()

        with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new=AsyncMock(return_value=resolved)), \
             patch("src.mcp_handlers.identity.handlers.derive_session_key", new=AsyncMock(return_value="mcp:test-session")), \
             patch("src.mcp_handlers.identity.handlers._cache_session", new=AsyncMock()), \
             patch("src.mcp_handlers.identity.handlers.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.context.get_session_signals", return_value=SimpleNamespace(user_agent="test")):
            result = await handle_bind_session({
                "client_session_id": "agent-abc123",
                "agent_id": "mcp_20260404_5",
                "strict": True,
            })
        data = parse_result(result)

        assert data["success"] is True
        assert data["bound"] is True
        assert data["agent_uuid"] == target_uuid

    @pytest.mark.asyncio
    async def test_bind_session_rejects_agent_id_mismatch(self):
        """bind_session fails fast when expected agent_id doesn't match target."""
        from src.mcp_handlers.identity.handlers import handle_bind_session

        resolved = {
            "agent_uuid": str(uuid.uuid4()),
            "agent_id": "Claude_Code_20260315",
            "label": "ExistingAgent",
            "created": False,
        }

        with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new=AsyncMock(return_value=resolved)), \
             patch("src.mcp_handlers.identity.handlers.derive_session_key", new=AsyncMock(return_value="mcp:test-session")), \
             patch("src.mcp_handlers.context.get_session_signals", return_value=SimpleNamespace(user_agent="test")):
            result = await handle_bind_session({
                "client_session_id": "agent-abc123",
                "agent_id": "GPT_Code_20260315",
                "resume": True,
            })
        data = parse_result(result)

        assert data["success"] is False
        assert "mismatch" in data["error"]

    @pytest.mark.asyncio
    async def test_bind_session_strict_requires_agent_id(self):
        """strict=true requires agent_id to prevent accidental cross-binding."""
        from src.mcp_handlers.identity.handlers import handle_bind_session

        result = await handle_bind_session({
            "client_session_id": "agent-abc123",
            "strict": True,
        })
        data = parse_result(result)

        assert data["success"] is False
        assert "requires agent_id" in data["error"]

    @pytest.mark.asyncio
    async def test_bind_session_requires_explicit_resume(self):
        """bind_session rejects silent reattach without resume=true or strict=true."""
        from src.mcp_handlers.identity.handlers import handle_bind_session

        result = await handle_bind_session({
            "client_session_id": "agent-abc123",
        })
        data = parse_result(result)

        assert data["success"] is False
        assert "resume=true" in data["error"]


# ============================================================================
# handle_identity_adapter - full decorator-wrapped handler (lines 1208-1410)
# ============================================================================

class TestHandleIdentityAdapter:

    @pytest.fixture
    def patch_identity_deps(self, mock_db, mock_redis, mock_raw_redis):
        """Patch all deps for handle_identity_adapter tests."""
        async def _get_raw():
            return mock_raw_redis

        mock_server = MagicMock()
        mock_server.agent_metadata = {}

        with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=mock_redis), \
             patch("src.mcp_handlers.identity.handlers.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.resolution.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.cache.redis_client.get_redis", new=_get_raw), \
             patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value=None), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
             patch("src.mcp_handlers.context.update_context_agent_id"), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server):
            yield mock_server

    @pytest.mark.asyncio
    async def test_basic_identity_call(self, patch_identity_deps, mock_db, mock_redis):
        """Basic identity() call with no arguments returns identity info."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None

        result = await handle_identity_adapter({"client_session_id": "test-sess-1"})
        data = parse_result(result)

        assert data["success"] is True
        assert "uuid" in data
        assert "agent_id" in data
        assert "client_session_id" in data
        assert "session_resolution_source" in data
        assert "continuity_token_supported" in data
        assert data["identity_status"] == "created"
        assert data["identity_resolution_outcome"] == "minted_after_resume_miss"
        assert data["bound_identity"]["uuid"] == data["uuid"]
        assert data["bound_identity"]["agent_id"] == data["agent_id"]
        # identity_summary, quick_reference, session_continuity moved behind verbose=true
        assert "identity_summary" not in data
        assert "quick_reference" not in data
        assert "session_continuity" not in data

    @pytest.mark.asyncio
    async def test_identity_returns_stable_client_session_id_for_existing_identity(self, patch_identity_deps, mock_db, mock_redis):
        """identity() should always return the stable agent-... session handle, not the transport key."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": test_uuid,
            "display_agent_id": "Gpt_5_Codex_20260404",
            "label": "DogfoodIdentity",
        }
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="i1",
            metadata={"public_agent_id": "Gpt_5_Codex_20260404"},
        )
        mock_db.get_agent_label.return_value = "DogfoodIdentity"
        mock_db.get_agent_status = AsyncMock(return_value="active")

        result = await handle_identity_adapter({"client_session_id": "transport-derived-key", "resume": True})
        data = parse_result(result)

        assert data["success"] is True
        assert data["uuid"] == test_uuid
        assert data["client_session_id"] == f"agent-{test_uuid[:12]}"
        assert data["client_session_id"] != "transport-derived-key"

    @pytest.mark.asyncio
    async def test_identity_preserves_public_agent_id_over_structured_id(self, patch_identity_deps, mock_db, mock_redis):
        """identity() should surface canonical public_agent_id even when structured_id also exists."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": test_uuid,
            "display_agent_id": "Gpt_5_Codex_20260404",
            "label": "DogfoodIdentity",
        }
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="i1",
            metadata={
                "public_agent_id": "Gpt_5_Codex_20260404",
                "agent_id": "Gpt_5_Codex_20260404",
                "structured_id": "mcp_20260404_5",
            },
        )
        mock_db.get_agent_label.return_value = "DogfoodIdentity"
        mock_db.get_agent_status = AsyncMock(return_value="active")
        patch_identity_deps.agent_metadata[test_uuid] = SimpleNamespace(
            public_agent_id="Gpt_5_Codex_20260404",
            structured_id="mcp_20260404_5",
            label="DogfoodIdentity",
        )

        result = await handle_identity_adapter({"client_session_id": "transport-derived-key", "resume": True})
        data = parse_result(result)

        assert data["success"] is True
        assert data["agent_id"] == "Gpt_5_Codex_20260404"



    @pytest.mark.asyncio
    async def test_identity_resumes_existing_agent(self, patch_identity_deps, mock_db, mock_redis, mock_raw_redis):
        """identity() auto-resumes existing agent under base key."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": test_uuid,
            "display_agent_id": "Claude_20260207",
        }
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.get_agent_label.return_value = "ExistingAgent"

        result = await handle_identity_adapter({"client_session_id": "resume-test", "resume": True})
        data = parse_result(result)

        assert data["success"] is True
        assert data["uuid"] == test_uuid
        assert data.get("resumed") is True
        assert data["identity_resolution_outcome"] == "resumed"
        assert data["message"] == "Identity confirmed for 'ExistingAgent'"
        assert "Welcome back" not in data["message"]
        assert "agent_signature" not in data

    @pytest.mark.asyncio
    async def test_identity_archived_session_warning_uses_lite_response(self, patch_identity_deps, mock_db, mock_redis, mock_raw_redis):
        """Archived identity warning should not reattach a contradictory agent_signature."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": test_uuid,
            "display_agent_id": "Claude_20260207",
        }
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={}, status="archived")
        mock_db.get_agent_label.return_value = "ArchivedAgent"

        result = await handle_identity_adapter({"client_session_id": "resume-archived-test", "resume": True})
        data = parse_result(result)

        assert data["success"] is True
        assert data["uuid"] == test_uuid
        assert data.get("archived") is True
        assert data.get("resumed") is False
        assert "agent_signature" not in data

    @pytest.mark.asyncio
    async def test_identity_with_model_type_new_agent(self, patch_identity_deps, mock_db, mock_redis):
        """identity(model_type='claude-opus-4') for new agent uses model differentiation (lines 1262-1277)."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None

        result = await handle_identity_adapter({
            "client_session_id": "model-new-test",
            "model_type": "claude-opus-4",
        })
        data = parse_result(result)

        assert data["success"] is True
        assert "Claude_Opus_4" in data.get("agent_id", "")

    @pytest.mark.asyncio
    async def test_identity_with_model_type_gemini(self, patch_identity_deps, mock_db, mock_redis):
        """Model normalization works for gemini (lines 1267-1268)."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None

        result = await handle_identity_adapter({
            "client_session_id": "gemini-test",
            "model_type": "gemini-pro-1.5",
        })
        data = parse_result(result)

        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_identity_with_model_type_gpt(self, patch_identity_deps, mock_db, mock_redis):
        """Model normalization works for gpt (lines 1269-1270)."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None

        result = await handle_identity_adapter({
            "client_session_id": "gpt-test",
            "model_type": "gpt-4-turbo",
        })
        data = parse_result(result)

        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_identity_with_model_type_composer(self, patch_identity_deps, mock_db, mock_redis):
        """Model normalization works for composer/cursor (lines 1271-1272)."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None

        result = await handle_identity_adapter({
            "client_session_id": "composer-test",
            "model_type": "cursor-composer",
        })
        data = parse_result(result)

        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_identity_with_model_type_llama(self, patch_identity_deps, mock_db, mock_redis):
        """Model normalization works for llama (lines 1273-1274)."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None

        result = await handle_identity_adapter({
            "client_session_id": "llama-test",
            "model_type": "llama-3.1-70b",
        })
        data = parse_result(result)

        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_identity_force_new(self, patch_identity_deps, mock_db, mock_redis):
        """identity(force_new=true) skips existing check and creates new."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None

        result = await handle_identity_adapter({
            "client_session_id": "force-new-adapter",
            "force_new": True,
        })
        data = parse_result(result)

        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_identity_model_type_in_response(self, patch_identity_deps, mock_db, mock_redis):
        """model_type is included in response when provided (line 1361)."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None

        result = await handle_identity_adapter({
            "client_session_id": "model-response-test",
            "model_type": "claude-opus-4",
        })
        data = parse_result(result)

        assert data.get("model_type") == "claude-opus-4"

    @pytest.mark.asyncio
    async def test_identity_none_arguments_handled(self, patch_identity_deps, mock_db, mock_redis):
        """identity() with None arguments does not crash (line 1407-1408)."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None

        # The decorator passes arguments=arguments, so None would come from decorator
        # but the function defaults to {} if None. Test with empty dict.
        result = await handle_identity_adapter({})
        data = parse_result(result)

        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_identity_existing_under_model_key_resumes(self, patch_identity_deps, mock_db, mock_redis, mock_raw_redis):
        """When no base key match but model-suffixed key matches, resumes (lines 1281-1303)."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        test_uuid = str(uuid.uuid4())

        call_count = [0]
        original_get = mock_redis.get

        async def side_effect_get(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call (base key): miss (created=True in resolve)
                return None
            else:
                # Second call (model-suffixed key): hit
                return {"agent_id": test_uuid, "display_agent_id": "Claude_20260207"}

        mock_redis.get.side_effect = side_effect_get
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.get_agent_label.return_value = "ModelAgent"

        result = await handle_identity_adapter({
            "client_session_id": "model-key-resume",
            "model_type": "claude-opus-4",
            "resume": True,
        })
        data = parse_result(result)

        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_identity_update_label_on_resume(self, patch_identity_deps, mock_db, mock_redis, mock_raw_redis):
        """identity(name='X') updates label on existing resumed agent (lines 1246-1249, 1291-1294)."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": test_uuid,
            "display_agent_id": "Claude_20260207",
        }
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.get_agent_label.return_value = "OldName"
        mock_db.find_agent_by_label.return_value = None
        mock_db.update_agent_fields.return_value = True

        result = await handle_identity_adapter({
            "client_session_id": "label-update-test",
            "name": "NewName",
            "resume": True,
        })
        data = parse_result(result)

        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_identity_persists_newly_created_agent(self, patch_identity_deps, mock_db, mock_redis):
        """
        When identity() creates a new agent, it MUST persist it before returning.

        Otherwise the continuity_token we emit references a UUID that only
        exists in-memory, and PATH 2.8 rebind fails with `agent not active`
        the next time the caller tries to resume. That failure mode produced
        the ghost identity proliferation that earlier fixes (718ccd3,
        d4d4370) were still papering over.

        Regression test for the "identity() is lazy" bug caught in dogfood
        on 2026-04-14.
        """
        from src.mcp_handlers.identity import handlers as identity_handlers

        # Fresh session — no prior identity. resolve_session_identity will
        # reach PATH 3 and mint a new UUID in-memory.
        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.get_identity.return_value = None
        mock_db.get_agent.return_value = None

        # Spy on ensure_agent_persisted so we can assert the handler calls it.
        spy = AsyncMock(return_value=True)
        with patch.object(identity_handlers, "ensure_agent_persisted", spy):
            result = await identity_handlers.handle_identity_adapter({
                "client_session_id": "test-fresh-identity-persist",
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data["identity_status"] == "created"
        # The emitted token must reference an agent that was persisted.
        assert spy.await_count == 1, "ensure_agent_persisted must be called once for a fresh identity"
        call_args = spy.await_args
        persisted_uuid = call_args.args[0] if call_args.args else call_args.kwargs.get("agent_uuid")
        assert persisted_uuid == data["uuid"], (
            "persistence must happen for the same UUID returned to the caller — "
            "otherwise the continuity_token is a promise we can't keep"
        )

    @pytest.mark.asyncio
    async def test_identity_binds_returned_stable_client_session_id(self, patch_identity_deps, mock_db, mock_redis):
        """Fresh identity() must bind the stable client_session_id it returns."""
        from src.mcp_handlers.identity import handlers as identity_handlers

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.get_identity.return_value = None
        mock_db.get_agent.return_value = None

        persist_spy = AsyncMock(return_value=True)
        bind_spy = AsyncMock(return_value={"bound": True})
        with patch.object(identity_handlers, "ensure_agent_persisted", persist_spy), \
             patch.object(identity_handlers, "_perform_session_bind", bind_spy):
            result = await identity_handlers.handle_identity_adapter({
                "client_session_id": "transport-session",
                "force_new": True,
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data["client_session_id"] == f"agent-{data['uuid'][:12]}"
        bind_spy.assert_any_await(
            agent_uuid=data["uuid"],
            session_key=data["client_session_id"],
            display_agent_id=data["agent_id"],
            source="identity_stable_session",
        )

    @pytest.mark.asyncio
    async def test_identity_does_not_re_persist_existing_agent(self, patch_identity_deps, mock_db, mock_redis):
        """
        identity() for an already-persisted agent must not redundantly call
        ensure_agent_persisted — the agent is already in PG, and re-upsert
        would churn last_update timestamps without cause.
        """
        from src.mcp_handlers.identity import handlers as identity_handlers

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": test_uuid,
            "display_agent_id": "Preexisting",
            "label": "Preexisting",
        }
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="i1",
            metadata={"public_agent_id": "Preexisting"},
        )
        mock_db.get_agent_label.return_value = "Preexisting"
        mock_db.get_agent_status = AsyncMock(return_value="active")

        spy = AsyncMock(return_value=False)
        with patch.object(identity_handlers, "ensure_agent_persisted", spy):
            result = await identity_handlers.handle_identity_adapter({
                "client_session_id": "preexisting-session",
                "resume": True,
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data["uuid"] == test_uuid
        assert spy.await_count == 0, "must not re-persist an already-resumed identity"

    # ------------------------------------------------------------------
    # PATH 0 characterization (UUID-direct resume, pre-refactor snapshot).
    # These lock in the fast-path / slow-path / error semantics so the
    # per-PATH extraction refactor stays behavior-preserving.
    # Part C gate (token ownership) is covered in test_identity_honesty_partc.py;
    # these tests use log-mode to exercise the resolution paths without
    # reconstructing tokens.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_path0_monitor_cache_hit_skips_db(self, monkeypatch):
        """PATH 0 FAST: UUID in monitor cache returns source=monitor_cache without a DB call."""
        monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "log")
        from src.mcp_handlers.identity import handlers as identity_handlers

        agent_uuid = "aaaaaaaa-1111-2222-3333-444444444444"
        fake_server = MagicMock(
            monitors={agent_uuid: MagicMock()},
            agent_metadata={},
        )
        db_spy = AsyncMock(return_value=True)
        status_spy = AsyncMock(return_value="active")

        with patch("src.mcp_handlers.shared.get_mcp_server", return_value=fake_server), \
             patch.object(identity_handlers, "_agent_exists_in_postgres", db_spy), \
             patch.object(identity_handlers, "_get_agent_status", status_spy):
            result = await identity_handlers.handle_identity_adapter({
                "agent_uuid": agent_uuid,
                "resume": True,
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data["uuid"] == agent_uuid
        assert data.get("source") == "monitor_cache", (
            f"Monitor-cache hit must not hit DB; got source={data.get('source')!r}"
        )
        assert db_spy.await_count == 0, "Monitor-cache hit must not query _agent_exists_in_postgres"
        assert status_spy.await_count == 0, "Monitor-cache hit must not query _get_agent_status"

    @pytest.mark.asyncio
    async def test_path0_db_slow_path_when_not_in_monitors(self, monkeypatch):
        """PATH 0 slow path: UUID absent from monitors falls through to DB-backed verification."""
        monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "log")
        from src.mcp_handlers.identity import handlers as identity_handlers

        agent_uuid = "bbbbbbbb-1111-2222-3333-444444444444"
        fake_server = MagicMock(monitors={}, agent_metadata={})
        db_spy = AsyncMock(return_value=True)
        status_spy = AsyncMock(return_value="active")
        meta_spy = AsyncMock(return_value="Slow_Path_Agent")
        label_spy = AsyncMock(return_value="SlowPathLabel")
        cache_spy = AsyncMock()

        with patch("src.mcp_handlers.shared.get_mcp_server", return_value=fake_server), \
             patch.object(identity_handlers, "_agent_exists_in_postgres", db_spy), \
             patch.object(identity_handlers, "_get_agent_status", status_spy), \
             patch.object(identity_handlers, "_get_agent_id_from_metadata", meta_spy), \
             patch.object(identity_handlers, "_get_agent_label", label_spy), \
             patch.object(identity_handlers, "_cache_session", cache_spy):
            result = await identity_handlers.handle_identity_adapter({
                "agent_uuid": agent_uuid,
                "resume": True,
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data["uuid"] == agent_uuid
        assert data.get("resumed") is True
        assert data.get("resumed_by_uuid") is True
        assert data.get("source") != "monitor_cache", "Slow path must not claim monitor_cache"
        assert data["message"] == "Identity confirmed for 'SlowPathLabel' via UUID"
        assert "Welcome back" not in data["message"]
        assert db_spy.await_count == 1, "Slow path must verify existence in DB"
        assert status_spy.await_count == 1, "Slow path must verify agent is active"

    @pytest.mark.asyncio
    async def test_path0_uuid_not_found_returns_error(self, monkeypatch):
        """PATH 0 slow path: UUID absent from DB returns uuid_not_found error (no ghost creation)."""
        monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "log")
        from src.mcp_handlers.identity import handlers as identity_handlers

        agent_uuid = "cccccccc-1111-2222-3333-444444444444"
        fake_server = MagicMock(monitors={}, agent_metadata={})

        with patch("src.mcp_handlers.shared.get_mcp_server", return_value=fake_server), \
             patch.object(identity_handlers, "_agent_exists_in_postgres", AsyncMock(return_value=False)):
            result = await identity_handlers.handle_identity_adapter({
                "agent_uuid": agent_uuid,
                "resume": True,
            })

        data = parse_result(result)
        assert data.get("success") is False
        assert data.get("error_code") == "UUID_NOT_FOUND" or "not found" in (data.get("error") or "").lower()
        recovery = data.get("recovery") or {}
        assert recovery.get("reason") == "uuid_not_found"
        assert recovery.get("agent_uuid") == agent_uuid

    @pytest.mark.asyncio
    async def test_path0_uuid_not_active_returns_error(self, monkeypatch):
        """PATH 0 slow path: UUID in DB but status != active returns error (no silent archive resume)."""
        monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "log")
        from src.mcp_handlers.identity import handlers as identity_handlers

        agent_uuid = "dddddddd-1111-2222-3333-444444444444"
        fake_server = MagicMock(monitors={}, agent_metadata={})

        with patch("src.mcp_handlers.shared.get_mcp_server", return_value=fake_server), \
             patch.object(identity_handlers, "_agent_exists_in_postgres", AsyncMock(return_value=True)), \
             patch.object(identity_handlers, "_get_agent_status", AsyncMock(return_value="archived")):
            result = await identity_handlers.handle_identity_adapter({
                "agent_uuid": agent_uuid,
                "resume": True,
            })

        data = parse_result(result)
        assert data.get("success") is False
        assert "not active" in (data.get("error") or "").lower()
        recovery = data.get("recovery") or {}
        assert recovery.get("reason") == "uuid_not_found"
        assert recovery.get("status") == "archived"

    @pytest.mark.asyncio
    async def test_resume_failed_branch_returns_error(self, patch_identity_deps, mock_db, mock_redis):
        """resolve_session_identity returning resume_failed must propagate as explicit error, not silent fork."""
        from src.mcp_handlers.identity import handlers as identity_handlers

        fake_token_uuid = "eeeeeeee-1111-2222-3333-444444444444"

        async def _fake_resolve(session_key, persist, resume, token_agent_uuid=None):
            return {
                "resume_failed": True,
                "message": "agent_not_active",
                "token_agent_uuid": fake_token_uuid,
            }

        with patch.object(identity_handlers, "resolve_session_identity", side_effect=_fake_resolve):
            result = await identity_handlers.handle_identity_adapter({
                "client_session_id": "resume-failed-test",
                "resume": True,
            })

        data = parse_result(result)
        assert data.get("success") is False
        assert "agent_not_active" in (data.get("error") or "") or "resume" in (data.get("error") or "").lower()
        recovery = data.get("recovery") or {}
        assert recovery.get("reason") == "resume_failed"
        assert recovery.get("token_agent_uuid") == fake_token_uuid

    # ------------------------------------------------------------------
    # Post-refactor coverage gaps (KG 2026-04-19T02:22:37).
    # Branch 7: Part C gate rejects a token whose `aid` claim does not
    # match the requested agent_uuid (the "wrong owner" case, distinct
    # from the bare-UUID case already covered in Part C tests).
    # Branch 22: parent_agent_id / spawn_reason flow from adapter args
    # into ensure_agent_persisted on the create path.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_path0_partc_mismatched_token_strict_rejects(self, monkeypatch):
        """Part C: token minted for uuid-A cannot authorize agent_uuid=uuid-B (strict mode).

        The bare-UUID case is covered by test_identity_honesty_partc.py. This
        locks in the mismatched-token case: a caller who holds a valid token
        for their own UUID cannot use it to resurrect someone else's UUID.
        Same invariant (#4: UUIDs are not lookup keys in disguise), distinct
        code path (_partc_owned fails because token_aid != _direct_uuid, not
        because token is absent).
        """
        monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "strict")
        monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", "test-secret-mismatch")

        from src.mcp_handlers.identity.session import create_continuity_token
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        uuid_legitimate = "aaaaaaaa-0000-1111-2222-333333333333"
        uuid_victim     = "bbbbbbbb-0000-1111-2222-333333333333"

        # Token honestly minted for uuid_legitimate.
        token = create_continuity_token(uuid_legitimate, "test-session")
        assert token is not None, "token creation prerequisite"

        fake_server = MagicMock(monitors={}, agent_metadata={})
        with patch("src.mcp_handlers.shared.get_mcp_server", return_value=fake_server):
            result = await handle_identity_adapter({
                "agent_uuid": uuid_victim,       # trying to resume someone else's UUID
                "continuity_token": token,        # with a token we legitimately hold
                "resume": True,
            })

        data = parse_result(result)
        assert data.get("success") is False, (
            f"Strict mode must reject mismatched-owner tokens. Got: {data}"
        )
        recovery = data.get("recovery") or {}
        assert recovery.get("reason") == "bare_uuid_resume_denied", (
            f"Expected bare_uuid_resume_denied for mismatched-owner case; got {recovery!r}"
        )
        assert recovery.get("agent_uuid") == uuid_victim

    @pytest.mark.asyncio
    async def test_identity_forwards_parent_and_spawn_reason_to_persist(
        self, patch_identity_deps, mock_db, mock_redis,
    ):
        """identity(parent_agent_id=..., spawn_reason=...) must forward to ensure_agent_persisted.

        Subagent / spawned-agent lineage depends on this — if the forwarding
        breaks, lineage drops silently and new identities land as root agents
        in core.identities instead of carrying a parent link. No test
        currently exercises this path through the adapter; coverage gap
        tracked in KG 2026-04-19T02:22:37 (Branch 22).
        """
        from src.mcp_handlers.identity import handlers as identity_handlers

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.get_identity.return_value = None
        mock_db.get_agent.return_value = None

        parent_uuid = "11111111-2222-3333-4444-555555555555"
        spawn_reason = "subagent"

        spy = AsyncMock(return_value=True)
        with patch.object(identity_handlers, "ensure_agent_persisted", spy):
            result = await identity_handlers.handle_identity_adapter({
                "client_session_id": "lineage-forward-test",
                "parent_agent_id": parent_uuid,
                "spawn_reason": spawn_reason,
            })

        data = parse_result(result)
        assert data["success"] is True
        assert data["identity_status"] == "created"
        assert spy.await_count == 1, "ensure_agent_persisted must be called for a fresh identity"

        call = spy.await_args
        kwargs = call.kwargs or {}
        assert kwargs.get("parent_agent_id") == parent_uuid, (
            f"parent_agent_id must forward through the adapter; got {kwargs!r}"
        )
        assert kwargs.get("spawn_reason") == spawn_reason, (
            f"spawn_reason must forward through the adapter; got {kwargs!r}"
        )


# ============================================================================
# handle_onboard_v2 - full flow (lines 1480-1857)
# ============================================================================

class TestHandleOnboardV2:

    @pytest.fixture
    def patch_onboard_deps(self, mock_db, mock_redis, mock_raw_redis):
        """Patch all deps for handle_onboard_v2 tests."""
        async def _get_raw():
            return mock_raw_redis

        def _discard_task(coro, **kwargs):
            try:
                coro.close()
            except Exception:
                pass
            task = MagicMock()
            task.cancel = MagicMock()
            return task

        mock_server = MagicMock()
        mock_server.agent_metadata = {}

        with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=mock_redis), \
             patch("src.mcp_handlers.identity.handlers.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.resolution.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.cache.redis_client.get_redis", new=_get_raw), \
             patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value="test-ctx-key"), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_client_hint", return_value="test"), \
             patch("src.mcp_handlers.context.update_context_agent_id"), \
             patch("asyncio.create_task", side_effect=_discard_task), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server), \
             patch("src.mcp_handlers.identity.shared._register_uuid_prefix"):
            yield mock_server

    @pytest.mark.asyncio
    async def test_basic_onboard_new_agent(self, patch_onboard_deps, mock_db, mock_redis):
        """Basic onboard() creates a new agent."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        # For ensure_agent_persisted
        mock_db.get_identity.side_effect = [
            None,  # resolve_session_identity PG lookup
            None,  # ensure_agent_persisted check
            SimpleNamespace(identity_id="new-ident", metadata={}),  # after upsert
        ]

        result = await handle_onboard_v2({"client_session_id": "onboard-new"})
        data = parse_result(result)

        assert data["success"] is True
        assert data["is_new"] is True
        assert data["identity_resolution_outcome"] == "minted_after_resume_miss"
        assert "uuid" in data
        assert "client_session_id" in data
        assert "session_resolution_source" in data
        assert "continuity_token_supported" in data
        assert "date_context" in data
        assert "next_step" in data
        # next_calls, session_continuity, workflow moved behind verbose=true
        assert "next_calls" not in data
        assert "session_continuity" not in data
        assert "workflow" not in data
        assert "what_this_does" not in data

    @pytest.mark.asyncio
    async def test_arg_less_onboard_gates_to_fresh_per_v2_ontology(self, patch_onboard_deps, mock_db, mock_redis, caplog):
        """S13: arg-less onboard() with no proof signal mints fresh per v2 ontology.

        Per identity.md §"Layered taxonomy of continuity", a fresh process-instance
        with no proof signal (no continuity_token, agent_uuid, agent_id,
        client_session_id, or name) mints fresh. The v2 gate at handle_onboard_v2
        entry flips force_new=True and emits a [FRESH_INSTANCE] log line.
        """
        import logging
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.side_effect = [
            None,
            None,
            SimpleNamespace(identity_id="new-ident", metadata={}),
        ]

        with caplog.at_level(logging.INFO, logger="src.mcp_handlers.identity.handlers"):
            result = await handle_onboard_v2({})

        data = parse_result(result)
        assert data["success"] is True
        assert data["is_new"] is True
        assert any(
            "[FRESH_INSTANCE]" in record.getMessage() for record in caplog.records
        ), "v2 gate should emit [FRESH_INSTANCE] log line for arg-less onboard()"

    @pytest.mark.asyncio
    async def test_arg_less_identity_gates_to_fresh_per_v2_ontology(self, patch_onboard_deps, mock_db, mock_redis, caplog):
        """S13 (2026-04-25): arg-less identity() with no proof signal mints fresh.

        Mirror of test_arg_less_onboard_gates_to_fresh — handle_identity_adapter
        gained a v2 gate analogous to handle_onboard_v2's so identity() also
        short-circuits to fresh-mint instead of silently re-binding via the
        IP:UA pin path. [FRESH_INSTANCE] log line confirms gate fired.
        """
        import logging
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.side_effect = [
            None,
            None,
            SimpleNamespace(identity_id="new-ident-v2", metadata={}),
        ]

        with caplog.at_level(logging.INFO, logger="src.mcp_handlers.identity.handlers"):
            await handle_identity_adapter({})

        assert any(
            "[FRESH_INSTANCE]" in record.getMessage()
            and "identity()" in record.getMessage()
            for record in caplog.records
        ), "v2 gate should emit [FRESH_INSTANCE] log line for arg-less identity()"

    @pytest.mark.asyncio
    async def test_arg_less_identity_with_context_binding_does_not_fork(self, patch_onboard_deps, mock_db, mock_redis, caplog):
        """A session-bound identity() call is introspection, not a fresh-process mint."""
        import logging
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        existing_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": existing_uuid,
            "display_agent_id": "Codex_20260429",
        }
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="existing-ident",
            metadata={"agent_id": "Codex_20260429", "label": "Codex"},
        )

        with patch("src.mcp_handlers.context.get_context_agent_id", return_value=existing_uuid), \
             caplog.at_level(logging.INFO, logger="src.mcp_handlers.identity.handlers"):
            result = await handle_identity_adapter({})

        data = parse_result(result)
        assert data["success"] is True
        assert data["uuid"] == existing_uuid
        assert data["identity_status"] == "resumed"
        assert not any(
            "[FRESH_INSTANCE]" in record.getMessage()
            and "identity()" in record.getMessage()
            for record in caplog.records
        ), "session-bound identity() must not fire the fresh-instance gate"

    @pytest.mark.asyncio
    async def test_proof_signal_bypasses_identity_v2_gate(self, patch_onboard_deps, mock_db, mock_redis, caplog):
        """S13: identity() with a proof signal must not fire the v2 gate.

        Preserves the proof-signal resume path: identity(continuity_token=...)
        or identity(agent_uuid=...) callers should still resolve to the prior
        identity, not get gated to fresh-mint.
        """
        import logging
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        existing_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": existing_uuid,
            "display_agent_id": "Claude_20260207",
        }
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="existing-ident",
            metadata={"agent_id": "Claude_20260207"},
        )

        with caplog.at_level(logging.INFO, logger="src.mcp_handlers.identity.handlers"):
            await handle_identity_adapter({"client_session_id": "preserve-resume-path"})

        assert not any(
            "[FRESH_INSTANCE]" in record.getMessage()
            and "identity()" in record.getMessage()
            for record in caplog.records
        ), "identity() v2 gate must NOT fire when client_session_id is presented"

    @pytest.mark.asyncio
    async def test_bind_session_non_coupling_to_s13_gate(self, patch_onboard_deps, mock_db, mock_redis, caplog):
        """S13: bind_session shares the derive_session_key plumbing but must not
        be coupled to the identity-adapter v2 gate. bind_session callers always
        present an explicit client_session_id (it's the bind target), so the
        gate's "no proof signal" predicate is structurally false. This test
        documents that contract — if a future refactor moves the gate into
        derive_session_key, bind_session callers would silently lose their
        resume path. The fix would not be obvious without this regression.
        """
        import logging
        from src.mcp_handlers.identity.handlers import handle_bind_session

        target_uuid = str(uuid.uuid4())
        resolved = {
            "agent_uuid": target_uuid,
            "agent_id": "Claude_20260225",
            "label": "TestAgent",
            "created": False,
        }
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="ident-1")
        mock_db.create_session = AsyncMock()

        with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new=AsyncMock(return_value=resolved)), \
             patch("src.mcp_handlers.identity.handlers.derive_session_key", new=AsyncMock(return_value="mcp:test-session")), \
             patch("src.mcp_handlers.identity.handlers._cache_session", new=AsyncMock()), \
             patch("src.mcp_handlers.identity.handlers.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.context.get_session_signals", return_value=SimpleNamespace(user_agent="test")), \
             caplog.at_level(logging.INFO, logger="src.mcp_handlers.identity.handlers"):
            result = await handle_bind_session({
                "client_session_id": "agent-bind-test",
                "agent_id": "Claude_20260225",
                "resume": True,
            })
        data = parse_result(result)

        # bind_session resolves to the existing identity (not fresh-minted)
        assert data["success"] is True
        assert data["bound"] is True
        assert data["agent_uuid"] == target_uuid
        # And the v2 gate did NOT fire — bind_session's resume path is preserved
        assert not any(
            "[FRESH_INSTANCE]" in record.getMessage() for record in caplog.records
        ), "bind_session must not be gated by S13's fresh-instance posture"

    @pytest.mark.asyncio
    async def test_proof_signal_bypasses_v2_gate(self, patch_onboard_deps, mock_db, mock_redis, caplog):
        """S13: passing client_session_id is a proof signal — gate must NOT fire.

        Preserves PATH 1/2 active-session-binding semantics (S9 territory).
        Without this carve-out, S13 would silently retire S9's resume path.
        """
        import logging
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.side_effect = [
            None,
            None,
            SimpleNamespace(identity_id="new-ident", metadata={}),
        ]

        with caplog.at_level(logging.INFO, logger="src.mcp_handlers.identity.handlers"):
            result = await handle_onboard_v2({"client_session_id": "preserve-resume-path"})

        data = parse_result(result)
        assert data["success"] is True
        assert not any(
            "[FRESH_INSTANCE]" in record.getMessage() for record in caplog.records
        ), "v2 gate must NOT fire when client_session_id is presented as a proof signal"

    @pytest.mark.asyncio
    async def test_onboard_resumes_existing_identity_by_default(self, patch_onboard_deps, mock_db, mock_redis, mock_raw_redis):
        """onboard(client_session_id=...) resumes existing identity — proof signal preserves resume default per S13."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        existing_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": existing_uuid,
            "display_agent_id": "Claude_20260207",
        }
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="i1", metadata={"agent_id": "Claude_20260207"}
        )
        mock_db.get_agent_label.return_value = "ExistingAgent"
        mock_db.get_agent_status = AsyncMock(return_value="active")
        mock_db.create_session = AsyncMock()

        result = await handle_onboard_v2({"client_session_id": "onboard-resume"})
        data = parse_result(result)

        assert data["success"] is True
        assert data["is_new"] is False
        assert data["uuid"] == existing_uuid
        assert data["identity_resolution_outcome"] == "resumed"

    @pytest.mark.asyncio
    async def test_onboard_then_identity_with_stable_session_id_keeps_same_uuid(self, patch_onboard_deps, mock_db, mock_redis, mock_raw_redis):
        """Stable client_session_id returned by onboard() should resolve to the same UUID in identity()."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2, handle_identity_adapter

        existing_uuid = str(uuid.uuid4())
        display_agent_id = "Gpt_5_Codex_20260401"
        stored = {}

        async def raw_setex(key, ttl, value):
            stored[key] = value

        async def cache_get(session_id):
            if session_id in {"resume-base", "resume-base:gpt"}:
                return {
                    "agent_id": existing_uuid,
                    "display_agent_id": display_agent_id,
                    "label": "Codex Dogfood",
                }
            raw = stored.get(f"session:{session_id}")
            if raw:
                return json.loads(raw)
            return None

        mock_raw_redis.setex.side_effect = raw_setex
        mock_redis.get.side_effect = cache_get
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="i1",
            metadata={"agent_id": display_agent_id},
        )
        mock_db.get_agent_label.return_value = "Codex Dogfood"
        mock_db.get_agent_status = AsyncMock(return_value="active")
        mock_db.create_session = AsyncMock()

        onboard_result = await handle_onboard_v2({
            "client_session_id": "resume-base",
            "resume": True,
            "model_type": "gpt-5-codex",
        })
        onboard_data = parse_result(onboard_result)
        stable_session_id = onboard_data["client_session_id"]

        identity_result = await handle_identity_adapter({
            "client_session_id": stable_session_id,
            "resume": True,
            "model_type": "gpt-5-codex",
        })
        identity_data = parse_result(identity_result)

        assert onboard_data["uuid"] == existing_uuid
        assert identity_data["uuid"] == existing_uuid
        assert identity_data["client_session_id"] == stable_session_id

    @pytest.mark.asyncio
    async def test_onboard_persists_stable_session_id_for_redis_miss(self, patch_onboard_deps, mock_db, mock_redis, mock_raw_redis):
        """Returned stable client_session_id should still resume via PostgreSQL when Redis misses."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2, handle_identity_adapter

        sessions = {}
        created_uuid = None
        stable_session_id = None

        async def cache_get(session_id):
            return None

        async def create_session(session_id, identity_id, expires_at, client_type="mcp", client_info=None):
            sessions[session_id] = SimpleNamespace(
                agent_id=client_info.get("agent_uuid"),
                client_info=client_info,
            )

        async def get_session(session_id):
            return sessions.get(session_id)

        async def get_identity(agent_uuid):
            return SimpleNamespace(
                identity_id=f"ident-{agent_uuid[:8]}",
                metadata={"agent_id": "Gpt_5_4_Codex_20260401"},
            )

        mock_redis.get.side_effect = cache_get
        mock_db.get_session.side_effect = get_session
        mock_db.create_session.side_effect = create_session
        mock_db.get_identity.side_effect = get_identity
        mock_db.get_agent_label.return_value = "Codex Dogfood"
        mock_db.find_agent_by_label.return_value = None

        onboard_result = await handle_onboard_v2({
            "client_session_id": "resume-base",
            "resume": True,
            "model_type": "gpt-5.4-codex",
        })
        onboard_data = parse_result(onboard_result)
        created_uuid = onboard_data["uuid"]
        stable_session_id = onboard_data["client_session_id"]
        public_agent_id = onboard_data["agent_id"]

        assert stable_session_id in sessions
        assert sessions[stable_session_id].agent_id == created_uuid
        assert sessions[stable_session_id].client_info["public_agent_id"] == public_agent_id
        assert sessions[stable_session_id].client_info["agent_id"] == public_agent_id

        identity_result = await handle_identity_adapter({
            "client_session_id": stable_session_id,
            "resume": True,
            "model_type": "gpt-5.4-codex",
        })
        identity_data = parse_result(identity_result)

        assert identity_data["uuid"] == created_uuid
        assert identity_data["client_session_id"] == stable_session_id

    @pytest.mark.asyncio
    async def test_onboard_resume_false_creates_new_identity(self, patch_onboard_deps, mock_db, mock_redis, mock_raw_redis):
        """onboard(resume=false) creates new identity (fresh — no auto lineage claim)."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        predecessor_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": predecessor_uuid,
            "display_agent_id": "Claude_20260207",
        }
        mock_db.get_identity.side_effect = [
            SimpleNamespace(identity_id="i1", metadata={}),  # predecessor lookup
            None,  # ensure_agent_persisted check
            SimpleNamespace(identity_id="new-ident", metadata={}),  # after upsert
        ]
        mock_db.get_agent_label.return_value = "PredecessorAgent"

        result = await handle_onboard_v2({
            "client_session_id": "onboard-resume-false",
            "resume": False,
        })
        data = parse_result(result)

        assert data["success"] is True
        assert data["is_new"] is True
        assert data["uuid"] != predecessor_uuid



    @pytest.mark.asyncio
    async def test_onboard_force_new(self, patch_onboard_deps, mock_db, mock_redis):
        """onboard(force_new=true) creates fresh identity."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="new-ident", metadata={})

        result = await handle_onboard_v2({
            "client_session_id": "onboard-force-new",
            "force_new": True,
        })
        data = parse_result(result)

        assert data["success"] is True
        assert data["is_new"] is True  # force_new_applied moved behind verbose=true

    @pytest.mark.asyncio
    async def test_onboard_emits_identity_resolution_observed(
        self, patch_onboard_deps, mock_db, mock_redis,
    ):
        """A successful onboard must emit one identity_resolution_observed audit event
        carrying the resolved agent_uuid and the captured resolution_source."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="new-ident", metadata={})

        with patch(
            "src.audit_log.audit_logger.log_identity_resolution_observed",
        ) as audit_call:
            result = await handle_onboard_v2({
                "client_session_id": "onboard-emit-ires",
                "force_new": True,
            })
        data = parse_result(result)

        assert data["success"] is True
        audit_call.assert_called_once()
        kwargs = audit_call.call_args.kwargs
        assert kwargs["agent_uuid"] == data["uuid"]
        assert "resolution_source" in kwargs
        assert "pin_match_scope" in kwargs
        assert "pin_entry_present" in kwargs
        assert "token_iat" in kwargs
        assert "token_exp" in kwargs
        # No continuity_token presented → token fields stay None.
        assert kwargs["token_iat"] is None
        assert kwargs["token_exp"] is None
        assert kwargs["token_age_seconds"] is None

    @pytest.mark.asyncio
    async def test_onboard_force_new_with_model_type(self, patch_onboard_deps, mock_db, mock_redis):
        """onboard(force_new=true, model_type='claude') uses model-suffixed key."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="new-ident", metadata={})

        result = await handle_onboard_v2({
            "client_session_id": "onboard-force-model",
            "force_new": True,
            "model_type": "claude-opus-4",
        })
        data = parse_result(result)

        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_onboard_force_new_persists_parent_and_spawn_reason(
        self, patch_onboard_deps, mock_db, mock_redis,
    ):
        """onboard(force_new=true, parent_agent_id=..., spawn_reason=...) must land lineage in PostgreSQL.

        The force_new branch goes through resolve_session_identity, which — before
        this fix — owned its own upsert path and silently dropped parent_agent_id
        and spawn_reason. Under the identity ontology v2 (docs/ontology/identity.md)
        lineage declaration at onboard is the **descriptive** floor: a fresh
        process-instance that declares a predecessor must have that link persisted,
        or the ontology's only earned cross-process signal (declared lineage) is
        theater. Regression against dogfood finding 2026-04-21.
        """
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="new-ident", metadata={})

        parent_uuid = "da300b4a-5320-480d-bac3-d029cd062842"
        spawn_reason = "new_session"

        result = await handle_onboard_v2({
            "client_session_id": "onboard-force-lineage",
            "force_new": True,
            "parent_agent_id": parent_uuid,
            "spawn_reason": spawn_reason,
        })
        data = parse_result(result)

        assert data["success"] is True
        assert data["is_new"] is True

        agent_calls = mock_db.upsert_agent.await_args_list
        assert agent_calls, "upsert_agent must be called for a fresh force_new identity"
        agent_kwargs = agent_calls[0].kwargs
        assert agent_kwargs.get("parent_agent_id") == parent_uuid, (
            f"parent_agent_id must reach core.agents on force_new; got {agent_kwargs!r}"
        )
        assert agent_kwargs.get("spawn_reason") == spawn_reason, (
            f"spawn_reason must reach core.agents on force_new; got {agent_kwargs!r}"
        )

        identity_calls = mock_db.upsert_identity.await_args_list
        assert identity_calls, "upsert_identity must be called for a fresh force_new identity"
        identity_kwargs = identity_calls[0].kwargs
        assert identity_kwargs.get("parent_agent_id") == parent_uuid, (
            f"parent_agent_id must reach core.identities on force_new; got {identity_kwargs!r}"
        )

    @pytest.mark.asyncio
    async def test_onboard_with_model_type_gemini(self, patch_onboard_deps, mock_db, mock_redis):
        """Model normalization for gemini in onboard flow."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.side_effect = [
            None, None,
            SimpleNamespace(identity_id="new-ident", metadata={})
        ]

        result = await handle_onboard_v2({
            "client_session_id": "onboard-gemini",
            "model_type": "gemini-pro",
        })
        data = parse_result(result)

        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_onboard_with_model_type_gpt(self, patch_onboard_deps, mock_db, mock_redis):
        """Model normalization for gpt in onboard flow."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.side_effect = [
            None, None,
            SimpleNamespace(identity_id="new-ident", metadata={})
        ]

        result = await handle_onboard_v2({
            "client_session_id": "onboard-gpt",
            "model_type": "chatgpt-4o",
        })
        data = parse_result(result)

        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_onboard_with_model_type_llama(self, patch_onboard_deps, mock_db, mock_redis):
        """Model normalization for llama in onboard flow."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.side_effect = [
            None, None,
            SimpleNamespace(identity_id="new-ident", metadata={})
        ]

        result = await handle_onboard_v2({
            "client_session_id": "onboard-llama",
            "model_type": "llama-3.1-70b",
        })
        data = parse_result(result)

        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_onboard_sets_label(self, patch_onboard_deps, mock_db, mock_redis, mock_raw_redis):
        """onboard(name='X') sets the display label."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.side_effect = [
            None, None,
            SimpleNamespace(identity_id="new-ident", metadata={}),
            SimpleNamespace(identity_id="new-ident", metadata={}),
        ]
        mock_db.update_agent_fields.return_value = True

        with patch("src.mcp_handlers.shared.get_mcp_server", side_effect=Exception("no server")):
            result = await handle_onboard_v2({
                "client_session_id": "onboard-label",
                "name": "MyNewAgent",
            })
        data = parse_result(result)

        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_onboard_with_trajectory_signature(self, patch_onboard_deps, mock_db, mock_redis, mock_raw_redis):
        """onboard() with trajectory_signature stores genesis."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": test_uuid,
            "display_agent_id": "Claude_20260207",
        }
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.get_agent_label.return_value = "TrajectoryAgent"

        mock_sig = MagicMock()
        mock_sig.identity_confidence = 0.8
        mock_sig.observation_count = 10

        with patch("src.trajectory_identity.TrajectorySignature") as MockTrajSig, \
             patch("src.trajectory_identity.store_genesis_signature", new_callable=AsyncMock, return_value=True):
            MockTrajSig.from_dict.return_value = mock_sig

            result = await handle_onboard_v2({
                "client_session_id": "onboard-trajectory",
                "resume": True,
                "trajectory_signature": {
                    "preferences": {}, "beliefs": {},
                    "stability_score": 0.9, "identity_confidence": 0.8,
                    "observation_count": 10,
                },
            })
        data = parse_result(result)

        assert data["success"] is True
        assert "trajectory" in data
        assert data["trajectory"]["genesis_stored"] is True
        assert "trust_tier" in data["trajectory"]

    @pytest.mark.asyncio
    async def test_onboard_trajectory_exception_non_blocking(self, patch_onboard_deps, mock_db, mock_redis, mock_raw_redis):
        """Trajectory store failure does not block onboard."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": test_uuid,
            "display_agent_id": "Claude_20260207",
        }
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.get_agent_label.return_value = None

        with patch("src.trajectory_identity.TrajectorySignature", side_effect=Exception("Import fail")):
            result = await handle_onboard_v2({
                "client_session_id": "onboard-traj-fail",
                "resume": True,
                "trajectory_signature": {"some": "data"},
            })
        data = parse_result(result)

        assert data["success"] is True
        assert "trajectory" not in data  # Not included on failure

    @pytest.mark.asyncio
    async def test_onboard_kwargs_string_unwrapping(self, patch_onboard_deps, mock_db, mock_redis):
        """onboard() unwraps kwargs string into arguments (lines 1483-1492)."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.side_effect = [
            None, None,
            SimpleNamespace(identity_id="new-ident", metadata={})
        ]

        result = await handle_onboard_v2({
            "client_session_id": "kwargs-test",
            "kwargs": json.dumps({"model_type": "claude-opus-4"}),
        })
        data = parse_result(result)

        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_onboard_kwargs_invalid_json_handled(self, patch_onboard_deps, mock_db, mock_redis):
        """onboard() handles invalid kwargs JSON gracefully (line 1491)."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.side_effect = [
            None, None,
            SimpleNamespace(identity_id="new-ident", metadata={})
        ]

        result = await handle_onboard_v2({
            "client_session_id": "kwargs-invalid",
            "kwargs": "not valid json{{{",
        })
        data = parse_result(result)

        assert data["success"] is True  # Should not crash

    @pytest.mark.asyncio
    async def test_onboard_tool_mode_info(self, patch_onboard_deps, mock_db, mock_redis, mock_raw_redis):
        """onboard() includes tool_mode info when available."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": test_uuid,
            "display_agent_id": "Claude_20260207",
        }
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.get_agent_label.return_value = None

        with patch("src.tool_modes.TOOL_MODE", "lite"), \
             patch("src.tool_modes.get_tools_for_mode", return_value=["t1", "t2", "t3"]), \
             patch("src.tool_schemas.get_tool_definitions", return_value={"t1": {}, "t2": {}, "t3": {}, "t4": {}, "t5": {}}):
            result = await handle_onboard_v2({"client_session_id": "tool-mode-test", "resume": True, "verbose": True})
        data = parse_result(result)

        assert data["success"] is True
        assert "tool_mode" in data
        assert data["tool_mode"]["current_mode"] == "lite"
        assert data["tool_mode"]["visible_tools"] == 3
        assert data["tool_mode"]["total_tools"] == 5

    @pytest.mark.asyncio
    async def test_onboard_tool_mode_exception_handled(self, patch_onboard_deps, mock_db, mock_redis, mock_raw_redis):
        """tool_mode import failure is swallowed."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": test_uuid,
            "display_agent_id": "Claude_20260207",
        }
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.get_agent_label.return_value = None

        with patch("src.tool_modes.TOOL_MODE", side_effect=Exception("No module")):
            result = await handle_onboard_v2({"client_session_id": "tool-mode-fail", "resume": True})
        data = parse_result(result)

        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_onboard_client_tips_chatgpt(self, patch_onboard_deps, mock_db, mock_redis, mock_raw_redis):
        """Client tips for chatgpt hint are included."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": test_uuid,
            "display_agent_id": "Claude_20260207",
        }
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.get_agent_label.return_value = None

        with patch("src.mcp_handlers.context.get_context_client_hint", return_value="chatgpt"):
            result = await handle_onboard_v2({"client_session_id": "chatgpt-tips", "resume": True, "verbose": True})
        data = parse_result(result)

        assert data["success"] is True
        tip = data.get("session_continuity", {}).get("tip", "")
        assert "ChatGPT" in tip or "client_session_id" in tip

    @pytest.mark.asyncio
    async def test_onboard_persist_failure_returns_error(self, patch_onboard_deps, mock_db, mock_redis):
        """When persist fails for fresh identity, returns error (line 1613-1615)."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None

        # Mock ensure_agent_persisted to raise an exception directly
        # This triggers the except block at line 1613 which returns error_response
        with patch("src.mcp_handlers.identity.handlers.ensure_agent_persisted", side_effect=Exception("Fatal persist error")):
            result = await handle_onboard_v2({"client_session_id": "persist-fail"})
        data = parse_result(result)

        assert data.get("success") is False
        assert "persist" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_onboard_none_arguments_handled(self, patch_onboard_deps, mock_db, mock_redis):
        """onboard(None) defaults to empty dict."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.side_effect = [
            None, None,
            SimpleNamespace(identity_id="new-ident", metadata={})
        ]

        result = await handle_onboard_v2(None)
        data = parse_result(result)

        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_onboard_structured_id_fallback(self, patch_onboard_deps, mock_db, mock_redis, mock_raw_redis):
        """When structured_id lookup from metadata returns nothing, falls back to agent_UUID prefix."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.side_effect = [
            None, None,
            SimpleNamespace(identity_id="new-ident", metadata={})
        ]

        result = await handle_onboard_v2({"client_session_id": "fallback-id-test"})
        data = parse_result(result)

        assert data["success"] is True
        # agent_id should be generated, not an empty UUID
        assert data.get("agent_id") is not None

    @pytest.mark.asyncio
    async def test_onboard_auto_unarchives_agent(self, patch_onboard_deps, mock_db, mock_redis, mock_raw_redis):
        """onboard() auto-unarchives an archived agent and sets auto_resumed flag."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": test_uuid,
            "display_agent_id": "Claude_20260207",
        }
        # Return archived identity
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="i1", metadata={}, status="archived"
        )
        mock_db.get_agent_label.return_value = "ArchivedAgent"
        mock_db.update_agent_fields.return_value = True

        result = await handle_onboard_v2({"client_session_id": "onboard-archived", "resume": True})
        data = parse_result(result)

        assert data["success"] is True
        assert data["is_new"] is False
        assert data.get("auto_resumed") is True
        assert data.get("previous_status") == "archived"
        assert "reactivated" in data.get("welcome", "").lower()
        # Verify DB update was called
        mock_db.update_agent_fields.assert_called_with(test_uuid, status="active")

    @pytest.mark.asyncio
    async def test_archived_token_without_explicit_resume_returns_clean_error(
        self, patch_onboard_deps, mock_db, mock_redis
    ):
        """Regression (2026-04-19): onboard with a continuity_token for an
        archived agent — no explicit `resume` arg — must return a clean
        resume_failed error instead of UnboundLocalError on session_key.

        The pre-fix chain was: OnboardParams default resume=False → handler's
        `coerce_bool(default=True)` was overridden by the Pydantic-materialized
        False → the archived-token path fell into the non-resume branch of
        resolve_session_identity (which also returns resume_failed), but the
        handler only checked resume_failed on the resume=True branch. The
        non-resume branch dropped through to code that referenced an
        uninitialized session_key, raising UnboundLocalError and returning
        an error_response with a misleading agent_signature echo.
        """
        from src.mcp_handlers.identity import handlers as identity_handlers

        archived_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        async def _fake_resolve(session_key, persist, resume, token_agent_uuid=None, **_):
            # Simulate PATH 2.8 rejecting an archived agent regardless of
            # the resume flag (matches real resolution.py:578-634 behavior).
            return {
                "resume_failed": True,
                "error": "resume_failed",
                "token_agent_uuid": token_agent_uuid,
                "message": (
                    f"Continuity token references agent {token_agent_uuid[:8]}... "
                    f"which is not active."
                ),
            }

        with patch.object(identity_handlers, "resolve_session_identity", side_effect=_fake_resolve), \
             patch.object(identity_handlers, "extract_token_agent_uuid", return_value=archived_uuid):
            # Deliberately omit `resume` — exercises the Pydantic-default path.
            result = await identity_handlers.handle_onboard_v2({
                "client_session_id": "archived-token-no-resume",
                "continuity_token": "v1.fake.sig",
            })

        data = parse_result(result)
        assert data.get("success") is False, "must NOT succeed on archived-token resume"
        err = (data.get("error") or "").lower()
        assert "resume" in err or "not active" in err
        recovery = data.get("recovery") or {}
        assert recovery.get("reason") == "resume_failed"
        assert recovery.get("token_agent_uuid") == archived_uuid
        # Crucially: we did not raise. Before the fix this path produced
        # UnboundLocalError on session_key.


# ============================================================================
# handle_verify_trajectory_identity (lines 1884-1921)
# ============================================================================

class TestHandleVerifyTrajectoryIdentity:

    @pytest.mark.asyncio
    async def test_no_agent_uuid_returns_error(self):
        """verify_trajectory_identity with no identity returns error."""
        from src.mcp_handlers.identity.handlers import handle_verify_trajectory_identity

        with patch("src.mcp_handlers.context.get_context_agent_id", return_value=None):
            result = await handle_verify_trajectory_identity({})
        data = parse_result(result)

        assert data["success"] is False
        assert "identity" in data["error"].lower() or "resolved" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_no_trajectory_signature_returns_error(self):
        """verify_trajectory_identity without trajectory_signature returns error."""
        from src.mcp_handlers.identity.handlers import handle_verify_trajectory_identity

        with patch("src.mcp_handlers.context.get_context_agent_id", return_value="uuid-123"):
            result = await handle_verify_trajectory_identity({})
        data = parse_result(result)

        assert data["success"] is False
        assert "trajectory_signature" in data["error"]

    @pytest.mark.asyncio
    async def test_invalid_trajectory_signature_type_returns_error(self):
        """verify_trajectory_identity with non-dict trajectory_signature returns error."""
        from src.mcp_handlers.identity.handlers import handle_verify_trajectory_identity

        with patch("src.mcp_handlers.context.get_context_agent_id", return_value="uuid-123"):
            result = await handle_verify_trajectory_identity({"trajectory_signature": "not a dict"})
        data = parse_result(result)

        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_successful_verification(self):
        """verify_trajectory_identity succeeds with valid inputs."""
        from src.mcp_handlers.identity.handlers import handle_verify_trajectory_identity

        mock_sig = MagicMock()
        mock_verification_result = {
            "verified": True,
            "tiers": {"coherence": {"similarity": 0.9}, "lineage": {"similarity": 0.85}},
        }

        async def verify_ok(*args, **kwargs):
            return mock_verification_result

        with patch("src.mcp_handlers.context.get_context_agent_id", return_value="uuid-verify"), \
             patch("src.trajectory_identity.TrajectorySignature") as MockTrajSig, \
             patch("src.trajectory_identity.verify_trajectory_identity", new=verify_ok):
            MockTrajSig.from_dict.return_value = mock_sig

            result = await handle_verify_trajectory_identity({
                "trajectory_signature": {"preferences": {}, "stability_score": 0.9},
                "coherence_threshold": 0.7,
                "lineage_threshold": 0.6,
            })
        data = parse_result(result)

        assert data["success"] is True
        assert data["verified"] is True

    @pytest.mark.asyncio
    async def test_verification_error_result(self):
        """verify_trajectory_identity with error in result returns error."""
        from src.mcp_handlers.identity.handlers import handle_verify_trajectory_identity

        mock_sig = MagicMock()
        mock_verification_result = {"error": "No genesis signature found"}

        async def verify_error(*args, **kwargs):
            return mock_verification_result

        with patch("src.mcp_handlers.context.get_context_agent_id", return_value="uuid-verify"), \
             patch("src.trajectory_identity.TrajectorySignature") as MockTrajSig, \
             patch("src.trajectory_identity.verify_trajectory_identity", new=verify_error):
            MockTrajSig.from_dict.return_value = mock_sig

            result = await handle_verify_trajectory_identity({
                "trajectory_signature": {"preferences": {}},
            })
        data = parse_result(result)

        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_verification_exception_returns_error(self):
        """verify_trajectory_identity exception returns error (lines 1919-1921)."""
        from src.mcp_handlers.identity.handlers import handle_verify_trajectory_identity

        mock_sig = MagicMock()

        async def verify_boom(*args, **kwargs):
            raise Exception("Verification module error")

        with patch("src.mcp_handlers.context.get_context_agent_id", return_value="uuid-verify"), \
             patch("src.trajectory_identity.TrajectorySignature") as MockTrajSig, \
             patch("src.trajectory_identity.verify_trajectory_identity", new=verify_boom):
            MockTrajSig.from_dict.return_value = mock_sig

            result = await handle_verify_trajectory_identity({
                "trajectory_signature": {"preferences": {}},
            })
        data = parse_result(result)

        assert data["success"] is False
        error_msg = data.get("error", "").lower()
        assert "failed" in error_msg or "verification" in error_msg


# ============================================================================
# handle_get_trajectory_status (lines 1938-1966)
# ============================================================================

class TestHandleGetTrajectoryStatus:

    @pytest.mark.asyncio
    async def test_no_agent_uuid_returns_error(self):
        """get_trajectory_status with no identity returns error."""
        from src.mcp_handlers.identity.handlers import handle_get_trajectory_status

        with patch("src.mcp_handlers.context.get_context_agent_id", return_value=None):
            result = await handle_get_trajectory_status({})
        data = parse_result(result)

        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_successful_status(self):
        """get_trajectory_status returns status info."""
        from src.mcp_handlers.identity.handlers import handle_get_trajectory_status

        mock_status_result = {
            "has_genesis": True,
            "has_current": True,
            "lineage_similarity": 0.85,
        }

        mock_db = AsyncMock()
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="i1", metadata={"total_updates": 20}
        )

        mock_trust_tier = {"tier": 2, "name": "stable"}

        with patch("src.mcp_handlers.context.get_context_agent_id", return_value="uuid-status"), \
             patch("src.trajectory_identity.get_trajectory_status", new_callable=AsyncMock, return_value=mock_status_result), \
             patch("src.trajectory_identity.compute_trust_tier", return_value=mock_trust_tier), \
             patch("src.db.get_db", return_value=mock_db):
            result = await handle_get_trajectory_status({})
        data = parse_result(result)

        assert data["success"] is True
        assert data["has_genesis"] is True
        assert "trust_tier" in data

    @pytest.mark.asyncio
    async def test_status_error_result(self):
        """get_trajectory_status with error in result returns error."""
        from src.mcp_handlers.identity.handlers import handle_get_trajectory_status

        mock_status_result = {"error": "Agent has no trajectory data"}

        with patch("src.mcp_handlers.context.get_context_agent_id", return_value="uuid-status"), \
             patch("src.trajectory_identity.get_trajectory_status", new_callable=AsyncMock, return_value=mock_status_result):
            result = await handle_get_trajectory_status({})
        data = parse_result(result)

        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_status_exception_returns_error(self):
        """get_trajectory_status exception returns error (lines 1964-1966)."""
        from src.mcp_handlers.identity.handlers import handle_get_trajectory_status

        with patch("src.mcp_handlers.context.get_context_agent_id", return_value="uuid-status"), \
             patch("src.trajectory_identity.get_trajectory_status", side_effect=Exception("Module error")):
            result = await handle_get_trajectory_status({})
        data = parse_result(result)

        assert data["success"] is False
        assert "failed" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_trust_tier_exception_non_blocking(self):
        """trust_tier computation failure does not block status response (lines 1959-1960)."""
        from src.mcp_handlers.identity.handlers import handle_get_trajectory_status

        mock_status_result = {
            "has_genesis": True,
            "has_current": False,
        }

        with patch("src.mcp_handlers.context.get_context_agent_id", return_value="uuid-status"), \
             patch("src.trajectory_identity.get_trajectory_status", new_callable=AsyncMock, return_value=mock_status_result), \
             patch("src.trajectory_identity.compute_trust_tier", side_effect=Exception("No trust data")), \
             patch("src.db.get_db", side_effect=Exception("DB down")):
            result = await handle_get_trajectory_status({})
        data = parse_result(result)

        assert data["success"] is True
        assert data["has_genesis"] is True
        # trust_tier should not be present since computation failed
        assert "trust_tier" not in data


# ============================================================================
# Additional coverage: set_agent_label structured_id migration (lines 611-621)
# ============================================================================

class TestIdentityAdapterStructuredIdRegeneration:

    @pytest.fixture
    def patch_identity_regen_deps(self, mock_db, mock_redis, mock_raw_redis):
        """Patch deps for structured_id regeneration tests."""
        async def _get_raw():
            return mock_raw_redis

        mock_server = MagicMock()

        with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=mock_redis), \
             patch("src.mcp_handlers.identity.handlers.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.resolution.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.cache.redis_client.get_redis", new=_get_raw), \
             patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value=None), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
             patch("src.mcp_handlers.context.update_context_agent_id"), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server):
            yield mock_server

    @pytest.mark.asyncio
    async def test_structured_id_regenerated_when_model_doesnt_match(self, patch_identity_regen_deps, mock_db, mock_redis):
        """structured_id is regenerated when model_type doesn't match existing ID (lines 1327-1342)."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None

        # Get the mock server from the fixture
        mock_server = patch_identity_regen_deps

        # We need the handler to create a new identity first, then check metadata
        # The trick is the metadata needs to exist AFTER resolve_session_identity runs
        original_resolve = None

        async def resolve_side_effect(*args, **kwargs):
            # Simulate creating a new identity and populating metadata
            agent_uuid = str(uuid.uuid4())
            agent_id = "Claude_Opus_4_20260207"
            meta = SimpleNamespace(
                label=None,
                structured_id="generic_id_1"  # Doesn't contain "claude"
            )
            mock_server.agent_metadata[agent_uuid] = meta
            return {
                "agent_id": agent_id,
                "agent_uuid": agent_uuid,
                "label": None,
                "created": True,
                "persisted": False,
                "source": "memory_only",
            }

        with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", side_effect=resolve_side_effect), \
             patch("src.mcp_handlers.support.naming_helpers.detect_interface_context", return_value={"type": "test"}), \
             patch("src.mcp_handlers.support.naming_helpers.generate_structured_id", return_value="claude_opus_1"), \
             patch("src.mcp_handlers.context.get_context_client_hint", return_value="cursor"):

            result = await handle_identity_adapter({
                "client_session_id": "regen-struct-id",
                "model_type": "claude-opus-4",
                "force_new": True,  # Skip base key lookup
            })
        data = parse_result(result)

        assert data["success"] is True


# ============================================================================
# Additional coverage: onboard force_new with model normalization branches
# ============================================================================

class TestOnboardForceNewModelNormalization:

    @pytest.fixture
    def patch_onboard_force_deps(self, mock_db, mock_redis, mock_raw_redis):
        """Patch deps for onboard force_new model tests."""
        async def _get_raw():
            return mock_raw_redis

        mock_server = MagicMock()
        mock_server.agent_metadata = {}

        with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=mock_redis), \
             patch("src.mcp_handlers.identity.handlers.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.resolution.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.cache.redis_client.get_redis", new=_get_raw), \
             patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value="force-ctx-key"), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_client_hint", return_value="test"), \
             patch("src.mcp_handlers.context.update_context_agent_id"), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server), \
             patch("src.mcp_handlers.identity.shared._register_uuid_prefix"):
            yield mock_server

    @pytest.mark.asyncio
    async def test_force_new_gemini_normalization(self, patch_onboard_force_deps, mock_db, mock_redis):
        """onboard(force_new=true, model_type='gemini-pro') normalizes to 'gemini' (line 1574-1575)."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="new-ident", metadata={})

        result = await handle_onboard_v2({
            "client_session_id": "force-gemini",
            "force_new": True,
            "model_type": "gemini-pro-1.5",
        })
        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_force_new_gpt_normalization(self, patch_onboard_force_deps, mock_db, mock_redis):
        """onboard(force_new=true, model_type='gpt-4') normalizes to 'gpt' (line 1576-1577)."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="new-ident", metadata={})

        result = await handle_onboard_v2({
            "client_session_id": "force-gpt",
            "force_new": True,
            "model_type": "gpt-4-turbo",
        })
        data = parse_result(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_force_new_llama_normalization(self, patch_onboard_force_deps, mock_db, mock_redis):
        """onboard(force_new=true, model_type='llama-3') normalizes to 'llama' (line 1578-1579)."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="new-ident", metadata={})

        result = await handle_onboard_v2({
            "client_session_id": "force-llama",
            "force_new": True,
            "model_type": "llama-3.1-70b",
        })
        data = parse_result(result)
        assert data["success"] is True


# ============================================================================
# Additional coverage: onboard resolve_session_identity failure in force_new (lines 1632-1634)
# ============================================================================

class TestOnboardResolveSessionIdentityFailure:

    @pytest.fixture
    def patch_onboard_resolve_fail_deps(self, mock_db, mock_redis, mock_raw_redis):
        async def _get_raw():
            return mock_raw_redis

        mock_server = MagicMock()
        mock_server.agent_metadata = {}

        with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=mock_redis), \
             patch("src.mcp_handlers.identity.handlers.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.resolution.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.cache.redis_client.get_redis", new=_get_raw), \
             patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value="ctx-key"), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_client_hint", return_value="test"), \
             patch("src.mcp_handlers.context.update_context_agent_id"), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server), \
             patch("src.mcp_handlers.identity.shared._register_uuid_prefix"):
            yield mock_server

    @pytest.mark.asyncio
    async def test_force_new_resolve_exception_returns_error(self, patch_onboard_resolve_fail_deps, mock_db, mock_redis):
        """When force_new + resolve_session_identity raises, returns error (lines 1632-1634)."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", side_effect=Exception("Resolve failed")):
            result = await handle_onboard_v2({
                "client_session_id": "force-resolve-fail",
                "force_new": True,
            })
        data = parse_result(result)

        assert data.get("success") is False
        assert "failed" in data.get("error", "").lower()


# ============================================================================
# Additional coverage: onboard already-persisted fresh identity (line 1603)
# ============================================================================

class TestOnboardAlreadyPersistedFreshIdentity:

    @pytest.fixture
    def patch_onboard_persisted_deps(self, mock_db, mock_redis, mock_raw_redis):
        async def _get_raw():
            return mock_raw_redis

        mock_server = MagicMock()
        mock_server.agent_metadata = {}

        with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=mock_redis), \
             patch("src.mcp_handlers.identity.handlers.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.resolution.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.cache.redis_client.get_redis", new=_get_raw), \
             patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value="ctx-key"), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_client_hint", return_value="test"), \
             patch("src.mcp_handlers.context.update_context_agent_id"), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server), \
             patch("src.mcp_handlers.identity.shared._register_uuid_prefix"):
            yield mock_server

    @pytest.mark.asyncio
    async def test_fresh_identity_already_persisted(self, patch_onboard_persisted_deps, mock_db, mock_redis):
        """When fresh identity is already persisted, ensure_agent_persisted returns False (line 1603)."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None

        # ensure_agent_persisted returns False (already persisted)
        with patch("src.mcp_handlers.identity.handlers.ensure_agent_persisted", new_callable=AsyncMock, return_value=False):
            result = await handle_onboard_v2({"client_session_id": "already-persisted"})
        data = parse_result(result)

        assert data["success"] is True
        assert data["is_new"] is True


# ============================================================================
# Additional coverage: update_context_agent_id exception paths
# ============================================================================

class TestContextUpdateExceptions:

    @pytest.fixture
    def patch_ctx_deps(self, mock_db, mock_redis, mock_raw_redis):
        async def _get_raw():
            return mock_raw_redis

        mock_server = MagicMock()
        mock_server.agent_metadata = {}

        with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=mock_redis), \
             patch("src.mcp_handlers.identity.handlers.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.resolution.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.cache.redis_client.get_redis", new=_get_raw), \
             patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value=None), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server):
            yield mock_server

    @pytest.mark.asyncio
    async def test_identity_adapter_context_update_exception(self, patch_ctx_deps, mock_db, mock_redis):
        """update_context_agent_id failure is swallowed in identity adapter (lines 1314-1315)."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None
        mock_db.find_agent_by_label.return_value = None

        with patch("src.mcp_handlers.context.update_context_agent_id", side_effect=Exception("Context error")):
            result = await handle_identity_adapter({
                "client_session_id": "ctx-fail-test",
                "force_new": True,
            })
        data = parse_result(result)

        assert data["success"] is True



# ============================================================================
# Additional coverage: onboard structured_id fallback (lines 1752-1763)
# ============================================================================

class TestOnboardStructuredIdFallback:

    @pytest.fixture
    def patch_sid_deps(self, mock_db, mock_redis, mock_raw_redis):
        async def _get_raw():
            return mock_raw_redis

        def _discard_task(coro, **kwargs):
            try:
                coro.close()
            except Exception:
                pass
            task = MagicMock()
            task.cancel = MagicMock()
            return task

        mock_server = MagicMock()
        mock_server.agent_metadata = {}

        with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=mock_redis), \
             patch("src.mcp_handlers.identity.handlers.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.resolution.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.cache.redis_client.get_redis", new=_get_raw), \
             patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value="ctx"), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_client_hint", return_value="test"), \
             patch("src.mcp_handlers.context.update_context_agent_id"), \
             patch("asyncio.create_task", side_effect=_discard_task), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server), \
             patch("src.mcp_handlers.identity.shared._register_uuid_prefix"):
            yield mock_server

    @pytest.mark.asyncio
    async def test_structured_id_from_metadata_lookup(self, patch_sid_deps, mock_db, mock_redis, mock_raw_redis):
        """When agent_id == agent_uuid, falls back to metadata for structured_id (lines 1752-1759)."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_server = patch_sid_deps
        test_uuid = str(uuid.uuid4())

        # Force resume path where agent_id might equal agent_uuid
        mock_redis.get.return_value = {
            "agent_id": test_uuid,
            "display_agent_id": test_uuid,  # Same as UUID -> triggers fallback
        }
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.get_agent_label.return_value = None

        # Add metadata with structured_id
        meta = SimpleNamespace(structured_id="custom_agent_1")
        mock_server.agent_metadata[test_uuid] = meta

        # resume=True to reuse old UUID (escape hatch) — tests structured_id fallback
        result = await handle_onboard_v2({"client_session_id": "sid-fallback", "resume": True})
        data = parse_result(result)

        assert data["success"] is True
        # structured_id should be from metadata
        assert data.get("agent_id") == "custom_agent_1"

    @pytest.mark.asyncio
    async def test_structured_id_uuid_prefix_fallback(self, patch_sid_deps, mock_db, mock_redis, mock_raw_redis):
        """When no structured_id anywhere, falls back to agent_{uuid[:8]} (line 1763)."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        mock_server = patch_sid_deps
        test_uuid = str(uuid.uuid4())

        mock_redis.get.return_value = {
            "agent_id": test_uuid,
            "display_agent_id": test_uuid,  # Same as UUID
        }
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.get_agent_label.return_value = None

        # resume=True to reuse old UUID — tests uuid prefix fallback
        result = await handle_onboard_v2({"client_session_id": "uuid-prefix-fallback", "resume": True})
        data = parse_result(result)

        assert data["success"] is True
        assert data.get("agent_id", "").startswith("agent_")


# ============================================================================
# Additional coverage: onboard pin/uuid_prefix exception paths (lines 1686-1698)
# ============================================================================

class TestOnboardPinAndPrefixExceptions:

    @pytest.fixture
    def patch_pin_deps(self, mock_db, mock_redis, mock_raw_redis):
        async def _get_raw():
            return mock_raw_redis

        mock_server = MagicMock()
        mock_server.agent_metadata = {}

        with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=mock_redis), \
             patch("src.mcp_handlers.identity.handlers.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.resolution.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.cache.redis_client.get_redis", new=_get_raw), \
             patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value="ctx"), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_client_hint", return_value="test"), \
             patch("src.mcp_handlers.context.update_context_agent_id"), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server):
            yield mock_server

    @pytest.mark.asyncio
    async def test_uuid_prefix_import_error_handled(self, patch_pin_deps, mock_db, mock_redis, mock_raw_redis):
        """ImportError for _register_uuid_prefix is swallowed (lines 1686-1687)."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": test_uuid,
            "display_agent_id": "Claude_20260207",
        }
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.get_agent_label.return_value = None

        with patch("src.mcp_handlers.identity.shared._register_uuid_prefix", side_effect=ImportError("not found")):
            result = await handle_onboard_v2({"client_session_id": "prefix-import-fail"})
        data = parse_result(result)

        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_set_onboard_pin_exception_handled(self, patch_pin_deps, mock_db, mock_redis, mock_raw_redis):
        """set_onboard_pin exception is swallowed (lines 1697-1698)."""
        from src.mcp_handlers.identity.handlers import handle_onboard_v2

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": test_uuid,
            "display_agent_id": "Claude_20260207",
        }
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.get_agent_label.return_value = None

        with patch("src.mcp_handlers.identity.shared._register_uuid_prefix"), \
             patch("src.mcp_handlers.identity.handlers.set_onboard_pin", side_effect=Exception("Pin error")):
            result = await handle_onboard_v2({"client_session_id": "pin-exception"})
        data = parse_result(result)

        assert data["success"] is True


# ============================================================================
# identity(agent_uuid=...) direct UUID lookup (PATH 0)
# ============================================================================

class TestIdentityAgentUuidDirectLookup:

    @pytest.fixture
    def patch_uuid_deps(self, mock_db, mock_redis, mock_raw_redis):
        """Patch deps for agent_uuid direct lookup tests."""
        async def _get_raw():
            return mock_raw_redis

        mock_server = MagicMock()
        mock_server.agent_metadata = {}

        with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=mock_redis), \
             patch("src.mcp_handlers.identity.handlers.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.resolution.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.cache.redis_client.get_redis", new=_get_raw), \
             patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value=None), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
             patch("src.mcp_handlers.context.update_context_agent_id"), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server):
            yield mock_server

    @pytest.mark.asyncio
    async def test_agent_uuid_resumes_active_agent(self, patch_uuid_deps, mock_db, mock_redis):
        """identity(agent_uuid=...) should resume an active agent directly."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        test_uuid = str(uuid.uuid4())

        with patch("src.mcp_handlers.identity.handlers._agent_exists_in_postgres", new_callable=AsyncMock, return_value=True), \
             patch("src.mcp_handlers.identity.handlers._get_agent_status", new_callable=AsyncMock, return_value="active"), \
             patch("src.mcp_handlers.identity.handlers._get_agent_id_from_metadata", new_callable=AsyncMock, return_value="Claude_20260415"), \
             patch("src.mcp_handlers.identity.handlers._get_agent_label", new_callable=AsyncMock, return_value="Vigil"), \
             patch("src.mcp_handlers.identity.handlers._cache_session", new_callable=AsyncMock):
            result = await handle_identity_adapter({
                "client_session_id": "uuid-direct-test",
                "agent_uuid": test_uuid,
                "resume": True,
            })
        data = parse_result(result)

        assert data["success"] is True
        assert data["uuid"] == test_uuid
        assert data.get("resumed") is True
        assert data.get("resumed_by_uuid") is True

    @pytest.mark.asyncio
    async def test_agent_uuid_not_found_returns_error(self, patch_uuid_deps, mock_db, mock_redis):
        """identity(agent_uuid=...) should fail if UUID not in DB."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        test_uuid = str(uuid.uuid4())

        with patch("src.mcp_handlers.identity.handlers._agent_exists_in_postgres", new_callable=AsyncMock, return_value=False):
            result = await handle_identity_adapter({
                "client_session_id": "uuid-missing-test",
                "agent_uuid": test_uuid,
                "resume": True,
            })
        data = parse_result(result)

        assert data["success"] is False
        assert "not found" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_agent_uuid_not_active_returns_error(self, patch_uuid_deps, mock_db, mock_redis):
        """identity(agent_uuid=...) should fail if agent exists but is archived."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        test_uuid = str(uuid.uuid4())

        with patch("src.mcp_handlers.identity.handlers._agent_exists_in_postgres", new_callable=AsyncMock, return_value=True), \
             patch("src.mcp_handlers.identity.handlers._get_agent_status", new_callable=AsyncMock, return_value="archived"):
            result = await handle_identity_adapter({
                "client_session_id": "uuid-archived-test",
                "agent_uuid": test_uuid,
                "resume": True,
            })
        data = parse_result(result)

        assert data["success"] is False
        assert "not active" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_agent_uuid_ignored_when_resume_false(self, patch_uuid_deps, mock_db, mock_redis):
        """agent_uuid should be ignored when resume=false (follows normal flow)."""
        from src.mcp_handlers.identity.handlers import handle_identity_adapter

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = None
        mock_db.get_session.return_value = None

        result = await handle_identity_adapter({
            "client_session_id": "uuid-no-resume",
            "agent_uuid": test_uuid,
            "resume": False,
        })
        data = parse_result(result)

        # Should create a new identity, not resume the provided UUID
        assert data["success"] is True
        assert data["uuid"] != test_uuid
