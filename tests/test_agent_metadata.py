"""
Tests for AgentMetadata in src/mcp_server_std.py.

Tests to_dict, add_lifecycle_event, validate_consistency, and _normalize_http_proxy_base.
"""

import pytest
import sys
from pathlib import Path
from datetime import datetime

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.agent_state import AgentMetadata, _normalize_http_proxy_base


# ============================================================================
# _normalize_http_proxy_base
# ============================================================================

@pytest.mark.smoke
class TestNormalizeHttpProxyBase:

    def test_strips_v1_tools_call(self):
        assert _normalize_http_proxy_base("http://localhost:8767/v1/tools/call") == "http://localhost:8767"

    def test_strips_v1_tools(self):
        assert _normalize_http_proxy_base("http://localhost:8767/v1/tools") == "http://localhost:8767"

    def test_strips_trailing_slash(self):
        assert _normalize_http_proxy_base("http://localhost:8767/") == "http://localhost:8767"

    def test_plain_url_unchanged(self):
        assert _normalize_http_proxy_base("http://localhost:8767") == "http://localhost:8767"

    def test_empty_string(self):
        assert _normalize_http_proxy_base("") == ""

    def test_none_returns_empty(self):
        assert _normalize_http_proxy_base(None) == ""

    def test_whitespace_stripped(self):
        assert _normalize_http_proxy_base("  http://localhost:8767  ") == "http://localhost:8767"

    def test_preserves_port(self):
        result = _normalize_http_proxy_base("http://example.com:9000/v1/tools/call")
        assert result == "http://example.com:9000"

    def test_preserves_path_before_v1(self):
        result = _normalize_http_proxy_base("http://example.com/mcp/v1/tools/call")
        assert result == "http://example.com/mcp"


# ============================================================================
# AgentMetadata.to_dict
# ============================================================================

@pytest.mark.smoke
class TestAgentMetadataToDict:

    def test_returns_dict(self):
        meta = AgentMetadata(agent_id="test", status="active",
                            created_at="2026-01-15T12:00:00",
                            last_update="2026-01-15T12:00:00")
        result = meta.to_dict()
        assert isinstance(result, dict)

    def test_contains_required_fields(self):
        meta = AgentMetadata(agent_id="test-agent", status="active",
                            created_at="2026-01-15T12:00:00",
                            last_update="2026-01-15T12:00:00")
        d = meta.to_dict()
        assert d['agent_id'] == "test-agent"
        assert d['status'] == "active"
        assert d['created_at'] == "2026-01-15T12:00:00"

    def test_default_values(self):
        meta = AgentMetadata(agent_id="test", status="active",
                            created_at="2026-01-15", last_update="2026-01-15")
        d = meta.to_dict()
        assert d['total_updates'] == 0
        assert d['version'] == "v1.0"
        assert d['tags'] == []
        assert d['lifecycle_events'] == []


# ============================================================================
# AgentMetadata.add_lifecycle_event
# ============================================================================

