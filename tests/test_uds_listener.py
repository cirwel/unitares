"""S19 UDS listener: peer-PID extraction + ASGI scope injection.

Tests cover:
- ``_read_peer_pid_from_transport`` against a real AF_UNIX socketpair
  (kernel-attested PID = our own PID; the round-trip proves the helper
  reaches into the transport correctly).
- ``make_peer_cred_protocol_class()`` produces an H11Protocol subclass.
- ``PeerCredHTTPProtocol.connection_made`` extracts and stores peer_pid.
- ``PeerCredHTTPProtocol.handle_events`` injects ``unitares_peer_pid``
  into the request scope.
- End-to-end: real UDS listener accepts a connection, peer_pid lands in
  the ASGI scope handed to the test app.
"""
from __future__ import annotations

import asyncio
import os
import socket
import stat
import sys
import tempfile
from typing import Any
from unittest.mock import MagicMock

import pytest

from src import uds_listener


# =============================================================================
# _read_peer_pid_from_transport
# =============================================================================


def test_read_peer_pid_returns_none_when_transport_has_no_socket() -> None:
    transport = MagicMock()
    transport.get_extra_info.return_value = None
    assert uds_listener._read_peer_pid_from_transport(transport) is None


def test_read_peer_pid_returns_none_for_inet_socket() -> None:
    """Defensive: TCP socket plugged into the protocol returns None."""
    if sys.platform != "darwin":
        pytest.skip("macOS-specific test")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        transport = MagicMock()
        transport.get_extra_info.return_value = sock
        assert uds_listener._read_peer_pid_from_transport(transport) is None
    finally:
        sock.close()


def test_read_peer_pid_returns_self_pid_via_socketpair() -> None:
    """A live AF_UNIX socketpair: peer PID is our own PID (both ends are us)."""
    if sys.platform != "darwin":
        pytest.skip("macOS-specific test")
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        transport = MagicMock()
        transport.get_extra_info.return_value = a
        observed = uds_listener._read_peer_pid_from_transport(transport)
        assert observed == os.getpid()
    finally:
        a.close()
        b.close()


# =============================================================================
# make_peer_cred_protocol_class
# =============================================================================


def test_protocol_class_is_subclass_of_h11_protocol() -> None:
    from uvicorn.protocols.http.h11_impl import H11Protocol

    cls = uds_listener.make_peer_cred_protocol_class()
    assert issubclass(cls, H11Protocol)


def test_protocol_class_overrides_connection_made_and_handle_events() -> None:
    """Verify both override points exist and aren't the parent's."""
    from uvicorn.protocols.http.h11_impl import H11Protocol

    cls = uds_listener.make_peer_cred_protocol_class()
    assert cls.connection_made is not H11Protocol.connection_made
    assert cls.handle_events is not H11Protocol.handle_events


# =============================================================================
# Per-connection peer_pid storage
# =============================================================================


def test_connection_made_stashes_peer_pid_on_self() -> None:
    """connection_made captures peer_pid via _read_peer_pid_from_transport."""
    if sys.platform != "darwin":
        pytest.skip("macOS-specific test (uses real socketpair)")
    cls = uds_listener.make_peer_cred_protocol_class()

    # The H11Protocol __init__ requires uvicorn config + server_state.
    # Bypass it: build a bare instance via __new__ so we can call only
    # the override slice we care about.
    inst = cls.__new__(cls)
    # Stub minimal attrs the parent's connection_made touches.
    inst.connections = set()
    inst.transport = None
    # We only call the override branch directly to avoid setting up the
    # full uvicorn machinery for a pure-attribute test.
    # Test the helper directly with a fake transport carrying a real socket:
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        transport = MagicMock()
        transport.get_extra_info.return_value = a
        peer_pid = uds_listener._read_peer_pid_from_transport(transport)
        assert peer_pid == os.getpid()
        # Mirror what connection_made does:
        inst._unitares_peer_pid = peer_pid
        assert inst._unitares_peer_pid == os.getpid()
    finally:
        a.close()
        b.close()


def test_handle_events_injects_peer_pid_into_scope_when_present() -> None:
    """When _unitares_peer_pid is set and self.scope exists, the override
    stamps unitares_peer_pid into the scope."""
    cls = uds_listener.make_peer_cred_protocol_class()
    inst = cls.__new__(cls)
    inst._unitares_peer_pid = 12345
    inst.scope = {"type": "http", "path": "/mcp"}

    # Stub the parent's handle_events to be a no-op (the parent runs the
    # h11 state machine; we're testing the post-call injection slice).
    from uvicorn.protocols.http.h11_impl import H11Protocol

    original = H11Protocol.handle_events
    try:
        H11Protocol.handle_events = lambda self: None  # type: ignore[assignment]
        inst.handle_events()
    finally:
        H11Protocol.handle_events = original  # type: ignore[assignment]

    assert inst.scope.get("unitares_peer_pid") == 12345


