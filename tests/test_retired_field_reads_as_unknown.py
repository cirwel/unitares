"""A retired metric must read as unknown, not as a plausible constant.

`core.agent_state.stability_index` was retired 2026-03-26 (commit 20684dd1)
but kept its `NOT NULL DEFAULT 0.5`, so every row since carried a
plausible-looking float that was never measured — the "fails toward healthy,
never toward unknown" pattern. Migration 058 (058_retire_stability_index.sql,
merged via #1525) makes NULL expressible, nulls only the post-retirement
hardcoded sentinels (bounded by BOTH the retirement date AND the two known
constants, so a real measurement cannot be caught), and the writers now omit
the column entirely.

These tests pin that end state so it cannot silently regress: the migration
stays value-and-date bounded (real pre-retirement measurements are history,
not backfill targets), fresh installs agree with migrated databases, and no
writer quietly reintroduces the column.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_ROOT = Path(__file__).parent.parent
_MIGRATION = _ROOT / "db/postgres/migrations/058_retire_stability_index.sql"
_SCHEMA = _ROOT / "db/postgres/schema.sql"


def _statements(sql: str) -> str:
    """SQL with comment lines stripped — rationale prose legitimately mentions
    destructive words, and matching prose would make these tests unfalsifiable."""
    return "\n".join(
        l for l in sql.splitlines() if not l.strip().startswith("--")
    ).upper()


def test_migration_058_makes_null_expressible():
    assert _MIGRATION.exists(), "migration 058 missing"
    sql = _MIGRATION.read_text().upper()
    assert "DROP NOT NULL" in sql
    assert "DROP DEFAULT" in sql


def test_migration_058_backfill_cannot_touch_real_measurements():
    """The sentinel backfill must stay double-bounded: by the retirement date
    AND by the two known hardcoded constants. Either bound alone could be
    wrong; together a genuine pre-retirement measurement is unreachable.
    Rewriting real history would destroy the only evidence the signal was
    ever alive."""
    stmts = _statements(_MIGRATION.read_text())
    assert "UPDATE" in stmts, "expected the bounded sentinel backfill"
    assert "2026-03-26" in stmts, "date bound missing from the backfill"
    assert "IN (0.0, 0.5)" in stmts, "value bound missing from the backfill"
    assert "DELETE" not in stmts
    assert "DROP COLUMN" not in stmts
    assert "TRUNCATE" not in stmts


def test_fresh_installs_match_migrated_databases():
    """schema.sql and the migration must agree, or a fresh install silently
    reintroduces the constraint that migration 058 removes."""
    schema = _SCHEMA.read_text()
    line = next(
        (l for l in schema.splitlines()
         if l.strip().startswith("stability_index") and "REAL" in l),
        None,
    )
    assert line is not None, "stability_index column not found in schema.sql"
    assert "NOT NULL" not in line.upper(), (
        "schema.sql still declares stability_index NOT NULL; a fresh install "
        "would diverge from a database migrated by 058"
    )


def test_writers_no_longer_reference_the_retired_column():
    """Phase 2 landed with #1525: the writers omit the column entirely, so new
    rows land as NULL. The actual core.agent_state INSERT column lists live in
    db/mixins/state.py and identity/bootstrap_checkin.py (agent_storage only
    wraps them) — scan all three, ignoring comment lines, which legitimately
    document the deliberate omission."""
    writer_files = [
        _ROOT / "src/agent_storage.py",
        _ROOT / "src/db/mixins/state.py",
        _ROOT / "src/mcp_handlers/identity/bootstrap_checkin.py",
    ]
    for f in writer_files:
        code = "\n".join(
            l for l in f.read_text().splitlines()
            if not l.strip().startswith("#")
        )
        assert "stability_index" not in code, (
            f"{f.name} references stability_index in non-comment code — the "
            "column is retired (migration 058); read S from entropy instead"
        )
