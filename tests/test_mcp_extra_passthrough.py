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
    """Still a register=True handler, so REST ``/v1/tools/call`` and in-process
    callers can invoke it by raw name.

    CAVEAT (learned the hard way, 2026-06-30): "registered handler" does NOT
    mean "callable over the MCP ``/mcp/`` wire". FastMCP only dispatches tools
    present in ``mcp._tool_manager`` (the advertised set). #1292 dropped the raw
    twin from that set, so every resident/SDK/gateway caller that reached it by
    raw name over ``/mcp/`` broke with ``Unknown tool``. Raw-name access is a
    REST/in-process affordance only — MCP-wire callers MUST use the advertised
    alias. See test_resident_workflow_aliases_advertised_on_mcp_wire below.
    """
    from src.mcp_handlers import TOOL_HANDLERS

    assert "process_agent_update" in TOOL_HANDLERS


def test_resident_workflow_aliases_advertised_on_mcp_wire():
    """The workflow aliases residents/SDK/gateway call over ``/mcp/`` MUST be
    present in the FastMCP tool manager (the advertised wire) — not merely in
    TOOL_HANDLERS.

    This guards the 2026-06-30 outage: #1292 pruned the raw twins
    (process_agent_update / get_governance_metrics / outcome_event) from the
    lite wire, and the check-in/metrics/outcome paths call these aliases. A
    future prune that drops an alias from the wire would silently dark every
    resident again; assert on the wire, not on internal dispatch.
    """
    from src import mcp_server

    for alias in ("sync_state", "check_working_state", "record_result"):
        assert mcp_server.mcp._tool_manager.get_tool(alias) is not None, (
            f"{alias!r} missing from the MCP wire — residents call it over /mcp/"
        )
