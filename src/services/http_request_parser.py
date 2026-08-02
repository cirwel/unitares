"""Request parsing and client-context inference for the REST tool endpoint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping


MAX_HTTP_TOOL_REQUEST_SIZE = 10 * 1024 * 1024
MAX_HTTP_TOOL_ARGUMENTS = 100


@dataclass(frozen=True)
class ParsedHttpToolRequest:
    body: dict[str, Any]
    tool_name: str
    arguments: MutableMapping[str, Any]


class HttpToolRequestError(ValueError):
    """Validation failure carrying the exact REST response contract."""

    def __init__(self, payload: dict[str, Any], *, status_code: int = 400):
        super().__init__(str(payload.get("error") or "Invalid request"))
        self.payload = payload
        self.status_code = status_code


def normalize_http_tool_name(body: Mapping[str, Any], mcp_server_name: str) -> str:
    """Resolve HTTP tool aliases to the canonical dispatch name."""
    tool_name = body.get("name") or body.get("tool_name") or "unknown"
    if not isinstance(tool_name, str) or not tool_name or tool_name == "unknown":
        return "unknown"
    mcp_prefix = f"mcp_{mcp_server_name}_"
    return (
        tool_name[len(mcp_prefix) :] if tool_name.startswith(mcp_prefix) else tool_name
    )


def validate_http_tool_content_length(content_length: str | None) -> None:
    """Reject an explicitly oversized body before JSON parsing."""
    if not content_length:
        return
    try:
        size = int(content_length)
    except ValueError:
        return
    if size > MAX_HTTP_TOOL_REQUEST_SIZE:
        raise HttpToolRequestError(
            {
                "success": False,
                "error": "Request body too large",
                "max_size_mb": MAX_HTTP_TOOL_REQUEST_SIZE // (1024 * 1024),
            },
            status_code=413,
        )


def parse_http_tool_request(
    body: Any,
    *,
    mcp_server_name: str,
) -> ParsedHttpToolRequest:
    """Validate the decoded JSON body and return normalized dispatch inputs."""
    if not isinstance(body, dict):
        raise HttpToolRequestError(
            {"success": False, "error": "Request body must be a JSON object"}
        )

    arguments = body.get("arguments", {})
    if not isinstance(arguments, dict):
        raise HttpToolRequestError(
            {"success": False, "error": "'arguments' must be a JSON object"}
        )
    if len(arguments) > MAX_HTTP_TOOL_ARGUMENTS:
        raise HttpToolRequestError(
            {
                "success": False,
                "error": "Too many arguments",
                "max_arguments": MAX_HTTP_TOOL_ARGUMENTS,
            }
        )

    tool_name = normalize_http_tool_name(body, mcp_server_name)
    if tool_name == "unknown":
        raise HttpToolRequestError(
            {
                "success": False,
                "error": (
                    "Missing 'name' field — pass the tool name as 'name', e.g. "
                    '{"name": "onboard", "arguments": {...}}'
                ),
            }
        )
    if len(tool_name) > 100:
        raise HttpToolRequestError(
            {"success": False, "error": "Invalid tool name format"}
        )
    return ParsedHttpToolRequest(body=body, tool_name=tool_name, arguments=arguments)


def deprecated_http_tool_payload(tool_name: str) -> dict[str, Any] | None:
    """Return the compatibility response for retired SSE-only tools."""
    messages = {
        "get_connected_clients": (
            "Tool deprecated. SSE transport deprecated by MCP. Use Streamable HTTP."
        ),
        "get_connection_diagnostics": (
            "Tool deprecated. SSE transport deprecated by MCP. Use Streamable HTTP."
        ),
    }
    if tool_name not in messages:
        return None
    return {
        "name": tool_name,
        "result": {"error": messages[tool_name]},
        "success": False,
    }


def enrich_http_client_context(
    headers: Mapping[str, str],
    arguments: MutableMapping[str, Any],
) -> tuple[str | None, str | None]:
    """Infer client/model hints without mutating caller-supplied values."""
    user_agent = (headers.get("user-agent") or "").lower()
    detected_client = None
    detected_model = None

    if "client_hint" not in arguments:
        from src.mcp_handlers.context import detect_client_from_user_agent

        detected_client = detect_client_from_user_agent(user_agent)
        if detected_client:
            arguments["client_hint"] = detected_client

    if "model_type" not in arguments:
        model_header = headers.get("x-model") or headers.get("X-Model")
        detected_model = model_header.strip().lower() if model_header else None
        if not detected_model:
            patterns = (
                ("gpt-5.3", "gpt-5.3-codex", "codex"),
                ("gpt-5.4", "gpt-5.4-codex", "codex"),
                ("gpt-5", "gpt-5-codex", "codex"),
                ("composer", "composer", None),
                ("codex", "codex", None),
                ("chatgpt", "gpt", None),
                ("openai", "gpt", None),
                ("gpt-5", "gpt", None),
                ("gpt-4", "gpt", None),
                ("gpt-3", "gpt", None),
                ("claude", "claude", None),
                ("gemini", "gemini", None),
            )
            for marker, value, required_marker in patterns:
                if marker not in user_agent:
                    continue
                if required_marker and required_marker not in user_agent:
                    continue
                if value == "claude" and any(
                    marker in user_agent for marker in ("codex", "gpt", "openai")
                ):
                    continue
                detected_model = value
                break
        if detected_model:
            arguments["model_type"] = detected_model

    return detected_client, detected_model
