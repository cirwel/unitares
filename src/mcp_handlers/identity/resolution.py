"""
Core identity resolution logic.

Houses resolve_session_identity and agent ID generation. Name-claim
resolution (resolve_by_name_claim) was removed 2026-04-17 — `name` is a
cosmetic label, not an identity key.
"""

from __future__ import annotations

from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import uuid
import re

from src.logging_utils import get_logger
from src.db import get_db
from .persistence import (
    _get_redis,
    _cache_session,
    _agent_exists_in_postgres,
    _get_agent_label,
    _get_agent_status,
    _get_agent_id_from_metadata,
)

from config.governance_config import GovernanceConfig

logger = get_logger(__name__)


def _created_identity_outcome(*, force_new: bool, spawn_reason: Optional[str]) -> str:
    """Classify a successful PATH 3 mint separately from the input lane."""
    if spawn_reason in {"dispatch_auto_mint", "auto_onboard_no_session"}:
        return "minted_after_resume_miss"
    if force_new:
        return "minted_force_new"
    return "minted_fresh"


def _audit_session_resolve_miss(
    *,
    session_key: str,
    reason: str,
    resume: bool,
    force_new: bool,
    token_agent_uuid: Optional[str],
    client_hint: Optional[str],
    model_type: Optional[str],
) -> None:
    try:
        from src.audit_log import audit_logger as _audit
        try:
            from ..context import get_session_resolution_source
            resolution_source = get_session_resolution_source()
        except Exception:
            resolution_source = None
        _audit.log_session_resolve_miss_observed(
            session_key=session_key,
            resolution_source=resolution_source,
            reason=reason,
            resume=resume,
            force_new=force_new,
            token_agent_uuid_present=bool(token_agent_uuid),
            client_hint=client_hint,
            model_type=model_type,
        )
    except Exception as e:
        logger.debug(f"[PATH2_RESUME_MISS] audit write failed (non-fatal): {e}")


# =============================================================================
# AGENT ID GENERATION (model+date format)
# =============================================================================

_CLIENT_HINT_SHAPE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def _is_valid_client_hint(client_hint: Optional[str]) -> bool:
    """Validate client_hint is a short identifier, not free text.

    `client_hint` is meant to identify the client *type* (cursor, vscode,
    claude_desktop). Free-form descriptions ("Anthropic Claude, mobile app,
    dogfooding UX review") must not become identifier fragments — descriptors
    are not handles.
    """
    if not client_hint:
        return False
    stripped = client_hint.strip()
    if stripped.lower() in ("unknown", "mcp", ""):
        return False
    return bool(_CLIENT_HINT_SHAPE.match(stripped))


def _client_differs_from_model(client_hint: str, model_type: str) -> bool:
    """True if the client is a third-party wrapper, not native to the model vendor.

    Prevents e.g. Cursor using Claude from being labeled as 'Claude_Opus_20260313'
    instead of 'Cursor_Claude_Opus_20260313'.
    """
    hint = client_hint.lower()
    model = model_type.lower()

    if hint in ("unknown", ""):
        return False

    # Claude/Anthropic models: native clients contain "claude" or "anthropic"
    if "claude" in model or "anthropic" in model:
        return "claude" not in hint and "anthropic" not in hint

    # Gemini models: native clients contain "gemini" or "google"
    if "gemini" in model or "google" in model:
        return "gemini" not in hint and "google" not in hint

    # GPT/OpenAI models: native clients contain "chatgpt" or "openai"
    if "gpt" in model or "openai" in model:
        return "chatgpt" not in hint and "openai" not in hint

    return False


def _generate_agent_id(model_type: Optional[str] = None, client_hint: Optional[str] = None) -> str:
    """
    Generate agent_id in model+client+date format.

    When a third-party client uses a model from a different vendor,
    the client is prefixed to prevent identity confusion:
        ("claude-opus-4-5", "cursor") -> "Cursor_Claude_Opus_4_5_20251227"
        ("claude-opus-4-5", "claude_desktop") -> "Claude_Opus_4_5_20251227"

    Priority:
    1. model_type with third-party client -> "Client_Model_20251227"
    2. model_type alone -> "Model_20251227"
    3. client_hint -> "client_20251227"
    4. fallback -> "anon_20251227"

    Args:
        model_type: Model identifier (e.g., "claude-opus-4-5", "gemini-pro")
        client_hint: Client identifier (e.g., "cursor", "vscode", "claude_desktop")

    Returns:
        Human-readable agent_id string
    """
    timestamp = datetime.now().strftime("%Y%m%d")

    # Reject free-text client_hint before it can leak into the identifier.
    # Descriptors are not handles. Invalid shapes fall through as if no hint
    # was given.
    valid_hint = _is_valid_client_hint(client_hint)

    if model_type:
        # Normalize and format model name
        model = model_type.strip()
        # Capitalize first letter of each word, replace separators with underscore
        model = model.replace("-", " ").replace(".", " ").replace("_", " ")
        model = "_".join(word.capitalize() for word in model.split())

        # Third-party clients get prefixed to prevent identity confusion
        if valid_hint and _client_differs_from_model(client_hint, model_type):
            client = client_hint.strip().replace("_", " ").replace("-", " ")
            client = "_".join(word.capitalize() for word in client.split())
            return f"{client}_{model}_{timestamp}"

        return f"{model}_{timestamp}"
    elif valid_hint:
        # Use client as fallback identifier (validator already restricts to
        # [A-Za-z0-9_-], so .strip().lower() is sufficient).
        client = client_hint.strip().lower()
        return f"{client}_{timestamp}"
    else:
        # Anonymous fallback. MUST NOT use a reserved prefix — the historical
        # f"mcp_{timestamp}" mint collided with validators.
        # validate_agent_id_reserved_names (RESERVED_PREFIXES includes 'mcp_'),
        # so every anonymous session's tool calls were rejected with
        # error_type=reserved_prefix. Invisible while tool_usage.success was
        # hardcoded-true; surfaced 2026-06-10 after PR #543 made recording
        # honest (~22-26k failure rows/day, all from auto-minted callers).
        # The mint↔gate coupling is pinned by tests/test_identity_core.py::
        # TestGenerateAgentId::test_auto_minted_names_pass_reserved_names_gate.
        return f"anon_{timestamp}"


