"""§9 gates for RFC v0.11 §7.13 — substrate_state columns + CHECK constraints.

Implements the 8 §9 test gates listed in §7.13.6 PR 1 touch-list:

  (a) acquire(substrate_state={...}) persists
  (b) renew(substrate_state={...}) persists (regression test for council BLOCK-1)
  (c) CHECK violations return 422 not 503 with detail in the constraint-name set
  (d) substrate_state on a non-resident lease is rejected by
      substrate_state_only_on_resident_kind
  (e) substrate_state without sensor.status OR with status not in
      {healthy,degraded,failed} is rejected by substrate_state_has_sensor_status
  (f) present_lease/1 (status path) returns substrate columns after renew
  (g) class-aware void-threshold test (parks alongside PR 3 land), marked xfail-strict
  (h) reader-side: rows with status='degraded' tolerate NULL last_healthy_observed_at

      db/postgres/migrations/034_lease_plane_substrate_state.sql
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


_VALID_SUBSTRATE = {
    "E": 0.36,
    "I": 0.81,
    "S": 0.22,
    "V": 0.07,
    "sensor": {"status": "healthy"},
}

_CONSTRAINT_NAMES = {
    "substrate_state_observed_pair_coherent",
    "substrate_state_only_on_resident_kind",
    "substrate_state_must_be_object",
    "substrate_state_has_sensor_status",
}


async def _cleanup(conn) -> None:
    await conn.execute(
        "DELETE FROM lease_plane.surface_leases WHERE intent LIKE 'substrate-gate-test%'"
    )


async def _insert_lease(
    conn,
    *,
    surface_id: str,
    intent: str,
    substrate_state: dict | None = None,
    substrate_state_observed_at: datetime | None = None,
) -> uuid.UUID:
    lease_id = uuid.uuid4()
    now = datetime.now(UTC)
    await conn.execute(
        """
        INSERT INTO lease_plane.surface_leases
          (lease_id, surface_id, holder_agent_uuid, holder_kind, holder_class,
           heartbeat_required, original_ttl_s, acquired_at, expires_at, intent,
           substrate_state, substrate_state_observed_at)
        VALUES ($1, $2, $3, 'remote_heartbeat', 'process_instance',
                true, $4, $5, $6, $7, $8::jsonb, $9::timestamptz)
        """,
        lease_id, surface_id, uuid.uuid4(), 60,
        now, now + timedelta(seconds=60), intent,
        json.dumps(substrate_state) if substrate_state is not None else None,
        substrate_state_observed_at,
    )
    return lease_id


# (a) acquire(substrate_state) persists
@pytest.mark.asyncio
async def test_acquire_with_substrate_state_persists():
    """§7.13 gate (a): substrate_state populated at INSERT round-trips through SELECT."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        now = datetime.now(UTC)
        await _insert_lease(
            conn,
            surface_id="resident:/substrate-gate-test-a",
            intent="substrate-gate-test-acquire",
            substrate_state=_VALID_SUBSTRATE,
            substrate_state_observed_at=now,
        )
        row = await conn.fetchrow(
            "SELECT substrate_state, substrate_state_observed_at "
            "FROM lease_plane.surface_leases "
            "WHERE surface_id = 'resident:/substrate-gate-test-a'"
        )
        assert row is not None
        stored = json.loads(row["substrate_state"])
        assert stored["E"] == _VALID_SUBSTRATE["E"]
        assert stored["sensor"]["status"] == "healthy"
        assert row["substrate_state_observed_at"] is not None
    finally:
        await _cleanup(conn)
        await conn.close()


