"""
Agent lifecycle management.

Monitor creation, agent archival, standardized info building.
"""

from __future__ import annotations

import re
from datetime import datetime

from src.logging_utils import get_logger
from src.agent_metadata_model import AgentMetadata, agent_metadata
from src.agent_monitor_state import monitors, load_monitor_state
from src.agent_metadata_persistence import get_or_create_metadata
from src.governance_monitor import UNITARESMonitor

logger = get_logger(__name__)


def get_or_create_monitor(agent_id: str) -> UNITARESMonitor:
    """Get existing monitor or create new one with metadata, loading state if it exists.

    Sync by design — async-cascading would force ~28 callers (background tasks,
    CLI, sync handler internals) to become async. When the file snapshot is
    missing but metadata indicates prior activity (`total_updates > 0`), the
    monitor is marked `_needs_hydration=True`; async handlers drain that mark
    via `ensure_hydrated(monitor, agent_id)` at their entry point. Sync callers
    skip hydration and see the same seed-default behavior they always have.
    """
    meta = get_or_create_metadata(agent_id)

    if agent_id not in monitors:
        monitor = UNITARESMonitor(agent_id)

        persisted_state = load_monitor_state(agent_id)
        if persisted_state is not None:
            monitor.state = persisted_state
            monitor._needs_hydration = False
            logger.info(f"Loaded persisted state for {agent_id} ({len(persisted_state.V_history)} history entries)")
        else:
            # File snapshot missing. Mark for DB hydration only if metadata says
            # we've seen prior activity — fresh-onboard agents stay update_count=0
            # so downstream "no measured trajectory" guards still trigger.
            # Lineage is recorded via meta.parent_agent_id but never transplants
            # state; see docs/specs/2026-04-16-sever-fingerprint-eisv-inheritance-design.md
            prior_activity = int(getattr(meta, "total_updates", 0) or 0) > 0
            monitor._needs_hydration = prior_activity
            if prior_activity:
                logger.info(f"Initialized monitor for {agent_id} (file missing, marked for DB hydration)")
            else:
                logger.info(f"Initialized new monitor for {agent_id}")

        monitors[agent_id] = monitor

    return monitors[agent_id]


def _agent_age_hours(meta: AgentMetadata) -> float | None:
    """Return hours since last activity, or None if unparseable.

    Timestamps without tzinfo are interpreted as UTC (the format UNITARES
    emits everywhere). This avoids mixing naive-local `datetime.now()` with
    a naive-UTC stored timestamp, which would off-by-local-offset the age.
    """
    from datetime import timezone
    try:
        last_update_str = meta.last_update or meta.created_at
        last_update_dt = datetime.fromisoformat(
            last_update_str.replace('Z', '+00:00') if 'Z' in last_update_str else last_update_str
        )
        if last_update_dt.tzinfo is None:
            last_update_dt = last_update_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - last_update_dt).total_seconds() / 3600
    except (ValueError, TypeError, AttributeError):
        return None


def _agent_update_count(meta: AgentMetadata) -> int:
    """Return total_updates as int, defaulting to 0."""
    try:
        return int(getattr(meta, 'total_updates', 0) or 0)
    except (TypeError, ValueError):
        return 0


_UUID_PATTERN = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I,
)
_EPHEMERAL_LABEL = re.compile(r'^claude_\w+_\d{8}', re.I)
_PROTECTED_TIERS = frozenset({"verified", "established", "trusted"})
_SYSTEM_AGENT_IDS: frozenset[str] = frozenset()


def is_agent_protected(agent_id: str, meta: AgentMetadata) -> bool:
    """Return True if the agent should never be auto-archived."""
    if agent_id in _SYSTEM_AGENT_IDS:
        return True
    tags = meta.tags or []
    if "pioneer" in tags:
        return True
    if "persistent" in tags or "protected" in tags:
        return True
    # Back-compat: ``Lumen`` label as protection marker, pending migration to
    # the generic ``persistent`` / ``protected`` tags. Drop once tagged.
    label = getattr(meta, 'label', None) or getattr(meta, 'display_name', None) or ""
    if label == "Lumen":
        return True
    if getattr(meta, 'trust_tier', None) in _PROTECTED_TIERS:
        return True
    return False


