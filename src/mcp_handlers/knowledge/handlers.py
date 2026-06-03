"""
Knowledge Graph MCP Handlers

Fast, indexed, non-blocking knowledge operations using knowledge graph.
Replaces deprecated file-based knowledge layer.

Performance:
- store_knowledge: ~0.01ms (vs 350ms file-based) - 35,000x faster
- search_knowledge: O(indexes) not O(n) - scales logarithmically
- find_similar: Tag-based overlap - no brute force scanning

Claude Desktop compatible: All operations are async and non-blocking.
"""

from typing import Dict, Any, Sequence, Optional
from mcp.types import TextContent
from datetime import datetime, timezone


def _utc_now_iso() -> str:
    """Return current UTC time as an offset-aware ISO 8601 string.

    Bug fix 2026-04-25: write-path timestamps were generated with
    naive ``datetime.now()`` (server local TZ), causing ``id`` to drift
    from ``created_at`` (UTC, via PG TIMESTAMPTZ) by the server's offset
    and breaking lex-sort across multi-tz fleets. All KG write-path
    timestamps must be UTC-aware.
    """
    return datetime.now(timezone.utc).isoformat()
import time
from ..utils import success_response, error_response, require_argument, require_agent_id, require_registered_agent
from ..decorators import mcp_tool
from ..validators import apply_param_aliases
from src.knowledge_graph import get_knowledge_graph, DiscoveryNode, ResponseTo, normalize_tags
from src.mcp_handlers.knowledge.limits import MAX_SUMMARY_LEN, MAX_DETAILS_LEN
from config.governance_config import config
from src.logging_utils import get_logger
from src.perf_monitor import record_ms
from ..support.llm_delegation import synthesize_results
from ..support.tool_hints import (
    KNOWLEDGE_SEARCH_TOOL,
    KNOWLEDGE_OPEN_QUESTIONS_WORKFLOW,
)

logger = get_logger(__name__)

from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
from src.broadcaster import broadcaster_instance

VALID_DISCOVERY_TYPES = {
    "architectural_decision", "learning", "pattern", "bug_fix",
    "refactoring", "documentation", "experiment", "question", "note", "rule",
    "insight", "bug_found", "improvement", "exploration", "observation",
    # System-generated rollup rows (Issue #1 synthesis). Listed so search can
    # filter discovery_type='topic_rollup'; written by the synthesis pass, not
    # by agents directly.
    "topic_rollup",
}
VALID_SEVERITIES = {"low", "medium", "high", "critical"}
SEVERITY_ALIASES = {
    "info": "low",
    "informational": "low",
    "warn": "medium",
    "warning": "medium",
    "error": "high",
    "fatal": "critical",
    "urgent": "critical",
}


def _normalize_discovery_type(discovery_type: Any) -> Any:
    if isinstance(discovery_type, str):
        normalized = discovery_type.strip().lower()
        if normalized == "bug":
            return "bug_found"
        return normalized
    return discovery_type


def _invalid_enum_response(field: str, value: Any, valid_values: set[str], *, tip: str | None = None):
    normalized = str(value).strip().lower()
    suggestion = None
    if field == "severity":
        suggestion = SEVERITY_ALIASES.get(normalized)
    elif field == "discovery_type" and normalized == "bug":
        suggestion = "bug_found"

    valid = sorted(valid_values)
    message = f"Invalid {field} '{normalized}'. Valid: {valid}."
    if suggestion in valid_values:
        message += f" Did you mean '{suggestion}'?"
    elif tip:
        message += f" {tip}"

    return error_response(
        message,
        error_code="PARAMETER_ERROR",
        error_category="validation_error",
        details={
            "error_type": "invalid_enum_value",
            "parameter": field,
            "provided_value": normalized,
            "valid_values": valid,
            "suggested_value": suggestion if suggestion in valid_values else None,
        },
        recovery={
            "action": (
                f"Use {field}='{suggestion}'"
                if suggestion in valid_values
                else f"Use one of: {', '.join(valid)}"
            )
        },
    )


async def _clamp_confidence_to_coherence(discovery, agent_id: str) -> bool:
    """Cross-check discovery confidence against agent's EISV coherence.

    If confidence > coherence + 0.3, clamp it and annotate provenance.
    Returns True if clamping occurred.
    """
    if discovery.confidence is None:
        return False
    try:
        monitor = mcp_server.monitors.get(agent_id)
        if monitor is None:
            return False
        coherence = monitor.state.coherence
        max_allowed = coherence + 0.3
        if discovery.confidence > max_allowed:
            original = discovery.confidence
            discovery.confidence = round(max_allowed, 6)
            # Annotate provenance
            if discovery.provenance is None:
                discovery.provenance = {}
            discovery.provenance["confidence_clamped"] = True
            discovery.provenance["original_confidence"] = original
            logger.info(
                "Knowledge confidence clamped: %.3f -> %.3f (coherence=%.3f)",
                original,
                discovery.confidence,
                coherence,
            )
            await broadcaster_instance.broadcast_event(
                "knowledge_confidence_clamped",
                agent_id=agent_id,
                payload={
                    "original_confidence": original,
                    "clamped_confidence": discovery.confidence,
                    "coherence": round(coherence, 6),
                },
            )
            return True
    except Exception as e:
        logger.debug("Confidence cross-check skipped: %s", e)
    return False


async def _broadcast_knowledge_write(discovery, agent_id: str) -> None:
    """Emit a ``knowledge_write`` event to the broadcaster (best-effort).

    Dashboard timeline and the bridge's WS subscriber both key off this
    event class to render KG writes in real time. Before this helper
    existed, Vigil and Sentinel notes landed in the KG but never reached
    either live surface — they were only visible via a full discovery
    fetch or ``/kg search``. Alerts disappeared from user view as soon
    as the macOS notification faded.

    Best-effort: any broadcaster failure is swallowed so a dead WS
    listener cannot break the KG write path.
    """
    try:
        tags = list(getattr(discovery, "tags", None) or [])
        summary = getattr(discovery, "summary", None) or ""
        if len(summary) > 500:
            summary = summary[:497] + "..."
        await broadcaster_instance.broadcast_event(
            "knowledge_write",
            agent_id=agent_id,
            payload={
                "discovery_id": getattr(discovery, "id", None),
                "discovery_type": getattr(discovery, "type", None) or "note",
                "severity": getattr(discovery, "severity", None) or "low",
                "summary": summary,
                "tags": tags,
            },
        )
    except Exception as exc:
        logger.debug("knowledge_write broadcast skipped: %s", exc)


