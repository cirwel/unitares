"""
Tests for src/mcp_handlers/identity_shared.py

Covers:
- UUID prefix index (register, lookup, collision)
- Session key derivation (_get_session_key precedence)
- make_client_session_id formatting/validation
- get_bound_agent_id (contextvar priority, fallback)
- is_session_bound
- require_write_permission (bound vs unbound)
- _get_identity_record_sync (in-memory cache, agent-prefix resolution)
- Lineage helpers (_get_lineage, _get_lineage_depth)
"""

import os
import pytest
from unittest.mock import patch, MagicMock

from src.mcp_handlers.identity.shared import (
    _register_uuid_prefix,
    _lookup_uuid_by_prefix,
    _uuid_prefix_index,
    _session_identities,
    _bind_fingerprints,
    _get_session_key,
    make_client_session_id,
    get_bound_agent_id,
    is_session_bound,
    require_write_permission,
    _get_identity_record_sync,
    _get_lineage,
)


@pytest.fixture(autouse=True)
def clean_shared_state():
    """Clear shared dicts before/after each test."""
    _uuid_prefix_index.clear()
    _session_identities.clear()
    _bind_fingerprints.clear()
    yield
    _uuid_prefix_index.clear()
    _session_identities.clear()
    _bind_fingerprints.clear()


# ============================================================================
# UUID Prefix Index
# ============================================================================

class TestUuidPrefixIndex:

    def test_register_and_lookup(self):
        full_uuid = "5e728ecb-1234-5678-9abc-def012345678"
        prefix = full_uuid[:12]
        _register_uuid_prefix(prefix, full_uuid)
        assert _lookup_uuid_by_prefix(prefix) == full_uuid

    def test_lookup_not_found(self):
        assert _lookup_uuid_by_prefix("nonexistent1") is None

    def test_collision_keeps_first(self):
        prefix = "5e728ecb1234"
        uuid1 = "5e728ecb-1234-aaaa-bbbb-ccccddddeeee"
        uuid2 = "5e728ecb-1234-ffff-0000-111122223333"
        _register_uuid_prefix(prefix, uuid1)
        _register_uuid_prefix(prefix, uuid2)  # collision
        assert _lookup_uuid_by_prefix(prefix) == uuid1  # first wins

    def test_same_uuid_no_collision(self):
        prefix = "5e728ecb1234"
        uuid1 = "5e728ecb-1234-aaaa-bbbb-ccccddddeeee"
        _register_uuid_prefix(prefix, uuid1)
        _register_uuid_prefix(prefix, uuid1)  # same UUID, no collision
        assert _lookup_uuid_by_prefix(prefix) == uuid1


# ============================================================================
# Session Key Derivation
# ============================================================================

class TestGetSessionKey:

    def test_explicit_session_id_wins(self):
        result = _get_session_key(
            arguments={"client_session_id": "from-args"},
            session_id="explicit-id",
        )
        assert result == "explicit-id"

    def test_client_session_id_from_arguments(self):
        result = _get_session_key(arguments={"client_session_id": "arg-session"})
        assert result == "arg-session"

    def test_context_key_fallback(self):
        with patch(
            "src.mcp_handlers.context.get_context_session_key",
            return_value="ctx-key-123",
        ):
            result = _get_session_key(arguments={})
            assert result == "ctx-key-123"

    def test_stdio_fallback(self):
        with patch(
            "src.mcp_handlers.context.get_context_session_key",
            return_value=None,
        ):
            result = _get_session_key(arguments={})
            assert result == f"stdio:{os.getpid()}"

    def test_none_arguments_uses_fallback(self):
        with patch(
            "src.mcp_handlers.context.get_context_session_key",
            return_value=None,
        ):
            result = _get_session_key()
            assert result.startswith("stdio:")


# ============================================================================
# make_client_session_id
# ============================================================================

