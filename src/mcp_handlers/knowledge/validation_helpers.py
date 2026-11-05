"""Validation, store/update/batch/note request structures, and closure-class helpers for knowledge handlers.

Extracted from ``knowledge/handlers.py`` to keep write validation, bounds checking,
closure-class verification, and request models modular.
"""

from dataclasses import dataclass, field, replace
from typing import Dict, Any, Sequence, Optional
from mcp.types import TextContent
from datetime import datetime, timezone, timedelta
import json
import os
import re
import sys

from ..utils import success_response, error_response
from ..identity.stable_session import audit_reference, redact_provenance
from src.knowledge_graph import (
    get_knowledge_graph,
    DiscoveryNode,
    ResponseTo,
    normalize_tags,
    normalize_response_type,
    VALID_RESPONSE_TYPES,
    VALID_DISCOVERY_STATUSES,
    VALID_SEVERITIES as _SHARED_VALID_SEVERITIES,
    CLOSURE_CLASS_ADMITTING_STATUSES,
    CLOSURE_CLASS_CLEARING_STATUSES,
    closure_evidence_to_json,
)
from src.knowledge_authority import (
    CHANNEL_MESSAGE,
    GOVERNED_CLAIM,
    IMPORTED_CONTEXT,
    PROMOTION_SCHEMA,
    PROMOTION_TAG,
    assess_authority,
)
from src.mcp_handlers.knowledge.limits import (
    MAX_CLOSURE_EVIDENCE_BYTES,
    MAX_DETAILS_LEN,
    MAX_DISCOVERY_METADATA_LEN,
    MAX_SUMMARY_LEN,
    MAX_TAG_LEN,
    MAX_TAGS,
    MAX_UPDATED_DETAILS_LEN,
    MAX_UPDATED_SUMMARY_LEN,
    SUMMARY_TRUNCATION_MARKER,
)
from src.coherence_provenance import (
    BEHAVIORAL_UPDATE_CONSISTENCY_ROLE,
    coherence_role_for_source,
)
from ..support.tool_hints import KNOWLEDGE_SEARCH_TOOL
from src.logging_utils import get_logger

logger = get_logger(__name__)


def _h_attr(name: str, default: Any = None) -> Any:
    mod = sys.modules.get("src.mcp_handlers.knowledge.handlers")
    return getattr(mod, name, default) if mod is not None else default


def _utc_now_iso() -> str:
    fn = _h_attr("_utc_now_iso", None)
    if fn is not None:
        return fn()
    return datetime.now(timezone.utc).isoformat()


def _optional_flag(*args: Any, **kwargs: Any) -> bool:
    from .search_helpers import _optional_flag as _impl
    return _impl(*args, **kwargs)

VALID_DISCOVERY_TYPES = {
    "architectural_decision", "learning", "pattern", "bug_fix",
    "refactoring", "documentation", "experiment", "question", "note", "rule",
    "insight", "bug_found", "improvement", "exploration", "observation",
    # System-generated rollup rows (Issue #1 synthesis). Listed so search can
    # filter discovery_type='topic_rollup'; written by the synthesis pass, not
    # by agents directly.
    "topic_rollup",
}


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
            "as distinct arguments; do not embed tags or markup inside content. "
            "If the text you authored is clean, your context may have been "
            "quoting these markers and contaminated the call: describe them "
            "without the literal strings (e.g. 'an antml parameter tag') or "
            "send the content from a file via REST. "
            f"Matched marker: {leaked_marker!r}."
        },
    )


def _write_bound_refusal(message: str, action: str) -> TextContent:
    """INVALID_PARAM for a knowledge write with a value over its bound."""
    return error_response(
        message,
        error_code="INVALID_PARAM",
        error_category="validation_error",
        recovery={"action": action, "related_tools": ["knowledge"]},
    )


