"""Unit tests for hyphenated-compound FTS query normalization.

PostgreSQL's ``english`` FTS parser indexes hyphenated tokens like
``VERIFIER-SPEC-v1`` as an exact ``asciihword`` lexeme (``'verifier-spec-v1':1``)
followed by its sub-parts (``'verifi':2 'spec':3 'v1':4``). Raw queries for a
sub-compound (``VERIFIER-SPEC``) require the exact ``'verifier-spec'`` lexeme
and return zero rows against longer compounds. Rewriting eligible unquoted
compounds to quoted phrases (``"VERIFIER SPEC"``) matches adjacent component
lexemes while avoiding regressions on signed-int digit suffixes, stopwords,
underscores, negations, paths, and user-quoted phrases.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.db.mixins.knowledge_graph import (
    KnowledgeGraphMixin,
    _apply_operator,
    _normalize_hyphenated_token,
    _or_default_query,
)


class TestHyphenatedTokenNormalization:
    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("VERIFIER-SPEC", '"VERIFIER SPEC"'),
            ("SPEC-v1", '"SPEC v1"'),
            ("bge-m3", '"bge m3"'),
            ("http2-v3", '"http2 v3"'),
            ("agent-135486de", '"agent 135486de"'),
            ("github-outcome-observer", '"github outcome observer"'),
            ("end-to-end", '"end to end"'),
            ("read-or-write", '"read or write"'),
            ("READ-OR-WRITE", '"READ OR WRITE"'),
            ("in-app-browser", '"in app browser"'),
            ("self-check-in", '"self check in"'),
            ("x-ray", '"x ray"'),
        ],
    )
    def test_eligible_compounds_rewrite_to_phrase(self, token, expected):
        assert _normalize_hyphenated_token(token) == expected

    @pytest.mark.parametrize(
        "token",
        [
            # Pure-digit segments: Postgres parses `-123`, `-1`, `-11` as signed ints.
            "pr-123",
            "tier-1",
            "phase-2",
            "v1-2",
            "2026-11-25",
            "agent-1234-abcd",
            "foo-bar-123",
            # Scientific-notation float shapes: Postgres parses standalone `7e3a`
            # as float `7e3` + word `a`, and leading `1e-3a` as float `1e-3`.
            "commit-7e3a",
            "commit-7E3A",
            "agent-4e9a-beef",
            "7e3a-commit",
            "commit-12e4abcd",
            "commit-0e0a",
            "1e3-foo",
            "1e-3a-foo",
            "1E-3a-foo",
            # Stopword-collapsed compounds: fewer than 2 non-stopword parts.
            "in-memory",
            "read-only",
            "follow-up",
            "check-in",
            "opt-in",
            "built-in",
            "t-shirt",
            "out-of-band",
            "in-to-the",
            # Negations, CLI flags, double-dashes, boundary dashes.
            "-VERIFIER-SPEC",
            "--dry-run",
            "foo--bar",
            "-foo",
            "foo-",
            # Underscores shift asciihword lexeme positions in Postgres.
            "foo_bar-baz",
            # Paths, URLs, emails, and punctuation-wrapped tokens.
            "src/foo-bar/baz-qux.py",
            "https://example.com/foo-bar",
            "ops@foo-bar.example",
            "(VERIFIER-SPEC)",
            "VERIFIER-SPEC,",
            # User-quoted phrases must never be rewritten (preserves inner offsets).
            '"see VERIFIER-SPEC now"',
            '"VERIFIER-SPEC"',
            # Plain single words.
            "verifier",
        ],
    )
    def test_ineligible_tokens_are_left_unchanged(self, token):
        assert _normalize_hyphenated_token(token) == token


class TestApplyOperatorWithHyphens:
    def test_single_hyphenated_compound(self):
        assert _apply_operator("VERIFIER-SPEC") == '"VERIFIER SPEC"'
        assert _or_default_query("VERIFIER-SPEC") == '"VERIFIER SPEC"'

    def test_multi_term_and_and_or_preserve_atomic_phrase(self):
        assert _apply_operator("VERIFIER-SPEC architecture", operator="AND") == '"VERIFIER SPEC" AND architecture'
        assert _apply_operator("VERIFIER-SPEC architecture", operator="OR") == '"VERIFIER SPEC" OR architecture'

    def test_explicit_boolean_query_normalizes_compounds_without_rejoining(self):
        assert _apply_operator("VERIFIER-SPEC OR fallback", operator="AND") == '"VERIFIER SPEC" OR fallback'
        assert (
            _apply_operator("review_gate OR github-outcome-observer OR pr-123", operator="AND")
            == 'review_gate OR "github outcome observer" OR pr-123'
        )

    def test_hyphenated_token_containing_uppercase_or_does_not_suppress_operator(self):
        assert _apply_operator("READ-OR-WRITE lock", operator="AND") == '"READ OR WRITE" AND lock'
        assert _apply_operator("READ-OR-WRITE lock", operator="OR") == '"READ OR WRITE" OR lock'

    def test_user_quoted_phrase_with_hyphen_is_preserved_alongside_bare_compound(self):
        assert (
            _apply_operator('"see VERIFIER-SPEC now" bge-m3', operator="AND")
            == '"see VERIFIER-SPEC now" AND "bge m3"'
        )

    def test_negated_and_digit_tokens_stay_unchanged_in_compound_queries(self):
        assert _apply_operator("pr-123 -VERIFIER-SPEC in-memory", operator="AND") == "pr-123 AND -VERIFIER-SPEC AND in-memory"


class _AcquireContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeDb(KnowledgeGraphMixin):
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _AcquireContext(self.conn)


class TestKgFullTextSearchUsesNormalizedQuery:
    @pytest.mark.asyncio
    async def test_kg_full_text_search_passes_normalized_compound_to_sql(self):
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[])

        await _FakeDb(conn).kg_full_text_search("VERIFIER-SPEC architecture", limit=10, operator="AND")

        sql, *params = conn.fetch.await_args.args
        assert "websearch_to_tsquery('english', $1)" in sql
        assert params[0] == '"VERIFIER SPEC" AND architecture'
        assert params[1] == 10


class TestPostgresFtsHyphenSemantics:
    """Exercise real PostgreSQL ``to_tsvector`` / ``websearch_to_tsquery``
    against ``_apply_operator`` so parser divergences cannot hide behind
    string-only unit tests. Read-only expressions; requires no tables."""

    @pytest.mark.asyncio
    async def test_live_postgres_match_and_non_match_matrix(self):
        try:
            import asyncpg
        except ImportError:
            pytest.skip("asyncpg not installed")

        from tests.test_db_utils import TEST_DB_URL, can_connect_to_test_db

        if not can_connect_to_test_db():
            pytest.skip("governance_test database not available")

        cases = [
            # Sub-compound / prefix matches recovered by normalization.
            ("VERIFIER-SPEC-v1 architecture", "VERIFIER-SPEC", "AND", True),
            ("VERIFIER-SPEC-v1 architecture", "SPEC-v1", "AND", True),
            ("VERIFIER-SPEC architecture", "VERIFIER-SPEC", "AND", True),
            ("verifier spec architecture", "VERIFIER-SPEC", "AND", True),
            ("spec verifier architecture", "VERIFIER-SPEC", "AND", False),
            ("bge-m3-small embedding", "bge-m3", "AND", True),
            ("agent-135486de-a5a1 logged in", "agent-135486de", "AND", True),
            ("github-outcome-observer-v2 check", "github-outcome-observer", "AND", True),
            ("VERIFIER-SPEC-v1 architecture", "VERIFIER-SPEC OR missing", "AND", True),
            ("end-to-end test suite", "end-to-end", "AND", True),
            ("end of the road", "end-to-end", "AND", False),
            ("READ-OR-WRITE lock", "READ-OR-WRITE lock", "AND", True),
            ("read the file lock", "READ-OR-WRITE lock", "AND", False),
            # Scientific-notation hex/UUID guardrails: must still match exact docs.
            ("commit-7e3a landed", "commit-7e3a", "AND", True),
            ("commit-7E3A landed", "commit-7E3A", "AND", True),
            ("agent-4e9a-beef logged", "agent-4e9a-beef", "AND", True),
            ("7e3a-commit landed", "7e3a-commit", "AND", True),
            ("commit-12e4abcd landed", "commit-12e4abcd", "AND", True),
            ("1e-3a-foo bar", "1e-3a-foo", "AND", True),
            # Pure-digit and date guardrails.
            ("merged pr-123 today", "pr-123", "AND", True),
            ("tier-1 probe passed", "tier-1", "AND", True),
            ("release v1-2 shipped", "v1-2", "AND", True),
            ("audit on 2026-11-25 completed", "2026-11-25", "AND", True),
            ("agent-1234-abcd logged", "agent-1234-abcd", "AND", True),
            ("foo-bar-123-v2 doc", "foo-bar-123", "AND", True),
            # Stopword-collapsed compounds stay exact without false-matching bare words.
            ("in-memory cache hit", "in-memory", "AND", True),
            ("memory leak detected", "in-memory", "AND", False),
            ("read-only mode active", "read-only", "AND", True),
            ("read the config file", "read-only", "AND", False),
            ("check-in recorded", "check-in", "AND", True),
            ("check the status now", "check-in", "AND", False),
            # Underscores, user-quoted phrases, and negations stay intact.
            ("foo_bar-baz doc", "foo_bar-baz", "AND", True),
            ("see VERIFIER-SPEC now", '"see VERIFIER-SPEC now"', "AND", True),
            ("VERIFIER-SPEC architecture", "architecture -VERIFIER-SPEC", "AND", False),
        ]

        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            for doc, query, operator, expected in cases:
                ts_query = _apply_operator(query, operator=operator)
                matched = await conn.fetchval(
                    "SELECT to_tsvector('english', $1) @@ websearch_to_tsquery('english', $2)",
                    doc,
                    ts_query,
                )
                assert matched is expected, (
                    f"doc={doc!r} query={query!r} normalized={ts_query!r}: "
                    f"expected {expected}, got {matched}"
                )
        finally:
            await conn.close()
