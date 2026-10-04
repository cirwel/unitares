"""A substrate-attested PATH 0 resume re-records the session's bind fingerprint.

A resident that moves from HTTP to the UDS socket keeps its ``agent-{uuid12}``
session, whose binding still carries the HTTP fingerprint. Every later UDS call
then reads as a fingerprint mismatch and broadcasts a false
``identity_hijack_suspected``. These tests pin that an accepted attestation
refreshes the fingerprint on bindings for the same UUID, and touches nothing
else.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_handlers.context import (
    SessionSignals,
    reset_session_signals,
    set_session_signals,
)
from src.substrate.verification import VerificationResult

_UUID = "ffffffff-1111-2222-3333-444444444444"
_OTHER = "eeeeeeee-1111-2222-3333-444444444444"
_KEY = "agent-ffffffff-111"
_HTTP_FP = "127.0.0.1:4b594d"
_UDS_FP = "unknown:4b594d"


@pytest.fixture
def _uds_signals():
    token = set_session_signals(SessionSignals(peer_pid=12345, ip_ua_fingerprint=_UDS_FP))
    yield
    reset_session_signals(token)


@pytest.fixture
def _clean_maps():
    from src.mcp_handlers.identity import shared
    saved = (dict(shared._bind_fingerprints), dict(shared._session_identities))
    shared._bind_fingerprints.pop(_KEY, None)
    shared._session_identities.pop(_KEY, None)
    yield shared
    shared._bind_fingerprints.clear()
    shared._bind_fingerprints.update(saved[0])
    shared._session_identities.clear()
    shared._session_identities.update(saved[1])


def _drop_task(coro, name=None):
    coro.close()


def test_refresh_replaces_stale_in_memory_fingerprint(_uds_signals, _clean_maps):
    from src.mcp_handlers.identity.persistence import schedule_attested_bind_fingerprint_refresh

    _clean_maps._bind_fingerprints[_KEY] = _HTTP_FP
    _clean_maps._session_identities[_KEY] = {"bound_agent_id": _UUID}
    with patch("src.background_tasks.create_tracked_task", side_effect=_drop_task) as sched:
        schedule_attested_bind_fingerprint_refresh(_KEY, _UUID)
    assert _clean_maps._bind_fingerprints[_KEY] == _UDS_FP
    sched.assert_called_once()


def test_refresh_leaves_another_agents_binding_alone(_uds_signals, _clean_maps):
    from src.mcp_handlers.identity.persistence import schedule_attested_bind_fingerprint_refresh

    _clean_maps._bind_fingerprints[_KEY] = _HTTP_FP
    _clean_maps._session_identities[_KEY] = {"bound_agent_id": _OTHER}
    with patch("src.background_tasks.create_tracked_task", side_effect=_drop_task):
        schedule_attested_bind_fingerprint_refresh(_KEY, _UUID)
    assert _clean_maps._bind_fingerprints[_KEY] == _HTTP_FP


def test_refresh_without_a_current_fingerprint_does_nothing(_clean_maps):
    from src.mcp_handlers.identity.persistence import schedule_attested_bind_fingerprint_refresh

    token = set_session_signals(SessionSignals(peer_pid=12345))
    try:
        _clean_maps._bind_fingerprints[_KEY] = _HTTP_FP
        with patch("src.background_tasks.create_tracked_task") as sched:
            schedule_attested_bind_fingerprint_refresh(_KEY, _UUID)
    finally:
        reset_session_signals(token)
    assert _clean_maps._bind_fingerprints[_KEY] == _HTTP_FP
    sched.assert_not_called()


fakeredis = pytest.importorskip("fakeredis")
import fakeredis.aioredis  # noqa: E402

_SLOT = f"session:{_KEY}"


def _slot(agent_id, fp):
    return json.dumps({
        "agent_id": agent_id, "bound_at": "2026-09-26T21:16:38+00:00",
        "spawn_reason": "explicit", "bind_ip_ua": fp,
    })


def _fake(server=None):
    return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)


@pytest.mark.asyncio
async def test_redis_slot_takes_new_fingerprint_and_keeps_the_rest():
    from src.mcp_handlers.identity.persistence import _refresh_redis_bind_fingerprint

    redis = _fake()
    await redis.set(_SLOT, _slot(_UUID, _HTTP_FP), ex=86400)
    with patch("src.cache.redis_client.get_redis", new=AsyncMock(return_value=redis)):
        await _refresh_redis_bind_fingerprint(_KEY, _UUID, _UDS_FP)
    data = json.loads(await redis.get(_SLOT))
    assert data["bind_ip_ua"] == _UDS_FP
    assert data["spawn_reason"] == "explicit"
    assert data["bound_at"] == "2026-09-26T21:16:38+00:00"
    assert 0 < await redis.ttl(_SLOT) <= 86400


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [_slot(_OTHER, _HTTP_FP), _slot(_UUID, _UDS_FP), None])
async def test_redis_slot_untouched_when_foreign_current_or_absent(stored):
    from src.mcp_handlers.identity.persistence import _refresh_redis_bind_fingerprint

    redis = _fake()
    if stored is not None:
        await redis.set(_SLOT, stored)
    with patch("src.cache.redis_client.get_redis", new=AsyncMock(return_value=redis)):
        await _refresh_redis_bind_fingerprint(_KEY, _UUID, _UDS_FP)
    assert await redis.get(_SLOT) == stored


@pytest.mark.asyncio
async def test_redis_bind_landing_mid_refresh_is_not_reverted():
    """A rebind between the read and the write wins; the stale snapshot is dropped."""
    from src.mcp_handlers.identity.persistence import _refresh_redis_bind_fingerprint

    server = fakeredis.FakeServer()
    redis, other = _fake(server), _fake(server)
    await redis.set(_SLOT, _slot(_UUID, _HTTP_FP))
    concurrent = _slot(_OTHER, "10.0.0.9:aaaaaa")

    class _Racing:
        def pipeline(self, transaction=True):
            pipe = redis.pipeline(transaction=transaction)
            real_get = pipe.get

            async def get(key):
                value = await real_get(key)
                await other.set(key, concurrent)
                return value

            pipe.get = get
            return pipe

    with patch("src.cache.redis_client.get_redis", new=AsyncMock(return_value=_Racing())):
        await _refresh_redis_bind_fingerprint(_KEY, _UUID, _UDS_FP)
    assert await redis.get(_SLOT) == concurrent


def test_fallback_cache_refreshed_without_redis(_uds_signals, _clean_maps):
    from src.cache import session_cache as session_cache_mod
    from src.mcp_handlers.identity.persistence import schedule_attested_bind_fingerprint_refresh

    with patch.dict(session_cache_mod._fallback_cache, {
        _KEY: {"agent_id": _UUID, "bind_ip_ua": _HTTP_FP},
        "agent-eeeeeeee-111": {"agent_id": _OTHER, "bind_ip_ua": _HTTP_FP},
    }), patch("src.background_tasks.create_tracked_task", side_effect=_drop_task):
        schedule_attested_bind_fingerprint_refresh(_KEY, _UUID)
        assert session_cache_mod._fallback_cache[_KEY]["bind_ip_ua"] == _UDS_FP
        schedule_attested_bind_fingerprint_refresh("agent-eeeeeeee-111", _UUID)
        assert session_cache_mod._fallback_cache["agent-eeeeeeee-111"]["bind_ip_ua"] == _HTTP_FP


@pytest.mark.asyncio
async def test_pg_mirror_refreshed_even_when_redis_fails():
    from src.mcp_handlers.identity import persistence

    db = MagicMock(refresh_session_binding_fingerprint=AsyncMock(return_value=True))
    with patch.object(
        persistence, "_refresh_redis_bind_fingerprint",
        new=AsyncMock(side_effect=RuntimeError("redis down")),
    ), patch.object(persistence, "get_db", return_value=db):
        await persistence._refresh_bind_fingerprint_stores(_KEY, _UUID, _UDS_FP)
    db.refresh_session_binding_fingerprint.assert_awaited_once_with(_KEY, _UUID, _UDS_FP)


@pytest.mark.asyncio
async def test_pg_update_is_scoped_to_the_same_agent():
    from src.db.mixins.session import SessionMixin

    conn = MagicMock(execute=AsyncMock(return_value="UPDATE 1"))
    acquire = MagicMock()
    acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    backend = MagicMock(acquire=acquire)

    updated = await SessionMixin.refresh_session_binding_fingerprint(
        backend, _KEY, _UUID, _UDS_FP,
    )
    assert updated is True
    sql, *params = conn.execute.await_args.args
    assert "SET bind_ip_ua = $3" in sql
    assert "agent_uuid = $2" in sql
    assert params == [_KEY, _UUID, _UDS_FP]


async def _resume(verification, signals):
    from src.mcp_handlers.identity.handlers import handle_identity_adapter

    token = set_session_signals(signals) if signals else None
    try:
        fake_server = MagicMock(monitors={_UUID: MagicMock()}, agent_metadata={})
        with patch(
            "src.substrate.handler_gate.verify_substrate_at_resume",
            new=AsyncMock(return_value=verification),
        ), patch(
            "src.mcp_handlers.shared.get_mcp_server", return_value=fake_server,
        ), patch(
            "src.mcp_handlers.identity.persistence.schedule_attested_bind_fingerprint_refresh",
        ) as refresh:
            result = await handle_identity_adapter({"agent_uuid": _UUID, "resume": True})
    finally:
        if token is not None:
            reset_session_signals(token)
    return json.loads(result[0].text), refresh


@pytest.mark.asyncio
async def test_accepted_attestation_refreshes_the_stable_session(monkeypatch):
    monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "strict")
    data, refresh = await _resume(
        VerificationResult(accepted=True, reason="substrate-claim verified"),
        SessionSignals(peer_pid=12345, ip_ua_fingerprint=_UDS_FP),
    )
    assert data.get("success") is True
    refresh.assert_called_once_with(_KEY, _UUID)


@pytest.mark.asyncio
async def test_rejected_attestation_refreshes_nothing(monkeypatch):
    monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "strict")
    data, refresh = await _resume(
        VerificationResult(accepted=False, reason="label mismatch", failure_code="label_mismatch"),
        SessionSignals(peer_pid=12345, ip_ua_fingerprint=_UDS_FP),
    )
    assert data.get("success") is False
    refresh.assert_not_called()


@pytest.mark.asyncio
async def test_http_resume_refreshes_nothing(monkeypatch):
    monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "off")
    data, refresh = await _resume(
        VerificationResult(accepted=True, reason="unused"), None,
    )
    assert data.get("success") is True
    refresh.assert_not_called()
