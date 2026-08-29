"""
Comprehensive tests for src/dialectic_db.py - PostgreSQL backend for dialectic sessions.

Tests the DialecticDB class methods, singleton get_dialectic_db(), and convenience
async wrappers. All asyncpg pool/connection interactions are mocked.
"""

import json
import asyncio
import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock

# Ensure project root is on sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import src.dialectic_db as dialectic_db_module
from src.dialectic_db import (
    DialecticDB,
    get_dialectic_db,
    create_session_async,
    get_session_async,
    get_session_by_agent_async,
    get_all_sessions_by_agent_async,
    is_agent_in_active_session_async,
    has_recently_reviewed_async,
    add_message_async,
    update_session_phase_async,
    update_session_reviewer_async,
    update_session_status_async,
    resolve_session_async,
    get_active_sessions_async,
    get_sessions_awaiting_reviewer_async,
)
from src.dialectic_protocol import DialecticPhase


# ============================================================================
# Fixtures
# ============================================================================

def _make_mock_pool():
    """Create a mock asyncpg pool with acquire() returning an async context manager."""
    pool = MagicMock()
    pool._closed = False  # Mimic asyncpg Pool — DialecticDB._pool_is_alive checks this
    conn = AsyncMock()

    # Make pool.acquire() return an async context manager that yields conn
    acm = AsyncMock()
    acm.__aenter__ = AsyncMock(return_value=conn)
    acm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = acm

    return pool, conn


@pytest.fixture
def mock_pool():
    """Fixture providing a mock pool and connection pair."""
    return _make_mock_pool()


