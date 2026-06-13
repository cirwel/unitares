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

from .context import UpdateContext
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
    "explicit_client_session_id",
    "explicit_client_session_id_scoped",
    "mcp_session_id",
    "x_session_id",
    "oauth_client_id",
    # X-Unitares-Operator bearer token, validated against the env allowlist
    # on every call (#425 dashboard-identity decision) — per-call proof,
    # stronger than the stable-header sources above.
    "operator_token",
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

    return {
        "tier": tier,
        "score": score,
        "session_source": source_key,
        "trajectory_confidence": trajectory_confidence,
        "reason": reason,
        "caller_proven": caller_proven,
        "proof_origin": proof_origin or "unknown",
    }

# ─── Purpose Inference ─────────────────────────────────────────────────

_PURPOSE_KEYWORDS = {
    'debugging': ('debug', 'fix', 'bug', 'error', 'traceback', 'exception', 'crash'),
    'implementation': ('implement', 'build', 'create', 'add', 'feature', 'develop'),
    'testing': ('test', 'assert', 'coverage', 'pytest', 'unittest', 'spec'),
    'review': ('review', 'audit', 'inspect', 'check', 'verify', 'validate'),
    'deployment': ('deploy', 'release', 'ship', 'publish', 'launch', 'rollout'),
    'exploration': ('explore', 'research', 'investigate', 'analyze', 'understand', 'learn'),
}

def _infer_purpose(response_text: str) -> Optional[str]:
    """Infer agent purpose from response text via keyword matching."""
    text_lower = response_text.lower()
    best_purpose = None
    best_count = 0
    for purpose, keywords in _PURPOSE_KEYWORDS.items():
        count = sum(1 for kw in keywords if kw in text_lower)
        if count > best_count:
            best_count = count
            best_purpose = purpose
    return best_purpose if best_count >= 1 else None


