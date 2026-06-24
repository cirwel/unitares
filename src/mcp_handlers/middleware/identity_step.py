"""Step 1: Resolve Session Identity."""

import asyncio
import time as _time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)

# =============================================================================
# STICKY TRANSPORT BINDING CACHE
# =============================================================================
# Once identity resolves on the first tool call from a transport fingerprint,
# reuse it for all subsequent calls. This prevents identity fragmentation
# caused by derive_session_key() producing different keys for different tools
# when using the IP:UA fingerprint path.

_TRANSPORT_CACHE_TTL = 7200  # 2 hours
_TRANSPORT_CACHE_MAX = 10_000


@dataclass
class TransportBinding:
    """Cached identity binding for a transport fingerprint.

    `source` is load-provenance — where this in-memory binding came from
    (e.g. "redis", "postgres", "agent_uuid_passthrough", "warmup",
    "post_handler:<tool>"). It is NOT the original proof tier.

    `original_session_source` is the proof source at mint time — the value
    that fed `_compute_identity_assurance` when this binding was first
    created. On cache hits, downstream code reads this field via the
    `sticky_cache:<original>` marker so tier-decay (strong→medium,
    medium→weak, weak→weak) can be applied honestly. Default "unknown"
    flows from pre-S3 Redis entries and decays as weak.
    """
    agent_uuid: str
    session_key: str
    bound_at: float  # monotonic timestamp
    source: str
    original_session_source: str = "unknown"


_transport_identity_cache: Dict[str, TransportBinding] = {}

_MIDDLEWARE_IDENTITY_ARG_KEYS = (
    "_middleware_identity_session_key",
    "_middleware_identity_result",
    "_core_agent_row_status",
)

# Identity-lifecycle tools are `pre_onboard` but are NOT pure reads — they
# establish/inspect identity and own their own resolution downstream. The
# pre_onboard read short-circuit (#945 §1) must skip these so onboard/identity
# still receive the dispatch-threaded resolution they rely on.
_IDENTITY_LIFECYCLE_TOOLS = frozenset({"identity", "onboard", "bind_session"})


def _clear_middleware_identity(arguments: Optional[Dict[str, Any]]) -> None:
    """Remove caller-provided internal identity handoff fields."""
    if arguments is None:
        return
    for key in _MIDDLEWARE_IDENTITY_ARG_KEYS:
        arguments.pop(key, None)


def _attach_middleware_identity(
    arguments: Dict[str, Any],
    *,
    session_key: Optional[str],
    identity_result: Optional[dict],
) -> None:
    """Thread dispatch-time identity resolution to handlers/auth."""
    _clear_middleware_identity(arguments)
    if arguments is None or identity_result is None:
        return
    arguments["_middleware_identity_session_key"] = session_key
    arguments["_middleware_identity_result"] = dict(identity_result)


def _transport_cache_key(signals) -> Optional[str]:
    """Compute sticky cache key from transport signals.

    Uses IP:UA fingerprint as the stable anchor, combined with MCP session ID
    when available. This prevents multiple MCP sessions from the same host
    (e.g. parallel Claude Code processes) from collapsing onto one identity.

    Returns None only for explicitly stable headers (x_session_id, x_client_id,
    oauth_client_id) where caching adds no value.
    """
    if not signals:
        return None
    # Truly stable paths — client controls the session ID, no caching needed
    if signals.x_session_id or signals.x_client_id or signals.oauth_client_id:
        return None
    if not signals.ip_ua_fingerprint:
        return None
    # Include mcp_session_id in the key when present so parallel MCP sessions
    # from the same IP:UA (e.g. multiple Claude Code processes on localhost)
    # each get their own cached identity instead of converging to one UUID.
    if signals.mcp_session_id:
        return f"sticky:{signals.ip_ua_fingerprint}:{signals.mcp_session_id}"
    # MCP transport without mcp_session_id is NEVER cached: two MCP processes on
    # the same host share IP:UA, so a fingerprint-only key would cross-bind their
    # identities (e.g. cron-launched Vigil siphoning a Hermes/Claude session into
    # its UUID). Force fresh identity resolution instead — the agent's onboard
    # call will mint or recover correctly.
    if signals.transport == "mcp":
        return None
    # Fingerprint-only for REST/SSE/stdio callers — these transports don't carry
    # an mcp-session-id header and would otherwise lose all caching.
    return f"sticky:{signals.ip_ua_fingerprint}"


