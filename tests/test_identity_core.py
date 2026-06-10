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

class TestGenerateAgentId:

    @pytest.fixture(autouse=True)
    def import_fn(self):
        from src.mcp_handlers.identity.handlers import _generate_agent_id
        self.generate = _generate_agent_id

    def test_with_model_type_claude(self):
        result = self.generate(model_type="claude-opus-4-5")
        assert result.startswith("Claude_Opus_4_5_")
        # Ends with YYYYMMDD
        date_part = result.split("_")[-1]
        assert len(date_part) == 8
        assert date_part.isdigit()

    def test_with_model_type_gemini(self):
        result = self.generate(model_type="gemini-pro")
        assert result.startswith("Gemini_Pro_")

    def test_with_model_type_dots(self):
        result = self.generate(model_type="gpt.4.turbo")
        assert "Gpt" in result
        assert "4" in result
        assert "Turbo" in result

    def test_with_client_hint(self):
        result = self.generate(client_hint="cursor")
        assert result.startswith("cursor_")

    def test_client_hint_with_spaces_rejected(self):
        # Spaces are not valid in a client_hint shape — descriptors must not
        # become identifiers. Falls through to anon_DATE.
        result = self.generate(client_hint="my editor")
        assert result.startswith("anon_")

    def test_fallback_no_args(self):
        result = self.generate()
        assert result.startswith("anon_")

    def test_third_party_client_prefixed_with_model(self):
        result = self.generate(model_type="gemini-pro", client_hint="cursor")
        assert result.startswith("Cursor_Gemini_Pro_")

    def test_native_client_not_prefixed_with_model(self):
        result = self.generate(model_type="claude-opus-4-5", client_hint="claude_desktop")
        assert result.startswith("Claude_Opus_4_5_")
        assert "Desktop" not in result

    def test_empty_client_hint_fallback(self):
        result = self.generate(client_hint="")
        assert result.startswith("anon_")

    def test_unknown_client_hint_fallback(self):
        result = self.generate(client_hint="unknown")
        assert result.startswith("anon_")

    def test_auto_minted_names_pass_reserved_names_gate(self):
        # The mint↔gate coupling test that was missing for months: the
        # auto-mint MUST produce names the reserved-names security gate
        # accepts. The historical default f"mcp_{timestamp}" was minted by
        # the server and then rejected by the server ('mcp_' is in
        # RESERVED_PREFIXES) — every anonymous poller failed every tool call
        # with error_type=reserved_prefix, invisible until PR #543 made
        # tool_usage.success honest (live incident, 2026-06-10).
        from src.mcp_handlers.validators import validate_agent_id_reserved_names

        for kwargs in (
            {},                              # bare anonymous session
            {"client_hint": "my editor"},    # invalid hint → fallback
            {"client_hint": ""},             # empty hint → fallback
            {"client_hint": "unknown"},      # filtered hint → fallback
            {"model_type": "claude-opus-4-5", "client_hint": "claude_desktop"},
            {"client_hint": "cursor"},
        ):
            minted = self.generate(**kwargs)
            ok, err = validate_agent_id_reserved_names(minted)
            assert err is None, (
                f"auto-mint produced {minted!r} which the reserved-names "
                f"gate rejects — server would refuse its own identity"
            )
            assert ok == minted

        # KNOWN GAP, pinned deliberately (council 2026-06-10): a model_type
        # or client_hint that BEGINS with a reserved family word still mints
        # a name the gate rejects — e.g. model_type="governance-core" →
        # "Governance_Core_<date>" → reserved prefix 'governance_' (the gate
        # lowercases before matching). Same incident class as the anonymous
        # fallback, for NAMED callers; rare today (no real model/client name
        # starts with a reserved word) and the mint-side remedy is an
        # identity-surface design call — tracked as a follow-up. Pinned here
        # so the gap stays visible instead of silent:
        named_collision = self.generate(model_type="governance-core")
        _, err = validate_agent_id_reserved_names(named_collision)
        assert err is not None, (
            "named reserved-word collision unexpectedly resolved — update "
            "this pin, the coupling-test scope, and close the follow-up"
        )

    def test_whitespace_model_type(self):
        result = self.generate(model_type="  claude-haiku  ")
        assert result.startswith("Claude_Haiku_")

    def test_underscores_in_model_type(self):
        result = self.generate(model_type="claude_opus_4")
        assert result.startswith("Claude_Opus_4_")


