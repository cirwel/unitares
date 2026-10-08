"""Migration 073: partition_maintenance() keeps the record.

Before 073, audit.partition_maintenance() deleted check-ins (core.agent_state)
older than 90 days and dropped audit.events, audit.tool_usage and
outcome_events months past their retention, every time it ran: weekly, and on
demand when an outcome insert found its partition missing. 073 redefines it
with no retention step; old audit.events and audit.tool_usage months move to
scripts/ops/archive-audit-partitions.py, which drops a month only after its
archive has been read back and counted.

Static half: the migration's shape, the bootstrap copy and the script's
verification. Live half: the function bootstrapped into governance_test.
"""

from __future__ import annotations

import csv
import gzip
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "db/postgres/migrations/073_retention_keep_record.sql"
MIGRATION_055 = ROOT / "db/postgres/migrations/055_audit_partition_utc_normalization.sql"
PARTITIONS = ROOT / "db/postgres/partitions.sql"
SCRIPT = ROOT / "scripts/ops/archive-audit-partitions.py"

RETENTION_CALLS = (
    "cleanup_old_agent_state",
    "drop_old_events_partitions",
    "drop_old_tool_usage_partitions",
    "drop_old_outcome_partitions",
)


def _function(sql: str) -> str:
    start = sql.index("CREATE OR REPLACE FUNCTION audit.partition_maintenance()")
    end = sql.index("$$ LANGUAGE plpgsql;", start)
    return sql[start:end]


def _without_retention(body: str) -> str:
    """055's body with the retention block (old partitions through the
    agent_state cleanup) cut out, for comparing the unchanged remainder."""
    head, rest = body.split("    -- Clean up old partitions\n", 1)
    return head + rest.split("core.cleanup_old_agent_state(90)\n    );\n", 1)[1]


def _without_073_note(body: str) -> str:
    head, rest = body.split("    -- No retention here (migration 073).", 1)
    sessions = rest.index("    -- Clean up expired sessions")
    tail = rest[sessions:].split("core.cleanup_expired_sessions()\n    );\n", 1)[1]
    return head + tail


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------


def test_073_maintenance_calls_no_retention():
    body = _function(MIGRATION.read_text())
    for call in RETENTION_CALLS:
        assert call not in body
    assert "core.cleanup_expired_sessions()" in body


def test_073_keeps_the_rest_of_055s_body_unchanged():
    assert _without_073_note(_function(MIGRATION.read_text())) == _without_retention(
        _function(MIGRATION_055.read_text())
    )


def test_bootstrap_copy_matches_073():
    """partitions.sql runs before 031/045/055 on a fresh install, so 073 is what
    finally defines the function; the bootstrap copy is kept identical to it."""
    assert _function(PARTITIONS.read_text()) == _function(MIGRATION.read_text())


def test_retention_functions_stay_defined_for_the_archive_script():
    bootstrap = PARTITIONS.read_text()
    for name in ("drop_old_events_partitions", "drop_old_tool_usage_partitions"):
        assert f"CREATE OR REPLACE FUNCTION audit.{name}(" in bootstrap


def test_073_is_one_registered_transaction():
    sql = MIGRATION.read_text()
    assert sql.index("BEGIN;") < sql.index("CREATE OR REPLACE FUNCTION") < sql.index("COMMIT;")
    assert "VALUES (73, 'retention_keep_record'" in sql
    assert "ON CONFLICT (version) DO NOTHING" in sql
    for statement in ("DELETE FROM", "DROP TABLE", "TRUNCATE"):
        assert statement not in sql


# ---------------------------------------------------------------------------
# Archive script
# ---------------------------------------------------------------------------


@pytest.fixture()
def archiver():
    spec = importlib.util.spec_from_file_location("archive_audit_partitions", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    with gzip.open(path, "wt", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "details"])
        writer.writerows(rows)


def test_count_counts_records_not_lines(archiver, tmp_path):
    path = tmp_path / "p.csv.gz"
    _write_csv(path, [["1", '{"note": "two\nlines"}'], ["2", "plain"]])
    assert archiver.count_csv_records(path) == 2


SOURCE = {"system_identifier": "7608", "database": "governance", "oid": 28353}
BOUND = "FOR VALUES FROM ('a') TO ('b')"


def _archive(archiver, tmp_path, source=SOURCE, bound=BOUND):
    path = tmp_path / "events_2026_03.csv.gz"
    _write_csv(path, [["1", "x"]])
    entry = {"partition": "events_2026_03", "rows": 1, "sha256": archiver.sha256(path),
             "source": source, "bound": bound}
    (tmp_path / "manifest.jsonl").write_text(json.dumps(entry) + "\n")
    return path, entry


