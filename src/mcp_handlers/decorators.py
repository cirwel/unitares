"""
MCP Tool Decorators - Auto-registration and utilities

Reduces boilerplate and enables auto-discovery of tools.
"""

from dataclasses import dataclass
from typing import Dict, Any, Callable, Optional, Sequence
from functools import wraps
import asyncio
import time
from mcp.types import TextContent

from src.logging_utils import get_logger
from .utils import error_response

logger = get_logger(__name__)


# --- Unified Tool Registry ---

@dataclass
class ToolDefinition:
    """Single source of truth for a registered MCP tool."""
    name: str
    handler: Callable
    timeout: float = 30.0
    description: str = ""
    deprecated: bool = False
    hidden: bool = False
    superseded_by: Optional[str] = None
    # Identity-bootstrap declaration (#425). One of:
    #   "required"     — default; tool needs a bound identity.
    #   "pre_onboard"  — protocol-inspection or identity-lifecycle tool;
    #                    callable without a bound identity.
    #   "scoped"       — admin-token gated; separate auth path.
    requires_identity: str = "required"
    # Action-level identity exemptions for consolidated (action_router)
    # tools whose tool-level requirement is "required" but whose READ
    # actions may serve unbound (e.g. knowledge.search vs knowledge.store).
    # Consulted by get_call_identity_requirement — both #425 gates (MCP
    # middleware + REST) resolve at CALL granularity, not tool granularity,
    # because tool-level classification of a mixed read-write tool either
    # opens its writes (pre_onboard) or refuses its browsable reads
    # (required). None for single-purpose tools.
    pre_onboard_actions: Optional[frozenset] = None
    # Mirror of action_router's default_action so the call-level resolver
    # treats an action-less call exactly as the router will route it.
    default_action: Optional[str] = None
    # Stakes-gate override (#775). One of:
    #   "baseline" — default; observed by the substrate sink, not pre-gated.
    #   "high"     — every call to this tool is a high-consequence boundary.
    # Per-action classification for mixed-action (action_router) tools lives in
    # stakes_table.py, the standalone authoritative table; this field is the
    # tool-level escape hatch for a single-purpose tool that wants to declare
    # its stakes inline. The table still wins for any (tool, action) it lists.
    requires_verdict: str = "baseline"

_TOOL_DEFINITIONS: Dict[str, ToolDefinition] = {}


