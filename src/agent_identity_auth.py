"""
Agent identity validation and authentication.

Format validation, API key generation, ownership verification, auth enforcement.
"""

from __future__ import annotations

import os
import json
import secrets
import base64
from datetime import datetime

from src.logging_utils import get_logger
from src.agent_metadata_model import agent_metadata, TextContent

logger = get_logger(__name__)


def check_agent_status(agent_id: str) -> str | None:
    """Check if agent status allows operations, return error if not"""
    if agent_id in agent_metadata:
        meta = agent_metadata[agent_id]
        if meta.status == "paused":
            return f"Agent '{agent_id}' is paused. Resume it first before processing updates."
        elif meta.status == "archived":
            return f"Agent '{agent_id}' is archived. It must be restored before processing updates."
        elif meta.status == "deleted":
            return f"Agent '{agent_id}' is deleted and cannot be used."
    return None


def check_agent_id_default(agent_id: str) -> str | None:
    """Check if using default agent_id and return warning if so"""
    if not agent_id or agent_id == "default_agent":
        return "⚠️ Using default agent_id. For multi-agent systems, specify explicit agent_id to avoid state mixing."
    return None


def _detect_ci_status() -> bool:
    """
    Auto-detect CI pass status from environment variables.

    Checks common CI environment variables:
    - CI=true + CI_STATUS=passed (custom)
    - GITHUB_ACTIONS + GITHUB_WORKFLOW_STATUS=success (GitHub Actions)
    - TRAVIS=true + TRAVIS_TEST_RESULT=0 (Travis CI)
    - CIRCLE_CI=true + CIRCLE_BUILD_STATUS=success (CircleCI)
    - GITLAB_CI=true + CI_JOB_STATUS=success (GitLab CI)

    Returns:
        bool: True if CI passed, False otherwise (conservative default)
    """
    ci_env = os.environ.get("CI", "").lower()
    if ci_env not in ("true", "1", "yes"):
        return False

    ci_status = os.environ.get("CI_STATUS", "").lower()
    if ci_status in ("passed", "success", "ok", "true", "1"):
        return True

    if os.environ.get("GITHUB_ACTIONS") == "true":
        workflow_status = os.environ.get("GITHUB_WORKFLOW_STATUS", "").lower()
        if workflow_status == "success":
            return True

    if os.environ.get("TRAVIS") == "true":
        test_result = os.environ.get("TRAVIS_TEST_RESULT", "")
        if test_result == "0":
            return True

    if os.environ.get("CIRCLE_CI") == "true":
        build_status = os.environ.get("CIRCLE_BUILD_STATUS", "").lower()
        if build_status == "success":
            return True

    if os.environ.get("GITLAB_CI") == "true":
        job_status = os.environ.get("CI_JOB_STATUS", "").lower()
        if job_status == "success":
            return True

    return False


def validate_agent_id_format(agent_id: str) -> tuple[bool, str, str]:
    """
    Validate agent_id follows recommended patterns.

    Returns:
        (is_valid, error_message, suggestion)
    """
    import re

    generic_ids = {
        "test", "demo", "default_agent", "agent", "monitor"
    }

    if agent_id.lower() in generic_ids:
        suggestion = f"{agent_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        return False, f"Generic ID '{agent_id}' is not allowed. Use a specific identifier.", suggestion

    if agent_id in ["claude_code_cli", "claude_chat", "composer", "cursor_ide"]:
        suggestion = f"{agent_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        return False, f"ID '{agent_id}' is too generic and may cause collisions. Add a session identifier.", suggestion

    if agent_id.startswith("test_") and len(agent_id.split("_")) < 3:
        suggestion = f"test_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        return False, f"Test IDs should include timestamp for uniqueness (e.g., 'test_20251124_143022').", suggestion

    if agent_id.startswith("demo_") and len(agent_id.split("_")) < 3:
        suggestion = f"demo_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        return False, f"Demo IDs should include timestamp for uniqueness (e.g., 'demo_20251124_143022').", suggestion

    if len(agent_id) < 3:
        return False, f"Agent ID '{agent_id}' is too short. Use at least 3 characters.", ""

    if not re.match(r'^[a-zA-Z0-9_-]+$', agent_id):
        return False, f"Agent ID '{agent_id}' contains invalid characters. Use only letters, numbers, underscores, and hyphens.", ""

    return True, "", ""


