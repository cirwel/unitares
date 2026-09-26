"""Conformance guard for the versioned public tool interface."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tomllib

import pytest

from src.interface_contract import (
    FEDERATION_LIFECYCLE_CAPABILITIES,
    INTERFACE_CONTRACT_VERSION,
    LIFECYCLE_ENVELOPE_SCHEMA,
    LIFECYCLE_SUCCESS_REQUIRED_FIELDS,
    SUPPORTED_MCP_SPECIFIER,
    build_interface_contract,
    get_interface_contract_summary,
    get_public_tool_definitions,
)
from src.mcp_compat import get_tool_input_schema
from src.mcp_handlers.middleware.envelope_step import build_experience_envelope


pytestmark = pytest.mark.usefixtures("first_party_tool_surface")


def _schema_digest(schema: dict) -> str:
    encoded = json.dumps(
        schema,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _expected(mode: str = "full") -> dict[str, dict]:
    return {
        tool.name: get_tool_input_schema(tool, {}) or {}
        for tool in get_public_tool_definitions(mode)
    }


def test_checked_in_contract_matches_runtime():
    artifact = Path("docs/interface-contract.v1.json")
    assert json.loads(artifact.read_text()) == build_interface_contract()


def test_capability_schema_hashes_match_public_definitions():
    contract = build_interface_contract()
    expected = _expected("full")

    assert {item["name"] for item in contract["capabilities"]} == set(expected)
    for item in contract["capabilities"]:
        assert item["input_schema_sha256"] == _schema_digest(
            expected[item["name"]]
        )


@pytest.mark.parametrize(
    ("hidden_name", "also_hidden"),
    [
        ("health_check", set()),
        ("onboard", {"start_session"}),
    ],
)
def test_hidden_schema_backed_tools_and_their_aliases_stay_out_of_every_catalog(
    monkeypatch,
    hidden_name,
    also_hidden,
):
    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS

    monkeypatch.setattr(_TOOL_DEFINITIONS[hidden_name], "hidden", True)

    direct = {tool.name for tool in get_public_tool_definitions("full")}
    gateway = {
        tool.name
        for tool in get_public_tool_definitions("full", include_unmounted=True)
    }

    assert hidden_name not in direct
    assert hidden_name not in gateway
    assert also_hidden.isdisjoint(direct)
    assert also_hidden.isdisjoint(gateway)


def test_federation_contract_names_live_negotiation_and_lifecycle_envelope():
    contract = build_interface_contract()
    federation = contract["federation"]

    assert contract["version"] == INTERFACE_CONTRACT_VERSION
    assert federation["negotiation"] == {
        "tool": "list_tools",
        "arguments": {"lite": True},
        "contract_path": "interface_contract",
        "capabilities_path": "tools[*].name",
    }
    assert federation["lifecycle"]["schema"] == LIFECYCLE_ENVELOPE_SCHEMA
    assert federation["lifecycle"]["capabilities"] == list(
        FEDERATION_LIFECYCLE_CAPABILITIES
    )
    assert federation["lifecycle"]["success_envelope"]["required"] == list(
        LIFECYCLE_SUCCESS_REQUIRED_FIELDS
    )


def test_declared_mcp_support_matches_install_dependency():
    dependencies = tomllib.loads(Path("pyproject.toml").read_text())["project"][
        "dependencies"
    ]
    assert f"mcp{SUPPORTED_MCP_SPECIFIER}" in dependencies


@pytest.mark.asyncio
async def test_list_tools_is_the_live_federation_handshake(monkeypatch):
    """list_tools(lite=true) negotiates the complete capability contract."""
    from src.mcp_handlers.introspection.tool_introspection import handle_list_tools

    monkeypatch.setattr("src.tool_modes.TOOL_MODE", "progressive")
    result = await handle_list_tools({"lite": True})
    payload = json.loads(result[0].text)

    assert payload["interface_contract"] == get_interface_contract_summary()
    names = {tool["name"] for tool in payload["tools"]}
    assert names == set(_expected("full"))
    assert set(FEDERATION_LIFECYCLE_CAPABILITIES) <= names
    assert payload["advertisement"]["mode"] == "progressive"


@pytest.mark.parametrize(
    ("friendly_name", "canonical_name", "payload"),
    [
        (
            "start_session",
            "onboard",
            {"success": True, "uuid": "agent-1", "client_session_id": "s-1"},
        ),
        (
            "sync_state",
            "process_agent_update",
            {"success": True, "decision": {"action": "proceed"}},
        ),
        (
            "record_result",
            "outcome_event",
            {"success": True, "outcome_id": "outcome-1"},
        ),
    ],
)
def test_federation_lifecycle_aliases_emit_required_success_envelope(
    friendly_name,
    canonical_name,
    payload,
):
    envelope = build_experience_envelope(
        friendly_name,
        canonical_name,
        payload,
    )

    assert set(LIFECYCLE_SUCCESS_REQUIRED_FIELDS) <= envelope.keys()
    assert envelope["success"] is True
    assert envelope["tool"] == friendly_name


@pytest.mark.asyncio
@pytest.mark.parametrize("check_ins", [0, 2, 30])
@pytest.mark.parametrize(
    "arguments",
    [{}, {"verbosity": "standard"}, {"verbosity": "full"}, {"include_state": True}],
)
async def test_check_working_state_emits_required_success_envelope_on_every_tier(
    check_ins, arguments
):
    """From the real handler, not a fixture: this case used to hand-supply a
    `guidance` the minimal tier sets only for an uninitialized agent, so the
    default read passed while shipping without next_action."""
    from tests.helpers.metrics_producer import real_metrics_payload

    payload, validated = await real_metrics_payload(arguments, check_ins=check_ins)
    envelope = build_experience_envelope(
        "check_working_state", "get_governance_metrics", payload, validated
    )

    assert set(LIFECYCLE_SUCCESS_REQUIRED_FIELDS) <= envelope.keys()
    assert envelope["success"] is True
    assert envelope["tool"] == "check_working_state"
    assert envelope["next_action"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["progressive", "full"])
async def test_rest_and_stdio_discovery_share_advertised_names_and_schemas(
    monkeypatch, mode,
):
    from src.http_routes.tools import http_list_tools
    import src.mcp_server_std as stdio

    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)
    monkeypatch.setattr(stdio, "STDIO_PROXY_HTTP_URL", None)
    monkeypatch.setattr(stdio, "STDIO_PROXY_URL", None)
    monkeypatch.setattr("src.tool_modes.TOOL_MODE", mode)

    request = SimpleNamespace(
        query_params={"mode": mode},
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

    expected = _expected(mode)
    assert response.status_code == 200
    assert rest == expected
    assert rest_payload["mode"] == mode
    assert stdio_surface == expected
    assert rest_payload["interface_contract"] == get_interface_contract_summary()
    assert rest_payload["total_available"] == len(_expected("full"))


@pytest.mark.asyncio
async def test_streamable_mcp_advertises_the_progressive_surface(monkeypatch):
    """The mount lists the progressive entry surface by default.

    The mount registers the whole surface and filters only its listing
    (src/tool_mode_listing.py), so the contract is checked against what
    list_tools() returns, not against the tool manager's registrations.
    """
    from src import mcp_server

    monkeypatch.setattr("src.tool_modes.TOOL_MODE", "progressive")
    expected = _expected("progressive")
    advertised = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}

    assert set(advertised) == set(expected)
    for name, schema in expected.items():
        # FastMCP normalizes schemas while creating typed wrappers, but the
        # accepted top-level arguments must remain the contract arguments.
        listed_schema = get_tool_input_schema(advertised[name], {}) or {}
        assert set(listed_schema.get("properties", {})) == set(
            schema.get("properties", {})
        )


@pytest.mark.asyncio
async def test_streamable_mcp_full_mode_still_filters_hidden_handlers(monkeypatch):
    from src import mcp_server
    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS

    monkeypatch.setattr("src.tool_modes.TOOL_MODE", "full")
    monkeypatch.setattr(_TOOL_DEFINITIONS["health_check"], "hidden", True)

    advertised = {tool.name for tool in await mcp_server.mcp.list_tools()}

    assert "health_check" not in advertised
    assert advertised == {
        tool.name for tool in get_public_tool_definitions("full")
    }


def test_streamable_mcp_registers_the_complete_contract_in_every_mode():
    """Every complete-contract name dispatches whatever the listing mode.

    Registration is mode-independent; only the listing is filtered. So the
    contract's names are all present in the tool manager even though the
    process default (minimal) advertises five of them.
    """
    from src import mcp_server

    registered = set(mcp_server.mcp._tool_manager._tools)
    assert set(_expected("full")) <= registered


@pytest.mark.parametrize("mode", ["minimal", "standard", "lite", "full", "operator_readonly", "core", "unknown"])
def test_legacy_mode_has_identical_federation_contract(mode):
    assert build_interface_contract(mode) == build_interface_contract()
    assert get_public_tool_definitions(mode) == get_public_tool_definitions("full")


def test_complete_catalog_preserves_every_registered_capability(first_party_tool_surface):
    from src.mcp_handlers.decorators import get_tool_registry
    from src.mcp_handlers.tool_stability import AGENT_WORKFLOW_ALIASES
    names = {tool.name for tool in get_public_tool_definitions()}
    assert names == set(get_tool_registry()) | set(AGENT_WORKFLOW_ALIASES)
