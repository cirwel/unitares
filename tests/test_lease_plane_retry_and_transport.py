"""
Phase A PR 4 — `acquire_with_retry()` jittered backoff + `_urllib_transport`
HTTP-error body-parse coverage.

Closes RFC v0.8 §7.3.3 (caller-library retry contract: jittered exponential,
floor 100ms, ceiling 5s, full jitter) and §7.3.5 (HTTP 409 on `held_by_other`
with the typed-absence body in the response).

 PR 4
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import UTC, datetime, timedelta
from unittest import mock
from uuid import UUID, uuid4

import pytest

from src.lease_plane import (
    AcquireHeldByOther,
    AcquireOk,
    AcquireRequest,
    AcquireServiceUnavailable,
    LeasePlaneClient,
)
from src.lease_plane.client import LeaseHTTPRequest, _urllib_transport


# ---------- §7.3.3 acquire_with_retry tests ----------


def _ok_lease_payload(holder: UUID) -> dict:
    """Minimum AcquireOk payload for a transport stub."""
    now = datetime.now(UTC).replace(microsecond=0)
    return {
        "ok": True,
        "idempotent": False,
        "drift_warning": [],
        "lease": {
            "lease_id": str(uuid4()),
            "surface_id": "dialectic:/pr4_retry_test",
            "surface_kind": "dialectic",
            "holder_agent_uuid": str(holder),
            "holder_class": "process_instance",
            "holder_kind": "remote_heartbeat",
            "heartbeat_required": True,
            "expires_at": (now + timedelta(seconds=60)).isoformat(),
            "original_ttl_s": 60,
            "earned_status": "provisional",
        },
    }


def _held_by_other_payload(blocking_lease: UUID, hint_ms: int = 1234) -> dict:
    return {
        "ok": False,
        "error": "held_by_other",
        "surface_id": "dialectic:/pr4_retry_test",
        "blocking_lease_id": str(blocking_lease),
        "held_by_uuid": str(uuid4()),
        "expires_at": (datetime.now(UTC) + timedelta(seconds=10)).isoformat(),
        "retry_after_hint_ms": hint_ms,
    }


def _make_request(holder: UUID) -> AcquireRequest:
    return AcquireRequest(
        surface_id="dialectic:/pr4_retry_test",
        holder_agent_uuid=holder,
        holder_class="process_instance",
        holder_kind="remote_heartbeat",
        ttl_s=60,
    )


def test_acquire_with_retry_returns_ok_on_first_attempt():
    """No retry needed when first acquire succeeds."""
    holder = uuid4()
    sleeps: list[float] = []
    payload_iter = iter([_ok_lease_payload(holder)])

    def transport(_req: LeaseHTTPRequest):
        return next(payload_iter)

    client = LeasePlaneClient(transport=transport)
    result = client.acquire_with_retry(
        _make_request(holder), max_attempts=3, sleep=lambda s: sleeps.append(s),
    )
    assert isinstance(result, AcquireOk)
    assert sleeps == []  # no backoff needed


def test_acquire_with_retry_retries_on_held_by_other_until_ok():
    """Retries on held_by_other, eventually returning AcquireOk."""
    holder = uuid4()
    sleeps: list[float] = []
    payloads = [
        _held_by_other_payload(uuid4()),
        _held_by_other_payload(uuid4()),
        _ok_lease_payload(holder),
    ]
    payload_iter = iter(payloads)

    def transport(_req: LeaseHTTPRequest):
        return next(payload_iter)

    client = LeasePlaneClient(transport=transport)
    result = client.acquire_with_retry(
        _make_request(holder), max_attempts=5, sleep=lambda s: sleeps.append(s),
    )
    assert isinstance(result, AcquireOk)
    assert len(sleeps) == 2  # two backoffs before the third (successful) attempt


# §9: test_acquire_with_retry_jittered_backoff
def test_acquire_with_retry_jittered_backoff_within_bounds():
    """Backoff is jittered exponential — floor 100ms, ceiling 5s, full jitter (RFC §7.3.3).

    The longer name is the descriptive form; the §9 alias above lets
    `audit_rfc_section_9_gates.py` recognize this as the RFC-named gate.
    """
    holder = uuid4()
    sleeps: list[float] = []
    # Always return held_by_other so we exhaust attempts and observe all sleeps.
    payload = _held_by_other_payload(uuid4())

    def transport(_req: LeaseHTTPRequest):
        return payload

    client = LeasePlaneClient(transport=transport)
    result = client.acquire_with_retry(
        _make_request(holder), max_attempts=6, sleep=lambda s: sleeps.append(s),
    )
    assert isinstance(result, AcquireHeldByOther), "exhausted retries returns final held_by_other"
    # Each sleep must be in [0.1, 5.0] (full jitter floor=100ms, ceiling=5s).
    assert all(0.1 <= s <= 5.0 for s in sleeps), (
        f"backoff sleeps must be in [0.1, 5.0]; got {sleeps}"
    )
    # With max_attempts=6 we expect 5 sleeps (one before each retry).
    assert len(sleeps) == 5


def test_acquire_with_retry_honors_retry_after_hint_as_floor():
    """retry_after_hint_ms is a server-provided lower bound on backoff."""
    holder = uuid4()
    sleeps: list[float] = []
    payload = _held_by_other_payload(uuid4(), hint_ms=2000)  # server hints 2s minimum

    def transport(_req: LeaseHTTPRequest):
        return payload

    client = LeasePlaneClient(transport=transport)
    client.acquire_with_retry(
        _make_request(holder), max_attempts=3, sleep=lambda s: sleeps.append(s),
    )
    # Each sleep should respect the server's 2s lower bound.
    assert all(s >= 2.0 for s in sleeps), (
        f"retry_after_hint_ms=2000 must serve as the floor; got {sleeps}"
    )


def test_acquire_with_retry_returns_service_unavailable_without_retry():
    """service_unavailable is the advisory escape valve — don't retry on it."""
    holder = uuid4()
    sleeps: list[float] = []

    def transport(_req: LeaseHTTPRequest):
        return {"ok": False, "error": "service_unavailable"}

    client = LeasePlaneClient(transport=transport)
    result = client.acquire_with_retry(
        _make_request(holder), max_attempts=5, sleep=lambda s: sleeps.append(s),
    )
    assert isinstance(result, AcquireServiceUnavailable)
    assert sleeps == []  # no retries — service_unavailable is terminal for this method


