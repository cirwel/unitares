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

from dataclasses import dataclass, field
from typing import Dict, Any, Sequence, Optional
from mcp.types import TextContent
from datetime import datetime, timezone, timedelta
import hashlib
import os
import secrets
import threading


def _utc_now_iso() -> str:
    """Return current UTC time as an offset-aware ISO 8601 string.

    Bug fix 2026-04-25: write-path timestamps were generated with
    naive ``datetime.now()`` (server local TZ), causing ``id`` to drift
    from ``created_at`` (UTC, via PG TIMESTAMPTZ) by the server's offset
    and breaking lex-sort across multi-tz fleets. All KG write-path
    timestamps must be UTC-aware.
    """
    return datetime.now(timezone.utc).isoformat()


_discovery_id_lock = threading.Lock()
_last_discovery_id_dt: Optional[datetime] = None


def _new_discovery_id() -> str:
    """Generate a strictly-monotonic UTC ISO timestamp for use as a discovery id.

    Discovery ids double as the primary key *and* as a lex-sortable creation
    timestamp (consumers parse them with ``datetime.fromisoformat`` and the
    not-found helper prefix-matches them). A bare ``datetime.now()`` collides
    when two writes land in the same microsecond — a batch loop or rapid
    successive single stores — and the storage layer's
    ``INSERT ... ON CONFLICT (id) DO UPDATE`` then *silently overwrites* the
    first write (data loss, no error returned). Bumping by 1µs on collision
    guarantees uniqueness within this process while preserving the
    ISO-timestamp contract and chronological ordering.

    Residual: two *separate* processes can still mint the same microsecond
    id; closing that requires moving the primary key off a bare timestamp,
    which is a contract change (docs + tests treat ids as parseable ISO
    timestamps) and is out of scope here.
    """
    global _last_discovery_id_dt
    with _discovery_id_lock:
        now = datetime.now(timezone.utc)
        if _last_discovery_id_dt is not None and now <= _last_discovery_id_dt:
            now = _last_discovery_id_dt + timedelta(microseconds=1)
        _last_discovery_id_dt = now
        return now.isoformat()
import time
from ..utils import success_response, error_response, require_argument, require_agent_id, require_registered_agent
from ..decorators import mcp_tool
from ..validators import apply_param_aliases
from src.knowledge_graph import (
    get_knowledge_graph, DiscoveryNode, ResponseTo, normalize_tags,
    VALID_RESPONSE_TYPES, VALID_DISCOVERY_STATUSES,
    VALID_SEVERITIES as _SHARED_VALID_SEVERITIES,
)
from src.mcp_handlers.knowledge.limits import MAX_SUMMARY_LEN, MAX_DETAILS_LEN
from config.governance_config import config
from src.logging_utils import get_logger
from src.perf_monitor import record_ms
from src.recall_telemetry import LOW_CONFIDENCE, ZERO_RESULT, record_recall_event
from ..support.llm_delegation import synthesize_results
from ..support.tool_hints import KNOWLEDGE_SEARCH_TOOL

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
# Single-sourced in src.knowledge_graph next to the status/response_type
# vocabularies; tests/test_knowledge_enum_sync.py pins all three against the
# SQL CHECK constraints.
VALID_SEVERITIES = _SHARED_VALID_SEVERITIES
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


# Tool-call serialization markers that must never appear in a stored discovery.
# Their presence means the harness folded a later argument (commonly `tags`)
# into a text field as raw markup while the structured arg arrived empty —
# storing it yields a corrupt, unsearchable row (tags=[] + literal markup in
# details) with no warning. Legit prose effectively never contains these, so a
# hard reject is low-false-positive. (KG 2026-06-13 footgun: "knowledge
# store/update silently accepts degenerate writes".)
_TOOLCALL_MARKUP_MARKERS = (
    "<parameter name=",
    "</parameter>",
    "<invoke name=",
    "</invoke>",
    "<function_calls>",
    "</function_calls>",
    "antml:parameter",
    "antml:invoke",
)


def _detect_toolcall_markup_leak(*texts: Any) -> "str | None":
    """Return the first tool-call markup marker found in any text arg, else None."""
    for text in texts:
        if not isinstance(text, str):
            continue
        for marker in _TOOLCALL_MARKUP_MARKERS:
            if marker in text:
                return marker
    return None


def _degenerate_write_response(leaked_marker: str, field: str):
    """Loud, actionable reject for a text field that absorbed tool-call markup."""
    return error_response(
        f"Refusing to store: `{field}` contains tool-call markup "
        f"({leaked_marker!r}). This usually means a later argument (commonly "
        f"`tags`) was folded into the text and the structured argument arrived "
        f"empty — storing it would persist a corrupt, unsearchable row. Re-send "
        f"with each argument as a separate parameter.",
        error_code="degenerate_write_rejected",
        error_category="validation_error",
        recovery={
            "action": "Resend the call with summary, content/details, and tags "
            "as distinct arguments; do not embed tags or markup inside content."
        },
    )


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


def _coerce_pagination_int(value: Any, *, default: int, minimum: int) -> int:
    if value is None:
        return default
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    if coerced < minimum:
        return default
    return coerced


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
    from ..context import get_context_agent_id, get_session_proof_origin
    explicit_reader = arguments.get("_agent_uuid") or arguments.get("agent_id")
    if explicit_reader:
        return explicit_reader
    if get_session_proof_origin() == "server_inferred":
        return None
    return get_context_agent_id()


async def _annotate_supersession(discovery_list: list, graph) -> None:
    """Flag superseded rows in a result list so agents don't cite stale notes.

    Agent-facing trust signal (2026-06-21). Default search excludes archived/cold
    but NOT superseded, so a superseded discovery surfaces in results (only
    down-ranked). This marks those rows `superseded: True` from the already-loaded
    `status` (free) and best-effort attaches the replacement id(s) from the AGE
    SUPERSEDES edge. Mutates `discovery_list` in place; fail-soft (a graph error
    still leaves the free status-based flag).
    """
    superseded_ids = [
        d["id"] for d in discovery_list
        if d.get("status") == "superseded" and d.get("id")
    ]
    if not superseded_ids:
        return
    successors: Dict[str, list] = {}
    if hasattr(graph, "get_superseded_by"):
        try:
            successors = await graph.get_superseded_by(superseded_ids)
        except Exception as exc:  # noqa: BLE001 — advisory flag, never break search
            logger.debug(f"[KG_SEARCH] supersession lookup failed (non-fatal): {exc}")
    for d in discovery_list:
        if d.get("status") != "superseded":
            continue
        d["superseded"] = True
        newer = successors.get(d.get("id")) or []
        if newer:
            d["superseded_by"] = newer
        d["superseded_warning"] = (
            "Superseded — a newer discovery replaced this. "
            + (f"Current version: {newer[0]}. " if newer else "")
            + "Prefer the newer entry over this one."
        )


async def _record_supersession_edge(graph, *, new_id: str, old_id: str) -> Optional[str]:
    """Durably record that ``new_id`` supersedes ``old_id`` (AGE SUPERSEDES edge).

    The relational row has no ``superseded_by`` column, so both write paths used
    to pass ``superseded_by`` into ``update_discovery``, which silently dropped
    it — the 2026-06-21 finding: 18 ``status='superseded'`` rows but 0 SUPERSEDES
    edges, i.e. the successor link was lost everywhere. Creating the edge here is
    what lets a reader/dashboard resolve what replaced a note. Returns an error
    string on failure (the caller surfaces it), or ``None`` on success.
    """
    if not new_id or not old_id or new_id == old_id:
        return None
    if not hasattr(graph, "supersede_discovery"):
        return "supersession link not recorded: requires the AGE graph backend"
    try:
        res = await graph.supersede_discovery(new_id=new_id, old_id=old_id)
    except Exception as exc:  # noqa: BLE001 — link is best-effort, never crash the write
        logger.warning(f"[KG] supersession edge {new_id[:8]}->{old_id[:8]} failed: {exc}")
        return f"supersession link not recorded: {exc}"
    if not (isinstance(res, dict) and res.get("success")):
        return f"supersession link not recorded: {(res or {}).get('error', 'unknown')}"
    return None


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

_AGENT_DISPLAY_LOOKUP_FIELDS = (
    "public_agent_id",
    "structured_id",
    "label",
    "display_name",
)


def _agent_metadata_text(meta: Any, field: str) -> Optional[str]:
    """Return one normalized textual metadata field."""
    value = getattr(meta, field, None)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _build_agent_display_payload(
    uuid_key: str, meta: Any, fallback_handle: str
) -> Dict[str, Any]:
    """Build the stable S22 display payload for one metadata record."""
    public_agent_id = _agent_metadata_text(meta, "public_agent_id")
    structured_id = _agent_metadata_text(meta, "structured_id")
    public_handle = public_agent_id or structured_id or fallback_handle
    display_name = (
        _agent_metadata_text(meta, "display_name")
        or _agent_metadata_text(meta, "label")
        or public_handle
    )

    payload: Dict[str, Any] = {"uuid": uuid_key}
    if public_handle:
        payload["agent_id"] = public_handle
        payload["structured_agent_id"] = public_handle
    if display_name:
        payload["display_name"] = display_name
    if display_name and display_name not in (
        public_agent_id,
        structured_id,
    ):
        payload["label_source"] = "claimed"
    elif display_name or public_handle:
        payload["label_source"] = "auto"
    else:
        payload["label_source"] = "uuid"
    return payload


def _agent_metadata_matches(meta: Any, agent_id: str) -> bool:
    """Return whether a metadata record exposes the requested alias."""
    return any(
        getattr(meta, field, None) == agent_id
        for field in _AGENT_DISPLAY_LOOKUP_FIELDS
    )


def _find_agent_display_metadata(
    agent_id: str,
) -> Optional[tuple[str, Any]]:
    """Locate agent metadata by registry UUID or a supported alias."""
    metadata = mcp_server.agent_metadata
    if agent_id in metadata:
        return agent_id, metadata[agent_id]
    for uuid_key, meta in metadata.items():
        if _agent_metadata_matches(meta, agent_id):
            return uuid_key, meta
    return None


def _resolve_agent_display(agent_id: str) -> Dict[str, Any]:
    """Resolve a registry UUID or alias to S22-shaped display info."""
    try:
        match = _find_agent_display_metadata(agent_id)
        if match:
            uuid_key, meta = match
            return _build_agent_display_payload(uuid_key, meta, agent_id)
    except Exception:
        pass
    return {"agent_id": agent_id, "display_name": agent_id}


