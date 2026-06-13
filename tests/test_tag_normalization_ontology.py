"""Tests for the tag-ontology layers: write-path normalize_tags formatting +
spelling-variant map, and the lifecycle-only curated semantic synonym map.

Mirrors the governance-plugin client normalizer so server-minted and
plugin-minted tags converge. See src/knowledge_ontology.py.
"""

from src.knowledge_graph import normalize_tags
from src.knowledge_ontology import (
    SEMANTIC_SYNONYMS,
    SPELLING_VARIANTS,
    apply_semantic_synonyms,
)


class TestNormalizeTagsFormatting:
    def test_lowercase_and_separator_folding(self):
        assert normalize_tags(["EISV", "eisv-dynamics", "eisv_framework"]) == [
            "eisv",
            "eisv-dynamics",
            "eisv-framework",
        ]

    def test_dedup_across_separator_variants(self):
        assert normalize_tags(["bug", "Bug Fix", "bug-fix", "bug_fix"]) == [
            "bug",
            "bug-fix",
        ]

    def test_punctuation_folds_to_hyphen(self):
        # Dots, slashes, plus signs, etc. all fold to a single hyphen — the
        # behavior that catches "node.js" vs "node-js" fragmentation.
        assert normalize_tags(["node.js"]) == ["node-js"]
        assert normalize_tags(["client/server"]) == ["client-server"]
        assert normalize_tags(["a..b__c  d"]) == ["a-b-c-d"]

    def test_trailing_punctuation_dropped(self):
        # "C++" -> "c" (trailing punctuation stripped after folding).
        assert normalize_tags(["C++"]) == ["c"]

    def test_pure_punctuation_tag_dropped(self):
        assert normalize_tags(["++", "  ", "--"]) == []

    def test_spelling_variant_map_applied(self):
        assert normalize_tags(["PostgreSQL"]) == ["postgres"]

    def test_spelling_variants_collapse_to_single_canonical(self):
        assert normalize_tags(["Postgres", "postgres", "PostgreSQL"]) == ["postgres"]

    def test_idempotent(self):
        once = normalize_tags(["Postgres", "node.js", "Bug Fix"])
        assert normalize_tags(once) == once

    def test_string_input_json_and_csv(self):
        assert normalize_tags('["ux", "identity"]') == ["ux", "identity"]
        assert normalize_tags("ux, identity") == ["ux", "identity"]

    def test_empty_input(self):
        assert normalize_tags([]) == []
        assert normalize_tags(None) == []

    def test_no_semantic_merge_at_write_time(self):
        # The formatting layer must NOT merge semantic synonyms — that is
        # reserved for the lifecycle pass.
        assert normalize_tags(["db", "auth"]) == ["db", "auth"]


class TestSemanticSynonyms:
    def test_documented_synonyms_map(self):
        assert apply_semantic_synonyms(["db"]) == ["database"]
        assert apply_semantic_synonyms(["auth"]) == ["identity"]

    def test_collapse_synonym_and_canonical(self):
        assert apply_semantic_synonyms(["db", "database"]) == ["database"]

    def test_unknown_tags_pass_through(self):
        assert apply_semantic_synonyms(["eisv", "coherence"]) == ["eisv", "coherence"]

    def test_order_preserving(self):
        assert apply_semantic_synonyms(["coherence", "db", "eisv"]) == [
            "coherence",
            "database",
            "eisv",
        ]

    def test_idempotent(self):
        # Canonical forms are never themselves keys, so a second pass is a
        # no-op.
        for canonical in SEMANTIC_SYNONYMS.values():
            assert canonical not in SEMANTIC_SYNONYMS

    def test_empty(self):
        assert apply_semantic_synonyms([]) == []


class TestLayerSeparation:
    def test_spelling_variants_are_formatting_not_semantic(self):
        # Spelling variants live in the write-path map; semantic synonyms do
        # not leak into it.
        assert "postgresql" in SPELLING_VARIANTS
        assert "db" not in SPELLING_VARIANTS
        assert "auth" not in SPELLING_VARIANTS

    def test_combined_pipeline(self):
        # The lifecycle pass runs normalize_tags THEN apply_semantic_synonyms.
        raw = ["PostgreSQL", "DB", "Auth_Service"]
        canonical = apply_semantic_synonyms(normalize_tags(raw))
        assert canonical == ["postgres", "database", "auth-service"]
