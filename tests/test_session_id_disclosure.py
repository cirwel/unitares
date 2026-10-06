"""Diagnostics never hand out another session's client_session_id.

A client_session_id is a bearer credential: whoever presents it acts as the
session it names. ``list_process_bindings`` takes any ``agent_uuid`` and
returned the session id each live process onboarded with, and
``admin(action='debug_context')``, open to any bound caller, listed raw keys
from the in-memory session cache and the 12-character uuid prefixes that are
the legacy session id itself. Both now show references.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_handlers.identity.session import session_id_reference

VICTIM_UUID = "5e728ecb-1234-4abc-8def-0123456789ab"
VICTIM_CSID = "agent-5e728ecb1234"
OTHER_CSID = "claude-host-7f3a9c2e41b8d6"


def test_a_reference_cannot_be_presented_as_the_id():
    ref = session_id_reference(OTHER_CSID)

    assert ref != OTHER_CSID and OTHER_CSID not in ref
    assert ref == session_id_reference(OTHER_CSID)  # stable, so rows can be matched
    assert ref != session_id_reference(OTHER_CSID + "x")
    assert session_id_reference(None) is None and session_id_reference("") is None


@pytest.mark.asyncio
async def test_list_process_bindings_shows_references_not_session_ids():
    from src.mcp_handlers.identity import process_binding_handler

    rows = [
        {"host_id": "h1", "pid": 100, "pid_start_time": 1.0, "transport": "http",
         "tty": None, "ppid": None, "anchor_path_hash": None,
         "client_session_id": VICTIM_CSID, "onboard_ts": None, "last_seen": None},
        {"host_id": "h2", "pid": 200, "pid_start_time": 2.0, "transport": "stdio",
         "tty": None, "ppid": None, "anchor_path_hash": None,
         "client_session_id": OTHER_CSID, "onboard_ts": None, "last_seen": None},
    ]
    with patch.object(process_binding_handler, "get_live_bindings", AsyncMock(return_value=rows)):
        result = await process_binding_handler.handle_list_process_bindings(
            {"agent_uuid": VICTIM_UUID}
        )

    text = result[0].text
    assert VICTIM_CSID not in text and OTHER_CSID not in text
    payload = json.loads(text)
    assert [b["client_session_ref"] for b in payload["bindings"]] == [
        session_id_reference(VICTIM_CSID), session_id_reference(OTHER_CSID),
    ]
    assert all("client_session_id" not in b for b in payload["bindings"])
    assert payload["live_binding_count"] == 2


@pytest.mark.asyncio
async def test_debug_context_shows_references_and_short_prefixes():
    session_identities = {
        VICTIM_CSID: {"bound_agent_id": VICTIM_UUID},
        OTHER_CSID: {"bound_agent_id": None},
    }
    prefix_index = {VICTIM_UUID[:12]: VICTIM_UUID}
    server = MagicMock()
    server.agent_metadata = {}

    with patch("src.mcp_handlers.admin.handlers.mcp_server", server), \
         patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
         patch("src.mcp_handlers.context.get_context_session_key", return_value=None), \
         patch("src.mcp_handlers.TOOL_HANDLERS", {}), \
         patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="mine"), \
         patch("src.mcp_handlers.identity.shared._session_identities", session_identities), \
         patch("src.mcp_handlers.identity.shared._uuid_prefix_index", prefix_index):
        from src.mcp_handlers.admin.handlers import handle_debug_request_context
        result = await handle_debug_request_context({})

    text = result[0].text
    assert VICTIM_CSID not in text and OTHER_CSID not in text
    assert VICTIM_UUID[:12] not in text  # the legacy session id's suffix
    diagnostics = json.loads(text)["diagnostics"]
    assert set(diagnostics["legacy_bindings_in_memory"]) == {
        session_id_reference(VICTIM_CSID), session_id_reference(OTHER_CSID),
    }
    assert diagnostics["legacy_bindings_count"] == 2
