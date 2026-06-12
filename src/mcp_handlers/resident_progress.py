"""record_progress_pulse MCP tool handler.

Sentinel (and other residents) call this tool to post a progress pulse
into ``resident_progress_pulse``. The probe's SentinelPulseSource reads
those rows back via a batched DISTINCT ON query.
"""
from __future__ import annotations

from typing import Dict, Any, Sequence

from mcp.types import TextContent

from .decorators import mcp_tool
from .utils import require_registered_agent, error_response, success_response
from src.logging_utils import get_logger

logger = get_logger(__name__)


@mcp_tool("record_progress_pulse", timeout=5.0, register=True)
async def handle_record_progress_pulse(
    arguments: Dict[str, Any],
) -> Sequence[TextContent]:
    """Post a progress pulse for the calling resident agent.

    Sentinel calls this periodically so the probe can detect whether
    Sentinel is alive and making forward progress.
    """
    from pydantic import ValidationError
    from src.mcp_handlers.schemas.progress_flat import RecordProgressPulseParams
    from src.mcp_handlers.identity.shared import get_bound_agent_id
    from src.db import get_db

    # Resolve the authenticated agent (must be registered)
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]

    # Resolve bound UUID (canonical UUID from context / identity middleware)
    bound_uuid = get_bound_agent_id(arguments=arguments)
    if not bound_uuid:
        # Fallback: caller may supply their UUID explicitly
        bound_uuid = arguments.get("_agent_uuid") or None

    # Auth binding check: session must be bound to a UUID before validation
    if not bound_uuid:
        return [error_response(
            "Session is not bound to a UUID",
            error_code="UNBOUND_SESSION",
            error_category="auth_error",
        )]

    # Validate parameters with Pydantic schema
    try:
        params = RecordProgressPulseParams.model_validate(arguments)
    except ValidationError as exc:
        return [error_response(
            f"Invalid parameters: {exc}",
            error_code="INVALID_PARAMS",
            error_category="validation_error",
        )]

    # Auth binding check: supplied resident_uuid must match the bound agent
    if params.resident_uuid is not None and params.resident_uuid != bound_uuid:
        return [error_response(
            "resident_uuid does not match authenticated agent's bound UUID",
            error_code="AUTH_RESIDENT_MISMATCH",
            error_category="auth_error",
            details={
                "supplied": params.resident_uuid,
                "bound": (bound_uuid[:8] + "...") if bound_uuid else None,
            },
        )]

    effective_uuid = params.resident_uuid or bound_uuid

    try:
        db = get_db()
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO resident_progress_pulse (resident_uuid, metric_name, value)
                VALUES ($1, $2, $3)
                """,
                effective_uuid,
                params.metric_name,
                params.value,
            )
    except Exception as exc:
        logger.error("record_progress_pulse: DB insert failed: %s", exc)
        return [error_response(
            f"Failed to record pulse: {exc}",
            error_code="DB_ERROR",
            error_category="system_error",
        )]

    return success_response({
        "resident_uuid": effective_uuid,
        "metric_name": params.metric_name,
        "value": params.value,
    })
