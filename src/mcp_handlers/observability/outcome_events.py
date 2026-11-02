"""MCP adapters for outcome recording and correlation queries."""

from typing import Dict, Any, Sequence

from mcp.types import TextContent

from src import outcomes
from src.logging_utils import get_logger
# Preserve the former helper/constant imports while the owner is extracted.
from src.outcomes import (
    BAD_OUTCOME_TYPES as BAD_OUTCOME_TYPES,
    GOOD_OUTCOME_TYPES as GOOD_OUTCOME_TYPES,
    NEUTRAL_OUTCOME_TYPES as NEUTRAL_OUTCOME_TYPES,
    VALID_OUTCOME_TYPES as VALID_OUTCOME_TYPES,
    HARD_EXOGENOUS_TYPES as HARD_EXOGENOUS_TYPES,
    _MIN_TACTICAL_EVIDENCE_WEIGHT as _MIN_TACTICAL_EVIDENCE_WEIGHT,
    _prediction_request_digest as _prediction_request_digest,
    _HARD_EXOGENOUS_DETAIL_KEYS as _HARD_EXOGENOUS_DETAIL_KEYS,
    _CONTROLLED_FIXTURE_FLAGS as _CONTROLLED_FIXTURE_FLAGS,
    _CONTROLLED_FIXTURE_BINDINGS as _CONTROLLED_FIXTURE_BINDINGS,
    _SERVER_SCRAPED_PREDICTION_SOURCES as _SERVER_SCRAPED_PREDICTION_SOURCES,
    _HARD_EXOGENOUS_TYPE_TO_CHANNEL as _HARD_EXOGENOUS_TYPE_TO_CHANNEL,
    _coerce_bool_flag as _coerce_bool_flag,
    _LITE_SNAPSHOT_KEYS as _LITE_SNAPSHOT_KEYS,
    _lite_eisv_snapshot as _lite_eisv_snapshot,
    _classify_hard_exogenous_signal as _classify_hard_exogenous_signal,
    _truthy_detail_flag as _truthy_detail_flag,
    _is_controlled_validation_fixture as _is_controlled_validation_fixture,
    _TRUSTED_INGESTION_KEY as _TRUSTED_INGESTION_KEY,
)

from ..utils import success_response, error_response
from ..decorators import mcp_tool

logger = get_logger(__name__)


