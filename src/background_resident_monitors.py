"""
Resident monitoring, silence detection, pause escalation, and progress-flat probe
background tasks for the governance MCP server.

Extracted from src/background_tasks.py. All public and private symbols are
re-exported on src.background_tasks and dynamically resolved through
src.background_tasks so test monkeypatches continue to work.
"""

import asyncio as _default_asyncio
import sys
from datetime import datetime, timezone

from config.governance_config import (
    LABEL_CHECKIN_INTERVALS as _PERSISTENT_AGENT_INTERVALS,
)
from src.background_task_health import (
    record_failure as _default_record_failure,
    record_success as _default_record_success,
)
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

    aio = _bt_attr("asyncio", _default_asyncio)
    log = _bt_attr("logger", logger)

    if interval_seconds is None:
        override = os.getenv("UNITARES_PROGRESS_FLAT_PROBE_INTERVAL_SECONDS")
        interval_seconds = float(override) if override else 300.0

    # Let the DB pool warm up before the first probe (mirror deep_health_probe pattern).
    await aio.sleep(5.0)

    # Lazy imports to avoid circular import at module load.
    from src.db import get_db
    from src.resident_progress.heartbeat import HeartbeatEvaluator
    from src.resident_progress.probe_task import ProgressFlatProbe
    from src.resident_progress.sentinel_source import SentinelPulseSource
    from src.resident_progress.snapshot_writer import SnapshotWriter
    from src.resident_progress.sources import (
        CheckinSource,
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
                from datetime import datetime as _dt
                from src.audit_log import AuditEntry, audit_logger

                entry = AuditEntry(
                    timestamp=_dt.now().isoformat(),
                    agent_id="progress_flat_probe",
                    event_type=event_type,
                    confidence=1.0,
                    details={"severity": severity, **payload},
                )
                audit_logger._write_entry(entry)
            except Exception as e:
                log.warning("[PROGRESS_FLAT] audit emit failed: %s", e)

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
        "agent_checkins":   CheckinSource(db),
    }
    # Third-party sources for out-of-tree residents. Inert unless the operator
    # installed a distribution declaring the entry-point group; first-party
    # names always win a collision. See src/resident_progress/plugins.py.
    from src.resident_progress.plugins import discover_progress_sources

    plugin_result = discover_progress_sources(db, builtin_names=sources.keys())
    for err in plugin_result.errors:
        log.warning("[PROGRESS_FLAT] source plugin rejected: %s", err)
    if plugin_result.sources:
        log.info(
            "[PROGRESS_FLAT] loaded %d third-party source(s): %s",
            len(plugin_result.sources),
            ", ".join(sorted(plugin_result.sources)),
        )
    sources.update(plugin_result.sources)
    probe = ProgressFlatProbe(
        sources_by_name=sources,
        heartbeat_evaluator=HeartbeatEvaluator(_MetadataStore()),
        writer=SnapshotWriter(db),
        audit_emitter=_AuditEmitter(),
    )
    log.info(
        "[PROGRESS_FLAT] probe started; interval=%ss", interval_seconds,
    )
    while True:
        try:
            await probe.tick()
        except aio.CancelledError:
            log.info("[PROGRESS_FLAT] task cancelled")
            break
        except Exception as e:
            log.warning("[PROGRESS_FLAT] tick failed: %s", e, exc_info=True)
        try:
            await aio.sleep(interval_seconds)
        except aio.CancelledError:
            break


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
    mapping = _bt_attr("CADENCE_FROM_TAG", CADENCE_FROM_TAG)
    for tag in (tags or []):
        interval = mapping.get(tag)
        if interval is not None:
            return interval
    return None


_silence_alerted: set[str] = set()
_silence_critical_alerted: set[str] = set()
_silence_duplicate_warned: set[str] = set()
# Agents whose open auto-filed resident-silence rows have been looked for since
# this process started (see _close_resident_silence_findings). Bounds the KG
# reads to one per agent per outage/restart rather than one per iteration.
_silence_close_checked: set[str] = set()
_silence_server_start: datetime | None = None  # set on first iteration

