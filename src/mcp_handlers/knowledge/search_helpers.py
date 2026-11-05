"""Search request/state models, candidate filtering, ranking, staleness, and serialization helpers for knowledge handlers.

Extracted from ``knowledge/handlers.py`` to keep search candidate scoring,
archived/cold tiering, staleness computation, and response projection modular.
"""

from dataclasses import dataclass, field, replace
from typing import Dict, Any, Sequence, Optional
from mcp.types import TextContent
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import asyncio
import json
import math
import os
import re
import sys
import time

from ..utils import success_response, error_response
from ..identity.stable_session import audit_reference, redact_provenance
from src.knowledge_graph import (
    DiscoveryNode,
    normalize_tags,
    VALID_DISCOVERY_STATUSES,
    CLOSURE_CLASS_ADMITTING_STATUSES,
)
from src.mcp_handlers.knowledge.imported_preview import annotate_imported_read
from src.mcp_handlers.knowledge.read_metadata import annotate_resolution_notes, add_resolution_notes_hint
from src.knowledge_authority import (
    CHANNEL_MESSAGE,
    GOVERNED_CLAIM,
    IMPORTED_CONTEXT,
    assess_authority,
    channel_lanes,
    is_imported_context,
    is_lane_filter,
    rank_by_authority as _default_rank_by_authority,
)
from src.recall_telemetry import (
    LEXICAL_INDEX_MISS,
    LOW_CONFIDENCE,
    ZERO_RESULT,
    record_recall_event as _default_record_recall_event,
)
from ..support.coerce import LimitError, parse_limit
from ..support.llm_delegation import LOCAL_LLM_WAIT_BUFFER_S, synthesize_results as _default_synthesize_results
from ..support.tool_hints import KNOWLEDGE_SEARCH_TOOL
from .validation_helpers import _CLOSING_STATUSES
from src.logging_utils import get_logger

logger = get_logger(__name__)


def _h_attr(name: str, default: Any = None) -> Any:
    mod = sys.modules.get("src.mcp_handlers.knowledge.handlers")
    return getattr(mod, name, default) if mod is not None else default


def rank_by_authority(*args: Any, **kwargs: Any) -> Any:
    return _h_attr("rank_by_authority", _default_rank_by_authority)(*args, **kwargs)


async def synthesize_results(*args: Any, **kwargs: Any) -> Any:
    return await _h_attr("synthesize_results", _default_synthesize_results)(*args, **kwargs)


def record_recall_event(*args: Any, **kwargs: Any) -> Any:
    return _h_attr("record_recall_event", _default_record_recall_event)(*args, **kwargs)


def _resolve_agent_display(agent_id: Optional[str]) -> Optional[Dict[str, str]]:
    fn = _h_attr("_resolve_agent_display", None)
    return fn(agent_id) if fn is not None else None


async def _run_archived_cold_pass(second: Any, limit: int) -> None:
    fn = _h_attr("_run_archived_cold_pass", None)
    if fn is not None:
        await fn(second, limit)

def _compact_identity_assurance(assurance: Any) -> Dict[str, Any]:
    """Return the useful proof summary for a lean read response."""
    if not isinstance(assurance, dict):
        return {}
    return {
        key: assurance[key]
        for key in ("tier", "caller_proven")
        if key in assurance
    }


