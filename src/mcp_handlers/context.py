"""
Session Context Module

Uses Python contextvars to propagate session context through the call stack
without threading arguments through every function call.

This solves the session key mismatch issue where:
- SSE transport binds under client_session_id (e.g., "34.162.136.91:0")
- But handlers call get_bound_agent_id(arguments=None) which falls back to stdio:{pid}

With contextvars, session context is set once at dispatch entry and accessible everywhere.
"""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, Optional
# =============================================================================
# SESSION SIGNALS (unified transport signal capture)
# =============================================================================

@dataclass(frozen=True)
class SessionSignals:
    """Frozen snapshot of all transport-level signals for session key derivation.

    Captured once at the ASGI/HTTP layer, stored in a contextvar, and read by
    the single ``derive_session_key()`` function in identity_v2.py.

    No priority decisions happen here — this is a pure data capture object.
    """
    mcp_session_id: Optional[str] = None      # mcp-session-id header
    x_session_id: Optional[str] = None        # X-Session-ID header
    x_client_id: Optional[str] = None         # X-Client-Id / X-MCP-Client-Id header
    oauth_client_id: Optional[str] = None     # oauth:CLIENT_ID from Bearer token
    ip_ua_fingerprint: Optional[str] = None   # IP:MD5(UA)[:6] fallback
    user_agent: Optional[str] = None          # raw User-Agent
    client_hint: Optional[str] = None         # detected client type (cursor, claude_desktop, etc.)
    x_agent_name: Optional[str] = None        # X-Agent-Name header
    x_agent_id: Optional[str] = None          # X-Agent-Id header
    transport: str = "unknown"                # "mcp", "rest", "sse", "stdio", "uds"
    # S19 substrate attestation: kernel-attested peer PID for UDS connections
    # only. Populated by the UDS listener (PR3c) at accept time via
    # LOCAL_PEERPID; left None for HTTP/SSE/stdio transports. Read by the
    # substrate-claim verification path in src/substrate/verification.py.
    peer_pid: Optional[int] = None
    # Operator-tier credential (X-Unitares-Operator header). Application-level
    # bearer token presented by trusted infrastructure (Discord bridge,
    # dashboard, ollama bridge) to opt into operator-class privileges in
    # specific handlers (initially: list_agents UUID disclosure). Compared
    # against UNITARES_OPERATOR_TOKENS env-var allowlist by
    # ``is_operator_caller`` in ``src/mcp_handlers/identity/operator.py``.
    # Distinct from transport fingerprints — see KG 2026-04-20T00:57:45 and
 # for why
    # client_session_id cannot serve this role.
    unitares_operator_token: Optional[str] = None

# Contextvar for SessionSignals — set once per request at the transport layer
_session_signals: ContextVar[Optional[SessionSignals]] = ContextVar('session_signals', default=None)

def set_session_signals(signals: SessionSignals) -> object:
    """Store SessionSignals for the current request. Returns token for reset."""
    return _session_signals.set(signals)

def get_session_signals() -> Optional[SessionSignals]:
    """Get SessionSignals for the current request, or None if not set."""
    return _session_signals.get()

def reset_session_signals(token: object) -> None:
    """Reset SessionSignals using token from set_session_signals."""
    _session_signals.reset(token)

# Session context - set at request entry, accessible throughout the request lifecycle
_session_context: ContextVar[Optional[Dict[str, Any]]] = ContextVar('session_context', default=None)

# Transport-level client hint - set at ASGI/HTTP layer, before MCP SDK processing
# This allows auto-detection of client type (e.g., "cursor") even when MCP SDK
# doesn't expose HTTP headers to tool handlers
_transport_client_hint: ContextVar[Optional[str]] = ContextVar('transport_client_hint', default=None)

# MCP session ID - extracted from mcp-session-id header at ASGI layer
# This enables implicit identity binding without clients manually passing client_session_id
_mcp_session_id: ContextVar[Optional[str]] = ContextVar('mcp_session_id', default=None)
_session_resolution_source: ContextVar[Optional[str]] = ContextVar('session_resolution_source', default=None)

def set_session_context(
    session_key: Optional[str] = None,
    client_session_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    **extra: Any
) -> object:
    """
    Set session context for the current request.

    Call at SSE/REST dispatch entry point. Returns a token for reset.

    Args:
        session_key: The resolved session key for identity binding
        client_session_id: Raw client session ID from transport
        agent_id: The bound agent ID if known
        **extra: Additional context (e.g., request metadata)

    Returns:
        Token for resetting context (use in finally block)
    """
    ctx = {
        'session_key': session_key or client_session_id,
        'client_session_id': client_session_id,
        'agent_id': agent_id,
        **extra
    }
    return _session_context.set(ctx)