def _generate_auto_label(model_type: Optional[str] = None, client_hint: Optional[str] = None) -> Optional[str]:
    """
    Generate a stable, deterministic label from client signals.

    Unlike _generate_agent_id (which includes date), this produces a
    time-independent label so repeated sessions converge to one identity.

    Examples:
        ("claude-opus-4", "claude-code") -> "claude-code-opus"
        ("claude-sonnet-4", None) -> "sonnet"
        (None, "cursor") -> "cursor"
        (None, None) -> None (can't generate)
    """
    parts = []

    # Client type first (if meaningful — same shape gate as _generate_agent_id)
    if _is_valid_client_hint(client_hint):
        parts.append(client_hint.strip().lower())

    # Model family (extract short name, drop version numbers)
    if model_type:
        model = model_type.strip().lower()
        # Extract model family: "claude-opus-4-5" -> "opus"
        for family in ["opus", "sonnet", "haiku"]:
            if family in model:
                parts.append(family)
                break
        else:
            # Non-Claude model: use full name
            parts.append(model.replace(" ", "-"))

    if not parts:
        return None

    return "-".join(parts)


def _normalize_model_type(model_type: str) -> str:
    """Normalize model_type to a canonical family name for session key suffixing.

    Used by handle_identity_adapter and handle_onboard_v2 to prevent identity
    collision across different models from the same transport.

    Examples:
        "claude-opus-4-5" -> "claude"
        "gemini-pro" -> "gemini"
        "gpt-4o" -> "gpt"
    """
    normalized = model_type.lower().replace("-", "_").replace(".", "_")
    if "claude" in normalized:
        return "claude"
    elif "gemini" in normalized:
        return "gemini"
    elif "gpt" in normalized or "chatgpt" in normalized:
        return "gpt"
    elif "composer" in normalized or "cursor" in normalized:
        return "composer"
    elif "llama" in normalized:
        return "llama"
    return normalized


# =============================================================================
# SOFT TRAJECTORY VERIFICATION (v2.8)
# =============================================================================

async def _soft_verify_trajectory(
    agent_uuid: str,
    trajectory_signature: Optional[dict],
    source: str,
) -> Dict[str, Any]:
    """Soft trajectory verification for PATH 1/2 resumption.

    Checks whether a resumed identity can be verified via trajectory signature.
    Fail-open: exceptions or missing data never block resumption.

    Returns:
        {"verified": bool, "warning": str|None, "checked": bool}
    """
    try:
        from src.trajectory_identity import get_trajectory_status
        traj_status = await get_trajectory_status(agent_uuid)

        has_genesis = bool(
            traj_status
            and not traj_status.get("error")
            and traj_status.get("has_genesis")
        )

        if not has_genesis:
            # Nothing to verify against
            return {"verified": True, "checked": False, "warning": None}

        if not trajectory_signature or not isinstance(trajectory_signature, dict):
            logger.info(
                f"[TRAJECTORY] Soft warning on {source} resume for {agent_uuid[:8]}... — "
                f"genesis exists but no signature provided"
            )
            return {"verified": False, "checked": False, "warning": "trajectory_unverified"}

        # Signature provided — verify it
        from src.trajectory_identity import TrajectorySignature, verify_trajectory_identity
        sig = TrajectorySignature.from_dict(trajectory_signature)
        verification = await verify_trajectory_identity(agent_uuid, sig)

        if verification and not verification.get("verified", True):
            lineage_sim = verification.get("tiers", {}).get("lineage", {}).get("similarity", 1.0)
            logger.info(
                f"[TRAJECTORY] Soft verification failed on {source} resume for {agent_uuid[:8]}... "
                f"(lineage={lineage_sim:.3f})"
            )
            return {
                "verified": False,
                "checked": True,
                "warning": "trajectory_mismatch",
                "lineage_similarity": lineage_sim,
            }

        logger.debug(f"[TRAJECTORY] Soft verification passed on {source} resume for {agent_uuid[:8]}...")
        return {"verified": True, "checked": True, "warning": None}

    except Exception as e:
        logger.debug(f"[TRAJECTORY] Soft verification exception on {source} resume: {e}")
        return {"verified": True, "checked": False, "warning": None}


