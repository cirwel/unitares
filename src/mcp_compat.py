"""Compatibility shim spanning the ``mcp`` SDK 1.x and 2.x lines.

Dependabot #1393 widened the ``mcp`` requirement to ``>=1.26.0,<3.0.0``, which
admits the 2.x major. 2.x is a ground-up rewrite of the SDK, but only two of its
breaking changes reach this codebase. This module resolves both so call sites
stay version-agnostic:

1. **The high-level server moved and was renamed.**
   - 1.x: ``mcp.server.fastmcp.FastMCP`` and ``mcp.server.fastmcp.Context``
   - 2.x: ``mcp.server.MCPServer``        and ``mcp.server.mcpserver.Context``

   ``MCPServer`` keeps 1.x's decorator API (``.tool()`` / ``.add_tool()``) and
   the ``_tool_manager`` + ``fn_metadata.arg_model`` internals this repo relies
   on for typed-wrapper registration and extra-argument passthrough. The
   internal high-level tool class moved with it
   (``...fastmcp.tools.base.Tool`` -> ``...mcpserver.tools.base.Tool``, exposed
   here as :data:`InternalTool`). What it drops are the ``host`` /
   ``transport_security`` constructor kwargs (host is
   applied at run time in 2.x) and it renames the low-level ASGI server
   attribute ``_mcp_server`` -> ``_lowlevel_server``. Use
   :func:`server_supports_kwarg` and :func:`lowlevel_server` to bridge those.

2. **``mcp.types.Tool`` renamed ``inputSchema`` -> ``input_schema``.**
   2.x keeps ``inputSchema`` as a validation/serialization alias with
   ``populate_by_name=True``, so *constructing* a ``Tool(inputSchema=...)`` still
   works on both lines — only *attribute access* changed. Read/write the schema
   through :func:`get_tool_input_schema` / :func:`set_tool_input_schema`.
"""

from __future__ import annotations

import inspect
from typing import Any

try:  # mcp 2.x
    from mcp.server import MCPServer as _ServerClass
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver.tools.base import Tool as InternalTool

    MCP_MAJOR = 2
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _ServerClass  # type: ignore[no-redef]
    from mcp.server.fastmcp import Context  # type: ignore[no-redef]
    from mcp.server.fastmcp.tools.base import Tool as InternalTool  # type: ignore[no-redef]

    MCP_MAJOR = 1

# Existing call sites construct the high-level server as ``FastMCP(...)``. Keep
# that name working regardless of the installed major.
FastMCP = _ServerClass

__all__ = [
    "FastMCP",
    "Context",
    "InternalTool",
    "MCP_MAJOR",
    "server_supports_kwarg",
    "get_tool_input_schema",
    "set_tool_input_schema",
    "lowlevel_server",
]


def server_supports_kwarg(name: str) -> bool:
    """Whether the resolved server class accepts ``name`` as a constructor kwarg.

    Lets a call site pass 1.x-only kwargs (``host``, ``transport_security``)
    conditionally instead of tripping ``TypeError`` on 2.x.
    """
    try:
        return name in inspect.signature(_ServerClass.__init__).parameters
    except (ValueError, TypeError):
        return False


def _tool_schema_attr(tool: Any) -> str:
    """Name of the attribute holding a tool's input schema on this object.

    For a real ``mcp.types.Tool`` the answer comes from the model's declared
    fields (``input_schema`` on 2.x, ``inputSchema`` on 1.x). Non-pydantic
    stand-ins (test mocks, plugin tool objects) have no ``model_fields`` dict —
    for those we prefer the legacy ``inputSchema`` name that callers set, so
    ``getattr`` returns the value they assigned rather than an auto-created
    ``Mock`` from probing the wrong name first.
    """
    fields = getattr(type(tool), "model_fields", None)
    if isinstance(fields, dict):
        if "input_schema" in fields:
            return "input_schema"
        if "inputSchema" in fields:
            return "inputSchema"
    return "inputSchema" if hasattr(tool, "inputSchema") else "input_schema"


def get_tool_input_schema(tool: Any, default: Any = None) -> Any:
    """Read an ``mcp.types.Tool`` input schema across 1.x/2.x.

    2.x exposes ``.input_schema``; 1.x exposes ``.inputSchema``. Returns
    ``default`` when the resolved attribute is unset.
    """
    val = getattr(tool, _tool_schema_attr(tool), None)
    return default if val is None else val


def set_tool_input_schema(tool: Any, value: Any) -> None:
    """Write an ``mcp.types.Tool`` input schema across 1.x/2.x."""
    setattr(tool, _tool_schema_attr(tool), value)


def lowlevel_server(mcp: Any) -> Any:
    """Return the low-level ASGI ``Server`` behind the high-level server.

    2.x names it ``_lowlevel_server``; 1.x named it ``_mcp_server``.
    """
    srv = getattr(mcp, "_lowlevel_server", None)
    if srv is None:
        srv = getattr(mcp, "_mcp_server", None)
    return srv


def make_lowlevel_server(
    name: str,
    *,
    list_tools: Any,
    call_tool: Any,
    list_resources: Any,
    read_resource: Any,
    resource_mime_type: str = "text/markdown",
) -> Any:
    """Build a low-level ``mcp.server.Server`` with request handlers, across 1.x/2.x.

    The four handlers use the plain 1.x return contract, which stays the single
    shape callers write to:

    - ``list_tools() -> list[Tool]``
    - ``call_tool(name: str, arguments: dict) -> Sequence[content blocks]``
    - ``list_resources() -> list[Resource]``
    - ``read_resource(uri: str) -> str``

    1.x registered these via ``@server.list_tools()`` / ``@server.call_tool()``
    style decorators that wrapped raw return values into result envelopes. 2.x
    removed those decorators: the low-level ``Server`` now takes ``on_*``
    constructor callbacks with an ``(request_context, params) -> Result``
    contract. This builder hides that split — on 2.x it adapts each plain
    handler into the ``on_*`` shape and wraps returns in the proper result type.
    """
    from mcp.server import Server

    if MCP_MAJOR >= 2:
        from mcp.types import (
            CallToolResult,
            ListResourcesResult,
            ListToolsResult,
            ReadResourceResult,
            TextResourceContents,
        )

        async def _on_list_tools(_ctx: Any, _params: Any) -> Any:
            return ListToolsResult(tools=list(await list_tools()))

        async def _on_call_tool(_ctx: Any, params: Any) -> Any:
            content = await call_tool(params.name, params.arguments or {})
            return CallToolResult(content=list(content))

        async def _on_list_resources(_ctx: Any, _params: Any) -> Any:
            return ListResourcesResult(resources=list(await list_resources()))

        async def _on_read_resource(_ctx: Any, params: Any) -> Any:
            text = await read_resource(params.uri)
            return ReadResourceResult(
                contents=[
                    TextResourceContents(
                        uri=params.uri, text=text, mime_type=resource_mime_type
                    )
                ]
            )

        return Server(
            name,
            on_list_tools=_on_list_tools,
            on_call_tool=_on_call_tool,
            on_list_resources=_on_list_resources,
            on_read_resource=_on_read_resource,
        )

    # mcp 1.x — the decorator API wraps raw return values for us.
    server = Server(name)
    server.list_resources()(list_resources)
    server.read_resource()(read_resource)
    server.list_tools()(list_tools)
    server.call_tool()(call_tool)
    return server