def mcp_tool(
    name: Optional[str] = None,
    timeout: float = 30.0,
    description: Optional[str] = None,
    deprecated: bool = False,
    hidden: bool = False,
    superseded_by: Optional[str] = None,
    register: bool = True,
    requires_identity: str = "required",
    pre_onboard_actions: Optional[set] = None,
    default_action: Optional[str] = None,
    requires_verdict: str = "baseline",
):
    """
    Decorator for MCP tool handlers with auto-registration and timeout protection.

    Provides:
    - Automatic timeout protection
    - Performance timing/observability (warns if >80% of timeout)
    - Error handling with recovery guidance
    - Tool registration for discovery
    - Deprecation and hiding support

    Usage:
        @mcp_tool("process_agent_update", timeout=60.0)
        async def handle_process_agent_update(arguments: Dict[str, Any]) -> Sequence[TextContent]:
            ...

        @mcp_tool("old_tool", deprecated=True, superseded_by="new_tool")
        async def handle_old_tool(...): ...

        @mcp_tool("internal_helper", register=False)  # Not exposed to MCP clients
        async def handle_internal_helper(...): ...

    Args:
        name: Tool name (defaults to function name without 'handle_' prefix)
        timeout: Timeout in seconds (default: 30.0)
        description: Tool description (defaults to function docstring)
        deprecated: If True, tool still works but warns users to use superseded_by
        hidden: If True, tool is not shown in list_tools (internal use only)
        superseded_by: Name of tool that replaces this one (for deprecation messages)
        register: If False, tool is NOT registered (for internal handlers called by consolidated tools)
    """
    def decorator(func: Callable) -> Callable:
        tool_name = name or func.__name__.replace('handle_', '')
        tool_description = description or (func.__doc__ and func.__doc__.strip().split('\n')[0].strip()) or ""

        if requires_identity not in ("required", "pre_onboard", "scoped"):
            raise ValueError(
                f"Tool {tool_name!r}: requires_identity must be one of "
                f"'required', 'pre_onboard', 'scoped'; got {requires_identity!r}"
            )
        if pre_onboard_actions is not None and requires_identity != "required":
            raise ValueError(
                f"Tool {tool_name!r}: pre_onboard_actions only applies to "
                f"requires_identity='required' tools (a 'pre_onboard' tool "
                f"already exempts every action); got {requires_identity!r}"
            )
        if requires_verdict not in ("baseline", "high"):
            raise ValueError(
                f"Tool {tool_name!r}: requires_verdict must be one of "
                f"'baseline', 'high'; got {requires_verdict!r}"
            )
        _pre_onboard_actions = (
            frozenset(a.lower() for a in pre_onboard_actions)
            if pre_onboard_actions
            else None
        )
        _default_action = default_action.lower() if default_action else None

        # Attach metadata to function for introspection
        func._mcp_tool_name = tool_name
        func._mcp_timeout = timeout
        func._mcp_deprecated = deprecated
        func._mcp_hidden = hidden
        func._mcp_superseded_by = superseded_by
        func._mcp_requires_identity = requires_identity

        @wraps(func)
        async def wrapper(arguments: Dict[str, Any]):
            # #429: log every call to a deprecated tool so the operator can
            # tell when usage drops to ~0 and removal is safe. structured
            # prefix `deprecated_tool_called:` makes the log line greppable.
            # Once-per-call (not once-per-process) — duplicate volume is the
            # signal, not noise. No DB write here to keep the hot path off
            # asyncpg per CLAUDE.md's anyio coupling notes.
            if deprecated:
                _superseded = superseded_by or "(no replacement registered)"
                logger.warning(
                    f"deprecated_tool_called: tool={tool_name} "
                    f"superseded_by={_superseded}"
                )
            start_time = time.time()
            try:
                result = await asyncio.wait_for(func(arguments), timeout=timeout)
                elapsed = time.time() - start_time
                if elapsed > timeout * 0.8:
                    logger.warning(
                        f"Tool '{tool_name}' took {elapsed:.2f}s "
                        f"({elapsed/timeout*100:.1f}% of {timeout}s timeout)"
                    )
                # Normalize: MCP SDK 1.26.0 calls list() on return value.
                # Pydantic v2 models are iterable (yield field tuples), so a
                # bare TextContent would be destructured into invalid tuples,
                # causing 20 CallToolResult validation errors.
                if isinstance(result, TextContent):
                    result = [result]
                return result
            except asyncio.TimeoutError:
                logger.warning(f"Tool '{tool_name}' timed out after {timeout}s")
                # Wave 0 step 2A (RFC roadmap §86): emit coordination_failure
                # via the SYNC audit_logger path. Avoids the anyio task-group
                # conflict (CLAUDE.md "Known Issue") that would deadlock if we
                # awaited asyncpg from inside the wrapper's except clause.
                # emit_coordination_failure_sync is failure-safe by contract —
                # never raises — so it cannot mask the original TimeoutError
                # that the response below reports to the caller.
                from src.coordination_failure_emit import (
                    emit_coordination_failure_sync,
                )
                from src.mcp_handlers.context import (
                    get_context_agent_id,
                    get_context_session_key,
                )

                # Caller agent_id: prefer arguments-supplied (target/explicit) but
                # fall back to the session contextvar so consolidated tools like
                # observe(action=aggregate) — which carry no agent_id arg — still
                # attribute to the bound caller. Without the fallback, ~100% of
                # observed timeouts since 2A merged had agent_id NULL.
                args_agent_id = (
                    arguments.get("agent_id") if isinstance(arguments, dict) else None
                )
                effective_agent_id = args_agent_id or get_context_agent_id()
                session_id = get_context_session_key()

                from uuid import uuid4

                emit_payload: Dict[str, Any] = {
                    "tool_name": tool_name,
                    "timeout_s": timeout,
                    "elapsed_s": round(time.time() - start_time, 3),
                    # Wave 0 step 2 dedup contract (wave-0-step-2-call-site-scoping.md §"Dedup contract"):
                    # §129 evaluates `COUNT(DISTINCT payload->>'incident_id')`. The other four wired
                    # emit sites carry this field; this site was the gap (see
 # Caveat 1).
                    "incident_id": str(uuid4()),
                }
                if isinstance(arguments, dict) and arguments.get("action"):
                    emit_payload["action"] = arguments["action"]

                emit_coordination_failure_sync(
                    service="governance_mcp",
                    event_type="coordination_failure.mcp_handler_timeout.tool_decorator",
                    payload=emit_payload,
                    agent_id=effective_agent_id,
                    session_id=session_id,
                )
                return [error_response(
                    f"Tool '{tool_name}' timed out after {timeout} seconds.",
                    recovery={
                        "action": "Try again with simpler parameters or check system health.",
                        "related_tools": ["health_check"],
                    }
                )]
            except Exception as e:
                logger.error(f"Tool '{tool_name}' error: {e}", exc_info=True)
                return [error_response(
                    f"Error executing tool '{tool_name}': {str(e)}",
                    recovery={
                        "action": "Check tool parameters and try again",
                        "related_tools": ["health_check", "list_tools"],
                    }
                )]

        if register:
            _TOOL_DEFINITIONS[tool_name] = ToolDefinition(
                name=tool_name,
                handler=wrapper,
                timeout=timeout,
                description=tool_description,
                deprecated=deprecated,
                hidden=hidden,
                superseded_by=superseded_by,
                requires_identity=requires_identity,
                pre_onboard_actions=_pre_onboard_actions,
                default_action=_default_action,
                requires_verdict=requires_verdict,
            )

        return wrapper
    return decorator


