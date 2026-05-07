"""
HTTP REST API endpoints for non-MCP clients (Llama, Mistral, GPT, dashboards, etc.).

Extracted from mcp_server.py to keep the server entry point focused on MCP transport.

Usage:
    from src.http_api import register_http_routes
    register_http_routes(app, ...)
"""

from __future__ import annotations

import ipaddress as _ipaddress
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional

_startup_ts = time.time()

from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute

from prometheus_client import REGISTRY, generate_latest, CONTENT_TYPE_LATEST

from src.logging_utils import get_logger
from src.metrics_registry import (
    AGENTS_TOTAL,
    DIALECTIC_SESSIONS_ACTIVE,
    KNOWLEDGE_NODES_TOTAL,
    SERVER_INFO,
    SERVER_UPTIME,
)
from src.connection_tracker import CONNECTIONS_ACTIVE
from src.broadcaster import broadcaster_instance
from src.services.http_tool_service import execute_http_tool

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.websockets import WebSocket
    from src.connection_tracker import ConnectionTracker

logger = get_logger(__name__)


def _build_http_session_signals(request):
    """Build SessionSignals from an HTTP request."""
    from src.mcp_handlers.context import SessionSignals

    ua = request.headers.get("user-agent", "")
    x_session_id = request.headers.get("X-Session-ID") or request.headers.get("x-session-id")

    ip_ua_fp = None
    try:
        host = request.client.host if request.client else "unknown"
        import hashlib
        ua_fp = hashlib.md5(ua.encode()).hexdigest()[:6] if ua else "000000"
        ip_ua_fp = f"{host}:{ua_fp}"
    except Exception:
        pass

    return SessionSignals(
        x_session_id=x_session_id,
        x_client_id=request.headers.get("x-client-id") or request.headers.get("x-mcp-client-id"),
        ip_ua_fingerprint=ip_ua_fp,
        user_agent=ua,
        x_agent_name=request.headers.get("x-agent-name"),
        x_agent_id=request.headers.get("x-agent-id"),
        transport="rest",
        unitares_operator_token=request.headers.get("x-unitares-operator"),
    )


def _serialize_mcp_content_item(item):
    """Convert MCP content items into JSON-serializable dicts."""
    if hasattr(item, "model_dump"):
        return item.model_dump(exclude_none=True)
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
    """Resolve HTTP tool aliases to the canonical dispatch name."""
    tool_name = body.get("name") or body.get("tool_name") or "unknown"
    if not tool_name or tool_name == "unknown":
        return "unknown"

    # Compatibility: Some MCP clients surface names as `mcp_<server>_<tool>`.
    # The HTTP API always dispatches by the canonical tool name (e.g. `list_tools`).
    mcp_prefix = f"mcp_{mcp_server_name}_"
    if tool_name.startswith(mcp_prefix):
        return tool_name[len(mcp_prefix):]
    return tool_name

# ---------------------------------------------------------------------------
# Trusted networks: localhost, Tailscale CGNAT, private RFC1918 ranges
# ---------------------------------------------------------------------------
_TRUSTED_NETWORKS = [
    _ipaddress.ip_network("127.0.0.0/8"),
    _ipaddress.ip_network("::1/128"),
    _ipaddress.ip_network("100.64.0.0/10"),   # Tailscale CGNAT
    _ipaddress.ip_network("192.168.0.0/16"),
    _ipaddress.ip_network("10.0.0.0/8"),
    _ipaddress.ip_network("172.16.0.0/12"),
]


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _is_trusted_network(request) -> bool:
    """Check if request originates from a trusted network.

    Uses the actual TCP peer address only -- never trust X-Forwarded-For
    since there is no reverse proxy stripping it before us.
    """
    client_ip = request.client.host if request.client else None
    if not client_ip:
        return False
    try:
        addr = _ipaddress.ip_address(client_ip)
        return any(addr in net for net in _TRUSTED_NETWORKS)
    except ValueError:
        return False


def _http_unauthorized():
    return JSONResponse(
        {
            "success": False,
            "error": "Unauthorized",
            "hint": "Set UNITARES_HTTP_API_TOKEN in your environment and pass it as: Authorization: Bearer <token>",
        },
        status_code=401,
    )


def _check_http_auth(request, *, http_api_token: str | None) -> bool:
    """Bearer token auth for HTTP endpoints. Trusted networks bypass auth."""
    if _is_trusted_network(request):
        return True
    if not http_api_token:
        return True
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth or not isinstance(auth, str):
        return False
    if not auth.lower().startswith("bearer "):
        return False
    token = auth.split(" ", 1)[1].strip()
    return secrets.compare_digest(token, http_api_token)


async def _extract_client_session_id(request) -> str:
    """
    Stable per-client session id for HTTP callers.
    Uses SessionSignals + derive_session_key() for unified derivation.
    Falls back to legacy logic if signals unavailable.
    """
    from src.mcp_handlers.identity.handlers import derive_session_key, ua_hash_from_header

    signals = _build_http_session_signals(request)
    ua = signals.user_agent or ""
    x_session_id = signals.x_session_id
    ip_ua_fp = signals.ip_ua_fingerprint

    result = await derive_session_key(signals)

    # If derive_session_key returned the raw IP:UA fingerprint (no pin found),
    # and there's no explicit session header, generate a unique ID so REST
    # clients without session headers get distinct identities per request chain.
    if result == ip_ua_fp and not x_session_id:
        try:
            if hasattr(request, "state") and hasattr(request.state, "governance_client_id"):
                return str(getattr(request.state, "governance_client_id"))
        except Exception:
            pass
        import uuid as _uuid
        unique_id = str(_uuid.uuid4())[:12]
        try:
            host = request.client.host if request.client else "unknown"
            return f"http:{host}:{unique_id}"
        except Exception:
            return f"http:unknown:{unique_id}"

    return result


async def _resolve_http_bound_agent(tool_name: str, arguments: dict, signals) -> str | None:
    """Resolve an existing identity for HTTP requests before direct tool calls.

    This keeps direct HTTP tools like process_agent_update aligned with the
    fallback middleware path, which would otherwise inject session-bound identity.
    """
    if not isinstance(arguments, dict):
        return None

    # These tools establish or inspect identity; they should not be pre-bound.
    skip_tools = {
        "identity",
        "onboard",
        "bind_session",
        "health_check",
        "list_tools",
        "get_server_info",
        "describe_tool",
        "debug_request_context",
    }
    if tool_name in skip_tools:
        return None

    from src.mcp_handlers.context import update_context_agent_id
    from src.mcp_handlers.identity.handlers import derive_session_key, resolve_session_identity

    # Respect an already explicit UUID.
    explicit_agent_id = arguments.get("agent_id")
    if isinstance(explicit_agent_id, str) and len(explicit_agent_id) == 36 and explicit_agent_id.count("-") == 4:
        update_context_agent_id(explicit_agent_id)
        return explicit_agent_id

    # Sticky transport cache: the MCP middleware path (identity_step.py) consults
    # this cache before calling resolve_session_identity, which prevents identity
    # fragmentation for repeat callers from the same IP:UA fingerprint. The REST
    # path previously bypassed this cache entirely — every call went through
    # PATH 3 creation, producing mcp_YYYYMMDD ghost identities at a rate of
    # hundreds per day (see identity-ghost-proliferation investigation 2026-04-15).
    # Mirror the same check here so REST gets the same ghost protection.
    force_new = bool(arguments.get("force_new"))
    client_session_id = arguments.get("client_session_id")
    continuity_token = arguments.get("continuity_token")
    transport_key = None
    if not force_new and not client_session_id and not continuity_token:
        try:
            import time as _time
            from src.mcp_handlers.middleware.identity_step import (
                _TRANSPORT_CACHE_TTL,
                _transport_cache_key,
                _transport_identity_cache,
            )
            transport_key = _transport_cache_key(signals)
            if transport_key:
                cached = _transport_identity_cache.get(transport_key)
                if cached and (_time.monotonic() - cached.bound_at) < _TRANSPORT_CACHE_TTL:
                    update_context_agent_id(cached.agent_uuid)
                    arguments["agent_id"] = cached.agent_uuid
                    return cached.agent_uuid
        except Exception as e:
            logger.debug("[STICKY-REST] cache check failed: %s", e)

    session_key = await derive_session_key(signals, arguments)

    # Extract agent UUID from continuity token so PATH 2.8 can rebind via
    # cryptographic ownership proof — without this the resolver cannot
    # distinguish \"Watcher owns agent-907e3195-c64\" from \"some prior REST
    # caller claimed that session_key and got cached\", and PATH 1 happily
    # returns whichever agent the cache holds (issue #110).
    _token_agent_uuid = None
    if continuity_token:
        try:
            from src.mcp_handlers.identity.session import extract_token_agent_uuid
            _token_agent_uuid = extract_token_agent_uuid(str(continuity_token))
        except Exception:
            pass

    resolved = await resolve_session_identity(
        session_key,
        persist=False,
        model_type=arguments.get("model_type"),
        client_hint=arguments.get("client_hint"),
        resume=True,
        token_agent_uuid=_token_agent_uuid,
    )
    if resolved and not resolved.get("created"):
        agent_uuid = resolved.get("agent_uuid")
        if agent_uuid:
            update_context_agent_id(agent_uuid)
            arguments["agent_id"] = agent_uuid
            # Populate sticky cache so subsequent REST calls from the same
            # fingerprint hit the cache path above and skip resolution entirely.
            if transport_key:
                try:
                    from src.mcp_handlers.middleware.identity_step import (
                        update_transport_binding,
                    )
                    update_transport_binding(
                        transport_key, agent_uuid, session_key, source="rest"
                    )
                except Exception as e:
                    logger.debug("[STICKY-REST] cache update failed: %s", e)
            return agent_uuid
    return None


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
        if not _check_http_auth(request, http_api_token=http_api_token):
            return _http_unauthorized()
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
                    "parameters": tool.inputSchema
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


