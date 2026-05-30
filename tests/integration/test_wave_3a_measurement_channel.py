"""
Wave 3a measurement-channel integration tests (PR #2 of Wave 3a sequencing).

Specification:
    ``docs/proposals/beam-wave-3a-read-only-handlers.md`` v0.2 §4.3
    (audit.coordination_measurements schema + write path) and §4.2
    (stop-sign event_type wiring for ``coordination_failure.wave_3a.*``).

Coverage (6 cases per RFC §5 PR #2 spec):

    1. Migration 041 applies cleanly: audit.coordination_measurements has the
       expected columns, indexes, and CHECK constraints.
    2. Direct insert with measurement_type='measurement.wave_3a.request'
       succeeds; constraints accept the canonical Wave 3a value.
    3. audit.events accepts coordination_failure.wave_3a.fallback (and the
       sibling ``.timeout`` / ``.envelope_invalid`` event types) — proves
       PR #3 / PR #4 can write the §4.2 stop-sign events without re-extending
       the event_type CHECK constraint.
    4. End-to-end: GET /v1/probe/health (no auth) writes one row with
       endpoint='/v1/probe/health', status='200', payload_bytes>0.
    5. End-to-end: GET /v1/probe/health_snapshot with WRONG bearer writes
       one row with status='401'.
    6. End-to-end: GET /v1/probe/health_snapshot with token UNSET writes
       one row with status='503'.

Test surface:
    Direct asyncpg connection to ``governance_test`` for the migration and
    direct-insert cases. End-to-end cases mount the probe routes on a
    Starlette TestClient (same pattern as ``tests/integration/test_wave_3a_probe.py``)
    and patch ``src.db.get_db`` to return a stub backend whose ``acquire()``
    yields a real asyncpg connection — this lets the fire-and-forget
    ``asyncio.create_task`` measurement write actually land in the live test
    DB without coupling the probe module to the test pool.

Test database bootstrap:
    Migration 041 is applied at module setup if not already present in
    ``governance_test``. This sidesteps the ``tests/test_db_utils.py``
    migration list (out of PR #2 scope per the implementation guide).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import pytest

project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

try:
    import asyncpg
except ImportError:  # pragma: no cover — guarded by skip below
    pytest.skip("asyncpg not installed", allow_module_level=True)

from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.test_db_utils import (  # type: ignore
    TEST_DB_URL,
    can_connect_to_test_db,
    ensure_test_database_schema,
)

if not can_connect_to_test_db():
    pytest.skip("governance_test database not available", allow_module_level=True)

from src.mcp_handlers import wave3a_probe  # noqa: E402
from src.mcp_handlers.wave3a_probe import (  # noqa: E402
    MEASUREMENT_TYPE_WAVE_3A_REQUEST,
    PROBE_PREFIX,
    PROBE_TOKEN_ENV,
    register_wave3a_probe_routes,
)


# ---------------------------------------------------------------------------
# Module-level bootstrap: ensure governance_test has migration 041 applied.
# ---------------------------------------------------------------------------


MIGRATION_041_PATH = (
    project_root / "db" / "postgres" / "migrations" / "041_wave3a_coordination_measurements.sql"
)


async def _ensure_measurement_table() -> None:
    """Idempotently apply migration 041 to governance_test.

    The repo-wide schema bootstrap in tests/test_db_utils.py stops at 036
    (current head as of the PR #1 baseline). PR #2 needs 041 applied to the
    same database. CREATE TABLE IF NOT EXISTS + DO $$ EXCEPTION blocks in the
    migration are idempotent, so re-applying is safe.
    """
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL, timeout=5)
    try:
        sql = MIGRATION_041_PATH.read_text(encoding="utf-8")
        await conn.execute(sql)
    finally:
        await conn.close()


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_measurement_table() -> None:
    asyncio.run(_ensure_measurement_table())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


VALID_TOKEN = "wave3a-measurement-test-token"
WRONG_TOKEN = "wave3a-measurement-wrong-token"


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _truncate_measurements() -> None:
    """Remove all rows from audit.coordination_measurements for test isolation."""
    conn = await asyncpg.connect(TEST_DB_URL, timeout=5)
    try:
        await conn.execute("TRUNCATE TABLE audit.coordination_measurements")
    finally:
        await conn.close()


async def _count_measurements(endpoint: str | None = None) -> int:
    conn = await asyncpg.connect(TEST_DB_URL, timeout=5)
    try:
        if endpoint is None:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM audit.coordination_measurements"
            )
        return await conn.fetchval(
            "SELECT COUNT(*) FROM audit.coordination_measurements WHERE endpoint = $1",
            endpoint,
        )
    finally:
        await conn.close()


async def _fetch_measurements(endpoint: str | None = None) -> list[dict[str, Any]]:
    conn = await asyncpg.connect(TEST_DB_URL, timeout=5)
    try:
        if endpoint is None:
            rows = await conn.fetch(
                "SELECT measurement_type, endpoint, elapsed_ms, status, "
                "payload_bytes, meta FROM audit.coordination_measurements "
                "ORDER BY recorded_at"
            )
        else:
            rows = await conn.fetch(
                "SELECT measurement_type, endpoint, elapsed_ms, status, "
                "payload_bytes, meta FROM audit.coordination_measurements "
                "WHERE endpoint = $1 ORDER BY recorded_at",
                endpoint,
            )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


@asynccontextmanager
async def _wait_for_rows(
    endpoint: str, *, expected: int = 1, timeout_s: float = 3.0
) -> AsyncIterator[None]:
    """Wait until the measurement row(s) land via the fire-and-forget task.

    asyncio.create_task is best-effort: the probe response returns before
    the row is written. Tests poll on a short interval rather than sleeping
    a fixed amount.
    """
    yield
    start = time.monotonic()
    while (time.monotonic() - start) < timeout_s:
        count = await _count_measurements(endpoint)
        if count >= expected:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"timed out waiting for {expected} row(s) at endpoint={endpoint!r}; "
        f"observed {await _count_measurements(endpoint)}"
    )


# ---------------------------------------------------------------------------
# Case 1 — migration 041 applies cleanly
# ---------------------------------------------------------------------------


class TestMigrationApplies:
    """§4.3 schema spec: columns, indexes, CHECK constraints."""

    def test_columns_match_spec(self) -> None:
        async def _check() -> dict[str, dict[str, Any]]:
            conn = await asyncpg.connect(TEST_DB_URL, timeout=5)
            try:
                rows = await conn.fetch(
                    """
                    SELECT column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_schema = 'audit'
                      AND table_name = 'coordination_measurements'
                    ORDER BY ordinal_position
                    """
                )
                return {r["column_name"]: dict(r) for r in rows}
            finally:
                await conn.close()

        columns = asyncio.run(_check())
        # Every column from the §4.3 schema spec must be present.
        expected = {
            "id",
            "recorded_at",
            "measurement_type",
            "endpoint",
            "elapsed_ms",
            "status",
            "payload_bytes",
            "meta",
        }
        assert expected.issubset(columns.keys()), (
            f"missing columns: {expected - columns.keys()}"
        )
        # measurement_type / endpoint / elapsed_ms / status are NOT NULL per spec.
        for col in ("measurement_type", "endpoint", "elapsed_ms", "status"):
            assert columns[col]["is_nullable"] == "NO", f"{col} should be NOT NULL"
        # payload_bytes is NULLABLE per spec.
        assert columns["payload_bytes"]["is_nullable"] == "YES"
        # meta is NULLABLE JSONB per spec.
        assert columns["meta"]["data_type"] == "jsonb"
        assert columns["meta"]["is_nullable"] == "YES"

    def test_indexes_exist(self) -> None:
        async def _check() -> set[str]:
            conn = await asyncpg.connect(TEST_DB_URL, timeout=5)
            try:
                rows = await conn.fetch(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE schemaname = 'audit'
                      AND tablename = 'coordination_measurements'
                    """
                )
                return {r["indexname"] for r in rows}
            finally:
                await conn.close()

        indexes = asyncio.run(_check())
        assert "idx_coord_meas_type_time" in indexes
        assert "idx_coord_meas_endpoint" in indexes


