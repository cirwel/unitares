"""Workflow orchestration for process_agent_update."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Sequence

from mcp.types import TextContent

from src.logging_utils import get_logger
from src.services.update_response_service import (
    build_process_update_response_data,
    serialize_process_update_response,
)

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
        except TimeoutError:
            _tick("lock_timeout")
            cleaned = False
            # Stale *file* locks only exist on the fcntl backend; under the advisory
            # backend a timeout means a live PostgreSQL session holds the lock, so the
            # file-oriented cleanup would no-op and emit a misleading message. Skip it.
            advisory_backend = (
                os.environ.get("UNITARES_AGENT_LOCK_BACKEND", "advisory").strip().lower() == "advisory"
            )
            if not advisory_backend:
                try:
                    from src.lock_cleanup import cleanup_stale_state_locks
                    project_root = Path(__file__).resolve().parent.parent
                    cleanup_result = await ctx.loop.run_in_executor(
                        None, cleanup_stale_state_locks, project_root, 60.0, False
                    )
                    if cleanup_result["cleaned"] > 0:
                        logger.info(f"Auto-recovery: Cleaned {cleanup_result['cleaned']} stale lock(s) after timeout")
                        cleaned = True
                except Exception as cleanup_error:
                    logger.warning(f"Could not perform emergency lock cleanup: {cleanup_error}")

            if advisory_backend:
                cleanup_msg = "Another live session currently holds the lock. "
            elif cleaned:
                cleanup_msg = "The system has automatically cleaned stale locks. "
            else:
                cleanup_msg = "Automatic lock cleanup was attempted but did not resolve the issue. "
            return [error_response(
                f"Failed to acquire lock for agent '{ctx.agent_id}' after automatic retries and cleanup. "
                f"This usually means another active process is updating this agent. "
                f"{cleanup_msg}If this persists, try: "
                f"1) Wait a few seconds and retry, 2) Check for other Cursor/Claude sessions, "
                f"3) Use cleanup_stale_locks tool, or 4) Restart Cursor if stuck."
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
        # Flag-gated: no-op unless GROUNDING_SHADOW/APPLY set. See #1092 ordering fix.
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