def update_transport_binding(
    key: str,
    agent_uuid: str,
    session_key: str,
    source: str,
    *,
    original_session_source: str = "unknown",
) -> None:
    """Set or update a sticky transport binding (in-memory + Redis).

    `original_session_source` should be the proof source at mint time
    (the value the tier mapper would consume — e.g. "x_session_id",
    "agent_uuid_direct"). Callers updating an existing binding without a
    fresh proof should leave it at the default; S3 cache-hit decay treats
    "unknown" as weak.
    """
    _transport_identity_cache[key] = TransportBinding(
        agent_uuid=agent_uuid,
        session_key=session_key,
        bound_at=_time.monotonic(),
        source=source,
        original_session_source=original_session_source,
    )
    _evict_stale_entries()
    # Persist to Redis so bindings survive server restarts
    _persist_binding_to_redis(key, agent_uuid, session_key, source, original_session_source)


def populate_transport_binding_from_recovery(
    key: str,
    agent_uuid: str,
    session_key: str,
    source: str,
    *,
    original_session_source: str = "unknown",
) -> None:
    """Populate the in-memory cache without triggering a Redis write-back.

    Used by startup warmup to hydrate from Redis without creating a loop where
    recovered entries get re-written to Redis on every boot.
    """
    _transport_identity_cache[key] = TransportBinding(
        agent_uuid=agent_uuid,
        session_key=session_key,
        bound_at=_time.monotonic(),
        source=source,
        original_session_source=original_session_source,
    )
    _evict_stale_entries()


def _persist_binding_to_redis(
    key: str,
    agent_uuid: str,
    session_key: str,
    source: str,
    original_session_source: str,
) -> None:
    """Best-effort fire-and-forget write of transport binding to Redis."""
    try:
        asyncio.get_running_loop()  # raises RuntimeError if no loop
        from src.background_tasks import create_tracked_task
        create_tracked_task(
            _persist_binding_to_redis_async(
                key, agent_uuid, session_key, source, original_session_source
            ),
            name="redis_persist_binding",
        )
    except RuntimeError:
        pass  # No event loop — skip Redis persist


async def _persist_binding_to_redis_async(
    key: str,
    agent_uuid: str,
    session_key: str,
    source: str,
    original_session_source: str,
) -> None:
    """Write transport binding to Redis."""
    try:
        from src.cache.redis_client import get_redis
        import json
        redis = await get_redis()
        if not redis:
            return
        redis_key = f"transport_binding:{key}"
        await redis.setex(redis_key, _TRANSPORT_CACHE_TTL, json.dumps({
            "agent_uuid": agent_uuid,
            "session_key": session_key,
            "source": source,
            "original_session_source": original_session_source,
        }))
    except Exception as e:
        logger.debug(f"[STICKY] Redis persist failed: {e}")


# Budget for in-band Redis recovery from the dispatch path. Short by design:
# the anyio-asyncio deadlock (see CLAUDE.md "Known Issue") can stall awaits
# indefinitely, so we degrade to a cold miss rather than hang the pipeline.
_REDIS_RECOVERY_TIMEOUT = 0.5


async def _lookup_core_agent_row_status(agent_uuid: str, source: str) -> Optional[str]:
    """Best-effort bounded lookup of the persisted lifecycle status."""
    try:
        from ..identity.handlers import _get_agent_status
        return await asyncio.wait_for(
            _get_agent_status(agent_uuid),
            timeout=_REDIS_RECOVERY_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[%s] core agent row status lookup timed out after %.3fs for %s...",
            source,
            _REDIS_RECOVERY_TIMEOUT,
            agent_uuid[:8],
        )
    except Exception as e:
        logger.debug(f"[{source}] core agent row status lookup failed: {e}")
    return None


async def _load_binding_from_redis(key: str) -> Optional[TransportBinding]:
    """Try to recover a transport binding from Redis after restart.

    Guarded by asyncio.wait_for: on timeout (typically the anyio-asyncio
    deadlock) we return None so the caller resolves identity fresh instead of
    blocking every subsequent MCP tool call.
    """
    try:
        return await asyncio.wait_for(
            _load_binding_from_redis_inner(key),
            timeout=_REDIS_RECOVERY_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"[STICKY] Redis recovery timed out after {_REDIS_RECOVERY_TIMEOUT}s "
            f"for {key} — falling back to cold path"
        )
        return None
    except Exception as e:
        logger.debug(f"[STICKY] Redis recovery failed: {e}")
        return None