async def http_call_tool(request):
    """Execute tool via HTTP - any model can call this"""
    # CRITICAL FIX: Ensure all code paths return valid JSONResponse
    # Empty or malformed responses cause Starlette ASGI protocol violations
    # (AssertionError: Unexpected message: http.response.start vs http.response.body)

    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    mcp_server_name = request.state._http_api_mcp_server_name

    # SECURITY: Limit request body size (prevent DoS via large payloads)
    MAX_REQUEST_SIZE = 10 * 1024 * 1024  # 10MB limit
    body = None
    tool_name = "unknown"
    try:
        if not _check_http_auth(request, http_api_token=http_api_token):
            return _http_unauthorized()
        # Check content length before parsing
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                size = int(content_length)
                if size > MAX_REQUEST_SIZE:
                    return JSONResponse({
                        "success": False,
                        "error": "Request body too large",
                        "max_size_mb": MAX_REQUEST_SIZE // (1024 * 1024)
                    }, status_code=413)
            except ValueError:
                pass  # Invalid content-length, let JSON parsing handle it

        body = await request.json()

        # SECURITY: Validate request structure
        if not isinstance(body, dict):
            return JSONResponse({"success": False, "error": "Request body must be a JSON object"}, status_code=400)

        # SECURITY: Limit arguments dictionary size (prevent DoS via large dicts)
        arguments = body.get("arguments", {})
        if isinstance(arguments, dict) and len(arguments) > 100:
            return JSONResponse({
                "success": False,
                "error": "Too many arguments",
                "max_arguments": 100
            }, status_code=400)

        tool_name = _normalize_http_tool_name(body, mcp_server_name)
        if not tool_name or tool_name == "unknown":
            return JSONResponse({"success": False, "error": "Missing 'name' field — pass the tool name as 'name', e.g. {\"name\": \"onboard\", \"arguments\": {...}}"}, status_code=400)

        # SECURITY: Validate tool name format (prevent injection)
        if not isinstance(tool_name, str) or len(tool_name) > 100:
            return JSONResponse({
                "success": False,
                "error": "Invalid tool name format"
            }, status_code=400)

        # DEPRECATED: SSE-specific tools removed
        # These tools are no longer registered but kept for backward compat
        if tool_name == "get_connected_clients":
            return JSONResponse({
                "name": tool_name,
                "result": {"error": "Tool deprecated. SSE transport deprecated by MCP. Use Streamable HTTP."},
                "success": False
            })

        if tool_name == "get_connection_diagnostics":
            # DEPRECATED: SSE-specific tool
            return JSONResponse({
                "name": tool_name,
                "result": {"error": "Tool deprecated. SSE transport deprecated by MCP. Use Streamable HTTP."},
                "success": False
            })

        # Inject stable client session for identity binding (avoid collision with dialectic session_id)
        # DO NOT TRUST client_session_id FOR AUTH — TRANSPORT-INJECTED HERE, NOT CLIENT-ASSERTED.
        # For auth signals, read SessionSignals (headers) or continuity_token (HMAC-signed).
        # See PR #35 revert (gap #1) and PR #42 Part C rationale.
        client_session_id = None
        if isinstance(arguments, dict) and "client_session_id" not in arguments:
            client_session_id = await _extract_client_session_id(request)
            arguments["client_session_id"] = client_session_id
        elif isinstance(arguments, dict):
            client_session_id = arguments.get("client_session_id")

        # NOTE: X-Agent-Id NOT injected as agent_id pre-dispatch.
        # Session binding via X-Session-ID handles identity.
        x_agent_id = request.headers.get("x-agent-id") or request.headers.get("X-Agent-Id")

        # AUTO-DETECT CLIENT TYPE and MODEL TYPE from User-Agent for better auto-naming
        # This ensures agent_id reflects actual runtime (e.g., Cursor + GPT/Codex)
        if isinstance(arguments, dict):
            ua = (request.headers.get("user-agent") or "").lower()

            # Detect client type via the shared detector so HTTP and MCP paths
            # agree on the claude_code / claude_desktop / claude disambiguation.
            if "client_hint" not in arguments:
                from src.mcp_handlers.context import detect_client_from_user_agent
                detected_client = detect_client_from_user_agent(ua)
                if detected_client:
                    arguments["client_hint"] = detected_client
                    logger.debug(f"[HTTP] Auto-detected client_hint={detected_client} from UA")

            # Detect model type to prevent identity collision
            if "model_type" not in arguments:
                detected_model = None

                # Prefer explicit model header if available.
                model_header = request.headers.get("x-model") or request.headers.get("X-Model")
                if model_header:
                    detected_model = model_header.strip().lower()

                # Then infer from User-Agent.
                if not detected_model:
                    if "gpt-5.3" in ua and "codex" in ua:
                        detected_model = "gpt-5.3-codex"
                    elif "gpt-5.4" in ua and "codex" in ua:
                        detected_model = "gpt-5.4-codex"
                    elif "gpt-5" in ua and "codex" in ua:
                        detected_model = "gpt-5-codex"
                    elif "composer" in ua:
                        detected_model = "composer"
                    elif "codex" in ua:
                        detected_model = "codex"
                    elif "chatgpt" in ua or "openai" in ua or "gpt-5" in ua or "gpt-4" in ua or "gpt-3" in ua:
                        detected_model = "gpt"
                    elif "claude" in ua and "codex" not in ua and "gpt" not in ua and "openai" not in ua:
                        detected_model = "claude"
                    elif "gemini" in ua:
                        detected_model = "gemini"

                if detected_model:
                    arguments["model_type"] = detected_model
                    logger.debug(f"[HTTP] Auto-detected model_type={detected_model} from headers")

        from src.mcp_handlers.context import (
            reset_session_context,
            reset_session_signals,
            set_session_context,
            set_session_signals,
        )

        signals = _build_http_session_signals(request)
        signals_token = set_session_signals(signals)

        # SET SESSION CONTEXT for contextvars-based identity lookup
        # This allows success_response() and status() to find binding without arguments
        context_token = set_session_context(
            session_key=client_session_id,
            client_session_id=client_session_id,
            agent_id=x_agent_id or (arguments.get("agent_id") if isinstance(arguments, dict) else None),
        )
        try:
            if isinstance(arguments, dict):
                await _resolve_http_bound_agent(tool_name, arguments, signals)
            result = await execute_http_tool(tool_name, arguments)
        finally:
            reset_session_context(context_token)
            reset_session_signals(signals_token)
        return JSONResponse(_build_http_tool_response(tool_name, result))
    except json.JSONDecodeError as e:
        # SECURITY: Sanitize JSON parsing errors
        logger.error(f"Invalid JSON in request: {e}", exc_info=True)
        return JSONResponse({
            "success": False,
            "error": "Invalid JSON format",
            "error_type": "JSONDecodeError"
        }, status_code=400)
    except ValueError as e:
        # SECURITY: Safe to expose validation errors
        logger.warning(f"Validation error: {e}")
        return JSONResponse({
            "success": False,
            "error": str(e),
            "error_type": "ValidationError"
        }, status_code=400)
    except KeyError as e:
        # SECURITY: Safe to expose missing key errors
        logger.warning(f"Missing required field: {e}")
        return JSONResponse({
            "success": False,
            "error": f"Missing required field: {str(e)}",
            "error_type": "KeyError"
        }, status_code=400)
    except Exception as e:
        # SECURITY: Sanitize internal errors (don't expose stack traces, file paths, etc.)
        tool_name_safe = body.get("name", "unknown") if body else "unknown"
        logger.error(f"Error calling tool '{tool_name_safe}': {e}", exc_info=True)

        # Only expose safe error information
        error_msg = "An error occurred processing your request"
        error_type = type(e).__name__

        # For known error types, provide more specific messages
        if isinstance(e, (AttributeError, TypeError)):
            error_msg = "Invalid request format"
        elif isinstance(e, RuntimeError):
            error_msg = "Service temporarily unavailable"

        return JSONResponse({
            "name": tool_name_safe if isinstance(tool_name_safe, str) else None,
            "result": None,
            "success": False,
            "error": error_msg,
            "error_type": error_type
        }, status_code=500)


async def http_health(request):
    """Health check endpoint -- always public (monitoring, load balancers)"""

    # These are injected by register_http_routes via request.state
    server_ready = request.state._http_api_server_ready_fn()
    server_start_time = request.state._http_api_server_start_time
    server_version = request.state._http_api_server_version
    conn_tracker: ConnectionTracker = request.state._http_api_connection_tracker
    has_streamable_http = request.state._http_api_has_streamable_http
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")

    # Calculate uptime
    uptime_seconds = time.time() - server_start_time
    uptime_hours = uptime_seconds / 3600
    uptime_days = uptime_hours / 24

    # Format uptime string
    if uptime_days >= 1:
        uptime_str = f"{int(uptime_days)}d {int((uptime_hours % 24))}h {int((uptime_seconds % 3600) / 60)}m"
    elif uptime_hours >= 1:
        uptime_str = f"{int(uptime_hours)}h {int((uptime_seconds % 3600) / 60)}m {int(uptime_seconds % 60)}s"
    else:
        uptime_str = f"{int(uptime_seconds / 60)}m {int(uptime_seconds % 60)}s"

    # DB pool health
    db_health = {"status": "unknown"}
    try:
        from src.db import get_db
        db = get_db()
        if hasattr(db, '_pool') and db._pool is not None:
            pool = db._pool
            db_health = {
                "status": "connected",
                "pool_size": pool.get_size(),
                "pool_idle": pool.get_idle_size(),
                "pool_max": pool.get_max_size(),
            }
        else:
            db_health = {"status": "no_pool"}
    except Exception as e:
        db_health = {"status": "error", "error": str(e)}

    return JSONResponse({
        "status": "ok" if server_ready else "warming_up",
        "version": server_version,
        "uptime": {
            "seconds": int(uptime_seconds),
            "formatted": uptime_str,
            "started_at": datetime.fromtimestamp(server_start_time).isoformat() if server_start_time else None
        },
        "connections": {
            "active": conn_tracker.count,
            "healthy": sum(1 for c in conn_tracker.connections.values() if c.get("health_status") == "healthy")
        },
        "database": db_health,
        "transports": {
            "streamable_http": "/mcp (primary, JSON response mode)" if has_streamable_http else "not available",
        },
        "endpoints": {
            "list_tools": "GET /v1/tools",
            "call_tool": "POST /v1/tools/call",
            "health": "GET /health",
            "metrics": "GET /metrics",
            "dashboard": "GET /dashboard"
        },
        "auth": {
            "enabled": bool(http_api_token),
            "header": "Authorization: Bearer <token>" if http_api_token else None
        },
        "session": {
            "header": "X-Session-ID (recommended for stable identity binding)"
        },
        "identity": {
            "header": "X-Agent-Id",
            "description": "CLI/GPT identity - pass your agent name to maintain identity across REST requests"
        },
        "note": "Use /mcp for MCP clients (Streamable HTTP)."
    })


async def http_health_live(request):
    """Liveness probe — server process is up. Always public, no checks."""
    return JSONResponse({"status": "alive"})


async def http_health_ready(request):
    """Readiness probe — server has completed warmup and is accepting requests."""
    server_ready = request.state._http_api_server_ready_fn()
    if server_ready:
        return JSONResponse({"status": "ready"})
    return JSONResponse({"status": "warming_up"}, status_code=503)


