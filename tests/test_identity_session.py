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

class TestDeriveSessionKey:

    @pytest.fixture(autouse=True)
    def import_fn(self):
        from src.mcp_handlers.identity.handlers import derive_session_key
        self.derive_async = derive_session_key

    @pytest.mark.asyncio
    async def test_priority_1_explicit_client_session_id(self):
        """client_session_id in arguments has highest priority."""
        result = await self.derive_async(None, {"client_session_id": "explicit-123"})
        assert result == "explicit-123"

    @pytest.mark.asyncio
    async def test_priority_2_mcp_session_id_header(self):
        """mcp-session-id header is second priority."""
        with patch("src.mcp_handlers.context.get_mcp_session_id", return_value="mcp-sess-abc"):
            result = await self.derive_async(None, {})
            assert result == "mcp:mcp-sess-abc"

    @pytest.mark.asyncio
    async def test_priority_3_contextvars_session_key(self):
        """contextvars session_key is third priority."""
        with patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value="ctx-key-789"):
            result = await self.derive_async(None, {})
            assert result == "ctx-key-789"

    @pytest.mark.asyncio
    async def test_priority_4_stdio_fallback(self):
        """Falls back to stdio:{pid} when nothing else is available."""
        with patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value=None):
            result = await self.derive_async(None, {})
            assert result.startswith("stdio:")
            assert str(os.getpid()) in result

    @pytest.mark.asyncio
    async def test_explicit_overrides_mcp_header(self):
        """client_session_id takes priority over mcp-session-id."""
        with patch("src.mcp_handlers.context.get_mcp_session_id", return_value="mcp-id"):
            result = await self.derive_async(None, {"client_session_id": "explicit"})
            assert result == "explicit"

    @pytest.mark.asyncio
    async def test_mcp_session_id_overrides_contextvars(self):
        """mcp-session-id takes priority over contextvars."""
        with patch("src.mcp_handlers.context.get_mcp_session_id", return_value="mcp-id"), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value="ctx-key"):
            result = await self.derive_async(None, {})
            assert result == "mcp:mcp-id"

    @pytest.mark.asyncio
    async def test_empty_client_session_id_falls_through(self):
        """Empty string client_session_id falls through to next priority."""
        with patch("src.mcp_handlers.context.get_mcp_session_id", return_value="mcp-id"):
            result = await self.derive_async(None, {"client_session_id": ""})
            assert result == "mcp:mcp-id"

    @pytest.mark.asyncio
    async def test_none_client_session_id_falls_through(self):
        """None client_session_id falls through to next priority."""
        with patch("src.mcp_handlers.context.get_mcp_session_id", return_value="mcp-id"):
            result = await self.derive_async(None, {"client_session_id": None})
            assert result == "mcp:mcp-id"

    @pytest.mark.asyncio
    async def test_mcp_session_id_exception_falls_through(self):
        """Exception in get_mcp_session_id falls through to stdio (single try block)."""
        with patch("src.mcp_handlers.context.get_mcp_session_id", side_effect=Exception("boom")), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value="ctx-fallback"):
            result = await self.derive_async(None, {})
            # derive_session_key uses single try block; exception skips to stdio
            assert result.startswith("stdio:")

    @pytest.mark.asyncio
    async def test_context_session_key_exception_falls_through(self):
        """Exception in get_context_session_key falls through to stdio."""
        with patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", side_effect=Exception("boom")):
            result = await self.derive_async(None, {})
            assert result.startswith("stdio:")


# ============================================================================
# Unified derive_session_key (async, with SessionSignals)
# ============================================================================