def _agent_display_for_response(agent_id: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Return display info enriched with the current call's proven identity.

    ``_resolve_agent_display`` is metadata-only and must not invent proof
    strength. For current-caller response blocks, mirror the same
    ``agent_signature`` source used by ``success_response`` so top-level
    ``agent.identity_assurance`` cannot disagree with the final envelope.
    """
    raw_display = arguments.get("_agent_display")
    if isinstance(raw_display, dict):
        agent_display = dict(raw_display)
    else:
        agent_display = _resolve_agent_display(agent_id)

    try:
        from ..support import agent_auth as _auth

        signature = _auth.compute_agent_signature(arguments=arguments)
    except Exception as exc:
        logger.debug("Could not enrich KG agent display with signature: %s", exc)
        return agent_display

    if not isinstance(signature, dict) or not signature.get("uuid"):
        return agent_display

    for key in (
        "uuid",
        "agent_id",
        "structured_agent_id",
        "display_name",
        "label_source",
        "identity_context",
        "identity_assurance",
    ):
        if key in signature:
            agent_display[key] = signature[key]

    return agent_display


_ANONYMOUS_WRITER_KEY_ENV = "UNITARES_CONTINUITY_TOKEN_SECRET"
_ANONYMOUS_WRITER_FALLBACK_KEY = secrets.token_bytes(32)


def _pseudonymize_anonymous_writer_source(source: str) -> str:
    """Return a keyed, non-reversible digest for a session-derived identifier."""
    configured_key = os.getenv(_ANONYMOUS_WRITER_KEY_ENV)
    key = (
        configured_key.encode("utf-8")
        if configured_key
        else _ANONYMOUS_WRITER_FALLBACK_KEY
    )
    return hashlib.pbkdf2_hmac(
        "sha256",
        source.encode("utf-8"),
        key,
        100_000,
        dklen=6,
    ).hex()


def _derive_anonymous_writer_id(arguments: Dict[str, Any]) -> str:
    """Derive a stable low-friction writer ID for anonymous low-severity writes.

    A deployment continuity secret keeps the pseudonym stable across restarts.
    Without one, stability is intentionally limited to the current server process.
    """
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
        digest = _pseudonymize_anonymous_writer_source(str(source))
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


class _StoreResponseError(Exception):
    """Abort a store operation with an already-structured MCP error."""

    def __init__(self, response: TextContent):
        super().__init__()
        self.response = response


@dataclass(frozen=True)
class _KnowledgeStoreRequest:
    arguments: Dict[str, Any]
    agent_id: str
    discovery_type: Any
    summary: Any
    supersedes_id: Optional[str]
    display_name_warning: Optional[str]
    is_anonymous_writer: bool


@dataclass
class _KnowledgeStoreState:
    request: _KnowledgeStoreRequest
    graph: Any
    summary: Any
    details: Any = ""
    truncation_info: dict[str, str] = field(default_factory=dict)
    response_to: Optional[ResponseTo] = None
    severity: Optional[str] = None
    provenance: Optional[dict[str, Any]] = None
    provenance_chain: Any = None
    discovery: Optional[DiscoveryNode] = None
    supersedes_target: Any = None
    supersedes_warning: Optional[str] = None
    similar: list[Any] = field(default_factory=list)
    similar_discoveries: list[dict[str, Any]] = field(default_factory=list)


def _resolve_store_writer(
    arguments: Dict[str, Any],
) -> tuple[str, Optional[TextContent], Optional[str], bool]:
    """Resolve the writer using the severity-dependent identity policy."""
    raw_severity = str(arguments.get("severity", "low")).lower()
    if raw_severity not in {"high", "critical"}:
        agent_id, error, is_anonymous = _resolve_low_friction_writer(arguments)
        return agent_id, error, None, is_anonymous

    agent_id, error = require_registered_agent(arguments)
    if error:
        return agent_id, error, None, False
    display_name_error, display_name_warning = _check_display_name_required(agent_id, arguments)
    return agent_id, display_name_error, display_name_warning, False


def _parse_single_store_request(
    arguments: Dict[str, Any],
    agent_id: str,
    display_name_warning: Optional[str],
    is_anonymous_writer: bool,
) -> _KnowledgeStoreRequest:
    """Validate the fields needed before opening the graph backend."""
    discovery_type = _normalize_discovery_type(arguments.get("discovery_type", "note"))
    if discovery_type not in VALID_DISCOVERY_TYPES:
        raise _StoreResponseError(
            _invalid_enum_response(
                "discovery_type",
                discovery_type,
                VALID_DISCOVERY_TYPES,
                tip="Tip: use 'bug_found' (or shorthand 'bug').",
            )
        )

    summary, error = require_argument(
        arguments,
        "summary",
        "summary is required - what did you discover/learn?",
    )
    if error:
        raise _StoreResponseError(error)

    supersedes_id = arguments.get("supersedes")
    if supersedes_id is not None:
        supersedes_id = str(supersedes_id).strip()
        if not supersedes_id:
            raise _StoreResponseError(error_response("supersedes parameter cannot be empty string"))

    return _KnowledgeStoreRequest(
        arguments=arguments,
        agent_id=agent_id,
        discovery_type=discovery_type,
        summary=summary,
        supersedes_id=supersedes_id,
        display_name_warning=display_name_warning,
        is_anonymous_writer=is_anonymous_writer,
    )


def _truncate_store_content(state: _KnowledgeStoreState) -> None:
    """Normalize text inputs and retain caller-visible truncation metadata."""
    arguments = state.request.arguments
    raw_summary = state.summary
    raw_details = arguments.get("details") or arguments.get("content") or ""
    leaked_marker = _detect_toolcall_markup_leak(raw_summary, raw_details)
    if leaked_marker:
        field = "summary" if isinstance(raw_summary, str) and leaked_marker in raw_summary else "content"
        raise _StoreResponseError(_degenerate_write_response(leaked_marker, field))

    if len(raw_summary) > MAX_SUMMARY_LEN:
        state.truncation_info["summary"] = f"Truncated from {len(raw_summary)} to {MAX_SUMMARY_LEN} chars"
        truncated = raw_summary[:MAX_SUMMARY_LEN]
        for end_char in [". ", "! ", "? "]:
            last_end = truncated.rfind(end_char, MAX_SUMMARY_LEN - 100)
            if last_end > 0:
                truncated = truncated[: last_end + 1]
                break
        else:
            last_space = truncated.rfind(" ")
            if last_space > MAX_SUMMARY_LEN - 50:
                truncated = truncated[:last_space]
        state.summary = truncated.rstrip() + "..."

    if len(raw_details) > MAX_DETAILS_LEN:
        state.truncation_info["details"] = f"Truncated from {len(raw_details)} to {MAX_DETAILS_LEN} chars"
        raw_details = raw_details[:MAX_DETAILS_LEN] + "... [truncated]"
    state.details = raw_details


def _parse_store_response_to(arguments: Dict[str, Any]) -> Optional[ResponseTo]:
    """Parse the optional typed link to a parent discovery."""
    response_data = arguments.get("response_to")
    if not response_data:
        return None
    if not (isinstance(response_data, dict) and "discovery_id" in response_data and "response_type" in response_data):
        return None

    parent_id = str(response_data["discovery_id"]).strip()
    if not parent_id:
        raise _StoreResponseError(error_response("Invalid response_to.discovery_id (empty)"))
    response_type = response_data["response_type"]
    if response_type not in VALID_RESPONSE_TYPES:
        raise _StoreResponseError(
            error_response(f"Invalid response_type '{response_type}'. Valid: {sorted(VALID_RESPONSE_TYPES)}")
        )
    return ResponseTo(discovery_id=parent_id, response_type=response_type)


def _parse_store_severity(arguments: Dict[str, Any]) -> Optional[str]:
    severity = arguments.get("severity")
    if severity is None:
        return None
    severity = str(severity).lower()
    if severity not in VALID_SEVERITIES:
        raise _StoreResponseError(_invalid_enum_response("severity", severity, VALID_SEVERITIES))
    return severity


def _parse_store_confidence(arguments: Dict[str, Any]) -> Optional[float]:
    raw_confidence = arguments.get("confidence")
    if raw_confidence is None:
        return None
    try:
        return max(0.0, min(1.0, float(raw_confidence)))
    except (ValueError, TypeError):
        return None


async def _capture_store_provenance(arguments: Dict[str, Any], agent_id: str) -> tuple[dict[str, Any], Any]:
    """Capture best-effort identity, lineage, and S22 write context."""
    system_version = getattr(mcp_server, "SERVER_VERSION", "unknown")
    provenance = None
    provenance_chain = None
    try:
        from src.provenance_context import attach_s22_context, build_s22_write_context
        from ..identity.shared import _get_lineage

        meta = None
        if agent_id in mcp_server.agent_metadata:
            meta = mcp_server.agent_metadata[agent_id]
            monitor_state = {}
            if agent_id in mcp_server.monitors:
                state = mcp_server.monitors[agent_id].state
                monitor_state = {
                    "regime": state.regime,
                    "coherence": round(state.coherence, 6),
                    "energy": round(state.E, 6),
                    "entropy": round(state.S, 6),
                    "void_active": state.void_active,
                }
            provenance = {
                "system_version": system_version,
                "agent_state": {
                    "status": meta.status,
                    "health": meta.health_status,
                    "total_updates": meta.total_updates,
                    **monitor_state,
                },
                "captured_at": _utc_now_iso(),
            }
            provenance["writer_label_at_write"] = (
                getattr(meta, "display_name", None)
                or getattr(meta, "label", None)
                or getattr(meta, "structured_id", None)
                or agent_id
            )
            writer_session = arguments.get("client_session_id")
            if not writer_session:
                try:
                    from ..context import get_context_client_session_id

                    writer_session = get_context_client_session_id()
                except Exception:
                    writer_session = None
            if writer_session:
                provenance["writer_session_id_at_write"] = writer_session

        provenance_chain = await _build_s7_provenance_chain_with_fallback(agent_id, meta, _get_lineage)
        from src.provenance_context import classify_fork_for_s22_context

        episode_fork_kind, identity_lineage_fork = classify_fork_for_s22_context(meta, agent_id)
        s22_context = build_s22_write_context(
            arguments,
            meta=meta,
            context_source="knowledge.store",
            default_governance_mode="explicit",
            episode_fork_kind=episode_fork_kind,
            identity_lineage_fork=identity_lineage_fork,
        )
        provenance = attach_s22_context(provenance, s22_context)
    except Exception as exc:
        logger.debug(f"Could not capture provenance: {exc}")

    if provenance is None:
        provenance = {
            "system_version": system_version,
            "captured_at": _utc_now_iso(),
        }
    elif "system_version" not in provenance:
        provenance["system_version"] = system_version

    from src.knowledge_graph import tag_provenance_source as _tag_src

    return _tag_src(provenance, "explicit_store"), provenance_chain


async def _build_store_discovery(state: _KnowledgeStoreState) -> None:
    request = state.request
    arguments = request.arguments
    state.response_to = _parse_store_response_to(arguments)
    state.severity = _parse_store_severity(arguments)
    state.provenance, state.provenance_chain = await _capture_store_provenance(arguments, request.agent_id)
    state.discovery = DiscoveryNode(
        id=_new_discovery_id(),
        agent_id=request.agent_id,
        type=request.discovery_type,
        summary=state.summary,
        details=state.details,
        tags=normalize_tags(arguments.get("tags", [])),
        severity=state.severity,
        status=arguments.get("status") or "open",
        response_to=state.response_to,
        references_files=arguments.get("related_files", []),
        provenance=state.provenance,
        provenance_chain=state.provenance_chain,
        confidence=_parse_store_confidence(arguments),
    )


async def _prepare_store_supersession(state: _KnowledgeStoreState) -> None:
    supersedes_id = state.request.supersedes_id
    if not supersedes_id:
        return
    state.supersedes_target = await state.graph.get_discovery(supersedes_id)
    if state.supersedes_target is None:
        state.supersedes_warning = (
            f"supersedes target '{supersedes_id}' not found; new discovery will be stored without flip"
        )
        return

    from src.knowledge_graph_lifecycle import KnowledgeGraphLifecycle

    lifecycle = KnowledgeGraphLifecycle()
    if lifecycle.get_lifecycle_policy(state.supersedes_target) == "permanent":
        target = state.supersedes_target
        raise _StoreResponseError(
            error_response(
                f"Cannot supersede permanent discovery '{supersedes_id}' "
                f"(type={target.type}, tags={target.tags}). "
                "Use knowledge(action='update') with explicit operator action "
                "to override."
            )
        )


async def _link_similar_store_discoveries(state: _KnowledgeStoreState) -> None:
    if not state.request.arguments.get("auto_link_related", True):
        return
    from .synthesis import is_rollup

    candidates = await state.graph.find_similar(state.discovery, limit=8)
    state.similar = [item for item in candidates if not is_rollup(item)][:5]
    state.discovery.related_to = [item.id for item in state.similar]
    state.similar_discoveries = [item.to_dict(include_details=False) for item in state.similar]


def _authorize_store_discovery(state: _KnowledgeStoreState) -> None:
    if state.discovery.severity not in {"high", "critical"}:
        return
    from ..utils import verify_agent_ownership

    if verify_agent_ownership(state.request.agent_id, state.request.arguments):
        return
    raise _StoreResponseError(
        error_response(
            "Authentication required for high-severity discoveries.",
            error_code="AUTH_REQUIRED",
            error_category="auth_error",
            recovery={
                "action": "Ensure your session is bound to this agent",
                "related_tools": ["identity"],
                "workflow": "Identity auto-binds on first tool call. Use identity() to check binding.",
            },
        )
    )


async def _persist_store_discovery(state: _KnowledgeStoreState) -> None:
    request = state.request
    discovery = state.discovery
    await state.graph.add_discovery(discovery)
    await _broadcast_knowledge_write(discovery, request.agent_id)
    if request.supersedes_id and state.supersedes_target is not None:
        await state.graph.update_discovery(
            request.supersedes_id,
            {
                "status": "superseded",
                "superseded_by": discovery.id,
                "updated_at": _utc_now_iso(),
            },
        )
        await _record_supersession_edge(
            state.graph,
            new_id=discovery.id,
            old_id=request.supersedes_id,
        )


def _attach_store_response_hints(response: dict[str, Any], state: _KnowledgeStoreState) -> None:
    request = state.request
    if request.is_anonymous_writer:
        response["agent_mode"] = "anonymous"
        response["_identity_hint"] = (
            "Stored under a lightweight anonymous writer ID. Bind an identity first if you want authorship continuity."
        )
    response["_resolve_when_done"] = (
        "When this is addressed, close the loop: "
        f"knowledge(action='update', discovery_id='{state.discovery.id}', "
        "status='resolved')"
    )
    if request.supersedes_id:
        if state.supersedes_target is not None:
            response["superseded"] = request.supersedes_id
        elif state.supersedes_warning:
            response["_supersedes_warning"] = state.supersedes_warning
    if request.display_name_warning:
        response["_name_hint"] = request.display_name_warning
    if state.truncation_info:
        response["_truncated"] = state.truncation_info
        response["_tip"] = (
            "Content was truncated. For longer content, split into multiple "
            f"discoveries or use the details field ({MAX_DETAILS_LEN} char limit)."
        )


def _attach_store_related_discoveries(response: dict[str, Any], state: _KnowledgeStoreState) -> None:
    if not state.similar_discoveries:
        return
    response["related_discoveries"] = state.similar_discoveries
    open_similar = [item for item in state.similar if item.status == "open"]
    if len(open_similar) < 3:
        return
    unique_agents = {item.agent_id for item in open_similar}
    response["consolidation_hint"] = (
        f"This issue has been found {len(open_similar)} times by "
        f"{len(unique_agents)} agent(s), all still open. Consider superseding "
        "older entries or resolving them."
    )


def _build_store_response(state: _KnowledgeStoreState) -> Sequence[TextContent]:
    request = state.request
    agent_display = _agent_display_for_response(request.agent_id, request.arguments)
    display_name = agent_display.get("display_name", request.agent_id)
    response = {
        "message": f"Discovery stored for agent '{display_name}'",
        "discovery_id": state.discovery.id,
        "agent": agent_display,
        "discovery": state.discovery.to_dict(include_details=False),
    }
    _attach_store_response_hints(response, state)
    _attach_store_related_discoveries(response, state)
    return success_response(response, arguments=request.arguments)


async def _execute_single_store(request: _KnowledgeStoreRequest, graph: Any) -> Sequence[TextContent]:
    state = _KnowledgeStoreState(
        request=request,
        graph=graph,
        summary=request.summary,
    )
    _truncate_store_content(state)
    await _build_store_discovery(state)
    await _prepare_store_supersession(state)
    await _clamp_confidence_to_coherence(state.discovery, request.agent_id)
    await _link_similar_store_discoveries(state)
    _authorize_store_discovery(state)
    await _persist_store_discovery(state)
    return _build_store_response(state)


@mcp_tool("store_knowledge_graph", timeout=20.0, register=False)
async def handle_store_knowledge_graph(
    arguments: Dict[str, Any],
) -> Sequence[TextContent]:
    """Store one discovery or delegate a discovery batch."""
    arguments = apply_param_aliases("store_knowledge_graph", arguments)
    agent_id, error, display_name_warning, is_anonymous_writer = _resolve_store_writer(arguments)
    if error:
        return [error]

    from ..utils import check_agent_can_operate

    blocked = check_agent_can_operate(agent_id)
    if blocked:
        return [blocked]
    if arguments.get("discoveries") is not None:
        return await _handle_store_knowledge_graph_batch(arguments, agent_id)

    arguments["_tool_name"] = "store_knowledge_graph"
    try:
        request = _parse_single_store_request(
            arguments,
            agent_id,
            display_name_warning,
            is_anonymous_writer,
        )
        graph = await get_knowledge_graph()
        return await _execute_single_store(request, graph)
    except _StoreResponseError as exc:
        return [exc.response]
    except ValueError as exc:
        error_message = str(exc)
        if "rate limit" in error_message.lower():
            return [
                error_response(
                    error_message,
                    recovery={
                        "action": "Wait before storing more discoveries, or reduce batch size",
                        "related_tools": [KNOWLEDGE_SEARCH_TOOL],
                    },
                )
            ]
        return [error_response(error_message)]
    except Exception as exc:
        return [error_response(f"Failed to store knowledge: {str(exc)}")]


class _SearchParameterError(ValueError):
    """Caller-visible validation error raised while parsing a KG search."""


@dataclass(frozen=True)
class _KnowledgeSearchRequest:
    arguments: Dict[str, Any]
    limit: int
    include_details: bool
    include_provenance: bool
    synthesize: bool
    query_text: Any
    agent_id: Optional[str]
    search_mode_requested: str
    operator_forced: Optional[str]
    exclude_labels: set[str]
    tags: Optional[list[str]]
    discovery_type: Any
    severity: Any
    status: Any
    include_archived: bool
    include_cold: bool

    @property
    def query_terms(self) -> list[str]:
        return str(self.query_text).split() if self.query_text else []

    @property
    def query_term_count(self) -> int:
        return len(self.query_terms)


@dataclass
class _KnowledgeSearchState:
    request: _KnowledgeSearchRequest
    graph: Any
    results: list[Any] = field(default_factory=list)
    candidates: list[Any] = field(default_factory=list)
    search_mode: str = ""
    operator_used: str = "N/A"
    fields_searched: list[str] = field(default_factory=list)
    semantic_scores: dict[str, float] = field(default_factory=dict)
    rerank_scores: dict[str, float] = field(default_factory=dict)
    rrf_scores: dict[str, float] = field(default_factory=dict)
    fts_anchor_ids: Optional[set[str]] = None
    search_degraded_warning: Optional[str] = None
    semantic_skipped_reason: Optional[str] = None
    hybrid_skipped_reason: Optional[str] = None
    fts_fallback_skipped_reason: Optional[str] = None
    fts_operator_used: Optional[str] = None
    fts_fallback_used: bool = False
    fallback_used: bool = False
    fallback_explanation: Optional[str] = None
    min_similarity: Any = 0.3
    rerank_on: bool = False
    rerank_pool_size: int = 0
    first_stage_limit: int = 0
    hybrid_on: bool = False
    graph_expand_on: bool = False
    use_semantic: bool = False
    hybrid_path: bool = False


def _parse_knowledge_search_request(
    arguments: Dict[str, Any],
) -> _KnowledgeSearchRequest:
    search_mode = str(arguments.get("search_mode") or "auto").lower()
    if search_mode not in {"auto", "fts", "semantic", "hybrid"}:
        raise _SearchParameterError(
            f"Invalid search_mode {search_mode!r}; expected one of: auto, fts, semantic, hybrid"
        )

    operator_raw = arguments.get("operator")
    operator_forced = None
    if operator_raw is not None:
        operator_forced = str(operator_raw).upper()
        if operator_forced not in {"AND", "OR"}:
            raise _SearchParameterError(f"Invalid operator {operator_raw!r}; expected 'AND' or 'OR'")

    exclude_labels_raw = arguments.get("exclude_agent_labels") or []
    exclude_labels = (
        {str(label).strip().lower() for label in exclude_labels_raw if str(label).strip()}
        if isinstance(exclude_labels_raw, (list, tuple))
        else set()
    )
    status = arguments.get("status")
    if isinstance(status, str) and status.lower() == "active":
        status = "open"

    return _KnowledgeSearchRequest(
        arguments=arguments,
        limit=arguments.get("limit") or config.KNOWLEDGE_QUERY_DEFAULT_LIMIT,
        include_details=arguments.get("include_details", False),
        include_provenance=arguments.get("include_provenance", False),
        synthesize=arguments.get("synthesize", False),
        query_text=arguments.get("query") or arguments.get("text"),
        agent_id=arguments.get("agent_id"),
        search_mode_requested=search_mode,
        operator_forced=operator_forced,
        exclude_labels=exclude_labels,
        tags=normalize_tags(arguments.get("tags", [])) or None,
        discovery_type=arguments.get("discovery_type"),
        severity=arguments.get("severity"),
        status=status,
        include_archived=arguments.get("include_archived", False),
        include_cold=arguments.get("include_cold", False),
    )


def _validate_search_backend(state: _KnowledgeSearchState) -> tuple[bool, bool]:
    """Validate a forced mode and return semantic/FTS capability flags."""
    request = state.request
    graph = state.graph
    has_semantic = hasattr(graph, "semantic_search")
    has_fts = hasattr(graph, "full_text_search")
    backend_label = graph.__class__.__name__

    if request.search_mode_requested == "semantic" and not has_semantic:
        raise _SearchParameterError(
            "search_mode=semantic requires a backend with semantic_search; "
            f"active backend {backend_label} has none. "
            "Use search_mode=fts, or set UNITARES_KNOWLEDGE_BACKEND=age."
        )
    if request.search_mode_requested == "hybrid" and not (has_semantic and has_fts):
        missing = []
        if not has_semantic:
            missing.append("semantic_search")
        if not has_fts:
            missing.append("full_text_search")
        raise _SearchParameterError(
            "search_mode=hybrid requires both semantic and FTS; "
            f"active backend {backend_label} is missing {', '.join(missing)}."
        )
    if request.search_mode_requested == "fts" and not has_fts:
        raise _SearchParameterError(
            f"search_mode=fts requires full_text_search; active backend {backend_label} has none."
        )
    return has_semantic, has_fts


def _select_search_modes(
    state: _KnowledgeSearchState,
    *,
    has_semantic: bool,
    has_fts: bool,
) -> None:
    request = state.request
    explicit_semantic = request.arguments.get("semantic")
    backend_label = state.graph.__class__.__name__

    if request.search_mode_requested in ("semantic", "hybrid"):
        state.use_semantic = True
    elif request.search_mode_requested == "fts":
        state.use_semantic = False
        if has_semantic:
            state.semantic_skipped_reason = "caller forced search_mode=fts"
    elif explicit_semantic is False:
        state.use_semantic = False
        state.semantic_skipped_reason = "caller passed semantic=false"
    elif explicit_semantic is True:
        if not has_semantic:
            raise _SearchParameterError(
                f"semantic=true requires a backend with semantic_search; active backend {backend_label} has none."
            )
        state.use_semantic = True
    else:
        state.use_semantic = has_semantic
        if not has_semantic:
            state.semantic_skipped_reason = (
                f"backend {backend_label} has no semantic_search (set UNITARES_KNOWLEDGE_BACKEND=age to enable)"
            )

    state.hybrid_path = request.search_mode_requested == "hybrid" or (
        request.search_mode_requested == "auto" and state.hybrid_on and state.use_semantic and has_fts
    )
    if state.hybrid_path and request.search_mode_requested == "auto" and request.query_term_count > 12:
        state.hybrid_path = False
        state.hybrid_skipped_reason = (
            f"auto hybrid skipped for {request.query_term_count}-term query "
            "(limit 12); use search_mode='hybrid' to force RRF fusion"
        )


async def _expand_hybrid_neighbors(
    state: _KnowledgeSearchState,
    fused: list[tuple[str, float]],
    pool: dict[str, Any],
    expand_with_neighbors: Any,
) -> list[tuple[str, float]]:
    if not state.graph_expand_on:
        return fused

    seed_neighbors: Dict[str, set] = {}
    for seed_id, _ in fused[:10]:
        seed_doc = pool.get(seed_id)
        if seed_doc is None:
            continue
        neighbors = set(seed_doc.related_to or [])
        neighbors.update(getattr(seed_doc, "responses_from", None) or [])
        if seed_doc.response_to:
            neighbors.add(seed_doc.response_to.discovery_id)
        neighbors.discard(seed_id)
        seed_neighbors[seed_id] = neighbors

    fused = expand_with_neighbors(
        fused,
        seed_neighbors,
        edge_weight=0.5,
        max_seeds=10,
    )
    missing_ids = [did for did, _ in fused if did not in pool]
    if not missing_ids:
        return fused

    import asyncio as _asyncio

    capped = missing_ids[:30]
    fetched = await _asyncio.gather(
        *(state.graph.get_discovery(discovery_id) for discovery_id in capped),
        return_exceptions=True,
    )
    for discovery_id, document in zip(capped, fetched):
        if isinstance(document, Exception):
            logger.debug(
                "[KG_SEARCH] neighbor fetch failed for %s...: %s",
                discovery_id[:8],
                document,
            )
        elif document is not None:
            pool[discovery_id] = document
    return fused


async def _retrieve_hybrid_candidates(
    state: _KnowledgeSearchState,
    *,
    rrf_fuse: Any,
    apply_tag_boost: Any,
    expand_with_neighbors: Any,
) -> None:
    import asyncio as _asyncio

    request = state.request
    min_similarity = request.arguments.get("min_similarity")
    state.min_similarity = 0.3 if min_similarity is None else min_similarity
    fetch_limit = max(state.first_stage_limit, 50)
    fts_operator = request.operator_forced or "AND"
    semantic_raw, fts_raw = await _asyncio.gather(
        state.graph.semantic_search(
            str(request.query_text),
            limit=fetch_limit,
            min_similarity=state.min_similarity,
        ),
        state.graph.full_text_search(
            str(request.query_text),
            limit=fetch_limit,
            operator=fts_operator,
        ),
    )
    state.fts_operator_used = fts_operator

    semantic_results = []
    if isinstance(semantic_raw, tuple) and len(semantic_raw) == 2 and isinstance(semantic_raw[1], dict):
        state.search_degraded_warning = (
            "Semantic search unavailable: "
            f"{semantic_raw[1].get('message', 'unknown error')}. "
            "Falling back to FTS-only in fusion."
        )
        logger.warning("[KG_SEARCH] %s", state.search_degraded_warning)
    else:
        semantic_results = list(semantic_raw)
    fts_results = list(fts_raw)

    semantic_ids = [document.id for document, _ in semantic_results]
    fts_ids = [document.id for document in fts_results]
    state.fts_anchor_ids = set(fts_ids)
    fused = rrf_fuse([semantic_ids, fts_ids], k=60)
    pool = {document.id: document for document, _ in semantic_results}
    for document in fts_results:
        pool.setdefault(document.id, document)

    if request.tags:
        tags_by_id = {discovery_id: (document.tags or []) for discovery_id, document in pool.items()}
        fused = apply_tag_boost(fused, tags_by_id, request.tags)

    fused = await _expand_hybrid_neighbors(
        state,
        fused,
        pool,
        expand_with_neighbors,
    )
    state.candidates = [pool[discovery_id] for discovery_id, _ in fused if discovery_id in pool]
    state.semantic_scores = {document.id: score for document, score in semantic_results}
    state.rrf_scores = dict(fused)
    state.search_mode = "hybrid_rrf_graph" if state.graph_expand_on else "hybrid_rrf"


async def _retrieve_semantic_candidates(state: _KnowledgeSearchState) -> None:
    request = state.request
    min_similarity = request.arguments.get("min_similarity")
    state.min_similarity = 0.3 if min_similarity is None else min_similarity
    semantic_results = await state.graph.semantic_search(
        str(request.query_text),
        limit=state.first_stage_limit,
        min_similarity=state.min_similarity,
    )
    if isinstance(semantic_results, tuple) and len(semantic_results) == 2 and isinstance(semantic_results[1], dict):
        state.search_degraded_warning = (
            "Semantic search unavailable: "
            f"{semantic_results[1].get('message', 'unknown error')}. "
            "Falling back to text search."
        )
        logger.warning("[KG_SEARCH] %s", state.search_degraded_warning)
        state.use_semantic = False
        return

    state.candidates = [document for document, _ in semantic_results]
    state.semantic_scores = {document.id: score for document, score in semantic_results}
    state.search_mode = "semantic"


async def _retrieve_fts_candidates(state: _KnowledgeSearchState) -> None:
    request = state.request
    base_limit = int(min(max(request.limit * 5, request.limit), 500))
    candidate_limit = max(base_limit, state.rerank_pool_size) if state.rerank_on else base_limit
    primary_operator = request.operator_forced or "AND"
    state.candidates = await state.graph.full_text_search(
        str(request.query_text),
        limit=candidate_limit,
        operator=primary_operator,
    )
    state.fts_operator_used = primary_operator

    if (
        not state.candidates
        and request.operator_forced is None
        and primary_operator == "AND"
        and request.query_term_count > 1
    ):
        if request.query_term_count <= 24:
            state.candidates = await state.graph.full_text_search(
                str(request.query_text),
                limit=candidate_limit,
                operator="OR",
            )
            if state.candidates:
                state.fts_operator_used = "OR"
                state.fts_fallback_used = True
        else:
            state.fts_fallback_skipped_reason = (
                f"automatic OR fallback skipped for {request.query_term_count}-term "
                "query (limit 24); pass operator='OR' to request broad recall"
            )
    state.fts_anchor_ids = {document.id for document in state.candidates}
    state.search_mode = "fts"


async def _retrieve_substring_candidates(state: _KnowledgeSearchState) -> None:
    state.candidates = await state.graph.query(limit=200)
    state.search_mode = "substring_scan"


def _candidate_matches_search(
    document: Any,
    state: _KnowledgeSearchState,
    substring_terms: Optional[list[str]],
) -> bool:
    request = state.request
    if substring_terms:
        tags_text = " ".join(document.tags or [])
        haystack = ((document.summary or "") + "\n" + (document.details or "") + "\n" + tags_text).lower()
        if not any(term in haystack for term in substring_terms):
            return False
    if request.agent_id and document.agent_id != request.agent_id:
        return False
    if request.discovery_type and document.type != request.discovery_type:
        return False
    if request.severity and document.severity != request.severity:
        return False
    if not _candidate_status_visible(document, request):
        return False
    if request.tags and not state.search_mode.startswith("hybrid_rrf"):
        if not any(tag in set(document.tags or []) for tag in request.tags):
            return False
    return True


def _candidate_status_visible(
    document: Any, request: _KnowledgeSearchRequest
) -> bool:
    if request.status:
        return document.status == request.status
    if not request.include_archived and document.status == "archived":
        return False
    return request.include_cold or document.status != "cold"


async def _filter_and_rerank_candidates(state: _KnowledgeSearchState) -> None:
    request = state.request
    substring_terms = str(request.query_text).lower().split() if state.search_mode == "substring_scan" else None
    filter_cap = state.rerank_pool_size if state.rerank_on else (50 if state.hybrid_on else request.limit)
    filtered = []
    for document in state.candidates:
        if _candidate_matches_search(document, state, substring_terms):
            filtered.append(document)
            if len(filtered) >= filter_cap:
                break

    if not (state.rerank_on and filtered):
        state.results = filtered[: request.limit]
        return

    try:
        from src.reranker import rerank as _rerank

        pairs = [(document.id, f"{document.summary}\n{(document.details or '')[:2000]}") for document in filtered]
        reranked = await _rerank(
            str(request.query_text),
            pairs,
            top_k=request.limit,
            max_rerank_size=state.rerank_pool_size,
        )
        state.rerank_scores = dict(reranked)
        by_id = {document.id: document for document in filtered}
        state.results = [by_id[discovery_id] for discovery_id, _ in reranked if discovery_id in by_id]
        state.search_mode = f"{state.search_mode}_reranked" if state.search_mode else "reranked"
    except Exception as exc:
        logger.warning(
            "[KG_SEARCH] reranker failed; keeping first-stage order: %s",
            exc,
        )
        state.results = filtered[: request.limit]


async def _run_text_search(state: _KnowledgeSearchState) -> None:
    from src.reranker import reranker_enabled
    from src.retrieval import (
        apply_tag_boost,
        expand_with_neighbors,
        graph_expansion_enabled,
        hybrid_enabled,
        rrf_fuse,
    )

    request = state.request
    state.rerank_on = reranker_enabled()
    state.rerank_pool_size = 50 if state.rerank_on else 0
    state.first_stage_limit = max(request.limit * 2, state.rerank_pool_size) if state.rerank_on else request.limit * 2
    state.hybrid_on = hybrid_enabled()
    state.graph_expand_on = graph_expansion_enabled()

    has_semantic, has_fts = _validate_search_backend(state)
    _select_search_modes(state, has_semantic=has_semantic, has_fts=has_fts)
    if state.hybrid_path:
        await _retrieve_hybrid_candidates(
            state,
            rrf_fuse=rrf_fuse,
            apply_tag_boost=apply_tag_boost,
            expand_with_neighbors=expand_with_neighbors,
        )
    elif state.use_semantic:
        await _retrieve_semantic_candidates(state)

    if not state.hybrid_path and not state.use_semantic:
        if has_fts:
            await _retrieve_fts_candidates(state)
        else:
            await _retrieve_substring_candidates(state)
    await _filter_and_rerank_candidates(state)
    state.operator_used = state.fts_operator_used or "N/A"
    state.fields_searched = ["summary", "details", "tags"]


async def _run_indexed_filter_search(state: _KnowledgeSearchState) -> None:
    request = state.request
    state.results = await state.graph.query(
        agent_id=request.agent_id,
        tags=request.tags,
        type=request.discovery_type,
        severity=request.severity,
        status=request.status,
        limit=request.limit,
        exclude_archived=not request.status and not request.include_archived,
        exclude_cold=not request.status and not request.include_cold,
    )
    state.search_mode = "indexed_filters"
    state.fields_searched = [
        name
        for name, value in (
            ("agent_id", request.agent_id),
            ("tags", request.tags),
            ("type", request.discovery_type),
            ("severity", request.severity),
            ("status", request.status),
        )
        if value
    ]


async def _apply_semantic_fts_fallback(state: _KnowledgeSearchState) -> None:
    request = state.request
    if (
        state.results
        or not request.query_text
        or state.search_mode not in {"fts", "semantic"}
        or state.search_mode != "semantic"
        or not hasattr(state.graph, "full_text_search")
    ):
        return

    try:
        logger.debug(
            "Semantic search returned 0 results, falling back to FTS for %r",
            request.query_text,
        )
        primary_operator = request.operator_forced or "AND"
        candidates = await state.graph.full_text_search(
            str(request.query_text),
            limit=request.limit * 2,
            operator=primary_operator,
        )
        fallback_operator = primary_operator
        used_or_retry = False
        if (
            not candidates
            and request.operator_forced is None
            and primary_operator == "AND"
            and request.query_term_count > 1
        ):
            if request.query_term_count <= 24:
                candidates = await state.graph.full_text_search(
                    str(request.query_text),
                    limit=request.limit * 2,
                    operator="OR",
                )
                if candidates:
                    fallback_operator = "OR"
                    used_or_retry = True
            else:
                state.fts_fallback_skipped_reason = (
                    f"automatic OR fallback skipped for {request.query_term_count}-term "
                    "query (limit 24); pass operator='OR' to request broad recall"
                )

        for document in candidates:
            if not _candidate_matches_semantic_fallback(document, request):
                continue
            state.results.append(document)
            if len(state.results) >= request.limit:
                break
        if not state.results:
            return

        state.fallback_used = True
        state.search_mode = "semantic_fallback_fts"
        state.fts_operator_used = fallback_operator
        state.fts_fallback_used = used_or_retry
        state.fallback_explanation = (
            f"Semantic search found no concepts similar to '{request.query_text}' "
            f"(similarity threshold: {state.min_similarity}). "
            f"Falling back to keyword search (FTS, operator={fallback_operator}) "
            "for exact term matching."
        )
    except Exception as exc:
        logger.debug("Semantic→FTS fallback failed: %s", exc)


def _candidate_matches_semantic_fallback(
    document: Any,
    request: _KnowledgeSearchRequest,
) -> bool:
    if request.agent_id and document.agent_id != request.agent_id:
        return False
    if request.discovery_type and document.type != request.discovery_type:
        return False
    if request.severity and document.severity != request.severity:
        return False
    if request.status and document.status != request.status:
        return False
    if not request.status and not request.include_archived and document.status == "archived":
        return False
    if request.tags:
        return any(tag in set(document.tags or []) for tag in request.tags)
    return True


def _exclude_search_labels(state: _KnowledgeSearchState) -> None:
    if not state.request.exclude_labels:
        return
    filtered = []
    for document in state.results:
        display = _resolve_agent_display(document.agent_id)
        display_name = display.get("display_name", document.agent_id) or ""
        if str(display_name).strip().lower() not in state.request.exclude_labels:
            filtered.append(document)
    state.results = filtered


def _serialize_search_discoveries(
    state: _KnowledgeSearchState,
    *,
    include_details: bool,
) -> list[dict[str, Any]]:
    current_server_version = getattr(mcp_server, "SERVER_VERSION", "unknown")
    discoveries = []
    for document in state.results:
        provenance = document.provenance if isinstance(document.provenance, dict) else None
        display_name = (provenance or {}).get("writer_label_at_write")
        if not display_name:
            display = _resolve_agent_display(document.agent_id)
            display_name = display.get("display_name", document.agent_id)

        item = {"by": display_name, "summary": document.summary}
        session_at_write = (provenance or {}).get("writer_session_id_at_write")
        if session_at_write:
            item["session_id_at_write"] = session_at_write

        serialized = document.to_dict(include_details=include_details)
        item.update(
            {
                "id": serialized.get("id"),
                "type": serialized.get("type"),
                "status": serialized.get("status"),
                "tags": serialized.get("tags", []),
                "created_at": serialized.get("created_at"),
            }
        )
        if include_details and serialized.get("details"):
            item["details"] = serialized.get("details")
        item["_agent_id"] = document.agent_id
        item["system_version"] = provenance.get("system_version") if provenance else None
        if document.status == "open":
            warning = _compute_staleness_warning(document, current_server_version)
            if warning:
                item["staleness_warning"] = warning
        if state.request.include_provenance:
            item["provenance"] = document.provenance
            if document.provenance_chain:
                item["provenance_chain"] = document.provenance_chain
        discoveries.append(item)
    return discoveries


def _base_search_response(
    state: _KnowledgeSearchState,
    discoveries: list[dict[str, Any]],
    *,
    auto_details: bool,
    include_details: bool,
) -> dict[str, Any]:
    request = state.request
    detail_suffix = (
        " (details auto-included for small result set)"
        if auto_details
        else ""
        if include_details
        else " (summaries only)"
    )
    return {
        "search_mode_used": state.search_mode,
        "search_mode_requested": request.search_mode_requested,
        "operator_used": state.operator_used,
        "fields_searched": state.fields_searched,
        "query": request.query_text,
        "discoveries": discoveries,
        "count": len(state.results),
        "message": f"Found {len(state.results)} discovery(ies){detail_suffix}",
    }


def _attach_search_diagnostics(
    response: dict[str, Any],
    state: _KnowledgeSearchState,
) -> None:
    if state.semantic_skipped_reason:
        response["semantic_skipped_reason"] = state.semantic_skipped_reason
    if state.fts_operator_used:
        response["fts_operator_used"] = state.fts_operator_used
        response["fts_fallback_used"] = state.fts_fallback_used
    if state.search_degraded_warning:
        response["search_degraded"] = True
        response["search_degraded_message"] = state.search_degraded_warning
    if state.hybrid_skipped_reason:
        response["hybrid_skipped_reason"] = state.hybrid_skipped_reason
    if state.fts_fallback_skipped_reason:
        response["fts_fallback_skipped_reason"] = state.fts_fallback_skipped_reason
    if state.fallback_used:
        response["fallback_used"] = True
        response["fallback_message"] = state.fallback_explanation or (
            "No exact matches found. Retried with individual terms (OR operator)."
        )
        response["fallback_terms"] = str(state.request.query_text).split()[:3] if state.request.query_text else []


def _empty_search_hints(request: _KnowledgeSearchRequest) -> list[str]:
    hints = []
    query_words = 0
    if request.query_text:
        query_text = str(request.query_text)
        normalized = query_text.replace("_", " ").replace("-", " ")
        query_words = len([word for word in normalized.split() if word.strip()])
        if query_words >= 5:
            hints.append(
                f"Long query ({query_words} words) - try semantic search: "
                f"knowledge(action='search', query='{request.query_text}', semantic=true)"
            )
            hints.append(
                "Or broaden to key concepts: knowledge(action='search', query='"
                + ", ".join(query_text.split()[:3])
                + "')"
            )
        elif query_words >= 2:
            hints.append("Multi-word query - try semantic search (semantic=true) for conceptual matching")
            hints.append("Or search individual terms: " + ", ".join(query_text.split()[:3]))
        else:
            hints.append(f"Single term '{request.query_text}' - try broader search or use tags")
            hints.append(f"Try: knowledge(action='search', tags=['{request.query_text}']) or broaden query")
        hints.append("Alternative: Search by tags instead (knowledge(action='search', tags=['tag1', 'tag2']))")

    if request.agent_id:
        hints.append(f"Filter active: agent_id='{request.agent_id[:20]}...' - remove to search across all agents")
    if request.tags:
        hints.append(f"Filter active: {len(request.tags)} tag(s) - remove or use fewer tags for broader results")
    if request.discovery_type:
        hints.append(f"Filter active: type='{request.discovery_type}' - remove to search all discovery types")
    if request.severity:
        hints.append(f"Filter active: severity='{request.severity}' - remove to search all severities")
    return hints


def _attach_empty_search_guidance(
    response: dict[str, Any],
    state: _KnowledgeSearchState,
) -> None:
    if state.results:
        return
    request = state.request
    if request.query_text:
        record_recall_event(
            ZERO_RESULT,
            request.query_text,
            query_terms=request.query_term_count,
            search_mode=state.search_mode,
            detail={
                "hybrid_skipped": bool(state.hybrid_skipped_reason),
                "fts_or_fallback_skipped": bool(state.fts_fallback_skipped_reason),
            },
        )
    hints = _empty_search_hints(request)
    if hints:
        response["empty_results_hints"] = hints
        response["tip"] = f"No results found. {hints[0]}"
        response["all_suggestions"] = hints


def _attach_score_map(
    response: dict[str, Any],
    state: _KnowledgeSearchState,
    *,
    key: str,
    scores: dict[str, float],
    digits: int,
) -> None:
    if not scores or not state.request.query_text:
        return
    visible = {document.id: round(scores[document.id], digits) for document in state.results if document.id in scores}
    if visible:
        response[key] = visible


def _attach_search_scores_and_confidence(
    response: dict[str, Any],
    state: _KnowledgeSearchState,
) -> None:
    _attach_score_map(
        response,
        state,
        key="similarity_scores",
        scores=state.semantic_scores,
        digits=3,
    )
    _attach_score_map(
        response,
        state,
        key="rerank_scores",
        scores=state.rerank_scores,
        digits=3,
    )
    _attach_score_map(
        response,
        state,
        key="rrf_scores",
        scores=state.rrf_scores,
        digits=4,
    )

    request = state.request
    if not (request.query_text and state.results and state.fts_anchor_ids is not None):
        return
    lexical_hits = sum(1 for document in state.results if document.id in state.fts_anchor_ids)
    if lexical_hits:
        return
    response["low_confidence"] = True
    record_recall_event(
        LOW_CONFIDENCE,
        request.query_text,
        query_terms=request.query_term_count,
        search_mode=state.search_mode,
    )
    response["confidence_note"] = (
        "No result matched your query terms lexically — these are semantic-only "
        "matches and may be tangential (semantic relevance is weakly calibrated "
        "on this corpus). Rephrase with distinctive key terms, or treat these as "
        "exploratory rather than authoritative."
    )


def _attach_search_usage_hints(
    response: dict[str, Any],
    state: _KnowledgeSearchState,
    *,
    include_details: bool,
) -> None:
    request = state.request
    if request.query_text and request.query_term_count > 1:
        if state.search_mode == "fts" and not state.fallback_used:
            response["operator_note"] = (
                "Multi-term FTS ran with "
                f"operator={state.fts_operator_used or state.operator_used}. "
                "Use operator='OR' for broader recall, or tags/filters for tighter scope."
            )
        elif state.search_mode == "semantic":
            response["operator_note"] = (
                "Semantic search considers all terms together (conceptual similarity, not keyword matching)."
            )
    if not include_details:
        response["_tip"] = (
            "Add include_details=true to expand all results inline (knowledge(action='search', include_details=true))"
        )
    if len(state.results) == request.limit:
        response["_more_available"] = f"Results may be limited to {request.limit}. Use limit=N (max 100) to get more."
    if state.search_mode == "substring_scan" and not state.results and request.query_text:
        response["search_hint"] = (
            "No results with substring matching. Try: "
            "1) Use specific tags: tags=['identity', 'philosophy'] "
            "2) Search by discovery_type: discovery_type='insight' "
            "3) Use single keywords instead of phrases"
        )


async def _attach_search_synthesis(
    response: dict[str, Any],
    state: _KnowledgeSearchState,
    discoveries: list[dict[str, Any]],
) -> None:
    if not state.request.synthesize:
        return
    if len(discoveries) < 3:
        response["_synthesis_note"] = "Synthesis skipped: fewer than 3 results"
        return
    try:
        synthesis = await synthesize_results(
            discoveries=discoveries,
            query=state.request.query_text,
            max_discoveries=10,
            max_tokens=400,
        )
        if synthesis:
            response["synthesis"] = synthesis
            logger.debug(
                "Knowledge synthesis generated for %d discoveries",
                len(discoveries),
            )
    except Exception as exc:
        logger.debug("Synthesis skipped: %s", exc)
        response["_synthesis_note"] = "Synthesis unavailable (local LLM not responding)"


async def _execute_knowledge_search(state: _KnowledgeSearchState) -> dict[str, Any]:
    started_at = time.perf_counter()
    if state.request.query_text:
        await _run_text_search(state)
    else:
        await _run_indexed_filter_search(state)
    await _apply_semantic_fts_fallback(state)
    record_ms(
        f"knowledge.search.{state.search_mode}",
        (time.perf_counter() - started_at) * 1000.0,
    )

    _exclude_search_labels(state)
    auto_details = not state.request.include_details and 0 < len(state.results) <= 3
    include_details = state.request.include_details or auto_details
    discoveries = _serialize_search_discoveries(
        state,
        include_details=include_details,
    )
    await _annotate_supersession(discoveries, state.graph)

    response = _base_search_response(
        state,
        discoveries,
        auto_details=auto_details,
        include_details=include_details,
    )
    _attach_search_diagnostics(response, state)
    _attach_empty_search_guidance(response, state)
    _attach_search_usage_hints(response, state, include_details=include_details)
    _attach_search_scores_and_confidence(response, state)
    await _attach_search_synthesis(response, state, discoveries)

    writers = list({document.agent_id for document in state.results if document.agent_id})[:10]
    await _broadcast_knowledge_read(
        "search",
        _resolve_reader_agent_id(state.request.arguments),
        payload={
            "result_count": len(state.results),
            "query_present": bool(state.request.query_text),
            "query_term_count": state.request.query_term_count,
            "search_mode": state.search_mode or state.request.search_mode_requested,
            "writer_agent_ids": writers,
            "filter_agent_id": state.request.arguments.get("agent_id"),
        },
    )
    return response


@mcp_tool("search_knowledge_graph", timeout=15.0, requires_identity="pre_onboard")
async def handle_search_knowledge_graph(
    arguments: Dict[str, Any],
) -> Sequence[TextContent]:
    """Search the KG through the explicit retrieval and response pipeline."""
    arguments = apply_param_aliases("search_knowledge_graph", arguments)
    try:
        graph = await get_knowledge_graph()
        request = _parse_knowledge_search_request(arguments)
        response = await _execute_knowledge_search(_KnowledgeSearchState(request=request, graph=graph))
        return success_response(response, arguments=arguments)
    except _SearchParameterError as exc:
        return [error_response(str(exc))]
    except Exception as exc:
        return [error_response(f"Failed to search knowledge: {str(exc)}")]


@mcp_tool("get_knowledge_graph", timeout=15.0, register=False)
async def handle_get_knowledge_graph(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Get agent knowledge or read back a specific discovery.

    ``knowledge(action="get")`` is the migration target for the legacy
    ``get_knowledge_graph`` tool, whose historical contract is agent-scoped.
    In practice, agents commonly pass a discovery id returned from search. Keep
    that path useful by routing it to the existing details reader instead of
    falling into the agent identity gate.
    """
    if arguments.get("discovery_id"):
        if arguments.get("agent_id"):
            return [error_response(
                "knowledge get accepts either discovery_id or agent_id, not both",
                error_code="AMBIGUOUS_KNOWLEDGE_GET_TARGET",
                error_category="validation_error",
                recovery={
                    "action": "Choose one read target",
                    "workflow": [
                        "Use discovery_id for search-result readback",
                        "Use agent_id to list one agent's knowledge",
                    ],
                    "related_tools": ["knowledge"],
                },
                arguments=arguments,
            )]
        return await handle_get_discovery_details(arguments)

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
        agent_display = _agent_display_for_response(agent_id, arguments)
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

        # Agent-facing trust flag (same as search): mark superseded rows.
        await _annotate_supersession(discovery_list, graph)

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

@mcp_tool("list_knowledge_graph", timeout=10.0, register=False)
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

@dataclass(frozen=True)
class _KnowledgeUpdateRequest:
    arguments: Dict[str, Any]
    discovery_id: str
    status: Any
    details: Any
    resolution_note: Optional[str]
    summary: Any
    severity: Any
    discovery_type: Any
    tags: Any
    superseded_by: Any


class _UpdateResponseError(Exception):
    """Abort a discovery update with an already-structured MCP error."""

    def __init__(self, response: TextContent):
        super().__init__()
        self.response = response


def _parse_knowledge_update_request(
    arguments: Dict[str, Any],
) -> _KnowledgeUpdateRequest:
    """Validate update fields before opening the graph backend."""
    discovery_id, error = require_argument(
        arguments, "discovery_id", "discovery_id is required"
    )
    if error:
        raise _UpdateResponseError(error)

    details = arguments.get("details")
    if details is None:
        details = arguments.get("content")

    resolution_notes = arguments.get("resolution_notes")
    resolution_note = None
    if resolution_notes is not None:
        resolution_note = str(resolution_notes).strip() or None

    request = _KnowledgeUpdateRequest(
        arguments=arguments,
        discovery_id=discovery_id,
        status=arguments.get("status"),
        details=details,
        resolution_note=resolution_note,
        summary=arguments.get("summary"),
        severity=arguments.get("severity"),
        discovery_type=arguments.get("discovery_type"),
        tags=arguments.get("tags"),
        superseded_by=arguments.get("superseded_by"),
    )
    leaked_marker = _detect_toolcall_markup_leak(
        request.summary, request.details, request.resolution_note
    )
    if leaked_marker:
        field_name = (
            "summary"
            if isinstance(request.summary, str) and leaked_marker in request.summary
            else "content"
        )
        raise _UpdateResponseError(
            _degenerate_write_response(leaked_marker, field_name)
        )

    update_values = (
        request.status,
        request.details,
        request.resolution_note,
        request.summary,
        request.severity,
        request.discovery_type,
        request.tags,
        request.superseded_by,
    )
    if not any(value is not None for value in update_values):
        raise _UpdateResponseError(
            error_response(
                "At least one updatable field is required. Provide status, "
                "details/content, resolution_notes, summary, severity, "
                "discovery_type, tags, or superseded_by."
            )
        )
    return request


_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_GATED_SEVERITIES = ("high", "critical")


def _effective_update_severity(
    request: _KnowledgeUpdateRequest, discovery: DiscoveryNode
) -> Optional[str]:
    """The severity an update's gates must answer to: the higher of the two.

    Gating on the STORED severity alone leaves escalation ungated: `store()`
    demands ``require_registered_agent`` + ``verify_agent_ownership`` for
    high/critical (see ``_resolve_store_writer`` / ``_authorize_store_discovery``),
    but an update that *raises* low -> critical would be judged against the
    stored ``low`` and take the anonymous low-friction path — landing a
    critical row that ``store()`` would have refused. Same mint-vs-gate
    coupling class as #598/#1056: a gate that guards one write path is not a
    gate.

    Taking the max also keeps de-escalation gated, which matters at least as
    much: silently downgrading someone else's critical finding hides a real
    problem rather than inventing a fake one.
    """
    stored = discovery.severity
    requested = request.severity
    if requested is None:
        return stored
    requested = str(requested).lower()
    if requested not in _SEVERITY_RANK:
        # Invalid values are rejected later by _apply_update_metadata_fields
        # with a proper enum error. Gate on the stored value meanwhile rather
        # than letting an unparseable severity waive the check.
        return stored
    if stored is None:
        return requested
    stored_key = str(stored).lower()
    if stored_key not in _SEVERITY_RANK:
        return requested
    return max(
        (stored_key, requested), key=lambda value: _SEVERITY_RANK[value]
    )


def _resolve_update_writer(
    request: _KnowledgeUpdateRequest, discovery: DiscoveryNode
) -> str:
    """Apply the effective-severity identity gate for discovery updates."""
    if _effective_update_severity(request, discovery) in _GATED_SEVERITIES:
        agent_id, error = require_registered_agent(request.arguments)
    else:
        agent_id, error, _ = _resolve_low_friction_writer(request.arguments)
    if error:
        raise _UpdateResponseError(error)
    return agent_id


def _requested_non_owner_edits(
    request: _KnowledgeUpdateRequest, allowed_statuses: set[str]
) -> list[str]:
    """List fields a non-owner cannot change on a high-severity discovery."""
    requested_edits = [
        field_name
        for field_name, field_value in {
            "details/content": request.details,
            "summary": request.summary,
            "severity": request.severity,
            "discovery_type": request.discovery_type,
            "tags": request.tags,
        }.items()
        if field_value is not None
    ]
    if (
        request.resolution_note is not None
        and request.status not in allowed_statuses
    ):
        requested_edits.append("resolution_notes")
    return requested_edits


def _authorize_high_severity_update(
    request: _KnowledgeUpdateRequest,
    discovery: DiscoveryNode,
    agent_id: str,
) -> None:
    """Enforce authentication and ownership rules for sensitive updates."""
    # Effective, not stored — an update that raises severity into the gated
    # band must clear the same bar store() sets for creating it there.
    if _effective_update_severity(request, discovery) not in _GATED_SEVERITIES:
        return

    from ..utils import verify_agent_ownership

    if not verify_agent_ownership(agent_id, request.arguments):
        raise _UpdateResponseError(
            error_response(
                "Authentication required for updating high-severity discoveries.",
                error_code="AUTH_REQUIRED",
                error_category="auth_error",
                recovery={
                    "action": "Ensure your session is bound to this agent",
                    "related_tools": ["identity"],
                    "workflow": (
                        "Identity auto-binds on first tool call. "
                        "Use identity() to check binding."
                    ),
                },
            )
        )

    allowed_statuses = {"resolved", "closed", "wont_fix"}
    if discovery.agent_id == agent_id:
        return

    requested_edits = _requested_non_owner_edits(request, allowed_statuses)
    allowed_list = sorted(allowed_statuses)
    if requested_edits:
        raise _UpdateResponseError(
            error_response(
                "Permission denied: Non-owners cannot edit "
                f"{', '.join(requested_edits)} on high-severity discovery "
                f"'{request.discovery_id}'. Allowed cross-agent status values: "
                f"{allowed_list}.",
                recovery={
                    "action": (
                        "Retry with status only. "
                        f"Allowed values: {allowed_list}"
                    ),
                    "related_tools": [
                        "get_discovery_details",
                        "search_knowledge_graph",
                    ],
                },
            )
        )
    if request.status not in allowed_statuses:
        raise _UpdateResponseError(
            error_response(
                f"Permission denied: Cannot set status '{request.status}' on "
                f"high-severity discovery '{request.discovery_id}'. Allowed "
                f"cross-agent status values: {allowed_list}.",
                recovery={
                    "action": (
                        f"Use status in {allowed_list} to close another "
                        "agent's discovery"
                    ),
                    "related_tools": [
                        "get_discovery_details",
                        "search_knowledge_graph",
                    ],
                },
            )
        )


def _apply_update_text_fields(
    request: _KnowledgeUpdateRequest,
    discovery: DiscoveryNode,
    updates: dict[str, Any],
) -> None:
    """Apply summary, details, and resolution-note edits."""
    if request.summary is not None:
        updates["summary"] = str(request.summary)
    if request.details is not None:
        updates["details"] = str(request.details)
    if request.resolution_note is not None:
        base_details = (
            str(request.details)
            if request.details is not None
            else (discovery.details or "")
        ).rstrip()
        note_block = (
            f"Resolution notes ({_utc_now_iso()}):\n"
            f"{request.resolution_note}"
        )
        updates["details"] = (
            f"{base_details}\n\n{note_block}" if base_details else note_block
        )


def _apply_update_metadata_fields(
    request: _KnowledgeUpdateRequest, updates: dict[str, Any]
) -> None:
    """Validate and apply severity, type, and tags edits."""
    if request.severity is not None:
        severity = str(request.severity).lower()
        if severity not in VALID_SEVERITIES:
            raise _UpdateResponseError(
                _invalid_enum_response("severity", severity, VALID_SEVERITIES)
            )
        updates["severity"] = severity

    if request.discovery_type is not None:
        discovery_type = _normalize_discovery_type(request.discovery_type)
        if discovery_type not in VALID_DISCOVERY_TYPES:
            raise _UpdateResponseError(
                _invalid_enum_response(
                    "discovery_type",
                    discovery_type,
                    VALID_DISCOVERY_TYPES,
                )
            )
        updates["type"] = discovery_type

    if request.tags is not None:
        updates["tags"] = request.tags


def _build_discovery_updates(
    request: _KnowledgeUpdateRequest, discovery: DiscoveryNode
) -> tuple[dict[str, Any], Optional[str]]:
    """Build and validate the backend update payload."""
    updates: dict[str, Any] = {"updated_at": _utc_now_iso()}
    normalized_status = None

    if request.status is not None:
        normalized_status = str(request.status).lower()
        if normalized_status not in VALID_DISCOVERY_STATUSES:
            raise _UpdateResponseError(
                error_response(
                    f"Invalid status '{normalized_status}'. "
                    f"Valid: {sorted(VALID_DISCOVERY_STATUSES)}"
                )
            )
        updates["status"] = normalized_status
        if normalized_status == "resolved":
            updates["resolved_at"] = _utc_now_iso()

    _apply_update_text_fields(request, discovery, updates)
    _apply_update_metadata_fields(request, updates)
    return updates, normalized_status


def _build_update_response(
    request: _KnowledgeUpdateRequest,
    discovery: Optional[DiscoveryNode],
    normalized_status: Optional[str],
    supersession_warning: Optional[str],
) -> Sequence[TextContent]:
    """Render the stable update response shape."""
    message = f"Discovery '{request.discovery_id}' updated"
    if normalized_status is not None:
        message = (
            f"Discovery '{request.discovery_id}' status updated to "
            f"'{normalized_status}'"
        )

    payload = {
        "message": message,
        "discovery": (
            discovery.to_dict(include_details=False) if discovery else None
        ),
    }
    if request.superseded_by:
        payload["superseded_by"] = str(request.superseded_by)
        if supersession_warning:
            payload["supersession_warning"] = supersession_warning
    return success_response(payload, arguments=request.arguments)


async def _execute_discovery_update(
    request: _KnowledgeUpdateRequest, graph: Any
) -> Sequence[TextContent]:
    """Authorize, persist, and render one discovery update."""
    discovery = await graph.get_discovery(request.discovery_id)
    if not discovery:
        raise _UpdateResponseError(
            await _discovery_not_found(request.discovery_id, graph)
        )

    agent_id = _resolve_update_writer(request, discovery)
    _authorize_high_severity_update(request, discovery, agent_id)
    updates, normalized_status = _build_discovery_updates(request, discovery)

    updated = await graph.update_discovery(request.discovery_id, updates)
    if not updated:
        raise _UpdateResponseError(
            error_response(f"Discovery '{request.discovery_id}' not found")
        )

    supersession_warning = None
    if request.superseded_by:
        supersession_warning = await _record_supersession_edge(
            graph,
            new_id=str(request.superseded_by),
            old_id=request.discovery_id,
        )
    refreshed = await graph.get_discovery(request.discovery_id)
    return _build_update_response(
        request,
        refreshed,
        normalized_status,
        supersession_warning,
    )


@mcp_tool("update_discovery_status_graph", timeout=10.0, register=False)
async def handle_update_discovery_status_graph(
    arguments: Dict[str, Any],
) -> Sequence[TextContent]:
    """Update discovery status, details, and selected metadata."""
    try:
        request = _parse_knowledge_update_request(arguments)
    except _UpdateResponseError as error:
        return [error.response]

    try:
        graph = await get_knowledge_graph()
        return await _execute_discovery_update(request, graph)
    except _UpdateResponseError as error:
        return [error.response]
    except Exception as error:
        return [error_response(f"Failed to update discovery: {str(error)}")]


@mcp_tool("get_discovery_details", timeout=10.0, register=False)
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
        offset = _coerce_pagination_int(arguments.get("offset"), default=0, minimum=0)
        length = _coerce_pagination_int(arguments.get("length"), default=2000, minimum=1)

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
            max_depth = _coerce_pagination_int(
                arguments.get("max_chain_depth"),
                default=10,
                minimum=1,
            )

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


class _BatchItemError(Exception):
    """Abort one batch item with a plain per-item error message."""


@dataclass(frozen=True)
class _PreparedBatchDiscovery:
    discovery: DiscoveryNode
    summary: Any
    discovery_type: Any
    truncated_fields: list[str]


@dataclass
class _BatchStoreResult:
    stored: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _validate_batch_discoveries(arguments: Dict[str, Any]) -> list[Any]:
    """Validate the batch envelope before opening the graph backend."""
    discoveries = arguments.get("discoveries")
    if not isinstance(discoveries, list):
        raise _StoreResponseError(
            error_response("discoveries must be a list of discovery objects")
        )
    if not discoveries:
        raise _StoreResponseError(
            error_response("discoveries list cannot be empty")
        )
    if len(discoveries) > 10:
        raise _StoreResponseError(
            error_response(
                "Maximum 10 discoveries per batch "
                "(to prevent context overflow)"
            )
        )
    return discoveries


def _truncate_batch_summary(summary: Any) -> tuple[Any, Optional[str]]:
    """Truncate a batch summary at a nearby sentence or word boundary."""
    if len(summary) <= MAX_SUMMARY_LEN:
        return summary, None

    truncation = f"summary ({len(summary)} → {MAX_SUMMARY_LEN})"
    shortened = summary[:MAX_SUMMARY_LEN]
    for end_char in [". ", "! ", "? "]:
        last_end = shortened.rfind(end_char, MAX_SUMMARY_LEN - 100)
        if last_end > 0:
            shortened = shortened[: last_end + 1]
            break
    else:
        last_space = shortened.rfind(" ")
        if last_space > MAX_SUMMARY_LEN - 50:
            shortened = shortened[:last_space]
    return shortened.rstrip() + "...", truncation


def _truncate_batch_details(details: Any) -> tuple[Any, Optional[str]]:
    """Apply the stable details limit used by batch writes."""
    if len(details) <= MAX_DETAILS_LEN:
        return details, None
    truncation = f"details ({len(details)} → {MAX_DETAILS_LEN})"
    return details[:MAX_DETAILS_LEN] + "... [truncated]", truncation


def _parse_batch_response_to(
    disc_data: dict[str, Any],
) -> Optional[ResponseTo]:
    """Parse a response edge, preserving the legacy ignore-invalid behavior."""
    if "response_to" not in disc_data or not disc_data["response_to"]:
        return None

    response_data = disc_data["response_to"]
    required_fields = {"discovery_id", "response_type"}
    if not isinstance(response_data, dict) or not required_fields.issubset(
        response_data
    ):
        return None

    parent_id = str(response_data["discovery_id"]).strip()
    if not parent_id:
        raise _BatchItemError("Invalid response_to.discovery_id (empty)")

    response_type = response_data["response_type"]
    if response_type not in VALID_RESPONSE_TYPES:
        return None
    return ResponseTo(
        discovery_id=parent_id,
        response_type=response_type,
    )


def _parse_batch_severity(
    disc_data: dict[str, Any],
    idx: int,
    result: _BatchStoreResult,
) -> Optional[str]:
    """Validate severity without fabricating a fallback value."""
    severity = disc_data.get("severity")
    if severity is None:
        return None

    normalized = str(severity).lower()
    if normalized in VALID_SEVERITIES:
        return normalized

    result.warnings.append(
        f"Discovery {idx}: invalid severity '{severity}' ignored "
        f"(stored unset). Valid: {sorted(VALID_SEVERITIES)}"
    )
    return None


def _parse_batch_confidence(disc_data: dict[str, Any]) -> Optional[float]:
    """Parse and clamp optional confidence, ignoring malformed values."""
    if disc_data.get("confidence") is None:
        return None
    try:
        confidence = float(disc_data["confidence"])
    except (ValueError, TypeError):
        return None
    return max(0.0, min(1.0, confidence))


def _prepare_batch_discovery(
    disc_data: Any,
    idx: int,
    agent_id: str,
    result: _BatchStoreResult,
) -> _PreparedBatchDiscovery:
    """Validate and materialize one batch discovery."""
    if not isinstance(disc_data, dict):
        raise _BatchItemError("must be a dict")

    discovery_type = _normalize_discovery_type(
        disc_data.get("discovery_type")
    )
    if not discovery_type:
        raise _BatchItemError("discovery_type is required")
    if discovery_type not in VALID_DISCOVERY_TYPES:
        raise _BatchItemError(
            f"invalid discovery_type '{discovery_type}'. "
            f"Valid: {sorted(VALID_DISCOVERY_TYPES)}."
        )

    summary = disc_data.get("summary", "")
    if not summary:
        raise _BatchItemError("summary is required")

    truncated_fields = []
    summary, summary_truncation = _truncate_batch_summary(summary)
    if summary_truncation:
        truncated_fields.append(summary_truncation)

    details = disc_data.get("details") or disc_data.get("content") or ""
    details, details_truncation = _truncate_batch_details(details)
    if details_truncation:
        truncated_fields.append(details_truncation)

    discovery_id = _new_discovery_id()
    response_to = _parse_batch_response_to(disc_data)
    severity = _parse_batch_severity(disc_data, idx, result)
    confidence = _parse_batch_confidence(disc_data)

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
        confidence=confidence,
        provenance=_tag_src(
            disc_data.get("provenance"), "explicit_store"
        ),
    )
    return _PreparedBatchDiscovery(
        discovery=discovery,
        summary=summary,
        discovery_type=discovery_type,
        truncated_fields=truncated_fields,
    )


