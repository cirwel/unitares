#!/usr/bin/env python3
"""
UNITARES Governance MCP Server - Streamable HTTP Transport

Multi-client support! Multiple agents (Cursor, Claude Desktop, etc.) can connect
simultaneously and share state via this single server instance.

Usage:
    python src/mcp_server.py [--port PORT] [--host HOST]

    Default bind: 127.0.0.1 (see src/mcp_listen_config.py). For LAN/tunnel use
    UNITARES_BIND_ALL_INTERFACES=1 and set UNITARES_MCP_ALLOWED_HOSTS / UNITARES_MCP_ALLOWED_ORIGINS.

    Default URL: http://127.0.0.1:8767/mcp

Configuration (in claude_desktop_config.json or cursor mcp config):
    {
      "governance-monitor-v1": {
        "url": "http://127.0.0.1:8767/mcp/"
      }
    }

Features:
    - Multiple clients share single server instance
    - Shared state across all agents (knowledge graph, dialectic, etc.)
    - Real multi-agent dialectic (agents can actually review each other!)
    - Persistent service (survives client restarts)
    - Uses MCP Streamable HTTP transport (SSE deprecated)
"""

from __future__ import annotations

import sys
import os
import asyncio
import argparse
import time
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime, timedelta, timezone
import json

# Load environment variables from ~/.env.mcp
try:
    from dotenv import load_dotenv
    env_path = Path.home() / ".env.mcp"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass

# Prometheus metrics (REGISTRY, generate_latest, CONTENT_TYPE_LATEST used in http_api.py)

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
from src._imports import ensure_project_root
project_root = ensure_project_root()

from src.logging_utils import get_logger
from src.services.identity_continuity import (
    format_identity_continuity_startup_message,
    probe_identity_continuity_status,
)
from src.versioning import load_version_from_file
logger = get_logger(__name__)

# Server readiness flag - prevents "request before initialization" errors
# when multiple clients reconnect simultaneously after a server restart
SERVER_READY = False
SERVER_STARTUP_TIME = None
SERVER_START_TIME = time.time()  # Track server start time for uptime metric

# ============================================================================
# Prometheus Metrics & Connection Tracking
# ============================================================================
from src.metrics_registry import (
    TOOL_CALLS_TOTAL, TOOL_CALL_DURATION,
)
from src.connection_tracker import (
    ConnectionTracker, ConnectionTrackingMiddleware,
)

# Try to import MCP SDK
try:
    from mcp.server import FastMCP
    from mcp.server.fastmcp import Context
    from mcp.types import TextContent  # noqa: F401 — availability probe
    MCP_SDK_AVAILABLE = True
except ImportError as e:
    MCP_SDK_AVAILABLE = False
    print(f"Error: MCP SDK not available: {e}", file=sys.stderr)
    print("Install with: pip install mcp", file=sys.stderr)
    sys.exit(1)

# Import dispatch_tool from handlers (reuse all existing tool logic)
from src.mcp_handlers import dispatch_tool
# Wave 3a per-tool routing table imports — hoisted to module load time so
# (a) the per-call ~200-500ns import cost vanishes from the dispatch hot
# path, and (b) ``patch("src.wave3a_routing.get_route")`` style mocks
# affect the wrapper (function-local imports bypass module-level patches —
# see memory ``feedback_patch-local-imports``). FIND-A3 council fold.
from src.wave3a_routing import get_route as _wave3a_get_route
from src.wave3a_beam_proxy import proxy_to_beam as _wave3a_proxy_to_beam

# Tool schemas are now in src/tool_schemas.py (shared module)

# ============================================================================
# Connection Tracking for Multi-Agent Awareness
# (ConnectionTracker and ConnectionTrackingMiddleware live in src/connection_tracker.py)
# ============================================================================

# Global connection tracker
connection_tracker = ConnectionTracker()

def _session_id_from_ctx(ctx: Context | None) -> str | None:
    """
    Resolve a stable per-client session identifier for identity binding.

    Uses SessionSignals when available (set by ASGI wrapper), otherwise
    falls back to legacy extraction paths.
    """
    # Check SessionSignals first (set by ASGI wrapper / ConnectionTrackingMiddleware)
    try:
        from src.mcp_handlers.context import get_session_signals
        signals = get_session_signals()
        if signals:
            # Same priority as derive_session_key, minus async pin lookup
            return (
                signals.x_session_id
                or (f"mcp:{signals.mcp_session_id}" if signals.mcp_session_id else None)
                or signals.oauth_client_id
                or signals.x_client_id
                or signals.ip_ua_fingerprint
            )
    except Exception:
        pass

    # Fallback: legacy extraction (for callers before signals are set)
    try:
        from src.mcp_handlers.context import get_mcp_session_id
        mcp_sid = get_mcp_session_id()
        if mcp_sid:
            return f"mcp:{mcp_sid}"
    except Exception:
        pass

    if ctx is None:
        return None

    try:
        req = ctx.request_context.request
        if req is not None:
            client_id = getattr(req.state, "governance_client_id", None)
            if client_id:
                return client_id
    except Exception:
        pass

    try:
        if ctx.client_id:
            return ctx.client_id
    except Exception:
        pass
    return None


# ============================================================================
# Server Version (sync with VERSION file)
# ============================================================================

def _load_version():
    """Load version from VERSION file."""
    return load_version_from_file(project_root)

SERVER_VERSION = _load_version()


# ============================================================================
# FastMCP Server Setup
# ============================================================================