@pytest.fixture
def db(mock_pool):
    """Fixture providing a DialecticDB with mock pool already set."""
    pool, conn = mock_pool
    instance = DialecticDB(pool=pool)
    instance._initialized = True
    return instance, pool, conn


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the module-level singleton before each test."""
    dialectic_db_module._db_instance = None
    dialectic_db_module._db_lock = None
    yield
    dialectic_db_module._db_instance = None
    dialectic_db_module._db_lock = None


# ============================================================================
# DialecticDB.__init__
# ============================================================================

class TestInit:
    def test_init_no_pool(self):
        """DialecticDB can be created without a pool."""
        db = DialecticDB()
        assert db._pool is None
        assert db._initialized is False

    def test_init_with_pool(self):
        """DialecticDB stores pool passed at construction."""
        pool = MagicMock()
        db = DialecticDB(pool=pool)
        assert db._pool is pool
        assert db._initialized is False


# ============================================================================
# DialecticDB.init
# ============================================================================

class TestInitMethod:
    @pytest.mark.asyncio
    async def test_init_with_provided_pool(self):
        """init() sets pool from argument and marks initialized."""
        pool = MagicMock()
        db = DialecticDB()
        await db.init(pool=pool)
        assert db._pool is pool
        assert db._initialized is True

    @pytest.mark.asyncio
    async def test_init_uses_existing_pool(self):
        """init() with no argument keeps existing pool."""
        pool = MagicMock()
        db = DialecticDB(pool=pool)
        await db.init()
        assert db._pool is pool
        assert db._initialized is True

    @pytest.mark.asyncio
    async def test_init_fallback_to_get_db(self):
        """init() without pool falls back to get_db()."""
        mock_db_instance = AsyncMock()
        mock_db_instance._pool = MagicMock()

        with patch("src.db.get_db", return_value=mock_db_instance) as mock_get_db:
            db = DialecticDB()
            await db.init()

            mock_get_db.assert_called_once()
            mock_db_instance.init.assert_awaited_once()
            assert db._pool is mock_db_instance._pool
            assert db._initialized is True


# ============================================================================
# DialecticDB._ensure_pool
# ============================================================================

class TestEnsurePool:
    @pytest.mark.asyncio
    async def test_ensure_pool_with_existing_pool(self, db):
        """_ensure_pool does nothing when pool already set."""
        instance, pool, conn = db
        await instance._ensure_pool()
        # Should not raise, pool remains unchanged
        assert instance._pool is pool

    @pytest.mark.asyncio
    async def test_ensure_pool_reinitializes_when_none(self):
        """_ensure_pool calls init() when pool is None."""
        db = DialecticDB()
        mock_db_backend = AsyncMock()
        mock_db_backend._pool = MagicMock()

        with patch("src.db.get_db", return_value=mock_db_backend):
            await db._ensure_pool()
            assert db._pool is mock_db_backend._pool

    @pytest.mark.asyncio
    async def test_ensure_pool_raises_if_still_none(self):
        """_ensure_pool raises RuntimeError if pool remains None after init."""
        db = DialecticDB()
        mock_db_backend = AsyncMock()
        mock_db_backend._pool = None  # init will set _pool to None

        with patch("src.db.get_db", return_value=mock_db_backend):
            with pytest.raises(RuntimeError, match="Failed to initialize"):
                await db._ensure_pool()

    @pytest.mark.asyncio
    async def test_ensure_pool_refreshes_closed_pool(self):
        """_ensure_pool detects a closed pool and refreshes from backend."""
        stale_pool = MagicMock()
        stale_pool._closed = True  # Simulate pool that was closed by PostgresBackend

        db = DialecticDB(pool=stale_pool)

        fresh_pool = MagicMock()
        fresh_pool._closed = False
        mock_db_backend = AsyncMock()
        mock_db_backend._pool = fresh_pool

        with patch("src.db.get_db", return_value=mock_db_backend):
            await db._ensure_pool()
            assert db._pool is fresh_pool
            assert db._pool is not stale_pool


# ============================================================================
# DialecticDB.create_session
# ============================================================================

class TestCreateSession:
    @pytest.mark.asyncio
    async def test_create_session_success(self, db):
        """create_session inserts and returns created=True."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="INSERT 0 1")

        result = await instance.create_session(
            session_id="sess-001",
            paused_agent_id="agent-A",
            reviewer_agent_id="agent-B",
            reason="test reason",
            discovery_id="disc-1",
            dispute_type="confidence",
            session_type="recovery",
            topic="test topic",
            max_synthesis_rounds=3,
            synthesis_round=1,
            paused_agent_state={"E": 0.7, "I": 0.8},
        )

        assert result == {"session_id": "sess-001", "created": True}
        conn.execute.assert_awaited_once()
        # Verify the SQL args include json.dumps of paused_agent_state
        call_args = conn.execute.call_args
        assert call_args[0][1] == "sess-001"
        assert call_args[0][2] == "agent-A"
        assert call_args[0][3] == "agent-B"
        assert call_args[0][4] == DialecticPhase.THESIS.value
        assert call_args[0][5] == "active"
        assert call_args[0][13] == json.dumps({"E": 0.7, "I": 0.8})

    @pytest.mark.asyncio
    async def test_create_session_minimal_args(self, db):
        """create_session works with only required args."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="INSERT 0 1")

        result = await instance.create_session(
            session_id="sess-002",
            paused_agent_id="agent-X",
        )

        assert result == {"session_id": "sess-002", "created": True}
        call_args = conn.execute.call_args
        # Optional args should be None
        assert call_args[0][3] is None  # reviewer_agent_id
        assert call_args[0][7] is None  # reason
        assert call_args[0][12] == 0     # synthesis_round defaults to 0
        assert call_args[0][13] is None  # paused_agent_state_json (None state)

    @pytest.mark.asyncio
    async def test_create_session_duplicate_key(self, db):
        """create_session returns created=False on duplicate key."""
        instance, pool, conn = db
        conn.execute = AsyncMock(
            side_effect=Exception("ERROR: duplicate key value violates unique constraint")
        )

        result = await instance.create_session(
            session_id="sess-dup",
            paused_agent_id="agent-A",
        )

        assert result["session_id"] == "sess-dup"
        assert result["created"] is False
        assert result["error"] == "already_exists"

    @pytest.mark.asyncio
    async def test_create_session_unique_violation(self, db):
        """create_session handles 'unique' in exception message."""
        instance, pool, conn = db
        conn.execute = AsyncMock(
            side_effect=Exception("unique constraint violation on session_id")
        )

        result = await instance.create_session(
            session_id="sess-uniq",
            paused_agent_id="agent-A",
        )

        assert result["created"] is False
        assert result["error"] == "already_exists"

    @pytest.mark.asyncio
    async def test_create_session_unexpected_error(self, db):
        """create_session re-raises non-duplicate exceptions."""
        instance, pool, conn = db
        conn.execute = AsyncMock(
            side_effect=Exception("connection refused")
        )

        with pytest.raises(Exception, match="connection refused"):
            await instance.create_session(
                session_id="sess-err",
                paused_agent_id="agent-A",
            )


# ============================================================================
# DialecticDB.get_session
# ============================================================================

class TestGetSession:
    @pytest.mark.asyncio
    async def test_get_session_not_found(self, db):
        """get_session returns None for missing session."""
        instance, pool, conn = db
        conn.fetchrow = AsyncMock(return_value=None)

        result = await instance.get_session("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_session_found(self, db):
        """get_session returns session dict with messages."""
        instance, pool, conn = db

        # Mock row as dict-like (asyncpg Record)
        session_row = MagicMock()
        session_row.__iter__ = MagicMock(return_value=iter([
            ("session_id", "sess-001"),
            ("paused_agent_id", "agent-A"),
            ("status", "active"),
            ("phase", "thesis"),
        ]))
        session_row.keys = MagicMock(return_value=["session_id", "paused_agent_id", "status", "phase"])
        session_row.__getitem__ = lambda self, k: {
            "session_id": "sess-001",
            "paused_agent_id": "agent-A",
            "status": "active",
            "phase": "thesis",
        }[k]
        # dict(row) needs items
        dict_result = {
            "session_id": "sess-001",
            "paused_agent_id": "agent-A",
            "status": "active",
            "phase": "thesis",
        }

        # Use a real dict to avoid complexity with MagicMock dict conversion
        class DictRecord(dict):
            pass

        session_record = DictRecord(dict_result)
        msg_record = DictRecord({
            "message_id": 1,
            "session_id": "sess-001",
            "agent_id": "agent-A",
            "message_type": "thesis",
        })

        conn.fetchrow = AsyncMock(return_value=session_record)
        conn.fetch = AsyncMock(return_value=[msg_record])

        result = await instance.get_session("sess-001")

        assert result["session_id"] == "sess-001"
        assert result["status"] == "active"
        assert len(result["messages"]) == 1
        assert result["messages"][0]["message_type"] == "thesis"

    @pytest.mark.asyncio
    async def test_get_session_parses_paused_agent_state_json_string(self, db):
        """get_session parses paused_agent_state_json from JSON string."""
        instance, pool, conn = db

        class DictRecord(dict):
            pass

        state_data = {"E": 0.5, "I": 0.6}
        session_record = DictRecord({
            "session_id": "sess-json",
            "paused_agent_state_json": json.dumps(state_data),
        })
        conn.fetchrow = AsyncMock(return_value=session_record)
        conn.fetch = AsyncMock(return_value=[])

        result = await instance.get_session("sess-json")

        assert "paused_agent_state_json" not in result
        assert result["paused_agent_state"] == state_data

    @pytest.mark.asyncio
    async def test_get_session_parses_paused_agent_state_dict(self, db):
        """get_session handles paused_agent_state_json already as dict."""
        instance, pool, conn = db

        class DictRecord(dict):
            pass

        state_data = {"E": 0.5, "I": 0.6}
        session_record = DictRecord({
            "session_id": "sess-dict",
            "paused_agent_state_json": state_data,  # already a dict
        })
        conn.fetchrow = AsyncMock(return_value=session_record)
        conn.fetch = AsyncMock(return_value=[])

        result = await instance.get_session("sess-dict")

        assert result["paused_agent_state"] == state_data

    @pytest.mark.asyncio
    async def test_get_session_parses_resolution_json_string(self, db):
        """get_session parses resolution_json from JSON string."""
        instance, pool, conn = db

        class DictRecord(dict):
            pass

        resolution_data = {"outcome": "resumed", "conditions": ["x", "y"]}
        session_record = DictRecord({
            "session_id": "sess-res",
            "resolution_json": json.dumps(resolution_data),
        })
        conn.fetchrow = AsyncMock(return_value=session_record)
        conn.fetch = AsyncMock(return_value=[])

        result = await instance.get_session("sess-res")

        assert "resolution_json" not in result
        assert result["resolution"] == resolution_data

    @pytest.mark.asyncio
    async def test_get_session_parses_resolution_json_dict(self, db):
        """get_session handles resolution_json already as dict."""
        instance, pool, conn = db

        class DictRecord(dict):
            pass

        resolution_data = {"outcome": "resumed"}
        session_record = DictRecord({
            "session_id": "sess-resdict",
            "resolution_json": resolution_data,
        })
        conn.fetchrow = AsyncMock(return_value=session_record)
        conn.fetch = AsyncMock(return_value=[])

        result = await instance.get_session("sess-resdict")

        assert result["resolution"] == resolution_data

    @pytest.mark.asyncio
    async def test_get_session_null_json_fields(self, db):
        """get_session handles None values in JSON fields gracefully."""
        instance, pool, conn = db

        class DictRecord(dict):
            pass

        session_record = DictRecord({
            "session_id": "sess-null",
            "paused_agent_state_json": None,
            "resolution_json": None,
        })
        conn.fetchrow = AsyncMock(return_value=session_record)
        conn.fetch = AsyncMock(return_value=[])

        result = await instance.get_session("sess-null")

        # Null JSON fields should be popped but not added as parsed keys
        assert "paused_agent_state_json" not in result
        assert "paused_agent_state" not in result
        assert "resolution_json" not in result
        assert "resolution" not in result

    @pytest.mark.asyncio
    async def test_get_session_no_json_columns(self, db):
        """get_session works when JSON columns are absent from row."""
        instance, pool, conn = db

        class DictRecord(dict):
            pass

        session_record = DictRecord({
            "session_id": "sess-nojson",
            "status": "active",
        })
        conn.fetchrow = AsyncMock(return_value=session_record)
        conn.fetch = AsyncMock(return_value=[])

        result = await instance.get_session("sess-nojson")

        assert result["session_id"] == "sess-nojson"
        assert "paused_agent_state" not in result
        assert "resolution" not in result


# ============================================================================
# DialecticDB.get_session_by_agent
# ============================================================================

class TestGetSessionByAgent:
    @pytest.mark.asyncio
    async def test_get_session_by_agent_found(self, db):
        """get_session_by_agent returns session when agent has active session."""
        instance, pool, conn = db

        class DictRecord(dict):
            def __getitem__(self, key):
                return dict.__getitem__(self, key)

        agent_row = DictRecord({"session_id": "sess-agent"})
        session_record = DictRecord({
            "session_id": "sess-agent",
            "paused_agent_id": "agent-A",
            "status": "active",
        })

        # First fetchrow for agent lookup, second for get_session
        conn.fetchrow = AsyncMock(side_effect=[agent_row, session_record])
        conn.fetch = AsyncMock(return_value=[])

        result = await instance.get_session_by_agent("agent-A")

        assert result is not None
        assert result["session_id"] == "sess-agent"

    @pytest.mark.asyncio
    async def test_get_session_by_agent_not_found(self, db):
        """get_session_by_agent returns None when no session for agent."""
        instance, pool, conn = db
        conn.fetchrow = AsyncMock(return_value=None)

        result = await instance.get_session_by_agent("agent-missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_session_by_agent_active_only_true(self, db):
        """get_session_by_agent with active_only=True filters resolved sessions."""
        instance, pool, conn = db
        conn.fetchrow = AsyncMock(return_value=None)

        await instance.get_session_by_agent("agent-A", active_only=True)

        # Check the SQL contains the status filter
        sql = conn.fetchrow.call_args[0][0]
        assert "NOT IN" in sql
        assert "resolved" in sql

    @pytest.mark.asyncio
    async def test_get_session_by_agent_active_only_false(self, db):
        """get_session_by_agent with active_only=False includes all sessions."""
        instance, pool, conn = db
        conn.fetchrow = AsyncMock(return_value=None)

        await instance.get_session_by_agent("agent-A", active_only=False)

        # Check the SQL does NOT contain the status filter
        sql = conn.fetchrow.call_args[0][0]
        assert "NOT IN" not in sql


# ============================================================================
# DialecticDB.get_all_sessions_by_agent
# ============================================================================

class TestGetAllSessionsByAgent:
    @pytest.mark.asyncio
    async def test_get_all_sessions_by_agent_multiple(self, db):
        """get_all_sessions_by_agent returns list of full sessions."""
        instance, pool, conn = db

        class DictRecord(dict):
            def __getitem__(self, key):
                return dict.__getitem__(self, key)

        # fetch returns list of session_id rows
        fetch_rows = [
            DictRecord({"session_id": "sess-1"}),
            DictRecord({"session_id": "sess-2"}),
        ]

        session_1 = DictRecord({"session_id": "sess-1", "status": "active"})
        session_2 = DictRecord({"session_id": "sess-2", "status": "active"})

        conn.fetch = AsyncMock(side_effect=[
            fetch_rows,        # get_all_sessions_by_agent fetch
            [],                # get_session messages for sess-1
            [],                # get_session messages for sess-2
        ])
        conn.fetchrow = AsyncMock(side_effect=[session_1, session_2])

        result = await instance.get_all_sessions_by_agent("agent-A")

        assert len(result) == 2
        assert result[0]["session_id"] == "sess-1"
        assert result[1]["session_id"] == "sess-2"

    @pytest.mark.asyncio
    async def test_get_all_sessions_by_agent_empty(self, db):
        """get_all_sessions_by_agent returns empty list when no sessions."""
        instance, pool, conn = db
        conn.fetch = AsyncMock(return_value=[])

        result = await instance.get_all_sessions_by_agent("agent-none")
        assert result == []

    @pytest.mark.asyncio
    async def test_get_all_sessions_by_agent_skips_missing(self, db):
        """get_all_sessions_by_agent skips sessions that get_session returns None for."""
        instance, pool, conn = db

        class DictRecord(dict):
            def __getitem__(self, key):
                return dict.__getitem__(self, key)

        fetch_rows = [DictRecord({"session_id": "sess-gone"})]

        conn.fetch = AsyncMock(side_effect=[
            fetch_rows,  # initial fetch
            [],          # get_session messages
        ])
        conn.fetchrow = AsyncMock(return_value=None)  # session not found

        result = await instance.get_all_sessions_by_agent("agent-A")
        assert result == []


# ============================================================================
# DialecticDB.update_session_phase
# ============================================================================

class TestUpdateSessionPhase:
    @pytest.mark.asyncio
    async def test_update_session_phase_success(self, db):
        """update_session_phase returns True when 1 row updated."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 1")

        result = await instance.update_session_phase("sess-001", "antithesis")
        assert result is True
        conn.execute.assert_awaited_once()
        call_args = conn.execute.call_args[0]
        assert call_args[1] == "antithesis"
        assert call_args[2] == "sess-001"

    @pytest.mark.asyncio
    async def test_update_session_phase_sql_has_terminal_guard(self, db):
        """Phase syncs must not stamp a terminal-status row (a phase write
        racing a concurrent resolution used to leave status='resolved',
        phase='failed'; rehydration trusts phase)."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 1")

        await instance.update_session_phase("sess-001", "failed")
        sql = conn.execute.call_args[0][0]
        assert "NOT IN ('resolved', 'failed')" in sql

    @pytest.mark.asyncio
    async def test_update_session_phase_not_found(self, db):
        """update_session_phase returns False when no rows updated."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 0")

        result = await instance.update_session_phase("sess-missing", "antithesis")
        assert result is False


