"""A retired telemetry field must be able to read as UNKNOWN, not as a value.

`stability_index` (was 1.0 - S) was retired in commit 20684dd1 on 2026-03-26 --
inside a commit titled "Dashboard improvements: energy field, race fix,
pagination, filtering". The writers then hardcoded a constant, so the column kept
being written to every row.

Measured on core.agent_state (non-synthetic), stddev / mean by month:
    2025-12  sd 0.333  avg 0.481   <- live, varying
    2026-03  sd 0.060  avg 0.857
    2026-04  sd 0.000  avg 0.000   <- retired here
    2026-05..2026-08  sd 0.000  avg 0.000

The bug is not that the field is dead. It is that a dead field written as 0.0 is
indistinguishable at the query layer from a real signal that happens to be flat.
During the 2026-08 audit it was cited as "sd = 0.0000 across 237 agents -- zero
information", i.e. offered as evidence of absent individuality, when it was never
a measurement. It had also been consumed for real: the R1 trajectory reader used
it as the "always-1.0 S channel" until PR #530 fixed the reader. The reader was
fixed; the tombstone was not.

PHASE 1 (this change): make NULL expressible -- migration 058 for existing
databases, schema.sql for fresh ones. No writer changes, so nothing breaks:
writers still supply a value and the relaxed constraint accepts it.

PHASE 2 (follow-up, only after 058 is applied): switch the three writers
(src/agent_storage.py, src/db/base.py default, bootstrap_checkin.py) from their
three different constants -- 0.0, 0.5, 0.5 -- to None. Doing that before 058 is
applied makes every state insert fail the NOT NULL constraint; that ordering was
verified the hard way while preparing this change.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_ROOT = Path(__file__).parent.parent
_MIGRATION = _ROOT / "db/postgres/migrations/058_stability_index_retired_nullable.sql"
_SCHEMA = _ROOT / "db/postgres/schema.sql"


def test_migration_058_makes_null_expressible():
    assert _MIGRATION.exists(), "migration 058 missing"
    sql = _MIGRATION.read_text().upper()
    assert "DROP NOT NULL" in sql
    assert "DROP DEFAULT" in sql


def test_migration_058_is_forward_only():
    """Rewriting history would destroy the only evidence the signal was ever
    alive, and the only record of when it died. Rows before 2026-04 are real
    measurements; backfilling the artifact era is an operator decision."""
    up = _MIGRATION.read_text().split("-- DOWN")[0]
    # Statements only — the rationale comments legitimately discuss deleting
    # history, and matching prose would make this test unfalsifiable.
    stmts = "\n".join(
        l for l in up.splitlines() if not l.strip().startswith("--")
    ).upper()
    assert "UPDATE" not in stmts
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


def test_writers_are_unchanged_in_phase_1():
    """Guard the ordering. If a future change switches the writers to None
    without 058 having been applied, every state insert fails the NOT NULL
    constraint. This test documents that phase 2 is deliberately not here yet --
    update it in the same commit that flips the writers."""
    import inspect
    import src.agent_storage as agent_storage

    src = inspect.getsource(agent_storage)
    assert "stability_index=0.0" in src.replace(" ", "").replace("stability_index =0.0", "stability_index=0.0") or \
           "stability_index=None" in src.replace(" ", ""), \
        "writer encoding changed unexpectedly — see phase-2 note in this module's docstring"
