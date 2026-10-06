"""Who may be handed an agent's credentials.

``identity()`` and ``onboard()`` answer with the agent's ``client_session_id``
and a signed ``continuity_token``. Either one lets the holder act as that
agent, so handing them out is itself an authentication decision: a response
that carries them must answer a caller who already proved ownership.

A request may receive an agent's credentials when, in that request:

- it created the agent (a mint);
- it presented a continuity token bound to that agent, or passed the UDS
  substrate attestation for it, on a direct UUID resume;
- a session the caller itself transmitted (``proof_origin ==
  "caller_asserted"``: its own ``client_session_id``, a verified token, an
  ``Mcp-Session-Id`` or the like) resolved to that agent through a stored
  binding, the same standard strict identity applies to a write; or
- it carries a valid operator token.

Anything else, a fingerprint pin, an onboard pin reached through a name or an
unverified ``agent_id``, an X-Agent-Id recovery, a transport-injected session,
resolves the caller only by inference, and inference must not be exchanged for
a credential: on a Docker bridge or behind a tunnel every client shares one
address, and a user agent is whatever the client sends.

Proof is recorded per agent (``credential_proof_uuid``), not per request:
the resolver records it where a caller-asserted session key resolves through
a stored binding (``note_session_proof``), PATH 0 where a matching token or a
UDS attestation proves the claimed UUID, and token rebind where a verified
token names the agent. A request-wide "caller asserted" flag would let a
caller's own session vouch for a different agent the same request resolved
by a UUID claim or a recovery path.

What this does not close: a ``client_session_id`` of the legacy shape
``agent-{uuid[:12]}`` is computable from the agent's public UUID, and a
caller who sends one counts as ``caller_asserted``. The rule cannot tell the
owner from someone who computed it; that needs an id that cannot be derived
from the UUID, which is a separate change.

The rule applies under strict identity, the default. A deployment that opted
out (``STRICT_IDENTITY_REQUIRED=false``) keeps its permissive behavior.

``UNITARES_CREDENTIAL_ISSUANCE=log`` issues as before but logs each response
the rule would have withheld (``[CREDENTIALS_WITHHELD] ... mode=log``), so an
operator can find the clients that depend on an inferred match before
enforcing. Unset, or any other value, enforces.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from src.logging_utils import get_logger

# Imported at module load, not inside credentials_issuable: operator.py binds
# get_session_signals by name when it is first imported, so a first import
# made while a caller has that function patched would keep the patch.
from .operator import is_operator_caller

logger = get_logger(__name__)

WITHHELD_HINT = (
    "This call was matched to the agent by inference (a transport fingerprint "
    "or onboard pin), which does not prove ownership, so its client_session_id "
    "and continuity_token are not returned. Pass the client_session_id your "
    "process received from start_session, or a continuity_token bound to this "
    "agent; or call start_session(force_new=true) to begin a new identity."
)


def credentials_issuable(agent_uuid: Optional[str], *, minted: bool = False) -> tuple[bool, str]:
    """(allowed, basis): may this request receive ``agent_uuid``'s credentials?"""
    if minted:
        return True, "minted"

    from src.mcp_handlers.identity_bootstrap import is_strict_identity_required

    if not is_strict_identity_required():
        return True, "permissive_posture"

    from src.mcp_handlers.context import (
        get_credential_proof_uuid,
        get_session_resolution_source,
    )

    if agent_uuid and get_credential_proof_uuid() == agent_uuid:
        return True, "proven_uuid"
    try:
        if is_operator_caller():
            return True, "operator"
    except Exception:
        pass
    basis = f"inferred:{get_session_resolution_source() or 'unknown'}"
    if issuance_mode() == "log":
        logger.warning(
            "[CREDENTIALS_WITHHELD] agent=%s... basis=%s mode=log (issued anyway)",
            (agent_uuid or "?")[:8],
            basis,
        )
        return True, f"log_only:{basis}"
    return False, basis


def issuance_mode() -> str:
    """``log`` to observe only; anything else enforces."""
    value = (os.environ.get("UNITARES_CREDENTIAL_ISSUANCE") or "").strip().lower()
    return "log" if value == "log" else "enforce"


def log_withheld(tool: str, agent_uuid: Optional[str], basis: str) -> None:
    logger.warning(
        "[CREDENTIALS_WITHHELD] %s for agent=%s... basis=%s",
        tool,
        (agent_uuid or "?")[:8],
        basis,
    )


# Session keys the server derives by inference: the IP:UA fingerprint
# ``{host}:{md5(user-agent)[:6]}`` (optionally scoped with ``|client|model``),
# the bare ``ua:{hash}`` and the stdio process key. A binding stored under one
# was made for a caller the server only inferred, and the key is low-entropy:
# anyone who knows or shares the address and User-Agent can construct it and
# send it back as an explicit session header.
_INFERRED_KEY = re.compile(r"^(?:ua:[0-9a-f]{6}|stdio:\d+|.+:[0-9a-f]{6})$")


def is_inferred_session_key(session_key: Optional[str]) -> bool:
    if not session_key:
        return True
    key = str(session_key).split("|", 1)[0]
    if _INFERRED_KEY.match(key):
        return True
    try:
        from src.mcp_handlers.context import get_session_signals

        signals = get_session_signals()
        fingerprint = getattr(signals, "ip_ua_fingerprint", None) if signals else None
    except Exception:
        fingerprint = None
    return bool(fingerprint) and key == fingerprint


def note_session_proof(agent_uuid: Optional[str], session_key: Optional[str]) -> None:
    """Record that this request's own session resolved to ``agent_uuid``.

    Called by the resolver when ``session_key`` resolves through a stored
    binding. Counts only when the key came from a signal the caller sent and
    is not a key the server derives by inference (``is_inferred_session_key``):
    sending a fingerprint back as a header proves nothing.
    """
    from src.mcp_handlers.context import get_session_proof_origin, set_credential_proof_uuid

    if (
        agent_uuid
        and get_session_proof_origin() == "caller_asserted"
        and not is_inferred_session_key(session_key)
    ):
        set_credential_proof_uuid(agent_uuid)
