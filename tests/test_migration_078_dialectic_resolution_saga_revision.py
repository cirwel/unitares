"""Live-DB contracts for migration 078's resolution-saga generation fence."""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    import asyncpg
except ImportError:
    pytest.skip("asyncpg not installed", allow_module_level=True)

from src.dialectic_db import DialecticDB
from tests.test_db_utils import (
    TEST_DB_URL,
    can_connect_to_test_db,
    ensure_test_database_schema,
)

if not can_connect_to_test_db():
    pytest.skip("governance_test database not available", allow_module_level=True)


async def _insert_session(conn) -> str:
    session_id = f"test_saga_fence_{uuid.uuid4().hex}"
    await conn.execute(
        """
        INSERT INTO core.dialectic_sessions (
            session_id, paused_agent_id, reviewer_agent_id, phase, status,
            awaiting_facilitation
        ) VALUES ($1, 'paused-test', 'reviewer-test', 'synthesis', 'active', false)
        """,
        session_id,
    )
    return session_id


async def _reserve_after_read(conn, session_id: str) -> int:
    row = await conn.fetchrow(
        """
        UPDATE core.dialectic_sessions
        SET resolution_saga_revision = resolution_saga_revision + 1
        WHERE session_id = $1
        RETURNING resolution_saga_revision, updated_at
        """,
        session_id,
    )
    revision = int(row["resolution_saga_revision"])
    await conn.execute(
        """
        INSERT INTO coordination.session_resolution_sagas (
            saga_id, session_id, paused_agent_id, reviewer_agent_id, state,
            resolution_payload_json, resolution_payload_hash,
            claim_revision, claim_updated_at, attempt_count
        ) VALUES (
            gen_random_uuid(), $1, 'paused-test', 'reviewer-test', 'reserved',
            '{"action":"failed"}'::jsonb, $2, $3, $4, 1
        )
        """,
        session_id,
        f"fence-{uuid.uuid4().hex}",
        revision,
        row["updated_at"],
    )
    return revision


async def _cleanup(conn, session_id: str) -> None:
    await conn.execute(
        "DELETE FROM coordination.session_resolution_sagas WHERE session_id = $1",
        session_id,
    )
    await conn.execute(
        "DELETE FROM core.dialectic_sessions WHERE session_id = $1", session_id
    )


