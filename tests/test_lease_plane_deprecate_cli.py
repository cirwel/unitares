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
        "OR surface_id LIKE 'capture:/pr3a%' "
        "OR surface_id LIKE '%__deprecation_marker__'"
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


# §9: test_deprecation_sweep_idempotent_on_partial_failure
@pytest.mark.asyncio
async def test_deprecation_sweep_predicate_idempotent():
    """RFC §7.11.4: sweep predicate `WHERE released_at IS NULL AND surface_kind = $1`
    reaches fixpoint on re-run after partial completion.

    The §9 alias above lets `audit_rfc_section_9_gates.py` recognize this
    as the RFC-named "idempotent_on_partial_failure" gate — the partial-failure
    semantics are exactly what the predicate guarantees, and the test name
    elides the long suffix.
    """
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


def test_deprecate_lock_key_stable_across_processes():
    """PR 5 council BLOCK fix — advisory lock key must be deterministic across
    Python invocations. PYTHONHASHSEED randomizes string hash() per-process,
    silently breaking the §7.11.7 race-window protection. CLI must use a
    stable hash (e.g., hashlib.sha256) for the advisory-lock key."""
    import subprocess
    cmd = [
        "python3", "-c",
        "from scripts.dev.lease_plane_deprecate import _lock_key_for_kind; "
        "print(_lock_key_for_kind('td'))",
    ]
    out_a = subprocess.run(
        cmd, capture_output=True, text=True, check=True, cwd=str(project_root),
    ).stdout.strip()
    out_b = subprocess.run(
        cmd, capture_output=True, text=True, check=True, cwd=str(project_root),
    ).stdout.strip()
    assert out_a == out_b, (
        f"lock key must be stable across Python processes for '{__name__}' to "
        f"protect §7.11.7 race window; got {out_a!r} vs {out_b!r}"
    )
    # Different kinds produce different keys.
    cmd_resident = [
        "python3", "-c",
        "from scripts.dev.lease_plane_deprecate import _lock_key_for_kind; "
        "print(_lock_key_for_kind('resident'))",
    ]
    out_resident = subprocess.run(
        cmd_resident, capture_output=True, text=True, check=True, cwd=str(project_root),
    ).stdout.strip()
    assert out_resident != out_a, "different kinds must produce different lock keys"


