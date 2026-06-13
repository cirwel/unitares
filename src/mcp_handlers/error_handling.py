"""Error response formatting for MCP handlers."""
from typing import Dict, Any, Tuple, Optional
from mcp.types import TextContent
import json
import re
from datetime import datetime, timezone

from src.logging_utils import get_logger

logger = get_logger(__name__)


def _infer_error_code_and_category(message: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Auto-infer error_code and error_category from message patterns.

    Returns (error_code, error_category) tuple.
    """
    msg_lower = message.lower()

    error_patterns = [
        # Validation errors
        (["not found", "does not exist", "doesn't exist"], "NOT_FOUND", "validation_error"),
        (["missing required", "required parameter", "must provide"], "MISSING_REQUIRED", "validation_error"),
        (["invalid", "must be", "should be"], "INVALID_PARAM", "validation_error"),
        (["already exists", "duplicate"], "ALREADY_EXISTS", "validation_error"),
        (["too long", "exceeds maximum", "too large"], "VALUE_TOO_LARGE", "validation_error"),
        (["too short", "too small", "minimum"], "VALUE_TOO_SMALL", "validation_error"),
        (["empty", "cannot be empty"], "EMPTY_VALUE", "validation_error"),

        # High-priority system errors.
        # Keep these after validation so explicit parameter problems still win,
        # but before broad auth patterns like "session"; otherwise tool names
        # such as list_dialectic_sessions make timeout failures look like auth.
        (["timeout", "timed out"], "TIMEOUT", "system_error"),

        # Auth errors
        (["permission", "not authorized", "forbidden", "access denied"], "PERMISSION_DENIED", "auth_error"),
        (["api key", "apikey"], "API_KEY_ERROR", "auth_error"),
        (["session", "identity not resolved"], "SESSION_ERROR", "auth_error"),

        # State errors
        (["paused", "is paused"], "AGENT_PAUSED", "state_error"),
        (["archived", "is archived"], "AGENT_ARCHIVED", "state_error"),
        (["deleted", "is deleted"], "AGENT_DELETED", "state_error"),
        (["locked", "already locked"], "RESOURCE_LOCKED", "state_error"),

        # System errors
        (["connection", "connect"], "CONNECTION_ERROR", "system_error"),
        (["database", "postgres", "db error"], "DATABASE_ERROR", "system_error"),
        (["failed to", "could not", "unable to"], "OPERATION_FAILED", "system_error"),
    ]

    for patterns, code, category in error_patterns:
        if any(p in msg_lower for p in patterns):
            return code, category

    return None, None


def _sanitize_error_message(message: str) -> str:
    """
    Sanitize error messages to prevent internal structure leakage while preserving actionable context.
    """
    if not isinstance(message, str):
        return str(message)

    # Remove full file paths (but keep filename for context)
    message = re.sub(r'/[^\s]+/([^/\s]+\.py)', r'\1', message)

    # Simplify stack trace line references
    message = re.sub(r', line \d+, in \w+', '', message)
    message = re.sub(r'line \d+', 'line N', message)

    # Remove full stack traces but keep the actual error message
    traceback_match = re.search(r'Traceback[\s\S]*\n(\S[^\n]+)$', message, flags=re.MULTILINE)
    if traceback_match:
        final_error = traceback_match.group(1).strip()
        message = re.sub(r'Traceback[\s\S]*', f'Error: {final_error}', message)

    message = re.sub(r'File "[^"]+", line \d+(?:, in \w+)?', '', message)

    # Clean up module qualifications for internal modules only
    message = re.sub(r'src\.mcp_handlers\.[a-z_]+\.', '', message)
    message = re.sub(r'governance_core\.[a-z_]+\.[a-z_]+\.', 'governance_core.', message)

    # Clean up whitespace
    message = re.sub(r'  +', ' ', message)
    message = re.sub(r'\n\s*\n', '\n', message)

    # Limit length
    from config.governance_config import config
    max_length = config.MAX_ERROR_MESSAGE_LENGTH
    if len(message) > max_length:
        truncated = message[:max_length]
        last_period = truncated.rfind('. ')
        if last_period > max_length * 0.7:
            message = truncated[:last_period + 1]
        else:
            message = truncated.rstrip() + "..."

    return message.strip()


def error_response(
    message: str,
    details: Optional[Dict[str, Any]] = None,
    recovery: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
    error_code: Optional[str] = None,
    error_category: Optional[str] = None,
    arguments: Optional[Dict[str, Any]] = None
) -> TextContent:
    """
    Create an error response with optional recovery guidance and system context.

    SECURITY: Sanitizes error messages to prevent internal structure leakage.
    Auto-infers error_code and error_category if not provided.
    """
    from . import serialization as _ser
    from .support import agent_auth as _auth

    sanitized_message = _sanitize_error_message(message)

    if not error_code or not error_category:
        inferred_code, inferred_category = _infer_error_code_and_category(message)
        if not error_code:
            error_code = inferred_code
        if not error_category:
            error_category = inferred_category

    response = {
        "success": False,
        "error": sanitized_message,
        "server_time": datetime.now(timezone.utc).isoformat()
    }

    if error_code:
        response["error_code"] = error_code

    if error_category:
        if error_category not in ["validation_error", "auth_error", "state_error", "system_error"]:
            logger.warning(f"Unknown error_category '{error_category}', using as-is")
        response["error_category"] = error_category

    if details:
        sanitized_details = {}
        for key, value in details.items():
            if isinstance(value, str):
                sanitized_details[key] = _sanitize_error_message(value)
            else:
                sanitized_details[key] = value
        response.update(sanitized_details)

    if recovery:
        response["recovery"] = recovery

    if context:
        response["context"] = context

    response["agent_signature"] = _auth.compute_agent_signature(arguments=arguments)

    try:
        serializable_response = _ser._make_json_serializable(response)
        json_text = json.dumps(serializable_response, indent=2, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.error(f"JSON serialization error in error_response: {e}", exc_info=True)
        try:
            serializable_response = _ser._make_json_serializable(response)
            json_text = json.dumps(serializable_response, indent=2, ensure_ascii=False, default=str)
        except Exception as e2:
            logger.error(f"Failed to serialize error response even after conversion: {e2}", exc_info=True)
            minimal_response = {
                "success": False,
                "error": sanitized_message,
                "error_code": error_code or "SERIALIZATION_ERROR",
                "server_time": datetime.now(timezone.utc).isoformat()
            }
            json_text = json.dumps(minimal_response, ensure_ascii=False)

    return TextContent(
        type="text",
        text=json_text
    )
