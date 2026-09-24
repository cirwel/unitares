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
    apl._released_sessions.clear()
    apl._locks.clear()
    apl._lease_sessions.clear()
    apl._last_sweep = 0.0
    apl._touched.clear()
    yield
    apl._lease_ids.clear()
    apl._released_at.clear()
    apl._released_sessions.clear()
    apl._locks.clear()
    apl._lease_sessions.clear()
    apl._last_sweep = 0.0
    apl._touched.clear()


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
        self.sdk_shape = False
        self.idempotent = False

    def acquire(self, req, *, identity_proof=None):
        self.acquired.append(req)
        if self.on_acquire is not None:
            self.on_acquire()
        self.identity_proofs.append(identity_proof)
        if self.sdk_shape:
            return SimpleNamespace(
                ok=True,
                lease=SimpleNamespace(lease_id=self.acquire_lease_id),
                idempotent=self.idempotent,
            )
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

    result = await apl.release_agent_presence("uuid-1", ("sess-x",))

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
        return "lease-from-db", "sess-x"

    monkeypatch.setattr(apl, "_lookup_live_lease", _lookup)

    result = await apl.release_agent_presence("uuid-1", ("sess-x",))

    assert result["released"] is True
    assert [r.lease_id for r in client.releases] == ["lease-from-db"]


@pytest.mark.asyncio
async def test_release_without_a_live_lease_reports_it(monkeypatch):
    client = _FakeClient()
    _patch_models(monkeypatch, client)

    async def _lookup(agent_uuid):
        return None, None

    monkeypatch.setattr(apl, "_lookup_live_lease", _lookup)

    assert await apl.release_agent_presence("uuid-1", ("sess-x",)) == {
        "released": False,
        "reason": "no_live_lease",
    }
    assert client.releases == []


@pytest.mark.asyncio
async def test_release_reports_a_failed_lookup_as_retryable_not_absent(monkeypatch):
    """After a restart the cache is empty; a database failure during the lookup
    says nothing about whether a lease is live, so it must not read as
    no_live_lease (which the handler treats as a clean exit)."""
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    attempts = []

    def _get_db():
        attempts.append(True)
        raise ConnectionError("pool unavailable")

    monkeypatch.setattr("src.db.get_db", _get_db)

    result = await apl.release_agent_presence("uuid-1", ("sess-x",))

    assert result == {"released": False, "reason": "lease_lookup_failed", "retryable": True}
    assert len(attempts) == 2  # one retry before reporting
    assert client.releases == []
    # The exiting session stays suppressed, so its late check-in cannot
    # re-acquire before the client retries.
    assert "sess-x" in apl._released_sessions["uuid-1"]


@pytest.mark.asyncio
async def test_release_retries_a_transient_lookup_failure(monkeypatch):
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    calls = []

    async def _lookup(agent_uuid):
        calls.append(agent_uuid)
        if len(calls) == 1:
            raise apl.LeaseLookupFailed("transient")
        return "lease-from-db", "sess-x"

    monkeypatch.setattr(apl, "_lookup_live_lease", _lookup)

    result = await apl.release_agent_presence("uuid-1", ("sess-x",))

    assert result == {"released": True, "reason": "released"}
    assert [r.lease_id for r in client.releases] == ["lease-from-db"]


@pytest.mark.asyncio
async def test_release_without_identity_is_a_no_op():
    assert await apl.release_agent_presence(None, ("sess-x",)) == {
        "released": False,
        "reason": "no_identity",
    }


@pytest.mark.asyncio
async def test_heartbeat_scheduled_before_release_does_not_reacquire(monkeypatch):
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    scheduled_at = apl.time.monotonic()
    await apl.release_agent_presence("uuid-1", ("sess-1",))

    await apl.heartbeat_agent_presence("uuid-1", "sess-1", scheduled_at)

    assert client.acquired == []
    assert "uuid-1" not in apl._lease_ids


