from scripts.analysis.count_tools import count_tools
from scripts.diagnostics.count_tools import get_total_count
from scripts.dev import update_docs_tool_count


def test_analysis_counter_matches_runtime_diagnostics_counter():
    by_module, total = count_tools()

    assert total == get_total_count()
    assert total >= 40
    assert sum(len(tool_names) for tool_names in by_module.values()) == total
    assert any(module.startswith("knowledge") for module in by_module)


def test_dev_doc_count_checker_skips_when_runtime_deps_are_missing(monkeypatch, capsys):
    def missing_dependency_counter():
        raise ModuleNotFoundError("No module named 'mcp'")

    monkeypatch.setattr("scripts.analysis.count_tools.count_tools", missing_dependency_counter)

    assert update_docs_tool_count.load_tool_count() == 0
    assert "Tool count unavailable" in capsys.readouterr().out