@pytest.mark.asyncio
async def test_migration_078_adds_nonnegative_generation_columns_and_drops_permanent_dedup():
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    session_id = await _insert_session(conn)
    try:
        assert await conn.fetchval(
            "SELECT resolution_saga_revision FROM core.dialectic_sessions WHERE session_id = $1",
            session_id,
        ) == 0
        assert await conn.fetchval(
            """
            SELECT count(*)
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = 'core'
              AND t.relname = 'dialectic_sessions'
              AND c.contype = 'c'
              AND pg_get_constraintdef(c.oid) LIKE '%resolution_saga_revision%'
            """
        ) == 1
        assert await conn.fetchval(
            """
            SELECT count(*)
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = 'coordination'
              AND t.relname = 'session_resolution_sagas'
              AND c.contype = 'u'
              AND pg_get_constraintdef(c.oid) LIKE '%session_id%resolution_payload_hash%'
            """
        ) == 0
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE core.dialectic_sessions SET resolution_saga_revision = -1 WHERE session_id = $1",
                session_id,
            )
    finally:
        await _cleanup(conn, session_id)
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ["reviewer_reassignment", "facilitation_mark", "rejection_close", "idle_reap"],
)
async def test_claim_after_sweeper_read_refuses_every_maintenance_mutation(mutation):
    """The BEAM claim wins even when the sweeper's early probe saw no saga."""
    await ensure_test_database_schema()
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
    db = DialecticDB(pool)
    async with pool.acquire() as conn:
        session_id = await _insert_session(conn)
        snapshot = await conn.fetchrow(
            """
            SELECT phase, updated_at, reviewer_agent_id, transcript_revision,
                   awaiting_facilitation, resolution_saga_revision
            FROM core.dialectic_sessions WHERE session_id = $1
            """,
            session_id,
        )
        await _reserve_after_read(conn, session_id)

    common = {
        "expected_phase": snapshot["phase"],
        "expected_updated_at": snapshot["updated_at"],
        "expected_reviewer": snapshot["reviewer_agent_id"],
        "expected_transcript_revision": snapshot["transcript_revision"],
        "expected_resolution_saga_revision": snapshot[
            "resolution_saga_revision"
        ],
        "exclude_inflight_saga": True,
    }
    try:
        if mutation == "reviewer_reassignment":
            written = await db.update_session_reviewer(
                session_id,
                "replacement-reviewer",
                expected_awaiting_facilitation=False,
                clear_facilitation=False,
                **common,
            )
        elif mutation == "facilitation_mark":
            written = await db.mark_awaiting_facilitation(session_id, **common)
        else:
            written = await db.resolve_session(
                session_id,
                {
                    "reason": (
                        "reviewer_rejection_stands"
                        if mutation == "rejection_close"
                        else "inactivity_timeout"
                    )
                },
                "failed",
                expected_awaiting_facilitation=False,
                clear_facilitation=True,
                **common,
            )

        assert written is False
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT status, phase, reviewer_agent_id, awaiting_facilitation,
                       resolution_json
                FROM core.dialectic_sessions WHERE session_id = $1
                """,
                session_id,
            )
            assert dict(row) == {
                "status": "active",
                "phase": "synthesis",
                "reviewer_agent_id": "reviewer-test",
                "awaiting_facilitation": False,
                "resolution_json": None,
            }
    finally:
        async with pool.acquire() as conn:
            await _cleanup(conn, session_id)
        await pool.close()


@pytest.mark.asyncio
async def test_inflight_guard_refuses_even_when_caller_has_current_generation():
    """The NOT EXISTS guard closes the gap independently of revision CAS."""
    await ensure_test_database_schema()
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=2)
    db = DialecticDB(pool)
    async with pool.acquire() as conn:
        session_id = await _insert_session(conn)
        revision = await _reserve_after_read(conn, session_id)
        current = await conn.fetchrow(
            """
            SELECT phase, updated_at, reviewer_agent_id, transcript_revision
            FROM core.dialectic_sessions WHERE session_id = $1
            """,
            session_id,
        )
    try:
        assert await db.mark_awaiting_facilitation(
            session_id,
            expected_phase=current["phase"],
            expected_updated_at=current["updated_at"],
            expected_reviewer=current["reviewer_agent_id"],
            expected_transcript_revision=current["transcript_revision"],
            expected_resolution_saga_revision=revision,
            exclude_inflight_saga=True,
        ) is False
    finally:
        async with pool.acquire() as conn:
            await _cleanup(conn, session_id)
        await pool.close()


@pytest.mark.asyncio
async def test_claim_commit_after_sweeper_snapshot_still_refuses_blocked_update():
    """The revision CAS survives PostgreSQL's row-lock/EPQ interleaving.

    The maintenance UPDATE takes its statement snapshot while the claim is
    uncommitted, so its NOT EXISTS subquery cannot see the saga.  It then waits
    on the session row.  After the claim commits, PostgreSQL must recheck the
    row predicate and reject the stale resolution_saga_revision.
    """
    await ensure_test_database_schema()
    claimant = await asyncpg.connect(TEST_DB_URL)
    sweeper = await asyncpg.connect(TEST_DB_URL)
    observer = await asyncpg.connect(TEST_DB_URL)
    sweeper_pid = await sweeper.fetchval("SELECT pg_backend_pid()")
    session_id = await _insert_session(claimant)
    tx = claimant.transaction()
    committed = False
    try:
        await tx.start()
        assert await _reserve_after_read(claimant, session_id) == 1

        maintenance = asyncio.create_task(
            sweeper.fetchval(
                """
                UPDATE core.dialectic_sessions AS s
                SET awaiting_facilitation = true,
                    updated_at = NOW()
                WHERE s.session_id = $1
                  AND s.status NOT IN ('resolved', 'failed')
                  AND s.phase = 'synthesis'
                  AND s.reviewer_agent_id = 'reviewer-test'
                  AND s.transcript_revision = 0
                  AND s.resolution_saga_revision = 0
                  AND NOT EXISTS (
                      SELECT 1
                      FROM coordination.session_resolution_sagas saga
                      WHERE saga.session_id = s.session_id
                        AND saga.state IN (
                            'reserved', 'paused_agent_applied',
                            'both_agents_applied', 'reverting'
                        )
                  )
                RETURNING s.session_id
                """,
                session_id,
            )
        )
        for _ in range(100):
            activity = await observer.fetchrow(
                """
                SELECT state, wait_event_type
                FROM pg_stat_activity WHERE pid = $1
                """,
                sweeper_pid,
            )
            if activity and activity["wait_event_type"] == "Lock":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail(
                "maintenance UPDATE never reached the claim's row lock"
            )
        assert activity["state"] == "active"
        assert not maintenance.done()

        await tx.commit()
        committed = True
        assert await maintenance is None

        row = await sweeper.fetchrow(
            """
            SELECT awaiting_facilitation, resolution_saga_revision
            FROM core.dialectic_sessions WHERE session_id = $1
            """,
            session_id,
        )
        assert dict(row) == {
            "awaiting_facilitation": False,
            "resolution_saga_revision": 1,
        }
    finally:
        if not committed:  # pragma: no cover - failure cleanup
            await tx.rollback()
        await _cleanup(claimant, session_id)
        await claimant.close()
        await sweeper.close()
        await observer.close()
