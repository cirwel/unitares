"""
Lifecycle query handlers — read-only agent listing and metadata retrieval.

Extracted from handlers.py for maintainability.
"""

import hashlib
import uuid
from typing import Any, Optional, Sequence
from mcp.types import TextContent
from datetime import datetime, timedelta, timezone

from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
from ..types import ToolArgumentsDict
from ..utils import (
    require_registered_agent,
    success_response,
    error_response,
)
from ..error_helpers import (
    system_error as system_error_helper,
)
from ..decorators import mcp_tool
from ..support.coerce import safe_float
from ..support.naming_helpers import disambiguate_public_handle
from src.logging_utils import get_logger
from src.agent_monitor_state import ensure_hydrated

from .helpers import _is_test_agent

logger = get_logger(__name__)


def _is_uuid_like_agent_id(value: Any) -> bool:
    """Return True for identifiers that could be used as UUID credentials."""
    if not isinstance(value, str) or not value:
        return False
    try:
        uuid.UUID(value)
        return True
    except (TypeError, ValueError, AttributeError):
        pass

    if value.startswith("agent-"):
        suffix = value[6:]
        return (
            len(suffix) >= 12
            and all(ch in "0123456789abcdefABCDEF" for ch in suffix[:12])
        )
    return False


def _public_agent_identifier(agent_id: str, meta: Any) -> str:
    # Disambiguate the bucket-form public_agent_id ({Model}_{date}) with the
    # agent's uuid8 fragment so distinct agents minted same-model/same-day do
    # not render as identical "duplicate" rows. agent_id is the registry UUID
    # in the primary call path, so it backstops a missing meta.agent_uuid.
    handle = disambiguate_public_handle(
        getattr(meta, "public_agent_id", None),
        getattr(meta, "structured_id", None),
        getattr(meta, "agent_uuid", None) or agent_id,
    )
    if handle:
        return handle
    for attr in ("label", "display_name"):
        value = getattr(meta, attr, None)
        if value:
            return str(value)
    digest = hashlib.sha256(str(agent_id).encode("utf-8")).hexdigest()[:12]
    return f"agent-redacted-{digest}"


def _can_disclose_agent_uuid(
    agent_id: Optional[str],
    *,
    caller_uuid: Optional[str],
    operator_caller: bool,
) -> bool:
    if not agent_id:
        return True
    if operator_caller or agent_id == caller_uuid:
        return True
    return not _is_uuid_like_agent_id(agent_id)


def _visible_agent_identifier(
    agent_id: str,
    meta: Any,
    *,
    caller_uuid: Optional[str],
    operator_caller: bool,
) -> tuple[str, bool]:
    if _can_disclose_agent_uuid(
        agent_id,
        caller_uuid=caller_uuid,
        operator_caller=operator_caller,
    ):
        return agent_id, False
    return _public_agent_identifier(agent_id, meta), True


def _visible_related_agent_identifier(
    agent_id: Optional[str],
    *,
    caller_uuid: Optional[str],
    operator_caller: bool,
) -> tuple[Optional[str], bool]:
    if not agent_id:
        return None, False
    if _can_disclose_agent_uuid(
        agent_id,
        caller_uuid=caller_uuid,
        operator_caller=operator_caller,
    ):
        return agent_id, False

    related_meta = mcp_server.agent_metadata.get(agent_id)
    if related_meta:
        return _public_agent_identifier(agent_id, related_meta), True

    digest = hashlib.sha256(str(agent_id).encode("utf-8")).hexdigest()[:12]
    return f"agent-redacted-{digest}", True


def _latest_lifecycle_event(meta: Any) -> Optional[dict[str, Any]]:
    events = getattr(meta, "lifecycle_events", None) or []
    for event in reversed(events):
        if isinstance(event, dict):
            return event
    return None


def _is_lineage_supersession(event: Optional[dict[str, Any]]) -> bool:
    if not event:
        return False
    return (
        event.get("event") == "archived"
        and event.get("reason") == "lineage_succession"
    )


def _compact_lifecycle_fields(meta: Any) -> dict[str, Any]:
    event = _latest_lifecycle_event(meta)
    if not event:
        return {}

    fields = {
        "last_lifecycle_event": event.get("event"),
        "last_lifecycle_reason": event.get("reason"),
    }
    timestamp = event.get("timestamp") or event.get("ts")
    if timestamp:
        fields["last_lifecycle_at"] = timestamp

    if _is_lineage_supersession(event):
        fields["superseded"] = True
        fields["superseded_reason"] = event.get("reason")

    return fields


