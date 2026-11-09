"""
Periodic maintenance, sweeper, persistence, and cache-warmup background tasks
for the governance MCP server.

Extracted from src/background_tasks.py. All symbols are re-exported on
src.background_tasks and dynamically resolve logger/asyncio/constants on
src.background_tasks so test monkeypatches continue to work.
"""

import asyncio as _default_asyncio
import sys
from typing import Optional

from src.logging_utils import get_logger

logger = get_logger("src.background_tasks")


def _bt():
    return sys.modules.get("src.background_tasks")


def _bt_attr(name: str, default=None):
    mod = _bt()
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return default


# ---------------------------------------------------------------------------
# S8a Phase-2 class promotion sweep
# ---------------------------------------------------------------------------


async def class_promotion_sweeper_task(interval_minutes: float = 30.0):
    """Promote ephemeral identities with ``total_updates >= 3`` to ``engaged_ephemeral``."""
    aio = _bt_attr("asyncio", _default_asyncio)
    log = _bt_attr("logger", logger)
    await aio.sleep(60.0)
    while True:
        try:
            from src.grounding.class_promotion import promote_engaged_ephemeral

            result = await promote_engaged_ephemeral()
            if result.get("promoted", 0) > 0:
                log.info(
                    f"[CLASS_PROMOTION] {result['promoted']} ephemeral → engaged_ephemeral "
                    f"(threshold={result['threshold']})"
                )
            else:
                log.debug(
                    f"[CLASS_PROMOTION] no promotions (threshold={result['threshold']})"
                )
        except aio.CancelledError:
            break
        except Exception as e:
            log.warning(f"[CLASS_PROMOTION] Sweep failed (will retry): {e}")

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
                log.info(
                    f"[S10_REBUCKET] tracked={rebucket['tracked_agents']} "
                    f"unresolved={rebucket['unresolved_agents']} "
                    f"errors={rebucket['classifier_errors']} "
                    f"buckets={rebucket['buckets']}"
                )
            else:
                log.debug("[S10_REBUCKET] no tracked agents this cycle")
        except aio.CancelledError:
            break
        except Exception as e:
            log.warning(f"[S10_REBUCKET] Sweep failed (will retry): {e}")

        try:
            await aio.sleep(interval_minutes * 60)
        except aio.CancelledError:
            break


# ---------------------------------------------------------------------------
# R2 PR 4 — lineage-eval sweeper
# ---------------------------------------------------------------------------


async def _lineage_eval_sweep_once() -> dict[str, int]:
    """Run a single lineage-eval sweep cycle."""
    from src.db import get_db
    from src.identity.lineage_lifecycle import evaluate_lineage_for

    log = _bt_attr("logger", logger)
    backend = get_db()
    candidates = await backend.select_lineage_eval_candidates()
    transitions = 0
    for successor_id in candidates:
        try:
            outcome = await evaluate_lineage_for(successor_id)
            if outcome.transition is not None:
                transitions += 1
        except Exception as exc:
            log.warning(
                "[R2_SWEEPER] eval failed for %s: %s",
                successor_id[:8], exc,
            )
    return {"candidates": len(candidates), "transitions": transitions}


async def lineage_eval_sweeper_task(interval_minutes: float = 30.0):
    """R2: re-evaluate provisional and confirmed lineage edges."""
    aio = _bt_attr("asyncio", _default_asyncio)
    log = _bt_attr("logger", logger)
    await aio.sleep(60.0)
    while True:
        try:
            sweep_fn = _bt_attr("_lineage_eval_sweep_once", _lineage_eval_sweep_once)
            result = await sweep_fn()
            if result["transitions"] > 0:
                log.info(
                    f"[R2_SWEEPER] cycle complete: {result['candidates']} candidates, "
                    f"{result['transitions']} transition(s)"
                )
            else:
                log.debug(
                    f"[R2_SWEEPER] cycle complete: {result['candidates']} candidates, "
                    f"no transitions"
                )
        except aio.CancelledError:
            break
        except Exception as e:
            log.warning(f"[R2_SWEEPER] Sweep failed (will retry): {e}")
        try:
            await aio.sleep(interval_minutes * 60)
        except aio.CancelledError:
            break


# ---------------------------------------------------------------------------
# Principal (octopus) rollup — derived, off the hot path
# ---------------------------------------------------------------------------


