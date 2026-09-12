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

def load_server_env() -> bool:
    """Load ~/.env.mcp into os.environ. Called from the entrypoint, NEVER at import.

    ⛔This used to run at module scope, and that made merely importing this
    module a side effect on the whole process. Measured 2026-09-09 in a clean
    interpreter: GITHUB_TOKEN absent before `import src.mcp_server`, present
    after, along with DB_POSTGRES_URL pointing at the PRODUCTION `governance`
    database. Three consequences, none of them intended:

      * os.environ is inherited by every subprocess spawned afterwards, so a
        live GitHub token and the production DSN reached child processes that
        have no business holding either -- including test fixtures that do
        `env = os.environ.copy()` and spawn a server.
      * It fired for any importer, including tests whose body touches no
        environment variable at all and merely imports this module for a symbol.
      * It silently pointed the local suite at the production database instead
        of governance_test.

    Safe to defer because nothing reads these at import time: the DSN is read
    in PostgresBackend.__init__ (db/postgres_backend.py) and DB_BACKEND inside
    functions in knowledge_graph.py, all of which run long after main() starts.
    ⛔If you add a module-level `os.getenv` for one of these, this deferral
    breaks silently -- read it inside a function instead.

    Returns True if a file was loaded, for callers that want to log it.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    env_path = Path.home() / ".env.mcp"
    if not env_path.exists():
        return False
    load_dotenv(env_path)
    return True


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
    auth_gate_refusal,
    build_transport_security_settings,
    default_listen_host,
)

# --- OAuth 2.1 configuration (optional, enabled by env var) ---
_oauth_issuer_url = os.environ.get("UNITARES_OAUTH_ISSUER_URL")
_oauth_provider = None
_auth_settings = None
_OAUTH_REQUIRED_SCOPES = ["mcp:tools"]

_oauth_setup_error: Exception | None = None

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
        # An operator who set the issuer URL asked for an auth gate. Serving
        # /mcp unauthenticated anyway answers a different question than the one
        # they asked, so the route now closes instead: authorize_mcp_request
        # reads gate_unavailable and answers 503. The process stays up, because
        # every other surface here has its own gate and none of them depend on
        # OAuth. A bearer allowlist still overrides the closure.
        #
        # Only the exception TYPE is printed. AuthSettings is a pydantic model
        # and a ValidationError echoes the offending input verbatim, so a
        # resource/issuer URL carrying userinfo would otherwise land in a log
        # that gets pasted into issues.
        _oauth_setup_error = e
        print(
            "[FastMCP] WARNING: OAuth setup FAILED — the MCP route has NO AUTH GATE "
            f"and is now CLOSED (503) rather than served open ({type(e).__name__})",
            file=sys.stderr, flush=True,
        )
        print(
            "[FastMCP] WARNING: set UNITARES_MCP_BEARER_TOKENS for a credential "
            "that does not depend on OAuth and reopens /mcp, or fix the OAuth "
            "configuration and restart. UNITARES_OAUTH_REQUIRED=1 additionally "
            "refuses to start the process at all.",
            file=sys.stderr, flush=True,
        )
        _oauth_provider = None
        _auth_settings = None

# Fail closed, for deployments that would rather be down than open. The
# predicate lives in mcp_listen_config so it can be exercised directly; the
# flag is read there, outside this module's issuer branch, so a misspelled
# issuer variable cannot silently disarm it.
#
# The cost, stated rather than discovered: this raises during module import, so
# the process exits before binding and launchd — KeepAlive with no
# ThrottleInterval — respawns it every ten seconds indefinitely. That outage is
# NOT self-announcing. src/mcp_compat.py records the precedent: a startup death
# here reads as healthy on any surface whose status is inferred from a sibling
# port, and the gateway's /health on :8768 is hardcoded to "ok".
#
# It is also wider than the MCP route. This process also serves /health*, the
# /v1 REST surface, the EISV websocket, the dashboard and the UDS resident
# listener, each with its own independent gate and none of them dependent on
# OAuth. Refusing to start takes all of them down for a fault in one. That blast
# radius is why this is opt-in rather than the default, and why a route-scoped
# refusal (503 on /mcp alone) is the better long-term shape.
_auth_refusal = auth_gate_refusal(
    provider_present=_oauth_provider is not None,
    issuer_set=bool(_oauth_issuer_url),
    setup_error_name=type(_oauth_setup_error).__name__ if _oauth_setup_error else None,
)
if _auth_refusal:
    raise RuntimeError(_auth_refusal)

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
# Connect-time orientation describes the complete catalog and primary workflow.
# It must remain registry-free: handlers have not loaded at this point.
if server_supports_kwarg("instructions"):
    from src.tool_modes import build_server_instructions

    _server_kwargs["instructions"] = build_server_instructions()
# serverInfo.version defaults to "" on both majors, so without this every
# client — directory crawlers included — sees a server that will not say
# which build it is. SERVER_VERSION already reads the VERSION file above.
if server_supports_kwarg("version"):
    _server_kwargs["version"] = SERVER_VERSION
if server_supports_kwarg("host"):
    _server_kwargs["host"] = _LISTEN_HOST
if server_supports_kwarg("transport_security"):
    _server_kwargs["transport_security"] = build_transport_security_settings()
# The listing wrapper compacts schema annotations without hiding capabilities.
from src.tool_mode_listing import mode_filtered_server_class

mcp = mode_filtered_server_class(FastMCP)(**_server_kwargs)


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
                # A configured gate that failed to build closes /mcp rather
                # than serving it open. Scoped to the route: every other
                # surface on this process keeps its own gate.
                gate_unavailable=_oauth_setup_error is not None,
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

    # Load operator env here, at the entrypoint, so importing this module for a
    # symbol does not inject secrets into the process. See load_server_env().
    load_server_env()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer stopped.")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
