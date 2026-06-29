"""Regression tests for FastMCP extra-argument passthrough wiring."""

from __future__ import annotations


def test_checkin_tool_preserves_s22_extra_argument_passthrough():
    """The advertised check-in tool (``sync_state``) must preserve internal S22
    envelope fields.

    ``process_agent_update`` is the raw implementation underneath. It is no longer
    advertised in the lite orientation surface (it was a duplicate of the promoted
    ``sync_state`` name), so the passthrough contract is asserted on the wire-facing
    alias — which inherits passthrough because its target is in
    ``EXTRA_ARGUMENT_PASSTHROUGH_TOOLS``.
    """
    from src import mcp_server

    tool = mcp_server.mcp._tool_manager.get_tool("sync_state")
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


def test_process_agent_update_handler_still_dispatchable():
    """Hidden from the lite wire, but still a register=True handler — so the
    gateway, hooks, and compat wrappers can call it by raw name (the server
    dispatches any registered handler whether or not it is advertised)."""
    from src.mcp_handlers import TOOL_HANDLERS

    assert "process_agent_update" in TOOL_HANDLERS
