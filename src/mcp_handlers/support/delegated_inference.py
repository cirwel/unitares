"""Long-running strong-model delegation through operator-authorized host CLIs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Sequence

from mcp.types import TextContent

from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server

from ..context import get_context_resolved_agent_id
from ..decorators import mcp_tool
from ..utils import error_response, require_argument, success_response
from .host_adapter import (
    codex_answer_region_located,
    host_cli_env_var,
    invoke_host_adapter,
)
from .inference_registry import get_inference_host, sha256_text as _sha256_text
from .inference_outcome import InferenceOutcome

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DelegatedInferenceRequest:
    """Validated input to the strong advisory inference service."""

    prompt: str
    requesting_agent_uuid: str | None
    host_id: str = "claude:host-adapter"
    model: str | None = None
    task_type: str = "reasoning"
    timeout_s: int = 240


async def _track_energy(
    agent_uuid: str | None,
    *,
    host_id: str,
    models_used: list[str],
    tokens_used: int,
    energy_cost: float,
) -> None:
    """Best-effort EISV accounting; inference still succeeds if tracking fails."""
    if not agent_uuid:
        return
    try:
        monitor = mcp_server.get_or_create_monitor(agent_uuid)
        from src.agent_monitor_state import ensure_hydrated

        await ensure_hydrated(monitor, agent_uuid)
        model_text = ",".join(models_used) if models_used else "provider_unreported"
        monitor.process_update({
            "response_text": (
                f"Delegated inference via {host_id}: models={model_text} "
                f"tokens={tokens_used}"
            ),
            "complexity": min(0.1 + energy_cost * 2, 0.3),
            "confidence": 0.8,
        })
    except Exception as exc:  # noqa: BLE001 - accounting is intentionally non-fatal
        logger.warning("Could not track delegated-inference Energy: %s", exc)


# Truncation budget for the diagnostic excerpt below. Large enough to carry a
# fence, a preamble, and the start of a JSON object; small enough that a runaway
# CLI cannot flood an error payload.
_RAW_EXCERPT_LIMIT = 2000
# When the budget binds, keep both ends. A head-only excerpt is structurally
# blind to the one region a terminal-answer failure is ever about: a CLI
# transcript opens with a banner and an echo of the prompt, and the answer — or
# its absence — is at the tail. Spending the whole budget on the head reliably
# shows the least informative bytes in the file.
_RAW_EXCERPT_HEAD = 500
_RAW_EXCERPT_TAIL = _RAW_EXCERPT_LIMIT - _RAW_EXCERPT_HEAD


def _raw_excerpt_details(adapter_result: Dict[str, Any]) -> Dict[str, Any]:
    """Diagnostic excerpt of the CLI's own output, for envelope failures only.

    `_validate_terminal_answer` fails closed on anything that is not exactly the
    contracted JSON object, which is the right posture: guessing what a model
    meant is how a plan gets accepted as an answer. But failing closed without
    saying what arrived leaves the caller unable to tell a fenced-but-valid
    payload from a refusal from a crash, and the raw text the adapter already
    captured is thrown away one layer up.

    Reported 2026-08-29: a codex:host-adapter call returned exit_status 0 after
    ~40s of real work, and surfaced only "malformed terminal answer envelope" —
    enough to know the lane is broken, not enough to fix it.

    This adds diagnosis, never tolerance: the call still fails, the status is
    still `malformed`, and nothing here is parsed or acted upon. The excerpt is
    unvalidated model-authored text and is labelled as such.
    """
    if adapter_result.get("status") == "complete":
        return {}
    raw = adapter_result.get("raw")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    full = raw.strip()
    truncated = len(full) > _RAW_EXCERPT_LIMIT
    if truncated:
        elided = len(full) - _RAW_EXCERPT_LIMIT
        excerpt = (
            full[:_RAW_EXCERPT_HEAD]
            + f"\n... [{elided} characters elided] ...\n"
            + full[-_RAW_EXCERPT_TAIL:]
        )
    else:
        excerpt = full
    details: Dict[str, Any] = {
        "adapter_raw_excerpt": excerpt,
        "adapter_raw_excerpt_truncated": truncated,
        "adapter_raw_excerpt_note": (
            "Unvalidated host-CLI output, included so an envelope failure is "
            "diagnosable. It was NOT parsed and is not an answer."
            + (" Head and tail of the output; the middle is elided." if truncated else "")
        ),
    }
    provenance = adapter_result.get("provenance")
    family = provenance.get("model_family") if isinstance(provenance, dict) else None
    if family == "openai_codex":
        located = codex_answer_region_located(full)
        details["adapter_answer_region_located"] = located
        if not located:
            details["adapter_answer_region_note"] = (
                "No 'codex' marker line in the transcript, so no answer region "
                "was isolated and the whole transcript reached the envelope "
                "validator. This is NOT the model returning a bad envelope — it "
                "is the adapter never finding an assistant turn. Check whether "
                "the CLI produced one at all before changing the envelope contract."
            )
    return details


async def run_delegated_inference(
    request: DelegatedInferenceRequest,
) -> InferenceOutcome:
    """Run one strong advisory inference without an MCP response envelope."""
    host_id = request.host_id
    # Registry construction includes a cached blocking Ollama probe even when
    # looking up Claude. Keep that probe off the MCP event loop.
    try:
        host = await asyncio.to_thread(get_inference_host, host_id)
    except Exception as exc:  # noqa: BLE001 - registry failure is pre-execution
        logger.warning("Could not resolve inference host %s: %s", host_id, exc)
        return InferenceOutcome.failed(
            f"Inference host '{host_id}' could not be resolved",
            code="INFERENCE_HOST_UNAVAILABLE",
            category="system_error",
            recovery={
                "action": "Check the inference host registry and retry",
                "related_tools": ["list_inference_hosts", "health_check"],
            },
        )
    if host is None:
        return InferenceOutcome.failed(
            f"Inference host '{host_id}' is not registered",
            code="INFERENCE_HOST_NOT_FOUND",
            category="validation_error",
            recovery={
                "action": "Call list_inference_hosts to see registered hosts",
                "related_tools": ["list_inference_hosts"],
            },
        )

    if "delegate_inference" not in (host.get("accepts_host_id_from") or []):
        return InferenceOutcome.failed(
            f"Inference host '{host_id}' is not reachable from delegate_inference",
            code="INFERENCE_HOST_UNREACHABLE",
            category="validation_error",
            details={
                "host": host,
                "accepts_host_id_from": host.get("accepts_host_id_from") or [],
            },
            recovery={
                "action": (
                    "Choose a host whose accepts_host_id_from includes "
                    "delegate_inference"
                ),
                "related_tools": ["list_inference_hosts"],
            },
        )

    if not host.get("configured") or not host.get("available"):
        return InferenceOutcome.failed(
            f"Inference host '{host_id}' is not available",
            code="INFERENCE_HOST_UNAVAILABLE",
            category="system_error",
            details={"host": host},
            recovery={
                "action": (
                    "Set UNITARES_HOST_ADAPTER_ENABLED=1, configure the "
                    "orchestrator bearer, and install or point "
                    f"{host_cli_env_var(host_id) or 'the host CLI variable'} "
                    "at an authenticated CLI for this host"
                ),
                "related_tools": ["describe_inference_host", "health_check"],
            },
        )

    adapter_result = await invoke_host_adapter(
        host_id,
        request.prompt,
        timeout_s=request.timeout_s,
        sandbox="read-only",
        model=request.model,
    )
    adapter_provenance = adapter_result.get("provenance") or {}
    terminal_answer = adapter_provenance.get("terminal_answer")
    validated_complete = (
        isinstance(terminal_answer, dict)
        and terminal_answer.get("schema") == "unitares.terminal_answer.v1"
        and terminal_answer.get("status") == "complete"
        and isinstance(adapter_result.get("text"), str)
        and bool(adapter_result["text"].strip())
    )
    if adapter_result.get("ok") and not validated_complete:
        # Defense in depth: a mock, stale adapter, or future transport must not
        # bypass the semantic completion contract merely by returning ok=true.
        adapter_result = {
            **adapter_result,
            "ok": False,
            "status": "malformed",
            "dispatch_phase": "terminal",
            "error": "Host adapter omitted a validated terminal answer envelope",
        }
    if not adapter_result.get("ok"):
        still_running = adapter_result.get("status") == "still_running"
        orchestrator_execution_id = (
            adapter_result.get("execution_id") or adapter_result.get("agent_id")
        )
        dispatch_phase = str(adapter_result.get("dispatch_phase") or "unknown")
        # A preflight failure or explicit HTTP rejection proves no child was
        # accepted. Every other phase is conservative: a spawn response can be
        # lost after the orchestrator accepted it, even when no id reached us.
        fallback_safe = dispatch_phase in {"preflight", "spawn_rejected"}
        execution_started = not fallback_safe
        terminal_result = dispatch_phase == "terminal" and not still_running
        possibly_running = execution_started and not terminal_result
        message = (
            f"Delegated inference exceeded its {request.timeout_s}s await window"
            if still_running
            else str(adapter_result.get("error") or "Host adapter returned a nonzero exit")
        )
        return InferenceOutcome.failed(
            message,
            code=(
                "DELEGATED_INFERENCE_TIMEOUT"
                if still_running
                else (
                    "DELEGATED_INFERENCE_FAILED"
                    if not fallback_safe
                    else "INFERENCE_HOST_UNAVAILABLE"
                )
            ),
            category="system_error",
            details={
                "host_id": host_id,
                "adapter_status": adapter_result.get("status"),
                "dispatch_phase": dispatch_phase,
                "orchestrator_execution_id": orchestrator_execution_id,
                "orchestrator_agent_id": orchestrator_execution_id,
                "exit_status": adapter_result.get("exit_status"),
                "inference_provenance": adapter_provenance,
                "execution_started": execution_started,
                "possibly_running": possibly_running,
                **_raw_excerpt_details(adapter_result),
            },
            recovery={
                "action": (
                    "Do not retry yet. The delegated execution may still be running; "
                    "ask the operator to reconcile or terminate it using the "
                    "orchestrator controls."
                    if possibly_running
                    else "Check the host adapter configuration and orchestrator logs"
                ),
                "related_tools": ["describe_inference_host", "health_check"],
            },
            execution_started=execution_started,
            possibly_running=possibly_running,
        )

    response_text = str(adapter_result.get("text") or "")
    models_used = [
        str(value) for value in (adapter_provenance.get("models_used") or [])
    ]
    tokens_used = int(adapter_provenance.get("tokens_used") or 0)
    # Strong external consultation gets a deliberately modest, fixed accounting
    # increment. Dollar cost remains provider-reported metadata, not an EISV proxy.
    energy_cost = 0.05
    await _track_energy(
        request.requesting_agent_uuid,
        host_id=host_id,
        models_used=models_used,
        tokens_used=tokens_used,
        energy_cost=energy_cost,
    )

    inference = {
        "schema": "unitares.inference_result.v0",
        "host_id": host_id,
        "provider_kind": host.get("provider_kind"),
        "transport": host.get("transport", "host_adapter"),
        "model_used": adapter_provenance.get("model_used"),
        "models_used": models_used,
        "model_requested": request.model,
        "task_type": request.task_type,
        "privacy_class": host.get("privacy_class"),
        "cost_class": host.get("cost_class"),
        "cost_usd": adapter_provenance.get("cost_usd"),
        "accountability_class": host.get("accountability_class", "tool_evidence"),
        "requesting_agent_uuid": request.requesting_agent_uuid,
        "orchestrator_execution_id": (
            adapter_result.get("execution_id") or adapter_result.get("agent_id")
        ),
        "orchestrator_agent_id": (
            adapter_result.get("execution_id") or adapter_result.get("agent_id")
        ),
        "latency_ms": adapter_provenance.get("latency_ms"),
        "tokens_used": tokens_used,
        "provider_usage": adapter_provenance.get("provider_usage") or {},
        "provider_model_usage": adapter_provenance.get("provider_model_usage") or {},
        "energy_cost": energy_cost,
        "prompt_hash": _sha256_text(request.prompt),
        "response_hash": _sha256_text(response_text),
        "finish_reason": adapter_provenance.get("finish_reason"),
        "terminal_answer": terminal_answer,
        "configured_by": "operator",
        "warnings": adapter_provenance.get("warnings") or [],
    }

    return InferenceOutcome(
        response=response_text,
        inference=inference,
        routed_via="agent_orchestrator",
        task_type=request.task_type,
        model_used=inference["model_used"],
        models_used=tuple(models_used),
        tokens_used=tokens_used,
        energy_cost=energy_cost,
        message=f"Delegated inference completed via {host.get('display_name') or host_id}",
    )


@mcp_tool("delegate_inference", timeout=480.0)
async def handle_delegate_inference(
    arguments: Dict[str, Any],
) -> Sequence[TextContent]:
    """Ask an operator-authorized strong model for attributed advisory evidence."""
    prompt, error = require_argument(arguments, "prompt")
    if error:
        return [error]

    requesting_agent_uuid = (
        get_context_resolved_agent_id() or arguments.get("agent_id")
    )
    request = DelegatedInferenceRequest(
        prompt=str(prompt),
        requesting_agent_uuid=requesting_agent_uuid,
        host_id=str(arguments.get("host_id") or "claude:host-adapter"),
        model=(str(arguments["model"]) if arguments.get("model") else None),
        task_type=str(arguments.get("task_type") or "reasoning"),
        timeout_s=int(arguments.get("timeout_s", 240)),
    )
    outcome = await run_delegated_inference(request)
    if not outcome.ok:
        failure = outcome.failure
        assert failure is not None
        # The typed service uses INFERENCE_HOST_UNAVAILABLE to tell consult a
        # pre-execution fallback is safe. Preserve the established raw tool
        # contract, where every post-registry adapter failure was reported as
        # DELEGATED_INFERENCE_FAILED (timeouts remain their own code).
        raw_error_code = failure.code
        raw_details = dict(failure.details)
        raw_details["execution_started"] = failure.execution_started
        raw_details["possibly_running"] = failure.possibly_running
        if (
            raw_error_code == "INFERENCE_HOST_UNAVAILABLE"
            and "dispatch_phase" in raw_details
        ):
            raw_error_code = "DELEGATED_INFERENCE_FAILED"
            raw_details.pop("dispatch_phase", None)
        return [error_response(
            failure.message,
            error_code=raw_error_code,
            error_category=failure.category,
            details=raw_details,
            recovery=failure.recovery,
            arguments=arguments,
        )]

    return success_response({
        "success": True,
        "response": outcome.response,
        "model_used": outcome.model_used,
        "models_used": list(outcome.models_used),
        "tokens_used": outcome.tokens_used,
        "energy_cost": outcome.energy_cost,
        "routed_via": outcome.routed_via,
        "task_type": outcome.task_type,
        "inference": outcome.inference,
        "message": outcome.message,
    }, agent_id=requesting_agent_uuid, arguments=arguments)
