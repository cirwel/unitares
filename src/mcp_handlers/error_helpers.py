"""
Enhanced error handling utilities for MCP handlers.

Standardizes error responses with recovery guidance and context.
"""

from typing import Dict, Any, Optional, Sequence
from mcp.types import TextContent
from src.logging_utils import get_logger
from .identity_bootstrap import SET_DISPLAY_NAME_CALL
from .support.coerce import coerce_bool
from .utils import error_response

logger = get_logger(__name__)

# Recovery for a caller that has no identity yet, then names it. The name
# call carries client_session_id: with only name= identity() resolves from
# transport signals, which can name a co-located agent instead.
_BIND_THEN_NAME_ACTION = (
    "Bind an identity with start_session(force_new=true) if this process has "
    f"none, then set a cosmetic name with {SET_DISPLAY_NAME_CALL}"
)
_BIND_THEN_NAME_WORKFLOW = [
    "1. If this process has no identity yet, call start_session(force_new=true) "
    "and keep the client_session_id it returns",
    f"2. Call {SET_DISPLAY_NAME_CALL} with that id to set your display name",
    "3. Then call this tool again",
]


# Standard recovery patterns for common error types
# Updated Dec 2025: API keys deprecated, UUID-based identity is now primary
RECOVERY_PATTERNS = {
    "agent_not_found": {
        "action": _BIND_THEN_NAME_ACTION,
        "related_tools": ["start_session", "identity", "agent"],
        "workflow": list(_BIND_THEN_NAME_WORKFLOW),
    },
    "agent_not_registered": {
        "action": _BIND_THEN_NAME_ACTION,
        "related_tools": ["start_session", "identity", "agent"],
        "workflow": list(_BIND_THEN_NAME_WORKFLOW),
    },
    "authentication_failed": {
        "action": "Pass the client_session_id start_session returned to this process",
        "related_tools": ["identity", "health_check"],
        "workflow": [
            # An argument-less identity() mints before it reads, so it cannot
            # report the binding this process already has.
            "1. Call identity(client_session_id='...') to check your current binding",
            "2. If this process never called start_session, call "
            "start_session(force_new=true) first",
            "3. Retry your original request with that client_session_id"
        ]
    },
    "authentication_required": {
        "action": "Pass the client_session_id start_session returned to this process",
        "related_tools": ["start_session", "identity", "process_agent_update"],
        "workflow": [
            "1. If this process has no identity yet, call start_session(force_new=true) "
            "and keep the client_session_id it returns",
            f"2. To set a display name, call {SET_DISPLAY_NAME_CALL} with that id",
            "3. Retry your original request with that client_session_id"
        ]
    },
    "ownership_required": {
        "action": "You can only modify your own resources",
        "related_tools": ["identity", "agent"],
        "workflow": [
            "1. Call identity(client_session_id='...') to verify your bound identity",
            "2. Ensure the resource belongs to your agent_uuid",
            "3. You cannot modify resources owned by other agents"
        ]
    },
    "rate_limit_exceeded": {
        "action": "Wait a few seconds before retrying",
        "related_tools": ["health_check"],
        "workflow": [
            "1. Wait 10-30 seconds",
            "2. Retry request",
            "3. If persistent, check system health"
        ]
    },
    "timeout": {
        "action": "This may indicate a blocking operation or system overload. Try again with simpler parameters.",
        "related_tools": ["health_check", "admin"],
        "workflow": [
            "1. Wait a few seconds and retry",
            "2. Check system health with health_check",
            "3. Simplify request parameters",
            "4. Check for system overload"
        ]
    },
    "invalid_parameters": {
        "action": "Check tool parameters and try again",
        "related_tools": ["list_tools", "health_check"],
        "workflow": [
            "1. Verify tool parameters match schema",
            "2. Check tool description with list_tools",
            "3. Retry with correct parameters"
        ]
    },
    "validation_error": {
        "action": "Check parameter format and constraints",
        "related_tools": ["list_tools"],
        "workflow": [
            "1. Review parameter requirements",
            "2. Check tool schema with list_tools",
            "3. Retry with valid parameters"
        ]
    },
    "system_error": {
        "action": "Check system health and retry",
        "related_tools": ["health_check", "admin"],
        "workflow": [
            "1. Check system health",
            "2. Wait a few seconds",
            "3. Retry request"
        ]
    },
    "resource_not_found": {
        "action": "Verify the resource ID exists",
        "related_tools": ["agent", "search_knowledge_graph"],
        "workflow": [
            "1. Check if resource exists",
            "2. Verify resource ID format",
            "3. Use search/list tools to find correct ID"
        ]
    },
    "not_connected": {
        "action": "Check MCP server connection status",
        "related_tools": ["admin", "health_check"],
        "workflow": [
            "1. Call admin(action='connections') to verify connection",
            "2. Check if MCP server is running",
            "3. Verify MCP configuration in client settings",
            "4. Retry your request"
        ]
    },
    "missing_client_session_id": {
        "action": "Provide active session continuity metadata or re-onboard explicitly",
        "related_tools": ["identity", "onboard"],
        "workflow": [
            "1. Call identity(client_session_id='...') to inspect the current binding",
            "2. Include continuity_token for a proof-owned UUID rebind when available",
            "3. For a fresh process, call onboard(force_new=true) and use parent_agent_id for lineage"
        ]
    },
    "session_mismatch": {
        "action": "Verify your session identity matches",
        "related_tools": ["identity", "admin"],
        "workflow": [
            "1. Call identity(client_session_id='...') to check your resolved identity",
            "2. Ensure client_session_id or continuity_token matches your active session",
            "3. If mismatch persists, call onboard(force_new=true) and declare parent_agent_id when inheriting work",
            "4. Retry your request with the resolved binding"
        ]
    },
    "missing_parameter": {
        "action": "Include the missing required parameter",
        "related_tools": ["describe_tool", "list_tools"],
        "workflow": [
            "1. Check tool description with describe_tool(tool_name=...)",
            "2. Add the missing parameter to your call",
            "3. Retry your request"
        ]
    },
    "invalid_parameter_type": {
        "action": "Check parameter type and format",
        "related_tools": ["describe_tool"],
        "workflow": [
            "1. Check parameter type with describe_tool(tool_name=...)",
            "2. Ensure parameter matches expected type (string, number, array, etc.)",
            "3. Retry with correct type"
        ]
    },
    "permission_denied": {
        "action": "Verify you have required permissions",
        "related_tools": ["identity", "get_governance_metrics"],
        "workflow": [
            "1. Call identity(client_session_id='...') to verify your identity",
            "2. Check if operation requires specific permissions",
            "3. Some operations require registered agent (call onboard() first)",
            "4. Retry after verifying permissions"
        ]
    }
}


