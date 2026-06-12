"""
Tool Stability and Migration System

Reduces friction from constant tool churn by:
1. Stability tiers (stable/experimental/beta)
2. Automatic aliases for renamed tools
3. Migration helpers
4. Single source of truth for tool lifecycle
"""

from typing import Dict, List, Optional, Set
from dataclasses import dataclass
from enum import Enum
from datetime import datetime
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
from .support.tool_hints import KNOWLEDGE_SEARCH_SIMILARITY_MIGRATION_NOTE
from .support.param_normalization import ParamNormalizer, normalize_unit_interval
class ToolStability(Enum):
    """Tool stability tier - helps users know what to expect"""
    STABLE = "stable"  # Production-ready, won't change
    BETA = "beta"  # Mostly stable, minor changes possible
    EXPERIMENTAL = "experimental"  # WIP, may change/break

@dataclass
class ToolAlias:
    """Alias mapping for renamed/consolidated tools"""
    old_name: str
    new_name: str
    reason: str  # "renamed", "consolidated", "deprecated", "intuitive_alias"
    deprecated_since: Optional[datetime] = None
    migration_note: Optional[str] = None
    inject_action: Optional[str] = None  # For consolidated tools: auto-inject this action parameter
    # Friendly aliases may absorb agent vocabulary (named levels, explicit
    # scale objects) before validation; canonical tools stay strict. Runs in
    # resolve_alias; transforms are disclosed via normalized_parameters.
    param_normalizer: Optional[ParamNormalizer] = None
    experience: bool = False  # Agent-experience alias: response gets the normalized envelope

@dataclass
class ToolLifecycle:
    """Complete tool lifecycle information"""
    name: str
    stability: ToolStability
    created_at: datetime
    deprecated_at: Optional[datetime] = None
    superseded_by: Optional[str] = None
    aliases: List[str] = None  # Old names that map to this tool
    migration_guide: Optional[str] = None
    
    def __post_init__(self):
        if self.aliases is None:
            self.aliases = []

# ============================================================================
# Tool Aliases Registry
# ============================================================================
# When tools are renamed/consolidated, add aliases here so old names still work
# This prevents breaking existing code/agents

_CHECKIN_COMPLEXITY_NORMALIZER = normalize_unit_interval("complexity")