async def http_health_deep(request):
    """Deep health — reads the cached snapshot produced by deep_health_probe_task.

    Does NOT touch the DB at request time (see
    docs/handoffs/2026-04-10-option-f-spec.md). If the probe has not populated
    the cache yet, returns 503 and instructs the caller to retry.
    """
    from src.services.health_snapshot import (
        get_snapshot,
        is_stale,
        PROBE_INTERVAL_SECONDS,
        STALENESS_THRESHOLD_SECONDS,
    )

    snapshot, age_seconds, produced_at = get_snapshot()
    if snapshot is None:
        return JSONResponse(
            {
                "status": "unavailable",
                "error": "Health snapshot not yet populated — deep probe has not run.",
                "retry_after_seconds": 5,
            },
            status_code=503,
        )

    response = dict(snapshot)
    response["_cache"] = {
        "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "produced_at": produced_at,
        "stale": is_stale(age_seconds),
        "probe_interval_seconds": PROBE_INTERVAL_SECONDS,
        "staleness_threshold_seconds": STALENESS_THRESHOLD_SECONDS,
    }
    return JSONResponse(response)


async def http_metrics(request):
    """Prometheus metrics endpoint using prometheus-client library"""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not _check_http_auth(request, http_api_token=http_api_token):
        return _http_unauthorized()

    # These are injected by register_http_routes via request.state
    server_start_time = request.state._http_api_server_start_time
    server_version = request.state._http_api_server_version
    conn_tracker: ConnectionTracker = request.state._http_api_connection_tracker

    try:
        # Update gauges with current values before generating output
        # Server info (static, set once)
        SERVER_INFO.labels(version=server_version).set(1)

        # Server uptime
        uptime_seconds = time.time() - server_start_time
        SERVER_UPTIME.set(uptime_seconds)

        # Connection metrics
        CONNECTIONS_ACTIVE.set(conn_tracker.count)

        # Agent metrics (from cached metadata — no DB call in handler path)
        try:
            from src.mcp_handlers.shared import get_mcp_server
            mcp_server = get_mcp_server()
            # Read already-loaded metadata dict; background tasks keep it fresh.
            # Do NOT call load_metadata_async() here — it awaits asyncpg.
            status_counts = {"active": 0, "paused": 0, "archived": 0, "waiting_input": 0, "deleted": 0}
            for meta in mcp_server.agent_metadata.values():
                status = getattr(meta, 'status', 'active')
                if status in status_counts:
                    status_counts[status] += 1
                else:
                    status_counts["active"] += 1

            for status, count in status_counts.items():
                AGENTS_TOTAL.labels(status=status).set(count)
        except Exception as e:
            logger.debug(f"Could not load agent metrics: {e}")

        # Dialectic sessions (in-memory, no DB call)
        try:
            from src.mcp_handlers.dialectic.session import ACTIVE_SESSIONS
            DIALECTIC_SESSIONS_ACTIVE.set(len(ACTIVE_SESSIONS))
        except Exception as e:
            logger.debug(f"Could not load dialectic metrics: {e}")

        # Generate Prometheus exposition format using the library
        output = generate_latest(REGISTRY)

        return Response(
            content=output,
            media_type=CONTENT_TYPE_LATEST
        )
    except Exception as e:
        logger.error(f"Error generating metrics: {e}", exc_info=True)
        return JSONResponse({
            "error": "Failed to generate metrics",
            "details": str(e)
        }, status_code=500)


# Dashboard endpoint
async def http_dashboard(request):
    """Serve the web dashboard"""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    dashboard_path = Path(__file__).parent.parent / "dashboard" / "index.html"
    if dashboard_path.exists():
        html = dashboard_path.read_text()
        # Cache-bust: append ?v=<max_mtime> so edits are picked up without restart
        import re as _re
        _dash_dir = dashboard_path.parent
        _v = str(int(max(
            (f.stat().st_mtime for f in _dash_dir.iterdir() if f.is_file()),
            default=_startup_ts,
        )))
        html = _re.sub(
            r'(src|href)="/dashboard/([^"]+)"',
            rf'\1="/dashboard/\2?v={_v}"',
            html,
        )
        # Inject API token so dashboard JS can authenticate.
        # Always overwrite — token may have rotated since last visit.
        if http_api_token:
            token_script = (
                f'<script>localStorage.setItem("unitares_api_token","{http_api_token}")</script>'
            )
            html = html.replace("</head>", f"{token_script}</head>", 1)
        return Response(
            content=html,
            media_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )
    return JSONResponse({
        "error": "Dashboard not found",
        "path": str(dashboard_path)
    }, status_code=404)


async def http_phase(request):
    """Serve the phase-space visualization"""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    phase_path = Path(__file__).parent.parent / "dashboard" / "phase.html"
    if phase_path.exists():
        html = phase_path.read_text()
        if http_api_token:
            token_script = (
                f'<script>if(!localStorage.getItem("unitares_api_token"))'
                f'{{localStorage.setItem("unitares_api_token","{http_api_token}")}}</script>'
            )
            html = html.replace("</head>", f"{token_script}</head>", 1)
        return Response(content=html, media_type="text/html")
    return JSONResponse({"error": "Phase view not found", "path": str(phase_path)}, status_code=404)


# Dashboard static files (utils.js, components.js)
async def http_dashboard_static(request):
    """Serve dashboard static files"""
    file_path = request.path_params.get("file", "")
    if not file_path or ".." in file_path:
        return JSONResponse({"error": "Invalid file path"}, status_code=400)

    # Only allow specific files for security
    allowed_files = [
        "utils.js", "state.js", "colors.js", "components.js",
        "visualizations.js", "agents.js", "discoveries.js",
        "dialectic.js", "eisv-charts.js", "timeline.js",
        "residents.js", "fleet-metrics.js", "watcher.js", "sentinel.js",
        "vigil.js", "resident-progress.js",
        "styles.css", "dashboard.js",
        "phase.js",
    ]
    if file_path not in allowed_files:
        return JSONResponse({
            "error": "File not allowed",
            "requested": file_path,
            "allowed": allowed_files
        }, status_code=403)

    static_path = Path(__file__).parent.parent / "dashboard" / file_path
    if static_path.exists() and static_path.is_file():
        # Determine content type
        content_type = "application/javascript"
        if file_path.endswith(".css"):
            content_type = "text/css"
        elif file_path.endswith(".json"):
            content_type = "application/json"

        return Response(
            content=static_path.read_text(),
            media_type=content_type,
            headers={"Cache-Control": "no-cache"},
        )
    return JSONResponse({
        "error": "File not found",
        "path": str(static_path)
    }, status_code=404)


# HTTP polling fallback for EISV (when WebSocket is blocked by proxy auth)
async def http_eisv_latest(request):
    """Return the latest EISV update as JSON (polling fallback for WebSocket)."""
    if broadcaster_instance.last_update:
        return JSONResponse(broadcaster_instance.last_update)
    return JSONResponse({"type": "no_data", "message": "No EISV updates yet"}, status_code=200)


async def http_eisv_recent(request):
    """Return the last N eisv_update events in chronological order.

    Backfill endpoint for dashboard clients that just connected — lets the
    chart populate immediately from the broadcaster's ring buffer instead of
    waiting for the next live check-in. Used both by WebSocket clients on
    reconnect and polling-fallback clients (when upstream proxies block the
    WS upgrade, e.g. Cloudflare tunnels without the WebSocket toggle).
    """
    try:
        limit = int(request.query_params.get("limit", 120))
    except (TypeError, ValueError):
        limit = 120
    limit = max(1, min(limit, 500))

    events: list = []
    for event in broadcaster_instance.event_history:
        if isinstance(event, dict) and event.get("type") == "eisv_update":
            events.append(event)
    events = events[-limit:]
    return JSONResponse({"type": "eisv_recent", "count": len(events), "events": events})


# Events API endpoint for dashboard
async def http_events(request):
    """Return recent governance events for dashboard."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not _check_http_auth(request, http_api_token=http_api_token):
        return _http_unauthorized()
    try:
        from src.event_detector import event_detector

        limit = int(request.query_params.get("limit", 50))
        agent_id = request.query_params.get("agent_id")
        event_type = request.query_params.get("type")
        since_raw = request.query_params.get("since")
        since = int(since_raw) if since_raw is not None else None

        events = event_detector.get_recent_events(
            limit=limit,
            agent_id=agent_id,
            event_type=event_type,
            since=since
        )

        # Supplement from PostgreSQL when in-memory buffer is thin
        # (e.g. right after a restart)
        if len(events) < limit:
            try:
                from src.audit_db import query_audit_events_async
                from datetime import datetime, timedelta, timezone
                start_time = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
                db_events = await query_audit_events_async(
                    agent_id=agent_id,
                    event_type=event_type,
                    start_time=start_time,
                    limit=limit,
                    order="desc",
                )
                # Merge: use in-memory event_ids to deduplicate
                mem_ids = {e.get("event_id") for e in events if e.get("event_id")}
                # When `since` is given, audit rows with non-int event_ids (UUIDs)
                # are unreachable via the int-cursor protocol and would replay
                # every poll — drop them. See CIRWEL/unitares#25.
                int_cursor = since is not None
                for de in db_events:
                    de_id = de.get("event_id")
                    if de_id in mem_ids:
                        continue
                    if int_cursor:
                        try:
                            int(de_id)
                        except (TypeError, ValueError):
                            continue
                    # Reshape audit row → dashboard event shape
                    payload = de.get("details", {})
                    events.append({
                        "type": payload.get("type", de.get("event_type", "")),
                        "severity": payload.get("severity", "info"),
                        "message": payload.get("message", de.get("event_type", "")),
                        "agent_id": de.get("agent_id"),
                        "agent_name": payload.get("agent_name", ""),
                        "timestamp": de.get("timestamp"),
                        "event_id": de_id,
                    })
                events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
                events = events[:limit]
            except Exception as db_err:
                logger.debug(f"Audit DB supplement failed (non-fatal): {db_err}")

        return JSONResponse({
            "success": True,
            "events": events,
            "count": len(events)
        })
    except Exception as e:
        logger.error(f"Error fetching events: {e}")
        return JSONResponse({
            "success": False,
            "error": str(e),
            "events": []
        }, status_code=500)


# Event types surfaced by /v1/lifecycle/recent. Pause-side answers
# "why did this agent stop?", resume-side answers "how did it come back?".
_LIFECYCLE_EVENT_TYPES = (
    "lifecycle_paused",
    "lifecycle_resumed",
    "lifecycle_archived",
    "lifecycle_loop_detected",
    "lifecycle_stuck_detected",
    "circuit_breaker_trip",
    "circuit_breaker_reset",
)


async def http_lifecycle_recent(request):
    """GET /v1/lifecycle/recent — recent lifecycle / circuit-breaker events
    from audit.events with the full payload (reason, EISV, drift) and
    agent label resolution.

    Query params:
      - agent_id: filter to one agent (UUID or label)
      - hours: lookback window in hours (default 24, max 168)
      - limit: max events (default 100, max 500)
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not _check_http_auth(request, http_api_token=http_api_token):
        return _http_unauthorized()
    try:
        from src.audit_db import query_audit_events_async
        from datetime import datetime, timedelta, timezone

        agent_id_param = request.query_params.get("agent_id")
        hours = max(1, min(168, int(request.query_params.get("hours", 24))))
        limit = max(1, min(500, int(request.query_params.get("limit", 100))))
        start_time = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        # Resolve label → UUID and build a UUID → label lookup for enrichment.
        from src.agent_metadata_model import agent_metadata
        label_to_uuid = {}
        uuid_to_label = {}
        for uuid_, meta in agent_metadata.items():
            label = getattr(meta, "label", None) or ""
            if label:
                label_to_uuid[label] = uuid_
                uuid_to_label[uuid_] = label
        resolved_agent_id = label_to_uuid.get(agent_id_param, agent_id_param) \
            if agent_id_param else None

        rows = await query_audit_events_async(
            agent_id=resolved_agent_id,
            event_types=list(_LIFECYCLE_EVENT_TYPES),
            start_time=start_time,
            limit=limit,
            order="desc",
        )

        events = []
        for r in rows:
            details = r.get("details") or {}
            events.append({
                "timestamp": r.get("timestamp"),
                "event_type": r.get("event_type"),
                "agent_id": r.get("agent_id"),
                "agent_label": uuid_to_label.get(r.get("agent_id"), ""),
                "reason": details.get("reason"),
                "details": details,
                "event_id": r.get("event_id"),
            })

        return JSONResponse({
            "success": True,
            "events": events,
            "count": len(events),
            "window_hours": hours,
        })
    except Exception as e:
        logger.error(f"Error fetching lifecycle events: {e}")
        return JSONResponse({
            "success": False,
            "error": str(e),
            "events": []
        }, status_code=500)