class TestUnifiedDeriveSessionKey:
    """Tests for the new async derive_session_key() with SessionSignals."""

    @pytest.mark.asyncio
    async def test_priority_1_explicit_client_session_id(self):
        """arguments['client_session_id'] has highest priority."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import get_session_resolution_source
        from src.mcp_handlers.context import SessionSignals
        signals = SessionSignals(mcp_session_id="mcp-id", x_session_id="x-id")
        result = await derive_session_key(signals, {"client_session_id": "explicit-123"})
        assert result == "explicit-123"
        assert get_session_resolution_source() == "explicit_client_session_id"

    @pytest.mark.asyncio
    async def test_priority_1_continuity_token(self):
        """Signed continuity_token should be preferred when valid."""
        from src.mcp_handlers.identity.handlers import derive_session_key, create_continuity_token
        from src.mcp_handlers.context import SessionSignals

        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            token = create_continuity_token(
                "11111111-2222-3333-4444-555555555555",
                "agent-111111111111:gpt",
                model_type="gpt-5-codex",
                client_hint="chatgpt",
            )
            signals = SessionSignals(user_agent="Codex/CLI")
            result = await derive_session_key(
                signals,
                {"continuity_token": token, "client_session_id": "wrong-session", "model_type": "gpt-5-codex"},
            )
            assert result == "agent-111111111111:gpt"

    @pytest.mark.asyncio
    async def test_priority_1_continuity_token_model_mismatch_ignored(self):
        """Token with mismatched model scope should not be used."""
        from src.mcp_handlers.identity.handlers import derive_session_key, create_continuity_token
        from src.mcp_handlers.context import SessionSignals

        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            token = create_continuity_token(
                "11111111-2222-3333-4444-555555555555",
                "agent-111111111111:claude",
                model_type="claude-opus-4-5",
                client_hint="claude_desktop",
            )
            signals = SessionSignals(user_agent="Codex/CLI")
            result = await derive_session_key(
                signals,
                {"continuity_token": token, "client_session_id": "explicit-123", "model_type": "gpt-5-codex"},
            )
            assert result == "explicit-123:gpt"

    @pytest.mark.asyncio
    async def test_priority_1_explicit_client_session_id_scoped_by_model(self):
        """Explicit client_session_id is model-scoped when model_type is present."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals

        signals = SessionSignals(user_agent="Codex/CLI")
        result = await derive_session_key(
            signals,
            {"client_session_id": "explicit-123", "model_type": "gpt-5-codex"},
        )
        assert result == "explicit-123:gpt"

    @pytest.mark.asyncio
    async def test_explicit_client_session_id_is_stripped(self):
        """Leading/trailing whitespace is not part of the explicit key."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals

        signals = SessionSignals(mcp_session_id="mcp-abc")
        result = await derive_session_key(
            signals,
            {"client_session_id": "  explicit-123  "},
        )
        assert result == "explicit-123"

    @pytest.mark.asyncio
    async def test_whitespace_only_client_session_id_falls_through(self):
        """Whitespace-only client_session_id is not an explicit proof signal."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import get_session_resolution_source, SessionSignals

        args = {"client_session_id": "\n\n\t  "}
        signals = SessionSignals(mcp_session_id="mcp-abc")
        result = await derive_session_key(signals, args)
        assert result == "mcp:mcp-abc"
        assert "client_session_id" not in args
        assert get_session_resolution_source() == "mcp_session_id"

    @pytest.mark.asyncio
    async def test_oversized_client_session_id_is_bounded(self):
        """Overlong explicit IDs are truncated before cache/DB key use."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals

        result = await derive_session_key(
            SessionSignals(),
            {"client_session_id": "a" * 300},
        )
        assert result == "a" * 256

    @pytest.mark.asyncio
    async def test_control_chars_in_client_session_id_are_sanitized(self):
        """Control characters are reduced to inert key text."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals

        result = await derive_session_key(
            SessionSignals(),
            {"client_session_id": "session\x00key\nnext"},
        )
        assert result == "session_key_next"

    @pytest.mark.asyncio
    async def test_path_traversal_client_session_id_is_sanitized(self):
        """Path traversal shapes are not preserved as path-like keys."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals

        result = await derive_session_key(
            SessionSignals(),
            {"client_session_id": "../../etc/passwd"},
        )
        assert "/" not in result
        assert "\\" not in result
        assert ".." not in result
        assert "etc_passwd" in result

    @pytest.mark.asyncio
    async def test_priority_2_mcp_session_id(self):
        """mcp_session_id from signals is second priority."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals
        signals = SessionSignals(mcp_session_id="mcp-abc", x_session_id="x-id")
        result = await derive_session_key(signals, {})
        assert result == "mcp:mcp-abc"

    @pytest.mark.asyncio
    async def test_priority_3_x_session_id(self):
        """x_session_id from signals is third priority."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals
        signals = SessionSignals(x_session_id="x-sess-456")
        result = await derive_session_key(signals, {})
        assert result == "x-sess-456"

    @pytest.mark.asyncio
    async def test_priority_4_oauth_client_id(self):
        """oauth_client_id from signals is fourth priority."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals
        signals = SessionSignals(oauth_client_id="oauth:client-789")
        result = await derive_session_key(signals, {})
        assert result == "oauth:client-789"

    @pytest.mark.asyncio
    async def test_priority_5_x_client_id(self):
        """x_client_id from signals is fifth priority."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals
        signals = SessionSignals(x_client_id="x-client-abc")
        result = await derive_session_key(signals, {})
        assert result == "x-client-abc"

    @pytest.mark.asyncio
    async def test_priority_6_ip_ua_fingerprint_no_pin(self):
        """ip_ua_fingerprint with no pin returns raw fingerprint."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals
        signals = SessionSignals(ip_ua_fingerprint="1.2.3.4:abc123")
        with patch("src.mcp_handlers.identity.session.lookup_onboard_pin", new_callable=AsyncMock, return_value=None):
            result = await derive_session_key(signals, {})
        assert result == "1.2.3.4:abc123"

    @pytest.mark.asyncio
    async def test_priority_6_ip_ua_fingerprint_with_pin(self):
        """ip_ua_fingerprint with pin returns pinned session ID."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals
        signals = SessionSignals(ip_ua_fingerprint="1.2.3.4:abc123")
        with patch("src.mcp_handlers.identity.session.lookup_onboard_pin", new_callable=AsyncMock, return_value="agent-pinned123"):
            result = await derive_session_key(signals, {})
        assert result == "agent-pinned123"

    @pytest.mark.asyncio
    async def test_priority_6_scoped_pin_prefers_model_client(self):
        """Scoped pin keys should be tried before unscoped keys."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals

        signals = SessionSignals(
            ip_ua_fingerprint="1.2.3.4:abc123",
            client_hint="chatgpt",
            user_agent="Codex/CLI",
        )

        async def lookup_side_effect(key, refresh_ttl=True):
            if key == "ua:abc123|chatgpt|gpt":
                return "agent-scoped123"
            return None

        with patch(
            "src.mcp_handlers.identity.session.lookup_onboard_pin",
            new_callable=AsyncMock,
            side_effect=lookup_side_effect,
        ):
            result = await derive_session_key(signals, {"model_type": "gpt-5-codex"})

        assert result == "agent-scoped123"

    @pytest.mark.asyncio
    async def test_pin_scope_recorded_client_model(self):
        """When the most-specific (client+model) candidate matches, scope is `client_model`."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals, get_pin_match_scope, set_pin_match_scope

        set_pin_match_scope(None)
        signals = SessionSignals(
            ip_ua_fingerprint="1.2.3.4:abc123",
            client_hint="chatgpt",
            user_agent="Codex/CLI",
        )

        async def lookup_side_effect(key, refresh_ttl=True):
            return "agent-cm123" if key == "ua:abc123|chatgpt|gpt" else None

        with patch(
            "src.mcp_handlers.identity.session.lookup_onboard_pin",
            new_callable=AsyncMock,
            side_effect=lookup_side_effect,
        ):
            await derive_session_key(signals, {"model_type": "gpt-5-codex"})

        assert get_pin_match_scope() == "client_model"

    @pytest.mark.asyncio
    async def test_pin_scope_recorded_client_only(self):
        """When the client-only candidate matches (model fallback), scope is `client`."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals, get_pin_match_scope, set_pin_match_scope

        set_pin_match_scope(None)
        signals = SessionSignals(
            ip_ua_fingerprint="1.2.3.4:abc123",
            client_hint="chatgpt",
            user_agent="Codex/CLI",
        )

        async def lookup_side_effect(key, refresh_ttl=True):
            return "agent-c123" if key == "ua:abc123|chatgpt" else None

        with patch(
            "src.mcp_handlers.identity.session.lookup_onboard_pin",
            new_callable=AsyncMock,
            side_effect=lookup_side_effect,
        ):
            await derive_session_key(signals, {"model_type": "gpt-5-codex"})

        assert get_pin_match_scope() == "client"

    @pytest.mark.asyncio
    async def test_pin_scope_recorded_unscoped(self):
        """Unscoped fallback match (no hint, no model) records `unscoped`."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals, get_pin_match_scope, set_pin_match_scope

        set_pin_match_scope(None)
        signals = SessionSignals(ip_ua_fingerprint="1.2.3.4:abc123")

        async def lookup_side_effect(key, refresh_ttl=True):
            return "agent-u123" if key == "ua:abc123" else None

        with patch(
            "src.mcp_handlers.identity.session.lookup_onboard_pin",
            new_callable=AsyncMock,
            side_effect=lookup_side_effect,
        ):
            await derive_session_key(signals, {})

        assert get_pin_match_scope() == "unscoped"

    @pytest.mark.asyncio
    async def test_pin_scope_unset_when_pin_misses(self):
        """When no pin matches, the scope contextvar is left at its default (None)."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals, get_pin_match_scope, set_pin_match_scope

        set_pin_match_scope(None)
        signals = SessionSignals(ip_ua_fingerprint="1.2.3.4:abc123")

        with patch(
            "src.mcp_handlers.identity.session.lookup_onboard_pin",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await derive_session_key(signals, {})

        assert get_pin_match_scope() is None

    @pytest.mark.asyncio
    async def test_shadow_lookup_runs_when_continuity_token_wins(self):
        """When continuity_token wins but a fingerprint signal exists, the shadow
        lookup runs and reports whether a pin would have been viable."""
        from src.mcp_handlers.identity.handlers import derive_session_key, create_continuity_token
        from src.mcp_handlers.context import (
            SessionSignals,
            get_shadow_pin_observation,
            set_shadow_pin_observation,
        )

        set_shadow_pin_observation(present=None, match=None, age_seconds=None)

        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            token = create_continuity_token(
                "11111111-2222-3333-4444-555555555555",
                "agent-shadow-test:claude",
                model_type="claude-opus-4-7",
            )
            signals = SessionSignals(
                ip_ua_fingerprint="1.2.3.4:abc123",
                user_agent="claude-cli/1",
            )

            mock_redis_client = AsyncMock()
            mock_redis_client.get = AsyncMock(return_value='{"client_session_id": "agent-shadow-test:claude"}')
            mock_redis_client.ttl = AsyncMock(return_value=1500)

            with patch(
                "src.cache.redis_client.get_redis",
                new_callable=AsyncMock,
                return_value=mock_redis_client,
            ):
                result = await derive_session_key(
                    signals,
                    {"continuity_token": token, "model_type": "claude-opus-4-7"},
                )

        assert result == "agent-shadow-test:claude"
        obs = get_shadow_pin_observation()
        assert obs["pin_entry_present"] is True
        assert obs["pin_fingerprint_match"] is True
        # _PIN_TTL is 1800; ttl_remaining 1500 → age 300
        assert obs["pin_entry_age_seconds"] == 300

    @pytest.mark.asyncio
    async def test_shadow_lookup_records_mismatch(self):
        """A pin that exists but points to a different identity records match=False."""
        from src.mcp_handlers.identity.handlers import derive_session_key, create_continuity_token
        from src.mcp_handlers.context import (
            SessionSignals,
            get_shadow_pin_observation,
            set_shadow_pin_observation,
        )

        set_shadow_pin_observation(present=None, match=None, age_seconds=None)

        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            token = create_continuity_token(
                "11111111-2222-3333-4444-555555555555",
                "agent-A:claude",
                model_type="claude-opus-4-7",
            )
            signals = SessionSignals(ip_ua_fingerprint="1.2.3.4:abc123", user_agent="claude-cli/1")

            mock_redis_client = AsyncMock()
            mock_redis_client.get = AsyncMock(return_value='{"client_session_id": "agent-B:codex"}')
            mock_redis_client.ttl = AsyncMock(return_value=900)

            with patch(
                "src.cache.redis_client.get_redis",
                new_callable=AsyncMock,
                return_value=mock_redis_client,
            ):
                await derive_session_key(
                    signals,
                    {"continuity_token": token, "model_type": "claude-opus-4-7"},
                )

        obs = get_shadow_pin_observation()
        assert obs["pin_entry_present"] is True
        assert obs["pin_fingerprint_match"] is False
        assert obs["pin_entry_age_seconds"] == 900  # 1800 - 900

    @pytest.mark.asyncio
    async def test_shadow_lookup_records_absent(self):
        """When no pin exists, present=False / match=False / age=None."""
        from src.mcp_handlers.identity.handlers import derive_session_key, create_continuity_token
        from src.mcp_handlers.context import (
            SessionSignals,
            get_shadow_pin_observation,
            set_shadow_pin_observation,
        )

        set_shadow_pin_observation(present=None, match=None, age_seconds=None)

        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            token = create_continuity_token(
                "11111111-2222-3333-4444-555555555555",
                "agent-A:claude",
                model_type="claude-opus-4-7",
            )
            signals = SessionSignals(ip_ua_fingerprint="1.2.3.4:abc123", user_agent="claude-cli/1")

            mock_redis_client = AsyncMock()
            mock_redis_client.get = AsyncMock(return_value=None)

            with patch(
                "src.cache.redis_client.get_redis",
                new_callable=AsyncMock,
                return_value=mock_redis_client,
            ):
                await derive_session_key(
                    signals,
                    {"continuity_token": token, "model_type": "claude-opus-4-7"},
                )

        obs = get_shadow_pin_observation()
        assert obs["pin_entry_present"] is False
        assert obs["pin_fingerprint_match"] is False
        assert obs["pin_entry_age_seconds"] is None

    @pytest.mark.asyncio
    async def test_shadow_lookup_skipped_when_pin_won(self):
        """If PATH 7 already won, the shadow lookup must NOT run — we already know the answer."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals, set_shadow_pin_observation, get_shadow_pin_observation

        set_shadow_pin_observation(present=None, match=None, age_seconds=None)

        signals = SessionSignals(ip_ua_fingerprint="1.2.3.4:abc123")
        get_redis_mock = AsyncMock()  # would observe a call if shadow ran

        with patch(
            "src.mcp_handlers.identity.session.lookup_onboard_pin",
            new_callable=AsyncMock,
            return_value="agent-pin-won",
        ), patch(
            "src.cache.redis_client.get_redis",
            new=get_redis_mock,
        ):
            result = await derive_session_key(signals, {})

        assert result == "agent-pin-won"
        get_redis_mock.assert_not_called()
        obs = get_shadow_pin_observation()
        # Shadow contextvars stay at the explicit None we set above.
        assert obs == {"pin_entry_present": None, "pin_fingerprint_match": None, "pin_entry_age_seconds": None}

    @pytest.mark.asyncio
    async def test_shadow_lookup_skipped_when_pin_already_missed(self):
        """If PATH 7 ran and missed (source=ip_ua_fingerprint), don't re-probe Redis."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals, set_shadow_pin_observation, get_shadow_pin_observation

        set_shadow_pin_observation(present=None, match=None, age_seconds=None)

        signals = SessionSignals(ip_ua_fingerprint="1.2.3.4:abc123")
        get_redis_mock = AsyncMock()

        with patch(
            "src.mcp_handlers.identity.session.lookup_onboard_pin",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "src.cache.redis_client.get_redis",
            new=get_redis_mock,
        ):
            await derive_session_key(signals, {})

        get_redis_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_shadow_lookup_skipped_without_fingerprint(self):
        """No IP/UA fingerprint signal → no shadow lookup."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals, set_shadow_pin_observation, get_shadow_pin_observation

        set_shadow_pin_observation(present=None, match=None, age_seconds=None)

        signals = SessionSignals(mcp_session_id="mcp-stable-1")
        get_redis_mock = AsyncMock()

        with patch("src.cache.redis_client.get_redis", new=get_redis_mock):
            await derive_session_key(signals, {})

        get_redis_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_shadow_lookup_failure_is_non_fatal(self):
        """Redis exception inside shadow must not break resolution."""
        from src.mcp_handlers.identity.handlers import derive_session_key, create_continuity_token
        from src.mcp_handlers.context import SessionSignals

        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            token = create_continuity_token(
                "11111111-2222-3333-4444-555555555555",
                "agent-A:claude",
                model_type="claude-opus-4-7",
            )
            signals = SessionSignals(ip_ua_fingerprint="1.2.3.4:abc123", user_agent="claude-cli/1")

            failing = AsyncMock(side_effect=RuntimeError("redis exploded"))

            with patch(
                "src.cache.redis_client.get_redis",
                new=failing,
            ):
                result = await derive_session_key(
                    signals,
                    {"continuity_token": token, "model_type": "claude-opus-4-7"},
                )

        assert result == "agent-A:claude"

    @pytest.mark.asyncio
    async def test_pin_match_does_not_split_resolution_source(self):
        """Splitting `pinned_onboard_session` into prefixed forms would silently
        bypass the gate at handlers.py:128. Pin scope is a side-channel ONLY;
        the resolution source string stays exact-match."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals, get_session_resolution_source

        signals = SessionSignals(
            ip_ua_fingerprint="1.2.3.4:abc123",
            client_hint="chatgpt",
            user_agent="Codex/CLI",
        )

        async def lookup_side_effect(key, refresh_ttl=True):
            return "agent-x" if key == "ua:abc123|chatgpt|gpt" else None

        with patch(
            "src.mcp_handlers.identity.session.lookup_onboard_pin",
            new_callable=AsyncMock,
            side_effect=lookup_side_effect,
        ):
            await derive_session_key(signals, {"model_type": "gpt-5-codex"})

        assert get_session_resolution_source() == "pinned_onboard_session"

    @pytest.mark.asyncio
    async def test_priority_6_scoped_pin_does_not_fallback_to_unscoped(self):
        """When scoped signals exist, unscoped pin fallback is intentionally skipped."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals

        signals = SessionSignals(
            ip_ua_fingerprint="1.2.3.4:abc123",
            client_hint="chatgpt",
            user_agent="Codex/CLI",
        )
        seen_keys = []

        async def lookup_side_effect(key, refresh_ttl=True):
            seen_keys.append(key)
            return None

        with patch(
            "src.mcp_handlers.identity.session.lookup_onboard_pin",
            new_callable=AsyncMock,
            side_effect=lookup_side_effect,
        ):
            result = await derive_session_key(signals, {"model_type": "gpt-5-codex"})

        assert result == "1.2.3.4:abc123"
        assert "ua:abc123" not in seen_keys

    @pytest.mark.asyncio
    async def test_priority_7_contextvars_fallback(self):
        """Falls back to contextvars when no signals."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        with patch("src.mcp_handlers.context.get_mcp_session_id", return_value="mcp-ctx"):
            result = await derive_session_key(None, {})
        assert result == "mcp:mcp-ctx"

    @pytest.mark.asyncio
    async def test_priority_8_stdio_fallback(self):
        """Falls back to stdio:{pid} when nothing available."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        with patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value=None):
            result = await derive_session_key(None, {})
        assert result.startswith("stdio:")

    @pytest.mark.asyncio
    async def test_none_signals_none_arguments(self):
        """Handles None signals and None arguments gracefully."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        with patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
             patch("src.mcp_handlers.context.get_context_session_key", return_value=None):
            result = await derive_session_key(None, None)
        assert result.startswith("stdio:")

    @pytest.mark.asyncio
    async def test_derive_session_key_explicit_client_session_id(self):
        """derive_session_key returns client_session_id when provided."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        result = await derive_session_key(None, {"client_session_id": "sync-test"})
        assert result == "sync-test"

    @pytest.mark.asyncio
    async def test_agent_prefix_skips_model_scoping(self):
        """agent-{uuid} IDs are globally unique — no model suffix appended."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals

        # Even with a Claude user agent that would normally produce ":claude" suffix
        signals = SessionSignals(user_agent="Claude-Code/1.0")
        result = await derive_session_key(
            signals,
            {"client_session_id": "agent-047202aa-56e"},
        )
        assert result == "agent-047202aa-56e"
        # No ":claude" suffix

    @pytest.mark.asyncio
    async def test_agent_prefix_skips_model_scoping_with_explicit_model_type(self):
        """agent-* IDs skip model scoping even when model_type is explicit."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals

        signals = SessionSignals(user_agent="Codex/CLI")
        result = await derive_session_key(
            signals,
            {"client_session_id": "agent-abc123-def", "model_type": "gpt-5-codex"},
        )
        assert result == "agent-abc123-def"

    @pytest.mark.asyncio
    async def test_non_agent_prefix_still_scoped(self):
        """Non-agent-* client_session_ids still get model scoping."""
        from src.mcp_handlers.identity.handlers import derive_session_key
        from src.mcp_handlers.context import SessionSignals

        signals = SessionSignals(user_agent="Codex/CLI")
        result = await derive_session_key(
            signals,
            {"client_session_id": "custom-session-123", "model_type": "gpt-5"},
        )
        assert result == "custom-session-123:gpt"


# ============================================================================
# _normalize_model_type
# ============================================================================

class TestSessionKeyValidation:

    @pytest.mark.asyncio
    async def test_empty_session_key_raises_valueerror(self, patch_all_deps):
        from src.mcp_handlers.identity.handlers import resolve_session_identity
        with pytest.raises(ValueError, match="session_key is required"):
            await resolve_session_identity(session_key="")

    @pytest.mark.asyncio
    async def test_long_session_key_truncated(self, patch_all_deps):
        """Session keys longer than 256 chars are truncated."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity
        long_key = "a" * 500
        result = await resolve_session_identity(session_key=long_key)
        assert result["created"] is True  # Should succeed

    @pytest.mark.asyncio
    async def test_special_chars_sanitized(self, patch_all_deps):
        """Characters outside allowed set are replaced with underscores."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity
        result = await resolve_session_identity(session_key="user'; DROP TABLE agents;--")
        assert result["created"] is True

    @pytest.mark.asyncio
    async def test_allowed_chars_not_sanitized(self, patch_all_deps):
        """Allowed characters pass through without sanitization."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity
        # alphanumeric, dash, underscore, colon, dot, at-sign
        clean_key = "user-name_123:test.session@host"
        result = await resolve_session_identity(session_key=clean_key)
        assert result["created"] is True

    @pytest.mark.asyncio
    async def test_sql_injection_in_session_key(self, patch_all_deps):
        """SQL injection attempts are safely handled."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity
        result = await resolve_session_identity(session_key="1 OR 1=1; --")
        assert result["created"] is True

    @pytest.mark.asyncio
    async def test_unicode_chars_sanitized(self, patch_all_deps):
        """Unicode characters outside allowed set are sanitized."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity
        result = await resolve_session_identity(session_key="test\x00null\x01ctrl")
        assert result["created"] is True


class TestContinuitySupportStatus:

    def test_support_status_disabled_without_secret(self):
        from src.mcp_handlers.identity.handlers import continuity_token_support_status
        with patch.dict("os.environ", {}, clear=True):
            status = continuity_token_support_status()
        assert status["enabled"] is False
        assert status["secret_source"] is None


# ============================================================================
# resolve_session_identity - PATH 1: Redis cache hit
# ============================================================================

class TestResolvePath1RedisHit:

    @pytest.mark.asyncio
    async def test_redis_uuid_hit_returns_cached(self, patch_all_deps, mock_redis, mock_db, mock_raw_redis):
        """When Redis has a UUID-format cached entry, return it directly."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": test_uuid,
            "display_agent_id": "Claude_Opus_20260206",
        }
        # Mock that agent exists in PG for the persisted check
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="id-1", metadata={})
        mock_db.get_agent_label.return_value = "TestAgent"

        result = await resolve_session_identity(session_key="redis-hit-session", resume=True)

        assert result["source"] == "redis"
        assert result["created"] is False
        assert result["agent_uuid"] == test_uuid
        assert result["agent_id"] == "Claude_Opus_20260206"
        assert result["persisted"] is True
        assert result["label"] == "TestAgent"

    @pytest.mark.asyncio
    async def test_redis_uuid_hit_without_display_agent_id(self, patch_all_deps, mock_redis, mock_db, mock_raw_redis):
        """When Redis has UUID but no display_agent_id, falls back to metadata lookup."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {"agent_id": test_uuid}
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="id-1",
            metadata={"agent_id": "Gemini_Pro_20260206"}
        )

        result = await resolve_session_identity(session_key="redis-hit-no-display", resume=True)

        assert result["source"] == "redis"
        assert result["agent_uuid"] == test_uuid
        assert result["agent_id"] == "Gemini_Pro_20260206"

    @pytest.mark.asyncio
    async def test_redis_uuid_hit_no_metadata_uses_uuid_as_agent_id(self, patch_all_deps, mock_redis, mock_db, mock_raw_redis):
        """When Redis has UUID but metadata lookup fails, agent_id falls back to UUID."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {"agent_id": test_uuid}
        # No metadata found
        mock_db.get_identity.return_value = None

        result = await resolve_session_identity(session_key="redis-hit-no-meta", resume=True)

        assert result["source"] == "redis"
        assert result["agent_uuid"] == test_uuid
        assert result["agent_id"] == test_uuid  # falls back to UUID

    @pytest.mark.asyncio
    async def test_redis_hit_refreshes_ttl(self, patch_all_deps, mock_redis, mock_db, mock_raw_redis):
        """Redis hit should refresh TTL via EXPIRE command (sliding window)."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {"agent_id": test_uuid, "display_agent_id": "Test_20260206"}
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="id-1", metadata={})

        await resolve_session_identity(session_key="ttl-refresh-test", resume=True)

        # Should have called expire on the raw redis
        mock_raw_redis.expire.assert_called()

    @pytest.mark.asyncio
    async def test_redis_hit_not_persisted(self, patch_all_deps, mock_redis, mock_db, mock_raw_redis):
        """Redis hit for agent that is NOT in PostgreSQL shows persisted=False."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {"agent_id": test_uuid, "display_agent_id": "Test_20260206"}
        mock_db.get_identity.return_value = None  # Not in PG

        result = await resolve_session_identity(session_key="not-persisted-session", resume=True)

        assert result["source"] == "redis"
        assert result["persisted"] is False
        assert result["label"] is None

    @pytest.mark.asyncio
    async def test_redis_exception_falls_through_to_pg(self, patch_all_deps, mock_redis, mock_db):
        """If Redis raises an exception, falls through to PostgreSQL path."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        mock_redis.get.side_effect = Exception("Redis connection refused")
        mock_db.get_session.return_value = None  # PG also has nothing

        result = await resolve_session_identity(session_key="redis-error-session")

        assert result["created"] is True  # Falls through to creation
        assert result["source"] in ("created", "memory_only")

    @pytest.mark.asyncio
    async def test_redis_returns_none_agent_id_falls_through(self, patch_all_deps, mock_redis, mock_db):
        """If Redis returns data with no agent_id, falls through."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        mock_redis.get.return_value = {"some_other_field": "value"}
        mock_db.get_session.return_value = None

        result = await resolve_session_identity(session_key="redis-no-agentid")

        assert result["created"] is True


# ============================================================================
# resolve_session_identity - PATH 2: PostgreSQL session lookup
# ============================================================================

class TestResolvePath2PostgresHit:

    @pytest.mark.asyncio
    async def test_pg_uuid_hit_returns_identity(self, patch_no_redis, mock_db):
        """When Redis misses but PG has session with UUID, returns it."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_db.get_session.return_value = SimpleNamespace(
            agent_id=test_uuid,
            session_id="pg-test-session",
        )
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="ident-1",
            metadata={"agent_id": "Claude_Opus_20260206"}
        )
        mock_db.get_agent_label.return_value = "MyAgent"

        result = await resolve_session_identity(session_key="pg-test-session", resume=True)

        assert result["source"] == "postgres"
        assert result["created"] is False
        assert result["persisted"] is True
        assert result["agent_uuid"] == test_uuid
        assert result["agent_id"] == "Claude_Opus_20260206"
        assert result["label"] == "MyAgent"

    @pytest.mark.asyncio
    async def test_pg_hit_updates_session_activity(self, patch_no_redis, mock_db):
        """PG hit should call update_session_activity (best effort)."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_db.get_session.return_value = SimpleNamespace(agent_id=test_uuid)
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="ident-1", metadata={"agent_id": "Test_20260206"}
        )

        await resolve_session_identity(session_key="activity-test", resume=True)

        mock_db.update_session_activity.assert_called_once_with("activity-test")

    @pytest.mark.asyncio
    async def test_pg_hit_warms_redis_cache(self, patch_all_deps, mock_redis, mock_db, mock_raw_redis):
        """PG hit should warm the Redis cache for next time."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        # Redis misses
        mock_redis.get.return_value = None
        # PG has the session
        mock_db.get_session.return_value = SimpleNamespace(agent_id=test_uuid)
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="ident-1", metadata={"agent_id": "Test_20260206"}
        )

        result = await resolve_session_identity(session_key="warm-cache-test", resume=True)

        assert result["source"] == "postgres"
        # Redis should have been written to (via _cache_session)
        # The _cache_session function uses raw redis setex when display_agent_id is different
        mock_raw_redis.setex.assert_called()

    @pytest.mark.asyncio
    async def test_pg_hit_no_metadata_uses_uuid(self, patch_no_redis, mock_db):
        """When PG has session but identity metadata lookup fails, falls back to UUID."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_db.get_session.return_value = SimpleNamespace(agent_id=test_uuid)
        # get_identity returns identity but with no agent_id in metadata
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="ident-1", metadata={}
        )

        result = await resolve_session_identity(session_key="no-meta-test", resume=True)

        assert result["source"] == "postgres"
        assert result["agent_uuid"] == test_uuid
        # agent_id falls back to uuid since metadata has no agent_id
        assert result["agent_id"] == test_uuid

    @pytest.mark.asyncio
    async def test_pg_exception_falls_through_to_create(self, patch_no_redis, mock_db):
        """If PG raises exception, falls through to PATH 3 (create new)."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        mock_db.get_session.side_effect = Exception("PG connection lost")

        result = await resolve_session_identity(session_key="pg-error-test")

        assert result["created"] is True
        assert result["source"] in ("created", "memory_only")


