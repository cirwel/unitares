"""Static contract for durable outcome prediction binding."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "db/postgres/migrations/070_outcome_prediction_bindings.sql"


def test_migration_070_uses_unpartitioned_authoritative_binding_table() -> None:
    sql = MIGRATION.read_text()
    assert "CREATE TABLE IF NOT EXISTS audit.outcome_prediction_bindings" in sql
    assert "PRIMARY KEY (agent_id, prediction_id)" in sql
    assert "request_digest" in sql
    assert "canonical_outcome_id" in sql
    assert "canonical_outcome_ts" in sql
    assert "canonical_detail" in sql
    assert "canonical_eisv_snapshot" in sql
    assert "PARTITION BY" not in sql
    assert "VALUES (70, 'outcome_prediction_bindings'" in sql


def test_binding_table_documents_partition_safe_reference() -> None:
    sql = MIGRATION.read_text().lower()
    assert "partition" in sql
    assert "canonical_outcome_ts" in sql
    assert "foreign key" not in sql
