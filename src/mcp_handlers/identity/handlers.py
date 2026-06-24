"""
Identity V2 - Simplified Session-to-UUID Resolution

Re-export facade — functions have moved to focused modules.
Existing imports continue to work unchanged.

Modules:
  identity_session      — session key derivation, fingerprinting, pin operations
  identity_persistence  — agent persistence, caching, label management
  identity_resolution   — core identity resolution, agent ID generation
"""

from typing import Optional, Dict, Any, Sequence
from datetime import datetime, timedelta
import os
import re

from mcp.types import TextContent

from src.logging_utils import get_logger
from src.db import get_db
from ..utils import success_response, error_response
from ..decorators import mcp_tool
from ..support.coerce import coerce_bool

from config.governance_config import GovernanceConfig
from src.services.identity_payloads import (
    build_identity_diag_payload,
    build_identity_response_data,
    build_onboard_response_data,
)

logger = get_logger(__name__)

# --- identity_session (leaf) ---
from .session import (
    derive_session_key,
    _extract_base_fingerprint,
    ua_hash_from_header,
    _PIN_TTL,
    lookup_onboard_pin,
    set_onboard_pin,
    create_continuity_token,
    resolve_continuity_token,
    extract_token_agent_uuid,
    extract_token_iat,
    normalize_client_session_id,
    normalize_client_session_id_argument,
    continuity_token_support_status,
    build_token_deprecation_block,
)

# --- identity_persistence ---
from .persistence import (
    _redis_cache,
    _get_redis,
    _cache_session,
    _agent_exists_in_postgres,
    _get_agent_status,
    _get_agent_label,
    _get_agent_id_from_metadata,
    _find_agent_by_label,
    ensure_agent_persisted,
    set_agent_label,
)


def _broadcaster():
    """Lazy accessor for the shared broadcaster. Returns None when broadcaster
    isn't importable (e.g., unit tests without a live server). Mirrors the
    helper in persistence.py; kept at module level here so tests patching
    handlers._broadcaster can intercept cleanly."""
    try:
        from src.broadcaster import broadcaster as _b
        return _b
    except Exception:
        return None


def _emit_continuity_token_deprecation(
    *,
    response_dict: Dict[str, Any],
    used_token_for_resume: bool,
    token_str: Optional[str],
    agent_uuid: str,
    response_agent_id: str,
    client_hint: Optional[str] = None,
    model_type: Optional[str] = None,
) -> None:
    """S1-a grace-period surface: warn + audit when a caller resumes across
    process-instance boundaries via continuity_token.

    Mutates ``response_dict["deprecations"]`` in place (creating the list if
    absent). The audit emit is best-effort — failures are logged at debug and
    never propagate.

    Ontology: only cross-process-instance resume is the deprecating path.
    Intra-session token use (request auth on process_agent_update, mid-session
    identity() rebind) is the Part-C anti-hijack proof and stays load-bearing.
    Each caller is responsible for deciding which case it's in via
 ``used_token_for_resume``. .
    """
    if not used_token_for_resume:
        return
    issued_at = extract_token_iat(str(token_str)) if token_str else None
    dep_block = build_token_deprecation_block(
        used_token_for_resume=True,
        token_issued_at=issued_at,
    )
    if dep_block is not None:
        response_dict.setdefault("deprecations", []).append(dep_block)
    # Audit emit is independent of dep_block presence: today
    # build_token_deprecation_block always returns a block when used_token_for_resume
    # is True, but if that contract ever changes (e.g., config-disabled surface,
    # localized severity gating), grace-period telemetry must still record the
    # accept event. The two side-effects are deliberately decoupled.
    try:
        import time as _time
        from src.audit_log import audit_logger as _audit
        _audit.log_continuity_token_deprecated_accept(
            agent_id=response_agent_id,
            caller_channel=client_hint,
            caller_model_type=model_type,
            issued_at=issued_at if issued_at is not None else 0,
            accepted_at=int(_time.time()),
            agent_uuid=agent_uuid,
        )
    except Exception as _audit_err:
        logger.debug(
            f"[S1-a] deprecated-accept audit write failed (non-fatal): {_audit_err}"
        )


def _continuity_token_resume_rejected(
    *,
    tool: str,
    token_str: Optional[str],
) -> TextContent:
    """S1-c post-grace gate for the retired token-as-resume path.

    ``continuity_token`` remains a PATH 0 ownership proof when paired with an
    explicit ``agent_uuid``. It is no longer accepted as a cross-process
    resume or bind credential. Callers should mint a fresh process identity and
    declare lineage through ``parent_agent_id``.
    """
    details: Dict[str, Any] = {
        "status": "continuity_token_resume_rejected",
        "tool": tool,
        "ontology_ref": "docs/ontology/s1-continuity-token-retirement.md#s1-c-post-grace-cross-process-instance-reject",
    }
    if token_str:
        issued_at = extract_token_iat(str(token_str))
        if issued_at is not None:
            details["token_issued_at"] = issued_at
    return error_response(
        (
            "Cross-process-instance resume via continuity_token is no longer "
            "accepted. Mint a fresh process identity and declare lineage with "
            "parent_agent_id."
        ),
        details=details,
        recovery={
            "reason": "continuity_token_resume_retired",
            "action": (
                "Call onboard(force_new=true, parent_agent_id=<prior UUID>, "
                "spawn_reason='new_session') instead of resuming by token."
            ),
            "preserved_path": (
                "Same-live-process PATH 0 remains available as "
                "identity(agent_uuid=<uuid>, continuity_token=<token>, resume=true)."
            ),
        },
        error_code="CONTINUITY_TOKEN_RESUME_RETIRED",
        error_category="auth_error",
    )


async def _emit_identity_hijack_event(
    direct_uuid: str,
    mode: str,
    token_aid: Optional[str],
) -> None:
    """Surface a suspected identity hijack on the shared broadcast channel.

    Fires when PATH 0 detects a bare-UUID resume without a matching
    continuity_token (see Part C gate). Dashboards and the Discord bridge
    subscribe to this event and surface it within one broadcast cycle,
    mirroring the `resident_fork_detected` pattern (#70).
    """
    b = _broadcaster()
    if b is None:
        return
    try:
        await b.broadcast_event(
            event_type="identity_hijack_suspected",
            agent_id=direct_uuid,
            payload={
                "mode": mode,
                "proof": "matching_token" if token_aid == direct_uuid else "none",
                "token_aid_mismatch": token_aid if (token_aid and token_aid != direct_uuid) else None,
            },
        )
    except Exception as e:
        logger.warning(f"[IDENTITY_HIJACK] broadcast_event failed: {e}")


async def _path2_ipua_pin_check(
    *,
    arguments: Dict[str, Any],
    base_session_key: str,
    force_new: bool,
    resume: bool,
) -> bool:
    """PATH 2 IP:UA pin cross-check.

    `derive_session_key` step 7 resolves an unauthenticated onboard() call to
    a previously-pinned session by IP:UA fingerprint alone. Multiple same-
    family agents on one machine silently adopt the first agent's UUID.
    Observation phase emits `identity_hijack_suspected` with
    `path="path2_ipua_pin"`; strict mode additionally forces a fresh mint by
    returning resume=False. Suppressed when the caller supplied any ownership
    proof (token, explicit session id, agent_uuid) or when force_new is set
    (a deliberate fresh ask).

    Returns the (possibly-flipped) `resume` flag the caller should use.
    """
    from ..context import get_session_resolution_source
    if get_session_resolution_source() != "pinned_onboard_session":
        return resume

    has_proof = bool(
        arguments.get("continuity_token")
        or arguments.get("client_session_id")
        or arguments.get("agent_id")
        or arguments.get("agent_uuid")
    )
    if has_proof or force_new:
        return resume

    from config.governance_config import ipua_pin_check_mode
    pin_mode = ipua_pin_check_mode()
    if pin_mode == "off":
        return resume

    logger.warning(
        "[PATH2_IPUA_PIN_RESUME] onboard() with no ownership proof resolved "
        "to pinned session via IP:UA fingerprint — session_key=%s... (mode=%s)",
        str(base_session_key)[:20],
        pin_mode,
    )
    try:
        b = _broadcaster()
        if b is not None:
            await b.broadcast_event(
                event_type="identity_hijack_suspected",
                agent_id=None,
                payload={
                    "path": "path2_ipua_pin",
                    "mode": pin_mode,
                    "source": "onboard_pin_fallback",
                    "session_key_prefix": str(base_session_key)[:16],
                },
            )
    except Exception as _be:
        logger.warning(f"[PATH2_IPUA_PIN_RESUME] broadcast failed: {_be}")

    # Strict mode: force fresh mint. The pin entry is left intact so the
    # legitimate owner can still resume by presenting a continuity_token or
    # agent_uuid.
    if pin_mode == "strict":
        return False
    return resume


# --- identity_resolution ---
from .resolution import (
    _generate_agent_id,
    _generate_auto_label,
    _normalize_model_type,
    resolve_session_identity,
)
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server


async def _assign_thread_for_new_agent(
    *,
    arguments: Dict[str, Any],
    session_key: str,
    agent_uuid: Optional[str],
    parent_agent_id: Optional[str],
    spawn_reason: Optional[str],
    thread_id_hint: Optional[str],
) -> tuple[Optional[str], Optional[int], Optional[str]]:
    """Claim thread membership for a newly minted agent.

    Policy: explicit thread_id wins; otherwise a declared parent's thread is
    inherited; otherwise derive a thread from the current session key.
    """
    from src.thread_identity import generate_thread_id, infer_spawn_reason

    db = get_db()
    thread_id = thread_id_hint
    thread_source = "hint" if thread_id_hint else "session"

    if not thread_id and parent_agent_id:
        try:
            parent_thread = await db.get_agent_thread_info(parent_agent_id)
            if parent_thread and parent_thread.get("thread_id"):
                thread_id = parent_thread["thread_id"]
                thread_source = "parent"
        except Exception as e:
            logger.debug(f"[THREAD] Could not read parent thread (non-fatal): {e}")

    if not thread_id:
        thread_id = generate_thread_id(session_key)

    await db.create_or_get_thread(thread_id)
    thread_position = await db.claim_thread_position(thread_id)

    existing_nodes = await db.get_thread_nodes(thread_id)
    prior_nodes = [
        n for n in existing_nodes
        if not agent_uuid or n.get("agent_id") != agent_uuid
    ]
    if not spawn_reason:
        spawn_reason = infer_spawn_reason(arguments, prior_nodes)

    logger.info(
        "[THREAD] Assigned new-agent thread position=%s source=%s reason_present=%s",
        thread_position,
        thread_source,
        bool(spawn_reason),
    )
    return thread_id, thread_position, spawn_reason


# =============================================================================
# SYSTEM EVIDENCE HELPER (real data for onboard response)
# =============================================================================

def _get_system_evidence() -> dict:
    """Compute system activity summary from real data.

    Iterates in-memory agent_metadata and loaded monitors.
    No DB calls — fast, read-only, graceful fallback.
    """
    try:
        counts = {"active": 0, "paused": 0, "archived": 0, "other": 0}
        total_checkins = 0
        pauses_issued = 0

        for _aid, meta in mcp_server.agent_metadata.items():
            status = getattr(meta, "status", None)
            if status in counts:
                counts[status] += 1
            else:
                counts["other"] += 1
            total_checkins += getattr(meta, "total_updates", 0) or 0

            # Count pause lifecycle events
            for evt in getattr(meta, "lifecycle_events", []) or []:
                evt_type = evt.get("event") if isinstance(evt, dict) else None
                if evt_type in ("paused", "pause"):
                    pauses_issued += 1

        # Aggregate verdict distribution from loaded monitors
        verdicts: dict[str, int] = {}
        for _mid, monitor in mcp_server.monitors.items():
            for entry in getattr(monitor, "decision_history", []) or []:
                action = entry.get("action") if isinstance(entry, dict) else None
                if action:
                    verdicts[action] = verdicts.get(action, 0) + 1

        # Count dialectic sessions (if accessible)
        dialectic_sessions = 0
        try:
            if hasattr(mcp_server, "dialectic_sessions"):
                dialectic_sessions = len(mcp_server.dialectic_sessions)
        except Exception:
            pass

        result = {
            "agents": {k: v for k, v in counts.items() if v > 0},
            "total_checkins": total_checkins,
        }
        if verdicts:
            result["verdicts"] = verdicts
        if pauses_issued:
            result["pauses_issued"] = pauses_issued
        if dialectic_sessions:
            result["dialectic_sessions"] = dialectic_sessions
        return result
    except Exception:
        return {}

# =============================================================================
# DATE CONTEXT HELPER (only used by onboard handler)
# =============================================================================

def _get_date_context() -> dict:
    """Generate date context for onboard response (replaces separate date-context MCP)."""
    now = datetime.now()
    from datetime import timezone
    utc_now = datetime.now(timezone.utc)
    return {
        "full": now.strftime('%B %d, %Y'),
        "short": now.strftime('%Y-%m-%d'),
        "compact": now.strftime('%Y%m%d'),
        "iso": now.isoformat(),
        "iso_utc": utc_now.isoformat().replace('+00:00', 'Z'),
        "year": now.strftime('%Y'),
        "month": now.strftime('%B'),
        "weekday": now.strftime('%A'),
    }


def _infer_model_type_from_signals(explicit_model_type: Optional[str]) -> Optional[str]:
    """Infer model type from transport User-Agent when caller omits model_type."""
    if explicit_model_type:
        return explicit_model_type
    try:
        from ..context import get_session_signals
        signals = get_session_signals()
        ua = (signals.user_agent or "").lower() if signals else ""
        if not ua:
            return explicit_model_type

        # Prefer Codex-specific matches first.
        if re.search(r"gpt[-\s_]?5\.3", ua) and "codex" in ua:
            return "gpt-5.3-codex"
        if re.search(r"gpt[-\s_]?5\.4", ua) and "codex" in ua:
            return "gpt-5.4-codex"
        if re.search(r"gpt[-\s_]?5", ua) and "codex" in ua:
            return "gpt-5-codex"
        if "codex" in ua:
            return "codex"
        if "gpt" in ua or "openai" in ua or "chatgpt" in ua:
            return "gpt"
        if "claude" in ua or "anthropic" in ua:
            return "claude"
        if "gemini" in ua or "google" in ua:
            return "gemini"
    except Exception:
        pass
    return explicit_model_type


def _model_family(model_type: Optional[str]) -> Optional[str]:
    if not model_type:
        return None
    raw = model_type.lower()
    if "gpt" in raw or "openai" in raw or "chatgpt" in raw or "codex" in raw:
        return "gpt"
    if "claude" in raw or "anthropic" in raw:
        return "claude"
    if "gemini" in raw or "google" in raw:
        return "gemini"
    return None


