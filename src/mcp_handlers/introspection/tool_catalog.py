"""Static catalog data for tool introspection responses."""

from typing import Any, Dict, List, Optional

from src.governance_glossary import EISV_INLINE_SUMMARY
from src.tool_meta import tool_relationships
from src.mcp_handlers.identity_bootstrap import SET_DISPLAY_NAME_CALL


# Deprecation registry surfaced by both list_tools (via TOOL_RELATIONSHIPS)
# and describe_tool (via describe_tool_deprecation_block). Keeping the
# migration string in one place avoids drift between the two surfaces.
#
# A MIGRATION TARGET MUST BE CALLABLE (2026-08-29)
# ------------------------------------------------------------------
# Every name a deprecation surface tells an agent to call -- `superseded_by`,
# and every `name(` token inside `migration` -- must be a register=True
# dispatch tool or an alias of one. Naming a register=False delegate reads as
# advice and behaves as a dead end: the agent calls it and gets
# tool_not_found_error, with the deprecated tool's own working handler sitting
# right there unused.
#
# Both entries of 2026-08-29 were in that state: `direct_resume_if_safe`
# pointed at `quick_resume` and `request_dialectic_review` at
# `self_recovery_review`; neither is registered -- both are internal delegates
# of `self_recovery`, reachable only as `self_recovery(action="quick"|"review")`.
# Same defect class as the dangling `direct_resume_if_safe -> quick_resume`
# alias removed in #1994, one surface over: that fix repaired what the alias
# *dispatched* to and left what the deprecation block *says* untouched.
# `direct_resume_if_safe` itself was removed on 2026-09-07, seven months after
# its deprecation; `request_dialectic_review` followed on 2026-09-28.
#
# Guarded by SUPERSEDED_BY_TARGET_MISSING / MIGRATION_TARGET_MISSING in
# scripts/dev/tool_edge_index.py and by
# test_every_deprecation_surface_names_a_callable_tool.
DEPRECATION_REGISTRY: Dict[str, Dict[str, str]] = {
    # leave_note was listed here from the #429 consolidation until 2026-08-29,
    # when the operator settled that it is NOT deprecated: it is a first-class
    # low-friction tool in LITE_MODE_TOOLS and in the shared contract. Sharing
    # an implementation with knowledge(action='note') is not supersession.
    # request_dialectic_review, the last entry, went with its alias on
    # 2026-09-28: the name no longer dispatches, so there is nothing callable
    # to describe as deprecated. Use dialectic(action='request').
}


