"""Parsing/shaping contract for knowledge(action='search').

Both behaviours here were found by a dogfood probe on 2026-08-14 and are
regressions of intent rather than crashes, which is why they survived: the
call returned success=true both times.
"""

from __future__ import annotations

import pytest

from src.mcp_handlers.knowledge import handlers


class TestBlankQueryRejected:
    """`"   "` is truthy, so it reached the semantic path, embedded whitespace,
    and returned nearest neighbours at ~0.34 similarity — arbitrary rows
    reported as a successful search."""

    @pytest.mark.parametrize("blank", ["   ", "\t", "\n", " \t\n "])
    def test_supplied_but_blank_query_is_a_parameter_error(self, blank: str):
        with pytest.raises(handlers._SearchParameterError) as exc:
            handlers._parse_knowledge_search_request({"query": blank})
        # The message has to say what to do instead, or the caller just retries.
        assert "omit" in str(exc.value).lower()

    def test_absent_query_is_still_allowed(self):
        """Filtering by tags alone is a real, supported shape — the fix must
        not take it away."""
        request = handlers._parse_knowledge_search_request({"tags": ["eisv"]})
        assert request.query_text is None

    def test_real_query_with_incidental_whitespace_survives(self):
        request = handlers._parse_knowledge_search_request({"query": "  dialectic  "})
        assert request.query_text == "  dialectic  "


class TestIncludeDetailsIsTriState:
    """The auto-include heuristic is a convenience for callers with no
    opinion, not an override of one."""

    def test_absent_means_no_preference(self):
        request = handlers._parse_knowledge_search_request({"query": "x"})
        assert request.include_details is None

    def test_explicit_false_is_preserved_not_collapsed(self):
        request = handlers._parse_knowledge_search_request(
            {"query": "x", "include_details": False}
        )
        assert request.include_details is False

    def test_explicit_true_is_preserved(self):
        request = handlers._parse_knowledge_search_request(
            {"query": "x", "include_details": True}
        )
        assert request.include_details is True

    @pytest.mark.parametrize(
        "raw,expected",
        [("false", False), ("FALSE", False), ("0", False), ("no", False),
         ("true", True), ("1", True), ("yes", True)],
    )
    def test_string_flags_coerce_the_way_mcp_sends_them(self, raw: str, expected: bool):
        """MCP admits these as strings. A bare bool("false") is True, which
        would turn an explicit opt-out into an opt-in."""
        request = handlers._parse_knowledge_search_request(
            {"query": "x", "include_details": raw}
        )
        assert request.include_details is expected


class TestAutoDetailsRespectsAnExplicitNo:
    """The measured cost of getting this wrong: 3 results, 44,205 bytes,
    into the caller's context after they asked for none of it."""

    @staticmethod
    def _resolve(requested, result_count: int) -> tuple[bool, bool]:
        """Calls the real decision, not a re-implementation of it. A test that
        mirrors the arithmetic passes against a broken caller."""
        return handlers._resolve_detail_inclusion(requested, result_count)

    def test_explicit_false_suppresses_auto_include(self):
        auto, include = self._resolve(False, 1)
        assert (auto, include) == (False, False)

    def test_absent_still_auto_includes_for_a_small_set(self):
        auto, include = self._resolve(None, 3)
        assert (auto, include) == (True, True)

    def test_absent_does_not_auto_include_for_a_large_set(self):
        auto, include = self._resolve(None, 4)
        assert (auto, include) == (False, False)

    def test_explicit_true_always_includes(self):
        assert self._resolve(True, 50) == (False, True)


class TestLegacySchemaPathKeepsAutoInclude:
    """`SearchKnowledgeGraphParams` backs the deprecated `search_knowledge_graph`
    tool. Its default was `False`, which after the tri-state fix would have read
    as an explicit "no" and silently removed auto-include from that path — a
    regression introduced by the fix rather than by the bug."""

    def test_absent_stays_unspecified_through_the_schema(self):
        from src.mcp_handlers.schemas.knowledge import SearchKnowledgeGraphParams

        params = SearchKnowledgeGraphParams(query="x")
        assert params.include_details is None

    def test_explicit_false_survives_the_schema(self):
        from src.mcp_handlers.schemas.knowledge import SearchKnowledgeGraphParams

        assert SearchKnowledgeGraphParams(query="x", include_details=False).include_details is False
        assert SearchKnowledgeGraphParams(query="x", include_details="false").include_details is False
