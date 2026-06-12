"""HTTP-specific fallback execution for tools that still use MCP handlers."""

from __future__ import annotations

from typing import Dict, Optional


async def execute_http_dispatch_fallback(name: str, arguments: Optional[Dict[str, object]]):
    """Run dispatch-like middleware for HTTP after session context is already set."""
    from src.mcp_handlers.middleware import POST_EXECUTION_STEPS, POST_VALIDATION_STEPS
    from src.mcp_handlers.middleware.params_step import (
        inject_identity,
        resolve_alias,
        unwrap_kwargs,
        validate_params,
    )
    from src.mcp_handlers.middleware.trajectory_step import verify_trajectory
    from src.services.tool_dispatch_service import run_tool_dispatch_pipeline

    return await run_tool_dispatch_pipeline(
        name=name,
        arguments=arguments,
        pre_steps=[
            verify_trajectory,
            unwrap_kwargs,
            resolve_alias,
            inject_identity,
            validate_params,
        ],
        post_steps=POST_VALIDATION_STEPS,
        post_execution_steps=POST_EXECUTION_STEPS,
    )
