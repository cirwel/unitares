#!/usr/bin/env python3
"""
UNITARES Governance MCP Server - STDIO Transport

Thin STDIO transport layer for MCP clients (Claude Desktop, Cursor).
All business logic lives in src.agent_state. This module only handles:
- STDIO MCP transport (stdin/stdout)
- Proxy mode (forwarding to HTTP governance server)
- MCP resource registration
- Tool dispatch via handler registry

Usage:
    python src/mcp_server_std.py

Configuration:
    Add to Cursor MCP config (for Composer) or Claude Desktop MCP config
"""

from __future__ import annotations

import sys
import json
import asyncio
import os
import time
from pathlib import Path
from typing import Any, Sequence
from datetime import datetime

# -----------------------------------------------------------------------------
# BOOTSTRAP IMPORT PATH (critical for Claude Desktop / script execution)
# -----------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Load .env file if present
try:
    from dotenv import load_dotenv
    _env_path = _PROJECT_ROOT / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass

from src.logging_utils import get_logger
from src.services.identity_continuity import (
    format_identity_continuity_startup_message,
    get_identity_continuity_status,
    probe_identity_continuity_status,
)
logger = get_logger(__name__)

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
except ImportError as e:
    print(f"Error: MCP SDK not available: {e}", file=sys.stderr)
    print(f"Install with: pip install mcp", file=sys.stderr)
    sys.exit(1)

# Import business logic from the canonical module (only what this transport layer needs)
from src.agent_state import (
    project_root,
    agent_metadata,
    process_mgr,
    load_metadata_async,
    load_metadata,
    get_or_create_metadata,
    generate_api_key,
    init_server_process,
    remove_pid_file,
    _normalize_http_proxy_base,
)

from src.tool_schemas import get_tool_definitions
from src.lock_cleanup import cleanup_stale_state_locks
from src.services.tool_usage_recorder import classify_tool_result, record_tool_usage
from src.background_tasks import create_tracked_task

# ============================================================================
# MCP Server Instance
# ============================================================================

server = Server("governance-monitor-v1")

# ============================================================================
# MCP Resource Registration (SKILL.md)
# ============================================================================

@server.list_resources()
async def list_resources():
    from mcp.types import Resource
    return [
        Resource(
            uri="unitares://skill",
            name="UNITARES Governance SKILL",
            description="Governance framework orientation document for agents",
            mimeType="text/markdown",
        )
    ]

@server.read_resource()
async def read_resource(uri):
    if str(uri) == "unitares://skill":
        skill_path = Path(project_root) / "skills" / "unitares-governance" / "SKILL.md"
        if skill_path.exists():
            content = skill_path.read_text()
        else:
            content = "# UNITARES Governance\n\nSKILL.md not found. Use onboard() to get started."
        return content
    raise ValueError(f"Unknown resource: {uri}")

# ============================================================================
# STDIO -> Server Proxy Mode
#
# When UNITARES_PROXY_URL or UNITARES_STDIO_PROXY_HTTP_URL is set, this
# server becomes a thin proxy forwarding list_tools/call_tool to the
# already-running HTTP governance server.
# ============================================================================

STDIO_PROXY_HTTP_URL = os.getenv("UNITARES_STDIO_PROXY_HTTP_URL")
STDIO_PROXY_URL = (os.getenv("UNITARES_PROXY_URL")
                    or os.getenv("UNITARES_STDIO_PROXY_URL")
                    or os.getenv("UNITARES_STDIO_PROXY_SSE_URL"))
STDIO_PROXY_STRICT = os.getenv("UNITARES_STDIO_PROXY_STRICT", "1").strip().lower() not in ("0", "false", "no")
STDIO_PROXY_HTTP_BEARER_TOKEN = os.getenv("UNITARES_STDIO_PROXY_HTTP_BEARER_TOKEN")