# ============================================================================
# resolve_session_identity - PATH 3: Create new agent
# ============================================================================

class TestResolvePath3CreateNew:

    @pytest.mark.asyncio
    async def test_creates_new_agent_lazy(self, patch_all_deps, mock_db):
        """Default persist=False creates lazy (memory only) agent."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        result = await resolve_session_identity(
            session_key="new-agent-lazy",
            model_type="claude-opus-4",
        )

        assert result["created"] is True
        assert result["persisted"] is False
        assert result["source"] == "memory_only"
        assert result["agent_id"].startswith("Claude_Opus_4_")
        # Auto-generated labels embed the UUID[:8] suffix for operator-facing
        # disambiguation (otherwise every "opus"-model agent shares one stem).
        uuid_prefix = result["agent_uuid"][:8]
        assert result["display_name"] == f"opus_{uuid_prefix}"
        assert result["label"] == f"opus_{uuid_prefix}"
        # UUID should be valid
        assert len(result["agent_uuid"]) == 36
        assert result["agent_uuid"].count("-") == 4
        # Should NOT have called upsert_agent (lazy)
        mock_db.upsert_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_new_agent_persisted(self, patch_all_deps, mock_db):
        """persist=True creates agent in PostgreSQL."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="new-ident-1", metadata={}
        )

        result = await resolve_session_identity(
            session_key="new-agent-persist",
            persist=True,
            model_type="gemini-pro",
        )

        assert result["created"] is True
        assert result["persisted"] is True
        assert result["source"] == "created"
        mock_db.upsert_agent.assert_called_once()
        mock_db.upsert_identity.assert_called_once()

    @pytest.mark.asyncio
    async def test_persisted_agent_creates_session_binding(self, patch_all_deps, mock_db):
        """persist=True also creates session binding in PG."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="ident-bind-1", metadata={}
        )

        await resolve_session_identity(
            session_key="session-bind-test",
            persist=True,
        )

        mock_db.create_session.assert_called_once()
        call_args = mock_db.create_session.call_args
        assert call_args.kwargs["session_id"] == "session-bind-test"

    @pytest.mark.asyncio
    async def test_auto_label_embeds_uuid_prefix(self, patch_all_deps, mock_db):
        """Auto-generated labels include the UUID[:8] suffix so dashboards and
        logs can tell otherwise-identical sessions apart (e.g. 16 Claude Code
        CLI sessions sharing the "claude_desktop-claude" stem). Explicit names
        passed at onboard take a different path (set_agent_label) and keep
        their verbatim value unless they collide."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        r1 = await resolve_session_identity(
            session_key="auto-label-1",
            client_hint="claude_code",
            model_type="claude-opus-4",
        )
        r2 = await resolve_session_identity(
            session_key="auto-label-2",
            client_hint="claude_code",
            model_type="claude-opus-4",
        )

        assert r1["label"].startswith("claude_code-opus_")
        assert r1["label"] == f"claude_code-opus_{r1['agent_uuid'][:8]}"
        assert r2["label"] == f"claude_code-opus_{r2['agent_uuid'][:8]}"
        assert r1["label"] != r2["label"]

    @pytest.mark.asyncio
    async def test_new_agent_uuid_is_unique(self, patch_all_deps):
        """Each new agent gets a unique UUID."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        result1 = await resolve_session_identity(session_key="unique-1")
        result2 = await resolve_session_identity(session_key="unique-2")

        assert result1["agent_uuid"] != result2["agent_uuid"]

    @pytest.mark.asyncio
    async def test_persist_failure_falls_through_to_memory_only(self, patch_all_deps, mock_db):
        """If PG persist fails, falls through to memory-only."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        mock_db.upsert_agent.side_effect = Exception("PG write failed")

        result = await resolve_session_identity(
            session_key="persist-fail-test",
            persist=True,
        )

        # Should fall through to memory_only
        assert result["created"] is True
        assert result["persisted"] is False
        assert result["source"] == "memory_only"