# ============================================================================
# _get_date_context (pure function)
# ============================================================================

class TestGetDateContext:

    @pytest.fixture(autouse=True)
    def import_fn(self):
        from src.mcp_handlers.identity.handlers import _get_date_context
        self.get_ctx = _get_date_context

    def test_returns_all_required_keys(self):
        result = self.get_ctx()
        required = ['full', 'short', 'compact', 'iso', 'iso_utc', 'year', 'month', 'weekday']
        for k in required:
            assert k in result, f"Missing key: {k}"

    def test_iso_utc_ends_with_z(self):
        result = self.get_ctx()
        assert result['iso_utc'].endswith('Z')

    def test_compact_is_digits(self):
        result = self.get_ctx()
        assert result['compact'].isdigit()
        assert len(result['compact']) == 8


# ============================================================================
# derive_session_key - Priority chain (signals=None uses context/stdio)
# ============================================================================

class TestNormalizeModelType:
    """Tests for model type normalization helper."""

    def test_claude_variants(self):
        from src.mcp_handlers.identity.handlers import _normalize_model_type
        assert _normalize_model_type("claude-opus-4-5") == "claude"
        assert _normalize_model_type("Claude-Sonnet-4") == "claude"
        assert _normalize_model_type("claude") == "claude"

    def test_gemini(self):
        from src.mcp_handlers.identity.handlers import _normalize_model_type
        assert _normalize_model_type("gemini-pro") == "gemini"

    def test_gpt(self):
        from src.mcp_handlers.identity.handlers import _normalize_model_type
        assert _normalize_model_type("gpt-4o") == "gpt"
        assert _normalize_model_type("chatgpt") == "gpt"

    def test_cursor(self):
        from src.mcp_handlers.identity.handlers import _normalize_model_type
        assert _normalize_model_type("cursor") == "composer"
        assert _normalize_model_type("composer") == "composer"

    def test_llama(self):
        from src.mcp_handlers.identity.handlers import _normalize_model_type
        assert _normalize_model_type("llama-3.1") == "llama"

    def test_unknown_passthrough(self):
        from src.mcp_handlers.identity.handlers import _normalize_model_type
        result = _normalize_model_type("mistral-7b")
        assert result == "mistral_7b"


# Name-claim tests removed 2026-04-17. See test_name_cosmetic_invariant.py
# for the current invariant (name is cosmetic; resolve_session_identity
# does not accept agent_name and does not look up by label).


# ============================================================================
# _agent_exists_in_postgres
# ============================================================================

class TestAgentExistsInPostgres:

    @pytest.mark.asyncio
    async def test_returns_true_when_identity_found(self):
        from src.mcp_handlers.identity.handlers import _agent_exists_in_postgres

        mock_db = AsyncMock()
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            assert await _agent_exists_in_postgres("uuid-exists") is True

    @pytest.mark.asyncio
    async def test_returns_false_when_not_found(self):
        from src.mcp_handlers.identity.handlers import _agent_exists_in_postgres

        mock_db = AsyncMock()
        mock_db.get_identity.return_value = None

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            assert await _agent_exists_in_postgres("uuid-not-found") is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        from src.mcp_handlers.identity.handlers import _agent_exists_in_postgres

        mock_db = AsyncMock()
        mock_db.get_identity.side_effect = Exception("DB down")

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            assert await _agent_exists_in_postgres("uuid-error") is False


# ============================================================================
# _get_agent_label
# ============================================================================

class TestGetAgentLabel:

    @pytest.mark.asyncio
    async def test_returns_label_from_db(self):
        from src.mcp_handlers.identity.handlers import _get_agent_label

        mock_db = AsyncMock()
        mock_db.get_agent_label.return_value = "MyAgent"

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            result = await _get_agent_label("uuid-label")
            assert result == "MyAgent"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        from src.mcp_handlers.identity.handlers import _get_agent_label

        mock_db = AsyncMock()
        mock_db.get_agent_label.return_value = None

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            result = await _get_agent_label("uuid-no-label")
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self):
        from src.mcp_handlers.identity.handlers import _get_agent_label

        mock_db = AsyncMock()
        mock_db.get_agent_label.side_effect = Exception("DB error")

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            result = await _get_agent_label("uuid-error")
            assert result is None