async def _proxy_http_list_tools() -> list[Tool]:
    """Proxy list_tools to HTTP (/v1/tools) and convert to MCP Tool objects."""
    import urllib.request

    base = _normalize_http_proxy_base(STDIO_PROXY_HTTP_URL)
    url = f"{base}/v1/tools"

    headers = {"Accept": "application/json", "X-Session-ID": f"stdio:{os.getpid()}"}
    if STDIO_PROXY_HTTP_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {STDIO_PROXY_HTTP_BEARER_TOKEN}"

    def _fetch_sync() -> dict[str, Any]:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read().decode("utf-8")
        return json.loads(data)

    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(None, _fetch_sync)

    tools = []
    for entry in payload.get("tools", []) or []:
        fn = entry.get("function") if isinstance(entry, dict) else None
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not name:
            continue
        tools.append(Tool(
            name=name,
            description=fn.get("description") or "",
            inputSchema=fn.get("parameters") or {"type": "object", "properties": {}},
        ))
    try:
        from src.tool_modes import TOOL_MODE, should_include_tool
        return [t for t in tools if should_include_tool(t.name, mode=TOOL_MODE)]
    except Exception:
        return tools


async def _proxy_http_call_tool(name: str, arguments: dict[str, Any]) -> Sequence[TextContent]:
    """Proxy call_tool to HTTP (/v1/tools/call)."""
    import urllib.request

    base = _normalize_http_proxy_base(STDIO_PROXY_HTTP_URL)
    url = f"{base}/v1/tools/call"

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Session-ID": f"stdio:{os.getpid()}",
    }
    if STDIO_PROXY_HTTP_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {STDIO_PROXY_HTTP_BEARER_TOKEN}"

    body = json.dumps({"name": name, "arguments": arguments or {}}).encode("utf-8")

    def _post_sync() -> dict[str, Any]:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read().decode("utf-8")
        return json.loads(data)

    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(None, _post_sync)

    if isinstance(payload, dict) and payload.get("success") is True and "result" in payload:
        out = payload["result"]
    else:
        out = payload
    return [TextContent(type="text", text=json.dumps(out, indent=2))]


def _create_http1_only_client_factory():
    """Create httpx client factory that forces HTTP/1.1 only (fixes reverse proxy 421 errors)."""
    import httpx
    def http1_client_factory(**kwargs):
        return httpx.AsyncClient(http2=False, **kwargs)
    return http1_client_factory