def _identity_view(
    agent_id: str,
    meta: Any,
    *,
    caller_uuid: Optional[str],
    operator_caller: bool,
) -> dict[str, Any]:
    visible_id, id_redacted = _visible_agent_identifier(
        agent_id,
        meta,
        caller_uuid=caller_uuid,
        operator_caller=operator_caller,
    )
    parent_agent_id = getattr(meta, "parent_agent_id", None)
    visible_parent_id, parent_redacted = _visible_related_agent_identifier(
        parent_agent_id,
        caller_uuid=caller_uuid,
        operator_caller=operator_caller,
    )
    lifecycle_event = _latest_lifecycle_event(meta)
    lifecycle_timestamp = None
    if lifecycle_event:
        lifecycle_timestamp = lifecycle_event.get("timestamp") or lifecycle_event.get("ts")

    current = {
        "id": visible_id,
        "id_redacted": id_redacted,
        "label": getattr(meta, "label", None),
        "display_name": getattr(meta, "display_name", None),
        "public_handle": disambiguate_public_handle(
            getattr(meta, "public_agent_id", None),
            getattr(meta, "structured_id", None),
            getattr(meta, "agent_uuid", None) or agent_id,
        ),
    }
    if (
        (operator_caller or agent_id == caller_uuid)
        and getattr(meta, "active_session_key", None)
    ):
        current["session_key"] = getattr(meta, "active_session_key")

    superseded = _is_lineage_supersession(lifecycle_event)
    lifecycle = {
        "status": getattr(meta, "status", None),
        "latest_event": lifecycle_event.get("event") if lifecycle_event else None,
        "latest_reason": lifecycle_event.get("reason") if lifecycle_event else None,
        "latest_at": lifecycle_timestamp,
        "superseded": superseded,
    }
    if superseded:
        lifecycle["superseded_reason"] = lifecycle_event.get("reason")

    return {
        "schema_version": "identity_view.v1",
        "current": current,
        "lineage": {
            "parent_agent_id": visible_parent_id,
            "parent_agent_id_redacted": parent_redacted,
            "spawn_reason": getattr(meta, "spawn_reason", None),
            "lineage_state": "declared" if parent_agent_id else "no_lineage_declared",
            "lineage_state_source": "derived",
        },
        "lifecycle": lifecycle,
    }


def _metadata_dict_for_response(
    agent_id: str,
    meta: Any,
    *,
    caller_uuid: Optional[str],
    operator_caller: bool,
) -> dict[str, Any]:
    metadata = dict(meta.to_dict())
    sensitive_identity_allowed = operator_caller or agent_id == caller_uuid

    visible_agent_id, uuid_redacted = _visible_agent_identifier(
        agent_id,
        meta,
        caller_uuid=caller_uuid,
        operator_caller=operator_caller,
    )
    if "agent_id" in metadata or uuid_redacted:
        metadata["agent_id"] = visible_agent_id
    if uuid_redacted:
        metadata["agent_id_redacted"] = True
        if "agent_uuid" in metadata:
            metadata.pop("agent_uuid", None)
            metadata["agent_uuid_redacted"] = True

    parent_agent_id = getattr(meta, "parent_agent_id", None) or metadata.get("parent_agent_id")
    visible_parent_id, parent_redacted = _visible_related_agent_identifier(
        parent_agent_id,
        caller_uuid=caller_uuid,
        operator_caller=operator_caller,
    )
    if parent_agent_id or "parent_agent_id" in metadata:
        metadata["parent_agent_id"] = visible_parent_id
    if parent_redacted:
        metadata["parent_agent_id_redacted"] = True

    if not sensitive_identity_allowed:
        if metadata.get("active_session_key"):
            metadata["active_session_key_redacted"] = True
        metadata.pop("active_session_key", None)
        if metadata.get("api_key"):
            metadata["api_key_redacted"] = True
        metadata.pop("api_key", None)

    return metadata


def _context_agent_id() -> Optional[str]:
    try:
        from ..context import get_context_agent_id
        return get_context_agent_id()
    except Exception:
        return None


def _is_operator_request() -> bool:
    try:
        from src.mcp_handlers.identity.operator import is_operator_caller
        return is_operator_caller()
    except Exception:
        return False


