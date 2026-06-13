"""Step 7: Rate limiting and loop detection."""

import time
from collections import defaultdict, deque
from typing import Any, Dict, Optional

from src.logging_utils import get_logger
from src.rate_limiter import get_rate_limiter
from ..utils import error_response
from ..error_helpers import rate_limit_error

logger = get_logger(__name__)

# Persistent state for expensive-read-only loop detection
_tool_call_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=200))

# Pure read tools exempt from the general limiter. These are standalone
# registered tools (not aliases), so the names survive resolve_alias on
# both the MCP and REST pipelines. Mixed read/write tools (knowledge,
# agent, observe, ...) are exempted per-call below via their declared
# pre_onboard_actions instead of by name. (A never-consulted
# rate_limit_exempt decorator flag was removed 2026-06-12 — this set and
# the pre_onboard_actions classification are the only exemption sources.)
_READ_ONLY_TOOLS = {
    'health_check', 'get_server_info', 'list_tools', 'get_thresholds',
    'search_knowledge_graph', 'get_governance_metrics', 'skills',
    'detect_stuck_agents',
}

# Expensive-read loop detection keys: (canonical name, resolved action).
# 'agent'/'list' is what the legacy list_agents alias dispatches as.
_EXPENSIVE_READ_CALLS = {("agent", "list")}
_LEGACY_EXPENSIVE_READ_TOOLS = {"list_agents"}  # direct-step callers/tests


def _loop_detection_key(name: str, arguments: Dict[str, Any]) -> Optional[str]:
    """Return the loop-detection history key for expensive read calls."""
    if name in _LEGACY_EXPENSIVE_READ_TOOLS:
        return name
    action = arguments.get("action") or arguments.get("op")
    action = str(action).lower() if action else None
    if (name, action) in _EXPENSIVE_READ_CALLS:
        return f"{name}:{action}"
    return None


def _rate_bucket_key(arguments: Dict[str, Any], ctx) -> str:
    """Resolve the rate-limit bucket for this call.

    Bound callers are bucketed by agent id. Unbound callers are bucketed
    per transport fingerprint (IP + UA hash) so one anonymous flood — a
    polling dashboard tab, an un-onboarded service — cannot exhaust a
    shared global bucket and lock every other caller out of onboard()
    (the 2026-06-12 bootstrap-lockout incident). The fingerprint is
    caller-supplied and spoofable; this is runaway protection, not an
    auth boundary — identity enforcement lives in the #425 gates.
    """
    agent_id = arguments.get('agent_id')
    if agent_id:
        return str(agent_id)
    bound = getattr(ctx, 'bound_agent_id', None)
    if bound:
        return str(bound)
    try:
        from ..context import get_session_signals
        signals = get_session_signals()
        if signals and signals.ip_ua_fingerprint:
            return f"anon:{signals.ip_ua_fingerprint}"
    except Exception:
        pass
    return 'anonymous'


def _is_pre_onboard_read_call(name: str, arguments: Dict[str, Any]) -> bool:
    """True iff this call resolves to a declared pre_onboard_actions read.

    Reuses the #425 call-granularity classification (alias-aware,
    default_action-aware). Tool-level pre_onboard lifecycle tools
    (onboard, identity, bind_session, ...) deliberately return False —
    they stay rate-limited per caller so identity minting can't run away.
    The drift-guard test on pre_onboard_actions pins that no mutating
    action can enter these sets.
    """
    try:
        from ..decorators import get_call_identity_requirement, get_tool_definition
        from ..tool_stability import resolve_tool_alias
        canonical, _ = resolve_tool_alias(name)
        td = get_tool_definition(canonical)
        if td is None or td.requires_identity != "required" or not td.pre_onboard_actions:
            return False
        return get_call_identity_requirement(name, arguments) == "pre_onboard"
    except Exception:
        # Classification failure fails closed: the call stays rate-limited.
        # Logged (like get_call_identity_requirement) because a regression
        # here silently re-limits every pre-onboard read with no signal.
        logger.warning(
            "_is_pre_onboard_read_call: classification failed for %r — "
            "failing closed (rate-limited)",
            name,
            exc_info=True,
        )
        return False


async def check_rate_limit(name: str, arguments: Dict[str, Any], ctx) -> Any:
    """Rate limiting for non-read-only tools + loop detection for expensive reads."""

    # Loop detection for expensive read-only tools
    loop_key = _loop_detection_key(name, arguments)
    if loop_key is not None:
        now = time.time()
        tool_history = _tool_call_history[loop_key]

        # Clean up old calls (keep last 60 seconds)
        cutoff = now - 60
        while tool_history and tool_history[0] < cutoff:
            tool_history.popleft()
        if not tool_history:
            del _tool_call_history[loop_key]

        if len(tool_history) >= 20:
            return [error_response(
                f"Tool call loop detected: '{name}' called {len(tool_history)} times globally in the last 60 seconds. "
                f"This may indicate a stuck agent. Please wait 30 seconds before retrying.",
                recovery={
                    "action": "Wait 30 seconds before retrying this tool",
                    "related_tools": ["health_check", "get_governance_metrics"],
                    "workflow": "1. Wait 30 seconds 2. Check agent health 3. Retry if needed"
                },
                context={
                    "tool_name": name,
                    "calls_in_last_minute": len(tool_history),
                    "note": "Global rate limit (agent listing has no per-caller key)"
                }
            )]

        _tool_call_history[loop_key].append(now)

    # General rate limiting (skip for read-only tools and pre-onboard reads)
    if name in _READ_ONLY_TOOLS or _is_pre_onboard_read_call(name, arguments):
        return name, arguments, ctx

    bucket = _rate_bucket_key(arguments, ctx)
    rate_limiter = get_rate_limiter()
    allowed, error_msg = rate_limiter.check_rate_limit(bucket)

    if not allowed:
        return rate_limit_error(bucket, rate_limiter.get_stats(bucket))

    return name, arguments, ctx