# ============================================================================
# _get_agent_id_from_metadata
# ============================================================================

class TestGetAgentIdFromMetadata:

    @pytest.mark.asyncio
    async def test_returns_agent_id_from_identity_metadata(self):
        from src.mcp_handlers.identity.handlers import _get_agent_id_from_metadata

        mock_db = AsyncMock()
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="i1",
            metadata={"agent_id": "Claude_Opus_20260206"}
        )

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            result = await _get_agent_id_from_metadata("uuid-meta")
            assert result == "Claude_Opus_20260206"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_identity(self):
        from src.mcp_handlers.identity.handlers import _get_agent_id_from_metadata

        mock_db = AsyncMock()
        mock_db.get_identity.return_value = None

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            result = await _get_agent_id_from_metadata("uuid-no-identity")
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_metadata(self):
        from src.mcp_handlers.identity.handlers import _get_agent_id_from_metadata

        mock_db = AsyncMock()
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="i1", metadata=None
        )

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            result = await _get_agent_id_from_metadata("uuid-no-meta")
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_metadata_has_no_agent_id(self):
        from src.mcp_handlers.identity.handlers import _get_agent_id_from_metadata

        mock_db = AsyncMock()
        mock_db.get_identity.return_value = SimpleNamespace(
            identity_id="i1", metadata={"some_other": "data"}
        )

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            result = await _get_agent_id_from_metadata("uuid-no-aid")
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self):
        from src.mcp_handlers.identity.handlers import _get_agent_id_from_metadata

        mock_db = AsyncMock()
        mock_db.get_identity.side_effect = Exception("DB error")

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            result = await _get_agent_id_from_metadata("uuid-error")
            assert result is None


# ============================================================================
# _find_agent_by_label
# ============================================================================

class TestFindAgentByLabel:

    @pytest.mark.asyncio
    async def test_returns_uuid_when_found(self):
        from src.mcp_handlers.identity.handlers import _find_agent_by_label

        mock_db = AsyncMock()
        mock_db.find_agent_by_label.return_value = "uuid-found-by-label"

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            result = await _find_agent_by_label("MyAgent")
            assert result == "uuid-found-by-label"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        from src.mcp_handlers.identity.handlers import _find_agent_by_label

        mock_db = AsyncMock()
        mock_db.find_agent_by_label.return_value = None

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            result = await _find_agent_by_label("Nonexistent")
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self):
        from src.mcp_handlers.identity.handlers import _find_agent_by_label

        mock_db = AsyncMock()
        mock_db.find_agent_by_label.side_effect = Exception("DB error")

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db):
            result = await _find_agent_by_label("Error")
            assert result is None


# ============================================================================
# ensure_agent_persisted (lazy creation)
# ============================================================================

class TestSetAgentLabel:

    @pytest.mark.asyncio
    async def test_sets_label_successfully(self):
        """Sets label via db.update_agent_fields."""
        from src.mcp_handlers.identity.handlers import set_agent_label

        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.find_agent_by_label.return_value = None  # No collision
        mock_db.update_agent_fields.return_value = True
        mock_db.upsert_agent = AsyncMock()
        mock_db.upsert_identity = AsyncMock()
        mock_db.create_session = AsyncMock()

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence._redis_cache", False), \
             patch("src.mcp_handlers.shared.get_mcp_server", side_effect=Exception("no server")):
            result = await set_agent_label("uuid-label-set", "NewLabel")

        assert result is True
        mock_db.update_agent_fields.assert_called_once_with("uuid-label-set", label="NewLabel")

    @pytest.mark.asyncio
    async def test_empty_label_returns_false(self):
        from src.mcp_handlers.identity.handlers import set_agent_label
        result = await set_agent_label("uuid-1", "")
        assert result is False

    @pytest.mark.asyncio
    async def test_empty_uuid_returns_false(self):
        from src.mcp_handlers.identity.handlers import set_agent_label
        result = await set_agent_label("", "Label")
        assert result is False

    @pytest.mark.asyncio
    async def test_label_collision_appends_suffix(self):
        """When label already exists for different agent, appends UUID suffix."""
        from src.mcp_handlers.identity.handlers import set_agent_label

        test_uuid = "aaaabbbb-1234-5678-9abc-def012345678"
        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.find_agent_by_label.return_value = "other-uuid"  # Collision!
        mock_db.update_agent_fields.return_value = True
        mock_db.upsert_agent = AsyncMock()
        mock_db.upsert_identity = AsyncMock()
        mock_db.create_session = AsyncMock()

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence._redis_cache", False), \
             patch("src.mcp_handlers.shared.get_mcp_server", side_effect=Exception("no server")):
            result = await set_agent_label(test_uuid, "DuplicateName")

        assert result is True
        # Should have been called with suffixed label
        call_args = mock_db.update_agent_fields.call_args
        label_used = call_args.kwargs.get("label") or call_args[1].get("label")
        assert label_used.startswith("DuplicateName_")
        assert test_uuid[:8] in label_used