# =============================================================================
# CORE IDENTITY RESOLUTION (3 paths)
# =============================================================================

async def resolve_session_identity(

    session_key: str,

    persist: bool = False,

    model_type: Optional[str] = None,

    client_hint: Optional[str] = None,

    force_new: bool = False,

    trajectory_signature: Optional[dict] = None,

    resume: bool = False,

    token_agent_uuid: Optional[str] = None,

    parent_agent_id: Optional[str] = None,

    spawn_reason: Optional[str] = None,

    thread_id: Optional[str] = None,

    thread_position: Optional[int] = None,

) -> Dict[str, Any]:

    """

    Resolve session to agent identity. Optionally creates new agent in PostgreSQL.



    This is the ONLY identity resolution function. All tools use this.



    Args:

        session_key: The session identifier (from SSE connection or stdio PID)

        persist: If True, create agent in PostgreSQL. If False (default),

                 return UUID without persisting (lazy creation).

        model_type: Model identifier (e.g., "claude-opus-4", "gemini"). Used to

                    generate agent_id in model+date format.

        client_hint: Client/interface hint (e.g., "cursor", "vscode"). Used in

                     agent_id generation.

        force_new: If True, ignore existing binding and create fresh identity.

        thread_id: Thread this agent belongs to, when the caller has claimed
                   membership before persistence.

        thread_position: Node position in the thread, when already claimed.

        resume: If True, reuse existing identity from cache/DB (PATH 1/2).

                If False (default), skip to PATH 3 and create a fresh identity.
                Fingerprint match is a routing hint, not a succession claim —
                no predecessor link is recorded (see 2026-04-16 spec).



    Returns:

        {

            "agent_id": str,        # The agent's ID (model+date format, e.g., "claude_20251227")

            "agent_uuid": str,      # Internal UUID (immutable)

            "label": str | None,    # Display name (if set)

            "created": bool,        # True if newly created this call

            "persisted": bool,      # True if agent exists in PostgreSQL

            "source": str,          # "redis" | "postgres" | "created" | "memory_only"

        }

    """

    if not session_key:
        raise ValueError("session_key is required")

    # SECURITY (Feb 2026): Validate and sanitize session_key to prevent injection attacks
    # Session keys should be reasonable length and contain only safe characters
    MAX_SESSION_KEY_LENGTH = 256
    if len(session_key) > MAX_SESSION_KEY_LENGTH:
        logger.warning(f"[SECURITY] Session key too long ({len(session_key)} chars), truncating")
        session_key = session_key[:MAX_SESSION_KEY_LENGTH]

    # Sanitize: Replace potentially dangerous characters
    # Allow: alphanumeric, dash, underscore, colon, dot, at-sign (for email-like IDs)
    if not re.match(r'^[\w\-.:@]+$', session_key):
        # Contains characters outside allowed set - sanitize
        original = session_key
        session_key = re.sub(r'[^\w\-.:@]', '_', session_key)
        logger.warning(f"[SECURITY] Session key sanitized: {original[:30]}... -> {session_key[:30]}...")

    # S19 defense-in-depth: a substrate resident's copied continuity token
    # must not resume over non-UDS transport even when the token's embedded
    # session id still has a live Redis/PG binding. Check before PATH 1/2.
    if token_agent_uuid and not force_new:
        try:
            from src.mcp_handlers.context import get_session_signals
            _signals = get_session_signals()
            _peer_pid = _signals.peer_pid if _signals else None
            if _peer_pid is None:
                from src.substrate.verification import fetch_substrate_claim
                _claim = await fetch_substrate_claim(token_agent_uuid)
                if _claim is not None:
                    logger.warning(
                        "[SUBSTRATE_HTTP_REJECT] token/session resume for "
                        "substrate-anchored UUID %s... over HTTP — refusing. "
                        "Resident must connect via UNITARES_UDS_SOCKET.",
                        token_agent_uuid[:8],
                    )
                    return {
                        "resume_failed": True,
                        "error": "substrate_anchored_uuid_requires_uds",
                        "token_agent_uuid": token_agent_uuid,
                        "message": (
                            f"Agent {token_agent_uuid[:8]}... is substrate-"
                            f"anchored ({_claim.expected_launchd_label}). "
                            f"Token-based resume is not accepted over HTTP. "
                            f"Connect via the UNITARES_UDS_SOCKET path so the "
                            f"kernel can attest peer credentials. See "
 f"."
                        ),
                    }
        except Exception as exc:
            logger.warning(
                "[SUBSTRATE_HTTP_REJECT] pre-session gate raised for %s...: %s; "
                "falling through to existing resolution",
                token_agent_uuid[:8], exc, exc_info=True,
            )

    # If force_new is requested, skip lookup paths and go straight to creation

    if not force_new:

        # PATH 1: Redis cache (fast path)

        # NOTE: As of v2.5.2, we always cache UUID (the true identity).

        # The model+date agent_id is just a display label in metadata.

        redis = _get_redis()

        if redis:

            try:

                cached = await redis.get(session_key)

                if cached and cached.get("agent_id"):

                    cached_id = cached["agent_id"]

                    # Detect format: UUID (correct) vs model+date (legacy, pre-v2.5.2)

                    is_uuid = len(cached_id) == 36 and cached_id.count("-") == 4

                    if is_uuid:

                        # Correct format: cached value is UUID

                        agent_uuid = cached_id

                        # First check if display_agent_id is in cache (v2.5.2+)

                        agent_id = cached.get("display_agent_id")

                        if not agent_id:

                            # Fall back to metadata lookup

                            agent_id = await _get_agent_id_from_metadata(agent_uuid) or agent_uuid

                    else:

                        # Legacy format (pre-v2.5.2): treat as both agent_id and UUID fallback
                        # v1 identity deleted Feb 2026 — no new entries in this format

                        agent_uuid = cached_id

                        agent_id = cached_id

                    # IDENTITY HONESTY: When resume=False, don't return cached identity.
                    # Fall through to PATH 3 (create new). Fingerprint match is a routing
                    # hint only, never a succession claim — we do NOT record the cached
                    # agent as the new identity's predecessor.
                    # See docs/specs/2026-04-16-sever-fingerprint-eisv-inheritance-design.md
                    if resume:

                        # PATH 1 token ownership cross-check (issue #110,
                        # 2026-04-23). When the caller provided a signed
                        # continuity_token whose aid claim doesn't match the
                        # cached UUID, the cache entry is someone else's
                        # binding — not the legitimate owner's. Unlike
                        # fingerprint (soft heuristic), token mismatch is
                        # cryptographic proof of a different owner. Fall
                        # through to PATH 2.8 so token_rebind can take over.
                        if token_agent_uuid and token_agent_uuid != agent_uuid:
                            logger.warning(
                                "[PATH1_TOKEN_MISMATCH] session_key=%s... "
                                "cached_uuid=%s... token_uuid=%s... — "
                                "cache hijacked, deferring to token rebind",
                                session_key[:20],
                                agent_uuid[:8],
                                token_agent_uuid[:8],
                            )
                            try:
                                from .handlers import _broadcaster
                                _b = _broadcaster()
                                if _b is not None:
                                    await _b.broadcast_event(
                                        event_type="identity_hijack_suspected",
                                        agent_id=agent_uuid,
                                        payload={
                                            "path": "path1_token_mismatch",
                                            "source": "path1_token_mismatch",
                                            "cached_uuid_prefix": agent_uuid[:8],
                                            "token_uuid_prefix": token_agent_uuid[:8],
                                        },
                                    )
                            except Exception as _be:
                                logger.warning(
                                    f"[PATH1_TOKEN_MISMATCH] broadcast failed: {_be}"
                                )
                            resume = False

                        # PATH 1 fingerprint cross-check (2026-04-20 council follow-up
                        # to identity-honesty Part C). Session IDs of form
                        # `agent-{uuid[:12]}` are UUID-derivable; PATH 1 resume by
                        # session_id alone has no ownership proof. Compare the
                        # binding-time fingerprint (written by _cache_session) against
                        # the current request's fingerprint. Mismatch fires
                        # identity_hijack_suspected with path="path1_session_id" and,
                        # in strict mode, falls through to a fresh session.
                        #
                        # #802 per-path hardening: the `agent-` prefix shape also
                        # honors UNITARES_PREFIX_BIND_FINGERPRINT, which may be set
                        # stricter than the global default. Under that per-path mode an
                        # ABSENT binding-time OR current fingerprint is itself
                        # non-authorizing for the prefix shape (a UUID-derivable key
                        # with no recorded fingerprint has no ownership proof — the
                        # bind_ip_ua-absent hole). The per-path mode is scoped to the
                        # prefix shape, so non-prefix keys and the default-off posture
                        # stay byte-identical to prior behavior. Closes only the
                        # cross-fingerprint hijack; same-fingerprint co-residents still
                        # pass (residual needs the substrate/UDS path — see #802).
                        from config.governance_config import (
                            session_fingerprint_check_mode,
                            prefix_bind_fingerprint_mode,
                        )
                        _is_prefix_key = session_key.startswith("agent-")
                        _global_fp_mode = session_fingerprint_check_mode()
                        _prefix_fp_mode = (
                            prefix_bind_fingerprint_mode() if _is_prefix_key else "off"
                        )
                        _FP_RANK = {"off": 0, "log": 1, "strict": 2}
                        _fp_mode = _global_fp_mode
                        if _FP_RANK.get(_prefix_fp_mode, 0) > _FP_RANK.get(_global_fp_mode, 0):
                            _fp_mode = _prefix_fp_mode
                        # The absent-fingerprint branches fire ONLY when the per-path
                        # flag is active (>=log). The global check must never penalize
                        # a missing fingerprint — that is the legacy-cache backward-
                        # compat contract (test_legacy_cache_without_bind_fp_no_event):
                        # strict-mode promotion must not retroactively invalidate every
                        # pre-existing session that predates the bind_ip_ua field.
                        _prefix_active = _is_prefix_key and _prefix_fp_mode != "off"

                        if _fp_mode != "off":
                            cached_bind_fp = cached.get("bind_ip_ua")
                            try:
                                from ..context import get_session_signals
                                _sig = get_session_signals()
                                current_fp = getattr(_sig, "ip_ua_fingerprint", None) if _sig else None
                            except Exception:
                                current_fp = None

                            _violation = None
                            if cached_bind_fp and current_fp and current_fp != cached_bind_fp:
                                _violation = "fingerprint_mismatch"
                            elif _prefix_active and not cached_bind_fp:
                                _violation = "no_bind_fingerprint"
                            elif _prefix_active and not current_fp:
                                _violation = "no_current_fingerprint"

                            if _violation:
                                logger.warning(
                                    "[PATH1_FINGERPRINT_MISMATCH] session_key=%s... "
                                    "bound_fp=%s current_fp=%s reason=%s — suspected "
                                    "hijack of agent=%s... (mode=%s)",
                                    session_key[:20],
                                    (cached_bind_fp or "<none>")[:16],
                                    (current_fp or "<none>")[:16],
                                    _violation,
                                    agent_uuid[:8],
                                    _fp_mode,
                                )
                                try:
                                    from .handlers import _broadcaster
                                    _b = _broadcaster()
                                    if _b is not None:
                                        await _b.broadcast_event(
                                            event_type="identity_hijack_suspected",
                                            agent_id=agent_uuid,
                                            payload={
                                                "path": "path1_session_id",
                                                "mode": _fp_mode,
                                                "source": "path1_fingerprint_mismatch",
                                                "reason": _violation,
                                                "prefix_scoped": _prefix_active,
                                                "bind_fp_prefix": (cached_bind_fp or "")[:8],
                                                "current_fp_prefix": (current_fp or "")[:8],
                                            },
                                        )
                                except Exception as _be:
                                    logger.warning(
                                        f"[PATH1_FINGERPRINT_MISMATCH] broadcast failed: {_be}"
                                    )
                                if _fp_mode == "strict":
                                    # Fall through to PATH 3 (fresh session).
                                    # Do NOT delete the cache entry — the
                                    # legitimate owner can still resume from
                                    # the correct fingerprint.
                                    resume = False

                        # If strict-mode fingerprint mismatch set resume=False
                        # above, skip the cached-resume return and fall through
                        # to PATH 2/3 so a fresh binding is established under
                        # the current fingerprint.
                        if not resume:
                            pass  # drops out of the `if cached ...:` block; PATH 2 continues below
                        else:
                            # Check if persisted in PostgreSQL

                            persisted = await _agent_exists_in_postgres(agent_uuid)

                            # Fetch label (DB first, then Redis cache fallback)

                            label = await _get_agent_label(agent_uuid) if persisted else None
                            if not label:
                                label = cached.get("label")

                            # Check archived status (prevents silent binding to archived agents)
                            agent_status = await _get_agent_status(agent_uuid) if persisted else None
                            is_archived = agent_status == "archived"

                            # Soft trajectory verification (v2.8)
                            traj_result = await _soft_verify_trajectory(agent_uuid, trajectory_signature, "redis")

                            # SLIDING TTL: Refresh Redis expiry on every hit (v2.5.5)

                            try:

                                from src.cache.redis_client import get_redis

                                raw_redis = await get_redis()

                                if raw_redis:
                                    await raw_redis.expire(f"session:{session_key}", GovernanceConfig.SESSION_TTL_SECONDS)

                            except Exception:

                                pass



                            return {

                                "agent_id": agent_id,   # Human-readable (model+date). UUID for lookup is agent_uuid.
                                "public_agent_id": agent_id,

                                "agent_uuid": agent_uuid,

                                "display_name": label,

                                "label": label,  # backward compat

                                "created": False,

                                "persisted": persisted,

                                "archived": is_archived,
                                "core_agent_row_status": agent_status,

                                "source": "redis",
                                "identity_resolution_outcome": "resumed",

                                "trajectory_verified": traj_result.get("verified"),

                                "trajectory_warning": traj_result.get("warning"),

                            }

            except Exception as e:
                # INFO level (v2.5.7): Redis lookup failures are recoverable but should be visible
                logger.info(f"Redis lookup failed for session {session_key[:20]}...: {e}")



        # PATH 2: PostgreSQL session lookup

        # NOTE: As of v2.5.2, sessions reference agents by UUID.

        # IDENTITY HONESTY: When resume=False, don't return PG identity.
        # Fingerprint match is a routing hint only, never a succession claim.
        # See docs/specs/2026-04-16-sever-fingerprint-eisv-inheritance-design.md
        try:

            db = get_db()

            if hasattr(db, "init"):

                await db.init()



            session = await db.get_session(session_key)

            # PATH 2 fail-closed (S21-a, 2026-04-27): when caller asked to
            # resume but PG has no session row, return MISS instead of
            # falling through to PATH 3 and silently minting a ghost. Per
            # identity.md design principle (KG 2026-04-06T02:34:27.323998):
            # "resolve to existing identity or fail explicitly, never
            # silently create a fork." Without this guard, every explicit-
            # client_session_id resume on a fresh process minted a ghost
            # AND ratified it into Redis, producing the chronic fleet-wide
            # ghost-fork rate documented in S21.
            #
            # Carve-out: when the caller passed a continuity_token, defer
            # to PATH 2.8's token-rebind / substrate-anchored gate below.
            # Failing closed here would short-circuit substrate UDS-only
            # rejection and the legitimate token-rebind recovery path.
            if resume and not (session and session.agent_id) and not token_agent_uuid:
                logger.info(
                    "[PATH2_RESUME_MISS] session_key=%s... no PG session "
                    "row, refusing silent PATH 3 fall-through (S21-a)",
                    session_key[:20],
                )
                _audit_session_resolve_miss(
                    session_key=session_key,
                    reason="pg_session_missing",
                    resume=resume,
                    force_new=force_new,
                    token_agent_uuid=token_agent_uuid,
                    client_hint=client_hint,
                    model_type=model_type,
                )
                return {
                    "resume_failed": True,
                    "error": "session_resolve_miss",
                    "session_key": session_key,
                    "message": (
                        f"No active session binding for "
                        f"session_key={session_key[:20]}.... Pass "
                        f"force_new=true to mint a fresh identity, or "
                        f"onboard with declared parent_agent_id."
                    ),
                }

            if session and session.agent_id and resume:

                stored_id = session.agent_id

                # Detect format: UUID (correct) vs model+date (legacy)

                is_uuid = len(stored_id) == 36 and stored_id.count("-") == 4

                if is_uuid:

                    agent_uuid = stored_id

                    # Fetch agent_id (model+date) from metadata

                    agent_id = await _get_agent_id_from_metadata(agent_uuid) or agent_uuid

                else:

                    # Legacy format (pre-v2.5.2): treat as both agent_id and UUID fallback

                    agent_uuid = stored_id

                    agent_id = stored_id

                label = await _get_agent_label(agent_uuid)

                # Check archived status
                agent_status = await _get_agent_status(agent_uuid)
                is_archived = agent_status == "archived"

                # Soft trajectory verification (v2.8)
                traj_result = await _soft_verify_trajectory(agent_uuid, trajectory_signature, "postgres")

                # Warm Redis cache for next time (cache UUID + display agent_id)

                # This also resets the TTL to 24h (sliding window)

                await _cache_session(
                    session_key, agent_uuid, display_agent_id=agent_id,
                    trajectory_required=traj_result.get("checked", False),
                )



                # Update DB last_active (non-blocking best effort)

                try:

                    await db.update_session_activity(session_key)

                except Exception:

                    pass



                return {

                    "agent_id": agent_id,   # Human-readable (model+date). UUID for lookup is agent_uuid.
                    "public_agent_id": agent_id,

                    "agent_uuid": agent_uuid,

                    "display_name": label,

                    "label": label,  # backward compat

                    "created": False,

                    "persisted": True,  # Found in PostgreSQL = persisted

                    "archived": is_archived,
                    "core_agent_row_status": agent_status,

                    "source": "postgres",
                    "identity_resolution_outcome": "resumed",

                    "trajectory_verified": traj_result.get("verified"),

                    "trajectory_warning": traj_result.get("warning"),

                }

        except Exception as e:

            # S21-a: promoted from logger.debug — silent fall-throughs were
            # hiding anyio-asyncio deadlocks and other PG hiccups, after
            # which PATH 3 would mint a ghost. Make the failure legible.
            logger.warning(f"[PATH2_DB_FAIL] PostgreSQL session lookup failed: {e}")

            # PATH 2 fail-closed on exception too — same reasoning as the
            # no-row branch above. If we couldn't read the session table,
            # we can't claim resume succeeded; mint requires explicit
            # force_new=True from the caller. Same token-bearing carve-out
            # as the no-row branch — let PATH 2.8 try the rebind.
            if resume and not token_agent_uuid:
                _audit_session_resolve_miss(
                    session_key=session_key,
                    reason="pg_lookup_exception",
                    resume=resume,
                    force_new=force_new,
                    token_agent_uuid=token_agent_uuid,
                    client_hint=client_hint,
                    model_type=model_type,
                )
                return {
                    "resume_failed": True,
                    "error": "session_resolve_miss",
                    "session_key": session_key,
                    "reason": "pg_lookup_exception",
                    "message": (
                        f"PostgreSQL session lookup failed for "
                        f"session_key={session_key[:20]}.... Pass "
                        f"force_new=true to mint a fresh identity."
                    ),
                }



    # NAME-CLAIM REMOVED (2026-04-17)
    # PATH 2.5 (explicit agent_name) and PATH 2.75 (auto-name) used to
    # resolve identity by label. They were the original source of the
    # "ghost siphon" — multiple distinct sessions colliding on one UUID
    # because they happened to send the same human-readable label. Name
    # is now strictly a cosmetic attribute stored at creation or updated
    # via set_agent_label. Identity resumption must go through a
    # cryptographic signal: PATH 0 (agent_uuid), PATH 2.8
    # (continuity_token), or explicit session binding via PATH 1/2.
    # Callers with only a label and no UUID/token get a fresh identity.
    # See project_name-claim-identity-ghost.md in operator memory.

    # PATH 2.8: Direct agent lookup from continuity token
    # The token embeds the agent UUID. If session bindings expired but the
    # agent still exists in PG, rebind the session and return — no fork.
    if token_agent_uuid and not force_new:
        # S19 PR4: substrate-anchored UUIDs cannot resume over HTTP via
        # token alone. The token is a copyable bearer; the substrate
        # commitment lives in the resident's process attestation over
        # UDS. If a core.substrate_claims row exists for this UUID AND
        # this request arrived without a kernel-attested peer PID
        # (i.e. HTTP path), refuse the resume with an explicit pointer
        # to UDS. Closes the Hermes-incident leak path in production.
        # The gate is self-scoping by the substrate_claims table —
        # non-substrate UUIDs are unaffected (no row, no rejection).
        try:
            from src.mcp_handlers.context import get_session_signals
            _signals = get_session_signals()
            _peer_pid = _signals.peer_pid if _signals else None
            if _peer_pid is None:
                from src.substrate.verification import fetch_substrate_claim
                _claim = await fetch_substrate_claim(token_agent_uuid)
                if _claim is not None:
                    logger.warning(
                        "[SUBSTRATE_HTTP_REJECT] PATH 2.8 token resume for "
                        "substrate-anchored UUID %s... over HTTP — refusing. "
                        "Resident must connect via UNITARES_UDS_SOCKET.",
                        token_agent_uuid[:8],
                    )
                    return {
                        "resume_failed": True,
                        "error": "substrate_anchored_uuid_requires_uds",
                        "token_agent_uuid": token_agent_uuid,
                        "message": (
                            f"Agent {token_agent_uuid[:8]}... is substrate-"
                            f"anchored ({_claim.expected_launchd_label}). "
                            f"Token-based resume is not accepted over HTTP. "
                            f"Connect via the UNITARES_UDS_SOCKET path so the "
                            f"kernel can attest peer credentials. See "
 f"."
                        ),
                    }
        except Exception as exc:
            # Defense-in-depth: any unexpected error in the gate falls
            # through to the existing PATH 2.8 behavior. Never default-
            # accepts AND never silently breaks the existing flow.
            logger.warning(
                "[SUBSTRATE_HTTP_REJECT] gate raised for %s...: %s; falling "
                "through to existing PATH 2.8 (HTTP path unchanged)",
                token_agent_uuid[:8], exc, exc_info=True,
            )

        try:
            if await _agent_exists_in_postgres(token_agent_uuid):
                status = await _get_agent_status(token_agent_uuid)
                if status == "active":
                    agent_id = await _get_agent_id_from_metadata(token_agent_uuid) or token_agent_uuid
                    label = await _get_agent_label(token_agent_uuid)

                    # Rebind session in Redis + PG
                    await _cache_session(session_key, token_agent_uuid, display_agent_id=agent_id)
                    try:
                        db = get_db()
                        identity = await db.get_identity(token_agent_uuid)
                        if identity:
                            await db.create_session(
                                session_id=session_key,
                                identity_id=identity.identity_id,
                                expires_at=datetime.now() + timedelta(hours=GovernanceConfig.SESSION_TTL_HOURS),
                                client_type="mcp",
                                client_info={"agent_uuid": token_agent_uuid, "rebound_from_token": True},
                            )
                    except Exception as e:
                        logger.debug(f"[TOKEN_REBIND] PG session rebind failed (non-fatal): {e}")

                    logger.info(
                        f"[TOKEN_REBIND] Rebound {token_agent_uuid[:8]}... via direct agent lookup "
                        f"(session binding had expired)"
                    )
                    return {
                        "agent_id": agent_id,
                        "public_agent_id": agent_id,
                        "agent_uuid": token_agent_uuid,
                        "display_name": label,
                        "label": label,
                        "created": False,
                        "persisted": True,
                        "core_agent_row_status": status,
                        "source": "token_rebind",
                        "identity_resolution_outcome": "resumed",
                    }
                else:
                    logger.info(f"[TOKEN_REBIND] Agent {token_agent_uuid[:8]}... is {status}, not resumable")
        except Exception as e:
            logger.warning(f"[TOKEN_REBIND] Direct agent lookup failed: {e}")

        # Token was provided but agent not found/resumable — don't silently fork
        logger.warning(
            f"[IDENTITY] resume with token but agent {token_agent_uuid[:8]}... "
            f"not found or not active — refusing to create fork"
        )
        return {
            "resume_failed": True,
            "error": "resume_failed",
            "token_agent_uuid": token_agent_uuid,
            "message": (
                f"Continuity token references agent {token_agent_uuid[:8]}... "
                f"which is not active. Use force_new=true or onboard() to create a new identity."
            ),
        }

    # PATH 3: Create new agent

    # UUID is the true identity (for lookup/persistence)
    # agent_id is human-readable label (model+date format, for display)
    agent_uuid = str(uuid.uuid4())
    agent_id = _generate_agent_id(model_type, client_hint)
    # Auto-assign a cosmetic default label from client signals. The label is
    # NOT used for lookup (name-claim removed 2026-04-17); operators can
    # override later via set_agent_label / identity(name=X), at which point
    # the explicit path's own collision rename at persistence.py:467 applies.
    #
    # Appended with the UUID prefix for operator-facing disambiguation: the
    # auto-label stem ("claude_desktop-claude", "cursor-opus") is shared by
    # every session of the same client+model, so bare labels in dashboards
    # and logs can't tell 16 concurrent "claude_desktop-claude" agents apart.
    # The suffix is purely display — UUID remains the real identity key, so
    # adding it here does not re-introduce the lookup-by-name primitive.
    label = _generate_auto_label(model_type, client_hint)
    if label:
        label = f"{label}_{agent_uuid[:8]}"

    if persist:
        # Persist immediately to PostgreSQL
        try:
            db = get_db()

            # Create agent in PostgreSQL with UUID as key (label set atomically).
            # parent_agent_id / spawn_reason are the descriptive-stance lineage
            # declaration (identity ontology v2 §Worked examples). They arrive
            # from onboard(force_new=true, parent_agent_id=..., spawn_reason=...)
            # and must be persisted or the ontology's only earned cross-process
            # signal is theater — dogfood regression 2026-04-21.
            await db.upsert_agent(
                agent_id=agent_uuid,  # UUID is the primary key
                api_key="",  # Legacy field, not used
                status="active",
                label=label,  # Set auto-label at creation time
                parent_agent_id=parent_agent_id,
                spawn_reason=spawn_reason,
                thread_id=thread_id,
                thread_position=thread_position,
            )

            if label:
                logger.info(f"[AUTO_NAME] New agent '{label}' (uuid: {agent_uuid[:8]}...)")

            # Create identity record with agent_id (display name) in metadata
            identity_metadata = {
                "source": "identity_v2",
                "created_at": datetime.now().isoformat(),
                "public_agent_id": agent_id,
                "agent_id": agent_id,  # Human-readable label (model+date)
                "model_type": model_type,
                "total_updates": 0,  # Initialize counter for persistence
            }
            if thread_id:
                identity_metadata["thread_id"] = thread_id
            if thread_position is not None:
                identity_metadata["thread_position"] = thread_position
                identity_metadata["node_index"] = thread_position
            if label:
                identity_metadata["label"] = label
            await db.upsert_identity(
                agent_id=agent_uuid,
                api_key_hash="",
                parent_agent_id=parent_agent_id,
                spawn_reason=spawn_reason,
                metadata=identity_metadata,
            )

            # S21-b §1: hydrate the in-memory dict so require_registered_agent
            # sees this UUID in the same request that minted it. Without this,
            # the caller's next tool call gets "not registered" until the next
            # bulk reload (axiom-#3 violation H14, council pass-2).
            try:
                from src.agent_metadata_persistence import register_minted_agent_in_dict
                register_minted_agent_in_dict(
                    agent_uuid,
                    status="active",
                    label=label,
                    public_agent_id=agent_id,
                    parent_agent_id=parent_agent_id,
                    spawn_reason=spawn_reason,
                    thread_id=thread_id,
                    node_index=thread_position or 1,
                )
            except Exception as e:
                logger.warning(f"Eager dict hydration failed for {agent_uuid[:8]}: {e}")

            # Create session binding
            identity = await db.get_identity(agent_uuid)
            if identity:
                await db.create_session(
                    session_id=session_key,
                    identity_id=identity.identity_id,
                    expires_at=datetime.now() + timedelta(hours=GovernanceConfig.SESSION_TTL_HOURS),
                    client_type="mcp",
                    client_info={"agent_uuid": agent_uuid, "agent_id": agent_id}
                )

            # Cache in Redis (session -> UUID + display agent_id).
            # mint_guard=True: PATH 3 must not silently overwrite an
            # existing live binding for the same session_key (S21-a).
            await _cache_session(
                session_key, agent_uuid, display_agent_id=agent_id,
                spawn_reason=spawn_reason,
                mint_guard=True,
            )

            logger.info(f"Created new agent: {agent_id} (uuid: {agent_uuid[:8]}...)")

            result = {
                "agent_id": agent_id,   # Human-readable (model+date). UUID for lookup is agent_uuid.
                "public_agent_id": agent_id,
                "agent_uuid": agent_uuid,
                "display_name": label,
                "label": label,
                "created": True,
                "persisted": True,
                "core_agent_row_status": "active",
                "source": "created",
                "spawn_reason": spawn_reason,
                "thread_id": thread_id,
                "thread_position": thread_position,
                "identity_resolution_outcome": _created_identity_outcome(
                    force_new=force_new,
                    spawn_reason=spawn_reason,
                ),
            }
            return result

        except Exception as e:
            logger.warning(f"Failed to persist new agent: {e}")
            # Fall through to memory-only path

    # Lazy creation: just cache in Redis, don't write to PostgreSQL.
    # mint_guard=True: PATH 3 lazy mint must not silently overwrite an
    # existing live binding for the same session_key (S21-a).
    await _cache_session(
        session_key, agent_uuid, display_agent_id=agent_id, label=label,
        spawn_reason=spawn_reason,
        mint_guard=True,
    )
    logger.debug(f"Created new agent (lazy): {agent_id} (uuid: {agent_uuid[:8]}...) label={label}")

    result = {
        "agent_id": agent_id,   # Human-readable (model+date). UUID for lookup is agent_uuid.
        "public_agent_id": agent_id,
        "agent_uuid": agent_uuid,
        "display_name": label,
        "label": label,
        "created": True,
        "persisted": False,
        "source": "memory_only",
        "spawn_reason": spawn_reason,
        "identity_resolution_outcome": _created_identity_outcome(
            force_new=force_new,
            spawn_reason=spawn_reason,
        ),
    }
    return result


# resolve_by_name_claim and _audit_identity_claim removed 2026-04-17.
# Name is now a cosmetic label set at agent creation or via set_agent_label;
# identity resolution goes through cryptographic signals only (UUID, token,
# or explicit session binding). See project_name-claim-identity-ghost.md.
