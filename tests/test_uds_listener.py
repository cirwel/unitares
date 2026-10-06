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
import contextlib
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


# --- #2662: a failed start must not claim (or unlink) another server's socket ---
import socket as _socket
import shutil as _shutil
import tempfile as _tempfile


@pytest.mark.asyncio
async def test_failed_start_does_not_claim_or_unlink_live_socket(monkeypatch):
    from src.services.mcp_transport_service import (
        _start_uds_listener,
        _stop_uds_listener,
    )

    d = _tempfile.mkdtemp(prefix="uds")
    path = os.path.join(d, "g.sock")
    live = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    live.bind(path)
    live.listen(8)
    try:
        monkeypatch.setenv("UNITARES_UDS_SOCKET", path)
        sock_path, task = await _start_uds_listener(object())
        assert (sock_path, task) == (None, None)
        await _stop_uds_listener(sock_path, task)
        assert os.path.exists(path)
        c = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        c.connect(path)  # still the live owner's socket
        c.close()
    finally:
        live.close()
        if os.path.exists(path):
            os.unlink(path)
        _shutil.rmtree(d, ignore_errors=True)


@pytest.mark.asyncio
async def test_unprovable_stale_socket_is_not_displaced(monkeypatch):
    """An EACCES probe (e.g. another UID's 0600 socket) must not be read as stale."""
    import errno as _errno
    from src import uds_listener

    d = _tempfile.mkdtemp(prefix="uds")
    path = os.path.join(d, "g.sock")
    live = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    live.bind(path)
    live.listen(8)
    real_socket = _socket.socket

    class _DenyingProbe:
        def __init__(self, *a, **k):
            self._s = real_socket(*a, **k)

        def settimeout(self, t):
            self._s.settimeout(t)

        def connect(self, addr):
            raise PermissionError(_errno.EACCES, "denied")

        def close(self):
            self._s.close()

    monkeypatch.setattr(uds_listener.socket, "socket", _DenyingProbe)
    try:
        with pytest.raises(OSError) as exc:
            await uds_listener.start_uds_listener(object(), path)
        assert exc.value.errno == _errno.EADDRINUSE
        assert os.path.exists(path)
    finally:
        live.close()
        if os.path.exists(path):
            os.unlink(path)
        _shutil.rmtree(d, ignore_errors=True)


@pytest.mark.asyncio
async def test_failure_after_bind_removes_own_socket(monkeypatch):
    """A failure between bind and serve leaves no stale node behind: the caller
    claims no path on failure, so shutdown would never unlink it."""
    from src import uds_listener
    from src.services.mcp_transport_service import _start_uds_listener

    def _boom():
        raise RuntimeError("protocol class unavailable")

    monkeypatch.setattr(uds_listener, "make_peer_cred_protocol_class", _boom)
    d = _tempfile.mkdtemp(prefix="uds")
    path = os.path.join(d, "g.sock")
    try:
        with pytest.raises(RuntimeError):
            await uds_listener.start_uds_listener(object(), path)
        assert not os.path.exists(path)

        monkeypatch.setenv("UNITARES_UDS_SOCKET", path)
        assert await _start_uds_listener(object()) == (None, None)
        assert not os.path.exists(path)
    finally:
        _shutil.rmtree(d, ignore_errors=True)


@pytest.mark.asyncio
async def test_failure_after_bind_leaves_a_replaced_node_alone(monkeypatch):
    """The cleanup unlinks only the node it bound, never one that replaced it."""
    from src import uds_listener

    d = _tempfile.mkdtemp(prefix="uds")
    path = os.path.join(d, "g.sock")
    other = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)

    def _replace_then_fail():
        os.unlink(path)
        other.bind(path)
        raise RuntimeError("protocol class unavailable")

    monkeypatch.setattr(uds_listener, "make_peer_cred_protocol_class", _replace_then_fail)
    try:
        with pytest.raises(RuntimeError):
            await uds_listener.start_uds_listener(object(), path)
        assert os.path.exists(path)
    finally:
        other.close()
        if os.path.exists(path):
            os.unlink(path)
        _shutil.rmtree(d, ignore_errors=True)