# --- Backward-Compatible Accessors ---
# These delegate to _TOOL_DEFINITIONS so existing callers don't need to change.

def get_tool_registry() -> Dict[str, Callable]:
    """Get the registered tool handlers."""
    return {name: td.handler for name, td in _TOOL_DEFINITIONS.items()}


def get_tool_timeout(tool_name: str) -> float:
    """Get timeout for a tool."""
    td = _TOOL_DEFINITIONS.get(tool_name)
    return td.timeout if td else 30.0


def get_tool_description(tool_name: str) -> str:
    """Get description for a tool."""
    td = _TOOL_DEFINITIONS.get(tool_name)
    return td.description if td else ""


def get_tool_metadata(tool_name: str) -> Dict[str, Any]:
    """Get metadata for a tool (deprecated, hidden, superseded_by)."""
    td = _TOOL_DEFINITIONS.get(tool_name)
    if not td:
        return {}
    return {
        "deprecated": td.deprecated,
        "hidden": td.hidden,
        "superseded_by": td.superseded_by,
    }


def is_tool_deprecated(tool_name: str) -> bool:
    """Check if a tool is deprecated."""
    td = _TOOL_DEFINITIONS.get(tool_name)
    return td.deprecated if td else False


def is_tool_hidden(tool_name: str) -> bool:
    """Check if a tool is hidden from list_tools."""
    td = _TOOL_DEFINITIONS.get(tool_name)
    return td.hidden if td else False


def get_tool_identity_requirement(tool_name: str) -> str:
    """Get the identity requirement for a tool: 'required', 'pre_onboard', or 'scoped'.

    Defaults to 'required' for unknown tools (fail-closed). Used by the
    identity-bootstrap middleware to decide whether the tool may run
    without a bound identity.
    """
    td = _TOOL_DEFINITIONS.get(tool_name)
    return td.requires_identity if td else "required"


