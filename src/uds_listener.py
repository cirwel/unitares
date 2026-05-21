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
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


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


async def start_uds_listener(
    app, uds_path: str, *, log_level: str = "info"
) -> "asyncio.Task[None]":
    """Start a uvicorn UDS listener as a background task.

    Returns the task; caller is responsible for cancellation at shutdown.
    The socket file is created with mode 0600 (owner-only read/write) so
    only the same OS user can connect — matches the same-UID threat
    boundary documented in proposal v2 §Adversary models.

    The protocol class is the one built by ``make_peer_cred_protocol_class()``,
    so every request scope will have ``scope["unitares_peer_pid"]`` set when
    the kernel was able to read peer credentials.
    """
    import uvicorn

    # Pre-create the socket dir if needed; remove stale socket file.
    sock_dir = os.path.dirname(uds_path)
    if sock_dir:
        os.makedirs(sock_dir, mode=0o700, exist_ok=True)
    if os.path.exists(uds_path):
        try:
            os.unlink(uds_path)
        except OSError as exc:
            logger.warning("[UDS] could not unlink stale socket %s: %s", uds_path, exc)

    protocol_class = make_peer_cred_protocol_class()
    config = uvicorn.Config(
        app=app,
        uds=uds_path,
        http=protocol_class,
        log_level=log_level,
        # Disable uvicorn's lifespan and CORS — those are handled by the
        # primary HTTP listener; UDS is just a transport into the same app.
        lifespan="off",
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="unitares-uds-listener")

    # Tighten socket permissions to mode 0600 once uvicorn has created it.
    # We poll briefly for the file because uvicorn binds asynchronously.
    async def _tighten_socket_mode() -> None:
        for _ in range(50):  # up to ~5s
            if os.path.exists(uds_path):
                try:
                    os.chmod(uds_path, 0o600)
                    logger.info(
                        "[UDS] listening at %s (mode 0600, peer-cred enabled)",
                        uds_path,
                    )
                except OSError as exc:
                    logger.warning(
                        "[UDS] chmod 0600 failed for %s: %s", uds_path, exc
                    )
                return
            await asyncio.sleep(0.1)
        logger.warning(
            "[UDS] socket %s did not appear within 5s; chmod skipped",
            uds_path,
        )

    asyncio.create_task(_tighten_socket_mode(), name="unitares-uds-chmod")
    return task
