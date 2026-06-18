"""describe_tool returns block must mention every documented response field.

Catches the regression class that triggered spec rev 3 — a documented
contract drifting from actual behavior because nothing tests the description.
"""

import pytest


def test_get_server_info_schema_tool_is_dispatch_registered():
    """A tool advertised by MCP schemas must also resolve at dispatch time."""
    from src.mcp_handlers import TOOL_HANDLERS
    from src.tool_schemas import get_tool_definitions

    schema_names = {tool.name for tool in get_tool_definitions(verbosity="full")}

    assert "get_server_info" in schema_names
    assert "get_server_info" in TOOL_HANDLERS, (
        "get_server_info is advertised in tool schemas and health_check docs; "
        "it must be dispatch-registered, not an unknown tool at call time"
    )


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
async def test_process_agent_update_lite_mentions_current_parameters():
    import json
    from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool

    result = await handle_describe_tool({"tool_name": "process_agent_update", "lite": True})
    data = json.loads(result[0].text)
    params = "\n".join(data["parameters"])

    for field in (
        "client_session_id",
        "response_text",
        "complexity",
        "confidence",
        "task_type",
        "response_mode",
        "require_strong_identity",
        "recent_tool_results",
    ):
        assert field in params

    assert "parameters:" not in params
    assert "ethical_drift" not in params


@pytest.mark.asyncio
async def test_workflow_alias_describe_resolves_to_canonical_schema():
    import json
    from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool

    result = await handle_describe_tool({"tool_name": "sync_state", "lite": True})
    data = json.loads(result[0].text)
    params = "\n".join(data["parameters"])

    assert data["tool"] == "sync_state"
    assert data["primary_tool"] == "sync_state"
    assert data["implementation_tool"] == "process_agent_update"
    assert data["canonical_tool"] == "process_agent_update"
    assert data["alias"]["reason"] == "intuitive_alias"
    assert data["alias"]["role"] == "primary_agent_workflow"
    assert "response_text" in params
    assert "complexity" in params
    assert "confidence" in params
    assert "sync_state(" in json.dumps(data["common_patterns"])


@pytest.mark.asyncio
async def test_list_tools_lite_surfaces_workflow_aliases():
    import json
    from src.mcp_handlers.introspection.tool_introspection import handle_list_tools

    result = await handle_list_tools({"essential_only": True, "lite": True})
    data = json.loads(result[0].text)
    names = [tool["name"] for tool in data["tools"]]

    for alias in (
        "start_session",
        "sync_state",
        "check_working_state",
        "search_shared_memory",
        "record_result",
        "request_review",
    ):
        assert alias in names

    for raw_tool in (
        "onboard",
        "process_agent_update",
        "get_governance_metrics",
        "outcome_event",
    ):
        assert raw_tool not in names

    assert data["getting_started_path"][0]["tool"] == "start_session"
    assert data["getting_started_path"][1]["tool"] == "sync_state"


@pytest.mark.asyncio
async def test_outcome_event_lite_mentions_calibration_parameters():
    import json
    from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool

    result = await handle_describe_tool({"tool_name": "outcome_event", "lite": True})
    data = json.loads(result[0].text)
    params = "\n".join(data["parameters"])

    for field in (
        "outcome_type",
        "confidence",
        "prediction_id",
        "decision_action",
        "session_id",
        "verification_source",
    ):
        assert field in params


@pytest.mark.asyncio
async def test_outcome_event_describe_mentions_current_types_and_provenance():
    import json
    from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool

    result = await handle_describe_tool({"tool_name": "outcome_event", "lite": False})
    description = json.loads(result[0].text)["tool"]["description"]

    for text in (
        "trajectory_validated",
        "dialectic_resolved",
        "prediction_id",
        "decision_action",
        "verification_source",
    ):
        assert text in description


@pytest.mark.asyncio
async def test_process_agent_update_describe_mentions_s22_h5_fields():
    from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
    result = await handle_describe_tool({"tool_name": "process_agent_update", "lite": False})
    body = result[0].text
    for field in ("harness_type", "comparison_key", "task_label", "task_outcome"):
        assert field in body, f"describe_tool must document S22 H5 field {field}"


# Issue #431 — Tool registry drift between describe_tool and the live registry.


