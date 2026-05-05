"""
MCP Tool Decorators - Auto-registration and utilities

Reduces boilerplate and enables auto-discovery of tools.
"""

from dataclasses import dataclass, field
from typing import Dict, Any, Callable, Optional, Sequence
from functools import wraps
import asyncio
import time
from mcp.types import TextContent

from src.logging_utils import get_logger
from .utils import error_response

logger = get_logger(__name__)


# --- Unified Tool Registry ---

@dataclass
class ToolDefinition:
    """Single source of truth for a registered MCP tool."""
    name: str
    handler: Callable
    timeout: float = 30.0
    description: str = ""
    deprecated: bool = False
    hidden: bool = False
    superseded_by: Optional[str] = None
    rate_limit_exempt: bool = False

_TOOL_DEFINITIONS: Dict[str, ToolDefinition] = {}


def mcp_tool(
    name: Optional[str] = None,
    timeout: float = 30.0,
    description: Optional[str] = None,
    rate_limit_exempt: bool = False,
    deprecated: bool = False,
    hidden: bool = False,
    superseded_by: Optional[str] = None,
    register: bool = True
):
    """
    Decorator for MCP tool handlers with auto-registration and timeout protection.

    Provides:
    - Automatic timeout protection
    - Performance timing/observability (warns if >80% of timeout)
    - Error handling with recovery guidance
    - Tool registration for discovery
    - Deprecation and hiding support

    Usage:
        @mcp_tool("process_agent_update", timeout=60.0)
        async def handle_process_agent_update(arguments: Dict[str, Any]) -> Sequence[TextContent]:
            ...

        @mcp_tool("old_tool", deprecated=True, superseded_by="new_tool")
        async def handle_old_tool(...): ...

        @mcp_tool("internal_helper", register=False)  # Not exposed to MCP clients
        async def handle_internal_helper(...): ...

    Args:
        name: Tool name (defaults to function name without 'handle_' prefix)
        timeout: Timeout in seconds (default: 30.0)
        description: Tool description (defaults to function docstring)
        rate_limit_exempt: If True, skip rate limiting for this tool
        deprecated: If True, tool still works but warns users to use superseded_by
        hidden: If True, tool is not shown in list_tools (internal use only)
        superseded_by: Name of tool that replaces this one (for deprecation messages)
        register: If False, tool is NOT registered (for internal handlers called by consolidated tools)
    """
    def decorator(func: Callable) -> Callable:
        tool_name = name or func.__name__.replace('handle_', '')
        tool_description = description or (func.__doc__ and func.__doc__.strip().split('\n')[0].strip()) or ""

        # Attach metadata to function for introspection
        func._mcp_tool_name = tool_name
        func._mcp_timeout = timeout
        func._mcp_rate_limit_exempt = rate_limit_exempt
        func._mcp_deprecated = deprecated
        func._mcp_hidden = hidden
        func._mcp_superseded_by = superseded_by

        @wraps(func)
        async def wrapper(arguments: Dict[str, Any]):
            start_time = time.time()
            try:
                result = await asyncio.wait_for(func(arguments), timeout=timeout)
                elapsed = time.time() - start_time
                if elapsed > timeout * 0.8:
                    logger.warning(
                        f"Tool '{tool_name}' took {elapsed:.2f}s "
                        f"({elapsed/timeout*100:.1f}% of {timeout}s timeout)"
                    )
                # Normalize: MCP SDK 1.26.0 calls list() on return value.
                # Pydantic v2 models are iterable (yield field tuples), so a
                # bare TextContent would be destructured into invalid tuples,
                # causing 20 CallToolResult validation errors.
                if isinstance(result, TextContent):
                    result = [result]
                return result
            except asyncio.TimeoutError:
                logger.warning(f"Tool '{tool_name}' timed out after {timeout}s")
                # Wave 0 step 2A (RFC roadmap §86): emit coordination_failure
                # via the SYNC audit_logger path. Avoids the anyio task-group
                # conflict (CLAUDE.md "Known Issue") that would deadlock if we
                # awaited asyncpg from inside the wrapper's except clause.
                # emit_coordination_failure_sync is failure-safe by contract —
                # never raises — so it cannot mask the original TimeoutError
                # that the response below reports to the caller.
                from src.coordination_failure_emit import (
                    emit_coordination_failure_sync,
                )
                from src.mcp_handlers.context import (
                    get_context_agent_id,
                    get_context_session_key,
                )

                # Caller agent_id: prefer arguments-supplied (target/explicit) but
                # fall back to the session contextvar so consolidated tools like
                # observe(action=aggregate) — which carry no agent_id arg — still
                # attribute to the bound caller. Without the fallback, ~100% of
                # observed timeouts since 2A merged had agent_id NULL.
                args_agent_id = (
                    arguments.get("agent_id") if isinstance(arguments, dict) else None
                )
                effective_agent_id = args_agent_id or get_context_agent_id()
                session_id = get_context_session_key()

                emit_payload: Dict[str, Any] = {
                    "tool_name": tool_name,
                    "timeout_s": timeout,
                    "elapsed_s": round(time.time() - start_time, 3),
                }
                if isinstance(arguments, dict) and arguments.get("action"):
                    emit_payload["action"] = arguments["action"]

                emit_coordination_failure_sync(
                    service="governance_mcp",
                    event_type="coordination_failure.mcp_handler_timeout.tool_decorator",
                    payload=emit_payload,
                    agent_id=effective_agent_id,
                    session_id=session_id,
                )
                return [error_response(
                    f"Tool '{tool_name}' timed out after {timeout} seconds.",
                    recovery={
                        "action": "Try again with simpler parameters or check system health.",
                        "related_tools": ["health_check"],
                    }
                )]
            except Exception as e:
                logger.error(f"Tool '{tool_name}' error: {e}", exc_info=True)
                return [error_response(
                    f"Error executing tool '{tool_name}': {str(e)}",
                    recovery={
                        "action": "Check tool parameters and try again",
                        "related_tools": ["health_check", "list_tools"],
                    }
                )]

        if register:
            _TOOL_DEFINITIONS[tool_name] = ToolDefinition(
                name=tool_name,
                handler=wrapper,
                timeout=timeout,
                description=tool_description,
                deprecated=deprecated,
                hidden=hidden,
                superseded_by=superseded_by,
                rate_limit_exempt=rate_limit_exempt,
            )

        return wrapper
    return decorator