def agent_not_found_error(
    agent_id: str, 
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "AGENT_NOT_FOUND"
) -> Sequence[TextContent]:
    """Standard error for agent not found"""
    return [error_response(
        f"Agent '{agent_id}' not found",
        error_code=error_code,
        error_category="validation_error",
        details={"error_type": "agent_not_found", "agent_id": agent_id},
        recovery=RECOVERY_PATTERNS["agent_not_found"],
        context=context or {}
    )]


def agent_not_registered_error(
    agent_id: str,
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "AGENT_NOT_REGISTERED"
) -> Sequence[TextContent]:
    """Standard error for agent not registered"""
    return [error_response(
        f"Agent '{agent_id}' is not registered. You must onboard first.",
        error_code=error_code,
        error_category="validation_error",
        details={"error_type": "agent_not_registered", "agent_id": agent_id},
        recovery=RECOVERY_PATTERNS["agent_not_registered"],
        context=context or {}
    )]


def authentication_error(
    message: str = "Authentication failed",
    agent_id: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "AUTHENTICATION_FAILED"
) -> Sequence[TextContent]:
    """Standard error for authentication failure"""
    if agent_id:
        message = f"Authentication failed for agent '{agent_id}'. Invalid API key."
        details = {"error_type": "authentication_failed", "agent_id": agent_id}
    else:
        details = {"error_type": "authentication_failed"}
    
    return [error_response(
        message,
        error_code=error_code,
        error_category="auth_error",
        details=details,
        recovery=RECOVERY_PATTERNS["authentication_failed"],
        context=context or {}
    )]


def authentication_required_error(
    operation: str = "this operation",
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "AUTHENTICATION_REQUIRED"
) -> Sequence[TextContent]:
    """Standard error for missing authentication"""
    return [error_response(
        f"API key required for {operation}. Authentication required to prevent unauthorized access.",
        error_code=error_code,
        error_category="auth_error",
        details={"error_type": "authentication_required", "operation": operation},
        recovery=RECOVERY_PATTERNS["authentication_required"],
        context=context or {}
    )]


def ownership_error(
    resource_type: str,
    resource_id: str,
    owner_agent_id: str,
    caller_agent_id: str,
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "OWNERSHIP_VIOLATION"
) -> Sequence[TextContent]:
    """Standard error for ownership violation"""
    return [error_response(
        f"Unauthorized: Agent '{caller_agent_id}' cannot modify {resource_type} '{resource_id}' owned by '{owner_agent_id}'.",
        error_code=error_code,
        error_category="auth_error",
        details={
            "error_type": "ownership_violation",
            "resource_type": resource_type,
            "resource_id": resource_id,
            "owner_agent_id": owner_agent_id,
            "caller_agent_id": caller_agent_id
        },
        recovery=RECOVERY_PATTERNS["ownership_required"],
        context=context or {}
    )]


def rate_limit_error(agent_id: str, stats: Optional[Dict[str, Any]] = None) -> Sequence[TextContent]:
    """Standard error for rate limit exceeded"""
    return [error_response(
        f"Rate limit exceeded for agent '{agent_id}'",
        error_code="RATE_LIMIT_EXCEEDED",
        error_category="validation_error",
        details={"error_type": "rate_limit_exceeded", "agent_id": agent_id},
        recovery=RECOVERY_PATTERNS["rate_limit_exceeded"],
        context={"rate_limit_stats": stats} if stats else {}
    )]


def timeout_error(tool_name: str, timeout: float) -> Sequence[TextContent]:
    """Standard error for the timeout of a call that changes nothing.

    Its recovery says to retry. A call that can write gets
    unknown_outcome_timeout_error instead.
    """
    return [error_response(
        f"Tool '{tool_name}' timed out after {timeout} seconds.",
        error_code="TIMEOUT",
        error_category="system_error",
        details={"error_type": "timeout", "tool_name": tool_name, "timeout_seconds": timeout},
        recovery=RECOVERY_PATTERNS["timeout"],
        context={"tool_name": tool_name, "timeout_seconds": timeout}
    )]


# Create-only knowledge actions: every call adds a new row.
_KNOWLEDGE_STORE_ACTIONS = frozenset({"store", "note"})


