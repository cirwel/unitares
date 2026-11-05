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

from dataclasses import dataclass, field, replace
from functools import wraps
from typing import Dict, Any, Sequence, Optional
from mcp.types import TextContent
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import asyncio
import hashlib
import json
import os
import re
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
from ..identity.stable_session import audit_reference, redact_provenance
from src.knowledge_graph import (
    get_knowledge_graph, DiscoveryNode, ResponseTo, normalize_tags,
    normalize_response_type,
    VALID_RESPONSE_TYPES, VALID_DISCOVERY_STATUSES,
    VALID_SEVERITIES as _SHARED_VALID_SEVERITIES,
    CLOSURE_CLASS_ADMITTING_STATUSES, CLOSURE_CLASS_CLEARING_STATUSES,
    closure_evidence_to_json,
)
from src.mcp_handlers.knowledge.imported_preview import annotate_imported_read
from src.mcp_handlers.knowledge.read_metadata import annotate_resolution_notes, add_resolution_notes_hint

from src.knowledge_authority import (
    CHANNEL_MESSAGE,
    GOVERNED_CLAIM,
    IMPORTED_CONTEXT,
    PROMOTION_SCHEMA,
    PROMOTION_TAG,
    assess_authority,
    channel_lanes,
    is_imported_context,
    is_lane_filter,
    rank_by_authority,
)
from src.mcp_handlers.knowledge.limits import (
    MAX_CLOSURE_EVIDENCE_BYTES, MAX_DETAILS_LEN, MAX_DISCOVERY_METADATA_LEN,
    MAX_SUMMARY_LEN, MAX_TAG_LEN, MAX_TAGS, MAX_UPDATED_DETAILS_LEN,
    MAX_UPDATED_SUMMARY_LEN, SUMMARY_TRUNCATION_MARKER,
)
from config.governance_config import config
from src.logging_utils import get_logger
from src.coherence_provenance import (
    BEHAVIORAL_UPDATE_CONSISTENCY_ROLE,
    coherence_role_for_source,
)
from src.perf_monitor import record_ms
from src.recall_telemetry import (
    LEXICAL_INDEX_MISS,
    LOW_CONFIDENCE,
    ZERO_RESULT,
    record_recall_event,
)
from ..support.coerce import LimitError, parse_limit
from ..support.llm_delegation import LOCAL_LLM_WAIT_BUFFER_S, synthesize_results
from ..support.tool_hints import KNOWLEDGE_SEARCH_TOOL

logger = get_logger(__name__)

from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
from src.broadcaster import broadcaster_instance


from .validation_helpers import (
    VALID_DISCOVERY_TYPES,
    VALID_SEVERITIES,
    SEVERITY_ALIASES,
    _normalize_discovery_type,
    _TOOLCALL_MARKUP_MARKERS,
    _detect_toolcall_markup_leak,
    _degenerate_write_response,
    _write_bound_refusal,
    _oversized_tags,
    _METADATA_PART_NAMES,
    _oversized_metadata,
    _invalid_enum_response,
    _coerce_pagination_int,
    _annotate_knowledge_confidence_authority,
    _AUDIT_QUERY_TEXT_MAX,
    _AUDIT_QUERY_REDACTIONS,
    _audit_safe_query_text,
    _audit_safe_tags,
    _discovery_not_found,
    _StoreResponseError,
    _KnowledgeStoreRequest,
    _KnowledgeStoreState,
    _InvalidResponseTo,
    _parse_response_to,
    _auto_link_requested,
    _link_similar_store_discoveries,
    _refuse_oversized_store_metadata,
    _authorize_store_discovery,
    _attach_store_response_hints,
    _attach_store_related_discoveries,
    _KnowledgeUpdateRequest,
    _UpdateResponseError,
    _refuse_oversized_summary_or_tags,
    _SEVERITY_RANK,
    _GATED_SEVERITIES,
    _effective_update_severity,
    _requested_non_owner_edits,
    _CLOSING_CALL_FIELDS,
    _non_owner_edit_refusal,
    _require_owned_binding,
    _authorize_high_severity_update,
    _apply_update_text_fields,
    _refuse_oversized_details,
    _apply_update_metadata_fields,
    CLOSURE_CLASSES,
    _CLOSING_STATUSES,
    _CLASS_ADMITTING_STATUSES,
    _CLOSURE_EVIDENCE_REQUIRED,
    CLOSURE_CLASS_PERSISTED,
    _closure_class_choices,
    _closure_evidence_keys_prose,
    _closure_evidence_examples,
    _CLOSURE_PREAMBLE,
    _UNOBSERVED_IS_HONEST,
    _closure_class_call,
    _unclassified_closure_note,
    _resolution_notes_call,
    _unstored_closure_class_note,
    _invalid_closure_param,
    _validate_closure_class,
    _closure_evidence_hint,
    _build_discovery_updates,
    _BatchItemError,
    _PreparedBatchDiscovery,
    _BatchStoreResult,
    _truncate_batch_summary,
    _truncate_batch_details,
    _parse_batch_response_to,
    _parse_batch_severity,
    _parse_batch_confidence,
    _NOTE_TOTAL_LEN,
    _NOTE_INFRASTRUCTURE_TAGS,
    _NoteResponseError,
    _KnowledgeNoteRequest,
    _truncate_note_text,
    _split_note_text,
    _infer_note_severity,
    _supersede_notes_block_details,
)
from .search_helpers import (
    _compact_identity_assurance,
    _compact_caller_identity_envelope,
    _LEAN_DISCOVERY_FIELDS,
    _one_line,
    _lean_search_payload,
    _compact_knowledge_read_result,
    _STALE_AFTER_DAYS,
    _is_durable_entry,
    _compute_staleness,
    _compute_staleness_warning,
    _SearchParameterError,
    _KnowledgeSearchRequest,
    _KnowledgeSearchState,
    _optional_flag,
    AUTO_DETAIL_MAX_RESULTS,
    _resolve_detail_inclusion,
    _parse_search_timestamp,
    RECENCY_HALF_LIFE_MAX_DAYS,
    _parse_half_life,
    _validate_search_backend,
    _fts_kwargs,
    _fts_page_size,
    _retrieve_fts_candidates,
    _retrieve_substring_candidates,
    _candidate_matches_search,
    _document_created_at,
    _within_window,
    _window_kwargs,
    _matches_tags,
    _tag_kwargs,
    _authority_ranking_enabled,
    AUTHORITY_POOL_SIZE,
    _authority_pool_size,
    _authority_score_map,
    _recency_weighted,
    _rank_search_documents,
    _candidate_status_visible,
    _filter_and_rerank_candidates,
    _run_newest_first_text_search,
    NEWEST_FIRST_SCAN_CEILING,
    _continue_newest_first_pages,
    _run_indexed_filter_search,
    _apply_semantic_fts_fallback,
    ARCHIVED_COLD_MIN_RESULTS,
    ARCHIVED_COLD_SCORE_PENALTY,
    ARCHIVED_COLD_TIER,
    KNOWLEDGE_SEARCH_TIMEOUT_S,
    ARCHIVED_COLD_RESPONSE_RESERVE_S,
    _ABANDONED_ARCHIVED_COLD_PASSES,
    SEARCH_SYNTHESIS_MAX_TIMEOUT_S,
    SEARCH_SYNTHESIS_RESERVE_S,
    SEARCH_SYNTHESIS_MIN_TIMEOUT_S,
    SUPERSESSION_LOOKUP_RESERVE_S,
    _archived_cold_widening,
    _settle_abandoned_pass,
    _bounded_supersession_lookup,
    _abandon_archived_cold_pass,
    _apply_archived_cold_tier,
    _candidate_matches_semantic_fallback,
    _omits_imported_lane,
    _label_excluded,
    _exclude_search_labels,
    _serialize_search_discoveries,
    _base_search_response,
    _search_order_echo,
    _attach_search_diagnostics,
    _empty_search_hints,
    _attach_empty_search_guidance,
    _attach_score_map,
    _verbatim_probe_terms,
    _contains_all_terms,
    _attach_search_scores_and_confidence,
    _attach_search_usage_hints,
    _attach_search_synthesis,
)





# The digest reuses the canonical _CLOSING_STATUSES (the closure_class
# contract, defined below) so the two cannot drift: a closing row that never
# declared a closure_class vouches for nothing in particular, and the digest
# says so instead of letting the bare status read as verified.









def _knowledge_read_response_mode(handler):
    """Make compact/lean opt-in for a read handler only.

    Write handlers are intentionally undecorated, so their attribution context
    stays full even when the unified schema carries ``response_mode``.
    """
    @wraps(handler)
    async def wrapped(arguments: Dict[str, Any]) -> Sequence[TextContent]:
        result = await handler(arguments)
        mode = str(arguments.get("response_mode") or "full").strip().lower()
        if mode not in {"compact", "lean"}:
            return result
        return _compact_knowledge_read_result(result, mode=mode)

    wrapped._lean_knowledge_read_response = True
    return wrapped

# Single-sourced in src.knowledge_graph next to the status/response_type
# vocabularies; tests/test_knowledge_enum_sync.py pins all three against the
# SQL CHECK constraints.




# Tool-call serialization markers that must never appear in a stored discovery.
# Their presence means the harness folded a later argument (commonly `tags`)
# into a text field as raw markup while the structured arg arrived empty —
# storing it yields a corrupt, unsearchable row (tags=[] + literal markup in
# details) with no warning. Legit prose effectively never contains these, so a
# hard reject is low-false-positive. (KG 2026-06-13 footgun: "knowledge
# store/update silently accepts degenerate writes".)