@pytest.mark.smoke
class TestAddLifecycleEvent:

    def test_event_added(self):
        meta = AgentMetadata(agent_id="test", status="active",
                            created_at="2026-01-15", last_update="2026-01-15")
        meta.add_lifecycle_event("created")
        assert len(meta.lifecycle_events) == 1
        assert meta.lifecycle_events[0]['event'] == "created"

    def test_event_has_timestamp(self):
        meta = AgentMetadata(agent_id="test", status="active",
                            created_at="2026-01-15", last_update="2026-01-15")
        meta.add_lifecycle_event("paused")
        assert 'timestamp' in meta.lifecycle_events[0]
        # Verify it's valid ISO format
        datetime.fromisoformat(meta.lifecycle_events[0]['timestamp'])

    def test_reason_optional(self):
        meta = AgentMetadata(agent_id="test", status="active",
                            created_at="2026-01-15", last_update="2026-01-15")
        meta.add_lifecycle_event("resumed")
        assert meta.lifecycle_events[0]['reason'] is None

    def test_reason_provided(self):
        meta = AgentMetadata(agent_id="test", status="active",
                            created_at="2026-01-15", last_update="2026-01-15")
        meta.add_lifecycle_event("paused", reason="User requested")
        assert meta.lifecycle_events[0]['reason'] == "User requested"

    def test_multiple_events_accumulate(self):
        meta = AgentMetadata(agent_id="test", status="active",
                            created_at="2026-01-15", last_update="2026-01-15")
        meta.add_lifecycle_event("created")
        meta.add_lifecycle_event("paused")
        meta.add_lifecycle_event("resumed")
        assert len(meta.lifecycle_events) == 3

    def test_lifecycle_events_capped_at_max(self):
        meta = AgentMetadata(agent_id="test", status="active",
                            created_at="2026-01-15", last_update="2026-01-15")
        for i in range(AgentMetadata.MAX_LIFECYCLE_EVENTS + 20):
            meta.add_lifecycle_event(f"event_{i}")
        assert len(meta.lifecycle_events) == AgentMetadata.MAX_LIFECYCLE_EVENTS

    def test_lifecycle_events_keeps_most_recent(self):
        meta = AgentMetadata(agent_id="test", status="active",
                            created_at="2026-01-15", last_update="2026-01-15")
        for i in range(AgentMetadata.MAX_LIFECYCLE_EVENTS + 10):
            meta.add_lifecycle_event(f"event_{i}")
        # Oldest events should be evicted, newest kept
        assert meta.lifecycle_events[0]["event"] == "event_10"
        assert meta.lifecycle_events[-1]["event"] == f"event_{AgentMetadata.MAX_LIFECYCLE_EVENTS + 9}"


# ============================================================================
# AgentMetadata.validate_consistency
# ============================================================================

@pytest.mark.smoke
class TestValidateConsistency:

    def _make_meta(self, **kwargs):
        defaults = dict(
            agent_id="test",
            status="active",
            created_at="2026-01-15T12:00:00",
            last_update="2026-01-15T12:00:00",
            total_updates=0,
        )
        defaults.update(kwargs)
        return AgentMetadata(**defaults)

    def test_valid_empty_metadata(self):
        meta = self._make_meta()
        is_valid, errors = meta.validate_consistency()
        assert is_valid is True
        assert errors == []

    def test_matching_arrays(self):
        meta = self._make_meta(
            total_updates=3,
            recent_update_timestamps=["2026-01-15T12:00:00", "2026-01-15T12:01:00", "2026-01-15T12:02:00"],
            recent_decisions=["proceed", "proceed", "reflect"],
        )
        is_valid, errors = meta.validate_consistency()
        assert is_valid is True

    def test_mismatched_array_lengths(self):
        meta = self._make_meta(
            total_updates=3,
            recent_update_timestamps=["2026-01-15T12:00:00", "2026-01-15T12:01:00"],
            recent_decisions=["proceed", "proceed", "reflect"],
        )
        is_valid, errors = meta.validate_consistency()
        assert is_valid is False
        assert any("mismatched lengths" in e for e in errors)

    def test_total_updates_mismatch(self):
        meta = self._make_meta(
            total_updates=5,
            recent_update_timestamps=["t1", "t2", "t3"],
            recent_decisions=["p", "p", "p"],
        )
        is_valid, errors = meta.validate_consistency()
        assert is_valid is False
        assert any("total_updates" in e for e in errors)

    def test_capped_arrays_valid(self):
        """total_updates > 10, arrays capped at 10 → valid"""
        meta = self._make_meta(
            total_updates=50,
            recent_update_timestamps=[f"2026-01-15T12:{i:02d}:00" for i in range(10)],
            recent_decisions=["proceed"] * 10,
        )
        is_valid, errors = meta.validate_consistency()
        assert is_valid is True

    def test_arrays_exceed_cap(self):
        """total_updates > 10 but arrays > 10 → invalid"""
        meta = self._make_meta(
            total_updates=15,
            recent_update_timestamps=["t"] * 12,
            recent_decisions=["p"] * 12,
        )
        is_valid, errors = meta.validate_consistency()
        assert is_valid is False
        assert any("exceeds cap" in e for e in errors)

    def test_paused_without_paused_at(self):
        meta = self._make_meta(status="paused", paused_at=None)
        is_valid, errors = meta.validate_consistency()
        assert is_valid is False
        assert any("paused_at is None" in e for e in errors)

    def test_paused_with_paused_at(self):
        meta = self._make_meta(status="paused", paused_at="2026-01-15T12:00:00")
        is_valid, errors = meta.validate_consistency()
        assert is_valid is True

    def test_invalid_timestamp_format(self):
        meta = self._make_meta(created_at="not-a-timestamp")
        is_valid, errors = meta.validate_consistency()
        assert is_valid is False
        assert any("timestamp format" in e.lower() for e in errors)

    def test_valid_z_timestamp(self):
        meta = self._make_meta(created_at="2026-01-15T12:00:00Z")
        is_valid, errors = meta.validate_consistency()
        assert is_valid is True

    def test_multiple_errors(self):
        meta = self._make_meta(
            status="paused",
            paused_at=None,
            total_updates=3,
            recent_update_timestamps=["t1"],
            recent_decisions=["p", "p"],
        )
        is_valid, errors = meta.validate_consistency()
        assert is_valid is False
        assert len(errors) >= 2


