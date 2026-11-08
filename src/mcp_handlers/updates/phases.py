"""
Update Phases — Extracted from handle_process_agent_update in core.py.

Phases 1-5 of the process_agent_update pipeline:
  1. resolve_identity_and_guards  — UUID, circuit breaker, lazy persist, label
  2. handle_onboarding_and_resume — KG guidance, auto-resume archived agents
  3. transform_inputs             — Extract & transform params (fail-fast before lock)
  4. execute_locked_update        — Policy, agent creation, ODE update
  5. execute_post_update_effects  — Health, CIRS, PG record, outcomes
"""

import asyncio
import os
import re
import secrets
import time
from typing import Optional, Sequence

from mcp.types import TextContent

from src.logging_utils import get_logger
from src import agent_storage
from src.perf_monitor import record_ms as _perf_record_ms
from src.report_excerpt import make_report_excerpt
from src.state_persistence import (
    STATE_PERSISTENCE_UNCONFIRMED_WARNING,
    StatePersistenceReceipt,
)

from .context import UpdateContext
from .post_update_helpers import (
    _PURPOSE_KEYWORDS,  # noqa: F401
    _infer_purpose,
    _derive_outcome,
    _prediction_id_for_phase5_evidence,
    _restamp_fork_after_thread_identity_update,
    _self_report_record,  # noqa: F401
    _track_thread_identity,
    _persist_thread_identity_async,
    _persist_inferred_purpose_async,
    _r2_post_update_hook,
    _rewrap_behavioral_verdict,  # noqa: F401
    _post_update_health_and_baselines,
    _eisv_instrument_stamp,
    _post_update_record_state,
    _recover_missing_agent_for_state_record,
    _post_update_save_baseline,
    _COMPLETION_SIGNAL_RE,  # noqa: F401
    _FAILURE_SIGNAL_RE,  # noqa: F401
    _post_update_auto_outcome,
)
from ..identity_bootstrap import (
    CALLER_PROOF_REMEDY as _CALLER_PROOF_REMEDY,
)
from ..utils import error_response
from ..support.tool_hints import (
    KNOWLEDGE_SEARCH_SUGGESTION,
    KNOWLEDGE_OPEN_QUESTIONS_WORKFLOW,
)
logger = get_logger(__name__)

_ALLOWED_EPISTEMIC_CLASSES = {
    "agent_report",
    "substrate_observation",
    "substrate_interpretation",
    "prediction",
}

_STRONG_IDENTITY_SOURCES = {
    "continuity_token",
    "client_session_id",
    "explicit_client_session_id",
    "explicit_client_session_id_scoped",
    "mcp_session_id",
    "x_session_id",
    "oauth_client_id",
    # X-Unitares-Operator bearer token, validated against the env allowlist
    # on every call (#425 dashboard-identity decision) — per-call proof,
    # stronger than the stable-header sources above.
    "operator_token",
    # Identity PATH 0 proof-owned UUID rebind: requires a signed
    # continuity_token whose `aid` matches the UUID, or an S19
    # kernel-attested substrate peer. Without these entries the
    # require_strong_identity refusal recommended this exact path in its
    # hint and then scored it weak (#1768).
    "agent_uuid_direct",
    "agent_uuid_direct_fastpath",
}

_MEDIUM_IDENTITY_SOURCES = {
    "x_client_id",
    "pinned_onboard_session",
    "context_mcp_session_id",
    "context_session_key",
}

# S3: `sticky_cache:<original>` envelopes are emitted by the cache-hit
# fast-paths in http_api.py and middleware/identity_step.py. The decay rule
# (strong→medium, medium→weak, weak→weak) honors the original mint while
# debiting one tier for per-call proof absence — the principled middle
# ground between #466's "always weak" and trusting the cache fully. Pre-S3
# bindings carry original="unknown" and continue to map to weak.
_STICKY_CACHE_PREFIX = "sticky_cache:"

# Single-step downward decay. Anything not in this map (including "weak"
# and unknowns) stays at its computed tier.
_DECAY_BY_ONE = {"strong": "medium", "medium": "weak"}
_TIER_SCORES = {"strong": 1.0, "medium": 0.7, "weak": 0.35}


def _tier_for_source(source_key: str) -> tuple[str, str]:
    """Map a raw source key to (tier, reason). Pure — no decay applied."""
    if source_key in _STRONG_IDENTITY_SOURCES:
        return "strong", "cryptographic or explicit stable session source"
    if source_key in _MEDIUM_IDENTITY_SOURCES:
        return "medium", "session continuity source with weaker explicit proof"
    return "weak", "heuristic or unknown session source"


# How a caller turns a server-inferred binding into a caller-proven one. The
# write-path assurance breadcrumb and the strict write refusal both quote it,
# so the refusal cannot drift from the `identity_assurance` it embeds (the
# refusal kept leading with continuity_token after #1715 reordered the
# breadcrumb). The text lives in identity_bootstrap (imported above as
# _CALLER_PROOF_REMEDY), beside the strict refusal for a call that resolved no
# identity, which quotes it too.


def _how_to_strengthen(
    tier: str,
    source_key: str,
    proof_origin: Optional[str],
) -> Optional[str]:
    """One-line breadcrumb telling the agent how to reach a higher tier (#732).

    Returns ``None`` for `strong` (no action needed). Mirrors
    `services.identity_payloads._how_to_strengthen` so the write-path
    assurance block agrees with the identity()/onboard read-path block.

    Leads with the normal active-session / `client_session_id` path and keeps
    `continuity_token` as an explicit proof-owned rebind fallback. Kept
    word-identical with `services.identity_payloads._how_to_strengthen`.
    """
    if tier == "strong":
        return None
    if proof_origin == "server_inferred":
        prefix = "binding was server-inferred (not caller-proven); "
    elif tier == "medium":
        prefix = "to strengthen this medium-assurance binding, "
    else:
        prefix = "to strengthen this weak binding, "
    return prefix + _CALLER_PROOF_REMEDY


def _compute_identity_assurance(
    source: Optional[str],
    trajectory_confidence: Optional[float],
    proof_origin: Optional[str] = None,
) -> dict:
    """Compute identity assurance tier for write-path governance updates.

    Recognizes the S3 `sticky_cache:<original>` envelope: the original
    proof source's tier is computed, then decayed one step (strong→medium,
    medium→weak, weak→weak) to honor the original mint while debiting
    for per-call proof absence.

    `proof_origin` ('caller_asserted' | 'server_inferred') is authoritative
    over the source label: a server-inferred binding (fingerprint pin,
    transport-injected CSID, context fallback) is NEVER strong, regardless of
    the source label it happens to wear. This closes the injected-CSID path
    that previously reported strong/explicit/1.0 for a write resolved to a
    concurrent sibling. The emitted `caller_proven` boolean is what the strict
    gate keys on.
    """
    source_key = (source or "unknown").strip().lower()
    caller_proven = (proof_origin == "caller_asserted")

    if proof_origin == "server_inferred":
        # Authoritative downgrade — a server-guessed binding is weak proof no
        # matter what source label resolution stamped on it.
        tier = "weak"
        score = _TIER_SCORES[tier]
        reason = f"server-inferred binding ('{source_key}'); not caller-proven"
    elif source_key.startswith(_STICKY_CACHE_PREFIX):
        original_key = source_key[len(_STICKY_CACHE_PREFIX):] or "unknown"
        original_tier, _ = _tier_for_source(original_key)
        tier = _DECAY_BY_ONE.get(original_tier, original_tier)
        if original_tier == tier:
            reason = (
                f"cache hit; original proof '{original_key}' was {original_tier} "
                "(no further decay)"
            )
        else:
            reason = (
                f"cache hit; original proof '{original_key}' was {original_tier}, "
                f"decayed one tier to {tier} for per-call proof absence"
            )
        score = _TIER_SCORES[tier]
    else:
        tier, reason = _tier_for_source(source_key)
        score = _TIER_SCORES[tier]

    # Trajectory acts as continuity evidence, not primary auth.
    if trajectory_confidence is not None:
        try:
            traj = max(0.0, min(1.0, float(trajectory_confidence)))
            score = round(min(1.0, score + (0.2 * traj)), 3)
            if tier == "weak" and traj >= 0.7:
                tier = "medium"
                reason = "weak session source, upgraded by high trajectory continuity"
        except (TypeError, ValueError):
            pass

    assurance = {
        "tier": tier,
        "score": score,
        "session_source": source_key,
        "trajectory_confidence": trajectory_confidence,
        "reason": reason,
        "caller_proven": caller_proven,
        "proof_origin": proof_origin or "unknown",
    }
    hint = _how_to_strengthen(tier, source_key, proof_origin)
    if hint:
        assurance["how_to_strengthen"] = hint
    return assurance



# Module-local strong-ref set for in-flight fire-and-forget persist tasks
# (thread-identity, inferred-purpose). Without this, `asyncio.create_task(coro)`
# returns a Task that CPython GC can collect mid-await — these persisters
# await asyncpg, so a collection between yields silently drops the write.
# Mirrors the canonical pattern at
# `src/coordination_failure_emit.py:_inflight_dedicated_writes` and
# `src/mcp_handlers/support/pause_ttl.py:_inflight_persistence_tasks`.
# P001 fix per Watcher findings #0a0616c2 and #acfc7012 (2026-05-18).
_inflight_persist_tasks: "set" = set()


