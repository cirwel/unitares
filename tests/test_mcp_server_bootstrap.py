"""Unit tests for MCP server bootstrap resource ownership."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.services import mcp_server_bootstrap as bootstrap


@pytest.mark.asyncio
async def test_shutdown_releases_resources_in_order(monkeypatch):
    events = []

    class Lease:
        async def stop_maintenance(self):
            events.append("stop_markers")

        def release(self):
            events.append("release_process")

    async def stop_background_tasks():
        events.append("stop_background")

    async def close_db():
        events.append("close_db")

    monkeypatch.setattr(
        "src.background_tasks.stop_all_background_tasks",
        stop_background_tasks,
    )
    monkeypatch.setattr("src.db.close_db", close_db)

    state = bootstrap.ServerBootstrap(process_lease=Lease(), db=object())
    await state.shutdown()

    assert events == [
        "stop_markers",
        "stop_background",
        "close_db",
        "release_process",
    ]


@pytest.mark.asyncio
async def test_bootstrap_releases_process_lease_when_database_init_fails(monkeypatch):
    lease = AsyncMock()
    lease.release = lambda: setattr(lease, "released", True)
    monkeypatch.setattr(
        bootstrap.ServerProcessLease,
        "acquire",
        lambda **_kwargs: lease,
    )
    monkeypatch.setattr(bootstrap, "load_entrypoint_plugins", lambda: None)
    monkeypatch.setattr(bootstrap, "sync_declared_host", lambda *_args: None)
    monkeypatch.setattr(bootstrap, "_cleanup_stale_agent_locks", lambda *_args: None)
    monkeypatch.setattr(
        bootstrap,
        "_initialize_database",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )

    with pytest.raises(bootstrap.ServerStartupError, match="database unavailable"):
        await bootstrap.bootstrap_server(
            force=False,
            host="127.0.0.1",
            port=8767,
            version="test",
            project_root=bootstrap.Path("."),
            mcp=object(),
        )

    lease.stop_maintenance.assert_awaited_once()
    assert lease.released is True


@pytest.mark.asyncio
async def test_bootstrap_initializes_dependencies_before_announcing(monkeypatch):
    events = []
    lease = AsyncMock()
    db = object()

    monkeypatch.setattr(
        bootstrap.ServerProcessLease,
        "acquire",
        lambda **_kwargs: lease,
    )
    monkeypatch.setattr(
        bootstrap,
        "load_entrypoint_plugins",
        lambda: events.append("plugins"),
    )
    monkeypatch.setattr(
        bootstrap,
        "sync_declared_host",
        lambda *_args: events.append("host"),
    )
    monkeypatch.setattr(
        bootstrap,
        "_cleanup_stale_agent_locks",
        lambda *_args: events.append("locks"),
    )

    async def initialize_database():
        events.append("database")
        return db

    async def report_continuity():
        events.append("continuity")

    async def seed_event_detector(_db):
        assert _db is db
        events.append("seed")

    monkeypatch.setattr(bootstrap, "_initialize_database", initialize_database)
    monkeypatch.setattr(bootstrap, "_report_identity_continuity", report_continuity)
    monkeypatch.setattr(bootstrap, "_seed_event_detector", seed_event_detector)
    monkeypatch.setattr(
        bootstrap,
        "_bind_audit_event_loop",
        lambda: events.append("audit"),
    )
    monkeypatch.setattr(
        bootstrap,
        "announce_server",
        lambda **_kwargs: events.append("announce"),
    )

    state = await bootstrap.bootstrap_server(
        force=False,
        host="127.0.0.1",
        port=8767,
        version="test",
        project_root=bootstrap.Path("."),
        mcp=object(),
    )

    assert state.process_lease is lease
    assert state.db is db
    assert events == [
        "plugins",
        "host",
        "locks",
        "database",
        "continuity",
        "seed",
        "audit",
        "announce",
    ]
