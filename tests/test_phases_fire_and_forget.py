"""Regression tests for the fire-and-forget helpers added to phases.py.

The fire-and-forget pattern moves PG persistence (thread-identity metadata
and auto-inferred purpose) out of the agent lock so `execute_locked_update`
doesn't hold the lock across PG roundtrips. Same shape as PR #360's
`_hydrate_metadata_cache_async`.

These tests pin three invariants:
1. The helpers call the underlying persist function with correct arguments
2. Errors in the helpers are swallowed (no propagation to caller)
3. The helpers are coroutines schedulable via asyncio.create_task
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_handlers.updates.phases import (
    _persist_inferred_purpose_async,
    _persist_thread_identity_async,
)


# ─── _persist_thread_identity_async ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_thread_identity_calls_db_with_correct_args():
    """Helper passes through to db.update_identity_metadata with merge=True."""
    fake_db = AsyncMock()
    metadata = {
        "thread_id": "t-abc12345",
        "node_index": 3,
        "active_session_key": "session-xyz",
    }

    with patch("src.db.get_db", return_value=fake_db):
        await _persist_thread_identity_async("agent-uuid-123", metadata)

    fake_db.update_identity_metadata.assert_awaited_once_with(
        "agent-uuid-123", metadata=metadata, merge=True
    )


@pytest.mark.asyncio
async def test_persist_thread_identity_swallows_db_errors():
    """Helper logs and returns on db failure — does NOT raise."""
    fake_db = AsyncMock()
    fake_db.update_identity_metadata.side_effect = RuntimeError("PG offline")

    with patch("src.db.get_db", return_value=fake_db):
        # Must not raise
        await _persist_thread_identity_async("agent-uuid-123", {"thread_id": "t-x"})


@pytest.mark.asyncio
async def test_persist_thread_identity_swallows_get_db_errors():
    """Helper handles get_db itself failing (e.g., pool not initialized)."""
    with patch("src.db.get_db", side_effect=RuntimeError("pool not ready")):
        # Must not raise
        await _persist_thread_identity_async("agent-uuid-123", {"thread_id": "t-x"})


@pytest.mark.asyncio
async def test_persist_thread_identity_schedulable_as_task():
    """Caller pattern: asyncio.create_task(...) must be valid."""
    fake_db = AsyncMock()

    with patch("src.db.get_db", return_value=fake_db):
        task = asyncio.create_task(
            _persist_thread_identity_async("agent-uuid-123", {"thread_id": "t-x"})
        )
        await task  # Awaiting from test should not raise

    assert task.done()
    assert task.exception() is None


# ─── _persist_inferred_purpose_async ────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_inferred_purpose_calls_storage_with_correct_args():
    """Helper passes through to agent_storage.update_agent with purpose kwarg."""
    with patch(
        "src.mcp_handlers.updates.phases.agent_storage.update_agent",
        new_callable=AsyncMock,
    ) as mock_update:
        await _persist_inferred_purpose_async("agent-id-abc", "refactoring")

    mock_update.assert_awaited_once_with("agent-id-abc", purpose="refactoring")


@pytest.mark.asyncio
async def test_persist_inferred_purpose_swallows_errors():
    """Helper logs and returns on storage failure — does NOT raise."""
    with patch(
        "src.mcp_handlers.updates.phases.agent_storage.update_agent",
        new_callable=AsyncMock,
        side_effect=RuntimeError("storage offline"),
    ):
        # Must not raise
        await _persist_inferred_purpose_async("agent-id-abc", "refactoring")


@pytest.mark.asyncio
async def test_persist_inferred_purpose_schedulable_as_task():
    """Caller pattern: asyncio.create_task(...) must be valid."""
    with patch(
        "src.mcp_handlers.updates.phases.agent_storage.update_agent",
        new_callable=AsyncMock,
    ):
        task = asyncio.create_task(
            _persist_inferred_purpose_async("agent-id-abc", "refactoring")
        )
        await task

    assert task.done()
    assert task.exception() is None