def reset_session_context(token: object) -> None:
    """Reset session context using token from set_session_context."""
    _session_context.reset(token)

def get_session_context() -> Dict[str, Any]:
    """Get the current session context dict."""
    return _session_context.get() or {}

def get_context_session_key() -> Optional[str]:
    """
    Get session key from context.

    Returns None if no context is set (caller should use fallback).
    """
    ctx = _session_context.get()
    return ctx.get('session_key') if ctx else None

def get_context_client_session_id() -> Optional[str]:
    """Get raw client_session_id from context."""
    ctx = _session_context.get()
    return ctx.get('client_session_id') if ctx else None

def get_context_agent_id() -> Optional[str]:
    """Get bound agent_id from context if known."""
    ctx = _session_context.get()
    return ctx.get('agent_id') if ctx else None

def update_context_agent_id(agent_id: str) -> None:
    """Update agent_id in context (e.g., after binding)."""
    ctx = _session_context.get()
    if ctx is None:
        return
    # Create a new dict to avoid mutating shared state
    _session_context.set({**ctx, 'agent_id': agent_id})

def get_context_client_hint() -> Optional[str]:
    """
    Get client_hint from context (e.g., 'cursor', 'chatgpt').

    Checks in order:
    1. Session context (set by dispatch_tool from arguments)
    2. Transport-level contextvar (set by ASGI handler from User-Agent)
    """
    # First check session context
    ctx = _session_context.get()
    hint = ctx.get('client_hint') if ctx else None
    if hint:
        return hint

    # Fall back to transport-level contextvar
    return _transport_client_hint.get()

def set_transport_client_hint(hint: str) -> object:
    """
    Set transport-level client hint (call from ASGI handler).

    Returns token for reset.
    """
    return _transport_client_hint.set(hint)

def reset_transport_client_hint(token: object) -> None:
    """Reset transport-level client hint."""
    _transport_client_hint.reset(token)

def set_mcp_session_id(session_id: str) -> object:
    """
    Set MCP session ID from mcp-session-id header (call from ASGI handler).

    Returns token for reset.
    """
    return _mcp_session_id.set(session_id)

def reset_mcp_session_id(token: object) -> None:
    """Reset MCP session ID."""
    _mcp_session_id.reset(token)

def get_mcp_session_id() -> Optional[str]:
    """
    Get MCP session ID from context.

    This is the implicit session identifier from the MCP protocol's
    mcp-session-id header. Use for identity binding when available.
    """
    return _mcp_session_id.get()


def set_session_resolution_source(source: str) -> object:
    """Set session resolution source for current request."""
    return _session_resolution_source.set(source)


def reset_session_resolution_source(token: object) -> None:
    """Reset session resolution source."""
    _session_resolution_source.reset(token)


def get_session_resolution_source() -> Optional[str]:
    """Get session resolution source for current request."""
    return _session_resolution_source.get()


# Pin scope detail. Set by derive_session_key when an onboard-pin lookup hits,
# distinguishing which candidate form matched (client_model / client / model /
# unscoped). Kept as a side-channel so the load-bearing exact-match comparison
# at handlers.py against `session_resolution_source == "pinned_onboard_session"`
# continues to work unchanged.
_pin_match_scope: ContextVar[Optional[str]] = ContextVar('pin_match_scope', default=None)


def set_pin_match_scope(scope: Optional[str]) -> object:
    """Set pin match scope for current request. Returns token for reset."""
    return _pin_match_scope.set(scope)


def reset_pin_match_scope(token: object) -> None:
    """Reset pin match scope."""
    _pin_match_scope.reset(token)


def get_pin_match_scope() -> Optional[str]:
    """Get pin match scope for current request, or None if no pin matched."""
    return _pin_match_scope.get()


# Shadow pin lookup observation. Set by derive_session_key after-hook when a
# non-pin path won despite an IP/UA fingerprint signal being present — answers
# "would the pin have hit if we'd looked it up?" without changing resolution
# order. None on all three keys means no shadow lookup ran.
_shadow_pin_present: ContextVar[Optional[bool]] = ContextVar('shadow_pin_present', default=None)
_shadow_pin_match: ContextVar[Optional[bool]] = ContextVar('shadow_pin_match', default=None)
_shadow_pin_age_seconds: ContextVar[Optional[int]] = ContextVar('shadow_pin_age_seconds', default=None)


