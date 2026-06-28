#!/usr/bin/env python3
"""Tool registration + dispatch-wrapping layer for the governance MCP server.

Extracted from ``src/mcp_server.py`` to keep the server entry point legible.
This module owns everything that turns the handler registry into
FastMCP-registered tools:

- ``get_tool_wrapper`` — the per-tool dispatch wrapper (the request hot path):
  Wave 3a BEAM routing + Python fallback, metrics, and TextContent unwrapping.
- ``auto_register_all_tools`` / ``_register_common_aliases`` — build typed
  FastMCP wrappers from the schema/alias registries and register them on the
  FastMCP instance.
- ``_session_id_from_ctx`` — resolve a stable per-client session id for the
  session-injection wrappers.
- ``TOOLS_NEEDING_SESSION_INJECTION`` / ``EXTRA_ARGUMENT_PASSTHROUGH_TOOLS`` —
  the per-tool policy sets the registrars consult.

Dependency-injection note: ``auto_register_all_tools(mcp)`` and
``_register_common_aliases(mcp)`` take the FastMCP instance as a parameter
rather than importing it from ``mcp_server``. That is deliberate — it breaks
what would otherwise be a circular import (``mcp_server`` creates ``mcp`` and
calls these; these would need ``mcp`` back). The call sites in
``mcp_server`` pass the freshly-constructed instance.

Patch-location note for tests: ``get_tool_wrapper``'s closure binds the module
globals of THIS module (``dispatch_tool``, ``_wave3a_proxy_to_beam``, ...). A
test that monkeypatches dispatch/proxy must patch ``src.tool_registration.*``
and clear ``src.tool_registration._tool_wrappers_cache`` — not ``mcp_server``.
"""

from __future__ import annotations

import json
import time
from typing import Dict

from mcp.server.fastmcp import Context

from src.logging_utils import get_logger
from src.metrics_registry import TOOL_CALLS_TOTAL, TOOL_CALL_DURATION

# Import dispatch_tool from handlers (reuse all existing tool logic)
from src.mcp_handlers import dispatch_tool
# Wave 3a per-tool routing table imports — hoisted to module load time so
# (a) the per-call ~200-500ns import cost vanishes from the dispatch hot
# path, and (b) ``patch("src.tool_registration.get_route")`` style mocks
# affect the wrapper (function-local imports bypass module-level patches —
# see memory ``feedback_patch-local-imports``). FIND-A3 review fold.
from src.wave3a_routing import get_route as _wave3a_get_route
from src.wave3a_beam_proxy import proxy_to_beam as _wave3a_proxy_to_beam

logger = get_logger(__name__)


def _session_id_from_ctx(ctx: Context | None) -> str | None:
    """
    Resolve a stable per-client session identifier for identity binding.

    Uses SessionSignals when available (set by ASGI wrapper), otherwise
    falls back to legacy extraction paths.
    """
    # Check SessionSignals first (set by ASGI wrapper / ConnectionTrackingMiddleware)
    try:
        from src.mcp_handlers.context import get_session_signals
        signals = get_session_signals()
        if signals:
            # Same priority as derive_session_key, minus async pin lookup
            return (
                signals.x_session_id
                or (f"mcp:{signals.mcp_session_id}" if signals.mcp_session_id else None)
                or signals.oauth_client_id
                or signals.x_client_id
                or signals.ip_ua_fingerprint
            )
    except Exception:
        pass

    # Fallback: legacy extraction (for callers before signals are set)
    try:
        from src.mcp_handlers.context import get_mcp_session_id
        mcp_sid = get_mcp_session_id()
        if mcp_sid:
            return f"mcp:{mcp_sid}"
    except Exception:
        pass

    if ctx is None:
        return None

    try:
        req = ctx.request_context.request
        if req is not None:
            client_id = getattr(req.state, "governance_client_id", None)
            if client_id:
                return client_id
    except Exception:
        pass

    try:
        if ctx.client_id:
            return ctx.client_id
    except Exception:
        pass
    return None


# ============================================================================
# Tool Registration Helper (Optimized)
# ============================================================================

