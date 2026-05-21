"""Tests for `_track_thread_identity` — issue #424 closure.

The original mid-update mint path produced two bugs simultaneously:
  - UUID-format thread_id (vs. onboard's ``t-<sha16>``) on the same session
  - node_index reset to 1 even when the durable record knew otherwise

These tests pin the post-fix invariants:
  1. No random UUIDs ever appear in ctx.meta.thread_id from this code path.
  2. Durable PG metadata is consulted before any fallback mint.
  3. Fallback minting uses the deterministic `generate_thread_id` function
     (matches onboard).
  4. node_index continuity is preserved across process restarts when PG has
     the persisted value (no silent reset to 1).
"""

from dataclasses import dataclass, field
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# Lightweight stand-in for AgentMetadata — only the attrs the helper reads.
@dataclass
class _FakeMeta:
    thread_id: Optional[str] = None
    node_index: Optional[int] = None
    active_session_key: Optional[str] = None


def _make_ctx(
    *,
    meta: Optional[_FakeMeta],
    session_key: Optional[str],
    agent_uuid: str = "11111111-1111-1111-1111-111111111111",
):
    ctx = MagicMock()
    ctx.meta = meta
    ctx.session_key = session_key
    ctx.agent_uuid = agent_uuid
    return ctx


def _identity_record(metadata: dict) -> Any:
    rec = MagicMock()
    rec.metadata = metadata
    return rec


# ──────────────────────────────────────────────────────────────────────────
# Guards
# ──────────────────────────────────────────────────────────────────────────


class TestTrackThreadIdentityGuards:
    @pytest.mark.asyncio
    async def test_no_meta_returns_false(self):
        from src.mcp_handlers.updates.phases import _track_thread_identity
        ctx = _make_ctx(meta=None, session_key="mcp:abc")
        assert await _track_thread_identity(ctx) is False

    @pytest.mark.asyncio
    async def test_no_session_key_returns_false(self):
        from src.mcp_handlers.updates.phases import _track_thread_identity
        ctx = _make_ctx(meta=_FakeMeta(), session_key=None)
        assert await _track_thread_identity(ctx) is False

    @pytest.mark.asyncio
    async def test_same_session_key_is_noop(self):
        from src.mcp_handlers.updates.phases import _track_thread_identity
        meta = _FakeMeta(
            thread_id="t-abcdef0123456789",
            node_index=7,
            active_session_key="mcp:abc",
        )
        ctx = _make_ctx(meta=meta, session_key="mcp:abc")
        assert await _track_thread_identity(ctx) is False
        # Nothing mutated
        assert meta.thread_id == "t-abcdef0123456789"
        assert meta.node_index == 7
        assert meta.active_session_key == "mcp:abc"


# ──────────────────────────────────────────────────────────────────────────
# #424 regression: no random UUIDs
# ──────────────────────────────────────────────────────────────────────────