_TOOL_ALIASES: Dict[str, ToolAlias] = {
    # Agent workflow aliases — first-class UX names for the core loop.
    # These are intentionally advertised/registered, unlike most historical
    # compatibility aliases, so cold MCP agents can discover task verbs.
    "start_session": ToolAlias(
        old_name="start_session",
        new_name="onboard",
        reason="intuitive_alias",
        migration_note=(
            "Workflow alias for onboard(force_new=true, parent_agent_id=...). "
            "Starts a fresh process identity or declares lineage."
        ),
    ),
    "sync_state": ToolAlias(
        old_name="sync_state",
        new_name="process_agent_update",
        reason="intuitive_alias",
        migration_note=(
            "Workflow alias for process_agent_update(response_text='...', "
            "complexity=..., confidence=...)."
        ),
    ),
    "check_working_state": ToolAlias(
        old_name="check_working_state",
        new_name="get_governance_metrics",
        reason="intuitive_alias",
        migration_note=(
            "Workflow alias for get_governance_metrics(). Reads current EISV "
            "state without writing a check-in."
        ),
    ),
    "search_shared_memory": ToolAlias(
        old_name="search_shared_memory",
        new_name="knowledge",
        reason="intuitive_alias",
        migration_note=(
            "Workflow alias for knowledge(action='search', query='...'). "
            "Search shared memory before writing a duplicate discovery."
        ),
        inject_action="search",
    ),
    "record_result": ToolAlias(
        old_name="record_result",
        new_name="outcome_event",
        reason="intuitive_alias",
        migration_note=(
            "Workflow alias for outcome_event(...). Records the real outcome "
            "of a task, test, tool call, or external validation signal."
        ),
    ),
    "request_review": ToolAlias(
        old_name="request_review",
        new_name="dialectic",
        reason="intuitive_alias",
        migration_note=(
            "Workflow alias for dialectic(action='request', "
            "issue_description='...'). Starts structured review/recovery."
        ),
        inject_action="request",
    ),

    # Identity tools - all point to identity() (the primary identity tool)
    # NOTE: who_am_i has its own handler in admin.py, so NOT aliased
    #
    # Common intuitive aliases for agent "status" checking
    "status": ToolAlias(
        old_name="status",
        new_name="get_governance_metrics",
        reason="intuitive_alias",
        migration_note="Use get_governance_metrics() for EISV status. Use identity() for who you are."
    ),
    "my_status": ToolAlias(
        old_name="my_status",
        new_name="get_governance_metrics",
        reason="intuitive_alias",
        migration_note="Use get_governance_metrics() for EISV status"
    ),
    "check_status": ToolAlias(
        old_name="check_status",
        new_name="get_governance_metrics",
        reason="intuitive_alias",
        migration_note="Use get_governance_metrics() for EISV status"
    ),
    "metrics": ToolAlias(
        old_name="metrics",
        new_name="get_governance_metrics",
        reason="intuitive_alias",
        migration_note="Use get_governance_metrics() for EISV status"
    ),
    "state": ToolAlias(
        old_name="state",
        new_name="get_governance_metrics",
        reason="intuitive_alias",
        migration_note="Use get_governance_metrics() for EISV state"
    ),
    # Onboarding aliases - common first-call guesses
    "start": ToolAlias(
        old_name="start",
        new_name="onboard",
        reason="intuitive_alias",
        migration_note="Use onboard() to start - creates identity and returns templates"
    ),
    "init": ToolAlias(
        old_name="init",
        new_name="onboard",
        reason="intuitive_alias",
        migration_note="Use onboard() to initialize - creates identity and returns templates"
    ),
    "register": ToolAlias(
        old_name="register",
        new_name="onboard",
        reason="intuitive_alias",
        migration_note="Use onboard() to register - creates identity and returns templates"
    ),
    "login": ToolAlias(
        old_name="login",
        new_name="onboard",
        reason="intuitive_alias",
        migration_note="Use onboard() - auto-creates identity, no login needed"
    ),
    # Logging work aliases. All carry the complexity normalizer: the friendly
    # surface accepts named levels and {'value', 'scale'} objects while the
    # canonical tool stays strict 0-1.
    "checkin": ToolAlias(
        old_name="checkin",
        new_name="process_agent_update",
        reason="intuitive_alias",
        migration_note="Use process_agent_update() to check in your work",
        param_normalizer=_CHECKIN_COMPLEXITY_NORMALIZER,
    ),
    "log": ToolAlias(
        old_name="log",
        new_name="process_agent_update",
        reason="intuitive_alias",
        migration_note="Use process_agent_update() to log your work",
        param_normalizer=_CHECKIN_COMPLEXITY_NORMALIZER,
    ),
    "update": ToolAlias(
        old_name="update",
        new_name="process_agent_update",
        reason="intuitive_alias",
        migration_note="Use process_agent_update() to log your work",
        param_normalizer=_CHECKIN_COMPLEXITY_NORMALIZER,
    ),
    "authenticate": ToolAlias(
        old_name="authenticate",
        new_name="identity",
        reason="consolidated",
        migration_note="Use identity() - auto-creates on first call"
    ),
    "session": ToolAlias(
        old_name="session",
        new_name="identity",
        reason="consolidated",
        migration_note="Use identity() - auto-creates on first call"
    ),
    "quick_start": ToolAlias(
        old_name="quick_start",
        new_name="identity",
        reason="consolidated",
        migration_note="Use identity() - auto-creates on first call"
    ),
    "recall_identity": ToolAlias(
        old_name="recall_identity",
        new_name="identity",
        reason="consolidated",
        migration_note="Use identity() - shows bound identity"
    ),
    "bind_identity": ToolAlias(
        old_name="bind_identity",
        new_name="identity",
        reason="consolidated",
        migration_note="Use identity() - auto-creates on first call"
    ),
    "hello": ToolAlias(
        old_name="hello",
        new_name="identity",
        reason="consolidated",
        migration_note="Use identity() - auto-creates on first call"
    ),
    "get_agent_api_key": ToolAlias(
        old_name="get_agent_api_key",
        new_name="identity",
        reason="deprecated",
        migration_note="API keys deprecated - UUID is now auth. Use identity() to see your agent_uuid."
    ),
    # NOTE: who_am_i is NOT aliased - it has its own handler in admin.py

    # Recovery tools - consolidated recovery hierarchy (Jan 2026)
    # direct_resume_if_safe is deprecated in favor of clearer recovery paths
    "direct_resume_if_safe": ToolAlias(
        old_name="direct_resume_if_safe",
        new_name="quick_resume",  # Default to quick_resume, but suggest self_recovery_review if thresholds not met
        reason="deprecated",
        deprecated_since=datetime(2026, 1, 29),
        migration_note="Use quick_resume() if coherence > 0.60 and risk < 0.40, otherwise use self_recovery_review(reflection='...')"
    ),
    
    # Dialectic tools - legacy creation remains archived (except request_dialectic_review restored)
    "request_exploration_session": ToolAlias(
        old_name="request_exploration_session",
        new_name="dialectic",
        reason="consolidated",
        migration_note="Use dialectic(action='get') to view sessions",
        inject_action="get"
    ),
    # Dialectic write tools → dialectic(action='...')  (Apr 2026 consolidation)
    "request_dialectic_review": ToolAlias(
        old_name="request_dialectic_review", new_name="dialectic", reason="consolidated",
        migration_note="Use dialectic(action='request', issue_description='...')", inject_action="request"),
    "submit_thesis": ToolAlias(
        old_name="submit_thesis", new_name="dialectic", reason="consolidated",
        migration_note="Use dialectic(action='thesis', session_id='...', root_cause='...')", inject_action="thesis"),
    "submit_antithesis": ToolAlias(
        old_name="submit_antithesis", new_name="dialectic", reason="consolidated",
        migration_note="Use dialectic(action='antithesis', session_id='...')", inject_action="antithesis"),
    "submit_synthesis": ToolAlias(
        old_name="submit_synthesis", new_name="dialectic", reason="consolidated",
        migration_note="Use dialectic(action='synthesis', session_id='...')", inject_action="synthesis"),
    "reassign_reviewer": ToolAlias(
        old_name="reassign_reviewer", new_name="dialectic", reason="consolidated",
        migration_note="Use dialectic(action='reassign', session_id='...')", inject_action="reassign"),
    
    # Knowledge graph tools
    "find_similar_discoveries_graph": ToolAlias(
        old_name="find_similar_discoveries_graph",
        new_name="search_knowledge_graph",
        reason="consolidated",
        migration_note=KNOWLEDGE_SEARCH_SIMILARITY_MIGRATION_NOTE
    ),
    "get_related_discoveries_graph": ToolAlias(
        old_name="get_related_discoveries_graph",
        new_name="knowledge",
        reason="consolidated",
        migration_note="Use knowledge(action='details') - includes related discoveries"
    ),
    "get_response_chain_graph": ToolAlias(
        old_name="get_response_chain_graph",
        new_name="knowledge",
        reason="consolidated",
        migration_note="Use knowledge(action='details') - includes response chain"
    ),
    "reply_to_question": ToolAlias(
        old_name="reply_to_question",
        new_name="knowledge",
        reason="consolidated",
        migration_note="Use knowledge(action='store', response_to=question_id) to reply"
    ),

    # ==========================================================================
    # Feb 2026 Tool Consolidation - removed tools map to consolidated versions
    # ==========================================================================

    # Pi tool aliases moved to unitares-pi-plugin; registered via
    # register_extra_aliases() at plugin load.

    # Observe tools → observe(action='...')
    "observe_agent": ToolAlias(old_name="observe_agent", new_name="observe", reason="consolidated",
        migration_note="Use observe(action='agent', agent_id='...')", inject_action="agent"),
    "compare_agents": ToolAlias(old_name="compare_agents", new_name="observe", reason="consolidated",
        migration_note="Use observe(action='compare', agent_ids=[...])", inject_action="compare"),
    "compare_me_to_similar": ToolAlias(old_name="compare_me_to_similar", new_name="observe", reason="consolidated",
        migration_note="Use observe(action='similar')", inject_action="similar"),
    "detect_anomalies": ToolAlias(old_name="detect_anomalies", new_name="observe", reason="consolidated",
        migration_note="Use observe(action='anomalies')", inject_action="anomalies"),
    "aggregate_metrics": ToolAlias(old_name="aggregate_metrics", new_name="observe", reason="consolidated",
        migration_note="Use observe(action='aggregate')", inject_action="aggregate"),

    # Dialectic tools → dialectic(action='...')
    "get_dialectic_session": ToolAlias(old_name="get_dialectic_session", new_name="dialectic", reason="consolidated",
        migration_note="Use dialectic(action='get', session_id='...')", inject_action="get"),
    "list_dialectic_sessions": ToolAlias(old_name="list_dialectic_sessions", new_name="dialectic", reason="consolidated",
        migration_note="Use dialectic(action='list')", inject_action="list"),

    # Config tools - registered directly (not aliased to avoid action parameter issues)
    # Use config(action='get') or config(action='set') for consolidated access

    # Export tools → export(action='...')
    "get_system_history": ToolAlias(old_name="get_system_history", new_name="export", reason="consolidated",
        migration_note="Use export(action='history')", inject_action="history"),
    "export_to_file": ToolAlias(old_name="export_to_file", new_name="export", reason="consolidated",
        migration_note="Use export(action='file')", inject_action="file"),

    # Agent lifecycle tools → agent(action='...')
    "list_agents": ToolAlias(old_name="list_agents", new_name="agent", reason="consolidated",
        migration_note="Use agent(action='list')", inject_action="list"),
    "get_agent_metadata": ToolAlias(old_name="get_agent_metadata", new_name="agent", reason="consolidated",
        migration_note="Use agent(action='get', agent_id='...')", inject_action="get"),
    "update_agent_metadata": ToolAlias(old_name="update_agent_metadata", new_name="agent", reason="consolidated",
        migration_note="Use agent(action='update', ...)", inject_action="update"),
    "archive_agent": ToolAlias(old_name="archive_agent", new_name="agent", reason="consolidated",
        migration_note="Use agent(action='archive', agent_id='...')", inject_action="archive"),
    "delete_agent": ToolAlias(old_name="delete_agent", new_name="agent", reason="consolidated",
        migration_note="Use agent(action='delete', agent_id='...', confirm=true)", inject_action="delete"),

    # Calibration tools → calibration(action='...')
    "check_calibration": ToolAlias(old_name="check_calibration", new_name="calibration", reason="consolidated",
        migration_note="Use calibration(action='check')", inject_action="check"),
    "update_calibration_ground_truth": ToolAlias(old_name="update_calibration_ground_truth", new_name="calibration", reason="consolidated",
        migration_note="Use calibration(action='update', actual_correct=...)", inject_action="update"),
    "backfill_calibration_from_dialectic": ToolAlias(old_name="backfill_calibration_from_dialectic", new_name="calibration", reason="consolidated",
        migration_note="Use calibration(action='backfill')", inject_action="backfill"),
    "rebuild_calibration": ToolAlias(old_name="rebuild_calibration", new_name="calibration", reason="consolidated",
        migration_note="Use calibration(action='rebuild')", inject_action="rebuild"),

    # Knowledge graph tools → knowledge(action='...')
    "store_knowledge_graph": ToolAlias(old_name="store_knowledge_graph", new_name="knowledge", reason="consolidated",
        migration_note="Use knowledge(action='store', summary='...')", inject_action="store"),
    "get_knowledge_graph": ToolAlias(old_name="get_knowledge_graph", new_name="knowledge", reason="consolidated",
        migration_note="Use knowledge(action='get')", inject_action="get"),
    "list_knowledge_graph": ToolAlias(old_name="list_knowledge_graph", new_name="knowledge", reason="consolidated",
        migration_note="Use knowledge(action='list')", inject_action="list"),
    "update_discovery_status_graph": ToolAlias(old_name="update_discovery_status_graph", new_name="knowledge", reason="consolidated",
        migration_note="Use knowledge(action='update', discovery_id='...', status='...')", inject_action="update"),
    "get_discovery_details": ToolAlias(old_name="get_discovery_details", new_name="knowledge", reason="consolidated",
        migration_note="Use knowledge(action='details', discovery_id='...')", inject_action="details"),
    "cleanup_knowledge_graph": ToolAlias(old_name="cleanup_knowledge_graph", new_name="knowledge", reason="consolidated",
        migration_note="Use knowledge(action='cleanup')", inject_action="cleanup"),
    "get_lifecycle_stats": ToolAlias(old_name="get_lifecycle_stats", new_name="knowledge", reason="consolidated",
        migration_note="Use knowledge(action='stats')", inject_action="stats"),

    # ==========================================================================
    # Agent-experience aliases (Jun 2026) — task-verb names for the core
    # agent workflow. Additive layer: canonical tools, schemas, and EISV
    # semantics are unchanged underneath. Identity classification is
    # inherited automatically: get_call_identity_requirement canonicalizes
    # through this registry (alias + inject_action) before judging.
    # `experience=True` opts the response into the normalized envelope
    # (middleware/envelope_step.py) — canonical names stay byte-identical.
    # ==========================================================================
    "start_session": ToolAlias(
        old_name="start_session", new_name="onboard", reason="intuitive_alias",
        migration_note="Resolves to onboard() - creates identity and returns templates",
        experience=True),
    "sync_state": ToolAlias(
        old_name="sync_state", new_name="process_agent_update", reason="intuitive_alias",
        migration_note="Resolves to process_agent_update() - check in your working state",
        param_normalizer=_CHECKIN_COMPLEXITY_NORMALIZER,
        experience=True),
    "check_working_state": ToolAlias(
        old_name="check_working_state", new_name="get_governance_metrics", reason="intuitive_alias",
        migration_note="Resolves to get_governance_metrics() - your current EISV working state",
        experience=True),
    "search_shared_memory": ToolAlias(
        old_name="search_shared_memory", new_name="knowledge", reason="intuitive_alias",
        migration_note="Resolves to knowledge(action='search') - find prior discoveries, avoid duplicate work",
        inject_action="search", experience=True),
    "record_result": ToolAlias(
        old_name="record_result", new_name="outcome_event", reason="intuitive_alias",
        migration_note="Resolves to outcome_event() - record what actually happened",
        experience=True),
    "request_review": ToolAlias(
        old_name="request_review", new_name="dialectic", reason="intuitive_alias",
        migration_note="Resolves to dialectic(action='request', issue_description='...') - ask for a structured review",
        inject_action="request", experience=True),
}

