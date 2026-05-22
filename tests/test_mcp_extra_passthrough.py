"""Regression tests for FastMCP extra-argument passthrough wiring."""

from __future__ import annotations


def test_process_agent_update_registered_for_extra_argument_passthrough():
    """The registered FastMCP tool must preserve internal S22 envelope fields."""
    from src import mcp_server

    tool = mcp_server.mcp._tool_manager.get_tool("process_agent_update")
    assert tool is not None

    arg_model = tool.fn_metadata.arg_model
    assert arg_model.model_config.get("extra") == "allow"
    assert getattr(arg_model, "__unitaires_extra_passthrough_enabled__", False)

    # Keep the public/LLM-facing schema unchanged: internal harness fields may be
    # forwarded by wrappers, but should not become advertised agent-fillable args.
    properties = tool.parameters.get("properties", {})
    assert "comparison_key" in properties
    assert "harness_type" not in properties
    assert "verification_source" not in properties
