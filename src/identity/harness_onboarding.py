"""Whether this server accepts identities created by a harness.

An identity is something an agent claims by calling ``start_session``. An
adapter can also create one for a session that never did. The operator of the
server owns the record those identities land in, so the operator decides
whether to accept them: ``UNITARES_HARNESS_ONBOARDING`` is ``welcome`` or
``agent_only``.

The policy applies to a call that asks for it. An adapter marks its own
identity-creating call with ``harness_request=true``; under ``agent_only`` the
server creates nothing for that call and says so. It is a control for adapters
that ask, not a security boundary: a caller that omits the flag is treated as
an agent, exactly as before. ``onboard_origin`` is not consulted. That label
stays descriptive and never affects policy (``onboard_provenance``).

Unset, the policy follows exposure. A server listening on loopback is reached
by its own operator and defaults to ``welcome``. A server listening beyond
loopback, or one that brings up its separate public listener for a tunnel,
defaults to ``agent_only`` until its operator says otherwise. The server
records what it actually bound at startup (``record_listen_exposure``), so a
``--host`` given on the command line counts. A loopback server published by a
proxy this process knows nothing about still reads as loopback: set the policy
deliberately for those.

A nested process's identity may also carry ``spawned_under``: the thread
anchor of the conversation it was started inside, as its adapter observed it.
Like ``host_session`` it is caller-declared context. It is not lineage, grants
nothing, and is never promoted to ``parent_agent_id``.
"""

from __future__ import annotations

import ipaddress
import os
import re
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Optional, cast

HarnessOnboardingPolicy = Literal["welcome", "agent_only"]
HARNESS_ONBOARDING_POLICIES = frozenset({"welcome", "agent_only"})
HARNESS_ONBOARDING_ENV = "UNITARES_HARNESS_ONBOARDING"
HARNESS_ONBOARDING_DECLINED = "harness_onboarding_declined"
SPAWNED_UNDER_SCHEMA = "unitares.spawned_under.v1"

# Used with fullmatch: `$` alone also matches before a final newline.
_ANCHOR_RE = re.compile(r"agent:[/_]thread-[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def is_loopback_host(host: object) -> bool:
    """True when a listen address reaches this machine only."""
    if not isinstance(host, str):
        return False
    value = host.strip().strip("[]").lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


# What the running server bound, recorded once at startup. None before that
# and in anything that is not the server process (tests, generators), where
# the environment's default listen address is the best available reading.
_recorded_host: Optional[str] = None
_recorded_public_listener = False


def record_listen_exposure(host: Optional[str], *, public_listener: bool = False) -> None:
    """Record the address the main listener bound and whether the public
    listener is up. Called by server startup; the default policy reads it."""
    global _recorded_host, _recorded_public_listener
    _recorded_host = host if isinstance(host, str) and host.strip() else None
    _recorded_public_listener = bool(public_listener)


def _listen_host() -> str:
    if _recorded_host is not None:
        return _recorded_host
    from src.mcp_listen_config import default_listen_host

    return default_listen_host()


def harness_onboarding_policy(
    listen_host: Optional[str] = None,
) -> tuple[HarnessOnboardingPolicy, str]:
    """Whether a harness may ask this server for an identity: ``welcome`` or ``agent_only``; unset, ``welcome`` on loopback and ``agent_only`` beyond it.

    Returns the policy and why it holds: ``explicit``, ``default_loopback`` or
    ``default_exposed``. An unrecognised setting is ignored, never guessed at.
    """
    declared = os.environ.get(HARNESS_ONBOARDING_ENV, "").strip().lower()
    if declared in HARNESS_ONBOARDING_POLICIES:
        return cast(HarnessOnboardingPolicy, declared), "explicit"
    host = listen_host if listen_host is not None else _listen_host()
    if is_loopback_host(host) and not _recorded_public_listener:
        return "welcome", "default_loopback"
    return "agent_only", "default_exposed"


def harness_onboarding_summary() -> dict[str, str]:
    """The served description of the policy, for ``GET /health``."""
    policy, basis = harness_onboarding_policy()
    return {"harness": policy, "basis": basis}


def harness_onboarding_declined_payload(tool_name: str) -> dict[str, Any]:
    """The answer to a harness call this server will not create an identity for.

    A structured success-shape, like the strict-identity refusals: an error
    response invites a caller's retry-with-mint path. ``refused`` marks it for
    a caller that does not know this contract, and there is no ``uuid``.
    """
    return {
        "refused": True,
        "status": HARNESS_ONBOARDING_DECLINED,
        "tool": tool_name,
        "policy": "agent_only",
        "hint": (
            "This server accepts identities an agent claims and does not create "
            "them for a harness. The agent can call start_session() itself. The "
            f"server's operator can accept harness-made identities with "
            f"{HARNESS_ONBOARDING_ENV}=welcome."
        ),
    }


def is_harness_onboarding_declined(payload: object) -> bool:
    """True when ``payload`` is the decline built above.

    The friendly-tool envelope reads it so a decline reaches the caller as
    itself and is not rebuilt into an ordinary onboarding response.
    """
    return (
        isinstance(payload, dict)
        and payload.get("refused") is True
        and payload.get("status") == HARNESS_ONBOARDING_DECLINED
    )


def normalize_spawned_under(
    value: Any, *, now: Optional[datetime] = None
) -> Optional[dict[str, Any]]:
    """Validate one declared ``spawned_under`` and return its stored form.

    ``None`` means nothing was declared. A malformed declaration raises
    ``ValueError``; callers treat that as "record nothing", never as a reason
    to refuse the onboard it rode in on.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("spawned_under must be an object")
    unknown = set(value) - {"anchor"}
    if unknown:
        raise ValueError(f"spawned_under has unknown fields: {', '.join(sorted(unknown))}")
    anchor = value.get("anchor")
    if not isinstance(anchor, str) or not _ANCHOR_RE.fullmatch(anchor):
        raise ValueError("spawned_under.anchor must be a thread anchor")
    return {
        "schema": SPAWNED_UNDER_SCHEMA,
        "anchor": anchor,
        "recorded_at": (now or datetime.now(timezone.utc)).isoformat(),
        "source": "caller_declared",
        "is_lineage": False,
    }
