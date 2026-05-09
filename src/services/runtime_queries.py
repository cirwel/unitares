"""Transport-neutral read/query services for core governance state."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from numbers import Real
from typing import Any, Dict

from config.governance_config import GovernanceConfig
from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
from src.services.identity_continuity import get_identity_continuity_status

logger = get_logger(__name__)


def _resolve_agent_identity_view(agent_uuid: str, meta: Any) -> tuple[str, str | None]:
    """Resolve the public-facing handle and display name for a UUID-bound agent."""
    public_agent_id = (
        getattr(meta, "public_agent_id", None)
        or getattr(meta, "structured_id", None)
        or agent_uuid
    ) if meta else agent_uuid
    display_name = (
        getattr(meta, "label", None)
        or getattr(meta, "display_name", None)
    ) if meta else None
    return public_agent_id, display_name


def _build_eisv_semantics(metrics: Dict[str, Any], monitor: Any) -> Dict[str, Any]:
    """Attach explicit primary/behavioral/ODE EISV views for read APIs."""
    primary_eisv = {
        "E": metrics.get("E"),
        "I": metrics.get("I"),
        "S": metrics.get("S"),
        "V": metrics.get("V"),
    }

    ode_eisv = dict(metrics.get("ode") or {})
    if not ode_eisv:
        ode_eisv = {
            "E": metrics.get("E"),
            "I": metrics.get("I"),
            "S": metrics.get("S"),
            "V": metrics.get("V"),
        }

    behavioral_eisv = None
    behavioral_confidence = 0.0
    try:
        beh = getattr(monitor, "_behavioral_state", None)
        confidence_value = getattr(beh, "confidence", 0.0) if beh is not None else 0.0
        if beh is not None and isinstance(confidence_value, Real):
            behavioral_confidence = float(confidence_value or 0.0)
            warmup = None
            try:
                beh_dict = beh.to_dict()
                warmup = beh_dict.get("warmup")
            except Exception:
                pass
            behavioral_eisv = {
                "E": float(getattr(beh, "E")),
                "I": float(getattr(beh, "I")),
                "S": float(getattr(beh, "S")),
                "V": float(getattr(beh, "V")),
                "confidence": behavioral_confidence,
                "warmup": warmup,
            }
    except Exception:
        behavioral_eisv = None
        behavioral_confidence = 0.0

    primary_source = "behavioral" if behavioral_confidence >= 0.3 else "ode_fallback"
    ode_diagnostics = {
        "phi": metrics.get("phi"),
        "verdict": metrics.get("verdict"),
        "coherence": metrics.get("coherence"),
        "regime": metrics.get("regime"),
        "lambda1": metrics.get("lambda1"),
        "status": metrics.get("status"),
    }

    return {
        "eisv": primary_eisv,
        "primary_eisv": primary_eisv,
        "primary_eisv_source": primary_source,
        "behavioral_eisv": behavioral_eisv,
        "ode_eisv": ode_eisv,
        "ode_diagnostics": ode_diagnostics,
        "state_semantics": {
            "flat_fields_mean": "primary_eisv",
            "primary_eisv_role": (
                "Live state to read first. Source is behavioral when confidence >= 0.3, "
                "ODE otherwise. Check primary_eisv_source for which is active."
            ),
            "behavioral_eisv_role": (
                "PRIMARY for verdicts. Observation-first EMA of actual agent behavior. "
                "Determines proceed/guide/pause/reject decisions."
            ),
            "ode_eisv_role": (
                "DIAGNOSTIC only. ODE dynamics with universal attractor. "
                "Used for convergence tracking and phi calculation. NOT used for verdicts."
            ),
            "ode_diagnostics_role": "Thermostat dynamics diagnostics (phi, regime, lambda1)",
            "verdict_source": primary_source,
            "hierarchy": [
                "1. behavioral_eisv (primary when confidence >= 0.3)",
                "2. ode_eisv (fallback when behavioral data insufficient)",
            ],
        },
    }


def _generate_contextual_reflection(metrics: dict, interpreted: dict) -> str | None:
    """Generate a reflection prompt only when state warrants attention."""
    is_uninit = metrics.get('initialized') is False or metrics.get('status') == 'uninitialized'
    if is_uninit:
        return "First check-in — submit a process_agent_update to activate governance."

    verdict = metrics.get('verdict', 'proceed')
    if verdict in ('guide', 'pause', 'reject'):
        return f"Your state triggered a {verdict} verdict. What changed?"

    state = interpreted.get('state', {})
    borderline = state.get('borderline')
    if borderline:
        return "You're near a basin boundary. Proceed carefully."

    S = metrics.get('S')
    if S is not None and S > 0.3:
        return f"Entropy is elevated ({S:.2f}). What's contributing to disorder?"

    return None


async def get_governance_metrics_data(agent_id: str, arguments: Dict[str, Any], server=None) -> Dict[str, Any]:
    """Build plain governance metrics data for an agent."""
    server = server or mcp_server
    verbosity = arguments.get("verbosity")
    if verbosity and verbosity in ("minimal", "standard", "full"):
        lite = verbosity == "minimal"
    else:
        lite = arguments.get("lite", True)
        verbosity = "minimal" if lite else "full"

    monitor = server.get_or_create_monitor(agent_id)
    # Heal the DB ↔ file persistence split: if the on-disk state file was
    # dropped (anyio executor stall, crash mid-write, etc.) but core.agent_state
    # has history, the monitor would otherwise report "uninitialized" forever.
    from src.agent_monitor_state import hydrate_from_db_if_fresh
    await hydrate_from_db_if_fresh(monitor, agent_id)
    include_state = arguments.get("include_state", False)
    metrics = monitor.get_metrics(include_state=include_state)

    from src.governance_monitor import UNITARESMonitor
    from src.mcp_handlers.utils import format_metrics_report, get_calibration_feedback

    metrics["eisv_labels"] = UNITARESMonitor.get_eisv_labels()
    standardized_metrics = format_metrics_report(
        metrics=metrics,
        agent_id=agent_id,
        include_timestamp=True,
        include_context=True,
    )
    try:
        from src.mcp_handlers.context import get_session_resolution_source
        standardized_metrics["session_continuity"] = {
            "resolution_source": get_session_resolution_source(),
        }
    except Exception:
        pass

    meta = server.agent_metadata.get(agent_id)
    public_agent_id, display_name = _resolve_agent_identity_view(agent_id, meta)
    # display_name (user-chosen) takes precedence over agent_id (auto-generated)
    standardized_metrics["agent_id"] = display_name or public_agent_id
    if public_agent_id != agent_id:
        standardized_metrics["agent_uuid"] = agent_id
    if display_name and public_agent_id != display_name:
        standardized_metrics["structured_agent_id"] = public_agent_id
    if display_name:
        standardized_metrics["display_name"] = display_name
    standardized_metrics.update(_build_eisv_semantics(metrics, monitor))

    if meta and getattr(meta, "purpose", None):
        standardized_metrics["purpose"] = meta.purpose

    calibration_feedback = {}
    try:
        if meta:
            derived_complexity = metrics.get("complexity", None)
            if derived_complexity is not None:
                calibration_feedback["complexity"] = {
                    "derived": derived_complexity,
                    "message": f"System-derived complexity: {derived_complexity:.2f} (based on current state)",
                }
    except Exception as e:
        logger.debug(f"Could not add complexity calibration feedback: {e}")

    confidence_feedback = get_calibration_feedback(include_complexity=False)
    if confidence_feedback:
        calibration_feedback.update(confidence_feedback)
    if calibration_feedback:
        standardized_metrics["calibration_feedback"] = calibration_feedback

    try:
        risk_score = metrics.get("risk_score") or metrics.get("latest_risk_score")
        interpreted_state = monitor.state.interpret_state(risk_score=risk_score)
        standardized_metrics["state"] = interpreted_state
        health = interpreted_state.get("health", "unknown")
        mode = interpreted_state.get("mode", "unknown")
        basin = interpreted_state.get("basin", "unknown")
        standardized_metrics["summary"] = f"{health} | {mode} | {basin} basin"
    except Exception as e:
        logger.debug(f"Could not generate state interpretation: {e}")

    try:
        from governance_core import compute_saturation_diagnostics
        from governance_core.parameters import DEFAULT_THETA

        unitares_state = monitor.state.unitaires_state
        theta = getattr(monitor.state, "unitaires_theta", None) or DEFAULT_THETA

        if unitares_state:
            sat_diag = compute_saturation_diagnostics(unitares_state, theta)
            standardized_metrics["saturation_diagnostics"] = {
                "sat_margin": sat_diag["sat_margin"],
                "dynamics_mode": sat_diag["dynamics_mode"],
                "will_saturate": sat_diag["will_saturate"],
                "at_boundary": sat_diag["at_boundary"],
                "I_equilibrium": sat_diag["I_equilibrium_linear"],
                "forcing_term_A": sat_diag["A"],
                "_interpretation": (
                    "⚠️ Positive sat_margin means push-to-boundary (logistic mode will saturate I→1)"
                    if sat_diag["sat_margin"] > 0
                    else "✓ Negative sat_margin - stable interior equilibrium exists"
                ),
            }
    except Exception as e:
        logger.debug(f"Could not compute saturation diagnostics: {e}")

    reflection = _generate_contextual_reflection(metrics, standardized_metrics)
    if reflection:
        standardized_metrics["reflection"] = reflection

    if verbosity == "standard":
        state = standardized_metrics.get("state", {})
        standard_metrics = {
            "agent_id": display_name or public_agent_id,
            "display_name": display_name,
        }
        if display_name and public_agent_id != display_name:
            standard_metrics["structured_agent_id"] = public_agent_id
        standard_metrics.update({
            "E": metrics.get("E"),
            "I": metrics.get("I"),
            "S": metrics.get("S"),
            "V": metrics.get("V"),
            "coherence": metrics.get("coherence"),
            "verdict": metrics.get("verdict", "uninitialized"),
            "risk_score": metrics.get("risk_score"),
            "primary_eisv_source": standardized_metrics.get("primary_eisv_source"),
            "basin": state.get("basin"),
            "mode": state.get("mode"),
            "summary": standardized_metrics.get("summary"),
            "guidance": state.get("guidance"),
        })
        if public_agent_id != agent_id:
            standard_metrics["agent_uuid"] = agent_id
        if reflection:
            standard_metrics["reflection"] = reflection
        standard_metrics["_note"] = "Use verbosity='full' for diagnostics, 'minimal' for quick check"
        return standard_metrics

    standardized_metrics["_debug_lite_received"] = lite

    if lite:
        coherence = metrics.get("coherence")
        risk_score = metrics.get("risk_score")
        health = standardized_metrics.get("state", {}).get("health", "unknown")
        status_indicator = {
            "healthy": "🟢",
            "moderate": "🟡",
            "critical": "🔴",
            "unknown": "⚪",
        }.get(health, "⚪")
        is_uninitialized = metrics.get("initialized") is False or metrics.get("status") == "uninitialized"

        if is_uninitialized:
            status_display = "⚪ uninitialized"
            coherence_status = "⚪ pending (first check-in required)"
            risk_status = "⚪ pending (first check-in required)"
        else:
            status_display = f"{status_indicator} {health}"
            if coherence is None:
                coherence_status = "⚪ unknown"
            elif coherence >= 0.50:
                coherence_status = "🟢 good"
            elif coherence >= 0.45:
                coherence_status = "🟡 moderate"
            else:
                coherence_status = "🔴 low"
            risk_status = (
                "🟢 low" if risk_score is not None and risk_score < 0.5 else
                "🟡 medium" if risk_score is not None and risk_score < 0.75 else
                "🔴 high" if risk_score is not None else
                "⚪ unknown"
            )

        void_raw = metrics.get("V")
        if void_raw is not None and void_raw != 0:
            void_display = round(void_raw, 6)
        else:
            void_display = 0.0 if void_raw == 0 else void_raw

        lite_metrics = {
            "agent_id": display_name or public_agent_id,
            "display_name": display_name,
        }
        if display_name and public_agent_id != display_name:
            lite_metrics["structured_agent_id"] = public_agent_id
        lite_metrics.update({
            "status": status_display,
            "purpose": getattr(meta, "purpose", None),
            "summary": standardized_metrics.get("summary", "unknown"),
            "primary_eisv_source": standardized_metrics.get("primary_eisv_source"),
            "E": {"value": metrics.get("E"), "range": "[0, 1]", "note": "Energy capacity"},
            "I": {"value": metrics.get("I"), "range": "[0, 1]", "note": "Information integrity"},
            "S": {"value": metrics.get("S"), "range": "[0, 1]", "ideal": "<0.2", "note": "Entropy (lower=better)"},
            "V": {"value": void_display, "range": "[-1, 1]", "ideal": "near 0", "note": "Void (E-I imbalance, settles toward 0)"},
            "coherence": {"value": coherence, "range": "[0, 1]", "status": coherence_status},
            "risk_score": {"value": risk_score, "threshold": 0.5, "status": risk_status},
        })
        if public_agent_id != agent_id:
            lite_metrics["agent_uuid"] = agent_id
        # Wrap mode / basin with glossary entries (#428). Bare values are
        # opaque to a cold agent; the explain_* helpers attach `meaning`
        # at point-of-use.
        from src.governance_glossary import (
            explain_basin,
            explain_mode,
            explain_verdict,
        )
        if "state" in standardized_metrics:
            lite_metrics["mode"] = explain_mode(standardized_metrics["state"].get("mode"))
            lite_metrics["basin"] = explain_basin(standardized_metrics["state"].get("basin"))
        if is_uninitialized:
            lite_metrics["verdict"] = explain_verdict("uninitialized")
            lite_metrics["guidance"] = "Submit one check-in to activate governance."
            lite_metrics["next_action"] = {
                "tool": "process_agent_update",
                "example": "process_agent_update(response_text='Starting work', complexity=0.3, confidence=0.7)",
                "note": "get_governance_metrics is read-only; it does not initialize state.",
            }
            lite_metrics["related_tools"] = ["process_agent_update", "onboard", "identity"]
        lite_metrics["thresholds"] = {
            "coherence_critical": GovernanceConfig.COHERENCE_CRITICAL_THRESHOLD,
            "coherence_good": 0.50,
            "risk_medium": 0.5,
            "risk_high": 0.75,
            "target_coherence": GovernanceConfig.TARGET_COHERENCE,
        }
        lite_metrics["_note"] = "Use lite=false for full diagnostics"
        return lite_metrics

    # Circuit breaker telemetry (full verbosity only)
    try:
        from src.agent_loop_detection import get_circuit_breaker_telemetry
        from src.cache.redis_client import get_circuit_breaker as get_redis_cb
        gov_telemetry = get_circuit_breaker_telemetry()
        redis_telemetry = get_redis_cb().get_telemetry()
        standardized_metrics["circuit_breakers"] = {
            "governance": gov_telemetry,
            "redis": redis_telemetry,
        }
    except Exception as e:
        logger.debug(f"Could not gather circuit breaker telemetry: {e}")

    return standardized_metrics


# Per-prefix scan ceiling for the `redis_cache.keys` health-check section.
# At 10000 keys per prefix and a typical SCAN cost of <0.1s the probe stays
# well inside the 15s budget; values past the cap are reported as "{cap}+".
_HEALTH_KEYS_CAP = 10000
_HEALTH_KEYS_SCAN_PAGE = 500


async def _bounded_scan_count(redis, pattern: str, *, cap: int = _HEALTH_KEYS_CAP, page: int = _HEALTH_KEYS_SCAN_PAGE):
    """Count keys matching `pattern` without materializing the cursor.

    Returns the integer count, or `f"{cap}+"` once the ceiling is hit.
    Caller should run multiple invocations under `asyncio.gather` for parallel fan-out.
    """
    n = 0
    async for _ in redis.scan_iter(match=pattern, count=page):
        n += 1
        if n >= cap:
            return f"{cap}+"
    return n


_LEASE_PLANE_PROBE_TIMEOUT_S = 2.0


async def _probe_lease_plane_boundary(loop) -> Dict[str, Any]:
    """Wave 2 Phase C.5: probe BEAM lease-plane /v1/health for the deep-health
    snapshot. Returns a `checks["lease_plane"]`-shaped dict.

    Failure-safe by composition: LeasePlaneClient.health_check() (Phase C,
    PR #417) never raises — it returns a typed HealthOk | HealthUnavailable.
    Wrapped here in a final try/except so even an import failure or config
    misread surfaces as `status: "error"` rather than crashing the snapshot.

    Uses `loop.run_in_executor` because the client is sync stdlib urllib —
    keeps the probe off the anyio task group entirely. Tight 2s timeout
    matches the snapshot's "fast liveness" purpose; full lease ops use the
    config-default 5s.
    """
    import os

    try:
        from src.lease_plane import (
            HealthOk,
            LeasePlaneClient,
            LeasePlaneClientConfig,
        )

        base_url = os.getenv("LEASE_PLANE_BASE_URL", "http://127.0.0.1:8788")
        bearer = os.getenv("LEASE_PLANE_BEARER_TOKEN", "") or ""

        # No bearer configured → this deploy doesn't have the lease-plane
        # boundary in scope. Return "unavailable" (mirrors the redis_cache
        # pattern when Redis isn't configured) so the snapshot's overall
        # status logic can pop it via the line-705 special-case rather than
        # degrading every test/CI run that doesn't have the bearer.
        if not bearer:
            return {
                "status": "unavailable",
                "ok": False,
                "url": base_url,
                "reason": "LEASE_PLANE_BEARER_TOKEN not configured",
            }

        config = LeasePlaneClientConfig(
            base_url=base_url,
            bearer_token=bearer,
            timeout_s=_LEASE_PLANE_PROBE_TIMEOUT_S,
        )
        client = LeasePlaneClient(config)
        health = await loop.run_in_executor(
            None,
            lambda: client.health_check(timeout_s=_LEASE_PLANE_PROBE_TIMEOUT_S),
        )
        if isinstance(health, HealthOk):
            return {"status": "healthy", "ok": True, "url": base_url}
        # HealthUnavailable: boundary did not confirm. Snapshot status is
        # "warning" (not "error") because the deep-health probe shouldn't
        # fail the whole snapshot just because a downstream is unhealthy —
        # the operator wants the rest of the surface visible.
        return {
            "status": "warning",
            "ok": False,
            "url": base_url,
            "reason": health.reason,
        }
    except Exception as e:  # noqa: BLE001 — probe must not poison snapshot
        return {"status": "error", "error": str(e)}


async def get_health_check_data(arguments: Dict[str, Any], server=None) -> Dict[str, Any]:
    """Build plain health-check data for operators and transports."""
    server = server or mcp_server
    import asyncio
    import os
    import time as _time

    from src.audit_log import audit_logger
    from src.calibration import calibration_checker
    from src.db import get_db

    checks = {}
    loop = asyncio.get_running_loop()
    continuity_status = None

    # Detect if we're in an MCP handler context (anyio task group).
    # Expensive checks (KG, Pi) block indefinitely due to nested anyio/connection pool issues.
    _in_mcp = False
    try:
        from src.mcp_handlers.context import get_session_signals
        _signals = get_session_signals()
        _in_mcp = _signals is not None and getattr(_signals, 'transport', None) == 'mcp'
    except Exception:
        pass

    try:
        pending = await loop.run_in_executor(None, lambda: calibration_checker.get_pending_updates())
        checks["calibration"] = {"status": "healthy", "pending_updates": pending}
    except Exception as e:
        checks["calibration"] = {"status": "error", "error": str(e)}


    try:
        from src.calibration_db import calibration_health_check_async
        info = await calibration_health_check_async()
        checks["calibration_db"] = {"status": "healthy", "backend": info.get("backend", "unknown"), "info": info}
    except Exception as e:
        checks["calibration_db"] = {"status": "error", "error": str(e)}


    try:
        log_exists = await loop.run_in_executor(None, lambda: audit_logger.log_file.exists())
        checks["telemetry"] = {"status": "healthy" if log_exists else "warning", "audit_log_exists": log_exists}
    except Exception as e:
        checks["telemetry"] = {"status": "error", "error": str(e)}


    try:
        configured = os.getenv("DB_BACKEND", "postgres").lower()
        db = get_db()
        backend_class = type(db).__name__
        init_error = None
        try:
            await db.init()
        except Exception as e:
            init_error = str(e)

        try:
            db_info = await db.health_check()
        except Exception as e:
            db_info = {"status": "error", "error": str(e)}

        db_status = db_info.get("status") if isinstance(db_info, dict) else None
        checks["primary_db"] = {
            "status": "healthy" if db_status == "healthy" else ("error" if db_status == "error" else "warning"),
            "configured_backend": configured,
            "backend_class": backend_class,
            "init_error": init_error,
            "info": db_info,
        }
    except Exception as e:
        checks["primary_db"] = {"status": "error", "error": str(e)}


    try:
        from src.audit_db import audit_health_check_async
        info = await audit_health_check_async()
        checks["audit_db"] = {"status": "healthy", "backend": info.get("backend", "unknown"), "info": info}
    except Exception as e:
        checks["audit_db"] = {"status": "error", "error": str(e)}

    try:
        from src.cache import get_distributed_lock, get_redis, get_session_cache, is_redis_available
        redis_available = is_redis_available()
        if redis_available:
            session_cache = get_session_cache()
            dist_lock = get_distributed_lock()
            cache_health = await session_cache.health_check()
            lock_health = await dist_lock.health_check()
            checks["redis_cache"] = {
                "status": cache_health.get("status", "unknown"),
                "present": True,
                "session_cache": cache_health,
                "distributed_lock": lock_health,
                "features": ["session_cache", "distributed_locking", "rate_limiting", "metadata_cache"],
            }
            try:
                redis = await get_redis()
                if redis:
                    info = await redis.info("stats")
                    hits = info.get("keyspace_hits", 0)
                    misses = info.get("keyspace_misses", 0)
                    total_lookups = hits + misses
                    checks["redis_cache"]["stats"] = {
                        "keyspace_hits": hits,
                        "keyspace_misses": misses,
                        "keyspace_hit_rate_percent": round((hits / total_lookups) * 100, 1) if total_lookups else None,
                        "total_commands": info.get("total_commands_processed", 0),
                        "scope": "redis_instance_wide",
                        "note": "Keyspace hit/miss counts cover the whole Redis instance, not just session_cache lookups.",
                    }
                    try:
                        # Count keys per prefix in parallel with a per-prefix ceiling.
                        # Sequential materializing scans (`len([k async for k in scan_iter(...)])`)
                        # blew the 15s probe budget once `session:*` and `agent_meta:*` grew past
                        # ~10K keys combined. `_bounded_scan_count` streams the cursor without
                        # materializing the key list and short-circuits at `cap`, returning
                        # `f"{cap}+"` so operators still see an "above ceiling" signal.
                        labels = ("sessions", "rate_limits", "metadata", "locks")
                        patterns = ("session:*", "rate_limit:*", "agent_meta:*", "lock:*")
                        # Read cap/page from module namespace so test patches and operator
                        # overrides take effect without per-call argument plumbing.
                        results = await asyncio.gather(
                            *(_bounded_scan_count(redis, p, cap=_HEALTH_KEYS_CAP, page=_HEALTH_KEYS_SCAN_PAGE) for p in patterns),
                            return_exceptions=True,
                        )
                        keys_info: Dict[str, Any] = {}
                        for label, value in zip(labels, results):
                            if isinstance(value, BaseException):
                                keys_info[f"{label}_error"] = str(value)
                            else:
                                keys_info[label] = value
                        checks["redis_cache"]["keys"] = keys_info
                    except Exception as e:
                        checks["redis_cache"]["keys_error"] = str(e)
            except Exception as e:
                checks["redis_cache"]["stats_error"] = str(e)
        else:
            checks["redis_cache"] = {
                "status": "unavailable",
                "present": False,
                "note": "Redis not available - using fallback modes",
            }
    except ImportError:
        checks["redis_cache"] = {
            "status": "unavailable",
            "present": False,
            "note": "Redis cache module not installed",
        }
    except Exception as e:
        checks["redis_cache"] = {"status": "error", "present": False, "error": str(e)}

    # Wave 2 §"Lease-integration boundary hardening" — Phase C.5 (#417 follow-on).
    # Surface the Python↔BEAM lease-plane boundary in the deep-health snapshot.
    # Uses the failure-safe LeasePlaneClient.health_check() (Phase C) so a
    # transport/auth/server failure here can't propagate. The client is sync
    # stdlib urllib, so it runs in the executor — keeps the probe off the
    # anyio task group entirely (the original reason the BEAM port exists).
    checks["lease_plane"] = await _probe_lease_plane_boundary(loop)

    continuity_status = get_identity_continuity_status(
        redis_present=checks.get("redis_cache", {}).get("present"),
        redis_operational=checks.get("redis_cache", {}).get("status") not in {"error", "unavailable"},
    )
    checks["identity_continuity"] = continuity_status

    # KG check: get_knowledge_graph() initializes the AGE backend which acquires a
    # PostgreSQL connection — this deadlocks in the anyio context. Skip DB interaction
    # and report capability flags via class introspection.
    #
    # Two separate signals — the embedder service can be up while the active
    # backend lacks semantic_search, in which case `knowledge action=search`
    # silently routes to FTS. Surfacing both lets callers see the gap.
    try:
        embedder_ok = False
        try:
            from src.embeddings import embeddings_available
            embedder_ok = embeddings_available()
        except Exception:
            pass
        try:
            from src.knowledge_graph import (
                backend_supports_semantic_search,
                selected_backend_name,
            )
            backend_name = selected_backend_name()
            semantic_backend_ok = backend_supports_semantic_search()
        except Exception:
            backend_name = "unknown"
            semantic_backend_ok = False

        semantic_search_reachable = embedder_ok and semantic_backend_ok
        checks["knowledge_graph"] = {
            "status": "healthy" if semantic_search_reachable else "degraded",
            "backend": backend_name,
            "embedder_available": embedder_ok,
            "semantic_backend_available": semantic_backend_ok,
            "semantic_search_reachable": semantic_search_reachable,
        }
        if not embedder_ok:
            checks["knowledge_graph"]["warning"] = (
                "Embedder service not loaded — semantic search unavailable."
            )
        elif not semantic_backend_ok:
            checks["knowledge_graph"]["warning"] = (
                f"Embedder is up but backend '{backend_name}' has no semantic_search; "
                f"`knowledge action=search` falls back to FTS."
            )
    except Exception as e:
        checks["knowledge_graph"] = {"status": "error", "error": str(e)}

    checks["agent_metadata"] = {
        "status": "healthy",
        "backend": "postgres",
        "note": "Agent metadata stored in core.identities table (PostgreSQL)",
    }

    try:
        data_dir = Path(server.project_root) / "data"
        data_dir_exists = await loop.run_in_executor(None, lambda: data_dir.exists())
        checks["data_directory"] = {"status": "healthy" if data_dir_exists else "warning", "exists": data_dir_exists}
    except Exception as e:
        checks["data_directory"] = {"status": "error", "error": str(e)}

    # Pi connectivity check is skipped entirely when the unitares-pi-plugin
    # isn't installed; a missing plugin is the default in OSS builds and
    # shouldn't surface as a "degraded" signal in the health payload.
    try:
        from unitares_pi_plugin.handlers import PI_MCP_URLS, call_pi_tool  # type: ignore
    except ImportError:
        pass
    else:
        try:
            pi_start = _time.time()
            pi_result = await asyncio.wait_for(call_pi_tool("get_health", {}, timeout=3.0), timeout=4.0)
            pi_latency = (_time.time() - pi_start) * 1000
            if isinstance(pi_result, dict) and "error" not in pi_result:
                checks["pi_connectivity"] = {
                    "status": "healthy",
                    "reachable": True,
                    "latency_ms": round(pi_latency, 1),
                    "urls_configured": PI_MCP_URLS,
                }
            else:
                error_msg = str(pi_result.get("error", "unknown")) if isinstance(pi_result, dict) else str(pi_result)
                checks["pi_connectivity"] = {
                    "status": "warning",
                    "reachable": False,
                    "error": error_msg,
                    "urls_configured": PI_MCP_URLS,
                }
        except (asyncio.TimeoutError, Exception) as e:
            checks["pi_connectivity"] = {"status": "warning", "reachable": False, "error": str(e)}

    effective_checks = dict(checks)
    redis_check = effective_checks.get("redis_cache")
    if (
        continuity_status
        and continuity_status.get("mode") == "degraded-local"
        and isinstance(redis_check, dict)
        and redis_check.get("status") == "unavailable"
    ):
        effective_checks.pop("redis_cache", None)

    # Wave 2 Phase C.5: pop lease_plane from overall-status calculation when
    # it's "unavailable" (no LEASE_PLANE_BEARER_TOKEN configured for this
    # deploy). Mirrors the redis-cache treatment: an opt-in component that
    # isn't configured shouldn't degrade the overall snapshot. A "warning"
    # or "error" status here (i.e. bearer IS configured but the BEAM is
    # unhealthy or unreachable) DOES still degrade — that's an actionable
    # signal the operator needs to see in overall status.
    lease_plane_check = effective_checks.get("lease_plane")
    if (
        isinstance(lease_plane_check, dict)
        and lease_plane_check.get("status") == "unavailable"
    ):
        effective_checks.pop("lease_plane", None)

    statuses = [c.get("status") for c in effective_checks.values()]
    overall_status = "critical" if "error" in statuses else ("healthy" if all(s == "healthy" for s in statuses) else "moderate")
    status_breakdown = {
        "healthy": sum(1 for s in statuses if s == "healthy"),
        "warning": sum(1 for s in statuses if s == "warning"),
        "deprecated": sum(1 for s in statuses if s == "deprecated"),
        "unavailable": sum(1 for s in statuses if s == "unavailable"),
        "error": sum(1 for s in statuses if s == "error"),
    }
    failing_checks = sorted(name for name, check in effective_checks.items() if check.get("status") == "error")
    degraded_checks = sorted(
        name for name, check in effective_checks.items() if check.get("status") in {"warning", "deprecated", "unavailable"}
    )

    first_action = "No action needed."
    if "primary_db" in failing_checks:
        first_action = "Check PostgreSQL availability and database initialization first."
    elif "redis_cache" in failing_checks:
        first_action = "Check Redis connectivity or continue in fallback mode if Redis is optional."
    elif "knowledge_graph" in failing_checks:
        first_action = "Check knowledge graph backend and embeddings availability."
    elif "pi_connectivity" in degraded_checks or "pi_connectivity" in failing_checks:
        first_action = "Check Pi/anima connectivity only if Pi orchestration is required."
    elif failing_checks:
        first_action = f"Inspect the first failing component: {failing_checks[0]}."
    elif degraded_checks:
        first_action = f"Review the first degraded component: {degraded_checks[0]}."

    response = {
        "status": overall_status,
        "version": getattr(server, "SERVER_VERSION", "unknown"),
        "redis_present": continuity_status.get("redis_present", False),
        "identity_continuity_mode": continuity_status.get("mode", "unknown"),
        "status_breakdown": status_breakdown,
        "operator_summary": {
            "overall_status": overall_status,
            "failing_checks": failing_checks,
            "degraded_checks": degraded_checks,
            "first_action": first_action,
            "identity_continuity_mode": continuity_status.get("mode", "unknown"),
        },
        "timestamp": datetime.now().isoformat(),
    }

    # Circuit breaker telemetry — surfaced in both lite and full so dashboards
    # can read trips_1h/trips_24h without needing get_governance_metrics. The
    # payload is small (three counters per breaker) so always-on is fine.
    try:
        from src.agent_loop_detection import get_circuit_breaker_telemetry
        from src.cache.redis_client import get_circuit_breaker as get_redis_cb
        response["circuit_breakers"] = {
            "governance": get_circuit_breaker_telemetry(),
            "redis": get_redis_cb().get_telemetry(),
        }
    except Exception as e:
        logger.debug(f"Could not gather circuit breaker telemetry: {e}")

    lite = arguments.get("lite", True)
    if lite:
        lite_checks = {}
        # Keys the lite payload always carries forward when present. Includes
        # the #165 capability split so operators reading the lite response can
        # see embedder_available vs semantic_backend_available without having
        # to opt into lite=false.
        lite_keys = (
            "mode", "redis_present", "present", "source_of_truth",
            "session_binding_backend", "backend",
            "embedder_available", "semantic_backend_available",
            "semantic_search_reachable",
        )
        for name, check in checks.items():
            lite_checks[name] = {"status": check.get("status", "unknown")}
            for key in lite_keys:
                if key in check:
                    lite_checks[name][key] = check[key]
            if "warning" in check:
                lite_checks[name]["warning"] = check["warning"]
            if "note" in check:
                lite_checks[name]["note"] = check["note"]
        response["checks"] = lite_checks
        response["_note"] = "Use lite=false for full diagnostic detail"
    else:
        response["checks"] = checks

    return response
