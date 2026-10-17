"""Live-DB contract tests for migration 077's transcript frontier."""

from __future__ import annotations

import asyncio
import json
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

from tests.test_db_utils import (
    TEST_DB_URL,
    can_connect_to_test_db,
    ensure_test_database_schema,
)
from src.dialectic_db import DialecticDB

if not can_connect_to_test_db():
    pytest.skip("governance_test database not available", allow_module_level=True)


async def _insert_session(conn) -> str:
    session_id = f"test_revision_{uuid.uuid4().hex}"
    await conn.execute(
        """
        INSERT INTO core.dialectic_sessions (
            session_id, paused_agent_id, reviewer_agent_id, phase, status
        ) VALUES ($1, 'paused-test', 'reviewer-test', 'synthesis', 'active')
        """,
        session_id,
    )
    return session_id


async def _cleanup(conn, session_id: str) -> None:
    await conn.execute(
        "DELETE FROM core.dialectic_sessions WHERE session_id = $1", session_id
    )


@pytest.mark.asyncio
async def test_migration_077_adds_nonnegative_transcript_frontier():
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    session_id = await _insert_session(conn)
    try:
        assert await conn.fetchval(
            "SELECT transcript_revision FROM core.dialectic_sessions WHERE session_id = $1",
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
              AND pg_get_constraintdef(c.oid) LIKE '%transcript_revision%'
            """
        ) == 1
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE core.dialectic_sessions SET transcript_revision = -1 WHERE session_id = $1",
                session_id,
            )
    finally:
        await _cleanup(conn, session_id)
        await conn.close()


@pytest.mark.asyncio
async def test_message_commit_wins_row_lock_and_refuses_stale_terminal_writer():
    """A terminal decision cannot omit a message committed from its old frontier."""
    await ensure_test_database_schema()
    writer = await asyncpg.connect(TEST_DB_URL)
    terminal = await asyncpg.connect(TEST_DB_URL)
    session_id = await _insert_session(writer)
    tx = writer.transaction()
    committed = False
    try:
        await tx.start()
        assert await writer.fetchval(
            """
            UPDATE core.dialectic_sessions
            SET transcript_revision = transcript_revision + 1
            WHERE session_id = $1 AND status = 'active' AND phase = 'synthesis'
              AND reviewer_agent_id = 'reviewer-test' AND transcript_revision = 0
            RETURNING transcript_revision
            """,
            session_id,
        ) == 1
        await writer.execute(
            """
            INSERT INTO core.dialectic_messages
                (session_id, agent_id, message_type, reasoning)
            VALUES ($1, 'reviewer-test', 'synthesis', 'new committed evidence')
            """,
            session_id,
        )

        terminal_attempt = asyncio.create_task(
            terminal.fetchval(
                """
                UPDATE core.dialectic_sessions
                SET status = 'resolved', phase = 'resolved'
                WHERE session_id = $1 AND status NOT IN ('resolved', 'failed')
                  AND phase = 'synthesis' AND reviewer_agent_id = 'reviewer-test'
                  AND transcript_revision = 0
                RETURNING session_id
                """,
                session_id,
            )
        )
        await asyncio.sleep(0.05)
        assert not terminal_attempt.done(), "terminal writer must wait on the row lock"
        await tx.commit()
        committed = True

        assert await terminal_attempt is None
        row = await terminal.fetchrow(
            """
            SELECT status, phase, transcript_revision,
                   (SELECT count(*) FROM core.dialectic_messages m
                    WHERE m.session_id = s.session_id) AS message_count
            FROM core.dialectic_sessions s WHERE session_id = $1
            """,
            session_id,
        )
        assert dict(row) == {
            "status": "active",
            "phase": "synthesis",
            "transcript_revision": 1,
            "message_count": 1,
        }
    finally:
        if not committed:  # pragma: no cover - failure cleanup
            await tx.rollback()
        await _cleanup(writer, session_id)
        await writer.close()
        await terminal.close()


@pytest.mark.asyncio
async def test_terminal_commit_refuses_late_message_append():
    """Once terminal, a stale protocol append cannot create a trailing message."""
    await ensure_test_database_schema()
    terminal = await asyncpg.connect(TEST_DB_URL)
    writer = await asyncpg.connect(TEST_DB_URL)
    session_id = await _insert_session(terminal)
    try:
        assert await terminal.fetchval(
            """
            UPDATE core.dialectic_sessions
            SET status = 'resolved', phase = 'resolved'
            WHERE session_id = $1 AND status = 'active'
              AND phase = 'synthesis' AND transcript_revision = 0
            RETURNING session_id
            """,
            session_id,
        ) == session_id

        bumped = await writer.fetchval(
            """
            UPDATE core.dialectic_sessions
            SET transcript_revision = transcript_revision + 1
            WHERE session_id = $1 AND status NOT IN ('resolved', 'failed')
              AND phase = 'synthesis' AND transcript_revision = 0
            RETURNING transcript_revision
            """,
            session_id,
        )
        assert bumped is None
        assert await writer.fetchval(
            "SELECT count(*) FROM core.dialectic_messages WHERE session_id = $1",
            session_id,
        ) == 0
    finally:
        await _cleanup(terminal, session_id)
        await terminal.close()
        await writer.close()


@pytest.mark.asyncio
async def test_message_and_phase_advance_refuse_a_duplicate_old_phase():
    """A second thesis cannot observe the first message with phase still thesis."""
    await ensure_test_database_schema()
    first = await asyncpg.connect(TEST_DB_URL)
    second = await asyncpg.connect(TEST_DB_URL)
    session_id = await _insert_session(first)
    await first.execute(
        "UPDATE core.dialectic_sessions SET phase = 'thesis' WHERE session_id = $1",
        session_id,
    )
    tx = first.transaction()
    committed = False
    try:
        await tx.start()
        assert await first.fetchval(
            """
            UPDATE core.dialectic_sessions
            SET transcript_revision = transcript_revision + 1,
                phase = 'antithesis'
            WHERE session_id = $1 AND status = 'active'
              AND phase = 'thesis' AND transcript_revision = 0
            RETURNING transcript_revision
            """,
            session_id,
        ) == 1
        await first.execute(
            """
            INSERT INTO core.dialectic_messages
                (session_id, agent_id, message_type, reasoning)
            VALUES ($1, 'paused-test', 'thesis', 'first thesis')
            """,
            session_id,
        )

        duplicate = asyncio.create_task(
            second.fetchval(
                """
                UPDATE core.dialectic_sessions
                SET transcript_revision = transcript_revision + 1,
                    phase = 'antithesis'
                WHERE session_id = $1 AND status = 'active'
                  AND phase = 'thesis' AND transcript_revision = 0
                RETURNING transcript_revision
                """,
                session_id,
            )
        )
        await asyncio.sleep(0.05)
        assert not duplicate.done(), "the duplicate must wait on the session row"
        await tx.commit()
        committed = True

        assert await duplicate is None
        row = await second.fetchrow(
            """
            SELECT phase, transcript_revision,
                   (SELECT count(*) FROM core.dialectic_messages m
                    WHERE m.session_id = s.session_id) AS message_count
            FROM core.dialectic_sessions s WHERE session_id = $1
            """,
            session_id,
        )
        assert dict(row) == {
            "phase": "antithesis",
            "transcript_revision": 1,
            "message_count": 1,
        }
    finally:
        if not committed:  # pragma: no cover - failure cleanup
            await tx.rollback()
        await _cleanup(first, session_id)
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_two_first_responders_assign_reviewer_with_their_verdict_atomically():
    """Exactly one open-slot reviewer and its antithesis commit together."""
    await ensure_test_database_schema()
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=2, max_size=3)
    db = DialecticDB(pool)
    async with pool.acquire() as conn:
        session_id = await _insert_session(conn)
        await conn.execute(
            """
            UPDATE core.dialectic_sessions
            SET phase = 'antithesis', reviewer_agent_id = NULL
            WHERE session_id = $1
            """,
            session_id,
        )

    async def submit(reviewer: str) -> int:
        return await db.add_message(
            session_id,
            reviewer,
            "antithesis",
            reasoning=f"verdict from {reviewer}",
            expected_phase="antithesis",
            expected_reviewer=None,
            expected_transcript_revision=0,
            expected_awaiting_facilitation=False,
            next_phase="synthesis",
            next_synthesis_round=1,
            next_reviewer=reviewer,
        )

    try:
        results = await asyncio.gather(submit("reviewer-a"), submit("reviewer-b"))
        assert sum(bool(result) for result in results) == 1
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT reviewer_agent_id, phase, transcript_revision,
                       (SELECT count(*) FROM core.dialectic_messages m
                        WHERE m.session_id = s.session_id) AS message_count
                FROM core.dialectic_sessions s WHERE session_id = $1
                """,
                session_id,
            )
            assert row["reviewer_agent_id"] in {"reviewer-a", "reviewer-b"}
            assert row["phase"] == "synthesis"
            assert row["transcript_revision"] == 1
            assert row["message_count"] == 1
    finally:
        async with pool.acquire() as conn:
            await _cleanup(conn, session_id)
        await pool.close()