def get_call_identity_requirement(tool_name: str, arguments) -> str:
    """Identity requirement for a CALL: tool + action, alias-aware.

    The #425 gates (MCP dispatch middleware and the REST gate in
    http_tool_service) resolve at call granularity because the mixed
    read-write tools (knowledge/dialectic/agent/...) cannot be honestly
    classified at tool level — 'pre_onboard' would open their writes,
    'required' refuses their browsable reads.

    Resolution order:
    1. Alias-canonicalize the tool name (tool_stability) — legacy names
       like detect_anomalies route to observe(anomalies) at dispatch, so
       the gate must judge the canonical call, not the alias string. The
       alias's inject_action stands in when the caller sent no action.
    2. Tool-level 'pre_onboard' or 'scoped' wins outright.
    3. For 'required' tools with pre_onboard_actions: resolve the action
       exactly as action_router will (explicit `action`/`op`, else the
       tool's default_action); membership → 'pre_onboard'.
    4. Otherwise the tool-level value; unknown tools fail closed to
       'required'.
    """
    canonical, action = _resolve_canonical_and_action(tool_name, arguments)
    td = _TOOL_DEFINITIONS.get(canonical)
    if td is None:
        return "required"
    if td.requires_identity != "required":
        return td.requires_identity
    if not td.pre_onboard_actions:
        return td.requires_identity

    # _resolve_canonical_and_action lowercases explicit/injected actions and
    # applies the tool default_action, so this membership check has one
    # canonical source of truth shared with the stakes resolver.
    if action and action in td.pre_onboard_actions:
        return "pre_onboard"
    return td.requires_identity


def _resolve_canonical_and_action(tool_name: str, arguments):
    """Canonical tool name + resolved action for a CALL — the shared seam.

    Both the #425 identity resolver and the #775 stakes resolver agree on which
    canonical (tool, action) a call maps to, or their decisions diverge on
    aliased calls. This helper is the single canonicalization point used by both
    resolvers; `test_action_level_identity.py` and `test_stakes_table.py` pin
    the important alias/default-action behavior from each gate's perspective.
    """
    canonical = tool_name
    implied_action = None
    try:
        from src.mcp_handlers.tool_stability import resolve_tool_alias
        canonical, alias = resolve_tool_alias(tool_name)
        if alias is not None:
            implied_action = getattr(alias, "inject_action", None)
            if implied_action:
                implied_action = str(implied_action).lower()
    except ImportError:
        pass
    except Exception:
        logger.warning(
            "_resolve_canonical_and_action: alias resolution failed for %r — "
            "judging on the raw name (fails closed)",
            tool_name,
            exc_info=True,
        )

    action = None
    if isinstance(arguments, dict):
        action = arguments.get("action") or arguments.get("op")
    action = (str(action).lower() if action else None) or implied_action
    td = _TOOL_DEFINITIONS.get(canonical)
    if action is None and td is not None:
        action = td.default_action
    return canonical, action


def get_call_stakes_requirement(tool_name: str, arguments) -> str:
    """Stakes level for a CALL: "high" or "baseline" (#775).

    Mirrors `get_call_identity_requirement`'s call-granular, alias-aware shape so
    the future stakes gate judges the canonical call, not the alias string.

    Resolution:
      1. Alias-canonicalize the tool name and resolve the action (shared helper).
      2. A tool-level `requires_verdict="high"` declaration on the ToolDefinition
         wins outright (the inline escape hatch for single-purpose tools).
      3. Otherwise delegate to the standalone `stakes_table`, which is the
         authoritative per-action classification and FAILS CLOSED to "high" for
         any unknown tool/action.

    Pure and synchronous — no I/O, safe to call on the dispatch path. This
    resolver does NOT gate anything on its own; it is the classification half of
    #775. The gate mechanism that consumes it is parked (see the proposal doc).
    """
    from src.mcp_handlers import stakes_table

    canonical, action = _resolve_canonical_and_action(tool_name, arguments)
    td = _TOOL_DEFINITIONS.get(canonical)
    if td is not None and td.requires_verdict == "high":
        return "high"
    return stakes_table.get_action_stakes(canonical, action)


def get_tool_definition(tool_name: str) -> Optional[ToolDefinition]:
    """Get the full ToolDefinition for a registered tool."""
    return _TOOL_DEFINITIONS.get(tool_name)


def list_registered_tools(include_hidden: bool = False, include_deprecated: bool = True) -> list[str]:
    """List all registered tool names, optionally filtering hidden/deprecated."""
    tools = []
    for name in sorted(_TOOL_DEFINITIONS.keys()):
        td = _TOOL_DEFINITIONS[name]
        if td.hidden and not include_hidden:
            continue
        if td.deprecated and not include_deprecated:
            continue
        tools.append(name)
    return tools


