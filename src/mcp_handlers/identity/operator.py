"""
Operator-tier authorization for handlers that need to disclose
cross-agent UUIDs (initially: list_agents).

Background — KG 2026-04-20T00:57:45 found that ``list_agents`` returns
every agent's UUID to any caller, enabling a two-call identity hijack
when combined with the PATH 1 ``agent-{uuid12}`` prefix-bind. The
proposed fix at ````
redacts UUIDs except for "operator-class" callers — trusted
infrastructure (Discord bridge, dashboard, ollama bridge) that
genuinely needs full visibility.

Code review of that proposal showed that ``client_session_id`` cannot
serve as the operator credential: it is a transport fingerprint
(IP:UA, MCP session header, or ``agent-{uuid12}`` prefix), not an
application-level bearer token. This module is the explicit auth
surface that the redaction PR will read from.

Design:

- Operators present an ``X-Unitares-Operator: <token>`` header.
- The header is captured into ``SessionSignals.unitares_operator_token``
  by the ASGI/HTTP layer.
- Tokens are compared against ``UNITARES_OPERATOR_TOKENS`` (csv env
  var). Empty/unset → operator status is unavailable to anyone.
- Default deny: if no token is presented or no allowlist is
  configured, ``is_operator_caller`` returns False.

Token storage:

- Tokens are deployment secrets. Recommended location:
  ``~/.config/cirwel/secrets.env`` (mode 600) per
  ``project_secrets-location`` memory.
- Bridge/dashboard/ollama plists or systemd units load the token
  into the client process and pass it on every request.
- Rotation is operator action: update the env var in
  ``UNITARES_OPERATOR_TOKENS`` and reissue tokens to clients.
- Tokens should be high-entropy random strings (e.g. ``openssl rand
  -hex 32``). Use one token per client identity, not a shared one,
  so a compromise can be revoked without disrupting all operators.
"""

import asyncio
import hashlib
import os
import time
from typing import Any, Dict, Optional, Set, Tuple

from src.logging_utils import get_logger
from src.mcp_handlers.context import SessionSignals, get_session_signals

logger = get_logger(__name__)


_OPERATOR_TOKENS_ENV = "UNITARES_OPERATOR_TOKENS"


def _allowlisted_tokens() -> Set[str]:
    """Parse the operator-token allowlist from env at call time.

    Read fresh each call rather than caching, so operators can rotate
    tokens without restarting the server. The set is small (a handful
    of entries) and the env read is cheap.
    """
    raw = os.environ.get(_OPERATOR_TOKENS_ENV, "")
    return {t.strip() for t in raw.split(",") if t.strip()}


def is_operator_caller(signals: Optional[SessionSignals] = None) -> bool:
    """Return True if the current request presents a valid operator token.

    The check is two-part:

    1. The request must carry a non-empty ``X-Unitares-Operator``
       header, captured into ``SessionSignals.unitares_operator_token``.
    2. The presented value must match a token in
       ``UNITARES_OPERATOR_TOKENS`` (csv env var).

    Both halves must hold. Empty header, missing env var, or empty
    allowlist all yield False (default deny).

    ``signals`` is optional; if omitted, we read from the contextvar
    set by the ASGI/HTTP layer. Callers in non-request contexts (e.g.
    in-process resident agents) will see ``signals=None`` and get
    False — they have other paths to operator-class data and should
    not pretend to be HTTP operators.
    """
    if signals is None:
        signals = get_session_signals()
    if signals is None:
        return False

    presented = signals.unitares_operator_token
    if not presented:
        return False

    allowlist = _allowlisted_tokens()
    if not allowlist:
        return False

    return presented in allowlist


# =============================================================================
# OPERATOR IDENTITY RESOLUTION (#425 dashboard-identity decision)
# =============================================================================
# Under STRICT_IDENTITY_REQUIRED, the REST gate keys on the RESOLVED context
# binding, never on credential presence (council finding, PR #610). For the
# dashboard's operator write actions (archive / resume / config-set /
# dialectic-request) to pass, the operator token must therefore EARN a
# resolved identity through the canonical resolver — not bypass the gate.
#
# Identity shape: one stable, persisted identity per token, keyed on a
# deterministic session_key ``operator:<sha256(token)[:16]>``. The PG-backed
# session row gives the same UUID across calls and server restarts, so
# operator writes carry consistent provenance in the audit trail. Token
# rotation mints a fresh identity for the new token; the old one goes
# quiet — attributable, never reassigned.

_OPERATOR_IDENTITY_TTL = 300.0  # seconds; bounds DB lookups, not authorization

# token fingerprint -> (agent_uuid, cached_at). Authorization is re-checked
# against the env allowlist on EVERY call before this cache is consulted, so
# rotation revokes immediately; the cache only skips re-resolution.
_operator_identity_cache: Dict[str, Tuple[str, float]] = {}

# Single-flight guard for the miss→mint path. First use of a token arrives
# as a concurrent burst (two dashboard tabs × a multi-tool sweep); without
# this, every in-flight request takes the resume-miss→force_new path and
# mints its own identity (2026-06-12: 5 mints in one burst, issue #644).
# Keyed per fingerprint so distinct tokens never serialize each other.
_operator_mint_locks: Dict[str, "asyncio.Lock"] = {}


