"""Lifecycle services for starting and stopping the HTTP MCP server."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.logging_utils import get_logger
from src.process_management import (
    SERVER_LOCK_FILE,
    SERVER_PID_FILE,
    acquire_server_lock,
    cleanup_existing_server_processes,
    ensure_server_lock,
    ensure_server_pid_file,
    is_process_alive,
    release_server_lock,
    remove_server_pid_file,
    write_server_pid_file,
)
from src.services.identity_continuity import (
    format_identity_continuity_startup_message,
    probe_identity_continuity_status,
)

logger = get_logger(__name__)


class ServerStartupError(RuntimeError):
    """Fatal startup error that should be shown without a traceback."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass
class ServerProcessLease:
    """Own the server lock, PID file, and their maintenance task."""

    lock_fd: int | None
    marker_task: asyncio.Task[None] | None = None

    @classmethod
    def acquire(cls, *, force: bool) -> ServerProcessLease:
        if force:
            _cleanup_forced_process_markers()

        killed = cleanup_existing_server_processes()
        if killed:
            logger.info("Cleaned up %d existing server process(es)", len(killed))

        try:
            lock_fd = acquire_server_lock()
        except RuntimeError as exc:
            raise ServerStartupError(
                str(exc),
                hint="Use --force to clean up stale locks",
            ) from exc

        write_server_pid_file()
        lease = cls(lock_fd=lock_fd)
        lease.marker_task = asyncio.create_task(
            lease._maintain_process_markers(),
            name="mcp_process_marker_maintenance",
        )
        return lease

    async def _maintain_process_markers(self) -> None:
        while True:
            try:
                ensure_server_pid_file()
                self.lock_fd = ensure_server_lock(self.lock_fd)
            except Exception as exc:
                logger.debug("Process marker maintenance skipped: %s", exc)
            await asyncio.sleep(15)

    async def stop_maintenance(self) -> None:
        if self.marker_task is None:
            return
        self.marker_task.cancel()
        try:
            await self.marker_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("Error stopping process marker maintenance: %s", exc)
        finally:
            self.marker_task = None

    def release(self) -> None:
        release_server_lock(self.lock_fd)
        self.lock_fd = None
        remove_server_pid_file()


@dataclass
class ServerBootstrap:
    """Resources acquired before the transport server starts."""

    process_lease: ServerProcessLease
    db: Any

    async def shutdown(self) -> None:
        """Release server resources in their established shutdown order."""
        await self.process_lease.stop_maintenance()

        try:
            from src.background_tasks import stop_all_background_tasks

            await stop_all_background_tasks()
        except Exception as exc:
            logger.debug("Error stopping background tasks: %s", exc)

        try:
            from src.db import close_db

            await close_db()
        except Exception as exc:
            logger.warning("Error closing database: %s", exc)

        self.process_lease.release()


def _cleanup_forced_process_markers() -> None:
    logger.info("--force: Cleaning up stale lock and PID files")
    try:
        if SERVER_LOCK_FILE.exists():
            SERVER_LOCK_FILE.unlink()
            logger.info("Removed lock file: %s", SERVER_LOCK_FILE)
    except Exception as exc:
        logger.warning("Could not remove lock file: %s", exc)

    try:
        if not SERVER_PID_FILE.exists():
            return
        try:
            old_pid = int(SERVER_PID_FILE.read_text().strip())
            if not is_process_alive(old_pid):
                SERVER_PID_FILE.unlink()
                logger.info(
                    "Removed stale PID file: %s (PID %d not running)",
                    SERVER_PID_FILE,
                    old_pid,
                )
            else:
                logger.warning(
                    "PID file exists for running process %d, will terminate it",
                    old_pid,
                )
        except (ValueError, OSError):
            SERVER_PID_FILE.unlink()
            logger.info("Removed invalid PID file: %s", SERVER_PID_FILE)
    except Exception as exc:
        logger.warning("Could not remove PID file: %s", exc)


def load_entrypoint_plugins() -> None:
    """Load plugins after the handler registry is fully initialized."""
    from src.mcp_handlers import refresh_tool_handlers_from_registry
    from src.plugin_loader import load_plugins

    loaded = load_plugins()
    added = refresh_tool_handlers_from_registry()
    if loaded:
        logger.info("plugins loaded: %s (+%d tools)", loaded, added)