def _spawn_persist_task(coro, *, name: str) -> None:
    """Spawn coro on the running loop and pin a strong ref until done.

    Callers should be inside an async handler (running loop required);
    `RuntimeError` from `get_running_loop` is left to the caller's
    existing try/except to swallow.
    """
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _inflight_persist_tasks.add(task)
    task.add_done_callback(_inflight_persist_tasks.discard)


# ─── Phase 1: Identity Resolution & Guards ─────────────────────────────

async def resolve_identity_and_guards(ctx: UpdateContext) -> Optional[Sequence[TextContent]]:
    """Resolve UUID identity, check circuit breaker, lazy-persist, set label.

    Returns an early-exit error response, or None to continue.
    """
    mcp_server = ctx.mcp_server

    from ..context import (
        get_context_agent_id,
        get_context_session_key,
        get_session_resolution_source,
        get_session_proof_origin,
        get_trajectory_confidence,
    )
    ctx.agent_uuid = get_context_agent_id()
    ctx.session_key = get_context_session_key()
    ctx.session_resolution_source = get_session_resolution_source()
    ctx.proof_origin = get_session_proof_origin()
    ctx.trajectory_confidence = get_trajectory_confidence()
    ctx.identity_assurance = _compute_identity_assurance(
        ctx.session_resolution_source,
        ctx.trajectory_confidence,
        proof_origin=ctx.proof_origin,
    )

    if not ctx.agent_uuid:
        logger.error("No agent_uuid in context - identity_v2 resolution failed at dispatch")
        return [error_response(
            "Identity not resolved for this check-in.",
            recovery={
                "action": (
                    "If you already called start_session(), retry with the "
                    "client_session_id returned by that start_session() call. "
                    "Otherwise call start_session(force_new=true) first."
                ),
                "related_tools": ["onboard", "identity", "process_agent_update"],
                "workflow": [
                    "1. Call start_session(force_new=true) to mint this process-instance",
                    "2. Save client_session_id from the start_session() response",
                    "3. Pass that client_session_id to sync_state()",
                ],
            },
            error_code="SESSION_ERROR",
            error_category="auth_error",
            arguments=ctx.arguments,
        )]

    # #425 strict WRITE precondition (caller-proven binding required).
    #
    # The strict gate must key on whether the CALLER PROVED this binding, not on
    # the mere presence of a resolved uuid. Otherwise a server-inferred
    # fingerprint/pin/context resolution to a concurrent same-host SIBLING
    # silently writes under the wrong identity (it resolved *something*, so the
    # is_new_agent / "no uuid" gates never fire). caller_proven is False for an
    # injected CSID, a pin match, ip_ua_fingerprint, and context fallback.
    #
    # Substrate-earned residents (Lumen, persistent residents) are EXEMPT — they
    # legitimately re-resolve a stable identity across restarts via the
    # substrate-earned pattern. The exemption is keyed on the RESOLVED agent, so
    # a colliding sibling cannot borrow it. Fail-closed on predicate error.
    #
    # Conservative trigger: refuse only on an EXPLICIT server_inferred origin
    # (injected CSID, pin, ip_ua_fingerprint, context fallback — the confirmed
    # mis-attribution paths). proof_origin None/unknown is left to pass
    # (fail-open) so paths that never ran derive_session_key are unaffected
    # while the confirmed bug is closed; tightening those is a follow-up.
    if ctx.identity_assurance.get("proof_origin") == "server_inferred":
        from src.mcp_handlers.identity_bootstrap import is_strict_identity_required
        if is_strict_identity_required():
            # One exemption predicate, shared with the knowledge write path
            # (identity_bootstrap.is_exempt_from_caller_proof). Fail-closed.
            from src.mcp_handlers.identity_bootstrap import (
                inferred_binding_refusal_payload,
                is_exempt_from_caller_proof,
            )
            if not await is_exempt_from_caller_proof(ctx.agent_uuid):
                from src.mcp_handlers.response_base import success_response
                logger.info(
                    "[PROCESS_UPDATE] STRICT refusing write: identity resolved via "
                    "%s (proof_origin=%s, not caller-proven) for agent %s...",
                    ctx.session_resolution_source,
                    ctx.identity_assurance.get("proof_origin"),
                    (ctx.agent_uuid or "")[:12],
                )
                return success_response(inferred_binding_refusal_payload(
                    "process_agent_update",
                    retry_call="sync_state",
                    retry_label="sync_state / process_agent_update",
                    identity_assurance=ctx.identity_assurance,
                    surface_context={
                        "transport_surface": "process_agent_update",
                        "session_resolution_source": ctx.session_resolution_source,
                        "proof_origin": ctx.identity_assurance.get("proof_origin"),
                        "lifecycle_automation": "not_confirmed",
                    },
                ))

    if ctx.arguments.get("require_strong_identity"):
        if ctx.identity_assurance.get("tier") != "strong":
            return [error_response(
                "process_agent_update requires strong identity assurance for this call",
                details={
                    "identity_assurance": ctx.identity_assurance,
                    "hint": (
                        "Use explicit client_session_id for the active process, "
                        "or proof-owned identity(agent_uuid=..., continuity_token=..., resume=true)."
                    ),
                },
                recovery={
                    "action": "Re-bind with strong identity proof, then retry.",
                    "related_tools": ["bind_session", "identity", "onboard"],
                }
            )]

    # Circuit breaker: paused / archived agents cannot update.
    # Pause TTL: stale pauses auto-expire via the shared helper, letting
    # the categorizer re-evaluate downstream. Implementation in
    # src/mcp_handlers/support/pause_ttl.py — same module serves
    # check_agent_can_operate to keep all gates in sync.
    if ctx.agent_uuid in mcp_server.agent_metadata:
        meta = mcp_server.agent_metadata[ctx.agent_uuid]
        if meta.status == "paused":
            from ..support.pause_ttl import (
                maybe_auto_expire_pause_async,
                paused_refusal_recovery,
            )
            expired = await maybe_auto_expire_pause_async(ctx.agent_uuid, meta)
            if not expired:
                return [error_response(
                    "Agent is paused - check-ins and new shared-memory entries are refused",
                    error_code="AGENT_PAUSED",
                    details={
                        "agent_id": ctx.agent_uuid[:12],
                        "paused_at": meta.paused_at,
                        "status": "paused",
                    },
                    recovery=paused_refusal_recovery(meta, ctx.agent_uuid),
                )]
            # Expired — fall through to normal processing; categorizer
            # will re-pause if state is genuinely degraded.
        # NOTE: Do NOT block archived here. Phase 2 (handle_onboarding_and_resume)
        # auto-resumes archived agents on engagement. Blocking here prevented
        # onboard() reactivation from taking effect (Phase 1 ran before metadata
        # was refreshed) and blocked the auto-resume path in Phase 2.
        # elif meta.status == "archived": ... REMOVED - let Phase 2 handle it

    # Lazy creation: persist agent in PostgreSQL on first real work
    from ..identity.handlers import ensure_agent_persisted
    if ctx.session_key:
        newly_persisted = await ensure_agent_persisted(ctx.agent_uuid, ctx.session_key)
        if newly_persisted:
            logger.info(f"Lazy-persisted agent {ctx.agent_uuid[:8]}... on first process_agent_update")

    ctx.is_new_agent = ctx.agent_uuid not in mcp_server.agent_metadata

    # Label from arguments or existing metadata
    ctx.label = ctx.arguments.get("agent_id") or ctx.arguments.get("id") or ctx.arguments.get("name")
    if not ctx.label and ctx.agent_uuid in mcp_server.agent_metadata:
        meta = mcp_server.agent_metadata[ctx.agent_uuid]
        ctx.label = getattr(meta, 'label', None)

    # Set up identity aliases
    ctx.agent_id = ctx.agent_uuid
    ctx.declared_agent_id = ctx.label or ctx.agent_uuid
    ctx.arguments["agent_id"] = ctx.declared_agent_id
    ctx.arguments["_agent_uuid"] = ctx.agent_uuid
    ctx.arguments["_agent_label"] = ctx.declared_agent_id

    # Store label in PostgreSQL
    if ctx.label and ctx.label != ctx.agent_uuid:
        try:
            from src.db import get_db
            db = get_db()
            await db.update_agent_fields(ctx.agent_uuid, label=ctx.label)
            logger.debug(f"PostgreSQL: Set label '{ctx.label}' for agent {ctx.agent_uuid[:8]}...")
        except Exception as e:
            logger.debug(f"Could not set label in PostgreSQL: {e}")
        if ctx.agent_uuid in mcp_server.agent_metadata:
            meta = mcp_server.agent_metadata[ctx.agent_uuid]
            meta.label = ctx.label

    # Substrate-agnostic resident-tag reconcile: server-side equivalent of the
    # SDK's GovernanceAgent._reconcile_resident_tags (#754). The creation-time
    # stamp only fires at mint; residents resume an existing UUID and so never
    # re-stamp. Crucially this covers the BEAM Sentinel, which never calls
    # onboard — it only attaches agent_id to this check-in — so an onboard-path
    # reconcile would never reach it. Memoized + subset-guarded: a no-op on the
    # healthy path, a single additive write the one time a tag is missing.
    if not ctx.is_new_agent:
        try:
            from src.grounding.onboard_classifier import reconcile_resident_tags
            _resident_meta = mcp_server.agent_metadata.get(ctx.agent_uuid)
            # Classify by meta.label (the canonical resident label), same
            # source the tag_audit endpoint uses. ctx.label is unreliable here:
            # the BEAM Sentinel attaches agent_id=<uuid>, so ctx.label is the
            # UUID, not "Sentinel". Fall back to ctx.label only when meta has
            # no label.
            _resident_label = getattr(_resident_meta, "label", None) or ctx.label
            reconciled = await reconcile_resident_tags(
                ctx.agent_uuid, _resident_label, meta=_resident_meta
            )
            if reconciled is not None:
                logger.info(
                    f"[CHECKIN] resident-tag reconcile: {ctx.agent_uuid[:8]}... "
                    f"-> {reconciled} (label={ctx.label!r})"
                )
        except Exception as e:
            logger.debug(f"resident-tag reconcile failed (non-fatal): {e}")

    ctx.loop = asyncio.get_running_loop()
    ctx.key_was_generated = False
    ctx.api_key_auto_retrieved = False
    ctx.dialectic_enforcement_warning = None

    return None  # Continue to next phase