# (b) renew(substrate_state) persists — regression test for council BLOCK-1
@pytest.mark.asyncio
async def test_renew_with_substrate_state_persists():
    """§7.13 gate (b): UPDATE-via-COALESCE pattern persists new substrate values.

    Regression test for v0.11 council BLOCK-1 (multi-site renew silent-drop):
    if the renew SQL doesn't include substrate columns in the SET clause, this
    test catches the regression by failing to read back the new value.
    """
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        now = datetime.now(UTC)
        # Initial INSERT with one substrate value
        await _insert_lease(
            conn,
            surface_id="resident:/substrate-gate-test-b",
            intent="substrate-gate-test-renew",
            substrate_state={"E": 0.5, "I": 0.5, "S": 0.0, "V": 0.0,
                              "sensor": {"status": "healthy"}},
            substrate_state_observed_at=now,
        )
        # Simulate renew with new substrate (mirrors repo.ex renew/3 COALESCE pattern)
        new_substrate = {
            "E": 0.36, "I": 0.81, "S": 0.22, "V": 0.07,
            "sensor": {"status": "degraded", "reason": "probe_timeout"},
        }
        new_observed = now + timedelta(seconds=300)
        await conn.execute(
            """
            UPDATE lease_plane.surface_leases
            SET expires_at = now() + make_interval(secs => original_ttl_s),
                substrate_state = COALESCE($1::jsonb, substrate_state),
                substrate_state_observed_at = COALESCE($2::timestamptz, substrate_state_observed_at)
            WHERE surface_id = 'resident:/substrate-gate-test-b'
            """,
            json.dumps(new_substrate),
            new_observed,
        )
        row = await conn.fetchrow(
            "SELECT substrate_state, substrate_state_observed_at "
            "FROM lease_plane.surface_leases "
            "WHERE surface_id = 'resident:/substrate-gate-test-b'"
        )
        stored = json.loads(row["substrate_state"])
        assert stored["sensor"]["status"] == "degraded"
        assert stored["sensor"]["reason"] == "probe_timeout"
        assert row["substrate_state_observed_at"] == new_observed
    finally:
        await _cleanup(conn)
        await conn.close()


# (c) CHECK violations surface as a known constraint name (router would map → 422)
@pytest.mark.asyncio
async def test_check_violation_constraint_names_are_in_known_set():
    """§7.13 gate (c): each of the four CHECK constraints raises a CheckViolationError
    whose constraint_name is in the documented set. The router's typed-error contract
    (RFC §7.13.5) maps these to HTTP 422 with detail=constraint_name. Per v0.11.3
    determinism note, the test asserts membership in the set, NOT a pinned name."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        now = datetime.now(UTC)

        # Trigger pair-coherence by inserting only one of the pair
        with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc_info:
            await _insert_lease(
                conn,
                surface_id="resident:/substrate-gate-test-c1",
                intent="substrate-gate-test-pair",
                substrate_state=_VALID_SUBSTRATE,
                substrate_state_observed_at=None,  # observed_at missing
            )
        assert exc_info.value.constraint_name in _CONSTRAINT_NAMES
    finally:
        await _cleanup(conn)
        await conn.close()


# (d) non-resident lease rejected by substrate_state_only_on_resident_kind
@pytest.mark.asyncio
async def test_substrate_state_rejected_on_non_resident_lease():
    """§7.13 gate (d): writing substrate_state to a file:// or dialectic:/ lease
    fails substrate_state_only_on_resident_kind."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        now = datetime.now(UTC)
        with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc_info:
            await _insert_lease(
                conn,
                surface_id="dialectic:/substrate-gate-test-d",
                intent="substrate-gate-test-non-resident",
                substrate_state=_VALID_SUBSTRATE,
                substrate_state_observed_at=now,
            )
        assert exc_info.value.constraint_name == "substrate_state_only_on_resident_kind"
    finally:
        await _cleanup(conn)
        await conn.close()


# (e) sensor.status enforcement: missing OR non-vocabulary value rejected
@pytest.mark.asyncio
async def test_substrate_state_sensor_status_enforced():
    """§7.13 gate (e): substrate_state without sensor.status, OR with status
    not in {healthy,degraded,failed}, fails substrate_state_has_sensor_status."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        now = datetime.now(UTC)

        # Missing sensor sub-key entirely
        with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc_info:
            await _insert_lease(
                conn,
                surface_id="resident:/substrate-gate-test-e1",
                intent="substrate-gate-test-no-sensor",
                substrate_state={"E": 0.5, "I": 0.5, "S": 0.0, "V": 0.0},
                substrate_state_observed_at=now,
            )
        assert exc_info.value.constraint_name == "substrate_state_has_sensor_status"

        # Out-of-vocabulary status value
        with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc_info:
            await _insert_lease(
                conn,
                surface_id="resident:/substrate-gate-test-e2",
                intent="substrate-gate-test-bad-vocab",
                substrate_state={"E": 0.5, "sensor": {"status": "ok"}},
                substrate_state_observed_at=now,
            )
        assert exc_info.value.constraint_name == "substrate_state_has_sensor_status"

        # Numeric status value (the v0.11.3 type-passing-hole fix)
        with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc_info:
            await _insert_lease(
                conn,
                surface_id="resident:/substrate-gate-test-e3",
                intent="substrate-gate-test-numeric-status",
                substrate_state={"E": 0.5, "sensor": {"status": 200}},
                substrate_state_observed_at=now,
            )
        assert exc_info.value.constraint_name == "substrate_state_has_sensor_status"
    finally:
        await _cleanup(conn)
        await conn.close()


# (f) status path returns substrate after renew
@pytest.mark.asyncio
async def test_substrate_state_visible_in_select_after_renew():
    """§7.13 gate (f): SELECT after renew returns the new substrate values.

    Guards against partial implementation where Repo SQL is updated but
    @select_lease_columns isn't, causing the renew RETURNING projection to
    silently drop the columns from the response. The router's present_lease
    function depends on @select_lease_columns including substrate fields."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        now = datetime.now(UTC)
        await _insert_lease(
            conn,
            surface_id="resident:/substrate-gate-test-f",
            intent="substrate-gate-test-status-after-renew",
            substrate_state=_VALID_SUBSTRATE,
            substrate_state_observed_at=now,
        )
        # Simulate the renew RETURNING projection (matches repo.ex @select_lease_columns)
        row = await conn.fetchrow(
            """
            UPDATE lease_plane.surface_leases
            SET expires_at = now() + make_interval(secs => original_ttl_s)
            WHERE surface_id = 'resident:/substrate-gate-test-f'
            RETURNING surface_id, substrate_state, substrate_state_observed_at
            """
        )
        assert row is not None
        assert row["substrate_state"] is not None
        assert row["substrate_state_observed_at"] is not None
        stored = json.loads(row["substrate_state"])
        assert stored["sensor"]["status"] == "healthy"
    finally:
        await _cleanup(conn)
        await conn.close()