from src.mcp_listen_config import (
    build_transport_security_settings,
    cors_extra_origins,
    default_listen_host,
)

# --- OAuth 2.1 configuration (optional, enabled by env var) ---
_oauth_issuer_url = os.environ.get("UNITARES_OAUTH_ISSUER_URL")
_oauth_provider = None
_auth_settings = None

if _oauth_issuer_url:
    try:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
        from src.oauth_provider import GovernanceOAuthProvider

        _oauth_secret = os.environ.get("UNITARES_OAUTH_SECRET")
        _auto_approve = os.environ.get("UNITARES_OAUTH_AUTO_APPROVE", "true").lower() in ("true", "1", "yes")
        _oauth_provider = GovernanceOAuthProvider(secret=_oauth_secret, auto_approve=_auto_approve)
        _auth_settings = AuthSettings(
            issuer_url=_oauth_issuer_url,
            resource_server_url=_oauth_issuer_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["mcp:tools"],
                default_scopes=["mcp:tools"],
            ),
        )
        print(f"[FastMCP] OAuth 2.1 enabled (issuer: {_oauth_issuer_url})", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[FastMCP] OAuth setup failed, continuing without auth: {e}", file=sys.stderr, flush=True)
        _oauth_provider = None
        _auth_settings = None

# Create the FastMCP server
# Default bind: 127.0.0.1 (see default_listen_host). LAN/tunnel: set UNITARES_BIND_ALL_INTERFACES=1
# and UNITARES_MCP_ALLOWED_HOSTS / UNITARES_MCP_ALLOWED_ORIGINS as needed.
_LISTEN_HOST = default_listen_host()
mcp = FastMCP(
    name="governance-monitor-v1",
    host=_LISTEN_HOST,
    auth_server_provider=_oauth_provider,
    auth=_auth_settings,
    transport_security=build_transport_security_settings(),
)


# Custom decorator that disables outputSchema to avoid schema validation errors
# FastMCP auto-generates outputSchema based on return type, but our tools return
# complex dicts that don't match the simple {"result": string} schema.
def tool_no_schema(description: str):
    """Decorator for registering tools without outputSchema validation."""
    return mcp.tool(description=description, structured_output=False)


# ============================================================================
# Tool Registration Helper (Optimized)
# ============================================================================

# Cache tool wrappers to avoid recreating functions on every call
# Max size: 100 tools (future-proofing for dynamic tool registration)
# Current tool count: ~51 tools, so plenty of headroom
_MAX_TOOL_WRAPPER_CACHE_SIZE = 100
_tool_wrappers_cache: Dict[str, callable] = {}

def get_tool_wrapper(tool_name: str):
    """
    Get cached tool wrapper or create new one.

    Optimized version that caches wrappers to avoid function creation overhead.
    Each wrapper calls dispatch_tool which routes to @mcp_tool decorated handlers.

    Cache has size limit (100 tools) to prevent unbounded growth if dynamic
    tool registration is added in the future.
    """
    if tool_name not in _tool_wrappers_cache:
        # Check cache size limit (future-proofing)
        if len(_tool_wrappers_cache) >= _MAX_TOOL_WRAPPER_CACHE_SIZE:
            logger.warning(
                f"Tool wrapper cache size limit reached ({_MAX_TOOL_WRAPPER_CACHE_SIZE}). "
                f"Cache contains {len(_tool_wrappers_cache)} tools. "
                f"Consider increasing _MAX_TOOL_WRAPPER_CACHE_SIZE if dynamic tool registration is used."
            )
            # Don't fail - allow cache to grow slightly beyond limit
            # But log warning for visibility
        async def wrapper(**kwargs):
            start_time = time.time()
            logger.info(f"[TOOL_WRAPPER] {tool_name}: called with keys={list(kwargs.keys())}")
            try:
                # Wave 3a per-tool routing table (RFC docs/proposals/
                # beam-wave-3a-read-only-handlers.md v0.2 §3.1). If the
                # tool has been cut over to BEAM, attempt the BEAM proxy
                # with §3.2's 500ms hard timeout. On any BEAM failure mode
                # we fall back to the existing Python dispatch — silent
                # skip is the worst possible outcome per §3.2.
                #
                # Hot-path discipline: the lookup is O(1) and cheap. For
                # the ~100 tools NOT in the routing table the lookup returns
                # None and the existing dispatch fires unchanged. Imports
                # are at module top-level (FIND-A3 council fold) so the
                # per-call cost is zero and ``patch()``-style mocks work.
                beam_url = _wave3a_get_route(tool_name)
                if beam_url is not None:
                    proxy_result = await _wave3a_proxy_to_beam(
                        tool_name=tool_name,
                        beam_url=beam_url,
                        kwargs=kwargs,
                    )
                    if proxy_result.ok:
                        # BEAM succeeded — return its response unchanged.
                        # Python implementation MUST NOT be touched.
                        duration = time.time() - start_time
                        TOOL_CALLS_TOTAL.labels(
                            tool_name=tool_name, status="success"
                        ).inc()
                        TOOL_CALL_DURATION.labels(tool_name=tool_name).observe(
                            duration
                        )
                        return proxy_result.response
                    # BEAM failed — fall through to Python dispatch.
                    # The proxy already emitted the §4.2 fallback event.
                    logger.info(
                        "[TOOL_WRAPPER] %s: BEAM fallback (reason=%s)",
                        tool_name,
                        proxy_result.fallback_reason,
                    )

                # Dispatch to existing handler (which has @mcp_tool timeout protection)
                result = await dispatch_tool(tool_name, kwargs)

                # Record successful call metrics
                duration = time.time() - start_time
                TOOL_CALLS_TOTAL.labels(tool_name=tool_name, status="success").inc()
                TOOL_CALL_DURATION.labels(tool_name=tool_name).observe(duration)

                if result is None:
                    TOOL_CALLS_TOTAL.labels(tool_name=tool_name, status="not_found").inc()
                    return {"success": False, "error": f"Tool '{tool_name}' not found"}

                # Extract structured payload from TextContent response
                # Many MCP clients enforce outputSchema and require structured output.
                # Our handlers return JSON in TextContent.text; parse it and return an object.
                if isinstance(result, (list, tuple)) and len(result) > 0:
                    first_result = result[0]
                    if hasattr(first_result, 'text'):
                        text = first_result.text
                        try:
                            return json.loads(text)
                        except Exception:
                            return {"success": True, "text": text}

                return {"success": True, "result": str(result)}

            except (KeyboardInterrupt, SystemExit):
                # Let system exceptions propagate (for proper shutdown)
                # These should not be caught by error handlers
                raise
            except Exception as e:
                # Record error metrics
                duration = time.time() - start_time
                TOOL_CALLS_TOTAL.labels(tool_name=tool_name, status="error").inc()
                TOOL_CALL_DURATION.labels(tool_name=tool_name).observe(duration)

                # Log error for visibility
                # Note: @mcp_tool decorator on handlers also catches exceptions,
                # but dispatch_tool may raise before reaching handler (e.g., rate limit)
                logger.error(f"Error in tool wrapper {tool_name}: {e}", exc_info=True)
                return {"success": False, "error": str(e), "error_type": type(e).__name__}

        wrapper.__name__ = tool_name
        wrapper.__doc__ = f"Wrapper for {tool_name} tool"
        _tool_wrappers_cache[tool_name] = wrapper

    return _tool_wrappers_cache[tool_name]


# ============================================================================
# AUTO-REGISTRATION SYSTEM
# ============================================================================
# Instead of manually decorating each tool, we auto-register from tool_schemas.py
# This prevents tools from getting out of sync between schemas and SSE server.

# Tools that need session injection from FastMCP Context
# These tools get client_session_id injected from the SSE connection
TOOLS_NEEDING_SESSION_INJECTION = {
    "onboard",
    "identity",
    "process_agent_update",
    "get_governance_metrics",
    "store_knowledge_graph",
    "search_knowledge_graph",
    "leave_note",
    "observe_agent",
    "get_agent_metadata",
    "update_agent_metadata",
    "archive_agent",
    "delete_agent",
    "get_system_history",
    "export_to_file",
    "mark_response_complete",
    "request_dialectic_review",
    "direct_resume_if_safe",
    "update_discovery_status_graph",
    "get_discovery_details",
    "dialectic",
    "get_knowledge_graph",
    "compare_me_to_similar",
}

# FastMCP validates tool arguments before dispatch_tool sees them. For these
# internal/provenance-heavy tools, UNITARES dispatch middleware is the source of
# truth and must receive caller-supplied extra fields unchanged.
EXTRA_ARGUMENT_PASSTHROUGH_TOOLS = {
    "process_agent_update",
}


def auto_register_all_tools():
    """
    Auto-register tools from tool_schemas.py with typed signatures.

    Only registers tools that are in the decorator registry (register=True)
    AND in the active tool mode's allowed set (unless mode is "full").
    Tools with register=False in @mcp_tool decorator are skipped.

    This generates wrappers with explicit parameter signatures from JSON schemas,
    allowing FastMCP to infer correct schemas without kwargs wrapping.

    Benefits:
    - Claude.ai sends parameters directly (no kwargs wrapper needed)
    - CLI's kwargs wrapping still works (dispatch_tool unwraps)
    - Proper client autocomplete from typed signatures
    - Mode filtering reduces tool count for Claude Code (no deferred tools)

    Just add the tool to:
    1. tool_schemas.py (definition)
    2. mcp_handlers/*.py (implementation with @mcp_tool)

    The SSE server will automatically pick it up.
    """
    from src.tool_schemas import get_tool_definitions
    from src.mcp_handlers.support.wrapper_generator import (
        create_typed_wrapper,
        enable_extra_argument_passthrough,
    )
    from src.mcp_handlers.decorators import get_tool_registry
    from src.tool_modes import TOOL_MODE, get_tools_for_mode

    tools = get_tool_definitions()
    registered_count = 0
    skipped_count = 0
    mode_filtered_count = 0

    # Get tools that are registered (register=True in @mcp_tool decorator)
    registered_tools = get_tool_registry()

    # Get allowed tools for current mode (skip filtering in full mode)
    allowed_tools = get_tools_for_mode(TOOL_MODE) if TOOL_MODE != "full" else None

    for tool in tools:
        tool_name = tool.name

        # Skip tools not in registry (register=False in decorator)
        if tool_name not in registered_tools:
            skipped_count += 1
            continue

        # Skip tools not in the active mode's allowed set
        if allowed_tools is not None and tool_name not in allowed_tools:
            mode_filtered_count += 1
            continue

        description = tool.description.split("\n")[0] if tool.description else f"Tool: {tool_name}"
        input_schema = getattr(tool, 'inputSchema', {}) or {}
        inject_session = tool_name in TOOLS_NEEDING_SESSION_INJECTION

        try:
            # Create typed wrapper with explicit parameter signature
            wrapper = create_typed_wrapper(
                tool_name=tool_name,
                input_schema=input_schema,
                get_handler=get_tool_wrapper,
                inject_session=inject_session,
                session_extractor=_session_id_from_ctx,
            )

            # Register with FastMCP - it will infer schema from signature
            mcp.tool(description=description, structured_output=False)(wrapper)
            if tool_name in EXTRA_ARGUMENT_PASSTHROUGH_TOOLS:
                tool_manager = getattr(mcp, "_tool_manager", None)
                registered_tool = (
                    tool_manager.get_tool(tool_name)
                    if tool_manager and hasattr(tool_manager, "get_tool")
                    else None
                )
                if registered_tool is None:
                    logger.warning(
                        "Failed to enable extra argument passthrough for %s: "
                        "registered FastMCP tool not found",
                        tool_name,
                    )
                else:
                    enabled = enable_extra_argument_passthrough(registered_tool)
                    if enabled:
                        logger.info(
                            "Enabled extra argument passthrough for %s",
                            tool_name,
                        )
            registered_count += 1

        except Exception as e:
            logger.warning(f"Failed to auto-register tool {tool_name}: {e}")

    logger.info(
        f"[AUTO_REGISTER] Registered {registered_count} tools, "
        f"skipped {skipped_count} (not in registry), "
        f"filtered {mode_filtered_count} (mode={TOOL_MODE})"
    )
    return registered_count

# Call auto-registration
auto_register_all_tools()

# ============================================================================
# COMMON ALIASES - Register most-guessed tool names as thin MCP wrappers
# ============================================================================
# Aliases are resolved at dispatch time (tool_stability.py), but FastMCP rejects
# unknown tool names before dispatch runs. These register the top aliases so
# agents can use intuitive names like status() without "Unknown tool" errors.

def _register_common_aliases():
    from src.mcp_handlers.tool_stability import (
        AGENT_WORKFLOW_ALIASES,
        resolve_tool_alias,
    )
    from src.mcp_handlers.support.wrapper_generator import (
        create_typed_wrapper,
        enable_extra_argument_passthrough,
    )

    common = list(AGENT_WORKFLOW_ALIASES)
    count = 0
    for alias_name in common:
        actual, info = resolve_tool_alias(alias_name)
        if not info:
            continue

        # Get the actual tool's schema so the alias has matching parameters
        from src.tool_schemas import get_tool_definitions
        actual_schema = {}
        for tool_def in get_tool_definitions():
            if tool_def.name == actual:
                actual_schema = getattr(tool_def, 'inputSchema', {}) or {}
                break

        # If inject_action is set, remove "action" from the alias schema —
        # the alias auto-injects it, so clients shouldn't need to provide it
        if info.inject_action and actual_schema:
            import copy
            actual_schema = copy.deepcopy(actual_schema)
            actual_schema.get("properties", {}).pop("action", None)
            req = actual_schema.get("required", [])
            if "action" in req:
                actual_schema["required"] = [r for r in req if r != "action"]

        try:
            # Create a handler that resolves the alias to the actual tool name
            # and auto-injects the action parameter if the alias defines one
            inject_action = info.inject_action
            def make_alias_handler(actual_name, action_to_inject):
                """Closure factory — captures actual_name and action per alias."""
                base_handler = get_tool_wrapper(actual_name)
                if action_to_inject:
                    async def aliased_handler(**kwargs):
                        kwargs.setdefault("action", action_to_inject)
                        return await base_handler(**kwargs)
                    return aliased_handler
                return base_handler

            alias_handler = make_alias_handler(actual, inject_action)

            # get_handler is called with tool_name at dispatch time, so we
            # return the pre-built alias_handler regardless of the name passed in
            def alias_get_handler(name, _h=alias_handler):
                return _h

            wrapper = create_typed_wrapper(
                tool_name=alias_name,
                input_schema=actual_schema,
                get_handler=alias_get_handler,
                inject_session=actual in TOOLS_NEEDING_SESSION_INJECTION,
                session_extractor=_session_id_from_ctx,
            )
            desc = f"{info.migration_note or f'Alias for {actual}'}"
            mcp.tool(description=desc, structured_output=False)(wrapper)
            if actual in EXTRA_ARGUMENT_PASSTHROUGH_TOOLS:
                tool_manager = getattr(mcp, "_tool_manager", None)
                registered_tool = (
                    tool_manager.get_tool(alias_name)
                    if tool_manager and hasattr(tool_manager, "get_tool")
                    else None
                )
                if registered_tool is not None:
                    enable_extra_argument_passthrough(registered_tool)
            count += 1
        except Exception as e:
            logger.debug(f"[ALIAS] Failed to register {alias_name}: {e}")

    if count:
        logger.info(f"[AUTO_REGISTER] Registered {count} common aliases")

_register_common_aliases()

# ============================================================================
# LEGACY MANUAL REGISTRATIONS (kept for reference, will be removed)
# ============================================================================
# The auto_register_all_tools() above handles all tools.
# These manual registrations below are now redundant but kept temporarily
# for any tools with special handling not captured above.

# NOTE: hello/who_am_i removed Dec 2025 - identity auto-binds on first tool call
# Use identity(name='...') for self-naming

# REMOVED: All manual @tool_no_schema decorators
# Tools are now auto-registered from tool_schemas.py

# ============================================================================




# ============================================================================
# Server Entry Point
# ============================================================================

DEFAULT_HOST = default_listen_host()
DEFAULT_PORT = 8767  # Standard port for unitares governance on Mac (8766 is anima, 8765 was old default)

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="UNITARES Governance MCP Server (Streamable HTTP)"
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=(
            "Host to bind to (default: from UNITARES_MCP_HOST, else 127.0.0.1, "
            "or 0.0.0.0 when UNITARES_BIND_ALL_INTERFACES=1). "
            "Override for LAN/tunnel; set UNITARES_MCP_ALLOWED_HOSTS for non-local Host headers."
        ),
    )
    parser.add_argument(
        "--port", 
        type=int, 
        default=DEFAULT_PORT,
        help=f"Port to bind to (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force start: clean up any stale lock files and PID files"
    )
    return parser.parse_args()


