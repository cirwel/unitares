"""
Background tasks for the governance MCP server.

Extracted from mcp_server.py to reduce file size and improve maintainability.
Each task runs as an asyncio coroutine, started during server initialization.
"""

import asyncio
import gzip
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.logging_utils import get_logger
from src.connection_tracker import CONNECTIONS_ACTIVE

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Connection heartbeat
# ---------------------------------------------------------------------------

async def connection_heartbeat_task(connection_tracker):
    """
    Comprehensive connection health monitoring:
    - Clean up stale connections every 5 minutes
    - Check health of all connections every 2 minutes
    - Log diagnostic summary every 10 minutes
    """
    consecutive_failures = 0
    max_consecutive_failures = 5
    iteration = 0

    while True:
        try:
            await asyncio.sleep(60)
            iteration += 1

            if iteration % 2 == 0:
                for client_id in list(connection_tracker.connections.keys()):
                    try:
                        health = await connection_tracker.check_health(client_id)
                        if not health.get("healthy"):
                            logger.warning(
                                f"[HEARTBEAT] Unhealthy connection: {client_id} - {health.get('issues', [])}"
                            )
                    except Exception as e:
                        logger.debug(f"[HEARTBEAT] Health check failed for {client_id}: {e}")

            if iteration % 5 == 0:
                await connection_tracker.cleanup_stale_connections(max_idle_minutes=30.0)

            if iteration % 10 == 0:
                diagnostics = await connection_tracker.get_diagnostics()
                health_summary = diagnostics.get("health_summary", {})
                reconnect_summary = diagnostics.get("reconnection_summary", {})

                logger.info(
                    f"[HEARTBEAT] Connection summary: "
                    f"{diagnostics['total_connections']} connected, "
                    f"{health_summary.get('healthy', 0)} healthy, "
                    f"{health_summary.get('degraded', 0)} degraded"
                )

                high_reconnectors = {k: v for k, v in reconnect_summary.items() if v > 5}
                if high_reconnectors:
                    logger.warning(
                        f"[HEARTBEAT] High reconnection clients: {high_reconnectors}. "
                        f"Check network stability."
                    )

                CONNECTIONS_ACTIVE.set(diagnostics['total_connections'])

            consecutive_failures = 0

        except asyncio.CancelledError:
            logger.info("[HEARTBEAT] Connection heartbeat task cancelled")
            break
        except Exception as e:
            consecutive_failures += 1
            logger.warning(
                f"[HEARTBEAT] Error (failure {consecutive_failures}/{max_consecutive_failures}): {e}",
                exc_info=True
            )
            if consecutive_failures >= max_consecutive_failures:
                logger.error(
                    f"[HEARTBEAT] Failed {consecutive_failures} times consecutively. "
                    f"Connection monitoring degraded. Consider restarting the server."
                )
                consecutive_failures = 0


# ---------------------------------------------------------------------------
# Auto calibration / ground truth
# ---------------------------------------------------------------------------