@pytest.mark.asyncio
async def test_heartbeat_scheduled_after_release_proceeds(monkeypatch):
    """A later session resuming the same identity keeps its presence."""
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    await apl.release_agent_presence("uuid-1", ("sess-x",))

    await apl.heartbeat_agent_presence("uuid-1", "sess-2", apl.time.monotonic())

    assert len(client.acquired) == 1
    assert apl._lease_ids["uuid-1"] == "lease-123"


@pytest.mark.asyncio
async def test_acquire_in_flight_during_release_is_handed_back(monkeypatch):
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    scheduled_at = apl.time.monotonic()
    # The session ends while this heartbeat's acquire is on the wire.
    def _session_ends():
        apl._released_at["uuid-1"] = apl.time.monotonic()
        apl._released_sessions["uuid-1"] = {"sess-1"}

    client.on_acquire = _session_ends

    await apl.heartbeat_agent_presence("uuid-1", "sess-1", scheduled_at)

    assert [r.lease_id for r in client.releases] == ["lease-123"]
    assert "uuid-1" not in apl._lease_ids


@pytest.mark.asyncio
async def test_late_checkin_from_the_releasing_session_does_not_reacquire(monkeypatch):
    """The host's final check-in can reach the server after its session-end
    release; that session must not resurrect the lease."""
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    await apl.release_agent_presence("uuid-1", ("sess-1",))

    await apl.heartbeat_agent_presence("uuid-1", "sess-1", apl.time.monotonic())

    assert client.acquired == []
    assert "uuid-1" not in apl._lease_ids


@pytest.mark.asyncio
async def test_release_without_a_session_id_leaves_the_ttl_in_charge(monkeypatch):
    """A fingerprint-bound caller has no session id to tombstone, so its late
    check-in could re-acquire; the release is refused rather than half-done."""
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    apl._lease_ids["uuid-1"] = "lease-abc"

    assert await apl.release_agent_presence("uuid-1") == {
        "released": False,
        "reason": "session_id_required",
    }
    assert client.releases == []
    assert apl._lease_ids["uuid-1"] == "lease-abc"


@pytest.mark.asyncio
async def test_resumed_session_heartbeat_waits_for_an_in_progress_release(monkeypatch):
    """A new session's heartbeat that arrives mid-release acquires only after the
    old row is released, so it is never left without a lease."""
    import asyncio

    client = _FakeClient()
    _patch_models(monkeypatch, client)
    apl._lease_ids["uuid-1"] = "old-lease"
    order = []
    gate = asyncio.Event()

    async def _slow_release(client_, agent_uuid, lease_id):
        order.append(("release", lease_id))
        await gate.wait()
        return True

    monkeypatch.setattr(apl, "_release_lease", _slow_release)

    release_task = asyncio.create_task(apl.release_agent_presence("uuid-1", ("sess-1",)))
    await asyncio.sleep(0)
    heartbeat_task = asyncio.create_task(
        apl.heartbeat_agent_presence("uuid-1", "sess-2", apl.time.monotonic())
    )
    await asyncio.sleep(0)
    assert client.acquired == []  # blocked behind the release
    gate.set()
    await release_task
    await heartbeat_task

    assert order == [("release", "old-lease")]
    assert len(client.acquired) == 1
    assert apl._lease_ids["uuid-1"] == "lease-123"


@pytest.mark.asyncio
async def test_release_leaves_a_lease_another_session_refreshed(monkeypatch):
    """A resumed session under the same identity heartbeat first; the old
    session's release must not free the lease the live session now holds."""
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    await apl.heartbeat_agent_presence("uuid-1", "sess-new", apl.time.monotonic())

    result = await apl.release_agent_presence("uuid-1", ("sess-old",))

    assert result == {"released": False, "reason": "held_by_other_session"}
    assert client.releases == []
    assert apl._lease_ids["uuid-1"] == "lease-123"


