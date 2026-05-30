"""HTTP tool execution helpers.

Provides a narrow direct-call path for core tools whose handlers already accept
plain argument dicts. Everything else falls back to the MCP dispatch pipeline.
"""

from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from src.mcp_handlers.identity.handlers import (
    handle_identity_adapter,
    handle_onboard_v2,
)
from src.mcp_handlers.core import handle_process_agent_update
from src.mcp_handlers.utils import require_agent_id
from src.services.http_dispatch_fallback import execute_http_dispatch_fallback
from src.services.runtime_queries import get_governance_metrics_data, get_health_check_data
from src.services.tool_usage_recorder import classify_tool_result, record_tool_usage

ToolHandler = Callable[[Dict[str, Any]], Awaitable[Any]]


def _normalize_direct_http_result(result: Any) -> Any:
    """Convert direct-handler MCP text output into plain data for HTTP callers."""
    if isinstance(result, (list, tuple)) and len(result) == 1 and hasattr(result[0], "text"):
        try:
            return json.loads(result[0].text)
        except (json.JSONDecodeError, TypeError):
            return result
    return result

async def _execute_http_get_governance_metrics(arguments: Dict[str, Any]) -> Any:
    agent_id, error = require_agent_id(arguments)
    if error:
        return [error]
    return await get_governance_metrics_data(agent_id, arguments)


async def _execute_http_health_check(arguments: Dict[str, Any]) -> Any:
    return await get_health_check_data(arguments)


_DIRECT_HTTP_TOOL_HANDLERS: Dict[str, ToolHandler] = {
    "get_governance_metrics": _execute_http_get_governance_metrics,
    "health_check": _execute_http_health_check,
    "identity": handle_identity_adapter,
    "onboard": handle_onboard_v2,
    "process_agent_update": handle_process_agent_update,
}


def get_direct_http_tool_handler(tool_name: str) -> Optional[ToolHandler]:
    """Return a direct handler for HTTP-safe core tools, if any."""
    return _DIRECT_HTTP_TOOL_HANDLERS.get(tool_name)


async def execute_http_tool(tool_name: str, arguments: Dict[str, Any]) -> Any:
    """Execute a tool for the HTTP API.

    Core governance tools use direct handlers so HTTP does not always depend on
    the full MCP dispatch path. All other tools use an HTTP-specific fallback
    that skips identity-resolution middleware because HTTP already set context.

    Records tool_usage telemetry (JSONL + audit.tool_usage) at every exit point.
    """
    agent_id = arguments.get("agent_id") if isinstance(arguments, dict) else None
    t0 = time.monotonic()
    try:
        handler = get_direct_http_tool_handler(tool_name)
        if handler is not None:
            result = await handler(arguments)
            latency_ms = int((time.monotonic() - t0) * 1000)
            success, error_type = classify_tool_result(result)
            record_tool_usage(tool_name=tool_name, agent_id=agent_id,
                              success=success, error_type=error_type, latency_ms=latency_ms)
            return _normalize_direct_http_result(result)
        result = await execute_http_dispatch_fallback(tool_name, arguments)
        latency_ms = int((time.monotonic() - t0) * 1000)
        success, error_type = classify_tool_result(result)
        record_tool_usage(tool_name=tool_name, agent_id=agent_id,
                          success=success, error_type=error_type, latency_ms=latency_ms)
        return result
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        record_tool_usage(tool_name=tool_name, agent_id=agent_id,
                          success=False, error_type=type(e).__name__,
                          latency_ms=latency_ms)
        raise
