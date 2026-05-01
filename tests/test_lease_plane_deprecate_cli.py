"""
Phase A PR 3a — deprecation CLI for surface-lease-plane.

Tests the `scripts/dev/lease_plane_deprecate.py` CLI which implements the
4-phase operator-driven scheme deprecation procedure (RFC v0.8 §7.11.2):

  Phase 0 (`deprecate`)              — mark scheme deprecated, INSERT into deprecated_schemes
  Phase 1 (automatic verification)   — unitares_doctor lint (out of CLI scope)
  Phase 2 (`deprecation-sweep`)      — force-release surviving leases, idempotent predicate
  Phase 3 (`deprecation-finalize`)   — extend grammar CHECK, atomic with sweep

Spec: docs/proposals/surface-lease-plane-v0.md §7.11
      docs/proposals/surface-lease-plane-phase-a-plan.md PR 3a
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
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

if not can_connect_to_test_db():
    pytest.skip("governance_test database not available", allow_module_level=True)


@pytest.fixture(autouse=True)
def _set_force_release_token(monkeypatch):
    """Provide LEASE_FORCE_RELEASE_TOKEN for Phase 2 sweep authorization (RFC §7.10)."""
    monkeypatch.setenv("LEASE_FORCE_RELEASE_TOKEN", "test-force-release-token-do-not-use-in-prod")


async def _cleanup(conn):
    """Reset deprecated_schemes + surface_leases between tests."""
    await conn.execute(
        "DELETE FROM lease_plane.deprecated_schemes WHERE marked_by_session_id LIKE 'test-cli-%'"
    )
    await conn.execute(
        "DELETE FROM lease_plane.surface_leases WHERE intent LIKE 'pr3a-cli-test%'"
    )
    await conn.execute(
        "DELETE FROM lease_plane.lease_plane_events WHERE surface_id LIKE 'td:/pr3a%' "
        "OR surface_id LIKE 'capture:/pr3a%'"
    )


async def _seed_lease(conn, surface_id: str, ttl_s: int = 60) -> uuid.UUID:
    """Insert an active lease to be swept."""
    lease_id = uuid.uuid4()
    holder_uuid = uuid.uuid4()
    now = datetime.now(UTC)
    await conn.execute(
        """
        INSERT INTO lease_plane.surface_leases
          (lease_id, surface_id, holder_agent_uuid, holder_kind, holder_class,
           heartbeat_required, original_ttl_s, acquired_at, expires_at, intent)
        VALUES ($1, $2, $3, 'remote_heartbeat', 'process_instance', true, $4, $5, $6, $7)
        """,
        lease_id, surface_id, holder_uuid, ttl_s, now,
        now + timedelta(seconds=ttl_s), "pr3a-cli-test-seed",
    )
    return lease_id


@pytest.mark.asyncio
async def test_deprecate_cli_phase_0_inserts_row():
    """`deprecate <kind>` writes a row to lease_plane.deprecated_schemes."""
    from scripts.dev import lease_plane_deprecate as cli

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        rc = await cli.deprecate_cmd(
            kind="td", session_id="test-cli-phase-0", drain_window_days=7,
            db_url=TEST_DB_URL,
        )
        assert rc == 0
        rows = await conn.fetch(
            "SELECT surface_kind, drain_window_days, marked_by_session_id "
            "FROM lease_plane.deprecated_schemes WHERE surface_kind = 'td'"
        )
        assert len(rows) == 1
        assert rows[0]["surface_kind"] == "td"
        assert rows[0]["drain_window_days"] == 7
        assert rows[0]["marked_by_session_id"] == "test-cli-phase-0"
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_deprecate_cli_idempotent_no_duplicate_row():
    """Re-marking an already-deprecated scheme returns 0 with no duplicate row."""
    from scripts.dev import lease_plane_deprecate as cli

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        rc1 = await cli.deprecate_cmd(
            kind="td", session_id="test-cli-idem", drain_window_days=30,
            db_url=TEST_DB_URL,
        )
        rc2 = await cli.deprecate_cmd(
            kind="td", session_id="test-cli-idem", drain_window_days=30,
            db_url=TEST_DB_URL,
        )
        assert rc1 == 0 and rc2 == 0
        count = await conn.fetchval(
            "SELECT count(*) FROM lease_plane.deprecated_schemes WHERE surface_kind = 'td'"
        )
        assert count == 1
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_deprecate_cli_unknown_kind_rejected():
    """Marking a scheme not in surface_kind_catalog returns nonzero rc."""
    from scripts.dev import lease_plane_deprecate as cli

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        rc = await cli.deprecate_cmd(
            kind="not_a_real_scheme",
            session_id="test-cli-unknown",
            drain_window_days=30,
            db_url=TEST_DB_URL,
        )
        assert rc != 0, "Unknown scheme should not succeed"
        count = await conn.fetchval(
            "SELECT count(*) FROM lease_plane.deprecated_schemes "
            "WHERE marked_by_session_id = 'test-cli-unknown'"
        )
        assert count == 0
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_deprecation_sweep_predicate_idempotent():
    """RFC §7.11.4: sweep predicate `WHERE released_at IS NULL AND surface_kind = $1`
    reaches fixpoint on re-run after partial completion."""
    from scripts.dev import lease_plane_deprecate as cli

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        # Seed 3 active leases on td:/ scheme.
        await _seed_lease(conn, "td:/pr3a_a")
        await _seed_lease(conn, "td:/pr3a_b")
        await _seed_lease(conn, "td:/pr3a_c")
        # Mark deprecated.
        await cli.deprecate_cmd(
            kind="td", session_id="test-cli-sweep-idem", drain_window_days=30,
            db_url=TEST_DB_URL,
        )
        # First sweep: releases all 3.
        rc1 = await cli.deprecation_sweep_cmd(kind="td", db_url=TEST_DB_URL)
        assert rc1 == 0
        unreleased1 = await conn.fetchval(
            "SELECT count(*) FROM lease_plane.surface_leases "
            "WHERE surface_kind = 'td' AND released_at IS NULL"
        )
        assert unreleased1 == 0
        # Re-run: idempotent fixpoint (zero rows to release).
        rc2 = await cli.deprecation_sweep_cmd(kind="td", db_url=TEST_DB_URL)
        assert rc2 == 0
        # Verify all 3 leases have release_reason='forced'.
        forced_count = await conn.fetchval(
            "SELECT count(*) FROM lease_plane.surface_leases "
            "WHERE surface_kind = 'td' AND release_reason = 'forced'"
        )
        assert forced_count == 3
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_deprecation_sweep_emits_lease_deprecation_swept_events():
    """RFC §7.11.3: each swept lease emits an event_type='lease.deprecation_swept'
    row in lease_plane_events with the deprecation_id for audit correlation."""
    from scripts.dev import lease_plane_deprecate as cli

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        await _seed_lease(conn, "td:/pr3a_event_a")
        await _seed_lease(conn, "td:/pr3a_event_b")
        await cli.deprecate_cmd(
            kind="td", session_id="test-cli-events", drain_window_days=30,
            db_url=TEST_DB_URL,
        )
        depr_id = await conn.fetchval(
            "SELECT deprecation_id FROM lease_plane.deprecated_schemes WHERE surface_kind = 'td'"
        )
        assert depr_id is not None

        await cli.deprecation_sweep_cmd(kind="td", db_url=TEST_DB_URL)
        # Two events emitted with the matching deprecation_id in payload.
        events = await conn.fetch(
            "SELECT event_type, payload FROM lease_plane.lease_plane_events "
            "WHERE event_type = 'lease.deprecation_swept' AND surface_kind = 'td' "
            "ORDER BY ts"
        )
        assert len(events) == 2
        for ev in events:
            assert ev["event_type"] == "lease.deprecation_swept"
            # payload jsonb stores deprecation_id for batch correlation.
            import json
            payload = json.loads(ev["payload"])
            assert payload.get("deprecation_id") == str(depr_id)
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_deprecation_finalize_records_check_migrated_at():
    """`deprecation-finalize` records check_migrated_at on the deprecated_schemes row."""
    from scripts.dev import lease_plane_deprecate as cli

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        await cli.deprecate_cmd(
            kind="td", session_id="test-cli-finalize", drain_window_days=30,
            db_url=TEST_DB_URL,
        )
        await cli.deprecation_sweep_cmd(kind="td", db_url=TEST_DB_URL)
        rc = await cli.deprecation_finalize_cmd(kind="td", db_url=TEST_DB_URL)
        assert rc == 0
        check_migrated_at = await conn.fetchval(
            "SELECT check_migrated_at FROM lease_plane.deprecated_schemes WHERE surface_kind = 'td'"
        )
        assert check_migrated_at is not None
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_deprecation_sweep_requires_force_release_token(monkeypatch):
    """RFC §7.10: only LEASE_FORCE_RELEASE_TOKEN authorizes the sweep, not GOVERNANCE_TOKEN."""
    from scripts.dev import lease_plane_deprecate as cli

    monkeypatch.delenv("LEASE_FORCE_RELEASE_TOKEN", raising=False)
    monkeypatch.setenv("GOVERNANCE_TOKEN", "should-not-authorize")

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        await cli.deprecate_cmd(
            kind="td", session_id="test-cli-token", drain_window_days=30,
            db_url=TEST_DB_URL,
        )
        rc = await cli.deprecation_sweep_cmd(kind="td", db_url=TEST_DB_URL)
        assert rc != 0, "Sweep without LEASE_FORCE_RELEASE_TOKEN must fail"
    finally:
        await _cleanup(conn)
        await conn.close()