def _call_literal(value: Any, placeholder: str) -> str:
    """A caller-supplied id to quote inside a call shape, else the placeholder.

    Anything that would break the quoting or bloat the reply falls back.
    """
    if (
        isinstance(value, str)
        and 0 < len(value) <= 200
        and value.isprintable()
        and "'" not in value
        and "\\" not in value
    ):
        return value
    return placeholder


# The search limit's ceiling (_parse_knowledge_search_request clamps above it).
_WINDOW_LOOKUP_LIMIT = 100

# Why a row under the writer id may not be this caller's, by
# KnowledgeWriteAuthor.kind. A bound identity has its own wording below.
_SHARED_WRITER_REASONS = {
    "anonymous": (
        "it is the anonymous id derived from your session's signals, and an "
        "anonymous caller whose session yields the same signals writes under "
        "it too"
    ),
    "anonymous_shared": (
        "it is the anonymous id every caller whose session carries no "
        "identifying signal writes under"
    ),
    "named": (
        "it is the agent_id this call passed, not a binding this session holds, "
        "and any caller can pass it"
    ),
}


def _render_call(tool: str, arguments: Dict[str, Any]) -> str:
    """A call shape for a recovery step: strings quoted, flags as true/false."""
    parts = []
    for key, value in arguments.items():
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, int):
            text = str(value)
        else:
            text = f"'{value}'"
        parts.append(f"{key}={text}")
    return f"{tool}({', '.join(parts)})"


def _knowledge_write_author(call: Any, arguments: Dict[str, Any]) -> Any:
    """The KnowledgeWriteAuthor a timed-out store or note records, else None."""
    try:
        from .knowledge.handlers import resolve_knowledge_write_author

        return resolve_knowledge_write_author(
            arguments, store=call.tool == "knowledge" and call.action == "store"
        )
    except Exception:
        logger.warning(
            "Could not resolve the author of a timed-out knowledge write",
            exc_info=True,
        )
        return None


def _store_batch_size(call: Any, arguments: Dict[str, Any]) -> Optional[int]:
    """How many items a timed-out batch store sent, else None for one row.

    knowledge(action='store') stores a batch whenever ``discoveries`` is
    present, the test its handler dispatches on. 0 stands for a batch whose
    size the arguments do not give.
    """
    if call.tool != "knowledge" or call.action != "store":
        return None
    discoveries = arguments.get("discoveries")
    if discoveries is None:
        return None
    return len(discoveries) if isinstance(discoveries, list) else 0


