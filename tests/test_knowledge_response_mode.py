"""Opt-in identity-envelope shaping for knowledge read actions (#1700)."""

import json
from unittest.mock import AsyncMock

import pytest
from mcp.types import TextContent

from src.mcp_handlers.knowledge import handlers
from src.mcp_handlers.knowledge.handlers import _knowledge_read_response_mode


def _text_payload(payload):
    return [TextContent(type="text", text=json.dumps(payload))]


def _full_identity_payload():
    assurance = {
        "tier": "strong",
        "caller_proven": True,
        "proof_origin": "caller_asserted",
        "session_source": "continuity_token",
        "reason": "repeated prose",
    }
    identity_context = {"schema": "s22.identity_response.v1"}
    signature = {
        "uuid": "11111111-2222-4333-8444-555555555555",
        "agent_id": "Agent_X",
        "display_name": "resident_x",
        "identity_context": identity_context,
        "identity_assurance": assurance,
    }
    return {
        "success": True,
        "identity_context": identity_context,
        "identity_assurance": assurance,
        "agent_signature": signature,
        "agent": dict(signature),
        "discoveries": [
            {"id": "d1", "provenance": {"identity_context": "keep-me"}}
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["compact", "lean"])
async def test_read_modes_compact_only_caller_identity(mode):
    handler = AsyncMock(return_value=_text_payload(_full_identity_payload()))
    wrapped = _knowledge_read_response_mode(handler)

    result = await wrapped({"response_mode": mode})
    payload = json.loads(result[0].text)

    compact_assurance = {"tier": "strong", "caller_proven": True}
    assert payload["agent_signature"] == {
        "uuid": "11111111-2222-4333-8444-555555555555",
        "identity_assurance": compact_assurance,
    }
    assert "identity_context" not in payload
    assert payload["identity_assurance"] == compact_assurance
    assert "identity_context" not in payload["agent"]
    assert payload["agent"]["identity_assurance"] == compact_assurance
    assert payload["agent"]["display_name"] == "resident_x"
    assert payload["discoveries"][0]["provenance"] == {
        "identity_context": "keep-me"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "full"])
async def test_default_and_full_modes_preserve_original_response(mode):
    original = _text_payload(_full_identity_payload())
    handler = AsyncMock(return_value=original)
    wrapped = _knowledge_read_response_mode(handler)
    arguments = {} if mode is None else {"response_mode": mode}

    result = await wrapped(arguments)

    assert result is original
    payload = json.loads(result[0].text)
    assert payload["agent_signature"]["identity_context"]["schema"] == (
        "s22.identity_response.v1"
    )
    assert payload["agent_signature"]["identity_assurance"]["reason"] == (
        "repeated prose"
    )


@pytest.mark.asyncio
async def test_public_compact_mode_does_not_change_undecorated_write_response():
    original = _text_payload(_full_identity_payload())
    write_handler = AsyncMock(return_value=original)

    result = await write_handler({"response_mode": "compact"})

    payload = json.loads(result[0].text)
    assert payload["agent_signature"]["identity_context"]["schema"] == (
        "s22.identity_response.v1"
    )
    assert payload["agent"]["identity_assurance"]["reason"] == "repeated prose"


def test_exact_initial_read_actions_are_mode_enabled():
    enabled = (
        handlers.handle_search_knowledge_graph,
        handlers.handle_get_knowledge_graph,
        handlers.handle_get_discovery_details,
        handlers.handle_get_lifecycle_stats,
    )
    for handler in enabled:
        assert getattr(handler, "_lean_knowledge_read_response", False)

    assert not getattr(
        handlers.handle_store_knowledge_graph,
        "_lean_knowledge_read_response",
        False,
    )
    assert not getattr(
        handlers.handle_update_discovery_status_graph,
        "_lean_knowledge_read_response",
        False,
    )
    assert not getattr(
        handlers.handle_list_knowledge_graph,
        "_lean_knowledge_read_response",
        False,
    )
