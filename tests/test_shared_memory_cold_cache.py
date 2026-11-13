"""A shared-memory pause decision must resolve cold-cache registry state."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.mcp_handlers.support.agent_auth import check_agent_can_operate_async
from tests.helpers import parse_result


@pytest.fixture
def gate(monkeypatch):
    server = SimpleNamespace(agent_metadata={})
    lookup = AsyncMock()
    monkeypatch.setattr('src.mcp_handlers.shared.get_mcp_server', lambda: server)
    monkeypatch.setattr('src.agent_metadata_persistence.load_agent_metadata_from_postgres', lookup)
    return server, lookup


def meta(status):
    return SimpleNamespace(status=status, paused_at=datetime.now(timezone.utc).isoformat())


@pytest.mark.asyncio
@pytest.mark.parametrize('status,code', [('paused', 'AGENT_PAUSED'), ('archived', 'AGENT_ARCHIVED'), ('active', None), ('waiting_input', None)])
async def test_cold_registry_status_enforced(gate, status, code):
    server, lookup = gate
    lookup.return_value = meta(status)
    result = await check_agent_can_operate_async('agent')
    assert (parse_result(result)['error_code'] if result else None) == code
    assert server.agent_metadata['agent'] is lookup.return_value
    lookup.assert_awaited_once_with('agent')


@pytest.mark.asyncio
async def test_registry_failure_refuses_without_negative_cache(gate):
    server, lookup = gate
    lookup.side_effect = RuntimeError('offline')
    assert parse_result(await check_agent_can_operate_async('agent'))['error_code'] == 'REGISTRY_UNAVAILABLE'
    assert server.agent_metadata == {}
    lookup.side_effect = None
    lookup.return_value = meta('paused')
    assert parse_result(await check_agent_can_operate_async('agent'))['error_code'] == 'AGENT_PAUSED'


@pytest.mark.asyncio
async def test_confirmed_absence_preserves_new_writer_policy(gate):
    server, lookup = gate
    lookup.return_value = None
    assert await check_agent_can_operate_async('new-writer') is None
    assert server.agent_metadata == {}


@pytest.mark.asyncio
async def test_cached_agent_needs_no_lookup(gate):
    server, lookup = gate
    server.agent_metadata['agent'] = meta('active')
    assert await check_agent_can_operate_async('agent') is None
    lookup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('snapshot', [None, meta('active')])
async def test_concurrent_pause_wins_over_registry_snapshot(gate, snapshot):
    server, lookup = gate
    paused = meta('paused')
    async def load(_):
        # A bulk load can replace the cache object while the read is in flight.
        server.agent_metadata = {'agent': paused}
        return snapshot
    lookup.side_effect = load
    assert parse_result(await check_agent_can_operate_async('agent'))['error_code'] == 'AGENT_PAUSED'
    assert server.agent_metadata['agent'] is paused


@pytest.mark.asyncio
@pytest.mark.parametrize('status,code', [('paused', 'AGENT_PAUSED'), ('archived', 'AGENT_ARCHIVED')])
async def test_stale_active_bulk_snapshot_does_not_override_restrictive_lookup(gate, status, code):
    server, lookup = gate
    async def load(_):
        server.agent_metadata = {'agent': meta('active')}
        return meta(status)
    lookup.side_effect = load
    assert parse_result(await check_agent_can_operate_async('agent'))['error_code'] == code
    assert parse_result(await check_agent_can_operate_async('agent'))['error_code'] == code
    lookup.assert_awaited_once()
