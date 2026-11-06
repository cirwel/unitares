"""Finding aliases must advertise the inputs their pinned actions require."""

import copy
import json

import pytest
from pydantic import ValidationError

from src.alias_schema import build_alias_input_schema
from src.mcp_compat import FastMCP
from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
from src.mcp_handlers.schemas.knowledge import KnowledgeParams
from src.tool_registration import _register_common_aliases


@pytest.mark.parametrize("alias,field", [
    ("store_finding", "summary"),
    ("update_finding", "discovery_id"),
])
@pytest.mark.asyncio
async def test_required_inputs_agree_in_discovery_and_transport(alias, field):
    server = FastMCP(name="finding-required-inputs")
    _register_common_aliases(server)
    tool = server._tool_manager.get_tool(alias)
    assert tool is not None
    described = json.loads((await handle_describe_tool({"tool_name": alias}))[0].text)
    schema = described["tool"]["inputSchema"]
    assert schema["required"] == [field]
    assert tool.parameters["required"] == [field]
    assert "default" not in schema["properties"][field]

    # Real FastMCP argument validation, not just schema snapshots. Both omitted
    # and explicit null inputs previously passed and failed only in the handler.
    for arguments in ({}, {field: None}, {field: 123}):
        with pytest.raises(ValidationError):
            tool.fn_metadata.arg_model.model_validate(arguments)
    accepted = tool.fn_metadata.arg_model.model_validate({field: "probe"})
    assert accepted.model_dump_one_level()[field] == "probe"
    lite = json.loads((await handle_describe_tool({"tool_name": alias, "lite": True}))[0].text)
    assert f"{field} (required)" in lite["parameters"]


@pytest.mark.parametrize("alias,field", [
    ("store_finding", "summary"),
    ("update_finding", "discovery_id"),
])
def test_alias_requirement_preserves_router_and_property_metadata(alias, field):
    router = KnowledgeParams.model_json_schema()
    router["properties"][field]["anyOf"][0]["maxLength"] = 123
    original = copy.deepcopy(router)
    schema = build_alias_input_schema(alias, router, inject_action=True)
    assert router == original
    assert router["required"] == ["action"]
    prop = schema["properties"][field]
    assert prop["type"] == "string"
    assert prop["maxLength"] == 123
    assert "anyOf" not in prop
    assert "default" not in prop
    # Unrelated fields remain optional, and applying the builder again is safe.
    assert schema["required"] == [field]
    assert build_alias_input_schema(alias, schema, inject_action=True) == schema
