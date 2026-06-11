#!/usr/bin/env python3
"""UNITARES Gateway MCP Server — simplified proxy to full governance.

Exposes 6 simple tools on port 8768, proxies to the full 76-tool server on 8767.
Designed for weaker MCP clients (Perplexity, Discord bots, smaller models).

Usage:
    python src/gateway_server.py [--port PORT] [--host HOST]

    Default: http://127.0.0.1:8768/mcp/
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

# Ensure src/ is on sys.path so `from gateway.x` works regardless of cwd
_src_dir = str(Path(__file__).resolve().parent)
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from mcp.server.fastmcp import FastMCP

from gateway.client import GovernanceMCPClient
from gateway.constants import GATEWAY_HOST, GATEWAY_PORT, GOVERNANCE_URL
from gateway import tools

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("gateway")

# FastMCP server
mcp = FastMCP(
    name="unitares-gateway",
    host=GATEWAY_HOST,
)

# Shared client instance (created at startup)
_client: GovernanceMCPClient | None = None


def _get_client() -> GovernanceMCPClient:
    global _client
    if _client is None:
        _client = GovernanceMCPClient(url=GOVERNANCE_URL)
    return _client


# -- Tool registrations --

@mcp.tool(description="Get agent EISV state, coherence, verdict, basin. Pass agent_id to identify yourself or query a specific agent.")
async def status(agent_id: Optional[str] = None) -> str:
    return await tools.handle_status(_get_client(), agent_id=agent_id)


@mcp.tool(description="Report work and get a governance verdict. agent_id=your identity, summary=what you did, complexity=0-1 (default 0.5), confidence=0-1 (default 0.7).")
async def checkin(summary: str, agent_id: Optional[str] = None, complexity: float = 0.5, confidence: float = 0.7) -> str:
    return await tools.handle_checkin(_get_client(), summary=summary, complexity=complexity, confidence=confidence, agent_id=agent_id)


@mcp.tool(description="Search the shared knowledge graph. Returns matching discoveries, notes, and findings.")
async def search(query: str, limit: int = 5, agent_id: Optional[str] = None) -> str:
    return await tools.handle_search(_get_client(), query=query, limit=limit, agent_id=agent_id)


@mcp.tool(description="Leave a note or discovery in the knowledge graph. tags=comma-separated (optional).")
async def note(content: str, tags: Optional[str] = None, agent_id: Optional[str] = None) -> str:
    return await tools.handle_note(_get_client(), content=content, tags=tags, agent_id=agent_id)


@mcp.tool(description="Natural language gateway — ask any question and it gets routed to the right tool automatically.")
async def query(question: str, agent_id: Optional[str] = None) -> str:
    return await tools.handle_query(_get_client(), question=question, agent_id=agent_id)


@mcp.tool(description="List all gateway tools with descriptions and examples.")
async def help() -> str:
    return tools.handle_help()


# -- Entry point --

def parse_args():
    parser = argparse.ArgumentParser(description="UNITARES Gateway MCP Server")
    parser.add_argument("--host", default=GATEWAY_HOST, help=f"Host to bind to (default: {GATEWAY_HOST})")
    parser.add_argument("--port", type=int, default=GATEWAY_PORT, help=f"Port to bind to (default: {GATEWAY_PORT})")
    parser.add_argument("--governance-url", default=GOVERNANCE_URL, help=f"Governance server URL (default: {GOVERNANCE_URL})")
    return parser.parse_args()


def main():
    args = parse_args()

    global _client
    _client = GovernanceMCPClient(url=args.governance_url)

    logger.info("UNITARES Gateway starting on %s:%d", args.host, args.port)
    logger.info("Proxying to governance at %s", args.governance_url)
    logger.info("Tools: status, checkin, search, note, query, help")

    mcp.settings.host = args.host
    mcp.settings.port = args.port

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nGateway stopped.")
        sys.exit(0)
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)
        sys.exit(1)