# ============================================================================
# DialecticDB.update_session_reviewer
# ============================================================================

class TestUpdateSessionReviewer:
    @pytest.mark.asyncio
    async def test_update_session_reviewer_success(self, db):
        """update_session_reviewer returns True on successful update."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 1")

        result = await instance.update_session_reviewer("sess-001", "reviewer-B")
        assert result is True
        call_args = conn.execute.call_args[0]
        assert call_args[1] == "reviewer-B"
        assert call_args[2] == "sess-001"

    @pytest.mark.asyncio
    async def test_update_session_reviewer_not_found(self, db):
        """update_session_reviewer returns False when session not found."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 0")
        conn.fetchrow = AsyncMock(return_value=None)

        result = await instance.update_session_reviewer("sess-nope", "reviewer-B")
        assert result is False

    @pytest.mark.asyncio
    async def test_update_session_reviewer_sql_has_terminal_guard(self, db):
        """The reviewer UPDATE must refuse terminal rows in SQL (dual-writer
        TOCTOU). Exact-clause assert: the guard set is pinned to precisely
        ('resolved','failed') — the storable terminal values under
        dialectic_sessions_status_check and DialecticSaga's commit guard.
        Broadening it (e.g. adding 'active' or the unstorable
        'timeout'/'abandoned') breaks this test on purpose."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 1")

        await instance.update_session_reviewer("sess-001", "reviewer-B")
        sql = conn.execute.call_args[0][0]
        assert "NOT IN ('resolved', 'failed')" in sql
        assert instance.TERMINAL_WRITE_GUARD == ("resolved", "failed")

    @pytest.mark.asyncio
    async def test_update_session_reviewer_refused_on_terminal(self, db):
        """A session that resolved mid-sweep must not get a reviewer write."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 0")
        conn.fetchrow = AsyncMock(return_value={"status": "resolved"})

        result = await instance.update_session_reviewer("sess-001", "reviewer-B")
        assert result is False