def _operator_mint_lock(fingerprint: str) -> "asyncio.Lock":
    lock = _operator_mint_locks.get(fingerprint)
    if lock is None:
        lock = _operator_mint_locks.setdefault(fingerprint, asyncio.Lock())
    return lock


def operator_token_fingerprint(token: str) -> str:
    """Stable, non-reversible fingerprint used to key operator identity.

    The raw token never appears in session keys, logs, or the database.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def operator_session_key(token: str) -> str:
    """Deterministic session_key for an operator token.

    The ``operator:`` prefix is disjoint from every shape
    ``derive_session_key`` produces (ip:ua fingerprints, ``http:<host>:``,
    explicit header values), so operator rows cannot collide with
    transport-derived sessions.
    """
    return f"operator:{operator_token_fingerprint(token)}"


async def _persisted_operator_uuid(session_key: str) -> Optional[str]:
    """UUID named by the PG session row for ``session_key``, or None.

    Deliberately bypasses the Redis session cache: this is the
    adopt-winner ground-truth read, and the cache may hold the calling
    process's own racing mint. Best-effort — any failure returns None
    and the caller keeps its minted identity.
    """
    try:
        from src.db import get_db
        db = get_db()
        if hasattr(db, "init"):
            await db.init()
        session = await db.get_session(session_key)
        stored = session.agent_id if session else None
        # PATH 2 convention: rows may carry a UUID or a legacy model+date
        # id; operator rows are always UUID-keyed, so require that shape.
        if stored and len(stored) == 36 and stored.count("-") == 4:
            return stored
    except Exception as e:
        logger.debug(
            "[OPERATOR] adopt-winner PG readback failed for %s...: %s",
            session_key[:24], e,
        )
    return None


async def resolve_operator_identity(
    signals: Optional[SessionSignals] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve a valid operator token to a stable, persisted identity.

    Returns ``{"agent_uuid", "session_key", "source": "operator_token"}``
    when the current request carries an allowlisted ``X-Unitares-Operator``
    token, else None (default deny — same posture as ``is_operator_caller``).

    First use of a token mints the identity via the canonical resolver with
    ``spawn_reason="operator_credential"``; subsequent calls resume the same
    UUID through the PG session row (S21-a resume semantics). The result is
    memoized per token fingerprint for a short TTL — but only AFTER the
    allowlist check passes, so token rotation revokes access immediately.
    """
    if signals is None:
        signals = get_session_signals()
    if not is_operator_caller(signals):
        return None

    token = signals.unitares_operator_token
    fingerprint = operator_token_fingerprint(token)
    session_key = operator_session_key(token)

    cached = _operator_identity_cache.get(fingerprint)
    if cached and (time.monotonic() - cached[1]) < _OPERATOR_IDENTITY_TTL:
        return {
            "agent_uuid": cached[0],
            "session_key": session_key,
            "source": "operator_token",
        }

    from src.mcp_handlers.identity.handlers import resolve_session_identity

    async with _operator_mint_lock(fingerprint):
        # Re-check under the lock: a concurrent request may have resolved
        # (or minted) while this one waited.
        cached = _operator_identity_cache.get(fingerprint)
        if cached and (time.monotonic() - cached[1]) < _OPERATOR_IDENTITY_TTL:
            return {
                "agent_uuid": cached[0],
                "session_key": session_key,
                "source": "operator_token",
            }

        identity = await resolve_session_identity(
            session_key,
            persist=True,
            client_hint="operator",
            resume=True,
        )
        if (identity.get("resume_failed")
                and identity.get("error") == "session_resolve_miss"):
            # First use of this token: mint explicitly (S21-a fail-closed
            # PATH 2 means resume never silently creates). spawn_reason makes
            # the mint legible in the lineage audit trail.
            identity = await resolve_session_identity(
                session_key,
                persist=True,
                client_hint="operator",
                force_new=True,
                spawn_reason="operator_credential",
            )
            logger.info(
                "[OPERATOR] minted operator identity %s... for token fp=%s",
                (identity.get("agent_uuid") or "")[:8],
                fingerprint,
            )
            # Adopt-winner: create_session is ON CONFLICT DO NOTHING, so if
            # another writer (a second server process, or a pre-lock burst)
            # persisted the row first, OUR mint is the ghost. Read the PG row
            # DIRECTLY — resolve_session_identity(resume=True) consults the
            # Redis cache (PATH 1) first, and PATH 3 just populated it with
            # OUR mint, so the resolver can never surface a cross-process
            # winner (council finding on this fix).
            winner_uuid = await _persisted_operator_uuid(session_key)
            our_uuid = identity.get("agent_uuid")
            if winner_uuid and our_uuid and winner_uuid != our_uuid:
                logger.info(
                    "[OPERATOR] adopting persisted operator identity %s... "
                    "over our racing mint %s... for token fp=%s",
                    winner_uuid[:8],
                    our_uuid[:8],
                    fingerprint,
                )
                identity = {"agent_uuid": winner_uuid, "source": "postgres"}

    agent_uuid = identity.get("agent_uuid")
    if not agent_uuid:
        logger.warning(
            "[OPERATOR] resolver returned no agent_uuid for token fp=%s "
            "(error=%s) — treating caller as unbound",
            fingerprint,
            identity.get("error"),
        )
        return None

    _operator_identity_cache[fingerprint] = (agent_uuid, time.monotonic())
    return {
        "agent_uuid": agent_uuid,
        "session_key": session_key,
        "source": "operator_token",
    }
