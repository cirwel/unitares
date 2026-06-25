from scripts.analysis.count_tools import count_tools
from scripts.diagnostics.count_tools import get_total_count


def test_analysis_counter_matches_runtime_diagnostics_counter():
    by_module, total = count_tools()

    assert total == get_total_count()
    assert total >= 40
    assert sum(len(tool_names) for tool_names in by_module.values()) == total
    assert any(module.startswith("knowledge") for module in by_module)
