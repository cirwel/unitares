"""
Tests for the sticky transport binding cache in identity_step.py.

The sticky cache prevents identity fragmentation for IP:UA fingerprint sessions
by reusing the first-resolved identity for all subsequent tool calls.
"""

import sys
import time
import asyncio
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.middleware import DispatchContext
from src.mcp_handlers.middleware.identity_step import (
    _transport_cache_key,
    _transport_identity_cache,
    TransportBinding,
    update_transport_binding,
    invalidate_transport_binding,
    _evict_stale_entries,
    _TRANSPORT_CACHE_TTL,
    _TRANSPORT_CACHE_MAX,
    resolve_identity,
)


# ============================================================================
# Helpers
# ============================================================================

@dataclass
class FakeSignals:
    """Minimal SessionSignals stand-in for testing."""
    mcp_session_id: Optional[str] = None
    x_session_id: Optional[str] = None
    x_client_id: Optional[str] = None
    oauth_client_id: Optional[str] = None
    ip_ua_fingerprint: Optional[str] = None
    user_agent: Optional[str] = None
    client_hint: Optional[str] = None
    x_agent_name: Optional[str] = None
    x_agent_id: Optional[str] = None
    # Default to "rest" so generic cache-mechanics tests still produce a sticky key
    # without mcp_session_id. MCP-specific tests must opt in by setting transport="mcp"
    # and providing mcp_session_id (the only combination allowed to cache for MCP).
    transport: str = "rest"


def _clear_cache():
    """Clear the module-level transport cache for test isolation."""
    _transport_identity_cache.clear()


@pytest.fixture(autouse=True)
def clean_cache():
    """Ensure each test starts with a clean cache."""
    _clear_cache()
    yield
    _clear_cache()


# ============================================================================
# 1. _transport_cache_key() unit tests
# ============================================================================

class TestTransportCacheKey:
    """Tests for _transport_cache_key()."""

    def test_returns_none_for_no_signals(self):
        assert _transport_cache_key(None) is None

    def test_returns_none_for_mcp_session_id(self):
        signals = FakeSignals(mcp_session_id="mcp-123")
        assert _transport_cache_key(signals) is None

    def test_returns_none_for_x_session_id(self):
        signals = FakeSignals(x_session_id="x-sess-456")
        assert _transport_cache_key(signals) is None

    def test_returns_none_for_x_client_id(self):
        signals = FakeSignals(x_client_id="client-789")
        assert _transport_cache_key(signals) is None

    def test_returns_none_for_oauth_client_id(self):
        signals = FakeSignals(oauth_client_id="oauth:abc")
        assert _transport_cache_key(signals) is None

    def test_returns_key_for_fingerprint_path_rest_transport(self):
        """REST callers without strong session signals still get fingerprint-only stickiness."""
        signals = FakeSignals(ip_ua_fingerprint="192.168.1.1:abc123", transport="rest")
        result = _transport_cache_key(signals)
        assert result == "sticky:192.168.1.1:abc123"

    def test_returns_none_for_no_fingerprint(self):
        signals = FakeSignals()
        assert _transport_cache_key(signals) is None

    def test_mcp_session_id_included_in_key(self):
        """mcp_session_id differentiates parallel MCP sessions from same host."""
        signals = FakeSignals(mcp_session_id="mcp-123", ip_ua_fingerprint="192.168.1.1:abc")
        assert _transport_cache_key(signals) == "sticky:192.168.1.1:abc:mcp-123"

    def test_mcp_transport_without_session_id_returns_none(self):
        """Regression: MCP clients without mcp_session_id MUST NOT collapse onto a
        fingerprint-only key. Two MCP processes on the same host (e.g. cron-launched
        Vigil + an interactive Hermes/Claude session) share IP:UA, so a
        fingerprint-only sticky key cross-binds their identities. Resolution: for
        MCP transport, no mcp_session_id means no sticky cache — fall through to
        identity resolution and let the agent onboard fresh.
        """
        signals = FakeSignals(ip_ua_fingerprint="192.168.1.1:abc", transport="mcp")
        assert _transport_cache_key(signals) is None

    def test_non_mcp_transports_still_use_fingerprint_fallback(self):
        """REST/SSE/stdio callers retain fingerprint-only stickiness — they don't
        share the MCP session-id signal and would otherwise lose all caching."""
        for transport in ("rest", "sse", "stdio", "unknown"):
            signals = FakeSignals(ip_ua_fingerprint="10.0.0.1:def", transport=transport)
            assert _transport_cache_key(signals) == "sticky:10.0.0.1:def", (
                f"transport={transport} should still get fingerprint stickiness"
            )


# ============================================================================
# 2. Cache management unit tests
# ============================================================================

