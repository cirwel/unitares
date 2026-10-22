"""Canonical advisory consultation facade.

``consult`` chooses between the standard inference lane and the strong
operator-authorized lane while keeping advisory evidence categorically
separate from governed dialectic review.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Sequence
from uuid import uuid4

from mcp.types import TextContent

from src.logging_utils import get_logger
from src.mcp_handlers.context import (
    get_context_resolved_agent_id,
    get_session_signals,
)

from ..decorators import mcp_tool
from ..utils import error_response, require_argument, success_response
from .coerce import coerce_bool
from ..schemas.core import council_host_ids
from .delegated_inference import (
    DelegatedInferenceRequest,
    run_delegated_inference,
)
from .host_adapter import (
    host_adapter_available,
    host_adapter_disabled_hosts,
    host_adapter_lane_configured,
)
from . import host_availability
from .inference_outcome import InferenceFailure, InferenceOutcome
from .inference_registry import sha256_text
from src import inference_config
from src.local_inference_env import ENDPOINT_NOT_LOCAL
from .model_inference import (
    CallModelRequest,
    _provider_timeout_s,
    run_model_inference,
)

logger = get_logger(__name__)


CONSULTATION_SCHEMA = "unitares.consultation.v1"
_INFERENCE_SCHEMA = "unitares.inference_result.v0"
_BRIEF_MAX_CHARS = 32_000
CONSULT_TIMEOUT_S = 480.0
_CONSULT_CLEANUP_GRACE_S = 30.0

_AUTHORITY = {
    "class": "tool_evidence",
    "advisory": True,
    "on_record": False,
    "can_satisfy_peer_review": False,
    "governed_review_tool": "request_review",
}

_PURPOSE_INSTRUCTIONS = {
    "answer": "Answer the request directly. State material uncertainty.",
    "critique": (
        "Provide a critical perspective as advisory model evidence. Identify concrete "
        "risks, counterarguments, or improvements where relevant; do not claim a review verdict."
    ),
    "summarize": "Summarize the material accurately and preserve important caveats.",
    "generate": "Generate the requested material while respecting the stated constraints.",
}

_STANDARD_TASK_TYPES = {
    "answer": "reasoning",
    "critique": "analysis",
    "summarize": "analysis",
    "generate": "generation",
}

_THOROUGH_TASK_TYPES = {
    "answer": "reasoning",
    "critique": "review",
    "summarize": "summarize",
    "generate": "reasoning",
}

_THOROUGH_HOST_ROUTES = {
    "claude:host-adapter": (
        "agent_orchestrator",
        "claude:host-adapter",
        "claude_host_adapter",
        "operator_authorized_external",
    ),
    "codex:host-adapter": (
        "agent_orchestrator",
        "codex:host-adapter",
        "codex_host_adapter",
        "operator_authorized_external",
    ),
    "antigravity:host-adapter": (
        "agent_orchestrator",
        "antigravity:host-adapter",
        "antigravity_host_adapter",
        "operator_authorized_external",
    ),
}

_STANDARD_HOST_ROUTES = {
    "ollama:local": ("ollama", "ollama:local", "ollama", "local"),
    "hf:router": ("huggingface", "hf:router", "hf", "external_cloud"),
}
_SELECTABLE_HOST_ROUTES = {**_STANDARD_HOST_ROUTES, **_THOROUGH_HOST_ROUTES}

@dataclass(frozen=True, slots=True)
class ThoroughPlan:
    """The consult.thorough candidates for one caller.

    Without an operator order, only a single available peer can be selected.
    ``peers`` keeps an operator's order with only static skips removed
    (``skipped``): a host the file switches off, the caller's own family, or a
    host the thorough lane cannot call. The system may only shorten the list,
    never add to or reorder it. ``config_error`` is set when the operator's
    file does not load; the job then refuses rather than falling back.
    """

    peers: tuple[str, ...]
    skipped: tuple[dict[str, str], ...]
    config_error: str | None = None
    job_configured: bool = False
    selection_required: bool = False
    # Every host in the operator's order with its static skip, or None when it
    # is a peer; the walk that reports `skipped` follows this, in order.
    order: tuple[tuple[str, dict[str, str] | None], ...] = ()


def _skip(host_id: str, reason: str, detail: str) -> dict[str, str]:
    return {"host_id": host_id, "reason": reason, "detail": detail}


def _thorough_plan(
    caller_family: str | None, *, loaded: inference_config.LoadResult | None = None,
) -> ThoroughPlan:
    loaded = loaded if loaded is not None else inference_config.load()
    if loaded.state == "error" or loaded.config is None:
        return ThoroughPlan((), (), config_error=str(loaded.error))
    config = loaded.config
    job = config.jobs.get("consult.thorough")
    # Without an operator order, inventory order cannot choose a provider.
    order = job.prefer if job is not None else tuple(_THOROUGH_HOST_ROUTES)
    walk: list[tuple[str, dict[str, str] | None]] = []
    for host_id in order:
        entry = config.hosts.get(host_id)
        static: dict[str, str] | None = None
        if host_id not in _THOROUGH_HOST_ROUTES:
            detail = ("declared hosts are not callable from consult yet"
                      if entry is not None and entry.kind != "builtin"
                      else "not a thorough-lane host")
            static = _skip(host_id, "unconfigured", detail)
        elif entry is not None and not entry.enabled:
            static = _skip(host_id, "disabled", "switched off in the inference hosts file")
        elif caller_family is not None and entry is not None and entry.family == caller_family:
            static = _skip(host_id, "excluded_family", f"same family as the caller ({caller_family})")
        elif job is None and not host_adapter_available(host_id):
            static = _availability_skip(host_id)
        walk.append((host_id, static))
    peers = tuple(h for h, static in walk if static is None)
    return ThoroughPlan(
        peers=peers,
        skipped=tuple(static for _, static in walk if static is not None),
        job_configured=job is not None,
        # Zero peers is a missing host, not an ambiguity a selection resolves.
        selection_required=job is None and len(peers) > 1,
        order=tuple(walk),
    )


def _availability_skip(host_id: str) -> dict[str, str]:
    cooling = host_availability.cooldown(host_id)
    if cooling is not None:
        return _skip(host_id, "cooldown", "provider cooling down after a quota or auth failure")
    return _skip(host_id, "unconfigured", "not available on this server (flag, CLI or orchestrator)")

_SAFE_PROVENANCE_FIELDS = (
    "host_id",
    "provider_kind",
    "transport",
    "model_used",
    "models_used",
    "model_requested",
    "task_type",
    "privacy_class",
    "cost_class",
    "cost_usd",
    "orchestrator_execution_id",
    "orchestrator_agent_id",
    "latency_ms",
    "tokens_used",
    "energy_cost",
    "finish_reason",
    "configured_by",
    "warnings",
    "model_reporting_status",
    "model_reroutes",
)


@dataclass(frozen=True, slots=True)
class ConsultRequest:
    """Validated public policy for one advisory consultation."""

    brief: str
    requester_uuid: str | None
    purpose: str = "answer"
    effort: str = "standard"
    privacy: str = "local"
    allow_degraded: bool = False
    response_mode: str = "compact"
    host_id: str | None = None
    # Explicitly pinned, or selected from the deployment policy.
    thorough_host_id: str | None = None
    # The caller's eligible peers in preference order (never its own family);
    # a thorough attempt that finds its provider unavailable fails over along
    # this list. Empty = no failover (the pre-failover behaviour).
    thorough_peers: tuple[str, ...] = ()
    # Hops the thorough lane took, appended as they happen. Shared by every
    # dataclasses.replace copy of this request (same list object), so the
    # handler sees them on every path, cancellation included, and the audit
    # record names each host the brief reached.
    failover_trace: list[dict[str, str]] = field(default_factory=list, compare=False)
    # Hosts the thorough lane passed over, in the operator's order, before the
    # one that served (failover hops add to it): each with a closed reason code
    # (inference_config.SKIP_REASONS) and a sentence. Shared across
    # dataclasses.replace copies, like failover_trace.
    skipped: list[dict[str, str]] = field(default_factory=list, compare=False)
    # The job's walk (host, static skip or None), for failover to report hosts
    # it passes; empty for pinned hosts and direct callers.
    thorough_order: tuple[tuple[str, dict[str, str] | None], ...] = ()
    # Every host in the job was skipped: refuse, or degrade when permitted,
    # without contacting any host.
    thorough_blocked: bool = False
    thorough_selection_required: bool = False
    # A policy refusal decided before routing (code, message, recovery); the
    # router returns it so it is recorded like every other routed refusal.
    policy_refusal: tuple[str, str, str] | None = None
    consultation_id: str = field(default_factory=lambda: str(uuid4()))
    # Set on each member of a council (host_ids), so the members' audit
    # records can be read together.
    council_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConsultationFailure:
    message: str
    code: str
    category: str
    recovery: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ConsultationOutcome:
    data: dict[str, Any]
    failure: ConsultationFailure | None = None
    # Safe provenance of a completed inference; feeds the audit record even
    # when the compact response omits it.
    provenance: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


def _constructed_prompt(request: ConsultRequest) -> str:
    instruction = _PURPOSE_INSTRUCTIONS[request.purpose]
    # The brief is the caller's request, not necessarily a proposal to critique.
    # Constraints apply to answer content; host adapters still add their own
    # terminal-answer envelope outside it.
    return (
        "Follow the request in the consultation brief, including its length, format, "
        "and scope constraints for the answer content. Use the consultation purpose "
        "within those constraints; do not add unrequested sections.\n\n"
        f"Consultation purpose: {instruction}\n\nConsultation brief:\n{request.brief}"
    )


def _family_of(value: Any) -> str | None:
    text = str(value or "").strip().lower().replace("_", "-")
    if not text:
        return None
    if any(m in text for m in ("claude", "anthropic")):
        return "anthropic"
    if any(m in text for m in ("codex", "chatgpt", "openai", "gpt")):
        return "openai"
    tokens = set(re.split(r"[^a-z]+", text))
    if any(m in text for m in ("antigravity", "gemini", "gemma", "google")) or (
        tokens & {"argon", "agy"}
    ):
        return "google"
    return None


def _caller_family() -> str | None:
    """The caller's model family from descriptive transport or session data.

    The reported model and provider decide first, because a harness can host
    several families (Antigravity runs Claude and GPT-OSS models as well as
    Gemini); then the reported harness, the detected client, and the raw user
    agent (the Gemini connector announces itself only as ``Google``). When the
    transport carries only a generic HTTP client UA (e.g. Antigravity's
    ``Go-http-client/1.1``), fall back to the bound caller's onboarded
    metadata (``public_agent_id``, ``auto_label``, ``structured_id``). None of
    these is proof; they choose between already-authorized peers and never
    grant authority.
    """
    signals = get_session_signals()
    if signals is not None:
        for value in (
            signals.reported_model,
            signals.model_provider,
            signals.reported_harness_type,
            signals.client_hint,
            signals.user_agent,
        ):
            family = _family_of(value)
            if family is not None:
                return family
    resolved_uuid = get_context_resolved_agent_id()
    if resolved_uuid:
        try:
            from src.agent_metadata_model import agent_metadata

            meta = agent_metadata.get(resolved_uuid)
        except Exception:
            meta = None
        if meta is not None:
            for value in (
                getattr(meta, "public_agent_id", None),
                getattr(meta, "auto_label", None),
                getattr(meta, "structured_id", None),
            ):
                family = _family_of(value)
                if family is not None:
                    return family
    return None


def _thorough_host_for_caller() -> str | None:
    """Select only from explicit preferences or a single available peer.

    Caller family exclusion remains descriptive; it never establishes host
    authorization. The adapter's configuration and availability gates still
    apply, and no configured preference means no arbitrary vendor tie-break.
    """
    chosen, _ = _thorough_choice(_thorough_plan(_caller_family()))
    return chosen


def _thorough_peers_for_caller() -> tuple[str, ...]:
    return _thorough_plan(_caller_family()).peers


def _thorough_choice(plan: ThoroughPlan) -> tuple[str | None, tuple[dict[str, str], ...]]:
    """The first available peer, and the hosts passed over before it, in order.

    When none is available the first peer the operator has not switched off is
    returned, so the failure names a route that setup can fix (a switched-off
    host is named only when every peer is switched off); the skips are then the
    hosts before that one. With no eligible peer at all, None and every skip.
    """
    if not plan.peers or plan.selection_required:
        return None, plan.skipped
    walk = plan.order or tuple((h, None) for h in plan.peers)
    passed: list[dict[str, str]] = []
    for host_id, static in walk:
        if static is not None:
            passed.append(static)
        elif host_adapter_available(host_id):
            return host_id, tuple(passed)
        else:
            passed.append(_availability_skip(host_id))
    disabled = host_adapter_disabled_hosts()
    chosen = next((h for h in plan.peers if h not in disabled), plan.peers[0])
    index = [h for h, _ in walk].index(chosen)
    before = {h for h, _ in walk[:index]}
    return chosen, tuple(s for s in passed if s["host_id"] in before)


#: Failures that prove the chosen provider could not serve the call and that
#: nothing is left running, so trying the next peer cannot duplicate work.
_PREFLIGHT_UNAVAILABLE_CODES = frozenset({
    "INFERENCE_HOST_NOT_FOUND",
    "INFERENCE_HOST_UNREACHABLE",
    "INFERENCE_HOST_UNAVAILABLE",
})
#: A failover attempt is started only while a full thorough attempt still fits
#: inside the consult tool timeout.
_THOROUGH_ATTEMPT_S = 240


def _clock() -> float:
    return time.monotonic()


def _failover_reason(failure: InferenceFailure) -> str | None:
    """Why the next peer may be tried, or None when it must not be."""
    if failure.possibly_running:
        return None
    unavailable = failure.details.get("provider_unavailable")
    if isinstance(unavailable, dict) and unavailable.get("reason"):
        return str(unavailable["reason"])
    if not failure.execution_started and failure.code in _PREFLIGHT_UNAVAILABLE_CODES:
        return "host_unavailable"
    return None


def _note_skip(request: ConsultRequest, item: dict[str, str]) -> None:
    if all(existing["host_id"] != item["host_id"] for existing in request.skipped):
        request.skipped.append(item)


def _next_peer(request: ConsultRequest, tried: list[str]) -> str | None:
    """The next available host after the last one tried, recording what it passes."""
    walk = request.thorough_order or tuple((h, None) for h in request.thorough_peers)
    ids = [h for h, _ in walk]
    start = ids.index(tried[-1]) + 1 if tried and tried[-1] in ids else 0
    passed: list[dict[str, str]] = []
    for host_id, static in walk[start:]:
        if host_id in tried:
            continue
        if static is not None:
            passed.append(static)
        elif host_adapter_available(host_id):
            # Only hosts passed on the way to one that will be tried count;
            # a scan that finds nothing leaves `skipped` unchanged.
            for item in passed:
                _note_skip(request, item)
            return host_id
        else:
            passed.append(_availability_skip(host_id))
    return None


def _safe_provenance(
    outcome: InferenceOutcome,
    *,
    requester_uuid: str | None,
    brief_hash: str,
    constructed_prompt_hash: str,
) -> dict[str, Any]:
    inference = outcome.inference
    safe: dict[str, Any] = {}
    for key in _SAFE_PROVENANCE_FIELDS:
        value = inference.get(key)
        if value is None:
            continue
        if key == "model_reroutes":
            if not isinstance(value, (list, tuple)):
                continue
            safe[key] = [
                {
                    name: str(item[source])[:200]
                    for name, source in (
                        ("from_model", "fromModel"),
                        ("to_model", "toModel"),
                        # Not "reason": that key is reserved for literals
                        # this module writes and kept readable, while a
                        # reroute reason is backend text and must pass the
                        # shape-or-hash rule.
                        ("reroute_reason", "reason"),
                    )
                    if isinstance(item, dict) and item.get(source) is not None
                }
                for item in value[:8]
            ]
        elif key in {"models_used", "warnings"}:
            if not isinstance(value, (list, tuple)):
                continue
            max_chars = 200 if key == "models_used" else 500
            safe[key] = [str(item)[:max_chars] for item in value[:8]]
        elif isinstance(value, str):
            safe[key] = value[:500]
        elif isinstance(value, (int, float, bool)):
            safe[key] = value
    # The served host's model family from the shared vocabulary (the operator
    # file may set it); declared, not inferred from what the backend says.
    if isinstance(safe.get("host_id"), str):
        family = inference_config.host_family(safe["host_id"])
        if family is not None:
            safe["family"] = family
    # Attribution is the dispatch-resolved caller, never a backend assertion.
    safe["requester_uuid"] = requester_uuid
    safe["accountability_class"] = "tool_evidence"
    if "tokens_used" in safe:
        safe["tokens_used_note"] = (
            "Backend aggregate usage, not answer length or billed cost. Counting "
            "rules vary by provider, including input/cache tokens; 0 can mean "
            "usage was not reported."
        )
    safe["hashes"] = {
        "brief": brief_hash,
        "constructed_prompt": constructed_prompt_hash,
        # Never trust a backend assertion for the text this facade actually
        # returns. Hash the normalized typed outcome at this boundary.
        # Guarded: a postcondition failure computes provenance before the
        # response type is validated.
        "response": (
            sha256_text(outcome.response)
            if isinstance(outcome.response, str)
            else None
        ),
    }
    return safe


def _safe_failure(failure: InferenceFailure) -> dict[str, Any]:
    safe: dict[str, Any] = {
        "code": failure.code,
        "category": failure.category,
        "execution_started": failure.execution_started,
        "possibly_running": failure.possibly_running,
    }
    execution_id = failure.details.get("orchestrator_execution_id") or failure.details.get(
        "orchestrator_agent_id"
    )
    if failure.possibly_running and execution_id:
        safe["execution"] = {
            "id": str(execution_id)[:200],
            "possibly_running": True,
        }
    # Registry route facts of the attempted host, so a failed call still says
    # where the brief was sent. Never base_url. Only when the brief left: a
    # delegated failure at preflight/spawn_rejected (execution_started false)
    # proves no child ran, and naming a host there would read as the brief
    # having gone to it. Standard-lane details carry these keys only from the
    # catch around the provider request itself.
    route = {
        key: str(failure.details[key])[:200]
        for key in ("host_id", "provider_kind", "privacy_class")
        if failure.details.get(key)
    }
    sent = failure.execution_started or failure.details.get("dispatch_phase") is None
    if route and sent:
        safe["route"] = route
    if failure.details.get("reason") == "extension_not_configured":
        safe["reason"] = "extension_not_configured"
    return safe


def _failure_diagnostics(failure: InferenceFailure) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    # The facade's generic failure summary must not erase the backend's
    # specific validation reason. Like excerpts, this is caller-only text.
    if isinstance(failure.message, str):
        diagnostics["message"] = failure.message[:500]
    for key in (
        "adapter_status",
        "dispatch_phase",
        "orchestrator_execution_id",
        "orchestrator_agent_id",
        "host_id",
        "exit_status",
        "adapter_raw_excerpt_note",
        "adapter_raw_excerpt_truncated",
        "adapter_answer_region_located",
        "adapter_answer_region_note",
    ):
        value = failure.details.get(key)
        if isinstance(value, str):
            diagnostics[key] = value[:500]
        elif isinstance(value, (int, float, bool)):
            diagnostics[key] = value
    # Caller-only diagnostic evidence: never advice and never part of the
    # durable consultation record. Keep both ends, as the answer is at the
    # tail of CLI output. The service normally already bounds this to ~2 KB.
    excerpt = failure.details.get("adapter_raw_excerpt")
    if isinstance(excerpt, str) and excerpt.strip():
        if len(excerpt) > 2500:
            excerpt = excerpt[:500] + "\n... [excerpt elided] ...\n" + excerpt[-1500:]
            diagnostics["adapter_raw_excerpt_truncated"] = True
        diagnostics["adapter_raw_excerpt"] = excerpt
        diagnostics["adapter_raw_excerpt_note"] = (
            "Unvalidated host-CLI output for diagnosis only; it is not an answer."
        )
    return diagnostics


def _completion(finish_reason: Any) -> dict[str, Any]:
    normalized = str(finish_reason).strip().lower() if finish_reason else None
    if normalized in {"stop", "success", "completed", "end_turn"}:
        state = "complete"
    elif normalized in {"length", "max_tokens", "max_output_tokens"}:
        state = "truncated"
    else:
        state = "unknown"
    return {
        "state": state,
        "finish_reason": finish_reason,
        "answer_complete": state == "complete",
    }


def _base_data(request: ConsultRequest) -> dict[str, Any]:
    return {
        "schema": CONSULTATION_SCHEMA,
        "consultation_id": request.consultation_id,
        "authority": dict(_AUTHORITY),
        "request": {
            "purpose": request.purpose,
            "effort": request.effort,
            "privacy": request.privacy,
            "allow_degraded": request.allow_degraded,
            **({"host_id": request.host_id} if request.host_id is not None else {}),
        },
        "guidance": {
            "on_record_review": (
                "Use request_review when you need governed, on-record judgment."
            )
        },
    }


def _failed(
    request: ConsultRequest,
    *,
    message: str,
    code: str,
    category: str,
    recovery_action: str,
    delivery: dict[str, Any] | None = None,
    failure_details: dict[str, Any] | None = None,
    degradation: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> ConsultationOutcome:
    data = _base_data(request)
    data.update({
        "status": "failed",
        "failure": {
            "code": code,
            "category": category,
            **(failure_details or {}),
        },
    })
    if delivery is not None:
        data["delivery"] = delivery
    if degradation is not None:
        data["degradation"] = degradation
    if request.response_mode == "full" and diagnostics:
        data["diagnostics"] = diagnostics
    elif diagnostics:
        recovery_action += (
            " Future calls can request response_mode='full' for bounded failure "
            "diagnostics; it does not retrieve this call's output."
        )
    return ConsultationOutcome(
        data=data,
        failure=ConsultationFailure(
            message=message,
            code=code,
            category=category,
            recovery={
                "action": recovery_action,
                "related_tools": ["consult", "health_check"],
            },
        ),
    )


async def _run_standard(
    request: ConsultRequest,
    prompt: str,
    *,
    force_local: bool,
) -> InferenceOutcome:
    return await run_model_inference(CallModelRequest(
        prompt=prompt,
        requesting_agent_uuid=request.requester_uuid,
        host_id=request.host_id,
        provider="ollama" if force_local else "auto",
        privacy="local" if force_local else "auto",
        task_type=_STANDARD_TASK_TYPES[request.purpose],
        max_tokens=2048,
        temperature=0.7,
        timeout_s=min(
            _provider_timeout_s(),
            CONSULT_TIMEOUT_S - _CONSULT_CLEANUP_GRACE_S,
        ),
    ))


async def _guarded_inference(
    lane: str,
    invoke: Callable[[], Awaitable[InferenceOutcome]],
) -> InferenceOutcome:
    """Normalize every internal-service contract failure without swallowing cancellation."""
    try:
        outcome = await invoke()
    except Exception as exc:  # CancelledError remains a BaseException on supported Python.
        thorough = lane == "thorough"
        return InferenceOutcome.failed(
            "The internal inference service raised unexpectedly",
            code="INTERNAL_INFERENCE_CONTRACT",
            category="system_error",
            details={"lane": lane, "exception_type": type(exc).__name__},
            recovery={"action": "Inspect the inference service logs before retrying"},
            # A strong-lane exception may be a lost response after dispatch.
            execution_started=thorough,
            possibly_running=thorough,
        )
    if isinstance(outcome, InferenceOutcome):
        return outcome
    return InferenceOutcome.failed(
        "The internal inference service returned an invalid result",
        code="INTERNAL_INFERENCE_CONTRACT",
        category="system_error",
        details={"lane": lane, "reason": "invalid_outcome_type"},
        recovery={"action": "Inspect the inference service contract before retrying"},
        execution_started=True,
    )


async def _run_thorough(
    request: ConsultRequest,
    prompt: str,
) -> InferenceOutcome:
    return await run_delegated_inference(DelegatedInferenceRequest(
        prompt=prompt,
        requesting_agent_uuid=request.requester_uuid,
        host_id=request.thorough_host_id,
        task_type=_THOROUGH_TASK_TYPES[request.purpose],
        timeout_s=_THOROUGH_ATTEMPT_S,
    ))


async def _run_thorough_with_failover(
    request: ConsultRequest,
    prompt: str,
) -> tuple[ConsultRequest, InferenceOutcome]:
    """Run the thorough lane, failing over to the next eligible peer when the
    chosen provider is out (usage limit, logged out, preflight-unavailable).

    Returns the request as actually served (its thorough_host_id is the host
    that produced the outcome, so the route postcondition and the record name
    the real destination) and the final outcome. Each hop is appended to
    ``request.failover_trace`` BEFORE the next attempt starts, so a
    cancellation mid-attempt still records where the brief went.
    """
    started = _clock()
    tried = [request.thorough_host_id]
    current = request
    while True:
        outcome = await _guarded_inference(
            "thorough", lambda: _run_thorough(current, prompt)
        )
        if outcome.ok or outcome.failure is None:
            return current, outcome
        reason = _failover_reason(outcome.failure)
        elapsed = _clock() - started
        if reason is None or (
            elapsed + _THOROUGH_ATTEMPT_S > CONSULT_TIMEOUT_S - _CONSULT_CLEANUP_GRACE_S
        ):
            return current, outcome
        nxt = _next_peer(current, tried)
        if nxt is None:
            return current, outcome
        request.failover_trace.append({
            "from_host_id": current.thorough_host_id,
            "reason": reason,
            "to_host_id": nxt,
        })
        tried.append(nxt)
        current = dataclasses.replace(current, thorough_host_id=nxt)


def _delivery_postcondition_error(
    outcome: InferenceOutcome,
    delivery_policy: str,
    *,
    thorough_host_id: str,
    selected_host_id: str | None = None,
) -> tuple[str, str] | None:
    """Fail closed when a backend result violates the facade's resolved route."""
    inference = outcome.inference
    actual = (
        outcome.routed_via,
        inference.get("host_id"),
        inference.get("provider_kind"),
        inference.get("privacy_class"),
    )
    if selected_host_id is not None and actual != _SELECTABLE_HOST_ROUTES[selected_host_id]:
        return (
            "CONSULT_ROUTE_POSTCONDITION_FAILED",
            "inference resolved outside the explicitly selected host route",
        )
    standard_local = ("ollama", "ollama:local", "ollama", "local")
    standard_cloud_allowed = {
        standard_local,
        # The configured local-route endpoint when it classifies external: the
        # caller permitted external processing, and the record says external.
        ("ollama", "ollama:local", "ollama", "external"),
        ("huggingface", "hf:router", "hf", "external_cloud"),
    }
    if delivery_policy == "standard_local" and actual != standard_local:
        return (
            "CONSULT_PRIVACY_POSTCONDITION_FAILED",
            "standard local inference resolved to a non-local or unexpected route",
        )
    if (
        delivery_policy == "standard_cloud_allowed"
        and actual not in standard_cloud_allowed
    ):
        return (
            "CONSULT_ROUTE_POSTCONDITION_FAILED",
            "standard inference resolved outside the approved local-first routes",
        )
    thorough_external = _THOROUGH_HOST_ROUTES.get(thorough_host_id)
    if delivery_policy == "thorough_external" and actual != thorough_external:
        return (
            "CONSULT_ROUTE_POSTCONDITION_FAILED",
            "thorough inference resolved outside the approved delegated route",
        )
    return None


