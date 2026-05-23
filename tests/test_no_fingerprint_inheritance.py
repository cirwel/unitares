"""
Tests for the spec docs/specs/2026-04-16-sever-fingerprint-eisv-inheritance-design.md

Covers:
- State transplant is gone (agent_lifecycle.get_or_create_monitor)
- Fingerprint match on resume=False no longer sets _predecessor_uuid
- Explicit parent_agent_id still records lineage (without state transplant)
- continuity_token round-trip preserves UUID
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from src.agent_metadata_model import AgentMetadata, agent_metadata
from src.agent_monitor_state import monitors


@pytest.fixture(autouse=True)
def _clear_process_state():
    """Each test starts with fresh in-memory identity state."""
    monitors.clear()
    agent_metadata.clear()
    yield
    monitors.clear()
    agent_metadata.clear()


def test_get_or_create_monitor_does_not_transplant_state_from_predecessor():
    """
    Regression guard: once agent_lifecycle.get_or_create_monitor no longer
    transplants state from a predecessor, a new agent with parent_agent_id
    set should start with a fresh GovernanceState (empty V_history).
    """
    from src.agent_lifecycle import get_or_create_monitor
    from src.governance_monitor import UNITARESMonitor

    # Build a predecessor monitor and populate its state so
    # load_monitor_state(parent_uuid) would return something real.
    # Fixed UUID4s (deterministic) — real UUIDs are required because
    # downstream code in agent_metadata_persistence.get_or_create_metadata
    # validates agent_id against a strict UUID4 pattern; using non-UUID
    # strings could cause the test to fail for the wrong reason or pass
    # vacuously after the Task 2 fix lands.
    parent_uuid = "11111111-1111-4111-8111-111111111111"
    parent_monitor = UNITARESMonitor(parent_uuid)
    parent_monitor.state.V_history.extend([0.1, 0.2, 0.3])
    monitors[parent_uuid] = parent_monitor

    # Child agent metadata points to the predecessor.
    # NOTE: get_or_create_monitor calls get_or_create_metadata(child_uuid)
    # BEFORE reading agent_metadata[child_uuid] to decide about transplant.
    # Verified (see src/agent_metadata_persistence.py:359) that
    # get_or_create_metadata is a no-op when the entry already exists —
    # it returns the existing AgentMetadata untouched — so the seed below
    # survives into the transplant branch.
    child_uuid = "22222222-2222-4222-8222-222222222222"
    now_iso = "2026-04-16T00:00:00+00:00"
    agent_metadata[child_uuid] = AgentMetadata(
        agent_id=child_uuid,
        status="active",
        created_at=now_iso,
        last_update=now_iso,
        parent_agent_id=parent_uuid,
    )

    # load_monitor_state(parent_uuid) in the real code path would return
    # the parent's persisted state. Force it to return the parent's in-memory
    # state so the "if we wanted to transplant, we could" path is exercised.
    def fake_load(agent_id):
        if agent_id == parent_uuid:
            return parent_monitor.state
        return None

    with patch("src.agent_lifecycle.load_monitor_state", side_effect=fake_load):
        child_monitor = get_or_create_monitor(child_uuid)

    assert child_monitor.state.V_history == [], (
        "Child agent must not inherit predecessor V_history "
        f"(got {child_monitor.state.V_history!r})"
    )


@pytest.mark.asyncio
async def test_path1_redis_hit_resume_false_does_not_set_predecessor():
    """
    PATH 1: Redis lookup finds a cached agent. resume=False now creates
    a new identity WITHOUT recording the cached agent as predecessor.
    """
    from unittest.mock import AsyncMock, MagicMock, patch
    from src.mcp_handlers.identity import resolution as resolution_mod

    existing_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    cache_hit = {
        "agent_id": existing_uuid,
        "display_agent_id": "OldAgent",
    }
    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(return_value=cache_hit)

    mock_raw_redis = AsyncMock()
    mock_raw_redis.expire = AsyncMock(return_value=True)

    mock_db = AsyncMock()
    mock_db.init = AsyncMock()
    mock_db.get_session = AsyncMock(return_value=None)
    mock_db.upsert_agent = AsyncMock()
    mock_db.upsert_identity = AsyncMock()
    mock_db.create_session = AsyncMock()
    mock_db.get_identity = AsyncMock(return_value=None)

    async def _get_raw():
        return mock_raw_redis

    with patch.object(resolution_mod, "_get_redis", return_value=mock_redis), \
         patch("src.cache.redis_client.get_redis", new=_get_raw), \
         patch.object(resolution_mod, "get_db", return_value=mock_db), \
         patch.object(resolution_mod, "_agent_exists_in_postgres", AsyncMock(return_value=True)), \
         patch.object(resolution_mod, "_get_agent_label", AsyncMock(return_value="OldAgent")), \
         patch.object(resolution_mod, "_get_agent_status", AsyncMock(return_value="active")), \
         patch.object(resolution_mod, "_soft_verify_trajectory", AsyncMock(return_value={"verified": True})), \
         patch.object(resolution_mod, "_cache_session", AsyncMock()):
        result = await resolution_mod.resolve_session_identity(
            session_key="fp-session-1",
            resume=False,
            persist=False,
        )

    # A brand-new identity should have been created.
    assert result["created"] is True
    assert result["agent_uuid"] != existing_uuid
    # And it MUST NOT carry predecessor_uuid forward.
    assert "predecessor_uuid" not in result, (
        f"resume=False + Redis fingerprint hit must not leak predecessor_uuid "
        f"(got {result.get('predecessor_uuid')!r})"
    )


@pytest.mark.asyncio
async def test_path2_postgres_hit_resume_false_does_not_set_predecessor():
    """
    PATH 2: Redis miss, PostgreSQL finds a session-bound agent.
    resume=False must not claim that agent as predecessor.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch
    from src.mcp_handlers.identity import resolution as resolution_mod

    existing_uuid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(return_value=None)  # PATH 1 miss

    mock_raw_redis = AsyncMock()
    mock_raw_redis.expire = AsyncMock(return_value=True)

    mock_db = AsyncMock()
    mock_db.init = AsyncMock()
    mock_db.get_session = AsyncMock(
        return_value=SimpleNamespace(agent_id=existing_uuid)
    )
    mock_db.upsert_agent = AsyncMock()
    mock_db.upsert_identity = AsyncMock()
    mock_db.create_session = AsyncMock()
    mock_db.get_identity = AsyncMock(return_value=None)

    async def _get_raw():
        return mock_raw_redis

    with patch.object(resolution_mod, "_get_redis", return_value=mock_redis), \
         patch("src.cache.redis_client.get_redis", new=_get_raw), \
         patch.object(resolution_mod, "get_db", return_value=mock_db), \
         patch.object(resolution_mod, "_agent_exists_in_postgres", AsyncMock(return_value=True)), \
         patch.object(resolution_mod, "_get_agent_label", AsyncMock(return_value="OldAgent")), \
         patch.object(resolution_mod, "_get_agent_status", AsyncMock(return_value="active")), \
         patch.object(resolution_mod, "_soft_verify_trajectory", AsyncMock(return_value={"verified": True})), \
         patch.object(resolution_mod, "_cache_session", AsyncMock()):
        result = await resolution_mod.resolve_session_identity(
            session_key="fp-session-2",
            resume=False,
            persist=False,
        )

    assert result["created"] is True
    assert result["agent_uuid"] != existing_uuid
    assert "predecessor_uuid" not in result, (
        f"resume=False + PostgreSQL session hit must not leak predecessor_uuid "
        f"(got {result.get('predecessor_uuid')!r})"
    )