class TestMakeClientSessionId:

    def test_format(self):
        uuid = "5e728ecb-1234-5678-9abc-def012345678"
        result = make_client_session_id(uuid)
        # uuid[:12] = "5e728ecb-123" (includes hyphen)
        assert result == "agent-5e728ecb-123"
        assert result == f"agent-{uuid[:12]}"

    def test_short_uuid_raises(self):
        with pytest.raises(ValueError, match="Invalid UUID"):
            make_client_session_id("short")

    def test_empty_uuid_raises(self):
        with pytest.raises(ValueError, match="Invalid UUID"):
            make_client_session_id("")

    def test_none_uuid_raises(self):
        with pytest.raises(ValueError, match="Invalid UUID"):
            make_client_session_id(None)


# ============================================================================
# get_bound_agent_id
# ============================================================================

class TestGetBoundAgentId:

    def test_returns_context_agent_id_first(self):
        with patch(
            "src.mcp_handlers.context.get_context_agent_id",
            return_value="ctx-agent-uuid",
        ):
            result = get_bound_agent_id()
            assert result == "ctx-agent-uuid"

    def test_falls_back_to_identity_record(self):
        """When context has no agent_id, falls back to _get_identity_record_sync."""
        with patch(
            "src.mcp_handlers.context.get_context_agent_id",
            return_value=None,
        ):
            # Pre-populate the in-memory cache
            _session_identities[f"stdio:{os.getpid()}"] = {
                "bound_agent_id": "fallback-uuid",
            }
            with patch(
                "src.mcp_handlers.context.get_context_session_key",
                return_value=None,
            ):
                result = get_bound_agent_id()
                assert result == "fallback-uuid"

    def test_returns_none_when_unbound(self):
        with patch(
            "src.mcp_handlers.context.get_context_agent_id",
            return_value=None,
        ), patch(
            "src.mcp_handlers.context.get_context_session_key",
            return_value="unbound-session",
        ):
            # Mock get_mcp_server for _get_identity_record_sync
            mock_server = MagicMock()
            mock_server.agent_metadata = {}
            with patch(
                "src.mcp_handlers.shared.get_mcp_server",
                return_value=mock_server,
            ):
                result = get_bound_agent_id()
                assert result is None


# ============================================================================
# is_session_bound
# ============================================================================

class TestIsSessionBound:

    def test_true_when_bound(self):
        with patch(
            "src.mcp_handlers.context.get_context_agent_id",
            return_value="some-agent",
        ):
            assert is_session_bound() is True

    def test_false_when_unbound(self):
        with patch(
            "src.mcp_handlers.context.get_context_agent_id",
            return_value=None,
        ), patch(
            "src.mcp_handlers.context.get_context_session_key",
            return_value="no-binding",
        ):
            mock_server = MagicMock()
            mock_server.agent_metadata = {}
            with patch(
                "src.mcp_handlers.shared.get_mcp_server",
                return_value=mock_server,
            ):
                assert is_session_bound() is False


# ============================================================================
# require_write_permission
# ============================================================================

class TestRequireWritePermission:

    def test_allowed_when_bound(self):
        with patch(
            "src.mcp_handlers.identity.shared.is_session_bound",
            return_value=True,
        ):
            allowed, error = require_write_permission()
            assert allowed is True
            assert error is None

    def test_denied_when_unbound(self):
        with patch(
            "src.mcp_handlers.identity.shared.is_session_bound",
            return_value=False,
        ):
            allowed, error = require_write_permission()
            assert allowed is False
            assert error is not None


# ============================================================================
# _get_identity_record_sync
# ============================================================================

