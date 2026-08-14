"""REST tool surface: /v1/tools listing and /v1/tools/call execution.

Split out of src/http_api.py (see that module for route registration).
"""

from __future__ import annotations

import json
import os
from typing import Any

from starlette.responses import JSONResponse


from src.logging_utils import get_logger
from src.services.http_tool_service import execute_http_tool
from src.services.http_request_parser import (
    HttpToolRequestError,
    deprecated_http_tool_payload,
    enrich_http_client_context,
    normalize_http_tool_name,
    parse_http_tool_request,
    validate_http_tool_content_length,
)
from src.mcp_compat import get_tool_input_schema

from src.http_routes import access

logger = get_logger(__name__)


def _serialize_mcp_content_item(item):
    """Convert MCP content items into JSON-serializable dicts."""
    if hasattr(item, "model_dump"):
        # by_alias keeps the MCP wire format (camelCase, e.g. `mimeType`). mcp
        # 1.x named the fields that way; 2.x renamed them to snake_case with the
        # camelCase kept as a serialization alias, so without by_alias the REST
        # surface would silently start emitting `mime_type`.
        return item.model_dump(exclude_none=True, by_alias=True)
    if isinstance(item, dict):
        return item
    if hasattr(item, "__dict__"):
        return {k: v for k, v in vars(item).items() if v is not None}
    return {"type": "unknown", "value": str(item)}


def _build_http_tool_response(tool_name: str, result) -> dict:
    """Normalize MCP handler output into the HTTP API response contract."""
    if result is None:
        return {
            "name": tool_name,
            "result": None,
            "success": False,
            "error": f"Tool '{tool_name}' returned no result"
        }

    if isinstance(result, (list, tuple)):
        if len(result) == 0:
            return {
                "name": tool_name,
                "result": None,
                "success": False,
                "error": f"Tool '{tool_name}' returned empty result"
            }

        if len(result) == 1 and hasattr(result[0], "text"):
            try:
                parsed = json.loads(result[0].text)
                return {"name": tool_name, "result": parsed, "success": True}
            except json.JSONDecodeError:
                text_result = result[0].text if result[0].text else "{}"
                return {"name": tool_name, "result": text_result, "success": True}

        return {
            "name": tool_name,
            "result": {"content": [_serialize_mcp_content_item(item) for item in result]},
            "success": True,
        }

    if isinstance(result, dict):
        return {"name": tool_name, "result": result, "success": True}

    result_str = str(result) if result else "null"
    return {"name": tool_name, "result": result_str, "success": True}


def _normalize_http_tool_name(body: dict, mcp_server_name: str) -> str:
    """Compatibility wrapper for callers importing the former local helper."""
    return normalize_http_tool_name(body, mcp_server_name)


# ---------------------------------------------------------------------------
# Endpoint handlers
# ---------------------------------------------------------------------------

