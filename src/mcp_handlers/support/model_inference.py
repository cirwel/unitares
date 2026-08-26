"""
Model Inference Tool - Free/low-cost LLM access for agents.

Supports two providers:
- Ollama (local, free) — default when privacy="local"
- Hugging Face Inference Providers (free tier, OpenAI-compatible) — requires
  HF_TOKEN

Agents call models for reasoning, generation, or analysis.
Usage tracked in EISV (Energy consumption) for self-regulation.
"""

from typing import Dict, Any, Sequence
from mcp.types import TextContent
import asyncio
from dataclasses import dataclass
import inspect
import os
import time

from ..utils import success_response, error_response, require_argument
from ..decorators import mcp_tool
from .inference_registry import (
    _ollama_available,
    default_local_model,
    get_inference_host,
    host_for_routed_provider,
    list_inference_hosts,
    ollama_base_url,
    sha256_text as _sha256_text,
)
from src.logging_utils import get_logger
from src.mcp_handlers.context import get_context_resolved_agent_id
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
from .inference_outcome import InferenceOutcome
logger = get_logger(__name__)

# Check if OpenAI SDK available (Ollama and HF Inference Providers expose
# OpenAI-compatible APIs that this client speaks). Keep the historical
# module-level ``OpenAI`` patch seam, but point it at the async client: the
# network request must remain cancellable by the facade's wall-clock deadline.
try:
    from openai import AsyncOpenAI

    OpenAI = AsyncOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


@dataclass(frozen=True, slots=True)
class CallModelRequest:
    """Validated input to the standard advisory inference service."""

    prompt: str
    requesting_agent_uuid: str | None
    model: str = "auto"
    provider: str = "auto"
    host_id: str | None = None
    task_type: str = "reasoning"
    max_tokens: int = 2048
    temperature: float = 0.7
    # None means "caller said nothing". It is NOT the same as an explicit
    # privacy="local": an explicit local is a policy floor that must never be
    # weakened, while an unstated one is only a default and must not silently
    # override an explicit provider. Collapsing the two is what made
    # provider="hf" route to Ollama and 404.
    privacy: str | None = None
    timeout_s: float = 235.0


# Model-id shapes that only exist on the Hugging Face side. Kept in one place so
# the routing test and the branch that acts on it cannot drift apart.
_HF_MODEL_PREFIXES = ("deepseek-ai/", "openai/gpt-oss", "hf:", "Qwen/", "qwen/")


def _is_hf_model_id(model: str) -> bool:
    """True when the model id itself names the Hugging Face lane."""
    return model.startswith(_HF_MODEL_PREFIXES)


def _invocation_gate() -> Dict[str, Any]:
    """Disclose inference-call identity gates on the two discovery reads.

    Both discovery tools serve ``pre_onboard``; ``call_model`` does not. So an
    unbound caller can enumerate every host and then fail on all of them — the
    same discoverable-but-not-usable shape that ``accepts_host_id_from`` exists
    to kill, one level up. The gate itself is worth keeping: model calls consume
    Energy in EISV, and unattributed consumption is exactly what that accounting
    is for. What is not worth keeping is finding out only at the call.

    Derived from the decorator rather than restated, so relaxing or tightening
    the gate cannot leave this text behind claiming the old rule.
    """
    from ..decorators import get_call_identity_requirement

    call_model_requirement = get_call_identity_requirement("call_model", {})
    delegate_requirement = get_call_identity_requirement("delegate_inference", {})
    consult_requirement = get_call_identity_requirement("consult", {})
    gate: Dict[str, Any] = {
        # Preserve the original scalar fields for older clients while exposing
        # the complete per-tool map for hosts routed outside call_model.
        "tool": "call_model",
        "requires_identity": call_model_requirement,
        "tools": {
            "call_model": {"requires_identity": call_model_requirement},
            "delegate_inference": {"requires_identity": delegate_requirement},
            "consult": {"requires_identity": consult_requirement},
        },
    }
    if "required" in {
        call_model_requirement,
        delegate_requirement,
        consult_requirement,
    }:
        gate["note"] = (
            "Listing hosts serves unbound callers; calling one does not. "
            "Bind first with start_session(force_new=true) — inference calls "
            "are attributed as Energy consumption and evidence."
        )
    return gate