def _success(
    request: ConsultRequest,
    outcome: InferenceOutcome,
    *,
    delivered_effort: str,
    delivery_policy: str,
    degradation: dict[str, Any] | None,
    brief_hash: str,
    prompt_hash: str,
) -> ConsultationOutcome:
    if not isinstance(outcome.inference, dict) or not outcome.inference:
        return _failed(
            request,
            message="Inference completed without the required provenance record",
            code="INTERNAL_INFERENCE_CONTRACT",
            category="system_error",
            recovery_action="Retry once; if this persists, inspect the inference service logs.",
            failure_details={"reason": "missing_inference_provenance"},
            degradation=degradation,
        )
    if outcome.inference.get("schema") != _INFERENCE_SCHEMA:
        return _failed(
            request,
            message="Inference returned an unsupported provenance schema",
            code="INTERNAL_INFERENCE_CONTRACT",
            category="system_error",
            recovery_action="Inspect the inference service contract before retrying.",
            failure_details={"reason": "invalid_inference_schema"},
            degradation=degradation,
        )
    # From here on the inference has run and its provenance is well-formed,
    # so every failure below carries the route: once a result came back, the
    # record says where the brief went even when no advice is returned. An
    # upstream failure (no result) records no route block: the lane,
    # requested privacy and failure code, the attempted host's registry route
    # under failure.upstream.route (_safe_failure), any possibly-running
    # execution id, and -- for a thorough call under privacy='cloud_allowed'
    # -- request.thorough_host_id.
    provenance = _safe_provenance(
        outcome,
        requester_uuid=request.requester_uuid,
        brief_hash=brief_hash,
        constructed_prompt_hash=prompt_hash,
    )
    if outcome.inference.get("accountability_class") != "tool_evidence":
        return dataclasses.replace(_failed(
            request,
            message="Inference returned an authority class that consult cannot carry",
            code="CONSULT_AUTHORITY_POSTCONDITION_FAILED",
            category="system_error",
            recovery_action=(
                "No advisory text was returned. Inspect the inference service "
                "authority contract before retrying."
            ),
            failure_details={"reason": "non_advisory_accountability_class"},
            degradation=degradation,
        ), provenance=provenance)
    postcondition_error = _delivery_postcondition_error(
        outcome,
        delivery_policy,
        thorough_host_id=request.thorough_host_id,
        selected_host_id=request.host_id,
    )
    if postcondition_error:
        code, reason = postcondition_error
        return dataclasses.replace(_failed(
            request,
            message=(
                "Inference returned on a route that does not satisfy the "
                "consultation policy"
            ),
            code=code,
            category="system_error",
            recovery_action=(
                "No advisory text was returned. Inspect inference registry and "
                "routing drift before retrying."
            ),
            failure_details={"reason": reason},
            degradation=degradation,
        ), provenance=provenance)
    if not isinstance(outcome.response, str) or not outcome.response.strip():
        return dataclasses.replace(_failed(
            request,
            message="Inference completed without an advisory response",
            code="INTERNAL_INFERENCE_CONTRACT",
            category="system_error",
            recovery_action="Retry once; if this persists, inspect the inference service logs.",
            failure_details={"reason": "empty_advisory_response"},
            degradation=degradation,
        ), provenance=provenance)

    data = _base_data(request)
    privacy_class = provenance.get("privacy_class", "unknown")
    external_processing = privacy_class != "local"
    data.update({
        "status": "degraded" if degradation else "completed",
        "advice": outcome.response,
        "delivery": {
            "effort": delivered_effort,
            "external_processing": external_processing,
        },
        "completion": _completion(provenance.get("finish_reason")),
        "message": (
            "Advisory consultation completed with a standard local fallback."
            if degradation
            else "Advisory consultation completed."
        ),
    })
    if degradation is not None:
        data["degradation"] = degradation
    if provenance.get("family"):
        data["family"] = provenance["family"]
    if external_processing:
        data["cost"] = {
            "class": provenance.get("cost_class", "unknown"),
            "usd": provenance.get("cost_usd"),
            "warning": "This consultation used an operator-authorized external service.",
        }
    if request.response_mode == "full":
        data["diagnostics"] = {
            "route": outcome.routed_via,
            **provenance,
        }
    return ConsultationOutcome(data=data, provenance=provenance)