async def _record_outcome_event_inline(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Compatibility entry point; outcome recording lives in its owner."""
    return await outcomes.record_outcome(arguments)


_PROVENANCE_CLAIM_KEYS = frozenset({
    "verification_source",
    "phase5_emitter",
    # The grader's OWN output, stripped as defence in depth. Two live controls
    # already neutralise a forged grade and neither depends on this set: the
    # public path re-grades through enrich(), which OVERWRITES all seven keys,
    # and the shared write path decides whether to re-grade from the
    # `corroboration_applied` PARAMETER, never from anything in `detail`.
    #
    # This comment previously said the write path "skips re-grading a detail
    # that already carries a grade". That was true of the first attempt at the
    # persistence fix and is no longer true of the code -- presence-checking a
    # caller-shaped field was itself the authorization bypass, which is why it
    # became a parameter. An external review of this PR (gpt-5.6-terra,
    # 2026-09-19) caught the stale wording still describing the defect as
    # though it were the contract.
    #
    # What stripping still buys: a future caller that passes
    # corroboration_applied=True with caller-shaped detail would persist those
    # keys verbatim, and this keeps the public path from being that caller.
    "corroboration_grade",
    "evidence_weight",
    "claim_risk",
    "claimed_fields",
    "verified_fields",
    "unverified_fields",
    "corroboration_reasons",
})


def _strip_provenance_claims(value):
    """Remove caller-claimable provenance keys from ``detail`` at every depth.

    The corroboration grader trusts these keys wherever they appear in the
    nested detail structure, so sanitizing only the top level converts the
    public-path provenance downgrade into a one-level speed bump.
    """
    if isinstance(value, dict):
        return {
            k: _strip_provenance_claims(v)
            for k, v in value.items()
            if k not in _PROVENANCE_CLAIM_KEYS
        }
    if isinstance(value, list):
        return [_strip_provenance_claims(v) for v in value]
    return value


@mcp_tool("outcome_event", timeout=15.0)
async def handle_outcome_event(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Record an outcome event paired with the agent's current EISV snapshot."""
    from ..context import get_context_agent_id

    outcome_type = arguments.get("outcome_type")
    if not outcome_type:
        return [error_response(
            "outcome_type is required",
            error_code="MISSING_PARAM",
            error_category="validation_error",
        )]

    if outcome_type not in VALID_OUTCOME_TYPES:
        return [error_response(
            f"Unknown outcome_type '{outcome_type}'. Valid: {sorted(VALID_OUTCOME_TYPES)}",
            error_code="INVALID_PARAM",
            error_category="validation_error",
        )]

    # Get agent_id from context
    agent_id = get_context_agent_id()
    if not agent_id:
        # Fall back to explicit argument
        agent_id = arguments.get("agent_id")
    if not agent_id:
        return [error_response(
            "Could not determine agent_id from session context. Provide agent_id explicitly.",
            error_code="NO_AGENT_ID",
            error_category="identity_error",
        )]

    # Ephemeral-write gate (#1351 item 1, defense-in-depth for #1319). The
    # middleware stamps dispatch-minted identities `ephemeral: true /
    # persisted: false`; until now no write path consulted it. Under strict
    # identity an ephemeral writer should be impossible (#1325 closed the
    # known route) — so if one appears, refuse fail-closed: it IS a future
    # leak route. Under non-strict, auto-minted ephemeral identities are the
    # designed flow — stamp the row and warn instead of breaking it.
    ephemeral_stamp = False
    try:
        from ..context import get_session_context
        _ident = get_session_context().get("identity_result") or {}
        if _ident.get("ephemeral") and not _ident.get("persisted"):
            from ..identity_bootstrap import is_strict_identity_required
            if is_strict_identity_required():
                logger.warning(
                    "[EPHEMERAL-GATE] refusing outcome_event write from "
                    "ephemeral-unpersisted identity %s... under strict "
                    "identity — this route should not exist (#1351)",
                    str(agent_id)[:8],
                )
                return [error_response(
                    "Durable write refused: this session is bound to an "
                    "ephemeral, unpersisted identity, which cannot own an "
                    "outcome row under strict identity. Onboard first "
                    "(start_session(force_new=true)) and retry.",
                    error_code="EPHEMERAL_IDENTITY_WRITE_REFUSED",
                    error_category="identity_error",
                )]
            ephemeral_stamp = True
            logger.warning(
                "[EPHEMERAL-GATE] outcome_event write from ephemeral-"
                "unpersisted identity %s... — stamping row (#1351)",
                str(agent_id)[:8],
            )
    except Exception:
        # The gate is defense-in-depth; a gate error must not block a
        # legitimate write. (Strict-mode refusal above returns before this.)
        logger.debug("ephemeral-write gate check failed", exc_info=True)

    # Delegate to the non-decorated helper to avoid the @mcp_tool decorator's
    # asyncio.wait_for wrapping (anyio-asyncio deadlock risk — see CLAUDE.md).
    # agent_id resolved above is injected so the helper can skip context lookup.
    _gate_args = {**arguments, "agent_id": agent_id}

    # PROVENANCE IS SERVER-DERIVED ON THE PUBLIC PATH.
    #
    # `verification_source` decides an outcome's corroboration grade
    # (outcome_corroboration._has_external_evidence short-circuits on the
    # string), so accepting it from the caller let any agent label its own
    # claim `external_signal` and receive EXTERNALLY_VERIFIED / weight 1.00.
    # A caller reaching this decorated MCP tool is, by definition, an agent
    # attesting its own result -- so that is what we record, regardless of
    # what it asked for.
    #
    # `external_signal` and `server_observation` remain available to
    # server-controlled ingestion, which calls outcomes.record_outcome
    # directly and never passes through here: the operator-gated REST harness
    # endpoint (http_api.http_harness_outcome) and in-process emitters such as
    # dialectic resolution. Those paths are unchanged.
    #
    # detail.phase5_emitter is stripped for the same reason: it is a
    # caller-controlled key that _has_tool_observation trusts unconditionally
    # for TOOL_OBSERVED (weight 0.65, which exactly meets the calibration
    # gate's `<` comparison).
    _claimed_source = _gate_args.get("verification_source")
    _gate_args["verification_source"] = "agent_reported_tool_result"
    # The corroboration grader walks NESTED detail contexts
    # (outcome_corroboration._nested_contexts), so a top-level strip alone
    # leaves detail={"verification_source": "external_signal"} forging
    # EXTERNALLY_VERIFIED / weight 1.00 straight past the column downgrade.
    # Strip the explicit provenance keys at every depth. Known residual, left
    # deliberately: the grader's free-text trusted-source vocabulary
    # (_has_verified_marker + _TRUSTED_EXTERNAL_SOURCES) remains claimable and
    # is tracked as follow-up hardening — it predates this change and closing
    # it means restructuring the grader's trust model, not sanitizing input.
    if _gate_args.get("detail"):
        _gate_args["detail"] = _strip_provenance_claims(_gate_args["detail"])
    # Stripping the explicit keys does not reach the grader's FREE-TEXT source
    # vocabulary: _source_text also reads source / evidence_source /
    # evidence_kind / observed_by / captured_by / epistemic_class, none of them
    # stripped, and _has_substrate_evidence matches those against
    # _TRUSTED_SUBSTRATE_MARKERS with no verified-marker requirement -- so
    # detail={"source": "sensor_sync"} still reached SUBSTRATE_OBSERVED (0.85).
    # The grade is capped in outcomes.record_outcome, which caps by
    # default; this path only has to make sure the caller cannot vouch for
    # ITSELF by supplying the internal trust key.
    _gate_args.pop(_TRUSTED_INGESTION_KEY, None)
    if _claimed_source and _claimed_source != "agent_reported_tool_result":
        logger.info(
            "outcome_event: downgraded caller-claimed verification_source=%r to "
            "agent_reported_tool_result (agent=%s)", _claimed_source, agent_id,
        )

    if ephemeral_stamp:
        _detail = dict(_gate_args.get("detail") or {})
        _detail["ephemeral_writer"] = True
        _gate_args["detail"] = _detail
    payload = await _record_outcome_event_inline(_gate_args)
    if ephemeral_stamp and isinstance(payload, dict) and "error" not in payload:
        payload.setdefault("identity_warnings", []).append({
            "code": "ephemeral_writer",
            "message": (
                "This outcome was recorded under an ephemeral, unpersisted "
                "identity and stamped ephemeral_writer=true. Onboard "
                "(start_session) to write under a durable identity."
            ),
        })

    if "error" in payload:
        error_code = payload.get("error_code", "DB_ERROR")
        error_category = payload.get("error_category", "system_error")
        details = {}
        if payload.get("canonical_outcome_id"):
            details["canonical_outcome_id"] = payload["canonical_outcome_id"]
        return [error_response(
            payload["error"],
            error_code=error_code,
            error_category=error_category,
            details=details or None,
        )]

    return success_response(payload)


@mcp_tool("outcome_correlation", timeout=30.0)
async def handle_outcome_correlation(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Run outcome correlation study: does EISV instability predict bad outcomes?"""
    from src.outcome_correlation import OutcomeCorrelation
    from ..context import get_context_agent_id
    import dataclasses

    agent_id = arguments.get("agent_id") or get_context_agent_id()
    since_hours = float(arguments.get("since_hours", 168))

    try:
        study = OutcomeCorrelation()
        report = await study.run(agent_id=agent_id, since_hours=since_hours)

        if report.total_outcomes == 0:
            return [error_response(
                f"No outcome events found in the last {since_hours:.0f} hours"
                + (f" for agent {agent_id}" if agent_id else ""),
                error_code="NO_DATA",
                error_category="validation_error",
            )]

        return success_response(dataclasses.asdict(report))
    except Exception as e:
        logger.error(f"Outcome correlation failed: {e}")
        return [error_response(
            f"Correlation study failed: {e}",
            error_code="STUDY_ERROR",
            error_category="system_error",
        )]