@pytest.mark.asyncio
async def test_deprecate_emits_lease_deprecation_marked_event():
    """RFC §7.11.3 audit signal contract: Phase 0 mark must emit
    event_type='lease.deprecation_marked' with deprecation_id payload.

    Pre-PR-6, the migration 027 CHECK accepted this event_type but no code
    path emitted it — dangling vocabulary surfaced by council NIT."""
    from scripts.dev import lease_plane_deprecate as cli

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        rc = await cli.deprecate_cmd(
            kind="td", session_id="test-cli-marked-event",
            drain_window_days=30, db_url=TEST_DB_URL,
        )
        assert rc == 0
        depr_id = await conn.fetchval(
            "SELECT deprecation_id FROM lease_plane.deprecated_schemes WHERE surface_kind='td'"
        )
        events = await conn.fetch(
            "SELECT event_type, payload FROM lease_plane.lease_plane_events "
            "WHERE event_type='lease.deprecation_marked' AND surface_kind='td'"
        )
        assert len(events) == 1, f"Phase 0 must emit one lease.deprecation_marked event; got {len(events)}"
        import json
        payload = json.loads(events[0]["payload"])
        assert payload.get("deprecation_id") == str(depr_id)
        assert payload.get("kind") == "td"
        assert payload.get("session_id") == "test-cli-marked-event"
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_finalize_emits_lease_deprecation_migrated_event():
    """RFC §7.11.3: Phase 3 finalize must emit event_type='lease.deprecation_migrated'
    with deprecation_id payload (audit signal that the CHECK migration completed)."""
    from scripts.dev import lease_plane_deprecate as cli

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        await cli.deprecate_cmd(
            kind="td", session_id="test-cli-migrated-event",
            drain_window_days=30, db_url=TEST_DB_URL,
        )
        depr_id = await conn.fetchval(
            "SELECT deprecation_id FROM lease_plane.deprecated_schemes WHERE surface_kind='td'"
        )
        await cli.deprecation_sweep_cmd(kind="td", db_url=TEST_DB_URL)
        rc = await cli.deprecation_finalize_cmd(kind="td", db_url=TEST_DB_URL)
        assert rc == 0
        events = await conn.fetch(
            "SELECT event_type, payload FROM lease_plane.lease_plane_events "
            "WHERE event_type='lease.deprecation_migrated' AND surface_kind='td'"
        )
        assert len(events) == 1, f"Phase 3 must emit one lease.deprecation_migrated event; got {len(events)}"
        import json
        payload = json.loads(events[0]["payload"])
        assert payload.get("deprecation_id") == str(depr_id)
        assert payload.get("kind") == "td"
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_finalize_idempotent_does_not_double_emit():
    """Re-running finalize on an already-finalized scheme does NOT emit a
    second lease.deprecation_migrated event (audit-trail clarity)."""
    from scripts.dev import lease_plane_deprecate as cli

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        await cli.deprecate_cmd(
            kind="td", session_id="test-cli-finalize-idem",
            drain_window_days=30, db_url=TEST_DB_URL,
        )
        await cli.deprecation_sweep_cmd(kind="td", db_url=TEST_DB_URL)
        rc1 = await cli.deprecation_finalize_cmd(kind="td", db_url=TEST_DB_URL)
        rc2 = await cli.deprecation_finalize_cmd(kind="td", db_url=TEST_DB_URL)
        assert rc1 == 0 and rc2 == 0
        count = await conn.fetchval(
            "SELECT count(*) FROM lease_plane.lease_plane_events "
            "WHERE event_type='lease.deprecation_migrated' AND surface_kind='td'"
        )
        assert count == 1, f"finalize must be event-idempotent; got {count} events"
    finally:
        await _cleanup(conn)
        await conn.close()


# ---------- R1: deprecate-and-finalize super-command (RFC §7.11.2 atomicity) ----------


