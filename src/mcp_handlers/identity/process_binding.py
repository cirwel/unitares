"""Concurrent identity binding invariant (issue #123).

Records the execution context a client declares at onboard() and detects
same-UUID siphoning across live execution contexts. V1 is audit-only:
collision emits `identity_concurrent_binding` via the broadcaster — no
automatic force-new.

 and issue #123 for the design.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# "Live" window used when rows have not been explicitly marked stale.
# Bindings whose last_seen is older than this are not counted for collision
# detection, so a resident that restarts 10 minutes later does not trip the
# alarm even before the sweeper runs. This is a collision window, not a
# resident-cadence setting.
LIVE_WINDOW_SECONDS = 300  # 5 minutes

ALLOWED_TRANSPORTS = frozenset({"stdio", "http", "websocket", "sse", "rest", "unknown"})


@dataclass(frozen=True)
class ProcessFingerprint:
    """Validated client-reported execution context."""

    host_id: str
    pid: int
    pid_start_time: float
    transport: str = "unknown"
    ppid: Optional[int] = None
    tty: Optional[str] = None
    anchor_path_hash: Optional[str] = None


def validate_fingerprint(raw: Any) -> Optional[ProcessFingerprint]:
    """Coerce a client-reported fingerprint dict into ProcessFingerprint.

    Returns None if the payload is missing or malformed. Callers treat a
    None return as "no fingerprint declared" — onboard still succeeds, we
    just can't record a binding. Validation is permissive-but-typed: the
    three identity-key fields are required; everything else is best-effort.
    """
    if not isinstance(raw, dict):
        return None

    host_id = raw.get("host_id")
    pid = raw.get("pid")
    pid_start_time = raw.get("pid_start_time")
    if not isinstance(host_id, str) or not host_id:
        return None
    if not isinstance(pid, int) or pid <= 0:
        return None
    if not isinstance(pid_start_time, (int, float)) or pid_start_time <= 0:
        return None

    transport = raw.get("transport") or "unknown"
    if not isinstance(transport, str) or transport not in ALLOWED_TRANSPORTS:
        transport = "unknown"

    ppid = raw.get("ppid")
    if ppid is not None and (not isinstance(ppid, int) or ppid <= 0):
        ppid = None

    tty = raw.get("tty")
    if tty is not None and not isinstance(tty, str):
        tty = None

    anchor_path_hash = raw.get("anchor_path_hash")
    if anchor_path_hash is not None and not isinstance(anchor_path_hash, str):
        anchor_path_hash = None

    return ProcessFingerprint(
        host_id=host_id,
        pid=pid,
        pid_start_time=float(pid_start_time),
        transport=transport,
        ppid=ppid,
        tty=tty,
        anchor_path_hash=anchor_path_hash,
    )


def _recently_released(agent_id: str) -> bool:
    try:
        from src.mcp_handlers.identity.agent_presence_lease import recently_released

        return recently_released(agent_id)
    except Exception:  # pragma: no cover - never let the guard break recording
        return False


def _presence_module():
    try:
        from src.mcp_handlers.identity import agent_presence_lease

        return agent_presence_lease
    except Exception:  # pragma: no cover - never let the guard break recording
        return None


async def record_binding_bg(
    agent_id: str,
    fp: ProcessFingerprint,
    client_session_id: Optional[str] = None,
) -> None:
    """Persist a binding and emit an audit event on concurrent collision.

    Fire-and-forget background coroutine — caller schedules via
    `create_tracked_task`. Never raises; all failures are logged.

    V1 behavior:
      1. UPSERT the binding row (last_seen bumps on repeat onboard from
         same execution context).
      2. Count distinct live execution-context tuples for this agent
         (live = stale_at IS NULL AND last_seen within LIVE_WINDOW_SECONDS).
      3. If count >= 2 and agent.allow_concurrent_contexts is false, emit
         an `identity_concurrent_binding` broadcaster event. No force-new.

    Skipped when the identity has just released its presence on a clean exit:
    a queued insert landing after that release would otherwise re-create the
    live binding the release retired, and block an immediate successor.
    """
    if _recently_released(agent_id):
        return
    # Open an in-flight window: the suppression tombstone expires after a
    # bounded time, but this insert can stall on the database for longer, and
    # its post-write check must still see a release that landed meanwhile.
    presence = _presence_module()
    opened = presence.begin_binding_insert(agent_id) if presence else None
    try:
        from src.db import get_db

        db = get_db()
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO core.agent_process_bindings
                    (agent_id, host_id, pid, pid_start_time, transport,
                     ppid, tty, anchor_path_hash, client_session_id,
                     onboard_ts, last_seen)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW(), NOW())
                ON CONFLICT (agent_id, host_id, pid, pid_start_time, transport)
                DO UPDATE SET
                    last_seen = NOW(),
                    tty = COALESCE(EXCLUDED.tty, core.agent_process_bindings.tty),
                    ppid = COALESCE(EXCLUDED.ppid, core.agent_process_bindings.ppid),
                    anchor_path_hash = COALESCE(
                        EXCLUDED.anchor_path_hash,
                        core.agent_process_bindings.anchor_path_hash
                    ),
                    client_session_id = COALESCE(
                        EXCLUDED.client_session_id,
                        core.agent_process_bindings.client_session_id
                    ),
                    stale_at = NULL
                """,
                agent_id,
                fp.host_id,
                fp.pid,
                fp.pid_start_time,
                fp.transport,
                fp.ppid,
                fp.tty,
                fp.anchor_path_hash,
                client_session_id,
            )

            # The identity may have exited while this insert was waiting on the
            # database. The release sets its marker before it retires bindings,
            # so re-checking after the write closes that interleaving: this row
            # is retired here if the retirement already ran, however long the
            # write stalled.
            if (
                presence.released_after(agent_id, opened)
                if presence
                else _recently_released(agent_id)
            ):
                await conn.execute(
                    """
                    UPDATE core.agent_process_bindings
                    SET stale_at = NOW()
                    WHERE agent_id = $1 AND stale_at IS NULL
                    """,
                    agent_id,
                )
                return

            live_rows = await conn.fetch(
                f"""
                SELECT host_id, pid, pid_start_time, transport, tty, ppid, last_seen
                FROM core.agent_process_bindings
                WHERE agent_id = $1
                  AND stale_at IS NULL
                  AND last_seen > NOW() - INTERVAL '{LIVE_WINDOW_SECONDS} seconds'
                ORDER BY last_seen DESC
                """,
                agent_id,
            )

            if len(live_rows) < 2:
                return

            agent_row = await conn.fetchrow(
                """
                SELECT COALESCE(allow_concurrent_contexts, FALSE) AS allow_concurrent
                FROM core.agents
                WHERE id = $1
                """,
                agent_id,
            )
            allow_concurrent = bool(agent_row and agent_row["allow_concurrent"])
            if allow_concurrent:
                return

        _emit_concurrent_binding_event(agent_id, live_rows)

    except Exception as e:  # pragma: no cover — defensive
        logger.debug(f"[PROCESS_BINDING] record_binding_bg failed (non-fatal): {e}")
    finally:
        if presence:
            presence.end_binding_insert(agent_id)