# Allowed severity values for externally posted findings
_FINDING_SEVERITIES = frozenset({"info", "low", "medium", "warning", "high", "critical"})
# Only accept *_finding event types via this endpoint (prevents spoofing
# reserved dashboard event types like verdict_change / risk_threshold)
_FINDING_TYPE_SUFFIX = "_finding"
# Required top-level fields on the posted JSON
_FINDING_REQUIRED_FIELDS = ("type", "severity", "message", "agent_id", "agent_name", "fingerprint")


async def http_post_metric(request):
    """POST /v1/metrics — write one `(name, value)` point into `metrics.series`.

    Body: `{"name": "...", "value": 1.23, "ts"?: "2026-04-20T..."}`
    Name must be registered in `src.fleet_metrics.catalog`; a leaked bearer
    token therefore cannot inject arbitrary metric names.
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not _check_http_auth(request, http_api_token=http_api_token):
        return _http_unauthorized()
    try:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"success": False, "error": "Invalid JSON"}, status_code=400)

        if not isinstance(payload, dict):
            return JSONResponse({"success": False, "error": "Body must be a JSON object"}, status_code=400)

        name = payload.get("name")
        value = payload.get("value")
        ts_raw = payload.get("ts")
        if not isinstance(name, str) or not name:
            return JSONResponse({"success": False, "error": "Missing or invalid 'name'"}, status_code=400)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return JSONResponse({"success": False, "error": "Missing or invalid 'value' (number required)"}, status_code=400)

        ts = None
        if ts_raw is not None:
            if not isinstance(ts_raw, str):
                return JSONResponse({"success": False, "error": "'ts' must be ISO8601 string"}, status_code=400)
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                return JSONResponse({"success": False, "error": "'ts' is not valid ISO8601"}, status_code=400)

        from src.fleet_metrics import record
        try:
            await record(name, float(value), ts=ts)
        except KeyError as exc:
            return JSONResponse(
                {"success": False, "error": str(exc)},
                status_code=404,
            )
        return JSONResponse({"success": True}, status_code=201)
    except Exception as e:
        logger.error(f"Error recording metric: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def http_get_metrics(request):
    """GET /v1/metrics?name=...&since=...&until=...&limit=... — return a series."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not _check_http_auth(request, http_api_token=http_api_token):
        return _http_unauthorized()
    try:
        params = request.query_params
        name = params.get("name")
        if not name:
            return JSONResponse({"success": False, "error": "'name' query param required"}, status_code=400)

        def _parse_ts(raw: str | None):
            if raw is None:
                return None
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return "INVALID"

        since = _parse_ts(params.get("since"))
        until = _parse_ts(params.get("until"))
        if since == "INVALID" or until == "INVALID":
            return JSONResponse({"success": False, "error": "'since'/'until' must be ISO8601"}, status_code=400)

        try:
            limit = int(params.get("limit", "10000"))
        except ValueError:
            return JSONResponse({"success": False, "error": "'limit' must be integer"}, status_code=400)

        from src.fleet_metrics import query
        points = await query(name=name, since=since, until=until, limit=limit)
        return JSONResponse({
            "success": True,
            "name": name,
            "points": [{"ts": p.ts.isoformat(), "value": p.value} for p in points],
            "count": len(points),
        })
    except Exception as e:
        logger.error(f"Error querying metrics: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def http_get_metrics_catalog(request):
    """GET /v1/metrics/catalog — list all registered metric names and descriptions."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not _check_http_auth(request, http_api_token=http_api_token):
        return _http_unauthorized()
    from src.fleet_metrics import catalog as _catalog
    from src.fleet_metrics.storage import latest_ts_for_names
    metrics = sorted(_catalog.values(), key=lambda x: x.name)
    # last_point_ts lets the dashboard suppress empty `.error` twins
    # without firing a per-name probe — see dashboard/fleet-metrics.js.
    try:
        last_ts = await latest_ts_for_names([m.name for m in metrics])
    except Exception as e:
        logger.warning(f"metrics catalog: latest_ts probe failed: {e}")
        last_ts = {}
    return JSONResponse({
        "success": True,
        "metrics": [
            {
                "name": m.name,
                "description": m.description,
                "unit": m.unit,
                "last_point_ts": last_ts[m.name].isoformat() if m.name in last_ts else None,
            }
            for m in metrics
        ],
    })


async def http_get_progress_flat_recent(request):
    """GET /v1/progress_flat/recent?hours=24 — latest snapshot per
    configured resident plus the probe-self row.
    """
    import json as _json
    from src.db import get_db
    from src.resident_progress.registry import RESIDENT_PROGRESS_REGISTRY
    from src.resident_progress.status import resolve_status

    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not _check_http_auth(request, http_api_token=http_api_token):
        return _http_unauthorized()

    try:
        hours = int(request.query_params.get("hours", "24"))
    except (TypeError, ValueError):
        hours = 24
    hours = max(1, min(hours, 168))  # clamp to [1, 168]

    db = get_db()
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (resident_label)
                   resident_label, resident_uuid::text AS resident_uuid,
                   ticked_at, source, metric_value, window_seconds,
                   threshold, metric_below_threshold, heartbeat_alive,
                   candidate, suppressed_reason, error_details,
                   liveness_inputs, loop_detector_state
            FROM progress_flat_snapshots
            WHERE ticked_at > now() - make_interval(hours => $1)
            ORDER BY resident_label, ticked_at DESC
            """,
            hours,
        )

    by_label: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        # Coerce types for JSON
        if d.get("ticked_at") is not None:
            d["ticked_at"] = d["ticked_at"].isoformat()
        # error_details / liveness_inputs / loop_detector_state may arrive
        # as JSON-serialized strings (asyncpg jsonb default) or dicts
        # depending on connection-pool init. Normalize to dict-or-None.
        for jk in ("error_details", "liveness_inputs", "loop_detector_state"):
            v = d.get(jk)
            if isinstance(v, str):
                try:
                    d[jk] = _json.loads(v)
                except Exception:
                    pass
        by_label[r["resident_label"]] = d

    out = []
    for label in list(RESIDENT_PROGRESS_REGISTRY) + ["progress_flat_probe"]:
        r = by_label.get(label)
        if r is None:
            out.append({
                "resident_label": label,
                "status": "unresolved",
                "metric_value": None,
                "threshold": None,
                "window_seconds": None,
                "ticked_at": None,
            })
            continue
        r["status"] = resolve_status(r)
        out.append(r)

    return JSONResponse({"success": True, "rows": out})


_WATCHER_FINDINGS_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "watcher" / "findings.jsonl"
)
_WATCHER_DAILY_WINDOW_DAYS = 30


def _watcher_summary_from_rows(rows, now=None, window_days=_WATCHER_DAILY_WINDOW_DAYS):
    """Aggregate watcher findings.jsonl rows into dashboard-ready shape.

    Pure function so test coverage doesn't need to stand up the full HTTP app —
    feed it a list of parsed-dict rows, get back the counts + daily buckets.
    """
    from collections import Counter, defaultdict
    from datetime import datetime, timedelta, timezone

    by_status = Counter()
    by_severity = Counter()   # open-only (surfaced + open) — the actionable queue
    by_pattern = defaultdict(lambda: {"surfaced": 0, "confirmed": 0, "dismissed": 0, "other": 0})
    daily = defaultdict(int)  # yyyy-mm-dd → count of detected_at in that day
    resolutions_daily = defaultdict(lambda: {"confirmed": 0, "dismissed": 0})

    if now is None:
        now = datetime.now(timezone.utc)
    window_start = (now - timedelta(days=window_days - 1)).date()

    def _parse_date(value):
        if not value:
            return None
        try:
            # Tolerate trailing Z and no tz
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            return datetime.fromisoformat(value)
        except Exception:
            return None

    # Status names used by Watcher (findings.py:VALID_FINDING_STATUSES):
    # the closed-resolved status is "confirmed", not "resolved" — earlier
    # versions of this aggregator looked for "resolved" and silently dropped
    # every confirmed finding into "other", which made the dashboard claim
    # zero confirms regardless of reality.
    for row in rows:
        status = str(row.get("status", "surfaced"))
        pattern = str(row.get("pattern") or "?")
        severity = str(row.get("severity") or "?")
        by_status[status] += 1

        bucket = by_pattern[pattern]
        if status in ("confirmed", "dismissed"):
            bucket[status] += 1
        elif status in ("surfaced", "open"):
            bucket["surfaced"] += 1
            by_severity[severity] += 1
        else:
            bucket["other"] += 1

        detected = _parse_date(row.get("detected_at"))
        if detected and detected.date() >= window_start:
            daily[detected.date().isoformat()] += 1

        # Resolution timestamps written by update_finding_status:
        # confirmed_at / dismissed_at (ISO 8601, UTC).
        for key, kind in (("confirmed_at", "confirmed"), ("dismissed_at", "dismissed")):
            ts = _parse_date(row.get(key))
            if ts and ts.date() >= window_start:
                resolutions_daily[ts.date().isoformat()][kind] += 1

    # Pattern table — include confirm/dismiss ratio for noise detection
    patterns_out = []
    for pat, b in by_pattern.items():
        total_closed = b["confirmed"] + b["dismissed"]
        dismiss_ratio = (b["dismissed"] / total_closed) if total_closed else None
        patterns_out.append({
            "pattern": pat,
            "surfaced": b["surfaced"],
            "confirmed": b["confirmed"],
            "dismissed": b["dismissed"],
            "other": b["other"],
            "dismiss_ratio": dismiss_ratio,
        })
    patterns_out.sort(
        key=lambda p: (-p["surfaced"], -(p["confirmed"] + p["dismissed"]), p["pattern"])
    )

    # Daily series spans the full window so the chart renders zeros instead of gaps
    timeline = []
    for i in range(window_days):
        day = (window_start + timedelta(days=i)).isoformat()
        timeline.append({
            "day": day,
            "detected": daily.get(day, 0),
            "confirmed": resolutions_daily[day]["confirmed"],
            "dismissed": resolutions_daily[day]["dismissed"],
        })

    return {
        "total": sum(by_status.values()),
        "by_status": dict(by_status),
        "by_severity_open": dict(by_severity),
        "patterns": patterns_out,
        "timeline": timeline,
        "window_days": window_days,
        "generated_at": now.isoformat(),
    }


