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

from functools import wraps
from typing import Dict, Any, Sequence, Optional
from mcp.types import TextContent
from datetime import datetime, timezone, timedelta
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
from ..utils import success_response, error_response, require_argument, require_agent_id, require_registered_agent  # noqa: F401
from ..decorators import mcp_tool
from ..validators import apply_param_aliases
from ..identity.stable_session import audit_reference
from src.knowledge_graph import (
    get_knowledge_graph, DiscoveryNode, ResponseTo, normalize_tags,  # noqa: F401
)
from src.mcp_handlers.knowledge.imported_preview import annotate_imported_read
from src.mcp_handlers.knowledge.read_metadata import annotate_resolution_notes, add_resolution_notes_hint

from src.knowledge_authority import (
    IMPORTED_CONTEXT,  # noqa: F401
    PROMOTION_SCHEMA,  # noqa: F401
    PROMOTION_TAG,  # noqa: F401
    assess_authority,  # noqa: F401
    rank_by_authority,  # noqa: F401
)
from src.mcp_handlers.knowledge.limits import (
    MAX_DETAILS_LEN, MAX_SUMMARY_LEN, SUMMARY_TRUNCATION_MARKER,  # noqa: F401
)
from config.governance_config import config
from src.logging_utils import get_logger
from src.coherence_provenance import (
    BEHAVIORAL_UPDATE_CONSISTENCY_ROLE,  # noqa: F401
    coherence_role_for_source,  # noqa: F401
)
from src.perf_monitor import record_ms
from src.recall_telemetry import record_recall_event  # noqa: F401
from ..support.coerce import LimitError, parse_limit
from ..support.llm_delegation import (
    LOCAL_LLM_WAIT_BUFFER_S,  # noqa: F401
    synthesize_results,  # noqa: F401
)
from ..support.tool_hints import KNOWLEDGE_SEARCH_TOOL

logger = get_logger(__name__)

from src.mcp_handlers.shared import lazy_mcp_server as mcp_server  # noqa: F401
from src.broadcaster import broadcaster_instance


