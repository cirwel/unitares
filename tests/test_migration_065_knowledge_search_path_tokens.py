"""
Schema test for migration 065 — path-token decomposition in KG search.

Issue #1711 reported that "hyphenated tokens never match the lexical arm".
Hyphens are not the cause: PostgreSQL already decomposes a standalone
`unitares-paper-v7` into the compound lexeme plus its parts, so bare
hyphenated identifiers and `slug-*` tags match without this migration. These
tests pin that fact so a future change cannot quietly break it while
"fixing hyphens".

The actual cause is the slash. `cirwel/unitares-paper-v7` is parsed as a single
`file` token and indexed as ONE lexeme, unreachable by its own segments.
Migration 065 indexes slash-joined spans a second time in decomposed form.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    import asyncpg
except ImportError:
    pytest.skip("asyncpg not installed", allow_module_level=True)

from tests.test_db_utils import (
    TEST_DB_URL,
    can_connect_to_test_db,
    ensure_test_database_schema,
)

if not can_connect_to_test_db():
    pytest.skip("governance_test database not available", allow_module_level=True)


async def _insert(conn, *, summary: str, details: str, tags: list[str]) -> str:
    discovery_id = f"test-065-{uuid.uuid4()}"
    await conn.execute(
        """
        INSERT INTO knowledge.discoveries (id, agent_id, type, summary, details, tags, status)
        VALUES ($1, $2, 'note', $3, $4, $5, 'open')
        """,
        discovery_id, f"agent-{uuid.uuid4()}", summary, details, tags,
    )
    return discovery_id


async def _matches(conn, discovery_id: str, query: str) -> bool:
    return await conn.fetchval(
        """
        SELECT search_vector @@ websearch_to_tsquery('english', $2)
        FROM knowledge.discoveries WHERE id = $1
        """,
        discovery_id, query,
    )


@pytest.mark.asyncio
async def test_slash_joined_identifier_reachable_by_its_own_segment():
    """The #1711 repro: an identifier appearing only inside a path.

    Before 065 the whole span indexed as one `file` lexeme, so a query for the
    trailing segment could not match the row that literally contained it.
    """
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        discovery_id = await _insert(
            conn,
            summary="v7 paper skeleton LIVE in private repo cirwel/unitares-paper-v7",
            details="see docs/ontology/identity.md for the contract",
            tags=["slug-paper-v7-skeleton"],
        )
        assert await _matches(conn, discovery_id, "unitares-paper-v7")
        assert await _matches(conn, discovery_id, "cirwel")
        # A path segment in details, and a nested path's inner segment.
        assert await _matches(conn, discovery_id, "identity.md")
        assert await _matches(conn, discovery_id, "ontology")
        # The full undecomposed span must still match — 065 adds, never replaces.
        assert await _matches(conn, discovery_id, "cirwel/unitares-paper-v7")
    finally:
        await conn.execute("DELETE FROM knowledge.discoveries WHERE id LIKE 'test-065-%'")
        await conn.close()


@pytest.mark.asyncio
async def test_multi_term_and_query_containing_a_path_segment():
    """Acceptance (b): adding a precise term must not destroy the query.

    `websearch_to_tsquery` ANDs terms, so one unreachable term zeroed the whole
    result set — precision made retrieval strictly worse.
    """
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        discovery_id = await _insert(
            conn,
            summary="v7 paper skeleton LIVE in private repo cirwel/unitares-paper-v7",
            details="outline plus claims ledger",
            tags=["slug-paper-v7-skeleton"],
        )
        assert await _matches(
            conn, discovery_id, "v7 AND paper AND skeleton AND unitares-paper-v7"
        )
    finally:
        await conn.execute("DELETE FROM knowledge.discoveries WHERE id LIKE 'test-065-%'")
        await conn.close()


@pytest.mark.asyncio
async def test_bare_hyphenated_identifier_and_slug_tag_match_in_every_field():
    """Acceptance (a), and the correction to the issue's stated diagnosis.

    A hyphenated identifier NOT inside a path is reachable in summary, details
    and tags alike. This held before 065 as well; it is pinned so the behaviour
    cannot regress under a future "hyphen fix".
    """
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        in_summary = await _insert(
            conn, summary="the anima-mcp server restarts", details="x", tags=["t"],
        )
        in_details = await _insert(
            conn, summary="x", details="the anima-mcp server restarts", tags=["t"],
        )
        in_tags = await _insert(
            conn, summary="x", details="y", tags=["slug-paper-v7-skeleton"],
        )
        assert await _matches(conn, in_summary, "anima-mcp")
        assert await _matches(conn, in_details, "anima-mcp")
        assert await _matches(conn, in_tags, "slug-paper-v7-skeleton")
    finally:
        await conn.execute("DELETE FROM knowledge.discoveries WHERE id LIKE 'test-065-%'")
        await conn.close()


@pytest.mark.asyncio
async def test_split_path_tokens_is_a_no_op_without_slashes():
    """Text with no slash-joined span must index exactly as it did before."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        plain = "a plain sentence with no paths at all"
        assert await conn.fetchval(
            "SELECT knowledge.split_path_tokens($1) = ''", plain
        )
        # A lone slash between spaces is not a path and must not be harvested.
        assert await conn.fetchval(
            "SELECT knowledge.split_path_tokens($1) = ''", "either / or"
        )
    finally:
        await conn.close()
