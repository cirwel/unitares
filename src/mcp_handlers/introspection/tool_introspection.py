"""
Tool introspection handlers (list_tools, describe_tool).

Extracted from admin.py for maintainability.
"""

from typing import Dict, Any, List, Sequence, Optional
from mcp.types import TextContent
from ..utils import success_response, error_response
from ..decorators import mcp_tool
from ..support.coerce import coerce_bool
from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
logger = get_logger(__name__)


# Deprecation registry surfaced by both list_tools (via tool_relationships)
# and describe_tool (via _describe_tool_deprecation_block). Keeping the
# migration string in one place avoids drift between the two surfaces.
# Mirrored into tool_relationships in handle_list_tools below.
_DEPRECATION_REGISTRY: Dict[str, Dict[str, str]] = {
    "leave_note": {
        "deprecated_since": "2026-05-20",
        "superseded_by": "knowledge",
        "migration": (
            "MCP tool surface: call `knowledge` with action='note', "
            "summary='...', tags=[...]. unitares_sdk callers can keep using "
            "client.leave_note() — that method now routes through `knowledge` "
            "internally (same audit shape, same DB row)."
        ),
    },
    "request_dialectic_review": {
        "deprecated_since": "2026-01-29",
        "superseded_by": "self_recovery_review",
        "migration": "Use self_recovery_review(reflection='...') instead",
    },
    "direct_resume_if_safe": {
        "deprecated_since": "2026-01-29",
        "superseded_by": "quick_resume",
        "migration": (
            "Use quick_resume() if coherence > 0.60 and risk < 0.40, "
            "otherwise use self_recovery_review(reflection='...')"
        ),
    },
}


