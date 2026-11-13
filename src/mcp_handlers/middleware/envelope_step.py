"""Post-execution step: normalized agent-experience envelope.

Alias-gated: only calls invoked via an `experience=True` alias in
tool_stability (start_session, sync_state, check_working_state,
search_shared_memory, store_finding, update_finding, record_result,
request_review) get their response reshaped. Canonical tool names stay
byte-identical, so no existing client contract changes.

Envelope shape (friendly fields first, raw payload available on demand):

    {
      "success": ...,
      "tool": "<friendly name as invoked>",
      "agent_uuid": ...,            # lifted when present
      "client_session_id": ...,     # lifted when present
      "action_summary": {...},      # action/reason/risk/evidence at a glance
      "next_action": ...,           # what to do next, concretely
      "state_summary": {...},       # compact working state
      "risk_summary": ...,          # plain-language risk read
      "legacy_diagnostics": {...},  # non-behavioral compatibility telemetry
      "memory_suggestions": [...],  # prior discoveries worth reading
      "response_options": {...},    # which response mode fits which task
      "_response_size": {...},      # approximate serialized size + reduction hint
      "recovery_hint": ...,         # only when state suggests trouble
      "raw_governance": {...}       # full canonical payload when requested
    }

Population is conservative: every field is harvested from values the
canonical handlers already return — this layer reorders and translates,
it does not compute new governance signals. Fields with nothing to say
are omitted. Default read aliases, bounded ``sync_state`` modes and the write
aliases ``store_finding``, ``update_finding`` and ``record_result`` omit the
repeated canonical payload and advertise an explicit full-response escape
hatch; the write aliases first lift the ids and warnings a caller needs next.
A plain fresh ``start_session`` omits it too, without a hint, because the only
way to act on one would be another mint. A routine ``request_review`` (the
review opened and waits on a known move) omits it with a hint naming the
session read; one that carries the review itself retains it. The documented
full mode restores it explicitly (``verbosity="full"`` on
``check_working_state``, ``response_mode="full"`` elsewhere), except on the
finding writes, whose route to the stored record is a
``knowledge(action="details")`` read. That read returns the record as stored, not the ack: write-time warnings
and the store-time similarity snapshot are lifted into the ack itself because
no later read returns them. A routine ``sync_state`` proceed also says each
fact once (``_drop_routine_proceed_duplicates``); guide, pause and provisional
responses keep their full shape.
Error payloads (success=False / "error") generally pass through unchanged:
the raw error contract carries its own recovery info. Checkpoint boundary
responses use a bounded acknowledgement envelope without operational advice.

The step must never break a response: any parse/build failure returns
the original handler result untouched.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from mcp.types import TextContent

from config.governance_config import GovernanceConfig  # noqa: F401
from src.logging_utils import get_logger
from src.state_persistence import project_state_persistence
from src.mcp_handlers.response_formatter import (
    canonical_response_mode,
    normalize_discovery_list,
)
from src.mcp_handlers.support.param_normalization import (
    resolve_metrics_verbosity,
)

logger = get_logger(__name__)

from src.mcp_handlers.middleware.envelope_helpers import (
    _review_risk_limit,
    _stopped_recovery_route,
    _MEMORY_SUMMARY_PREVIEW_CHARS,
    _MEMORY_BY_LABEL_CHARS,
    _MEMORY_TAG_LIMIT,
    _SYNC_ROUTINE_BUDGET_BYTES,
    _SEARCH_LEAN_BUDGET_BYTES,
    _SEARCH_FULL_MODE_HINT,
    _SEARCH_MORE_BUDGET_BYTES,
    _SEARCH_MORE_SUMMARY_CHARS,
    _START_SESSION_BUDGET_BYTES,  # noqa: F401
    _START_SESSION_SIBLING_BUDGET_BYTES,  # noqa: F401
    _START_SESSION_NOTICE_ALLOWANCE_BYTES,  # noqa: F401
    _ONBOARD_RAW_MODES,
    _ONBOARD_LIFT_KEYS,
    _RECORDED_LINEAGE_STATES,
    _ONBOARD_ASSURANCE_KEYS,
    _FRESH_MINT_ASSURANCE_NOTE,
    _lift,
    _harvest_payload,
    _project_checkpoint_receipt,
    _checkpoint_receipt_only,
    _checkpoint_only_envelope,
    _coherence_and_risk,
    _risk_summary,
    _verdict_value,  # noqa: F401
    _decision_action,  # noqa: F401
    _one_line,
    _action_summary,
    _is_routine_proceed,
    _drop_routine_proceed_duplicates,
    _drop_all_clear_status,
    _legacy_diagnostics,
    _metrics_state_summary,
    _metrics_next_action,
    _metrics_identity_assurance,
    _needs_attention,  # noqa: F401
    _compact_eisv,
    _recovery_hint,
    _resolves,  # noqa: F401
    _verdict_caveat,
    _reflection,
    _UNKNOWN_WRITER,
    _DIGEST_ATTRIBUTION_KEYS,
    _summary_preview,
    _compact_related_discoveries,
    _explicit_search_limit,
    _knowledge_write_summary,
    _experience_aliases,  # noqa: F401
    _friendly_action_hint,
    _as_bool,
    _PREDICTION_BOUND_OUTCOME_HINT,
    _UNBOUND_OUTCOME_HINT,
    _is_alias_injected_action,
    _ROUTINE_REVIEW_KEYS,  # noqa: F401
    _review_ack_blockers,
    _review_ack_raw_policy,
    _onboard_assurance_is_abnormal,
    _FULL_ONBOARD_SHAPE_KEYS,
    _RESIDENT_REGISTRATION_STATUSES,  # noqa: F401
    _lift_mint_notices,
    _routine_mint_blockers,
    _is_routine_mint,
    _start_session_budget,
    _effective_discovery_retrieval_options,
    _response_options,
    _mode_in_effect,
    _attach_response_size,
    _prune_resolution_notes_hint,
    _note_mixed_relevance,
    _STALENESS_NOTE,
    _RETRIEVAL_KEPT_FOR_ATTRIBUTION,
    _truncate_search_projection,
    _restore_digest_attribution,
)

# Recovery threshold quoted to agents — the same risk ceiling used by the
# quick_resume contract. Legacy coherence is directional controller feedback
# and is deliberately absent from recovery guidance.









_MEMORY_SUGGESTION_LIMIT = 3
# Bound on a digest's `by` display label. `agent_id` is never bounded: it is
# the identity key a reader passes back as `agent_id_filter`, so a prefix of it
# would silently name nobody (or the wrong writer).
# store_finding's related_discoveries is the store-time similarity snapshot:
# the findings this write resembled when it was stored. Its ids are also the
# stored record's related_to, so the write-time content a details read cannot
# give back is the summary previews. The ack keeps a bounded list of ids and
# short previews; each record is one details read away.
# A lean search shows three digests whatever `limit` asked for, so a caller
# who wanted row four had one route to it: the full tier, which serializes
# every record. The bounded read therefore lists one line per further result
# (id, type, status, short summary) under `more_results`. Until 2026-10-17
# that needed an explicit `limit` above the digest count, and a search that
# passed none showed "3 of 20" with nothing about the other 17: the 2026-10-16
# dogfood read (docs/evaluations/ax-dogfood/read-2026-10-16.md, item 1) paid a
# 32 KB full read to learn they held nothing. Now an unset `limit` lists them
# too, and an explicit `limit` of 3 or less is the way to omit them. The rows
# have their own budget on top of the digest's 3 KB.
_SEARCH_MORE_ROW_LIMIT = 17
# Default start_session, per mint class (tests/test_response_budgets.py states
# the measurements). An anonymous mint at thread position 1: continuity_token
# alone is ~330 B of it. The keyed client_session_id (identity/stable_session.py)
# adds 49 B: 21 characters in the id and its base64 share of the token.
# Both base budgets rose 100 B on 2026-10-17 for _FRESH_MINT_ASSURANCE_NOTE
# (~95 B): a weak tier at mint otherwise reached the agent unexplained.
# A fresh mint on a thread earlier process-instances occupied (sibling_locus)
# also carries predecessor_uuid, episode_fork_kind and the ~210 B sentence
# saying co-location does not establish lineage, which stays: it is what stops
# the earlier node being read as this process's parent.
# Each mint notice lifted into a routine envelope (_ROUTINE_MINT_NOTICES) is
# paid for on top of its class budget; the largest measured is 294 B (a
# not_on_roster verdict), a written bootstrap ack 224 B.
# Onboard fields a caller or adapter reads off the friendly envelope. The
# plugin's post-identity hook and identity_sidecar read agent_id/display_name
# from the top level or from raw_governance, so lifting them is what lets the
# default response drop raw_governance.
# session_resolution_source, when a payload carries it, is what the plugin's
# post-identity hook caches and its identity-contract audit checks.
# The resolution outcomes of a mint that went as asked. A resume miss, a
# reactivated archive or any resumed binding is not routine: the envelope then
# carries the whole onboard record, because those facts exist only in the
# mint's own response (identity() re-reads the binding, not how it was made).
# lineage_state values that mean a live parent edge is on the record: onboard's
# "provisional", derive_lineage_state's "confirmed"
# (src/identity/lineage_lifecycle.py), and "declared" as the lifecycle query
# reports it (src/mcp_handlers/lifecycle/query.py). Rejected, demoted and
# archived edges are not live.
# Compact identity_assurance for a mint that went as asked. The operator guide
# tells agents to confirm the binding from tier and session_source here.
# The minimal onboard payload carries its source only here (it has no
# top-level session_resolution_source), so session_source stays.
# baseline stays: "fresh_identity" is what says a weak mint binding is
# expected, not a deficiency to fix.
# Appended to a fresh mint's next_action when the minting call was not
# caller-proven. Such a call has no session signal of its own (the /mcp/
# transport is stateless and issues no Mcp-Session-Id), so its tier describes
# that call, not the threading of later ones.

_ACTION_ALIASES = {
    "approve": ("proceed", None),
    "continue": ("proceed", None),
    "healthy": ("proceed", None),
    "ok": ("proceed", None),
    "safe": ("proceed", None),
    "caution": ("proceed", "guide"),
    "guide": ("proceed", "guide"),
    "resumed": ("proceed", "resumed"),
    "not_paused": ("proceed", "not_paused"),
    "block": ("pause", "block"),
    "high-risk": ("pause", "high-risk"),
    "reject": ("pause", "reject"),
    "stop": ("pause", "stop"),
}

_COMPACT_READ_ALIASES = frozenset({
    "check_working_state",
    "search_shared_memory",
})

# Write aliases whose acknowledgement omits the repeated canonical payload by
# default. Each one lifts the identifiers a caller needs next (and the warnings
# a writer must see) before the omission, so the ack stays self-sufficient.
# request_review is not one of them: an ack that carries the review itself
# (antithesis, synthesis, recommendation, recusal, facilitation, reviewer-host
# limits) keeps its payload, and only a routine ack omits it
# (_review_ack_raw_policy, #2443). A thesis that was not recorded comes back
# with success=false and passes through whole. start_session is absent too:
# its ack shape is decided separately, with the rest of the identity/onboarding
# surface.
_COMPACT_WRITE_ALIASES = frozenset({
    "store_finding",
    "update_finding",
    "record_result",
})
















































# Decisions that stop the agent (governance_glossary._STOP_ACTIONS).










def _inferred_binding_assurance() -> Optional[Dict[str, Any]]:
    """This request's identity assurance, only when the server inferred it.

    Read from the dispatch context the identity step stamped, through the same
    source-to-tier rule the identity responses use (#679: server-inferred is
    never strong). Imported lazily: the middleware must not load the identity
    services at import time.
    """
    try:
        from ..context import get_session_proof_origin, get_session_resolution_source
        from src.services.identity_payloads import (
            _identity_assurance_from_source,
            _normalize_source,
        )

        if get_session_proof_origin() != "server_inferred":
            return None
        return _identity_assurance_from_source(
            _normalize_source(get_session_resolution_source()),
            "server_inferred",
        )
    except Exception:
        return None






















def _append_more_results(
    envelope: Dict[str, Any],
    source_payload: Dict[str, Any],
    arguments: Optional[Dict[str, Any]],
) -> None:
    """List the results after the digests, one line each.

    Runs after the digest budget has settled. It adds nothing when the caller
    passed a `limit` within the digest count, or when the budget steps had to
    drop a digest: a response that could not afford its third digest has no
    room to promise a fourth result. An unset `limit` lists whatever further
    results the search returned.
    """
    limit = _explicit_search_limit(arguments)
    if limit is not None and limit <= _MEMORY_SUGGESTION_LIMIT:
        return
    suggestions = envelope.get("memory_suggestions")
    if not isinstance(suggestions, list) or len(suggestions) != _MEMORY_SUGGESTION_LIMIT:
        return
    candidates = source_payload.get("results") or source_payload.get("discoveries")
    if not isinstance(candidates, list):
        return
    rows: List[Dict[str, Any]] = []
    used = 0
    window = candidates[_MEMORY_SUGGESTION_LIMIT:limit][:_SEARCH_MORE_ROW_LIMIT]
    for item in window:
        if not isinstance(item, dict):
            continue
        discovery_id = item.get("discovery_id") or item.get("id")
        if discovery_id is None:
            continue
        row: Dict[str, Any] = {"discovery_id": discovery_id}
        row.update(
            _lift(item, "type", "status", "retrieval_tier", "imported_memory", "resolution_notes")
        )
        summary = item.get("summary") or item.get("title")
        if isinstance(summary, str):
            row["summary"], _ = _summary_preview(summary, _SEARCH_MORE_SUMMARY_CHARS)
        size = len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 1
        if used + size > _SEARCH_MORE_BUDGET_BYTES:
            break
        used += size
        rows.append(row)
    if not rows:
        return
    envelope["more_results"] = rows
    state = envelope.get("state_summary")
    if isinstance(state, dict):
        state["more_results_shown"] = len(rows)


def _memory_suggestions(payload: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """Surface bounded discovery digests the canonical payload already carries.

    `relevant_prior_work` is the check-in path's contribution: the formatter
    already builds it from the mirror's KG lookup and puts it in the response,
    but nothing read it here, so `memory_suggestions` stayed empty on the one
    tool that had prior work to offer.

    `relevant_discoveries` arrives as {"message": ..., "discoveries": [...]}
    rather than a bare list, which the previous isinstance check discarded.
    """
    payload = _harvest_payload(payload)
    candidates = normalize_discovery_list(payload.get("relevant_discoveries"))
    from_prior_work = False
    if not candidates:
        from_prior_work = bool(payload.get("relevant_prior_work"))
        candidates = (
            payload.get("relevant_prior_work")
            or payload.get("results")
            or payload.get("discoveries")
        )
    if not isinstance(candidates, list) or not candidates:
        return None
    suggestions = []
    for item in candidates[:_MEMORY_SUGGESTION_LIMIT]:
        if isinstance(item, dict):
            # Freshness travels as the handler's structured
            # `last_activity_days` (days since the last write, present only
            # past the staleness threshold), not its `staleness_warning`
            # sentence: three copies of the same suffix used to outbid
            # attribution for the digest budget. One response-level note
            # explains the field (_note_stale_digests).
            suggestion = _lift(
                item,
                "title",
                "type",
                "status",
                "retrieval_tier",
                "last_activity_days",
                "imported_memory",
                "closure_class",
                "closure_standard",
                "resolution_notes",
            )
            # A superseded row names what replaced it, so "prefer the newer
            # entry" can be followed without a full-mode re-call. First id
            # only; details on this row lists every successor. A core field,
            # not attribution: the budget steps measure it before any
            # attribution is restored.
            successor = item.get("superseded_by")
            if isinstance(successor, list):
                successor = successor[0] if successor else None
            if successor:
                suggestion["superseded_by"] = str(successor)
            discovery_id = item.get("discovery_id") or item.get("id")
            if discovery_id is not None:
                suggestion["discovery_id"] = discovery_id

            # Attribution survives the digest: the canonical result leads with
            # `by` (the write-time label) and carries `_agent_id` (the
            # identity), and a lean reader asking who wrote a finding must not
            # need a second, full-mode call to learn it. The identity is
            # copied whole; only the display label is bounded, and a bounded
            # label says so rather than posing as the complete value.
            # `relevant_prior_work` rows are the one exception: the mirror
            # formatter writes the writer's id into `by` (response_formatter
            # `_format_mirror`), so there `by` is the identity, not a label.
            # "unknown" is the producers' placeholder for a missing id and is
            # neither a label nor an identity.
            by = item.get("by")
            agent_id = item.get("_agent_id") or item.get("agent_id")
            if from_prior_work and not agent_id:
                agent_id, by = by, None
            if isinstance(by, str) and by and by != _UNKNOWN_WRITER:
                if len(by) > _MEMORY_BY_LABEL_CHARS:
                    suggestion["by"] = by[: _MEMORY_BY_LABEL_CHARS - 1] + "…"
                    suggestion["by_truncated"] = True
                else:
                    suggestion["by"] = by
            if agent_id and str(agent_id) != _UNKNOWN_WRITER:
                suggestion["agent_id"] = str(agent_id)

            summary = item.get("summary")
            if isinstance(summary, str):
                preview, truncated = _summary_preview(
                    summary, _MEMORY_SUMMARY_PREVIEW_CHARS
                )
                suggestion["summary"] = preview
                if truncated:
                    suggestion["preview_truncated"] = True

            tags = item.get("tags")
            if isinstance(tags, list):
                suggestion["tags"] = tags[:_MEMORY_TAG_LIMIT]
                if len(tags) > _MEMORY_TAG_LIMIT:
                    suggestion["tags_truncated"] = True

            # Lean projections expose one score plus its basis. Score maps are
            # useful in the canonical response but make a three-result digest
            # surprisingly expensive and force callers to understand ranking
            # internals merely to choose which record to open.
            score_fields = (
                ("relevance", "relevance"),
                ("fusion_score", "fusion"),
                ("rrf_score", "rrf"),
                ("similarity", "semantic"),
                ("score", "score"),
            )
            for field, default_basis in score_fields:
                value = item.get(field)
                if value is not None:
                    suggestion["relevance"] = value
                    suggestion["relevance_basis"] = (
                        item.get("relevance_basis") or default_basis
                    )
                    break
            if "relevance" not in suggestion and item.get("relevance_basis"):
                suggestion["relevance_basis"] = item["relevance_basis"]

            if any(
                key in item for key in ("has_details", "details", "details_preview")
            ):
                suggestion["has_details"] = bool(
                    item.get("has_details")
                    or item.get("details")
                    or item.get("details_preview")
                )
            suggestions.append(suggestion or {"summary": ""})
        else:
            suggestions.append({"summary": str(item)})
    return suggestions or None








# Keys whose values are the caller's own words (a finding's summary, a review's
# reasoning), echoed back. Translating tool names inside them rewrote user text:
# a summary saying "auto-onboard" came back as "auto-start_session".






# record_result hints. There is no read by outcome id, so the only route back
# to an omitted outcome payload is the idempotent replay of a prediction-bound
# outcome; see _write_ack_raw_policy.




def _write_ack_raw_policy(
    friendly_name: str,
    arguments: Dict[str, Any],
    payload: Dict[str, Any],
) -> tuple[bool, Optional[str]]:
    """Decide whether a write acknowledgement repeats its canonical payload.

    Default: omit it. The envelope already lifts the ids a caller needs next,
    so the canonical copy was most of a typical ack and mostly repeated the
    agent signature and the stored record.

    Three things keep the payload inline. An explicit full request does, on
    record_result (``response_mode='full'``; outcome_event's
    ``include_semantics`` is the same request under an older name and survives
    ``/mcp/`` validation). A payload whose record id the envelope cannot lift
    does too, because then the canonical copy is the only place the caller
    could find out what was written. And a finding alias whose caller named an
    action other than the alias's own does, because the result is that
    action's answer, not a write ack (#2457).

    store_finding and update_finding have no full request here. This step sees
    the arguments after canonical validation, and KnowledgeParams fills
    ``response_mode='full'`` by default (the search alias works around the
    same default in normalize_compact_search_details), so a caller's explicit
    'full' and an omitted parameter arrive identical. A later
    knowledge(action='details') read returns the stored record, not
    everything this ack carried: the write-time warnings and a bounded
    related_discoveries snapshot (for its summary previews) are lifted into
    the ack for that reason. The
    canonical knowledge tool still returns the whole payload directly.
    outcome_event validates response_mode to None, so on record_result an
    arriving 'full' was the caller's.

    The hint never tells a caller to repeat a write that would write again: a
    second store mints a second finding. For the finding writes it names a
    details read, which returns the stored record. record_result has no read
    by outcome id. An outcome bound to a prediction_id is the one write a
    repeat does not duplicate under the same identity: while its binding is
    retained, the identical call (with ``client_session_id``) replays the
    stored outcome (``idempotent_replay``) and writes nothing,
    and the idempotency digest does not cover ``response_mode`` or
    ``include_semantics``, so repeating it with ``response_mode='full'``
    returns the full payload. Its hint names that route (a changed outcome is
    refused as PREDICTION_REUSE_CONFLICT, so the repeat must be identical).
    An outcome without a prediction_id has no such route: a repeat records a
    second outcome, so its hint names the full-mode parameter for a later
    outcome and says so.

    None of these acks sets raw_governance_available. Elsewhere that flag
    means a re-call that only reads; here the one route that returns the
    omitted payload is a repeat of the write, safe only for an identical
    prediction-bound call inside the binding's retention, and the hint states
    those conditions where a bare flag could not.
    """
    if friendly_name == "record_result":
        # include_semantics is read the way the handler reads it (the schema
        # lets a string through validation), so the ack keeps the payload
        # exactly when the handler built the full snapshot for it.
        from ..observability.outcome_events import _coerce_bool_flag

        full_mode = (
            str(arguments.get("response_mode") or "").strip().lower() == "full"
        )
        wants_full = full_mode or _coerce_bool_flag(arguments.get("include_semantics"))
        identifiable = payload.get("outcome_id") is not None
        # The handler sets idempotent_replay exactly when the outcome was
        # recorded against a prediction_id (outcome_events), so its presence,
        # not its value, says which route exists.
        hint = (
            _PREDICTION_BOUND_OUTCOME_HINT
            if "idempotent_replay" in payload
            else _UNBOUND_OUTCOME_HINT
        )
        return wants_full or not identifiable, hint

    # store_finding / update_finding: no full request (see above).
    if not _is_alias_injected_action(friendly_name, arguments):
        # The alias injects its action only when the caller sent none, and an
        # explicit one wins (resolve_alias). REST and stdio do not narrow
        # `action` out of these aliases, so update_finding(action='details',
        # discovery_id=...) runs a details read. That result is not a write
        # ack: its payload is what the caller asked for, and the details
        # hint would send it to repeat the read it just made (#2457).
        return True, None
    discovery = payload.get("discovery")
    discovery = discovery if isinstance(discovery, dict) else {}
    discovery_id = (
        payload.get("discovery_id")
        or discovery.get("id")
        or arguments.get("discovery_id")
    )
    target = discovery_id if discovery_id is not None else "..."
    hint = (
        f"knowledge(action='details', discovery_id='{target}') returns the "
        "stored record, not this ack's payload; do not repeat the write to "
        "see it."
    )
    return discovery_id is None, hint


# The keys a request_review ack carries when the review opened as asked and its
# next move is a known one: the reviewer's (assigned, dispatched, or an open
# slot awaiting a claim) or the caller's own thesis call (the two-call shape,
# whose next_call becomes next_action). Each actionable value among them is
# lifted (session_id, phase, whose_move, one_call_review, thesis_source,
# reviewer_agent_id, orchestrated_review, reviewer_dispatch, next_call or
# next_step) or is the caller's own request echoed back (reason, session_type,
# trigger_source, paused_agent_id, reviewed_work_authorship); the attribution in
# agent_signature is lifted as agent_uuid/written_as. `note` restates
# whose_move in prose. An allowlist, not a blocklist, so a key the handler adds
# later keeps the whole payload until someone decides it is routine: the same
# positive-evidence rule as a routine start_session (#2437).
# Phases in which the review has not happened yet: the thesis is owed (two-call)
# or recorded and awaiting the reviewer (one-call). A resolved, failed or
# escalated review is the review's own content and keeps the payload.








# Every key a plain fresh minimal onboard carries (live shape, 2026-09-25).
# An allowlist, not a denylist: onboard adds keys after building the record
# (label_renamed, resident_registration, bootstrap, deprecations, ...), and a
# key neither this list nor _ROUTINE_MINT_NOTICES knows about must show the
# full record rather than be dropped as routine.

# Keys only onboard's full and verbose shapes carry
# (build_onboard_response_data). A caller that asked for that shape gets them
# on a plain mint too, so they never name why the record was kept; a fact
# the full shape shares with the minimal one (label_renamed, a lineage state)
# still does.


# resident_registration statuses (src/grounding/onboard_classifier.py). Every
# one is a normal outcome of a named mint; an unknown status is not routine.




# write_bootstrap's success shape (identity/bootstrap_checkin.py). Any other
# shape (not written, an error, a field this does not know) keeps the record.




# Keys onboard adds after building the record that a mint can carry and still
# be routine, each with the compact form the envelope lifts in its place. Not
# here, so they keep the full record: label_renamed (a refused label is an
# abnormal event), deprecations (the caller used a retired resume path) and
# temporal_context (the narrator has no history to report for a just-created
# identity, so on a mint it would be a surprise worth the whole record).










def _onboard_raw_requested(arguments: Dict[str, Any]) -> bool:
    """The caller asked for the record, by the signals onboard uses to pick
    its own full or verbose shape (_derive_onboard_response_mode)."""
    requested = str(arguments.get("response_mode") or "").strip().lower()
    return requested in _ONBOARD_RAW_MODES or (
        not requested and _as_bool(arguments.get("verbose"), default=False)
    )


def _onboard_built_full_shape(arguments: Dict[str, Any]) -> bool:
    """Whether onboard built its full shape, by onboard's own rule.

    Differs from _onboard_raw_requested for an unknown response_mode:
    onboard then falls back to the verbose flag, so ("detailed", verbose)
    builds the full shape although the envelope sees no explicit request.
    """
    from src.mcp_handlers.identity.handlers import _derive_onboard_response_mode

    return _derive_onboard_response_mode(arguments)[1] == "full"




def _raw_governance_policy(
    friendly_name: str,
    arguments: Optional[Dict[str, Any]],
    payload: Optional[Dict[str, Any]] = None,
) -> tuple[bool, Optional[str]]:
    """Choose whether a friendly alias should repeat its canonical payload.

    Canonical tools are unchanged. Read aliases, bounded ``sync_state`` modes
    and the write aliases in ``_COMPACT_WRITE_ALIASES`` default to their
    bounded experience envelope and retain an explicit full-response escape
    hatch. A routine ``start_session`` or ``request_review`` omits its payload
    and any other one keeps it.
    """
    if friendly_name == "sync_state":
        arguments = arguments or {}
        payload = payload or {}
        requested_mode = canonical_response_mode(
            arguments.get("response_mode") or "auto"
        )
        resolved_mode = payload.get("_mode")

        # Explicit full/verbose always wins. When auto has no resolved marker,
        # retain the historical raw payload: formatter full mode deliberately
        # has no `_mode`, and direct/helper callers may hand this layer a
        # canonical response. Actual compact/mirror/standard/minimal payloads
        # carry `_mode`, so routine calls can safely omit the duplicate.
        include_raw = requested_mode == "full" or (
            requested_mode == "auto" and resolved_mode is None
        )
        # Re-calling sync_state writes another check-in, and no read returns
        # this check-in's decision payload, so the hint names the next call
        # rather than a re-call and the envelope does not set
        # raw_governance_available.
        return include_raw, (
            "Pass response_mode='full' on the next sync_state for diagnostics."
        )

    if friendly_name in _COMPACT_WRITE_ALIASES:
        return _write_ack_raw_policy(friendly_name, arguments or {}, payload or {})

    if friendly_name == "request_review":
        return _review_ack_raw_policy(payload or {})

    if friendly_name == "start_session":
        # A mint that went as asked is fully described by the lifted fields,
        # so repeating the whole onboard record beneath them was the same
        # record twice. That includes a co-located sibling and a named mint's
        # registration verdict (see _routine_mint_blockers). Anything else
        # about the mint (resume miss, reactivated archive, lineage,
        # trajectory, abnormal assurance, a label rename) keeps the record:
        # those facts exist only here. An explicit request always gets it.
        requested = _onboard_raw_requested(arguments or {})
        include_raw = requested or not _is_routine_mint(payload or {})
        # No hint: the only way to act on one is another mint, and a mint that
        # was not routine already carries the record.
        return include_raw, None

    if friendly_name not in _COMPACT_READ_ALIASES:
        return True, None

    arguments = arguments or {}
    if friendly_name == "check_working_state":
        # The tier alone decides. include_state adds no state on any tier
        # (runtime_queries never requests the monitor's state dict), so
        # escalating on it (#1715) only doubled the default read with a copy
        # of itself.
        # The minimal envelope's E/I/S/V are bare values too, so what
        # 'standard' adds is basin, mode and guidance. Before the first
        # check-in no tier has basin or mode, so the hint names only 'full',
        # which still returns the omitted canonical payload.
        verdict = (payload or {}).get("verdict")
        verdict_value = verdict.get("value") if isinstance(verdict, dict) else verdict
        if verdict_value == "unbound":
            # No tier shows agent state until the caller binds, and
            # next_action names that step and why nothing bound, so no tier
            # hint. 'standard' and 'full' still include the raw unbound
            # payload under raw_governance, with its unbound_reason when
            # resolution failed or a resume was refused.
            return resolve_metrics_verbosity(arguments) != "minimal", None
        uninitialized = verdict_value == "uninitialized" or "uninitialized" in str(
            (payload or {}).get("status") or ""
        )
        return resolve_metrics_verbosity(arguments) != "minimal", (
            "Re-call check_working_state(verbosity='full') for the complete "
            "canonical payload; basin and mode appear after the first check-in."
            if uninitialized
            else "Re-call check_working_state(verbosity='standard') for basin and "
            "mode with their meanings and guidance, or verbosity='full' for "
            "the complete canonical diagnostics."
        )

    response_mode = str(
        arguments.get("response_mode") or "lean"
    ).strip().lower()
    return response_mode == "full", _SEARCH_FULL_MODE_HINT












def _enforce_search_projection_budget(
    envelope: Dict[str, Any], *, notes_in_more: bool = False,
) -> None:
    """Keep the friendly KG digest inside its declared wire budget.

    Result summaries and tags are already bounded independently. This final
    guard protects the *whole* response from additive metadata growth. It only
    drops optional mode/retrieval coaching and then lower-ranked digests; the
    result count and explicit expansion route remain visible.
    """

    def wire_bytes() -> int:
        return len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))

    # Attribution never costs a result or its fields. Set it aside, run the
    # budget steps exactly as for an attribution-free payload (which decides
    # which digests and fields survive), then give each survivor back as much
    # attribution as still fits, in rank order: label and identity, else the
    # identity alone, else neither. An identity is only ever whole. One
    # envelope-level marker says something was withheld.
    suggestions = envelope.get("memory_suggestions")
    set_aside: List[Dict[str, Any]] = []
    if isinstance(suggestions, list):
        for item in suggestions:
            snap = {}
            if isinstance(item, dict):
                for key in _DIGEST_ATTRIBUTION_KEYS:
                    if key in item:
                        snap[key] = item.pop(key)
            set_aside.append(snap)

    # The staleness note is written before the budget steps so they measure
    # it, and re-checked after them: it goes if no stale digest survived.
    _note_stale_digests(envelope)
    _note_mixed_relevance(envelope)
    _prune_resolution_notes_hint(envelope, notes_in_more)
    if wire_bytes() > _SEARCH_LEAN_BUDGET_BYTES:
        _truncate_search_projection(envelope, wire_bytes, notes_in_more=notes_in_more)
        _note_stale_digests(envelope)
        _note_mixed_relevance(envelope)
    _restore_digest_attribution(envelope, set_aside, wire_bytes)








def _note_stale_digests(envelope: Dict[str, Any]) -> None:
    """Explain `last_activity_days` once per response, only while a shown
    digest has it.

    The canonical per-row `staleness_warning` sentence is not lifted into the
    digest, so this is the one place the reader learns what the field means
    and that it calls for verification.
    """
    state = envelope.get("state_summary")
    if not isinstance(state, dict):
        return
    suggestions = envelope.get("memory_suggestions")
    if isinstance(suggestions, list) and any(
        isinstance(item, dict) and "last_activity_days" in item
        for item in suggestions
    ):
        state["staleness_note"] = _STALENESS_NOTE
    else:
        state.pop("staleness_note", None)


# The retrieval options a digest keeps when coaching yields to attribution:
# its tier, the open-one route, and the include_details override disclosure.
# A lean envelope carries no `normalized_parameters`, so `requested_tier` and
# `details_omitted_by` are the only in-band notice that include_details=true
# was not honoured; that is a disclosure, not coaching, so it never yields to
# attribution. Over-budget truncation (_truncate_search_projection) still cuts
# retrieval options to current_tier and open_one, as it did before; this
# tuple does not protect the disclosure from that step.








def build_experience_envelope(
    friendly_name: str,
    canonical_name: str,
    payload: Dict[str, Any],
    arguments: Optional[Dict[str, Any]] = None,
    *,
    binding_assurance: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Reshape a successful canonical payload into the experience envelope.

    Pure function over the parsed payload — raises nothing the caller
    can't recover from (callers guard anyway). ``binding_assurance`` is the
    request's identity assurance when the dispatch context has one to report
    (see ``_inferred_binding_assurance``); only the metrics read uses it.
    """
    envelope: Dict[str, Any] = {
        "success": payload.get("success", True),
        "tool": friendly_name,
    }
    envelope.update(_lift(payload, "agent_uuid", "client_session_id"))
    if "agent_uuid" not in envelope and payload.get("uuid") is not None:
        envelope["agent_uuid"] = payload["uuid"]

    source_payload = _harvest_payload(payload)
    checkpoint = (
        _project_checkpoint_receipt(source_payload.get("checkpoint"))
        if canonical_name == "process_agent_update" else None
    )
    if canonical_name == "process_agent_update" and _checkpoint_receipt_only(source_payload, checkpoint):
        return _checkpoint_only_envelope(friendly_name, source_payload, checkpoint, arguments)
    # Resolve the tactical prediction id ONCE, canonical-source first.
    # _harvest_payload treats a nested raw_governance as the canonical payload,
    # so preferring the outer copy could hand back a stale id when a caller
    # passes an already-enveloped response carrying both (codex review, #2123).
    # Both the prose below and the top-level key use this one value.
    prediction_id = source_payload.get("prediction_id") or payload.get("prediction_id")
    if not (isinstance(prediction_id, str) and prediction_id):
        prediction_id = None
    coherence, risk = _coherence_and_risk(source_payload)
    include_raw, raw_hint = _raw_governance_policy(
        friendly_name,
        arguments,
        source_payload,
    )
    retrieval_options = _effective_discovery_retrieval_options(
        friendly_name,
        source_payload,
        include_raw=include_raw,
        arguments=arguments,
    )
    lean_search = (
        friendly_name == "search_shared_memory"
        and _mode_in_effect(friendly_name, arguments, source_payload) == "lean"
    )
    if lean_search and retrieval_options:
        # The routine read needs its tier, a way to open evidence, and any
        # disclosure that an explicit details request was overridden. The
        # full serialization diagnostics and tier ladder remain in compact.
        retrieval_options = _lift(retrieval_options, *_RETRIEVAL_KEPT_FOR_ATTRIBUTION)

    if canonical_name in {"process_agent_update", "get_governance_metrics"}:
        summary = _action_summary(source_payload, risk)
        if summary:
            envelope["action_summary"] = summary
        legacy = _legacy_diagnostics(source_payload)

    bounded_sync = friendly_name == "sync_state" and not include_raw
    sync_mode = source_payload.get("_mode") if bounded_sync else None
    routine_sync = bounded_sync and sync_mode == "compact"
    # The default metrics read teaches the tier ladder once, in its
    # raw_governance_hint, as a bounded sync_state does; response_options
    # reports the tier whenever a non-default one is served.
    bounded_metrics = friendly_name == "check_working_state" and not include_raw
    options = (
        None
        if bounded_sync or bounded_metrics or lean_search
        else _response_options(friendly_name, source_payload, arguments)
    )

    next_action: Any = None
    state_summary: Optional[Dict[str, Any]] = None
    is_knowledge_search = False

    if canonical_name == "onboard":
        next_action = (
            "Save agent_uuid and client_session_id, then check in with "
            "sync_state(response_text='...', complexity=0.5, "
            "client_session_id=...) as you work."
        )
        state_summary = _lift(
            payload,
            "lineage_state",
            "session_key",
            "onboard_origin",
            "onboard_origin_basis",
        )
        predecessor = (
            payload.get("thread_context", {}).get("predecessor", {})
            if isinstance(payload.get("thread_context"), dict)
            else {}
        )
        # Lifted in every mode, so a client reading the top-level fields does
        # not lose them when it asks for response_mode='full'.
        envelope.update(_lift(payload, *_ONBOARD_LIFT_KEYS))
        if payload.get("display_name"):
            envelope["label_is"] = "social_or_cosmetic"
        assurance = payload.get("identity_assurance")
        if isinstance(assurance, dict):
            envelope["identity_assurance"] = (
                assurance
                if _onboard_assurance_is_abnormal(assurance)
                else _lift(assurance, *_ONBOARD_ASSURANCE_KEYS)
            )
            if (
                assurance.get("baseline") == "fresh_identity"
                and assurance.get("caller_proven") is not True
            ):
                # The routine shape drops baseline_note, so without this the
                # agent sees tier=weak beside nothing that says it is expected.
                next_action += _FRESH_MINT_ASSURANCE_NOTE
        token = payload.get("continuity_token")
        if isinstance(token, str) and token:
            # Grouped with its caveat: the token is an advanced same-process
            # rebind proof, not something to attach to ordinary calls.
            envelope["rebind"] = {
                "continuity_token": token,
                "use_only_for": "identity(agent_uuid=..., continuity_token=..., resume=true)",
            }
        # Also in every mode: a named mint's registration verdict, compact.
        envelope.update(_lift_mint_notices(payload))
        envelope["response_shape"] = "full" if include_raw else "routine"
        if include_raw:
            # Name what kept the record, so the agent knows where to look.
            # Absent when the only reason is an explicit full request: the
            # keys that shape adds to every mint are not reasons.
            blockers = _routine_mint_blockers(
                source_payload,
                _FULL_ONBOARD_SHAPE_KEYS
                if _onboard_built_full_shape(arguments or {})
                else frozenset(),
            )
            if blockers:
                envelope["response_shape_reason"] = ", ".join(blockers)
        thread_context = payload.get("thread_context")
        thread_context = thread_context if isinstance(thread_context, dict) else {}
        fork_kind = thread_context.get("episode_fork_kind")
        if fork_kind not in (None, "none"):
            # R6's discriminator stays on the entry-point response when the
            # routine shape drops thread_context.
            state_summary["episode_fork_kind"] = fork_kind
        if isinstance(predecessor, dict) and predecessor.get("uuid"):
            state_summary["predecessor_uuid"] = predecessor["uuid"]
            lineage_fork = thread_context.get("identity_lineage_fork")
            lineage_state = payload.get("lineage_state")
            call_args = arguments or {}
            # The fork flag alone is not a recorded edge: a lineage
            # spawn_reason with no parent_agent_id also sets it, and then the
            # predecessor is only the previous thread node. Only lineage_state
            # says whether an edge was recorded.
            is_lineage_fork = fork_kind == "identity_lineage" and lineage_fork is True
            if is_lineage_fork and lineage_state in _RECORDED_LINEAGE_STATES:
                next_action += (
                    " Declared lineage for this fork is already recorded; do not "
                    "redeclare it on the current session."
                )
            elif isinstance(lineage_state, str) and lineage_state.startswith("rejected"):
                next_action += (
                    f" The declared parent was not recorded as lineage "
                    f"(lineage_state={lineage_state}); the full record says why."
                )
            elif (
                is_lineage_fork
                and lineage_state == "no_lineage_declared"
                and call_args.get("spawn_reason")
                and not call_args.get("parent_agent_id")
            ):
                # Not "pass parent_agent_id now": on a minted identity that
                # means a second mint, and a running process keeps its uuid.
                next_action += (
                    " spawn_reason alone records no lineage: none was recorded "
                    "for this identity, and the earlier thread node is not "
                    "your parent. Keep this identity; a parent is declared "
                    "with parent_agent_id when a session starts."
                )
            elif is_lineage_fork:
                # Archived, demoted or unread lineage keeps or lost its parent
                # for reasons the record holds; name the state, claim nothing.
                next_action += (
                    f" This fork's lineage is lineage_state="
                    f"{lineage_state or 'missing'}; read it from the record "
                    "rather than redeclaring."
                )
            else:
                next_action += (
                    " A prior node in this thread was detected, but thread "
                    "co-location does not establish lineage. Pass parent_agent_id "
                    "only if you are deliberately continuing a process that "
                    "has ended."
                )

    elif canonical_name == "process_agent_update":
        decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
        state_summary = _lift(payload, "status", "health_status")
        # margin_scope and unmeasurable_edges ride WITH margin: a bare
        # "comfortable" would otherwise read as "no limit is near" when an edge
        # was never assessed at all. Lifting them here is what makes the
        # unmeasurable state agent-visible rather than a comment in `details`,
        # which this envelope strips.
        state_summary.update(_lift(decision, "action", "margin", "nearest_edge",
                                   "margin_scope", "unmeasurable_edges"))
        for key, value in _lift(
            payload,
            "action",
            "sub_action",
            "margin",
            "nearest_edge",
            "margin_scope",
            "unmeasurable_edges",
        ).items():
            state_summary.setdefault(key, value)
        # A paused check-in defaults to mirror mode, whose payload carries the
        # policy action only under the `verdict` wrapper, not `decision.action`,
        # so the lifts above missed it and a paused agent reading
        # state_summary.action saw nothing. action_summary already
        # resolved the action from every shape; reuse it here.
        action_summary = envelope.get("action_summary")
        action_summary = action_summary if isinstance(action_summary, dict) else {}
        for key in ("action", "sub_action"):
            if action_summary.get(key) is not None:
                state_summary.setdefault(key, action_summary[key])
        if coherence is not None:
            if legacy:
                # Match get_governance_metrics's inline badge (runtime_queries.py's
                # lite branch) instead of a bare float agents have to cross-reference
                # against the separate legacy_diagnostics block to interpret.
                state_summary["coherence"] = {
                    "value": coherence,
                    "status": "⚪ legacy control feedback (not health-rated)",
                    "source": legacy.get("source", "legacy_tanh_v"),
                    "role": legacy.get("role", "ode_control_feedback"),
                }
            else:
                state_summary["coherence"] = coherence
        if risk is not None:
            state_summary["risk_score"] = risk
        # The outcome account (#2610) rides beside the state it explains. The
        # formatter already decided whether this turn shows it (adverse and
        # changed, or full mode). Bounded shapes drop the raw payload, so lift
        # it there; when raw_governance goes out it already carries the key.
        outcomes_in_e = payload.get("outcomes_in_e")
        if isinstance(outcomes_in_e, dict) and not include_raw:
            envelope["outcomes_in_e"] = outcomes_in_e
        # The reading-coverage account rides the same way: the formatter
        # decided whether this turn shows it (the defaulted set changed).
        reading_coverage = payload.get("reading_coverage")
        if isinstance(reading_coverage, dict) and not include_raw:
            envelope["reading_coverage"] = reading_coverage
        # This check-in's non-fatal warnings (update_response_service mints
        # them, e.g. "confidence dampened" on a weak binding or a failed
        # evidence write; the tool description says to inspect them for
        # silent calibration loss). They are what makes auto resolve to
        # mirror, every bounded formatter passes them through, and no later
        # read returns them, so a bounded shape lifts them; full mode carries
        # them under raw_governance (#2125).
        warnings = source_payload.get("warnings")
        if isinstance(warnings, list) and warnings and not include_raw:
            envelope["warnings"] = list(warnings)
        if state_summary.get("action") == "pause":
            # The generic continuation text below was emitted on pause verdicts
            # too, telling a paused agent to "keep working". Match recovery_hint.
            review_limit = _review_risk_limit()
            if risk is not None and risk >= review_limit:
                # Reviewed recovery records the reflection, then refuses here.
                next_action = (
                    "Paused - stop this line of work and do not continue it. "
                    + _stopped_recovery_route(risk, review_limit, paused=True)
                )
            else:
                next_action = (
                    "Paused - stop this line of work and do not continue it. Call "
                    "self_recovery(action='review', reflection='...') to request "
                    "resumption."
                )
        elif prediction_id:
            # Naming prediction_id here is what makes registry-bound
            # record_result discoverable — otherwise the outcome grades a
            # confidence borrowed from an unrelated earlier turn (fallback
            # binding dominates calibration rows, #2123). The id itself is
            # lifted beside this text, so it is named rather than repeated.
            next_action = (
                "Keep working; sync_state only at a meaningful milestone when "
                "you also hold a judgment the substrate cannot infer. When "
                "an outcome lands, pass this prediction_id to record_result."
            )
        else:
            next_action = (
                "Keep working - sync_state only at a meaningful milestone when "
                "you also hold a judgment the substrate cannot infer, "
                "and record_result(...) when an outcome lands."
            )
        # In-flow review nudge (#1685): the formatter attaches review_suggested
        # (every response mode) when this check-in reported uncertain ground.
        nudge = payload.get("review_suggested")
        if isinstance(nudge, dict):
            phrase = {
                "low_confidence": "low confidence",
                "high_complexity": "high complexity",
                "guide_verdict": "a guide verdict",
            }.get(str(nudge.get("trigger")), "uncertain ground")
            next_action += (
                f" This check-in carried {phrase} - a reviewer can pressure-test "
                "it in one call: request_review(issue_description='...', "
                "reasoning='...'), or consult(brief='...', purpose='critique') "
                "for advisory design critique."
            )

    elif canonical_name == "get_governance_metrics":
        # Preserve the essential EISV read at the friendly surface so compact
        # mode can omit the repeated canonical payload without making the state
        # tool useless.
        state_summary = _metrics_state_summary(payload)
        write_gate = payload.get("write_gate")
        if isinstance(write_gate, dict) and "writes_held" in write_gate:
            # The first read answers "am I paused, will my writes be held"
            # from the gate itself (pause_ttl.describe_write_gate), so a fresh
            # identity's "uninitialized" no longer sends the agent to a write
            # to find out.
            gate_summary = envelope.setdefault("action_summary", {})
            gate_summary.update(_lift(
                write_gate, "paused", "writes_held", "expiry_eligible",
                "held", "expires_at", "note"
            ))
        next_action = _metrics_next_action(payload)
        if next_action is not None:
            # One next step, at the top level. The verdict's step is either
            # the one lifted there or one it outranks (an uninitialized read's
            # {tool, example} step says what the verdict's prose says), and on
            # standard and full raw_governance still carries the verdict.
            state_summary.pop("next_action", None)
        summary = envelope.get("action_summary")
        if isinstance(summary, dict) and summary.get("reason") in {
            text
            for text in (
                # The verdict's meaning, which state_summary already carries.
                _one_line(state_summary.get("meaning")),
                # The payload's guidance: a next step, not a reason. It is the
                # top-level next_action, or a pause-blind one a stop decision
                # outranks (raw_governance keeps it on standard and full).
                _one_line(payload.get("guidance")),
            )
            if text
        }:
            summary.pop("reason")
        assurance = _metrics_identity_assurance(payload, binding_assurance)
        if assurance:
            envelope["identity_assurance"] = assurance
        coherence_badge = state_summary.get("coherence")
        if (
            legacy
            and isinstance(coherence_badge, dict)
            and coherence_badge.get("source") == legacy.get("source")
            and coherence_badge.get("role") == legacy.get("role")
            and coherence_badge.get("value") == legacy.get("coherence")
            and "not health-rated" in str(coherence_badge.get("status") or "")
        ):
            # The inline badge already says this reading is legacy control
            # feedback, not a health rating. Before the first check-in its
            # status reads "pending" instead, so legacy_diagnostics stays
            # there and carries the disclosure.
            legacy = None

    elif canonical_name == "knowledge" and friendly_name in {
        "store_finding",
        "update_finding",
    }:
        discovery_id, state_summary = _knowledge_write_summary(
            source_payload,
            arguments,
        )
        message = source_payload.get("message")
        if message is not None:
            envelope["message"] = message
        if discovery_id is not None:
            envelope["discovery_id"] = discovery_id
        # Write-time warnings and outcomes the handler reports beside the
        # record. With raw_governance omitted by default these would otherwise
        # vanish: a failed supersession, truncated content, an anonymous
        # writer, or a closure that declares no standard.
        envelope.update(_lift(
            source_payload,
            "agent_mode",
            "_identity_hint",
            "superseded",
            "_supersedes_warning",
            "superseded_by",
            "supersession_warning",
            "_name_hint",
            "_truncated",
            "_tip",
            "consolidation_hint",
            "closure_class_note",
        ))
        if "closure_class" in source_payload:
            envelope["closure_class"] = source_payload["closure_class"]
        # The store-time similarity snapshot. Its ids are the stored record's
        # related_to, which a details read returns; its summary previews are
        # what the details read cannot give back, so a bounded form stays in
        # the ack (consolidation_hint, lifted above, summarizes the same set).
        related, related_total = _compact_related_discoveries(source_payload)
        if related:
            envelope["related_discoveries"] = related
            if related_total is not None:
                envelope["related_discoveries_total"] = related_total
            if friendly_name == "store_finding":
                # store sets discovery.related_to to exactly these rows' ids,
                # so the summary copy would carry the same list twice.
                state_summary.pop("related_to", None)

        call_arguments = arguments or {}
        if not _is_alias_injected_action(friendly_name, call_arguments):
            # An explicit other action (details, get, supersede, update...)
            # ran that action, not this alias's own, so the alias's
            # "updated"/"stored" line would misreport it. The other action may
            # itself write (store, update, supersede, note...), so the line
            # names what ran and makes no claim about whether it wrote. Its
            # payload stays inline (#2457). It reports what ran, so it is not
            # written as a call: the hint rewrite would turn a call into a
            # use_tool route the caller never took.
            ran = str(call_arguments.get("action")).strip().lower()
            next_action = (
                f"{friendly_name} ran the knowledge router's '{ran}' action because an "
                "explicit action was passed, not this alias's own action. "
                "That action's full response is under raw_governance; read "
                "it there before repeating the call, since a writing action "
                "writes again."
            )
        elif friendly_name == "store_finding":
            next_action = source_payload.get("_resolve_when_done")
            if not next_action:
                suffix = (
                    f"discovery_id='{discovery_id}'"
                    if discovery_id is not None
                    else "discovery_id='...'"
                )
                next_action = (
                    "Finding stored. When it is addressed, close the loop with "
                    f"update_finding({suffix}, status='resolved', "
                    "resolution_notes='...')."
                )
        else:
            status = state_summary.get("status")
            target = discovery_id or "..."
            if status in {
                "resolved",
                "closed",
                "wont_fix",
                "archived",
                "superseded",
                "cold",
            }:
                next_action = (
                    f"Finding '{target}' is now '{status}'. Read the final record "
                    "with knowledge(action='details', "
                    f"discovery_id='{target}')."
                )
            else:
                next_action = (
                    f"Finding '{target}' was updated. Read it with "
                    "knowledge(action='details', "
                    f"discovery_id='{target}'); when addressed, close it with "
                    f"update_finding(discovery_id='{target}', status='resolved', "
                    "resolution_notes='...')."
                )

    elif canonical_name == "knowledge":
        is_knowledge_search = True
        candidates = source_payload.get("results") or source_payload.get("discoveries") or []
        total = source_payload.get("total_count")
        if total is None:
            total = source_payload.get("count")
        if total is None:
            total = len(candidates)
        low_confidence = bool(source_payload.get("low_confidence"))
        if low_confidence:
            next_action = (
                f"{total} exploratory low-confidence discoveries surfaced. "
                "Treat them as possible leads, not authoritative matches; "
                "rephrase with distinctive terms or open details before using them."
            )
        elif total:
            next_action = (
                f"{total} prior discoveries matched - read before redoing work. "
                "Full context: knowledge(action='details', discovery_id=...). "
                "Record new findings: knowledge(action='store', summary='...'). "
                "Revise one: knowledge(action='update', discovery_id='...', "
                "summary='...')."
            )
        else:
            next_action = (
                "No prior discoveries matched. Broaden terms or search tags before "
                "recording a new finding."
            )
        if lean_search and total:
            # Search is a read: write recipes belong in the write tools.
            # The structured open_one/full routes below already explain
            # expansion, while confidence_note keeps the actual caveat.
            next_action = (
                "Verify these exploratory matches before acting."
                if low_confidence else "Read relevant details before acting."
            )
        state_summary = _lift(
            source_payload,
            "count",
            "total_count",
            "search_mode_used",
            "search_mode_requested",
            "operator_used",
            "low_confidence",
            "search_degraded",
            "tag_filter_dropped",
            # Present only when a queryless listing left imported memory out;
            # the hint names the two ways to read that lane.
            "imported_omitted",
            "imported_omitted_hint",
            # How the list was ordered and windowed. Present only when the
            # caller changed the default, so the default digest is unchanged;
            # without them the friendly alias silently dropped the echo.
            "sort_by",
            "created_after",
            "created_before",
            "recency_half_life_days",
            # The limit the caller asked for, present only when the server
            # clamped it to its maximum. The handler keeps it in the lean
            # projection so a clamped caller can tell truncation from "that
            # was all"; count alone cannot (#2125).
            "limit_clamped_from",
        )
        # Present only when a short page was filled from archived/cold rows.
        # The counts ride in the digest; the full note stays in full mode, and
        # each such row carries retrieval_tier.
        archived_cold = source_payload.get("archived_cold_fallback")
        if isinstance(archived_cold, dict):
            state_summary["archived_cold_fallback"] = _lift(
                archived_cold, "primary_count", "added", "statuses_searched", "score_penalty"
            )
        # Present only when the second pass failed, timed out or was skipped
        # for lack of time: the page is first-pass rows alone, and says why.
        archived_cold_error = source_payload.get("archived_cold_fallback_error")
        if isinstance(archived_cold_error, str) and archived_cold_error:
            state_summary["archived_cold_fallback_error"] = archived_cold_error
        # `confidence_note` is not copied here: the envelope lifts it to the
        # top level for every alias (next to search_degraded_message), and a
        # second ~270 B copy was paid out of the lean digest's budget before
        # attribution. The `low_confidence` flag stays in both places.
        if retrieval_options and retrieval_options.get("current_tier"):
            state_summary["result_tier"] = retrieval_options["current_tier"]
        state_summary["results_shown_in_digest"] = min(
            len(candidates),
            _MEMORY_SUGGESTION_LIMIT,
        )
        if len(candidates) > _MEMORY_SUGGESTION_LIMIT:
            state_summary["result_set_truncated"] = True

    elif canonical_name == "outcome_event":
        state_summary = _lift(
            payload,
            "outcome_id",
            "outcome_type",
            "outcome_score",
            "recorded_at",
            "corroboration_grade",
            "evidence_weight",
            "claim_risk",
            "corroboration_reasons",
            "corroboration_hint",
        )
        snapshot = payload.get("eisv_snapshot")
        if isinstance(snapshot, dict):
            compact = _compact_eisv(snapshot)
            if compact:
                state_summary["working_state"] = compact
        # Binding disclosure. The producer returns prediction_binding /
        # prediction_source / calibration_excluded precisely so a caller can
        # tell a registry-bound outcome from one that fell back to scraped
        # confidence — and a scraped-confidence row is excluded from
        # calibration entirely. Confined to raw_governance, those three left
        # the friendly surface reporting "Outcome recorded" identically either
        # way, so an agent could not tell a counted outcome from a dropped one.
        binding = _lift(
            source_payload,
            "prediction_binding",
            "prediction_source",
            "calibration_excluded",
        )
        state_summary.update(binding)
        # A replayed prediction_id and claims the grader could not verify
        # change what the recorded outcome means; the default ack no longer
        # repeats the canonical payload, so they ride here.
        for key, value in _lift(
            source_payload, "is_bad", "idempotent_replay", "unverified_fields"
        ).items():
            if value != []:
                state_summary[key] = value
        next_action = "Outcome recorded - continue, or sync_state to fold it into your working state."
        if binding.get("calibration_excluded"):
            next_action = (
                "Outcome recorded, but stamped calibration_excluded (binding: "
                f"{binding.get('prediction_binding') or 'unknown'}), so it does "
                "not train calibration. Pass the prediction_id from the "
                "sync_state you are grading to bind the outcome to it."
            )

    elif canonical_name == "dialectic":
        state_summary = _lift(
            payload,
            "session_id",
            "phase",
            "reviewer",
            "reviewer_agent_id",
            "whose_move",
            "one_call_review",
            "thesis_source",
            "review_verdict",
            # Who is reviewing when an independent reviewer process was
            # spawned. A routine ack omits raw_governance, and these were
            # only there (the dialectic canary records orchestrated_review).
            "orchestrated_review",
            "reviewer_dispatch",
        )
        if friendly_name == "request_review":
            # routine: the payload was omitted because the review opened as
            # asked (_review_ack_raw_policy). full: it was kept, and the
            # reason names what kept it, so "absent by design" and "absent
            # by a bug" read differently.
            envelope["response_shape"] = "full" if include_raw else "routine"
            if include_raw:
                blockers = _review_ack_blockers(source_payload)
                if blockers:
                    envelope["response_shape_reason"] = ", ".join(blockers)
        session_id = state_summary.get("session_id", "...")
        next_action = payload.get("next_call") or payload.get("next_step")
        if not next_action:
            whose_move = str(payload.get("whose_move") or "").strip()
            phase = str(payload.get("phase") or "").lower()
            if whose_move:
                next_action = whose_move
            elif phase in {"resolved", "failed", "escalated"}:
                verdict = payload.get("review_verdict") or phase
                next_action = (
                    f"Review session {session_id} is {phase} ({verdict}); follow "
                    "the recorded verdict or resolution."
                )
            else:
                next_action = (
                    "Review session open - advance it without copying the saved "
                    f"brief: dialectic(action='thesis', session_id='{session_id}', "
                    "use_brief_as_thesis=true)."
                )

    if next_action is not None:
        envelope["next_action"] = _friendly_action_hint(next_action)

    # Surface the tactical prediction id as a machine-readable field, not only
    # inside the next_action prose #1696 added. Every other formatter already
    # passes it through top-level (response_formatter _format_standard /
    # _format_mirror / _format_compact; minimal opts out on bandwidth grounds),
    # and update_response_service mints it onto the base payload with a
    # docstring naming top-level placement as the contract — this envelope,
    # which rebuilds the response from scratch, was the sole surface dropping
    # it. What an unbound record_result costs: prediction_source falls back to
    # "prev_confidence_fallback", which is in SCRAPED_PREDICTION_SOURCES, so
    # the row is stamped calibration_excluded and never reaches the tactical
    # lane (outcome_events.py); and decision_action for test_passed/test_failed
    # is hardcoded "proceed" rather than read off the registered prediction.
    # The prose stays — it tells the agent what the id is for (#2123).
    if prediction_id and canonical_name == "process_agent_update":
        envelope["prediction_id"] = prediction_id
    if canonical_name == "process_agent_update":
        # A nested canonical payload owns the acknowledgement. Do not revive
        # an outer stale receipt when that payload contains no valid receipt.
        persistence = project_state_persistence(source_payload.get("state_persistence"))
        if persistence is not None:
            envelope["state_persistence"] = persistence
        if checkpoint is not None:
            envelope["checkpoint"] = checkpoint
    if state_summary:
        # state_summary can carry glossary coaching (e.g. an uninitialized
        # verdict's "Submit one process_agent_update...") — translate it like
        # next_action so canonical names don't leak through the friendly
        # surface (dogfood 2026-08-20 register-mismatch report).
        envelope["state_summary"] = _friendly_action_hint(state_summary)

    risk_text = _risk_summary(coherence, risk)
    if risk_text:
        envelope["risk_summary"] = risk_text

    # Provisional-verdict caveat: pull the cold-start / non-discriminative
    # self-disclosure out of risk_attribution so a reader of state_summary
    # alone is not misled by a clean 'safe'/'proceed'. Also flag it inside
    # state_summary itself, where the skimmer actually looks.
    caveat = _verdict_caveat(
        source_payload,
        include_raw=include_raw,
        state_summary=envelope.get("state_summary"),
    )
    if caveat:
        envelope["verdict_caveat"] = caveat
        summary = envelope.get("state_summary")
        if isinstance(summary, dict):
            summary["verdict_provisional"] = True

    # Keep compatibility and verbosity metadata after the operational answer.
    if (
        canonical_name in {"process_agent_update", "get_governance_metrics"}
        and legacy
        and not routine_sync
    ):
        envelope["legacy_diagnostics"] = legacy
    if options:
        envelope["response_options"] = options

    reflection = _reflection(source_payload)
    if reflection:
        envelope["reflection"] = reflection

    suggestions_enabled = (
        canonical_name != "process_agent_update"
        or _as_bool((arguments or {}).get("include_memory_suggestions"), default=False)
    )
    suggestions = _memory_suggestions(payload) if suggestions_enabled else None
    # When the full canonical discoveries list is about to go out under
    # raw_governance, a trimmed top-N copy under memory_suggestions is a
    # strict subset of it in the same response — drop the duplicate rather
    # than serialize the same results twice.
    if suggestions and not (is_knowledge_search and include_raw):
        envelope["memory_suggestions"] = suggestions
    if retrieval_options:
        envelope["discovery_retrieval_options"] = _friendly_action_hint(
            retrieval_options
        )
    for key in (
        "low_confidence",
        "confidence_note",
        "resolution_notes_hint",
        "search_degraded",
        "search_degraded_message",
    ):
        value = source_payload.get(key)
        if value is not None:
            # Server-written hints, not caller text: route any tool call they
            # name the way next_action is routed (a knowledge(...) read is
            # reachable on the progressive surface only through use_tool).
            envelope[key] = _friendly_action_hint(value) if isinstance(value, str) else value

    hint = _recovery_hint(source_payload, coherence, risk)
    if hint:
        envelope["recovery_hint"] = hint

    if (
        friendly_name in _COMPACT_WRITE_ALIASES or friendly_name == "request_review"
    ) and not include_raw:
        # A handler-side identity warning (e.g. record_result's
        # ephemeral_writer) must not vanish with the omitted payload. The
        # identity-warning step runs after this one and appends to this list.
        warnings = source_payload.get("identity_warnings")
        if isinstance(warnings, list) and warnings:
            envelope["identity_warnings"] = list(warnings)
        # Who the write was recorded under. The finding, outcome and review
        # payloads carry no top-level uuid: their attribution is success_response's
        # agent_signature, which rode only under raw_governance. A caller that
        # bound weakly (no client_session_id, a transport-fingerprint pin) has
        # to be able to see which identity it wrote as, and how well that
        # binding was proven, without asking for the full payload.
        if "agent_uuid" not in envelope:
            signature = source_payload.get("agent_signature")
            if isinstance(signature, dict) and signature.get("uuid"):
                envelope["agent_uuid"] = signature["uuid"]
                written_as = _lift(signature, "agent_id", "display_name")
                assurance = signature.get("identity_assurance")
                if isinstance(assurance, dict):
                    written_as.update(_lift(
                        assurance, "tier", "caller_proven", "proof_origin"
                    ))
                if written_as:
                    envelope["written_as"] = written_as

    if include_raw:
        raw_payload = payload
        if canonical_name == "process_agent_update" and "checkpoint" in source_payload:
            # Full mode retains the canonical assessment, but the new receipt
            # remains the same bounded projection as its top-level copy.
            canonical_copy = dict(source_payload)
            canonical_copy.pop("checkpoint", None)
            if checkpoint is not None:
                canonical_copy["checkpoint"] = checkpoint
            if source_payload is payload:
                raw_payload = canonical_copy
            else:
                raw_payload = {**payload, "raw_governance": canonical_copy}
                raw_payload.pop("checkpoint", None)
        envelope["raw_governance"] = raw_payload
    else:
        # raw_governance_available promises a re-call that fetches the
        # omitted payload by reading, so only the read aliases claim it: the
        # re-call their hint names is the same read at a fuller tier. Every
        # bounded write's only route to its omitted payload writes again.
        # A routine start_session's record cannot be fetched afterwards (only
        # another mint would produce one). A bounded sync_state's payload is
        # this check-in's decision (its reason, margin, policy gates,
        # enforcement, prediction_id, warnings), and no read returns it:
        # check_working_state(verbosity='full') and get_governance_metrics
        # report the agent's current state, not what this check-in decided,
        # so the hint names response_mode='full' on the next check-in, which
        # is a new check-in. The write acks have no such read either: a
        # finding's details read returns the stored record, not this ack's
        # payload, and record_result has no read by outcome id. Its one route
        # back is a repeat of the write, which replays instead of writing only
        # for an identical prediction-bound outcome while the binding is
        # retained; a bare flag cannot carry those conditions, so the hint
        # states them (see _write_ack_raw_policy). A routine request_review's
        # read (dialectic get) returns the session as it stands later, not
        # this ack.
        # An unbound metrics read (no tier hint) has nothing more to fetch
        # at any tier until the caller binds.
        if friendly_name in _COMPACT_READ_ALIASES and not (
            friendly_name == "check_working_state" and raw_hint is None
        ):
            envelope["raw_governance_available"] = True
        if raw_hint:
            # A finding write's hint names a knowledge(action='details') read
            # and a review's a dialectic(action='get') read; route it like
            # next_action so it names a reachable call.
            envelope["raw_governance_hint"] = _friendly_action_hint(raw_hint)
    if routine_sync and _is_routine_proceed(envelope):
        _drop_routine_proceed_duplicates(envelope)
    if canonical_name == "process_agent_update":
        _drop_all_clear_status(envelope)
    bounded_search =friendly_name == "search_shared_memory" and not include_raw
    bounded_start = friendly_name == "start_session" and not include_raw
    if bounded_search:
        # Preview the separately bounded extra rows to reserve coaching only
        # if a note is reachable there. If budgeting later drops a digest,
        # extras are withheld too, and the hint is reconciled at that step.
        more_preview = {"memory_suggestions": envelope.get("memory_suggestions")}
        _append_more_results(more_preview, source_payload, arguments)
        notes_in_more = any("resolution_notes" in row for row in more_preview.get("more_results", []))
        _enforce_search_projection_budget(envelope, notes_in_more=notes_in_more)

    # Routine sync responses should spend their budget on the decision, not on
    # a size receipt about the decision. Keep the receipt when the projection
    # itself misses its contract so the regression is visible in-band; other
    # tools retain their existing always-on measurement behavior.
    if bounded_sync:
        measured_bytes = len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
        budget = _SYNC_ROUTINE_BUDGET_BYTES if routine_sync else 4_000
        if measured_bytes > budget:
            _attach_response_size(envelope, friendly_name, arguments, source_payload)
    elif bounded_search:
        measured_bytes = len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
        if measured_bytes > _SEARCH_LEAN_BUDGET_BYTES:
            _attach_response_size(envelope, friendly_name, arguments, source_payload)
        else:
            # After the 3 KB check, which is the digest's contract: these
            # rows are bounded by their own budget, not the digest's.
            _append_more_results(envelope, source_payload, arguments)
    elif bounded_start:
        measured_bytes = len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
        if measured_bytes > _start_session_budget(envelope):
            _attach_response_size(envelope, friendly_name, arguments, source_payload)
    else:
        _attach_response_size(envelope, friendly_name, arguments, source_payload)
    return envelope