# Reverse mapping: new_name -> list of old names
_ALIAS_REVERSE: Dict[str, List[str]] = {}
for alias in _TOOL_ALIASES.values():
    if alias.new_name not in _ALIAS_REVERSE:
        _ALIAS_REVERSE[alias.new_name] = []
    _ALIAS_REVERSE[alias.new_name].append(alias.old_name)


AGENT_WORKFLOW_ALIASES: tuple[str, ...] = (
    "start_session",
    "sync_state",
    "check_working_state",
    "search_shared_memory",
    "record_result",
    "request_review",
)

# ============================================================================
# Tool Stability Registry
# ============================================================================
# Mark tools by stability tier to help users know what to expect

_TOOL_STABILITY: Dict[str, ToolStability] = {
    # STABLE: Production-ready, won't change
    "identity": ToolStability.STABLE,  # Primary identity tool (renamed from status)
    "who_am_i": ToolStability.STABLE,  # Quick identity check
    "process_agent_update": ToolStability.STABLE,
    "get_governance_metrics": ToolStability.STABLE,
    "store_knowledge_graph": ToolStability.STABLE,
    "search_knowledge_graph": ToolStability.STABLE,
    "get_knowledge_graph": ToolStability.STABLE,
    "list_knowledge_graph": ToolStability.STABLE,
    "get_discovery_details": ToolStability.STABLE,
    "list_agents": ToolStability.STABLE,
    "health_check": ToolStability.STABLE,
    "list_tools": ToolStability.STABLE,
    "describe_tool": ToolStability.STABLE,
    "self_recovery_review": ToolStability.STABLE,  # Primary recovery path
    "quick_resume": ToolStability.STABLE,  # Fast recovery path
    "check_recovery_options": ToolStability.STABLE,  # Diagnostic tool

    # BETA: Mostly stable, minor changes possible
    "dialectic": ToolStability.BETA,  # Consolidated dialectic queries (get/list)
    "observe_agent": ToolStability.BETA,
    "compare_agents": ToolStability.BETA,
    "archive_agent": ToolStability.BETA,
    "update_discovery_status_graph": ToolStability.BETA,
    "leave_note": ToolStability.BETA,
    "operator_resume_agent": ToolStability.BETA,  # Operator tool
    
    "request_dialectic_review": ToolStability.BETA,  # Restored Feb 2026 - full protocol active

    # DEPRECATED: Will be removed in v2.0
    "direct_resume_if_safe": ToolStability.EXPERIMENTAL,  # Deprecated - use quick_resume or self_recovery_review

    # EXPERIMENTAL: WIP, may change/break
    "simulate_update": ToolStability.EXPERIMENTAL,
    "detect_anomalies": ToolStability.EXPERIMENTAL,
    "aggregate_metrics": ToolStability.EXPERIMENTAL,
}