async def http_bootstrap_silent(request):
    """GET /v1/bootstrap/silent — agents bootstrapped past N hours with no real check-in.

    Validation surface for onboard-bootstrap-checkin §6 (population
    observability). The proposal exists to count exactly this population:
    agents with a synthetic t=0 anchor but no measured trajectory.

    Query params:
      min_age_hours (int, default 24): skip recently-bootstrapped agents
                                       that may genuinely be about to check in.
      limit (int, default 50, max 200): cap the returned list.

    Returns:
      {success, count, min_age_hours, agents: [{agent_id, identity_id,
        bootstrap_state_id, bootstrap_recorded_at, bootstrap_age_hours,
        display_name}]}
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not _check_http_auth(request, http_api_token=http_api_token):
        return _http_unauthorized()

    try:
        min_age_hours = int(request.query_params.get("min_age_hours", 24))
    except (TypeError, ValueError):
        min_age_hours = 24
    min_age_hours = max(0, min_age_hours)

    try:
        limit = int(request.query_params.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))

    try:
        from src.db import get_db
        db = get_db()
        count = await db.count_bootstrap_only_agents(min_age_hours=min_age_hours)
        rows = await db.list_bootstrap_only_agents(
            min_age_hours=min_age_hours, limit=limit,
        )
    except Exception as e:
        return JSONResponse(
            {"success": False, "error": f"bootstrap_silent query failed: {e}"},
            status_code=500,
        )

    # Datetimes need to be JSON-serializable.
    def _norm(row):
        out = dict(row)
        ts = out.get("bootstrap_recorded_at")
        if ts is not None and hasattr(ts, "isoformat"):
            out["bootstrap_recorded_at"] = ts.isoformat()
        age = out.get("bootstrap_age_hours")
        if age is not None:
            out["bootstrap_age_hours"] = round(float(age), 3)
        return out

    return JSONResponse({
        "success": True,
        "count": count,
        "min_age_hours": min_age_hours,
        "limit": limit,
        "returned": len(rows),
        "agents": [_norm(r) for r in rows],
    })


async def http_watcher_summary(request):
    """GET /v1/watcher/summary — aggregate Watcher findings for the dashboard panel.

    Reads data/watcher/findings.jsonl in-process (watcher's append-only audit
    log) and returns counts + a daily time series. Data is gitignored, so
    absence = empty summary (not an error)."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not _check_http_auth(request, http_api_token=http_api_token):
        return _http_unauthorized()

    rows = []
    path = _WATCHER_FINDINGS_PATH
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        # Skip malformed lines silently — findings.jsonl is
                        # append-only and a partial write shouldn't 500 the panel.
                        continue
    except OSError as e:
        return JSONResponse({"success": False, "error": f"findings read failed: {e}"}, status_code=500)

    summary = _watcher_summary_from_rows(rows)
    summary["success"] = True
    summary["findings_path"] = str(path)
    return JSONResponse(summary)


_SENTINEL_DEFAULT_WINDOW_HOURS = 24
_SENTINEL_DEFAULT_RECENT_LIMIT = 50


def _sentinel_summary_from_events(
    events, now=None, window_hours=_SENTINEL_DEFAULT_WINDOW_HOURS,
    recent_limit=_SENTINEL_DEFAULT_RECENT_LIMIT,
):
    """Aggregate sentinel_finding and sentinel_alarm_finding events into
    dashboard-ready shape.

    Pure function so tests can feed parsed-dict events and assert on the
    output without standing up Starlette or the event_detector singleton.

    Two event shapes are accepted: fleet-analysis findings (carry
    `finding_type` + `violation_class`) and forced-release alarms (carry
    `alarm_kind`, no violation class assigned in taxonomy yet). Stream
    entries fall back `finding_type` to `alarm_kind` so the dashboard panel
    has a non-null finding_type column for alarm rows. Sentinel findings
    have no open/closed lifecycle — they're transient fleet-state signals.
    """
    from collections import Counter, defaultdict
    from datetime import datetime, timedelta, timezone

    if now is None:
        now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=window_hours)

    def _parse_ts(value):
        if not value:
            return None
        try:
            if isinstance(value, str) and value.endswith("Z"):
                value = value[:-1] + "+00:00"
            return datetime.fromisoformat(value)
        except Exception:
            return None

    windowed = []
    for e in events:
        ts = _parse_ts(e.get("timestamp"))
        if ts is None:
            # Malformed timestamp — count toward totals but skip window check
            windowed.append((None, e))
            continue
        if ts >= window_start:
            windowed.append((ts, e))

    by_severity = Counter()
    by_class_counts = Counter()
    by_class_severity = defaultdict(Counter)

    for _ts, e in windowed:
        severity = str(e.get("severity") or "?")
        vclass = str(e.get("violation_class") or "?")
        by_severity[severity] += 1
        by_class_counts[vclass] += 1
        by_class_severity[vclass][severity] += 1

    by_violation_class = [
        {
            "violation_class": vc,
            "count": by_class_counts[vc],
            "by_severity": dict(by_class_severity[vc]),
        }
        for vc in sorted(by_class_counts, key=lambda v: (-by_class_counts[v], v))
    ]

    # Recent stream — newest first. Events with bad timestamps sort last but
    # are still included so operators can see they exist.
    def _sort_key(pair):
        ts, _ = pair
        return ts or datetime.min.replace(tzinfo=timezone.utc)

    recent_sorted = sorted(windowed, key=_sort_key, reverse=True)
    recent = [
        {
            "timestamp": e.get("timestamp"),
            "severity": e.get("severity"),
            "violation_class": e.get("violation_class"),
            # Alarm events don't carry finding_type — fall back to alarm_kind
            # so the dashboard panel doesn't show a blank cell.
            "finding_type": e.get("finding_type") or e.get("alarm_kind"),
            "message": e.get("message"),
            "agent_id": e.get("agent_id"),
            "event_id": e.get("event_id"),
        }
        for _ts, e in recent_sorted[:recent_limit]
    ]

    return {
        "total": len(windowed),
        "by_severity": dict(by_severity),
        "by_violation_class": by_violation_class,
        "recent": recent,
        "window_hours": window_hours,
        "generated_at": now.isoformat(),
    }


