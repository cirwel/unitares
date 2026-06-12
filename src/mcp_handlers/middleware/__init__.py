"""
Dispatch Middleware — Pipeline steps for tool dispatch.

Each step is a standalone async function: (name, arguments, ctx) → (name, arguments, ctx) or list[TextContent].
Returning a list short-circuits the pipeline with that response.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple, Union

from mcp.types import TextContent

# Re-export all step functions
from .identity_step import resolve_identity
from .trajectory_step import verify_trajectory
from .params_step import unwrap_kwargs, resolve_alias, inject_identity, validate_params, _format_pydantic_error
from .rate_limit_step import check_rate_limit, _tool_call_history
from .pattern_step import track_patterns
from .envelope_step import apply_experience_envelope

# Type alias for middleware return
MiddlewareResult = Union[
    Tuple[str, Dict[str, Any], "DispatchContext"],  # continue
    list,  # short-circuit
]


@dataclass
class DispatchContext:
    """State that flows between dispatch middleware steps."""
    session_key: Optional[str] = None
    client_session_id: Optional[str] = None
    bound_agent_id: Optional[str] = None
    context_token: Optional[object] = None
    trajectory_confidence_token: Optional[object] = None
    migration_note: Optional[str] = None
    normalized_parameters: Optional[Dict[str, Any]] = None
    original_name: Optional[str] = None
    client_hint: Optional[str] = None
    identity_result: Optional[dict] = None
    _transport_key: Optional[str] = None  # Sticky transport binding cache key


# Steps that must succeed (short-circuit on error)
PRE_DISPATCH_STEPS = [
    unwrap_kwargs,
    resolve_identity,
    verify_trajectory,
    resolve_alias,
    inject_identity,
    validate_params,
]

# Steps that run but don't block (best-effort)
POST_VALIDATION_STEPS = [
    check_rate_limit,
    track_patterns,
]

# Steps that run AFTER the handler, over its result (best-effort).
# Signature: (name, arguments, ctx, result) -> result. The runner
# guards each step; a failing step never breaks the response.
POST_EXECUTION_STEPS = [
    apply_experience_envelope,
]