async def _load_binding_from_redis_inner(key: str) -> Optional[TransportBinding]:
    from src.cache.redis_client import get_redis
    import json
    redis = await get_redis()
    if not redis:
        return None
    data = await redis.get(f"transport_binding:{key}")
    if not data:
        return None
    parsed = json.loads(data)
    binding = TransportBinding(
        agent_uuid=parsed["agent_uuid"],
        session_key=parsed["session_key"],
        bound_at=_time.monotonic(),  # Treat as fresh since Redis TTL handles expiry
        source=parsed.get("source", "redis_recovery"),
        # Pre-S3 Redis entries lack this field; default decays as weak.
        original_session_source=parsed.get("original_session_source", "unknown"),
    )
    # Warm the in-memory cache
    _transport_identity_cache[key] = binding
    logger.debug(f"[STICKY] Redis recovery for {key}: agent={binding.agent_uuid[:8]}...")
    return binding


def invalidate_transport_binding(key: str) -> None:
    """Remove a sticky transport binding (e.g. on force_new)."""
    _transport_identity_cache.pop(key, None)
    try:
        asyncio.get_running_loop()
        from src.background_tasks import create_tracked_task
        create_tracked_task(_invalidate_binding_redis_async(key), name="redis_invalidate_binding")
    except RuntimeError:
        pass


async def _invalidate_binding_redis_async(key: str) -> None:
    try:
        from src.cache.redis_client import get_redis
        redis = await get_redis()
        if redis:
            await redis.delete(f"transport_binding:{key}")
    except Exception:
        pass


@dataclass
class StickyConsult:
    """Result of `consult_sticky_binding` — the single-sourced sticky-cache
    consult shared by the MCP dispatch middleware (`resolve_identity`) and
    the REST prebind path (`http_api._resolve_http_bound_agent`).

    `transport_key` is the raw cache key for the signals (None when the
    transport is uncacheable). `cacheable` is True only when the consult
    guard passed — no force_new / client_session_id / continuity_token /
    explicit UUID on the request — i.e. when a caller that resolves
    identity afterwards may write the result back under `transport_key`.
    The REST path writes back only when `cacheable`; the middleware
    deliberately writes back on the raw key even for proof-carrying
    requests (e.g. PATH 0 agent_uuid passthrough).
    """
    transport_key: Optional[str]
    binding: Optional[TransportBinding]
    cacheable: bool


async def consult_sticky_binding(
    signals,
    arguments: Optional[Dict[str, Any]],
    *,
    has_explicit_uuid: bool = False,
    redis_recovery: bool = True,
) -> StickyConsult:
    """Consult the sticky transport-binding cache (MCP middleware + REST).

    Single-sources the consult guard, the TTL check, and (optionally) the
    Redis restart-recovery fallback so the two transports cannot drift —
    the REST copy of this logic had already diverged silently (no Redis
    recovery, no explicit-UUID guard) before consolidation.
    `redis_recovery=False` preserves the REST path's historical behavior:
    it only ever consulted the in-memory cache, never Redis after a
    restart. The divergence is now an explicit parameter instead of a
    drifting copy.
    """
    transport_key = _transport_cache_key(signals)
    args = arguments if isinstance(arguments, dict) else {}
    cacheable = bool(
        transport_key
        and not args.get("force_new")
        and not args.get("client_session_id")
        and not args.get("continuity_token")
        and not has_explicit_uuid
    )
    if not cacheable:
        return StickyConsult(transport_key, None, False)
    cached = _transport_identity_cache.get(transport_key)
    if not cached and redis_recovery:
        cached = await _load_binding_from_redis(transport_key)
    if cached and (_time.monotonic() - cached.bound_at) < _TRANSPORT_CACHE_TTL:
        return StickyConsult(transport_key, cached, True)
    return StickyConsult(transport_key, None, True)


def sticky_resolution_source(binding: TransportBinding) -> str:
    """The S3 cache-hit envelope ``sticky_cache:<original>``, single-sourced.

    Both transports must emit the identical envelope so
    `_compute_identity_assurance` applies the same decay-by-one against the
    original proof tier regardless of which surface served the hit.
    """
    return f"sticky_cache:{binding.original_session_source}"


