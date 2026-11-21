"""Tests for LeasePlaneClient Unix domain socket transport (Daybreak Finding 10)."""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import tempfile
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from unitares_sdk.lease_plane import (
    PROTOCOL_VERSION,
    AcquireHeldByOther,
    AcquireOk,
    AcquirePermissionDenied,
    AcquireRequest,
    AcquireServiceUnavailable,
    HealthOk,
    HealthUnavailable,
    LeasePlaneClient,
    LeasePlaneClientConfig,
    ReleaseRequest,
    SimpleOk,
    StatusOk,
)
from unitares_sdk.lease_plane.advisory import make_advisory_client
from unitares_sdk.lease_plane.client import LeaseHTTPRequest, _UnixHTTPConnection


class _RecordingUnixHTTPServer(socketserver.UnixStreamServer):
    allow_reuse_address = True

    def __init__(self, server_address: str, handler_cls: type[http.server.BaseHTTPRequestHandler]):
        self.recorded_requests: list[dict[str, Any]] = []
        self.next_override: tuple[int, bytes] | None = None
        super().__init__(server_address, handler_cls)


def _make_handler(expected_bearer: str) -> type[http.server.BaseHTTPRequestHandler]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send_json(self, status: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)

        def _send_raw(self, status: int, raw: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            if raw:
                self.wfile.write(raw)

        def _record_and_dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(length) if length > 0 else b""
            parsed_body = json.loads(raw_body.decode("utf-8")) if raw_body else None

            server: _RecordingUnixHTTPServer = self.server  # type: ignore[assignment]
            server.recorded_requests.append(
                {
                    "method": method,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": parsed_body,
                }
            )

            if server.next_override is not None:
                status, raw = server.next_override
                server.next_override = None
                self._send_raw(status, raw)
                return

            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {expected_bearer}":
                self._send_json(
                    401,
                    {
                        "ok": False,
                        "error": "permission_denied",
                        "reason": "bearer token missing or invalid",
                        "protocol_version": PROTOCOL_VERSION,
                    },
                )
                return

            split = urlsplit(self.path)
            if method == "GET" and split.path == "/v1/health":
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "status": "ok",
                        "protocol_version": PROTOCOL_VERSION,
                    },
                )
                return

            if method == "GET" and split.path == "/v1/lease/status":
                qs = parse_qs(split.query)
                surface_id = (qs.get("surface_id") or [""])[0]
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "lease": None,
                        "surface_id": surface_id,
                        "protocol_version": PROTOCOL_VERSION,
                    },
                )
                return

            if method == "POST" and split.path == "/v1/lease/acquire":
                now = datetime.now(UTC)
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "lease": {
                            "lease_id": str(uuid.uuid4()),
                            "surface_id": parsed_body["surface_id"],
                            "surface_kind": "file",
                            "holder_agent_uuid": parsed_body["holder_agent_uuid"],
                            "holder_kind": parsed_body["holder_kind"],
                            "holder_class": parsed_body["holder_class"],
                            "heartbeat_required": parsed_body["holder_kind"] == "remote_heartbeat",
                            "acquired_at": now.isoformat(),
                            "expires_at": (now + timedelta(seconds=30)).isoformat(),
                            "last_heartbeat_at": now.isoformat(),
                            "original_ttl_s": parsed_body["ttl_s"],
                            "intent": parsed_body.get("intent"),
                            "earned_status": "provisional",
                        },
                        "idempotent": False,
                        "drift_warning": [],
                        "protocol_version": PROTOCOL_VERSION,
                    },
                )
                return

            if method == "POST" and split.path == "/v1/lease/release":
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "protocol_version": PROTOCOL_VERSION,
                    },
                )
                return

            self._send_json(
                404,
                {
                    "ok": False,
                    "error": "not_found",
                    "protocol_version": PROTOCOL_VERSION,
                },
            )

        def do_GET(self) -> None:
            self._record_and_dispatch("GET")

        def do_POST(self) -> None:
            self._record_and_dispatch("POST")

    return _Handler


@contextmanager
def _uds_lease_server(
    bearer: str = "test-uds-bearer",
    mode: int = 0o600,
) -> Iterator[tuple[str, _RecordingUnixHTTPServer]]:
    with tempfile.TemporaryDirectory(prefix="lp_uds_") as tmpdir:
        os.chmod(tmpdir, 0o700)
        sock_path = os.path.join(tmpdir, "lease.sock")
        server = _RecordingUnixHTTPServer(sock_path, _make_handler(bearer))
        os.chmod(sock_path, mode)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.daemon = True
        thread.start()
        try:
            yield sock_path, server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)


def _sample_acquire_request(
    surface_id: str,
    holder_uuid: uuid.UUID,
    *,
    intent: str | None = None,
) -> AcquireRequest:
    return AcquireRequest(
        surface_id=surface_id,
        holder_agent_uuid=holder_uuid,
        holder_class="process_instance",
        holder_kind="remote_heartbeat",
        ttl_s=30,
        intent=intent,
    )


