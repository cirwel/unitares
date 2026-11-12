"""A check-in asks PostgreSQL before treating a cache miss as "not registered".

``agent_metadata`` is a cache of the registry. A registered agent is missing
from it until the background load finishes after a server start, and for good
once it is older than the bulk loader's row cap. Before #3243 the check-in
read that absence as "new agent", and under STRICT_IDENTITY_REQUIRED refused a
resumed agent with ``identity_required`` although its row was in PostgreSQL.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.agent_metadata_persistence as persistence
import src.db as dbmod
import src.grounding.onboard_classifier as classifier
import src.mcp_handlers.context as ctxmod
import src.mcp_handlers.identity.handlers as id_handlers
import src.mcp_handlers.identity_bootstrap as ib
from src import agent_storage
from src.agent_storage import AgentRecord
from src.mcp_handlers.updates import phases
from src.mcp_handlers.updates.context import UpdateContext
from src.mcp_handlers.updates.phases import (
    execute_locked_update,
    resolve_identity_and_guards,
)
from tests.helpers import parse_result

AGENT_UUID = "11111111-2222-4333-8444-555555555555"


def _record(status="active", **metadata):
    return AgentRecord(
        agent_id=AGENT_UUID,
        api_key="",
        api_key_hash="h",
        status=status,
        created_at=datetime(2026, 4, 7, tzinfo=timezone.utc),
        tags=["persistent"],
        metadata={"label": "resident", "tags": ["persistent"], **metadata},
    )


def _patch_context(monkeypatch, *, strict=True, label=None):
    monkeypatch.setattr(ctxmod, "get_context_agent_id", lambda: AGENT_UUID)
    monkeypatch.setattr(ctxmod, "get_context_session_key", lambda: "k")
    monkeypatch.setattr(ctxmod, "get_session_resolution_source",
                        lambda: "explicit_client_session_id")
    monkeypatch.setattr(ctxmod, "get_session_proof_origin", lambda: "caller_asserted")
    monkeypatch.setattr(ctxmod, "get_trajectory_confidence", lambda: None)
    monkeypatch.setattr(ib, "is_strict_identity_required", lambda: strict)
    monkeypatch.setattr(classifier, "reconcile_resident_tags", AsyncMock(return_value=None))

    class _DB:
        async def update_agent_fields(self, *a, **k):
            return None

        async def get_agent_label(self, agent_id):
            return label
    monkeypatch.setattr(dbmod, "get_db", lambda: _DB())
    monkeypatch.setattr(id_handlers, "ensure_agent_persisted", AsyncMock(return_value=False))


def _cold_ctx():
    """A check-in arriving while the metadata cache is still empty."""
    ctx = UpdateContext(arguments={})
    ctx.mcp_server = SimpleNamespace(agent_metadata={})
    return ctx


def _registry(monkeypatch, result):
    lookup = AsyncMock(side_effect=result) if isinstance(result, Exception) \
        else AsyncMock(return_value=result)
    monkeypatch.setattr(agent_storage, "get_agent", lookup)
    return lookup


@pytest.mark.asyncio
async def test_registered_agent_missing_from_cold_cache_is_not_new(monkeypatch):
    _patch_context(monkeypatch)
    lookup = _registry(monkeypatch, _record())
    ctx = _cold_ctx()

    assert await resolve_identity_and_guards(ctx) is None

    lookup.assert_awaited_once_with(AGENT_UUID)
    assert ctx.is_new_agent is False
    meta = ctx.mcp_server.agent_metadata[AGENT_UUID]
    assert meta.status == "active"
    assert meta.label == "resident"
    assert meta.tags == ["persistent"]


@pytest.mark.asyncio
async def test_unregistered_agent_is_still_refused_under_strict(monkeypatch):
    """The gate keeps its job: no row in PostgreSQL, no check-in."""
    _patch_context(monkeypatch)
    _registry(monkeypatch, None)
    ctx = _cold_ctx()

    assert await resolve_identity_and_guards(ctx) is None
    assert ctx.is_new_agent is True
    assert AGENT_UUID not in ctx.mcp_server.agent_metadata

    data = parse_result(await execute_locked_update(ctx))
    assert data["status"] == "identity_required"


@pytest.mark.asyncio
async def test_lookup_error_fails_closed(monkeypatch):
    """An unreadable registry is not evidence of registration."""
    _patch_context(monkeypatch)
    _registry(monkeypatch, RuntimeError("pool closed"))
    ctx = _cold_ctx()

    assert await resolve_identity_and_guards(ctx) is None
    assert ctx.is_new_agent is True

    data = parse_result(await execute_locked_update(ctx))
    assert data["status"] == "identity_required"


@pytest.mark.asyncio
async def test_deleted_identity_is_not_registered(monkeypatch):
    _patch_context(monkeypatch)
    _registry(monkeypatch, _record(status="deleted"))
    ctx = _cold_ctx()

    assert await resolve_identity_and_guards(ctx) is None
    assert ctx.is_new_agent is True


@pytest.mark.asyncio
async def test_paused_agent_is_refused_from_a_cold_cache(monkeypatch):
    """The lookup runs ahead of the pause guard, so a pause recorded in
    PostgreSQL holds during the window in which the cache is empty."""
    _patch_context(monkeypatch)
    _registry(monkeypatch, _record(
        status="paused", paused_at=datetime.now(timezone.utc).isoformat(),
    ))
    ctx = _cold_ctx()

    result = await resolve_identity_and_guards(ctx)

    assert result is not None
    assert parse_result(result)["error_code"] == "AGENT_PAUSED"


@pytest.mark.asyncio
async def test_cached_agent_costs_no_registry_read(monkeypatch):
    _patch_context(monkeypatch)
    lookup = _registry(monkeypatch, _record())
    ctx = _cold_ctx()
    cached = SimpleNamespace(status="active", paused_at=None, label="resident", tags=[])
    ctx.mcp_server.agent_metadata[AGENT_UUID] = cached

    assert await resolve_identity_and_guards(ctx) is None

    lookup.assert_not_awaited()
    assert ctx.mcp_server.agent_metadata[AGENT_UUID] is cached


@pytest.mark.asyncio
async def test_bulk_load_landing_mid_lookup_keeps_its_entry(monkeypatch):
    """If the background load fills the cache while the single read is in
    flight, its complete entry wins over the one-record copy."""
    _patch_context(monkeypatch)
    ctx = _cold_ctx()
    ctx.agent_uuid = AGENT_UUID
    bulk = SimpleNamespace(status="active", paused_at=None, label="resident", tags=[])

    async def _get_agent(agent_id):
        ctx.mcp_server.agent_metadata[agent_id] = bulk
        return _record()
    monkeypatch.setattr(agent_storage, "get_agent", _get_agent)

    await phases._hydrate_registered_agent(ctx)

    assert ctx.mcp_server.agent_metadata[AGENT_UUID] is bulk


def test_bulk_and_single_loaders_build_the_same_entry():
    """Both loaders go through one builder, so a persisted field cannot reach
    the cache by one route and not the other."""
    record = _record(status="paused", paused_at="2026-11-12T01:09:00+00:00",
                     thread_id="t-1", node_index=4, total_updates=9)
    meta = persistence._metadata_from_agent_record(record, "2026-11-12T00:00:00+00:00")

    assert (meta.status, meta.paused_at) == ("paused", "2026-11-12T01:09:00+00:00")
    assert (meta.thread_id, meta.node_index, meta.total_updates) == ("t-1", 4, 9)
    assert meta.created_at == "2026-04-07T00:00:00+00:00"


@pytest.mark.asyncio
async def test_label_comes_from_core_agents_as_in_the_bulk_load(monkeypatch):
    _patch_context(monkeypatch, label="renamed")
    _registry(monkeypatch, _record())
    ctx = _cold_ctx()

    assert await resolve_identity_and_guards(ctx) is None

    assert ctx.mcp_server.agent_metadata[AGENT_UUID].label == "renamed"