class TestGetIdentityRecordSync:

    def test_returns_cached_record(self):
        _session_identities["cached-key"] = {
            "bound_agent_id": "cached-uuid",
        }
        with patch(
            "src.mcp_handlers.context.get_context_session_key",
            return_value=None,
        ):
            result = _get_identity_record_sync(session_id="cached-key")
            assert result["bound_agent_id"] == "cached-uuid"

    @pytest.mark.parametrize("key, registered", [
        ("agent-5e728ecb-123", True),    # uuid prefix registered in the index
        ("agent-5e728ecb-123", False),   # found only by scanning metadata
        ("agent-5", False),              # a short prefix matched the first agent
        ("agent-", False),               # an empty one matched any agent
    ])
    def test_an_agent_key_is_never_derived_from_its_shape(self, key, registered):
        """A key resolves only through a binding stored for it. The sync path
        used to turn any "agent-" key into a uuid prefix and bind it to the
        first matching agent, with no binding and no proof."""
        full_uuid = "5e728ecb-1234-5678-9abc-def012345678"
        if registered:
            _register_uuid_prefix(full_uuid[:12], full_uuid)
        mock_server = MagicMock()
        mock_server.agent_metadata = {full_uuid: MagicMock(api_key=None)}

        with patch(
            "src.mcp_handlers.shared.get_mcp_server",
            return_value=mock_server,
        ), patch(
            "src.mcp_handlers.context.get_context_session_key",
            return_value=None,
        ):
            result = _get_identity_record_sync(session_id=key)

        assert result["bound_agent_id"] is None
        assert _session_identities[key]["bound_agent_id"] is None

    def test_a_stored_binding_for_an_agent_key_still_resolves(self):
        full_uuid = "5e728ecb-1234-5678-9abc-def012345678"
        key = f"agent-{full_uuid[:12]}"
        _session_identities[key] = {"bound_agent_id": full_uuid, "bind_count": 1}
        with patch(
            "src.mcp_handlers.context.get_context_session_key",
            return_value=None,
        ):
            assert _get_identity_record_sync(session_id=key)["bound_agent_id"] == full_uuid

    def test_unknown_key_creates_empty_record(self):
        mock_server = MagicMock()
        mock_server.agent_metadata = {}

        with patch(
            "src.mcp_handlers.shared.get_mcp_server",
            return_value=mock_server,
        ), patch(
            "src.mcp_handlers.context.get_context_session_key",
            return_value=None,
        ):
            result = _get_identity_record_sync(session_id="random-key")
            assert result["bound_agent_id"] is None
            assert result["bind_count"] == 0


# ============================================================================
# PATH 1 sync fingerprint check
# ============================================================================
# Mirrors the resolution.py:441-487 async-path check on the sync site
# (_get_identity_record_sync). Closes the residual sync half of KG
# 2026-04-20T00:57:45 (PATH 1 hijack via agent-{uuid12} prefix-bind).
# Gated by UNITARES_SESSION_FINGERPRINT_CHECK env var via
# config.governance_config.session_fingerprint_check_mode().