# Cache tool wrappers to avoid recreating functions on every call
# Max size: 100 tools (future-proofing for dynamic tool registration)
# Current tool count: ~51 tools, so plenty of headroom
_MAX_TOOL_WRAPPER_CACHE_SIZE = 100
_tool_wrappers_cache: Dict[str, callable] = {}

def get_tool_wrapper(tool_name: str):
    """
    Get cached tool wrapper or create new one.

    Optimized version that caches wrappers to avoid function creation overhead.
    Each wrapper calls dispatch_tool which routes to @mcp_tool decorated handlers.

    Cache has size limit (100 tools) to prevent unbounded growth if dynamic
    tool registration is added in the future.
    """
    if tool_name not in _tool_wrappers_cache:
        # Check cache size limit (future-proofing)
        if len(_tool_wrappers_cache) >= _MAX_TOOL_WRAPPER_CACHE_SIZE:
            logger.warning(
                f"Tool wrapper cache size limit reached ({_MAX_TOOL_WRAPPER_CACHE_SIZE}). "
                f"Cache contains {len(_tool_wrappers_cache)} tools. "
                f"Consider increasing _MAX_TOOL_WRAPPER_CACHE_SIZE if dynamic tool registration is used."
            )
            # Don't fail - allow cache to grow slightly beyond limit
            # But log warning for visibility
        async def wrapper(**kwargs):
            start_time = time.time()
            logger.info(f"[TOOL_WRAPPER] {tool_name}: called with keys={list(kwargs.keys())}")
            try:
                # Wave 3a per-tool routing table (RFC docs/proposals/
                # beam-wave-3a-read-only-handlers.md v0.2 §3.1). If the
                # tool has been cut over to BEAM, attempt the BEAM proxy
                # with §3.2's 500ms hard timeout. On any BEAM failure mode
                # we fall back to the existing Python dispatch — silent
                # skip is the worst possible outcome per §3.2.
                #
                # Hot-path discipline: the lookup is O(1) and cheap. For
                # the ~100 tools NOT in the routing table the lookup returns
                # None and the existing dispatch fires unchanged. Imports
                # are at module top-level (FIND-A3 review fold) so the
                # per-call cost is zero and ``patch()``-style mocks work.
                beam_url = _wave3a_get_route(tool_name)
                if beam_url is not None:
                    proxy_result = await _wave3a_proxy_to_beam(
                        tool_name=tool_name,
                        beam_url=beam_url,
                        kwargs=kwargs,
                    )
                    if proxy_result.ok:
                        # BEAM succeeded — return its response unchanged.
                        # Python implementation MUST NOT be touched.
                        duration = time.time() - start_time
                        TOOL_CALLS_TOTAL.labels(
                            tool_name=tool_name, status="success"
                        ).inc()
                        TOOL_CALL_DURATION.labels(tool_name=tool_name).observe(
                            duration
                        )
                        return proxy_result.response
                    # BEAM failed — fall through to Python dispatch.
                    # The proxy already emitted the §4.2 fallback event.
                    logger.info(
                        "[TOOL_WRAPPER] %s: BEAM fallback (reason=%s)",
                        tool_name,
                        proxy_result.fallback_reason,
                    )

                # Dispatch to existing handler (which has @mcp_tool timeout protection)
                result = await dispatch_tool(tool_name, kwargs)

                # Record successful call metrics
                duration = time.time() - start_time
                TOOL_CALLS_TOTAL.labels(tool_name=tool_name, status="success").inc()
                TOOL_CALL_DURATION.labels(tool_name=tool_name).observe(duration)

                if result is None:
                    TOOL_CALLS_TOTAL.labels(tool_name=tool_name, status="not_found").inc()
                    return {"success": False, "error": f"Tool '{tool_name}' not found"}

                # Extract structured payload from TextContent response
                # Many MCP clients enforce outputSchema and require structured output.
                # Our handlers return JSON in TextContent.text; parse it and return an object.
                if isinstance(result, (list, tuple)) and len(result) > 0:
                    first_result = result[0]
                    if hasattr(first_result, 'text'):
                        text = first_result.text
                        try:
                            return json.loads(text)
                        except Exception:
                            return {"success": True, "text": text}

                return {"success": True, "result": str(result)}

            except (KeyboardInterrupt, SystemExit):
                # Let system exceptions propagate (for proper shutdown)
                # These should not be caught by error handlers
                raise
            except Exception as e:
                # Record error metrics
                duration = time.time() - start_time
                TOOL_CALLS_TOTAL.labels(tool_name=tool_name, status="error").inc()
                TOOL_CALL_DURATION.labels(tool_name=tool_name).observe(duration)

                # Log error for visibility
                # Note: @mcp_tool decorator on handlers also catches exceptions,
                # but dispatch_tool may raise before reaching handler (e.g., rate limit)
                logger.error(f"Error in tool wrapper {tool_name}: {e}", exc_info=True)
                return {"success": False, "error": str(e), "error_type": type(e).__name__}

        wrapper.__name__ = tool_name
        wrapper.__doc__ = f"Wrapper for {tool_name} tool"
        _tool_wrappers_cache[tool_name] = wrapper

    return _tool_wrappers_cache[tool_name]