from .validation_helpers import (
    VALID_DISCOVERY_TYPES,
    VALID_SEVERITIES,  # noqa: F401
    SEVERITY_ALIASES,  # noqa: F401
    _normalize_discovery_type,
    _detect_toolcall_markup_leak,
    _degenerate_write_response,
    _write_bound_refusal,
    _oversized_tags,
    _oversized_metadata,  # noqa: F401
    _invalid_enum_response,
    _coerce_pagination_int,
    _annotate_knowledge_confidence_authority,  # noqa: F401
    _AUDIT_QUERY_TEXT_MAX,  # noqa: F401
    _audit_safe_query_text,
    _audit_safe_tags,
    _discovery_not_found,
    _StoreResponseError,
    _KnowledgeStoreRequest,
    _KnowledgeStoreState,  # noqa: F401
    _InvalidResponseTo,  # noqa: F401
    _parse_response_to,  # noqa: F401
    _auto_link_requested,  # noqa: F401
    _link_similar_store_discoveries,  # noqa: F401
    _refuse_oversized_store_metadata,  # noqa: F401
    _authorize_store_discovery,  # noqa: F401
    _attach_store_response_hints,  # noqa: F401
    _attach_store_related_discoveries,  # noqa: F401
    _KnowledgeUpdateRequest,
    _UpdateResponseError,
    _refuse_oversized_summary_or_tags,
    _GATED_SEVERITIES,  # noqa: F401
    _effective_update_severity,  # noqa: F401
    _requested_non_owner_edits,  # noqa: F401
    _require_owned_binding,  # noqa: F401
    _authorize_high_severity_update,  # noqa: F401
    _apply_update_metadata_fields,  # noqa: F401
    CLOSURE_CLASSES,  # noqa: F401
    _CLOSING_STATUSES,  # noqa: F401
    _CLASS_ADMITTING_STATUSES,  # noqa: F401
    CLOSURE_CLASS_PERSISTED,  # noqa: F401
    _unclassified_closure_note,  # noqa: F401
    _resolution_notes_call,  # noqa: F401
    _unstored_closure_class_note,  # noqa: F401
    _validate_closure_class,  # noqa: F401
    _build_discovery_updates,  # noqa: F401
    _BatchItemError,  # noqa: F401
    _PreparedBatchDiscovery,  # noqa: F401
    _BatchStoreResult,  # noqa: F401
    _truncate_batch_summary,  # noqa: F401
    _truncate_batch_details,  # noqa: F401
    _parse_batch_response_to,  # noqa: F401
    _parse_batch_severity,  # noqa: F401
    _parse_batch_confidence,  # noqa: F401
    _NOTE_TOTAL_LEN,  # noqa: F401
    _NoteResponseError,
    _KnowledgeNoteRequest,
    _truncate_note_text,  # noqa: F401
    _split_note_text,  # noqa: F401
    _infer_note_severity,  # noqa: F401
    _supersede_notes_block_details,  # noqa: F401
)
from .search_helpers import (
    _LEAN_DISCOVERY_FIELDS,  # noqa: F401
    _one_line,  # noqa: F401
    _lean_search_payload,  # noqa: F401
    _compact_knowledge_read_result,
    _compute_staleness,  # noqa: F401
    _compute_staleness_warning,  # noqa: F401
    _SearchParameterError,
    _KnowledgeSearchRequest,
    _KnowledgeSearchState,
    _optional_flag,
    _resolve_detail_inclusion,
    _parse_search_timestamp,
    _parse_half_life,
    _validate_search_backend,  # noqa: F401
    _retrieve_fts_candidates,  # noqa: F401
    _retrieve_substring_candidates,  # noqa: F401
    _within_window,  # noqa: F401
    _window_kwargs,  # noqa: F401
    _tag_kwargs,  # noqa: F401
    _authority_ranking_enabled,  # noqa: F401
    _authority_pool_size,  # noqa: F401
    _recency_weighted,  # noqa: F401
    _rank_search_documents,
    _candidate_status_visible,  # noqa: F401
    _filter_and_rerank_candidates,  # noqa: F401
    _run_newest_first_text_search,  # noqa: F401
    _run_indexed_filter_search,
    _apply_semantic_fts_fallback,
    ARCHIVED_COLD_MIN_RESULTS,  # noqa: F401
    ARCHIVED_COLD_SCORE_PENALTY,  # noqa: F401
    ARCHIVED_COLD_TIER,  # noqa: F401
    KNOWLEDGE_SEARCH_TIMEOUT_S,
    ARCHIVED_COLD_RESPONSE_RESERVE_S,  # noqa: F401
    _ABANDONED_ARCHIVED_COLD_PASSES,  # noqa: F401
    SEARCH_SYNTHESIS_RESERVE_S,  # noqa: F401
    SEARCH_SYNTHESIS_MIN_TIMEOUT_S,  # noqa: F401
    SUPERSESSION_LOOKUP_RESERVE_S,  # noqa: F401
    _archived_cold_widening,  # noqa: F401
    _bounded_supersession_lookup,  # noqa: F401
    _apply_archived_cold_tier,
    _candidate_matches_semantic_fallback,  # noqa: F401
    _exclude_search_labels,
    _serialize_search_discoveries,
    _base_search_response,
    _attach_search_diagnostics,
    _attach_empty_search_guidance,
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


from .mutation_helpers import (
    _broadcast_knowledge_write,  # noqa: F401
    _resolve_reader_agent_id,
    _annotate_supersession,
    _record_supersession_edge,  # noqa: F401
    _build_s7_provenance_chain_with_fallback,  # noqa: F401
    _check_display_name_required,  # noqa: F401
    _AGENT_DISPLAY_LOOKUP_FIELDS,  # noqa: F401
    _agent_metadata_text,  # noqa: F401
    _build_agent_display_payload,  # noqa: F401
    _agent_metadata_matches,  # noqa: F401
    _find_agent_display_metadata,  # noqa: F401
    _resolve_agent_display,  # noqa: F401
    _agent_display_for_response,
    _anonymous_writer_signature_arguments,  # noqa: F401
    _ANONYMOUS_WRITER_KEY_ENV,  # noqa: F401
    _ANONYMOUS_WRITER_FALLBACK_KEY,  # noqa: F401
    _ANONYMOUS_WRITER_PREFIX,  # noqa: F401
    _ANONYMOUS_WRITER_SHARED_SUFFIX,  # noqa: F401
    _pseudonymize_anonymous_writer_source,  # noqa: F401
    _derive_anonymous_writer_id,  # noqa: F401
    _inferred_writer_exempt,
    _inferred_binding_untrusted,  # noqa: F401
    _inferred_binding_refusal,  # noqa: F401
    _unproven_agent_id_refusal,  # noqa: F401
    _requires_proven_agent_id,
    _resolve_low_friction_writer,
    _store_needs_registered_writer,  # noqa: F401
    _gated_write_refused_on_inferred_binding,  # noqa: F401
    _requires_proven_writer,
    _resolve_store_writer,
    KnowledgeWriteAuthor,  # noqa: F401
    resolve_knowledge_write_author,  # noqa: F401
    _truncate_store_content,  # noqa: F401
    _parse_store_response_to,  # noqa: F401
    _parse_store_severity,  # noqa: F401
    _parse_store_confidence,  # noqa: F401
    _capture_store_provenance,  # noqa: F401
    _build_store_discovery,  # noqa: F401
    _prepare_store_supersession,  # noqa: F401
    _persist_store_discovery,  # noqa: F401
    _build_store_response,  # noqa: F401
    _execute_single_store,
    _resolve_update_writer,  # noqa: F401
    _build_update_response,  # noqa: F401
    _execute_discovery_update,
    _validate_batch_discoveries,  # noqa: F401
    _prepare_batch_discovery,  # noqa: F401
    _persist_batch_discovery,  # noqa: F401
    _process_batch_discovery,  # noqa: F401
    _build_batch_store_response,  # noqa: F401
    _handle_store_knowledge_graph_batch,
    _parse_note_response_to,  # noqa: F401
    _capture_note_provenance,  # noqa: F401
    _build_note_discovery,  # noqa: F401
    _persist_note_discovery,  # noqa: F401
    _build_note_response,  # noqa: F401
    _execute_note_write,
    _handle_cleanup_knowledge_graph,
    _handle_synthesize_knowledge_graph,
    _handle_get_lifecycle_stats,
    _authorize_supersede_notes,  # noqa: F401
    _prepare_supersede_notes,  # noqa: F401
    _supersede_flip_details,  # noqa: F401
    _handle_supersede_discovery,
    _handle_promote_memory_claim,
    _handle_audit_knowledge_graph,
    store_discovery_internal,  # noqa: F401
)


def _get_store_lineage_fn():
    from ..identity.shared import _get_lineage
    return _get_lineage


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
    """Run knowledge graph lifecycle cleanup."""
    return await _handle_cleanup_knowledge_graph(arguments)


@mcp_tool("synthesize_knowledge_graph", timeout=120.0, register=False)
@_requires_proven_writer("synthesize")
async def handle_synthesize_knowledge_graph(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Compound discrete discoveries into rolled-up topic summaries (Issue #1)."""
    return await _handle_synthesize_knowledge_graph(arguments)


@mcp_tool("get_lifecycle_stats", timeout=30.0, register=False)
@_knowledge_read_response_mode
async def handle_get_lifecycle_stats(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Get knowledge graph lifecycle statistics."""
    return await _handle_get_lifecycle_stats(arguments)


@mcp_tool("supersede_discovery", timeout=15.0, register=False)
@_requires_proven_writer("supersede")
async def handle_supersede_discovery(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Mark a discovery as superseding another."""
    return await _handle_supersede_discovery(arguments)


@mcp_tool("promote_memory_claim", timeout=20.0, register=False)
@_requires_proven_writer("promote")
async def handle_promote_memory_claim(
    arguments: Dict[str, Any],
) -> Sequence[TextContent]:
    """Promote one imported memory into a corroborated, governed KG claim."""
    default_type = arguments.get("discovery_type") or "insight"
    return await _handle_promote_memory_claim(arguments, default_discovery_type=default_type)


@mcp_tool("audit_knowledge_graph", timeout=60.0, register=False)
@_requires_proven_writer("audit")
async def handle_audit_knowledge_graph(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Audit knowledge graph for staleness and health."""
    return await _handle_audit_knowledge_graph(arguments)
