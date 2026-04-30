"""Step 1: Resolve Session Identity."""

import asyncio
import time as _time
from dataclasses import dataclass, field
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
    """Cached identity binding for a transport fingerprint."""
    agent_uuid: str
    session_key: str
    bound_at: float  # monotonic timestamp
    source: str  # e.g. "redis", "postgres", "created"


_transport_identity_cache: Dict[str, TransportBinding] = {}

_MIDDLEWARE_IDENTITY_ARG_KEYS = (
    "_middleware_identity_session_key",
    "_middleware_identity_result",
    "_core_agent_row_status",
)


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


def update_transport_binding(key: str, agent_uuid: str, session_key: str, source: str) -> None:
    """Set or update a sticky transport binding (in-memory + Redis)."""
    _transport_identity_cache[key] = TransportBinding(
        agent_uuid=agent_uuid,
        session_key=session_key,
        bound_at=_time.monotonic(),
        source=source,
    )
    _evict_stale_entries()
    # Persist to Redis so bindings survive server restarts
    _persist_binding_to_redis(key, agent_uuid, session_key, source)


def populate_transport_binding_from_recovery(
    key: str, agent_uuid: str, session_key: str, source: str
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
    )
    _evict_stale_entries()


def _persist_binding_to_redis(key: str, agent_uuid: str, session_key: str, source: str) -> None:
    """Best-effort fire-and-forget write of transport binding to Redis."""
    try:
        asyncio.get_running_loop()  # raises RuntimeError if no loop
        from src.background_tasks import create_tracked_task
        create_tracked_task(
            _persist_binding_to_redis_async(key, agent_uuid, session_key, source),
            name="redis_persist_binding",
        )
    except RuntimeError:
        pass  # No event loop — skip Redis persist


async def _persist_binding_to_redis_async(key: str, agent_uuid: str, session_key: str, source: str) -> None:
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

    # --- Sticky transport binding: early return if cached ---
    transport_key = _transport_cache_key(signals)
    ctx._transport_key = transport_key

    _has_agent_uuid = bool(arguments and arguments.get("agent_uuid"))
    if (transport_key
        and not force_new
        and not client_session_id
        and not continuity_token
        and not _has_agent_uuid):
        cached = _transport_identity_cache.get(transport_key)
        # On miss, try Redis (survives restarts)
        if not cached:
            cached = await _load_binding_from_redis(transport_key)
        if cached and (_time.monotonic() - cached.bound_at) < _TRANSPORT_CACHE_TTL:
            logger.debug(
                f"[STICKY] Cache hit for {transport_key}: agent={cached.agent_uuid[:8]}... "
                f"session_key={cached.session_key[:30]}..."
            )
            core_status = await _lookup_core_agent_row_status(
                cached.agent_uuid,
                "STICKY",
            )
            # Reuse cached binding — set context and return early
            from ..context import set_session_context
            client_hint = arguments.get("client_hint") if arguments else None
            identity_result = {
                "agent_uuid": cached.agent_uuid,
                "source": "sticky_cache",
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
        _partc_token_aid = None
        _partc_token = arguments.get("continuity_token") if arguments else None
        if _partc_token:
            try:
                from ..identity.session import extract_token_agent_uuid
                _partc_token_aid = extract_token_agent_uuid(str(_partc_token))
            except Exception:
                _partc_token_aid = None
        _partc_owned = _partc_token_aid == _direct_uuid

        if not _partc_owned:
            from config.governance_config import identity_strict_mode
            _partc_mode = identity_strict_mode()

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

            if _partc_mode == "strict":
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
            elif _partc_mode == "log":
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
            update_transport_binding(transport_key, _direct_uuid, session_key, "agent_uuid_passthrough")
        logger.info(f"[DISPATCH] PATH 0 passthrough: agent_uuid={_direct_uuid[:8]}... (skipped resolution)")
        return name, arguments, ctx

    # Extract agent UUID from continuity token for PATH 2.8 direct lookup
    _token_agent_uuid = None
    if arguments and arguments.get("continuity_token"):
        try:
            from ..identity.session import extract_token_agent_uuid
            _token_agent_uuid = extract_token_agent_uuid(str(arguments["continuity_token"]))
        except Exception:
            pass

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
            read_only_diagnostic_tools = {
                "health_check",
                "get_server_info",
                "list_tools",
                "describe_tool",
                "get_governance_metrics",
                "skills",
            }
            identity_lifecycle_tools = {"identity", "onboard"}
            if name in read_only_diagnostic_tools or name in identity_lifecycle_tools:
                logger.info(
                    "[DISPATCH] session_resolve_miss for %s under %s... "
                    "— leaving request unbound (no middleware auto-mint)",
                    name, session_key[:20],
                )
            else:
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
        bound_agent_id = identity_result.get("agent_uuid")

        # PATH 2.75: X-Agent-Id UUID recovery
        # If session resolution created a NEW identity but X-Agent-Id header contains
        # a known UUID, rebind to the existing agent instead of creating a duplicate.
        # This handles reconnection when session key changes (e.g., Pi restart).
        if identity_result.get("created") and x_agent_id_header:
            is_uuid = len(x_agent_id_header) == 36 and x_agent_id_header.count("-") == 4
            if is_uuid:
                try:
                    from ..identity.handlers import _agent_exists_in_postgres, _cache_session
                    if await _agent_exists_in_postgres(x_agent_id_header):
                        logger.info(
                            f"[DISPATCH] X-Agent-Id recovery: rebinding session to existing "
                            f"agent {x_agent_id_header[:8]}... (was about to create {bound_agent_id[:8]}...)"
                        )
                        bound_agent_id = x_agent_id_header
                        identity_result["agent_uuid"] = x_agent_id_header
                        identity_result["created"] = False
                        identity_result["persisted"] = True
                        identity_result["source"] = "x_agent_id_recovery"
                        # Cache this session → UUID binding for future requests
                        await _cache_session(session_key, x_agent_id_header)
                except Exception as e:
                    logger.debug(f"[DISPATCH] X-Agent-Id recovery failed: {e}")

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
        update_transport_binding(transport_key, bound_agent_id, session_key, source)

    return name, arguments, ctx