async def _persist_batch_discovery(
    prepared: _PreparedBatchDiscovery,
    disc_data: dict[str, Any],
    graph: Any,
    agent_id: str,
    arguments: Dict[str, Any],
) -> dict[str, Any]:
    """Apply graph-side enrichment, authorization, and persistence."""
    discovery = prepared.discovery
    await _clamp_confidence_to_coherence(discovery, agent_id)

    if disc_data.get("auto_link_related", True):
        similar = await graph.find_similar(discovery, limit=3)
        discovery.related_to = [item.id for item in similar]

    if discovery.severity in ["high", "critical"]:
        from ..utils import verify_agent_ownership

        if not verify_agent_ownership(agent_id, arguments):
            raise _BatchItemError(
                "Authentication required for high-severity discoveries"
            )

    await graph.add_discovery(discovery)
    await _broadcast_knowledge_write(discovery, agent_id)
    stored_item = {
        "discovery_id": discovery.id,
        "summary": prepared.summary,
        "type": prepared.discovery_type,
    }
    if prepared.truncated_fields:
        stored_item["_truncated"] = prepared.truncated_fields
    return stored_item


async def _process_batch_discovery(
    disc_data: Any,
    idx: int,
    graph: Any,
    agent_id: str,
    arguments: Dict[str, Any],
    result: _BatchStoreResult,
) -> None:
    """Process one item while isolating its errors from the rest of the batch."""
    try:
        prepared = _prepare_batch_discovery(
            disc_data, idx, agent_id, result
        )
        stored_item = await _persist_batch_discovery(
            prepared,
            disc_data,
            graph,
            agent_id,
            arguments,
        )
        result.stored.append(stored_item)
    except _BatchItemError as error:
        result.errors.append(f"Discovery {idx}: {str(error)}")
    except ValueError as error:
        error_message = str(error)
        if "rate limit" in error_message.lower():
            result.errors.append(
                f"Discovery {idx}: Rate limit exceeded - {error_message}"
            )
        else:
            result.errors.append(
                f"Discovery {idx}: Validation error - {error_message}"
            )
    except Exception as error:
        result.errors.append(f"Discovery {idx}: {str(error)}")


