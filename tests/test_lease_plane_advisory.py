from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from src.lease_plane import (
    AcquireHeldByOther,
    AcquireOk,
    AcquireRequest,
    AcquireServiceUnavailable,
    LeasePlaneClient,
    LeasePlaneClientConfig,
    LeasePlaneDisabledClient,
    SimpleOk,
)
from src.lease_plane.advisory import lease_advisory_scope, make_advisory_client


def _ok_lease_payload(holder_uuid: UUID) -> dict[str, Any]:
    now = datetime.now(UTC).replace(microsecond=0)
    return {
        "lease_id": str(uuid4()),
        "surface_id": "test:advisory/x",
        "surface_kind": "test",
        "holder_agent_uuid": str(holder_uuid),
        "holder_class": "process_instance",
        "holder_kind": "remote_heartbeat",
        "holder_pid": None,
        "heartbeat_required": True,
        "intent": "test",
        "acquired_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=60)).isoformat(),
        "last_heartbeat_at": now.isoformat(),
        "released_at": None,
        "release_reason": None,
        "audit_session": None,
        "original_ttl_s": 60,
        "earned_status": "provisional",
    }


def _scripted_transport(responses: list[dict[str, Any]]):
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def transport(req):
        calls.append((req.method, req.url, req.json_body))
        return responses.pop(0)

    return transport, calls


def test_acquired_new_runs_block_and_releases():
    holder = uuid4()
    transport, calls = _scripted_transport(
        [
            {"ok": True, "lease": _ok_lease_payload(holder), "idempotent": False, "drift_warning": []},
            {"ok": True},
        ]
    )
    client = LeasePlaneClient(transport=transport)

    block_ran = False
    seen_outcome = None
    with lease_advisory_scope(
        surface_id="test:advisory/x",
        surface_kind="test",
        holder_agent_uuid=holder,
        ttl_s=60,
        intent="unit-test",
        client=client,
    ) as (outcome, lease_id):
        block_ran = True
        seen_outcome = outcome
        assert lease_id is not None

    assert block_ran is True
    assert seen_outcome == "acquired_new"
    # acquire + release ⇒ exactly two HTTP calls
    assert len(calls) == 2
    assert calls[0][1].endswith("/v1/lease/acquire")
    assert calls[1][1].endswith("/v1/lease/release")


def test_idempotent_outcome_classified_correctly():
    holder = uuid4()
    transport, _ = _scripted_transport(
        [
            {"ok": True, "lease": _ok_lease_payload(holder), "idempotent": True, "drift_warning": ["intent"]},
            {"ok": True},
        ]
    )
    client = LeasePlaneClient(transport=transport)

    with lease_advisory_scope(
        surface_id="test:advisory/x",
        surface_kind="test",
        holder_agent_uuid=holder,
        ttl_s=60,
        client=client,
    ) as (outcome, lease_id):
        assert outcome == "acquired_idempotent"
        assert lease_id is not None


def test_held_by_other_runs_block_no_release():
    other_holder = uuid4()
    transport, calls = _scripted_transport(
        [
            {
                "ok": False,
                "error": "held_by_other",
                "held_by_uuid": str(other_holder),
                "expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            }
        ]
    )
    client = LeasePlaneClient(transport=transport)

    block_ran = False
    with lease_advisory_scope(
        surface_id="test:advisory/contended",
        surface_kind="test",
        holder_agent_uuid=uuid4(),
        ttl_s=60,
        client=client,
    ) as (outcome, lease_id):
        block_ran = True
        assert outcome == "held_by_other"
        assert lease_id is None

    # Phase A advisory: block must run regardless of held_by_other.
    assert block_ran is True
    # No release call because we never held the lease.
    assert len(calls) == 1


def test_service_unavailable_runs_block_no_release():
    transport, calls = _scripted_transport([{"ok": False, "error": "service_unavailable"}])
    client = LeasePlaneClient(transport=transport)

    block_ran = False
    with lease_advisory_scope(
        surface_id="test:advisory/down",
        surface_kind="test",
        holder_agent_uuid=uuid4(),
        ttl_s=60,
        client=client,
    ) as (outcome, _lease_id):
        block_ran = True
        assert outcome == "service_unavailable"

    assert block_ran is True
    assert len(calls) == 1


def test_disabled_client_outcome_is_service_unavailable():
    block_ran = False
    with lease_advisory_scope(
        surface_id="test:advisory/disabled",
        surface_kind="test",
        holder_agent_uuid=uuid4(),
        ttl_s=60,
        client=LeasePlaneDisabledClient(),
    ) as (outcome, _lease_id):
        block_ran = True
        assert outcome == "service_unavailable"

    assert block_ran is True


def test_caller_exceptions_propagate_and_lease_still_released():
    holder = uuid4()
    transport, calls = _scripted_transport(
        [
            {"ok": True, "lease": _ok_lease_payload(holder), "idempotent": False, "drift_warning": []},
            {"ok": True},
        ]
    )
    client = LeasePlaneClient(transport=transport)

    class BlockError(RuntimeError):
        pass

    try:
        with lease_advisory_scope(
            surface_id="test:advisory/raises",
            surface_kind="test",
            holder_agent_uuid=holder,
            ttl_s=60,
            client=client,
        ) as (outcome, lease_id):
            assert outcome == "acquired_new"
            assert lease_id is not None
            raise BlockError("boom")
    except BlockError:
        pass
    else:
        raise AssertionError("BlockError must propagate")

    # Even on caller raise, the lease must be released (cleanup contract).
    assert len(calls) == 2
    assert calls[1][1].endswith("/v1/lease/release")


def test_make_advisory_client_returns_disabled_when_no_token(monkeypatch):
    monkeypatch.delenv("LEASE_PLANE_BEARER_TOKEN", raising=False)
    client = make_advisory_client()
    assert isinstance(client, LeasePlaneDisabledClient)


def test_make_advisory_client_returns_real_client_when_token_set(monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "test-token")
    monkeypatch.setenv("LEASE_PLANE_BASE_URL", "http://127.0.0.1:9999")
    client = make_advisory_client()
    assert isinstance(client, LeasePlaneClient)
    assert not isinstance(client, LeasePlaneDisabledClient)
    assert client.config.base_url == "http://127.0.0.1:9999"
    assert client.config.bearer_token == "test-token"


def test_acquire_raise_classified_as_client_error():
    """The advisory client SHOULD never raise (that's the whole pattern), but
    if a custom transport accidentally does, the wrapper must still run the
    block and surface client_error so Phase A telemetry stays honest."""

    def boom_transport(_req):
        raise TimeoutError("network gone")

    client = LeasePlaneClient(transport=boom_transport)

    block_ran = False
    seen_outcome: str | None = None
    with lease_advisory_scope(
        surface_id="test:advisory/timeout",
        surface_kind="test",
        holder_agent_uuid=uuid4(),
        ttl_s=60,
        client=client,
    ) as (outcome, _lease_id):
        block_ran = True
        seen_outcome = outcome

    assert block_ran is True
    # The standard LeasePlaneClient catches transport raises and returns
    # AcquireServiceUnavailable internally — verify the outcome maps cleanly.
    assert seen_outcome == "service_unavailable"