_THOROUGH_NOT_CONFIGURED_RECOVERY = (
    "This server does not run the agent orchestrator extension that serves "
    "effort='thorough'. Use effort='standard', or pass allow_degraded=true to "
    "accept a standard local answer."
)


def _recovery_for_upstream(
    failure: InferenceFailure, *, lane: str, selected_host_id: str | None = None,
) -> str:
    if selected_host_id is not None and not failure.possibly_running:
        return (
            "Inspect the selected host with describe_inference_host, then retry "
            "when it is ready or choose another host explicitly. No alternate "
            "host was requested."
        )
    if failure.details.get("reason") == "extension_not_configured":
        return _THOROUGH_NOT_CONFIGURED_RECOVERY
    if failure.possibly_running:
        return (
            "The thorough execution may still be running; reconcile the execution "
            "reported in failure details before starting another consultation."
        )
    if failure.code in {
        "MODEL_PROVIDER_TIMEOUT",
        "DELEGATED_INFERENCE_TIMEOUT",
        "RATE_LIMITED",
    }:
        return "Wait briefly, then retry the consultation once."
    if failure.code == ENDPOINT_NOT_LOCAL:
        return (
            "The configured model endpoint is not classified local, so a "
            "privacy='local' brief was not sent. Use privacy='cloud_allowed', or "
            "have the operator reclassify the endpoint (UNITARES_TRUSTED_NETWORKS, "
            "UNITARES_MODEL_LOCAL_HOSTS or UNITARES_MODEL_PRIVACY)."
        )
    if failure.code in {
        "INFERENCE_HOST_NOT_FOUND",
        "INFERENCE_HOST_UNREACHABLE",
        "INFERENCE_HOST_UNAVAILABLE",
    }:
        return (
            "Start or configure the requested local inference service, then retry."
            if lane == "standard"
            else "Restore the configured thorough inference route, then retry."
        )
    return "Inspect the inference service logs before retrying."


