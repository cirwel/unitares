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


def test_breakdown_buckets_routers_by_declaring_module():
    """An ``action_router``'s handler is defined inside decorators.py, so a
    breakdown keyed on ``handler.__module__`` filed the eight consolidated
    routers under ``decorators`` (F10 of the 2026-09-12 tool-surface audit).
    The bucket is the module that declared the tool."""
    breakdown = diag_count_tools.get_tool_breakdown()

    assert "decorators" not in breakdown
    assert "consolidated" in breakdown


def test_breakdown_files_a_plugin_router_under_the_plugin():
    """The same distinction keeps a plugin's router out of the governance
    buckets: a router declared from another package counts against that
    package, not against ``decorators``."""
    import types

    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS, action_router

    module = types.ModuleType("fake_governance_plugin.handlers")
    module.__dict__["action_router"] = action_router
    exec(
        "async def _ping(arguments):\n"
        "    return []\n"
        "action_router('count_probe_plugin_router', actions={'ping': _ping})\n",
        module.__dict__,
    )
    try:
        breakdown = diag_count_tools.get_tool_breakdown()
    finally:
        _TOOL_DEFINITIONS.pop("count_probe_plugin_router", None)

    assert breakdown.get("fake_governance_plugin.handlers") == 1
    assert "decorators" not in breakdown


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


# --- The advertised roster: a second, different quantity ---------------------


def test_resolve_advertised_count_is_registry_union_workflow_aliases():
    from src.mcp_handlers.decorators import get_tool_registry
    from src.mcp_handlers.tool_stability import AGENT_WORKFLOW_ALIASES
    from src.tool_modes import advertised_tool_names_full

    result = diag_count_tools.resolve_advertised_tool_count()

    assert result.available is True
    assert result.total == len(advertised_tool_names_full())
    assert result.total == len(set(get_tool_registry()) | set(AGENT_WORKFLOW_ALIASES))
    assert sum(result.breakdown.values()) == result.total
    assert result.breakdown["registry"] == len(get_tool_registry())
    assert result.total > diag_count_tools.resolve_tool_count().total


def test_resolve_advertised_reports_unavailable_not_zero(monkeypatch):
    def _raise():
        raise ModuleNotFoundError("No module named 'mcp'")

    monkeypatch.setattr(diag_count_tools, "_advertised_roster", _raise)

    result = diag_count_tools.resolve_advertised_tool_count()

    assert result.available is False
    assert result.total is None
    assert "No module named 'mcp'" in result.reason


# --- Doc guard: each quantity checked against its own source -----------------

_REGISTRY_COUNT = 42
_ADVERTISED_COUNT = 50


def _counts(registry=_REGISTRY_COUNT, advertised=_ADVERTISED_COUNT):
    def available(total):
        return diag_count_tools.ToolCount(available=True, total=total)

    return {
        update_docs_tool_count.REGISTRY: available(registry),
        update_docs_tool_count.ADVERTISED: available(advertised),
    }


def _run_guard(monkeypatch, tmp_path, docs, *argv, required=None, counts=None):
    """Run main() against ``docs`` ({relpath: text}) under ``tmp_path``."""
    for relpath, text in docs.items():
        (tmp_path / relpath).write_text(text)
    required = required or {}
    monkeypatch.setattr(update_docs_tool_count, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        update_docs_tool_count,
        "DOC_FILES",
        {relpath: frozenset(required.get(relpath, ())) for relpath in docs},
    )
    monkeypatch.setattr(
        update_docs_tool_count, "load_tool_counts", lambda: counts or _counts()
    )
    monkeypatch.setattr("sys.argv", ["update_docs_tool_count.py", *argv])
    with pytest.raises(SystemExit) as excinfo:
        update_docs_tool_count.main()
    return excinfo.value.code


def test_doc_guard_passes_a_correct_advertised_count(monkeypatch, tmp_path, capsys):
    code = _run_guard(
        monkeypatch,
        tmp_path,
        {"README.md": "The catalog lists **50 advertised tools**.\n"},
        "--check",
        required={"README.md": {update_docs_tool_count.ADVERTISED}},
    )

    assert code == 0
    assert "checked advertised" in capsys.readouterr().out


