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
import os

from ..utils import success_response, error_response, require_argument
from ..decorators import mcp_tool
from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
logger = get_logger(__name__)

# Check if OpenAI SDK available (Ollama and HF Inference Providers expose
# OpenAI-compatible APIs that this client speaks).
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

@mcp_tool("call_model", timeout=30.0)
async def handle_call_model(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Call a free/low-cost LLM for reasoning, generation, or analysis.

    Providers:
    - Ollama (local, free) — default when privacy="local" (the default). Pass a
      model name that is actually pulled on the host (`ollama list`). The
      local fallback for model="auto" is taken from UNITARES_LLM_MODEL
      (default "gemma4:latest"). Requested model names are passed through
      verbatim — no silent aliasing.
    - Hugging Face Inference Providers (privacy="cloud", provider="hf")
      — requires HF_TOKEN or HUGGINGFACE_TOKEN. Model names like
      "deepseek-ai/DeepSeek-R1" or "Qwen/Qwen2.5-72B-Instruct" route here.

    Usage tracked in EISV (Energy consumption):
    - Model calls consume Energy
    - High usage → higher Energy → agent learns efficiency
    - Natural self-regulation

    Example:
    {
      "prompt": "Analyze this code for potential bugs",
      "model": "gemma4:latest",
      "task_type": "analysis",
      "max_tokens": 2048
    }
    """
    if not OPENAI_AVAILABLE:
        return [error_response(
            "OpenAI SDK required for model inference. Install with: pip install openai",
            error_code="DEPENDENCY_MISSING",
            error_category="system_error",
            recovery={
                "action": "Install OpenAI SDK",
                "related_tools": ["health_check"],
                "workflow": [
                    "1. Install: pip install openai",
                    "2. Restart MCP server",
                    "3. Retry call_model tool"
                ]
            }
        )]
    
    # Validate required parameter
    prompt, error = require_argument(arguments, "prompt")
    if error:
        return [error]
    
    # Get optional parameters
    model = arguments.get("model", "auto")  # auto, or any Ollama/HF model name
    task_type = arguments.get("task_type", "reasoning")  # reasoning, generation, analysis
    max_tokens = int(arguments.get("max_tokens", 2048))  # Must be int for Ollama
    temperature = float(arguments.get("temperature", 0.7))
    privacy = arguments.get("privacy", "local")  # local (Ollama default), auto, cloud
    provider = arguments.get("provider", "auto")  # auto, hf, ollama
    
    # Privacy routing: Force local if requested
    if privacy == "local" or provider == "ollama":
        # Route to Ollama (local). Model names pass through verbatim so
        # callers get a clean 404 if the model isn't pulled — no silent
        # aliasing to a model that may also be absent.
        base_url = "http://localhost:11434/v1"  # Ollama OpenAI-compatible API
        if model == "auto":
            model = os.getenv("UNITARES_LLM_MODEL", "gemma4:latest")
        api_key = "ollama"  # Dummy key - Ollama ignores it but OpenAI SDK requires non-None
        provider = "ollama"
        logger.info(f"Privacy mode: local - routing to Ollama with model {model}")
    elif provider == "hf" or (provider == "auto" and (model.startswith("deepseek-ai/") or model.startswith("openai/gpt-oss") or model.startswith("hf:") or model.startswith("Qwen/") or model.startswith("qwen/"))):
        # Hugging Face Inference Providers (free tier, OpenAI-compatible)
        base_url = "https://router.huggingface.co/v1"
        api_key = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")
        if not api_key:
            return [error_response(
                "HF_TOKEN or HUGGINGFACE_TOKEN required for Hugging Face Inference Providers",
                error_code="MISSING_CONFIG",
                error_category="system_error",
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
            )]
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
        logger.info(f"Using Hugging Face Inference Providers: {model}")
    elif provider == "auto":
        # Auto-select: Try Ollama first (local, free), then Gemini, then HF
        # Check if Ollama is available
        ollama_available = False
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            result = sock.connect_ex(('localhost', 11434))
            sock.close()
            ollama_available = (result == 0)
        except Exception:
            pass

        if ollama_available:
            # Prefer Ollama (local, free, no token needed)
            base_url = "http://localhost:11434/v1"
            api_key = "ollama"
            model = os.getenv("UNITARES_LLM_MODEL", "gemma4:latest") if model == "auto" else model
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
                return [error_response(
                    "No provider available. Ollama not running and HF_TOKEN not configured.",
                    error_code="MISSING_CONFIG",
                    error_category="system_error",
                    recovery={
                        "action": "Start Ollama (recommended) or set HF_TOKEN",
                        "related_tools": ["health_check"],
                        "workflow": [
                            "1. Install & run Ollama: ollama serve (recommended - free, local)",
                            "2. Or get HF token: https://huggingface.co/settings/tokens",
                            "3. Retry call_model tool"
                        ]
                    }
                )]
    else:
        # Unknown provider value. Pydantic schema (Literal["auto","hf","ollama"])
        # blocks this in normal MCP calls; only direct calls can reach here.
        return [error_response(
            f"Unknown provider '{provider}'. Expected one of: auto, hf, ollama.",
            error_code="INVALID_PROVIDER",
            error_category="validation_error",
        )]
    
    try:
        client = OpenAI(base_url=base_url, api_key=api_key)
        
        logger.debug(f"Calling model '{model}' via {base_url} for task_type='{task_type}'")
        
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature
        )
        
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
        agent_id = arguments.get("agent_id")
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
        
        # Determine routing method
        if "router.huggingface.co" in base_url:
            routed_via = "huggingface"
        elif "localhost" in base_url or "127.0.0.1" in base_url:
            routed_via = "ollama"
        else:
            routed_via = "direct"
        
        return success_response({
            "success": True,
            "response": result_text,
            "model_used": model_used,
            "tokens_used": tokens_used,
            "energy_cost": energy_cost,
            "routed_via": routed_via,
            "task_type": task_type,
            "message": f"Model inference completed via {routed_via}"
        }, agent_id=arguments.get("agent_id"), arguments=arguments)
        
    except Exception as e:
        logger.error(f"Model inference failed: {e}", exc_info=True)
        
        # Provide helpful error message
        error_msg = str(e)
        if "timeout" in error_msg.lower():
            error_code = "TIMEOUT"
            recovery_hint = "Try a shorter prompt or increase timeout"
        elif "rate limit" in error_msg.lower():
            error_code = "RATE_LIMIT_EXCEEDED"
            recovery_hint = "Wait a moment and retry, or use a different model"
        elif (
            ("localhost" in base_url or "127.0.0.1" in base_url)
            and any(marker in error_msg.lower() for marker in ("connection refused", "connection error", "failed to establish", "connect"))
        ):
            error_code = "MODEL_PROVIDER_UNAVAILABLE"
            recovery_hint = (
                "Ollama is not reachable. Start Ollama, or explicitly opt into fallback "
                "routing with privacy='auto' or privacy='cloud' and provider='hf'."
            )
        elif "not found" in error_msg.lower() or "invalid" in error_msg.lower():
            error_code = "MODEL_NOT_AVAILABLE"
            if "localhost" in base_url or "127.0.0.1" in base_url:
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
        
        return [error_response(
            f"Model inference failed: {error_msg}",
            error_code=error_code,
            error_category="system_error",
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
        )]

