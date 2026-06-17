"""SDK exception hierarchy."""

from __future__ import annotations


class GovernanceError(Exception):
    """Base exception for all SDK errors."""


class GovernanceConnectionError(GovernanceError):
    """Cannot reach governance server."""


class GovernanceTimeoutError(GovernanceError):
    """MCP call exceeded timeout (likely anyio deadlock)."""


class GovernanceUnavailableError(GovernanceError):
    """Server returned the Wave 3 §3.2 typed-unavailable response (HTTP 503
    body ``{"ok": false, "error": "governance_temporarily_unavailable",
    "retry_after_seconds": N}`` with a matching ``Retry-After`` header).

    Raised after the client's retry budget is exhausted. Carries the
    server-suggested delay so calling layers with their own scheduling
    (resident cycle loops) can back off honestly instead of guessing.
    """

    def __init__(self, message: str, retry_after_seconds: float = 5.0):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


# Pinned by docs/proposals/beam-wave-3-handler-dispatch.md §3.2 step 3; the
# server-side single source is src/mcp_transport.py::make_unavailable_body.
UNAVAILABLE_ERROR = "governance_temporarily_unavailable"

# Server-suggested delays are honored but bounded — a misconfigured or
# malicious Retry-After must not park a resident cycle indefinitely.
MAX_RETRY_AFTER_SECONDS = 30.0
DEFAULT_RETRY_AFTER_SECONDS = 5.0


def extract_retry_after_seconds(payload: object) -> float | None:
    """Return the bounded retry delay when ``payload`` is the §3.2
    typed-unavailable body, else ``None``.

    Detection keys on ``error == "governance_temporarily_unavailable"`` —
    NOT on the bare presence of ``retry_after_seconds``, which the deep
    health endpoint also uses for an unrelated warming-up shape.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("error") != UNAVAILABLE_ERROR:
        return None
    return _bound_retry_after(payload.get("retry_after_seconds"))


def parse_retry_after_header(value: object) -> float | None:
    """Parse an HTTP ``Retry-After`` header (delta-seconds form only — the
    HTTP-date form is not used by the §3.2 contract). Returns the bounded
    delay, or ``None`` when absent/unparseable."""
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return _bound_retry_after(seconds)


def _bound_retry_after(value: object) -> float:
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_SECONDS
    if seconds < 0:
        return DEFAULT_RETRY_AFTER_SECONDS
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


class IdentityDriftError(GovernanceError):
    """Agent UUID changed unexpectedly during session."""

    def __init__(self, expected_uuid: str, received_uuid: str, message: str = ""):
        self.expected_uuid = expected_uuid
        self.received_uuid = received_uuid
        super().__init__(
            message
            or f"Identity drift: expected {expected_uuid[:12]}... got {received_uuid[:12]}..."
        )


class VerdictError(GovernanceError):
    """Governance issued a pause or reject verdict."""

    def __init__(self, verdict: str, guidance: str | None = None):
        self.verdict = verdict
        self.guidance = guidance
        msg = f"Governance verdict: {verdict}"
        if guidance:
            msg += f" — {guidance}"
        super().__init__(msg)


class IdentityBootstrapRefused(GovernanceError):
    """Raised when a resident agent's anchor is missing and the agent was
    configured with refuse_fresh_onboard=True (the default for Vigil,
    Sentinel, Watcher).

    Fix: either restore the anchor from a rotation backup, or explicitly
    bootstrap a new identity by running the agent once with the
    UNITARES_FIRST_RUN=1 environment variable set. Never silently swap
    identities."""


# Single source for the #425 STRICT_IDENTITY_REQUIRED typed-refusal shape. The
# server emits it via strict_identity_refusal_payload() wrapped in
# success_response() — i.e. HTTP 200, NOT isError, NO "error" key — so neither
# the transport layer nor _raise_for_tool_failure flags it. Detection therefore
# keys on the payload's own marker field, mirroring extract_retry_after_seconds.
STRICT_IDENTITY_ROLLOUT_FLAG = "STRICT_IDENTITY_REQUIRED"


class IdentityRefusedError(GovernanceError):
    """Server refused a write because identity was not caller-proven under
    STRICT_IDENTITY_REQUIRED (#425).

    The refusal is a *structured success-shape*, not an error — by design, so
    that catch-paths don't retry-with-mint and reintroduce ghost leaks. But for
    a resident's check-in that shape is dangerous: without this detection the
    SDK reads the absent verdict as a default "proceed" and reports a SUCCESSFUL
    check-in while the server recorded nothing. A resident then goes silently
    dark — clean logs, 200 OK, zero recorded updates (Chronicler 2026-06-14:
    three days of "successful" daily runs, no EISV trajectory written).

    Raising instead makes the refusal loud: the cycle fails, the launchd log
    shows it, and notify_on_error fires. Fix the offender per the rollout doc
    (carry a caller-proven binding — echo the onboarded client_session_id or
    pass continuity_token — or add parent_agent_id / substrate exemption)."""

    def __init__(self, tool: str, hint: str | None = None, status: str | None = None):
        self.tool = tool
        self.hint = hint
        self.status = status
        msg = f"Governance refused {tool} under strict identity (status={status})"
        if hint:
            msg += f" — {hint}"
        super().__init__(msg)


def extract_identity_refusal(payload: object) -> "IdentityRefusedError | None":
    """Return an IdentityRefusedError when ``payload`` is the #425 typed
    strict-identity refusal, else ``None``.

    Keys on ``rollout_flag == STRICT_IDENTITY_REQUIRED`` — the marker the
    single-sourced server payload always sets — NOT on the bare presence of a
    ``status`` field, so a normal check-in response (decision/metrics, no
    rollout_flag) never trips it.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("rollout_flag") != STRICT_IDENTITY_ROLLOUT_FLAG:
        return None
    return IdentityRefusedError(
        tool=payload.get("tool") or "unknown",
        hint=payload.get("hint"),
        status=payload.get("status"),
    )