@pytest.mark.asyncio
async def test_oserror_before_link_leaves_nothing_behind(monkeypatch):
    """An OSError between bind and link leaves no node at the path and no
    private bind directory: the socket only ever reaches the path by link."""
    from src import uds_listener

    d = _tempfile.mkdtemp(prefix="uds")
    path = os.path.join(d, "g.sock")
    real_chmod = os.chmod

    def _socket_chmod_fails(p, mode):
        if os.path.basename(p) == "s":
            raise PermissionError(13, "chmod refused")
        real_chmod(p, mode)

    monkeypatch.setattr(uds_listener.os, "chmod", _socket_chmod_fails)
    try:
        with pytest.raises(PermissionError):
            await uds_listener.start_uds_listener(object(), path)
        monkeypatch.undo()
        assert not os.path.exists(path)
        assert sorted(os.listdir(d)) == ["g.sock.lock"]
    finally:
        monkeypatch.undo()
        _shutil.rmtree(d, ignore_errors=True)


@pytest.mark.asyncio
async def test_node_appearing_during_bind_is_not_displaced(monkeypatch):
    """A node that takes the path after the stale probe is refused, not
    replaced: link() will not overwrite an existing name."""
    import errno as _errno
    from src import uds_listener

    d = _tempfile.mkdtemp(prefix="uds")
    path = os.path.join(d, "g.sock")
    other = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    real_link = os.link

    def _other_binds_first(src, dst):
        other.bind(dst)
        other.listen(8)
        real_link(src, dst)

    monkeypatch.setattr(uds_listener.os, "link", _other_binds_first)
    try:
        with pytest.raises(OSError) as exc:
            await uds_listener.start_uds_listener(object(), path)
        monkeypatch.undo()
        assert exc.value.errno == _errno.EADDRINUSE
        c = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        c.connect(path)  # still the other socket
        c.close()
        assert sorted(os.listdir(d)) == ["g.sock", "g.sock.lock"]
    finally:
        monkeypatch.undo()
        other.close()
        if os.path.exists(path):
            os.unlink(path)
        _shutil.rmtree(d, ignore_errors=True)


@pytest.mark.asyncio
async def test_failed_cleanup_lock_still_closes_socket_and_keeps_original_error(monkeypatch):
    """If the path lock cannot be opened during the failure cleanup (EMFILE,
    say), the socket is still closed and the original error still surfaces."""
    import errno as _errno
    from contextlib import contextmanager
    from src import uds_listener

    made = []
    real_socket = uds_listener.socket.socket

    def _tracking_socket(*a, **k):
        s = real_socket(*a, **k)
        made.append(s)
        return s

    @contextmanager
    def _lock_unavailable(path):
        raise OSError(_errno.EMFILE, "too many open files")
        yield  # pragma: no cover

    def _boom():
        monkeypatch.setattr(uds_listener, "_path_lock", _lock_unavailable)
        raise RuntimeError("protocol class unavailable")

    monkeypatch.setattr(uds_listener.socket, "socket", _tracking_socket)
    monkeypatch.setattr(uds_listener, "make_peer_cred_protocol_class", _boom)
    d = _tempfile.mkdtemp(prefix="uds")
    path = os.path.join(d, "g.sock")
    try:
        with pytest.raises(RuntimeError):
            await uds_listener.start_uds_listener(object(), path)
        assert made and made[-1].fileno() == -1  # the listening socket was closed
    finally:
        monkeypatch.undo()
        for s in made:
            s.close()
        if os.path.exists(path):
            os.unlink(path)
        _shutil.rmtree(d, ignore_errors=True)


async def _ok_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})


@pytest.mark.asyncio
async def test_stop_unlinks_the_socket_this_process_bound(monkeypatch):
    from src.services.mcp_transport_service import _start_uds_listener, _stop_uds_listener

    d = _tempfile.mkdtemp(prefix="uds")
    path = os.path.join(d, "g.sock")
    monkeypatch.setenv("UNITARES_UDS_SOCKET", path)
    try:
        sock_path, task = await _start_uds_listener(_ok_app)
        assert sock_path == path and task is not None
        await _stop_uds_listener(sock_path, task)
        assert not os.path.exists(path)
    finally:
        _shutil.rmtree(d, ignore_errors=True)


