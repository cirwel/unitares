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

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path

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
from src.versioning import load_build_sha_from_repo, load_version_from_file
logger = get_logger(__name__)

# Server readiness flag - prevents "request before initialization" errors
# when multiple clients reconnect simultaneously after a server restart
SERVER_READY = False
SERVER_STARTUP_TIME = None
SERVER_START_TIME = time.time()  # Track server start time for uptime metric

# Try to import MCP SDK
try:
    # mcp_compat resolves FastMCP (1.x) / MCPServer (2.x) behind one name.
    from src.mcp_compat import FastMCP, server_supports_kwarg
    from mcp.types import TextContent  # noqa: F401 — availability probe
    MCP_SDK_AVAILABLE = True
except ImportError as e:
    MCP_SDK_AVAILABLE = False
    print(f"Error: MCP SDK not available: {e}", file=sys.stderr)
    print("Install with: pip install mcp", file=sys.stderr)
    sys.exit(1)

# Tool dispatch, the Wave 3a routing hot path, session-id resolution, and tool
# registration now live in src/tool_registration.py (imported below, after the
# FastMCP instance is built).
# Tool schemas live in src/tool_schemas.py (shared module).


# ============================================================================
# Server Version (sync with VERSION file)
# ============================================================================

def _load_version():
    """Load version from VERSION file."""
    return load_version_from_file(project_root)

SERVER_VERSION = _load_version()
SERVER_BUILD_SHA = load_build_sha_from_repo(project_root)


# ============================================================================
# FastMCP Server Setup
# ============================================================================

from src.mcp_listen_config import (
    build_transport_security_settings,
    default_listen_host,
)

# --- OAuth 2.1 configuration (optional, enabled by env var) ---
_oauth_issuer_url = os.environ.get("UNITARES_OAUTH_ISSUER_URL")
_oauth_provider = None
_auth_settings = None
_OAUTH_REQUIRED_SCOPES = ["mcp:tools"]

if _oauth_issuer_url:
    try:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
        from src.oauth_provider import GovernanceOAuthProvider

        _oauth_secret = os.environ.get("UNITARES_OAUTH_SECRET")
        _auto_approve = os.environ.get("UNITARES_OAUTH_AUTO_APPROVE", "true").lower() in ("true", "1", "yes")
        _oauth_resource_url = (
            os.environ.get("UNITARES_OAUTH_RESOURCE_URL")
            or f"{_oauth_issuer_url.rstrip('/')}/mcp"
        )
        _oauth_provider = GovernanceOAuthProvider(secret=_oauth_secret, auto_approve=_auto_approve)
        _auth_settings = AuthSettings(
            issuer_url=_oauth_issuer_url,
            resource_server_url=_oauth_resource_url,
            required_scopes=_OAUTH_REQUIRED_SCOPES,
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
# mcp 1.x's FastMCP accepted host/transport_security at construction time; 2.x's
# MCPServer dropped both (host is applied at run time, transport security moved
# to the streamable-HTTP manager). Pass the 1.x-only kwargs only when supported.
_server_kwargs = dict(
    name="governance-monitor-v1",
    auth_server_provider=_oauth_provider,
    auth=_auth_settings,
)
if server_supports_kwarg("host"):
    _server_kwargs["host"] = _LISTEN_HOST
if server_supports_kwarg("transport_security"):
    _server_kwargs["transport_security"] = build_transport_security_settings()
mcp = FastMCP(**_server_kwargs)


# Custom decorator that disables outputSchema to avoid schema validation errors
# FastMCP auto-generates outputSchema based on return type, but our tools return
# complex dicts that don't match the simple {"result": string} schema.
def tool_no_schema(description: str):
    """Decorator for registering tools without outputSchema validation."""
    return mcp.tool(description=description, structured_output=False)
# ============================================================================
# Tool Registration (extracted to src/tool_registration.py)
# ============================================================================
# auto_register_all_tools / _register_common_aliases take the FastMCP instance
# as a parameter (dependency injection) to avoid a circular import: this module
# builds `mcp`; tool_registration builds the wrappers registered onto it.
from src.tool_registration import (
    auto_register_all_tools,
    _register_common_aliases,
)

auto_register_all_tools(mcp)
_register_common_aliases(mcp)

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
        help=f"Port to bind to (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force start: clean up any stale lock files and PID files",
    )
    return parser.parse_args()


async def main():
    """Start the governance server and own its lifecycle."""
    args = parse_args()

    from src.services.mcp_server_bootstrap import (
        ServerStartupError,
        bootstrap_server,
    )

    try:
        bootstrap = await bootstrap_server(
            force=args.force,
            host=args.host,
            port=args.port,
            version=SERVER_VERSION,
            project_root=Path(project_root),
            mcp=mcp,
        )
    except ServerStartupError as exc:
        print(f"\\n❌ Error: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"💡 Tip: {exc.hint}", file=sys.stderr)
        raise SystemExit(1) from exc

    try:
        from src.services.mcp_transport_service import (
            McpAuthConfig,
            build_transport_runtime,
        )

        def _set_server_ready() -> None:
            global SERVER_READY, SERVER_STARTUP_TIME
            SERVER_READY = True
            SERVER_STARTUP_TIME = datetime.now()

        runtime = build_transport_runtime(
            mcp,
            auth_config=McpAuthConfig(
                oauth_provider=_oauth_provider,
                auth_settings=_auth_settings,
                required_scopes=tuple(_OAUTH_REQUIRED_SCOPES),
            ),
            host=args.host,
            port=args.port,
            reload=args.reload,
            server_ready_fn=lambda: SERVER_READY,
            set_server_ready=_set_server_ready,
            server_start_time=SERVER_START_TIME,
            server_version=SERVER_VERSION,
            server_build_sha=SERVER_BUILD_SHA,
        )
        await runtime.serve()
    except ImportError:
        print(
            "Error: uvicorn not installed. Install with: pip install uvicorn",
            file=sys.stderr,
        )
        raise SystemExit(1)
    except Exception as exc:
        logger.error("Server error: %s", exc, exc_info=True)
        raise SystemExit(1) from exc
    finally:
        await bootstrap.shutdown()


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
