"""
Tool introspection handlers (list_tools, describe_tool).

Extracted from admin.py for maintainability.
"""

import json
from typing import Dict, Any, List, Sequence
from mcp.types import TextContent
from src.mcp_compat import get_tool_input_schema
from ..utils import success_response, error_response
from ..decorators import mcp_tool
from ..support.coerce import coerce_bool
from . import tool_catalog
from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
logger = get_logger(__name__)

import time as _time

# Progressive-ordering usage cache. list_tools(progressive=True) reads tool-call
# counts to order the tool list; reading audit.tool_usage per call would put a
# ~100ms windowed aggregate on the discovery path, so the {tool: stats} map is
# cached briefly. Keyed by window_hours -> (monotonic_expiry, tools_dict).
_USAGE_ORDER_CACHE: Dict[float, Any] = {}
_USAGE_ORDER_TTL_S = 120.0


async def _usage_tools_for_ordering(window_hours: float = 168) -> Dict[str, Dict[str, Any]]:
    """Tool-call stats for progressive ordering — DB-first (audit.tool_usage),
    TTL-cached, JSONL fallback. Returns ``{tool: {total_calls, ...}}`` (empty on
    any failure so ordering degrades to tier-based)."""
    now = _time.monotonic()
    cached = _USAGE_ORDER_CACHE.get(window_hours)
    if cached and cached[0] > now:
        return cached[1]
    tools: Dict[str, Any] = {}
    try:
        from src.audit_db import get_tool_usage_stats_async
        stats = await get_tool_usage_stats_async(window_hours=window_hours)
        if stats is None:
            from src.tool_usage_tracker import get_tool_usage_tracker
            stats = get_tool_usage_tracker().get_usage_stats(window_hours=window_hours)
        tools = stats.get("tools", {}) if isinstance(stats, dict) else {}
    except Exception:
        tools = {}
    _USAGE_ORDER_CACHE[window_hours] = (now + _USAGE_ORDER_TTL_S, tools)
    return tools


def _describe_tool_deprecation_block(tool_name: str) -> Dict[str, Any] | None:
    return tool_catalog.describe_tool_deprecation_block(tool_name)


def _describe_operation(requested_tool_name: str, tool_name: str, alias_info: Any) -> str:
    """read / write / admin for the name the caller asked about.

    A roster name (registered tool or workflow alias) has its own class. A
    legacy alias reports the class of the action it pins when that is narrower
    than its router's (list_agents reads; the agent router also deletes), else
    the canonical tool's.
    """
    from src.tool_modes import TOOL_OPERATIONS

    if requested_tool_name in TOOL_OPERATIONS:
        return TOOL_OPERATIONS[requested_tool_name]
    if alias_info is not None and alias_info.operation:
        return alias_info.operation
    return TOOL_OPERATIONS.get(tool_name, "read")


def _alias_role(alias_info: Any) -> str:
    """A workflow name is the primary surface; every other alias is compatibility."""
    return "primary_agent_workflow" if alias_info.experience else "compatibility_alias"


def _alias_block(requested_tool_name: str, alias_info: Any) -> Dict[str, Any]:
    """The ``alias`` block of a describe_tool response; one shape for every response mode.

    ``deprecated_since`` is the date the old name stopped being canonical
    (``ToolAlias.deprecated_since``); a workflow or intuitive alias carries
    none, because that name was never canonical.
    """
    since = alias_info.deprecated_since
    return {
        "reason": alias_info.reason,
        "role": _alias_role(alias_info),
        "primary_tool": requested_tool_name,
        "implementation_tool": alias_info.new_name,
        "canonical_tool": alias_info.new_name,
        "injected_action": alias_info.inject_action,
        "deprecated_since": since.date().isoformat() if since else None,
        "note": alias_info.migration_note,
    }


def _resolve_json_schema_type(field_info: Dict[str, Any]) -> str:
    """Render a Pydantic/JSON Schema field's type for lite-mode param lists.

    Pydantic emits Optional[T] as ``{"anyOf": [{"type": T}, {"type": "null"}]}``
    with no top-level ``type`` key. A naive ``.get("type", "any")`` therefore
    collapses every Optional field to "any", which defeats the purpose of lite
    mode — agents use lite schemas to shape arguments.

    Resolution:
      - top-level ``type`` wins if present
      - otherwise, walk ``anyOf`` and collect non-null ``type``s
        - 1 non-null type → return it (the Optional case)
        - 2+ non-null types → join with ``|`` (the Union case)
        - 0 non-null types or no shape info → fall back to "any"
    """
    t = field_info.get("type")
    if isinstance(t, str):
        return t
    any_of = field_info.get("anyOf")
    if isinstance(any_of, list):
        non_null = [variant.get("type") for variant in any_of
                    if isinstance(variant, dict)
                    and variant.get("type")
                    and variant.get("type") != "null"]
        if len(non_null) == 1:
            return non_null[0]
        if len(non_null) > 1:
            return "|".join(non_null)
    return "any"


def _format_lite_parameter(
    field_name: str,
    field_info: Dict[str, Any],
    *,
    required: bool = False,
    required_at_call_time: bool = False,
) -> str:
    if required_at_call_time:
        # The schema marks it optional (a router's flat schema can only
        # require `action`), so a client validating against the schema will
        # not catch its absence; the handler will.
        return f"{field_name} (required at call time)"
    if required:
        return f"{field_name} (required)"

    field_type = _resolve_json_schema_type(field_info)
    default = field_info.get("default")
    if default is not None:
        return f"{field_name}: {field_type} (default: {default})"
    return f"{field_name}: {field_type}"

def _not_advertised_summary(tools_list, mode: str) -> dict:
    """Explain capabilities omitted from the initial advertisement."""
    names = sorted(t["name"] for t in tools_list if not t.get("advertised", True))
    return {
        "count": len(names), "tools": names, "mode": mode,
        "reason": "Public capabilities omitted from the initial progressive tools/list advertisement",
        "note": "Discover with list_tools, inspect with describe_tool, and invoke with use_tool; set UNITARES_TOOL_ADVERTISEMENT=full to advertise every schema up front.",
    }