# ============================================================================
# resolve_session_identity - force_new
# ============================================================================

class TestResolveForceNew:

    @pytest.mark.asyncio
    async def test_force_new_skips_all_lookups(self, patch_all_deps, mock_redis, mock_db):
        """force_new=True bypasses Redis and PG lookups."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        result = await resolve_session_identity(
            session_key="force-new-test",
            force_new=True,
        )

        assert result["created"] is True
        # Should NOT have called Redis get or PG get_session
        mock_redis.get.assert_not_called()
        mock_db.get_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_new_creates_different_uuid(self, patch_all_deps, mock_redis):
        """force_new creates a new UUID even when cache exists."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        # First call creates an agent
        first = await resolve_session_identity(session_key="force-diff-test")

        # Reset redis mock to return the first agent
        mock_redis.get.return_value = {
            "agent_id": first["agent_uuid"],
            "display_agent_id": first["agent_id"],
        }

        # Second call with force_new should create a different UUID
        second = await resolve_session_identity(
            session_key="force-diff-test",
            force_new=True,
        )

        assert second["agent_uuid"] != first["agent_uuid"]
        assert second["created"] is True


# ============================================================================
# resolve_session_identity - PATH 2.5: Name-based identity claim
# ============================================================================

class TestCacheSession:

    @pytest.mark.asyncio
    async def test_cache_with_display_agent_id_uses_raw_redis(self, mock_raw_redis):
        """When display_agent_id differs from UUID, uses raw Redis setex."""
        from src.mcp_handlers.identity.handlers import _cache_session

        mock_cache = AsyncMock()

        async def _get_raw():
            return mock_raw_redis

        with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=mock_cache), \
             patch("src.cache.redis_client.get_redis", new=_get_raw):
            await _cache_session("sess-1", "uuid-1234", display_agent_id="Claude_20260206")

        mock_raw_redis.setex.assert_called_once()
        call_args = mock_raw_redis.setex.call_args
        assert call_args[0][0] == "session:sess-1"
        stored_data = json.loads(call_args[0][2])
        assert stored_data["agent_id"] == "uuid-1234"
        assert stored_data["display_agent_id"] == "Claude_20260206"

    @pytest.mark.asyncio
    async def test_cache_with_display_agent_id_updates_fallback_cache(self, mock_raw_redis):
        """Rich session writes should update SessionCache fallback state too."""
        from src.mcp_handlers.identity.handlers import _cache_session
        from src.cache.session_cache import _fallback_cache

        mock_cache = AsyncMock()

        async def _get_raw():
            return mock_raw_redis

        _fallback_cache.clear()
        try:
            with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
                 patch("src.cache.get_session_cache", return_value=mock_cache), \
                 patch("src.cache.redis_client.get_redis", new=_get_raw):
                await _cache_session(
                    "sess-rich",
                    "uuid-rich",
                    display_agent_id="Claude_20260206",
                    label="RichLabel",
                )

            assert "sess-rich" in _fallback_cache
            assert _fallback_cache["sess-rich"]["agent_id"] == "uuid-rich"
            assert _fallback_cache["sess-rich"]["display_agent_id"] == "Claude_20260206"
            assert _fallback_cache["sess-rich"]["label"] == "RichLabel"
        finally:
            _fallback_cache.clear()

    @pytest.mark.asyncio
    async def test_cache_with_spawn_reason_uses_rich_payload(self, mock_raw_redis):
        """Lineage metadata should survive Redis/fallback cache hydration."""
        from src.mcp_handlers.identity.handlers import _cache_session
        from src.cache.session_cache import _fallback_cache

        mock_cache = AsyncMock()

        async def _get_raw():
            return mock_raw_redis

        _fallback_cache.clear()
        try:
            with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
                 patch("src.cache.get_session_cache", return_value=mock_cache), \
                 patch("src.cache.redis_client.get_redis", new=_get_raw):
                await _cache_session(
                    "sess-spawn",
                    "uuid-spawn",
                    display_agent_id="uuid-spawn",
                    spawn_reason="dispatch_auto_mint",
                )

            mock_raw_redis.setex.assert_called_once()
            stored_data = json.loads(mock_raw_redis.setex.call_args[0][2])
            assert stored_data["agent_id"] == "uuid-spawn"
            assert stored_data["spawn_reason"] == "dispatch_auto_mint"
            assert _fallback_cache["sess-spawn"]["spawn_reason"] == "dispatch_auto_mint"
        finally:
            _fallback_cache.clear()

    @pytest.mark.asyncio
    async def test_redis_guard_exception_warns_and_fails_open_by_default(self):
        """H10: guard read errors must be visible but preserve default behavior."""
        from src.mcp_handlers.identity.persistence import _redis_slot_blocks_overwrite

        raw_redis = AsyncMock()
        raw_redis.get = AsyncMock(side_effect=RuntimeError("WRONGTYPE Operation"))

        with patch.dict(os.environ, {"UNITARES_NX_FAIL_CLOSED": "0"}, clear=False), \
             patch("src.mcp_handlers.identity.persistence.logger.warning") as log_warning:
            blocked = await _redis_slot_blocks_overwrite(
                raw_redis,
                "session:sess-guard-error",
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )

        assert blocked is False
        log_warning.assert_called_once()
        assert "S21A_REDIS_GUARD_READ_FAILED" in log_warning.call_args.args[0]

    @pytest.mark.asyncio
    async def test_redis_guard_exception_can_fail_closed(self, mock_raw_redis):
        """H10: UNITARES_NX_FAIL_CLOSED=1 refuses writes when guard read fails."""
        from src.mcp_handlers.identity.handlers import _cache_session

        mock_raw_redis.get = AsyncMock(side_effect=RuntimeError("connection reset"))
        mock_raw_redis.setex = AsyncMock()

        async def _get_raw():
            return mock_raw_redis

        mock_cache = AsyncMock()
        mock_cache.bind = AsyncMock()

        with patch.dict(os.environ, {"UNITARES_NX_FAIL_CLOSED": "1"}, clear=False), \
             patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=mock_cache), \
             patch("src.cache.redis_client.get_redis", new=_get_raw):
            await _cache_session(
                "sess-guard-fail-closed",
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                display_agent_id="GuardFailure",
                mint_guard=True,
            )

        mock_raw_redis.setex.assert_not_called()
        mock_cache.bind.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_without_display_id_uses_bind(self):
        """Without display_agent_id, uses SessionCache.bind()."""
        from src.mcp_handlers.identity.handlers import _cache_session

        mock_cache = AsyncMock()
        mock_cache.bind = AsyncMock()

        with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=mock_cache):
            await _cache_session("sess-2", "uuid-5678")

        mock_cache.bind.assert_called_once_with("sess-2", "uuid-5678")

    @pytest.mark.asyncio
    async def test_cache_display_id_same_as_uuid_uses_bind(self):
        """When display_agent_id == uuid, uses bind (no separate storage needed)."""
        from src.mcp_handlers.identity.handlers import _cache_session

        mock_cache = AsyncMock()
        mock_cache.bind = AsyncMock()

        with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=mock_cache):
            await _cache_session("sess-3", "uuid-same", display_agent_id="uuid-same")

        mock_cache.bind.assert_called_once_with("sess-3", "uuid-same")

    @pytest.mark.asyncio
    async def test_cache_redis_unavailable_no_error(self):
        """When Redis is unavailable, _cache_session does not raise."""
        from src.mcp_handlers.identity.handlers import _cache_session

        with patch("src.mcp_handlers.identity.persistence._redis_cache", False):
            # Should not raise
            await _cache_session("sess-4", "uuid-noop")

    @pytest.mark.asyncio
    async def test_cache_session_bind_deadlock_times_out(self):
        """If SessionCache.bind hangs (anyio-asyncio deadlock), _cache_session
        bails out within _REDIS_WRITE_TIMEOUT instead of blocking the handler.

        Watcher P004 fingerprint ee881e2a — regression guard.
        """
        import asyncio
        from src.mcp_handlers.identity.handlers import _cache_session
        from src.mcp_handlers.identity import persistence

        # Force a short timeout so the test does not rely on the default
        # handler budget, then bound the whole call with a CI-tolerant guard.
        original_timeout = persistence._REDIS_WRITE_TIMEOUT

        mock_cache = AsyncMock()
        cancelled = asyncio.Event()

        async def _never_returns(*_a, **_kw):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        mock_cache.bind = AsyncMock(side_effect=_never_returns)

        try:
            persistence._REDIS_WRITE_TIMEOUT = 0.05
            with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
                 patch("src.cache.get_session_cache", return_value=mock_cache):
                # Must not raise, must return promptly.
                await asyncio.wait_for(
                    _cache_session("sess-deadlock", "uuid-deadlock"),
                    timeout=1.0,
                )
            assert cancelled.is_set()
        finally:
            persistence._REDIS_WRITE_TIMEOUT = original_timeout

    @pytest.mark.asyncio
    async def test_cache_session_raw_setex_deadlock_times_out(self, mock_raw_redis):
        """Same guard on the raw-Redis branch (display_agent_id != uuid)."""
        import asyncio
        from src.mcp_handlers.identity.handlers import _cache_session
        from src.mcp_handlers.identity import persistence

        original_timeout = persistence._REDIS_WRITE_TIMEOUT
        cancelled = asyncio.Event()

        async def _never_returns(*_a, **_kw):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        mock_raw_redis.setex = AsyncMock(side_effect=_never_returns)

        async def _get_raw():
            return mock_raw_redis

        mock_cache = AsyncMock()

        try:
            persistence._REDIS_WRITE_TIMEOUT = 0.05
            with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
                 patch("src.cache.get_session_cache", return_value=mock_cache), \
                 patch("src.cache.redis_client.get_redis", new=_get_raw):
                await asyncio.wait_for(
                    _cache_session(
                        "sess-setex-deadlock",
                        "uuid-setex",
                        display_agent_id="Claude_Code_20260420",
                    ),
                    timeout=1.0,
                )
            assert cancelled.is_set()
        finally:
            persistence._REDIS_WRITE_TIMEOUT = original_timeout


