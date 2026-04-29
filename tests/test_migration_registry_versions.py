"""Static checks for PostgreSQL migration registry metadata."""

from __future__ import annotations

import re
from pathlib import Path


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "postgres" / "migrations"


def test_migration_registry_versions_match_filenames():
    """Files that register a migration must claim their filename version."""
    mismatches = []
    missing_registry = []

    for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        file_version = int(path.name.split("_", 1)[0])
        text = path.read_text()
        if "INSERT INTO core.schema_migrations" not in text:
            continue

        insert_blocks = re.findall(
            r"INSERT\s+INTO\s+core\.schema_migrations\s*\([^)]*version[^)]*\)"
            r"\s*VALUES\s*(.*?)(?:ON\s+CONFLICT|;)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        registered_versions = {
            int(v)
            for block in insert_blocks
            for v in re.findall(r"\(\s*(\d+)\s*,", block)
        }
        if not registered_versions:
            missing_registry.append(path.name)
            continue

        if file_version not in registered_versions:
            mismatches.append(
                f"{path.name} registers {sorted(registered_versions)}, missing {file_version}"
            )
        if path.name != "022_reconcile_schema_migration_drift.sql":
            extra_versions = registered_versions - {file_version}
            if extra_versions:
                mismatches.append(
                    f"{path.name} also registers {sorted(extra_versions)}, expected only {file_version}"
                )

    assert not missing_registry, "Could not parse migration registry inserts: " + ", ".join(missing_registry)
    assert not mismatches, "Migration version mismatch: " + "; ".join(mismatches)