@pytest.mark.asyncio
async def test_first_responder_authorship_guard_refuses_verdict_and_assignment():
    """A declared author cannot atomically claim the independent-review slot."""
    await ensure_test_database_schema()
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=2)
    db = DialecticDB(pool)
    proxy = "11111111-1111-4111-8111-111111111111"
    author = "22222222-2222-4222-8222-222222222222"
    session_id = f"test_revision_{uuid.uuid4().hex}"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO core.dialectic_sessions (
                session_id, paused_agent_id, reviewer_agent_id, phase, status,
                paused_agent_state_json
            ) VALUES ($1, $2, NULL, 'antithesis', 'active', $3::jsonb)
            """,
            session_id,
            proxy,
            json.dumps({
                "_dialectic_reviewed_work_authorship": {
                    "schema": "unitares.dialectic-reviewed-work-authorship.v1",
                    "status": "declared",
                    "reviewed_work_author_agent_ids": [author],
                    "declared_by_agent_id": proxy,
                    "authorship_verified": False,
                }
            }),
        )
    try:
        assert await db.add_message(
            session_id,
            author,
            "antithesis",
            reasoning="self-authored verdict",
            expected_phase="antithesis",
            expected_reviewer=None,
            expected_transcript_revision=0,
            expected_awaiting_facilitation=False,
            next_phase="synthesis",
            next_synthesis_round=1,
            next_reviewer=author,
        ) == 0
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT reviewer_agent_id, phase, transcript_revision,
                       (SELECT count(*) FROM core.dialectic_messages m
                        WHERE m.session_id = s.session_id) AS message_count
                FROM core.dialectic_sessions s WHERE session_id = $1
                """,
                session_id,
            )
            assert dict(row) == {
                "reviewer_agent_id": None,
                "phase": "antithesis",
                "transcript_revision": 0,
                "message_count": 0,
            }
    finally:
        async with pool.acquire() as conn:
            await _cleanup(conn, session_id)
        await pool.close()