def set_shadow_pin_observation(
    *,
    present: Optional[bool],
    match: Optional[bool],
    age_seconds: Optional[int],
) -> None:
    """Record an observation-only shadow pin lookup result."""
    _shadow_pin_present.set(present)
    _shadow_pin_match.set(match)
    _shadow_pin_age_seconds.set(age_seconds)


def get_shadow_pin_observation() -> Dict[str, Optional[Any]]:
    """Get the shadow pin observation as a dict; all-None if not run."""
    return {
        "pin_entry_present": _shadow_pin_present.get(),
        "pin_fingerprint_match": _shadow_pin_match.get(),
        "pin_entry_age_seconds": _shadow_pin_age_seconds.get(),
    }

# Trajectory identity confidence - set during dispatch if verification runs
_trajectory_confidence: ContextVar[Optional[float]] = ContextVar('trajectory_confidence', default=None)

def set_trajectory_confidence(confidence: float) -> object:
    """Set trajectory confidence for current request. Returns token for reset."""
    return _trajectory_confidence.set(confidence)

def reset_trajectory_confidence(token: object) -> None:
    """Reset trajectory confidence."""
    _trajectory_confidence.reset(token)

def get_trajectory_confidence() -> Optional[float]:
    """Get trajectory confidence from context, or None if not set."""
    return _trajectory_confidence.get()

_logged_ua_fingerprints: set = set()
_MAX_LOGGED_UA_FINGERPRINTS = 512


def note_ua_fingerprint(ua_fingerprint: Optional[str], user_agent: Optional[str]) -> None:
    """Record the fingerprint -> raw User-Agent preimage, once per distinct
    hash per process lifetime.

    Session keys carry only md5(UA)[:6], which is one-way: when an unbound
    caller shows up in the logs as e.g. ``127.0.0.1:f304dd:*`` there is
    nothing in the system that says what client that actually is (the
    stage-1 strict-identity burn-in spent a candidate hunt on exactly that
    and gave up with "revisit with a header capture" — this is that
    capture). One INFO line per distinct fingerprint; the set is capped so
    a UA-randomizing caller cannot grow it unbounded.
    """
    if not ua_fingerprint or ua_fingerprint in _logged_ua_fingerprints:
        return
    if len(_logged_ua_fingerprints) >= _MAX_LOGGED_UA_FINGERPRINTS:
        return
    _logged_ua_fingerprints.add(ua_fingerprint)
    import logging
    logging.getLogger(__name__).info(
        "[UA_FINGERPRINT] %s -> %r", ua_fingerprint, user_agent
    )


def detect_client_from_user_agent(user_agent: str) -> Optional[str]:
    """
    Detect client type from User-Agent string.

    Used for auto-generating meaningful structured_id (e.g., "cursor_20251226").

    Claude-family clients are disambiguated: Claude Code CLI, Claude Desktop app,
    and the generic Anthropic-SDK / unknown-Claude bucket all have distinct
    labels. Previously every "claude"-bearing UA was labeled "claude_desktop",
    which mislabeled Claude Code sessions, Python SDK scripts, and anything else
    using the anthropic libraries.

    Args:
        user_agent: HTTP User-Agent header value

    Returns:
        Client hint string or None if not detected
    """
    if not user_agent:
        return None

    ua = user_agent.lower()

    if "cursor" in ua:
        return "cursor"
    # Prefer OpenAI/Codex detection before Claude in mixed/proxy UAs.
    if "codex" in ua or "chatgpt" in ua or "openai" in ua or "gpt" in ua:
        return "chatgpt"
    # Claude Code CLI — check before generic claude fallthrough. Claude Code
    # UAs include the literal "claude-code" slug.
    if "claude-code" in ua or "claudecode" in ua:
        return "claude_code"
    # Claude Desktop — specific match for the desktop app's UA. Historic Anthropic
    # desktop builds have used "claude desktop", "claude-desktop", and the bare
    # "Claude/<version>" format; match the first two explicitly.
    if "claude-desktop" in ua or "claude desktop" in ua:
        return "claude_desktop"
    # Generic Claude / Anthropic fallback. Covers anthropic-python, anthropic-ts,
    # custom MCP clients, and anything else carrying "claude" or "anthropic" in
    # the UA that we can't narrow further. Honest label: we know it's an
    # Anthropic-family client, we don't know which one.
    if "claude" in ua or "anthropic" in ua:
        return "claude"
    if "vscode" in ua or "visual studio code" in ua:
        return "vscode"

    return None
