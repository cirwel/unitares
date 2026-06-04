"""P004 regression: enrich_identity_notifications must bound its Redis awaits.

A hung Redis (anyio<->asyncpg/Redis deadlock class) must not stall the update
enrichment pipeline — the enrichment degrades to "no notifications this turn".
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_handlers.updates import enrichments
from src.mcp_handlers.updates.enrichments import enrich_identity_notifications


def _ctx():
    return SimpleNamespace(agent_uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", response_data={})


@pytest.mark.asyncio
async def test_hung_redis_lrange_does_not_stall(monkeypatch):
    """If redis.lrange hangs, the enrichment times out and returns cleanly."""
    monkeypatch.setattr(enrichments, "_REDIS_NOTIF_TIMEOUT", 0.01)

    async def _hang(*_a, **_k):
        await asyncio.sleep(5)  # never completes within the timeout
        return []

    fake_redis = SimpleNamespace(lrange=_hang, delete=AsyncMock())
    ctx = _ctx()

    with patch("src.cache.redis_client.get_redis", new=AsyncMock(return_value=fake_redis)):
        # Must finish well under the hang; generous outer bound guards against regressions.
        await asyncio.wait_for(enrich_identity_notifications(ctx), timeout=1.0)

    assert "_identity_notifications" not in ctx.response_data
    fake_redis.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_happy_path_surfaces_and_clears(monkeypatch):
    """Normal path: notifications are surfaced and the key is cleared."""
    fake_redis = SimpleNamespace(
        lrange=AsyncMock(return_value=[json.dumps({"message": "session accessed elsewhere"})]),
        delete=AsyncMock(),
    )
    ctx = _ctx()

    with patch("src.cache.redis_client.get_redis", new=AsyncMock(return_value=fake_redis)):
        await enrich_identity_notifications(ctx)

    assert ctx.response_data["_identity_notifications"] == [{"message": "session accessed elsewhere"}]
    fake_redis.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_redis_is_noop():
    """When Redis is unavailable, the enrichment is a clean no-op."""
    ctx = _ctx()
    with patch("src.cache.redis_client.get_redis", new=AsyncMock(return_value=None)):
        await enrich_identity_notifications(ctx)
    assert "_identity_notifications" not in ctx.response_data