async def principal_rollup_sweeper_task(interval_seconds: float = 60.0):
    """Recompute the DERIVED principal (octopus) rollup from the in-memory
    agent-metadata cache."""
    aio = _bt_attr("asyncio", _default_asyncio)
    log = _bt_attr("logger", logger)
    await aio.sleep(60.0)
    while True:
        try:
            from src.agent_metadata_model import agent_metadata
            from src.services import principal_rollup

            mapped = principal_rollup.recompute(agent_metadata)
            log.debug(f"[PRINCIPAL_ROLLUP] recompute complete: {mapped} principal(s)")
        except aio.CancelledError:
            break
        except Exception as e:
            log.warning(f"[PRINCIPAL_ROLLUP] recompute failed (keeping prior map): {e}")
        try:
            await aio.sleep(interval_seconds)
        except aio.CancelledError:
            break


# ---------------------------------------------------------------------------
# Session cleanup
# ---------------------------------------------------------------------------

_SESSION_CLEANUP_REDIS_TIMEOUT_S = 2.0


async def _session_cleanup_pass() -> tuple[int, int, int]:
    """Run one cleanup pass. Returns ``(pg_deleted, redis_deleted, redis_live_kept)``."""
    aio = _bt_attr("asyncio", _default_asyncio)
    log = _bt_attr("logger", logger)
    redis_timeout_s = _bt_attr(
        "_SESSION_CLEANUP_REDIS_TIMEOUT_S", _SESSION_CLEANUP_REDIS_TIMEOUT_S
    )
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
        log.warning(f"[SESSION_CLEANUP] PG cleanup failed: {e}")

    if expired_session_keys:
        try:
            from src.cache.redis_client import get_redis

            redis = await get_redis()
            if redis is not None:
                for sk in expired_session_keys:
                    key = f"session:{sk}"
                    try:
                        ttl = await aio.wait_for(
                            redis.ttl(key), timeout=redis_timeout_s,
                        )
                        if isinstance(ttl, int) and ttl > 0:
                            redis_live_kept += 1
                            continue
                        if ttl != -1:
                            continue  # -2: already gone; anything else: unknown
                        removed = await aio.wait_for(
                            redis.delete(key), timeout=redis_timeout_s,
                        )
                        if removed:
                            redis_deleted += 1
                    except Exception:
                        pass
        except Exception as e:
            log.warning(f"[SESSION_CLEANUP] Redis cleanup failed: {e}")

    return pg_deleted, redis_deleted, redis_live_kept


