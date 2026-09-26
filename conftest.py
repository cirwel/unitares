"""Repo-wide test guards that must be in place before any test module imports.

Redis: tests/conftest.py isolates PostgreSQL, but the Redis client connects to
REDIS_URL (default redis://localhost:6379/0, the operator's live session store)
the first time anything calls get_redis(), and identity code reaches it from
paths a test does not obviously exercise (shadow-pin reads in
derive_session_key, metadata-cache invalidation in set_agent_label). This file
sits at the repo root so it covers both testpaths (tests/ and agents/). It
points every test process at a local port nothing listens on, so an unmocked
call fails fast into the client's no-Redis branch instead of reading or
writing live keys. Tests that exercise the client use fakeredis or patch
REDIS_URL themselves, which still wins. Set UNITARES_TEST_LIVE_REDIS=1 to opt
out deliberately.
"""

import os

UNREACHABLE_REDIS_URL = "redis://127.0.0.1:1/0"

if os.environ.get("UNITARES_TEST_LIVE_REDIS") != "1":
    os.environ["REDIS_URL"] = UNREACHABLE_REDIS_URL
    os.environ.pop("REDIS_SENTINEL_HOSTS", None)
