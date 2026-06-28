"""Gateway tool handlers — 6 tools that proxy to governance MCP."""

from __future__ import annotations

import json
import logging
from typing import Optional

from . import simplifiers
from .client import GovernanceMCPClient, MCPError, CircuitOpenError
from .constants import HELP_TEXT
from .query_engine import route_query

logger = logging.getLogger("gateway.tools")


def _error_envelope(exc: Exception) -> str:
    """Convert an exception to a JSON error envelope string."""
    if isinstance(exc, CircuitOpenError):
        envelope = simplifiers.err("Governance server temporarily unavailable", str(exc))
    elif isinstance(exc, ConnectionError):
        envelope = simplifiers.err("Cannot reach governance server", str(exc))
    elif isinstance(exc, MCPError):
        envelope = simplifiers.err("Governance error", str(exc))
    else:
        envelope = simplifiers.err("Unexpected error", str(exc))
    return json.dumps(envelope)


async def handle_status(client: GovernanceMCPClient, agent_id: Optional[str] = None) -> str:
    """Get agent EISV state, coherence, verdict, basin."""
    try:
        args: dict = {}
        if agent_id:
            args["agent_id"] = agent_id
        raw = await client.call_tool("get_governance_metrics", args)
        return json.dumps(simplifiers.simplify_status(raw))
    except Exception as exc:
        logger.warning("status failed: %s", exc)
        return _error_envelope(exc)


async def handle_checkin(
    client: GovernanceMCPClient,
    summary: str,
    complexity: float = 0.5,
    confidence: float = 0.7,
    agent_id: Optional[str] = None,
) -> str:
    """Report work and get a governance verdict."""
    try:
        args: dict = {
            "summary": summary,
            "complexity": complexity,
            "confidence": confidence,
        }
        if agent_id:
            args["agent_id"] = agent_id
        raw = await client.call_tool("process_agent_update", args)
        return json.dumps(simplifiers.simplify_checkin(raw))
    except Exception as exc:
        logger.warning("checkin failed: %s", exc)
        return _error_envelope(exc)


async def handle_search(
    client: GovernanceMCPClient,
    query: str,
    limit: int = 5,
    agent_id: Optional[str] = None,
) -> str:
    """Search the shared knowledge graph."""
    try:
        args: dict = {"action": "search", "query": query, "limit": limit}
        if agent_id:
            args["agent_id"] = agent_id
        raw = await client.call_tool("knowledge", args)
        return json.dumps(simplifiers.simplify_search(raw))
    except Exception as exc:
        logger.warning("search failed: %s", exc)
        return _error_envelope(exc)


async def handle_note(
    client: GovernanceMCPClient,
    content: str,
    tags: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> str:
    """Leave a note or discovery in the knowledge graph."""
    try:
        args: dict = {"content": content}
        if tags:
            args["tags"] = tags
        if agent_id:
            args["agent_id"] = agent_id
        raw = await client.call_tool("leave_note", args)
        return json.dumps(simplifiers.simplify_note(raw))
    except Exception as exc:
        logger.warning("note failed: %s", exc)
        return _error_envelope(exc)


async def handle_query(
    client: GovernanceMCPClient,
    question: str,
    agent_id: Optional[str] = None,
) -> str:
    """Natural language gateway — route question to the right tool."""
    try:
        route = await route_query(question, client)
        tool = route["tool"]
        args = route["args"]
        # Forward agent_id to whichever tool gets routed
        if agent_id:
            args["agent_id"] = agent_id

        if tool == "status":
            return await handle_status(client, **args)
        elif tool == "checkin":
            return await handle_checkin(client, **args)
        elif tool == "search":
            return await handle_search(client, **args)
        elif tool == "note":
            return await handle_note(client, **args)
        elif tool == "help":
            return handle_help()
        else:
            return await handle_search(client, query=question, agent_id=agent_id)
    except Exception as exc:
        logger.warning("query routing failed: %s", exc)
        return _error_envelope(exc)


def handle_help() -> str:
    """List all gateway tools with examples."""
    return json.dumps(simplifiers.ok("UNITARES Gateway — 6 tools available", HELP_TEXT))
