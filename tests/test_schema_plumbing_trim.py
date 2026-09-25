"""Advertised-only narrowing of identity plumbing (2026-09-25).

continuity_token leaves the advertised schemas of ten tools, and
start_session's alias stops advertising adapter plumbing. Each field stays on
its Pydantic model and on the canonical tool, so REST callers (which pass
undeclared keys through) and canonical /mcp/ callers are unaffected. These
tests pin both halves: gone from the advertisement, still accepted where a
real caller sends it.
"""

from __future__ import annotations

import asyncio

import pytest

from src.alias_schema import ALIAS_ADVERTISED_DROP


def _advertised(mode: str = "full") -> dict:
    from src import mcp_server, tool_modes

    previous = tool_modes.TOOL_MODE
    tool_modes.TOOL_MODE = mode
    try:
        tools = asyncio.run(mcp_server.mcp.list_tools())
        # The SDK's Tool type spells the field inputSchema or input_schema
        # depending on which mcp major a neighbouring test left loaded.
        return {
            t.name: (getattr(t, "inputSchema", None) or getattr(t, "input_schema"))
            for t in tools
        }
    finally:
        tool_modes.TOOL_MODE = previous


TOKEN_HIDDEN = (
    "check_working_state",
    "consult",
    "describe_tool",
    "list_tools",
    "use_tool",
    "search_shared_memory",
    "store_finding",
    "record_result",
    "request_review",
    "self_recovery",
)


@pytest.mark.parametrize("tool", TOKEN_HIDDEN)
def test_continuity_token_is_not_advertised(tool):
    schemas = _advertised()
    assert tool in schemas
    assert "continuity_token" not in schemas[tool].get("properties", {})
    # client_session_id is the per-call binding and stays everywhere.
    assert "client_session_id" in schemas[tool].get("properties", {})


@pytest.mark.parametrize(
    "tool",
    ("identity", "sync_state", "onboard", "get_governance_metrics",
     "knowledge", "outcome_event", "dialectic"),
)
def test_continuity_token_stays_where_it_is_load_bearing(tool):
    """identity is the rebind path; sync_state carries the SDK's strict-refusal
    retry; the canonical tools keep every field for their callers."""
    schemas = _advertised()
    assert "continuity_token" in schemas[tool].get("properties", {})


def test_sync_state_passes_an_undeclared_continuity_token_through():
    """The Python SDK's strict-refusal retry (agents/sdk/.../client.py) attaches
    continuity_token to sync_state over /mcp/. If a later cut removed the
    declaration, process_agent_update's passthrough must still keep the key."""
    from src import mcp_server

    tool = mcp_server.mcp._tool_manager.get_tool("sync_state")
    arg_model = tool.fn_metadata.arg_model
    assert arg_model.model_config.get("extra") == "allow"
    parsed = arg_model.model_validate(
        {"response_text": "x", "continuity_token": "v1.token", "undeclared_key": 1}
    )
    dumped = parsed.model_dump()
    assert dumped.get("continuity_token") == "v1.token" or (
        (parsed.model_extra or {}).get("continuity_token") == "v1.token"
    )
    # The property that protects the retry if the declaration ever goes: a key
    # the schema does not declare survives the /mcp/ argument model.
    assert (parsed.model_extra or {}).get("undeclared_key") == 1


@pytest.mark.parametrize(
    "model_path, payload",
    [
        ("src.mcp_handlers.schemas.lifecycle:SelfRecoveryParams", {"action": "check"}),
        ("src.mcp_handlers.schemas.core:ConsultParams", {}),
    ],
)
def test_models_still_accept_the_hidden_token(model_path, payload):
    """REST validates against the model, not the advertisement. ConsultParams
    is extra='forbid', so the field has to stay declared on the model."""
    import importlib

    module_name, class_name = model_path.split(":")
    model = getattr(importlib.import_module(module_name), class_name)
    fields = model.model_fields
    assert "continuity_token" in fields


START_SESSION_DROPPED = ALIAS_ADVERTISED_DROP["start_session"]
START_SESSION_KEPT = (
    "initial_state", "model_type", "client_session_id", "resume",
    "force_new", "parent_agent_id", "spawn_reason", "name", "response_mode",
)


def test_start_session_alias_advertises_only_the_mint_contract():
    schemas = _advertised()
    alias = set(schemas["start_session"].get("properties", {}))
    canonical = set(schemas["onboard"].get("properties", {}))

    assert START_SESSION_DROPPED.isdisjoint(alias)
    # Every dropped field is still on canonical onboard, where every code
    # caller that sets it sends it.
    assert START_SESSION_DROPPED <= canonical
    assert set(START_SESSION_KEPT) <= alias
    # initial_state still resolves: its $defs survived the narrowing.
    assert "$defs" in schemas["start_session"]


def test_progressive_surface_uses_the_same_narrowing():
    progressive = _advertised("progressive")
    assert "continuity_token" not in progressive["record_result"]["properties"]
    assert START_SESSION_DROPPED.isdisjoint(progressive["start_session"]["properties"])
