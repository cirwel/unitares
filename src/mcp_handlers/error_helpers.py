"""
Enhanced error handling utilities for MCP handlers.

Standardizes error responses with recovery guidance and context.
"""

from typing import Dict, Any, Optional, Sequence
from mcp.types import TextContent
from .utils import error_response


# Standard recovery patterns for common error types
# Updated Dec 2025: API keys deprecated, UUID-based identity is now primary
RECOVERY_PATTERNS = {
    "agent_not_found": {
        "action": "Call any tool to auto-create identity, then use identity() to name yourself",
        "related_tools": ["identity", "agent"],
        "workflow": [
            "1. Call process_agent_update() or any tool - identity auto-creates",
            "2. Call identity(name='your_name') to set your display name",
            "3. Then call this tool again"
        ]
    },
    "agent_not_registered": {
        "action": "Call any tool to auto-create identity, then use identity() to name yourself",
        "related_tools": ["identity", "agent"],
        "workflow": [
            "1. Call process_agent_update() or any tool - identity auto-creates",
            "2. Call identity(name='your_name') to set your display name",
            "3. Then call this tool again"
        ]
    },
    "authentication_failed": {
        "action": "Identity should auto-bind on first tool call",
        "related_tools": ["identity", "health_check"],
        "workflow": [
            "1. Call identity() to check your current binding",
            "2. If unbound, call any tool - identity auto-creates",
            "3. Retry your original request"
        ]
    },
    "authentication_required": {
        "action": "Identity auto-binds on first tool call",
        "related_tools": ["identity", "process_agent_update"],
        "workflow": [
            "1. Call any tool - identity auto-binds from session",
            "2. Use identity(name='your_name') to set your display name",
            "3. Retry your original request"
        ]
    },
    "ownership_required": {
        "action": "You can only modify your own resources",
        "related_tools": ["identity", "agent"],
        "workflow": [
            "1. Call identity() to verify your bound identity",
            "2. Ensure the resource belongs to your agent_uuid",
            "3. You cannot modify resources owned by other agents"
        ]
    },
    "rate_limit_exceeded": {
        "action": "Wait a few seconds before retrying",
        "related_tools": ["health_check"],
        "workflow": [
            "1. Wait 10-30 seconds",
            "2. Retry request",
            "3. If persistent, check system health"
        ]
    },
    "timeout": {
        "action": "This may indicate a blocking operation or system overload. Try again with simpler parameters.",
        "related_tools": ["health_check", "admin"],
        "workflow": [
            "1. Wait a few seconds and retry",
            "2. Check system health with health_check",
            "3. Simplify request parameters",
            "4. Check for system overload"
        ]
    },
    "invalid_parameters": {
        "action": "Check tool parameters and try again",
        "related_tools": ["list_tools", "health_check"],
        "workflow": [
            "1. Verify tool parameters match schema",
            "2. Check tool description with list_tools",
            "3. Retry with correct parameters"
        ]
    },
    "validation_error": {
        "action": "Check parameter format and constraints",
        "related_tools": ["list_tools"],
        "workflow": [
            "1. Review parameter requirements",
            "2. Check tool schema with list_tools",
            "3. Retry with valid parameters"
        ]
    },
    "system_error": {
        "action": "Check system health and retry",
        "related_tools": ["health_check", "admin"],
        "workflow": [
            "1. Check system health",
            "2. Wait a few seconds",
            "3. Retry request"
        ]
    },
    "resource_not_found": {
        "action": "Verify the resource ID exists",
        "related_tools": ["agent", "search_knowledge_graph"],
        "workflow": [
            "1. Check if resource exists",
            "2. Verify resource ID format",
            "3. Use search/list tools to find correct ID"
        ]
    },
    "not_connected": {
        "action": "Check MCP server connection status",
        "related_tools": ["admin", "health_check"],
        "workflow": [
            "1. Call admin(action='connections') to verify connection",
            "2. Check if MCP server is running",
            "3. Verify MCP configuration in client settings",
            "4. Retry your request"
        ]
    },
    "missing_client_session_id": {
        "action": "Provide active session continuity metadata or re-onboard explicitly",
        "related_tools": ["identity", "onboard"],
        "workflow": [
            "1. Call identity() to inspect the current binding",
            "2. Include continuity_token for a proof-owned UUID rebind when available",
            "3. For a fresh process, call onboard(force_new=true) and use parent_agent_id for lineage"
        ]
    },
    "session_mismatch": {
        "action": "Verify your session identity matches",
        "related_tools": ["identity", "admin"],
        "workflow": [
            "1. Call identity() to check your resolved identity",
            "2. Ensure client_session_id or continuity_token matches your active session",
            "3. If mismatch persists, call onboard(force_new=true) and declare parent_agent_id when inheriting work",
            "4. Retry your request with the resolved binding"
        ]
    },
    "missing_parameter": {
        "action": "Include the missing required parameter",
        "related_tools": ["describe_tool", "list_tools"],
        "workflow": [
            "1. Check tool description with describe_tool(tool_name=...)",
            "2. Add the missing parameter to your call",
            "3. Retry your request"
        ]
    },
    "invalid_parameter_type": {
        "action": "Check parameter type and format",
        "related_tools": ["describe_tool"],
        "workflow": [
            "1. Check parameter type with describe_tool(tool_name=...)",
            "2. Ensure parameter matches expected type (string, number, array, etc.)",
            "3. Retry with correct type"
        ]
    },
    "permission_denied": {
        "action": "Verify you have required permissions",
        "related_tools": ["identity", "get_governance_metrics"],
        "workflow": [
            "1. Call identity() to verify your identity",
            "2. Check if operation requires specific permissions",
            "3. Some operations require registered agent (call onboard() first)",
            "4. Retry after verifying permissions"
        ]
    }
}