def _evict_stale_entries() -> None:
    """Lazy TTL eviction + max size enforcement."""
    now = _time.monotonic()
    # TTL eviction
    stale = [k for k, v in _transport_identity_cache.items()
             if (now - v.bound_at) > _TRANSPORT_CACHE_TTL]
    for k in stale:
        del _transport_identity_cache[k]
    # Max size eviction (oldest first)
    if len(_transport_identity_cache) > _TRANSPORT_CACHE_MAX:
        sorted_keys = sorted(_transport_identity_cache, key=lambda k: _transport_identity_cache[k].bound_at)
        for k in sorted_keys[:len(_transport_identity_cache) - _TRANSPORT_CACHE_MAX]:
            del _transport_identity_cache[k]


async def _maybe_recover_via_x_agent_id(
    identity_result: Dict[str, Any],
    x_agent_id_header: Optional[str],
    session_key: str,
) -> Optional[str]:
    """PATH 2.75: rebind a freshly-created session to an existing UUID supplied
    via the ``X-Agent-Id`` header (reconnection when the session key changed,
    e.g. a Pi restart), UNLESS that UUID is substrate-anchored arriving over
    HTTP.

    #802 parity (council 2026-06-24): PATH 2.75 was the one sibling resume path
    missing the ``_substrate_http_reject`` gate that PATH 1 (cache/prefix) and
    PATH 2 (PG session) already enforce. A copyable ``X-Agent-Id`` header bearing
    a substrate resident's UUID must not rebind the session to it over HTTP —
    only a UDS peer (kernel-attested, ``peer_pid`` present) may recover a
    substrate UUID. The gate is self-scoping: non-substrate UUIDs and UDS callers
    pass through untouched, so legitimate Pi/Lumen reconnection over UDS is
    unaffected; only the same-host copyable-header exfiltration path is denied.

    Mutates ``identity_result`` in place on a successful rebind and returns the
    (possibly rebound) bound agent UUID. On refusal or any error it leaves the
    freshly-created identity in place — the safe denial — and never raises.
    """
    bound_agent_id = identity_result.get("agent_uuid")
    if not (identity_result.get("created") and x_agent_id_header):
        return bound_agent_id
    is_uuid = len(x_agent_id_header) == 36 and x_agent_id_header.count("-") == 4
    if not is_uuid:
        return bound_agent_id
    try:
        from ..identity.handlers import _agent_exists_in_postgres, _cache_session
        if not await _agent_exists_in_postgres(x_agent_id_header):
            return bound_agent_id
        # Substrate-over-HTTP gate — same one PATH 1/2 apply.
        from ..identity.resolution import _substrate_http_reject
        if await _substrate_http_reject(
            x_agent_id_header, source="path2_75_x_agent_id_recovery"
        ) is not None:
            logger.warning(
                "[DISPATCH] X-Agent-Id recovery refused for substrate-anchored "
                "UUID %s... over HTTP; keeping freshly-created identity "
                "(resident must connect via UNITARES_UDS_SOCKET).",
                x_agent_id_header[:8],
            )
            return bound_agent_id
        logger.info(
            "[DISPATCH] X-Agent-Id recovery: rebinding session to existing agent "
            "%s... (was about to create %s...)",
            x_agent_id_header[:8], (bound_agent_id or "?")[:8],
        )
        identity_result["agent_uuid"] = x_agent_id_header
        identity_result["created"] = False
        identity_result["persisted"] = True
        identity_result["source"] = "x_agent_id_recovery"
        # Cache this session → UUID binding for future requests
        await _cache_session(session_key, x_agent_id_header)
        return x_agent_id_header
    except Exception as e:
        logger.debug(f"[DISPATCH] X-Agent-Id recovery failed: {e}")
        return bound_agent_id


