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
