"""
Connection tracking for multi-agent awareness and reliability.

Extracted from mcp_server.py — contains:
- ConnectionTracker: tracks client connections, reconnections, health
- ConnectionTrackingMiddleware: ASGI middleware for connection lifecycle
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Callable, Dict

from prometheus_client import Counter, Gauge, Histogram

from src.logging_utils import get_logger

logger = get_logger(__name__)

# ============================================================================
# Prometheus Metrics for Connection Tracking
# ============================================================================

CONNECTION_EVENTS = Counter(
    'unitares_connection_events_total',
    'Connection lifecycle events',
    ['event_type']  # connected, disconnected, reconnected, stale_cleaned
)

CONNECTION_DURATION = Histogram(
    'unitares_connection_duration_seconds',
    'Duration of client connections',
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600)
)

CONNECTION_HEALTH = Gauge(
    'unitares_connection_health',
    'Connection health status (1=healthy, 0=unhealthy)',
    ['client_id']
)

# Also owns CONNECTIONS_ACTIVE — imported back by mcp_server.py where needed
CONNECTIONS_ACTIVE = Gauge(
    'unitares_connections_active',
    'Number of active SSE connections'
)


class ConnectionTracker:
    """
    Enhanced connection tracker for multi-agent awareness and reliability.

    Features:
    - Reconnection tracking (detects clients that reconnect frequently)
    - Connection health monitoring (idle time, request rate)
    - Detailed diagnostics for debugging
    - History for forensics
    """

    _MAX_HISTORY = 100
    _MAX_DISCONNECTION_REASONS = 500
    _MAX_RECONNECTION_IPS = 200

    def __init__(self):
        self.connections: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        # Track connection history for diagnostics (bounded)
        self._history: list = []
        # Track reconnections by base client ID (IP) — bounded
        self._reconnection_counts: Dict[str, int] = {}
        # Track disconnection reasons — bounded
        self._disconnection_reasons: Dict[str, str] = {}

    def _log_event(self, event_type: str, client_id: str, details: Dict[str, Any] = None):
        """Log connection event to history and metrics."""
        event = {
            "timestamp": datetime.now().isoformat(),
            "event": event_type,
            "client_id": client_id,
            "details": details or {}
        }
        self._history.append(event)
        # Trim history if needed
        if len(self._history) > self._MAX_HISTORY:
            self._history = self._history[-self._MAX_HISTORY:]

        # Update Prometheus metrics
        CONNECTION_EVENTS.labels(event_type=event_type).inc()

        # Log at appropriate level
        if event_type in ("disconnected", "stale_cleaned"):
            logger.info(f"[CONNECTION] {event_type}: {client_id} - {details}")
        else:
            logger.info(f"[CONNECTION] {event_type}: {client_id}")

    def _get_base_client_id(self, client_id: str) -> str:
        """Extract base client ID (IP) for reconnection tracking."""
        # client_id format is typically "IP:PORT" or "IP:PORT:uuid"
        parts = client_id.split(":")
        return parts[0] if parts else client_id

    async def add_connection(self, client_id: str, metadata: Dict[str, Any] = None):
        """Register a new client connection with reconnection detection."""
        now = datetime.now()
        now_iso = now.isoformat()
        base_id = self._get_base_client_id(client_id)

        # Track reconnection
        reconnect_count = self._reconnection_counts.get(base_id, 0)
        is_reconnect = reconnect_count > 0

        connection_data = {
            "connected_at": now_iso,
            "last_activity": now_iso,
            "metadata": metadata or {},
            "request_count": 0,
            "reconnect_count": reconnect_count,
            "health_status": "healthy",
            "last_health_check": now_iso
        }

        async with self._lock:
            # Check if connection already exists (collision detection)
            if client_id in self.connections:
                existing = self.connections[client_id]
                prev_connected = existing.get('connected_at', 'unknown')
                prev_requests = existing.get('request_count', 0)

                # Calculate duration of previous connection
                try:
                    prev_connected_dt = datetime.fromisoformat(prev_connected)
                    duration = (now - prev_connected_dt).total_seconds()
                    CONNECTION_DURATION.observe(duration)
                except (ValueError, TypeError):
                    duration = 0

                logger.warning(
                    f"[CONNECTION] Client ID collision: '{client_id}' replacing existing. "
                    f"Previous: connected={prev_connected}, requests={prev_requests}, duration={duration:.1f}s"
                )

                # Increment reconnection count
                self._reconnection_counts[base_id] = reconnect_count + 1
                connection_data["reconnect_count"] = reconnect_count + 1

                self._log_event("reconnected", client_id, {
                    "previous_duration_seconds": duration,
                    "previous_requests": prev_requests,
                    "reconnect_count": reconnect_count + 1
                })
            else:
                self._reconnection_counts[base_id] = reconnect_count + 1
                event_type = "reconnected" if is_reconnect else "connected"
                self._log_event(event_type, client_id, {
                    "user_agent": (metadata or {}).get("user_agent", "unknown"),
                    "reconnect_count": reconnect_count if is_reconnect else 0
                })

            self.connections[client_id] = connection_data

            # Update Prometheus gauge
            CONNECTIONS_ACTIVE.set(len(self.connections))
            CONNECTION_HEALTH.labels(client_id=client_id).set(1)

            logger.info(
                f"[CONNECTION] Client {'reconnected' if is_reconnect else 'connected'}: "
                f"{client_id} (total: {len(self.connections)}, reconnects: {connection_data['reconnect_count']})"
            )

    async def remove_connection(self, client_id: str, reason: str = "client_disconnect"):
        """Remove a client connection with reason tracking."""
        async with self._lock:
            if client_id in self.connections:
                conn_data = self.connections[client_id]
                connected_at = conn_data.get("connected_at")
                request_count = conn_data.get("request_count", 0)

                # Calculate connection duration
                try:
                    connected_dt = datetime.fromisoformat(connected_at)
                    duration = (datetime.now() - connected_dt).total_seconds()
                    CONNECTION_DURATION.observe(duration)
                except (ValueError, TypeError):
                    duration = 0

                del self.connections[client_id]
                self._disconnection_reasons[client_id] = reason
                # Prune oldest disconnection reasons if over limit
                if len(self._disconnection_reasons) > self._MAX_DISCONNECTION_REASONS:
                    excess = len(self._disconnection_reasons) - self._MAX_DISCONNECTION_REASONS
                    for key in list(self._disconnection_reasons)[:excess]:
                        del self._disconnection_reasons[key]

                # Update Prometheus
                CONNECTIONS_ACTIVE.set(len(self.connections))
                try:
                    CONNECTION_HEALTH.remove(client_id)
                except Exception:
                    pass  # Label may not exist

                self._log_event("disconnected", client_id, {
                    "reason": reason,
                    "duration_seconds": duration,
                    "request_count": request_count
                })

                logger.info(
                    f"[CONNECTION] Client disconnected: {client_id} "
                    f"(reason={reason}, duration={duration:.1f}s, requests={request_count}, total: {len(self.connections)})"
                )

    async def update_activity(self, client_id: str):
        """Update last activity timestamp for a client."""
        now = datetime.now().isoformat()

        async with self._lock:
            if client_id in self.connections:
                self.connections[client_id]["last_activity"] = now
                self.connections[client_id]["request_count"] += 1

    async def check_health(self, client_id: str) -> Dict[str, Any]:
        """Check health of a specific connection."""
        now = datetime.now()

        async with self._lock:
            if client_id not in self.connections:
                return {"healthy": False, "reason": "not_connected"}

            conn = self.connections[client_id]

            # Check idle time
            try:
                last_activity = datetime.fromisoformat(conn["last_activity"])
                idle_seconds = (now - last_activity).total_seconds()
            except (ValueError, TypeError):
                idle_seconds = float('inf')

            # Check connection age
            try:
                connected_at = datetime.fromisoformat(conn["connected_at"])
                age_seconds = (now - connected_at).total_seconds()
            except (ValueError, TypeError):
                age_seconds = 0

            # Determine health status
            issues = []
            if idle_seconds > 300:  # 5 minutes idle
                issues.append(f"idle for {idle_seconds:.0f}s")
            if conn.get("reconnect_count", 0) > 5:
                issues.append(f"reconnected {conn['reconnect_count']} times")

            healthy = len(issues) == 0
            health_status = "healthy" if healthy else "degraded"

            # Update stored health status
            conn["health_status"] = health_status
            conn["last_health_check"] = now.isoformat()

            # Update Prometheus
            CONNECTION_HEALTH.labels(client_id=client_id).set(1 if healthy else 0)

            return {
                "healthy": healthy,
                "status": health_status,
                "idle_seconds": idle_seconds,
                "age_seconds": age_seconds,
                "request_count": conn.get("request_count", 0),
                "reconnect_count": conn.get("reconnect_count", 0),
                "issues": issues if issues else None
            }

    async def cleanup_stale_connections(self, max_idle_minutes: float = 30.0):
        """Remove connections that haven't been active recently and prune history bounds."""
        now = datetime.now()
        stale = []

        # First pass: identify stale connections and prune bounds
        async with self._lock:
            # Prune unbounded dicts
            if len(self._disconnection_reasons) > self._MAX_DISCONNECTION_REASONS:
                keep_count = self._MAX_DISCONNECTION_REASONS // 2
                items_to_keep = list(self._disconnection_reasons.items())[-keep_count:]
                self._disconnection_reasons = dict(items_to_keep)
            if len(self._reconnection_counts) > self._MAX_RECONNECTION_IPS:
                # Keep IPs with active connections, prune the rest
                active_ips = {self._get_base_client_id(cid) for cid in self.connections}
                pruned = {ip: cnt for ip, cnt in self._reconnection_counts.items()
                          if ip in active_ips}
                self._reconnection_counts = pruned

            for client_id, conn_data in self.connections.items():
                last_activity_str = conn_data.get("last_activity")
                if last_activity_str:
                    try:
                        last_activity = datetime.fromisoformat(last_activity_str)
                        idle_minutes = (now - last_activity).total_seconds() / 60
                        if idle_minutes > max_idle_minutes:
                            stale.append((client_id, idle_minutes))
                    except (ValueError, TypeError):
                        stale.append((client_id, float('inf')))

        # Second pass: remove stale connections
        if stale:
            for client_id, idle_mins in stale:
                await self.remove_connection(client_id, reason=f"stale_idle_{idle_mins:.1f}min")

            self._log_event("stale_cleaned", "batch", {
                "count": len(stale),
                "clients": [c[0] for c in stale]
            })

            logger.info(f"[CONNECTION] Cleaned up {len(stale)} stale connection(s)")

    async def get_diagnostics(self) -> Dict[str, Any]:
        """Get comprehensive connection diagnostics."""
        now = datetime.now()

        async with self._lock:
            clients = []
            for client_id, conn in self.connections.items():
                try:
                    connected_at = datetime.fromisoformat(conn["connected_at"])
                    last_activity = datetime.fromisoformat(conn["last_activity"])
                    age = (now - connected_at).total_seconds()
                    idle = (now - last_activity).total_seconds()
                except (ValueError, TypeError):
                    age = idle = 0

                clients.append({
                    "client_id": client_id,
                    "user_agent": conn.get("metadata", {}).get("user_agent", "unknown"),
                    "connected_at": conn["connected_at"],
                    "age_seconds": age,
                    "idle_seconds": idle,
                    "request_count": conn.get("request_count", 0),
                    "reconnect_count": conn.get("reconnect_count", 0),
                    "health_status": conn.get("health_status", "unknown")
                })

            # Sort by most recent activity
            clients.sort(key=lambda x: x["idle_seconds"])

            # Get recent events
            recent_events = self._history[-20:] if self._history else []

            # Identify potentially problematic clients
            problematic = [c for c in clients if c["reconnect_count"] > 3 or c["idle_seconds"] > 300]

            return {
                "timestamp": now.isoformat(),
                "total_connections": len(self.connections),
                "connections": clients,
                "recent_events": recent_events,
                "problematic_clients": problematic,
                "reconnection_summary": dict(self._reconnection_counts),
                "health_summary": {
                    "healthy": len([c for c in clients if c["health_status"] == "healthy"]),
                    "degraded": len([c for c in clients if c["health_status"] != "healthy"]),
                }
            }

    def get_connected_clients(self) -> Dict[str, Dict[str, Any]]:
        """Get all connected clients."""
        return dict(self.connections)

    @property
    def count(self) -> int:
        """Number of connected clients."""
        return len(self.connections)


