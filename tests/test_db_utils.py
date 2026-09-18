"""
Shared utilities for live PostgreSQL integration tests.

Use when tests run against governance_test and need schema bootstrap,
connectivity checks, or table cleanup. Other tests use mocks and bypass real DB.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

try:
    import asyncpg
except ImportError:
    asyncpg = None  # type: ignore

_PROJECT_ROOT = Path(__file__).parent.parent
TEST_DB_URL = "postgresql://postgres:postgres@localhost:5432/governance_test"
_SCHEMA_READY = False


def can_connect_to_test_db() -> bool:
    """
    Check if governance_test database is reachable.

    Runs in a separate thread to avoid nesting event loops when called from
    async fixtures (pytest-asyncio).
    """
    if asyncpg is None:
        return False

    def _run() -> bool:
        loop = asyncio.new_event_loop()
        try:
            conn = loop.run_until_complete(asyncpg.connect(TEST_DB_URL, timeout=3))
            loop.run_until_complete(conn.close())
            return True
        except Exception:
            return False
        finally:
            loop.close()

    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_run).result(timeout=5)
    except Exception:
        return False


async def _execute_sql_file(conn, relative_path: str) -> None:
    """Execute a SQL file from repo root against the active connection."""
    sql_path = _PROJECT_ROOT / relative_path
    sql = sql_path.read_text(encoding="utf-8")
    await conn.execute(sql)



async def _report_foreign_migrations(conn) -> None:
    """Name migrations the shared test database carries that this tree does not.

    `governance_test` is SHARED across every worktree and is never reset, while
    this bootstrap applies every migration in the CURRENT checkout. The database
    therefore accumulates the union of every branch that has ever run tests, and
    an unmerged branch's migration silently becomes part of the schema that
    master's tests read.

    That is not hypothetical. On 2026-08-28 migration 069
    (`lease_plane_topic_messages`, unmerged) seeded a `topic` row into
    `lease_plane.surface_kind_catalog`. A migration-027 test asserting the exact
    catalog contents then failed on master and on every other branch at once —
    for a reason present in neither the test nor the code under it. It read as a
    flake, which is the expensive part: a cross-branch schema fact wearing the
    costume of nondeterminism gets retried, not diagnosed.

    `core.schema_migrations` already records what was applied. Nothing read it.
    This does — loudly and by name, so the next such failure is legible at the
    moment it happens.

    Reporting only, never mutating. Dropping or rewinding a database that other
    worktrees and agents are concurrently using would trade a confusing failure
    for a destructive one. The remedy is the operator's call, and it is printed
    rather than taken.
    """
    try:
        rows = await conn.fetch("SELECT version, name FROM core.schema_migrations")
    except Exception:  # noqa: BLE001 — table absent on a fresh DB; nothing to compare
        return

    on_disk = set()
    for path in (_PROJECT_ROOT / "db/postgres/migrations").glob("*.sql"):
        head = path.name.split("_", 1)[0]
        if head.isdigit():
            on_disk.add(int(head))

    foreign = sorted(
        (r["version"], r["name"]) for r in rows
        if r["version"] is not None and int(r["version"]) not in on_disk
    )
    if not foreign:
        return

    listed = ", ".join(f"{v:03d}_{n}" for v, n in foreign)
    print(
        f"[test-db-bootstrap] WARNING: governance_test carries {len(foreign)} "
        f"migration(s) absent from this checkout: {listed}. "
        "The shared test database is AHEAD of this branch — another worktree "
        "applied them. A DB-backed test failing here may be reading schema or "
        "seed rows this branch does not create; check that before treating the "
        "failure as flaky. To rebuild from this checkout alone: "
        "dropdb governance_test && createdb governance_test (destructive, and "
        "it will disrupt any other worktree mid-run)."
    )

async def ensure_test_database_schema() -> None:
    """
    Ensure governance_test has the schema expected by PostgresBackend and related tests.

    Idempotent: safe to call multiple times. Creates core/knowledge schemas if missing,
    applies migrations, partitions, and AGE graph. Use before any live-DB integration test.
    """
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return

    if asyncpg is None:
        raise ImportError("asyncpg is required for test DB bootstrap. pip install asyncpg")

    conn = await asyncpg.connect(TEST_DB_URL, timeout=5)
    try:
        has_core_schema = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = 'core')"
        )
        if not has_core_schema:
            await _execute_sql_file(conn, "db/postgres/schema.sql")

        has_knowledge_discoveries = await conn.fetchval(
            """
            SELECT EXISTS(
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'knowledge' AND table_name = 'discoveries'
            )
            """
        )
        if not has_knowledge_discoveries:
            await _execute_sql_file(conn, "db/postgres/knowledge_schema.sql")

        # Apply EVERY migration on disk, in numeric order.
        #
        # This was a hand-maintained list of `_execute_sql_file` calls, and it
        # had drifted 22 migrations behind `db/postgres/migrations/` — 038
        # (agent_state.risk_score) through 058, plus 015/016/037. The result was
        # a test database whose schema was not the one the migrations produce,
        # so `tests/test_postgres_backend_integration.py` failed on a fresh DB
        # with `column "risk_score" does not exist` while passing locally on a
        # long-lived `governance_test` that had drifted into correctness.
        #
        # A list that must be appended to by hand every time someone adds a
        # migration will fall behind again; a scan cannot. Migrations are
        # numerically prefixed, so sorted order is apply order, and each is
        # written to be re-runnable (this bootstrap re-applies on every call).
        for migration in sorted(
            (_PROJECT_ROOT / "db/postgres/migrations").glob("*.sql"),
            key=lambda p: p.name,
        ):
            try:
                await _execute_sql_file(conn, f"db/postgres/migrations/{migration.name}")
            except Exception as exc:  # noqa: BLE001
                # Loud, not fatal: one non-idempotent migration must not block
                # every DB-backed test, but it must not pass silently either —
                # a swallowed failure here is how the schema drifted in the
                # first place.
                print(
                    f"[test-db-bootstrap] migration {migration.name} did not apply: "
                    f"{type(exc).__name__}: {exc}"
                )

        await _report_foreign_migrations(conn)

        # Ensure partitioned audit tables can accept inserts for current month.
        await _execute_sql_file(conn, "db/postgres/partitions.sql")

        # Ensure AGE graph exists for graph integration tests.
        try:
            await conn.execute("LOAD 'age'")
            await conn.execute("SET search_path = ag_catalog, core, audit, public")
            graph_exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1)",
                "governance_graph",
            )
            if not graph_exists:
                await conn.execute("SELECT * FROM ag_catalog.create_graph('governance_graph')")
        except Exception:
            # AGE can be unavailable in some environments; graph tests will surface it.
            pass

        _SCHEMA_READY = True
    finally:
        await conn.close()


# Tables to truncate for test isolation. Order respects FK constraints, and
# the list must name every table CASCADE would reach. A table left off the
# list still gets truncated -- CASCADE finds it -- but it is then locked in an
# order Postgres derives at runtime rather than one this list declares, which
# is how a TRUNCATE and a concurrent FK-checking DELETE can take the same two
# tables in opposite orders. `test_truncate_tables_covers_cascade_reachable`
# fails if a new foreign key adds a child that is not named here.
TRUNCATE_TABLES = [
    "coordination.session_resolution_sagas",
    "core.dialectic_messages",
    "core.dialectic_sessions",
    "core.agent_state",
    "core.agent_sessions",
    "core.agent_baselines",
    "core.agent_behavioral_baselines",
    "core.agent_process_bindings",
    "core.substrate_claims",
    "core.session_bindings",
    "core.onboard_pins",
    "core.sessions",
    "core.identities",
    "core.agents",
    "core.threads",
    "core.discovery_embeddings",
    "audit.events",
    "audit.tool_usage",
    "audit.outcome_events",
    "knowledge.discovery_edges",
    "knowledge.discovery_tags",
    "knowledge.discoveries",
]

TRUNCATE_SQL = f"TRUNCATE {', '.join(TRUNCATE_TABLES)} CASCADE"

async def truncate_test_tables(conn, *, attempts: int = 3) -> None:
    """Run the per-test TRUNCATE, retrying a deadlock and reporting its DETAIL.

    Two reasons this is not a plain `conn.execute(TRUNCATE_SQL)`:

    A deadlock is a *transient* condition by definition -- Postgres picks a
    victim arbitrarily and the documented remedy is to retry. Failing a build
    on one is reporting the coin flip, not a defect. This is test-harness
    scope only: TRUNCATE is never issued by the server, so nothing in
    production can reach this path, and a retry here cannot mask a product
    bug.

    The DETAIL line is the reason a deadlock is diagnosable at all -- it names
    both processes and the statements they were holding. On 2026-09-17 a
    deadlock took `tests/db/test_lineage_lifecycle_storage.py` down in CI and
    the log was gone by the time anyone looked, leaving no way to tell which
    statement was the other side. Printing it means the next one is
    actionable even if it never reproduces locally.
    """
    for attempt in range(1, attempts + 1):
        try:
            await conn.execute(TRUNCATE_SQL)
            return
        except Exception as exc:  # asyncpg.exceptions.DeadlockDetectedError
            if type(exc).__name__ != "DeadlockDetectedError" or attempt == attempts:
                raise
            print(
                f"[test-db] TRUNCATE deadlocked (attempt {attempt}/{attempts}); "
                f"retrying. server detail: {getattr(exc, 'detail', None)!r}"
            )
            await asyncio.sleep(0.25 * attempt)


CALIBRATION_RESET_SQL = """
    INSERT INTO core.calibration (id, data, version)
    VALUES (TRUE, '{}', 1)
    ON CONFLICT (id) DO UPDATE SET data = '{}', version = 1