def _build_batch_store_response(
    arguments: Dict[str, Any],
    discoveries: list[Any],
    result: _BatchStoreResult,
) -> Sequence[TextContent]:
    """Render aggregate batch results using the existing response contract."""
    response = {
        "message": (
            f"Stored {len(result.stored)}/{len(discoveries)} "
            "discovery/discoveries"
        ),
        "stored": result.stored,
        "total": len(discoveries),
        "success_count": len(result.stored),
        "error_count": len(result.errors),
    }
    if result.errors:
        response["errors"] = result.errors
    if result.warnings:
        response["warnings"] = result.warnings

    truncated_count = sum(
        1 for stored in result.stored if "_truncated" in stored
    )
    if truncated_count > 0:
        response["_tip"] = (
            f"{truncated_count} discovery(ies) had content truncated. "
            f"Limits: summary={MAX_SUMMARY_LEN}, details={MAX_DETAILS_LEN} chars."
        )
    return success_response(response, arguments=arguments)


async def _handle_store_knowledge_graph_batch(
    arguments: Dict[str, Any], agent_id: str
) -> Sequence[TextContent]:
    """Store up to ten discoveries while isolating per-item failures."""
    try:
        discoveries = _validate_batch_discoveries(arguments)
    except _StoreResponseError as error:
        return [error.response]

    try:
        graph = await get_knowledge_graph()
        result = _BatchStoreResult()
        for idx, disc_data in enumerate(discoveries):
            await _process_batch_discovery(
                disc_data,
                idx,
                graph,
                agent_id,
                arguments,
                result,
            )
        return _build_batch_store_response(arguments, discoveries, result)
    except Exception as error:
        return [
            error_response(
                f"Failed to store batch knowledge: {str(error)}"
            )
        ]