def agent_not_found_error(
    agent_id: str, 
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "AGENT_NOT_FOUND"
) -> Sequence[TextContent]:
    """Standard error for agent not found"""
    return [error_response(
        f"Agent '{agent_id}' not found",
        error_code=error_code,
        error_category="validation_error",
        details={"error_type": "agent_not_found", "agent_id": agent_id},
        recovery=RECOVERY_PATTERNS["agent_not_found"],
        context=context or {}
    )]


def agent_not_registered_error(
    agent_id: str,
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "AGENT_NOT_REGISTERED"
) -> Sequence[TextContent]:
    """Standard error for agent not registered"""
    return [error_response(
        f"Agent '{agent_id}' is not registered. You must onboard first.",
        error_code=error_code,
        error_category="validation_error",
        details={"error_type": "agent_not_registered", "agent_id": agent_id},
        recovery=RECOVERY_PATTERNS["agent_not_registered"],
        context=context or {}
    )]


def authentication_error(
    message: str = "Authentication failed",
    agent_id: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "AUTHENTICATION_FAILED"
) -> Sequence[TextContent]:
    """Standard error for authentication failure"""
    if agent_id:
        message = f"Authentication failed for agent '{agent_id}'. Invalid API key."
        details = {"error_type": "authentication_failed", "agent_id": agent_id}
    else:
        details = {"error_type": "authentication_failed"}
    
    return [error_response(
        message,
        error_code=error_code,
        error_category="auth_error",
        details=details,
        recovery=RECOVERY_PATTERNS["authentication_failed"],
        context=context or {}
    )]


def authentication_required_error(
    operation: str = "this operation",
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "AUTHENTICATION_REQUIRED"
) -> Sequence[TextContent]:
    """Standard error for missing authentication"""
    return [error_response(
        f"API key required for {operation}. Authentication required to prevent unauthorized access.",
        error_code=error_code,
        error_category="auth_error",
        details={"error_type": "authentication_required", "operation": operation},
        recovery=RECOVERY_PATTERNS["authentication_required"],
        context=context or {}
    )]


def ownership_error(
    resource_type: str,
    resource_id: str,
    owner_agent_id: str,
    caller_agent_id: str,
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "OWNERSHIP_VIOLATION"
) -> Sequence[TextContent]:
    """Standard error for ownership violation"""
    return [error_response(
        f"Unauthorized: Agent '{caller_agent_id}' cannot modify {resource_type} '{resource_id}' owned by '{owner_agent_id}'.",
        error_code=error_code,
        error_category="auth_error",
        details={
            "error_type": "ownership_violation",
            "resource_type": resource_type,
            "resource_id": resource_id,
            "owner_agent_id": owner_agent_id,
            "caller_agent_id": caller_agent_id
        },
        recovery=RECOVERY_PATTERNS["ownership_required"],
        context=context or {}
    )]


def rate_limit_error(agent_id: str, stats: Optional[Dict[str, Any]] = None) -> Sequence[TextContent]:
    """Standard error for rate limit exceeded"""
    return [error_response(
        f"Rate limit exceeded for agent '{agent_id}'",
        error_code="RATE_LIMIT_EXCEEDED",
        error_category="validation_error",
        details={"error_type": "rate_limit_exceeded", "agent_id": agent_id},
        recovery=RECOVERY_PATTERNS["rate_limit_exceeded"],
        context={"rate_limit_stats": stats} if stats else {}
    )]


