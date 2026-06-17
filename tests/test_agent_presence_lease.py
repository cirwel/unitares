"""Producer side of ephemeral-agent liveness lease (agent_presence_lease).

Pins the check-in-path lease lifecycle: no-op without uuid/client, acquire-then-
cache, heartbeat-when-cached, re-acquire-on-heartbeat-failure, and never-raises
(fire-and-forget must never affect the check-in). The SDK request models are
guarded-imported (None in this isolated env), so tests monkeypatch them + the
client factory with fakes.
"""

from types import SimpleNamespace

import pytest

from src.mcp_handlers.identity import agent_presence_lease as apl


@pytest.fixture(autouse=True)
def _clear_cache():
    apl._lease_ids.clear()
    yield
    apl._lease_ids.clear()


def _fake_req(**kw):
    return SimpleNamespace(**kw)


class _FakeClient:
    def __init__(self):
        self.acquired = []
        self.heartbeats = []
        self.heartbeat_should_fail = False
        self.acquire_lease_id = "lease-123"

    def acquire(self, req):
        self.acquired.append(req)
        return SimpleNamespace(lease_id=self.acquire_lease_id)

    def heartbeat(self, req):
        if self.heartbeat_should_fail:
            raise RuntimeError("expired/reaped")
        self.heartbeats.append(req)
        return SimpleNamespace(ok=True)


def _patch_models(monkeypatch, client):
    monkeypatch.setattr(apl, "_make_client", lambda: client)
    monkeypatch.setattr(apl, "AcquireRequest", _fake_req)
    monkeypatch.setattr(apl, "HeartbeatRequest", _fake_req)


@pytest.mark.asyncio
async def test_no_uuid_noop():
    await apl.heartbeat_agent_presence(None)
    await apl.heartbeat_agent_presence("")
    assert apl._lease_ids == {}


@pytest.mark.asyncio
async def test_no_client_noop(monkeypatch):
    monkeypatch.setattr(apl, "_make_client", lambda: None)
    await apl.heartbeat_agent_presence("uuid-1")
    assert apl._lease_ids == {}


@pytest.mark.asyncio
async def test_acquire_then_cache(monkeypatch):
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    await apl.heartbeat_agent_presence("uuid-1", "sess-1")
    assert apl._lease_ids["uuid-1"] == "lease-123"
    assert len(client.acquired) == 1
    assert len(client.heartbeats) == 0
    req = client.acquired[0]
    assert req.surface_id == "agent:/uuid-1"          # colon-slash contract
    assert req.holder_kind == "remote_heartbeat"
    assert req.holder_class == "process_instance"
    assert req.ttl_s == apl._PRESENCE_TTL_S
    assert req.audit_session == "sess-1"


@pytest.mark.asyncio
async def test_heartbeat_when_cached(monkeypatch):
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    apl._lease_ids["uuid-1"] = "lease-xyz"
    await apl.heartbeat_agent_presence("uuid-1")
    assert len(client.heartbeats) == 1
    assert client.heartbeats[0].lease_id == "lease-xyz"
    assert len(client.acquired) == 0                  # did NOT re-acquire


@pytest.mark.asyncio
async def test_reacquire_on_heartbeat_failure(monkeypatch):
    client = _FakeClient()
    client.heartbeat_should_fail = True
    _patch_models(monkeypatch, client)
    apl._lease_ids["uuid-1"] = "stale-lease"
    await apl.heartbeat_agent_presence("uuid-1")
    # heartbeat tried + failed -> dropped -> re-acquired with a fresh id
    assert len(client.acquired) == 1
    assert apl._lease_ids["uuid-1"] == "lease-123"


@pytest.mark.asyncio
async def test_never_raises_on_client_error(monkeypatch):
    class _BoomClient:
        def acquire(self, req):
            raise RuntimeError("boom")

        def heartbeat(self, req):
            raise RuntimeError("boom")

    monkeypatch.setattr(apl, "_make_client", lambda: _BoomClient())
    monkeypatch.setattr(apl, "AcquireRequest", _fake_req)
    monkeypatch.setattr(apl, "HeartbeatRequest", _fake_req)
    # Must swallow everything — a lease failure can never break a check-in.
    await apl.heartbeat_agent_presence("uuid-1")
    assert "uuid-1" not in apl._lease_ids
