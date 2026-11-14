"""
Background tasks for the governance MCP server.

Extracted from mcp_server.py to reduce file size and improve maintainability.
Each task runs as an asyncio coroutine, started during server initialization.
"""

import asyncio
import gzip
import shutil
from datetime import datetime, timezone  # noqa: F401
from pathlib import Path
from typing import Optional  # noqa: F401

from src.background_maintenance_tasks import (  # noqa: F401
    _PERF_PERSIST_TARGETS,
    _SESSION_CLEANUP_REDIS_TIMEOUT_S,
    _lineage_eval_sweep_once,
    _session_cleanup_pass,
    class_promotion_sweeper_task,
    identity_cache_warmup,
    lineage_eval_sweeper_task,
    perf_monitor_persist_task,
    principal_rollup_sweeper_task,
    restore_host_cooldowns,
    session_cleanup_task,
    substrate_claims_refresh,
    transport_binding_cache_warmup,
)
from src.background_resident_monitors import (  # noqa: F401
    CADENCE_FROM_TAG,
    RESIDENT_PAUSE_REALERT_SECONDS,
    _PERSISTENT_AGENT_INTERVALS,
    _SILENCE_ANY_ALERT_EVENTS,
    _SILENCE_CRITICAL_ALERT_EVENTS,
    _SILENCE_PROXY_AGENTS,
    _canonical_active_resident_ids,
    _close_resident_silence_findings,
    _get_expected_interval,
    _log_resident_silence_finding,
    _parse_last_update_aware,
    _proxy_alive,
    _record_silence_close,
    _resident_pause_alerted,
    _resident_pause_check_iteration,
    _safe_total_updates,
    _silence_alerted,
    _silence_check_iteration,
    _silence_close_checked,
    _silence_close_pending,
    _silence_critical_alerted,
    _silence_duplicate_warned,
    _silence_episode_already_alerted,
    _silence_server_start,
    cadence_from_tags,
    check_agent_silence,
    progress_flat_probe_task,
)
from src.background_task_health import record_failure, record_success
from src.logging_utils import get_logger

logger = get_logger(__name__)


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
            # WARNING, not debug: a failure here used to be invisible, and the
            # task produced zero Concept vertices for months without a trace.
            logger.warning(
                f"[CONCEPT_EXTRACTION] Run failed: {type(e).__name__}: {e}",
                exc_info=True,
            )
        try:
            await asyncio.sleep(interval_hours * 3600)
        except asyncio.CancelledError:
            break


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
            record_success("partition_maintenance", log=logger)
        except Exception as e:
            record_failure("partition_maintenance", e, log=logger)
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
# the ``archive_orphan_agents`` MCP tool, which defaults to preview-only.
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

            # In-process trusted caller: bypasses the MCP handler's operator
            # gate on auto_recover, which a background task (no request,
            # no X-Unitares-Operator header) could never satisfy.
            from src.mcp_handlers.lifecycle.stuck import run_stuck_agent_sweep

            # Preserve the MCP decorator's former 15-second bound even though
            # this trusted path no longer goes through the request handler.
            result = await asyncio.wait_for(run_stuck_agent_sweep({
                "max_age_minutes": 30.0,
                "critical_margin_timeout_minutes": 5.0,
                "tight_margin_timeout_minutes": 15.0,
                "min_updates": 1,
                "note_cooldown_minutes": 120.0
            }, auto_recover=True), timeout=15.0)

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
# Dialectic stuck-session sweep
# ---------------------------------------------------------------------------

# Upper bound on one periodic dialectic sweep cycle, in seconds. A normal cycle
# takes milliseconds (pilot, 2026-08-29..09-27: periodic p50 6 ms, p99 31 ms,
# n=3,596), so the bound only ever fires on a hung cycle. Before it existed the
# loop awaited each cycle with no timeout, so a hung cycle emitted nothing and
# stopped every later cycle too; its gap was indistinguishable from a lost audit
# write (Wave 3 gate council 2026-09-27, finding B5). A timed-out cycle is
# cancelled and still emits its `dialectic_sweep_cycle` row with
# error="timeout". Overridable for an operator who needs a different bound;
# a non-positive or unparseable value falls back to the default.
DIALECTIC_SWEEP_CYCLE_TIMEOUT_ENV = "UNITARES_DIALECTIC_SWEEP_CYCLE_TIMEOUT_S"
DIALECTIC_SWEEP_CYCLE_TIMEOUT_DEFAULT_S = 300.0


def _dialectic_sweep_cycle_timeout_s() -> float:
    import os

    raw = os.environ.get(DIALECTIC_SWEEP_CYCLE_TIMEOUT_ENV, "").strip()
    if not raw:
        return DIALECTIC_SWEEP_CYCLE_TIMEOUT_DEFAULT_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[DIALECTIC_SWEEP] %s=%r is not a number; using %.0fs",
            DIALECTIC_SWEEP_CYCLE_TIMEOUT_ENV, raw, DIALECTIC_SWEEP_CYCLE_TIMEOUT_DEFAULT_S,
        )
        return DIALECTIC_SWEEP_CYCLE_TIMEOUT_DEFAULT_S
    if value <= 0:
        return DIALECTIC_SWEEP_CYCLE_TIMEOUT_DEFAULT_S
    return value