def _compact_caller_identity_envelope(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Compact caller identity metadata without touching discovery payloads."""
    compact = dict(payload)
    compact.pop("identity_context", None)

    if "identity_assurance" in compact:
        compact["identity_assurance"] = _compact_identity_assurance(
            compact["identity_assurance"]
        )

    signature = compact.get("agent_signature")
    if isinstance(signature, dict):
        compact_signature = {"uuid": signature.get("uuid")}
        assurance = _compact_identity_assurance(
            signature.get("identity_assurance")
        )
        if assurance:
            compact_signature["identity_assurance"] = assurance
        compact["agent_signature"] = compact_signature

    agent = compact.get("agent")
    if isinstance(agent, dict):
        compact_agent = dict(agent)
        compact_agent.pop("identity_context", None)
        if "identity_assurance" in compact_agent:
            compact_agent["identity_assurance"] = _compact_identity_assurance(
                compact_agent["identity_assurance"]
            )
        compact["agent"] = compact_agent

    return compact


_LEAN_DISCOVERY_FIELDS = (
    "id",
    "discovery_id",
    "summary",
    "type",
    "status",
    "severity",
    "tags",
    "superseded",
    "superseded_by",
    "staleness_warning",
    "last_activity_days",
    "authority",
    "imported_memory",
    "closure_class",
    "resolution_notes",
    # Set only on rows the archived/cold second pass appended, so a lean
    # reader can tell an aged-out row from a current one.
    "retrieval_tier",
    # Who wrote it: the write-time display label and the identity a reader
    # passes back as agent_id_filter. Without them the default lean search
    # told a reader what a finding says but not who wrote it (#2386); the
    # envelope's digest budget decides how much of this survives.
    "by",
    "_agent_id",
)


def _one_line(value: Any, limit: int = 240) -> Any:
    if not isinstance(value, str):
        return value
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 3].rstrip() + "..."


def _lean_search_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return one-line search digests without preview, score-map, or identity bloat."""
    discoveries = payload.get("discoveries")
    if not isinstance(discoveries, list):
        return payload

    # `relevance` must mean match QUALITY, never rank wearing a score's clothes.
    #
    # RRF assigns 1/(k + rank); at the default k=60 that makes rank 1 score
    # 0.0164, rank 2 0.0161, rank 3 0.0159 — for every query, whether the top
    # hit is exact or unrelated. Measured live 2026-08-29: two unrelated
    # queries returned byte-identical score triples, one of them a flat miss.
    # `rrf_fuse`'s docstring calls the scores "comparable across queries", and
    # they are — in the useless sense that rank 1 is always 1/61.
    #
    # The bug was ordering: rrf_scores sat AHEAD of similarity_scores in a
    # first-match-wins list, and the cross-encoder that would have outranked
    # both is flag-off by default (UNITARES_ENABLE_RERANKER). So the common
    # path picked the rank-derived number and never reached the cosine
    # similarity sitting in the same payload, which does carry match strength.
    #
    # Quality signals only, best first. The fusion score is still reported,
    # under a name that says what it is.
    quality_maps = (
        ("cross_encoder_rerank", payload.get("rerank_scores")),
        ("semantic_similarity", payload.get("similarity_scores")),
    )
    lean_discoveries = []
    for discovery in discoveries:
        if not isinstance(discovery, dict):
            continue
        lean = {
            key: discovery[key]
            for key in _LEAN_DISCOVERY_FIELDS
            if discovery.get(key) is not None
        }
        if "summary" in lean:
            lean["summary"] = _one_line(lean["summary"])
        if lean.get("status") in _CLOSING_STATUSES and "closure_class" not in lean:
            lean["closure_standard"] = "undeclared"
        if isinstance(lean.get("tags"), list):
            lean["tags"] = lean["tags"][:12]
        discovery_id = discovery.get("id") or discovery.get("discovery_id")
        for basis, score_map in quality_maps:
            if isinstance(score_map, dict) and discovery_id in score_map:
                lean["relevance"] = score_map[discovery_id]
                lean["relevance_basis"] = basis
                break
        else:
            # A lexical-only hit has no quality number anywhere in the payload.
            # Omitting `relevance` is the point: substituting the rank-derived
            # RRF value here is what made a miss indistinguishable from a hit.
            lean["relevance_basis"] = "lexical_rank_only"

        fusion = payload.get("rrf_scores")
        if isinstance(fusion, dict) and discovery_id in fusion:
            lean["fusion_score"] = fusion[discovery_id]
        lean_discoveries.append(lean)

    lean_payload = {
        key: payload[key]
        for key in (
            "success",
            "query",
            "count",
            "message",
            "search_mode_used",
            "low_confidence",
            "confidence_note",
            "resolution_notes_hint",
            "search_degraded",
            "search_degraded_message",
            "limit_clamped_from",
            "_more_available",
            # A queryless listing that left unpromoted imported memory out
            # says so, and how to read that lane; without these the lean
            # digest showed the remainder as the whole set (#2125).
            "imported_omitted",
            "imported_omitted_hint",
            "sort_by",
            "created_after",
            "created_before",
            "recency_half_life_days",
        )
        if payload.get(key) is not None
    }
    # The archived/cold second pass reports itself here too: its counts when
    # it added rows (the long note stays in full mode), or why the page holds
    # first-pass rows only. search_shared_memory reads this lean payload, so
    # without these its digest could not say either.
    archived_cold = payload.get("archived_cold_fallback")
    if isinstance(archived_cold, dict):
        lean_payload["archived_cold_fallback"] = {
            key: value for key, value in archived_cold.items() if key != "note"
        }
    if payload.get("archived_cold_fallback_error"):
        lean_payload["archived_cold_fallback_error"] = payload["archived_cold_fallback_error"]
    lean_payload["discoveries"] = lean_discoveries
    lean_payload["discovery_retrieval_options"] = {
        "current_tier": "lean_digest",
        "open_one": "knowledge(action='details', discovery_id='...')",
    }

    signature = payload.get("agent_signature")
    if isinstance(signature, dict) and signature.get("uuid"):
        assurance = _compact_identity_assurance(signature.get("identity_assurance"))
        lean_payload["caller"] = {"uuid": signature["uuid"], **assurance}
    return lean_payload


def _compact_knowledge_read_result(
    result: Sequence[TextContent],
    *,
    mode: str,
) -> Sequence[TextContent]:
    """Apply response-mode shaping to JSON text blocks, preserving other content."""
    compacted = []
    for item in result:
        if not isinstance(item, TextContent):
            compacted.append(item)
            continue
        try:
            payload = json.loads(item.text)
        except (TypeError, ValueError):
            compacted.append(item)
            continue
        if not isinstance(payload, dict):
            compacted.append(item)
            continue
        shaped = _compact_caller_identity_envelope(payload)
        if mode == "lean":
            shaped = _lean_search_payload(shaped)
        compacted.append(item.model_copy(update={
            "text": json.dumps(
                shaped,
                ensure_ascii=False,
            )
        }))
    return compacted


_STALE_AFTER_DAYS = 60


def _is_durable_entry(discovery) -> bool:
    """Whether ``open`` is this entry's resting status rather than "unresolved".

    Reuses the lifecycle's retention classification (the permanent types and
    the permanent/foundational/architecture/decision tags) instead of keeping
    a second list that can drift, plus ``insight``, which is resting-open but
    deliberately not a permanent type: adding it there would change retention.
    Known edge (operator decision, 2026-09-26): an open ``bug_found`` tagged
    ``architecture`` counts as durable here, because the tag makes it
    permanent for retention.
    """
    from src.knowledge_graph_lifecycle import KnowledgeGraphLifecycle

    kind = getattr(discovery, "type", None)
    if kind == "insight":
        return True
    # A read path: a malformed legacy tag must not make set() raise mid-search.
    tags = [tag for tag in (getattr(discovery, "tags", None) or []) if isinstance(tag, str)]
    view = SimpleNamespace(type=kind, tags=tags)
    return KnowledgeGraphLifecycle().get_lifecycle_policy(view) == "permanent"


def _compute_staleness(discovery) -> Optional[tuple[int, str]]:
    """``(days since the last write, warning)`` for an open entry past the
    staleness threshold, else ``None``.

    A non-open entry (``status`` in ``CLOSURE_CLASS_ADMITTING_STATUSES`` —
    every status except the two that reopen a row, ``open`` and ``disputed``:
    ``resolved``, ``closed``, ``wont_fix``, ``superseded``, plus the lifecycle
    retention states ``archived`` and ``cold`` a closed row is later tiered
    into) never gets this warning, regardless of how long ago it was last
    touched: closure is the reason a stale-looking timestamp is fine, and the
    "...and is still open" wording below would otherwise contradict the
    entry's own recorded status. Reuses ``CLOSURE_CLASS_ADMITTING_STATUSES``
    (the same open/closed partition ``apply_closure_reopen_rule`` and
    migration 071's check constraint key off, imported at module top) rather
    than a second list that can drift from it. A missing/unknown ``status``
    is treated as open (matching ``DiscoveryNode``'s own ``status`` default),
    not silently reclassified as closed.

    Keyed on the last write (``updated_at`` when newer than the store time),
    not the first store. Before 2026-08-16 the checks keyed on store-time
    facts, so long-lived entries that are actively maintained — e.g. the
    memory-mirror corpus, re-pushed on every source-file edit — warned forever,
    and a warning that always fires trains readers to ignore it (observed live:
    an entry updated 25 minutes earlier still said "written against v2.14.0").

    There is no version clause. Until 2026-09-26 a never-updated entry also
    warned when its store-time ``system_version`` was 2+ minor releases behind
    the server's. Both values come from the VERSION file, so that counted
    release cuts, not code change: it fired on entries 22-57 days old
    depending on when releases happened, and never on a leave_note, which
    records no version (operator decision D1, 2026-09-26). ``system_version``
    stays on the result as store-time provenance.

    The wording follows the entry's type (operator decision D2): for durable
    entries, where ``open`` is the resting status, "still open" would frame a
    record as a neglected task, so they get a neutral sentence that still says
    to verify — permanent rules carry volatile details too.

    ``updated_at`` is stamped by any field update (status, tags, severity), not
    only content rewrites — accepted imprecision: a lifecycle touch on an open
    entry is itself a recency signal, and content-hash timestamps are not worth
    the machinery.
    """

    if getattr(discovery, "status", None) in CLOSURE_CLASS_ADMITTING_STATUSES:
        return None

    def _parse_utc(value):
        ts = datetime.fromisoformat(value) if isinstance(value, str) else value
        if ts.tzinfo is None:
            # Legacy rows may have naive timestamps (treat as UTC);
            # post-2026-04-25 rows are UTC-aware.
            ts = ts.replace(tzinfo=timezone.utc)
        return ts

    # >60 days since the last write. Compare in UTC.
    was_updated = False
    try:
        last_write = _parse_utc(discovery.timestamp)
        try:
            if getattr(discovery, "updated_at", None):
                updated = _parse_utc(discovery.updated_at)
                if updated > last_write:
                    last_write = updated
                    was_updated = True
        except (ValueError, TypeError):
            pass
        age_days = (_h_attr("datetime", datetime).now(timezone.utc) - last_write).days
    except (ValueError, TypeError):
        return None
    if age_days <= _STALE_AFTER_DAYS:
        return None

    if _is_durable_entry(discovery):
        return age_days, (
            f"Last written {age_days} days ago; durable entries can carry "
            "volatile details (ports, paths, refs) — verify before acting on them."
        )
    if was_updated:
        sentence = f"This entry was last updated {age_days} days ago and is still open."
    else:
        sentence = f"This entry is {age_days} days old and still open."
    return age_days, sentence + " It may be outdated — verify before acting on it."


def _compute_staleness_warning(discovery) -> Optional[str]:
    """The canonical per-entry ``staleness_warning`` string, or ``None``."""
    staleness = _compute_staleness(discovery)
    return staleness[1] if staleness else None


class _SearchParameterError(ValueError):
    """Caller-visible validation error raised while parsing a KG search."""


@dataclass(frozen=True)
class _KnowledgeSearchRequest:
    arguments: Dict[str, Any]
    limit: int
    # None = caller expressed no preference; True/False = they did.
    include_details: Optional[bool]
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
    authority_mode: str
    # Set when the caller over-asked and the limit was clamped down — surfaced
    # in the response so truncation is distinguishable from "that was all".
    limit_clamped_from: Optional[int] = None
    # "relevance" or "created_at". created_at orders the query's full-text
    # matches newest first; see _run_newest_first_text_search.
    sort_by: str = "relevance"
    # Exclusive, timezone-aware bounds on created_at.
    created_after: Optional[datetime] = None
    created_before: Optional[datetime] = None
    # Opt-in recency weight on the relevance score (operator decision
    # 2026-09-27: per call, default off). None = no weighting.
    recency_half_life_days: Optional[float] = None
    # The caller's literal include_archived / include_cold: None when unsent.
    # An explicit false keeps that status out of the archived/cold second
    # pass; the plain booleans above cannot tell false from unsent.
    include_archived_explicit: Optional[bool] = None
    include_cold_explicit: Optional[bool] = None

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
    tag_filter_dropped: int = 0
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
    authority_reranked: bool = False
    # Unpromoted imported-memory rows the queryless listing skipped while
    # filling its page (see _omits_imported_lane).
    imported_omitted: int = 0
    # Archived/cold rows appended by the second pass (_apply_archived_cold_tier).
    archived_cold_ids: set[str] = field(default_factory=set)
    archived_cold_primary_count: int = 0
    archived_cold_statuses: list[str] = field(default_factory=list)
    archived_cold_error: Optional[str] = None
    # time.monotonic() at which the handler's timeout fires; None when the
    # search runs outside handle_search_knowledge_graph (no budget to keep).
    deadline: Optional[float] = None


def _optional_flag(value: Any) -> Optional[bool]:
    """Tri-state flag: None when the caller said nothing, else a real bool.

    Collapsing "unspecified" and "explicitly false" into `False` is what made
    `include_details=false` unhonorable — the auto-include heuristic could not
    tell a caller who wanted summaries from one who had no opinion, so it
    overrode both.

    MCP admits these as strings, so `"false"` must not become `True` the way a
    bare `bool("false")` would.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in ("true", "1", "yes"):
            return True
        if token in ("false", "0", "no"):
            return False
        return None
    return bool(value)


AUTO_DETAIL_MAX_RESULTS = 3


def _resolve_detail_inclusion(
    requested: Optional[bool], result_count: int
) -> tuple[bool, bool]:
    """Decide ``(auto_details, include_details)`` for one search response.

    Auto-include is a convenience for a caller with no opinion, NOT an
    override of one. The old expression was ``not request.include_details``,
    which was True for "unspecified" and for an explicit ``false`` alike, so
    there was no way to ask for summaries of a small result set. A dogfood
    probe measured the cost of that: 3 results, 44,205 bytes, into the
    caller's context after they had asked for none of it.

    Extracted so the contract is testable directly. A test that re-implements
    this arithmetic would pass against a broken caller.
    """
    auto = requested is None and 0 < result_count <= AUTO_DETAIL_MAX_RESULTS
    return auto, bool(requested) or auto


def _parse_search_timestamp(name: str, value: Any) -> Optional[datetime]:
    """Parse a created_after/created_before bound into an aware UTC datetime.

    A value without an offset is read as UTC, the zone every discovery id and
    created_at is written in. An unparseable value is refused rather than
    dropped: a window the caller wrote must not silently become no window.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise _SearchParameterError(
                f"{name} {value!r} is not an ISO 8601 timestamp; "
                "pass e.g. '2026-09-26T00:00:00Z' or '2026-09-26'."
            ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


RECENCY_HALF_LIFE_MAX_DAYS = 36500.0


def _parse_half_life(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        days = float(value)
    except (TypeError, ValueError):
        raise _SearchParameterError(
            f"recency_half_life_days {value!r} is not a number of days."
        ) from None
    if not (0 < days <= RECENCY_HALF_LIFE_MAX_DAYS):
        raise _SearchParameterError(
            f"recency_half_life_days must be above 0 and at most "
            f"{RECENCY_HALF_LIFE_MAX_DAYS:g}; got {value!r}."
        )
    return days


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


def _fts_kwargs(request: _KnowledgeSearchRequest) -> dict[str, Any]:
    kwargs = {**_tag_kwargs(request), **_window_kwargs(request)}
    if request.sort_by == "created_at":
        kwargs["order_by"] = "created_at"
        # The post-LIMIT filter stays, but under time order it would leave a
        # page of newer ineligible rows and nothing to return.
        kwargs["filters"] = {
            "agent_id": request.agent_id,
            "type": request.discovery_type,
            "severity": request.severity,
            "status": request.status,
            "exclude_archived": not request.include_archived,
            "exclude_cold": not request.include_cold,
        }
    return kwargs


def _fts_page_size(state: _KnowledgeSearchState) -> int:
    request = state.request
    base_limit = int(min(max(request.limit * 5, request.limit), 500))
    return max(
        base_limit,
        state.rerank_pool_size if state.rerank_on else 0,
        _authority_pool_size(request),
    )


async def _retrieve_fts_candidates(state: _KnowledgeSearchState) -> None:
    request = state.request
    candidate_limit = _fts_page_size(state)
    primary_operator = request.operator_forced or "AND"
    fts_kwargs = _fts_kwargs(request)
    state.candidates = await state.graph.full_text_search(
        str(request.query_text),
        limit=candidate_limit,
        operator=primary_operator,
        **fts_kwargs,
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
                **fts_kwargs,
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
    if request.tags and not _matches_tags(document, request.tags):
        return False
    return _within_window(document, request)


def _document_created_at(document: Any) -> Optional[datetime]:
    raw = getattr(document, "timestamp", None) or getattr(document, "created_at", None)
    if isinstance(raw, datetime):
        parsed = raw
    else:
        try:
            parsed = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _within_window(document: Any, request: _KnowledgeSearchRequest) -> bool:
    """Apply created_after/created_before to a candidate already in hand.

    The SQL paths filter inside the query; semantic retrieval cannot, so its
    candidates are held to the window here. A row whose creation time cannot
    be read is outside any window: it cannot be shown to be inside one.
    """
    if not (request.created_after or request.created_before):
        return True
    created = _document_created_at(document)
    if created is None:
        return False
    if request.created_after and created <= request.created_after:
        return False
    if request.created_before and created >= request.created_before:
        return False
    return True


def _window_kwargs(request: _KnowledgeSearchRequest) -> dict[str, Any]:
    # Sent only when a window was asked for, so an unwindowed search makes
    # exactly the backend call it made before.
    kwargs: dict[str, Any] = {}
    if request.created_after:
        kwargs["created_after"] = request.created_after
    if request.created_before:
        kwargs["created_before"] = request.created_before
    return kwargs


def _matches_tags(document: Any, tags: list[str]) -> bool:
    stored = set(document.tags or [])
    return any(tag in stored for tag in tags)


def _tag_kwargs(request: _KnowledgeSearchRequest) -> dict[str, Any]:
    # The predicate rides inside the backend's ranked query, so a tagged row
    # that ranks below the first-stage limit is still retrieved. Untagged
    # searches send nothing and their backend calls are unchanged.
    return {"tags": request.tags} if request.tags else {}


def _authority_ranking_enabled(request: _KnowledgeSearchRequest) -> bool:
    """Prefer governed knowledge unless the caller explicitly asks for raw order.

    A source-tag query is already an explicit request to inspect the imported
    lane, so applying its default penalty there would only distort that lane's
    own relevance order. Only a filter made entirely of source-memory tags
    counts: tags match any-of, so a mixed filter also returns ordinary
    findings. A `channel-*` filter is honoured per row, in rank_by_authority,
    because a channel- tag can also be an ordinary topic.
    """
    if request.authority_mode == "all":
        return False
    if request.sort_by == "created_at":
        # The caller asked for time order; an authority nudge would reorder it.
        return False
    return not is_lane_filter(request.tags)


AUTHORITY_POOL_SIZE = 50


def _authority_pool_size(request: _KnowledgeSearchRequest) -> int:
    # A recency weight reorders the same way authority does, so it needs the
    # same pool: a fresh row just below the first page must be retrieved to
    # be lifted.
    if _authority_ranking_enabled(request) or request.recency_half_life_days:
        return AUTHORITY_POOL_SIZE
    return 0


def _authority_score_map(state: _KnowledgeSearchState) -> dict[str, float]:
    if state.rerank_scores:
        return state.rerank_scores
    if state.rrf_scores:
        return state.rrf_scores
    if state.semantic_scores:
        return state.semantic_scores
    return {}


def _recency_weighted(
    documents: list[Any],
    scores: dict[str, float],
    half_life_days: float,
) -> tuple[list[Any], dict[str, float]]:
    """Multiply each row's relevance by 0.5 ** (age / half-life) and re-sort.

    A true half-life: a row half_life_days old keeps half its score. (The AGE
    backend's semantic blend uses 1 / (1 + age / 90), which is 0.5 at 90
    days but decays more slowly after that.) The base score is the one
    authority ranking reads: rerank, then RRF, then semantic similarity,
    then a full-text row's own rank, then position. RRF is rank-derived and
    nearly flat, so on a hybrid search the weight dominates the order. A
    row whose creation time cannot be read is left unweighted.
    """
    now = _h_attr("datetime", datetime).now(timezone.utc)
    weighted: dict[str, float] = {}
    for index, document in enumerate(documents):
        document_id = str(getattr(document, "id", ""))
        base = scores.get(document_id)
        if not isinstance(base, (int, float)):
            base = getattr(document, "relevance", None)
        if not isinstance(base, (int, float)):
            base = 1.0 / (1.0 + index * 0.05)
        created = _document_created_at(document)
        decay = 1.0
        if created is not None:
            age_days = max(0.0, (now - created).total_seconds() / 86400.0)
            decay = 0.5 ** (age_days / half_life_days)
        weighted[document_id] = float(base) * decay
    ordered = sorted(
        enumerate(documents),
        key=lambda item: (-weighted[str(getattr(item[1], "id", ""))], item[0]),
    )
    return [document for _, document in ordered], weighted


def _rank_search_documents(
    state: _KnowledgeSearchState,
    documents: list[Any],
) -> list[Any]:
    scores = _authority_score_map(state)
    half_life = state.request.recency_half_life_days
    if half_life:
        documents, scores = _recency_weighted(documents, scores, half_life)
    ranked, changed = rank_by_authority(
        documents,
        relevance_scores=scores,
        enabled=_authority_ranking_enabled(state.request),
        read_lanes=channel_lanes(state.request.tags),
    )
    state.authority_reranked = state.authority_reranked or changed
    return ranked


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
    substring_terms = (
        str(request.query_text).lower().split()
        if state.search_mode in ("substring_scan", "substring_newest_first")
        else None
    )
    # Authority ranking has to see more than the page it reorders: capped at
    # `limit`, a native finding just below a page of down-ranked rows was
    # dropped before its multiplier could lift it.
    if state.rerank_on:
        filter_cap = state.rerank_pool_size
    elif state.hybrid_on or _authority_pool_size(request):
        filter_cap = max(request.limit, AUTHORITY_POOL_SIZE)
    else:
        filter_cap = request.limit
    filtered = []
    for document in state.candidates:
        if request.tags and not _matches_tags(document, request.tags):
            state.tag_filter_dropped += 1
            continue
        if _candidate_matches_search(document, state, substring_terms) and not _label_excluded(
            document, request
        ):
            filtered.append(document)
            if len(filtered) >= filter_cap:
                break

    if not (state.rerank_on and filtered):
        state.results = _rank_search_documents(state, filtered)[: request.limit]
        return

    try:
        from src.reranker import rerank as _rerank

        pairs = [(document.id, f"{document.summary}\n{(document.details or '')[:2000]}") for document in filtered]
        reranked = await _rerank(
            str(request.query_text),
            pairs,
            top_k=len(filtered),
            max_rerank_size=state.rerank_pool_size,
        )
        state.rerank_scores = dict(reranked)
        by_id = {document.id: document for document in filtered}
        reranked_documents = [
            by_id[discovery_id]
            for discovery_id, _ in reranked
            if discovery_id in by_id
        ]
        state.results = _rank_search_documents(state, reranked_documents)[: request.limit]
        state.search_mode = f"{state.search_mode}_reranked" if state.search_mode else "reranked"
    except Exception as exc:
        logger.warning(
            "[KG_SEARCH] reranker failed; keeping first-stage order: %s",
            exc,
        )
        state.results = _rank_search_documents(state, filtered)[: request.limit]


async def _run_newest_first_text_search(
    state: _KnowledgeSearchState, *, has_fts: bool
) -> None:
    """sort_by=created_at: the query's full-text matches, newest first.

    The tsquery is the membership test and the database does the ordering, so
    a match written a minute ago comes first however weakly it ranks.
    Re-sorting a relevance page instead would only reorder rows that already
    ranked in, which is the failure this exists for. No reranker, no
    semantic leg, no authority reorder: each would put relevance back.
    """
    state.rerank_on = False
    state.hybrid_on = False
    if has_fts:
        await _retrieve_fts_candidates(state)
        state.search_mode = "fts_newest_first"
    else:
        # query() is already newest first; the substring filter runs below.
        await _retrieve_substring_candidates(state)
        state.search_mode = "substring_newest_first"
    await _filter_and_rerank_candidates(state)
    if has_fts:
        await _continue_newest_first_pages(state)
    state.operator_used = state.fts_operator_used or "N/A"
    state.fields_searched = ["summary", "details", "tags"]


NEWEST_FIRST_SCAN_CEILING = 500


async def _continue_newest_first_pages(state: _KnowledgeSearchState) -> None:
    """Read older pages while filters the SQL cannot express leave the page short.

    Everything the database can filter already sits in the ordered query.
    Writer-label exclusion cannot (a label is resolved from provenance or
    agent metadata), so a run of newer matches from excluded writers can
    fill a whole page. Keyset continuation reads the next older page from
    the oldest row seen, doubling the page each time, until the limit fills,
    the matches run out, or NEWEST_FIRST_SCAN_CEILING rows have been read.
    """
    request = state.request
    page_size = _fts_page_size(state)
    page = state.candidates
    pool = list(page)
    while len(pool) < NEWEST_FIRST_SCAN_CEILING:
        if len(state.results) >= request.limit or len(page) < page_size:
            return
        page_size = min(page_size * 2, NEWEST_FIRST_SCAN_CEILING - len(pool))
        oldest = _document_created_at(page[-1])
        if oldest is None:
            return
        kwargs = _fts_kwargs(request)
        kwargs["before"] = (oldest, page[-1].id)
        page = await state.graph.full_text_search(
            str(request.query_text),
            limit=page_size,
            operator=state.fts_operator_used or request.operator_forced or "AND",
            **kwargs,
        )
        if not page:
            return
        if state.fts_anchor_ids is not None:
            state.fts_anchor_ids.update(document.id for document in page)
        pool.extend(page)
        state.candidates = pool
        state.tag_filter_dropped = 0
        await _filter_and_rerank_candidates(state)


async def _run_indexed_filter_search(state: _KnowledgeSearchState) -> None:
    request = state.request

    async def _read(limit: int) -> list[Any]:
        return await state.graph.query(
            agent_id=request.agent_id,
            tags=request.tags,
            type=request.discovery_type,
            severity=request.severity,
            status=request.status,
            limit=limit,
            exclude_archived=not request.status and not request.include_archived,
            exclude_cold=not request.status and not request.include_cold,
            **_window_kwargs(request),
        )

    omit_imported = _omits_imported_lane(request)
    if not request.exclude_labels and not omit_imported:
        state.results = await _read(request.limit)
    else:
        # Writer-label and imported-lane exclusion cannot run in the query, so
        # reading exactly `limit` rows let excluded rows take the page. Re-read
        # with a doubling limit until the visible page fills, the rows run
        # out, or the same row ceiling newest-first continuation uses is
        # reached. Imports are the uncommon case on most graphs, so that
        # exclusion alone starts at the page size and widens only when it
        # actually dropped something.
        fetch_limit = (
            min(request.limit * 5, NEWEST_FIRST_SCAN_CEILING)
            if request.exclude_labels
            else request.limit
        )
        while True:
            rows = await _read(fetch_limit)
            visible = [
                document for document in rows if not _label_excluded(document, request)
            ]
            if omit_imported:
                kept = [
                    document for document in visible if not is_imported_context(document)
                ]
                state.imported_omitted = len(visible) - len(kept)
                visible = kept
            state.results = visible
            if (
                len(state.results) >= request.limit
                or len(rows) < fetch_limit
                or fetch_limit >= NEWEST_FIRST_SCAN_CEILING
            ):
                break
            fetch_limit = min(fetch_limit * 2, NEWEST_FIRST_SCAN_CEILING)
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
        fallback_limit = max(request.limit * 2, _authority_pool_size(request))
        candidates = await state.graph.full_text_search(
            str(request.query_text),
            limit=fallback_limit,
            operator=primary_operator,
            **_tag_kwargs(request),
            **_window_kwargs(request),
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
                    limit=fallback_limit,
                    operator="OR",
                    **_tag_kwargs(request),
                    **_window_kwargs(request),
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
            if request.tags and not _matches_tags(document, request.tags):
                state.tag_filter_dropped += 1
                continue
            if not _candidate_matches_semantic_fallback(document, request):
                continue
            if _label_excluded(document, request):
                continue
            state.results.append(document)
            # The executor ranks and then cuts the page, so collect the
            # whole authority pool here, not just `limit`.
            if len(state.results) >= max(request.limit, _authority_pool_size(request)):
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


ARCHIVED_COLD_MIN_RESULTS = 3


ARCHIVED_COLD_SCORE_PENALTY = 0.5


ARCHIVED_COLD_TIER = "archived_cold_fallback"


KNOWLEDGE_SEARCH_TIMEOUT_S = 15.0


ARCHIVED_COLD_RESPONSE_RESERVE_S = 3.0


_ABANDONED_ARCHIVED_COLD_PASSES: set[asyncio.Task] = set()


SEARCH_SYNTHESIS_MAX_TIMEOUT_S = 30.0


SEARCH_SYNTHESIS_RESERVE_S = 1.0


SEARCH_SYNTHESIS_MIN_TIMEOUT_S = 2.0


SUPERSESSION_LOOKUP_RESERVE_S = 1.0


def _archived_cold_widening(request: _KnowledgeSearchRequest) -> Optional[dict[str, bool]]:
    """The include flags a second pass would turn on, or None when it should not run.

    Query searches only: a queryless listing is a newest-first poll of a known
    lane, and padding it with stale rows would hand a poller old work as new.
    An explicit status filter already says which statuses the caller wants,
    and an explicit include_archived=false / include_cold=false keeps that
    status out.
    """
    if not request.query_text or request.status:
        return None
    widen_archived = not request.include_archived and request.include_archived_explicit is not False
    widen_cold = not request.include_cold and request.include_cold_explicit is not False
    if not (widen_archived or widen_cold):
        return None
    return {
        "include_archived": request.include_archived or widen_archived,
        "include_cold": request.include_cold or widen_cold,
    }


def _settle_abandoned_pass(task: asyncio.Task) -> None:
    _ABANDONED_ARCHIVED_COLD_PASSES.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.debug("[KG_SEARCH] abandoned archived/cold pass ended with: %s", task.exception())


async def _bounded_supersession_lookup(
    graph, superseded_ids: list, deadline: float,
) -> Dict[str, list]:
    """``get_superseded_by`` within what is left of the search budget.

    An empty mapping when no time is left or the lookup overruns; the overrun
    task is stopped like an abandoned second pass. Cancelling the request
    cancels the lookup too.
    """
    budget = deadline - _h_attr("SUPERSESSION_LOOKUP_RESERVE_S", SUPERSESSION_LOOKUP_RESERVE_S) - time.monotonic()
    if budget <= 0:
        logger.info("[KG_SEARCH] supersession lookup skipped: search budget spent")
        return {}
    task = asyncio.create_task(graph.get_superseded_by(superseded_ids))
    try:
        done, _ = await asyncio.wait({task}, timeout=budget)
    except asyncio.CancelledError:
        _abandon_archived_cold_pass(task)
        raise
    if not done:
        _abandon_archived_cold_pass(task)
        logger.warning(
            "[KG_SEARCH] supersession lookup did not finish within %.1fs; "
            "returning superseded rows without superseded_by", budget,
        )
        return {}
    return task.result()


def _abandon_archived_cold_pass(task: asyncio.Task) -> None:
    """Stop a second pass the handler no longer waits for.

    The cancel stops later retrieval stages from starting. A statement already
    running on the ExecutorPool loop may still finish there (CPython #105836);
    nothing reads its result.
    """
    task.cancel()
    _ABANDONED_ARCHIVED_COLD_PASSES.add(task)
    task.add_done_callback(_settle_abandoned_pass)


async def _apply_archived_cold_tier(
    state: _KnowledgeSearchState,
    *,
    first_pass_s: float = 0.0,
) -> None:
    """Fill a short page from archived and cold rows, ranked below every active row.

    Re-runs the same retrieval with the widened status filter, so ranking,
    tag, window, label and authority rules are the ones the first pass used.
    Only rows the first pass hid by status are added.

    The pass is optional, so it never costs the caller the first page. With a
    deadline it is skipped when what remains (less the response reserve) is
    shorter than the first pass took, and otherwise the handler stops waiting
    for it when that remainder runs out. Either way the first page comes back
    with archived_cold_error saying why. The pass runs as its own task so the
    handler can stop waiting without a timeout wrapper around the retrieval;
    cancellation of the whole request still propagates.
    """
    request = state.request
    threshold = min(ARCHIVED_COLD_MIN_RESULTS, request.limit)
    if len(state.results) >= threshold:
        return
    widening = _archived_cold_widening(request)
    if widening is None:
        return

    budget: Optional[float] = None
    if state.deadline is not None:
        budget = state.deadline - _h_attr("ARCHIVED_COLD_RESPONSE_RESERVE_S", ARCHIVED_COLD_RESPONSE_RESERVE_S) - time.monotonic()
        if budget <= 0 or budget < first_pass_s:
            state.archived_cold_error = (
                f"second pass skipped: {max(budget, 0.0):.1f}s of the "
                f"{_h_attr("KNOWLEDGE_SEARCH_TIMEOUT_S", KNOWLEDGE_SEARCH_TIMEOUT_S):g}s search budget was left after a "
                f"{first_pass_s:.1f}s first pass; returning the first page only"
            )
            logger.info("[KG_SEARCH] archived/cold %s", state.archived_cold_error)
            return

    second = _KnowledgeSearchState(
        request=replace(request, **widening),
        graph=state.graph,
    )
    task = asyncio.create_task(_run_archived_cold_pass(second, request.limit))
    try:
        done, _ = await asyncio.wait({task}, timeout=budget)
    except asyncio.CancelledError:
        # The request itself was cancelled: take the pass down with it.
        _abandon_archived_cold_pass(task)
        raise
    if not done:
        _abandon_archived_cold_pass(task)
        state.archived_cold_error = (
            f"second pass did not finish within the {budget:.1f}s left of the "
            f"{_h_attr("KNOWLEDGE_SEARCH_TIMEOUT_S", KNOWLEDGE_SEARCH_TIMEOUT_S):g}s search budget; returning the first page only"
        )
        logger.warning("[KG_SEARCH] archived/cold %s", state.archived_cold_error)
        return
    if task.cancelled():
        state.archived_cold_error = "second pass was cancelled; returning the first page only"
        return
    exc = task.exception()
    if exc is not None:
        if not isinstance(exc, Exception):
            raise exc
        logger.warning("[KG_SEARCH] archived/cold second pass failed: %s", exc)
        state.archived_cold_error = str(exc)
        return

    seen = {document.id for document in state.results}
    room = request.limit - len(state.results)
    added = [
        document
        for document in second.results
        if document.id not in seen and not _candidate_status_visible(document, request)
    ][:room]
    if not added:
        return

    state.archived_cold_primary_count = len(state.results)
    state.archived_cold_statuses = [
        status
        for status, widened in (
            ("archived", widening["include_archived"] and not request.include_archived),
            ("cold", widening["include_cold"] and not request.include_cold),
        )
        if widened
    ]
    for document in added:
        state.archived_cold_ids.add(document.id)
        for source, target in (
            (second.semantic_scores, state.semantic_scores),
            (second.rerank_scores, state.rerank_scores),
            (second.rrf_scores, state.rrf_scores),
        ):
            if document.id in source:
                target[document.id] = source[document.id] * ARCHIVED_COLD_SCORE_PENALTY
    if state.fts_anchor_ids is not None and second.fts_anchor_ids:
        state.fts_anchor_ids.update(
            document.id for document in added if document.id in second.fts_anchor_ids
        )
    state.results = list(state.results) + added
    if not state.search_mode:
        state.search_mode = second.search_mode


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
    # The same status predicate as the main path: this one used to check
    # archived only, so a cold row could come back from a default search.
    if not _candidate_status_visible(document, request):
        return False
    if request.tags and not any(tag in set(document.tags or []) for tag in request.tags):
        return False
    return _within_window(document, request)


def _omits_imported_lane(request: _KnowledgeSearchRequest) -> bool:
    """Whether a queryless listing leaves unpromoted imported memory out.

    With no query there is no relevance contest for the authority multiplier
    to decide, so ranking only reshuffles a page imports have already filled,
    and open imports read as open work to every sweep. The listing omits them
    and reports the count instead. A caller still reads the lane by asking
    for it: authority_mode='all', or a tag filter made only of
    imported-memory tags (the same lane test ranking honours).
    """
    if request.authority_mode == "all":
        return False
    return not is_lane_filter(request.tags)


def _label_excluded(document: Any, request: _KnowledgeSearchRequest) -> bool:
    """True when the writer's display label is one the caller excluded.

    Checked while candidates are collected, not only after the page is cut:
    otherwise an excluded writer's row can take a slot under the limit and
    leave the page short, or empty at limit=1 under newest-first order.
    """
    if not request.exclude_labels:
        return False
    display = _resolve_agent_display(document.agent_id)
    display_name = display.get("display_name", document.agent_id) or ""
    return str(display_name).strip().lower() in request.exclude_labels


def _exclude_search_labels(state: _KnowledgeSearchState) -> None:
    if not state.request.exclude_labels:
        return
    state.results = [
        document for document in state.results if not _label_excluded(document, state.request)
    ]


def _serialize_search_discoveries(
    state: _KnowledgeSearchState,
    *,
    include_details: bool,
) -> list[dict[str, Any]]:
    discoveries = []
    for document in state.results:
        provenance = document.provenance if isinstance(document.provenance, dict) else None
        display_name = (provenance or {}).get("writer_label_at_write")
        if not display_name:
            display = _resolve_agent_display(document.agent_id)
            display_name = display.get("display_name", document.agent_id)

        # Summary-only searches are lists of leads. Expanded results promise
        # full inline content, so keep their original summary as well.
        summary = document.summary if include_details else _one_line(document.summary, limit=200)
        item = {"by": display_name, "summary": summary}
        item["authority"] = assess_authority(document).to_dict()
        session_at_write = (provenance or {}).get("writer_session_id_at_write")
        if session_at_write:
            item["session_id_at_write"] = audit_reference(session_at_write)

        serialized = document.to_dict(include_details=include_details)
        # Summary-mode serialization already computes lifecycle metadata and a
        # bounded details preview. Keep those fields on the wire instead of
        # collapsing the result to id/type/status/tags: the intermediate digest
        # tier should let a caller choose one record to open without first
        # expanding every discovery inline.
        for key in (
            "id",
            "type",
            "status",
            "severity",
            "tags",
            "created_at",
            "updated_at",
            "superseded_by",
            "closure_class",
            "has_details",
            "details_preview",
            "details_length",
            "has_more_details",
        ):
            value = serialized.get(key)
            if value is not None:
                item[key] = value
        if include_details and serialized.get("details"):
            item["details"] = serialized.get("details")
        # The evidence rides with an expanded result only, as details do.
        if include_details and serialized.get("closure_evidence") is not None:
            item["closure_evidence"] = serialized.get("closure_evidence")
        item["_agent_id"] = document.agent_id
        item["system_version"] = provenance.get("system_version") if provenance else None
        if document.status == "open":
            staleness = _compute_staleness(document)
            if staleness:
                # The prose is the canonical field (the dashboard reads it);
                # `last_activity_days` is the same fact in structured form,
                # present only when the warning is, so a digest can carry it
                # without the sentence. The name is the one
                # knowledge(action='audit') already gives days since the last
                # write; its `age_days` counts from creation instead.
                item["last_activity_days"], item["staleness_warning"] = staleness
        if state.request.include_provenance:
            item["provenance"] = redact_provenance(document.provenance)
            if document.provenance_chain:
                item["provenance_chain"] = document.provenance_chain
        annotate_resolution_notes(item, document)
        annotate_imported_read(item, document)
        if document.id in state.archived_cold_ids:
            item["retrieval_tier"] = ARCHIVED_COLD_TIER
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
    response = {
        "search_mode_used": state.search_mode,
        "search_mode_requested": request.search_mode_requested,
        "operator_used": state.operator_used,
        "fields_searched": state.fields_searched,
        "query": request.query_text,
        "discoveries": discoveries,
        "count": len(state.results),
        "message": f"Found {len(state.results)} discovery(ies){detail_suffix}",
        **_search_order_echo(request),
    }
    add_resolution_notes_hint(response)
    return response


def _search_order_echo(request: _KnowledgeSearchRequest) -> dict[str, Any]:
    """Say how the list is ordered and windowed, only when it differs from default."""
    echo: dict[str, Any] = {}
    if request.sort_by != "relevance":
        echo["sort_by"] = request.sort_by
    if request.created_after:
        echo["created_after"] = request.created_after.isoformat()
    if request.created_before:
        echo["created_before"] = request.created_before.isoformat()
    if request.recency_half_life_days:
        echo["recency_half_life_days"] = request.recency_half_life_days
    return echo


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
    if state.tag_filter_dropped:
        response["tag_filter_dropped"] = state.tag_filter_dropped
    if state.imported_omitted:
        response["imported_omitted"] = state.imported_omitted
        response["imported_omitted_hint"] = (
            "This listing left out unpromoted imported-memory rows. Pass "
            "authority_mode='all', or filter on an imported-memory tag, to "
            "read them; a query search still returns them, down-ranked."
        )
    if state.fallback_used:
        response["fallback_used"] = True
        response["fallback_message"] = state.fallback_explanation or (
            "No exact matches found. Retried with individual terms (OR operator)."
        )
        response["fallback_terms"] = str(state.request.query_text).split()[:3] if state.request.query_text else []
    if state.archived_cold_ids:
        statuses = " and ".join(state.archived_cold_statuses)
        response[ARCHIVED_COLD_TIER] = {
            "used": True,
            "primary_count": state.archived_cold_primary_count,
            "added": len(state.archived_cold_ids),
            "statuses_searched": state.archived_cold_statuses,
            "threshold": ARCHIVED_COLD_MIN_RESULTS,
            "score_penalty": ARCHIVED_COLD_SCORE_PENALTY,
            "note": (
                f"Only {state.archived_cold_primary_count} active result(s) matched, "
                f"so a second pass searched {statuses} rows. Those rows come after "
                f"every active result, carry retrieval_tier='{ARCHIVED_COLD_TIER}', "
                f"and their scores are multiplied by {ARCHIVED_COLD_SCORE_PENALTY}. "
                "Check status and superseded_by before relying on one. Pass "
                "include_archived=false or include_cold=false to leave that status out."
            ),
        }
    elif state.archived_cold_error:
        response[f"{ARCHIVED_COLD_TIER}_error"] = state.archived_cold_error

    authority_counts: dict[str, int] = {}
    for document in state.results:
        tier = assess_authority(document).tier
        authority_counts[tier] = authority_counts.get(tier, 0) + 1
    if authority_counts and (
        IMPORTED_CONTEXT in authority_counts
        or CHANNEL_MESSAGE in authority_counts
        or GOVERNED_CLAIM in authority_counts
    ):
        response["authority_policy"] = {
            "mode": state.request.authority_mode,
            "reranked": state.authority_reranked,
            "result_tiers": authority_counts,
            "note": (
                "Authority affects close ranking contests; it is provenance-aware "
                "retrieval, not a truth verdict. Filter by source or channel tags "
                "or pass authority_mode='all' to inspect raw relevance order."
            ),
        }


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
                "tags_present": bool(request.tags),
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


def _verbatim_probe_terms(terms: list[str]) -> list[str]:
    """Query terms whose literal presence in a result is meaningful.

    Drops websearch control tokens: bare boolean operators, and negated terms,
    where a leading '-' asks for ABSENCE so demanding the literal would invert
    the caller's intent. Phrase quotes are stripped so a quoted phrase is
    probed as its own text.
    """
    probes: list[str] = []
    for term in terms:
        if term in ("OR", "AND") or term.startswith("-"):
            continue
        cleaned = term.strip('"').lower()
        if cleaned:
            probes.append(cleaned)
    return probes


def _contains_all_terms(document: Any, probes: list[str]) -> bool:
    """True when every probe term appears verbatim in the document's own text."""
    if not probes:
        return False
    haystack = " ".join(
        (
            getattr(document, "summary", "") or "",
            getattr(document, "details", "") or "",
            " ".join(getattr(document, "tags", None) or []),
        )
    ).lower()
    return all(probe in haystack for probe in probes)


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

    # An unanchored FTS arm is a fact about the index, not about the results.
    # PostgreSQL's parser emits some spans as one undecomposed lexeme -- paths,
    # urls, emails -- so a result can carry the caller's terms verbatim and
    # still anchor nothing. Issue #1711: this note fired on a rank-1 hit whose
    # summary contained the query string exactly, and told the caller to treat
    # it as "exploratory rather than authoritative". Read the text before
    # making a claim about the text.
    probes = _verbatim_probe_terms(request.query_terms)
    verbatim = [document for document in state.results if _contains_all_terms(document, probes)]
    if verbatim:
        record_recall_event(
            LEXICAL_INDEX_MISS,
            request.query_text,
            query_terms=request.query_term_count,
            search_mode=state.search_mode,
            detail={"verbatim_results": len(verbatim), "returned": len(state.results)},
        )
        response["lexical_index_note"] = (
            f"{len(verbatim)} of {len(state.results)} results contain every query "
            "term verbatim but were not anchored by the full-text index, which "
            "indexes some spans (paths, urls, emails) whole. Those results are "
            "literal matches; rank them accordingly."
        )
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
    auto_details: bool,
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
    response["discovery_retrieval_options"] = {
        "current_tier": (
            "full_inline_auto"
            if auto_details
            else "full_inline"
            if include_details
            else "digest"
        ),
        "digest": "include_details=false",
        "open_one": "knowledge(action='details', discovery_id='...')",
        "all_inline": "include_details=true (can be large for multi-result searches)",
    }
    if not include_details:
        response["_tip"] = (
            "Results include bounded previews. Open one with knowledge(action='details', "
            "discovery_id='...'); use include_details=true only when every result "
            "is needed inline."
        )
    if request.limit_clamped_from is not None:
        # A clamped caller must be able to tell truncation from "that was
        # all" — and must not be advised to raise a limit that is already
        # at the ceiling.
        response["limit_clamped_from"] = request.limit_clamped_from
        if len(state.results) == request.limit:
            response["_more_available"] = (
                f"You asked for {request.limit_clamped_from} results; the "
                f"server maximum is {request.limit}, so results were "
                f"truncated there. Narrow the query to see the rest."
            )
    elif len(state.results) == request.limit:
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
    # The LLM call gets what retrieval left of the handler's budget, less a
    # reserve for the rest of the response. Without this a slow model ran
    # into the handler timeout and the caller lost the results it had: the
    # archived/cold second pass makes that likelier, since filling a short
    # page to 3 rows is what turns synthesis on.
    llm_timeout = SEARCH_SYNTHESIS_MAX_TIMEOUT_S
    if state.deadline is not None:
        llm_timeout = min(
            llm_timeout,
            state.deadline
            - time.monotonic()
            - SEARCH_SYNTHESIS_RESERVE_S
            - LOCAL_LLM_WAIT_BUFFER_S,
        )
        if llm_timeout < SEARCH_SYNTHESIS_MIN_TIMEOUT_S:
            response["_synthesis_note"] = (
                "Synthesis skipped: too little of the "
                f"{_h_attr("KNOWLEDGE_SEARCH_TIMEOUT_S", KNOWLEDGE_SEARCH_TIMEOUT_S):g}s search budget left after retrieval"
            )
            return
    try:
        synthesis = await synthesize_results(
            discoveries=discoveries,
            query=state.request.query_text,
            max_discoveries=10,
            max_tokens=400,
            timeout=llm_timeout,
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