# ─── Phase 2: Onboarding & Auto-Resume ─────────────────────────────────

async def handle_onboarding_and_resume(ctx: UpdateContext) -> Optional[Sequence[TextContent]]:
    """Surface KG guidance for new agents; auto-resume archived agents.

    Returns an early-exit error response, or None to continue.
    """
    mcp_server = ctx.mcp_server
    agent_id = ctx.agent_id

    # Onboarding guidance for new agents
    if ctx.is_new_agent:
        try:
            from src.knowledge_graph import get_knowledge_graph
            graph = await get_knowledge_graph()
            stats = await graph.get_stats()

            # Surface open questions
            open_questions = []
            try:
                questions = await graph.query(type="question", status="open", limit=3)
                questions.sort(key=lambda q: q.timestamp, reverse=True)
                for q in questions[:2]:
                    q_dict = q.to_dict(include_details=False)
                    simplified = {
                        "id": q_dict["id"],
                        "summary": q_dict["summary"][:200] if len(q_dict.get("summary", "")) > 200 else q_dict.get("summary", ""),
                        "tags": q_dict.get("tags", [])[:3] if q_dict.get("tags") else [],
                        "severity": q_dict.get("severity")
                    }
                    open_questions.append(simplified)
                logger.debug(f"Found {len(open_questions)} open questions for onboarding")
            except Exception as e:
                logger.warning(f"Could not fetch open questions for onboarding: {e}", exc_info=True)
                open_questions = []

            if stats.get("total_discoveries", 0) > 0:
                question_count = stats.get("by_type", {}).get("question", 0)
                ctx.onboarding_guidance = {
                    "message": f"Welcome! The knowledge graph contains {stats['total_discoveries']} discoveries from {stats['total_agents']} agents.",
                    "suggestion": KNOWLEDGE_SEARCH_SUGGESTION,
                    "example_tags": list(stats.get("by_type", {}).keys())[:5] if stats.get("by_type") else []
                }

                # Naming suggestions
                try:
                    from ..support.naming_helpers import (
                        detect_interface_context,
                        generate_name_suggestions
                    )
                    existing_names = [
                        getattr(m, 'label', None)
                        for m in mcp_server.agent_metadata.values()
                        if getattr(m, 'label', None)
                    ]
                    context = detect_interface_context()
                    purpose_hint = None
                    response_text = ctx.arguments.get("response_text", "")
                    if response_text:
                        purpose_keywords = ["debug", "fix", "implement", "test", "explore", "analyze", "refactor", "review"]
                        response_lower = response_text.lower()
                        for keyword in purpose_keywords:
                            if keyword in response_lower:
                                purpose_hint = keyword
                                break
                    suggestions = generate_name_suggestions(
                        context=context,
                        purpose=purpose_hint,
                        existing_names=existing_names
                    )
                    from ..identity_bootstrap import SET_DISPLAY_NAME_CALL

                    ctx.onboarding_guidance["naming"] = {
                        "message": "Name yourself to make your work easier to find",
                        "action": f"Call {SET_DISPLAY_NAME_CALL} to set your name",
                        "suggestions": suggestions[:3],
                        "quick_example": suggestions[0]["name"] if suggestions else None
                    }
                except Exception as e:
                    logger.debug(f"Could not generate naming suggestions for onboarding: {e}")

                if open_questions:
                    ctx.onboarding_guidance["open_questions"] = {
                        "message": f"Found {len(open_questions)} open question(s) waiting for answers. Want to try responding to one?",
                        "questions": open_questions,
                        "invitation": "Answer one with knowledge(action='store', summary=<answer>, response_to={'discovery_id': <id>, 'response_type': 'answer'}) to help build shared knowledge.",
                        "tool": "knowledge"
                    }
                elif question_count > 0:
                    ctx.onboarding_guidance["open_questions"] = {
                        "message": f"There are {question_count} open question(s) in the knowledge graph.",
                        "suggestion": KNOWLEDGE_OPEN_QUESTIONS_WORKFLOW,
                        "tool": "knowledge"
                    }
        except Exception as e:
            logger.warning(f"Could not check knowledge graph for onboarding: {e}")

    # Archived-agent refusal. Historically this branch would silently
    # auto-resume archived agents on engagement, papering over orphan-sweep
    # false-positives on live residents. Removed once residents became
    # self-tagging 'persistent' (PR #39) — sweep no longer targets live agents,
    # so nothing legitimate hits the archived branch anymore. Manual archives
    # (PR #33) and the 2026-04-18 incident race (PR #33) were never supposed
    # to auto-resume anyway. An archived agent comes back through a
    # proof-carrying start_session(resume=true), which unarchives the same
    # UUID, or starts over with start_session(force_new=true). self_recovery
    # rejects archived identities, so it is not offered here.
    meta = mcp_server.agent_metadata.get(ctx.agent_uuid)
    ctx.meta = meta

    if meta:
        if meta.status == "archived":
            logger.warning(
                f"Refused process_agent_update on archived agent {agent_id[:12]}...."
            )
            from ..support.pause_ttl import describe_write_gate
            return [error_response(
                f"Agent '{agent_id}' is archived and cannot be updated.",
                recovery={
                    "action": describe_write_gate(meta)["held"],
                    "related_tools": ["start_session"],
                },
                context={
                    "agent_id": agent_id,
                    "status": "archived",
                    "archived_at": getattr(meta, "archived_at", None),
                }
            )]

        elif meta.status == "paused":
            from ..support.pause_ttl import paused_refusal_recovery
            recovery = paused_refusal_recovery(meta, ctx.agent_uuid)
            recovery["related_tools"] = ["get_governance_metrics", "self_recovery"]
            return [error_response(
                f"Agent '{agent_id}' is paused - check-ins and new shared-memory entries are refused.",
                recovery=recovery,
                context={
                    "agent_id": agent_id,
                    "status": "paused",
                    "reason": recovery["why"],
                }
            )]

        elif meta.status == "deleted":
            return [error_response(
                f"Agent '{agent_id}' is deleted and cannot be used.",
                recovery={
                    "action": "Cannot recover deleted agents",
                    "related_tools": ["agent"],
                    "workflow": "Deleted agents are permanently removed. Use agent(action='list') to see available agents."
                },
                context={
                    "agent_id": agent_id,
                    "status": "deleted",
                    "note": "Deleted agents cannot be recovered. Use agent(action='archive') instead of agent(action='delete') to preserve agent state."
                }
            )]

    return None  # Continue

# ─── Phase 3: Validate Inputs ──────────────────────────────────────────

