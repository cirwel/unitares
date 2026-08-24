"""Canonical advisory consultation facade.

``consult`` chooses between the standard inference lane and the strong
operator-authorized lane while keeping advisory evidence categorically
separate from governed dialectic review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Sequence
from uuid import uuid4

from mcp.types import TextContent

from src.mcp_handlers.context import get_context_resolved_agent_id

from ..decorators import mcp_tool
from ..utils import error_response, require_argument, success_response
from .coerce import coerce_bool
from .delegated_inference import (
    DelegatedInferenceRequest,
    run_delegated_inference,
)
from .inference_outcome import InferenceFailure, InferenceOutcome
from .inference_registry import sha256_text
from .model_inference import (
    CallModelRequest,
    _provider_timeout_s,
    run_model_inference,
)


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
        "Critique the proposal as advisory model evidence. Identify concrete "
        "risks, counterarguments, and improvements; do not claim a review verdict."
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
    "orchestrator_agent_id",
    "latency_ms",
    "tokens_used",
    "energy_cost",
    "finish_reason",
    "configured_by",
    "warnings",
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
    consultation_id: str = field(default_factory=lambda: str(uuid4()))


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

    @property
    def ok(self) -> bool:
        return self.failure is None


def _constructed_prompt(request: ConsultRequest) -> str:
    instruction = _PURPOSE_INSTRUCTIONS[request.purpose]
    return f"{instruction}\n\nConsultation brief:\n{request.brief}"


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
        if key in {"models_used", "warnings"}:
            if not isinstance(value, (list, tuple)):
                continue
            max_chars = 200 if key == "models_used" else 500
            safe[key] = [str(item)[:max_chars] for item in value[:8]]
        elif isinstance(value, str):
            safe[key] = value[:500]
        elif isinstance(value, (int, float, bool)):
            safe[key] = value
    # Attribution is the dispatch-resolved caller, never a backend assertion.
    safe["requester_uuid"] = requester_uuid
    safe["accountability_class"] = "tool_evidence"
    safe["hashes"] = {
        "brief": brief_hash,
        "constructed_prompt": constructed_prompt_hash,
        # Never trust a backend assertion for the text this facade actually
        # returns. Hash the normalized typed outcome at this boundary.
        "response": sha256_text(outcome.response),
    }
    return safe


def _safe_failure(failure: InferenceFailure) -> dict[str, Any]:
    safe: dict[str, Any] = {
        "code": failure.code,
        "category": failure.category,
        "execution_started": failure.execution_started,
        "possibly_running": failure.possibly_running,
    }
    execution_id = failure.details.get("orchestrator_agent_id")
    if failure.possibly_running and execution_id:
        safe["execution"] = {
            "id": str(execution_id)[:200],
            "possibly_running": True,
        }
    return safe


def _failure_diagnostics(failure: InferenceFailure) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    for key in (
        "adapter_status",
        "dispatch_phase",
        "orchestrator_agent_id",
        "host_id",
        "exit_status",
    ):
        value = failure.details.get(key)
        if isinstance(value, str):
            diagnostics[key] = value[:500]
        elif isinstance(value, (int, float, bool)):
            diagnostics[key] = value
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
        task_type=_THOROUGH_TASK_TYPES[request.purpose],
        timeout_s=240,
    ))


def _delivery_postcondition_error(
    outcome: InferenceOutcome,
    delivery_policy: str,
) -> tuple[str, str] | None:
    """Fail closed when a backend result violates the facade's resolved route."""
    inference = outcome.inference
    actual = (
        outcome.routed_via,
        inference.get("host_id"),
        inference.get("provider_kind"),
        inference.get("privacy_class"),
    )
    standard_local = ("ollama", "ollama:local", "ollama", "local")
    standard_cloud_allowed = {
        standard_local,
        ("huggingface", "hf:router", "hf", "external_cloud"),
    }
    thorough_external = (
        "agent_orchestrator",
        "claude:host-adapter",
        "claude_host_adapter",
        "operator_authorized_external",
    )
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
    if outcome.inference.get("accountability_class") != "tool_evidence":
        return _failed(
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
        )
    postcondition_error = _delivery_postcondition_error(outcome, delivery_policy)
    if postcondition_error:
        code, reason = postcondition_error
        return _failed(
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
        )
    if not isinstance(outcome.response, str) or not outcome.response.strip():
        return _failed(
            request,
            message="Inference completed without an advisory response",
            code="INTERNAL_INFERENCE_CONTRACT",
            category="system_error",
            recovery_action="Retry once; if this persists, inspect the inference service logs.",
            failure_details={"reason": "empty_advisory_response"},
            degradation=degradation,
        )

    provenance = _safe_provenance(
        outcome,
        requester_uuid=request.requester_uuid,
        brief_hash=brief_hash,
        constructed_prompt_hash=prompt_hash,
    )
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
    return ConsultationOutcome(data=data)


