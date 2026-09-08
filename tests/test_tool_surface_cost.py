"""Measure final MCP definitions, and fail visibly when measurement is unknown."""

import asyncio
import json

import pytest

from scripts.diagnostics import tool_surface_cost as cost

pytestmark = pytest.mark.usefixtures("first_party_tool_surface")


def test_byte_count_is_utf8_and_includes_result_framing():
    assert cost._wire_bytes({"text": "é"}) == len(b'{"text":"\xc3\xa9"}')
    assert cost._wire_bytes({"tools": []}) == 12


def test_default_measurement_matches_the_sdk_list_result(monkeypatch):
    from mcp.types import ListToolsResult
    from src import mcp_server, tool_modes

    monkeypatch.setattr(tool_modes, "TOOL_MODE", "standard")
    listed = asyncio.run(mcp_server.mcp.list_tools())
    serialized = ListToolsResult(tools=listed).model_dump_json(
        by_alias=True, exclude_none=True,
    ).encode("utf-8")
    measured = cost.measure_profile("standard")
    assert measured.available
    assert measured.total_bytes == len(serialized)
    assert {tool.name for tool in measured.tools} == {tool.name for tool in listed}
    assert measured.total_bytes > sum(tool.total_bytes for tool in measured.tools)


def test_source_catalog_is_an_explicit_different_measurement():
    mounted = cost.measure_profile("standard")
    catalog = cost.measure_profile("standard", "catalog")
    assert mounted.tool_count == catalog.tool_count
    assert mounted.total_bytes != catalog.total_bytes


def test_missing_dependencies_are_unknown_and_cannot_pass_the_ladder(monkeypatch):
    def unavailable(*args):
        raise ModuleNotFoundError("missing MCP runtime")

    monkeypatch.setattr(cost, "_definitions", unavailable)
    measured = cost.measure_profiles()
    assert all(not row.available and row.total_bytes is None for row in measured.values())
    assert all(row.estimated_tokens() is None for row in measured.values())
    assert cost.check_ladder(measured) == [
        "ladder unchecked: unavailable profiles ['minimal', 'standard', 'lite', 'full']"
    ]


def test_ladder_requires_all_rungs_and_checks_measured_names():
    tool = cost.ToolCost("only_minimal", 10, 1, 1, 0)
    rows = {name: cost.ProfileCost(name, True, (i + 1) * 100, [])
            for i, name in enumerate(cost.LADDER)}
    rows["minimal"] = cost.ProfileCost("minimal", True, 100, [tool])
    assert any("only_minimal" in violation for violation in cost.check_ladder(rows))
    assert "unchecked" in cost.check_ladder({"standard": rows["standard"]})[0]


@pytest.mark.parametrize("argv", [
    ["--mode", "standrad"], ["--mode", "standard", "--check-ladder"],
    ["--bytes-per-token", "0"],
])
def test_cli_rejects_misleading_measurement_requests(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["tool_surface_cost", *argv])
    with pytest.raises(SystemExit) as exc:
        cost.main()
    assert exc.value.code == 2


def test_json_discloses_surface_estimate_and_boilerplate(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["tool_surface_cost", "--mode", "minimal", "--json", "--boilerplate"])
    assert cost.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["surface"] == "mcp"
    assert payload["tokens_are_estimates"]
    assert "excludes JSON-RPC" in payload["serialization"]
    assert payload["boilerplate_savings"]["minimal"]["property_title"] == 0


def test_hypothetical_null_cut_preserves_multi_type_unions():
    schema = {"properties": {"value": {"anyOf": [
        {"type": "string"}, {"type": "number"}, {"type": "null"},
    ]}}}
    assert cost._without_null_unions(schema) == schema