def timeout_error(tool_name: str, timeout: float) -> Sequence[TextContent]:
    """Standard error for timeout"""
    return [error_response(
        f"Tool '{tool_name}' timed out after {timeout} seconds.",
        error_code="TIMEOUT",
        error_category="system_error",
        details={"error_type": "timeout", "tool_name": tool_name, "timeout_seconds": timeout},
        recovery=RECOVERY_PATTERNS["timeout"],
        context={"tool_name": tool_name, "timeout_seconds": timeout}
    )]


def invalid_parameters_error(
    tool_name: str, 
    details: Optional[str] = None,
    param_name: Optional[str] = None
) -> Sequence[TextContent]:
    """Standard error for invalid parameters"""
    message = f"Invalid parameters for tool '{tool_name}'"
    if details:
        message += f": {details}"
    
    error_details = {"error_type": "invalid_parameters", "tool_name": tool_name}
    if param_name:
        error_details["param_name"] = param_name
    
    return [error_response(
        message,
        error_code="INVALID_PARAMETERS",
        error_category="validation_error",
        details=error_details,
        recovery=RECOVERY_PATTERNS["invalid_parameters"],
        context={"tool_name": tool_name, "details": details, "param_name": param_name}
    )]


def validation_error(
    message: str,
    param_name: Optional[str] = None,
    provided_value: Any = None,
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "VALIDATION_ERROR"
) -> Sequence[TextContent]:
    """Standard error for validation failures"""
    details = {"error_type": "validation_error"}
    if param_name:
        details["param_name"] = param_name
    if provided_value is not None:
        details["provided_value"] = str(provided_value)
    
    return [error_response(
        message,
        error_code=error_code,
        error_category="validation_error",
        details=details,
        recovery=RECOVERY_PATTERNS["validation_error"],
        context=context or {}
    )]


def resource_not_found_error(
    resource_type: str,
    resource_id: str,
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "RESOURCE_NOT_FOUND"
) -> Sequence[TextContent]:
    """Standard error for resource not found"""
    return [error_response(
        f"{resource_type.capitalize()} '{resource_id}' not found",
        error_code=error_code,
        error_category="validation_error",
        details={"error_type": "resource_not_found", "resource_type": resource_type, "resource_id": resource_id},
        recovery=RECOVERY_PATTERNS["resource_not_found"],
        context=context or {}
    )]


def system_error(
    tool_name: str,
    error: Exception,
    context: Optional[Dict[str, Any]] = None
) -> Sequence[TextContent]:
    """Standard error for system errors"""
    return [error_response(
        f"System error executing tool '{tool_name}': {str(error)}",
        error_code="SYSTEM_ERROR",
        error_category="system_error",
        details={"error_type": "system_error", "tool_name": tool_name, "exception_type": type(error).__name__},
        recovery=RECOVERY_PATTERNS["system_error"],
        context=context or {}
    )]


def not_connected_error(
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "NOT_CONNECTED"
) -> Sequence[TextContent]:
    """Standard error for MCP connection issues"""
    return [error_response(
        "MCP server connection not available",
        error_code=error_code,
        error_category="system_error",
        details={"error_type": "not_connected"},
        recovery=RECOVERY_PATTERNS["not_connected"],
        context=context or {}
    )]


def missing_client_session_id_error(
    operation: str = "this operation",
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "MISSING_CLIENT_SESSION_ID"
) -> Sequence[TextContent]:
    """Standard error for missing client_session_id"""
    return [error_response(
        f"client_session_id required for {operation}",
        error_code=error_code,
        error_category="validation_error",
        details={"error_type": "missing_client_session_id", "operation": operation},
        recovery=RECOVERY_PATTERNS["missing_client_session_id"],
        context=context or {}
    )]


def session_mismatch_error(
    expected_id: str,
    provided_id: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "SESSION_MISMATCH"
) -> Sequence[TextContent]:
    """Standard error for session identity mismatch"""
    message = f"Session identity mismatch. Expected: {expected_id[:8]}..."
    if provided_id:
        message += f", Provided: {provided_id[:8]}..."
    
    return [error_response(
        message,
        error_code=error_code,
        error_category="auth_error",
        details={
            "error_type": "session_mismatch",
            "expected_resolved_id": expected_id,
            "provided_id": provided_id
        },
        recovery=RECOVERY_PATTERNS["session_mismatch"],
        context=context or {}
    )]


