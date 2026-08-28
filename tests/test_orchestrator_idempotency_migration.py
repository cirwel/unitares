"""Static contract for the durable agent-orchestrator idempotency ledger."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "db/postgres/migrations/068_agent_orchestrator_idempotency.sql"


def test_migration_068_is_registered_bounded_and_stateful() -> None:
    sql = MIGRATION.read_text()
    assert "orchestration.spawn_idempotency" in sql
    assert "key_hash       TEXT PRIMARY KEY" in sql
    assert "state IN ('reserved', 'started')" in sql
    assert "idx_spawn_idempotency_expires_at" in sql
    assert "VALUES (68, 'agent_orchestrator_idempotency'" in sql


def test_ledger_stores_no_spawn_material_or_raw_key() -> None:
    table_sql = MIGRATION.read_text().split("CREATE TABLE", 1)[1].split(");", 1)[0]
    forbidden_columns = ("raw_key", "command", "args", "environment", "output", "secret")
    assert all(column not in table_sql.lower() for column in forbidden_columns)


def test_reserved_rows_cannot_claim_started_without_a_timestamp() -> None:
    sql = MIGRATION.read_text()
    assert "state = 'reserved' AND started_at IS NULL" in sql
    assert "state = 'started' AND started_at IS NOT NULL" in sql
