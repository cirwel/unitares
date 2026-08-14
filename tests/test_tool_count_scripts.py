"""Pins for the canonical tool-count scripts (scripts/diagnostics/).

The counter reads the runtime decorator registry — the same source dispatch
uses — and must degrade to a visible warning plus a zero count (never a
crash) in dependency-less environments like the doc-validation CI runner.
"""

from scripts.diagnostics import count_tools as diag_count_tools
from scripts.diagnostics import update_docs_tool_count


def test_registry_counter_reports_tools():
    total = diag_count_tools.get_total_count()
    breakdown = diag_count_tools.get_tool_breakdown()

    assert total >= 40
    assert sum(breakdown.values()) == total
    assert any(module.startswith("knowledge") for module in breakdown)


def test_doc_count_checker_skips_when_runtime_deps_are_missing(monkeypatch, capsys):
    def missing_dependency_counter(**_kwargs):
        raise ModuleNotFoundError("No module named 'mcp'")

    monkeypatch.setattr(
        "scripts.diagnostics.count_tools.get_total_count", missing_dependency_counter
    )

    assert update_docs_tool_count.load_tool_count() == 0
    assert "Tool count unavailable" in capsys.readouterr().err


def test_counter_cli_skips_when_runtime_deps_are_missing(monkeypatch, capsys):
    def missing_dependency_counter(**_kwargs):
        raise ModuleNotFoundError("No module named 'mcp'")

    monkeypatch.setattr(diag_count_tools, "get_total_count", missing_dependency_counter)
    monkeypatch.setattr(diag_count_tools, "get_tool_breakdown", missing_dependency_counter)
    monkeypatch.setattr("sys.argv", ["count_tools.py", "--by-module"])

    diag_count_tools.main()

    captured = capsys.readouterr()
    assert "Tool count unavailable" in captured.err
    assert " 0 tools" in captured.out


def test_doc_count_checker_refuses_zero_count_update(monkeypatch, capsys):
    def zero_counter(**_kwargs):
        return 0

    monkeypatch.setattr("scripts.diagnostics.count_tools.get_total_count", zero_counter)
    monkeypatch.setattr("sys.argv", ["update_docs_tool_count.py", "--update"])

    try:
        update_docs_tool_count.main()
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("expected SystemExit(1) on zero-count --update")

    assert "Refusing to update" in capsys.readouterr().out
