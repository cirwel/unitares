"""
Tool Stability and Migration System

Reduces friction from constant tool churn by:
1. Stability tiers (stable/experimental/beta)
2. Automatic aliases for renamed tools
3. Migration helpers
4. Single source of truth for tool lifecycle
"""

from typing import Any, Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime
from .support.param_normalization import (
    ParamNormalizer,
    normalize_compact_search_details,
    normalize_unit_interval,
)
from src.governance_glossary import EISV_INLINE_SUMMARY
# The stability enum and the per-tool tier live with the one record per tool
# in src/tool_meta.py (2026-09-07); both are re-exported here so
# `from src.mcp_handlers.tool_stability import ToolStability, _TOOL_STABILITY`
# keeps working.
from src.tool_meta import TOOL_STABILITY as _TOOL_STABILITY  # noqa: F401
from src.tool_meta import ToolStability

# Every advertised description is paid for on every tools/list. The full EISV
# field contract (#1434) is carried once on the advertised surface, by
# check_working_state, whose envelope returns E/I/S/V; the other workflow
# aliases point at it. The pointer is a call, not a cross-reference: a client
# that defers tool loading and selects only sync_state never loads
# check_working_state's description, but can call describe_tool. The
# canonical tools keep the full contract.
EISV_POINTER = "EISV field definitions: describe_tool(tool_name='check_working_state')."


def expand_description_pointers(text: Optional[str]) -> Optional[str]:
    """Return the explained form of an advertised description.

    The wire orients and describe_tool explains: an alias description that
    points at the EISV contract on tools/list gets the full contract back when
    one tool is described. It is appended as a second paragraph, so the first
    line still matches the wire.
    """
    if not text or EISV_POINTER not in text:
        return text
    return f"{text}\n\n{EISV_INLINE_SUMMARY}"

@dataclass
class ToolAlias:
    """Alias mapping for renamed/consolidated tools"""
    old_name: str
    new_name: str
    reason: str  # "renamed", "consolidated", "deprecated", "intuitive_alias"
    deprecated_since: Optional[datetime] = None
    migration_note: Optional[str] = None
    inject_action: Optional[str] = None  # For consolidated tools: auto-inject this action parameter
    inject_defaults: Optional[Dict[str, Any]] = None  # Friendly-surface defaults applied only when omitted
    # read / write / admin when the alias pins an action narrower than its
    # router's class (an alias that pins agent(action='list') is a read; the
    # agent router also archives and deletes). None: same class as the
    # canonical tool. No built-in alias sets it since the 2026-09-28 cut;
    # plugin aliases (register_extra_aliases) still may.
    operation: Optional[str] = None
    # Friendly aliases may absorb agent vocabulary or materialize advertised
    # workflow defaults before validation; canonical tools stay strict. Runs in
    # resolve_alias; caller-visible transforms are disclosed via
    # normalized_parameters.
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
# When tools are renamed/consolidated, add aliases here so old names still work.
# Primary agent workflow names also live here: they dispatch through raw
# implementation tools, but are the public first-run surface for agents.

_CHECKIN_COMPLEXITY_NORMALIZER = normalize_unit_interval("complexity")
_SEARCH_SHARED_MEMORY_NORMALIZER = normalize_compact_search_details

# deprecated_since: when a consolidated or deprecated old name stopped being
# canonical, read as the commit that added its alias (git log -S, 2026-09-07).
# Intuitive aliases (the workflow names) carry no date: those
# names were never canonical, so nothing was deprecated. The repository's
# history opens with a full-state import on 2026-01-13, so that date is a
# floor ("on or before"), not a day.
_SINCE_HISTORY_FLOOR = datetime(2026, 1, 13)           # c7e0c800, full-state import
_SINCE_FEB_2026_CONSOLIDATION = datetime(2026, 2, 4)   # fbe8014e, the action routers
_SINCE_TOOL_MODE_ENFORCEMENT = datetime(2026, 4, 1)    # e688c2b4, reassign_reviewer
_SINCE_ADMIN_ROUTER = datetime(2026, 6, 29)            # ebc30169, admin router (#1278)