class TestCacheManagement:
    """Tests for update_transport_binding, invalidate, and eviction."""

    def test_update_creates_binding(self):
        update_transport_binding("sticky:fp1", "uuid-111", "sk-111", "redis")
        assert "sticky:fp1" in _transport_identity_cache
        binding = _transport_identity_cache["sticky:fp1"]
        assert binding.agent_uuid == "uuid-111"
        assert binding.session_key == "sk-111"
        assert binding.source == "redis"

    def test_update_overwrites_existing(self):
        update_transport_binding("sticky:fp1", "uuid-111", "sk-111", "redis")
        update_transport_binding("sticky:fp1", "uuid-222", "sk-222", "bind_session")
        binding = _transport_identity_cache["sticky:fp1"]
        assert binding.agent_uuid == "uuid-222"
        assert binding.source == "bind_session"

    def test_invalidate_removes_binding(self):
        update_transport_binding("sticky:fp1", "uuid-111", "sk-111", "redis")
        invalidate_transport_binding("sticky:fp1")
        assert "sticky:fp1" not in _transport_identity_cache

    def test_invalidate_nonexistent_key_is_noop(self):
        invalidate_transport_binding("sticky:nonexistent")  # Should not raise

    def test_evict_stale_entries_by_ttl(self):
        """Entries older than TTL are evicted."""
        _transport_identity_cache["sticky:old"] = TransportBinding(
            agent_uuid="uuid-old",
            session_key="sk-old",
            bound_at=time.monotonic() - _TRANSPORT_CACHE_TTL - 100,
            source="test",
        )
        update_transport_binding("sticky:new", "uuid-new", "sk-new", "test")
        # Eviction happens inside update_transport_binding
        assert "sticky:old" not in _transport_identity_cache
        assert "sticky:new" in _transport_identity_cache

    def test_evict_max_size(self):
        """When cache exceeds max size, oldest entries are evicted."""
        base_time = time.monotonic()
        # Fill beyond max
        for i in range(_TRANSPORT_CACHE_MAX + 5):
            _transport_identity_cache[f"sticky:fp{i}"] = TransportBinding(
                agent_uuid=f"uuid-{i}",
                session_key=f"sk-{i}",
                bound_at=base_time + i * 0.001,  # Slightly increasing timestamps
                source="test",
            )
        _evict_stale_entries()
        assert len(_transport_identity_cache) <= _TRANSPORT_CACHE_MAX


# ============================================================================
# 3. resolve_identity() integration with sticky cache
# ============================================================================