def test_explicit_parent_agent_id_records_lineage_without_state_transplant():
    """
    When a caller explicitly asserts a predecessor via parent_agent_id on
    agent_metadata, the metadata row records lineage but the new agent's
    monitor starts with a fresh GovernanceState.
    """
    from datetime import datetime, timezone
    from src.agent_lifecycle import get_or_create_monitor
    from src.governance_monitor import UNITARESMonitor

    parent_uuid = "33333333-3333-4333-8333-333333333333"
    parent_monitor = UNITARESMonitor(parent_uuid)
    parent_monitor.state.V_history.extend([0.5, 0.6])
    monitors[parent_uuid] = parent_monitor

    child_uuid = "44444444-4444-4444-8444-444444444444"
    now_iso = datetime.now(timezone.utc).isoformat()
    # Explicit caller assertion: "I am forking from parent_uuid"
    agent_metadata[child_uuid] = AgentMetadata(
        agent_id=child_uuid,
        parent_agent_id=parent_uuid,
        status="active",
        created_at=now_iso,
        last_update=now_iso,
    )

    def fake_load(agent_id):
        if agent_id == parent_uuid:
            return parent_monitor.state
        return None

    with patch("src.agent_lifecycle.load_monitor_state", side_effect=fake_load):
        child_monitor = get_or_create_monitor(child_uuid)

    # Lineage is recorded in metadata.
    assert agent_metadata[child_uuid].parent_agent_id == parent_uuid
    # But state is NOT transplanted.
    assert child_monitor.state.V_history == []