async def _run_dialectic_auto_resolve_cycle() -> dict[str, int]:
    """One sweep of stuck dialectic sessions, returning a flattened summary.

    Extracted from ``dialectic_auto_resolve_sweeper_task`` so the cycle is
    unit-testable (mirrors the ``lineage_eval_sweeper`` extraction). Note the
    auto-resolve return key ``resolved_count`` actually counts sessions marked
    FAILED — the mapping here pins that naming so callers read ``failed``.

    The cycle is bounded by ``_dialectic_sweep_cycle_timeout_s()``. The bound
    is applied inside ``auto_resolve_stuck_sessions`` rather than by wrapping
    this await, because only there can the cancellation be told apart from a
    shutdown and reported as ``error="timeout"`` on the cycle row.
    """
    from src.mcp_handlers.dialectic.auto_resolve import auto_resolve_stuck_sessions
    result = await auto_resolve_stuck_sessions(
        trigger_source="periodic",
        timeout_s=_dialectic_sweep_cycle_timeout_s(),
    )
    return {
        "failed": int(result.get("resolved_count", 0) or 0),
        "reassigned": int(result.get("reassigned_count", 0) or 0),
        "facilitation": int(result.get("facilitation_count", 0) or 0),
        "skipped": int(result.get("skipped_count", 0) or 0),
    }


async def dialectic_auto_resolve_sweeper_task(interval_minutes: float = 10.0):
    """Periodically sweep stuck dialectic sessions.

    ``auto_resolve_stuck_sessions()`` was previously only invoked lazily — from
    ``is_agent_in_active_session()``, which only runs when an agent queries
    active sessions or requests a review. A dialectic session that went stale
    with no further dialectic traffic was therefore never swept: session
    ``e8417ad0`` sat in ``antithesis`` for 3 days. This task gives the sweep a
    real timer, so stuck sessions are reaped within ~interval of crossing the
    inactivity threshold (``auto_resolve.STUCK_SESSION_THRESHOLD`` = 2h),
    regardless of whether any agent happens to hit the lazy path.

    Mirrors ``class_promotion_sweeper_task`` / ``stuck_agent_recovery_task``:
    runs outside the request path, 90s startup delay to let the metadata cache
    and pools warm, errors WARNING-logged so a silent gap is operator-visible.
    The sweep itself is failure-isolated (``auto_resolve_stuck_sessions``
    returns an error dict rather than raising).
    """
    await asyncio.sleep(90.0)
    interval_seconds = interval_minutes * 60
    logger.info(
        f"[DIALECTIC_SWEEP] Started stuck-session sweep (every {interval_minutes}m)"
    )
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            summary = await _run_dialectic_auto_resolve_cycle()
            # `skipped` counts guarded writes the database refused -- the
            # direct signal that another writer had already finished the
            # session. It was absent from both the condition and the message,
            # so a sweep that refused EVERY write logged nothing at all.
            if any(summary.values()):
                logger.info(
                    f"[DIALECTIC_SWEEP] {summary['reassigned']} reassigned, "
                    f"{summary['facilitation']} awaiting facilitation, "
                    f"{summary['failed']} failed, "
                    f"{summary['skipped']} skipped (write refused)"
                )
        except asyncio.CancelledError:
            logger.info("[DIALECTIC_SWEEP] Task cancelled")
            break
        except Exception as e:
            logger.warning(f"[DIALECTIC_SWEEP] Error in sweep: {e}", exc_info=True)
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
        failed: list[str] = []
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
                # A real copy or truncate failure: the log keeps growing past
                # max_size_mb until someone notices, so it is not a debug event.
                failed.append(f"{log_name}: {type(e).__name__}: {e}")

        if failed:
            record_failure("server_log_rotation", "; ".join(failed), log=logger)
        else:
            record_success("server_log_rotation", log=logger)
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

# Set True at the top of stop_all_background_tasks() before the cancel loop.
# Graceful shutdown cancels every supervised task at once (a sub-5ms fanout
# burst); those are benign lifecycle, NOT substrate-coupling incidents, so the
# cancellation emitter suppresses them when this is set. Runtime-anomalous
# cancellations (server up — e.g. cancel_and_respawn_task) still emit, which is
# the genuine §129 substrate-tax signal. One-way latch: never reset (once the
# graceful-shutdown sequence starts, the process is going down).
_background_tasks_shutting_down: bool = False


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
    if _background_tasks_shutting_down:
        # Graceful-shutdown teardown cancels all supervised tasks at once; these
        # are expected lifecycle, not coordination failures. Emitting them
        # pollutes the §129 substrate-tax incident count with restart noise (a
        # nesting+noise audit on 2026-06-03 found 69 such rows = 8 restart
        # bursts). Suppress; runtime-anomalous cancellations (flag still False)
        # still emit and remain the real signal.
        logger.debug(
            "[coord-events] suppressing graceful-shutdown cancellation for task %r", task_name
        )
        return
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
    # Latch the shutdown flag BEFORE the cancel loop so the done-callbacks (which
    # fire during the gather await below) see it and suppress their cancellation
    # emits — otherwise every graceful restart writes ~N benign coordination-
    # failure rows that poison the §129 substrate-tax measurement.
    global _background_tasks_shutting_down
    _background_tasks_shutting_down = True
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