@pytest.mark.asyncio
async def test_release_after_restart_respects_the_acquiring_session(monkeypatch):
    client = _FakeClient()
    _patch_models(monkeypatch, client)

    async def _lookup(agent_uuid):
        return "lease-from-db", "sess-new"

    monkeypatch.setattr(apl, "_lookup_live_lease", _lookup)

    result = await apl.release_agent_presence("uuid-1", ("sess-old",))

    assert result["reason"] == "held_by_other_session"
    assert client.releases == []


@pytest.mark.asyncio
async def test_other_session_heartbeat_queued_before_release_still_proceeds(monkeypatch):
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    queued_at = apl.time.monotonic()
    await apl.release_agent_presence("uuid-1", ("sess-old",))

    await apl.heartbeat_agent_presence("uuid-1", "sess-new", queued_at)

    assert len(client.acquired) == 1
    assert apl._lease_ids["uuid-1"] == "lease-123"


@pytest.mark.asyncio
async def test_release_after_restart_leaves_a_renewed_lease_to_the_ttl(monkeypatch):
    client = _FakeClient()
    _patch_models(monkeypatch, client)

    async def _lookup(agent_uuid):
        return "lease-from-db", apl._HOLDER_UNKNOWN

    monkeypatch.setattr(apl, "_lookup_live_lease", _lookup)

    result = await apl.release_agent_presence("uuid-1", ("sess-old",))

    assert result == {"released": False, "reason": "holder_unknown"}
    assert client.releases == []


@pytest.mark.asyncio
async def test_sessionless_heartbeat_queued_before_release_is_dropped(monkeypatch):
    """With no session id to judge by, the release timestamp decides."""
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    queued_at = apl.time.monotonic()
    await apl.release_agent_presence("uuid-1", ("sess-1",))

    await apl.heartbeat_agent_presence("uuid-1", None, queued_at)

    assert client.acquired == []


@pytest.mark.asyncio
async def test_nameless_refresh_blocks_an_earlier_sessions_release(monkeypatch):
    """A refresh without a session id proves a live caller without naming it,
    so the session that acquired first cannot release the lease out from under it."""
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    await apl.heartbeat_agent_presence("uuid-1", "sess-a", apl.time.monotonic())
    await apl.heartbeat_agent_presence("uuid-1", None, apl.time.monotonic())

    result = await apl.release_agent_presence("uuid-1", ("sess-a",))

    assert result == {"released": False, "reason": "holder_unknown"}
    assert client.releases == []


@pytest.mark.asyncio
async def test_release_keeps_a_lease_while_another_session_is_live_in_either_order(monkeypatch):
    """B refreshes, then A's delayed heartbeat lands, then A exits: B is still
    live, so the shared lease stays."""
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    await apl.heartbeat_agent_presence("uuid-1", "sess-a", apl.time.monotonic())
    await apl.heartbeat_agent_presence("uuid-1", "sess-b", apl.time.monotonic())
    await apl.heartbeat_agent_presence("uuid-1", "sess-a", apl.time.monotonic())

    first = await apl.release_agent_presence("uuid-1", ("sess-a",))
    assert first == {"released": False, "reason": "held_by_other_session"}
    assert client.releases == []

    # When B exits too, nobody is left and the lease is freed.
    second = await apl.release_agent_presence("uuid-1", ("sess-b",))
    assert second == {"released": True, "reason": "released"}
    assert [r.lease_id for r in client.releases] == ["lease-123"]


@pytest.mark.asyncio
async def test_acquire_reads_the_sdk_nested_lease_id(monkeypatch):
    """The real AcquireOk nests the id under .lease; it must still be cached so
    later heartbeats renew instead of re-acquiring."""
    client = _FakeClient()
    client.sdk_shape = True
    _patch_models(monkeypatch, client)

    await apl.heartbeat_agent_presence("uuid-1", "sess-1", apl.time.monotonic())
    await apl.heartbeat_agent_presence("uuid-1", "sess-1", apl.time.monotonic())

    assert apl._lease_ids["uuid-1"] == "lease-123"
    assert len(client.acquired) == 1
    assert len(client.heartbeats) == 1


