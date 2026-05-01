"""
Phase A PR 3b — Sentinel forced-release alarm rule (RFC v0.8 §7.10 + §7.11.5).

Tests the forced-release alarm wiring in `agents/sentinel/forced_release_alarm.py`:

  - Per-event alarm for ad-hoc `event_type='forced'` (RFC §7.10 alarm-on-every-event).
  - Batched alarm for `event_type='lease.deprecation_swept'`: groups by
    `deprecation_id`, emits one summary alarm per completed batch
    (RFC §7.11.5 batch suppression).
  - Cursor state so successive polls don't re-emit alarms for already-seen events.

Spec: docs/proposals/surface-lease-plane-v0.md §7.10, §7.11.5
      docs/proposals/surface-lease-plane-phase-a-plan.md PR 3b
"""

from __future__ import annotations

import json
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


async def _cleanup(conn):
    """Reset sweep / event / deprecated_schemes rows from prior tests."""
    await conn.execute(
        "DELETE FROM lease_plane.lease_plane_events WHERE surface_id LIKE 'td:/pr3b%' "
        "OR payload->>'deprecation_id' IN (SELECT deprecation_id::text FROM lease_plane.deprecated_schemes "
        "WHERE marked_by_session_id LIKE 'test-pr3b-%')"
    )
    await conn.execute(
        "DELETE FROM lease_plane.deprecated_schemes WHERE marked_by_session_id LIKE 'test-pr3b-%'"
    )
    await conn.execute(
        "DELETE FROM lease_plane.surface_leases WHERE intent LIKE 'pr3b-test%'"
    )


async def _emit_event(
    conn, *, event_type: str, surface_id: str,
    surface_kind: str | None = None, payload: dict | None = None,
) -> uuid.UUID:
    """Insert a synthetic lease_plane_events row, return event_id."""
    if surface_kind is None:
        surface_kind = surface_id.split(":", 1)[0]
    event_id = await conn.fetchval(
        """
        INSERT INTO lease_plane.lease_plane_events
          (event_type, surface_id, surface_kind, advisory_mode, payload)
        VALUES ($1, $2, $3, false, $4::jsonb)
        RETURNING event_id
        """,
        event_type, surface_id, surface_kind, json.dumps(payload or {}),
    )
    return event_id