@mcp_tool("list_agents", timeout=15.0, register=False)
async def handle_list_agents(arguments: ToolArgumentsDict) -> Sequence[TextContent]:
    """List all agents currently being monitored with lifecycle metadata and health status

    LITE MODE: Use lite=true for minimal response (~1KB vs ~15KB)
    """
    try:
        # In-memory metadata is kept current by process_agent_update/onboard.
        # A forced full DB reload here caused 14s+ timeouts and ClosedResourceError crashes.
        # Use what's in memory — it's already fresh enough for listing.

        # LITE MODE: Minimal response for local/smaller models (DEFAULT)
        lite_explicit = "lite" in arguments
        lite_mode = arguments.get("lite", True)
        # If caller is asking for non-lite behavior (metrics/pagination/filters), honor it
        # even if they didn't explicitly set lite=false.
        if not lite_explicit:
            if arguments.get("include_metrics") is True:
                lite_mode = False
            elif arguments.get("limit") is not None or arguments.get("offset") is not None:
                lite_mode = False
            elif arguments.get("status_filter") not in (None, "active"):
                lite_mode = False
            elif arguments.get("include_test_agents") is True:
                lite_mode = False
            elif arguments.get("summary_only") is True or arguments.get("grouped") is False:
                lite_mode = False
        caller_uuid = _context_agent_id()
        operator_caller = _is_operator_request()
        if lite_mode:
            # Ultra-compact response - only real agents
            limit = arguments.get("limit", 20)
            status_filter = arguments.get("status_filter", "active")
            include_test_agents = arguments.get("include_test_agents", False)
            # Default: include zero-update agents so newly created agents are discoverable.
            # Callers can still pass min_updates=1 to hide ghost agents.
            min_updates = arguments.get("min_updates", 0)
            # Smart default: show labeled agents first; if none, show active unlabeled ones
            named_only = arguments.get("named_only")  # None = auto, True/False = explicit
            # NEW: Filter by recency - default 7 days to reduce noise from stale agents
            recent_days = arguments.get("recent_days", 7)

            # Calculate cutoff time for recency filter
            cutoff_time = None
            if recent_days and recent_days > 0:
                cutoff_time = datetime.now(timezone.utc) - timedelta(days=recent_days)

            agents = []
            total_all = 0  # Count all agents before filtering
            ghost_count = 0
            ephemeral_count = 0
            test_count = 0
            for agent_id, meta in list(mcp_server.agent_metadata.items()):
                total_all += 1
                # Ghost: no label, no purpose, zero check-ins — session-binding artifact
                has_label = bool(getattr(meta, 'label', None))
                has_purpose = bool(getattr(meta, 'purpose', None))
                is_ghost = (
                    meta.total_updates < 1
                    and not has_label
                    and not has_purpose
                )
                if is_ghost:
                    ghost_count += 1
                elif _is_test_agent(agent_id, getattr(meta, 'label', None)):
                    # Test agents (pytest/itest suites) get their own health
                    # bucket — they carry labels, so without this they'd
                    # inflate "real". Buckets are mutually exclusive; ghost
                    # classification wins for unlabeled test-pattern ids.
                    test_count += 1
                elif meta.total_updates <= 2 and not has_label:
                    # Short-lived but did some work — legitimate ephemeral
                    ephemeral_count += 1

                if status_filter != "all" and meta.status != status_filter:
                    continue
                if min_updates and meta.total_updates < min_updates:
                    continue
                if not include_test_agents and _is_test_agent(agent_id, getattr(meta, 'label', None)):
                    continue
                if named_only is True and not getattr(meta, 'label', None):
                    continue
                if named_only is None:
                    # Auto mode: skip ghost agents (lazy_creation + no updates + no label)
                    if is_ghost:
                        continue

                # Apply recency filter
                if cutoff_time and meta.last_update:
                    try:
                        last_dt = datetime.fromisoformat(meta.last_update.replace('Z', '+00:00'))
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                        if last_dt < cutoff_time:
                            continue  # Skip stale agents
                    except Exception:
                        pass  # Keep agents with unparseable dates

                label = getattr(meta, 'label', None)
                public_id = disambiguate_public_handle(
                    getattr(meta, 'public_agent_id', None),
                    getattr(meta, 'structured_id', None),
                    getattr(meta, 'agent_uuid', None) or agent_id,
                )
                from src.resident_progress.registry import is_event_driven_label
                visible_id, uuid_redacted = _visible_agent_identifier(
                    agent_id,
                    meta,
                    caller_uuid=caller_uuid,
                    operator_caller=operator_caller,
                )
                parent_id, parent_redacted = _visible_related_agent_identifier(
                    getattr(meta, 'parent_agent_id', None),
                    caller_uuid=caller_uuid,
                    operator_caller=operator_caller,
                )
                agent_entry = {
                    "id": visible_id,
                    # display_name (user-chosen) takes precedence; agent_id is fallback
                    "label": label or public_id,
                    "status": meta.status,
                    "purpose": getattr(meta, 'purpose', None),
                    "updates": meta.total_updates,
                    "last": meta.last_update[:10] if meta.last_update else None,
                    "last_update": meta.last_update,
                    "trust_tier": getattr(meta, 'trust_tier', None),
                    "parent_agent_id": parent_id,
                    "spawn_reason": getattr(meta, 'spawn_reason', None),
                    "event_driven": is_event_driven_label(label),
                }
                agent_entry.update(_compact_lifecycle_fields(meta))
                if uuid_redacted:
                    agent_entry["uuid_redacted"] = True
                if parent_redacted:
                    agent_entry["parent_agent_id_redacted"] = True
                agents.append(agent_entry)
            # Sort: labeled first, then by most recent activity
            agents.sort(key=lambda x: (0 if x.get("label") else 1, -(x.get("updates") or 0), x.get("last_update", "") or ""), reverse=False)

            # Always include the requesting agent even if filtered out
            caller_in_list = caller_uuid and any(a["id"] == caller_uuid for a in agents)
            if caller_uuid and not caller_in_list:
                caller_meta = mcp_server.agent_metadata.get(caller_uuid)
                if caller_meta:
                    caller_label = getattr(caller_meta, 'label', None)
                    caller_public = disambiguate_public_handle(
                        getattr(caller_meta, 'public_agent_id', None),
                        getattr(caller_meta, 'structured_id', None),
                        getattr(caller_meta, 'agent_uuid', None) or caller_uuid,
                    )
                    parent_id, parent_redacted = _visible_related_agent_identifier(
                        getattr(caller_meta, 'parent_agent_id', None),
                        caller_uuid=caller_uuid,
                        operator_caller=operator_caller,
                    )
                    caller_entry = {
                        "id": caller_uuid,
                        "label": caller_label or caller_public,
                        "status": caller_meta.status,
                        "purpose": getattr(caller_meta, 'purpose', None),
                        "updates": caller_meta.total_updates,
                        "last": caller_meta.last_update[:10] if caller_meta.last_update else None,
                        "last_update": caller_meta.last_update,
                        "trust_tier": getattr(caller_meta, 'trust_tier', None),
                        "parent_agent_id": parent_id,
                        "spawn_reason": getattr(caller_meta, 'spawn_reason', None),
                        "you": True,
                    }
                    caller_entry.update(_compact_lifecycle_fields(caller_meta))
                    agents.append(caller_entry)
                    if parent_redacted:
                        agents[-1]["parent_agent_id_redacted"] = True

            for a in agents:
                # Mark the requesting agent
                if caller_uuid and a["id"] == caller_uuid and "you" not in a:
                    a["you"] = True
                a.pop("last_update", None)

            result = {
                "agents": agents[: max(0, int(limit))] if limit is not None else agents,
                "shown": min(len(agents), int(limit)) if limit else len(agents),
                "matching": len(agents),  # How many matched filters
                "total_all": total_all,  # Total agents in system
                "identity_health": {
                    "ghosts": ghost_count,
                    "ephemeral": ephemeral_count,
                    "test": test_count,
                    "real": total_all - ghost_count - test_count,
                },
            }

            # Add helpful hints. `limit` is None when the caller omits it and the
            # Pydantic layer injects the null default (the lite-path default of 20
            # only applies when the key is absent, not when it arrives as None), so
            # guard int(limit) like the slices above (:470/:471) or the whole list
            # call crashes with int(NoneType) — the dashboard's read sweep hit this.
            if limit and len(agents) > int(limit):
                result["more"] = f"Showing {limit} of {len(agents)} recent. Use limit=50 or recent_days=30 to see more."
            if recent_days:
                result["filter"] = f"Active in last {recent_days} days. Use recent_days=0 for all."

            return success_response(result)

        grouped = arguments.get("grouped", True)
        include_metrics = arguments.get("include_metrics", True)
        status_filter = arguments.get("status_filter", "active")  # Changed: default to active only
        loaded_only = arguments.get("loaded_only", False)
        summary_only = arguments.get("summary_only", False)
        standardized = arguments.get("standardized", True)
        include_test_agents = arguments.get("include_test_agents", False)  # Default: filter out test agents
        # Default: include zero-update agents so newly created agents are discoverable.
        # Callers can still pass min_updates=2 to hide one-shot / placeholder agents.
        min_updates = arguments.get("min_updates", 0)
        recent_days = arguments.get("recent_days")  # Optional recency filter

        # Pagination support (optimization)
        offset = arguments.get("offset", 0)
        limit = arguments.get("limit")  # None = no limit (backward compatible)

        # Calculate cutoff time for recency filter
        cutoff_time = None
        if recent_days and recent_days > 0:
            cutoff_time = datetime.now(timezone.utc) - timedelta(days=recent_days)

        agents_list = []

        # First pass: collect all matching agents (without loading monitors)
        for agent_id, meta in list(mcp_server.agent_metadata.items()):
            # Filter by status if requested
            if status_filter != "all" and meta.status != status_filter:
                continue

            # Filter out test agents by default (unless explicitly requested)
            if not include_test_agents and _is_test_agent(agent_id, getattr(meta, 'label', None)):
                continue

            # Filter out low-activity agents (one-shot fragmentation cleanup)
            if min_updates and meta.total_updates < min_updates:
                continue

            # Apply recency filter
            if cutoff_time and meta.last_update:
                try:
                    last_dt = datetime.fromisoformat(meta.last_update.replace('Z', '+00:00'))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    if last_dt < cutoff_time:
                        continue
                except Exception:
                    pass

            # Filter by loaded status if requested
            if loaded_only:
                if agent_id not in mcp_server.monitors:
                    continue

            # Infer status for agents with None/unrecognized status
            inferred_status = meta.status
            if inferred_status not in ["active", "waiting_input", "paused", "archived", "deleted"]:
                # Infer status based on activity patterns
                now = datetime.now(timezone.utc)

                # Check if agent has any activity
                has_updates = meta.total_updates > 0
                is_recent = False
                days_since_update = None

                if meta.last_update:
                    try:
                        last_dt = datetime.fromisoformat(meta.last_update.replace('Z', '+00:00'))
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                        days_since_update = (now - last_dt).total_seconds() / 86400
                        is_recent = days_since_update < 7  # Active within last week
                    except Exception:
                        pass

                # Infer status:
                # - No updates or no last_update = archived (inactive)
                # - Recent activity (<7 days) = active
                # - Old activity (>7 days) = archived
                if not has_updates or meta.last_update is None:
                    inferred_status = "archived"  # No activity = archived
                elif is_recent:
                    inferred_status = "active"  # Recent activity = active
                else:
                    inferred_status = "archived"  # Old activity = archived

            from src.resident_progress.registry import is_event_driven_label
            visible_agent_id, uuid_redacted = _visible_agent_identifier(
                agent_id,
                meta,
                caller_uuid=caller_uuid,
                operator_caller=operator_caller,
            )
            parent_id, parent_redacted = _visible_related_agent_identifier(
                getattr(meta, 'parent_agent_id', None),
                caller_uuid=caller_uuid,
                operator_caller=operator_caller,
            )
            agent_info = {
                "agent_id": visible_agent_id,
                "label": getattr(meta, 'label', None),
                "purpose": getattr(meta, 'purpose', None),
                "lifecycle_status": inferred_status,
                "created": meta.created_at,
                "last_update": meta.last_update,
                "total_updates": meta.total_updates,
                "tags": meta.tags.copy() if meta.tags else [],
                "notes": meta.notes if meta.notes else "",
                "parent_agent_id": parent_id,
                "spawn_reason": getattr(meta, 'spawn_reason', None),
                "event_driven": is_event_driven_label(getattr(meta, 'label', None)),
            }
            agent_info.update(_compact_lifecycle_fields(meta))
            if uuid_redacted:
                agent_info["agent_id_redacted"] = True
            if parent_redacted:
                agent_info["parent_agent_id_redacted"] = True

            # Lazy load metrics only if requested (optimization)
            if include_metrics:
                # Only load monitor if already in memory (fast path)
                if agent_id in mcp_server.monitors:
                    try:
                        monitor = mcp_server.monitors[agent_id]
                        metrics = monitor.get_metrics()

                        # Calculate health_status consistently with process_agent_update
                        # Use health_checker.get_health_status() instead of metrics.get("status")
                        # to ensure consistency across all tools
                        risk_score = metrics.get("risk_score") or metrics.get("current_risk")
                        coherence = float(monitor.state.coherence) if monitor.state else None
                        void_active = bool(monitor.state.void_active) if monitor.state else False

                        health_status_obj, _ = mcp_server.health_checker.get_health_status(
                            risk_score=risk_score,
                            coherence=coherence,
                            void_active=void_active
                        )
                        agent_info["health_status"] = health_status_obj.value
                        agent_info["metrics"] = {
                            "E": safe_float(monitor.state.E),
                            "I": safe_float(monitor.state.I),
                            "S": safe_float(monitor.state.S),
                            "V": safe_float(monitor.state.V),
                            "coherence": safe_float(monitor.state.coherence),
                            "current_risk": metrics.get("current_risk"),  # Recent trend (last 10) - USED FOR HEALTH STATUS
                            "risk_score": safe_float(metrics.get("risk_score") or metrics.get("current_risk") or metrics.get("mean_risk", 0.5)),  # Governance/operational risk
                            "phi": metrics.get("phi"),  # Primary physics signal: Phi objective function
                            "verdict": metrics.get("verdict"),  # Primary governance signal: safe/caution/high-risk
                            "mean_risk": safe_float(metrics.get("mean_risk", 0.5)),  # Overall mean (all-time average) - for historical context
                            "lambda1": safe_float(monitor.state.lambda1),
                            "void_active": bool(monitor.state.void_active) if monitor.state.void_active is not None else False
                        }
                    except Exception as e:
                        agent_info["health_status"] = "error"
                        agent_info["metrics"] = None
                        logger.warning(f"Error getting metrics for {agent_id}: {e}")
                else:
                    # Monitor not in memory - load it to get metrics
                    cached_health = getattr(meta, 'health_status', None)
                    try:
                        monitor = mcp_server.get_or_create_monitor(agent_id)
                        await ensure_hydrated(monitor, agent_id)
                        metrics_dict = monitor.get_metrics()

                        # Get health status
                        if cached_health and cached_health != "unknown":
                            agent_info["health_status"] = cached_health
                        else:
                            risk_score = metrics_dict.get('risk_score', None)
                            coherence = float(monitor.state.coherence) if monitor.state else metrics_dict.get('coherence', None)
                            void_active = bool(monitor.state.void_active) if monitor.state else metrics_dict.get('void_active', False)

                            health_status_obj, _ = mcp_server.health_checker.get_health_status(
                                risk_score=risk_score,
                                coherence=coherence,
                                void_active=void_active
                            )
                            agent_info["health_status"] = health_status_obj.value

                            # Cache for future use
                            if meta:
                                meta.health_status = health_status_obj.value

                        # Populate metrics from monitor state
                        if monitor.state:
                            agent_info["metrics"] = {
                                "E": safe_float(monitor.state.E),
                                "I": safe_float(monitor.state.I),
                                "S": safe_float(monitor.state.S),
                                "V": safe_float(monitor.state.V),
                                "coherence": safe_float(monitor.state.coherence),
                                "current_risk": metrics_dict.get("current_risk"),
                                "risk_score": safe_float(metrics_dict.get("risk_score") or metrics_dict.get("current_risk") or metrics_dict.get("mean_risk", 0.5)),
                                "phi": metrics_dict.get("phi"),
                                "verdict": metrics_dict.get("verdict"),
                                "mean_risk": safe_float(metrics_dict.get("mean_risk", 0.5)),
                                "lambda1": safe_float(monitor.state.lambda1),
                                "void_active": bool(monitor.state.void_active) if monitor.state.void_active is not None else False
                            }
                        else:
                            agent_info["metrics"] = None
                    except Exception as e:
                        logger.debug(f"Could not load metrics for agent '{agent_id}': {e}")
                        agent_info["health_status"] = cached_health or "unknown"
                        agent_info["metrics"] = None
            else:
                # No metrics requested - try cached health_status first, calculate if missing
                cached_health = getattr(meta, 'health_status', None)
                if cached_health and cached_health != "unknown":
                    agent_info["health_status"] = cached_health
                else:
                    # No cached health status or it's "unknown" - calculate it
                    try:
                        monitor = mcp_server.get_or_create_monitor(agent_id)
                        await ensure_hydrated(monitor, agent_id)
                        metrics_dict = monitor.get_metrics()
                        attention_score = metrics_dict.get('attention_score') or metrics_dict.get('risk_score', None)
                        coherence = metrics_dict.get('coherence', None)
                        void_active = metrics_dict.get('void_active', False)

                        health_status_obj, _ = mcp_server.health_checker.get_health_status(
                            risk_score=attention_score,
                            coherence=coherence,
                            void_active=void_active
                        )
                        agent_info["health_status"] = health_status_obj.value

                        # Cache for future use
                        if meta:
                            meta.health_status = health_status_obj.value
                    except Exception as e:
                        logger.debug(f"Could not calculate health status for agent '{agent_id}': {e}")
                        agent_info["health_status"] = "unknown"
                agent_info["metrics"] = None

            # Add standardized fields if requested
            if standardized:
                agent_info.setdefault("health_status", "unknown")
                agent_info.setdefault("metrics", None)

            # Trust tier from cached trajectory data (DB fallback done in batch below)
            cached_tier = getattr(meta, 'trust_tier', None)
            agent_info["trust_tier"] = cached_tier
            agent_info["_agent_uuid"] = agent_id

            agents_list.append(agent_info)

        # Batch-load trust tiers for agents missing cached values (avoids N+1 queries)
        # (S6 Option B: substrate-earned routing)
        agents_needing_tiers = [a for a in agents_list if a["trust_tier"] is None]
        if agents_needing_tiers:
            try:
                from src.identity.trust_tier_routing import resolve_trust_tier
                from src.db import get_db as _get_db
                db = _get_db()
                ids_to_fetch = [a["_agent_uuid"] for a in agents_needing_tiers]
                identities = await db.get_identities_batch(ids_to_fetch)
                for agent_info in agents_needing_tiers:
                    aid = agent_info["_agent_uuid"]
                    identity = identities.get(aid)
                    if identity and identity.metadata:
                        meta_obj = mcp_server.agent_metadata.get(aid)
                        tier_info = await resolve_trust_tier(
                            aid,
                            identity.metadata,
                            prefetched_tags=getattr(meta_obj, "tags", None) if meta_obj else None,
                            prefetched_label=getattr(meta_obj, "label", None) if meta_obj else None,
                        )
                        agent_info["trust_tier"] = tier_info.get("name", "unknown")
                        # Cache for next time
                        if meta_obj:
                            meta_obj.trust_tier = agent_info["trust_tier"]
                            meta_obj.trust_tier_num = tier_info.get("tier", 0)
            except Exception as e:
                logger.debug(f"Batch trust tier lookup failed: {e}")

        # Sort by last_update (most recent first)
        agents_list.sort(key=lambda x: x.get("last_update", ""), reverse=True)

        # Calculate status counts BEFORE pagination (for accurate totals)
        total_count = len(agents_list)
        status_counts = {
            "active": sum(1 for a in agents_list if a.get("lifecycle_status") == "active"),
            "waiting_input": sum(1 for a in agents_list if a.get("lifecycle_status") == "waiting_input"),
            "paused": sum(1 for a in agents_list if a.get("lifecycle_status") == "paused"),
            "archived": sum(1 for a in agents_list if a.get("lifecycle_status") == "archived"),
            "deleted": sum(1 for a in agents_list if a.get("lifecycle_status") == "deleted"),
            "unknown": sum(1 for a in agents_list if a.get("lifecycle_status") not in ["active", "waiting_input", "paused", "archived", "deleted"])
        }
        # Participation split (before pagination): an agent has "participated"
        # once it has recorded at least one check-in (total_updates >= 1). This
        # is the same signal the lite-path ghost filter uses (total_updates < 1).
        # Surfaced so count consumers can show working agents, not raw row count.
        participated = sum(1 for a in agents_list if (a.get("total_updates") or 0) >= 1)
        never_participated = total_count - participated

        # Apply pagination (optimization)
        if limit is not None:
            agents_list = agents_list[offset:offset + limit]
        elif offset > 0:
            agents_list = agents_list[offset:]
        for agent_info in agents_list:
            agent_info.pop("_agent_uuid", None)

        # Group by status if requested (for returned agents only)
        if grouped and not summary_only:
            grouped_agents = {
                "active": [a for a in agents_list if a.get("lifecycle_status") == "active"],
                "waiting_input": [a for a in agents_list if a.get("lifecycle_status") == "waiting_input"],
                "paused": [a for a in agents_list if a.get("lifecycle_status") == "paused"],
                "archived": [a for a in agents_list if a.get("lifecycle_status") == "archived"],
                "deleted": [a for a in agents_list if a.get("lifecycle_status") == "deleted"],
                "unknown": [a for a in agents_list if a.get("lifecycle_status") not in ["active", "waiting_input", "paused", "archived", "deleted"]]
            }

            response_data = {
                "success": True,
                "agents": grouped_agents,
                "summary": {
                    "total": total_count,  # Use total_count (before pagination)
                    "returned": len(agents_list),  # Number actually returned (after pagination)
                    "offset": offset,
                    "limit": limit,
                    "by_status": status_counts,  # Use counts from BEFORE pagination
                    "participated": participated,  # checked in >=1 time
                    "never_participated": never_participated  # onboarded but never checked in
                }
            }

            # Add health breakdown if include_metrics
            if include_metrics:
                response_data["summary"]["by_health"] = {
                    "healthy": sum(1 for a in agents_list if a.get("health_status") == "healthy"),
                    "moderate": sum(1 for a in agents_list if a.get("health_status") == "moderate"),
                    "critical": sum(1 for a in agents_list if a.get("health_status") == "critical"),
                    "unknown": sum(1 for a in agents_list if a.get("health_status") == "unknown"),
                    "error": sum(1 for a in agents_list if a.get("health_status") == "error")
                }
        else:
            response_data = {
                "success": True,
                "agents": agents_list,
                "summary": {
                    "total": total_count,  # Use total_count (before pagination)
                    "returned": len(agents_list),  # Number actually returned (after pagination)
                    "offset": offset,
                    "limit": limit,
                    "by_status": status_counts,  # Use counts from BEFORE pagination
                    "participated": participated,  # checked in >=1 time
                    "never_participated": never_participated  # onboarded but never checked in
                }
            }

            if include_metrics:
                health_statuses = {"healthy": 0, "moderate": 0, "critical": 0, "unknown": 0, "error": 0}
                for agent in agents_list:
                    status = agent.get("health_status", "unknown")
                    health_statuses[status] = health_statuses.get(status, 0) + 1
                response_data["summary"]["by_health"] = health_statuses

        if summary_only:
            return success_response(response_data["summary"])

        # Add EISV labels for API documentation (only if metrics are included)
        if include_metrics:
            response_data["eisv_labels"] = __import__('src.governance_monitor', fromlist=['UNITARESMonitor']).UNITARESMonitor.get_eisv_labels()

        return success_response(response_data)

    except Exception as e:
        return system_error_helper(
            "list_agents",
            e
        )