class TestStickyResolveIdentity:
    """Integration tests for sticky cache in resolve_identity()."""

    @pytest.mark.asyncio
    async def test_cache_hit_reuses_identity(self):
        """When cache has a fresh binding, resolve_identity returns it without calling derive_session_key."""
        # Pre-populate cache
        update_transport_binding("sticky:192.168.1.1:abc", "uuid-cached", "sk-cached", "redis")

        signals = FakeSignals(ip_ua_fingerprint="192.168.1.1:abc")
        ctx = DispatchContext()

        with patch("src.mcp_handlers.context.get_session_signals", return_value=signals):
            with patch("src.mcp_handlers.context.set_session_context", return_value="tok") as mock_set:
                # derive_session_key should NOT be called
                with patch("src.mcp_handlers.identity.handlers.derive_session_key") as mock_derive:
                    with patch("src.mcp_handlers.identity.handlers._get_agent_status", new_callable=AsyncMock, return_value="active") as status_spy:
                        result = await resolve_identity("some_tool", {}, ctx)

                    name, args, out_ctx = result
                    assert out_ctx.bound_agent_id == "uuid-cached"
                    assert out_ctx.session_key == "sk-cached"
                    assert out_ctx.identity_result["source"] == "sticky_cache"
                    assert out_ctx.identity_result["core_agent_row_status"] == "active"
                    # derive_session_key was never called
                    mock_derive.assert_not_called()
                    status_spy.assert_awaited_once_with("uuid-cached")

    @pytest.mark.asyncio
    async def test_cache_hit_status_lookup_timeout_degrades_to_none(self):
        """Sticky-cache DB status lookup is bounded on the hot path."""
        update_transport_binding("sticky:192.168.1.1:abc", "uuid-cached", "sk-cached", "redis")

        async def _slow_status(_agent_uuid):
            await asyncio.sleep(0.05)
            return "archived"

        signals = FakeSignals(ip_ua_fingerprint="192.168.1.1:abc")
        ctx = DispatchContext()

        with patch("src.mcp_handlers.context.get_session_signals", return_value=signals), \
             patch("src.mcp_handlers.context.set_session_context", return_value="tok"), \
             patch("src.mcp_handlers.middleware.identity_step._REDIS_RECOVERY_TIMEOUT", 0.001), \
             patch("src.mcp_handlers.identity.handlers._get_agent_status", new=AsyncMock(side_effect=_slow_status)):
            _, args, out_ctx = await resolve_identity("some_tool", {}, ctx)

        assert out_ctx.bound_agent_id == "uuid-cached"
        assert out_ctx.identity_result["source"] == "sticky_cache"
        assert out_ctx.identity_result["core_agent_row_status"] is None
        assert args["_middleware_identity_result"]["core_agent_row_status"] is None

    @pytest.mark.asyncio
    async def test_cache_bypass_on_force_new(self):
        """force_new=True bypasses and invalidates the cache."""
        update_transport_binding("sticky:192.168.1.1:abc", "uuid-old", "sk-old", "redis")

        signals = FakeSignals(ip_ua_fingerprint="192.168.1.1:abc")
        ctx = DispatchContext()

        mock_identity = {
            "agent_uuid": "uuid-new",
            "created": True,
            "persisted": False,
            "source": "created",
        }

        with patch("src.mcp_handlers.context.get_session_signals", return_value=signals):
            with patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="sk-derived"):
                with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new_callable=AsyncMock, return_value=mock_identity):
                    with patch("src.mcp_handlers.context.set_session_context", return_value="tok"):
                        result = await resolve_identity("identity", {"force_new": True}, ctx)

                        _, _, out_ctx = result
                        assert out_ctx.bound_agent_id == "uuid-new"
                        # Old cache entry should be invalidated
                        assert "sticky:192.168.1.1:abc" not in _transport_identity_cache or \
                               _transport_identity_cache["sticky:192.168.1.1:abc"].agent_uuid == "uuid-new"

    @pytest.mark.asyncio
    async def test_cache_bypass_on_client_session_id(self):
        """Explicit client_session_id bypasses the cache."""
        update_transport_binding("sticky:192.168.1.1:abc", "uuid-cached", "sk-cached", "redis")

        signals = FakeSignals(ip_ua_fingerprint="192.168.1.1:abc")
        ctx = DispatchContext()

        mock_identity = {
            "agent_uuid": "uuid-explicit",
            "source": "redis",
        }

        with patch("src.mcp_handlers.context.get_session_signals", return_value=signals):
            with patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="sk-explicit"):
                with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new_callable=AsyncMock, return_value=mock_identity):
                    with patch("src.mcp_handlers.context.set_session_context", return_value="tok"):
                        result = await resolve_identity(
                            "process_agent_update",
                            {"client_session_id": "my-explicit-session"},
                            ctx,
                        )
                        _, _, out_ctx = result
                        # Should use the explicitly resolved identity, not the cached one
                        assert out_ctx.bound_agent_id == "uuid-explicit"

    @pytest.mark.asyncio
    async def test_agent_uuid_passthrough_skips_resolution(self):
        """agent_uuid in arguments bypasses resolve_session_identity entirely."""
        signals = FakeSignals(ip_ua_fingerprint="192.168.1.1:path0test")
        ctx = DispatchContext()

        with patch("src.mcp_handlers.context.get_session_signals", return_value=signals):
            with patch("src.mcp_handlers.middleware.identity_step._load_binding_from_redis", new_callable=AsyncMock, return_value=None):
                with patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="sk-derived"):
                    with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new_callable=AsyncMock) as mock_resolve:
                        with patch("src.mcp_handlers.context.set_session_context", return_value="tok"):
                            with patch("src.mcp_handlers.identity.handlers._get_agent_status", new_callable=AsyncMock, return_value="active") as status_spy:
                                result = await resolve_identity(
                                    "identity",
                                    {"agent_uuid": "e55caaf1-43a7-4fbb-a8fa-c69a9a8f50e4", "resume": True},
                                    ctx,
                                )
                            _, _, out_ctx = result
                            assert out_ctx.bound_agent_id == "e55caaf1-43a7-4fbb-a8fa-c69a9a8f50e4"
                            assert out_ctx.identity_result["source"] == "agent_uuid_passthrough"
                            assert out_ctx.identity_result["core_agent_row_status"] == "active"
                            # resolve_session_identity should NOT have been called
                            mock_resolve.assert_not_called()
                            status_spy.assert_awaited_once_with("e55caaf1-43a7-4fbb-a8fa-c69a9a8f50e4")

    @pytest.mark.asyncio
    async def test_agent_uuid_passthrough_threads_archived_core_status(self):
        """PATH 0 passthrough carries core row status for downstream auth."""
        signals = FakeSignals(ip_ua_fingerprint="192.168.1.1:path0arch")
        ctx = DispatchContext()
        uid = "e55caaf1-43a7-4fbb-a8fa-c69a9a8f50e4"
        args = {"agent_uuid": uid, "resume": True}

        with patch("src.mcp_handlers.context.get_session_signals", return_value=signals), \
             patch("src.mcp_handlers.middleware.identity_step._load_binding_from_redis", new_callable=AsyncMock, return_value=None), \
             patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="sk-derived"), \
             patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new_callable=AsyncMock) as mock_resolve, \
             patch("src.mcp_handlers.identity.handlers._get_agent_status", new_callable=AsyncMock, return_value="archived") as status_spy, \
             patch("src.mcp_handlers.context.set_session_context", return_value="tok"):
            _, out_args, out_ctx = await resolve_identity("identity", args, ctx)

        mock_resolve.assert_not_called()
        status_spy.assert_awaited_once_with(uid)
        assert out_ctx.identity_result["core_agent_row_status"] == "archived"
        assert out_args["_middleware_identity_result"]["core_agent_row_status"] == "archived"

    @pytest.mark.asyncio
    async def test_agent_uuid_passthrough_populates_sticky_cache(self):
        """agent_uuid passthrough should populate the sticky cache for future calls."""
        signals = FakeSignals(ip_ua_fingerprint="192.168.1.1:path0cache")
        ctx = DispatchContext()

        with patch("src.mcp_handlers.context.get_session_signals", return_value=signals):
            with patch("src.mcp_handlers.middleware.identity_step._load_binding_from_redis", new_callable=AsyncMock, return_value=None):
                with patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="sk-derived"):
                    with patch("src.mcp_handlers.context.set_session_context", return_value="tok"):
                        with patch("src.mcp_handlers.identity.handlers._get_agent_status", new_callable=AsyncMock, return_value=None) as status_spy:
                            await resolve_identity(
                                "identity",
                                {"agent_uuid": "e55caaf1-43a7-4fbb-a8fa-c69a9a8f50e4"},
                                ctx,
                            )

        # Sticky cache should have the UUID
        cache_key = "sticky:192.168.1.1:path0cache"
        assert cache_key in _transport_identity_cache
        assert _transport_identity_cache[cache_key].agent_uuid == "e55caaf1-43a7-4fbb-a8fa-c69a9a8f50e4"
        status_spy.assert_awaited_once_with("e55caaf1-43a7-4fbb-a8fa-c69a9a8f50e4")

    @pytest.mark.asyncio
    async def test_agent_uuid_passthrough_archived_status_rejected_by_auth(self):
        """PATH 0 archived row status must drive require_registered_agent."""
        from src.mcp_handlers.utils import require_registered_agent

        signals = FakeSignals(ip_ua_fingerprint="192.168.1.1:path0reject")
        ctx = DispatchContext()
        uid = "e55caaf1-43a7-4fbb-a8fa-c69a9a8f50e4"
        args = {"agent_uuid": uid, "resume": True}

        with patch("src.mcp_handlers.context.get_session_signals", return_value=signals), \
             patch("src.mcp_handlers.middleware.identity_step._load_binding_from_redis", new_callable=AsyncMock, return_value=None), \
             patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="sk-derived"), \
             patch("src.mcp_handlers.identity.handlers._get_agent_status", new_callable=AsyncMock, return_value="archived"), \
             patch("src.mcp_handlers.context.set_session_context", return_value="tok"):
            _, out_args, _ = await resolve_identity("identity", args, ctx)

        stale_meta = MagicMock()
        stale_meta.status = "active"
        stale_meta.label = "StaleActive"
        server = MagicMock()
        server.agent_metadata = {uid: stale_meta}
        server.ensure_metadata_loaded = MagicMock()

        out_args["agent_id"] = uid
        with patch("src.mcp_handlers.validators.validate_agent_id_format", return_value=(uid, None)), \
             patch("src.mcp_handlers.validators.validate_agent_id_reserved_names", return_value=(uid, None)), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
             patch("src.mcp_handlers.context.get_session_context", return_value={}), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=server):
            agent_uuid, error = require_registered_agent(out_args)

        assert agent_uuid is None
        assert "archived" in error.text.lower()

    @pytest.mark.asyncio
    async def test_agent_uuid_passthrough_only_for_identity_tools(self):
        """agent_uuid passthrough should NOT fire for non-identity tools."""
        signals = FakeSignals(ip_ua_fingerprint="192.168.1.1:path0other")
        ctx = DispatchContext()

        mock_identity = {"agent_uuid": "uuid-resolved", "source": "redis"}

        with patch("src.mcp_handlers.context.get_session_signals", return_value=signals):
            with patch("src.mcp_handlers.middleware.identity_step._load_binding_from_redis", new_callable=AsyncMock, return_value=None):
                with patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="sk-derived"):
                    with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new_callable=AsyncMock, return_value=mock_identity) as mock_resolve:
                        with patch("src.mcp_handlers.context.set_session_context", return_value="tok"):
                            result = await resolve_identity(
                                "process_agent_update",
                                {"agent_uuid": "e55caaf1-43a7-4fbb-a8fa-c69a9a8f50e4"},
                                ctx,
                            )
                            _, _, out_ctx = result
                            # Should have gone through normal resolution, not passthrough
                            mock_resolve.assert_called_once()
                            assert out_ctx.bound_agent_id == "uuid-resolved"

    @pytest.mark.asyncio
    async def test_cache_bypass_on_continuity_token(self):
        """Explicit continuity_token bypasses the cache."""
        update_transport_binding("sticky:192.168.1.1:abc", "uuid-cached", "sk-cached", "redis")

        signals = FakeSignals(ip_ua_fingerprint="192.168.1.1:abc")
        ctx = DispatchContext()

        mock_identity = {
            "agent_uuid": "uuid-token",
            "source": "continuity",
        }

        with patch("src.mcp_handlers.context.get_session_signals", return_value=signals):
            with patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="sk-token"):
                with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new_callable=AsyncMock, return_value=mock_identity):
                    with patch("src.mcp_handlers.context.set_session_context", return_value="tok"):
                        result = await resolve_identity(
                            "some_tool",
                            {"continuity_token": "ct-abc123"},
                            ctx,
                        )
                        _, _, out_ctx = result
                        assert out_ctx.bound_agent_id == "uuid-token"

    @pytest.mark.asyncio
    async def test_mcp_session_id_uses_sticky_cache(self):
        """mcp_session_id is included in sticky cache key to isolate parallel sessions."""
        signals = FakeSignals(mcp_session_id="mcp-volatile-123", ip_ua_fingerprint="192.168.1.1:abc")
        ctx = DispatchContext()

        mock_identity = {
            "agent_uuid": "uuid-mcp",
            "source": "redis",
        }

        expected_key = "sticky:192.168.1.1:abc:mcp-volatile-123"
        with patch("src.mcp_handlers.context.get_session_signals", return_value=signals):
            with patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="mcp:mcp-volatile-123"):
                with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new_callable=AsyncMock, return_value=mock_identity):
                    with patch("src.mcp_handlers.context.set_session_context", return_value="tok"):
                        result = await resolve_identity("some_tool", {}, ctx)
                        _, _, out_ctx = result
                        assert out_ctx._transport_key == expected_key
                        assert expected_key in _transport_identity_cache
                        binding = _transport_identity_cache[expected_key]
                        assert binding.agent_uuid == "uuid-mcp"

    @pytest.mark.asyncio
    async def test_ttl_expiry_falls_back_to_normal(self):
        """Expired cache entries are not used; normal resolution proceeds."""
        # Insert expired entry
        _transport_identity_cache["sticky:192.168.1.1:abc"] = TransportBinding(
            agent_uuid="uuid-expired",
            session_key="sk-expired",
            bound_at=time.monotonic() - _TRANSPORT_CACHE_TTL - 100,
            source="test",
        )

        signals = FakeSignals(ip_ua_fingerprint="192.168.1.1:abc")
        ctx = DispatchContext()

        mock_identity = {
            "agent_uuid": "uuid-fresh",
            "source": "postgres",
        }

        with patch("src.mcp_handlers.context.get_session_signals", return_value=signals):
            with patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="sk-fresh"):
                with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new_callable=AsyncMock, return_value=mock_identity):
                    with patch("src.mcp_handlers.context.set_session_context", return_value="tok"):
                        result = await resolve_identity("some_tool", {}, ctx)
                        _, _, out_ctx = result
                        assert out_ctx.bound_agent_id == "uuid-fresh"
                        # Cache should be refreshed with the new identity
                        binding = _transport_identity_cache.get("sticky:192.168.1.1:abc")
                        assert binding is not None
                        assert binding.agent_uuid == "uuid-fresh"

    @pytest.mark.asyncio
    async def test_normal_resolution_populates_cache(self):
        """After normal resolution (no cache hit), the cache is populated for next time."""
        signals = FakeSignals(ip_ua_fingerprint="192.168.1.1:new")
        ctx = DispatchContext()

        mock_identity = {
            "agent_uuid": "uuid-resolved",
            "source": "redis",
        }

        with patch("src.mcp_handlers.context.get_session_signals", return_value=signals):
            with patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="sk-resolved"):
                with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new_callable=AsyncMock, return_value=mock_identity):
                    with patch("src.mcp_handlers.context.set_session_context", return_value="tok"):
                        result = await resolve_identity("some_tool", {}, ctx)
                        _, _, out_ctx = result
                        assert out_ctx.bound_agent_id == "uuid-resolved"
                        # Cache should now contain the binding
                        binding = _transport_identity_cache.get("sticky:192.168.1.1:new")
                        assert binding is not None
                        assert binding.agent_uuid == "uuid-resolved"
                        assert binding.session_key == "sk-resolved"

    @pytest.mark.asyncio
    async def test_different_mcp_session_ids_get_separate_cache_keys(self):
        """Two calls with DIFFERENT mcp_session_id get different cache entries.

        Prevents parallel Claude Code sessions from converging to one UUID.
        Each MCP session ID produces a distinct sticky cache key.
        """
        fingerprint = "10.0.0.1:claude_ua"

        # --- Call 1: first mcp_session_id ---
        signals_1 = FakeSignals(mcp_session_id="mcp-aaa-111", ip_ua_fingerprint=fingerprint)
        ctx_1 = DispatchContext()
        mock_identity_1 = {"agent_uuid": "uuid-first", "source": "created"}

        with patch("src.mcp_handlers.context.get_session_signals", return_value=signals_1):
            with patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="mcp:mcp-aaa-111"):
                with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new_callable=AsyncMock, return_value=mock_identity_1):
                    with patch("src.mcp_handlers.context.set_session_context", return_value="tok"):
                        result_1 = await resolve_identity("tool_a", {}, ctx_1)
                        _, _, out_1 = result_1
                        assert out_1.bound_agent_id == "uuid-first"

        # --- Call 2: DIFFERENT mcp_session_id, same fingerprint ---
        # Should NOT hit the first call's cache — different mcp_session_id = different key
        signals_2 = FakeSignals(mcp_session_id="mcp-bbb-222", ip_ua_fingerprint=fingerprint)
        ctx_2 = DispatchContext()
        mock_identity_2 = {"agent_uuid": "uuid-second", "source": "created"}

        with patch("src.mcp_handlers.context.get_session_signals", return_value=signals_2):
            with patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="mcp:mcp-bbb-222"):
                with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new_callable=AsyncMock, return_value=mock_identity_2):
                    with patch("src.mcp_handlers.context.set_session_context", return_value="tok"):
                        result_2 = await resolve_identity("tool_b", {}, ctx_2)
                        _, _, out_2 = result_2
                        assert out_2.bound_agent_id == "uuid-second", (
                            f"Different mcp_session_id should get different identity, got {out_2.bound_agent_id}"
                        )
                        assert out_2._transport_key == f"sticky:{fingerprint}:mcp-bbb-222"

    @pytest.mark.asyncio
    async def test_same_mcp_session_id_hits_cache(self):
        """Same mcp_session_id on repeat call hits sticky cache (no re-resolution)."""
        fingerprint = "10.0.0.1:claude_ua"
        mcp_sid = "mcp-stable-999"

        # --- Call 1: populates cache ---
        signals_1 = FakeSignals(mcp_session_id=mcp_sid, ip_ua_fingerprint=fingerprint)
        ctx_1 = DispatchContext()
        mock_identity = {"agent_uuid": "uuid-cached", "source": "created"}

        with patch("src.mcp_handlers.context.get_session_signals", return_value=signals_1):
            with patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="mcp:stable"):
                with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new_callable=AsyncMock, return_value=mock_identity):
                    with patch("src.mcp_handlers.context.set_session_context", return_value="tok"):
                        await resolve_identity("tool_a", {}, ctx_1)

        # --- Call 2: same session ID → cache hit ---
        signals_2 = FakeSignals(mcp_session_id=mcp_sid, ip_ua_fingerprint=fingerprint)
        ctx_2 = DispatchContext()

        with patch("src.mcp_handlers.context.get_session_signals", return_value=signals_2):
            with patch("src.mcp_handlers.context.set_session_context", return_value="tok"):
                result_2 = await resolve_identity("tool_b", {}, ctx_2)
                _, _, out_2 = result_2
                assert out_2.bound_agent_id == "uuid-cached"