# How a metadata refusal names a part of the node metadata that a caller
# fills under another name. Every other part is named by its own key.










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


#: Upper bound on the query text stored in an audit row. Long enough for the
#: real distribution (mean term count is 0.1; a two-digit-term query is already
#: an outlier) and short enough that an accidental paste cannot bloat the table.

#: Credential shapes to blank before a query string reaches audit.events. Sibling
#: of `_redact_sensitive_report_text` in scripts/analysis/eisv_ablation_matrix.py;
#: kept local because src/ must not import from scripts/. An agent searching the
#: KG for "rotate the api_key=..." would otherwise persist the value verbatim.






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


async def _annotate_supersession(
    discovery_list: list, graph, *, deadline: Optional[float] = None,
) -> None:
    """Flag superseded rows in a result list so agents don't cite stale notes.

    Agent-facing trust signal (2026-06-21). Default search excludes archived/cold
    but NOT superseded, so a superseded discovery surfaces in results (only
    down-ranked). This marks those rows `superseded: True` from the already-loaded
    `status` (free) and best-effort attaches the replacement id(s) from the AGE
    SUPERSEDES edge. Mutates `discovery_list` in place; fail-soft (a graph error
    still leaves the free status-based flag).

    With a ``deadline`` (search's monotonic budget), the replacement-id lookup
    gets only what is left of it, less SUPERSESSION_LOOKUP_RESERVE_S, and is
    stopped the way an overrunning archived/cold pass is: the rows keep the free
    flag and lose only ``superseded_by``.
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
            if deadline is None:
                successors = await graph.get_superseded_by(superseded_ids)
            else:
                successors = await _bounded_supersession_lookup(graph, superseded_ids, deadline)
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
                # The server chose this name, so label_source reads "auto"
                # while the label still equals it (services/identity_payloads
                # .label_source_for). Recorded where the name is: in memory.
                meta.auto_label = auto_name
            except Exception as e:
                logger.debug(f"Could not save auto-generated display_name: {e}")

        # client_session_id names whose label to set: identity() resumes the
        # agent under that session key and relabels it. Without it, identity()
        # resolves the caller from transport signals alone, which on a route
        # that carries no session (plain REST) need not be this agent.
        warning = (
            f"KG entry attributed to '{auto_name}' (auto-generated). "
            "Set a personalized name with identity(client_session_id='...', "
            "name='YourName'), passing the client_session_id this agent's "
            "writes use."
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
    from src.services.identity_payloads import label_source_for

    payload["label_source"] = label_source_for(
        display_name,
        public_agent_id=public_agent_id,
        structured_id=structured_id,
        auto_label=_agent_metadata_text(meta, "auto_label"),
        has_public_handle=bool(public_handle),
    )
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

    ``identity_context`` is deliberately NOT mirrored here. It is the largest
    block in the envelope (~970 bytes, ~16% of a measured 6.1KB store response)
    and it describes the caller rather than this KG row, so copying it made
    every write serialize the same ontology twice — once under ``agent`` and
    again under ``agent_signature``. One attribution envelope is attribution;
    the second is repetition. The canonical copy stays on ``agent_signature``,
    the cross-tool contract ``success_response`` attaches and
    ``tool_usage_recorder`` reads. What the consistency requirement above
    actually needs is ``uuid`` and ``identity_assurance`` agreeing between the
    two blocks, and both still do.
    """
    raw_display = arguments.get("_agent_display")
    if isinstance(raw_display, dict):
        agent_display = dict(raw_display)
    else:
        agent_display = _resolve_agent_display(agent_id)

    # Drop inherited context at the construction site, not after enrichment.
    # Both paths below return ``agent_display`` early — a signature that raises,
    # and one with no uuid — so a pop placed after the enrichment loop would
    # leave a caller-seeded ``_agent_display`` emitting identity_context in
    # precisely the unproven and degraded cases (review on #2192 at d0496b9).
    agent_display.pop("identity_context", None)

    try:
        from ..support import agent_auth as _auth

        signature = _auth.compute_agent_signature(
            arguments=_anonymous_writer_signature_arguments(arguments)
        )
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
        "identity_assurance",
    ):
        if key in signature:
            agent_display[key] = signature[key]

    return agent_display


