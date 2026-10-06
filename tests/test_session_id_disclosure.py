"""Diagnostics never hand out another session's client_session_id.

A client_session_id is a bearer credential: whoever presents it acts as the
session it names. ``list_process_bindings`` takes any ``agent_uuid`` and
returned the session id each live process onboarded with, and
``admin(action='debug_context')``, open to any bound caller, listed raw keys
from the in-memory session cache and the 12-character uuid prefixes that are
the legacy session id itself. Both now show opaque references, keyed per
process so they are no oracle for guessing ids, and the prefix index only by
its size.
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
    assert ref == session_id_reference(OTHER_CSID)  # stable within the process
    assert ref != session_id_reference(OTHER_CSID + "x")
    assert session_id_reference(None) is None and session_id_reference("") is None


_FIXED_KEY = b"test-session-reference-key-0001!"


# Hex-only ids, so a reference (hex itself) could contain them if it leaked
# any part. The key is pinned so the expected reference is exact and the run
# deterministic; under a random key an HMAC can contain a 6-character window
# of its input by coincidence.
@pytest.mark.parametrize("csid", ["c0ffee42", "5e728ecb1234", "0123456789abcdef", "deadbeefcafe0042"])
def test_a_reference_reveals_no_part_of_the_id(csid, monkeypatch):
    import hashlib
    import hmac

    from src.mcp_handlers.identity import session

    monkeypatch.setattr(session, "_SESSION_REFERENCE_KEY", _FIXED_KEY)
    ref = session_id_reference(csid)

    expected = hmac.new(_FIXED_KEY, csid.encode(), hashlib.sha256).hexdigest()[:16]
    assert ref == "ref-" + expected
    windows = {csid[i:i + 6] for i in range(len(csid) - 5)}
    assert not any(w in ref for w in windows), (csid, ref)


def test_a_reference_is_keyed_so_it_is_no_offline_oracle(monkeypatch):
    # Without the process key, hashing a guessed id does not reproduce it.
    import hashlib

    from src.mcp_handlers.identity import session

    ref = session_id_reference(VICTIM_CSID)
    assert ref[4:] != hashlib.sha256(VICTIM_CSID.encode()).hexdigest()[:16]
    monkeypatch.setattr(session, "_SESSION_REFERENCE_KEY", b"another process")
    assert session_id_reference(VICTIM_CSID) != ref


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
async def test_debug_context_shows_references_and_a_prefix_count():
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
    assert VICTIM_CSID[6:] not in text  # the legacy session id's uuid part
    diagnostics = json.loads(text)["diagnostics"]
    assert diagnostics["legacy_uuid_prefix_index"] == {"count": 1}
    assert set(diagnostics["legacy_bindings_in_memory"]) == {
        session_id_reference(VICTIM_CSID), session_id_reference(OTHER_CSID),
    }
    assert diagnostics["legacy_bindings_count"] == 2
