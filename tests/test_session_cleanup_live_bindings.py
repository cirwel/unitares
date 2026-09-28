"""session_cleanup_task must not delete a Redis session binding that is still live.

A ``core.sessions`` row expires SESSION_TTL_HOURS after bind unless a lookup
reaches PostgreSQL; a lookup served from Redis slides only the Redis key's TTL.
The cleanup used to delete ``session:<key>`` for every expired row, so a client
that stayed active through Redis lost its binding at the next pass and was then
refused as ``pg_session_missing`` on every call (live 2026-09-01 to 09-07: three
discord-bridge keys, each missing on every 30-second HUD poll until the bridge
restarted).

Redis runs as fakeredis, so TTL semantics (-2 absent, -1 no expiry, >0 live)
are the real protocol's, not a mock's.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

fakeredis = pytest.importorskip("fakeredis")
import fakeredis.aioredis  # noqa: E402

from src import background_tasks  # noqa: E402


class _FakeConn:
    def __init__(self, expired_keys):
        self.expired_keys = list(expired_keys)
        self.executed: list[str] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch(self, sql):
        assert "expires_at <= now()" in sql
        return [{"session_id": k} for k in self.expired_keys]

    async def execute(self, sql):
        self.executed.append(sql)
        return f"DELETE {len(self.expired_keys)}"


class _FakeDB:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.fixture
def fake_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def wire(monkeypatch, fake_redis):
    """Point the cleanup at a fake PG connection and at fakeredis."""

    def _wire(expired_keys, redis_client=None):
        conn = _FakeConn(expired_keys)
        import src.db as db_module
        import src.cache.redis_client as redis_module

        monkeypatch.setattr(db_module, "get_db", lambda: _FakeDB(conn))
        client = fake_redis if redis_client is None else redis_client

        async def _get_redis():
            return client

        monkeypatch.setattr(redis_module, "get_redis", _get_redis)
        return conn

    return _wire


@pytest.mark.asyncio
async def test_live_binding_survives_its_expired_pg_row(wire, fake_redis):
    # The client kept resolving through Redis, so its key's TTL was slid
    # forward while the PG row ran out.
    await fake_redis.setex("session:agent-live-0001", 80_000, '{"agent_id": "u"}')
    conn = wire(["agent-live-0001"])

    pg_deleted, redis_deleted, kept = await background_tasks._session_cleanup_pass()

    assert await fake_redis.exists("session:agent-live-0001") == 1
    assert (pg_deleted, redis_deleted, kept) == (1, 0, 1)
    # The PG side is unchanged: the expired row is still deleted.
    assert conn.executed == ["DELETE FROM core.sessions WHERE expires_at <= now()"]


@pytest.mark.asyncio
async def test_key_without_expiry_is_still_deleted_as_an_orphan(wire, fake_redis):
    await fake_redis.set("session:agent-orphan-01", '{"agent_id": "u"}')
    wire(["agent-orphan-01"])

    pg_deleted, redis_deleted, kept = await background_tasks._session_cleanup_pass()

    assert await fake_redis.exists("session:agent-orphan-01") == 0
    assert (pg_deleted, redis_deleted, kept) == (1, 1, 0)


@pytest.mark.asyncio
async def test_mixed_pass_counts_each_kind_once(wire, fake_redis):
    await fake_redis.setex("session:agent-live-0002", 3_600, "{}")
    await fake_redis.set("session:agent-orphan-02", "{}")
    # agent-gone-00001 has no Redis key at all (expired on its own).
    wire(["agent-live-0002", "agent-orphan-02", "agent-gone-00001"])

    pg_deleted, redis_deleted, kept = await background_tasks._session_cleanup_pass()

    assert (pg_deleted, redis_deleted, kept) == (3, 1, 1)
    assert await fake_redis.exists("session:agent-live-0002") == 1
    assert await fake_redis.exists("session:agent-orphan-02") == 0


@pytest.mark.asyncio
async def test_unreadable_ttl_leaves_the_key_alone(wire):
    class _TTLFails:
        def __init__(self):
            self.deleted: list[str] = []

        async def ttl(self, key):
            raise ConnectionError("redis hiccup")

        async def delete(self, key):
            # Recorded, not raised: the cleanup swallows exceptions per key,
            # so a raise here would hide a delete that should not happen.
            self.deleted.append(key)
            return 1

    client = _TTLFails()
    wire(["agent-unknown-1"], redis_client=client)

    pg_deleted, redis_deleted, kept = await background_tasks._session_cleanup_pass()

    assert client.deleted == []
    assert (pg_deleted, redis_deleted, kept) == (1, 0, 0)


@pytest.mark.asyncio
async def test_pg_failure_touches_no_redis_key(wire, fake_redis, monkeypatch):
    await fake_redis.set("session:agent-orphan-03", "{}")
    wire(["agent-orphan-03"])

    import src.db as db_module

    def _boom():
        raise RuntimeError("pg down")

    monkeypatch.setattr(db_module, "get_db", _boom)

    assert await background_tasks._session_cleanup_pass() == (0, 0, 0)
    assert await fake_redis.exists("session:agent-orphan-03") == 1