@pytest.mark.asyncio
async def test_sentinel_force_release_alarm_per_event():
    """RFC §7.10: ad-hoc event_type='forced' produces one alarm per event."""
    from agents.sentinel.forced_release_alarm import poll_forced_release_alarms

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        # Seed two ad-hoc forced events.
        e1 = await _emit_event(conn, event_type="forced", surface_id="td:/pr3b_a")
        e2 = await _emit_event(conn, event_type="forced", surface_id="td:/pr3b_b")

        alarms, new_cursor = await poll_forced_release_alarms(
            db_url=TEST_DB_URL, last_event_ts=None,
        )
        # One alarm per event; both should appear.
        per_event = [a for a in alarms if a.kind == "ad_hoc"]
        assert len(per_event) == 2, f"expected 2 ad-hoc alarms, got {len(per_event)}"
        surface_ids = {a.extra["surface_id"] for a in per_event}
        assert surface_ids == {"td:/pr3b_a", "td:/pr3b_b"}
        assert new_cursor is not None
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_sentinel_force_release_alarm_dedupes_via_cursor():
    """Re-polling with the cursor returned by a previous poll yields zero new alarms."""
    from agents.sentinel.forced_release_alarm import poll_forced_release_alarms

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        await _emit_event(conn, event_type="forced", surface_id="td:/pr3b_dedupe")

        first_alarms, cursor = await poll_forced_release_alarms(
            db_url=TEST_DB_URL, last_event_ts=None,
        )
        assert len([a for a in first_alarms if a.kind == "ad_hoc"]) >= 1

        # Re-poll with the cursor; no new alarms.
        second_alarms, _ = await poll_forced_release_alarms(
            db_url=TEST_DB_URL, last_event_ts=cursor,
        )
        ad_hoc_second = [a for a in second_alarms if a.kind == "ad_hoc"]
        assert ad_hoc_second == [], (
            f"cursor-based dedup failed; got {len(ad_hoc_second)} re-emitted alarms"
        )
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_sentinel_batch_alarm_for_deprecation_sweep():
    """RFC §7.11.5: N lease.deprecation_swept events from same deprecation_id
    produce ONE summary alarm, not N per-event alarms."""
    from agents.sentinel.forced_release_alarm import poll_forced_release_alarms

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        # Mark a scheme deprecated and complete the sweep so sweep_completed_at IS NOT NULL.
        depr_id = await conn.fetchval(
            """
            INSERT INTO lease_plane.deprecated_schemes
              (surface_kind, marked_by_session_id, drain_window_days,
               sweep_started_at, sweep_completed_at)
            VALUES ('td', 'test-pr3b-batch', 30, now(), now())
            RETURNING deprecation_id
            """
        )
        # Seed 3 sweep events with the same deprecation_id.
        for sid in ("td:/pr3b_batch_a", "td:/pr3b_batch_b", "td:/pr3b_batch_c"):
            await _emit_event(
                conn, event_type="lease.deprecation_swept",
                surface_id=sid,
                payload={"deprecation_id": str(depr_id), "kind": "td"},
            )

        alarms, _ = await poll_forced_release_alarms(
            db_url=TEST_DB_URL, last_event_ts=None,
        )
        batched = [a for a in alarms if a.kind == "deprecation_batch"]
        assert len(batched) == 1, (
            f"expected exactly 1 batched alarm for deprecation_id={depr_id}, got {len(batched)}"
        )
        assert batched[0].extra["count"] == 3
        assert batched[0].extra["deprecation_id"] == str(depr_id)
        assert batched[0].extra["kind"] == "td"
        # Importantly: NO per-event 'ad_hoc' alarms for the deprecation_swept events.
        per_event = [a for a in alarms if a.kind == "ad_hoc"]
        ad_hoc_for_swept = [a for a in per_event if "pr3b_batch" in a.extra.get("surface_id", "")]
        assert ad_hoc_for_swept == [], (
            f"deprecation_swept events must not produce per-event alarms; got {ad_hoc_for_swept}"
        )
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_sentinel_batch_alarm_only_after_sweep_completed_at():
    """Batched alarm waits for sweep_completed_at to be set on the
    deprecated_schemes row — partial sweeps don't fire premature alarms."""
    from agents.sentinel.forced_release_alarm import poll_forced_release_alarms

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        # In-progress sweep: sweep_started_at set, sweep_completed_at NULL.
        depr_id = await conn.fetchval(
            """
            INSERT INTO lease_plane.deprecated_schemes
              (surface_kind, marked_by_session_id, drain_window_days, sweep_started_at)
            VALUES ('td', 'test-pr3b-partial', 30, now())
            RETURNING deprecation_id
            """
        )
        await _emit_event(
            conn, event_type="lease.deprecation_swept",
            surface_id="td:/pr3b_partial_a",
            payload={"deprecation_id": str(depr_id), "kind": "td"},
        )

        alarms, _ = await poll_forced_release_alarms(
            db_url=TEST_DB_URL, last_event_ts=None,
        )
        # No batched alarm yet (sweep still in progress).
        batched = [
            a for a in alarms if a.kind == "deprecation_batch"
            and a.extra.get("deprecation_id") == str(depr_id)
        ]
        assert batched == [], "batched alarm fired before sweep_completed_at was set"

        # Now complete the sweep — alarm should fire on next poll.
        await conn.execute(
            "UPDATE lease_plane.deprecated_schemes SET sweep_completed_at = now() "
            "WHERE deprecation_id = $1",
            depr_id,
        )
        alarms2, _ = await poll_forced_release_alarms(
            db_url=TEST_DB_URL, last_event_ts=None,
        )
        batched2 = [
            a for a in alarms2 if a.kind == "deprecation_batch"
            and a.extra.get("deprecation_id") == str(depr_id)
        ]
        assert len(batched2) == 1, (
            f"batched alarm should fire after sweep_completed_at; got {len(batched2)}"
        )
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_phase_zero_acquire_race_blocked():
    """RFC §7.11.7: serializable transaction + advisory lock during deprecate_cmd
    blocks concurrent acquires from racing the Phase 0 mark.

    The CLI's deprecate_cmd holds pg_advisory_xact_lock(_lock_key_for_kind(kind)).
    A concurrent pg_advisory_xact_lock attempt on the same key blocks until the
    deprecate transaction commits.

    Note (PR 5): test now imports _lock_key_for_kind() — the SHA-256-derived
    deterministic key. The pre-PR-5 implementation used `abs(hash(kind))` which
    is salted per-process by PYTHONHASHSEED, so the test passed only by accident
    (both holder and deprecate_cmd ran in the same process). PR 5 fixes the CLI;
    this test now uses the same helper as production.
    """
    import asyncio
    from scripts.dev import lease_plane_deprecate as cli
    from scripts.dev.lease_plane_deprecate import _lock_key_for_kind

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)

        kind = "td"
        lock_key = _lock_key_for_kind(kind)

        # Open a separate connection that holds the SAME advisory lock.
        # While it holds, deprecate_cmd's serializable transaction must wait.
        holder_conn = await asyncpg.connect(TEST_DB_URL)
        await holder_conn.execute("BEGIN")
        await holder_conn.execute("SELECT pg_advisory_xact_lock($1)", lock_key)

        # Run deprecate_cmd with a tight asyncio timeout — should NOT complete
        # while holder_conn still holds the lock.
        deprecate_task = asyncio.create_task(
            cli.deprecate_cmd(
                kind=kind, session_id="test-pr3b-race",
                drain_window_days=30, db_url=TEST_DB_URL,
            )
        )
        try:
            await asyncio.wait_for(asyncio.shield(deprecate_task), timeout=0.5)
            assert False, "deprecate_cmd completed despite advisory lock being held by another tx"
        except asyncio.TimeoutError:
            pass  # expected — lock contention blocks the deprecate

        # Release the holder lock; deprecate_cmd should now complete.
        await holder_conn.execute("ROLLBACK")
        await holder_conn.close()

        rc = await asyncio.wait_for(deprecate_task, timeout=5.0)
        assert rc == 0, f"deprecate_cmd should succeed once lock is released; rc={rc}"
        # Verify the row landed.
        count = await conn.fetchval(
            "SELECT count(*) FROM lease_plane.deprecated_schemes "
            "WHERE marked_by_session_id = 'test-pr3b-race'"
        )
        assert count == 1
    finally:
        await _cleanup(conn)
        await conn.close()