# ============================================================================
# _extract_base_fingerprint
# ============================================================================

class TestExtractBaseFingerprint:

    @pytest.fixture(autouse=True)
    def import_fn(self):
        from src.mcp_handlers.identity.handlers import _extract_base_fingerprint
        self.extract = _extract_base_fingerprint

    def test_mcp_prefix_returns_none(self):
        assert self.extract("mcp:session-abc") is None

    def test_stdio_prefix_returns_none(self):
        assert self.extract("stdio:12345") is None

    def test_agent_prefix_returns_none(self):
        assert self.extract("agent-uuid-prefix") is None

    def test_ip_ua_hash_extracts_ua(self):
        result = self.extract("192.168.1.1:d20c2f")
        assert result == "ua:d20c2f"

    def test_ip_ua_hash_suffix_extracts_ua(self):
        result = self.extract("192.168.1.1:d20c2f:extra_suffix")
        assert result == "ua:d20c2f"

    def test_single_part_returns_as_is(self):
        result = self.extract("onlyone")
        assert result == "onlyone"

    def test_none_returns_none(self):
        assert self.extract(None) is None

    def test_empty_returns_none(self):
        assert self.extract("") is None


# ============================================================================
# ua_hash_from_header
# ============================================================================

class TestUaHashFromHeader:

    @pytest.fixture(autouse=True)
    def import_fn(self):
        from src.mcp_handlers.identity.handlers import ua_hash_from_header
        self.ua_hash = ua_hash_from_header

    def test_returns_ua_prefix_hash(self):
        import hashlib
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        expected_hash = hashlib.md5(ua.encode()).hexdigest()[:6]
        result = self.ua_hash(ua)
        assert result == f"ua:{expected_hash}"

    def test_returns_none_for_empty(self):
        assert self.ua_hash("") is None

    def test_returns_none_for_none(self):
        assert self.ua_hash(None) is None

    def test_consistent_results(self):
        """Same UA string always produces same hash."""
        ua = "TestAgent/1.0"
        r1 = self.ua_hash(ua)
        r2 = self.ua_hash(ua)
        assert r1 == r2

    def test_different_ua_different_hash(self):
        """Different UA strings produce different hashes."""
        r1 = self.ua_hash("Agent/1.0")
        r2 = self.ua_hash("Agent/2.0")
        assert r1 != r2


# ============================================================================
# lookup_onboard_pin / set_onboard_pin
# ============================================================================

# TestResolveByNameClaim removed 2026-04-17 — see test_name_cosmetic_invariant.py



# ============================================================================
# _cache_session
# ============================================================================

