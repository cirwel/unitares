"""describe_tool returns block must mention every documented response field.

Catches the regression class that triggered spec rev 3 — a documented
contract drifting from actual behavior because nothing tests the description.
"""

import pytest


@pytest.mark.asyncio
async def test_process_agent_update_describe_mentions_prediction_id():
    from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
    result = await handle_describe_tool({"tool_name": "process_agent_update", "lite": False})
    body = result[0].text  # MCP TextContent
    assert "prediction_id" in body, (
        "describe_tool returns block must document prediction_id "
        "(spec §6 — exposed in default response modes)"
    )


@pytest.mark.asyncio
async def test_process_agent_update_describe_mentions_warnings():
    from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
    result = await handle_describe_tool({"tool_name": "process_agent_update", "lite": False})
    body = result[0].text
    assert "warnings" in body, (
        "describe_tool returns block must document warnings "
        "(spec §2 — surfaced via formatters)"
    )


@pytest.mark.asyncio
async def test_process_agent_update_describe_mentions_recent_tool_results():
    from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
    result = await handle_describe_tool({"tool_name": "process_agent_update", "lite": False})
    body = result[0].text
    assert "recent_tool_results" in body, (
        "describe_tool block must document recent_tool_results "
        "(spec §1 — new agent contract field)"
    )


@pytest.mark.asyncio
async def test_process_agent_update_describe_mentions_s22_h5_fields():
    from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
    result = await handle_describe_tool({"tool_name": "process_agent_update", "lite": False})
    body = result[0].text
    for field in ("harness_type", "comparison_key", "task_label", "task_outcome"):
        assert field in body, f"describe_tool must document S22 H5 field {field}"