async def _proxy_list_tools() -> list[Tool]:
    """Proxy list_tools to remote MCP server (auto-detects Streamable HTTP vs legacy SSE)."""
    from mcp.client.session import ClientSession
    http1_factory = _create_http1_only_client_factory()

    if "/mcp" in STDIO_PROXY_URL:
        import httpx
        from mcp.client.streamable_http import streamable_http_client
        async with httpx.AsyncClient(http2=False, timeout=15) as http_client:
            async with streamable_http_client(STDIO_PROXY_URL, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    res = await session.list_tools()
                    return res.tools
    else:
        from mcp.client.sse import sse_client
        async with sse_client(STDIO_PROXY_URL, httpx_client_factory=http1_factory) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.list_tools()
                return res.tools


async def _proxy_call_tool(name: str, arguments: dict[str, Any]) -> Sequence[TextContent]:
    """Proxy call_tool to remote MCP server (per-request connection for teardown safety)."""
    from mcp.client.session import ClientSession
    http1_factory = _create_http1_only_client_factory()

    if "/mcp" in STDIO_PROXY_URL:
        import httpx
        from mcp.client.streamable_http import streamable_http_client
        async with httpx.AsyncClient(http2=False, timeout=15) as http_client:
            async with streamable_http_client(STDIO_PROXY_URL, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    res = await session.call_tool(name, arguments)
                    return res.content
    else:
        from mcp.client.sse import sse_client
        async with sse_client(STDIO_PROXY_URL, httpx_client_factory=http1_factory) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(name, arguments)
                return res.content


# ============================================================================
# Activity Tracking (for auto-heartbeat injection)
# ============================================================================

from src.activity_tracker import get_activity_tracker, HeartbeatConfig

HEARTBEAT_CONFIG = HeartbeatConfig(
    conversation_turn_threshold=5,
    tool_call_threshold=10,
    time_threshold_minutes=15,
    complexity_threshold=3.0,
    file_modification_threshold=3,
    enabled=True,
    track_conversation_turns=True,
    track_tool_calls=True,
    track_complexity=True
)

activity_tracker = get_activity_tracker(HEARTBEAT_CONFIG)


# ============================================================================
# MCP Tool Handlers
# ============================================================================

@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available MCP tools"""
    def _filtered_local_tools() -> list[Tool]:
        tools = get_tool_definitions()
        try:
            from src.tool_modes import TOOL_MODE, should_include_tool
            return [t for t in tools if should_include_tool(t.name, mode=TOOL_MODE)]
        except Exception:
            return tools

    if STDIO_PROXY_HTTP_URL:
        try:
            return await _proxy_http_list_tools()
        except Exception as e:
            logger.error(f"STDIO proxy list_tools failed (HTTP {STDIO_PROXY_HTTP_URL}): {e}", exc_info=True)
            if STDIO_PROXY_STRICT:
                raise
            return _filtered_local_tools()
    if STDIO_PROXY_URL:
        try:
            return await _proxy_list_tools()
        except Exception as e:
            logger.error(f"STDIO proxy list_tools failed ({STDIO_PROXY_URL}): {e}", exc_info=True)
            if STDIO_PROXY_STRICT:
                raise
            return _filtered_local_tools()

    return _filtered_local_tools()


async def inject_lightweight_heartbeat(
    agent_id: str,
    trigger_reason: str,
    activity_summary: dict,
    tracker
) -> None:
    """Inject a lightweight governance heartbeat (non-blocking, fire-and-forget)."""
    try:
        load_metadata()

        if agent_id not in agent_metadata:
            # #425 Path E: when STRICT_IDENTITY_REQUIRED is on, do NOT
            # auto-create metadata for an unknown agent. Heartbeat for
            # an unbound agent_id is itself a side-effect of an upstream
            # auto-mint that the contract refuses; minting metadata here
            # would re-introduce the leak outside the middleware. Default
            # off; gated by env flag for staged rollout.
            from src.mcp_handlers.identity_bootstrap import is_strict_identity_required
            if is_strict_identity_required():
                logger.debug(
                    "[HEARTBEAT] STRICT_IDENTITY_REQUIRED=true and "
                    "agent_id %s... not in metadata cache — skipping "
                    "heartbeat (#425 Path E)",
                    (agent_id or "")[:12],
                )
                return
            get_or_create_metadata(agent_id)

        meta = agent_metadata.get(agent_id)
        if not meta:
            return

        api_key = meta.api_key
        if not api_key:
            api_key = generate_api_key()
            meta.api_key = api_key
            try:
                from src import agent_storage
                await agent_storage.update_agent(agent_id, api_key=api_key)
            except Exception as e:
                logger.debug(f"PostgreSQL API key update failed: {e}")

        from src.mcp_handlers.core import handle_process_agent_update

        heartbeat_args = {
            'agent_id': agent_id,
            'api_key': api_key,
            'heartbeat': True,
            'trigger_reason': trigger_reason,
            'activity_summary': activity_summary,
            'response_text': f"Auto-heartbeat ({trigger_reason})",
            'complexity': activity_summary.get('average_complexity', 0.5)
        }

        await handle_process_agent_update(heartbeat_args)
        tracker.reset_after_governance_update(agent_id)

    except Exception as e:
        logger.error(f"Error injecting heartbeat for {agent_id}: {e}", exc_info=True)


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any] | None) -> Sequence[TextContent]:
    """Handle tool calls from MCP client"""
    process_mgr.write_heartbeat()

    if arguments is None:
        arguments = {}

    # Proxy mode: forward to HTTP server
    if STDIO_PROXY_HTTP_URL:
        try:
            return await _proxy_http_call_tool(name, arguments)
        except Exception as e:
            logger.error(f"STDIO proxy call_tool failed (HTTP {STDIO_PROXY_HTTP_URL}) name={name}: {e}", exc_info=True)
            if STDIO_PROXY_STRICT:
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": f"STDIO proxy mode enabled but HTTP server unavailable for tool '{name}'.",
                        "details": {"proxy_url": STDIO_PROXY_HTTP_URL, "tool": name, "exception": str(e)}
                    }, indent=2)
                )]

    # Proxy mode: forward to MCP server
    if STDIO_PROXY_URL:
        try:
            return await _proxy_call_tool(name, arguments)
        except Exception as e:
            logger.error(f"STDIO proxy call_tool failed ({STDIO_PROXY_URL}) name={name}: {e}", exc_info=True)
            if STDIO_PROXY_STRICT:
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": f"STDIO proxy mode enabled but server unavailable for tool '{name}'.",
                        "details": {"proxy_url": STDIO_PROXY_URL, "tool": name, "exception": str(e)}
                    }, indent=2)
                )]

    # Activity tracking for auto-heartbeat
    agent_id = arguments.get('agent_id')
    if agent_id and HEARTBEAT_CONFIG.enabled:
        should_trigger, trigger_reason = activity_tracker.track_tool_call(agent_id, name)

        lightweight_tools = {
            "process_agent_update", "reply_to_question", "leave_note",
            "get_discovery_details", "search_knowledge_graph",
            "get_knowledge_graph", "list_knowledge_graph",
            "request_dialectic_review", "request_exploration_session",
            "submit_thesis", "submit_antithesis", "submit_synthesis",
            "get_dialectic_session", "dialectic",
        }
        if should_trigger and name not in lightweight_tools:
            try:
                activity = activity_tracker.get_or_create(agent_id)
                activity_summary = {
                    "conversation_turns": activity.conversation_turns,
                    "tool_calls": activity.tool_calls,
                    "files_modified": activity.files_modified,
                    "average_complexity": (
                        activity.cumulative_complexity / len(activity.complexity_samples)
                        if activity.complexity_samples else 0.5
                    ),
                    "duration_minutes": (
                        (datetime.now() - datetime.fromisoformat(activity.session_start))
                        .total_seconds() / 60
                        if activity.session_start else 0
                    )
                }
                from src.background_tasks import create_tracked_task
                create_tracked_task(
                    inject_lightweight_heartbeat(agent_id, trigger_reason, activity_summary, activity_tracker),
                    name="inject_lightweight_heartbeat",
                )
                logger.info(f"Auto-triggered heartbeat for {agent_id}: {trigger_reason}")
            except Exception as e:
                logger.warning(f"Could not inject heartbeat: {e}", exc_info=True)

    # Dispatch to handler registry; record tool_usage at each exit point
    # (JSONL tracker + fire-and-forget write to audit.tool_usage)
    t0 = time.monotonic()
    try:
        from src.mcp_handlers import dispatch_tool
        result = await dispatch_tool(name, arguments)
        latency_ms = int((time.monotonic() - t0) * 1000)
        if result is not None:
            success, error_type = classify_tool_result(result)
            record_tool_usage(tool_name=name, agent_id=agent_id, success=success,
                              error_type=error_type, latency_ms=latency_ms)
            return result
        record_tool_usage(tool_name=name, agent_id=agent_id, success=False,
                          error_type="unknown_tool", latency_ms=latency_ms)
        return [TextContent(type="text", text=json.dumps({"success": False, "error": f"Unknown tool: {name}"}, indent=2))]
    except ImportError:
        return [TextContent(type="text", text=json.dumps({"success": False, "error": f"Handler registry not available for tool '{name}'"}, indent=2))]
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        logger.error(f"Tool '{name}' execution error: {e}", exc_info=True)
        from src.mcp_handlers.utils import error_response as create_error_response
        sanitized_error = create_error_response(
            f"Error executing tool '{name}': {str(e)}",
            recovery={"action": "Check tool parameters and try again"}
        )
        record_tool_usage(tool_name=name, agent_id=agent_id, success=False,
                          error_type="execution_error", latency_ms=latency_ms)
        return [sanitized_error]


# ============================================================================
# Main Entry Point
# ============================================================================

async def main():
    """Main entry point for STDIO MCP server"""
    # Initialize server process (PID file, signal handlers, heartbeat)
    init_server_process()

    if STDIO_PROXY_URL or STDIO_PROXY_HTTP_URL:
        logger.info("Identity continuity mode: proxied (delegated to upstream HTTP server)")
    else:
        continuity_status = await probe_identity_continuity_status()
        continuity_message = format_identity_continuity_startup_message(continuity_status)
        if continuity_status.get("mode") == "redis":
            logger.info(continuity_message)
        else:
            logger.warning(continuity_message)

    async def startup_background_tasks():
        """Run startup tasks in background after server starts"""
        await asyncio.sleep(0.5)

        try:
            await load_metadata_async()
        except Exception as e:
            logger.warning(f"Could not load metadata in background: {e}", exc_info=True)

        # Startup orphan sweep removed 2026-04-19 — it was part of the
        # auto-archive behavior that hid initializing-agent bugs. Call the
        # archive_orphan_agents MCP tool manually (defaults to dry_run) if
        # wanted.

        try:
            from src.auto_ground_truth import collect_ground_truth_automatically, auto_ground_truth_collector_task
            result = await collect_ground_truth_automatically(min_age_hours=2.0, max_decisions=50, dry_run=False)
            if result.get('updated', 0) > 0:
                logger.info(f"Auto-collected ground truth: {result['updated']} decisions updated")
            from src.background_tasks import create_tracked_task
            create_tracked_task(
                auto_ground_truth_collector_task(interval_hours=6.0),
                name="auto_ground_truth_collector",
            )
        except Exception as e:
            logger.warning(f"Could not auto-collect ground truth: {e}", exc_info=True)

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, cleanup_stale_state_locks, project_root, 300, False)
            if result.get('cleaned', 0) > 0:
                logger.info(f"Cleaned {result['cleaned']} stale lock files")
        except Exception as e:
            logger.warning(f"Could not clean up stale locks: {e}", exc_info=True)

        try:
            from src.mcp_handlers.dialectic.handlers import load_all_sessions
            loaded_sessions = await load_all_sessions()
            if loaded_sessions > 0:
                logger.info(f"Restored {loaded_sessions} active dialectic session(s) from disk")
        except Exception as e:
            logger.warning(f"Could not load dialectic sessions: {e}", exc_info=True)

        try:
            from src.mcp_handlers.dialectic.session import run_startup_consolidation
            consolidation_result = await run_startup_consolidation()
            if consolidation_result.get('exported', 0) > 0:
                logger.info(f"Dialectic consolidation: exported {consolidation_result['exported']} sessions")
            if consolidation_result.get('synced', 0) > 0:
                logger.info(f"Dialectic consolidation: synced {consolidation_result['synced']} sessions")
        except Exception as e:
            logger.warning(f"Could not run dialectic consolidation: {e}", exc_info=True)

    try:
        async with stdio_server() as (read_stream, write_stream):
            async def safe_startup_background_tasks():
                try:
                    await startup_background_tasks()
                except Exception as e:
                    logger.warning(f"Background task error (non-critical): {e}", exc_info=True)

            # Only run background tasks in non-proxy mode
            if not (STDIO_PROXY_URL or STDIO_PROXY_HTTP_URL):
                from src.background_tasks import create_tracked_task
                create_tracked_task(
                    safe_startup_background_tasks(),
                    name="stdio_startup_background_tasks",
                )

            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options()
            )
    except ExceptionGroup as eg:
        def _flatten(ex):
            if isinstance(ex, ExceptionGroup):
                for sub in ex.exceptions:
                    yield from _flatten(sub)
            else:
                yield ex
        flat = list(_flatten(eg))
        normal_disconnect_types = (BrokenPipeError, ConnectionResetError, asyncio.CancelledError)
        if not any(isinstance(e, normal_disconnect_types) for e in flat):
            logger.error(f"TaskGroup error: {eg}")
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
    finally:
        remove_pid_file()


if __name__ == "__main__":
    asyncio.run(main())