class TestSetAgentLabelCacheManagement:

    @pytest.mark.asyncio
    async def test_syncs_label_to_existing_metadata_entry(self):
        """When agent is already in cache, label is synced to existing entry."""
        from src.mcp_handlers.identity.handlers import set_agent_label

        test_uuid = "aaaabbbb-1111-2222-3333-444455556666"
        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.find_agent_by_label.return_value = None
        mock_db.update_agent_fields.return_value = True

        mock_server = MagicMock()
        meta = SimpleNamespace(label=None, structured_id="existing_id")
        mock_server.agent_metadata = {test_uuid: meta}

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence._redis_cache", False), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server):
            result = await set_agent_label(test_uuid, "NewLabel")

        assert result is True
        assert meta.label == "NewLabel"

    @pytest.mark.asyncio
    async def test_creates_new_metadata_entry_when_not_cached(self):
        """When agent is NOT in cache, a new AgentMetadata entry is created."""
        from src.mcp_handlers.identity.handlers import set_agent_label

        test_uuid = "aaaabbbb-1111-2222-3333-444455556666"
        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.find_agent_by_label.return_value = None
        mock_db.update_agent_fields.return_value = True

        mock_server = MagicMock()
        mock_server.agent_metadata = {}  # Empty - agent not cached

        # Mock AgentMetadata class
        mock_meta_class = MagicMock()
        mock_meta_instance = SimpleNamespace(
            agent_id=test_uuid, status='active', created_at='', last_update=''
        )
        mock_meta_class.return_value = mock_meta_instance

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence._redis_cache", False), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server), \
             patch("src.agent_state.AgentMetadata", mock_meta_class), \
             patch("src.mcp_handlers.identity.handlers.detect_interface_context", return_value={"type": "test"}, create=True), \
             patch("src.mcp_handlers.identity.handlers.generate_structured_id", return_value="test_1", create=True), \
             patch("src.mcp_handlers.context.get_context_client_hint", return_value="test"):
            result = await set_agent_label(test_uuid, "FreshLabel")

        assert result is True
        # Agent should now be in the metadata dict
        assert test_uuid in mock_server.agent_metadata

    @pytest.mark.asyncio
    async def test_structured_id_generation_failure_handled(self):
        """If structured_id generation fails, label is still set."""
        from src.mcp_handlers.identity.handlers import set_agent_label

        test_uuid = "aaaabbbb-1111-2222-3333-444455556666"
        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.find_agent_by_label.return_value = None
        mock_db.update_agent_fields.return_value = True

        mock_server = MagicMock()
        meta = SimpleNamespace(label=None, structured_id=None)
        mock_server.agent_metadata = {test_uuid: meta}

        # detect_interface_context raises
        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence._redis_cache", False), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server), \
             patch("src.mcp_handlers.support.naming_helpers.detect_interface_context", side_effect=Exception("No context")):
            result = await set_agent_label(test_uuid, "LabelWithoutStructured")

        assert result is True
        assert meta.label == "LabelWithoutStructured"

    @pytest.mark.asyncio
    async def test_session_binding_cache_updated(self):
        """Session binding cache is updated when label is set."""
        from src.mcp_handlers.identity.handlers import set_agent_label

        test_uuid = "aaaabbbb-1111-2222-3333-444455556666"
        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.find_agent_by_label.return_value = None
        mock_db.update_agent_fields.return_value = True

        mock_server = MagicMock()
        meta = SimpleNamespace(label=None, structured_id="existing_id")
        mock_server.agent_metadata = {test_uuid: meta}

        # Create a session binding
        session_identities = {
            "test-session": {"bound_agent_id": test_uuid, "agent_label": None}
        }

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence._redis_cache", False), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server), \
             patch("src.mcp_handlers.identity.shared._session_identities", session_identities):
            result = await set_agent_label(test_uuid, "UpdatedLabel")

        assert result is True
        assert session_identities["test-session"]["agent_label"] == "UpdatedLabel"

    @pytest.mark.asyncio
    async def test_session_binding_update_failure_handled(self):
        """If session binding update fails, label is still set."""
        from src.mcp_handlers.identity.handlers import set_agent_label

        test_uuid = "aaaabbbb-1111-2222-3333-444455556666"
        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.find_agent_by_label.return_value = None
        mock_db.update_agent_fields.return_value = True

        mock_server = MagicMock()
        meta = SimpleNamespace(label=None, structured_id="existing_id")
        mock_server.agent_metadata = {test_uuid: meta}

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence._redis_cache", False), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server), \
             patch("src.mcp_handlers.identity.shared._session_identities", side_effect=Exception("import fail")):
            result = await set_agent_label(test_uuid, "StillWorks")

        assert result is True

    @pytest.mark.asyncio
    async def test_redis_metadata_invalidation_on_label_set(self):
        """Redis metadata cache is invalidated after label set."""
        from src.mcp_handlers.identity.handlers import set_agent_label

        test_uuid = "aaaabbbb-1111-2222-3333-444455556666"
        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.find_agent_by_label.return_value = None
        mock_db.update_agent_fields.return_value = True

        mock_redis = MagicMock()  # Non-None value so _get_redis() returns it
        mock_metadata_cache = AsyncMock()
        mock_metadata_cache.invalidate = AsyncMock()

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence._get_redis", return_value=mock_redis), \
             patch("src.mcp_handlers.shared.get_mcp_server", side_effect=Exception("no server")), \
             patch("src.cache.get_metadata_cache", return_value=mock_metadata_cache):
            result = await set_agent_label(test_uuid, "InvalidateTest")

        assert result is True
        mock_metadata_cache.invalidate.assert_called_once_with(test_uuid)

    @pytest.mark.asyncio
    async def test_redis_invalidation_exception_handled(self):
        """Redis metadata invalidation failure is swallowed (lines 681-682)."""
        from src.mcp_handlers.identity.handlers import set_agent_label

        test_uuid = "aaaabbbb-1111-2222-3333-444455556666"
        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.find_agent_by_label.return_value = None
        mock_db.update_agent_fields.return_value = True

        mock_redis = MagicMock()

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence._get_redis", return_value=mock_redis), \
             patch("src.mcp_handlers.shared.get_mcp_server", side_effect=Exception("no server")), \
             patch("src.cache.get_metadata_cache", side_effect=Exception("cache error")):
            result = await set_agent_label(test_uuid, "StillOK")

        assert result is True

    @pytest.mark.asyncio
    async def test_overall_exception_returns_false(self):
        """When the entire set_agent_label throws, returns False (lines 686-688)."""
        from src.mcp_handlers.identity.handlers import set_agent_label

        test_uuid = "aaaabbbb-1111-2222-3333-444455556666"

        with patch("src.mcp_handlers.identity.persistence.get_db", side_effect=Exception("Fatal DB error")):
            result = await set_agent_label(test_uuid, "WillFail")

        assert result is False

    @pytest.mark.asyncio
    async def test_set_label_with_session_key_calls_ensure_persisted(self):
        """set_agent_label with session_key calls ensure_agent_persisted."""
        from src.mcp_handlers.identity.handlers import set_agent_label

        test_uuid = "aaaabbbb-1111-2222-3333-444455556666"
        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        # First call from ensure_agent_persisted, second from set_agent_label
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.find_agent_by_label.return_value = None
        mock_db.update_agent_fields.return_value = True

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence._redis_cache", False), \
             patch("src.mcp_handlers.shared.get_mcp_server", side_effect=Exception("no server")):
            result = await set_agent_label(test_uuid, "PersistLabel", session_key="sess-key")

        assert result is True