async def http_sentinel_summary(request):
    """GET /v1/sentinel/summary — aggregate recent sentinel_finding and
    sentinel_alarm_finding events for the dashboard panel.

    Reads from event_detector's in-memory ring buffer (same source that
    powers the live event stream). Transient across governance-mcp
    restarts by design — sentinel findings are fleet-state signals, not a
    historical backlog. Both fleet-analysis findings and forced-release
    alarms are surfaced together so the panel reflects the full Sentinel
    output stream (Surface 2 + Surface 3 + Surface 4)."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not _check_http_auth(request, http_api_token=http_api_token):
        return _http_unauthorized()

    try:
        window_hours = int(request.query_params.get("window_hours", _SENTINEL_DEFAULT_WINDOW_HOURS))
    except ValueError:
        window_hours = _SENTINEL_DEFAULT_WINDOW_HOURS
    window_hours = max(1, min(window_hours, 24 * 30))

    try:
        recent_limit = int(request.query_params.get("limit", _SENTINEL_DEFAULT_RECENT_LIMIT))
    except ValueError:
        recent_limit = _SENTINEL_DEFAULT_RECENT_LIMIT
    recent_limit = max(1, min(recent_limit, 500))

    from src.event_detector import event_detector
    # Fetch both Sentinel emit shapes. Pre-2026-05-06 the alarm path's
    # `type` was `sentinel_forced_release_alarm` and got 400'd at the gate
    # (#398); now that it lands as `sentinel_alarm_finding`, the panel
    # also has to look it up here or alarms remain invisible.
    events = list(event_detector.get_recent_events(
        event_type="sentinel_finding", limit=500,
    ))
    events.extend(event_detector.get_recent_events(
        event_type="sentinel_alarm_finding", limit=500,
    ))

    summary = _sentinel_summary_from_events(
        events, window_hours=window_hours, recent_limit=recent_limit,
    )
    summary["success"] = True
    return JSONResponse(summary)


# ---------------------------------------------------------------------------
# Vigil panel endpoint
# ---------------------------------------------------------------------------

_VIGIL_DEFAULT_WINDOW_HOURS = 72
_VIGIL_DEFAULT_RECENT_LIMIT = 30
_VIGIL_CYCLE_HISTORY_LIMIT = 48  # ~24h at one cycle / 30min


def _vigil_agent_id(mcp_server_obj) -> Optional[str]:
    """Resolve the active Vigil agent_id from mcp_server agent_metadata.

    Mirrors the label->meta preference logic in ``http_residents`` (prefer
    active over archived; within same tier, prefer more total_updates) so
    the panel tracks the same Vigil row the residents strip shows.
    """
    best: Optional[tuple[str, Any]] = None
    for agent_id, meta in list(getattr(mcp_server_obj, "agent_metadata", {}).items()):
        label = getattr(meta, "label", None) or getattr(meta, "display_name", None)
        if not label or label.lower() != "vigil":
            continue
        if best is None:
            best = (agent_id, meta)
            continue
        b_meta = best[1]
        b_active = getattr(b_meta, "status", None) == "active"
        n_active = getattr(meta, "status", None) == "active"
        if n_active and not b_active:
            best = (agent_id, meta)
            continue
        if b_active and not n_active:
            continue
        if (getattr(meta, "total_updates", 0) or 0) > \
           (getattr(b_meta, "total_updates", 0) or 0):
            best = (agent_id, meta)
    return best[0] if best else None


def _vigil_cycle_history(agent_id: str, window_hours: int,
                         limit: int = _VIGIL_CYCLE_HISTORY_LIMIT) -> list[dict]:
    """Flatten eisv_update events for Vigil into a cycle history for the panel.

    Each entry: ts, coherence, risk, verdict, E, I, S, V. Newest first.
    Pulls from the broadcaster ring buffer (~6h of fleet-wide events, longer
    for low-traffic residents like Vigil), clipped to ``window_hours``.
    """
    cutoff = time.time() - window_hours * 3600
    points: list[dict] = []
    for event in broadcaster_instance.event_history:
        if not isinstance(event, dict):
            continue
        if event.get("type") != "eisv_update":
            continue
        if event.get("agent_id") != agent_id:
            continue
        ts_str = event.get("timestamp")
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        flat = _extract_eisv_fields(event)
        points.append({
            "timestamp": ts_str,
            "ts": ts,
            "E": flat.get("E"),
            "I": flat.get("I"),
            "S": flat.get("S"),
            "V": flat.get("V"),
            "coherence": flat.get("coherence"),
            "risk": flat.get("risk_score"),
            "verdict": flat.get("verdict"),
        })
    points.sort(key=lambda p: p["ts"], reverse=True)
    return points[:limit]


def _vigil_stats(cycles: list[dict], writes: list[dict]) -> dict:
    """Roll up cycle list + write list into summary metrics for the stat strip."""
    now_ts = time.time()
    last_cycle_ts = cycles[0]["ts"] if cycles else None
    last_cycle_iso = cycles[0]["timestamp"] if cycles else None

    def _within(hours, items, key="ts"):
        floor = now_ts - hours * 3600
        return sum(1 for it in items if (it.get(key) or 0) >= floor)

    cycles_24h = _within(24, cycles)
    writes_24h = 0
    for w in writes:
        ts_str = w.get("timestamp")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
        if ts >= now_ts - 24 * 3600:
            writes_24h += 1

    coh_values = [c.get("coherence") for c in cycles
                  if isinstance(c.get("coherence"), (int, float))]
    avg_coh = (sum(coh_values) / len(coh_values)) if coh_values else None

    verdicts = [c.get("verdict") for c in cycles if c.get("verdict")]
    last_verdict = verdicts[0] if verdicts else None

    return {
        "last_cycle_at": last_cycle_iso,
        "last_cycle_age_seconds": (now_ts - last_cycle_ts) if last_cycle_ts else None,
        "cycles_24h": cycles_24h,
        "writes_24h": writes_24h,
        "avg_coherence_window": avg_coh,
        "last_verdict": last_verdict,
        "total_cycles_in_window": len(cycles),
        "total_writes_in_window": len(writes),
    }


async def http_vigil_summary(request):
    """GET /v1/vigil/summary — Vigil panel data.

    Vigil is a resident janitor that runs every 30min via launchd. Its KG
    writes are mostly low-severity groundskeeper deltas ("N stale, M archived")
    that crowd the main activity feed. This endpoint segregates them into a
    dedicated stream so the main feed can filter them out by default.

    Response shape::

        {
            "success": true,
            "agent_id": "...",
            "window_hours": 72,
            "stats": {"last_cycle_at": ..., "cycles_24h": N, "writes_24h": N, ...},
            "cycles": [{"timestamp": ..., "coherence": ..., "verdict": ..., ...}, ...],
            "recent_writes": [{"id": ..., "summary": ..., "severity": ..., ...}, ...],
            "generated_at": "..."
        }
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not _check_http_auth(request, http_api_token=http_api_token):
        return _http_unauthorized()

    try:
        window_hours = int(request.query_params.get("window_hours", _VIGIL_DEFAULT_WINDOW_HOURS))
    except ValueError:
        window_hours = _VIGIL_DEFAULT_WINDOW_HOURS
    window_hours = max(1, min(window_hours, 24 * 30))

    try:
        recent_limit = int(request.query_params.get("limit", _VIGIL_DEFAULT_RECENT_LIMIT))
    except ValueError:
        recent_limit = _VIGIL_DEFAULT_RECENT_LIMIT
    recent_limit = max(1, min(recent_limit, 200))

    from src.mcp_handlers.shared import lazy_mcp_server
    agent_id = _vigil_agent_id(lazy_mcp_server)

    if not agent_id:
        return JSONResponse({
            "success": True,
            "agent_id": None,
            "window_hours": window_hours,
            "stats": {},
            "cycles": [],
            "recent_writes": [],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "note": "no Vigil agent found in metadata",
        })

    cycles = _vigil_cycle_history(agent_id, window_hours)
    writes = await _recent_writes_for_agent(agent_id, limit=recent_limit)
    stats = _vigil_stats(cycles, writes)

    return JSONResponse({
        "success": True,
        "agent_id": agent_id,
        "window_hours": window_hours,
        "stats": stats,
        "cycles": cycles,
        "recent_writes": writes,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })


async def http_record_finding(request):
    """POST /api/findings — ingest an external finding into the event ring buffer."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not _check_http_auth(request, http_api_token=http_api_token):
        return _http_unauthorized()
    try:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"success": False, "error": "Invalid JSON"}, status_code=400)

        if not isinstance(payload, dict):
            return JSONResponse({"success": False, "error": "Body must be a JSON object"}, status_code=400)

        missing = [f for f in _FINDING_REQUIRED_FIELDS if not payload.get(f)]
        if missing:
            return JSONResponse(
                {"success": False, "error": f"Missing required fields: {missing}"},
                status_code=400,
            )

        if not str(payload["type"]).endswith(_FINDING_TYPE_SUFFIX):
            return JSONResponse(
                {"success": False, "error": f"type must end in {_FINDING_TYPE_SUFFIX}"},
                status_code=400,
            )

        if payload["severity"] not in _FINDING_SEVERITIES:
            return JSONResponse(
                {"success": False, "error": f"severity must be one of {sorted(_FINDING_SEVERITIES)}"},
                status_code=400,
            )

        from src.event_detector import event_detector
        stored = event_detector.record_event(payload)
        return JSONResponse({
            "success": True,
            "deduped": stored is None,
            "event": stored,
        })
    except Exception as e:
        logger.error(f"Error recording finding: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# Incident history endpoint (anomalies + stuck agents from audit log)
async def http_incidents(request):
    """Return historical anomaly and stuck-agent incidents from the audit trail."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not _check_http_auth(request, http_api_token=http_api_token):
        return _http_unauthorized()
    try:
        from src.audit_db import query_audit_events_async

        event_type = request.query_params.get("type")  # "anomaly_detected" or "stuck_detected"
        limit = min(int(request.query_params.get("limit", 200)), 500)

        # Query both types if none specified
        types_to_query = [event_type] if event_type else ["anomaly_detected", "stuck_detected"]
        all_events = []
        for et in types_to_query:
            events = await query_audit_events_async(event_type=et, order="desc", limit=limit)
            all_events.extend(events)

        # Sort by timestamp descending, limit total
        all_events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        all_events = all_events[:limit]

        return JSONResponse({"success": True, "incidents": all_events, "count": len(all_events)})
    except Exception as e:
        logger.error(f"Error fetching incidents: {e}")
        return JSONResponse({"success": False, "error": str(e), "incidents": []}, status_code=500)


# Activity sparkline endpoint
async def http_activity(request):
    """Return check-in activity buckets for sparkline chart."""
    try:
        window = int(request.query_params.get("window", 60))
        bucket = int(request.query_params.get("bucket", 5))
        # Clamp to reasonable limits
        window = max(10, min(window, 360))
        bucket = max(1, min(bucket, 30))
        buckets = broadcaster_instance.get_activity_buckets(
            window_minutes=window, bucket_minutes=bucket
        )
        return JSONResponse({
            "success": True,
            "buckets": buckets,
            "window_minutes": window,
            "bucket_minutes": bucket
        })
    except Exception as e:
        logger.error(f"Error fetching activity: {e}")
        return JSONResponse({
            "success": False,
            "error": str(e),
            "buckets": []
        }, status_code=500)


# ---------------------------------------------------------------------------
# Residents endpoint — per-operator configurable "always-on agents" view
# ---------------------------------------------------------------------------


# Default silence thresholds in seconds — agents that go longer than this without
# a check-in are flagged as "silent" on the dashboard. Only used for agents the
# operator hasn't configured explicitly.
_DEFAULT_RESIDENT_SILENCE_SECONDS: Dict[str, int] = {
    # Long cron cadence agents get generous thresholds.
    "vigil": 40 * 60,      # 30-min cron + buffer
    "sentinel": 15 * 60,   # 5-min continuous + 10min tolerance
    "lumen": 10 * 60,      # continuous poll
    # Event-driven agents may be quiet for a long time and still be healthy.
    "watcher": 24 * 3600,
    # Daily scraper — 24hr cadence + 6hr buffer before silence is flagged.
    "chronicler": 30 * 3600,
}


def _resolve_resident_labels(mcp_server_obj) -> tuple[list[str], str]:
    """Figure out which agent labels to treat as residents.

    Precedence (operator choice wins):
    1. ``UNITARES_RESIDENT_AGENTS`` env var — comma-separated labels  → "env"
    2. Agent metadata with a ``resident`` attribute set to True       → "metadata"
    3. ``KNOWN_RESIDENT_LABELS`` ∩ labels present in agent_metadata   → "known-residents"
       (the canonical resident list used by grounding/class_indicator
       is the source of truth; dashboard reuses it rather than re-declaring)
    4. Empty list                                                     → "none"

    Returns ``(labels, source)`` so the caller can label the response without
    re-deriving the precedence state.
    """
    env_value = os.getenv("UNITARES_RESIDENT_AGENTS", "").strip()
    if env_value:
        labels = [lbl.strip() for lbl in env_value.split(",") if lbl.strip()]
        return labels, "env"

    flagged: list[str] = []
    for meta in getattr(mcp_server_obj, "agent_metadata", {}).values():
        if getattr(meta, "resident", False):
            label = getattr(meta, "label", None) or getattr(meta, "display_name", None)
            if label:
                flagged.append(label)
    if flagged:
        return flagged, "metadata"

    # Path 3: auto-detect from the canonical resident list, intersected with
    # the actual fleet so a fresh install doesn't advertise absent residents.
    from src.grounding.class_indicator import KNOWN_RESIDENT_LABELS
    present: set[str] = set()
    for meta in getattr(mcp_server_obj, "agent_metadata", {}).values():
        label = getattr(meta, "label", None) or getattr(meta, "display_name", None)
        if label and label in KNOWN_RESIDENT_LABELS:
            present.add(label)
    if present:
        # Canonical order is stable (Vigil, Sentinel, Watcher, Steward,
        # Chronicler, Lumen) so dashboard layout doesn't jitter when the dict
        # ordering shifts.
        canonical_order = ["Vigil", "Sentinel", "Watcher", "Steward", "Chronicler", "Lumen"]
        ordered = [lbl for lbl in canonical_order if lbl in present]
        return ordered, "known-residents"

    return [], "none"


