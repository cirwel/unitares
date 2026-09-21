"""Progressive MCP discovery with compact schema annotations.

Every public tool remains registered and dispatchable.  The listing alone is
filtered, and the progressive surface carries ``use_tool`` so schema-driven
clients can discover and invoke capabilities omitted from the initial list.
"""

from __future__ import annotations

import copy
from typing import Any, Iterable, Optional

from src.logging_utils import get_logger
from src.mcp_compat import get_tool_input_schema, set_tool_input_schema
from src.schema_brief import (
    apply_null_default_mode,
    apply_property_title_mode,
    resolve_null_default_mode,
    resolve_property_title_mode,
)

logger = get_logger(__name__)


def advertised_tool_names(mode: Optional[str] = None) -> Optional[set[str]]:
    """Visible names ``tools/list`` may show for this advertisement mode.

    Full mode still needs a concrete allowlist: the mounted FastMCP table can
    contain internal ``hidden=True`` handlers, which are dispatchable by the
    server but must never become part of public discovery.
    """
    from src import tool_modes

    resolved = (mode or tool_modes.TOOL_MODE or "full").lower()
    from src.interface_contract import get_public_tool_definitions

    surface_mode = "progressive" if resolved == "progressive" else "full"
    names = {tool.name for tool in get_public_tool_definitions(surface_mode)}
    return names


def filter_listed_tools(tools: Iterable[Any], mode: Optional[str] = None) -> list[Any]:
    """Keep the tools ``mode`` advertises, preserving the listing order."""
    listed = list(tools)
    names = advertised_tool_names(mode)
    if names is None:
        return listed
    return [tool for tool in listed if getattr(tool, "name", None) in names]


def apply_listed_schema_policy(tools: Iterable[Any]) -> list[Any]:
    """Apply generated-annotation policy on every advertised listing.

    The registrar hands FastMCP the catalog schema with generated titles and
    null defaults still present (``get_tool_definitions(...="keep")`` in
    ``src/tool_registration.py``), and this is where they come off — per call,
    reading the modes each time, so both
    ``UNITARES_TOOL_SCHEMA_PROPERTY_TITLES=keep`` and
    ``UNITARES_TOOL_SCHEMA_NULL_DEFAULTS=keep`` stay reversible without
    depending on a cached schema built under a previous setting. Only the
    advertised Tool objects are copied; the argument models and dispatch
    validators stay intact. After this step the ``/mcp/`` listing is the
    catalog schema byte for byte
    (tests/test_mcp_schema_parity.py).
    """
    title_mode = resolve_property_title_mode()
    null_default_mode = resolve_null_default_mode()
    result = []
    for tool in tools:
        schema = get_tool_input_schema(tool)
        if (
            schema is None
            or (title_mode == "keep" and null_default_mode == "keep")
        ):
            result.append(tool)
            continue
        advertised = copy.copy(tool)
        schema = apply_property_title_mode(schema, title_mode)
        schema = apply_null_default_mode(schema, null_default_mode)
        set_tool_input_schema(advertised, schema)
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
