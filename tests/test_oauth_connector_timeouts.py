"""Connector DB deadlines execute on the real ExecutorPool's owning loop."""
import asyncio
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src import oauth_connectors as registry
from src.db.executor_pool import ExecutorPool


CLIENT_ID = registry.CLIENT_PREFIX + "a" * 32
CALLBACK = "https://client.example/callback"


class ConnectorDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.committed = False
        self.delay = 0
        self.deadline_finished = threading.Event()
        raw_conn = MagicMock()
        raw_conn.fetchrow = self.fetchrow
        raw_conn.fetch = self.fetch
        raw_conn.execute = self.execute
        acquire = AsyncMock()
        acquire.__aenter__.return_value = raw_conn
        self.raw_pool = MagicMock()
        self.raw_pool.acquire.return_value = acquire
        self.raw_pool.close = AsyncMock()
        self.pool = ExecutorPool(self.raw_pool)
        self.registry = registry.ConnectorRegistry()
        self.db_patch = patch.object(registry, "get_db", return_value=self.pool)
        self.db_patch.start()

    async def asyncTearDown(self):
        self.db_patch.stop()
        await self.pool.close()

    def record(self, method, timeout):
        self.calls.append((method, timeout, threading.get_ident()))

    async def fetchrow(self, query, *args, timeout=None):
        self.record("fetchrow", timeout)
        if query.startswith("SELECT"):
            return None
        # Emulate asyncpg's statement deadline INSIDE its executor loop.
        # A missing forwarded deadline lets the delayed INSERT complete,
        # so this test fails if the caller-side wrapper is restored.
        try:
            async with asyncio.timeout(timeout):
                await asyncio.sleep(self.delay)
                self.committed = True
                return {"client_id": args[0], "name": args[1],
                        "redirect_uris": args[3], "created_at": datetime.now(timezone.utc),
                        "revoked_at": None}
        finally:
            self.deadline_finished.set()

    async def fetch(self, query, *, timeout=None):
        self.record("fetch", timeout)
        return []

    async def execute(self, query, *args, timeout=None):
        self.record("execute", timeout)
        return "UPDATE 1"

    async def test_all_operations_forward_deadlines_to_asyncpg(self):
        caller_thread = threading.get_ident()
        await self.registry.get(CLIENT_ID)
        await self.registry.list()
        result = await self.registry.create("App", [CALLBACK], "operator")
        self.assertIn("client_secret", result)
        self.assertTrue(await self.registry.revoke(CLIENT_ID, "operator"))
        self.assertEqual(len(self.calls), 4)
        for _, timeout, thread in self.calls:
            self.assertEqual(timeout, registry.DB_TIMEOUT_SECONDS)
            self.assertNotEqual(thread, caller_thread)
        self.assertEqual(self.raw_pool.acquire.call_count, 4)
        for call in self.raw_pool.acquire.call_args_list:
            self.assertEqual(call.kwargs, {"timeout": registry.DB_TIMEOUT_SECONDS})

    async def test_slow_insert_finishes_with_deadline_before_returning_failure(self):
        self.delay = 0.1
        with patch.object(registry, "DB_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(TimeoutError):
                await self.registry.create("App", [CALLBACK], "operator")
        self.assertTrue(self.deadline_finished.is_set())
        self.assertFalse(self.committed)
        # This models a query deadline before commit, not ambiguous network
        # loss after a real Postgres commit. No exactly-once claim is made.