# ============================================================================
# 4. DispatchContext._transport_key field
# ============================================================================

class TestDispatchContextTransportKey:
    """Verify _transport_key field on DispatchContext."""

    def test_default_is_none(self):
        ctx = DispatchContext()
        assert ctx._transport_key is None

    def test_settable(self):
        ctx = DispatchContext(_transport_key="sticky:fp1")
        assert ctx._transport_key == "sticky:fp1"


# ============================================================================
# 5. REST path (_resolve_http_bound_agent) sticky cache integration
# ============================================================================

class TestStickyRESTPath:
    """REST /v1/tools/call previously bypassed the sticky cache and minted a
    fresh identity per call — source of the Apr 12-14 ~860-ghost spike.
    These tests pin the fix: REST now consults and populates the same cache.
    """

    @pytest.mark.asyncio
    async def test_rest_cache_hit_skips_resolution(self):
        """Cached fingerprint → REST returns cached UUID without calling resolve_session_identity."""
        from src.http_api import _resolve_http_bound_agent

        update_transport_binding("sticky:1.2.3.4:ua1", "uuid-cached-rest", "sk-cached", "rest")
        signals = FakeSignals(ip_ua_fingerprint="1.2.3.4:ua1")
        arguments: dict = {}

        with patch(
            "src.mcp_handlers.identity.handlers.resolve_session_identity",
            new_callable=AsyncMock,
        ) as mock_resolve:
            result = await _resolve_http_bound_agent("call_model", arguments, signals)

        assert result == "uuid-cached-rest"
        assert arguments["agent_id"] == "uuid-cached-rest"
        mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_rest_cache_hit_marks_session_resolution_source(self):
        """Cache-hit short-circuit must mark session_resolution_source.

        Regression for the REST process_agent_update identity-assurance
        mismatch surfaced during R6 H1 dogfood 2026-05-19: the cache-hit
        branch bound the right agent_uuid but never called
        ``set_session_resolution_source``. Downstream
        ``_compute_identity_assurance`` then read None and reported
        ``tier: weak / session_source: unknown`` even though the row
        landed under the correctly bound UUID. Response body and durable
        state disagreed about assurance tier — the kind of identity-
        honesty regression the 2026-04-17 rollout was meant to close.
        """
        from src.http_api import _resolve_http_bound_agent
        from src.mcp_handlers.context import get_session_resolution_source

        update_transport_binding("sticky:7.7.7.7:ua-hit", "uuid-cached", "sk", "rest")
        signals = FakeSignals(ip_ua_fingerprint="7.7.7.7:ua-hit")

        result = await _resolve_http_bound_agent("call_model", {}, signals)

        assert result == "uuid-cached"
        # The mark is the load-bearing assertion. Pre-fix, the cache-hit
        # branch returned without ever calling set_session_resolution_source,
        # leaving the contextvar at None — _compute_identity_assurance then
        # reported session_source="unknown".
        source = get_session_resolution_source()
        assert source == "sticky_transport_cache"

        # Tier intentionally stays weak: a cache hit means the caller
        # supplied no per-call proof. The 04-17 identity-honesty stance
        # is that per-call proof absence is weak even when the server's
        # own cache trusts the binding. The mark gives diagnostic
        # clarity (source identified) without claiming a stronger tier
        # than the caller proved. See the _MEDIUM_IDENTITY_SOURCES note
        # in phases.py for rationale.
        from src.mcp_handlers.updates.phases import _compute_identity_assurance
        assurance = _compute_identity_assurance(source, None)
        assert assurance["tier"] == "weak"
        assert assurance["session_source"] == "sticky_transport_cache"

    @pytest.mark.asyncio
    async def test_rest_cache_populated_on_miss(self):
        """First REST call for a fingerprint populates the cache so next call hits it."""
        from src.http_api import _resolve_http_bound_agent

        signals = FakeSignals(ip_ua_fingerprint="5.6.7.8:ua2")
        arguments: dict = {}

        mock_identity = {
            "agent_uuid": "uuid-rest-new",
            "created": False,  # existing agent found in Redis/PG
            "source": "postgres",
        }

        with patch(
            "src.mcp_handlers.identity.handlers.derive_session_key",
            new_callable=AsyncMock,
            return_value="sk-rest-new",
        ):
            with patch(
                "src.mcp_handlers.identity.handlers.resolve_session_identity",
                new_callable=AsyncMock,
                return_value=mock_identity,
            ):
                result = await _resolve_http_bound_agent("call_model", arguments, signals)

        assert result == "uuid-rest-new"
        # Cache now populated for this fingerprint
        assert "sticky:5.6.7.8:ua2" in _transport_identity_cache
        binding = _transport_identity_cache["sticky:5.6.7.8:ua2"]
        assert binding.agent_uuid == "uuid-rest-new"
        assert binding.source == "rest"

    @pytest.mark.asyncio
    async def test_rest_skip_tools_bypass_cache(self):
        """Identity-establishing tools (onboard, identity, etc.) skip the whole path."""
        from src.http_api import _resolve_http_bound_agent

        update_transport_binding("sticky:9.9.9.9:ua3", "uuid-should-not-see", "sk", "rest")
        signals = FakeSignals(ip_ua_fingerprint="9.9.9.9:ua3")

        # Skip tools return None immediately, no cache consultation
        result = await _resolve_http_bound_agent("onboard", {}, signals)
        assert result is None

        result = await _resolve_http_bound_agent("identity", {}, signals)
        assert result is None

    @pytest.mark.asyncio
    async def test_rest_explicit_agent_id_takes_precedence(self):
        """Explicit agent_id UUID wins over cache."""
        from src.http_api import _resolve_http_bound_agent

        update_transport_binding("sticky:11.22.33.44:ua4", "uuid-cached", "sk", "rest")
        signals = FakeSignals(ip_ua_fingerprint="11.22.33.44:ua4")
        explicit_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"  # valid UUID shape

        result = await _resolve_http_bound_agent(
            "call_model", {"agent_id": explicit_uuid}, signals
        )
        assert result == explicit_uuid

    @pytest.mark.asyncio
    async def test_rest_client_session_id_bypasses_cache(self):
        """Explicit client_session_id bypasses cache (mirrors MCP middleware behavior)."""
        from src.http_api import _resolve_http_bound_agent

        update_transport_binding("sticky:77.77.77.77:ua5", "uuid-cached", "sk", "rest")
        signals = FakeSignals(ip_ua_fingerprint="77.77.77.77:ua5")

        mock_identity = {"agent_uuid": "uuid-explicit-session", "created": False}
        with patch(
            "src.mcp_handlers.identity.handlers.derive_session_key",
            new_callable=AsyncMock,
            return_value="sk-explicit",
        ):
            with patch(
                "src.mcp_handlers.identity.handlers.resolve_session_identity",
                new_callable=AsyncMock,
                return_value=mock_identity,
            ):
                result = await _resolve_http_bound_agent(
                    "call_model",
                    {"client_session_id": "my-session"},
                    signals,
                )

        # Uses resolved identity, not cached
        assert result == "uuid-explicit-session"