# ============================================================================
# _cache_session - fallback bind and exception paths (lines 818-823)
# ============================================================================

class TestSetAgentLabelStructuredIdMigration:

    @pytest.mark.asyncio
    async def test_existing_agent_missing_structured_id_gets_migrated(self):
        """When existing cache entry has no structured_id, it attempts generation."""
        from src.mcp_handlers.identity.handlers import set_agent_label

        test_uuid = "aaaabbbb-1111-2222-3333-444455556666"
        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_identity.return_value = SimpleNamespace(identity_id="i1", metadata={})
        mock_db.find_agent_by_label.return_value = None
        mock_db.update_agent_fields.return_value = True

        mock_server = MagicMock()
        # Agent in cache but no structured_id (None)
        meta = SimpleNamespace(label=None, structured_id=None)
        # Ensure getattr returns None for structured_id
        mock_server.agent_metadata = {test_uuid: meta}

        with patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
             patch("src.mcp_handlers.identity.persistence._redis_cache", False), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server), \
             patch("src.mcp_handlers.support.naming_helpers.detect_interface_context", return_value={"type": "test"}), \
             patch("src.mcp_handlers.support.naming_helpers.generate_structured_id", return_value="migrated_id_1"), \
             patch("src.mcp_handlers.context.get_context_client_hint", return_value="cursor"):
            result = await set_agent_label(test_uuid, "MigrateLabel")

        assert result is True
        assert meta.label == "MigrateLabel"
        assert meta.structured_id == "migrated_id_1"




# TestIdentityAuditLogging removed 2026-04-17. It tested the
# _audit_identity_claim helper that existed solely to log name-claim
# events. With name-claim gone, the helper is gone too.


# ============================================================================
# Additional coverage: handle_identity_adapter structured_id regeneration (lines 1323-1345)
# ============================================================================