def _oversized_tags(tags: Any, outcome: str) -> Optional[tuple[str, str]]:
    """(message, recovery action) when tags are over MAX_TAGS or MAX_TAG_LEN.

    Measured on normalize_tags output, the list storage writes. ``outcome``
    ends the message ("Nothing was stored." or "Nothing was changed."). Input
    normalize_tags cannot read is left to the write path, which fails on it as
    it did before these bounds.
    """
    if tags is None:
        return None
    try:
        normalized = normalize_tags(tags)
    except Exception:
        return None
    if len(normalized) > MAX_TAGS:
        return (
            f"tags has {len(normalized)} entries after normalization; the "
            f"limit is {MAX_TAGS}. {outcome}",
            f"Send at most {MAX_TAGS} tags, naming the topics and components a "
            "search should find this by. Put descriptive text in summary or "
            "details.",
        )
    long_tags = [tag for tag in normalized if len(tag) > MAX_TAG_LEN]
    if long_tags:
        longest = max(long_tags, key=len)
        return (
            f"{len(long_tags)} tag(s) are over {MAX_TAG_LEN} characters after "
            f"normalization (the longest is {len(longest):,}, starting "
            f"'{longest[:40]}'); the limit is {MAX_TAG_LEN}. {outcome}",
            f"Tags are short labels such as 'age-backend'. Shorten each to at "
            f"most {MAX_TAG_LEN} characters and put descriptive text in summary "
            "or details.",
        )
    return None


_METADATA_PART_NAMES = {
    "references_files": "related_files",
    "provenance": (
        "provenance (memory_context, task_label, task_outcome and the other "
        "provenance fields, or a batch item's provenance)"
    ),
}


def _oversized_metadata(discovery: DiscoveryNode) -> Optional[tuple[str, str]]:
    """(message, recovery action) when a new finding's metadata is over its bound.

    Measured as the AGE backend writes it: the node metadata its store builds
    (KnowledgeGraphAGE._build_discovery_metadata), serialized with json.dumps
    as GraphMixin._sanitize_cypher_param serializes it. A value json.dumps
    cannot encode is measured as its str(); the store then fails on it as it
    did before this bound. Held on both backends, so a finding stored on one
    can be carried to the other. Call it after the handler has set every
    field it stores (related_to included).
    """
    from src.storage.knowledge_graph_age import KnowledgeGraphAGE

    metadata = KnowledgeGraphAGE._build_discovery_metadata(discovery)
    if not metadata:
        return None
    try:
        size = len(json.dumps(metadata, default=str))
    except ValueError:
        # A circular reference: the store fails on it as it did before.
        return None
    if size <= MAX_DISCOVERY_METADATA_LEN:
        return None
    parts = sorted(
        ((len(json.dumps(value, default=str)), key) for key, value in metadata.items()),
        reverse=True,
    )
    largest = ", ".join(
        f"{_METADATA_PART_NAMES.get(key, key)} {length:,}"
        for length, key in parts[:3]
    )
    return (
        f"This finding's metadata would be {size:,} characters as stored JSON; "
        f"the limit is {MAX_DISCOVERY_METADATA_LEN:,}. Nothing was stored. "
        f"Largest parts, in characters: {largest}.",
        "Send fewer or shorter related_files, and keep provenance fields such as "
        "memory_context, task_label and task_outcome to short labels. Put long "
        "material in details.",
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


def _coerce_pagination_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if value is None:
        return default
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    if coerced < minimum:
        return default
    if maximum is not None and coerced > maximum:
        return maximum
    return coerced


def _annotate_knowledge_confidence_authority(discovery) -> None:
    """Preserve writer confidence and record its actual authority boundary.

    The retired implementation capped a discovery at ``legacy C(V) + 0.3``.
    That directional ODE control value is neither evidence about the discovery
    nor a calibrated writer-reliability measurement, so the cap manufactured a
    federation-wide confidence ceiling from an unrelated signal.
    """
    if discovery.confidence is None:
        return
    if discovery.provenance is None:
        discovery.provenance = {}
    discovery.provenance["confidence_authority"] = {
        "schema": "knowledge.confidence.authority.v2",
        "source": "writer_supplied",
        "validation": "range_only",
        "legacy_coherence_clamp": "retired",
        "coherence_used": False,
    }


_AUDIT_QUERY_TEXT_MAX = 300


_AUDIT_QUERY_REDACTIONS = (
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+):([^\s/@]+)@"), r"\1:[REDACTED]@"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|passwd|password|secret|token|bearer)\s*([:=]|\s)\s*[^\s,;|]+"
        ),
        r"\1\2[REDACTED]",
    ),
)