# ============================================================================
# Redis recovery deadlock guard
# ============================================================================

class TestRedisRecoveryTimeoutGuard:
    """The middleware's Redis read is guarded by asyncio.wait_for so the
    documented anyio-asyncio deadlock (CLAUDE.md) degrades to a cold miss
    instead of hanging every subsequent MCP tool call."""

    @pytest.mark.asyncio
    async def test_redis_timeout_returns_none(self):
        """A stuck Redis read is bounded by the recovery timeout."""
        import asyncio as _asyncio
        from src.mcp_handlers.middleware import identity_step as step

        async def _hang(*_args, **_kwargs):
            await _asyncio.sleep(10)

        hung_redis = MagicMock()
        hung_redis.get = AsyncMock(side_effect=_hang)

        with patch("src.cache.redis_client.get_redis", new_callable=AsyncMock, return_value=hung_redis):
            with patch.object(step, "_REDIS_RECOVERY_TIMEOUT", 0.05):
                result = await step._load_binding_from_redis("sticky:deadlock-sim")

        assert result is None

    @pytest.mark.asyncio
    async def test_redis_success_returns_binding(self):
        """Fast Redis reads still hydrate the cache."""
        import json
        from src.mcp_handlers.middleware import identity_step as step

        fast_redis = MagicMock()
        fast_redis.get = AsyncMock(return_value=json.dumps({
            "agent_uuid": "uuid-redis-hit",
            "session_key": "sk-redis",
            "source": "redis_recovery",
        }))

        with patch("src.cache.redis_client.get_redis", new_callable=AsyncMock, return_value=fast_redis):
            result = await step._load_binding_from_redis("sticky:hot")

        assert result is not None
        assert result.agent_uuid == "uuid-redis-hit"
        assert step._transport_identity_cache["sticky:hot"].agent_uuid == "uuid-redis-hit"