def _orientation_description(
    tool_name: str,
    wire_descriptions: Dict[str, str],
    catalog_descriptions: Dict[str, str],
) -> str:
    """The one-line description list_tools serves for ``tool_name``.

    An advertised name gets the first line of the description ``tools/list``
    serves for it, so the two discovery surfaces cannot disagree about the
    same tool. Until 2026-09-12 ``tool_catalog.TOOL_DESCRIPTION_OVERRIDES``
    outranked the wire here, and the rewrites of #2148, #2151 and #2158
    corrected what an MCP client read while orientation kept the old
    one-liners for 28 of the 50 advertised names (F2 of
    docs/operations/tool-surface-audit-2026-09-12.md). The override table now
    carries dispatch-only alias names only, which this listing never shows,
    so it is not consulted.

    A registered name the deployment does not advertise (a plugin tool
    registered after the server mounted its table) keeps the pre-existing
    fallback chain minus the override: the alias note below, then the schema
    catalog, then the decorator description, then a generic placeholder.

    The alias link is load-bearing rather than defensive. A workflow alias is
    in ``registered_tool_names`` unconditionally, but it reaches
    ``wire_descriptions`` only through ``build_alias_tool_definition``, which
    ``get_public_tool_definitions`` skips with ``except KeyError`` when the
    alias's implementation tool is missing from the schema catalog — the
    partial-catalog case that module documents as deliberately supported for
    embedded consumers. An alias is in neither the schema catalog nor the
    decorator registry, so without this link all eight would render as
    ``Tool: sync_state`` there, where the pre-2026-09-12 override table showed
    a curated line. ``migration_note`` is the same authority the wire itself
    would have used, so the degraded surface now says what the healthy one says.

    Note on a failure mode this does NOT cover, so nobody re-derives it: if
    ``get_public_tool_definitions`` were to RAISE, this helper is never reached.
    ``get_interface_contract_summary`` calls the same function unguarded a few
    lines earlier in the handler, so the whole call fails first. The reachable
    degradation is a partial return, not an unavailable catalog.
    """
    from src.tool_schemas import first_line
    from ..decorators import get_tool_description
    from ..tool_stability import resolve_tool_alias

    description = wire_descriptions.get(tool_name)
    if not description:
        _, alias_info = resolve_tool_alias(tool_name)
        if alias_info is not None:
            description = alias_info.migration_note
    description = (
        description
        or catalog_descriptions.get(tool_name)
        or get_tool_description(tool_name)
    )
    return first_line(description) or f"Tool: {tool_name}"


def _registered_public_tool_names() -> list[str]:
    """Return the live public dispatch index used by discovery and gateway.

    Entry-point plugins can register after the mounted MCP schema table was
    built, and a partial mount can omit a schema while leaving its handler
    callable. The decorator/handler registry is therefore the authority for
    the complete on-demand capability index; both ``list_tools`` and
    ``use_tool`` must consult the same refreshed snapshot.
    """
    from src.interface_contract import get_public_tool_definitions
    from src.mcp_handlers import refresh_tool_handlers_from_registry

    refresh_tool_handlers_from_registry()
    return [
        tool.name
        for tool in get_public_tool_definitions(
            "full", include_unmounted=True
        )
    ]