# ============================================================================
# AgentMetadata.add_recent_update (Watcher P002 regression)
#
# Caps the parallel recent_update_timestamps / recent_decisions arrays so
# callers can't accidentally grow them unboundedly.
# ============================================================================

class TestAddRecentUpdate:

    def _make(self):
        return AgentMetadata(agent_id="a", status="active", created_at="t", last_update="t")

    def test_appends_to_both_arrays(self):
        meta = self._make()
        meta.add_recent_update("2026-04-16T10:00:00", "approve")
        assert meta.recent_update_timestamps == ["2026-04-16T10:00:00"]
        assert meta.recent_decisions == ["approve"]

    def test_caps_at_max(self):
        meta = self._make()
        for i in range(meta.MAX_RECENT_UPDATES + 5):
            meta.add_recent_update(f"t{i}", f"a{i}")
        assert len(meta.recent_update_timestamps) == meta.MAX_RECENT_UPDATES
        assert len(meta.recent_decisions) == meta.MAX_RECENT_UPDATES
        # Newest entries are retained; oldest are dropped.
        assert meta.recent_update_timestamps[-1] == f"t{meta.MAX_RECENT_UPDATES + 4}"
        assert meta.recent_decisions[-1] == f"a{meta.MAX_RECENT_UPDATES + 4}"

    def test_arrays_stay_parallel_after_cap(self):
        meta = self._make()
        for i in range(meta.MAX_RECENT_UPDATES * 2):
            meta.add_recent_update(f"t{i}", f"a{i}")
        assert len(meta.recent_update_timestamps) == len(meta.recent_decisions)
        for ts, decision in zip(meta.recent_update_timestamps, meta.recent_decisions):
            assert ts.replace("t", "") == decision.replace("a", "")


# ============================================================================
# S21-b §1 / §3: register_minted_agent_in_dict + mirror_status_to_dict
# ============================================================================
# These helpers close the H14 axiom-#3 gap surfaced by S21-a review pass-2.
# Without them, freshly-minted core.identities rows are invisible to
# require_registered_agent, and update_identity_status writes silently drift
# the dict away from PG (verifier observed 67 active/archived inversions).


