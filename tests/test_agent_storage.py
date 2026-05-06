"""
Comprehensive tests for src/agent_storage.py

Tests every public async function with mocked asyncpg pool/connections.
The module under test is a PostgreSQL-backed agent storage layer that
wraps DatabaseBackend operations into a higher-level AgentRecord API.
"""

import hashlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.db.base import AgentStateRecord, IdentityRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_identity(
    agent_id: str = "agent-1",
    identity_id: int = 42,
    status: str = "active",
    metadata: dict | None = None,
    parent_agent_id: str | None = None,
    spawn_reason: str | None = None,
    api_key_hash: str = "abc123hash",
) -> IdentityRecord:
    """Build a minimal IdentityRecord for testing."""
    now = datetime.now(timezone.utc)
    return IdentityRecord(
        identity_id=identity_id,
        agent_id=agent_id,
        api_key_hash=api_key_hash,
        created_at=now,
        updated_at=now,
        status=status,
        parent_agent_id=parent_agent_id,
        spawn_reason=spawn_reason,
        metadata=metadata or {},
    )


_SENTINEL = object()

def _make_state(
    state_id: int = 1,
    identity_id: int = 42,
    agent_id: str = "agent-1",
    state_json: dict | None | object = _SENTINEL,
) -> AgentStateRecord:
    """Build a minimal AgentStateRecord for testing."""
    if state_json is _SENTINEL:
        state_json = {"health_status": "healthy", "E": 0.7}
    return AgentStateRecord(
        state_id=state_id,
        identity_id=identity_id,
        agent_id=agent_id,
        recorded_at=datetime.now(timezone.utc),
        entropy=0.15,
        integrity=0.8,
        stability_index=0.85,
        void=-0.01,
        regime="nominal",
        coherence=0.5,
        state_json=state_json if state_json is not None else {},
    )


def _mock_db(**overrides) -> MagicMock:
    """
    Return a MagicMock that quacks like DatabaseBackend.

    Every method the module calls is pre-wired as an AsyncMock.
    Pass keyword overrides to set specific return values.
    """
    db = MagicMock()
    db.init = AsyncMock()
    db.get_identity = AsyncMock(return_value=overrides.get("get_identity", None))
    db.get_latest_agent_state = AsyncMock(return_value=overrides.get("get_latest_agent_state", None))
    db.upsert_agent = AsyncMock()
    db.upsert_identity = AsyncMock()
    db.update_agent_fields = AsyncMock()
    db.update_identity_metadata = AsyncMock()
    db.update_identity_status = AsyncMock()
    db.list_identities = AsyncMock(return_value=overrides.get("list_identities", []))
    db.record_agent_state = AsyncMock(return_value=overrides.get("record_agent_state", 1))
    db.get_agent_state_history = AsyncMock(return_value=overrides.get("get_agent_state_history", []))

    # For list_agents label fetching -- db.acquire() is an async context manager
    if "acquire" in overrides:
        db.acquire = overrides["acquire"]
    else:
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        db.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=False),
        ))

    return db


@pytest.fixture(autouse=True)
def _clear_db_ready_cache():
    """Reset the module-level _db_ready_cache between tests."""
    import src.agent_storage as mod
    mod._db_ready_cache.clear()
    yield
    mod._db_ready_cache.clear()


# ---------------------------------------------------------------------------
# _ensure_db_ready
# ---------------------------------------------------------------------------

class TestEnsureDbReady:
    """Tests for the internal _ensure_db_ready helper."""

    @pytest.mark.asyncio
    async def test_calls_init_once(self):
        db = _mock_db()
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import _ensure_db_ready
            await _ensure_db_ready()
            await _ensure_db_ready()  # second call should skip init
            db.init.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_raises_on_init_failure(self):
        db = _mock_db()
        db.init = AsyncMock(side_effect=RuntimeError("pg down"))
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import _ensure_db_ready
            with pytest.raises(RuntimeError, match="pg down"):
                await _ensure_db_ready()

    @pytest.mark.asyncio
    async def test_no_init_method(self):
        """If the backend has no init(), skip silently."""
        db = _mock_db()
        del db.init  # remove the attribute
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import _ensure_db_ready
            await _ensure_db_ready()  # should not raise


# ---------------------------------------------------------------------------
# get_agent
# ---------------------------------------------------------------------------

