"""Migration 084 contract tests: persisted lease principals (holder_issuer, holder_subject).

Covers:
  - Static registration and SQL definitions (always runs without DB).
  - Live PostgreSQL CHECK constraint `surface_leases_holder_principal_pair_coherent`
    and `enforce_immutable_lease_fields()` immutability trigger preserving migration 029's
    `earned_status` guard while locking `holder_issuer` and `holder_subject`.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

MIGRATION_PATH = project_root / "db/postgres/migrations/084_lease_principal_issuer_subject.sql"


def test_migration_084_static_contract() -> None:
    sql = MIGRATION_PATH.read_text()
    assert "ADD COLUMN IF NOT EXISTS holder_issuer" in sql
    assert "ADD COLUMN IF NOT EXISTS holder_subject" in sql
    assert "surface_leases_holder_principal_pair_coherent" in sql
    assert "idx_surface_leases_active_principal" in sql
    assert "enforce_immutable_lease_fields()" in sql
    assert "NEW.holder_issuer IS DISTINCT FROM OLD.holder_issuer" in sql
    assert "NEW.holder_subject IS DISTINCT FROM OLD.holder_subject" in sql
    assert "NEW.earned_status IS DISTINCT FROM OLD.earned_status" in sql
    assert "VALUES (84, 'lease_principal_issuer_subject'" in sql


async def _seed_lease(
    conn,
    surface_id: str,
    *,
    holder_issuer: str | None = None,
    holder_subject: str | None = None,
) -> uuid.UUID:
    lease_id = uuid.uuid4()
    holder_uuid = uuid.uuid4()
    now = datetime.now(UTC)
    await conn.execute(
        """
        INSERT INTO lease_plane.surface_leases
          (lease_id, surface_id, holder_agent_uuid, holder_issuer, holder_subject,
           holder_kind, holder_class, heartbeat_required, original_ttl_s, acquired_at, expires_at)
        VALUES ($1, $2, $3, $4, $5, 'remote_heartbeat', 'process_instance', true, 60, $6, $7)
        """,
        lease_id,
        surface_id,
        holder_uuid,
        holder_issuer,
        holder_subject,
        now,
        now + timedelta(seconds=60),
    )
    return lease_id


@pytest.mark.asyncio
async def test_migration_084_principal_check_and_immutability_trigger() -> None:
    try:
        import asyncpg
    except ImportError:
        pytest.skip("asyncpg not installed")

    from tests.test_db_utils import (
        TEST_DB_URL,
        can_connect_to_test_db,
        ensure_test_database_schema,
    )

    if not can_connect_to_test_db():
        pytest.skip("governance_test database not available")

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    sid_unattested = f"td:/m084_unattested_{uuid.uuid4().hex[:8]}"
    sid_attested = f"td:/m084_attested_{uuid.uuid4().hex[:8]}"
    sid_bad = f"td:/m084_bad_{uuid.uuid4().hex[:8]}"
    try:
        # Both NULL is valid (legacy/unattested)
        unattested_id = await _seed_lease(conn, sid_unattested)

        # Both set is valid (attested)
        attested_id = await _seed_lease(
            conn, sid_attested, holder_issuer="operator-a", holder_subject="subject-1"
        )

        # Half-set, empty, whitespace, control characters, and >256-char values fail CHECK
        for bad_issuer, bad_subject in [
            ("operator-a", None),
            (None, "subject-1"),
            ("", "subject-1"),
            ("operator-a", ""),
            ("operator a", "subject-1"),
            ("operator-a", "subject\t1"),
            ("operator\x1ca", "subject-1"),
            ("operator-a", "subject\x071"),
            ("operator\x7fa", "subject-1"),
            ("operator-a", "subject\u00851"),
            ("operator\u00a0a", "subject-1"),
            ("a" * 257, "subject-1"),
            ("operator-a", "s" * 257),
        ]:
            with pytest.raises(asyncpg.CheckViolationError):
                await _seed_lease(
                    conn, sid_bad, holder_issuer=bad_issuer, holder_subject=bad_subject
                )

        # Immutability trigger blocks changing holder_issuer or holder_subject
        # (including NULL -> non-NULL, non-NULL -> other, and non-NULL -> NULL)
        with pytest.raises(asyncpg.RaiseError) as exc_info:
            await conn.execute(
                "UPDATE lease_plane.surface_leases "
                "SET holder_issuer = 'operator-a', holder_subject = 'subject-1' "
                "WHERE lease_id = $1",
                unattested_id,
            )
        assert "holder_issuer" in str(exc_info.value)

        with pytest.raises(asyncpg.RaiseError) as exc_info:
            await conn.execute(
                "UPDATE lease_plane.surface_leases SET holder_issuer = 'operator-b' WHERE lease_id = $1",
                attested_id,
            )
        assert "holder_issuer" in str(exc_info.value)

        with pytest.raises(asyncpg.RaiseError) as exc_info:
            await conn.execute(
                "UPDATE lease_plane.surface_leases SET holder_subject = 'subject-2' WHERE lease_id = $1",
                attested_id,
            )
        assert "holder_subject" in str(exc_info.value)

        # Migration 029's earned_status guard must remain intact
        with pytest.raises(asyncpg.RaiseError) as exc_info:
            await conn.execute(
                "UPDATE lease_plane.surface_leases SET earned_status = 'earned' WHERE lease_id = $1",
                attested_id,
            )
        assert "earned_status" in str(exc_info.value)

        # Normal lifecycle updates (release) still succeed
        await conn.execute(
            "UPDATE lease_plane.surface_leases SET released_at = now(), release_reason = 'normal' "
            "WHERE lease_id = $1",
            attested_id,
        )
        released_at = await conn.fetchval(
            "SELECT released_at FROM lease_plane.surface_leases WHERE lease_id = $1",
            attested_id,
        )
        assert released_at is not None
    finally:
        await conn.execute(
            "DELETE FROM lease_plane.surface_leases WHERE surface_id = ANY($1::text[])",
            [sid_unattested, sid_attested, sid_bad],
        )
        await conn.close()
