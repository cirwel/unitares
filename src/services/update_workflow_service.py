"""Workflow orchestration for process_agent_update."""

from __future__ import annotations

import time
from typing import Sequence

from mcp.types import TextContent

from src.logging_utils import get_logger
from src.services.update_response_service import (
    build_process_update_response_data,
    serialize_process_update_response,
)
from src.state_locking import LockTimeoutError

logger = get_logger(__name__)


async def run_process_update_workflow(ctx, *, serializer=None) -> Sequence[TextContent]:
    """Execute the extracted process_agent_update workflow for a prepared UpdateContext."""
    from src.mcp_handlers.updates.phases import (
        execute_locked_update,
        execute_post_update_effects,
        handle_onboarding_and_resume,
        prepare_unlocked_inputs,
        resolve_identity_and_guards,
        transform_inputs,
    )
    from src.mcp_handlers.updates.pipeline import run_enrichment_pipeline
    from src.mcp_handlers.updates.enrichments import run_grounding_stage
    from src.mcp_handlers.response_formatter import format_response
    from src.mcp_handlers.utils import error_response

    # Per-phase latency instrumentation. Emits one INFO line per call so we can
    # see in data whether the anyio-asyncio serialization cost lives in the
    # locked Phase 4 or the post-lock enrichment pipeline.
    _phase_ms: dict[str, int] = {}
    _t_total = time.perf_counter()
    _t_phase = _t_total

    def _tick(label: str) -> None:
        nonlocal _t_phase
        now = time.perf_counter()
        _phase_ms[label] = int((now - _t_phase) * 1000)
        _t_phase = now

    try:
        early_exit = await resolve_identity_and_guards(ctx)
        _tick("resolve_identity")
        if early_exit:
            return early_exit

        early_exit = await handle_onboarding_and_resume(ctx)
        _tick("onboard_resume")
        if early_exit:
            return early_exit

        early_exit = transform_inputs(ctx)
        _tick("transform")
        if early_exit:
            return early_exit

        await prepare_unlocked_inputs(ctx)
        _tick("prepare_unlocked")

        try:
            async with ctx.mcp_server.lock_manager.acquire_agent_lock_async(ctx.agent_id, timeout=5.0, max_retries=3):
                # Separate the lock-acquisition wait from the locked-update work so
                # the (A.2) falsifier can attribute any p99 change to the lock itself
                # rather than confounding it with the work under the lock.
                _tick("lock_acquire")
                early_exit = await execute_locked_update(ctx)
                _tick("locked_update")
                if early_exit:
                    return early_exit

                # Capture monitor ref while lock guarantees consistent state
                ctx.monitor = ctx.mcp_server.monitors.get(ctx.agent_id)
        except LockTimeoutError:
            # Only a lock-acquisition timeout. A TimeoutError from the locked
            # body (a slow query, a Redis wait) is not contention and must not
            # be reported as one, so it propagates.
            _tick("lock_timeout")
            # No cleanup here. A lock timeout means a live holder: the acquire loop
            # already removed any lock file no process held, and a held lock
            # (file or advisory) is released only when its holder finishes or
            # exits. Removing it would admit a second writer.
            return [error_response(
                f"Failed to acquire lock for agent '{ctx.agent_id}' after automatic retries. "
                f"Another live session or process holds this agent's lock and is still "
                f"updating it; the lock is released when that holder finishes or exits. "
                f"If this persists, try: 1) Wait a few seconds and retry, "
                f"2) Check for other sessions acting as this agent, "
                f"3) Restart your MCP client if it is the one stuck."
                ,
                error_code="LOCK_TIMEOUT",
                error_category="system_error",
                details={
                    "lock_error": True,
                    "agent_id": ctx.agent_id,
                },
                arguments=ctx.arguments,
            )]

        # --- Everything below runs OUTSIDE the lock ---

        # Grounding must run BEFORE persist + response-build read ctx.result.
        # (enrich_grounding still runs in the late pipeline but is idempotent.)
        # Metric replacement is flag-gated. Provenance is always stamped on ctx
        # so the persisted row identifies the winning coherence instrument even
        # when both flags are off. See #1092 ordering fix.
        await run_grounding_stage(ctx)
        _tick("grounding")

        await execute_post_update_effects(ctx)
        _tick("post_update")

        ctx.response_data = build_process_update_response_data(
            result=ctx.result,
            agent_id=ctx.agent_id,
            identity_assurance=ctx.identity_assurance,
            monitor=ctx.monitor,
            ctx_warnings=getattr(ctx, "warnings", ()),
        )
        _tick("build_response")

        await run_enrichment_pipeline(ctx)
        _tick("enrichment")

        try:
            ctx.response_data = format_response(
                ctx.response_data,
                ctx.arguments,
                meta=ctx.meta,
                is_new_agent=ctx.is_new_agent,
                key_was_generated=ctx.key_was_generated,
                api_key_auto_retrieved=ctx.api_key_auto_retrieved,
                task_type=ctx.task_type,
                # Match audit.tool_usage's session-id source exactly so the
                # nudge event can be joined to a later request action without
                # conflating a server-derived/scoped binding with the caller's
                # explicit session token. Missing explicit attribution stays
                # NULL and is reported separately by adoption_kpi.py.
                session_id=(ctx.arguments or {}).get("client_session_id"),
            )
        except Exception as fmt_err:
            logger.error(f"Response formatting failed: {fmt_err}", exc_info=True)

        ctx.arguments["lite_response"] = True
        result = serialize_process_update_response(
            response_data=ctx.response_data,
            agent_uuid=ctx.agent_uuid,
            arguments=ctx.arguments,
            fallback_result=ctx.result,
            serializer=serializer,
        )
        _tick("serialize")
        return result
    finally:
        total_ms = int((time.perf_counter() - _t_total) * 1000)
        phases_str = " ".join(f"{k}={v}ms" for k, v in _phase_ms.items())
        logger.info(f"[checkin_phases] total={total_ms}ms {phases_str}")
