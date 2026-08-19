"""Tests for parse_limit (src/mcp_handlers/support/coerce.py).

parse_limit exists because a silently substituted row limit is not a smaller
answer to the caller's question -- it is an answer to a different question. A
non-positive limit used to reach PostgreSQL as a malformed LIMIT, and the
dialectic session backend rendered the resulting error as an empty result set,
so `dialectic(action='list', limit=-1)` reported "No dialectic sessions found"
with success=true while 50 sessions existed.
"""

import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.support.coerce import LimitError, parse_limit


class TestParseLimitAccepts:
    def test_none_returns_default(self):
        assert parse_limit(None, default=50, maximum=200) == 50

    def test_in_range_value_passes_through(self):
        assert parse_limit(10, default=50, maximum=200) == 10

    def test_minimum_is_accepted(self):
        assert parse_limit(1, default=50, maximum=200) == 1

    def test_maximum_is_accepted(self):
        assert parse_limit(200, default=50, maximum=200) == 200

    def test_numeric_string_is_accepted(self):
        """MCP transport routinely delivers ints as strings."""
        assert parse_limit("25", default=50, maximum=200) == 25

    def test_over_maximum_clamps_down(self):
        """Over-asking has always been allowed; that behaviour is preserved."""
        assert parse_limit(999, default=50, maximum=200) == 200

    def test_custom_minimum(self):
        assert parse_limit(5, default=50, maximum=200, minimum=5) == 5


class TestParseLimitRejects:
    def test_zero_raises(self):
        """Previously `int(0) or 50` silently became the default of 50."""
        with pytest.raises(LimitError):
            parse_limit(0, default=50, maximum=200)

    def test_negative_raises(self):
        """Previously reached SQL as `LIMIT -1` and surfaced as an empty list."""
        with pytest.raises(LimitError):
            parse_limit(-1, default=50, maximum=200)

    def test_non_numeric_string_raises(self):
        with pytest.raises(LimitError):
            parse_limit("all", default=50, maximum=200)

    def test_below_custom_minimum_raises(self):
        with pytest.raises(LimitError):
            parse_limit(4, default=50, maximum=200, minimum=5)

    def test_limit_error_is_a_value_error(self):
        """Callers that only catch ValueError still degrade safely."""
        assert issubclass(LimitError, ValueError)

    def test_message_names_the_parameter_and_the_range(self):
        with pytest.raises(LimitError) as exc:
            parse_limit(-1, default=50, maximum=200, name="limit")
        message = str(exc.value)
        assert "limit" in message
        assert "1" in message

    def test_message_starts_with_invalid_for_error_code_inference(self):
        """error_handling._infer_error_code_and_category keys on 'invalid'
        to emit INVALID_PARAM / validation_error."""
        with pytest.raises(LimitError) as exc:
            parse_limit(0, default=50, maximum=200)
        assert str(exc.value).lower().startswith("invalid")