async def http_list_tools(request):
    """List all tools in OpenAI-compatible format

    Query params:
        mode: Tool mode filter - "minimal", "lite", "full" (default from GOVERNANCE_TOOL_MODE env)
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    try:
        if not access._check_http_auth(request, http_api_token=http_api_token):
            return access._http_unauthorized()
        from src.tool_schemas import get_tool_definitions
        from src.tool_modes import TOOL_MODE, should_include_tool

        # Get mode from query param or env default
        query_mode = request.query_params.get("mode", TOOL_MODE)

        # get_tool_definitions() is synchronous, no await needed
        mcp_tools = get_tool_definitions()

        # Filter tools by mode
        filtered_tools = [t for t in mcp_tools if should_include_tool(t.name, mode=query_mode)]

        openai_tools = []
        for tool in filtered_tools:
            description = tool.description.split("\n")[0] if tool.description else f"Tool: {tool.name}"
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": description,
                    "parameters": get_tool_input_schema(tool)
                }
            })
        return JSONResponse({
            "tools": openai_tools,
            "count": len(openai_tools),
            "mode": query_mode,
            "total_available": len(mcp_tools),
            "note": f"Showing {len(filtered_tools)}/{len(mcp_tools)} tools in '{query_mode}' mode. Use ?mode=full for all."
        })
    except Exception as e:
        logger.error(f"Error listing tools: {e}", exc_info=True)
        return JSONResponse({
            "tools": [],
            "count": 0,
            "error": str(e)
        }, status_code=500)


async def _inject_http_client_session(request, arguments: dict) -> str | None:
    from src.mcp_handlers.context import set_csid_transport_injected

    set_csid_transport_injected(False)
    if "client_session_id" in arguments:
        return arguments.get("client_session_id")

    client_session_id = await access._extract_client_session_id(request)
    arguments["client_session_id"] = client_session_id
    set_csid_transport_injected(True)
    return client_session_id


async def _execute_http_tool_in_context(
    request,
    tool_name: str,
    arguments: dict,
    client_session_id: str | None,
):
    from src.mcp_handlers.context import (
        reset_session_context,
        reset_session_signals,
        set_session_context,
        set_session_signals,
    )

    signals = access._build_http_session_signals(request)
    signals_token = set_session_signals(signals)
    # Start the identity context unbound. ``X-Agent-Id`` remains available in
    # SessionSignals as a compatibility/recovery hint, but it is caller input —
    # never a binding. Only access._resolve_http_bound_agent may stamp the context via
    # update_context_agent_id after it verifies a supported proof path (#1431).
    context_token = set_session_context(
        session_key=client_session_id,
        client_session_id=client_session_id,
    )
    try:
        await access._resolve_http_bound_agent(tool_name, arguments, signals)
        return await execute_http_tool(tool_name, arguments)
    finally:
        reset_session_context(context_token)
        reset_session_signals(signals_token)


def _http_tool_error_response(exc: Exception, body: Any) -> JSONResponse:
    if isinstance(exc, HttpToolRequestError):
        return JSONResponse(exc.payload, status_code=exc.status_code)
    if isinstance(exc, json.JSONDecodeError):
        logger.error("Invalid JSON in request: %s", exc, exc_info=True)
        return JSONResponse(
            {
                "success": False,
                "error": "Invalid JSON format",
                "error_type": "JSONDecodeError",
            },
            status_code=400,
        )
    if isinstance(exc, ValueError):
        logger.warning("Validation error: %s", exc)
        return JSONResponse(
            {
                "success": False,
                "error": str(exc),
                "error_type": "ValidationError",
            },
            status_code=400,
        )
    if isinstance(exc, KeyError):
        logger.warning("Missing required field: %s", exc)
        return JSONResponse(
            {
                "success": False,
                "error": f"Missing required field: {exc}",
                "error_type": "KeyError",
            },
            status_code=400,
        )

    tool_name = body.get("name", "unknown") if isinstance(body, dict) else "unknown"
    logger.error("Error calling tool '%s': %s", tool_name, exc, exc_info=True)
    error_message = "An error occurred processing your request"
    if isinstance(exc, (AttributeError, TypeError)):
        error_message = "Invalid request format"
    elif isinstance(exc, RuntimeError):
        error_message = "Service temporarily unavailable"
    return JSONResponse(
        {
            "name": tool_name if isinstance(tool_name, str) else None,
            "result": None,
            "success": False,
            "error": error_message,
            "error_type": type(exc).__name__,
        },
        status_code=500,
    )


async def http_call_tool(request):
    """Execute one canonical MCP tool through the REST transport boundary."""
    body = None
    try:
        if not access._check_http_auth(
            request,
            http_api_token=os.getenv("UNITARES_HTTP_API_TOKEN"),
        ):
            return access._http_unauthorized()

        validate_http_tool_content_length(
            request.headers.get("content-length")
        )
        body = await request.json()
        parsed = parse_http_tool_request(
            body,
            mcp_server_name=request.state._http_api_mcp_server_name,
        )
        deprecated = deprecated_http_tool_payload(parsed.tool_name)
        if deprecated:
            return JSONResponse(deprecated)

        detected_client, detected_model = enrich_http_client_context(
            request.headers,
            parsed.arguments,
        )
        if detected_client:
            logger.debug(
                "[HTTP] Auto-detected client_hint=%s from UA", detected_client
            )
        if detected_model:
            logger.debug(
                "[HTTP] Auto-detected model_type=%s from headers", detected_model
            )

        client_session_id = await _inject_http_client_session(
            request, parsed.arguments
        )
        result = await _execute_http_tool_in_context(
            request,
            parsed.tool_name,
            parsed.arguments,
            client_session_id,
        )
        return JSONResponse(
            _build_http_tool_response(parsed.tool_name, result)
        )
    except Exception as exc:
        return _http_tool_error_response(exc, body)
