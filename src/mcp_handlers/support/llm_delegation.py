"""
Internal LLM Delegation - Handler-to-handler model inference.

Provides internal interface for handlers to delegate reasoning tasks
to local/cloud LLMs via call_model infrastructure. Non-blocking and
graceful-failure by design.

Use cases:
- Knowledge synthesis (summarizing many discoveries)
- Anomaly explanation (interpreting governance patterns)
- Recovery coaching (generating personalized guidance)
- Background housekeeping (classifying, archiving decisions)

Usage:
    from .llm_delegation import synthesize_results, explain_anomaly

    # In any handler:
    synthesis = await synthesize_results(discoveries, query="error handling")
    if synthesis:
        response["synthesis"] = synthesis
"""

from typing import Optional, List, Dict, Any
import os
import json

from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
logger = get_logger(__name__)

# Check if OpenAI SDK available
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

def _get_ollama_client() -> Optional[Any]:
    """Get Ollama client if available."""
    if not OPENAI_AVAILABLE:
        return None

    try:
        # Ollama's OpenAI-compatible API
        return OpenAI(
            base_url="http://localhost:11434/v1",
            api_key="ollama"  # Required by SDK but ignored by Ollama
        )
    except Exception as e:
        logger.debug(f"Ollama client not available: {e}")
        return None

def _get_default_model() -> str:
    """Get default model for local inference."""
    # Check environment for override
    env_model = os.getenv("UNITARES_LLM_MODEL")
    if env_model:
        return env_model

    # gemma4 for governance coaching — needs real reasoning
    return "gemma4:latest"


def _reviewer_timeout(default: float = 120.0) -> float:
    """Timeout budget for a structured dialectic reviewer call. A paused agent
    awaiting recovery is not latency-critical, and a real structured antithesis
    on a local model can take >60s (gemma4 measured 43-70s+ on a Mac, variable),
    so the budget is generous. Tunable via UNITARES_DIALECTIC_REVIEWER_TIMEOUT."""
    raw = os.getenv("UNITARES_DIALECTIC_REVIEWER_TIMEOUT")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return default


def _wants_reasoning_effort_none(model: str) -> bool:
    """Thinking-mode models hide their answer behind a <think> block that
    /v1/chat/completions does not surface, so the whole token budget burns
    on invisible reasoning. reasoning.effort=none skips it and returns the
    final answer directly. Currently applies to qwen3 / qwen3.6 families."""
    if not model:
        return False
    m = model.lower()
    return m.startswith("qwen3") or m.startswith("qwen-3")

async def call_local_llm(
    prompt: str,
    model: Optional[str] = None,
    max_tokens: int = 2048,
    temperature: float = 0.7,
    timeout: float = 30.0
) -> Optional[str]:
    """
    Call local LLM (Ollama) for internal delegation.

    Non-blocking and graceful-failure - returns None if unavailable.
    Use for optional enhancements, not critical path operations.

    Args:
        prompt: The prompt to send to the model
        model: Model name (default: gemma4:latest)
        max_tokens: Maximum tokens in response
        temperature: Sampling temperature (0.0-1.0)
        timeout: Request timeout in seconds

    Returns:
        Model response text, or None if unavailable/failed
    """
    if not OPENAI_AVAILABLE:
        logger.warning("OpenAI SDK not available for local LLM delegation")
        return None

    client = _get_ollama_client()
    if not client:
        logger.warning("Ollama client could not be created")
        return None

    model = model or _get_default_model()

    # qwen3.x defaults to thinking-mode, which hides the final answer from
    # /v1/chat/completions — the entire token budget gets consumed by the
    # unsurfaced <think> block. ollama's OpenAI-compat layer honors
    # reasoning.effort=none, which skips thinking and returns the answer
    # directly. Other families ignore the field.
    extra_kwargs: Dict[str, Any] = {}
    if _wants_reasoning_effort_none(model):
        extra_kwargs["extra_body"] = {"reasoning": {"effort": "none"}}

    try:
        import asyncio

        # Run synchronous OpenAI call in executor to avoid blocking
        loop = asyncio.get_running_loop()

        def _call_sync():
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
                **extra_kwargs,
            )
            return response.choices[0].message.content

        result = await asyncio.wait_for(
            loop.run_in_executor(None, _call_sync),
            timeout=timeout + 5  # Extra buffer for executor overhead
        )

        logger.info(f"Local LLM call successful: model={model}, tokens≤{max_tokens}")
        return result

    except asyncio.TimeoutError:
        logger.warning(f"Local LLM timed out after {timeout}s (model={model})")
        return None
    except Exception as e:
        logger.warning(f"Local LLM call failed: {type(e).__name__}: {e}")
        return None