async def run_consultation(request: ConsultRequest) -> ConsultationOutcome:
    """Route and normalize one advisory consultation."""
    prompt = _constructed_prompt(request)
    brief_hash = sha256_text(request.brief)
    prompt_hash = sha256_text(prompt)

    if request.host_id is not None:
        selected_effort = "standard" if request.host_id in _STANDARD_HOST_ROUTES else "thorough"
        if request.allow_degraded or request.effort != selected_effort:
            return _failed(
                request,
                message="Explicit host selection conflicts with the consultation policy",
                code="CONSULT_POLICY_UNSATISFIED",
                category="validation_error",
                recovery_action=(
                    f"Use effort='{selected_effort}' and allow_degraded=false for "
                    "this host, or omit host_id for automatic routing."
                ),
                failure_details={"reason": "selected_host_policy_conflict"},
            )
        if request.privacy == "local" and request.host_id != "ollama:local":
            return _failed(
                request,
                message="The selected host cannot satisfy privacy='local'",
                code="CONSULT_POLICY_UNSATISFIED",
                category="validation_error",
                recovery_action=(
                    "Select ollama:local to stay local, or explicitly permit "
                    "external processing with privacy='cloud_allowed'."
                ),
                failure_details={"reason": "selected_host_requires_external_processing"},
            )
        if selected_effort == "thorough":
            request = dataclasses.replace(
                request, thorough_host_id=request.host_id, thorough_peers=(),
            )

    if request.policy_refusal is not None:
        code, message, recovery = request.policy_refusal
        return _failed(
            request,
            message=message,
            code=code,
            category="system_error" if code == "INFERENCE_CONFIG_INVALID" else "validation_error",
            recovery_action=recovery,
            failure_details={"reason": code.lower()},
        )

    if request.effort == "standard":
        outcome = await _guarded_inference(
            "standard",
            lambda: _run_standard(
                request,
                prompt,
                force_local=request.privacy == "local",
            ),
        )
        if not outcome.ok:
            failure = outcome.failure
            assert failure is not None
            return _failed(
                request,
                message="Standard advisory consultation is unavailable",
                code=failure.code,
                category=failure.category,
                recovery_action=_recovery_for_upstream(
                    failure, lane="standard", selected_host_id=request.host_id,
                ),
                failure_details={"upstream": _safe_failure(failure)},
                diagnostics={"upstream": _failure_diagnostics(failure)},
            )
        return _success(
            request,
            outcome,
            delivered_effort="standard",
            delivery_policy=(
                "standard_local"
                if request.privacy == "local"
                else "standard_cloud_allowed"
            ),
            degradation=None,
            brief_hash=brief_hash,
            prompt_hash=prompt_hash,
        )

    if request.privacy == "local":
        if not request.allow_degraded:
            return _failed(
                request,
                message="A thorough consultation cannot satisfy privacy='local'",
                code="CONSULT_POLICY_UNSATISFIED",
                category="validation_error",
                recovery_action=(
                    "Choose privacy='cloud_allowed', or permit a standard local "
                    "result with allow_degraded=true."
                    if host_adapter_lane_configured()
                    else _THOROUGH_NOT_CONFIGURED_RECOVERY
                ),
                delivery=None,
                failure_details={"reason": "thorough_route_requires_external_processing"},
            )
        degradation = {
            "occurred": True,
            "reason_code": "privacy_policy_requires_local",
            "fallback_from": "thorough",
            "fallback_to": "standard_local",
        }
        local_outcome = await _guarded_inference(
            "standard",
            lambda: _run_standard(request, prompt, force_local=True),
        )
        if not local_outcome.ok:
            fallback_failure = local_outcome.failure
            assert fallback_failure is not None
            return _failed(
                request,
                message="The permitted standard local fallback also failed",
                code="CONSULT_FALLBACK_FAILED",
                category=fallback_failure.category,
                recovery_action=_recovery_for_upstream(
                    fallback_failure, lane="standard"
                ),
                failure_details={"fallback": _safe_failure(fallback_failure)},
                degradation=degradation,
                diagnostics={
                    "fallback": _failure_diagnostics(fallback_failure),
                },
            )
        return _success(
            request,
            local_outcome,
            delivered_effort="standard",
            delivery_policy="standard_local",
            degradation=degradation,
            brief_hash=brief_hash,
            prompt_hash=prompt_hash,
        )

    if request.thorough_blocked or request.thorough_host_id is None:
        # No eligible or unambiguous host was selected. Refuse, or answer
        # locally when the caller permitted it, without contacting a peer.
        selection_required = request.thorough_selection_required or (
            request.thorough_host_id is None and not request.thorough_blocked
        )
        reason_code = "CONSULT_HOST_SELECTION_REQUIRED" if selection_required else "CONSULT_NO_ELIGIBLE_HOST"
        if not request.allow_degraded:
            return _failed(
                request,
                message=("A thorough consultation needs an explicit or unambiguous host selection"
                         if selection_required else
                         "No thorough-lane host is eligible for this caller"),
                code=reason_code,
                category="system_error",
                recovery_action=(
                    "Pass host_id from list_inference_hosts, or configure "
                    "consult.thorough.prefer in UNITARES_INFERENCE_HOSTS_FILE. "
                    "Without a preference, exactly one enabled and available peer "
                    "of another family is required. Read skipped for setup details; "
                    "allow_degraded=true permits a standard local answer."
                ),
                failure_details={"skipped": [dict(item) for item in request.skipped]},
            )
        degradation = {
            "occurred": True,
            "reason_code": reason_code,
            "fallback_from": "thorough",
            "fallback_to": "standard_local",
        }
        local_outcome = await _guarded_inference(
            "standard",
            lambda: _run_standard(request, prompt, force_local=True),
        )
        if not local_outcome.ok:
            fallback_failure = local_outcome.failure
            assert fallback_failure is not None
            return _failed(
                request,
                message="No thorough host was eligible and the standard local fallback failed",
                code="CONSULT_FALLBACK_FAILED",
                category=fallback_failure.category,
                recovery_action=_recovery_for_upstream(fallback_failure, lane="standard"),
                failure_details={"fallback": _safe_failure(fallback_failure)},
                degradation=degradation,
                diagnostics={"fallback": _failure_diagnostics(fallback_failure)},
            )
        return _success(
            request,
            local_outcome,
            delivered_effort="standard",
            delivery_policy="standard_local",
            degradation=degradation,
            brief_hash=brief_hash,
            prompt_hash=prompt_hash,
        )

    request, thorough_outcome = await _run_thorough_with_failover(request, prompt)
    if thorough_outcome.ok:
        return _success(
            request,
            thorough_outcome,
            delivered_effort="thorough",
            delivery_policy="thorough_external",
            degradation=None,
            brief_hash=brief_hash,
            prompt_hash=prompt_hash,
        )

    primary_failure = thorough_outcome.failure
    assert primary_failure is not None
    # allow_degraded is the caller opting into a local answer when the strong
    # lane cannot serve. A provider that answered "usage limit" / "logged out"
    # with nothing left running cannot serve either, exactly like a host
    # refused before spawn while it cools down: both degrade, or the same
    # outage would hard-fail on the first call and degrade on the retry.
    fallback_safe = request.allow_degraded and (
        (
            not primary_failure.execution_started
            and primary_failure.code in _PREFLIGHT_UNAVAILABLE_CODES
        )
        or (
            not primary_failure.possibly_running
            and isinstance(primary_failure.details.get("provider_unavailable"), dict)
        )
    )
    if not fallback_safe:
        return _failed(
            request,
            message="Thorough advisory consultation failed",
            code=primary_failure.code,
            category=primary_failure.category,
            recovery_action=_recovery_for_upstream(
                primary_failure, lane="thorough", selected_host_id=request.host_id,
            ),
            failure_details={"upstream": _safe_failure(primary_failure)},
            diagnostics={"upstream": _failure_diagnostics(primary_failure)},
        )

    degradation = {
        "occurred": True,
        "reason_code": primary_failure.code,
        "fallback_from": "thorough",
        "fallback_to": "standard_local",
    }
    local_outcome = await _guarded_inference(
        "standard",
        lambda: _run_standard(request, prompt, force_local=True),
    )
    if not local_outcome.ok:
        fallback_failure = local_outcome.failure
        assert fallback_failure is not None
        return _failed(
            request,
            message="The thorough route and permitted standard local fallback both failed",
            code="CONSULT_FALLBACK_FAILED",
            category=fallback_failure.category,
            recovery_action=_recovery_for_upstream(
                fallback_failure, lane="standard"
            ),
            failure_details={
                "primary": _safe_failure(primary_failure),
                "fallback": _safe_failure(fallback_failure),
            },
            degradation=degradation,
            diagnostics={
                "primary": _failure_diagnostics(primary_failure),
                "fallback": _failure_diagnostics(fallback_failure),
            },
        )
    return _success(
        request,
        local_outcome,
        delivered_effort="standard",
        delivery_policy="standard_local",
        degradation=degradation,
        brief_hash=brief_hash,
        prompt_hash=prompt_hash,
    )