# ONE NAME, ONE HOME (2026-08-29)
# ------------------------------------------------------------------
# A name must be EITHER a key in this table OR a register=True dispatch tool,
# never both. resolve_alias (middleware/params_step.py) rewrites the tool name
# before run_tool_dispatch_pipeline consults TOOL_HANDLERS, so a name that is
# both has a registration nothing can ever reach.
#
# Fifteen names were in that state. Fourteen were merely redundant -- the alias
# routed to a consolidated router that delegates back to the very handler the
# dead registration pointed at -- and their registrations are now register=False
# (admin group in admin/handlers.py, dialectic group in dialectic/handlers.py).
# The fifteenth, direct_resume_if_safe, was broken rather than redundant,
# because its alias target was itself register=False; it was removed outright
# on 2026-09-07 (see the recovery note below).
#
# Guarded by ALIAS_SHADOWS_REGISTERED_TOOL in scripts/dev/tool_edge_index.py and
# by test_no_alias_name_is_also_a_registered_tool.
_TOOL_ALIASES: Dict[str, ToolAlias] = {
    # LEGACY ALIASES REMOVED 2026-09-28 (operator direction: too many aliases).
    # This table used to hold 62 more names beside the eight workflow aliases
    # below: 24 guessed or pre-consolidation redirects (status, start, checkin,
    # hello, ...) in #2576, then 37 of the 38 pre-consolidation tool names that each
    # renamed a router call and injected its action (list_agents ->
    # agent(action='list'), submit_thesis -> dialectic(action='thesis'),
    # store_knowledge_graph -> knowledge(action='store'), ...). No capability
    # went with them: every router and action is unchanged, and the
    # register=False handlers keep their names as internal router delegates.
    # A caller of a removed name gets tool_not_found_error before any identity
    # step runs; docs/changelog.d lists the replacements. Removed tools that
    # were never aliased (who_am_i, direct_resume_if_safe) stay unaliased.

    # KEPT: get_server_info is the one legacy name still in this table. The
    # Wave 3a BEAM route (src/wave3a_routing.py, WAVE_3A_GET_SERVER_INFO_ON_BEAM)
    # is keyed on this name, so REST serves it from BEAM before Python dispatch.
    # Removing the alias alone would leave REST answering a name that /mcp
    # refuses. Re-keying that route is a BEAM change, decided separately.
    "get_server_info": ToolAlias(old_name="get_server_info", new_name="admin", reason="consolidated",
        deprecated_since=_SINCE_ADMIN_ROUTER,
        operation="read",
        migration_note="Use admin(action='server_info')", inject_action="server_info"),

    # ==========================================================================
    # Primary agent workflow names (Jun 2026) — task verbs for the core
    # agent workflow. Additive layer: raw implementation tools, schemas,
    # and EISV semantics are unchanged underneath. Identity classification is
    # inherited automatically: get_call_identity_requirement canonicalizes
    # through this registry (alias + inject_action) before judging.
    # `experience=True` opts the response into the normalized envelope
    # (middleware/envelope_step.py) — canonical names stay byte-identical.
    # ==========================================================================
    "start_session": ToolAlias(
        old_name="start_session", new_name="onboard", reason="intuitive_alias",
        migration_note=(
            "Register this process-instance and mint its agent identity; keep the "
            "returned client_session_id for later calls. Call with force_new=true "
            "— a bare call with no ownership proof is defaulted to force_new or "
            "refused under strict identity, never resumed onto another process's "
            "uuid. parent_agent_id claims succession from an EXITED predecessor: "
            "naming a still-live parent is rejected as coincidental and the claim "
            "cleared, unless spawn_reason marks a dispatched child or a "
            "compaction continuation. Use identity to inspect or rename an "
            "existing binding. onboard is the canonical twin; this name adds a "
            "digest envelope. Read the uuid from agent_uuid; response_mode='full' "
            "keeps the raw payload under raw_governance."
        ),
        experience=True),
    "sync_state": ToolAlias(
        old_name="sync_state", new_name="process_agent_update", reason="intuitive_alias",
        migration_note=(
            "Record a work check-in and get a governance decision: it advances "
            "and persists this agent's EISV state and returns proceed or pause "
            "with a named reason, plus a prediction_id — only when you pass "
            "confidence — to grade that check-in later with record_result. The "
            "first call auto-binds an identity, except under strict identity, "
            "which refuses and points at start_session. simulate_update previews "
            "a proposed check-in without advancing state, though it still "
            "appends an audit event; check_working_state reads the current "
            "verdict without writing. process_agent_update is the canonical twin; "
            "this name returns a digest envelope, and a routine check-in omits "
            "the raw payload (response_mode='full' adds it under raw_governance). "
            f"{EISV_POINTER}"
        ),
        param_normalizer=_CHECKIN_COMPLEXITY_NORMALIZER,
        experience=True),
    "check_working_state": ToolAlias(
        old_name="check_working_state", new_name="get_governance_metrics", reason="intuitive_alias",
        migration_note=(
            "Read your current governance state and verdict without running "
            "a cycle, writing, or minting an identity. Only proof sent with "
            "the call reads your state (start_session's client_session_id, "
            "an X-Session-ID header or a verified continuity_token), never an "
            "inferred binding; otherwise a self-read is unbound and "
            "next_action says how to recover. agent_id, dropped on /mcp/, "
            "names the agent to read through use_tool or REST unless you are "
            "bound as a different agent (identity_mismatch); that read is "
            "marked identity_assurance.caller_proven=false on an inferred "
            "session. verbosity='standard' adds mode and basin with their "
            "meanings under raw_governance; verbosity='full' (alias "
            "lite=false) returns the full canonical diagnostics. sync_state "
            "also logs work and returns proceed or pause. "
            "get_governance_metrics returns this read's raw payload. "
            f"{EISV_INLINE_SUMMARY}"
        ),
        experience=True),
    "search_shared_memory": ToolAlias(
        old_name="search_shared_memory", new_name="knowledge", reason="intuitive_alias",
        migration_note=(
            "Search the cross-agent knowledge graph for prior findings. Rows in "
            "status archived or cold are excluded unless you set status "
            "explicitly or pass include_archived / include_cold; a resolved or "
            "closed finding is still returned. Reading is not free of effect: "
            "every successful search appends a knowledge_read audit row naming "
            "the reader and a redacted copy of the query, which is why this tool "
            "is not annotated read-only. It serves unbound callers, so it works "
            "before start_session, "
            "unlike the writes: use store_finding to add a finding and "
            "update_finding to revise one."
        ),
        inject_action="search",
        param_normalizer=_SEARCH_SHARED_MEMORY_NORMALIZER,
        experience=True),
    # The write half of shared memory. `search_shared_memory` named the read and
    # left store/update reachable only through the `knowledge` router — and a
    # caller that matches on the domain noun never gets to the router, because
    # the read alias absorbs the intent first. Measured 2026-08-11 with the
    # fleet's local gemma4: 0/3 on both a store task and an update task against
    # the deployed surface (it picked `record_result` and `search_shared_memory`
    # respectively), 3/3 once it could reach `knowledge` directly. These give the
    # two write actions their own names so reaching them does not depend on
    # out-reasoning the read alias. `knowledge` stays registered and unchanged;
    # this adds names, it does not move capability.
    "store_finding": ToolAlias(
        old_name="store_finding", new_name="knowledge", reason="intuitive_alias",
        migration_note=(
            "Write one new durable finding into the cross-agent knowledge graph "
            "and get back its discovery_id. summary is required at call time even "
            "though the schema marks every field optional; severity high or "
            "critical is refused unless the session is bound to a registered "
            "agent, while low and medium fall back to an anonymous writer id. "
            "Every call mints a NEW discovery — search_shared_memory first, and "
            "use update_finding to revise one that already exists. Use it for a "
            "discovery, root cause or correction, and record_result for task, "
            "tool or test outcomes; budget 20 findings an hour."
        ),
        inject_action="store", experience=True),
    "update_finding": ToolAlias(
        old_name="update_finding", new_name="knowledge", reason="intuitive_alias",
        migration_note=(
            "Revise a discovery already in the knowledge graph. discovery_id is "
            "required at call time even though the schema marks it optional; get "
            "one from search_shared_memory. summary and details replace what was "
            "there, while resolution_notes appends a timestamped block, so a "
            "repeated call appends again. Editing at high or critical severity, "
            "or raising a discovery into that band, needs a registered, "
            "session-bound identity, and a non-owner may then only set status to "
            "resolved, closed or wont_fix. Set status when the finding is "
            "resolved or superseded."
        ),
        inject_action="update", experience=True),
    "record_result": ToolAlias(
        old_name="record_result", new_name="outcome_event", reason="intuitive_alias",
        migration_note=(
            "Record a measurable outcome and pair it with this agent's EISV "
            "snapshot so verdicts can be graded against what really happened. "
            "Pass the prediction_id from a sync_state reply to bind the outcome "
            "to that check-in's confidence; it is consumed on first use and "
            "TTL-bound (an hour by default). Needs a bound or explicit agent_id, "
            "and refuses under strict identity from an ephemeral session. "
            "Provenance cannot be self-attested here: verification_source is "
            "forced and provenance keys in detail are stripped. Use store_finding "
            "for durable knowledge. outcome_event is the canonical twin; this "
            "name adds a digest envelope and keeps the raw payload under "
            "raw_governance only with response_mode='full' or "
            "include_semantics=true, or when the write returned no outcome_id. "
            f"{EISV_POINTER}"
        ),
        experience=True),
    "request_review": ToolAlias(
        old_name="request_review", new_name="dialectic", reason="intuitive_alias",
        migration_note=(
            "Open a governed, on-record review session for this agent. "
            "issue_description is reused as the thesis by default, so one call "
            "can reach a verdict; pass use_brief_as_thesis=false for the two-call "
            "form. Requires a session-owned registered identity, refuses with "
            "SESSION_EXISTS while one is active, and returns skipped with no "
            "session when the agent is waiting_input. dialectic advances an open "
            "session; consult gives advisory evidence with no verdict."
        ),
        inject_action="request", inject_defaults={"use_brief_as_thesis": True},
        experience=True),
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
    "store_finding",
    "update_finding",
    "record_result",
    "request_review",
)

# Default stability for a tool with no record (a plugin tool, or a name the
# registry has not settled yet).
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
    """Stability tier for a tool; an alias reports its canonical tool's tier."""
    canonical, _ = resolve_tool_alias(tool_name)
    return _TOOL_STABILITY.get(canonical, _DEFAULT_STABILITY)


def is_experience_alias(tool_name: str) -> bool:
    """True when the INVOKED name is an agent-experience alias whose
    response should receive the normalized envelope."""
    alias = _TOOL_ALIASES.get(tool_name)
    return bool(alias and alias.experience)


def experience_alias_map() -> Dict[str, str]:
    """Primary agent workflow name -> raw implementation tool name.

    Single source for the discoverability surfaces (list_tools catalog)
    so the advertised primary names can never drift from the registry.
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
