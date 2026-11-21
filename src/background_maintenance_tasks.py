"""
Periodic maintenance, sweeper, persistence, and cache-warmup background tasks
for the governance MCP server.

Extracted from src/background_tasks.py. Public and internal entrypoints are
re-exported on src/background_tasks.py for backwards compatibility.
"""

import asyncio
from typing import Optional

from src.logging_utils import get_logger

logger = get_logger("src.background_tasks")


# ---------------------------------------------------------------------------
# S8a Phase-2 class promotion sweep
# ---------------------------------------------------------------------------


async def class_promotion_sweeper_task(interval_minutes: float = 30.0):
    """Promote ephemeral identities with ``total_updates >= 3`` to ``engaged_ephemeral``.

    See ``src/grounding/class_promotion.py`` for the rule and
 ```` for the audit that produced the
    threshold. Cadence is 30min — same as Vigil's launchd cycle. The sweep
    is monotone, idempotent, and concurrency-safe (CTE with FOR UPDATE
    SKIP LOCKED + re-check on update), so cadence affects only freshness
    of the class-conditional partition, not correctness.

    Startup delay 60s to let metadata cache warm. Errors are logged at
    WARNING (not debug) so a 30-min silent gap is visible to operators.

    S10.2: at the tail of each cycle, also call
    ``SequentialCalibrationTracker.rebucket_from_agent_states`` so the
    by-class calibration rollup tracks any promotions just executed.
    Rebucket is decoupled from promotion success — even cycles with zero
    promotions still rebucket so that out-of-band tag edits (manual
    re-tagging, label changes) eventually converge in the by-class view.
    Errors in the rebucket pass are isolated from the promotion pass so a
    rebucket failure cannot starve future promotions.
    """
    await asyncio.sleep(60.0)
    while True:
        try:
            from src.grounding.class_promotion import promote_engaged_ephemeral

            result = await promote_engaged_ephemeral()
            if result.get("promoted", 0) > 0:
                logger.info(
                    f"[CLASS_PROMOTION] {result['promoted']} ephemeral → engaged_ephemeral "
                    f"(threshold={result['threshold']})"
                )
            else:
                logger.debug(
                    f"[CLASS_PROMOTION] no promotions (threshold={result['threshold']})"
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"[CLASS_PROMOTION] Sweep failed (will retry): {e}")

        # S10.2: rebucket calibration class_states from current metadata.
        try:
            from src.agent_metadata_model import agent_metadata
            from src.grounding.class_indicator import classify_agent
            from src.sequential_calibration import get_sequential_calibration_tracker

            def _s10_classifier(aid: str) -> Optional[str]:
                meta = agent_metadata.get(aid)
                return classify_agent(meta) if meta is not None else None

            rebucket = get_sequential_calibration_tracker().rebucket_from_agent_states(
                classifier=_s10_classifier,
            )
            if rebucket["tracked_agents"] > 0 or rebucket["classifier_errors"] > 0:
                logger.info(
                    f"[S10_REBUCKET] tracked={rebucket['tracked_agents']} "
                    f"unresolved={rebucket['unresolved_agents']} "
                    f"errors={rebucket['classifier_errors']} "
                    f"buckets={rebucket['buckets']}"
                )
            else:
                logger.debug("[S10_REBUCKET] no tracked agents this cycle")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"[S10_REBUCKET] Sweep failed (will retry): {e}")

        try:
            await asyncio.sleep(interval_minutes * 60)
        except asyncio.CancelledError:
            break


# ---------------------------------------------------------------------------
# R2 PR 4 — lineage-eval sweeper
# ---------------------------------------------------------------------------


async def _lineage_eval_sweep_once() -> dict[str, int]:
    """Run a single lineage-eval sweep cycle.

    Pulled out of ``lineage_eval_sweeper_task`` so the inner cycle can
    be unit-tested without spinning the infinite loop. Returns a dict
    with ``candidates`` (count selected) and ``transitions`` (count of
    FSM evals that produced a state change), used by the caller for
    log-level decision-making.

    Per-eval exceptions are caught and logged at WARNING but do not
    abort the cycle — one badly-shaped row should not starve the rest.
    """
    from src.db import get_db
    from src.identity.lineage_lifecycle import evaluate_lineage_for

    backend = get_db()
    candidates = await backend.select_lineage_eval_candidates()
    transitions = 0
    for successor_id in candidates:
        try:
            outcome = await evaluate_lineage_for(successor_id)
            if outcome.transition is not None:
                transitions += 1
        except Exception as exc:
            logger.warning(
                "[R2_SWEEPER] eval failed for %s: %s",
                successor_id[:8], exc,
            )
    return {"candidates": len(candidates), "transitions": transitions}