def _with_failover(request: ConsultRequest, outcome: ConsultationOutcome) -> ConsultationOutcome:
    """Every outcome of a call that failed over says so: success, failure,
    degraded fallback and cancellation alike."""
    if request.failover_trace:
        outcome.data["failover"] = [dict(hop) for hop in request.failover_trace]
    if request.skipped:
        outcome.data["skipped"] = [dict(item) for item in request.skipped]
    return outcome


def _cancelled(request: ConsultRequest) -> ConsultationOutcome:
    """The outcome recorded when the caller's request is cancelled mid-route."""
    data = _base_data(request)
    data["status"] = "cancelled"
    return ConsultationOutcome(data=data)


_RECORD_SCHEMA = "unitares.consultation_record.v1"

# Route facts a record keeps. host_id, provider_kind, transport,
# privacy_class and cost_class come from the server's inference registry and
# task_type from the request; the model names and orchestrator ids are
# reported by the backend. Every string, from either source, is kept only
# when it has the shape of an identifier and is otherwise stored as a keyed
# hash. ``warnings`` and the raw ``finish_reason`` are not kept at all; the
# record carries the normalized completion state.
#
# Nothing here is hashed for *resembling the brief*. Any such rule makes the
# hashing itself an oracle (a reader sees which values were hashed and learns
# what the brief mentions) and costs the row the model it exists to name. The
# accepted limit: an identifier-shaped piece of the brief that a backend
# echoes into any backend-reported value other than a code -- a model name,
# an orchestrator execution or agent id, an upstream execution id -- is
# stored as reported. Codes are held to the narrower constant shape.
_RECORD_ROUTE_FIELDS = (
    "host_id",
    "provider_kind",
    "transport",
    "model_used",
    "models_used",
    "model_requested",
    "task_type",
    "privacy_class",
    "cost_class",
    "cost_usd",
    "orchestrator_execution_id",
    "orchestrator_agent_id",
    "latency_ms",
    "tokens_used",
    "model_reporting_status",
    "model_reroutes",
    "family",
)
_IDENTIFIER_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
# Codes and categories are snake_case constants (upstream failure codes are
# UPPER_SNAKE, this facade's own reason codes lower); holding them to that
# narrower shape keeps a hyphenated or dotted echoed token out of them.
_CODE_SHAPE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_CODE_KEYS = frozenset({"code", "reason_code", "category"})
# ``reason`` is only ever a literal this module writes (failure_details and
# the delivery postconditions), never backend text, so it is kept readable.
_SERVER_TEXT_KEYS = frozenset({"reason"})
_SERVER_TEXT_MAX = 200