def _emit_concurrent_binding_event(agent_id: str, live_rows: List[Any]) -> None:
    """Broadcast `identity_concurrent_binding` for a detected collision."""
    contexts = [
        {
            "host_id": row["host_id"],
            "pid": row["pid"],
            "pid_start_time": row["pid_start_time"],
            "transport": row["transport"],
            "tty": row["tty"],
            "ppid": row["ppid"],
            "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None,
        }
        for row in live_rows
    ]
    logger.warning(
        "[CONCURRENT_BINDING] Agent %s has %d live execution contexts; "
        "allow_concurrent_contexts=false. Contexts: %s",
        agent_id[:8] + "...",
        len(contexts),
        contexts,
    )

    try:
        from src.broadcaster import broadcaster_instance
    except Exception:
        return

    async def _broadcast():
        try:
            await broadcaster_instance.broadcast_event(
                event_type="identity_concurrent_binding",
                agent_id=agent_id,
                payload={
                    "live_context_count": len(contexts),
                    "contexts": contexts,
                },
            )
        except Exception as e:
            logger.debug(f"[CONCURRENT_BINDING] broadcast failed: {e}")

    try:
        from src.background_tasks import create_tracked_task
        create_tracked_task(_broadcast(), name="concurrent_binding_event")
    except Exception:
        import asyncio
        try:
            asyncio.ensure_future(_broadcast())
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Sweeper — marks bindings stale once their last_seen falls outside the live
# window. Audit-only v1 does not act on stale_at, but populating it lets the
# diagnose view and future enforcement (v2) distinguish "no longer live" from
# "never existed."
# -----------------------------------------------------------------------------


async def sweep_stale_bindings() -> int:
    """Mark bindings stale when last_seen exceeds LIVE_WINDOW_SECONDS.

    Returns the number of rows marked stale. Safe to call concurrently; the
    UPDATE is idempotent and narrows on `stale_at IS NULL`.
    """
    try:
        from src.db import get_db
        db = get_db()
        async with db.acquire() as conn:
            updated = await conn.execute(
                f"""
                UPDATE core.agent_process_bindings
                SET stale_at = NOW()
                WHERE stale_at IS NULL
                  AND last_seen <= NOW() - INTERVAL '{LIVE_WINDOW_SECONDS} seconds'
                """
            )
        # asyncpg execute() returns a status string like "UPDATE 17"; parse the
        # count if present, otherwise fall back to 0.
        try:
            return int((updated or "UPDATE 0").split()[-1])
        except Exception:
            return 0
    except Exception as e:
        logger.debug(f"[PROCESS_BINDING] sweep_stale_bindings failed: {e}")
        return 0


