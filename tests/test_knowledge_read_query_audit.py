"""Audit payload for `knowledge_read` search events.

Read traffic has been counted since PR #532, but only as query_present /
query_term_count / result_count — never the terms. That makes retrievability
unauditable: the log can show that a search ran and how many rows came back, but
not whether the right entry was reachable for what was actually asked.

These tests pin the two things that make the log answer that question, and the
redaction that makes storing it safe.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.mcp_handlers.knowledge.handlers import (  # noqa: E402
    _AUDIT_QUERY_TEXT_MAX,
    _audit_safe_query_text,
    _audit_safe_tags,
)


class TestAuditSafeQueryText:
    def test_plain_query_survives_intact(self):
        assert _audit_safe_query_text("Lumen stopped checking in") == (
            "Lumen stopped checking in"
        )

    def test_strips_surrounding_whitespace(self):
        assert _audit_safe_query_text("  sentinel  ") == "sentinel"

    def test_absent_for_non_strings_and_blanks(self):
        # None (not present-and-null) so the payload key is simply absent.
        for value in (None, "", "   ", 42, ["sentinel"], {"q": "x"}):
            assert _audit_safe_query_text(value) is None

    def test_redacts_credential_assignments(self):
        got = _audit_safe_query_text("rotate api_key=sk-live-abc123 before deploy")
        assert "sk-live-abc123" not in got
        assert "[REDACTED]" in got
        assert "rotate" in got and "before deploy" in got  # context preserved

    def test_redacts_each_credential_keyword(self):
        for kw in ("api_key", "api-key", "apikey", "password", "passwd",
                   "secret", "token", "bearer"):
            got = _audit_safe_query_text(f"{kw}: hunter2taboo")
            assert "hunter2taboo" not in got, kw

    def test_redacts_url_embedded_password(self):
        got = _audit_safe_query_text("postgres://admin:s3cr3t@localhost/governance")
        assert "s3cr3t" not in got
        assert "[REDACTED]" in got

    def test_truncates_long_queries_with_marker(self):
        got = _audit_safe_query_text("term " * 400)
        assert len(got) == _AUDIT_QUERY_TEXT_MAX + 1  # +1 for the ellipsis
        assert got.endswith("…")

    def test_realistic_query_is_never_truncated(self):
        """The measured distribution is ~0.1 terms; a real query must survive
        whole or the log answers a different question than the one asked."""
        q = "does the check-in schema work for non-Claude agents like Lumen or BEAM"
        assert _audit_safe_query_text(q) == q


class TestAuditSafeTags:
    def test_string_tags_pass_through(self):
        assert _audit_safe_tags(["sentinel", "vigil"]) == ["sentinel", "vigil"]

    def test_absent_for_non_lists_and_empties(self):
        for value in (None, "sentinel", {}, [], [None], [{"a": 1}]):
            assert _audit_safe_tags(value) is None

    def test_bounds_count_and_length(self):
        assert len(_audit_safe_tags([f"t{i}" for i in range(50)])) == 10
        assert len(_audit_safe_tags(["x" * 500])[0]) == 64

    def test_separates_scoped_poll_from_open_recall(self):
        """The distinction the field exists for: Vigil polls a known lane with
        tags + semantic=False; an open recall carries no tag filter. Judging
        retrieval without splitting them measures the wrong population."""
        assert _audit_safe_tags(["sentinel"]) == ["sentinel"]   # Vigil's poll
        assert _audit_safe_tags(None) is None                    # open recall