def _knowledge_store_recovery(
    call: Any,
    arguments: Dict[str, Any],
    *,
    call_started_at: Optional[str],
    settled_by: Optional[str],
) -> Dict[str, Any]:
    """Recovery for a timed-out store or note: list this writer's new rows.

    The check filters on the author and on creation time, not on relevance:
    a queryless search with a created_at window reads the rows newest first
    straight from knowledge.discoveries on both backends (graph.query), so a
    page below the limit is every row that author created in the window,
    whatever its status. The window runs from the call's start to settled_by;
    the row a store or note writes is stamped when the handler builds it,
    before the timeout.

    A batch store (``discoveries``) writes its items one at a time, each
    committed on its own, so a timed-out batch can have saved some items and
    not others. Resending the whole batch would store every item that landed a
    second time, so the caller compares each item with the rows by its summary
    and details together (the lookup carries include_details for the details)
    and resends only the items no row matches. A summary alone cannot tell two
    items apart. Summary and details are the fields stored as sent, except
    that a long one is cut short; discovery_type, severity and tags are
    normalized on the way in (aliases mapped, case folded), so a caller
    comparing them could miss its own row and resend it. Items that agree on
    summary and details cannot be told apart by any row, so once a row
    matches them the recovery says resending cannot be made safe for them.

    Both are database writes: the row commits in one transaction per row
    before the handler returns, so settled_by bounds when it can still land.
    """
    author = _knowledge_write_author(call, arguments)
    writer = _call_literal(author.agent_id, "") if author is not None else ""
    lookup: Dict[str, Any] = {"action": "search"}
    if writer:
        lookup["agent_id_filter"] = writer
    lookup.update(
        {
            "created_after": call_started_at or "<call_started_at>",
            "created_before": settled_by or "<settled_by>",
            "sort_by": "created_at",
            "include_archived": True,
            "include_cold": True,
            "limit": _WINDOW_LOOKUP_LIMIT,
        }
    )
    batch_size = _store_batch_size(call, arguments)
    batch = batch_size is not None
    # Rows are compared by their full content, details included, for a single
    # store as for a batch: the same writer can store two rows with the same
    # summary in the window, and without this a page of more than a few rows
    # carries a 500-character preview.
    lookup["include_details"] = True
    check = _render_call("knowledge", lookup)
    limit = _WINDOW_LOOKUP_LIMIT
    # What a row of this call's would carry.
    is_note = call.tool == "leave_note" or (
        call.tool == "knowledge" and call.action == "note"
    )
    if is_note:
        from .knowledge.limits import MAX_DETAILS_LEN, MAX_SUMMARY_LEN

        # _truncate_note_text runs first: a note over the combined limit is
        # cut to it and marked, and only then split.
        note_total = MAX_SUMMARY_LEN + MAX_DETAILS_LEN

        # _split_note_text: a note up to MAX_SUMMARY_LEN is stored verbatim
        # as the summary; a longer one is split at a nearby sentence or word
        # boundary, or at the limit inside a word when none is near, as
        # text[:p].rstrip() and text[p:].strip(). The trim drops whatever
        # whitespace the split fell on and a cut inside a word had none, so
        # no joiner gives back the text sent. The exact inverse: the text
        # is the summary, only whitespace, the details, only whitespace.
        # Ignoring whitespace everywhere would match a short note to another
        # that differs only in spacing, and suppress a needed resend.
        pair = (
            "your note's text (matched against a row's summary and details "
            "as step 2 says)"
        )
        match = "your note's text"
        same = "the same note"
        yours = (
            f"your note's text: a note up to {MAX_SUMMARY_LEN} characters is "
            "stored exactly as sent as the summary, with empty details, so "
            "compare it exactly. A longer one is split at a nearby sentence "
            "or word boundary, or inside a word when none is near, its start "
            "in summary and the rest in details, both trimmed, so a row is "
            "yours when your text starts with its summary and continues, "
            "after nothing or only whitespace, with its details, followed by "
            f"nothing or only whitespace. A note over {note_total} characters "
            f"is first cut to its first {note_total} characters followed by "
            "'... [truncated]', and that is the text to compare. Any other "
            "row is not this call's"
        )
    else:
        pair = "your summary and details together"
        match = "an item's content" if batch else "your summary and details"
        same = "the same item" if batch else "the same summary and details"
        yours = (
            "your summary and details, both stored as sent except that a long "
            "one is cut short; a row with your summary but other details is "
            "not this call's"
        )
    matching = (
        "Compare each item you sent with the rows by its summary and details "
        "together, not by summary alone. Both are stored as sent, except that "
        "a long one is cut short; the other fields are normalized when "
        "stored, so do not match on them"
    )

    if writer and author.kind == "bound":
        listed = f"every row your bound identity '{writer}' created"
        attribution = (
            "Rows under your identity come from calls bound to it, so a row "
            f"with {match} is your write: this call's, unless you sent {same} "
            "in another call in the window. The exception is a "
            "call that reaches the handler unbound and passes your id as "
            "agent_id on a low or medium write, which no ownership check stops."
        )
        if batch:
            found_step = (
                f"2. {matching}. An item a row matches was saved. Do not send it "
                "again"
            )
        else:
            found_step = (
                f"2. If a row in it has {yours}, it was saved. Do not store it again"
            )
    else:
        if writer:
            listed = f"every row writer '{writer}' created"
            reason = _SHARED_WRITER_REASONS.get(
                author.kind, "another caller can write under it"
            )
            attribution = (
                f"That id is not yours alone: {reason}. A row with {match} "
                "shows a write under it landed, possibly another caller's, so "
                "only its absence is proof."
            )
        else:
            listed = "every row any writer created"
            attribution = (
                "The id this call would be recorded under could not be "
                "resolved, so the list is not filtered by writer: a row with "
                f"{match} may be another writer's, and only its absence is "
                "proof."
            )
        if batch:
            found_step = (
                f"2. {matching}. A row matching an item shows a write like it "
                "landed in the window but may be another caller's. Do not send "
                "that item again unless you know that row is not yours"
            )
        else:
            found_step = (
                f"2. If a row in it has {yours}, a write like yours landed in the "
                "window but may be another caller's. Do not store it again unless "
                "you know that row is not yours"
            )

    listing = (
        f"{check} lists {listed} between call_started_at and settled_by, "
        "newest first and whatever its status: it filters on writer and "
        f"creation time, not relevance. {attribution}"
    )
    rows = "rows" if batch else "row"
    paging_step = (
        f"4. If count is {limit}, older rows in the window, where this "
        f"call's {rows} would be, may be missing: read again with "
        "created_before set to the created_at of the oldest row listed, "
        f"until a read returns fewer than {limit}"
    )
    if batch:
        sent = f"a batch of {batch_size} items" if batch_size else "a batch"
        action = (
            f"Do not send this batch again. It was {sent}, and the store "
            "commits each item on its own, so some items may be saved and "
            "others not. Every store adds a new row, so resending the whole "
            "batch leaves a second finding for every item that landed. "
            f"{listing} Compare each item you sent with those rows by its "
            "summary and details together, not by its summary alone, and send "
            "again only the items no row matches. An item no row matches is "
            "proven unsaved only when the list was read after settled_by and "
            f"its count is below {limit}. Items that agree on summary and "
            "details cannot be told apart: once a row matches them, the list "
            "cannot say which of them landed, and resending cannot be made "
            "safe for them."
        )
        resend_step = (
            "3. On a read after settled_by whose count is below "
            f"{limit}, an item no row matches was not saved: send only those "
            "items again, in one store or a batch of just them. Items that agree "
            "on summary and details cannot be told apart: when a row "
            "matches them, the list cannot say which of them landed, so "
            "resending cannot be made safe for them. Do not resend them blind. "
            "Never resend the whole batch"
        )
    else:
        action = (
            "Do not store this again yet. It may already be saved, and every "
            "store adds a new row, so a second call leaves two findings. "
            f"{listing} A list with no row carrying {pair} proves nothing "
            "was saved, but only when it was read after settled_by and its "
            f"count is below {limit}."
        )
        resend_step = (
            f"3. If no row has {pair} on a read after settled_by and count is "
            f"below {limit}, nothing was saved: store it again. A row that "
            "matches only in part is not this call's"
        )

    workflow = [
        f"1. Call {check}; search needs no bound identity",
        found_step,
        resend_step,
        paging_step,
    ]
    supersedes = arguments.get("supersedes") if not batch else None
    if call.tool == "knowledge" and call.action == "store" and supersedes:
        # The store marks the old row superseded in a second write after its
        # own row commits, which this list does not show. Marking it again
        # sets a status and MERGEs an edge, so it adds nothing twice.
        old = _call_literal(str(supersedes).strip(), "<supersedes>")
        workflow.append(
            f"5. This store also marks '{old}' superseded, a separate write "
            "after the row is saved that this list does not show; a timeout "
            "between the two can leave its status set and its edge missing. "
            "If you found your row, run knowledge(action='supersede', "
            f"discovery_id=<your row's id>, supersedes_id='{old}') whatever "
            f"'{old}' shows: it sets a status and merges an edge, so it adds "
            "nothing twice and repairs a missing edge. Do not store the row "
            "again for it"
        )

    return {
        "action": action,
        "check_before_retry": check,
        "check_arguments": lookup,
        "workflow": workflow,
        "related_tools": ["knowledge", "health_check"],
    }