def transform_inputs(ctx: UpdateContext) -> Optional[Sequence[TextContent]]:
    """Extract and transform validated parameters to context BEFORE acquiring lock.
    (Pydantic handles the actual validation in middleware, so these values
    are guaranteed to be structurally correct).

    Returns an early-exit error response (None if successful).
    """
    # Response Text — Pydantic schema defines Optional[str] with default None,
    # so the key is always present (possibly as None). Coerce None → "" so
    # downstream string ops (re.findall, .lower(), slicing) stay safe.
    ctx.response_text = ctx.arguments.get("response_text") or ""

    # Complexity (coerce to float — Pydantic schema accepts str|float|None)
    raw_complexity = ctx.arguments.get("complexity", 0.5)
    try:
        ctx.complexity = float(raw_complexity) if raw_complexity is not None else 0.5
    except (TypeError, ValueError):
        ctx.complexity = 0.5

    # Pre-ODE: Enforce complexity_limit from dialectic conditions
    if ctx.meta and getattr(ctx.meta, "dialectic_conditions", None):
        try:
            from ..dialectic.enforcement import enforce_complexity_limit
            ctx.complexity, cap_warning = enforce_complexity_limit(
                ctx.meta.dialectic_conditions, ctx.complexity
            )
            if cap_warning:
                ctx.dialectic_enforcement_warning = cap_warning
                ctx.arguments["complexity"] = ctx.complexity
        except Exception as e:
            logger.warning(f"Could not enforce complexity limit: {e}", exc_info=True)

    # Confidence (coerce to float — same str|float|None pattern)
    raw_confidence = ctx.arguments.get("confidence")
    try:
        reported_confidence = float(raw_confidence) if raw_confidence is not None else None
    except (TypeError, ValueError):
        reported_confidence = None
    ctx.confidence = reported_confidence
    # Auto-correction is OFF pending the #1321 rebuild. The tactical bins the
    # corrector read were ~96% trained by the tool-invocation-success feeder
    # (removed in monitor_calibration.py) whose inverted low-confidence scoring
    # made the applied corrections run backwards — an honest 0.5–0.7 report was
    # halved because the agent's tools succeeded. Identity is strictly less
    # wrong than scaling against those bins. Re-enable only against
    # hard-exogenous channels once the bins are re-versioned;
    # apply_confidence_correction and its tests stay intact for that rebuild.
    ctx.calibration_correction_info = None

    # Epistemic class: forward-only storage label for the state row. Pydantic
    # validates the public schema; this fallback keeps non-schema internal
    # callers on the honest default.
    # ⛔ Do NOT "fix" this default by flipping it to a substrate class.
    #
    # The coercion is real and it is the mechanism by which the ledger
    # over-labels automation as agent voice: a caller that omits the field is
    # recorded as having claimed authorship. But `agent_report` is ~80.3% of
    # all state rows, so changing the default silently RECLASSIFIES history —
    # every stored row keeps its old label while new rows get a new one, and
    # any series that spans the change quietly switches units mid-flight.
    #
    # The fix is at the CALLER: residents now state their class explicitly
    # (unitares_sdk.agent.GovernanceAgent.epistemic_class defaults to
    # substrate_interpretation, and checkin() omits the field when unset).
    # Keep this default until every producer is explicit, then retire it in a
    # dated change that says so — not as a side effect.
    raw_epistemic_class = ctx.arguments.get("epistemic_class") or "agent_report"
    if raw_epistemic_class not in _ALLOWED_EPISTEMIC_CLASSES:
        logger.warning(
            "Unknown epistemic_class %r on process_agent_update; defaulting to agent_report",
            raw_epistemic_class,
        )
        raw_epistemic_class = "agent_report"
    ctx.epistemic_class = raw_epistemic_class

    # Low-assurance identity should not drive high-confidence updates.
    if ctx.identity_assurance.get("tier") == "weak" and ctx.confidence is not None:
        original_confidence = ctx.confidence
        ctx.confidence = min(ctx.confidence, 0.55)
        if ctx.confidence != original_confidence:
            ctx.confidence_dampened = True
            ctx.warnings.append(
                f"Identity assurance is weak ({ctx.identity_assurance.get('session_source')}); "
                f"confidence dampened from {original_confidence:.2f} to {ctx.confidence:.2f}."
            )

    # Ethical Drift
    ctx.ethical_drift = ctx.arguments.get("ethical_drift", [0.0, 0.0, 0.0])

    # Task Type
    ctx.task_type = ctx.arguments.get("task_type", "mixed")

    # Phase-5 evidence supply: collect self-reported tool results.
    # model_dump() in params_step.py flattens ToolResultEvidence → plain dicts.
    # The actual per-item outcome_event iteration (which is async) runs in
    # execute_post_update_effects after the ODE update, using ctx.recent_tool_results.
    ctx.recent_tool_results = ctx.arguments.get("recent_tool_results") or []
    for warning in ctx.arguments.get("_mangled_s22_recovery_warnings") or []:
        if warning not in ctx.warnings:
            ctx.warnings.append(warning)

    return None  # Continue

# ─── Phase 4: Locked Update ────────────────────────────────────────────