@mcp_tool("get_agent_metadata", timeout=10.0, register=False)
async def handle_get_agent_metadata(arguments: Sequence[TextContent]) -> list:
    """Get complete metadata for an agent including lifecycle events, current state, and computed fields.

    Args:
        target_agent: Optional UUID or label of agent to look up.
                      If not provided, returns calling agent's metadata.
    """
    # Check for target_agent parameter (allows looking up other agents by UUID or label)
    target_agent = arguments.get("target_agent") or arguments.get("agent_id")
    caller_uuid = _context_agent_id()
    operator_caller = _is_operator_request()

    if target_agent:
        # FAST PATH: Check Redis cache first (by UUID)
        try:
            from src.cache import get_metadata_cache
            metadata_cache = get_metadata_cache()
            cached_meta = await metadata_cache.get(target_agent)
            if cached_meta:
                # Found in Redis cache - use it directly
                logger.debug(f"Metadata cache hit: {target_agent[:8]}...")
                agent_id = target_agent
                # Convert cached dict back to AgentMetadata for consistency
                from src.agent_state import AgentMetadata
                meta = AgentMetadata(**cached_meta)
                # Update in-memory cache for consistency
                mcp_server.agent_metadata[agent_id] = meta
                # Skip to response building (meta already loaded)
                monitor = mcp_server.monitors.get(agent_id)
                metadata_response = _metadata_dict_for_response(
                    agent_id,
                    meta,
                    caller_uuid=caller_uuid,
                    operator_caller=operator_caller,
                )
                # Add computed fields
                if monitor:
                    metadata_response["current_state"] = {
                        "lambda1": float(monitor.state.lambda1),
                        "coherence": float(monitor.state.coherence),
                        "void_active": bool(monitor.state.void_active),
                        "E": float(monitor.state.E),
                        "I": float(monitor.state.I),
                        "S": float(monitor.state.S),
                        "V": float(monitor.state.V),
                    }
                else:
                    metadata_response["current_state"] = None
                metadata_response["identity_view"] = _identity_view(
                    agent_id,
                    meta,
                    caller_uuid=caller_uuid,
                    operator_caller=operator_caller,
                )
                # Add EISV labels (__import__('src.governance_monitor', fromlist=['UNITARESMonitor']).UNITARESMonitor imported at module level)
                metadata_response["eisv_labels"] = __import__('src.governance_monitor', fromlist=['UNITARESMonitor']).UNITARESMonitor.get_eisv_labels()
                return success_response(metadata_response)
        except Exception as e:
            logger.debug(f"Metadata cache check failed: {e}")

        # Look up by UUID first (in-memory cache)
        if target_agent in mcp_server.agent_metadata:
            agent_id = target_agent
        else:
            # Try label lookup in cache
            agent_id = None
            for uuid_key, m in list(mcp_server.agent_metadata.items()):
                if getattr(m, 'label', None) == target_agent:
                    agent_id = uuid_key
                    break

            # In-memory metadata is kept current by onboard/process_agent_update.
            # Do NOT reload from DB here — asyncpg awaits inside MCP handlers
            # deadlock under the anyio task group (same bug as list_agents had).
            if not agent_id:
                # Provide helpful error message
                return [error_response(
                    f"Agent not found: '{target_agent}'. Use UUID or label.",
                    recovery={
                        "action": "Use list_agents() to find valid agent IDs",
                        "tip": "Labels are case-sensitive. Use list_agents(named_only=true) to see agents with labels.",
                        "note": "If you just set a label with identity(name='...'), it may take a moment to persist. Try again in a few seconds."
                    },
                    details={
                        "searched_in": "in-memory cache (Redis + live metadata)",
                        "suggestion": "Use UUID from list_agents() output, or wait a moment if you just set a label"
                    }
                )]
    else:
        # Default: get calling agent's metadata
        agent_id, error = require_registered_agent(arguments)
        if error:
            return [error]  # Returns onboarding guidance if not registered
        caller_uuid = caller_uuid or agent_id

    meta = mcp_server.agent_metadata[agent_id]
    monitor = mcp_server.monitors.get(agent_id)

    # Populate Redis cache for future lookups (best effort, non-blocking)
    try:
        from src.cache import get_metadata_cache
        await get_metadata_cache().set(agent_id, meta.to_dict(), ttl=300)
    except Exception as e:
        logger.debug(f"Failed to cache metadata: {e}")

    metadata_response = _metadata_dict_for_response(
        agent_id,
        meta,
        caller_uuid=caller_uuid,
        operator_caller=operator_caller,
    )
    metadata_response["identity_view"] = _identity_view(
        agent_id,
        meta,
        caller_uuid=caller_uuid,
        operator_caller=operator_caller,
    )

    # Add computed fields
    if monitor:
        metadata_response["current_state"] = {
            "lambda1": float(monitor.state.lambda1),
            "coherence": float(monitor.state.coherence),
            "void_active": bool(monitor.state.void_active),
            "E": float(monitor.state.E),
            "I": float(monitor.state.I),
            "S": float(monitor.state.S),
            "V": float(monitor.state.V)
        }

    # Days since update
    try:
        if meta.last_update:
            # Handle various datetime formats
            last_update_str = meta.last_update.replace('Z', '+00:00')
            last_update_dt = datetime.fromisoformat(last_update_str)
            if last_update_dt.tzinfo is None:
                last_update_dt = last_update_dt.replace(tzinfo=timezone.utc)
            now_dt = datetime.now(timezone.utc)
            days_since = (now_dt - last_update_dt).days
            metadata_response["days_since_update"] = days_since
        else:
            metadata_response["days_since_update"] = None
    except Exception as e:
        logger.debug(f"Could not calculate days_since_update: {e}")
        metadata_response["days_since_update"] = None

    # Add EISV labels for API documentation (only if current_state exists)
    if "current_state" in metadata_response:
        metadata_response["eisv_labels"] = __import__('src.governance_monitor', fromlist=['UNITARESMonitor']).UNITARESMonitor.get_eisv_labels()

    return success_response(metadata_response)