def _ollama_native_url() -> str:
    """Native Ollama /api/chat endpoint (supports JSON-schema-constrained output
    via the `format` field). Distinct from the OpenAI-compat /v1 base used by
    call_local_llm; the native endpoint is what was validated for structured
    dialectic output."""
    base = os.getenv("UNITARES_OLLAMA_BASE", "http://localhost:11434").rstrip("/")
    return base + "/api/chat"


async def call_local_llm_structured(
    messages: List[Dict[str, str]],
    schema: Dict[str, Any],
    model: Optional[str] = None,
    max_tokens: int = 2048,
    temperature: float = 0.7,
    timeout: float = 60.0,
) -> Optional[Dict[str, Any]]:
    """Call local LLM constrained to a JSON schema (Ollama native `format=`).

    Returns the parsed dict, or None on unavailability / timeout / malformed JSON.
    Graceful-failure by design — callers fall back to free-text generation.

    Design notes (learned from the 2026-06-02 reviewer experiment):
      - Thinking is NOT disabled. Constrained decoding + think-off degenerates
        (qwen returned a schema-valid object with every field empty). Let the
        model reason; the JSON is filled from that reasoning.
      - Schema field ORDER matters for autoregressive models: put reasoning
        fields before verdict/derived fields so the model reasons before it
        commits. Callers own the ordering.
      - JSON `format=` is the transport, not a reasoning constraint; a general
        (non-coding) model is the right default (see _get_default_model).
    """
    import asyncio
    import urllib.request

    model = model or _get_default_model()
    body = {
        "model": model,
        "messages": messages,
        "format": schema,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    payload = json.dumps(body).encode()
    url = _ollama_native_url()

    def _call_sync():
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.load(r)
        return resp.get("message", {}).get("content")

    try:
        loop = asyncio.get_running_loop()
        content = await asyncio.wait_for(
            loop.run_in_executor(None, _call_sync),
            timeout=timeout + 5,
        )
        if not content:
            logger.warning(f"Structured LLM returned empty content (model={model})")
            return None
        parsed = json.loads(content)
        logger.info(f"Structured LLM call successful: model={model}")
        return parsed if isinstance(parsed, dict) else None
    except asyncio.TimeoutError:
        logger.warning(f"Structured LLM timed out after {timeout}s (model={model})")
        return None
    except json.JSONDecodeError as e:
        logger.warning(f"Structured LLM returned non-JSON (model={model}): {e}")
        return None
    except Exception as e:
        logger.warning(f"Structured LLM call failed: {type(e).__name__}: {e}")
        return None


async def synthesize_results(
    discoveries: List[Dict[str, Any]],
    query: Optional[str] = None,
    max_discoveries: int = 8,
    max_tokens: int = 2048
) -> Optional[Dict[str, Any]]:
    """
    Synthesize knowledge graph search results into key insights.

    Called when search returns many results to help agents understand
    the key themes and actionable patterns.

    Args:
        discoveries: List of discovery dicts (with summary, type, tags)
        query: Original search query (for context)
        max_discoveries: Max discoveries to include in synthesis prompt (default 8 for speed)
        max_tokens: Max tokens for synthesis response (default 250 for speed)

    Returns:
        Dict with synthesis text and metadata, or None if unavailable
    """
    if not discoveries:
        return None

    # Build concise context from discoveries (keep prompt small for speed)
    discovery_summaries = []
    for i, d in enumerate(discoveries[:max_discoveries]):
        summary = d.get("summary", "")[:100]  # Truncate for speed
        dtype = d.get("type", "")
        discovery_summaries.append(f"{i+1}. [{dtype}] {summary}")

    discoveries_text = "\n".join(discovery_summaries)

    # Concise prompt for faster inference
    query_context = f"Query: '{query}'\n" if query else ""
    prompt = f"""{query_context}Discoveries found:
{discoveries_text}

Give 2-3 key insights in 2-3 sentences total. Be concise."""

    result = await call_local_llm(
        prompt=prompt,
        max_tokens=max_tokens,
        temperature=0.7,
        timeout=30.0
    )

    if not result:
        return None

    return {
        "text": result,
        "discoveries_analyzed": len(discovery_summaries),
        "query": query,
        "_note": "AI-synthesized summary via local LLM"
    }

async def explain_anomaly(
    agent_id: str,
    anomaly_type: str,
    description: str,
    metrics: Optional[Dict[str, Any]] = None,
    max_tokens: int = 2048
) -> Optional[str]:
    """
    Generate explanation for governance anomaly.

    Called when detect_anomalies finds unusual patterns to help
    operators understand root cause and recommended actions.

    Args:
        agent_id: Agent experiencing anomaly
        anomaly_type: Type of anomaly (risk_spike, coherence_drop, etc.)
        description: Anomaly description
        metrics: Optional EISV or other metrics for context
        max_tokens: Max tokens for explanation

    Returns:
        Explanation text, or None if unavailable
    """
    metrics_context = ""
    if metrics:
        metrics_context = f"\nCurrent metrics: {metrics}"

    prompt = f"""Agent '{agent_id[:20]}...' has a governance anomaly:
Type: {anomaly_type}
Description: {description}{metrics_context}

What might cause this anomaly and what should the agent do?
Give a brief root cause hypothesis and one concrete action."""

    return await call_local_llm(
        prompt=prompt,
        max_tokens=max_tokens,
        temperature=0.7
    )

async def generate_recovery_coaching(
    agent_id: str,
    blockers: List[str],
    current_state: Optional[Dict[str, Any]] = None,
    max_tokens: int = 2048
) -> Optional[str]:
    """
    Generate personalized recovery coaching for stuck agent.

    Called during self-recovery when agent is blocked to provide
    specific, actionable guidance.

    Args:
        agent_id: Agent needing recovery
        blockers: List of current blockers
        current_state: Optional governance state for context
        max_tokens: Max tokens for coaching

    Returns:
        Coaching text, or None if unavailable
    """
    blockers_text = "\n".join(f"- {b}" for b in blockers[:5])

    state_context = ""
    if current_state:
        eisv = current_state.get("eisv", {})
        if eisv:
            state_context = f"\nEISV metrics: E={eisv.get('E', '?'):.2f}, I={eisv.get('I', '?'):.2f}, S={eisv.get('S', '?'):.2f}, V={eisv.get('V', '?'):.2f}"

    prompt = f"""Agent is blocked by the following issues:
{blockers_text}{state_context}

What should this agent focus on first to recover?
Give ONE clear, specific action they can take right now."""

    return await call_local_llm(
        prompt=prompt,
        max_tokens=max_tokens,
        temperature=0.7
    )

# ==============================================================================
# DIALECTIC LLM DELEGATION
# ==============================================================================
# These functions enable LLM-assisted dialectic when no peer reviewer is available.
# The dialectic protocol (thesis/antithesis/synthesis) was designed for multi-agent
# coordination, but ephemeral agents make synchronous peer review impractical.
# Using local LLM as a "synthetic reviewer" preserves the dialectic structure
# while making single-agent recovery viable.
# ==============================================================================

async def generate_antithesis(
    thesis: Dict[str, Any],
    agent_state: Optional[Dict[str, Any]] = None,
    max_tokens: int = 2048
) -> Optional[Dict[str, Any]]:
    """
    Generate dialectic antithesis (counterargument) for a thesis.

    When no peer reviewer is available, use local LLM to provide
    the antithesis perspective - observing metrics, raising concerns,
    and offering counter-reasoning.

    Args:
        thesis: The thesis dict containing:
            - root_cause: Agent's understanding of what happened
            - proposed_conditions: Suggested recovery conditions
            - reasoning: Agent's explanation
        agent_state: Optional EISV state for context
        max_tokens: Max tokens for response

    Returns:
        Dict with antithesis components, or None if unavailable:
            - concerns: list[str] of specific concerns with the thesis
            - counter_reasoning: alternative perspective (prose)
            - grounding_cited: which independent signal the critique rests on
            - position: agree | dispute | refine
            - suggested_conditions: list[str] of modified/added conditions
            - raw_response: model output (JSON string, or prose on fallback)
            - _structured: True if JSON-schema path succeeded
            - _degraded: True if it fell back to free-text (fields best-effort)

    Strategy (2026-06-02 reviewer rework): a heterogeneous (non-Claude) model
    with a JSON-schema contract + an adversarial, grounding-citing prompt
    produces a substantive antithesis — replacing the brittle CONCERNS:/
    COUNTER-REASONING: text-scrape that silently degraded to empty fields. JSON
    is the transport; the schema orders reasoning fields BEFORE the verdict so
    the model reasons before it commits. Falls back to free-text capture (never
    silent-empty) if the structured call is unavailable or returns nothing.
    """
    root_cause = thesis.get("root_cause", "Unknown")
    proposed_conditions = thesis.get("proposed_conditions", [])
    reasoning = thesis.get("reasoning", "")

    conditions_text = "\n".join(f"  - {c}" for c in proposed_conditions[:5]) if proposed_conditions else "  (none proposed)"

    state_context = ""
    if agent_state:
        state_context = f"""
Independent governance signals you can ground critique in:
  - risk_score: {agent_state.get('risk_score', '?')}
  - coherence: {agent_state.get('coherence', '?')}
  - E (energy): {agent_state.get('E', '?')}
  - I (information integrity): {agent_state.get('I', '?')}
  - S (entropy): {agent_state.get('S', '?')}
  - V (valence): {agent_state.get('V', '?')}
"""

    system = (
        "You are an INDEPENDENT adversarial reviewer in a UNITARES governance "
        "dialectic. The paused agent wrote the thesis; your job is the ANTITHESIS "
        "— find what it underestimates or gets wrong. Do not be agreeable. Ground "
        "every concern in an independent governance signal (EISV state E/I/S/V, "
        "coherence, calibration, trajectory, audit history), NOT in re-reading the "
        "thesis's own prose. If after genuine scrutiny the thesis holds, set "
        "position=agree; otherwise dispute or refine."
    )
    user = (
        f"THESIS\n"
        f"Root cause (agent's view): {root_cause}\n"
        f"Proposed conditions:\n{conditions_text}\n"
        f"Reasoning: {reasoning[:400] if reasoning else '(none)'}\n"
        f"{state_context}"
    )

    # Reasoning fields FIRST so an autoregressive model reasons before it
    # commits to a verdict (position) or derived conditions.
    schema = {
        "type": "object",
        "properties": {
            "concerns": {
                "type": "array", "items": {"type": "string"}, "minItems": 2,
                "description": "Specific concerns the thesis underestimates",
            },
            "counter_reasoning": {"type": "string"},
            "grounding_cited": {
                "type": "string",
                "description": "Which independent signal (EISV/calibration/"
                               "trajectory/audit) the critique rests on",
            },
            "position": {"type": "string", "enum": ["agree", "dispute", "refine"]},
            "suggested_conditions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["concerns", "counter_reasoning", "grounding_cited",
                     "position", "suggested_conditions"],
    }

    parsed = await call_local_llm_structured(
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        schema=schema,
        max_tokens=max_tokens,
        temperature=0.7,
        timeout=_reviewer_timeout(),
    )

    if parsed and parsed.get("concerns"):
        return {
            "concerns": parsed.get("concerns", []),
            "counter_reasoning": parsed.get("counter_reasoning", ""),
            "grounding_cited": parsed.get("grounding_cited", ""),
            "position": parsed.get("position", "refine"),
            "suggested_conditions": parsed.get("suggested_conditions", []),
            "raw_response": json.dumps(parsed),
            "source": "llm_synthetic_reviewer",
            "_structured": True,
            "_degraded": False,
            "_note": "Heterogeneous structured-JSON adversarial reviewer",
        }

    # Fallback: structured path unavailable or returned empty. Capture free-text
    # so the antithesis is never silently empty (the prior bug).
    prose = await call_local_llm(
        prompt=system + "\n\n" + user + "\n\nWrite a critical antithesis with "
        "concrete concerns and the independent signal each rests on.",
        max_tokens=max_tokens, temperature=0.7, timeout=45.0,
    )
    if not prose:
        return None
    return {
        "concerns": [],
        "counter_reasoning": prose.strip()[:1500],
        "grounding_cited": "",
        "position": "refine",
        "suggested_conditions": [],
        "raw_response": prose,
        "source": "llm_synthetic_reviewer",
        "_structured": False,
        "_degraded": True,
        "_note": "Structured path unavailable; free-text antithesis captured",
    }

async def generate_synthesis(
    thesis: Dict[str, Any],
    antithesis: Dict[str, Any],
    synthesis_round: int = 1,
    max_tokens: int = 2048
) -> Optional[Dict[str, Any]]:
    """
    Generate dialectic synthesis - merging thesis and antithesis.

    Creates a resolution proposal that incorporates both perspectives,
    finding common ground while addressing concerns raised.

    Args:
        thesis: Original thesis from paused agent
        antithesis: Counterargument (from peer or LLM)
        synthesis_round: Current synthesis round (1-5)
        max_tokens: Max tokens for response

    Returns:
        Dict with synthesis components, or None if unavailable:
            - merged_conditions: Combined recovery conditions
            - agreed_root_cause: Consensus understanding
            - reasoning: How synthesis was reached
            - recommendation: RESUME, COOLDOWN, or ESCALATE
    """
    thesis_cause = thesis.get("root_cause", "Unknown")
    thesis_conditions = thesis.get("proposed_conditions", [])
    thesis_reasoning = thesis.get("reasoning", "")[:200]

    def _as_text(v) -> str:
        if isinstance(v, list):
            return "; ".join(str(x) for x in v)
        return str(v) if v else ""

    antithesis_concerns = _as_text(antithesis.get("concerns"))
    antithesis_counter = _as_text(antithesis.get("counter_reasoning"))
    antithesis_suggested = _as_text(antithesis.get("suggested_conditions"))

    thesis_cond_text = ", ".join(thesis_conditions[:3]) if thesis_conditions else "(none)"

    system = (
        "You are synthesizing a UNITARES governance dialectic between a paused "
        "agent (thesis) and an independent reviewer (antithesis). Produce a "
        "synthesis that genuinely integrates the reviewer's concerns — do not "
        "rubber-stamp the thesis. The recommendation must follow from the "
        "merged conditions, not from a desire to resume."
    )
    user = (
        f"THESIS (agent)\n"
        f"- Root cause: {thesis_cause}\n"
        f"- Proposed conditions: {thesis_cond_text}\n"
        f"- Reasoning: {thesis_reasoning}\n\n"
        f"ANTITHESIS (reviewer)\n"
        f"- Concerns: {antithesis_concerns[:400] if antithesis_concerns else '(none)'}\n"
        f"- Counter-reasoning: {antithesis_counter[:400] if antithesis_counter else '(none)'}\n"
        f"- Suggested conditions: {antithesis_suggested[:400] if antithesis_suggested else '(none)'}\n\n"
        f"This is synthesis round {synthesis_round}."
    )

    # Reasoning fields first; recommendation (the verdict) derived last.
    schema = {
        "type": "object",
        "properties": {
            "agreed_root_cause": {"type": "string"},
            "reasoning": {
                "type": "string",
                "description": "How the synthesis integrates both sides",
            },
            "merged_conditions": {
                "type": "array", "items": {"type": "string"},
                "description": "Recovery conditions that honor the reviewer's concerns",
            },
            "recommendation": {
                "type": "string", "enum": ["RESUME", "COOLDOWN", "ESCALATE"],
            },
        },
        "required": ["agreed_root_cause", "reasoning", "merged_conditions",
                     "recommendation"],
    }

    parsed = await call_local_llm_structured(
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        schema=schema,
        max_tokens=max_tokens,
        temperature=0.6,
        timeout=_reviewer_timeout(),
    )

    if parsed and parsed.get("recommendation"):
        rec = str(parsed.get("recommendation", "")).upper()
        rec = "RESUME" if "RESUME" in rec else "COOLDOWN" if "COOLDOWN" in rec \
            else "ESCALATE" if "ESCALATE" in rec else rec
        return {
            "agreed_root_cause": parsed.get("agreed_root_cause", ""),
            "reasoning": parsed.get("reasoning", ""),
            "merged_conditions": parsed.get("merged_conditions", []),
            "recommendation": rec,
            "raw_response": json.dumps(parsed),
            "synthesis_round": synthesis_round,
            "source": "llm_synthesis",
            "_structured": True,
            "_degraded": False,
            "_note": "Structured-JSON dialectic synthesis",
        }

    # Fallback: free-text synthesis, recommendation parsed leniently, ESCALATE
    # as the honest default when the model won't commit.
    prose = await call_local_llm(
        prompt=system + "\n\n" + user + "\n\nGive the agreed cause, merged "
        "conditions, a recommendation (RESUME/COOLDOWN/ESCALATE), and reasoning.",
        max_tokens=max_tokens, temperature=0.6, timeout=45.0,
    )
    if not prose:
        return None
    upper = prose.upper()
    rec = "RESUME" if "RESUME" in upper else "COOLDOWN" if "COOLDOWN" in upper \
        else "ESCALATE"
    return {
        "agreed_root_cause": "",
        "reasoning": prose.strip()[:1500],
        "merged_conditions": [],
        "recommendation": rec,
        "raw_response": prose,
        "synthesis_round": synthesis_round,
        "source": "llm_synthesis",
        "_structured": False,
        "_degraded": True,
        "_note": "Structured path unavailable; free-text synthesis captured",
    }

async def run_full_dialectic(
    thesis: Dict[str, Any],
    agent_state: Optional[Dict[str, Any]] = None,
    max_synthesis_rounds: int = 2
) -> Optional[Dict[str, Any]]:
    """
    Run a complete dialectic process: thesis -> antithesis -> synthesis.

    This is the main entry point for LLM-assisted dialectic recovery.
    When an agent is stuck and no peer reviewer is available, this
    runs the full dialectic protocol using local LLM as synthetic reviewer.

    Args:
        thesis: Agent's thesis with root_cause, proposed_conditions, reasoning
        agent_state: Current EISV metrics for context
        max_synthesis_rounds: Maximum synthesis iterations (default 2)

    Returns:
        Dict with complete dialectic result:
            - thesis: Original thesis
            - antithesis: Generated counterargument
            - synthesis: Final merged resolution
            - recommendation: RESUME/COOLDOWN/ESCALATE
            - success: Whether dialectic completed
    """
    result = {
        "thesis": thesis,
        "success": False,
        "source": "llm_full_dialectic"
    }

    # Generate antithesis
    antithesis = await generate_antithesis(thesis, agent_state)
    if not antithesis:
        result["error"] = "Failed to generate antithesis"
        return result

    result["antithesis"] = antithesis

    # Generate synthesis (may iterate)
    synthesis = None
    for round_num in range(1, max_synthesis_rounds + 1):
        synthesis = await generate_synthesis(
            thesis=thesis,
            antithesis=antithesis,
            synthesis_round=round_num
        )
        if synthesis and synthesis.get("recommendation"):
            break

    if not synthesis:
        result["error"] = "Failed to generate synthesis"
        return result

    result["synthesis"] = synthesis
    result["recommendation"] = synthesis.get("recommendation", "ESCALATE")
    result["success"] = True

    return result

async def is_llm_available() -> bool:
    """Check if local LLM is available for delegation."""
    if not OPENAI_AVAILABLE:
        return False

    client = _get_ollama_client()
    if not client:
        return False

    # Quick ping test
    try:
        import asyncio
        loop = asyncio.get_running_loop()

        def _ping():
            # List models endpoint is quick
            client.models.list()
            return True

        result = await asyncio.wait_for(
            loop.run_in_executor(None, _ping),
            timeout=2.0
        )
        return result
    except Exception:
        return False
