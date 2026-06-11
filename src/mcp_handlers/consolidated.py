"""
Consolidated MCP Tool Handlers

Reduces cognitive load for AI agents by consolidating related tools:
- knowledge: 11 actions → 1 tool (store, search, get, list, update, details, note, cleanup, stats, supersede, audit)
- agent: 6 actions → 1 tool (list, get, update, archive, resume, delete)
- calibration: 4 actions → 1 tool (check, update, backfill, rebuild)
- config: 2 actions → 1 tool (get, set)
- export: 2 actions → 1 tool (history, file)
- observe: 5 actions → 1 tool (agent, compare, similar, anomalies, aggregate)
- pi: 12 actions → 1 tool (tools, context, health, sync_eisv, display, say, message, qa, query, workflow, git_pull, power)
- dialectic: 2 actions → 1 tool (get, list)

Each consolidated tool uses an 'action' parameter to select the operation.
Original tools remain available for backwards compatibility.
"""

from .decorators import action_router

# Import original handlers to delegate to
from .knowledge.handlers import (
    handle_store_knowledge_graph,
    handle_search_knowledge_graph,
    handle_get_knowledge_graph,
    handle_list_knowledge_graph,
    handle_update_discovery_status_graph,
    handle_get_discovery_details,
    handle_leave_note,
    handle_cleanup_knowledge_graph,
    handle_synthesize_knowledge_graph,
    handle_get_lifecycle_stats,
    handle_supersede_discovery,
    handle_audit_knowledge_graph,
)
from .lifecycle.handlers import (
    handle_list_agents,
    handle_get_agent_metadata,
    handle_update_agent_metadata,
    handle_archive_agent,
    handle_resume_agent,
    handle_delete_agent,
)
from .admin.calibration import (
    handle_check_calibration,
    handle_update_calibration_ground_truth,
    handle_backfill_calibration_from_dialectic,
    handle_rebuild_calibration,
)
from .admin.handlers import (
    handle_get_telemetry_metrics,
)
from .admin.config import (
    handle_get_thresholds,
    handle_set_thresholds,
)
from .introspection.export import (
    handle_get_system_history,
    handle_export_to_file,
)
from .observability.handlers import (
    handle_observe_agent,
    handle_compare_agents,
    handle_compare_me_to_similar,
    handle_detect_anomalies,
    handle_aggregate_metrics,
    handle_audit_events,
)
from .dialectic.handlers import (
    handle_get_dialectic_session,
    handle_list_dialectic_sessions,
    handle_quick_dialectic,
    handle_request_dialectic_review,
    handle_submit_thesis,
    handle_submit_antithesis,
    handle_submit_synthesis,
    handle_reassign_reviewer,
)
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
# Pi orchestration (``pi`` action router + handlers) moved to the
# ``unitares-pi-plugin`` package — registered via the
# ``governance_mcp.plugins`` entry point at server startup.

# ============================================================
# Consolidated Knowledge Graph Tool
# ============================================================

handle_knowledge = action_router(
    "knowledge",
    actions={
        "store": handle_store_knowledge_graph,
        "search": handle_search_knowledge_graph,
        "get": handle_get_knowledge_graph,
        "list": handle_list_knowledge_graph,
        "update": handle_update_discovery_status_graph,
        "details": handle_get_discovery_details,
        "note": handle_leave_note,
        "cleanup": handle_cleanup_knowledge_graph,
        "synthesize": handle_synthesize_knowledge_graph,
        "stats": handle_get_lifecycle_stats,
        "supersede": handle_supersede_discovery,
        "audit": handle_audit_knowledge_graph,
    },
    timeout=120.0,
    description="Unified knowledge graph operations: store, search, get, list, update, details, note, cleanup, synthesize, stats, supersede, audit",
    # #425 action-level identity: browsable READS may serve unbound
    # (fleet-scoped KG queries — the dashboard's search/stats calls);
    # every write/admin action stays identity-gated.
    pre_onboard_actions={"search", "get", "list", "details", "stats"},
    param_maps={
        "search": {"query": "search_query"},
        "store": {"content": "details"},  # Allow 'content' as alias for 'details'
        "update": {"content": "details"},  # Allow 'content' as alias for 'details'
        "note": {"content": "summary"},
    },
    examples=[
        "knowledge(action='store', summary='Found bug in auth', discovery_type='bug_found')",
        "knowledge(action='search', query='authentication issues')",
        "knowledge(action='note', content='Remember to check cache')",
        "knowledge(action='synthesize')  # roll up the densest topics into summaries",
        "knowledge(action='synthesize', topic='identity', dry_run=true)",
    ],
)

# ============================================================
# Consolidated Agent Lifecycle Tool
# ============================================================