def sync_declared_host(mcp: Any, host: str) -> None:
    """Keep FastMCP's declared host aligned with the uvicorn bind host."""
    try:
        mcp.settings.host = host
    except Exception as exc:
        logger.debug("Could not sync mcp.settings.host to %s: %s", host, exc)


def _cleanup_stale_agent_locks(project_root: Path) -> None:
    try:
        from src.lock_cleanup import cleanup_stale_state_locks

        cleanup_result = cleanup_stale_state_locks(
            project_root=project_root,
            max_age_seconds=300.0,
        )
        if cleanup_result.get("cleaned", 0) > 0:
            logger.info(
                "Cleaned up %d stale agent lock(s) at startup",
                cleanup_result["cleaned"],
            )
    except Exception as exc:
        logger.warning("Could not clean up stale locks at startup: %s", exc)


async def _initialize_database() -> Any:
    from src.db import get_db, init_db

    await init_db()
    db = get_db()
    logger.info("Database initialized: backend=postgres")
    return db


async def _report_identity_continuity() -> None:
    continuity_status = await probe_identity_continuity_status()
    continuity_message = format_identity_continuity_startup_message(continuity_status)
    if continuity_status.get("mode") == "redis":
        logger.info(continuity_message)
    else:
        logger.warning(continuity_message)


async def _seed_event_detector(db: Any) -> None:
    """Seed recently active agents so restarts do not emit false agent_new events.

    This must use the activity-ordered query. Creation ordering can push an old,
    still-active substrate resident out of the seed as ephemeral agents accrue.
    """
    try:
        from src.event_detector import event_detector

        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        identities = await db.list_recently_active_identities(cutoff, limit=500)
        recent = [
            (identity.agent_id, identity.metadata.get("label") or identity.agent_id[:12])
            for identity in identities
        ]
        seeded = event_detector.seed_known_agents(recent)
        if seeded:
            logger.info("Event detector seeded with %d known agent(s)", seeded)
    except Exception as exc:
        logger.warning("Could not seed event detector: %s", exc)


def _bind_audit_event_loop() -> None:
    from src.audit_log import AuditLogger

    AuditLogger._event_loop = asyncio.get_running_loop()


def announce_server(*, host: str, port: int, version: str) -> None:
    endpoint = f"http://{host}:{port}/mcp"
    print(
        f"""
╔════════════════════════════════════════════════════════════════════╗
║       UNITARES Governance MCP Server                               ║
╠════════════════════════════════════════════════════════════════════╣
║  Version:  {version}                                                   ║
║                                                                    ║
║  MCP Transport:                                                    ║
║    Streamable HTTP:    {endpoint:<46}║
║                                                                    ║
║  REST API:                                                         ║
║    List tools:         GET  /v1/tools                              ║
║    Call tool:          POST /v1/tools/call                         ║
║    Health:             GET  /health                                ║
║    Metrics:            GET  /metrics                               ║
╚════════════════════════════════════════════════════════════════════╝
"""
    )

    logger.info("Starting governance server on %s", endpoint)
    if host in ("127.0.0.1", "::1", "localhost"):
        logger.info(
            "Listening on loopback only. For LAN/tunnel set --host 0.0.0.0 or "
            "UNITARES_BIND_ALL_INTERFACES=1, and configure "
            "UNITARES_MCP_ALLOWED_HOSTS / UNITARES_MCP_ALLOWED_ORIGINS."
        )


async def bootstrap_server(
    *,
    force: bool,
    host: str,
    port: int,
    version: str,
    project_root: Path,
    mcp: Any,
) -> ServerBootstrap:
    """Acquire process resources and initialize server dependencies."""
    load_entrypoint_plugins()
    sync_declared_host(mcp, host)
    process_lease = ServerProcessLease.acquire(force=force)

    try:
        _cleanup_stale_agent_locks(project_root)
        db = await _initialize_database()
        await _report_identity_continuity()
        await _seed_event_detector(db)
        _bind_audit_event_loop()
        announce_server(host=host, port=port, version=version)
        return ServerBootstrap(process_lease=process_lease, db=db)
    except Exception as exc:
        await process_lease.stop_maintenance()
        process_lease.release()
        logger.error("Failed to initialize server dependencies: %s", exc)
        raise ServerStartupError(f"Server initialization failed: {exc}") from exc
