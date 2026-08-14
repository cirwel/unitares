"""Unit tests for scripts/dev/apply_migrations.py.

The script's I/O (psql subprocess calls) touches the live machine, which is
brittle in CI. These tests exercise the pure planning logic — ``compute_plan``,
which decides what is pending vs. drifted — using fake registry dicts.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = REPO_ROOT / "scripts" / "dev"
SCRIPT = SCRIPT_DIR / "apply_migrations.py"


@pytest.fixture(scope="module")
def mod():
    # The script does `from unitares_doctor import ...` at top level; make that
    # resolvable the same way running it as a script (sys.path[0]) would.
    sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location("apply_migrations", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["apply_migrations"] = module
    spec.loader.exec_module(module)
    return module


def test_clean_registry_has_no_pending_or_drift(mod):
    expected = {1: "a", 2: "b"}
    actual = {1: "a", 2: "b"}
    pending, mismatches, unexpected = mod.compute_plan(expected, actual)
    assert pending == []
    assert mismatches == []
    assert unexpected == []


def test_pending_is_source_versions_absent_from_db(mod):
    expected = {1: "a", 2: "b", 3: "c"}
    actual = {1: "a", 2: "b"}
    pending, mismatches, unexpected = mod.compute_plan(expected, actual)
    assert pending == [3]
    assert mismatches == []
    assert unexpected == []


def test_pending_is_sorted(mod):
    expected = {1: "a", 5: "e", 3: "c", 2: "b"}
    actual = {1: "a"}
    pending, _, _ = mod.compute_plan(expected, actual)
    assert pending == [2, 3, 5]


def test_name_mismatch_is_flagged_not_pending(mod):
    expected = {1: "a", 2: "renamed"}
    actual = {1: "a", 2: "old_name"}
    pending, mismatches, unexpected = mod.compute_plan(expected, actual)
    assert pending == []
    assert mismatches == [2]
    assert unexpected == []


def test_db_version_with_no_source_file_is_unexpected(mod):
    expected = {1: "a"}
    actual = {1: "a", 99: "mystery"}
    pending, mismatches, unexpected = mod.compute_plan(expected, actual)
    assert unexpected == [99]
    assert pending == []
    assert mismatches == []


def test_known_exception_is_not_reported_unexpected(mod):
    expected = {1: "a"}
    actual = {1: "a", 18: "progress flat telemetry tables"}
    exceptions = {18: "progress flat telemetry tables"}
    pending, mismatches, unexpected = mod.compute_plan(expected, actual, exceptions)
    assert unexpected == []
    assert pending == []
    assert mismatches == []


def test_empty_db_makes_all_source_pending(mod):
    expected = {1: "a", 2: "b"}
    actual: dict[int, str] = {}
    pending, mismatches, unexpected = mod.compute_plan(expected, actual)
    assert pending == [1, 2]
    assert mismatches == []
    assert unexpected == []


# ── --check preflight gate ───────────────────────────────────────────────────
# --check must exit non-zero on ANYTHING the DB-is-not-ready-for-this-code: a
# pending migration (which the default dry-run treats as exit 0), a name
# mismatch, or a DB version with no source file. It exits 0 only when fully in
# sync. We drive main() with stubbed registry I/O so no live DB is needed.


def _run_check(mod, monkeypatch, expected, actual, content_drift=()):
    monkeypatch.setattr(mod, "_source_schema_migrations", lambda _root: expected)
    monkeypatch.setattr(mod, "query_applied", lambda _url: actual)
    monkeypatch.setattr(mod, "KNOWN_SCHEMA_MIGRATION_EXCEPTIONS", {})
    # Content anchoring reads the registry a second time; stub it too or the
    # run does live I/O against the fake DSN.
    monkeypatch.setattr(mod, "checksum_refusals", lambda _url: list(content_drift))
    return mod.main(["--check", "--db-url", "postgresql://x/y"])


def test_check_passes_when_in_sync(mod, monkeypatch):
    assert _run_check(mod, monkeypatch, {1: "a", 2: "b"}, {1: "a", 2: "b"}) == 0


def test_check_fails_on_pending(mod, monkeypatch):
    # The default dry-run returns 0 here; --check must block instead.
    assert _run_check(mod, monkeypatch, {1: "a", 2: "b"}, {1: "a"}) == 1


def test_check_fails_on_name_mismatch(mod, monkeypatch):
    assert _run_check(mod, monkeypatch, {1: "a", 2: "renamed"}, {1: "a", 2: "old"}) == 1


def test_check_fails_on_unexpected_db_version(mod, monkeypatch):
    assert _run_check(mod, monkeypatch, {1: "a"}, {1: "a", 99: "mystery"}) == 1


# ── content drift (migration 062 checksums) ──────────────────────────────────
# A registered version whose FILE has since changed. Distinct from a name
# mismatch: the name can agree while the SQL does not, which is exactly how
# migration 034 shipped 3-of-4 CHECK constraints to production for three months.


def test_check_fails_on_content_drift(mod, monkeypatch):
    """In sync by version AND name, but a file changed after it was applied."""
    assert _run_check(
        mod, monkeypatch, {1: "a"}, {1: "a"},
        content_drift=["version 1 (001_a.sql): recorded aaa… but file is bbb…"],
    ) == 1


def test_check_passes_when_content_anchored(mod, monkeypatch):
    assert _run_check(mod, monkeypatch, {1: "a"}, {1: "a"}, content_drift=[]) == 0


def test_sql_literal_escapes_quotes(mod):
    assert mod._sql_literal("ab'c") == "'ab''c'"


def test_record_checksum_tolerates_pre_062_database(mod, monkeypatch, tmp_path):
    """An older deployment has no checksum column; that must not fail the apply."""
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = 'ERROR:  column "checksum" of relation "schema_migrations" does not exist'

    p = tmp_path / "010_x.sql"
    p.write_text("SELECT 1;")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Proc())
    assert mod.record_checksum("postgresql://x/y", 10, p) is True


def test_record_checksum_reports_other_failures(mod, monkeypatch, tmp_path):
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "could not connect to server"

    p = tmp_path / "010_x.sql"
    p.write_text("SELECT 1;")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Proc())
    assert mod.record_checksum("postgresql://x/y", 10, p) is False


class TestEveryMigrationRegistersItself:
    """A migration file that does not INSERT its own schema_migrations row is
    INERT — and silently so, which is the dangerous part.

    `unitares_doctor._source_schema_migrations` builds the expected version set
    by parsing `INSERT INTO core.schema_migrations ... VALUES (n, 'name')` out
    of each file. A migration with no such INSERT is never in the expected set,
    so it is never reported pending, never applied by `--apply`, and `--check`
    happily reports "in sync" while the schema change it carries is absent.

    2026-08-14: migration 063 shipped without the INSERT. deploy-mcp.sh's
    preflight passed ("DB at version 62 ... max 62 — OK") and restarted the
    governance MCP onto code that writes regime='TRANSITION' and coerces
    out-of-set values to 'unknown', against a CHECK constraint that allowed
    neither. The preflight exists precisely to stop a code/schema half-deploy
    and it could not see the gap, because the gap was in its own input.
    """

    def test_every_migration_file_inserts_its_own_version_row(self):
        import re
        from pathlib import Path

        migrations = Path(__file__).resolve().parents[1] / "db" / "postgres" / "migrations"
        insert_re = re.compile(
            r"INSERT\s+INTO\s+core\.schema_migrations", re.IGNORECASE
        )

        missing = [
            p.name
            for p in sorted(migrations.glob("[0-9][0-9][0-9]_*.sql"))
            if not insert_re.search(p.read_text())
        ]
        assert not missing, (
            "these migrations never register themselves, so the deploy preflight "
            "cannot see them and they will never be applied: " + ", ".join(missing)
        )

    def test_registered_version_matches_the_filename_number(self):
        """A registration row whose version disagrees with the filename is the
        same failure wearing a disguise: the file looks registered, but it
        claims a slot the deploy tooling maps to different SQL."""
        import re
        from pathlib import Path

        migrations = Path(__file__).resolve().parents[1] / "db" / "postgres" / "migrations"
        value_re = re.compile(r"VALUES\s*\(\s*(\d+)\s*,", re.IGNORECASE)

        mismatched = []
        for p in sorted(migrations.glob("[0-9][0-9][0-9]_*.sql")):
            text = p.read_text()
            if "core.schema_migrations" not in text:
                continue
            declared = {int(m) for m in value_re.findall(text)}
            from_name = int(p.name[:3])
            if declared and from_name not in declared:
                mismatched.append(f"{p.name} registers {sorted(declared)}")
        assert not mismatched, "filename/registration mismatch: " + "; ".join(mismatched)