from src.process_management import (
    is_process_alive, cleanup_existing_server_processes,
    write_server_pid_file, remove_server_pid_file,
    acquire_server_lock, release_server_lock,
    ensure_server_pid_file, ensure_server_lock,
    SERVER_PID_FILE, SERVER_LOCK_FILE,
)


async def main():
    """Main entry point for governance MCP server."""
    args = parse_args()

    # Load entry-point plugins now that src.mcp_handlers is fully
    # initialised (deferred here to avoid a circular import when a
    # plugin's handlers module imports from src.mcp_handlers.*).
    from src.plugin_loader import load_plugins
    from src.mcp_handlers import refresh_tool_handlers_from_registry
    loaded = load_plugins()
    added = refresh_tool_handlers_from_registry()
    if loaded:
        logger.info("plugins loaded: %s (+%d tools)", loaded, added)

    # Keep FastMCP's declared host aligned with uvicorn when --host overrides env defaults
    try:
        mcp.settings.host = args.host
    except Exception as e:
        logger.debug("Could not sync mcp.settings.host to %s: %s", args.host, e)

    # --force: Explicitly clean up lock file and PID file before starting
    if args.force:
        logger.info("--force: Cleaning up stale lock and PID files")
        try:
            if SERVER_LOCK_FILE.exists():
                SERVER_LOCK_FILE.unlink()
                logger.info(f"Removed lock file: {SERVER_LOCK_FILE}")
        except Exception as e:
            logger.warning(f"Could not remove lock file: {e}")
        try:
            if SERVER_PID_FILE.exists():
                # Check if PID is actually running before removing
                try:
                    with open(SERVER_PID_FILE, 'r') as f:
                        old_pid = int(f.read().strip())
                    if not is_process_alive(old_pid):
                        SERVER_PID_FILE.unlink()
                        logger.info(f"Removed stale PID file: {SERVER_PID_FILE} (PID {old_pid} not running)")
                    else:
                        logger.warning(f"PID file exists for running process {old_pid}, will terminate it")
                except (ValueError, IOError):
                    # Invalid PID file, safe to remove
                    SERVER_PID_FILE.unlink()
                    logger.info(f"Removed invalid PID file: {SERVER_PID_FILE}")
        except Exception as e:
            logger.warning(f"Could not remove PID file: {e}")

    # Process deduplication: Check for and kill existing server processes
    killed = cleanup_existing_server_processes()
    if killed:
        logger.info(f"Cleaned up {len(killed)} existing server process(es)")

    # Acquire lock to prevent multiple instances
    lock_fd = None
    try:
        lock_fd = acquire_server_lock()
    except RuntimeError as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        print("💡 Tip: Use --force to clean up stale locks", file=sys.stderr)
        sys.exit(1)
    
    # Write PID file
    write_server_pid_file()

    async def _maintain_process_markers():
        nonlocal lock_fd
        while True:
            try:
                ensure_server_pid_file()
                lock_fd = ensure_server_lock(lock_fd)
            except Exception as e:
                logger.debug(f"Process marker maintenance skipped: {e}")
            await asyncio.sleep(15)

    marker_task = asyncio.create_task(_maintain_process_markers())

    # Clean up stale agent locks from crashed processes
    try:
        from src.lock_cleanup import cleanup_stale_state_locks
        cleanup_result = cleanup_stale_state_locks(
            project_root=Path(project_root),
            max_age_seconds=300.0  # 5 minutes
        )
        if cleanup_result.get('cleaned', 0) > 0:
            logger.info(f"Cleaned up {cleanup_result['cleaned']} stale agent lock(s) at startup")
    except Exception as e:
        logger.warning(f"Could not clean up stale locks at startup: {e}")

    # Initialize database abstraction layer
    try:
        from src.db import init_db, close_db, get_db
        await init_db()
        db = get_db()
        logger.info("Database initialized: backend=postgres")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        print(f"\n❌ Database initialization failed: {e}", file=sys.stderr)
        release_server_lock(lock_fd)
        remove_server_pid_file()
        sys.exit(1)

    continuity_status = await probe_identity_continuity_status()
    continuity_message = format_identity_continuity_startup_message(continuity_status)
    if continuity_status.get("mode") == "redis":
        logger.info(continuity_message)
    else:
        logger.warning(continuity_message)

    # Seed event detector with known agents so restarts don't fire false agent_new.
    # Uses list_recently_active_identities (server-side filter on last_activity_at,
    # ordered DESC) rather than list_identities — the latter orders by created_at
    # DESC, so old-but-active substrate-anchored agents (Lumen) get pushed off the
    # seed once ephemeral session creation outpaces the limit, and every restart
    # re-fires agent_new for them. Each governance-mcp restart in the
    # wedge-symptom window produced one spurious "New Agent: Lumen" alert per
    # cycle — three within ~90min observed live 2026-04-27.
    try:
        from src.event_detector import event_detector
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        identities = await db.list_recently_active_identities(cutoff, limit=500)
        recent = [
            (ident.agent_id, ident.metadata.get("label") or ident.agent_id[:12])
            for ident in identities
        ]
        seeded = event_detector.seed_known_agents(recent)
        if seeded:
            logger.info("Event detector seeded with %d known agent(s)", seeded)
    except Exception as e:
        logger.warning("Could not seed event detector: %s", e)

    # Give audit logger a reference to the event loop for executor-thread writes
    from src.audit_log import AuditLogger
    AuditLogger._event_loop = asyncio.get_running_loop()

    endpoint = f"http://{args.host}:{args.port}/mcp"
    config_json = f'{{"url": "{endpoint}"}}'

    print(f"""
╔════════════════════════════════════════════════════════════════════╗
║       UNITARES Governance MCP Server                               ║
╠════════════════════════════════════════════════════════════════════╣
║  Version:  {SERVER_VERSION}                                                   ║
║                                                                    ║
║  MCP Transport:                                                    ║
║    Streamable HTTP:    {endpoint:<46}║
║                                                                    ║
║  REST API:                                                         ║
║    List tools:         GET  /v1/tools                              ║
║    Call tool:          POST /v1/tools/call                         ║
║    Health:             GET  /health                                ║
║    Metrics:            GET  /metrics                               ║
╚════════════════════════════════════════════════════════════════════╝
""")
    
    logger.info(f"Starting governance server on http://{args.host}:{args.port}/mcp")
    if args.host in ("127.0.0.1", "::1", "localhost"):
        logger.info(
            "Listening on loopback only. For LAN/tunnel set --host 0.0.0.0 or "
            "UNITARES_BIND_ALL_INTERFACES=1, and configure UNITARES_MCP_ALLOWED_HOSTS / "
            "UNITARES_MCP_ALLOWED_ORIGINS."
        )

    # Run the governance MCP server
    try:
        import uvicorn
        from starlette.applications import Starlette  # noqa: F401 — availability probe
        from starlette.responses import JSONResponse
        from starlette.middleware.cors import CORSMiddleware
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        # Create Streamable HTTP session manager (primary MCP transport)
        # stateless=True: any client can connect without MCP-level session management
        #   (we handle identity separately via transport signals + sticky cache)
        _streamable_session_manager = StreamableHTTPSessionManager(
            app=mcp._mcp_server,
            stateless=True,
        )
        HAS_STREAMABLE_HTTP = True
        logger.info("Streamable HTTP transport available at /mcp")

        # NOTE: sse_app() provides the Starlette base app. The SSE transport at /sse
        # is unused (all clients use /mcp), but sse_app() is needed because bare
        # Starlette(routes=[]) breaks POST body reading for REST routes.
        app = mcp.sse_app()
        
        # === Add CORS support for web-based GPT/Gemini clients ===
        # CORS: restrict to known origins (dashboard, local dev, Tailscale)
        _cors_allow_origin = os.getenv("UNITARES_HTTP_CORS_ALLOW_ORIGIN")
        _cors_origins = [
            "http://localhost:8767",
            "http://127.0.0.1:8767",
        ]
        if _cors_allow_origin:
            _cors_origins.append(_cors_allow_origin)
        _cors_origins.extend(cors_extra_origins())
        app.add_middleware(
            CORSMiddleware,
            allow_origins=_cors_origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["*"],
        )
        
        # === Connection Tracking Middleware ===
        # Class lives in src/connection_tracker.py — see ConnectionTrackingMiddleware
        app.add_middleware(
            ConnectionTrackingMiddleware,
            connection_tracker=connection_tracker,
            server_ready_fn=lambda: SERVER_READY,
            server_version=SERVER_VERSION,
        )
        
        # === Start all background tasks ===
        from src.background_tasks import start_all_background_tasks, stop_all_background_tasks

        def _set_server_ready():
            global SERVER_READY, SERVER_STARTUP_TIME
            SERVER_READY = True
            SERVER_STARTUP_TIME = datetime.now()

        start_all_background_tasks(
            connection_tracker=connection_tracker,
            set_ready=_set_server_ready,
        )

        # === HTTP REST endpoints for non-MCP clients (Llama, Mistral, etc.) ===
        HTTP_CORS_ALLOW_ORIGIN = os.getenv("UNITARES_HTTP_CORS_ALLOW_ORIGIN")  # e.g. "*" or "http://localhost:3000"

        from src.http_api import register_http_routes
        register_http_routes(
            app,
            connection_tracker=connection_tracker,
            server_ready_fn=lambda: SERVER_READY,
            server_start_time=SERVER_START_TIME,
            server_version=SERVER_VERSION,
            has_streamable_http=HAS_STREAMABLE_HTTP,
            mcp_server_name=mcp.name,
        )

        # === Wave 3a probe endpoint (PR #1 scaffolding, see docs/proposals/
        # beam-wave-3a-read-only-handlers.md §2.3). Internal-only surface
        # consumed by the BEAM listener once PR #4 lands. Fail-closed:
        # missing WAVE_3A_PROBE_TOKEN -> 503 on every /v1/probe/* call.
        from src.mcp_handlers.wave3a_probe import register_wave3a_probe_routes
        register_wave3a_probe_routes(app)

        # === Wave 3a admin surface (PR #3 of v0.2 sequencing, see
        # docs/proposals/beam-wave-3a-read-only-handlers.md §3.1). Backs
        # scripts/ops/wave-3a-rollback.sh. Operator-token gated
        # (UNITARES_OPERATOR_TOKENS); missing/wrong → 401. The routing
        # table starts empty at every process boot (§3.1 invariant).
        from src.mcp_handlers.wave3a_admin import register_wave3a_admin_routes
        register_wave3a_admin_routes(app)

        # === Wave 3a per-handler env-flag cutover (PR #5+). Reads
        # ``_ENV_FLAG_ROUTES`` in ``src/wave3a_routing.py`` and adds a
        # routing-table row for every flag that is truthy. Default-OFF
        # posture: every flag is unset by default; behavior is unchanged
        # until the operator sets a flag in
        # ``~/.config/cirwel/secrets.env`` and restarts. PR #5 (the first
        # cutover) flips ``WAVE_3A_HEALTH_CHECK_ON_BEAM``.
        from src.wave3a_routing import apply_env_flag_routes
        _wave3a_added = apply_env_flag_routes()
        if _wave3a_added:
            logger.info(
                "[wave3a-routing] startup-hook added %d route(s): %s",
                len(_wave3a_added),
                _wave3a_added,
            )

        # === Streamable HTTP endpoint (/mcp) ===
        if HAS_STREAMABLE_HTTP:
            # Create a pure ASGI app for /mcp that wraps the session manager
            # Using Mount with an ASGI app avoids Starlette's Route handler wrapper
            # which expects a Response to be returned (causing NoneType callable error)
            async def streamable_mcp_asgi(scope, receive, send):
                """ASGI app for Streamable HTTP MCP at /mcp."""
                if scope.get("type") != "http":
                    return

                # BUILD SESSION SIGNALS — single capture of all transport headers
                # No priority decisions here; derive_session_key() handles that.
                client_hint_token = None
                mcp_session_token = None
                signals_token = None
                try:
                    from starlette.datastructures import Headers
                    from src.mcp_handlers.context import (
                        SessionSignals, set_session_signals, reset_session_signals,
                        detect_client_from_user_agent, set_transport_client_hint, reset_transport_client_hint,
                        set_mcp_session_id, reset_mcp_session_id, note_ua_fingerprint
                    )
                    headers = Headers(scope=scope)

                    # Extract all headers into SessionSignals (no priority decisions)
                    mcp_sid = headers.get("mcp-session-id")
                    client = scope.get("client")
                    client_ip = client[0] if (client and len(client) >= 1) else "unknown"
                    ua = headers.get("user-agent", "unknown")
                    import hashlib
                    ua_fingerprint = hashlib.md5(ua.encode()).hexdigest()[:6]
                    note_ua_fingerprint(ua_fingerprint, ua)
                    x_session_id = headers.get("x-session-id")
                    x_client_id = headers.get("x-client-id") or headers.get("x-mcp-client-id")

                    # Extract OAuth client identity from Bearer token
                    oauth_client_id = None
                    auth_header = headers.get("authorization", "")
                    if auth_header.startswith("Bearer "):
                        token = auth_header[7:]
                        try:
                            client_id = _oauth_provider.get_token_client_id(token) if _oauth_provider else None
                            if client_id:
                                oauth_client_id = f"oauth:{client_id}"
                        except Exception:
                            pass

                    detected_client = detect_client_from_user_agent(ua)
                    ip_ua_fp = f"{client_ip}:{ua_fingerprint}"

                    # S19: kernel-attested peer PID from the UDS listener,
                    # if this request arrived over Unix-domain socket. The
                    # PeerCredHTTPProtocol in src/uds_listener.py stamps
                    # this value into the scope at connection-accept; HTTP
                    # requests have it absent.
                    _peer_pid = scope.get("unitares_peer_pid")
                    _transport_label = "uds" if _peer_pid is not None else "mcp"

                    signals = SessionSignals(
                        mcp_session_id=mcp_sid,
                        x_session_id=x_session_id,
                        x_client_id=x_client_id,
                        oauth_client_id=oauth_client_id,
                        ip_ua_fingerprint=ip_ua_fp,
                        user_agent=ua,
                        client_hint=detected_client,
                        x_agent_name=headers.get("x-agent-name"),
                        x_agent_id=headers.get("x-agent-id"),
                        transport=_transport_label,
                        peer_pid=_peer_pid,
                        unitares_operator_token=headers.get("x-unitares-operator"),
                    )
                    signals_token = set_session_signals(signals)

                    # Backward compat: set individual contextvars that downstream code reads
                    if mcp_sid:
                        mcp_session_token = set_mcp_session_id(mcp_sid)

                    # Backward compat: expose client_id in scope.state for ConnectionTrackingMiddleware consumers
                    client_id = x_session_id or oauth_client_id or x_client_id or ip_ua_fp
                    state = scope.setdefault("state", {})
                    state["governance_client_id"] = client_id

                    # Backward compat: set session context
                    from src.mcp_handlers.context import set_session_context, reset_session_context
                    session_context_token = set_session_context(
                        session_key=signals.ip_ua_fingerprint or "unknown",
                        client_session_id=x_session_id or x_client_id,
                        user_agent=ua,
                    )

                    if detected_client:
                        client_hint_token = set_transport_client_hint(detected_client)

                except Exception as e:
                    logger.debug(f"[/mcp] Could not capture context: {e}")

                try:
                    # Delegate to the session manager - it handles ASGI directly
                    await _streamable_session_manager.handle_request(scope, receive, send)
                except Exception as e:
                    logger.error(f"Streamable HTTP error: {e}", exc_info=True)
                    response = JSONResponse({
                        "error": "Streamable HTTP transport error",
                        "details": str(e)
                    }, status_code=500)
                    await response(scope, receive, send)
                finally:
                    # Reset contextvars
                    if 'session_context_token' in locals() and session_context_token is not None:
                        try:
                            from src.mcp_handlers.context import reset_session_context
                            reset_session_context(session_context_token)
                        except Exception:
                            pass
                    if mcp_session_token is not None:
                        try:
                            from src.mcp_handlers.context import reset_mcp_session_id
                            reset_mcp_session_id(mcp_session_token)
                        except Exception:
                            pass
                    if client_hint_token is not None:
                        try:
                            from src.mcp_handlers.context import reset_transport_client_hint
                            reset_transport_client_hint(client_hint_token)
                        except Exception:
                            pass
                    if signals_token is not None:
                        try:
                            from src.mcp_handlers.context import reset_session_signals
                            reset_session_signals(signals_token)
                        except Exception:
                            pass

            # Mount as ASGI app instead of Route handler (avoids NoneType callable error)
            from starlette.routing import Mount
            app.routes.append(Mount("/mcp", app=streamable_mcp_asgi))
            logger.info("Registered /mcp endpoint for Streamable HTTP transport")

        # NOTE: CORS middleware is already registered above.
        # Do not add a second CORSMiddleware — duplicate registration causes confusing behavior.
        
        # Run with uvicorn
        # SECURITY: Add connection limits and timeouts to prevent DoS
        config = uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_level="info",
            reload=args.reload,
            limit_concurrency=100,  # Max concurrent connections
            timeout_keep_alive=5,  # Keep-alive timeout (seconds)
            timeout_graceful_shutdown=10,  # Graceful shutdown timeout
            forwarded_allow_ips="127.0.0.1",  # Only trust proxy headers from localhost (cloudflared)
            proxy_headers=True  # Process X-Forwarded-* headers
        )
        server = uvicorn.Server(config)

        # S19: optional UDS listener for substrate-anchored residents. Gated
        # by env var so existing HTTP-only deployments are unaffected. The
        # UDS listener serves the same ASGI app, but every request scope
        # gains `unitares_peer_pid` populated from kernel-attested
        # LOCAL_PEERPID — used downstream by the substrate-claim verification
 # path. v2.
        _uds_socket_path = os.getenv("UNITARES_UDS_SOCKET")
        _uds_task: Optional[asyncio.Task[None]] = None
        if _uds_socket_path:
            try:
                from src.uds_listener import start_uds_listener
                _uds_task = await start_uds_listener(app, _uds_socket_path)
                logger.info(
                    "[UDS] substrate-attestation listener started at %s",
                    _uds_socket_path,
                )
            except Exception as e:
                logger.error(
                    "[UDS] failed to start listener at %s: %s; "
                    "HTTP-only mode (substrate residents will fall back)",
                    _uds_socket_path, e, exc_info=True,
                )
                _uds_task = None

        # session_manager.run() owns the anyio task group lifecycle;
        # no manual _task_group/_has_started poking needed.
        try:
            if HAS_STREAMABLE_HTTP:
                async with _streamable_session_manager.run():
                    logger.info("[STREAMABLE] Session manager started")
                    await server.serve()
                logger.info("[STREAMABLE] Session manager shut down")
            else:
                await server.serve()
        finally:
            if _uds_task is not None:
                _uds_task.cancel()
                try:
                    await _uds_task
                except (asyncio.CancelledError, Exception):
                    pass
                if _uds_socket_path and os.path.exists(_uds_socket_path):
                    try:
                        os.unlink(_uds_socket_path)
                    except OSError:
                        pass
    except ImportError:
        print("Error: uvicorn not installed. Install with: pip install uvicorn", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        logger.error(f"Server error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        try:
            marker_task.cancel()
            await marker_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Error stopping process marker maintenance: {e}")
        try:
            await stop_all_background_tasks()
        except Exception as e:
            logger.debug(f"Error stopping background tasks: {e}")
        try:
            await close_db()
        except Exception as e:
            logger.warning(f"Error closing database: {e}")
        release_server_lock(lock_fd)
        remove_server_pid_file()


if __name__ == "__main__":
    # Tracemalloc is opt-in — enable with UNITARES_TRACEMALLOC=1 (optionally
    # UNITARES_TRACEMALLOC_FRAMES=N to control traceback depth). It was
    # previously unconditional and pegged the event loop at high CPU because
    # a 25-frame traceback was captured on every allocation in a very
    # allocation-heavy async server. Default off; turn on only when actively
    # chasing a memory leak, and use a small frame count (e.g. 3-5).
    import os
    if os.getenv("UNITARES_TRACEMALLOC", "").lower() in ("1", "true", "yes"):
        import tracemalloc
        try:
            _frames = int(os.getenv("UNITARES_TRACEMALLOC_FRAMES", "5"))
        except ValueError:
            _frames = 5
        tracemalloc.start(_frames)
        print(f"[tracemalloc] enabled with {_frames} frames")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer stopped.")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