"""


# -----------------------------------------------------------------------------
# Unit tests (no DB required)
# -----------------------------------------------------------------------------


def test_truncate_sql_includes_all_tables():
    """TRUNCATE_SQL should reference all tables in TRUNCATE_TABLES."""
    for table in TRUNCATE_TABLES:
        assert table in TRUNCATE_SQL, f"Missing {table} in TRUNCATE_SQL"


@pytest.mark.asyncio
async def test_truncate_tables_covers_cascade_reachable():
    """Every table CASCADE would reach must be named in TRUNCATE_TABLES.

    An unlisted child is still truncated, so nothing fails visibly -- but it
    is locked in an order Postgres derives rather than one this list declares.
    `core.agent_behavioral_baselines`, `core.agent_process_bindings` and
    `core.substrate_claims` sat in that state and are the reason this check
    exists. The point is to fail when a *new* foreign key adds another.
    """
    if not can_connect_to_test_db():
        pytest.skip("governance_test database not available")

    import asyncpg

    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT conrelid::regclass::text AS child
              FROM pg_constraint
             WHERE contype = 'f'
               AND confrelid::regclass::text = ANY($1::text[])
            """,
            TRUNCATE_TABLES,
        )
    finally:
        await conn.close()

    listed = set(TRUNCATE_TABLES)
    missing = sorted(r["child"] for r in rows if r["child"] not in listed)
    assert not missing, (
        "foreign-key children reachable by TRUNCATE ... CASCADE but not named "
        f"in TRUNCATE_TABLES: {missing}. Add them (children before parents) so "
        "the lock order stays declared rather than derived."
    )