# ============================================================================
# Integration-style tests (multiple paths)
# ============================================================================

class TestGetRedisExceptionPath:

    def test_redis_exception_marks_unavailable(self):
        """When get_session_cache raises, _get_redis sets cache to False."""
        import src.mcp_handlers.identity.persistence as mod

        # Save original
        original = mod._redis_cache
        try:
            mod._redis_cache = None
            with patch("src.cache.get_session_cache", side_effect=Exception("Connection refused")):
                result = mod._get_redis()

            assert result is None
            # Module-level _redis_cache should now be False (unavailable)
            assert mod._redis_cache is False
        finally:
            mod._redis_cache = original

    def test_redis_already_false_returns_none(self):
        """When _redis_cache is False (marked unavailable), returns None."""
        import src.mcp_handlers.identity.persistence as mod

        original = mod._redis_cache
        try:
            mod._redis_cache = False
            result = mod._get_redis()
            assert result is None
        finally:
            mod._redis_cache = original


# ============================================================================
# ============================================================================
# Redis TTL refresh exception (lines 354-356)
# ============================================================================

class TestRedisTtlRefreshException:

    @pytest.mark.asyncio
    async def test_ttl_refresh_failure_does_not_break_result(self, mock_db, mock_redis):
        """If TTL refresh fails, the cached result is still returned."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {
            "agent_id": test_uuid,
            "display_agent_id": "Claude_20260206",
        }
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="id-1", metadata={})
        mock_db.get_agent_label.return_value = "TestAgent"

        # Make raw redis raise on expire (TTL refresh)
        async def _raise_redis():
            raise Exception("Redis expire failed")

        with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=mock_redis), \
             patch("src.mcp_handlers.identity.resolution.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.cache.redis_client.get_redis", side_effect=Exception("Redis error")):
            result = await resolve_session_identity(session_key="ttl-fail-test", resume=True)

        assert result["source"] == "redis"
        assert result["agent_uuid"] == test_uuid
        assert result["created"] is False


# ============================================================================
# PG update_session_activity exception (lines 446-448)
# ============================================================================

class TestPgSessionActivityException:

    @pytest.mark.asyncio
    async def test_session_activity_update_failure_ignored(self, patch_no_redis, mock_db):
        """When update_session_activity raises, PG result is still returned."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_db.get_session.return_value = SimpleNamespace(agent_id=test_uuid)
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="ident-1", metadata={"agent_id": "Test_20260206"}
        )
        mock_db.get_agent_label.return_value = "MyAgent"
        mock_db.update_session_activity.side_effect = Exception("Activity update failed")

        result = await resolve_session_identity(session_key="activity-fail-test", resume=True)

        assert result["source"] == "postgres"
        assert result["agent_uuid"] == test_uuid
        assert result["created"] is False