def _keyed_hash(key: str, text: str) -> str:
    """HMAC-SHA256 of ``text`` under the caller-held consultation key."""
    digest = hmac.new(bytes.fromhex(key), text.encode("utf-8"), hashlib.sha256)
    return "hmac-sha256:" + digest.hexdigest()


def _record_value(value: Any, key: str) -> Any:
    """An identifier-shaped string or a number as-is; any other text as its keyed hash."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(k): _record_field(k, item, key) for k, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_record_value(item, key) for item in value]
    text = str(value)
    if _IDENTIFIER_SHAPE.fullmatch(text):
        return text
    return {"unrecorded_text": _keyed_hash(key, text)}


def _record_field(name: Any, value: Any, key: str) -> Any:
    """Apply the rule for a named field inside a recorded dict."""
    if name in _SERVER_TEXT_KEYS and isinstance(value, str):
        return value[:_SERVER_TEXT_MAX]
    if name in _CODE_KEYS:
        return _record_code(value, key)
    return _record_value(value, key)


def _record_code(value: Any, key: str) -> Any:
    """A code as-is only when it has the internal constant shape."""
    if isinstance(value, str) and _CODE_SHAPE.fullmatch(value):
        return value
    return {"unrecorded_text": _keyed_hash(key, str(value))}


def _consultation_record(
    request: ConsultRequest,
    outcome: ConsultationOutcome,
    key: str,
    *,
    delivered: bool = True,
) -> dict[str, Any]:
    """Build the durable envelope for one consultation: who, route, outcome, hashes.

    Never the brief, the constructed prompt, or the advice: those appear only
    as HMAC-SHA256 hashes under ``key``, which is returned to the caller and
    not stored. Anyone who can read audit events can see that the
    consultation happened and how it was routed, but cannot test a guessed
    brief against the row; the caller, holding the key and the text, can
    prove which exchange the row describes. ``request`` and ``completion``
    hold only server-validated values (the purpose/effort/privacy enums, a
    boolean, host ids from the accepted registry enum, the normalized
    completion state). Every other string passes ``_record_value``: it is
    kept only when it has the shape of an identifier (codes: of a
    snake_case constant), and otherwise as a keyed hash. Backend-reported
    identifiers are stored as reported; see ``_RECORD_ROUTE_FIELDS`` for
    why nothing is hashed for resembling the brief, and for the limit that
    leaves.
    """
    data = outcome.data
    record: dict[str, Any] = {
        "schema": _RECORD_SCHEMA,
        "consultation_id": request.consultation_id,
        "status": data.get("status"),
        "request": dict(data.get("request") or {}),
    }
    if request.council_id is not None:
        record["council_id"] = request.council_id
    if data.get("status") == "cancelled" or not delivered:
        # The caller never receives a cancelled call's key (nor, with
        # delivered false, this outcome's), so hashes under it could never be
        # verified; the row records only that it happened.
        record["hashes"] = None
        hashes: dict[str, Any] = {}
    else:
        hashes = {
            "scheme": "hmac-sha256",
            # handle_consult already strips; stripping here too makes the
            # caller note's recipe hold for any ConsultRequest.
            "brief": _keyed_hash(key, request.brief.strip()),
            "constructed_prompt": _keyed_hash(key, _constructed_prompt(request)),
        }
        record["hashes"] = hashes
    if (
        request.effort == "thorough"
        and request.privacy == "cloud_allowed"
        and not request.thorough_blocked
        and request.thorough_host_id is not None
        and request.policy_refusal is None
    ):
        # Only when the thorough lane can be attempted: under privacy='local'
        # the call is refused or degraded before any host is contacted, and
        # naming one would read as the brief having gone there.
        record["request"]["thorough_host_id"] = request.thorough_host_id
        if request.failover_trace:
            # Every host the brief reached, in order; the last is the one that
            # answered, failed last, or (cancelled) may still be running.
            record["request"]["thorough_host_id"] = request.failover_trace[-1]["to_host_id"]
            record["failover"] = [dict(hop) for hop in request.failover_trace]
    if request.skipped:
        record["skipped"] = [dict(item) for item in request.skipped]
    for field_name in ("delivery", "degradation", "failure"):
        if data.get(field_name) is not None:
            record[field_name] = _record_value(data[field_name], key)
    completion = data.get("completion")
    if completion is not None:
        record["completion"] = {
            "state": completion.get("state"),
            "answer_complete": completion.get("answer_complete"),
        }
    if outcome.provenance:
        record["route"] = {
            name: _record_value(outcome.provenance[name], key)
            for name in _RECORD_ROUTE_FIELDS
            if outcome.provenance.get(name) is not None
        }
    advice = data.get("advice")
    if isinstance(advice, str) and record["hashes"] is not None:
        hashes["advice"] = _keyed_hash(key, advice)
    return record


def _record_consultation(
    request: ConsultRequest,
    outcome: ConsultationOutcome,
    *,
    delivered: bool = True,
) -> None:
    """Append the consultation record and tell the caller how to verify it.

    A failed audit write never fails the consultation; the caller-facing
    ``record`` block is added only when the row was handed to the audit
    writer. ``delivered`` false records an outcome the caller will never
    receive (a council cancelled after this member finished), without hashes.
    """
    key = secrets.token_hex(32)
    try:
        from src.audit_log import audit_logger

        audit_logger.log_consultation(
            agent_id=request.requester_uuid,
            record=_consultation_record(request, outcome, key, delivered=delivered),
        )
    except Exception as exc:
        logger.warning(f"consultation audit record failed (non-fatal): {exc}")
        return
    outcome.data["record"] = {
        "event_type": "consultation",
        "consultation_id": request.consultation_id,
        "hash_scheme": "hmac-sha256",
        "hash_key": key,
        "note": (
            "The server kept keyed hashes of the brief and advice, not their "
            "text, and does not store this key. To prove which audit row "
            "describes this exchange: HMAC-SHA256 with bytes.fromhex(hash_key) "
            "as the key over the UTF-8 text; the brief is hashed after "
            "stripping leading and trailing whitespace. The row is written "
            "asynchronously, so a key is not proof the row landed."
        ),
    }


def _hosts_file_refusal(error: Any, *, council: bool = False) -> tuple[str, str, str]:
    """The thorough lane's refusal when the operator's hosts file does not load.

    A council cannot fall back to standard effort, so its recovery does not
    offer it.
    """
    return (
        "INFERENCE_CONFIG_INVALID",
        f"The inference hosts file does not load, so the thorough lane refuses: {error}",
        f"The operator fixes the file named by {inference_config.HOSTS_FILE_ENV}; "
        "the doctor's inference_hosts_file check names the line or field."
        + ("" if council else " Standard effort still works."),
    )


def _pinned_host_refusal(
    host_id: str, config: inference_config.InferenceConfig,
) -> tuple[str, str, str] | None:
    """Why the operator's hosts file refuses a pinned thorough host, or None."""
    job = config.jobs.get("consult.thorough")
    entry = config.hosts.get(host_id)
    if job is not None and host_id not in job.prefer:
        return (
            "CONSULT_HOST_NOT_IN_JOB",
            f"{host_id} is not in the consult.thorough job's list "
            f"({', '.join(job.prefer)})",
            "Pin a host the consult.thorough job lists, or omit host_id.",
        )
    if entry is not None and not entry.enabled:
        return (
            "CONSULT_HOST_DISABLED",
            f"{host_id} is switched off in the inference hosts file",
            "Pin another host, or omit host_id for automatic routing.",
        )
    return None


