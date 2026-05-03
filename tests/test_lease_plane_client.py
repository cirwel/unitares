from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from src.lease_plane import (
    AcquireHeldByOther,
    AcquireOk,
    AcquireRequest,
    AcquireSchemaInvalid,
    AcquireServiceUnavailable,
    ForceReleaseRequest,
    HandoffAcceptRequest,
    HandoffOfferRequest,
    HeartbeatRequest,
    LeasePlaneClient,
    LeasePlaneClientConfig,
    LeasePlaneDisabledClient,
    LeaseRecord,
    ReleaseRequest,
    RenewRequest,
    SimpleError,
    SimpleOk,
    StatusOk,
)
from src.lease_plane.client import LeaseHTTPRequest


def _lease_payload(
    *,
    lease_id: UUID | None = None,
    surface_id: str = "file:///Users/cirwel/projects/unitares/src/x.py",
    holder_agent_uuid: UUID | None = None,
    holder_kind: str = "remote_heartbeat",
) -> dict[str, Any]:
    now = datetime.now(UTC).replace(microsecond=0)
    return {
        "lease_id": str(lease_id or uuid4()),
        "surface_id": surface_id,
        "surface_kind": surface_id.split(":", 1)[0],
        "holder_agent_uuid": str(holder_agent_uuid or uuid4()),
        "holder_class": "process_instance",
        "holder_kind": holder_kind,
        "holder_pid": None,
        "heartbeat_required": holder_kind == "remote_heartbeat",
        "intent": "test",
        "acquired_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=90)).isoformat(),
        "last_heartbeat_at": now.isoformat() if holder_kind == "remote_heartbeat" else None,
        "released_at": None,
        "release_reason": None,
        "audit_session": "session-1",
        "original_ttl_s": 90,
    }


def test_acquire_request_rejects_role_holder_class():
    try:
        AcquireRequest(
            surface_id="file:///tmp/a",
            holder_agent_uuid=uuid4(),
            holder_class="role",
            holder_kind="remote_heartbeat",
            ttl_s=90,
        )
    except ValidationError as exc:
        assert "holder_class" in str(exc)
    else:
        raise AssertionError("role holder_class must be rejected by the Python contract")


def test_lease_record_requires_heartbeat_to_match_holder_kind():
    payload = _lease_payload(holder_kind="local_beam")
    payload["heartbeat_required"] = True

    try:
        LeaseRecord.model_validate(payload)
    except ValidationError as exc:
        assert "heartbeat_required must match holder_kind" in str(exc)
    else:
        raise AssertionError("mismatched heartbeat_required should fail validation")


def test_acquire_ok_parses_idempotent_drift_warning():
    holder = uuid4()

    def transport(_request: LeaseHTTPRequest):
        return {
            "ok": True,
            "lease": _lease_payload(holder_agent_uuid=holder),
            "idempotent": True,
            "drift_warning": ["ttl_s", "intent"],
        }

    result = LeasePlaneClient(transport=transport).acquire(
        AcquireRequest(
            surface_id="file:///tmp/a",
            holder_agent_uuid=holder,
            holder_class="process_instance",
            holder_kind="remote_heartbeat",
            ttl_s=90,
            intent="retry",
        )
    )

    assert isinstance(result, AcquireOk)
    assert result.idempotent is True
    assert result.drift_warning == ["ttl_s", "intent"]
    assert result.lease.holder_agent_uuid == holder


def test_acquire_held_by_other_parses_holder_and_expiry():
    holder = uuid4()
    blocking_lease = uuid4()
    expires_at = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=30)

    def transport(_request: LeaseHTTPRequest):
        return {
            "ok": False,
            "error": "held_by_other",
            "surface_id": "file:///tmp/a",
            "blocking_lease_id": str(blocking_lease),
            "held_by_uuid": str(holder),
            "expires_at": expires_at.isoformat(),
            "retry_after_hint_ms": 5000,
        }

    result = LeasePlaneClient(transport=transport).acquire(
        AcquireRequest(
            surface_id="file:///tmp/a",
            holder_agent_uuid=uuid4(),
            holder_class="process_instance",
            holder_kind="remote_heartbeat",
            ttl_s=90,
        )
    )

    assert isinstance(result, AcquireHeldByOther)
    assert result.held_by_uuid == holder
    assert result.expires_at == expires_at