async def prepare_unlocked_inputs(ctx: UpdateContext) -> None:
    """Build agent_state inputs and policy warnings *before* the agent lock.

    Lifted out of execute_locked_update because every step here is read-only
    against the per-agent lock invariant: ctx.agent_state is the call-local
    input dict (not yet persisted), the behavioral sensor reads monitor
    history but cannot corrupt it, and policy validators are pure CPU on
    request fields. Running these unlocked drops the locked-phase floor by
    ~80% of its 7s steady-state cost (per [checkin_phases] log analysis
    2026-05-04: behavioral_sensor + policy together dominate locked_update
    when the call is on a recurring agent).

    Race-window analysis: the behavioral sensor reads monitor.state at
    time T, then the lock is acquired and the ODE may see monitor.state
    advanced by 1 tick from a concurrent call. The sensor's computed
    EISV is therefore at most 1 tick stale — well within sensor noise
    (rolling-window over 3+ history points), and the ODE itself still
    runs against the locked, current state.
    """
    mcp_server = ctx.mcp_server
    import numpy as np

    ctx.agent_state = {
        "parameters": np.array(ctx.arguments.get("parameters", [])),
        "ethical_drift": np.array(ctx.ethical_drift),
        "response_text": ctx.response_text,
        "complexity": ctx.complexity,
        "epistemic_class": ctx.epistemic_class,
        "confidence_dampened": ctx.confidence_dampened,
    }
    ctx.submitted_afferents = None
    try:
        from src.provenance_context import (
            build_s22_write_context,
            classify_fork_for_s22_context,
        )

        meta = mcp_server.agent_metadata.get(ctx.agent_uuid) if ctx.agent_uuid else None
        episode_fork_kind, identity_lineage_fork = classify_fork_for_s22_context(
            meta, ctx.agent_uuid
        )

        provenance_context = build_s22_write_context(
            ctx.arguments,
            meta=meta,
            context_source="process_agent_update",
            default_governance_mode="explicit",
            episode_fork_kind=episode_fork_kind,
            identity_lineage_fork=identity_lineage_fork,
            include_runtime_provenance=True,
        )
        if provenance_context:
            ctx.agent_state["provenance_context"] = provenance_context
    except Exception as exc:
        logger.debug(f"S22 provenance context skipped: {exc}")

    # Inject sensor EISV for spring coupling when the caller provides it.
    # Agents with physical sensors should publish `sensor_data["eisv"]` in
    # their process_agent_update payload; anything else falls through to the
    # behavioral sensor below.
    sensor_data = ctx.arguments.get("sensor_data")
    if sensor_data and isinstance(sensor_data, dict):
        from src.eisv_telemetry import build_submitted_afferents

        try:
            # Structurally separate raw afferents from the dictionary passed to
            # the monitor. Telemetry failures must not abort a governance update.
            ctx.submitted_afferents = build_submitted_afferents(
                sensor_data,
                submitted_source="physical",
            )
        except Exception as exc:
            logger.warning(
                "Submitted afferent telemetry skipped for %s...: %s",
                (ctx.agent_id or "")[:8],
                exc,
            )

        sensor_eisv = sensor_data.get("eisv")
        if sensor_eisv and isinstance(sensor_eisv, dict):
            ctx.agent_state["sensor_eisv"] = sensor_eisv
            # Caller-published physical sensor (e.g. Lumen's Pi). Source tag lets
            # the monitor apply per-source coupling policy (sensor_coupling_allows).
            ctx.agent_state["sensor_eisv_source"] = "physical"
            from src.eisv_telemetry import build_physical_sensor_derivation
            ctx.agent_state["_eisv_derivation"] = build_physical_sensor_derivation(
                sensor_eisv
            )

    # Behavioral sensor: compute EISV from governance observables for non-embodied agents
    if "sensor_eisv" not in ctx.agent_state:
        try:
            monitor = mcp_server.monitors.get(ctx.agent_id)
            if monitor and len(getattr(monitor.state, 'decision_history', [])) >= 3:
                from src.behavioral_sensor import (
                    CALIBRATION_FREE_I_ABLATION_SCHEMA,
                    DECISION_SELF_LOOP_ABLATION_SCHEMA,
                    LEGACY_COHERENCE_ABLATION_SCHEMA,
                    REGIME_SELF_LOOP_ABLATION_SCHEMA,
                    compute_behavioral_sensor_eisv,
                    compute_calibration_free_i_shadow,
                    compute_decision_self_loop_shadow,
                    compute_legacy_coherence_dependency_shadow,
                    compute_regime_self_loop_shadow,
                    filter_behavioral_outcomes,
                    neutralize_sensor_self_loops_enabled,
                )
                from src.coherence_provenance import (
                    LEGACY_COHERENCE_SOURCE,
                    ODE_CONTROL_FEEDBACK_ROLE,
                )
                from src.dual_log.continuity import should_exclude_template_divergence
                from src.eisv_telemetry import build_behavioral_derivation
                from src.mcp_handlers.updates.context import get_mean_calibration_error

                cal_error = get_mean_calibration_error(ctx)

                # Drift norm from previous check-in
                drift_n = None
                dv = getattr(monitor, '_last_drift_vector', None)
                if dv is not None:
                    drift_n = getattr(dv, 'norm', None)

                # Continuity metrics from previous check-in
                comp_div = None
                cont_E, cont_I, cont_S = None, None, None
                substrate_canaries = {}
                cm = getattr(monitor, '_last_continuity_metrics', None)
                if cm is not None:
                    # cm is the previous check-in's ContinuityMetrics, so read
                    # the exclusion flag stamped when cm was created rather than
                    # mixing cm with the current check-in's text/epistemic_class.
                    exclude_template_div = should_exclude_template_divergence(cm)
                    cont_E = getattr(cm, 'E_input', None)
                    if not exclude_template_div:
                        comp_div = getattr(cm, 'complexity_divergence', None)
                        cont_I = getattr(cm, 'I_input', None)
                        cont_S = getattr(cm, 'S_input', None)
                    # Observe-only: were these three features measurements, or
                    # defaults/constants? getattr-with-default so a monitor
                    # carrying a pre-upgrade ContinuityMetrics stays readable.
                    substrate_canaries = {
                        "self_complexity_defaulted": bool(
                            getattr(cm, 'self_complexity_defaulted', False)
                        ),
                        "E_input_clipped": bool(getattr(cm, 'E_input_clipped', False)),
                        "continuity_degenerate": bool(
                            getattr(cm, 'continuity_degenerate', False)
                        ),
                        "template_divergence_excluded": bool(exclude_template_div),
                    }

                # Tool usage signals for behavioral sensor
                tool_err, tool_vel, tool_div = None, None, None
                try:
                    from src.tool_usage_tracker import get_tool_usage_tracker
                    tu_stats = get_tool_usage_tracker().get_usage_stats(
                        agent_id=ctx.agent_id, window_hours=1
                    )
                    tu_total = tu_stats.get("total_calls", 0)
                    if tu_total > 0:
                        tu_failed = sum(
                            t.get("error_count", 0)
                            for t in tu_stats.get("tools", {}).values()
                        )
                        tool_err = tu_failed / tu_total
                        tool_vel = tu_total / 60.0  # calls per minute
                        tool_div = tu_stats.get("unique_tools", 0) / tu_total
                except Exception as e:
                    logger.debug(f"Tool usage stats unavailable for {ctx.agent_id}: {e}")

                # Recent outcome events for behavioral feedback
                outcome_hist = None
                try:
                    from src.db import get_db
                    _db = get_db()
                    if _db and hasattr(_db, 'get_recent_outcomes'):
                        if neutralize_sensor_self_loops_enabled():
                            raw_outcomes = await _db.get_recent_outcomes(
                                agent_id=ctx.agent_id,
                                limit=100,
                                since_hours=24.0,
                            )
                            if raw_outcomes is not None:
                                filtered = filter_behavioral_outcomes(raw_outcomes)
                                outcome_hist = filtered[:20] if filtered is not None else None
                        else:
                            outcome_hist = await _db.get_recent_outcomes(
                                agent_id=ctx.agent_id,
                                limit=20,
                                since_hours=24.0,
                            )
                except Exception as e:
                    logger.debug(f"Outcome history unavailable for {ctx.agent_id}: {e}")

                # Pass outcome history call-locally into agent_state so pre-acceptance
                # preparation never mutates the shared monitor cache before lock
                # acquisition and checkpoint acceptance.
                if outcome_hist is not None:
                    ctx.agent_state["_outcome_history"] = outcome_hist

                agent_cal_channel = (
                    ctx._calibration_signal.get("agent_channel")
                    if isinstance(ctx._calibration_signal, dict)
                    else None
                )

                decision_history = list(monitor.state.decision_history)
                coherence_history = list(monitor.state.coherence_history)
                regime_history = list(getattr(monitor.state, 'regime_history', []))
                E_history = list(monitor.state.E_history)
                I_history = list(monitor.state.I_history)
                S_history = list(monitor.state.S_history)
                V_history = list(monitor.state.V_history)
                behavioral_sensor_inputs = dict(
                    decision_history=decision_history,
                    coherence_history=coherence_history,
                    regime_history=regime_history,
                    E_history=E_history,
                    I_history=I_history,
                    S_history=S_history,
                    V_history=V_history,
                    calibration_error=cal_error,
                    drift_norm=drift_n,
                    complexity_divergence=comp_div,
                    continuity_E_input=cont_E,
                    continuity_I_input=cont_I,
                    continuity_S_input=cont_S,
                    outcome_history=outcome_hist,
                    tool_error_rate=tool_err,
                    tool_call_velocity=tool_vel,
                    unique_tools_ratio=tool_div,
                    agent_calibration_channel=agent_cal_channel,
                )
                behavioral_eisv = compute_behavioral_sensor_eisv(
                    **behavioral_sensor_inputs
                )
                # A failed shadow is unavailable evidence, never a reason to
                # skip the live sensor handoff or the other measurements.
                try:
                    behavioral_shadow = compute_legacy_coherence_dependency_shadow(
                        **behavioral_sensor_inputs,
                        deployed_observation=behavioral_eisv,
                    )
                except Exception as shadow_exc:  # noqa: BLE001 -- measurement only
                    behavioral_shadow = {
                        "schema": LEGACY_COHERENCE_ABLATION_SCHEMA,
                        "mode": "measurement_only",
                        "policy_applied": False,
                        "eligible": False,
                        "eligibility_reason": f"shadow_error:{type(shadow_exc).__name__}",
                        "deployed": None,
                        "candidate": None,
                        "candidate_minus_deployed": None,
                    }
                # Sibling ablation: decision_e is the LARGEST term in E (0.35
                # with outcomes, 0.40 without) and reads governance_monitor's
                # own prior verdicts, so E -> basin -> guide -> decision_e -> E
                # is topologically present. This one-step neutralization shows
                # whether the channel is excited on the current check-in; it
                # cannot identify closed-loop gain, cross-checkin propagation,
                # or a basin-boundary counterfactual (the payload says so under
                # `cannot_measure`). Measurement-only, same contract as the
                # sibling above.
                # A failed shadow is unavailable evidence, never a reason to
                # skip the live sensor handoff or the other measurements.
                try:
                    decision_shadow = compute_decision_self_loop_shadow(
                        **behavioral_sensor_inputs,
                        deployed_observation=behavioral_eisv,
                    )
                except Exception as shadow_exc:  # noqa: BLE001 -- measurement only
                    decision_shadow = {
                        "schema": DECISION_SELF_LOOP_ABLATION_SCHEMA,
                        "mode": "measurement_only",
                        "policy_applied": False,
                        "eligible": False,
                        "eligibility_reason": f"shadow_error:{type(shadow_exc).__name__}",
                        "deployed": None,
                        "candidate": None,
                        "candidate_minus_deployed": None,
                    }
                # Third sibling, for S: detect_regime labels regimes from
                # behavioral S's own change and _regime_instability counts the
                # flips at 35% of S (ledger row 46). Guarded separately: this
                # block also hands the deployed observation to the monitor, and
                # a shadow must never be able to interrupt that.
                try:
                    regime_shadow = compute_regime_self_loop_shadow(
                        **behavioral_sensor_inputs,
                        deployed_observation=behavioral_eisv,
                    )
                except Exception as regime_exc:  # noqa: BLE001 -- measurement only
                    regime_shadow = {
                        "schema": REGIME_SELF_LOOP_ABLATION_SCHEMA,
                        "mode": "measurement_only",
                        "policy_applied": False,
                        "eligible": False,
                        "eligibility_reason": f"shadow_error:{type(regime_exc).__name__}",
                        "deployed": None,
                        "candidate": None,
                        "candidate_minus_deployed": None,
                    }
                try:
                    calibration_free_i_shadow = compute_calibration_free_i_shadow(
                        **behavioral_sensor_inputs,
                        deployed_observation=behavioral_eisv,
                    )
                except Exception as cal_free_exc:  # noqa: BLE001 -- measurement only
                    calibration_free_i_shadow = {
                        "schema": CALIBRATION_FREE_I_ABLATION_SCHEMA,
                        "mode": "measurement_only",
                        "policy_applied": False,
                        "eligible": False,
                        "eligibility_reason": f"shadow_error:{type(cal_free_exc).__name__}",
                        "deployed": None,
                        "candidate": None,
                        "candidate_minus_deployed": None,
                    }
                ctx.agent_state["_eisv_shadow_ablations"] = {
                    "legacy_coherence_neutralized": {
                        "behavioral_sensor": behavioral_shadow,
                    },
                    "decision_self_loop_neutralized": {
                        "behavioral_sensor": decision_shadow,
                    },
                    "regime_self_loop_neutralized": {
                        "behavioral_sensor": regime_shadow,
                    },
                    "calibration_free_i": {
                        "behavioral_sensor": calibration_free_i_shadow,
                    },
                }
                ctx.agent_state["_eisv_derivation"] = build_behavioral_derivation(
                    decision_history=decision_history,
                    coherence_history=coherence_history,
                    regime_history=regime_history,
                    E_history=E_history,
                    I_history=I_history,
                    S_history=S_history,
                    V_history=V_history,
                    calibration_error=cal_error,
                    drift_norm=drift_n,
                    complexity_divergence=comp_div,
                    continuity_E_input=cont_E,
                    continuity_I_input=cont_I,
                    continuity_S_input=cont_S,
                    outcome_history=outcome_hist,
                    tool_error_rate=tool_err,
                    tool_call_velocity=tool_vel,
                    unique_tools_ratio=tool_div,
                    computed=behavioral_eisv,
                    substrate_canaries=substrate_canaries,
                    calibration_signal=ctx._calibration_signal,
                    coherence_source=LEGACY_COHERENCE_SOURCE,
                    coherence_role=ODE_CONTROL_FEEDBACK_ROLE,
                )
                if behavioral_eisv:
                    ctx.agent_state["sensor_eisv"] = behavioral_eisv
                    ctx.agent_state["sensor_eisv_source"] = "behavioral"
                    logger.debug(f"Behavioral sensor_eisv injected for {ctx.agent_id}: {behavioral_eisv}")
        except Exception as e:
            logger.debug(f"Behavioral sensor skipped for {ctx.agent_id}: {e}")
            pass  # Fail-safe: ODE runs open-loop if anything fails

    # Pre-load behavioral baseline outside the agent lock. `ensure_baseline_loaded`
    # has an in-memory cache (`_baselines` dict) — calling it here primes the
    # cache so the in-lock anomaly check at `execute_locked_update` (this file's
    # ~line 833) hits the cache instead of waiting on a cold-start PG roundtrip.
    # Same fix-shape as PR #360's `_hydrate_metadata_cache_async` — moves a
    # sequential PG await out of the critical section without changing
    # semantics. Failure is non-fatal; the in-lock anomaly block has its own
    # try/except that just skips entropy injection if the baseline isn't ready.
    try:
        from src.agent_behavioral_baseline import ensure_baseline_loaded
        await ensure_baseline_loaded(ctx.agent_id)
    except Exception as e:
        logger.debug(f"Baseline preload skipped for {ctx.agent_id}: {e}")

    # Policy checks
    from ..validators import (
        validate_file_path_policy,
        validate_agent_id_policy,
        detect_script_creation_avoidance
    )

    ctx.policy_warnings = []
    response_text = ctx.agent_state["response_text"]

    if ctx.dialectic_enforcement_warning:
        ctx.policy_warnings.append(ctx.dialectic_enforcement_warning)

    agent_id_warning, _ = validate_agent_id_policy(ctx.agent_id)
    if agent_id_warning:
        ctx.policy_warnings.append(agent_id_warning)

    avoidance_warnings = detect_script_creation_avoidance(response_text)
    if avoidance_warnings:
        ctx.policy_warnings.extend(avoidance_warnings)

    file_patterns = re.findall(r'(?:test_|demo_)\w+\.py', response_text)
    for file_pattern in file_patterns:
        warning, _ = validate_file_path_policy(file_pattern)
        if warning:
            ctx.policy_warnings.append(warning)

    if re.search(r'(?:creat|writ|generat)(?:e|ing|ed).*(?:test_|demo_)\w+\.py', response_text, re.IGNORECASE):
        if not re.search(r'tests?/', response_text, re.IGNORECASE):
            ctx.policy_warnings.append(
                "POLICY REMINDER: Creating test scripts? They belong in tests/ directory.\n"
                "See AI_ASSISTANT_GUIDE.md for details."
            )