LITE_PARAMETER_PRIORITIES: Dict[str, List[str]] = {
    "request_review": [
        "issue_description",
        "reasoning",
        "root_cause",
        "proposed_conditions",
        "use_brief_as_thesis",
    ],
    "consult": [
        "brief",
        "purpose",
        "effort",
        "privacy",
        "allow_degraded",
        "response_mode",
    ],
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


# One relationship record per advertised name, derived from the one record
# per tool in src/tool_meta.py (2026-09-07). list_tools(category=...) filters
# on the `category` here, which is by construction the category
# tool_modes.TOOL_CATEGORIES puts the name in. The literal this replaces
# carried 87 entries: the roster, 35 legacy alias names that no surface ever
# read (list_tools lists registered tools and workflow aliases only), and the
# key "dialectic" twice, with the second entry silently winning.
TOOL_RELATIONSHIPS: Dict[str, Dict[str, Any]] = tool_relationships()


# One presentation record per src/tool_meta.py category: how list_tools
# labels a category. Which tools sit in a category is the roster's business
# (tool_meta.TOOL_CATEGORIES, read through TOOL_RELATIONSHIPS); nothing here
# may name a tool.
#
# Until 2026-09-12 the full list_tools view carried a hand-written
# `categories` dict beside the derived `categories_summary`. It predated the
# router consolidation and was never regenerated: of the 47 names it listed,
# 30 were dispatch-only twins an MCP client cannot call (`list_agents`,
# `store_knowledge_graph`, `get_server_info`, `submit_thesis`, ...) and it
# omitted 33 of the 50 advertised names, every router and workflow alias among
# them (F1 of docs/operations/tool-surface-audit-2026-09-12.md). The labels
# are what was worth keeping; they live here so the names cannot come back.
#
# `priority` orders the categories for a reader (identity first); it is not a
# tier and nothing in the runtime branches on it. `for_new_agents` marks the
# two categories the getting-started block points a fresh caller at.
CATEGORY_PRESENTATION: Dict[str, Dict[str, Any]] = {
    "identity": {
        "icon": "🚀", "name": "Identity & Onboarding", "priority": 1, "for_new_agents": True,
        "description": "Get started - create your identity and set up your session",
    },
    "core": {
        "icon": "💬", "name": "Core Governance", "priority": 2, "for_new_agents": True,
        "description": "Main tools for sharing work and getting feedback",
    },
    "lifecycle": {
        "icon": "👥", "name": "Agent Lifecycle", "priority": 3, "for_new_agents": False,
        "description": "Manage agents, view metadata, and handle agent states",
    },
    "knowledge": {
        "icon": "💡", "name": "Knowledge Graph", "priority": 4, "for_new_agents": False,
        "description": "Store and search discoveries, insights, and notes",
    },
    "observability": {
        "icon": "👁️", "name": "Observability", "priority": 5, "for_new_agents": False,
        "description": "Monitor agents, compare patterns, and detect anomalies",
    },
    "inference": {
        "icon": "🧠", "name": "Inference", "priority": 6, "for_new_agents": False,
        "description": "Ask an advisory model, and list the hosts that serve one",
    },
    "export": {
        "icon": "📊", "name": "Export & History", "priority": 7, "for_new_agents": False,
        "description": "Export governance history and system data",
    },
    "config": {
        "icon": "⚙️", "name": "Configuration", "priority": 8, "for_new_agents": False,
        "description": "Configure thresholds and system settings",
    },
    "admin": {
        "icon": "🔧", "name": "Admin & Diagnostics", "priority": 9, "for_new_agents": False,
        "description": "System administration, health checks, and diagnostics",
    },
    "workspace": {
        "icon": "📁", "name": "Workspace", "priority": 10, "for_new_agents": False,
        "description": "Workspace health and file validation",
    },
    "dialectic": {
        "icon": "💭", "name": "Dialectic", "priority": 11, "for_new_agents": False,
        "description": "Structured peer review and recovery protocol",
    },
}


def category_presentation(category: str) -> Dict[str, Any]:
    """The presentation record for ``category``, or a generic one for a
    category the table does not know (a plugin's, or a typo the roster test
    will catch): unknown categories get a neutral label, never a crash."""
    record = CATEGORY_PRESENTATION.get(category)
    if record is not None:
        return record
    label = category.title() if isinstance(category, str) and category else "Other"
    return {
        "icon": "🔹", "name": label, "priority": len(CATEGORY_PRESENTATION) + 1,
        "for_new_agents": False, "description": f"{label} tools",
    }


# Served by list_tools(lite=false) under `workflows`. Every entry is an
# advertised name or a call shape against an advertised router: a step a
# schema-driven client can take from tools/list alone. Six entries named a
# dispatch-only twin (`list_agents`, `observe_agent`, `aggregate_metrics`,
# `detect_anomalies`, `get_system_history`, `export_to_file`) until
# 2026-09-12; the hint scanner seeds WORKFLOWS by name, and
# tests/test_list_tools_names_the_wire.py holds the served payload to the
# mount.
WORKFLOWS: Dict[str, List[str]] = {
    "model_help": [
        "consult",
        "request_review",
    ],
    "onboarding": [
        "onboard",  # 🚀 Portal tool - call FIRST
        "process_agent_update",  # Start working
        "identity",  # (Optional) Check/name yourself later
        "agent(action='list')",  # See who else is here
    ],
    "monitoring": [
        "agent(action='list')",
        "get_governance_metrics",
        "observe(action='agent')",
        "observe(action='aggregate')",
        "observe(action='anomalies')",
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
        "export(action='history')",
        "export(action='file')",
    ],
}


# Descriptions for DISPATCH-ONLY alias names: the pre-consolidation names
# (list_agents, observe_agent, get_server_info, ...) that resolve through
# src/mcp_handlers/tool_stability.py but are never on the wire, and which
# describe_tool still answers for. Nothing else belongs here.
#
# An advertised name -- a registered tool or one of the eight workflow aliases
# -- has exactly one description: src/tool_descriptions.py for a tool, the
# ToolAlias.migration_note for a workflow alias. tools/list serves it,
# list_tools serves its first line, and describe_tool opens with the same
# line. Until 2026-09-12 this table also carried 28 advertised names and
# outranked the wire in list_tools, so the rewrites of #2148, #2151 and #2158
# reached MCP clients and never reached orientation (audit F2): `identity`
# still read "Check current binding or set your display name" while the wire
# warned that an argument-less call may mint and persist a new identity, and
# the `dialectic` entry was a hand-maintained action list that had drifted
# twice. test_override_table_carries_no_advertised_name pins the scope.
# Since 2026-09-28 it holds only get_server_info: every other entry described
# a pre-consolidation name whose alias was removed.
TOOL_DESCRIPTION_OVERRIDES: Dict[str, str] = {
    # The one dispatch-only alias left after the 2026-09-28 cut (kept for the
    # Wave 3a BEAM route); without this it would describe itself with its
    # migration note.
    "get_server_info": "ℹ️ Server version, PID, uptime, health",
}


COMMON_PATTERNS: Dict[str, Dict[str, str]] = {
    "process_agent_update": {
        "basic": "process_agent_update(complexity=0.5)  # identity auto-injected",
        "with_response": "process_agent_update(response_text=\"Fixed bug\", complexity=0.3)",
        "task_type": "process_agent_update(complexity=0.7, task_type=\"divergent\")"
    },
    "start_session": {
        "fresh": "start_session(force_new=true)",
        "lineage": "start_session(force_new=true, parent_agent_id=\"<EXITED predecessor>\", spawn_reason=\"explicit\")  # only for a real handoff; a live parent is rejected",
    },
    "sync_state": {
        "basic": "sync_state(response_text=\"Fixed bug\", complexity=0.3, confidence=0.9)",
        "compact": "sync_state(response_text=\"Finished task\", complexity=0.5, response_mode=\"compact\")",
        "diagnostic_mirror": "sync_state(response_text=\"Reviewing drift\", complexity=0.5, response_mode=\"mirror\")",
    },
    "check_working_state": {
        "basic": "check_working_state()",
        "standard": "check_working_state(verbosity=\"standard\")",
        "full": "check_working_state(verbosity=\"full\")",
    },
    "search_shared_memory": {
        "by_query": "search_shared_memory(query=\"identity continuity\", limit=5)",
        "with_details": "search_shared_memory(query=\"calibration\", response_mode=\"full\", include_details=true)",
    },
    "record_result": {
        "test_passed": "record_result(outcome_type=\"test_passed\", confidence=0.8)",
        "linked": "record_result(outcome_type=\"task_completed\", prediction_id=\"...\", detail={\"summary\":\"...\"})",
    },
    "request_review": {
        "one_call": "request_review(issue_description=\"Review my decision to X. I chose X because ... my main uncertainty is ...\")",
        "neutral_two_call": "request_review(issue_description=\"Review X\", use_brief_as_thesis=false)",
        "recovery": "request_review(issue_description=\"Paused after conflicting evidence\", proposed_conditions=[\"Re-check the evidence before resuming\"])",
        "with_reason": "request_review(issue_description=\"Need adversarial review\", reason=\"uncertain root cause\")",
    },
    "consult": {
        "answer": "consult(brief=\"Explain this code\")",
        "critique": "consult(brief=\"Critique this design\", purpose=\"critique\")",
        "local_summary": "consult(brief=\"Summarize this document\", purpose=\"summarize\")",
        "thorough": "consult(brief=\"Analyze this deeply\", effort=\"thorough\", privacy=\"cloud_allowed\")",
        "full_diagnostics": "consult(brief=\"Explain this route\", response_mode=\"full\")",
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
        "check_state": "get_governance_metrics(client_session_id=\"<your client_session_id>\")  # Reads YOUR state; with no proof the read returns unbound",
        "with_history": "get_governance_metrics(include_history=true)"
    },
    "identity": {
        "check_identity": "identity(client_session_id=\"<your client_session_id>\")  # Reports on YOUR binding; with no proof argument the call is gated to a fresh mint",
        "name_yourself": f"{SET_DISPLAY_NAME_CALL}  # Set your display name; without client_session_id the call can resolve a co-located agent"
    },
    "agent": {
        "all_agents": "agent(action=\"list\")  # List all agents with metadata",
        "active_only": "agent(action=\"list\", status_filter=\"active\")  # Only active agents",
        "with_metrics": "agent(action=\"list\", include_metrics=true)  # Include governance metrics",
        "lite_view": "agent(action=\"list\", summary_only=true)  # Minimal summary view"
    },
    "observe": {
        "basic_observation": "observe(action=\"agent\", target_agent_id=\"my_agent\")  # Analyze agent patterns",
        "with_history": "observe(action=\"agent\", target_agent_id=\"my_agent\", include_history=true)  # Include historical patterns",
        "pattern_analysis": "observe(action=\"agent\", target_agent_id=\"my_agent\", analyze_patterns=true)  # Deep pattern analysis"
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
            "why": "Mint a fresh process identity. Only if a finished predecessor handed you its work, include parent_agent_id and spawn_reason='explicit'.",
        },
        {
            "step": 2,
            "tool": "sync_state",
            "call": "sync_state(response_text='what changed', complexity=0.5)",
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
        "small_surface": "Use list_tools(lite=true) for the name-only index, or add essential_only=true to narrow it to the core workflow.",
        "preferred_consolidated_tools": {
            "knowledge": "Use action='search'|'note'|'store' instead of older KG-specific tools.",
            "consult": "Use for advisory answers, critique, summaries, and generation; it is never an on-record verdict.",
            "request_review": "Use when you need governed, on-record judgment or paused-state recovery with reviewer provenance.",
            "dialectic": "Use the raw action router only for explicit session lifecycle operations after request_review.",
            "calibration": "Use action='check' first; add ground truth with action='update' only when you have trusted external evidence.",
            "export": "Use action='history' for in-memory export; action='file' writes a server-side file.",
        },
    }


def common_patterns_for(tool_name: str) -> Dict[str, str]:
    """Get common usage patterns for a tool."""
    return COMMON_PATTERNS.get(tool_name, {})