def classify_for_archival(
    agent_id: str,
    meta: AgentMetadata,
    *,
    low_update_hours: float = 3.0,
    unlabeled_hours: float = 6.0,
    ephemeral_hours: float = 6.0,
    ephemeral_max_updates: int = 5,
) -> tuple[bool, str]:
    """Decide whether an agent should be archived and why.

    Agents that have never checked in (``updates == 0``) are treated as
    *initializing*, not orphaned — they stay visible as ghosts rather than
    being swept under the rug, which used to hide onboarding bugs behind
    an aggressive 1h UUID auto-archive.
    """
    label = getattr(meta, 'label', None) or getattr(meta, 'display_name', None) or ""
    has_label = bool(label)
    is_uuid_named = bool(_UUID_PATTERN.match(agent_id))
    is_ephemeral = bool(_EPHEMERAL_LABEL.match(label))

    age_hours = _agent_age_hours(meta)
    if age_hours is None:
        return False, ""
    updates = _agent_update_count(meta)

    # Initializing agents (never checked in) are ghosts, not orphans: visible,
    # never auto-archived. Manual `archive_agent` still works if operators want
    # them gone.
    if updates == 0:
        return False, ""

    if not has_label and updates <= 1 and age_hours >= low_update_hours:
        return True, f"unlabeled agent, {updates} updates, {age_hours:.1f}h old"
    if is_uuid_named and not has_label and updates >= 2 and age_hours >= unlabeled_hours:
        return True, f"stale UUID agent, {updates} updates, {age_hours:.1f}h old"
    if is_ephemeral and updates <= ephemeral_max_updates and age_hours >= ephemeral_hours:
        return True, f"ephemeral session agent '{label}', {updates} updates, {age_hours:.1f}h old"
    return False, ""


async def auto_archive_orphan_agents(
    low_update_hours: float = 3.0,
    unlabeled_hours: float = 6.0,
    ephemeral_hours: float = 6.0,
    ephemeral_max_updates: int = 5,
    dry_run: bool = False,
    *,
    zero_update_hours: float | None = None,  # deprecated no-op, see classify_for_archival
    _metadata: dict | None = None,
    _monitors: dict | None = None,
) -> list[dict]:
    """
    Archive orphan and ephemeral agents to prevent proliferation.

    Delegates classification to ``classify_for_archival`` and protection
    checks to ``is_agent_protected``. Archival execution uses the
    persist-first ``_archive_one_agent`` helper.

    Args:
        zero_update_hours: Accepted for back-compat with callers that still
            pass it; ignored. Initializing agents (0 updates) are never
            auto-archived — see ``classify_for_archival``.
        _metadata: Override the agent_metadata dict (used by handler wrappers
            that need to iterate mcp_server.agent_metadata instead).

    Returns:
        List of dicts describing each archived (or would-archive) agent.
    """
    from src.mcp_handlers.lifecycle.helpers import _archive_one_agent
    del zero_update_hours  # accepted for back-compat, no longer used

    source = _metadata if _metadata is not None else agent_metadata
    mon = _monitors if _monitors is not None else monitors
    results: list[dict] = []

    for agent_id, meta in list(source.items()):
        if meta.status in ("archived", "deleted"):
            continue
        if is_agent_protected(agent_id, meta):
            continue

        should, reason = classify_for_archival(
            agent_id, meta,
            low_update_hours=low_update_hours,
            unlabeled_hours=unlabeled_hours,
            ephemeral_hours=ephemeral_hours,
            ephemeral_max_updates=ephemeral_max_updates,
        )
        if not should:
            continue

        if not dry_run:
            ok = await _archive_one_agent(
                agent_id, meta, f"Auto-archived: {reason}",
                monitors=mon,
            )
            if not ok:
                continue
            try:
                from src.sequential_calibration import get_sequential_calibration_tracker
                get_sequential_calibration_tracker().drop_agent_state(agent_id)
            except Exception as exc:
                logger.warning(
                    "Archived orphan agent %s, but failed to prune sequential "
                    "calibration state: %s",
                    agent_id[:12],
                    exc,
                )
            logger.info(f"Auto-archived orphan agent: {agent_id[:12]}... ({reason})")

        results.append({
            "id": agent_id,
            "reason": reason,
            "updates": _agent_update_count(meta),
            "label": getattr(meta, 'label', None),
        })

    return results