async def execute_locked_update(ctx: UpdateContext) -> Optional[Sequence[TextContent]]:
    """Ensure agent exists, run agent-state mutations, call ODE update.

    Must be called inside the agent lock context manager.
    Caller must have already invoked prepare_unlocked_inputs(ctx) so
    ctx.agent_state and ctx.policy_warnings are populated.
    Returns an early-exit error response, or None to continue.
    """
    mcp_server = ctx.mcp_server

    # Ensure agent exists
    if ctx.is_new_agent:
        # #425 Path C: when STRICT_IDENTITY_REQUIRED is on, refuse to
        # auto-create the agent here — process_agent_update should not
        # mint identity. Caller must onboard() first. Default off; gated
        # by env flag for staged rollout.
        from src.mcp_handlers.identity_bootstrap import is_strict_identity_required
        if is_strict_identity_required():
            logger.info(
                "[PROCESS_UPDATE] STRICT_IDENTITY_REQUIRED=true and "
                "agent %s... not in PG — refusing to auto-create "
                "(#425 Path C)",
                (ctx.agent_id or "")[:12],
            )
            from src.mcp_handlers.identity_bootstrap import (
                strict_identity_refusal_payload,
            )
            from src.mcp_handlers.response_base import success_response
            # Single-sourced refusal shape (review fold, PR #610) — only
            # the hint is Path-C-specific.
            return success_response(strict_identity_refusal_payload(
                "process_agent_update",
                hint=(
                    "Agent is not registered. Call start_session() first to "
                    "mint identity, then retry the update."
                ),
            ))

        purpose = ctx.arguments.get("purpose")
        purpose_str = purpose.strip() if purpose and isinstance(purpose, str) else None
        ctx.api_key = secrets.token_urlsafe(32)

        pg_create_succeeded = False
        try:
            agent_record, created_agent = await agent_storage.get_or_create_agent(
                agent_id=ctx.agent_id,
                api_key=ctx.api_key,
                status='active',
                purpose=purpose_str,
            )
            pg_create_succeeded = True
            logger.debug(f"PostgreSQL: Created agent {ctx.agent_id}")
            await ctx.loop.run_in_executor(
                None,
                lambda: mcp_server.get_or_create_metadata(
                    ctx.agent_id,
                    purpose=purpose_str,
                    emit_lifecycle_created=created_agent,
                )
            )
            ctx.meta = mcp_server.agent_metadata.get(ctx.agent_id)
            if ctx.meta:
                ctx.meta.api_key = ctx.api_key
            # S8a Phase-2: stamp default class tag on the auto-create path so
            # process_agent_update-first agents land in the same class partition
            # as onboard-first agents. Without this, the day-7 audit found 72 of
            # 200 in-window identities untagged (claude_desktop-claude with 441
 # updates among them). .
            if created_agent:
                try:
                    from src.grounding.onboard_classifier import stamp_default_class_tags
                    stamped = await stamp_default_class_tags(
                        ctx.agent_id, ctx.label, meta=ctx.meta
                    )
                    if stamped is not None:
                        logger.info(
                            f"[PROCESS_UPDATE] S8a default-stamp: {ctx.agent_id[:8]}... "
                            f"tagged {stamped} (label={ctx.label!r})"
                        )
                except Exception as stamp_err:
                    logger.debug(
                        f"[PROCESS_UPDATE] default-stamp failed (non-fatal): {stamp_err}"
                    )
        except Exception as e:
            logger.warning(f"PostgreSQL create agent failed: {e}", exc_info=True)
            ctx.meta = await ctx.loop.run_in_executor(
                None,
                lambda: mcp_server.get_or_create_metadata(ctx.agent_id, purpose=purpose_str)
            )
            if pg_create_succeeded:
                # PG already has the freshly-generated ctx.api_key — sync the
                # fallback metadata to match instead of overwriting ctx with a
                # stale value from the cache. Without this, ctx.api_key gets
                # silently replaced and the agent's auth desyncs from PG.
                if ctx.meta:
                    ctx.meta.api_key = ctx.api_key
            else:
                # PG insert never happened; the metadata cache is source of
                # truth for whatever credential downstream code will see.
                ctx.api_key = ctx.meta.api_key if ctx.meta else None
    else:
        try:
            agent_record = await agent_storage.get_agent(ctx.agent_id)
            if agent_record:
                ctx.api_key = agent_record.api_key if agent_record.api_key else None
                if ctx.agent_id not in mcp_server.agent_metadata:
                    await ctx.loop.run_in_executor(None, mcp_server.get_or_create_metadata, ctx.agent_id)
                ctx.meta = mcp_server.agent_metadata.get(ctx.agent_id)
                if ctx.meta and ctx.api_key:
                    ctx.meta.api_key = ctx.api_key
            else:
                ctx.meta = mcp_server.agent_metadata.get(ctx.agent_id)
                ctx.api_key = ctx.meta.api_key if ctx.meta else None
        except Exception:
            ctx.meta = mcp_server.agent_metadata.get(ctx.agent_id)
            ctx.api_key = ctx.meta.api_key if ctx.meta else None

    # Capture previous void state for CIRS
    ctx.previous_void_active = False
    try:
        monitor = mcp_server.monitors.get(ctx.agent_id)
        if monitor and hasattr(monitor.state, 'void_active'):
            ctx.previous_void_active = bool(monitor.state.void_active)
    except Exception:
        pass

    # Preload agent baseline from PostgreSQL (if not already cached in-memory)
    try:
        from governance_core import get_baseline_or_none, set_agent_baseline, AgentBaseline
        if get_baseline_or_none(ctx.agent_id) is None:
            from src.db import get_db
            db = get_db()
            baseline_data = await db.load_agent_baseline(ctx.agent_id)
            if baseline_data:
                set_agent_baseline(ctx.agent_id, AgentBaseline.from_dict(baseline_data))
                logger.debug(f"Loaded baseline from PostgreSQL for {ctx.agent_id[:12]}...")
    except Exception as e:
        logger.debug(f"Baseline preload skipped: {e}")

    # Track Thread Identity across sessions. See `_track_thread_identity` for
    # the #424 fix: no random-UUID mint mid-update; hydrate from PG, then fall
    # back to deterministic derivation from session_key (matches onboard).
    session_changed = await _track_thread_identity(ctx)

    if session_changed:
        # Re-stamp R6 fork fields into the durable S22 envelope using the
        # post-mutation node_index. The early stamp at prepare_unlocked_inputs
        # ran before this block bumped node_index, so without the re-stamp
        # a fresh-session transition would persist episode_fork_kind="none"
        # while enrich_thread_identity (order=230) returns "sibling_locus"
        # in the response. See _restamp_fork_after_thread_identity_update.
        _restamp_fork_after_thread_identity_update(ctx)

        # Fire-and-forget: PG metadata persist doesn't need to hold the
        # agent lock. In-memory ctx.meta has already been mutated above
        # and is the process-local source of truth; PG copy is for
        # cross-process visibility. Same pattern as PR #360.
        try:
            _thread_metadata_snapshot = {
                "thread_id": ctx.meta.thread_id,
                "node_index": ctx.meta.node_index,
                "active_session_key": ctx.meta.active_session_key,
            }
            _spawn_persist_task(
                _persist_thread_identity_async(ctx.agent_uuid, _thread_metadata_snapshot),
                name="persist_thread_identity",
            )
        except RuntimeError:
            # No running loop — extremely unusual for this code path
            # since we're inside an async handler. Swallow per the
            # PR #360 precedent.
            pass
        except Exception as e:
            logger.debug(f"Could not schedule thread identity persist: {e}")

    # Per-agent anomaly detection: add entropy if current signals deviate from baseline
    try:
        from src.agent_behavioral_baseline import (
            ensure_baseline_loaded, compute_anomaly_entropy,
        )
        baseline = await ensure_baseline_loaded(ctx.agent_id)
        # Collect current signals for anomaly check
        anomaly_signals = {}
        try:
            from src.tool_usage_tracker import get_tool_usage_tracker
            _tu = get_tool_usage_tracker().get_usage_stats(
                agent_id=ctx.agent_id, window_hours=1,
            )
            _tu_total = _tu.get("total_calls", 0)
            if _tu_total > 0:
                _tu_failed = sum(
                    t.get("error_count", 0)
                    for t in _tu.get("tools", {}).values()
                )
                anomaly_signals["tool_error_rate"] = _tu_failed / _tu_total
                anomaly_signals["tool_call_velocity"] = _tu_total / 60.0
        except Exception:
            pass

        _mon = mcp_server.monitors.get(ctx.agent_id)

        # Complexity divergence from continuity metrics (previous check-in's cm)
        if _mon:
            from src.dual_log.continuity import should_exclude_template_divergence
            _cm = getattr(_mon, '_last_continuity_metrics', None)
            if (
                _cm
                and hasattr(_cm, 'complexity_divergence')
                and not should_exclude_template_divergence(_cm)
            ):
                anomaly_signals["complexity_divergence"] = _cm.complexity_divergence

        anomaly_noise = compute_anomaly_entropy(baseline, anomaly_signals)
        if anomaly_noise > 0:
            existing_noise = ctx.agent_state.get("noise_S", 0.0) or 0.0
            ctx.agent_state["noise_S"] = existing_noise + anomaly_noise
            logger.debug(
                f"Anomaly entropy +{anomaly_noise:.3f} for {ctx.agent_id} "
                f"(total noise_S={ctx.agent_state['noise_S']:.3f})"
            )
    except Exception as e:
        logger.debug(f"Anomaly detection skipped for {ctx.agent_id}: {e}")

    # Execute ODE update — timed for Wave 3 RFC §0 disconfirmer (A′.1).
 # B1.2 — A′.1 measures
    # whether >60% of process_agent_update p99 floor lives in this call.
    # The surrounding [checkin_phases] log line in update_workflow_service.py
    # already captures locked_update_total + total_ms; this wrap captures the
    # ODE-only time so A′.1 = ode_call_ms / total_ms is computable.
    ctx.agent_state["task_type"] = ctx.task_type

    _ode_start = time.perf_counter()
    from src.services.checkpoint_service import accept_before_assessment

    async def _accept_checkpoint():
        await accept_before_assessment(ctx)

    try:
        ctx.result = await mcp_server.process_update_authenticated_async(
            agent_id=ctx.agent_id,
            api_key=ctx.api_key,
            agent_state=ctx.agent_state,
            auto_save=True,
            confidence=ctx.confidence,
            session_bound=True,
            before_assessment=_accept_checkpoint,
        )
    except PermissionError:
        raise
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in process_update_authenticated_async: {e}", exc_info=True)
        raise Exception(f"Error processing update: {str(e)}") from e
    finally:
        _perf_record_ms("phases.ode_call_ms", (time.perf_counter() - _ode_start) * 1000)

    # Cache monitor reference for Phase 5 and Phase 6 (guaranteed to exist post-ODE)
    ctx.monitor = mcp_server.monitors.get(ctx.agent_id)

    # Auto-infer purpose from response_text if agent has none.
    # In-memory mutation stays inside the lock (cheap); PG persist moves
    # to fire-and-forget so the agent lock isn't held across a PG UPDATE
    # roundtrip. Same pattern as PR #360.
    try:
        meta = ctx.meta
        if meta and not getattr(meta, 'purpose', None) and ctx.response_text:
            inferred = _infer_purpose(ctx.response_text)
            if inferred:
                meta.purpose = inferred
                try:
                    _spawn_persist_task(
                        _persist_inferred_purpose_async(ctx.agent_id, inferred),
                        name="persist_inferred_purpose",
                    )
                except RuntimeError:
                    pass
                except Exception as e:
                    logger.debug(f"Could not schedule inferred purpose persist: {e}")
    except Exception as e:
        logger.debug(f"Purpose inference skipped: {e}")

    return None  # Continue

