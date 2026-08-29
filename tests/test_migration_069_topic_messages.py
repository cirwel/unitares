"""
Schema test for migration 069 — `topic:/` surfaces and `lease_plane.topic_messages`.

069 does two things: it registers the `topic:/` surface scheme (so a topic can
later be leased for topic-key gating), and it creates the message table that
replaces governance-KG channel notes as the carrier for addressed agent-to-agent
traffic.

These tests pin the constraints that make the table a *message* store rather
than another knowledge store: bounded lifetime, bounded reply depth, and a
delivery state that cannot disagree with its timestamp.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    import asyncpg
except ImportError:
    pytest.skip("asyncpg not installed", allow_module_level=True)

from tests.test_db_utils import (
    TEST_DB_URL,
    can_connect_to_test_db,
    ensure_test_database_schema,
)

if not can_connect_to_test_db():
    pytest.skip("governance_test database not available", allow_module_level=True)


async def _insert_lease(conn, *, surface_id: str, ttl_s: int = 60) -> uuid.UUID:
    holder_uuid = uuid.uuid4()
    now = datetime.now(UTC)
    lease_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO lease_plane.surface_leases
          (lease_id, surface_id, holder_agent_uuid, holder_kind,
           holder_class, heartbeat_required, original_ttl_s,
           acquired_at, expires_at)
        VALUES ($1, $2, $3, 'remote_heartbeat', 'process_instance', true, $4, $5, $6)
        """,
        lease_id, surface_id, holder_uuid, ttl_s, now, now + timedelta(seconds=ttl_s),
    )
    return lease_id


async def _insert_message(conn, **overrides):
    now = datetime.now(UTC)
    params = {
        "message_id": uuid.uuid4(),
        "topic": "topic:/migration-069-test",
        "sender_agent_uuid": uuid.uuid4(),
        "recipient_agent_uuid": uuid.uuid4(),
        "envelope": "{}",
        "response_to_id": None,
        "reply_depth": 0,
        "delivery_state": "pending",
        "created_at": now,
        "expires_at": now + timedelta(hours=1),
        "delivered_at": None,
    }
    params.update(overrides)
    await conn.execute(
        """
        INSERT INTO lease_plane.topic_messages
          (message_id, topic, sender_agent_uuid, recipient_agent_uuid, envelope,
           response_to_id, reply_depth, delivery_state, created_at, expires_at,
           delivered_at)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11)
        """,
        *params.values(),
    )
    return params["message_id"]


@pytest.mark.asyncio
async def test_069_accepts_topic_scheme_and_derives_kind():
    """A topic:/ surface_id INSERTs cleanly and derives surface_kind='topic'."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        lease_id = await _insert_lease(conn, surface_id="topic:/revenue-engine")
        kind = await conn.fetchval(
            "SELECT surface_kind FROM lease_plane.surface_leases WHERE lease_id = $1",
            lease_id,
        )
        assert kind == "topic", f"expected surface_kind='topic', got {kind!r}"
    finally:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        await conn.close()


@pytest.mark.asyncio
async def test_069_registers_topic_in_surface_kind_catalog():
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        found = await conn.fetchval(
            "SELECT surface_kind FROM lease_plane.surface_kind_catalog "
            "WHERE surface_kind = 'topic'"
        )
        assert found == "topic"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_069_still_rejects_non_canonical_scheme():
    """069 only adds topic:/; a bogus scheme is still rejected."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_lease(conn, surface_id="potato:/still_not_real")
    finally:
        await conn.execute("DELETE FROM lease_plane.surface_leases")
        await conn.close()


@pytest.mark.asyncio
async def test_069_message_lifetime_is_capped():
    """The ceiling is what keeps this from becoming another permanently-open note."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        now = datetime.now(UTC)
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_message(
                conn, created_at=now, expires_at=now + timedelta(days=8)
            )
    finally:
        await conn.execute("DELETE FROM lease_plane.topic_messages")
        await conn.close()


@pytest.mark.asyncio
async def test_069_early_expiry_is_allowed():
    """The bound is a ceiling, not a floor: a message can be retracted early."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        now = datetime.now(UTC)
        message_id = await _insert_message(
            conn, created_at=now, expires_at=now - timedelta(seconds=1)
        )
        assert message_id is not None
    finally:
        await conn.execute("DELETE FROM lease_plane.topic_messages")
        await conn.close()