def test_transport_exception_degrades_to_service_unavailable():
    def transport(_request: LeaseHTTPRequest):
        raise TimeoutError("down")

    result = LeasePlaneClient(transport=transport).acquire(
        AcquireRequest(
            surface_id="file:///tmp/a",
            holder_agent_uuid=uuid4(),
            holder_class="process_instance",
            holder_kind="remote_heartbeat",
            ttl_s=90,
        )
    )

    assert isinstance(result, AcquireServiceUnavailable)


def test_invalid_response_degrades_to_schema_invalid():
    def transport(_request: LeaseHTTPRequest):
        return {"ok": True, "lease": {"lease_id": "not-a-uuid"}}

    result = LeasePlaneClient(transport=transport).acquire(
        AcquireRequest(
            surface_id="file:///tmp/a",
            holder_agent_uuid=uuid4(),
            holder_class="process_instance",
            holder_kind="remote_heartbeat",
            ttl_s=90,
        )
    )

    assert isinstance(result, AcquireSchemaInvalid)
    assert result.detail


def test_status_query_encodes_surface_id_and_parses_empty_result():
    seen: dict[str, str] = {}

    def transport(request: LeaseHTTPRequest):
        seen["method"] = request.method
        seen["url"] = request.url
        return {"ok": True, "lease": None}

    result = LeasePlaneClient(
        LeasePlaneClientConfig(base_url="http://127.0.0.1:9999"),
        transport=transport,
    ).status("file:///tmp/a b")

    assert isinstance(result, StatusOk)
    assert result.lease is None
    assert seen["method"] == "GET"
    assert seen["url"].endswith("/v1/lease/status?surface_id=file%3A%2F%2F%2Ftmp%2Fa+b")


def test_renew_and_heartbeat_do_not_send_ttl():
    bodies: list[dict[str, Any] | None] = []
    lease_id = uuid4()

    def transport(request: LeaseHTTPRequest):
        bodies.append(request.json_body)
        return {"ok": True}

    client = LeasePlaneClient(transport=transport)
    assert client.renew(RenewRequest(lease_id=lease_id)).ok is True
    assert client.heartbeat(HeartbeatRequest(lease_id=lease_id)).ok is True

    assert bodies == [{"lease_id": str(lease_id)}, {"lease_id": str(lease_id)}]


def test_release_sends_reason_and_parses_not_holder():
    lease_id = uuid4()
    seen: dict[str, Any] = {}

    def transport(request: LeaseHTTPRequest):
        seen["body"] = request.json_body
        return {"ok": False, "error": "not_holder"}

    result = LeasePlaneClient(transport=transport).release(
        ReleaseRequest(lease_id=lease_id, release_reason="handoff")
    )

    assert isinstance(result, SimpleError)
    assert result.error == "not_holder"
    assert seen["body"] == {"lease_id": str(lease_id), "release_reason": "handoff"}


def test_force_release_rejects_governance_token():
    """RFC §7.10 contract gate: a release() call with reason='forced' must be
    rejected at the contract layer when the caller is authenticated only with
    the standard bearer (LEASE_PLANE_BEARER_TOKEN / GOVERNANCE_TOKEN-equivalent),
    not just at the application layer. The transport must not be invoked.
    """
    transport_called = False

    def transport(_request: LeaseHTTPRequest):
        nonlocal transport_called
        transport_called = True
        return {"ok": True}

    config = LeasePlaneClientConfig(bearer_token="governance-token-xyz")
    result = LeasePlaneClient(config=config, transport=transport).release(
        ReleaseRequest(lease_id=uuid4(), release_reason="forced")
    )

    assert isinstance(result, SimpleError)
    assert result.error == "permission_denied"
    assert transport_called is False, "release() must reject before sending"