_NOTE_TOTAL_LEN = MAX_SUMMARY_LEN + MAX_DETAILS_LEN
_NOTE_INFRASTRUCTURE_TAGS = frozenset(
    {
        "infrastructure",
        "search",
        "embedding",
        "silent-failure",
        "degraded",
        "database",
        "service",
    }
)


class _NoteResponseError(Exception):
    """Abort a note write with an already-structured MCP error."""

    def __init__(self, response: TextContent):
        super().__init__()
        self.response = response


@dataclass(frozen=True)
class _KnowledgeNoteRequest:
    arguments: Dict[str, Any]
    agent_id: str
    text: Any
    is_anonymous_writer: bool


def _truncate_note_text(text: Any) -> Any:
    """Apply the legacy combined note limit before splitting fields."""
    if len(text) <= _NOTE_TOTAL_LEN:
        return text
    return text[:_NOTE_TOTAL_LEN] + "... [truncated]"


def _parse_note_response_to(
    arguments: Dict[str, Any],
) -> Optional[ResponseTo]:
    """Parse an optional typed parent link for a note."""
    response_data = arguments.get("response_to")
    if not response_data:
        return None
    required_fields = {"discovery_id", "response_type"}
    if not isinstance(response_data, dict) or not required_fields.issubset(
        response_data
    ):
        return None

    parent_id = str(response_data["discovery_id"]).strip()
    if not parent_id:
        raise _NoteResponseError(
            error_response("Invalid response_to.discovery_id (empty)")
        )

    response_type = response_data["response_type"]
    if response_type not in VALID_RESPONSE_TYPES:
        raise _NoteResponseError(
            error_response(
                f"Invalid response_type '{response_type}'. "
                f"Valid: {sorted(VALID_RESPONSE_TYPES)}"
            )
        )
    return ResponseTo(
        discovery_id=parent_id,
        response_type=response_type,
    )


