"""Pin the knowledge enum vocabularies against the SQL CHECK constraints.

History: the handler validation sets for response_type/status/severity grew
over time (9/7/4 values) while the ResponseTo dataclass Literal and the CHECK
constraints in db/postgres/knowledge_schema.sql stayed at the originals
(4/3/3). On any database built from the base DDL, handler-valid writes like
status='superseded' (the supersede action) or severity='critical' violated
the CHECK. Migration 047 widened the constraints and the vocabularies were
single-sourced in src.knowledge_graph; these tests keep the three layers —
Python constants, base DDL, migration — from drifting apart again.
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
MIGRATION_SQL = (
    REPO_ROOT / "db" / "postgres" / "migrations"
    / "047_knowledge_check_constraints_widen.sql"
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
    text = MIGRATION_SQL.read_text()

    response_sets = _check_sets(text, "response_type")
    assert len(response_sets) == 2, "047 must widen both response_type CHECKs"
    for s in response_sets:
        assert s == VALID_RESPONSE_TYPES

    status_sets = _check_sets(text, "status")
    assert status_sets == [VALID_DISCOVERY_STATUSES]

    severity_sets = _check_sets(text, "severity")
    assert severity_sets == [VALID_SEVERITIES]