def start_all_background_tasks(set_ready):
    """
    Start all background tasks. Call once during server initialization.

    Args:
        set_ready: Callable that sets SERVER_READY = True
    """
    _supervised_create_task(startup_auto_calibration(), name="auto_calibration")
    from src.calibration_delivery import calibration_delivery_task
    _supervised_create_task(calibration_delivery_task(), name="calibration_delivery")
    # KG lifecycle temporarily disabled — AGE graph queries deadlock in anyio context
    # _supervised_create_task(startup_kg_lifecycle(), name="kg_lifecycle")
    logger.info("[KG_LIFECYCLE] Disabled — AGE query deadlock under investigation")
    _supervised_create_task(concept_extraction_background_task(), name="concept_extraction")
    # Topic rollups fed by store-time consolidation hints; shadow by default
    # (UNITARES_KG_SYNTHESIS_SCHEDULE, see src/kg_synthesis_schedule.py).
    from src.kg_synthesis_schedule import kg_synthesis_background_task
    _supervised_create_task(kg_synthesis_background_task(), name="kg_synthesis")
    _supervised_create_task(class_promotion_sweeper_task(), name="class_promotion_sweeper")
    logger.info("[CLASS_PROMOTION] Started ephemeral → engaged_ephemeral sweep (every 30m)")
    _supervised_create_task(principal_rollup_sweeper_task(), name="principal_rollup_sweeper")
    logger.info("[PRINCIPAL_ROLLUP] Started derived octopus rollup recompute (every 60s)")
    _supervised_create_task(
        lineage_eval_sweeper_task(),
        name="r2_lineage_eval_sweeper",
    )
    logger.info("[R2_SWEEPER] Started lineage-eval sweep (every 30m, 6h re-eval guard)")
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
    # manually (defaults to preview-only) if a sweep is actually wanted.
    _supervised_create_task(stuck_agent_recovery_task(), name="stuck_agent_recovery")
    _supervised_create_task(
        dialectic_auto_resolve_sweeper_task(), name="dialectic_auto_resolve_sweeper"
    )
    logger.info("[DIALECTIC_SWEEP] Started stuck dialectic-session sweep (every 10m)")
    _supervised_create_task(server_warmup_task(set_ready), name="server_warmup")
    _supervised_create_task(restore_host_cooldowns(), name="host_cooldown_restore")
    _supervised_create_task(deep_health_probe_task(), name="deep_health_probe")
    logger.info("[HEALTH_PROBE] Deep health probe started (cached snapshots for health_check handler)")

    _supervised_create_task(progress_flat_probe_task(), name="progress_flat_probe")
    logger.info("[PROGRESS_FLAT] Resident-progress flat probe started")

    _supervised_create_task(session_cleanup_task(interval_hours=6.0), name="session_cleanup")
    logger.info("[SESSION_CLEANUP] Started periodic expired session cleanup (every 6h)")

    _supervised_create_task(perf_monitor_persist_task(interval_minutes=5.0), name="perf_monitor_persist")
    logger.info("[PERF_PERSIST] Started perf_monitor snapshot persistence (every 5m)")

    # Wave 3 §3.2 cutover 503-rate halt aggregator (§14 prereq PR #10).
    # Inert unless WAVE3_CUTOVER_503_AGGREGATOR is set; the task logs and
    # returns immediately when the flag is off.
    from src.mcp_transport import cutover_503_aggregator_task
    _supervised_create_task(cutover_503_aggregator_task(), name="cutover_503_aggregator")

    _supervised_create_task(periodic_telemetry_rotation(), name="telemetry_rotation")
    _supervised_create_task(periodic_audit_log_rotation(), name="audit_log_rotation")
    _supervised_create_task(periodic_tool_usage_rotation(), name="tool_usage_rotation")
    _supervised_create_task(periodic_server_log_rotation(), name="server_log_rotation")
    logger.info("[ROTATION] Started periodic log/telemetry rotation tasks")

    _supervised_create_task(check_agent_silence(), name="silence_detection")
    logger.info("[SILENCE] Started agent silence detection (every 10m)")

    _supervised_create_task(identity_cache_warmup(), name="identity_cache_warmup")
    _supervised_create_task(substrate_claims_refresh(), name="substrate_claims_refresh")
    _supervised_create_task(transport_binding_cache_warmup(), name="transport_binding_cache_warmup")