@pytest.mark.asyncio
async def test_continuity_token_only_roundtrip_is_rejected_s1c():
    """
    S1-c regression guard for the old explicit-continuity path.

    Round-trip: onboard() -> capture uuid + continuity_token ->
    call identity() again with only that token. Token-only resume is no longer
    accepted; callers must use force_new + parent_agent_id or same-live-process
    PATH 0 with agent_uuid + continuity_token.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from tests.helpers import parse_result

    # --- Mock plumbing (inline to keep this file self-contained) -------------
    mock_db = AsyncMock()
    mock_db.init = AsyncMock()
    mock_db.get_session = AsyncMock(return_value=None)
    mock_db.get_identity = AsyncMock(return_value=None)
    mock_db.get_agent = AsyncMock(return_value=None)
    mock_db.get_agent_label = AsyncMock(return_value="RoundtripAgent")
    mock_db.get_agent_status = AsyncMock(return_value="active")
    mock_db.upsert_agent = AsyncMock()
    mock_db.upsert_identity = AsyncMock()
    mock_db.create_session = AsyncMock()
    mock_db.update_session_activity = AsyncMock()
    mock_db.find_agent_by_label = AsyncMock(return_value=None)
    mock_db.get_agent_thread_info = AsyncMock(return_value=None)
    mock_db.get_thread_nodes = AsyncMock(return_value=[])

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.bind = AsyncMock()

    mock_raw_redis = AsyncMock()
    mock_raw_redis.setex = AsyncMock()
    mock_raw_redis.expire = AsyncMock()
    mock_raw_redis.get = AsyncMock(return_value=None)

    async def _get_raw():
        return mock_raw_redis

    def _discard_task(coro, **kwargs):
        try:
            coro.close()
        except Exception:
            pass
        t = MagicMock()
        t.cancel = MagicMock()
        return t

    mock_server = MagicMock()
    mock_server.agent_metadata = {}

    # --- First call: onboard a fresh identity --------------------------------
    # get_identity is called twice: once by resolve_session_identity (expect None
    # so we create), once by ensure_agent_persisted check (None -> persist), and
    # then after upsert ensure_agent_persisted re-reads it.
    mock_db.get_identity.side_effect = [
        None,  # resolution PG lookup (PATH 2 miss)
        None,  # ensure_agent_persisted existence check
        SimpleNamespace(identity_id="i-new", metadata={}),  # post-upsert read
    ]

    from src.mcp_handlers.identity.handlers import (
        handle_onboard_v2,
        handle_identity_adapter,
    )

    env = {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-roundtrip-secret"}

    with patch.dict("os.environ", env, clear=False), \
         patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
         patch("src.cache.get_session_cache", return_value=mock_redis), \
         patch("src.mcp_handlers.identity.handlers.get_db", return_value=mock_db), \
         patch("src.mcp_handlers.identity.resolution.get_db", return_value=mock_db), \
         patch("src.mcp_handlers.identity.persistence.get_db", return_value=mock_db), \
         patch("src.cache.redis_client.get_redis", new=_get_raw), \
         patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
         patch("src.mcp_handlers.context.get_context_session_key", return_value="roundtrip-ctx"), \
         patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
         patch("src.mcp_handlers.context.get_context_client_hint", return_value="test"), \
         patch("src.mcp_handlers.context.update_context_agent_id"), \
         patch("asyncio.create_task", side_effect=_discard_task), \
         patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server), \
         patch("src.mcp_handlers.identity.shared._register_uuid_prefix"):

        first_result = await handle_onboard_v2({
            "client_session_id": "roundtrip-session-1",
            "resume": True,
        })
        first_data = parse_result(first_result)

        # Sanity: onboard succeeded and handed us a UUID + token.
        assert first_data.get("success") is True, first_data
        assert first_data.get("is_new") is True
        first_uuid = first_data.get("uuid")
        assert first_uuid, f"onboard response missing uuid: {first_data}"
        captured_token = first_data.get("continuity_token")
        assert captured_token, (
            "onboard response must include continuity_token when the secret "
            f"is configured (got keys: {sorted(first_data.keys())})"
        )

        second_result = await handle_identity_adapter({
            "continuity_token": captured_token,
            "resume": True,
        })
        second_data = parse_result(second_result)

    # --- The contract: token-only resume is rejected, not silently minted ----
    assert second_data.get("success") is False, second_data
    assert second_data.get("status") == "continuity_token_resume_rejected"
    assert second_data.get("tool") == "identity"
    assert second_data.get("recovery", {}).get("reason") == "continuity_token_resume_retired"
