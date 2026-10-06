"""Keyed stable session ids: an agent's client_session_id that cannot be derived.

The stable session id used to be ``agent-{uuid[:12]}``, a pure function of the
agent's public UUID: anyone who knew the UUID could compute it and present it
as the agent. It is now

    agent-{uuid[:12]}-{tag}
    tag = base32(HMAC-SHA256(K, "unitares.csid.v2|" + uuid))[:20], lowercase

with ``K = HMAC-SHA256(continuity key, "unitares.csid.v1")``, so the server's own
continuity key (src/continuity_secret.py) is the only key, and rotating it
rotates every id. The id stays deterministic per agent, so every site that
recomputes "this agent's own key" keeps working, and it keeps the ``agent-``
prefix and ``key[6:18]`` as the display prefix.

A keyed id authenticates itself. The resolver accepts one when exactly one
identity that is not deleted has that uuid prefix and the tag verifies for it
(``resolve_keyed``), with no stored binding needed and nothing written, so a
resident that resumed by token or UDS attestation and received its id keeps
working however its bindings expire; an archived identity resolves as archived,
so onboard's reactivation still works. A forged tag, an ambiguous prefix or a
deleted identity is a terminal refusal, never a fall-through to other lookups.

Legacy ids (exactly ``agent-{uuid[:12]}``) follow ``UNITARES_LEGACY_SESSION_IDS``:
``refuse`` (the default), ``log`` (accept and log ``[LEGACY_SESSION_ID]``, for an
operator's migration window) or ``accept``. Refusal applies only when this
server has a key; without one it still issues legacy ids itself and must
accept them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import time
from typing import Any, Dict, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)

_KEYED = re.compile(r"^agent-([0-9a-f]{8}-[0-9a-f]{3})-([a-z2-7]{20})$")
_LEGACY = re.compile(r"^agent-([0-9a-f]{8}-[0-9a-f]{3})$")
_TAG_DOMAIN = b"unitares.csid.v2|"
_KEY_DOMAIN = b"unitares.csid.v1"
LEGACY_MODE_ENV = "UNITARES_LEGACY_SESSION_IDS"

# Resolution result cache: key -> (agent_uuid, expires_at). It saves the
# prefix search only; a hit still re-checks the tag and the identity's status
# (resolve_keyed). The TTL bounds how long a newly ambiguous prefix goes unseen.
_VERIFIED_TTL_S = 60.0
_VERIFIED_MAX = 10_000
_verified: Dict[str, tuple[str, float]] = {}
_logged_no_key = False


def csid_key() -> Optional[bytes]:
    """The tag key, derived from the continuity key, or None without one."""
    from src.continuity_secret import resolve

    resolved = resolve()
    if resolved is None:
        return None
    return hmac.new(resolved[0], _KEY_DOMAIN, hashlib.sha256).digest()


def _tag(key: bytes, agent_uuid: str) -> str:
    digest = hmac.new(key, _TAG_DOMAIN + str(agent_uuid).lower().encode(), hashlib.sha256).digest()
    return base64.b32encode(digest).decode().lower()[:20]


def keyed_session_id(agent_uuid: str) -> Optional[str]:
    """This agent's keyed id, or None when the server has no continuity key."""
    key = csid_key()
    if key is None:
        global _logged_no_key
        if not _logged_no_key:
            _logged_no_key = True
            logger.warning(
                "[STABLE_SESSION] no continuity key: issuing legacy agent-{uuid12} "
                "session ids, which anyone who knows a UUID can compute. Set "
                "UNITARES_CONTINUITY_TOKEN_SECRET or let the server generate its key."
            )
        return None
    uuid = str(agent_uuid).lower()
    return f"agent-{uuid[:12]}-{_tag(key, uuid)}"


def classify(session_key: Optional[str]) -> str:
    """``keyed``, ``legacy`` or ``other``."""
    if not session_key:
        return "other"
    if _KEYED.match(session_key):
        return "keyed"
    if _LEGACY.match(session_key):
        return "legacy"
    return "other"


def verifies_for(session_key: str, agent_uuid: Optional[str]) -> bool:
    """Whether ``session_key`` is ``agent_uuid``'s keyed id."""
    match = _KEYED.match(session_key or "")
    if not match or not agent_uuid:
        return False
    uuid = str(agent_uuid).lower()
    if uuid[:12] != match.group(1):
        return False
    key = csid_key()
    if key is None:
        return False
    return hmac.compare_digest(match.group(2), _tag(key, uuid))


def legacy_mode() -> str:
    """How presented legacy agent-{uuid12} ids are treated: refuse, log or accept."""
    value = (os.environ.get(LEGACY_MODE_ENV, "refuse") or "").strip().lower()
    return value if value in ("accept", "log", "refuse") else "refuse"


def legacy_refused(session_key: str) -> bool:
    """Apply the legacy-id policy to a presented legacy key; True to refuse."""
    if classify(session_key) != "legacy":
        return False
    if csid_key() is None:
        return False  # this server still issues legacy ids itself
    mode = legacy_mode()
    if mode == "accept":
        return False
    logger.warning(
        "[LEGACY_SESSION_ID] %s session id agent-%s... (mode=%s)",
        "refused legacy" if mode == "refuse" else "accepted legacy",
        session_key[6:14],
        mode,
    )
    return mode == "refuse"