@pytest.mark.asyncio
async def test_069_reply_depth_is_bounded():
    """Two responsive agents must not be able to reply to each other forever."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_message(conn, reply_depth=17)
    finally:
        await conn.execute("DELETE FROM lease_plane.topic_messages")
        await conn.close()


@pytest.mark.asyncio
async def test_069_delivery_state_and_timestamp_cannot_disagree():
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_message(conn, delivery_state="delivered", delivered_at=None)
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_message(
                conn, delivery_state="pending", delivered_at=datetime.now(UTC)
            )
    finally:
        await conn.execute("DELETE FROM lease_plane.topic_messages")
        await conn.close()


@pytest.mark.asyncio
async def test_069_rejects_self_addressed_and_off_grammar_topic():
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        same = uuid.uuid4()
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_message(
                conn, sender_agent_uuid=same, recipient_agent_uuid=same
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_message(conn, topic="channel-resource-agent")
    finally:
        await conn.execute("DELETE FROM lease_plane.topic_messages")
        await conn.close()


@pytest.mark.asyncio
async def test_069_purging_an_expired_parent_does_not_block_on_its_reply():
    """A reply routinely outlives the message it answers.

    With the FK left at the default RESTRICT, purging the expired parent raises
    a foreign-key violation, which fails the whole purge batch and lets expired
    mail accumulate forever -- turning this table back into the
    permanently-open note store it was built to replace. ON DELETE SET NULL
    keeps the reply and drops only the thread pointer; reply_depth is already
    materialised, so the loop bound survives the parent.
    """
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM lease_plane.topic_messages")
        now = datetime.now(UTC)

        parent_id = await _insert_message(
            conn, created_at=now, expires_at=now - timedelta(seconds=1)
        )
        child_id = await _insert_message(
            conn,
            created_at=now,
            expires_at=now + timedelta(hours=1),
            response_to_id=parent_id,
            reply_depth=1,
        )

        # Exactly the statement Repo.purge_expired_messages/1 issues.
        await conn.execute(
            """
            WITH doomed AS (
              SELECT message_id FROM lease_plane.topic_messages
              WHERE expires_at <= now()
              ORDER BY expires_at
              LIMIT 1000
            )
            DELETE FROM lease_plane.topic_messages m
            USING doomed d
            WHERE m.message_id = d.message_id
            """
        )

        row = await conn.fetchrow(
            "SELECT response_to_id, reply_depth FROM lease_plane.topic_messages "
            "WHERE message_id = $1",
            child_id,
        )
        assert row is not None, "the reply must survive its parent's expiry"
        assert row["response_to_id"] is None
        assert row["reply_depth"] == 1

        assert await conn.fetchval(
            "SELECT count(*) FROM lease_plane.topic_messages WHERE message_id = $1",
            parent_id,
        ) == 0
    finally:
        await conn.execute("DELETE FROM lease_plane.topic_messages")
        await conn.close()


@pytest.mark.asyncio
async def test_069_topic_check_enforces_canonical_form_not_just_the_prefix():
    """`Repo.send_message/1` is public and does not canonicalize.

    A prefix-only CHECK would let a non-HTTP caller create
    `topic:/Revenue-Engine` beside `topic:/revenue-engine` — two mailboxes for
    one conversation, the exact split-brain the canonicalizer prevents at the
    HTTP edge.
    """
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM lease_plane.topic_messages")

        ok_id = await _insert_message(conn, topic="topic:/revenue-engine")
        assert ok_id is not None

        for bad in (
            "topic:/Revenue-Engine",   # uppercase
            "topic:/",                 # empty key
            "topic://",                # empty key, doubled slash
            "topic:/a b",              # whitespace
            "topic:/a#b",              # reserved
            "topic:/a&b",              # reserved
            "topic:/a?b",              # reserved
            "topic:/trailing/",        # trailing slash
            "channel-resource-agent",  # not a topic at all
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert_message(conn, topic=bad)
    finally:
        await conn.execute("DELETE FROM lease_plane.topic_messages")
        await conn.close()