# ============================================================================
# DialecticDB.update_session_status
# ============================================================================

class TestUpdateSessionStatus:
    @pytest.mark.asyncio
    async def test_update_session_status_success(self, db):
        """update_session_status returns True on successful update."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 1")

        result = await instance.update_session_status("sess-001", "failed")
        assert result is True
        call_args = conn.execute.call_args[0]
        # Both status and phase set to the same value
        assert call_args[1] == "failed"
        assert call_args[2] == "sess-001"

    @pytest.mark.asyncio
    async def test_update_session_status_not_found(self, db):
        """update_session_status returns False when session not found."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 0")
        conn.fetchrow = AsyncMock(return_value=None)

        result = await instance.update_session_status("sess-gone", "failed")
        assert result is False

    @pytest.mark.asyncio
    async def test_update_session_status_sql_has_terminal_guard(self, db):
        """The status UPDATE must refuse terminal rows in SQL (dual-writer
        TOCTOU). Exact clause pinned — see the reviewer-guard test."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 1")

        await instance.update_session_status("sess-001", "failed")
        sql = conn.execute.call_args[0][0]
        assert "NOT IN ('resolved', 'failed')" in sql

    @pytest.mark.asyncio
    async def test_update_session_status_refused_overwrite_of_resolved(self, db):
        """The sweeper must not clobber a session another writer resolved."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 0")
        conn.fetchrow = AsyncMock(return_value={"status": "resolved"})

        result = await instance.update_session_status("sess-001", "failed")
        assert result is False

    @pytest.mark.asyncio
    async def test_update_session_status_same_state_is_not_this_callers_win(self, db):
        """No-write returns False even when the row already holds the
        requested status: another writer produced that outcome (BEAM
        liveness also writes 'failed'); the caller must not narrate it as
        its own. Idempotent-replay callers use resolve_session instead."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 0")
        conn.fetchrow = AsyncMock(return_value={"status": "failed"})

        result = await instance.update_session_status("sess-001", "failed")
        assert result is False


# ============================================================================
# DialecticDB.update_session_awaiting_facilitation (#1167 Ask 2)
# ============================================================================

class TestUpdateSessionAwaitingFacilitation:
    @pytest.mark.asyncio
    async def test_set_true_success(self, db):
        """update_session_awaiting_facilitation persists the flag and returns True."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 1")

        result = await instance.update_session_awaiting_facilitation("sess-001", True)
        assert result is True
        call_args = conn.execute.call_args[0]
        assert call_args[1] is True
        assert call_args[2] == "sess-001"

    @pytest.mark.asyncio
    async def test_clear_false_success(self, db):
        """Clearing the flag passes False to the UPDATE."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 1")

        result = await instance.update_session_awaiting_facilitation("sess-001", False)
        assert result is True
        assert conn.execute.call_args[0][1] is False

    @pytest.mark.asyncio
    async def test_not_found(self, db):
        """Returns False when no row matched."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 0")

        result = await instance.update_session_awaiting_facilitation("sess-gone", True)
        assert result is False


# ============================================================================
# DialecticDB.mark_awaiting_facilitation
# ============================================================================

class TestMarkAwaitingFacilitation:
    @pytest.mark.asyncio
    async def test_carries_the_terminal_write_guard(self, db):
        """Setting the flag is guarded; the unguarded writer is for clearing it.

        `reopen_session` reopens exactly `status='failed' AND
        awaiting_facilitation=true`, so stamping the flag onto a row another
        writer has just failed would make an ordinary failure revivable.
        """
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 1")

        result = await instance.mark_awaiting_facilitation("sess-001")

        assert result is True
        sql = " ".join(conn.execute.call_args[0][0].split())
        assert "awaiting_facilitation = true" in sql
        assert "status NOT IN ('resolved', 'failed')" in sql
        assert conn.execute.call_args[0][1] == "sess-001"

    @pytest.mark.asyncio
    async def test_does_not_touch_updated_at(self, db):
        """`updated_at` is the sweeper's staleness clock — leave it alone.

        The sweeper is the only unattended retry of `select_reviewer`. Bumping
        `updated_at` here would drop the session out of the stuck set for a
        full STUCK_SESSION_THRESHOLD (2h), so a reviewer who becomes available
        ten minutes after the request would not be picked up for two hours.
        Recording that a session waits on a human must not stop a machine
        rescuing it.
        """
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 1")

        await instance.mark_awaiting_facilitation("sess-001")

        assert "updated_at" not in conn.execute.call_args[0][0]

    @pytest.mark.asyncio
    async def test_refused_on_a_terminal_row(self, db):
        """A refused write returns False so the caller does not narrate it."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 0")
        conn.fetchrow = AsyncMock(return_value={"status": "resolved"})

        assert await instance.mark_awaiting_facilitation("sess-001") is False

    @pytest.mark.asyncio
    async def test_missing_row(self, db):
        """A vanished row is also False, and does not raise."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="UPDATE 0")
        conn.fetchrow = AsyncMock(return_value=None)

        assert await instance.mark_awaiting_facilitation("sess-gone") is False


# ============================================================================
# DialecticDB.resolve_session
# ============================================================================

class TestResolveSession:
    @pytest.mark.asyncio
    async def test_resolve_session_success(self, db):
        """resolve_session sets status, phase, resolution_json via the guarded UPDATE."""
        instance, pool, conn = db
        # B-4 guard: UPDATE...RETURNING now goes through fetchrow; a returned row
        # means the terminal transition was performed.
        conn.fetchrow = AsyncMock(return_value={"session_id": "sess-001"})

        resolution = {"outcome": "resumed", "conditions": ["monitor"]}
        result = await instance.resolve_session("sess-001", resolution)

        assert result is True
        call_args = conn.fetchrow.call_args_list[0][0]  # UPDATE...RETURNING
        # args: sql, status, phase, resolution_json, session_id
        assert call_args[1] == "resolved"  # status
        assert call_args[2] == "resolved"  # phase matches status
        assert call_args[3] == json.dumps(resolution)
        assert call_args[4] == "sess-001"
        assert "NOT IN ('resolved', 'failed')" in call_args[0]  # idempotent guard

    @pytest.mark.asyncio
    async def test_resolve_session_custom_status(self, db):
        """resolve_session accepts custom status (e.g. failed)."""
        instance, pool, conn = db
        conn.fetchrow = AsyncMock(return_value={"session_id": "sess-001"})

        result = await instance.resolve_session("sess-001", {"x": 1}, status="failed")
        assert result is True
        call_args = conn.fetchrow.call_args_list[0][0]
        assert call_args[1] == "failed"   # status
        assert call_args[2] == "failed"   # phase matches status (no longer hardcoded 'resolved')

    @pytest.mark.asyncio
    async def test_resolve_session_not_found(self, db):
        """resolve_session returns False when session missing.

        The guarded UPDATE matches no row (fetchrow -> None); the follow-up
        existence SELECT also returns None -> the session does not exist.
        """
        instance, pool, conn = db
        conn.fetchrow = AsyncMock(side_effect=[None, None])

        result = await instance.resolve_session("sess-nope", {"x": 1})
        assert result is False


# ============================================================================
# DialecticDB.add_message
# ============================================================================

