"""The repo-root conftest keeps every test process off the live Redis."""

import os

import pytest


def test_the_suite_points_redis_at_an_unreachable_port():
    if os.environ.get("UNITARES_TEST_LIVE_REDIS") == "1":
        pytest.skip("live Redis explicitly allowed for this run")
    from src.cache.redis_client import RedisConfig

    config = RedisConfig()
    assert config.url == "redis://127.0.0.1:1/0"
    assert not config.sentinel_hosts


@pytest.mark.asyncio
async def test_an_unmocked_get_redis_does_not_reach_a_server():
    if os.environ.get("UNITARES_TEST_LIVE_REDIS") == "1":
        pytest.skip("live Redis explicitly allowed for this run")
    from src.cache import redis_client

    client = await redis_client.get_redis()
    if client is not None:
        # A client object may exist before any command; a command must fail.
        with pytest.raises(Exception):
            await client.ping()
