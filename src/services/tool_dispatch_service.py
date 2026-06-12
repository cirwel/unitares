"""Shared tool-dispatch pipeline runner."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Sequence

from mcp.types import TextContent


def _disclose_normalized_parameters(result: Any, normalized: Dict[str, Any]) -> Any:
    """Add the alias-layer normalized_parameters record to the response payload.

    Best-effort: only JSON-object payloads are annotated; anything else is
    returned untouched so disclosure can never break a working response.
    """
    try:
        if not result or not isinstance(result, (list, tuple)):
            return result
        first = result[0]
        text = getattr(first, "text", None)
        if not text:
            return result
        payload = json.loads(text)
        if not isinstance(payload, dict):
            return result
        payload["normalized_parameters"] = normalized
        first.text = json.dumps(payload, indent=2, ensure_ascii=False)
    except Exception:
        pass
    return result


async def run_tool_dispatch_pipeline(
    *,
    name: str,
    arguments: Optional[Dict[str, Any]],
    pre_steps,
    post_steps,
    post_execution_steps=(),
) -> Sequence[TextContent] | None:
    """Run a configurable middleware pipeline and execute the resolved handler.

    ``post_execution_steps`` run over the handler RESULT
    ((name, arguments, ctx, result) -> result) and are best-effort:
    a raising step is logged and skipped, never breaking the response.
    """
    from src.mcp_handlers import TOOL_HANDLERS
    from src.mcp_handlers.context import reset_session_context, reset_trajectory_confidence
    from src.mcp_handlers.error_helpers import tool_not_found_error
    from src.mcp_handlers.middleware import DispatchContext
    from src.mcp_handlers.middleware.identity_step import update_transport_binding
    from src.logging_utils import get_logger

    logger = get_logger(__name__)

    if arguments is None:
        arguments = {}

    ctx = DispatchContext()

    # Normalize wrappers at the runner boundary so identity/continuity logic
    # sees explicit inputs regardless of how a caller orders pre_steps.
    if "kwargs" in arguments:
        from src.mcp_handlers.middleware.params_step import unwrap_kwargs

        name, arguments, ctx = await unwrap_kwargs(name, arguments, ctx)

    for step in pre_steps:
        result = await step(name, arguments, ctx)
        if isinstance(result, list):
            if ctx.context_token is not None:
                reset_session_context(ctx.context_token)
            if ctx.trajectory_confidence_token is not None:
                reset_trajectory_confidence(ctx.trajectory_confidence_token)
            return result
        name, arguments, ctx = result

    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        if ctx.context_token is not None:
            reset_session_context(ctx.context_token)
        if ctx.trajectory_confidence_token is not None:
            reset_trajectory_confidence(ctx.trajectory_confidence_token)
        return tool_not_found_error(name, list(TOOL_HANDLERS.keys()))

    if ctx.migration_note:
        logger.info(f"Tool alias used: '{ctx.original_name}' → '{name}'. Migration: {ctx.migration_note}")

    for step in post_steps:
        result = await step(name, arguments, ctx)
        if isinstance(result, list):
            if ctx.context_token is not None:
                reset_session_context(ctx.context_token)
            if ctx.trajectory_confidence_token is not None:
                reset_trajectory_confidence(ctx.trajectory_confidence_token)
            return result
        name, arguments, ctx = result

    try:
        result = await handler(arguments)
        if ctx.normalized_parameters:
            result = _disclose_normalized_parameters(result, ctx.normalized_parameters)
        if result:
            if isinstance(result, (list, tuple)) and len(result) > 0:
                if hasattr(result[0], "text") and "Handler not yet extracted" in result[0].text:
                    return None
            elif hasattr(result, "text"):
                if "Handler not yet extracted" in result.text:
                    return None

        if ctx._transport_key:
            try:
                from src.mcp_handlers.context import (
                    get_context_agent_id,
                    get_session_resolution_source,
                )
                current_agent = get_context_agent_id()
                if current_agent and current_agent != ctx.bound_agent_id:
                    # S3: read whichever proof source the handler established
                    # for the rebound identity (the handler will have called
                    # set_session_resolution_source as part of its mint/bind).
                    # Strip any prior `sticky_cache:` envelope so the new
                    # binding records the underlying proof, not the cache-hit
                    # marker that brought us into this request.
                    _rebind_original = get_session_resolution_source() or "unknown"
                    if _rebind_original.startswith("sticky_cache:"):
                        _rebind_original = _rebind_original[len("sticky_cache:"):]
                    update_transport_binding(
                        ctx._transport_key,
                        current_agent,
                        ctx.session_key or "",
                        f"post_handler:{name}",
                        original_session_source=_rebind_original,
                    )
            except Exception:
                pass

        for exec_step in post_execution_steps:
            try:
                result = await exec_step(name, arguments, ctx, result)
            except Exception:
                logger.warning(
                    "post-execution step %s failed for tool %r - response returned unmodified",
                    getattr(exec_step, "__name__", exec_step),
                    name,
                    exc_info=True,
                )

        return result
    finally:
        if ctx.context_token is not None:
            reset_session_context(ctx.context_token)
        if ctx.trajectory_confidence_token is not None:
            reset_trajectory_confidence(ctx.trajectory_confidence_token)