def test_uds_round_trip_health_acquire_status_release_and_headers() -> None:
    bearer = "uds-secret-bearer"
    proof = "lat.v1.test-attestation-token"
    holder_uuid = uuid.uuid4()

    with _uds_lease_server(bearer=bearer) as (sock_path, server):
        client = LeasePlaneClient(
            LeasePlaneClientConfig(
                bearer_token=bearer,
                unix_socket=sock_path,
                timeout_s=2.0,
            )
        )

        health = client.health_check()
        assert isinstance(health, HealthOk)
        assert health.ok is True
        assert health.status == "ok"

        acq = client.acquire(
            _sample_acquire_request(
                "file:///tmp/uds-test.txt",
                holder_uuid,
                intent="uds sdk test",
            ),
            identity_proof=proof,
        )
        assert isinstance(acq, AcquireOk)
        assert acq.lease.holder_agent_uuid == holder_uuid

        status = client.status("file:///tmp/uds-test.txt")
        assert isinstance(status, StatusOk)
        assert status.lease is None

        rel = client.release(
            ReleaseRequest(lease_id=acq.lease.lease_id, release_reason="normal"),
            identity_proof=proof,
        )
        assert isinstance(rel, SimpleOk)

        assert len(server.recorded_requests) == 4
        acq_req = server.recorded_requests[1]
        assert acq_req["method"] == "POST"
        assert acq_req["path"] == "/v1/lease/acquire"
        assert acq_req["headers"]["authorization"] == f"Bearer {bearer}"
        assert acq_req["headers"]["x-unitares-identity-proof"] == proof

        status_req = server.recorded_requests[2]
        assert status_req["method"] == "GET"
        assert "surface_id=" in status_req["path"]


def test_uds_base_url_scheme_and_from_env_and_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    bearer = "uds-env-bearer"
    with _uds_lease_server(bearer=bearer) as (sock_path, _server):
        # 1. unix:///path in base_url
        scheme_client = LeasePlaneClient(
            LeasePlaneClientConfig(
                base_url=f"unix://{sock_path}",
                bearer_token=bearer,
                timeout_s=2.0,
            )
        )
        assert isinstance(scheme_client.health_check(), HealthOk)

        # 2. LeasePlaneClientConfig.from_env()
        monkeypatch.setenv("UNITARES_LEASE_PLANE_UNIX_SOCKET", sock_path)
        monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", bearer)
        monkeypatch.delenv("LEASE_FORCE_RELEASE_TOKEN", raising=False)
        env_cfg = LeasePlaneClientConfig.from_env(timeout_s=2.0)
        assert env_cfg.unix_socket == sock_path
        assert env_cfg.bearer_token == bearer
        assert isinstance(LeasePlaneClient(env_cfg).health_check(), HealthOk)

        # 3. make_advisory_client() picks up UNITARES_LEASE_PLANE_UNIX_SOCKET
        advisory_client = make_advisory_client()
        assert advisory_client.config.unix_socket == sock_path
        assert isinstance(advisory_client.health_check(), HealthOk)


def test_uds_preserves_error_classification() -> None:
    bearer = "expected-bearer"
    holder_uuid = uuid.uuid4()
    req = _sample_acquire_request("file:///tmp/uds-errors.txt", holder_uuid)

    with _uds_lease_server(bearer=bearer) as (sock_path, server):
        # Wrong bearer -> 401 JSON -> AcquirePermissionDenied
        bad_auth_client = LeasePlaneClient(
            LeasePlaneClientConfig(
                bearer_token="wrong-bearer",
                unix_socket=sock_path,
                timeout_s=2.0,
            )
        )
        res_401 = bad_auth_client.acquire(req)
        assert isinstance(res_401, AcquirePermissionDenied)
        health_401 = bad_auth_client.health_check()
        assert isinstance(health_401, HealthUnavailable)

        good_client = LeasePlaneClient(
            LeasePlaneClientConfig(
                bearer_token=bearer,
                unix_socket=sock_path,
                timeout_s=2.0,
            )
        )

        # Empty-body 401 -> permission_denied fallback
        server.next_override = (401, b"")
        assert isinstance(good_client.acquire(req), AcquirePermissionDenied)

        # 409 held_by_other JSON envelope -> AcquireHeldByOther
        other_uuid = str(uuid.uuid4())
        held_body = json.dumps(
            {
                "ok": False,
                "error": "held_by_other",
                "surface_id": "file:///tmp/uds-errors.txt",
                "blocking_lease_id": str(uuid.uuid4()),
                "held_by_uuid": other_uuid,
                "expires_at": (datetime.now(UTC) + timedelta(seconds=15)).isoformat(),
                "retry_after_hint_ms": 500,
                "protocol_version": PROTOCOL_VERSION,
            }
        ).encode("utf-8")
        server.next_override = (409, held_body)
        res_409 = good_client.acquire(req)
        assert isinstance(res_409, AcquireHeldByOther)
        assert str(res_409.held_by_uuid) == other_uuid

        # 503 service_unavailable -> AcquireServiceUnavailable
        server.next_override = (503, b"")
        res_503 = good_client.acquire(req)
        assert isinstance(res_503, AcquireServiceUnavailable)