# ============================================================================
# AUTO-REGISTRATION SYSTEM
# ============================================================================
# Instead of manually decorating each tool, we auto-register from tool_schemas.py
# This prevents tools from getting out of sync between schemas and SSE server.

# Tools that need session injection from FastMCP Context
# These tools get client_session_id injected from the SSE connection
TOOLS_NEEDING_SESSION_INJECTION = {
    "onboard",
    "identity",
    "process_agent_update",
    "get_governance_metrics",
    "store_knowledge_graph",
    "search_knowledge_graph",
    "leave_note",
    "observe_agent",
    "get_agent_metadata",
    "update_agent_metadata",
    "archive_agent",
    "delete_agent",
    "get_system_history",
    "export_to_file",
    "mark_response_complete",
    "request_dialectic_review",
    "direct_resume_if_safe",
    "update_discovery_status_graph",
    "get_discovery_details",
    "dialectic",
    "get_knowledge_graph",
    "compare_me_to_similar",
}

# FastMCP validates tool arguments before dispatch_tool sees them. For these
# internal/provenance-heavy tools, UNITARES dispatch middleware is the source of
# truth and must receive caller-supplied extra fields unchanged.
EXTRA_ARGUMENT_PASSTHROUGH_TOOLS = {
    "process_agent_update",
}


def auto_register_all_tools(mcp):
    """
    Auto-register tools from tool_schemas.py with typed signatures.

    Only registers tools that are in the decorator registry (register=True)
    AND in the active tool mode's allowed set (unless mode is "full").
    Tools with register=False in @mcp_tool decorator are skipped.

    This generates wrappers with explicit parameter signatures from JSON schemas,
    allowing FastMCP to infer correct schemas without kwargs wrapping.

    Benefits:
    - Claude.ai sends parameters directly (no kwargs wrapper needed)
    - CLI's kwargs wrapping still works (dispatch_tool unwraps)
    - Proper client autocomplete from typed signatures
    - Mode filtering reduces tool count for Claude Code (no deferred tools)

    Just add the tool to:
    1. tool_schemas.py (definition)
    2. mcp_handlers/*.py (implementation with @mcp_tool)

    The SSE server will automatically pick it up.
    """
    from src.tool_schemas import get_tool_definitions
    from src.mcp_handlers.support.wrapper_generator import (
        create_typed_wrapper,
        enable_extra_argument_passthrough,
    )
    from src.mcp_handlers.decorators import get_tool_registry
    from src.tool_modes import TOOL_MODE, get_tools_for_mode

    tools = get_tool_definitions()
    registered_count = 0
    skipped_count = 0
    mode_filtered_count = 0

    # Get tools that are registered (register=True in @mcp_tool decorator)
    registered_tools = get_tool_registry()

    # Get allowed tools for current mode (skip filtering in full mode)
    allowed_tools = get_tools_for_mode(TOOL_MODE) if TOOL_MODE != "full" else None

    for tool in tools:
        tool_name = tool.name

        # Skip tools not in registry (register=False in decorator)
        if tool_name not in registered_tools:
            skipped_count += 1
            continue

        # Skip tools not in the active mode's allowed set
        if allowed_tools is not None and tool_name not in allowed_tools:
            mode_filtered_count += 1
            continue

        description = tool.description.split("\n")[0] if tool.description else f"Tool: {tool_name}"
        input_schema = getattr(tool, 'inputSchema', {}) or {}
        inject_session = tool_name in TOOLS_NEEDING_SESSION_INJECTION

        try:
            # Create typed wrapper with explicit parameter signature
            wrapper = create_typed_wrapper(
                tool_name=tool_name,
                input_schema=input_schema,
                get_handler=get_tool_wrapper,
                inject_session=inject_session,
                session_extractor=_session_id_from_ctx,
            )

            # Register with FastMCP - it will infer schema from signature
            mcp.tool(description=description, structured_output=False)(wrapper)
            if tool_name in EXTRA_ARGUMENT_PASSTHROUGH_TOOLS:
                tool_manager = getattr(mcp, "_tool_manager", None)
                registered_tool = (
                    tool_manager.get_tool(tool_name)
                    if tool_manager and hasattr(tool_manager, "get_tool")
                    else None
                )
                if registered_tool is None:
                    logger.warning(
                        "Failed to enable extra argument passthrough for %s: "
                        "registered FastMCP tool not found",
                        tool_name,
                    )
                else:
                    enabled = enable_extra_argument_passthrough(registered_tool)
                    if enabled:
                        logger.info(
                            "Enabled extra argument passthrough for %s",
                            tool_name,
                        )
            registered_count += 1

        except Exception as e:
            logger.warning(f"Failed to auto-register tool {tool_name}: {e}")

    logger.info(
        f"[AUTO_REGISTER] Registered {registered_count} tools, "
        f"skipped {skipped_count} (not in registry), "
        f"filtered {mode_filtered_count} (mode={TOOL_MODE})"
    )
    return registered_count