# ============================================================================
# set_agent_label - structured_id migration and cache creation (lines 622-688)
# ============================================================================

class TestCacheSessionEdgeCases:

    @pytest.mark.asyncio
    async def test_raw_redis_none_falls_back_to_bind(self):
        """When raw redis returns None, falls back to session_cache.bind (line 818)."""
        from src.mcp_handlers.identity.handlers import _cache_session

        mock_cache = AsyncMock()
        mock_cache.bind = AsyncMock()

        async def _get_no_raw():
            return None

        with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=mock_cache), \
             patch("src.cache.redis_client.get_redis", new=_get_no_raw):
            await _cache_session("sess-fallback", "uuid-fb", display_agent_id="Agent_20260206")

        mock_cache.bind.assert_called_once_with("sess-fallback", "uuid-fb")

    @pytest.mark.asyncio
    async def test_cache_exception_is_caught(self):
        """When cache write raises, exception is swallowed (lines 821-823)."""
        from src.mcp_handlers.identity.handlers import _cache_session

        mock_cache = AsyncMock()
        mock_cache.bind.side_effect = Exception("Redis write error")

        with patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
             patch("src.cache.get_session_cache", return_value=mock_cache):
            # Should not raise
            await _cache_session("sess-err", "uuid-err")


# ============================================================================
# Soft Trajectory Verification (v2.8) — PATH 1/2
# ============================================================================