def _should_rebadge_agent_id(current_agent_id: Optional[str], model_type: Optional[str], client_hint: Optional[str]) -> bool:
    """Return True when legacy structured IDs clearly mismatch current runtime."""
    if not current_agent_id or not model_type:
        return False
    aid = current_agent_id.lower()
    family = _model_family(model_type)
    if family == "gpt" and ("gpt" not in aid and "codex" not in aid):
        return True
    if family == "claude" and "claude" not in aid:
        return True
    if family == "gemini" and "gemini" not in aid:
        return True
    if family == "gpt" and "claude" in aid:
        return True
    if family == "claude" and ("gpt" in aid or "codex" in aid):
        return True
    if client_hint and client_hint.lower() == "cursor" and "claude_code" in aid and family == "gpt":
        return True
    return False


async def _persist_rebadged_agent_id(agent_uuid: str, new_agent_id: str) -> None:
    """Best-effort sync of refreshed structured agent_id to memory + DB.

    Updates both `agent_id` and `public_agent_id` in the in-memory metadata
    so lifecycle events and any other readers of `meta.agent_id` see the
    current identity — not the stale pre-rebadge value.
    """
    try:
        if agent_uuid in mcp_server.agent_metadata:
            meta = mcp_server.agent_metadata[agent_uuid]
            meta.agent_id = new_agent_id
            meta.public_agent_id = new_agent_id
    except Exception:
        pass
    try:
        db = get_db()
        await db.upsert_identity(
            agent_id=agent_uuid,
            api_key_hash="",
            metadata={"public_agent_id": new_agent_id, "agent_id": new_agent_id},
        )
    except Exception as e:
        logger.debug(f"[ONBOARD] Could not persist rebadged agent_id: {e}")


async def _collect_identity_aliases(
    agent_uuid: str,
    *,
    primary_agent_id: Optional[str] = None,
    label: Optional[str] = None,
) -> set[str]:
    """Collect acceptable aliases for one canonical UUID."""
    aliases = {str(agent_uuid)}
    for value in (primary_agent_id, label):
        if value:
            aliases.add(str(value))

    try:
        meta = mcp_server.agent_metadata.get(agent_uuid)
        if meta:
            for attr in ("public_agent_id", "structured_id", "label"):
                value = getattr(meta, attr, None)
                if value:
                    aliases.add(str(value))
    except Exception:
        pass

    try:
        from .shared import _session_identities

        for binding in _session_identities.values():
            if binding.get("bound_agent_id") == agent_uuid or binding.get("agent_uuid") == agent_uuid:
                for key in ("display_agent_id", "public_agent_id", "agent_label", "label"):
                    value = binding.get(key)
                    if value:
                        aliases.add(str(value))
    except Exception:
        pass

    try:
        db = get_db()
        identity = await db.get_identity(agent_uuid)
        metadata = getattr(identity, "metadata", None) or {}
        for key in ("public_agent_id", "agent_id", "structured_id", "label"):
            value = metadata.get(key)
            if value:
                aliases.add(str(value))
    except Exception:
        pass

    return aliases

# =============================================================================
# TOOL HANDLER (replaces identity() tool)
# =============================================================================

async def handle_identity_v2(
    arguments: Dict[str, Any],
    session_key: str,
    model_type: Optional[str] = None
) -> Dict[str, Any]:
    """
    identity() tool handler - simplified.

    Usage:
        identity()              -> Returns your UUID and label (lazy, not persisted)
        identity(name="X")      -> Sets your label to X, returns UUID (persists agent)

    This tool does NOT look up other agents. Use get_agent_metadata for that.
    """
    # Resolve session to identity (lazy — doesn't persist yet).
    # Name-claim was removed 2026-04-17: `name` is now a cosmetic label,
    # set via set_agent_label after the session resolves normally.
    name = arguments.get("name")

    # Pass model_type to generate proper agent_id (model+date format)
    identity = await resolve_session_identity(
        session_key,
        persist=False,
        model_type=model_type or arguments.get("model_type")
    )
    agent_id = identity.get("agent_id", identity["agent_uuid"])
    agent_uuid = identity["agent_uuid"]
    persisted = identity.get("persisted", False)

    # Set label if requested (this will persist the agent)
    if name:
        success = await set_agent_label(agent_uuid, name, session_key=session_key)
        if success:
            identity["label"] = name
            identity["label_set"] = True
            persisted = True  # set_agent_label calls ensure_agent_persisted

    display_name = identity.get("label")  # label is stored internally, exposed as display_name
    return {
        "success": True,
        "agent_id": agent_id,  # model+date format (e.g., "Claude_Opus_20251227")
        "agent_uuid": agent_uuid,  # internal UUID
        "display_name": display_name,  # user-chosen name (three-tier identity)
        "label": display_name,  # DEPRECATED alias for display_name (backward compat)
        "bound": True,
        "persisted": persisted,
        "source": identity.get("source"),
        "created": identity.get("created", False),
        "identity_resolution_outcome": identity.get("identity_resolution_outcome"),
        "message": f"Identity: {display_name or agent_id}",
    }

# =============================================================================
# DECORATOR-COMPATIBLE ADAPTER
# =============================================================================

def _resolve_response_client_hint(arguments: Optional[Dict[str, Any]]) -> Optional[str]:
    """Resolve client_hint for the response harness_context, matching onboard().

    identity() previously reported ``harness_context.harness_type="unknown"``
    on resume because its response builders received only
    ``arguments.get("client_hint")`` — callers rarely repeat the hint on a
    resume. onboard() falls back to the transport-bound hint
    (handle_onboard_adapter, see get_context_client_hint). Mirror that
    fallback so the descriptive harness_context is consistent across the two
    surfaces within one session. Response-shape only — never feeds the
    continuity-token pin or assurance tier.
    """
    hint = (arguments or {}).get("client_hint")
    if not hint or hint == "unknown":
        try:
            from ..context import get_context_client_hint
            hint = get_context_client_hint() or hint
        except Exception:
            pass
    return hint