# ---------------------------------------------------------------------------
# Case 2 — direct insert with the canonical Wave 3a measurement_type
# ---------------------------------------------------------------------------


class TestMeasurementInsert:
    """measurement_type='measurement.wave_3a.request' passes the namespace CHECK."""

    def test_canonical_insert_succeeds(self) -> None:
        async def _check() -> int:
            await _truncate_measurements()
            conn = await asyncpg.connect(TEST_DB_URL, timeout=5)
            try:
                await conn.execute(
                    """
                    INSERT INTO audit.coordination_measurements
                        (measurement_type, endpoint, elapsed_ms, status,
                         payload_bytes, meta)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                    """,
                    MEASUREMENT_TYPE_WAVE_3A_REQUEST,
                    "/v1/probe/health",
                    7,
                    "200",
                    42,
                    json.dumps({"probe_token_set": True}),
                )
                return await conn.fetchval(
                    "SELECT COUNT(*) FROM audit.coordination_measurements"
                )
            finally:
                await conn.close()

        assert asyncio.run(_check()) == 1

    def test_namespace_check_rejects_malformed(self) -> None:
        async def _check() -> None:
            await _truncate_measurements()
            conn = await asyncpg.connect(TEST_DB_URL, timeout=5)
            try:
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute(
                        """
                        INSERT INTO audit.coordination_measurements
                            (measurement_type, endpoint, elapsed_ms, status)
                        VALUES ('BAD_NAMESPACE', '/x', 1, '200')
                        """
                    )
            finally:
                await conn.close()

        asyncio.run(_check())