def missing_parameter_error(
    parameter_name: str,
    tool_name: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "MISSING_PARAMETER"
) -> Sequence[TextContent]:
    """Standard error for missing required parameter"""
    message = f"Missing required parameter: '{parameter_name}'"
    if tool_name:
        message += f" for tool '{tool_name}'"
    
    # Enhance message with custom guidance if provided
    if context and "custom_message" in context:
        message += f". {context['custom_message']}"
    
    # Add examples for common tools
    examples = {}
    if tool_name == "leave_note":
        examples = {
            "example": 'leave_note(summary="Your note here")',
            "aliases": "You can also use: 'note', 'text', 'content', 'message', 'insight', 'finding', 'learning'",
            "quick_fix": "Add summary parameter: leave_note(summary='Your note text')"
        }
    elif tool_name == "store_knowledge_graph":
        examples = {
            "example": 'store_knowledge_graph(summary="Discovery description", tags=["tag1"])',
            "quick_fix": "Add summary parameter: store_knowledge_graph(summary='Your discovery')"
        }
    
    details = {
        "error_type": "missing_parameter",
        "parameter": parameter_name,
        "tool_name": tool_name
    }
    if examples:
        details["examples"] = examples
    
    return [error_response(
        message,
        error_code=error_code,
        error_category="validation_error",
        details=details,
        recovery=RECOVERY_PATTERNS["missing_parameter"],
        context=context or {}
    )]


def invalid_parameter_type_error(
    parameter_name: str,
    expected_type: str,
    provided_type: str,
    tool_name: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "INVALID_PARAMETER_TYPE"
) -> Sequence[TextContent]:
    """Standard error for invalid parameter type"""
    message = f"Parameter '{parameter_name}' must be {expected_type}, got {provided_type}"
    if tool_name:
        message += f" for tool '{tool_name}'"
    
    return [error_response(
        message,
        error_code=error_code,
        error_category="validation_error",
        details={
            "error_type": "invalid_parameter_type",
            "parameter": parameter_name,
            "expected_type": expected_type,
            "provided_type": provided_type,
            "tool_name": tool_name
        },
        recovery=RECOVERY_PATTERNS["invalid_parameter_type"],
        context=context or {}
    )]


def permission_denied_error(
    operation: str,
    required_role: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "PERMISSION_DENIED"
) -> Sequence[TextContent]:
    """Standard error for permission denied"""
    message = f"Permission denied for {operation}"
    if required_role:
        message += f". Required role: {required_role}"
    
    return [error_response(
        message,
        error_code=error_code,
        error_category="auth_error",
        details={
            "error_type": "permission_denied",
            "operation": operation,
            "required_role": required_role
        },
        recovery=RECOVERY_PATTERNS["permission_denied"],
        context=context or {}
    )]


def tool_not_found_error(
    tool_name: str,
    available_tools: list,
    context: Optional[Dict[str, Any]] = None
) -> Sequence[TextContent]:
    """
    Elegant error for unknown tool with fuzzy suggestions.

    Uses difflib to find similar tool names and provides helpful recovery.
    """
    import difflib

    # Find similar tool names (fuzzy match)
    similar = difflib.get_close_matches(tool_name, available_tools, n=3, cutoff=0.4)

    # Build helpful message
    if similar:
        suggestions_str = ", ".join(f"'{s}'" for s in similar)
        message = f"Tool '{tool_name}' not found. Did you mean: {suggestions_str}?"
    else:
        message = f"Tool '{tool_name}' not found."

    # Categorize tools for discovery
    common_tools = [t for t in available_tools if t in {
        'process_agent_update', 'status', 'search_knowledge_graph',
        'list_agents', 'health_check', 'list_tools', 'describe_tool'
    }]

    return [error_response(
        message,
        error_code="TOOL_NOT_FOUND",
        error_category="validation_error",
        details={
            "error_type": "tool_not_found",
            "requested_tool": tool_name,
            "similar_tools": similar,
            "total_available": len(available_tools)
        },
        recovery={
            "action": "Use list_tools to see all available tools, or try a suggested alternative",
            "related_tools": ["list_tools", "health_check"] + similar[:2],
            "workflow": [
                "1. Check the tool name spelling",
                f"2. Try one of the suggested alternatives: {similar}" if similar else "2. Use list_tools to browse available tools",
                "3. Use describe_tool(tool_name) to see tool details"
            ],
            "suggestions": similar,
            "common_tools": common_tools[:5]
        },
        context=context or {}
    )]