@pytest.mark.asyncio
async def test_truncate_retries_a_deadlock_then_succeeds():
    """The retry must actually fire, not just exist."""

    class _Deadlock(Exception):
        __name__ = "DeadlockDetectedError"
        detail = "Process 1 waits for AccessExclusiveLock; Process 2 waits ..."

    _Deadlock.__name__ = "DeadlockDetectedError"

    class _Conn:
        def __init__(self):
            self.calls = 0

        async def execute(self, sql):
            self.calls += 1
            if self.calls == 1:
                raise _Deadlock()
            return "TRUNCATE"

    conn = _Conn()
    await truncate_test_tables(conn, attempts=3)
    assert conn.calls == 2


@pytest.mark.asyncio
async def test_truncate_gives_up_after_the_last_attempt():
    """A persistent deadlock must still fail, not loop or pass silently."""

    class _Deadlock(Exception):
        detail = None

    _Deadlock.__name__ = "DeadlockDetectedError"

    class _Conn:
        def __init__(self):
            self.calls = 0

        async def execute(self, sql):
            self.calls += 1
            raise _Deadlock()

    conn = _Conn()
    with pytest.raises(_Deadlock):
        await truncate_test_tables(conn, attempts=2)
    assert conn.calls == 2


@pytest.mark.asyncio
async def test_truncate_does_not_retry_other_errors():
    """Only a deadlock is transient; everything else must surface at once."""

    class _Conn:
        def __init__(self):
            self.calls = 0

        async def execute(self, sql):
            self.calls += 1
            raise RuntimeError("undefined_table")

    conn = _Conn()
    with pytest.raises(RuntimeError):
        await truncate_test_tables(conn, attempts=3)
    assert conn.calls == 1