def _audit_safe_query_text(query: Any) -> Optional[str]:
    """Redact and bound a search query for storage in ``audit.events``.

    Read traffic has been counted since PR #532, but only ever as
    ``query_present`` / ``query_term_count`` / ``result_count`` — never the
    terms. That makes retrievability unauditable: you can see that a search
    happened and how many rows came back, but not whether the right entry was
    reachable for what was actually asked. Measuring retrieval quality has so
    far required hand-building a gold set, which does not scale and does not
    reflect production traffic.

    Returns None for anything that is not a usable string, so the payload key is
    simply absent rather than present-and-null.
    """
    if not isinstance(query, str):
        return None
    text = query.strip()
    if not text:
        return None
    for pattern, replacement in _AUDIT_QUERY_REDACTIONS:
        text = pattern.sub(replacement, text)
    if len(text) > _AUDIT_QUERY_TEXT_MAX:
        text = text[:_AUDIT_QUERY_TEXT_MAX] + "…"
    return text


def _audit_safe_tags(tags: Any) -> Optional[list]:
    """Bounded, string-only view of a caller's tag filter for the audit row."""
    if not isinstance(tags, (list, tuple)):
        return None
    cleaned = [str(tag)[:64] for tag in tags if isinstance(tag, (str, int))][:10]
    return cleaned or None


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


class _InvalidResponseTo(Exception):
    """A response_to the caller supplied but that cannot become a typed link."""


def _parse_response_to(response_data: Any) -> Optional[ResponseTo]:
    """Parse the optional typed parent link shared by store, batch store and note.

    An absent or empty value means no link. Anything else either becomes a
    ResponseTo or raises _InvalidResponseTo with a caller-facing message: a
    write the caller asked to thread must not land unthreaded and report
    success. The batch path used to drop an unknown response_type silently,
    and all three dropped a malformed shape, so the reply edge (which lineage
    scoring in src/identity/memory_integration.py reads) went missing
    without a trace.
    """
    if not response_data:
        return None
    required_fields = {"discovery_id", "response_type"}
    if not isinstance(response_data, dict) or not required_fields.issubset(
        response_data
    ):
        raise _InvalidResponseTo(
            "Invalid response_to: expected "
            "{'discovery_id': <parent id>, 'response_type': <type>}"
        )

    raw_parent_id = response_data["discovery_id"]
    parent_id = "" if raw_parent_id is None else str(raw_parent_id).strip()
    if not parent_id:
        raise _InvalidResponseTo("Invalid response_to.discovery_id (empty)")

    response_type = normalize_response_type(response_data["response_type"])
    if response_type not in VALID_RESPONSE_TYPES:
        raise _InvalidResponseTo(
            f"Invalid response_type '{response_data['response_type']}'. "
            f"Valid: {sorted(VALID_RESPONSE_TYPES)}"
        )
    return ResponseTo(discovery_id=parent_id, response_type=response_type)


def _auto_link_requested(value: Any) -> bool:
    """Whether a store links the new row to similar ones. On unless explicitly false.

    The params middleware validates through a Pydantic model whose
    ``auto_link_related`` defaults to None and then merges ``model_dump()`` back
    into the arguments, so an unsent flag arrives as a present None. Reading it
    with ``.get(..., True)`` therefore saw None and skipped linking for every
    validated store; a string "false" read raw was truthy the other way round.
    """
    return _optional_flag(value) is not False


async def _link_similar_store_discoveries(state: _KnowledgeStoreState) -> None:
    if not _auto_link_requested(state.request.arguments.get("auto_link_related")):
        return
    from .synthesis import is_rollup

    candidates = await state.graph.find_similar(state.discovery, limit=8)
    state.similar = [item for item in candidates if not is_rollup(item)][:5]
    state.discovery.related_to = [item.id for item in state.similar]
    state.similar_discoveries = [item.to_dict(include_details=False) for item in state.similar]


def _refuse_oversized_store_metadata(state: _KnowledgeStoreState) -> None:
    oversized = _oversized_metadata(state.discovery)
    if oversized:
        raise _StoreResponseError(_write_bound_refusal(*oversized))


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
                "workflow": "Pass client_session_id on this call to bind as yourself. identity(client_session_id=...) reports the binding; a bare identity() mints a new agent instead of reading yours.",
            },
        )
    )