def _argument_refusal(request: ConsultRequest) -> ConsultationOutcome | None:
    """The refusal for an invalid call, decided before routing, or None.

    These refusals are not recorded: tool-usage counts them.
    """
    if request.requester_uuid is None:
        return _failed(
            request,
            message="A resolved caller identity is required for consultation",
            code="CONSULT_IDENTITY_REQUIRED",
            category="auth_error",
            recovery_action=(
                "Bind this transport with start_session(force_new=true), then retry."
            ),
            failure_details={"reason": "resolved_identity_missing"},
        )
    if not request.brief or len(request.brief) > _BRIEF_MAX_CHARS:
        return _failed(
            request,
            message="brief must contain 1 to 32000 non-whitespace characters",
            code="INVALID_CONSULT_BRIEF",
            category="validation_error",
            recovery_action="Provide a shorter, non-empty consultation brief.",
            failure_details={"reason": "brief_out_of_bounds"},
        )
    if request.purpose not in _PURPOSE_INSTRUCTIONS:
        return _failed(
            request,
            message="Unknown consultation purpose",
            code="INVALID_CONSULT_PURPOSE",
            category="validation_error",
            recovery_action="Use answer, critique, summarize, or generate.",
        )
    if request.effort not in {"standard", "thorough"}:
        return _failed(
            request,
            message="Unknown consultation effort",
            code="INVALID_CONSULT_EFFORT",
            category="validation_error",
            recovery_action="Use standard or thorough.",
        )
    if request.privacy not in {"local", "cloud_allowed"}:
        return _failed(
            request,
            message="Unknown consultation privacy policy",
            code="INVALID_CONSULT_PRIVACY",
            category="validation_error",
            recovery_action="Use local or cloud_allowed.",
        )
    if request.response_mode not in {"compact", "full"}:
        return _failed(
            request,
            message="Unknown consultation response mode",
            code="INVALID_CONSULT_RESPONSE_MODE",
            category="validation_error",
            recovery_action="Use compact or full.",
        )
    if request.host_id is not None and request.host_id not in _SELECTABLE_HOST_ROUTES:
        return _failed(
            request,
            message="Unknown consultation host",
            code="INFERENCE_HOST_NOT_FOUND",
            category="validation_error",
            recovery_action=(
                "Choose a host listed by list_inference_hosts that accepts consult: "
                + ", ".join(_SELECTABLE_HOST_ROUTES)
            ),
            failure_details={"valid_host_ids": list(_SELECTABLE_HOST_ROUTES)},
        )
    return None


_COUNCIL_SCHEMA = "unitares.consultation_council.v1"
_COUNCIL_SIZES = (2, 3)


def _council_refusal(
    template: ConsultRequest, hosts: Any, host_id: Any,
) -> tuple[str, str, str, dict[str, Any]] | None:
    """Why a council call is refused before any host is contacted, or None:
    (code, message, recovery, failure details)."""
    if host_id is not None:
        return (
            "CONSULT_POLICY_UNSATISFIED",
            "host_id and host_ids cannot be combined",
            "Name every council member in host_ids, or pin one host with host_id.",
            {"reason": "host_id_with_host_ids"},
        )
    if (
        not isinstance(hosts, (list, tuple))
        or not all(isinstance(h, str) for h in hosts)
        or len(set(hosts)) != len(hosts)
        or len(hosts) not in _COUNCIL_SIZES
    ):
        return (
            "INVALID_CONSULT_COUNCIL",
            "host_ids takes 2 or 3 distinct host ids",
            "Name 2 or 3 different hosts: " + ", ".join(_THOROUGH_HOST_ROUTES),
            {"reason": "council_size", "valid_host_ids": list(_THOROUGH_HOST_ROUTES)},
        )
    unknown = [h for h in hosts if h not in _THOROUGH_HOST_ROUTES]
    if unknown:
        return (
            "INFERENCE_HOST_NOT_FOUND",
            "A council member is not a thorough-lane host",
            "A council asks CLI hosts only: " + ", ".join(_THOROUGH_HOST_ROUTES),
            {"reason": "council_host_not_thorough", "valid_host_ids": list(_THOROUGH_HOST_ROUTES)},
        )
    if (
        template.effort != "thorough"
        or template.privacy != "cloud_allowed"
        or template.allow_degraded
    ):
        return (
            "CONSULT_POLICY_UNSATISFIED",
            "A council needs effort='thorough', privacy='cloud_allowed' and "
            "allow_degraded=false",
            "Pass privacy='cloud_allowed'; omit effort and allow_degraded.",
            {"reason": "council_policy_conflict"},
        )
    return None


async def _council_member(request: ConsultRequest) -> ConsultationOutcome:
    """Run one council member; a member that raises fails alone. The caller
    records it (see _run_council)."""
    try:
        outcome = await run_consultation(request)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(f"council member {request.host_id} raised: {type(exc).__name__}")
        outcome = _failed(
            request,
            message="The council member raised unexpectedly",
            code="INTERNAL_INFERENCE_CONTRACT",
            category="system_error",
            recovery_action="Inspect the server logs before retrying this host.",
            failure_details={"reason": "council_member_raised"},
        )
    return _with_failover(request, outcome)


async def _run_council(members: list[ConsultRequest]) -> list[ConsultationOutcome]:
    """Run every member in parallel, then record each once.

    Records are written after the whole council returns, with no await in
    between, so a completed member's hashes are written in the same step
    that hands its hash key back toward the caller (a transport that drops
    the response afterwards loses it, as for a single consult). When the
    council is cancelled, nothing reaches the caller, so no member's row
    keeps hashes: a member that had answered, or was still running, is
    recorded as cancelled; a member already refused or failed keeps that
    outcome, without hashes.
    """
    finished: dict[int, ConsultationOutcome] = {}

    async def run(index: int, member: ConsultRequest) -> None:
        finished[index] = await _council_member(member)

    try:
        await asyncio.gather(*(run(i, member) for i, member in enumerate(members)))
    except asyncio.CancelledError:
        for i, member in enumerate(members):
            outcome = finished.get(i)
            if outcome is None or outcome.ok:
                outcome = _with_failover(member, _cancelled(member))
            _record_consultation(member, outcome, delivered=False)
        raise
    outcomes = [finished[i] for i in range(len(members))]
    for member, outcome in zip(members, outcomes):
        _record_consultation(member, outcome)
    return outcomes


