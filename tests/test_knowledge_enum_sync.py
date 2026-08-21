"""Pin the knowledge enum vocabularies against the SQL CHECK constraints.

History: the handler validation sets for response_type/status/severity grew
over time (9/8/4 values) while the ResponseTo dataclass Literal and the CHECK
constraints in db/postgres/knowledge_schema.sql stayed at the originals
(4/3/3). On any database built from the base DDL, handler-valid writes like
status='superseded' (the supersede action) or severity='critical' violated
the CHECK. Migration 047 widened the original sets; migration 060 restored two
constraints lost during its partial live apply and added the lifecycle's
status='cold'. The vocabularies are single-sourced in src.knowledge_graph;
these tests keep Python, base DDL, and repair migrations from drifting again.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from src.knowledge_graph import (
    ResponseType,
    VALID_DISCOVERY_STATUSES,
    VALID_RESPONSE_TYPES,
    VALID_SEVERITIES,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = REPO_ROOT / "db" / "postgres" / "knowledge_schema.sql"
MIGRATION_047_SQL = (
    REPO_ROOT / "db" / "postgres" / "migrations"
    / "047_knowledge_check_constraints_widen.sql"
)
MIGRATION_060_SQL = (
    REPO_ROOT / "db" / "postgres" / "migrations"
    / "060_knowledge_constraint_integrity.sql"
)


def _check_sets(sql_text: str, column: str) -> list[frozenset[str]]:
    """Extract every CHECK (column IN (...)) value set from SQL text."""
    sets = []
    for m in re.finditer(
        rf"CHECK\s*\(\s*{column}\s+IN\s*\(([^)]*)\)", sql_text
    ):
        values = re.findall(r"'([^']*)'", m.group(1))
        sets.append(frozenset(values))
    return sets


# --- Python-internal consistency ---------------------------------------------


def test_response_type_literal_matches_valid_set():
    """The ResponseTo Literal and the runtime validation set are one vocabulary."""
    assert frozenset(get_args(ResponseType)) == VALID_RESPONSE_TYPES


def test_response_type_aliases_normalize_onto_canonical_vocabulary():
    """Conjugation variants fold onto canonical values; storage never widens.

    The canonical set mixes bare verbs ("extend") with one conjugated form
    ("supersedes"), so writers reliably type the other conjugation
    ("extends" — dogfood 2026-08-20). Every alias must land INSIDE the
    canonical set so the SQL CHECK constraints stay authoritative.
    """
    from src.knowledge_graph import _RESPONSE_TYPE_ALIASES, normalize_response_type

    for alias, canonical in _RESPONSE_TYPE_ALIASES.items():
        assert canonical in VALID_RESPONSE_TYPES, (alias, canonical)
        assert alias not in VALID_RESPONSE_TYPES, alias
        assert normalize_response_type(alias) == canonical

    # Canonical values pass through untouched; unknowns are preserved so the
    # membership check still rejects them with the canonical error.
    for canonical in VALID_RESPONSE_TYPES:
        assert normalize_response_type(canonical) == canonical
    assert normalize_response_type("EXTENDS") == "extend"
    assert normalize_response_type(" extend ") == "extend"
    assert normalize_response_type("bogus") == "bogus"


def test_handlers_use_shared_severities():
    """handlers.VALID_SEVERITIES is the shared constant, not a local copy."""
    from src.mcp_handlers.knowledge import handlers

    assert handlers.VALID_SEVERITIES is VALID_SEVERITIES


# --- SQL: base DDL ------------------------------------------------------------


def test_schema_response_type_checks_match():
    """Both response_type CHECKs (discoveries + discovery_edges) carry the full set."""
    sets = _check_sets(SCHEMA_SQL.read_text(), "response_type")
    assert len(sets) == 2, "expected response_type CHECKs on discoveries and discovery_edges"
    for s in sets:
        assert s == VALID_RESPONSE_TYPES


def test_schema_status_check_matches():
    sets = _check_sets(SCHEMA_SQL.read_text(), "status")
    assert len(sets) == 1, "expected exactly one status CHECK in knowledge_schema.sql"
    assert sets[0] == VALID_DISCOVERY_STATUSES


def test_schema_severity_check_matches():
    sets = _check_sets(SCHEMA_SQL.read_text(), "severity")
    assert len(sets) == 1, "expected exactly one severity CHECK in knowledge_schema.sql"
    assert sets[0] == VALID_SEVERITIES


# --- SQL: migration 047 -------------------------------------------------------


def test_migration_047_matches_vocabularies():
    """Keep the historical 047 contract explicit without rewriting history."""
    text = MIGRATION_047_SQL.read_text()

    response_sets = _check_sets(text, "response_type")
    assert len(response_sets) == 2, "047 must widen both response_type CHECKs"
    for s in response_sets:
        assert s == VALID_RESPONSE_TYPES

    status_sets = _check_sets(text, "status")
    assert status_sets == [VALID_DISCOVERY_STATUSES - {"cold"}]

    severity_sets = _check_sets(text, "severity")
    assert severity_sets == [VALID_SEVERITIES]


# --- SQL: migration 060 -------------------------------------------------------


def test_migration_060_matches_vocabularies():
    text = MIGRATION_060_SQL.read_text()

    assert _check_sets(text, "status") == [VALID_DISCOVERY_STATUSES]
    assert _check_sets(text, "severity") == [VALID_SEVERITIES]


def test_migration_060_is_atomic_and_registers_after_postconditions():
    """A failed constraint re-add must roll back instead of registering applied."""
    text = MIGRATION_060_SQL.read_text()
    statements = text.strip()

    assert statements.startswith("-- 060_knowledge_constraint_integrity.sql")
    assert re.search(r"(?m)^BEGIN;$", text)
    assert statements.endswith("COMMIT;")
    assert "RAISE EXCEPTION" in text
    assert text.index("RAISE EXCEPTION") < text.index(
        "INSERT INTO core.schema_migrations"
    )