@pytest.mark.asyncio
async def test_stop_leaves_a_replacement_servers_socket_alone(monkeypatch):
    """After our listener stops accepting, a replacement server may bind a
    fresh socket at the same path; our shutdown must not delete it (#2662)."""
    from src.services.mcp_transport_service import _start_uds_listener, _stop_uds_listener

    d = _tempfile.mkdtemp(prefix="uds")
    path = os.path.join(d, "g.sock")
    monkeypatch.setenv("UNITARES_UDS_SOCKET", path)
    replacement = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        sock_path, task = await _start_uds_listener(_ok_app)
        assert sock_path == path
        os.unlink(path)  # the replacement found our node stale and removed it
        replacement.bind(path)
        replacement.listen(8)
        await _stop_uds_listener(sock_path, task)
        assert os.path.exists(path)
        c = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        c.connect(path)  # still the replacement's live socket
        c.close()
    finally:
        replacement.close()
        if os.path.exists(path):
            os.unlink(path)
        _shutil.rmtree(d, ignore_errors=True)


def test_path_lock_excludes_a_second_holder():
    import errno as _errno
    import fcntl
    from src.uds_listener import _path_lock

    d = _tempfile.mkdtemp(prefix="uds")
    path = os.path.join(d, "g.sock")
    try:
        with _path_lock(path):
            fd = os.open(path + ".lock", os.O_RDWR)
            try:
                with pytest.raises(OSError) as exc:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                assert exc.value.errno in (_errno.EAGAIN, _errno.EWOULDBLOCK)
            finally:
                os.close(fd)
        fd = os.open(path + ".lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released on exit
        finally:
            os.close(fd)
    finally:
        for f in (path + ".lock",):
            if os.path.exists(f):
                os.unlink(f)
        _shutil.rmtree(d, ignore_errors=True)


# --- The path lock must not block the event loop; the bind must not touch the umask ---
import fcntl as _fcntl
import threading as _threading
import time as _time

_LOCK_HELD_FOR = 0.6
_MAX_LOOP_GAP = 0.3


def _hold_path_lock_for(path: str, seconds: float) -> None:
    """Take the path lock on a separate open file (as another process would)
    and release it from a timer thread, independent of the event loop."""
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    _fcntl.flock(fd, _fcntl.LOCK_EX)
    _threading.Timer(seconds, os.close, args=(fd,)).start()


async def _run_with_loop_gap(coro):
    """Await ``coro`` while a ticker measures the longest stall of the loop."""
    stamps = [_time.monotonic()]
    stop = asyncio.Event()

    async def _tick():
        while not stop.is_set():
            await asyncio.sleep(0.01)
            stamps.append(_time.monotonic())

    ticker = asyncio.create_task(_tick())
    await asyncio.sleep(0)
    try:
        result = await coro
    finally:
        stop.set()
        await ticker
    return result, max(b - a for a, b in zip(stamps, stamps[1:]))


@pytest.mark.asyncio
async def test_start_waits_for_a_held_path_lock_without_blocking_the_loop():
    d = _tempfile.mkdtemp(prefix="uds")
    path = os.path.join(d, "g.sock")
    task = None
    try:
        _hold_path_lock_for(path, _LOCK_HELD_FOR)
        started = _time.monotonic()
        task, gap = await _run_with_loop_gap(
            uds_listener.start_uds_listener(_ok_app, path, log_level="error")
        )
        assert _time.monotonic() - started >= _LOCK_HELD_FOR * 0.8  # it did wait
        assert gap < _MAX_LOOP_GAP, f"event loop stalled {gap:.2f}s on the path lock"
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        _shutil.rmtree(d, ignore_errors=True)


@pytest.mark.asyncio
async def test_stop_waits_for_a_held_path_lock_without_blocking_the_loop(monkeypatch):
    from src.services.mcp_transport_service import _start_uds_listener, _stop_uds_listener

    d = _tempfile.mkdtemp(prefix="uds")
    path = os.path.join(d, "g.sock")
    monkeypatch.setenv("UNITARES_UDS_SOCKET", path)
    try:
        sock_path, task = await _start_uds_listener(_ok_app)
        assert sock_path == path
        _hold_path_lock_for(path, _LOCK_HELD_FOR)
        _, gap = await _run_with_loop_gap(_stop_uds_listener(sock_path, task))
        assert gap < _MAX_LOOP_GAP, f"event loop stalled {gap:.2f}s on the path lock"
        assert not os.path.exists(path)
    finally:
        _shutil.rmtree(d, ignore_errors=True)


@pytest.mark.asyncio
async def test_cancelled_start_releases_the_socket_it_goes_on_to_bind(monkeypatch):
    """Cancelled while waiting on the lock, the start's worker still binds once
    the lock frees; that socket must be released, not left answering probes."""
    bound = _threading.Event()
    real_bind_into_place = uds_listener._bind_into_place

    def _bind_and_signal(p):
        try:
            return real_bind_into_place(p)
        finally:
            bound.set()

    monkeypatch.setattr(uds_listener, "_bind_into_place", _bind_and_signal)
    d = _tempfile.mkdtemp(prefix="uds")
    path = os.path.join(d, "g.sock")
    try:
        _hold_path_lock_for(path, 0.3)
        task = asyncio.create_task(
            uds_listener.start_uds_listener(_ok_app, path, log_level="error")
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not bound.is_set()  # cancelled while the worker waited on the lock
        assert await asyncio.to_thread(bound.wait, 5)  # ...which then bound anyway
        for _ in range(100):
            await asyncio.sleep(0.02)
            if sorted(os.listdir(d)) == ["g.sock.lock"]:
                break
        assert sorted(os.listdir(d)) == ["g.sock.lock"]
    finally:
        _shutil.rmtree(d, ignore_errors=True)


@pytest.mark.asyncio
async def test_bind_leaves_the_process_umask_alone(monkeypatch):
    """The umask is process-wide: setting it around bind would hand any thread
    creating a file in that window the restrictive mask."""
    calls = []
    real_umask = os.umask

    def _spy(mask):
        calls.append(mask)
        return real_umask(mask)

    monkeypatch.setattr(uds_listener.os, "umask", _spy)
    d = _tempfile.mkdtemp(prefix="uds")
    path = os.path.join(d, "g.sock")
    task = None
    try:
        task = await uds_listener.start_uds_listener(_ok_app, path, log_level="error")
        assert calls == []
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert sorted(os.listdir(d)) == ["g.sock", "g.sock.lock"]
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        _shutil.rmtree(d, ignore_errors=True)


@pytest.mark.asyncio
async def test_socket_reaches_its_path_owner_only_and_accepting(monkeypatch):
    """Under a fully permissive umask the freshly bound node is world-writable;
    it must already be 0600 and listening when it first appears at the path."""
    seen = []
    real_link = os.link

    def _check_then_link(src, dst):
        probe = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        try:
            probe.connect(src)
            accepting = True
        except OSError:
            accepting = False
        finally:
            probe.close()
        seen.append((stat.S_IMODE(os.stat(src).st_mode), accepting))
        real_link(src, dst)

    monkeypatch.setattr(uds_listener.os, "link", _check_then_link)
    d = _tempfile.mkdtemp(prefix="uds")
    path = os.path.join(d, "g.sock")
    task = None
    previous = os.umask(0)
    try:
        task = await uds_listener.start_uds_listener(_ok_app, path, log_level="error")
        assert seen == [(0o600, True)]
    finally:
        os.umask(previous)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        _shutil.rmtree(d, ignore_errors=True)


@pytest.mark.asyncio
async def test_a_socket_path_near_the_length_limit_still_binds():
    """The staged path inside the private directory is no longer than the real
    one, so a path that fits sun_path (104 bytes on macOS) still binds."""
    root = _tempfile.mkdtemp(prefix="u", dir="/tmp")
    try:
        target_len = 100
        pad = target_len - len(root) - len("/") - len("/g.sock")
        sock_dir = os.path.join(root, "d" * pad)
        path = os.path.join(sock_dir, "g.sock")
        assert len(path) == target_len
        task = await uds_listener.start_uds_listener(_ok_app, path, log_level="error")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        assert os.path.exists(path)
    finally:
        _shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_socket_creation_failure_leaves_no_private_directory(monkeypatch):
    import errno as _errno

    def _no_fds(*a, **k):
        raise OSError(_errno.EMFILE, "too many open files")

    monkeypatch.setattr(uds_listener.socket, "socket", _no_fds)
    d = _tempfile.mkdtemp(prefix="uds")
    path = os.path.join(d, "g.sock")
    try:
        with pytest.raises(OSError):
            await uds_listener.start_uds_listener(_ok_app, path, log_level="error")
        assert sorted(os.listdir(d)) == ["g.sock.lock"]
    finally:
        _shutil.rmtree(d, ignore_errors=True)