@pytest.mark.asyncio
async def test_message_lock_first_refuses_stale_facilitation_mark():
    """A progress message wins the row lock and a stale flag cannot follow it."""
    await ensure_test_database_schema()
    writer = await asyncpg.connect(TEST_DB_URL)
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=2)
    db = DialecticDB(pool)
    session_id = await _insert_session(writer)
    observed = await writer.fetchrow(
        """
        SELECT phase, updated_at, reviewer_agent_id, transcript_revision
        FROM core.dialectic_sessions WHERE session_id = $1
        """,
        session_id,
    )
    tx = writer.transaction()
    committed = False
    try:
        await tx.start()
        assert await writer.fetchval(
            """
            UPDATE core.dialectic_sessions
            SET transcript_revision = transcript_revision + 1, updated_at = now()
            WHERE session_id = $1 AND transcript_revision = 0
            RETURNING transcript_revision
            """,
            session_id,
        ) == 1
        await writer.execute(
            """
            INSERT INTO core.dialectic_messages
                (session_id, agent_id, message_type, reasoning)
            VALUES ($1, 'reviewer-test', 'synthesis', 'new activity')
            """,
            session_id,
        )

        marker = asyncio.create_task(
            db.mark_awaiting_facilitation(
                session_id,
                expected_phase=observed["phase"],
                expected_updated_at=observed["updated_at"],
                expected_reviewer=observed["reviewer_agent_id"],
                expected_transcript_revision=observed["transcript_revision"],
            )
        )
        await asyncio.sleep(0.05)
        assert not marker.done(), "the marker must wait on the message row lock"
        await tx.commit()
        committed = True

        assert await marker is False
        assert await writer.fetchval(
            "SELECT awaiting_facilitation FROM core.dialectic_sessions WHERE session_id = $1",
            session_id,
        ) is False
    finally:
        if not committed:  # pragma: no cover - failure cleanup
            await tx.rollback()
        await _cleanup(writer, session_id)
        await writer.close()
        await pool.close()


@pytest.mark.asyncio
async def test_facilitation_mark_first_refuses_stale_message_append():
    """A newly raised request cannot be erased by an append from an old read."""
    await ensure_test_database_schema()
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=2)
    db = DialecticDB(pool)
    async with pool.acquire() as conn:
        session_id = await _insert_session(conn)
    try:
        assert await db.mark_awaiting_facilitation(
            session_id,
            expected_phase="synthesis",
            expected_reviewer="reviewer-test",
            expected_transcript_revision=0,
            touch_updated_at=True,
        ) is True
        assert await db.add_message(
            session_id,
            "reviewer-test",
            "synthesis",
            reasoning="stale answer",
            expected_phase="synthesis",
            expected_reviewer="reviewer-test",
            expected_transcript_revision=0,
            expected_awaiting_facilitation=False,
            next_phase="synthesis",
            next_synthesis_round=1,
            next_awaiting_facilitation=False,
        ) == 0
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT awaiting_facilitation FROM core.dialectic_sessions WHERE session_id = $1",
                session_id,
            ) is True
            assert await conn.fetchval(
                "SELECT count(*) FROM core.dialectic_messages WHERE session_id = $1",
                session_id,
            ) == 0
    finally:
        async with pool.acquire() as conn:
            await _cleanup(conn, session_id)
        await pool.close()
