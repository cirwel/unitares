"""GovernanceMCPClient — httpx async client with circuit breaker for JSON-RPC to governance."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

import httpx

from .constants import (
    GOVERNANCE_URL,
    REQUEST_TIMEOUT,
    CIRCUIT_THRESHOLD,
    CIRCUIT_BACKOFF_BASE,
    CIRCUIT_BACKOFF_MAX,
)

logger = logging.getLogger("gateway.client")

_HEADERS_BASE = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


class GovernanceMCPClient:
    """Async client that proxies tool calls to the full governance MCP server via JSON-RPC."""

    def __init__(
        self,
        url: str = GOVERNANCE_URL,
        timeout: float = REQUEST_TIMEOUT,
        session_id: Optional[str] = None,
    ):
        self._url = url
        self._timeout = timeout
        self._external_session_id = session_id  # For X-Session-ID forwarding

        # MCP session state (lazy-initialized)
        self._mcp_session_id: str | None = None
        self._initialized = False

        # Circuit breaker state
        self._circuit_failures = 0
        self._circuit_open_until = 0.0
        self._circuit_threshold = CIRCUIT_THRESHOLD
        self._circuit_backoff_base = CIRCUIT_BACKOFF_BASE
        self._circuit_backoff_max = CIRCUIT_BACKOFF_MAX
        self._circuit_current_backoff = CIRCUIT_BACKOFF_BASE

        self._request_id = 0

    # -- Circuit breaker --

    def _is_circuit_open(self) -> bool:
        """Return True if circuit breaker is open (should skip requests)."""
        if self._circuit_failures < self._circuit_threshold:
            return False
        now = time.monotonic()
        if now < self._circuit_open_until:
            return True
        # Half-open: allow one attempt
        return False

    def _record_success(self) -> None:
        """Reset circuit breaker on success."""
        self._circuit_failures = 0
        self._circuit_current_backoff = self._circuit_backoff_base

    def _record_failure(self) -> None:
        """Increment failure count and maybe open circuit."""
        self._circuit_failures += 1
        if self._circuit_failures >= self._circuit_threshold:
            self._circuit_open_until = time.monotonic() + self._circuit_current_backoff
            logger.warning(
                "Circuit breaker open for %.0fs (%d consecutive failures)",
                self._circuit_current_backoff,
                self._circuit_failures,
            )
            self._circuit_current_backoff = min(
                self._circuit_current_backoff * 2, self._circuit_backoff_max
            )

    # -- MCP session management --

    def _build_headers(self) -> dict[str, str]:
        """Build request headers including MCP session ID if available."""
        headers = dict(_HEADERS_BASE)
        if self._mcp_session_id:
            headers["Mcp-Session-Id"] = self._mcp_session_id
        if self._external_session_id:
            headers["X-Session-ID"] = self._external_session_id
        # The gateway proxies to the full server's /mcp. If that gate is
        # configured, the proxy needs the bearer like any other client;
        # unset, no header is sent and nothing changes.
        bearer = os.environ.get("UNITARES_MCP_BEARER_TOKEN") or None
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return headers

    async def _ensure_initialized(self, http: httpx.AsyncClient) -> None:
        """Lazy MCP session initialization — called before first tool call."""
        if self._initialized:
            return

        self._request_id += 1
        init_payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "unitares-gateway", "version": "1.0.0"},
            },
        }

        resp = await http.post(self._url, json=init_payload, headers=self._build_headers())
        resp.raise_for_status()

        # Capture MCP session ID from response header
        mcp_sid = resp.headers.get("mcp-session-id")
        if mcp_sid:
            self._mcp_session_id = mcp_sid
            logger.info("MCP session established: %s", mcp_sid[:16])

        self._initialized = True

    # -- MCP response parsing --

    @staticmethod
    def parse_mcp_result(response_json: dict) -> Any:
        """Extract the actual data from an MCP JSON-RPC response.

        MCP wraps tool results: result["result"]["content"][0]["text"] is a JSON string.
        We unwrap that to return the actual data dict.
        """
        if "error" in response_json:
            error = response_json["error"]
            msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            raise MCPError(msg)

        result = response_json.get("result", response_json)

        # MCP content wrapper: content[0]["text"] contains JSON string
        if isinstance(result, dict) and "content" in result and result["content"]:
            content = result["content"]
            if isinstance(content, list) and len(content) > 0:
                first = content[0]
                if isinstance(first, dict) and first.get("type") == "text" and first.get("text"):
                    try:
                        return json.loads(first["text"])
                    except (json.JSONDecodeError, TypeError):
                        return first["text"]

        return result

    # -- Core call method --

    async def call_tool(self, tool_name: str, arguments: dict | None = None) -> Any:
        """Call a governance MCP tool via JSON-RPC POST.

        Returns the parsed result data (unwrapped from MCP envelope).
        Raises MCPError on MCP-level errors, CircuitOpenError if breaker is open.
        """
        if self._is_circuit_open():
            raise CircuitOpenError(
                f"Circuit breaker open (retry in {self._circuit_open_until - time.monotonic():.0f}s)"
            )

        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments or {},
            },
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                # Lazy MCP session init
                await self._ensure_initialized(http)

                resp = await http.post(self._url, json=payload, headers=self._build_headers())
                resp.raise_for_status()

                # Handle SSE vs JSON response
                content_type = resp.headers.get("content-type", "")
                if "text/event-stream" in content_type:
                    response_json = self._parse_sse(resp.text)
                else:
                    response_json = resp.json()

                if response_json is None:
                    raise MCPError("Empty response from governance server")

                result = self.parse_mcp_result(response_json)
                self._record_success()
                return result

        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            self._record_failure()
            self._initialized = False  # Force re-init on next attempt
            raise ConnectionError(f"Cannot reach governance server: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            # Session expired? Force re-init
            if exc.response.status_code in (400, 404):
                self._initialized = False
                self._mcp_session_id = None
            self._record_failure()
            raise MCPError(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}") from exc

    @staticmethod
    def _parse_sse(text: str) -> dict | None:
        """Parse SSE response to extract JSON data."""
        for line in text.split("\n"):
            if line.startswith("data: "):
                try:
                    return json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
        return None

    async def list_tools(self) -> list[dict]:
        """List available tools on the governance server."""
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/list",
            "params": {},
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                await self._ensure_initialized(http)
                resp = await http.post(
                    self._url, json=payload, headers=self._build_headers(),
                )
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if "text/event-stream" in content_type:
                    data = self._parse_sse(resp.text)
                else:
                    data = resp.json()
                return (data or {}).get("result", {}).get("tools", [])
        except Exception as exc:
            logger.warning("Failed to list governance tools: %s", exc)
            return []


class MCPError(Exception):
    """Error from the MCP governance server."""


class CircuitOpenError(Exception):
    """Circuit breaker is open — governance server unavailable."""