async def lineage_eval_sweeper_task(interval_minutes: float = 30.0):
    """R2: re-evaluate provisional and confirmed lineage edges.

    Mirrors ``class_promotion_sweeper_task``: runs outside the anyio
    context (asyncio task, no anyio handler context), so direct
    ``await`` on asyncpg is safe. Per the design doc §Observability,
    the sweeper itself emits no audit events on cycles with zero
    transitions — only state transitions emit, and those are handled
    inside ``evaluate_lineage_for``.

    Startup delay 60s to let metadata cache warm. Errors at the cycle
    boundary are logged at WARNING (not debug) so a 30-minute silent
    gap is visible to operators.

 See: §"Evaluation
    triggers" and §"Observability"
    """
    await asyncio.sleep(60.0)
    while True:
        try:
            result = await _lineage_eval_sweep_once()
            if result["transitions"] > 0:
                logger.info(
                    f"[R2_SWEEPER] cycle complete: {result['candidates']} candidates, "
                    f"{result['transitions']} transition(s)"
                )
            else:
                logger.debug(
                    f"[R2_SWEEPER] cycle complete: {result['candidates']} candidates, "
                    f"no transitions"
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"[R2_SWEEPER] Sweep failed (will retry): {e}")
        try:
            await asyncio.sleep(interval_minutes * 60)
        except asyncio.CancelledError:
            break


# ---------------------------------------------------------------------------
# Principal (octopus) rollup — derived, off the hot path
# ---------------------------------------------------------------------------


async def principal_rollup_sweeper_task(interval_seconds: float = 60.0):
    """Recompute the DERIVED principal (octopus) rollup from the in-memory
    agent-metadata cache, so identity/onboard responses can surface "you are
    instance K of principal P" (see ``src/services/principal_rollup.py`` and
    docs/proposals/active/principal-rollup-v0.md).

    Council 2026-06-18 (unanimous): the principal is NEVER resolved at mint — it
    is recomputed here from the accumulated declared edges. Pure CPU over the
    in-memory cache: no DB/Redis await, so it cannot hit the anyio-asyncio
    coupling class. Startup delay 60s to let the cache warm. A recompute failure
    leaves the prior map intact (fail-stale, never fail-error).
    """
    await asyncio.sleep(60.0)
    while True:
        try:
            from src.agent_metadata_model import agent_metadata
            from src.services import principal_rollup

            mapped = principal_rollup.recompute(agent_metadata)
            logger.debug(f"[PRINCIPAL_ROLLUP] recompute complete: {mapped} principal(s)")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"[PRINCIPAL_ROLLUP] recompute failed (keeping prior map): {e}")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break


# ---------------------------------------------------------------------------
# Session cleanup
# ---------------------------------------------------------------------------

_SESSION_CLEANUP_REDIS_TIMEOUT_S = 2.0


async def _session_cleanup_pass() -> tuple[int, int, int]:
    """Run one cleanup pass. Returns ``(pg_deleted, redis_deleted, redis_live_kept)``.

    A ``core.sessions`` row gets ``expires_at`` = bind time + SESSION_TTL_HOURS
    and is extended only when a lookup reaches PostgreSQL (PATH 2). A lookup
    served from Redis (PATH 1) slides the Redis key's TTL instead
    (``_refresh_session_ttl``), so a client that stays active keeps a live Redis
    binding while its PG row runs out. Such a key is not an orphan: deleting it
    with the expired row strands the client, whose next call misses both stores
    and is refused as ``pg_session_missing`` until it re-onboards.

    So the Redis side deletes only a key with no expiry (TTL -1), which Redis
    would otherwise keep forever. A key with a positive TTL is left for Redis to
    expire once its client goes idle: the row and the key get the same TTL at
    bind time, so a key nobody refreshed expires within moments of its row. A
    key whose TTL cannot be read is left alone. The PG rows are deleted exactly
    as before.

    Live 2026-09-01 to 09-07: three discord-bridge session keys were stranded
    this way. Each flood of misses began right after a cleanup pass that
    deleted one Redis key, and lasted until the bridge restarted.
    """
    pg_deleted = 0
    redis_deleted = 0
    redis_live_kept = 0

    expired_session_keys = []
    try:
        from src.db import get_db

        db = get_db()
        async with db.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    "SELECT session_id FROM core.sessions WHERE expires_at <= now()"
                )
                expired_session_keys = [r["session_id"] for r in rows]
                result = await conn.execute("DELETE FROM core.sessions WHERE expires_at <= now()")
                pg_deleted = int(result.split()[-1]) if result else 0
    except Exception as e:
        logger.warning(f"[SESSION_CLEANUP] PG cleanup failed: {e}")

    if expired_session_keys:
        try:
            from src.cache.redis_client import get_redis

            redis = await get_redis()
            if redis is not None:
                for sk in expired_session_keys:
                    key = f"session:{sk}"
                    try:
                        ttl = await asyncio.wait_for(
                            redis.ttl(key), timeout=_SESSION_CLEANUP_REDIS_TIMEOUT_S,
                        )
                        if isinstance(ttl, int) and ttl > 0:
                            redis_live_kept += 1
                            continue
                        if ttl != -1:
                            continue  # -2: already gone; anything else: unknown
                        removed = await asyncio.wait_for(
                            redis.delete(key), timeout=_SESSION_CLEANUP_REDIS_TIMEOUT_S,
                        )
                        if removed:
                            redis_deleted += 1
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"[SESSION_CLEANUP] Redis cleanup failed: {e}")

    return pg_deleted, redis_deleted, redis_live_kept