# ============================================================================
# Startup warmup task
# ============================================================================

class TestTransportBindingCacheWarmup:
    """Startup warmup pre-populates the in-memory cache from Redis so the
    in-band Redis read almost never has to fire."""

    @pytest.mark.asyncio
    async def test_warmup_populates_cache(self, monkeypatch):
        import json
        from src.mcp_handlers.middleware import identity_step as step
        import src.background_tasks as bg_tasks

        fake_entries = {
            "transport_binding:sticky:10.0.0.1:ua-a": json.dumps({
                "agent_uuid": "uuid-a",
                "session_key": "sk-a",
                "source": "agent_uuid_passthrough",
            }),
            "transport_binding:sticky:10.0.0.2:ua-b": json.dumps({
                "agent_uuid": "uuid-b",
                "session_key": "sk-b",
                "source": "redis",
            }),
            "transport_binding:": json.dumps({  # malformed, should be skipped
                "agent_uuid": "uuid-bad",
                "session_key": "sk-bad",
            }),
        }

        async def _scan_iter(match=None, count=None):
            for k in fake_entries:
                yield k

        fake_redis = MagicMock()
        fake_redis.scan_iter = _scan_iter
        fake_redis.get = AsyncMock(side_effect=lambda k: fake_entries.get(k))

        # Skip the 2-second sleep at the top of the warmup
        async def _no_sleep(_):
            return None
        monkeypatch.setattr(bg_tasks.asyncio, "sleep", _no_sleep)
        monkeypatch.setattr("src.cache.is_redis_available", lambda: True)
        monkeypatch.setattr("src.cache.redis_client.get_redis", AsyncMock(return_value=fake_redis))

        await bg_tasks.transport_binding_cache_warmup()

        assert "sticky:10.0.0.1:ua-a" in step._transport_identity_cache
        assert "sticky:10.0.0.2:ua-b" in step._transport_identity_cache
        assert step._transport_identity_cache["sticky:10.0.0.1:ua-a"].agent_uuid == "uuid-a"
        # Malformed key (no suffix after prefix) was skipped
        assert "" not in step._transport_identity_cache

    @pytest.mark.asyncio
    async def test_warmup_skips_when_redis_unavailable(self, monkeypatch):
        from src.mcp_handlers.middleware import identity_step as step
        import src.background_tasks as bg_tasks

        async def _no_sleep(_):
            return None
        monkeypatch.setattr(bg_tasks.asyncio, "sleep", _no_sleep)
        monkeypatch.setattr("src.cache.is_redis_available", lambda: False)

        await bg_tasks.transport_binding_cache_warmup()

        assert step._transport_identity_cache == {}

    def test_populate_does_not_trigger_redis_write(self, monkeypatch):
        """populate_transport_binding_from_recovery must NOT call _persist_binding_to_redis —
        otherwise startup warmup would write every recovered entry back to Redis."""
        from src.mcp_handlers.middleware import identity_step as step

        called = {"persist": False}

        def _spy(*_args, **_kwargs):
            called["persist"] = True

        monkeypatch.setattr(step, "_persist_binding_to_redis", _spy)

        step.populate_transport_binding_from_recovery(
            "sticky:1.2.3.4:x", "uuid-warmed", "sk-warmed", source="warmup",
        )

        assert called["persist"] is False
        assert step._transport_identity_cache["sticky:1.2.3.4:x"].agent_uuid == "uuid-warmed"


