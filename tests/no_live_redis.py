"""Keep a test off the live Redis.

tests/conftest.py isolates PostgreSQL (``_isolate_db_backend``) but not Redis:
the client connects to ``redis://localhost:6379/0``, the operator's live
session store, the first time anything calls ``get_redis()``. Identity code
reaches it from paths a test does not obviously exercise: the shadow pin
observation in ``derive_session_key`` reads pins, and ``set_agent_label``
deletes a metadata-cache key.

Patching ``src.cache.redis_client.get_redis`` alone is not enough, because
four cache modules bind the function at import. This fixture replaces every
binding with one that reports Redis as unavailable, so the code under test
takes its no-Redis branch.

Use it per module::

    from tests.no_live_redis import no_live_redis  # noqa: F401

    pytestmark = pytest.mark.usefixtures("no_live_redis")
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

_GET_REDIS_BINDINGS = (
    "src.cache.redis_client.get_redis",
    "src.cache.get_redis",
    "src.cache.metadata_cache.get_redis",
    "src.cache.rate_limiter.get_redis",
    "src.cache.distributed_lock.get_redis",
    "src.cache.session_cache.get_redis",
)


@pytest.fixture
def no_live_redis():
    patches = [patch(target, AsyncMock(return_value=None)) for target in _GET_REDIS_BINDINGS]
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in reversed(patches):
            p.stop()