def _build_identity_diag_payload_for_request(
    arguments: Dict[str, Any],
    model_type: Optional[str],
    *,
    agent_uuid: str,
    agent_id: str,
    label: Optional[str],
    status: str,
    identity_resolution_outcome: Optional[str] = None,
    provisional_lineage: bool = False,
    lineage_state: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the standard identity-success diag payload for `arguments` + `model_type`.

    Extracted from handle_identity_adapter so per-PATH resolvers can produce
    the same payload shape without the inner-function closure.

    Fast-path callers (monitor cache, archived warning) MUST omit
    ``lineage_state`` (default ``None``) — those paths skip the DB read
    that would derive it. Slow paths derive via
    ``derive_lineage_state(read_lineage_state(uuid))``.
    """
    from .shared import make_client_session_id

    stable_session_id = make_client_session_id(agent_uuid)
    try:
        from ..context import get_session_resolution_source, get_session_proof_origin
        continuity_source = get_session_resolution_source()
        proof_origin = get_session_proof_origin()
    except Exception:
        continuity_source = None
        proof_origin = None
    continuity_support = continuity_token_support_status()
    continuity_token = create_continuity_token(
        agent_uuid,
        stable_session_id,
        model_type=model_type,
        client_hint=arguments.get("client_hint"),
    )
    return build_identity_diag_payload(
        agent_uuid=agent_uuid,
        agent_id=agent_id,
        display_name=label,
        client_session_id=stable_session_id,
        continuity_source=continuity_source,
        continuity_support=continuity_support,
        continuity_token=continuity_token,
        identity_status=status,
        identity_resolution_outcome=identity_resolution_outcome,
        provisional_lineage=provisional_lineage,
        lineage_state=lineage_state,
        client_hint=_resolve_response_client_hint(arguments),
        model_type=model_type,
        proof_origin=proof_origin,
    )


def _identity_success_for_request(
    arguments: Dict[str, Any],
    payload: Dict[str, Any],
    *,
    agent_uuid: Optional[str] = None,
) -> Sequence[TextContent]:
    """Wrap an identity payload in a lite success response.

    Extracted from handle_identity_adapter. Does NOT mutate `arguments`.
    """
    response_arguments = dict(arguments)
    response_arguments["lite_response"] = True
    return success_response(payload, agent_id=agent_uuid, arguments=response_arguments)


async def _try_resume_by_agent_uuid_direct(
    arguments: Dict[str, Any],
    *,
    resume: bool,
    base_session_key: str,
    model_type: Optional[str],
) -> Optional[Sequence[TextContent]]:
    """PATH 0: Resolve identity directly by agent_uuid when supplied.

    Returns a TextContent response if this path handled the request, or None
    to let the dispatcher continue to the session-key pipeline. Order of
    attempts within the path is preserved exactly from the pre-refactor
    implementation so behavior is unchanged:

      1. Part C ownership gate (strict/log/off modes)
      2. Monitor-cache fast path (anyio-deadlock-safe, no DB)
      3. DB-backed verification (exists + active)

    Never creates a ghost — missing/archived UUIDs return explicit errors.
    """
    _direct_uuid = arguments.get("agent_uuid")
    if not (_direct_uuid and resume):
        return None

    # Identity Honesty Part C: PATH 0 must prove UUID ownership.
    # Bare agent_uuid without a matching signed continuity_token would
    # let any caller resurrect any known UUID — effectively making UUIDs
    # lookup keys in disguise (invariant #4 violation). Require a token
    # whose `aid` claim matches the requested UUID.
    _partc_token_aid = None
    if arguments.get("continuity_token"):
        _partc_token_aid = extract_token_agent_uuid(str(arguments["continuity_token"]))
    _partc_owned = _partc_token_aid == _direct_uuid

    # S19 substrate-attestation gate (PR3e): when the resume request arrives
    # over UDS (peer_pid set by PeerCredHTTPProtocol) AND the UUID has a
    # core.substrate_claims row, kernel-attested peer match is treated as
    # ownership proof equivalent to the continuity_token. Verification
    # rejection short-circuits to an explicit error pointing at the cause
    # (label/exec/start-time mismatch). HTTP requests (peer_pid is None)
    # and non-substrate UUIDs (no claim row) fall through unchanged.
    if not _partc_owned:
        try:
            from ..context import get_session_signals
            _signals = get_session_signals()
            _peer_pid = _signals.peer_pid if _signals else None
            if _peer_pid is not None:
                from src.substrate.handler_gate import verify_substrate_at_resume
                _substrate_result = await verify_substrate_at_resume(
                    _direct_uuid, _peer_pid,
                )
                if _substrate_result is not None:
                    if _substrate_result.accepted:
                        _partc_owned = True
                        logger.info(
                            "[SUBSTRATE_VERIFIED] %s... via UDS peer attestation "
                            "(pid=%d)", _direct_uuid[:8], _peer_pid,
                        )
                    else:
                        # Verification fired and rejected — return an
                        # explicit error naming the failure mode rather
                        # than falling through to the generic strict-mode
                        # message. The specific reason helps operators
                        # diagnose deployment issues (label mismatch,
                        # binary substitution, PID reuse).
                        return error_response(
                            _substrate_result.reason,
                            recovery={
                                "reason": _substrate_result.failure_code,
                                "agent_uuid": _direct_uuid,
                                "hint": (
                                    "Substrate-anchored UUIDs require "
                                    "kernel-attested peer match over UDS. "
 "."
                                ),
                            },
                        )
        except Exception as _exc:
            # Defense-in-depth: handler_gate is designed to fail-closed
            # within itself. This except catches truly unexpected errors
            # (e.g., import failures) and falls through to the existing
            # strict-mode behavior. Never default-accepts.
            logger.warning(
                "[SUBSTRATE_GATE] unexpected error for %s...: %s",
                _direct_uuid[:8], _exc, exc_info=True,
            )

    if not _partc_owned:
        from config.governance_config import identity_strict_mode
        _partc_mode = identity_strict_mode()
        if _partc_mode == "strict":
            await _emit_identity_hijack_event(_direct_uuid, "strict", _partc_token_aid)
            return error_response(
                (
                    "Bare agent_uuid resume is not permitted. Include "
                    "continuity_token (bound to this UUID) or call "
                    "identity(force_new=true) / onboard(force_new=true) to create a new identity."
                ),
                recovery={
                    "reason": "bare_uuid_resume_denied",
                    "agent_uuid": _direct_uuid,
                    "hint": (
                        "Resident agents should load continuity_token from their "
                        "anchor file and pass it on every identity() call."
                    ),
                },
            )
        elif _partc_mode == "log":
            logger.warning(
                "[IDENTITY_STRICT] Would reject PATH 0: agent_uuid=%s... without "
                "matching continuity_token (token_aid=%s). Caller would fork a "
                "session bound to a UUID it has not proven it owns. Upgrade caller "
                "to pass continuity_token.",
                _direct_uuid[:8],
                (_partc_token_aid[:8] + "...") if _partc_token_aid else "none",
            )
            await _emit_identity_hijack_event(_direct_uuid, "log", _partc_token_aid)
        # mode == "off": unchanged behavior, no log, no broadcast

    # PATH 0 FAST: if the UUID has a live in-process monitor, trust it
    # and skip DB verification entirely. Anyio-deadlock-safe (no awaits).
    # Rationale: monitors are loaded at startup for all persisted agents,
    # so a hit here means the agent is known to governance. Worst case
    # is we briefly serve an agent that was just archived, which the
    # next full check-in will surface via the archival log path.
    #
    # This is the structural fix for the 34-Watcher-fork incident:
    # transient governance slowness can't cascade into forks when
    # UUID-direct resume has a synchronous fallback.
    try:
        from ..shared import get_mcp_server
        srv = get_mcp_server()
        monitors = getattr(srv, "monitors", None) if srv is not None else None
        if monitors is not None and _direct_uuid in monitors:
            try:
                from ..context import update_context_agent_id, set_session_resolution_source
                update_context_agent_id(_direct_uuid)
                set_session_resolution_source("agent_uuid_direct_fastpath")
            except Exception:
                pass
            payload = _build_identity_diag_payload_for_request(
                arguments, model_type,
                agent_uuid=_direct_uuid,
                agent_id=_direct_uuid,
                label=None,
                status="resumed",
                identity_resolution_outcome="resumed",
            )
            payload.update({
                "resumed": True,
                "resumed_by_uuid": True,
                "source": "monitor_cache",
                "message": f"Resumed identity {_direct_uuid[:12]}... via in-process monitor cache",
            })
            return _identity_success_for_request(arguments, payload, agent_uuid=_direct_uuid)
    except Exception:
        # Any fast-path failure falls through to the DB-backed slow path.
        pass

    exists = await _agent_exists_in_postgres(_direct_uuid)
    if not exists:
        return error_response(
            f"Agent UUID {_direct_uuid[:12]}... not found",
            recovery={"reason": "uuid_not_found", "agent_uuid": _direct_uuid},
        )
    status = await _get_agent_status(_direct_uuid)
    if status != "active":
        return error_response(
            f"Agent UUID {_direct_uuid[:12]}... is not active (status={status})",
            recovery={"reason": "uuid_not_found", "agent_uuid": _direct_uuid, "status": status},
        )
    agent_id = await _get_agent_id_from_metadata(_direct_uuid) or _direct_uuid
    label = await _get_agent_label(_direct_uuid)
    # Update label if requested
    requested_name = arguments.get("name")
    if requested_name and requested_name != label:
        if await set_agent_label(_direct_uuid, requested_name, session_key=base_session_key):
            label = requested_name
    await _cache_session(base_session_key, _direct_uuid, display_agent_id=agent_id)
    try:
        from ..context import update_context_agent_id, set_session_resolution_source
        update_context_agent_id(_direct_uuid)
        set_session_resolution_source("agent_uuid_direct")
    except Exception:
        pass
    # R2 PR 3: surface provisional_lineage + lineage_state on the slow
    # PATH 0 resume. Slow path is already DB-bound, so the
    # `read_lineage_state` read is in budget and lets us derive
    # lineage_state from the same row (single round-trip, consistent
    # snapshot). Council fix: identity() now matches onboard()'s
    # field surface.
    _r2_prov = False
    _r2_state: Optional[str] = None
    try:
        _row = await get_db().read_lineage_state(_direct_uuid)
        if isinstance(_row, dict):
            _r2_prov = bool(_row.get("provisional_lineage"))
            from src.identity.lineage_lifecycle import derive_lineage_state
            _r2_state = derive_lineage_state(_row)
    except Exception:
        pass
    payload = _build_identity_diag_payload_for_request(
        arguments, model_type,
        agent_uuid=_direct_uuid,
        agent_id=agent_id,
        label=label,
        status="resumed",
        identity_resolution_outcome="resumed",
        provisional_lineage=_r2_prov,
        lineage_state=_r2_state,
    )
    payload.update({
        "resumed": True,
        "resumed_by_uuid": True,
        "message": f"Welcome back! Resumed identity '{label or agent_id}' via UUID",
    })
    return _identity_success_for_request(arguments, payload, agent_uuid=_direct_uuid)


def _middleware_identity_for_session(
    arguments: Dict[str, Any],
    base_session_key: str,
) -> Optional[Dict[str, Any]]:
    """Return dispatch-resolved identity when it belongs to this session."""
    if not arguments:
        return None
    identity = arguments.get("_middleware_identity_result")
    if not isinstance(identity, dict):
        return None
    if arguments.get("_middleware_identity_session_key") != base_session_key:
        return None
    return dict(identity)


async def _try_resume_by_session_key(
    arguments: Dict[str, Any],
    *,
    base_session_key: str,
    force_new: bool,
    resume: bool,
    token_agent_uuid: Optional[str],
    model_type: Optional[str],
    resolved_identity: Optional[Dict[str, Any]] = None,
) -> "tuple[Optional[Sequence[TextContent]], Optional[Dict[str, Any]]]":
    """STEP 1: Resolve identity under the base session key.

    Returns `(response, existing_identity)`. `response` is non-None when:
      - A resume succeeded (returns the success payload)
      - `resolve_session_identity` reported `resume_failed` (returns error)
      - The session maps to an archived agent (returns warning payload)
    Returns `(None, existing_identity)` when the caller should fall through
    to create-via-handle_identity_v2. `existing_identity` is also returned so
    the dispatcher can apply the "skip auto-bind on archived" guard downstream.
    """
    if force_new:
        return None, None

    existing_identity = resolved_identity
    if existing_identity is None:
        existing_identity = await resolve_session_identity(
            base_session_key, persist=False, resume=resume,
            token_agent_uuid=token_agent_uuid,
        )

    # Token-based resume failed — agent not found or not active.
    # S21-a: distinguish session_resolve_miss (PATH 2 fail-closed, no existing
    # session row) from token-rebind failures. The former is the normal path
    # for a fresh session and should fall through to create-finisher exactly
    # as a `created=True` shape did before; the latter remains an error.
    if existing_identity.get("resume_failed"):
        if existing_identity.get("error") == "session_resolve_miss":
            return None, existing_identity
        return (
            error_response(
                existing_identity.get("message", "Could not resume identity"),
                recovery={
                    "reason": "resume_failed",
                    "token_agent_uuid": existing_identity.get("token_agent_uuid"),
                    "hint": "Call onboard(force_new=true) to create a new identity.",
                },
            ),
            existing_identity,
        )

    if existing_identity.get("created"):
        # Not an existing agent — fall through to create-finisher.
        return None, existing_identity

    # EXISTING AGENT FOUND under base key (only happens when resume=True)
    agent_uuid = existing_identity.get("agent_uuid")
    agent_id = existing_identity.get("agent_id", agent_uuid)
    label = existing_identity.get("label")

    # FIX: Don't silently resume archived agents — warn the caller
    if existing_identity.get("archived"):
        logger.info(f"[IDENTITY] Found archived agent {agent_uuid[:8]}... — returning warning instead of silent resume")
        payload = _build_identity_diag_payload_for_request(
            arguments, model_type,
            agent_uuid=agent_uuid,
            agent_id=agent_id,
            label=label,
            status="archived",
            identity_resolution_outcome=existing_identity.get("identity_resolution_outcome") or "resumed",
        )
        payload.update({
            "archived": True,
            "resumed": False,
            "message": f"Session maps to archived agent '{label or agent_id}'. Use onboard() to reactivate or force_new=true for a fresh identity.",
            "hint": "onboard() will auto-reactivate this agent. force_new=true creates a new one.",
            "options": {
                "reactivate": "Call onboard() to resume this archived agent",
                "fresh": "Call identity(force_new=true) or onboard(force_new=true) for a new identity"
            }
        })
        return _identity_success_for_request(arguments, payload, agent_uuid=agent_uuid), existing_identity

    logger.info(f"[IDENTITY] Resuming existing agent {agent_uuid[:8]}... (explicit resume=true)")

    # Update label if requested
    if arguments.get("name") and arguments.get("name") != label:
        success = await set_agent_label(agent_uuid, arguments.get("name"), session_key=base_session_key)
        if success:
            label = arguments.get("name")

    # R2 PR 3: surface provisional_lineage + lineage_state at top level.
    # Slow path is already DB-bound here (label set, etc.), so the row
    # read is in budget. The fast resume paths above (monitor cache,
    # archived warning) default to False / None — those callers can
    # re-query via `identity()` slow path if they need the flag.
    # Council fix: identity() now matches onboard()'s field surface.
    _r2_prov = False
    _r2_state: Optional[str] = None
    try:
        _row = await get_db().read_lineage_state(agent_uuid)
        if isinstance(_row, dict):
            _r2_prov = bool(_row.get("provisional_lineage"))
            from src.identity.lineage_lifecycle import derive_lineage_state
            _r2_state = derive_lineage_state(_row)
    except Exception:
        pass

    payload = _build_identity_diag_payload_for_request(
        arguments, model_type,
        agent_uuid=agent_uuid,
        agent_id=agent_id,
        label=label,
        status="resumed",
        identity_resolution_outcome=existing_identity.get("identity_resolution_outcome") or "resumed",
        provisional_lineage=_r2_prov,
        lineage_state=_r2_state,
    )
    payload.update({
        "resumed": True,
        "message": f"Welcome back! Resumed identity '{label or agent_id}'",
        "hint": "Use force_new=true to create a new identity instead"
    })
    return _identity_success_for_request(arguments, payload, agent_uuid=agent_uuid), existing_identity


@mcp_tool("identity", timeout=10.0, requires_identity="pre_onboard")
async def handle_identity_adapter(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    IDENTITY - Who am I? Auto-creates identity if first call.

    Simplified v2 implementation with 3 paths:
    - Redis cache (fast)
    - PostgreSQL lookup
    - Create new agent

    Optional: Pass name='...' to set your display name.
    Optional: Pass model_type='...' to create distinct identity per model.
    Optional: Pass resume=false to force a new identity (with predecessor link).
    Optional: Pass force_new=true to create new identity with no predecessor link.
    Optional: Pass agent_uuid='...' plus a matching continuity_token to rebind
              a known identity. Bare UUID resume is hijack-shaped under strict
              identity mode. Returns error if UUID is not found or active —
              never creates a ghost.
    Fresh process instances should prefer onboard(force_new=true) and use
    parent_agent_id to declare lineage to prior work.

    Dispatcher structure (2026-04-19 refactor):
      1. PATH 0  — _try_resume_by_agent_uuid_direct (proof-owned UUID rebind)
      2. STEP 1  — _try_resume_by_session_key (base-session-key resolve)
      3. Finisher — fall through to handle_identity_v2 + persist + response
    """
    arguments = arguments or {}
    normalize_client_session_id_argument(arguments)

    # S13 v2-ontology gate: arg-less identity() from a fresh process-instance
    # with no proof signal mints fresh by default per identity.md §"Layered
    # taxonomy of continuity". Mirrors the gate in handle_onboard_v2 (~L1197);
    # without it, arg-less identity() falls through to IP:UA-pin resolution
    # and silently re-binds to the prior session, which is the resolution
    # path S13 retires for the unauthenticated case.
    _has_proof_signal = bool(
        arguments.get("continuity_token")
        or arguments.get("client_session_id")
        or arguments.get("agent_uuid")
        or arguments.get("agent_id")
        or arguments.get("name")
    )
    if not _has_proof_signal and not arguments.get("force_new"):
        arguments["force_new"] = True
        logger.info(
            "[FRESH_INSTANCE] arg-less identity() with no proof signal — "
            "defaulting to force_new=true per v2 ontology (S13)"
        )

    force_new = arguments.get("force_new", False)
    resume = arguments.get("resume", True)
    model_type = arguments.get("model_type")

    # Derive base session key (unified)
    from ..context import get_session_signals
    signals = get_session_signals()
    base_session_key = await derive_session_key(signals, arguments)
    normalized_model = None
    explicit_resume_binding = bool(arguments.get("client_session_id") or arguments.get("continuity_token"))

    # PATH 0: Direct UUID lookup with Part-C ownership proof.
    path0_response = await _try_resume_by_agent_uuid_direct(
        arguments,
        resume=resume,
        base_session_key=base_session_key,
        model_type=model_type,
    )
    if path0_response is not None:
        return path0_response

    # PATH 2.5 (name-claim) removed 2026-04-17. `name` is now a cosmetic
    # label updated after the session resolves; it never drives lookup.
    name = arguments.get("name")

    _id_caller_token = arguments.get("continuity_token")
    if not isinstance(_id_caller_token, str) or not _id_caller_token:
        _id_caller_token = None
    if _id_caller_token and not force_new:
        return _continuity_token_resume_rejected(
            tool="identity",
            token_str=_id_caller_token,
        )

    # Extract agent UUID from continuity token for direct lookup fallback.
    # If session bindings expired, this allows rebinding without forking.
    _token_agent_uuid = None
    if _id_caller_token:
        _token_agent_uuid = extract_token_agent_uuid(_id_caller_token)

    # STEP 1: Check for existing identity under BASE key first (unless force_new).
    session_key = base_session_key
    step1_response, existing_identity = await _try_resume_by_session_key(
        arguments,
        base_session_key=base_session_key,
        force_new=force_new,
        resume=resume,
        token_agent_uuid=_token_agent_uuid,
        model_type=model_type,
        resolved_identity=_middleware_identity_for_session(arguments, base_session_key),
    )
    if step1_response is not None:
        return step1_response

    # model_type is passed through for agent_id generation, but does NOT fork session keys.
    # All identities for a session use the base session key to prevent fragmentation.

    # Call simplified handler with model_type for agent_id generation
    result = await handle_identity_v2(arguments, session_key, model_type=model_type)
    agent_id = result.get("agent_id", result["agent_uuid"])
    agent_uuid = result["agent_uuid"]

    # CRITICAL: Update request context so signature in response matches resolved identity
    try:
        from ..context import update_context_agent_id
        update_context_agent_id(agent_uuid)
    except Exception as e:
        logger.debug(f"Could not update context in identity: {e}")

    # Persist newly-created identities before minting a continuity token.
    #
    # Previously identity() was "lazy": new agents only existed in-memory until
    # the caller also passed name=. But we still issued a continuity_token
    # referencing the in-memory UUID. The token looked durable, but any later
    # rebind via PATH 2.8 hit `agent not active` because the UUID was never
    # written to core.agents. Callers were left holding dead tokens, which
    # manifested as ghost identity proliferation (cf. d4d4370, 718ccd3).
    #
    # Fix: when identity() creates a fresh agent (result.created is True),
    # write it to PostgreSQL before returning so the token's promise is real.
    if result.get("created") and not result.get("persisted"):
        try:
            # parent_agent_id is only set when the caller explicitly asserted
            # succession (post-2026-04-16 EISV inheritance spec). Fingerprint
            # match no longer auto-claims lineage, so no predecessor_uuid to
            # read here. Read from `arguments` first because `handle_identity_v2`
            # does not currently plumb these through its return dict —
            # without this fallback, identity(parent_agent_id=...) silently
            # drops lineage and contradicts the SDK contract at
            # unitares_sdk/client.py::identity.
            _parent = arguments.get("parent_agent_id") or result.get("parent_agent_id")
            _spawn = arguments.get("spawn_reason") or result.get("spawn_reason") or ("new_session" if _parent else None)
            newly_persisted = await ensure_agent_persisted(
                agent_uuid,
                session_key,
                parent_agent_id=_parent,
                spawn_reason=_spawn,
            )
            if newly_persisted:
                logger.info(
                    f"[IDENTITY] Persisted fresh identity {agent_uuid[:8]}... "
                    f"(parent={_parent[:8] + '...' if _parent else 'none'})"
                )
                result["persisted"] = True
        except Exception as e:
            # Persistence failure is visible but not fatal — caller still gets
            # the identity in the response; the token just won't rebind later.
            logger.warning(
                f"[IDENTITY] Failed to persist fresh identity {agent_uuid[:8]}...: {e}"
            )

    # Get public/structured identity handles from runtime metadata.
    public_agent_id = result.get("public_agent_id") or agent_id
    structured_id = None
    try:
        if agent_uuid in mcp_server.agent_metadata:
            meta = mcp_server.agent_metadata[agent_uuid]
            public_agent_id = getattr(meta, "public_agent_id", None) or public_agent_id
            structured_id = getattr(meta, 'structured_id', None)

            # If model_type provided and structured_id doesn't include it, regenerate
            if model_type and structured_id and normalized_model and normalized_model not in structured_id:
                from ..support.naming_helpers import detect_interface_context, generate_structured_id
                from ..context import get_context_client_hint
                context = detect_interface_context()
                existing_ids = [
                    getattr(m, 'structured_id', None)
                    for m in mcp_server.agent_metadata.values()
                    if getattr(m, 'structured_id', None)
                ]
                meta.structured_id = generate_structured_id(
                    context=context,
                    existing_ids=existing_ids,
                    client_hint=get_context_client_hint(),
                    model_type=model_type,
                    agent_uuid=agent_uuid
                )
                structured_id = meta.structured_id
                logger.info(f"[IDENTITY] Regenerated structured_id with model: {structured_id}")
    except Exception as e:
        logger.debug(f"Could not get/update structured_id: {e}")

    # Format response - four-tier identity (v2.5.2)
    final_agent_id = public_agent_id or structured_id or agent_uuid
    user_name = result.get("label")

    # Derive client_session_id for session continuity
    from .shared import make_client_session_id
    client_session_id = make_client_session_id(agent_uuid)
    continuity_token = create_continuity_token(
        agent_uuid,
        client_session_id,
        model_type=model_type,
        client_hint=arguments.get("client_hint"),
    )
    try:
        from ..context import get_session_resolution_source, get_session_proof_origin
        continuity_source = get_session_resolution_source()
        proof_origin = get_session_proof_origin()
    except Exception:
        continuity_source = None
        proof_origin = None
    continuity_support = continuity_token_support_status()

    verbose = coerce_bool(arguments.get("verbose"), default=False) if arguments else False
    identity_status = "created" if result.get("created") else "resumed"
    identity_resolution_outcome = result.get("identity_resolution_outcome")
    if (
        result.get("created")
        and existing_identity
        and existing_identity.get("error") == "session_resolve_miss"
    ):
        identity_resolution_outcome = "minted_after_resume_miss"
        result["identity_resolution_outcome"] = identity_resolution_outcome
    elif not identity_resolution_outcome:
        identity_resolution_outcome = "minted_fresh" if result.get("created") else "resumed"
    auto_bind = coerce_bool(arguments.get("auto_bind", True))
    if auto_bind and not (existing_identity and existing_identity.get("archived")):
        try:
            await _perform_session_bind(
                agent_uuid=agent_uuid,
                session_key=client_session_id,
                display_agent_id=final_agent_id,
                source="identity_stable_session",
            )
        except Exception as e:
            logger.debug(f"[IDENTITY] Stable session bind failed (non-fatal): {e}")

    # R2 PR 3 council fix: derive provisional_lineage + lineage_state
    # from the persisted row so identity()'s primary response surface
    # matches onboard()'s. Single read, single derive — fast paths that
    # already returned earlier never reach this site.
    _r2_prov_main = False
    _r2_state_main: Optional[str] = None
    try:
        _row_main = await get_db().read_lineage_state(agent_uuid)
        if isinstance(_row_main, dict):
            _r2_prov_main = bool(_row_main.get("provisional_lineage"))
            from src.identity.lineage_lifecycle import derive_lineage_state
            _r2_state_main = derive_lineage_state(_row_main)
    except Exception as _e_lineage:
        logger.debug(
            f"[R2] read_lineage_state in identity() main path failed (non-fatal): {_e_lineage}"
        )

    response_data = build_identity_response_data(
        agent_uuid=agent_uuid,
        agent_id=final_agent_id,
        display_name=user_name,
        client_session_id=client_session_id,
        continuity_source=continuity_source,
        continuity_support=continuity_support,
        continuity_token=continuity_token,
        identity_status=identity_status,
        identity_resolution_outcome=identity_resolution_outcome,
        model_type=model_type,
        resumed=False if result.get("created") else (True if result.get("source") else None),
        session_continuity=result.get("session_continuity"),
        verbose=verbose,
        provisional_lineage=_r2_prov_main,
        lineage_state=_r2_state_main,
        client_hint=_resolve_response_client_hint(arguments),
        proof_origin=proof_origin,
    )

    # Auto-bind: automatically perform session binding so agents don't need a separate bind_session call
    if auto_bind and not (existing_identity and existing_identity.get("archived")):
        try:
            from ..context import get_session_signals as _abs_signals
            mcp_signals = _abs_signals()
            mcp_key = await derive_session_key(mcp_signals)
            if mcp_key and mcp_key != base_session_key:
                await _perform_session_bind(
                    agent_uuid=agent_uuid,
                    session_key=mcp_key,
                    display_agent_id=final_agent_id,
                    source="identity_auto_bind",
                )
                response_data["auto_bound"] = True
        except Exception as e:
            logger.debug(f"[IDENTITY] Auto-bind failed (non-fatal): {e}")

    # S1-a (2026-04-29): grace-period deprecation surface for identity().
    # Cross-process-instance resume via continuity_token is deprecating;
    # intra-session use (request auth on process_agent_update etc.) is NOT.
    # identity() called with continuity_token AND without force_new is the
    # cross-instance resume case; intra-process callers don't need to pass
    # the token to identity() (their session is already bound).
    # Adversarial-input guard: a non-string continuity_token (list/dict/bytes)
    # would otherwise inflate grace-period telemetry without the caller
    # actually holding a verifiable token.
    _emit_continuity_token_deprecation(
        response_dict=response_data,
        used_token_for_resume=(_id_caller_token is not None) and not force_new,
        token_str=_id_caller_token,
        agent_uuid=agent_uuid,
        response_agent_id=final_agent_id,
        client_hint=arguments.get("client_hint"),
        model_type=model_type,
    )

    # `arguments` was guaranteed non-None at the top of the handler (L850-851);
    # the `lite_response` flag tells success_response to skip redundant
    # agent_signature since identity already contains that info.
    arguments["lite_response"] = True
    return success_response(response_data, agent_id=final_agent_id, arguments=arguments)


async def _perform_session_bind(
    agent_uuid: str,
    session_key: str,
    display_agent_id: str = None,
    source: str = "auto_bind",
) -> dict:
    """Bind a session key to an agent UUID (Redis + PostgreSQL + sticky transport).

    Shared helper used by both identity() auto-bind and bind_session().
    All steps are best-effort — failures are logged but don't prevent binding.
    """
    bound_info = {"bound": False, "session_key": session_key[:20] + "..." if session_key else None}

    # 1. Redis cache
    try:
        await _cache_session(session_key, agent_uuid, display_agent_id=display_agent_id)
        bound_info["redis"] = True
    except Exception as e:
        logger.debug(f"[{source}] Redis cache failed (non-fatal): {e}")
        bound_info["redis"] = False

    # 2. PostgreSQL session
    try:
        db = get_db()
        if hasattr(db, "init"):
            await db.init()
        identity_record = await db.get_identity(agent_uuid)
        if identity_record:
            client_info = {"agent_uuid": agent_uuid, "bound_via": source}
            if display_agent_id and display_agent_id != agent_uuid:
                client_info["public_agent_id"] = display_agent_id
                client_info["agent_id"] = display_agent_id
            await db.create_session(
                session_id=session_key,
                identity_id=identity_record.identity_id,
                expires_at=datetime.now() + timedelta(hours=GovernanceConfig.SESSION_TTL_HOURS),
                client_type="mcp",
                client_info=client_info,
            )
            bound_info["postgres"] = True
    except Exception as e:
        logger.debug(f"[{source}] PostgreSQL session binding failed (non-fatal): {e}")
        bound_info["postgres"] = False

    # 3. Sticky transport
    try:
        from ..context import get_session_signals as _get_signals
        from ..middleware.identity_step import _transport_cache_key, update_transport_binding
        _signals = _get_signals()
        _tkey = _transport_cache_key(_signals)
        if _tkey:
            # S3: derive original_session_source from the strongest proof
            # signal present on the current request. _transport_cache_key
            # only returns a key for the ip_ua_fingerprint paths (with
            # optional mcp_session_id), so the realistic originals here are
            # "context_mcp_session_id" (medium) or "ip_ua_fingerprint" (weak).
            # Falls back to the caller's source label otherwise so audit
            # trails stay non-empty.
            _original = (
                "context_mcp_session_id"
                if getattr(_signals, "mcp_session_id", None)
                else "ip_ua_fingerprint"
                if getattr(_signals, "ip_ua_fingerprint", None)
                else source
            )
            update_transport_binding(
                _tkey,
                agent_uuid,
                session_key,
                source,
                original_session_source=_original,
            )
            bound_info["transport"] = True
    except Exception:
        bound_info["transport"] = False

    bound_info["bound"] = True
    logger.info(f"[{source}] Bound session {session_key[:20]}... -> agent {agent_uuid[:8]}...")
    return bound_info


@mcp_tool("bind_session", timeout=5.0, requires_identity="pre_onboard")
async def handle_bind_session(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Bind current MCP session to an existing agent identity.

    Bridges the identity gap between REST hooks (which onboard via curl)
    and MCP Streamable HTTP (which uses a different session key).

    Call this once at session start with the client_session_id from your
    startup hook context.
    """
    arguments = arguments or {}
    normalize_client_session_id_argument(arguments)
    strict = coerce_bool(arguments.get("strict"))
    resume_requested = coerce_bool(arguments.get("resume"))

    # Safety guard: prevent accidental cross-session reattachment.
    # Callers must explicitly opt in to rebind with resume=true, or use strict mode.
    if not resume_requested and not strict:
        return error_response(
            "bind_session requires explicit resume=true (or strict=true) to prevent accidental reattachment",
            recovery={
                "action": "Pass resume=true when intentionally restoring a prior identity.",
                "example": "bind_session(client_session_id='agent-xxxx', resume=true)",
                "alternative": "Use onboard() for fresh/new identity bootstrap.",
            }
        )

    client_session_id = arguments.get("client_session_id")
    expected_agent_id = arguments.get("agent_id")
    # S1-c (2026-05-23): token-only bind was the retired cross-process
    # resume surface. Explicit client_session_id binding remains valid.
    _bs_caller_token = arguments.get("continuity_token")
    if not isinstance(_bs_caller_token, str) or not _bs_caller_token:
        _bs_caller_token = None
    if not client_session_id and _bs_caller_token:
        return _continuity_token_resume_rejected(
            tool="bind_session",
            token_str=_bs_caller_token,
        )
    if not client_session_id:
        return error_response("client_session_id is required")
    if strict and not expected_agent_id:
        return error_response(
            "strict bind_session requires agent_id",
            recovery={
                "action": "Provide agent_id (UUID or display agent_id) with strict=true.",
                "example": "bind_session(client_session_id='agent-xxxx', agent_id='Claude_Code_20260315', strict=true)",
            }
        )

    # Get the current MCP session key (the one we want to rebind)
    from ..context import get_session_signals
    signals = get_session_signals()
    mcp_session_key = await derive_session_key(signals)

    # Resolve the agent from the provided client_session_id
    # resume=True is correct here — bind_session is explicitly resuming an existing identity
    target_identity = await resolve_session_identity(client_session_id, persist=False, resume=True)
    # S21-a: resume=True with no PG row now returns resume_failed instead
    # of silently minting a ghost. Treat that the same as "no existing agent".
    if (not target_identity or target_identity.get("created")
            or target_identity.get("resume_failed")):
        return error_response(
            f"No existing agent found for client_session_id '{client_session_id[:20]}...'. "
            "Ensure the session-start hook onboarded successfully."
        )

    target_uuid = target_identity["agent_uuid"]
    target_label = target_identity.get("label")
    target_agent_id = target_identity.get("agent_id", target_uuid)

    # Guard against accidental cross-binding by allowing callers to pin
    # bind_session to a specific identity (UUID or display agent_id).
    if expected_agent_id:
        expected_agent_id = str(expected_agent_id).strip()
        accepted_aliases = await _collect_identity_aliases(
            target_uuid,
            primary_agent_id=target_agent_id,
            label=target_label,
        )
        if expected_agent_id not in accepted_aliases:
            return error_response(
                "agent_id mismatch for requested session binding",
                details={
                    "expected_agent_id": expected_agent_id,
                    "resolved_agent_id": target_agent_id,
                    "resolved_agent_uuid": target_uuid,
                    "accepted_aliases": sorted(accepted_aliases),
                    "client_session_id": client_session_id,
                },
                recovery={
                    "action": "Verify client_session_id belongs to the intended agent, or pass the correct agent_id/UUID.",
                    "hint": "Use identity() or get_governance_metrics() to confirm your active identity first.",
                }
            )

    # Rebind: cache the MCP session key → target agent UUID
    if mcp_session_key and mcp_session_key != client_session_id:
        await _perform_session_bind(target_uuid, mcp_session_key, display_agent_id=target_agent_id, source="bind_session")

    # Update request context so subsequent calls in this request use the correct agent
    try:
        from ..context import update_context_agent_id
        update_context_agent_id(target_uuid)
    except Exception:
        pass

    bind_response: Dict[str, Any] = {
        "bound": True,
        "agent_uuid": target_uuid,
        "agent_id": target_agent_id,
        "display_name": target_label,
        "mcp_session_key": mcp_session_key[:20] + "..." if mcp_session_key else None,
        "message": f"MCP session bound to agent '{target_label or target_agent_id}'",
    }

    return success_response(bind_response)


@mcp_tool("onboard", timeout=15.0, requires_identity="pre_onboard")
async def handle_onboard_v2(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    ONBOARD - Single entry point for new agents.

    This is THE portal tool. Call it first, get back everything you need:
    - Your identity (auto-created)
    - Ready-to-use templates for next calls
    - Client-specific guidance

    Returns a "toolcard" payload with next_calls array.
    """
    from ..shared import get_mcp_server

    # DEBUG: Log entry
    logger.debug(f"[SESSION_DEBUG] onboard() entry: args_keys={list(arguments.keys()) if arguments else []}")

    # === KWARGS STRING UNWRAPPING ===
    if arguments and "kwargs" in arguments and isinstance(arguments["kwargs"], str):
        try:
            import json
            kwargs_parsed = json.loads(arguments["kwargs"])
            if isinstance(kwargs_parsed, dict):
                del arguments["kwargs"]
                arguments.update(kwargs_parsed)
                logger.info(f"[KWARGS] Unwrapped: {list(kwargs_parsed.keys())}")
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"[KWARGS] Failed to parse: {e}")

    arguments = arguments or {}
    normalize_client_session_id_argument(arguments)

    # S13 v2-ontology gate: arg-less onboard from a fresh process-instance
    # mints fresh by default per identity.md §"Layered taxonomy of continuity".
    # Caller opts back into resume by passing any proof signal — explicit
    # force_new flag, ownership proof (continuity_token, agent_uuid, agent_id),
    # transport-bound session signal (client_session_id), or display name.
    # Without any signal we have nothing to honestly resume to; the legacy
    # IP:UA pin path was the performative fill-in this gate retires for the
    # arg-less case. Plugin-side complement of this gate landed as
    # unitares-governance-plugin#17 (S11) on 2026-04-21.
    try:
        from ..context import get_csid_transport_injected
        _client_session_id_caller_asserted = (
            bool(arguments.get("client_session_id"))
            and not get_csid_transport_injected()
        )
    except Exception:
        # Backward-compatible fallback: if the context side channel is
        # unavailable, preserve the legacy caller-supplied interpretation.
        _client_session_id_caller_asserted = bool(arguments.get("client_session_id"))

    _has_proof_signal = bool(
        arguments.get("continuity_token")
        or _client_session_id_caller_asserted
        or arguments.get("agent_uuid")
        or arguments.get("agent_id")
        or arguments.get("name")
    )
    _force_new_requested = coerce_bool(arguments.get("force_new"), default=False)
    if not _has_proof_signal and not _force_new_requested:
        from src.mcp_handlers.identity_bootstrap import (
            is_strict_identity_required,
            strict_identity_refusal_payload,
        )
        if is_strict_identity_required():
            logger.info(
                "[FRESH_INSTANCE] STRICT_IDENTITY_REQUIRED=true and "
                "arg-less onboard() has no caller-proof signal — refusing "
                "instead of defaulting to force_new=true"
            )
            return success_response(strict_identity_refusal_payload(
                "onboard",
                status="lineage_declaration_required",
                hint=(
                    "Bare onboard() is ambiguous — pass "
                    "parent_agent_id=<prior UUID> to continue prior work, "
                    "OR force_new=true to confirm a fresh process-instance "
                    "with no lineage."
                ),
            ))
        arguments["force_new"] = True
        logger.info(
            "[FRESH_INSTANCE] arg-less onboard() with no proof signal — "
            "defaulting to force_new=true per v2 ontology (S13)"
        )

    # Extract optional parameters
    name = arguments.get("name")  # Optional: set display name
    force_new = coerce_bool(arguments.get("force_new"), default=False)  # Force new identity creation
    model_type = _infer_model_type_from_signals(arguments.get("model_type"))

    # Thread identity parameters (honest forking)
    _parent_agent_id = arguments.get("parent_agent_id")  # UUID of predecessor
    _spawn_reason = arguments.get("spawn_reason")  # compaction|subagent|new_session|explicit
    _thread_id_hint = arguments.get("thread_id")  # Explicit thread to join

    # Auto-detect client_hint from transport if not provided
    client_hint = arguments.get("client_hint")
    if not client_hint or client_hint == "unknown":
        from ..context import get_context_client_hint
        client_hint = get_context_client_hint() or "unknown"

    orchestrated = coerce_bool(arguments.get("orchestrated"), default=False)
    raw_client_session_id_arg = arguments.get("client_session_id")

    # Derive base session key (unified — pin lookup integrated in derive_session_key)
    from ..context import get_session_signals
    signals = get_session_signals()
    base_session_key = await derive_session_key(signals, arguments)
    raw_client_session_id = arguments.get("client_session_id")
    anchor_candidates = [raw_client_session_id_arg, raw_client_session_id]
    orchestrated_thread_anchor = (
        orchestrated
        and any(
            isinstance(candidate, str)
            and (
                candidate.startswith("agent:/thread-")
                or candidate.startswith("agent:_thread-")
            )
            for candidate in anchor_candidates
        )
    )
    # Initialize session_key eagerly so no downstream branch can hit
    # UnboundLocalError if the control flow misses its assignment (the
    # 2026-04-19 crash was exactly this — schema/handler resume-default
    # mismatch put us on a code path that never set session_key). The
    # fresh-identity and force_new branches rebind it below.
    session_key = base_session_key
    normalized_model = None

    # Session continuity: resume existing identity by default.
    # Agents can pass resume=false for a new identity, or force_new=true for a clean break.
    resume = coerce_bool(arguments.get("resume"), default=True)

    # PATH 2 IP:UA pin cross-check (2026-04-20 council follow-up to #83).
    resume = await _path2_ipua_pin_check(
        arguments=arguments,
        base_session_key=base_session_key,
        force_new=force_new,
        resume=resume,
    )

    # Extract agent UUID from continuity token for direct lookup fallback (PATH 2.8).
    # Token is a cryptographic proof of identity — stronger than name claim.
    _token_agent_uuid = None
    _caller_token_for_resume = arguments.get("continuity_token")
    if not isinstance(_caller_token_for_resume, str) or not _caller_token_for_resume:
        _caller_token_for_resume = None
    if _caller_token_for_resume and not force_new:
        return _continuity_token_resume_rejected(
            tool="onboard",
            token_str=_caller_token_for_resume,
        )
    if _caller_token_for_resume:
        _token_agent_uuid = extract_token_agent_uuid(_caller_token_for_resume)

    middleware_identity = _middleware_identity_for_session(arguments, base_session_key)

    # STEP 1: Check if an identity already exists for this session (base key)
    # When resume=True (default): reuse existing identity
    # When resume=False: create new identity with predecessor link
    existing_identity = None
    created_fresh_identity = False  # Track if we got a fresh identity to persist
    _was_archived = False  # Track if agent was auto-unarchived
    if not force_new:
        # Token-based resume is the only name-free resume path (PATH 2.8).
        # Name-based reconnection was removed 2026-04-17 — a label alone is
        # not proof of identity. Callers who previously relied on
        # `onboard(name=X, resume=true)` must now pass agent_uuid or
        # continuity_token, or accept a fresh identity via force_new=true.
        if middleware_identity is not None:
            existing_identity = middleware_identity
        elif _token_agent_uuid and resume:
            existing_identity = await resolve_session_identity(
                base_session_key, persist=False, resume=resume,
                token_agent_uuid=_token_agent_uuid,
            )
        else:
            existing_identity = await resolve_session_identity(
                base_session_key, persist=False, resume=resume,
                token_agent_uuid=_token_agent_uuid,
            )
        # Token-based resume can fail on either branch above (PATH 2.8 in
        # resolve_session_identity returns resume_failed whenever the
        # token's agent is not active, regardless of the resume flag).
        # Hoisting the check past both branches ensures an archived-token
        # onboard gets a clean rejection instead of falling through to
        # code that assumes an active existing_identity.
        # S21-a: session_resolve_miss is *not* a hard failure — it just
        # means "no prior binding for this session_key." Null out
        # existing_identity so the resume / captured-fresh branches below
        # are skipped, and the STEP-2 else branch at line ~1509 mints via
        # resolve_session_identity(persist=True) with the caller-provided
        # force_new value.
        if existing_identity.get("resume_failed"):
            if existing_identity.get("error") == "session_resolve_miss":
                # If the caller didn't declare lineage, default the spawn
                # reason to a legible label so the captured-fresh
                # persistence path's ensure_agent_persisted call (~L1494)
                # writes core.agents with a non-NULL spawn_reason. Without
                # this fallback, every dispatch-time onboard with no prior
                # session row lands in the no-lineage ghost population —
                # the metric S21-a was supposed to move (council follow-up
                # on the H4/lineage-decl gap, 2026-04-27 post-deploy
                # canary refuted by 0 dispatch_auto_mint rows in
                # core.agents). Caller-provided values are preserved.
                if not _spawn_reason:
                    _spawn_reason = (
                        "orchestrated_thread_anchor"
                        if orchestrated_thread_anchor
                        else "auto_onboard_no_session"
                    )

                # #425 Path B: when STRICT_IDENTITY_REQUIRED is on, refuse
                # bare onboard() (no parent_agent_id and no force_new=True)
                # because the caller hasn't declared lineage intent. Either
                # path is fine — declared lineage or explicit force_new —
                # but the implicit auto_onboard_no_session mint violates
                # the ontology's "lineage declared, not auto-minted" rule.
                # Default off; gated by env flag for staged rollout.
                from src.mcp_handlers.identity_bootstrap import is_strict_identity_required
                if (is_strict_identity_required()
                        and not _parent_agent_id
                        and not force_new
                        and not orchestrated_thread_anchor):
                    logger.info(
                        "[ONBOARD] STRICT_IDENTITY_REQUIRED=true and bare "
                        "onboard() (no parent_agent_id, no force_new=true) "
                        "— refusing rather than auto-minting "
                        "auto_onboard_no_session (#425 Path B)"
                    )
                    # success_response is imported at module level (line 22).
                    # Do NOT re-import here — it shadows the module binding
                    # for the entire function scope and breaks every other
                    # success_response call in handle_onboard_v2.
                    # Single-sourced refusal shape (council fold, PR #610) —
                    # the status override is deliberate: Path B is not
                    # "you lack identity" but "your onboard is ambiguous".
                    from src.mcp_handlers.identity_bootstrap import (
                        strict_identity_refusal_payload,
                    )
                    return success_response(strict_identity_refusal_payload(
                        "onboard",
                        status="lineage_declaration_required",
                        hint=(
                            "Bare onboard() is ambiguous — pass "
                            "parent_agent_id=<prior UUID> to continue prior "
                            "work, OR force_new=true to confirm a fresh "
                            "process-instance with no lineage."
                        ),
                    ))
                logger.info(
                    "[ONBOARD] No existing session for session_key=%s... "
                    "minting fresh in-memory identity (spawn_reason=%s)",
                    base_session_key[:20], _spawn_reason,
                )
                # Mint via persist=False + force_new=True so the captured-
                # fresh branch below handles persistence + error surfacing
                # uniformly. The mint_guard inside _cache_session prevents
                # this fresh UUID from ratifying itself over a concurrently-
                # bound legitimate session in Redis.
                existing_identity = await resolve_session_identity(
                    base_session_key,
                    persist=False,
                    model_type=model_type,
                    client_hint=client_hint,
                    force_new=True,
                    parent_agent_id=_parent_agent_id,
                    spawn_reason=_spawn_reason,
                )
            else:
                return error_response(
                    existing_identity.get("message", "Could not resume identity"),
                    recovery={
                        "reason": "resume_failed",
                        "token_agent_uuid": existing_identity.get("token_agent_uuid"),
                        "hint": "Call onboard(force_new=true) to create a new identity.",
                    }
                )
        if existing_identity and not existing_identity.get("created"):
            if existing_identity.get("archived"):
                # ARCHIVED AGENT — auto-unarchive with same UUID (only when resume=True)
                if resume:
                    agent_uuid = existing_identity.get("agent_uuid")
                    agent_id = existing_identity.get("agent_id", agent_uuid)
                    label = existing_identity.get("label")
                    logger.info(f"[ONBOARD] Found archived agent {agent_uuid[:8]}... — auto-unarchiving")
                    try:
                        db = get_db()
                        await db.update_agent_fields(agent_uuid, status="active")
                        # S21-b §3: also write core.identities.status. The pre-S21-b
                        # auto-unarchive only touched core.agents, generating the
                        # 88-row identity='archived'/agent='active' inversion class
                        # the reconciler has to clean up. Mirror both surfaces here.
                        try:
                            await db.update_identity_status(agent_uuid, "active", None)
                        except Exception as _us_err:
                            logger.warning(f"[ONBOARD] Could not sync core.identities.status: {_us_err}")
                        try:
                            srv = get_mcp_server()
                            if agent_uuid in srv.agent_metadata:
                                srv.agent_metadata[agent_uuid].status = "active"
                                srv.agent_metadata[agent_uuid].archived_at = None
                        except Exception:
                            pass
                        try:
                            from src.cache import get_metadata_cache
                            await get_metadata_cache().invalidate(agent_uuid)
                        except Exception:
                            pass
                        # Wave 2 audit: force=True dropped per PR #350 precedent.
                        # The in-memory mutation 10 lines above (status='active',
                        # archived_at=None) is the actual consistency mechanism;
                        # the cache invalidation above covers external readers.
                        # The full PG reload here was redundant belt-and-suspenders
                        # and triggered 3221 sequential cache.set awaits per onboard
                        # of an archived agent.
                        try:
                            await srv.load_metadata_async()
                        except Exception:
                            pass
                        logger.info(f"[ONBOARD] Auto-unarchived agent {agent_uuid[:8]}...")
                        _was_archived = True
                    except Exception as e:
                        logger.warning(f"[ONBOARD] Could not unarchive agent: {e}")
                # If resume=False, archived agent is ignored — fall through to create new
            elif resume:
                # Explicit resume: reuse existing UUID
                agent_uuid = existing_identity.get("agent_uuid")
                agent_id = existing_identity.get("agent_id", agent_uuid)
                label = existing_identity.get("label")
                logger.info(f"[ONBOARD] Resuming existing identity {agent_uuid[:8]}... (explicit resume=true)")
            # If resume=False and not archived: existing_identity.created will be True
            # (resolve_session_identity with resume=False skips to PATH 3)
        else:
            # NEW AGENT - got a fresh identity from persist=False call
            # CRITICAL FIX (v2.5.7): Capture the fresh identity to persist it directly
            # instead of calling resolve_session_identity again (which could create a different UUID
            # if Redis caching failed silently)
            created_fresh_identity = True
            agent_uuid = existing_identity.get("agent_uuid")
            agent_id = existing_identity.get("agent_id", agent_uuid)
            logger.info(f"[ONBOARD] Created fresh identity {agent_uuid[:8]}... (will persist)")

            # Use base session key — model_type goes into metadata, not session key
            session_key = base_session_key
    else:
        # force_new requested — use base session key
        session_key = base_session_key

    # STEP 2: Handle resume flag (explicit consent to resume existing identity)
    # (resume was extracted earlier at STEP 1)
    # R2 PR 3 default — overridden by the created_fresh_identity and
    # force_new branches below when they run the cross-role pre-check.
    # The resume branch (an existing identity, no fresh declaration)
    # leaves it as "no_lineage_declared" by design: this onboard call
    # is not declaring a new lineage edge, so no PR 3 audit fires and
    # the response surfaces lineage_state=None / provisional_lineage
    # read from the row (the existing row's prior FSM state survives).
    _lineage_for_response: Optional[str] = "no_lineage_declared"
    if resume and existing_identity and not existing_identity.get("created"):
        # User explicitly chose to resume - use existing identity
        agent_uuid = existing_identity.get("agent_uuid")
        agent_id = existing_identity.get("agent_id", agent_uuid)
        agent_label = existing_identity.get("label")
        session_key = base_session_key  # Use base key, don't fork
        is_new = False
        identity = existing_identity
        logger.info(f"[ONBOARD] Resuming existing agent {agent_uuid[:8]}... (explicit resume=true)")
    elif created_fresh_identity:
        # CRITICAL FIX (v2.5.7): Persist the fresh identity we already created
        # instead of calling resolve_session_identity again (which could create a different UUID)
        try:
            # THREAD IDENTITY: Create/join thread for new agent
            _thread_id = None
            _thread_position = None
            try:
                _thread_id, _thread_position, _spawn_reason = await _assign_thread_for_new_agent(
                    arguments=arguments,
                    session_key=session_key,
                    agent_uuid=agent_uuid,
                    parent_agent_id=_parent_agent_id,
                    spawn_reason=_spawn_reason,
                    thread_id_hint=_thread_id_hint,
                )
            except Exception as e:
                logger.debug(f"[THREAD] Could not assign thread (non-fatal): {e}")

            # Persist the identity we got from the persist=False call
            newly_persisted = await ensure_agent_persisted(
                agent_uuid, session_key,
                parent_agent_id=_parent_agent_id,
                spawn_reason=_spawn_reason,
                thread_id=_thread_id,
                thread_position=_thread_position,
            )
            if newly_persisted:
                logger.info(f"[ONBOARD] Persisted fresh identity {agent_uuid[:8]}... to PostgreSQL")
                # Sync parent_agent_id to in-memory metadata for EISV inheritance
                if _parent_agent_id:
                    try:
                        from src.agent_metadata_model import agent_metadata as _agent_metadata
                        from src.agent_metadata_persistence import get_or_create_metadata
                        meta = get_or_create_metadata(agent_uuid)
                        meta.parent_agent_id = _parent_agent_id
                        meta.spawn_reason = _spawn_reason
                    except Exception as e:
                        logger.debug(f"[ONBOARD] Could not sync parent to metadata: {e}")
            else:
                logger.debug(f"[ONBOARD] Fresh identity {agent_uuid[:8]}... was already persisted")

            # R2 PR 3: cross-role pre-check + lineage_declared audit.
            # If the pre-check rejects (class mismatch), parent_agent_id
            # is cleared and the downstream lineage tasks are skipped —
            # there's no lineage edge to score, edge to draw, or genesis
            # to seed once the cross-role envelope vetoed it.
            if _parent_agent_id:
                _r2_state, _ = await _r2_pre_check_and_declare(
                    agent_uuid,
                    _parent_agent_id,
                    name,
                    mcp_server.agent_metadata.get(agent_uuid),
                    _spawn_reason,
                )
                if _r2_state in ("rejected_cross_role", "rejected_coincidental"):
                    _parent_agent_id = None
                    _lineage_for_response = _r2_state
                else:
                    _lineage_for_response = "provisional"
                    from src.background_tasks import create_tracked_task
                    # Create SPAWNED edge in AGE graph (non-blocking)
                    create_tracked_task(
                        _create_spawned_edge_bg(agent_uuid, _parent_agent_id, _spawn_reason),
                        name="spawned_edge",
                    )
                    # Q2 reseed: seed child's genesis from parent's trajectory_current
                    # so tier<=1 agents with lineage get a meaningful baseline rather
                    # than comparing their first 10 samples against themselves.
                    create_tracked_task(
                        _seed_genesis_from_parent_bg(agent_uuid, _parent_agent_id),
                        name="seed_genesis_from_parent",
                    )
                    # R1 v3.3-D `marks` policy: score declared lineage and stamp
                    # provisional on inconclusive verdicts. Fire-and-forget — onboard
                    # response must not block on the per-dim DTW + audit write.
                    create_tracked_task(
                        _score_lineage_continuity_bg(agent_uuid, _parent_agent_id),
                        name="score_lineage_continuity",
                    )
            else:
                _lineage_for_response = "no_lineage_declared"

            # Cache with the adjusted session_key (may include model suffix)
            await _cache_session(session_key, agent_uuid, display_agent_id=agent_id)

            identity = existing_identity
            identity["persisted"] = True
            identity["source"] = "created"
            is_new = True
            agent_label = None
        except Exception as e:
            logger.error(f"[ONBOARD] Failed to persist fresh identity: {e}")
            return error_response(f"Failed to persist identity: {e}")
    else:
        # STEP 2b: Get or create identity (using v2 logic).
        # The force_new branch persists via resolve_session_identity's own
        # create path, which — before 2026-04-21 — silently dropped declared
        # lineage. parent_agent_id / spawn_reason are now threaded through so
        # the create path mirrors ensure_agent_persisted's write.
        try:
            _thread_id = None
            _thread_position = None
            try:
                _thread_id, _thread_position, _spawn_reason = await _assign_thread_for_new_agent(
                    arguments=arguments,
                    session_key=session_key,
                    agent_uuid=None,
                    parent_agent_id=_parent_agent_id,
                    spawn_reason=_spawn_reason,
                    thread_id_hint=_thread_id_hint,
                )
            except Exception as e:
                logger.debug(f"[THREAD] Could not assign thread (non-fatal): {e}")

            identity = await resolve_session_identity(
                session_key,
                persist=True,  # Onboard always persists (it's an explicit "I am here" action)
                model_type=model_type,
                client_hint=client_hint,
                force_new=force_new,
                parent_agent_id=_parent_agent_id,
                spawn_reason=_spawn_reason,
                thread_id=_thread_id,
                thread_position=_thread_position,
            )
            agent_uuid = identity["agent_uuid"]
            agent_id = identity.get("agent_id", agent_uuid)
            is_new = identity.get("created", False) or force_new
            agent_label = identity.get("label")

            # Mirror the created_fresh_identity branch: sync lineage into
            # in-memory metadata (EISV inheritance) and create the SPAWNED
            # edge in AGE. Without this the force_new branch would have DB
            # lineage but no trajectory/graph continuity.
            if is_new and _parent_agent_id:
                try:
                    from src.agent_metadata_persistence import get_or_create_metadata
                    meta = get_or_create_metadata(agent_uuid)
                    meta.parent_agent_id = _parent_agent_id
                    meta.spawn_reason = _spawn_reason
                except Exception as e:
                    logger.debug(f"[ONBOARD] Could not sync parent to metadata (force_new branch): {e}")
                # R2 PR 3: cross-role pre-check + lineage_declared audit.
                # Mirrors the created_fresh_identity branch above.
                _r2_state, _ = await _r2_pre_check_and_declare(
                    agent_uuid,
                    _parent_agent_id,
                    name,
                    mcp_server.agent_metadata.get(agent_uuid),
                    _spawn_reason,
                )
                if _r2_state in ("rejected_cross_role", "rejected_coincidental"):
                    _parent_agent_id = None
                    _lineage_for_response = _r2_state
                else:
                    _lineage_for_response = "provisional"
                    try:
                        from src.background_tasks import create_tracked_task
                        create_tracked_task(
                            _create_spawned_edge_bg(agent_uuid, _parent_agent_id, _spawn_reason),
                            name="spawned_edge",
                        )
                        # Q2 reseed: mirror the created_fresh_identity branch.
                        create_tracked_task(
                            _seed_genesis_from_parent_bg(agent_uuid, _parent_agent_id),
                            name="seed_genesis_from_parent",
                        )
                        # R1 v3.3-D `marks` policy: mirror the created_fresh_identity
                        # branch. See _score_lineage_continuity_bg for contract.
                        create_tracked_task(
                            _score_lineage_continuity_bg(agent_uuid, _parent_agent_id),
                            name="score_lineage_continuity",
                        )
                    except Exception as e:
                        logger.debug(f"[ONBOARD] Could not schedule SPAWNED edge (force_new branch): {e}")
            else:
                _lineage_for_response = "no_lineage_declared"
        except Exception as e:
            logger.error(f"onboard() failed to create identity: {e}")
            return error_response(f"Failed to create identity: {e}")

    # CRITICAL: Update request context so signature in response matches new identity
    try:
        from ..context import update_context_agent_id
        update_context_agent_id(agent_uuid)
    except Exception as e:
        logger.debug(f"Could not update context in onboard: {e}")

    # Refresh stale structured IDs when runtime model/client clearly changed.
    # Keeps UUID continuity while fixing misattribution (e.g., Claude label in Cursor+Codex).
    if _should_rebadge_agent_id(agent_id, model_type, client_hint):
        refreshed_agent_id = _generate_agent_id(model_type, client_hint)
        if refreshed_agent_id and refreshed_agent_id != agent_id:
            await _persist_rebadged_agent_id(agent_uuid, refreshed_agent_id)
            agent_id = refreshed_agent_id
            identity["agent_id"] = refreshed_agent_id
            logger.info(f"[ONBOARD] Rebadged agent_id -> {refreshed_agent_id}")

    # Set label if requested (and different from current)
    if name and name != agent_label:
        success = await set_agent_label(agent_uuid, name, session_key=session_key)
        if success:
            agent_label = name
            # Refresh identity object
            identity["label"] = name
        else:
            logger.warning(f"[ONBOARD] set_agent_label returned False for {agent_uuid[:8]}... name={name}")
            # Fallback: use the name for this response even if DB persistence failed
            if agent_label is None:
                agent_label = name
                identity["label"] = name

    # S8a Phase-1: default-stamp class tag on fresh identities so the class
    # partition (ephemeral / resident / ...) is populated from onboard rather
    # than left to out-of-band SDK writes that only fire for resident
    # subclasses. Rule lives in src/grounding/onboard_classifier.py; see
 # .
    if is_new:
        try:
            await _stamp_default_tags_on_onboard(agent_uuid, name)
        except Exception as e:
            logger.debug(f"[ONBOARD] default-stamp failed (non-fatal): {e}")

    # CONCURRENT IDENTITY BINDING (#123): if the client declared a process
    # fingerprint, record the binding and run audit-only collision detection.
    # Fire-and-forget — declaration-only, never resolves or recovers identity.
    _raw_fp = arguments.get("process_fingerprint")
    if _raw_fp is not None:
        try:
            from .process_binding import validate_fingerprint, record_binding_bg
            _fp = validate_fingerprint(_raw_fp)
            if _fp is not None:
                from src.background_tasks import create_tracked_task

                # PPID LINEAGE VERIFICATION (#128): if parent_agent_id and
                # ppid are both present, sequence the verifier after the
                # binding upsert so the UPDATE has a row to write to.
                # Otherwise just schedule the binding alone.
                _do_verify = bool(_parent_agent_id) and bool(_fp.ppid)
                if _do_verify:
                    from .lineage_verification import verify_lineage_bg

                    async def _record_then_verify(
                        agent_uuid=agent_uuid, fp=_fp,
                        client_session_id=arguments.get("client_session_id"),
                        parent_uuid=_parent_agent_id,
                    ):
                        await record_binding_bg(agent_uuid, fp, client_session_id)
                        await verify_lineage_bg(
                            child_uuid=agent_uuid,
                            parent_uuid=parent_uuid,
                            child_host_id=fp.host_id,
                            child_ppid=fp.ppid,
                            child_pid=fp.pid,
                            child_pid_start_time=fp.pid_start_time,
                            child_transport=fp.transport,
                        )

                    create_tracked_task(
                        _record_then_verify(),
                        name="record_and_verify_process_binding",
                    )
                else:
                    create_tracked_task(
                        record_binding_bg(
                            agent_uuid,
                            _fp,
                            arguments.get("client_session_id"),
                        ),
                        name="record_process_binding",
                    )
        except Exception as e:
            logger.debug(f"[PROCESS_BINDING] onboard scheduling failed (non-fatal): {e}")

    # TRAJECTORY IDENTITY: Store genesis signature if provided (optional, non-blocking)
    # Agents from anima-mcp can include trajectory_signature in their onboard call
    trajectory_result = None
    trajectory_signature = arguments.get("trajectory_signature")
    if trajectory_signature and isinstance(trajectory_signature, dict):
        try:
            from src.trajectory_identity import TrajectorySignature, store_genesis_signature
            sig = TrajectorySignature.from_dict(trajectory_signature)
            stored = await store_genesis_signature(agent_uuid, sig)
            if stored:
                trajectory_result = {
                    "genesis_stored": True,
                    "confidence": sig.identity_confidence,
                    "observations": sig.observation_count,
                }
                logger.info(f"[TRAJECTORY] Stored genesis for {agent_uuid[:8]}... at onboard")
        except Exception as e:
            logger.debug(f"[TRAJECTORY] Could not store genesis at onboard: {e}")
            # Non-blocking - trajectory is optional

    # STEP 3: Generate stable session ID
    # Import helper to ensure consistent format
    from .shared import make_client_session_id
    stable_session_id = make_client_session_id(agent_uuid)

    # STEP 4: Register binding under stable session ID (in v2 cache)
    # This allows future calls using stable_session_id to find the agent
    # even if the transport session key changes
    await _cache_session(stable_session_id, agent_uuid, display_agent_id=agent_id)

    # Persist the stable session ID too, not just the transport/base session key.
    # Otherwise a Redis miss on the returned client_session_id can fall through to
    # PATH 3 and create an unrelated UUID.
    await _perform_session_bind(
        agent_uuid,
        stable_session_id,
        display_agent_id=agent_id,
        source="onboard_stable_session",
    )

    # Also register in O(1) prefix index (legacy support)
    try:
        from .shared import _register_uuid_prefix
        uuid_prefix = agent_uuid[:12]
        _register_uuid_prefix(uuid_prefix, agent_uuid)
    except ImportError:
        pass

    # STEP 4b: Pin onboard identity for transport-level session continuity
    # When Claude.ai doesn't pass client_session_id, dispatch_tool() can
    # use this pin to inject the correct session ID based on transport fingerprint.
    # This prevents knowledge graph attribution from scattering across random UUIDs.
    #
    # Subagent onboards write the pin only-if-absent: a spawned helper
    # shares the driver's exact fingerprint, and an unconditional write
    # here CAPTURES the driver's argument-less resolution for the rest of
    # the session (incident 2026-06-10 — see SUBAGENT_PIN_NX_SPAWN_REASONS
    # in identity/session.py for the full mechanism).
    #
    # EXPLICITLY-DECLARED spawn_reason only — never the inferred
    # `_spawn_reason`. infer_spawn_reason() classifies a succession
    # onboard (parent_agent_id declared, spawn_reason omitted, thread
    # has prior nodes) as "subagent" whenever client_hint isn't in the
    # arguments, which would NX-block a legitimate fresh DRIVER behind
    # its dead predecessor's still-live pin — the same wrong-resolution
    # bug in the opposite direction (council block, PR #604). The cost:
    # a subagent that omits spawn_reason keeps today's displacing write;
    # that boundary is documented on the constant and in
    # docs/ontology/identity.md.
    try:
        logger.debug(f"[ONBOARD_PIN] base_session_key={base_session_key!r}")
        base_fp = _extract_base_fingerprint(base_session_key)
        from .session import SUBAGENT_PIN_NX_SPAWN_REASONS
        await set_onboard_pin(
            base_fp,
            agent_uuid,
            stable_session_id,
            client_hint=client_hint,
            model_type=model_type,
            user_agent=signals.user_agent if signals else None,
            if_absent=arguments.get("spawn_reason") in SUBAGENT_PIN_NX_SPAWN_REASONS,
        )
    except Exception as e:
        logger.warning(f"[ONBOARD_PIN] Could not set pin: {e}")

    # STEP 5: Build thread context (async — must happen before sync helper)
    thread_context = None
    try:
        db = get_db()
        thread_info = await db.get_agent_thread_info(agent_uuid)
        if thread_info and thread_info.get("thread_id"):
            _tid = thread_info["thread_id"]
            all_nodes = await db.get_thread_nodes(_tid)
            from src.thread_identity import build_fork_context
            thread_context = build_fork_context(
                thread_id=_tid,
                position=thread_info.get("thread_position", 1),
                parent_uuid=thread_info.get("parent_agent_id") or _parent_agent_id,
                spawn_reason=_spawn_reason,
                all_nodes=all_nodes,
                agent_uuid=agent_uuid,
            )
    except Exception as e:
        logger.debug(f"[THREAD] Could not build thread context: {e}")

    # STEP 6: Build response
    verbose = coerce_bool(arguments.get("verbose"), default=False)
    # #734: lean onboard envelope is now the DEFAULT. "minimal" drops the
    # nested identity ontology (identity_context) and the descriptive extras
    # (session_resolution_source, continuity_token_supported, date_context),
    # keeping uuid / agent_id / client_session_id / a single identity_assurance
    # block / trajectory genesis+trust_tier / lineage flags / continuity_token /
    # next_step. Callers that need the full self-description pass
    # response_mode="full". The functional fields the plugin/dashboard rely on
    # (uuid, client_session_id) are retained in both modes.
    #
    # verbose=True is the legacy "give me everything" signal; honor it as full
    # so existing verbose callers keep the next_calls/session_continuity/
    # tool_mode/workflow extras (minimal returns before those are built). An
    # explicit response_mode always wins over the verbose-derived default.
    response_mode = str(
        arguments.get("response_mode") or ("full" if verbose else "minimal")
    ).strip().lower()
    if response_mode not in {"full", "minimal"}:
        response_mode = "full" if verbose else "minimal"
    try:
        from ..context import get_session_resolution_source, get_session_proof_origin
        continuity_source = get_session_resolution_source()
        proof_origin = get_session_proof_origin()
    except Exception:
        continuity_source = None
        proof_origin = None
    continuity_support = continuity_token_support_status()
    continuity_token = create_continuity_token(
        agent_uuid,
        stable_session_id,
        model_type=model_type,
        client_hint=client_hint,
    )

    public_agent_id = agent_id if agent_id and agent_id != agent_uuid else None
    structured_id = None
    try:
        # Atomic read via .get — avoids TOCTOU between `in` check and subscript
        # if another task mutates agent_metadata concurrently.
        meta = mcp_server.agent_metadata.get(agent_uuid)
        if meta is not None:
            public_agent_id = getattr(meta, "public_agent_id", None) or public_agent_id
            structured_id = getattr(meta, 'structured_id', None)
    except Exception:
        pass
    response_agent_id = public_agent_id or structured_id or f"agent_{agent_uuid[:8]}"
    identity_resolution_outcome = (
        identity.get("identity_resolution_outcome")
        if isinstance(identity, dict)
        else None
    )
    if not identity_resolution_outcome:
        if is_new and _spawn_reason in {
            "dispatch_auto_mint",
            "auto_onboard_no_session",
            "orchestrated_thread_anchor",
        }:
            identity_resolution_outcome = "minted_after_resume_miss"
        elif is_new and force_new:
            identity_resolution_outcome = "minted_force_new"
        elif is_new:
            identity_resolution_outcome = "minted_fresh"
        else:
            identity_resolution_outcome = "resumed"

    tool_mode_info = None
    if verbose:
        try:
            from src.tool_modes import TOOL_MODE, get_tools_for_mode
            from src.tool_schemas import get_tool_definitions
            all_tools = get_tool_definitions()
            mode_tools = get_tools_for_mode(TOOL_MODE)
            tool_mode_info = {
                "current_mode": TOOL_MODE,
                "visible_tools": len(mode_tools),
                "total_tools": len(all_tools),
                "available_modes": ["minimal", "lite", "full"],
                "tip": f"You're seeing {len(mode_tools)}/{len(all_tools)} tools in '{TOOL_MODE}' mode. Use list_tools() for discovery, or ask for ?mode=full if you need more."
            }
        except Exception as e:
            logger.debug(f"Could not add tool_mode info: {e}")

    # R2 PR 3: read the post-pre-check lineage state from the row so
    # the response surfaces both the per-call decision (lineage_state)
    # and the persisted column (provisional_lineage). On the resume
    # branch these come from the existing row; on the fresh/forced
    # branches they reflect what _r2_pre_check_and_declare just stamped.
    # `isinstance(..., dict)` guards against test backends that return
    # AsyncMock auto-children instead of None (per the conftest leak-
    # detection contract noted at L143 — bare get() on a Mock surfaces
    # a coroutine that the response builder never awaits).
    _r2_provisional_lineage = False
    try:
        _r2_lineage_row = await get_db().read_lineage_state(agent_uuid)
        if isinstance(_r2_lineage_row, dict):
            _r2_provisional_lineage = bool(_r2_lineage_row.get("provisional_lineage"))
            # Resume branch fallback: if the row already has a parent and
            # the FSM hasn't moved it to a terminal state, surface the
            # row's lineage state in the response. The fresh/forced
            # branches set _lineage_for_response explicitly above and
            # we honor that — only override when this onboard call was
            # the resume branch (no fresh declaration this call).
            # Council fix: delegated to `derive_lineage_state` so the
            # cascade lives in one place (shared with identity()).
            if _lineage_for_response == "no_lineage_declared" and _r2_lineage_row.get("parent_agent_id"):
                from src.identity.lineage_lifecycle import derive_lineage_state
                _derived = derive_lineage_state(_r2_lineage_row)
                if _derived is not None:
                    _lineage_for_response = _derived
    except Exception as e:
        logger.debug(f"[R2] read_lineage_state failed (non-fatal): {e}")

    result = build_onboard_response_data(
        agent_uuid=agent_uuid,
        response_agent_id=response_agent_id,
        agent_label=agent_label,
        stable_session_id=stable_session_id,
        is_new=is_new,
        force_new=force_new,
        client_hint=client_hint,
        was_archived=_was_archived,
        trajectory_result=trajectory_result,
        parent_agent_id=_parent_agent_id,
        thread_context=thread_context,
        verbose=verbose,
        continuity_source=continuity_source,
        continuity_support=continuity_support,
        continuity_token=continuity_token,
        identity_resolution_outcome=identity_resolution_outcome,
        system_activity=_get_system_evidence() if verbose else None,
        tool_mode_info=tool_mode_info,
        lineage_state=_lineage_for_response,
        provisional_lineage=_r2_provisional_lineage,
        proof_origin=proof_origin,
        response_mode=response_mode,
    )

    # Bootstrap check-in (onboard-bootstrap-checkin §3.5). Conditional on
    # initial_state being present in the request — the response gains a
    # `bootstrap` key only when the caller asked for a bootstrap row. The
    # write itself is timeout-bounded and fail-open per §3.5.
    _initial_state = arguments.get("initial_state")
    if _initial_state is not None:
        try:
            from src.mcp_handlers.identity.bootstrap_checkin import write_bootstrap
            from src.mcp_handlers.schemas.core import BootstrapStateParams
            _bootstrap_params = (
                _initial_state if isinstance(_initial_state, BootstrapStateParams)
                else BootstrapStateParams(**_initial_state)
            )
            db = get_db()
            _identity_record = await db.get_identity(agent_uuid)
            if _identity_record is not None:
                result["bootstrap"] = await write_bootstrap(
                    db,
                    identity_id=_identity_record.identity_id,
                    agent_id=agent_uuid,
                    params=_bootstrap_params,
                    client_hint=client_hint,
                    purpose=arguments.get("purpose"),
                )
        except Exception as _bootstrap_err:  # noqa: BLE001 — bootstrap is fail-open
            logger.warning(
                f"[BOOTSTRAP] onboard initial_state handling failed (non-fatal): {_bootstrap_err}"
            )

    # S1-a (2026-04-24): grace-period deprecation surface. onboard() called
    # with continuity_token AND without force_new=true is the retired
    # cross-process-instance resume path. `force_new` is set once at the top
    # of this handler (L1180) from arguments and never reassigned — signal is
    # honest. Non-string token inputs (adversarial list/dict/bytes) would
    # otherwise inflate grace-period telemetry without holding a real token.
 # / §6.
    _caller_token = arguments.get("continuity_token")
    if not isinstance(_caller_token, str) or not _caller_token:
        _caller_token = None
    _emit_continuity_token_deprecation(
        response_dict=result,
        used_token_for_resume=(_caller_token is not None) and not force_new,
        token_str=_caller_token,
        agent_uuid=agent_uuid,
        response_agent_id=response_agent_id,
        client_hint=client_hint,
        model_type=model_type,
    )

    # Temporal narrator — contextual time awareness (silence by default)
    try:
        from src.temporal import build_temporal_context
        temporal = await build_temporal_context(agent_uuid, get_db())
        if temporal:
            result["temporal_context"] = temporal
    except Exception:
        pass  # Temporal narrator is non-critical

    logger.info(f"[ONBOARD] Agent {agent_uuid[:8]}... onboarded (is_new={is_new}, label={agent_label})")

    # Identity-resolution observation event. One per successful onboard, used
    # later to answer "is the 30-min sliding pin TTL bleeding into
    # continuity-token-only resumes?" Pin shadow fields are populated when
    # the IP/UA pin path didn't win but a fingerprint signal was present.
 # and the audit-log
    # schema in src/audit_log.py:log_identity_resolution_observed.
    try:
        import time as _ires_time
        from src.audit_log import audit_logger as _ires_audit
        from ..context import (
            get_session_resolution_source,
            get_pin_match_scope,
            get_shadow_pin_observation,
        )
        _ires_token = arguments.get("continuity_token")
        _ires_iat: Optional[int] = None
        _ires_exp: Optional[int] = None
        _ires_age: Optional[int] = None
        if _ires_token:
            _ires_iat = extract_token_iat(str(_ires_token))
            from .session import extract_token_exp as _extract_exp
            _ires_exp = _extract_exp(str(_ires_token))
            if _ires_iat is not None:
                _ires_age = max(0, int(_ires_time.time()) - int(_ires_iat))
        _ires_shadow = get_shadow_pin_observation()
        _ires_audit.log_identity_resolution_observed(
            agent_uuid=agent_uuid,
            resolution_source=get_session_resolution_source(),
            pin_match_scope=get_pin_match_scope(),
            pin_entry_present=_ires_shadow["pin_entry_present"],
            pin_fingerprint_match=_ires_shadow["pin_fingerprint_match"],
            pin_entry_age_seconds=_ires_shadow["pin_entry_age_seconds"],
            token_iat=_ires_iat,
            token_exp=_ires_exp,
            token_age_seconds=_ires_age,
        )
    except Exception as _ires_err:  # pragma: no cover — defensive
        logger.debug(f"[IRES] identity_resolution_observed write failed (non-fatal): {_ires_err}")

    # Identity Honesty Part C: onboard-triggered orphan sweep REMOVED.
    # It was the driver of 'agent archived almost immediately' — catching
    # siblings of fresh onboards via the 2h zero_update_hours heuristic.
    # With ghost creation gated upstream (PATH 0 + FALLBACK 2), the nightly
    # sweep in src/background_tasks.py is sufficient. Users who want an
    # immediate sweep can still call the archive_orphan_agents tool.

    # Use lite_response to skip redundant signature
    arguments["lite_response"] = True
    return success_response(result, agent_id=agent_uuid, arguments=arguments)

# =============================================================================
# TRAJECTORY IDENTITY VERIFICATION TOOL
# =============================================================================

@mcp_tool("verify_trajectory_identity", timeout=10.0)
async def handle_verify_trajectory_identity(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    VERIFY_TRAJECTORY_IDENTITY - Two-tier identity verification via trajectory signature.

    Verifies agent identity using the Trajectory Identity framework:
    - Tier 1 (Coherence): Compare to recent signature (short-term consistency)
    - Tier 2 (Lineage): Compare to genesis signature (long-term identity continuity)

    Args:
        trajectory_signature: The current trajectory signature to verify (dict with:
            preferences, beliefs, attractor, recovery, relational, stability_score,
            identity_confidence, observation_count)
        coherence_threshold: Optional threshold for Tier 1 (default: 0.7)
        lineage_threshold: Optional threshold for Tier 2 (default: 0.6)

    Returns:
        Verification result with tier details and overall verdict.
    """
    # Get agent UUID from context
    from ..context import get_context_agent_id
    agent_uuid = get_context_agent_id()

    if not agent_uuid:
        return error_response("Identity not resolved. Call identity() or onboard() first.")

    trajectory_signature = arguments.get("trajectory_signature")
    if not trajectory_signature or not isinstance(trajectory_signature, dict):
        return error_response(
            "trajectory_signature is required",
            recovery={
                "action": "Include your trajectory signature from anima-mcp",
                "example": "verify_trajectory_identity(trajectory_signature={...})"
            }
        )

    coherence_threshold = arguments.get("coherence_threshold", 0.7)
    lineage_threshold = arguments.get("lineage_threshold", 0.6)

    try:
        from src.trajectory_identity import TrajectorySignature, verify_trajectory_identity

        sig = TrajectorySignature.from_dict(trajectory_signature)
        result = await verify_trajectory_identity(
            agent_uuid,
            sig,
            coherence_threshold=coherence_threshold,
            lineage_threshold=lineage_threshold
        )

        if result.get("error"):
            return error_response(result["error"])

        return success_response(result, agent_id=agent_uuid, arguments=arguments)

    except Exception as e:
        logger.error(f"[TRAJECTORY] Verification failed: {e}")
        return error_response(f"Trajectory verification failed: {e}")

@mcp_tool("get_trajectory_status", timeout=10.0)
async def handle_get_trajectory_status(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    GET_TRAJECTORY_STATUS - Check trajectory identity status for an agent.

    Returns information about the agent's trajectory identity including:
    - Whether genesis signature exists
    - Current signature details
    - Lineage similarity (if both exist)
    - Drift detection status

    No arguments required - uses current session identity.
    """
    # Get agent UUID from context
    from ..context import get_context_agent_id
    agent_uuid = get_context_agent_id()

    if not agent_uuid:
        return error_response("Identity not resolved. Call identity() or onboard() first.")

    try:
        from src.trajectory_identity import get_trajectory_status

        result = await get_trajectory_status(agent_uuid)

        if result.get("error"):
            return error_response(result["error"])

        # Add trust tier to status response (S6 Option B: substrate-earned routing)
        try:
            from src.identity.trust_tier_routing import resolve_trust_tier
            from src.db import get_db
            identity = await get_db().get_identity(agent_uuid)
            if identity and identity.metadata:
                _meta = mcp_server.agent_metadata.get(agent_uuid)
                result["trust_tier"] = await resolve_trust_tier(
                    agent_uuid,
                    identity.metadata,
                    prefetched_tags=getattr(_meta, "tags", None) if _meta else None,
                    prefetched_label=getattr(_meta, "label", None) if _meta else None,
                )
        except Exception:
            pass

        return success_response(result, agent_id=agent_uuid, arguments=arguments)

    except Exception as e:
        logger.error(f"[TRAJECTORY] Status check failed: {e}")
        return error_response(f"Trajectory status check failed: {e}")

async def _create_spawned_edge_bg(
    child_id: str, parent_id: str, reason: str | None
):
    """Create SPAWNED edge in AGE graph (fire-and-forget background task)."""
    try:
        db = get_db()
        from src.db.age_queries import create_spawned_edge, create_agent_node
        # Ensure both Agent nodes exist
        q, p = create_agent_node(parent_id)
        await db.graph_query(q, p)
        q, p = create_agent_node(child_id)
        await db.graph_query(q, p)
        # Create edge
        q, p = create_spawned_edge(parent_id, child_id, spawn_reason=reason)
        await db.graph_query(q, p)
        logger.info(f"[SPAWNED] Created edge {parent_id[:8]}... -> {child_id[:8]}...")
    except Exception as e:
        logger.debug(f"SPAWNED edge creation failed (non-fatal): {e}")


async def _stamp_default_tags_on_onboard(agent_uuid: str, name: Optional[str]) -> None:
    """Stamp default class tags from the onboard handler. Thin wrapper around
    ``stamp_default_class_tags`` in ``src.grounding.onboard_classifier``;
    keeps the original log line shape so onboard-specific log filters keep
    working. Phase-2 (2026-04-30) added the same stamp call to the
    auto-create sites in ``src/mcp_handlers/updates/phases.py`` directly,
    skipping this wrapper.
    """
    from src.grounding.onboard_classifier import stamp_default_class_tags

    meta = mcp_server.agent_metadata.get(agent_uuid)
    stamped = await stamp_default_class_tags(agent_uuid, name, meta=meta)
    if stamped is not None:
        logger.info(
            f"[ONBOARD] S8a default-stamp: {agent_uuid[:8]}... tagged {stamped} (name={name!r})"
        )


async def _seed_genesis_from_parent_bg(child_id: str, parent_id: str):
    """Seed child's trajectory_genesis from parent's trajectory_current.

 Ontology v2 Q2 reseed — R3-appendix and
    `src/trajectory_identity.seed_genesis_from_parent`. Primitive is
    permissive (no-op if parent lacks trajectory_current, refuses to
    clobber a tier>=2 genesis), so fire-and-forget here is safe.
    """
    try:
        from src.trajectory_identity import seed_genesis_from_parent
        result = await seed_genesis_from_parent(child_id, parent_id)
        if result.get("seeded"):
            logger.info(
                f"[SEED_GENESIS] Seeded {child_id[:8]}... genesis "
                f"from parent {parent_id[:8]}..."
            )
        else:
            logger.debug(
                f"[SEED_GENESIS] Skipped {child_id[:8]}... <- "
                f"{parent_id[:8]}...: {result.get('reason')}"
            )
    except Exception as e:
        logger.debug(f"seed_genesis_from_parent scheduling failed (non-fatal): {e}")


async def _r2_pre_check_and_declare(
    agent_uuid: str,
    parent_id: str,
    name: Optional[str],
    meta: Optional[Any],
    spawn_reason: Optional[str] = None,
) -> tuple[str, Optional[Dict[str, Any]]]:
    """R2 PR 3 — cross-role pre-check + lineage_declared emission.

    Returns ``(lineage_state, rejection_details)``:
      - ``("provisional", None)`` — declaration accepted; the row's
        ``lineage_declared_at`` was stamped (idempotent), the
        ``lineage_declared`` audit fired. Caller continues with
        existing lineage dispatches (R1 score, SPAWNED edge,
        genesis seed).
      - ``("rejected_cross_role", details)`` — class mismatch.
        ``parent_agent_id`` was cleared from ``core.identities`` and
        from in-memory metadata; ``lineage_cross_role_rejected`` audit
        fired. Caller MUST skip the downstream lineage dispatches —
        they would re-declare the lineage we just rejected.
      - ``("provisional", None)`` is also returned on the orphan/charitable
        accept path (parent or successor without a class tag).

    The class for the cross-role check is derived from the same
    classifier the onboard handler uses to stamp default tags later
    (`default_tags_for_onboard`). The classifier returns ``None`` when
    the caller already supplied tags; in that case we use the caller-
    supplied tags directly. The first tag is the "primary class" for
    envelope-comparison purposes (see
    ``src/grounding/onboard_classifier.py``).

    All DB and audit failures are caught + logged; this helper does
    NOT propagate exceptions. The lineage state returned reflects what
    actually committed: if `declare_lineage` raises, we still return
    ``"provisional"`` because the cross-role check passed and the
    caller should still dispatch (the FSM will pick up the missing
    declaration on the next sweep — `read_lineage_state` will return
    the row regardless).

    Awaiting DB calls from inside the handler matches the pattern
    already established in the surrounding onboard flow (e.g.
    ``set_agent_label`` at L1810, ``ensure_agent_persisted`` at
    L1680). Per CLAUDE.md the anyio-asyncpg deadlock is a concern
    for *new* MCP handlers; the onboard handler's existing posture
    on this is the precedent we follow.
    """
    from src.identity.lineage_lifecycle import (
        pre_check_cross_role,
        _emit_audit,
    )
    from src.grounding.onboard_classifier import default_tags_for_onboard
    from src.db import get_db

    backend = get_db()

    # Liveness pre-check (declaration-time concurrent-sibling guard).
    # Declaring parent_agent_id attests ancestry, not that the parent exited.
    # If the named parent is a CURRENTLY-LIVE process, the declarant is a
    # concurrent sibling, not a successor — minting the edge is what produced
    # the 2026-06-14 false-archival chain (1b4172bb -> ad111882 -> d8c219dd).
    # Reject (clear + audit), mirroring the cross-role path. Symmetric with
    # PR #720's archival-time liveness guard.
    #   - subagent: exempt — the dispatcher is alive by design.
    #   - compaction: exempt — the same live session continuing past a context
    #     boundary legitimately has a live "parent".
    #   - explicit / new_session: a live parent means concurrent sibling → reject.
    #     A dead parent stays provisional and R1 adjudicates (preserves the
    #     genuine serial-handoff signal).
    # Best-effort: get_live_bindings returns [] on DB error → treated as
    # not-live → allow, same fail-open posture as #720.
    if spawn_reason not in ("subagent", "compaction"):
        from src.mcp_handlers.identity.process_binding import get_live_bindings
        parent_uuid = parent_id
        try:
            _prec = await backend.get_identity(parent_id)
            if _prec:
                parent_uuid = (
                    _prec.get("agent_uuid")
                    or _prec.get("id")
                    or _prec.get("uuid")
                    or parent_id
                )
        except Exception:
            pass
        try:
            live_bindings = await get_live_bindings(parent_uuid)
        except Exception as e:
            logger.warning(
                f"[R2] liveness pre-check failed for {agent_uuid[:8]}...: {e}"
            )
            live_bindings = []
        if live_bindings:
            try:
                await backend.clear_lineage_declaration(agent_uuid)
            except Exception as e:
                logger.warning(
                    f"[R2] coincidental: failed to clear lineage declaration "
                    f"for {agent_uuid[:8]}...: {e}"
                )
            if meta is not None:
                try:
                    meta.parent_agent_id = None
                    meta.spawn_reason = None
                except Exception:
                    pass
            try:
                await _emit_audit(
                    "lineage_coincidental_rejected",
                    agent_uuid,
                    details={
                        "claimed_parent_id": parent_id,
                        "reason": "parent_live_at_declaration",
                        "spawn_reason": spawn_reason,
                        "live_binding_count": len(live_bindings),
                    },
                )
            except Exception as e:
                logger.warning(
                    f"[R2] coincidental audit emit failed for "
                    f"{agent_uuid[:8]}...: {e}"
                )
            logger.info(
                f"[R2] Coincidental lineage rejected: {agent_uuid[:8]}... -> "
                f"{parent_id[:8]}... (parent live: {len(live_bindings)} "
                f"binding(s), spawn_reason={spawn_reason})"
            )
            return "rejected_coincidental", None

    # Determine the successor's would-be primary class. The classifier
    # returns None when existing_tags is non-empty (caller-asserted
    # class) — in that branch use the caller's tags directly so the
    # check still runs with a real class, not a None.
    existing_tags = getattr(meta, "tags", None) if meta is not None else None
    successor_tags = default_tags_for_onboard(name, existing_tags)
    if successor_tags is None and existing_tags:
        successor_tags = list(existing_tags)
    successor_class = successor_tags[0] if successor_tags else None

    rejection = await pre_check_cross_role(parent_id, successor_class)

    if rejection is not None:
        # Clear parent_agent_id AND spawn_reason from the storage row
        # first so the downstream FSM never reads them back. PR 3
        # council fix (reviewer #2): the original rejection path only
        # cleared parent_agent_id; spawn_reason became asymmetric.
        # `clear_lineage_declaration` keeps both columns in sync.
        try:
            await backend.clear_lineage_declaration(agent_uuid)
        except Exception as e:
            logger.warning(
                f"[R2] cross-role: failed to clear lineage declaration for "
                f"{agent_uuid[:8]}...: {e}"
            )
        # Clear in-memory metadata so the same handler call's response
        # reflects the rejection (e.g. predecessor block omitted).
        # Council fix: also clear spawn_reason for symmetry with the
        # storage-side clear above.
        if meta is not None:
            try:
                meta.parent_agent_id = None
                meta.spawn_reason = None
            except Exception:
                pass
        # Emit audit event. _emit_audit is fail-soft inside; this
        # outer try is defense-in-depth against import-time errors.
        try:
            await _emit_audit(
                "lineage_cross_role_rejected",
                agent_uuid,
                details={
                    **rejection,
                    "claimed_parent_id": parent_id,
                },
            )
        except Exception as e:
            logger.warning(
                f"[R2] cross-role audit emit failed for "
                f"{agent_uuid[:8]}...: {e}"
            )
        logger.info(
            f"[R2] Cross-role rejected: {agent_uuid[:8]}... -> "
            f"{parent_id[:8]}... (parent={rejection['parent_class']}, "
            f"successor={rejection['successor_class']})"
        )
        return "rejected_cross_role", rejection

    # PR 3 council fix (architect F1): if the row is already in a
    # terminal state (archived after grace expiry, or demoted), the
    # FSM's terminal-state guard would permanently skip evaluation
    # even after `declare_lineage` stamps a fresh
    # `lineage_declared_at` — `lineage_archived_at` /
    # `lineage_demoted_at` are still set, so the FSM short-circuits
    # to `skipped_reason="terminal_state"` and the lineage is
    # silently dead while the response surfaces "provisional".
    # Reset terminal markers atomically here so re-onboarding actually
    # re-enters the FSM with a clean slate. Audit anchor for the prior
    # terminal state survives in `audit.events` (lineage_grace_expired
    # / lineage_demoted from the FSM's prior tick).
    try:
        existing_state = await backend.read_lineage_state(agent_uuid)
        if existing_state and (
            existing_state.get("lineage_archived_at") is not None
            or existing_state.get("lineage_demoted_at") is not None
        ):
            try:
                reset = await backend.reset_lineage_for_redeclaration(agent_uuid)
                if reset:
                    logger.info(
                        f"[R2] reset_lineage_for_redeclaration: cleared terminal "
                        f"state for {agent_uuid[:8]}... (re-declaring lineage to "
                        f"{parent_id[:8]}...)"
                    )
            except Exception as e:
                logger.warning(
                    f"[R2] reset_lineage_for_redeclaration failed: {e}"
                )
    except Exception as e:
        logger.debug(
            f"[R2] read_lineage_state pre-redeclare check failed (non-fatal): {e}"
        )

    # Accept path — stamp lineage_declared_at (idempotent) + emit
    # the lineage_declared audit. The FSM (PR 2) and sweeper (PR 4)
    # use lineage_declared_at as the grace-window anchor; an unstamped
    # row would archive immediately on first eval.
    try:
        await backend.declare_lineage(agent_uuid)
    except Exception as e:
        logger.warning(
            f"[R2] declare_lineage failed for {agent_uuid[:8]}...: {e}"
        )
    try:
        await _emit_audit(
            "lineage_declared",
            agent_uuid,
            details={
                "parent_id": parent_id,
                "successor_class": successor_class,
            },
        )
    except Exception as e:
        logger.warning(
            f"[R2] declare audit emit failed for {agent_uuid[:8]}...: {e}"
        )
    return "provisional", None


async def _score_lineage_continuity_bg(child_id: str, parent_id: str) -> None:
    """R1 onboard-time lineage scoring with the `marks` policy.

 "Caller policy" and §v3.3-D:
    onboard scores the declared lineage and stamps `provisional_lineage=true`
    on the successor's identity row when the verdict is `inconclusive`.
    `plausible` and `unsupported` are no-ops at this gate — orphan-archival
    is the enforcement path for `unsupported` (see spec §"Caller policy"
    line 256: re-scoring `unsupported` after maturation triggers archival,
    not onboard refusal).

    Fire-and-forget by design: the score does DB work (per-dim trajectory
    reconstruction + audit write), and onboard must not block on it.
    Mirrors `_seed_genesis_from_parent_bg` shape — non-fatal degradation,
    log-only on failure, no exception propagates to the onboard response.

    Almost every fresh successor will land at `inconclusive` until enough
    check-ins accumulate (per `min_observations=5` floor). That's the
    expected shadow-mode signal, not an error.
    """
    try:
        from src.identity.trajectory_continuity import score_trajectory_continuity
        score = await score_trajectory_continuity(parent_id, child_id)
        logger.info(
            f"[R1] Scored {parent_id[:8]}... -> {child_id[:8]}...: "
            f"verdict={score.verdict} plausibility={score.plausibility:.3f} "
            f"n_dims_used={score.n_dims_used} "
            f"calibration_status={score.calibration_status}"
        )
        if score.verdict == "inconclusive":
            backend = get_db()
            marked = await backend.mark_lineage_provisional(child_id, score.score_id)
            if marked:
                logger.info(
                    f"[R1] Marked {child_id[:8]}... provisional "
                    f"(score_id={score.score_id[:8]}..., parent={parent_id[:8]}...)"
                )
            else:
                logger.debug(
                    f"[R1] mark_lineage_provisional matched 0 rows for "
                    f"{child_id[:8]}... — identity row missing?"
                )
    except Exception as e:
        logger.debug(f"score_trajectory_continuity scheduling failed (non-fatal): {e}")