def _latest_eisv_for_agent(agent_id: str) -> Optional[dict]:
    """Find the most recent eisv_update event for a given agent_id in the broadcaster history."""
    for event in reversed(broadcaster_instance.event_history):
        if not isinstance(event, dict):
            continue
        if event.get("type") != "eisv_update":
            # Broadcaster puts eisv_updates in event_history too; non-eisv events are skipped.
            continue
        if event.get("agent_id") == agent_id:
            return event
    return None


def _extract_eisv_fields(event: dict) -> dict:
    """Pull the data-shape we expose to the dashboard from a raw broadcaster event.

    The broadcaster stores eisv updates with nested ``eisv`` and ``metrics``
    dicts. Surface them flat so the JSON payload is convenient for the
    frontend without re-mapping.
    """
    eisv = event.get("eisv") or {}
    metrics = event.get("metrics") or {}
    decision = event.get("decision") or {}
    return {
        "E": eisv.get("E"),
        "I": eisv.get("I"),
        "S": eisv.get("S"),
        "V": eisv.get("V"),
        "coherence": event.get("coherence") if event.get("coherence") is not None else metrics.get("coherence"),
        "risk_score": metrics.get("risk_score") if metrics.get("risk_score") is not None else event.get("risk"),
        # Verdict can come from decision.action (governance dynamics) or
        # metrics.verdict (behavioral classifier — "safe", "caution", etc.).
        "verdict": decision.get("action") or metrics.get("verdict"),
        "agent_name": event.get("agent_name"),
        "timestamp": event.get("timestamp"),
    }


def _parse_resident_timestamp(value: object) -> Optional[datetime]:
    """Parse resident activity timestamps as timezone-aware datetimes."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _coherence_history_for_agent(agent_id: str, window_minutes: int = 60) -> list[dict]:
    """Collect coherence (plus risk, verdict) data points for a sparkline.

    Pulls from the broadcaster's 2000-entry event ring buffer — this covers
    roughly 6 hours of moderate activity. Each point has ts, coherence, risk.
    """
    cutoff = time.time() - window_minutes * 60
    points: list[dict] = []
    for event in broadcaster_instance.event_history:
        if not isinstance(event, dict):
            continue
        if event.get("type") != "eisv_update":
            continue
        if event.get("agent_id") != agent_id:
            continue
        ts_str = event.get("timestamp")
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        flat = _extract_eisv_fields(event)
        if flat["coherence"] is None:
            continue
        points.append({
            "ts": ts,
            "coherence": float(flat["coherence"]),
            "risk": float(flat["risk_score"]) if flat["risk_score"] is not None else None,
            "verdict": flat["verdict"],
        })
    return points


async def _recent_writes_for_agent(agent_id: str, limit: int = 5) -> list[dict]:
    """Pull recent KG writes authored by this agent, newest first.

    Uses the shared graph query rather than re-reading the broadcaster history,
    so this survives broadcaster restarts and covers more than the last 6h.
    """
    try:
        from src.knowledge_graph import get_knowledge_graph
        graph = await get_knowledge_graph()
        discoveries = await graph.query(agent_id=agent_id, limit=limit)
        out = []
        for d in (discoveries or [])[:limit]:
            out.append({
                "id": getattr(d, "id", None),
                "type": getattr(d, "type", None) or "note",
                "severity": getattr(d, "severity", None) or "low",
                "summary": (getattr(d, "summary", None) or "")[:200],
                "tags": list(getattr(d, "tags", None) or []),
                "timestamp": getattr(d, "timestamp", None),
            })
        return out
    except Exception as exc:
        logger.debug("_recent_writes_for_agent(%s) failed: %s", agent_id, exc)
        return []


async def http_residents(request):
    """Per-resident fleet view for the dashboard.

    Response shape::

        {
            "success": true,
            "configured": ["Vigil", "Sentinel", ...],
            "residents": [
                {
                    "label": "Vigil",
                    "agent_id": "...",
                    "status": "healthy" | "silent" | "paused" | "unknown",
                    "silence_seconds": 142,
                    "silence_threshold_seconds": 2400,
                    "last_checkin_at": "2026-04-14T...",
                    "last_checkin_source": "broadcaster_eisv" | "agent_metadata",
                    "metadata_last_update": "2026-04-14T...",
                    "latest_eisv_at": "2026-04-14T...",
                    "eisv": {"E": ..., "I": ..., "S": ..., "V": ...},
                    "coherence": 0.48,
                    "risk_score": 0.12,
                    "verdict": "proceed",
                    "history": [{"ts": ..., "coherence": ..., "risk": ...}, ...],
                    "recent_writes": [{"summary": ..., "tags": ..., ...}, ...],
                    "total_updates": 467
                },
                ...
            ],
            "source": "env" | "metadata" | "known-residents" | "none"
        }
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not _check_http_auth(request, http_api_token=http_api_token):
        return _http_unauthorized()

    try:
        from src.mcp_handlers.shared import lazy_mcp_server
        mcp_server_obj = lazy_mcp_server

        labels, source = _resolve_resident_labels(mcp_server_obj)

        # Index agent_metadata by label for O(1) lookup. When the same label
        # appears multiple times (e.g. archived + active duplicates created
        # across server restarts), prefer the most-active live record so the
        # dashboard tracks the agent that's actually running.
        label_to_meta = {}
        for agent_id, meta in list(getattr(mcp_server_obj, "agent_metadata", {}).items()):
            label = getattr(meta, "label", None)
            if not label:
                continue
            existing = label_to_meta.get(label)
            if existing is None:
                label_to_meta[label] = (agent_id, meta)
                continue
            existing_meta = existing[1]
            # Prefer active over archived/paused.
            existing_status = getattr(existing_meta, "status", None)
            new_status = getattr(meta, "status", None)
            existing_active = existing_status == "active"
            new_active = new_status == "active"
            if new_active and not existing_active:
                label_to_meta[label] = (agent_id, meta)
                continue
            if existing_active and not new_active:
                continue
            # Both same activity tier — prefer the one with more updates.
            if (getattr(meta, "total_updates", 0) or 0) > \
               (getattr(existing_meta, "total_updates", 0) or 0):
                label_to_meta[label] = (agent_id, meta)

        residents: list[dict] = []
        now_ts = time.time()
        for label in labels:
            entry = label_to_meta.get(label)
            agent_id = entry[0] if entry else None
            meta = entry[1] if entry else None

            latest = _latest_eisv_for_agent(agent_id) if agent_id else None
            history = _coherence_history_for_agent(agent_id) if agent_id else []
            recent_writes = await _recent_writes_for_agent(agent_id) if agent_id else []

            # Compute silence in seconds. The dashboard agent list uses
            # metadata.last_update while the resident strip also has access to
            # websocket/broadcaster EISV events. Treat both as activity signals
            # and choose the newest, otherwise the two dashboard rows can
            # disagree by several minutes after broadcaster gaps/restarts.
            metadata_last_update = getattr(meta, "last_update", None) if meta else None
            latest_eisv_at = latest.get("timestamp") if latest and latest.get("timestamp") else None
            metadata_dt = _parse_resident_timestamp(metadata_last_update)
            latest_dt = _parse_resident_timestamp(latest_eisv_at)
            last_checkin_str = None
            last_checkin_source = None
            if metadata_dt and latest_dt:
                if metadata_dt >= latest_dt:
                    last_checkin_str = metadata_last_update
                    last_checkin_source = "agent_metadata"
                else:
                    last_checkin_str = latest_eisv_at
                    last_checkin_source = "broadcaster_eisv"
            elif metadata_dt:
                last_checkin_str = metadata_last_update
                last_checkin_source = "agent_metadata"
            elif latest_dt:
                last_checkin_str = latest_eisv_at
                last_checkin_source = "broadcaster_eisv"

            silence_seconds: Optional[float] = None
            last_dt = _parse_resident_timestamp(last_checkin_str)
            if last_dt:
                silence_seconds = max(0.0, now_ts - last_dt.timestamp())

            # Prefer tag-driven cadence (generic, label-independent); fall
            # back to the hardcoded per-label default for agents not yet
            # migrated to ``cadence.*`` tags.
            silence_threshold: int = 30 * 60
            meta_tags = getattr(meta, "tags", None) or []
            from src.background_tasks import cadence_from_tags
            tag_cadence = cadence_from_tags(meta_tags)
            if tag_cadence is not None:
                # Threshold = 2x expected cadence — tolerates one missed cycle.
                silence_threshold = tag_cadence * 2
            else:
                silence_threshold = _DEFAULT_RESIDENT_SILENCE_SECONDS.get(label.lower(), 30 * 60)

            # Status: paused > silent > healthy > unknown.
            status = "unknown"
            if meta:
                if getattr(meta, "status", None) in ("paused", "archived"):
                    status = getattr(meta, "status")
                elif silence_seconds is not None and silence_seconds > silence_threshold:
                    status = "silent"
                elif latest is not None or silence_seconds is not None:
                    status = "healthy"

            flat = _extract_eisv_fields(latest) if latest else None
            residents.append({
                "label": label,
                "agent_id": agent_id,
                "status": status,
                "silence_seconds": round(silence_seconds, 1) if silence_seconds is not None else None,
                "silence_threshold_seconds": silence_threshold,
                "last_checkin_at": last_checkin_str,
                "last_checkin_source": last_checkin_source,
                "metadata_last_update": metadata_last_update,
                "latest_eisv_at": latest_eisv_at,
                "eisv": {
                    "E": flat["E"],
                    "I": flat["I"],
                    "S": flat["S"],
                    "V": flat["V"],
                } if flat else None,
                "coherence": flat["coherence"] if flat else None,
                "risk_score": flat["risk_score"] if flat else None,
                "verdict": flat["verdict"] if flat else None,
                "history": history,
                "recent_writes": recent_writes,
                "total_updates": getattr(meta, "total_updates", 0) if meta else 0,
            })

        return JSONResponse({
            "success": True,
            "configured": labels,
            "residents": residents,
            "source": source,
        })
    except Exception as exc:
        logger.error("http_residents error: %s", exc)
        return JSONResponse({
            "success": False,
            "error": str(exc),
            "residents": [],
        }, status_code=500)