def _anonymous_writer_signature_arguments(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """The arguments a response's signature is computed from.

    An anonymous write records ``anonkg_*`` in ``agent_id``, but the session
    may still carry a server-inferred binding to some agent, and
    ``compute_agent_signature`` reads that binding whenever the arguments name
    an identity. A response for an anonymous write must not then name the
    inferred agent as its author, so the identity keys are dropped from a
    copy: with none, an inferred binding signs as no one, and an unbound
    session already did. Other arguments pass through unchanged.
    """
    if not str(arguments.get("agent_id") or "").startswith(_ANONYMOUS_WRITER_PREFIX):
        return arguments
    unsigned = dict(arguments)
    unsigned.pop("agent_id", None)
    unsigned.pop("_agent_uuid", None)
    return unsigned


_ANONYMOUS_WRITER_KEY_ENV = "UNITARES_CONTINUITY_TOKEN_SECRET"
_ANONYMOUS_WRITER_FALLBACK_KEY = secrets.token_bytes(32)
_ANONYMOUS_WRITER_PREFIX = "anonkg_"
# The pseudonym of a session with no identifying signal; every such caller
# shares it. A derived pseudonym ends in a hex digest instead.
_ANONYMOUS_WRITER_SHARED_SUFFIX = "_local"


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
        return f"{_ANONYMOUS_WRITER_PREFIX}{client_hint}_{digest}"
    return f"{_ANONYMOUS_WRITER_PREFIX}{client_hint}{_ANONYMOUS_WRITER_SHARED_SUFFIX}"


async def _inferred_writer_exempt() -> Optional[bool]:
    """Whether this call's server-inferred binding may keep its attribution.

    None when the binding is not server-inferred (nothing to decide). Otherwise
    the strict check-in gate's own predicate, asked of the RESOLVED agent (the
    session's binding, never an agent_id the call passes), so a colliding
    sibling cannot borrow a resident's exemption. Fails closed.
    """
    from ..context import get_context_agent_id, get_session_proof_origin
    from ..identity_bootstrap import is_exempt_from_caller_proof

    if get_session_proof_origin() != "server_inferred":
        return None
    return await is_exempt_from_caller_proof(get_context_agent_id())


def _inferred_binding_untrusted(inferred_exempt: Optional[bool]) -> bool:
    """True when the binding is server-inferred and its agent is not exempt.

    ``inferred_exempt`` is what ``_inferred_writer_exempt`` returned for this
    call. A caller that did not ask passes None, which on a server-inferred
    binding counts as not exempt: the predicate failing closed.
    """
    from ..context import get_session_proof_origin

    return get_session_proof_origin() == "server_inferred" and not inferred_exempt


def _inferred_binding_refusal(
    *, update: bool = False, action: Optional[str] = None
) -> TextContent:
    """The strict gate's refusal, for a gated write on an inferred binding.

    ``update`` names the update tool in the retry routes, so a refused update
    is not sent to the store tool (which would mint a second finding).
    ``action`` names any other knowledge action, so its retry repeats it.
    """
    from ..context import get_session_proof_origin, get_session_resolution_source
    from ..identity_bootstrap import inferred_binding_refusal_payload

    if action:
        retry_call, retry_label = "knowledge", f"knowledge(action='{action}')"
    elif update:
        retry_call = "update_finding"
        retry_label = "update_finding / knowledge(action='update')"
    else:
        retry_call = "store_finding"
        retry_label = "store_finding / knowledge(action='store')"
    payload = inferred_binding_refusal_payload(
        "knowledge",
        retry_call=retry_call,
        retry_label=retry_label,
        identity_assurance=None,
        read_only_option={
            "action": "stay_read_only",
            "call": "search_shared_memory(query=<what you need>)",
            "when": (
                "You want to read shared memory without writing. "
                "Knowledge reads need no identity."
            ),
        },
        surface_context={
            "transport_surface": "knowledge",
            "session_resolution_source": get_session_resolution_source(),
            "proof_origin": get_session_proof_origin(),
            "lifecycle_automation": "not_confirmed",
        },
    )
    if action:
        # knowledge has no default action, so a retry route without one fails
        # with "action parameter required": write it into every route.
        for option in payload.get("safe_options", []):
            call = option.get("call")
            if isinstance(call, str):
                option["call"] = call.replace(
                    "knowledge(...", f"knowledge(action='{action}', ..."
                )
    return success_response(payload)[0]


def _unproven_agent_id_refusal(
    arguments: Dict[str, Any], *, administrative: bool = False
) -> Optional[TextContent]:
    """Refuse a write naming an agent_id this call has not proven, under strict identity.

    An explicit ``agent_id`` with no resolved binding is the caller's word
    alone. The REST gate lets a non-UUID one through as a legacy name
    reference, and below high severity nothing then checks it, so every
    invented name wrote as its own author with its own store budget, and an
    update could edit or close another agent's finding. Bound callers are
    unaffected: dispatch injects their own id, and ``inject_identity`` already
    refuses a different one. The anonymous pseudonym this module derives is
    let through when it is written back into the same arguments.

    ``administrative`` is for handlers with no anonymous writer: any call
    without a resolved binding is refused, with or without an agent_id. A
    missing agent_id is no evidence that the gate saw none (REST dispatch
    unwraps ``kwargs`` after the gate, which can null the name the gate
    passed), and a pseudonym is an anonymous writer's label, not an identity.
    """
    from ..context import (
        get_context_resolved_agent_id,
        get_session_proof_origin,
        get_session_resolution_source,
    )
    from ..identity_bootstrap import (
        is_strict_identity_required,
        strict_identity_refusal_payload,
    )

    if not is_strict_identity_required():
        return None
    claimed = arguments.get("agent_id")
    if get_context_resolved_agent_id() or (not claimed and not administrative):
        return None
    if (
        not administrative
        and str(claimed).startswith(_ANONYMOUS_WRITER_PREFIX)
    ):
        rest = {k: v for k, v in arguments.items() if k != "agent_id"}
        if claimed == _derive_anonymous_writer_id(rest):
            return None
    return success_response(
        strict_identity_refusal_payload(
            "knowledge",
            hint=(
                "agent_id names an identity this call has not proven. Under strict "
                "identity a knowledge write needs a binding: leave agent_id out and "
                "pass the client_session_id that start_session returned."
                if claimed
                else "This knowledge action needs a bound caller under strict "
                "identity: pass the client_session_id that start_session returned."
            ),
            next_step=(
                "Retry without agent_id and with client_session_id=<from "
                "start_session>. If this process never called start_session, call "
                "start_session(force_new=true) first."
            ),
            surface_context={
                "transport_surface": "knowledge",
                "reason": "unproven_agent_id" if claimed else "unbound",
                "session_resolution_source": get_session_resolution_source(),
                "proof_origin": get_session_proof_origin(),
                "lifecycle_automation": "not_confirmed",
            },
        )
    )[0]


def _requires_proven_agent_id(handler):
    """Apply ``_unproven_agent_id_refusal`` before an identity-gated knowledge handler.

    The writer resolver already runs the check, but supersede (without notes),
    cleanup, synthesize, promote and audit reach their writes without it, each
    registered both as a router action and as a standalone tool. Gating every
    such handler at its entry keeps one rule for all of them.
    """

    @wraps(handler)
    async def guarded(arguments: Dict[str, Any], *args, **kwargs):
        refusal = _unproven_agent_id_refusal(arguments)
        if refusal is not None:
            return [refusal]
        return await handler(arguments, *args, **kwargs)

    return guarded


def _resolve_low_friction_writer(
    arguments: Dict[str, Any], *, inferred_exempt: Optional[bool] = None
) -> tuple[str, Optional[TextContent], bool]:
    """Resolve agent_id for low/medium knowledge writes.

    If the caller has no explicit or bound identity, use a stable anonymous writer
    ID instead of creating a new auto_* identity for each quick write.

    A server-inferred binding (fingerprint pin, transport-injected
    client_session_id, context fallback) also gets the anonymous writer,
    unless its resolved agent is exempt from caller proof (``inferred_exempt``,
    see ``_inferred_writer_exempt``). Every client behind one address with one
    User-Agent shares a fingerprint, so attributing the write to the inferred
    agent credits it to whichever agent last matched, not to the caller.
    Dispatch has already injected that agent into ``agent_id``, so the proof
    origin decides here, as it does for ``_resolve_reader_agent_id`` and the
    strict write gate. A substrate-earned or dedicated-substrate resident
    resolves by fingerprint by design and keeps its attribution, as the strict
    check-in gate lets it through.
    """
    from ..context import get_context_agent_id

    refusal = _unproven_agent_id_refusal(arguments)
    if refusal is not None:
        return "", refusal, False

    if _inferred_binding_untrusted(inferred_exempt):
        arguments.pop("agent_id", None)
        agent_id = _derive_anonymous_writer_id(arguments)
        arguments["agent_id"] = agent_id
        return agent_id, None, True

    if arguments.get("agent_id") or get_context_agent_id():
        agent_id, error = require_agent_id(arguments)
        return agent_id, error, False

    agent_id = _derive_anonymous_writer_id(arguments)
    arguments["agent_id"] = agent_id
    return agent_id, None, True








def _store_needs_registered_writer(arguments: Dict[str, Any]) -> bool:
    """A high or critical store must come from a registered agent."""
    return str(arguments.get("severity", "low")).lower() in {"high", "critical"}


def _gated_write_refused_on_inferred_binding(
    inferred_exempt: Optional[bool], *, update: bool = False, action: Optional[str] = None
) -> Optional[TextContent]:
    """The refusal for a high or critical write on an untrusted inferred binding.

    Low and medium writes fall back to the anonymous writer; a gated write
    must come from a registered agent, and the inferred one is not proven to
    be the caller, so under strict identity it is refused the way the check-in
    gate refuses, pointing the caller at its own client_session_id. With
    strict identity off the registered-agent path runs unchanged.
    """
    from ..identity_bootstrap import is_strict_identity_required

    if _inferred_binding_untrusted(inferred_exempt) and is_strict_identity_required():
        return _inferred_binding_refusal(update=update, action=action)
    return None


def _requires_proven_writer(action: str):
    """``_requires_proven_agent_id`` plus the inferred-binding refusal.

    For identity-gated knowledge handlers that reach their writes without the
    writer resolver (supersede without notes, cleanup, synthesize, promote,
    audit). A server-inferred binding (same address and User-Agent as some
    agent) is not proof that the caller is that agent, so under strict
    identity it is refused the way a high or critical write is, unless the
    resolved agent is exempt from caller proof (a substrate resident). These
    handlers have no anonymous writer, so under strict identity they need a
    resolved binding: a missing agent_id or the anonymous pseudonym is refused
    like any other unproven agent_id. ``action`` is the knowledge action the
    handler serves, which the refusal's retry repeats.
    """

    def decorate(handler):
        @wraps(handler)
        async def guarded(arguments: Dict[str, Any], *args, **kwargs):
            refusal = _unproven_agent_id_refusal(arguments, administrative=True)
            if refusal is None:
                refusal = _gated_write_refused_on_inferred_binding(
                    await _inferred_writer_exempt(), action=action
                )
            if refusal is not None:
                return [refusal]
            return await handler(arguments, *args, **kwargs)

        return guarded

    return decorate


async def _resolve_store_writer(
    arguments: Dict[str, Any],
) -> tuple[str, Optional[TextContent], Optional[str], bool]:
    """Resolve the writer using the severity-dependent identity policy."""
    inferred_exempt = await _inferred_writer_exempt()
    if not _store_needs_registered_writer(arguments):
        agent_id, error, is_anonymous = _resolve_low_friction_writer(
            arguments, inferred_exempt=inferred_exempt
        )
        return agent_id, error, None, is_anonymous

    refusal = _gated_write_refused_on_inferred_binding(inferred_exempt)
    if refusal is not None:
        return "", refusal, None, False
    agent_id, error = require_registered_agent(arguments)
    if error:
        return agent_id, error, None, False
    display_name_error, display_name_warning = _check_display_name_required(agent_id, arguments)
    return agent_id, display_name_error, display_name_warning, False


@dataclass(frozen=True)
class KnowledgeWriteAuthor:
    """The agent_id a knowledge store or note records, and who else can use it.

    kind says whether only this caller writes under agent_id:

    - "bound": the identity bound to this session. Calls bound to it write
      under it; so can a call that reaches the handler unbound and passes it
      as agent_id on a low or medium write, which no ownership check stops
      (only high and critical stores verify ownership).
    - "anonymous": the low-friction pseudonym derived from this session's
      signals. An anonymous caller whose session yields the same signals
      (the last fallback is an IP and user-agent fingerprint) writes under
      it too.
    - "anonymous_shared": the pseudonym for a session with no identifying
      signal (``anonkg_<client>_local``), which every such caller shares.
    - "named": an agent_id the call passed that is not this session's
      binding. Any caller can pass it.
    """

    agent_id: str
    kind: str


def resolve_knowledge_write_author(
    arguments: Dict[str, Any], *, store: bool
) -> Optional[KnowledgeWriteAuthor]:
    """The author a knowledge store (``store``) or note records for this call.

    For the recovery of a timed-out write: it runs the writer resolution the
    handler runs (the store's severity-dependent policy, or the note's
    low-friction one) on a copy, so the caller's arguments stay as sent. The
    display-name check the store also runs is skipped; it can relabel an
    agent but never changes the author. None when the handler would refuse
    the writer or the resolution fails, which it logs.
    """
    from ..context import get_context_agent_id, get_session_proof_origin

    if get_session_proof_origin() == "server_inferred":
        # On a server-inferred binding the handler's writer turns on an async
        # exemption check of the resolved agent (_inferred_writer_exempt) that
        # this synchronous recovery cannot repeat, so it does not guess.
        return None
    candidate = dict(arguments)
    try:
        if store and _store_needs_registered_writer(candidate):
            agent_id, error = require_registered_agent(candidate)
        else:
            agent_id, error, _ = _resolve_low_friction_writer(candidate)
        bound = get_context_agent_id()
    except Exception:
        logger.warning(
            "Could not resolve the author of a timed-out knowledge write",
            exc_info=True,
        )
        return None
    if error is not None or not agent_id:
        return None
    agent_id = str(agent_id)
    if bound and agent_id == bound:
        kind = "bound"
    elif agent_id.startswith(_ANONYMOUS_WRITER_PREFIX):
        kind = (
            "anonymous_shared"
            if agent_id.endswith(_ANONYMOUS_WRITER_SHARED_SUFFIX)
            else "anonymous"
        )
    else:
        kind = "named"
    return KnowledgeWriteAuthor(agent_id=agent_id, kind=kind)


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

    oversized_tags = _oversized_tags(arguments.get("tags"), "Nothing was stored.")
    if oversized_tags:
        raise _StoreResponseError(_write_bound_refusal(*oversized_tags))

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
        state.summary = truncated.rstrip() + SUMMARY_TRUNCATION_MARKER

    if len(raw_details) > MAX_DETAILS_LEN:
        state.truncation_info["details"] = f"Truncated from {len(raw_details)} to {MAX_DETAILS_LEN} chars"
        raw_details = raw_details[:MAX_DETAILS_LEN] + "... [truncated]"
    state.details = raw_details






def _parse_store_response_to(arguments: Dict[str, Any]) -> Optional[ResponseTo]:
    """Parse the optional typed link to a parent discovery."""
    try:
        return _parse_response_to(arguments.get("response_to"))
    except _InvalidResponseTo as exc:
        raise _StoreResponseError(error_response(str(exc))) from None


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
                monitor = mcp_server.monitors[agent_id]
                state = monitor.state
                monitor_metrics = {}
                try:
                    candidate_metrics = monitor.get_metrics()
                    if isinstance(candidate_metrics, dict):
                        monitor_metrics = candidate_metrics
                except (AttributeError, TypeError, ValueError):
                    pass
                coherence_source = str(
                    monitor_metrics.get("coherence_source") or "unknown"
                )
                coherence_role = str(
                    monitor_metrics.get("coherence_role")
                    or coherence_role_for_source(coherence_source)
                )
                monitor_state = {
                    "regime": state.regime,
                    "coherence": round(state.coherence, 6),
                    "coherence_source": coherence_source,
                    "coherence_role": coherence_role,
                    "coherence_health_evidence": (
                        coherence_role == BEHAVIORAL_UPDATE_CONSISTENCY_ROLE
                    ),
                    "coherence_knowledge_confidence_evidence": False,
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
                # Search results return this to any reader; a keyed session id
                # is a bearer credential, so store its audit reference.
                provenance["writer_session_id_at_write"] = audit_reference(writer_session)

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
    # Tag, not policy. `get_lifecycle_policy` also returns "permanent" for
    # permanent TYPES, and gating supersession on that would refuse the normal
    # way an architectural decision is retired — replacing it with its next
    # revision. Retention and replaceability are different questions; see
    # KnowledgeGraphLifecycle.is_permanent_by_tag.
    if lifecycle.is_permanent_by_tag(state.supersedes_target):
        target = state.supersedes_target
        raise _StoreResponseError(
            error_response(
                f"Cannot supersede permanent discovery '{supersedes_id}' "
                f"(type={target.type}, tags={target.tags}). "
                "Use knowledge(action='update') with explicit operator action "
                "to override."
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
    return success_response(
        response, arguments=_anonymous_writer_signature_arguments(request.arguments)
    )


async def _execute_single_store(request: _KnowledgeStoreRequest, graph: Any) -> Sequence[TextContent]:
    state = _KnowledgeStoreState(
        request=request,
        graph=graph,
        summary=request.summary,
    )
    _truncate_store_content(state)
    await _build_store_discovery(state)
    await _prepare_store_supersession(state)
    _annotate_knowledge_confidence_authority(state.discovery)
    await _link_similar_store_discoveries(state)
    _authorize_store_discovery(state)
    _refuse_oversized_store_metadata(state)
    await _persist_store_discovery(state)
    return _build_store_response(state)


@mcp_tool("store_knowledge_graph", timeout=20.0, register=False)
@_requires_proven_agent_id
async def handle_store_knowledge_graph(
    arguments: Dict[str, Any],
) -> Sequence[TextContent]:
    """Store one discovery or delegate a discovery batch."""
    arguments = apply_param_aliases("store_knowledge_graph", arguments)
    agent_id, error, display_name_warning, is_anonymous_writer = await _resolve_store_writer(arguments)
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





















def _parse_knowledge_search_request(
    arguments: Dict[str, Any],
) -> _KnowledgeSearchRequest:
    search_mode = str(arguments.get("search_mode") or "auto").lower()
    if search_mode not in {"auto", "fts", "semantic", "hybrid"}:
        raise _SearchParameterError(
            f"Invalid search_mode {search_mode!r}; expected one of: auto, fts, semantic, hybrid"
        )

    sort_by = str(arguments.get("sort_by") or "relevance").lower()
    if sort_by not in {"relevance", "created_at"}:
        raise _SearchParameterError(
            f"Invalid sort_by {sort_by!r}; expected 'relevance' or 'created_at'"
        )
    if sort_by == "created_at" and _optional_flag(arguments.get("semantic")) is True:
        raise _SearchParameterError(
            "sort_by='created_at' orders the query's full-text matches by time "
            "and cannot be combined with semantic=true. Drop semantic, or use "
            "sort_by='relevance'."
        )
    if sort_by == "created_at" and search_mode in {"semantic", "hybrid"}:
        # Newest-first needs a match SET to order. The full-text query gives
        # one; similarity has no boundary short of the min_similarity knob, so
        # "newest semantic match" would be "newest row above an arbitrary
        # cutoff" — mostly unrelated rows on this corpus.
        raise _SearchParameterError(
            f"sort_by='created_at' orders the query's full-text matches by time "
            f"and cannot be combined with search_mode={search_mode!r}. Use "
            "search_mode='auto' or 'fts', or omit the query to list the newest "
            "entries by filter."
        )
    recency_half_life_days = _parse_half_life(arguments.get("recency_half_life_days"))
    if recency_half_life_days is not None:
        if sort_by == "created_at":
            raise _SearchParameterError(
                "recency_half_life_days weights relevance by age; sort_by='created_at' "
                "already orders by time. Use one or the other."
            )
        if not (arguments.get("query") or arguments.get("text")):
            raise _SearchParameterError(
                "recency_half_life_days needs a query: without one, results are "
                "already newest first."
            )
    created_after = _parse_search_timestamp("created_after", arguments.get("created_after"))
    created_before = _parse_search_timestamp("created_before", arguments.get("created_before"))
    query_present = bool(arguments.get("query") or arguments.get("text"))
    if not query_present and (created_after or created_before) and not arguments.get("sort_by"):
        # A queryless read with a date window is "what is new since T": the
        # listing is already newest first, and the authority nudge that a
        # default listing gets would move an imported row out of time order.
        sort_by = "created_at"
    if created_after and created_before and created_after >= created_before:
        raise _SearchParameterError(
            f"created_after ({created_after.isoformat()}) must be earlier than "
            f"created_before ({created_before.isoformat()})."
        )

    authority_mode = str(arguments.get("authority_mode") or "prefer_governed").lower()
    if authority_mode not in {"prefer_governed", "all"}:
        raise _SearchParameterError(
            f"Invalid authority_mode {authority_mode!r}; expected 'prefer_governed' or 'all'"
        )

    query_effective = arguments.get("query") or arguments.get("text")
    if isinstance(query_effective, str) and query_effective and not query_effective.strip():
        # A supplied-but-blank query is a caller mistake, and silence about it
        # is worse than an error. `"   "` is truthy, so it reached the semantic
        # path, embedded whitespace, and returned nearest neighbours at ~0.34
        # similarity — arbitrary rows reported as success=true. Meanwhile
        # `"   ".split()` is `[]`, so every term-based path downstream already
        # saw "no terms": the two halves of the request disagreed.
        #
        # Only a *supplied* blank raises. An absent query is a real, supported
        # shape (filter by tags / type / severity alone) and keeps its path.
        raise _SearchParameterError(
            "Query is blank. Pass a non-empty query, or omit `query` entirely "
            "to filter by tags/discovery_type/severity alone."
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

    # Same defect class dialectic fixed in #1733: the old falsy `or` turned
    # limit=0 into the default, and a negative limit reached PostgreSQL as a
    # malformed LIMIT, surfacing as a generic failure — neither answered the
    # question the caller asked. The 100 ceiling is NEW behavior (the old
    # code had no upper bound; only the _more_available hint claimed one), so
    # a clamp is disclosed via limit_clamped_from rather than applied silently.
    try:
        limit = parse_limit(
            arguments.get("limit"),
            default=config.KNOWLEDGE_QUERY_DEFAULT_LIMIT,
            maximum=100,
        )
    except LimitError as exc:
        raise _SearchParameterError(str(exc)) from None
    try:
        requested_limit = int(arguments.get("limit"))
    except (TypeError, ValueError):
        requested_limit = None
    limit_clamped_from = (
        requested_limit
        if requested_limit is not None and requested_limit > limit
        else None
    )

    raw_tags = arguments.get("tags", [])
    tags = normalize_tags(raw_tags) or None
    tags_supplied = bool(raw_tags) and not (isinstance(raw_tags, str) and raw_tags.strip() in ("", "[]"))
    if tags_supplied and not tags:
        # A filter the caller wrote must not silently become no filter.
        raise _SearchParameterError(
            f"tags {raw_tags!r} normalize to nothing; pass at least one tag containing a letter or digit."
        )

    explicit_author_filter = arguments.get("agent_id_filter")
    if isinstance(explicit_author_filter, str):
        explicit_author_filter = explicit_author_filter.strip()
        if not explicit_author_filter:
            raise _SearchParameterError(
                "agent_id_filter must be a non-blank author agent UUID"
            )
    author_filter = (
        explicit_author_filter
        if explicit_author_filter is not None
        else arguments.get("agent_id")
    )

    return _KnowledgeSearchRequest(
        arguments=arguments,
        limit=limit,
        limit_clamped_from=limit_clamped_from,
        include_details=_optional_flag(arguments.get("include_details")),
        include_provenance=arguments.get("include_provenance", False),
        synthesize=arguments.get("synthesize", False),
        query_text=arguments.get("query") or arguments.get("text"),
        # The explicit author filter is preferred; legacy agent_id is a
        # fallback only when that filter was omitted.
        agent_id=author_filter,
        search_mode_requested=search_mode,
        operator_forced=operator_forced,
        exclude_labels=exclude_labels,
        tags=tags,
        discovery_type=arguments.get("discovery_type"),
        severity=arguments.get("severity"),
        status=status,
        # Parsed, not read raw: the string "false" is truthy, and used to
        # switch the status filter it names off.
        include_archived=_optional_flag(arguments.get("include_archived")) is True,
        include_cold=_optional_flag(arguments.get("include_cold")) is True,
        include_archived_explicit=_optional_flag(arguments.get("include_archived")),
        include_cold_explicit=_optional_flag(arguments.get("include_cold")),
        authority_mode=authority_mode,
        sort_by=sort_by,
        created_after=created_after,
        created_before=created_before,
        recency_half_life_days=recency_half_life_days,
    )




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
    tag_kwargs = _tag_kwargs(request)
    semantic_raw, fts_raw = await _asyncio.gather(
        state.graph.semantic_search(
            str(request.query_text),
            limit=fetch_limit,
            min_similarity=state.min_similarity,
            # RRF needs semantic relevance, not AGE's implicit graph/age/status
            # blend. A resolved incident can still be the best answer. Apply
            # visibility, explicit recency and authority in their handler stages.
            connectivity_weight=0.0,
            temporal_decay=False,
            status_weight=False,
            **tag_kwargs,
            **_window_kwargs(request),
        ),
        state.graph.full_text_search(
            str(request.query_text),
            limit=fetch_limit,
            operator=fts_operator,
            **tag_kwargs,
            **_window_kwargs(request),
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
        # Match the semantic leg of hybrid search; recency is opt-in below.
        connectivity_weight=0.0,
        temporal_decay=False,
        status_weight=False,
        **_tag_kwargs(request),
        **_window_kwargs(request),
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
























# Candidates authority ranking sees before the page is cut. Retrieval
# windows widen to at least this when it is on, or a native finding just
# below a page of down-ranked rows is never retrieved to be lifted. The
# queryless listing is exempt: it is a newest-first read, and widening it
# would pull older rows above newer ones rather than break close contests.














async def _run_text_search(state: _KnowledgeSearchState) -> None:
    from src.reranker import reranker_enabled
    from src.retrieval import (
        apply_tag_boost,
        expand_with_neighbors,
        related_expansion_enabled,
        hybrid_enabled,
        rrf_fuse,
    )

    request = state.request
    state.rerank_on = reranker_enabled()
    state.rerank_pool_size = 50 if state.rerank_on else 0
    state.first_stage_limit = max(
        request.limit * 2, state.rerank_pool_size, _authority_pool_size(request)
    )
    state.hybrid_on = hybrid_enabled()
    state.graph_expand_on = related_expansion_enabled()

    has_semantic, has_fts = _validate_search_backend(state)
    if request.sort_by == "created_at":
        await _run_newest_first_text_search(state, has_fts=has_fts)
        return
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




# Rows newest-first continuation may read in total, the same ceiling as one
# full-text candidate page. Pages double, so a long run of excluded writers
# is crossed in a few queries rather than cut off after a fixed page count.








# A query search that finds fewer active rows than this runs a second pass
# over archived and cold rows (KG decision 2026-09-29T04:11:58.480600+00:00,
# proposal 1). About two thirds of the graph is archived or cold, and lifecycle
# aging moves a row there by age alone, so a default search could miss the only
# entry that answers the question.
# Second-pass rows sit after every active row; their reported scores are
# multiplied by this so the numbers agree with the order.
# search_knowledge_graph's handler timeout. The second pass is optional, so it
# gets only what the first pass left of this budget, less this reserve for
# building the response. Without the bound, a slow second pass ran into the
# handler timeout and the caller lost the first page it had already earned.
# Second passes the handler stopped waiting for, kept referenced until they
# finish unwinding so the loop does not collect them mid-cancel.
# Search synthesis (synthesize=true) is bounded by the same budget: its LLM
# request timeout is what is left, less this reserve and call_local_llm's
# wait buffer, capped at the old fixed 30s; under the minimum it is skipped.
# The replacement-id lookup for superseded rows runs last, after the optional
# second pass may have spent the budget down to its reserve, so it gets what is
# left less this reserve for building the response.




async def _run_archived_cold_pass(second: _KnowledgeSearchState, limit: int) -> None:
    await _run_text_search(second)
    await _apply_semantic_fts_fallback(second)
    if second.fallback_used:
        second.results = _rank_search_documents(second, second.results)[:limit]










































async def _execute_knowledge_search(state: _KnowledgeSearchState) -> dict[str, Any]:
    started_at = time.perf_counter()
    if state.request.query_text:
        await _run_text_search(state)
    else:
        await _run_indexed_filter_search(state)
        state.results = _rank_search_documents(state, state.results)[: state.request.limit]
    await _apply_semantic_fts_fallback(state)
    if state.fallback_used:
        state.results = _rank_search_documents(state, state.results)[: state.request.limit]
    await _apply_archived_cold_tier(state, first_pass_s=time.perf_counter() - started_at)
    record_ms(
        f"knowledge.search.{state.search_mode}",
        (time.perf_counter() - started_at) * 1000.0,
    )

    _exclude_search_labels(state)
    auto_details, include_details = _resolve_detail_inclusion(
        state.request.include_details, len(state.results)
    )
    discoveries = _serialize_search_discoveries(
        state,
        include_details=include_details,
    )
    await _annotate_supersession(discoveries, state.graph, deadline=state.deadline)

    response = _base_search_response(
        state,
        discoveries,
        auto_details=auto_details,
        include_details=include_details,
    )
    _attach_search_diagnostics(response, state)
    _attach_empty_search_guidance(response, state)
    _attach_search_usage_hints(
        response,
        state,
        auto_details=auto_details,
        include_details=include_details,
    )
    _attach_search_scores_and_confidence(response, state)
    await _attach_search_synthesis(response, state, discoveries)

    writers = list({document.agent_id for document in state.results if document.agent_id})[:10]
    # Sent in the background: the response does not depend on it, and a
    # stalled dashboard WebSocket can hold a broadcast for 2s per send plus
    # 2s per close, which no response reserve should have to cover. The
    # payload (reader identity included) is built here, before returning.
    from src.background_tasks import create_tracked_task

    create_tracked_task(_broadcast_knowledge_read(
        "search",
        _resolve_reader_agent_id(state.request.arguments),
        payload={
            "result_count": len(state.results),
            "query_present": bool(state.request.query_text),
            "query_term_count": state.request.query_term_count,
            # The terms themselves, redacted and bounded. Without these the
            # counts above can say a search happened but never whether the right
            # entry was reachable for what was asked.
            "query_text": _audit_safe_query_text(state.request.query_text),
            "search_mode": state.search_mode or state.request.search_mode_requested,
            # Tag filter separates the two very different callers that both
            # land here: an open semantic recall, versus a tag-scoped poll of a
            # known lane (Vigil reads tags=["sentinel"] with semantic=False on a
            # 30-minute cron, and is the majority of queried searches). Judging
            # retrieval without splitting those measures the wrong population.
            "filter_tags": _audit_safe_tags(state.request.arguments.get("tags")),
            "writer_agent_ids": writers,
            "filter_agent_id": state.request.agent_id,
        },
    ), name="knowledge_read_broadcast")
    return response


@mcp_tool("search_knowledge_graph", timeout=KNOWLEDGE_SEARCH_TIMEOUT_S, requires_identity="pre_onboard")
@_knowledge_read_response_mode
async def handle_search_knowledge_graph(
    arguments: Dict[str, Any],
) -> Sequence[TextContent]:
    """Search the KG through the explicit retrieval and response pipeline."""
    # The decorator's timeout started a moment earlier; the second pass's
    # response reserve covers the difference.
    deadline = time.monotonic() + KNOWLEDGE_SEARCH_TIMEOUT_S
    arguments = apply_param_aliases("search_knowledge_graph", arguments)
    try:
        graph = await get_knowledge_graph()
        request = _parse_knowledge_search_request(arguments)
        response = await _execute_knowledge_search(
            _KnowledgeSearchState(request=request, graph=graph, deadline=deadline)
        )
        return success_response(response, arguments=arguments)
    except _SearchParameterError as exc:
        return [error_response(str(exc))]
    except Exception as exc:
        return [error_response(f"Failed to search knowledge: {str(exc)}")]


@mcp_tool("get_knowledge_graph", timeout=15.0, register=False)
@_knowledge_read_response_mode
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
                d_dict["session_id_at_write"] = audit_reference(session_at_write)
            if include_details and full_dict.get("details"):
                d_dict["details"] = full_dict.get("details")
            d_dict["_agent_id"] = d.agent_id
            annotate_resolution_notes(d_dict, d)
            annotate_imported_read(d_dict, d)
            discovery_list.append(d_dict)

        # Agent-facing trust flag (same as search): mark superseded rows.
        await _annotate_supersession(discovery_list, graph)

        response_data = {
            "agent": agent_display,
            "discoveries": discovery_list,
            "count": len(discoveries)
        }
        add_resolution_notes_hint(response_data)

        # Visibility hints (v2.5.0+)
        if not include_details and len(discoveries) > 0:
            response_data["_tip"] = "Add include_details=true for inline details (imports capped at 2 KiB); use action=details for full pages"
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
        closure_class=(
            str(arguments["closure_class"]).strip().lower()
            if arguments.get("closure_class") is not None
            else None
        ),
        closure_evidence=arguments.get("closure_evidence"),
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
        # A class alone classifies a finding closed earlier; it is validated
        # against the stored status.
        request.closure_class,
    )
    if not any(value is not None for value in update_values):
        raise _UpdateResponseError(
            error_response(
                "At least one updatable field is required. Provide status, "
                "details/content, resolution_notes, summary, severity, "
                "discovery_type, tags, superseded_by, or closure_class."
            )
        )
    _refuse_oversized_summary_or_tags(request)
    return request








def _resolve_update_writer(
    request: _KnowledgeUpdateRequest,
    discovery: DiscoveryNode,
    *,
    inferred_exempt: Optional[bool] = None,
) -> str:
    """Apply the effective-severity identity gate for discovery updates.

    ``inferred_exempt`` is ``_inferred_writer_exempt()`` for this call; the
    same server-inferred binding rule as ``_resolve_store_writer`` applies.
    """
    if _effective_update_severity(request, discovery) in _GATED_SEVERITIES:
        refusal = _gated_write_refused_on_inferred_binding(
            inferred_exempt, update=True
        )
        if refusal is not None:
            raise _UpdateResponseError(refusal)
        agent_id, error = require_registered_agent(request.arguments)
    else:
        agent_id, error, _ = _resolve_low_friction_writer(
            request.arguments, inferred_exempt=inferred_exempt
        )
    if error:
        raise _UpdateResponseError(error)
    return agent_id




# What _requested_non_owner_edits lets a non-owner set on a gated finding, but
# only in the call that closes it. Every other field it lists is the owner's.














# By what standard was this closed. `status` alone is two-valued over a
# three-valued world — the state a reconciler keeps meeting is "not currently
# observed, cause unknown", and forced to pick, everyone picks the one that
# shortens the queue. Recording the standard keeps a weak closure attached to
# its own row instead of diluting what "resolved" means graph-wide.


# Where a class may sit: the closing statuses, and archived and cold, where the
# lifecycle moves closed rows for retention without reopening them. Migration
# 071 made discoveries_closure_class_requires_closed admit exactly this set;
# tests/test_kg_closure_class.py holds the two together.
# Reopening a row (open, disputed) clears its class.

# Evidence each class must actually carry. These two encode the specific ways a
# closure went wrong on 2026-08-19 and are not generic diligence prompts.
#
#   fix_verified requires `observed` because the failure was claiming a verified
#   fix on evidence that the OLD SYMPTOM WAS GONE. Absence of the symptom is not
#   observation of the fix: the subject-keying half of one dedup repair had never
#   been exercised live, and the closure said it was working.
#
#   unobserved requires `instrument_check` because the failure was concluding a
#   condition had ceased from a sibling signal still being written. Siblings
#   share the sink, not the emitter, so that check carries no information about
#   whether THIS condition would still be recorded.

# Whether the storage layer writes closure_class and closure_evidence. It does.
# #1752 added the validation above and migration 064's two columns, but until
# 2026-09-26 no update path wrote them. Now the AGE SQL sync, the AGE SQL-only
# fallback and the Postgres backend each write both (the AGE node mirrors
# them), and each clears both on an update that reopens the row.
# tests/test_kg_closure_hints.py derives this value from those three update
# paths, so dropping a write without flipping it (or the reverse) fails there.
#
# Storing the class needs migration 071. 064's
# discoveries_closure_class_requires_closed admitted a class only on resolved,
# closed, wont_fix or superseded rows, so the lifecycle's resolved -> archived
# move (and archived -> cold) would have been refused for every classified row.


























def _build_update_response(
    request: _KnowledgeUpdateRequest,
    discovery: Optional[DiscoveryNode],
    normalized_status: Optional[str],
    supersession_warning: Optional[str],
) -> Sequence[TextContent]:
    """Render the stable update response shape.

    ``discovery`` is the record read back after the write, so closure_class in
    the response is what the row holds, not what the caller sent.
    """
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

    stored_class = getattr(discovery, "closure_class", None) if discovery else None
    if request.closure_class is not None:
        payload["closure_class"] = stored_class
        if stored_class != request.closure_class:
            payload["closure_class_note"] = _unstored_closure_class_note(
                request.discovery_id,
                request.closure_class,
                normalized_status,
                notes_passed=request.resolution_note is not None,
            )
    elif normalized_status in _CLOSING_STATUSES:
        # Non-breaking on purpose: a required field would break the KG
        # gardener's mechanical auto-resolve on its next run. But a silent
        # accept is how the graph got here, so say it at the moment of closing
        # rather than leaving the reader to discover it later. A row that kept
        # a class from an earlier update is not unclassified: its class is
        # echoed and nothing is said.
        payload["closure_class"] = stored_class
        if stored_class is None:
            payload["closure_class_note"] = _unclassified_closure_note(
                request.discovery_id,
                normalized_status,
                notes_passed=request.resolution_note is not None,
            )
    return success_response(
        payload, arguments=_anonymous_writer_signature_arguments(request.arguments)
    )


async def _execute_discovery_update(
    request: _KnowledgeUpdateRequest, graph: Any
) -> Sequence[TextContent]:
    """Authorize, persist, and render one discovery update."""
    discovery = await graph.get_discovery(request.discovery_id)
    if not discovery:
        raise _UpdateResponseError(
            await _discovery_not_found(request.discovery_id, graph)
        )

    agent_id = _resolve_update_writer(
        request, discovery, inferred_exempt=await _inferred_writer_exempt()
    )
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
@_requires_proven_agent_id
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
@_knowledge_read_response_mode
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

            summary_view = discovery.to_dict(include_details=False)
            # The page slices details only; the closure evidence is part of
            # the record this route returns, as on the unpaginated branch.
            if discovery.closure_class is not None and discovery.closure_evidence is not None:
                summary_view["closure_evidence"] = discovery.closure_evidence
            response = {
                "discovery": summary_view,
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

        annotate_resolution_notes(response["discovery"], discovery)
        add_resolution_notes_hint(response)

        # This is the route a search digest's open_one hint points at, so a
        # superseded record has to name its replacement here too; the stored
        # row carries no successor column, only the AGE SUPERSEDES edge.
        if response["discovery"].get("status") == "superseded":
            await _annotate_supersession([response["discovery"]], graph)

        # Response chain traversal (Dec 2025 - restores get_response_chain_graph functionality)
        include_chain = arguments.get("include_response_chain", False)
        if include_chain:
            max_depth = _coerce_pagination_int(
                arguments.get("max_chain_depth"),
                default=10,
                minimum=1,
                maximum=32,
            )
            chain_limit = _coerce_pagination_int(
                arguments.get("response_chain_limit"),
                default=100,
                minimum=1,
                maximum=200,
            )
            chain_offset = _coerce_pagination_int(
                arguments.get("response_chain_offset"),
                default=0,
                minimum=0,
                maximum=10_000,
            )
            page_limit = (
                chain_limit
                if chain_offset == 10_000
                else min(chain_limit, 10_000 - chain_offset)
            )

            # Check if backend supports response chain traversal
            if hasattr(graph, 'get_response_chain'):
                try:
                    chain_with_lookahead = await graph.get_response_chain(
                        discovery_id,
                        max_depth=max_depth,
                        limit=page_limit,
                        offset=chain_offset,
                        lookahead=True,
                    )
                    more_rows_exist = len(chain_with_lookahead) > page_limit
                    next_offset = chain_offset + page_limit
                    truncated = more_rows_exist and next_offset > 10_000
                    has_more = more_rows_exist and not truncated
                    chain = chain_with_lookahead[:page_limit]
                    response["response_chain"] = {
                        "count": len(chain),
                        "max_depth": max_depth,
                        "limit": chain_limit,
                        "offset": chain_offset,
                        "has_more": has_more,
                        "next_offset": next_offset if has_more else None,
                        "truncated": truncated,
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

    oversized_tags = _oversized_tags(disc_data.get("tags"), "It was not stored.")
    if oversized_tags:
        raise _BatchItemError(" ".join(oversized_tags))

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
    _annotate_knowledge_confidence_authority(discovery)

    if _auto_link_requested(disc_data.get("auto_link_related")):
        similar = await graph.find_similar(discovery, limit=3)
        discovery.related_to = [item.id for item in similar]

    if discovery.severity in ["high", "critical"]:
        from ..utils import verify_agent_ownership

        if not verify_agent_ownership(agent_id, arguments):
            raise _BatchItemError(
                "Authentication required for high-severity discoveries"
            )

    oversized = _oversized_metadata(discovery)
    if oversized:
        raise _BatchItemError(" ".join(oversized))

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
    return success_response(
        response, arguments=_anonymous_writer_signature_arguments(arguments)
    )


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










def _parse_note_response_to(
    arguments: Dict[str, Any],
) -> Optional[ResponseTo]:
    """Parse an optional typed parent link for a note."""
    try:
        return _parse_response_to(arguments.get("response_to"))
    except _InvalidResponseTo as exc:
        raise _NoteResponseError(error_response(str(exc))) from None






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
    oversized = _oversized_metadata(note)
    if oversized:
        raise _NoteResponseError(_write_bound_refusal(*oversized))
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
    return success_response(
        response, arguments=_anonymous_writer_signature_arguments(request.arguments)
    )


async def _execute_note_write(
    request: _KnowledgeNoteRequest, graph: Any
) -> Sequence[TextContent]:
    """Build and persist one note through the shared write path."""
    note = _build_note_discovery(request)
    await _persist_note_discovery(graph, note, request.agent_id)
    return _build_note_response(request, note)


@_requires_proven_agent_id
async def handle_knowledge_note(
    arguments: Dict[str, Any],
) -> Sequence[TextContent]:
    """Implement the preferred knowledge(action='note') write path."""
    arguments.setdefault("_tool_name", "knowledge")
    agent_id, error, is_anonymous_writer = _resolve_low_friction_writer(
        arguments, inferred_exempt=await _inferred_writer_exempt()
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

    oversized_tags = _oversized_tags(arguments.get("tags"), "Nothing was stored.")
    if oversized_tags:
        return [_write_bound_refusal(*oversized_tags)]

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


# NOT deprecated (operator decision, 2026-08-29). This carried
# deprecated=True/superseded_by="knowledge" from the #429 consolidation while
# simultaneously sitting in LITE_MODE_TOOLS and in the CLAUDE.md/AGENTS.md
# shared contract as a first-class workflow tool. Both cannot be true: the
# flag suppressed it from list_tools orientation and stamped a [DEPRECATED]
# prefix on a tool the contract tells agents to reach for. The contract wins;
# leave_note is a supported low-friction entry point that happens to share an
# implementation with knowledge(action='note').
@mcp_tool(
    "leave_note",
    timeout=10.0,
)
@_requires_proven_agent_id
async def handle_leave_note(
    arguments: Dict[str, Any],
) -> Sequence[TextContent]:
    """Low-friction note entry point; shares the knowledge(action='note') handler."""
    adapted_arguments = apply_param_aliases("leave_note", arguments)
    adapted_arguments["_tool_name"] = "leave_note"
    return await handle_knowledge_note(adapted_arguments)


@mcp_tool("cleanup_knowledge_graph", timeout=60.0, register=False)
@_requires_proven_writer("cleanup")
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
@_requires_proven_writer("synthesize")
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
@_knowledge_read_response_mode
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
                    lifecycle_total = stats.get("total_discoveries")
                    lifecycle_statuses = dict(stats.get("by_status") or {})
                    raw_total = raw_stats.get("total_discoveries")
                    raw_statuses = dict(raw_stats.get("by_status") or {})
                    stats["raw_current_counts"] = {
                        "total_discoveries": raw_total,
                        "by_status": raw_statuses,
                        "scope": raw_stats.get("scope", {}),
                    }
                    stats["canonical_current_total"] = raw_total
                    stats["lifecycle_managed_counts"] = {
                        "total_discoveries": lifecycle_total,
                        "by_status": lifecycle_statuses,
                        "included_statuses": ["open", "resolved", "archived", "cold"],
                    }
                    if (
                        isinstance(lifecycle_total, int)
                        and isinstance(raw_total, int)
                        and lifecycle_total != raw_total
                    ):
                        stats["statuses_outside_lifecycle_buckets"] = {
                            status: count
                            for status, count in raw_statuses.items()
                            if status not in lifecycle_statuses and count
                        }
                        stats["count_scope_warning"] = (
                            "canonical_current_total is the corpus-size field. "
                            "lifecycle_managed_counts intentionally covers only "
                            "open/resolved/archived/cold; terminal statuses such as "
                            "superseded or closed remain in raw_current_counts."
                        )
        except Exception as exc:
            stats["raw_current_counts_error"] = str(exc)

        return success_response({
            "message": "Lifecycle statistics; canonical_current_total is corpus size",
            "stats": stats,
        }, arguments=arguments)

    except Exception as e:
        return [error_response(f"Failed to get lifecycle stats: {str(e)}")]




def _authorize_supersede_notes(
    request: _KnowledgeUpdateRequest,
    finding: DiscoveryNode,
    old_id: str,
    *,
    inferred_exempt: Optional[bool] = None,
) -> None:
    """Refuse notes on a high or critical finding from anyone but its owner.

    Run on the first read, before the supersede writes anything, and again on
    the read taken just before the flip: a finding raised to high or critical
    in between is held to the rule as it stands then. Raises
    _UpdateResponseError. ``inferred_exempt`` is ``_inferred_writer_exempt()``
    for this call.
    """
    agent_id = _resolve_update_writer(
        request, finding, inferred_exempt=inferred_exempt
    )
    if _effective_update_severity(request, finding) in _GATED_SEVERITIES:
        _require_owned_binding(request, agent_id)
        if finding.agent_id != agent_id:
            raise _UpdateResponseError(
                error_response(
                    f"Permission denied on high-severity discovery '{old_id}': "
                    "a supersede appends resolution_notes to its details, and "
                    "only its owner may add notes while superseding it. "
                    "Nothing was changed.",
                    recovery={
                        "action": (
                            "Record your correction and its rationale as a "
                            "finding that responds to this one (response_to={"
                            f"'discovery_id': '{old_id}', 'response_type': "
                            "'supersedes'})."
                        ),
                        "related_tools": ["knowledge", "search_knowledge_graph"],
                    },
                )
            )


async def _prepare_supersede_notes(
    arguments: Dict[str, Any], graph: Any, new_id: str, old_id: str, note: str
) -> Optional[_KnowledgeUpdateRequest]:
    """Check a supersede's resolution_notes before the supersede writes anything.

    resolution_notes are appended to the older finding's details the way an
    update appends them, and held to the same bound and the same gate. On a
    high or critical finding only its owner may append them: an update lets a
    non-owner add notes there only while closing it as resolved, closed or
    wont_fix, never superseded. A refusal raises _UpdateResponseError.
    Returns the request the notes are built from, or None when the older
    finding does not exist, which supersede_discovery then reports. The
    details themselves are built later, from a read taken just before the
    write (_supersede_notes_block_details), so an edit that lands while the
    edge is created is kept.
    """
    leaked_marker = _detect_toolcall_markup_leak(note)
    if leaked_marker:
        raise _UpdateResponseError(
            _degenerate_write_response(leaked_marker, "resolution_notes")
        )
    old = await graph.get_discovery(old_id)
    if old is None:
        return None
    request = _KnowledgeUpdateRequest(
        arguments=arguments,
        discovery_id=old_id,
        status="superseded",
        details=None,
        resolution_note=note,
        summary=None,
        severity=None,
        discovery_type=None,
        tags=None,
        superseded_by=new_id,
    )
    _authorize_supersede_notes(
        request, old, old_id, inferred_exempt=await _inferred_writer_exempt()
    )
    _supersede_notes_block_details(request, old)
    return request


async def _supersede_flip_details(
    graph: Any, request: _KnowledgeUpdateRequest, old_id: str
) -> tuple[Optional[str], Optional[str]]:
    """(details, problem) for the flip, from a fresh read of the older finding.

    Read after the edge is created and just before the flip, so the window
    in which a concurrent edit can be overwritten is the read-then-write gap
    an update has, not that gap plus the edge work. The bound and the owner
    rule are checked again on the fresh read: if the details grew past the
    bound meanwhile, the finding was raised to high or critical and the
    caller is not its owner, or the finding is gone, the flip goes ahead
    without notes and ``problem`` says why.
    """
    latest = await graph.get_discovery(old_id)
    if latest is None:
        return None, f"'{old_id}' could not be read again before the flip."
    inferred_exempt = await _inferred_writer_exempt()
    try:
        _authorize_supersede_notes(
            request, latest, old_id, inferred_exempt=inferred_exempt
        )
    except _UpdateResponseError:
        return None, (
            f"'{old_id}' was raised to high or critical while the supersede "
            "ran, and only its owner may add notes to it now."
        )
    try:
        return _supersede_notes_block_details(request, latest), None
    except _UpdateResponseError:
        return None, (
            f"The details of '{old_id}' grew while the supersede ran and no "
            "longer leave room for these notes."
        )


@mcp_tool("supersede_discovery", timeout=15.0, register=False)
@_requires_proven_writer("supersede")
async def handle_supersede_discovery(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Mark a discovery as superseding another.

    Creates a SUPERSEDES edge in the knowledge graph and flips the older
    discovery to superseded. Superseded entries receive a ranking penalty in
    search results. resolution_notes, when given, are appended to the older
    discovery's details in that same flip, as an update appends them.

    Args:
        discovery_id: The newer discovery (the one that replaces)
        supersedes_id: The older discovery being replaced
        resolution_notes: Why it was superseded (optional)

    Returns success/failure status.
    """
    new_id = arguments.get("discovery_id")
    old_id = arguments.get("supersedes_id")

    if not new_id or not old_id:
        return [error_response("Both discovery_id and supersedes_id are required")]

    # Parsed as update parses them: blank notes are no notes.
    raw_notes = arguments.get("resolution_notes")
    note = None
    if raw_notes is not None:
        note = str(raw_notes).strip() or None

    try:
        graph = await get_knowledge_graph()
        if not hasattr(graph, "supersede_discovery"):
            return [error_response("SUPERSEDES edges require AGE graph backend")]

        notes_request = None
        if note is not None:
            try:
                notes_request = await _prepare_supersede_notes(
                    arguments, graph, str(new_id), str(old_id), note
                )
            except _UpdateResponseError as refused:
                return [refused.response]

        result = await graph.supersede_discovery(new_id=new_id, old_id=old_id)
        if result.get("success"):
            # Also flip the old entry to superseded so it's flagged stale in
            # search — keep all three supersede paths consistent (store/update
            # both set status + edge; the edge alone doesn't change status).
            flip: dict[str, Any] = {
                "status": "superseded",
                "updated_at": _utc_now_iso(),
            }
            notes_problem = None
            if notes_request is not None:
                details, notes_problem = await _supersede_flip_details(
                    graph, notes_request, str(old_id)
                )
                if details is not None:
                    flip["details"] = details
            flipped = False
            try:
                flipped = await graph.update_discovery(old_id, flip)
            except Exception as exc:  # noqa: BLE001 — edge is the primary effect
                logger.warning(f"[KG] supersede status flip for {old_id[:8]} failed: {exc}")
            if notes_request is not None:
                if flipped and "details" in flip:
                    result["resolution_notes_appended_to"] = old_id
                elif notes_problem:
                    result["resolution_notes_warning"] = (
                        f"The SUPERSEDES edge was recorded, but resolution_notes "
                        f"were not appended: {notes_problem} Read it with "
                        f"knowledge(action='details', discovery_id='{old_id}') "
                        "and record the notes as a finding that responds to it "
                        f"(response_to={{'discovery_id': '{old_id}', "
                        "'response_type': 'supersedes'})."
                    )
                else:
                    result["resolution_notes_warning"] = (
                        f"The SUPERSEDES edge was recorded, but the update "
                        f"that marks '{old_id}' superseded and appends "
                        "resolution_notes to its details did not apply. Read "
                        f"it with knowledge(action='details', discovery_id="
                        f"'{old_id}'); if the notes are not there, send "
                        f"knowledge(action='update', discovery_id='{old_id}', "
                        "status='superseded', resolution_notes=...)."
                    )
            return success_response(
                result, arguments=_anonymous_writer_signature_arguments(arguments)
            )
        else:
            return [error_response(result.get("error", "Failed to create SUPERSEDES edge"))]
    except Exception as e:
        return [error_response(f"Failed to supersede discovery: {str(e)}")]


@mcp_tool("promote_memory_claim", timeout=20.0, register=False)
@_requires_proven_writer("promote")
async def handle_promote_memory_claim(
    arguments: Dict[str, Any],
) -> Sequence[TextContent]:
    """Promote one imported memory into a corroborated, governed KG claim.

    Promotion creates a new discovery and leaves the source memory unchanged.
    The new row keeps the compatible knowledge-authority receipt and embeds a
    content-addressed ``unitares.transition_receipt.v1`` over the exact
    promotion projection.  A normal store cannot forge either receipt because
    callers cannot supply provenance directly.  The transition receipt proves
    attributable same-object persistence, not correctness or approval.
    """
    from ..support.agent_auth import verify_agent_ownership
    from ..utils import check_agent_can_operate

    agent_id, error = require_registered_agent(arguments)
    if error:
        return [error]
    blocked = check_agent_can_operate(agent_id)
    if blocked:
        return [blocked]
    if not verify_agent_ownership(agent_id, arguments):
        return [
            error_response(
                "Promotion requires a caller-owned registered identity.",
                error_code="AUTH_REQUIRED",
                error_category="auth_error",
                recovery={
                    "action": "Pass the client_session_id returned by start_session on this call.",
                    "related_tools": ["start_session", "identity"],
                },
            )
        ]

    source_id = str(arguments.get("discovery_id") or "").strip()
    summary = str(arguments.get("summary") or "").strip()
    verification_basis = str(arguments.get("verification_basis") or "").strip()
    decision_standard = str(arguments.get("decision_standard") or "").strip()
    raw_evidence_ids = arguments.get("evidence_ids") or []
    evidence_ids = list(dict.fromkeys(
        str(value).strip() for value in raw_evidence_ids if str(value).strip()
    ))

    missing = [
        name
        for name, value in (
            ("discovery_id", source_id),
            ("summary", summary),
            ("verification_basis", verification_basis),
            ("decision_standard", decision_standard),
        )
        if not value
    ]
    if missing:
        return [error_response(f"Promotion requires: {', '.join(missing)}")]
    if not evidence_ids:
        return [error_response("Promotion requires at least one evidence_ids entry")]
    if source_id in evidence_ids:
        return [error_response("The source memory cannot corroborate itself")]

    try:
        import asyncio

        graph = await get_knowledge_graph()
        source = await graph.get_discovery(source_id)
        if source is None:
            return [error_response(f"Source discovery '{source_id}' not found")]
        source_authority = assess_authority(source)
        if source_authority.tier != IMPORTED_CONTEXT:
            return [
                error_response(
                    "Only imported memory context uses this promotion path; "
                    f"source tier is {source_authority.tier!r}. Store or update "
                    "native findings directly."
                )
            ]

        fetched = await asyncio.gather(
            *(graph.get_discovery(evidence_id) for evidence_id in evidence_ids),
            return_exceptions=True,
        )
        evidence_rows = []
        missing_evidence = []
        for evidence_id, row in zip(evidence_ids, fetched):
            if isinstance(row, Exception) or row is None:
                missing_evidence.append(evidence_id)
            else:
                evidence_rows.append(row)
        if missing_evidence:
            return [
                error_response(
                    "Evidence discoveries not found: " + ", ".join(missing_evidence)
                )
            ]

        imported_evidence = [
            row.id
            for row in evidence_rows
            if assess_authority(row).tier == IMPORTED_CONTEXT
        ]
        if imported_evidence:
            return [
                error_response(
                    "Imported memories cannot independently corroborate another "
                    "memory. Promote or verify these evidence rows first: "
                    + ", ".join(imported_evidence)
                )
            ]

        discovery_type = _normalize_discovery_type(
            arguments.get("discovery_type") or "insight"
        )
        if discovery_type not in VALID_DISCOVERY_TYPES:
            return [
                _invalid_enum_response(
                    "discovery_type",
                    discovery_type,
                    VALID_DISCOVERY_TYPES,
                )
            ]
        severity = _parse_store_severity(arguments)
        details = str(arguments.get("details") or arguments.get("content") or "")
        if len(summary) > MAX_SUMMARY_LEN:
            return [
                error_response(
                    f"Promotion summary exceeds {MAX_SUMMARY_LEN} characters; "
                    "state one bounded claim and put support in details."
                )
            ]
        if len(details) > MAX_DETAILS_LEN:
            return [
                error_response(
                    f"Promotion details exceed {MAX_DETAILS_LEN} characters"
                )
            ]

        provenance, provenance_chain = await _capture_store_provenance(
            arguments, agent_id
        )
        # One server-minted timestamp identifies the durable row and the
        # accepted transition.  The receipt normalizes it to fixed UTC
        # microseconds when hashing; storage may render the same instant with
        # ``+00:00`` instead of ``Z`` on read-back.
        claim_id = _new_discovery_id()
        promoted_at = claim_id
        provenance["source"] = "explicit_promotion"
        provenance["knowledge_authority"] = {
            "schema": PROMOTION_SCHEMA,
            "source_id": source_id,
            "evidence_ids": evidence_ids,
            "promoted_by": agent_id,
            "promoted_at": promoted_at,
            "verification_basis": verification_basis,
            "decision_standard": decision_standard,
        }
        tags = normalize_tags([
            *(arguments.get("tags") or []),
            PROMOTION_TAG,
            "promoted-from-memory",
        ])
        claim = DiscoveryNode(
            id=claim_id,
            agent_id=agent_id,
            type=discovery_type,
            summary=summary,
            details=details,
            tags=tags,
            severity=severity,
            timestamp=promoted_at,
            status="open",
            related_to=[source_id, *evidence_ids],
            references_files=arguments.get("related_files") or [],
            provenance=provenance,
            provenance_chain=provenance_chain,
            confidence=_parse_store_confidence(arguments),
        )
        from src.transition_receipt import build_knowledge_promotion_receipt

        transition_receipt = build_knowledge_promotion_receipt(claim)
        provenance["transition_receipt"] = transition_receipt
        _annotate_knowledge_confidence_authority(claim)
        # Measured on the stored list, which includes the two promotion tags.
        oversized = _oversized_tags(
            tags,
            f"The stored list includes the tags '{PROMOTION_TAG}' and "
            "'promoted-from-memory', which promotion adds. Nothing was stored.",
        ) or _oversized_metadata(claim)
        if oversized:
            return [_write_bound_refusal(*oversized)]
        await graph.add_discovery(claim)
        await _broadcast_knowledge_write(claim, agent_id)

        return success_response(
            {
                "message": "Imported memory promoted to a governed claim",
                "discovery_id": claim.id,
                "source_id": source_id,
                "evidence_ids": evidence_ids,
                "authority": assess_authority(claim).to_dict(),
                "promotion_receipt": provenance["knowledge_authority"],
                "transition_receipt": transition_receipt,
                "discovery": claim.to_dict(include_details=False),
                "note": (
                    "Governed means the promotion transition is attributable and "
                    "evidence-linked; it does not make the claim infallible."
                ),
            },
            arguments=arguments,
        )
    except Exception as exc:
        return [error_response(f"Failed to promote memory claim: {str(exc)}")]


@mcp_tool("audit_knowledge_graph", timeout=60.0, register=False)
@_requires_proven_writer("audit")
async def handle_audit_knowledge_graph(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Audit knowledge graph for staleness and health.

    Read-only analysis that scores open KG entries by age, activity,
    and type, grouping them into health buckets. Does NOT modify anything.

    Args:
        scope: "open" (default), "all", "by_agent"
        top_n: Number of stale entries to return (default: 10)
        use_model: If true, ask the local model to assess relevance (default: false)

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