# ---------- §7.3.5 _urllib_transport HTTP-error body-parse tests ----------


def _http_error(status: int, body_dict: dict | None) -> urllib.error.HTTPError:
    """Construct a real HTTPError with a JSON body (the failure path
    `_urllib_transport` was historically uncovered for; verifier finding)."""
    body_bytes = json.dumps(body_dict).encode("utf-8") if body_dict is not None else b""
    return urllib.error.HTTPError(
        url="http://stub/v1/lease/acquire",
        code=status,
        msg="error",
        hdrs={},  # type: ignore[arg-type]
        fp=io.BytesIO(body_bytes),
    )


def test_urllib_transport_parses_409_body():
    """RFC §7.3.5: HTTP 409 + held_by_other body is parsed and returned as the typed payload."""
    held_payload = {
        "ok": False,
        "error": "held_by_other",
        "surface_id": "dialectic:/pr4_409_probe",
        "blocking_lease_id": str(uuid4()),
        "held_by_uuid": str(uuid4()),
        "expires_at": datetime.now(UTC).isoformat(),
        "retry_after_hint_ms": 4500,
    }
    request = LeaseHTTPRequest(
        method="POST", url="http://stub/v1/lease/acquire",
        json_body={"surface_id": "dialectic:/pr4_409_probe"},
        headers={}, timeout_s=1.0,
    )
    with mock.patch(
        "urllib.request.urlopen", side_effect=_http_error(409, held_payload),
    ):
        result = _urllib_transport(request)
    assert result == held_payload, (
        f"_urllib_transport must parse the 409 body verbatim; got {result!r}"
    )


def test_urllib_transport_401_returns_permission_denied_when_no_body():
    """Empty 401 body falls back to permission_denied envelope."""
    request = LeaseHTTPRequest(
        method="POST", url="http://stub/v1/lease/acquire",
        json_body={}, headers={}, timeout_s=1.0,
    )
    with mock.patch(
        "urllib.request.urlopen", side_effect=_http_error(401, None),
    ):
        result = _urllib_transport(request)
    assert result["ok"] is False
    assert result["error"] == "permission_denied"


def test_urllib_transport_500_returns_service_unavailable_when_no_body():
    """Empty 5xx body falls back to service_unavailable (advisory escape valve)."""
    request = LeaseHTTPRequest(
        method="POST", url="http://stub/v1/lease/acquire",
        json_body={}, headers={}, timeout_s=1.0,
    )
    with mock.patch(
        "urllib.request.urlopen", side_effect=_http_error(500, None),
    ):
        result = _urllib_transport(request)
    assert result == {"ok": False, "error": "service_unavailable"}


# ---------- §7.3.3 acquire_with_retry input validation (PR 5 council fix) ----------


def test_acquire_with_retry_rejects_max_attempts_below_1():
    """RFC §7.3.3: max_attempts must be >= 1; pre-PR-5 max_attempts=0 silently
    fired one HTTP call. Validation now raises ValueError."""
    holder = uuid4()
    calls: list[bool] = []

    def transport(_req: LeaseHTTPRequest):
        calls.append(True)
        return _ok_lease_payload(holder)

    client = LeasePlaneClient(transport=transport)
    with pytest.raises(ValueError):
        client.acquire_with_retry(_make_request(holder), max_attempts=0)
    assert calls == [], "no HTTP call should have fired with max_attempts=0"
    with pytest.raises(ValueError):
        client.acquire_with_retry(_make_request(holder), max_attempts=-1)
    assert calls == []


def test_acquire_with_retry_rejects_floor_exceeding_ceiling():
    """floor_s must be <= ceiling_s; otherwise backoff bounds are nonsense."""
    holder = uuid4()

    def transport(_req: LeaseHTTPRequest):
        return _ok_lease_payload(holder)

    client = LeasePlaneClient(transport=transport)
    with pytest.raises(ValueError):
        client.acquire_with_retry(
            _make_request(holder), max_attempts=3, floor_s=2.0, ceiling_s=1.0,
        )
