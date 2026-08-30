"""Pins for the canonical tool-count scripts (scripts/diagnostics/).

The counter reads the runtime decorator registry — the same source dispatch
uses. In dependency-less environments (the doc-validation CI runner) it must
degrade without crashing, and it must degrade to an explicit *unavailable*
state rather than to a zero: a sentinel that a caller can render as an
inventory is the failure these pins exist to prevent.
"""

import pytest

from scripts.diagnostics import count_tools as diag_count_tools
from scripts.diagnostics import update_docs_tool_count


def _raise_missing_dependency(**_kwargs):
    raise ModuleNotFoundError("No module named 'mcp'")


def test_registry_counter_reports_tools():
    total = diag_count_tools.get_total_count()
    breakdown = diag_count_tools.get_tool_breakdown()

    assert total >= 40
    assert sum(breakdown.values()) == total
    assert any(module.startswith("knowledge") for module in breakdown)


def test_resolve_reports_available_count():
    result = diag_count_tools.resolve_tool_count()

    assert result.available is True
    assert result.total == sum(result.breakdown.values())
    assert result.reason is None


def test_resolve_reports_unavailable_not_zero(monkeypatch):
    """Unavailable must be a distinct state, never the number 0."""
    monkeypatch.setattr(diag_count_tools, "get_tool_breakdown", _raise_missing_dependency)

    result = diag_count_tools.resolve_tool_count()

    assert result.available is False
    assert result.total is None
    assert result.breakdown == {}
    assert "No module named 'mcp'" in result.reason


def test_counter_cli_prints_non_numeric_sentinel(monkeypatch, capsys):
    """Plain stdout must not be interpolatable as a count."""
    monkeypatch.setattr(diag_count_tools, "get_tool_breakdown", _raise_missing_dependency)
    monkeypatch.setattr("sys.argv", ["count_tools.py"])

    assert diag_count_tools.main() == 0

    captured = capsys.readouterr()
    assert "Tool count unavailable" in captured.err
    assert captured.out.strip() == diag_count_tools.UNAVAILABLE_SENTINEL
    assert "0" not in captured.out


def test_counter_cli_by_module_says_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(diag_count_tools, "get_tool_breakdown", _raise_missing_dependency)
    monkeypatch.setattr("sys.argv", ["count_tools.py", "--by-module"])

    assert diag_count_tools.main() == 0

    captured = capsys.readouterr()
    assert "Tool count unavailable" in captured.err
    assert "Tool count unavailable" in captured.out
    assert " 0 tools" not in captured.out


def test_counter_cli_json_flags_availability(monkeypatch, capsys):
    monkeypatch.setattr(diag_count_tools, "get_tool_breakdown", _raise_missing_dependency)
    monkeypatch.setattr("sys.argv", ["count_tools.py", "--json"])

    diag_count_tools.main()

    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["available"] is False
    assert payload["total"] is None
    assert "No module named 'mcp'" in payload["reason"]


def test_counter_cli_require_registry_exits_non_zero(monkeypatch, capsys):
    monkeypatch.setattr(diag_count_tools, "get_tool_breakdown", _raise_missing_dependency)
    monkeypatch.setattr("sys.argv", ["count_tools.py", "--require-registry"])

    assert diag_count_tools.main() == diag_count_tools.EXIT_REGISTRY_UNAVAILABLE
    capsys.readouterr()


def test_doc_count_checker_reports_unavailable_when_deps_are_missing(monkeypatch, capsys):
    monkeypatch.setattr(
        "scripts.diagnostics.count_tools.get_tool_breakdown", _raise_missing_dependency
    )

    result = update_docs_tool_count.load_tool_count()

    assert result.available is False
    assert result.total is None


def test_doc_count_checker_skip_states_it_enforced_nothing(monkeypatch, capsys):
    """A green skip must not read as a passed check."""
    monkeypatch.setattr(
        "scripts.diagnostics.count_tools.get_tool_breakdown", _raise_missing_dependency
    )
    monkeypatch.setattr("sys.argv", ["update_docs_tool_count.py", "--check"])

    with pytest.raises(SystemExit) as excinfo:
        update_docs_tool_count.main()

    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "SKIPPED" in captured.out
    assert "enforced nothing" in captured.out
    assert "Actual tool count: 0" not in captured.out


def test_doc_count_checker_require_registry_fails_when_unavailable(monkeypatch, capsys):
    """The gate that makes the smoke-job check binding rather than vacuous."""
    monkeypatch.setattr(
        "scripts.diagnostics.count_tools.get_tool_breakdown", _raise_missing_dependency
    )
    monkeypatch.setattr(
        "sys.argv", ["update_docs_tool_count.py", "--check", "--require-registry"]
    )

    with pytest.raises(SystemExit) as excinfo:
        update_docs_tool_count.main()

    assert excinfo.value.code == 1
    assert "Tool count unavailable" in capsys.readouterr().out


def test_doc_count_checker_refuses_zero_count_update(monkeypatch, capsys):
    monkeypatch.setattr(
        "scripts.diagnostics.count_tools.get_tool_breakdown", lambda **_kwargs: {}
    )
    monkeypatch.setattr("sys.argv", ["update_docs_tool_count.py", "--update"])

    with pytest.raises(SystemExit) as excinfo:
        update_docs_tool_count.main()

    assert excinfo.value.code == 1
    assert "Refusing to update" in capsys.readouterr().out
