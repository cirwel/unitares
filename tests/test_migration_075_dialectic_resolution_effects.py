"""Migration 075: terminal decisions durably enqueue runtime effects."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from tests.test_db_utils import (
    TEST_DB_URL,
    can_connect_to_test_db,
    ensure_test_database_schema,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "db/postgres/migrations/075_dialectic_resolution_effects.sql"


def _resolution():
    return {
        "action": "resume",
        "conditions": ["observe"],
        "root_cause": "independent review",
        "reasoning": "the parties agreed",
        "signature_a": "",
        "signature_b": "",
        "timestamp": "2026-10-16T00:00:00+00:00",
        "signature_version": 3,
    }


def test_migration_is_passive_during_schema_before_code_deploy():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS coordination.dialectic_resolution_effects" in sql
    assert "CREATE TRIGGER" not in sql
    assert "enqueue_dialectic_resolution_effect" not in sql
    assert "effect_required" in sql
    assert "pause_token" in sql
    assert "unknown_partial" in sql
    assert "VALUES (75, 'dialectic_resolution_effects'" in sql


def test_migration_does_not_backfill_legacy_resolutions():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "insert into coordination.dialectic_resolution_effects\n        select" not in sql
    assert "create trigger" not in sql


@pytest.mark.asyncio
async def test_migration_stays_passive_and_rejects_malformed_effect_rows():
    if not can_connect_to_test_db():
        pytest.skip("governance_test database not available")

    import asyncpg

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    current_shape = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'coordination'
              AND table_name = 'dialectic_resolution_effects'
              AND column_name = 'effect_required'
        ) AND NOT EXISTS (
            SELECT 1 FROM pg_trigger
            WHERE tgrelid = 'core.dialectic_sessions'::regclass
              AND tgname = 'dialectic_resolution_effect_enqueue'
              AND NOT tgisinternal
        )
        """
    )
    if not current_shape:
        await conn.close()
        pytest.skip(
            "shared governance_test has an earlier migration-075 draft; "
            "fresh CI validates the final passive schema"
        )
    outer = conn.transaction()
    await outer.start()
    try:
        first = f"m075-{uuid4()}"
        resolution = _resolution()
        await conn.execute(
            """
            INSERT INTO core.dialectic_sessions (
                session_id, paused_agent_id, reviewer_agent_id, phase, status,
                paused_agent_state_json
            ) VALUES (
                $1, 'paused-a', 'reviewer-b', 'synthesis', 'active',
                '{"pause_effect_required": false, "pause_token": null}'::jsonb
            )
            """,
            first,
        )
        await conn.execute(
            """
            UPDATE core.dialectic_sessions
               SET phase = 'resolved', status = 'resolved',
                   resolution_json = ($2::text)::jsonb
             WHERE session_id = $1
            """,
            first,
            json.dumps(resolution),
        )
        assert await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM coordination.dialectic_resolution_effects "
            "WHERE session_id = $1)",
            first,
        ) is False

        for malformed in ({}, {"action": "resume"}):
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO coordination.dialectic_resolution_effects (
                            session_id, paused_agent_id, reviewer_agent_id,
                            effect_required, pause_token, resolution_payload_json
                        ) VALUES (
                            $1, 'paused-a', 'reviewer-b', false, NULL,
                            ($2::text)::jsonb
                        )
                        """,
                        first,
                        json.dumps(malformed),
                    )

        legacy = f"m075-{uuid4()}"
        await conn.execute(
            """
            INSERT INTO core.dialectic_sessions (
                session_id, paused_agent_id, reviewer_agent_id, phase, status,
                resolution_json
            ) VALUES (
                $1, 'paused-a', 'reviewer-b', 'resolved', 'resolved',
                ($2::text)::jsonb
            )
            """,
            legacy,
            json.dumps(resolution),
        )
        assert await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM coordination.dialectic_resolution_effects "
            "WHERE session_id = $1)",
            legacy,
        ) is False
    finally:
        await outer.rollback()
        await conn.close()