class TestSoftTrajectoryVerification:
    """Test soft trajectory verification for PATH 1 (Redis) and PATH 2 (PostgreSQL) resumption."""

    # -- _soft_verify_trajectory unit tests --

    @pytest.mark.asyncio
    async def test_helper_no_genesis_returns_verified_unchecked(self):
        """No stored genesis → verified=True, checked=False, no warning."""
        from src.mcp_handlers.identity.resolution import _soft_verify_trajectory

        with patch("src.trajectory_identity.get_trajectory_status",
                   new_callable=AsyncMock, return_value={"has_genesis": False}):
            result = await _soft_verify_trajectory("uuid-1", {"sig": "data"}, "redis")

        assert result["verified"] is True
        assert result["checked"] is False
        assert result["warning"] is None

    @pytest.mark.asyncio
    async def test_helper_genesis_exists_no_signature_warns(self):
        """Genesis exists but no signature → verified=False, warning=trajectory_unverified."""
        from src.mcp_handlers.identity.resolution import _soft_verify_trajectory

        with patch("src.trajectory_identity.get_trajectory_status",
                   new_callable=AsyncMock, return_value={"has_genesis": True}):
            result = await _soft_verify_trajectory("uuid-2", None, "postgres")

        assert result["verified"] is False
        assert result["checked"] is False
        assert result["warning"] == "trajectory_unverified"

    @pytest.mark.asyncio
    async def test_helper_genesis_exists_signature_verified(self):
        """Genesis exists + valid signature → verified=True, checked=True.
        Does NOT mock TrajectorySignature.from_dict — exercises the real conversion."""
        from src.mcp_handlers.identity.resolution import _soft_verify_trajectory
        from src.trajectory_identity import TrajectorySignature

        async def _verify_accepts_signature(agent_id, sig):
            assert isinstance(sig, TrajectorySignature), \
                f"verify_trajectory_identity received {type(sig).__name__}, expected TrajectorySignature"
            return {"verified": True}

        with patch("src.trajectory_identity.get_trajectory_status",
                   new_callable=AsyncMock, return_value={"has_genesis": True}), \
             patch("src.trajectory_identity.verify_trajectory_identity",
                   new_callable=AsyncMock, side_effect=_verify_accepts_signature):
            result = await _soft_verify_trajectory("uuid-3", {"preferences": {"vector": [1, 0]}}, "redis")

        assert result["verified"] is True
        assert result["checked"] is True
        assert result["warning"] is None

    @pytest.mark.asyncio
    async def test_helper_genesis_exists_signature_mismatch(self):
        """Genesis exists + mismatched signature → verified=False, warning=trajectory_mismatch.
        Does NOT mock TrajectorySignature.from_dict — exercises the real conversion."""
        from src.mcp_handlers.identity.resolution import _soft_verify_trajectory
        from src.trajectory_identity import TrajectorySignature

        mock_verification = {
            "verified": False,
            "tiers": {"lineage": {"similarity": 0.3}},
        }

        async def _verify_accepts_signature(agent_id, sig):
            assert isinstance(sig, TrajectorySignature), \
                f"verify_trajectory_identity received {type(sig).__name__}, expected TrajectorySignature"
            return mock_verification

        with patch("src.trajectory_identity.get_trajectory_status",
                   new_callable=AsyncMock, return_value={"has_genesis": True}), \
             patch("src.trajectory_identity.verify_trajectory_identity",
                   new_callable=AsyncMock, side_effect=_verify_accepts_signature):
            result = await _soft_verify_trajectory("uuid-4", {"beliefs": {"values": [0.5]}}, "postgres")

        assert result["verified"] is False
        assert result["checked"] is True
        assert result["warning"] == "trajectory_mismatch"
        assert result["lineage_similarity"] == 0.3

    @pytest.mark.asyncio
    async def test_helper_exception_fails_open(self):
        """Any exception → fail-open: verified=True, checked=False."""
        from src.mcp_handlers.identity.resolution import _soft_verify_trajectory

        with patch("src.trajectory_identity.get_trajectory_status",
                   new_callable=AsyncMock, side_effect=Exception("DB down")):
            result = await _soft_verify_trajectory("uuid-5", {"sig": "x"}, "redis")

        assert result["verified"] is True
        assert result["checked"] is False
        assert result["warning"] is None

    @pytest.mark.asyncio
    async def test_helper_trajectory_status_error_field(self):
        """get_trajectory_status returns error → treated as no genesis."""
        from src.mcp_handlers.identity.resolution import _soft_verify_trajectory

        with patch("src.trajectory_identity.get_trajectory_status",
                   new_callable=AsyncMock, return_value={"error": "not found", "has_genesis": False}):
            result = await _soft_verify_trajectory("uuid-6", None, "redis")

        assert result["verified"] is True
        assert result["checked"] is False

    @pytest.mark.asyncio
    async def test_helper_empty_dict_signature_treated_as_missing(self):
        """Empty dict {} is falsy in Python, treated same as None."""
        from src.mcp_handlers.identity.resolution import _soft_verify_trajectory

        with patch("src.trajectory_identity.get_trajectory_status",
                   new_callable=AsyncMock, return_value={"has_genesis": True}):
            result = await _soft_verify_trajectory("uuid-7", {}, "redis")

        # Empty dict is falsy → "not trajectory_signature" is True → treated as missing
        assert result["verified"] is False
        assert result["checked"] is False
        assert result["warning"] == "trajectory_unverified"

    # -- PATH 1 integration tests --

    @pytest.mark.asyncio
    async def test_path1_no_trajectory_stored(self, patch_all_deps, mock_redis, mock_db, mock_raw_redis):
        """PATH 1: No trajectory stored → no warning, trajectory_verified=True."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {"agent_id": test_uuid, "display_agent_id": "Agent_20260328"}
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="id-1", metadata={})

        with patch("src.trajectory_identity.get_trajectory_status",
                   new_callable=AsyncMock, return_value={"has_genesis": False}):
            result = await resolve_session_identity(session_key="p1-no-traj", resume=True)

        assert result["source"] == "redis"
        assert result["trajectory_verified"] is True
        assert result["trajectory_warning"] is None

    @pytest.mark.asyncio
    async def test_path1_trajectory_stored_signature_provided_verified(self, patch_all_deps, mock_redis, mock_db, mock_raw_redis):
        """PATH 1: Trajectory stored + valid signature → trajectory_verified=True."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {"agent_id": test_uuid, "display_agent_id": "Agent_20260328"}
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="id-1", metadata={})

        with patch("src.trajectory_identity.get_trajectory_status",
                   new_callable=AsyncMock, return_value={"has_genesis": True}), \
             patch("src.trajectory_identity.verify_trajectory_identity",
                   new_callable=AsyncMock, return_value={"verified": True}):
            result = await resolve_session_identity(
                session_key="p1-verified", resume=True,
                trajectory_signature={"preferences": [0.1]},
            )

        assert result["source"] == "redis"
        assert result["trajectory_verified"] is True
        assert result["trajectory_warning"] is None

    @pytest.mark.asyncio
    async def test_path1_trajectory_stored_signature_mismatch(self, patch_all_deps, mock_redis, mock_db, mock_raw_redis):
        """PATH 1: Trajectory stored + mismatched signature → trajectory_verified=False, warning set."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {"agent_id": test_uuid, "display_agent_id": "Agent_20260328"}
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="id-1", metadata={})

        mock_verification = {"verified": False, "tiers": {"lineage": {"similarity": 0.2}}}

        with patch("src.trajectory_identity.get_trajectory_status",
                   new_callable=AsyncMock, return_value={"has_genesis": True}), \
             patch("src.trajectory_identity.verify_trajectory_identity",
                   new_callable=AsyncMock, return_value=mock_verification):
            result = await resolve_session_identity(
                session_key="p1-mismatch", resume=True,
                trajectory_signature={"preferences": [0.9]},
            )

        assert result["source"] == "redis"
        assert result["trajectory_verified"] is False
        assert result["trajectory_warning"] == "trajectory_mismatch"

    @pytest.mark.asyncio
    async def test_path1_trajectory_stored_no_signature(self, patch_all_deps, mock_redis, mock_db, mock_raw_redis):
        """PATH 1: Trajectory stored + no signature → trajectory_verified=False, warning=trajectory_unverified."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {"agent_id": test_uuid, "display_agent_id": "Agent_20260328"}
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="id-1", metadata={})

        with patch("src.trajectory_identity.get_trajectory_status",
                   new_callable=AsyncMock, return_value={"has_genesis": True}):
            result = await resolve_session_identity(session_key="p1-no-sig", resume=True)

        assert result["source"] == "redis"
        assert result["trajectory_verified"] is False
        assert result["trajectory_warning"] == "trajectory_unverified"

    # -- PATH 2 integration tests --

    @pytest.mark.asyncio
    async def test_path2_no_trajectory_stored(self, patch_no_redis, mock_db):
        """PATH 2: No trajectory stored → trajectory_verified=True, no warning."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_db.get_session.return_value = SimpleNamespace(agent_id=test_uuid)
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="ident-1", metadata={"agent_id": "Agent_20260328"}
        )

        with patch("src.trajectory_identity.get_trajectory_status",
                   new_callable=AsyncMock, return_value={"has_genesis": False}):
            result = await resolve_session_identity(session_key="p2-no-traj", resume=True)

        assert result["source"] == "postgres"
        assert result["trajectory_verified"] is True
        assert result["trajectory_warning"] is None

    @pytest.mark.asyncio
    async def test_path2_trajectory_stored_signature_provided_verified(self, patch_no_redis, mock_db):
        """PATH 2: Trajectory stored + valid signature → trajectory_verified=True."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_db.get_session.return_value = SimpleNamespace(agent_id=test_uuid)
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="ident-1", metadata={"agent_id": "Agent_20260328"}
        )

        with patch("src.trajectory_identity.get_trajectory_status",
                   new_callable=AsyncMock, return_value={"has_genesis": True}), \
             patch("src.trajectory_identity.verify_trajectory_identity",
                   new_callable=AsyncMock, return_value={"verified": True}):
            result = await resolve_session_identity(
                session_key="p2-verified", resume=True,
                trajectory_signature={"preferences": [0.1]},
            )

        assert result["source"] == "postgres"
        assert result["trajectory_verified"] is True
        assert result["trajectory_warning"] is None

    @pytest.mark.asyncio
    async def test_path2_trajectory_stored_signature_mismatch(self, patch_no_redis, mock_db):
        """PATH 2: Trajectory stored + mismatched signature → trajectory_verified=False."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_db.get_session.return_value = SimpleNamespace(agent_id=test_uuid)
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="ident-1", metadata={"agent_id": "Agent_20260328"}
        )

        mock_verification = {"verified": False, "tiers": {"lineage": {"similarity": 0.15}}}

        with patch("src.trajectory_identity.get_trajectory_status",
                   new_callable=AsyncMock, return_value={"has_genesis": True}), \
             patch("src.trajectory_identity.verify_trajectory_identity",
                   new_callable=AsyncMock, return_value=mock_verification):
            result = await resolve_session_identity(
                session_key="p2-mismatch", resume=True,
                trajectory_signature={"preferences": [0.9]},
            )

        assert result["source"] == "postgres"
        assert result["trajectory_verified"] is False
        assert result["trajectory_warning"] == "trajectory_mismatch"

    @pytest.mark.asyncio
    async def test_path2_trajectory_stored_no_signature(self, patch_no_redis, mock_db):
        """PATH 2: Trajectory stored + no signature → trajectory_unverified warning."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_db.get_session.return_value = SimpleNamespace(agent_id=test_uuid)
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="ident-1", metadata={"agent_id": "Agent_20260328"}
        )

        with patch("src.trajectory_identity.get_trajectory_status",
                   new_callable=AsyncMock, return_value={"has_genesis": True}):
            result = await resolve_session_identity(session_key="p2-no-sig", resume=True)

        assert result["source"] == "postgres"
        assert result["trajectory_verified"] is False
        assert result["trajectory_warning"] == "trajectory_unverified"

    # -- Exception fail-open test --

    @pytest.mark.asyncio
    async def test_verification_exception_fails_open(self, patch_all_deps, mock_redis, mock_db, mock_raw_redis):
        """Exception in trajectory check → fail-open (verified=True, no warning)."""
        from src.mcp_handlers.identity.handlers import resolve_session_identity

        test_uuid = str(uuid.uuid4())
        mock_redis.get.return_value = {"agent_id": test_uuid, "display_agent_id": "Agent_20260328"}
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="id-1", metadata={})

        with patch("src.trajectory_identity.get_trajectory_status",
                   new_callable=AsyncMock, side_effect=Exception("trajectory service down")):
            result = await resolve_session_identity(session_key="p1-exception", resume=True)

        assert result["source"] == "redis"
        assert result["trajectory_verified"] is True
        assert result["trajectory_warning"] is None


# ============================================================================
# lookup_onboard_pin / set_onboard_pin exception paths (lines 1138-1175)
# ============================================================================