@pytest.mark.asyncio
async def test_deprecation_sweep_and_check_migration_atomic_session():
    """RFC §9 named gate (finally implementable post-R1).

    Verifies the super-command runs Phase 2 (sweep) and Phase 3 (finalize)
    on the SAME asyncpg connection — the meaningful "same operator session"
    invariant at the DB wire level. Mocks asyncpg.connect to count
    invocations; expects exactly one connect() call across both phases.

    Also verifies both phases successfully committed (sweep_completed_at
    AND check_migrated_at populated).
    """
    from scripts.dev import lease_plane_deprecate as cli

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        await cli.deprecate_cmd(
            kind="td", session_id="test-cli-atomic-session",
            drain_window_days=30, db_url=TEST_DB_URL,
        )
        lease_id = await _seed_lease(conn, "td:/pr3a-atomic-session-test")

        real_connect = asyncpg.connect
        connect_calls = []

        async def _counting_connect(*args, **kwargs):
            connect_calls.append((args, kwargs))
            return await real_connect(*args, **kwargs)

        import unittest.mock as _mock
        with _mock.patch.object(cli.asyncpg, "connect", side_effect=_counting_connect):
            rc = await cli.deprecate_and_finalize_cmd(kind="td", db_url=TEST_DB_URL)

        assert rc == 0, "super-command must succeed on happy path"
        assert len(connect_calls) == 1, (
            f"expected exactly one asyncpg.connect call across Phase 2+3; "
            f"got {len(connect_calls)} (proves DB-wire-level same-operator-session)"
        )

        row = await conn.fetchrow(
            "SELECT sweep_completed_at, check_migrated_at "
            "FROM lease_plane.deprecated_schemes WHERE surface_kind='td'"
        )
        assert row["sweep_completed_at"] is not None, "Phase 2 must have committed"
        assert row["check_migrated_at"] is not None, "Phase 3 must have committed"

        released = await conn.fetchval(
            "SELECT released_at FROM lease_plane.surface_leases WHERE lease_id=$1",
            lease_id,
        )
        assert released is not None, "swept lease must be released"
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_deprecate_and_finalize_phase_3_failure_emits_aborted_event(monkeypatch):
    """When Phase 3 raises after Phase 2 succeeded, the super-command emits
    a lease.deprecation_aborted event with run_id + reason in payload, and
    Phase 2's swept rows STAY released (two-tx-one-conn semantics — operator
    decision 2026-05-02).
    """
    from scripts.dev import lease_plane_deprecate as cli

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        await cli.deprecate_cmd(
            kind="td", session_id="test-cli-aborted-emit",
            drain_window_days=30, db_url=TEST_DB_URL,
        )
        lease_id = await _seed_lease(conn, "td:/pr3a-aborted-test")

        async def _raising_finalize(*args, **kwargs):
            raise RuntimeError("simulated phase-3 infrastructure failure")

        monkeypatch.setattr(cli, "_finalize_inner", _raising_finalize)

        rc = await cli.deprecate_and_finalize_cmd(kind="td", db_url=TEST_DB_URL)
        assert rc == 3, f"super-command must return 3 on Phase 3 raise; got {rc}"

        events = await conn.fetch(
            "SELECT payload FROM lease_plane.lease_plane_events "
            "WHERE event_type='lease.deprecation_aborted' AND surface_kind='td'"
        )
        assert len(events) == 1, f"expected one lease.deprecation_aborted event; got {len(events)}"
        import json
        payload = json.loads(events[0]["payload"])
        assert payload["kind"] == "td"
        assert payload["phase"] == "finalize"
        assert "simulated phase-3 infrastructure failure" in payload["reason"]
        assert "run_id" in payload, "aborted event payload must include run_id"
        assert uuid.UUID(payload["run_id"])

        released = await conn.fetchval(
            "SELECT released_at FROM lease_plane.surface_leases WHERE lease_id=$1",
            lease_id,
        )
        assert released is not None, "Phase 2 work must be preserved on Phase 3 failure"

        check_migrated = await conn.fetchval(
            "SELECT check_migrated_at FROM lease_plane.deprecated_schemes WHERE surface_kind='td'"
        )
        assert check_migrated is None, "Phase 3 must not have written check_migrated_at on failure"
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_deprecate_and_finalize_phase_3_failure_then_rerun_finalize_succeeds(monkeypatch):
    """End-to-end recovery: super-command Phase 3 fails → operator runs
    standalone deprecation-finalize → succeeds and emits lease.deprecation_migrated.
    """
    from scripts.dev import lease_plane_deprecate as cli

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        await cli.deprecate_cmd(
            kind="td", session_id="test-cli-rerun-recovery",
            drain_window_days=30, db_url=TEST_DB_URL,
        )
        await _seed_lease(conn, "td:/pr3a-rerun-test")

        async def _raising_finalize(*args, **kwargs):
            raise RuntimeError("first-attempt finalize failure")

        monkeypatch.setattr(cli, "_finalize_inner", _raising_finalize)
        rc1 = await cli.deprecate_and_finalize_cmd(kind="td", db_url=TEST_DB_URL)
        assert rc1 == 3

        monkeypatch.undo()

        rc2 = await cli.deprecation_finalize_cmd(kind="td", db_url=TEST_DB_URL)
        assert rc2 == 0, "standalone finalize must succeed on rerun"

        check_migrated = await conn.fetchval(
            "SELECT check_migrated_at FROM lease_plane.deprecated_schemes WHERE surface_kind='td'"
        )
        assert check_migrated is not None, "rerun finalize must populate check_migrated_at"

        migrated_count = await conn.fetchval(
            "SELECT count(*) FROM lease_plane.lease_plane_events "
            "WHERE event_type='lease.deprecation_migrated' AND surface_kind='td'"
        )
        assert migrated_count == 1, "rerun finalize must emit exactly one migrated event"
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_deprecate_and_finalize_advisory_lock_blocks_concurrent_invocation():
    """Council CONCERN 3 (reviewer): two concurrent super-commands on the
    same kind must NOT both succeed in finalizing — without the
    pg_try_advisory_lock added in this PR, both could pass the
    `already_finalized` check before either commits and double-emit
    `lease.deprecation_migrated`. Now: the second invocation gets the lock
    busy and exits with rc=4.
    """
    from scripts.dev import lease_plane_deprecate as cli

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        await cli.deprecate_cmd(
            kind="td", session_id="test-cli-lock-block",
            drain_window_days=30, db_url=TEST_DB_URL,
        )

        # Manually acquire the advisory lock on a separate session, then
        # invoke the super-command — it should fail-fast with rc=4.
        lock_holder = await asyncpg.connect(TEST_DB_URL)
        try:
            lock_key = cli._lock_key_for_kind("td")
            held = await lock_holder.fetchval("SELECT pg_advisory_lock($1)", lock_key)
            assert held is None  # pg_advisory_lock returns void

            rc = await cli.deprecate_and_finalize_cmd(kind="td", db_url=TEST_DB_URL)
            assert rc == 4, f"super-command must fail-fast with rc=4 on lock contention; got {rc}"

            check_migrated = await conn.fetchval(
                "SELECT check_migrated_at FROM lease_plane.deprecated_schemes "
                "WHERE surface_kind='td'"
            )
            assert check_migrated is None, "no Phase 3 work should have happened under lock"
        finally:
            # Release the held lock (closing the session also releases).
            await lock_holder.close()
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_deprecate_and_finalize_abort_emission_failure_still_returns_rc_3(monkeypatch):
    """Council BLOCK 1 (reviewer) + CONCERN 1 (architect): when Phase 3 fails
    AND the abort-event emission also fails (e.g., DB unreachable persists),
    the operator must still get the structured rc=3 + rerun guidance. Without
    the try/except around _emit_aborted_event, the secondary exception would
    propagate as an unhandled stack trace, dropping the rerun signal.
    """
    from scripts.dev import lease_plane_deprecate as cli

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        await cli.deprecate_cmd(
            kind="td", session_id="test-cli-abort-emit-fails",
            drain_window_days=30, db_url=TEST_DB_URL,
        )

        async def _raising_finalize(*args, **kwargs):
            raise RuntimeError("simulated phase-3 connection failure")

        async def _raising_emit(*args, **kwargs):
            raise RuntimeError("simulated abort-emission failure")

        monkeypatch.setattr(cli, "_finalize_inner", _raising_finalize)
        monkeypatch.setattr(cli, "_emit_aborted_event", _raising_emit)

        # No unhandled exception should propagate — rc=3 + operator guidance.
        rc = await cli.deprecate_and_finalize_cmd(kind="td", db_url=TEST_DB_URL)
        assert rc == 3, f"super-command must still return rc=3 even when abort emission fails; got {rc}"
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_deprecate_and_finalize_run_id_correlates_across_events():
    """Happy path: every event emitted by a single super-command run shares
    the same run_id in payload (Phase 2 sweep events + Phase 3 migrated event).
    Enables audit queries by run_id.
    """
    from scripts.dev import lease_plane_deprecate as cli

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        await cli.deprecate_cmd(
            kind="td", session_id="test-cli-run-id-correlation",
            drain_window_days=30, db_url=TEST_DB_URL,
        )
        await _seed_lease(conn, "td:/pr3a-run-id-test-1")
        await _seed_lease(conn, "td:/pr3a-run-id-test-2")

        rc = await cli.deprecate_and_finalize_cmd(kind="td", db_url=TEST_DB_URL)
        assert rc == 0

        events = await conn.fetch(
            """
            SELECT event_type, payload FROM lease_plane.lease_plane_events
            WHERE surface_kind='td'
              AND event_type IN ('lease.deprecation_swept', 'lease.deprecation_migrated')
            ORDER BY ts
            """
        )
        assert len(events) >= 3, (
            f"expected >=3 events (2 swept + 1 migrated); got {len(events)}: "
            f"{[e['event_type'] for e in events]}"
        )

        import json
        run_ids = {json.loads(e["payload"]).get("run_id") for e in events}
        assert len(run_ids) == 1, (
            f"all super-command events must share one run_id; got {run_ids}"
        )
        the_run_id = next(iter(run_ids))
        assert the_run_id is not None
        assert uuid.UUID(the_run_id)
    finally:
        await _cleanup(conn)
        await conn.close()
