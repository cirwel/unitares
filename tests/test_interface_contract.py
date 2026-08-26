"""Conformance guard for the versioned public tool interface."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.interface_contract import (
    build_interface_contract,
    get_interface_contract_summary,
    get_public_tool_definitions,
)
from src.mcp_compat import get_tool_input_schema


def _schema_digest(schema: dict) -> str:
    encoded = json.dumps(
        schema,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _expected_lite() -> dict[str, dict]:
    return {
        tool.name: get_tool_input_schema(tool, {}) or {}
        for tool in get_public_tool_definitions("lite")
    }


def test_checked_in_lite_contract_matches_runtime():
    artifact = Path("docs/interface-contract.v1.json")
    assert json.loads(artifact.read_text()) == build_interface_contract("lite")


def test_capability_schema_hashes_match_public_definitions():
    contract = build_interface_contract("lite")
    expected = _expected_lite()

    assert {item["name"] for item in contract["capabilities"]} == set(expected)
    for item in contract["capabilities"]:
        assert item["input_schema_sha256"] == _schema_digest(
            expected[item["name"]]
        )


@pytest.mark.asyncio
async def test_rest_and_stdio_discovery_share_lite_names_and_schemas(
    monkeypatch,
):
    from src.http_routes.tools import http_list_tools
    import src.mcp_server_std as stdio

    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)
    monkeypatch.setattr(stdio, "STDIO_PROXY_HTTP_URL", None)
    monkeypatch.setattr(stdio, "STDIO_PROXY_URL", None)

    request = SimpleNamespace(
        query_params={"mode": "lite"},
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    response = await http_list_tools(request)
    rest_payload = json.loads(response.body)
    rest = {
        entry["function"]["name"]: entry["function"]["parameters"]
        for entry in rest_payload["tools"]
    }
    stdio_tools = await stdio.list_tools()
    stdio_surface = {
        tool.name: get_tool_input_schema(tool, {}) or {}
        for tool in stdio_tools
    }

    expected = _expected_lite()
    assert response.status_code == 200
    assert rest == expected
    assert stdio_surface == expected
    assert rest_payload["interface_contract"] == get_interface_contract_summary(
        "lite"
    )


def test_streamable_mcp_advertises_the_lite_contract():
    from src import mcp_server

    expected = _expected_lite()
    manager = mcp_server.mcp._tool_manager
    advertised = manager._tools

    assert set(advertised) == set(expected)
    for name, schema in expected.items():
        # FastMCP normalizes schemas while creating typed wrappers, but the
        # accepted top-level arguments must remain the contract arguments.
        assert set(advertised[name].parameters.get("properties", {})) == set(
            schema.get("properties", {})
        )