# ============================================================================
# COMMON ALIASES - Register most-guessed tool names as thin MCP wrappers
# ============================================================================
# Aliases are resolved at dispatch time (tool_stability.py), but FastMCP rejects
# unknown tool names before dispatch runs. These register the top aliases so
# agents can use intuitive names like status() without "Unknown tool" errors.

def _register_common_aliases(mcp):
    from src.mcp_handlers.tool_stability import (
        AGENT_WORKFLOW_ALIASES,
        resolve_tool_alias,
    )
    from src.mcp_handlers.support.wrapper_generator import (
        create_typed_wrapper,
        enable_extra_argument_passthrough,
    )

    common = list(AGENT_WORKFLOW_ALIASES)
    count = 0
    for alias_name in common:
        actual, info = resolve_tool_alias(alias_name)
        if not info:
            continue

        # Get the actual tool's schema so the alias has matching parameters
        from src.tool_schemas import get_tool_definitions
        actual_schema = {}
        for tool_def in get_tool_definitions():
            if tool_def.name == actual:
                actual_schema = getattr(tool_def, 'inputSchema', {}) or {}
                break

        # If inject_action is set, remove "action" from the alias schema —
        # the alias auto-injects it, so clients shouldn't need to provide it
        if info.inject_action and actual_schema:
            import copy
            actual_schema = copy.deepcopy(actual_schema)
            actual_schema.get("properties", {}).pop("action", None)
            req = actual_schema.get("required", [])
            if "action" in req:
                actual_schema["required"] = [r for r in req if r != "action"]

        try:
            wrapper = create_typed_wrapper(
                tool_name=alias_name,
                input_schema=actual_schema,
                get_handler=get_tool_wrapper,
                inject_session=actual in TOOLS_NEEDING_SESSION_INJECTION,
                session_extractor=_session_id_from_ctx,
            )
            desc = f"{info.migration_note or f'Alias for {actual}'}"
            mcp.tool(description=desc, structured_output=False)(wrapper)
            if actual in EXTRA_ARGUMENT_PASSTHROUGH_TOOLS:
                tool_manager = getattr(mcp, "_tool_manager", None)
                registered_tool = (
                    tool_manager.get_tool(alias_name)
                    if tool_manager and hasattr(tool_manager, "get_tool")
                    else None
                )
                if registered_tool is not None:
                    enable_extra_argument_passthrough(registered_tool)
            count += 1
        except Exception as e:
            logger.debug(f"[ALIAS] Failed to register {alias_name}: {e}")

    if count:
        logger.info(f"[AUTO_REGISTER] Registered {count} common aliases")
