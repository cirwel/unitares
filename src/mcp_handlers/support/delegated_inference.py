"""Long-running strong-model delegation through operator-authorized host CLIs."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Sequence

from mcp.types import TextContent

from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server

from ..context import get_context_agent_id
from ..decorators import mcp_tool
from ..utils import error_response, require_argument, success_response
from .host_adapter import invoke_host_adapter
from .inference_registry import get_inference_host, sha256_text as _sha256_text

logger = get_logger(__name__)


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


@mcp_tool("delegate_inference", timeout=480.0)
async def handle_delegate_inference(
    arguments: Dict[str, Any],
) -> Sequence[TextContent]:
    """Ask an operator-authorized strong model for attributed advisory evidence."""
    prompt, error = require_argument(arguments, "prompt")
    if error:
        return [error]

    host_id = str(arguments.get("host_id") or "claude:host-adapter")
    task_type = str(arguments.get("task_type") or "reasoning")
    model = arguments.get("model")
    timeout_s = int(arguments.get("timeout_s", 240))

    # Registry construction includes a cached blocking Ollama probe even when
    # looking up Claude. Keep that probe off the MCP event loop.
    host = await asyncio.to_thread(get_inference_host, host_id)
    if host is None:
        return [error_response(
            f"Inference host '{host_id}' is not registered",
            error_code="INFERENCE_HOST_NOT_FOUND",
            error_category="validation_error",
            recovery={
                "action": "Call list_inference_hosts to see registered hosts",
                "related_tools": ["list_inference_hosts"],
            },
            arguments=arguments,
        )]

    if "delegate_inference" not in (host.get("accepts_host_id_from") or []):
        return [error_response(
            f"Inference host '{host_id}' is not reachable from delegate_inference",
            error_code="INFERENCE_HOST_UNREACHABLE",
            error_category="validation_error",
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
            arguments=arguments,
        )]

    if not host.get("configured") or not host.get("available"):
        return [error_response(
            f"Inference host '{host_id}' is not available",
            error_code="INFERENCE_HOST_UNAVAILABLE",
            error_category="system_error",
            details={"host": host},
            recovery={
                "action": (
                    "Set UNITARES_HOST_ADAPTER_ENABLED=1, configure the "
                    "orchestrator bearer, and install or point UNITARES_CLAUDE_CLI "
                    "at an authenticated Claude CLI"
                ),
                "related_tools": ["describe_inference_host", "health_check"],
            },
            arguments=arguments,
        )]

    adapter_result = await invoke_host_adapter(
        host_id,
        str(prompt),
        timeout_s=timeout_s,
        sandbox="read-only",
        model=str(model) if model else None,
    )
    adapter_provenance = adapter_result.get("provenance") or {}
    if not adapter_result.get("ok"):
        still_running = adapter_result.get("status") == "still_running"
        message = (
            f"Delegated inference exceeded its {timeout_s}s await window"
            if still_running
            else str(adapter_result.get("error") or "Host adapter returned a nonzero exit")
        )
        return [error_response(
            message,
            error_code=(
                "DELEGATED_INFERENCE_TIMEOUT"
                if still_running
                else "DELEGATED_INFERENCE_FAILED"
            ),
            error_category="system_error",
            details={
                "host_id": host_id,
                "adapter_status": adapter_result.get("status"),
                "orchestrator_agent_id": adapter_result.get("agent_id"),
                "exit_status": adapter_result.get("exit_status"),
                "inference_provenance": adapter_provenance,
            },
            recovery={
                "action": (
                    "Retry with a shorter prompt or a larger timeout_s"
                    if still_running
                    else "Check the host adapter configuration and orchestrator logs"
                ),
                "related_tools": ["describe_inference_host", "health_check"],
            },
            arguments=arguments,
        )]

    response_text = str(adapter_result.get("text") or "")
    models_used = [
        str(value) for value in (adapter_provenance.get("models_used") or [])
    ]
    tokens_used = int(adapter_provenance.get("tokens_used") or 0)
    # Strong external consultation gets a deliberately modest, fixed accounting
    # increment. Dollar cost remains provider-reported metadata, not an EISV proxy.
    energy_cost = 0.05
    requesting_agent_uuid = get_context_agent_id() or arguments.get("agent_id")

    await _track_energy(
        requesting_agent_uuid,
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
        "model_requested": model,
        "task_type": task_type,
        "privacy_class": host.get("privacy_class"),
        "cost_class": host.get("cost_class"),
        "cost_usd": adapter_provenance.get("cost_usd"),
        "accountability_class": host.get("accountability_class", "tool_evidence"),
        "requesting_agent_uuid": requesting_agent_uuid,
        "orchestrator_agent_id": adapter_result.get("agent_id"),
        "latency_ms": adapter_provenance.get("latency_ms"),
        "tokens_used": tokens_used,
        "provider_usage": adapter_provenance.get("provider_usage") or {},
        "provider_model_usage": adapter_provenance.get("provider_model_usage") or {},
        "energy_cost": energy_cost,
        "prompt_hash": _sha256_text(str(prompt)),
        "response_hash": _sha256_text(response_text),
        "finish_reason": adapter_provenance.get("finish_reason"),
        "configured_by": "operator",
        "warnings": adapter_provenance.get("warnings") or [],
    }

    return success_response({
        "success": True,
        "response": response_text,
        "model_used": inference["model_used"],
        "models_used": models_used,
        "tokens_used": tokens_used,
        "energy_cost": energy_cost,
        "routed_via": "agent_orchestrator",
        "task_type": task_type,
        "inference": inference,
        "message": "Delegated inference completed via the Claude host adapter",
    }, agent_id=requesting_agent_uuid, arguments=arguments)
