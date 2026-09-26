"""Migration 071: a classified finding can still be archived and moved to cold.

064 admitted a closure class only on resolved, closed, wont_fix or superseded
rows. Once the class is stored, the KG lifecycle's resolved -> archived move
(and archived -> cold) would violate that for every classified row: the AGE
backend's update rolls back and returns False, which the lifecycle ignores, so
the row stays resolved and the error repeats every run; the Postgres backend
raises and the rest of that lifecycle run is lost.

The static half holds the migration's shape. The live half runs against
governance_test through the suite's own fixture (it skips when that database
is unavailable, like the other live-DB tests), after the fixture's bootstrap
has applied every migration on disk, 071 included.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "db/postgres/migrations/071_knowledge_closure_class_survives_tiering.sql"
MIGRATION_064 = ROOT / "db/postgres/migrations/064_knowledge_closure_class.sql"

ADMITTED = {"resolved", "closed", "wont_fix", "superseded", "archived", "cold"}


def _check_list(sql: str) -> set[str]:
    check = sql.split("ADD CONSTRAINT discoveries_closure_class_requires_closed", 1)[1]
    return set(re.findall(r"'(\w+)'", check.split(";", 1)[0]))


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------


def test_071_replaces_064s_constraint_under_the_same_name():
    sql = MIGRATION.read_text()
    assert (
        "DROP CONSTRAINT IF EXISTS discoveries_closure_class_requires_closed" in sql
    )
    assert _check_list(sql) == ADMITTED
    # 064's list is the narrower one this replaces; 064 itself is never edited
    # (its checksum is anchored in core.schema_migrations since 062).
    assert _check_list(MIGRATION_064.read_text()) == ADMITTED - {"archived", "cold"}


def test_071_still_refuses_a_class_on_a_reopened_row():
    assert not _check_list(MIGRATION.read_text()) & {"open", "disputed"}


def test_071_is_one_idempotent_transaction_that_checks_its_own_change():
    sql = MIGRATION.read_text()
    assert sql.index("BEGIN;") < sql.index("ALTER TABLE") < sql.index("COMMIT;")
    assert "VALUES (71, 'knowledge_closure_class_survives_tiering'" in sql
    assert "ON CONFLICT (version) DO NOTHING" in sql
    # The postcondition reads the definition: the name already existed after
    # 064, so its presence alone would prove nothing.
    assert "pg_get_constraintdef" in sql
    assert "RAISE EXCEPTION" in sql


# ---------------------------------------------------------------------------
# Live, against governance_test
# ---------------------------------------------------------------------------

EVIDENCE = {"deployed": "abc123 in the running build", "observed": "the new field on a live read"}


async def _require_071(backend) -> None:
    """governance_test is shared: a branch without 071 re-applies 064 on its
    own bootstrap. Name that instead of failing on a constraint violation."""
    async with backend.acquire() as conn:
        definition = await conn.fetchval(
            """
            SELECT pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conname = 'discoveries_closure_class_requires_closed'
              AND conrelid = 'knowledge.discoveries'::regclass
            """
        )
    assert definition and "'archived'" in definition and "'cold'" in definition, (
        "governance_test carries the pre-071 constraint "
        f"({definition!r}); another worktree's bootstrap re-applied 064 after "
        "this session's applied 071. Re-run to re-apply it."
    )


async def _seed_classified_resolved(graph, backend, discovery_id: str) -> None:
    from src.knowledge_graph import DiscoveryNode

    await backend.kg_add_discovery(
        DiscoveryNode(id=discovery_id, agent_id="agent-071", type="bug_found", summary="s")
    )
    long_ago = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    assert await graph.update_discovery(
        discovery_id,
        {
            "status": "resolved",
            "resolved_at": long_ago,
            "updated_at": long_ago,
            "closure_class": "fix_verified",
            "closure_evidence": EVIDENCE,
        },
    )


async def _row(backend, discovery_id: str) -> dict:
    async with backend.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, closure_class, closure_evidence FROM knowledge.discoveries WHERE id = $1",
            discovery_id,
        )
    evidence = row["closure_evidence"]
    return {
        "status": row["status"],
        "closure_class": row["closure_class"],
        "closure_evidence": json.loads(evidence) if isinstance(evidence, str) else evidence,
    }


def _graphs(backend, monkeypatch):
    """The Postgres backend, and the AGE backend on its SQL-only fallback.

    governance_test has no AGE graph, so the AGE backend is exercised on the
    path it takes when the graph is unavailable; graph_available is pinned to
    False so no AGE catalog is probed on the shared database.
    """
    from src.storage.knowledge_graph_age import KnowledgeGraphAGE
    from src.storage.knowledge_graph_postgres import KnowledgeGraphPostgres

    postgres = KnowledgeGraphPostgres()
    postgres._db = backend
    age = KnowledgeGraphAGE()
    age._db = backend

    async def _no_graph():
        return False

    monkeypatch.setattr(backend, "graph_available", _no_graph)
    return {"postgres": postgres, "age_sql_fallback": age}


async def _seed_classified_archived_long_ago(backend, discovery_id: str) -> None:
    """An archived, classified row last written 200 days ago.

    Inserted directly: knowledge.discoveries stamps updated_at on every UPDATE
    (trg_knowledge_discoveries_updated_at), so no update can age a row past
    the cold threshold, and an INSERT is not an UPDATE.
    """
    long_ago = datetime.now(timezone.utc) - timedelta(days=200)
    async with backend.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO knowledge.discoveries (
                id, agent_id, type, summary, status, created_at, updated_at,
                closure_class, closure_evidence
            ) VALUES ($1, 'agent-071', 'bug_found', 's', 'archived', $2, $2,
                      'fix_verified', $3)
            """,
            discovery_id,
            long_ago,
            json.dumps(EVIDENCE),
        )