class TestPath1SyncFingerprintCheck:

    def _setup_cached_binding(self, key, agent_uuid, bind_fp):
        """Pre-populate _session_identities and _bind_fingerprints as if
        _cache_session had been called for this session."""
        _session_identities[key] = {
            "bound_agent_id": agent_uuid,
            "agent_uuid": agent_uuid,
            "bind_count": 0,
        }
        _bind_fingerprints[key] = bind_fp

    def _make_signals(self, current_fp):
        sig = MagicMock()
        sig.ip_ua_fingerprint = current_fp
        return sig

    def test_cached_path_fingerprint_match_returns_record(self):
        self._setup_cached_binding("agent-abc123def456", "abc123def456-...", "fp_legit")
        with patch(
            "src.mcp_handlers.context.get_context_session_key",
            return_value=None,
        ), patch(
            "src.mcp_handlers.identity.shared.get_session_signals",
            return_value=self._make_signals("fp_legit"),
        ), patch(
            "src.mcp_handlers.identity.shared.session_fingerprint_check_mode",
            return_value="log",
        ):
            result = _get_identity_record_sync(session_id="agent-abc123def456")
            assert result["bound_agent_id"] == "abc123def456-..."

    def test_cached_path_fingerprint_mismatch_log_mode_returns_record_with_warning(self, caplog):
        self._setup_cached_binding("agent-abc123def456", "abc123def456-...", "fp_legit")
        import logging
        caplog.set_level(logging.WARNING)
        with patch(
            "src.mcp_handlers.context.get_context_session_key",
            return_value=None,
        ), patch(
            "src.mcp_handlers.identity.shared.get_session_signals",
            return_value=self._make_signals("fp_attacker"),
        ), patch(
            "src.mcp_handlers.identity.shared.session_fingerprint_check_mode",
            return_value="log",
        ):
            result = _get_identity_record_sync(session_id="agent-abc123def456")
            assert result["bound_agent_id"] == "abc123def456-..."
        assert any("PATH1_FINGERPRINT_MISMATCH" in r.message for r in caplog.records)

    def test_cached_path_fingerprint_mismatch_strict_mode_returns_empty(self):
        self._setup_cached_binding("agent-abc123def456", "abc123def456-...", "fp_legit")
        with patch(
            "src.mcp_handlers.context.get_context_session_key",
            return_value=None,
        ), patch(
            "src.mcp_handlers.identity.shared.get_session_signals",
            return_value=self._make_signals("fp_attacker"),
        ), patch(
            "src.mcp_handlers.identity.shared.session_fingerprint_check_mode",
            return_value="strict",
        ):
            result = _get_identity_record_sync(session_id="agent-abc123def456")
            assert result["bound_agent_id"] is None
            assert result.get("_session_key_type") == "path1_sync_fingerprint_strict_mismatch"

    def test_cached_path_fingerprint_mismatch_off_mode_returns_record(self, caplog):
        self._setup_cached_binding("agent-abc123def456", "abc123def456-...", "fp_legit")
        import logging
        caplog.set_level(logging.WARNING)
        with patch(
            "src.mcp_handlers.context.get_context_session_key",
            return_value=None,
        ), patch(
            "src.mcp_handlers.identity.shared.get_session_signals",
            return_value=self._make_signals("fp_attacker"),
        ), patch(
            "src.mcp_handlers.identity.shared.session_fingerprint_check_mode",
            return_value="off",
        ):
            result = _get_identity_record_sync(session_id="agent-abc123def456")
            assert result["bound_agent_id"] == "abc123def456-..."
        assert not any("PATH1_FINGERPRINT_MISMATCH" in r.message for r in caplog.records)

    def test_an_unbound_agent_key_records_no_bind_fingerprint(self):
        """The removed derivation also wrote the first caller's fingerprint for
        the key; with no derivation nothing is recorded for an unbound key."""
        key = "agent-abcdef123456"
        mock_server = MagicMock()
        mock_server.agent_metadata = {"abcdef123456-7890-abcd-ef01-234567890abc": MagicMock(api_key=None)}
        with patch(
            "src.mcp_handlers.shared.get_mcp_server",
            return_value=mock_server,
        ), patch(
            "src.mcp_handlers.context.get_context_session_key",
            return_value=None,
        ), patch(
            "src.mcp_handlers.identity.shared.get_session_signals",
            return_value=self._make_signals("fp_first_caller"),
        ):
            assert _get_identity_record_sync(session_id=key)["bound_agent_id"] is None
        assert key not in _bind_fingerprints

    def test_fallback_does_not_overwrite_existing_bind_fingerprint(self):
        full_uuid = "abcdef123456-7890-abcd-ef01-234567890abc"
        prefix = "abcdef123456"
        key = f"agent-{prefix}"
        # Pre-populate _bind_fingerprints as if _cache_session had recorded it
        _bind_fingerprints[key] = "fp_legit_first_bind"

        mock_server = MagicMock()
        mock_meta = MagicMock()
        mock_meta.api_key = None
        mock_server.agent_metadata = {full_uuid: mock_meta}

        # Attacker arrives via FALLBACK with a different fingerprint
        with patch(
            "src.mcp_handlers.shared.get_mcp_server",
            return_value=mock_server,
        ), patch(
            "src.mcp_handlers.context.get_context_session_key",
            return_value=None,
        ), patch(
            "src.mcp_handlers.identity.shared.get_session_signals",
            return_value=self._make_signals("fp_attacker"),
        ):
            _get_identity_record_sync(session_id=key)

        assert _bind_fingerprints[key] == "fp_legit_first_bind"

    def test_cache_session_populates_bind_fingerprints(self):
        """_cache_session writes the binding-time fingerprint to
        _bind_fingerprints so the sync PATH 1 check can read it."""
        import asyncio
        from src.mcp_handlers.identity.persistence import _cache_session

        key = "agent-aaaa11112222"
        agent_uuid = "aaaa1111-2222-3333-4444-555566667777"

        async def _run():
            await _cache_session(key, agent_uuid, display_agent_id="alice")

        with patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=self._make_signals("fp_first_bind"),
        ), patch(
            "src.mcp_handlers.identity.persistence._get_redis",
            return_value=None,
        ):
            asyncio.run(_run())

        assert _bind_fingerprints.get(key) == "fp_first_bind"

    def test_cache_session_does_not_overwrite_existing_bind_fingerprint(self):
        """A second _cache_session call for the same key must not overwrite
        the original bind fingerprint — first-bind owner is authoritative."""
        import asyncio
        from src.mcp_handlers.identity.persistence import _cache_session

        key = "agent-aaaa11112222"
        agent_uuid = "aaaa1111-2222-3333-4444-555566667777"
        _bind_fingerprints[key] = "fp_legit_first_bind"

        async def _run():
            await _cache_session(key, agent_uuid, display_agent_id="alice")

        with patch(
            "src.mcp_handlers.context.get_session_signals",
            return_value=self._make_signals("fp_attacker"),
        ), patch(
            "src.mcp_handlers.identity.persistence._get_redis",
            return_value=None,
        ):
            asyncio.run(_run())

        assert _bind_fingerprints[key] == "fp_legit_first_bind"