class TestAddMessage:
    @pytest.mark.asyncio
    async def test_add_message_full_args(self, db):
        """add_message inserts with all parameters and returns message_id."""
        instance, pool, conn = db

        class DictRecord(dict):
            def __getitem__(self, key):
                return dict.__getitem__(self, key)

        msg_row = DictRecord({"message_id": 42})
        conn.fetchrow = AsyncMock(return_value=msg_row)
        conn.execute = AsyncMock(return_value="UPDATE 1")

        result = await instance.add_message(
            session_id="sess-001",
            agent_id="agent-A",
            message_type="thesis",
            root_cause="stuck on confidence",
            proposed_conditions=["wait", "retry"],
            reasoning="Agent was below threshold",
            observed_metrics={"confidence": 0.3},
            concerns=["low sample size"],
            agrees=True,
            signature="sig-xyz",
        )

        assert result == 42

        # Verify INSERT call
        insert_call = conn.fetchrow.call_args[0]
        assert insert_call[1] == "sess-001"
        assert insert_call[2] == "agent-A"
        assert insert_call[3] == "thesis"
        assert insert_call[4] == "stuck on confidence"
        assert insert_call[5] == json.dumps(["wait", "retry"])
        assert insert_call[6] == "Agent was below threshold"
        assert insert_call[7] == json.dumps({"confidence": 0.3})
        assert insert_call[8] == json.dumps(["low sample size"])
        assert insert_call[9] is True
        assert insert_call[10] == "sig-xyz"

        # Verify session updated_at was also updated
        conn.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_add_message_minimal_args(self, db):
        """add_message with only required args, optional are None."""
        instance, pool, conn = db

        class DictRecord(dict):
            def __getitem__(self, key):
                return dict.__getitem__(self, key)

        msg_row = DictRecord({"message_id": 7})
        conn.fetchrow = AsyncMock(return_value=msg_row)
        conn.execute = AsyncMock(return_value="UPDATE 1")

        result = await instance.add_message(
            session_id="sess-002",
            agent_id="agent-B",
            message_type="antithesis",
        )

        assert result == 7
        insert_call = conn.fetchrow.call_args[0]
        assert insert_call[4] is None   # root_cause
        assert insert_call[5] is None   # proposed_conditions
        assert insert_call[7] is None   # observed_metrics
        assert insert_call[8] is None   # concerns
        assert insert_call[9] is None   # agrees

    @pytest.mark.asyncio
    async def test_add_message_returns_0_on_null_row(self, db):
        """add_message returns 0 when INSERT RETURNING yields None."""
        instance, pool, conn = db
        conn.fetchrow = AsyncMock(return_value=None)
        conn.execute = AsyncMock(return_value="UPDATE 1")

        result = await instance.add_message(
            session_id="sess-003",
            agent_id="agent-C",
            message_type="synthesis",
        )

        assert result == 0


# ============================================================================
# DialecticDB.is_agent_in_active_session
# ============================================================================

class TestIsAgentInActiveSession:
    @pytest.mark.asyncio
    async def test_agent_in_active_session_true(self, db):
        """is_agent_in_active_session returns True when row found."""
        instance, pool, conn = db
        conn.fetchrow = AsyncMock(return_value={"exists": 1})

        result = await instance.is_agent_in_active_session("agent-A")
        assert result is True

    @pytest.mark.asyncio
    async def test_excludes_terminal_phase_not_only_terminal_status(self, db):
        """The guard must be terminal on EITHER column, not on status alone.

        Python may not write terminal status (TERMINAL_WRITE_GUARD reserves both
        terminal transitions for BEAM), so the synthesis-timeout path sets
        phase='failed' and leaves status='active'. Reading status alone treated
        that finished session as active and locked the agent out of opening a
        new one until the >2h auto-resolve sweeper reaped it.

        Asserting on the SQL because the shape only exists inside that window --
        a point-in-time integration query almost always returns zero rows and
        would let this regress silently.
        """
        instance, pool, conn = db
        conn.fetchrow = AsyncMock(return_value=None)

        await instance.is_agent_in_active_session("agent-A")

        sql = " ".join(conn.fetchrow.call_args[0][0].split())
        assert "status NOT IN ('resolved', 'failed', 'timeout', 'abandoned')" in sql
        assert "phase NOT IN ('resolved', 'failed')" in sql

    @pytest.mark.asyncio
    async def test_agent_in_active_session_false(self, db):
        """is_agent_in_active_session returns False when no row."""
        instance, pool, conn = db
        conn.fetchrow = AsyncMock(return_value=None)

        result = await instance.is_agent_in_active_session("agent-Z")
        assert result is False


# ============================================================================
# DialecticDB.has_recently_reviewed
# ============================================================================

class TestHasRecentlyReviewed:
    @pytest.mark.asyncio
    async def test_has_recently_reviewed_true(self, db):
        """has_recently_reviewed returns True when resolved session found."""
        instance, pool, conn = db
        conn.fetchrow = AsyncMock(return_value={"exists": 1})

        result = await instance.has_recently_reviewed("reviewer-B", "agent-A")
        assert result is True

    @pytest.mark.asyncio
    async def test_has_recently_reviewed_false(self, db):
        """has_recently_reviewed returns False when no match."""
        instance, pool, conn = db
        conn.fetchrow = AsyncMock(return_value=None)

        result = await instance.has_recently_reviewed("reviewer-B", "agent-A")
        assert result is False

    @pytest.mark.asyncio
    async def test_has_recently_reviewed_custom_hours(self, db):
        """has_recently_reviewed passes hours parameter to SQL."""
        instance, pool, conn = db
        conn.fetchrow = AsyncMock(return_value=None)

        await instance.has_recently_reviewed("reviewer-B", "agent-A", hours=48)

        call_args = conn.fetchrow.call_args[0]
        assert call_args[1] == "reviewer-B"
        assert call_args[2] == "agent-A"
        assert call_args[3] == 48

    @pytest.mark.asyncio
    async def test_has_recently_reviewed_checks_both_pair_directions(self, db):
        """A->B followed by B->A is inside the same reciprocal cooldown."""
        instance, pool, conn = db
        conn.fetchrow = AsyncMock(return_value=None)

        await instance.has_recently_reviewed("agent-B", "agent-A")

        sql = " ".join(conn.fetchrow.await_args.args[0].split())
        assert "reviewer_agent_id = $1 AND paused_agent_id = $2" in sql
        assert "reviewer_agent_id = $2 AND paused_agent_id = $1" in sql


# ============================================================================
# DialecticDB.get_active_sessions
# ============================================================================

class TestGetActiveSessions:
    @pytest.mark.asyncio
    async def test_get_active_sessions(self, db):
        """get_active_sessions returns list of session dicts."""
        instance, pool, conn = db

        class DictRecord(dict):
            pass

        rows = [
            DictRecord({"session_id": "s1", "status": "active"}),
            DictRecord({"session_id": "s2", "status": "active"}),
        ]
        conn.fetch = AsyncMock(return_value=rows)

        result = await instance.get_active_sessions()

        assert len(result) == 2
        assert result[0]["session_id"] == "s1"
        assert result[1]["session_id"] == "s2"

    @pytest.mark.asyncio
    async def test_get_active_sessions_empty(self, db):
        """get_active_sessions returns empty list when none."""
        instance, pool, conn = db
        conn.fetch = AsyncMock(return_value=[])

        result = await instance.get_active_sessions()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_active_sessions_custom_limit(self, db):
        """get_active_sessions passes limit to SQL."""
        instance, pool, conn = db
        conn.fetch = AsyncMock(return_value=[])

        await instance.get_active_sessions(limit=10)

        call_args = conn.fetch.call_args[0]
        assert call_args[1] == 10


