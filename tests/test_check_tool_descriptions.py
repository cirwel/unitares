"""Unit tests for scripts/dev/check_tool_descriptions.py (pure functions, no DB)."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "check_tool_descriptions",
    Path(__file__).parent.parent / "scripts" / "dev" / "check_tool_descriptions.py",
)
checker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(checker)


def test_conforming_routing_line_passes():
    desc = "Ask a model for advisory help; for on-record judgment use request_review.\n\nDetail."
    assert checker.check_description("consult", desc) == []


def test_long_first_line_flagged():
    desc = "Run " + "x" * checker.ROUTING_LINE_MAX
    assert any("chars" in p for p in checker.check_description("t", desc))


def test_non_verb_opener_flagged():
    problems = checker.check_description("t", "The thing that does stuff.")
    assert any("verb" in p for p in problems)


def test_dated_policy_id_flagged_anywhere_in_text():
    desc = "Mint an identity.\n\nPosture (S1-c, 2026-05-23): fresh by default."
    problems = checker.check_description("onboard", desc)
    assert any("dated policy id" in p for p in problems)


def test_only_first_line_counts_for_length():
    desc = "List hosts.\n" + "y" * 500
    assert checker.check_description("t", desc) == []
