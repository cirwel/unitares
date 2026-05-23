#!/usr/bin/env python3
"""
Tests for onboard session pinning (attribution fix).

Verifies the Feb 2026 fix for knowledge graph attribution fragmentation:
when Claude.ai doesn't echo client_session_id, dispatch_tool() should
inject it from a Redis pin set during onboard().

Tests cover:
1. _extract_base_fingerprint() — pure function, various session key formats
2. ua_hash_from_header() — canonical UA hash computation (single source of truth)
3. set_onboard_pin() / lookup_onboard_pin() — shared pin operations via Redis
4. Pin-setting in onboard handler — Redis key created with correct TTL
5. Pin-lookup in dispatch_tool — client_session_id injected when missing
6. REST ↔ MCP fingerprint consistency — no divergence between paths
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestExtractBaseFingerprint:
    """Unit tests for _extract_base_fingerprint().

    The function extracts the UA hash only (not IP) because Claude.ai's
    proxy pool rotates IPs per request while the UA string stays stable.
    """

    def test_ip_ua_hash_two_parts(self):
        """Standard HTTP fingerprint: IP:UA_hash → extract UA hash only."""
        from src.mcp_handlers.identity.handlers import _extract_base_fingerprint

        result = _extract_base_fingerprint("34.162.136.91:abc123")
        assert result == "ua:abc123"

    def test_ip_ua_hash_with_random_suffix(self):
        """HTTP fingerprint with random suffix: IP:UA_hash:random → UA hash only."""
        from src.mcp_handlers.identity.handlers import _extract_base_fingerprint

        result = _extract_base_fingerprint("34.162.136.91:abc123:deadbeef")
        assert result == "ua:abc123"

    def test_ip_ua_hash_multiple_suffixes(self):
        """Multiple suffixes should all be stripped, only UA hash kept."""
        from src.mcp_handlers.identity.handlers import _extract_base_fingerprint

        result = _extract_base_fingerprint("34.162.136.91:abc123:dead:beef")
        assert result == "ua:abc123"

    def test_different_ips_same_ua_hash_match(self):
        """CRITICAL: Different IPs with same UA hash must resolve to same pin.

        This is the core fix for Claude.ai attribution fragmentation —
        the proxy pool rotates IPs but the UA string is stable.
        """
        from src.mcp_handlers.identity.handlers import _extract_base_fingerprint

        fp1 = _extract_base_fingerprint("160.79.106.108:d20c2f")
        fp2 = _extract_base_fingerprint("160.79.106.126:d20c2f")
        fp3 = _extract_base_fingerprint("34.162.136.91:d20c2f:deadbeef")
        assert fp1 == fp2 == fp3 == "ua:d20c2f"

    def test_mcp_session_key_returns_none(self):
        """MCP session keys already have stable identity — skip pinning."""
        from src.mcp_handlers.identity.handlers import _extract_base_fingerprint

        assert _extract_base_fingerprint("mcp:some-session-id") is None

    def test_stdio_key_returns_none(self):
        """stdio keys are stable by default — skip pinning."""
        from src.mcp_handlers.identity.handlers import _extract_base_fingerprint

        assert _extract_base_fingerprint("stdio:12345") is None

    def test_agent_session_id_returns_none(self):
        """agent- keys already provide stable identity — skip pinning."""
        from src.mcp_handlers.identity.handlers import _extract_base_fingerprint

        assert _extract_base_fingerprint("agent-5e728ecb1234") is None

    def test_empty_string_returns_none(self):
        from src.mcp_handlers.identity.handlers import _extract_base_fingerprint

        assert _extract_base_fingerprint("") is None

    def test_none_returns_none(self):
        from src.mcp_handlers.identity.handlers import _extract_base_fingerprint

        assert _extract_base_fingerprint(None) is None

    def test_single_part_returns_as_is(self):
        """Single-part key (unusual) should be returned as-is."""
        from src.mcp_handlers.identity.handlers import _extract_base_fingerprint

        assert _extract_base_fingerprint("somekey") == "somekey"


class TestUaHashFromHeader:
    """Tests for ua_hash_from_header() — the canonical UA hash computation.

    This function is the SINGLE SOURCE OF TRUTH for computing UA hashes.
    Both REST (from raw User-Agent header) and MCP (from session key parts[1])
    paths must produce the same hash for the same User-Agent string.
    """

    def test_basic_ua_string(self):
        """Standard User-Agent string should produce a ua: prefixed hash."""
        from src.mcp_handlers.identity.handlers import ua_hash_from_header

        result = ua_hash_from_header("Mozilla/5.0 (compatible; Claude/1.0)")
        assert result is not None
        assert result.startswith("ua:")
        assert len(result) == 9  # "ua:" + 6 hex chars

    def test_empty_string_returns_none(self):
        from src.mcp_handlers.identity.handlers import ua_hash_from_header
        assert ua_hash_from_header("") is None

    def test_none_returns_none(self):
        from src.mcp_handlers.identity.handlers import ua_hash_from_header
        assert ua_hash_from_header(None) is None

    def test_same_ua_produces_same_hash(self):
        """Deterministic: same input → same output."""
        from src.mcp_handlers.identity.handlers import ua_hash_from_header

        ua = "python-httpx/0.27.0"
        assert ua_hash_from_header(ua) == ua_hash_from_header(ua)

    def test_different_ua_produces_different_hash(self):
        """Different User-Agent strings should produce different hashes."""
        from src.mcp_handlers.identity.handlers import ua_hash_from_header

        h1 = ua_hash_from_header("Mozilla/5.0 Chrome/120")
        h2 = ua_hash_from_header("python-httpx/0.27.0")
        assert h1 != h2

    def test_rest_and_mcp_paths_produce_same_pin_key(self):
        """CRITICAL: REST computes hash from raw UA header, MCP extracts from
        session key. Both must resolve to the SAME Redis pin key.

        This test verifies the contract that prevents fingerprint divergence.
        """
        import hashlib
        from src.mcp_handlers.identity.handlers import ua_hash_from_header, _extract_base_fingerprint

        # The actual User-Agent string as seen in HTTP headers
        raw_ua = "python-httpx/0.27.0"

        # REST path: compute directly from User-Agent header
        rest_fp = ua_hash_from_header(raw_ua)

        # MCP path: the ASGI middleware computes md5(ua)[:6] and puts it in
        # the session key as IP:UA_hash. _extract_base_fingerprint gets parts[1].
        ua_hash_in_session_key = hashlib.md5(raw_ua.encode()).hexdigest()[:6]
        mcp_session_key = f"34.162.136.91:{ua_hash_in_session_key}"
        mcp_fp = _extract_base_fingerprint(mcp_session_key)

        # Both MUST produce the same fingerprint → same Redis pin key
        assert rest_fp == mcp_fp, (
            f"REST/MCP fingerprint divergence! REST={rest_fp}, MCP={mcp_fp}. "
            f"This would cause pin misses across transport paths."
        )


class TestSharedPinOperations:
    """Tests for the consolidated set_onboard_pin() and lookup_onboard_pin().

    These shared functions replace the duplicated inline Redis logic that
    was previously in 3 separate locations (onboard setter, MCP dispatcher,
    REST path).
    """

    @pytest.mark.asyncio
    async def test_set_and_lookup_roundtrip(self):
        """Pin set by set_onboard_pin() should be retrievable by lookup_onboard_pin()."""
        from src.mcp_handlers.identity.handlers import set_onboard_pin, lookup_onboard_pin

        agent_uuid = "7f7d20a3-1234-5678-9abc-def012345678"
        stable_session_id = f"agent-{agent_uuid[:12]}"

        mock_redis = AsyncMock()
        stored_data = {}

        async def mock_setex(key, ttl, data):
            stored_data[key] = data

        async def mock_get(key):
            return stored_data.get(key)

        mock_redis.setex = mock_setex
        mock_redis.get = mock_get
        mock_redis.expire = AsyncMock()

        async def get_redis_mock():
            return mock_redis

        with patch("src.cache.redis_client.get_redis", get_redis_mock):
            # Set the pin
            result = await set_onboard_pin("ua:abc123", agent_uuid, stable_session_id)
            assert result is True

            # Look it up
            found = await lookup_onboard_pin("ua:abc123")
            assert found == stable_session_id

    @pytest.mark.asyncio
    async def test_set_pin_with_none_fingerprint(self):
        """set_onboard_pin() with None fingerprint should return False."""
        from src.mcp_handlers.identity.handlers import set_onboard_pin

        result = await set_onboard_pin(None, "some-uuid", "agent-12345")
        assert result is False

    @pytest.mark.asyncio
    async def test_lookup_pin_with_none_fingerprint(self):
        """lookup_onboard_pin() with None fingerprint should return None."""
        from src.mcp_handlers.identity.handlers import lookup_onboard_pin

        result = await lookup_onboard_pin(None)
        assert result is None

    @pytest.mark.asyncio
    async def test_lookup_miss_returns_none(self):
        """lookup_onboard_pin() returns None when no pin exists."""
        from src.mcp_handlers.identity.handlers import lookup_onboard_pin

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)

        async def get_redis_mock():
            return mock_redis

        with patch("src.cache.redis_client.get_redis", get_redis_mock):
            result = await lookup_onboard_pin("ua:nonexistent")
            assert result is None

    @pytest.mark.asyncio
    async def test_lookup_refreshes_ttl_by_default(self):
        """lookup_onboard_pin() should refresh TTL on successful lookup."""
        from src.mcp_handlers.identity.handlers import lookup_onboard_pin, _PIN_TTL

        pin_data = json.dumps({
            "agent_uuid": "test-uuid",
            "client_session_id": "agent-test123",
        })

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=pin_data.encode())
        mock_redis.expire = AsyncMock()

        async def get_redis_mock():
            return mock_redis

        with patch("src.cache.redis_client.get_redis", get_redis_mock):
            result = await lookup_onboard_pin("ua:abc123")
            assert result == "agent-test123"
            mock_redis.expire.assert_called_once_with("recent_onboard:ua:abc123", _PIN_TTL)

    @pytest.mark.asyncio
    async def test_lookup_skip_ttl_refresh(self):
        """lookup_onboard_pin(refresh_ttl=False) should NOT refresh TTL."""
        from src.mcp_handlers.identity.handlers import lookup_onboard_pin

        pin_data = json.dumps({
            "agent_uuid": "test-uuid",
            "client_session_id": "agent-test123",
        })

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=pin_data.encode())
        mock_redis.expire = AsyncMock()

        async def get_redis_mock():
            return mock_redis

        with patch("src.cache.redis_client.get_redis", get_redis_mock):
            result = await lookup_onboard_pin("ua:abc123", refresh_ttl=False)
            assert result == "agent-test123"
            mock_redis.expire.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_pin_redis_unavailable(self):
        """set_onboard_pin() should return False when Redis is unavailable."""
        from src.mcp_handlers.identity.handlers import set_onboard_pin

        async def get_redis_mock():
            return None

        with patch("src.cache.redis_client.get_redis", get_redis_mock):
            result = await set_onboard_pin("ua:abc123", "test-uuid", "agent-test123")
            assert result is False

    @pytest.mark.asyncio
    async def test_lookup_pin_redis_unavailable(self):
        """lookup_onboard_pin() should return None when Redis is unavailable."""
        from src.mcp_handlers.identity.handlers import lookup_onboard_pin

        async def get_redis_mock():
            return None

        with patch("src.cache.redis_client.get_redis", get_redis_mock):
            result = await lookup_onboard_pin("ua:abc123")
            assert result is None

    @pytest.mark.asyncio
    async def test_lookup_pin_redis_hang_degrades_to_none(self):
        """lookup_onboard_pin() must time-bound redis under anyio task-group
        deadlock; on timeout, return None rather than hanging the MCP pipeline."""
        import asyncio
        from src.mcp_handlers.identity.handlers import lookup_onboard_pin

        # Simulate a redis client whose .get() never resolves (the anyio-asyncio
        # deadlock signature). The wait_for guard must short-circuit.
        mock_redis = MagicMock()

        async def hung_get(key):
            await asyncio.sleep(10)  # well past the timeout

        mock_redis.get = hung_get

        async def get_redis_mock():
            return mock_redis

        with patch("src.cache.redis_client.get_redis", get_redis_mock), \
             patch("src.mcp_handlers.identity.session._PIN_REDIS_TIMEOUT", 0.05):
            start = asyncio.get_event_loop().time()
            result = await lookup_onboard_pin("ua:hung")
            elapsed = asyncio.get_event_loop().time() - start
            assert result is None
            assert elapsed < 1.0  # must NOT have waited the full sleep

    @pytest.mark.asyncio
    async def test_set_pin_redis_hang_degrades_to_false(self):
        """set_onboard_pin() must time-bound redis; on timeout, return False
        and let onboard succeed without a pin."""
        import asyncio
        from src.mcp_handlers.identity.handlers import set_onboard_pin

        mock_redis = MagicMock()

        async def hung_setex(key, ttl, data):
            await asyncio.sleep(10)

        mock_redis.setex = hung_setex

        async def get_redis_mock():
            return mock_redis

        with patch("src.cache.redis_client.get_redis", get_redis_mock), \
             patch("src.mcp_handlers.identity.session._PIN_REDIS_TIMEOUT", 0.05):
            start = asyncio.get_event_loop().time()
            result = await set_onboard_pin("ua:hung", "test-uuid", "agent-test123")
            elapsed = asyncio.get_event_loop().time() - start
            assert result is False
            assert elapsed < 1.0

    @pytest.mark.asyncio
    async def test_set_pin_correct_ttl(self):
        """set_onboard_pin() should use _PIN_TTL constant (1800s)."""
        from src.mcp_handlers.identity.handlers import set_onboard_pin, _PIN_TTL

        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock()

        async def get_redis_mock():
            return mock_redis

        with patch("src.cache.redis_client.get_redis", get_redis_mock):
            await set_onboard_pin("ua:abc123", "test-uuid", "agent-test123")
            call_args = mock_redis.setex.call_args
            assert call_args[0][0] == "recent_onboard:ua:abc123"
            assert call_args[0][1] == _PIN_TTL
            # Verify the stored data
            stored = json.loads(call_args[0][2])
            assert stored["agent_uuid"] == "test-uuid"
            assert stored["client_session_id"] == "agent-test123"

    @pytest.mark.asyncio
    async def test_set_pin_scoped_key_for_model_and_client(self):
        """Scoped pin should include client+model and skip unscoped fallback."""
        from src.mcp_handlers.identity.handlers import set_onboard_pin

        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock()

        async def get_redis_mock():
            return mock_redis

        with patch("src.cache.redis_client.get_redis", get_redis_mock):
            ok = await set_onboard_pin(
                "ua:abc123",
                "test-uuid",
                "agent-test123",
                client_hint="chatgpt",
                model_type="gpt-5-codex",
            )

        assert ok is True
        keys = [call.args[0] for call in mock_redis.setex.call_args_list]
        assert "recent_onboard:ua:abc123|chatgpt|gpt" in keys
        assert "recent_onboard:ua:abc123" not in keys

    @pytest.mark.asyncio
    async def test_lookup_scoped_key_roundtrip(self):
        """Scoped key lookup should resolve the pinned session id."""
        from src.mcp_handlers.identity.handlers import set_onboard_pin, lookup_onboard_pin

        mock_redis = AsyncMock()
        storage = {}

        async def mock_setex(key, ttl, data):
            storage[key] = data

        async def mock_get(key):
            return storage.get(key)

        mock_redis.setex = mock_setex
        mock_redis.get = mock_get
        mock_redis.expire = AsyncMock()

        async def get_redis_mock():
            return mock_redis

        with patch("src.cache.redis_client.get_redis", get_redis_mock):
            await set_onboard_pin(
                "ua:abc123",
                "test-uuid",
                "agent-test123",
                client_hint="chatgpt",
                model_type="gpt-5-codex",
            )
            found = await lookup_onboard_pin("ua:abc123|chatgpt|gpt")

        assert found == "agent-test123"


class TestOnboardPinSetting:
    """Tests that onboard() sets the Redis pin correctly via set_onboard_pin()."""

    @pytest.mark.asyncio
    async def test_onboard_sets_redis_pin(self):
        """After onboard, a recent_onboard:{base_fp} Redis key should be set."""
        from src.mcp_handlers.identity.handlers import _extract_base_fingerprint, set_onboard_pin

        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock()

        base_session_key = "34.162.136.91:abc123"
        agent_uuid = "7f7d20a3-1234-5678-9abc-def012345678"
        stable_session_id = f"agent-{agent_uuid[:12]}"

        base_fp = _extract_base_fingerprint(base_session_key)
        assert base_fp == "ua:abc123"

        async def get_redis_mock():
            return mock_redis

        # Use the shared function (same as onboard handler now does)
        with patch("src.cache.redis_client.get_redis", get_redis_mock):
            result = await set_onboard_pin(base_fp, agent_uuid, stable_session_id)
            assert result is True

        mock_redis.setex.assert_called_once()
        call_args = mock_redis.setex.call_args
        assert call_args[0][0] == "recent_onboard:ua:abc123"

    def test_pin_not_set_for_stdio(self):
        """stdio transports should not set a pin."""
        from src.mcp_handlers.identity.handlers import _extract_base_fingerprint

        base_fp = _extract_base_fingerprint("stdio:12345")
        assert base_fp is None  # No pin should be set


class TestDispatchPinLookup:
    """Tests for pin-lookup injection in dispatch_tool() via lookup_onboard_pin()."""

    @pytest.mark.asyncio
    async def test_pin_injects_client_session_id(self):
        """When no client_session_id in args, pin should inject one."""
        from src.mcp_handlers.identity.handlers import _extract_base_fingerprint, lookup_onboard_pin

        agent_uuid = "7f7d20a3-1234-5678-9abc-def012345678"
        stable_session_id = f"agent-{agent_uuid[:12]}"

        pin_data = json.dumps({
            "agent_uuid": agent_uuid,
            "client_session_id": stable_session_id,
        })

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=pin_data.encode())
        mock_redis.expire = AsyncMock()

        arguments = {}  # No client_session_id
        request_state_id = "34.162.136.91:abc123"

        assert arguments.get("client_session_id") is None

        # Use the shared lookup (same as dispatch_tool now does)
        base_fp = _extract_base_fingerprint(request_state_id)
        assert base_fp == "ua:abc123"

        async def get_redis_mock():
            return mock_redis

        with patch("src.cache.redis_client.get_redis", get_redis_mock):
            pinned_session_id = await lookup_onboard_pin(base_fp)

        assert pinned_session_id == stable_session_id
        arguments["client_session_id"] = pinned_session_id
        assert arguments["client_session_id"] == stable_session_id

    @pytest.mark.asyncio
    async def test_pin_not_used_when_client_session_id_present(self):
        """If client_session_id is already in args, pin should be skipped."""
        arguments = {"client_session_id": "agent-existing123"}
        client_session_id = arguments.get("client_session_id")

        # Pin lookup should NOT happen when client_session_id exists
        assert client_session_id is not None
        # In dispatch_tool, the condition is: if not client_session_id and request_state_id
        # So this path is skipped entirely

    @pytest.mark.asyncio
    async def test_pin_miss_falls_through(self):
        """When no pin exists in Redis, arguments should remain unchanged."""
        from src.mcp_handlers.identity.handlers import _extract_base_fingerprint, lookup_onboard_pin

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)

        arguments = {}
        request_state_id = "34.162.136.91:abc123"

        base_fp = _extract_base_fingerprint(request_state_id)

        async def get_redis_mock():
            return mock_redis

        with patch("src.cache.redis_client.get_redis", get_redis_mock):
            result = await lookup_onboard_pin(base_fp)

        assert result is None
        assert "client_session_id" not in arguments

    @pytest.mark.asyncio
    async def test_pin_lookup_with_random_suffix_still_matches(self):
        """Transport key with random suffix should match pin set with base key."""
        from src.mcp_handlers.identity.handlers import _extract_base_fingerprint

        onboard_key = "34.162.136.91:abc123"
        base_fp_onboard = _extract_base_fingerprint(onboard_key)

        dispatch_key = "34.162.136.91:abc123:deadbeef"
        base_fp_dispatch = _extract_base_fingerprint(dispatch_key)

        assert base_fp_onboard == base_fp_dispatch
        assert base_fp_onboard == "ua:abc123"

    @pytest.mark.asyncio
    async def test_pin_lookup_with_different_ip_still_matches(self):
        """CRITICAL: Different IPs (Claude.ai proxy rotation) must match same pin.

        This is the core scenario: onboard happens from IP .108, subsequent
        calls come from .126, .91, etc. — but all have same UA hash.
        """
        from src.mcp_handlers.identity.handlers import _extract_base_fingerprint

        onboard_key = "160.79.106.108:d20c2f:claude"
        base_fp_onboard = _extract_base_fingerprint(onboard_key)

        dispatch_keys = [
            "160.79.106.126:d20c2f",
            "160.79.106.200:d20c2f:deadbeef",
            "34.162.136.91:d20c2f:a1b2c3d4",
        ]
        for key in dispatch_keys:
            base_fp_dispatch = _extract_base_fingerprint(key)
            assert base_fp_dispatch == base_fp_onboard, (
                f"Fingerprint mismatch: onboard={onboard_key!r} -> {base_fp_onboard!r}, "
                f"dispatch={key!r} -> {base_fp_dispatch!r}"
            )

        assert base_fp_onboard == "ua:d20c2f"


class TestSkillMdResource:
    """Tests for SKILL.md MCP resource deployment."""

    def test_skill_file_exists(self):
        """SKILL.md should exist in the expected location."""
        from pathlib import Path

        skill_path = Path(__file__).parent.parent / "skills" / "unitares-governance" / "SKILL.md"
        assert skill_path.exists(), f"SKILL.md not found at {skill_path}"

    def test_skill_file_has_frontmatter(self):
        """SKILL.md should have YAML frontmatter."""
        from pathlib import Path

        skill_path = Path(__file__).parent.parent / "skills" / "unitares-governance" / "SKILL.md"
        content = skill_path.read_text()
        assert content.startswith("---"), "SKILL.md should start with YAML frontmatter"
        assert "name: unitares-governance" in content

    def test_skill_file_covers_key_concepts(self):
        """SKILL.md should cover essential framework concepts."""
        from pathlib import Path

        skill_path = Path(__file__).parent.parent / "skills" / "unitares-governance" / "SKILL.md"
        content = skill_path.read_text()

        # Key concepts that must be present
        assert "EISV" in content, "SKILL.md should explain the EISV model"
        assert "onboard()" in content, "SKILL.md should reference onboard tool"
        assert "process_agent_update()" in content, "SKILL.md should reference check-in tool"
        assert "client_session_id" in content, "SKILL.md should explain session continuity"
        assert "knowledge graph" in content.lower(), "SKILL.md should mention knowledge graph"


class TestToolSchemaClientSessionId:
    """Verify that critical tool schemas include client_session_id.

    Claude.ai only sends parameters that are in a tool's inputSchema.
    If client_session_id is missing from the schema, the agent won't
    send it, causing attribution fragmentation across identity UUIDs.
    """

    @pytest.fixture
    def tool_schemas(self):
        """Load all tool schemas from tool_schemas.py."""
        from src.tool_schemas import get_tool_definitions
        tools = get_tool_definitions(verbosity="full")
        return {t.name: t for t in tools}

    # Critical agent-facing tools that MUST have client_session_id
    CRITICAL_TOOLS = [
        "knowledge",
        "agent",
        "calibration",
        "cirs_protocol",
        "self_recovery",
        "health_check",
        "search_knowledge_graph",
        "leave_note",
        "process_agent_update",
        "get_governance_metrics",
        "onboard",
        "identity",
        "dialectic",
        "submit_thesis",
        "submit_antithesis",
        "submit_synthesis",
        "request_dialectic_review",
    ]

    @pytest.mark.parametrize("tool_name", CRITICAL_TOOLS)
    def test_critical_tool_has_client_session_id(self, tool_schemas, tool_name):
        """Each critical tool schema must include client_session_id in its properties."""
        assert tool_name in tool_schemas, f"Tool '{tool_name}' not found in TOOL_SCHEMAS"
        tool = tool_schemas[tool_name]
        input_schema = tool.inputSchema or {}
        props = input_schema.get("properties", {})
        assert "client_session_id" in props, (
            f"Tool '{tool_name}' is missing client_session_id in inputSchema.properties. "
            f"Claude.ai agents won't send it, causing attribution fragmentation."
        )

    @pytest.mark.parametrize("tool_name", CRITICAL_TOOLS)
    def test_client_session_id_is_string_type(self, tool_schemas, tool_name):
        """client_session_id should be typed as string (or anyOf including string)."""
        tool = tool_schemas[tool_name]
        input_schema = tool.inputSchema or {}
        props = input_schema.get("properties", {})
        if "client_session_id" in props:
            csid = props["client_session_id"]
            # Accept both plain {"type": "string"} and Pydantic's
            # {"anyOf": [{"type": "string"}, {"type": "null"}]}
            is_string = csid.get("type") == "string"
            if not is_string and "anyOf" in csid:
                is_string = any(
                    alt.get("type") == "string" for alt in csid["anyOf"]
                )
            assert is_string, (
                f"Tool '{tool_name}': client_session_id should accept type 'string'"
            )

    @pytest.mark.parametrize("tool_name", CRITICAL_TOOLS)
    def test_critical_tool_has_continuity_token(self, tool_schemas, tool_name):
        """Critical tools expose continuity_token for PATH 0 ownership proof."""
        assert tool_name in tool_schemas, f"Tool '{tool_name}' not found in TOOL_SCHEMAS"
        tool = tool_schemas[tool_name]
        input_schema = tool.inputSchema or {}
        props = input_schema.get("properties", {})
        assert "continuity_token" in props, (
            f"Tool '{tool_name}' is missing continuity_token in inputSchema.properties."
        )

    def test_no_critical_tool_missing(self, tool_schemas):
        """Sanity check: all critical tools exist in the schema registry."""
        missing = [t for t in self.CRITICAL_TOOLS if t not in tool_schemas]
        assert not missing, f"Critical tools missing from schema registry: {missing}"

    def test_unified_tools_have_client_session_id(self, tool_schemas):
        """Unified/consolidated tools added in the schema fix should all have client_session_id."""
        unified_tools_added = [
            "knowledge",
            "agent",
            "calibration",
            "cirs_protocol",
            "self_recovery",
            "health_check",
            "search_knowledge_graph",
            "get_discovery_details",
            "list_knowledge_graph",
            "dialectic",
            "submit_thesis",
            "submit_antithesis",
            "submit_synthesis",
            "get_workspace_health",
            "get_server_info",
            "get_connection_status",
            "list_tools",
            "describe_tool",
            "list_agents",
            "detect_anomalies",
            "cleanup_knowledge_graph",
            "get_lifecycle_stats",
        ]
        missing = []
        for tool_name in unified_tools_added:
            if tool_name not in tool_schemas:
                continue  # Tool may have been renamed/removed
            tool = tool_schemas[tool_name]
            input_schema = tool.inputSchema or {}
            props = input_schema.get("properties", {})
            if "client_session_id" not in props:
                missing.append(tool_name)
        assert not missing, (
            f"Unified tools missing client_session_id (regression): {missing}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
