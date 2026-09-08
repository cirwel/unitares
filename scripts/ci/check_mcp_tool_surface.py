#!/usr/bin/env python3
"""Check the running Compose server's discovery and named-call contract.

Run inside the governance-mcp container, using its installed MCP dependency:
    docker compose exec -T governance-mcp python - --mode minimal < scripts/ci/check_mcp_tool_surface.py

This checks the real HTTP mount, not only the in-process registration table.
The only tool call is the read-only list_tools introspection handler, which
must dispatch even when minimal mode does not advertise it.
"""

import argparse
import json

import anyio
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from src.mcp_compat import mcp_httpx
from src.tool_modes import get_tools_for_mode


async def check(mode: str, url: str) -> None:
    expected = get_tools_for_mode(mode)
    with anyio.fail_after(30):
        # The Compose-local probe must not inherit a host's outbound proxy.
        async with mcp_httpx().AsyncClient(timeout=15, trust_env=False) as http_client:
            async with streamable_http_client(url, http_client=http_client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    names = {tool.name for tool in listed.tools}
                    if names != expected:
                        raise RuntimeError(
                            f"{mode}: discovery mismatch; "
                            f"missing={sorted(expected - names)}, "
                            f"extra={sorted(names - expected)}"
                        )

                    result = await session.call_tool("list_tools", {"lite": True})
                    if getattr(result, "is_error", getattr(result, "isError", False)):
                        raise RuntimeError(f"{mode}: list_tools named call failed")
                    payload = json.loads(next(
                        block.text for block in result.content if block.type == "text"
                    ))
                    if payload.get("success") is False:
                        raise RuntimeError(f"{mode}: list_tools returned an application error")
                    shown = {tool["name"] for tool in payload["tools"]}
                    if shown != expected:
                        raise RuntimeError(f"{mode}: introspection disagrees with discovery")

    print(f"PASS: {mode} advertises {len(names)} tools; list_tools dispatches by name")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("minimal", "lite"), required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8767/mcp/")
    args = parser.parse_args()
    anyio.run(check, args.mode, args.url)