def _derive_outcome(evidence: dict) -> tuple:
    """Map a ToolResultEvidence dict to (outcome_type, is_bad) per spec §1 mapping table.

    Evidence arrives as a plain dict because params_step.py calls model_dump() which
    flattens nested Pydantic models. Use dict access throughout.
    """
    is_bad = evidence.get("is_bad")
    if is_bad is None:
        exit_code = evidence.get("exit_code")
        is_bad = (exit_code is not None and exit_code != 0)
    kind = evidence.get("kind")
    if kind == "test":
        return ("test_failed" if is_bad else "test_passed", is_bad)
    if kind == "tool_call" and is_bad:
        return ("tool_rejected", is_bad)
    return ("task_failed" if is_bad else "task_completed", is_bad)


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
        return [error_response("Identity not resolved. Try calling identity() first.")]

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
            exempt = False
            try:
                from src.db import get_db
                exempt = await get_db().is_substrate_earned(ctx.agent_uuid)
                if not exempt:
                    # is_substrate_earned only checks core.substrate_claims +
                    # the (currently sentinel-only) Pi allowlist. Also honor the
                    # canonical substrate-earned pattern so embodied/anchored
                    # residents that resolve by fingerprint are not refused —
                    # notably Lumen (embodied tag, NOT in substrate_claims).
                    # Key on `dedicated_substrate` (embodied, or persistent+
                    # anchor) — the "real substrate-anchored resident" signal —
                    # not full R4 `earned`, so a freshly-restarted resident that
                    # hasn't yet met the tenure bar is still exempt. Fail-closed.
                    from src.identity.substrate import verify_substrate_earned
                    result = await verify_substrate_earned(ctx.agent_uuid)
                    exempt = bool(
                        (result.get("conditions") or {}).get("dedicated_substrate")
                    )
            except Exception:
                exempt = False
            if not exempt:
                from src.mcp_handlers.identity_bootstrap import strict_identity_refusal_payload
                from src.mcp_handlers.response_base import success_response
                logger.info(
                    "[PROCESS_UPDATE] STRICT refusing write: identity resolved via "
                    "%s (proof_origin=%s, not caller-proven) for agent %s...",
                    ctx.session_resolution_source,
                    ctx.identity_assurance.get("proof_origin"),
                    (ctx.agent_uuid or "")[:12],
                )
                return success_response(strict_identity_refusal_payload(
                    "process_agent_update",
                    hint=(
                        "This write resolved your identity by transport fingerprint, "
                        "not by a proof you supplied — under strict identity, writes "
                        "require a caller-proven binding. Echo the client_session_id "
                        "your onboard() returned (or pass continuity_token)."
                    ),
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
            from ..support.pause_ttl import maybe_auto_expire_pause_async
            expired = await maybe_auto_expire_pause_async(ctx.agent_uuid, meta)
            if not expired:
                return [error_response(
                    "Agent is paused and cannot process updates",
                    error_code="AGENT_PAUSED",
                    details={
                        "agent_id": ctx.agent_uuid[:12],
                        "paused_at": meta.paused_at,
                        "status": "paused",
                    },
                    recovery={
                        "action": "Use self_recovery(action='quick') for safe states, or self_recovery(action='review', reflection='...') for full recovery",
                        "note": "Circuit breaker triggered due to governance threshold violation",
                        "auto_recovery": "Dialectic recovery may already be in progress",
                    }
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
                    ctx.onboarding_guidance["naming"] = {
                        "message": "Name yourself to make your work easier to find",
                        "action": "Call identity(name='your_chosen_name') to set your name",
                        "suggestions": suggestions[:3],
                        "quick_example": suggestions[0]["name"] if suggestions else None
                    }
                except Exception as e:
                    logger.debug(f"Could not generate naming suggestions for onboarding: {e}")

                if open_questions:
                    ctx.onboarding_guidance["open_questions"] = {
                        "message": f"Found {len(open_questions)} open question(s) waiting for answers. Want to try responding to one?",
                        "questions": open_questions,
                        "invitation": "Use reply_to_question tool to answer any of these questions and help build shared knowledge.",
                        "tool": "reply_to_question"
                    }
                elif question_count > 0:
                    ctx.onboarding_guidance["open_questions"] = {
                        "message": f"There are {question_count} open question(s) in the knowledge graph.",
                        "suggestion": KNOWLEDGE_OPEN_QUESTIONS_WORKFLOW,
                        "tool": "reply_to_question"
                    }
        except Exception as e:
            logger.warning(f"Could not check knowledge graph for onboarding: {e}")

    # Archived-agent refusal. Historically this branch would silently
    # auto-resume archived agents on engagement, papering over orphan-sweep
    # false-positives on live residents. Removed once residents became
    # self-tagging 'persistent' (PR #39) — sweep no longer targets live agents,
    # so nothing legitimate hits the archived branch anymore. Manual archives
    # (PR #33) and the 2026-04-18 incident race (PR #33) were never supposed
    # to auto-resume anyway. Any archived agent must explicitly self_recovery
    # or onboard(force_new=true) to come back.
    meta = mcp_server.agent_metadata.get(ctx.agent_uuid)
    ctx.meta = meta

    if meta:
        if meta.status == "archived":
            logger.warning(
                f"Refused process_agent_update on archived agent {agent_id[:12]}.... "
                f"Use self_recovery(action='quick') to restore, "
                f"or onboard(force_new=true) for a new identity."
            )
            return [error_response(
                f"Agent '{agent_id}' is archived and cannot be updated.",
                recovery={
                    "action": "Use self_recovery(action='quick') to restore yourself, "
                              "or onboard(force_new=true) for a new identity",
                    "related_tools": ["self_recovery", "onboard"],
                },
                context={
                    "agent_id": agent_id,
                    "status": "archived",
                    "archived_at": getattr(meta, "archived_at", None),
                }
            )]

        elif meta.status == "paused":
            return [error_response(
                f"Agent '{agent_id}' is paused. Resume it first before processing updates.",
                recovery={
                    "action": "Check your state and resume when ready",
                    "related_tools": ["get_governance_metrics", "self_recovery"],
                    "workflow": (
                        "1. Check your state with get_governance_metrics "
                        "2. Reflect on what triggered the pause "
                        "3. Use self_recovery(action='quick') if safe (coherence > 0.60, risk < 0.40), otherwise use self_recovery(action='review', reflection='...')"
                    )
                },
                context={
                    "agent_id": agent_id,
                    "status": "paused",
                    "reason": "Circuit breaker triggered - governance threshold exceeded",
                    "note": "Paused and archived agents both require explicit recovery via self_recovery()."
                }
            )]

        elif meta.status == "deleted":
            return [error_response(
                f"Agent '{agent_id}' is deleted and cannot be used.",
                recovery={
                    "action": "Cannot recover deleted agents",
                    "related_tools": ["list_agents"],
                    "workflow": "Deleted agents are permanently removed. Use list_agents to see available agents."
                },
                context={
                    "agent_id": agent_id,
                    "status": "deleted",
                    "note": "Deleted agents cannot be recovered. Use archive_agent instead of delete_agent to preserve agent state."
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

    # Confidence & Auto-Calibration (coerce to float — same str|float|None pattern)
    raw_confidence = ctx.arguments.get("confidence")
    try:
        reported_confidence = float(raw_confidence) if raw_confidence is not None else None
    except (TypeError, ValueError):
        reported_confidence = None
    ctx.confidence = reported_confidence
    ctx.calibration_correction_info = None

    if reported_confidence is not None:
        try:
            from src.calibration import calibration_checker
            corrected, correction_info = calibration_checker.apply_confidence_correction(reported_confidence)
            if correction_info:
                ctx.calibration_correction_info = correction_info
                logger.info(f"Agent {ctx.agent_id}: {correction_info}")
            ctx.confidence = corrected
        except Exception as e:
            logger.debug(f"Calibration correction skipped: {e}")

    # Epistemic class: forward-only storage label for the state row. Pydantic
    # validates the public schema; this fallback keeps non-schema internal
    # callers on the honest default.
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


def _restamp_fork_after_thread_identity_update(ctx: UpdateContext) -> None:
    """Override S22 fork fields with the post-``node_index``-mutation classification.

    The early stamp at ``prepare_unlocked_inputs`` runs before
    ``execute_locked_update`` mutates ``ctx.meta.node_index`` on session-key
    transitions (lines ~849-858 below). Without this re-stamp, a fresh-session
    transition would persist ``episode_fork_kind="none"`` while the response
    pipeline returns ``"sibling_locus"`` — split-brain on the durable side.
    """
    from src.provenance_context import classify_fork_for_s22_context

    pc = ctx.agent_state.get("provenance_context")
    if not isinstance(pc, dict):
        return
    fork_kind, lineage_fork = classify_fork_for_s22_context(ctx.meta, ctx.agent_uuid)
    if fork_kind is not None:
        pc["episode_fork_kind"] = fork_kind
    if lineage_fork is not None:
        pc["identity_lineage_fork"] = lineage_fork


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
    }
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
        sensor_eisv = sensor_data.get("eisv")
        if sensor_eisv and isinstance(sensor_eisv, dict):
            ctx.agent_state["sensor_eisv"] = sensor_eisv

    # Behavioral sensor: compute EISV from governance observables for non-embodied agents
    if "sensor_eisv" not in ctx.agent_state:
        try:
            monitor = mcp_server.monitors.get(ctx.agent_id)
            if monitor and len(getattr(monitor.state, 'decision_history', [])) >= 3:
                from src.behavioral_sensor import compute_behavioral_sensor_eisv
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
                cm = getattr(monitor, '_last_continuity_metrics', None)
                if cm is not None:
                    comp_div = getattr(cm, 'complexity_divergence', None)
                    cont_E = getattr(cm, 'E_input', None)
                    cont_I = getattr(cm, 'I_input', None)
                    cont_S = getattr(cm, 'S_input', None)

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
                        outcome_hist = await _db.get_recent_outcomes(
                            agent_id=ctx.agent_id,
                            limit=20,
                            since_hours=24.0,
                        )
                except Exception as e:
                    logger.debug(f"Outcome history unavailable for {ctx.agent_id}: {e}")

                # Cache outcome history on monitor for use in sync process_update
                if outcome_hist is not None:
                    monitor._cached_outcome_history = outcome_hist

                behavioral_eisv = compute_behavioral_sensor_eisv(
                    decision_history=list(monitor.state.decision_history),
                    coherence_history=list(monitor.state.coherence_history),
                    regime_history=list(getattr(monitor.state, 'regime_history', [])),
                    E_history=list(monitor.state.E_history),
                    I_history=list(monitor.state.I_history),
                    S_history=list(monitor.state.S_history),
                    V_history=list(monitor.state.V_history),
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
                )
                if behavioral_eisv:
                    ctx.agent_state["sensor_eisv"] = behavioral_eisv
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


async def _track_thread_identity(ctx: "UpdateContext") -> bool:
    """Resolve `ctx.meta.thread_id` + `node_index` for a (possibly) new session.

    Closes #424 (\"two independent thread_id mint paths\"). The previous
    implementation called ``uuid.uuid4()`` mid-update whenever the in-memory
    metadata cache had no thread_id — producing a UUID-format id that
    conflicted with onboard's ``t-<sha16>`` format and resetting node_index
    to 1 even when the durable PG record knew otherwise. The 2026-05-08
    trace showed the path was reachable on a 30s session-resolve-miss
    cadence in normal operation, not a rare race.

    Resolution order (no random mints):

    1. In-memory ``ctx.meta`` already has ``thread_id``. Use it; only bump
       ``node_index`` and stamp ``active_session_key``.

    2. ``ctx.meta`` lacks ``thread_id`` → read ``core.identities.metadata``
       for the durable copy. The onboard-time persist may have committed
       there even when the in-memory cache hadn't been rehydrated yet
       (fresh process-instance, IP+UA fingerprint resolve, etc.).

    3. PG also empty → derive deterministically from ``ctx.session_key``
       via ``generate_thread_id`` — the exact function onboard uses, so
       the format stays consistent. This is the final \"no random UUID\"
       guarantee.

    Returns True when the session-key actually changed and downstream
    re-stamp/persist effects should fire; False on a no-op (already
    tracking the same session).

    **Heal-on-read for pre-#483 UUID-format thread_ids (#484, 2026-05-20).**
    886 of 2521 populated ``core.identities.metadata.thread_id`` rows still
    carry UUID-format values from the broken mint path that pre-dated #483.
    Step 1 detects them by the ``t-`` prefix check and treats them as malformed
    — the value is skipped, and step 2 derives a fresh deterministic
    ``t-<sha16>`` from ``session_key``. One update per affected agent migrates
    the row via the regular persist path. The single downstream consumer that
    keys on thread_id format is ``src/db/mixins/thread.py:71``; lookups on
    the old UUID value will miss until that agent next updates, which is the
    expected migration behavior. Spread cost, no concentrated burst.

    ``node_index`` and ``active_session_key`` are still imported from the
    persisted row — only ``thread_id`` is the corrupt field.
    """
    if not (ctx.meta and ctx.session_key):
        return False
    if getattr(ctx.meta, "active_session_key", None) == ctx.session_key:
        return False

    # Step 1: hydrate from durable PG metadata when the in-memory cache is
    # empty. ExecutorPool means we can `await` asyncpg directly here without
    # the locked-update concern (per CLAUDE.md post-PR-#218 note).
    if not getattr(ctx.meta, "thread_id", None) and ctx.agent_uuid:
        try:
            from src.db import get_db
            identity_record = await get_db().get_identity(ctx.agent_uuid)
            if identity_record and identity_record.metadata:
                persisted_thread_id = identity_record.metadata.get("thread_id")
                persisted_node_index = identity_record.metadata.get("node_index")
                persisted_session_key = identity_record.metadata.get(
                    "active_session_key"
                )
                # #484 heal-on-read: pre-#483 mint path emitted UUID-format
                # thread_ids. Treat them as malformed so step 2 re-derives
                # the canonical t-<sha16> shape via generate_thread_id.
                if persisted_thread_id and not persisted_thread_id.startswith("t-"):
                    logger.info(
                        "[PROCESS_UPDATE] heal-on-read: skipping legacy "
                        "UUID-format thread_id %s... for agent=%s...; "
                        "step 2 will re-derive canonical form",
                        persisted_thread_id[:8],
                        (ctx.agent_uuid or "?")[:8],
                    )
                    persisted_thread_id = None
                if persisted_thread_id:
                    ctx.meta.thread_id = persisted_thread_id
                if persisted_node_index is not None and not getattr(
                    ctx.meta, "node_index", None
                ):
                    ctx.meta.node_index = persisted_node_index
                if persisted_session_key is not None and getattr(
                    ctx.meta, "active_session_key", None
                ) is None:
                    ctx.meta.active_session_key = persisted_session_key
        except Exception as e:
            logger.debug(
                "thread_id PG hydration skipped for %s...: %s",
                (ctx.agent_uuid or "?")[:8], e,
            )

    # Re-check after hydration: caller may have just adopted the session
    # already (rare — only if PG had the exact same active_session_key).
    if getattr(ctx.meta, "active_session_key", None) == ctx.session_key:
        return False

    # Step 2: deterministic fallback if still no thread_id. Matches onboard
    # format; never invents a random UUID.
    if not getattr(ctx.meta, "thread_id", None):
        from src.thread_identity import generate_thread_id
        ctx.meta.thread_id = generate_thread_id(ctx.session_key)
        logger.info(
            "[PROCESS_UPDATE] thread_id fallback-derived from session_key "
            "(no in-memory cache or PG record): %s... agent=%s...",
            ctx.meta.thread_id[:8],
            (ctx.agent_uuid or "?")[:8],
        )

    # node_index bookkeeping. Re-entry into the same session leaves
    # position alone (first branch); a session transition with a tracked
    # prior session bumps (second branch). The PG hydration above already
    # imported persisted_node_index when present, so process-restart no
    # longer silently loses position continuity.
    if getattr(ctx.meta, "active_session_key", None) is None:
        ctx.meta.node_index = getattr(ctx.meta, "node_index", None) or 1
    else:
        ctx.meta.node_index = (getattr(ctx.meta, "node_index", None) or 1) + 1
    ctx.meta.active_session_key = ctx.session_key
    return True


async def _persist_thread_identity_async(agent_uuid: str, metadata: dict) -> None:
    """Fire-and-forget thread-identity metadata persist. Eventual consistency
    is fine: in-memory ctx.meta is the source of truth within this process,
    PG copy is for cross-process visibility. Errors are swallowed because
    failure here doesn't change governance correctness — next session will
    re-derive thread_id/node_index from in-memory state.

    Same shape as PR #360's `_hydrate_metadata_cache_async`: sequential
    awaits in our own loop, moved out of the critical section so the agent
    lock isn't held across a PG UPDATE roundtrip. Classification: NOT an
    anyio/asyncio coupling pattern — just lock-holding-too-long. See
 `` v0.2 RESOLUTION.
    """
    try:
        from src.db import get_db
        db = get_db()
        await db.update_identity_metadata(agent_uuid, metadata=metadata, merge=True)
        logger.info(
            f"Thread identity persisted (deferred) for {agent_uuid[:8]}... "
            f"-> thread {metadata.get('thread_id', '')[:8]}... "
            f"(node {metadata.get('node_index')})"
        )
    except Exception as e:
        logger.debug(f"Could not persist thread identity (deferred): {e}")


async def _persist_inferred_purpose_async(agent_id: str, purpose: str) -> None:
    """Fire-and-forget purpose persist. Same rationale as
    `_persist_thread_identity_async` — in-memory `meta.purpose` is the
    process-local source of truth; PG copy is for cross-process visibility.
    Failure here doesn't change governance correctness.
    """
    try:
        await agent_storage.update_agent(agent_id, purpose=purpose)
        logger.debug(f"Auto-inferred purpose '{purpose}' persisted (deferred) for {agent_id[:12]}...")
    except Exception as e:
        logger.debug(f"Could not persist inferred purpose (deferred): {e}")


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
            # Single-sourced refusal shape (council fold, PR #610) — only
            # the hint is Path-C-specific.
            return success_response(strict_identity_refusal_payload(
                "process_agent_update",
                hint=(
                    "Agent is not registered. Call onboard() first to "
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

        # Coherence from monitor history
        _mon = mcp_server.monitors.get(ctx.agent_id)
        if _mon and hasattr(_mon.state, 'coherence_history') and _mon.state.coherence_history:
            anomaly_signals["coherence"] = _mon.state.coherence_history[-1]

        # Complexity divergence from continuity metrics
        if _mon:
            _cm = getattr(_mon, '_last_continuity_metrics', None)
            if _cm and hasattr(_cm, 'complexity_divergence'):
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
    try:
        ctx.result = await mcp_server.process_update_authenticated_async(
            agent_id=ctx.agent_id,
            api_key=ctx.api_key,
            agent_state=ctx.agent_state,
            auto_save=True,
            confidence=ctx.confidence,
            session_bound=True
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

async def _r2_post_update_hook(ctx: UpdateContext) -> None:
    """R2 PR 5: post-update lineage trigger.

    For agents with confirmed lineage, increment ``chain_obs_count``
    (cheap single UPDATE — fine to ``await`` inline; matches the shape
    of other UPDATEs already awaited in ``execute_post_update_effects``).

    For agents with any lineage edge, dispatch ``evaluate_lineage_for``
    in a tracked task (anyio-safe: must NOT inline-await R1's per-dim
    DTW + audit write under the MCP handler's anyio task group).

    Cadence guard inside the FSM (``eval_cadence`` default 1h) prevents
    tight re-eval if multiple check-ins fire in the same window.

    All paths fail-soft — failures here must not break the
    ``process_agent_update`` response.
    """
    agent_id = ctx.agent_id
    # Fast-path: most agents are orphan. Skip the DB roundtrip if the
    # in-memory metadata has no parent_agent_id. The cache is set at
    # onboard (PR 3 wiring) and reliably populated for declared-lineage
    # agents in this process. The DB read remains the source of truth
    # but we avoid hitting it for the orphan-majority case.
    meta = getattr(ctx, "meta", None)
    if not (meta is not None and getattr(meta, "parent_agent_id", None)):
        return
    try:
        from src.db import get_db
        backend = get_db()
        if backend is None:
            return
        lineage = await backend.read_lineage_state(agent_id)
        if not lineage or not lineage.get("parent_agent_id"):
            return
        # Confirmed lineage: increment chain counter (await OK — single UPDATE)
        if (
            lineage.get("confirmed_at") is not None
            and not lineage.get("provisional_lineage")
        ):
            try:
                await backend.increment_chain_obs_count(agent_id)
            except Exception as e:
                # Counter miscount is recoverable — sweeper will reconcile
                # chain_obs_count on the next scheduled eval (≤6h). Keep at
                # debug to avoid log noise on transient UPDATE failures.
                logger.debug(
                    f"[R2] increment_chain_obs_count failed for "
                    f"{agent_id[:8]}...: {e}"
                )
        # Dispatch FSM eval (fire-and-forget; cadence guard inside)
        try:
            from src.background_tasks import create_tracked_task
            from src.identity.lineage_lifecycle import evaluate_lineage_for
            create_tracked_task(
                evaluate_lineage_for(agent_id),
                name=f"r2_lineage_eval_{agent_id[:8]}",
            )
        except Exception as e:
            # Dispatch failure means R2 governance is silently degraded for
            # this agent — FSM never runs for this check-in, lineage state
            # stays stale until the sweeper picks it up (up to 6h later).
            # Worth operator attention.
            logger.warning(
                f"[R2] lineage eval dispatch failed for "
                f"{agent_id[:8]}...: {e}"
            )
    except Exception as e:
        # Outer read failure means DB issue or schema drift — both warrant
        # operator attention, not silent debug.
        logger.warning(
            f"[R2] post-update lineage hook failed for "
            f"{agent_id[:8]}...: {e}"
        )


async def execute_post_update_effects(ctx: UpdateContext) -> None:
    """Health check, CIRS emissions, PG record, outcome events. All fail-safe."""
    mcp_server = ctx.mcp_server
    agent_id = ctx.agent_id

    # Heartbeat
    try:
        await ctx.loop.run_in_executor(None, mcp_server.process_mgr.write_heartbeat)
    except Exception as e:
        logger.debug(f"Heartbeat write skipped: {e}")

    # Health status
    ctx.metrics_dict = ctx.result.get('metrics', {})
    ctx.risk_score = ctx.metrics_dict.get('risk_score', None)
    ctx.coherence = ctx.metrics_dict.get('coherence', None)
    void_active = ctx.metrics_dict.get('void_active', False)

    # Record current signals into per-agent behavioral baseline (post-ODE)
    try:
        from src.agent_behavioral_baseline import get_agent_behavioral_baseline, schedule_baseline_save
        baseline = get_agent_behavioral_baseline(agent_id)
        if ctx.coherence is not None:
            baseline.update("coherence", ctx.coherence)
        # Tool usage signals
        try:
            from src.tool_usage_tracker import get_tool_usage_tracker
            _tu = get_tool_usage_tracker().get_usage_stats(
                agent_id=agent_id, window_hours=1,
            )
            _tu_total = _tu.get("total_calls", 0)
            if _tu_total > 0:
                _tu_failed = sum(
                    t.get("error_count", 0)
                    for t in _tu.get("tools", {}).values()
                )
                baseline.update("tool_error_rate", _tu_failed / _tu_total)
                baseline.update("tool_call_velocity", _tu_total / 60.0)
        except Exception:
            pass
        # Complexity divergence
        monitor = mcp_server.monitors.get(agent_id)
        if monitor:
            cm = getattr(monitor, '_last_continuity_metrics', None)
            if cm and hasattr(cm, 'complexity_divergence'):
                baseline.update("complexity_divergence", cm.complexity_divergence)
        # Fire-and-forget persist to PostgreSQL
        schedule_baseline_save(agent_id)
    except Exception as e:
        logger.debug(f"Baseline recording skipped for {agent_id}: {e}")

    # Agent profile — differentiated per-agent metrics (outside ODE)
    try:
        from src.agent_profile import get_agent_profile, save_profile_to_postgres
        profile = get_agent_profile(agent_id)
        profile.record_checkin(
            complexity=ctx.complexity,
            confidence=ctx.confidence,
            ethical_drift=ctx.ethical_drift,
            verdict=ctx.metrics_dict.get('verdict'),
        )
        # Persist every 10th update to avoid excessive writes
        if profile.total_updates % 10 == 0:
            await save_profile_to_postgres(agent_id)
    except Exception as e:
        logger.debug(f"Agent profile update skipped for {agent_id}: {e}")

    # Post-ODE: Enforce risk_target and coherence_target from dialectic conditions
    try:
        if ctx.meta and getattr(ctx.meta, 'dialectic_conditions', None):
            from ..dialectic.enforcement import enforce_post_ode_conditions
            decision = ctx.result.get('decision', {})
            escalated_decision, condition_warnings = enforce_post_ode_conditions(
                ctx.meta.dialectic_conditions, ctx.metrics_dict, decision
            )
            if escalated_decision is not decision:
                ctx.result['decision'] = escalated_decision
                ctx.result['dialectic_escalation'] = True
            ctx.warnings.extend(condition_warnings)
    except Exception as e:
        logger.debug(f"Dialectic condition enforcement skipped: {e}")

    try:
        ctx.health_status, ctx.health_message = mcp_server.health_checker.get_health_status(
            risk_score=ctx.risk_score,
            coherence=ctx.coherence,
            void_active=void_active
        )
    except Exception as e:
        logger.debug(f"Health status check failed: {e}")

    if 'metrics' not in ctx.result:
        ctx.result['metrics'] = {}
    _hs = getattr(ctx.health_status, 'value', 'unknown') if ctx.health_status else 'unknown'
    ctx.result['metrics']['health_status'] = _hs
    ctx.result['metrics']['health_message'] = ctx.health_message or ""

    if ctx.meta:
        ctx.meta.health_status = _hs

    # CIRS: Void alert
    ctx.cirs_alert = None
    try:
        from .cirs.protocol import maybe_emit_void_alert
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
        from .cirs.protocol import auto_emit_state_announce
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
        from .cirs.protocol import maybe_emit_resonance_signal
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

    # Drift: Auto-trigger dialectic review after sustained high drift
    try:
        monitor = ctx.monitor
        consecutive = getattr(monitor, '_consecutive_high_drift', 0) if monitor else 0
        if consecutive >= 3:
            # Check agent isn't already in a dialectic session
            from ..dialectic import is_agent_in_active_session
            already_in_session = await is_agent_in_active_session(agent_id)
            if not already_in_session:
                drift_vec = getattr(monitor, '_last_drift_vector', None)
                drift_desc = f"||Δη||={drift_vec.norm:.3f}" if drift_vec else "sustained high drift"
                from ..dialectic import handle_request_dialectic_review
                await handle_request_dialectic_review({
                    'agent_id': agent_id,
                    'issue_description': f'Ethical drift threshold exceeded: {drift_desc}',
                    'reason': 'Auto-triggered by sustained drift (3+ consecutive high-drift updates)',
                    'session_type': 'recovery',
                    'reviewer_mode': 'auto',
                })
                monitor._consecutive_high_drift = 0  # Reset after triggering
                logger.info(f"Drift-triggered dialectic review for {agent_id}")
    except Exception as e:
        logger.debug(f"Drift dialectic trigger skipped: {e}")

    # Snapshot the behavioral baseline so the DB row carries it (survives a
    # JSON-snapshot loss + DB-hydrate restart). Fail-open: a serialization
    # error must never break the state record. (Fleet starvation fix 2026-06-03.)
    try:
        behavioral_snapshot = (
            monitor._behavioral_state.to_dict_for_persistence()
            if monitor is not None else None
        )
    except Exception:
        behavioral_snapshot = None

    # PostgreSQL: Record EISV state
    try:
        await agent_storage.record_agent_state(
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
        )
        logger.debug(f"PostgreSQL: Recorded state for {agent_id}")
    except ValueError:
        # #425 Path D: when STRICT_IDENTITY_REQUIRED is on, do NOT auto-
        # create on the recovery path. The agent should have onboarded
        # explicitly; missing-row at this layer means something went
        # wrong upstream (orphan agent, race, or upstream auto-mint that
        # was correctly refused). Fail loud rather than silently mint.
        # Default off; gated by env flag for staged rollout.
        from src.mcp_handlers.identity_bootstrap import is_strict_identity_required
        if is_strict_identity_required():
            logger.warning(
                "[PROCESS_UPDATE] STRICT_IDENTITY_REQUIRED=true and "
                "agent %s... missing from PG at record_agent_state — "
                "skipping self-create (#425 Path D); state for this "
                "update will not be recorded.",
                (agent_id or "")[:12],
            )
            return
        logger.debug(f"Agent {agent_id} not found, creating...")
        try:
            await agent_storage.create_agent(
                agent_id=agent_id,
                api_key=ctx.api_key or "",
                status='active',
            )
            # S21-b §1: hydrate dict so require_registered_agent sees the new
            # row immediately (axiom-#3 H14). Self-healing path: this branch
            # fires when record_agent_state finds no PG row for this agent_id.
            try:
                from src.agent_metadata_persistence import register_minted_agent_in_dict
                register_minted_agent_in_dict(agent_id, status='active')
            except Exception as hyd_err:
                logger.debug(f"Phase eager hydration failed for {agent_id}: {hyd_err}")
            # S8a Phase-2: stamp default class tag on the recovery-create path.
            # Same rationale as the is_new_agent branch above; this branch
            # fires when record_agent_state hits a missing-row ValueError.
            try:
                from src.grounding.onboard_classifier import stamp_default_class_tags
                recovery_meta = mcp_server.agent_metadata.get(agent_id)
                # Resolve a name explicitly: prefer ctx.label (always set
                # at phase 1), fall back to meta.label. Without this, a
                # known resident hitting the recovery path would land as
                # ``ephemeral`` (council finding 2026-04-30 HIGH#4).
                recovery_label = (
                    getattr(ctx, "label", None)
                    or (recovery_meta.label if recovery_meta else None)
                )
                stamped = await stamp_default_class_tags(
                    agent_id, recovery_label, meta=recovery_meta
                )
                if stamped is not None:
                    logger.info(
                        f"[PROCESS_UPDATE] S8a default-stamp (recovery): "
                        f"{agent_id[:8]}... tagged {stamped} (label={recovery_label!r})"
                    )
            except Exception as stamp_err:
                # Recovery path is exactly the case where stamping matters
                # most — the agent was found missing from PG. Log at
                # warning so a silently-failed stamp here is visible.
                logger.warning(
                    f"[PROCESS_UPDATE] recovery default-stamp failed for "
                    f"{agent_id[:8]}... (agent will be misclassified until "
                    f"next stamp): {stamp_err}"
                )
            await agent_storage.record_agent_state(
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
            )
            logger.debug(f"PostgreSQL: Created agent and recorded state for {agent_id}")
        except Exception as create_error:
            logger.warning(f"PostgreSQL create+record failed: {create_error}", exc_info=True)
    except Exception as e:
        logger.warning(f"PostgreSQL record_agent_state failed: {e}", exc_info=True)

    # PostgreSQL: Save agent baseline (fire-and-forget, matches record_agent_state pattern)
    try:
        from governance_core import get_baseline_or_none
        baseline = get_baseline_or_none(agent_id)
        if baseline:
            from src.db import get_db
            db = get_db()
            if db:
                await db.save_agent_baseline(baseline.to_dict())
                logger.debug(f"PostgreSQL: Saved baseline for {agent_id[:12]}...")
    except Exception as e:
        logger.debug(f"Baseline save skipped: {e}")

    # Auto-emit outcome event
    # Use behavioral coherence (real per-agent signal) when available,
    # fall back to ODE coherence (thermostat attractor ~0.48)
    _beh = ctx.result.get('behavioral', {}).get('assessment', {}) if ctx.result else {}
    _beh_coherence = _beh.get('coherence')
    _coherence_for_outcome = _beh_coherence if _beh_coherence is not None else ctx.metrics_dict.get('coherence', 0.5)

    ctx.outcome_event_id = None
    try:
        if ctx.response_text and ctx.complexity >= 0.3:
            _rt_lower = ctx.response_text.lower()
            _completion_signals = (
                'completed', 'implemented', 'deployed', 'finished',
                'fixed', 'resolved', 'shipped', 'merged', 'built',
                'created', 'added', 'refactored', 'migrated',
            )
            if any(sig in _rt_lower for sig in _completion_signals):
                from src.db import get_db
                _db = get_db()
                if _db:
                    _summary = ctx.response_text[:500] if len(ctx.response_text) > 500 else ctx.response_text
                    ctx.outcome_event_id = await _db.record_outcome_event(
                        agent_id=agent_id,
                        outcome_type='task_completed',
                        is_bad=False,
                        outcome_score=min(1.0, _coherence_for_outcome * 1.5),
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
                            'source': 'auto_checkin',
                            'complexity': ctx.complexity,
                            'confidence': ctx.arguments.get('confidence'),
                            'summary': _summary,
                        },
                        # Inferred from keyword regex over agent's own
                        # response_text — closest match in v1 enum.
                        verification_source='agent_reported_tool_result',
                    )
                    if ctx.outcome_event_id:
                        logger.debug(f"Auto-emitted outcome event {ctx.outcome_event_id} for {agent_id}")
                        # Record calibration from auto-emitted positive outcome
                        _conf = ctx.confidence
                        if _conf is not None:
                            try:
                                from src.calibration import calibration_checker
                                _outcome_score = min(1.0, _coherence_for_outcome * 1.5)
                                calibration_checker.record_prediction(
                                    confidence=float(_conf),
                                    predicted_correct=(float(_conf) >= 0.5),
                                    actual_correct=_outcome_score,
                                )
                            except Exception as _ce:
                                logger.debug(f"Calibration from positive outcome skipped: {_ce}")
            # Auto-emit negative outcome event for failure signals
            if not ctx.outcome_event_id:
                _failure_signals = (
                    'failed', 'error', 'broken', 'reverted', 'blocked',
                    'stuck', 'crash', 'regression',
                )
                if any(sig in _rt_lower for sig in _failure_signals):
                    from src.db import get_db
                    _db = get_db()
                    if _db:
                        _summary = ctx.response_text[:500] if len(ctx.response_text) > 500 else ctx.response_text
                        _bad_score = max(0.0, 1.0 - _coherence_for_outcome * 1.5)
                        _bad_oid = await _db.record_outcome_event(
                            agent_id=agent_id,
                            outcome_type='task_failed',
                            is_bad=True,
                            outcome_score=_bad_score,
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
                                'source': 'auto_checkin',
                                'complexity': ctx.complexity,
                                'confidence': ctx.arguments.get('confidence'),
                                'summary': _summary,
                                'is_negative': True,
                            },
                            # Same regex-on-response-text inference as
                            # task_completed above; not server-observed.
                            verification_source='agent_reported_tool_result',
                        )
                        if _bad_oid:
                            logger.debug(f"Auto-emitted negative outcome event {_bad_oid} for {agent_id}")
                            _conf = ctx.confidence
                            if _conf is not None:
                                try:
                                    from src.calibration import calibration_checker
                                    calibration_checker.record_prediction(
                                        confidence=float(_conf),
                                        predicted_correct=(float(_conf) >= 0.5),
                                        actual_correct=_bad_score,
                                    )
                                except Exception as _ce:
                                    logger.debug(f"Calibration from negative outcome skipped: {_ce}")
    except Exception as e:
        logger.debug(f"Outcome event auto-emit skipped: {e}")

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
                    },
                    # Quality computed server-side from ctx.result trajectory data.
                    verification_source='server_observation',
                )
    except Exception as e:
        logger.debug(f"Trajectory validation record skipped: {e}")

    # Phase-5: iterate self-reported tool evidence. Spec §2 + §8.
    # ctx.recent_tool_results was populated in transform_inputs (sync phase).
    # Evidence arrives as plain dicts (model_dump() flattens Pydantic models).
    evidence_mode = os.environ.get("UNITARES_PHASE5_EVIDENCE_WRITE", "").lower()
    if ctx.recent_tool_results and evidence_mode in ("shadow", "1", "enable"):
        from src.mcp_handlers.observability.outcome_events import _record_outcome_event_inline
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
                await _record_outcome_event_inline({
                    "outcome_type": outcome_type,
                    "is_bad": is_bad,
                    "prediction_id": evidence.get("prediction_id"),
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

    # R2 PR 5: lineage hooks — chain_obs_count increment + evaluate_lineage_for
    # dispatch. Fail-soft inside the helper. Placed at the end so trajectory
    # row has been written and any preceding outcome events are flushed.
    await _r2_post_update_hook(ctx)