# ---------------------------------------------------------------------------
# Case 3 — coordination_failure.wave_3a.* event_types accepted by CHECK
# ---------------------------------------------------------------------------


class TestWave3aEventTypeAccepted:
    """Migration 035's CHECK regex already accepts arbitrarily deep subtypes
    under coordination_failure, so coordination_failure.wave_3a.fallback /
    .timeout / .envelope_invalid pass at the DB layer without a follow-up
    migration. The Python-side allowlist in src/coordination_events.py is
    what this PR extended; the DB CHECK is verified here.
    """

    def test_all_wave_3a_event_types_pass_check(self) -> None:
        event_types = [
            "coordination_failure.wave_3a.fallback",
            "coordination_failure.wave_3a.timeout",
            "coordination_failure.wave_3a.envelope_invalid",
        ]

        async def _check() -> None:
            conn = await asyncpg.connect(TEST_DB_URL, timeout=5)
            try:
                # Ensure a partition for the current ts exists. The test DB
                # bootstrap creates the standard month partitions via 035 +
                # partitions.sql; here we only insert and rely on the default
                # partition catching anything outside.
                for et in event_types:
                    await conn.execute(
                        """
                        INSERT INTO audit.coordination_events
                            (ts, service, event_type, payload, context)
                        VALUES (NOW(), 'governance_mcp', $1, '{}'::jsonb,
                                '{}'::jsonb)
                        """,
                        et,
                    )
                count = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM audit.coordination_events
                    WHERE event_type LIKE 'coordination_failure.wave_3a.%'
                    """
                )
                assert count >= len(event_types)
            finally:
                await conn.close()

        asyncio.run(_check())

    def test_python_allowlist_contains_wave_3a_types(self) -> None:
        from src.coordination_events import WAVE_0_EVENT_TYPES

        assert "coordination_failure.wave_3a.fallback" in WAVE_0_EVENT_TYPES
        assert "coordination_failure.wave_3a.timeout" in WAVE_0_EVENT_TYPES
        assert "coordination_failure.wave_3a.envelope_invalid" in WAVE_0_EVENT_TYPES


# ---------------------------------------------------------------------------
# End-to-end: probe call -> measurement row
# ---------------------------------------------------------------------------


class _StubBackend:
    """Stub DatabaseBackend exposing only the ``acquire()`` interface.

    The probe write path uses ``async with get_db().acquire() as conn:`` —
    that contract is the only surface required. The stub returns a real
    asyncpg connection from a per-call connect (no pool needed for tests),
    so the row actually lands in governance_test.
    """

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        conn = await asyncpg.connect(TEST_DB_URL, timeout=5)
        try:
            yield conn
        finally:
            await conn.close()


@pytest.fixture
def patched_db(monkeypatch: pytest.MonkeyPatch) -> _StubBackend:
    """Make ``src.db.get_db()`` return the stub backend for the test."""
    backend = _StubBackend()

    def _get_db() -> _StubBackend:
        return backend

    import src.db

    monkeypatch.setattr(src.db, "get_db", _get_db)
    return backend


@pytest.fixture
def app() -> Starlette:
    app = Starlette(routes=[])
    register_wave3a_probe_routes(app)
    return app


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client


@pytest.fixture
def token_set(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(PROBE_TOKEN_ENV, VALID_TOKEN)
    return VALID_TOKEN


@pytest.fixture
def token_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PROBE_TOKEN_ENV, raising=False)


class TestEndToEndMeasurementWrite:
    """End-to-end probe call -> row in audit.coordination_measurements.

    The probe handler uses ``asyncio.create_task(_write_measurement(...))``
    so the row lands asynchronously. The TestClient's underlying portal
    drives the loop until the create_task'd write completes; we still poll
    explicitly because the response returns before the task does.
    """

    def test_health_records_200(
        self,
        client: TestClient,
        patched_db: _StubBackend,
    ) -> None:
        endpoint = f"{PROBE_PREFIX}/health"

        async def _setup_and_assert() -> None:
            await _truncate_measurements()

        asyncio.run(_setup_and_assert())

        response = client.get(endpoint)
        assert response.status_code == 200

        async def _wait() -> list[dict[str, Any]]:
            start = time.monotonic()
            while (time.monotonic() - start) < 3.0:
                count = await _count_measurements(endpoint)
                if count >= 1:
                    return await _fetch_measurements(endpoint)
                await asyncio.sleep(0.05)
            raise AssertionError(
                f"timed out: {await _count_measurements(endpoint)} rows for {endpoint}"
            )

        rows = asyncio.run(_wait())
        assert len(rows) == 1
        row = rows[0]
        assert row["measurement_type"] == MEASUREMENT_TYPE_WAVE_3A_REQUEST
        assert row["endpoint"] == endpoint
        assert row["status"] == "200"
        assert row["payload_bytes"] is not None and row["payload_bytes"] > 0
        assert row["elapsed_ms"] >= 0
        meta = row["meta"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        # /v1/probe/health requires no auth; token may or may not be set
        # depending on env state, but the flags are recorded honestly.
        assert "probe_token_set" in meta
        assert "auth_header_present" in meta

    def test_health_snapshot_wrong_token_records_401(
        self,
        client: TestClient,
        patched_db: _StubBackend,
        token_set: str,
    ) -> None:
        endpoint = f"{PROBE_PREFIX}/health_snapshot"

        async def _setup() -> None:
            await _truncate_measurements()

        asyncio.run(_setup())

        response = client.get(endpoint, headers=_bearer(WRONG_TOKEN))
        assert response.status_code == 401

        async def _wait() -> list[dict[str, Any]]:
            start = time.monotonic()
            while (time.monotonic() - start) < 3.0:
                count = await _count_measurements(endpoint)
                if count >= 1:
                    return await _fetch_measurements(endpoint)
                await asyncio.sleep(0.05)
            raise AssertionError(
                f"timed out: {await _count_measurements(endpoint)} rows for {endpoint}"
            )

        rows = asyncio.run(_wait())
        assert len(rows) == 1
        row = rows[0]
        assert row["endpoint"] == endpoint
        assert row["status"] == "401"
        meta = row["meta"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta["probe_token_set"] is True
        assert meta["auth_header_present"] is True

    def test_health_snapshot_token_unset_records_503(
        self,
        client: TestClient,
        patched_db: _StubBackend,
        token_unset: None,
    ) -> None:
        endpoint = f"{PROBE_PREFIX}/health_snapshot"

        async def _setup() -> None:
            await _truncate_measurements()

        asyncio.run(_setup())

        response = client.get(endpoint, headers=_bearer(VALID_TOKEN))
        assert response.status_code == 503

        async def _wait() -> list[dict[str, Any]]:
            start = time.monotonic()
            while (time.monotonic() - start) < 3.0:
                count = await _count_measurements(endpoint)
                if count >= 1:
                    return await _fetch_measurements(endpoint)
                await asyncio.sleep(0.05)
            raise AssertionError(
                f"timed out: {await _count_measurements(endpoint)} rows for {endpoint}"
            )

        rows = asyncio.run(_wait())
        assert len(rows) == 1
        row = rows[0]
        assert row["endpoint"] == endpoint
        assert row["status"] == "503"
        meta = row["meta"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        # Token is unset -> probe_token_set should be False.
        assert meta["probe_token_set"] is False
        # Auth header WAS supplied by the test but the token-unset gate
        # fires before auth validation; record honestly.
        assert meta["auth_header_present"] is True
