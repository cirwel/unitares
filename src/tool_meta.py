"""One record per advertised tool: the source the bookkeeping maps derive from.

Until 2026-09-07 five hand-maintained maps described the same fifty names:
TOOL_TIERS, TOOL_OPERATIONS and TOOL_CATEGORIES in src/tool_modes.py,
_TOOL_STABILITY in src/mcp_handlers/tool_stability.py, and the category half
of TOOL_RELATIONSHIPS in src/mcp_handlers/introspection/tool_catalog.py.
Nothing regenerated them, they disagreed with the registry and with each
other (23 registered tools had no tier or category, 22 fell to the `read`
default, TOOL_RELATIONSHIPS carried the key "dialectic" twice and the second
entry silently won), and a new tool had to be added in five places.

This module holds one ToolMeta per name on the advertised roster: every
register=True dispatch tool plus the eight workflow aliases. The five maps
are derived below and re-exported under their old names, so every reader and
every test patch (`patch("src.tool_modes.TOOL_TIERS", ...)`) is unchanged.

The order of the registered tools in TOOL_META IS the wire order of
tools/list (src/tool_schemas.py TOOL_ORDER) and therefore the interface
contract's surface hash. A workflow alias sits beside its implementation
tool for reading and is not part of the wire order: the registrar and the
interface contract advertise aliases separately, aliases first, in
AGENT_WORKFLOW_ALIASES order.

Standard library only. src/tool_modes.py must stay importable with no
project dependency (scripts/diagnostics/count_tools.py degrades gracefully on
a runner that installs nothing), and anything under src/mcp_handlers/ drags
the whole handler tree in on import.

Adding a tool: one record here, its *Params model, its description in
src/tool_descriptions.py, and the @mcp_tool handler.
tests/test_tool_registry_bookkeeping.py holds the table to the registry:
every non-alias record is a registered tool, every registered tool has a
record, and the alias records are exactly AGENT_WORKFLOW_ALIASES.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Set, Tuple


class ToolStability(Enum):
    """Tool stability tier - helps users know what to expect"""
    STABLE = "stable"  # Production-ready, won't change
    BETA = "beta"  # Mostly stable, minor changes possible
    EXPERIMENTAL = "experimental"  # WIP, may change/break


STABLE = ToolStability.STABLE
BETA = ToolStability.BETA
EXPERIMENTAL = ToolStability.EXPERIMENTAL

TIERS: Tuple[str, ...] = ("essential", "common", "advanced")
OPERATIONS: Tuple[str, ...] = ("read", "write", "admin")
CATEGORIES: Tuple[str, ...] = (
    "core", "identity", "admin", "workspace", "export", "config",
    "lifecycle", "observability", "knowledge", "inference", "dialectic",
)


@dataclass(frozen=True)
class ToolMeta:
    """Everything the discovery surfaces say about one advertised name.

    category:   list_tools(category=...) browsing filter.
    tier:       essential (used in every session) / common / advanced (operator,
                maintenance, specialist); list_tools tier filters and describe_tool.
    operation:  read / write / admin. A router carries the most privileged class
                among its actions; a legacy alias that pins a narrower action
                declares its own on the ToolAlias entry.
    stability:  the declared promise. Nothing in the runtime branches on it;
                describe_tool and list_tools report it. A router carries a tier
                forward only where every flat predecessor agreed. None only for
                a workflow alias, which reports its implementation tool's.
    workflow_alias: one of AGENT_WORKFLOW_ALIASES; dispatches through the
                implementation tool it is listed beside.
    depends_on / related_to: the introspection catalog's relationship graph,
                served by list_tools(lite=false) under `relationships`. Every
                entry is a name on this roster or a call shape against a
                router on it (`agent(action='list')`). Never a dispatch-only
                twin (`list_agents`): REST and stdio resolve those through the
                alias table, but the /mcp/ mount registers only the roster, so
                a schema-driven client that follows such an entry gets
                `Unknown tool`. Twelve records named one until 2026-09-12 (F1
                of docs/operations/tool-surface-audit-2026-09-12.md).
                tests/test_tool_registry_bookkeeping.py holds this, and
                scripts/diagnostics/hint_target_advertisement.py scans this
                file for it.
    """

    name: str
    category: str
    tier: str
    operation: str
    stability: Optional[ToolStability] = None
    workflow_alias: bool = False
    depends_on: Tuple[str, ...] = ()
    related_to: Tuple[str, ...] = ()
    description: Optional[str] = None
    recovery_hierarchy: Optional[Dict[str, str]] = None

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"{self.name}: unknown category {self.category!r}")
        if self.tier not in TIERS:
            raise ValueError(f"{self.name}: unknown tier {self.tier!r}")
        if self.operation not in OPERATIONS:
            raise ValueError(f"{self.name}: unknown operation {self.operation!r}")
        if self.workflow_alias == (self.stability is not None):
            raise ValueError(
                f"{self.name}: a workflow alias carries no stability of its own "
                "and a registered tool must declare one"
            )


# Registered tools in wire order; each workflow alias beside its
# implementation tool. Section comments follow the historical order of the
# wire list, which the interface contract hash pins; regroup only with a
# deliberate contract change.
TOOL_META: Tuple[ToolMeta, ...] = (
    # -- Health and workspace
    # System status
    ToolMeta("health_check", category="admin", tier="essential", operation="read", stability=STABLE,
             related_to=("admin(action='server_info')", "admin(action='telemetry')")),
    # Keeps register=True: the operator modes advertise it and do not carry admin
    ToolMeta("get_workspace_health", category="workspace", tier="common", operation="read", stability=BETA,
             related_to=('health_check', "admin(action='server_info')")),
    # -- The check-in loop
    # The check-in; implemented by process_agent_update
    ToolMeta("sync_state", category="core", tier="essential", operation="write", workflow_alias=True,
             related_to=('process_agent_update', 'check_working_state')),
    ToolMeta("process_agent_update", category="core", tier="common", operation="write", stability=STABLE,
             related_to=('simulate_update', 'get_governance_metrics', "export(action='history')")),
    # Read the verdict without writing; implemented by get_governance_metrics
    ToolMeta("check_working_state", category="core", tier="essential", operation="read", workflow_alias=True,
             related_to=('get_governance_metrics', 'sync_state')),
    ToolMeta("get_governance_metrics", category="core", tier="common", operation="read", stability=STABLE,
             related_to=('process_agent_update', "observe(action='agent')", "export(action='history')")),
    # -- Lifecycle and maintenance
    # Updates agent status
    ToolMeta("mark_response_complete", category="lifecycle", tier="common", operation="write", stability=BETA,
             related_to=('process_agent_update', "agent(action='get')")),
    # write: auto_recover=true resumes agents
    ToolMeta("detect_stuck_agents", category="observability", tier="advanced", operation="write", stability=BETA,
             related_to=('observe', 'agent')),
    ToolMeta("archive_old_test_agents", category="lifecycle", tier="advanced", operation="write", stability=BETA,
             related_to=("agent(action='archive')", "agent(action='list')")),
    # Preview by default; archives on request
    ToolMeta("archive_orphan_agents", category="lifecycle", tier="advanced", operation="write", stability=BETA,
             related_to=('agent', "agent(action='list')")),
    # -- Simulation and thresholds
    # Dry-run, no state change
    ToolMeta("simulate_update", category="core", tier="advanced", operation="read", stability=EXPERIMENTAL,
             related_to=('process_agent_update', 'get_governance_metrics')),
    ToolMeta("get_thresholds", category="config", tier="common", operation="read", stability=BETA,
             related_to=('set_thresholds', 'process_agent_update')),
    ToolMeta("set_thresholds", category="config", tier="advanced", operation="write", stability=BETA,
             depends_on=('get_thresholds',),
             related_to=('get_thresholds', 'process_agent_update')),
    # -- Outcome evidence
    # Outcome evidence; implemented by outcome_event
    ToolMeta("record_result", category="core", tier="essential", operation="write", workflow_alias=True,
             depends_on=('sync_state',),
             related_to=('outcome_event', 'process_agent_update')),
    ToolMeta("outcome_event", category="core", tier="common", operation="write", stability=BETA,
             related_to=('record_result', 'process_agent_update')),
    # -- Discovery
    ToolMeta("list_tools", category="admin", tier="essential", operation="read", stability=STABLE,
             related_to=('describe_tool',)),
    ToolMeta("describe_tool", category="admin", tier="essential", operation="read", stability=STABLE,
             related_to=('list_tools',)),
    # Server-authored skill bundle
    ToolMeta("skills", category="admin", tier="common", operation="read", stability=BETA,
             related_to=('list_tools', 'describe_tool')),
    # -- Knowledge (flat)
    ToolMeta("search_knowledge_graph", category="knowledge", tier="common", operation="read", stability=STABLE,
             related_to=("knowledge(action='store')", "knowledge(action='details')")),
    # Not deprecated (operator decision, 2026-08-29): a first-class low-friction write
    ToolMeta("leave_note", category="knowledge", tier="essential", operation="write", stability=BETA,
             related_to=('knowledge', "knowledge(action='store')")),
    # -- Inference
    ToolMeta("list_inference_hosts", category="inference", tier="common", operation="read", stability=BETA,
             related_to=('describe_inference_host', 'consult', 'call_model', 'delegate_inference')),
    ToolMeta("describe_inference_host", category="inference", tier="common", operation="read", stability=BETA,
             depends_on=('list_inference_hosts',),
             related_to=('consult', 'call_model', 'delegate_inference')),
    # Canonical advisory inference facade
    ToolMeta("consult", category="inference", tier="essential", operation="read", stability=BETA,
             related_to=('call_model', 'delegate_inference', 'request_review')),
    # Advisory inference; returns an evidence artifact
    ToolMeta("call_model", category="inference", tier="common", operation="read", stability=BETA,
             related_to=('consult', 'list_inference_hosts', 'describe_inference_host', 'knowledge', 'dialectic')),
    # Strong advisory inference; returns an evidence artifact
    ToolMeta("delegate_inference", category="inference", tier="common", operation="read", stability=BETA,
             depends_on=('list_inference_hosts',),
             related_to=('consult', 'describe_inference_host', 'dialectic')),
    # -- Identity
    # Mint a process identity; implemented by onboard
    ToolMeta("start_session", category="identity", tier="essential", operation="read", workflow_alias=True,
             related_to=('onboard', 'identity')),
    # Returns identity + templates (creates if new)
    ToolMeta("onboard", category="identity", tier="common", operation="read", stability=BETA,
             related_to=('identity', 'process_agent_update')),
    # Primary identity tool (auto-creates on first call); renamed from status
    ToolMeta("identity", category="identity", tier="essential", operation="read", stability=STABLE,
             related_to=('onboard', 'process_agent_update', "agent(action='list')")),
    # Binds the transport session to an identity (session-start hook)
    ToolMeta("bind_session", category="identity", tier="common", operation="write", stability=BETA,
             related_to=('onboard', 'identity', 'start_session')),
    # -- Knowledge (router and workflow names)
    # Shared-memory read; implemented by knowledge(action='search')
    ToolMeta("search_shared_memory", category="knowledge", tier="essential", operation="read", workflow_alias=True,
             related_to=('knowledge', 'leave_note')),
    # Shared-memory write; implemented by knowledge(action='store')
    ToolMeta("store_finding", category="knowledge", tier="essential", operation="write", workflow_alias=True,
             related_to=('knowledge',)),
    # Revise a finding; implemented by knowledge(action='update')
    ToolMeta("update_finding", category="knowledge", tier="common", operation="write", workflow_alias=True,
             depends_on=('store_finding',),
             related_to=('knowledge',)),
    # Router: search / get / list read; store / update / note / cleanup write. STABLE: every flat predecessor was
    ToolMeta("knowledge", category="knowledge", tier="common", operation="write", stability=STABLE,
             related_to=('search_knowledge_graph', 'leave_note')),
    # -- Routers and operator tools
    # Router: list / get read; update / archive / resume / delete write. list_agents was STABLE, archive_agent BETA
    ToolMeta("agent", category="lifecycle", tier="common", operation="write", stability=BETA,
             related_to=('onboard', 'identity', 'observe')),
    # Router: check reads; update / backfill / rebuild write
    ToolMeta("calibration", category="core", tier="common", operation="write", stability=BETA,
             depends_on=('process_agent_update',),
             related_to=('process_agent_update', 'observe')),
    # Router: get reads; set writes (privileged)
    ToolMeta("config", category="config", tier="advanced", operation="write", stability=BETA,
             related_to=('get_thresholds', 'set_thresholds')),
    # Router: history reads; file writes to disk. Related to what its flat
    # predecessor get_system_history was related to, not to its own action.
    ToolMeta("export", category="export", tier="advanced", operation="write", stability=BETA,
             related_to=('get_governance_metrics', 'observe')),
    # Multi-agent coordination protocol state
    ToolMeta("cirs_protocol", category="core", tier="advanced", operation="write", stability=BETA,
             related_to=('dialectic', 'self_recovery')),
    # Router: check reads; quick / review resume. STABLE: review / quick / check all were
    ToolMeta("self_recovery", category="core", tier="common", operation="write", stability=STABLE,
             related_to=('request_review', 'check_working_state'),
             description='Self-recovery router: check (read-only eligibility), quick (clearly safe states, no reflection), review (moderate states, genuine reflection required)',
             recovery_hierarchy={'fastest': "self_recovery(action='quick')", 'primary': "self_recovery(action='review', reflection='...')", 'diagnostic': "self_recovery(action='check')"}),
    # Privileged operator override
    ToolMeta("operator_resume_agent", category="lifecycle", tier="advanced", operation="admin", stability=BETA,
             related_to=('agent', 'self_recovery')),
    # Router: every action reads. observe_agent / compare_agents BETA, anomalies / aggregate EXPERIMENTAL
    ToolMeta("observe", category="observability", tier="common", operation="read", stability=BETA,
             related_to=('agent', 'process_agent_update')),
    # Structured review; implemented by dialectic(action='request')
    ToolMeta("request_review", category="dialectic", tier="essential", operation="write", workflow_alias=True,
             depends_on=('sync_state',),
             related_to=('dialectic', 'self_recovery', 'consult')),
    # Router: get / list read; request / thesis / antithesis / synthesis / reassign write
    ToolMeta("dialectic", category="dialectic", tier="common", operation="write", stability=BETA,
             related_to=('request_review', 'process_agent_update')),
    # Active agents with their EISV vectors
    ToolMeta("dashboard", category="observability", tier="advanced", operation="read", stability=BETA,
             related_to=('observe', 'health_check')),
    # Router: mixed read / maintenance (Jun 2026)
    ToolMeta("admin", category="admin", tier="advanced", operation="admin", stability=BETA,
             related_to=('health_check', 'observe', 'config')),
    # -- Added 2026-08-29: registered tools that had drifted out of the wire list
    ToolMeta("get_trajectory_status", category="identity", tier="advanced", operation="read", stability=BETA,
             related_to=('verify_trajectory_identity', 'identity')),
    ToolMeta("verify_trajectory_identity", category="identity", tier="advanced", operation="read", stability=BETA,
             related_to=('get_trajectory_status', 'identity')),
    ToolMeta("list_process_bindings", category="identity", tier="advanced", operation="read", stability=BETA,
             related_to=('identity', 'agent')),
    ToolMeta("outcome_correlation", category="observability", tier="advanced", operation="read", stability=BETA,
             related_to=('observe', 'outcome_event')),
    # Resident progress pulse
    ToolMeta("record_progress_pulse", category="core", tier="common", operation="write", stability=BETA,
             related_to=('process_agent_update', 'sync_state')),
)


def _index() -> Dict[str, ToolMeta]:
    by_name: Dict[str, ToolMeta] = {}
    for meta in TOOL_META:
        if meta.name in by_name:
            raise ValueError(f"duplicate ToolMeta record: {meta.name}")
        by_name[meta.name] = meta
    return by_name


TOOL_META_BY_NAME: Dict[str, ToolMeta] = _index()

# The registered tools, in the order tools/list advertises them.
WIRE_ORDER: Tuple[str, ...] = tuple(m.name for m in TOOL_META if not m.workflow_alias)
WORKFLOW_ALIAS_NAMES: Tuple[str, ...] = tuple(m.name for m in TOOL_META if m.workflow_alias)

# The five derived maps. Same shapes as the literals they replace.
TOOL_TIERS: Dict[str, Set[str]] = {
    tier: {m.name for m in TOOL_META if m.tier == tier} for tier in TIERS
}
TOOL_OPERATIONS: Dict[str, str] = {m.name: m.operation for m in TOOL_META}
TOOL_CATEGORIES: Dict[str, Set[str]] = {
    category: {m.name for m in TOOL_META if m.category == category} for category in CATEGORIES
}
TOOL_STABILITY: Dict[str, ToolStability] = {
    m.name: m.stability for m in TOOL_META if m.stability is not None
}


def tool_relationships() -> Dict[str, Dict[str, Any]]:
    """The introspection catalog's relationship records for the roster."""
    out: Dict[str, Dict[str, Any]] = {}
    for m in TOOL_META:
        entry: Dict[str, Any] = {
            "depends_on": list(m.depends_on),
            "related_to": list(m.related_to),
            "category": m.category,
        }
        if m.recovery_hierarchy is not None:
            entry["recovery_hierarchy"] = dict(m.recovery_hierarchy)
        if m.description is not None:
            entry["description"] = m.description
        out[m.name] = entry
    return out