# ─── Phase 5: Post-Update Side Effects ─────────────────────────────────


async def _post_update_cirs_and_drift(ctx: UpdateContext) -> None:
    agent_id = ctx.agent_id
    void_active = ctx.metrics_dict.get("void_active", False)
    # CIRS: Void alert
    ctx.cirs_alert = None
    try:
        from ..cirs.protocol import maybe_emit_void_alert
        V_value = ctx.metrics_dict.get('V', 0.0)
        ctx.cirs_alert = maybe_emit_void_alert(
            agent_id=agent_id,
            V=V_value,
            void_active=void_active,
            coherence=ctx.coherence or 0.5,
            risk_score=ctx.risk_score or 0.0,
            previous_void_active=ctx.previous_void_active
        )
    except Exception as e:
        logger.debug(f"CIRS void_alert auto-emit skipped: {e}")

    # CIRS: State announce
    ctx.cirs_state_announce = None
    try:
        from ..cirs.protocol import auto_emit_state_announce
        monitor = ctx.monitor
        ctx.cirs_state_announce = auto_emit_state_announce(
            agent_id=agent_id,
            metrics=ctx.metrics_dict,
            monitor_state=monitor.state
        )
    except Exception as e:
        logger.debug(f"CIRS state_announce auto-emit skipped: {e}")

    # CIRS: Resonance signal
    try:
        from ..cirs.protocol import maybe_emit_resonance_signal
        cirs_data = ctx.result.get('cirs', {})
        monitor = ctx.monitor
        was_resonant = False
        if monitor and hasattr(monitor, 'adaptive_governor') and monitor.adaptive_governor:
            was_resonant = monitor.adaptive_governor.state.was_resonant
        maybe_emit_resonance_signal(
            agent_id=agent_id,
            cirs_result=cirs_data,
            was_resonant=was_resonant,
        )
    except Exception as e:
        logger.debug(f"CIRS resonance auto-emit skipped: {e}")

    # CIRS: Persist resonance event to PostgreSQL
    try:
        cirs_data = ctx.result.get('cirs', {})
        if cirs_data.get('resonant'):
            from src.db import get_db
            _db = get_db()
            if _db:
                await _db.record_outcome_event(
                    agent_id=agent_id,
                    outcome_type='cirs_resonance',
                    is_bad=True,
                    outcome_score=0.0,
                    session_id=ctx.arguments.get('client_session_id'),
                    eisv_e=ctx.metrics_dict.get('E'),
                    eisv_i=ctx.metrics_dict.get('I'),
                    eisv_s=ctx.metrics_dict.get('S'),
                    eisv_v=ctx.metrics_dict.get('V'),
                    eisv_phi=ctx.metrics_dict.get('phi'),
                    eisv_verdict=ctx.metrics_dict.get('verdict'),
                    eisv_coherence=ctx.metrics_dict.get('coherence'),
                    eisv_regime=ctx.metrics_dict.get('regime'),
                    detail={
                        'source': 'cirs_resonance',
                        'oi': cirs_data.get('oi'),
                        'flips': cirs_data.get('flips'),
                        'trigger': cirs_data.get('trigger'),
                        'response_tier': cirs_data.get('response_tier'),
                    },
                    # CIRS resonance is computed server-side from telemetry,
                    # not claimed by the agent.
                    verification_source='server_observation',
                )
                logger.info(f"CIRS resonance event persisted for {agent_id}")
    except Exception as e:
        logger.debug(f"CIRS resonance persistence skipped: {e}")

    # No drift-triggered review. One lived here, keyed on three consecutive
    # high-drift updates, and it never ran after the 2026-03-09 package reorg:
    # its import of is_agent_in_active_session failed and the error was logged
    # at debug level. Removed 2026-09-27 rather than revived: a review nobody
    # asked for has nobody to answer the reviewer, and an unanswered rejection
    # is already how most dialectic sessions strand.