# ---------------------------------------------------------------------------
# Resident tag-hygiene audit — Vigil consumes this to detect tag drift
# ---------------------------------------------------------------------------

# Tags every active resident must carry. Keep in sync with
# agents/sdk/src/unitares_sdk/agent.py::RESIDENT_TAGS. The Steward regression
# of 2026-04-20 was caused by this set drifting across onboarding paths —
# this endpoint exists so future drift is detectable in production.
RESIDENT_REQUIRED_TAGS: frozenset[str] = frozenset({"persistent", "autonomous"})


async def http_resident_tag_audit(request):
    """Report which active residents are missing required tags.

    Response shape::

        {
            "success": true,
            "required_tags": ["persistent", "autonomous"],
            "checked": ["Vigil", "Sentinel", "Watcher", "Steward", "Chronicler", "Lumen"],
            "missing": {
                "Watcher": ["autonomous"],
                ...
            },
            "ok_count": 4
        }

    `missing` is empty when the fleet is healthy. Each entry is a sorted list
    of tags that the resident SHOULD carry but doesn't. Residents absent from
    the running fleet are absent from both ``checked`` and ``missing``.
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not _check_http_auth(request, http_api_token=http_api_token):
        return _http_unauthorized()

    try:
        from src.mcp_handlers.shared import lazy_mcp_server
        from src.grounding.class_indicator import KNOWN_RESIDENT_LABELS

        mcp_server_obj = lazy_mcp_server
        checked: list[str] = []
        missing: dict[str, list[str]] = {}

        for meta in getattr(mcp_server_obj, "agent_metadata", {}).values():
            label = getattr(meta, "label", None)
            if not label or label not in KNOWN_RESIDENT_LABELS:
                continue
            if getattr(meta, "status", None) != "active":
                continue
            if label in checked:
                # Duplicate rows for the same label (ghost identities) — only
                # audit the first active one we encounter. Consistent with
                # the label_to_meta deduplication http_residents does.
                continue
            checked.append(label)
            have = set(getattr(meta, "tags", None) or [])
            gap = sorted(RESIDENT_REQUIRED_TAGS - have)
            if gap:
                missing[label] = gap

        return JSONResponse({
            "success": True,
            "required_tags": sorted(RESIDENT_REQUIRED_TAGS),
            "checked": sorted(checked),
            "missing": missing,
            "ok_count": len(checked) - len(missing),
        })
    except Exception as exc:
        logger.error("http_resident_tag_audit error: %s", exc)
        return JSONResponse({
            "success": False,
            "error": str(exc),
        }, status_code=500)


# ---------------------------------------------------------------------------
# Violation taxonomy endpoint — surface vocabulary for dashboards/bridges
# ---------------------------------------------------------------------------


async def http_taxonomy(request):
    """Return the violation taxonomy + reverse-lookup index as JSON.

    Lets the dashboard (and any other consumer) classify Watcher findings,
    Sentinel findings, and broadcast events into violation classes
    (CON / INT / ENT / REC / BEH / VOI) without having to ship its own copy
    of the YAML.

    Response shape::

        {
            "success": true,
            "version": 1,
            "classes": [{ "id": "INT", "name": "Integrity", ... }, ...],
            "reverse": {
                "watcher_patterns": {"P010": "INT", "P011": "INT", ...},
                "sentinel_findings": {"coordinated_degradation": "CON", ...},
                "broadcast_events": {"identity_drift": "CON", ...}
            }
        }

    Best-effort: if the taxonomy file is missing or malformed, returns a
    success=false response with an empty taxonomy rather than 500. The
    dashboard renders fine without classification — class badges just
    don't appear.
    """
    if not _check_http_auth(request, http_api_token=os.getenv("UNITARES_HTTP_API_TOKEN")):
        return _http_unauthorized()

    try:
        from agents.common import taxonomy as taxonomy_mod
        data = taxonomy_mod.load_taxonomy()
        # Build reverse index (taxonomy.py keeps it private; reconstruct here
        # so we don't depend on its internal _get_reverse implementation).
        reverse: dict = {
            "watcher_patterns": {},
            "sentinel_findings": {},
            "broadcast_events": {},
        }
        for cls in data.get("classes", []):
            cid = cls["id"]
            for kind in reverse:
                for sid in cls.get("surfaces", {}).get(kind, []):
                    reverse[kind][sid] = cid
        return JSONResponse({
            "success": True,
            "version": data.get("version"),
            "classes": data.get("classes", []),
            "reverse": reverse,
        })
    except Exception as exc:
        logger.warning("http_taxonomy failed: %s", exc)
        return JSONResponse({
            "success": False,
            "error": str(exc),
            "classes": [],
            "reverse": {
                "watcher_patterns": {},
                "sentinel_findings": {},
                "broadcast_events": {},
            },
        })


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

async def websocket_eisv_stream(websocket):
    """WebSocket endpoint for live EISV streaming to dashboard."""
    await broadcaster_instance.connect(websocket)
    try:
        while True:
            # Keep connection alive -- client sends pings, we just listen
            await websocket.receive_text()
    except Exception:
        await broadcaster_instance.disconnect(websocket)


# ---------------------------------------------------------------------------
# Debug: memory profiling (tracemalloc)
# ---------------------------------------------------------------------------

async def http_debug_memory(request):
    """Top memory allocations via tracemalloc (if enabled)."""
    import tracemalloc
    if not tracemalloc.is_tracing():
        return JSONResponse({"error": "tracemalloc not enabled"}, status_code=503)

    snapshot = tracemalloc.take_snapshot()
    # Filter out importlib/tracemalloc noise
    snapshot = snapshot.filter_traces([
        tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
        tracemalloc.Filter(False, "<frozen importlib._bootstrap_external>"),
        tracemalloc.Filter(False, tracemalloc.__file__),
    ])

    top_n = int(request.query_params.get("top", "25"))
    stats = snapshot.statistics("lineno")

    current, peak = tracemalloc.get_traced_memory()
    result = {
        "current_mb": round(current / 1024 / 1024, 1),
        "peak_mb": round(peak / 1024 / 1024, 1),
        "top_allocations": [
            {
                "file": str(stat.traceback),
                "size_mb": round(stat.size / 1024 / 1024, 2),
                "count": stat.count,
            }
            for stat in stats[:top_n]
        ],
    }

    # Also include monitor cache size
    try:
        from src.agent_monitor_state import monitors
        result["monitors_cached"] = len(monitors)
    except Exception:
        pass

    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register_http_routes(
    app: Starlette,
    *,
    connection_tracker: ConnectionTracker,
    server_ready_fn,
    server_start_time: float,
    server_version: str,
    has_streamable_http: bool,
    mcp_server_name: str = "governance-monitor-v1",
):
    """
    Register all HTTP REST endpoints on the given Starlette ``app``.

    Parameters that vary per-deployment (connection tracker, server readiness,
    version, etc.) are injected via a lightweight ASGI middleware that sets
    ``request.state`` attributes before each handler runs.  This avoids
    module-level globals while keeping handler signatures clean.
    """
    from starlette.middleware import Middleware
    from starlette.types import ASGIApp, Receive, Scope, Send

    # Tiny middleware that injects server context into request.state
    # so endpoint handlers can access connection_tracker, server_version, etc.
    class _InjectContextMiddleware:
        def __init__(self, app: ASGIApp):
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send):
            if scope["type"] in ("http", "websocket"):
                state = scope.setdefault("state", {})
                state["_http_api_connection_tracker"] = connection_tracker
                state["_http_api_server_ready_fn"] = server_ready_fn
                state["_http_api_server_start_time"] = server_start_time
                state["_http_api_server_version"] = server_version
                state["_http_api_has_streamable_http"] = has_streamable_http
                state["_http_api_mcp_server_name"] = mcp_server_name
            await self.app(scope, receive, send)

    app.add_middleware(_InjectContextMiddleware)

    # IMPORTANT: Static file route must come BEFORE dashboard route
    # to match /dashboard/utils.js, etc.
    app.routes.append(Route("/dashboard/{file}", http_dashboard_static, methods=["GET"]))
    app.routes.append(Route("/dashboard", http_dashboard, methods=["GET"]))
    app.routes.append(Route("/phase", http_phase, methods=["GET"]))
    app.routes.append(Route("/", http_dashboard, methods=["GET"]))  # Root also serves dashboard
    app.routes.append(Route("/v1/tools", http_list_tools, methods=["GET"]))
    app.routes.append(Route("/v1/tools/call", http_call_tool, methods=["POST"]))
    app.routes.append(Route("/health", http_health, methods=["GET"]))
    app.routes.append(Route("/health/live", http_health_live, methods=["GET"]))
    app.routes.append(Route("/health/ready", http_health_ready, methods=["GET"]))
    app.routes.append(Route("/health/deep", http_health_deep, methods=["GET"]))
    app.routes.append(Route("/metrics", http_metrics, methods=["GET"]))
    app.routes.append(Route("/v1/eisv/latest", http_eisv_latest, methods=["GET"]))
    app.routes.append(Route("/v1/eisv/recent", http_eisv_recent, methods=["GET"]))
    app.routes.append(Route("/v1/lifecycle/recent", http_lifecycle_recent, methods=["GET"]))
    app.routes.append(Route("/api/events", http_events, methods=["GET"]))
    app.routes.append(Route("/api/findings", http_record_finding, methods=["POST"]))
    app.routes.append(Route("/v1/metrics", http_post_metric, methods=["POST"]))
    app.routes.append(Route("/v1/metrics/series", http_get_metrics, methods=["GET"]))
    app.routes.append(Route("/v1/metrics/catalog", http_get_metrics_catalog, methods=["GET"]))
    app.routes.append(Route("/v1/progress_flat/recent", http_get_progress_flat_recent, methods=["GET"]))
    app.routes.append(Route("/v1/watcher/summary", http_watcher_summary, methods=["GET"]))
    app.routes.append(Route("/v1/bootstrap/silent", http_bootstrap_silent, methods=["GET"]))
    app.routes.append(Route("/v1/sentinel/summary", http_sentinel_summary, methods=["GET"]))
    app.routes.append(Route("/v1/vigil/summary", http_vigil_summary, methods=["GET"]))
    app.routes.append(Route("/api/activity", http_activity, methods=["GET"]))
    app.routes.append(Route("/api/incidents", http_incidents, methods=["GET"]))
    app.routes.append(Route("/v1/residents", http_residents, methods=["GET"]))
    app.routes.append(Route("/v1/residents/tag_audit", http_resident_tag_audit, methods=["GET"]))
    app.routes.append(Route("/v1/taxonomy", http_taxonomy, methods=["GET"]))
    app.routes.append(WebSocketRoute("/ws/eisv", websocket_eisv_stream))
    app.routes.append(Route("/debug/memory", http_debug_memory, methods=["GET"]))