def refusal(reason: str) -> Dict[str, Any]:
    """The resolver's terminal refusal for a stable session id."""
    hints = {
        "legacy_session_id": (
            "This server no longer accepts agent-{uuid12} session ids, which anyone "
            "who knows the UUID can compute. Rebind with "
            "identity(agent_uuid=..., continuity_token=..., resume=true) to receive "
            "your current client_session_id, or call start_session(force_new=true)."
        ),
        "tag_mismatch": "This client_session_id is not valid for any agent.",
        "ambiguous_prefix": (
            "This client_session_id's uuid prefix matches more than one agent; "
            "rebind with your continuity_token or start a new session."
        ),
        "no_such_agent": "No active agent owns this client_session_id.",
        "agent_deleted": "The agent this client_session_id names was deleted.",
        "lookup_unavailable": (
            "The identity store is unavailable and this session has no verified "
            "binding to fall back on; retry shortly."
        ),
    }
    return {
        "resume_failed": True,
        "error": "stable_session_id_rejected",
        "reason": reason,
        "message": hints.get(reason, "This client_session_id is not accepted."),
    }


async def _candidates(prefix: str) -> list[Dict[str, Any]]:
    from src.db import get_db

    db = get_db()
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT agent_id, status, disabled_at FROM core.identities "
            "WHERE agent_id LIKE $1 AND status <> 'deleted' LIMIT 2",
            prefix + "%",
        )
    return [dict(r) for r in rows]


async def _status(agent_uuid: str) -> Optional[str]:
    """The identity's status by primary key, or None when it has no row."""
    from src.db import get_db

    db = get_db()
    async with db.acquire() as conn:
        return await conn.fetchval(
            "SELECT status FROM core.identities WHERE agent_id = $1", agent_uuid,
        )


async def resolve_keyed(session_key: str) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    """(agent_uuid, None) for a valid keyed id, else (None, refusal)."""
    match = _KEYED.match(session_key or "")
    if not match:
        return None, refusal("tag_mismatch")
    cached = _verified.get(session_key)
    # The cache saves the prefix search, not the checks: on a hit the tag is
    # re-verified, so a rotated continuity key revokes the id at once, and the
    # identity's status is re-read by primary key, so a deletion (in this or
    # any other server process) refuses the id at once.
    if cached and cached[1] > time.monotonic() and verifies_for(session_key, cached[0]):
        try:
            status = await _status(cached[0])
        except Exception as e:
            # Store unavailable: the verification is under a minute old.
            logger.warning("[STABLE_SESSION] status recheck failed (%s); using cache", type(e).__name__)
            return cached[0], None
        if status is not None and status != "deleted":
            return cached[0], None
        _verified.pop(session_key, None)
        return None, refusal("agent_deleted" if status == "deleted" else "no_such_agent")
    rows = await _candidates(match.group(1))
    if not rows:
        return None, refusal("no_such_agent")
    if len(rows) > 1:
        logger.warning("[STABLE_SESSION] ambiguous uuid prefix %s for a keyed id", match.group(1)[:8])
        return None, refusal("ambiguous_prefix")
    agent_uuid = str(rows[0]["agent_id"])
    if not verifies_for(session_key, agent_uuid):
        return None, refusal("tag_mismatch")
    # _candidates leaves deleted identities out (so one cannot make a live
    # agent's prefix ambiguous): a deleted agent's id was refused above as
    # no_such_agent. Archiving is reversible (onboard with resume reactivates
    # the same identity), so an archived one resolves and is reported archived.
    if len(_verified) >= _VERIFIED_MAX:
        now = time.monotonic()
        for k in [k for k, (_, exp) in _verified.items() if exp <= now]:
            _verified.pop(k, None)
        if len(_verified) >= _VERIFIED_MAX:
            _verified.clear()  # still full of live entries: start over
    _verified[session_key] = (agent_uuid, time.monotonic() + _VERIFIED_TTL_S)
    return agent_uuid, None


def audit_reference(session_id: Optional[str]) -> Optional[str]:
    """What audit.events stores for a session id.

    A keyed id is a bearer credential that audit rows would otherwise hand to
    every caller who can read them (observe audit_events), so it is stored as
    a stable digest that still correlates events of one session. Other values
    are stored as given: a legacy id is derivable from the UUID anyway.
    """
    if session_id and classify(session_id) == "keyed":
        return "csid:" + hashlib.sha256(session_id.encode()).hexdigest()[:24]
    return session_id


def redact_provenance(provenance: Any) -> Any:
    """A copy of a discovery's provenance safe to show other readers.

    Writers store ``writer_session_id_at_write`` as an audit reference, but a
    row written before that (or imported) may hold a raw keyed id.
    """
    if not isinstance(provenance, dict):
        return provenance
    session = provenance.get("writer_session_id_at_write")
    if not session or audit_reference(session) == session:
        return provenance
    return {**provenance, "writer_session_id_at_write": audit_reference(session)}


def forget_verified() -> None:
    """Drop cached verifications (tests, key rotation)."""
    _verified.clear()