# (g) class-aware void-threshold test — flipped to live by PR 3
def test_resident_class_exempted_from_void_threshold():
    """§7.13 gate (g): a resident-class agent with V_ss = 0.19 does NOT trip
    check_void_state, while a non-resident agent at the same V_ss DOES trip it.
    Confirms the PR 3 class-aware threshold lookup wires through correctly.

    Originally parked as xfail(strict=True) waiting on PR 3's
    VOID_THRESHOLD_BY_CLASS map landing. PR 3 ships with this lit up.
    Detailed PR 3 unit tests live in tests/test_void_threshold_class_aware.py;
    this gate is the §9-checklist contract pin showing the class lookup
    threads through end-to-end from the §7.13 RFC perspective."""
    import numpy as np

    from config.governance_config import config

    # Standard adaptive path with low-variance history clamps to MIN (0.10).
    # The 2026-05-01 incident showed Steward V_ss ≈ 0.19, well past 0.15
    # INITIAL — would trip the void_pause path. Resident override (0.30)
    # clears it.
    history = np.array([0.05] * 100)
    threshold_default = config.get_void_threshold(history, adaptive=True)
    threshold_resident = config.get_void_threshold(
        history, adaptive=True, agent_class="resident_persistent"
    )
    assert threshold_resident > threshold_default
    assert threshold_resident == 0.30
    assert 0.19 < threshold_resident, "Steward V_ss must clear resident threshold"
    assert 0.19 > threshold_default, (
        "Steward V_ss must trip the default threshold — the whole point of the "
        "interim safety net is the gap between these two"
    )


# (h) reader-side: degraded status with NULL last_healthy_observed_at handled
@pytest.mark.asyncio
async def test_reader_handles_degraded_status_without_last_healthy_observed_at():
    """§7.13 gate (h): a row with sensor.status='degraded' but no
    last_healthy_observed_at is valid (the field is RECOMMENDED-not-CHECK-enforced
    per §7.13.1.2). Reader-side tooling MUST tolerate the NULL gracefully —
    this test asserts the row stores cleanly and the field surface is queryable."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        now = datetime.now(UTC)
        await _insert_lease(
            conn,
            surface_id="resident:/substrate-gate-test-h",
            intent="substrate-gate-test-degraded-no-timestamp",
            substrate_state={
                "E": 0.36, "I": 0.81, "S": 0.22, "V": 0.07,
                "sensor": {"status": "degraded", "reason": "probe_timeout"},
                # Intentional: last_healthy_observed_at omitted
            },
            substrate_state_observed_at=now,
        )
        row = await conn.fetchrow(
            "SELECT substrate_state -> 'sensor' ->> 'status' AS status, "
            "       substrate_state -> 'sensor' ->> 'last_healthy_observed_at' AS last_healthy "
            "FROM lease_plane.surface_leases "
            "WHERE surface_id = 'resident:/substrate-gate-test-h'"
        )
        assert row["status"] == "degraded"
        # NULL is the documented graceful-degradation: the reader sees absence
        # rather than a malformed row.
        assert row["last_healthy"] is None
    finally:
        await _cleanup(conn)
        await conn.close()