# Default stability for unlisted tools
_DEFAULT_STABILITY = ToolStability.BETA

# ============================================================================
# Public API
# ============================================================================

def resolve_tool_alias(tool_name: str) -> tuple[str, Optional[ToolAlias]]:
    """
    Resolve tool alias to actual tool name.
    
    Returns:
        (actual_tool_name, alias_info) - alias_info is None if not an alias
    """
    if tool_name in _TOOL_ALIASES:
        alias = _TOOL_ALIASES[tool_name]
        return alias.new_name, alias
    return tool_name, None

def get_tool_stability(tool_name: str) -> ToolStability:
    """Get stability tier for a tool"""
    return _TOOL_STABILITY.get(tool_name, _DEFAULT_STABILITY)


def is_experience_alias(tool_name: str) -> bool:
    """True when the INVOKED name is an agent-experience alias whose
    response should receive the normalized envelope."""
    alias = _TOOL_ALIASES.get(tool_name)
    return bool(alias and alias.experience)


def experience_alias_map() -> Dict[str, str]:
    """Friendly experience-alias name -> canonical tool name.

    Single source for the discoverability surfaces (list_tools catalog)
    so the advertised friendly names can never drift from the registry.
    """
    return {
        name: alias.new_name
        for name, alias in _TOOL_ALIASES.items()
        if alias.experience
    }


def list_all_aliases() -> Dict[str, ToolAlias]:
    """Get all tool aliases (for admin/debugging)"""
    return _TOOL_ALIASES.copy()


def register_extra_aliases(aliases: Dict[str, ToolAlias]) -> None:
    """Merge plugin-supplied aliases into ``_TOOL_ALIASES``.

    Called by ``governance_mcp.plugins`` entry-point plugins during
    ``plugin_loader.load_plugins()``. Conflicting keys raise — aliases
    must be unique per tool name.
    """
    for old_name, alias in aliases.items():
        if old_name in _TOOL_ALIASES and _TOOL_ALIASES[old_name] is not alias:
            raise ValueError(
                f"alias conflict: '{old_name}' already registered to "
                f"'{_TOOL_ALIASES[old_name].new_name}'"
            )
        _TOOL_ALIASES[old_name] = alias