def test_doc_guard_fails_a_wrong_advertised_count(monkeypatch, tmp_path, capsys):
    code = _run_guard(
        monkeypatch,
        tmp_path,
        {"README.md": "The catalog lists **49 advertised tools**.\n"},
        "--check",
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "advertised: found 49, expected 50" in out


def test_doc_guard_checks_registry_framing_against_the_registry(monkeypatch, tmp_path, capsys):
    """The EVIDENCE_AND_LIMITS shape: registry count in registry framing passes,
    and the advertised number written in that shape is rejected — the two
    quantities are distinct and neither falls back to the other."""
    ok = _run_guard(
        monkeypatch,
        tmp_path,
        {"ledger.md": "| **42 tools** on the wire | ... |\n"},
        "--check",
        required={"ledger.md": {update_docs_tool_count.REGISTRY}},
    )
    assert ok == 0
    capsys.readouterr()

    wrong = _run_guard(
        monkeypatch, tmp_path, {"ledger.md": "| **50 tools** on the wire |\n"}, "--check"
    )
    assert wrong == 1
    assert "registry: found 50, expected 42" in capsys.readouterr().out


def test_doc_guard_checks_both_quantities_in_one_file(monkeypatch, tmp_path, capsys):
    code = _run_guard(
        monkeypatch,
        tmp_path,
        {"README.md": "the 42 registered tools plus 8 workflow aliases, 50 advertised tools in all\n"},
        "--check",
        required={
            "README.md": {update_docs_tool_count.REGISTRY, update_docs_tool_count.ADVERTISED}
        },
    )

    assert code == 0
    assert "checked advertised, registry (2 claim(s))" in capsys.readouterr().out


def test_doc_guard_require_registry_fails_when_advertised_count_unavailable(monkeypatch, capsys):
    """Either count being unavailable fails the binding gate; the other does
    not stand in for it."""
    def _raise():
        raise ModuleNotFoundError("No module named 'mcp'")

    monkeypatch.setattr("scripts.diagnostics.count_tools._advertised_roster", _raise)
    monkeypatch.setattr(
        "sys.argv", ["update_docs_tool_count.py", "--check", "--require-registry"]
    )

    with pytest.raises(SystemExit) as excinfo:
        update_docs_tool_count.main()

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "Tool count unavailable" in out
    assert "advertised: No module named 'mcp'" in out


def test_doc_guard_fails_a_file_that_lost_its_required_count(monkeypatch, tmp_path, capsys):
    """#2129: the full-profile figure written as a bare ``**50**`` matched no
    marker, which silently removed the file from enforcement."""
    code = _run_guard(
        monkeypatch,
        tmp_path,
        {"README.md": "The full profile advertises **50**.\n"},
        "--check",
        required={"README.md": {update_docs_tool_count.ADVERTISED}},
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "README.md: states no advertised tool count" in out
    assert "NN advertised tools" in out


def test_doc_guard_reports_a_file_with_no_count(monkeypatch, tmp_path, capsys):
    """A guarded file with no count and no requirement passes, but visibly."""
    code = _run_guard(
        monkeypatch, tmp_path, {"START_HERE.md": "No numbers here.\n"}, "--check"
    )

    assert code == 0
    assert "START_HERE.md: no recognised tool count — nothing enforced" in capsys.readouterr().out


def test_doc_guard_fails_a_missing_guarded_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(update_docs_tool_count, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(update_docs_tool_count, "DOC_FILES", {"gone.md": frozenset()})
    monkeypatch.setattr(update_docs_tool_count, "load_tool_counts", _counts)
    monkeypatch.setattr("sys.argv", ["update_docs_tool_count.py", "--check"])

    with pytest.raises(SystemExit) as excinfo:
        update_docs_tool_count.main()

    assert excinfo.value.code == 1
    assert "gone.md: guarded file not found" in capsys.readouterr().out


def test_doc_guard_update_writes_each_quantity(monkeypatch, tmp_path, capsys):
    doc = tmp_path / "README.md"
    code = _run_guard(
        monkeypatch,
        tmp_path,
        {"README.md": "**40 tools**, 41 registered tools, 38+ tools, 47 advertised tools\n"},
        "--update",
        required={
            "README.md": {update_docs_tool_count.REGISTRY, update_docs_tool_count.ADVERTISED}
        },
    )

    assert code == 0
    assert doc.read_text() == (
        "**42 tools**, 42 registered tools, 42 tools, 50 advertised tools\n"
    )
    assert "Updated 1 files" in capsys.readouterr().out


def test_doc_guard_refuses_zero_advertised_count_update(monkeypatch, tmp_path, capsys):
    code = _run_guard(
        monkeypatch,
        tmp_path,
        {"README.md": "47 advertised tools\n"},
        "--update",
        counts=_counts(advertised=0),
    )

    assert code == 1
    assert "Refusing to update" in capsys.readouterr().out
    assert (tmp_path / "README.md").read_text() == "47 advertised tools\n"


def test_doc_guard_markers_are_mutually_exclusive():
    """Every number is claimed by exactly one marker, so no count is checked
    against two quantities at once."""
    line = "**42 tools** (42 tools) count: 42) 42+ tools 42 registered tools 50 advertised tools"
    claims = [
        (m.quantity, match.start(1))
        for m in update_docs_tool_count.MARKERS
        for match in m.pattern.finditer(line)
    ]

    assert len(claims) == len({start for _, start in claims}) == 6