handle_agent = action_router(
    "agent",
    actions={
        "list": handle_list_agents,
        "get": handle_get_agent_metadata,
        "update": handle_update_agent_metadata,
        "archive": handle_archive_agent,
        "resume": handle_resume_agent,
        "delete": handle_delete_agent,
    },
    timeout=20.0,
    description="Unified agent lifecycle operations: list, get, update, archive, resume, delete",
    # #425 action-level identity: fleet reads unbound; lifecycle writes
    # (update/archive/resume/delete) stay identity-gated — the dashboard's
    # operator buttons will need an operator credential under strict.
    pre_onboard_actions={"list", "get"},
    examples=[
        "agent(action='list')",
        "agent(action='get', agent_id='claude-opus-20251215')",
        "agent(action='update', tags=['explorer', 'governance'])",
        "agent(action='archive', agent_id='old-agent-id')",
        "agent(action='resume', agent_id='stuck-agent-id')",
    ],
)

# ============================================================
# Consolidated Calibration Tool
# ============================================================

handle_calibration = action_router(
    "calibration",
    actions={
        "check": handle_check_calibration,
        "update": handle_update_calibration_ground_truth,
        "backfill": handle_backfill_calibration_from_dialectic,
        "rebuild": handle_rebuild_calibration,
    },
    timeout=60.0,
    description="Unified calibration operations: check, update, backfill, rebuild",
    default_action="check",
    # #425 action-level identity: 'check' is a fleet-scoped read (the
    # dashboard's check_calibration); update/backfill/rebuild mutate
    # calibration state.
    pre_onboard_actions={"check"},
    examples=[
        "calibration(action='check')",
        "calibration(action='update', ground_truth=True)",
    ],
)

# ============================================================
# Consolidated Config Tool
# ============================================================

handle_config = action_router(
    "config",
    actions={
        "get": handle_get_thresholds,
        "set": handle_set_thresholds,
    },
    timeout=15.0,
    description="Unified configuration operations: get, set thresholds",
    default_action="get",
    # #425 action-level identity: threshold reads unbound; 'set' is an
    # operator write.
    pre_onboard_actions={"get"},
    examples=[
        "config(action='get')",
        "config(action='set', thresholds={'PAUSE_RISK_THRESHOLD': 0.75})",
    ],
)

# ============================================================
# Consolidated Export Tool
# ============================================================

handle_export = action_router(
    "export",
    actions={
        "history": handle_get_system_history,
        "file": handle_export_to_file,
    },
    timeout=45.0,
    description="Unified export operations: history, file",
    default_action="history",
    examples=[
        "export(action='history', format='json')",
        "export(action='file', format='json', filename='my_export')",
    ],
)

# ============================================================
# Consolidated Observe Tool
# ============================================================

handle_observe = action_router(
    "observe",
    actions={
        "agent": handle_observe_agent,
        "compare": handle_compare_agents,
        "similar": handle_compare_me_to_similar,
        "anomalies": handle_detect_anomalies,
        "aggregate": handle_aggregate_metrics,
        "telemetry": handle_get_telemetry_metrics,
        "audit_events": handle_audit_events,
    },
    timeout=15.0,
    description="Unified observability operations: agent, compare, similar, anomalies, aggregate, telemetry, audit_events",
    # #425 action-level identity: the analysis reads (incl. the
    # dashboard's anomalies/compare) serve unbound; telemetry and
    # audit_events are operator surfaces and stay identity-gated.
    pre_onboard_actions={"agent", "compare", "similar", "anomalies", "aggregate"},
    examples=[
        "observe(action='agent', agent_id='claude-opus-20251215')",
        "observe(action='compare', agent_ids=['agent1', 'agent2'])",
        "observe(action='similar')",
        "observe(action='anomalies')",
        "observe(action='aggregate')",
        "observe(action='telemetry')",
        "observe(action='audit_events', event_type='continuity_token_deprecated_accept', since='14d')",
    ],
)

# ``pi`` consolidated tool moved to unitares-pi-plugin (see register() there).

# ============================================================
# Consolidated Dialectic Tool
# ============================================================

handle_dialectic = action_router(
    "dialectic",
    actions={
        "get": handle_get_dialectic_session,
        "list": handle_list_dialectic_sessions,
        "quick": handle_quick_dialectic,
        "request": handle_request_dialectic_review,
        "thesis": handle_submit_thesis,
        "antithesis": handle_submit_antithesis,
        "synthesis": handle_submit_synthesis,
        "reassign": handle_reassign_reviewer,
    },
    timeout=60.0,
    description="Dialectic operations: get, list, request, thesis, antithesis, synthesis, reassign",
    default_action="list",
    # #425 action-level identity: session browsing (get/list) serves
    # unbound; every session-mutating action stays identity-gated.
    pre_onboard_actions={"get", "list"},
    examples=[
        "dialectic(action='list')",
        "dialectic(action='get', session_id='abc123')",
        "dialectic(action='quick', issue_description='Should I proceed?', position='Proceed after tests pass')",
        "dialectic(action='request', issue_description='Agent stuck in loop')",
        "dialectic(action='thesis', session_id='abc123', root_cause='...', proposed_conditions=[...])",
        "dialectic(action='vote', session_id='abc123', vote='resume', reasoning='...')",
    ],
)
