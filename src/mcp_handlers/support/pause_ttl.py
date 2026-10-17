"""Pause TTL — auto-expire stale paused states across all gate-traversal sites.

A paused agent's status persists in `mcp_server.agent_metadata` until an
explicit `self_recovery` call. Sleep-wake artifacts (Mac clamshell, host
suspend, Pi power blip) can trigger a categorizer-driven pause whose
underlying "stale state input" cause has long since resolved, but the
pause itself persists indefinitely because every subsequent
gate-traversal is rejected before the categorizer can re-evaluate.

This module provides a TTL: pauses older than
`GovernanceConfig.PAUSE_AUTO_EXPIRE_SECONDS` auto-clear on the next
gate-traversal, letting the categorizer re-evaluate. A genuinely
degraded agent re-pauses on the next cycle via the normal
circuit-breaker path. A sleep-wake artifact resumes clean.

All callers share `_pause_is_stale` for the decision and the same token-fenced
durable resume. The cache is published active only after both durable status
copies and the pause token commit atomically. Audit visibility via
`audit_logger.log_pause_auto_expired` follows that commit.

The async entry point can await the commit and continue the current request.
The sync entry point schedules the same transaction on the running loop (or
AuditLogger's captured main loop) and keeps the current request blocked; the
next retry observes the committed active cache. A synchronous gate must never
grant operation while its durable resume is still in flight.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)


# ─── Stale-pause detection ─────────────────────────────────────────────


def _pause_is_stale(paused_at: Optional[str]) -> bool:
    """Return True if the recorded pause is older than the TTL.

    `paused_at` is an ISO 8601 string per `AgentMetadata`. Unparseable
    or absent values are treated as not-stale (fail-closed: keep the
    pause rather than auto-clear on bad data).

    Naive timestamps are interpreted as UTC. Both the canonical write
    path (`src/agent_loop_detection.py:514` via
    `datetime.now(timezone.utc).isoformat()`) and legacy persisted
    naive forms compare correctly under this convention.
    """
    if not paused_at:
        return False
    try:
        from config.governance_config import GovernanceConfig
        threshold_s = int(GovernanceConfig.PAUSE_AUTO_EXPIRE_SECONDS)
        paused_dt = datetime.fromisoformat(paused_at.replace("Z", "+00:00"))
        if paused_dt.tzinfo is None:
            # Legacy naive form — interpret as UTC, matching the
            # convention used elsewhere when serializing timestamps.
            paused_dt = paused_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - paused_dt).total_seconds() > threshold_s
    except (ValueError, TypeError, AttributeError, ImportError):
        return False


def _apply_in_memory_expire(meta: Any) -> Optional[str]:
    """Flip status and clear paused_at in-memory; record a lifecycle event.

    Returns the original paused_at string for use by callers that need
    to log it. Preserves the system invariant that
    `status == "paused" ⟺ paused_at is truthy` (asserted at
    `src/agent_metadata_model.py:260` and read at
    `src/auto_ground_truth.py:285`).
    """
    original_paused_at = meta.paused_at
    meta.status = "active"
    meta.paused_at = None
    meta.add_lifecycle_event(
        "pause_auto_expired",
        f"pause from {original_paused_at} aged out beyond "
        f"PAUSE_AUTO_EXPIRE_SECONDS; categorizer re-evaluation triggered "
        f"on this gate-traversal",
    )
    return original_paused_at


_TTL_RECOVERY_REFUSAL_ATTR = "_pause_ttl_recovery_refusal"


def _clear_ttl_recovery_refusal(meta: Any, pause_token: str) -> None:
    """Clear only the refusal record owned by ``pause_token``.

    Multiple TTL attempts can overlap a later pause installation. A completion
    for generation A must never erase recovery guidance already recorded for
    generation B.
    """
    state = getattr(meta, "__dict__", None)
    if not isinstance(state, dict):
        return
    refusal = state.get(_TTL_RECOVERY_REFUSAL_ATTR)
    if (
        isinstance(refusal, dict)
        and refusal.get("pause_token") == pause_token
    ):
        state.pop(_TTL_RECOVERY_REFUSAL_ATTR, None)


def _record_ttl_recovery_refusal(
    meta: Any,
    *,
    pause_token: str,
    resume: dict,
) -> None:
    """Record durable repair guidance only for the still-current generation."""
    if (
        getattr(meta, "status", None) != "paused"
        or getattr(meta, "paused_at", None) != pause_token
    ):
        return
    setattr(meta, _TTL_RECOVERY_REFUSAL_ATTR, {
        "pause_token": pause_token,
        "reason": resume.get("reason"),
        "agent_status": resume.get("agent_status"),
        "identity_status": resume.get("identity_status"),
    })


def _emit_audit(agent_uuid: str, original_paused_at: Optional[str]) -> None:
    """Fire-and-forget audit emit. Never raises."""
    try:
        from src.audit_log import audit_logger
        elapsed_s: float = 0.0
        if original_paused_at:
            try:
                paused_dt = datetime.fromisoformat(
                    original_paused_at.replace("Z", "+00:00")
                )
                if paused_dt.tzinfo is None:
                    paused_dt = paused_dt.replace(tzinfo=timezone.utc)
                elapsed_s = (
                    datetime.now(timezone.utc) - paused_dt
                ).total_seconds()
            except (ValueError, TypeError):
                pass
        audit_logger.log_pause_auto_expired(
            agent_id=agent_uuid,
            original_paused_at=original_paused_at,
            elapsed_seconds=elapsed_s,
        )
    except Exception as exc:  # noqa: BLE001 — observability MUST NOT mask
        logger.debug("[pause-ttl] audit emit failed: %r", exc)


# ─── Async entry point (process_agent_update path) ─────────────────────


async def _expire_pause_durably(
    agent_uuid: str,
    meta: Any,
    original_paused_at: Optional[str],
    *,
    sync_path: bool = False,
) -> bool:
    """Consume one exact stale pause, then publish the committed cache state."""
    if not isinstance(original_paused_at, str) or not original_paused_at.strip():
        return False

    lifecycle_event = {
        "event": "pause_auto_expired",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": (
            f"pause from {original_paused_at} aged out"
            + (" (sync path)" if sync_path else "")
        ),
        "pause_token": original_paused_at,
    }
    try:
        from src import agent_storage

        resume = await agent_storage.expire_pause_if_token(
            agent_uuid,
            expected_pause_token=original_paused_at,
            lifecycle_event=lifecycle_event,
        )
    except Exception as exc:  # noqa: BLE001 — a failed commit stays paused
        logger.warning(
            "[pause-ttl] durable pause expiry failed; keeping cache paused: %r",
            exc,
        )
        return False

    if not resume.get("applied"):
        # A concurrent durable transition won. Reconcile only facts the locked
        # read proved; never clear a newer pause generation from stale cache.
        if resume.get("reason") == "pause_generation_superseded":
            current_token = resume.get("current_pause_token")
            if (
                resume.get("agent_status") == "paused"
                and resume.get("identity_status") == "paused"
                and isinstance(current_token, str)
                and current_token.strip()
                and getattr(meta, "paused_at", None) == original_paused_at
            ):
                meta.paused_at = current_token
            _clear_ttl_recovery_refusal(meta, original_paused_at)
        elif resume.get("reason") in {
            "durable_identity_missing",
            "operator_reconciliation_required",
            "terminal_agent_status",
        }:
            _record_ttl_recovery_refusal(
                meta,
                pause_token=original_paused_at,
                resume=resume,
            )
        logger.warning(
            "[pause-ttl] durable pause expiry refused (%s); keeping cache paused",
            resume.get("reason") or "unknown",
        )
        return False

    _clear_ttl_recovery_refusal(meta, original_paused_at)
    if resume.get("already_active") or resume.get("repaired_stale_pause_token"):
        logger.warning(
            "[pause-ttl] reconciled stale cache pause against active durable "
            "state (paused_at=%s)%s",
            original_paused_at,
            " (sync path)" if sync_path else "",
        )
        if (
            getattr(meta, "status", None) == "paused"
            and getattr(meta, "paused_at", None) == original_paused_at
        ):
            meta.status = "active"
            meta.paused_at = None
            return True
        return getattr(meta, "status", None) != "paused"

    logger.warning(
        "[pause-ttl] auto-expired stale pause (paused_at=%s); categorizer will "
        "re-evaluate%s",
        original_paused_at,
        " (sync path)" if sync_path else "",
    )
    _emit_audit(agent_uuid, original_paused_at)
    if (
        getattr(meta, "status", None) == "paused"
        and getattr(meta, "paused_at", None) == original_paused_at
    ):
        _apply_in_memory_expire(meta)
        return True
    # Another process published a newer cache pause while the transaction was
    # in flight. The old durable generation did expire, but this traversal must
    # still respect the newer local pause.
    return getattr(meta, "status", None) != "paused"


async def maybe_auto_expire_pause_async(agent_uuid: str, meta: Any) -> bool:
    """Auto-expire a stale pause; await persistence. Returns True if expired.

    Callers should invoke this BEFORE returning the AGENT_PAUSED error
    response. If it returns True, the gate should fall through to
    normal processing (the agent is no longer paused). If False, the
    pause is not stale and the gate should reject as before.
    """
    if not _pause_is_stale(meta.paused_at):
        return False

    original_paused_at = meta.paused_at
    return await _expire_pause_durably(agent_uuid, meta, original_paused_at)


# ─── Sync entry point (check_agent_can_operate path) ───────────────────


# Module-local strong-ref set for in-flight fire-and-forget persistence
# tasks. Mirrors the pattern in `src/coordination_failure_emit.py`: we
# can't rely on the background-tasks supervisor because callers of this
# helper are themselves invoked from a wide variety of contexts (sync
# tools, REST endpoints, MCP handlers).
_inflight_persistence_tasks: "set[asyncio.Task]" = set()
_inflight_expiry_by_agent: "dict[str, asyncio.Task]" = {}


def _spawn_persistence_task(
    loop: asyncio.AbstractEventLoop,
    agent_uuid: str,
    coro_factory: Any,
) -> None:
    """Spawn one expiry per agent and pin a strong ref until it completes."""
    existing = _inflight_expiry_by_agent.get(agent_uuid)
    if existing is not None and not existing.done():
        return
    task = loop.create_task(
        coro_factory(), name="pause_ttl_fire_and_forget_persist"
    )
    _inflight_persistence_tasks.add(task)

    def _discard(completed: asyncio.Task) -> None:
        _inflight_persistence_tasks.discard(completed)
        if _inflight_expiry_by_agent.get(agent_uuid) is completed:
            _inflight_expiry_by_agent.pop(agent_uuid, None)

    _inflight_expiry_by_agent[agent_uuid] = task
    task.add_done_callback(_discard)


def _schedule_persistence_fire_and_forget(
    agent_uuid: str,
    meta: Any,
    original_paused_at: Optional[str],
) -> None:
    """Schedule the persistence coroutine on the best-available loop.

    Mirrors `src/coordination_failure_emit.py:_schedule_coordination_events_dual_write`.
    """
    try:
        def _coro_factory():
            return _expire_pause_durably(
                agent_uuid, meta, original_paused_at, sync_path=True
            )

        try:
            loop = asyncio.get_running_loop()
            _spawn_persistence_task(loop, agent_uuid, _coro_factory)
            return
        except RuntimeError:
            pass

        captured_loop = None
        try:
            from src.audit_log import AuditLogger
            captured_loop = getattr(AuditLogger, "_event_loop", None)
        except Exception:  # noqa: BLE001
            captured_loop = None

        if captured_loop is not None and captured_loop.is_running():
            def _spawn_on_main():
                _spawn_persistence_task(
                    captured_loop, agent_uuid, _coro_factory
                )
            captured_loop.call_soon_threadsafe(_spawn_on_main)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[pause-ttl] persistence schedule failed: %r", exc)


def _parse_utc(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# A pause's lifecycle event is stamped at or after paused_at: exactly at it when
# persisted, and after it in memory (add_lifecycle_event takes its own clock,
# after at least one database round trip). An older pause's event is always
# before the current paused_at, since a later pause moves paused_at forward.
# So the current pause's event is the latest "paused" event not earlier than
# paused_at; the slack only absorbs sub-second clock and serialization noise.
_PAUSE_EVENT_EARLY_SLACK_SECONDS = 1.0


def _post_gap_repause_text() -> str:
    """Describe when a still-bad reading can pause again after expiry.

    Every check-in that arrives more than GAP_RECOVERY_ARM_SECONDS after the
    previous one re-arms gap recovery (GAP_RECOVERY_CYCLES) and cannot pause,
    and neither can the check-ins that drain the counter. An agent that
    checks in less often than the arm interval is therefore never re-paused
    by the gap path, and the text must not promise otherwise.
    """
    try:
        from config.governance_config import GovernanceConfig

        arm = int(GovernanceConfig.GAP_RECOVERY_ARM_SECONDS)
        cycles = int(GovernanceConfig.GAP_RECOVERY_CYCLES)
    except (ValueError, TypeError, AttributeError, ImportError):
        return (
            "Check-ins after a long gap are gap-suppressed, so a reading that "
            "still trips may not pause again at once."
        )
    if cycles <= 0:
        return "A reading that still trips pauses again on the next check-in."
    nth = {1: "first", 2: "second", 3: "third"}.get(cycles, f"{cycles}th")
    rest = cycles - 1
    also = (
        "" if rest == 0
        else ", and neither can the check-in after it" if rest == 1
        else f", and neither can the {rest} check-ins after it"
    )
    return (
        f"A check-in that arrives more than {arm}s after the previous one cannot "
        f"pause{also}. A reading that still "
        f"trips pauses again only on the {nth} check-in in a row to "
        f"arrive within {arm}s of the one before; at a slower cadence it does "
        "not pause again."
    )


def _pause_expires_at(paused_raw: Any) -> Optional[str]:
    """When the current pause ages out under PAUSE_AUTO_EXPIRE_SECONDS, or None."""
    paused_dt = _parse_utc(paused_raw) if isinstance(paused_raw, str) and paused_raw else None
    if paused_dt is None:
        return None
    try:
        from config.governance_config import GovernanceConfig

        return (
            paused_dt
            + timedelta(seconds=int(GovernanceConfig.PAUSE_AUTO_EXPIRE_SECONDS))
        ).isoformat()
    except (ValueError, TypeError, AttributeError, ImportError, OverflowError):
        # OverflowError: an operator can effectively disable expiry with a
        # huge PAUSE_AUTO_EXPIRE_SECONDS; callers must still render.
        return None


def describe_write_gate(meta: Any) -> dict:
    """Whether this agent's governed writes would be refused right now.

    Read from the predicates the write gates apply:
    check_agent_can_operate (support/agent_auth.py) for new shared-memory
    entries, and the check-in phases in updates/phases.py. Both refuse while
    ``status == "paused"`` and the pause is not past
    PAUSE_AUTO_EXPIRE_SECONDS (``_pause_is_stale``). Both also refuse an
    archived identity. Its way back is a proof-carrying
    start_session(resume=true), which unarchives the same UUID
    (require_registered_agent routes there); self_recovery rejects archived
    identities. The check-in phase refuses a deleted identity too, which
    check_agent_can_operate does not check. Any other status, and an
    identity the server holds no record for yet, passes both gates.

    Read-only: unlike the gates it never expires a pause, so a stale pause is
    reported as paused with writes not held, because the next gated write
    lifts it.
    """
    status = getattr(meta, "status", None) if meta is not None else None
    if status == "paused":
        paused_at = getattr(meta, "paused_at", None)
        if _pause_is_stale(paused_at):
            return {
                "paused": True,
                "writes_held": False,
                "note": (
                    "The pause is past its expiry: the next check-in or new "
                    "shared-memory entry lifts it."
                ),
            }
        gate = {
            "paused": True,
            "writes_held": True,
            "held": (
                "Check-ins and new shared-memory entries are refused, not "
                "queued. Dialectic moves are still accepted."
            ),
        }
        expires_at = _pause_expires_at(paused_at)
        if expires_at:
            gate["expires_at"] = expires_at
        return gate
    if status == "archived":
        return {
            "paused": False,
            "writes_held": True,
            "held": (
                "Check-ins and new shared-memory entries are refused while "
                "archived. The same live process reclaims this identity with "
                "start_session(resume=true) and its client_session_id, which "
                "unarchives it; otherwise start_session(force_new=true) "
                "starts a new one."
            ),
        }
    if status == "deleted":
        return {
            "paused": False,
            "writes_held": True,
            "held": (
                "Check-ins are refused: this identity is deleted and cannot "
                "be recovered. start_session(force_new=true) starts a new one."
            ),
        }
    return {"paused": False, "writes_held": False}


def paused_refusal_recovery(meta: Any, agent_uuid: Optional[str] = None) -> dict:
    """The `recovery` block for a write refused because the agent is paused.

    Says what a paused agent can use from its own refusal: why it was paused,
    what happens to its writes, and how a pause actually ends. The earlier
    text said only "circuit breaker triggered due to governance threshold
    violation", which named neither the reason nor an end.

    It does not present self_recovery as the way out. A paused agent cannot
    write the check-in that would lower its risk, so self_recovery (quick at
    <= 0.40, review below 0.65) cannot lift a pause whose risk is frozen above
    those gates; measured 2026-09-24, every pause since July froze at >= 0.70.
    self_recovery(action='check') is still the authority on self-recovery
    eligibility. The reason is taken only from a "paused" event
    stamped at this pause's paused_at, so an older pause's reason is never
    presented as the current one.
    """
    paused_raw = getattr(meta, "paused_at", None)
    paused_dt = _parse_utc(paused_raw) if isinstance(paused_raw, str) and paused_raw else None
    reason = None
    if paused_dt is not None:
        for event in reversed(getattr(meta, "lifecycle_events", None) or []):
            if not (isinstance(event, dict) and event.get("event") == "paused"):
                continue
            event_dt = _parse_utc(event.get("timestamp"))
            if event_dt is not None and (
                (event_dt - paused_dt).total_seconds() >= -_PAUSE_EVENT_EARLY_SLACK_SECONDS
            ):
                reason = event.get("reason")
                break
    expires_at = _pause_expires_at(paused_raw)
    if expires_at:
        re_evaluation = (
            f"After {expires_at} the next check-in attempts an exact-token durable "
            "resume and proceeds only after the durable state is active. A new "
            "shared-memory entry starts the same attempt, but that entry remains "
            "refused; resubmit it after the pause lifts. If the durable identity or "
            "status copies need repair, the refusal names operator reconciliation "
            "instead. " + _post_gap_repause_text()
        )
    else:
        re_evaluation = "No re-evaluation time is recorded for this pause."
    # Dialectic sessions are keyed by the identity UUID (paused_agent_id);
    # meta.agent_id is often a structured display handle, which finds nothing.
    lookup_id = agent_uuid or None
    get_call = (
        f"dialectic(action='get', agent_id='{lookup_id}')" if lookup_id
        else "dialectic(action='get', agent_id=<your agent UUID>)"
    )
    recovery = {
        "action": (
            "Check-ins (sync_state) and new shared-memory entries (store_finding, notes) are refused while "
            "paused and are not queued; resubmit them after the pause lifts. "
            "Dialectic moves are still accepted. self_recovery(action='check') "
            "reports self-recovery eligibility: self_recovery lifts a pause only "
            "while the risk that paused the agent is below its gates, and a "
            "paused agent cannot write the check-in that would lower it."
        ),
        "why": reason or "No reason is recorded for this pause.",
        "other_exits": (
            f"If a dialectic review was opened for this pause, {get_call} "
            "finds it; answering it is how the pause is reviewed. An operator "
            "can resume the agent. " + re_evaluation
        ),
    }
    state = getattr(meta, "__dict__", None)
    durable_refusal = (
        state.get(_TTL_RECOVERY_REFUSAL_ATTR)
        if isinstance(state, dict)
        else None
    )
    if isinstance(durable_refusal, dict):
        refusal_token = durable_refusal.get("pause_token")
        if (
            getattr(meta, "status", None) != "paused"
            or not isinstance(refusal_token, str)
            or refusal_token != getattr(meta, "paused_at", None)
        ):
            # Tokenless legacy records and records for an older generation are
            # stale. Drop only the object we inspected so a newer record cannot
            # be removed by this read.
            if state.get(_TTL_RECOVERY_REFUSAL_ATTR) is durable_refusal:
                state.pop(_TTL_RECOVERY_REFUSAL_ATTR, None)
            durable_refusal = None
    if isinstance(durable_refusal, dict):
        reason_code = durable_refusal.get("reason") or "unknown"
        explanations = {
            "durable_identity_missing": "the durable identity is missing",
            "operator_reconciliation_required": (
                "the durable status or pause-token copies disagree"
            ),
            "terminal_agent_status": "a durable status is terminal",
        }
        recovery["other_exits"] = (
            "Automatic expiry is blocked because "
            f"{explanations.get(reason_code, 'durable state could not be verified')}. "
            "An operator must inspect and repair the durable identity before this "
            "cache pause can be lifted; cache-only resume is refused."
        )
        recovery["durable_reconciliation"] = {
            "required": True,
            "reason": reason_code,
            "agent_status": durable_refusal.get("agent_status"),
            "identity_status": durable_refusal.get("identity_status"),
        }
    if expires_at:
        recovery["expires_at"] = expires_at
    return recovery


def maybe_auto_expire_pause_sync(agent_uuid: str, meta: Any) -> bool:
    """Sync variant: schedule durable expiry and keep this traversal blocked.

    Returns True if the pause was expired; False if it should still
    block. Callers should check the return value before returning the
    AGENT_PAUSED error.
    """
    if not _pause_is_stale(meta.paused_at):
        return False

    original_paused_at = meta.paused_at
    _schedule_persistence_fire_and_forget(
        agent_uuid, meta, original_paused_at
    )
    # A synchronous caller cannot wait for the status/token transaction, so it
    # must not operate yet. The scheduled task publishes the cache only after
    # commit; the next retry will then return through the ordinary active path.
    return False
