"""
Tool introspection handlers (list_tools, describe_tool).

Extracted from admin.py for maintainability.
"""

from typing import Dict, Any, List, Sequence
from mcp.types import TextContent
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
) -> str:
    if required:
        return f"{field_name} (required)"

    field_type = _resolve_json_schema_type(field_info)
    default = field_info.get("default")
    if default is not None:
        return f"{field_name}: {field_type} (default: {default})"
    return f"{field_name}: {field_type}"

@mcp_tool("list_tools", timeout=10.0, requires_identity="pre_onboard")
async def handle_list_tools(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """List all available governance tools with descriptions and categories
    
    Parameters:
        essential_only (bool): If true, return only Tier 1 (essential) tools (default: false)
        include_advanced (bool): If false, exclude Tier 3 (advanced) tools (default: true)
        tier (str): Filter by tier: "essential", "common", "advanced", or "all" (default: "all")
        lite (bool): If true, return minimal response (names + descriptions only, ~500B vs ~4KB)
        progressive (bool): If true, order tools by usage frequency (most used first). Works with all filter modes. Default false.
    """
    
    # Get actual registered tools from TOOL_HANDLERS registry
    from src.mcp_handlers import TOOL_HANDLERS
    from ..tool_stability import AGENT_WORKFLOW_ALIASES
    registered_tool_names = sorted(set(TOOL_HANDLERS.keys()) | set(AGENT_WORKFLOW_ALIASES))
    
    # Parse filter parameters (handle string booleans from MCP transport)
    essential_only = coerce_bool(arguments.get("essential_only"), False)
    include_advanced = coerce_bool(arguments.get("include_advanced"), True)
    tier_filter = arguments.get("tier", "all")
    # LITE-FIRST: Default to minimal response for local/smaller models
    lite_mode = coerce_bool(arguments.get("lite"), True)
    # Progressive disclosure: Order tools by usage frequency
    progressive = coerce_bool(arguments.get("progressive"), False)
    
    # Import TOOL_TIERS from single source of truth
    from src.tool_modes import TOOL_TIERS

    # Deprecated tools - hidden from list_tools by default
    # Source of truth: tool_stability.py (aliases handle routing)
    from ..tool_stability import list_all_aliases
    DEPRECATED_TOOLS = set(list_all_aliases().keys()) - set(AGENT_WORKFLOW_ALIASES)

    tool_relationships = tool_catalog.TOOL_RELATIONSHIPS
    workflows = tool_catalog.WORKFLOWS
    tool_descriptions = tool_catalog.TOOL_DESCRIPTION_OVERRIDES
    
    # Build tools list from registered tools with metadata from decorators
    from ..decorators import get_tool_timeout, get_tool_description
    # Import tool schemas to get proper descriptions
    from src.tool_schemas import get_tool_definitions
    schema_tools = {t.name: t.description for t in get_tool_definitions()}
    
    tools_list = []
    for tool_name in registered_tool_names:
        # Priority: 1. tool_descriptions dict, 2. schema description, 3. decorator description, 4. fallback
        # Check each source explicitly to avoid empty string issues
        description = None
        if tool_name in tool_descriptions and tool_descriptions[tool_name]:
            description = tool_descriptions[tool_name]
        elif tool_name in schema_tools and schema_tools[tool_name]:
            description = schema_tools[tool_name]
        else:
            desc_from_decorator = get_tool_description(tool_name)
            if desc_from_decorator:
                description = desc_from_decorator
        
        # Fallback to generic description if none found
        if not description:
            description = f"Tool: {tool_name}"
        
        # Extract first line of description for brevity (full description available in tool schemas)
        if description and '\n' in description:
            description = description.split('\n')[0]
        
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
        
        tool_info = {
            "name": tool_name,
            "description": description,
            "tier": tool_tier
        }
        # Add operation type (read/write/admin) from tool_modes
        from src.tool_modes import TOOL_OPERATIONS
        tool_info["op"] = TOOL_OPERATIONS.get(tool_name, "read")  # Default to read
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
            # Add category metadata for better UX
            category_meta_dict = {
                "identity": {"icon": "🚀", "name": "Identity & Onboarding"},
                "core": {"icon": "💬", "name": "Core Governance"},
                "lifecycle": {"icon": "👥", "name": "Agent Lifecycle"},
                "knowledge": {"icon": "💡", "name": "Knowledge Graph"},
                "observability": {"icon": "👁️", "name": "Observability"},
                "export": {"icon": "📊", "name": "Export & History"},
                "config": {"icon": "⚙️", "name": "Configuration"},
                "admin": {"icon": "🔧", "name": "Admin & Diagnostics"},
                "workspace": {"icon": "📁", "name": "Workspace"},
                "dialectic": {"icon": "💭", "name": "Dialectic"}
            }
            if category_name in category_meta_dict:
                category_meta = category_meta_dict[category_name]
            else:
                # Fallback for unknown categories - category_name is guaranteed to be a string here
                fallback_name = category_name.title() if isinstance(category_name, str) else "Other"
                category_meta = {"icon": "🔹", "name": fallback_name}
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
    
    # Count tools by tier
    # LITE MODE: Return only ESSENTIAL tools (~1KB vs ~20KB)
    if lite_mode:
        # Import from single source of truth
        from src.tool_modes import LITE_MODE_TOOLS
        lite_tools = [
            {
                "name": t["name"],
                "hint": t["description"][:100] + ("..." if len(t["description"]) > 100 else ""),
                "tier": t.get("tier", "common"),  # essential/common/advanced
                "op": t.get("op", "read"),  # read/write/admin
                "category": t.get("category"),
                "category_icon": t.get("category_icon"),
                "category_name": t.get("category_name")
            }
            for t in tools_list
            if t["name"] in LITE_MODE_TOOLS
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
                "search_shared_memory", "record_result", "request_review",
                "onboard", "identity", "process_agent_update",
                "get_governance_metrics", "list_tools", "describe_tool",
                "agent", "knowledge", "dialectic", "health_check",
                "store_knowledge_graph", "search_knowledge_graph", "leave_note",
            ]
            lite_tools.sort(key=lambda x: order.index(x["name"]) if x["name"] in order else 99)
        
        # Group by category for better organization
        categories_in_lite = {}
        category_metadata = {}
        for tool in lite_tools:
            cat = tool.get("category") or "other"
            if cat not in categories_in_lite:
                categories_in_lite[cat] = []
                cat_name = tool.get("category_name")
                if not cat_name:
                    cat_name = cat.title() if cat and isinstance(cat, str) else "Other"
                category_metadata[cat] = {
                    "icon": tool.get("category_icon", "🔹"),
                    "name": cat_name
                }
            categories_in_lite[cat].append(tool["name"])
        
        # Check if this might be a new agent (no bound identity)
        is_new_agent = False
        try:
            from ..context import get_context_agent_id
            bound_id = get_context_agent_id()  # Set by identity_v2 at dispatch entry
            is_new_agent = not bound_id
        except Exception:
            pass
        
        # Count lite tools by tier
        lite_tier_counts = {"essential": 0, "common": 0, "advanced": 0}
        for t in lite_tools:
            tier = t.get("tier", "common")
            if tier in lite_tier_counts:
                lite_tier_counts[tier] += 1

        response_data = {
            "tools": lite_tools,
            "total_available": len(tools_list),
            "shown": len(lite_tools),
            # Tier summary for quick understanding of tool importance
            "tier_summary": {
                "essential": {
                    "count": lite_tier_counts["essential"],
                    "note": "Core tools - use these for basic workflows"
                },
                "common": {
                    "count": lite_tier_counts["common"],
                    "note": "Standard tools - commonly used for specific tasks"
                },
                "advanced": {
                    "count": lite_tier_counts["advanced"],
                    "note": "Advanced tools - specialized functionality"
                }
            },
            "categories_summary": {
                cat: {
                    "icon": category_metadata[cat]["icon"],
                    "name": category_metadata[cat]["name"],
                    "tools": tools
                }
                for cat, tools in categories_in_lite.items()
            },
            # Quick workflows (v2.5.0+) - progressive disclosure
            "workflows": {
                "new_agent": ["start_session(force_new=true)", "sync_state(response_text='...', complexity=0.5)", "agent(action='list')"],
                "check_in": ["sync_state(response_text='...', complexity=0.5)"],
                "save_insight": ["knowledge(action='note', content='...')", "OR knowledge(action='store', summary='...', tags=[...])"],
                "find_info": ["search_shared_memory(query='...')", "OR knowledge(action='search', tags=[...])"],
                "recover": ["request_review(issue_description='...')", "OR self_recovery(action='review', reflection='...')"],
            },
            # Common signatures (type hints at a glance)
            "signatures": {
                "start_session": "(force_new:bool=true, parent_agent_id?:str, spawn_reason?:str)",
                "sync_state": "(response_text?:str, complexity?:float, confidence?:float, task_type?:str)",
                "check_working_state": "(lite?:bool, include_state?:bool)",
                "search_shared_memory": "(query?:str, tags?:list, limit?:int, include_details?:bool)",
                "record_result": "(outcome_type:str, confidence?:float, prediction_id?:str, detail?:dict)",
                "request_review": "(issue_description:str, reason?:str)",
                "store_knowledge_graph": "(summary:str, tags?:list, severity?:str, details?:str)",
                "search_knowledge_graph": "(query?:str, tags?:list, limit?:int, include_details?:bool)",
                "knowledge_search": "(action='search', query?:str, tags?:list, limit?:int, include_details?:bool)",
                "leave_note": "(summary:str, tags?:list)"
            },
            "more": "list_tools(lite=false) for all tools with full category details",
            "tip": "describe_tool(tool_name=...) for parameter details and examples",
            "quick_start": "Start fresh with start_session(force_new=true); pass client_session_id on later writes",
            "getting_started_path": tool_catalog.getting_started_path(),
            "essential_toolkit": tool_catalog.essential_toolkit(),
        }
        
        # Add first-time hint for new agents
        if is_new_agent:
            response_data["first_time"] = {
                "hint": "First time here? Start with start_session(force_new=true) to create your identity.",
                "next_step": "Call start_session(force_new=true), then pass its client_session_id on later writes."
            }
        
        # Add progressive metadata if enabled
        if progressive:
            response_data["progressive"] = {
                "enabled": True,
                "ordered_by": "usage_frequency",
                "window": "7 days"
            }
        
        return success_response(response_data)
    
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
    
    tools_info = {
        "success": True,
        "server_version": mcp_server.SERVER_VERSION,
        "tools": tools_list,
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
            "progressive": progressive,
        },
        "categories": {
            "identity": {
                "name": "🚀 Identity & Onboarding",
                "description": "Get started - create your identity and set up your session",
                "tools": ["onboard", "identity"],
                "priority": 1,
                "for_new_agents": True
            },
            "core": {
                "name": "💬 Core Governance",
                "description": "Main tools for sharing work and getting feedback",
                "tools": ["process_agent_update", "get_governance_metrics", "simulate_update"],
                "priority": 2,
                "for_new_agents": True
            },
            "lifecycle": {
                "name": "👥 Agent Lifecycle",
                "description": "Manage agents, view metadata, and handle agent states",
                "tools": ["list_agents", "get_agent_metadata", "update_agent_metadata", "archive_agent", "delete_agent", "archive_old_test_agents", "mark_response_complete", "self_recovery"],
                "priority": 3,
                "for_new_agents": False
            },
            "knowledge": {
                "name": "💡 Knowledge Graph",
                "description": "Store and search discoveries, insights, and notes",
                "tools": ["store_knowledge_graph", "search_knowledge_graph", "get_knowledge_graph", "list_knowledge_graph", "get_discovery_details", "leave_note", "update_discovery_status_graph"],
                "priority": 4,
                "for_new_agents": False
            },
            "observability": {
                "name": "👁️ Observability",
                "description": "Monitor agents, compare patterns, and detect anomalies",
                "tools": ["observe_agent", "compare_agents", "compare_me_to_similar", "detect_anomalies", "aggregate_metrics"],
                "priority": 5,
                "for_new_agents": False
            },
            "export": {
                "name": "📊 Export & History",
                "description": "Export governance history and system data",
                "tools": ["get_system_history", "export_to_file"],
                "priority": 6,
                "for_new_agents": False
            },
            "config": {
                "name": "⚙️ Configuration",
                "description": "Configure thresholds and system settings",
                "tools": ["get_thresholds", "set_thresholds"],
                "priority": 7,
                "for_new_agents": False
            },
            "admin": {
                "name": "🔧 Admin & Diagnostics",
                "description": "System administration, health checks, and diagnostics",
                "tools": ["reset_monitor", "get_server_info", "health_check", "check_calibration", "update_calibration_ground_truth", "get_telemetry_metrics", "get_tool_usage_stats", "list_tools", "describe_tool", "cleanup_stale_locks", "backfill_calibration_from_dialectic", "validate_file_path"],
                "priority": 8,
                "for_new_agents": False
            },
            "workspace": {
                "name": "📁 Workspace",
                "description": "Workspace health and file validation",
                "tools": ["get_workspace_health"],
                "priority": 9,
                "for_new_agents": False
            },
            "dialectic": {
                "name": "💭 Dialectic",
                "description": "Structured peer review and recovery protocol",
                "tools": ["request_dialectic_review", "submit_thesis", "submit_antithesis", "submit_synthesis", "dialectic"],
                "priority": 10,
                "for_new_agents": False
            }
        },
        "category_descriptions": {
            "identity": "🚀 Start here! Create your identity and get ready-to-use templates",
            "core": "💬 Your main tools - share work, get feedback, check your state",
            "lifecycle": "👥 Manage agents and view agent metadata",
            "knowledge": "💡 Store discoveries, search insights, leave notes",
            "observability": "👁️ Monitor agents, compare patterns, detect issues",
            "export": "📊 Export history and system data",
            "config": "⚙️ Configure thresholds and settings",
            "admin": "🔧 System administration and diagnostics",
            "workspace": "📁 Workspace health and validation",
            "dialectic": "💭 View archived dialectic sessions"
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
                    "tools": ["list_agents"],
                    "why": "See who else is here"
                },
                {
                    "category": "knowledge",
                    "tools": ["store_knowledge_graph", "leave_note"],
                    "why": "Save discoveries and insights"
                }
            ]
        },
        "workflows": workflows,
        "relationships": tool_relationships,
        "note": "Use this tool to discover available capabilities. MCP protocol also provides tool definitions, but this provides categorized overview useful for onboarding. Use 'essential_only=true' or 'tier=essential' to reduce cognitive load by showing only core workflow tools (~10 tools).",
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
            "lite_mode": "Use list_tools(lite=true) for minimal response (~2KB vs ~15KB) - better for local/smaller models",
            "describe_tool": "Use describe_tool(tool_name, lite=true) for simplified schemas with fewer parameters"
        },
        # Visual tool relationship map (v2.5.0+)
        "tool_map": """
┌─────────────────────────────────────────────────────────────────────┐
│                        TOOL RELATIONSHIP MAP                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  🚀 START                                                           │
│     │                                                               │
│     ▼                                                               │
│  ┌───────────────┐                                                  │
│  │ start_session │────────────┐                                     │
│  └───────┬───────┘            │                                     │
│       │                       ▼                                     │
│       │              ┌──────────────┐                               │
│       │              │   identity   │ ◄── name yourself             │
│       │              └──────────────┘                               │
│       │                                                             │
│       ▼                                                             │
│  ┌────────────────────────┐       ┌─────────────────────────────┐  │
│  │ sync_state             │◄─────►│ check_working_state         │  │
│  │ (main check-in)        │       │ (view state)                │  │
│  └───────────┬────────────┘       └─────────────────────────────┘  │
│              │                                                      │
│              ├───────────────────────────────────────┐              │
│              │                                       │              │
│              ▼                                       ▼              │
│  ┌───────────────────────┐              ┌────────────────────────┐ │
│  │ KNOWLEDGE GRAPH       │              │ OBSERVABILITY          │ │
│  ├───────────────────────┤              ├────────────────────────┤ │
│  │ search_shared_memory  │              │ list_agents            │ │
│  │ knowledge             │              │ observe_agent          │ │
│  │ leave_note            │              │ compare_agents         │ │
│  │ get_discovery_details │              │ detect_anomalies       │ │
│  └───────────────────────┘              └────────────────────────┘ │
│                                                                     │
│  ─────────────────────────────────────────────────────────────────  │
│  ADMIN/CONFIG: health_check, get_thresholds, describe_tool         │
│  EXPORT: get_system_history, export_to_file                        │
│  LIFECYCLE: archive_agent, delete_agent, update_agent_metadata     │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
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

        include_schema = arguments.get("include_schema", True)
        include_full_description = arguments.get("include_full_description", True)
        # LITE-FIRST: Simpler schemas by default for local models
        lite = arguments.get("lite", True)

        from ..tool_stability import resolve_tool_alias
        tool_name, alias_info = resolve_tool_alias(requested_tool_name)

        from src.tool_descriptions import TOOL_DESCRIPTIONS
        from src.tool_schemas import get_pydantic_schemas

        schema_model = get_pydantic_schemas().get(tool_name)
        tool_schema = None
        if schema_model is not None:
            tool_schema = schema_model.model_json_schema()
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
                        tool_schema = candidate.inputSchema or {
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

        if not include_full_description:
            description = (description or "").splitlines()[0].strip() if description else ""

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
                            )
                        )
                        shown_fields.add(field_name)
                        return True

                    for field_name in required_fields:
                        add_lite_field(field_name, required=True)

                    shown = 0
                    for field_name in tool_catalog.LITE_PARAMETER_PRIORITIES.get(tool_name, []):
                        if add_lite_field(field_name, force=True):
                            shown += 1

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
                from src.tool_modes import TOOL_TIERS, TOOL_OPERATIONS
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
                    "description": (description or "").splitlines()[0].strip(),
                    "tier": tool_tier,
                    "tier_note": tier_guidance.get(tool_tier, ""),
                    "operation": TOOL_OPERATIONS.get(tier_lookup_name, TOOL_OPERATIONS.get(tool_name, "read")),  # read/write/admin
                    "parameters": params_simple,
                    "note": "Lite mode - use describe_tool(tool_name=..., lite=false) for full schema"
                }
                if alias_info:
                    response_data["primary_tool"] = requested_tool_name
                    response_data["implementation_tool"] = tool_name
                    response_data["canonical_tool"] = tool_name
                    response_data["alias"] = {
                        "reason": alias_info.reason,
                        "role": "primary_agent_workflow",
                        "primary_tool": requested_tool_name,
                        "implementation_tool": alias_info.new_name,
                        "canonical_tool": alias_info.new_name,
                        "injected_action": alias_info.inject_action,
                        "note": alias_info.migration_note,
                    }

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
                    "description": (description or "").splitlines()[0].strip(),
                    "parameters": params_simple,
                    "note": "Lite mode - use describe_tool(tool_name=..., lite=false) for full schema"
                }
                if alias_info:
                    response_data["primary_tool"] = requested_tool_name
                    response_data["implementation_tool"] = tool_name
                    response_data["canonical_tool"] = tool_name
                    response_data["alias"] = {
                        "reason": alias_info.reason,
                        "role": "primary_agent_workflow",
                        "primary_tool": requested_tool_name,
                        "implementation_tool": alias_info.new_name,
                        "canonical_tool": alias_info.new_name,
                        "injected_action": alias_info.inject_action,
                        "note": alias_info.migration_note,
                    }

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
        if alias_info:
            full_response["tool"]["canonical_name"] = tool_name
            full_response["tool"]["implementation_name"] = tool_name
            full_response["tool"]["role"] = "primary_agent_workflow"
            full_response["alias"] = {
                "reason": alias_info.reason,
                "role": "primary_agent_workflow",
                "primary_tool": requested_tool_name,
                "implementation_tool": alias_info.new_name,
                "canonical_tool": alias_info.new_name,
                "injected_action": alias_info.inject_action,
                "note": alias_info.migration_note,
            }
        deprecation = tool_catalog.describe_tool_deprecation_block(tool_name)
        if deprecation is not None:
            full_response["deprecation"] = deprecation
        return success_response(full_response)
    except Exception as e:
        return [error_response(f"Error describing tool: {str(e)}")]