# ============================================================================
# DialecticDB.get_sessions_awaiting_reviewer
# ============================================================================

class TestGetSessionsAwaitingReviewer:
    @pytest.mark.asyncio
    async def test_get_sessions_awaiting_reviewer(self, db):
        """get_sessions_awaiting_reviewer returns sessions with no reviewer."""
        instance, pool, conn = db

        class DictRecord(dict):
            pass

        rows = [
            DictRecord({"session_id": "s-wait", "reviewer_agent_id": None}),
        ]
        conn.fetch = AsyncMock(return_value=rows)

        result = await instance.get_sessions_awaiting_reviewer()

        assert len(result) == 1
        assert result[0]["reviewer_agent_id"] is None

    @pytest.mark.asyncio
    async def test_get_sessions_awaiting_reviewer_empty(self, db):
        """get_sessions_awaiting_reviewer returns empty list when all have reviewers."""
        instance, pool, conn = db
        conn.fetch = AsyncMock(return_value=[])

        result = await instance.get_sessions_awaiting_reviewer()
        assert result == []


# ============================================================================
# DialecticDB.get_stats
# ============================================================================

class TestGetStats:
    @pytest.mark.asyncio
    async def test_get_stats(self, db):
        """get_stats returns aggregated statistics."""
        instance, pool, conn = db

        class DictRecord(dict):
            def __getitem__(self, key):
                return dict.__getitem__(self, key)

        type_rows = [
            DictRecord({"session_type": "recovery", "count": 15}),
            DictRecord({"session_type": None, "count": 10}),
        ]
        msg_count_row = DictRecord({"count": 100})
        sess_count_row = DictRecord({"count": 25})

        # One fetch only: the status breakdown was removed 2026-08-22.
        conn.fetch = AsyncMock(side_effect=[type_rows])
        conn.fetchrow = AsyncMock(side_effect=[msg_count_row, sess_count_row])

        result = await instance.get_stats()

        # ⛔No `by_status`: a per-status count is not outcome data (#1689).
        # Removed 2026-08-22; use get_outcome_breakdown() for quality reads.
        assert "by_status" not in result
        assert result["by_type"] == {"recovery": 15, "unknown": 10}
        assert result["total_messages"] == 100
        assert result["total_sessions"] == 25

    @pytest.mark.asyncio
    async def test_get_stats_empty_db(self, db):
        """get_stats handles empty database."""
        instance, pool, conn = db

        class DictRecord(dict):
            def __getitem__(self, key):
                return dict.__getitem__(self, key)

        conn.fetch = AsyncMock(side_effect=[[], []])
        msg_row = DictRecord({"count": 0})
        sess_row = DictRecord({"count": 0})
        conn.fetchrow = AsyncMock(side_effect=[msg_row, sess_row])

        result = await instance.get_stats()

        assert "by_status" not in result
        assert result["by_type"] == {}
        assert result["total_messages"] == 0
        assert result["total_sessions"] == 0

    @pytest.mark.asyncio
    async def test_get_stats_null_counts(self, db):
        """get_stats handles None from fetchrow."""
        instance, pool, conn = db
        conn.fetch = AsyncMock(side_effect=[[], []])
        conn.fetchrow = AsyncMock(return_value=None)

        result = await instance.get_stats()

        assert result["total_messages"] == 0
        assert result["total_sessions"] == 0


# ============================================================================
# DialecticDB.health_check
# ============================================================================

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check(self, db):
        """health_check returns backend info and counts."""
        instance, pool, conn = db
        conn.fetchval = AsyncMock(side_effect=[72, 149])

        result = await instance.health_check()

        assert result["backend"] == "postgres"
        assert result["total_sessions"] == 72
        assert result["total_messages"] == 149

    @pytest.mark.asyncio
    async def test_health_check_null_counts(self, db):
        """health_check handles None from fetchval."""
        instance, pool, conn = db
        conn.fetchval = AsyncMock(return_value=None)

        result = await instance.health_check()

        assert result["backend"] == "postgres"
        assert result["total_sessions"] == 0
        assert result["total_messages"] == 0


# ============================================================================
# Singleton: get_dialectic_db()
# ============================================================================

class TestGetDialecticDb:
    @pytest.mark.asyncio
    async def test_get_dialectic_db_creates_singleton(self):
        """get_dialectic_db creates and returns a DialecticDB instance."""
        mock_db_backend = AsyncMock()
        mock_db_backend._pool = MagicMock()

        with patch("src.db.get_db", return_value=mock_db_backend):
            db1 = await get_dialectic_db()
            assert isinstance(db1, DialecticDB)
            assert db1._initialized is True

    @pytest.mark.asyncio
    async def test_get_dialectic_db_returns_same_instance(self):
        """get_dialectic_db returns the same instance on subsequent calls."""
        mock_db_backend = AsyncMock()
        mock_db_backend._pool = MagicMock()

        with patch("src.db.get_db", return_value=mock_db_backend):
            db1 = await get_dialectic_db()
            db2 = await get_dialectic_db()
            assert db1 is db2

    @pytest.mark.asyncio
    async def test_get_dialectic_db_creates_lock(self):
        """get_dialectic_db creates a lock on first call."""
        mock_db_backend = AsyncMock()
        mock_db_backend._pool = MagicMock()

        assert dialectic_db_module._db_lock is None

        with patch("src.db.get_db", return_value=mock_db_backend):
            await get_dialectic_db()
            assert dialectic_db_module._db_lock is not None
            assert isinstance(dialectic_db_module._db_lock, asyncio.Lock)


# ============================================================================
# Convenience Async Wrappers
# ============================================================================