async def session_cleanup_task(interval_hours: float = 6.0):
    """Delete expired sessions from PG and orphaned Redis session cache keys.

    Live Redis bindings are kept; see ``_session_cleanup_pass``.
    """
    while True:
        await asyncio.sleep(interval_hours * 3600)
        pg_deleted, redis_deleted, redis_live_kept = await _session_cleanup_pass()
        if pg_deleted or redis_deleted or redis_live_kept:
            logger.info(
                f"[SESSION_CLEANUP] Deleted {pg_deleted} expired PG sessions, "
                f"{redis_deleted} Redis cache keys; kept {redis_live_kept} "
                f"live Redis bindings"
            )


# ---------------------------------------------------------------------------
# perf_monitor snapshot persistence
# ---------------------------------------------------------------------------

_PERF_PERSIST_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("ode.numpy_step_ms",                           "p50_ms", "ode.numpy_step_ms.p50"),
    ("ode.numpy_step_ms",                           "p99_ms", "ode.numpy_step_ms.p99"),
    ("lease_plane.client.v1.lease.acquire",         "p50_ms", "lease_plane.client.v1.lease.acquire.p50"),
    ("lease_plane.client.v1.lease.acquire",         "p99_ms", "lease_plane.client.v1.lease.acquire.p99"),
)