# Resident-pause escalation. The silence detector above only inspects
# status=="active" agents, so a PAUSED resident is invisible to it — that is
# exactly how a paused Sentinel stayed dark for ~18h. A one-shot lifecycle_paused
# event fires at pause time, but it is not critical-severity and does not
# distinguish a load-bearing resident from a routine ephemeral-agent pause, so it
# scrolls away. This watchdog re-checks the explicit paused state of configured
# residents and escalates (critical → #alerts via the bridge), re-nagging while
# the resident stays paused. Keyed on the explicit status, so it is immune to the
# silence-inference fragilities (proxy-alive suppression, server-restart clock
# reset, duplicate-identity blindness).
_resident_pause_alerted: dict[str, datetime] = {}  # agent_id -> last escalation time
RESIDENT_PAUSE_REALERT_SECONDS = 3600  # re-nag at most hourly while still paused

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

    parse_fn = _bt_attr("_parse_last_update_aware", _parse_last_update_aware)
    by_label: dict[str, tuple[str, object, datetime | None]] = {}
    for agent_id, meta in list(agent_metadata_map.items()):
        if getattr(meta, "status", None) != "active":
            continue
        label = getattr(meta, "label", None)
        if not label or label not in KNOWN_RESIDENT_LABELS:
            continue
        last = parse_fn(getattr(meta, "last_update", None) or "")
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
      1. resident-progress event-driven labels -> no heartbeat interval
      2. ``cadence.*`` tag (generic, label-independent)
      3. Hardcoded label fallback (back-compat; retires with tag migration)
      4. ``embodied`` / ``autonomous`` tag -> 300s default
    """
    label = getattr(meta, "label", None)
    if label:
        try:
            from src.resident_progress.registry import is_event_driven_label

            if is_event_driven_label(label):
                return None
        except Exception:
            pass
    tags = meta.tags or []
    cadence_fn = _bt_attr("cadence_from_tags", cadence_from_tags)
    tagged_cadence = cadence_fn(tags)
    if tagged_cadence is not None:
        return tagged_cadence
    intervals = _bt_attr("_PERSISTENT_AGENT_INTERVALS", _PERSISTENT_AGENT_INTERVALS)
    if label and label in intervals:
        return intervals[label]
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

    proxy_map = _bt_attr("_SILENCE_PROXY_AGENTS", _SILENCE_PROXY_AGENTS)
    proxy_key = proxy_map.get(label)
    if not proxy_key:
        return False
    parse_fn = _bt_attr("_parse_last_update_aware", _parse_last_update_aware)
    # The proxy may be keyed by agent_id or by label depending on how
    # it was registered. Search both.
    for _aid, _meta in agent_metadata.items():
        match = (_aid == proxy_key) or (getattr(_meta, 'label', None) == proxy_key)
        if not match:
            continue
        if _meta.status != "active":
            continue
        proxy_last = parse_fn(_meta.last_update or "")
        if proxy_last is None:
            continue
        if (now - proxy_last).total_seconds() <= threshold_seconds:
            return True
    return False


async def _log_resident_silence_finding(
    agent_id: str, meta, silence_minutes: float, interval: int
) -> None:
    """Best-effort durable KG finding for an agent gone CRITICAL-silent (deduped, open-only).

    The SILENCE probe already detects this correctly — the CRITICAL log line
    and the ``lifecycle_silent_critical`` broadcast above are real — but
    neither reaches anything a human or agent actually reads later; they are
    a log line and a live-only event. This mirrors ``_log_stuck_intervention``
    (``src/mcp_handlers/lifecycle/stuck.py``): the same "server observes an
    agent and files a note about it" shape, deduped the same way (skip if an
    open note with this tag already exists for this agent_id) so a server
    restart mid-outage — which resets the in-memory ``_silence_critical_alerted``
    set — cannot refile the same outage twice.

    Not resident-specific: gated only on this agent having reached the
    CRITICAL branch above (a real label + expected interval), never on a
    resident name (see the fleet-neutrality contract in CLAUDE.md/AGENTS.md).
    """
    log = _bt_attr("logger", logger)
    rec_success = _bt_attr("record_success", _default_record_success)
    rec_failure = _bt_attr("record_failure", _default_record_failure)
    try:
        from src.knowledge_graph import DiscoveryNode, get_knowledge_graph

        graph = await get_knowledge_graph()
        existing = await graph.query(
            status="open", agent_id=agent_id, tags=["resident-silence"], limit=500
        )
        if any("resident-silence" in (d.tags or []) for d in existing):
            return  # already filed and still open

        label = getattr(meta, "label", None) or agent_id[:12]
        tags = ["resident-silence", "auto-filed"]
        if getattr(meta, "label", None):
            tags.append(meta.label.lower())

        note = DiscoveryNode(
            id=datetime.now(timezone.utc).isoformat(),
            agent_id=agent_id,
            type="bug_found",
            summary=(
                f"{label} silent for {silence_minutes:.0f}m "
                f"(expected every {interval // 60}m) — resident-progress SILENCE probe"
            ),
            details=(
                f"background_tasks._silence_check_iteration escalated {label} "
                f"({agent_id}) to CRITICAL: no check-in for {silence_minutes:.0f} minutes "
                f"against an expected {interval // 60}-minute cadence. Filed automatically; "
                "close the loop once it reconnects: knowledge(action='update', "
                "discovery_id=<this id>, status='resolved')."
            ),
            tags=tags,
            severity="critical",
            status="open",
        )
        await graph.add_discovery(note)
        rec_success("silence_finding_write", log=log)
    except Exception as e:
        rec_failure("silence_finding_write", e, log=log)


# Agents whose last close attempt failed. The health record is one task for
# every agent, so a success for one agent must not clear another's failure:
# the task stays failing while any agent here is still pending.
_silence_close_pending: dict[str, str] = {}


def _record_silence_close(agent_id: str, error: "BaseException | str | None") -> None:
    log = _bt_attr("logger", logger)
    rec_success = _bt_attr("record_success", _default_record_success)
    rec_failure = _bt_attr("record_failure", _default_record_failure)
    pending_map = _bt_attr("_silence_close_pending", _silence_close_pending)
    if error is None:
        pending_map.pop(agent_id, None)
    else:
        pending_map[agent_id] = (
            f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
        )
    if not pending_map:
        rec_success("silence_finding_close", log=log)
        return
    # A successful close for another agent is not another failed attempt.
    # Keep the pending failure visible without advancing its streak or time.
    if error is None:
        return
    pending = sorted(pending_map.items())
    rec_failure(
        "silence_finding_close",
        "; ".join(f"{agent}: {reason}" for agent, reason in pending),
        log=log,
    )


async def _close_resident_silence_findings(agent_id: str, reconnected_at: datetime) -> bool:
    """Resolve this agent's open auto-filed resident-silence rows after it is healthy again.

    Closes the loop ``_log_resident_silence_finding`` promises. Touches only
    rows that are open, belong to ``agent_id`` and carry BOTH the
    ``resident-silence`` and ``auto-filed`` tags; anything a human or another
    agent filed is left alone. Idempotent: a resolved row no longer matches the
    open-only query, so a second pass finds nothing.

    closure_class is ``unobserved``, not ``fix_verified`` (nothing was fixed or
    deployed) and not ``not_reproducible`` (the condition was real, it ended):
    the standard is "the symptom stopped appearing, cause unknown". Its contract
    wants ``window`` and ``instrument_check``; both are supplied from what the
    probe actually saw. The check-in recorder is the very field that raised the
    alarm, so it is known to be live.

    Returns True when the lookup completed and every close landed (including
    "nothing to close"), False on a KG error or a rejected update so the caller
    retries on a later iteration.
    """
    rec_close = _bt_attr("_record_silence_close", _record_silence_close)
    try:
        from src.knowledge_graph import get_knowledge_graph

        graph = await get_knowledge_graph()
        existing = await graph.query(
            status="open", agent_id=agent_id, tags=["resident-silence"], limit=500
        )
        rows = [
            d for d in existing
            if {"resident-silence", "auto-filed"} <= set(d.tags or [])
            and getattr(d, "agent_id", agent_id) == agent_id
        ]
        if not rows:
            rec_close(agent_id, None)
            return True
        now = datetime.now(timezone.utc)
        reconnect_iso = reconnected_at.isoformat()
        all_landed = True
        for d in rows:
            note = (
                f"Resolution notes ({now.isoformat()}):\n"
                f"Resident observed healthy again by the silence probe: last check-in "
                f"{reconnect_iso}. Closed automatically; no fix was deployed and the "
                "cause of the silence is not established."
            )
            base = (d.details or "").rstrip()
            landed = await graph.update_discovery(d.id, {
                "status": "resolved",
                "resolved_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "details": f"{base}\n\n{note}" if base else note,
                "closure_class": "unobserved",
                "closure_evidence": {
                    "window": f"{reconnect_iso} to {now.isoformat()}",
                    "instrument_check": (
                        "the silence probe reads this agent's last_update, the same "
                        "field that raised the alarm, and it is advancing again"
                    ),
                },
            })
            if landed is False:
                all_landed = False
        rec_close(
            agent_id,
            None if all_landed else "the knowledge graph rejected a resolve update; retried next pass",
        )
        return all_landed
    except Exception as e:
        rec_close(agent_id, e)
        return False


# Event types that record a silence alert in audit.events.
_SILENCE_CRITICAL_ALERT_EVENTS: tuple[str, ...] = ("lifecycle_silent_critical",)
_SILENCE_ANY_ALERT_EVENTS: tuple[str, ...] = (
    "lifecycle_silent",
    "agent_silent",
    "lifecycle_silent_critical",
)


async def _silence_episode_already_alerted(
    agent_id: str, last_active: datetime, event_types: tuple[str, ...]
) -> bool:
    """True when this silence episode was already alerted, per durable state."""
    log = _bt_attr("logger", logger)
    try:
        from src.audit_db import query_audit_events_async

        rows = await query_audit_events_async(
            agent_id=agent_id,
            event_types=list(event_types),
            start_time=last_active.isoformat(),
            limit=1,
            order="desc",
        )
    except Exception as e:
        log.debug(f"[SILENCE] durable alert lookup failed for {agent_id[:12]}: {e}")
        return False
    return bool(rows)


async def _silence_check_iteration() -> None:
    """Single pass of silence detection. Extracted for testability."""
    global _silence_server_start
    from src.agent_metadata_model import agent_metadata
    from src.audit_db import append_audit_event_async
    from src.broadcaster import broadcaster_instance

    log = _bt_attr("logger", logger)
    rec_success = _bt_attr("record_success", _default_record_success)
    mod = _bt()

    now = datetime.now(timezone.utc)

    # Record server start time on first call.  Only alert about silence
    # that accumulated *while this process was running* — pre-existing
    # staleness from Mac sleep / prior shutdown is not actionable.
    server_start = getattr(mod, "_silence_server_start", _silence_server_start) if mod is not None else _silence_server_start
    if server_start is None:
        server_start = now
        _silence_server_start = now
        if mod is not None:
            mod._silence_server_start = now

    alerted_set = _bt_attr("_silence_alerted", _silence_alerted)
    crit_set = _bt_attr("_silence_critical_alerted", _silence_critical_alerted)
    dup_warned_set = _bt_attr("_silence_duplicate_warned", _silence_duplicate_warned)
    close_checked_set = _bt_attr("_silence_close_checked", _silence_close_checked)
    close_pending_map = _bt_attr("_silence_close_pending", _silence_close_pending)

    canon_fn = _bt_attr("_canonical_active_resident_ids", _canonical_active_resident_ids)
    interval_fn = _bt_attr("_get_expected_interval", _get_expected_interval)
    parse_fn = _bt_attr("_parse_last_update_aware", _parse_last_update_aware)
    proxy_fn = _bt_attr("_proxy_alive", _proxy_alive)
    already_fn = _bt_attr("_silence_episode_already_alerted", _silence_episode_already_alerted)
    log_finding_fn = _bt_attr("_log_resident_silence_finding", _log_resident_silence_finding)
    close_finding_fn = _bt_attr("_close_resident_silence_findings", _close_resident_silence_findings)

    canonical_residents = canon_fn(agent_metadata)

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
                if agent_id not in dup_warned_set:
                    dup_warned_set.add(agent_id)
                    log.warning(
                        f"[SILENCE] Skipping duplicate resident row {label} "
                        f"({agent_id[:8]}...) for silence detection; canonical "
                        "active row exists"
                    )
                continue
        interval = interval_fn(meta)
        if interval is None:
            continue
        if not meta.last_update:
            continue

        last = parse_fn(meta.last_update)
        if last is None:
            continue

        # Cap silence to time since this server started — don't alert
        # for gaps that occurred before we were running.
        effective_last = max(last, server_start)
        silence_seconds = (now - effective_last).total_seconds()

        silence_minutes = silence_seconds / 60

        if silence_seconds >= interval * 5 and agent_id not in crit_set:
            if proxy_fn(meta.label, now, threshold_seconds=interval * 2):
                if agent_id not in alerted_set:
                    alerted_set.add(agent_id)
                    if await already_fn(
                        agent_id, last, _SILENCE_ANY_ALERT_EVENTS
                    ):
                        continue
                    log.warning(
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
            elif await already_fn(
                agent_id, last, _SILENCE_CRITICAL_ALERT_EVENTS
            ):
                crit_set.add(agent_id)
                alerted_set.add(agent_id)
                log.info(
                    f"[SILENCE] {meta.label or agent_id[:12]} still silent; CRITICAL "
                    "already recorded for this episode, not re-alerting"
                )
                await log_finding_fn(agent_id, meta, silence_minutes, interval)
            else:
                crit_set.add(agent_id)
                close_checked_set.discard(agent_id)
                alerted_set.add(agent_id)
                log.error(
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
                await log_finding_fn(agent_id, meta, silence_minutes, interval)
        elif silence_seconds >= interval * 2 and agent_id not in alerted_set:
            alerted_set.add(agent_id)
            if await already_fn(
                agent_id, last, _SILENCE_ANY_ALERT_EVENTS
            ):
                continue
            log.warning(
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
            alerted_set.discard(agent_id)
            was_critical = agent_id in crit_set
            crit_set.discard(agent_id)
            really_fresh = (now - last).total_seconds() < interval * 2
            if really_fresh and (was_critical or agent_id not in close_checked_set):
                if await close_finding_fn(agent_id, last):
                    close_checked_set.add(agent_id)

    # Prune alert sets — remove agents no longer active
    active_ids = {aid for aid, m in agent_metadata.items() if m.status == "active"}
    alerted_set.intersection_update(active_ids)
    crit_set.intersection_update(active_ids)
    dup_warned_set.intersection_update(active_ids)
    close_checked_set.intersection_update(active_ids)
    inactive_pending = set(close_pending_map) - active_ids
    for agent_id in sorted(inactive_pending):
        close_pending_map.pop(agent_id)
        log.warning(
            "[SILENCE] stopped close retries for inactive agent %s; "
            "its finding remains unresolved", agent_id,
        )
    if inactive_pending and not close_pending_map:
        rec_success("silence_finding_close", log=log)


async def _resident_pause_check_iteration() -> None:
    """Escalate configured residents stuck in a paused (circuit-breaker) state."""
    from src.agent_metadata_model import agent_metadata
    from src.audit_db import append_audit_event_async
    from src.broadcaster import broadcaster_instance

    try:
        from src.grounding.class_indicator import KNOWN_RESIDENT_LABELS
    except Exception:
        KNOWN_RESIDENT_LABELS = frozenset()

    log = _bt_attr("logger", logger)
    pause_alerted = _bt_attr("_resident_pause_alerted", _resident_pause_alerted)
    realert_s = _bt_attr("RESIDENT_PAUSE_REALERT_SECONDS", RESIDENT_PAUSE_REALERT_SECONDS)
    parse_fn = _bt_attr("_parse_last_update_aware", _parse_last_update_aware)

    now = datetime.now(timezone.utc)
    paused_resident_ids: set[str] = set()

    for agent_id, meta in list(agent_metadata.items()):
        if getattr(meta, "status", None) != "paused":
            continue
        label = getattr(meta, "label", None)
        if not label or label not in KNOWN_RESIDENT_LABELS:
            continue

        paused_resident_ids.add(agent_id)

        last_alert = pause_alerted.get(agent_id)
        if last_alert is not None and (now - last_alert).total_seconds() < realert_s:
            continue  # already escalated recently — don't spam

        pause_alerted[agent_id] = now

        paused_at_dt = parse_fn(getattr(meta, "paused_at", None) or "")
        paused_minutes = (now - paused_at_dt).total_seconds() / 60 if paused_at_dt else None
        dur = f"{paused_minutes:.0f}m" if paused_minutes is not None else "unknown duration"

        log.error(
            f"[RESIDENT_PAUSE] CRITICAL: resident {label} ({agent_id[:12]}) paused for {dur} "
            "— dark to governance until recovered; escalating to operator."
        )
        await broadcaster_instance.broadcast_event(
            "lifecycle_resident_paused",
            agent_id=agent_id,
            payload={
                "severity": "critical",
                "label": label,
                "paused_at": getattr(meta, "paused_at", None),
                "paused_minutes": round(paused_minutes, 1) if paused_minutes is not None else None,
                "note": (
                    "Configured resident is paused and dark to governance. "
                    "Operator action: review and resume."
                ),
            },
        )
        await append_audit_event_async({
            "timestamp": now.isoformat(),
            "event_type": "resident_paused_escalation",
            "agent_id": agent_id,
            "details": {
                "label": label,
                "paused_minutes": round(paused_minutes, 1) if paused_minutes is not None else None,
            },
        })

    # Re-arm: forget residents that are no longer paused so a future pause
    # escalates immediately rather than waiting out the re-nag window.
    for aid in [aid for aid in pause_alerted if aid not in paused_resident_ids]:
        del pause_alerted[aid]


async def check_agent_silence():
    """Detect persistent agents that have missed expected check-ins."""
    aio = _bt_attr("asyncio", _default_asyncio)
    await aio.sleep(120)  # startup delay
    while True:
        log = _bt_attr("logger", logger)
        rec_success = _bt_attr("record_success", _default_record_success)
        rec_failure = _bt_attr("record_failure", _default_record_failure)
        silence_iter = _bt_attr("_silence_check_iteration", _silence_check_iteration)
        pause_iter = _bt_attr("_resident_pause_check_iteration", _resident_pause_check_iteration)
        try:
            await silence_iter()
            rec_success("silence_check", log=log)
        except Exception as e:
            rec_failure("silence_check", e, log=log)

        try:
            await pause_iter()
            rec_success("resident_pause_check", log=log)
        except Exception as e:
            rec_failure("resident_pause_check", e, log=log)

        await aio.sleep(600)  # every 10 minutes