def _split_note_text(text: Any) -> tuple[Any, Any]:
    """Split a note into summary and details at a nearby boundary."""
    if len(text) <= MAX_SUMMARY_LEN:
        return text, ""

    shortened = text[:MAX_SUMMARY_LEN]
    split_pos = MAX_SUMMARY_LEN
    for end_char in [". ", "! ", "? ", "\n"]:
        last_end = shortened.rfind(end_char, MAX_SUMMARY_LEN - 200)
        if last_end > 0:
            split_pos = last_end + len(end_char)
            break
    else:
        last_space = shortened.rfind(" ")
        if last_space > MAX_SUMMARY_LEN - 100:
            split_pos = last_space
    return text[:split_pos].rstrip(), text[split_pos:].strip()


def _infer_note_severity(tags: list[str]) -> str:
    """Preserve the legacy infrastructure-bug severity inference."""
    tag_set = set(tags)
    if "bug" in tag_set and tag_set & _NOTE_INFRASTRUCTURE_TAGS:
        return "medium"
    return "low"


def _capture_note_provenance(
    arguments: Dict[str, Any], agent_id: str
) -> dict[str, Any]:
    """Capture S22 note context without making provenance a write blocker."""
    from src.knowledge_graph import tag_provenance_source as _tag_src

    provenance = _tag_src(None, "explicit_leave_note")
    try:
        from src.provenance_context import (
            attach_s22_context,
            build_s22_write_context,
            classify_fork_for_s22_context,
        )

        meta = mcp_server.agent_metadata.get(agent_id)
        episode_fork_kind, identity_lineage_fork = (
            classify_fork_for_s22_context(meta, agent_id)
        )
        s22_context = build_s22_write_context(
            arguments,
            meta=meta,
            context_source="knowledge.note",
            default_governance_mode="explicit",
            episode_fork_kind=episode_fork_kind,
            identity_lineage_fork=identity_lineage_fork,
        )
        return attach_s22_context(provenance, s22_context)
    except Exception as exc:
        logger.debug("Could not capture note S22 provenance: %s", exc)
        return provenance