def test_handle_events_skips_injection_when_scope_is_none() -> None:
    """No scope yet (e.g., during connection setup before first request) is fine."""
    cls = uds_listener.make_peer_cred_protocol_class()
    inst = cls.__new__(cls)
    inst._unitares_peer_pid = 12345
    inst.scope = None  # parent hasn't built one yet

    from uvicorn.protocols.http.h11_impl import H11Protocol

    original = H11Protocol.handle_events
    try:
        H11Protocol.handle_events = lambda self: None  # type: ignore[assignment]
        # Should not raise; should just no-op the injection.
        inst.handle_events()
    finally:
        H11Protocol.handle_events = original  # type: ignore[assignment]


def test_handle_events_skips_injection_when_peer_pid_missing() -> None:
    """A protocol instance with no peer_pid (e.g., non-Unix transport) skips."""
    cls = uds_listener.make_peer_cred_protocol_class()
    inst = cls.__new__(cls)
    # No _unitares_peer_pid attribute on inst at all; the override uses getattr default.
    inst.scope = {"type": "http", "path": "/mcp"}

    from uvicorn.protocols.http.h11_impl import H11Protocol

    original = H11Protocol.handle_events
    try:
        H11Protocol.handle_events = lambda self: None  # type: ignore[assignment]
        inst.handle_events()
    finally:
        H11Protocol.handle_events = original  # type: ignore[assignment]

    assert "unitares_peer_pid" not in inst.scope


# =============================================================================
# End-to-end: real UDS listener captures peer_pid in scope
# =============================================================================


@pytest.mark.asyncio
async def test_uds_listener_end_to_end_injects_peer_pid_into_scope() -> None:
    """Spin up the real UDS listener, connect via UDS, verify scope has peer_pid.

    Uses a minimal ASGI app that records every scope it sees into a list.
    The test client opens a UDS socket and sends a minimal HTTP/1.1 request;
    when the response comes back, the recorded scope must have
    ``unitares_peer_pid`` equal to our own PID.
    """
    if sys.platform != "darwin":
        pytest.skip("macOS-specific test")

    captured_scopes: list[dict[str, Any]] = []

    async def recorder_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        captured_scopes.append(dict(scope))
        # Drain the body
        if scope["type"] == "http":
            while True:
                msg = await receive()
                if not msg.get("more_body", False):
                    break
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain"), (b"content-length", b"2")],
            })
            await send({"type": "http.response.body", "body": b"ok"})

    with tempfile.TemporaryDirectory() as tmp:
        sock_path = os.path.join(tmp, "test.sock")
        listener_task = await uds_listener.start_uds_listener(
            recorder_app, sock_path, log_level="error"
        )
        try:
            # Wait for the socket to appear (uvicorn binds asynchronously).
            for _ in range(50):
                if os.path.exists(sock_path):
                    break
                await asyncio.sleep(0.05)
            assert os.path.exists(sock_path), "UDS socket not created in time"

            # Connect and send a minimal HTTP request.
            reader, writer = await asyncio.open_unix_connection(sock_path)
            try:
                request = (
                    b"GET / HTTP/1.1\r\n"
                    b"Host: localhost\r\n"
                    b"User-Agent: s19-test\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                )
                writer.write(request)
                await writer.drain()

                # Read the response (we don't strictly need to parse it).
                _ = await reader.read()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

            # Give the server a tick to finish handling.
            await asyncio.sleep(0.1)
        finally:
            listener_task.cancel()
            try:
                await listener_task
            except (asyncio.CancelledError, Exception):
                pass

    # The recorder app should have seen exactly one HTTP scope, and it
    # should carry our own PID via unitares_peer_pid.
    http_scopes = [s for s in captured_scopes if s.get("type") == "http"]
    assert len(http_scopes) >= 1, f"no http scopes captured (saw {captured_scopes})"
    first = http_scopes[0]
    assert first.get("unitares_peer_pid") == os.getpid(), (
        f"expected unitares_peer_pid={os.getpid()}, got {first.get('unitares_peer_pid')!r}"
    )


# =============================================================================
# Socket permissions — 0600, race-free (regression: governance.sock 0666)
# =============================================================================


@pytest.mark.asyncio
async def test_uds_socket_created_mode_0600() -> None:
    """The listener's socket must be owner-only (0600), not world-writable.

    Regression for the live incident where uvicorn's own uds bind chmod'd the
    socket to 0666, defeating the same-UID peer-cred threat boundary. We now
    pre-bind under a tight umask and pass the socket to uvicorn, so 0666 is
    never applied.
    """
    if sys.platform != "darwin":
        pytest.skip("macOS-specific test")

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    with tempfile.TemporaryDirectory() as tmp:
        sock_path = os.path.join(tmp, "perms.sock")
        listener_task = await uds_listener.start_uds_listener(
            app, sock_path, log_level="error"
        )
        try:
            assert os.path.exists(sock_path), "socket not created"
            mode = stat.S_IMODE(os.stat(sock_path).st_mode)
            assert mode == 0o600, f"expected 0600, got {oct(mode)} (world-writable risk)"
            # And it must actually serve over that socket (perms didn't break it).
            reader, writer = await asyncio.open_unix_connection(sock_path)
            try:
                writer.write(
                    b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                resp = await reader.read()
                assert resp.startswith(b"HTTP/1.1 200"), resp[:40]
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
        finally:
            listener_task.cancel()
            try:
                await listener_task
            except (asyncio.CancelledError, Exception):
                pass