class TestConvenienceWrappers:
    """Test that convenience async wrappers delegate to the singleton instance."""

    @pytest.fixture
    def mock_singleton(self):
        """Set up a mock DialecticDB as the singleton."""
        mock_db = AsyncMock(spec=DialecticDB)
        dialectic_db_module._db_instance = mock_db
        dialectic_db_module._db_lock = asyncio.Lock()
        return mock_db

    @pytest.mark.asyncio
    async def test_create_session_async(self, mock_singleton):
        mock_singleton.create_session = AsyncMock(return_value={"session_id": "s1", "created": True})

        result = await create_session_async(session_id="s1", paused_agent_id="a1")

        mock_singleton.create_session.assert_awaited_once_with(session_id="s1", paused_agent_id="a1")
        assert result["created"] is True

    @pytest.mark.asyncio
    async def test_get_session_async(self, mock_singleton):
        mock_singleton.get_session = AsyncMock(return_value={"session_id": "s1"})

        result = await get_session_async("s1")

        mock_singleton.get_session.assert_awaited_once_with("s1")
        assert result["session_id"] == "s1"

    @pytest.mark.asyncio
    async def test_get_session_by_agent_async(self, mock_singleton):
        mock_singleton.get_session_by_agent = AsyncMock(return_value={"session_id": "s1"})

        result = await get_session_by_agent_async("agent-A", active_only=False)

        mock_singleton.get_session_by_agent.assert_awaited_once_with("agent-A", False)
        assert result["session_id"] == "s1"

    @pytest.mark.asyncio
    async def test_get_all_sessions_by_agent_async(self, mock_singleton):
        mock_singleton.get_all_sessions_by_agent = AsyncMock(return_value=[{"session_id": "s1"}])

        result = await get_all_sessions_by_agent_async("agent-A")

        mock_singleton.get_all_sessions_by_agent.assert_awaited_once_with("agent-A")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_is_agent_in_active_session_async(self, mock_singleton):
        mock_singleton.is_agent_in_active_session = AsyncMock(return_value=True)

        result = await is_agent_in_active_session_async("agent-A")

        mock_singleton.is_agent_in_active_session.assert_awaited_once_with("agent-A")
        assert result is True

    @pytest.mark.asyncio
    async def test_has_recently_reviewed_async(self, mock_singleton):
        mock_singleton.has_recently_reviewed = AsyncMock(return_value=False)

        result = await has_recently_reviewed_async("reviewer-B", "agent-A", hours=48)

        mock_singleton.has_recently_reviewed.assert_awaited_once_with("reviewer-B", "agent-A", 48)
        assert result is False

    @pytest.mark.asyncio
    async def test_add_message_async(self, mock_singleton):
        mock_singleton.add_message = AsyncMock(return_value=42)

        result = await add_message_async(session_id="s1", agent_id="a1", message_type="thesis")

        mock_singleton.add_message.assert_awaited_once_with(
            session_id="s1", agent_id="a1", message_type="thesis"
        )
        assert result == 42

    @pytest.mark.asyncio
    async def test_update_session_phase_async(self, mock_singleton):
        mock_singleton.update_session_phase = AsyncMock(return_value=True)

        result = await update_session_phase_async("s1", "antithesis")

        # synthesis_round defaults to None so the writer COALESCEs and leaves the
        # stored value alone — callers that do not track rounds are unchanged.
        mock_singleton.update_session_phase.assert_awaited_once_with(
            "s1", "antithesis", None
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_update_session_phase_async_forwards_synthesis_round(
        self, mock_singleton
    ):
        """The round must reach the writer. Before 2026-08-16 it was dropped on
        both sides, so every row read synthesis_round=0 regardless of how many
        synthesis messages a session carried."""
        mock_singleton.update_session_phase = AsyncMock(return_value=True)

        await update_session_phase_async("s1", "synthesis", 2)

        mock_singleton.update_session_phase.assert_awaited_once_with("s1", "synthesis", 2)

    @pytest.mark.asyncio
    async def test_update_session_phase_async_forwards_round_zero(self, mock_singleton):
        """0 is a real round, not 'absent'."""
        mock_singleton.update_session_phase = AsyncMock(return_value=True)

        await update_session_phase_async("s1", "thesis", 0)

        mock_singleton.update_session_phase.assert_awaited_once_with("s1", "thesis", 0)

    @pytest.mark.asyncio
    async def test_update_session_reviewer_async(self, mock_singleton):
        mock_singleton.update_session_reviewer = AsyncMock(return_value=True)

        result = await update_session_reviewer_async("s1", "reviewer-B")

        mock_singleton.update_session_reviewer.assert_awaited_once_with("s1", "reviewer-B")
        assert result is True

    @pytest.mark.asyncio
    async def test_update_session_status_async(self, mock_singleton):
        mock_singleton.update_session_status = AsyncMock(return_value=True)

        result = await update_session_status_async("s1", "failed")

        mock_singleton.update_session_status.assert_awaited_once_with("s1", "failed")
        assert result is True

    @pytest.mark.asyncio
    async def test_resolve_session_async(self, mock_singleton):
        mock_singleton.resolve_session = AsyncMock(return_value=True)

        result = await resolve_session_async("s1", {"outcome": "ok"}, status="resolved")

        mock_singleton.resolve_session.assert_awaited_once_with("s1", {"outcome": "ok"}, "resolved")
        assert result is True

    @pytest.mark.asyncio
    async def test_get_active_sessions_async(self, mock_singleton):
        mock_singleton.get_active_sessions = AsyncMock(return_value=[])

        result = await get_active_sessions_async(limit=50)

        mock_singleton.get_active_sessions.assert_awaited_once_with(50)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_sessions_awaiting_reviewer_async(self, mock_singleton):
        mock_singleton.get_sessions_awaiting_reviewer = AsyncMock(return_value=[{"session_id": "s1"}])

        result = await get_sessions_awaiting_reviewer_async()

        mock_singleton.get_sessions_awaiting_reviewer.assert_awaited_once()
        assert len(result) == 1


# ============================================================================
# Edge Cases
# ============================================================================

class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_create_session_paused_agent_state_none_serialization(self, db):
        """create_session passes None for paused_agent_state_json when state is None."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="INSERT 0 1")

        await instance.create_session(
            session_id="sess-nostate",
            paused_agent_id="agent-A",
            paused_agent_state=None,
        )

        call_args = conn.execute.call_args[0]
        assert call_args[13] is None

    @pytest.mark.asyncio
    async def test_create_session_synthesis_round_defaults_to_zero(self, db):
        """create_session defaults synthesis_round to 0 when None."""
        instance, pool, conn = db
        conn.execute = AsyncMock(return_value="INSERT 0 1")

        await instance.create_session(
            session_id="sess-synth0",
            paused_agent_id="agent-A",
            synthesis_round=None,
        )

        call_args = conn.execute.call_args[0]
        assert call_args[12] == 0  # synthesis_round

    @pytest.mark.asyncio
    async def test_resolve_session_serializes_complex_resolution(self, db):
        """resolve_session correctly JSON-serializes complex resolution objects."""
        instance, pool, conn = db
        conn.fetchrow = AsyncMock(return_value={"session_id": "sess-complex"})

        resolution = {
            "outcome": "resumed",
            "conditions": [
                {"type": "monitor", "duration": 3600},
                {"type": "threshold", "value": 0.5},
            ],
            "agreed_by": ["agent-A", "reviewer-B"],
        }

        await instance.resolve_session("sess-complex", resolution)

        call_args = conn.fetchrow.call_args_list[0][0]  # UPDATE...RETURNING
        # args: sql, status, phase, resolution_json, session_id
        assert json.loads(call_args[3]) == resolution

    @pytest.mark.asyncio
    async def test_add_message_json_serialization(self, db):
        """add_message serializes list and dict fields to JSON."""
        instance, pool, conn = db

        class DictRecord(dict):
            def __getitem__(self, key):
                return dict.__getitem__(self, key)

        conn.fetchrow = AsyncMock(return_value=DictRecord({"message_id": 1}))
        conn.execute = AsyncMock(return_value="UPDATE 1")

        conditions = ["cond1", "cond2"]
        metrics = {"m1": 0.5}
        concerns = ["c1"]

        await instance.add_message(
            session_id="sess-ser",
            agent_id="agent-A",
            message_type="synthesis",
            proposed_conditions=conditions,
            observed_metrics=metrics,
            concerns=concerns,
        )

        insert_call = conn.fetchrow.call_args[0]
        assert json.loads(insert_call[5]) == conditions
        assert json.loads(insert_call[7]) == metrics
        assert json.loads(insert_call[8]) == concerns

    @pytest.mark.asyncio
    async def test_get_session_multiple_messages(self, db):
        """get_session returns all messages ordered by message_id."""
        instance, pool, conn = db

        class DictRecord(dict):
            pass

        session_record = DictRecord({"session_id": "sess-msgs", "status": "active"})
        msg1 = DictRecord({"message_id": 1, "message_type": "thesis"})
        msg2 = DictRecord({"message_id": 2, "message_type": "antithesis"})
        msg3 = DictRecord({"message_id": 3, "message_type": "synthesis"})

        conn.fetchrow = AsyncMock(return_value=session_record)
        conn.fetch = AsyncMock(return_value=[msg1, msg2, msg3])

        result = await instance.get_session("sess-msgs")

        assert len(result["messages"]) == 3
        assert result["messages"][0]["message_type"] == "thesis"
        assert result["messages"][1]["message_type"] == "antithesis"
        assert result["messages"][2]["message_type"] == "synthesis"

    @pytest.mark.asyncio
    async def test_get_session_both_json_fields_present(self, db):
        """get_session handles both paused_agent_state_json and resolution_json."""
        instance, pool, conn = db

        class DictRecord(dict):
            pass

        state = {"E": 0.7}
        resolution = {"outcome": "ok"}
        session_record = DictRecord({
            "session_id": "sess-both",
            "paused_agent_state_json": json.dumps(state),
            "resolution_json": json.dumps(resolution),
        })
        conn.fetchrow = AsyncMock(return_value=session_record)
        conn.fetch = AsyncMock(return_value=[])

        result = await instance.get_session("sess-both")

        assert result["paused_agent_state"] == state
        assert result["resolution"] == resolution
        assert "paused_agent_state_json" not in result
        assert "resolution_json" not in result


class TestTerminalWriteGuardIsLoadBearing:
    """`TERMINAL_WRITE_GUARD` must govern the SQL it claims to govern.

    The constant is declarative: every guarded ``UPDATE`` inlines
    ``('resolved', 'failed')`` and none of them interpolates
    ``TERMINAL_WRITE_GUARD``. So the constant and the SQL are two independent
    copies, and before this test the only thing tying them together was a
    docstring -- three of which say a writer "carries TERMINAL_WRITE_GUARD"
    when nothing in the code makes that true.

    ⛔This does NOT make the constant load-bearing at runtime; the SQL is still
    a literal. What it makes load-bearing is the *claim*: change the constant
    without changing all five statements, or add a sixth guarded writer that
    disagrees, and CI fails here instead of the divergence being discovered by
    a dual-writer incident.

    Deliberately derives the expected fragment FROM the constant rather than
    hardcoding it, which is the difference between this and the per-method
    assertion above -- that one restates the literal twice and passes even if
    both copies drift together.
    """

    GUARDED_WRITERS = (
        "update_session_phase",
        "update_session_reviewer",
        "update_session_status",
        "mark_awaiting_facilitation",
        "resolve_session",
    )

    @staticmethod
    def _squash(text):
        """Drop whitespace so formatting is not part of the assertion.

        Without this the check fails on a cosmetic reformat -- reindenting the
        SQL, or writing ``('resolved','failed')`` without the space. Membership
        is what must not drift; layout is not this test's business, and a test
        that cries on a reindent gets weakened by whoever hits it.
        """
        return "".join(text.split())

    def _expected_fragment(self, instance):
        joined = ", ".join(f"'{status}'" for status in instance.TERMINAL_WRITE_GUARD)
        return self._squash(f"NOT IN ({joined})")

    def test_every_guarded_writer_carries_the_constant(self, db):
        """All five statements must spell exactly what the constant declares."""
        import inspect

        instance, _pool, _conn = db
        expected = self._expected_fragment(instance)

        for name in self.GUARDED_WRITERS:
            source = self._squash(inspect.getsource(getattr(type(instance), name)))
            assert expected in source, (
                f"{name} does not carry {expected!r}. Either its SQL drifted from "
                "TERMINAL_WRITE_GUARD, or the constant changed and this writer was "
                "missed -- both are how a terminal row becomes writable again."
            )

    def test_no_unlisted_writer_carries_the_guard(self, db):
        """A sixth guarded WRITE must be declared here, not appear silently.

        The failure this catches is additive: someone adds a guarded write, the
        five above still pass, and the new one is never checked against the
        constant again.

        ⛔Keyed on methods that actually ``UPDATE`` the sessions table, not on
        occurrences of the fragment. `is_agent_in_active_session` carries the
        same literal as a **read** predicate (`phase NOT IN (...)`), and an
        earlier draft of this test counted it as a sixth writer and failed.
        That near-miss is the reason for the distinction rather than an
        argument against it: the literal is load-bearing in two different
        senses in one file, and only one of them is this constant's business.
        """
        import inspect

        instance, _pool, _conn = db
        expected = self._expected_fragment(instance)
        cls = type(instance)

        unlisted = []
        for name in dir(cls):
            if name.startswith("__") or name in self.GUARDED_WRITERS:
                continue
            attr = inspect.unwrap(getattr(cls, name, None) or (lambda: None))
            try:
                source = inspect.getsource(attr)
            except (TypeError, OSError):
                continue
            squashed = self._squash(source)
            if self._squash("UPDATE core.dialectic_sessions") in squashed and expected in squashed:
                unlisted.append(name)

        assert not unlisted, (
            f"{unlisted} UPDATE core.dialectic_sessions carrying {expected!r} but are "
            "not in GUARDED_WRITERS. Add them so they are checked against the constant."
        )


class TestProbeInflightSagaTriState:
    """`has_inflight_saga` fails open; `probe_inflight_saga` must not.

    Fail-open is right for a write gate — no saga infrastructure means nothing
    to race, so the sweep should proceed. It is wrong for an instrument: a probe
    that answers "no saga" when it could not look turns an outage into evidence
    of a collision-free system.
    """

    @pytest.mark.asyncio
    async def test_probe_reports_present(self, db):
        instance, _pool, conn = db
        conn.fetchrow = AsyncMock(return_value={"?column?": 1})
        assert await instance.probe_inflight_saga("s1") is True

    @pytest.mark.asyncio
    async def test_probe_reports_absent(self, db):
        instance, _pool, conn = db
        conn.fetchrow = AsyncMock(return_value=None)
        assert await instance.probe_inflight_saga("s1") is False

    @pytest.mark.asyncio
    async def test_probe_reports_unanswerable_as_none(self, db):
        """The third state — not False, which would be a fabricated absence."""
        instance, _pool, conn = db
        conn.fetchrow = AsyncMock(side_effect=RuntimeError("relation absent"))

        result = await instance.probe_inflight_saga("s1")

        assert result is None, (
            "an unanswerable probe must be None; returning False would let a "
            "saga-table outage count as an observed absence of overlap"
        )

    @pytest.mark.asyncio
    async def test_write_gate_still_fails_open(self, db):
        """The guard's behaviour is unchanged — it must still proceed on error."""
        instance, _pool, conn = db
        conn.fetchrow = AsyncMock(side_effect=RuntimeError("relation absent"))

        assert await instance.has_inflight_saga("s1") is False, (
            "the write gate must keep failing open; only the instrument "
            "distinguishes 'could not look' from 'nothing there'"
        )