def _build_note_discovery(
    request: _KnowledgeNoteRequest,
) -> DiscoveryNode:
    """Normalize note fields and materialize the discovery node."""
    text = _truncate_note_text(request.text)
    response_to = _parse_note_response_to(request.arguments)
    tags = normalize_tags(request.arguments.get("tags", []))
    summary, details = _split_note_text(text)
    return DiscoveryNode(
        id=_utc_now_iso(),
        agent_id=request.agent_id,
        type="note",
        summary=summary,
        details=details,
        tags=tags,
        severity=_infer_note_severity(tags),
        status="open",
        response_to=response_to,
        provenance=_capture_note_provenance(
            request.arguments, request.agent_id
        ),
    )


async def _persist_note_discovery(
    graph: Any, note: DiscoveryNode, agent_id: str
) -> None:
    """Link, store, and broadcast one prepared note."""
    if note.tags:
        similar = await graph.find_similar(note, limit=3)
        note.related_to = [item.id for item in similar]
    await graph.add_discovery(note)
    await _broadcast_knowledge_write(note, agent_id)


def _build_note_response(
    request: _KnowledgeNoteRequest, note: DiscoveryNode
) -> Sequence[TextContent]:
    """Render the stable legacy and consolidated note response."""
    response = {
        "message": "Note saved",
        "note_id": note.id,
        "agent": _agent_display_for_response(
            request.agent_id, request.arguments
        ),
        "note": note.to_dict(include_details=False),
        "visibility": "shared",
        "discoverable": True,
        "_visibility_note": (
            "Notes are shared and searchable by other agents. "
            "Use response_to to reply to discoveries."
        ),
        "_resolve_when_done": (
            "When this is addressed, close the loop: "
            f"knowledge(action='update', discovery_id='{note.id}', "
            "status='resolved')"
        ),
    }
    if request.is_anonymous_writer:
        response["agent_mode"] = "anonymous"
        response["_identity_hint"] = (
            "Stored under a lightweight anonymous writer ID. "
            "Bind an identity first if you want authorship continuity."
        )
    return success_response(response, arguments=request.arguments)