@mcp_tool("list_tools", timeout=10.0, requires_identity="pre_onboard")
async def handle_list_tools(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """List the governance capability catalog (lite: names only; full: descriptions and categories).
    
    Parameters:
        essential_only (bool): If true, return only Tier 1 (essential) tools (default: false)
        include_advanced (bool): If false, exclude Tier 3 (advanced) tools (default: true)
        tier (str): Filter by tier: "essential", "common", "advanced", or "all" (default: "all")
        category (str): Filter by catalog category, for example "dialectic" or "knowledge" (default: "all")
        lite (bool): If true, return the compact federation handshake: one name-only record per public capability, the interface contract and continuation hints (default: true)
        progressive (bool): If true, order tools by usage frequency (most used first). Works with all filter modes. Default false.
        verbose (bool): Ignored; accepted for compatibility.
    """
    
    # Get actual registered tools from TOOL_HANDLERS registry. Entry-point
    # plugins can be registered after this module took its initial snapshot
    # (notably in embedded/test hosts); synchronize the decorator registry
    # first so orientation never advertises a tool the dispatcher cannot yet
    # resolve. The normal server bootstrap performs the same idempotent step.
    from ..tool_stability import AGENT_WORKFLOW_ALIASES
    registered_tool_names = _registered_public_tool_names()
    
    # Parse filter parameters (handle string booleans from MCP transport)
    essential_only = coerce_bool(arguments.get("essential_only"), False)
    include_advanced = coerce_bool(arguments.get("include_advanced"), True)
    raw_tier = arguments.get("tier")
    tier_filter = (
        "all" if raw_tier is None else (str(raw_tier).strip().lower() or "all")
    )
    valid_tiers = ("all", "essential", "common", "advanced")
    if tier_filter not in valid_tiers:
        return [error_response(
            "tier must be one of: all, essential, common, advanced",
            error_code="INVALID_TIER",
            error_category="validation_error",
            recovery={"allowed_values": list(valid_tiers)},
        )]
    category_filter = str(arguments.get("category") or "all").strip().lower() or "all"
    # LITE-FIRST: Default to minimal response for local/smaller models
    lite_mode = coerce_bool(arguments.get("lite"), True)
    # Progressive disclosure: Order tools by usage frequency
    progressive = coerce_bool(arguments.get("progressive"), False)
    
    # Import public-surface metadata from their single sources of truth.
    from src.interface_contract import (
        get_interface_contract_summary,
        get_public_tool_definitions,
    )
    from src.tool_modes import TOOL_MODE, TOOL_TIERS
    interface_contract = get_interface_contract_summary()

    # list_tools is the complete capability index even when the initial MCP
    # advertisement is progressive.  Keep a second set so the rich view can
    # say which names a schema-driven client received directly.
    try:
        public_definitions = list(
            get_public_tool_definitions("full", include_unmounted=True)
        )
        directly_advertised = list(get_public_tool_definitions(TOOL_MODE))
    except Exception:
        public_definitions = []
        directly_advertised = []
    advertised_names = {tool.name for tool in directly_advertised} or None
    # An unavailable/empty schema catalog fails open to registration.
    wire_descriptions = {
        tool.name: tool.description or "" for tool in public_definitions
    }
    public_names = set(wire_descriptions) or None

    # Deprecated tools - hidden from list_tools by default.
    # Two independent sources, and both are needed:
    #   - tool_stability.py alias keys: legacy names that resolve elsewhere.
    #   - @mcp_tool(deprecated=True): a tool that keeps its OWN handler while
    #     declaring itself superseded. Alias membership used to be the only
    #     source, which quietly made "is deprecated" mean "has an alias" --
    #     so dropping a deprecated tool's alias promoted it INTO orientation.
    #     direct_resume_if_safe hit exactly that when its broken alias was
    #     removed (2026-08-29).
    from ..tool_stability import list_all_aliases
    from ..decorators import _TOOL_DEFINITIONS
    deprecated_aliases = (
        set(list_all_aliases().keys()) - set(AGENT_WORKFLOW_ALIASES)
    )
    deprecated_handlers = {
        name for name, definition in _TOOL_DEFINITIONS.items()
        if definition.deprecated
    }
    _deprecated = deprecated_aliases | deprecated_handlers
    # ...but never hide a name this deployment exposes in its complete public
    # catalog. list_tools is the negotiation index even when tools/list starts
    # with the progressive subset; omitting a public deprecated plugin here
    # makes the contract and gateway name a capability discovery cannot find.
    #
    # This exemption was introduced because leave_note carried
    # deprecated=True/superseded_by="knowledge" while still sitting in
    # LITE_MODE_TOOLS and in the shared contract as a first-class tool, so
    # folding the decorator flag in silently dropped it from orientation. The
    # operator has since settled that contradiction the other way -- leave_note
    # is not deprecated and no longer carries the flag -- so the exemption is
    # no longer load-bearing for any tool shipping today. It stays as the
    # general rule: whatever this deployment exposes publicly, orientation
    # lists.
    #
    # In the degraded path (public surface unavailable) fall back to the
    # pre-2026-08-29 rule exactly -- alias keys only -- so an unavailable
    # advertised set can never hide a tool that the decorator flag alone
    # would suppress.
    DEPRECATED_TOOLS = deprecated_aliases | (
        set()
        if public_names is None
        else deprecated_handlers - public_names
    )

    tool_relationships = tool_catalog.TOOL_RELATIONSHIPS
    workflows = tool_catalog.WORKFLOWS

    # Build tools list from registered tools with metadata from decorators
    from ..decorators import get_tool_timeout
    # The schema catalog is the description fallback for a registered name
    # the deployment does not advertise; an advertised name reads the wire
    # definition itself (wire_descriptions).
    from src.tool_schemas import get_tool_definitions
    schema_tools = {t.name: t.description or "" for t in get_tool_definitions()}

    tools_list = []
    for tool_name in registered_tool_names:
        description = _orientation_description(
            tool_name, wire_descriptions, schema_tools
        )

        # Determine tool tier
        tool_tier = "common"  # Default
        if tool_name in TOOL_TIERS["essential"]:
            tool_tier = "essential"
        elif tool_name in TOOL_TIERS["common"]:
            tool_tier = "common"
        elif tool_name in TOOL_TIERS["advanced"]:
            tool_tier = "advanced"
        
        # Apply filters
        # Hide deprecated tools by default (they still work, just not shown)
        if tool_name in DEPRECATED_TOOLS:
            continue
        if essential_only and tool_tier != "essential":
            continue
        if not include_advanced and tool_tier == "advanced":
            continue
        if tier_filter != "all" and tool_tier != tier_filter:
            continue
        if category_filter != "all":
            relationship = tool_relationships.get(tool_name) or {}
            if relationship.get("category") != category_filter:
                continue
        
        tool_info = {
            "name": tool_name,
            "description": description,
            "tier": tool_tier,
            # False = registered and dispatchable by name, but NOT on this
            # deployment's MCP wire, so a schema-driven client cannot call it.
            "advertised": (
                True if advertised_names is None else tool_name in advertised_names
            ),
        }
        if tool_name in _deprecated:
            tool_info["deprecated"] = True
            superseded_by = getattr(
                _TOOL_DEFINITIONS.get(tool_name), "superseded_by", None
            )
            if superseded_by:
                tool_info["superseded_by"] = superseded_by
        # Add operation type (read/write/admin) from tool_modes
        from src.tool_modes import TOOL_OPERATIONS
        tool_info["op"] = TOOL_OPERATIONS.get(tool_name, "read")  # Default to read
        # Declared stability tier (src/tool_meta.py); an alias reports its
        # implementation tool's. Surfaced since 2026-09-07; until then the
        # tier was recorded and never reported anywhere.
        from ..tool_stability import get_tool_stability
        tool_info["stability"] = get_tool_stability(tool_name).value
        # Add timeout metadata if available from decorator
        timeout = get_tool_timeout(tool_name)
        if timeout:
            tool_info["timeout"] = timeout
        # Add category from relationships if available
        if tool_name in tool_relationships:
            category_name = tool_relationships[tool_name].get("category")
            # Ensure category_name is never None
            if not category_name or not isinstance(category_name, str):
                category_name = "unknown"
            tool_info["category"] = category_name
            # Category label from the one presentation table (tool_catalog);
            # an unknown category gets a neutral label, never a crash.
            category_meta = tool_catalog.category_presentation(category_name)
            tool_info["category_icon"] = category_meta["icon"]
            tool_info["category_name"] = category_meta["name"]
        tools_list.append(tool_info)
    
    # PROGRESSIVE DISCLOSURE: Order tools by usage frequency (if enabled)
    async def get_usage_data(window_hours: int = 168) -> Dict[str, Dict[str, Any]]:
        """Get tool usage statistics for ordering (DB-first, cached, JSONL fallback)."""
        return await _usage_tools_for_ordering(window_hours)

    def order_tools_by_usage(tools: List[Dict[str, Any]], usage_data: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Order tools by usage frequency, fallback to tier-based ordering."""
        # Tier priority for fallback (essential > common > advanced)
        tier_priority = {"essential": 3, "common": 2, "advanced": 1}

        def sort_key(tool: Dict[str, Any]) -> tuple:
            tool_name = tool["name"]
            # get_usage_stats reports per-tool counts under "total_calls".
            call_count = usage_data.get(tool_name, {}).get("total_calls", 0)
            tier_prio = tier_priority.get(tool.get("tier", "common"), 0)
            # Primary: usage count (descending), Secondary: tier priority (descending)
            return (-call_count, -tier_prio)

        return sorted(tools, key=sort_key)

    # Apply progressive ordering if enabled
    usage_data = {}
    if progressive:
        usage_data = await get_usage_data()
        tools_list = order_tools_by_usage(tools_list, usage_data)
    
    # LITE MODE: the complete public capability-name index used by federation
    # negotiation, without repeating per-tool metadata or onboarding prose.
    # ``interface_contract.federation.negotiation.capabilities_path`` is
    # ``tools[*].name``, so every advertised name remains present. Rich
    # browsing lives in lite=false and one-tool detail in describe_tool.
    if lite_mode:
        lite_tools = [
            {"name": t["name"]}
            for t in tools_list
        ]
        # Sort by workflow order (onboard first) or usage if progressive enabled
        if progressive and usage_data:
            # Re-order lite tools by usage (they're already filtered from tools_list which was ordered)
            lite_tools_dict = {t["name"]: t for t in lite_tools}
            ordered_lite_names = [t["name"] for t in tools_list if t["name"] in lite_tools_dict]
            lite_tools = [lite_tools_dict[name] for name in ordered_lite_names if name in lite_tools_dict]
        else:
            # Default workflow order
            order = [
                "start_session", "sync_state", "check_working_state",
                "search_shared_memory", "record_result", "consult", "request_review",
                "onboard", "identity", "process_agent_update",
                "get_governance_metrics", "list_tools", "describe_tool",
                "agent", "knowledge", "dialectic", "health_check",
                "search_knowledge_graph", "leave_note",
            ]
            lite_tools.sort(key=lambda x: order.index(x["name"]) if x["name"] in order else 99)
        
        response_data = {
            "tools": lite_tools,
            "interface_contract": interface_contract,
            "total_available": len(tools_list),
            "shown": len(lite_tools),
            "more": "list_tools(lite=false) for descriptions, categories, tiers, workflows, and relationships",
            # An unqualified describe_tool returns the full record on the
            # Python route (lite=false is its advertised default, 10-17 KB for
            # the core write tools); with WAVE_3A_DESCRIBE_TOOL_ON_BEAM on,
            # BEAM serves it through the Wave 3a probe, which asks for lite.
            # The tip names lite explicitly, so it holds on both.
            "tip": "describe_tool(tool_name=..., lite=true) for parameters (action=... on a router); lite=false for the full schema; use_tool(tool_name=..., arguments={...}) when the capability is absent from the initial tools/list",
            "advertisement": {
                "mode": TOOL_MODE,
                "direct_count": len(advertised_names or lite_tools),
                "full_mode_env": "UNITARES_TOOL_ADVERTISEMENT=full",
            },
        }
        
        # Add progressive metadata if enabled
        if progressive:
            response_data["progressive"] = {
                "enabled": True,
                "ordered_by": "usage_frequency",
                "window": "7 days"
            }

        # The handshake is identity-independent: no agent_signature. For a
        # caller-asserted binding that is not a routine explicit session
        # (mcp_session_id, x_client_id) the signature is 1.5-1.9 KB and put the
        # capped handshake over its own 4 KiB bound; a server-inferred one
        # already collapses to {"uuid": null}. lite=false keeps it.
        return success_response(response_data, arguments={"lite_response": True})
    
    tier_counts = {
        "essential": sum(1 for t in tools_list if t.get("tier") == "essential"),
        "common": sum(1 for t in tools_list if t.get("tier") == "common"),
        "advanced": sum(1 for t in tools_list if t.get("tier") == "advanced"),
    }
    
    # PROGRESSIVE GROUPING: Group tools by usage frequency (full mode only)
    progressive_sections = None
    if progressive:
        def group_tools_progressively(tools: List[Dict[str, Any]], usage_data: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
            """Group tools into Most Used / Commonly Used / Available."""
            most_used = []
            commonly_used = []
            available = []
            
            for tool in tools:
                tool_name = tool["name"]
                # get_usage_stats reports per-tool counts under "total_calls".
                call_count = usage_data.get(tool_name, {}).get("total_calls", 0)

                if call_count > 10:
                    most_used.append(tool["name"])
                elif call_count > 0:
                    commonly_used.append(tool["name"])
                else:
                    available.append(tool["name"])
            
            return {
                "most_used": {
                    "tools": most_used,
                    "count": len(most_used),
                    "threshold": ">10 calls/week"
                },
                "commonly_used": {
                    "tools": commonly_used,
                    "count": len(commonly_used),
                    "threshold": "1-10 calls/week"
                },
                "available": {
                    "tools": available,
                    "count": len(available),
                    "threshold": "0 calls or new"
                }
            }
        
        try:
            if not usage_data:  # Get if not already fetched
                usage_data = await get_usage_data()
            progressive_sections = group_tools_progressively(tools_list, usage_data)
        except Exception:
            pass  # Graceful degradation - skip grouping if stats unavailable
    
    # `categories` is derived from the tools this response lists, so it can
    # only name what `tools` already names: every entry is on this
    # deployment's wire by construction, and a filter (tier, category)
    # narrows it the same way. Ordered by the presentation priority.
    #
    # Until 2026-09-12 this was a hand-written dict beside the then-derived
    # `categories_summary` of the compact view. It predated the router
    # consolidation: 30 of its 47 names were dispatch-only twins (`list_agents`,
    # `store_knowledge_graph`, `get_server_info`, ...) that return Unknown tool
    # on the /mcp/ mount, and it omitted every router and workflow alias (F1
    # of docs/operations/tool-surface-audit-2026-09-12.md).
    listed_names = [t["name"] for t in tools_list]
    categories_block: Dict[str, Dict[str, Any]] = {}
    for t in tools_list:
        cat = t.get("category") or "other"
        if cat not in categories_block:
            presentation = tool_catalog.category_presentation(cat)
            categories_block[cat] = {
                "name": f"{presentation['icon']} {presentation['name']}",
                "description": presentation["description"],
                "tools": [],
                "priority": presentation["priority"],
                "for_new_agents": presentation["for_new_agents"],
            }
        categories_block[cat]["tools"].append(t["name"])
    categories_block = dict(
        sorted(categories_block.items(), key=lambda item: item[1]["priority"])
    )
    # `relationships` carries records for the names listed above only. The
    # catalog also holds records for plugin-provided tools; a deployment
    # without the plugin would otherwise describe relationships of a tool it
    # cannot dispatch.
    tool_relationships = {
        name: tool_relationships[name]
        for name in listed_names
        if name in tool_relationships
    }

    tools_info = {
        "success": True,
        "server_version": mcp_server.SERVER_VERSION,
        "interface_contract": interface_contract,
        "tools": tools_list,
        "not_advertised": _not_advertised_summary(tools_list, TOOL_MODE),
        "tiers": {
            "essential": list(TOOL_TIERS["essential"]),
            "common": list(TOOL_TIERS["common"]),
            "advanced": list(TOOL_TIERS["advanced"]),
        },
        "tier_counts": tier_counts,
        "filter_applied": {
            "essential_only": essential_only,
            "include_advanced": include_advanced,
            "tier_filter": tier_filter,
            "category_filter": category_filter,
            "progressive": progressive,
        },
        "categories": categories_block,
        "category_descriptions": {
            cat: f"{p['icon']} {p['description']}"
            for cat, p in tool_catalog.CATEGORY_PRESENTATION.items()
        },
        "getting_started": {
            "path": tool_catalog.getting_started_path(),
            "essential_toolkit": tool_catalog.essential_toolkit(),
            "for_new_agents": [
                {
                    "category": "identity",
                    "tools": ["onboard", "identity"],
                    "why": "Create your identity and get started"
                },
                {
                    "category": "core",
                    "tools": ["process_agent_update", "get_governance_metrics"],
                    "why": "Share your work and check your state"
                }
            ],
            "next_steps": [
                {
                    "category": "lifecycle",
                    "tools": ["agent(action='list')"],
                    "why": "See who else is here"
                },
                {
                    "category": "knowledge",
                    "tools": ["store_finding", "leave_note"],
                    "why": "Save discoveries and insights"
                }
            ]
        },
        "workflows": workflows,
        "relationships": tool_relationships,
        "note": (
            "Use this tool to discover available capabilities. MCP protocol also provides tool "
            "definitions, but this provides categorized overview useful for onboarding. Use "
            "'essential_only=true' or 'tier=essential' to reduce cognitive load by showing only the "
            f"{len(TOOL_TIERS['essential'])} core workflow tools."
        ),
        "quick_start": {
            "new_agent": [
                "1. Call start_session(force_new=true) - creates a fresh process identity",
                "2. Save uuid and client_session_id from the response",
                "3. Pass client_session_id on later check-ins and writes",
                "4. Use parent_agent_id only for a real handoff from a finished predecessor",
                "5. Use identity(name='...') only to set a cosmetic label"
            ],
            "categories_to_explore": [
                "🚀 Identity & Onboarding - Start here!",
                "💬 Core Governance - Your main tools",
                "👥 Agent Lifecycle - See who else is here",
                "💡 Knowledge Graph - Save discoveries"
            ]
        },
        "options": {
            "lite_mode": "Use list_tools(lite=true) for the compact name index and interface handshake; use lite=false to browse metadata",
            "describe_tool": "Use describe_tool(tool_name, lite=true) for simplified schemas with fewer parameters"
        },
        # Visual tool relationship map (v2.5.0+). Names on the wire only, or
        # call shapes against a router on it: the ten dispatch-only twins it
        # drew until 2026-09-12 (list_agents, observe_agent, export_to_file,
        # delete_agent, ...) were Unknown tool on /mcp/.
        "tool_map": """
┌──────────────────────────────────────────────────────────────────────┐
│                        TOOL RELATIONSHIP MAP                         │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  🚀 START                                                            │
│     │                                                                │
│     ▼                                                                │
│  ┌───────────────┐                                                   │
│  │ start_session │────────────┐                                      │
│  └───────┬───────┘            │                                      │
│          │                    ▼                                      │
│          │           ┌──────────────┐                                │
│          │           │   identity   │ ◄── name yourself              │
│          │           └──────────────┘                                │
│          │                                                           │
│          ▼                                                           │
│  ┌────────────────────────┐       ┌─────────────────────────────┐    │
│  │ sync_state             │◄─────►│ check_working_state         │    │
│  │ (main check-in)        │       │ (read the verdict)          │    │
│  └───────────┬────────────┘       └─────────────────────────────┘    │
│              │                                                       │
│              ├───────────────────────────────────────┐               │
│              │                                       │               │
│              ▼                                       ▼               │
│  ┌────────────────────────────┐   ┌────────────────────────────────┐ │
│  │ KNOWLEDGE GRAPH            │   │ OBSERVABILITY                  │ │
│  ├────────────────────────────┤   ├────────────────────────────────┤ │
│  │ search_shared_memory       │   │ agent(action='list')           │ │
│  │ store_finding              │   │ observe(action='agent')        │ │
│  │ knowledge                  │   │ observe(action='compare')      │ │
│  │ leave_note                 │   │ observe(action='anomalies')    │ │
│  └────────────────────────────┘   └────────────────────────────────┘ │
│                                                                      │
│  ────────────────────────────────────────────────────────────────    │
│  ADMIN/CONFIG: health_check, get_thresholds, describe_tool, admin    │
│  EXPORT: export(action='history'), export(action='file')             │
│  LIFECYCLE: agent (action=get | update | archive | delete)           │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
"""
    }

    # Calculate total_tools dynamically to avoid discrepancies
    tools_info["total_tools"] = len(tools_info["tools"])
    
    # Add progressive disclosure metadata if enabled
    if progressive:
        tools_info["progressive"] = {
            "enabled": True,
            "ordered_by": "usage_frequency",
            "window": "7 days"
        }
        if progressive_sections:
            tools_info["sections"] = progressive_sections
    
    return success_response(tools_info)


@mcp_tool("use_tool", timeout=None, requires_identity="pre_onboard")
async def handle_use_tool(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Invoke a public capability omitted from progressive advertisement."""
    target = str(arguments.get("tool_name") or "").strip()
    if not target:
        return [error_response(
            "tool_name is required",
            error_code="TOOL_NAME_REQUIRED",
            recovery={"action": "Call list_tools(lite=true) for capability names"},
        )]
    if target == "use_tool":
        return [error_response(
            "use_tool cannot invoke itself",
            error_code="RECURSIVE_TOOL_INVOCATION",
            recovery={"action": "Name the final target capability directly"},
        )]

    nested = arguments.get("arguments")
    if nested is None:
        nested = {}
    if not isinstance(nested, dict):
        return [error_response(
            "arguments must be a JSON object",
            error_code="INVALID_TOOL_ARGUMENTS",
            recovery={"action": f"Call describe_tool(tool_name={target!r})"},
        )]
    nested = dict(nested)

    public_names = set(_registered_public_tool_names())
    if target not in public_names:
        return [error_response(
            f"Unknown public capability: {target}",
            error_code="TOOL_NOT_FOUND",
            recovery={
                "action": "Call list_tools(lite=true) and use an exact returned name",
                "related_tools": ["list_tools", "describe_tool"],
            },
        )]

    # The active transport owns re-entry. This preserves MCP session proof and
    # Wave 3a routing, REST target-specific prebinding, and stdio activity plus
    # telemetry behavior instead of approximating them in this handler.
    from src.mcp_handlers.context import get_nested_tool_invoker

    invoker = get_nested_tool_invoker()
    if invoker is not None:
        transport_result = await invoker(target, nested)
        if isinstance(transport_result, (list, tuple)):
            return transport_result
        return [TextContent(
            type="text", text=json.dumps(transport_result, default=str)
        )]

    # Direct handler calls (tests and embedders) have no transport callback.
    # Preserve their historical local fallback and propagate an explicit outer
    # session only here; real transports decide session provenance themselves.
    if (
        "client_session_id" not in nested
        and "client_session_id" in arguments
    ):
        nested["client_session_id"] = arguments.get("client_session_id")
    from src.mcp_handlers import dispatch_tool
    from src.services.tool_usage_recorder import (
        build_tool_usage_payload,
        classify_tool_result,
        record_tool_usage,
        resolve_minted_agent_id,
    )

    usage_payload = build_tool_usage_payload(target, nested)
    started = _time.monotonic()
    try:
        result = await dispatch_tool(target, nested)
    except Exception as exc:
        record_tool_usage(
            tool_name=target,
            agent_id=nested.get("agent_id"),
            success=False,
            error_type=type(exc).__name__,
            latency_ms=int((_time.monotonic() - started) * 1000),
            session_id=nested.get("client_session_id"),
            payload=usage_payload,
        )
        raise
    latency_ms = int((_time.monotonic() - started) * 1000)
    if result is None:
        record_tool_usage(
            tool_name=target,
            agent_id=nested.get("agent_id"),
            success=False,
            error_type="unknown_tool",
            latency_ms=latency_ms,
            session_id=nested.get("client_session_id"),
            payload=usage_payload,
        )
        return [error_response(
            f"Capability did not dispatch: {target}",
            error_code="TOOL_NOT_FOUND",
        )]

    success, error_type = classify_tool_result(result)
    actor = resolve_minted_agent_id(target, nested.get("agent_id"), result)
    record_tool_usage(
        tool_name=target,
        agent_id=actor,
        success=success,
        error_type=error_type,
        latency_ms=latency_ms,
        session_id=nested.get("client_session_id"),
        payload=usage_payload,
    )
    return result

@mcp_tool("describe_tool", timeout=10.0, requires_identity="pre_onboard")
async def handle_describe_tool(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Return full details for a single tool (full description + full schema) on demand.
    This is intended to keep MCP tool lists compact while still enabling deep discovery.
    
    LITE MODE: Use lite=true to get a simplified schema suitable for smaller models.
    Shows only required params + key optional params with simple examples.
    """
    try:
        requested_tool_name = (arguments.get("tool_name") or "").strip()
        if not requested_tool_name:
            return [error_response(
                "tool_name is required",
                recovery={
                    "action": "Call list_tools to find the primary tool name, then call describe_tool(tool_name=...)",
                    "related_tools": ["list_tools"],
                },
            )]

        requested_action = (arguments.get("action") or "").strip().lower() or None
        include_schema = arguments.get("include_schema", True)
        include_full_description = arguments.get("include_full_description", True)
        # The schema default (DescribeToolParams.lite=False, the advertised
        # contract) decides on every validated Python route; this matches it
        # for in-process callers, which pass lite explicitly for the short
        # form. Exception: with WAVE_3A_DESCRIBE_TOOL_ON_BEAM on, the wrappers
        # hand raw arguments to BEAM, whose Wave 3a probe passes lite=True, so
        # an unqualified call is served short there (pre-existing; the
        # probe's pinned parity bytes depend on it).
        lite = arguments.get("lite", False)

        from ..tool_stability import (
            expand_description_pointers,
            get_tool_stability,
            resolve_tool_alias,
        )
        tool_name, alias_info = resolve_tool_alias(requested_tool_name)
        from ..decorators import is_tool_hidden

        if is_tool_hidden(requested_tool_name) or is_tool_hidden(tool_name):
            return [error_response(
                f"Unknown tool: {requested_tool_name}",
                recovery={
                    "action": "Call list_tools to see available tool names",
                    "related_tools": ["list_tools"],
                },
                context={"tool_name": requested_tool_name},
            )]
        stability = get_tool_stability(tool_name).value

        from src.tool_descriptions import TOOL_DESCRIPTIONS
        from src.tool_schemas import (
            advertised_input_schema,
            first_line,
            get_pydantic_schemas,
        )

        schema_model = get_pydantic_schemas().get(tool_name)
        tool_schema = None
        if schema_model is not None:
            # The same hiding the wire applies (session-injected identity
            # params), so describe_tool never advertises what the registered
            # schema does not carry.
            tool_schema = advertised_input_schema(tool_name, schema_model.model_json_schema())
            description = TOOL_DESCRIPTIONS.get(tool_name) or schema_model.__doc__ or f"Tool: {tool_name}"
        else:
            # Fallback for decorator-defined/plugin tools that are not backed
            # by a Pydantic Params model. Avoid rebuilding every Tool object
            # just to describe one name.
            from ..decorators import get_tool_definition

            definition = get_tool_definition(tool_name)
            if definition is not None:
                description = definition.description or TOOL_DESCRIPTIONS.get(tool_name) or f"Tool: {tool_name}"
                tool_schema = {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True,
                }
            else:
                # Compatibility path for test/plugin tool objects that are
                # only discoverable through the MCP Tool schema builder. The
                # common Pydantic path above still avoids rebuilding the full
                # registry for normal describe_tool calls.
                from src.tool_schemas import get_tool_definitions

                description = None
                for candidate in get_tool_definitions():
                    if candidate.name == tool_name:
                        description = (
                            candidate.description
                            or TOOL_DESCRIPTIONS.get(tool_name)
                            or f"Tool: {tool_name}"
                        )
                        tool_schema = get_tool_input_schema(candidate) or {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": True,
                        }
                        break

        if tool_schema is None:
            return [error_response(
                f"Unknown tool: {tool_name}",
                recovery={
                    "action": "Call list_tools to see available tool names",
                    "related_tools": ["list_tools"],
                },
                context={
                    "tool_name": requested_tool_name,
                    "resolved_tool_name": tool_name,
                },
            )]

        if alias_info:
            # Discovery must describe the alias clients can actually call, not
            # the wider implementation router. Registration uses this same
            # builder, preventing describe_tool from drifting from the wire.
            from src.alias_schema import build_alias_input_schema

            tool_schema = build_alias_input_schema(
                requested_tool_name,
                tool_schema,
                inject_action=bool(alias_info.inject_action),
            )
            # A workflow alias describes itself with its migration note, which
            # is the text tools/list serves for it (build_alias_tool_definition).
            # The override table holds dispatch-only alias names only, so it
            # answers here for names that are never on the wire (list_agents)
            # and cannot put a second description on an advertised one.
            description = (
                tool_catalog.TOOL_DESCRIPTION_OVERRIDES.get(requested_tool_name)
                or expand_description_pointers(alias_info.migration_note)
                or description
            )

        # A router advertises the union of every action's parameters, because
        # the wire schema must be flat. That union is not what any single call
        # takes: `knowledge` shows 50 parameters for each of its 12 actions.
        # With action=..., answer for that one action instead.
        action_view = None
        if schema_model is not None and not alias_info:
            from src.mcp_handlers.schemas.router_actions import (
                declared_action_fields,
                narrow_schema_to_action,
            )

            declared = declared_action_fields(schema_model)
            if declared is not None:
                if requested_action:
                    narrowed = narrow_schema_to_action(
                        tool_schema, schema_model, requested_action
                    )
                    if narrowed is None:
                        return [error_response(
                            f"Unknown action for {tool_name}: {requested_action}",
                            recovery={
                                "valid_actions": sorted(declared),
                                "action": (
                                    f"Call describe_tool(tool_name={tool_name!r}) "
                                    "with no action for the full surface"
                                ),
                            },
                            context={"tool_name": tool_name},
                        )]
                    full_count = len(tool_schema.get("properties") or {})
                    tool_schema = narrowed
                    action_view = {
                        "action": requested_action,
                        "parameters_shown": len(narrowed.get("properties") or {}),
                        "parameters_on_the_wire": full_count,
                        "note": (
                            "Narrowed to this action. The wire schema stays "
                            "flat, so the other parameters are still accepted "
                            "and ignored."
                        ),
                    }
                else:
                    action_view = {
                        "actions": sorted(declared),
                        "note": (
                            "This tool routes actions and advertises the union "
                            "of their parameters. Call "
                            f"describe_tool(tool_name={tool_name!r}, "
                            "action=...) for one action's parameters."
                        ),
                    }

        if not include_full_description:
            description = first_line(description)

        # Helper function to get common patterns (shared between both branches)
        def get_common_patterns(tool_name: str) -> dict:
            return tool_catalog.common_patterns_for(tool_name)

        # === LITE MODE: Simplified schema for smaller models ===
        if lite:
            # Try Pydantic schema first for structured lite output
            lite_schema = None
            try:
                pydantic_model = schema_model
                if pydantic_model:
                    schema = tool_schema or pydantic_model.model_json_schema()
                    properties = schema.get("properties", {})
                    required_fields = schema.get("required", [])

                    params_simple = []
                    shown_fields = set()

                    def add_lite_field(
                        field_name: str,
                        *,
                        required: bool = False,
                        required_at_call_time: bool = False,
                        force: bool = False,
                    ) -> bool:
                        if field_name in shown_fields:
                            return False
                        if field_name in tool_catalog.LITE_IDENTITY_FIELDS and not force:
                            return False
                        field_info = properties.get(field_name, {})
                        params_simple.append(
                            _format_lite_parameter(
                                field_name,
                                field_info,
                                required=required,
                                required_at_call_time=required_at_call_time,
                            )
                        )
                        shown_fields.add(field_name)
                        return True

                    for field_name in required_fields:
                        add_lite_field(field_name, required=True)

                    from src.mcp_handlers.schemas.router_actions import (
                        action_fields_by_priority,
                        declared_action_required_fields,
                    )

                    # The one action this view describes: the action an
                    # alias pins, or the one describe_tool(action=...) narrowed
                    # a router to. Its handler's call-time requirements lead,
                    # then its own parameters, primary first. Filling from the
                    # schema's property order instead listed update_finding as
                    # content, details, summary, discovery_type and tags, and
                    # left out discovery_id, which the update refuses to run
                    # without, and status, which it exists to set.
                    if alias_info is not None and alias_info.inject_action:
                        lite_action = alias_info.inject_action
                    elif action_view is not None:
                        lite_action = action_view.get("action")
                    else:
                        lite_action = None
                    for field_name in declared_action_required_fields(
                        pydantic_model, lite_action
                    ):
                        # Only what this tool's wire carries: an alias
                        # schema is narrower than its router's.
                        if field_name in properties:
                            add_lite_field(
                                field_name,
                                required_at_call_time=True,
                                force=True,
                            )

                    shown = 0
                    priorities = (
                        tool_catalog.LITE_PARAMETER_PRIORITIES.get(requested_tool_name)
                        or tool_catalog.LITE_PARAMETER_PRIORITIES.get(tool_name, [])
                    )
                    action_priorities = (
                        None
                        if priorities
                        else action_fields_by_priority(pydantic_model, lite_action)
                    )
                    for field_name in priorities:
                        if add_lite_field(field_name, force=True):
                            shown += 1

                    if action_priorities is not None:
                        # No fill from the rest of the schema: on an alias
                        # that does not narrow its router's schema, the other
                        # properties belong to other actions.
                        for field_name in action_priorities:
                            if shown >= 5:
                                break
                            if field_name in properties and add_lite_field(field_name):
                                shown += 1
                    else:
                        for field_name in properties:
                            if field_name in required_fields:
                                continue
                            if field_name in shown_fields:
                                continue
                            if shown >= 5 and tool_name not in tool_catalog.LITE_PARAMETER_PRIORITIES:
                                break
                            if shown >= 8:
                                break
                            if add_lite_field(field_name):
                                shown += 1

                    lite_schema = {"params_simple": params_simple, "required": required_fields}
            except Exception:
                pass

            if lite_schema:
                params_simple = lite_schema["params_simple"]

                # Get common patterns
                common_patterns = get_common_patterns(requested_tool_name) or get_common_patterns(tool_name)

                # Get parameter aliases for discoverability
                from ..validators import PARAM_ALIASES
                tool_aliases = PARAM_ALIASES.get(tool_name, {})

                # UX FIX (Feb 2026): Add tier information to help agents understand tool complexity
                from src.tool_modes import TOOL_TIERS
                tool_tier = "common"  # Default
                tier_lookup_name = requested_tool_name if alias_info else tool_name
                if tier_lookup_name in TOOL_TIERS["essential"] or tool_name in TOOL_TIERS["essential"]:
                    tool_tier = "essential"
                elif tier_lookup_name in TOOL_TIERS["advanced"] or tool_name in TOOL_TIERS["advanced"]:
                    tool_tier = "advanced"

                tier_guidance = {
                    "essential": "Core tool - regularly used for basic workflows",
                    "common": "Standard tool - commonly used for specific tasks",
                    "advanced": "Advanced tool - use when you need specialized functionality"
                }

                response_data = {
                    "tool": requested_tool_name,
                    "description": first_line(description),
                    "tier": tool_tier,
                    "tier_note": tier_guidance.get(tool_tier, ""),
                    "operation": _describe_operation(requested_tool_name, tool_name, alias_info),  # read/write/admin
                    "stability": stability,  # stable/beta/experimental
                    "parameters": params_simple,
                    "note": "Lite mode - use describe_tool(tool_name=..., lite=false) for full schema"
                }
                if alias_info:
                    response_data["primary_tool"] = requested_tool_name
                    response_data["implementation_tool"] = tool_name
                    response_data["canonical_tool"] = tool_name
                    response_data["alias"] = _alias_block(requested_tool_name, alias_info)

                deprecation = tool_catalog.describe_tool_deprecation_block(tool_name)
                if deprecation is not None:
                    response_data["deprecation"] = deprecation

                if tool_aliases:
                    # Format: {"content": "summary"} → "content → summary"
                    response_data["parameter_aliases"] = {
                        alias: f"→ {canonical}" for alias, canonical in tool_aliases.items()
                    }

                if common_patterns:
                    response_data["common_patterns"] = common_patterns

                return success_response(response_data)
            else:
                # Fallback: extract from inputSchema
                schema = tool_schema or {}
                properties = schema.get("properties", {})
                required = schema.get("required", [])
                
                params_simple = []
                for param in required:
                    params_simple.append(f"{param} (required)")
                # Show transport continuity metadata without implying it is
                # long-term identity proof.
                shown_count = 0
                if "client_session_id" in properties and "client_session_id" not in required:
                    params_simple.append("client_session_id: string (in-session continuity)")
                    shown_count += 1
                for param, prop in list(properties.items())[:8]:
                    if param not in required and param != "client_session_id":
                        ptype = _resolve_json_schema_type(prop)
                        params_simple.append(f"{param}: {ptype}")
                        shown_count += 1
                total_optional = sum(1 for p in properties if p not in required)
                if total_optional > shown_count:
                    params_simple.append(f"... and {total_optional - shown_count} more (use lite=false for full schema)")
                
                # Get common patterns using shared helper
                common_patterns = get_common_patterns(requested_tool_name) or get_common_patterns(tool_name)

                # Get parameter aliases for discoverability
                from ..validators import PARAM_ALIASES
                tool_aliases = PARAM_ALIASES.get(tool_name, {})

                response_data = {
                    "tool": requested_tool_name,
                    "description": first_line(description),
                    "parameters": params_simple,
                    "note": "Lite mode - use describe_tool(tool_name=..., lite=false) for full schema"
                }
                if alias_info:
                    response_data["primary_tool"] = requested_tool_name
                    response_data["implementation_tool"] = tool_name
                    response_data["canonical_tool"] = tool_name
                    response_data["alias"] = _alias_block(requested_tool_name, alias_info)

                deprecation = tool_catalog.describe_tool_deprecation_block(tool_name)
                if deprecation is not None:
                    response_data["deprecation"] = deprecation

                if tool_aliases:
                    response_data["parameter_aliases"] = {
                        alias: f"→ {canonical}" for alias, canonical in tool_aliases.items()
                    }

                if common_patterns:
                    response_data["common_patterns"] = common_patterns

                return success_response(response_data)

        full_response: Dict[str, Any] = {
            "tool": {
                "name": requested_tool_name,
                "description": description,
                "inputSchema": tool_schema if include_schema else None,
            }
        }
        full_response["tool"]["stability"] = stability
        if action_view is not None:
            full_response["actions"] = action_view
        if alias_info:
            full_response["tool"]["canonical_name"] = tool_name
            full_response["tool"]["implementation_name"] = tool_name
            full_response["tool"]["role"] = _alias_role(alias_info)
            full_response["alias"] = _alias_block(requested_tool_name, alias_info)
        deprecation = tool_catalog.describe_tool_deprecation_block(tool_name)
        if deprecation is not None:
            full_response["deprecation"] = deprecation
        return success_response(full_response)
    except Exception as e:
        return [error_response(f"Error describing tool: {str(e)}")]