@pytest.mark.asyncio
async def test_idempotent_acquire_joins_the_existing_holders(monkeypatch):
    client = _FakeClient()
    client.sdk_shape = True
    _patch_models(monkeypatch, client)
    await apl.heartbeat_agent_presence("uuid-1", "sess-a", apl.time.monotonic())
    apl._lease_ids.clear()  # e.g. a process that never cached it
    client.idempotent = True

    await apl.heartbeat_agent_presence("uuid-1", "sess-b", apl.time.monotonic())

    assert set(apl._lease_sessions["uuid-1"]) == {"sess-a", "sess-b"}
    result = await apl.release_agent_presence("uuid-1", ("sess-a",))
    assert result == {"released": False, "reason": "held_by_other_session"}


@pytest.mark.asyncio
async def test_cold_idempotent_reacquire_keeps_the_persisted_holder(monkeypatch):
    """After a restart, B's idempotent reacquire returns A's record; A still
    counts, so B's release must not free the lease."""
    from datetime import datetime, timezone

    client = _FakeClient()
    client.sdk_shape = True
    client.idempotent = True
    _patch_models(monkeypatch, client)
    acquired = datetime(2026, 9, 24, tzinfo=timezone.utc)
    original = client.acquire

    def _acquire(req, *, identity_proof=None):
        result = original(req, identity_proof=identity_proof)
        result.lease.audit_session = "sess-a"
        result.lease.acquired_at = acquired
        result.lease.last_heartbeat_at = None
        return result

    client.acquire = _acquire

    await apl.heartbeat_agent_presence("uuid-1", "sess-b", apl.time.monotonic())

    assert set(apl._lease_sessions["uuid-1"]) == {"sess-a", "sess-b"}
    result = await apl.release_agent_presence("uuid-1", ("sess-b",))
    assert result == {"released": False, "reason": "held_by_other_session"}


def test_persisted_holder_is_unknown_once_renewed():
    from datetime import datetime, timedelta, timezone

    acquired = datetime(2026, 9, 24, tzinfo=timezone.utc)
    renewed = SimpleNamespace(
        audit_session="sess-a", acquired_at=acquired,
        last_heartbeat_at=acquired + timedelta(minutes=1),
    )
    fresh = SimpleNamespace(audit_session="sess-a", acquired_at=acquired, last_heartbeat_at=None)

    assert apl._persisted_holder(renewed) == apl._HOLDER_UNKNOWN
    assert apl._persisted_holder(fresh) == "sess-a"
    assert apl._persisted_holder(None) is None


@pytest.mark.asyncio
async def test_cold_reacquire_does_not_revive_a_session_that_already_exited(monkeypatch):
    from datetime import datetime, timezone

    client = _FakeClient()
    client.sdk_shape = True
    client.idempotent = True
    _patch_models(monkeypatch, client)
    apl._released_at["uuid-1"] = apl.time.monotonic()
    apl._released_sessions["uuid-1"] = {"sess-a"}
    original = client.acquire

    def _acquire(req, *, identity_proof=None):
        result = original(req, identity_proof=identity_proof)
        result.lease.audit_session = "sess-a"
        result.lease.acquired_at = datetime(2026, 9, 24, tzinfo=timezone.utc)
        result.lease.last_heartbeat_at = None
        return result

    client.acquire = _acquire

    await apl.heartbeat_agent_presence("uuid-1", "sess-b", apl.time.monotonic())

    assert set(apl._lease_sessions["uuid-1"]) == {"sess-b"}


