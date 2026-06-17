"""Lease-plane liveness signal for ephemeral agents (has_live_agent_lease).

Pins the consumer contract: surface_id is ``agent:/<uuid>`` (colon-SLASH — must
match the producer + the BEAM canonical grammar), liveness keys on
``expires_at``, and every failure path fails OPEN (False) so the archival gate
falls back to its other signals — this can only ADD protection, never remove it.
"""

import pytest
from unittest.mock import AsyncMock, patch

from src.mcp_handlers.identity import process_binding as pb


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


class _DbWithConn:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


@pytest.mark.asyncio
async def test_missing_uuid_is_false():
    assert await pb.has_live_agent_lease(None) is False
    assert await pb.has_live_agent_lease("") is False


@pytest.mark.asyncio
async def test_live_lease_is_true():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=True)
    with patch("src.db.get_db", return_value=_DbWithConn(conn)):
        assert await pb.has_live_agent_lease("uuid-1") is True


@pytest.mark.asyncio
async def test_no_lease_is_false():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=False)
    with patch("src.db.get_db", return_value=_DbWithConn(conn)):
        assert await pb.has_live_agent_lease("uuid-1") is False


@pytest.mark.asyncio
async def test_surface_id_uses_colon_slash():
    # Producer/consumer contract: the BEAM grammar is `agent:/<uuid>` (slash),
    # not `agent:<uuid>`. A mismatch here means the gate silently never matches
    # the producer's leases.
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=False)
    with patch("src.db.get_db", return_value=_DbWithConn(conn)):
        await pb.has_live_agent_lease("abc-123")
    assert conn.fetchval.await_args.args[1] == "agent:/abc-123"


@pytest.mark.asyncio
async def test_db_error_fails_open_false():
    with patch("src.db.get_db", side_effect=RuntimeError("db down")):
        assert await pb.has_live_agent_lease("uuid-1") is False


@pytest.mark.asyncio
async def test_query_error_fails_open_false():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=RuntimeError("query boom"))
    with patch("src.db.get_db", return_value=_DbWithConn(conn)):
        assert await pb.has_live_agent_lease("uuid-1") is False