class TestGetAgent:

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        db = _mock_db(get_identity=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import get_agent
            result = await get_agent("nonexistent")
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_agent_record_with_health(self):
        identity = _make_identity(
            metadata={"tags": ["cli"], "notes": "test", "purpose": "testing"},
            parent_agent_id="parent-1",
            spawn_reason="test spawn",
        )
        state = _make_state(state_json={"health_status": "healthy", "E": 0.7})
        db = _mock_db(get_identity=identity, get_latest_agent_state=state)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import get_agent
            rec = await get_agent("agent-1")

        assert rec is not None
        assert rec.agent_id == "agent-1"
        assert rec.api_key == ""  # never returns plaintext
        assert rec.status == "active"
        assert rec.tags == ["cli"]
        assert rec.notes == "test"
        assert rec.purpose == "testing"
        assert rec.parent_agent_id == "parent-1"
        assert rec.spawn_reason == "test spawn"
        assert rec.health_status == "healthy"
        assert rec.identity_id == 42

    @pytest.mark.asyncio
    async def test_health_defaults_unknown_when_no_state(self):
        identity = _make_identity()
        db = _mock_db(get_identity=identity, get_latest_agent_state=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import get_agent
            rec = await get_agent("agent-1")
            assert rec.health_status == "unknown"

    @pytest.mark.asyncio
    async def test_health_defaults_unknown_when_state_json_empty(self):
        identity = _make_identity()
        state = _make_state(state_json={})
        db = _mock_db(get_identity=identity, get_latest_agent_state=state)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import get_agent
            rec = await get_agent("agent-1")
            assert rec.health_status == "unknown"

    @pytest.mark.asyncio
    async def test_health_fallback_on_state_exception(self):
        """If get_latest_agent_state raises (schema mismatch), use metadata."""
        identity = _make_identity(metadata={"health_status": "degraded"})
        db = _mock_db(get_identity=identity)
        db.get_latest_agent_state = AsyncMock(side_effect=Exception("column missing"))
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import get_agent
            rec = await get_agent("agent-1")
            assert rec.health_status == "degraded"

    @pytest.mark.asyncio
    async def test_no_state_lookup_when_no_identity_id(self):
        """If identity_id is falsy (0 or None), skip state lookup."""
        identity = _make_identity(identity_id=0)
        db = _mock_db(get_identity=identity)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import get_agent
            rec = await get_agent("agent-1")
            db.get_latest_agent_state.assert_not_awaited()
            assert rec.health_status == "unknown"


# ---------------------------------------------------------------------------
# agent_exists
# ---------------------------------------------------------------------------

class TestAgentExists:

    @pytest.mark.asyncio
    async def test_returns_true_when_found(self):
        db = _mock_db(get_identity=_make_identity())
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import agent_exists
            assert await agent_exists("agent-1") is True

    @pytest.mark.asyncio
    async def test_returns_false_when_not_found(self):
        db = _mock_db(get_identity=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import agent_exists
            assert await agent_exists("nope") is False


# ---------------------------------------------------------------------------
# create_agent
# ---------------------------------------------------------------------------

class TestCreateAgent:

    @pytest.mark.asyncio
    async def test_creates_new_agent(self):
        db = _mock_db(get_identity=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import create_agent
            rec = await create_agent(
                "new-agent",
                "my-api-key",
                status="active",
                tags=["test"],
                notes="hello",
                purpose="unit-test",
                parent_agent_id="parent-1",
                spawn_reason="testing",
            )

        assert rec.agent_id == "new-agent"
        assert rec.api_key == "my-api-key"  # plaintext returned for new agents
        assert rec.api_key_hash == hashlib.sha256(b"my-api-key").hexdigest()
        assert rec.status == "active"
        assert rec.tags == ["test"]
        assert rec.notes == "hello"
        assert rec.purpose == "unit-test"
        assert rec.parent_agent_id == "parent-1"
        assert rec.spawn_reason == "testing"
        assert rec.health_status == "unknown"
        assert rec.created_at is not None

        db.upsert_agent.assert_awaited_once()
        db.upsert_identity.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_raises_if_agent_already_exists(self):
        db = _mock_db(get_identity=_make_identity())
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import create_agent
            with pytest.raises(ValueError, match="already exists"):
                await create_agent("agent-1", "key")

    @pytest.mark.asyncio
    async def test_empty_api_key_hashes_to_empty(self):
        db = _mock_db(get_identity=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import create_agent
            rec = await create_agent("agent-empty-key", "")
            assert rec.api_key_hash == ""

    @pytest.mark.asyncio
    async def test_default_tags_is_empty_list(self):
        db = _mock_db(get_identity=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import create_agent
            rec = await create_agent("agent-no-tags", "key")
            assert rec.tags == []

    @pytest.mark.asyncio
    async def test_custom_created_at(self):
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        db = _mock_db(get_identity=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import create_agent
            rec = await create_agent("agent-ts", "key", created_at=ts)
            assert rec.created_at == ts
            assert rec.updated_at == ts

    @pytest.mark.asyncio
    async def test_upsert_identity_metadata_has_source(self):
        db = _mock_db(get_identity=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import create_agent
            await create_agent("agent-src", "key", tags=["a"])

        call_kwargs = db.upsert_identity.call_args
        metadata = call_kwargs.kwargs.get("metadata") or call_kwargs[1].get("metadata")
        assert metadata["source"] == "agent_storage.create_agent"
        assert metadata["total_updates"] == 0
        assert metadata["tags"] == ["a"]

    @pytest.mark.asyncio
    async def test_skips_upsert_agent_when_method_missing(self):
        """If db doesn't have upsert_agent, skip it (no error)."""
        db = _mock_db(get_identity=None)
        del db.upsert_agent  # remove attribute so hasattr returns False
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import create_agent
            rec = await create_agent("agent-no-upsert", "key")
            assert rec.agent_id == "agent-no-upsert"
            # upsert_identity should still be called
            db.upsert_identity.assert_awaited_once()


# ---------------------------------------------------------------------------
# get_or_create_agent
# ---------------------------------------------------------------------------

class TestGetOrCreateAgent:

    @pytest.mark.asyncio
    async def test_returns_existing_agent(self):
        identity = _make_identity(metadata={"tags": ["existing"]})
        db = _mock_db(get_identity=identity, get_latest_agent_state=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import get_or_create_agent
            rec, is_new = await get_or_create_agent("agent-1", "new-key")

        assert is_new is False
        assert rec.agent_id == "agent-1"
        assert rec.api_key == "new-key"  # caller's key is set on existing
        db.upsert_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_creates_when_not_found(self):
        db = _mock_db(get_identity=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import get_or_create_agent
            rec, is_new = await get_or_create_agent("new-agent", "key", tags=["auto"])

        assert is_new is True
        assert rec.agent_id == "new-agent"
        db.upsert_identity.assert_awaited_once()


# ---------------------------------------------------------------------------
# update_agent
# ---------------------------------------------------------------------------

class TestUpdateAgent:

    @pytest.mark.asyncio
    async def test_updates_metadata_and_status(self):
        db = _mock_db()
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import update_agent
            result = await update_agent(
                "agent-1",
                status="paused",
                tags=["updated"],
                notes="new notes",
                purpose="new purpose",
            )

        assert result is True
        db.update_agent_fields.assert_awaited_once()
        db.update_identity_metadata.assert_awaited_once()
        db.update_identity_status.assert_awaited_once()

        # Check metadata merge args
        meta_call = db.update_identity_metadata.call_args
        metadata_arg = meta_call[0][1] if len(meta_call[0]) > 1 else meta_call.kwargs.get("metadata")
        assert metadata_arg is not None

    @pytest.mark.asyncio
    async def test_status_archived_sets_disabled_at(self):
        db = _mock_db()
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import update_agent
            await update_agent("agent-1", status="archived")

        status_call = db.update_identity_status.call_args
        assert status_call[1].get("status") or status_call[0][1] == "archived"
        # disabled_at should be set
        disabled_at = status_call[1].get("disabled_at") or (status_call[0][2] if len(status_call[0]) > 2 else None)
        assert disabled_at is not None

    @pytest.mark.asyncio
    async def test_status_deleted_sets_disabled_at(self):
        db = _mock_db()
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import update_agent
            await update_agent("agent-1", status="deleted")

        status_call = db.update_identity_status.call_args
        disabled_at = status_call[1].get("disabled_at") or (status_call[0][2] if len(status_call[0]) > 2 else None)
        assert disabled_at is not None

    @pytest.mark.asyncio
    async def test_active_status_no_disabled_at(self):
        db = _mock_db()
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import update_agent
            await update_agent("agent-1", status="active")

        status_call = db.update_identity_status.call_args
        # For active status, disabled_at should be None
        disabled_at = status_call.kwargs.get("disabled_at")
        if disabled_at is None and len(status_call.args) > 2:
            disabled_at = status_call.args[2]
        assert disabled_at is None

    @pytest.mark.asyncio
    async def test_no_status_update_skips_update_identity_status(self):
        db = _mock_db()
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import update_agent
            await update_agent("agent-1", tags=["only-tags"])

        db.update_identity_status.assert_not_awaited()
        db.update_identity_metadata.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_update_agent_fields_when_missing(self):
        db = _mock_db()
        del db.update_agent_fields
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import update_agent
            result = await update_agent("agent-1", notes="ok")
            assert result is True

    @pytest.mark.asyncio
    async def test_metadata_updates_include_updated_at(self):
        db = _mock_db()
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import update_agent
            await update_agent("agent-1", notes="timestamped")

        meta_call = db.update_identity_metadata.call_args
        metadata_arg = meta_call[0][1] if len(meta_call[0]) > 1 else meta_call.kwargs.get("metadata")
        assert "updated_at" in metadata_arg

    @pytest.mark.asyncio
    async def test_parent_and_spawn_reason(self):
        db = _mock_db()
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import update_agent
            await update_agent("agent-1", parent_agent_id="parent-2", spawn_reason="forked")

        fields_call = db.update_agent_fields.call_args
        assert fields_call.kwargs.get("parent_agent_id") == "parent-2"
        assert fields_call.kwargs.get("spawn_reason") == "forked"


# ---------------------------------------------------------------------------
# archive_agent
# ---------------------------------------------------------------------------

class TestArchiveAgent:

    @pytest.mark.asyncio
    async def test_sets_archived_status(self):
        db = _mock_db()
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import archive_agent
            result = await archive_agent("agent-1")

        assert result is True
        db.update_identity_status.assert_awaited_once()
        call_kwargs = db.update_identity_status.call_args.kwargs
        assert call_kwargs.get("status") == "archived"
        assert call_kwargs.get("disabled_at") is not None

    @pytest.mark.asyncio
    async def test_also_updates_agent_fields(self):
        db = _mock_db()
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import archive_agent
            await archive_agent("agent-1")

        db.update_agent_fields.assert_awaited_once()
        fields_call = db.update_agent_fields.call_args
        assert fields_call.kwargs.get("status") == "archived"

    @pytest.mark.asyncio
    async def test_skips_update_agent_fields_when_missing(self):
        db = _mock_db()
        del db.update_agent_fields
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import archive_agent
            result = await archive_agent("agent-1")
            assert result is True
            db.update_identity_status.assert_awaited_once()


# ---------------------------------------------------------------------------
# delete_agent
# ---------------------------------------------------------------------------

class TestDeleteAgent:

    @pytest.mark.asyncio
    async def test_sets_deleted_status(self):
        db = _mock_db()
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import delete_agent
            result = await delete_agent("agent-1")

        assert result is True
        db.update_identity_status.assert_awaited_once()
        call_kwargs = db.update_identity_status.call_args.kwargs
        assert call_kwargs.get("status") == "deleted"
        assert call_kwargs.get("disabled_at") is not None

    @pytest.mark.asyncio
    async def test_also_updates_agent_fields(self):
        db = _mock_db()
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import delete_agent
            await delete_agent("agent-1")

        db.update_agent_fields.assert_awaited_once()
        fields_call = db.update_agent_fields.call_args
        assert fields_call.kwargs.get("status") == "deleted"

    @pytest.mark.asyncio
    async def test_skips_update_agent_fields_when_missing(self):
        db = _mock_db()
        del db.update_agent_fields
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import delete_agent
            result = await delete_agent("agent-1")
            assert result is True


# ---------------------------------------------------------------------------
# list_agents
# ---------------------------------------------------------------------------

class TestListAgents:

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_agents(self):
        db = _mock_db(list_identities=[])
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import list_agents
            result = await list_agents()
            assert result == []

    @pytest.mark.asyncio
    async def test_returns_active_agents(self):
        identities = [
            _make_identity("a1", identity_id=1, metadata={"tags": ["t1"]}),
            _make_identity("a2", identity_id=2, metadata={"tags": ["t2"]}),
        ]
        db = _mock_db(list_identities=identities, get_latest_agent_state=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import list_agents
            result = await list_agents()

        assert len(result) == 2
        assert result[0].agent_id == "a1"
        assert result[1].agent_id == "a2"

    @pytest.mark.asyncio
    async def test_filters_archived_by_default(self):
        identities = [
            _make_identity("active-1", identity_id=1),
            _make_identity("archived-1", identity_id=2, status="archived"),
        ]
        db = _mock_db(list_identities=identities, get_latest_agent_state=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import list_agents
            result = await list_agents()

        assert len(result) == 1
        assert result[0].agent_id == "active-1"

    @pytest.mark.asyncio
    async def test_includes_archived_when_requested(self):
        identities = [
            _make_identity("active-1", identity_id=1),
            _make_identity("archived-1", identity_id=2, status="archived"),
        ]
        db = _mock_db(list_identities=identities, get_latest_agent_state=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import list_agents
            result = await list_agents(include_archived=True)

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_filters_deleted_by_default(self):
        identities = [
            _make_identity("active-1", identity_id=1),
            _make_identity("deleted-1", identity_id=2, status="deleted"),
        ]
        db = _mock_db(list_identities=identities, get_latest_agent_state=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import list_agents
            result = await list_agents()

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_includes_deleted_when_requested(self):
        identities = [
            _make_identity("active-1", identity_id=1),
            _make_identity("deleted-1", identity_id=2, status="deleted"),
        ]
        db = _mock_db(list_identities=identities, get_latest_agent_state=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import list_agents
            result = await list_agents(include_deleted=True)

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_passes_status_limit_offset(self):
        db = _mock_db(list_identities=[])
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import list_agents
            await list_agents(status="paused", limit=50, offset=10)

        db.list_identities.assert_awaited_once_with(status="paused", limit=50, offset=10)

    @pytest.mark.asyncio
    async def test_health_from_state(self):
        identity = _make_identity("a1", identity_id=1)
        state = _make_state(state_json={"health_status": "degraded"})
        db = _mock_db(list_identities=[identity], get_latest_agent_state=state)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import list_agents
            result = await list_agents()

        assert result[0].health_status == "degraded"

    @pytest.mark.asyncio
    async def test_health_fallback_on_exception(self):
        identity = _make_identity("a1", identity_id=1, metadata={"health_status": "warning"})
        db = _mock_db(list_identities=[identity])
        db.get_latest_agent_state = AsyncMock(side_effect=Exception("bad column"))
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import list_agents
            result = await list_agents()

        assert result[0].health_status == "warning"

    @pytest.mark.asyncio
    async def test_label_merged_from_pool_query(self):
        identity = _make_identity("a1", identity_id=1, metadata={"tags": []})

        # Build a mock acquire() that returns a connection with label rows
        mock_row = {"id": "a1", "label": "My Agent"}
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[mock_row])
        mock_acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=False),
        ))

        db = _mock_db(list_identities=[identity], get_latest_agent_state=None, acquire=mock_acquire)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import list_agents
            result = await list_agents()

        assert result[0].metadata.get("label") == "My Agent"

    @pytest.mark.asyncio
    async def test_label_fetch_failure_is_silent(self):
        """If the acquire() call for labels fails, agents still return."""
        identity = _make_identity("a1", identity_id=1)

        mock_acquire = MagicMock(side_effect=Exception("pool gone"))

        db = _mock_db(list_identities=[identity], get_latest_agent_state=None, acquire=mock_acquire)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import list_agents
            result = await list_agents()

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_acquire_returns_empty_labels_gracefully(self):
        """If acquire() returns no label rows, agents still load without labels."""
        identity = _make_identity("a1", identity_id=1)
        # Default mock_db already returns empty rows from acquire()
        db = _mock_db(list_identities=[identity], get_latest_agent_state=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import list_agents
            result = await list_agents()

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_no_state_lookup_for_zero_identity_id(self):
        """identity_id=0 should skip state lookup."""
        identity = _make_identity("a1", identity_id=0)
        db = _mock_db(list_identities=[identity], get_latest_agent_state=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import list_agents
            result = await list_agents()

        assert result[0].health_status == "unknown"
        db.get_latest_agent_state.assert_not_awaited()


# ---------------------------------------------------------------------------
# record_agent_state
# ---------------------------------------------------------------------------

class TestRecordAgentState:

    @pytest.mark.asyncio
    async def test_records_state_successfully(self):
        identity = _make_identity(identity_id=42)
        db = _mock_db(get_identity=identity, record_agent_state=99)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import record_agent_state
            state_id = await record_agent_state(
                "agent-1",
                E=0.7, I=0.8, S=0.15, V=-0.01,
                regime="EXPLORATION",
                coherence=0.5,
                health_status="healthy",
            )

        assert state_id == 99
        db.record_agent_state.assert_awaited_once()
        call_kwargs = db.record_agent_state.call_args.kwargs
        assert call_kwargs["identity_id"] == 42
        assert call_kwargs["entropy"] == 0.15
        assert call_kwargs["integrity"] == 0.8
        assert call_kwargs["stability_index"] == 0.0  # Dead field, no longer computed
        assert call_kwargs["void"] == -0.01
        assert call_kwargs["regime"] == "EXPLORATION"
        assert call_kwargs["coherence"] == 0.5
        assert call_kwargs["state_json"]["health_status"] == "healthy"
        assert call_kwargs["state_json"]["E"] == 0.7

    @pytest.mark.asyncio
    async def test_raises_when_agent_not_found(self):
        db = _mock_db(get_identity=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import record_agent_state
            with pytest.raises(ValueError, match="not found"):
                await record_agent_state(
                    "nonexistent",
                    E=0.5, I=0.5, S=0.5, V=0.0,
                    regime="nominal", coherence=1.0,
                )

    @pytest.mark.asyncio
    async def test_unknown_regime_maps_to_nominal(self):
        identity = _make_identity()
        db = _mock_db(get_identity=identity, record_agent_state=1)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import record_agent_state
            await record_agent_state(
                "agent-1",
                E=0.5, I=0.5, S=0.5, V=0.0,
                regime="TOTALLY_UNKNOWN",
                coherence=1.0,
            )

        call_kwargs = db.record_agent_state.call_args.kwargs
        assert call_kwargs["regime"] == "nominal"

    @pytest.mark.asyncio
    async def test_allowed_regimes_pass_through(self):
        """All allowed regime values should pass through unchanged."""
        allowed = [
            "nominal", "warning", "critical", "recovery",
            "EXPLORATION", "CONVERGENCE", "DIVERGENCE", "STABLE",
        ]
        identity = _make_identity()
        for regime in allowed:
            db = _mock_db(get_identity=identity, record_agent_state=1)
            with patch("src.agent_storage.get_db", return_value=db):
                from src.agent_storage import record_agent_state
                await record_agent_state(
                    "agent-1",
                    E=0.5, I=0.5, S=0.5, V=0.0,
                    regime=regime, coherence=1.0,
                )
            call_kwargs = db.record_agent_state.call_args.kwargs
            assert call_kwargs["regime"] == regime, f"Regime {regime} should pass through"

    @pytest.mark.asyncio
    async def test_optional_fields_in_state_json(self):
        identity = _make_identity()
        db = _mock_db(get_identity=identity, record_agent_state=1)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import record_agent_state
            await record_agent_state(
                "agent-1",
                E=0.5, I=0.5, S=0.5, V=0.0,
                regime="nominal", coherence=1.0,
                risk_score=0.3, phi=0.42, verdict="safe",
            )

        state_json = db.record_agent_state.call_args.kwargs["state_json"]
        assert state_json["risk_score"] == 0.3
        assert state_json["phi"] == 0.42
        assert state_json["verdict"] == "safe"

    @pytest.mark.asyncio
    async def test_provenance_context_in_state_json(self):
        identity = _make_identity()
        db = _mock_db(get_identity=identity, record_agent_state=1)
        context = {
            "schema": "s22.write_context.v1",
            "harness_type": "codex-cli",
            "comparison_key": "h5-bounded-task",
        }
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import record_agent_state
            await record_agent_state(
                "agent-1",
                E=0.5, I=0.5, S=0.5, V=0.0,
                regime="nominal", coherence=1.0,
                provenance_context=context,
            )

        state_json = db.record_agent_state.call_args.kwargs["state_json"]
        assert state_json["provenance_context"] == context

    @pytest.mark.asyncio
    async def test_optional_fields_omitted_when_none(self):
        identity = _make_identity()
        db = _mock_db(get_identity=identity, record_agent_state=1)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import record_agent_state
            await record_agent_state(
                "agent-1",
                E=0.5, I=0.5, S=0.5, V=0.0,
                regime="nominal", coherence=1.0,
            )

        state_json = db.record_agent_state.call_args.kwargs["state_json"]
        assert "risk_score" not in state_json
        assert "phi" not in state_json
        assert "verdict" not in state_json

    @pytest.mark.asyncio
    async def test_stability_index_always_zero(self):
        """stability_index is a dead field — always 0.0 regardless of S."""
        identity = _make_identity()
        db = _mock_db(get_identity=identity, record_agent_state=1)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import record_agent_state
            await record_agent_state(
                "agent-1",
                E=0.5, I=0.5, S=0.0, V=0.0,
                regime="nominal", coherence=1.0,
            )

        call_kwargs = db.record_agent_state.call_args.kwargs
        assert call_kwargs["stability_index"] == 0.0


# ---------------------------------------------------------------------------
# get_agent_state_history
# ---------------------------------------------------------------------------

class TestGetAgentStateHistory:

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_agent(self):
        db = _mock_db(get_identity=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import get_agent_state_history
            result = await get_agent_state_history("nonexistent")
            assert result == []

    @pytest.mark.asyncio
    async def test_returns_history(self):
        identity = _make_identity(identity_id=42)
        states = [_make_state(state_id=i) for i in range(3)]
        db = _mock_db(get_identity=identity, get_agent_state_history=states)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import get_agent_state_history
            result = await get_agent_state_history("agent-1", limit=50)

        assert len(result) == 3
        db.get_agent_state_history.assert_awaited_once_with(42, limit=50)

    @pytest.mark.asyncio
    async def test_default_limit_is_100(self):
        identity = _make_identity(identity_id=42)
        db = _mock_db(get_identity=identity, get_agent_state_history=[])
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import get_agent_state_history
            await get_agent_state_history("agent-1")

        db.get_agent_state_history.assert_awaited_once_with(42, limit=100)


# ---------------------------------------------------------------------------
# get_latest_agent_state
# ---------------------------------------------------------------------------

class TestGetLatestAgentState:

    @pytest.mark.asyncio
    async def test_returns_none_when_no_agent(self):
        db = _mock_db(get_identity=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import get_latest_agent_state
            result = await get_latest_agent_state("nonexistent")
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_latest_state(self):
        identity = _make_identity(identity_id=42)
        state = _make_state(state_id=999)
        db = _mock_db(get_identity=identity, get_latest_agent_state=state)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import get_latest_agent_state
            result = await get_latest_agent_state("agent-1")

        assert result is not None
        assert result.state_id == 999
        db.get_latest_agent_state.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_returns_none_when_no_state(self):
        identity = _make_identity(identity_id=42)
        db = _mock_db(get_identity=identity, get_latest_agent_state=None)
        with patch("src.agent_storage.get_db", return_value=db):
            from src.agent_storage import get_latest_agent_state
            result = await get_latest_agent_state("agent-1")
            assert result is None


# ---------------------------------------------------------------------------
# AgentRecord dataclass
# ---------------------------------------------------------------------------

class TestAgentRecord:

    def test_defaults(self):
        from src.agent_storage import AgentRecord
        rec = AgentRecord(agent_id="x", api_key="k", api_key_hash="h")
        assert rec.status == "active"
        assert rec.tags == []
        assert rec.notes is None
        assert rec.purpose is None
        assert rec.parent_agent_id is None
        assert rec.spawn_reason is None
        assert rec.health_status == "unknown"
        assert rec.identity_id is None
        assert rec.metadata == {}
        assert rec.created_at is None
        assert rec.updated_at is None

    def test_full_construction(self):
        from src.agent_storage import AgentRecord
        now = datetime.now(timezone.utc)
        rec = AgentRecord(
            agent_id="a",
            api_key="k",
            api_key_hash="h",
            status="paused",
            created_at=now,
            updated_at=now,
            tags=["x"],
            notes="n",
            purpose="p",
            parent_agent_id="parent",
            spawn_reason="fork",
            health_status="healthy",
            identity_id=7,
            metadata={"extra": True},
        )
        assert rec.agent_id == "a"
        assert rec.status == "paused"
        assert rec.identity_id == 7
        assert rec.metadata == {"extra": True}
