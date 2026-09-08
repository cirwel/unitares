"""Advertise-only tool modes for the FastMCP ``/mcp/`` mount.

``GOVERNANCE_TOOL_MODE`` decides what ``tools/list`` shows. It must not decide
what dispatches. Until the 2026-09 surface cut the FastMCP registrars applied
the mode at *registration* time, so on the streamable-HTTP mount a
``register=True`` handler outside the mode came back ``Unknown tool`` (verified
2026-08-11 against the deployed server; see
``tests/test_lite_wire_surface.py``). REST ``/v1/tools/call`` and the stdio
server never had that coupling: both dispatch any registered name and filter
only their listing. This module gives the FastMCP mount the same shape:

- ``src/tool_registration.py`` registers every ``register=True`` handler and
  every workflow alias with FastMCP regardless of mode.
- :func:`mode_filtered_server_class` wraps the high-level server so its
  ``list_tools()`` advertises only the mode's public surface
  (``src.interface_contract.get_public_tool_definitions``), the same predicate
  the REST and stdio listings use.

The filter fails open toward *listing*: if the advertised surface cannot be
computed, the full registered surface is advertised rather than nothing.
Hiding everything would strand a schema-driven client; over-listing costs
orientation noise only.
"""

from __future__ import annotations

import copy
from typing import Any, Iterable, Optional

from src.logging_utils import get_logger
from src.mcp_compat import get_tool_input_schema, set_tool_input_schema
from src.schema_brief import apply_property_title_mode, resolve_property_title_mode

logger = get_logger(__name__)


def advertised_tool_names(mode: Optional[str] = None) -> Optional[set[str]]:
    """Names ``tools/list`` may show in ``mode``; ``None`` means unfiltered.

    ``mode`` defaults to the live ``src.tool_modes.TOOL_MODE`` read at call
    time (not import time) so a process that changes the mode - tests do -
    sees the change on the next listing. ``full`` and an empty computed
    surface both return ``None``: an empty set means the surface could not be
    determined, never that the deployment offers nothing.
    """
    from src import tool_modes

    resolved = (mode or tool_modes.TOOL_MODE or "full").lower()
    if resolved == "full":
        return None

    from src.interface_contract import get_public_tool_definitions

    names = {tool.name for tool in get_public_tool_definitions(resolved)}
    if not names:
        logger.warning(
            "tool mode %r advertises no tools; listing the full registered "
            "surface instead of an empty tools/list",
            resolved,
        )
        return None
    return names


def filter_listed_tools(tools: Iterable[Any], mode: Optional[str] = None) -> list[Any]:
    """Keep the tools ``mode`` advertises, preserving the listing order."""
    listed = list(tools)
    names = advertised_tool_names(mode)
    if names is None:
        return listed
    return [tool for tool in listed if getattr(tool, "name", None) in names]


def apply_listed_schema_policy(tools: Iterable[Any]) -> list[Any]:
    """Apply annotation policy after FastMCP regenerates its argument schemas.

    Trimming the source catalog alone does not trim MCP: Pydantic puts titles
    back when the registrar builds typed wrappers. Only copy the advertised
    Tool objects here; the argument models and dispatch validators stay intact.
    Reading the mode on each listing also makes ``keep`` reversible without
    depending on a cached schema built under a previous setting.
    """
    mode = resolve_property_title_mode()
    result = []
    for tool in tools:
        schema = get_tool_input_schema(tool)
        if schema is None or mode == "keep":
            result.append(tool)
            continue
        advertised = copy.copy(tool)
        set_tool_input_schema(advertised, apply_property_title_mode(schema, mode))
        result.append(advertised)
    return result


def mode_filtered_server_class(base: type) -> type:
    """Subclass ``base`` so ``list_tools()`` advertises only the mode's surface.

    Works on both supported mcp majors: 1.x binds ``self.list_tools`` into the
    low-level ``ListToolsRequest`` handler at construction, 2.x routes
    ``on_list_tools`` through ``self.list_tools()``. Overriding the method on a
    subclass covers both; ``call_tool`` is untouched, so every registered name
    still dispatches.
    """

    class ModeFilteredServer(base):  # type: ignore[misc,valid-type]
        async def list_tools(self, *args: Any, **kwargs: Any):
            listed = await super().list_tools(*args, **kwargs)
            try:
                return apply_listed_schema_policy(filter_listed_tools(listed))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "tools/list mode filter failed; advertising the full "
                    "registered surface: %s",
                    exc,
                )
                return listed

    ModeFilteredServer.__name__ = f"ModeFiltered{base.__name__}"
    ModeFilteredServer.__qualname__ = ModeFilteredServer.__name__
    return ModeFilteredServer