def test_count_handles_fields_beyond_the_csv_default_limit(archiver, tmp_path):
    path = tmp_path / "big.csv.gz"
    _write_csv(path, [["1", "x" * 300_000], ["2", "y"]])
    assert archiver.count_csv_records(path) == 2


def test_archive_is_reused_only_for_the_same_table(archiver, tmp_path):
    _, entry = _archive(archiver, tmp_path)
    assert archiver.archived_entry(tmp_path, "events_2026_03", SOURCE, BOUND) == entry


@pytest.mark.parametrize("source, bound", [
    ({**SOURCE, "database": "governance_test"}, BOUND),   # another database
    ({**SOURCE, "oid": 99999}, BOUND),                     # recreated table
    ({**SOURCE, "system_identifier": "1"}, BOUND),         # another cluster
    (SOURCE, "FOR VALUES FROM ('c') TO ('d')"),            # other bounds
])
def test_an_archive_of_another_table_is_refused_not_trusted(archiver, tmp_path, source, bound):
    path, _ = _archive(archiver, tmp_path)
    before = path.read_bytes()
    with pytest.raises(SystemExit):
        archiver.archived_entry(tmp_path, "events_2026_03", source, bound)
    assert path.read_bytes() == before  # left in place, not overwritten


def test_a_changed_archive_file_is_refused(archiver, tmp_path):
    path, _ = _archive(archiver, tmp_path)
    _write_csv(path, [["1", "changed"]])
    with pytest.raises(SystemExit):
        archiver.archived_entry(tmp_path, "events_2026_03", SOURCE, BOUND)


def test_no_archive_file_means_export(archiver, tmp_path):
    assert archiver.archived_entry(tmp_path, "events_2026_03", SOURCE, BOUND) is None


def test_dry_run_is_the_default_and_writes_nothing(archiver, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        archiver, "eligible",
        lambda dsn, parent, days: [(f"{parent}_2026_03", "FOR VALUES FROM ('a') TO ('b')")],
    )

    def no_writes(*args, **kwargs):
        raise AssertionError("dry run must not export or drop")

    monkeypatch.setattr(archiver, "export", no_writes)
    monkeypatch.setattr(archiver, "drop", no_writes)
    out_dir = tmp_path / "archive"
    assert archiver.main(["--archive-dir", str(out_dir)]) == 0
    assert not out_dir.exists()
    assert "dry run" in capsys.readouterr().out


def test_export_without_apply_never_drops(archiver, tmp_path, monkeypatch):
    monkeypatch.setattr(
        archiver, "eligible",
        lambda dsn, parent, days: [(f"{parent}_2026_03", "b")] if parent == "events" else [],
    )
    monkeypatch.setattr(archiver, "source_of", lambda dsn, name: SOURCE)
    exported = []

    def fake_export(dsn, name, out_dir):
        exported.append(name)
        _write_csv(out_dir / f"{name}.csv.gz", [["1", "x"]] * 3)
        return {"partition": name, "rows": 3, "bytes": 1,
                "sha256": archiver.sha256(out_dir / f"{name}.csv.gz"),
                "file": f"{name}.csv.gz"}

    monkeypatch.setattr(archiver, "export", fake_export)
    dropped = []
    monkeypatch.setattr(archiver, "drop",
                        lambda dsn, name, rows, oid: dropped.append((name, rows, oid)))
    assert archiver.main(["--export", "--archive-dir", str(tmp_path)]) == 0
    assert dropped == []
    # --apply reuses the archive just written (same source, same bound) and
    # drops with the archived count and the OID it was taken from.
    assert archiver.main(["--apply", "--archive-dir", str(tmp_path)]) == 0
    assert exported == ["events_2026_03"]
    assert dropped == [("events_2026_03", 3, SOURCE["oid"])]


def test_unexpected_partition_names_are_refused(archiver, tmp_path, monkeypatch):
    monkeypatch.setattr(archiver, "eligible", lambda dsn, parent, days: [("x; drop", "b")])
    with pytest.raises(SystemExit):
        archiver.main(["--archive-dir", str(tmp_path)])


# ---------------------------------------------------------------------------
# Live, against governance_test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrapped_maintenance_deletes_nothing(live_postgres_backend):
    async with live_postgres_backend.acquire() as conn:
        definition = await conn.fetchval(
            "SELECT pg_get_functiondef('audit.partition_maintenance()'::regprocedure)"
        )
    assert "No retention here (migration 073)" in definition, (
        "governance_test carries a pre-073 partition_maintenance(); another "
        "worktree's bootstrap may have re-applied 055 after this one applied 073."
    )
    for call in RETENTION_CALLS:
        assert call not in definition
