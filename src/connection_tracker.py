"""
Per-request identity propagation middleware.

Contains ConnectionTrackingMiddleware — pure-ASGI middleware that derives a
per-request ``governance_client_id``, propagates it via contextvars for tool
handlers, and gates ``/health`` during warmup. The SSE-only connection registry
(ConnectionTracker) was removed when the ``/sse`` transport was retired —
nothing populated it once /mcp became the only transport.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Callable

from src.logging_utils import get_logger

logger = get_logger(__name__)


class ConnectionTrackingMiddleware:
    """
    Pure-ASGI middleware for per-request identity propagation and warmup gating.

    IMPORTANT:
    Do NOT use Starlette's BaseHTTPMiddleware here. It is known to break streaming
    responses and can trigger:
      AssertionError: Unexpected message: {'type': 'http.response.start', ...}

    This middleware is implemented as a pure ASGI middleware to be safe for
    streaming responses (the /mcp Streamable HTTP transport).

    Constructor params:
      app             - ASGI application (passed by Starlette's add_middleware)
      server_ready_fn - callable returning bool (is the server ready?)
      server_version  - version string for warmup responses
    """

    def __init__(self, app,
                 server_ready_fn: Callable[[], bool] = lambda: True,
                 server_version: str = "unknown"):
        self.app = app
        self.server_ready_fn = server_ready_fn
        self.server_version = server_version

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        from starlette.datastructures import Headers
        headers = Headers(scope=scope)

        # === Server Warmup Check ===
        # Prevent "request before initialization" errors when clients reconnect
        # too quickly after a server restart. SSE connections are allowed (they need
        # to establish to complete MCP initialization), but health checks report status.
        if path == "/health" and not self.server_ready_fn():
            response_body = json.dumps({
                "status": "warming_up",
                "message": "Server is starting up, please retry in 2 seconds",
                "hint": "This prevents 'request before initialization' errors during multi-client reconnection",
                "server_version": self.server_version,
            }).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"retry-after", b"2"],
                ],
            })
            await send({
                "type": "http.response.body",
                "body": response_body,
            })
            return

        # Generate base id (unique per HTTP request).
        # For /mcp/ path, SessionSignals are already set by the ASGI wrapper
        # so we just read from scope.state if available, otherwise compute here.
        from src.mcp_handlers.context import get_session_signals, SessionSignals, set_session_signals
        signals = get_session_signals()

        if signals and signals.transport == "mcp":
            # ASGI wrapper already set signals — reuse computed client_id
            state = scope.get("state", {})
            base_id = state.get("governance_client_id")
            if not base_id:
                base_id = signals.x_client_id or signals.ip_ua_fingerprint or "unknown"
        else:
            # Legacy / REST paths — compute fingerprint and build signals
            base_id = headers.get("x-client-id") or headers.get("x-mcp-client-id")
            ua = headers.get("user-agent", "unknown")

            if not base_id:
                client = scope.get("client")
                client_ip = client[0] if (client and len(client) >= 1) else "unknown"
                ua_fingerprint = hashlib.md5(ua.encode()).hexdigest()[:6]
                from src.mcp_handlers.context import note_ua_fingerprint
                note_ua_fingerprint(ua_fingerprint, ua)
                base_id = f"{client_ip}:{ua_fingerprint}"

            # Build SessionSignals for legacy paths (if not already set)
            if not signals:
                from src.mcp_handlers.context import detect_client_from_user_agent
                legacy_signals = SessionSignals(
                    x_client_id=headers.get("x-client-id") or headers.get("x-mcp-client-id"),
                    x_session_id=headers.get("x-session-id"),
                    ip_ua_fingerprint=base_id,
                    user_agent=ua,
                    client_hint=detect_client_from_user_agent(ua),
                    x_agent_name=headers.get("x-agent-name"),
                    x_agent_id=headers.get("x-agent-id"),
                    transport="rest",
                )
                set_session_signals(legacy_signals)

        # Every request is now an ephemeral HTTP request (the long-lived /sse
        # transport was removed). Unique client_id per request.
        client_id = f"{base_id}:{uuid.uuid4().hex[:8]}"

        # Expose to downstream tool calls via FastMCP Context.request_context.request.state
        try:
            state = scope.setdefault("state", {})
            state["governance_client_id"] = client_id
        except Exception:
            pass

        # PROPAGATE IDENTITY via contextvars for tool handlers
        from src.mcp_handlers.context import set_session_context, reset_session_context
        context_token = set_session_context(
            session_key=client_id,
            client_session_id=headers.get("x-client-id"),
            user_agent=headers.get("user-agent")
        )

        # Pass the raw ASGI send/receive straight through — no wrapping. The
        # wrappers only existed to track SSE connection lifecycle, which is gone;
        # a raw pass-through is also maximally safe for /mcp streaming responses.
        try:
            return await self.app(scope, receive, send)
        finally:
            # Reset contextvars to prevent leakage
            if 'context_token' in locals():
                reset_session_context(context_token)
