"""Integration tests for the Redis-retirement Phase 1 session-binding +
onboard-pin mirror tables (migration 051) and their DB mixin methods.

Runs against governance_test; skips if unavailable. The methods are inert in
production (nothing wires them yet) — these tests exercise them directly against
a real PostgreSQL so the durable mirror is proven before the dual-write PR.

See docs/proposals/redis-retirement-phase-1-plan.md.
"""

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    import asyncpg  # noqa: F401
except ImportError:
    pytest.skip("asyncpg not installed", allow_module_level=True)

from tests.test_db_utils import can_connect_to_test_db

if not can_connect_to_test_db():
    pytest.skip("governance_test database not available", allow_module_level=True)


@pytest_asyncio.fixture
async def backend(live_postgres_backend):
    """Alias for the conftest live_postgres_backend fixture (governance_test)."""
    return live_postgres_backend


def _uuid() -> str:
    return str(uuid.uuid4())


def _future(seconds: int = 3600) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _past(seconds: int = 3600) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# session_bindings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_then_get_roundtrips_rich_fields(backend):
    sk, au = "agent-" + _uuid()[:12], _uuid()
    outcome = await backend.upsert_session_binding(
        sk, au,
        public_agent_id="Claude_Code_20260628",
        display_agent_id="Claude_Code_20260628",
        api_key_hash="hash-abc",
        spawn_reason="new_session",
        bind_ip_ua="127.0.0.1:deadbe",
        trajectory_required=True,
        expires_at=_future(),
    )
    assert outcome == "inserted"

    row = await backend.get_session_binding(sk)
    assert row is not None
    assert isinstance(row, dict)  # B2: callers do row.get("bind_ip_ua")
    assert row["agent_uuid"] == au
    assert row["public_agent_id"] == "Claude_Code_20260628"
    assert row["api_key_hash"] == "hash-abc"
    assert row["spawn_reason"] == "new_session"
    assert row["bind_ip_ua"] == "127.0.0.1:deadbe"
    assert row["trajectory_required"] is True
    assert row["bind_count"] == 1


@pytest.mark.asyncio
async def test_same_uuid_reupsert_updates_and_bumps_bind_count(backend):
    sk, au = "agent-" + _uuid()[:12], _uuid()
    await backend.upsert_session_binding(sk, au, expires_at=_future())
    outcome = await backend.upsert_session_binding(sk, au, expires_at=_future())
    assert outcome == "updated"
    row = await backend.get_session_binding(sk)
    assert row["bind_count"] == 2


@pytest.mark.asyncio
async def test_mint_guard_blocks_divergent_uuid(backend):
    """S21-a: mint_guard must refuse to overwrite a binding for a different uuid."""
    sk, au1, au2 = "agent-" + _uuid()[:12], _uuid(), _uuid()
    assert await backend.upsert_session_binding(sk, au1, expires_at=_future()) == "inserted"

    outcome = await backend.upsert_session_binding(sk, au2, mint_guard=True, expires_at=_future())
    assert outcome == "blocked"

    row = await backend.get_session_binding(sk)
    assert row["agent_uuid"] == au1  # original binding intact


@pytest.mark.asyncio
async def test_corrective_write_overwrites_divergent_uuid(backend):
    """Without mint_guard, a corrective write may overwrite (authoritative source)."""
    sk, au1, au2 = "agent-" + _uuid()[:12], _uuid(), _uuid()
    await backend.upsert_session_binding(sk, au1, expires_at=_future())
    outcome = await backend.upsert_session_binding(sk, au2, mint_guard=False, expires_at=_future())
    assert outcome == "updated"
    row = await backend.get_session_binding(sk)
    assert row["agent_uuid"] == au2


@pytest.mark.asyncio
async def test_expired_binding_is_invisible(backend):
    sk, au = "agent-" + _uuid()[:12], _uuid()
    await backend.upsert_session_binding(sk, au, expires_at=_past())
    assert await backend.get_session_binding(sk) is None


@pytest.mark.asyncio
async def test_permanent_binding_null_expiry_visible(backend):
    sk, au = "agent-" + _uuid()[:12], _uuid()
    await backend.upsert_session_binding(sk, au, expires_at=None)
    row = await backend.get_session_binding(sk)
    assert row is not None and row["expires_at"] is None


@pytest.mark.asyncio
async def test_get_missing_binding_returns_none(backend):
    assert await backend.get_session_binding("agent-" + _uuid()[:12]) is None


@pytest.mark.asyncio
async def test_delete_session_binding(backend):
    sk, au = "agent-" + _uuid()[:12], _uuid()
    await backend.upsert_session_binding(sk, au, expires_at=_future())
    assert await backend.delete_session_binding(sk) is True
    assert await backend.get_session_binding(sk) is None
    assert await backend.delete_session_binding(sk) is False  # already gone


@pytest.mark.asyncio
async def test_uuid_check_rejects_display_label(backend):
    """The agent_uuid CHECK enforces 'proof not label' at the schema layer."""
    sk = "agent-" + _uuid()[:12]
    with pytest.raises(Exception):  # asyncpg.CheckViolationError
        await backend.upsert_session_binding(sk, "Claude_Code_20260628", expires_at=_future())


# ---------------------------------------------------------------------------
# onboard_pins
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_and_lookup_onboard_pin(backend):
    fp, au, csid = f"ua:{_uuid()[:6]}|unknown|claude", _uuid(), "agent-" + _uuid()[:12]
    assert await backend.set_onboard_pin_pg(fp, au, csid) is True
    assert await backend.lookup_onboard_pin_pg(fp) == csid


@pytest.mark.asyncio
async def test_onboard_pin_if_absent_nx_semantics(backend):
    """if_absent must not displace an existing pin (subagent NX-claim)."""
    fp = f"ua:{_uuid()[:6]}|unknown"
    csid1, csid2 = "agent-" + _uuid()[:12], "agent-" + _uuid()[:12]
    assert await backend.set_onboard_pin_pg(fp, _uuid(), csid1, if_absent=True) is True
    assert await backend.set_onboard_pin_pg(fp, _uuid(), csid2, if_absent=True) is False
    assert await backend.lookup_onboard_pin_pg(fp) == csid1  # first claim wins


@pytest.mark.asyncio
async def test_onboard_pin_upsert_overwrites(backend):
    fp = f"ua:{_uuid()[:6]}|claude"
    csid1, csid2 = "agent-" + _uuid()[:12], "agent-" + _uuid()[:12]
    await backend.set_onboard_pin_pg(fp, _uuid(), csid1)
    await backend.set_onboard_pin_pg(fp, _uuid(), csid2)  # default upsert
    assert await backend.lookup_onboard_pin_pg(fp) == csid2


@pytest.mark.asyncio
async def test_expired_onboard_pin_is_invisible(backend):
    fp, au, csid = f"ua:{_uuid()[:6]}", _uuid(), "agent-" + _uuid()[:12]
    # negative TTL -> already expired
    await backend.set_onboard_pin_pg(fp, au, csid, ttl_seconds=-10)
    assert await backend.lookup_onboard_pin_pg(fp) is None


@pytest.mark.asyncio
async def test_lookup_missing_onboard_pin_returns_none(backend):
    assert await backend.lookup_onboard_pin_pg(f"ua:{_uuid()[:6]}") is None