# --- Action Router ---

def action_router(
    name: str,
    actions: Dict[str, Callable],
    *,
    timeout: float = 30.0,
    description: str = "",
    default_action: Optional[str] = None,
    param_maps: Optional[Dict[str, Dict[str, str]]] = None,
    examples: Optional[list] = None,
    pre_onboard_actions: Optional[set] = None,
):
    """
    Create a consolidated MCP tool from an action→handler mapping.

    Replaces repetitive if/elif action routing with a declarative definition.
    The generated handler extracts 'action' from arguments, validates it,
    applies parameter mappings, and delegates to the registered handler.

    Args:
        name: Tool name for MCP registration
        actions: Mapping of action name → async handler function
        timeout: Timeout in seconds
        description: Tool description
        default_action: If set, use this action when 'action' param is missing
        description: Prose summary ONLY (e.g. "Unified knowledge graph
            operations"). Do NOT hand-list the actions — the router appends the
            canonical action list derived from ``actions`` so discovery can
            never drift from the routing table.
        param_maps: Per-action parameter remapping.
            {"search": {"query": "search_query"}} means if action="search"
            and "query" is in arguments but "search_query" is not, copy it.
        examples: Example usage strings for error recovery messages

    Returns:
        The registered handler (same as @mcp_tool would return)
    """
    valid_actions = sorted(actions.keys())
    _param_maps = param_maps or {}
    _examples = examples or [f"{name}(action='{valid_actions[0]}')"]

    # Drift-proof discovery: the authoritative action list is DERIVED from the
    # action map, never a hand-maintained string. Callers pass prose only; the
    # router appends the canonical actions so a newly-wired action can't be
    # silently omitted from list_tools/describe_tool. This had drifted twice —
    # the `dialectic` description dropped `quick`, `knowledge` dropped
    # `synthesize`. Insertion order (not sorted) preserves the author's intended
    # reading order, e.g. dialectic's request -> thesis -> antithesis ->
    # synthesis flow.
    _prose = (description or f"{name} operations").rstrip(" .:")
    _full_description = f"{_prose} — actions: {', '.join(actions.keys())}."

    if pre_onboard_actions:
        # Compare lowercase-to-lowercase: action keys are lowercase by
        # convention everywhere today, but a mixed-case key would
        # otherwise make this guard fire a confusing false positive on a
        # CORRECT exemption (review fold, PR #611).
        unknown = set(a.lower() for a in pre_onboard_actions) - set(
            a.lower() for a in valid_actions
        )
        if unknown:
            raise ValueError(
                f"action_router {name!r}: pre_onboard_actions contains "
                f"unregistered actions {sorted(unknown)!r} — the exemption "
                f"set must be a subset of the action map so a typo can't "
                f"silently widen or narrow the identity gate."
            )

    @mcp_tool(
        name,
        timeout=timeout,
        description=_full_description,
        pre_onboard_actions=pre_onboard_actions,
        default_action=default_action,
    )
    async def router(arguments: Dict[str, Any]) -> Sequence[TextContent]:
        # Support both 'action' and 'op' (op is alias for consistency with other tools)
        action = (arguments.get("action") or arguments.get("op") or "").lower() or default_action

        if not action:
            return [error_response(
                "action parameter required",
                recovery={
                    "valid_actions": valid_actions,
                    "examples": _examples,
                }
            )]

        handler = actions.get(action)
        if handler is None:
            return [error_response(
                f"Unknown action: {action}",
                recovery={"valid_actions": valid_actions}
            )]

        # Apply parameter mappings for this action
        for src_key, dst_key in _param_maps.get(action, {}).items():
            if src_key in arguments and arguments.get(src_key) is not None:
                # Fill dst when absent or None; preserve explicit falsy values.
                if dst_key not in arguments or arguments.get(dst_key) is None:
                    arguments[dst_key] = arguments[src_key]

        return await handler(arguments)

    return router