# ============================================================================
# Lineage Helpers
# ============================================================================

class TestLineage:

    def _make_mock_server(self, metadata_map):
        """Create mock server with agent_metadata."""
        mock_server = MagicMock()
        mock_server.agent_metadata = metadata_map
        return mock_server

    def test_get_lineage_single_agent(self):
        mock_meta = MagicMock()
        mock_meta.parent_agent_id = None
        mock_server = self._make_mock_server({"root": mock_meta})

        with patch(
            "src.mcp_handlers.shared.get_mcp_server",
            return_value=mock_server,
        ):
            lineage = _get_lineage("root")
            assert lineage == ["root"]

    def test_get_lineage_chain(self):
        grandparent = MagicMock()
        grandparent.parent_agent_id = None
        parent = MagicMock()
        parent.parent_agent_id = "grandparent"
        child = MagicMock()
        child.parent_agent_id = "parent"
        mock_server = self._make_mock_server({
            "grandparent": grandparent,
            "parent": parent,
            "child": child,
        })

        with patch(
            "src.mcp_handlers.shared.get_mcp_server",
            return_value=mock_server,
        ):
            lineage = _get_lineage("child")
            # oldest ancestor first
            assert lineage == ["grandparent", "parent", "child"]

    def test_get_lineage_unknown_parent(self):
        """Agent with parent not in metadata."""
        child = MagicMock()
        child.parent_agent_id = "missing-parent"
        mock_server = self._make_mock_server({"child": child})

        with patch(
            "src.mcp_handlers.shared.get_mcp_server",
            return_value=mock_server,
        ):
            lineage = _get_lineage("child")
            # walks to missing-parent, can't find it, stops
            assert "child" in lineage
