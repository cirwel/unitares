"""A start_session response built by the real producers.

The routine-mint budget test used to feed the envelope a hand-written
anonymous mint at thread position 1, the one class that fit its budget. The
common cases (a fresh uuid on a shared thread, a named mint) kept returning
the whole onboard record a second time under raw_governance, 4.4-5.2 KB, while
the test stayed green.

`mint()` runs the real onboard handler (``handle_onboard_v2``) with its I/O
mocked, on the arguments dispatch hands it for a start_session call (the
alias resolved and the parameters validated, which fills response_mode's
default), so the payload is whatever the handler builds: the thread context
from ``build_fork_context``, the lineage state from the R2 path, the resident
verdict from ``resident_registration`` and a signed continuity_token. Only
storage is faked: the thread position and its earlier nodes, the recorded
onboard provenance, and the tag write. `start_session()` then passes it
through the real alias, validation and envelope steps and returns the wire
text. It runs only under pytest: tests/conftest.py isolates ``src.db.get_db``
and the audit writers, which some onboard paths reach directly, and outside
it they write to the configured governance database.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

from mcp.types import TextContent

THREAD_ID = "t-f3a8bb1ebde032c8"
# A synthetic earlier node on the same thread.
EARLIER_UUID = "0b1c2d3e-4f50-4a61-8b72-9c8d7e6f5a4b"
EARLIER_NODE = {
    "agent_id": EARLIER_UUID,
    "thread_position": 1,
    "label": "claude_code-opus_0b1c2d3e",
}


def _db(position: int, nodes: list, lineage_row: Optional[dict]) -> AsyncMock:
    db = AsyncMock()
    # What the creation writers recorded for an unmarked call, which is what
    # the response reads back as onboard_origin / onboard_origin_basis.
    identity = SimpleNamespace(
        identity_id="identity-1",
        metadata={
            "onboard_origin": "agent",
            "onboard_origin_basis": "default_unmarked_call",
        },
    )
    db.get_session = AsyncMock(return_value=None)
    db.get_identity = AsyncMock(return_value=identity)
    db.get_agent = AsyncMock(return_value=None)
    db.get_agent_label = AsyncMock(return_value=None)
    db.find_agent_by_label = AsyncMock(return_value=None)
    db.update_agent_fields = AsyncMock(return_value=True)
    db.get_agent_thread_info = AsyncMock(return_value={
        "thread_id": THREAD_ID,
        "thread_position": position,
        "parent_agent_id": None,
    })
    db.create_or_get_thread = AsyncMock(
        return_value={"thread_id": THREAD_ID, "created": position == 1}
    )
    db.claim_thread_position = AsyncMock(return_value=position)
    db.get_thread_nodes = AsyncMock(return_value=nodes)
    db.get_active_sessions_for_identity = AsyncMock(return_value=[])
    db.get_last_inactive_session = AsyncMock(return_value=None)
    db.get_latest_agent_state = AsyncMock(return_value=None)
    db.get_agent_state_history = AsyncMock(return_value=[])
    db.kg_query = AsyncMock(return_value=[])
    db.read_lineage_state = AsyncMock(return_value=lineage_row)
    return db


async def _dispatched(arguments: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Any]:
    """A start_session call through the real alias and validation steps, as
    dispatch runs them before the handler."""
    from src.mcp_handlers.middleware import DispatchContext
    from src.mcp_handlers.middleware.params_step import resolve_alias, validate_params

    ctx = DispatchContext()
    name, resolved, ctx = await resolve_alias("start_session", dict(arguments), ctx)
    return await validate_params(name, resolved, ctx)


async def mint(
    arguments: Dict[str, Any],
    *,
    position: int = 1,
    lineage_row: Optional[dict] = None,
) -> Dict[str, Any]:
    """The payload the real onboard handler returns for ``arguments``.

    ``position`` > 1 puts EARLIER_NODE on the thread before this mint.
    ``lineage_row`` is what storage reports for the new row's lineage
    (``read_lineage_state``) after a declaration.
    """
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError(
            "onboard_producer.mint must run under pytest: tests/conftest.py "
            "isolates the database and the audit writers the handler reaches"
        )
    from src.mcp_handlers.identity import handlers

    _name, resolved, _ctx = await _dispatched(arguments)
    nodes = [EARLIER_NODE] if position > 1 else []
    nodes = nodes + [{"agent_id": "self", "thread_position": position, "label": None}]
    db = _db(position, nodes, lineage_row)
    session_cache = AsyncMock()
    session_cache.get = AsyncMock(return_value=None)
    raw_redis = AsyncMock()
    raw_redis.get = AsyncMock(return_value=None)

    async def _get_raw_redis():
        return raw_redis

    def _discard_task(coro, **_kwargs):
        coro.close()
        task = MagicMock()
        task.cancel = MagicMock()
        return task

    server = MagicMock()
    server.agent_metadata = {}
    with patch.dict(os.environ, {"UNITARES_CONTINUITY_TOKEN_SECRET": "t" * 32}), \
         patch("src.mcp_handlers.identity.persistence._redis_cache", None), \
         patch("src.cache.get_session_cache", return_value=session_cache), \
         patch("src.mcp_handlers.identity.handlers.get_db", return_value=db), \
         patch("src.mcp_handlers.identity.resolution.get_db", return_value=db), \
         patch("src.mcp_handlers.identity.persistence.get_db", return_value=db), \
         patch("src.cache.redis_client.get_redis", new=_get_raw_redis), \
         patch("src.cache.metadata_cache.get_redis", new=_get_raw_redis), \
         patch("src.mcp_handlers.context.get_mcp_session_id", return_value=None), \
         patch("src.mcp_handlers.context.get_context_session_key",
               return_value="203.0.113.7:ua-0f3e"), \
         patch("src.mcp_handlers.context.get_context_agent_id", return_value=None), \
         patch("src.mcp_handlers.context.get_context_client_hint",
               return_value="claude_code"), \
         patch("src.mcp_handlers.context.update_context_agent_id"), \
         patch("asyncio.create_task", side_effect=_discard_task), \
         patch("src.mcp_handlers.shared.get_mcp_server", return_value=server), \
         patch("src.mcp_handlers.identity.shared._register_uuid_prefix"), \
         patch("src.agent_storage.update_agent", AsyncMock(return_value=True)):
        result = await handlers.handle_onboard_v2(dict(resolved))
    return json.loads(result[0].text)


async def start_session(
    arguments: Dict[str, Any], payload: Dict[str, Any]
) -> Tuple[Dict[str, Any], int]:
    """The start_session envelope for ``payload`` and its wire size in bytes,
    through the real alias, validation and envelope steps."""
    from src.mcp_handlers.middleware.envelope_step import apply_experience_envelope

    name, resolved, ctx = await _dispatched(arguments)
    out = await apply_experience_envelope(
        name, dict(resolved), ctx,
        [TextContent(type="text", text=json.dumps(payload))],
    )
    text = out[0].text
    return json.loads(text), len(text.encode("utf-8"))