async def resolve_identity(name: str, arguments: Dict[str, Any], ctx) -> Any:
    """Extract session identity, resolve onboard pin, bind agent."""
    _clear_middleware_identity(arguments)

    # Unified session key derivation via SessionSignals + derive_session_key()
    from ..context import get_session_signals
    from ..identity.handlers import derive_session_key

    signals = get_session_signals()
    client_session_id = arguments.get("client_session_id")
    force_new = arguments.get("force_new", False)
    continuity_token = arguments.get("continuity_token")
    try:
        from ..tool_stability import resolve_tool_alias
        canonical_name, _alias_info = resolve_tool_alias(name)
    except Exception:
        canonical_name = name

    # --- Sticky transport binding: early return if cached ---
    consult = await consult_sticky_binding(
        signals,
        arguments,
        has_explicit_uuid=bool(arguments and arguments.get("agent_uuid")),
    )
    transport_key = consult.transport_key
    ctx._transport_key = transport_key

    if consult.binding is not None:
        cached = consult.binding
        logger.debug(
            f"[STICKY] Cache hit for {transport_key}: agent={cached.agent_uuid[:8]}... "
            f"session_key={cached.session_key[:30]}..."
        )
        core_status = await _lookup_core_agent_row_status(
            cached.agent_uuid,
            "STICKY",
        )
        # Reuse cached binding — set context and return early.
        # S3: mark session_resolution_source with the cache-hit envelope
        # `sticky_cache:<original>` so the tier mapper can apply
        # decay-by-one (strong→medium, medium→weak, weak→weak) against
        # the original proof, instead of treating every cache hit as
        # uniformly weak. See `_compute_identity_assurance`.
        from ..context import (
            set_session_context,
            set_session_resolution_source,
            set_session_proof_origin,
        )
        set_session_resolution_source(sticky_resolution_source(cached))
        # Sticky-cache hit = fingerprint resolution with no per-call proof. Stamp
        # server_inferred so the strict write gate refuses it unless the resolved
        # agent is substrate-earned (this early-return runs before _mark). Mirror
        # of the REST sticky path in http_api._resolve_http_bound_agent.
        set_session_proof_origin("server_inferred")
        client_hint = arguments.get("client_hint") if arguments else None
        identity_result = {
            "agent_uuid": cached.agent_uuid,
            "source": "sticky_cache",
            "original_session_source": cached.original_session_source,
            "core_agent_row_status": core_status,
        }
        context_token = set_session_context(
            session_key=cached.session_key,
            client_session_id=client_session_id,
            agent_id=cached.agent_uuid,
            client_hint=client_hint,
            identity_result=identity_result,
        )
        ctx.session_key = cached.session_key
        ctx.client_session_id = client_session_id
        ctx.bound_agent_id = cached.agent_uuid
        ctx.context_token = context_token
        ctx.client_hint = client_hint
        ctx.identity_result = identity_result
        _attach_middleware_identity(
            arguments,
            session_key=cached.session_key,
            identity_result=identity_result,
        )
        return name, arguments, ctx

    # Invalidate cache on force_new
    if force_new and transport_key:
        invalidate_transport_binding(transport_key)

    session_key = await derive_session_key(signals, arguments)

    logger.debug(
        f"[SESSION] dispatch entry: tool={name} session_key={session_key[:30] if session_key else 'None'}... "
        f"client_session_id={client_session_id!r} signals={signals.transport if signals else 'None'}"
    )

    # Resolve identity (Redis → PostgreSQL → Token Rebind → Create).
    # Name-claim lookup removed 2026-04-17 — middleware no longer passes
    # agent_name into resolution. Label is set by onboard/identity handlers.
    from ..identity.handlers import resolve_session_identity

    # Extract X-Agent-Id from SessionSignals (set at transport layer) or fallback to request headers
    x_agent_id_header = signals.x_agent_id if signals else None
    if not x_agent_id_header:
        try:
            from ..context import get_session_context
            ctx_data = get_session_context()
            req = ctx_data.get('request')
            if req and hasattr(req, 'headers'):
                x_agent_id_header = req.headers.get("x-agent-id") or req.headers.get("X-Agent-Id")
        except Exception:
            pass

    # X-Agent-Name auto-resume REMOVED (identity honesty refactor):
    # Silent name claims from transport headers bypass consent. All
    # name-claim resolution paths were removed 2026-04-17 — a non-UUID
    # X-Agent-Id header is ignored (only UUID values are used for PATH
    # 2.75 UUID recovery below).
    trajectory_sig = arguments.get("trajectory_signature") if arguments else None

    # PATH 0 passthrough: when caller supplies agent_uuid, skip session
    # resolution entirely. The identity/onboard handler will verify the UUID
    # exists; the middleware just needs to bind the session to it so context
    # is set correctly. This prevents ghost creation for resident agents.
    _direct_uuid = arguments.get("agent_uuid") if arguments else None
    if _direct_uuid and name in ("identity", "onboard"):
        # Identity Honesty Part C: require matching continuity_token.
        # Matches the handler-layer gate in identity/handlers.py PATH 0.
        from ..identity.session import extract_token_agent_uuid_safe
        _partc_token_aid = extract_token_agent_uuid_safe(
            arguments.get("continuity_token") if arguments else None
        )
        _partc_owned = _partc_token_aid == _direct_uuid

        if not _partc_owned:
            _peer_pid = getattr(signals, "peer_pid", None) if signals else None
            _transport = getattr(signals, "transport", None) if signals else None
            _defer_to_substrate_handler = (
                _transport == "uds" and isinstance(_peer_pid, int)
            )
            _partc_mode = None

            async def _emit_middleware_hijack(mode: str) -> None:
                """Mirror the handlers.py event emission so middleware-path
                hijack attempts surface on the same broadcast channel as
                handler-path attempts. Source tag distinguishes them."""
                try:
                    from ..identity.handlers import _broadcaster
                except Exception:
                    return
                b = _broadcaster()
                if b is None:
                    return
                try:
                    await b.broadcast_event(
                        event_type="identity_hijack_suspected",
                        agent_id=_direct_uuid,
                        payload={
                            "mode": mode,
                            "source": "middleware",
                            "proof": "matching_token" if _partc_token_aid == _direct_uuid else "none",
                            "token_aid_mismatch": (
                                _partc_token_aid
                                if (_partc_token_aid and _partc_token_aid != _direct_uuid)
                                else None
                            ),
                        },
                    )
                except Exception as e:
                    logger.warning(f"[IDENTITY_HIJACK] middleware broadcast_event failed: {e}")

            if _defer_to_substrate_handler:
                logger.debug(
                    "[SUBSTRATE_GATE] Middleware PATH 0 saw UDS peer_pid=%d "
                    "for %s...; deferring ownership decision to identity handler",
                    _peer_pid,
                    _direct_uuid[:8],
                )
            else:
                from config.governance_config import identity_strict_mode
                _partc_mode = identity_strict_mode()

            if not _defer_to_substrate_handler and _partc_mode == "strict":
                ctx.strict_reject = True
                ctx.identity_result = {
                    "error": (
                        "Bare agent_uuid passthrough denied. Include "
                        "continuity_token or use force_new=true."
                    ),
                    "reason": "bare_uuid_resume_denied",
                    "agent_uuid": _direct_uuid,
                }
                logger.warning(
                    "[IDENTITY_STRICT] Middleware rejected PATH 0 passthrough: "
                    "agent_uuid=%s... without matching token",
                    _direct_uuid[:8],
                )
                await _emit_middleware_hijack("strict")
                return name, arguments, ctx
            elif not _defer_to_substrate_handler and _partc_mode == "log":
                logger.warning(
                    "[IDENTITY_STRICT] Would reject middleware PATH 0 passthrough: "
                    "agent_uuid=%s... token_aid=%s",
                    _direct_uuid[:8],
                    (_partc_token_aid[:8] + "...") if _partc_token_aid else "none",
                )
                await _emit_middleware_hijack("log")
            # mode == "off": unchanged behavior, no log, no broadcast

        from ..context import set_session_context
        client_hint = arguments.get("client_hint") if arguments else None
        core_status = await _lookup_core_agent_row_status(_direct_uuid, "PATH0")
        identity_result = {
            "agent_uuid": _direct_uuid,
            "source": "agent_uuid_passthrough",
            "core_agent_row_status": core_status,
        }
        context_token = set_session_context(
            session_key=session_key,
            client_session_id=client_session_id,
            agent_id=_direct_uuid,
            client_hint=client_hint,
            identity_result=identity_result,
        )
        ctx.session_key = session_key
        ctx.client_session_id = client_session_id
        ctx.bound_agent_id = _direct_uuid
        ctx.context_token = context_token
        ctx.client_hint = client_hint
        ctx.identity_result = identity_result
        ctx._transport_key = transport_key
        _attach_middleware_identity(
            arguments,
            session_key=session_key,
            identity_result=identity_result,
        )
        # Populate sticky cache so subsequent tool calls reuse this UUID
        if transport_key:
            update_transport_binding(
                transport_key,
                _direct_uuid,
                session_key,
                "agent_uuid_passthrough",
                original_session_source="agent_uuid_passthrough",
            )
        logger.info(f"[DISPATCH] PATH 0 passthrough: agent_uuid={_direct_uuid[:8]}... (skipped resolution)")
        return name, arguments, ctx

    # Extract agent UUID from continuity token for PATH 2.8 direct lookup
    from ..identity.session import extract_token_agent_uuid_safe
    _token_agent_uuid = extract_token_agent_uuid_safe(
        arguments.get("continuity_token") if arguments else None
    )

    # #945 §1: short-circuit identity resolution for pre_onboard READ calls
    # that present no proof. The old shape resolved-then-guarded — EVERY tool
    # ran resolve_session_identity(resume=True) and pre_onboard tools were only
    # spared the force_new auto-mint *retry* below. But the resume itself can
    # fingerprint-bind to an existing identity, which then gets written into the
    # sticky transport cache at the tail of this function, so a pure read by an
    # unbound caller still PRODUCED a cacheable identity as a side effect.
    # Guarding-first instead: when the call resolves to pre_onboard, is not an
    # identity-lifecycle tool, and the caller supplied no proof (continuity
    # token / client_session_id / agent_uuid / UUID X-Agent-Id), skip resolution
    # entirely and leave the request unbound. Reads that DO carry proof still
    # resume-resolve — reading an existing identity is legitimate, not
    # "producing" one — preserving the test_read_only_*_session_miss contract.
    _has_identity_proof = bool(
        _token_agent_uuid
        or (arguments and arguments.get("client_session_id"))
        or (arguments and arguments.get("agent_uuid"))
        or (
            x_agent_id_header
            and len(x_agent_id_header) == 36
            and x_agent_id_header.count("-") == 4
        )
    )
    if not _has_identity_proof and canonical_name not in _IDENTITY_LIFECYCLE_TOOLS:
        from src.mcp_handlers.decorators import get_call_identity_requirement
        if get_call_identity_requirement(canonical_name, arguments) == "pre_onboard":
            logger.info(
                "[DISPATCH] pre_onboard read %s with no proof — short-circuiting "
                "identity resolution (no resolve, no mint, no sticky cache)",
                name,
            )
            from ..context import set_session_context
            client_hint = arguments.get("client_hint") if arguments else None
            context_token = set_session_context(
                session_key=session_key,
                client_session_id=client_session_id,
                agent_id=None,
                client_hint=client_hint,
                identity_result=None,
            )
            ctx.session_key = session_key
            ctx.client_session_id = client_session_id
            ctx.bound_agent_id = None
            ctx.context_token = context_token
            ctx.client_hint = client_hint
            ctx.identity_result = None
            _attach_middleware_identity(
                arguments,
                session_key=session_key,
                identity_result=None,
            )
            return name, arguments, ctx

    bound_agent_id = None
    identity_result = None
    try:
        # Middleware resolves the CURRENT session's binding (established by
        # onboard/identity earlier). resume=True is correct here — we are NOT
        # creating new identities, we are looking up the existing one.
        identity_result = await resolve_session_identity(
            session_key,
            trajectory_signature=trajectory_sig,
            resume=True,
            token_agent_uuid=_token_agent_uuid,
        )
        # S21-a: PATH 2 now fail-closes when there is no PG session row, so
        # tools called without a prior onboard arrive here with a
        # session_resolve_miss instead of a silent PATH-3 ghost. Retry with
        # force_new=True so the handler chain still has an identity to work
        # with; the mint_guard in _cache_session ensures the fresh UUID
        # cannot overwrite a concurrently-bound legitimate session.
        # The retry declares spawn_reason="dispatch_auto_mint" so these
        # mints carry lineage instead of contributing to the no-lineage
        # ghost rate (the underlying motivation for the S21 incident).
        # Without this, S21-a would stop the Redis-overwrite bleed but
        # leave the lineage-declaration bleed open.
        if (identity_result.get("resume_failed")
                and identity_result.get("error") == "session_resolve_miss"):
            # #425: identity-requirement is now a per-tool attribute on the
            # tool registry (`requires_identity` on the @mcp_tool decorator).
            # pre_onboard tools are exempt from auto-mint at this layer; the
            # request proceeds unbound. Behavior matches the prior hardcoded
            # allowlist exactly — switching to attribute lookup so adding a
            # new pre_onboard tool no longer requires editing this file.
            # The previous allowlist {health_check, get_server_info,
            # list_tools, describe_tool, get_governance_metrics, skills,
            # identity, onboard, bind_session} now lives as decorator
            # arguments on those handlers.
            # Call-level resolution (#425 action-level fold): mixed
            # read-write tools (knowledge/dialectic/agent/...) declare
            # pre_onboard_actions so their browsable READ actions serve
            # unbound while writes stay identity-gated — tool-level
            # classification can't express that split.
            from src.mcp_handlers.decorators import get_call_identity_requirement
            if get_call_identity_requirement(canonical_name, arguments) == "pre_onboard":
                logger.info(
                    "[DISPATCH] session_resolve_miss for %s under %s... "
                    "— leaving request unbound (no middleware auto-mint, "
                    "call resolved to pre_onboard)",
                    name, session_key[:20],
                )
            else:
                # #425 contract: when STRICT_IDENTITY_REQUIRED is on, return
                # a typed-refusal response instead of auto-minting an
                # ephemeral identity. Default off for staged rollout —
                # local → Lumen → dispatch → flip default. The refusal is
                # a structured success-shape, not an MCP error: error
                # responses invite retry-with-mint catch paths and would
                # reintroduce the leak.
                from src.mcp_handlers.identity_bootstrap import (
                    is_strict_identity_required,
                    strict_identity_refusal_payload,
                )
                if is_strict_identity_required():
                    logger.info(
                        "[DISPATCH] session_resolve_miss for %s... "
                        "— STRICT_IDENTITY_REQUIRED=true; returning typed "
                        "refusal (no auto-mint).",
                        session_key[:20],
                    )
                    from src.mcp_handlers.response_base import success_response
                    # Single-sourced payload — the REST gate returns the
                    # same dict, so the two transports cannot drift
                    # (stage-1 burn-in fold, 2026-06-11).
                    return success_response(strict_identity_refusal_payload(name))
                logger.info(
                    "[DISPATCH] session_resolve_miss for %s... — minting "
                    "ephemeral dispatch identity (S21-a, spawn_reason="
                    "dispatch_auto_mint)",
                    session_key[:20],
                )
                identity_result = await resolve_session_identity(
                    session_key,
                    trajectory_signature=trajectory_sig,
                    force_new=True,
                    token_agent_uuid=_token_agent_uuid,
                    spawn_reason="dispatch_auto_mint",
                )
        # PATH 2.75: X-Agent-Id UUID recovery (substrate-over-HTTP gated, #802).
        bound_agent_id = await _maybe_recover_via_x_agent_id(
            identity_result, x_agent_id_header, session_key
        )

        # Mark dispatch-created identities as ephemeral
        if identity_result.get("created") and not identity_result.get("persisted"):
            identity_result["ephemeral"] = True
            identity_result["created_via"] = "dispatch"
            logger.info(f"[DISPATCH] Ephemeral identity created (not persisted): {bound_agent_id[:8]}...")

        # Update session TTL for persisted identities
        if identity_result.get("persisted"):
            for attempt in range(2):
                try:
                    from src.db import get_db
                    result = await get_db().update_session_activity(session_key)
                    break
                except Exception as e:
                    if attempt == 0:
                        await asyncio.sleep(0.05)
                    else:
                        logger.warning(f"[DISPATCH] Session TTL update failed for {session_key[:20]}...: {e}")
    except Exception as e:
        logger.debug(f"Could not resolve session identity: {e}")

    # Set context for this request
    from ..context import set_session_context
    client_hint = arguments.get("client_hint") if arguments else None
    context_token = set_session_context(
        session_key=session_key,
        client_session_id=client_session_id,
        agent_id=bound_agent_id,
        client_hint=client_hint,
        spawn_reason=identity_result.get("spawn_reason") if identity_result else None,
        identity_result=identity_result,
    )

    logger.info(
        f"[DISPATCH_ENTRY] tool={name}, has_kwargs={'kwargs' in arguments}, "
        f"arg_keys={list(arguments.keys())[:5]}, "
        f"bound_agent_id={bound_agent_id[:8] + '...' if bound_agent_id else 'None'}"
    )

    ctx.session_key = session_key
    ctx.client_session_id = client_session_id
    ctx.bound_agent_id = bound_agent_id
    ctx.context_token = context_token
    ctx.client_hint = client_hint
    ctx.identity_result = identity_result
    _attach_middleware_identity(
        arguments,
        session_key=session_key,
        identity_result=identity_result,
    )

    # --- Populate sticky cache after successful resolution ---
    if transport_key and bound_agent_id:
        source = identity_result.get("source", "unknown") if identity_result else "unknown"
        # identity_result.source IS the proof source the tier mapper consumes,
        # so original_session_source mirrors it at mint time. On future cache
        # hits the decay function reads this value and applies tier-decay.
        update_transport_binding(
            transport_key,
            bound_agent_id,
            session_key,
            source,
            original_session_source=source,
        )

    return name, arguments, ctx
