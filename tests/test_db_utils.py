"""
Shared utilities for live PostgreSQL integration tests.

Use when tests run against governance_test and need schema bootstrap,
connectivity checks, or table cleanup. Other tests use mocks and bypass real DB.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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

        # Bring older test DBs forward to current backend assumptions.
        await _execute_sql_file(conn, "db/postgres/migrations/004_outcome_events.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/005_agent_baselines.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/006_thread_identity.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/007_epochs.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/008_dashboard_matview.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/009_quorum_support.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/010_trigger_source.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/011_behavioral_baselines.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/012_identity_last_activity_at.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/013_metrics_series.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/014_seed_epoch_2.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/017_substrate_claims.sql")
        # Slots 018 and 019 were renumbered to 022 and 023 in PR #236
        # (migration-drift triage); the old paths no longer exist.
        await _execute_sql_file(conn, "db/postgres/migrations/022_bootstrap_synthetic_state.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/023_matview_measured_only.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/020_progress_flat_telemetry.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/021_seed_epoch_3.sql")
        # Surface lease plane (RFC docs/proposals/surface-lease-plane-v0.md):
        # 024 + 025 build the lease_plane schema + immutability triggers; 026 adds
        # storage-layer grammar CHECK + generated surface_kind column (PR 1, this branch).
        await _execute_sql_file(conn, "db/postgres/migrations/024_lease_plane.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/025_lease_plane_invariants.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/026_lease_plane_grammar.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/027_lease_plane_deprecation.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/028_lease_plane_trigger_fix.sql")
        await _execute_sql_file(conn, "db/postgres/migrations/029_lease_plane_earned_status_guard.sql")

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


# Tables to truncate for test isolation. Order respects FK constraints.
TRUNCATE_TABLES = [
    "core.dialectic_messages",
    "core.dialectic_sessions",
    "core.agent_state",
    "core.agent_sessions",
    "core.agent_baselines",
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
