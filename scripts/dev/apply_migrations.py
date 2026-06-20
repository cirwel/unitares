#!/usr/bin/env python3
"""Apply pending UNITARES Postgres migrations.

Runs the numbered SQL files in ``db/postgres/migrations/`` whose version is on
disk but not yet recorded in ``core.schema_migrations``, in version order,
through ``psql`` — exactly as a human would by hand (``psql -f``), into the same
table. It is a governed *executor* for the existing migration convention, **not
a new migration layer**: it owns no version table of its own and changes no
apply semantics. Drift *detection* stays owned by ``unitares_doctor.py``; this
tool imports the doctor's source/registry parsers so the two never diverge.

Safety posture (DDL on the governance DB is a deliberate, approved action):

* **Dry-run by default.** Without ``--apply`` it only reports what *would* run.
  The explicit ``--apply`` flag is the operator's approval of the DDL.
* **Refuses on registry drift.** A recorded migration whose name disagrees with
  its source file (``mismatch``), or a DB version with no source file
  (``unexpected`` — you are on a stale checkout), blocks ``--apply`` until the
  checkout/DB are reconciled.
* **Per-file verification.** After each file it confirms the
  ``core.schema_migrations`` row landed before continuing; it aborts (leaving
  later files unapplied) on the first failure.
* **Idempotent by construction.** Migration files self-register with
  ``ON CONFLICT (version) DO NOTHING`` and use ``IF NOT EXISTS`` DDL, and this
  tool only runs files whose version is absent from the registry — so a re-run
  is a no-op.

Usage::

    python3 scripts/dev/apply_migrations.py             # dry run (report only)
    python3 scripts/dev/apply_migrations.py --apply      # apply pending migrations
    python3 scripts/dev/apply_migrations.py --db-url postgresql://...

Exit status is non-zero on refusal (drift / missing file) or on any
apply/verify failure.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# scripts/dev is on sys.path[0] when this runs as a script; import the doctor's
# single-sourced parsers so detection logic never forks from the CI gate.
from unitares_doctor import (
    DEFAULT_DB_URL,
    KNOWN_SCHEMA_MIGRATION_EXCEPTIONS,
    _parse_schema_migration_rows,
    _redact,
    _source_schema_migrations,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "db" / "postgres" / "migrations"


def compute_plan(
    expected: dict[int, str],
    actual: dict[int, str],
    exceptions: dict[int, str] | None = None,
) -> tuple[list[int], list[int], list[int]]:
    """Return (pending, mismatches, unexpected) version lists.

    * ``pending`` — versions defined by source files but not yet in the DB.
    * ``mismatches`` — versions in both whose recorded name != source name.
    * ``unexpected`` — versions in the DB with no source file (excluding the
      doctor's accepted history), i.e. the checkout is behind the deployed DB.
    """
    accepted = {**(exceptions or {}), **expected}
    pending = sorted(v for v in expected if v not in actual)
    mismatches = sorted(v for v in expected if v in actual and actual[v] != expected[v])
    unexpected = sorted(v for v in actual if v not in accepted)
    return pending, mismatches, unexpected


def query_applied(db_url: str) -> dict[int, str]:
    """Return {version: name} currently recorded in core.schema_migrations."""
    proc = subprocess.run(
        ["psql", db_url, "-Atqc",
         "SELECT version || '|' || name FROM core.schema_migrations ORDER BY version"],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"error: core.schema_migrations not queryable at {_redact(db_url)}\n"
            f"{proc.stderr.strip()}"
        )
    return _parse_schema_migration_rows(proc.stdout)


def file_for_version(version: int) -> Path | None:
    """Return the migration file for a version, or None if absent."""
    matches = sorted(MIGRATIONS_DIR.glob(f"{version:03d}_*.sql"))
    return matches[0] if matches else None


def apply_file(db_url: str, path: Path) -> bool:
    """Run one migration file through psql, stopping on the first SQL error.

    Uses plain ``psql -f`` (no wrapping transaction) to match the documented
    manual path exactly — some migrations (e.g. CREATE INDEX CONCURRENTLY)
    cannot run inside a single transaction.
    """
    proc = subprocess.run(
        ["psql", db_url, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(path)],
        capture_output=True, text=True,
    )
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.returncode != 0:
        print(f"  FAILED: {path.name}\n{proc.stderr.strip()}", file=sys.stderr)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply pending UNITARES Postgres migrations (dry-run by default).",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually apply pending migrations (default: report only)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="preflight gate: exit non-zero if the DB is not fully in sync with "
             "the source manifest (any pending migration or drift). Unlike the "
             "default dry-run — which exits 0 when migrations are merely pending "
             "— this is meant to gate a deploy from restarting code that expects "
             "an unapplied schema.",
    )
    parser.add_argument(
        "--db-url", default=DEFAULT_DB_URL,
        help=f"Postgres DSN (default: {_redact(DEFAULT_DB_URL)})",
    )
    args = parser.parse_args(argv)

    expected = _source_schema_migrations(REPO_ROOT)
    actual = query_applied(args.db_url)
    pending, mismatches, unexpected = compute_plan(
        expected, actual, KNOWN_SCHEMA_MIGRATION_EXCEPTIONS
    )

    db_version = max(actual) if actual else None
    src_max = max(expected) if expected else None
    print(
        f"DB {_redact(args.db_url)} at version {db_version}; "
        f"source manifest defines {len(expected)} migration(s) (max {src_max})."
    )

    if mismatches:
        print("\nregistry mismatch (db name != source name):", file=sys.stderr)
        for v in mismatches:
            print(f"  {v}: db={actual[v]!r} source={expected[v]!r}", file=sys.stderr)
    if unexpected:
        print(
            "\nDB has versions with no source file (checkout behind the deployed DB?):",
            file=sys.stderr,
        )
        for v in unexpected:
            print(f"  {v}: {actual[v]!r}", file=sys.stderr)

    if args.check:
        # Preflight gate: the DB is "ready for this code" only when there is
        # nothing left to do. Any pending migration, name mismatch, or DB
        # version with no source file blocks — a deploy must not restart code
        # that expects an unapplied (or divergent) schema.
        blockers: list[str] = []
        if pending:
            blockers.append(f"{len(pending)} pending migration(s): {pending}")
        if mismatches:
            blockers.append(f"{len(mismatches)} name mismatch(es): {mismatches}")
        if unexpected:
            blockers.append(
                f"{len(unexpected)} DB version(s) with no source file "
                f"(checkout behind the deployed DB): {unexpected}"
            )
        if blockers:
            print("\nMigration check FAILED — DB not in sync with the source manifest:",
                  file=sys.stderr)
            for b in blockers:
                print(f"  - {b}", file=sys.stderr)
            print("\nApply with: python3 scripts/dev/apply_migrations.py --apply",
                  file=sys.stderr)
            return 1
        print("\nMigration check OK — DB in sync with the source manifest.")
        return 0

    if not pending:
        if mismatches or unexpected:
            print("\nNo pending migrations, but drift is present (see above).", file=sys.stderr)
            return 1
        print("\nNo pending migrations — registry matches the source manifest.")
        return 0

    print(f"\n{len(pending)} pending migration(s):")
    plan: list[tuple[int, Path]] = []
    missing_files: list[int] = []
    for v in pending:
        path = file_for_version(v)
        if path is None:
            missing_files.append(v)
            print(f"  {v}: {expected[v]!r}  -> NO FILE FOUND", file=sys.stderr)
            continue
        backfill = "  (back-fill: below current DB max)" if (
            db_version is not None and v < db_version
        ) else ""
        print(f"  {v}: {expected[v]!r}  [{path.name}]{backfill}")
        plan.append((v, path))

    if mismatches or unexpected or missing_files:
        print(
            "\nRefusing to apply: reconcile the drift / missing-file issues above first.",
            file=sys.stderr,
        )
        return 1

    if not args.apply:
        print("\nDry run. Re-run with --apply to execute the plan above.")
        return 0

    print("\nApplying...")
    for v, path in plan:
        print(f"-> {path.name}")
        if not apply_file(args.db_url, path):
            print(f"Aborted at version {v}; later migrations not applied.", file=sys.stderr)
            return 1
        recorded = query_applied(args.db_url)
        if v not in recorded:
            print(
                f"  applied but version {v} is not recorded in schema_migrations — abort.",
                file=sys.stderr,
            )
            return 1
        print(f"  ok: version {v} recorded ({recorded[v]!r})")

    print(f"\nApplied {len(plan)} migration(s); DB now at version {max(query_applied(args.db_url))}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
