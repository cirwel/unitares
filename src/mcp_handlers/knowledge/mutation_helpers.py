"""Write/mutation, writer resolution, candidate retrieval, batch/note, and lifecycle/audit helpers for knowledge handlers.

Extracted from ``knowledge/handlers.py`` to keep write persistence, author resolution,
hybrid/semantic candidate retrieval, batch/note mutations, and lifecycle/audit operations modular.
"""

from dataclasses import dataclass
from functools import wraps
from typing import Dict, Any, Sequence, Optional
from mcp.types import TextContent
from datetime import datetime, timezone
import hashlib
import os
import secrets
import sys

from ..utils import (
    success_response,
    error_response,
    require_agent_id as _default_require_agent_id,
    require_registered_agent as _default_require_registered_agent,
)
from ..identity.stable_session import audit_reference
from src.knowledge_graph import (
    get_knowledge_graph as _default_get_knowledge_graph,
    DiscoveryNode,
    ResponseTo,
    normalize_tags,
)
from src.knowledge_authority import (
    IMPORTED_CONTEXT,
    PROMOTION_SCHEMA,
    PROMOTION_TAG,
    assess_authority,
)
from src.mcp_handlers.knowledge.limits import (
    MAX_DETAILS_LEN,
    MAX_SUMMARY_LEN,
    SUMMARY_TRUNCATION_MARKER,
)
from src.logging_utils import get_logger
from src.coherence_provenance import (
    BEHAVIORAL_UPDATE_CONSISTENCY_ROLE,
    coherence_role_for_source,
)
from src.mcp_handlers.shared import lazy_mcp_server as _default_mcp_server
from src.broadcaster import broadcaster_instance as _default_broadcaster_instance

from .validation_helpers import (
    VALID_DISCOVERY_TYPES,
    VALID_SEVERITIES,
    SEVERITY_ALIASES,
    _normalize_discovery_type,
    _detect_toolcall_markup_leak,
    _degenerate_write_response,
    _write_bound_refusal,
    _oversized_tags,
    _oversized_metadata,
    _invalid_enum_response,
    _annotate_knowledge_confidence_authority,
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
    _GATED_SEVERITIES,
    _effective_update_severity,
    _require_owned_binding,
    _authorize_high_severity_update,
    _CLOSING_STATUSES,
    _unclassified_closure_note,
    _unstored_closure_class_note,
    _build_discovery_updates,
    _BatchItemError,
    _PreparedBatchDiscovery,
    _BatchStoreResult,
    _truncate_batch_summary,
    _truncate_batch_details,
    _parse_batch_response_to,
    _parse_batch_severity,
    _parse_batch_confidence,
    _NoteResponseError,
    _KnowledgeNoteRequest,
    _truncate_note_text,
    _split_note_text,
    _infer_note_severity,
    _supersede_notes_block_details,
)
from .search_helpers import (
    _bounded_supersession_lookup,
)

logger = get_logger(__name__)


def _h_attr(name: str, default: Any = None) -> Any:
    mod = sys.modules.get("src.mcp_handlers.knowledge.handlers")
    return getattr(mod, name, default) if mod is not None else default


class _McpServerProxy:
    def __getattr__(self, name: str) -> Any:
        server = _h_attr("mcp_server", _default_mcp_server)
        return getattr(server, name)


class _BroadcasterProxy:
    def __getattr__(self, name: str) -> Any:
        b = _h_attr("broadcaster_instance", _default_broadcaster_instance)
        return getattr(b, name)


mcp_server = _McpServerProxy()
broadcaster_instance = _BroadcasterProxy()


def _utc_now_iso() -> str:
    fn = _h_attr("_utc_now_iso", None)
    if fn is not None:
        return fn()
    return datetime.now(timezone.utc).isoformat()


def _new_discovery_id() -> str:
    fn = _h_attr("_new_discovery_id", None)
    if fn is not None:
        return fn()
    return datetime.now(timezone.utc).isoformat()


async def get_knowledge_graph(*args: Any, **kwargs: Any) -> Any:
    return await _h_attr("get_knowledge_graph", _default_get_knowledge_graph)(*args, **kwargs)


def require_agent_id(*args: Any, **kwargs: Any) -> Any:
    return _h_attr("require_agent_id", _default_require_agent_id)(*args, **kwargs)


def require_registered_agent(*args: Any, **kwargs: Any) -> Any:
    return _h_attr("require_registered_agent", _default_require_registered_agent)(*args, **kwargs)


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
        agent_display = _h_attr("_resolve_agent_display", _resolve_agent_display)(agent_id)

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
        agent_id, error, is_anonymous = _h_attr("_resolve_low_friction_writer", _resolve_low_friction_writer)(
            arguments, inferred_exempt=inferred_exempt
        )
        return agent_id, error, None, is_anonymous

    refusal = _gated_write_refused_on_inferred_binding(inferred_exempt)
    if refusal is not None:
        return "", refusal, None, False
    agent_id, error = require_registered_agent(arguments)
    if error:
        return agent_id, error, None, False
    display_name_error, display_name_warning = _h_attr("_check_display_name_required", _check_display_name_required)(agent_id, arguments)
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
            agent_id, error, _ = _h_attr("_resolve_low_friction_writer", _resolve_low_friction_writer)(candidate)
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
        _get_lineage = _h_attr("_get_store_lineage_fn")()

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

        provenance_chain = await _h_attr("_build_s7_provenance_chain_with_fallback", _build_s7_provenance_chain_with_fallback)(agent_id, meta, _get_lineage)
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
    state.provenance, state.provenance_chain = await _h_attr("_capture_store_provenance", _capture_store_provenance)(arguments, request.agent_id)
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
    await _h_attr("_broadcast_knowledge_write", _broadcast_knowledge_write)(discovery, request.agent_id)
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
    agent_display = _h_attr("_agent_display_for_response", _agent_display_for_response)(request.agent_id, request.arguments)
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
        agent_id, error, _ = _h_attr("_resolve_low_friction_writer", _resolve_low_friction_writer)(
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
    await _h_attr("_broadcast_knowledge_write", _broadcast_knowledge_write)(discovery, agent_id)
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
    await _h_attr("_broadcast_knowledge_write", _broadcast_knowledge_write)(note, agent_id)


def _build_note_response(
    request: _KnowledgeNoteRequest, note: DiscoveryNode
) -> Sequence[TextContent]:
    """Render the stable legacy and consolidated note response."""
    response = {
        "message": "Note saved",
        "note_id": note.id,
        "agent": _h_attr("_agent_display_for_response", _agent_display_for_response)(
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


async def _handle_cleanup_knowledge_graph(arguments: Dict[str, Any]) -> Sequence[TextContent]:
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

async def _handle_synthesize_knowledge_graph(arguments: Dict[str, Any]) -> Sequence[TextContent]:
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

async def _handle_get_lifecycle_stats(arguments: Dict[str, Any]) -> Sequence[TextContent]:
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


async def _handle_supersede_discovery(arguments: Dict[str, Any]) -> Sequence[TextContent]:
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


async def _handle_promote_memory_claim(
    arguments: Dict[str, Any],
    *,
    default_discovery_type: Any = None,
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
            default_discovery_type if default_discovery_type is not None else (arguments.get("discovery_type") or "insight")
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

        provenance, provenance_chain = await _h_attr("_capture_store_provenance", _capture_store_provenance)(
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
        await _h_attr("_broadcast_knowledge_write", _broadcast_knowledge_write)(claim, agent_id)

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


async def _handle_audit_knowledge_graph(arguments: Dict[str, Any]) -> Sequence[TextContent]:
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