def _council_member_entry(
    request: ConsultRequest, outcome: ConsultationOutcome,
) -> dict[str, Any]:
    entry = {
        key: value for key, value in outcome.data.items()
        if key not in {"schema", "authority", "guidance"}
    }
    entry["host_id"] = request.host_id
    if outcome.failure is not None:
        entry["message"] = outcome.failure.message
        entry["recovery"] = outcome.failure.recovery
    return entry


async def _handle_council(
    arguments: Dict[str, Any], brief_text: str, requester_uuid: str | None,
) -> Sequence[TextContent]:
    """Ask each host in host_ids the same brief, in parallel.

    Each member is an ordinary pinned consultation: the same route checks,
    its own consultation id, record and hash key, and no failover. The
    council only runs them together and returns them side by side; it does
    not combine, rank or vote on the answers.
    """
    council_id = str(uuid4())
    hosts = council_host_ids(arguments.get("host_ids"))
    template = ConsultRequest(
        brief=brief_text,
        requester_uuid=requester_uuid,
        purpose=str(arguments.get("purpose", "answer")),
        effort=str(arguments.get("effort", "thorough")),
        privacy=str(arguments.get("privacy", "local")),
        allow_degraded=coerce_bool(arguments.get("allow_degraded"), default=False),
        response_mode=str(arguments.get("response_mode", "compact")),
        council_id=council_id,
    )
    data: dict[str, Any] = {
        "schema": _COUNCIL_SCHEMA,
        "council_id": council_id,
        "authority": dict(_AUTHORITY),
        "request": {
            "purpose": template.purpose,
            "effort": template.effort,
            "privacy": template.privacy,
            "host_ids": list(hosts) if isinstance(hosts, (list, tuple)) else hosts,
        },
        "guidance": {
            "on_record_review": (
                "Use request_review when you need governed, on-record judgment."
            ),
            "council": (
                "Each member answered on its own. Nothing combined or ranked the "
                "answers; agreement between models is not a verdict."
            ),
        },
    }

    refused = _argument_refusal(template)
    if refused is not None:
        failure = refused.failure
        assert failure is not None
        data.update(status="failed", failure=refused.data["failure"])
        return [error_response(
            failure.message,
            error_code=failure.code,
            error_category=failure.category,
            details=data,
            recovery=failure.recovery,
            arguments=None if failure.code == "CONSULT_IDENTITY_REQUIRED" else arguments,
        )]
    council_refusal = _council_refusal(template, hosts, arguments.get("host_id"))
    if council_refusal is not None:
        code, message, recovery, details = council_refusal
        data.update(status="failed", failure={"code": code, "category": "validation_error", **details})
        return [error_response(
            message,
            error_code=code,
            error_category="validation_error",
            details=data,
            recovery={"action": recovery, "related_tools": ["consult", "list_inference_hosts"]},
            arguments=arguments,
        )]

    loaded = inference_config.load()
    members = []
    for host_id in hosts:
        if loaded.state == "error" or loaded.config is None:
            refusal: tuple[str, str, str] | None = _hosts_file_refusal(
                loaded.error, council=True,
            )
        else:
            refusal = _pinned_host_refusal(host_id, loaded.config)
        members.append(ConsultRequest(
            brief=template.brief,
            requester_uuid=template.requester_uuid,
            purpose=template.purpose,
            effort=template.effort,
            privacy=template.privacy,
            response_mode=template.response_mode,
            host_id=host_id,
            thorough_host_id=host_id,
            policy_refusal=refusal,
            council_id=council_id,
        ))

    outcomes = await _run_council(members)
    answered = sum(1 for outcome in outcomes if outcome.ok)
    data.update({
        "status": (
            "completed" if answered == len(members)
            else "partial" if answered
            else "failed"
        ),
        "answered": answered,
        "asked": len(members),
        "members": [
            _council_member_entry(member, outcome)
            for member, outcome in zip(members, outcomes)
        ],
        "message": f"{answered} of {len(members)} council members answered.",
    })
    if not answered:
        # Caller-correctable only when every member was refused as invalid
        # (a host the operator's job excludes or switched off).
        categories = {outcome.failure.category for outcome in outcomes if outcome.failure}
        return [error_response(
            "No council member answered",
            error_code="CONSULT_COUNCIL_FAILED",
            error_category=(
                "validation_error" if categories == {"validation_error"} else "system_error"
            ),
            details=data,
            recovery={
                "action": "Read each member's failure and recovery; retry the hosts that can serve.",
                "related_tools": ["consult", "list_inference_hosts"],
            },
            arguments=arguments,
        )]
    return success_response(data, agent_id=requester_uuid, arguments=arguments)


@mcp_tool("consult", timeout=CONSULT_TIMEOUT_S)
async def handle_consult(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Ask for advisory model help without creating a governed review record."""
    brief, error = require_argument(arguments, "brief")
    if error:
        return [error]

    brief_text = str(brief).strip()
    # Attribution is proof-bearing dispatch context, never caller text. REST
    # historically accepts non-UUID agent_id values as legacy references; that
    # compatibility must not turn an unbound value into inference authority.
    requester_uuid = get_context_resolved_agent_id()
    if arguments.get("host_ids") is not None:
        return await _handle_council(arguments, brief_text, requester_uuid)
    selected_host = (
        str(arguments["host_id"]) if arguments.get("host_id") is not None else None
    )
    # Middleware materializes the same conditional schema default. Direct
    # handler callers need it too; a supplied value is never overwritten.
    effort = str(arguments.get(
        "effort", "thorough" if selected_host in _THOROUGH_HOST_ROUTES else "standard",
    ))
    privacy = str(arguments.get("privacy", "local"))
    thorough_fields: dict[str, Any] = {}
    # The consult.thorough job applies only where the thorough lane can run:
    # privacy='local' never contacts a thorough host, so it neither reports
    # skips nor refuses on the operator's hosts file.
    policy_refusal: tuple[str, str, str] | None = None
    if effort == "thorough" and privacy == "cloud_allowed":
        loaded = inference_config.load()
        if loaded.state == "error" or loaded.config is None:
            policy_refusal = _hosts_file_refusal(loaded.error)
        elif selected_host is None:
            plan = _thorough_plan(_caller_family(), loaded=loaded)
            chosen, skipped = _thorough_choice(plan)
            thorough_fields.update(
                thorough_host_id=chosen,
                thorough_peers=plan.peers,
                thorough_order=plan.order,
                thorough_blocked=not plan.peers or plan.selection_required,
                thorough_selection_required=plan.selection_required,
                # No selection means no host is contacted; keep every skip.
                skipped=[dict(item) for item in skipped
                         if not plan.peers or item["host_id"] != chosen],
            )
        elif selected_host in _THOROUGH_HOST_ROUTES:
            policy_refusal = _pinned_host_refusal(selected_host, loaded.config)
    request = ConsultRequest(
        brief=brief_text,
        requester_uuid=requester_uuid,
        purpose=str(arguments.get("purpose", "answer")),
        effort=effort,
        privacy=privacy,
        allow_degraded=coerce_bool(arguments.get("allow_degraded"), default=False),
        response_mode=str(arguments.get("response_mode", "compact")),
        host_id=selected_host,
        # Only a thorough consult delegates; a standard one never probes hosts.
        **thorough_fields,
        policy_refusal=policy_refusal,
        **(
            {"thorough_host_id": selected_host}
            if selected_host in _THOROUGH_HOST_ROUTES
            else {}
        ),
    )
    outcome = _argument_refusal(request)
    if outcome is None:
        try:
            outcome = await run_consultation(request)
        except asyncio.CancelledError:
            # A thorough call may already be running on an external host;
            # the record must show the consultation was started.
            _record_consultation(request, _with_failover(request, _cancelled(request)))
            raise
        outcome = _with_failover(request, outcome)
        # Argument-validation refusals are not recorded (tool-usage counts
        # them); every call that reaches the router is, policy refusals
        # included.
        _record_consultation(request, outcome)

    if not outcome.ok:
        failure = outcome.failure
        assert failure is not None
        return [error_response(
            failure.message,
            error_code=failure.code,
            error_category=failure.category,
            details=outcome.data,
            recovery=failure.recovery,
            arguments=(
                None
                if failure.code == "CONSULT_IDENTITY_REQUIRED"
                else arguments
            ),
        )]
    return success_response(
        outcome.data,
        agent_id=requester_uuid,
        arguments=arguments,
    )
