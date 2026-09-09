"""Complete MCP discovery with compact schema annotations.

Legacy class/function names are retained for embedded hosts. Mode arguments
are ignored; registration determines reachability and discovery alike.
"""

from __future__ import annotations

import copy
from typing import Any, Iterable, Optional

from src.logging_utils import get_logger
from src.mcp_compat import get_tool_input_schema, set_tool_input_schema
from src.schema_brief import apply_property_title_mode, resolve_property_title_mode

logger = get_logger(__name__)


def advertised_tool_names(mode: Optional[str] = None) -> Optional[set[str]]:
    """Compatibility API: the complete registered MCP catalog is unfiltered.

    Also preserves tools contributed by installed federation plugins; a static
    first-party allowlist must never hide a dynamically registered capability.
    """
    return None


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