async def session_cleanup_task(interval_hours: float = 6.0):
    """Delete expired sessions from PG and orphaned Redis session cache keys."""
    aio = _bt_attr("asyncio", _default_asyncio)
    log = _bt_attr("logger", logger)
    while True:
        await aio.sleep(interval_hours * 3600)
        cleanup_fn = _bt_attr("_session_cleanup_pass", _session_cleanup_pass)
        pg_deleted, redis_deleted, redis_live_kept = await cleanup_fn()
        if pg_deleted or redis_deleted or redis_live_kept:
            log.info(
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
    to ``metrics.series``."""
    aio = _bt_attr("asyncio", _default_asyncio)
    log = _bt_attr("logger", logger)
    targets = _bt_attr("_PERF_PERSIST_TARGETS", _PERF_PERSIST_TARGETS)
    await aio.sleep(90.0)
    log.info(
        f"[PERF_PERSIST] Started perf_monitor → metrics.series persistence "
        f"(every {interval_minutes}m, {len(targets)} series)"
    )
    from src.fleet_metrics.storage import record as _record_metric
    from src.perf_monitor import snapshot as _perf_snapshot

    while True:
        try:
            await aio.sleep(interval_minutes * 60.0)
            targets = _bt_attr("_PERF_PERSIST_TARGETS", _PERF_PERSIST_TARGETS)
            snap = _perf_snapshot()
            written = 0
            for op_key, pct_field, metric_name in targets:
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
                    log.warning(
                        f"[PERF_PERSIST] record {metric_name} failed: {e}"
                    )
            if written:
                log.debug(f"[PERF_PERSIST] wrote {written} series points")

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
                    log.debug(
                        f"[PERF_PERSIST] wrote {len(rows)} lease measurement rows"
                    )
            except Exception as e:
                log.warning(f"[PERF_PERSIST] lease measurement drain failed: {e}")
        except aio.CancelledError:
            log.info("[PERF_PERSIST] Task cancelled")
            break
        except Exception as e:
            log.warning(f"[PERF_PERSIST] Error: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Cache warmup & substrate refresh tasks
# ---------------------------------------------------------------------------


async def transport_binding_cache_warmup():
    """Pre-populate the sticky transport-binding cache from Redis at startup."""
    aio = _bt_attr("asyncio", _default_asyncio)
    log = _bt_attr("logger", logger)
    await aio.sleep(2)  # Wait for DB and Redis to be ready
    try:
        from src.cache import is_redis_available

        if not is_redis_available():
            log.info("[WARMUP] Redis unavailable, skipping transport binding warmup")
            return

        from src.cache.redis_client import get_redis

        redis = await get_redis()
        if not redis:
            log.info("[WARMUP] Redis client not ready, skipping transport binding warmup")
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
                log.debug(f"[WARMUP] Transport binding scan cap reached ({max_scan})")
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
            log.info(f"[WARMUP] Pre-populated transport binding cache with {warmed} entry(s)")
        else:
            log.info("[WARMUP] No transport bindings found to warm")
    except Exception as e:
        log.warning(f"[WARMUP] Transport binding warmup failed (non-fatal): {e}")


async def substrate_claims_refresh(interval_s: float = 60.0):
    """Keep the substrate-claimed UUIDs current, so the HTTP-reject gates can
    still refuse an enrolled resident if a claims lookup fails (#2682)."""
    from src.substrate.verification import load_known_substrate_claims

    aio = _bt_attr("asyncio", _default_asyncio)
    log = _bt_attr("logger", logger)
    await aio.sleep(2)  # Wait for DB to be ready
    backoff = 5.0
    attempt = 0
    loaded = False
    while True:
        attempt += 1
        try:
            count = await load_known_substrate_claims()
            if not loaded:
                log.info("[WARMUP] %d substrate-claimed identities loaded", count)
                loaded = True
        except Exception as e:
            log.warning("[SUBSTRATE_CLAIMS] load failed (attempt %d): %s", attempt, e)
        if loaded:
            await aio.sleep(interval_s)
        else:
            await aio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def identity_cache_warmup():
    """Pre-populate sticky transport identity cache from recent session bindings."""
    aio = _bt_attr("asyncio", _default_asyncio)
    log = _bt_attr("logger", logger)
    await aio.sleep(2)  # Wait for DB and Redis to be ready
    try:
        from src.cache import is_redis_available

        if not is_redis_available():
            log.info("[WARMUP] Redis unavailable, skipping identity cache warmup")
            return

        from src.cache.redis_client import get_redis

        redis = await get_redis()
        if not redis:
            log.info("[WARMUP] Redis client not ready, skipping identity cache warmup")
            return

        import json
        from src.mcp_handlers.middleware.identity_step import update_transport_binding

        warmed = 0
        max_scan = 500
        scanned = 0
        async for key in redis.scan_iter(match="session:*", count=50):
            scanned += 1
            if scanned > max_scan:
                log.debug(f"[WARMUP] Scan cap reached ({max_scan} keys)")
                break
            try:
                data = await redis.get(key)
                if not data:
                    continue
                binding = json.loads(data)
                agent_uuid = binding.get("agent_uuid") or binding.get("agent_id")
                session_key = binding.get("session_key") or (key.decode() if isinstance(key, bytes) else str(key))
                ip_ua_fp = binding.get("ip_ua_fingerprint")
                if agent_uuid and ip_ua_fp:
                    cache_key = f"sticky:{ip_ua_fp}"
                    update_transport_binding(
                        cache_key,
                        agent_uuid,
                        session_key,
                        source="warmup",
                        original_session_source="unknown",
                    )
                    warmed += 1
            except Exception:
                continue  # Skip malformed entries

        if warmed > 0:
            log.info(f"[WARMUP] Pre-populated identity cache with {warmed} binding(s)")
        else:
            log.info("[WARMUP] No recent session bindings found to warm")
    except Exception as e:
        log.warning(f"[WARMUP] Identity cache warmup failed (non-fatal): {e}")


async def restore_host_cooldowns():
    """Refill provider cooldowns from Redis so a restart does not spend one
    failed call per provider still at its limit. Fail-soft inside."""
    from src.mcp_handlers.support import host_availability

    await host_availability.load_from_redis()