def require_explicit_agent_id(arguments: dict, reject_existing: bool = False) -> tuple[str | None, TextContent | None]:
    """
    Require explicit agent_id, validate format, return error if missing or invalid.

    NOTE (council 2026-06-24): this is the legacy explicit-only validator (no
    session-binding, no alias canonicalization, no strict-mode FALLBACK). It was
    renamed from ``require_agent_id`` to remove a name collision with the
    load-bearing write-gate resolver ``mcp_handlers.support.agent_auth.require_agent_id``
    (the canonical 1→2→3 resolution-order resolver). For session-bound caller
    identity, prefer that one; this validator only checks a literally-supplied
    ``agent_id`` string.

    Args:
        arguments: Tool arguments dict containing 'agent_id'
        reject_existing: If True, reject agent_ids that already exist (for new agent creation).

    Returns:
        (agent_id, None) if valid, (None, TextContent error) if invalid
    """
    agent_id = arguments.get("agent_id")
    if not agent_id:
        error_msg = json.dumps({
            "success": False,
            "error": "agent_id is required. Each agent must have a UNIQUE identifier to prevent state mixing.",
            "details": "Use a unique session/purpose identifier (e.g., 'cursor_ide_session_001', 'claude_code_cli_20251124', 'debugging_session_20251124').",
            "why_unique": "Each agent_id is a unique identity. Using another agent's ID is identity theft - you would impersonate them, corrupt their history, and erase their governance record.",
            "examples": [
                "cursor_ide_session_001",
                "claude_code_cli_20251124",
                "debugging_session_20251124",
                "production_agent_v2"
            ],
            "suggestion": "\"agent_id\": \"your_unique_session_id\"",
            "recovery": {
                "action": "Provide a unique agent_id in your request",
                "related_tools": ["get_agent_api_key", "list_agents"],
                "workflow": "1. Generate unique agent_id (e.g., timestamp-based) 2. Call get_agent_api_key to get/create agent 3. Use agent_id and api_key in subsequent calls"
            }
        }, indent=2)
        return None, TextContent(type="text", text=error_msg)

    if reject_existing and agent_id in agent_metadata:
        existing_meta = agent_metadata[agent_id]
        try:
            created_dt = datetime.fromisoformat(existing_meta.created_at.replace('Z', '+00:00') if 'Z' in existing_meta.created_at else existing_meta.created_at)
            created_str = created_dt.strftime('%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError, AttributeError):
            created_str = existing_meta.created_at

        error_msg = json.dumps({
            "success": False,
            "error": "Identity collision: This agent_id already exists",
            "details": f"'{agent_id}' is an existing agent identity (created {created_str}, {existing_meta.total_updates} updates)",
            "why_this_matters": "Using another agent's ID is identity theft. You would impersonate them and corrupt their governance history.",
            "suggestion": f"Create a unique agent_id for yourself (e.g., 'your_name_session_{datetime.now().strftime('%Y%m%d_%H%M%S')}')",
            "help": "Use list_agents to see existing agent IDs and avoid collisions"
        }, indent=2)
        return None, TextContent(type="text", text=error_msg)

    if agent_id not in agent_metadata:
        is_valid, error_message, suggestion = validate_agent_id_format(agent_id)
        if not is_valid:
            error_data = {
                "success": False,
                "error": error_message,
                "agent_id_provided": agent_id
            }
            if suggestion:
                error_data["suggestion"] = f"Try: '{suggestion}'"
                error_data["example"] = f"Or use a more descriptive ID like: '{agent_id}_session_001'"

            error_msg = json.dumps(error_data, indent=2)
            return None, TextContent(type="text", text=error_msg)

    return agent_id, None


def generate_api_key() -> str:
    """
    Generate a secure 32-byte API key for agent authentication.

    Returns:
        Base64-encoded API key string (URL-safe, no padding)
    """
    key_bytes = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(key_bytes).decode('ascii').rstrip('=')


def verify_agent_ownership(agent_id: str, api_key: str, session_bound: bool = False) -> tuple[bool, str | None]:
    """
    Verify that the caller owns the agent_id by checking API key or session binding.

    Args:
        agent_id: Agent ID to verify
        api_key: API key provided by caller (can be None for session-bound agents)
        session_bound: If True, skip API key verification (session IS the auth)

    Returns:
        (is_valid, error_message)
    """
    if session_bound:
        return True, None

    if agent_id not in agent_metadata:
        return False, f"Agent '{agent_id}' does not exist"

    meta = agent_metadata[agent_id]
    stored_key = meta.api_key

    if not stored_key:
        return True, None

    if not isinstance(api_key, str) or not api_key:
        return False, "API key is required and must be a non-empty string"

    if not secrets.compare_digest(api_key, stored_key):
        return False, "Invalid API key. This agent_id belongs to another identity."

    return True, None


def require_agent_auth(agent_id: str, arguments: dict, enforce: bool = False) -> tuple[bool, TextContent | None]:
    """
    Require and verify API key for agent authentication.

    Args:
        agent_id: Agent ID being accessed
        arguments: Tool arguments dict (should contain 'api_key')
        enforce: If True, require API key even for agents without one

    Returns:
        (is_valid, error) - is_valid=True if authenticated, False if error
    """
    api_key = arguments.get("api_key")

    if agent_id not in agent_metadata:
        return True, None

    meta = agent_metadata[agent_id]

    if meta.api_key is None:
        if enforce:
            return False, TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "API key required for authentication",
                    "details": f"Agent '{agent_id}' requires an API key for updates. This is a security requirement to prevent impersonation.",
                    "migration": "This agent was created before authentication was added. Generate a key using get_agent_api_key tool.",
                    "suggestion": "Use get_agent_api_key tool to retrieve or generate your API key"
                }, indent=2)
            )
        else:
            return True, None

    if not api_key:
        return False, TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": "API key required",
                "details": f"Agent '{agent_id}' requires an API key for authentication. This prevents impersonation and protects your identity.",
                "why_this_matters": "Without authentication, anyone could update your agent's state, corrupt your history, and manipulate your governance record.",
                "suggestion": "Include 'api_key' parameter in your request. Use get_agent_api_key tool to retrieve your key."
            }, indent=2)
        )

    is_valid, error_msg = verify_agent_ownership(agent_id, api_key)
    if not is_valid:
        return False, TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": "Authentication failed",
                "details": error_msg or "Invalid API key",
                "why_this_matters": "This agent_id belongs to another identity. Using it would be identity theft.",
                "suggestion": "Use your own agent_id and API key, or create a new agent_id for yourself"
            }, indent=2)
        )

    return True, None