def _attach_store_response_hints(response: dict[str, Any], state: _KnowledgeStoreState) -> None:
    request = state.request
    if request.is_anonymous_writer:
        response["agent_mode"] = "anonymous"
        # Conditional on purpose, like the unbound metrics read: a process
        # that already called start_session and dropped the id on this call
        # would fork its work across two identities by minting again.
        response["_identity_hint"] = (
            "Stored under a lightweight anonymous writer ID, not an agent "
            "identity. For authorship continuity, pass on each write the "
            "client_session_id that start_session returned to this process; "
            "if it has none, call start_session(force_new=true) first."
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
    # The scheduled synthesis pass rolls these topics up next
    # (src/kg_synthesis_schedule.py); before it, the hint went nowhere.
    from src.kg_synthesis_schedule import hint_topics, record_consolidation_hint

    record_consolidation_hint(
        hint_topics(state.discovery.tags, (item.tags for item in open_similar)),
        discovery_id=state.discovery.id,
    )


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
    closure_class: Optional[str] = None
    closure_evidence: Any = None


class _UpdateResponseError(Exception):
    """Abort a discovery update with an already-structured MCP error."""

    def __init__(self, response: TextContent):
        super().__init__()
        self.response = response


def _refuse_oversized_summary_or_tags(request: _KnowledgeUpdateRequest) -> None:
    """Refuse an update whose summary or tags are over their bounds.

    Measured on the values storage would write (the summary as a string, the
    tags normalized). On AGE either one over the Cypher parameter limit fails
    the whole update and reads back as "Discovery not found"; refusing here
    names the real cause and changes nothing.
    """
    if request.summary is not None:
        size = len(str(request.summary))
        if size > MAX_UPDATED_SUMMARY_LEN:
            raise _UpdateResponseError(
                _write_bound_refusal(
                    f"summary is {size:,} characters; the limit is "
                    f"{MAX_UPDATED_SUMMARY_LEN:,}. Nothing was changed.",
                    f"Send a summary of at most {MAX_UPDATED_SUMMARY_LEN:,} "
                    "characters: the claim itself. Put supporting material in "
                    "details, or in resolution_notes when closing.",
                )
            )
    oversized_tags = _oversized_tags(request.tags, "Nothing was changed.")
    if oversized_tags:
        raise _UpdateResponseError(_write_bound_refusal(*oversized_tags))


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
    # Same rule as resolution_notes: a non-owner declares the standard of a
    # closure only in the call that makes it.
    if (
        request.closure_class is not None
        and request.status not in allowed_statuses
    ):
        requested_edits.append("closure_class")
    return requested_edits


_CLOSING_CALL_FIELDS = ("resolution_notes", "closure_class")


def _non_owner_edit_refusal(
    discovery_id: str, requested_edits: list[str], allowed_list: list[str]
) -> TextContent:
    """Refuse a non-owner's edit, naming the call that would be accepted.

    "Retry with status only" is right for the owner's fields and wrong for
    resolution_notes and closure_class: following it drops them, when the same
    call with a cross-agent closing status would have landed them.
    """
    owner_only = [f for f in requested_edits if f not in _CLOSING_CALL_FIELDS]
    closing_call = [f for f in requested_edits if f in _CLOSING_CALL_FIELDS]
    if not closing_call:
        message = (
            "Permission denied: Non-owners cannot edit "
            f"{', '.join(owner_only)} on high-severity discovery "
            f"'{discovery_id}'. Allowed cross-agent status values: "
            f"{allowed_list}."
        )
        action = f"Retry with status only. Allowed values: {allowed_list}"
    else:
        closing_fields = " and ".join(closing_call)
        verb = "is" if len(closing_call) == 1 else "are"
        accepted = (
            f"{closing_fields} {verb} accepted from a non-owner only together "
            f"with a cross-agent closing status, one of {allowed_list}"
        )
        if owner_only:
            message = (
                "Permission denied: Non-owners cannot edit "
                f"{', '.join(owner_only)} on high-severity discovery "
                f"'{discovery_id}', and {accepted}."
            )
            action = (
                f"Drop {', '.join(owner_only)}, and pass {closing_fields} "
                f"together with status in {allowed_list}."
            )
        else:
            message = (
                f"Permission denied on high-severity discovery "
                f"'{discovery_id}': {accepted}."
            )
            action = f"Pass {closing_fields} together with status in {allowed_list}."
    return error_response(
        message,
        recovery={
            "action": action,
            "related_tools": [
                "knowledge",
                "search_knowledge_graph",
            ],
        },
    )


def _require_owned_binding(request: _KnowledgeUpdateRequest, agent_id: str) -> None:
    """Refuse a write to a high or critical finding from an unproven caller."""
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
                        "Pass client_session_id on this call to bind as yourself. "
                        "identity(client_session_id=...) reports the binding; a bare "
                        "identity() mints a new agent instead of reading yours."
                    ),
                },
            )
        )


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

    _require_owned_binding(request, agent_id)

    allowed_statuses = {"resolved", "closed", "wont_fix"}
    if discovery.agent_id == agent_id:
        return

    requested_edits = _requested_non_owner_edits(request, allowed_statuses)
    allowed_list = sorted(allowed_statuses)
    if requested_edits:
        raise _UpdateResponseError(
            _non_owner_edit_refusal(
                request.discovery_id, requested_edits, allowed_list
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
                        "knowledge",
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


def _refuse_oversized_details(
    request: _KnowledgeUpdateRequest,
    updates: dict[str, Any],
    call: str = "update",
) -> None:
    """Refuse an update whose details value is over MAX_UPDATED_DETAILS_LEN.

    Measured on the value storage would write, so the stored details (earlier
    notes included) or the details this call sends count along with the new
    notes block. On AGE a value over the Cypher parameter limit fails the
    whole update and reads back as "Discovery not found"; refusing here
    names the real cause and changes nothing. ``call`` names the call the
    recovery tells the caller to resend (a supersede appends notes too).
    """
    details = updates.get("details")
    if details is None or len(details) <= MAX_UPDATED_DETAILS_LEN:
        return

    size = len(details)
    limit = f"the limit is {MAX_UPDATED_DETAILS_LEN:,}. Nothing was changed."
    elsewhere = (
        "Put long material such as logs in the repository, a PR or a new "
        "finding that responds to this one (response_to={'discovery_id': "
        f"'{request.discovery_id}', 'response_type': 'elaboration'}}) and "
        "point to it."
    )
    if request.resolution_note is None:
        message = f"details is {size:,} characters; {limit}"
        action = (
            f"Send details of at most {MAX_UPDATED_DETAILS_LEN:,} characters. "
            f"{elsewhere}"
        )
    else:
        message = (
            "details with these resolution_notes appended would be "
            f"{size:,} characters; {limit}"
        )
        room = MAX_UPDATED_DETAILS_LEN - (size - len(request.resolution_note))
        if room > 0:
            action = (
                f"Shorten resolution_notes to at most {room:,} characters: "
                "what closed the finding, with pointers (a commit, a PR, a "
                f"finding id) to the rest. {elsewhere}"
            )
        else:
            held_by = (
                "The details this call sends leave"
                if request.details is not None
                else "This finding's stored details leave"
            )
            action = (
                f"{held_by} no room for a notes block. Send the {call} "
                "without resolution_notes and record the notes as a new "
                "finding that responds to this one (response_to="
                f"{{'discovery_id': '{request.discovery_id}', "
                "'response_type': 'elaboration'})."
            )
    raise _UpdateResponseError(
        error_response(
            message,
            error_code="INVALID_PARAM",
            error_category="validation_error",
            recovery={"action": action, "related_tools": ["knowledge"]},
        )
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


CLOSURE_CLASSES = {
    "fix_verified",
    "unobserved",
    "not_reproducible",
    "obsolete",
    "duplicate",
}


_CLOSING_STATUSES = {"resolved", "closed", "wont_fix", "superseded"}


_CLASS_ADMITTING_STATUSES = CLOSURE_CLASS_ADMITTING_STATUSES


_CLOSURE_EVIDENCE_REQUIRED = {
    "fix_verified": ("deployed", "observed"),
    "unobserved": ("window", "instrument_check"),
}


CLOSURE_CLASS_PERSISTED = True


def _closure_class_choices() -> str:
    """The closure classes as prose: a list literal reads as a list value."""
    return ", ".join(f"'{name}'" for name in sorted(CLOSURE_CLASSES))


def _closure_evidence_keys_prose() -> str:
    """Which classes need which closure_evidence keys, from the validator's table."""
    return "; ".join(
        f"'{closure_class}' needs {' and '.join(keys)}"
        for closure_class, keys in sorted(_CLOSURE_EVIDENCE_REQUIRED.items())
    )


def _closure_evidence_examples() -> str:
    """The closure_evidence object each class needs, as a caller would send it."""
    return "; ".join(
        f"'{closure_class}' also needs closure_evidence="
        "{" + ", ".join(f"'{key}': '...'" for key in keys) + "}"
        for closure_class, keys in sorted(_CLOSURE_EVIDENCE_REQUIRED.items())
    )


_CLOSURE_PREAMBLE = (
    "This closure declares no standard and reads as unclassified: a later "
    "reader cannot tell it from a closure resting on a deployed fix whose "
    "effect was observed."
)


_UNOBSERVED_IS_HONEST = (
    "'unobserved' is the honest label when the evidence is that a symptom "
    "stopped appearing."
)


def _closure_class_call(discovery_id: str, status: Optional[str]) -> str:
    """The follow-up that records a class, as this caller can make it.

    update_finding declares closure_class and closure_evidence (interface
    contract 1.20.0). The status this update set is repeated for the reason
    _resolution_notes_call gives: a non-owner of a high or critical finding may
    set a class only together with a cross-agent closing status. For anyone
    else the repeated status is a no-op: resolved_at is stamped only on the
    transition into resolved or on a resolved row that has none, so repeating
    'resolved' leaves an existing stamp alone.
    """
    status_argument = f", status='{status}'" if status else ""
    return (
        f"update_finding(discovery_id='{discovery_id}'{status_argument}, "
        "closure_class='...')"
    )


def _unclassified_closure_note(
    discovery_id: str, status: str, *, notes_passed: bool = False
) -> str:
    """What a closing update without closure_class should do next.

    A call that already carried notes is told they are on the record rather
    than that it declares nothing: the notes may name the standard, and the
    class records it as a field of its own.
    """
    if not _h_attr("CLOSURE_CLASS_PERSISTED", CLOSURE_CLASS_PERSISTED):
        # Kept so the notes stay true if storage stops writing the class
        # again: resolution_notes is then the only durable place for it.
        standards = (
            f"{_closure_class_choices()} ({_closure_evidence_keys_prose()})"
        )
        return (
            f"{_CLOSURE_PREAMBLE} The server does not store closure_class, so "
            "name the standard and its evidence in resolution_notes, which is "
            f"stored: {_resolution_notes_call(discovery_id, status)} appends "
            f"them. The standard is one of {standards}. {_UNOBSERVED_IS_HONEST}"
        )
    classes = (
        f"closure_class is a string, one of: {_closure_class_choices()}; "
        f"{_closure_evidence_examples()}."
    )
    call = _closure_class_call(discovery_id, status)
    if notes_passed:
        return (
            "This closure passed no closure_class. Its resolution_notes are "
            "stored with the record; the class records the standard they rest "
            f"on as a field of its own, and {call} sets it. {classes} "
            f"{_UNOBSERVED_IS_HONEST}"
        )
    return (
        f"{_CLOSURE_PREAMBLE} {call} records the standard. {classes} "
        f"{_UNOBSERVED_IS_HONEST}"
    )


def _resolution_notes_call(discovery_id: str, status: Optional[str]) -> str:
    """The follow-up that appends resolution_notes, as this caller can make it.

    update_finding's wire declares discovery_id, status and resolution_notes.
    The status this update set is repeated because a non-owner of a high or
    critical finding may add resolution_notes only together with a cross-agent
    closing status (_requested_non_owner_edits): without it the same call is
    refused. For anyone else the repeated status is a no-op; resolved_at is
    stamped on the transition into resolved, or on a resolved row that has
    none, so repeating 'resolved' leaves an existing stamp alone. An update
    that set no status
    needs none: a non-owner of a gated finding cannot make one succeed.
    """
    status_argument = f", status='{status}'" if status else ""
    return (
        f"update_finding(discovery_id='{discovery_id}'{status_argument}, "
        "resolution_notes='...')"
    )


def _unstored_closure_class_note(
    discovery_id: str,
    closure_class: str,
    status: Optional[str],
    *,
    notes_passed: bool = False,
) -> str:
    """A class that passed validation but is not on the record read back."""
    unstored = (
        f"closure_class '{closure_class}' passed validation, but the record "
        "read back after this update does not carry it. resolution_notes is "
        "stored"
    )
    call = _resolution_notes_call(discovery_id, status)
    if notes_passed:
        return (
            f"{unstored}, and this call's notes are on the record: if they do "
            f"not name the standard and its evidence, {call} appends a note "
            "that does."
        )
    return f"{unstored}: name the standard and its evidence there with {call}."


def _invalid_closure_param(message: str, recovery: Optional[str] = None) -> _UpdateResponseError:
    return _UpdateResponseError(
        error_response(
            message,
            error_code="INVALID_PARAM",
            error_category="validation_error",
            recovery={"action": recovery} if recovery else None,
        )
    )


def _validate_closure_class(
    request: _KnowledgeUpdateRequest,
    normalized_status: Optional[str],
    stored_status: Optional[str] = None,
) -> None:
    """Reject an ill-formed or contradictory closure class.

    ``normalized_status`` is the status this update sets, if any; otherwise the
    class is judged against ``stored_status``, the status the row already has.
    Without that fallback, now that storage writes the class, a class sent to
    an open row would reach storage, the constraint would refuse it, and the
    AGE backend would report the refusal as "Discovery not found".
    """
    if request.closure_class is None:
        if request.closure_evidence is not None:
            # Evidence is stored with its class, never alone.
            raise _invalid_closure_param(
                "closure_evidence is evidence for a closure_class and is "
                "stored only with one; pass closure_class in the same call, "
                f"one of: {_closure_class_choices()}."
            )
        return

    if request.closure_class not in CLOSURE_CLASSES:
        raise _UpdateResponseError(
            error_response(
                f"Invalid closure_class '{request.closure_class}'. It is a "
                f"string, one of: {_closure_class_choices()}.",
                error_code="INVALID_PARAM",
                error_category="validation_error",
                recovery={
                    "action": (
                        "Pick the class describing what you actually had. "
                        "'unobserved' is the honest label for closing because "
                        "the condition stopped occurring without an established "
                        "cause — prefer it over 'fix_verified' when the evidence "
                        "is that a symptom is absent."
                    ),
                    "related_tools": ["knowledge"],
                },
            )
        )

    # A standard for a closure that is not happening. The DB rejects this too;
    # failing here gives the caller a usable message instead of a constraint.
    admitted = ", ".join(sorted(_CLASS_ADMITTING_STATUSES))
    if normalized_status is not None:
        if normalized_status not in _CLASS_ADMITTING_STATUSES:
            raise _invalid_closure_param(
                "closure_class names the standard a closure rests on, so it "
                f"cannot sit on status='{normalized_status}'. It is admitted "
                f"on: {admitted}."
            )
    elif stored_status is not None:
        stored = str(stored_status).lower()
        if stored not in _CLASS_ADMITTING_STATUSES:
            raise _invalid_closure_param(
                "closure_class names the standard a closure rests on, and "
                f"discovery '{request.discovery_id}' is '{stored}' while this "
                "call sets no status.",
                recovery=(
                    "Close it in the same call: pass status as one of "
                    f"{', '.join(sorted(_CLOSING_STATUSES))} with the class."
                ),
            )

    evidence = request.closure_evidence
    if evidence is not None and not isinstance(evidence, dict):
        raise _invalid_closure_param(
            "closure_evidence is an object of named evidence, not "
            f"{type(evidence).__name__}.",
            recovery=_closure_evidence_hint(request.closure_class),
        )
    if evidence is not None:
        # Refused before storage: on AGE an oversized property fails the whole
        # update (status included) and reads back as "Discovery not found".
        stored_size = len(closure_evidence_to_json(evidence).encode("utf-8"))
        if stored_size > MAX_CLOSURE_EVIDENCE_BYTES:
            raise _invalid_closure_param(
                f"closure_evidence is {stored_size:,} bytes as stored JSON; the "
                f"limit is {MAX_CLOSURE_EVIDENCE_BYTES:,}. Nothing was changed.",
                recovery=(
                    "Keep each evidence value to a short statement or a pointer "
                    "(a commit, a build_sha, a query, a finding id) and put long "
                    "material such as log excerpts in resolution_notes, which is "
                    "appended to details."
                ),
            )

    required = _CLOSURE_EVIDENCE_REQUIRED.get(request.closure_class)
    if not required:
        return

    if not isinstance(evidence, dict):
        raise _UpdateResponseError(
            error_response(
                f"closure_class='{request.closure_class}' requires "
                f"closure_evidence as an object with keys {list(required)}.",
                error_code="INVALID_PARAM",
                error_category="validation_error",
                recovery={"action": _closure_evidence_hint(request.closure_class)},
            )
        )

    missing = [
        key
        for key in required
        if not str(evidence.get(key) or "").strip()
    ]
    if missing:
        raise _UpdateResponseError(
            error_response(
                f"closure_class='{request.closure_class}' is missing required "
                f"closure_evidence: {missing}.",
                error_code="INVALID_PARAM",
                error_category="validation_error",
                recovery={"action": _closure_evidence_hint(request.closure_class)},
            )
        )


def _closure_evidence_hint(closure_class: str) -> str:
    if closure_class == "fix_verified":
        return (
            "deployed: what shipped and how you confirmed it is in the RUNNING "
            "build (compare build_sha, not a cached status line). "
            "observed: the new behaviour you saw. The old symptom being absent "
            "is not an observation of the fix — if that is all you have, the "
            "class is 'unobserved'."
        )
    if closure_class == "unobserved":
        return (
            "window: the period over which the condition did not occur. "
            "instrument_check: how you established the recorder for THIS "
            "condition is still live. A sibling signal still arriving does not "
            "establish it; siblings share the sink, not the emitter."
        )
    return (
        "Send closure_evidence as an object of named facts, for example "
        "{'of': '<discovery_id>'} for 'duplicate', or omit it."
    )


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
        # Stamp resolved_at on the transition into resolved only. Repeating
        # status='resolved' (the closure note's follow-up names it so a
        # non-owner of a high/critical finding can append notes) must not move
        # when the finding was actually resolved.
        if normalized_status == "resolved" and (
            getattr(discovery, "status", None) != "resolved"
            or not getattr(discovery, "resolved_at", None)
        ):
            # ...or backfill a resolved row that never got one.
            updates["resolved_at"] = _utc_now_iso()

    _validate_closure_class(request, normalized_status, discovery.status)
    if request.closure_class is not None:
        # The pair is written together, so a class changed to one that needs
        # no evidence does not keep the previous class's evidence.
        updates["closure_class"] = request.closure_class
        updates["closure_evidence"] = request.closure_evidence
    elif normalized_status in CLOSURE_CLASS_CLEARING_STATUSES:
        # Reopening: the closure the class described is no longer the state.
        updates["closure_class"] = None
        updates["closure_evidence"] = None

    _apply_update_text_fields(request, discovery, updates)
    _refuse_oversized_details(request, updates)
    _apply_update_metadata_fields(request, updates)
    return updates, normalized_status


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
    return shortened.rstrip() + SUMMARY_TRUNCATION_MARKER, truncation


def _truncate_batch_details(details: Any) -> tuple[Any, Optional[str]]:
    """Apply the stable details limit used by batch writes."""
    if len(details) <= MAX_DETAILS_LEN:
        return details, None
    truncation = f"details ({len(details)} → {MAX_DETAILS_LEN})"
    return details[:MAX_DETAILS_LEN] + "... [truncated]", truncation


def _parse_batch_response_to(
    disc_data: dict[str, Any],
) -> Optional[ResponseTo]:
    """Parse one batch item's response edge; an invalid one fails that item."""
    try:
        return _parse_response_to(disc_data.get("response_to"))
    except _InvalidResponseTo as exc:
        raise _BatchItemError(str(exc)) from None


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


def _supersede_notes_block_details(
    request: _KnowledgeUpdateRequest, discovery: DiscoveryNode
) -> str:
    """The older finding's details with the supersede's notes appended.

    Built as an update builds them (_apply_update_text_fields) and held to
    the same bound; over it, raises _UpdateResponseError.
    """
    updates: dict[str, Any] = {}
    _apply_update_text_fields(request, discovery, updates)
    _refuse_oversized_details(request, updates, call="supersede")
    return updates["details"]