class TestRegisterMintedAgentInDict:
    def setup_method(self):
        from src.agent_metadata_model import agent_metadata
        agent_metadata.clear()

    def test_inserts_new_entry(self):
        from src.agent_metadata_persistence import register_minted_agent_in_dict
        from src.agent_metadata_model import agent_metadata
        uid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        added = register_minted_agent_in_dict(
            uid,
            label="claude_desktop-claude_aaaaaaaa",
            public_agent_id="claude_desktop-claude-2026-04-27",
            parent_agent_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            spawn_reason="dispatch_auto_mint",
        )
        assert added is True
        assert uid in agent_metadata
        meta = agent_metadata[uid]
        assert meta.status == "active"
        assert meta.label == "claude_desktop-claude_aaaaaaaa"
        assert meta.public_agent_id == "claude_desktop-claude-2026-04-27"
        assert meta.spawn_reason == "dispatch_auto_mint"
        assert meta.parent_agent_id == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        assert meta.agent_uuid == uid

    def test_does_not_clobber_existing(self):
        # Existing entry (e.g., from bulk reload) wins. Mint helper is for
        # the no-entry case only.
        from src.agent_metadata_persistence import register_minted_agent_in_dict
        from src.agent_metadata_model import agent_metadata, AgentMetadata
        uid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        agent_metadata[uid] = AgentMetadata(
            agent_id="prior", status="paused", created_at="2026-01-01",
            last_update="2026-01-01", label="prior_label",
        )
        added = register_minted_agent_in_dict(
            uid, label="overwrite_attempt", status="active",
        )
        assert added is False
        # Existing meta unchanged.
        assert agent_metadata[uid].label == "prior_label"
        assert agent_metadata[uid].status == "paused"

    def test_backfills_thread_id_when_existing_entry_has_none(self):
        # Regression for #424: if an auto-mint path (e.g. process_agent_update
        # self-create) populated agent_metadata with thread_id=None before
        # onboard ran, onboard's register call must backfill the missing
        # thread_id rather than short-circuiting. Otherwise the in-memory
        # cache stays desynced from PG and phases.py mints a fresh UUID-form
        # thread_id at the next update, resetting node_index to 1.
        from src.agent_metadata_persistence import register_minted_agent_in_dict
        from src.agent_metadata_model import agent_metadata, AgentMetadata
        uid = "11111111-1111-4111-8111-111111111111"
        # Auto-mint path: entry exists, thread_id is None.
        agent_metadata[uid] = AgentMetadata(
            agent_id="auto_minted", status="active",
            created_at="2026-05-08", last_update="2026-05-08",
        )
        assert agent_metadata[uid].thread_id is None

        # Onboard runs, has the real thread_id.
        added = register_minted_agent_in_dict(
            uid, thread_id="t-d588dc931ace7a3b", node_index=1,
        )
        assert added is False  # didn't insert; entry was already there
        # But thread_id and node_index were filled.
        assert agent_metadata[uid].thread_id == "t-d588dc931ace7a3b"
        assert agent_metadata[uid].node_index == 1

    def test_backfill_does_not_overwrite_existing_thread_id(self):
        # Strict fill-only: never overwrite a non-None value, even with a
        # different one. Protects against the inverse failure mode where
        # an auto-mint *did* manage to record a thread_id and a later call
        # would otherwise replace it.
        from src.agent_metadata_persistence import register_minted_agent_in_dict
        from src.agent_metadata_model import agent_metadata, AgentMetadata
        uid = "22222222-2222-4222-8222-222222222222"
        agent_metadata[uid] = AgentMetadata(
            agent_id="prior", status="active",
            created_at="2026-05-08", last_update="2026-05-08",
            thread_id="t-existing000000",
            node_index=3,
        )
        added = register_minted_agent_in_dict(
            uid, thread_id="t-different00000", node_index=99,
        )
        assert added is False
        assert agent_metadata[uid].thread_id == "t-existing000000"
        assert agent_metadata[uid].node_index == 3


class TestMirrorStatusToDict:
    def setup_method(self):
        from src.agent_metadata_model import agent_metadata
        agent_metadata.clear()

    def test_updates_existing(self):
        from src.agent_metadata_persistence import mirror_status_to_dict
        from src.agent_metadata_model import agent_metadata, AgentMetadata
        uid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        agent_metadata[uid] = AgentMetadata(
            agent_id="X", status="active", created_at="2026-01-01",
            last_update="2026-01-01",
        )
        ok = mirror_status_to_dict(uid, "archived")
        assert ok is True
        assert agent_metadata[uid].status == "archived"

    def test_returns_false_when_missing(self):
        from src.agent_metadata_persistence import mirror_status_to_dict
        ok = mirror_status_to_dict("ffffffff-ffff-4fff-8fff-ffffffffffff", "deleted")
        assert ok is False