async def _broadcast_knowledge_read(
    action: str,
    reader_agent_id: Optional[str],
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit a ``knowledge_read`` event so read traffic is observable.

    Writes have been audited since the broadcaster shipped; reads were not,
    which made the central usage question for the KG ("is anyone actually
    pulling from this?") unanswerable from audit.events. This helper closes
    that gap. ``action`` is one of ``search``/``get``/``list``/``details``;
    when knowable (``details``, search-result enumeration), the payload
    carries the writer agent_id so cross-agent reads can be distinguished
    from self-reads in SQL.
    """
    try:
        body: Dict[str, Any] = {"action": action}
        if payload:
            body.update(payload)
        await broadcaster_instance.broadcast_event(
            "knowledge_read",
            agent_id=reader_agent_id,
            payload=body,
        )
    except Exception as exc:
        logger.debug("knowledge_read broadcast skipped: %s", exc)


def _resolve_reader_agent_id(arguments: Dict[str, Any]) -> Optional[str]:
    """Best-effort reader-identity extraction for read-side audit events."""
    from ..context import get_context_agent_id
    return (
        arguments.get("_agent_uuid")
        or get_context_agent_id()
        or arguments.get("agent_id")
    )


def _compute_staleness_warning(discovery, current_server_version: str) -> Optional[str]:
    """Flag open entries that are likely stale (>60 days old or 2+ minor versions behind)."""
    warning_parts = []

    # Age-based check: >60 days old
    # Compare in UTC. Legacy rows may have naive timestamps (treat as UTC);
    # post-2026-04-25 rows are UTC-aware.
    try:
        created = datetime.fromisoformat(discovery.timestamp) if isinstance(discovery.timestamp, str) else discovery.timestamp
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - created).days
        if age_days > 60:
            warning_parts.append(f"This entry is {age_days} days old and still open.")
    except (ValueError, TypeError):
        pass

    # Version-based check: 2+ minor versions behind current
    entry_version = None
    if discovery.provenance and isinstance(discovery.provenance, dict):
        entry_version = discovery.provenance.get("system_version")
    if entry_version and current_server_version and current_server_version != "unknown":
        try:
            ev = [int(x) for x in str(entry_version).split(".")]
            cv = [int(x) for x in str(current_server_version).split(".")]
            if len(ev) >= 2 and len(cv) >= 2:
                minor_distance = (cv[0] - ev[0]) * 100 + (cv[1] - ev[1])
                if minor_distance >= 2:
                    warning_parts.append(f"Written against v{entry_version} (current: v{current_server_version}).")
        except (ValueError, IndexError):
            pass

    if warning_parts:
        return " ".join(warning_parts) + " It may be outdated — verify before acting on it."
    return None


async def _build_s7_provenance_chain_with_fallback(
    agent_id: str,
    meta: Optional[Any],
    lineage_fn,
) -> Optional[list[dict[str, Any]]]:
    """Build S7 provenance chain from DB, falling back to metadata on errors."""
    try:
        from src.identity.provenance_chain import build_lineage_provenance_chain

        chain = await build_lineage_provenance_chain(agent_id)
        return chain or None
    except Exception as lineage_error:
        logger.debug(
            "Could not capture authoritative provenance chain: %s",
            lineage_error,
        )

    try:
        parent_agent_id = getattr(meta, "parent_agent_id", None) if meta else None
        if not parent_agent_id:
            return None

        lineage = lineage_fn(agent_id)  # [oldest_ancestor, ..., parent, self]
        if len(lineage) <= 1:
            return None
        provenance_chain = []
        for ancestor_id in lineage[:-1]:
            ancestor_meta = mcp_server.agent_metadata.get(ancestor_id)
            if ancestor_meta:
                provenance_chain.append(
                    {
                        "agent_id": ancestor_id,
                        "relationship": "ancestor",
                        "spawn_reason": ancestor_meta.spawn_reason,
                        "created_at": ancestor_meta.created_at,
                        "lineage_depth": len(provenance_chain),
                        "source": "agent_metadata_fallback",
                    }
                )

        if parent_agent_id:
            parent_meta = mcp_server.agent_metadata.get(parent_agent_id)
            if parent_meta:
                provenance_chain.append(
                    {
                        "agent_id": parent_agent_id,
                        "relationship": "direct_parent",
                        "spawn_reason": getattr(meta, "spawn_reason", None),
                        "created_at": parent_meta.created_at,
                        "lineage_depth": len(provenance_chain),
                        "source": "agent_metadata_fallback",
                    }
                )
        return provenance_chain or None
    except Exception as fallback_error:
        logger.debug("Could not capture fallback provenance chain: %s", fallback_error)
        return None


async def _discovery_not_found(discovery_id: str, graph) -> TextContent:
    """Build a 'not found' error with prefix-match suggestions.

    LLMs sometimes truncate ISO-timestamp discovery IDs (e.g. '2025-12-20T15:43:51' → '2025').
    When an exact match fails, search for IDs that start with the given prefix and offer
    suggestions so the agent can retry with the correct full ID.
    """
    suggestions = []
    try:
        db = await graph._get_db()
        cypher = f"""
            MATCH (d:Discovery)
            WHERE d.id STARTS WITH ${{prefix}}
            RETURN d.id
            LIMIT 5
        """
        rows = await db.graph_query(cypher, {"prefix": discovery_id})
        for row in rows:
            if isinstance(row, dict) and "d.id" in row:
                suggestions.append(row["d.id"])
            elif isinstance(row, str):
                suggestions.append(row)
    except Exception:
        pass  # Best-effort suggestions

    if suggestions:
        return error_response(
            f"Discovery '{discovery_id}' not found. Did you mean one of these?",
            recovery={
                "matching_ids": suggestions,
                "action": "Retry with the full discovery_id from the list above",
                "hint": "Discovery IDs are ISO timestamps (e.g. '2025-12-20T15:43:51.020454'). "
                        "Pass the complete ID, not just the year.",
            }
        )
    return error_response(f"Discovery '{discovery_id}' not found")

def _check_display_name_required(agent_id: str, arguments: Dict[str, Any]) -> tuple[Optional[TextContent], Optional[str]]:
    """
    Check if agent has a meaningful display_name set for KG attribution.

    UX FIX (Feb 2026): Auto-generate display_name instead of blocking.
    If no meaningful display_name is set, auto-generates one and returns a warning.
    This allows agents to contribute to KG immediately without the name-setting ritual.

    Returns:
        Tuple of (error_if_any, warning_message_if_generated)
        - (None, None) if display_name is set and meaningful
        - (None, "warning message") if display_name was auto-generated
        - Error only returned for critical failures (rare)
    """
    try:
        from ..context import get_context_agent_id
        import uuid as uuid_module

        # Get the actual UUID for this agent
        bound_uuid = get_context_agent_id()

        # Check if display_name is set in metadata
        meta = None
        if bound_uuid and bound_uuid in mcp_server.agent_metadata:
            meta = mcp_server.agent_metadata[bound_uuid]
        elif agent_id in mcp_server.agent_metadata:
            meta = mcp_server.agent_metadata[agent_id]

        if meta:
            display_name = getattr(meta, 'display_name', None) or getattr(meta, 'label', None)

            # Check if display_name is meaningful (not just a UUID or auto-generated)
            if display_name:
                # Skip check if it looks like a real name (not UUID pattern)
                is_uuid_pattern = False
                try:
                    uuid_module.UUID(display_name, version=4)
                    is_uuid_pattern = True
                except (ValueError, AttributeError):
                    pass

                # Also check for auto-generated patterns like "auto_20251229_abc123"
                is_auto_pattern = display_name.startswith("auto_") or display_name.startswith("Agent_")

                if not is_uuid_pattern and not is_auto_pattern:
                    return None, None  # Has a real display_name, OK to proceed

        # No meaningful display_name - auto-generate instead of blocking
        # UX FIX (Feb 2026): Don't block first contribution, just warn
        auto_name = f"Agent_{(bound_uuid or agent_id)[:8]}"

        # Try to set the auto-generated name in metadata
        if meta and bound_uuid:
            try:
                meta.label = auto_name
                meta.display_name = auto_name
            except Exception as e:
                logger.debug(f"Could not save auto-generated display_name: {e}")

        warning = (
            f"KG entry attributed to '{auto_name}' (auto-generated). "
            f"Call identity(name='YourName') to set a personalized name."
        )
        return None, warning

    except Exception as e:
        logger.debug(f"Could not check display_name: {e}")
        return None, None  # Don't block on check failures

def _resolve_agent_display(agent_id: str) -> Dict[str, str]:
    """
    Resolve agent_id to display info (v2.5.4).

    Returns dict with agent_id, display_name for human-readable output.
    UUID is never exposed - kept internal for session binding only.

    Args:
        agent_id: Either model+date format (new) or UUID (legacy lookups)
    """
    try:
        # Try direct lookup (if agent_id is actually a UUID in legacy data)
        if agent_id in mcp_server.agent_metadata:
            meta = mcp_server.agent_metadata[agent_id]
            structured_id = getattr(meta, 'structured_id', None) or agent_id
            display_name = (
                getattr(meta, 'display_name', None) or
                getattr(meta, 'label', None) or
                structured_id
            )
            return {"agent_id": structured_id, "display_name": display_name}

        # Search by structured_id or label
        for uuid_key, meta in mcp_server.agent_metadata.items():
            if getattr(meta, 'structured_id', None) == agent_id or getattr(meta, 'label', None) == agent_id:
                display_name = (
                    getattr(meta, 'display_name', None) or
                    getattr(meta, 'label', None) or
                    agent_id
                )
                return {"agent_id": agent_id, "display_name": display_name}
    except Exception:
        pass
    # Fallback: use agent_id as-is
    return {"agent_id": agent_id, "display_name": agent_id}


def _derive_anonymous_writer_id(arguments: Dict[str, Any]) -> str:
    """Derive a stable low-friction writer ID for anonymous low-severity writes."""
    import hashlib
    from ..context import get_context_client_session_id, get_context_session_key, get_session_signals

    signals = get_session_signals()
    source = (
        arguments.get("client_session_id")
        or get_context_client_session_id()
        or get_context_session_key()
        or (signals.x_session_id if signals else None)
        or (signals.mcp_session_id if signals else None)
        or (signals.x_client_id if signals else None)
        or (signals.oauth_client_id if signals else None)
        or (signals.ip_ua_fingerprint if signals else None)
    )
    client_hint = (signals.client_hint if signals else None) or (signals.transport if signals else None) or "client"
    client_hint = "".join(ch if ch.isalnum() else "_" for ch in client_hint.lower()).strip("_") or "client"

    if source:
        digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12]
        return f"anonkg_{client_hint}_{digest}"
    return f"anonkg_{client_hint}_local"


def _resolve_low_friction_writer(arguments: Dict[str, Any]) -> tuple[str, Optional[TextContent], bool]:
    """Resolve agent_id for low/medium knowledge writes.

    If the caller has no explicit or bound identity, use a stable anonymous writer
    ID instead of creating a new auto_* identity for each quick write.
    """
    from ..context import get_context_agent_id

    if arguments.get("agent_id") or get_context_agent_id():
        agent_id, error = require_agent_id(arguments)
        return agent_id, error, False

    agent_id = _derive_anonymous_writer_id(arguments)
    arguments["agent_id"] = agent_id
    return agent_id, None, True

@mcp_tool("store_knowledge_graph", timeout=20.0, register=False)
async def handle_store_knowledge_graph(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Store knowledge discovery/discoveries in graph - fast, non-blocking, transparent

    Accepts either:
    - Single discovery: discovery_type, summary, details, tags, etc.
    - Batch discoveries: discoveries array (max 10 per batch)
    """
    # MAGNET PATTERN: Accept fuzzy inputs (discovery, insight, finding → summary)
    arguments = apply_param_aliases("store_knowledge_graph", arguments)

    # REDUCE FRICTION (Dec 2025): Allow unregistered agents to write low/medium notes
    # Only enforce strict registration and display name for high/critical severity (security)
    # UX FIX (Feb 2026): Auto-generate display_name instead of blocking
    raw_severity = str(arguments.get("severity", "low")).lower()
    display_name_warning = None  # Track if we auto-generated a name
    is_anonymous_writer = False

    if raw_severity in ["high", "critical"]:
        agent_id, error = require_registered_agent(arguments)
        if not error:
            # Check display_name (auto-generates if missing, returns warning)
            display_name_error, display_name_warning = _check_display_name_required(agent_id, arguments)
            if display_name_error:
                return [display_name_error]
    else:
        agent_id, error, is_anonymous_writer = _resolve_low_friction_writer(arguments)

    if error:
        return [error]

    # CIRCUIT BREAKER: Paused agents cannot store knowledge
    from ..utils import check_agent_can_operate
    blocked = check_agent_can_operate(agent_id)
    if blocked:
        return [blocked]

    # Check if batch mode (discoveries array provided)
    if "discoveries" in arguments and arguments["discoveries"] is not None:
        # Batch mode - delegate to batch handler logic
        return await _handle_store_knowledge_graph_batch(arguments, agent_id)
    
    # Set tool name in context for better error messages
    arguments["_tool_name"] = "store_knowledge_graph"
    
    # Single discovery mode (original behavior)
    # LITE-FIRST: discovery_type defaults to "note" (simplest form)
    discovery_type = arguments.get("discovery_type", "note")
    discovery_type = _normalize_discovery_type(discovery_type)
    
    # Validate discovery_type enum
    if discovery_type not in VALID_DISCOVERY_TYPES:
        return _invalid_enum_response(
            "discovery_type",
            discovery_type,
            VALID_DISCOVERY_TYPES,
            tip="Tip: use 'bug_found' (or shorthand 'bug').",
        )

    summary, error = require_argument(arguments, "summary",
                                    "summary is required - what did you discover/learn?")
    if error:
        return [error]

    # KG hygiene v1: supersedes parameter — early validation only.
    # Pre-flight predecessor lookup + permanent-policy veto happens inside
    # the try block once we have a graph instance.
    supersedes_id = arguments.get("supersedes")
    if supersedes_id is not None:
        supersedes_id = str(supersedes_id).strip()
        if not supersedes_id:
            return [error_response("supersedes parameter cannot be empty string")]

    try:
        # SECURITY: Rate limiting is handled by the knowledge graph backend
        # Backend handles rate limiting internally (O(1) per store)
        # No need for inefficient O(n) query here - let graph handle it
        graph = await get_knowledge_graph()
        
        # Truncate fields to prevent context overflow. Limits are imported
        # from limits.py; see that module for how they relate to the BGE-M3
        # embed budget.
        raw_summary = summary
        # Accept both 'details' and 'content' as parameter names
        raw_details = arguments.get("details") or arguments.get("content") or ""

        # Track truncation for visibility (v2.5.0+)
        truncation_info = {}

        if len(raw_summary) > MAX_SUMMARY_LEN:
            truncation_info["summary"] = f"Truncated from {len(raw_summary)} to {MAX_SUMMARY_LEN} chars"
            # Try to cut at sentence boundary, else word boundary
            truncated = raw_summary[:MAX_SUMMARY_LEN]
            # Look for last sentence end in final 100 chars
            for end_char in ['. ', '! ', '? ']:
                last_end = truncated.rfind(end_char, MAX_SUMMARY_LEN - 100)
                if last_end > 0:
                    truncated = truncated[:last_end + 1]
                    break
            else:
                # No sentence boundary, cut at word
                last_space = truncated.rfind(' ')
                if last_space > MAX_SUMMARY_LEN - 50:
                    truncated = truncated[:last_space]
            summary = truncated.rstrip() + "..."

        if len(raw_details) > MAX_DETAILS_LEN:
            truncation_info["details"] = f"Truncated from {len(raw_details)} to {MAX_DETAILS_LEN} chars"
            raw_details = raw_details[:MAX_DETAILS_LEN] + "... [truncated]"
        
        # Create discovery node
        discovery_id = _utc_now_iso()
        
        # Parse response_to if provided (typed response to parent discovery)
        response_to = None
        if "response_to" in arguments and arguments["response_to"]:
            resp_data = arguments["response_to"]
            if isinstance(resp_data, dict) and "discovery_id" in resp_data and "response_type" in resp_data:
                # Validate discovery_id format
                parent_id = str(resp_data["discovery_id"]).strip()
                if not parent_id:
                    return error_response("Invalid response_to.discovery_id (empty)")

                # Validate response_type enum
                response_type = resp_data["response_type"]
                VALID_RESPONSE_TYPES = {"extend", "question", "disagree", "support", "answer", "follow_up", "correction", "elaboration", "supersedes"}
                if response_type not in VALID_RESPONSE_TYPES:
                    return error_response(f"Invalid response_type '{response_type}'. Valid: {sorted(VALID_RESPONSE_TYPES)}")

                from src.knowledge_graph import ResponseTo
                response_to = ResponseTo(
                    discovery_id=parent_id,
                    response_type=response_type
                )

        # Validate severity if provided
        severity = arguments.get("severity")
        if severity is not None:
            severity = str(severity).lower()
            if severity not in VALID_SEVERITIES:
                return _invalid_enum_response("severity", severity, VALID_SEVERITIES)

        # Auto-populate system_version at write time (Task 1: KG version coupling)
        system_version = getattr(mcp_server, "SERVER_VERSION", "unknown")

        # ENHANCED PROVENANCE: Capture agent state at creation time
        # Answers: "What was the agent's context when they made this discovery?"
        provenance = None
        provenance_chain = None
        try:
            from src.provenance_context import (
                attach_s22_context,
                build_s22_write_context,
            )
            from ..identity.shared import _get_lineage  # Import lineage function

            meta = None
            if agent_id in mcp_server.agent_metadata:
                meta = mcp_server.agent_metadata[agent_id]

                # Get monitor state if available
                monitor_state = {}
                if agent_id in mcp_server.monitors:
                    monitor = mcp_server.monitors[agent_id]
                    state = monitor.state
                    monitor_state = {
                        "regime": state.regime,
                        "coherence": round(state.coherence, 6),
                        "energy": round(state.E, 6),  # E, I, S, V are uppercase
                        "entropy": round(state.S, 6),
                        "void_active": state.void_active,
                    }

                # CAPTURE BASIC PROVENANCE
                provenance = {
                    "system_version": system_version,
                    "agent_state": {
                        "status": meta.status,
                        "health": meta.health_status,
                        "total_updates": meta.total_updates,
                        **monitor_state
                    },
                    "captured_at": _utc_now_iso(),
                }

                # Bug A fix 2026-04-25: pin writer attribution at write time.
                # `agent_id` (UUID) is stable across resumed sessions; the
                # display_name and active session_id are not. Without this,
                # search/get rebuilds `by:` from current metadata and erases
                # which session/label actually authored each row.
                writer_label = (
                    getattr(meta, "display_name", None)
                    or getattr(meta, "label", None)
                    or getattr(meta, "structured_id", None)
                    or agent_id
                )
                provenance["writer_label_at_write"] = writer_label
                writer_session = arguments.get("client_session_id")
                if not writer_session:
                    try:
                        from ..context import get_context_client_session_id
                        writer_session = get_context_client_session_id()
                    except Exception:
                        writer_session = None
                if writer_session:
                    provenance["writer_session_id_at_write"] = writer_session

            provenance_chain = await _build_s7_provenance_chain_with_fallback(
                agent_id,
                meta,
                _get_lineage,
            )
            from src.provenance_context import classify_fork_for_s22_context
            episode_fork_kind, identity_lineage_fork = classify_fork_for_s22_context(
                meta, agent_id
            )
            s22_context = build_s22_write_context(
                arguments,
                meta=meta,
                context_source="knowledge.store",
                default_governance_mode="explicit",
                episode_fork_kind=episode_fork_kind,
                identity_lineage_fork=identity_lineage_fork,
            )
            provenance = attach_s22_context(provenance, s22_context)
        except Exception as e:
            logger.debug(f"Could not capture provenance: {e}")  # Non-critical

        # Ensure system_version is always in provenance, even if agent metadata was unavailable
        if provenance is None:
            provenance = {"system_version": system_version, "captured_at": _utc_now_iso()}
        elif "system_version" not in provenance:
            provenance["system_version"] = system_version
        # Tag write origin so list/stats can split caller-intentional writes
        # from automation traffic (#165). Single-discovery store path is the
        # canonical "explicit" write surface.
        from src.knowledge_graph import tag_provenance_source as _tag_src
        provenance = _tag_src(provenance, "explicit_store")

        # Parse confidence if provided
        raw_confidence = arguments.get("confidence")
        parsed_confidence = None
        if raw_confidence is not None:
            try:
                parsed_confidence = float(raw_confidence)
                parsed_confidence = max(0.0, min(1.0, parsed_confidence))
            except (ValueError, TypeError):
                pass

        discovery = DiscoveryNode(
            id=discovery_id,
            agent_id=agent_id,
            type=discovery_type,
            summary=summary,
            details=raw_details,
            tags=normalize_tags(arguments.get("tags", [])),
            severity=severity,
            status=arguments.get("status", "open"),
            response_to=response_to,
            references_files=arguments.get("related_files", []),
            provenance=provenance,
            provenance_chain=provenance_chain,
            confidence=parsed_confidence
        )

        # KG hygiene v1: supersedes pre-flight.
        # Look up predecessor; veto if permanent (would silently downgrade an
        # ADR/learning/etc.); warn if missing (new entry still stored, but no
        # supersession applied). Veto must run BEFORE add_discovery so a
        # rejected supersession does not orphan the new entry.
        supersedes_target = None
        supersedes_warning = None
        if supersedes_id:
            supersedes_target = await graph.get_discovery(supersedes_id)
            if supersedes_target is None:
                supersedes_warning = (
                    f"supersedes target '{supersedes_id}' not found; "
                    f"new discovery will be stored without flip"
                )
            else:
                from src.knowledge_graph_lifecycle import KnowledgeGraphLifecycle
                lifecycle = KnowledgeGraphLifecycle()
                if lifecycle.get_lifecycle_policy(supersedes_target) == "permanent":
                    return [error_response(
                        f"Cannot supersede permanent discovery '{supersedes_id}' "
                        f"(type={supersedes_target.type}, tags={supersedes_target.tags}). "
                        "Use knowledge(action='update') with explicit operator action to override."
                    )]

        # CONFIDENCE CROSS-CHECK: Clamp to agent coherence + 0.3
        await _clamp_confidence_to_coherence(discovery, agent_id)

        # Find similar discoveries (fast with tag index) - DEFAULT: true for better linking
        similar_discoveries = []
        if arguments.get("auto_link_related", True):  # Default to true - new graph uses indexes (fast)
            similar = await graph.find_similar(discovery, limit=5)
            discovery.related_to = [s.id for s in similar]
            similar_discoveries = [s.to_dict(include_details=False) for s in similar]
        
        # SECURITY: Require session ownership for high-severity discoveries (UUID-based auth, Dec 2025)
        # This prevents unauthorized agents from storing critical security issues
        if discovery.severity in ["high", "critical"]:
            from ..utils import verify_agent_ownership
            if not verify_agent_ownership(agent_id, arguments):
                return [error_response(
                    "Authentication required for high-severity discoveries.",
                    error_code="AUTH_REQUIRED",
                    error_category="auth_error",
                    recovery={
                        "action": "Ensure your session is bound to this agent",
                        "related_tools": ["identity"],
                        "workflow": "Identity auto-binds on first tool call. Use identity() to check binding."
                    }
                )]
        
        # HUMAN REVIEW FLAGGING: Flag high-severity discoveries for review
        requires_review = discovery.severity in ["high", "critical"]
        
        # Add to graph (fast, non-blocking)
        await graph.add_discovery(discovery)
        await _broadcast_knowledge_write(discovery, agent_id)

        # KG hygiene v1: flip predecessor status now that the new entry exists.
        # Pre-flight already verified the predecessor exists and is non-permanent.
        if supersedes_id and supersedes_target is not None:
            await graph.update_discovery(supersedes_id, {
                "status": "superseded",
                "superseded_by": discovery_id,
                "updated_at": datetime.now().isoformat(),
            })

        # v2.5.3: Resolve UUID to display name for human-readable output
        agent_display = arguments.get("_agent_display") or _resolve_agent_display(agent_id)
        display_name = agent_display.get("display_name", agent_id)

        response = {
            "message": f"Discovery stored for agent '{display_name}'",
            "discovery_id": discovery_id,
            "agent": agent_display,  # Include full display info
            "discovery": discovery.to_dict(include_details=False)  # Summary only in response
        }

        if is_anonymous_writer:
            response["agent_mode"] = "anonymous"
            response["_identity_hint"] = (
                "Stored under a lightweight anonymous writer ID. "
                "Bind an identity first if you want authorship continuity."
            )

        # KG loop closure: remind agents to resolve when addressed
        response["_resolve_when_done"] = f"When this is addressed, close the loop: knowledge(action='update', discovery_id='{discovery_id}', status='resolved')"

        # KG hygiene v1: surface supersession outcome
        if supersedes_id:
            if supersedes_target is not None:
                response["superseded"] = supersedes_id
            elif supersedes_warning:
                response["_supersedes_warning"] = supersedes_warning

        # UX FIX (Feb 2026): Include warning if display_name was auto-generated
        if display_name_warning:
            response["_name_hint"] = display_name_warning

        # Add truncation warning if content was truncated (v2.5.0+)
        if truncation_info:
            response["_truncated"] = truncation_info
            response["_tip"] = "Content was truncated. For longer content, split into multiple discoveries or use details field (5000 char limit)."

        # Add human review flag if needed
        if requires_review:
            response["human_review_required"] = True
            response["review_message"] = f"High-severity discovery ({discovery.severity}) - please review for accuracy and safety"
        
        if similar_discoveries:
            response["related_discoveries"] = similar_discoveries
            # Consolidation hint: flag when the same issue keeps being rediscovered
            open_similar = [s for s in similar if s.status == "open"]
            if len(open_similar) >= 3:
                unique_agents = {s.agent_id for s in open_similar}
                response["consolidation_hint"] = (
                    f"This issue has been found {len(open_similar)} times by {len(unique_agents)} agent(s), "
                    f"all still open. Consider superseding older entries or resolving them."
                )

        return success_response(response, arguments=arguments)
        
    except ValueError as e:
        # Handle rate limiting errors from graph backend (efficient O(1) check)
        error_msg = str(e)
        if "rate limit" in error_msg.lower() or "Rate limit" in error_msg:
            return [error_response(
                error_msg,
                recovery={
                    "action": "Wait before storing more discoveries, or reduce batch size",
                    "related_tools": [KNOWLEDGE_SEARCH_TOOL]
                }
            )]
        # Other ValueError (validation errors, etc.)
        return [error_response(error_msg)]
    except Exception as e:
        return [error_response(f"Failed to store knowledge: {str(e)}")]

@mcp_tool("search_knowledge_graph", timeout=15.0, rate_limit_exempt=True)
async def handle_search_knowledge_graph(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Search knowledge graph (indexed filters; optional FTS query).

    Use include_provenance=True to get provenance and lineage chain for each discovery.
    """
    # MAGNET PATTERN: Accept fuzzy inputs (search, term, find → query)
    arguments = apply_param_aliases("search_knowledge_graph", arguments)

    try:
        graph = await get_knowledge_graph()

        limit = arguments.get("limit") or config.KNOWLEDGE_QUERY_DEFAULT_LIMIT
        include_details = arguments.get("include_details", False)
        include_provenance = arguments.get("include_provenance", False)  # Merged from query_provenance

        # LLM delegation: synthesize results via local model
        # When enabled, uses Ollama to summarize key patterns from multiple discoveries
        synthesize = arguments.get("synthesize", False)

        # Optional full-text query (PostgreSQL FTS or AGE)
        # Accept both "query" and "text" as parameter names for better UX
        query_text = arguments.get("query") or arguments.get("text")
        agent_id = arguments.get("agent_id")
        # Force a specific retrieval mode. 'auto' (default) preserves the
        # historical heuristic; explicit values fail honestly instead of silently
        # routing to FTS — the whole point of this surface is making the routing
        # decision visible (issue #165).
        search_mode_param = (arguments.get("search_mode") or "auto").lower()
        if search_mode_param not in {"auto", "fts", "semantic", "hybrid"}:
            return [error_response(
                f"Invalid search_mode {search_mode_param!r}; "
                "expected one of: auto, fts, semantic, hybrid"
            )]
        # FTS boolean operator: None = handler picks AND with OR-on-zero
        # fallback. "AND" or "OR" force that operator with no fallback.
        operator_param_raw = arguments.get("operator")
        if operator_param_raw is None:
            operator_forced: Optional[str] = None
        else:
            op_upper = str(operator_param_raw).upper()
            if op_upper not in {"AND", "OR"}:
                return [error_response(
                    f"Invalid operator {operator_param_raw!r}; expected 'AND' or 'OR'"
                )]
            operator_forced = op_upper
        # Labels to exclude (e.g. ["Vigil"]) — lets the dashboard hide
        # janitorial residents from the default Discoveries feed. Resolved to
        # agent_ids below, applied as a post-query filter over `results`.
        exclude_labels_raw = arguments.get("exclude_agent_labels") or []
        exclude_labels_lc: set[str] = {
            str(lbl).strip().lower()
            for lbl in exclude_labels_raw
            if str(lbl).strip()
        } if isinstance(exclude_labels_raw, (list, tuple)) else set()
        tags = normalize_tags(arguments.get("tags", [])) or None
        dtype = arguments.get("discovery_type")
        severity = arguments.get("severity")
        status = arguments.get("status")
        # Back-compat alias: older schemas/docs used "active".
        if isinstance(status, str) and status.lower() == "active":
            status = "open"
        # Default: exclude archived entries unless explicitly requested
        include_archived = arguments.get("include_archived", False)

        # Track semantic scores if semantic search is used
        semantic_scores_dict = {}
        rerank_scores_dict = {}
        rrf_scores_dict = {}
        search_degraded_warning = None
        hybrid_skipped_reason = None
        fts_fallback_skipped_reason = None
        query_terms = str(query_text).split() if query_text else []
        query_term_count = len(query_terms)
        # Broad agent-generated queries can spend the whole tool budget in
        # hybrid fan-out or automatic OR recall fallback. Keep explicit caller
        # intent available, but make auto mode cheap by default.
        complex_query_term_limit = 4

        # Phase 3: cross-encoder reranker. When enabled, first-stage retrieval
        # fetches a wider pool (up to rerank_pool_size) so the reranker has
        # something to work with before we truncate to the caller's `limit`.
        from src.reranker import reranker_enabled as _reranker_enabled
        rerank_on = _reranker_enabled()
        rerank_pool_size = 50 if rerank_on else 0
        first_stage_limit = max(limit * 2, rerank_pool_size) if rerank_on else limit * 2

        # Phase 4: hybrid RRF fusion. When enabled, fetch semantic + FTS in
        # parallel and fuse via Reciprocal Rank Fusion (k=60). Tags, if passed,
        # act as a small boost in the fused space rather than a hard post-filter.
        from src.retrieval import (
            hybrid_enabled as _hybrid_enabled,
            graph_expansion_enabled as _graph_expansion_enabled,
            rrf_fuse,
            apply_tag_boost,
            expand_with_neighbors,
        )
        hybrid_on = _hybrid_enabled()
        graph_expand_on = _graph_expansion_enabled()

        t0 = time.perf_counter()
        # Track why the chosen mode is what it is — surfaced in the response so
        # callers can tell a configured-FTS run from a silent-degrade-to-FTS run
        # (issue #165).
        semantic_skipped_reason: Optional[str] = None
        # FTS operator observability (#165 part 2). None when no FTS ran;
        # "AND"/"OR" otherwise. fallback_used flips true when AND returned zero
        # and we retried with OR.
        fts_operator_used: Optional[str] = None
        fts_fallback_used = False
        if query_text:
            has_semantic = hasattr(graph, "semantic_search")
            has_fts = hasattr(graph, "full_text_search")
            backend_label = graph.__class__.__name__
            explicit_semantic = arguments.get("semantic")

            # Forced modes: error honestly when the backend can't deliver.
            # 'auto' falls back to FTS but records the reason. 'fts' is always
            # OK as long as the backend exposes full_text_search.
            if search_mode_param == "semantic" and not has_semantic:
                return [error_response(
                    f"search_mode=semantic requires a backend with semantic_search; "
                    f"active backend {backend_label} has none. "
                    f"Use search_mode=fts, or set UNITARES_KNOWLEDGE_BACKEND=age."
                )]
            if search_mode_param == "hybrid" and not (has_semantic and has_fts):
                missing = []
                if not has_semantic:
                    missing.append("semantic_search")
                if not has_fts:
                    missing.append("full_text_search")
                return [error_response(
                    f"search_mode=hybrid requires both semantic and FTS; "
                    f"active backend {backend_label} is missing {', '.join(missing)}."
                )]
            if search_mode_param == "fts" and not has_fts:
                return [error_response(
                    f"search_mode=fts requires full_text_search; "
                    f"active backend {backend_label} has none."
                )]

            # Decide whether semantic should run.
            if search_mode_param in ("semantic", "hybrid"):
                use_semantic = True
            elif search_mode_param == "fts":
                use_semantic = False
                if has_semantic:
                    semantic_skipped_reason = "caller forced search_mode=fts"
            elif explicit_semantic is False:
                use_semantic = False
                semantic_skipped_reason = "caller passed semantic=false"
            elif explicit_semantic is True:
                if not has_semantic:
                    return [error_response(
                        f"semantic=true requires a backend with semantic_search; "
                        f"active backend {backend_label} has none."
                    )]
                use_semantic = True
            else:
                # auto: prefer semantic when the backend supports it
                use_semantic = has_semantic
                if not has_semantic:
                    semantic_skipped_reason = (
                        f"backend {backend_label} has no semantic_search "
                        "(set UNITARES_KNOWLEDGE_BACKEND=age to enable)"
                    )

            # Phase 4 hybrid path: only when caller forces hybrid, OR (auto + flag on + capable).
            if search_mode_param == "hybrid":
                hybrid_path = True  # already validated has_semantic and has_fts above
            else:
                hybrid_path = (
                    search_mode_param == "auto" and hybrid_on and use_semantic and has_fts
                )
            if (
                hybrid_path
                and search_mode_param == "auto"
                and query_term_count > complex_query_term_limit
            ):
                hybrid_path = False
                hybrid_skipped_reason = (
                    f"auto hybrid skipped for {query_term_count}-term query "
                    f"(limit {complex_query_term_limit}); use search_mode='hybrid' "
                    "to force RRF fusion"
                )
            if hybrid_path:
                import asyncio as _asyncio
                min_similarity = arguments.get("min_similarity", 0.3)
                hybrid_fetch_limit = max(first_stage_limit, 50)
                sem_task = graph.semantic_search(
                    str(query_text), limit=hybrid_fetch_limit, min_similarity=min_similarity
                )
                # Hybrid uses the caller's operator if forced, else AND. We
                # don't AND→OR fallback inside hybrid because semantic+FTS
                # already cover the recall/precision tradeoff via RRF.
                hybrid_fts_op = operator_forced or "AND"
                fts_task = graph.full_text_search(
                    str(query_text), limit=hybrid_fetch_limit, operator=hybrid_fts_op,
                )
                fts_operator_used = hybrid_fts_op
                sem_raw, fts_raw = await _asyncio.gather(sem_task, fts_task)
                sem_res = []
                if isinstance(sem_raw, tuple) and len(sem_raw) == 2 and isinstance(sem_raw[1], dict):
                    search_degraded_warning = (
                        f"Semantic search unavailable: {sem_raw[1].get('message', 'unknown error')}. "
                        f"Falling back to FTS-only in fusion."
                    )
                    logger.warning(f"[KG_SEARCH] {search_degraded_warning}")
                else:
                    sem_res = list(sem_raw)
                fts_res = list(fts_raw)

                sem_ids = [d.id for d, _ in sem_res]
                fts_ids = [d.id for d in fts_res]
                fused = rrf_fuse([sem_ids, fts_ids], k=60)

                pool: Dict[str, Any] = {d.id: d for d, _ in sem_res}
                for d in fts_res:
                    pool.setdefault(d.id, d)

                if tags:
                    doc_tags_map = {doc_id: (doc.tags or []) for doc_id, doc in pool.items()}
                    fused = apply_tag_boost(fused, doc_tags_map, tags)

                # Phase 5: 1-hop graph expansion. Top seeds pull their typed-edge
                # neighbors (related_to / responses_from / response_to) into the
                # pool at a discounted score.
                if graph_expand_on:
                    seed_neighbors: Dict[str, set] = {}
                    for seed_id, _ in fused[:10]:
                        seed_doc = pool.get(seed_id)
                        if seed_doc is None:
                            continue
                        nbrs: set = set()
                        nbrs.update(seed_doc.related_to or [])
                        nbrs.update(getattr(seed_doc, "responses_from", None) or [])
                        if seed_doc.response_to:
                            nbrs.add(seed_doc.response_to.discovery_id)
                        nbrs.discard(seed_id)
                        seed_neighbors[seed_id] = nbrs

                    fused = expand_with_neighbors(
                        fused, seed_neighbors, edge_weight=0.5, max_seeds=10,
                    )
                    missing_ids = [did for did, _ in fused if did not in pool]
                    if missing_ids:
                        # Parallel fetch of neighbor docs not already in the
                        # semantic+FTS pool. Sequential `await` here was the
                        # in-handler floor on the 60× KG-call amplification
                        # measured 2026-05-04 (per-call 21–71ms, in-handler
                        # ~4,464ms). Each get_discovery does 2 PG round-trips
                        # (row + backlinks); 30 × 2 = 60 sequential awaits is
                        # what asyncio.gather collapses to ~pool-size waves.
                        # Same shape as PR #350/#360 — Python-fixable.
                        capped = missing_ids[:30]
                        results = await _asyncio.gather(
                            *(graph.get_discovery(nid) for nid in capped),
                            return_exceptions=True,
                        )
                        for nid, doc in zip(capped, results):
                            if isinstance(doc, Exception):
                                logger.debug(
                                    f"[KG_SEARCH] neighbor fetch failed for "
                                    f"{nid[:8]}...: {doc}"
                                )
                                continue
                            if doc is not None:
                                pool[nid] = doc

                candidates = [pool[did] for did, _ in fused if did in pool]
                semantic_scores_dict = {d.id: score for d, score in sem_res}
                rrf_scores_dict = {did: score for did, score in fused}
                search_mode = "hybrid_rrf_graph" if graph_expand_on else "hybrid_rrf"
            elif use_semantic:
                # Semantic search using vector embeddings
                # Default 0.3 for precision; auto-fallback to 0.2 catches edge cases
                min_similarity = arguments.get("min_similarity", 0.3)
                semantic_results = await graph.semantic_search(
                    str(query_text),
                    limit=first_stage_limit,  # wider pool when reranker is on
                    min_similarity=min_similarity
                )
                # Check for degraded response: ([], error_info_dict)
                if (isinstance(semantic_results, tuple) and len(semantic_results) == 2
                        and isinstance(semantic_results[1], dict)):
                    _results, error_info = semantic_results
                    search_degraded_warning = (
                        f"Semantic search unavailable: {error_info.get('message', 'unknown error')}. "
                        f"Falling back to text search."
                    )
                    logger.warning(f"[KG_SEARCH] {search_degraded_warning}")
                    # Fall through to FTS/substring fallback below
                    use_semantic = False
                else:
                    candidates = [d for d, _ in semantic_results]
                    semantic_scores_dict = {d.id: score for d, score in semantic_results}
                    search_mode = "semantic"
            if not hybrid_path and not use_semantic and hasattr(graph, "full_text_search"):
                # Prefer DB-native FTS when available (fallback from degraded semantic or no semantic)
                # When reranker is on, use a wider pool so the cross-encoder has candidates.
                base_fts_limit = int(min(max(limit * 5, limit), 500))
                candidate_limit = max(base_fts_limit, rerank_pool_size) if rerank_on else base_fts_limit
                primary_op = operator_forced or "AND"
                candidates = await graph.full_text_search(
                    str(query_text), limit=candidate_limit, operator=primary_op,
                )
                fts_operator_used = primary_op
                # AND→OR fallback (#165): when caller didn't force an operator
                # and AND returned nothing, retry with OR. Marks fts_fallback_used
                # so the caller can tell broad-recall results apart from
                # precision-first results.
                if (
                    not candidates
                    and operator_forced is None
                    and primary_op == "AND"
                    and query_term_count > 1
                ):
                    if query_term_count <= complex_query_term_limit:
                        candidates = await graph.full_text_search(
                            str(query_text), limit=candidate_limit, operator="OR",
                        )
                        if candidates:
                            fts_operator_used = "OR"
                            fts_fallback_used = True
                    else:
                        fts_fallback_skipped_reason = (
                            f"automatic OR fallback skipped for {query_term_count}-term "
                            f"query (limit {complex_query_term_limit}); pass operator='OR' "
                            "to request broad recall"
                        )
                search_mode = "fts"
            elif not hybrid_path and not use_semantic:
                # JSON backend fallback: bounded scan of most recent entries.
                # 200 balances coverage vs context size (post-hoc substring filter
                # reduces this to at most `limit` results).
                candidates = await graph.query(limit=200)
                search_mode = "substring_scan"

            # For FTS/semantic: trust the search engine's ranking, only apply metadata filters
            # For substring_scan: also require query term matches (OR-default)
            filtered = []
            q_terms = str(query_text).lower().split() if search_mode == "substring_scan" else None

            # When reranker OR hybrid is on, keep up to rerank_pool_size candidates
            # so the cross-encoder / hybrid fuse sees more than the first-stage top-limit.
            filter_cap = rerank_pool_size if rerank_on else (50 if hybrid_on else limit)

            for d in candidates:
                # Substring filter only for non-FTS backends (OR-default)
                if q_terms:
                    tags_str = " ".join(d.tags or [])
                    hay = ((d.summary or "") + "\n" + (d.details or "") + "\n" + tags_str).lower()
                    if not any(term in hay for term in q_terms):
                        continue
                # Metadata filters apply to all modes
                if agent_id and d.agent_id != agent_id:
                    continue
                if dtype and d.type != dtype:
                    continue
                if severity and d.severity != severity:
                    continue
                if status and d.status != status:
                    continue
                # Exclude archived entries by default (unless status filter or include_archived)
                if not status and not include_archived and d.status == "archived":
                    continue
                if tags and not search_mode.startswith("hybrid_rrf"):
                    # In hybrid mode, tags are a score boost in RRF space (handled
                    # upstream via apply_tag_boost). Everywhere else, they remain a
                    # hard post-filter — preserves pre-Phase-4 behavior.
                    d_tags = set(d.tags or [])
                    if not any(t in d_tags for t in tags):
                        continue
                filtered.append(d)
                if len(filtered) >= filter_cap:
                    break

            # Phase 3: cross-encoder rerank. Score (query, doc) pairs jointly
            # and reorder. Reranker input text mirrors what the embedder sees.
            if rerank_on and filtered:
                try:
                    from src.reranker import rerank as _rerank
                    pairs = [
                        (d.id, f"{d.summary}\n{(d.details or '')[:2000]}")
                        for d in filtered
                    ]
                    reranked = await _rerank(str(query_text), pairs, top_k=limit,
                                             max_rerank_size=rerank_pool_size)
                    rerank_scores_dict = {doc_id: score for doc_id, score in reranked}
                    id_to_doc = {d.id: d for d in filtered}
                    filtered = [id_to_doc[doc_id] for doc_id, _ in reranked if doc_id in id_to_doc]
                    search_mode = search_mode + "_reranked" if search_mode else "reranked"
                except Exception as exc:
                    logger.warning(f"[KG_SEARCH] reranker failed; keeping first-stage order: {exc}")
                    filtered = filtered[:limit]
            else:
                filtered = filtered[:limit]

            results = filtered
            # search_mode already set above
            # operator_used reports what the FTS layer actually ran. For
            # non-FTS modes (semantic, hybrid, substring) this stays "N/A" —
            # boolean operators only apply to the FTS query string. (#165)
            if fts_operator_used and len(query_terms) > 1:
                operator_used = fts_operator_used
            elif len(query_terms) > 1:
                operator_used = "N/A"  # semantic / hybrid / substring
            else:
                operator_used = "N/A"  # single-term query
            fields_searched = ["summary", "details", "tags"]
        else:
            # Indexed filter query (fast)
            # Push exclude_archived into the query so LIMIT applies after filtering.
            # Without this, LIMIT grabs N most recent (mostly archived junk), then
            # post-hoc filtering removes them, returning far fewer than N results.
            should_exclude_archived = not status and not include_archived
            results = await graph.query(
                agent_id=agent_id,
                tags=tags,
                type=dtype,
                severity=severity,
                status=status,
                limit=limit,
                exclude_archived=should_exclude_archived,
            )
            search_mode = "indexed_filters"
            operator_used = "N/A"  # No text search, just filters
            fields_searched = []
            if agent_id:
                fields_searched.append("agent_id")
            if tags:
                fields_searched.append("tags")
            if dtype:
                fields_searched.append("type")
            if severity:
                fields_searched.append("severity")
            if status:
                fields_searched.append("status")
        
        # UX FIX: Auto-retry with fallback if 0 results and query provided
        # Make fallback behavior explicit upfront
        fallback_used = False
        fallback_explanation = None
        if len(results) == 0 and query_text and search_mode in ["fts", "semantic"]:
            # Strategy 1: If semantic search returned 0, try FTS (more permissive)
            if search_mode == "semantic" and hasattr(graph, "full_text_search"):
                try:
                    logger.debug(f"Semantic search returned 0 results, falling back to FTS for '{query_text}'")
                    primary_op = operator_forced or "AND"
                    fts_candidates = await graph.full_text_search(
                        str(query_text), limit=limit * 2, operator=primary_op,
                    )
                    semantic_fallback_op = primary_op
                    semantic_fallback_or_retry = False
                    if (
                        not fts_candidates
                        and operator_forced is None
                        and primary_op == "AND"
                        and query_term_count > 1
                    ):
                        if query_term_count <= complex_query_term_limit:
                            fts_candidates = await graph.full_text_search(
                                str(query_text), limit=limit * 2, operator="OR",
                            )
                            if fts_candidates:
                                semantic_fallback_op = "OR"
                                semantic_fallback_or_retry = True
                        else:
                            fts_fallback_skipped_reason = (
                                f"automatic OR fallback skipped for {query_term_count}-term "
                                f"query (limit {complex_query_term_limit}); pass operator='OR' "
                                "to request broad recall"
                            )
                    # Apply same filters
                    for d in fts_candidates:
                        if agent_id and d.agent_id != agent_id:
                            continue
                        if dtype and d.type != dtype:
                            continue
                        if severity and d.severity != severity:
                            continue
                        if status and d.status != status:
                            continue
                        if not status and not include_archived and d.status == "archived":
                            continue
                        if tags:
                            d_tags = set(d.tags or [])
                            if not any(t in d_tags for t in tags):
                                continue
                        results.append(d)
                        if len(results) >= limit:
                            break
                    if len(results) > 0:
                        fallback_used = True
                        search_mode = "semantic_fallback_fts"
                        fts_operator_used = semantic_fallback_op
                        fts_fallback_used = semantic_fallback_or_retry
                        fallback_explanation = (
                            f"Semantic search found no concepts similar to '{query_text}' "
                            f"(similarity threshold: {min_similarity}). "
                            f"Falling back to keyword search (FTS, operator={semantic_fallback_op}) "
                            f"for exact term matching."
                        )
                except Exception as e:
                    logger.debug(f"Semantic→FTS fallback failed: {e}")

            # Strategy 2 (removed): Individual-term FTS fallback is no longer
            # needed. As of #165, the FTS path runs AND first and automatically
            # retries with OR on zero hits (see fts_fallback_used in response),
            # which subsumes the per-term retry strategy.

            # Strategy 3 (removed 2026-04-20): Previously retried semantic search at
            # threshold 0.2, which is near cosine noise floor for our embedder. This
            # confidently returned random-looking results on genuine misses, which is
            # worse than returning nothing — callers couldn't tell a real hit from a
            # noise hit. If semantic + FTS both return zero, an honest empty result
            # is the correct answer. Tracked in docs/plans/2026-04-20-kg-retrieval-rebuild.md.
        
        dt_ms = (time.perf_counter() - t0) * 1000.0
        record_ms(f"knowledge.search.{search_mode}", dt_ms)

        # Post-query exclude-by-label filter. Kept here (not in the DB query)
        # so it composes with all retrieval modes — FTS, semantic, hybrid RRF,
        # graph expansion — without each branch needing to know the filter.
        if exclude_labels_lc:
            filtered = []
            for d in results:
                agent_display = _resolve_agent_display(d.agent_id)
                display_name = agent_display.get("display_name", d.agent_id) or ""
                if str(display_name).strip().lower() in exclude_labels_lc:
                    continue
                filtered.append(d)
            results = filtered

        # Auto-include details when result set is small (saves a round-trip)
        auto_details = not include_details and 0 < len(results) <= 3
        if auto_details:
            include_details = True

        # Build discovery list with optional provenance
        # UX FIX (Dec 2025): Display name FIRST for human readability
        # Format: {"by": "DisplayName", "summary": "...", ...}
        current_server_version = getattr(mcp_server, "SERVER_VERSION", "unknown")
        discovery_list = []
        for d in results:
            # Bug A fix 2026-04-25: prefer write-time writer label from
            # provenance over live resolve. Multiple sessions may resume
            # the same agent UUID; live resolve would rewrite past `by:`
            # values to the current session's display_name.
            prov = d.provenance if isinstance(d.provenance, dict) else None
            display_name = (prov or {}).get("writer_label_at_write")
            if not display_name:
                agent_display = _resolve_agent_display(d.agent_id)
                display_name = agent_display.get("display_name", d.agent_id)

            # Build dict with display_name first for prominence
            d_dict = {
                "by": display_name,  # WHO - first for attribution
                "summary": d.summary,  # WHAT - second for context
            }

            # Surface write-time session id when present (audit trail)
            session_at_write = (prov or {}).get("writer_session_id_at_write")
            if session_at_write:
                d_dict["session_id_at_write"] = session_at_write

            # Add remaining fields from discovery
            full_dict = d.to_dict(include_details=include_details)
            d_dict["id"] = full_dict.get("id")
            d_dict["type"] = full_dict.get("type")
            d_dict["status"] = full_dict.get("status")
            d_dict["tags"] = full_dict.get("tags", [])
            d_dict["created_at"] = full_dict.get("created_at")

            # Include details if requested
            if include_details and full_dict.get("details"):
                d_dict["details"] = full_dict.get("details")

            # Keep agent_id for internal reference (de-emphasized)
            d_dict["_agent_id"] = d.agent_id

            # Surface system_version from provenance (Task 1: KG version coupling)
            if prov:
                d_dict["system_version"] = prov.get("system_version")
            else:
                d_dict["system_version"] = None  # Pre-v2.8.0 discovery

            # Staleness warning (Task 3: stale entry detection)
            if d.status == "open":
                _staleness = _compute_staleness_warning(d, current_server_version)
                if _staleness:
                    d_dict["staleness_warning"] = _staleness

            if include_provenance:
                d_dict["provenance"] = d.provenance
                if d.provenance_chain:
                    d_dict["provenance_chain"] = d.provenance_chain
            discovery_list.append(d_dict)
        
        # Include similarity scores for semantic search
        response_data = {
            "search_mode_used": search_mode,
            "search_mode_requested": search_mode_param,
            "operator_used": operator_used,
            "fields_searched": fields_searched,
            "query": query_text,
            "discoveries": discovery_list,
            "count": len(results),
            "message": f"Found {len(results)} discovery(ies)" + (" (details auto-included for small result set)" if auto_details else "" if include_details else " (summaries only)")
        }
        if semantic_skipped_reason:
            response_data["semantic_skipped_reason"] = semantic_skipped_reason
        # FTS operator visibility (#165 part 2). Only populate when FTS actually
        # ran — for semantic/hybrid/substring runs these stay absent so callers
        # know the boolean operator was not the controlling factor.
        if fts_operator_used:
            response_data["fts_operator_used"] = fts_operator_used
            response_data["fts_fallback_used"] = fts_fallback_used

        # Surface semantic search degradation to caller
        if search_degraded_warning:
            response_data["search_degraded"] = True
            response_data["search_degraded_message"] = search_degraded_warning
        if hybrid_skipped_reason:
            response_data["hybrid_skipped_reason"] = hybrid_skipped_reason
        if fts_fallback_skipped_reason:
            response_data["fts_fallback_skipped_reason"] = fts_fallback_skipped_reason

        # UX FIX: Make fallback behavior explicit and transparent
        if fallback_used:
            response_data["fallback_used"] = True
            response_data["fallback_message"] = fallback_explanation or "No exact matches found. Retried with individual terms (OR operator)."
            response_data["fallback_terms"] = str(query_text).split()[:3] if query_text else []
        
        # UX FIX: Add contextual helpful hints for empty results
        if len(results) == 0:
            hints = []
            # Count words properly (split on spaces, also handle underscores as word separators)
            if query_text:
                query_str = str(query_text)
                # Replace underscores with spaces for word counting
                query_normalized = query_str.replace("_", " ").replace("-", " ")
                query_words = len([w for w in query_normalized.split() if w.strip()])
            else:
                query_words = 0
            
            if query_text:
                # Contextual suggestions based on query characteristics
                if query_words >= 5:
                    # Long, specific query - suggest semantic search prominently
                    hints.append(f"Long query ({query_words} words) - try semantic search: knowledge(action='search', query='{query_text}', semantic=true)")
                    hints.append(f"Or broaden to key concepts: knowledge(action='search', query='{', '.join(str(query_text).split()[:3])}')")
                elif query_words >= 2:
                    # Multi-word query - suggest semantic or broader terms
                    hints.append(f"Multi-word query - try semantic search (semantic=true) for conceptual matching")
                    hints.append(f"Or search individual terms: {', '.join(str(query_text).split()[:3])}")
                else:
                    # Single word - suggest broadening or tags
                    hints.append(f"Single term '{query_text}' - try broader search or use tags")
                    hints.append(f"Try: knowledge(action='search', tags=['{query_text}']) or broaden query")
                
                # Always suggest tag search as alternative
                hints.append("Alternative: Search by tags instead (knowledge(action='search', tags=['tag1', 'tag2']))")
            
            # Filter-specific suggestions
            if agent_id:
                hints.append(f"Filter active: agent_id='{agent_id[:20]}...' - remove to search across all agents")
            if tags:
                hints.append(f"Filter active: {len(tags)} tag(s) - remove or use fewer tags for broader results")
            if dtype:
                hints.append(f"Filter active: type='{dtype}' - remove to search all discovery types")
            if severity:
                hints.append(f"Filter active: severity='{severity}' - remove to search all severities")
            
            if hints:
                response_data["empty_results_hints"] = hints
                # Prioritize most actionable hint first
                primary_hint = hints[0] if hints else "Try adjusting your search parameters"
                response_data["tip"] = f"No results found. {primary_hint}"
                response_data["all_suggestions"] = hints  # Keep all hints available
        
        # UX FIX: Document operator behavior upfront for multi-term queries
        if query_text and len(str(query_text).split()) > 1:
            if search_mode == "fts" and not fallback_used:
                response_data["operator_note"] = (
                    f"Multi-term FTS ran with operator={fts_operator_used or operator_used}. "
                    "Use operator='OR' for broader recall, or tags/filters for tighter scope."
                )
            elif search_mode == "semantic":
                response_data["operator_note"] = "Semantic search considers all terms together (conceptual similarity, not keyword matching)."

        # Visibility hints about options (v2.5.0+)
        if not include_details:
            response_data["_tip"] = "Add include_details=true to expand all results inline (knowledge(action='search', include_details=true))"
        if len(results) == limit:
            response_data["_more_available"] = f"Results may be limited to {limit}. Use limit=N (max 100) to get more."
        
        # Surface similarity scores whenever we have them, regardless of search
        # mode — helps agents calibrate "is this a real match or noise?"
        if semantic_scores_dict and query_text:
            similarity_scores = {
                d.id: round(semantic_scores_dict[d.id], 3)
                for d in results
                if d.id in semantic_scores_dict
            }
            if similarity_scores:
                response_data["similarity_scores"] = similarity_scores

        # Surface rerank scores when the cross-encoder ran, so agents can see
        # the ordering came from joint (query, doc) scoring rather than cosine.
        if rerank_scores_dict and query_text:
            rerank_scores = {
                d.id: round(rerank_scores_dict[d.id], 3)
                for d in results
                if d.id in rerank_scores_dict
            }
            if rerank_scores:
                response_data["rerank_scores"] = rerank_scores

        # Surface RRF scores when hybrid fusion ran. These are on a different
        # scale than cosine similarity (typically 0.01-0.05 per rank-1 hit),
        # but comparable across queries and usefully diagnostic.
        if rrf_scores_dict and query_text:
            rrf_scores = {
                d.id: round(rrf_scores_dict[d.id], 4)
                for d in results
                if d.id in rrf_scores_dict
            }
            if rrf_scores:
                response_data["rrf_scores"] = rrf_scores


        # UX FIX (Dec 2025): Add helpful hint when substring scan returns no results
        if search_mode == "substring_scan" and len(results) == 0 and query_text:
            response_data["search_hint"] = (
                "No results with substring matching. Try: "
                "1) Use specific tags: tags=['identity', 'philosophy'] "
                "2) Search by discovery_type: discovery_type='insight' "
                "3) Use single keywords instead of phrases"
            )

        # LLM DELEGATION: Synthesize results via local model when requested
        # Threshold: Only synthesize when there are enough results to make it worthwhile
        SYNTHESIS_THRESHOLD = 3  # Minimum discoveries to trigger synthesis
        if synthesize and len(discovery_list) >= SYNTHESIS_THRESHOLD:
            try:
                synthesis_result = await synthesize_results(
                    discoveries=discovery_list,
                    query=query_text,
                    max_discoveries=10,  # Cap at 10 for prompt size
                    max_tokens=400
                )
                if synthesis_result:
                    response_data["synthesis"] = synthesis_result
                    logger.debug(f"Knowledge synthesis generated for {len(discovery_list)} discoveries")
            except Exception as e:
                # Non-blocking: If synthesis fails, still return results
                logger.debug(f"Synthesis skipped: {e}")
                response_data["_synthesis_note"] = "Synthesis unavailable (local LLM not responding)"
        elif synthesize and len(discovery_list) < SYNTHESIS_THRESHOLD:
            response_data["_synthesis_note"] = f"Synthesis skipped: fewer than {SYNTHESIS_THRESHOLD} results"

        # Touch last_referenced on results that had details included (fire-and-forget)
        if include_details and results:
            import asyncio

            async def _touch_referenced(ids):
                try:
                    g = await get_knowledge_graph()
                    now_iso = _utc_now_iso()
                    for did in ids:
                        await g.update_discovery(did, {"last_referenced": now_iso})
                except Exception:
                    pass  # Best-effort, don't fail the search

            asyncio.create_task(_touch_referenced([d.id for d in results]))

        writer_sample = list({d.agent_id for d in results if d.agent_id})[:10]
        await _broadcast_knowledge_read(
            "search",
            _resolve_reader_agent_id(arguments),
            payload={
                "result_count": len(results),
                "query_present": bool(query_text),
                "query_term_count": query_term_count,
                "search_mode": locals().get("search_mode") or search_mode_param,
                "writer_agent_ids": writer_sample,
                "filter_agent_id": arguments.get("agent_id"),
            },
        )
        return success_response(response_data, arguments=arguments)

    except Exception as e:
        return [error_response(f"Failed to search knowledge: {str(e)}")]

@mcp_tool("get_knowledge_graph", timeout=15.0, rate_limit_exempt=True, register=False)
async def handle_get_knowledge_graph(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Get all knowledge for an agent - summaries only (use get_discovery_details for full content)"""
    # SECURITY FIX: Verify agent_id is registered (prevents phantom agent_ids)
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]
    
    try:
        graph = await get_knowledge_graph()
        
        limit = arguments.get("limit")
        t0 = time.perf_counter()
        discoveries = await graph.get_agent_discoveries(agent_id, limit=limit)
        record_ms("knowledge.get_agent_discoveries", (time.perf_counter() - t0) * 1000.0)
        
        # Return summaries only by default
        include_details = arguments.get("include_details", False)

        # UX FIX (Dec 2025): Display name FIRST for human readability
        agent_display = arguments.get("_agent_display") or _resolve_agent_display(agent_id)
        live_display_name = agent_display.get("display_name", agent_id)
        discovery_list = []
        for d in discoveries:
            full_dict = d.to_dict(include_details=include_details)
            # Bug A fix 2026-04-25: prefer write-time label over live resolve.
            # The same UUID may have been written under multiple display_names
            # across resumed sessions; live resolve would erase that history.
            prov = d.provenance if isinstance(d.provenance, dict) else None
            row_display_name = (prov or {}).get("writer_label_at_write") or live_display_name
            # Build dict with display_name first for prominence
            d_dict = {
                "by": row_display_name,  # WHO - first for attribution
                "summary": d.summary,  # WHAT - second for context
                "id": full_dict.get("id"),
                "type": full_dict.get("type"),
                "status": full_dict.get("status"),
                "tags": full_dict.get("tags", []),
                "created_at": full_dict.get("created_at"),
            }
            session_at_write = (prov or {}).get("writer_session_id_at_write")
            if session_at_write:
                d_dict["session_id_at_write"] = session_at_write
            if include_details and full_dict.get("details"):
                d_dict["details"] = full_dict.get("details")
            d_dict["_agent_id"] = d.agent_id
            discovery_list.append(d_dict)

        response_data = {
            "agent": agent_display,
            "discoveries": discovery_list,
            "count": len(discoveries)
        }

        # Visibility hints (v2.5.0+)
        if not include_details and len(discoveries) > 0:
            response_data["_tip"] = "Add include_details=true to expand all results inline"
        if limit and len(discoveries) == limit:
            response_data["_more_available"] = f"Results limited to {limit}. Use limit=N to get more."

        await _broadcast_knowledge_read(
            "get",
            _resolve_reader_agent_id(arguments),
            payload={
                "target_agent_id": agent_id,
                "result_count": len(discoveries),
                "include_details": bool(include_details),
            },
        )
        return success_response(response_data, arguments=arguments)

    except Exception as e:
        return [error_response(f"Failed to retrieve knowledge: {str(e)}")]

@mcp_tool("list_knowledge_graph", timeout=10.0, rate_limit_exempt=True, register=False)
async def handle_list_knowledge_graph(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """List knowledge graph statistics — raw status aggregate.

    Use ``epoch_scope`` ("current"|"all") and ``including_cold`` (bool) to
    align this view with knowledge action=stats (which uses lifecycle
    buckets). #165 — same-name fields used to report different totals
    silently.
    """
    try:
        graph = await get_knowledge_graph()
        epoch_scope = (arguments.get("epoch_scope") or "current").lower()
        if epoch_scope not in {"current", "all"}:
            return [error_response(
                f"Invalid epoch_scope {epoch_scope!r}; expected 'current' or 'all'"
            )]
        including_cold = bool(arguments.get("including_cold", False))

        t0 = time.perf_counter()
        try:
            stats = await graph.get_stats(
                epoch_scope=epoch_scope, including_cold=including_cold,
            )
        except TypeError:
            # Older backend not yet updated to the new signature — best-effort
            # call without scope params, then annotate the response.
            stats = await graph.get_stats()
            stats.setdefault("scope", {
                "kind": "raw_status_aggregate",
                "epoch_scope": "unknown",
                "including_cold": "unknown",
                "note": "backend predates #165 scope-flag plumbing",
            })
        record_ms("knowledge.get_stats", (time.perf_counter() - t0) * 1000.0)

        scope_summary = (
            f"epoch_scope={stats.get('scope', {}).get('epoch_scope', '?')}, "
            f"including_cold={stats.get('scope', {}).get('including_cold', '?')}"
        )
        await _broadcast_knowledge_read(
            "list",
            _resolve_reader_agent_id(arguments),
            payload={
                "epoch_scope": stats.get("scope", {}).get("epoch_scope") if isinstance(stats, dict) else None,
                "including_cold": including_cold,
            },
        )
        return success_response({
            "stats": stats,
            "message": (
                f"Knowledge graph contains {stats['total_discoveries']} "
                f"discoveries from {stats['total_agents']} agents "
                f"({scope_summary}). For lifecycle-bucketed counts see "
                f"knowledge action=stats."
            ),
        }, arguments=arguments)

    except Exception as e:
        return [error_response(f"Failed to list knowledge: {str(e)}")]

@mcp_tool("update_discovery_status_graph", timeout=10.0, register=False)
async def handle_update_discovery_status_graph(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Update discovery fields - status, details, and selected metadata.
    
    SECURITY: Requires authentication for high-severity discoveries.
    Low/medium severity discoveries can be updated by any registered agent (collaborative).
    """
    # SECURITY FIX: Require registered agent_id (prevents phantom agent_ids)
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]
    
    discovery_id, error = require_argument(arguments, "discovery_id",
                                         "discovery_id is required")
    if error:
        return [error]
    
    # Validate discovery_id format
    
    status = arguments.get("status")
    raw_details = arguments.get("details")
    if raw_details is None:
        raw_details = arguments.get("content")
    resolution_notes = arguments.get("resolution_notes")
    resolution_note_text = None
    if resolution_notes is not None:
        resolution_note_text = str(resolution_notes).strip() or None
    summary = arguments.get("summary")
    severity = arguments.get("severity")
    discovery_type = arguments.get("discovery_type")
    tags = arguments.get("tags")

    if not any(value is not None for value in (status, raw_details, resolution_note_text, summary, severity, discovery_type, tags)):
        return [error_response(
            "At least one updatable field is required. Provide status, details/content, resolution_notes, summary, severity, discovery_type, or tags."
        )]
    
    try:
        graph = await get_knowledge_graph()
        
        # Get discovery to check severity and ownership
        discovery = await graph.get_discovery(discovery_id)
        if not discovery:
            return [await _discovery_not_found(discovery_id, graph)]
        
        # SECURITY: Require session ownership for high-severity discoveries (UUID-based auth, Dec 2025)
        if discovery.severity in ["high", "critical"]:
            from ..utils import verify_agent_ownership
            if not verify_agent_ownership(agent_id, arguments):
                return [error_response(
                    "Authentication required for updating high-severity discoveries.",
                    error_code="AUTH_REQUIRED",
                    error_category="auth_error",
                    recovery={
                        "action": "Ensure your session is bound to this agent",
                        "related_tools": ["identity"],
                        "workflow": "Identity auto-binds on first tool call. Use identity() to check binding."
                    }
                )]
            
            # Ownership check: non-owners may only close high-severity discoveries,
            # and may not edit content/metadata while doing so.
            allowed_non_owner_statuses = {"resolved", "closed", "wont_fix"}
            requested_non_status_edits = [
                field_name
                for field_name, field_value in {
                    "details/content": raw_details,
                    "summary": summary,
                    "severity": severity,
                    "discovery_type": discovery_type,
                    "tags": tags,
                }.items()
                if field_value is not None
            ]
            if resolution_note_text is not None and status not in allowed_non_owner_statuses:
                requested_non_status_edits.append("resolution_notes")
            if discovery.agent_id != agent_id and requested_non_status_edits:
                allowed_list = sorted(allowed_non_owner_statuses)
                return [error_response(
                    f"Permission denied: Non-owners cannot edit {', '.join(requested_non_status_edits)} on high-severity discovery '{discovery_id}'. "
                    f"Allowed cross-agent status values: {allowed_list}.",
                    recovery={
                        "action": f"Retry with status only. Allowed values: {allowed_list}",
                        "related_tools": ["get_discovery_details", "search_knowledge_graph"],
                    }
                )]
            if discovery.agent_id != agent_id and status not in allowed_non_owner_statuses:
                allowed_list = sorted(allowed_non_owner_statuses)
                return [error_response(
                    f"Permission denied: Cannot set status '{status}' on high-severity discovery '{discovery_id}'. "
                    f"Allowed cross-agent status values: {allowed_list}.",
                    recovery={
                        "action": f"Use status in {allowed_list} to close another agent's discovery",
                        "related_tools": ["get_discovery_details", "search_knowledge_graph"],
                    }
                )]
        
        updates = {"updated_at": _utc_now_iso()}

        if status is not None:
            VALID_STATUSES = {"open", "resolved", "archived", "disputed", "closed", "wont_fix", "superseded"}
            status = str(status).lower()
            if status not in VALID_STATUSES:
                return [error_response(f"Invalid status '{status}'. Valid: {sorted(VALID_STATUSES)}")]
            updates["status"] = status
            if status == "resolved":
                updates["resolved_at"] = _utc_now_iso()

        if summary is not None:
            updates["summary"] = str(summary)

        if raw_details is not None:
            updates["details"] = str(raw_details)

        if resolution_note_text is not None:
            base_details = (
                str(raw_details)
                if raw_details is not None
                else (discovery.details or "")
            ).rstrip()
            note_block = f"Resolution notes ({_utc_now_iso()}):\n{resolution_note_text}"
            updates["details"] = (
                f"{base_details}\n\n{note_block}" if base_details else note_block
            )

        if severity is not None:
            severity = str(severity).lower()
            if severity not in VALID_SEVERITIES:
                return [_invalid_enum_response("severity", severity, VALID_SEVERITIES)]
            updates["severity"] = severity

        if discovery_type is not None:
            discovery_type = _normalize_discovery_type(discovery_type)
            if discovery_type not in VALID_DISCOVERY_TYPES:
                return [_invalid_enum_response("discovery_type", discovery_type, VALID_DISCOVERY_TYPES)]
            updates["type"] = discovery_type

        if tags is not None:
            updates["tags"] = tags
        
        success = await graph.update_discovery(discovery_id, updates)
        
        if not success:
            return [error_response(f"Discovery '{discovery_id}' not found")]
        
        discovery = await graph.get_discovery(discovery_id)
        
        message = f"Discovery '{discovery_id}' updated"
        if status is not None:
            message = f"Discovery '{discovery_id}' status updated to '{status}'"

        return success_response({
            "message": message,
            "discovery": discovery.to_dict(include_details=False) if discovery else None
        }, arguments=arguments)
        
    except Exception as e:
        return [error_response(f"Failed to update discovery: {str(e)}")]

@mcp_tool("get_discovery_details", timeout=10.0, rate_limit_exempt=True, register=False)
async def handle_get_discovery_details(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Get full details for a specific discovery with optional pagination and response chain.

    Parameters:
    - discovery_id: ID of the discovery to retrieve (required)
    - offset: Character offset for details pagination (default: 0)
    - length: Max characters to return for details (default: 2000)
    - include_response_chain: Include the chain of responses (Q→A→followup) (default: false)
    - max_chain_depth: Max depth for response chain traversal (default: 10)

    Migration Note (Dec 2025): This tool now includes response chain functionality
    previously available via get_response_chain_graph (deprecated).
    """
    discovery_id, error = require_argument(arguments, "discovery_id",
                                         "discovery_id is required")
    if error:
        return [error]

    # Validate discovery_id format

    try:
        graph = await get_knowledge_graph()

        discovery = await graph.get_discovery(discovery_id)
        if not discovery:
            return [await _discovery_not_found(discovery_id, graph)]

        # UX FIX: Pagination support for long details
        offset = arguments.get("offset", 0)
        length = arguments.get("length", 2000)

        details = discovery.details or ""
        total_length = len(details)

        # Apply pagination if details exceed length or offset > 0
        if offset > 0 or total_length > length:
            details_slice = details[offset:offset + length]
            has_more = (offset + length) < total_length

            response = {
                "discovery": discovery.to_dict(include_details=False),
                "details": details_slice,
                "pagination": {
                    "offset": offset,
                    "length": len(details_slice),
                    "total_length": total_length,
                    "has_more": has_more,
                    "next_offset": offset + length if has_more else None
                },
                "message": f"Details for discovery '{discovery_id}' (showing {offset}-{offset + len(details_slice)} of {total_length} chars)"
            }
        else:
            # Full content fits - no pagination needed
            response = {
                "discovery": discovery.to_dict(include_details=True),
                "message": f"Full details for discovery '{discovery_id}'"
            }

        # Response chain traversal (Dec 2025 - restores get_response_chain_graph functionality)
        include_chain = arguments.get("include_response_chain", False)
        if include_chain:
            max_depth = arguments.get("max_chain_depth", 10)

            # Check if backend supports response chain traversal
            if hasattr(graph, 'get_response_chain'):
                try:
                    chain = await graph.get_response_chain(discovery_id, max_depth=max_depth)
                    response["response_chain"] = {
                        "count": len(chain),
                        "max_depth": max_depth,
                        "discoveries": [d.to_dict(include_details=False) for d in chain]
                    }
                    response["message"] += f" (includes {len(chain)} discoveries in response chain)"
                except Exception as chain_err:
                    # Non-fatal: include error but don't fail the request
                    response["response_chain"] = {
                        "error": f"Chain traversal failed: {str(chain_err)}",
                        "note": "Discovery details still returned successfully"
                    }
            else:
                # Backend doesn't support chain traversal
                response["response_chain"] = {
                    "error": "Response chain traversal not supported by current backend",
                    "note": "Use AGE backend (UNITARES_KNOWLEDGE_BACKEND=age) for full graph features"
                }

        # Touch last_referenced (fire-and-forget keep-alive signal)
        import asyncio

        async def _touch(did):
            try:
                await graph.update_discovery(did, {"last_referenced": _utc_now_iso()})
            except Exception:
                pass

        asyncio.create_task(_touch(discovery_id))

        await _broadcast_knowledge_read(
            "details",
            _resolve_reader_agent_id(arguments),
            payload={
                "discovery_id": discovery_id,
                "writer_agent_id": getattr(discovery, "agent_id", None),
                "include_response_chain": bool(arguments.get("include_response_chain", False)),
            },
        )
        return success_response(response, arguments=arguments)

    except Exception as e:
        return [error_response(f"Failed to get discovery details: {str(e)}")]

async def _handle_store_knowledge_graph_batch(arguments: Dict[str, Any], agent_id: str) -> Sequence[TextContent]:
    """Internal batch handler - called by store_knowledge_graph when discoveries array is provided"""
    discoveries = arguments.get("discoveries")
    
    if not isinstance(discoveries, list):
        return [error_response("discoveries must be a list of discovery objects")]
    
    if len(discoveries) == 0:
        return [error_response("discoveries list cannot be empty")]
    
    if len(discoveries) > 10:
        return [error_response("Maximum 10 discoveries per batch (to prevent context overflow)")]
    
    # agent_id already validated by caller
    
    try:
        graph = await get_knowledge_graph()
        
        # SECURITY: Rate limiting is handled by the knowledge graph backend per-discovery
        # Backend handles rate limiting internally (O(1) per store)
        # No need for inefficient O(n) query here - let graph handle it per-discovery
        
        # Process each discovery with graceful error handling
        stored = []
        errors = []
        
        for idx, disc_data in enumerate(discoveries):
            try:
                # Validate required fields
                if not isinstance(disc_data, dict):
                    errors.append(f"Discovery {idx}: must be a dict")
                    continue
                
                discovery_type = _normalize_discovery_type(disc_data.get("discovery_type"))
                if not discovery_type:
                    errors.append(f"Discovery {idx}: discovery_type is required")
                    continue
                
                if discovery_type not in VALID_DISCOVERY_TYPES:
                    errors.append(f"Discovery {idx}: invalid discovery_type '{discovery_type}'")
                    continue
                
                summary = disc_data.get("summary", "")
                if not summary:
                    errors.append(f"Discovery {idx}: summary is required")
                    continue
                
                # Truncate fields (limits imported at module top).
                truncated_fields = []
                if len(summary) > MAX_SUMMARY_LEN:
                    truncated_fields.append(f"summary ({len(summary)} → {MAX_SUMMARY_LEN})")
                    # Try to cut at sentence boundary, else word boundary
                    truncated = summary[:MAX_SUMMARY_LEN]
                    for end_char in ['. ', '! ', '? ']:
                        last_end = truncated.rfind(end_char, MAX_SUMMARY_LEN - 100)
                        if last_end > 0:
                            truncated = truncated[:last_end + 1]
                            break
                    else:
                        last_space = truncated.rfind(' ')
                        if last_space > MAX_SUMMARY_LEN - 50:
                            truncated = truncated[:last_space]
                    summary = truncated.rstrip() + "..."

                # Accept both 'details' and 'content' as parameter names
                details = disc_data.get("details") or disc_data.get("content") or ""
                if len(details) > MAX_DETAILS_LEN:
                    truncated_fields.append(f"details ({len(details)} → {MAX_DETAILS_LEN})")
                    details = details[:MAX_DETAILS_LEN] + "... [truncated]"
                
                # Create discovery node
                discovery_id = _utc_now_iso()
                
                # Parse response_to if provided
                response_to = None
                if "response_to" in disc_data and disc_data["response_to"]:
                    resp_data = disc_data["response_to"]
                    if isinstance(resp_data, dict) and "discovery_id" in resp_data and "response_type" in resp_data:
                        parent_id = str(resp_data["discovery_id"]).strip()
                        if not parent_id:
                            errors.append(f"Discovery {idx}: Invalid response_to.discovery_id (empty)")
                            continue

                        response_type = resp_data["response_type"]
                        VALID_RESPONSE_TYPES = {"extend", "question", "disagree", "support", "answer", "follow_up", "correction", "elaboration", "supersedes"}
                        if response_type in VALID_RESPONSE_TYPES:
                            response_to = ResponseTo(
                                discovery_id=parent_id,
                                response_type=response_type
                            )
                
                # Validate severity
                severity = disc_data.get("severity")
                if severity is not None:
                    if severity not in VALID_SEVERITIES:
                        severity = "medium"  # Use default if invalid
                
                # Parse confidence if provided
                batch_confidence = None
                if disc_data.get("confidence") is not None:
                    try:
                        batch_confidence = float(disc_data["confidence"])
                        batch_confidence = max(0.0, min(1.0, batch_confidence))
                    except (ValueError, TypeError):
                        pass

                from src.knowledge_graph import tag_provenance_source as _tag_src
                discovery = DiscoveryNode(
                    id=discovery_id,
                    agent_id=agent_id,
                    type=discovery_type,
                    summary=summary,
                    details=details,
                    tags=disc_data.get("tags", []),
                    severity=severity,
                    response_to=response_to,
                    references_files=disc_data.get("related_files", []),
                    confidence=batch_confidence,
                    provenance=_tag_src(disc_data.get("provenance"), "explicit_store"),
                )

                # CONFIDENCE CROSS-CHECK: Clamp to agent coherence + 0.3
                await _clamp_confidence_to_coherence(discovery, agent_id)

                # Auto-link similar discoveries
                if disc_data.get("auto_link_related", True):
                    similar = await graph.find_similar(discovery, limit=3)
                    discovery.related_to = [s.id for s in similar]
                
                # SECURITY: Require session ownership for high-severity discoveries (UUID-based auth, Dec 2025)
                if discovery.severity in ["high", "critical"]:
                    from ..utils import verify_agent_ownership
                    if not verify_agent_ownership(agent_id, arguments):
                        errors.append(f"Discovery {idx}: Authentication required for high-severity discoveries")
                        continue
                
                # Add to graph (rate limiting handled internally)
                await graph.add_discovery(discovery)
                stored_item = {
                    "discovery_id": discovery_id,
                    "summary": summary,
                    "type": discovery_type
                }
                if truncated_fields:
                    stored_item["_truncated"] = truncated_fields
                stored.append(stored_item)
                
            except ValueError as e:
                # Handle rate limiting and validation errors gracefully
                error_msg = str(e)
                if "rate limit" in error_msg.lower() or "Rate limit" in error_msg:
                    errors.append(f"Discovery {idx}: Rate limit exceeded - {error_msg}")
                else:
                    errors.append(f"Discovery {idx}: Validation error - {error_msg}")
            except Exception as e:
                errors.append(f"Discovery {idx}: {str(e)}")
        
        # Return results
        response = {
            "message": f"Stored {len(stored)}/{len(discoveries)} discovery/discoveries",
            "stored": stored,
            "total": len(discoveries),
            "success_count": len(stored),
            "error_count": len(errors)
        }

        if errors:
            response["errors"] = errors

        # Check if any items were truncated (v2.5.0+)
        truncated_count = sum(1 for s in stored if "_truncated" in s)
        if truncated_count > 0:
            response["_tip"] = f"{truncated_count} discovery(ies) had content truncated. Limits: summary=1000, details=5000 chars."

        return success_response(response, arguments=arguments)
        
    except Exception as e:
        return [error_response(f"Failed to store batch knowledge: {str(e)}")]

@mcp_tool("answer_question", timeout=15.0, register=False)
async def handle_answer_question(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Answer a question in the knowledge graph - closes the Q&A loop.

    Searches for matching questions and stores your answer linked to it.
    No need to know the question's discovery_id - just provide the question text and your answer.

    Parameters:
    - question: Text to match against existing questions (fuzzy search)
    - answer: Your answer to the question
    - tags: Optional tags for the answer
    """
    # SECURITY FIX: Verify agent_id is registered
    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]

    question_text, error = require_argument(arguments, "question",
                                           "question is required - what question are you answering?")
    if error:
        return [error]

    answer_text, error = require_argument(arguments, "answer",
                                         "answer is required - your response to the question")
    if error:
        return [error]

    try:
        graph = await get_knowledge_graph()

        # Search for matching questions
        candidates = await graph.query(type="question", limit=20)

        # Find best match using substring matching
        q_lower = question_text.lower()
        matched_question = None
        best_score = 0

        for d in candidates:
            summary_lower = (d.summary or "").lower()
            # Simple scoring: longer common substring = better match
            if q_lower in summary_lower or summary_lower in q_lower:
                score = len(set(q_lower.split()) & set(summary_lower.split()))
                if score > best_score:
                    best_score = score
                    matched_question = d

        if not matched_question:
            # No matching question found - list available questions
            recent_questions = await graph.query(type="question", limit=5)
            question_summaries = [
                {"id": q.id, "summary": q.summary[:100] + "..." if len(q.summary) > 100 else q.summary}
                for q in recent_questions
            ]
            return [error_response(
                f"No matching question found for: '{question_text[:50]}...'",
                details={"recent_questions": question_summaries},
                recovery={
                    "action": "Try a different search term or use store_knowledge_graph with response_to",
                    "related_tools": [KNOWLEDGE_SEARCH_TOOL],
                    "workflow": KNOWLEDGE_OPEN_QUESTIONS_WORKFLOW,
                }
            )]

        # Truncate answer if too long
        MAX_ANSWER_LEN = 2000
        if len(answer_text) > MAX_ANSWER_LEN:
            answer_text = answer_text[:MAX_ANSWER_LEN] + "... [truncated]"

        # Create answer linked to the question
        from src.knowledge_graph import tag_provenance_source as _tag_src
        answer = DiscoveryNode(
            id=_utc_now_iso(),
            agent_id=agent_id,
            type="answer",
            summary=f"Answer: {answer_text[:200]}..." if len(answer_text) > 200 else f"Answer: {answer_text}",
            details=answer_text,
            tags=normalize_tags(arguments.get("tags", [])),
            severity="low",
            status="open",
            response_to=ResponseTo(
                discovery_id=matched_question.id,
                response_type="answers"
            ),
            provenance=_tag_src(None, "explicit_answer"),
        )

        # Link answer to question
        answer.related_to = [matched_question.id]

        await graph.add_discovery(answer)

        # Optionally mark question as resolved
        if arguments.get("resolve_question", False):
            await graph.update_discovery(matched_question.id, {
                "status": "resolved",
                "resolved_at": _utc_now_iso()
            })

        return success_response({
            "message": "Answer stored and linked to question",
            "answer_id": answer.id,
            "question": {
                "id": matched_question.id,
                "summary": matched_question.summary,
                "status": "resolved" if arguments.get("resolve_question") else matched_question.status
            },
            "answer": answer.to_dict(include_details=False)
        }, arguments=arguments)

    except Exception as e:
        return [error_response(f"Failed to answer question: {str(e)}")]

@mcp_tool("leave_note", timeout=10.0, deprecated=True, superseded_by="knowledge")
async def handle_leave_note(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """[DEPRECATED — use knowledge(action='note') instead] Leave a quick note in the knowledge graph.

    Just agent_id + summary + optional tags. Auto-sets type='note', severity='low'.
    Functionally identical to ``knowledge(action='note', summary=...)``; deprecation
    is per dogfood-UX issue #429 (tool aliasing — pick one). Calls still work.
    """
    # Apply parameter aliases (e.g., "text" → "summary", "note" → "summary")
    arguments = apply_param_aliases("leave_note", arguments)
    
    # Set tool name in context for better error messages
    arguments["_tool_name"] = "leave_note"

    # Notes are always low-severity, so allow a stable anonymous writer when
    # no explicit or bound identity exists.
    agent_id, error, is_anonymous_writer = _resolve_low_friction_writer(arguments)
    if error:
        return [error]

    # CIRCUIT BREAKER: Paused agents cannot leave notes
    from ..utils import check_agent_can_operate
    blocked = check_agent_can_operate(agent_id)
    if blocked:
        return [blocked]

    text, error = require_argument(arguments, "summary",
                                  "Note content required. Use 'summary', 'note', 'text', or 'content' parameter.")
    if error:
        return [error]
    
    try:
        graph = await get_knowledge_graph()
        
        # Notes use the same limits as store_knowledge_graph (imported at top).
        MAX_NOTE_TOTAL = MAX_SUMMARY_LEN + MAX_DETAILS_LEN
        if len(text) > MAX_NOTE_TOTAL:
            text = text[:MAX_NOTE_TOTAL] + "... [truncated]"
        
        # Parse response_to if provided (for threading)
        response_to = None
        if "response_to" in arguments and arguments["response_to"]:
            resp_data = arguments["response_to"]
            if isinstance(resp_data, dict) and "discovery_id" in resp_data and "response_type" in resp_data:
                # Validate discovery_id format
                parent_id = str(resp_data["discovery_id"]).strip()
                if not parent_id:
                    return error_response("Invalid response_to.discovery_id (empty)")

                # Validate response_type enum
                response_type = resp_data["response_type"]
                VALID_RESPONSE_TYPES = {"extend", "question", "disagree", "support", "answer", "follow_up", "correction", "elaboration", "supersedes"}
                if response_type not in VALID_RESPONSE_TYPES:
                    return error_response(f"Invalid response_type '{response_type}'. Valid: {sorted(VALID_RESPONSE_TYPES)}")

                response_to = ResponseTo(
                    discovery_id=parent_id,
                    response_type=response_type
                )

        # Tags pass through verbatim. Callers opt in to the ephemeral lifecycle
        # by tagging scratch/temp/ephemeral themselves; the handler does NOT
        # inject ephemeral on their behalf. The prior auto-inject silently
        # scheduled every non-permanent note for 7-day auto-archive, which
        # swept real design-gap notes that agents had no idea were on a timer.
        tags = normalize_tags(arguments.get("tags", []))

        # Split long notes into summary + details
        if len(text) <= MAX_SUMMARY_LEN:
            note_summary = text
            note_details = ""
        else:
            # Try to split at a sentence boundary within summary limit
            truncated = text[:MAX_SUMMARY_LEN]
            split_pos = MAX_SUMMARY_LEN
            for end_char in ['. ', '! ', '? ', '\n']:
                last_end = truncated.rfind(end_char, MAX_SUMMARY_LEN - 200)
                if last_end > 0:
                    split_pos = last_end + len(end_char)
                    break
            else:
                last_space = truncated.rfind(' ')
                if last_space > MAX_SUMMARY_LEN - 100:
                    split_pos = last_space
            note_summary = text[:split_pos].rstrip()
            note_details = text[split_pos:].strip()

        # Tag-based severity inference: auto-bump infrastructure bugs to medium
        note_severity = "low"
        tag_set = set(tags)
        INFRA_TAGS = {"infrastructure", "search", "embedding", "silent-failure", "degraded", "database", "service"}
        if "bug" in tag_set and (tag_set & INFRA_TAGS):
            note_severity = "medium"

        # Create note with minimal ceremony
        from src.knowledge_graph import tag_provenance_source as _tag_src
        provenance = _tag_src(None, "explicit_leave_note")
        try:
            from src.provenance_context import (
                attach_s22_context,
                build_s22_write_context,
                classify_fork_for_s22_context,
            )

            meta = mcp_server.agent_metadata.get(agent_id)
            episode_fork_kind, identity_lineage_fork = classify_fork_for_s22_context(
                meta, agent_id
            )
            s22_context = build_s22_write_context(
                arguments,
                meta=meta,
                context_source="knowledge.note",
                default_governance_mode="explicit",
                episode_fork_kind=episode_fork_kind,
                identity_lineage_fork=identity_lineage_fork,
            )
            provenance = attach_s22_context(provenance, s22_context)
        except Exception as exc:
            logger.debug("Could not capture note S22 provenance: %s", exc)

        note = DiscoveryNode(
            id=_utc_now_iso(),
            agent_id=agent_id,
            type="note",
            summary=note_summary,
            details=note_details,
            tags=tags,
            severity=note_severity,
            status="open",
            response_to=response_to,
            provenance=provenance,
        )
        
        # Auto-link if tags provided (fast with indexes)
        if note.tags:
            similar = await graph.find_similar(note, limit=3)
            note.related_to = [s.id for s in similar]
        
        await graph.add_discovery(note)
        await _broadcast_knowledge_write(note, agent_id)

        # v2.5.3: Include agent display info
        agent_display = arguments.get("_agent_display") or _resolve_agent_display(agent_id)

        # UX FIX (Feb 2026): Clarify visibility - notes are shared and discoverable
        # KG loop closure: remind agents to resolve when addressed
        response = {
            "message": f"Note saved",
            "note_id": note.id,
            "agent": agent_display,
            "note": note.to_dict(include_details=False),
            # Clarify visibility for agent understanding
            "visibility": "shared",
            "discoverable": True,
            "_visibility_note": "Notes are shared and searchable by other agents. Use response_to to reply to discoveries.",
            "_resolve_when_done": f"When this is addressed, close the loop: knowledge(action='update', discovery_id='{note.id}', status='resolved')",
        }

        if is_anonymous_writer:
            response["agent_mode"] = "anonymous"
            response["_identity_hint"] = (
                "Stored under a lightweight anonymous writer ID. "
                "Bind an identity first if you want authorship continuity."
            )

        return success_response(response, arguments=arguments)

    except Exception as e:
        return [error_response(f"Failed to leave note: {str(e)}")]

@mcp_tool("cleanup_knowledge_graph", timeout=60.0, register=False)
async def handle_cleanup_knowledge_graph(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Run knowledge graph lifecycle cleanup.

    Manages discovery lifecycle based on type-based policies:
    - Permanent: architecture_decision, learning, pattern (never auto-archive)
    - Standard: resolved items archived after 30 days
    - Ephemeral: tagged with ephemeral/temp/scratch, archived after 7 days

    Args:
        dry_run: If true, preview changes without applying them (default: true)

    Returns lifecycle cleanup summary with counts of archived/moved discoveries.

    Philosophy: Never delete. Archive forever.
    """
    dry_run = arguments.get("dry_run", True)
    if isinstance(dry_run, str):
        dry_run = dry_run.lower() in ("true", "1", "yes")
    elif dry_run is None:
        dry_run = True

    try:
        from src.knowledge_graph_lifecycle import run_kg_lifecycle_cleanup
        result = await run_kg_lifecycle_cleanup(dry_run=dry_run)

        return success_response({
            "message": f"{'[DRY RUN] ' if dry_run else ''}Lifecycle cleanup complete",
            "cleanup_result": result,
        }, arguments=arguments)

    except Exception as e:
        return [error_response(f"Failed to run lifecycle cleanup: {str(e)}")]

@mcp_tool("synthesize_knowledge_graph", timeout=120.0, register=False)
async def handle_synthesize_knowledge_graph(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Compound discrete discoveries into rolled-up topic summaries (Issue #1).

    Closes the loop the knowledge-graph skill admits is open ("does not close
    loops automatically"): a periodic/on-demand pass that maintains a
    cross-referenced, compounded narrative per topic *before* query time, the
    way GraphRAG maintains hierarchical community summaries.

    Deliberately NOT a per-write hook — running an LLM pass on every store/note
    across a multi-agent fleet is the auto-checkin anti-pattern. This runs like
    lint/cleanup: on demand, or wired to a periodic trigger. Rollups are stored
    as ordinary discovery rows (type='topic_rollup', deterministic id
    'rollup::<topic>'), so they upsert in place and need no schema change.

    Args:
        topic:       Synthesize just this one tag. Omit to sweep the densest topics.
        limit:       Max topics processed this run (default 20). Bounds cost.
        min_members: Minimum discoveries a topic needs to be rolled up (default 3).
        use_llm:     Use the local LLM for the narrative (default true). When the
                     LLM is unreachable, falls back to a deterministic rollup.
        dry_run:     Preview the rollups without persisting them.

    Returns a per-topic report (member counts, cross-references, summary source).
    """
    from .synthesis import synthesize_topics, MIN_TOPIC_MEMBERS, DEFAULT_TOPIC_LIMIT

    topic = arguments.get("topic")

    dry_run = arguments.get("dry_run", False)
    if isinstance(dry_run, str):
        dry_run = dry_run.lower() in ("true", "1", "yes")
    elif dry_run is None:
        dry_run = False

    use_llm = arguments.get("use_llm", True)
    if isinstance(use_llm, str):
        use_llm = use_llm.lower() in ("true", "1", "yes")
    elif use_llm is None:
        use_llm = True

    def _as_int(value, default):
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return default

    limit = _as_int(arguments.get("limit"), DEFAULT_TOPIC_LIMIT)
    min_members = _as_int(arguments.get("min_members"), MIN_TOPIC_MEMBERS)

    try:
        graph = await get_knowledge_graph()
        result = await synthesize_topics(
            graph,
            topic=topic,
            limit=limit,
            min_members=min_members,
            use_llm=use_llm,
            dry_run=dry_run,
        )
        prefix = "[DRY RUN] " if dry_run else ""
        scope = f"topic '{topic}'" if topic else f"top {limit} topics"
        return success_response({
            "message": (
                f"{prefix}Synthesis complete over {scope}: "
                f"{result['rollups_written']} rollup(s) written"
            ),
            **result,
        }, arguments=arguments)
    except Exception as e:
        return [error_response(f"Failed to synthesize knowledge graph: {str(e)}")]

@mcp_tool("get_lifecycle_stats", timeout=30.0, rate_limit_exempt=True, register=False)
async def handle_get_lifecycle_stats(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Get knowledge graph lifecycle statistics.

    Shows discovery counts by status and lifecycle policy, plus candidates
    ready for archival or cold storage.

    Useful for understanding knowledge graph health and what cleanup would do.
    """
    try:
        from src.knowledge_graph_lifecycle import get_kg_lifecycle_stats
        stats = await get_kg_lifecycle_stats()
        try:
            graph = await get_knowledge_graph()
            get_stats = getattr(graph, "get_stats", None)
            if callable(get_stats):
                try:
                    raw_stats = await get_stats(epoch_scope="current", including_cold=True)
                except TypeError:
                    raw_stats = await get_stats()
                if isinstance(raw_stats, dict):
                    stats["raw_current_counts"] = {
                        "total_discoveries": raw_stats.get("total_discoveries"),
                        "by_status": raw_stats.get("by_status", {}),
                        "scope": raw_stats.get("scope", {}),
                    }
                    lifecycle_total = stats.get("total_discoveries")
                    raw_total = raw_stats.get("total_discoveries")
                    if (
                        isinstance(lifecycle_total, int)
                        and isinstance(raw_total, int)
                        and lifecycle_total != raw_total
                    ):
                        stats["count_scope_warning"] = (
                            "Lifecycle bucket totals differ from raw current counts. "
                            "Use raw_current_counts.by_status to confirm immediate "
                            "status updates; lifecycle buckets may span backend or "
                            "historical query scope."
                        )
        except Exception as exc:
            stats["raw_current_counts_error"] = str(exc)

        return success_response({
            "message": "Lifecycle statistics",
            "stats": stats,
        }, arguments=arguments)

    except Exception as e:
        return [error_response(f"Failed to get lifecycle stats: {str(e)}")]

@mcp_tool("supersede_discovery", timeout=15.0, register=False)
async def handle_supersede_discovery(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Mark a discovery as superseding another.

    Creates a SUPERSEDES edge in the knowledge graph. Superseded entries
    receive a ranking penalty in search results.

    Args:
        discovery_id: The newer discovery (the one that replaces)
        supersedes_id: The older discovery being replaced

    Returns success/failure status.
    """
    new_id = arguments.get("discovery_id")
    old_id = arguments.get("supersedes_id")

    if not new_id or not old_id:
        return [error_response("Both discovery_id and supersedes_id are required")]

    try:
        graph = await get_knowledge_graph()
        if not hasattr(graph, "supersede_discovery"):
            return [error_response("SUPERSEDES edges require AGE graph backend")]

        result = await graph.supersede_discovery(new_id=new_id, old_id=old_id)
        if result.get("success"):
            return success_response(result, arguments=arguments)
        else:
            return [error_response(result.get("error", "Failed to create SUPERSEDES edge"))]
    except Exception as e:
        return [error_response(f"Failed to supersede discovery: {str(e)}")]


@mcp_tool("audit_knowledge_graph", timeout=60.0, register=False)
async def handle_audit_knowledge_graph(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Audit knowledge graph for staleness and health.

    Read-only analysis that scores open KG entries by age, activity,
    and type, grouping them into health buckets. Does NOT modify anything.

    Args:
        scope: "open" (default), "all", "by_agent"
        top_n: Number of stale entries to return (default: 10)
        use_model: If true, use call_model to assess relevance (default: false)

    Returns audit report with bucket counts and top stale entries.
    """
    scope = arguments.get("scope", "open")
    top_n = int(arguments.get("top_n", 10))
    use_model = arguments.get("use_model", False)
    if isinstance(use_model, str):
        use_model = use_model.lower() in ("true", "1", "yes")

    try:
        from src.knowledge_graph_lifecycle import run_kg_audit
        result = await run_kg_audit(
            scope=scope,
            top_n=top_n,
            use_model=use_model,
            agent_id=arguments.get("agent_id"),
        )
        return success_response({
            "message": f"KG audit complete ({scope} scope, {result['total_audited']} entries)",
            "audit": result,
        }, arguments=arguments)
    except Exception as e:
        return [error_response(f"Failed to run KG audit: {str(e)}")]


async def store_discovery_internal(
    agent_id: str,
    summary: str,
    *,
    source: str,
    discovery_type: str = "note",
    details: str = "",
    tags: Optional[list] = None,
    severity: str = "low",
    extra_provenance: Optional[Dict[str, Any]] = None,
) -> None:
    """Internal helper for storing discoveries without MCP handler overhead.

    Used by lifecycle/self_recovery and dialectic to log reflections and
    resume events. Every implicit write declares its origin via the
    required ``source`` parameter — recorded in provenance.source so list
    and stats can split caller-intentional writes from automation traffic
    (#165 phantom-write surface).

    Raises on failure (callers should catch exceptions).
    """
    from src.knowledge_graph import tag_provenance_source

    graph = await get_knowledge_graph()
    discovery_id = _utc_now_iso()
    provenance = tag_provenance_source(extra_provenance, source)
    node = DiscoveryNode(
        id=discovery_id,
        agent_id=agent_id,
        type=discovery_type,
        summary=summary,
        details=details,
        tags=normalize_tags(tags or []),
        severity=severity,
        provenance=provenance,
    )
    await graph.add_discovery(node)