async def perf_monitor_persist_task(interval_minutes: float = 5.0):
    """Snapshot perf_monitor every N minutes; persist load-bearing percentiles
    to ``metrics.series``.

    The in-process perf_monitor singleton holds a 1000-sample ring per op key
    (``src/perf_monitor.py``). That's volatile — process restart wipes it,
    and snapshots are only visible via the snapshot HTTP endpoint. This task
    samples the snapshot every 5min and writes the catalog-gated subset to
    ``metrics.series`` so the substrate-question measurement gates
    (``beam-footprint-roadmap-v0.md`` v0.3 RESOLUTION + v0.3.2 amendment) can
    accumulate longitudinal data without each restart resetting the window.

    Startup delay 90s to let monitor traffic accumulate at least one sample.
    Errors are logged at WARNING so a silent gap is visible.
    """
    await asyncio.sleep(90.0)
    logger.info(
        f"[PERF_PERSIST] Started perf_monitor → metrics.series persistence "
        f"(every {interval_minutes}m, {len(_PERF_PERSIST_TARGETS)} series)"
    )
    from src.fleet_metrics.storage import record as _record_metric
    from src.perf_monitor import snapshot as _perf_snapshot

    while True:
        try:
            await asyncio.sleep(interval_minutes * 60.0)
            snap = _perf_snapshot()
            written = 0
            for op_key, pct_field, metric_name in _PERF_PERSIST_TARGETS:
                op_stats = snap.get(op_key)
                if not op_stats:
                    continue
                value = op_stats.get(pct_field)
                if value is None:
                    continue
                try:
                    await _record_metric(metric_name, float(value))
                    written += 1
                except Exception as e:
                    logger.warning(
                        f"[PERF_PERSIST] record {metric_name} failed: {e}"
                    )
            if written:
                logger.debug(f"[PERF_PERSIST] wrote {written} series points")

            try:
                import json as _json

                from governance_core.coordination_events_helpers import (
                    make_measurement_payload,
                )
                from src.coordination_events import MEASUREMENT_LEASE_PLANE_REQUEST
                from src.db import get_db
                from src.lease_plane.client import (
                    drain_measurement_samples,
                    measurement_samples_dropped,
                )

                samples = drain_measurement_samples()
                if samples:
                    rows = [
                        (
                            ts,
                            MEASUREMENT_LEASE_PLANE_REQUEST,
                            endpoint,
                            elapsed_ms,
                            outcome,
                            payload_bytes,
                            _json.dumps(
                                make_measurement_payload(
                                    endpoint=endpoint,
                                    method=method,
                                    status_code=None,
                                    elapsed_ms=elapsed_ms,
                                    payload_bytes=payload_bytes,
                                )
                                | {"samples_dropped_total": measurement_samples_dropped()}
                            ),
                        )
                        for (ts, endpoint, method, outcome, elapsed_ms, payload_bytes) in samples
                    ]
                    db = get_db()
                    async with db.acquire() as conn:
                        await conn.executemany(
                            """
                            INSERT INTO audit.coordination_measurements
                                (recorded_at, measurement_type, endpoint,
                                 elapsed_ms, status, payload_bytes, meta)
                            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                            """,
                            rows,
                        )
                    logger.debug(
                        f"[PERF_PERSIST] wrote {len(rows)} lease measurement rows"
                    )
            except Exception as e:
                logger.warning(f"[PERF_PERSIST] lease measurement drain failed: {e}")
        except asyncio.CancelledError:
            logger.info("[PERF_PERSIST] Task cancelled")
            break
        except Exception as e:
            logger.warning(f"[PERF_PERSIST] Error: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Cache warmup & substrate refresh tasks
# ---------------------------------------------------------------------------


async def transport_binding_cache_warmup():
    """Pre-populate the sticky transport-binding cache from Redis at startup.

    Reads the transport_binding:* keys that identity_step.py writes. Running
    this at boot means the dispatch-path Redis read (guarded by wait_for)
    rarely has to fire, which keeps the anyio-asyncio deadlock off the
    critical path.
    """
    await asyncio.sleep(2)  # Wait for DB and Redis to be ready
    try:
        from src.cache import is_redis_available

        if not is_redis_available():
            logger.info("[WARMUP] Redis unavailable, skipping transport binding warmup")
            return

        from src.cache.redis_client import get_redis

        redis = await get_redis()
        if not redis:
            logger.info("[WARMUP] Redis client not ready, skipping transport binding warmup")
            return

        import json
        from src.mcp_handlers.middleware.identity_step import (
            populate_transport_binding_from_recovery,
        )

        warmed = 0
        max_scan = 500
        scanned = 0
        async for redis_key in redis.scan_iter(match="transport_binding:*", count=50):
            scanned += 1
            if scanned > max_scan:
                logger.debug(f"[WARMUP] Transport binding scan cap reached ({max_scan})")
                break
            try:
                data = await redis.get(redis_key)
                if not data:
                    continue
                parsed = json.loads(data)
                agent_uuid = parsed.get("agent_uuid")
                session_key = parsed.get("session_key")
                if not (agent_uuid and session_key):
                    continue
                raw_key = redis_key.decode() if isinstance(redis_key, bytes) else str(redis_key)
                cache_key = raw_key.removeprefix("transport_binding:")
                if not cache_key or cache_key == raw_key:
                    continue
                populate_transport_binding_from_recovery(
                    cache_key,
                    agent_uuid,
                    session_key,
                    source=parsed.get("source", "warmup"),
                )
                warmed += 1
            except Exception:
                continue  # Skip malformed entries

        if warmed > 0:
            logger.info(f"[WARMUP] Pre-populated transport binding cache with {warmed} entry(s)")
        else:
            logger.info("[WARMUP] No transport bindings found to warm")
    except Exception as e:
        logger.warning(f"[WARMUP] Transport binding warmup failed (non-fatal): {e}")


async def substrate_claims_refresh(interval_s: float = 60.0):
    """Keep the substrate-claimed UUIDs current, so the HTTP-reject gates can
    still refuse an enrolled resident if a claims lookup fails (#2682).

    The first load retries with backoff until it succeeds; until then a failed
    gate lookup treats every UUID as possibly claimed, so starting during a
    database outage refuses rather than admits. After that the table is
    reloaded every ``interval_s`` so an enrollment is known within a minute.
    The set only grows (see load_known_substrate_claims)."""
    from src.substrate.verification import load_known_substrate_claims

    await asyncio.sleep(2)  # Wait for DB to be ready
    backoff = 5.0
    attempt = 0
    loaded = False
    while True:
        attempt += 1
        try:
            count = await load_known_substrate_claims()
            if not loaded:
                logger.info("[WARMUP] %d substrate-claimed identities loaded", count)
                loaded = True
        except Exception as e:
            logger.warning("[SUBSTRATE_CLAIMS] load failed (attempt %d): %s", attempt, e)
        if loaded:
            await asyncio.sleep(interval_s)
        else:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def restore_host_cooldowns():
    """Refill provider cooldowns from Redis so a restart does not spend one
    failed call per provider still at its limit. Fail-soft inside."""
    from src.mcp_handlers.support import host_availability

    await host_availability.load_from_redis()