class ConnectionTrackingMiddleware:
    """
    ASGI middleware for connection lifecycle tracking.

    IMPORTANT:
    Do NOT use Starlette's BaseHTTPMiddleware here. It is known to break streaming
    responses (like SSE) and can trigger:
      AssertionError: Unexpected message: {'type': 'http.response.start', ...}

    This middleware is implemented as a pure ASGI middleware to be safe for /sse.

    Constructor params:
      app               - ASGI application (passed by Starlette's add_middleware)
      connection_tracker - ConnectionTracker instance
      server_ready_fn    - callable returning bool (is the server ready?)
      server_version     - version string for probe/health responses
    """

    def __init__(self, app, connection_tracker: ConnectionTracker,
                 server_ready_fn: Callable[[], bool] = lambda: True,
                 server_version: str = "unknown"):
        self.app = app
        self.connection_tracker = connection_tracker
        self.server_ready_fn = server_ready_fn
        self.server_version = server_version

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        is_sse = path == "/sse"
        from starlette.datastructures import Headers
        headers = Headers(scope=scope)

        # === SSE Probe Safeguard ===
        # Prevent agents from hanging by providing ?probe=true to test connectivity
        # Returns immediately with server status instead of starting streaming connection
        if is_sse:
            query_string = scope.get("query_string", b"").decode("utf-8", errors="ignore")
            if "probe=true" in query_string or "probe=1" in query_string:
                server_ready = self.server_ready_fn()
                response_body = json.dumps({
                    "status": "ready" if server_ready else "warming_up",
                    "endpoint": "/sse",
                    "transport": "SSE",
                    "message": "SSE endpoint is available. Remove ?probe to start streaming connection." if server_ready else "Server is warming up, please retry in 2 seconds.",
                    "hint": "Use /health for quick health checks, /sse for MCP client connections",
                    "server_version": self.server_version,
                }).encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": 200 if server_ready else 503,
                    "headers": [[b"content-type", b"application/json"]],
                })
                await send({
                    "type": "http.response.body",
                    "body": response_body,
                })
                return

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

        # Generate base id (stable per SSE connection, unique per HTTP request)
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
            # Legacy paths (SSE, REST) — compute fingerprint and build signals
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
                    transport="sse" if is_sse else "rest",
                )
                set_session_signals(legacy_signals)

        if is_sse:
            client_id = base_id
        else:
            client_id = f"{base_id}:{uuid.uuid4().hex[:8]}"

        # Expose to downstream tool calls via FastMCP Context.request_context.request.state
        try:
            state = scope.setdefault("state", {})
            state["governance_client_id"] = client_id
        except Exception:
            pass

        # Track SSE connections (long-lived); HTTP requests are ephemeral.
        if is_sse:
            await self.connection_tracker.add_connection(client_id, {
                "type": "sse",
                "path": path,
                "user_agent": headers.get("user-agent", "unknown"),
            })

        # PROPAGATE IDENTITY via contextvars for tool handlers
        from src.mcp_handlers.context import set_session_context, reset_session_context
        context_token = set_session_context(
            session_key=client_id,
            client_session_id=headers.get("x-client-id"),
            user_agent=headers.get("user-agent")
        )

        disconnected = False

        connection_tracker_ref = self.connection_tracker

        async def wrapped_receive():
            nonlocal disconnected
            try:
                message = await receive()
                if message.get("type") == "http.disconnect":
                    disconnected = True
                    if is_sse:
                        try:
                            await connection_tracker_ref.remove_connection(client_id)
                        except Exception:
                            pass
                return message
            except Exception as e:
                # Handle receive errors gracefully
                logger.debug(f"Error in wrapped_receive for {client_id}: {e}")
                disconnected = True
                if is_sse:
                    try:
                        await connection_tracker_ref.remove_connection(client_id)
                    except Exception:
                        pass
                raise

        # CRITICAL FIX: Wrap send to handle streaming responses properly
        # SSE responses are streaming, so we need to pass through all messages
        # without interfering with the ASGI protocol
        async def wrapped_send(message):
            try:
                # Pass through all ASGI messages unchanged for SSE streaming
                await send(message)
                # Only update activity after response starts (not on every chunk)
                if is_sse and message.get("type") == "http.response.start":
                    try:
                        await connection_tracker_ref.update_activity(client_id)
                    except Exception:
                        pass  # Don't fail on activity update errors
            except Exception as e:
                # If send fails, mark as disconnected
                logger.debug(f"Error in wrapped_send for {client_id}: {e}")
                disconnected = True
                if is_sse:
                    try:
                        await connection_tracker_ref.remove_connection(client_id)
                    except Exception:
                        pass
                raise

        try:
            return await self.app(scope, wrapped_receive, wrapped_send)
        finally:
            # Reset contextvars to prevent leakage
            if 'context_token' in locals():
                reset_session_context(context_token)

            # Only remove non-SSE connections (HTTP REST endpoints)
            if not is_sse:
                await self.connection_tracker.remove_connection(client_id)