@mcp_tool("list_inference_hosts", timeout=5.0, requires_identity="pre_onboard")
async def handle_list_inference_hosts(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """List known inference hosts and adapter placeholders."""
    include_unconfigured = arguments.get("include_unconfigured", True)
    if isinstance(include_unconfigured, str):
        include_unconfigured = include_unconfigured.strip().lower() not in (
            "0", "false", "no",
        )
    provider_kind = arguments.get("provider_kind")
    # list_inference_hosts() runs a blocking Ollama socket probe; offload it off
    # the event loop so this unauthenticated pre_onboard read can't stall the
    # anyio task group (see CLAUDE.md "Substrate Tax" + the cached probe).
    hosts = await asyncio.to_thread(
        list_inference_hosts,
        include_unconfigured=bool(include_unconfigured),
        provider_kind=provider_kind,
    )
    return success_response({
        "success": True,
        "schema": "unitares.inference_hosts.v0",
        "hosts": hosts,
        "count": len(hosts),
        "invocation": _invocation_gate(),
    }, agent_id=arguments.get("agent_id"), arguments=arguments)


@mcp_tool("describe_inference_host", timeout=5.0, requires_identity="pre_onboard")
async def handle_describe_inference_host(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Describe one inference host by host_id."""
    host_id, error = require_argument(arguments, "host_id")
    if error:
        return [error]

    # get_inference_host() runs a blocking Ollama socket probe; offload it (same
    # reasoning as list_inference_hosts above — unauthenticated pre_onboard read).
    host = await asyncio.to_thread(get_inference_host, str(host_id))
    if host is None:
        return [error_response(
            f"Inference host '{host_id}' is not registered",
            error_code="INFERENCE_HOST_NOT_FOUND",
            error_category="validation_error",
            recovery={
                "action": "Call list_inference_hosts to see registered hosts",
                "related_tools": ["list_inference_hosts"],
            },
        )]

    return success_response({
        "success": True,
        "schema": "unitares.inference_host.v0",
        "host": host,
        "invocation": _invocation_gate(),
    }, agent_id=arguments.get("agent_id"), arguments=arguments)


def _call_model_timeout(default: float = 240.0) -> float:
    """Wall-clock budget for one call_model round-trip. The 30s this tool
    shipped with on 2026-01-13 was never tuned against a real local model:
    gemma4 measures 43-70s warm (see llm_delegation._reviewer_timeout) and
    2-3 minutes cold-loading, so a cold call could not fit inside the cap
    even in principle. Until 2026-07-28 the mismatch was masked — the sync
    OpenAI client blocked the event loop, which disarmed the asyncio.wait_for
    timer that was supposed to fire. Moving that call to an executor thread
    (below) armed the timer, and the cap then began cutting local inference
    off at exactly 30.00s: 731 of the 786 mcp_handler_timeout events in the
    30 days to 2026-08-23 were this tool, every one of them landing within
    15ms of the cap. 240s matches the delegated-inference lane's default
    (support/delegated_inference.py) so the two inference paths cannot drift
    apart. Tunable via UNITARES_CALL_MODEL_TIMEOUT."""
    raw = os.getenv("UNITARES_CALL_MODEL_TIMEOUT")
    if raw:
        try:
            parsed = float(raw)
        except ValueError:
            logger.warning(
                "UNITARES_CALL_MODEL_TIMEOUT=%r is not a number; "
                "falling back to %ss", raw, default,
            )
            return default
        if parsed <= 0:
            logger.warning(
                "UNITARES_CALL_MODEL_TIMEOUT=%r is not positive; "
                "falling back to %ss", raw, default,
            )
            return default
        return parsed
    return default


def _provider_timeout_s() -> float:
    """End the provider request before the public tool deadline.

    Provider timeout values are phase budgets in HTTPX, not an aggregate
    deadline. ``run_model_inference`` therefore also wraps the cancellable
    async request in ``asyncio.timeout``; this shorter budget leaves cleanup
    margin for the MCP wrapper.
    """
    return max(1.0, _call_model_timeout() - 5.0)


async def run_model_inference(request: CallModelRequest) -> InferenceOutcome:
    """Run one standard advisory inference without an MCP response envelope."""
    if not OPENAI_AVAILABLE:
        return InferenceOutcome.failed(
            "OpenAI SDK required for model inference. Install with: pip install openai",
            code="DEPENDENCY_MISSING",
            category="system_error",
            recovery={
                "action": "Install OpenAI SDK",
                "related_tools": ["health_check"],
                "workflow": [
                    "1. Install: pip install openai",
                    "2. Restart MCP server",
                    "3. Retry call_model tool"
                ]
            }
        )

    prompt = request.prompt
    model = request.model
    task_type = request.task_type
    max_tokens = request.max_tokens
    temperature = request.temperature
    privacy = request.privacy
    provider = request.provider
    host_id = request.host_id

    if host_id:
        host = get_inference_host(str(host_id))
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
        # Reachability is checked BEFORE availability on purpose. A host that
        # nothing routes to stays unreachable no matter how configured it is, so
        # reporting it as "not available" would send the caller off to enable a
        # flag that cannot help. Say the true thing first.
        if "call_model" not in (host.get("accepts_host_id_from") or []):
            return InferenceOutcome.failed(
                f"Inference host '{host_id}' is registered but not reachable from call_model",
                code="INFERENCE_HOST_UNREACHABLE",
                category="validation_error",
                details={"host": host, "accepts_host_id_from": host.get("accepts_host_id_from") or []},
                recovery={
                    "action": (
                        "Pick a host whose accepts_host_id_from includes call_model. "
                        "The Claude and Codex strong-model hosts run through "
                        "delegate_inference instead."
                    ),
                    "related_tools": ["list_inference_hosts", "delegate_inference"],
                },
            )
        if not host.get("configured") or not host.get("available"):
            return InferenceOutcome.failed(
                f"Inference host '{host_id}' is not available",
                code="INFERENCE_HOST_UNAVAILABLE",
                category="system_error",
                details={"host": host},
                recovery={
                    "action": "Choose an available host or configure the requested adapter",
                    "related_tools": ["list_inference_hosts", "describe_inference_host"],
                },
            )
        provider_kind = host.get("provider_kind")
        if provider_kind == "ollama":
            provider = "ollama"
            privacy = "local"
        elif provider_kind == "hf":
            provider = "hf"
            privacy = "cloud"
        else:
            # Reachable per the registry but this handler has no branch for it —
            # a registry/handler drift, not an agent error.
            return InferenceOutcome.failed(
                f"Inference host '{host_id}' uses unsupported provider kind '{provider_kind}'",
                code="INFERENCE_HOST_UNSUPPORTED",
                category="system_error",
                details={"host": host},
                recovery={
                    "action": (
                        "Registry drift: the host claims call_model reachability but "
                        "call_model has no route for this provider_kind. Use an "
                        "ollama or hf host and report the drift."
                    ),
                    "related_tools": ["list_inference_hosts"],
                },
            )
    
    # `privacy is None` means the caller said nothing, and that is the only way to
    # tell an unstated default from an explicit privacy="local". The distinction is
    # load-bearing: an explicit local is a policy floor that must never be weakened,
    # but a default must not silently outrank an explicit provider. Collapsing the
    # two sent provider="hf" to Ollama, which then 404'd with a recovery hint
    # advising `ollama pull` on a Hugging Face model id.
    privacy_stated = privacy is not None
    privacy = privacy or "local"

    # An HF model id under provider="auto" requests the HF lane as plainly as
    # naming the provider does.
    wants_hf = provider == "hf" or (provider == "auto" and _is_hf_model_id(model))

    if privacy_stated and privacy == "local" and provider == "hf":
        return InferenceOutcome.failed(
            "provider='hf' conflicts with privacy='local'",
            code="PROVIDER_PRIVACY_CONFLICT",
            category="validation_error",
            details={"provider": provider, "privacy": privacy},
            recovery={
                "action": (
                    "Hugging Face is external, so this asks to stay local and to "
                    "leave at the same time. Drop provider='hf' to stay local, or "
                    "pass privacy='cloud' (or 'auto') to allow external "
                    "processing. Privacy is never weakened implicitly."
                ),
                "related_tools": ["list_inference_hosts", "describe_inference_host"],
            },
        )

    # Privacy routing: local unless the caller asked for a lane that is not.
    if provider == "ollama" or (privacy == "local" and not wants_hf):
        # Route to Ollama (local). Model names pass through verbatim so
        # callers get a clean 404 if the model isn't pulled — no silent
        # aliasing to a model that may also be absent.
        base_url = ollama_base_url() + "/v1"  # Ollama OpenAI-compatible API
        if model == "auto":
            model = default_local_model()
        api_key = "ollama"  # Dummy key - Ollama ignores it but OpenAI SDK requires non-None
        provider = "ollama"
        logger.info(f"Privacy mode: local - routing to Ollama with model {model}")
    elif wants_hf:
        # Hugging Face Inference Providers (free tier, OpenAI-compatible)
        base_url = "https://router.huggingface.co/v1"
        api_key = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")
        if not api_key:
            return InferenceOutcome.failed(
                "HF_TOKEN or HUGGINGFACE_TOKEN required for Hugging Face Inference Providers",
                code="MISSING_CONFIG",
                category="system_error",
                recovery={
                    "action": "Set HF_TOKEN environment variable (get free token from https://huggingface.co/settings/tokens)",
                    "related_tools": ["health_check"],
                    "workflow": [
                        "1. Get free token: https://huggingface.co/settings/tokens",
                        "2. Set: export HF_TOKEN=your_token",
                        "3. Restart MCP server",
                        "4. Retry call_model tool"
                    ]
                }
            )
        # Clean model name (remove hf: prefix if present)
        if model.startswith("hf:"):
            model = model[3:]
        # Default model if auto
        if model == "auto":
            model = "deepseek-ai/DeepSeek-R1:fastest"  # Default HF model
        # Qwen shorthand: bare "qwen" or "qwen2.5" → full HF model ID
        elif model.lower() in ("qwen", "qwen2.5"):
            model = "Qwen/Qwen2.5-72B-Instruct:fastest"
        # Use HF model with :fastest or :cheapest suffix for auto-selection (if not already present)
        elif ":" not in model:
            model = f"{model}:fastest"  # Auto-select fastest provider
        provider = "hf"  # may arrive as "auto" via model-prefix detection
        logger.info(f"Using Hugging Face Inference Providers: {model}")
    elif provider == "auto":
        # Auto-select: Try Ollama first (local, free), then Gemini, then HF.
        # Reuse the registry's cached probe instead of a second inline socket
        # connect — same source of truth, and it benefits from the TTL cache.
        ollama_available = _ollama_available()

        if ollama_available:
            # Prefer Ollama (local, free, no token needed)
            base_url = ollama_base_url() + "/v1"
            api_key = "ollama"
            model = default_local_model() if model == "auto" else model
            provider = "ollama"
            logger.info(f"Auto-selected Ollama (local): {model}")
        else:
            # Fallback: HF if a token is configured; otherwise give up.
            hf_token = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")

            if hf_token:
                base_url = "https://router.huggingface.co/v1"
                api_key = hf_token
                model = "deepseek-ai/DeepSeek-R1:fastest" if model == "auto" else model
                if ":" not in model and not model.startswith("deepseek-ai/") and not model.startswith("openai/gpt-oss"):
                    model = f"{model}:fastest"
                provider = "hf"
                logger.info(f"Auto-selected Hugging Face: {model}")
            else:
                return InferenceOutcome.failed(
                    "No provider available. Ollama not running and HF_TOKEN not configured.",
                    code="MISSING_CONFIG",
                    category="system_error",
                    recovery={
                        "action": "Start Ollama (recommended) or set HF_TOKEN",
                        "related_tools": ["health_check"],
                        "workflow": [
                            "1. Install & run Ollama: ollama serve (recommended - free, local)",
                            "2. Or get HF token: https://huggingface.co/settings/tokens",
                            "3. Retry call_model tool"
                        ]
                    }
                )
    else:
        # Unknown provider value. Pydantic schema (Literal["auto","hf","ollama"])
        # blocks this in normal MCP calls; only direct calls can reach here.
        return InferenceOutcome.failed(
            f"Unknown provider '{provider}'. Expected one of: auto, hf, ollama.",
            code="INVALID_PROVIDER",
            category="validation_error",
        )
    
    try:
        started = time.monotonic()

        logger.debug(f"Calling model '{model}' via {base_url} for task_type='{task_type}'")

        # Use AsyncOpenAI rather than an executor-wrapped sync client. The
        # latter keeps running after task cancellation, so a cold or wedged
        # provider could outlive consult's public deadline. Async HTTP I/O is
        # cancelled with this task, and asyncio.timeout supplies an aggregate
        # wall-clock cap in addition to HTTPX's per-phase timeouts.
        client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=request.timeout_s,
            max_retries=0,
        )
        try:
            async with asyncio.timeout(request.timeout_s):
                pending_response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                # Some downstream tests and embedders retain a synchronous
                # mock at the historical OpenAI patch seam. Production's
                # AsyncOpenAI result is always awaitable.
                response = (
                    await pending_response
                    if inspect.isawaitable(pending_response)
                    else pending_response
                )
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                try:
                    close_result = close()
                    if inspect.isawaitable(close_result):
                        await close_result
                except Exception as close_error:
                    # Cleanup must never replace an active CancelledError (or a
                    # provider exception) and turn cancellation into a normal
                    # failed outcome. Async cancellation remains uncaught; an
                    # ordinary close failure is diagnostic only.
                    logger.warning(
                        "Could not close async inference client: %s",
                        close_error,
                    )
        latency_ms = int((time.monotonic() - started) * 1000)
        
        message = response.choices[0].message
        result_text = message.content or ""
        # Ollama's OpenAI-compat adapter surfaces a non-standard `reasoning`
        # field for thinking-style models (gemma4, deepseek-r1, etc.). When a
        # model exhausts max_tokens while reasoning and never emits a final
        # answer, `content` comes back empty but `reasoning` holds the trace.
        # Returning empty would hide the model's output entirely; surface the
        # reasoning instead so callers can see what the model was working on.
        reasoning_text = getattr(message, "reasoning", None) or ""
        if not result_text and reasoning_text:
            logger.warning(
                f"Model '{model}' returned empty content with "
                f"{len(reasoning_text)} chars of reasoning — likely hit "
                "max_tokens mid-thought. Returning reasoning trace."
            )
            result_text = (
                "[Model hit token limit before emitting a final answer; "
                "returning the thinking trace it produced.]\n\n"
                + reasoning_text
            )
        tokens_used = response.usage.total_tokens if hasattr(response, 'usage') else 0
        model_used = getattr(response, 'model', model)
        
        # Estimate Energy cost (simple: +0.01 per call; refine later based on tokens)
        # Free/local models (llama, qwen, gemma): minimal cost.
        # Everything else gets the default estimate.
        if "llama" in model.lower() or "qwen" in model.lower() or "gemma" in model.lower():
            energy_cost = 0.01  # Free tier
        else:
            energy_cost = 0.03  # Default estimate
        
        # Track usage and update Energy in governance monitor
        logger.info(f"Model inference: model={model_used}, tokens={tokens_used}, energy_cost={energy_cost}")
        
        # Update Energy in governance monitor (if agent_id available)
        agent_id = request.requesting_agent_uuid
        if agent_id:
            try:
                monitor = mcp_server.get_or_create_monitor(agent_id)
                from src.agent_monitor_state import ensure_hydrated
                await ensure_hydrated(monitor, agent_id)

                # Update Energy through a lightweight process_update
                # Model inference consumes Energy - reflect this in EISV dynamics
                # Use low complexity (0.1-0.2) since inference is a tool, not core work
                # The energy_cost affects how much Energy is consumed
                inference_complexity = min(0.1 + energy_cost * 2, 0.3)  # Scale energy_cost to complexity
                
                # Create a lightweight update that reflects model inference usage
                # This flows through normal EISV dynamics, updating Energy appropriately
                monitor.process_update({
                    "response_text": f"Model inference: {task_type} via {model_used} ({tokens_used} tokens)",
                    "complexity": inference_complexity,
                    "confidence": 0.8  # Model inference is generally reliable
                })
                
                logger.debug(f"Updated Energy for agent {agent_id}: model inference tracked (cost={energy_cost}, complexity={inference_complexity})")
            except Exception as e:
                # Non-critical: if Energy tracking fails, still return the inference result
                logger.warning(f"Could not update Energy for model inference: {e}")
        else:
            logger.debug("No agent_id available for Energy tracking (model inference still successful)")
        
        # Determine routing method by the resolved provider, not by substring
        # matching on the URL — UNITARES_OLLAMA_BASE may point at a non-local
        # host and the route is still Ollama.
        if provider == "hf":
            routed_via = "huggingface"
        elif provider == "ollama":
            routed_via = "ollama"
        else:
            routed_via = "direct"

        host = host_for_routed_provider(provider)
        finish_reason = getattr(response.choices[0], "finish_reason", None)
        if not isinstance(finish_reason, str):
            finish_reason = None
        inference = {
            "schema": "unitares.inference_result.v0",
            "host_id": host.get("host_id"),
            "provider_kind": host.get("provider_kind", provider),
            "transport": host.get("transport", "direct"),
            "model_used": model_used,
            "task_type": task_type,
            "privacy_class": host.get("privacy_class", privacy),
            "cost_class": host.get("cost_class", "unknown"),
            "accountability_class": host.get("accountability_class", "tool_evidence"),
            "requesting_agent_uuid": request.requesting_agent_uuid,
            "latency_ms": latency_ms,
            "tokens_used": tokens_used,
            "energy_cost": energy_cost,
            "prompt_hash": _sha256_text(prompt),
            "response_hash": _sha256_text(result_text),
            "finish_reason": finish_reason,
            "configured_by": (
                "operator" if host.get("provider_kind") == "hf" else "local_runtime"
            ),
            "warnings": [],
        }
        
        return InferenceOutcome(
            response=result_text,
            inference=inference,
            routed_via=routed_via,
            task_type=task_type,
            model_used=model_used,
            tokens_used=tokens_used,
            energy_cost=energy_cost,
            message=f"Model inference completed via {routed_via}",
        )
        
    except Exception as e:
        logger.error(f"Model inference failed: {e}", exc_info=True)
        
        # Provide helpful error message
        error_msg = str(e)
        if isinstance(e, TimeoutError):
            error_msg = (
                f"request exceeded the {request.timeout_s:g}s wall-clock timeout"
            )
            error_code = "TIMEOUT"
            recovery_hint = "Try a shorter prompt or increase timeout"
        elif "timeout" in error_msg.lower():
            error_code = "TIMEOUT"
            recovery_hint = "Try a shorter prompt or increase timeout"
        elif "rate limit" in error_msg.lower():
            error_code = "RATE_LIMIT_EXCEEDED"
            recovery_hint = "Wait a moment and retry, or use a different model"
        elif (
            provider == "ollama"
            and any(marker in error_msg.lower() for marker in ("connection refused", "connection error", "failed to establish", "connect"))
        ):
            error_code = "MODEL_PROVIDER_UNAVAILABLE"
            recovery_hint = (
                "Ollama is not reachable. Start Ollama, or explicitly opt into fallback "
                "routing with privacy='auto' or privacy='cloud' and provider='hf'."
            )
        elif "not found" in error_msg.lower() or "invalid" in error_msg.lower():
            error_code = "MODEL_NOT_AVAILABLE"
            if provider == "ollama":
                recovery_hint = (
                    f"Model '{model}' is not pulled on this host. "
                    f"Run `ollama list` to see available models, `ollama pull {model}` to fetch it, "
                    "or call with privacy='auto' to allow configured cloud fallback."
                )
            else:
                recovery_hint = (
                    f"Model '{model}' not available on this provider. "
                    "Check the provider's model catalog or try a different model."
                )
        else:
            error_code = "INFERENCE_ERROR"
            recovery_hint = "Check provider configuration and model availability"
        
        return InferenceOutcome.failed(
            f"Model inference failed: {error_msg}",
            code=error_code,
            category="system_error",
            details={
                "model_requested": model,
                "base_url": base_url,
                "task_type": task_type
            },
            recovery={
                "action": recovery_hint,
                "related_tools": ["health_check", "get_connection_status"],
                "workflow": [
                    "1. Check provider configuration",
                    "2. Verify model is available (`ollama list` for local)",
                    "3. For local failures, start Ollama or pull the requested model",
                    "4. To allow fallback, retry with privacy='auto' or privacy='cloud' and provider='hf'",
                    "5. Check server logs for details"
                ]
            }
        )


@mcp_tool("call_model", timeout=_call_model_timeout())
async def handle_call_model(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Call a standard advisory model for reasoning, generation, or analysis."""
    prompt, error = require_argument(arguments, "prompt")
    if error:
        return [error]

    requesting_agent_uuid = (
        get_context_resolved_agent_id() or arguments.get("agent_id")
    )
    request = CallModelRequest(
        prompt=str(prompt),
        requesting_agent_uuid=requesting_agent_uuid,
        model=str(arguments.get("model", "auto")),
        provider=str(arguments.get("provider", "auto")),
        host_id=(str(arguments["host_id"]) if arguments.get("host_id") else None),
        task_type=str(arguments.get("task_type", "reasoning")),
        max_tokens=int(arguments.get("max_tokens", 2048)),
        temperature=float(arguments.get("temperature", 0.7)),
        privacy=(str(arguments["privacy"]) if arguments.get("privacy") else None),
        timeout_s=_provider_timeout_s(),
    )
    outcome = await run_model_inference(request)
    if not outcome.ok:
        failure = outcome.failure
        assert failure is not None
        return [error_response(
            failure.message,
            error_code=failure.code,
            error_category=failure.category,
            details=failure.details,
            recovery=failure.recovery,
        )]

    return success_response({
        "success": True,
        "response": outcome.response,
        "model_used": outcome.model_used,
        "tokens_used": outcome.tokens_used,
        "energy_cost": outcome.energy_cost,
        "routed_via": outcome.routed_via,
        "task_type": outcome.task_type,
        "inference": outcome.inference,
        "message": outcome.message,
    }, agent_id=requesting_agent_uuid, arguments=arguments)