class TestNoRandomUuidMintRegression:
    @pytest.mark.asyncio
    async def test_empty_in_memory_and_pg_falls_back_to_session_key_derivation(self):
        """The #424 worst case: no thread_id in memory, no PG record.

        Old behavior: ``uuid.uuid4()`` produced a UUID-format id, position-1.
        New behavior: deterministic derivation from session_key — same format
        as onboard's ``generate_thread_id``.
        """
        from src.mcp_handlers.updates.phases import _track_thread_identity
        from src.thread_identity import generate_thread_id

        meta = _FakeMeta()  # all None
        ctx = _make_ctx(meta=meta, session_key="mcp:fresh-session-xyz")
        fake_db = MagicMock()
        fake_db.get_identity = AsyncMock(return_value=None)

        with patch("src.db.get_db", return_value=fake_db):
            changed = await _track_thread_identity(ctx)

        assert changed is True
        # Format guarantee: matches onboard's t-<sha16> shape
        assert meta.thread_id.startswith("t-")
        assert len(meta.thread_id) == 18  # "t-" + 16 hex
        # Format is deterministic — same call twice produces the same id
        assert meta.thread_id == generate_thread_id("mcp:fresh-session-xyz")
        # No UUID-format strings (UUID has hyphens at fixed positions)
        assert meta.thread_id.count("-") == 1
        # node_index = 1 because active_session_key was None pre-call
        assert meta.node_index == 1
        assert meta.active_session_key == "mcp:fresh-session-xyz"

    @pytest.mark.asyncio
    async def test_pg_hydration_wins_over_fallback(self):
        """When PG has a thread_id, the fallback derivation must NOT fire."""
        from src.mcp_handlers.updates.phases import _track_thread_identity

        meta = _FakeMeta()
        ctx = _make_ctx(meta=meta, session_key="mcp:new-session")
        fake_db = MagicMock()
        fake_db.get_identity = AsyncMock(
            return_value=_identity_record({
                "thread_id": "t-persistedfromonbo",
                "node_index": 5,
                "active_session_key": "mcp:prior-session",
            })
        )

        with patch("src.db.get_db", return_value=fake_db):
            changed = await _track_thread_identity(ctx)

        assert changed is True
        assert meta.thread_id == "t-persistedfromonbo"
        # node_index bumped from 5 → 6 because active_session_key existed
        assert meta.node_index == 6
        assert meta.active_session_key == "mcp:new-session"

    @pytest.mark.asyncio
    async def test_pg_hydration_preserves_node_index_continuity(self):
        """Process restart: in-memory empty, PG has node_index=4. The bump
        must reflect prior continuity, not reset to 1."""
        from src.mcp_handlers.updates.phases import _track_thread_identity

        meta = _FakeMeta()
        ctx = _make_ctx(meta=meta, session_key="mcp:after-restart-session")
        fake_db = MagicMock()
        fake_db.get_identity = AsyncMock(
            return_value=_identity_record({
                "thread_id": "t-restartcontinuity",
                "node_index": 4,
                "active_session_key": "mcp:pre-restart-session",
            })
        )

        with patch("src.db.get_db", return_value=fake_db):
            await _track_thread_identity(ctx)

        # Continuity: 4 → 5, NOT 1 → 2
        assert meta.node_index == 5

    @pytest.mark.asyncio
    async def test_pg_uuid_format_thread_id_heals_to_canonical(self):
        """#484 heal-on-read: pre-#483 PG rows carry UUID-format thread_ids.
        Step 1 must skip them so step 2 re-derives t-<sha16> from session_key.
        node_index continuity is preserved (it's not the corrupt field).
        """
        from src.mcp_handlers.updates.phases import _track_thread_identity
        from src.thread_identity import generate_thread_id

        meta = _FakeMeta()
        ctx = _make_ctx(meta=meta, session_key="mcp:heal-session-xyz")
        fake_db = MagicMock()
        # UUID-format thread_id from broken pre-#483 mint path
        fake_db.get_identity = AsyncMock(
            return_value=_identity_record({
                "thread_id": "a3f2e1d0-1234-5678-9abc-def012345678",
                "node_index": 7,
                "active_session_key": "mcp:prior-session",
            })
        )

        with patch("src.db.get_db", return_value=fake_db):
            changed = await _track_thread_identity(ctx)

        assert changed is True
        # thread_id healed to canonical t-<sha16> derived from session_key
        assert meta.thread_id.startswith("t-")
        assert len(meta.thread_id) == 18
        assert meta.thread_id == generate_thread_id("mcp:heal-session-xyz")
        # node_index continuity preserved (7 → 8, NOT reset to 1)
        assert meta.node_index == 8
        assert meta.active_session_key == "mcp:heal-session-xyz"

    @pytest.mark.asyncio
    async def test_pg_canonical_thread_id_not_touched_by_heal_logic(self):
        """Regression guard for #484: canonical t-<sha> values must pass
        through step 1 unchanged. Heal-on-read must not break the happy path.
        """
        from src.mcp_handlers.updates.phases import _track_thread_identity

        meta = _FakeMeta()
        ctx = _make_ctx(meta=meta, session_key="mcp:happy-session")
        fake_db = MagicMock()
        fake_db.get_identity = AsyncMock(
            return_value=_identity_record({
                "thread_id": "t-canonicalvalue1",
                "node_index": 3,
                "active_session_key": "mcp:prior",
            })
        )

        with patch("src.db.get_db", return_value=fake_db):
            await _track_thread_identity(ctx)

        # Canonical value preserved — NOT regenerated from session_key
        assert meta.thread_id == "t-canonicalvalue1"

    @pytest.mark.asyncio
    async def test_pg_lookup_failure_falls_back_safely(self):
        """A DB error during hydration must not crash the update — fall back
        to deterministic derivation, log, and proceed."""
        from src.mcp_handlers.updates.phases import _track_thread_identity

        meta = _FakeMeta()
        ctx = _make_ctx(meta=meta, session_key="mcp:fault-tolerant")
        fake_db = MagicMock()
        fake_db.get_identity = AsyncMock(side_effect=RuntimeError("pg unreachable"))

        with patch("src.db.get_db", return_value=fake_db):
            changed = await _track_thread_identity(ctx)

        assert changed is True
        assert meta.thread_id.startswith("t-")
        assert len(meta.thread_id) == 18


# ──────────────────────────────────────────────────────────────────────────
# Happy path: in-memory cache already populated
# ──────────────────────────────────────────────────────────────────────────


class TestInMemoryCachePopulated:
    @pytest.mark.asyncio
    async def test_no_pg_lookup_when_in_memory_thread_id_present(self):
        """If ctx.meta.thread_id is already populated, do NOT round-trip to PG."""
        from src.mcp_handlers.updates.phases import _track_thread_identity

        meta = _FakeMeta(
            thread_id="t-inmemory12345678",
            node_index=2,
            active_session_key="mcp:old-session",
        )
        ctx = _make_ctx(meta=meta, session_key="mcp:new-session")
        fake_db = MagicMock()
        fake_db.get_identity = AsyncMock()

        with patch("src.db.get_db", return_value=fake_db):
            changed = await _track_thread_identity(ctx)

        # Did not call PG
        fake_db.get_identity.assert_not_called()
        assert changed is True
        assert meta.thread_id == "t-inmemory12345678"
        # 2 → 3 (active_session_key was non-None)
        assert meta.node_index == 3
        assert meta.active_session_key == "mcp:new-session"

    @pytest.mark.asyncio
    async def test_first_session_with_thread_id_sets_position_1(self):
        """First-time session adoption: active_session_key was None → position 1."""
        from src.mcp_handlers.updates.phases import _track_thread_identity

        meta = _FakeMeta(
            thread_id="t-firstcontactxxxx",
            node_index=None,
            active_session_key=None,
        )
        ctx = _make_ctx(meta=meta, session_key="mcp:first")

        # No PG call needed (thread_id already present)
        changed = await _track_thread_identity(ctx)

        assert changed is True
        assert meta.thread_id == "t-firstcontactxxxx"
        assert meta.node_index == 1
        assert meta.active_session_key == "mcp:first"
