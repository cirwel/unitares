"""S19 UDS listener — kernel-attested peer-PID transport.

Adds a Unix-domain socket listener parallel to the existing HTTP-on-loopback
at port 8767. Clients connecting over UDS get their PID extracted by the
kernel (via ``LOCAL_PEERPID`` getsockopt) at connection-accept time; this
peer PID is then propagated into the ASGI scope and from there into
``SessionSignals.peer_pid`` for the substrate-claim verification path.

See ```` v2 §M3-v2 for the design.

Why this is additive (not a replacement for HTTP):
- HTTP at port 8767 keeps serving non-substrate-anchored clients.
- UDS at the configured path ONLY serves substrate-anchored residents
  (Vigil/Sentinel/Chronicler) once they migrate (PR5).
- A request that arrives over UDS gets ``scope["unitares_peer_pid"]``
  populated; HTTP requests get the field unset (no peer_pid plumbing).
- The verification gate in handlers fires only when ``peer_pid`` is set
  AND the resuming UUID has a substrate-claim row.

The implementation extends uvicorn's ``H11Protocol`` so we don't reimplement
HTTP parsing. The override is narrow: ``connection_made`` extracts peer_pid
via ``getsockopt(SOL_LOCAL, LOCAL_PEERPID)`` from the underlying socket, and
``handle_events`` injects it into the constructed ASGI scope.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import logging
import os
import secrets
import socket
import stat
import string
from typing import Optional

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _path_lock(uds_path: str):
    """Hold an exclusive flock on ``<uds_path>.lock`` for the with-body.

    The lock file is left in place (removing it would reopen the race). The
    flock blocks while another process holds it, for up to a full stale-socket
    probe, so callers on an event loop run the locked section in a worker
    thread rather than on the loop.
    """
    fd = os.open(uds_path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the fd releases the flock


NodeId = tuple[int, int, int]


def _node_id(path: str) -> NodeId:
    """Identify the filesystem node at ``path``: (device, inode, ctime_ns).

    The inode number alone is not enough: once our node is unlinked, the
    filesystem may give the same number to the next node created, such as a
    replacement server's socket (ext4 does this readily). That node's change
    time cannot match ours, so the triple tells them apart.
    """
    st = os.stat(path)
    return (st.st_dev, st.st_ino, st.st_ctime_ns)


# Identity of the socket node this process bound at each path, set when a
# start succeeds. Shutdown unlinks a path only while it still holds that node.
_bound_nodes: dict[str, NodeId] = {}


def unlink_own_socket(uds_path: str, node: Optional[NodeId] = None) -> bool:
    """Unlink ``uds_path`` only if it is still the node this process bound.

    ``node`` defaults to the identity recorded by a successful start. Once our
    listener stops accepting, a replacement server can probe the path as stale
    and bind its own socket there (#2662); a blind unlink at our shutdown would
    then delete its live socket. The check runs under the path lock, so no
    start can replace the node between the stat and the unlink. Never raises:
    a lock file that cannot be opened (EMFILE, a permission change) leaves the
    node in place rather than failing a cleanup path. It blocks on that lock,
    so an async caller runs it with ``asyncio.to_thread``.
    """
    if node is None:
        node = _bound_nodes.pop(uds_path, None)
    if node is None:
        return False
    try:
        with _path_lock(uds_path):
            if _node_id(uds_path) != node:
                return False
            os.unlink(uds_path)
    except OSError:
        return False
    return True


def _read_peer_pid_from_transport(transport: asyncio.BaseTransport) -> Optional[int]:
    """Extract kernel-attested peer PID from the underlying socket.

    Thin wrapper that reaches into uvicorn's transport to find the socket,
    then delegates to ``peer_attestation.read_peer_pid``. Returns ``None``
    when the transport has no socket (defensive — only happens with non-
    standard transports) or when the socket is not AF_UNIX. The kernel
    writes peer PID at ``connect()``/``accept()``; user-space cannot forge it.
    """
    sock = transport.get_extra_info("socket")
    if sock is None:
        return None
    from src.substrate import peer_attestation

    return peer_attestation.read_peer_pid(sock)


def make_peer_cred_protocol_class() -> type:
    """Build a ``H11Protocol`` subclass that injects peer_pid into ASGI scope.

    Lazy import keeps the module loadable in environments without uvicorn
    (e.g. unit-test contexts that import this module to reach
    ``_read_peer_pid_from_transport``).
    """
    from uvicorn.protocols.http.h11_impl import H11Protocol

    class PeerCredHTTPProtocol(H11Protocol):
        """H11Protocol that captures kernel-attested peer PID at connect time
        and stamps it onto every ASGI scope it constructs.

        The capture is per-connection; the same peer_pid value flows into
        every request scope on that connection. This is correct: once a
        UDS connection is established, the peer process at the other end
        is fixed for the connection's lifetime (no migration, no switch).
        """

        def connection_made(  # type: ignore[override]
            self, transport: asyncio.Transport
        ) -> None:
            super().connection_made(transport)
            self._unitares_peer_pid = _read_peer_pid_from_transport(transport)
            if self._unitares_peer_pid is not None:
                logger.debug(
                    "[UDS] connection_made peer_pid=%d", self._unitares_peer_pid
                )

        def handle_events(self) -> None:
            # Run the parent's request/response cycle, which builds self.scope
            # for each request. We patch peer_pid into the scope as soon as
            # it exists. The hook is light: a single dict update on the
            # scope dict immediately after H11Protocol assigned it.
            super().handle_events()
            scope = getattr(self, "scope", None)
            if scope is None:
                return
            peer_pid = getattr(self, "_unitares_peer_pid", None)
            if peer_pid is not None and "unitares_peer_pid" not in scope:
                # Add as a top-level scope key. We avoid the
                # ASGI ``extensions`` key because that's reserved for
                # standard extensions; ``unitares_*`` is namespaced clearly
                # and won't collide with future ASGI vocabulary.
                scope["unitares_peer_pid"] = peer_pid

    return PeerCredHTTPProtocol


#: Listen backlog for the pre-bound UDS socket (uvicorn's own default is 2048).
_UDS_BACKLOG = 2048

_PRIVATE_DIR_ALPHABET = string.ascii_lowercase + string.digits


def _make_private_dir(sock_dir: str, basename_len: int) -> str:
    """Create an owner-only (0700) directory beside the socket path.

    The name is sized so that ``<private_dir>/s`` is no longer than the socket's
    own path whenever the basename has at least four characters, so a socket
    path that fits ``sun_path`` (104 bytes on macOS) still fits while staged.
    """
    name_len = max(1, basename_len - 3)
    for _ in range(100):
        name = "." + "".join(
            secrets.choice(_PRIVATE_DIR_ALPHABET) for _ in range(name_len)
        )
        path = os.path.join(sock_dir, name)
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            continue
        # mkdir applies the umask, which can only remove bits: re-assert 0700 so
        # an unusual umask cannot leave the owner unable to bind inside it.
        os.chmod(path, 0o700)
        return path
    raise FileExistsError(errno.EEXIST, f"no free private bind directory in {sock_dir}")


def _remove_private_dir(private_dir: str, staged: str) -> None:
    """Remove the staged name and its directory; a second call is a no-op."""
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(staged)
        with contextlib.suppress(FileNotFoundError):
            os.rmdir(private_dir)
    except OSError as exc:
        # Litter, not a fault: the directory is owner-only and the listener at
        # the real path is unaffected.
        logger.warning("[UDS] could not remove bind directory %s: %s", private_dir, exc)


def _bind_into_place(uds_path: str) -> tuple[socket.socket, NodeId]:
    """Bind a listening 0600 socket and link it into ``uds_path``.

    The socket is bound inside a fresh 0700 directory, where no other user can
    reach it, then ``chmod``-ed to 0600 and put into ``listen`` before
    ``os.link`` gives it its real name. The node therefore appears at
    ``uds_path`` already owner-only and already accepting, and the process
    umask is never touched (changing it is process-wide, so a thread creating a
    file in that window would inherit the restrictive mask). ``link`` refuses
    an existing name, so a node that appeared at the path since the stale
    probe is never displaced.

    Returns the listening socket and the identity of its node at
    ``uds_path``, read after the staged name is gone: ``link`` and ``unlink``
    both move the node's change time. A failure before the link leaves no node
    at the path; one after it leaves at most our closed socket's node, which is
    stale, so the next start's probe removes it.
    """
    sock_dir = os.path.dirname(uds_path) or "."
    private_dir = _make_private_dir(sock_dir, len(os.path.basename(uds_path)))
    staged = os.path.join(private_dir, "s")
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(staged)
            os.chmod(staged, 0o600)
            # listen() before handing the socket to uvicorn so the kernel
            # queues connections immediately; otherwise a client connecting
            # before uvicorn's serve() task calls listen() gets ECONNREFUSED.
            # asyncio's create_server(sock=...) calling listen() again is
            # harmless.
            sock.listen(_UDS_BACKLOG)
            sock.setblocking(False)
            staged_st = os.stat(staged)
            try:
                os.link(staged, uds_path)
            except FileExistsError as exc:
                raise OSError(
                    errno.EADDRINUSE,
                    f"{uds_path} appeared during bind; refusing to displace it",
                ) from exc
            _remove_private_dir(private_dir, staged)
            bound_node = _node_id(uds_path)
            if bound_node[:2] != (staged_st.st_dev, staged_st.st_ino):
                raise OSError(
                    errno.EADDRINUSE,
                    f"{uds_path} was replaced during bind; refusing to claim it",
                )
        except BaseException:
            sock.close()
            raise
    finally:
        _remove_private_dir(private_dir, staged)
    return sock, bound_node


def _claim_and_bind(uds_path: str) -> tuple[socket.socket, NodeId]:
    """Probe, clear a stale node and bind, under the path lock.

    Blocking (the flock and the up-to-1s probe), so start_uds_listener runs it
    in a worker thread.
    """
    sock_dir = os.path.dirname(uds_path)
    if sock_dir:
        os.makedirs(sock_dir, mode=0o700, exist_ok=True)
    # Serialize probe/unlink/bind across processes: two starts that both see
    # the same stale path must not unlink each other's fresh bind (#2662).
    with _path_lock(uds_path):
        if os.path.exists(uds_path):
            # Refuse to displace a live listener (#2662): only a stale socket
            # file (nobody accepting) is safe to remove.
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(1.0)
                probe.connect(uds_path)
            except OSError as exc:
                # Only these mean nobody is accepting. Anything else (EACCES from
                # another UID's 0600 socket, EAGAIN/ETIMEDOUT from a saturated
                # listener) may be a live owner, so refuse rather than displace it.
                if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT, errno.ENOTSOCK):
                    raise OSError(
                        errno.EADDRINUSE,
                        f"cannot prove {uds_path} is stale ({exc}); refusing to displace it",
                    ) from exc
            else:
                raise OSError(
                    errno.EADDRINUSE, f"live listener already bound at {uds_path}"
                )
            finally:
                probe.close()
            try:
                os.unlink(uds_path)
            except OSError as exc:
                logger.warning("[UDS] could not unlink stale socket %s: %s", uds_path, exc)
        return _bind_into_place(uds_path)


def _release(uds_path: str, sock: socket.socket, node: NodeId) -> None:
    """Remove our node (identity-checked, under the lock), then close the socket.

    Unlinking first means a concurrent start still probes a live listener
    rather than a stale path, and the identity check leaves alone any node
    that is no longer ours.
    """
    try:
        unlink_own_socket(uds_path, node)
    finally:
        sock.close()


def _release_off_loop(
    loop: asyncio.AbstractEventLoop, uds_path: str, sock: socket.socket, node: NodeId
) -> "asyncio.Future[None]":
    """Run ``_release`` in a worker thread, so the lock wait never blocks the loop.

    The release runs to completion even if whoever awaits it is cancelled.
    """
    try:
        return loop.run_in_executor(None, _release, uds_path, sock, node)
    except RuntimeError:
        # The executor is already shut down. Closing never blocks, and a closed
        # socket's node is stale, which the next start's probe removes.
        sock.close()
        done = loop.create_future()
        done.set_result(None)
        return done


async def start_uds_listener(
    app, uds_path: str, *, log_level: str = "info"
) -> "asyncio.Task[None]":
    """Start a uvicorn UDS listener as a background task.

    Returns the task; caller is responsible for cancellation at shutdown.

    **Socket permissions are 0600 (owner-only), race-free.** We bind the
    AF_UNIX socket *ourselves* and hand it to uvicorn via ``serve(sockets=...)``
    rather than letting uvicorn bind a ``uds=`` path. The reason is a real
    incident (governance.sock observed at mode 0666 live, 2026-06-17): when
    uvicorn binds the uds itself it ``chmod``s the socket to ``0o666`` during
    startup, which *races with and overwrites* any post-bind ``chmod 0600`` we
    do — and on a restart that skips the tighten step, the socket simply stays
    world-writable. World-writable defeats the same-UID threat boundary the S19
    peer-cred design documents (proposal v2 §Adversary models): any local
    process could connect and present a UUID.

    By binding inside a private 0700 directory, ``chmod``-ing to 0600 and only
    then linking the node to its real path (``_bind_into_place``), the socket
    is *never* reachable at a looser mode, and because uvicorn is given an
    already-bound socket it never applies its own 0666. This holds as long as
    the socket's directory is not writable by another user without the sticky
    bit; such a user could replace the node at the real path in any case. The
    protocol class from ``make_peer_cred_protocol_class()`` still injects
    ``scope["unitares_peer_pid"]`` per request.

    The locked probe/unlink/bind section runs in a worker thread: its flock
    waits while another process holds the path, and that wait must not stall
    the event loop.
    """
    import uvicorn

    loop = asyncio.get_running_loop()
    claim = loop.run_in_executor(None, _claim_and_bind, uds_path)
    try:
        sock, bound_node = await asyncio.shield(claim)
    except asyncio.CancelledError:
        # The worker keeps running and may still bind. Release whatever it
        # binds, or the node would answer probes with nobody serving it.
        def _release_abandoned(fut: "asyncio.Future[tuple[socket.socket, NodeId]]") -> None:
            if fut.cancelled() or fut.exception() is not None:
                return
            abandoned_sock, abandoned_node = fut.result()
            _release_off_loop(loop, uds_path, abandoned_sock, abandoned_node)

        claim.add_done_callback(_release_abandoned)
        raise

    try:
        # Verify the on-disk mode is actually 0600 before we serve — surfaces any
        # future regression loudly instead of silently shipping a 0666 socket.
        actual_mode = stat.S_IMODE(os.stat(uds_path).st_mode)
        if actual_mode != 0o600:
            logger.warning(
                "[UDS] socket %s mode is %o after bind+chmod, expected 0600",
                uds_path, actual_mode,
            )
        else:
            logger.info(
                "[UDS] listening at %s (mode 0600, peer-cred enabled)", uds_path
            )

        protocol_class = make_peer_cred_protocol_class()
        config = uvicorn.Config(
            app=app,
            # NOTE: no uds= here — we pass the pre-bound socket to serve() so
            # uvicorn does not re-bind (and does not apply its own 0666 chmod).
            http=protocol_class,
            log_level=log_level,
            # Disable uvicorn's lifespan and CORS — those are handled by the
            # primary HTTP listener; UDS is just a transport into the same app.
            lifespan="off",
            ws="none",
            access_log=False,
            backlog=_UDS_BACKLOG,
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(
            server.serve(sockets=[sock]), name="unitares-uds-listener"
        )
    except BaseException:
        # The caller claims no path on failure (#2662), so nothing else will
        # unlink this one: remove the node we bound and close the socket. The
        # release completes even if this wait is cancelled.
        await asyncio.shield(_release_off_loop(loop, uds_path, sock, bound_node))
        raise
    _bound_nodes[uds_path] = bound_node
    return task