def test_uds_rejects_symlink_socket_fail_closed() -> None:
    with _uds_lease_server() as (sock_path, server):
        symlink_path = os.path.join(os.path.dirname(sock_path), "link.sock")
        os.symlink(sock_path, symlink_path)

        with pytest.raises(PermissionError, match="symlink"):
            _UnixHTTPConnection(symlink_path, timeout=1.0).connect()

        client = LeasePlaneClient(
            LeasePlaneClientConfig(
                bearer_token="test-uds-bearer",
                unix_socket=symlink_path,
                timeout_s=1.0,
            )
        )
        health = client.health_check()
        assert isinstance(health, HealthUnavailable)
        assert "PermissionError" in (health.reason or "")

        acq = client.acquire(
            _sample_acquire_request("file:///tmp/uds-symlink.txt", uuid.uuid4())
        )
        assert isinstance(acq, AcquireServiceUnavailable)
        assert acq.reason == "transport_exception"
        assert server.recorded_requests == []


@pytest.mark.parametrize("bad_mode", [0o660, 0o644, 0o666, 0o604])
def test_uds_rejects_group_or_world_accessible_socket(bad_mode: int) -> None:
    with _uds_lease_server(mode=bad_mode) as (sock_path, server):
        with pytest.raises(PermissionError, match="unsafe permissions"):
            _UnixHTTPConnection(sock_path, timeout=1.0).connect()

        client = LeasePlaneClient(
            LeasePlaneClientConfig(
                bearer_token="test-uds-bearer",
                unix_socket=sock_path,
                timeout_s=1.0,
            )
        )
        health = client.health_check()
        assert isinstance(health, HealthUnavailable)
        assert "PermissionError" in (health.reason or "")
        assert server.recorded_requests == []


def test_uds_rejects_non_socket_wrong_uid_and_malformed_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="lp_uds_bad_") as tmpdir:
        regular_file = os.path.join(tmpdir, "not-a-socket")
        with open(regular_file, "wb") as fh:
            fh.write(b"nope")
        os.chmod(regular_file, 0o600)

        with pytest.raises(PermissionError, match="not a unix socket"):
            _UnixHTTPConnection(regular_file, timeout=1.0).connect()

    with pytest.raises(ValueError, match="absolute"):
        _UnixHTTPConnection("relative/lease.sock", timeout=1.0).connect()

    # Malformed unix:// URL with non-empty netloc must fail closed, not hit TCP.
    malformed_client = LeasePlaneClient(
        LeasePlaneClientConfig(
            base_url="unix://relative/lease.sock",
            bearer_token="tok",
            timeout_s=1.0,
        )
    )
    malformed_health = malformed_client.health_check()
    assert isinstance(malformed_health, HealthUnavailable)
    assert "ValueError" in (malformed_health.reason or "")

    # Different UID must be rejected fail-closed.
    with _uds_lease_server() as (sock_path, server):
        monkeypatch.setattr(os, "getuid", lambda: os.lstat(sock_path).st_uid + 1)
        with pytest.raises(PermissionError, match="not owned by current uid"):
            _UnixHTTPConnection(sock_path, timeout=1.0).connect()
        assert server.recorded_requests == []


def test_uds_keeps_governance_attestation_exchange_on_http() -> None:
    seen: list[LeaseHTTPRequest] = []

    def fake_transport(request: LeaseHTTPRequest) -> dict[str, Any]:
        seen.append(request)
        if request.url.endswith("/v1/lease-holder/attest"):
            return {"ok": True, "attestation": "lat.v1.minted-over-http"}
        now = datetime.now(UTC)
        return {
            "ok": True,
            "lease": {
                "lease_id": str(uuid.uuid4()),
                "surface_id": "file:///tmp/uds-attest.txt",
                "surface_kind": "file",
                "holder_agent_uuid": str(uuid.uuid4()),
                "holder_kind": "remote_heartbeat",
                "holder_class": "process_instance",
                "heartbeat_required": True,
                "acquired_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=30)).isoformat(),
                "last_heartbeat_at": now.isoformat(),
                "original_ttl_s": 30,
                "earned_status": "provisional",
            },
            "idempotent": False,
            "drift_warning": [],
            "protocol_version": PROTOCOL_VERSION,
        }

    client = LeasePlaneClient(
        LeasePlaneClientConfig(
            bearer_token="tok",
            unix_socket="/run/unitares/lease.sock",
            governance_url="http://127.0.0.1:8767",
        ),
        transport=fake_transport,
    )
    res = client.acquire(
        _sample_acquire_request("file:///tmp/uds-attest.txt", uuid.uuid4()),
        identity_proof="continuity-token-raw",
    )
    assert isinstance(res, AcquireOk)
    assert len(seen) == 2
    assert seen[0].url == "http://127.0.0.1:8767/v1/lease-holder/attest"
    assert seen[0].unix_socket is None
    assert seen[1].unix_socket == "/run/unitares/lease.sock"
    assert seen[1].headers["X-Unitares-Identity-Proof"] == "lat.v1.minted-over-http"