@pytest.mark.asyncio
async def test_health_check_describe_mentions_agent_signature():
    """agent_signature appears in every success response (response_base.py:107)
    but was missing from health_check's RETURNS block. Other tools that pass
    through the same wrapper share the same gap; this test guards health_check
    specifically (the case surfaced in #431).

    Inspects the description text inside the response envelope, not the outer
    envelope — the envelope auto-attaches agent_signature via success_response,
    which would mask the docs gap if we asserted on the raw body."""
    import json as _json
    from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
    result = await handle_describe_tool({"tool_name": "health_check", "lite": False})
    description = _json.loads(result[0].text)["tool"]["description"]
    for field in ("agent_signature", "server_time", "_cache"):
        assert field in description, (
            "describe_tool(health_check) description text must document "
            f"{field} from the shared response wrapper"
        )


def test_get_server_info_is_registered():
    """get_server_info is cross-referenced from health_check's describe text.
    PR #433 left it register=False with the comment 're-enabled separately
    per #431'. This is that re-enable."""
    from src.mcp_handlers.decorators import list_registered_tools
    assert "get_server_info" in list_registered_tools(include_hidden=True), (
        "get_server_info must be registered; describe_tool advertises it as "
        "a related/alternative tool from health_check, get_connection_status, "
        "get_workspace_health, and the admin toolset banner. If unregistered, "
        "agents reading the docs call the name and hit 'Unknown tool'."
    )


def test_no_new_describe_cross_refs_to_unreachable_tools():
    """Names referenced in tool_descriptions.json (SEE ALSO / RELATED TOOLS /
    ALTERNATIVES blocks) must resolve to either a registered tool or a known
    alias. Known consolidated-but-unaliased cases are pinned below and tracked
    against #429 (tool-aliasing cleanup) — any NEW reference will fail this
    test, forcing a decision: register, alias, or remove the reference."""
    import json
    import pathlib
    import re

    from src.mcp_handlers.decorators import list_registered_tools
    from src.mcp_handlers.tool_stability import list_all_aliases

    descriptions = json.loads(
        pathlib.Path("src/tool_descriptions.json").read_text()
    )
    registered = set(list_registered_tools(include_hidden=True))
    aliased = set(list_all_aliases().keys())

    # Consolidated/internal tools advertised in describe text but not directly
    # callable. Each remains visible because its describe block teaches an
    # umbrella surface (e.g. observe/agent/knowledge) or is an internal helper
    # the operator surface still references. Track via #429 — when an alias is
    # added or the reference is removed, drop from this set.
    known_unreachable_refs = {
        "check_recovery_options",
        "quick_resume",
        "self_recovery_review",
    }

    # Tokens that look like tool names but aren't (markdown words, status
    # values, action names that live behind a consolidated tool).
    not_tools = {
        "status", "metrics", "checkin", "log", "update", "register", "init",
        "session", "hello", "authenticate", "login", "start", "state",
        "quick_start", "my_status", "check_status", "bind_identity",
        "recall_identity",
    }

    referenced = set()
    # Tool references in SEE ALSO / RELATED TOOLS / ALTERNATIVES blocks are
    # formatted as list items: `- tool_name:` or `- tool_name -` or `- tool_name()`.
    # Match only that shape — parameter mentions like `- agent_id (string):` are
    # also list items, so dedupe via the not_tools / param-shape filter below.
    block_re = re.compile(
        r"(?:SEE ALSO|RELATED TOOLS|ALTERNATIVES):\n(.*?)(?:\n\n|\Z)",
        re.S,
    )
    item_re = re.compile(r"^- ([a-z][a-z0-9_]{2,})\b", re.M)
    for tool_name, text in descriptions.items():
        for block in block_re.findall(text):
            for token in item_re.findall(block):
                referenced.add(token)

    plausible_tools = referenced - not_tools
    unresolved = plausible_tools - registered - aliased - known_unreachable_refs

    assert not unresolved, (
        f"describe_tool cross-references {sorted(unresolved)} but those are "
        f"neither registered nor aliased nor pinned in known_unreachable_refs. "
        f"Either register the tool, add an alias in tool_stability.py, remove "
        f"the cross-reference, or add it to known_unreachable_refs with a "
        f"comment tying it to #429."
    )