def get_agent_or_error(agent_id: str) -> tuple[UNITARESMonitor | None, str | None]:
    """Get agent with friendly error message if not found"""
    if agent_id not in monitors:
        available = list(monitors.keys())
        if available:
            error = f"Agent '{agent_id}' not found. Available agents: {available}. Call process_agent_update first to initialize."
        else:
            error = f"Agent '{agent_id}' not found. No agents initialized yet. Call process_agent_update first."
        return None, error
    return monitors[agent_id], None


def build_standardized_agent_info(
    agent_id: str,
    meta: AgentMetadata,
    monitor: UNITARESMonitor | None = None,
    include_metrics: bool = True
) -> dict:
    """
    Build standardized agent info structure.
    Always returns same fields, null if unavailable.
    """
    from src.agent_process_mgmt import health_checker

    if monitor:
        if hasattr(monitor, 'created_at') and monitor.created_at:
            created_ts = monitor.created_at.isoformat()
        else:
            created_ts = meta.created_at

        if hasattr(monitor, 'last_update') and monitor.last_update:
            last_update_ts = monitor.last_update.isoformat()
        else:
            last_update_ts = meta.last_update

        update_count = meta.total_updates
    else:
        created_ts = meta.created_at
        last_update_ts = meta.last_update
        update_count = meta.total_updates

    try:
        from datetime import timezone
        created_dt = datetime.fromisoformat(created_ts.replace('Z', '+00:00') if 'Z' in created_ts else created_ts)
        if created_dt.tzinfo is None:
            # Naive stored timestamps are UTC (see _agent_age_hours).
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - created_dt).days
    except (ValueError, TypeError, AttributeError):
        age_days = None

    primary_tags = (meta.tags or [])[:3] if meta.tags else []

    notes_preview = None
    if meta.notes:
        notes_preview = meta.notes[:100] + "..." if len(meta.notes) > 100 else meta.notes

    summary = {
        "updates": update_count,
        "last_activity": last_update_ts,
        "age_days": age_days,
        "primary_tags": primary_tags
    }

    metrics = None
    health_status = "unknown"
    state_info = {
        "loaded_in_process": monitor is not None,
        "metrics_available": False,
        "error": None
    }

    if monitor and include_metrics:
        try:
            monitor_state = monitor.state
            risk_score = getattr(monitor_state, 'risk_score', None)
            health_status_obj, _ = health_checker.get_health_status(
                risk_score=risk_score,
                coherence=monitor_state.coherence,
                void_active=monitor_state.void_active
            )
            health_status = health_status_obj.value

            monitor_metrics = monitor.get_metrics() if hasattr(monitor, 'get_metrics') else {}
            risk_score_value = monitor_metrics.get("risk_score") or risk_score

            metrics = {
                "risk_score": float(risk_score_value) if risk_score_value is not None else None,
                "phi": monitor_metrics.get("phi"),
                "verdict": monitor_metrics.get("verdict"),
                "coherence": float(monitor_state.coherence),
                "void_active": bool(monitor_state.void_active),
                "E": float(monitor_state.E),
                "I": float(monitor_state.I),
                "S": float(monitor_state.S),
                "V": float(monitor_state.V),
                "lambda1": float(monitor_state.lambda1)
            }
            state_info["metrics_available"] = True
        except Exception as e:
            health_status = "error"
            state_info["error"] = str(e)
            state_info["metrics_available"] = False

    lineage_info = None
    if meta.parent_agent_id:
        lineage_info = {
            "parent_agent_id": meta.parent_agent_id,
            "creation_reason": meta.spawn_reason or "created",
            "has_lineage": True
        }
        if meta.parent_agent_id in agent_metadata:
            parent_meta = agent_metadata[meta.parent_agent_id]
            lineage_info["parent_status"] = parent_meta.status
        else:
            lineage_info["parent_status"] = "deleted"

    return {
        "agent_id": agent_id,
        "lifecycle_status": meta.status,
        "health_status": health_status,
        "summary": summary,
        "metrics": metrics,
        "metadata": {
            "created": created_ts,
            "last_update": last_update_ts,
            "version": meta.version,
            "total_updates": meta.total_updates,
            "tags": meta.tags or [],
            "notes_preview": notes_preview,
            "lineage_info": lineage_info
        },
        "state": state_info
    }
