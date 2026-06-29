"""Static catalog data for tool introspection responses."""

from typing import Any, Dict, List, Optional


# Deprecation registry surfaced by both list_tools (via TOOL_RELATIONSHIPS)
# and describe_tool (via describe_tool_deprecation_block). Keeping the
# migration string in one place avoids drift between the two surfaces.
DEPRECATION_REGISTRY: Dict[str, Dict[str, str]] = {
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


LITE_PARAMETER_PRIORITIES: Dict[str, List[str]] = {
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

LITE_IDENTITY_FIELDS = {"continuity_token", "client_session_id", "agent_id"}


TOOL_RELATIONSHIPS: Dict[str, Dict[str, Any]] = {
    "start_session": {
        "depends_on": [],
        "related_to": ["onboard", "identity"],
        "category": "identity",
    },
    "sync_state": {
        "depends_on": [],
        "related_to": ["process_agent_update", "check_working_state"],
        "category": "core",
    },
    "check_working_state": {
        "depends_on": [],
        "related_to": ["get_governance_metrics", "sync_state"],
        "category": "core",
    },
    "search_shared_memory": {
        "depends_on": [],
        "related_to": ["knowledge", "leave_note"],
        "category": "knowledge",
    },
    "record_result": {
        "depends_on": ["sync_state"],
        "related_to": ["outcome_event", "process_agent_update"],
        "category": "core",
    },
    "request_review": {
        "depends_on": ["sync_state"],
        "related_to": ["dialectic", "self_recovery"],
        "category": "dialectic",
    },
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


WORKFLOWS: Dict[str, List[str]] = {
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


TOOL_DESCRIPTION_OVERRIDES: Dict[str, str] = {
    "start_session": "Start a UNITARES session; primary workflow name for onboarding",
    "sync_state": "Check in after meaningful work; primary workflow name for state updates",
    "check_working_state": "Read current EISV state without mutating history",
    "search_shared_memory": "Search shared memory before writing duplicate discoveries",
    "record_result": "Record real task/tool/test outcome for calibration",
    "request_review": "Ask for structured review/recovery",
    "onboard": "Register a fresh process identity. Prefer start_session(force_new=true); use parent_agent_id only for real handoffs.",
    "identity": "🪞 Check current binding or set your display name. Not the normal start/resume path; use start_session first.",
    "process_agent_update": "Raw implementation for sync_state(); updates agent governance state",
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
    "archive_old_test_agents": "🧹 Preview stale agent archival candidates",
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


COMMON_PATTERNS: Dict[str, Dict[str, str]] = {
    "process_agent_update": {
        "basic": "process_agent_update(complexity=0.5)  # identity auto-injected",
        "with_response": "process_agent_update(response_text=\"Fixed bug\", complexity=0.3, confidence=0.9)",
        "task_type": "process_agent_update(complexity=0.7, task_type=\"divergent\")"
    },
    "start_session": {
        "fresh": "start_session(force_new=true)",
        "lineage": "start_session(force_new=true, parent_agent_id=\"...\", spawn_reason=\"new_session\")",
    },
    "sync_state": {
        "basic": "sync_state(response_text=\"Fixed bug\", complexity=0.3, confidence=0.9)",
        "compact": "sync_state(response_text=\"Finished task\", complexity=0.5, response_mode=\"compact\")",
        "diagnostic_mirror": "sync_state(response_text=\"Reviewing drift\", complexity=0.5, response_mode=\"mirror\")",
    },
    "check_working_state": {
        "basic": "check_working_state()",
        "full": "check_working_state(lite=false)",
    },
    "search_shared_memory": {
        "by_query": "search_shared_memory(query=\"identity continuity\", limit=5)",
        "with_details": "search_shared_memory(query=\"calibration\", include_details=true)",
    },
    "record_result": {
        "test_passed": "record_result(outcome_type=\"test_passed\", confidence=0.8)",
        "linked": "record_result(outcome_type=\"task_completed\", prediction_id=\"...\", detail={\"summary\":\"...\"})",
    },
    "request_review": {
        "recovery": "request_review(issue_description=\"Paused after conflicting evidence\")",
        "with_reason": "request_review(issue_description=\"Need adversarial review\", reason=\"uncertain root cause\")",
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
        "name_yourself": "identity(name=\"my_agent\")  # Set your display name"
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


def describe_tool_deprecation_block(tool_name: str) -> Optional[Dict[str, Any]]:
    """Return a deprecation block for the named tool, or None if not deprecated."""
    entry = DEPRECATION_REGISTRY.get(tool_name)
    if entry is None:
        return None
    return {
        "deprecated": True,
        "deprecated_since": entry["deprecated_since"],
        "superseded_by": entry["superseded_by"],
        "migration": entry["migration"],
    }


def getting_started_path() -> List[Dict[str, Any]]:
    """Primary low-friction path for first-time governance callers."""
    return [
        {
            "step": 1,
            "tool": "start_session",
            "call": "start_session(force_new=true)",
            "canonical_tool": "onboard",
            "implementation_tool": "onboard",
            "why": "Mint a fresh process identity. If continuing prior work, include parent_agent_id and spawn_reason='new_session'.",
        },
        {
            "step": 2,
            "tool": "sync_state",
            "call": "sync_state(response_text='what changed', complexity=0.5, confidence=0.7)",
            "canonical_tool": "process_agent_update",
            "implementation_tool": "process_agent_update",
            "why": "Record meaningful work and receive a governance verdict.",
        },
        {
            "step": 3,
            "tool": "check_working_state",
            "call": "check_working_state()",
            "canonical_tool": "get_governance_metrics",
            "implementation_tool": "get_governance_metrics",
            "why": "Inspect current EISV state without mutating history.",
        },
        {
            "step": 4,
            "tool": "search_shared_memory",
            "call": "search_shared_memory(query='topic')",
            "canonical_tool": "knowledge(action='search')",
            "implementation_tool": "knowledge(action='search')",
            "why": "Reuse shared memory before writing duplicate discoveries.",
        },
        {
            "step": 5,
            "tool": "list_tools",
            "call": "list_tools(essential_only=true)",
            "why": "Stay in the small core tool set until the workflow needs more surface area.",
        },
    ]


def essential_toolkit() -> Dict[str, Any]:
    """Short orientation block for agents trying not to drown in the tool list."""
    return {
        "default_path": [item["tool"] for item in getting_started_path()],
        "small_surface": "Use list_tools(essential_only=true) or list_tools(lite=true) before exploring the full registry.",
        "preferred_consolidated_tools": {
            "knowledge": "Use action='search'|'note'|'store' instead of older KG-specific tools.",
            "dialectic": "Use action='quick' for simple decision triage; use request/thesis/antithesis/synthesis for paused-state recovery.",
            "calibration": "Use action='check' first; add ground truth with action='update' only when you have trusted external evidence.",
            "export": "Use action='history' for in-memory export; action='file' writes a server-side file.",
        },
    }


def common_patterns_for(tool_name: str) -> Dict[str, str]:
    """Get common usage patterns for a tool."""
    return COMMON_PATTERNS.get(tool_name, {})