async def retire_bindings(agent_id: str) -> Optional[int]:
    """Mark the identity's live bindings stale on a clean exit.

    The lineage gate counts a live binding as a running parent for
    LIVE_WINDOW_SECONDS, so an exit that only released its presence lease would
    still block an immediate successor. Scoped to the identity, not a session
    id: a fresh start_session records its binding before its client_session_id
    exists, and one live process per identity is the contract. The caller
    retires only when the exiting session was the identity's last live holder.
    Returns the number of rows retired, or None when the update failed, so a
    caller can tell "nothing to retire" from "a live binding may remain".
    """
    if not agent_id:
        return 0
    try:
        from src.db import get_db
        db = get_db()
        async with db.acquire() as conn:
            updated = await conn.execute(
                """
                UPDATE core.agent_process_bindings
                SET stale_at = NOW()
                WHERE agent_id = $1
                  AND stale_at IS NULL
                """,
                agent_id,
            )
        try:
            return int((updated or "UPDATE 0").split()[-1])
        except Exception:
            return 0
    except Exception as e:
        logger.warning(f"[PROCESS_BINDING] retire_bindings failed: {e}")
        return None


async def get_live_bindings(agent_id: str) -> List[Dict[str, Any]]:
    """Return all live bindings for an agent, most-recent first.

    "Live" = `stale_at IS NULL AND last_seen within LIVE_WINDOW_SECONDS`.
    Returns an empty list on DB failure — callers surface that as "no
    bindings reported" rather than raising.
    """
    try:
        from src.db import get_db
        db = get_db()
        async with db.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT host_id, pid, pid_start_time, transport,
                       tty, ppid, anchor_path_hash, client_session_id,
                       onboard_ts, last_seen, same_host_ppid_consistent
                FROM core.agent_process_bindings
                WHERE agent_id = $1
                  AND stale_at IS NULL
                  AND last_seen > NOW() - INTERVAL '{LIVE_WINDOW_SECONDS} seconds'
                ORDER BY last_seen DESC
                """,
                agent_id,
            )
        return [
            {
                "host_id": r["host_id"],
                "pid": r["pid"],
                "pid_start_time": r["pid_start_time"],
                "transport": r["transport"],
                "tty": r["tty"],
                "ppid": r["ppid"],
                "anchor_path_hash": r["anchor_path_hash"],
                "client_session_id": r["client_session_id"],
                "onboard_ts": r["onboard_ts"].isoformat() if r["onboard_ts"] else None,
                "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
                "same_host_ppid_consistent": r["same_host_ppid_consistent"],
            }
            for r in rows
        ]
    except Exception as e:
        logger.debug(f"[PROCESS_BINDING] get_live_bindings failed: {e}")
        return []


async def has_live_agent_lease(agent_uuid: Optional[str]) -> bool:
    """True if the agent holds a live ``agent:/<uuid>`` lease-plane presence lease.

    The lease-plane liveness signal for EPHEMERAL agents — the ones that don't
    hold a ``local_beam`` resident lease and (today) write no process binding, so
    ``get_live_bindings`` is structurally blind to them. The recurring
    false-archival bug is exactly that blindness: an ephemeral agent with no
    runtime liveness signal gets archived as a succeeded predecessor while still
    working. Pairs with the check-in-path producer that acquires/heartbeats the
    ``agent:/<uuid>`` lease.

    Liveness = an unreleased ``agent:/`` surface lease whose TTL has not expired
    (keyed on ``expires_at``, NOT ``last_heartbeat_at`` — resident leases leave
    that NULL by design). Reads ``lease_plane.surface_leases`` directly (same
    governance DB). Returns False on a missing uuid or ANY error, so the caller
    falls back to its other liveness signals — this can only ADD protection,
    never remove it.
    """
    if not agent_uuid:
        return False
    try:
        from src.db import get_db
        db = get_db()
        async with db.acquire() as conn:
            live = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM lease_plane.surface_leases
                    WHERE surface_id = $1
                      AND released_at IS NULL
                      AND expires_at > NOW()
                )
                """,
                f"agent:/{agent_uuid}",
            )
        return bool(live)
    except Exception as e:  # pragma: no cover - defensive, fail-open to other signals
        logger.debug(f"[PROCESS_BINDING] has_live_agent_lease failed (non-fatal): {e}")
        return False


async def process_binding_sweeper_task() -> None:
    """Periodic sweeper — runs every LIVE_WINDOW_SECONDS.

    Registered from src/background_tasks.py alongside the other periodic
    tasks. Cadence matches the live-window: a binding that did not refresh
    in the last window is marked stale on the next tick.
    """
    import asyncio
    await asyncio.sleep(30.0)  # startup delay, match matview/partition pattern
    while True:
        try:
            n = await sweep_stale_bindings()
            if n:
                logger.info(f"[PROCESS_BINDING] swept {n} stale binding(s)")
        except Exception as e:
            logger.debug(f"[PROCESS_BINDING] sweeper tick failed: {e}")
        await asyncio.sleep(LIVE_WINDOW_SECONDS)
