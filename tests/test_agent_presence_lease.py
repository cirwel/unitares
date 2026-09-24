"""Producer side of ephemeral-agent liveness lease (agent_presence_lease).

Pins the lease lifecycle used by onboard and check-in: no-op without uuid/client,
acquire-then-cache, heartbeat-when-cached, re-acquire-on-heartbeat-failure, and
never-raises (fire-and-forget must never affect the caller). The SDK request
models are guarded-imported (None in this isolated env), so tests monkeypatch
them + the client factory with fakes.
"""

from types import SimpleNamespace

import pytest

from src.mcp_handlers.identity import agent_presence_lease as apl


@pytest.fixture(autouse=True)
def _clear_cache():
    apl._lease_ids.clear()
    apl._released_at.clear()
    yield
    apl._lease_ids.clear()
    apl._released_at.clear()


def _fake_req(**kw):
    return SimpleNamespace(**kw)


class _FakeClient:
    def __init__(self):
        self.acquired = []
        self.heartbeats = []
        self.heartbeat_should_fail = False
        self.heartbeat_ok = True
        self.acquire_lease_id = "lease-123"
        self.identity_proofs = []
        self.releases = []
        self.on_acquire = None

    def acquire(self, req, *, identity_proof=None):
        self.acquired.append(req)
        if self.on_acquire is not None:
            self.on_acquire()
        self.identity_proofs.append(identity_proof)
        return SimpleNamespace(lease_id=self.acquire_lease_id)

    def heartbeat(self, req, *, identity_proof=None):
        if self.heartbeat_should_fail:
            raise RuntimeError("expired/reaped")
        self.heartbeats.append(req)
        self.identity_proofs.append(identity_proof)
        return SimpleNamespace(ok=self.heartbeat_ok)

    def release(self, req, *, identity_proof=None):
        self.releases.append(req)
        self.identity_proofs.append(identity_proof)
        return SimpleNamespace(ok=True)


def _patch_models(monkeypatch, client):
    monkeypatch.setattr(apl, "_make_client", lambda: client)
    monkeypatch.setattr(apl, "AcquireRequest", _fake_req)
    monkeypatch.setattr(apl, "HeartbeatRequest", _fake_req)
    monkeypatch.setattr(apl, "ReleaseRequest", _fake_req)
    monkeypatch.setattr(
        apl,
        "_mint_presence_attestation",
        lambda agent_uuid, path, request: f"lat.v1.{agent_uuid}.{path.rsplit('/', 1)[-1]}",
    )


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
    assert client.identity_proofs == ["lat.v1.uuid-1.acquire"]


@pytest.mark.asyncio
async def test_heartbeat_when_cached(monkeypatch):
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    apl._lease_ids["uuid-1"] = "lease-xyz"
    await apl.heartbeat_agent_presence("uuid-1")
    assert len(client.heartbeats) == 1
    assert client.heartbeats[0].lease_id == "lease-xyz"
    assert len(client.acquired) == 0                  # did NOT re-acquire
    assert client.identity_proofs == ["lat.v1.uuid-1.heartbeat"]


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
async def test_reacquire_when_heartbeat_returns_typed_failure(monkeypatch):
    client = _FakeClient()
    client.heartbeat_ok = False
    _patch_models(monkeypatch, client)
    apl._lease_ids["uuid-1"] = "stale-lease"
    await apl.heartbeat_agent_presence("uuid-1")
    assert len(client.heartbeats) == 1
    assert len(client.acquired) == 1
    assert apl._lease_ids["uuid-1"] == "lease-123"


@pytest.mark.asyncio
async def test_never_raises_on_client_error(monkeypatch):
    class _BoomClient:
        def acquire(self, req, *, identity_proof=None):
            raise RuntimeError("boom")

        def heartbeat(self, req, *, identity_proof=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(apl, "_make_client", lambda: _BoomClient())
    monkeypatch.setattr(apl, "AcquireRequest", _fake_req)
    monkeypatch.setattr(apl, "HeartbeatRequest", _fake_req)
    # Must swallow everything — a lease failure can never break a check-in.
    await apl.heartbeat_agent_presence("uuid-1")
    assert "uuid-1" not in apl._lease_ids


# --- clean-exit release -------------------------------------------------------


@pytest.mark.asyncio
async def test_release_hands_back_the_cached_lease(monkeypatch):
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    apl._lease_ids["uuid-1"] = "lease-abc"

    result = await apl.release_agent_presence("uuid-1")

    assert result == {"released": True, "reason": "released"}
    assert [r.lease_id for r in client.releases] == ["lease-abc"]
    assert client.releases[0].release_reason == "normal"
    assert client.identity_proofs[-1] == "lat.v1.uuid-1.release"
    assert "uuid-1" not in apl._lease_ids


@pytest.mark.asyncio
async def test_release_finds_the_lease_after_a_server_restart(monkeypatch):
    client = _FakeClient()
    _patch_models(monkeypatch, client)

    async def _lookup(agent_uuid):
        return "lease-from-db"

    monkeypatch.setattr(apl, "_lookup_live_lease_id", _lookup)

    result = await apl.release_agent_presence("uuid-1")

    assert result["released"] is True
    assert [r.lease_id for r in client.releases] == ["lease-from-db"]


@pytest.mark.asyncio
async def test_release_without_a_live_lease_reports_it(monkeypatch):
    client = _FakeClient()
    _patch_models(monkeypatch, client)

    async def _lookup(agent_uuid):
        return None

    monkeypatch.setattr(apl, "_lookup_live_lease_id", _lookup)

    assert await apl.release_agent_presence("uuid-1") == {
        "released": False,
        "reason": "no_live_lease",
    }
    assert client.releases == []


@pytest.mark.asyncio
async def test_release_without_identity_is_a_no_op():
    assert await apl.release_agent_presence(None) == {
        "released": False,
        "reason": "no_identity",
    }


@pytest.mark.asyncio
async def test_heartbeat_scheduled_before_release_does_not_reacquire(monkeypatch):
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    scheduled_at = apl.time.monotonic()
    await apl.release_agent_presence("uuid-1")

    await apl.heartbeat_agent_presence("uuid-1", "sess-1", scheduled_at)

    assert client.acquired == []
    assert "uuid-1" not in apl._lease_ids


@pytest.mark.asyncio
async def test_heartbeat_scheduled_after_release_proceeds(monkeypatch):
    """A later session resuming the same identity keeps its presence."""
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    await apl.release_agent_presence("uuid-1")

    await apl.heartbeat_agent_presence("uuid-1", "sess-2", apl.time.monotonic())

    assert len(client.acquired) == 1
    assert apl._lease_ids["uuid-1"] == "lease-123"


@pytest.mark.asyncio
async def test_acquire_in_flight_during_release_is_handed_back(monkeypatch):
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    scheduled_at = apl.time.monotonic()
    # The session ends while this heartbeat's acquire is on the wire.
    client.on_acquire = lambda: apl._released_at.__setitem__(
        "uuid-1", apl.time.monotonic()
    )

    await apl.heartbeat_agent_presence("uuid-1", "sess-1", scheduled_at)

    assert [r.lease_id for r in client.releases] == ["lease-123"]
    assert "uuid-1" not in apl._lease_ids
