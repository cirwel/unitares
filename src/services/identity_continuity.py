"""Helpers for reporting identity continuity mode across transports and health surfaces."""

from __future__ import annotations

from typing import Any, Dict, Optional


async def probe_identity_continuity_status() -> Dict[str, Any]:
    """Probe actual Redis operability before announcing continuity mode."""
    redis_configured = False
    redis_operational = False
    try:
        from src.cache import get_redis, is_redis_available

        redis_configured = bool(is_redis_available())
        redis_operational = bool(await get_redis())
    except Exception:
        redis_operational = False

    status = get_identity_continuity_status(
        redis_present=redis_operational,
        redis_operational=redis_operational,
    )
    if redis_configured and not redis_operational:
        status["configured_but_unavailable"] = True
        status["note"] = (
            "Redis is configured but unavailable; identity continuity is running in degraded-local "
            "mode with process-local session bindings and PostgreSQL persistence."
        )
        status["warning"] = (
            "Redis connectivity failed during startup; degraded-local continuity is active."
        )
    return status


def get_identity_continuity_status(
    *,
    redis_present: Optional[bool] = None,
    redis_operational: Optional[bool] = None,
) -> Dict[str, Any]:
    """Describe whether identity continuity is Redis-backed or degraded-local."""
    if redis_present is None:
        try:
            from src.cache import is_redis_available

            redis_present = bool(is_redis_available())
        except Exception:
            redis_present = False

    mode = "redis" if redis_present else "degraded-local"
    if redis_operational is None:
        redis_operational = bool(redis_present)

    if mode == "redis":
        status = "healthy" if redis_operational else "warning"
        note = (
            "Redis is present and is the AUTHORITATIVE store for live session "
            "bindings — most active sessions exist only in Redis, because the "
            "default resolve path is persist=False. PostgreSQL is durable for "
            "identities; it holds sessions only for the onboarded subset, plus "
            "a best-effort mirror when UNITARES_SESSION_MIRROR_SHADOW is set "
            "that nothing reads yet. Losing Redis loses live session bindings."
        )
    else:
        # Degraded-local is an expected fallback in local/dev mode, so surface it
        # explicitly without marking the whole system unhealthy by default.
        status = "healthy"
        note = (
            "Redis is absent; identity continuity is running in degraded-local mode "
            "with process-local session bindings and PostgreSQL persistence."
        )

    if mode == "redis":
        capabilities = {
            "identity_persistence": "postgres (survives restart)",
            "session_binding": "redis-backed (cross-process, fast TTL expiry)",
            "onboard_pins": "redis (30min TTL, browser fingerprint resumption)",
            "distributed_locking": "redis (prevents concurrent updates)",
            "metadata_cache": "redis (sub-ms reads)",
        }
        degraded_capabilities: list = []
    else:
        capabilities = {
            "identity_persistence": "postgres (survives restart)",
            "session_binding": "in-memory (this process only, lost on restart)",
            "onboard_pins": "unavailable (redis-only)",
            "distributed_locking": "unavailable (single-process assumed)",
            "metadata_cache": "in-memory (per-process, no shared cache)",
        }
        degraded_capabilities = [
            "session_binding: in-memory only, lost on restart, not shared across processes",
            "onboard_pins: unavailable, clients must pass explicit client_session_id",
            "distributed_locking: unavailable, concurrent access not guarded",
            "metadata_cache: per-process only, no cross-instance sharing",
        ]

    payload: Dict[str, Any] = {
        "status": status,
        "mode": mode,
        "redis_present": bool(redis_present),
        # Split deliberately: the old flat "postgres" was false for the half that
        # matters operationally. Identities are durable in PG; live session
        # bindings are Redis-authoritative and are NOT mirrored durably yet
        # (Redis-retirement Phase 1 — docs/proposals/redis-retirement-v0.md).
        "source_of_truth": (
            "postgres (identities); redis (live session bindings)"
            if mode == "redis"
            else "postgres (identities); in-memory (session bindings, lost on restart)"
        ),
        "identity_source_of_truth": "postgres",
        "session_binding_source_of_truth": "redis" if mode == "redis" else "in-memory",
        "session_binding_backend": (
            "redis-backed session cache" if mode == "redis" else "in-memory fallback cache"
        ),
        "capabilities": capabilities,
        "degraded_capabilities": degraded_capabilities,
        "note": note,
    }
    if mode == "redis" and not redis_operational:
        payload["warning"] = (
            "Redis is present but not operating cleanly; fallback session behavior may be active."
        )
    return payload


def format_identity_continuity_startup_message(status: Optional[Dict[str, Any]] = None) -> str:
    """Render a single-line startup message for operators."""
    status = status or get_identity_continuity_status()
    redis_clause = "Redis present" if status.get("redis_present") else "Redis absent"
    binding = status.get("session_binding_source_of_truth", "unknown")
    return (
        f"Identity continuity mode: {status.get('mode', 'unknown')} "
        f"({redis_clause}; identities durable in PostgreSQL, "
        f"live session bindings authoritative in {binding})"
    )