# --- Backward-Compatible Accessors ---
# These delegate to _TOOL_DEFINITIONS so existing callers don't need to change.

def get_tool_registry() -> Dict[str, Callable]:
    """Get the registered tool handlers."""
    return {name: td.handler for name, td in _TOOL_DEFINITIONS.items()}


def get_tool_timeout(tool_name: str) -> float:
    """Get timeout for a tool."""
    td = _TOOL_DEFINITIONS.get(tool_name)
    return td.timeout if td else 30.0


def get_tool_description(tool_name: str) -> str:
    """Get description for a tool."""
    td = _TOOL_DEFINITIONS.get(tool_name)
    return td.description if td else ""


def get_tool_metadata(tool_name: str) -> Dict[str, Any]:
    """Get metadata for a tool (deprecated, hidden, superseded_by)."""
    td = _TOOL_DEFINITIONS.get(tool_name)
    if not td:
        return {}
    return {
        "deprecated": td.deprecated,
        "hidden": td.hidden,
        "superseded_by": td.superseded_by,
    }


def is_tool_deprecated(tool_name: str) -> bool:
    """Check if a tool is deprecated."""
    td = _TOOL_DEFINITIONS.get(tool_name)
    return td.deprecated if td else False


def is_tool_hidden(tool_name: str) -> bool:
    """Check if a tool is hidden from list_tools."""
    td = _TOOL_DEFINITIONS.get(tool_name)
    return td.hidden if td else False


def get_tool_definition(tool_name: str) -> Optional[ToolDefinition]:
    """Get the full ToolDefinition for a registered tool."""
    return _TOOL_DEFINITIONS.get(tool_name)


def list_registered_tools(include_hidden: bool = False, include_deprecated: bool = True) -> list[str]:
    """List all registered tool names, optionally filtering hidden/deprecated."""
    tools = []
    for name in sorted(_TOOL_DEFINITIONS.keys()):
        td = _TOOL_DEFINITIONS[name]
        if td.hidden and not include_hidden:
            continue
        if td.deprecated and not include_deprecated:
            continue
        tools.append(name)
    return tools


# --- Action Router ---

def action_router(
    name: str,
    actions: Dict[str, Callable],
    *,
    timeout: float = 30.0,
    description: str = "",
    default_action: Optional[str] = None,
    param_maps: Optional[Dict[str, Dict[str, str]]] = None,
    examples: Optional[list] = None,
):
    """
    Create a consolidated MCP tool from an action→handler mapping.

    Replaces repetitive if/elif action routing with a declarative definition.
    The generated handler extracts 'action' from arguments, validates it,
    applies parameter mappings, and delegates to the registered handler.

    Args:
        name: Tool name for MCP registration
        actions: Mapping of action name → async handler function
        timeout: Timeout in seconds
        description: Tool description
        default_action: If set, use this action when 'action' param is missing
        param_maps: Per-action parameter remapping.
            {"search": {"query": "search_query"}} means if action="search"
            and "query" is in arguments but "search_query" is not, copy it.
        examples: Example usage strings for error recovery messages

    Returns:
        The registered handler (same as @mcp_tool would return)
    """
    valid_actions = sorted(actions.keys())
    _param_maps = param_maps or {}
    _examples = examples or [f"{name}(action='{valid_actions[0]}')"]

    @mcp_tool(name, timeout=timeout, description=description)
    async def router(arguments: Dict[str, Any]) -> Sequence[TextContent]:
        # Support both 'action' and 'op' (op is alias for consistency with other tools)
        action = (arguments.get("action") or arguments.get("op") or "").lower() or default_action

        if not action:
            return [error_response(
                "action parameter required",
                recovery={
                    "valid_actions": valid_actions,
                    "examples": _examples,
                }
            )]

        handler = actions.get(action)
        if handler is None:
            return [error_response(
                f"Unknown action: {action}",
                recovery={"valid_actions": valid_actions}
            )]

        # Apply parameter mappings for this action
        for src_key, dst_key in _param_maps.get(action, {}).items():
            if src_key in arguments and arguments.get(src_key) is not None:
                # Fill dst when absent or None; preserve explicit falsy values.
                if dst_key not in arguments or arguments.get(dst_key) is None:
                    arguments[dst_key] = arguments[src_key]

        return await handler(arguments)

    return router