def _refusal_with_friendly_tool(payload: dict, invoked: str, result):
    """Pass a typed refusal through, but name the tool the caller actually
    invoked.

    The refusal's ``tool`` is the canonical name (``process_agent_update``)
    because that is what the emission point knows. An agent that called
    ``sync_state`` should not be answered about a tool it never called — the
    same register-mismatch the envelope already guards against for
    ``next_action`` and ``state_summary`` (dogfood 2026-08-20). The Python SDK
    surfaces this field verbatim in ``IdentityRefusedError``, so it reaches a
    human. Nothing else is touched, and the payload is only rebuilt when the
    name actually differs.
    """
    if not invoked or payload.get("tool") == invoked:
        return result
    friendly = dict(payload)
    friendly["tool"] = invoked
    return [
        TextContent(type="text", text=json.dumps(friendly, ensure_ascii=False)),
        *result[1:],
    ]


async def _apply_experience_envelope(name: str, arguments: Dict[str, Any], ctx, result):
    """POST_EXECUTION step. `name` is the canonical (post-alias) tool;
    the invoked name lives in ctx.original_name. Returns the (possibly
    reshaped) handler result; on ANY failure returns it untouched."""
    try:
        from ..identity_bootstrap import identity_refusal_status
        from ..tool_stability import is_experience_alias

        invoked = getattr(ctx, "original_name", None)
        if not invoked or not is_experience_alias(invoked):
            return result

        if not (isinstance(result, (list, tuple)) and result and hasattr(result[0], "text")):
            return result
        payload = json.loads(result[0].text)
        if not isinstance(payload, dict):
            return result
        checkpoint_error = (
            name == "process_agent_update"
            and "checkpoint" in _harvest_payload(payload)
        )
        if (payload.get("success") is False or "error" in payload) and not checkpoint_error:
            return result  # raw error contract carries its own recovery info

        # A #425 typed identity refusal is success-SHAPED (`success: true`, no
        # `error` key), so the guard above does not catch it and the refusal
        # was being rebuilt as an ordinary check-in.
        #
        # Precisely: the recovery block was RELOCATED, not destroyed —
        # `hint` / `next_step` / `safe_options` / `do_not` / `status` all
        # survived under `raw_governance`, and `status` was also lifted into
        # `state_summary`. Two things were actually wrong. `next_action`
        # became the generic "Keep working - sync_state again after your next
        # substantial step", because `decision.get("action")` is None for a
        # refusal — an agent whose write was refused was told to continue. And
        # both shipped SDKs read the refusal at the TOP level, which the
        # envelope no longer had: `agents/sdk/.../errors.py` keys on
        # `rollout_flag`, and `elixir/unitares_sdk/.../envelope.ex` keys on
        # `status == "identity_required"` with two production outages pinned
        # as tests. Enveloping a refusal broke the detection both rely on.
        #
        # So the refusal passes through as its own contract. Everything the
        # envelope would add is derived from governance state that is never
        # computed before identity is proven, so there is nothing to lose.
        if identity_refusal_status(payload) is not None:
            return _refusal_with_friendly_tool(payload, invoked, result)

        envelope = build_experience_envelope(
            invoked,
            name,
            payload,
            arguments,
            binding_assurance=(
                _inferred_binding_assurance()
                if name == "get_governance_metrics"
                else None
            ),
        )
        return [TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False))]
    except Exception:
        logger.warning(
            "experience envelope failed for %r - returning raw response",
            getattr(ctx, "original_name", name),
            exc_info=True,
        )
        return result


async def apply_experience_envelope(name: str, arguments: Dict[str, Any], ctx, result):
    """Project the response, then attach bounded caller context and notices."""
    from src.knowledge_notices import attach_notices
    from src.services.model_observations import attach_model_observation

    projected = await _apply_experience_envelope(name, arguments, ctx, result)
    projected = await attach_model_observation(projected)
    return await attach_notices(name, arguments, ctx, projected)