def _describe_tool_deprecation_block(tool_name: str) -> Optional[Dict[str, Any]]:
    """Return a deprecation block for the named tool, or None if not deprecated.

    Surfaces server-side deprecation metadata in describe_tool responses so
    agents don't have to call list_tools to learn the migration story.
    """
    entry = _DEPRECATION_REGISTRY.get(tool_name)
    if entry is None:
        return None
    return {
        "deprecated": True,
        "deprecated_since": entry["deprecated_since"],
        "superseded_by": entry["superseded_by"],
        "migration": entry["migration"],
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


_LITE_PARAMETER_PRIORITIES: Dict[str, List[str]] = {
    "process_agent_update": [
        "client_session_id",
        "response_text",
        "complexity",
        "confidence",
        "task_type",
        "response_mode",
        "require_strong_identity",
        "recent_tool_results",
    ],
    "outcome_event": [
        "confidence",
        "prediction_id",
        "decision_action",
        "session_id",
        "verification_source",
        "detail",
    ],
}

_LITE_IDENTITY_FIELDS = {"continuity_token", "client_session_id", "agent_id"}


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


def _getting_started_path() -> List[Dict[str, Any]]:
    """Canonical low-friction path for first-time governance callers."""
    return [
        {
            "step": 1,
            "tool": "onboard",
            "call": "onboard(force_new=true)",
            "why": "Mint a fresh process identity. If continuing prior work, include parent_agent_id and spawn_reason='new_session'.",
        },
        {
            "step": 2,
            "tool": "process_agent_update",
            "call": "process_agent_update(response_text='what changed', complexity=0.5, confidence=0.7)",
            "why": "Record meaningful work and receive a governance verdict.",
        },
        {
            "step": 3,
            "tool": "get_governance_metrics",
            "call": "get_governance_metrics()",
            "why": "Inspect current EISV state without mutating history.",
        },
        {
            "step": 4,
            "tool": "knowledge",
            "call": "knowledge(action='search', query='topic') or knowledge(action='note', content='short note')",
            "why": "Reuse shared memory before writing; leave lightweight notes when useful.",
        },
        {
            "step": 5,
            "tool": "list_tools",
            "call": "list_tools(essential_only=true)",
            "why": "Stay in the small core tool set until the workflow needs more surface area.",
        },
    ]


def _essential_toolkit() -> Dict[str, Any]:
    """Short orientation block for agents trying not to drown in the tool list."""
    return {
        "default_path": [item["tool"] for item in _getting_started_path()],
        "small_surface": "Use list_tools(essential_only=true) or list_tools(lite=true) before exploring the full registry.",
        "preferred_consolidated_tools": {
            "knowledge": "Use action='search'|'note'|'store' instead of older KG-specific tools.",
            "dialectic": "Use action='quick' for simple decision triage; use request/thesis/antithesis/synthesis for paused-state recovery.",
            "calibration": "Use action='check' first; add ground truth with action='update' only when you have trusted external evidence.",
            "export": "Use action='history' for in-memory export; action='file' writes a server-side file.",
        },
    }

@mcp_tool("list_tools", timeout=10.0, rate_limit_exempt=True, requires_identity="pre_onboard")
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
    registered_tool_names = sorted(TOOL_HANDLERS.keys())
    
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
    DEPRECATED_TOOLS = set(list_all_aliases().keys())

    # Define tool relationships and workflows
    tool_relationships = {
        "process_agent_update": {
            "depends_on": [],  # No deps - identity auto-creates
            "related_to": ["simulate_update", "get_governance_metrics", "get_system_history"],
            "category": "core"
        },
        "get_governance_metrics": {
            "depends_on": [],
            "related_to": ["process_agent_update", "observe_agent", "get_system_history"],
            "category": "core"
        },
        "simulate_update": {
            "depends_on": [],
            "related_to": ["process_agent_update", "get_governance_metrics"],
            "category": "core"
        },
        "get_thresholds": {
            "depends_on": [],
            "related_to": ["set_thresholds", "process_agent_update"],
            "category": "config"
        },
        "set_thresholds": {
            "depends_on": ["get_thresholds"],
            "related_to": ["get_thresholds", "process_agent_update"],
            "category": "config"
        },
        "observe_agent": {
            "depends_on": ["list_agents"],
            "related_to": ["get_governance_metrics", "compare_agents", "detect_anomalies"],
            "category": "observability"
        },
        "compare_agents": {
            "depends_on": ["list_agents"],
            "related_to": ["observe_agent", "aggregate_metrics", "detect_anomalies"],
            "category": "observability"
        },
        "detect_anomalies": {
            "depends_on": ["list_agents"],
            "related_to": ["observe_agent", "compare_agents", "aggregate_metrics"],
            "category": "observability"
        },
        "aggregate_metrics": {
            "depends_on": [],
            "related_to": ["observe_agent", "compare_agents", "detect_anomalies"],
            "category": "observability"
        },
        "list_agents": {
            "depends_on": [],
            "related_to": ["get_agent_metadata", "identity"],
            "category": "lifecycle"
        },
        "get_agent_metadata": {
            "depends_on": ["list_agents"],
            "related_to": ["list_agents", "update_agent_metadata"],
            "category": "lifecycle"
        },
        "update_agent_metadata": {
            "depends_on": ["list_agents"],
            "related_to": ["get_agent_metadata", "list_agents"],
            "category": "lifecycle"
        },
        "archive_agent": {
            "depends_on": ["list_agents"],
            "related_to": ["list_agents", "delete_agent"],
            "category": "lifecycle"
        },
        "delete_agent": {
            "depends_on": ["list_agents"],
            "related_to": ["archive_agent", "list_agents"],
            "category": "lifecycle"
        },
        "archive_old_test_agents": {
            "depends_on": [],
            "related_to": ["archive_agent", "list_agents"],
            "category": "lifecycle"
        },
        # get_agent_api_key REMOVED - aliased to identity()
        "mark_response_complete": {
            "depends_on": [],
            "related_to": ["process_agent_update", "get_agent_metadata"],
            "category": "lifecycle"
        },
        "request_dialectic_review": {
            "deprecated": True,
            "deprecated_since": "2026-01-29",
            "superseded_by": "self_recovery_review",
            "depends_on": ["get_agent_metadata"],
            "related_to": ["self_recovery_review", "dialectic"],
            "category": "lifecycle",
            "migration": "Use self_recovery_review(reflection='...') instead"
        },
        "direct_resume_if_safe": {
            "deprecated": True,
            "deprecated_since": "2026-01-29",
            "superseded_by": "quick_resume",
            "depends_on": [],
            "related_to": ["quick_resume", "self_recovery_review", "check_recovery_options"],
            "category": "lifecycle",
            "migration": "Use quick_resume() if coherence > 0.60 and risk < 0.40, otherwise use self_recovery_review(reflection='...')"
        },
        "self_recovery_review": {
            "depends_on": ["get_governance_metrics"],
            "related_to": ["quick_resume", "check_recovery_options"],
            "replaces": ["direct_resume_if_safe", "request_dialectic_review"],
            "category": "lifecycle",
            "recovery_hierarchy": {
                "fastest": "quick_resume",
                "primary": "self_recovery_review",
                "diagnostic": "check_recovery_options"
            },
            "description": "Primary recovery path - requires reflection but allows recovery at moderate thresholds"
        },
        "quick_resume": {
            "depends_on": ["get_governance_metrics"],
            "related_to": ["self_recovery_review", "check_recovery_options"],
            "category": "lifecycle",
            "recovery_hierarchy": {
                "fastest": "quick_resume",
                "primary": "self_recovery_review",
                "diagnostic": "check_recovery_options"
            },
            "description": "Fastest recovery path - no reflection needed, but requires very safe state"
        },
        "check_recovery_options": {
            "depends_on": ["get_governance_metrics"],
            "related_to": ["self_recovery_review", "quick_resume"],
            "category": "lifecycle",
            "description": "Read-only diagnostic tool to check recovery eligibility"
        },
        "get_system_history": {
            "depends_on": ["list_agents"],
            "related_to": ["export_to_file", "get_governance_metrics", "observe_agent"],
            "category": "export"
        },
        "export_to_file": {
            "depends_on": ["get_system_history"],
            "related_to": ["get_system_history"],
            "category": "export"
        },
        "reset_monitor": {
            "depends_on": ["list_agents"],
            "related_to": ["process_agent_update"],
            "category": "admin"
        },
        "get_server_info": {
            "depends_on": [],
            "related_to": ["health_check", "cleanup_stale_locks"],
            "category": "admin"
        },
        "get_connection_status": {
            "depends_on": [],
            "related_to": ["health_check", "get_server_info", "debug_request_context"],
            "category": "admin"
        },
        "health_check": {
            "depends_on": [],
            "related_to": ["get_server_info", "get_telemetry_metrics"],
            "category": "admin"
        },
        "check_calibration": {
            "depends_on": ["update_calibration_ground_truth"],
            "related_to": ["update_calibration_ground_truth"],
            "category": "admin"
        },
        "update_calibration_ground_truth": {
            "depends_on": [],
            "related_to": ["check_calibration"],
            "category": "admin"
        },
        "get_telemetry_metrics": {
            "depends_on": [],
            "related_to": ["health_check", "aggregate_metrics"],
            "category": "admin"
        },
        "get_tool_usage_stats": {
            "depends_on": [],
            "related_to": ["get_telemetry_metrics", "list_tools"],
            "category": "admin"
        },
        "get_workspace_health": {
            "depends_on": [],
            "related_to": ["health_check", "get_server_info"],
            "category": "workspace"
        },
        # Dialectic tools - full protocol restored (Feb 2026)
        "request_dialectic_review": {
            "depends_on": [],
            "related_to": ["submit_thesis", "dialectic"],
            "category": "dialectic"
        },
        "submit_thesis": {
            "depends_on": ["request_dialectic_review"],
            "related_to": ["submit_antithesis", "submit_synthesis"],
            "category": "dialectic"
        },
        "submit_antithesis": {
            "depends_on": ["submit_thesis"],
            "related_to": ["submit_synthesis"],
            "category": "dialectic"
        },
        "submit_synthesis": {
            "depends_on": ["submit_antithesis"],
            "related_to": ["dialectic"],
            "category": "dialectic"
        },
        "dialectic": {
            "depends_on": [],
            "related_to": ["request_dialectic_review", "submit_thesis"],
            "category": "dialectic"
        },
        "cleanup_stale_locks": {
            "depends_on": [],
            "related_to": ["get_server_info"],
            "category": "admin"
        },
        "list_tools": {
            "depends_on": [],
            "related_to": ["describe_tool"],
            "category": "admin"
        },
        "describe_tool": {
            "depends_on": [],
            "related_to": ["list_tools"],
            "category": "admin"
        },
        # nudge_dialectic_session REMOVED - dialectic simplified
        # Knowledge Graph Tools
        "store_knowledge_graph": {
            "depends_on": [],  # No deps - identity auto-binds
            "related_to": ["search_knowledge_graph", "get_knowledge_graph", "list_knowledge_graph"],
            "category": "knowledge"
        },
        "search_knowledge_graph": {
            "depends_on": [],
            "related_to": ["store_knowledge_graph", "get_discovery_details"],
            "category": "knowledge"
        },
        "get_knowledge_graph": {
            "depends_on": ["list_agents"],
            "related_to": ["search_knowledge_graph", "list_knowledge_graph", "get_discovery_details"],
            "category": "knowledge"
        },
        "list_knowledge_graph": {
            "depends_on": [],
            "related_to": ["get_knowledge_graph", "search_knowledge_graph"],
            "category": "knowledge"
        },
        # find_similar_discoveries_graph, get_related_discoveries_graph,
        # get_response_chain_graph, reply_to_question REMOVED - aliased
        "get_discovery_details": {
            "depends_on": ["search_knowledge_graph"],
            "related_to": ["search_knowledge_graph", "update_discovery_status_graph"],
            "category": "knowledge"
        },
        "leave_note": {
            "deprecated": True,
            "deprecated_since": "2026-05-20",
            "superseded_by": "knowledge",
            "depends_on": [],  # No deps - identity auto-binds
            "related_to": ["knowledge", "store_knowledge_graph"],
            "category": "knowledge",
            "migration": (
                "MCP tool surface: call `knowledge` with action='note', "
                "summary='...', tags=[...]. unitares_sdk callers can keep using "
                "client.leave_note() — that method now routes through `knowledge` "
                "internally (same audit shape, same DB row)."
            ),
        },
        "update_discovery_status_graph": {
            "depends_on": ["get_discovery_details"],
            "related_to": ["get_discovery_details", "search_knowledge_graph"],
            "category": "knowledge"
        },
        # Identity Tools - Dec 2025: onboard() is portal, identity() is primary
        "onboard": {
            "depends_on": [],
            "related_to": ["identity", "process_agent_update"],
            "category": "identity"
        },
        "identity": {
            "depends_on": [],
            "related_to": ["onboard", "process_agent_update", "list_agents"],
            "category": "identity"
        },
        # Admin Tools
        "backfill_calibration_from_dialectic": {
            "depends_on": ["check_calibration"],
            "related_to": ["check_calibration", "update_calibration_ground_truth"],
            "category": "admin"
        },
        "validate_file_path": {
            "depends_on": [],
            "related_to": ["get_workspace_health"],
            "category": "admin"
        },
        "debug_request_context": {
            "depends_on": [],
            "related_to": ["get_server_info", "identity"],
            "category": "admin"
        },
        # Observability Tools
        "compare_me_to_similar": {
            "depends_on": ["get_governance_metrics"],
            "related_to": ["compare_agents", "observe_agent"],
            "category": "observability"
        },
        # Dialectic Tools - Feb 2026: Consolidated into dialectic(action=get/list)
        "dialectic": {
            "depends_on": [],
            "related_to": ["request_dialectic_review", "process_agent_update"],
            "category": "dialectic"
        },
        # Consolidated tools (common tier) - Feb 2026 dogfood fix
        "agent": {
            "depends_on": [],
            "related_to": ["onboard", "identity", "observe"],
            "category": "lifecycle"
        },
        "calibration": {
            "depends_on": ["process_agent_update"],
            "related_to": ["process_agent_update", "observe"],
            "category": "core"
        },
        "call_model": {
            "depends_on": [],
            "related_to": ["knowledge", "dialectic"],
            "category": "core"
        },
        "config": {
            "depends_on": [],
            "related_to": ["get_thresholds", "set_thresholds"],
            "category": "config"
        },
        "export": {
            "depends_on": [],
            "related_to": ["get_system_history", "observe"],
            "category": "export"
        },
        "knowledge": {
            "depends_on": [],
            "related_to": ["search_knowledge_graph", "leave_note"],
            "category": "knowledge"
        },
        "observe": {
            "depends_on": [],
            "related_to": ["agent", "process_agent_update"],
            "category": "observability"
        },
        # "pi" introspection entry lives in unitares-pi-plugin when that
        # plugin is installed; omitted here so OSS builds don't advertise
        # an embodied-system tool that isn't loaded.
    }
    
    # Define common workflows
    workflows = {
        "onboarding": [
            "onboard",  # 🚀 Portal tool - call FIRST
            "process_agent_update",  # Start working
            "identity",  # (Optional) Check/name yourself later
            "list_agents"  # See who else is here
        ],
        "monitoring": [
            "list_agents",
            "get_governance_metrics",
            "observe_agent",
            "aggregate_metrics",
            "detect_anomalies"
        ],
        "governance_cycle": [
            "process_agent_update",
            "get_governance_metrics"
        ],
        "recovery": [
            "dialectic",  # View dialectic sessions (action=get/list)
            "self_recovery"  # Resume if state is safe
        ],
        "export_analysis": [
            "get_system_history",
            "export_to_file"
        ]
    }
    
    # Build tools list dynamically from registered tools
    # Description mapping for tools (fallback to generic if not found)
    tool_descriptions = {
        "onboard": "Register fresh process-instance with governance. Per v2 ontology, declare lineage via parent_agent_id rather than resume via token.",
        "identity": "🪞 Check who you are or set your display name. Per v2 ontology, arg-less identity() with no proof signal mints fresh; pass continuity_token / agent_uuid + proof to resume.",
        "process_agent_update": "💬 Share your work and get supportive feedback. Your main check-in tool",
        "get_governance_metrics": "📊 Get current state and metrics without updating",
        "simulate_update": "🧪 Test decisions without persisting state",
        "get_thresholds": "⚙️ View current threshold configuration",
        "set_thresholds": "⚙️ Set runtime threshold overrides",
        "observe_agent": "👁️ View agent state and patterns (collaborative awareness)",
        "compare_agents": "🔍 Compare state patterns across agents",
        "detect_anomalies": "🚨 Scan for unusual patterns across fleet",
        "aggregate_metrics": "📈 Fleet-level health overview",
        "list_agents": "👥 List all agents with lifecycle metadata",
        "get_agent_metadata": "📋 Full metadata for single agent (accepts UUID or label)",
        "update_agent_metadata": "✏️ Update tags and notes",
        "archive_agent": "📦 Archive for long-term storage",
        "delete_agent": "🗑️ Delete agent (protected for pioneers)",
        "archive_old_test_agents": "🧹 Auto-archive stale test agents",
        "mark_response_complete": "✅ Mark agent as having completed response, waiting for input",
        "self_recovery": "▶️ Self-recovery: use action='quick' for safe states, action='review' for full recovery with reflection",
        "get_system_history": "📜 Export time-series history (inline)",
        "export_to_file": "💾 Export history to JSON/CSV file",
        "reset_monitor": "🔄 Reset agent state",
        "get_server_info": "ℹ️ Server version, PID, uptime, health",
        # Knowledge Graph (Fast, indexed, transparent)
        "store_knowledge_graph": "💡 Store knowledge discovery in graph (fast, non-blocking)",
        "search_knowledge_graph": "🔎 Search knowledge graph by tags, type, agent (indexed queries)",
        "get_knowledge_graph": "📚 Get all knowledge for an agent (fast index lookup)",
        "list_knowledge_graph": "📊 List knowledge graph statistics (full transparency)",
        "update_discovery_status_graph": "🔄 Update discovery status or content/metadata on an existing discovery",
        "leave_note": "📝 Leave a quick note in the knowledge graph (minimal friction)",
        "list_tools": "📚 Discover all available tools. Your guide to what's possible",
        "describe_tool": "📖 Get full details for a specific tool. Deep dive into any tool",
        "cleanup_stale_locks": "🧹 Clean up stale lock files from crashed/killed processes",
        "dialectic": "📋 Dialectic operations: get, list, request, thesis, antithesis, synthesis, vote, reassign",
        "health_check": "🏥 Quick health check - system status and component health",
        "check_calibration": "📏 Check calibration of confidence estimates",
        "update_calibration_ground_truth": "📝 Record external truth signal for calibration (optional)",
        "get_telemetry_metrics": "📊 Get comprehensive telemetry metrics",
        "get_workspace_health": "🏥 Get comprehensive workspace health status",
        "get_tool_usage_stats": "📈 Get tool usage statistics to identify which tools are actually used vs unused",
    }
    
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
    def get_usage_data(window_hours: int = 168) -> Dict[str, Dict[str, Any]]:
        """Get tool usage statistics for ordering."""
        try:
            from src.tool_usage_tracker import get_tool_usage_tracker
            tracker = get_tool_usage_tracker()
            stats = tracker.get_usage_stats(window_hours=window_hours)
            return stats.get("tools", {})
        except Exception:
            return {}
    
    def order_tools_by_usage(tools: List[Dict[str, Any]], usage_data: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Order tools by usage frequency, fallback to tier-based ordering."""
        # Tier priority for fallback (essential > common > advanced)
        tier_priority = {"essential": 3, "common": 2, "advanced": 1}
        
        def sort_key(tool: Dict[str, Any]) -> tuple:
            tool_name = tool["name"]
            call_count = usage_data.get(tool_name, {}).get("call_count", 0)
            tier_prio = tier_priority.get(tool.get("tier", "common"), 0)
            # Primary: usage count (descending), Secondary: tier priority (descending)
            return (-call_count, -tier_prio)
        
        return sorted(tools, key=sort_key)
    
    # Apply progressive ordering if enabled
    usage_data = {}
    if progressive:
        usage_data = get_usage_data()
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
            order = ["onboard", "identity", "process_agent_update", "get_governance_metrics",
                     "list_tools", "describe_tool", "list_agents", "health_check",
                     "store_knowledge_graph", "search_knowledge_graph", "leave_note"]
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
                "new_agent": ["onboard(force_new=true)", "process_agent_update(response_text='...', complexity=0.5)", "agent(action='list') or list_agents()"],
                "check_in": ["process_agent_update(response_text='...', complexity=0.5)"],
                "save_insight": ["knowledge(action='note', content='...')", "OR knowledge(action='store', summary='...', tags=[...])"],
                "find_info": ["knowledge(action='search', query='...')", "OR knowledge(action='search', tags=[...])"]
            },
            # Common signatures (type hints at a glance)
            "signatures": {
                "process_agent_update": "(complexity:float, response_text?:str, confidence?:float, task_type?:str)",
                "store_knowledge_graph": "(summary:str, tags?:list, severity?:str, details?:str)",
                "search_knowledge_graph": "(query?:str, tags?:list, limit?:int, include_details?:bool)",
                "knowledge_search": "(action='search', query?:str, tags?:list, limit?:int, include_details?:bool)",
                "leave_note": "(summary:str, tags?:list)"
            },
            "more": "list_tools(lite=false) for all tools with full category details",
            "tip": "describe_tool(tool_name=...) for parameter details and examples",
            "quick_start": "Start fresh with onboard(force_new=true); use parent_agent_id for lineage, not bare UUID resume",
            "getting_started_path": _getting_started_path(),
            "essential_toolkit": _essential_toolkit(),
        }
        
        # Add first-time hint for new agents
        if is_new_agent:
            response_data["first_time"] = {
                "hint": "👋 First time here? Start with onboard(force_new=true) to create your identity!",
                "next_step": "Call onboard(force_new=true). If inheriting prior work, also pass parent_agent_id and spawn_reason='new_session'."
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
                call_count = usage_data.get(tool_name, {}).get("call_count", 0)
                
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
                usage_data = get_usage_data()
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
            "path": _getting_started_path(),
            "essential_toolkit": _essential_toolkit(),
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
                "1. Call onboard(force_new=true) - creates a fresh process identity",
                "2. If inheriting prior work, include parent_agent_id and spawn_reason='new_session'",
                "3. Save uuid plus continuity diagnostics from response",
                "4. Call process_agent_update() to share meaningful work",
                "5. Use identity(name='...') to set a cosmetic label"
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
│  ┌─────────┐                                                        │
│  │ onboard │──────────────────┐                                     │
│  └────┬────┘                  │                                     │
│       │                       ▼                                     │
│       │              ┌──────────────┐                               │
│       │              │   identity   │ ◄── name yourself             │
│       │              └──────────────┘                               │
│       │                                                             │
│       ▼                                                             │
│  ┌────────────────────────┐       ┌─────────────────────────────┐  │
│  │ process_agent_update   │◄─────►│ get_governance_metrics      │  │
│  │ (main check-in)        │       │ (view state)                │  │
│  └───────────┬────────────┘       └─────────────────────────────┘  │
│              │                                                      │
│              ├───────────────────────────────────────┐              │
│              │                                       │              │
│              ▼                                       ▼              │
│  ┌───────────────────────┐              ┌────────────────────────┐ │
│  │ KNOWLEDGE GRAPH       │              │ OBSERVABILITY          │ │
│  ├───────────────────────┤              ├────────────────────────┤ │
│  │ store_knowledge_graph │              │ list_agents            │ │
│  │ search_knowledge_graph│              │ observe_agent          │ │
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

@mcp_tool("describe_tool", timeout=10.0, rate_limit_exempt=True, requires_identity="pre_onboard")
async def handle_describe_tool(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Return full details for a single tool (full description + full schema) on demand.
    This is intended to keep MCP tool lists compact while still enabling deep discovery.
    
    LITE MODE: Use lite=true to get a simplified schema suitable for smaller models.
    Shows only required params + key optional params with simple examples.
    """
    try:
        tool_name = (arguments.get("tool_name") or "").strip()
        if not tool_name:
            return [error_response(
                "tool_name is required",
                recovery={
                    "action": "Call list_tools to find the canonical name, then call describe_tool(tool_name=...)",
                    "related_tools": ["list_tools"],
                },
            )]

        include_schema = arguments.get("include_schema", True)
        include_full_description = arguments.get("include_full_description", True)
        # LITE-FIRST: Simpler schemas by default for local models
        lite = arguments.get("lite", True)

        from src.tool_schemas import get_tool_definitions
        tools = get_tool_definitions(verbosity="full")
        tool = next((t for t in tools if t.name == tool_name), None)
        if tool is None:
            return [error_response(
                f"Unknown tool: {tool_name}",
                recovery={
                    "action": "Call list_tools to see available tool names",
                    "related_tools": ["list_tools"],
                },
                context={"tool_name": tool_name},
            )]

        description = tool.description
        if not include_full_description:
            description = (tool.description or "").splitlines()[0].strip() if tool.description else ""

        # Helper function to get common patterns (shared between both branches)
        def get_common_patterns(tool_name: str) -> dict:
                """Get common usage patterns for a tool."""
                patterns = {
                    "process_agent_update": {
                        "basic": "process_agent_update(complexity=0.5)  # identity auto-injected",
                        "with_response": "process_agent_update(response_text=\"Fixed bug\", complexity=0.3, confidence=0.9)",
                        "task_type": "process_agent_update(complexity=0.7, task_type=\"divergent\")"
                    },
                    "store_knowledge_graph": {
                        "insight": "store_knowledge_graph(summary=\"Key insight about X\", tags=[\"insight\"])",
                        "bug_found": "store_knowledge_graph(summary=\"Bug in module Y\", tags=[\"bug\"], severity=\"medium\")",
                        "question": "store_knowledge_graph(summary=\"How does X work?\", discovery_type=\"question\")"
                    },
                    "search_knowledge_graph": {
                        "by_tag": "knowledge(action=\"search\", tags=[\"bug\"], limit=10)",
                        "by_type": "knowledge(action=\"search\", discovery_type=\"insight\", limit=5)",
                        "full_text": "knowledge(action=\"search\", query=\"authentication\", limit=10)"
                    },
                    "knowledge": {
                        "quick_note": "knowledge(action=\"note\", content=\"Short shared note\", tags=[\"topic\"])",
                        "store_discovery": "knowledge(action=\"store\", summary=\"Found issue\", discovery_type=\"bug_found\", severity=\"medium\")",
                        "search": "knowledge(action=\"search\", query=\"authentication\", limit=10)"
                    },
                    "dialectic": {
                        "quick_decision": "dialectic(action=\"quick\", issue_description=\"...\", position=\"...\", concerns=[...])",
                        "request_recovery": "dialectic(action=\"request\", issue_description=\"Agent is paused because ...\")",
                        "submit_thesis": "dialectic(action=\"thesis\", session_id=\"...\", root_cause=\"...\", proposed_conditions=[...])"
                    },
                    "calibration": {
                        "check": "calibration(action=\"check\")",
                        "update_truth": "calibration(action=\"update\", confidence=0.8, actual_correct=true)",
                        "rebuild_preview": "calibration(action=\"rebuild\", dry_run=true)"
                    },
                    "export": {
                        "history": "export(action=\"history\", format=\"json\")",
                        "file": "export(action=\"file\", format=\"json\", filename=\"agent_history\")"
                    },
                    "get_governance_metrics": {
                        "check_state": "get_governance_metrics()  # uses bound identity",
                        "with_history": "get_governance_metrics(include_history=true)"
                    },
                    "identity": {
                        "check_identity": "identity()  # Shows current bound identity",
                        "name_yourself": "identity(name=\"my_agent\")  # Set your display name",
                        "proof_owned_rebind": "identity(agent_uuid=\"...\", continuity_token=\"...\", resume=true)  # Same-owner rebind"
                    },
                    "list_agents": {
                        "all_agents": "list_agents()  # List all agents with metadata",
                        "active_only": "list_agents(status_filter=\"active\")  # Only active agents",
                        "with_metrics": "list_agents(include_metrics=true)  # Include governance metrics",
                        "lite_view": "list_agents(summary_only=true)  # Minimal summary view"
                    },
                    "observe_agent": {
                        "basic_observation": "observe_agent(agent_id=\"my_agent\")  # Analyze agent patterns",
                        "with_history": "observe_agent(agent_id=\"my_agent\", include_history=true)  # Include historical patterns",
                        "pattern_analysis": "observe_agent(agent_id=\"my_agent\", analyze_patterns=true)  # Deep pattern analysis"
                    }
                }
                return patterns.get(tool_name, {})

        # === LITE MODE: Simplified schema for smaller models ===
        if lite:
            # Try Pydantic schema first for structured lite output
            lite_schema = None
            try:
                from src.tool_schemas import get_pydantic_schemas
                pydantic_model = get_pydantic_schemas().get(tool_name)
                if pydantic_model:
                    schema = pydantic_model.model_json_schema()
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
                        if field_name in _LITE_IDENTITY_FIELDS and not force:
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
                    for field_name in _LITE_PARAMETER_PRIORITIES.get(tool_name, []):
                        if add_lite_field(field_name, force=True):
                            shown += 1

                    for field_name in properties:
                        if field_name in required_fields:
                            continue
                        if field_name in shown_fields:
                            continue
                        if shown >= 5 and tool_name not in _LITE_PARAMETER_PRIORITIES:
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
                common_patterns = get_common_patterns(tool_name)

                # Get parameter aliases for discoverability
                from ..validators import PARAM_ALIASES
                tool_aliases = PARAM_ALIASES.get(tool_name, {})

                # UX FIX (Feb 2026): Add tier information to help agents understand tool complexity
                from src.tool_modes import TOOL_TIERS, TOOL_OPERATIONS
                tool_tier = "common"  # Default
                if tool_name in TOOL_TIERS["essential"]:
                    tool_tier = "essential"
                elif tool_name in TOOL_TIERS["advanced"]:
                    tool_tier = "advanced"

                tier_guidance = {
                    "essential": "Core tool - regularly used for basic workflows",
                    "common": "Standard tool - commonly used for specific tasks",
                    "advanced": "Advanced tool - use when you need specialized functionality"
                }

                response_data = {
                    "tool": tool_name,
                    "description": (description or "").splitlines()[0].strip(),
                    "tier": tool_tier,
                    "tier_note": tier_guidance.get(tool_tier, ""),
                    "operation": TOOL_OPERATIONS.get(tool_name, "read"),  # read/write/admin
                    "parameters": params_simple,
                    "note": "Lite mode - use describe_tool(tool_name=..., lite=false) for full schema"
                }

                deprecation = _describe_tool_deprecation_block(tool_name)
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
                schema = tool.inputSchema or {}
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
                common_patterns = get_common_patterns(tool_name)

                # Get parameter aliases for discoverability
                from ..validators import PARAM_ALIASES
                tool_aliases = PARAM_ALIASES.get(tool_name, {})

                response_data = {
                    "tool": tool_name,
                    "description": (description or "").splitlines()[0].strip(),
                    "parameters": params_simple,
                    "note": "Lite mode - use describe_tool(tool_name=..., lite=false) for full schema"
                }

                deprecation = _describe_tool_deprecation_block(tool_name)
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
                "name": tool.name,
                "description": description,
                "inputSchema": tool.inputSchema if include_schema else None,
            }
        }
        deprecation = _describe_tool_deprecation_block(tool_name)
        if deprecation is not None:
            full_response["deprecation"] = deprecation
        return success_response(full_response)
    except Exception as e:
        return [error_response(f"Error describing tool: {str(e)}")]