def _knowledge_update_recovery(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Recovery for a timed-out knowledge update: look for the notes it appends.

    The update commits its row in one transaction; what it does after that is
    derived (the embedding refresh it schedules) or idempotent (the SUPERSEDES
    edge it MERGEs), so settled_by bounds when the row can still change. Every
    field it sends is set, not added to, except resolution_notes, which are
    appended to details as a block headed ``Resolution notes (<time>):``,
    stamped when the handler builds the update, after the call began. A
    resend can duplicate only that block, so the check is for a block stamped
    at or after call_started_at that carries the caller's text: the text
    alone can already be in an earlier block. It is looked for anywhere in
    details, not only at its end, since another writer can append after it.
    The read asks for MAX_UPDATED_DETAILS_LEN characters, the most an update
    stores, and the workflow pages on while has_more says a row is longer.
    The update writes all its fields in one statement, so a found block shows
    the whole update landed and nothing of it is sent again: a partial resend
    carrying the call's details would overwrite the block it just found. An
    update without resolution_notes only sets fields, so it can be sent again
    after settled_by.

    updated_at settles nothing: any writer's update moves it, and an update
    built before this call began can commit after it and set it back.
    """
    from .knowledge.limits import MAX_UPDATED_DETAILS_LEN

    lookup = {
        "action": "details",
        "discovery_id": _call_literal(arguments.get("discovery_id"), "<discovery_id>"),
        "length": MAX_UPDATED_DETAILS_LEN,
    }
    check = _render_call("knowledge", lookup)
    # superseded_by is recorded after the fields commit, as a separate write
    # the details read does not show; a timeout between the two leaves the
    # edge missing. supersede sets a status and merges an edge, so running it
    # once the update's fields are confirmed adds nothing twice.
    superseded_by = arguments.get("superseded_by")
    supersede_step = []
    if superseded_by:
        new = _call_literal(str(superseded_by).strip(), "<superseded_by>")
        old = lookup["discovery_id"]
        supersede_step = [
            f"5. This update also records '{new}' as superseding '{old}', a "
            "separate write after its fields commit that details does not "
            "show; a timeout between the two can leave the edge missing. Once "
            "step 2 or 4 shows the update's fields landed, run "
            f"knowledge(action='supersede', discovery_id='{new}', "
            f"supersedes_id='{old}') whatever the row shows: it sets a status "
            "and merges an edge, so it adds nothing twice and repairs a missing "
            "edge"
        ]
    block = (
        "a line 'Resolution notes (<time>):' with a time at or after "
        "call_started_at, followed by the text of your resolution_notes "
        "(trimmed of surrounding whitespace)"
    )
    return {
        "action": (
            "Do not send this update again yet. It may already be saved, and "
            "resolution_notes append, so a second call adds them twice. Every "
            "other field an update sends is set, not added to, so sending it "
            "again stores no second copy, though it replaces any change "
            "another writer made since. After settled_by, read the discovery "
            f"with {check} and look anywhere in details, not only at its end, "
            f"for the notes block this call would have written: {block}. Your "
            "text alone proves nothing, since an earlier block can hold the "
            "same words, and a block in the window can be another call's that "
            "sent the same notes since call_started_at. If that block is there "
            "and every other field you sent shows the value you sent, the whole "
            "update landed: it writes all its fields in one statement. Only "
            "the block's absence "
            "from the whole of details, read after settled_by, shows it did "
            "not. updated_at settles nothing: any writer's update moves it."
        ),
        "check_before_retry": check,
        "check_arguments": lookup,
        "workflow": [
            f"1. After settled_by, call {check}. If pagination.has_more is "
            "true, read on from pagination.next_offset until it is false, so "
            "you have all of details",
            f"2. If you sent resolution_notes, look anywhere in details for "
            f"{block}. If it is there, notes like yours landed in the window: "
            "this call's, unless another call to this discovery sent the same "
            "notes since call_started_at. If every other field you sent shows "
            "the value you sent, the whole update landed (it writes all its "
            "fields in one statement): do not send any of it again"
            + (" (for superseded_by, see step 5)" if superseded_by else "")
            + ". If you sent only the notes, or a field shows another value, "
            "the block cannot be told apart from another caller's: do not "
            "resend blind",
            "3. If you sent resolution_notes and no such block is in details "
            "on the read after settled_by, your notes are not stored: send the "
            "update again",
            "4. If you sent no resolution_notes, every field you sent is set, "
            "not added to, so sending the update again after settled_by "
            "stores no second copy, though it replaces any change another "
            "writer made since",
        ] + supersede_step,
        "related_tools": ["knowledge", "health_check"],
    }


def _asks_for_new_identity(call: Any, arguments: Dict[str, Any]) -> bool:
    """Whether an identity-minting call asked for a new identity, not a resume.

    Each flag is read the way the handler reads it. onboard (and its aliases)
    coerces force_new (default false) and resume (default true); resume=false
    skips the resume and mints a new identity even when client_session_id
    names a binding. identity reads force_new as sent. Its resume never
    replaces a binding the session holds: dispatch resolves that binding
    before the handler, which reuses it whatever resume says, and the
    identity schema fills an omitted resume with false, so resume=false is
    the ordinary identity read.
    """
    if call.tool == "identity":
        return bool(arguments.get("force_new"))
    if coerce_bool(arguments.get("force_new"), default=False):
        return True
    return not coerce_bool(arguments.get("resume"), default=True)


def _unknown_outcome_recovery(
    call: Any,
    arguments: Dict[str, Any],
    *,
    call_started_at: Optional[str] = None,
    settled_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Recovery for a timed-out call that may have written: read, then decide.

    A step that resends is offered only where settled_by bounds the work (a
    knowledge store, note or update, each one database transaction per row)
    and the named read covers everything the call would have written; it
    comes after settled_by, when a statement still running at the timeout has
    finished, so a resend cannot race the write it would repeat. Any other
    write gets no resend step: its outcome cannot be settled by reading.
    call_started_at and settled_by are the reply's values; a step that needs
    them as literals falls back to placeholders without them.
    """
    tool = call.tool
    action = call.action

    if getattr(call, "mints_identity", False):
        session_id = _call_literal(arguments.get("client_session_id"), "")
        if session_id and not _asks_for_new_identity(call, arguments):
            # A named binding is resumed, not replaced; reading it settles
            # whether anything needs repeating. A call that asked for a new
            # identity takes the minting recovery below: this read finding
            # the old agent_uuid would call a fork that may never have
            # happened settled.
            check = f"identity(client_session_id='{session_id}')"
            return {
                "action": (
                    f"This call named an existing binding. Read it with {check} "
                    "before calling again: if it returns your agent_uuid, the "
                    "binding is in place and start_session is not needed."
                ),
                "check_before_retry": check,
                "workflow": [
                    f"1. Call {check}",
                    "2. If it returns your agent_uuid, the binding is in place. "
                    "Call again only for a change it does not show yet, such as "
                    "a display name",
                    "3. If it does not resolve on a read after settled_by, call "
                    "start_session(force_new=true) once",
                ],
                "related_tools": ["identity", "start_session", "health_check"],
            }
        if tool == "identity":
            return {
                "action": (
                    "This call may have created an identity: identity creates one "
                    "when no binding is proven. With a client_session_id from "
                    "start_session, calling identity(client_session_id=...) reads "
                    "that binding. Without one, calling again can create another "
                    "identity."
                ),
                "check_before_retry": (
                    "identity(client_session_id='<your client_session_id>')"
                ),
                "workflow": [
                    "1. If you have a client_session_id from start_session, call "
                    "identity(client_session_id=...) to read your binding",
                    "2. If you have none, call start_session(force_new=true) once "
                    "instead of calling identity again",
                    "3. If timeouts repeat, call health_check",
                ],
                "related_tools": ["identity", "start_session", "health_check"],
            }
        return {
            "action": (
                "This call may already have created an identity. The "
                "client_session_id it would have returned never reached you, so no "
                "read can find it, and every further call creates another. Call "
                "start_session(force_new=true) once more if you need an identity; "
                "do not retry in a loop. If you already hold a client_session_id "
                "from an earlier start_session, identity(client_session_id=...) "
                "reads that binding instead."
            ),
            "check_before_retry": None,
            "workflow": [
                "1. If you hold a client_session_id from an earlier start_session, "
                "call identity(client_session_id=...) and keep that binding",
                "2. Otherwise call start_session(force_new=true) once more; an "
                "identity the timed-out call created stays unused",
                "3. If that also times out, call health_check before trying again",
            ],
            "related_tools": ["start_session", "identity", "health_check"],
        }

    if tool == "knowledge" and action == "update":
        return _knowledge_update_recovery(arguments)

    if (tool == "knowledge" and action in _KNOWLEDGE_STORE_ACTIONS) or tool == "leave_note":
        # Search serves unbound callers; an anonymous writer's id (anonkg_*) is
        # not a registered agent, so knowledge(action='get', agent_id=...) would
        # refuse the very caller who needs the check. A relevance search would
        # not settle it: its page is ranked and bounded, so the row can fall
        # off it, and another writer's row with the same summary matches too.
        return _knowledge_store_recovery(
            call,
            arguments,
            call_started_at=call_started_at,
            settled_by=settled_by,
        )

    # Any other call: no resend step. This recovery knows neither whether the
    # tool's work is bounded by settled_by (a tool can write a file on an
    # executor thread, hand work to a background task or call another service,
    # all of which outlive the cancelled await) nor what a read would have to
    # cover to prove the call left no effect.
    call_shape = f"{tool}(action='{action}')" if action else tool
    related = [tool, "describe_tool", "health_check"]
    return {
        "action": (
            f"Do not call {call_shape} again blind: it may already have taken "
            "effect, or still be running. Its outcome cannot be settled by "
            "reading alone. settled_by bounds only a database statement "
            "running at the timeout; a tool can also write files, start "
            "background tasks or call other services, and that work can finish "
            "later. Inspect the effect this call would have with a read-only "
            f"tool (describe_tool(tool_name='{tool}') lists the related tools). "
            "If the effect is there, do not call it again. If you cannot find "
            "it, that does not show the call failed: unless you can confirm "
            "the effect is absent everywhere the call writes and cannot still "
            "land, do not send it again."
        ),
        "check_before_retry": f"describe_tool(tool_name='{tool}')",
        "workflow": [
            "1. Inspect the effect this call would have with a read-only tool; "
            "describe_tool lists the related tools",
            "2. If the effect is there, the call took effect. Do not send it "
            "again",
            "3. If you cannot find it, that is not proof the call failed: work "
            "outside the database can land after settled_by, and a read may not "
            "cover everywhere the call writes. Do not send the call again blind",
            "4. If timeouts repeat, call health_check",
        ],
        "related_tools": list(dict.fromkeys(related)),
    }


def unknown_outcome_timeout_error(
    tool_name: str,
    timeout: float,
    *,
    call: Any,
    arguments: Dict[str, Any],
    started_at: float,
) -> TextContent:
    """Timeout of a call that may have written: the outcome is unknown.

    The decorator's timeout cancels the await, not the work. A handler can be
    past its commit, and a COMMIT the server is already executing on the
    ExecutorPool loop lands after the await is cancelled. So the reply says the
    change may have been saved, names the read that settles it, and gives no
    bare retry. ``call`` is the decorators.CallOperation for the interrupted
    call.

    ``settled_by`` is this reply's time plus the pool's per-statement command
    timeout: a database statement still running now has finished by then. It
    bounds nothing else. Work a tool runs on an executor thread, hands to a
    background task or sends to another service can finish later, so only a
    recovery for a write made of database statements relies on it.
    error_code and error_category stay TIMEOUT / system_error, the values
    every timeout already carried.
    """
    import time
    from datetime import datetime, timezone

    from src.db.postgres_backend import COMMAND_TIMEOUT_SECONDS

    def _iso(seconds: float) -> str:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()

    call_started_at = _iso(started_at)
    settled_by = _iso(time.time() + COMMAND_TIMEOUT_SECONDS)
    return error_response(
        f"Tool '{tool_name}' timed out after {timeout} seconds. The outcome is "
        "unknown: a timeout ends the wait for the reply, not the work, so the "
        "change may have been saved.",
        error_code="TIMEOUT",
        error_category="system_error",
        details={
            "outcome": "unknown",
            "operation": call.operation,
            "call_started_at": call_started_at,
            "settled_by": settled_by,
        },
        recovery=_unknown_outcome_recovery(
            call,
            arguments,
            call_started_at=call_started_at,
            settled_by=settled_by,
        ),
    )


def invalid_parameters_error(
    tool_name: str, 
    details: Optional[str] = None,
    param_name: Optional[str] = None
) -> Sequence[TextContent]:
    """Standard error for invalid parameters"""
    message = f"Invalid parameters for tool '{tool_name}'"
    if details:
        message += f": {details}"
    
    error_details = {"error_type": "invalid_parameters", "tool_name": tool_name}
    if param_name:
        error_details["param_name"] = param_name
    
    return [error_response(
        message,
        error_code="INVALID_PARAMETERS",
        error_category="validation_error",
        details=error_details,
        recovery=RECOVERY_PATTERNS["invalid_parameters"],
        context={"tool_name": tool_name, "details": details, "param_name": param_name}
    )]


def validation_error(
    message: str,
    param_name: Optional[str] = None,
    provided_value: Any = None,
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "VALIDATION_ERROR"
) -> Sequence[TextContent]:
    """Standard error for validation failures"""
    details = {"error_type": "validation_error"}
    if param_name:
        details["param_name"] = param_name
    if provided_value is not None:
        details["provided_value"] = str(provided_value)
    
    return [error_response(
        message,
        error_code=error_code,
        error_category="validation_error",
        details=details,
        recovery=RECOVERY_PATTERNS["validation_error"],
        context=context or {}
    )]


def resource_not_found_error(
    resource_type: str,
    resource_id: str,
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "RESOURCE_NOT_FOUND"
) -> Sequence[TextContent]:
    """Standard error for resource not found"""
    return [error_response(
        f"{resource_type.capitalize()} '{resource_id}' not found",
        error_code=error_code,
        error_category="validation_error",
        details={"error_type": "resource_not_found", "resource_type": resource_type, "resource_id": resource_id},
        recovery=RECOVERY_PATTERNS["resource_not_found"],
        context=context or {}
    )]


def system_error(
    tool_name: str,
    error: Exception,
    context: Optional[Dict[str, Any]] = None
) -> Sequence[TextContent]:
    """Standard error for system errors"""
    return [error_response(
        f"System error executing tool '{tool_name}': {str(error)}",
        error_code="SYSTEM_ERROR",
        error_category="system_error",
        details={"error_type": "system_error", "tool_name": tool_name, "exception_type": type(error).__name__},
        recovery=RECOVERY_PATTERNS["system_error"],
        context=context or {}
    )]


def not_connected_error(
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "NOT_CONNECTED"
) -> Sequence[TextContent]:
    """Standard error for MCP connection issues"""
    return [error_response(
        "MCP server connection not available",
        error_code=error_code,
        error_category="system_error",
        details={"error_type": "not_connected"},
        recovery=RECOVERY_PATTERNS["not_connected"],
        context=context or {}
    )]


def missing_client_session_id_error(
    operation: str = "this operation",
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "MISSING_CLIENT_SESSION_ID"
) -> Sequence[TextContent]:
    """Standard error for missing client_session_id"""
    return [error_response(
        f"client_session_id required for {operation}",
        error_code=error_code,
        error_category="validation_error",
        details={"error_type": "missing_client_session_id", "operation": operation},
        recovery=RECOVERY_PATTERNS["missing_client_session_id"],
        context=context or {}
    )]


def session_mismatch_error(
    expected_id: str,
    provided_id: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "SESSION_MISMATCH"
) -> Sequence[TextContent]:
    """Standard error for session identity mismatch"""
    message = f"Session identity mismatch. Expected: {expected_id[:8]}..."
    if provided_id:
        message += f", Provided: {provided_id[:8]}..."
    
    return [error_response(
        message,
        error_code=error_code,
        error_category="auth_error",
        details={
            "error_type": "session_mismatch",
            "expected_resolved_id": expected_id,
            "provided_id": provided_id
        },
        recovery=RECOVERY_PATTERNS["session_mismatch"],
        context=context or {}
    )]


def missing_parameter_error(
    parameter_name: str,
    tool_name: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "MISSING_PARAMETER"
) -> Sequence[TextContent]:
    """Standard error for missing required parameter"""
    message = f"Missing required parameter: '{parameter_name}'"
    if tool_name:
        message += f" for tool '{tool_name}'"
    
    # Enhance message with custom guidance if provided
    if context and "custom_message" in context:
        message += f". {context['custom_message']}"
    
    # Add examples for common tools
    examples = {}
    if tool_name == "leave_note":
        examples = {
            "example": 'leave_note(summary="Your note here")',
            "aliases": "You can also use: 'note', 'text', 'content', 'message', 'insight', 'finding', 'learning'",
            "quick_fix": "Add summary parameter: leave_note(summary='Your note text')"
        }
    elif tool_name == "store_knowledge_graph":
        examples = {
            "example": 'store_knowledge_graph(summary="Discovery description", tags=["tag1"])',
            "quick_fix": "Add summary parameter: store_knowledge_graph(summary='Your discovery')"
        }
    
    details = {
        "error_type": "missing_parameter",
        "parameter": parameter_name,
        "tool_name": tool_name
    }
    if examples:
        details["examples"] = examples
    
    return [error_response(
        message,
        error_code=error_code,
        error_category="validation_error",
        details=details,
        recovery=RECOVERY_PATTERNS["missing_parameter"],
        context=context or {}
    )]


def invalid_parameter_type_error(
    parameter_name: str,
    expected_type: str,
    provided_type: str,
    tool_name: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "INVALID_PARAMETER_TYPE"
) -> Sequence[TextContent]:
    """Standard error for invalid parameter type"""
    message = f"Parameter '{parameter_name}' must be {expected_type}, got {provided_type}"
    if tool_name:
        message += f" for tool '{tool_name}'"
    
    return [error_response(
        message,
        error_code=error_code,
        error_category="validation_error",
        details={
            "error_type": "invalid_parameter_type",
            "parameter": parameter_name,
            "expected_type": expected_type,
            "provided_type": provided_type,
            "tool_name": tool_name
        },
        recovery=RECOVERY_PATTERNS["invalid_parameter_type"],
        context=context or {}
    )]


def permission_denied_error(
    operation: str,
    required_role: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    error_code: str = "PERMISSION_DENIED"
) -> Sequence[TextContent]:
    """Standard error for permission denied"""
    message = f"Permission denied for {operation}"
    if required_role:
        message += f". Required role: {required_role}"
    
    return [error_response(
        message,
        error_code=error_code,
        error_category="auth_error",
        details={
            "error_type": "permission_denied",
            "operation": operation,
            "required_role": required_role
        },
        recovery=RECOVERY_PATTERNS["permission_denied"],
        context=context or {}
    )]


def tool_not_found_error(
    tool_name: str,
    available_tools: list,
    context: Optional[Dict[str, Any]] = None
) -> Sequence[TextContent]:
    """
    Elegant error for unknown tool with fuzzy suggestions.

    Uses difflib to find similar tool names and provides helpful recovery.
    """
    import difflib

    # Find similar tool names (fuzzy match)
    similar = difflib.get_close_matches(tool_name, available_tools, n=3, cutoff=0.4)

    # Build helpful message
    if similar:
        suggestions_str = ", ".join(f"'{s}'" for s in similar)
        message = f"Tool '{tool_name}' not found. Did you mean: {suggestions_str}?"
    else:
        message = f"Tool '{tool_name}' not found."

    # Categorize tools for discovery
    common_tools = [t for t in available_tools if t in {
        'process_agent_update', 'search_knowledge_graph',
        'health_check', 'list_tools', 'describe_tool'
    }]

    return [error_response(
        message,
        error_code="TOOL_NOT_FOUND",
        error_category="validation_error",
        details={
            "error_type": "tool_not_found",
            "requested_tool": tool_name,
            "similar_tools": similar,
            "total_available": len(available_tools)
        },
        recovery={
            "action": "Use list_tools to see all available tools, or try a suggested alternative",
            "related_tools": ["list_tools", "health_check"] + similar[:2],
            "workflow": [
                "1. Check the tool name spelling",
                f"2. Try one of the suggested alternatives: {similar}" if similar else "2. Use list_tools to browse available tools",
                "3. Use describe_tool(tool_name) to see tool details"
            ],
            "suggestions": similar,
            "common_tools": common_tools[:5]
        },
        context=context or {}
    )]