@pytest.mark.asyncio
async def test_release_during_a_lease_plane_outage_can_be_retried(monkeypatch):
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    await apl.heartbeat_agent_presence("uuid-1", "sess-a", apl.time.monotonic())
    monkeypatch.setattr(apl, "_make_client", lambda: None)

    first = await apl.release_agent_presence("uuid-1", ("sess-a",))

    assert first == {"released": False, "reason": "lease_plane_unavailable"}
    assert apl._lease_ids["uuid-1"] == "lease-123"
    assert "sess-a" not in apl._lease_sessions["uuid-1"]

    monkeypatch.setattr(apl, "_make_client", lambda: client)
    second = await apl.release_agent_presence("uuid-1", ("sess-a",))
    assert second == {"released": True, "reason": "released"}


@pytest.mark.asyncio
async def test_release_suppression_expires_for_a_later_rebind(monkeypatch):
    """client_session_id is derived from the identity, so a later rebind reuses
    it; once the short suppression window passes, its heartbeats proceed."""
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    await apl.release_agent_presence("uuid-1", ("agent-uuid-1",))
    apl._released_at["uuid-1"] -= apl._RELEASE_SUPPRESS_S + 1

    await apl.heartbeat_agent_presence("uuid-1", "agent-uuid-1", apl.time.monotonic())

    assert len(client.acquired) == 1
    assert "uuid-1" not in apl._released_at


def test_sweep_drops_state_for_identities_nothing_refreshes():
    import asyncio

    now = 10_000.0
    stale = now - apl._PRESENCE_TTL_S - 1
    apl._lease_sessions["gone"] = {"agent-gone": stale}
    apl._lease_ids["gone"] = "lease-gone"
    apl._touched["gone"] = stale
    apl._locks["gone"] = asyncio.Lock()
    apl._lease_sessions["live"] = {"agent-live": now}
    apl._lease_ids["live"] = "lease-live"
    apl._touched["live"] = now
    apl._locks["live"] = asyncio.Lock()
    apl._last_sweep = 0.0

    apl._sweep(now)

    assert "gone" not in apl._lease_sessions
    assert "gone" not in apl._lease_ids
    assert "gone" not in apl._locks
    assert apl._lease_ids["live"] == "lease-live"
    assert "live" in apl._locks


@pytest.mark.asyncio
async def test_failed_release_keeps_its_lease_through_a_sweep(monkeypatch):
    """A refused release empties the holder set while the lease is still live;
    the sweep must not drop its cached id before a retry."""
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    await apl.heartbeat_agent_presence("uuid-1", "sess-a", apl.time.monotonic())

    async def _refused(client_, agent_uuid, lease_id):
        return False

    monkeypatch.setattr(apl, "_release_lease", _refused)
    assert (await apl.release_agent_presence("uuid-1", ("sess-a",)))["reason"] == "release_refused"

    apl._last_sweep = 0.0
    apl._sweep(apl.time.monotonic() + apl._SWEEP_INTERVAL_S + 1)

    assert apl._lease_ids["uuid-1"] == "lease-123"


@pytest.mark.asyncio
async def test_no_lease_plane_still_reports_another_live_holder(monkeypatch):
    client = _FakeClient()
    _patch_models(monkeypatch, client)
    await apl.heartbeat_agent_presence("uuid-1", "sess-b", apl.time.monotonic())
    monkeypatch.setattr(apl, "_make_client", lambda: None)

    result = await apl.release_agent_presence("uuid-1", ("sess-a",))

    assert result == {"released": False, "reason": "held_by_other_session"}


@pytest.mark.asyncio
async def test_failed_heartbeat_and_reacquire_keep_the_cached_id(monkeypatch):
    client = _FakeClient()
    client.heartbeat_should_fail = True

    def _acquire_fails(req, *, identity_proof=None):
        raise RuntimeError("transport down")

    client.acquire = _acquire_fails
    _patch_models(monkeypatch, client)
    apl._lease_ids["uuid-1"] = "lease-live"

    await apl.heartbeat_agent_presence("uuid-1", "sess-1", apl.time.monotonic())

    assert apl._lease_ids["uuid-1"] == "lease-live"