async def _delete(backend, *discovery_ids: str) -> None:
    async with backend.acquire() as conn:
        await conn.execute(
            "DELETE FROM knowledge.discoveries WHERE id = ANY($1::text[])",
            list(discovery_ids),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["postgres", "age_sql_fallback"])
async def test_the_lifecycle_archives_a_classified_resolved_row(
    live_postgres_backend, monkeypatch, path
):
    from src.knowledge_graph_lifecycle import KnowledgeGraphLifecycle

    backend = live_postgres_backend
    await _require_071(backend)
    graph = _graphs(backend, monkeypatch)[path]
    discovery_id = f"071-archive-{path}"
    try:
        await _seed_classified_resolved(graph, backend, discovery_id)
        assert await _row(backend, discovery_id) == {
            "status": "resolved",
            "closure_class": "fix_verified",
            "closure_evidence": EVIDENCE,
        }

        lifecycle = KnowledgeGraphLifecycle(graph=graph)
        archived, _skipped = await lifecycle._archive_old_resolved(
            datetime.now(), dry_run=False
        )
        assert discovery_id in archived
        assert await _row(backend, discovery_id) == {
            "status": "archived",
            "closure_class": "fix_verified",
            "closure_evidence": EVIDENCE,
        }

        # The read path returns what the row holds. AGE's get_discovery reads
        # the graph node first; governance_test has no graph, so read the row
        # the way its no-node fallback does.
        if path == "postgres":
            node = await graph.get_discovery(discovery_id)
        else:
            node = graph._dict_to_discovery(await backend.kg_get_discovery(discovery_id))
        assert node.status == "archived"
        assert node.closure_class == "fix_verified"
        assert node.closure_evidence == EVIDENCE
    finally:
        await _delete(backend, discovery_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["postgres", "age_sql_fallback"])
async def test_the_lifecycle_moves_a_classified_archived_row_to_cold(
    live_postgres_backend, monkeypatch, path
):
    from src.knowledge_graph_lifecycle import KnowledgeGraphLifecycle

    backend = live_postgres_backend
    await _require_071(backend)
    graph = _graphs(backend, monkeypatch)[path]
    discovery_id = f"071-cold-{path}"
    try:
        await _seed_classified_archived_long_ago(backend, discovery_id)
        cold = await KnowledgeGraphLifecycle(graph=graph)._move_to_cold(
            datetime.now(), dry_run=False
        )
        assert discovery_id in cold
        assert await _row(backend, discovery_id) == {
            "status": "cold",
            "closure_class": "fix_verified",
            "closure_evidence": EVIDENCE,
        }
    finally:
        await _delete(backend, discovery_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["postgres", "age_sql_fallback"])
async def test_reopening_a_classified_row_clears_the_pair(
    live_postgres_backend, monkeypatch, path
):
    """Written the way dialectic's resolution writes it: status only."""
    backend = live_postgres_backend
    await _require_071(backend)
    graph = _graphs(backend, monkeypatch)[path]
    discovery_id = f"071-reopen-{path}"
    try:
        await _seed_classified_resolved(graph, backend, discovery_id)
        assert await graph.update_discovery(
            discovery_id, {"status": "open", "details": "verified again"}
        )
        assert await _row(backend, discovery_id) == {
            "status": "open",
            "closure_class": None,
            "closure_evidence": None,
        }
    finally:
        await _delete(backend, discovery_id)


@pytest.mark.asyncio
async def test_the_schema_still_refuses_a_class_on_an_open_row(live_postgres_backend):
    import asyncpg

    backend = live_postgres_backend
    await _require_071(backend)
    from src.knowledge_graph import DiscoveryNode

    discovery_id = "071-open"
    try:
        await backend.kg_add_discovery(
            DiscoveryNode(id=discovery_id, agent_id="agent-071", type="note", summary="s")
        )
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with backend.acquire() as conn:
                await conn.execute(
                    "UPDATE knowledge.discoveries SET closure_class = 'duplicate' WHERE id = $1",
                    discovery_id,
                )
    finally:
        await _delete(backend, discovery_id)
