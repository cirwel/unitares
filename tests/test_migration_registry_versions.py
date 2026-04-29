"""Static checks for PostgreSQL migration registry metadata."""

from __future__ import annotations

import re
from pathlib import Path


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "postgres" / "migrations"


def _registered_versions(path: Path) -> set[int]:
    text = path.read_text()
    insert_blocks = re.findall(
        r"INSERT\s+INTO\s+core\.schema_migrations\s*\([^)]*version[^)]*\)"
        r"\s*VALUES\s*(.*?)(?:ON\s+CONFLICT|;)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    return {
        int(version)
        for block in insert_blocks
        for version in re.findall(r"\(\s*(\d+)\s*,", block)
    }


def test_migration_registry_versions_match_filenames():
    """A migration file must not silently claim another file's registry slot."""
    mismatches = []
    claimed_by: dict[int, str] = {}

    for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        file_version = int(path.name.split("_", 1)[0])
        registered_versions = _registered_versions(path)
        if not registered_versions:
            continue

        if file_version not in registered_versions:
            mismatches.append(
                f"{path.name} registers {sorted(registered_versions)}, missing {file_version}"
            )
        extra_versions = registered_versions - {file_version}
        if extra_versions:
            mismatches.append(
                f"{path.name} also registers {sorted(extra_versions)}, expected only {file_version}"
            )

        for version in registered_versions:
            previous = claimed_by.get(version)
            if previous is not None:
                mismatches.append(
                    f"version {version} claimed by both {previous} and {path.name}"
                )
            claimed_by[version] = path.name

    assert not mismatches, "Migration registry drift: " + "; ".join(mismatches)
