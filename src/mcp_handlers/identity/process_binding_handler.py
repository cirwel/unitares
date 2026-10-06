"""MCP tool: list_process_bindings — surface live execution-context bindings.

Operator-facing diagnostic for issue #123. `/diagnose` (Codex slash command at
commands/diagnose.md) can call this to show the caller what contexts the
server sees bound to an agent UUID. Emits a concurrent-binding warning in the
response when ≥2 distinct live contexts are present on an agent whose
`allow_concurrent_contexts` flag is false.

Read-only, no DB writes. Uses the existing `get_live_bindings` helper.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Sequence

from mcp.types import TextContent

from ..decorators import mcp_tool
from ..utils import error_response, success_response
from .process_binding import get_live_bindings
from .shared import get_bound_agent_id

logger = logging.getLogger(__name__)


@mcp_tool("list_process_bindings", timeout=10.0)
async def handle_list_process_bindings(
    arguments: Dict[str, Any],
) -> Sequence[TextContent]:
    """List live execution-context bindings for an agent (#123 v1 diagnose).

    Arguments:
        agent_uuid: optional — defaults to the caller's bound agent.

    Returns a payload like:
        {
          "agent_uuid": "...",
          "live_binding_count": 2,
          "concurrent_binding_detected": true,
          "bindings": [ {host_id, pid, pid_start_time, transport, tty, ...}, ... ]
        }
    """
    arguments = arguments or {}

    agent_uuid = arguments.get("agent_uuid")
    if not agent_uuid:
        agent_uuid = get_bound_agent_id(
            session_id=arguments.get("client_session_id"),
            arguments=arguments,
        )
    if not agent_uuid:
        return error_response(
            "list_process_bindings requires agent_uuid or a bound session."
        )

    # The stored client_session_id is a bearer credential, and any caller can
    # name any agent here, so rows carry a reference to it instead.
    from .session import session_id_reference

    bindings = [
        {
            **{k: v for k, v in b.items() if k != "client_session_id"},
            "client_session_ref": session_id_reference(b.get("client_session_id")),
        }
        for b in await get_live_bindings(agent_uuid)
    ]

    distinct_contexts = {
        (b["host_id"], b["pid"], b["pid_start_time"], b["transport"])
        for b in bindings
    }
    concurrent = len(distinct_contexts) >= 2

    payload: Dict[str, Any] = {
        "agent_uuid": agent_uuid,
        "live_binding_count": len(bindings),
        "concurrent_binding_detected": concurrent,
        "bindings": bindings,
    }
    if concurrent:
        payload["note"] = (
            "Multiple live execution contexts for this agent. If the agent's "
            "allow_concurrent_contexts flag is false (the default), this is a "
            "concurrent identity binding violation — see issue #123."
        )

    return success_response(payload, agent_id=agent_uuid, arguments=arguments)