async def startup_auto_calibration():
    """Start automatic ground truth collection at startup and periodically."""
    await asyncio.sleep(1.0)

    # Load calibration from DB now that the event loop is running.
    # sync load_state() at __init__ time can only read JSON; this gets the DB state.
    try:
        from src.calibration import get_calibration_checker
        await get_calibration_checker().load_state_async()
        logger.info("[CALIBRATION] Loaded calibration state from DB")
    except Exception as e:
        logger.warning(f"[CALIBRATION] Async calibration load failed (JSON fallback used): {e}")

    try:
        from src.auto_ground_truth import collect_ground_truth_automatically, auto_ground_truth_collector_task

        result = await collect_ground_truth_automatically(
            min_age_hours=2.0, max_decisions=50, dry_run=False
        )
        if result.get('updated', 0) > 0:
            logger.info(f"Auto-collected ground truth: {result['updated']} decisions updated")

        _supervised_create_task(
            auto_ground_truth_collector_task(interval_hours=6.0),
            name="auto_ground_truth_collector",
        )
        logger.info("Started periodic auto ground truth collector (runs every 6 hours)")
    except Exception as e:
        logger.warning(f"Could not start auto ground truth collector: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# KG lifecycle cleanup
# ---------------------------------------------------------------------------

async def startup_kg_lifecycle():
    """Start periodic KG lifecycle cleanup after server init."""
    await asyncio.sleep(5.0)
    try:
        from src.knowledge_graph_lifecycle import kg_lifecycle_background_task, run_kg_lifecycle_cleanup

        # KG cleanup does AGE graph queries that block the event loop in the
        # MCP server's anyio context. Wrap in timeout to prevent server freeze.
        try:
            result = await asyncio.wait_for(run_kg_lifecycle_cleanup(dry_run=False), timeout=10.0)
            archived = result.get("ephemeral_archived", 0) + result.get("discoveries_archived", 0)
            if archived > 0:
                logger.info(f"KG lifecycle startup: archived {archived} entries")
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning("KG lifecycle startup cleanup timed out (non-fatal)")

        _supervised_create_task(
            kg_lifecycle_background_task(interval_hours=24.0),
            name="kg_lifecycle",
        )
        logger.info("Started periodic KG lifecycle cleanup (runs every 24 hours)")
    except Exception as e:
        logger.warning(f"Could not start KG lifecycle task: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Concept extraction
# ---------------------------------------------------------------------------

async def concept_extraction_background_task(interval_hours: float = 24.0):
    """Daily concept extraction from tags + embeddings."""
    await asyncio.sleep(300)  # 5 min startup delay
    while True:
        try:
            from src.concept_extraction import ConceptExtractor
            extractor = ConceptExtractor()
            result = await extractor.run()
            logger.info(f"[CONCEPT_EXTRACTION] {result}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"[CONCEPT_EXTRACTION] Skipped: {e}")
        try:
            await asyncio.sleep(interval_hours * 3600)
        except asyncio.CancelledError:
            break


# ---------------------------------------------------------------------------
# S8a Phase-2 class promotion sweep
# ---------------------------------------------------------------------------

async def class_promotion_sweeper_task(interval_minutes: float = 30.0):
    """Promote ephemeral identities with ``total_updates >= 3`` to ``engaged_ephemeral``.

    See ``src/grounding/class_promotion.py`` for the rule and
    ``docs/ontology/s8a-phase2-prep.md`` for the audit that produced the
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
        # Isolated try-block so a rebucket failure does not abort the sweeper
        # loop or starve future promotion cycles. Telemetry is logged at INFO
        # when meaningful (non-zero tracked or any errors) and at DEBUG on
        # the idle path so the cycle stays quiet under steady state.
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

    See: docs/ontology/r2-honest-memory-integration.md §"Evaluation
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
# Materialized view refresh (moved from per-insert to periodic)
# ---------------------------------------------------------------------------

async def periodic_matview_refresh():
    """Refresh mv_latest_agent_states periodically instead of per-insert."""
    await asyncio.sleep(30.0)
    while True:
        try:
            from src.db import get_db
            db = get_db()
            async with db.acquire() as conn:
                await conn.execute(
                    "REFRESH MATERIALIZED VIEW CONCURRENTLY core.mv_latest_agent_states"
                )
        except Exception as e:
            logger.debug(f"Matview refresh skipped: {e}")
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Partition maintenance
# ---------------------------------------------------------------------------

async def periodic_partition_maintenance():
    """Run audit.partition_maintenance() weekly to create/drop partitions."""
    await asyncio.sleep(60.0)
    while True:
        try:
            from src.db import get_db
            db = get_db()
            async with db.acquire() as conn:
                result = await conn.fetchval("SELECT audit.partition_maintenance()")
            logger.info(f"Partition maintenance completed: {result}")
        except Exception as e:
            logger.debug(f"Partition maintenance skipped: {e}")
        await asyncio.sleep(7 * 24 * 3600)


# ---------------------------------------------------------------------------
# Metadata loading
# ---------------------------------------------------------------------------

async def background_metadata_load():
    """Load metadata in background after server starts accepting connections."""
    await asyncio.sleep(0.5)
    try:
        from src.agent_state import load_metadata_async
        await load_metadata_async()
        logger.info("[STARTUP] Background metadata load complete")
    except Exception as e:
        logger.warning(f"[STARTUP] Background metadata load failed: {e}. Lazy loading will handle on first access.")


# ---------------------------------------------------------------------------
# Orphan agent cleanup — automatic sweep removed 2026-04-19.
#
# The periodic sweep used to hide real onboarding/check-in bugs (initializing
# agents being archived before their first check-in). The canonical engine
# ``auto_archive_orphan_agents`` remains available for manual operator use via
# the ``archive_orphan_agents`` MCP tool, which defaults to dry_run=True.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Stuck agent recovery
# ---------------------------------------------------------------------------

async def stuck_agent_recovery_task():
    """Automatically detect and recover stuck agents every 5 minutes."""
    await asyncio.sleep(10.0)

    interval_minutes = 5.0
    interval_seconds = interval_minutes * 60

    logger.info(f"[STUCK_AGENT_RECOVERY] Starting automatic recovery (runs every {interval_minutes} minutes)")

    while True:
        try:
            await asyncio.sleep(interval_seconds)

            from src.mcp_handlers.lifecycle.handlers import handle_detect_stuck_agents

            result = await handle_detect_stuck_agents({
                "max_age_minutes": 30.0,
                "critical_margin_timeout_minutes": 5.0,
                "tight_margin_timeout_minutes": 15.0,
                "auto_recover": True,
                "min_updates": 1,
                "note_cooldown_minutes": 120.0
            })

            if result and len(result) > 0:
                import json
                try:
                    from mcp.types import TextContent
                    result_text = result[0].text if isinstance(result[0], TextContent) else str(result[0])

                    if result_text.strip().startswith('{'):
                        result_data = json.loads(result_text)
                        stuck_agents = result_data.get('stuck_agents', [])
                        recovered = result_data.get('recovered', [])

                        if len(stuck_agents) > 0 or len(recovered) > 0:
                            logger.info(
                                f"[STUCK_AGENT_RECOVERY] Detected {len(stuck_agents)} stuck agent(s), "
                                f"recovered {len(recovered)} safe agent(s)"
                            )
                            for rec in recovered:
                                logger.debug(
                                    f"[STUCK_AGENT_RECOVERY] Recovered agent {rec.get('agent_id', 'unknown')[:8]}... "
                                    f"(reason: {rec.get('reason', 'unknown')})"
                                )
                except (json.JSONDecodeError, AttributeError, KeyError) as e:
                    logger.debug(f"[STUCK_AGENT_RECOVERY] Could not parse result: {e}")

        except asyncio.CancelledError:
            logger.info("[STUCK_AGENT_RECOVERY] Task cancelled")
            break
        except Exception as e:
            logger.warning(f"[STUCK_AGENT_RECOVERY] Error in recovery task: {e}", exc_info=True)
            await asyncio.sleep(60.0)


# ---------------------------------------------------------------------------
# Server warmup
# ---------------------------------------------------------------------------

async def server_warmup_task(set_ready):
    """Set server ready flag after short warmup to allow MCP initialization."""
    await asyncio.sleep(2.0)
    set_ready()
    logger.info("[WARMUP] Server ready to accept requests (warmup complete)")


# ---------------------------------------------------------------------------
# Deep health probe (Option F — cached health snapshots)
# ---------------------------------------------------------------------------

PROBE_TIMEOUT_SECONDS = 15.0  # Hard ceiling on a single probe call


async def deep_health_probe_task(interval_seconds: float | None = None):
    """Periodically run the deep health check and cache the result.

    Runs in the main event loop alongside other background tasks, NOT inside
    an MCP tool handler's anyio context. This sidesteps the anyio/asyncpg
    deadlock that makes calling get_health_check_data from a handler hang.
    Readers (the health_check MCP handler, /health/deep REST endpoint) serve
    the cached snapshot instead of touching the DB at request time.

    Each probe is bounded by PROBE_TIMEOUT_SECONDS. If a probe exceeds the
    budget we log a warning and keep whatever snapshot was there before —
    the `_cache.stale` flag will trip naturally based on age. That way a
    single slow component (huge Redis keyspace, KG stall, etc.) cannot lock
    the probe task forever.

    See docs/handoffs/2026-04-10-option-f-spec.md.
    """
    import os
    from src.services.health_snapshot import (
        set_snapshot,
        PROBE_INTERVAL_SECONDS,
    )

    if interval_seconds is None:
        override = os.getenv("UNITARES_HEALTH_PROBE_INTERVAL_SECONDS")
        interval_seconds = float(override) if override else PROBE_INTERVAL_SECONDS

    # Let the DB pool warm up before the first probe
    await asyncio.sleep(5.0)
    logger.info(
        f"[HEALTH_PROBE] Starting deep health probe "
        f"(every {interval_seconds}s, timeout {PROBE_TIMEOUT_SECONDS}s)"
    )

    while True:
        try:
            from src.services.runtime_queries import get_health_check_data
            # lite=False → capture full per-check detail; the handler filters at read time.
            # Bounded by PROBE_TIMEOUT_SECONDS so a single hang can't freeze the task.
            snapshot = await asyncio.wait_for(
                get_health_check_data({"lite": False}),
                timeout=PROBE_TIMEOUT_SECONDS,
            )
            await set_snapshot(snapshot)
            logger.debug("[HEALTH_PROBE] Snapshot refreshed")
        except asyncio.TimeoutError:
            logger.warning(
                f"[HEALTH_PROBE] Probe exceeded {PROBE_TIMEOUT_SECONDS}s budget — "
                f"keeping previous snapshot; staleness will trip naturally."
            )
        except asyncio.CancelledError:
            logger.info("[HEALTH_PROBE] Task cancelled")
            break
        except Exception as e:
            logger.warning(f"[HEALTH_PROBE] Probe failed: {e}", exc_info=True)

        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break


# ---------------------------------------------------------------------------
# Resident-progress flat probe
# ---------------------------------------------------------------------------

async def progress_flat_probe_task(interval_seconds: float | None = None):
    """Resident-progress telemetry probe. See plan Task 9 + spec.

    Runs in the main event loop alongside other background tasks.
    Builds the full dependency graph (sources, heartbeat, writer, audit
    emitter) at startup then loops calling probe.tick() every
    UNITARES_PROGRESS_FLAT_PROBE_INTERVAL_SECONDS seconds (default 300s).
    """
    import os

    if interval_seconds is None:
        override = os.getenv("UNITARES_PROGRESS_FLAT_PROBE_INTERVAL_SECONDS")
        interval_seconds = float(override) if override else 300.0

    # Let the DB pool warm up before the first probe (mirror deep_health_probe pattern).
    await asyncio.sleep(5.0)

    # Lazy imports to avoid circular import at module load.
    from src.db import get_db
    from src.resident_progress.heartbeat import HeartbeatEvaluator
    from src.resident_progress.probe_task import ProgressFlatProbe
    from src.resident_progress.sentinel_source import SentinelPulseSource
    from src.resident_progress.snapshot_writer import SnapshotWriter
    from src.resident_progress.sources import (
        EISVSyncSource,
        KnowledgeDiscoverySource,
        MetricsSeriesSource,
        WatcherFindingSource,
    )

    db = get_db()

    class _AuditEmitter:
        async def emit(self, *, event_type, severity, payload):
            # audit_logger has specific named methods — no generic log_event().
            # Use _write_entry with a synthetic AuditEntry to stay consistent
            # with the existing pattern without adding a new public method.
            try:
                from src.audit_log import audit_logger, AuditEntry
                from datetime import datetime
                entry = AuditEntry(
                    timestamp=datetime.now().isoformat(),
                    agent_id="progress_flat_probe",
                    event_type=event_type,
                    confidence=1.0,
                    details={"severity": severity, **payload},
                )
                audit_logger._write_entry(entry)
            except Exception as e:
                logger.warning("[PROGRESS_FLAT] audit emit failed: %s", e)

    class _MetadataStore:
        async def get(self, agent_uuid: str):
            # Use get_agent from agent_storage — that's the canonical async
            # identity/metadata fetch.  AgentRecord.last_activity_at maps to
            # last_update. Cadence is supplied by the probe via
            # cadence_override_s from the registry; we no longer fabricate
            # a 60s default for residents whose natural cadence is hours
            # or days.
            try:
                from src.agent_storage import get_agent
                record = await get_agent(agent_uuid)
                if record is None:
                    return None
                return {
                    "last_update": record.last_activity_at,
                    "expected_cadence_s": (
                        record.metadata.get("expected_cadence_s")
                        or record.metadata.get("cadence_s")
                    ),
                }
            except Exception:
                return None

    sources = {
        "kg_writes":        KnowledgeDiscoverySource(db),
        "watcher_findings": WatcherFindingSource(db),
        "eisv_sync_rows":   EISVSyncSource(db),
        "metrics_series":   MetricsSeriesSource(db),
        "sentinel_pulse":   SentinelPulseSource(db),
    }
    probe = ProgressFlatProbe(
        sources_by_name=sources,
        heartbeat_evaluator=HeartbeatEvaluator(_MetadataStore()),
        writer=SnapshotWriter(db),
        audit_emitter=_AuditEmitter(),
    )
    logger.info(
        "[PROGRESS_FLAT] probe started; interval=%ss", interval_seconds,
    )
    while True:
        try:
            await probe.tick()
        except asyncio.CancelledError:
            logger.info("[PROGRESS_FLAT] task cancelled")
            break
        except Exception as e:
            logger.warning("[PROGRESS_FLAT] tick failed: %s", e, exc_info=True)
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break


# ---------------------------------------------------------------------------
# Session cleanup
# ---------------------------------------------------------------------------

async def session_cleanup_task(interval_hours: float = 6.0):
    """Delete expired sessions from PG and orphaned Redis session cache keys."""
    while True:
        await asyncio.sleep(interval_hours * 3600)
        pg_deleted = 0
        redis_deleted = 0

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
                        try:
                            removed = await redis.delete(f"session:{sk}")
                            if removed:
                                redis_deleted += 1
                        except Exception:
                            pass
            except Exception as e:
                logger.warning(f"[SESSION_CLEANUP] Redis cleanup failed: {e}")

        if pg_deleted or redis_deleted:
            logger.info(
                f"[SESSION_CLEANUP] Deleted {pg_deleted} expired PG sessions, "
                f"{redis_deleted} Redis cache keys"
            )


# ---------------------------------------------------------------------------
# Coherence monitoring
# ---------------------------------------------------------------------------

async def coherence_monitoring_task(interval_minutes: float = 10.0):
    """Proactively monitor agent coherence and log warnings for declining agents."""
    from config.governance_config import config

    await asyncio.sleep(30.0)  # Let server settle
    target = config.TARGET_COHERENCE

    logger.info(f"[COHERENCE_MONITOR] Started (target={target}, interval={interval_minutes}m)")

    while True:
        try:
            await asyncio.sleep(interval_minutes * 60)

            from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
            monitors = getattr(mcp_server, 'monitors', {})
            if not monitors:
                continue

            for agent_id, monitor in list(monitors.items()):
                try:
                    coherence = getattr(monitor.state, 'coherence', None)
                    if coherence is None:
                        continue
                    if coherence < 0.45:
                        logger.error(
                            f"[COHERENCE_MONITOR] CRITICAL: Agent {agent_id[:12]}... "
                            f"coherence={coherence:.3f} (target={target})"
                        )
                    elif coherence < target:
                        logger.warning(
                            f"[COHERENCE_MONITOR] Below target: Agent {agent_id[:12]}... "
                            f"coherence={coherence:.3f} (target={target})"
                        )
                except Exception:
                    pass

        except asyncio.CancelledError:
            logger.info("[COHERENCE_MONITOR] Task cancelled")
            break
        except Exception as e:
            logger.warning(f"[COHERENCE_MONITOR] Error: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Telemetry & log rotation
# ---------------------------------------------------------------------------

async def periodic_telemetry_rotation(interval_hours: float = 24.0):
    """Rotate drift_telemetry.jsonl daily when >100MB."""
    await asyncio.sleep(120.0)  # Let server settle
    while True:
        try:
            from src.drift_telemetry import get_telemetry
            telemetry = get_telemetry()
            result = telemetry.rotate(max_size_mb=50.0, archive_months=3)
            if result:
                logger.info(f"[ROTATION] Drift telemetry rotated -> {result}")
        except Exception as e:
            logger.debug(f"[ROTATION] Drift telemetry rotation skipped: {e}")
        await asyncio.sleep(interval_hours * 3600)


async def periodic_audit_log_rotation(interval_hours: float = 24.0):
    """Rotate audit_log.jsonl daily. Data is fully duplicated in PostgreSQL.

    Trim window matches the default 7-day query horizon for ``get_skip_rate_metrics``
    and ``query_audit_log`` callers — there is no reason to keep older data hot in
    JSONL when Postgres has the durable copy.
    """
    await asyncio.sleep(180.0)
    while True:
        try:
            from src.audit_log import get_audit_log
            audit = get_audit_log()
            kept, archive_path = audit.rotate_log(max_age_days=7)
            if archive_path:
                logger.info(f"[ROTATION] Audit log rotated: {kept} entries kept, archived to {archive_path}")
        except Exception as e:
            logger.debug(f"[ROTATION] Audit log rotation skipped: {e}")
        await asyncio.sleep(interval_hours * 3600)


async def periodic_tool_usage_rotation(interval_hours: float = 24.0):
    """Rotate tool_usage.jsonl daily. No Postgres mirror, so we keep 30 days hot."""
    await asyncio.sleep(240.0)
    while True:
        try:
            from src.tool_usage_tracker import get_tool_usage_tracker
            tracker = get_tool_usage_tracker()
            kept, archive_path = tracker.rotate_log(max_age_days=30)
            if archive_path:
                logger.info(f"[ROTATION] Tool usage log rotated: {kept} entries kept, archived to {archive_path}")
        except Exception as e:
            logger.debug(f"[ROTATION] Tool usage log rotation skipped: {e}")
        await asyncio.sleep(interval_hours * 3600)


async def periodic_server_log_rotation(interval_hours: float = 24.0, max_size_mb: float = 50.0):
    """Rotate launchd-managed server log files by copy+truncate."""
    await asyncio.sleep(300.0)

    project_root = Path(__file__).parent.parent
    log_dir = project_root / "data" / "logs"
    archive_dir = log_dir / "archive"

    log_files = ["mcp_server.log", "mcp_server_error.log"]

    while True:
        for log_name in log_files:
            log_path = log_dir / log_name
            try:
                if not log_path.exists():
                    continue
                size_mb = log_path.stat().st_size / (1024 * 1024)
                if size_mb < max_size_mb:
                    continue

                archive_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                stem = log_path.stem
                archive_path = archive_dir / f"{stem}_{stamp}.log.gz"

                # Copy then truncate in-place (launchd holds the fd)
                with open(log_path, 'rb') as f_in:
                    with gzip.open(archive_path, 'wb') as f_out:
                        shutil.copyfileobj(f_in, f_out)

                # Truncate original (launchd's fd stays valid)
                with open(log_path, 'w') as f:
                    pass

                logger.info(f"[ROTATION] {log_name} ({size_mb:.0f}MB) -> {archive_path}")

                # Prune archives older than 6 months
                _prune_log_archives(archive_dir, stem, keep_months=6)

            except Exception as e:
                logger.debug(f"[ROTATION] {log_name} rotation failed: {e}")

        await asyncio.sleep(interval_hours * 3600)


def _prune_log_archives(archive_dir: Path, stem: str, keep_months: int = 6):
    """Remove log archives older than keep_months."""
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=keep_months * 30)
    for gz_file in sorted(archive_dir.glob(f"{stem}_*.log.gz")):
        try:
            date_str = gz_file.stem.replace(f"{stem}_", "").replace(".log", "")
            file_date = datetime.strptime(date_str, "%Y%m%d_%H%M%S")
            if file_date < cutoff:
                gz_file.unlink()
                logger.info(f"[ROTATION] Pruned old log archive: {gz_file.name}")
        except (ValueError, OSError):
            pass


# ---------------------------------------------------------------------------
# Orchestrator — called from mcp_server.py
# ---------------------------------------------------------------------------

_supervised_tasks: list = []


def _on_background_task_done(task: asyncio.Task) -> None:
    """Callback for background task completion — logs crashes, emits a
    coordination_failure event for cancellations, and removes the task
    from the supervised list.

    The cancellation emit is Wave 0 step 2C-1 (RFC roadmap §86): per the
    v0.2 scoping doc §2, the OUTER supervisor is the single point that
    sees every supervised-task cancellation, so one emit here covers all
    background tasks without per-site instrumentation."""
    if task.cancelled():
        _emit_background_task_cancellation(task.get_name())
    else:
        exc = task.exception()
        if exc:
            logger.error(
                f"Background task '{task.get_name()}' crashed: {exc}",
                exc_info=exc,
            )
    # Prevent unbounded growth of _supervised_tasks
    try:
        _supervised_tasks.remove(task)
    except ValueError:
        pass


def _emit_background_task_cancellation(task_name: str) -> None:
    """Failure-safe emit for `coordination_failure.anyio_cancellation.background_task`.

    The inner function is failure-safe by contract; the outer try/except
    is defense-in-depth so an ImportError at the wire-up site cannot break
    the supervisor's bookkeeping (the `_supervised_tasks.remove(...)` that
    follows in `_on_background_task_done`)."""
    try:
        from uuid import uuid4

        from src.coordination_failure_emit import emit_coordination_failure_sync

        emit_coordination_failure_sync(
            service="governance_mcp",
            event_type="coordination_failure.anyio_cancellation.background_task",
            payload={
                "task_name": task_name,
                "incident_id": str(uuid4()),
            },
            agent_id=None,
        )
    except Exception as exc:  # noqa: BLE001 — observability MUST NOT mask the real bug
        logger.warning(
            "[coord-events] background_task cancellation emit raised — "
            "supervisor bookkeeping continues: %r",
            exc,
        )


# ---------------------------------------------------------------------------
# Agent silence detection
# ---------------------------------------------------------------------------

# Expected check-in intervals in seconds, derived from ``cadence.*`` agent tags.
# Generic — any agent with a cadence tag gets the matching interval regardless
# of label. Adding a new cadence is a single-line change here.
CADENCE_FROM_TAG: dict[str, int] = {
    "cadence.1min": 60,
    "cadence.5min": 300,
    "cadence.10min": 600,
    "cadence.30min": 1800,
    "cadence.1hr": 3600,
    "cadence.6hr": 21600,
    "cadence.24hr": 86400,
}


def cadence_from_tags(tags) -> int | None:
    """Return the expected check-in interval (seconds) for an agent from its tags, or None."""
    for tag in (tags or []):
        interval = CADENCE_FROM_TAG.get(tag)
        if interval is not None:
            return interval
    return None


# Back-compat label-based intervals: used only when an agent has no
# ``cadence.*`` tag yet. Retire this once Lumen/Vigil/Sentinel are tagged.
_PERSISTENT_AGENT_INTERVALS = {
    "Vigil": 1800,     # 30 min
    "Lumen": 300,      # 5 min
    "Sentinel": 600,   # 10 min
    "Watcher": 21600,  # 6 hr; hook-driven, not a 5-minute daemon
}

_silence_alerted: set[str] = set()
_silence_critical_alerted: set[str] = set()
_silence_duplicate_warned: set[str] = set()
_silence_server_start: datetime | None = None  # set on first iteration

# Proxy agents whose recent activity proves another agent is alive.
# Maps agent label → label of the proxy agent that calls the same host.
# When the proxy has checked in recently, a missing direct check-in is
# a path issue (circuit breaker, threading) not a real outage.
_SILENCE_PROXY_AGENTS: dict[str, str] = {}


def _safe_total_updates(meta) -> int:
    try:
        return int(getattr(meta, "total_updates", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _canonical_active_resident_ids(agent_metadata_map) -> set[str]:
    """Return active resident rows that should drive silence detection.

    Resident label collisions can leave a fresh 0-update fork active beside the
    canonical resident. Alerting on the fork says "Sentinel is down" while the
    real Sentinel is healthy. For duplicate resident labels, prefer rows that
    have real updates, then the freshest update timestamp.
    """
    try:
        from src.grounding.class_indicator import KNOWN_RESIDENT_LABELS
    except Exception:
        KNOWN_RESIDENT_LABELS = frozenset()

    by_label: dict[str, tuple[str, object, datetime | None]] = {}
    for agent_id, meta in list(agent_metadata_map.items()):
        if getattr(meta, "status", None) != "active":
            continue
        label = getattr(meta, "label", None)
        if not label or label not in KNOWN_RESIDENT_LABELS:
            continue
        last = _parse_last_update_aware(getattr(meta, "last_update", None) or "")
        current = by_label.get(label)
        if current is None:
            by_label[label] = (agent_id, meta, last)
            continue

        _, current_meta, current_last = current
        total = _safe_total_updates(meta)
        current_total = _safe_total_updates(current_meta)
        has_updates = total > 0
        current_has_updates = current_total > 0
        if has_updates and not current_has_updates:
            by_label[label] = (agent_id, meta, last)
            continue
        if (
            has_updates == current_has_updates
            and last
            and (current_last is None or last > current_last)
        ):
            by_label[label] = (agent_id, meta, last)
            continue
        if has_updates == current_has_updates and last == current_last and total > current_total:
            by_label[label] = (agent_id, meta, last)

    return {agent_id for agent_id, _, _ in by_label.values()}


def _get_expected_interval(meta) -> int | None:
    """Return expected check-in interval for persistent agents, None for ephemeral.

    Priority:
      1. ``cadence.*`` tag (generic, label-independent)
      2. Hardcoded label fallback (back-compat; retires with tag migration)
      3. ``embodied`` / ``autonomous`` tag → 300s default
    """
    tags = meta.tags or []
    tagged_cadence = cadence_from_tags(tags)
    if tagged_cadence is not None:
        return tagged_cadence
    if meta.label and meta.label in _PERSISTENT_AGENT_INTERVALS:
        return _PERSISTENT_AGENT_INTERVALS[meta.label]
    if "embodied" in tags or "autonomous" in tags:
        return 300  # default for embodied/autonomous agents
    return None  # ephemeral — skip


def _parse_last_update_aware(last_update: str) -> datetime | None:
    """Parse ``meta.last_update`` into a tz-aware UTC datetime.

    The stored string may be naive (written at runtime via
    ``datetime.now().isoformat()``) or tz-aware (hydrated from the
    postgres ``TIMESTAMPTZ`` column). Normalising here prevents the
    silence detector from silently swallowing a ``TypeError`` when the
    two forms are subtracted.
    """
    try:
        parsed = datetime.fromisoformat(last_update)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed


def _proxy_alive(label: str, now: datetime, threshold_seconds: float) -> bool:
    """Return True if a proxy agent has checked in recently.

    Used to distinguish 'agent truly unreachable' from 'check-in path
    broken but host is alive'. The proxy must have updated within
    *threshold_seconds* of *now*.
    """
    from src.agent_metadata_model import agent_metadata

    proxy_key = _SILENCE_PROXY_AGENTS.get(label)
    if not proxy_key:
        return False
    # The proxy may be keyed by agent_id or by label depending on how
    # it was registered. Search both.
    for _aid, _meta in agent_metadata.items():
        match = (_aid == proxy_key) or (getattr(_meta, 'label', None) == proxy_key)
        if not match:
            continue
        if _meta.status != "active":
            continue
        proxy_last = _parse_last_update_aware(_meta.last_update or "")
        if proxy_last is None:
            continue
        if (now - proxy_last).total_seconds() <= threshold_seconds:
            return True
    return False


async def _silence_check_iteration() -> None:
    """Single pass of silence detection. Extracted for testability."""
    global _silence_server_start
    from src.agent_metadata_model import agent_metadata
    from src.broadcaster import broadcaster_instance
    from src.audit_db import append_audit_event_async

    now = datetime.now(timezone.utc)

    # Record server start time on first call.  Only alert about silence
    # that accumulated *while this process was running* — pre-existing
    # staleness from Mac sleep / prior shutdown is not actionable.
    if _silence_server_start is None:
        _silence_server_start = now

    canonical_residents = _canonical_active_resident_ids(agent_metadata)

    for agent_id, meta in list(agent_metadata.items()):
        if meta.status != "active":
            continue
        label = getattr(meta, "label", None)
        if label and canonical_residents and agent_id not in canonical_residents:
            try:
                from src.grounding.class_indicator import KNOWN_RESIDENT_LABELS
            except Exception:
                KNOWN_RESIDENT_LABELS = frozenset()
            if label in KNOWN_RESIDENT_LABELS:
                if agent_id not in _silence_duplicate_warned:
                    _silence_duplicate_warned.add(agent_id)
                    logger.warning(
                        f"[SILENCE] Skipping duplicate resident row {label} "
                        f"({agent_id[:8]}...) for silence detection; canonical "
                        "active row exists"
                    )
                continue
        interval = _get_expected_interval(meta)
        if interval is None:
            continue
        if not meta.last_update:
            continue

        last = _parse_last_update_aware(meta.last_update)
        if last is None:
            continue

        # Cap silence to time since this server started — don't alert
        # for gaps that occurred before we were running.
        effective_last = max(last, _silence_server_start)
        silence_seconds = (now - effective_last).total_seconds()

        silence_minutes = silence_seconds / 60

        if silence_seconds >= interval * 5 and agent_id not in _silence_critical_alerted:
            # Before firing CRITICAL, check if a proxy agent proves the
            # host is alive. Use 2× the agent's expected interval as the
            # proxy freshness threshold (generous, since the proxy may
            # run on a different cadence).
            if _proxy_alive(meta.label, now, threshold_seconds=interval * 2):
                if agent_id not in _silence_alerted:
                    _silence_alerted.add(agent_id)
                    logger.warning(
                        f"[SILENCE] {meta.label or agent_id[:12]} check-in path silent for {silence_minutes:.0f}m "
                        f"(expected every {interval // 60}m) — proxy alive, suppressing CRITICAL"
                    )
                    await broadcaster_instance.broadcast_event(
                        "lifecycle_silent",
                        agent_id=agent_id,
                        payload={
                            "silence_duration_minutes": round(silence_minutes, 1),
                            "expected_interval_minutes": interval // 60,
                            "label": meta.label,
                            "proxy_alive": True,
                        },
                    )
            else:
                _silence_critical_alerted.add(agent_id)
                _silence_alerted.add(agent_id)  # prevent downgrade WARNING after CRITICAL
                logger.error(
                    f"[SILENCE] CRITICAL: {meta.label or agent_id[:12]} silent for {silence_minutes:.0f}m "
                    f"(expected every {interval // 60}m)"
                )
                await broadcaster_instance.broadcast_event(
                    "lifecycle_silent_critical",
                    agent_id=agent_id,
                    payload={
                        "silence_duration_minutes": round(silence_minutes, 1),
                        "expected_interval_minutes": interval // 60,
                        "label": meta.label,
                    },
                )
        elif silence_seconds >= interval * 2 and agent_id not in _silence_alerted:
            _silence_alerted.add(agent_id)
            logger.warning(
                f"[SILENCE] {meta.label or agent_id[:12]} silent for {silence_minutes:.0f}m "
                f"(expected every {interval // 60}m)"
            )
            await broadcaster_instance.broadcast_event(
                "lifecycle_silent",
                agent_id=agent_id,
                payload={
                    "silence_duration_minutes": round(silence_minutes, 1),
                    "expected_interval_minutes": interval // 60,
                    "label": meta.label,
                },
            )
            await append_audit_event_async({
                "timestamp": now.isoformat(),
                "event_type": "agent_silent",
                "agent_id": agent_id,
                "details": {
                    "silence_duration_minutes": round(silence_minutes, 1),
                    "expected_interval_minutes": interval // 60,
                    "label": meta.label,
                },
            })
        elif silence_seconds < interval * 2:
            # Agent recovered — clear alert state
            _silence_alerted.discard(agent_id)
            _silence_critical_alerted.discard(agent_id)

    # Prune alert sets — remove agents no longer active
    active_ids = {aid for aid, m in agent_metadata.items() if m.status == "active"}
    _silence_alerted.intersection_update(active_ids)
    _silence_critical_alerted.intersection_update(active_ids)
    _silence_duplicate_warned.intersection_update(active_ids)


async def check_agent_silence():
    """Detect persistent agents that have missed expected check-ins."""
    await asyncio.sleep(120)  # startup delay
    while True:
        try:
            await _silence_check_iteration()
        except Exception as e:
            logger.debug(f"[SILENCE] Check failed: {e}")

        await asyncio.sleep(600)  # every 10 minutes


async def transport_binding_cache_warmup():
    """Pre-populate the sticky transport-binding cache from Redis at startup.

    Mirrors identity_cache_warmup but reads the transport_binding:* keys that
    identity_step.py writes. Running this at boot means the dispatch-path
    Redis read (guarded by wait_for) rarely has to fire, which keeps the
    anyio-asyncio deadlock off the critical path.
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

        from src.mcp_handlers.middleware.identity_step import (
            populate_transport_binding_from_recovery,
        )
        import json

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
                # Strip the "transport_binding:" prefix to recover the cache key
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


async def identity_cache_warmup():
    """Pre-populate sticky transport identity cache from recent session bindings.

    Prevents first-request timeouts after server restart by warming the
    in-memory cache from Redis/PostgreSQL before any client connects.
    """
    await asyncio.sleep(2)  # Wait for DB and Redis to be ready
    try:
        from src.cache import is_redis_available
        if not is_redis_available():
            logger.info("[WARMUP] Redis unavailable, skipping identity cache warmup")
            return

        from src.cache.redis_client import get_redis
        redis = await get_redis()
        if not redis:
            logger.info("[WARMUP] Redis client not ready, skipping identity cache warmup")
            return

        from src.mcp_handlers.middleware.identity_step import update_transport_binding
        import json
        warmed = 0
        max_scan = 500  # Cap total keys scanned to avoid memory/time issues
        scanned = 0
        async for key in redis.scan_iter(match="session:*", count=50):
            scanned += 1
            if scanned > max_scan:
                logger.debug(f"[WARMUP] Scan cap reached ({max_scan} keys)")
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
                    # S3: warmup scans `session:*` Redis keys (session-cache
                    # entries), not the per-binding transport_binding:* keys
                    # that carry original_session_source. The original proof
                    # tier isn't recoverable from this path, so the warmed
                    # binding defaults to "unknown" — cache hits against it
                    # decay as weak until a fresh proof rebinds.
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
            logger.info(f"[WARMUP] Pre-populated identity cache with {warmed} binding(s)")
        else:
            logger.info("[WARMUP] No recent session bindings found to warm")
    except Exception as e:
        logger.warning(f"[WARMUP] Identity cache warmup failed (non-fatal): {e}")


def _supervised_create_task(coro, *, name: str | None = None) -> asyncio.Task:
    """Create a background task with crash logging."""
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(_on_background_task_done)
    _supervised_tasks.append(task)
    return task


# Public alias for use outside background_tasks (handlers, models, etc.)
create_tracked_task = _supervised_create_task


# ---------------------------------------------------------------------------
# Restartable named tasks
# ---------------------------------------------------------------------------
#
# Some background tasks (eisv_sync, etc) can hang on remote calls or DB pool
# acquisition. The dashboard's "unstick" button used to flip a status flag in
# Postgres without touching the asyncio Task itself — purely cosmetic. To make
# unstick actually unstick, we keep a registry of (name -> factory, current
# task) so the resume handler can cancel the wedged task and respawn a fresh
# one via the original factory.
#
# Only register tasks here that are safely re-entrant: each new spawn must
# start cleanly without depending on state left behind by the cancelled run.

from typing import Awaitable, Callable

_RESTARTABLE_TASK_FACTORIES: dict[str, Callable[[], Awaitable]] = {}
_RESTARTABLE_TASKS: dict[str, asyncio.Task] = {}


def _spawn_restartable_task(
    name: str, factory: Callable[[], Awaitable]
) -> asyncio.Task:
    """Spawn a supervised task and register it as restartable by ``name``.

    The factory is called with no arguments and must return a fresh coroutine
    each invocation (so the task can be respawned). Replaces any prior task
    registered under the same name.
    """
    task = _supervised_create_task(factory(), name=name)
    _RESTARTABLE_TASK_FACTORIES[name] = factory
    _RESTARTABLE_TASKS[name] = task
    return task


def cancel_and_respawn_task(name: str) -> dict:
    """Cancel the named restartable task and spawn a fresh one.

    Returns a small dict describing what happened so the caller can include
    it in operator-facing responses:
      - ``restarted: bool`` — whether a new task was actually spawned
      - ``previous_state``: "running" | "done" | "cancelled" | "unknown"
      - ``reason``: human-readable explanation when restarted is False

    This is the real "unstick" primitive — unlike a status-flag flip, this
    cancels the underlying asyncio Task and replaces it with a fresh one
    via the registered factory.
    """
    factory = _RESTARTABLE_TASK_FACTORIES.get(name)
    if factory is None:
        return {
            "restarted": False,
            "previous_state": "unknown",
            "reason": f"no restartable task registered under name '{name}'",
        }

    prior = _RESTARTABLE_TASKS.get(name)
    previous_state = "unknown"
    if prior is not None:
        if prior.done():
            previous_state = "cancelled" if prior.cancelled() else "done"
        else:
            previous_state = "running"
            prior.cancel()
            # Don't await cancellation — the cancel is scheduled on the loop.
            # Spawning the replacement immediately is intentional: we don't
            # want a window where the registry has no live task for this name.

    new_task = _supervised_create_task(factory(), name=name)
    _RESTARTABLE_TASKS[name] = new_task
    logger.info(
        f"[RESTART] Cancelled and respawned task '{name}' "
        f"(previous_state={previous_state})"
    )
    return {
        "restarted": True,
        "previous_state": previous_state,
        "reason": None,
    }


def list_restartable_tasks() -> list[str]:
    """Return the names of all currently registered restartable tasks."""
    return sorted(_RESTARTABLE_TASK_FACTORIES.keys())


async def stop_all_background_tasks() -> None:
    """Cancel and await all supervised background tasks before teardown."""
    tasks = [task for task in list(_supervised_tasks) if not task.done()]
    if not tasks:
        return

    logger.info(f"[SHUTDOWN] Cancelling {len(tasks)} background task(s)")
    for task in tasks:
        task.cancel()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for task, result in zip(tasks, results):
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            logger.debug(
                f"[SHUTDOWN] Background task '{task.get_name()}' exited with error during cancellation: {result}"
            )


def start_all_background_tasks(connection_tracker, set_ready):
    """
    Start all background tasks. Call once during server initialization.

    Args:
        connection_tracker: The ConnectionTracker instance
        set_ready: Callable that sets SERVER_READY = True
    """
    _supervised_create_task(connection_heartbeat_task(connection_tracker), name="heartbeat")
    logger.info("[HEARTBEAT] Connection health monitoring started")

    _supervised_create_task(startup_auto_calibration(), name="auto_calibration")
    # KG lifecycle temporarily disabled — AGE graph queries deadlock in anyio context
    # _supervised_create_task(startup_kg_lifecycle(), name="kg_lifecycle")
    logger.info("[KG_LIFECYCLE] Disabled — AGE query deadlock under investigation")
    _supervised_create_task(concept_extraction_background_task(), name="concept_extraction")
    _supervised_create_task(class_promotion_sweeper_task(), name="class_promotion_sweeper")
    logger.info("[CLASS_PROMOTION] Started ephemeral → engaged_ephemeral sweep (every 30m)")
    _supervised_create_task(
        lineage_eval_sweeper_task(),
        name="r2_lineage_eval_sweeper",
    )
    logger.info("[R2_SWEEPER] Started lineage-eval sweep (every 30m, 6h re-eval guard)")
    _supervised_create_task(periodic_matview_refresh(), name="matview_refresh")
    _supervised_create_task(periodic_partition_maintenance(), name="partition_maintenance")
    # Concurrent identity binding sweeper (#123): marks bindings stale once
    # they fall outside the live window so the diagnose view and v2
    # enforcement can distinguish stale from live.
    from src.mcp_handlers.identity.process_binding import (
        process_binding_sweeper_task,
    )
    _supervised_create_task(
        process_binding_sweeper_task(), name="process_binding_sweeper"
    )
    _supervised_create_task(background_metadata_load(), name="metadata_load")
    # periodic_orphan_cleanup removed 2026-04-19 — auto-sweep was hiding
    # onboarding bugs behind archival. Use the archive_orphan_agents MCP tool
    # manually (defaults to dry_run) if a sweep is actually wanted.
    _supervised_create_task(stuck_agent_recovery_task(), name="stuck_agent_recovery")
    _supervised_create_task(server_warmup_task(set_ready), name="server_warmup")
    _supervised_create_task(deep_health_probe_task(), name="deep_health_probe")
    logger.info("[HEALTH_PROBE] Deep health probe started (cached snapshots for health_check handler)")

    _supervised_create_task(progress_flat_probe_task(), name="progress_flat_probe")
    logger.info("[PROGRESS_FLAT] Resident-progress flat probe started")

    _supervised_create_task(session_cleanup_task(interval_hours=6.0), name="session_cleanup")
    logger.info("[SESSION_CLEANUP] Started periodic expired session cleanup (every 6h)")

    _supervised_create_task(coherence_monitoring_task(interval_minutes=10.0), name="coherence_monitor")
    logger.info("[COHERENCE_MONITOR] Started proactive coherence monitoring (every 10m)")

    _supervised_create_task(periodic_telemetry_rotation(), name="telemetry_rotation")
    _supervised_create_task(periodic_audit_log_rotation(), name="audit_log_rotation")
    _supervised_create_task(periodic_tool_usage_rotation(), name="tool_usage_rotation")
    _supervised_create_task(periodic_server_log_rotation(), name="server_log_rotation")
    logger.info("[ROTATION] Started periodic log/telemetry rotation tasks")

    _supervised_create_task(check_agent_silence(), name="silence_detection")
    logger.info("[SILENCE] Started agent silence detection (every 10m)")

    _supervised_create_task(identity_cache_warmup(), name="identity_cache_warmup")
    _supervised_create_task(transport_binding_cache_warmup(), name="transport_binding_cache_warmup")