# ============================================================================
# 7. Cross-process MCP siphoning regression
# ============================================================================

class TestMCPCrossProcessSiphoning:
    """Regression for the 2026-04-25 Vigil-siphoning incident: a Hermes/Claude
    MCP session's knowledge write attributed to launchd-cron Vigil's UUID
    because both used MCP transport from the same IP:UA without distinct
    mcp_session_id headers, collapsing onto a fingerprint-only sticky key.
    """

    @pytest.mark.asyncio
    async def test_mcp_without_session_id_does_not_inherit_prior_identity(self):
        """Two MCP processes from same host without mcp_session_id must NOT share
        identity. Pre-seeding the (would-be-buggy) fingerprint-only cache key
        must not affect a fresh MCP call — it should fall through to identity
        resolution and bind to the freshly-resolved UUID."""
        # Pre-seed the legacy fingerprint-only cache key with Vigil-like UUID,
        # mimicking the state left by an earlier Vigil cron run on this host.
        update_transport_binding(
            "sticky:127.0.0.1:hermes_ua",
            "e55caaf1-43a7-4fbb-a8fa-c69a9a8f50e4",
            "sk-vigil",
            "redis",
        )

        # New MCP request from same fingerprint, no mcp_session_id (the bug case).
        signals = FakeSignals(
            ip_ua_fingerprint="127.0.0.1:hermes_ua",
            transport="mcp",
            mcp_session_id=None,
        )
        ctx = DispatchContext()

        fresh_identity = {
            "agent_uuid": "11111111-1111-1111-1111-111111111111",
            "source": "created",
        }

        with patch("src.mcp_handlers.context.get_session_signals", return_value=signals), \
             patch("src.mcp_handlers.identity.handlers.derive_session_key",
                   new_callable=AsyncMock, return_value="sk-fresh"), \
             patch("src.mcp_handlers.identity.handlers.resolve_session_identity",
                   new_callable=AsyncMock, return_value=fresh_identity), \
             patch("src.mcp_handlers.context.set_session_context", return_value="tok"):
            _, _, out_ctx = await resolve_identity("knowledge", {}, ctx)

        assert out_ctx.bound_agent_id != "e55caaf1-43a7-4fbb-a8fa-c69a9a8f50e4", (
            "MCP request without mcp_session_id must NOT siphon a pre-seeded "
            "fingerprint-only sticky entry (the Vigil-attribution bug)"
        )
        assert out_ctx.bound_agent_id == "11111111-1111-1111-1111-111111111111"
        assert out_ctx.identity_result.get("source") != "sticky_cache"
