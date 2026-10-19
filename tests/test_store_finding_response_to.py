"""store_finding accepts response_to: a linked record in one call.

2026-10-16 AX dogfood read (docs/evaluations/ax-dogfood/read-2026-10-16.md),
item 5. Before this, a linked finding needed describe_tool(knowledge) plus
use_tool(tool_name='knowledge', arguments={'action': 'store', ...}).
"""

import re

import pytest

from src.alias_schema import ALIAS_SCHEMA_KEEP
from src.knowledge_graph import VALID_RESPONSE_TYPES
from src.mcp_handlers.middleware.envelope_step import build_experience_envelope

pytestmark = pytest.mark.usefixtures("first_party_tool_surface")

LINK = {"discovery_id": "d-parent", "response_type": "extend"}


def _advertised_store_finding():
    from scripts.diagnostics import tool_surface_cost as cost

    for definition in cost._definitions("progressive", "catalog"):
        data = definition if isinstance(definition, dict) else definition.model_dump(
            by_alias=True, exclude_none=True
        )
        if data.get("name") == "store_finding":
            return data
    pytest.fail("store_finding is not on the progressive surface")


def test_store_finding_keeps_response_to():
    assert "response_to" in ALIAS_SCHEMA_KEEP["store_finding"]


def test_advertised_response_to_lists_every_response_type():
    prop = _advertised_store_finding()["inputSchema"]["properties"]["response_to"]
    listed = set(re.findall(r"[a-z_]+", prop["description"].split("type:", 1)[1]))
    assert listed == set(VALID_RESPONSE_TYPES)


@pytest.mark.asyncio
async def test_response_to_reaches_the_store_handler():
    from tests.test_write_ack_envelope import _store_payload, _through_real_steps

    name, arguments, _ = await _through_real_steps(
        "store_finding",
        {"summary": "linked note", "response_to": dict(LINK)},
        _store_payload(),
    )
    assert name == "knowledge"
    assert arguments["action"] == "store"
    assert arguments["response_to"] == LINK


def test_store_ack_confirms_the_recorded_link():
    payload = {
        "success": True,
        "message": "Discovery stored",
        "discovery_id": "d-new",
        "discovery": {
            "id": "d-new", "type": "note", "status": "open", "summary": "linked note",
            "response_to": dict(LINK),
        },
    }
    env = build_experience_envelope(
        "store_finding", "knowledge", payload,
        {"summary": "linked note", "response_to": dict(LINK)},
    )
    assert env["state_summary"]["response_to"] == LINK


def test_unlinked_store_ack_has_no_response_to():
    payload = {
        "success": True,
        "message": "Discovery stored",
        "discovery_id": "d-new",
        "discovery": {"id": "d-new", "type": "note", "status": "open", "summary": "s"},
    }
    env = build_experience_envelope("store_finding", "knowledge", payload, {"summary": "s"})
    assert "response_to" not in env["state_summary"]