async def _persist_agent_state_row(
    ctx: UpdateContext,
    behavioral_snapshot: Optional[dict],
    eisv_telemetry: Optional[dict],
) -> bool:
    agent_id = ctx.agent_id
    monitor = ctx.monitor
    # Computed once, before the write: make_report_excerpt never raises, so a
    # bad report can cost the excerpt but never the state row.
    report_excerpt = make_report_excerpt(ctx.response_text)

    # PostgreSQL: Record EISV state
    # Failure to receive an acknowledgement does not establish that no row
    # committed. Keep this call-local, including while the writer is awaiting.
    ctx.state_persistence = StatePersistenceReceipt()
    try:
        state_id = await agent_storage.record_agent_state(
            agent_id=agent_id,
            E=ctx.metrics_dict.get('E', 0.7),
            I=ctx.metrics_dict.get('I', 0.8),
            S=ctx.metrics_dict.get('S', 0.1),
            V=ctx.metrics_dict.get('V', 0.0),
            regime=ctx.metrics_dict.get('regime', 'EXPLORATION'),
            coherence=ctx.metrics_dict.get('coherence', 0.5),
            health_status=ctx.health_status.value,
            risk_score=ctx.risk_score,
            phi=ctx.metrics_dict.get('phi', 0.0),
            verdict=ctx.metrics_dict.get('verdict', 'continue'),
            action=(ctx.result.get('decision') or {}).get('sub_action')
                or (ctx.result.get('decision') or {}).get('action'),
            provenance_context=ctx.agent_state.get("provenance_context"),
            epistemic_class=ctx.epistemic_class,
            behavioral_eisv=behavioral_snapshot,
            sensor_eisv_source=ctx.agent_state.get("sensor_eisv_source"),
            eisv_telemetry=eisv_telemetry,
            coherence_form=getattr(ctx, "coherence_form", None),
            eisv_instrument=_eisv_instrument_stamp(monitor),
            report_excerpt=report_excerpt,
        )
        ctx.state_persistence = StatePersistenceReceipt.from_acknowledgement(state_id)
        logger.debug(f"PostgreSQL: Recorded state for {agent_id}")
    except ValueError:
        from src.mcp_handlers.identity_bootstrap import is_strict_identity_required
        if is_strict_identity_required():
            logger.warning(
                "[PROCESS_UPDATE] STRICT_IDENTITY_REQUIRED=true and "
                "agent %s... missing from PG at record_agent_state — "
                "skipping self-create (#425 Path D); state persistence "
                "is unconfirmed.",
                (agent_id or "")[:12],
            )
            return False
        logger.debug(f"Agent {agent_id} not found, creating...")
        try:
            await _recover_missing_agent_for_state_record(ctx)
            state_id = await agent_storage.record_agent_state(
                agent_id=agent_id,
                E=ctx.metrics_dict.get('E', 0.7),
                I=ctx.metrics_dict.get('I', 0.8),
                S=ctx.metrics_dict.get('S', 0.1),
                V=ctx.metrics_dict.get('V', 0.0),
                regime=ctx.metrics_dict.get('regime', 'EXPLORATION'),
                coherence=ctx.metrics_dict.get('coherence', 0.5),
                health_status=ctx.health_status.value,
                risk_score=ctx.risk_score,
                phi=ctx.metrics_dict.get('phi', 0.0),
                verdict=ctx.metrics_dict.get('verdict', 'continue'),
                action=(ctx.result.get('decision') or {}).get('sub_action')
                    or (ctx.result.get('decision') or {}).get('action'),
                provenance_context=ctx.agent_state.get("provenance_context"),
                epistemic_class=ctx.epistemic_class,
                behavioral_eisv=behavioral_snapshot,
                sensor_eisv_source=ctx.agent_state.get("sensor_eisv_source"),
                eisv_telemetry=eisv_telemetry,
                coherence_form=getattr(ctx, "coherence_form", None),
                eisv_instrument=_eisv_instrument_stamp(monitor),
                report_excerpt=report_excerpt,
            )
            ctx.state_persistence = StatePersistenceReceipt.from_acknowledgement(state_id)
            logger.debug(f"PostgreSQL: Created agent and recorded state for {agent_id}")
        except Exception as create_error:
            logger.warning(f"PostgreSQL create+record failed: {create_error}", exc_info=True)
    except Exception as e:
        logger.warning(f"PostgreSQL record_agent_state failed: {e}", exc_info=True)
    finally:
        if (
            not ctx.state_persistence.confirmed
            and STATE_PERSISTENCE_UNCONFIRMED_WARNING not in ctx.warnings
        ):
            ctx.warnings.append(STATE_PERSISTENCE_UNCONFIRMED_WARNING)
    return True


async def _record_auto_outcome_event(db, **kwargs):
    return await db.record_outcome_event(**kwargs)




async def _post_update_trajectory(ctx: UpdateContext) -> None:
    agent_id = ctx.agent_id
    # Auto-record trajectory self-validation outcome
    try:
        tv = ctx.result.get('trajectory_validation') if ctx.result else None
        if tv is not None:
            from src.db import get_db
            _db = get_db()
            if _db:
                await _db.record_outcome_event(
                    agent_id=agent_id,
                    outcome_type='trajectory_validated',
                    is_bad=(tv['quality'] < 0.4),
                    outcome_score=tv['quality'],
                    session_id=ctx.arguments.get('client_session_id'),
                    eisv_e=ctx.metrics_dict.get('E'),
                    eisv_i=ctx.metrics_dict.get('I'),
                    eisv_s=ctx.metrics_dict.get('S'),
                    eisv_v=ctx.metrics_dict.get('V'),
                    eisv_phi=ctx.metrics_dict.get('phi'),
                    eisv_verdict=ctx.metrics_dict.get('verdict'),
                    eisv_coherence=ctx.metrics_dict.get('coherence'),
                    eisv_regime=ctx.metrics_dict.get('regime'),
                    detail={
                        'source': 'trajectory_self_validation',
                        'prev_verdict': tv['prev_verdict'],
                        'prev_norm': tv['prev_norm'],
                        'current_norm': tv['current_norm'],
                        'norm_delta': tv['norm_delta'],
                        'improvement': tv.get('improvement'),
                    },
                    # Quality computed server-side from ctx.result trajectory data.
                    verification_source='server_observation',
                )
    except Exception as e:
        logger.debug(f"Trajectory validation record skipped: {e}")


async def _post_update_phase5_evidence(ctx: UpdateContext) -> None:
    """Record recent tool results when Phase-5 evidence writes are enabled."""
    # Phase-5: iterate self-reported tool evidence. Spec §2 + §8.
    # ctx.recent_tool_results was populated in transform_inputs (sync phase).
    # Evidence arrives as plain dicts (model_dump() flattens Pydantic models).
    evidence_mode = os.environ.get("UNITARES_PHASE5_EVIDENCE_WRITE", "").lower()
    if ctx.recent_tool_results and evidence_mode in ("shadow", "1", "enable"):
        from src.outcomes import record_outcome
        # #1320: disclose what happens to the evidence. "recorded" rows still
        # carry verification_source="agent_reported_tool_result" downstream —
        # recorded means graded-and-stored, not independently verified.
        ctx.result["evidence_status"] = (
            "recorded:shadow" if evidence_mode == "shadow" else "recorded"
        )
        for evidence in ctx.recent_tool_results:
            try:
                outcome_type, is_bad = _derive_outcome(evidence)
                detail = {
                    "tool": evidence.get("tool", "?"),
                    "summary": evidence.get("summary", ""),
                    "kind": evidence.get("kind"),
                    "exit_code": evidence.get("exit_code"),
                    "phase5_emitter": True,
                }
                if evidence_mode == "shadow":
                    detail["shadow_write"] = True
                await record_outcome({
                    # In-process emitter: vouched, but its ceiling still comes
                    # from the provenance below, so these agent-attested rows
                    # stay capped exactly as the public path's do.
                    "_trusted_ingestion": True,
                    "outcome_type": outcome_type,
                    "is_bad": is_bad,
                    "prediction_id": _prediction_id_for_phase5_evidence(ctx, evidence),
                    "confidence": ctx.confidence,
                    "verification_source": "agent_reported_tool_result",
                    "detail": detail,
                    "agent_id": ctx.agent_id,
                    "client_session_id": ctx.arguments.get("client_session_id"),
                })
            except Exception as e:
                ctx.warnings.append(
                    f"evidence record failed for tool={evidence.get('tool', '?')}: {e}"
                )
                logger.debug("Phase-5 evidence record failed: %s", e, exc_info=True)
    elif ctx.recent_tool_results:
        # Default off: log per-item count only, per spec §8.
        logger.info(
            "Phase-5 evidence iteration skipped (UNITARES_PHASE5_EVIDENCE_WRITE unset); "
            "would have processed %d items for agent=%s",
            len(ctx.recent_tool_results), ctx.agent_id,
        )
        # #1320: the deploy gate (spec §8) is server policy, but silence toward
        # the caller is not part of it. Without this, the payload validates,
        # persists into the state row, and reads like evidence downstream while
        # having had zero effect on scoring — disclose that explicitly.
        ctx.result["evidence_status"] = "ignored:flag_off"
        warning = (
            f"recent_tool_results ({len(ctx.recent_tool_results)} item(s)) was "
            "accepted but NOT scored or recorded: evidence recording is "
            "disabled on this server (UNITARES_PHASE5_EVIDENCE_WRITE unset). "
            "The items did not affect risk, coherence, or calibration."
        )
        if warning not in ctx.warnings:
            ctx.warnings.append(warning)


async def execute_post_update_effects(ctx: UpdateContext) -> None:
    """Run post-update effects in their established fail-soft order."""
    await _post_update_health_and_baselines(ctx)
    await _post_update_cirs_and_drift(ctx)
    if not await _post_update_record_state(ctx):
        return
    await _post_update_save_baseline(ctx)
    await _post_update_auto_outcome(ctx)
    await _post_update_trajectory(ctx)
    await _post_update_phase5_evidence(ctx)
    await _r2_post_update_hook(ctx)