def _recovery_for_upstream(failure: InferenceFailure, *, lane: str) -> str:
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
                recovery_action=_recovery_for_upstream(failure, lane="standard"),
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

    thorough_outcome = await _guarded_inference(
        "thorough",
        lambda: _run_thorough(request, prompt),
    )
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
    fallback_safe = (
        request.allow_degraded
        and not primary_failure.execution_started
        and primary_failure.code in {
            "INFERENCE_HOST_NOT_FOUND",
            "INFERENCE_HOST_UNREACHABLE",
            "INFERENCE_HOST_UNAVAILABLE",
        }
    )
    if not fallback_safe:
        return _failed(
            request,
            message="Thorough advisory consultation failed",
            code=primary_failure.code,
            category=primary_failure.category,
            recovery_action=_recovery_for_upstream(
                primary_failure, lane="thorough"
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
    request = ConsultRequest(
        brief=brief_text,
        requester_uuid=requester_uuid,
        purpose=str(arguments.get("purpose", "answer")),
        effort=str(arguments.get("effort", "standard")),
        privacy=str(arguments.get("privacy", "local")),
        allow_degraded=coerce_bool(arguments.get("allow_degraded"), default=False),
        response_mode=str(arguments.get("response_mode", "compact")),
    )
    if requester_uuid is None:
        outcome = _failed(
            request,
            message="A resolved caller identity is required for consultation",
            code="CONSULT_IDENTITY_REQUIRED",
            category="auth_error",
            recovery_action=(
                "Bind this transport with start_session(force_new=true), then retry."
            ),
            failure_details={"reason": "resolved_identity_missing"},
        )
    elif not brief_text or len(brief_text) > _BRIEF_MAX_CHARS:
        outcome = _failed(
            request,
            message="brief must contain 1 to 32000 non-whitespace characters",
            code="INVALID_CONSULT_BRIEF",
            category="validation_error",
            recovery_action="Provide a shorter, non-empty consultation brief.",
            failure_details={"reason": "brief_out_of_bounds"},
        )
    elif request.purpose not in _PURPOSE_INSTRUCTIONS:
        outcome = _failed(
            request,
            message="Unknown consultation purpose",
            code="INVALID_CONSULT_PURPOSE",
            category="validation_error",
            recovery_action="Use answer, critique, summarize, or generate.",
        )
    elif request.effort not in {"standard", "thorough"}:
        outcome = _failed(
            request,
            message="Unknown consultation effort",
            code="INVALID_CONSULT_EFFORT",
            category="validation_error",
            recovery_action="Use standard or thorough.",
        )
    elif request.privacy not in {"local", "cloud_allowed"}:
        outcome = _failed(
            request,
            message="Unknown consultation privacy policy",
            code="INVALID_CONSULT_PRIVACY",
            category="validation_error",
            recovery_action="Use local or cloud_allowed.",
        )
    elif request.response_mode not in {"compact", "full"}:
        outcome = _failed(
            request,
            message="Unknown consultation response mode",
            code="INVALID_CONSULT_RESPONSE_MODE",
            category="validation_error",
            recovery_action="Use compact or full.",
        )
    else:
        outcome = await run_consultation(request)

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