def test_force_release_requires_force_release_token():
    """force_release() refuses to send without LEASE_FORCE_RELEASE_TOKEN
    configured. Contract-layer rejection — transport never called.
    """
    transport_called = False

    def transport(_request: LeaseHTTPRequest):
        nonlocal transport_called
        transport_called = True
        return {"ok": True}

    config = LeasePlaneClientConfig(bearer_token="governance-token-xyz")
    result = LeasePlaneClient(config=config, transport=transport).force_release(
        ForceReleaseRequest(lease_id=uuid4())
    )

    assert isinstance(result, SimpleError)
    assert result.error == "permission_denied"
    assert transport_called is False


def test_force_release_uses_elevated_token_and_force_release_endpoint():
    """force_release() sends to /v1/lease/force-release with the elevated
    LEASE_FORCE_RELEASE_TOKEN (NOT the standard bearer). The Elixir router
    sets release_reason='forced' server-side; Python only sends lease_id.
    """
    seen: dict[str, Any] = {}

    def transport(request: LeaseHTTPRequest):
        seen["url"] = request.url
        seen["headers"] = dict(request.headers)
        seen["body"] = request.json_body
        return {"ok": True}

    config = LeasePlaneClientConfig(
        bearer_token="standard-bearer",
        force_release_token="elevated-force-release-token",
    )
    lease_id = uuid4()
    result = LeasePlaneClient(config=config, transport=transport).force_release(
        ForceReleaseRequest(lease_id=lease_id)
    )

    assert isinstance(result, SimpleOk)
    assert "/v1/lease/force-release" in seen["url"], (
        f"must POST to /v1/lease/force-release, not /v1/lease/release; url={seen['url']!r}"
    )
    assert seen["headers"]["Authorization"] == "Bearer elevated-force-release-token"
    assert seen["body"] == {"lease_id": str(lease_id)}


def test_disabled_client_returns_service_unavailable():
    result = LeasePlaneDisabledClient().status("file:///tmp/a")

    assert result.error == "service_unavailable"


def test_lease_record_defaults_earned_status_to_provisional():
    record = LeaseRecord.model_validate(_lease_payload())
    assert record.earned_status == "provisional"


def test_lease_record_accepts_earned_status_earned():
    payload = _lease_payload()
    payload["earned_status"] = "earned"
    record = LeaseRecord.model_validate(payload)
    assert record.earned_status == "earned"


def test_handoff_offer_returns_handoff_id_on_ok():
    handoff_id = uuid4()
    seen: dict[str, Any] = {}

    def transport(request: LeaseHTTPRequest):
        seen["body"] = request.json_body
        seen["url"] = request.url
        return {"ok": True, "handoff_id": str(handoff_id)}

    lease_id = uuid4()
    to_holder = uuid4()
    result = LeasePlaneClient(transport=transport).handoff_offer(
        HandoffOfferRequest(lease_id=lease_id, to_holder_agent_uuid=to_holder, ttl_s=120)
    )

    assert isinstance(result, SimpleOk)
    assert result.handoff_id == handoff_id
    assert seen["body"] == {
        "lease_id": str(lease_id),
        "to_holder_agent_uuid": str(to_holder),
        "ttl_s": 120,
    }
    assert seen["url"].endswith("/v1/lease/handoff/offer")


def test_handoff_accept_parses_not_found():
    handoff_id = uuid4()

    def transport(_request: LeaseHTTPRequest):
        return {"ok": False, "error": "not_found"}

    result = LeasePlaneClient(transport=transport).handoff_accept(
        HandoffAcceptRequest(handoff_id=handoff_id)
    )

    assert isinstance(result, SimpleError)
    assert result.error == "not_found"


def test_simple_unknown_error_preserves_raw_string_in_reason():
    def transport(_request: LeaseHTTPRequest):
        return {"ok": False, "error": "wormhole_collapsed"}

    result = LeasePlaneClient(transport=transport).release(
        ReleaseRequest(lease_id=uuid4(), release_reason="normal")
    )

    assert isinstance(result, SimpleError)
    assert result.error == "service_unavailable"
    assert result.reason is not None
    assert "wormhole_collapsed" in result.reason
