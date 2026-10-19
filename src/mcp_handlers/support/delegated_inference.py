"""Long-running strong-model delegation through operator-authorized host CLIs."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, Sequence

from mcp.types import TextContent

from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server

from ..context import get_context_resolved_agent_id
from ..decorators import mcp_tool
from ..schemas.core import (
    DELEGATE_INFERENCE_DEFAULT_TIMEOUT_S,
    DELEGATE_INFERENCE_OVERHEAD_S,
)
from ..utils import error_response, require_argument, success_response
from . import host_availability
from src import inference_config
from .host_adapter import (
    HOST_ADAPTER_EXTENSION,
    HOST_ADAPTER_EXTENSION_NOTE,
    ORCHESTRATOR_BACKSTOP_GRACE_S,
    codex_answer_region_located,
    host_adapter_disabled_hosts,
    host_adapter_lane_configured,
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
    timeout_s: int = DELEGATE_INFERENCE_DEFAULT_TIMEOUT_S


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


# Bookkeeping that outlived its call's deadline, held so it is not garbage
# collected mid-flight (the src/agent_loop_detection.py pattern).
_background_tasks: set[asyncio.Task] = set()


def _log_deferred_outcome(task: asyncio.Task, step: str) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        logger.warning("Deferred delegated-inference %s was cancelled", step)
    elif task.exception() is not None:
        logger.warning(
            "Deferred delegated-inference %s failed: %r", step, task.exception()
        )


async def _finish_by_deadline(
    coro: Any, deadline: float, *, step: str,
) -> tuple[bool, Any]:
    """Await ``coro`` until ``deadline``; return ``(finished, result)``.

    Past the deadline the step keeps running as a tracked background task and
    this returns ``(False, None)`` at once, so post-answer bookkeeping cannot
    hold the reply past the caller's budget. The response headroom beyond the
    deadline (DELEGATE_INFERENCE_RESPONSE_HEADROOM_S) covers building and
    delivering the reply. A step that fails inside the deadline is logged and
    counted as finished: bookkeeping is best-effort and never fails the call.
    """
    task = asyncio.ensure_future(coro)
    try:
        result = await asyncio.wait_for(
            asyncio.shield(task), timeout=max(0.0, deadline - time.monotonic()),
        )
    except asyncio.CancelledError:
        # The call itself was cancelled; the shielded step still runs.
        _background_tasks.add(task)
        task.add_done_callback(lambda done: _log_deferred_outcome(done, step))
        raise
    except TimeoutError:
        _background_tasks.add(task)
        task.add_done_callback(lambda done: _log_deferred_outcome(done, step))
        logger.warning(
            "Delegated-inference %s did not finish by the call deadline; "
            "it continues in the background",
            step,
        )
        return False, None
    except Exception as exc:  # noqa: BLE001 - bookkeeping is best-effort
        logger.warning("Delegated-inference %s failed: %r", step, exc)
        return True, None
    return True, result


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
                "No Codex app-server answer, typed agent_message event, or "
                "legacy 'codex' marker was found, so no assistant answer region "
                "was isolated. This "
                "is NOT the model returning a bad envelope — it is the adapter "
                "never finding an assistant turn. Check whether the CLI produced "
                "one at all before changing the envelope contract."
            )
    return details


async def run_delegated_inference(
    request: DelegatedInferenceRequest,
) -> InferenceOutcome:
    """Run one strong advisory inference without an MCP response envelope.

    Answers within ``request.timeout_s + DELEGATE_INFERENCE_OVERHEAD_S`` of
    being called: one wall-clock deadline bounds host resolution, the spawn
    request and every await, so their own timeouts cannot add up past it.
    """
    host_id = request.host_id
    deadline = time.monotonic() + request.timeout_s + DELEGATE_INFERENCE_OVERHEAD_S
    # Registry construction includes a cached blocking Ollama probe even when
    # looking up Claude. Keep that probe off the MCP event loop, and off the
    # caller's clock: past the overhead allowance it is a pre-execution failure.
    try:
        host = await asyncio.wait_for(
            asyncio.to_thread(get_inference_host, host_id),
            timeout=DELEGATE_INFERENCE_OVERHEAD_S,
        )
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

    # A server that never set up the orchestrator extension has nothing to
    # restore: say that, rather than listing flags as if one were missing.
    if (
        host.get("extension") == HOST_ADAPTER_EXTENSION
        and not host_adapter_lane_configured()
    ):
        return InferenceOutcome.failed(
            f"Inference host '{host_id}' needs the agent orchestrator "
            "extension, which this server does not have configured",
            code="INFERENCE_HOST_UNAVAILABLE",
            category="system_error",
            details={
                "host": host,
                "reason": "extension_not_configured",
                "extension": HOST_ADAPTER_EXTENSION,
            },
            recovery={
                "action": (
                    "Use consult(effort='standard') for the local lane. "
                    + HOST_ADAPTER_EXTENSION_NOTE
                ),
                "related_tools": ["consult", "list_inference_hosts"],
            },
        )

    loaded = inference_config.load()
    if loaded.state == "error":
        return InferenceOutcome.failed(
            "The inference hosts file does not load, so delegated inference refuses",
            code="INFERENCE_CONFIG_INVALID",
            category="system_error",
            details={"error": str(loaded.error)},
            recovery={
                "action": (
                    f"The operator fixes the file named by {inference_config.HOSTS_FILE_ENV}; "
                    "the doctor's inference_hosts_file check names the line or field."
                ),
                "related_tools": ["list_inference_hosts"],
            },
        )
    entry = loaded.config.hosts.get(host_id) if loaded.config is not None else None
    if entry is not None and not entry.enabled:
        return InferenceOutcome.failed(
            f"Inference host '{host_id}' is switched off in the inference hosts file",
            code="INFERENCE_HOST_UNAVAILABLE",
            category="system_error",
            details={"host": host},
            recovery={
                "action": "Choose another host, or enable it in the inference hosts file",
                "related_tools": ["list_inference_hosts"],
            },
        )

    if host_id in host_adapter_disabled_hosts():
        return InferenceOutcome.failed(
            f"Inference host '{host_id}' is switched off by the operator",
            code="INFERENCE_HOST_UNAVAILABLE",
            category="system_error",
            details={"host": host},
            recovery={
                "action": (
                    f"Choose another host, or remove {host_id} from "
                    "UNITARES_HOST_ADAPTER_DISABLED_HOSTS"
                ),
                "related_tools": ["list_inference_hosts"],
            },
        )

    cooling = host_availability.cooldown(host_id)
    if cooling is not None:
        return InferenceOutcome.failed(
            f"Inference host '{host_id}' is cooling down after a {cooling['reason']} "
            f"failure until {cooling['retry_after']}",
            code="INFERENCE_HOST_UNAVAILABLE",
            category="system_error",
            details={"host": host, "provider_unavailable": cooling},
            recovery={
                "action": (
                    f"Choose another host, or retry after {cooling['retry_after']}; "
                    "the next call after that re-checks the provider"
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
        deadline=deadline,
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
        # Only a call the provider actually answered can say the provider is
        # out: a spawn rejection is our own orchestrator, not the account.
        provider_unavailable = None
        accounting_deferred: list[str] = []
        if terminal_result:
            classified = host_availability.classify(str(adapter_result.get("error") or ""))
            if classified is not None:
                finished, provider_unavailable = await _finish_by_deadline(
                    host_availability.record_unavailable_async(
                        host_id, classified, detail=message,
                    ),
                    deadline,
                    step="provider_cooldown",
                )
                if not finished:
                    accounting_deferred.append("provider_cooldown")
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
                "provider_kind": host.get("provider_kind"),
                "privacy_class": host.get("privacy_class"),
                "adapter_status": adapter_result.get("status"),
                "dispatch_phase": dispatch_phase,
                "orchestrator_execution_id": orchestrator_execution_id,
                "orchestrator_agent_id": orchestrator_execution_id,
                "exit_status": adapter_result.get("exit_status"),
                "inference_provenance": adapter_provenance,
                "execution_started": execution_started,
                "possibly_running": possibly_running,
                # The window this call waited, and when the orchestrator
                # stops a child nobody is waiting for any more.
                "timeout_s": request.timeout_s,
                "orchestrator_backstop_s": (
                    request.timeout_s + ORCHESTRATOR_BACKSTOP_GRACE_S
                ),
                **(
                    {"provider_unavailable": provider_unavailable}
                    if provider_unavailable
                    else {}
                ),
                **(
                    {"accounting_deferred": accounting_deferred}
                    if accounting_deferred
                    else {}
                ),
                **_raw_excerpt_details(adapter_result),
            },
            recovery={
                "action": (
                    "Do not retry yet. The delegated execution may still be running; "
                    "the orchestrator stops it about "
                    f"{request.timeout_s + ORCHESTRATOR_BACKSTOP_GRACE_S}s after "
                    "dispatch. Sooner than that, ask the operator to reconcile or "
                    "terminate it using the orchestrator controls and "
                    "orchestrator_execution_id."
                    + (
                        " When you do retry, raise timeout_s only to a value "
                        "your own MCP client's per-tool timeout exceeds by "
                        "~15s, or the client gives up first and the execution "
                        "id never reaches you."
                        if still_running
                        else ""
                    )
                    if possibly_running
                    else "Check the host adapter configuration and orchestrator logs"
                ),
                "related_tools": ["describe_inference_host", "health_check"],
            },
            execution_started=execution_started,
            possibly_running=possibly_running,
        )

    # The answer is in hand. Bookkeeping after this point must not cost the
    # caller the answer: each step gets only what is left of the deadline,
    # and one that cannot finish in it completes in the background instead.
    accounting_deferred = []
    finished, _ = await _finish_by_deadline(
        host_availability.clear_async(host_id), deadline, step="cooldown_clear",
    )
    if not finished:
        accounting_deferred.append("cooldown_clear")
    response_text = str(adapter_result.get("text") or "")
    models_used = [
        str(value) for value in (adapter_provenance.get("models_used") or [])
    ]
    tokens_used = int(adapter_provenance.get("tokens_used") or 0)
    # Strong external consultation gets a deliberately modest, fixed accounting
    # increment. Dollar cost remains provider-reported metadata, not an EISV proxy.
    energy_cost = 0.05
    finished, _ = await _finish_by_deadline(
        _track_energy(
            request.requesting_agent_uuid,
            host_id=host_id,
            models_used=models_used,
            tokens_used=tokens_used,
            energy_cost=energy_cost,
        ),
        deadline,
        step="energy",
    )
    if not finished:
        accounting_deferred.append("energy")

    inference = {
        "schema": "unitares.inference_result.v0",
        "host_id": host_id,
        "provider_kind": host.get("provider_kind"),
        "transport": host.get("transport", "host_adapter"),
        "model_used": adapter_provenance.get("model_used"),
        "models_used": models_used,
        # Why model_used may be None (the CLI did not report one) and any
        # mid-run model switch the host observed; dropped here until 2026-09-26.
        "model_reporting_status": adapter_provenance.get("model_reporting_status"),
        "model_reroutes": adapter_provenance.get("model_reroutes") or [],
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
    if accounting_deferred:
        # Named steps still finishing after this response: the answer and its
        # execution id are complete, the bookkeeping lands shortly after.
        inference["accounting_deferred"] = accounting_deferred

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
        timeout_s=int(
            arguments.get("timeout_s", DELEGATE_INFERENCE_DEFAULT_TIMEOUT_S)
        ),
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
