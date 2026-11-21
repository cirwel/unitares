"""Tests for scripts/dev/branch_hygiene/cherry.py."""
from scripts.dev.branch_hygiene.cherry import CherryVerdict, parse_cherry


class TestParseCherry:
    def test_empty_output_is_skip(self):
        result = parse_cherry("")
        assert result.verdict == CherryVerdict.SKIP
        assert "empty" in result.reason

    def test_whitespace_only_is_skip(self):
        result = parse_cherry("   \n\n  \n")
        assert result.verdict == CherryVerdict.SKIP

    def test_all_minus_is_delete(self):
        out = "- abc123\n- def456\n- 789ghi\n"
        result = parse_cherry(out)
        assert result.verdict == CherryVerdict.DELETE
        assert result.minus_count == 3
        assert result.plus_count == 0
        assert "3 commit(s) squash-merged" in result.reason

    def test_any_plus_is_hold(self):
        out = "- abc123\n+ def456\n- 789ghi\n"
        result = parse_cherry(out)
        assert result.verdict == CherryVerdict.HOLD
        assert result.plus_count == 1
        assert result.minus_count == 2
        assert "1 unique" in result.reason

    def test_all_plus_is_hold(self):
        out = "+ abc123\n+ def456\n"
        result = parse_cherry(out)
        assert result.verdict == CherryVerdict.HOLD
        assert result.plus_count == 2
        assert result.minus_count == 0

    def test_unparseable_line_is_skip(self):
        out = "- abc123\n??? weird line\n"
        result = parse_cherry(out)
        assert result.verdict == CherryVerdict.SKIP
        assert "unparseable" in result.reason

    def test_single_minus_is_delete(self):
        result = parse_cherry("- abc\n")
        assert result.verdict == CherryVerdict.DELETE
        assert result.minus_count == 1