async def _execute_note_write(
    request: _KnowledgeNoteRequest, graph: Any
) -> Sequence[TextContent]:
    """Build and persist one note through the shared write path."""
    note = _build_note_discovery(request)
    await _persist_note_discovery(graph, note, request.agent_id)
    return _build_note_response(request, note)


async def handle_knowledge_note(
    arguments: Dict[str, Any],
) -> Sequence[TextContent]:
    """Implement the preferred knowledge(action='note') write path."""
    arguments.setdefault("_tool_name", "knowledge")
    agent_id, error, is_anonymous_writer = _resolve_low_friction_writer(
        arguments
    )
    if error:
        return [error]

    from ..utils import check_agent_can_operate

    blocked = check_agent_can_operate(agent_id)
    if blocked:
        return [blocked]

    note_text, error = require_argument(
        arguments,
        "summary",
        "Note content required. Use 'summary', 'note', 'text', "
        "or 'content' parameter.",
    )
    if error:
        return [error]

    request = _KnowledgeNoteRequest(
        arguments=arguments,
        agent_id=agent_id,
        text=note_text,
        is_anonymous_writer=is_anonymous_writer,
    )
    try:
        graph = await get_knowledge_graph()
        return await _execute_note_write(request, graph)
    except _NoteResponseError as exc:
        return [exc.response]
    except Exception as exc:
        return [error_response(f"Failed to leave note: {str(exc)}")]


@mcp_tool(
    "leave_note",
    timeout=10.0,
    deprecated=True,
    superseded_by="knowledge",
)
async def handle_leave_note(
    arguments: Dict[str, Any],
) -> Sequence[TextContent]:
    """Adapt the deprecated tool to knowledge(action='note')."""
    adapted_arguments = apply_param_aliases("leave_note", arguments)
    adapted_arguments["_tool_name"] = "leave_note"
    return await handle_knowledge_note(adapted_arguments)


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

@mcp_tool("get_lifecycle_stats", timeout=30.0, register=False)
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
            # Also flip the old entry to superseded so it's flagged stale in
            # search — keep all three supersede paths consistent (store/update
            # both set status + edge; the edge alone doesn't change status).
            try:
                await graph.update_discovery(old_id, {
                    "status": "superseded",
                    "updated_at": _utc_now_iso(),
                })
            except Exception as exc:  # noqa: BLE001 — edge is the primary effect
                logger.warning(f"[KG] supersede status flip for {old_id[:8]} failed: {exc}")
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
    # The consolidated KnowledgeParams model includes omitted optional fields
    # as explicit ``None`` values.  ``dict.get(key, default)`` only applies the
    # default when the key is absent, so use None-aware fallbacks here.
    scope = arguments.get("scope") or "open"
    top_n_argument = arguments.get("top_n")
    top_n = int(10 if top_n_argument is None else top_n_argument)
    use_model = arguments.get("use_model") or False
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

    normalized_severity = str(severity).strip().lower()
    normalized_severity = SEVERITY_ALIASES.get(
        normalized_severity, normalized_severity
    )
    if normalized_severity not in VALID_SEVERITIES:
        raise ValueError(
            f"Invalid internal discovery severity '{severity}'. "
            f"Valid: {sorted(VALID_SEVERITIES)}"
        )

    graph = await get_knowledge_graph()
    discovery_id = _new_discovery_id()
    provenance = tag_provenance_source(extra_provenance, source)
    node = DiscoveryNode(
        id=discovery_id,
        agent_id=agent_id,
        type=discovery_type,
        summary=summary,
        details=details,
        tags=normalize_tags(tags or []),
        severity=normalized_severity,
        provenance=provenance,
    )
    await graph.add_discovery(node)
